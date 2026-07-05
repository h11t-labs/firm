"""Retention: keep-forever default, age-based pruning, and the background loop."""

from __future__ import annotations

import time
from datetime import timedelta

from sqlalchemy import update

from firm._core.clock import now_utc
from firm._core.database import transaction
from firm.audit import AuditLog, schema
from firm.audit.retention import RetentionLoop


def test_keep_forever_default_is_a_noop(audit: AuditLog) -> None:
    audit.record("a")
    audit.record("b")
    assert audit.max_age is None
    assert audit.retention.run_once() == 0
    assert len(audit.history()) == 2


def test_prune_deletes_only_rows_older_than_max_age(db_url: str) -> None:
    audit = AuditLog(database_url=db_url, max_age=3600.0)
    try:
        audit.record("old")
        audit.record("new")
        with transaction(audit.engine) as conn:
            conn.execute(
                update(schema.audits)
                .where(schema.audits.c.action == "old")
                .values(created_at=now_utc() - timedelta(hours=2))
            )

        assert audit.retention.run_once() == 1
        rows = audit.history()
        assert [r["action"] for r in rows] == ["new"]
    finally:
        audit.close()


def test_recording_never_triggers_retention(db_url: str) -> None:
    audit = AuditLog(database_url=db_url, max_age=0.001)
    try:
        audit.record("a")
        time.sleep(0.05)
        audit.record("b")
        # max_age is aggressively short, but record() never calls into retention itself.
        assert len(audit.history()) == 2
    finally:
        audit.close()


def test_background_retention_flag_starts_loop(db_url: str) -> None:
    audit = AuditLog(
        database_url=db_url, max_age=3600.0, background_retention=True, retention_interval=0.05
    )
    try:
        assert audit._loop is not None
        assert audit._loop.name == "audit-retention"
    finally:
        audit.close()


def test_retention_loop_runs_a_pass(db_url: str) -> None:
    audit = AuditLog(database_url=db_url, max_age=3600.0)
    try:
        audit.record("old")
        with transaction(audit.engine) as conn:
            conn.execute(
                update(schema.audits)
                .where(schema.audits.c.action == "old")
                .values(created_at=now_utc() - timedelta(hours=2))
            )

        loop = RetentionLoop(audit.retention, interval=0.05)
        loop.start()
        try:
            for _ in range(40):
                if not audit.history():
                    break
                time.sleep(0.05)
            assert audit.history() == []
        finally:
            loop.stop()
    finally:
        audit.close()
