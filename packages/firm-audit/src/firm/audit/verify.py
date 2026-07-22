"""Verify independent seals, the retirement floor, activation, rows, and the anchor.

Every database read in a run shares one snapshot. Present seals authenticate themselves and their
exact rows; valid floor advances authorize an empty pruned prefix; one signed activation marker
separates legacy rows from the protected region. The append-only anchor remembers records the
database may no longer contain. Range ordering is expressed only in event-id space.

Default runs always check markers, seal MACs, contiguity, the legacy prefix, the unsealed tail,
duplicates, anchor completeness, the newest range, and a stateless date-derived slice of older
ranges. Only ``full=True`` recomputes every sealed range. Attacker-controlled marker and anchor
fields are parsed defensively: malformed values produce TAMPERED findings, never exceptions.
A strict-prefix anchor fragment from an interrupted append is the sole WARNING exception. Unknown
``key_id`` values remain a verification error and are persisted as ``outcome="error"``.
"""

from __future__ import annotations

import hmac
import json
import math
import os
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from itertools import pairwise
from typing import TYPE_CHECKING, Any

from sqlalchemy import Connection, String, cast, func, select
from sqlalchemy.exc import SQLAlchemyError

from .._core.clock import now_utc
from .._core.database import snapshot_transaction
from .._core.dialects import get_dialect
from . import integrity, schema
from .integrity import Key, parse_keyring

if TYPE_CHECKING:
    from .log import AuditLog

_audits = schema.audit_events
_seals = schema.seals
_status = schema.verify_status

_RETIRED_KEYS_ENV = "FIRM_AUDIT_RETIRED_KEYS"
_RETIRED_SEAL_KEYS_ENV = "FIRM_AUDIT_RETIRED_SEAL_KEYS"
_PAGE = 1000
_MAX_AFFECTED = 20
_MAX_FINDINGS = 1000
_STATUS_ID = 1


class VerifyError(Exception):
    """Verification cannot reach a verdict, most commonly because a signing key is unavailable."""


@dataclass(frozen=True)
class SealRecord:
    """One defensively parsed row from ``firm_audit_seals``."""

    id: int
    kind: str
    from_id: int | None
    to_id: int | None
    row_count: int | None
    rows_mac: str | None
    seal_mac: str | None
    sealed_at: datetime | None
    key_id: str | None
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class AnchorEvent:
    """One validly parsed new-format anchor line."""

    kind: str
    at: datetime
    from_id: int | None
    to_id: int
    mac: str
    line_number: int


@dataclass(frozen=True)
class AnchorData:
    exists: bool
    events: tuple[AnchorEvent, ...]
    malformed: tuple[MalformedAnchorLine, ...]
    partial_tail: MalformedAnchorLine | None


@dataclass(frozen=True)
class MalformedAnchorLine:
    """One anchor line that did not parse, retained for position-aware classification."""

    line_number: int | None
    content: str | None
    message: str


def _parse_int(value: Any, *, name: str, required: bool) -> tuple[int | None, str | None]:
    if value is None:
        return (None, f"{name} is NULL") if required else (None, None)
    try:
        text = str(value)
        parsed = int(text)
        if str(parsed) != text.strip():
            raise ValueError
        return parsed, None
    except (TypeError, ValueError):
        return None, f"{name} {value!r} is not an integer"


def _parse_datetime(value: Any) -> tuple[datetime | None, str | None]:
    if value is None:
        return None, "sealed_at is NULL"
    try:
        return datetime.fromisoformat(str(value)), None
    except (TypeError, ValueError):
        return None, f"sealed_at {value!r} is not an ISO datetime"


def load_seal_records(conn: Connection) -> list[SealRecord]:
    """Load side-table rows without invoking dialect datetime/integer result processors.

    Casting attacker-controlled typed fields to text lets verification classify malformed values
    instead of raising while SQLAlchemy decodes them.
    """
    rows = conn.execute(
        select(
            _seals.c.id,
            cast(_seals.c.kind, String).label("kind"),
            cast(_seals.c.from_id, String).label("from_id"),
            cast(_seals.c.to_id, String).label("to_id"),
            cast(_seals.c.row_count, String).label("row_count"),
            cast(_seals.c.rows_mac, String).label("rows_mac"),
            cast(_seals.c.seal_mac, String).label("seal_mac"),
            cast(_seals.c.sealed_at, String).label("sealed_at"),
            cast(_seals.c.key_id, String).label("key_id"),
        ).order_by(_seals.c.id)
    ).all()
    records: list[SealRecord] = []
    for row in rows:
        errors: list[str] = []
        kind = row.kind or ""
        from_id, error = _parse_int(row.from_id, name="from_id", required=kind == "seal")
        if error:
            errors.append(error)
        to_id, error = _parse_int(row.to_id, name="to_id", required=True)
        if error:
            errors.append(error)
        row_count, error = _parse_int(row.row_count, name="row_count", required=kind == "seal")
        if error:
            errors.append(error)
        sealed_at, error = _parse_datetime(row.sealed_at)
        if error:
            errors.append(error)
        if kind not in {"seal", "floor", "activation"}:
            errors.append(f"unknown kind {kind!r}")
        if row.seal_mac is None:
            errors.append("seal_mac is NULL")
        if row.key_id is None:
            errors.append("key_id is NULL")
        if kind == "seal":
            if row.rows_mac is None:
                errors.append("rows_mac is NULL")
            if from_id is not None and to_id is not None and from_id >= to_id:
                errors.append("seal range is empty or reversed")
            if row_count is not None and row_count < 0:
                errors.append("row_count is negative")
        elif kind == "activation":
            if from_id != -1:
                errors.append("activation from_id is not the reserved -1 value")
            if row_count is not None or row.rows_mac is not None:
                errors.append("activation carries non-canonical range fields")
        elif kind == "floor":
            if from_id is not None or row_count is not None or row.rows_mac is not None:
                errors.append("floor carries non-canonical range fields")
        records.append(
            SealRecord(
                id=row.id,
                kind=kind,
                from_id=from_id,
                to_id=to_id,
                row_count=row_count,
                rows_mac=row.rows_mac,
                seal_mac=row.seal_mac,
                sealed_at=sealed_at,
                key_id=row.key_id,
                errors=tuple(errors),
            )
        )
    return records


