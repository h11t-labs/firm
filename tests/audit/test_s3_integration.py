"""Opt-in real S3-compatible service tests, using an isolated prefix in a test bucket."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest

from firm.audit.archive import Archive
from firm.audit.object_store import ObjectConflict, ObjectStore
from firm.audit.s3 import S3AuditLog
from firm.audit.sql_archive import SQLArchiver


@pytest.fixture
def live_storage():
    bucket = os.getenv("FIRM_TEST_S3_BUCKET")
    if not bucket:
        pytest.skip("set FIRM_TEST_S3_BUCKET and SDK credentials for real provider tests")
    boto3 = pytest.importorskip("boto3")
    client = boto3.client(
        "s3",
        endpoint_url=os.getenv("FIRM_TEST_S3_ENDPOINT"),
        region_name=os.getenv("AWS_REGION", "us-east-1"),
    )
    prefix = "firm-integration/" + str(uuid4())
    log = S3AuditLog(bucket, stream=str(UUID(int=1)), prefix=prefix, client=client)
    archive = Archive(ObjectStore(bucket, prefix=prefix + "/cold", client=client))
    try:
        yield log, archive
    finally:
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


def test_live_conditional_publication_pagination_archive_and_restore(live_storage):
    log, archive = live_storage
    now = datetime(2026, 1, 1, tzinfo=UTC)
    event = log.prepare("one", created_at=now)
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(log.write, [event] * 8))
    assert len(set(receipts)) == 1
    assert log.get(receipts[0])["action"] == "one"
    conflicting = log.prepare("other", created_at=now, event_id=log.get(receipts[0])["id"])
    with pytest.raises(ObjectConflict):
        log.write(conflicting)
    for i in range(5):
        log.record("many", created_at=now, data={"i": i})
    token = None
    rows = []
    while True:
        page = log.scan(start=date(2026, 1, 1), end=date(2026, 1, 2), limit=2, continuation=token)
        rows.extend(page.items)
        if page.exhausted:
            break
        token = page.continuation
    assert len(rows) == 6 and len({row["id"] for row in rows}) == 6
    receipt = archive.export_s3(
        log, start=date(2026, 1, 1), end=date(2026, 1, 2), batch_id=str(uuid4())
    )
    assert len(archive.read(receipt)) == 6
    target = S3AuditLog(
        log.store.bucket,
        stream=log.stream,
        prefix=log.store.prefix + "restore",
        client=log.store.client,
    )
    assert len(archive.restore_s3(receipt, target)) == 6


def test_live_sql_publication_and_guarded_prune(live_storage, audit):
    _, archive = live_storage
    worker = SQLArchiver(audit, archive, stream=str(UUID(int=1)))
    worker.enable()
    audit.record("old")
    audit.max_age = -1
    assert audit.retention.run_once() == 0
    receipt = worker.run_once()
    assert len(archive.read(receipt)) == 1
    assert audit.retention.run_once() == 1


def test_process_restart_after_accepted_put_uses_durable_retry(live_storage, tmp_path):
    import json
    import subprocess
    import sys
    from dataclasses import asdict

    log, _ = live_storage
    event = log.prepare("survives-process-loss", created_at=datetime(2026, 1, 1, tzinfo=UTC))
    path = tmp_path / "request-state.json"
    path.write_text(json.dumps(asdict(event)))
    config = {
        "bucket": log.store.bucket,
        "stream": log.stream,
        "prefix": log.store.prefix,
        "endpoint_url": os.getenv("FIRM_TEST_S3_ENDPOINT"),
    }
    env = {
        **os.environ,
        "FIRM_S3_PROCESS_CONFIG": json.dumps(config),
        "FIRM_S3_PROCESS_EVENTFILE": str(path),
    }
    script = """
import json, os
from dataclasses import asdict
from pathlib import Path
from firm.audit.s3 import S3AuditLog, PreparedEvent
log = S3AuditLog(**json.loads(os.environ["FIRM_S3_PROCESS_CONFIG"]))
event = PreparedEvent(**json.loads(Path(os.environ["FIRM_S3_PROCESS_EVENTFILE"]).read_text()))
if os.getenv("FIRM_S3_CRASH_AFTER_PUT"):
    original = log.store.client.put_object
    def crash(**kwargs):
        original(**kwargs)
        os._exit(17)
    log.store.client.put_object = crash
receipt = log.write(event)
print(json.dumps(asdict(receipt)))
log.close()
"""
    failed = subprocess.run(  # noqa: S603 - fixed interpreter/script, no shell
        [sys.executable, "-c", script],
        env={**env, "FIRM_S3_CRASH_AFTER_PUT": "1"},
        capture_output=True,
        timeout=30,
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
                    timeout=30,
                ),
                range(2),
            )
        )
    assert all(p.returncode == 0 for p in completed), [p.stderr for p in completed]
    receipts = [json.loads(p.stdout) for p in completed]
    assert receipts[0] == receipts[1]
    assert log.get(receipts[0]["key"])["action"] == "survives-process-loss"
