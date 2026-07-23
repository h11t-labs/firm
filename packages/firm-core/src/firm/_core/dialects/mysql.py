"""MySQL/MariaDB dialect — plain transaction + ``FOR UPDATE SKIP LOCKED``."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Any

from sqlalchemy import Engine, Insert, Select, Table
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.engine import Connection

from ..database import transaction
from .base import Dialect


class MysqlDialect(Dialect):
    name = "mysql"

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
        # MySQL can't name the conflict index; ON DUPLICATE KEY UPDATE fires on whichever
        # unique key conflicts, so index_elements is informational here.
        stmt = mysql_insert(table).values(**values)
        return stmt.on_duplicate_key_update({c: stmt.inserted[c] for c in update_columns})

    def insert_ignore(
        self, table: Table, values: Mapping[str, Any], *, index_elements: Sequence[str]
    ) -> Insert:
        # NOT `INSERT IGNORE`: that downgrades *every* error (NOT NULL, FK, truncation) to a
        # warning, whereas PG/SQLite scope their DO NOTHING to the named unique index. Match
        # that by only swallowing the duplicate-key conflict via a no-op upsert. Setting a key
        # column to its own existing value (`col = col`) changes nothing, so MySQL's
        # affected-rows stays 1 on insert and 0 on conflict — exactly the rowcount contract
        # inserted_count() reads (no RETURNING needed here, unlike Postgres).
        index_col = index_elements[0]
        stmt = mysql_insert(table).values(**values)
        return stmt.on_duplicate_key_update({index_col: table.c[index_col]})
