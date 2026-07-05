"""The ``AuditLog`` — a database-backed, append-only audit log."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Connection, Engine

from .._core.database import create_engine_for, dispose_engine, transaction
from . import events, schema
from .events import Reference
from .retention import Retention, RetentionLoop


def record(
    conn: Connection,
    action: str,
    *,
    subject: Reference = None,
    actor: Reference = None,
    data: dict[str, Any] | None = None,
    changes: dict[str, Any] | None = None,
    correlation_id: str | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    """Record an event inside the caller's transaction (the shared-DB, atomic path).

    The row joins ``conn``'s transaction, so it commits or rolls back together with whatever else
    that transaction does — the same-transaction guarantee. This only holds when ``firm_audits``
    lives in the database ``conn`` belongs to.
    """
    events.append(
        conn,
        action=action,
        subject=subject,
        actor=actor,
        data=data,
        changes=changes,
        correlation_id=correlation_id,
        context=context,
    )


class AuditLog:
    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: Engine | None = None,
        create_schema: bool = True,
        max_age: float | None = None,
        background_retention: bool = False,
        retention_interval: float = 3600.0,
    ) -> None:
        if engine is not None:
            self.engine = engine
            self._owns_engine = False
        elif database_url is not None:
            self.engine = create_engine_for(database_url)
            self._owns_engine = True
        else:
            raise ValueError("AuditLog requires either database_url or engine")

        self.max_age = max_age

        if create_schema:
            schema.create_all(self.engine)

        self.retention = Retention(self)
        self._loop = (
            RetentionLoop(self.retention, retention_interval) if background_retention else None
        )
        if self._loop is not None:
            self._loop.start()

    def record(
        self,
        action: str,
        *,
        subject: Reference = None,
        actor: Reference = None,
        data: dict[str, Any] | None = None,
        changes: dict[str, Any] | None = None,
        correlation_id: str | None = None,
        context: dict[str, Any] | None = None,
        conn: Connection | None = None,
    ) -> None:
        """Record an event. Pass ``conn`` to join the caller's transaction (atomic, shared DB);
        omit it to write durably in this ``AuditLog``'s own transaction (separate DB or
        standalone — not atomic with anything else)."""
        if conn is not None:
            events.append(
                conn,
                action=action,
                subject=subject,
                actor=actor,
                data=data,
                changes=changes,
                correlation_id=correlation_id,
                context=context,
            )
        else:
            with transaction(self.engine) as own_conn:
                events.append(
                    own_conn,
                    action=action,
                    subject=subject,
                    actor=actor,
                    data=data,
                    changes=changes,
                    correlation_id=correlation_id,
                    context=context,
                )

    def history(
        self,
        *,
        subject: Reference = None,
        subject_type: str | None = None,
        subject_id: Any | None = None,
        actor: Reference = None,
        actor_type: str | None = None,
        actor_id: Any | None = None,
        action: str | None = None,
        correlation_id: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with transaction(self.engine) as conn:
            return events.history(
                conn,
                subject=subject,
                subject_type=subject_type,
                subject_id=subject_id,
                actor=actor,
                actor_type=actor_type,
                actor_id=actor_id,
                action=action,
                correlation_id=correlation_id,
                since=since,
                limit=limit,
            )

    def close(self) -> None:
        if self._loop is not None:
            self._loop.stop()
        if self._owns_engine:
            dispose_engine(self.engine)

    def __enter__(self) -> AuditLog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
