"""Retry, pagination, concurrency, and lossless S3 storage contracts."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from firm.audit import Ref
from firm.audit.object_store import ObjectConflict, Receipt
from firm.audit.s3 import PreparedEvent, UnknownEventOutcome

DAY = date(2026, 1, 1)
END = date(2026, 1, 2)
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_roundtrip_and_retry_across_instances(s3_log, object_client):
    event = s3_log.prepare(
        "invoice.paid",
        created_at=NOW,
        subject=Ref("Invoice", 42, "Named"),
        data={"money": Decimal("12.05"), "at": NOW, "uuid": UUID(int=1)},
        context={"none": None},
    )
    serial = json.dumps({"key": event.key, "body": event.body})
    receipt = s3_log.write(event)
    object_client.fail_after_put = True
    restored = PreparedEvent(**json.loads(serial))
    assert s3_log.write(restored) == receipt
    row = s3_log.get(receipt)
    assert row["subject_label"] == "Named"
    assert row["data"] == {"money": Decimal("12.05"), "at": NOW, "uuid": UUID(int=1)}
    assert row["created_at"] == NOW
    assert row["changes"] is None
    assert row["context"] == {"none": None}


def test_timeout_after_success_reconciles(s3_log, object_client):
    object_client.fail_after_put = True
    receipt = s3_log.record("test", created_at=NOW)
    assert s3_log.get(receipt)["action"] == "test"


def test_unknown_outcome_exposes_retryable_envelope(s3_log, object_client):
    object_client.fail_key = "/events/"
    with pytest.raises(UnknownEventOutcome) as caught:
        s3_log.record("test", created_at=NOW)
    prepared = caught.value.prepared
    object_client.fail_key = None
    assert s3_log.get(s3_log.write(prepared))["action"] == "test"


def test_changed_content_conflicts(s3_log):
    prepared = s3_log.prepare("a", created_at=NOW, event_id=str(UUID(int=5)))
    s3_log.write(prepared)
    changed = s3_log.prepare("b", created_at=NOW, event_id=str(UUID(int=5)))
    with pytest.raises(ObjectConflict):
        s3_log.write(changed)


def test_shared_prepared_event_has_single_logical_publication(s3_log, object_client):
    prepared = s3_log.prepare("one", created_at=NOW)
    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(s3_log.write, [prepared] * 20))
    assert len(set(receipts)) == 1
    assert len(object_client.objects[("events", prepared.key)]) == 1


def test_scan_never_skips_partially_consumed_page(s3_log):
    for i in range(20):
        s3_log.record("even" if i % 2 == 0 else "odd", created_at=NOW, event_id=str(UUID(int=i)))
    seen = []
    token = None
    while True:
        page = s3_log.scan(
            start=DAY,
            end=END,
            limit=2,
            max_objects=3,
            max_requests=3,
            action="even",
            continuation=token,
        )
        assert page.requests <= 3 and page.scanned <= 3
        seen.extend(row["id"] for row in page.items)
        if page.exhausted:
            assert page.continuation is None
            break
        token = page.continuation
    assert seen == [str(UUID(int=i)) for i in range(0, 20, 2)]


def test_zero_matches_still_advances_and_invalid_query_tokens_fail(s3_log):
    s3_log.record("a", created_at=NOW)
    page = s3_log.scan(start=DAY, end=END, max_requests=2, action="missing")
    assert page.items == []
    assert page.exhausted
    s3_log.record("b", created_at=NOW)
    page = s3_log.scan(start=DAY, end=END, limit=1)
    with pytest.raises(ValueError):
        s3_log.scan(start=DAY, end=END, action="b", continuation=page.continuation)
    with pytest.raises(ValueError):
        s3_log.scan(start=DAY, end=END, continuation="not a token")


def test_receipt_reads_original_version_and_verifies_digest(s3_log, object_client):
    receipt = s3_log.record("original", created_at=NOW)
    object_client.objects[("events", receipt.key)].append(b"modified")
    assert s3_log.get(receipt)["action"] == "original"
    with pytest.raises(ObjectConflict):
        s3_log.get(Receipt(receipt.key, "wrong", receipt.version_id))


def test_transaction_and_locator_mismatch_rejected(s3_log):
    with pytest.raises(ValueError, match="SQL transaction"):
        s3_log.record("test", conn=object())
    prepared = s3_log.prepare("a", created_at=NOW)
    with pytest.raises(ValueError):
        s3_log.write(PreparedEvent(prepared.key.replace("2026-01-01", "2026-01-02"), prepared.body))


def test_malformed_query_bound_cursor_is_value_error(s3_log):
    from firm.audit.event_format import token_encode

    filters = dict.fromkeys(
        ("action", "subject_type", "subject_id", "actor_type", "actor_id", "correlation_id")
    )
    query = [s3_log.store.identity, s3_log.stream, DAY.isoformat(), END.isoformat(), filters]
    for malformed in ({}, None, [DAY.isoformat()], 0):
        with pytest.raises(ValueError):
            s3_log.scan(start=DAY, end=END, continuation=token_encode(query, malformed))


def test_prepared_bytes_must_retain_canonical_format(s3_log, object_client):
    prepared = s3_log.prepare("a", created_at=NOW)
    reformatted = PreparedEvent(prepared.key, json.dumps(json.loads(prepared.body), indent=2))
    with pytest.raises(ValueError, match="canonical"):
        s3_log.write(reformatted)
    assert ("events", prepared.key) not in object_client.objects
