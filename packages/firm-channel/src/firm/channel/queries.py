"""Read-only queries over the channel (pub/sub) buffer (SQLAlchemy only — no heavy deps).

``channel_stats`` / ``channel_top`` / ``channel_recent`` are a supported read surface — the
dashboard (firm-ui) builds on it, and so can your own dashboards, exporters, or health checks.
Changing their signatures is a breaking change.

All three take a live ``Connection`` and return plain dicts. Channel names and payloads come back
as the raw ``bytes`` they were stored as (never a lossy decode): a payload is arbitrary bytes, so
rendering it as text is the caller's decision.

Input contract: a negative ``limit``/``offset`` raises ``ValueError``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from . import schema

_messages = schema.messages


def _check_window(limit: int, offset: int) -> None:
    """Reject a negative page window before it reaches SQL — the backends disagree about what a
    negative LIMIT/OFFSET means (SQLite reads a negative limit as "no limit"), so it is a caller
    bug worth naming rather than a silently different result set."""
    if limit < 0:
        raise ValueError(f"limit must be >= 0, got {limit}")
    if offset < 0:
        raise ValueError(f"offset must be >= 0, got {offset}")


def channel_stats(conn: Connection) -> dict[str, int]:
    """Buffered message count and how many distinct channels they span."""
    total = conn.execute(select(func.count()).select_from(_messages)).scalar() or 0
    distinct = (
        conn.execute(select(func.count(func.distinct(_messages.c.channel_hash)))).scalar() or 0
    )
    return {"messages": total, "channels": distinct}


def channel_top(conn: Connection, limit: int = 25, offset: int = 0) -> list[dict[str, Any]]:
    """A page of the busiest channels, most messages first. ``channel`` is ``bytes``; ``count``
    is that channel's buffered messages and ``last`` its newest ``created_at``."""
    _check_window(limit, offset)
    rows = conn.execute(
        select(
            _messages.c.channel,
            func.count().label("n"),
            func.max(_messages.c.created_at).label("last"),
        )
        .group_by(_messages.c.channel)
        # channel as tiebreaker: count-only ordering lets tied rows repeat/vanish across
        # pages (audit_search uses an id tiebreaker for the same reason)
        .order_by(func.count().desc(), _messages.c.channel)
        .limit(limit)
        .offset(offset)
    ).all()
    # bytes(...) normalizes the driver's binary type (MySQL hands back a memoryview) so the
    # contract is the same on every backend.
    return [{"channel": bytes(r.channel), "count": r.n, "last": r.last} for r in rows]


def channel_recent(conn: Connection, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    """A page of the most recently buffered messages, newest first. ``channel`` and ``payload``
    are ``bytes``."""
    _check_window(limit, offset)
    rows = conn.execute(
        select(_messages.c.id, _messages.c.channel, _messages.c.payload, _messages.c.created_at)
        .order_by(_messages.c.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return [
        {
            "id": r.id,
            "channel": bytes(r.channel),
            "payload": bytes(r.payload),
            "created_at": r.created_at,
        }
        for r in rows
    ]
