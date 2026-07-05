"""Read-only queries for the channel (pub/sub) part."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from firm.channel import schema

_messages = schema.messages


def _decode(raw: Any) -> str:
    try:
        return bytes(raw).decode("utf-8")
    except (UnicodeDecodeError, TypeError):
        return repr(bytes(raw))


def channel_stats(conn: Connection) -> dict[str, int]:
    total = conn.execute(select(func.count()).select_from(_messages)).scalar() or 0
    distinct = (
        conn.execute(select(func.count(func.distinct(_messages.c.channel_hash)))).scalar() or 0
    )
    return {"messages": total, "channels": distinct}


def channel_top(conn: Connection, limit: int = 25, offset: int = 0) -> list[dict[str, Any]]:
    rows = conn.execute(
        select(
            _messages.c.channel,
            func.count().label("n"),
            func.max(_messages.c.created_at).label("last"),
        )
        .group_by(_messages.c.channel)
        .order_by(func.count().desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return [{"channel": _decode(r.channel), "count": r.n, "last": r.last} for r in rows]


def channel_recent(conn: Connection, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    rows = conn.execute(
        select(_messages.c.id, _messages.c.channel, _messages.c.payload, _messages.c.created_at)
        .order_by(_messages.c.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return [
        {
            "id": r.id,
            "channel": _decode(r.channel),
            "payload": _decode(r.payload),
            "created_at": r.created_at,
        }
        for r in rows
    ]
