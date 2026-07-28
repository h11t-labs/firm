"""Specs for the per-row tamper-evidence marks in the audit events table + detail page.

The status itself is derived by :func:`firm.audit.queries.row_status` (specced in
``tests/audit/test_integrity_status.py``); this file covers how :mod:`firm.ui.render` shows it —
the table column, the detail cell, and their conditional visibility.
"""

from __future__ import annotations

from datetime import datetime

from firm.ui import render

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
