"""firm-ui — a small, optional web dashboard for firm-queue.

Run it with the ``firm-ui`` command (or ``python -m firm.ui``). It's a stdlib HTTP server
(Jinja2 for templates) and nothing else in firm imports it — it's a pure, optional add-on that
reads the queue tables and reuses the queue's own pause/resume/retry helpers.

The public API here is for running the dashboard from your own process — typically to put it
behind your own authentication::

    from firm.ui import BasicAuth, build_dashboard, serve

    dashboard = build_dashboard(database_url="sqlite:///app.db")
    serve(dashboard, authenticator=BasicAuth("admin", password="secret"))
"""

from __future__ import annotations

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
    "build_dashboard",
    "create_server",
    "hash_password",
    "load_authenticator",
    "serve",
    "verify_password",
]
