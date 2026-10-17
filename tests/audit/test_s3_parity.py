"""The S3 audit log's tamper-evidence is the SQL audit log's, scenario for scenario.

Each scenario records the same events into an ``AuditLog`` (SQLite) and an ``S3AuditLog``
(in-memory S3), seals, retires and tampers with both the same way — through each storage's own
raw access, as an attacker with write access to it would — and requires the shared verifier,
sealer and retention to reach the same verdict with the same findings on both.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch
from uuid import UUID

import pytest
from sqlalchemy import delete, insert, select, update

from firm._core.clock import now_utc
from firm.audit import AuditLog, schema
from firm.audit.event_format import canonical
from firm.audit.s3 import S3AuditLog
from tests.audit.conftest import MemoryS3

KEY = "parity-row-and-seal-secret-0123456789abcdef"
OTHER = "parity-a-different-secret-0123456789abcdef"
STREAM = str(UUID(int=1))
OLD = timedelta(hours=3)
FORGED = "01ZZZZZZZZZZZZZZZZZZZZZZZZ"


@contextmanager
def written_at(delta: timedelta):
    """Record events (on both backends) as if ``delta`` ago."""
    moment = now_utc() - delta
    with (
        patch("firm.audit.events.now_utc", lambda: moment),
        patch("firm.audit.s3.now_utc", lambda: moment),
    ):
        yield


@contextmanager
def sealed_at(delta: timedelta):
    moment = now_utc() - delta
    with patch("firm.audit.sealing.now_utc", lambda: moment):
        yield


class SqlSide:
    name = "sql"

    def __init__(self, tmp_path, **kwargs):
        self.log = AuditLog(f"sqlite:///{tmp_path / 'parity.db'}", **kwargs)
        self.engine = self.log.engine

    def set_field(self, event_id, field, value):
        with self.engine.begin() as conn:
            conn.execute(
                update(schema.audit_events)
                .where(schema.audit_events.c.id == event_id)
                .values({field: value})
            )

    def delete_row(self, event_id):
        with self.engine.begin() as conn:
            conn.execute(delete(schema.audit_events).where(schema.audit_events.c.id == event_id))

    def copy_row(self, source_id, new_id):
        """Insert a copy of a signed row at another id (with a new entry id: the attacker
        cannot reuse the original under SQL's unique index)."""
        with self.engine.begin() as conn:
            row = dict(
                conn.execute(
                    select(schema.audit_events).where(schema.audit_events.c.id == source_id)
                )
                .one()
                ._mapping
            )
            conn.execute(
                insert(schema.audit_events).values({**row, "id": new_id, "entry_id": FORGED})
            )

    def delete_records(self, kind):
        with self.engine.begin() as conn:
            conn.execute(delete(schema.seals).where(schema.seals.c.kind == kind))

    def set_record_field(self, kind, field, value):
        with self.engine.begin() as conn:
            conn.execute(
                update(schema.seals).where(schema.seals.c.kind == kind).values({field: value})
            )


class S3Side:
    name = "s3"

    def __init__(self, tmp_path, **kwargs):
        self.client = MemoryS3()
        self.log = S3AuditLog("events", stream=STREAM, client=self.client, **kwargs)
        self.layout = self.log.storage.layout

    def _event_key(self, event_id):
        prefix = f"{self.layout.events}{event_id:020d}-"
        return next(
            k for (_, k), v in self.client.objects.items() if k.startswith(prefix) and v[-1]
        )

    def _put(self, key, value):
        self.client.put_object(Bucket="events", Key=key, Body=canonical(value))

    def set_field(self, event_id, field, value):
        key = self._event_key(event_id)
        row = json.loads(self.client.objects[("events", key)][-1])
        self._put(key, {**row, field: value})

    def delete_row(self, event_id):
        self.client.delete_objects(
            Bucket="events", Delete={"Objects": [{"Key": self._event_key(event_id)}]}
        )

    def copy_row(self, source_id, new_id, *, entry_id=None):
        row = json.loads(self.client.objects[("events", self._event_key(source_id))][-1])
        entry = entry_id or FORGED
        self._put(self.layout.event(new_id, entry), {**row, "id": new_id, "entry_id": entry})

    def _records(self, kind):
        found = []
        for (_, key), versions in self.client.objects.items():
            if key.startswith(self.layout.seals) and versions[-1] is not None:
                record = json.loads(versions[-1])
                if record.get("kind") == kind:
                    found.append((key, record))
        return found

    def delete_records(self, kind):
        keys = [{"Key": key} for key, _ in self._records(kind)]
        self.client.delete_objects(Bucket="events", Delete={"Objects": keys})

    def set_record_field(self, kind, field, value):
        for key, record in self._records(kind):
            self._put(key, {**record, field: value})


@pytest.fixture(params=[SqlSide, S3Side], ids=["sql", "s3"])
def side_cls(request):
    return request.param


def _both(tmp_path, **kwargs):
    kwargs = {"mac_key": KEY, "grace": 0.0, "seal_batch_size": 3, **kwargs}
    (tmp_path / "sql").mkdir()
    return SqlSide(tmp_path / "sql", **kwargs), S3Side(tmp_path, **kwargs)


def _verdict(side, **kwargs):
    report = side.log.verify(full=True, **kwargs)
    return (
        report.outcome,
        report.ok_count,
        report.unprotected_count,
        report.tampered_count,
        sorted(finding.message for finding in report.findings),
    )


def _seeded(tmp_path, *, events=7, **kwargs):
    sides = _both(tmp_path, **kwargs)
    for side in sides:
        with written_at(OLD):
            for index in range(events):
                side.log.record(f"event.{index}", subject=("Order", index), actor=("User", 1))
        with sealed_at(timedelta(hours=2)):
            side.log.sealer.run_once()
    return sides


def _assert_same(tmp_path, tamper, **kwargs):
    sql, s3 = _seeded(tmp_path, **kwargs)
    for side in (sql, s3):
        tamper(side)
    verdicts = {side.name: _verdict(side) for side in (sql, s3)}
    assert verdicts["s3"] == verdicts["sql"]
    for side in (sql, s3):
        side.log.close()
    return verdicts["sql"]


def test_clean_history_verifies_ok_on_both(tmp_path):
    outcome, ok, *_ = _assert_same(tmp_path, lambda side: None)
    assert outcome == "ok" and ok == 7


@pytest.mark.parametrize(
    ("name", "tamper", "expected"),
    [
        ("modified sealed row", lambda side: side.set_field(2, "action", "forged"), "tampered"),
        ("modified tail row", lambda side: side.set_field(7, "data", '{"x":1}'), "tampered"),
        ("deleted sealed row", lambda side: side.delete_row(2), "tampered"),
        ("deleted tail row", lambda side: side.delete_row(7), "tampered"),
        (
            "forged row in a sealed position",
            lambda side: (side.delete_row(2), side.copy_row(1, 2)),
            "tampered",
        ),
        ("copied row in the tail", lambda side: side.copy_row(3, 50), "tampered"),
        ("stripped signature", lambda side: side.set_field(4, "row_mac", None), "tampered"),
        ("deleted activation", lambda side: side.delete_records("activation"), "tampered"),
        # Without an anchor, losing every seal leaves an over-age unsealed tail: a warning on
        # both. Detecting the deletion itself is the anchor's job (see the anchor test).
        ("deleted seals", lambda side: side.delete_records("seal"), "warning"),
        (
            "forged seal mac",
            lambda side: side.set_record_field("seal", "rows_mac", "0" * 64),
            "tampered",
        ),
        (
            "moved activation boundary",
            lambda side: side.set_record_field("activation", "to_id", 9),
            "tampered",
        ),
    ],
)
def test_tampering_is_reported_identically(tmp_path, name, tamper, expected):
    outcome, *_ = _assert_same(tmp_path, tamper)
    assert outcome == expected, name


def test_s3_reports_a_replayed_entry_id(tmp_path):
    """SQL's unique index stops a replayed ``entry_id`` at insert time; S3 has no such index,
    so the verifier's duplicate check (shared with SQL) is what reports it."""
    _, s3 = _seeded(tmp_path)
    entry = json.loads(s3.client.objects[("events", s3._event_key(3))][-1])["entry_id"]
    s3.copy_row(3, 50, entry_id=entry)
    report = s3.log.verify(full=True)
    assert report.outcome == "tampered"
    assert any("appears more than once" in finding.message for finding in report.findings)
    s3.log.close()


def test_retention_retires_and_refuses_identically(tmp_path):
    sql, s3 = _seeded(tmp_path, max_age=3600)
    pruned = {side.name: side.log.retention.run_once() for side in (sql, s3)}
    assert pruned["s3"] == pruned["sql"] == 7  # every settled seal range, the short tail's too
    assert _verdict(s3) == _verdict(sql)
    assert s3.log.history() == sql.log.history() == []
    # A tampered range is refused, not deleted, on both.
    (tmp_path / "again").mkdir()
    sql2, s32 = _seeded(tmp_path / "again", max_age=3600)
    for side in (sql2, s32):
        side.set_field(2, "action", "forged")
        assert side.log.retention.run_once() == 0
        assert side.log.retention.last_refused_tampered == 1
    for side in (sql, s3, sql2, s32):
        side.log.close()


def test_anchor_detects_seal_deletion_identically(tmp_path):
    anchors = {}
    sides = []
    for side_cls in (SqlSide, S3Side):
        root = tmp_path / side_cls.name
        root.mkdir()
        anchor = root / "audit.anchor"
        anchors[side_cls.name] = anchor
        side = side_cls(root, mac_key=KEY, grace=0.0, seal_batch_size=3, anchor_path=str(anchor))
        with written_at(OLD):
            for index in range(6):
                side.log.record(f"event.{index}")
        with sealed_at(timedelta(hours=2)):
            side.log.sealer.run_once()
        sides.append(side)
    assert _verdict(sides[1]) == _verdict(sides[0])
    for side in sides:
        side.delete_records("seal")
        side.delete_row(1)
    first, second = (_verdict(side) for side in sides)
    assert second == first and first[0] == "tampered"
    for side in sides:
        side.log.close()


def test_rotated_keys_verify_identically(tmp_path, monkeypatch):
    sql, s3 = _seeded(tmp_path)
    for side in (sql, s3):
        side.log.close()
    monkeypatch.setenv("FIRM_AUDIT_RETIRED_KEYS", f"old={KEY}")
    monkeypatch.setenv("FIRM_AUDIT_RETIRED_SEAL_KEYS", f"old={KEY}")
    rotated = (
        AuditLog(sql.engine.url.render_as_string(hide_password=False), mac_key=OTHER, grace=0.0),
        S3AuditLog("events", stream=STREAM, client=s3.client, mac_key=OTHER, grace=0.0),
    )
    for log in rotated:
        log.record("after.rotation")
    reports = [log.verify(full=True) for log in rotated]
    assert [(r.outcome, r.ok_count, r.tampered_count) for r in reports] == [
        (reports[0].outcome, 8, 0)
    ] * 2
    for log in rotated:
        log.close()


def test_a_missing_key_is_an_error_on_both(tmp_path, side_cls):
    side = side_cls(tmp_path)
    side.log.record("unsigned")
    from firm.audit.verify import VerifyError

    with pytest.raises(VerifyError, match="row key"):
        side.log.verify()
    side.log.close()
