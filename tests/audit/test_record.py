"""Recording: ``append``/``record``/``AuditLog.record``, and subject/actor coercion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from firm._core.database import transaction
from firm.audit import AuditLog, Ref, record


@dataclass
class Invoice:
    id: int


def test_record_via_audit_log(audit: AuditLog) -> None:
    audit.record("invoice.paid", subject=Invoice(42), data={"amount": 100})
    rows = audit.history()
    assert len(rows) == 1
    assert rows[0]["action"] == "invoice.paid"
    assert rows[0]["subject_type"] == "Invoice"
    assert rows[0]["subject_id"] == "42"
    assert rows[0]["data"] == {"amount": 100}


def test_record_module_level_inside_shared_transaction(db_url: str) -> None:
    audit = AuditLog(database_url=db_url)
    try:
        with transaction(audit.engine) as conn:
            record(conn, "user.login", actor=("User", 7))
        rows = audit.history()
        assert len(rows) == 1
        assert rows[0]["actor_type"] == "User"
        assert rows[0]["actor_id"] == "7"
    finally:
        audit.close()


def test_subject_from_explicit_tuple(audit: AuditLog) -> None:
    audit.record("x", subject=("Widget", 9))
    row = audit.history()[0]
    assert row["subject_type"] == "Widget"
    assert row["subject_id"] == "9"


def test_subject_object_without_id_raises(audit: AuditLog) -> None:
    class NoId:
        pass

    with pytest.raises(TypeError):
        audit.record("x", subject=NoId())


def test_none_subject_and_actor_are_null(audit: AuditLog) -> None:
    audit.record("system.boot")
    row = audit.history()[0]
    assert row["subject_type"] is None
    assert row["subject_id"] is None
    assert row["actor_type"] is None
    assert row["actor_id"] is None


def test_data_changes_context_roundtrip_json(audit: AuditLog) -> None:
    audit.record(
        "invoice.paid",
        data={"amount": 100, "nested": {"a": [1, 2, 3]}, "name": "café"},
        changes={"status": ["pending", "paid"]},
        context={"ip": "127.0.0.1", "request_id": "abc"},
    )
    row = audit.history()[0]
    assert row["data"] == {"amount": 100, "nested": {"a": [1, 2, 3]}, "name": "café"}
    assert row["changes"] == {"status": ["pending", "paid"]}
    assert row["context"] == {"ip": "127.0.0.1", "request_id": "abc"}


def test_data_roundtrips_datetime_decimal_uuid(audit: AuditLog) -> None:
    dt = datetime(2026, 6, 30, 12, 0, 0)
    dec = Decimal("9.99")
    uid = uuid4()
    audit.record("x", data={"at": dt, "price": dec, "id": uid})
    row = audit.history()[0]
    assert row["data"] == {"at": dt, "price": dec, "id": uid}


def test_none_payloads_store_as_null(audit: AuditLog) -> None:
    audit.record("x")
    row = audit.history()[0]
    assert row["data"] is None
    assert row["changes"] is None
    assert row["context"] is None


def test_correlation_id_is_recorded(audit: AuditLog) -> None:
    audit.record("x", correlation_id="req-123")
    assert audit.history()[0]["correlation_id"] == "req-123"


# -- flexible references: optional type/id, labels, protocol ------------------------------------


def test_label_actor_stores_type_only(audit: AuditLog) -> None:
    audit.record("sync.ran", actor="cron")
    row = audit.history()[0]
    assert row["actor_type"] == "cron"
    assert row["actor_id"] is None
    assert row["actor_label"] is None


def test_label_subject_is_symmetric(audit: AuditLog) -> None:
    audit.record("flag.flipped", subject="checkout-flag")
    row = audit.history()[0]
    assert row["subject_type"] == "checkout-flag"
    assert row["subject_id"] is None


def test_tuple_none_id_stores_null_not_the_string_none(audit: AuditLog) -> None:
    audit.record("x", subject=("Invoice", None))
    row = audit.history()[0]
    assert row["subject_type"] == "Invoice"
    assert row["subject_id"] is None


def test_tuple_empty_id_collapses_to_null(audit: AuditLog) -> None:
    audit.record("x", subject=("Invoice", ""))
    assert audit.history()[0]["subject_id"] is None


def test_id_only_reference(audit: AuditLog) -> None:
    audit.record("x", actor=(None, "abc"))
    row = audit.history()[0]
    assert row["actor_type"] is None
    assert row["actor_id"] == "abc"


def test_ref_with_name_populates_label(audit: AuditLog) -> None:
    audit.record("invoice.paid", actor=Ref("User", 7, name="alice@example.com"))
    row = audit.history()[0]
    assert row["actor_type"] == "User"
    assert row["actor_id"] == "7"
    assert row["actor_label"] == "alice@example.com"


def test_ref_label_only(audit: AuditLog) -> None:
    audit.record("sync.ran", actor=Ref(type="cron"))
    row = audit.history()[0]
    assert row["actor_type"] == "cron"
    assert row["actor_id"] is None


def test_zero_id_is_preserved(audit: AuditLog) -> None:
    audit.record("x", subject=("Widget", 0))
    assert audit.history()[0]["subject_id"] == "0"


def test_custom_audit_ref_protocol(audit: AuditLog) -> None:
    class Account:
        def __firm_audit_ref__(self) -> Ref:
            return Ref("Account", "acct_1", name="Acme, Inc.")

    audit.record("account.created", subject=Account())
    row = audit.history()[0]
    assert row["subject_type"] == "Account"
    assert row["subject_id"] == "acct_1"
    assert row["subject_label"] == "Acme, Inc."


def test_invalid_reference_type_raises(audit: AuditLog) -> None:
    with pytest.raises(TypeError):
        audit.record("x", actor=123)


def test_three_element_tuple_raises(audit: AuditLog) -> None:
    with pytest.raises(TypeError):
        audit.record("x", subject=("Invoice", 1, "extra"))


def test_label_is_null_by_default(audit: AuditLog) -> None:
    audit.record("x", subject=("Invoice", 1))
    assert audit.history()[0]["subject_label"] is None
