"""The dialect interface: row locking for claims, and conflict-handling inserts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Any

from sqlalchemy import Engine, Insert, Select, Table
from sqlalchemy.engine import Connection


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
        ``index_elements``, do nothing. Executing it yields ``rowcount`` 1 on insert and 0 on
        an ignored conflict, on every backend."""
