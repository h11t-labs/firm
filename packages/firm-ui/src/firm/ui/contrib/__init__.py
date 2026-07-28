"""Mount the dashboard inside a host application, on its domain and behind its own auth.

One adapter per framework, each behind its own extra — ``firm-ui[django]``, ``firm-ui[flask]``,
``firm-ui[fastapi]`` — and each a thin translation between that framework's request/response
objects and the pair in :mod:`firm.ui.app`. Import the one you need; nothing here is imported by
``firm.ui`` itself, so the framework dependency stays optional::

    from firm.ui.contrib.flask import blueprint
    app.register_blueprint(blueprint(dash, host_auth=True), url_prefix="/firm")

``firm-ui serve`` is unaffected — it is the same :class:`firm.ui.app.DashboardApp` behind the
stdlib transport.

Two things every mount has to settle, and both are explicit here rather than defaulted:

* **Who authenticates.** Pass an ``authenticator`` (firm-ui checks it itself, exactly as the
  standalone server does), or ``host_auth=True`` to say the host application already guards this
  route — with its own decorator, dependency, or middleware. Neither one set is an error, so a
  mount can never silently mean "no auth at all".
* **CSRF.** The dashboard's destructive actions are plain POST forms guarded by a same-origin
  ``Origin``/``Referer`` check, not by the host framework's CSRF token (the forms are rendered by
  firm-ui and carry no token). The adapters therefore exempt these routes from the host's token
  check where the host applies one by default; the same-origin guard runs on every POST.
"""

from __future__ import annotations

from ..app import DashboardApp
from ..auth import Authenticator
from ..context import Dashboard

__all__ = ["build_app", "mount_prefix"]


def build_app(
    dashboard: Dashboard,
    *,
    authenticator: Authenticator | None = None,
    host_auth: bool = False,
    channel_trim_retention: float | None = None,
    static_url: str | None = None,
) -> DashboardApp:
    """Shared construction for the framework adapters — including the guard that a mount states
    who authenticates it. This mirrors the standalone server's refusal to bind a non-loopback
    address without authentication: "nothing configured" must never quietly mean "open"."""
    if authenticator is None and not host_auth:
        raise ValueError(
            "Mounting the dashboard needs either authenticator=<Authenticator> (firm-ui checks it) "
            "or host_auth=True (the host application already guards this route). The dashboard "
            "exposes tracebacks and destructive actions, so it will not mount unguarded."
        )
    return DashboardApp(dashboard, authenticator, channel_trim_retention, static_url)


def mount_prefix(full_path: str, subpath: str) -> str:
    """Where the dashboard is mounted, derived from the path the client asked for and the part of
    it the mount's catch-all route matched. Frameworks report the mount point in their own ways
    (and not at all through ``include_router``), so taking the difference is the one method that
    holds for all of them."""
    relative = "/" + subpath.lstrip("/")
    if relative == "/":
        return full_path.rstrip("/")
    if full_path.endswith(relative):
        return full_path[: -len(relative)]
    return ""  # unreachable through the adapters' own routes; unprefixed links beat wrong ones
