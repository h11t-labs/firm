"""Shared Alembic environment runner used by every firm package's ``migrations/env.py``.

Each package calls :func:`run_migrations` with its own metadata, URL env var, and — crucially
— its own ``version_table`` (``firm_<module>_alembic_version``). Separate version tables are
what let the four packages migrate independently against one shared database; with Alembic's
default ``alembic_version`` table they would overwrite each other's revision stamps.

The URL is resolved (in order) from ``-x url=...``, the package's environment variable, or
``sqlalchemy.url`` in the ini. Online migrations reuse :func:`create_engine_for` so they get
the same SQLite PRAGMAs as the running system.
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import MetaData

from .database import create_engine_for


def run_migrations(*, metadata: MetaData, env_var: str, version_table: str) -> None:
    def _resolve_url() -> str:
        x_args = context.get_x_argument(as_dictionary=True)
        url = (
            x_args.get("url")
            or os.environ.get(env_var)
            or context.config.get_main_option("sqlalchemy.url")
        )
        if not url:
            raise RuntimeError(
                f"No database URL: set {env_var}, pass -x url=..., "
                "or set sqlalchemy.url in alembic.ini."
            )
        return url

    if context.is_offline_mode():
        context.configure(
            url=_resolve_url(),
            target_metadata=metadata,
            literal_binds=True,
            dialect_opts={"paramstyle": "named"},
            version_table=version_table,
        )
        with context.begin_transaction():
            context.run_migrations()
    else:
        engine = create_engine_for(_resolve_url())
        try:
            with engine.connect() as connection:
                context.configure(
                    connection=connection,
                    target_metadata=metadata,
                    render_as_batch=True,
                    version_table=version_table,
                )
                with context.begin_transaction():
                    context.run_migrations()
        finally:
            # Dispose even if run_migrations() raises, so a failed migration never leaks the pool.
            engine.dispose()
