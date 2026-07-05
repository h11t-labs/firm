"""Read-only queries for the cache part."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection

from firm.cache import schema
from firm.cache.estimate import entry_count, estimate_size

_entries = schema.entries


def _decode(raw: Any) -> str:
    try:
        return bytes(raw).decode("utf-8")
    except (UnicodeDecodeError, TypeError):
        return repr(bytes(raw))


def cache_stats(conn: Connection) -> dict[str, int]:
    return {"entries": entry_count(conn), "estimated_size": estimate_size(conn)}


def cache_recent(conn: Connection, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    rows = conn.execute(
        select(_entries.c.id, _entries.c.key, _entries.c.byte_size, _entries.c.created_at)
        .order_by(_entries.c.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return [
        {"id": r.id, "key": _decode(r.key), "byte_size": r.byte_size, "created_at": r.created_at}
        for r in rows
    ]
