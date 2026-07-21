"""Fixtures for the UI tests — a database with all four schemas, a seeder, and a Dashboard."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy import insert

from firm._core.clock import now_utc
from firm._core.config import Runtime
from firm.audit import schema as audit_schema
from firm.cache import schema as cache_schema
from firm.cache.keys import key_hash
from firm.channel import schema as channel_schema
from firm.channel.keys import channel_hash
from firm.queue import schema as queue_schema
from firm.queue.config import configure, set_runtime
from firm.ui.context import Dashboard, build_dashboard


@pytest.fixture
def db_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'app.db'}"


@pytest.fixture
def runtime(db_url: str) -> Iterator[Runtime]:
    rt = configure(db_url)
    queue_schema.create_all(rt.engine)
    cache_schema.create_all(rt.engine)
    channel_schema.create_all(rt.engine)
    audit_schema.create_all(rt.engine)
    try:
        yield rt
    finally:
        set_runtime(None)


@pytest.fixture
def dashboard(runtime: Runtime, db_url: str) -> Iterator[Dashboard]:
    dash = build_dashboard(database_url=db_url)
    try:
        yield dash
    finally:
        dash.close()


class Seeder:
    """Inserts rows directly so tests can set up each part's state."""

    def __init__(self, runtime: Runtime) -> None:
        self.engine = runtime.engine

    # -- queue ---------------------------------------------------------------------------------

    def _job(self, *, queue: str = "default", class_name: str = "app.task", finished: bool = False):
        with self.engine.begin() as conn:
            return conn.execute(
                insert(queue_schema.jobs).values(
                    queue_name=queue,
                    class_name=class_name,
                    finished_at=now_utc() if finished else None,
                )
            ).inserted_primary_key[0]

    def ready(self, *, queue: str = "default") -> int:
        job_id = self._job(queue=queue)
        with self.engine.begin() as conn:
            conn.execute(
                insert(queue_schema.ready_executions).values(
                    job_id=job_id, queue_name=queue, priority=0
                )
            )
        return job_id

    def scheduled(self, *, queue: str = "default") -> int:
        job_id = self._job(queue=queue)
        with self.engine.begin() as conn:
            conn.execute(
                insert(queue_schema.scheduled_executions).values(
                    job_id=job_id, queue_name=queue, priority=0, scheduled_at=now_utc()
                )
            )
        return job_id

    def claimed(self, *, process_id: int = 1) -> int:
        job_id = self._job()
        with self.engine.begin() as conn:
            conn.execute(
                insert(queue_schema.claimed_executions).values(job_id=job_id, process_id=process_id)
            )
        return job_id

    def failed(self, *, error: str = "Traceback...\nValueError: boom") -> int:
        job_id = self._job()
        with self.engine.begin() as conn:
            conn.execute(insert(queue_schema.failed_executions).values(job_id=job_id, error=error))
        return job_id

    def finished(self) -> int:
        return self._job(finished=True)

    def process(self, *, kind: str = "worker", name: str = "w1", age_seconds: float = 0.0) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                insert(queue_schema.processes).values(
                    kind=kind,
                    name=name,
                    pid=4242,
                    hostname="testhost",
                    last_heartbeat_at=now_utc() - timedelta(seconds=age_seconds),
                )
            )

    def recurring_task(
        self,
        *,
        key: str = "cleanup",
        schedule: str = "*/10 * * * *",
        class_name: str = "app.task",
        queue: str = "default",
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                insert(queue_schema.recurring_tasks).values(
                    key=key, schedule=schedule, class_name=class_name, queue_name=queue
                )
            )

    # -- cache ---------------------------------------------------------------------------------

    def cache_entry(self, *, key: bytes = b"user:1", value: bytes = b"payload") -> None:
        with self.engine.begin() as conn:
            conn.execute(
                insert(cache_schema.entries).values(
                    key=key,
                    value=value,
                    key_hash=key_hash(key),
                    byte_size=len(key) + len(value) + 140,
                    created_at=now_utc(),
                )
            )

    # -- channel -------------------------------------------------------------------------------

    def channel_message(
        self, *, channel: bytes = b"room:1", payload: bytes = b"hello", age_seconds: float = 0.0
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                insert(channel_schema.messages).values(
                    channel=channel,
                    payload=payload,
                    channel_hash=channel_hash(channel),
                    created_at=now_utc() - timedelta(seconds=age_seconds),
                )
            )

    # -- audit -----------------------------------------------------------------------------

    def audit_record(
        self,
        *,
        action: str = "user.login",
        subject_type: str | None = "User",
        subject_id: str | None = "1",
        subject_label: str | None = None,
        actor_type: str | None = None,
        actor_id: str | None = None,
        actor_label: str | None = None,
        correlation_id: str | None = None,
        data: str | None = None,
        row_mac: str | None = None,
    ) -> int:
        """``row_mac`` left None mimics a legacy/pre-key row (renders "unprotected"); pass hex to
        mimic a signed row (renders sealed/unsealed depending on the seal range)."""
        with self.engine.begin() as conn:
            return conn.execute(
                insert(audit_schema.audit_events).values(
                    action=action,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    subject_label=subject_label,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    actor_label=actor_label,
                    correlation_id=correlation_id,
                    data=data,
                    row_mac=row_mac,
                    created_at=now_utc(),
                )
            ).inserted_primary_key[0]

    def seal(
        self,
        *,
        seq: int = 1,
        kind: str = "seal",
        from_id: int = 0,
        to_id: int = 1,
        row_count: int = 1,
        sealed_at=None,
    ) -> None:
        """A ``firm_audit_seals`` row — the panel reads only ``sealed_at`` (for the activation
        moment), so the MAC columns get harmless placeholder hex."""
        with self.engine.begin() as conn:
            conn.execute(
                insert(audit_schema.seals).values(
                    seq=seq,
                    kind=kind,
                    from_id=from_id,
                    to_id=to_id,
                    row_count=row_count,
                    rows_mac="00" * 32,
                    prev_mac="genesis",
                    seal_mac="ab" * 32,
                    sealed_at=sealed_at or now_utc(),
                    key_id="deadbeef",
                )
            )

    def verify_status(
        self,
        *,
        outcome: str = "ok",
        ran_at=None,
        ok_count: int = 0,
        warning_count: int = 0,
        unprotected_count: int = 0,
        tampered_count: int = 0,
        error_message: str | None = None,
        last_full_coverage_at=None,
        cycle_position: int | None = None,
        cycle_length: int | None = None,
        newest_anchor_at=None,
        anchor_configured: bool = False,
        unsealed_tail_count: int = 0,
        unsealed_tail_oldest_at=None,
        affected_identifiers: str | None = None,
        duration_seconds: float | None = None,
    ) -> None:
        """The single ``firm_audit_verify_status`` row the verifier would upsert."""
        with self.engine.begin() as conn:
            conn.execute(
                insert(audit_schema.verify_status).values(
                    ran_at=ran_at or now_utc(),
                    outcome=outcome,
                    ok_count=ok_count,
                    warning_count=warning_count,
                    unprotected_count=unprotected_count,
                    tampered_count=tampered_count,
                    error_message=error_message,
                    last_full_coverage_at=last_full_coverage_at,
                    cycle_position=cycle_position,
                    cycle_length=cycle_length,
                    newest_anchor_at=newest_anchor_at,
                    anchor_configured=anchor_configured,
                    unsealed_tail_count=unsealed_tail_count,
                    unsealed_tail_oldest_at=unsealed_tail_oldest_at,
                    affected_identifiers=affected_identifiers,
                    duration_seconds=duration_seconds,
                )
            )


@pytest.fixture
def seed(runtime: Runtime) -> Seeder:
    return Seeder(runtime)
