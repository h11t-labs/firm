"""Retention — opt-in, age-based pruning.

Unlike :mod:`firm.cache`'s expiry, this is never triggered by writes — ``AuditLog.record`` never
calls into this module. Pruning only happens via an explicit :meth:`Retention.run_once` call,
``firm-audit prune``, or an opted-in :class:`RetentionLoop`. The default ``max_age=None`` means
"keep forever": :meth:`run_once` is then a no-op.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from .._core.clock import now_utc
from .._core.dialects import get_dialect
from .._core.poller import InterruptiblePoller
from . import schema

if TYPE_CHECKING:
    from .log import AuditLog

_audits = schema.audits

# Rows deleted per transaction. Batching keeps each delete short (no long-held locks over a
# large table) and lets concurrent pruners interleave instead of fighting.
_BATCH_SIZE = 1000


class Retention:
    def __init__(self, audit: AuditLog) -> None:
        self.audit = audit

    def run_once(self) -> int:
        """Delete all rows older than ``max_age`` seconds, in batches; return how many. A no-op
        (returns 0) when ``max_age`` is ``None`` (the default — keep forever).

        Each batch selects its victims with ``FOR UPDATE SKIP LOCKED`` (Postgres/MySQL) — the
        same pattern as :func:`firm.channel.messages.trim_old` — so two pruners running at
        once split the work instead of blocking on each other's rows.
        """
        max_age = self.audit.max_age
        if max_age is None:
            return 0
        cutoff = now_utc() - timedelta(seconds=max_age)
        engine = self.audit.engine
        dialect = get_dialect(engine)
        total = 0
        offset = 0
        while True:
            with dialect.begin_claim_tx(engine) as conn:
                from .sql_archive import eligible, lock_policy

                policy = lock_policy(conn)
                stmt = dialect.with_skip_locked(
                    select(_audits)
                    .where(_audits.c.created_at < cutoff)
                    .order_by(_audits.c.id)
                    .offset(offset)
                    .limit(_BATCH_SIZE)
                )
                rows = conn.execute(stmt).all()
                ids = [
                    row.id
                    for row in rows
                    if not policy or not policy.stream or eligible(conn, row, policy.stream)
                ]
                offset += len(rows) - len(ids)
                if ids:
                    conn.execute(delete(_audits).where(_audits.c.id.in_(ids)))
                    if policy is not None:
                        conn.execute(
                            delete(schema.archive_imports).where(
                                schema.archive_imports.c.event_id.in_(ids)
                            )
                        )
                        conn.execute(
                            delete(schema.archive_members).where(
                                schema.archive_members.c.event_id.in_(ids)
                            )
                        )
            total += len(ids)
            if len(rows) < _BATCH_SIZE:
                return total


class RetentionLoop(InterruptiblePoller):
    """Optional background loop that runs pruning on a timer. Off by default."""

    def __init__(
        self,
        retention: Retention,
        interval: float = 3600.0,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        super().__init__(interval, name="audit-retention", on_error=on_error)
        self.retention = retention

    def poll(self) -> int:
        return self.retention.run_once()
