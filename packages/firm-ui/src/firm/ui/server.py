"""A tiny stdlib HTTP server for the dashboard (no web framework).

``ThreadingHTTPServer`` + ``BaseHTTPRequestHandler`` is plenty for a single-user, localhost ops
tool. This module is only the transport: it turns a socket request into a
:class:`firm.ui.app.UIRequest`, hands it to :class:`firm.ui.app.DashboardApp`, and writes the
:class:`firm.ui.app.UIResponse` back. Routing, pages, and actions all live in :mod:`firm.ui.app`,
which is what the framework mounts in :mod:`firm.ui.contrib` reuse.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import unquote, urlsplit

from .app import DashboardApp, Headers, UIRequest, UIResponse
from .auth import Authenticator
from .context import Dashboard


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler: type, app: DashboardApp) -> None:
        super().__init__(address, handler)
        self.app = app


class Handler(BaseHTTPRequestHandler):
    server_version = "firm-ui"

    def log_message(self, format: str, *args: object) -> None:  # keep the console quiet
        pass

    @property
    def _app(self) -> DashboardApp:
        return cast(DashboardServer, self.server).app

    def _request(self, body: bytes = b"") -> UIRequest:
        parsed = urlsplit(self.path)
        return UIRequest(
            method=self.command,
            # Decoded here so the routes see what a WSGI/ASGI framework would have handed them:
            # PATH_INFO and ASGI's scope["path"] are decoded before the application sees them.
            path=unquote(parsed.path),
            query=parsed.query,
            headers=Headers(self.headers.items()),
            body=body,
            peer=self.client_address[0],
        )

    def _write(self, response: UIResponse) -> None:
        self.send_response(response.status)
        for name, value in response.headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(response.body)))
        self.end_headers()
        if response.body:
            self.wfile.write(response.body)

    def _framing_ok(self) -> bool:
        """``BaseHTTPRequestHandler`` frames a body by ``Content-Length`` and nothing else, so a
        chunked request would leave its body in the socket to be read as the next request — the
        back half of a request-smuggling pair. Refuse anything that announces another framing."""
        if not self.headers.get("Transfer-Encoding"):
            return True
        self.close_connection = True
        headers = [("Content-Type", "text/plain; charset=utf-8")]
        self._write(UIResponse(400, headers, b"Unsupported Transfer-Encoding.\n"))
        return False

    def do_GET(self) -> None:
        if not self._framing_ok():
            return
        self._write(self._app.handle(self._request()))

    def do_POST(self) -> None:
        if not self._framing_ok():
            return
        # The body is read only if the app asks for it — it declines to before the request has
        # authenticated and passed the size limit. If it never asks, the announced bytes are still
        # in the socket, so the connection has to close: keep-alive would misparse them as the
        # next request.
        read = False

        def read_body() -> bytes:
            nonlocal read
            read = True
            return self.rfile.read(_to_int(self.headers.get("Content-Length", "0"), 0))

        response = self._app.handle(self._request(), read_body=read_body)
        if not read:
            self.close_connection = True
        self._write(response)


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
    channel_trim_retention: float | None = None,
) -> DashboardServer:
    """``channel_trim_retention`` (seconds) controls the trim button's cutoff; None keeps the
    1-day default. Set it to your app's ``Channel(message_retention=...)`` so a dashboard
    click never deletes messages your app still retains."""
    app = DashboardApp(dashboard, authenticator, channel_trim_retention)
    return DashboardServer((host, port), Handler, app)


def serve(
    dashboard: Dashboard,
    host: str = "127.0.0.1",
    port: int = 8787,
    *,
    authenticator: Authenticator | None = None,
    channel_trim_retention: float | None = None,
) -> None:
    """Create and run the dashboard server until interrupted. The caller owns ``dashboard`` and is
    responsible for closing it; this only manages the HTTP server's lifecycle."""
    server = create_server(
        dashboard,
        host,
        port,
        authenticator=authenticator,
        channel_trim_retention=channel_trim_retention,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
