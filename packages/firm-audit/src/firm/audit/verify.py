"""Verification — read-only checking of Layers 1-3, with the four verdict classes.

Verification recomputes what the writer and sealer signed and compares it to what the database
now holds. It never writes an audit row or a seal; the only thing it persists is a single-row
snapshot of its own outcome in ``firm_audit_verify_status`` (read by the dashboard's integrity
panel). It runs anywhere the key is available, over plain read connections, on every dialect.

Three layers, checked in one pass:

* **Layer 1 — per-row MAC.** Each row's ``row_mac`` is recomputed from the row *as the database
  returned it* (the :mod:`.integrity` round-trip rule) and compared. A mismatch is modification;
  a missing MAC after the activation boundary is a forged insert or config drift; a missing MAC
  at or below the boundary is a legacy row written before the key existed (unprotected, not an
  alarm). Reading is keyset-paginated on ``id`` so memory stays bounded on a large table.

* **Layer 2 — seal chain.** Seals are walked in ``seq`` order: dense (no gaps), ``prev_mac``
  linked, each ``seal_mac`` recomputed. For the ranges verified this run, ``rows_mac`` and
  ``row_count`` are recomputed over the rows actually present — a deletion or forged insert in a
  sealed range shows up here even when Layer 1 alone could not (a *deleted* row leaves nothing to
  check per-row). A retention ``checkpoint`` seal legitimately authorizes the pruning of the
  ranges below it, so the chain is allowed to start above ``seq 1`` when — and only when — a
  key-signed checkpoint vouches for the missing front.

* **Layer 3 — anchor.** With ``--anchor`` the newest exported chain head is compared to the
  stored chain: a chain that no longer contains the anchored seal, or is shorter than it, is
  tail-truncation or a wholesale drop-and-recreate (neither is visible from the database alone).
  An anchor older than ``anchor_max_age`` forces a non-zero exit even absent tampering (design
  review D16): the silently-truncatable window the anchor exists to bound must not grow unwatched.

**Rolling full coverage (design review D12).** Recomputing every sealed row every run is wasteful
on a large log, but a tail-only incremental would never re-read old ranges — an edit in last
week's data would stay invisible. So each default run verifies the unsealed tail, the newest
range, *and a rotating slice of older ranges* sized so every range is recomputed at least once
per ``verify_cycle`` runs. The rotation cursor is **advisory only**: it reorders work, never
suppresses a verdict. The seal chain (cheap, seals-table only) is walked in full every run
regardless, ``--full`` recomputes every range from the genesis/checkpoint floor, and the
always-on checks (chain walk, pruned-region-empty probe, unsealed tail, anchor) do not depend on
the cursor at all.

**Only ``--full`` guarantees coverage of every sealed range.** The rotation cursor is persisted to
an ordinary state file (or held in memory) — *not* under a MAC — so an attacker with write access
to it can **pin** it: rewriting it to the same value before every non-``--full`` run keeps one
chosen older range out of the rotating slice indefinitely, deferring recomputation of an edit in
that range for as long as they keep pinning. The rolling slice is therefore a *freshness
optimization for an honest operator*, not a security guarantee against a cursor-tampering attacker.
What the attacker cannot defer is a periodic ``firm-audit verify --full`` (which ignores the cursor
and recomputes every range) and the always-on checks above. Run ``--full`` on a schedule —
especially when the verify state and the database share a host — and treat the rolling cursor as an
accelerator, never as proof that an old range was recently recomputed. Verify emits a warning
whenever a non-``--full`` run does a partial slice it cannot prove will rotate (see
:meth:`Verifier._select_ranges`).

Verdicts (design review 1A/5A): ``OK`` · ``WARNING`` (a valid-MAC late commit, or a stalled-sealer
liveness signal — never an alarm) · ``UNPROTECTED`` (legacy NULL-MAC rows at/below activation) ·
``TAMPERED`` (anything cryptographically inconsistent after activation). Exit code 0 covers
OK/UNPROTECTED (WARNINGs print but exit 0); TAMPERED and a stale anchor exit non-zero. An unknown
``key_id`` is a hard failure (:class:`VerifyError`) — the outcome is persisted as ``error`` before
it re-raises (design review D24) so a dead verify cron and a real tamper never look alike.
"""

from __future__ import annotations

import hmac
import json
import math
import os
import sys
import time
import warnings
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise
from typing import TYPE_CHECKING, Any

from sqlalchemy import Connection, func, select

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

#: The synthetic ``prev_mac`` of the very first seal — mirrors :data:`.sealing._GENESIS`.
_GENESIS = "genesis"

#: Verify-only archives of *retired* keys, read only here (never by a writer or sealer). They are
#: role-scoped so a retired key can only validate what its role signed: ``RETIRED_KEYS`` holds
#: retired **row** keys (eligible for row-MAC verification only — never a seal), and
#: ``RETIRED_SEAL_KEYS`` holds retired **seal** keys (eligible for seal verification *and*, as the
#: higher-privilege key, row verification). Keeping retired row keys out of the seal ring in every
#: mode is what stops a stolen row key, once rotated out, from becoming a seal-capable key.
_RETIRED_KEYS_ENV = "FIRM_AUDIT_RETIRED_KEYS"
_RETIRED_SEAL_KEYS_ENV = "FIRM_AUDIT_RETIRED_SEAL_KEYS"

#: Rows read per keyset page when recomputing Layer-1 MACs (bounded memory on a large table).
_PAGE = 1000

#: How many tampered findings the status row carries in full detail (the same bound feeds the
#: :class:`IntegrityAlert` line). The exact count and worst verdict already come from the counters;
#: this only caps the *per-finding* detail so a mass-tamper never grows the persisted JSON (or a log
#: line) without bound — a longer run collapses the overflow into one ``kind="more"`` marker.
_MAX_AFFECTED = 20

#: The fixed primary key of the single ``firm_audit_verify_status`` row (the "single-row" contract).
_STATUS_ID = 1

#: Hard cap on how many :class:`Finding` objects verify keeps in memory in one run (Bug #6).
#: The *counters* carry the exact totals (and drive the persisted outcome + the alert), so a mass
#: tamper needs no more than a bounded sample of findings for display — a million tampered rows must
#: not grow this list without bound and OOM verify *before* it persists the red status / fires
#: ``on_finding``. Comfortably above :data:`_MAX_AFFECTED` so the handful of displayed chips are
#: always present; the honest overflow count for the "+N more" marker comes from the exact counter.
_MAX_FINDINGS = 1000


class _Findings(list):  # type: ignore[type-arg]
    """A ``list`` of :class:`Finding` that stops growing past :data:`_MAX_FINDINGS` (Bug #6).

    ``append`` silently drops findings once the cap is reached; the counters (not the list) are the
    source of truth for the exact totals, so dropping the tail of the *sample* is safe. This is the
    single change that keeps a mass-tamper run bounded in memory all the way through to persisting
    the TAMPERED outcome and firing the alert."""

    def append(self, item: Finding) -> None:
        if len(self) < _MAX_FINDINGS:
            super().append(item)


