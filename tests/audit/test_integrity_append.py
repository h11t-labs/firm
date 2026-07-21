"""Layer-1 write path: ``AuditLog.record`` / module ``record`` with a key configured.

Two things are proved here. First, the design's **mandatory per-dialect round-trip property**
(review 2A): insert → read the row back → recompute its MAC == the stored MAC, on SQLite by
default and on Postgres/MySQL when their ``FIRM_TEST_*`` URLs are set, with adversarial payloads
(emoji/4-byte unicode, embedded NUL, framing-marker bytes, very long strings). A value the
database round-trips lossily would else verify as TAMPERED on one dialect and OK on another.
Second, the **no-key regression invariant**: without a key the three new columns stay NULL and
``record``/``history`` behave exactly as before tamper-evidence existed.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from firm._core.clock import now_utc
from firm._core.database import transaction
from firm.audit import AuditLog, Ref, record, schema
from firm.audit.integrity import KEY_ID_LENGTH, KEY_MIN_LENGTH, load_key, row_mac

# A valid throwaway writer key (>= 32 chars).
_SECRET = "roundtrip-secret-key-padding-0123456789"  # noqa: S105
_KEY = load_key(_SECRET)
assert _KEY is not None

_audits = schema.audit_events


@pytest.fixture(autouse=True)
def _no_ambient_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a stray ``FIRM_AUDIT_KEY`` in the environment leak into a test — each test that
    wants a key passes it explicitly (or sets the env itself)."""
    monkeypatch.delenv("FIRM_AUDIT_KEY", raising=False)


def _raw_rows(engine) -> list:
    with transaction(engine) as conn:
        return conn.execute(select(_audits).order_by(_audits.c.id)).all()


def _recompute(row) -> str:
    """Recompute a row's MAC from the values *as the database returned them* (round-trip rule)."""
    return row_mac(
        _KEY,
        entry_id=row.entry_id,
        action=row.action,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        subject_label=row.subject_label,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        actor_label=row.actor_label,
        correlation_id=row.correlation_id,
        data=row.data,
        changes=row.changes,
        context=row.context,
        created_at=row.created_at,
    )


def test_key_populates_entry_id_row_mac_key_id(db_url: str) -> None:
    log = AuditLog(database_url=db_url, mac_key=_SECRET)
    try:
        log.record("invoice.paid", subject=("Invoice", 42), data={"amount": 100})
        (row,) = _raw_rows(log.engine)
        assert row.entry_id is not None and len(row.entry_id) == 26
        assert row.row_mac is not None and len(row.row_mac) == 64
        assert row.key_id == _KEY.id and len(row.key_id) == KEY_ID_LENGTH
        assert _recompute(row) == row.row_mac
    finally:
        log.close()


