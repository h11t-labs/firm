"""Specs for the per-row tamper-evidence status shown in the audit events table + detail page.

Mirrors the split in :mod:`firm.ui.audit_queries` / :mod:`firm.ui.render`:

* :func:`row_status` is pure over one row + a context dict, so its four states plus the
  None-when-inactive case are asserted directly without a database;
* :func:`row_integrity_context` is checked against a real (seeded) audit schema;
* the table column / detail cell (and their conditional visibility) go through :mod:`firm.ui`.
"""

from __future__ import annotations

from datetime import datetime

from firm.ui import audit_queries, render
from firm.ui.audit_queries import row_status

NOW = datetime(2026, 7, 20, 12, 0, 0)

_EMPTY_STATS = {"events": 0, "actions": 0, "last_event_at": None}
_EMPTY_FILTERS = {"action": "", "subject": "", "actor": "", "correlation_id": ""}
_MAC = "ab" * 32


def _ctx(
    *,
    active: bool = True,
    max_sealed: int = 0,
    tampered: set[int] | None = None,
    truncated: bool = False,
) -> dict:
    return {
        "active": active,
        "max_sealed_to_id": max_sealed,
        "tampered_ids": tampered or set(),
        "tampered_truncated": truncated,
    }


# -- row_status: the four states + None-when-inactive -------------------------------------------


def test_row_status_none_when_inactive() -> None:
    # A plain audit log (no key, no seals, no verify) reports no status at all.
    assert row_status({"id": 1, "row_mac": _MAC}, _ctx(active=False)) is None


def test_row_status_sealed_within_seal_range() -> None:
    assert row_status({"id": 3, "row_mac": _MAC}, _ctx(max_sealed=5)) == "sealed"


def test_row_status_unsealed_past_the_newest_seal() -> None:
    # Signed but beyond the newest sealed id — the grace-window tail.
    assert row_status({"id": 9, "row_mac": _MAC}, _ctx(max_sealed=5)) == "unsealed"


def test_row_status_unprotected_when_no_row_mac() -> None:
    assert row_status({"id": 1, "row_mac": None}, _ctx(max_sealed=5)) == "unprotected"


def test_row_status_tampered_dominates_a_sealed_row() -> None:
    assert row_status({"id": 3, "row_mac": _MAC}, _ctx(max_sealed=5, tampered={3})) == "tampered"


def test_row_status_degrades_to_unverified_when_findings_truncated() -> None:
    # Bug #8. A tamper run flagged more rows than its affected list carries ids for (truncated). A
    # sealed row NOT in the known tampered set could still be one of the un-listed tampered rows, so
    # it must NOT read "Sealed & verified" — it degrades to the honest "unverified".
    ctx = _ctx(max_sealed=5, tampered={2}, truncated=True)
    assert row_status({"id": 4, "row_mac": _MAC}, ctx) == "unverified"  # sealed, but unvouched
    # A row that IS in the known tampered set still reads tampered (priority unchanged).
    assert row_status({"id": 2, "row_mac": _MAC}, ctx) == "tampered"
    # Without truncation the same sealed row reads sealed & verified.
    assert row_status({"id": 4, "row_mac": _MAC}, _ctx(max_sealed=5, tampered={2})) == "sealed"


def test_row_status_tampered_dominates_an_unprotected_row() -> None:
    # Priority is tampered first, before the row_mac-null check.
    assert row_status({"id": 3, "row_mac": None}, _ctx(max_sealed=5, tampered={3})) == "tampered"


# -- row_integrity_context: gathered once per page ---------------------------------------------


def test_context_inactive_without_seals_or_verify(runtime) -> None:
    with runtime.engine.connect() as conn:
        ctx = audit_queries.row_integrity_context(conn)
    assert ctx["active"] is False
    assert ctx["max_sealed_to_id"] == 0
    assert ctx["tampered_ids"] == set()


