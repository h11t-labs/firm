"""Specs for the read-query surface (:mod:`firm.audit.queries`): stats, paginated search, detail.

The filter contract mirrors ``history()`` (see ``test_history.py``): a paired ``subject=``/
``actor=`` reference or the split ``*_type=``/``*_id=`` halves, never both.
"""

from __future__ import annotations

import pytest

from firm.audit import AuditLog, Ref, queries


def test_audit_stats_and_search(audit: AuditLog) -> None:
    audit.record("a", correlation_id="r1")
    audit.record("b", correlation_id="r2")
    with audit.engine.connect() as conn:
        stats = queries.audit_stats(conn)
        rows = queries.audit_search(conn, correlation_id="r1")
    assert stats["events"] == 2
    assert stats["actions"] == 2  # "a" and "b" are distinct
    assert stats["last_event_at"] is not None
    assert [r["action"] for r in rows] == ["a"]


def test_audit_stats_on_empty_table(audit: AuditLog) -> None:
    with audit.engine.connect() as conn:
        stats = queries.audit_stats(conn)
    assert stats == {"events": 0, "actions": 0, "last_event_at": None}


def test_audit_search_rows_carry_row_mac(audit: AuditLog) -> None:
    # The read layer adds row_mac to the history() shape, so row_status can classify a row.
    audit.record("a")
    with audit.engine.connect() as conn:
        (row,) = queries.audit_search(conn)
    assert row["row_mac"] is None  # unsigned log: no key configured
    assert row["changes"] is None and row["context"] is None


def test_audit_search_sorts_by_each_column(audit: AuditLog) -> None:
    audit.record("b.action", subject=("Z", "1"), correlation_id="c2")
    audit.record("a.action", subject=("A", "1"), correlation_id="c1")
    with audit.engine.connect() as conn:
        by_action_asc = queries.audit_search(conn, sort="action", dir="asc")
        by_action_desc = queries.audit_search(conn, sort="action", dir="desc")
        by_subject_asc = queries.audit_search(conn, sort="subject", dir="asc")
    assert [r["action"] for r in by_action_asc] == ["a.action", "b.action"]
    assert [r["action"] for r in by_action_desc] == ["b.action", "a.action"]
    assert [r["subject_type"] for r in by_subject_asc] == ["A", "Z"]


def test_audit_search_falls_back_to_default_sort_for_unknown_key(audit: AuditLog) -> None:
    audit.record("first")
    audit.record("second")
    with audit.engine.connect() as conn:
        rows = queries.audit_search(conn, sort="not-a-real-column")
        unknown_dir = queries.audit_search(conn, dir="sideways")
    assert [r["action"] for r in rows] == ["second", "first"]  # falls back to created_at desc
    assert [r["action"] for r in unknown_dir] == ["second", "first"]  # and to descending


def test_audit_search_paginates(audit: AuditLog) -> None:
    for i in range(30):
        audit.record(f"event.{i}")
    with audit.engine.connect() as conn:
        ids = [r["id"] for r in queries.audit_search(conn, sort="id", dir="asc", limit=30)]
        page1 = queries.audit_search(conn, sort="id", dir="asc", limit=10, offset=0)
        page2 = queries.audit_search(conn, sort="id", dir="asc", limit=10, offset=10)
        page3 = queries.audit_search(conn, sort="id", dir="asc", limit=10, offset=20)
    assert [r["id"] for r in page1] == ids[0:10]
    assert [r["id"] for r in page2] == ids[10:20]
    assert [r["id"] for r in page3] == ids[20:30]


def test_audit_search_rejects_a_negative_window(audit: AuditLog) -> None:
    with audit.engine.connect() as conn:
        with pytest.raises(ValueError, match="limit"):
            queries.audit_search(conn, limit=-1)
        with pytest.raises(ValueError, match="offset"):
            queries.audit_search(conn, offset=-1)


def test_audit_count_matches_filtered_search(audit: AuditLog) -> None:
    audit.record("invoice.paid", correlation_id="r1")
    audit.record("invoice.paid", correlation_id="r2")
    audit.record("invoice.voided", correlation_id="r3")
    with audit.engine.connect() as conn:
        assert queries.audit_count(conn) == 3
        assert queries.audit_count(conn, action="invoice.paid") == 2
        assert queries.audit_count(conn, action="invoice.voided") == 1
        assert queries.audit_count(conn, correlation_id="r1") == 1
        assert queries.audit_count(conn, action="nonexistent") == 0


