"""Run the firm-ui dashboard behind authentication.

    FIRM_DATABASE_URL=sqlite:///firm-quickstart.db uv run python examples/secured_dashboard.py

Then open http://127.0.0.1:8787 and sign in as  admin / s3cret  (set DASHBOARD_PASSWORD to change
it). Run examples/quickstart.py first if the database has no firm tables yet.

This script shows the two code-driven paths — built-in Basic auth and a custom Authenticator. The
no-code paths need no script at all:

    # HTTP Basic auth, secret from the environment:
    FIRM_UI_PASSWORD=s3cret firm-ui --database-url sqlite:///app.db --basic-auth-user admin

    # Trust an upstream auth proxy (oauth2-proxy / Cloudflare Access / nginx auth_request):
    firm-ui --database-url sqlite:///app.db --trust-auth-header X-Forwarded-User

    # Load the custom Authenticator below by import path:
    DASHBOARD_TOKEN=secret \\
      firm-ui --database-url sqlite:///app.db \\
               --authenticator examples.secured_dashboard:shared_token_auth
"""

from __future__ import annotations

import hmac
import os

from firm.ui import Allow, BasicAuth, Deny, build_dashboard, serve

DB = os.environ.get("FIRM_DATABASE_URL", "sqlite:///firm-quickstart.db")


class SharedTokenAuth:
    """A minimal custom Authenticator: allow requests carrying a shared secret header.

    Any object with ``authenticate(req) -> Allow | Deny`` is an Authenticator. A real deployment
    would validate a signed session cookie or a JWT here instead — the shape is identical. ``req``
    exposes ``.method``, ``.path``, ``.header(name, default="")`` and ``.client_addr``.
    """

    def __init__(self, token: str) -> None:
        self._token = token

    def authenticate(self, req):
        sent = req.header("X-Dashboard-Token")
        if sent and hmac.compare_digest(sent, self._token):
            return Allow(user="token")
        return Deny(status=401, message="Send a valid X-Dashboard-Token header.")


# A module-level instance so `--authenticator examples.secured_dashboard:shared_token_auth` works.
shared_token_auth = SharedTokenAuth(os.environ.get("DASHBOARD_TOKEN", "change-me"))


def main() -> None:
    dashboard = build_dashboard(database_url=DB)
    if not dashboard.parts:
        raise SystemExit(f"No firm tables at {DB}; run examples/quickstart.py first.")

    # Built-in HTTP Basic auth — the browser shows a sign-in dialog. Swap in shared_token_auth (or
    # your own Authenticator) to integrate a session / SSO instead.
    password = os.environ.get("DASHBOARD_PASSWORD", "s3cret")
    authenticator = BasicAuth("admin", password=password)

    print(f"firm-ui (secured) → http://127.0.0.1:8787   sign in as  admin / {password}")
    try:
        serve(dashboard, host="127.0.0.1", port=8787, authenticator=authenticator)
    finally:
        dashboard.close()


if __name__ == "__main__":
    main()
