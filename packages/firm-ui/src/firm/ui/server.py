"""A tiny stdlib HTTP server for the dashboard (no web framework).

``ThreadingHTTPServer`` + ``BaseHTTPRequestHandler`` is plenty for a single-user, localhost ops
tool. Routes are matched by hand; GETs render pages, POSTs run an action and redirect back. Each
route is guarded by whether that part (queue / cache / channel) is enabled on the dashboard.
"""

from __future__ import annotations

import re
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import cast
from urllib.parse import parse_qs, unquote, urlsplit

from firm._core.clock import now_utc

from . import actions, audit_queries, cache_queries, channel_queries, queries, render
from .auth import Allow, Authenticator, AuthRequest
from .context import Dashboard

# Per-page auto-refresh preference: one cookie per part, holding a value from
# render.REFRESH_OPTIONS (seconds, or 0 for off). Unset -> the page's historical default, so
# existing behaviour is unchanged until someone opens the control.
_REFRESH_DEFAULTS = {"queue": 5, "cache": 10, "channel": 10, "audit": 10}
_REFRESH_VALID = frozenset(secs for secs, _ in render.REFRESH_OPTIONS)
_REFRESH_COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year
# Dashboard POSTs carry at most a tiny settings form; anything bigger is abuse of the buffer.
_MAX_BODY_BYTES = 1 << 20

# Read once at import time -- the dashboard is a short-lived local process, not a place where
# hot-reloading the stylesheet from disk on every request would buy anything.
_STATIC_CSS = resources.files("firm.ui").joinpath("static", "style.css").read_bytes()


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type,
        dashboard: Dashboard,
        authenticator: Authenticator | None = None,
    ) -> None:
        super().__init__(address, handler)
        self.dashboard = dashboard
        self.authenticator = authenticator


