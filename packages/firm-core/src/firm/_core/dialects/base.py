"""The dialect interface: row locking for claims, and conflict-handling inserts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Any

from sqlalchemy import Engine, Insert, Select, Table
from sqlalchemy.engine import Connection, CursorResult


def inserted_count(result: CursorResult) -> int:
    """How many rows an executed :meth:`Dialect.insert_ignore` statement actually inserted.

    Postgres statements carry ``RETURNING`` (psycopg reports ``rowcount`` -1 for compiled
    single-row INSERTs), so there we count the returned rows; MySQL/SQLite report a sane
    ``rowcount`` directly.
    """
    if result.returns_rows:
        return len(result.fetchall())
    return result.rowcount


class Dialect(ABC):
    """Per-database SQL strategy: everything dialect-specific the firm packages need lives
    behind this seam (locking for the claim/dispatch paths, native upserts), so feature
    packages never branch on the engine's dialect name themselves."""

    name: str

    @abstractmethod
    def begin_claim_tx(self, engine: Engine) -> AbstractContextManager[Connection]:
        """Open the transaction a claim/mutate sequence runs inside (``BEGIN IMMEDIATE`` on
        SQLite; an ordinary transaction on Postgres/MySQL)."""

    @abstractmethod
    def with_skip_locked(self, stmt: Select) -> Select:
        """Apply ``FOR UPDATE SKIP LOCKED`` to a ``SELECT`` that precedes a mutation, so two
        processes never pick the same row (Postgres/MySQL). A no-op on SQLite, where
        ``begin_claim_tx`` already holds the write lock."""

    @abstractmethod
    def with_row_lock(self, stmt: Select) -> Select:
        """Lock selected coordination rows until transaction end.

        PostgreSQL/MySQL use ``FOR UPDATE``; SQLite is a no-op because callers pair it with
        ``BEGIN IMMEDIATE``.
        """

    @abstractmethod
    def upsert(
        self,
        table: Table,
        values: Mapping[str, Any],
        *,
        index_elements: Sequence[str],
        update_columns: Sequence[str],
    ) -> Insert:
        """Build a native last-write-wins upsert: insert ``values``; on a conflict against the
        unique index over ``index_elements``, overwrite ``update_columns`` from the incoming
        row (``ON CONFLICT DO UPDATE`` / ``ON DUPLICATE KEY UPDATE``). MySQL has no way to
        name the index, so there ``index_elements`` is informational only."""

    @abstractmethod
    def insert_ignore(
        self, table: Table, values: Mapping[str, Any], *, index_elements: Sequence[str]
    ) -> Insert:
        """Build an insert-if-absent: on a conflict against the unique index over
        ``index_elements``, do nothing. Pass the executed result to :func:`inserted_count` to
        learn whether the row was inserted (1) or the conflict ignored (0) on every backend —
        the raw ``rowcount`` is *not* reliable for these statements on Postgres.

        Backend divergence: PostgreSQL/SQLite scope the no-op to the named unique index
        (``ON CONFLICT ... DO NOTHING``), so only a duplicate-key conflict is swallowed. MySQL
        uses ``INSERT ... IGNORE`` — the only form that meets the 1/0 rowcount contract there
        (a no-op ``ON DUPLICATE KEY UPDATE`` reports 1 on conflict under the ``CLIENT.FOUND_ROWS``
        flag SQLAlchemy always sets) — which also downgrades unrelated errors (NOT NULL, FK,
        truncation) to warnings. Callers must therefore pass rows that can only fail on the
        conflict."""
