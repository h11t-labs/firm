"""Mount the dashboard in a FastAPI (or plain Starlette) application (``firm-ui[fastapi]``).

    app.include_router(router(dash, host_auth=True), prefix="/firm",
                       dependencies=[Depends(require_admin)])

See :mod:`firm.ui.contrib`; a runnable app is in ``examples/mounted_dashboard_fastapi.py``.
"""

from __future__ import annotations

import anyio.from_thread
from fastapi import APIRouter, Request, Response
from starlette.concurrency import run_in_threadpool

from ..app import Headers, UIRequest
from ..auth import Authenticator
from ..context import Dashboard
from . import build_app, mount_prefix


def router(
    dashboard: Dashboard,
    *,
    authenticator: Authenticator | None = None,
    host_auth: bool = False,
    channel_trim_retention: float | None = None,
    static_url: str | None = None,
) -> APIRouter:
    """The whole dashboard, under whatever ``prefix`` you include it at. Its work is synchronous
    database I/O, so each request runs in the threadpool, never on the event loop. ``dashboard``
    owns database engines: build it once at startup, close it on shutdown."""
    app = build_app(
        dashboard,
        authenticator=authenticator,
        host_auth=host_auth,
        channel_trim_retention=channel_trim_retention,
        static_url=static_url,
    )
    api = APIRouter()

    async def dispatch(request: Request, subpath: str) -> Response:
        scope = request.scope
        # Starlette versions disagree on whether root_path is still part of scope["path"], so add
        # it only when absent — otherwise a nested mount doubles its own prefix.
        root, path = scope.get("root_path", ""), scope["path"]
        inside_root = root and (path == root or path.startswith(f"{root}/"))
        full_path = path if inside_root else f"{root}{path}"
        ui_request = UIRequest(
            method=request.method,
            path=f"/{subpath}",
            query=scope["query_string"].decode("latin-1"),
            headers=Headers(request.headers.items()),
            peer=request.client.host if request.client else "",
            prefix=mount_prefix(full_path, subpath),
            host=request.headers.get("host", ""),
            scheme=request.url.scheme,
        )
        # The app asks for the body only after auth and its size check, so it stays in the receive
        # channel until then; reading it from the worker thread is what anyio's portal is for.
        result = await run_in_threadpool(
            app.handle, ui_request, read_body=lambda: anyio.from_thread.run(request.body)
        )
        response = Response(content=result.body, status_code=result.status)
        for name, value in result.headers:
            if name.lower() == "set-cookie":  # repeatable: never collapse it into headers[]
                response.raw_headers.append((b"set-cookie", value.encode("latin-1")))
            else:
                response.headers[name] = value
        return response

    # Two routes, two signatures: a `subpath` outside the path pattern would be read from the
    # query string instead, and `?subpath=` picking the page is a way around the routes.
    async def root_route(request: Request) -> Response:
        return await dispatch(request, "")

    async def sub_route(request: Request, subpath: str) -> Response:
        return await dispatch(request, subpath)

    api.add_api_route(
        "/", root_route, methods=["GET", "POST"], include_in_schema=False, name="firm-ui-root"
    )
    api.add_api_route(
        "/{subpath:path}",
        sub_route,
        methods=["GET", "POST"],
        include_in_schema=False,
        name="firm-ui",
    )
    return api
