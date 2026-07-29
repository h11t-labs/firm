"""Mount the firm-ui dashboard inside a FastAPI application.

    FIRM_DATABASE_URL=sqlite:///firm-quickstart.db \
      uv run uvicorn examples.mounted_dashboard_fastapi:app

Then open http://127.0.0.1:8000/firm/ — it answers 403 until you send the demo credential
(``curl -H 'X-Demo-Admin: yes' http://127.0.0.1:8000/firm/``). Run examples/quickstart.py first if
the database has no firm tables yet. Needs ``firm-ui[fastapi]``.

The dependency below is what guards the dashboard — which is what ``host_auth=True`` states. Swap
it for the one you already use on admin routes.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request

from firm.ui import build_dashboard
from firm.ui.contrib.fastapi import router

DB = os.environ.get("FIRM_DATABASE_URL", "sqlite:///firm-quickstart.db")

# It owns database engines: one per process, built at startup and closed on shutdown.
dashboard = build_dashboard(database_url=DB)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    dashboard.close()


def require_admin(request: Request) -> None:
    """Stand-in for your real dependency — an OAuth2 scope check, ``Depends(get_current_admin)``."""
    if request.headers.get("X-Demo-Admin") != "yes":
        raise HTTPException(status_code=403, detail="admins only")


app = FastAPI(lifespan=lifespan)
app.include_router(
    router(dashboard, host_auth=True),
    prefix="/firm",
    dependencies=[Depends(require_admin)],
)


@app.get("/")
def index() -> dict[str, str]:
    return {"dashboard": "/firm/ (send X-Demo-Admin: yes)"}
