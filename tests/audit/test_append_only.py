"""Append-only enforcement: the public surface exposes no update/delete mutator."""

from __future__ import annotations

from sqlalchemy import event

import firm.audit as audit_pkg
from firm.audit import AuditLog, events


def test_public_api_has_no_update_or_delete() -> None:
    assert audit_pkg.__all__ == ["AuditLog", "IntegrityAlert", "Ref", "__version__", "record"]
    assert not hasattr(audit_pkg, "update")
    assert not hasattr(audit_pkg, "delete")
    assert not hasattr(AuditLog, "update")
    assert not hasattr(AuditLog, "delete")


def test_events_module_exposes_no_update_or_delete_helper() -> None:
    assert not hasattr(events, "update")
    assert not hasattr(events, "delete")


def test_record_issues_no_update_or_delete(audit: AuditLog) -> None:
    mutations: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany) -> None:
        verb = statement.strip().split(None, 1)[0].upper()
        if verb in ("INSERT", "UPDATE", "DELETE"):
            mutations.append(verb)

    event.listen(audit.engine, "before_cursor_execute", _capture)
    try:
        audit.record("x")
        audit.record("y", subject=("Widget", 1))
    finally:
        event.remove(audit.engine, "before_cursor_execute", _capture)

    assert mutations == ["INSERT", "INSERT"]