def test_context_active_from_a_seal(runtime, seed) -> None:
    seed.seal(seq=1, to_id=7)
    with runtime.engine.connect() as conn:
        ctx = audit_queries.row_integrity_context(conn)
    assert ctx["active"] is True
    assert ctx["max_sealed_to_id"] == 7


def test_context_active_from_a_verify_row_alone(runtime, seed) -> None:
    seed.verify_status(outcome="ok")
    with runtime.engine.connect() as conn:
        ctx = audit_queries.row_integrity_context(conn)
    assert ctx["active"] is True


def test_context_collects_tampered_row_ids(runtime, seed) -> None:
    affected = (
        '[{"kind": "row", "label": "row 42", "id": 42, "verdict": "tampered"},'
        '{"kind": "seal", "label": "seal 3", "verdict": "tampered"},'
        '{"kind": "row", "label": "row 7", "id": 7, "verdict": "ok"}]'
    )
    seed.verify_status(outcome="tampered", tampered_count=1, affected_identifiers=affected)
    with runtime.engine.connect() as conn:
        ctx = audit_queries.row_integrity_context(conn)
    assert ctx["tampered_ids"] == {42}  # only the tampered finding with an integer id


def test_context_tampered_ids_survives_malformed_json(runtime, seed) -> None:
    seed.verify_status(outcome="tampered", tampered_count=1, affected_identifiers="{not json")
    with runtime.engine.connect() as conn:
        ctx = audit_queries.row_integrity_context(conn)
    assert ctx["tampered_ids"] == set()


def test_context_flags_truncated_findings(runtime, seed) -> None:
    # Bug #8. A tampered run whose affected list carries the "more" overflow marker sets
    # tampered_truncated, so the table stops vouching for un-listed sealed rows.
    affected = (
        '[{"kind": "row", "label": "row 1", "id": 1, "verdict": "tampered"},'
        '{"kind": "more", "label": "+40 more finding(s)", "verdict": "tampered"}]'
    )
    seed.verify_status(outcome="tampered", tampered_count=41, affected_identifiers=affected)
    with runtime.engine.connect() as conn:
        ctx = audit_queries.row_integrity_context(conn)
    assert ctx["tampered_truncated"] is True
    assert ctx["tampered_ids"] == {1}


def test_context_not_truncated_without_the_more_marker(runtime, seed) -> None:
    affected = '[{"kind": "row", "label": "row 1", "id": 1, "verdict": "tampered"}]'
    seed.verify_status(outcome="tampered", tampered_count=1, affected_identifiers=affected)
    with runtime.engine.connect() as conn:
        ctx = audit_queries.row_integrity_context(conn)
    assert ctx["tampered_truncated"] is False


def test_tampered_row_ids_survives_deeply_nested_json() -> None:
    # Bug #3. A DB-write attacker could set affected_identifiers to a deeply-nested JSON blob;
    # json.loads raises RecursionError (not ValueError/TypeError), which used to 500 the audit page
    # on every render (parsed twice per request). It must degrade to an empty set instead.
    deep = "[" * 5000 + "]" * 5000
    assert audit_queries._tampered_row_ids(deep) == set()


def test_tampered_row_ids_rejects_oversized_json() -> None:
    # An oversized blob is rejected before json.loads — the verifier only ever writes a handful of
    # small findings, so anything past the cap is corrupt or hostile.
    huge = '[{"kind":"row","id":1,"verdict":"tampered"}]' + " " * (audit_queries._MAX_AFFECTED_JSON)
    assert audit_queries._tampered_row_ids(huge) == set()


# -- the events table column -------------------------------------------------------------------


def _row(id_: int, *, row_mac: str | None = _MAC) -> dict:
    return {
        "id": id_,
        "action": "user.login",
        "subject_type": "User",
        "subject_id": "1",
        "subject_label": None,
        "actor_type": None,
        "actor_id": None,
        "actor_label": None,
        "correlation_id": None,
        "data": None,
        "created_at": NOW,
        "row_mac": row_mac,
    }


