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
        # `INSERT ... IGNORE` is the only MySQL form that satisfies inserted_count()'s rowcount
        # contract (1 on insert, 0 on conflict). The obvious alternative — a no-op
        # `ON DUPLICATE KEY UPDATE col = col` scoped to the key — does NOT: SQLAlchemy's MySQL
        # dialects always set CLIENT.FOUND_ROWS (see sqlalchemy/dialects/mysql/base.py), and
        # under that flag MySQL reports affected-rows *1* for a row "set to its current values"
        # (per the manual), making a conflict indistinguishable from an insert. There is no
        # IODKU variant that yields 0-on-conflict under FOUND_ROWS.
        #
        # Known downside / divergence: unlike PG/SQLite's index-scoped DO NOTHING, IGNORE also
        # downgrades unrelated errors (NOT NULL, FK, truncation) to warnings. Two callers use
        # this: schema_setup's schema auto-create race (fully-formed row, so the only possible
        # failure is the duplicate-key conflict), and firm-cache's entry first-write
        # (entries.py `ensure_entry` / `set(unless_exist=True)`). The cache path carries a real
        # residual risk on MySQL: a row violating an unrelated constraint (e.g. an oversized
        # value) is silently skipped, and inserted_count() reads 0 — indistinguishable from a
        # benign key conflict. This is a documented divergence from PG/SQLite, where only the
        # key conflict is swallowed and the constraint violation still raises.
        return mysql_insert(table).values(**values).prefix_with("IGNORE")
