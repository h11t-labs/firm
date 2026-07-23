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
        return mysql_insert(table).values(**values).prefix_with("IGNORE")
