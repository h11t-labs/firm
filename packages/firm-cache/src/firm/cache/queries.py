"""Read-only queries over the cache store (SQLAlchemy only — no heavy deps).

``cache_stats`` / ``cache_recent`` are a supported read surface — the dashboard (firm-ui) builds
on it, and so can your own dashboards, exporters, or health checks. Changing their signatures is a
breaking change.

Both take a live ``Connection`` and return plain dicts. Keys come back as the raw ``bytes`` they
were stored as (never a lossy decode): a cache key is arbitrary bytes, so rendering it as text is
the caller's decision.

Input contract: a negative ``limit``/``offset`` raises ``ValueError``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection

from . import schema
from .estimate import entry_count, estimate_size

_entries = schema.entries


def _check_window(limit: int, offset: int) -> None:
    """Reject a negative page window before it reaches SQL — the backends disagree about what a
    negative LIMIT/OFFSET means (SQLite reads a negative limit as "no limit"), so it is a caller
    bug worth naming rather than a silently different result set."""
    if limit < 0:
        raise ValueError(f"limit must be >= 0, got {limit}")
    if offset < 0:
        raise ValueError(f"offset must be >= 0, got {offset}")


def cache_stats(conn: Connection) -> dict[str, int]:
    """Entry count plus the sampled total size (see :mod:`.estimate`)."""
    return {"entries": entry_count(conn), "estimated_size": estimate_size(conn)}


def cache_recent(conn: Connection, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    """A page of the most recently written entries, newest first. ``key`` is ``bytes``; the
    value itself is never read (it may be large, encoded, or encrypted)."""
    _check_window(limit, offset)
    rows = conn.execute(
        select(_entries.c.id, _entries.c.key, _entries.c.byte_size, _entries.c.created_at)
        .order_by(_entries.c.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    # bytes(...) normalizes the driver's binary type (MySQL hands back a memoryview) so the
    # contract is the same on every backend.
    return [
        {"id": r.id, "key": bytes(r.key), "byte_size": r.byte_size, "created_at": r.created_at}
        for r in rows
    ]