def _iter_rows(conn: Connection, low: int, high: int | None) -> Iterator[Any]:
    """Yield rows with ``low < id`` (and ``id <= high`` when given) in id order, keyset-paginated
    at :data:`_PAGE` so a range or tail of any size stays memory-bounded (7A). Module-level so
    both the verifier and :mod:`.retention`'s pre-prune gate read rows the same way."""
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
    """Recompute one row's Layer-1 MAC from the columns *as the database returned them*.

    The single place the row's canonical field list is spelled out for recomputation, shared by
    the verifier's per-row check and :mod:`.retention`'s pre-prune gate so the two can never
    disagree about what a row's MAC should be (a divergence would masquerade as tampering)."""
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
    """The Layer-1 verdict for one row — the single source of truth for "is this MAC valid".

    ``"ok"`` is a genuine, validly-signed row; ``"unprotected"`` is a legacy NULL-MAC row at or
    below the activation ``boundary`` (written before the key existed — not an alarm);
    ``"tampered"`` is a modified row, or a NULL-MAC row past the boundary. An unknown ``key_id``
    raises :class:`VerifyError` — verify itself cannot check, distinct from a tampering *finding*.
    Pure and module-level so the verifier's per-row check, its per-range classifier, and (through
    :func:`classify_range`) retention's pre-prune gate all decide row validity identically — a
    divergence would masquerade as tampering."""
    if row.row_mac is None:
        if boundary is None or row.id <= boundary:
            return "unprotected"  # legacy row from before the key existed — not an alarm
        return "tampered"
    key = keyring.get(row.key_id)
    if key is None:
        raise VerifyError(
            f"row {row.id} was signed by unknown key_id {row.key_id!r} — add its secret to "
            "FIRM_AUDIT_RETIRED_KEYS (retired row keys) or FIRM_AUDIT_RETIRED_SEAL_KEYS "
            "(retired seal keys)."
        )
    if not hmac.compare_digest(recompute_row_mac(key, row), row.row_mac):
        return "tampered"
    return "ok"


def classify_range(
    conn: Connection,
    seal: Any,
    boundary: int | None,
    keyring: dict[str, Key],
    seal_keyring: dict[str, Key],
) -> tuple[str, list[tuple[Any, str]]]:
    """Classify a sealed range against its seal — the one classifier verify and retention share.

    Recomputes every row's ``row_mac`` from its content (Layer 1) *and* the range's
    ``rows_mac``/``row_count`` (Layer 2), then returns ``(verdict, per_row)`` where ``verdict`` is:

    * ``"ok"`` — the recomputed ``rows_mac`` and ``row_count`` still match the seal exactly;
    * ``"late_commit"`` — the *only* divergence is extra rows that **all** carry valid row MACs
      **and** land in ids the seal recorded as gaps (design 1A): a transaction that outran the grace
      window committed into an already-sealed range. A valid MAC in a *recorded gap* of a sealed
      range is a latecomer, never an attack — verify reports it as an amber WARNING and retention
      treats it as prunable (the extras have all expired since the range is past ``max_age``, so
      deleting them destroys no evidence). Downgrading this to WARNING is deliberate: "false alarms
      train people to ignore real ones";
    * ``"tampered"`` — anything else: a deletion, a count-preserving swap, an invalid/missing MAC
      after the boundary, or an extra row that is *not* a valid-MAC late commit (e.g. a NULL-MAC
      forged insert — ``_row_verdict`` calls it "unprotected" per-row, but once it makes the range's
      count/``rows_mac`` diverge it is a forged insert, not a benign latecomer).

    **The late-commit test proves the sealed set *survived*, not merely that the present rows are
    valid (Bug #1).** The seal signs both ``rows_mac`` (over the covered ``(id, row_mac)`` pairs)
    and ``gap_ranges`` (the ids in ``(from_id, to_id]`` it did *not* cover). A benign late commit
    fills one of those recorded gaps and leaves every covered row in place, so restricting the
    present rows to the non-gap ids still reproduces ``rows_mac``/``row_count`` exactly. A
    delete-and-relocate laundering attack — delete a genuinely covered row, back-fill id-gaps with
    other valid signed rows so the count climbs past ``row_count`` — cannot: the deleted id was a
    *covered* id, so the covered subset no longer reproduces ``rows_mac`` (or is short of
    ``row_count``) → TAMPERED. Without this, "every present row is validly signed and there are more
    of them than were sealed" wrongly read as a late commit and let retention prune the evidence.

    ``per_row`` is ``[(row, row_verdict), …]`` so the verifier can emit granular per-row findings
    without re-reading. The per-row recompute is load-bearing: an attacker who edits a sealed row's
    content but leaves its ``row_mac`` column untouched leaves ``rows_mac`` (which hashes the stored
    MAC strings, not the content) matching — only recomputing each MAC from the content catches it.

    ``keyring`` resolves each row's ``row_mac`` (row key); ``seal_keyring`` resolves the seal's
    ``rows_mac`` (seal key). A seal whose ``key_id`` is not a seal key (a two-key deployment's row
    key, or an unknown key) is unverifiable — :class:`VerifyError`, never a laundered pass."""
    seal_key = seal_keyring.get(seal.key_id)
    if seal_key is None:
        raise VerifyError(
            f"seal seq {seal.seq} was signed by key_id {seal.key_id!r}, which is not available as "
            "a seal key — the range cannot be verified (unknown key, or a two-key row key)."
        )
    per_row: list[tuple[Any, str]] = []
    pairs: list[tuple[int, str | None]] = []
    all_ok = True
    for row in _iter_rows(conn, seal.from_id, seal.to_id):
        verdict = _row_verdict(row, boundary, keyring)
        per_row.append((row, verdict))
        pairs.append((row.id, row.row_mac))
        if verdict != "ok":
            all_ok = False
    if hmac.compare_digest(integrity.rows_mac(seal_key, pairs), seal.rows_mac) and (
        len(pairs) == seal.row_count
    ):
        return "ok", per_row
    # Late commit (Bug #1): every present row is validly signed, there ARE surplus rows, and the
    # rows at the seal's *covered* (non-gap) ids still reproduce the signed ``rows_mac``/count
    # — i.e. no covered row was deleted or swapped; the surplus all sits in ids the seal recorded as
    # gaps. Anything short of that (a covered id missing, a covered row's MAC changed, an extra that
    # is not in a recorded gap, or an invalid/missing MAC) falls through to TAMPERED.
    try:
        gaps = integrity.parse_gaps(seal.gap_ranges)
    except ValueError:
        # A seal whose ``gap_ranges`` is unparseable is TAMPERED, never an uncaught crash (Bug C).
        # ``gap_ranges`` is signed into ``seal_mac``, so a malformed value can only appear after an
        # attacker edited the column without the key — the chain walk already flags the resulting
        # ``seal_mac`` mismatch; classifying the range TAMPERED here (rather than letting
        # :func:`~firm.audit.integrity.parse_gaps` raise out of the whole run) is what keeps verify
        # from propagating the ValueError, skipping persistence, and freezing the dashboard's last
        # status at ``ok`` while the database is tampered.
        return "tampered", per_row
    covered = [(rid, mac) for rid, mac in pairs if not integrity.id_in_gaps(rid, gaps)]
    if (
        all_ok
        and len(pairs) > seal.row_count
        and len(covered) == seal.row_count
        and hmac.compare_digest(integrity.rows_mac(seal_key, covered), seal.rows_mac)
    ):
        return "late_commit", per_row
    return "tampered", per_row


def range_is_prunable(
    conn: Connection,
    seal: Any,
    boundary: int | None,
    keyring: dict[str, Key],
    seal_keyring: dict[str, Key],
) -> bool:
    """Retention's pre-prune gate — the contract behind "retention only prunes what verifies", now
    aligned with verify's classification so the two cannot drift (design "Retention integration").

    Returns ``True`` when the range is safe to prune: it either verifies OK, or its only divergence
    is valid-MAC late commits (:func:`classify_range` ``"late_commit"`` — the extras are all expired
    and pruning them, together with checkpointing over the range, destroys no evidence). Returns
    ``False`` only for a ``"tampered"`` range — a deletion, a count-preserving swap, an
    invalid/missing MAC, or a non-late-commit extra — and for an unverifiable seal (unknown/row-only
    ``key_id`` → :class:`VerifyError`, caught here). Refusing a tampered range is what keeps
    retention from erasing evidence instead of surfacing it: pruning stops there, the checkpoint
    never advances past it, and every later run refuses it again until an operator investigates.

    Refusing *only* what verify would call TAMPERED is the fix for the false refusal a benign late
    commit used to trigger — on real-concurrency backends a writer that outruns ``grace`` genuinely
    lands a valid row in a just-sealed range, and that must not block retention forever.

    "TAMPERED" is judged exactly as verify judges the range's contribution to its outcome: the
    range-level verdict *or* any per-row verdict being ``"tampered"``. The per-row arm is
    load-bearing — an attacker who edits a sealed row's *content* but leaves its ``row_mac`` column
    untouched keeps the seal's ``rows_mac`` (which hashes the stored MAC strings) matching, so the
    range-level verdict is ``"ok"``; only the per-row recompute from content catches the edit, and
    retention must refuse on it just as verify reports it TAMPERED."""
    try:
        verdict, per_row = classify_range(conn, seal, boundary, keyring, seal_keyring)
    except VerifyError:
        return False  # unverifiable (unknown or row-only seal key) — never prunable
    if verdict == "tampered":
        return False
    return not any(row_verdict == "tampered" for _, row_verdict in per_row)