def _iter_rows(conn: Connection, low: int, high: int | None) -> Iterator[Any]:
    """Yield event rows in id order with keyset pagination."""
    last = low
    while True:
        stmt = select(_audits).where(_audits.c.id > last)
        if high is not None:
            stmt = stmt.where(_audits.c.id <= high)
        rows = conn.execute(stmt.order_by(_audits.c.id).limit(_PAGE)).all()
        if not rows:
            return
        yield from rows
        if len(rows) < _PAGE:
            return
        last = rows[-1].id


def recompute_row_mac(key: Key, row: Any) -> str:
    """Recompute one row MAC from the values returned by the database."""
    return integrity.row_mac(
        key,
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


def _row_verdict(row: Any, boundary: int | None, keyring: dict[str, Key]) -> str:
    if row.row_mac is None:
        if boundary is None or row.id <= boundary:
            return "unprotected"
        return "tampered"
    key = keyring.get(row.key_id)
    if key is None:
        raise VerifyError(
            f"row {row.id} was signed by unknown key_id {row.key_id!r} — add its secret to "
            "FIRM_AUDIT_RETIRED_KEYS."
        )
    try:
        expected = recompute_row_mac(key, row)
    except (TypeError, ValueError, UnicodeError):
        return "tampered"
    return "ok" if hmac.compare_digest(expected, row.row_mac) else "tampered"


RowCallback = Callable[[Any, str], None]


def classify_range(
    conn: Connection,
    seal: SealRecord,
    boundary: int | None,
    keyring: dict[str, Key],
    seal_keyring: dict[str, Key],
    *,
    on_row: RowCallback | None = None,
) -> str:
    """Recompute a range exactly; verify and retention share this one classifier.

    Any surplus, missing, altered, unsigned, or otherwise invalid row makes the result
    ``"tampered"``. The iterator and aggregate HMAC are streaming, so range size does not control
    memory use.
    """
    if seal.errors or seal.kind != "seal":
        return "tampered"
    assert seal.from_id is not None
    assert seal.to_id is not None
    assert seal.row_count is not None
    assert seal.rows_mac is not None
    assert seal.key_id is not None
    from_id = seal.from_id
    to_id = seal.to_id
    key = seal_keyring.get(seal.key_id)
    if key is None:
        raise VerifyError(
            f"sealed range ({seal.from_id}, {seal.to_id}] was signed by key_id "
            f"{seal.key_id!r}, which is not available as a seal key."
        )

    count = 0
    rows_ok = True

    def pairs() -> Iterator[tuple[int, str]]:
        nonlocal count, rows_ok
        for row in _iter_rows(conn, from_id, to_id):
            count += 1
            verdict = _row_verdict(row, boundary, keyring)
            if on_row is not None:
                on_row(row, verdict)
            if verdict != "ok" or row.row_mac is None:
                rows_ok = False
            yield row.id, row.row_mac or ""

    aggregate = integrity.rows_mac(key, pairs())
    if rows_ok and count == seal.row_count and hmac.compare_digest(aggregate, seal.rows_mac):
        return "ok"
    return "tampered"


def range_is_prunable(
    conn: Connection,
    seal: SealRecord,
    boundary: int | None,
    keyring: dict[str, Key],
    seal_keyring: dict[str, Key],
) -> bool:
    """Retention gate: only a range the shared classifier calls exactly ``ok`` is prunable."""
    try:
        return classify_range(conn, seal, boundary, keyring, seal_keyring) == "ok"
    except VerifyError:
        return False


def recompute_seal_mac(key: Key, record: SealRecord) -> str:
    """Recompute any side-table record's kind-specific MAC."""
    assert record.to_id is not None
    assert record.sealed_at is not None
    assert record.key_id is not None
    if record.kind == "seal":
        assert record.from_id is not None
        assert record.row_count is not None
        assert record.rows_mac is not None
        return integrity.seal_mac(
            key,
            from_id=record.from_id,
            to_id=record.to_id,
            row_count=record.row_count,
            rows_mac=record.rows_mac,
            sealed_at=record.sealed_at,
            key_id=record.key_id,
        )
    if record.kind == "floor":
        return integrity.floor_mac(
            key,
            through_id=record.to_id,
            retired_at=record.sealed_at,
            key_id=record.key_id,
        )
    return integrity.activation_mac(
        key,
        boundary_id=record.to_id,
        at=record.sealed_at,
        key_id=record.key_id,
    )


def seal_is_intact(record: SealRecord, seal_keyring: dict[str, Key]) -> bool:
    """Whether one independent seal/floor/activation record is canonical and authentic."""
    if record.errors or record.key_id is None or record.seal_mac is None:
        return False
    key = seal_keyring.get(record.key_id)
    if key is None:
        return False
    return hmac.compare_digest(recompute_seal_mac(key, record), record.seal_mac)


def _format_anchor_event(
    *, kind: str, from_id: int | None, to_id: int, mac: str, at: datetime
) -> str:
    """Return the canonical anchor line without its terminating newline."""
    created_at = integrity.canonical_created_at(at)
    if kind == "seal":
        assert from_id is not None
        return f"{created_at} SEAL {from_id} {to_id} {mac}"
    if kind == "floor":
        return f"{created_at} FLOOR {to_id} {mac}"
    if kind == "activation":
        return f"{created_at} ACTIVATION {to_id} {mac}"
    raise ValueError(f"unknown anchor event kind {kind!r}")


def _record_anchor_line(record: SealRecord) -> str:
    assert record.to_id is not None
    assert record.seal_mac is not None
    assert record.sealed_at is not None
    return _format_anchor_event(
        kind=record.kind,
        from_id=record.from_id,
        to_id=record.to_id,
        mac=record.seal_mac,
        at=record.sealed_at,
    )


def _event_anchor_line(event: AnchorEvent) -> str:
    return _format_anchor_event(
        kind=event.kind,
        from_id=event.from_id,
        to_id=event.to_id,
        mac=event.mac,
        at=event.at,
    )


def _classify_anchor_issues(
    anchor: AnchorData,
    intact_records: Sequence[SealRecord],
    *,
    retired_through: int = 0,
) -> tuple[tuple[MalformedAnchorLine, ...], tuple[MalformedAnchorLine, ...]]:
    """Split malformed input into benign partial appends and actual corruption.

    A crash can leave the final append as a strict prefix of its committed record's canonical
    line. The sealer's heal pass appends the complete line after that fragment, moving the fragment
    into the middle of the file. Position therefore is only an initial parsing hint: a fragment is
    benign whenever it is a strict prefix of either an intact committed record or its complete,
    healed SEAL event below an honored retirement floor. Every other malformed line is corruption.
    This keeps healed fragments benign after retention legitimately removes their database record
    without treating arbitrary uncommitted anchor events as reconstruction evidence.
    """
    reconstructible = {_record_anchor_line(record) for record in intact_records}
    reconstructible.update(
        _event_anchor_line(event)
        for event in anchor.events
        if event.kind == "seal" and event.to_id <= retired_through
    )
    issues = [*anchor.malformed]
    if anchor.partial_tail is not None:
        issues.append(anchor.partial_tail)
    partial: list[MalformedAnchorLine] = []
    corrupt: list[MalformedAnchorLine] = []
    for issue in issues:
        content = issue.content
        if content and any(
            line != content and line.startswith(content) for line in reconstructible
        ):
            partial.append(issue)
        else:
            corrupt.append(issue)
    return tuple(partial), tuple(corrupt)


def _read_anchor(path: str) -> AnchorData:
    """Parse new-format anchor events and retain malformed lines without ever raising.

    The final malformed non-blank line is carried separately as a potential interrupted append.
    Its final classification still requires the strict-prefix rule in
    :func:`_classify_anchor_issues`; arbitrary garbage at EOF is not automatically trusted.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError:
        return AnchorData(False, (), (), None)
    except OSError as exc:
        issue = MalformedAnchorLine(None, None, f"anchor cannot be read: {exc}")
        return AnchorData(True, (), (issue,), None)

    events: list[AnchorEvent] = []
    malformed: list[MalformedAnchorLine] = []
    partial_tail: MalformedAnchorLine | None = None
    nonblank = [index for index, line in enumerate(lines) if line.strip()]
    last_nonblank = nonblank[-1] if nonblank else None
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        parts = line.split()
        try:
            at = datetime.fromisoformat(parts[0])
            kind = parts[1]
            if kind == "SEAL" and len(parts) == 5:
                from_id = int(parts[2])
                to_id = int(parts[3])
                mac = parts[4]
                if from_id >= to_id:
                    raise ValueError
                events.append(AnchorEvent("seal", at, from_id, to_id, mac, line_number))
            elif kind in {"FLOOR", "ACTIVATION"} and len(parts) == 4:
                to_id = int(parts[2])
                events.append(AnchorEvent(kind.lower(), at, None, to_id, parts[3], line_number))
            else:
                raise ValueError
        except (IndexError, ValueError):
            issue = MalformedAnchorLine(
                line_number, line, f"anchor line {line_number} is malformed"
            )
            if line_number - 1 == last_nonblank:
                partial_tail = issue
            else:
                malformed.append(issue)
    return AnchorData(True, tuple(events), tuple(malformed), partial_tail)


def _record_matches_anchor(record: SealRecord, event: AnchorEvent) -> bool:
    if (
        record.kind != event.kind
        or record.to_id != event.to_id
        or record.seal_mac != event.mac
        or record.sealed_at is None
    ):
        return False
    if event.kind == "seal" and record.from_id != event.from_id:
        return False
    return integrity.canonical_created_at(record.sealed_at) == integrity.canonical_created_at(
        event.at
    )


class _Findings(list):  # type: ignore[type-arg]
    def append(self, item: Finding) -> None:
        if len(self) < _MAX_FINDINGS:
            super().append(item)


@dataclass(frozen=True)
class Finding:
    verdict: str
    message: str
    identifier: str | None = None
    id: int | None = None


@dataclass
class _Counters:
    ok: int = 0
    warning: int = 0
    unprotected: int = 0
    tampered: int = 0


@dataclass
class VerifyReport:
    outcome: str
    exit_code: int
    findings: list[Finding] = field(default_factory=list)
    ok_count: int = 0
    warning_count: int = 0
    unprotected_count: int = 0
    tampered_count: int = 0
    error_message: str | None = None
    last_full_coverage_at: datetime | None = None
    newest_anchor_at: datetime | None = None
    anchor_configured: bool = False
    unsealed_tail_count: int = 0
    unsealed_tail_oldest_at: datetime | None = None
    affected_identifiers: str | None = None
    duration_seconds: float = 0.0


@dataclass(frozen=True)
class IntegrityAlert:
    severity: str
    outcome: str
    ran_at: datetime
    ok_count: int
    warning_count: int
    unprotected_count: int
    tampered_count: int
    affected: tuple[str, ...]


def default_on_finding(alert: IntegrityAlert) -> None:
    n = alert.tampered_count if alert.severity == "critical" else alert.warning_count
    headline = "tamper detected" if alert.severity == "critical" else "verify warning"
    detail = f", affected: {', '.join(alert.affected)}" if alert.affected else ""
    ran_at = alert.ran_at.isoformat(sep=" ", timespec="seconds")
    print(
        f"firm-audit: {alert.severity.upper()} {headline} — {n} finding{'' if n == 1 else 's'}"
        f"{detail} (verified {ran_at})",
        file=sys.stderr,
    )


def _affected_json(findings: Sequence[Finding], total_tampered: int) -> str | None:
    shown = [f for f in findings if f.verdict == "tampered" and f.identifier][:_MAX_AFFECTED]
    if not shown:
        return None
    items: list[dict[str, Any]] = []
    for finding in shown:
        assert finding.identifier is not None
        item: dict[str, Any] = {
            "kind": "row" if finding.id is not None else "seal",
            "label": finding.identifier,
            "message": finding.message,
            "verdict": finding.verdict,
        }
        if finding.id is not None:
            item["id"] = finding.id
        items.append(item)
    overflow = total_tampered - len(items)
    if overflow > 0:
        items.append(
            {"kind": "more", "label": f"+{overflow} more finding(s)", "verdict": "tampered"}
        )
    return json.dumps(items, separators=(",", ":"))


class Verifier:
    """Run the eight-point invariant for one :class:`~firm.audit.log.AuditLog`."""

    def __init__(self, audit: AuditLog) -> None:
        self.audit = audit

    @property
    def keyring(self) -> dict[str, Key]:
        ring: dict[str, Key] = {}
        if self.audit._key is not None:
            self._ring_add(ring, self.audit._key, source="FIRM_AUDIT_KEY")
        for extra in self._parse_ring_env(_RETIRED_KEYS_ENV).values():
            self._ring_add(ring, extra, source=_RETIRED_KEYS_ENV)
        return ring

    @property
    def seal_keyring(self) -> dict[str, Key]:
        ring: dict[str, Key] = {}
        if self.audit._seal_key is not None:
            self._ring_add(ring, self.audit._seal_key, source="FIRM_AUDIT_SEAL_KEY")
        if self.audit._key is not None and not self.audit._seal_key_split:
            self._ring_add(ring, self.audit._key, source="FIRM_AUDIT_KEY")
        for extra in self._parse_ring_env(_RETIRED_SEAL_KEYS_ENV).values():
            self._ring_add(ring, extra, source=_RETIRED_SEAL_KEYS_ENV)
        return ring

    @staticmethod
    def _parse_ring_env(env: str) -> dict[str, Key]:
        try:
            return parse_keyring(os.environ.get(env), source=env)
        except ValueError as exc:
            raise VerifyError(str(exc)) from exc

    @staticmethod
    def _ring_add(ring: dict[str, Key], key: Key, *, source: str) -> None:
        try:
            integrity.add_key(ring, key, source=source)
        except ValueError as exc:
            raise VerifyError(str(exc)) from exc

    def run(self, *, anchor_path: str | None = None, full: bool = False) -> VerifyReport:
        started = time.monotonic()
        try:
            report = self._verify(anchor_path=anchor_path, full=full)
        except VerifyError as exc:
            self._persist_error(str(exc))
            raise
        except (SQLAlchemyError, TypeError, ValueError, UnicodeError) as exc:
            now = now_utc()
            finding = Finding(
                "tampered",
                f"tamper-evidence storage could not be parsed or read ({type(exc).__name__})",
                "audit-integrity-storage",
            )
            report = VerifyReport(
                outcome="tampered",
                exit_code=1,
                findings=[finding],
                tampered_count=1,
                last_full_coverage_at=now if full else None,
                anchor_configured=anchor_path is not None,
                affected_identifiers=_affected_json([finding], 1),
            )
        report.duration_seconds = time.monotonic() - started
        self._persist(report)
        self._emit_finding(report)
        return report

    def _verify(self, *, anchor_path: str | None, full: bool) -> VerifyReport:
        keyring = self.keyring
        if not keyring:
            raise VerifyError("audit verification needs a configured row key")
        seal_keyring = self.seal_keyring
        now = now_utc()
        counters = _Counters()
        findings: list[Finding] = _Findings()
        anchor = _read_anchor(anchor_path) if anchor_path is not None else None

        with snapshot_transaction(self.audit.engine) as conn:
            records = load_seal_records(conn)
            intact = self._check_records(records, keyring, seal_keyring, counters, findings)
            floor = self._resolve_floor(records, intact, anchor, seal_keyring, counters, findings)
            boundary = self._resolve_activation(
                records, intact, anchor, seal_keyring, counters, findings
            )

            self._check_pruned_region_empty(conn, floor, counters, findings)
            self._check_duplicates(conn, counters, findings)
            self._verify_legacy_prefix(conn, floor, boundary, keyring, counters, findings)

            covering = [
                record
                for record in records
                if record.kind == "seal"
                and not record.errors
                and record.to_id is not None
                and record.to_id > floor
            ]
            covering.sort(key=lambda record: (record.from_id or -1, record.id))
            origin = max(floor, boundary or 0)
            self._check_contiguity(covering, origin, counters, findings)
            for record in self._select_ranges(covering, full=full, now=now):
                self._verify_one_range(
                    conn,
                    record,
                    boundary,
                    keyring,
                    seal_keyring,
                    counters,
                    findings,
                )

            max_covered = max(
                [origin, *(record.to_id or origin for record in covering)], default=origin
            )
            tail_count, tail_oldest = self._verify_tail(
                conn, max_covered, boundary, keyring, counters, findings
            )
            self._check_tail_liveness(
                any(record.kind == "activation" for record in records),
                tail_oldest,
                now,
                counters,
                findings,
            )
            newest_anchor_at, force_nonzero = self._check_anchor(
                anchor,
                records,
                intact,
                floor,
                seal_keyring,
                now,
                counters,
                findings,
            )
            prior = conn.execute(
                select(_status.c.last_full_coverage_at).where(_status.c.id == _STATUS_ID)
            ).first()

        return self._build_report(
            counters=counters,
            findings=findings,
            full=full,
            prior_full_coverage=prior.last_full_coverage_at if prior else None,
            now=now,
            tail_count=tail_count,
            tail_oldest=tail_oldest,
            newest_anchor_at=newest_anchor_at,
            anchor_configured=anchor_path is not None,
            force_nonzero=force_nonzero,
        )

    def _check_records(
        self,
        records: Sequence[SealRecord],
        keyring: dict[str, Key],
        seal_keyring: dict[str, Key],
        counters: _Counters,
        findings: list[Finding],
    ) -> set[int]:
        intact: set[int] = set()
        for record in records:
            label = f"{record.kind or 'unknown'} record {record.id}"
            if record.errors:
                findings.append(Finding("tampered", "; ".join(record.errors), label))
                counters.tampered += 1
                continue
            assert record.key_id is not None
            key = seal_keyring.get(record.key_id)
            if key is None:
                if record.key_id in keyring:
                    raise VerifyError(
                        f"{label} uses key_id {record.key_id!r}, a row key that is not a seal key"
                    )
                raise VerifyError(f"{label} uses unknown seal key_id {record.key_id!r}")
            assert record.seal_mac is not None
            if not hmac.compare_digest(recompute_seal_mac(key, record), record.seal_mac):
                findings.append(Finding("tampered", f"{label} has an invalid MAC", label))
                counters.tampered += 1
                continue
            intact.add(record.id)
        return intact

    def _resolve_floor(
        self,
        records: Sequence[SealRecord],
        intact: set[int],
        anchor: AnchorData | None,
        seal_keyring: dict[str, Key],
        counters: _Counters,
        findings: list[Finding],
    ) -> int:
        valid = [record for record in records if record.kind == "floor" and record.id in intact]
        previous = -1
        for record in valid:
            assert record.to_id is not None
            if record.to_id <= previous:
                findings.append(
                    Finding(
                        "tampered",
                        f"retirement floor is non-monotonic ({record.to_id} after {previous})",
                        f"floor {record.to_id}",
                    )
                )
                counters.tampered += 1
            previous = max(previous, record.to_id)

        honored: list[SealRecord] = []
        for record in valid:
            if anchor is None or any(
                _record_matches_anchor(record, event) for event in anchor.events
            ):
                honored.append(record)
            else:
                findings.append(
                    Finding(
                        "tampered",
                        f"signed floor through id {record.to_id} has no anchor record",
                        f"floor {record.to_id}",
                    )
                )
                counters.tampered += 1
        return max((record.to_id or 0 for record in honored), default=0)

    def _resolve_activation(
        self,
        records: Sequence[SealRecord],
        intact: set[int],
        anchor: AnchorData | None,
        seal_keyring: dict[str, Key],
        counters: _Counters,
        findings: list[Finding],
    ) -> int | None:
        del seal_keyring  # role validation already happened in _check_records
        valid = [
            record for record in records if record.kind == "activation" and record.id in intact
        ]
        if len(valid) > 1:
            findings.append(
                Finding("tampered", "more than one activation marker is present", "activation")
            )
            counters.tampered += 1
        if not valid:
            if any(record.kind in {"seal", "floor"} for record in records):
                findings.append(
                    Finding(
                        "tampered",
                        "seals or floors exist without a valid activation marker",
                        "activation",
                    )
                )
                counters.tampered += 1
            return None
        marker = valid[0]
        if anchor is not None and not any(
            _record_matches_anchor(marker, event) for event in anchor.events
        ):
            assert marker.sealed_at is not None
            age = (now_utc() - marker.sealed_at).total_seconds()
            if age > self.audit._anchor_max_age:
                findings.append(
                    Finding(
                        "tampered",
                        "activation marker is absent from the anchor after the grace window",
                        "activation",
                    )
                )
                counters.tampered += 1
        return marker.to_id

    def _check_contiguity(
        self,
        covering: Sequence[SealRecord],
        origin: int,
        counters: _Counters,
        findings: list[Finding],
    ) -> None:
        if not covering:
            return
        first = covering[0]
        if first.from_id != origin:
            findings.append(
                Finding(
                    "tampered",
                    f"covering seals start at {first.from_id}, expected {origin}",
                    f"sealed range ({first.from_id}, {first.to_id}]",
                )
            )
            counters.tampered += 1
        for previous, current in pairwise(covering):
            if current.from_id != previous.to_id:
                findings.append(
                    Finding(
                        "tampered",
                        f"sealed ranges are not contiguous ({previous.to_id} != {current.from_id})",
                        f"sealed range ({current.from_id}, {current.to_id}]",
                    )
                )
                counters.tampered += 1

    def _select_ranges(
        self, covering: Sequence[SealRecord], *, full: bool, now: datetime
    ) -> list[SealRecord]:
        if full or len(covering) <= 1:
            return list(covering)
        n_ranges = len(covering)
        per_run = max(1, math.ceil(n_ranges / max(1, self.audit.verify_cycle)))
        days_since_epoch = (now.date() - date(1970, 1, 1)).days
        start = days_since_epoch % n_ranges
        selected = {
            covering[(start + offset) % n_ranges].id: covering[(start + offset) % n_ranges]
            for offset in range(per_run)
        }
        selected[covering[-1].id] = covering[-1]
        return sorted(selected.values(), key=lambda record: (record.from_id or -1, record.id))

    def _verify_one_range(
        self,
        conn: Connection,
        record: SealRecord,
        boundary: int | None,
        keyring: dict[str, Key],
        seal_keyring: dict[str, Key],
        counters: _Counters,
        findings: list[Finding],
    ) -> None:
        verdict = classify_range(
            conn,
            record,
            boundary,
            keyring,
            seal_keyring,
            on_row=lambda row, row_verdict: self._record_row_verdict(
                row, row_verdict, counters, findings
            ),
        )
        if verdict == "tampered":
            findings.append(
                Finding(
                    "tampered",
                    f"rows no longer exactly reproduce sealed range ({record.from_id}, "
                    f"{record.to_id}]",
                    f"sealed range ({record.from_id}, {record.to_id}]",
                )
            )
            counters.tampered += 1

    def _verify_legacy_prefix(
        self,
        conn: Connection,
        floor: int,
        boundary: int | None,
        keyring: dict[str, Key],
        counters: _Counters,
        findings: list[Finding],
    ) -> None:
        if boundary is None or boundary <= floor:
            return
        for row in _iter_rows(conn, floor, boundary):
            self._record_row_verdict(row, _row_verdict(row, boundary, keyring), counters, findings)

    def _verify_tail(
        self,
        conn: Connection,
        max_covered: int,
        boundary: int | None,
        keyring: dict[str, Key],
        counters: _Counters,
        findings: list[Finding],
    ) -> tuple[int, datetime | None]:
        count = 0
        oldest: datetime | None = None
        for row in _iter_rows(conn, max_covered, None):
            count += 1
            if oldest is None or row.created_at < oldest:
                oldest = row.created_at
            self._record_row_verdict(row, _row_verdict(row, boundary, keyring), counters, findings)
        return count, oldest

    def _record_row_verdict(
        self,
        row: Any,
        verdict: str,
        counters: _Counters,
        findings: list[Finding],
    ) -> None:
        if verdict == "ok":
            counters.ok += 1
        elif verdict == "unprotected":
            counters.unprotected += 1
        elif row.row_mac is None:
            findings.append(
                Finding(
                    "tampered",
                    "unsigned record exists above the activation boundary",
                    f"#{row.id} {row.action}",
                    id=row.id,
                )
            )
            counters.tampered += 1
        else:
            findings.append(
                Finding(
                    "tampered",
                    "modified after signing (row signature no longer matches)",
                    f"#{row.id} {row.action}",
                    id=row.id,
                )
            )
            counters.tampered += 1

    def _check_pruned_region_empty(
        self,
        conn: Connection,
        floor: int,
        counters: _Counters,
        findings: list[Finding],
    ) -> None:
        if floor <= 0:
            return
        rows = conn.execute(
            select(_audits.c.id).where(_audits.c.id <= floor).order_by(_audits.c.id).limit(5)
        ).all()
        if rows:
            ids = ", ".join(str(row.id) for row in rows)
            findings.append(
                Finding(
                    "tampered",
                    f"row(s) {ids} are present at or below retirement floor {floor}",
                    f"rows <= {floor}",
                )
            )
            counters.tampered += 1

    def _check_duplicates(
        self, conn: Connection, counters: _Counters, findings: list[Finding]
    ) -> None:
        rows = conn.execute(
            select(_audits.c.entry_id)
            .where(_audits.c.entry_id.is_not(None))
            .group_by(_audits.c.entry_id)
            .having(func.count() > 1)
            .limit(_MAX_FINDINGS)
        ).all()
        for row in rows:
            findings.append(
                Finding(
                    "tampered",
                    f"entry_id {row.entry_id!r} appears more than once (replay)",
                    f"entry_id {row.entry_id}",
                )
            )
            counters.tampered += 1

    def _check_tail_liveness(
        self,
        active: bool,
        oldest: datetime | None,
        now: datetime,
        counters: _Counters,
        findings: list[Finding],
    ) -> None:
        if not active or oldest is None:
            return
        age = (now - oldest).total_seconds()
        if age > self.audit._unsealed_tail_max_age:
            findings.append(
                Finding(
                    "warning",
                    f"the oldest unsealed row is {int(age)}s old; the sealer looks stalled",
                    "unsealed-tail",
                )
            )
            counters.warning += 1

    def _anchor_marker_valid(self, event: AnchorEvent, seal_keyring: dict[str, Key]) -> bool:
        if event.kind == "floor":
            return any(
                hmac.compare_digest(
                    integrity.floor_mac(
                        key, through_id=event.to_id, retired_at=event.at, key_id=key.id
                    ),
                    event.mac,
                )
                for key in seal_keyring.values()
            )
        if event.kind == "activation":
            return any(
                hmac.compare_digest(
                    integrity.activation_mac(
                        key, boundary_id=event.to_id, at=event.at, key_id=key.id
                    ),
                    event.mac,
                )
                for key in seal_keyring.values()
            )
        return True

    def _check_anchor(
        self,
        anchor: AnchorData | None,
        records: Sequence[SealRecord],
        intact: set[int],
        floor: int,
        seal_keyring: dict[str, Key],
        now: datetime,
        counters: _Counters,
        findings: list[Finding],
    ) -> tuple[datetime | None, bool]:
        if anchor is None:
            return None, False
        intact_records = [record for record in records if record.id in intact]
        partial, corrupt = _classify_anchor_issues(anchor, intact_records, retired_through=floor)
        for issue in partial:
            findings.append(
                Finding(
                    "warning",
                    f"{issue.message} (partial anchor append; a sealer run will heal it)",
                    "anchor",
                )
            )
            counters.warning += 1
        for issue in corrupt:
            findings.append(Finding("tampered", issue.message, "anchor"))
            counters.tampered += 1
        if not anchor.exists or not anchor.events:
            if records:
                findings.append(
                    Finding(
                        "warning",
                        "anchor file is missing or empty while tamper-evidence records exist",
                        "anchor",
                    )
                )
                counters.warning += 1
            return None, False

        for event in anchor.events:
            matches = [record for record in records if _record_matches_anchor(record, event)]
            if event.kind == "seal":
                if event.to_id <= floor:
                    continue
                if not any(record.id in intact for record in matches):
                    findings.append(
                        Finding(
                            "tampered",
                            f"anchored seal ({event.from_id}, {event.to_id}] is missing or invalid",
                            f"sealed range ({event.from_id}, {event.to_id}]",
                        )
                    )
                    counters.tampered += 1
            else:
                if not self._anchor_marker_valid(event, seal_keyring):
                    findings.append(
                        Finding(
                            "tampered",
                            f"anchored {event.kind} line has an invalid MAC",
                            event.kind,
                        )
                    )
                    counters.tampered += 1
                elif not any(record.id in intact for record in matches):
                    if event.kind == "floor" and floor >= event.to_id:
                        continue
                    pending_seals = [
                        candidate
                        for candidate in anchor.events
                        if candidate.kind == "seal" and floor < candidate.to_id <= event.to_id
                    ]
                    crashed_prune = event.kind == "floor" and all(
                        any(
                            record.id in intact and _record_matches_anchor(record, candidate)
                            for record in records
                        )
                        for candidate in pending_seals
                    )
                    if crashed_prune:
                        findings.append(
                            Finding(
                                "warning",
                                "a recorded floor advance never committed — crashed prune; "
                                "the next successful prune supersedes it",
                                f"floor {event.to_id}",
                            )
                        )
                        counters.warning += 1
                    else:
                        findings.append(
                            Finding(
                                "tampered",
                                f"anchored {event.kind} record is missing from the database",
                                event.kind,
                            )
                        )
                        counters.tampered += 1

        for record in records:
            if record.id not in intact:
                continue
            anchored = any(_record_matches_anchor(record, event) for event in anchor.events)
            if anchored:
                continue
            assert record.sealed_at is not None
            age = (now - record.sealed_at).total_seconds()
            if record.kind == "floor" or age > self.audit._anchor_max_age:
                findings.append(
                    Finding(
                        "tampered",
                        f"present {record.kind} record is absent from the anchor",
                        f"{record.kind} {record.to_id}",
                    )
                )
                counters.tampered += 1

        newest = max(event.at for event in anchor.events)
        force_nonzero = False
        age = (now - newest).total_seconds()
        if age > self.audit._anchor_max_age:
            findings.append(
                Finding(
                    "warning",
                    f"the newest anchor is {int(age)}s old; the anchor sink looks stalled",
                    "anchor",
                )
            )
            counters.warning += 1
            force_nonzero = True
        return newest, force_nonzero

    def _build_report(
        self,
        *,
        counters: _Counters,
        findings: list[Finding],
        full: bool,
        prior_full_coverage: datetime | None,
        now: datetime,
        tail_count: int,
        tail_oldest: datetime | None,
        newest_anchor_at: datetime | None,
        anchor_configured: bool,
        force_nonzero: bool,
    ) -> VerifyReport:
        outcome = "tampered" if counters.tampered else "warning" if counters.warning else "ok"
        return VerifyReport(
            outcome=outcome,
            exit_code=1 if counters.tampered or force_nonzero else 0,
            findings=findings,
            ok_count=counters.ok,
            warning_count=counters.warning,
            unprotected_count=counters.unprotected,
            tampered_count=counters.tampered,
            last_full_coverage_at=now if full else prior_full_coverage,
            newest_anchor_at=newest_anchor_at,
            anchor_configured=anchor_configured,
            unsealed_tail_count=tail_count,
            unsealed_tail_oldest_at=tail_oldest,
            affected_identifiers=_affected_json(findings, counters.tampered),
        )

    def _emit_finding(self, report: VerifyReport) -> None:
        if report.outcome not in {"tampered", "warning"}:
            return
        severity = "critical" if report.outcome == "tampered" else "warning"
        wanted = "tampered" if severity == "critical" else "warning"
        affected = tuple(
            finding.identifier
            for finding in report.findings
            if finding.verdict == wanted and finding.identifier
        )[:_MAX_AFFECTED]
        alert = IntegrityAlert(
            severity=severity,
            outcome=report.outcome,
            ran_at=now_utc(),
            ok_count=report.ok_count,
            warning_count=report.warning_count,
            unprotected_count=report.unprotected_count,
            tampered_count=report.tampered_count,
            affected=affected,
        )
        try:
            self.audit.on_finding(alert)
        except Exception as exc:
            self.audit.on_error(exc)

    def _persist(self, report: VerifyReport) -> None:
        self._upsert_status(
            ran_at=now_utc(),
            outcome=report.outcome,
            ok_count=report.ok_count,
            warning_count=report.warning_count,
            unprotected_count=report.unprotected_count,
            tampered_count=report.tampered_count,
            error_message=report.error_message,
            last_full_coverage_at=report.last_full_coverage_at,
            newest_anchor_at=report.newest_anchor_at,
            anchor_configured=report.anchor_configured,
            unsealed_tail_count=report.unsealed_tail_count,
            unsealed_tail_oldest_at=report.unsealed_tail_oldest_at,
            affected_identifiers=report.affected_identifiers,
            duration_seconds=report.duration_seconds,
        )

    def _persist_error(self, message: str) -> None:
        try:
            self._upsert_status(
                ran_at=now_utc(),
                outcome="error",
                ok_count=0,
                warning_count=0,
                unprotected_count=0,
                tampered_count=0,
                error_message=message,
                last_full_coverage_at=None,
                newest_anchor_at=None,
                anchor_configured=False,
                unsealed_tail_count=0,
                unsealed_tail_oldest_at=None,
                affected_identifiers=None,
                duration_seconds=0.0,
            )
        except Exception as exc:
            self.audit.on_error(exc)

    def _upsert_status(self, **values: Any) -> None:
        dialect = get_dialect(self.audit.engine)
        payload = {"id": _STATUS_ID, **values}
        stmt = dialect.upsert(
            _status,
            payload,
            index_elements=["id"],
            update_columns=[column for column in payload if column != "id"],
        )
        with dialect.begin_claim_tx(self.audit.engine) as conn:
            conn.execute(stmt)