def test_table_shows_shield_x_on_tampered_and_check_on_sealed_when_active(runtime) -> None:
    rows = [_row(3), _row(1)]
    ctx = _ctx(max_sealed=5, tampered={3})
    body = render.audit_page(["audit"], _EMPTY_STATS, rows, _EMPTY_FILTERS, row_ctx=ctx)
    assert 'class="row-status-th"' in body  # the column exists
    assert render._ICONS["shield-x"] in body  # tampered row 3
    assert 'class="row-status danger"' in body
    assert "Tampered — failed verification" in body
    assert render._ICONS["shield-check"] in body  # sealed row 1
    assert 'class="row-status ok"' in body


def test_table_shows_unsealed_and_unprotected_marks(runtime) -> None:
    rows = [_row(9), _row(2, row_mac=None)]
    ctx = _ctx(max_sealed=5)
    body = render.audit_page(["audit"], _EMPTY_STATS, rows, _EMPTY_FILTERS, row_ctx=ctx)
    assert 'class="row-status warn"' in body  # id 9 signed, past the seal tail
    assert render._ICONS["shield-alert"] in body
    assert 'class="row-status muted"' in body  # id 2 has no row_mac
    assert "Unprotected — recorded before tamper-evidence" in body


def test_table_shows_unverified_mark_on_a_truncated_tamper_run(runtime) -> None:
    # Bug #8. On a truncated tamper run a sealed row not in the known set renders the honest
    # "unverified" mark (warn), never the green "Sealed & verified" checkmark it used to.
    rows = [_row(4)]
    ctx = _ctx(max_sealed=5, tampered={2}, truncated=True)
    body = render.audit_page(["audit"], _EMPTY_STATS, rows, _EMPTY_FILTERS, row_ctx=ctx)
    assert "verify it (findings truncated)" in body  # apostrophe is HTML-escaped in the title attr
    assert 'class="row-status warn"' in body
    assert "Sealed & verified" not in body  # never falsely green


def test_table_has_no_status_column_when_inactive(runtime) -> None:
    body = render.audit_page(
        ["audit"], _EMPTY_STATS, [_row(1)], _EMPTY_FILTERS, row_ctx=_ctx(active=False)
    )
    assert "row-status" not in body


def test_table_has_no_status_column_without_context(runtime) -> None:
    # A plain audit log (row_ctx omitted entirely) looks exactly as it did before this feature.
    body = render.audit_page(["audit"], _EMPTY_STATS, [_row(1)], _EMPTY_FILTERS)
    assert "row-status" not in body


# -- the detail page cell ----------------------------------------------------------------------


def _event(id_: int, *, row_mac: str | None = _MAC) -> dict:
    return {
        "id": id_,
        "action": "user.login",
        "subject_type": "User",
        "subject_id": "1",
        "subject_label": None,
        "actor_type": None,
        "actor_id": None,
        "actor_label": None,
        "correlation_id": None,
        "created_at": NOW,
        "row_mac": row_mac,
        "data": None,
        "changes": None,
        "context": None,
    }


def test_detail_shows_integrity_cell_when_active(runtime) -> None:
    body = render.audit_detail_page(["audit"], _event(1), row_ctx=_ctx(max_sealed=5))
    assert ">integrity</div>" in body  # the Kv label
    assert 'class="row-status ok"' in body
    assert "Sealed &amp; verified" in body  # the word beside the shield


def test_detail_shows_tampered_cell(runtime) -> None:
    body = render.audit_detail_page(["audit"], _event(3), row_ctx=_ctx(tampered={3}))
    assert 'class="row-status danger"' in body
    assert "Tampered" in body


def test_detail_has_no_integrity_cell_when_inactive(runtime) -> None:
    body = render.audit_detail_page(["audit"], _event(1), row_ctx=_ctx(active=False))
    assert "row-status" not in body
    assert ">integrity</div>" not in body


def test_detail_has_no_integrity_cell_without_context(runtime) -> None:
    body = render.audit_detail_page(["audit"], _event(1))
    assert "row-status" not in body
