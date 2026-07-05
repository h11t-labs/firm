"""``history()`` filtering and ``get()`` lookup by id."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import update

from firm._core.clock import now_utc
from firm._core.database import transaction
from firm.audit import AuditLog, events, schema


def test_filter_by_subject(audit: AuditLog) -> None:
    audit.record("a", subject=("Invoice", "1"))
    audit.record("b", subject=("Invoice", "2"))
    rows = audit.history(subject=("Invoice", "1"))
    assert [r["action"] for r in rows] == ["a"]


def test_filter_by_actor(audit: AuditLog) -> None:
    audit.record("a", actor=("User", "1"))
    audit.record("b", actor=("User", "2"))
    rows = audit.history(actor=("User", "2"))
    assert [r["action"] for r in rows] == ["b"]


def test_filter_by_action(audit: AuditLog) -> None:
    audit.record("invoice.paid")
    audit.record("invoice.voided")
    rows = audit.history(action="invoice.paid")
    assert [r["action"] for r in rows] == ["invoice.paid"]


def test_filter_by_correlation_id(audit: AuditLog) -> None:
    audit.record("a", correlation_id="req-1")
    audit.record("b", correlation_id="req-2")
    rows = audit.history(correlation_id="req-1")
    assert [r["action"] for r in rows] == ["a"]


def test_filter_by_since(audit: AuditLog) -> None:
    audit.record("old")
    with transaction(audit.engine) as conn:
        conn.execute(
            update(schema.audits)
            .where(schema.audits.c.action == "old")
            .values(created_at=now_utc() - timedelta(days=2))
        )
    audit.record("new")

    cutoff = now_utc() - timedelta(hours=1)
    rows = audit.history(since=cutoff)
    assert [r["action"] for r in rows] == ["new"]


def test_combined_filters_and_together(audit: AuditLog) -> None:
    audit.record("a", subject=("Invoice", "1"), actor=("User", "1"))
    audit.record("a", subject=("Invoice", "1"), actor=("User", "2"))
    rows = audit.history(action="a", subject=("Invoice", "1"), actor=("User", "1"))
    assert len(rows) == 1
    assert rows[0]["actor_id"] == "1"


def test_default_limit_and_order_newest_first(audit: AuditLog) -> None:
    for i in range(5):
        audit.record(f"e{i}")
    rows = audit.history(limit=3)
    assert [r["action"] for r in rows] == ["e4", "e3", "e2"]


def test_no_match_returns_empty(audit: AuditLog) -> None:
    audit.record("a")
    assert audit.history(action="nonexistent") == []


def test_get_returns_the_row_by_id(audit: AuditLog) -> None:
    audit.record("invoice.paid", subject=("Invoice", "1"), data={"amount": 100})
    row_id = audit.history()[0]["id"]
    with transaction(audit.engine) as conn:
        event = events.get(conn, row_id)
    assert event is not None
    assert event["action"] == "invoice.paid"
    assert event["subject_type"] == "Invoice"
    assert event["data"] == {"amount": 100}


def test_get_returns_none_when_missing(audit: AuditLog) -> None:
    with transaction(audit.engine) as conn:
        assert events.get(conn, 999999) is None


def test_filter_by_subject_type_alone(audit: AuditLog) -> None:
    audit.record("a", subject=("Invoice", "1"))
    audit.record("b", subject=("Rule", "1"))
    rows = audit.history(subject_type="Invoice")
    assert [r["action"] for r in rows] == ["a"]


def test_filter_by_subject_id_alone(audit: AuditLog) -> None:
    audit.record("a", subject=("Invoice", "1"))
    audit.record("b", subject=("Rule", "1"))
    rows = audit.history(subject_id="1")
    assert {r["action"] for r in rows} == {"a", "b"}


def test_filter_by_actor_type_alone(audit: AuditLog) -> None:
    audit.record("a", actor=("Model", "9"))
    audit.record("b", actor=("User", "9"))
    rows = audit.history(actor_type="Model")
    assert [r["action"] for r in rows] == ["a"]


def test_filter_by_actor_id_alone(audit: AuditLog) -> None:
    audit.record("a", actor=("Model", "9"))
    audit.record("b", actor=("User", "9"))
    rows = audit.history(actor_id="9")
    assert {r["action"] for r in rows} == {"a", "b"}


def test_split_subject_matches_paired_form(audit: AuditLog) -> None:
    audit.record("a", subject=("Invoice", "1"))
    audit.record("b", subject=("Invoice", "2"))
    split = audit.history(subject_type="Invoice", subject_id=1)
    paired = audit.history(subject=("Invoice", "1"))
    assert split == paired
    assert [r["action"] for r in split] == ["a"]


def test_split_actor_matches_paired_form(audit: AuditLog) -> None:
    audit.record("a", actor=("Model", "9"))
    audit.record("b", actor=("Model", "10"))
    split = audit.history(actor_type="Model", actor_id=9)
    paired = audit.history(actor=("Model", "9"))
    assert split == paired
    assert [r["action"] for r in split] == ["a"]


def test_mixing_subject_and_split_params_raises(audit: AuditLog) -> None:
    with pytest.raises(ValueError, match="subject"):
        audit.history(subject=("Invoice", "1"), subject_type="Invoice")
    with pytest.raises(ValueError, match="subject"):
        audit.history(subject=("Invoice", "1"), subject_id="1")


def test_mixing_actor_and_split_params_raises(audit: AuditLog) -> None:
    with pytest.raises(ValueError, match="actor"):
        audit.history(actor=("User", "1"), actor_type="User")
    with pytest.raises(ValueError, match="actor"):
        audit.history(actor=("User", "1"), actor_id="1")


def test_bare_string_actor_filters_by_type_only(audit: AuditLog) -> None:
    audit.record("a", actor="cron")
    audit.record("b", actor=("User", "1"))
    rows = audit.history(actor="cron")
    assert [r["action"] for r in rows] == ["a"]
