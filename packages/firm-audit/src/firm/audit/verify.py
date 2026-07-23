"""Verify independent seals, the retirement floor, activation, rows, and anchor watermarks.

Every database read in a run shares one snapshot. Present seals authenticate themselves and their
exact rows; valid floor advances authorize an empty pruned prefix; one signed activation marker
separates legacy rows from the protected region. The append-only anchor remembers records the
database may no longer contain. The anchor is streamed once into monotonic coverage/floor
watermarks, so its size never controls verification memory or correctness.

Default runs always check markers, seal MACs, contiguity, the legacy prefix, the unsealed tail,
duplicates, anchor watermarks, the newest range, and a stateless date-derived slice of older
ranges. Only ``full=True`` recomputes every sealed range. Attacker-controlled database fields are
parsed defensively as TAMPERED; malformed anchor lines cannot lower a monotonic maximum, so they
are skipped and collapsed into one WARNING. Unknown row ``key_id`` values remain a verification
error only when no tampering was found; an unavailable Layer-2 signer is itself TAMPERED.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING, Any

from sqlalchemy import Connection, String, case, cast, func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from .._core.clock import now_utc
from .._core.database import snapshot_transaction
from .._core.dialects import get_dialect
from . import integrity, schema
from .integrity import HmacSigner, Key, parse_keyring

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
_MAX_SEAL_RECORDS = 100_000
_MAX_ANCHOR_LINE_CHARS = 4096
_MAX_SCALAR_CHARS = 255
_MAX_JSON_CHARS = 65_535
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
class AnchorData:
    """O(1)-memory summary of one streamed anchor."""

    exists: bool
    coverage_watermark: int = 0
    floor_watermark: int = 0
    newest_at: datetime | None = None
    unreadable_lines: int = 0


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


class SealRecords(list[SealRecord]):
    """A bounded, keyset-paged side-table scan."""

    capped: bool = False


def load_seal_records(conn: Connection) -> SealRecords:
    """Load side-table rows without invoking dialect datetime/integer result processors.

    Casting attacker-controlled typed fields to text lets verification classify malformed values
    instead of raising while SQLAlchemy decodes them.
    """

    def bounded(column: Any, chars: int, label: str) -> Any:
        return func.substr(cast(column, String), 1, chars + 1).label(label)

    columns = (
        _seals.c.id,
        bounded(_seals.c.kind, 32, "kind"),
        bounded(_seals.c.from_id, 64, "from_id"),
        bounded(_seals.c.to_id, 64, "to_id"),
        bounded(_seals.c.row_count, 64, "row_count"),
        bounded(_seals.c.rows_mac, 64, "rows_mac"),
        bounded(_seals.c.seal_mac, 64, "seal_mac"),
        bounded(_seals.c.sealed_at, 64, "sealed_at"),
        bounded(_seals.c.key_id, 16, "key_id"),
    )
    records = SealRecords()
    last_id = 0
    while len(records) < _MAX_SEAL_RECORDS:
        rows = conn.execute(
            select(*columns).where(_seals.c.id > last_id).order_by(_seals.c.id).limit(_PAGE)
        ).all()
        if not rows:
            break
        for row in rows:
            if len(records) >= _MAX_SEAL_RECORDS:
                records.capped = True
                break
            records.append(_parse_seal_record(row))
        last_id = rows[-1].id
        if len(rows) < _PAGE:
            break
    if len(records) == _MAX_SEAL_RECORDS:
        records.capped = (
            conn.execute(select(_seals.c.id).where(_seals.c.id > last_id).limit(1)).first()
            is not None
        )
    return records


def _parse_seal_record(row: Any) -> SealRecord:
    """Defensively parse one raw side-table row."""
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
    return SealRecord(
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


def _iter_rows(conn: Connection, low: int, high: int | None) -> Iterator[Any]:
    """Yield bounded event projections in id order with keyset pagination."""
    text_limits = {
        "action": _MAX_SCALAR_CHARS,
        "subject_type": _MAX_SCALAR_CHARS,
        "subject_id": _MAX_SCALAR_CHARS,
        "subject_label": _MAX_SCALAR_CHARS,
        "actor_type": _MAX_SCALAR_CHARS,
        "actor_id": _MAX_SCALAR_CHARS,
        "actor_label": _MAX_SCALAR_CHARS,
        "correlation_id": _MAX_SCALAR_CHARS,
        "data": _MAX_JSON_CHARS,
        "changes": _MAX_JSON_CHARS,
        "context": _MAX_JSON_CHARS,
        "entry_id": 26,
        "row_mac": 64,
        "key_id": 16,
    }
    oversized = or_(
        *(func.length(_audits.c[name]) > limit for name, limit in text_limits.items()),
        func.length(cast(_audits.c.created_at, String)) > 64,
    )
    columns = [
        _audits.c.id,
        *(
            func.substr(_audits.c[name], 1, limit + 1).label(name)
            for name, limit in text_limits.items()
            if name not in {"entry_id", "row_mac", "key_id"}
        ),
        func.substr(cast(_audits.c.created_at, String), 1, 65).label("created_at"),
        func.substr(_audits.c.entry_id, 1, 27).label("entry_id"),
        func.substr(_audits.c.row_mac, 1, 65).label("row_mac"),
        func.substr(_audits.c.key_id, 1, 17).label("key_id"),
        case((oversized, True), else_=False).label("oversized"),
    ]
    last = low
    while True:
        stmt = select(*columns).where(_audits.c.id > last)
        if high is not None:
            stmt = stmt.where(_audits.c.id <= high)
        rows = conn.execute(stmt.order_by(_audits.c.id).limit(_PAGE)).all()
        if not rows:
            return
        yield from rows
        if len(rows) < _PAGE:
            return
        last = rows[-1].id


def _row_mac_input(row: Any) -> bytes:
    """Return the canonical row message from bounded database values."""
    created_at = datetime.fromisoformat(row.created_at)
    return integrity.row_mac_input(
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
        created_at=created_at,
    )


def _row_verdict(row: Any, boundary: int | None, keyring: dict[str, Key]) -> str:
    if row.oversized:
        return "tampered"
    if row.row_mac is None:
        if boundary is None or row.id <= boundary:
            return "unprotected"
        return "tampered"
    key = keyring.get(row.key_id)
    if key is None:
        return "unresolved"
    try:
        return "ok" if HmacSigner(key).verify(_row_mac_input(row), row.row_mac) else "tampered"
    except (TypeError, ValueError, UnicodeError):
        return "tampered"


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
    unresolved = False

    def pairs() -> Iterator[tuple[int, str]]:
        nonlocal count, rows_ok, unresolved
        for row in _iter_rows(conn, from_id, to_id):
            count += 1
            verdict = _row_verdict(row, boundary, keyring)
            if on_row is not None:
                on_row(row, verdict)
            if verdict == "unresolved":
                unresolved = True
            elif verdict != "ok" or row.row_mac is None:
                rows_ok = False
            yield row.id, row.row_mac or ""

    aggregate = integrity.rows_mac(key, pairs())
    if rows_ok and count == seal.row_count and HmacSigner.tags_match(aggregate, seal.rows_mac):
        return "unresolved" if unresolved else "ok"
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


def _record_mac_input(record: SealRecord) -> bytes:
    """Return one side-table record's canonical kind-specific message."""
    assert record.to_id is not None
    assert record.sealed_at is not None
    assert record.key_id is not None
    if record.kind == "seal":
        assert record.from_id is not None
        assert record.row_count is not None
        assert record.rows_mac is not None
        return integrity.seal_mac_input(
            from_id=record.from_id,
            to_id=record.to_id,
            row_count=record.row_count,
            rows_mac=record.rows_mac,
            sealed_at=record.sealed_at,
            key_id=record.key_id,
        )
    if record.kind == "floor":
        return integrity.floor_mac_input(
            through_id=record.to_id,
            retired_at=record.sealed_at,
            key_id=record.key_id,
        )
    return integrity.activation_mac_input(
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
    return HmacSigner(key).verify(_record_mac_input(record), record.seal_mac)


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


def _format_checkpoint(*, at: datetime, coverage_id: int, floor_id: int, mac: str) -> str:
    return f"{integrity.canonical_created_at(at)} CHECKPOINT {coverage_id} {floor_id} {mac}"


def _read_anchor(
    path: str,
    *,
    coverage_cutoff: datetime,
    seal_keyring: dict[str, Key],
) -> AnchorData:
    """Stream one anchor into monotonic watermarks without retaining per-line state."""
    try:
        with open(path, encoding="utf-8") as handle:
            return _parse_anchor_stream(
                _bounded_anchor_lines(handle),
                coverage_cutoff=coverage_cutoff,
                seal_keyring=seal_keyring,
            )
    except FileNotFoundError:
        return AnchorData(False)
    except (OSError, UnicodeError):
        return AnchorData(True, unreadable_lines=1)


def _parse_anchor_stream(
    lines: Iterator[str],
    *,
    coverage_cutoff: datetime,
    seal_keyring: dict[str, Key],
) -> AnchorData:
    """Parse SEAL/FLOOR/CHECKPOINT maxima in O(1) memory; skip unreadable lines."""
    coverage = 0
    floor = 0
    newest: datetime | None = None
    unreadable = 0
    signers = tuple(HmacSigner(key) for key in seal_keyring.values())

    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if not line.strip():
            continue
        parts = line.split()
        try:
            at = datetime.fromisoformat(parts[0])
            kind = parts[1]
            if kind == "SEAL" and len(parts) == 5:
                from_id = int(parts[2])
                to_id = int(parts[3])
                if from_id < 0 or from_id >= to_id:
                    raise ValueError
                if at <= coverage_cutoff:
                    coverage = max(coverage, to_id)
            elif kind == "FLOOR" and len(parts) == 4:
                to_id = int(parts[2])
                if to_id < 0 or not any(
                    signer.verify(
                        integrity.floor_mac_input(
                            through_id=to_id,
                            retired_at=at,
                            key_id=signer.key_id,
                        ),
                        parts[3],
                    )
                    for signer in signers
                ):
                    raise ValueError
                floor = max(floor, to_id)
            elif kind == "ACTIVATION" and len(parts) == 4:
                if int(parts[2]) < 0:
                    raise ValueError
            elif kind == "CHECKPOINT" and len(parts) == 5:
                checkpoint_coverage = int(parts[2])
                checkpoint_floor = int(parts[3])
                if (
                    checkpoint_coverage < 0
                    or checkpoint_floor < 0
                    or not any(
                        signer.verify(
                            integrity.checkpoint_mac_input(
                                coverage_id=checkpoint_coverage,
                                floor_id=checkpoint_floor,
                                at=at,
                                key_id=signer.key_id,
                            ),
                            parts[4],
                        )
                        for signer in signers
                    )
                ):
                    raise ValueError
                if at <= coverage_cutoff:
                    coverage = max(coverage, checkpoint_coverage)
                floor = max(floor, checkpoint_floor)
            else:
                raise ValueError
        except (IndexError, ValueError):
            unreadable += 1
            continue
        if newest is None or at > newest:
            newest = at
    return AnchorData(True, coverage, floor, newest, unreadable)


def _bounded_anchor_lines(handle: Any) -> Iterator[str]:
    """Yield physical lines without ever materializing an attacker-sized line."""
    while True:
        chunk = handle.readline(_MAX_ANCHOR_LINE_CHARS + 1)
        if not chunk:
            return
        if len(chunk) <= _MAX_ANCHOR_LINE_CHARS or chunk.endswith(("\n", "\r")):
            yield chunk
            continue
        while chunk and not chunk.endswith(("\n", "\r")):
            chunk = handle.readline(_MAX_ANCHOR_LINE_CHARS + 1)
        yield "anchor-line-exceeded-size-cap\n"


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
class _UnresolvedRows:
    """Bounded unknown-key identifiers plus the number omitted."""

    messages: dict[int, str] = field(default_factory=dict)
    overflow: int = 0

    def add(self, row_id: int, message: str) -> bool:
        if row_id in self.messages:
            return False
        if len(self.messages) < _MAX_FINDINGS:
            self.messages[row_id] = message
        else:
            self.overflow += 1
        return True

    def first_error(self) -> str | None:
        if not self.messages:
            return None
        message = next(iter(self.messages.values()))
        if self.overflow:
            return f"{message} (+{self.overflow} more unresolved rows)"
        return message


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
    sealing_observed: bool = False
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
            self.audit.on_error(exc)
            raise
        except (MemoryError, SQLAlchemyError, TypeError, ValueError, UnicodeError) as exc:
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
        try:
            self._persist(report)
        except Exception as exc:
            self.audit.on_error(exc)
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
        unresolved_rows = _UnresolvedRows()

        with snapshot_transaction(self.audit.engine) as conn:
            # The first SQL read acquires the PG/MySQL snapshot. Read the external anchor only
            # afterward so both verify and retention compare it with an already-fixed DB view.
            records = load_seal_records(conn)
            anchor = (
                _read_anchor(
                    anchor_path,
                    coverage_cutoff=now - timedelta(seconds=self.audit.grace),
                    seal_keyring=seal_keyring,
                )
                if anchor_path is not None
                else None
            )
            prior = conn.execute(
                select(
                    _status.c.last_full_coverage_at,
                    _status.c.sealing_observed,
                ).where(_status.c.id == _STATUS_ID)
            ).first()
            if records.capped:
                findings.append(
                    Finding(
                        "warning",
                        f"seal-record scan reached its {_MAX_SEAL_RECORDS}-record safety cap",
                        "audit-integrity-storage",
                    )
                )
                counters.warning += 1
            intact = self._check_records(records, keyring, seal_keyring, counters, findings)
            floor = self._resolve_floor(records, intact, anchor, counters, findings)
            boundary = self._resolve_activation(records, intact, counters, findings)
            sealing_observed = bool(prior and prior.sealing_observed) or any(
                record.id in intact and record.kind in {"activation", "seal"} for record in records
            )
            self._check_missing_activation_guard(
                conn,
                records,
                anchor_configured=anchor is not None,
                sealing_observed=sealing_observed,
                counters=counters,
                findings=findings,
            )

            self._check_pruned_region_empty(conn, floor, counters, findings)
            self._check_duplicates(conn, counters, findings)
            self._verify_legacy_prefix(
                conn, floor, boundary, keyring, unresolved_rows, counters, findings
            )

            covering = [
                record
                for record in records
                if record.kind == "seal"
                and record.id in intact
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
                    unresolved_rows,
                    counters,
                    findings,
                )

            max_covered = max(
                [origin, *(record.to_id or origin for record in covering)], default=origin
            )
            tail_count, tail_oldest = self._verify_tail(
                conn, max_covered, boundary, keyring, unresolved_rows, counters, findings
            )
            self._check_tail_liveness(
                any(record.kind == "activation" for record in records),
                tail_oldest,
                now,
                counters,
                findings,
            )
            newest_anchor_at, anchor_force_nonzero = self._check_anchor(
                anchor,
                records,
                intact,
                floor,
                now,
                counters,
                findings,
            )
            unresolved_error = unresolved_rows.first_error()
            if unresolved_error is not None and not counters.tampered:
                raise VerifyError(unresolved_error)

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
            sealing_observed=sealing_observed,
            force_nonzero=anchor_force_nonzero,
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
                role = (
                    "a row key that is not a seal key"
                    if record.key_id in keyring
                    else "an unavailable seal key"
                )
                findings.append(
                    Finding(
                        "tampered",
                        f"{label} has an unverifiable seal signer: key_id "
                        f"{record.key_id!r} is {role}",
                        label,
                    )
                )
                counters.tampered += 1
                continue
            assert record.seal_mac is not None
            if not HmacSigner(key).verify(_record_mac_input(record), record.seal_mac):
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

        database_floor = max((record.to_id or 0 for record in valid), default=0)
        anchor_floor = anchor.floor_watermark if anchor is not None else 0
        return max(database_floor, anchor_floor)

    def _resolve_activation(
        self,
        records: Sequence[SealRecord],
        intact: set[int],
        counters: _Counters,
        findings: list[Finding],
    ) -> int | None:
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
        return marker.to_id

    def _check_missing_activation_guard(
        self,
        conn: Connection,
        records: Sequence[SealRecord],
        *,
        anchor_configured: bool,
        sealing_observed: bool,
        counters: _Counters,
        findings: list[Finding],
    ) -> None:
        """Use persisted no-anchor sealing memory without guessing from event counts."""
        if (
            anchor_configured
            or self.audit._seal_key is None
            or not sealing_observed
            or any(record.kind in {"activation", "seal"} for record in records)
        ):
            return
        events_present = conn.execute(select(_audits.c.id).limit(1)).first() is not None
        if not events_present:
            return
        findings.append(
            Finding(
                "tampered",
                "audit events exist but the activation and all other tamper-evidence records "
                "are missing after sealing coverage was previously observed",
                "activation",
            )
        )
        counters.tampered += 1

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
        unresolved_rows: _UnresolvedRows,
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
                row, row_verdict, unresolved_rows, counters, findings
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
        unresolved_rows: _UnresolvedRows,
        counters: _Counters,
        findings: list[Finding],
    ) -> None:
        if boundary is None or boundary <= floor:
            return
        for row in _iter_rows(conn, floor, boundary):
            self._record_row_verdict(
                row,
                _row_verdict(row, boundary, keyring),
                unresolved_rows,
                counters,
                findings,
            )

    def _verify_tail(
        self,
        conn: Connection,
        max_covered: int,
        boundary: int | None,
        keyring: dict[str, Key],
        unresolved_rows: _UnresolvedRows,
        counters: _Counters,
        findings: list[Finding],
    ) -> tuple[int, datetime | None]:
        count = 0
        oldest: datetime | None = None
        for row in _iter_rows(conn, max_covered, None):
            count += 1
            try:
                created_at = datetime.fromisoformat(row.created_at)
            except (TypeError, ValueError):
                created_at = None
            if created_at is not None and (oldest is None or created_at < oldest):
                oldest = created_at
            self._record_row_verdict(
                row,
                _row_verdict(row, boundary, keyring),
                unresolved_rows,
                counters,
                findings,
            )
        return count, oldest

    def _record_row_verdict(
        self,
        row: Any,
        verdict: str,
        unresolved_rows: _UnresolvedRows,
        counters: _Counters,
        findings: list[Finding],
    ) -> None:
        if verdict == "ok":
            counters.ok += 1
        elif verdict == "unprotected":
            counters.unprotected += 1
        elif verdict == "unresolved":
            message = (
                f"row {row.id} was signed by unknown key_id {row.key_id!r} — add its secret to "
                "FIRM_AUDIT_RETIRED_KEYS."
            )
            if unresolved_rows.add(row.id, message):
                findings.append(Finding("warning", message, f"#{row.id} {row.action}", id=row.id))
                counters.warning += 1
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

    def _check_anchor(
        self,
        anchor: AnchorData | None,
        records: Sequence[SealRecord],
        intact: set[int],
        floor: int,
        now: datetime,
        counters: _Counters,
        findings: list[Finding],
    ) -> tuple[datetime | None, bool]:
        if anchor is None:
            return None, False
        if anchor.unreadable_lines:
            findings.append(
                Finding(
                    "warning",
                    f"anchor has {anchor.unreadable_lines} unreadable line(s)",
                    "anchor",
                )
            )
            counters.warning += 1
        present_coverage = max(
            [
                floor,
                *(
                    record.to_id or 0
                    for record in records
                    if record.id in intact and record.kind == "seal"
                ),
            ]
        )
        if present_coverage < anchor.coverage_watermark:
            findings.append(
                Finding(
                    "tampered",
                    "database seal coverage ends at "
                    f"{present_coverage}, below anchor watermark {anchor.coverage_watermark}",
                    "anchor-coverage",
                )
            )
            counters.tampered += 1

        force_nonzero = False
        newest = anchor.newest_at
        if not anchor.exists or newest is None:
            findings.append(
                Finding(
                    "warning",
                    "anchor file is missing or has no readable timestamp",
                    "anchor",
                )
            )
            counters.warning += 1
            return None, True
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
        sealing_observed: bool,
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
            sealing_observed=sealing_observed,
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
            sealing_observed=report.sealing_observed,
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
                sealing_observed=self._prior_sealing_observed(),
                unsealed_tail_count=0,
                unsealed_tail_oldest_at=None,
                affected_identifiers=None,
                duration_seconds=0.0,
            )
        except Exception as exc:
            self.audit.on_error(exc)

    def _prior_sealing_observed(self) -> bool:
        try:
            with self.audit.engine.connect() as conn:
                value = conn.execute(
                    select(_status.c.sealing_observed).where(_status.c.id == _STATUS_ID)
                ).scalar_one_or_none()
            return bool(value)
        except Exception:
            return False

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
