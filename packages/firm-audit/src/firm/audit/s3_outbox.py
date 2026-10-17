"""The transactional outbox behind ``S3AuditLog.record(conn=...)``.

S3 cannot take part in a SQL transaction, so an event recorded with ``conn`` is staged as a row
in ``firm_audit_s3_outbox`` inside the caller's transaction: it commits or rolls back with the
caller's work. A relay (:func:`flush`, or the log's ``outbox_engine`` background loop) then writes
each staged event to S3 and deletes its row. The S3 write is idempotent per prepared event, so a
relay that dies after writing but before deleting simply writes it again (at least once, never
twice): the guarantee is "committed ⇒ it will appear", not "committed ⇒ it is there".
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from sqlalchemy import Connection, Engine, delete, select

from .._core.clock import now_utc
from .._core.poller import InterruptiblePoller
from . import schema

if TYPE_CHECKING:
    from .s3 import S3AuditLog

_outbox = schema.s3_outbox


def create_outbox(bind: Engine | Connection) -> None:
    """Create only the outbox table (the caller's database needs no other audit table)."""
    _outbox.create(bind, checkfirst=True)


def stage(conn: Connection, stream: str, body: str) -> None:
    """Insert one prepared event into the outbox on the caller's connection and transaction."""
    import json

    entry_id = json.loads(body)["entry_id"]
    conn.execute(
        _outbox.insert().values(stream=stream, entry_id=entry_id, body=body, created_at=now_utc())
    )


def pending(engine: Engine, stream: str) -> int:
    """How many staged events of ``stream`` still wait for publication."""
    from sqlalchemy import func

    with engine.connect() as conn:
        return conn.execute(
            select(func.count()).select_from(_outbox).where(_outbox.c.stream == stream)
        ).scalar_one()


def flush(engine: Engine, log: S3AuditLog, *, limit: int = 1000) -> int:
    """Publish up to ``limit`` staged events of ``log``'s stream, oldest first.

    Each event is written to S3 and only then removed from the outbox, one short transaction per
    batch read and per delete; concurrent relays are safe because the write is idempotent.
    """
    from .s3 import PreparedEvent

    with engine.connect() as conn:
        rows = conn.execute(
            select(_outbox.c.id, _outbox.c.body)
            .where(_outbox.c.stream == log.stream)
            .order_by(_outbox.c.id)
            .limit(limit)
        ).all()
    published = 0
    for row in rows:
        log.write(PreparedEvent(row.body))
        with engine.begin() as conn:
            conn.execute(delete(_outbox).where(_outbox.c.id == row.id))
        published += 1
    return published


class OutboxLoop(InterruptiblePoller):
    """Optional background relay: publish staged events on a timer."""

    def __init__(
        self,
        log: S3AuditLog,
        engine: Engine,
        interval: float = 5.0,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        super().__init__(interval, name="audit-s3-outbox", on_error=on_error)
        self.log = log
        self.engine = engine

    def poll(self) -> Any:
        return flush(self.engine, self.log)
