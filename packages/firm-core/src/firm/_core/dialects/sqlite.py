"""SQLite dialect — serialize claimers via ``BEGIN IMMEDIATE`` (no row-level locking)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Any

from sqlalchemy import Engine, Insert, Select, Table
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection

from ..database import immediate_transaction
from .base import Dialect


class SqliteDialect(Dialect):
    name = "sqlite"

    def begin_claim_tx(self, engine: Engine) -> AbstractContextManager[Connection]:
        return immediate_transaction(engine)

    def with_skip_locked(self, stmt: Select) -> Select:
        # No row-level locking on SQLite; the IMMEDIATE write lock from begin_claim_tx is what
        # guarantees two claimers never select the same row.
        return stmt

    def with_row_lock(self, stmt: Select) -> Select:
        return stmt

    def upsert(
        self,
        table: Table,
        values: Mapping[str, Any],
        *,
        index_elements: Sequence[str],
        update_columns: Sequence[str],
    ) -> Insert:
        stmt = sqlite_insert(table).values(**values)
        return stmt.on_conflict_do_update(
            index_elements=list(index_elements),
            set_={c: stmt.excluded[c] for c in update_columns},
        )

    def insert_ignore(
        self, table: Table, values: Mapping[str, Any], *, index_elements: Sequence[str]
    ) -> Insert:
        return (
            sqlite_insert(table)
            .values(**values)
            .on_conflict_do_nothing(index_elements=list(index_elements))
        )
