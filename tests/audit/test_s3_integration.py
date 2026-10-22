"""Opt-in real S3-compatible service tests, using an isolated prefix in a test bucket."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

from firm._core.clock import now_utc
from firm.audit import AuditLog, S3Storage
from firm.audit.archive import Archive
from firm.audit.event_format import canonical
from firm.audit.object_store import ObjectStore
from firm.audit.storage import StorageBusy

KEY = "live-provider-row-and-seal-key-0123456789abc"
STREAM = str(UUID(int=1))


@pytest.fixture
def live():
    bucket = os.getenv("FIRM_TEST_S3_BUCKET")
    # CI's live job sets this so a misconfiguration fails the run instead of skipping it green.
    required = bool(os.getenv("FIRM_TEST_S3_REQUIRED"))
    if not bucket:
        message = "set FIRM_TEST_S3_BUCKET and SDK credentials for real provider tests"
        if required:
            pytest.fail(message)
        pytest.skip(message)
    if required:
        import boto3
    else:
        boto3 = pytest.importorskip("boto3")
    client = boto3.client(
        "s3",
        endpoint_url=os.getenv("FIRM_TEST_S3_ENDPOINT"),
        region_name=os.getenv("AWS_REGION", "us-east-1"),
    )
    prefix = "firm-integration/" + str(uuid4())
    logs: list[AuditLog] = []

    def make(**kwargs) -> AuditLog:
        storage_options = {
            name: kwargs.pop(name)
            for name in ("workers", "lease_ttl", "lease_wait")
            if name in kwargs
        }
        log = AuditLog(
            storage=S3Storage(
                bucket, stream=STREAM, prefix=prefix, client=client, **storage_options
            ),
            **kwargs,
        )
        logs.append(log)
        return log

    archive = Archive(ObjectStore(bucket, prefix=prefix + "/cold", client=client))
    try:
        yield make, archive, client, bucket
    finally:
        for log in logs:
            log._stop_maintenance()
            log.storage.close()
        # Delete only this fixture's objects/versions, never the supplied bucket.
        for page in client.get_paginator("list_object_versions").paginate(
            Bucket=bucket, Prefix=prefix
        ):
            objects = [
                {"Key": obj["Key"], "VersionId": obj["VersionId"]}
                for obj in page.get("Versions", []) + page.get("DeleteMarkers", [])
            ]
            if objects:
                client.delete_objects(Bucket=bucket, Delete={"Objects": objects})
        client.close()


def test_live_concurrent_writers_and_retries(live):
    make, *_ = live
    log = make()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: log.record("parallel", data={"i": i}), range(24)))
    prepared = log.prepare("retried", subject=("Invoice", 42))
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = set(pool.map(lambda _: log.write(prepared), range(4)))
    assert len(receipts) == 1
    assert sorted(e["id"] for e in log.history(limit=100)) == list(range(1, 26))
    assert [e["action"] for e in log.history(subject=("Invoice", 42))] == ["retried"]


def test_live_seal_verify_tamper_and_lease(live):
    make, _, client, bucket = live
    log = make(mac_key=KEY, grace=0.0, seal_batch_size=3)
    with patch("firm.audit.events.now_utc", lambda: now_utc() - timedelta(hours=1)):
        for index in range(5):
            log.record(f"event.{index}")
    assert log.sealer.run_once() == 5
    assert log.verify(full=True).outcome == "ok"
    contender = make(mac_key=KEY, lease_wait=0.5)
    with log.storage.claim(), pytest.raises(StorageBusy):
        contender.storage.claim().__enter__()
    key = next(log.storage.store.iter_keys(log.storage.layout.events))
    row = json.loads(log.storage.store.read(key)[0])
    client.put_object(Bucket=bucket, Key=key, Body=canonical({**row, "action": "forged"}))
    assert log.verify(full=True).outcome == "tampered"


def test_live_export_restore_and_sql_outbox(live, tmp_path):
    from sqlalchemy import create_engine

    make, archive, *_ = live
    log = make(mac_key=KEY)
    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    from firm.audit.s3_outbox import create_outbox

    create_outbox(engine)
    with engine.begin() as conn:
        log.record("staged", conn=conn)
    log.record("direct")
    assert log.flush_outbox(engine) == 1
    receipt = archive.export_s3(log, batch_id=str(uuid4()))
    assert len(archive.read(receipt)) == 2
    target = AuditLog(
        storage=S3Storage(
            log.storage.store.bucket,
            stream=STREAM,
            prefix=log.storage.store.prefix + "restore",
            client=log.storage.store.client,
        ),
        mac_key=KEY,
    )
    try:
        assert archive.restore_s3(receipt, target) == 2
        assert target.history() == log.history()
        assert target.verify(full=True).outcome in {"ok", "warning"}
    finally:
        target._stop_maintenance()
        target.storage.close()


def test_process_death_after_claim_converges_on_retry(live, tmp_path):
    import subprocess
    import sys

    make, *_ = live
    log = make()
    prepared = log.prepare("survives-process-loss")
    path = tmp_path / "request-state.json"
    path.write_text(json.dumps({"body": prepared.body}))
    config = {
        "bucket": log.storage.store.bucket,
        "stream": log.storage.stream,
        "prefix": log.storage.store.prefix,
        "endpoint_url": os.getenv("FIRM_TEST_S3_ENDPOINT"),
    }
    env = {
        **os.environ,
        "FIRM_S3_PROCESS_CONFIG": json.dumps(config),
        "FIRM_S3_PROCESS_EVENTFILE": str(path),
    }
    script = """
import json, os
from pathlib import Path
from firm.audit import AuditLog, S3Storage, PreparedEvent
log = AuditLog(storage=S3Storage(**json.loads(os.environ["FIRM_S3_PROCESS_CONFIG"])))
event = PreparedEvent(**json.loads(Path(os.environ["FIRM_S3_PROCESS_EVENTFILE"]).read_text()))
if os.getenv("FIRM_S3_CRASH_AFTER_CLAIM"):
    original = log.storage._write_row
    def crash(row):
        os._exit(17)
    log.storage._write_row = crash
receipt = log.write(event)
print(json.dumps(receipt.key))
log.close()
"""
    failed = subprocess.run(  # noqa: S603 - fixed interpreter/script, no shell
        [sys.executable, "-c", script],
        env={**env, "FIRM_S3_CRASH_AFTER_CLAIM": "1"},
        capture_output=True,
        timeout=60,
    )
    assert failed.returncode == 17
    with ThreadPoolExecutor(max_workers=2) as pool:
        completed = list(
            pool.map(
                lambda _: subprocess.run(  # noqa: S603 - fixed interpreter/script, no shell
                    [sys.executable, "-c", script],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                ),
                range(2),
            )
        )
    assert all(p.returncode == 0 for p in completed), [p.stderr for p in completed]
    assert completed[0].stdout == completed[1].stdout
    assert [e["action"] for e in log.history()] == ["survives-process-loss"]
