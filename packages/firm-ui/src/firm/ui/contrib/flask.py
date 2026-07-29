"""Mount the dashboard in a Flask application (``firm-ui[flask]``).

    app.register_blueprint(blueprint(dash, host_auth=True), url_prefix="/firm")

See :mod:`firm.ui.contrib`; a runnable app is in ``examples/mounted_dashboard_flask.py``.
"""

from __future__ import annotations

from flask import Blueprint, Response, request

from ..app import Headers, UIRequest
from ..auth import Authenticator
from ..context import Dashboard
from . import build_app, mount_prefix


def blueprint(
    dashboard: Dashboard,
    *,
    name: str = "firm_ui",
    authenticator: Authenticator | None = None,
    host_auth: bool = False,
    channel_trim_retention: float | None = None,
    static_url: str | None = None,
) -> Blueprint:
    """The whole dashboard, under whatever ``url_prefix`` you register it at. ``dashboard`` owns
    database engines: build it once at startup, close it on shutdown."""
    app = build_app(
        dashboard,
        authenticator=authenticator,
        host_auth=host_auth,
        channel_trim_retention=channel_trim_retention,
        static_url=static_url,
    )
    bp = Blueprint(name, __name__)

    @bp.route("/", defaults={"subpath": ""}, methods=["GET", "POST"])
    @bp.route("/<path:subpath>", methods=["GET", "POST"])
    def dashboard_route(subpath: str) -> Response:
        # script_root covers an application itself mounted under a path (WSGI SCRIPT_NAME).
        full_path = request.script_root + request.path
        ui_request = UIRequest(
            method=request.method,
            path=f"/{subpath}",
            query=request.query_string.decode("latin-1"),
            headers=Headers(request.headers.items()),
            peer=request.remote_addr or "",
            prefix=mount_prefix(full_path, subpath),
            host=request.host,
            scheme=request.scheme,
        )
        # Passed as a reader, not a value: the app calls it only after auth and its size check.
        result = app.handle(ui_request, read_body=lambda: request.get_data())
        return Response(result.body, status=result.status, headers=result.headers)

    return bp
