"""HTML rendering — Jinja2 templates (``templates/``) plus the data/formatting glue that feeds
them, no JS framework.

Actions are ordinary POST forms (so the UI works with JavaScript disabled); auto-refreshing pages
use a ``<meta refresh>``. The stylesheet is a static file (``static/style.css``) and the few icons
are inline SVG, so the dashboard loads no external fonts, scripts, or stylesheets. Templates
autoescape by default; helpers whose output is trusted markup (icons, URLs built from
already-encoded pieces) are wrapped in ``Markup`` so they pass through unescaped.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from jinja2 import Environment, PackageLoader, select_autoescape
from jinjax import Catalog
from jinjax.jinjax import JinjaX
from markupsafe import Markup

from firm._core.clock import now_utc

from .queries import STATES

_PART_NAV = {
    "queue": ("Queue", "/"),
    "cache": ("Cache", "/cache"),
    "channel": ("Channels", "/channels"),
    "audit": ("Audit", "/audit"),
}

# Inline SVG icons (stroke = currentColor, so they inherit the surrounding text colour).
_ICONS = {
    "pause": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>',
    "play": '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>',
    "retry": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.6-6.3"/><path d="M21 3v6h-6"/></svg>',
    "trash": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M10 11v6M14 11v6M5 7l1 12a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2l1-12M9 7V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v3"/></svg>',
    "scissors": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><line x1="8.5" y1="8.5" x2="20" y2="20"/><line x1="8.5" y1="15.5" x2="20" y2="4"/></svg>',
    "back": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>',
    "x": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>',
    "empty": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 14h4l2 3h6l2-3h4"/><path d="M5 14V6a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v8"/></svg>',
    "chevron": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>',
    "check": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>',
    "filter": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5h16l-6 7.5V19l-4 2v-8.5L4 5z"/></svg>',
}

# The auto-refresh interval choices, in display order; 0 means off. Shared by the header control
# (render.py) and the cookie-value allowlist the server checks against (server.py).
REFRESH_OPTIONS = [(0, "off"), (5, "5s"), (10, "10s"), (30, "30s"), (60, "1m"), (300, "5m")]

# A tiny self-contained firm favicon (data URI) so the page makes no external request — the same
# rock mark used in the header brand.
_FAVICON = "data:image/svg+xml," + quote(
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>"
    "<text x='50' y='72' font-size='72' text-anchor='middle'>\U0001faa8</text>"
    "</svg>"
)


def _confirm(message: str) -> str:
    """An ``onsubmit`` guard for destructive POSTs: it prompts when JavaScript is enabled and
    submits unchanged when it is not, so the no-JS path is unaffected. ``message`` must contain no
    apostrophes or double quotes (it sits inside a single-quoted JS string in an HTML attribute)."""
    return f" onsubmit=\"return confirm('{message}')\""


def _num(value: Any) -> str:
    # Plain (unescaped) text -- Jinja autoescapes it at the `{{ }}` call site.
    return f"{value:,}" if isinstance(value, int) else str(value)


def _dt(value: datetime | None) -> str:
    return "—" if value is None else value.strftime("%Y-%m-%d %H:%M:%S")


def _humanize(secs: float) -> str:
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _reltime(value: datetime | None) -> str:
    """A compact relative time like ``3s ago`` / ``in 5m`` (scheduled jobs sit in the future)."""
    if value is None:
        return "—"
    delta = (now_utc() - value).total_seconds()
    if -1 < delta < 5:
        return "just now"
    return f"in {_humanize(-delta)}" if delta < 0 else f"{_humanize(delta)} ago"


_CRON_WEEKDAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")

_CRON_NICKNAMES = {
    "@yearly": "Yearly on January 1 at 00:00",
    "@annually": "Yearly on January 1 at 00:00",
    "@monthly": "Monthly on day 1 at 00:00",
    "@weekly": "Weekly on Sunday at 00:00",
    "@daily": "Daily at 00:00",
    "@midnight": "Daily at 00:00",
    "@hourly": "Every hour",
}


def _cron_step(field: str) -> int | None:
    if not field.startswith("*/"):
        return None
    n = field[2:]
    return int(n) if n.isdigit() and int(n) > 0 else None


def _describe_cron(expr: str) -> str | None:
    """Best-effort English description of a standard 5-field cron expression.

    Covers the common shapes only (every-N minutes/hours, daily/weekly/monthly at a fixed
    time); anything else -- ranges, lists, step values on day/month -- returns ``None`` so the
    caller falls back to the raw expression instead of risking a wrong description.
    """
    expr = expr.strip()
    if expr in _CRON_NICKNAMES:
        return _CRON_NICKNAMES[expr]

    parts = expr.split()
    if len(parts) != 5:
        return None
    minute, hour, day, month, weekday = parts
    if month != "*":
        return None

    minute_step = _cron_step(minute)
    if minute_step and hour == day == weekday == "*":
        return "Every minute" if minute_step == 1 else f"Every {minute_step} minutes"
    if not minute.isdigit():
        return None
    m = int(minute)

    hour_step = _cron_step(hour)
    if hour_step and day == weekday == "*":
        suffix = f", at minute {m}" if m else ""
        return f"Every hour{suffix}" if hour_step == 1 else f"Every {hour_step} hours{suffix}"
    if hour == "*":
        return f"Every hour, at minute {m}" if m else "Every hour"
    if not hour.isdigit():
        return None
    time = f"{int(hour):02d}:{m:02d}"

    if day == "*" and weekday == "*":
        return f"Daily at {time}"
    if day == "*" and weekday.isdigit() and 0 <= int(weekday) <= 7:
        return f"Weekly on {_CRON_WEEKDAYS[int(weekday) % 7]} at {time}"
    if weekday == "*" and day.isdigit():
        return f"Monthly on day {int(day)} at {time}"
    return None


def _bytes(n: int | float) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


def _json_preview(value: dict[str, Any] | None) -> str:
    if not value:
        return ""
    return json.dumps(value, default=str, ensure_ascii=False, separators=(",", ":"))


def _json_pretty(value: dict[str, Any] | None) -> str:
    if not value:
        return ""
    return json.dumps(value, default=str, ensure_ascii=False, indent=2)


def _jobs_href(state: str, queue: str | None = None) -> str:
    q = f"&queue={quote(queue, safe='')}" if queue else ""
    return f"/jobs?state={state}{q}"


JOBS_DEFAULT_PER_PAGE = 50


def _jobs_list_href(
    state: str, queue: str | None, *, page: int = 1, per_page: int = JOBS_DEFAULT_PER_PAGE
) -> str:
    """Like :func:`_jobs_href`, but also carries this list's own page/page-size — kept separate
    since switching state or queue is meant to land back on page 1 at the default size."""
    params = {"state": state}
    if queue:
        params["queue"] = queue
    if page > 1:
        params["page"] = str(page)
    if per_page != JOBS_DEFAULT_PER_PAGE:
        params["per_page"] = str(per_page)
    return f"/jobs?{urlencode(params, quote_via=quote)}"


# Shared "how many rows" choices for every paginated table (jobs list, audit list).
TABLE_PER_PAGE_OPTIONS = (10, 25, 50, 100)


def _markup_fn(fn: Any) -> Any:
    """Wrap a URL-builder so Jinja treats its return value as trusted markup -- these already
    percent-encode their own params (via ``quote``/``urlencode``), and historically were never
    HTML-escaped either (the ``&`` between query params has always been rendered raw)."""

    def wrapped(*args: Any, **kwargs: Any) -> Markup:
        return Markup(fn(*args, **kwargs))

    return wrapped


_ENV = Environment(
    loader=PackageLoader("firm.ui", "templates"),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
_ENV.filters.update(
    num=_num,
    dt=_dt,
    humanize=_humanize,
    reltime=_reltime,
    filesize=_bytes,
    json_preview=_json_preview,
    json_pretty=_json_pretty,
    urlquote=lambda s: quote(s, safe=""),
)
_GLOBALS: dict[str, Any] = {
    "icons": {name: Markup(svg) for name, svg in _ICONS.items()},
    "favicon": _FAVICON,
    "confirm": lambda message: Markup(_confirm(message)),
    "jobs_href": _markup_fn(_jobs_href),
}
_ENV.globals.update(_GLOBALS)

# The presentation layer is a set of real components under ``templates/components/*.jinja``
# (``<Card/>``, ``<Pagination/>``, ...), rendered by JinjaX. The catalog copies this module's
# Jinja globals/filters at construction, so components can use ``num``, ``dt``,
# ``icons`` etc.; adding the ``JinjaX`` extension to ``_ENV`` lets the page templates invoke
# components with the ``<Component/>`` tag syntax. Components decide *markup only* -- every value
# and URL they render is computed here in Python and passed in (see the helpers below).
_CATALOG = Catalog(jinja_env=_ENV)
_CATALOG.jinja_env.autoescape = True  # components are .jinja, so escape-by-filename wouldn't fire
_CATALOG.jinja_env.trim_blocks = True
_CATALOG.jinja_env.lstrip_blocks = True
_CATALOG.add_folder(str(Path(__file__).resolve().parent / "templates" / "components"))
_ENV.add_extension(JinjaX)


# -- component data builders -------------------------------------------------------------------
# These turn raw values into the plain dicts/lists the components render. Keeping the branching
# and arithmetic here (instead of inside the templates) is what makes the components "dumb".


def _card(
    label: str,
    number: str,
    *,
    state: str | None = None,
    href: str | None = None,
    failed: bool = False,
) -> dict[str, Any]:
    return {"label": label, "number": number, "state": state, "href": href, "failed": failed}


def _bar_pct(value: float, max_value: float) -> int:
    """A bar width as an integer 0-100 percent of ``max_value`` (clamped, like the old macro)."""
    return max(0, min(100, int(100 * value / max_value)))


def _latency_tag(secs: float) -> dict[str, str]:
    cls = "ok" if secs < 10 else ("warn" if secs < 60 else "danger")
    text = f"{secs:.1f}s" if secs < 10 else _humanize(secs)
    return {"cls": cls, "text": text}


def _pagination(page: int, per_page: int, total: int, href_for: Any) -> dict[str, Any]:
    """The whole pager as data: the shown range, the clamped current page, and the
    first/prev/next/last URLs (``None`` where that link should not appear)."""
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(page, 1), total_pages)
    return {
        "start": 0 if total == 0 else (page - 1) * per_page + 1,
        "end": min(page * per_page, total),
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "first": Markup(href_for(1)) if page > 2 else None,
        "prev": Markup(href_for(page - 1)) if page > 1 else None,
        "next": Markup(href_for(page + 1)) if page < total_pages else None,
        "last": Markup(href_for(total_pages)) if page < total_pages - 1 else None,
    }


def _pagesize_options(current: int, href_for: Any) -> list[dict[str, Any]]:
    return [
        {"n": n, "url": Markup(href_for(n)), "active": n == current} for n in TABLE_PER_PAGE_OPTIONS
    ]


def _statenav_items(
    active_state: str, counts: dict[str, int] | None, queue: str | None
) -> list[dict[str, Any]]:
    return [
        {
            "state": state,
            "url": Markup(_jobs_href(state, queue)),
            "count": _num(counts[state]) if counts is not None else None,
            "active": state == active_state,
        }
        for state in STATES
    ]


def _header_tabs(parts: list[str], active_part: str) -> list[dict[str, Any]]:
    return [
        {"label": _PART_NAV[part][0], "href": _PART_NAV[part][1], "active": part == active_part}
        for part in parts
    ]


def _refresh_label(refresh: int) -> str:
    return "auto-refresh off" if not refresh else f"auto-refresh {refresh}s"


def _refresh_choices(refresh: int) -> list[dict[str, Any]]:
    return [
        {"seconds": secs, "label": label, "active": secs == refresh}
        for secs, label in REFRESH_OPTIONS
    ]


# Detail-page key/value cells hold small pre-rendered fragments; rendering the relevant component
# straight from Python keeps a single source of truth (the ``.jinja`` file) for that markup.
# ``Catalog.render`` is typed as returning ``str``, so re-wrap as ``Markup`` (idempotent) to keep
# the fragment from being re-escaped when the cell is rendered.
def _mono(value: Any) -> Markup:
    return Markup(_CATALOG.render("Mono", value=value))


def _dash() -> Markup:
    return Markup(_CATALOG.render("Dash"))


def _when(value: datetime | None) -> Markup:
    return Markup(_CATALOG.render("When", value=value))


# Context defaults for the layout chrome -- always present so `layout.html` never has to guard
# against an undefined variable; a page-specific kwarg of the same name overrides it.
_LAYOUT_DEFAULTS = {
    "substate": None,
    "substate_counts": None,
    "queue": None,
    "refresh": None,
    "request_path": None,
}


def _render(template_name: str, **context: Any) -> str:
    ctx = {**_LAYOUT_DEFAULTS, **context}
    # Build the chrome (header tabs, refresh control, state sub-nav) that layout.html renders on
    # every page, so its components receive ready-made data instead of computing it in-template.
    ctx["request_path"] = ctx["request_path"] or "/"
    ctx["header_tabs"] = _header_tabs(ctx["parts"], ctx["active_part"])
    refresh = ctx["refresh"]
    ctx["refresh_label"] = _refresh_label(refresh) if refresh is not None else ""
    ctx["refresh_choices"] = _refresh_choices(refresh) if refresh is not None else []
    ctx["statenav_items"] = (
        _statenav_items(ctx["substate"], ctx["substate_counts"], ctx["queue"])
        if ctx["substate"] is not None
        else None
    )
    return _ENV.get_template(template_name).render(**ctx)


# -- queue pages -------------------------------------------------------------------------------


def overview_page(
    parts: list[str],
    counts: dict[str, int],
    queue_rows: list[dict[str, Any]],
    processes: list[dict[str, Any]],
    recurring: list[dict[str, Any]],
    *,
    refresh: int = 5,
    request_path: str = "/",
) -> str:
    cards = [
        _card(
            state,
            _num(counts.get(state, 0)),
            state=state,
            href=f"/jobs?state={state}",
            failed=(state == "failed" and counts.get(state, 0) > 0),
        )
        for state in STATES
    ]
    cards.append(_card("total", _num(counts.get("total", 0))))
    max_size = max((q["size"] for q in queue_rows), default=0) or 1
    for q in queue_rows:
        q["bar_pct"] = _bar_pct(q["size"], max_size)
        q["latency_tag"] = _latency_tag(q["latency"])
    for task in recurring:
        task["schedule_desc"] = _describe_cron(task["schedule"]) or ""
    return _render(
        "overview.html",
        title="Overview",
        parts=parts,
        active_part="queue",
        refresh=refresh,
        request_path=request_path,
        cards=cards,
        counts=counts,
        queue_rows=queue_rows,
        processes=processes,
        recurring=recurring,
    )


def jobs_page(
    parts: list[str],
    state: str,
    jobs: list[dict[str, Any]],
    page: int,
    per_page: int,
    counts: dict[str, int],
    *,
    queue: str | None = None,
    refresh: int = 5,
    request_path: str = "/jobs",
) -> str:
    total = counts.get(state, 0)
    return _render(
        "jobs.html",
        title=f"{state} jobs",
        parts=parts,
        active_part="queue",
        substate=state,
        substate_counts=counts,
        queue=queue,
        refresh=refresh,
        request_path=request_path,
        state=state,
        jobs=jobs,
        counts=counts,
        per_page=per_page,
        pagination=_pagination(
            page,
            per_page,
            total,
            lambda n: _jobs_list_href(state, queue, page=n, per_page=per_page),
        ),
        pagesize=_pagesize_options(per_page, lambda n: _jobs_list_href(state, queue, per_page=n)),
    )


def job_page(parts: list[str], job: dict[str, Any], *, queue: str | None = None) -> str:
    cells = [
        ("class", _mono(job["class_name"])),
        ("queue", job["queue_name"]),
        ("priority", job["priority"]),
        ("attempts", job["attempts"]),
        ("scheduled", _when(job["scheduled_at"])),
        ("finished", _when(job["finished_at"])),
        ("concurrency key", _mono(job["concurrency_key"]) if job["concurrency_key"] else _dash()),
        ("process", job["process_id"] if job["process_id"] is not None else _dash()),
        ("created", _when(job["created_at"])),
    ]
    return _render(
        "job_detail.html",
        title=f"Job #{job['id']}",
        parts=parts,
        active_part="queue",
        substate=job["state"],
        queue=queue,
        job=job,
        cells=cells,
    )


# -- cache page --------------------------------------------------------------------------------


CACHE_DEFAULT_PER_PAGE = 50


def _cache_href(*, page: int = 1, per_page: int = CACHE_DEFAULT_PER_PAGE) -> str:
    params = {}
    if page > 1:
        params["page"] = str(page)
    if per_page != CACHE_DEFAULT_PER_PAGE:
        params["per_page"] = str(per_page)
    return f"/cache?{urlencode(params, quote_via=quote)}" if params else "/cache"


def cache_page(
    parts: list[str],
    stats: dict[str, int],
    entries: list[dict[str, Any]],
    *,
    page: int = 1,
    per_page: int = CACHE_DEFAULT_PER_PAGE,
    refresh: int = 10,
    request_path: str = "/cache",
) -> str:
    cards = [
        _card("entries", _num(stats["entries"])),
        _card("est. size", _bytes(stats["estimated_size"])),
        _card(
            "avg / entry",
            _bytes(stats["estimated_size"] / stats["entries"]) if stats["entries"] else "—",
        ),
    ]
    max_b = max((e["byte_size"] for e in entries), default=0) or 1
    for e in entries:
        e["bar_pct"] = _bar_pct(e["byte_size"], max_b)
    return _render(
        "cache.html",
        title="Cache",
        parts=parts,
        active_part="cache",
        refresh=refresh,
        request_path=request_path,
        cards=cards,
        entries=entries,
        per_page=per_page,
        pagination=_pagination(
            page, per_page, stats["entries"], lambda p: _cache_href(page=p, per_page=per_page)
        ),
        pagesize=_pagesize_options(per_page, lambda p: _cache_href(per_page=p)),
    )


# -- channel page ------------------------------------------------------------------------------


CHANNEL_TOP_DEFAULT_PER_PAGE = 25
CHANNEL_MSG_DEFAULT_PER_PAGE = 50


def _channels_href(
    *,
    top_page: int = 1,
    top_per_page: int = CHANNEL_TOP_DEFAULT_PER_PAGE,
    page: int = 1,
    per_page: int = CHANNEL_MSG_DEFAULT_PER_PAGE,
) -> str:
    """Both tables on this page paginate independently but share one URL, so every link carries
    the *other* table's current page/size through unchanged — paginating messages never resets
    which page of busiest-channels you were looking at, and vice versa."""
    params = {}
    if top_page > 1:
        params["top_page"] = str(top_page)
    if top_per_page != CHANNEL_TOP_DEFAULT_PER_PAGE:
        params["top_per_page"] = str(top_per_page)
    if page > 1:
        params["page"] = str(page)
    if per_page != CHANNEL_MSG_DEFAULT_PER_PAGE:
        params["per_page"] = str(per_page)
    return f"/channels?{urlencode(params, quote_via=quote)}" if params else "/channels"


def channel_page(
    parts: list[str],
    stats: dict[str, int],
    top: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    *,
    top_page: int = 1,
    top_per_page: int = CHANNEL_TOP_DEFAULT_PER_PAGE,
    page: int = 1,
    per_page: int = CHANNEL_MSG_DEFAULT_PER_PAGE,
    refresh: int = 10,
    request_path: str = "/channels",
) -> str:
    cards = [
        _card("messages", _num(stats["messages"])),
        _card("channels", _num(stats["channels"])),
        _card("last message", _reltime(messages[0]["created_at"]) if messages else "—"),
    ]
    max_n = max((t["count"] for t in top), default=0) or 1
    for t in top:
        t["bar_pct"] = int(100 * t["count"] / max_n)
    return _render(
        "channels.html",
        title="Channels",
        parts=parts,
        active_part="channel",
        refresh=refresh,
        request_path=request_path,
        cards=cards,
        top=top,
        messages=messages,
        top_per_page=top_per_page,
        per_page=per_page,
        top_pagination=_pagination(
            top_page,
            top_per_page,
            stats["channels"],
            lambda p: _channels_href(
                top_page=p, top_per_page=top_per_page, page=page, per_page=per_page
            ),
        ),
        top_pagesize=_pagesize_options(
            top_per_page,
            lambda p: _channels_href(top_per_page=p, page=page, per_page=per_page),
        ),
        msg_pagination=_pagination(
            page,
            per_page,
            stats["messages"],
            lambda p: _channels_href(
                top_page=top_page, top_per_page=top_per_page, page=p, per_page=per_page
            ),
        ),
        msg_pagesize=_pagesize_options(
            per_page,
            lambda p: _channels_href(top_page=top_page, top_per_page=top_per_page, per_page=p),
        ),
    )


# -- audit page --------------------------------------------------------------------------------


AUDIT_DEFAULT_PER_PAGE = 25
AUDIT_DEFAULT_SORT = "created_at"
AUDIT_DEFAULT_DIR = "desc"
# label, sort key, default direction when clicked from an unsorted state
_AUDIT_COLUMNS = [
    ("ID", "id", "desc"),
    ("When", "created_at", "desc"),
    ("Action", "action", "asc"),
    ("Subject", "subject", "asc"),
    ("Actor", "actor", "asc"),
    ("Correlation", "correlation_id", "asc"),
]


def _audit_href(
    filters: dict[str, str],
    *,
    sort: str = AUDIT_DEFAULT_SORT,
    dir: str = AUDIT_DEFAULT_DIR,
    page: int = 1,
    per_page: int = AUDIT_DEFAULT_PER_PAGE,
) -> str:
    """Build an ``/audit`` URL carrying only the params that differ from their defaults, so a
    plain unsorted/unpaginated view still round-trips to the same short ``/audit?...`` links the
    existing filter-by-field links have always produced."""
    params = {k: v for k, v in filters.items() if v}
    if sort != AUDIT_DEFAULT_SORT:
        params["sort"] = sort
    if dir != AUDIT_DEFAULT_DIR:
        params["dir"] = dir
    if page > 1:
        params["page"] = str(page)
    if per_page != AUDIT_DEFAULT_PER_PAGE:
        params["per_page"] = str(per_page)
    return f"/audit?{urlencode(params, quote_via=quote)}" if params else "/audit"


_ENV.globals["audit_href"] = _markup_fn(_audit_href)


def _ref_display(type_: str | None, id_: str | None, label: str | None) -> str | None:
    """How a subject/actor reference is shown: its human label if it has one, else ``Type:id``,
    else just the bare ``Type``. ``None`` (no type at all) renders as an em dash."""
    if not type_:
        return None
    if label:
        return label
    return f"{type_}:{id_}" if id_ else type_


def _ref_filter(type_: str, id_: str | None) -> str:
    """The ``Type:id`` (or bare ``Type``) value that filters the list to this reference."""
    return f"{type_}:{id_}" if id_ else type_


def _sort_columns(
    filters: dict[str, str], sort: str, dir_: str, per_page: int
) -> list[dict[str, Any]]:
    """One entry per column header: its toggled sort URL, whether it is the active sort, and the
    arrow (``↑``/``↓``) to show when active. Clicking the active column flips its direction."""
    columns = []
    for label, key, default_dir in _AUDIT_COLUMNS:
        active = sort == key
        next_dir = ("asc" if dir_ == "desc" else "desc") if active else default_dir
        columns.append(
            {
                "label": label,
                "url": Markup(_audit_href(filters, sort=key, dir=next_dir, per_page=per_page)),
                "active": active,
                "arrow": ("↑" if dir_ == "asc" else "↓") if active else "",
            }
        )
    return columns


def audit_page(
    parts: list[str],
    stats: dict[str, Any],
    rows: list[dict[str, Any]],
    filters: dict[str, str],
    *,
    total: int = 0,
    page: int = 1,
    per_page: int = AUDIT_DEFAULT_PER_PAGE,
    sort: str = AUDIT_DEFAULT_SORT,
    dir: str = AUDIT_DEFAULT_DIR,
    refresh: int = 10,
    request_path: str = "/audit",
) -> str:
    cards = [
        _card("events", _num(stats["events"])),
        _card("actions", _num(stats["actions"])),
        _card("last event", _reltime(stats["last_event_at"])),
    ]
    for r in rows:
        r["subject_display"] = _ref_display(r["subject_type"], r["subject_id"], r["subject_label"])
        r["subject_filter"] = _ref_filter(r["subject_type"], r["subject_id"])
        r["actor_display"] = _ref_display(r["actor_type"], r["actor_id"], r["actor_label"])
        r["actor_filter"] = _ref_filter(r["actor_type"], r["actor_id"])
    return _render(
        "audit.html",
        title="Audit",
        parts=parts,
        active_part="audit",
        refresh=refresh,
        request_path=request_path,
        cards=cards,
        rows=rows,
        filters=filters,
        sort=sort,
        dir=dir,
        per_page=per_page,
        columns=_sort_columns(filters, sort, dir, per_page),
        pagination=_pagination(
            page,
            per_page,
            total,
            lambda n: _audit_href(filters, sort=sort, dir=dir, page=n, per_page=per_page),
        ),
        pagesize=_pagesize_options(
            per_page, lambda n: _audit_href(filters, sort=sort, dir=dir, per_page=n)
        ),
    )


def audit_detail_page(parts: list[str], event: dict[str, Any]) -> str:
    subject = _ref_display(event["subject_type"], event["subject_id"], event["subject_label"])
    actor = _ref_display(event["actor_type"], event["actor_id"], event["actor_label"])
    cells = [
        ("action", _mono(event["action"])),
        ("subject", subject if subject else _dash()),
        ("actor", actor if actor else _dash()),
        ("correlation id", event["correlation_id"] if event["correlation_id"] else _dash()),
        ("recorded", _when(event["created_at"])),
    ]
    return _render(
        "audit_detail.html",
        title=f"Event #{event['id']}",
        parts=parts,
        active_part="audit",
        event=event,
        cells=cells,
    )


# -- misc --------------------------------------------------------------------------------------


def empty_page(parts: list[str]) -> str:
    return _render("empty.html", title="firm", parts=parts, active_part="")


def not_found(parts: list[str]) -> str:
    return _render("not_found.html", title="Not found", parts=parts, active_part="")


def auth_page(title: str, message: str) -> str:
    # No parts/tabs: this renders before authentication, so it must not reveal the configured parts.
    return _render("auth.html", title=title, parts=[], active_part="", message=message)


def error_page(parts: list[str], message: str) -> str:
    return _render("error.html", title="Error", parts=parts, active_part="", message=message)