def recompute_seal_mac(key: Key, seal: Any) -> str:
    """Recompute one seal's ``seal_mac`` from its stored fields — the single place the seal's
    canonical field list is spelled out for recomputation, shared by the verifier's chain walk and
    :func:`seal_is_intact` (retention's pre-prune gate) so the two can never disagree about what a
    seal's MAC should be (a divergence would masquerade as tampering)."""
    return integrity.seal_mac(
        key,
        seq=seal.seq,
        kind=seal.kind,
        from_id=seal.from_id,
        to_id=seal.to_id,
        row_count=seal.row_count,
        rows_mac=seal.rows_mac,
        prev_mac=seal.prev_mac,
        sealed_at=seal.sealed_at,
        gaps=seal.gap_ranges or "",
    )


def seal_is_intact(
    seal: Any,
    predecessor: Any | None,
    has_checkpoint: bool,
    seal_keyring: dict[str, Key],
) -> bool:
    """Whether a seal's **own** integrity holds — the seal-level counterpart to
    :func:`classify_range`'s row-level check, used by retention's pre-prune gate (Bug A).

    :func:`classify_range` only proves the *rows* under a seal still reproduce its ``rows_mac`` /
    ``row_count``; it never re-checks the seal's own ``seal_mac`` or its ``prev_mac`` chain linkage.
    So a seal whose ``seal_mac`` was edited (or whose ``prev_mac`` no longer links to its
    predecessor) still classified as prunable — retention would delete its rows and checkpoint over
    it, laundering the tampered seal to OK on the next verify. This gate closes that: the seal's key
    must be a seal key, its ``seal_mac`` must recompute, and its ``prev_mac`` must chain to the
    predecessor (``"genesis"`` for ``seq 1``). A seal with a missing predecessor is intact only when
    a checkpoint authorizes the pruned front — mirroring :meth:`Verifier._walk_chain`, so a range is
    never prunable to retention while its seal reads TAMPERED to verify."""
    key = seal_keyring.get(seal.key_id)
    if key is None:
        return False  # unverifiable seal key (unknown, or a two-key row key) — never intact
    if not hmac.compare_digest(recompute_seal_mac(key, seal), seal.seal_mac):
        return False
    if seal.seq == 1:
        return hmac.compare_digest(seal.prev_mac, _GENESIS)
    if predecessor is not None:
        return hmac.compare_digest(seal.prev_mac, predecessor.seal_mac)
    # No predecessor present: the earlier seals were pruned. Legitimate only when a key-signed
    # checkpoint authorizes the missing front (the chain walk re-verifies that checkpoint's own
    # seal_mac); otherwise it is a front truncation and the seal is not intact.
    return has_checkpoint


class VerifyError(Exception):
    """A verification could not reach a verdict — e.g. a row/seal signed by an unknown ``key_id``.

    Distinct from a TAMPERED *finding*: this is verify itself unable to check, not proof of
    tampering. The run persists ``outcome="error"`` with this message before re-raising (D24).
    """


