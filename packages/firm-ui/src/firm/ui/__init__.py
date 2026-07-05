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
"""

from __future__ import annotations

__version__ = "0.1.0"

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
    "Deny",
    "ProxyHeaderAuth",
    "__version__",
    "build_dashboard",
    "create_server",
    "hash_password",
    "load_authenticator",
    "serve",
    "verify_password",
]
