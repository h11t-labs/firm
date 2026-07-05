"""Row-level cache I/O: atomic upsert on ``key_hash``, read, locked read, delete, byte sizing.

Writes are a single dialect-native upsert (``ON CONFLICT DO UPDATE`` / ``ON DUPLICATE KEY
UPDATE``) so two processes writing the same key never collide on the ``key_hash`` unique index —
the race the old check-then-insert had on Postgres/MySQL (SQLite hid it behind its write lock).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, delete, select

from .._core.clock import now_utc
from .._core.dialects import get_dialect
from .._core.dialects.base import inserted_count
from . import schema
from .keys import key_hash

_entries = schema.entries

# Fixed per-row overhead estimates (indexes, headers, free space).
ESTIMATED_ROW_OVERHEAD = 140
ESTIMATED_ENCRYPTION_OVERHEAD = 170

_CONFLICT_COLS = ("key", "value", "byte_size", "created_at")


def compute_byte_size(key_bytes: bytes, value_bytes: bytes, encrypted: bool) -> int:
    overhead = ESTIMATED_ENCRYPTION_OVERHEAD if encrypted else ESTIMATED_ROW_OVERHEAD
    return len(key_bytes) + len(value_bytes) + overhead


def _values(key_bytes: bytes, value_bytes: bytes, encrypted: bool) -> dict[str, Any]:
    return {
        "key": key_bytes,
        "value": value_bytes,
        "key_hash": key_hash(key_bytes),
        "byte_size": compute_byte_size(key_bytes, value_bytes, encrypted),
        "created_at": now_utc(),
    }


def _upsert(conn: Connection, values: dict[str, Any], *, overwrite: bool) -> int:
    """Insert ``values``; on a ``key_hash`` conflict either overwrite the row or do nothing."""
    dialect = get_dialect(conn.engine)
    if overwrite:
        stmt = dialect.upsert(
            _entries, values, index_elements=("key_hash",), update_columns=_CONFLICT_COLS
        )
        return conn.execute(stmt).rowcount
    stmt = dialect.insert_ignore(_entries, values, index_elements=("key_hash",))
    return inserted_count(conn.execute(stmt))


def write_entry(conn: Connection, key_bytes: bytes, value_bytes: bytes, encrypted: bool) -> None:
    """Upsert a cache entry (last write wins)."""
    _upsert(conn, _values(key_bytes, value_bytes, encrypted), overwrite=True)


def ensure_entry(conn: Connection, key_bytes: bytes, value_bytes: bytes, encrypted: bool) -> bool:
    """Insert the entry only if absent; return ``True`` iff this call inserted the row.

    Used by ``increment`` to materialize the row first, and by ``set(unless_exist=True)``.
    Whether the insert happened comes from ``inserted_count`` (row-returning on Postgres,
    ``rowcount`` elsewhere) — see ``Dialect.insert_ignore``.
    """
    return bool(_upsert(conn, _values(key_bytes, value_bytes, encrypted), overwrite=False))


def read_entry(conn: Connection, key_bytes: bytes) -> bytes | None:
    kh = key_hash(key_bytes)
    row = conn.execute(
        select(_entries.c.key, _entries.c.value).where(_entries.c.key_hash == kh)
    ).first()
    if row is None or bytes(row.key) != key_bytes:  # guard against hash collisions
        return None
    return bytes(row.value)


def read_entry_locked(conn: Connection, key_bytes: bytes) -> bytes | None:
    """Like :func:`read_entry` but ``SELECT ... FOR UPDATE`` (Postgres/MySQL) so a concurrent
    ``increment`` of the same key waits its turn. A no-op lock on SQLite (the immediate
    transaction already serializes writers)."""
    kh = key_hash(key_bytes)
    row = conn.execute(
        select(_entries.c.key, _entries.c.value).where(_entries.c.key_hash == kh).with_for_update()
    ).first()
    if row is None or bytes(row.key) != key_bytes:
        return None
    return bytes(row.value)


def delete_entry(conn: Connection, key_bytes: bytes) -> bool:
    kh = key_hash(key_bytes)
    result = conn.execute(delete(_entries).where(_entries.c.key_hash == kh))
    return bool(result.rowcount)
