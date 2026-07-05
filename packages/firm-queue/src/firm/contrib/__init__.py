"""Optional, framework-specific glue for embedding firm-queue in a web app.

Nothing in core imports this package — it's opt-in. Each module pulls its framework lazily and
tells you which extra to install if it's missing:

    from firm.contrib.fastapi import lifespan          # needs: firm[fastapi]
    from firm.contrib.flask import Firm          # needs: firm[flask]
    from firm.contrib.sqlalchemy import enqueue_after_commit  # SQLAlchemy is already core

These adapters only wire up lifecycle + enqueuing; you still define jobs with the normal
``@bq.job`` decorator.
"""

from __future__ import annotations

__all__: list[str] = []
