"""Mount the firm-ui dashboard inside a Flask application.

    FIRM_DATABASE_URL=sqlite:///firm-quickstart.db \
      uv run flask --app examples.mounted_dashboard_flask run

Then open http://127.0.0.1:5000/ — you land on a fake sign-in, and the dashboard lives at
http://127.0.0.1:5000/firm/ behind it. Run examples/quickstart.py first if the database has no
firm tables yet. Needs ``firm-ui[flask]``.

The ``before_request`` below is what guards the dashboard — which is what ``host_auth=True``
states. Swap it for Flask-Login or your own session check.
"""

from __future__ import annotations

import os

from flask import Flask, redirect, session, url_for

from firm.ui import build_dashboard
from firm.ui.contrib.flask import blueprint

DB = os.environ.get("FIRM_DATABASE_URL", "sqlite:///firm-quickstart.db")

app = Flask(__name__)
app.secret_key = "demo-only-not-a-real-secret"  # noqa: S105 - a demo session key

# It owns database engines: one per process, built at startup.
dashboard = build_dashboard(database_url=DB)

firm_ui = blueprint(dashboard, host_auth=True)


@firm_ui.before_request
def require_admin():
    """Stand-in for whatever you already use — Flask-Login, an SSO session, a permission table."""
    if not session.get("admin"):
        return redirect(url_for("sign_in"))
    return None


app.register_blueprint(firm_ui, url_prefix="/firm")


@app.get("/")
def sign_in() -> str:
    return (
        "<h1>Demo app</h1>"
        '<form method="post" action="/sign-in"><button>Sign in as admin</button></form>'
    )


@app.post("/sign-in")
def do_sign_in():
    session["admin"] = True  # a real app would check a password here
    return redirect("/firm/")
