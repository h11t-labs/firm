"""Database-dialect seam.

All database-specific SQL the firm packages need lives behind :class:`~.base.Dialect`:

* row locking for claim/trim paths — a plain transaction + ``SELECT ... FOR UPDATE SKIP
  LOCKED`` on PostgreSQL/MySQL; ``BEGIN IMMEDIATE`` (which serializes writers) on SQLite;
* conflict-handling inserts — native upsert and insert-if-absent statements.

Feature code is written once against this seam, so adding a database later means
implementing one small :class:`~.base.Dialect` subclass.
"""

from __future__ import annotations

from sqlalchemy import Engine

from .base import Dialect
from .mysql import MysqlDialect
from .postgresql import PostgresDialect
from .sqlite import SqliteDialect

__all__ = ["Dialect", "MysqlDialect", "PostgresDialect", "SqliteDialect", "get_dialect"]

# Dialects are stateless; hand out one shared instance per backend.
_DIALECTS: dict[str, Dialect] = {
    "sqlite": SqliteDialect(),
    "postgresql": PostgresDialect(),
    "mysql": MysqlDialect(),
    "mariadb": MysqlDialect(),
}


def get_dialect(engine: Engine) -> Dialect:
    try:
        return _DIALECTS[engine.dialect.name]
    except KeyError:
        raise ValueError(f"Unsupported database dialect: {engine.dialect.name!r}") from None
