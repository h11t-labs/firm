"""Archive recovery, SQL coverage/pruning, and exact restores."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from firm._core.database import transaction
from firm.audit import AuditLog, record, schema
from firm.audit.archive import sql_entry
from firm.audit.object_store import ObjectConflict, UnknownWriteOutcome
from firm.audit.sql_archive import SQLArchiver, restore_sql

STREAM = str(UUID(int=1))
NOW = datetime(2020, 1, 1, tzinfo=UTC)


def archiver(audit, archive):
    result = SQLArchiver(audit, archive, stream=STREAM)
    result.enable()
    return result


def test_archive_blocks_unverified_pruning_and_resumes(audit, archive, object_client):
    audit.record("old", data={"decimal": Decimal("1.20")})
    audit.max_age = -1
    worker = archiver(audit, archive)
    assert audit.retention.run_once() == 0
    object_client.fail_key = "manifest.json"
    with pytest.raises(UnknownWriteOutcome):
        worker.run_once()
    assert worker.status() == {"events": 1, "unarchived": 1, "pending_batches": 1}
    assert audit.retention.run_once() == 0
    object_client.fail_key = None
    receipt = worker.run_once()
    assert archive.read(receipt)[0]["event"]["action"] == "old"
    assert audit.retention.run_once() == 1
    assert worker.status()["unarchived"] == 0
    assert archive.read(receipt)[0]["original_id"] == 1


def test_sql_restore_keeps_identity_timestamp_and_tagged_data(audit, archive, tmp_path):
    audit.record("a", data={"at": NOW, "decimal": Decimal("1.20")}, subject=("Invoice", 42))
    worker = archiver(audit, archive)
    receipt = worker.run_once()
    with AuditLog(f"sqlite:///{tmp_path / 'restore.db'}") as target:
        assert restore_sql(archive, [receipt, receipt], target) == 1
        original = audit.history()[0]
        assert target.history()[0] == original
        target.record("next")
        assert target.history()[0]["id"] > original["id"]
        with pytest.raises(ValueError, match="empty"):
            restore_sql(archive, [receipt], target)


def test_discovery_does_not_skip_later_visible_lower_id(audit, archive):
    with transaction(audit.engine) as conn:
        conn.execute(schema.audits.insert().values(id=20, action="high", created_at=NOW))
    worker = archiver(audit, archive)
    assert worker.run_once() is not None
    with transaction(audit.engine) as conn:
        conn.execute(schema.audits.insert().values(id=10, action="late-low", created_at=NOW))
    receipt = worker.run_once()
    assert [entry["original_id"] for entry in archive.read(receipt)] == [10]


def test_discovery_advances_past_already_archived_prefix(audit, archive):
    for _ in range(12):
        audit.record("event")
    worker = archiver(audit, archive)
    receipts = [worker.run_once(batch_size=2) for _ in range(6)]
    assert all(receipts)
    assert worker.run_once() is None
    assert worker.status()["unarchived"] == 0


def test_module_level_record_is_covered(audit, archive):
    worker = archiver(audit, archive)
    with transaction(audit.engine) as conn:
        record(conn, "module")
    assert archive.read(worker.run_once())[0]["event"]["action"] == "module"


def test_reused_sqlite_id_does_not_inherit_acknowledgement(audit, archive, is_sqlite):
    if not is_sqlite:
        pytest.skip("SQLite-specific integer reuse")
    worker = archiver(audit, archive)
    audit.record("first")
    worker.run_once()
    audit.max_age = -1
    assert audit.retention.run_once() == 1
    audit.record("second")
    assert audit.history()[0]["id"] == 1
    assert audit.retention.run_once() == 0
    assert worker.run_once() is not None
    assert audit.retention.run_once() == 1


def test_concurrent_exporters_freeze_one_batch(audit, archive):
    worker = archiver(audit, archive)
    with ThreadPoolExecutor(max_workers=4) as pool:
        for _ in range(20):
            audit.record("one")
            results = list(pool.map(lambda _: worker.run_once(), range(8)))
            receipts = {result for result in results if result is not None}
            assert len(receipts) == 1
            assert worker.status()["unarchived"] == 0


def test_archive_corruption_and_input_conflict_fail(audit, archive, object_client):
    audit.record("a")
    entry = sql_entry(STREAM, audit.history()[0])
    batch_id = str(uuid4())
    receipt = archive.publish([entry], batch_id=batch_id)
    audit.record("b")
    with pytest.raises(ObjectConflict):
        archive.publish([sql_entry(STREAM, audit.history()[0])], batch_id=batch_id)
    data_key = receipt.key.replace("manifest.json", "data.jsonl.gz")
    object_client.objects[("archives", data_key)][0] = b"corrupt"
    with pytest.raises(ObjectConflict):
        archive.read(receipt)


def test_s3_archive_restore_and_resume(s3_log, archive, object_client):
    s3_log.record("one", created_at=NOW, data={"amount": Decimal("2.00")})
    batch_id = str(uuid4())
    kwargs = {"start": date(2020, 1, 1), "end": date(2020, 1, 2), "batch_id": batch_id}
    object_client.fail_key = "manifest.json"
    with pytest.raises(UnknownWriteOutcome):
        archive.export_s3(s3_log, **kwargs)
    object_client.fail_key = None
    receipt = archive.export_s3(s3_log, **kwargs)
    from firm.audit.s3 import S3AuditLog

    target = S3AuditLog("restored", stream=STREAM, prefix="other", client=object_client)
    restored = archive.restore_s3(receipt, target)
    assert target.get(restored[0])["data"] == {"amount": Decimal("2.00")}
    assert archive.restore_s3(receipt, target) == restored
    assert s3_log.scan(start=date(2020, 1, 1), end=date(2020, 1, 2)).items


def test_policy_cannot_silently_switch_destinations(audit, archive):
    archiver(audit, archive)
    with pytest.raises(ValueError):
        SQLArchiver(audit, archive, stream=str(UUID(int=2))).enable()
    with transaction(audit.engine) as conn:
        assert conn.execute(select(schema.archive_policy.c.stream)).scalar_one() == STREAM


def test_commit_order_gap_on_live_sql(audit, archive, is_sqlite):
    if is_sqlite:
        pytest.skip("requires concurrent SQL writers")
    worker = archiver(audit, archive)
    with audit.engine.connect() as delayed:
        tx = delayed.begin()
        try:
            record(delayed, "delayed")
            audit.record("committed-first")
            first = archive.read(worker.run_once())
            assert [e["event"]["action"] for e in first] == ["committed-first"]
            tx.commit()
        finally:
            if tx.is_active:
                tx.rollback()
    second = archive.read(worker.run_once())
    assert [e["event"]["action"] for e in second] == ["delayed"]
    assert second[0]["original_id"] < first[0]["original_id"]


def test_copy_only_export_preserves_retention_and_frozen_selection(audit, archive):
    audit.record("first")
    batch_id = str(uuid4())
    receipt = archive.export_sql(audit, stream=STREAM, batch_id=batch_id)
    audit.record("second")
    assert archive.export_sql(audit, stream=STREAM, batch_id=batch_id) == receipt
    assert len(archive.read(receipt)) == 1
    with transaction(audit.engine) as conn:
        assert conn.execute(select(schema.archive_policy.c.stream)).scalar_one() is None
    audit.max_age = -1
    assert audit.retention.run_once() == 2


def test_pruning_passes_blocked_rows_without_skipping_eligible_rows(audit, archive, monkeypatch):
    monkeypatch.setattr("firm.audit.retention._BATCH_SIZE", 2)
    for i in range(6):
        audit.record(f"event-{i}")
    worker = archiver(audit, archive)
    worker.run_once(batch_size=2)
    # Leave newer records blocked; first eligible two are still pruned.
    audit.max_age = -1
    assert audit.retention.run_once() == 2
    assert len(audit.history()) == 4
    worker.run_once(batch_size=4)
    assert audit.retention.run_once() == 4


def test_copy_only_pagination_and_overlap_read(audit, archive):
    with transaction(audit.engine) as conn:
        conn.execute(
            schema.audits.insert(), [{"action": "same-time", "created_at": NOW} for _ in range(7)]
        )
    token = None
    receipts = []
    while True:
        receipt = archive.export_sql(
            audit, stream=STREAM, batch_id=str(uuid4()), limit=2, continuation=token
        )
        receipts.append(receipt)
        manifest = archive.inspect(receipt)
        if manifest["scope"]["traversal_exhausted"]:
            break
        token = manifest["scope"]["continuation"]
    assert len(receipts) == 4
    rows = archive.read_many([*receipts, receipts[0]])
    assert [row["original_id"] for row in rows] == list(range(1, 8))
    assert len(audit.history()) == 7


def test_cross_backend_restore_preserves_origin(audit, archive, s3_log, tmp_path):
    from firm.audit.sql_archive import import_provenance

    audit.record("sql-origin", data={"money": Decimal("4.00")})
    receipt = archive.export_sql(audit, stream=STREAM, batch_id=str(uuid4()))
    restored = archive.restore_s3(receipt, s3_log)
    origin = s3_log.get(restored[0])["provenance"]
    assert origin["source"] == "sql" and origin["original_id"] == 1
    assert archive.restore_s3(receipt, s3_log) == restored
    timestamp = audit.history()[0]["created_at"].date()
    from datetime import timedelta

    converted = archive.export_s3(
        s3_log, start=timestamp, end=timestamp + timedelta(days=1), batch_id=str(uuid4())
    )
    with AuditLog(f"sqlite:///{tmp_path / 'converted.db'}") as target:
        assert restore_sql(archive, [converted], target) == 1
        assert target.history()[0]["data"] == {"money": Decimal("4.00")}
        with transaction(target.engine) as conn:
            assert import_provenance(conn, target.history()[0]["id"]) == origin
        reexported = archive.export_sql(target, stream=STREAM, batch_id=str(uuid4()))
        assert archive.read(reexported)[0]["provenance"] == origin


def test_restore_generated_identity_on_each_database(audit, archive):
    worker = archiver(audit, archive)
    audit.record("original")
    original = audit.history()[0]
    receipt = worker.run_once()
    audit.max_age = -1
    assert audit.retention.run_once() == 1
    assert restore_sql(archive, [receipt], audit) == 1
    audit.record("next")
    assert audit.history()[0]["id"] > original["id"]
    assert audit.history()[1] == original


def test_overlap_rejects_conflicting_original_provenance(audit, archive):
    audit.record("event")
    entry = sql_entry(STREAM, audit.history()[0])
    first = archive.publish([entry])
    conflicting = {**entry, "provenance": {"source": "sql", "stream": STREAM, "identity": "other"}}
    second = archive.publish([conflicting])
    with pytest.raises(ObjectConflict, match="identity"):
        archive.read_many([first, second])
