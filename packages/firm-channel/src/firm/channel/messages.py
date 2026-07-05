"""Row-level message I/O: insert a broadcast, read new messages, trim old ones."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Connection, Engine, Row, delete, func, insert, select

from .._core.clock import now_utc
from .._core.dialects import Dialect
from . import schema
from .keys import channel_hash

_messages = schema.messages


def insert_message(conn: Connection, channel_bytes: bytes, payload_bytes: bytes) -> None:
    conn.execute(
        insert(_messages).values(
            channel=channel_bytes,
            payload=payload_bytes,
            channel_hash=channel_hash(channel_bytes),
            created_at=now_utc(),
        )
    )


def current_max_id(conn: Connection) -> int:
    """The highest message id, or 0 when the table is empty (the listener's starting point)."""
    return conn.execute(select(func.max(_messages.c.id))).scalar() or 0


def message_count(conn: Connection) -> int:
    return conn.execute(select(func.count()).select_from(_messages)).scalar() or 0


def fetch_since(conn: Connection, channel_hashes: Sequence[int], after_id: int) -> Sequence[Row]:
    """Messages on the given channels with ``id > after_id``, oldest first.

    ``created_at`` is included so the listener can advance its scan floor only past rows
    older than the commit-grace window (see ``Channel._dispatch_new``).
    """
    if not channel_hashes:
        return []
    stmt = (
        select(_messages.c.id, _messages.c.channel, _messages.c.payload, _messages.c.created_at)
        .where(_messages.c.channel_hash.in_(channel_hashes))
        .where(_messages.c.id > after_id)
        .order_by(_messages.c.id)
    )
    return conn.execute(stmt).all()


def trim_old(engine: Engine, dialect: Dialect, cutoff: datetime, batch_size: int) -> int:
    """Delete up to ``batch_size`` messages with ``created_at < cutoff``, skipping rows another
    trimmer holds (Postgres/MySQL ``FOR UPDATE SKIP LOCKED``) so concurrent trims never fight."""
    with dialect.begin_claim_tx(engine) as conn:
        stmt = dialect.with_skip_locked(
            select(_messages.c.id).where(_messages.c.created_at < cutoff).limit(batch_size)
        )
        ids = [row.id for row in conn.execute(stmt)]
        if ids:
            conn.execute(delete(_messages).where(_messages.c.id.in_(ids)))
    return len(ids)
