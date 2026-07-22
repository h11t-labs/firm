"""Backend-parametrized E2E coverage for concurrent writes, sealing, pruning, and verification."""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from firm._core.clock import now_utc
from firm._core.database import transaction
from firm.audit import AuditLog, schema

_SECRET = "e2e-secret-key-padding-0123456789abcdef"  # noqa: S105


def _max_audit_id(audit: AuditLog) -> int | None:
    with transaction(audit.engine) as conn:
        return conn.execute(select(func.max(schema.audit_events.c.id))).scalar()


def _max_sealed_id(audit: AuditLog) -> int | None:
    with transaction(audit.engine) as conn:
        return conn.execute(select(func.max(schema.seals.c.to_id))).scalar()


def test_tamper_evidence_lifecycle(db_url: str, tmp_path, at_time) -> None:
    anchor = tmp_path / "anchor.log"
    with pytest.warns(UserWarning):
        audit = AuditLog(
            database_url=db_url,
            mac_key=_SECRET,
            grace=0.0,
            background_sealing=True,
            seal_interval=0.02,
            anchor_path=str(anchor),
            anchor_max_age=3600.0,
            max_age=60.0,
        )
    try:
        for _ in range(100):
            with transaction(audit.engine) as conn:
                active = conn.execute(
                    select(func.count())
                    .select_from(schema.seals)
                    .where(schema.seals.c.kind == "activation")
                ).scalar_one()
            if active:
                break
            time.sleep(0.02)
        assert active == 1

        n_threads = 6
        per_thread = 15
        total = n_threads * per_thread
        errors: list[Exception] = []

        def writer(thread_id: int) -> None:
            try:
                for i in range(per_thread):
                    audit.record(f"e2e.{thread_id}.{i}", actor=("Worker", str(thread_id)))
            except Exception as exc:
                errors.append(exc)

        # Sign all lifecycle rows at a past instant so they are genuinely old (and expired against
        # max_age) without any MAC-invalidating created_at edit — retention re-verifies before it
        # prunes, so a mutated row would be refused, not laundered.
        threads = [threading.Thread(target=writer, args=(tid,)) for tid in range(n_threads)]
        old = now_utc() - timedelta(seconds=120)
        with at_time(old), patch("firm.audit.sealing.now_utc", lambda: old):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            written_max = _max_audit_id(audit)
            for _ in range(100):
                if _max_sealed_id(audit) == written_max:
                    break
                time.sleep(0.02)

        assert errors == []
        assert len(audit.history(limit=total)) == total

        assert _max_sealed_id(audit) == written_max
        assert written_max is not None

        audit.record("e2e.retention.keep", actor=("Worker", "retention"))  # fresh — must survive
        final_max = _max_audit_id(audit)
        for _ in range(100):
            if _max_sealed_id(audit) == final_max:
                break
            time.sleep(0.02)
        assert _max_sealed_id(audit) == final_max

        pruned = audit.retention.run_once()
        assert pruned == total
        with transaction(audit.engine) as conn:
            floors = conn.execute(
                select(func.count()).select_from(schema.seals).where(schema.seals.c.kind == "floor")
            ).scalar_one()
        assert floors >= 1

        report = audit.verify(anchor_path=str(anchor), full=True)
        assert report.outcome == "ok"
        assert report.exit_code == 0
    finally:
        audit.close()