# -- the two reference filter forms ------------------------------------------------------------


def test_filter_by_subject_halves(audit: AuditLog) -> None:
    audit.record("a", subject=("Invoice", "1"))
    audit.record("b", subject=("Invoice", "2"))
    audit.record("c", subject=("Rule", "1"))
    with audit.engine.connect() as conn:
        by_type = queries.audit_search(conn, subject_type="Invoice")
        by_id = queries.audit_search(conn, subject_id=1)  # coerced to str, like history()
        by_pair = queries.audit_search(conn, subject_type="Invoice", subject_id="1")
        assert queries.audit_count(conn, subject_type="Invoice") == 2
    assert {r["action"] for r in by_type} == {"a", "b"}
    assert {r["action"] for r in by_id} == {"a", "c"}
    assert [r["action"] for r in by_pair] == ["a"]


def test_filter_by_actor_halves(audit: AuditLog) -> None:
    audit.record("a", actor=("User", "1"))
    audit.record("b", actor=("Model", "1"))
    with audit.engine.connect() as conn:
        rows = queries.audit_search(conn, actor_type="User")
        assert queries.audit_count(conn, actor_type="User", actor_id="1") == 1
    assert [r["action"] for r in rows] == ["a"]


def test_filter_by_a_reference_matches_the_split_form(audit: AuditLog) -> None:
    audit.record("a", subject=Ref("Invoice", 1, name="ACME #1"))
    audit.record("b", subject=("Invoice", 2))
    with audit.engine.connect() as conn:
        paired = queries.audit_search(conn, subject=Ref("Invoice", 1))
        tuple_form = queries.audit_search(conn, subject=("Invoice", 1))
        split = queries.audit_search(conn, subject_type="Invoice", subject_id=1)
    assert paired == tuple_form == split
    assert [r["action"] for r in paired] == ["a"]


def test_bare_string_reference_filters_by_type_only(audit: AuditLog) -> None:
    # Per the _ref convention a bare string is a role/kind, stored as the *type* — so
    # subject="Invoice:42" filters on the type "Invoice:42", never on type + id.
    audit.record("a", actor="cron")
    audit.record("b", actor=("User", "1"))
    audit.record("c", subject="Invoice:42")
    with audit.engine.connect() as conn:
        by_role = queries.audit_search(conn, actor="cron")
        colon_string = queries.audit_search(conn, subject="Invoice:42")
        as_pair = queries.audit_search(conn, subject=("Invoice", 42))
    assert [r["action"] for r in by_role] == ["a"]
    assert [r["action"] for r in colon_string] == ["c"]  # matched the literal type
    assert as_pair == []


def test_mixing_both_reference_forms_raises(audit: AuditLog) -> None:
    with audit.engine.connect() as conn:
        with pytest.raises(ValueError, match="subject"):
            queries.audit_search(conn, subject=("Invoice", "1"), subject_type="Invoice")
        with pytest.raises(ValueError, match="subject"):
            queries.audit_count(conn, subject=("Invoice", "1"), subject_id="1")
        with pytest.raises(ValueError, match="actor"):
            queries.audit_search(conn, actor=("User", "1"), actor_type="User")
        with pytest.raises(ValueError, match="actor"):
            queries.audit_count(conn, actor=("User", "1"), actor_id="1")


# -- detail ------------------------------------------------------------------------------------


def test_audit_detail_returns_the_event_with_its_row_mac(audit: AuditLog) -> None:
    audit.record("invoice.paid", subject=("Invoice", "1"), data={"total": 10})
    with audit.engine.connect() as conn:
        (row,) = queries.audit_search(conn)
        detail = queries.audit_detail(conn, row["id"])
    assert detail is not None
    assert detail["action"] == "invoice.paid"
    assert detail["data"] == {"total": 10}
    assert detail["row_mac"] is None  # present as a key even on an unsigned row


def test_audit_detail_missing_returns_none(audit: AuditLog) -> None:
    with audit.engine.connect() as conn:
        assert queries.audit_detail(conn, 999) is None
