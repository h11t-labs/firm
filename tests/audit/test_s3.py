"""S3AuditLog behaves as AuditLog does: the same API and results, with S3 objects as storage."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from firm._core.clock import now_utc
from firm.audit import AuditLog, Ref
from firm.audit.event_format import canonical
from firm.audit.integrity import load_key, row_mac
from firm.audit.object_store import Receipt
from firm.audit.s3 import PreparedEvent, S3AuditLog, StorageBusy, UnknownEventOutcome

STREAM = str(UUID(int=1))
KEY = "s3-parity-test-row-and-seal-key-0123456789"


def _log(client, **kwargs) -> S3AuditLog:
    return S3AuditLog("events", stream=STREAM, client=client, **kwargs)


def _record_same(log, audit) -> None:
    events = [
        ("invoice.created", ("Invoice", 42), Ref("User", 7, "alice"), {"amount": Decimal("1.50")}),
        ("invoice.paid", ("Invoice", 42), ("User", 7), None),
        ("invoice.created", ("Invoice", 43), "cron", {"amount": 3}),
        ("user.login", None, ("User", 8), {"ip": "10.0.0.1"}),
    ]
    for index, (action, subject, actor, data) in enumerate(events):
        for target in (log, audit):
            target.record(
                action, subject=subject, actor=actor, data=data, correlation_id=f"req-{index % 2}"
            )


def _comparable(rows):
    return [{k: v for k, v in row.items() if k != "created_at"} for row in rows]


def test_history_matches_audit_log_for_every_filter(object_client, tmp_path):
    log = _log(object_client)
    with AuditLog(f"sqlite:///{tmp_path / 'audit.db'}") as audit:
        _record_same(log, audit)
        for filters in (
            {},
            {"subject": ("Invoice", 42)},
            {"subject_type": "Invoice"},
            {"subject_id": "43"},
            {"actor": ("User", 7)},
            {"actor_type": "cron"},
            {"actor_id": 8},
            {"action": "invoice.created"},
            {"correlation_id": "req-1"},
            {"action": "invoice.created", "subject_id": 42},
            {"subject": ("Invoice", 42), "actor": ("User", 7), "limit": 1},
            {"limit": 2},
            {"action": "nothing"},
        ):
            assert _comparable(log.history(**filters)) == _comparable(audit.history(**filters))
        assert _comparable([log.get(3)]) == _comparable(audit.history(limit=4)[1:2])
        assert log.get(99) is None
        with pytest.raises(ValueError):
            log.history(subject=("Invoice", 1), subject_type="Invoice")
        with pytest.raises(ValueError):
            log.history(actor="cron", actor_id=1)
    log.close()


def test_since_is_exact_even_for_late_ids(object_client):
    log = _log(object_client)
    now = now_utc()
    log.write(log.prepare("old", created_at=now - timedelta(hours=2)))
    log.record("fresh")
    # A late publication: an old created_at at the highest id (the outbox case).
    log.write(log.prepare("late", created_at=now - timedelta(hours=3)))
    assert [e["action"] for e in log.history(since=now - timedelta(hours=1))] == ["fresh"]
    assert [e["action"] for e in log.history(since=now - timedelta(hours=2, minutes=30))] == [
        "fresh",
        "old",
    ]
    aware = (now - timedelta(hours=1)).replace(tzinfo=UTC)
    assert [e["action"] for e in log.history(since=aware)] == ["fresh"]


def test_signed_rows_carry_the_sql_row_mac(object_client):
    log = _log(object_client, mac_key=KEY)
    log.record("signed", subject=("Order", 1), data={"x": 1})
    (key,) = [k for (_, k), v in object_client.objects.items() if "/ev/" in k]
    row = json.loads(object_client.objects[("events", key)][-1])
    expected = row_mac(
        load_key(KEY),
        entry_id=row["entry_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        **{
            name: row[name]
            for name in (
                "action",
                "subject_type",
                "subject_id",
                "subject_label",
                "actor_type",
                "actor_id",
                "actor_label",
                "correlation_id",
                "data",
                "changes",
                "context",
            )
        },
    )
    assert row["row_mac"] == expected and row["key_id"] == load_key(KEY).id


def test_retries_converge_on_one_event_across_processes(object_client):
    first, second = _log(object_client), _log(object_client)
    prepared = first.prepare("invoice.paid", subject=("Invoice", 42))
    token = json.dumps({"body": prepared.body})  # what an application persists
    again = PreparedEvent(**json.loads(token))
    receipts = {first.write(prepared), second.write(again), first.write(prepared)}
    assert len(receipts) == 1
    assert [e["action"] for e in second.history()] == ["invoice.paid"]


def test_concurrent_writers_get_distinct_contiguous_ids(object_client):
    log = _log(object_client)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: log.record("parallel", data={"i": i}), range(40)))
    ids = sorted(e["id"] for e in log.history(limit=100))
    assert ids == list(range(1, 41))


def test_concurrent_retries_of_one_event_write_it_once(object_client):
    log = _log(object_client)
    prepared = log.prepare("once")
    with ThreadPoolExecutor(max_workers=6) as pool:
        receipts = set(pool.map(lambda _: log.write(prepared), range(6)))
    assert len(receipts) == 1 and len(log.history()) == 1


def test_a_stale_claim_without_event_takes_a_fresh_id(object_client):
    log = _log(object_client, grace=0.0)
    prepared = log.prepare("crashed")
    # A writer claimed id 1 for this event and died before writing it.
    entry = prepared.entry_id
    old = (now_utc() - timedelta(minutes=5)).isoformat()
    claim = log.storage.layout.entry(entry)
    object_client.put_object(Bucket="events", Key=claim, Body=canonical({"id": 1, "at": old}))
    object_client.put_object(Bucket="events", Key=log.storage.layout.seq, Body=b'{"next":2}')
    log.write(prepared)
    assert [e["id"] for e in log.history()] == [2]  # id 1 stays a gap, like a rolled-back insert


def test_timeout_after_success_reconciles(object_client):
    log = _log(object_client)
    prepared = log.prepare("timeout")
    object_client.fail_after_put = True
    with pytest.raises(UnknownEventOutcome) as caught:
        log.write(prepared)
    assert caught.value.prepared == prepared
    log.write(caught.value.prepared)
    assert len(log.history()) == 1


def test_prepared_bytes_must_stay_canonical(object_client):
    log = _log(object_client)
    prepared = log.prepare("event")
    body = json.loads(prepared.body)
    with pytest.raises(ValueError, match="canonical"):
        log.write(PreparedEvent(json.dumps(body, indent=2)))
    with pytest.raises(ValueError):
        log.write(PreparedEvent(canonical({**body, "id": 7}).decode()))


def test_get_by_receipt_verifies_the_digest(object_client):
    log = _log(object_client)
    receipt = log.record("pinned", data={"amount": Decimal("2.50")})
    assert log.get(receipt)["data"] == {"amount": Decimal("2.50")}
    with pytest.raises(ValueError):
        log.get(Receipt(receipt.key, "0" * 64))


def test_record_with_conn_stages_in_the_caller_transaction(object_client, tmp_path):
    from sqlalchemy import create_engine, text

    from firm.audit.s3_outbox import create_outbox, pending

    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    create_outbox(engine)
    log = _log(object_client)
    with engine.begin() as conn:
        conn.execute(text("create table orders (id integer primary key)"))
    with engine.begin() as conn:
        conn.execute(text("insert into orders (id) values (1)"))
        assert log.record("order.created", subject=("Order", 1), conn=conn) is None
    with pytest.raises(RuntimeError), engine.begin() as conn:
        log.record("order.rolled_back", conn=conn)
        raise RuntimeError("the caller's work failed")
    assert pending(engine, STREAM) == 1 and log.history() == []
    assert log.flush_outbox(engine) == 1
    assert [e["action"] for e in log.history()] == ["order.created"]
    assert pending(engine, STREAM) == 0 and log.flush_outbox(engine) == 0


def test_an_interrupted_floor_advance_is_completed_by_the_next_lease_holder(object_client):
    log = _log(object_client, mac_key=KEY, grace=0.0, seal_batch_size=2)
    for _ in range(4):
        log.write(log.prepare("old", created_at=now_utc() - timedelta(hours=2)))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("firm.audit.sealing.now_utc", lambda: now_utc() - timedelta(hours=1))
        assert log.sealer.run_once() == 4
    log.max_age = 1800
    view_cls = type(log.storage.connect().__enter__())
    original = view_cls.complete_pending_prune

    def crash_after_intent(self):
        if self.store.exists(self.layout.prune):
            raise RuntimeError("process died right after recording the intent")
        return original(self)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(view_cls, "complete_pending_prune", crash_after_intent)
        with pytest.raises(RuntimeError):
            log.retention.run_once()
    assert log.storage.store.exists(log.storage.layout.prune)  # the intent survived the crash
    # The next maintenance scope finishes it before doing anything else.
    report = log.verify(full=True)
    assert not log.storage.store.exists(log.storage.layout.prune)
    assert report.outcome in {"ok", "warning"} and report.tampered_count == 0
    assert log.history() == []


def test_maintenance_waits_for_the_lease(object_client):
    holder = _log(object_client, mac_key=KEY)
    contender = _log(object_client, mac_key=KEY, lease_wait=0.2)
    with holder.storage.claim(), pytest.raises(StorageBusy):
        contender.storage.claim().__enter__()
    with contender.storage.claim():
        pass  # released: free again


def test_a_short_key_fails_before_any_client_exists():
    with pytest.raises(ValueError, match="at least 32"):
        S3AuditLog("events", stream=STREAM, mac_key="short", client=object())
