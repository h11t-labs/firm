"""firm-ui — a small, optional web dashboard for all four firm modules.

Run it with the ``firm-ui`` command (or ``python -m firm.ui``). It's a stdlib HTTP server
(Jinja2 for templates) and nothing else in firm imports it — a pure, optional add-on with a
tab per part found in the database(s): queue (with pause/resume/retry/discard actions), cache,
channel, and audit.

The public API here is for running the dashboard from your own process — typically to put it
behind your own authentication::

    from firm.ui import BasicAuth, build_dashboard, serve

    dashboard = build_dashboard(database_url="sqlite:///app.db")
    serve(dashboard, authenticator=BasicAuth("admin", password="secret"))

The dashboard itself is transport-free (:class:`DashboardApp`, request in / response out), so it
can also be mounted inside an application that already serves HTTP — see
:mod:`firm.ui.contrib` for the Django, Flask, and FastAPI adapters.
"""

from __future__ import annotations

__version__ = "1.0.0"

from .app import DashboardApp, Headers, UIRequest, UIResponse, static_dir
from .auth import (
    Allow,
    Authenticator,
    AuthRequest,
    BasicAuth,
    Deny,
    ProxyHeaderAuth,
    hash_password,
    load_authenticator,
    verify_password,
)
from .context import build_dashboard
from .server import create_server, serve

__all__ = [
    "Allow",
    "AuthRequest",
    "Authenticator",
    "BasicAuth",
    "DashboardApp",
    "Deny",
    "Headers",
    "ProxyHeaderAuth",
    "UIRequest",
    "UIResponse",
    "__version__",
    "build_dashboard",
    "create_server",
    "hash_password",
    "load_authenticator",
    "serve",
    "static_dir",
    "verify_password",
]
