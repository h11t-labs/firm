"""AuditLog's storage contract, durable retries and resource ownership."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch
from uuid import UUID

import pytest
from sqlalchemy import create_engine, event

from firm._core.clock import now_utc
from firm._core.database import create_engine_for
from firm.audit import AuditLog, PreparedEvent, S3Storage, SqlStorage, Storage
from firm.audit.archive import Archive
from firm.audit.event_format import canonical
from firm.audit.object_store import ObjectConflict, ObjectStore
from firm.audit.s3 import S3AuditLog
from firm.audit.sql_archive import Archiver
from tests.audit.conftest import MemoryS3

STREAM = str(UUID(int=1))
KEY = "shared-storage-contract-key-0123456789abcdef"


@pytest.fixture(params=["sql", "s3"])
def storage(request, db_url):
    adapter = (
        SqlStorage(database_url=db_url)
        if request.param == "sql"
        else S3Storage("events", stream=STREAM, client=MemoryS3())
    )
    try:
        yield adapter
    finally:
        adapter.close()


class OpaqueStorage:
    """Expose only the declared contract, hiding the adapter's engine, store and concrete type."""

    def __init__(self, adapter):
        self._adapter = adapter

    def __getattr__(self, name):
        if name in Storage.__dict__:
            return getattr(self._adapter, name)
        raise AttributeError(name)


def test_audit_policy_uses_only_the_storage_contract(storage):
    archive = Archive(ObjectStore("cold", client=MemoryS3()))
    with AuditLog(storage=OpaqueStorage(storage), mac_key=KEY, grace=0, max_age=3600) as log:
        with patch("firm.audit.events.now_utc", lambda: now_utc() - timedelta(hours=2)):
            log.record("invoice.paid", subject=("Invoice", 42), data={"amount": 12})
            log.record("invoice.sent", subject=("Invoice", 42))
        assert [row["action"] for row in log.history(subject=("Invoice", 42))] == [
            "invoice.sent",
            "invoice.paid",
        ]
        assert log.get(1)["data"] == {"amount": 12}
        with patch("firm.audit.sealing.now_utc", lambda: now_utc() - timedelta(minutes=90)):
            assert log.sealer.run_once() == 2
        assert log.verify(full=True).outcome == "ok"
        archiver = Archiver(log, archive, stream=STREAM)
        archiver.enable()
        assert log.retention.run_once() == 0
        receipt = archiver.run_once()
        assert len(archive.read(receipt)) == 2
        assert log.retention.run_once() == 2
        assert log.history() == []
        assert log.verify(full=True).tampered_count == 0
    archive.close()


def test_prepared_retries_keep_one_event_and_reject_changed_bytes(storage):
    with AuditLog(storage=storage, mac_key=KEY) as log:
        prepared = log.prepare("invoice.paid", data={"amount": 12})
        persisted = PreparedEvent(**json.loads(json.dumps({"body": prepared.body})))
        first = log.write(prepared)
        assert log.write(persisted) == first
        assert len(log.history()) == 1
        assert log.get(first)["data"] == {"amount": 12}
        changed = {**json.loads(prepared.body), "action": "forged"}
        with pytest.raises(ObjectConflict):
            log.write(PreparedEvent(canonical(changed).decode()))
        assert log.get(first)["action"] == "invoice.paid"


def test_concurrent_prepared_retries_converge(storage):
    with AuditLog(storage=storage) as log:
        prepared = log.prepare("one-event")
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: log.write(prepared), range(8)))
        assert len(set(results)) == 1
        assert [row["action"] for row in log.history()] == ["one-event"]


def test_close_is_idempotent_and_closes_the_adapter(storage):
    with patch.object(storage, "close", wraps=storage.close) as close:
        log = AuditLog(storage=storage)
        log.record("before-close")
        log.close()
        log.close()
        close.assert_called_once()


def test_injected_sql_engine_remains_caller_owned(db_url):
    engine = create_engine_for(db_url)
    disposed = []
    event.listen(engine, "engine_disposed", lambda _: disposed.append(True))
    with AuditLog(storage=SqlStorage(engine)) as log:
        log.record("caller-owned")
    assert disposed == []
    engine.dispose()


def test_injected_sdk_client_remains_caller_owned():
    client = MemoryS3()
    with patch.object(client, "close", create=True) as close:
        with AuditLog(storage=S3Storage("events", stream=STREAM, client=client)) as log:
            log.record("caller-owned")
        close.assert_not_called()


def test_conflicting_storage_configuration_fails_before_adoption(storage):
    with patch.object(storage, "close", wraps=storage.close) as close:
        with pytest.raises(ValueError, match="not both"):
            AuditLog("sqlite://", storage=storage)
        close.assert_not_called()


def test_failing_outbox_initialization_closes_storage(monkeypatch):
    from firm.audit.storage.s3 import outbox as s3_outbox

    def unavailable(*args, **kwargs):
        raise RuntimeError("outbox unavailable")

    monkeypatch.setattr(s3_outbox, "create_outbox", unavailable)
    storage = S3Storage("events", stream=STREAM, client=MemoryS3())
    engine = create_engine("sqlite://")
    try:
        with patch.object(storage, "close", wraps=storage.close) as close:
            with pytest.raises(RuntimeError, match="outbox unavailable"):
                AuditLog(storage=storage, outbox_engine=engine)
            close.assert_called_once()
    finally:
        engine.dispose()


def test_legacy_s3_constructor_uses_the_same_audit_log():
    with S3AuditLog("events", stream=STREAM, client=MemoryS3(), mac_key=KEY) as log:
        assert isinstance(log, AuditLog)
        assert S3AuditLog.record is AuditLog.record
        assert S3AuditLog.prepare is AuditLog.prepare
        log.record("compatibility")
        assert log.history()[0]["action"] == "compatibility"


def test_one_prepared_event_can_be_written_to_either_storage(db_url):
    with (
        AuditLog(storage=SqlStorage(database_url=db_url), mac_key=KEY, grace=0) as sql,
        AuditLog(
            storage=S3Storage("events", stream=STREAM, client=MemoryS3()), mac_key=KEY, grace=0
        ) as s3,
    ):
        prepared = sql.prepare(
            "portable", data={"amount": 12}, created_at=now_utc() - timedelta(hours=2)
        )
        sql.write(prepared)
        s3.write(prepared)
        assert sql.history() == s3.history()
        for log in (sql, s3):
            assert log.sealer.run_once() == 1
            assert log.verify(full=True).outcome == "ok"


def test_invalid_keys_close_the_selected_adapter(storage):
    with patch.object(storage, "close", wraps=storage.close) as close:
        with pytest.raises(ValueError):
            AuditLog(storage=storage, mac_key="short")
        close.assert_called_once()
