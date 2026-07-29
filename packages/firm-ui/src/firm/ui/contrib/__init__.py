"""Mount the dashboard inside a host application — one adapter per framework, behind its own extra.

Each adapter translates between its framework's request/response objects and the pair in
:mod:`firm.ui.app`; nothing here is imported by ``firm.ui``, so the framework dependency stays
optional. See ``docs/ui.md`` for the per-framework snippets and ``examples/mounted_dashboard_*.py``
for runnable ones.

Two things hold for every mount and are not visible from any single adapter:

* A mount states who authenticates it — ``authenticator=`` or ``host_auth=True``, never neither
  (see :func:`build_app`). The adapters exempt their routes from the host's CSRF token check,
  since the dashboard renders its own tokenless forms; its same-origin guard runs on every POST.
* A body that announces no length (a chunked one) is buffered by the host's server before the
  dashboard's own 1 MiB limit can apply, so the host's request-size limit is the bound there
  (Django's ``DATA_UPLOAD_MAX_MEMORY_SIZE``, Flask's ``MAX_CONTENT_LENGTH``). The standalone
  server frames bodies by ``Content-Length`` alone and rejects the rest.
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
    """Shared construction for the adapters, refusing a mount that names no authentication —
    the standalone server's refusal to bind a public address unguarded, one layer up."""
    if authenticator is None and not host_auth:
        raise ValueError(
            "Mounting the dashboard needs either authenticator=<Authenticator> (firm-ui checks it) "
            "or host_auth=True (the host application already guards this route). The dashboard "
            "exposes tracebacks and destructive actions, so it will not mount unguarded."
        )
    return DashboardApp(dashboard, authenticator, channel_trim_retention, static_url)


def mount_prefix(full_path: str, subpath: str) -> str:
    """The mount point, as the difference between the path the client asked for and the part the
    catch-all route matched — frameworks report it in their own ways, and ``include_router`` not
    at all, so the difference is the one method that holds for all of them."""
    relative = "/" + subpath.lstrip("/")
    if relative == "/":
        return full_path.rstrip("/")
    if full_path.endswith(relative):
        return full_path[: -len(relative)]
    return ""  # unreachable through the adapters' own routes; unprefixed links beat wrong ones