# --- findings & report -----------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One thing verify noticed, tagged with its verdict class and a human identifier.

    ``verdict`` is ``"warning"``, ``"unprotected"``, or ``"tampered"`` (an ``OK`` row produces no
    finding). ``identifier`` is a display label for the affected row/seal/id-range (``"row 42"``,
    ``"seal 3"``) the CLI prints and the dashboard shows as a chip. ``id`` is the numeric
    ``firm_audit_events`` id when the finding is about *one specific row* — set so the dashboard can
    link the chip into ``/audit/<id>``; ``None`` for seal-level findings (chain gap, count mismatch)
    that name a seal, not a row.
    """

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
    """The outcome of one run — also what gets upserted into ``firm_audit_verify_status``.

    ``outcome`` collapses the counts to the single worst class (``ok`` unless a warning or
    tampering was seen); ``exit_code`` is what ``firm-audit verify`` returns (non-zero for
    tampering or a stale anchor, zero for OK/UNPROTECTED/plain WARNING).
    """

    outcome: str
    exit_code: int
    findings: list[Finding] = field(default_factory=list)
    ok_count: int = 0
    warning_count: int = 0
    unprotected_count: int = 0
    tampered_count: int = 0
    error_message: str | None = None
    last_full_coverage_at: datetime | None = None
    cycle_position: int | None = None
    cycle_length: int | None = None
    newest_anchor_at: datetime | None = None
    anchor_configured: bool = False
    unsealed_tail_count: int = 0
    unsealed_tail_oldest_at: datetime | None = None
    affected_identifiers: str | None = None
    duration_seconds: float = 0.0


@dataclass(frozen=True)
class IntegrityAlert:
    """One high-severity signal, emitted **once per verify run** whose outcome is ``tampered``
    (``severity="critical"``) or ``warning`` (``severity="warning"``) — never for ``ok`` /
    ``unprotected``. It is routed to :class:`~firm.audit.log.AuditLog`'s ``on_finding`` hook so a
    scheduled or in-process verify that *detects* tampering surfaces an event in the operator's log
    pipeline out of the box (the default sink writes one stderr line; a custom sink can forward it
    to Datadog/Loki/JSON logs). A frozen, structured record — it carries counts and human labels,
    never the key or row content, so it is safe to ship off-host.
    """

    severity: str  # "critical" (tampered) | "warning"
    outcome: str
    ran_at: datetime
    ok_count: int
    warning_count: int
    unprotected_count: int
    tampered_count: int
    #: Human labels of the offending findings (``"row 42"``, ``"seal 3"``), bounded to
    #: :data:`_MAX_AFFECTED` so the line/event stays a fixed size on a mass tamper.
    affected: tuple[str, ...]


def default_on_finding(alert: IntegrityAlert) -> None:
    """Last-resort sink for :class:`IntegrityAlert`: the project bans stdlib logging, so a detected
    tamper (or warning) writes **one** concise high-severity line to stderr rather than vanishing —
    mirroring :func:`~firm._core.poller.default_on_error`'s stderr route, but a single line meant to
    land in a stock deployment's logstream. Callers silence or redirect it via
    ``AuditLog(on_finding=...)`` (pass a no-op to mute, a forwarder to route elsewhere)."""
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
    """Serialize the tampered findings the dashboard links from into the ``affected_identifiers``
    JSON list — ``[{"kind","label","id"?,"message","verdict"}, …]`` — which ``_affected_cells`` (in
    :mod:`firm.ui.render`) parses into linked chips + the per-finding "what/why". ``kind`` is
    ``"row"`` for a row-level finding (it carries a numeric ``id`` the chip links to) or ``"seal"``
    for a seal/chain-level one; the ``label`` is the display identity (e.g. ``"#3 invoice.paid"`` /
    ``"sealed range #1"``). Bounded to :data:`_MAX_AFFECTED`; a longer run appends one
    ``kind="more"`` marker naming the overflow. ``None`` when nothing tampered, so a clean run
    leaves the column NULL exactly as before.

    ``total_tampered`` is the **exact** tampered counter — the ``findings`` list itself is capped at
    :data:`_MAX_FINDINGS` (Bug #6), so the overflow marker is computed from the counter, not from
    ``len(findings)``, to stay honest under a mass tamper that dropped most of its sample."""
    shown = [f for f in findings if f.verdict == "tampered" and f.identifier][:_MAX_AFFECTED]
    if not shown:
        return None
    items: list[dict[str, Any]] = []
    for f in shown:
        assert f.identifier is not None  # guarded by the comprehension above
        item: dict[str, Any] = {
            "kind": "row" if f.id is not None else "seal",
            "label": f.identifier,
            "message": f.message,
            "verdict": f.verdict,
        }
        if f.id is not None:
            item["id"] = f.id
        items.append(item)
    overflow = total_tampered - len(items)
    if overflow > 0:
        items.append(
            {"kind": "more", "label": f"+{overflow} more finding(s)", "verdict": "tampered"}
        )
    return json.dumps(items, separators=(",", ":"))


# --- anchor file -----------------------------------------------------------------------------


def _read_newest_anchor(path: str) -> tuple[int, str, datetime] | None:
    """Parse the newest parseable ``"<sealed_at> <seq> <to_id> <seal_mac>"`` line, or ``None`` if
    the file is missing/empty/unparseable. ``sealed_at`` is the
    :func:`~.integrity.canonical_created_at` ISO string (no embedded spaces), so a whitespace split
    yields the fields positionally. ``to_id`` is skipped here (the newest line drives the
    seq/staleness checks); the maximum coverage across *all* lines is read separately by
    :func:`_read_anchor_max_to_id` for the truncation guard (Bug B).

    A malformed or truncated line is skipped, not raised on (Bug C): the anchor is an append-only
    external file whose last append is best-effort, so a partial final line — or a legacy 3-field
    ``"<sealed_at> <seq> <seal_mac>"`` line from before the ``to_id`` column — must never crash
    verify and freeze its status. Legacy 3-field lines still carry a valid seq/seal_mac/sealed_at,
    so they are read (``seal_mac`` from the last field) rather than discarded."""
    try:
        with open(path, encoding="utf-8") as handle:
            lines = [line for line in handle.read().splitlines() if line.strip()]
    except FileNotFoundError:
        return None
    for line in reversed(lines):
        parts = line.split()
        if len(parts) < 3:  # need at least sealed_at, seq, seal_mac
            continue
        try:
            seq = int(parts[1])
            sealed_at = datetime.fromisoformat(parts[0])
        except ValueError:
            continue
        seal_mac = parts[3] if len(parts) >= 4 else parts[2]  # 4-field new vs 3-field legacy
        return seq, seal_mac, sealed_at
    return None


def _read_anchor_max_to_id(path: str) -> int:
    """The maximum ``to_id`` (coverage) across **every** anchor line — the highest id the anchor
    ever recorded as sealed. ``0`` when the file is missing/empty or holds no parseable coverage.

    This is the external memory Bug B needs: an attacker who deletes a real seal *and* its rows
    leaves nothing in the database that ids above the checkpoint floor were ever sealed, but each
    seal's ``to_id`` was exported to the anchor when it was written. Comparing this high-water mark
    to the maximum coverage still present in the chain surfaces a truncated range (see
    :meth:`Verifier._check_anchor`). A malformed line is skipped rather than raised on — the anchor
    is an append-only external file, and one corrupt line must not crash verify (Bug C spirit)."""
    try:
        with open(path, encoding="utf-8") as handle:
            lines = [line for line in handle.read().splitlines() if line.strip()]
    except FileNotFoundError:
        return 0
    best = 0
    for line in lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            best = max(best, int(parts[2]))
        except ValueError:
            continue
    return best


# --- the verifier ----------------------------------------------------------------------------


class Verifier:
    """Runs :meth:`run` against the owning :class:`~firm.audit.log.AuditLog`'s engine and config.

    Reads its keys, ``anchor_max_age``, ``verify_cycle``, and rolling state from the
    :class:`~firm.audit.log.AuditLog`, exactly as :class:`~firm.audit.sealing.Sealer` and
    :class:`~firm.audit.retention.Retention` read theirs — so ``AuditLog.verify()`` and
    ``firm-audit verify`` share one implementation.
    """

    def __init__(self, audit: AuditLog) -> None:
        self.audit = audit
        # Advisory rotation cursor (design D12): persisted to ``verify_state_path`` when set, else
        # kept in memory for the life of this object. Never trusted for a verdict.
        self._cursor = self._load_cursor()

    @property
    def keyring(self) -> dict[str, Key]:
        """All keys a *row* MAC may be checked against, indexed by ``key_id``: the current row key,
        the current seal key (a two-key deployment configures both; the seal key stays row-eligible
        so a history that *migrated* single→split still verifies the rows the now-seal key signed),
        and both retired archives — retired row keys (``FIRM_AUDIT_RETIRED_KEYS``) and retired seal
        keys (``FIRM_AUDIT_RETIRED_SEAL_KEYS``, higher-privilege, so row-eligible too). Empty when
        the feature is off. In single-key mode the seal key *is* the row key, so the current-key
        part is byte-identical to the pre-split keyring."""
        ring: dict[str, Key] = {}
        if self.audit._key is not None:
            self._ring_add(ring, self.audit._key, source="FIRM_AUDIT_KEY")
        if self.audit._seal_key is not None:
            self._ring_add(ring, self.audit._seal_key, source="FIRM_AUDIT_SEAL_KEY")
        for env in (_RETIRED_KEYS_ENV, _RETIRED_SEAL_KEYS_ENV):
            for extra in self._parse_ring_env(env).values():
                self._ring_add(ring, extra, source=env)
        return ring

    @staticmethod
    def _parse_ring_env(env: str) -> dict[str, Key]:
        """Parse a retired-keyring env var, raising :class:`VerifyError` (not a bare ``ValueError``)
        on a malformed value — so an operator's typo in ``FIRM_AUDIT_RETIRED_KEYS`` surfaces as
        verify's ``error`` outcome (D24), never an uncaught crash that freezes the dashboard at its
        last status."""
        try:
            return parse_keyring(os.environ.get(env), source=env)
        except ValueError as exc:
            raise VerifyError(str(exc)) from exc

    @staticmethod
    def _ring_add(ring: dict[str, Key], key: Key, *, source: str) -> None:
        """Merge one key into a key_id-keyed ring, turning a collision into a :class:`VerifyError`.

        Wraps :func:`~firm.audit.integrity.add_key` (which raises ``ValueError`` on two distinct
        secrets sharing a ``key_id``) so the failure is verify's own error class — caught by
        :meth:`run` and persisted as the ``error`` outcome (D24), never a silent overwrite that
        would flag the shadowed key's objects as TAMPERED."""
        try:
            integrity.add_key(ring, key, source=source)
        except ValueError as exc:
            raise VerifyError(str(exc)) from exc

    @property
    def seal_keyring(self) -> dict[str, Key]:
        """The keys eligible to have signed a *seal* (``rows_mac`` + ``seal_mac``), indexed by
        ``key_id``: the current seal key, and retired seal keys (``FIRM_AUDIT_RETIRED_SEAL_KEYS``).

        The current **row** key is a seal signer **only in single-key mode** — where the one key
        signs both rows and seals. In a two-key deployment (a distinct ``FIRM_AUDIT_SEAL_KEY``) it
        is excluded: a compromised app instance holds only the row key, so refusing it as a seal
        signer is what keeps the seal chain out of a row-key attacker's reach. An attacker who
        re-signs a seal with the row key and relabels its ``key_id`` to the row key's therefore
        lands on an id not in this ring — an unverifiable seal, never a laundered OK (see
        :meth:`_walk_chain`).

        Retired **row** keys (``FIRM_AUDIT_RETIRED_KEYS``) are **never** in this ring, in any mode:
        role-scoping the archive is the whole point of splitting the retired vars. A row key stolen
        from an app instance and later rotated out into ``FIRM_AUDIT_RETIRED_KEYS`` can validate the
        rows it signed, but can never be promoted into a seal-capable key — so the same relabel
        attack with a *retired* row key is just as unverifiable as with the current one. Retired
        seal keys stay eligible because they live only on the verifier, not on any instance an
        attacker could compromise."""
        ring: dict[str, Key] = {}
        if self.audit._seal_key is not None:
            self._ring_add(ring, self.audit._seal_key, source="FIRM_AUDIT_SEAL_KEY")
        # The current row key signs seals only in single-key mode (there it *is* the seal key). In a
        # split deployment it never signs a seal, so it is not a seal signer here.
        if self.audit._key is not None and not self.audit._seal_key_split:
            self._ring_add(ring, self.audit._key, source="FIRM_AUDIT_KEY")
        for extra in self._parse_ring_env(_RETIRED_SEAL_KEYS_ENV).values():
            self._ring_add(ring, extra, source=_RETIRED_SEAL_KEYS_ENV)
        return ring

    # -- rolling state (advisory) --------------------------------------------------------------

    def _load_cursor(self) -> int:
        path = self.audit._verify_state_path
        if not path:
            return 0
        try:
            with open(path, encoding="utf-8") as handle:
                return max(0, int(handle.read().strip()))
        except (OSError, ValueError):
            # Missing or corrupted state simply restarts the rotation from 0. The cursor is
            # advisory: it only reorders which older range a *non-``--full``* run recomputes first,
            # and the always-full chain walk / anchor / pruned-region / tail checks never depend on
            # it. It is *not* MAC-protected, so an attacker who pins it can defer recomputation of
            # a chosen range across non-``--full`` runs forever — which is why coverage of every
            # sealed range is guaranteed only by a periodic ``--full`` (see the module docstring).
            return 0

    def _save_cursor(self, value: int) -> None:
        self._cursor = value
        path = self.audit._verify_state_path
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(str(value))
        except OSError as exc:  # advisory only — a failed write must not fail the verification
            self.audit.on_error(exc)

    # -- entry point ---------------------------------------------------------------------------

    def run(
        self, *, anchor_path: str | None = None, from_seq: int | None = None, full: bool = False
    ) -> VerifyReport:
        """Verify the log and persist the outcome. Returns a :class:`VerifyReport`.

        On a :class:`VerifyError` (e.g. an unknown ``key_id``) the outcome is written as ``error``
        before the exception re-raises, so the dashboard's liveness never mistakes a broken verify
        for a clean one (design review D24).
        """
        started = time.monotonic()
        try:
            report = self._verify(anchor_path=anchor_path, from_seq=from_seq, full=full)
        except VerifyError as exc:
            self._persist_error(str(exc))
            raise
        report.duration_seconds = time.monotonic() - started
        self._persist(report)
        self._emit_finding(report)
        return report

    def _emit_finding(self, report: VerifyReport) -> None:
        """Fire ``AuditLog.on_finding`` once, after persistence, on a ``tampered`` (critical) or
        ``warning`` run — so a scheduled/looped verify that *detects* tampering emits a readable
        high-severity event to the operator's log pipeline (default: one stderr line), not just a
        return value. ``ok`` / ``unprotected`` stay silent; the ``error`` outcome never reaches here
        (it re-raises before ``run`` returns). A broken user sink is routed to ``on_error`` — a
        read-only verify must never crash because a log forwarder threw."""
        if report.outcome not in ("tampered", "warning"):
            return
        severity = "critical" if report.outcome == "tampered" else "warning"
        wanted = "tampered" if severity == "critical" else "warning"
        affected = tuple(
            f.identifier for f in report.findings if f.verdict == wanted and f.identifier
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
        except Exception as exc:  # a failing sink must not fail the verification
            self.audit.on_error(exc)

    # -- the verification pass -----------------------------------------------------------------

    def _verify(self, *, anchor_path: str | None, from_seq: int | None, full: bool) -> VerifyReport:
        keyring = self.keyring
        if not keyring:
            raise VerifyError(
                "audit verification needs a key — set FIRM_AUDIT_KEY (and, during a rotation, the "
                "retired archives FIRM_AUDIT_RETIRED_KEYS / FIRM_AUDIT_RETIRED_SEAL_KEYS) before "
                "running verify."
            )
        # Seals (rows_mac + seal_mac) are checked against the seal keyring; rows against the full
        # keyring. The two differ only in a two-key deployment, where the row key is not a seal
        # signer — see :attr:`seal_keyring`.
        seal_keyring = self.seal_keyring

        now = now_utc()
        counters = _Counters()
        findings: list[Finding] = _Findings()  # bounded accumulation (Bug #6)
        engine = self.audit.engine

        # A snapshot transaction (design "Bug #5"): verify reads seals, then rows, across many
        # statements. On Postgres/MySQL READ COMMITTED a concurrent legitimate prune committing
        # between those reads would make verify compare stale seals to already-pruned rows and cry
        # a false TAMPERED. Reading them all from one REPEATABLE READ / WAL snapshot removes that
        # window; verify stays read-only.
        with snapshot_transaction(engine) as conn:
            seals = conn.execute(select(_seals).order_by(_seals.c.seq)).all()
            floor, boundary = self._floor_and_boundary(seals)
            max_sealed = max((s.to_id for s in seals), default=0)

            # Layer 2, part 1: the seal chain itself (dense seq, prev_mac linkage, seal_mac
            # recompute) — cheap, always walked in full so seal tampering never depends on rotation.
            self._walk_chain(seals, keyring, seal_keyring, counters, findings)

            # The pruned region (ids at/below the checkpoint floor) must be empty — retention
            # deleted every row through the floor, so any surviving row there is a forged insert
            # into a range the checkpoint asserts holds zero rows. Verify skips recomputation
            # at/below the floor, so without this bounded probe such a row would be invisible even
            # to ``--full`` (design "row present in a pruned range").
            self._check_pruned_region_empty(conn, floor, counters, findings)

            # Anti-replay backstop: the unique index rejects duplicate entry_ids at insert, but if
            # it was dropped, verify still reports them (design "Layer 1", replay).
            self._check_duplicates(conn, counters, findings)

            # Layer 2, part 2 + Layer 1: recompute rows for the ranges selected this run.
            self._verify_ranges(
                conn,
                seals,
                floor,
                boundary,
                keyring,
                seal_keyring,
                counters,
                findings,
                from_seq=from_seq,
                full=full,
            )
            # Layer 1 for the unsealed tail (always) — plus its size/age for liveness reporting.
            tail_count, tail_oldest = self._verify_tail(
                conn, max_sealed, boundary, keyring, counters, findings
            )
            self._check_tail_liveness(seals, tail_oldest, now, counters, findings)

            # Layer 3.
            newest_anchor_at, anchor_configured, force_nonzero = self._check_anchor(
                anchor_path, seals, floor, now, counters, findings
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
            anchor_configured=anchor_configured,
            force_nonzero=force_nonzero,
        )

    # -- floor / activation boundary -----------------------------------------------------------

    def _floor_and_boundary(self, seals: Sequence[Any]) -> tuple[int, int | None]:
        """The coverage floor and the activation boundary a run reads from the seals.

        The **floor** is the highest checkpoint ``to_id`` (0 if never pruned): rows and seal ranges
        at or below it are legitimately gone, so their recomputation is skipped. The **activation
        boundary** classifies NULL-MAC rows: at/below it they are legacy (unprotected), above it a
        missing MAC is tampering.

        The boundary is the **highest sealed id**, not seq-1's ``to_id``. The first backlog drain
        seals the pre-existing rows in successive batches of ``seal_batch_size`` (review 7A), so
        seq 1 only reaches the end of the *first* batch — trusting its ``to_id`` would flag every
        legacy row batched into seq 2, 3, … as tampered (a false red). Every legacy NULL-MAC row is
        at or below the highest sealed id, and every post-activation row carries a MAC that is
        checked regardless of the boundary, so taking the maximum never under-counts the legacy
        region and never lets a genuine post-activation forgery through: a NULL-MAC row *above* the
        highest seal (i.e. in the tail) is still tampering, and a NULL-MAC row slipped into a sealed
        range trips that range's ``rows_mac``/count check (see :meth:`_verify_one_range`). With no
        seals at all the boundary is ``None`` — nothing is "after activation" yet, so a straggler
        NULL-MAC row during a two-phase rollout is unprotected, not tampered.
        """
        floor = max((s.to_id for s in seals if s.kind == "checkpoint"), default=0)
        if not seals:
            return 0, None
        return floor, max(s.to_id for s in seals)

    # -- Layer 2: chain walk -------------------------------------------------------------------

    def _walk_chain(
        self,
        seals: Sequence[Any],
        keyring: dict[str, Key],
        seal_keyring: dict[str, Key],
        counters: _Counters,
        findings: list[Finding],
    ) -> None:
        """Dense ``seq``, ``prev_mac`` linkage, and ``seal_mac`` recompute over every seal.

        A gap in ``seq`` is a deleted mid-chain seal; a broken ``prev_mac`` is a reordered or edited
        seal; a ``seal_mac`` that no longer recomputes is an edited seal. The single lowest survivor
        is allowed to have a pruned predecessor **only** when a key-signed checkpoint authorizes the
        missing front — otherwise a front truncation would masquerade as a legitimate prune.
        """
        if not seals:
            return
        by_seq = {s.seq: s for s in seals}
        has_checkpoint = any(s.kind == "checkpoint" for s in seals)

        for prev, cur in pairwise(seals):
            if cur.seq != prev.seq + 1:
                findings.append(
                    Finding(
                        "tampered",
                        f"seal chain has a gap between seq {prev.seq} and {cur.seq} "
                        "(a mid-chain seal was deleted)",
                        f"seal {prev.seq}->{cur.seq}",
                    )
                )
                counters.tampered += 1

        for seal in seals:
            key = seal_keyring.get(seal.key_id)
            if key is None:
                if seal.key_id in keyring:
                    # Known as a row key (current or retired) but not eligible to sign seals — a row
                    # key can never validate a seal. Either a two-key verifier is missing its seal
                    # key, or an attacker re-signed the seal with a row key they hold (a current
                    # one, or a rotated-out row key from FIRM_AUDIT_RETIRED_KEYS) and relabeled
                    # ``key_id`` to match. Both are unverifiable — never a laundered OK.
                    raise VerifyError(
                        f"seal seq {seal.seq} is signed by key_id {seal.key_id!r}, which is a "
                        "known row key but not a seal key — a row key cannot validate a seal. "
                        "Either this two-key verifier is missing its seal key (add "
                        "FIRM_AUDIT_SEAL_KEY / FIRM_AUDIT_RETIRED_SEAL_KEYS), or a "
                        "current-or-retired row key was used to forge the seal (tampering)."
                    )
                raise VerifyError(
                    f"seal seq {seal.seq} was signed by unknown key_id {seal.key_id!r} — "
                    "add its secret to FIRM_AUDIT_RETIRED_SEAL_KEYS (or FIRM_AUDIT_SEAL_KEY)."
                )
            if not hmac.compare_digest(
                integrity.seal_mac(
                    key,
                    seq=seal.seq,
                    kind=seal.kind,
                    from_id=seal.from_id,
                    to_id=seal.to_id,
                    row_count=seal.row_count,
                    rows_mac=seal.rows_mac,
                    prev_mac=seal.prev_mac,
                    sealed_at=seal.sealed_at,
                    gaps=seal.gap_ranges or "",
                ),
                seal.seal_mac,
            ):
                findings.append(
                    Finding(
                        "tampered",
                        f"seal seq {seal.seq} has an invalid seal_mac (edited)",
                        f"seal {seal.seq}",
                    )
                )
                counters.tampered += 1

            predecessor = by_seq.get(seal.seq - 1)
            if seal.seq == 1:
                if seal.prev_mac != _GENESIS:
                    findings.append(
                        Finding(
                            "tampered",
                            "the genesis seal (seq 1) is not chained to 'genesis'",
                            "seal 1",
                        )
                    )
                    counters.tampered += 1
            elif predecessor is not None:
                if not hmac.compare_digest(seal.prev_mac, predecessor.seal_mac):
                    findings.append(
                        Finding(
                            "tampered",
                            f"seal seq {seal.seq} prev_mac does not link to seq "
                            f"{predecessor.seq} (reordered or edited)",
                            f"seal {seal.seq}",
                        )
                    )
                    counters.tampered += 1
            elif not has_checkpoint:
                # Lowest survivor with no predecessor and nothing authorizing the missing front:
                # the earlier seals (and rows) were truncated away.
                findings.append(
                    Finding(
                        "tampered",
                        f"seal chain starts at seq {seal.seq} with no checkpoint "
                        "authorizing the missing earlier seals (front truncation)",
                        f"seal {seal.seq}",
                    )
                )
                counters.tampered += 1

    def _check_duplicates(
        self, conn: Connection, counters: _Counters, findings: list[Finding]
    ) -> None:
        dups = conn.execute(
            select(_audits.c.entry_id)
            .where(_audits.c.entry_id.is_not(None))
            .group_by(_audits.c.entry_id)
            .having(func.count() > 1)
        ).all()
        for row in dups:
            findings.append(
                Finding(
                    "tampered",
                    f"entry_id {row.entry_id!r} appears more than once (replay)",
                    f"entry_id {row.entry_id}",
                )
            )
            counters.tampered += 1

    def _check_pruned_region_empty(
        self, conn: Connection, floor: int, counters: _Counters, findings: list[Finding]
    ) -> None:
        """Assert the pruned region (``id <= floor``) is empty — a forged insert there is TAMPERED.

        A checkpoint records ``row_count = 0`` over the range it pruned and verify skips recomputing
        anything at or below the floor (the rows are legitimately gone), so a row inserted at a low,
        already-pruned id would otherwise never be looked at — invisible even to ``--full`` while
        ``history()`` still returns it. Retention deletes *every* row through the floor, so after a
        prune there are legitimately none; a single bounded ``LIMIT``ed probe (never a full scan)
        turns any survivor into a tampering finding. Inert when nothing was pruned (floor 0)."""
        if floor <= 0:
            return
        stragglers = conn.execute(
            select(_audits.c.id).where(_audits.c.id <= floor).order_by(_audits.c.id).limit(5)
        ).all()
        if not stragglers:
            return
        ids = ", ".join(str(row.id) for row in stragglers)
        findings.append(
            Finding(
                "tampered",
                f"row(s) {ids} are present at or below the checkpoint floor {floor}, a pruned "
                "range the checkpoint asserts is empty (forged insert into pruned history)",
                f"rows <= {floor}",
            )
        )
        counters.tampered += 1

    # -- Layer 2 + Layer 1: sealed ranges ------------------------------------------------------

    def _verify_ranges(
        self,
        conn: Connection,
        seals: Sequence[Any],
        floor: int,
        boundary: int | None,
        keyring: dict[str, Key],
        seal_keyring: dict[str, Key],
        counters: _Counters,
        findings: list[Finding],
        *,
        from_seq: int | None,
        full: bool,
    ) -> None:
        # Ranges with live rows to recompute: ``seal`` kind above the checkpoint floor. Checkpoints
        # and ranges at/below the floor are skipped (their rows are pruned) — the chain walk already
        # validated their seal_macs.
        covering = [s for s in seals if s.kind == "seal" and s.to_id > floor]
        if not covering:
            return

        # Contiguity of the covering ranges: deleting a whole covering seal shows as a from_id/to_id
        # gap even if seq stayed dense (the deleted seal's rows now fall through a hole).
        if covering[0].from_id != floor:
            findings.append(
                Finding(
                    "tampered",
                    f"the earliest covering seal starts at id {covering[0].from_id}, "
                    f"not the expected floor {floor} (coverage gap at the front)",
                    f"seal {covering[0].seq}",
                )
            )
            counters.tampered += 1
        for prev, cur in pairwise(covering):
            if cur.from_id != prev.to_id:
                findings.append(
                    Finding(
                        "tampered",
                        f"seals {prev.seq} and {cur.seq} are not contiguous "
                        f"({prev.to_id} != {cur.from_id})",
                        f"seal {prev.seq}->{cur.seq}",
                    )
                )
                counters.tampered += 1

        selected = self._select_ranges(covering, from_seq=from_seq, full=full)
        for seal in selected:
            self._verify_one_range(conn, seal, boundary, keyring, seal_keyring, counters, findings)

    def _select_ranges(self, covering: list[Any], *, from_seq: int | None, full: bool) -> list[Any]:
        """Pick which covering ranges to recompute this run (design review D12).

        ``full`` → all of them; ``from_seq`` → every range at or after that seq; otherwise the
        rotating slice: the newest range always, plus ``ceil(n / verify_cycle)`` older ranges from
        the advisory cursor, so a full sweep completes within ``verify_cycle`` runs **for an honest
        operator**. The cursor is not MAC-protected, so a run that does a partial slice cannot
        *prove* it will rotate (a pinning attacker can hold it fixed); only a periodic ``--full``
        guarantees every range is recomputed. A warning fires whenever a non-``--full`` run does a
        partial slice, so the reliance on ``--full`` is never silent.
        """
        if full:
            return covering
        if from_seq is not None:
            return [s for s in covering if s.seq >= from_seq]

        n = len(covering)
        per_run = max(1, math.ceil(n / max(1, self.audit.verify_cycle)))
        if per_run < n and not self.audit._verify_state_path:
            # Rolling coverage rotates through the older ranges via the cursor — but with no
            # persisted state path the cursor lives only in this process. A fresh per-run
            # ``firm-audit verify`` (the documented cron deployment, D12) then always restarts the
            # rotation at 0, re-checking the same newest ranges every run and never reaching the
            # middle ones, so an edit in an old range stays invisible until a ``--full``. This is
            # the hard, always-true version of the coverage gap; make it loud rather than ship a
            # guarantee that does not hold. (Even *with* a persisted state the cursor is advisory
            # and attacker-pinnable — see the module docstring — so ``--full`` is the only real
            # coverage guarantee; the seal chain, anchor, pruned-region and tail checks are walked
            # in full every run regardless.)
            warnings.warn(
                "audit verify is doing rolling (non-full) coverage without a persisted rotation "
                "state — set verify_state_path / FIRM_AUDIT_VERIFY_STATE so a per-run cron rotates "
                "through old ranges, and run `firm-audit verify --full` periodically (only --full "
                "guarantees every sealed range is recomputed; the rotation cursor is advisory and "
                "not MAC-protected). Otherwise each fresh run re-checks only newest ranges (D12).",
                stacklevel=2,
            )
        start = self._cursor % n
        chosen = {covering[(start + i) % n].seq: covering[(start + i) % n] for i in range(per_run)}
        chosen[covering[-1].seq] = covering[-1]  # always the newest range
        self._save_cursor((start + per_run) % n)
        return [s for _, s in sorted(chosen.items())]

    def _verify_one_range(
        self,
        conn: Connection,
        seal: Any,
        boundary: int | None,
        keyring: dict[str, Key],
        seal_keyring: dict[str, Key],
        counters: _Counters,
        findings: list[Finding],
    ) -> None:
        """Recompute one sealed range and fold its verdict into the report.

        Delegates the classification to :func:`classify_range` — the *same* classifier retention's
        pre-prune gate uses, so a range can never be a WARNING to verify and a refusal to retention
        (or vice versa). ``"ok"`` adds nothing; ``"late_commit"`` (every present row validly signed
        plus surplus rows — a valid MAC in a sealed range is a latecomer, design 1A) is an amber
        WARNING; anything else is TAMPERED. The per-row verdicts drive granular findings so the
        dashboard/CLI can name the exact offending row.
        """
        verdict, per_row = classify_range(conn, seal, boundary, keyring, seal_keyring)
        for row, row_verdict in per_row:
            self._record_row_verdict(row, row_verdict, counters, findings)
        if verdict == "ok":
            return
        present = len(per_row)
        if verdict == "late_commit":
            findings.append(
                Finding(
                    "warning",
                    f"seal seq {seal.seq} covers {present} rows but sealed "
                    f"{seal.row_count} — {present - seal.row_count} valid-MAC late "
                    "commit(s). Widen grace or record long jobs via the own-transaction path.",
                    f"seal {seal.seq}",
                )
            )
            counters.warning += 1
        else:
            findings.append(
                Finding(
                    "tampered",
                    "records deleted, inserted, or swapped in this sealed range "
                    f"(rows {seal.from_id + 1}-{seal.to_id})",
                    f"sealed range #{seal.seq}",
                )
            )
            counters.tampered += 1

    # -- Layer 1: unsealed tail ----------------------------------------------------------------

    def _verify_tail(
        self,
        conn: Connection,
        max_sealed: int,
        boundary: int | None,
        keyring: dict[str, Key],
        counters: _Counters,
        findings: list[Finding],
    ) -> tuple[int, datetime | None]:
        """Recompute every unsealed row's MAC (Layer 1) and return the tail's size and oldest age.

        The tail is modification/forgery-protected but not yet deletion/position-protected — that
        arrives with the seal. Its size and age feed the sealer-liveness signal.
        """
        count = 0
        oldest: datetime | None = None
        for row in _iter_rows(conn, max_sealed, None):
            count += 1
            if oldest is None or row.created_at < oldest:
                oldest = row.created_at
            self._record_row_verdict(row, _row_verdict(row, boundary, keyring), counters, findings)
        return count, oldest

    def _check_tail_liveness(
        self,
        seals: Sequence[Any],
        tail_oldest: datetime | None,
        now: datetime,
        counters: _Counters,
        findings: list[Finding],
    ) -> None:
        """WARN when the oldest unsealed row has outlived ``unsealed_tail_max_age`` — the sealer is
        stalled (design review D15). Only meaningful once sealing is active (some seal exists)."""
        if not seals or tail_oldest is None:
            return
        age = (now - tail_oldest).total_seconds()
        if age > self.audit._unsealed_tail_max_age:
            findings.append(
                Finding(
                    "warning",
                    f"the oldest unsealed row is {int(age)}s old (> "
                    f"{int(self.audit._unsealed_tail_max_age)}s) — the sealer looks stalled; "
                    "un-sealed rows are not deletion-protected.",
                    "unsealed-tail",
                )
            )
            counters.warning += 1

    def _record_row_verdict(
        self,
        row: Any,
        verdict: str,
        counters: _Counters,
        findings: list[Finding],
    ) -> None:
        """Fold a per-row verdict from :func:`_row_verdict` into the counters and findings.

        The verdict itself is decided by the shared :func:`_row_verdict` (so the tail check, the
        per-range classifier, and retention's gate all agree on row validity); this only turns it
        into the report's counters and, for a ``"tampered"`` row, a human finding naming the exact
        row and how it failed (a missing MAC after the boundary vs a MAC that no longer recomputes).
        """
        if verdict == "ok":
            counters.ok += 1
        elif verdict == "unprotected":
            counters.unprotected += 1  # legacy row from before the key existed — not an alarm
        elif row.row_mac is None:
            findings.append(
                Finding(
                    "tampered",
                    "unsigned record inserted after sealing (no signature, forged insert)",
                    f"#{row.id} {row.action}",
                    id=row.id,
                )
            )
            counters.tampered += 1
        else:
            findings.append(
                Finding(
                    "tampered",
                    "modified after it was sealed (signature no longer matches its contents)",
                    f"#{row.id} {row.action}",
                    id=row.id,
                )
            )
            counters.tampered += 1

    # -- Layer 3: anchor -----------------------------------------------------------------------

    def _check_anchor(
        self,
        anchor_path: str | None,
        seals: Sequence[Any],
        floor: int,
        now: datetime,
        counters: _Counters,
        findings: list[Finding],
    ) -> tuple[datetime | None, bool, bool]:
        """Compare the newest exported anchor to the stored chain (design Layer 3 / D16).

        Returns ``(newest_anchor_at, anchor_configured, force_nonzero)``. A chain that no longer
        contains the anchored seq, or is shorter than the anchor, is tail-truncation or a
        drop-and-recreate → TAMPERED. An anchor older than ``anchor_max_age`` → WARNING that forces
        a non-zero exit, because the window between the last anchored seal and the head is the one
        thing only the anchor guards.

        The one benign way an anchored seal can be absent from the chain is retention: a
        ``checkpoint`` seal legitimately prunes the seals (and rows) below it. Pruning and
        truncation are told apart **in seq-space**: a pruned seal sits below the checkpoint that
        subsumed it, so its ``seq`` is at or below the surviving head; a tail-truncated seal *is*
        the old head, so its ``seq`` is above the surviving head. So an anchored seq that is absent
        but at or below ``head_seq`` — with a checkpoint present to authorize a prune — was pruned,
        not truncated. (An earlier version compared the anchored ``seq`` to the checkpoint floor,
        a *row id*; the unit mismatch meant that once any checkpoint existed the test passed for
        almost any seq and laundered a real tail truncation to OK.) Accepting this is safe because a
        checkpoint's ``seal_mac`` is key-signed and re-verified by the chain walk — an attacker
        without the key cannot manufacture one, and a wholesale drop-and-recreate leaves no
        surviving checkpoint. Retention also exports the checkpoint to the anchor, so the newest
        anchor normally names the checkpoint itself; the ``seq <= head_seq`` clause only covers the
        residual case where that write was lost and the newest anchor still names a pruned seal.
        """
        if anchor_path is None:
            return None, False, False
        anchor = _read_newest_anchor(anchor_path)
        if anchor is None:
            if seals:
                findings.append(
                    Finding(
                        "warning",
                        f"anchor file {anchor_path!r} is missing or empty but seals "
                        "exist — the external truncation guard is not being written.",
                        "anchor",
                    )
                )
                counters.warning += 1
            return None, True, False

        seq, seal_mac, sealed_at = anchor
        by_seq = {s.seq: s for s in seals}
        head_seq = max((s.seq for s in seals), default=0)
        has_checkpoint = any(s.kind == "checkpoint" for s in seals)

        # Coverage watermark (Bug B). Retention writes the checkpoint at the *head* (highest seq)
        # but covering the *lowest* range, so a real seal it leaves behind sits at a LOWER seq than
        # the checkpoint. An attacker who deletes that seal AND its rows leaves a chain that is
        # dense and self-consistent by seq — the checkpoint's front-truncation excuse hides the
        # dangling link, no covering seal remains to fail a rows_mac check, and the tail starts
        # empty above the now-lowered max ``to_id``. Nothing in the database remembers those ids
        # were sealed. The anchor does: every seal exported its ``to_id``. If the highest coverage
        # the anchor ever recorded exceeds the highest coverage still present in the chain, a sealed
        # range above the floor was truncated — the newest-seq test below cannot see it because the
        # deleted seal was never the anchored head. A legitimately pruned seal only ever covered ids
        # at or below the checkpoint floor (subsumption deletes ``to_id <= checkpoint.to_id``), and
        # that floor is itself a present seal, so a benign prune never makes the watermark exceed
        # the present max — no false TAMPERED on a clean post-prune log or a genuine later seal.
        max_present_to_id = max((s.to_id for s in seals), default=0)
        max_anchored_to_id = _read_anchor_max_to_id(anchor_path)
        if max_anchored_to_id > max_present_to_id:
            findings.append(
                Finding(
                    "tampered",
                    f"the anchor recorded coverage through id {max_anchored_to_id}, but the stored "
                    f"chain now covers only through id {max_present_to_id} — a sealed range above "
                    "the checkpoint floor was truncated (its seal and rows both deleted).",
                    f"coverage <= {max_present_to_id}",
                )
            )
            counters.tampered += 1
        matched = by_seq.get(seq)
        # An anchored seal legitimately absent from the chain was *pruned* by a checkpoint, never
        # *truncated* from the tail. The two are told apart in seq-space: a pruned seal sits BELOW
        # the checkpoint that subsumed it, so its ``seq`` is at or below the surviving head; a
        # tail-truncated seal is (was) the head, so its ``seq`` is ABOVE the surviving head. The old
        # test compared the anchored ``seq`` to the checkpoint ``floor`` — a *row id*, not a seq —
        # so once any checkpoint existed ``seq <= floor`` was almost always true and a genuine tail
        # truncation was laundered to OK. Gate on the seq-space head instead, and only when a
        # checkpoint actually authorizes a prune (its key-signed seal_mac is re-verified by the
        # chain walk, so an attacker without the key cannot manufacture one).
        legitimately_pruned = matched is None and has_checkpoint and seq <= head_seq
        if not legitimately_pruned and (
            matched is None or not hmac.compare_digest(matched.seal_mac, seal_mac) or head_seq < seq
        ):
            findings.append(
                Finding(
                    "tampered",
                    f"the newest anchor (seq {seq}) is not present at the head of "
                    "the stored chain — tail truncation or a drop-and-recreate.",
                    f"seal {seq}",
                )
            )
            counters.tampered += 1

        force_nonzero = False
        age = (now - sealed_at).total_seconds()
        if age > self.audit._anchor_max_age:
            findings.append(
                Finding(
                    "warning",
                    f"the newest anchor is {int(age)}s old (> "
                    f"{int(self.audit._anchor_max_age)}s) — the anchor sink looks stalled; the "
                    "silently-truncatable window is growing.",
                    "anchor",
                )
            )
            counters.warning += 1
            force_nonzero = True
        return sealed_at, True, force_nonzero

    # -- report assembly & persistence ---------------------------------------------------------

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
        if counters.tampered:
            outcome = "tampered"
        elif counters.warning:
            outcome = "warning"
        else:
            outcome = "ok"  # UNPROTECTED-only stays OK/exit-0 (reported as a count, not an alarm)
        exit_code = 1 if (counters.tampered or force_nonzero) else 0
        return VerifyReport(
            outcome=outcome,
            exit_code=exit_code,
            findings=findings,
            ok_count=counters.ok,
            warning_count=counters.warning,
            unprotected_count=counters.unprotected,
            tampered_count=counters.tampered,
            error_message=None,
            last_full_coverage_at=now if full else prior_full_coverage,
            cycle_position=self._cursor,
            cycle_length=self.audit.verify_cycle,
            newest_anchor_at=newest_anchor_at,
            anchor_configured=anchor_configured,
            unsealed_tail_count=tail_count,
            unsealed_tail_oldest_at=tail_oldest,
            affected_identifiers=_affected_json(findings, counters.tampered),
        )

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
            cycle_position=report.cycle_position,
            cycle_length=report.cycle_length,
            newest_anchor_at=report.newest_anchor_at,
            anchor_configured=report.anchor_configured,
            unsealed_tail_count=report.unsealed_tail_count,
            unsealed_tail_oldest_at=report.unsealed_tail_oldest_at,
            affected_identifiers=report.affected_identifiers,
            duration_seconds=report.duration_seconds,
        )

    def _persist_error(self, message: str) -> None:
        """Write the ``error`` outcome before a :class:`VerifyError` re-raises (design D24). Best
        effort — if even this write fails, the original error must still surface."""
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
                cycle_position=self._cursor,
                cycle_length=self.audit.verify_cycle,
                newest_anchor_at=None,
                anchor_configured=False,
                unsealed_tail_count=0,
                unsealed_tail_oldest_at=None,
                affected_identifiers=None,
                duration_seconds=0.0,
            )
        except Exception as exc:  # never mask the VerifyError we are about to re-raise
            self.audit.on_error(exc)

    def _upsert_status(self, **values: Any) -> None:
        """Upsert the single ``firm_audit_verify_status`` row (id 1) via the native upsert."""
        dialect = get_dialect(self.audit.engine)
        payload = {"id": _STATUS_ID, **values}
        stmt = dialect.upsert(
            _status,
            payload,
            index_elements=["id"],
            update_columns=[c for c in payload if c != "id"],
        )
        with get_dialect(self.audit.engine).begin_claim_tx(self.audit.engine) as conn:
            conn.execute(stmt)
