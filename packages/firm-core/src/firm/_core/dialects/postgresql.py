"""PostgreSQL dialect — plain transaction + ``FOR UPDATE SKIP LOCKED``."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Any

from sqlalchemy import Engine, Insert, Select, Table
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from ..database import transaction
from .base import Dialect


class PostgresDialect(Dialect):
    name = "postgresql"

    def begin_claim_tx(self, engine: Engine) -> AbstractContextManager[Connection]:
        return transaction(engine)

    def with_skip_locked(self, stmt: Select) -> Select:
        return stmt.with_for_update(skip_locked=True)

    def with_row_lock(self, stmt: Select) -> Select:
        return stmt.with_for_update()

    def upsert(
        self,
        table: Table,
        values: Mapping[str, Any],
        *,
        index_elements: Sequence[str],
        update_columns: Sequence[str],
    ) -> Insert:
        stmt = pg_insert(table).values(**values)
        return stmt.on_conflict_do_update(
            index_elements=list(index_elements),
            set_={c: stmt.excluded[c] for c in update_columns},
        )

    def insert_ignore(
        self, table: Table, values: Mapping[str, Any], *, index_elements: Sequence[str]
    ) -> Insert:
        # RETURNING the PK is how callers can tell insert from ignored conflict: psycopg
        # reports rowcount -1 for compiled single-row INSERTs, so inserted_count() counts the
        # returned rows instead (one on insert, none on conflict).
        return (
            pg_insert(table)
            .values(**values)
            .on_conflict_do_nothing(index_elements=list(index_elements))
            .returning(*table.primary_key.columns)
        )