def test_created_at_is_captured_once_for_row_and_mac(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # append must capture created_at exactly once and feed that single value to both the stored
    # column and the MAC input. A counter that yields a different datetime on every now_utc() call
    # makes a hypothetical double-capture sign a timestamp the row does not store, so the read-back
    # row would fail to recompute — this test would then go red. Under the single-capture contract
    # the stored created_at and the signed created_at are identical, so recompute matches.
    ticks = (datetime(2026, 1, 1, 0, 0, 0) + timedelta(seconds=i) for i in range(1, 1000))
    monkeypatch.setattr("firm.audit.events.now_utc", lambda: next(ticks))
    log = AuditLog(database_url=db_url, mac_key=_SECRET)
    try:
        log.record("clock.capture", subject=("Invoice", 7), data={"amount": 1})
        (row,) = _raw_rows(log.engine)
        assert _recompute(row) == row.row_mac
    finally:
        log.close()


# Adversarial payloads for the round-trip property. Embedded NUL / 4-byte unicode / framing-marker
# bytes / very long strings live inside the JSON payload (stored as opaque ``Text``); the emoji
# label additionally exercises a 4-byte character in an indexed ``String`` column (utf8mb4 on
# MySQL, per the design's charset requirement).
_ADVERSARIAL = {
    "plain": {"data": {"amount": 100, "name": "café"}, "label": None},
    "emoji_4byte": {"data": {"msg": "🎉 café 🀄 4-byte"}, "label": "🎉 acct"},
    "embedded_nul": {"data": {"blob": "a\x00b\x00c"}, "label": None},
    "framing_bytes": {"data": {"x": "\x01\x00\x02 length-prefix bait"}, "label": None},
    "very_long": {"data": {"blob": "x" * 10_000}, "label": None},
}


@pytest.mark.parametrize("case", list(_ADVERSARIAL), ids=list(_ADVERSARIAL))
def test_roundtrip_property_recompute_matches_stored(db_url: str, case: str) -> None:
    payload = _ADVERSARIAL[case]
    label = payload["label"]
    subject = Ref("Account", 7, name=label) if label else ("Account", 7)
    log = AuditLog(database_url=db_url, mac_key=_SECRET)
    try:
        log.record(
            "obj.changed",
            subject=subject,
            data=payload["data"],
            changes={"before": None, "after": payload["data"]},
            context={"ip": "127.0.0.1"},
            correlation_id="req-abc",
        )
        (row,) = _raw_rows(log.engine)
        assert row.row_mac is not None
        assert _recompute(row) == row.row_mac
    finally:
        log.close()


def test_duplicate_entry_id_rejected_by_unique_index(db_url: str) -> None:
    log = AuditLog(database_url=db_url, mac_key=_SECRET)
    try:
        log.record("first")
        (row,) = _raw_rows(log.engine)
        # Replaying a real row's entry_id (the anti-replay case) must be refused at insert.
        with pytest.raises(IntegrityError), transaction(log.engine) as conn:
            conn.execute(
                _audits.insert().values(
                    action="replayed",
                    created_at=now_utc(),
                    entry_id=row.entry_id,
                    row_mac=row.row_mac,
                    key_id=row.key_id,
                )
            )
    finally:
        log.close()


def test_module_level_record_signs_from_env_key(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIRM_AUDIT_KEY", _SECRET)
    log = AuditLog(database_url=db_url)
    try:
        with transaction(log.engine) as conn:
            record(conn, "user.login", actor=("User", 7))
        (row,) = _raw_rows(log.engine)
        assert row.entry_id is not None and row.key_id == _KEY.id
        assert _recompute(row) == row.row_mac
    finally:
        log.close()


def test_short_key_is_a_hard_error_at_construction(db_url: str) -> None:
    with pytest.raises(ValueError, match="at least"):
        AuditLog(database_url=db_url, mac_key="too-short")
    assert len("too-short") < KEY_MIN_LENGTH


# -- no-key regression invariant ----------------------------------------------------------------


def test_no_key_leaves_integrity_columns_null(db_url: str) -> None:
    log = AuditLog(database_url=db_url)  # no mac_key, no FIRM_AUDIT_KEY (autouse fixture)
    try:
        log.record("system.boot", subject=("Widget", 1), data={"k": "v"})
        (row,) = _raw_rows(log.engine)
        assert row.entry_id is None
        assert row.row_mac is None
        assert row.key_id is None
    finally:
        log.close()


def test_no_key_record_and_history_behave_as_before(db_url: str) -> None:
    # Canary: the read surface is untouched by the presence of the new columns.
    log = AuditLog(database_url=db_url)
    try:
        log.record("invoice.paid", subject=("Invoice", 42), actor="cron", data={"amount": 100})
        rows = log.history()
        assert len(rows) == 1
        assert rows[0]["action"] == "invoice.paid"
        assert rows[0]["subject_type"] == "Invoice"
        assert rows[0]["subject_id"] == "42"
        assert rows[0]["actor_type"] == "cron"
        assert rows[0]["data"] == {"amount": 100}
        # _row_to_dict exposes exactly the pre-tamper-evidence keys — no entry_id/row_mac/key_id.
        assert "entry_id" not in rows[0]
        assert "row_mac" not in rows[0]
    finally:
        log.close()