class Handler(BaseHTTPRequestHandler):
    server_version = "firm-ui"

    def log_message(self, format: str, *args: object) -> None:  # keep the console quiet
        pass

    @property
    def _dash(self) -> Dashboard:
        return cast(DashboardServer, self.server).dashboard

    # -- responses -----------------------------------------------------------------------------

    def _html(self, body: str, status: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def _not_found(self) -> None:
        self._html(render.not_found(self._dash.parts), 404)

    def _static_css(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/css; charset=utf-8")
        self.send_header("Content-Length", str(len(_STATIC_CSS)))
        self.end_headers()
        self.wfile.write(_STATIC_CSS)

    def _origin_ok(self) -> bool:
        """Basic CSRF guard: a cross-origin browser form POST carries a mismatched ``Origin`` (or
        ``Referer``). Non-browser clients that send neither are allowed — the threat model is the
        operator's own browser being tricked into POSTing to this fixed localhost address."""
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if origin:
            return origin in (f"http://{host}", f"https://{host}")
        referer = self.headers.get("Referer")
        if referer:
            return urlsplit(referer).netloc == host
        return True

    def _check_auth(self) -> bool:
        """Run the configured authenticator (if any). On denial, write the response and return
        ``False`` so the caller stops; on success — or when no authenticator is set — return
        ``True``."""
        authenticator = cast(DashboardServer, self.server).authenticator
        if authenticator is None:
            return True
        req = AuthRequest(
            method=self.command,
            path=self.path,
            headers=self.headers,
            client_addr=self.client_address[0],
        )
        result = authenticator.authenticate(req)
        if isinstance(result, Allow):
            return True
        title = "Forbidden" if result.status == 403 else "Sign in"
        body = render.auth_page(title, result.message).encode("utf-8")
        self.send_response(result.status)
        for name, value in result.headers.items():
            self.send_header(name, value)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def _refresh_seconds(self, part: str) -> int:
        """The visitor's saved auto-refresh interval for ``part`` (0 = off), from its cookie, or
        that page's historical default if unset or the cookie holds an invalid value."""
        cookies = SimpleCookie()
        cookies.load(self.headers.get("Cookie", ""))
        morsel = cookies.get(f"firm_refresh_{part}")
        if morsel is not None:
            value = _to_int(morsel.value, -1)
            if value in _REFRESH_VALID:
                return value
        return _REFRESH_DEFAULTS[part]

    @staticmethod
    def _per_page(raw: str | None, default: int) -> int:
        """Validate a ``per_page`` query param against the shared table-size allowlist, falling
        back to ``default`` when it's missing or not one of the offered choices."""
        value = _to_int(raw or "", default)
        return value if value in render.TABLE_PER_PAGE_OPTIONS else default

    def _set_refresh(self, fields: dict[str, list[str]]) -> None:
        """Handle a refresh-control POST: validate, set the cookie, and redirect back to the page
        the visitor was on. ``return`` is restricted to a same-site relative path — it comes from a
        hidden form field, so this closes off using it as an open redirect."""
        part = fields.get("part", [""])[0]
        seconds = _to_int(fields.get("seconds", [""])[0], -1)
        return_to = fields.get("return", ["/"])[0]
        if part not in _REFRESH_DEFAULTS or seconds not in _REFRESH_VALID:
            self._not_found()
            return
        if not return_to.startswith("/") or return_to.startswith("//"):
            return_to = "/"
        cookie = SimpleCookie()
        name = f"firm_refresh_{part}"
        cookie[name] = str(seconds)
        cookie[name]["path"] = "/"
        cookie[name]["max-age"] = _REFRESH_COOKIE_MAX_AGE
        self.send_response(303)
        self.send_header("Location", return_to)
        self.send_header("Set-Cookie", cookie[name].OutputString())
        self.end_headers()

    # -- routing -------------------------------------------------------------------------------

    def do_GET(self) -> None:
        if not self._check_auth():
            return
        dash = self._dash
        parsed = urlsplit(self.path)
        params = parse_qs(parsed.query)
        try:
            if parsed.path in ("/", ""):
                self._landing()
            elif parsed.path == "/static/style.css":
                self._static_css()
            elif parsed.path == "/jobs" and dash.queue is not None:
                state = params.get("state", ["ready"])[0]
                page = max(1, _to_int(params.get("page", ["1"])[0], 1))
                self._jobs(
                    state, page, params.get("queue", [None])[0], params.get("per_page", [None])[0]
                )
            elif (match := re.fullmatch(r"/job/(\d+)", parsed.path)) and dash.queue is not None:
                self._job(int(match.group(1)), params.get("queue", [None])[0])
            elif parsed.path == "/cache" and dash.cache is not None:
                self._cache(
                    max(1, _to_int(params.get("page", ["1"])[0], 1)),
                    params.get("per_page", [None])[0],
                )
            elif parsed.path == "/channels" and dash.channel is not None:
                self._channels(
                    max(1, _to_int(params.get("top_page", ["1"])[0], 1)),
                    params.get("top_per_page", [None])[0],
                    max(1, _to_int(params.get("page", ["1"])[0], 1)),
                    params.get("per_page", [None])[0],
                )
            elif parsed.path == "/audit" and dash.audit is not None:
                self._audit(
                    params.get("action", [None])[0],
                    params.get("subject", [None])[0],
                    params.get("actor", [None])[0],
                    params.get("correlation_id", [None])[0],
                    params.get("sort", [None])[0],
                    params.get("dir", [None])[0],
                    max(1, _to_int(params.get("page", ["1"])[0], 1)),
                    params.get("per_page", [None])[0],
                )
            elif (match := re.fullmatch(r"/audit/(\d+)", parsed.path)) and dash.audit is not None:
                self._audit_detail(int(match.group(1)))
            else:
                self._not_found()
        except Exception as exc:  # never crash the server on a bad request
            self._html(render.error_page(dash.parts, repr(exc)), 500)

    def do_POST(self) -> None:
        dash = self._dash
        path = urlsplit(self.path).path
        if not self._check_auth():
            # The body was never read: close the connection instead of letting keep-alive
            # misparse the unread bytes as the next request.
            self.close_connection = True
            return
        length = _to_int(self.headers.get("Content-Length", "0"), 0)
        if length > _MAX_BODY_BYTES:
            self.close_connection = True
            self._html(render.error_page(dash.parts, "Request body too large."), 413)
            return
        raw_body = self.rfile.read(length)  # only /settings/refresh reads form fields from this
        if not self._origin_ok():
            body = render.error_page(dash.parts, "Cross-origin POST rejected (CSRF guard).")
            self._html(body, 403)
            return
        try:
            if path == "/settings/refresh":
                self._set_refresh(parse_qs(raw_body.decode("utf-8")))
            elif (m := re.fullmatch(r"/queue/(.+)/pause", path)) and dash.queue is not None:
                actions.pause(dash.queue, unquote(m.group(1)))
                self._redirect("/")
            elif (m := re.fullmatch(r"/queue/(.+)/resume", path)) and dash.queue is not None:
                actions.resume(dash.queue, unquote(m.group(1)))
                self._redirect("/")
            elif (m := re.fullmatch(r"/job/(\d+)/retry", path)) and dash.queue is not None:
                actions.retry(dash.queue, int(m.group(1)))
                self._redirect("/jobs?state=failed")
            elif (m := re.fullmatch(r"/job/(\d+)/discard", path)) and dash.queue is not None:
                actions.discard(dash.queue, int(m.group(1)))
                self._redirect("/jobs?state=failed")
            elif path == "/failed/retry-all" and dash.queue is not None:
                actions.retry_all(dash.queue)
                self._redirect("/jobs?state=failed")
            elif path == "/cache/clear" and dash.cache is not None:
                actions.clear_cache(dash.cache)
                self._redirect("/cache")
            elif path == "/channels/trim" and dash.channel is not None:
                actions.trim_channel(dash.channel)
                self._redirect("/channels")
            else:
                self._not_found()
        except Exception as exc:
            self._html(render.error_page(dash.parts, repr(exc)), 500)

    # -- pages ---------------------------------------------------------------------------------

    def _landing(self) -> None:
        dash = self._dash
        if dash.queue is not None:
            self._overview()
        elif dash.cache is not None:
            self._redirect("/cache")
        elif dash.channel is not None:
            self._redirect("/channels")
        elif dash.audit is not None:
            self._redirect("/audit")
        else:
            self._html(render.empty_page(dash.parts))

    def _overview(self) -> None:
        dash = self._dash
        assert dash.queue is not None  # guarded by the route check before this is called
        now = now_utc()
        with dash.queue.engine.connect() as conn:
            body = render.overview_page(
                dash.parts,
                queries.state_counts(conn),
                queries.queue_rows(conn, now),
                queries.processes(conn, now),
                queries.recurring(conn),
                refresh=self._refresh_seconds("queue"),
                request_path=self.path,
            )
        self._html(body)

    def _jobs(self, state: str, page: int, queue: str | None, per_page: str | None) -> None:
        dash = self._dash
        assert dash.queue is not None
        if state not in queries.STATES:
            state = "ready"
        per_page_n = self._per_page(per_page, render.JOBS_DEFAULT_PER_PAGE)
        offset = (page - 1) * per_page_n
        with dash.queue.engine.connect() as conn:
            jobs = queries.jobs_by_state(conn, state, limit=per_page_n, offset=offset, queue=queue)
            counts = queries.state_counts(conn, queue=queue)
        body = render.jobs_page(
            dash.parts,
            state,
            jobs,
            page,
            per_page_n,
            counts,
            queue=queue,
            refresh=self._refresh_seconds("queue"),
            request_path=self.path,
        )
        self._html(body)

    def _job(self, job_id: int, queue: str | None) -> None:
        dash = self._dash
        assert dash.queue is not None
        with dash.queue.engine.connect() as conn:
            job = queries.job_detail(conn, job_id)
        if job is None:
            self._not_found()
        else:
            self._html(render.job_page(dash.parts, job, queue=queue))

    def _cache(self, page: int, per_page: str | None) -> None:
        dash = self._dash
        assert dash.cache is not None
        per_page_n = self._per_page(per_page, render.CACHE_DEFAULT_PER_PAGE)
        offset = (page - 1) * per_page_n
        with dash.cache.connect() as conn:
            body = render.cache_page(
                dash.parts,
                cache_queries.cache_stats(conn),
                cache_queries.cache_recent(conn, limit=per_page_n, offset=offset),
                page=page,
                per_page=per_page_n,
                refresh=self._refresh_seconds("cache"),
                request_path=self.path,
            )
        self._html(body)

    def _channels(
        self, top_page: int, top_per_page: str | None, page: int, per_page: str | None
    ) -> None:
        dash = self._dash
        assert dash.channel is not None
        top_per_page_n = self._per_page(top_per_page, render.CHANNEL_TOP_DEFAULT_PER_PAGE)
        per_page_n = self._per_page(per_page, render.CHANNEL_MSG_DEFAULT_PER_PAGE)
        top_offset = (top_page - 1) * top_per_page_n
        offset = (page - 1) * per_page_n
        with dash.channel.connect() as conn:
            body = render.channel_page(
                dash.parts,
                channel_queries.channel_stats(conn),
                channel_queries.channel_top(conn, limit=top_per_page_n, offset=top_offset),
                channel_queries.channel_recent(conn, limit=per_page_n, offset=offset),
                top_page=top_page,
                top_per_page=top_per_page_n,
                page=page,
                per_page=per_page_n,
                refresh=self._refresh_seconds("channel"),
                request_path=self.path,
            )
        self._html(body)

    def _audit(
        self,
        action: str | None,
        subject: str | None,
        actor: str | None,
        correlation_id: str | None,
        sort: str | None,
        dir: str | None,
        page: int,
        per_page: str | None,
    ) -> None:
        dash = self._dash
        assert dash.audit is not None
        sort = sort if sort in audit_queries.SORT_COLUMNS else render.AUDIT_DEFAULT_SORT
        dir = dir if dir in ("asc", "desc") else render.AUDIT_DEFAULT_DIR
        per_page_n = self._per_page(per_page, render.AUDIT_DEFAULT_PER_PAGE)
        offset = (page - 1) * per_page_n
        with dash.audit.connect() as conn:
            body = render.audit_page(
                dash.parts,
                audit_queries.audit_stats(conn),
                audit_queries.audit_search(
                    conn,
                    action=action,
                    subject=subject,
                    actor=actor,
                    correlation_id=correlation_id,
                    sort=sort,
                    dir=dir,
                    limit=per_page_n,
                    offset=offset,
                ),
                {
                    "action": action or "",
                    "subject": subject or "",
                    "actor": actor or "",
                    "correlation_id": correlation_id or "",
                },
                total=audit_queries.audit_count(
                    conn,
                    action=action,
                    subject=subject,
                    actor=actor,
                    correlation_id=correlation_id,
                ),
                page=page,
                per_page=per_page_n,
                sort=sort,
                dir=dir,
                refresh=self._refresh_seconds("audit"),
                request_path=self.path,
            )
        self._html(body)

    def _audit_detail(self, event_id: int) -> None:
        dash = self._dash
        assert dash.audit is not None
        with dash.audit.connect() as conn:
            event = audit_queries.audit_detail(conn, event_id)
        if event is None:
            self._not_found()
        else:
            self._html(render.audit_detail_page(dash.parts, event))


def _to_int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def create_server(
    dashboard: Dashboard,
    host: str,
    port: int,
    *,
    authenticator: Authenticator | None = None,
) -> DashboardServer:
    return DashboardServer((host, port), Handler, dashboard, authenticator)


def serve(
    dashboard: Dashboard,
    host: str = "127.0.0.1",
    port: int = 8787,
    *,
    authenticator: Authenticator | None = None,
) -> None:
    """Create and run the dashboard server until interrupted. The caller owns ``dashboard`` and is
    responsible for closing it; this only manages the HTTP server's lifecycle."""
    server = create_server(dashboard, host, port, authenticator=authenticator)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
