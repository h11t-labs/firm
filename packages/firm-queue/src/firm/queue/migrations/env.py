"""Alembic environment for firm-queue (shared runner: firm._core.alembic_env)."""

from __future__ import annotations

from firm._core.alembic_env import run_migrations
from firm.queue import schema

run_migrations(
    metadata=schema.metadata,
    env_var="FIRM_QUEUE_DATABASE_URL",
    version_table=schema.VERSION_TABLE,
)
