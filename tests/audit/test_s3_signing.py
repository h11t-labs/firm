"""Signed S3 events: HMAC on prepare, verification on every read, downgrade and tamper handling."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest

from firm.audit.archive import Archive
from firm.audit.event_format import canonical
from firm.audit.object_store import ObjectStore
from firm.audit.s3 import EventSignatureError, PreparedEvent, S3AuditLog

STREAM = str(UUID(int=1))
KEY = "s3-event-signing-test-key-0123456789abcdef"
OLD_KEY = "s3-event-signing-retired-key-0123456789ab"
DAY = datetime(2026, 1, 1, tzinfo=UTC)


def _log(client, **kwargs) -> S3AuditLog:
    return S3AuditLog("events", stream=STREAM, client=client, **kwargs)


def _overwrite(client, key: str, envelope: dict) -> None:
    """Replace an object's bytes the way someone with plain PutObject access could."""
    client.put_object(Bucket="events", Key=key, Body=canonical(envelope))


def test_prepare_signs_once_so_retries_write_identical_bytes(object_client):
    log = _log(object_client, mac_key=KEY)
    prepared = log.prepare("invoice.paid", subject=("Invoice", 42), data={"amount": 1})
    sig = json.loads(prepared.body)["sig"]
    assert sig["key_id"] == log.key_id and len(sig["mac"]) == 64
    first, second = log.write(prepared), log.write(prepared)
    assert first == second
    assert log.get(first)["signature"] == {"status": "valid", "key_id": log.key_id}


def test_every_envelope_field_is_covered(object_client):
    log = _log(object_client, mac_key=KEY)
    receipt = log.record("invoice.paid", data={"amount": 1}, created_at=DAY)
    envelope = json.loads(log.store.read(receipt.key)[0])
    for field, value in (("data", '{"amount":999}'), ("actor_label", "mallory")):
        forged = {**envelope, "event": {**envelope["event"], field: value}}
        _overwrite(object_client, receipt.key, forged)
        with pytest.raises(EventSignatureError, match="does not verify"):
            log.get(receipt.key)
    _overwrite(object_client, receipt.key, {**envelope, "provenance": None})
    with pytest.raises(EventSignatureError):
        log.get(receipt.key)


def test_readers_classify_valid_unverified_and_unsigned(object_client, monkeypatch):
    signed = _log(object_client, mac_key=KEY).record("signed", created_at=DAY)
    unsigned = _log(object_client).record("unsigned", created_at=DAY)
    keyless = _log(object_client)
    assert keyless.get(signed.key)["signature"]["status"] == "unverified"
    assert keyless.get(unsigned.key)["signature"] == {"status": "unsigned", "key_id": None}
    assert not keyless.verifies_signatures and keyless.key_id is None
    # A retired key still verifies what it signed, without signing anything new.
    monkeypatch.setenv("FIRM_AUDIT_RETIRED_KEYS", f"old={KEY}")
    rotated = _log(object_client, mac_key=OLD_KEY)
    assert rotated.get(signed.key)["signature"]["status"] == "valid"
    assert json.loads(rotated.prepare("new").body)["sig"]["key_id"] == rotated.key_id


def test_env_key_signs_like_audit_log(object_client, monkeypatch):
    monkeypatch.setenv("FIRM_AUDIT_KEY", KEY)
    log = _log(object_client)
    assert log.get(log.record("from-env"))["signature"]["status"] == "valid"


def test_require_signatures_rejects_both_downgrades(object_client):
    unsigned = _log(object_client).record("unsigned", created_at=DAY)
    foreign = _log(object_client, mac_key=OLD_KEY).record("foreign", created_at=DAY)
    signed = _log(object_client, mac_key=KEY).record("signed", created_at=DAY)
    strict = _log(object_client, mac_key=KEY, require_signatures=True)
    assert strict.get(signed)["signature"]["status"] == "valid"
    for receipt, state in ((unsigned, "unsigned"), (foreign, "unverified")):
        with pytest.raises(EventSignatureError, match=state):
            strict.get(receipt.key)
    with pytest.raises(EventSignatureError):
        strict.scan(start=date(2026, 1, 1), end=date(2026, 1, 2))


@pytest.mark.parametrize(
    "sig",
    [
        "not-a-dict",
        {"key_id": "x"},
        {"key_id": "x", "mac": "ab"},
        {"key_id": 1, "mac": "a" * 64},
        {"key_id": "x", "mac": "a" * 64, "extra": True},
    ],
)
def test_malformed_signatures_raise_even_without_a_key(object_client, sig):
    log = _log(object_client)
    receipt = log.record("event", created_at=DAY)
    envelope = json.loads(log.store.read(receipt.key)[0])
    _overwrite(object_client, receipt.key, {**envelope, "sig": sig})
    with pytest.raises(EventSignatureError, match="malformed"):
        log.get(receipt.key)


def test_write_refuses_a_body_whose_signature_fails(object_client):
    log = _log(object_client, mac_key=KEY)
    prepared = log.prepare("invoice.paid", data={"amount": 1})
    envelope = json.loads(prepared.body)
    envelope["event"]["data"] = '{"amount":2}'
    forged = PreparedEvent(prepared.key, canonical(envelope).decode())
    before = object_client.request_count
    with pytest.raises(EventSignatureError):
        log.write(forged)
    assert object_client.request_count == before  # rejected before any PUT


def test_scan_reports_status_and_fails_closed_on_a_tampered_object(object_client):
    log = _log(object_client, mac_key=KEY)
    receipts = [log.record("scanned", created_at=DAY) for _ in range(3)]
    page = log.scan(start=date(2026, 1, 1), end=date(2026, 1, 2))
    assert [item["signature"]["status"] for item in page.items] == ["valid"] * 3
    envelope = json.loads(log.store.read(receipts[1].key)[0])
    forged = {**envelope, "event": {**envelope["event"], "action": "forged"}}
    _overwrite(object_client, receipts[1].key, forged)
    with pytest.raises(EventSignatureError):
        log.scan(start=date(2026, 1, 1), end=date(2026, 1, 2))


def test_signatures_survive_export_and_restore(object_client):
    log = _log(object_client, mac_key=KEY)
    for _ in range(2):
        log.record("archived", created_at=DAY)
    archive = Archive(ObjectStore("archives", client=object_client))
    receipt = archive.export_s3(
        log, start=date(2026, 1, 1), end=date(2026, 1, 2), batch_id=str(uuid4())
    )
    entries = archive.read(receipt)
    assert all(entry["envelope"]["sig"]["key_id"] == log.key_id for entry in entries)
    (summary,), _ = archive.batches()
    assert (summary["signed"], summary["count"], summary["sources"]) == (2, 2, ["s3"])
    target = S3AuditLog(
        "restored", stream=STREAM, prefix="other", client=object_client, mac_key=KEY
    )
    restored = archive.restore_s3(receipt, target)
    assert [target.get(r)["signature"]["status"] for r in restored] == ["valid", "valid"]


def test_a_short_key_fails_before_any_client_exists():
    with pytest.raises(ValueError, match="at least 32"):
        S3AuditLog("events", stream=STREAM, mac_key="short", client=object())


def test_a_keyed_writer_still_accepts_an_unsigned_retry_from_before_the_key(object_client):
    # Rollout: a retry token prepared before FIRM_AUDIT_KEY existed must still be writable.
    old_token = _log(object_client).prepare("prepared-before-key", created_at=DAY)
    receipt = _log(object_client, mac_key=KEY).write(old_token)
    assert _log(object_client, mac_key=KEY).get(receipt)["signature"]["status"] == "unsigned"
