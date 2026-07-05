"""Alembic environment for firm-channel (shared runner: firm._core.alembic_env)."""

from __future__ import annotations

from firm._core.alembic_env import run_migrations
from firm.channel import schema

run_migrations(
    metadata=schema.metadata,
    env_var="FIRM_CHANNEL_DATABASE_URL",
    version_table=schema.VERSION_TABLE,
)
