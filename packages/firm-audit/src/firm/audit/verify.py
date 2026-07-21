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
regardless, and ``--full`` recomputes every range from the genesis/checkpoint floor. An attacker
who rewrites the cursor (or the whole state) only changes *which* range is recomputed first, never
*whether* tampering is eventually found.

Verdicts (design review 1A/5A): ``OK`` · ``WARNING`` (a valid-MAC late commit, or a stalled-sealer
liveness signal — never an alarm) · ``UNPROTECTED`` (legacy NULL-MAC rows at/below activation) ·
``TAMPERED`` (anything cryptographically inconsistent after activation). Exit code 0 covers
OK/UNPROTECTED (WARNINGs print but exit 0); TAMPERED and a stale anchor exit non-zero. An unknown
``key_id`` is a hard failure (:class:`VerifyError`) — the outcome is persisted as ``error`` before
it re-raises (design review D24) so a dead verify cron and a real tamper never look alike.
"""

from __future__ import annotations

import hmac
import math
import os
import time
import warnings
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise
from typing import TYPE_CHECKING, Any

from sqlalchemy import Connection, func, select

from .._core.clock import now_utc
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

#: Rows read per keyset page when recomputing Layer-1 MACs (bounded memory on a large table).
_PAGE = 1000

#: The fixed primary key of the single ``firm_audit_verify_status`` row (the "single-row" contract).
_STATUS_ID = 1


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


def range_is_intact(conn: Connection, seal: Any, keyring: dict[str, Key]) -> bool:
    """Re-verify a sealed range end-to-end — the contract behind "retention only prunes what
    verifies" (design "Retention integration").

    Returns ``True`` only when *every* signed row in ``(seal.from_id, seal.to_id]`` still recomputes
    its ``row_mac`` **and** the range's ``rows_mac``/``row_count`` still match the seal. The per-row
    recompute is load-bearing: an attacker who edits a sealed row's content but leaves its
    ``row_mac`` column untouched leaves ``rows_mac`` (which hashes the stored MAC strings, not the
    content) matching — only recomputing each MAC from the content catches it. The rows_mac/count
    check additionally catches deletions and insertions. A NULL-MAC legacy row is folded in with the
    ``nomac`` marker exactly as the sealer did, so an untampered legacy range still verifies; an
    unknown ``key_id`` (row or seal) makes the range unverifiable, hence not prunable.

    Any mismatch returns ``False`` so retention refuses to prune (and never checkpoints past) the
    range — pruning it would erase the evidence instead of surfacing it."""
    key = keyring.get(seal.key_id)
    if key is None:
        return False
    pairs: list[tuple[int, str | None]] = []
    for row in _iter_rows(conn, seal.from_id, seal.to_id):
        pairs.append((row.id, row.row_mac))
        if row.row_mac is not None:
            row_key = keyring.get(row.key_id)
            if row_key is None or not hmac.compare_digest(
                recompute_row_mac(row_key, row), row.row_mac
            ):
                return False
    return (
        hmac.compare_digest(integrity.rows_mac(key, pairs), seal.rows_mac)
        and len(pairs) == seal.row_count
    )


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
    finding). ``identifier`` names the affected row/seal/id-range so the dashboard can link into
    the audit table and the CLI can print it.
    """

    verdict: str
    message: str
    identifier: str | None = None


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


# --- anchor file -----------------------------------------------------------------------------


def _read_newest_anchor(path: str) -> tuple[int, str, datetime] | None:
    """Parse the newest ``"<sealed_at> <seq> <seal_mac>"`` line, or ``None`` if the file is
    missing/empty. ``sealed_at`` is the :func:`~.integrity.canonical_created_at` ISO string (no
    embedded spaces), so a plain whitespace split yields exactly three fields."""
    try:
        with open(path, encoding="utf-8") as handle:
            lines = [line for line in handle.read().splitlines() if line.strip()]
    except FileNotFoundError:
        return None
    if not lines:
        return None
    parts = lines[-1].split()
    seq = int(parts[1])
    seal_mac = parts[2]
    sealed_at = datetime.fromisoformat(parts[0])
    return seq, seal_mac, sealed_at


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
        """All keys verification may check against, indexed by ``key_id``: the writer key plus any
        rotation keys in ``FIRM_AUDIT_KEYS``. Empty when the feature is off."""
        ring: dict[str, Key] = {}
        if self.audit._key is not None:
            ring[self.audit._key.id] = self.audit._key
        for extra in parse_keyring(os.environ.get("FIRM_AUDIT_KEYS")).values():
            ring[extra.id] = extra
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
            # Missing or attacker-corrupted state simply restarts the rotation from 0; it can never
            # cause a range to be *skipped forever* (the chain walk is always full, and the cursor
            # only reorders which older range is recomputed first).
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
        return report

    # -- the verification pass -----------------------------------------------------------------

    def _verify(
        self, *, anchor_path: str | None, from_seq: int | None, full: bool
    ) -> VerifyReport:
        keyring = self.keyring
        if not keyring:
            raise VerifyError(
                "audit verification needs a key — set FIRM_AUDIT_KEY (or FIRM_AUDIT_KEYS for a "
                "rotation) before running verify."
            )

        now = now_utc()
        counters = _Counters()
        findings: list[Finding] = []
        engine = self.audit.engine

        with engine.connect() as conn:
            seals = conn.execute(select(_seals).order_by(_seals.c.seq)).all()
            floor, boundary = self._floor_and_boundary(seals)
            max_sealed = max((s.to_id for s in seals), default=0)

            # Layer 2, part 1: the seal chain itself (dense seq, prev_mac linkage, seal_mac
            # recompute) — cheap, always walked in full so seal tampering never depends on rotation.
            self._walk_chain(seals, keyring, counters, findings)

            # Anti-replay backstop: the unique index rejects duplicate entry_ids at insert, but if
            # it was dropped, verify still reports them (design "Layer 1", replay).
            self._check_duplicates(conn, counters, findings)

            # Layer 2, part 2 + Layer 1: recompute rows for the ranges selected this run.
            self._verify_ranges(
                conn, seals, floor, boundary, keyring, counters, findings,
                from_seq=from_seq, full=full,
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
            key = keyring.get(seal.key_id)
            if key is None:
                raise VerifyError(
                    f"seal seq {seal.seq} was signed by unknown key_id {seal.key_id!r} — "
                    "add its secret to FIRM_AUDIT_KEYS."
                )
            if not hmac.compare_digest(integrity.seal_mac(
                key,
                seq=seal.seq,
                kind=seal.kind,
                from_id=seal.from_id,
                to_id=seal.to_id,
                row_count=seal.row_count,
                rows_mac=seal.rows_mac,
                prev_mac=seal.prev_mac,
                sealed_at=seal.sealed_at,
            ), seal.seal_mac):
                findings.append(
                    Finding("tampered", f"seal seq {seal.seq} has an invalid seal_mac (edited)",
                            f"seal {seal.seq}")
                )
                counters.tampered += 1

            predecessor = by_seq.get(seal.seq - 1)
            if seal.seq == 1:
                if seal.prev_mac != _GENESIS:
                    findings.append(
                        Finding("tampered", "the genesis seal (seq 1) is not chained to 'genesis'",
                                "seal 1")
                    )
                    counters.tampered += 1
            elif predecessor is not None:
                if not hmac.compare_digest(seal.prev_mac, predecessor.seal_mac):
                    findings.append(
                        Finding("tampered", f"seal seq {seal.seq} prev_mac does not link to seq "
                                f"{predecessor.seq} (reordered or edited)", f"seal {seal.seq}")
                    )
                    counters.tampered += 1
            elif not has_checkpoint:
                # Lowest survivor with no predecessor and nothing authorizing the missing front:
                # the earlier seals (and rows) were truncated away.
                findings.append(
                    Finding("tampered", f"seal chain starts at seq {seal.seq} with no checkpoint "
                            "authorizing the missing earlier seals (front truncation)",
                            f"seal {seal.seq}")
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
                Finding("tampered", f"entry_id {row.entry_id!r} appears more than once (replay)",
                        f"entry_id {row.entry_id}")
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
                Finding("tampered",
                        f"the earliest covering seal starts at id {covering[0].from_id}, "
                        f"not the expected floor {floor} (coverage gap at the front)",
                        f"seal {covering[0].seq}")
            )
            counters.tampered += 1
        for prev, cur in pairwise(covering):
            if cur.from_id != prev.to_id:
                findings.append(
                    Finding("tampered", f"seals {prev.seq} and {cur.seq} are not contiguous "
                            f"({prev.to_id} != {cur.from_id})", f"seal {prev.seq}->{cur.seq}")
                )
                counters.tampered += 1

        selected = self._select_ranges(covering, from_seq=from_seq, full=full)
        for seal in selected:
            self._verify_one_range(conn, seal, boundary, keyring, counters, findings)

    def _select_ranges(
        self, covering: list[Any], *, from_seq: int | None, full: bool
    ) -> list[Any]:
        """Pick which covering ranges to recompute this run (design review D12).

        ``full`` → all of them; ``from_seq`` → every range at or after that seq; otherwise the
        rotating slice: the newest range always, plus ``ceil(n / verify_cycle)`` older ranges from
        the advisory cursor, so a full sweep completes within ``verify_cycle`` runs.
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
            # middle ones, so an edit in an old range stays invisible until a manual ``--full``.
            # Make that silent gap loud rather than shipping a coverage guarantee that quietly
            # does not hold (D12; the seal chain and anchor are still walked in full every run).
            warnings.warn(
                "audit verify is doing rolling (non-full) coverage without a persisted rotation "
                "state — set verify_state_path / FIRM_AUDIT_VERIFY_STATE so a per-run cron rotates "
                "through old ranges, or run `firm-audit verify --full` periodically. Otherwise "
                "each fresh run re-checks only the newest ranges, never older ones (D12).",
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
        counters: _Counters,
        findings: list[Finding],
    ) -> None:
        """Recompute one sealed range: every row's MAC (Layer 1) and the range's ``rows_mac``/count.

        A ``rows_mac``/count mismatch where *every present row is validly signed and there are
        extra rows* is a late commit (WARNING — the extras landed after the range was sealed,
        design 1A). Any other mismatch is TAMPERED: a deletion, a count-preserving swap, or an
        extra row that is *not* a valid-MAC late commit — including a NULL-MAC row slipped into a
        sealed range (which :meth:`_check_row` calls "unprotected" per-row, since it sits at or
        below the activation boundary, but which is a forged insert once it makes the range's count
        or ``rows_mac`` diverge). Requiring every present row to be validly signed keeps that a
        TAMPERED verdict rather than an amber late-commit WARNING (design 1A: an extra row with a
        *valid* MAC is a WARNING; anything else is tampering).
        """
        pairs: list[tuple[int, str | None]] = []
        all_signed = True
        for row in _iter_rows(conn, seal.from_id, seal.to_id):
            pairs.append((row.id, row.row_mac))
            if self._check_row(row, boundary, keyring, counters, findings) != "ok":
                all_signed = False

        key = keyring[seal.key_id]  # known: the chain walk already validated this seal's key_id
        recomputed = integrity.rows_mac(key, pairs)
        if recomputed == seal.rows_mac and len(pairs) == seal.row_count:
            return
        if all_signed and len(pairs) > seal.row_count:
            findings.append(
                Finding("warning", f"seal seq {seal.seq} covers {len(pairs)} rows but sealed "
                        f"{seal.row_count} — {len(pairs) - seal.row_count} valid-MAC late "
                        "commit(s). Widen grace or record long jobs via the own-transaction path.",
                        f"seal {seal.seq}")
            )
            counters.warning += 1
        else:
            findings.append(
                Finding("tampered",
                        f"seal seq {seal.seq} range ({seal.from_id}, {seal.to_id}] no longer "
                        "matches its rows_mac/row_count (rows deleted, inserted, or swapped)",
                        f"seal {seal.seq} ids {seal.from_id + 1}..{seal.to_id}")
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
            self._check_row(row, boundary, keyring, counters, findings)
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
                Finding("warning", f"the oldest unsealed row is {int(age)}s old (> "
                        f"{int(self.audit._unsealed_tail_max_age)}s) — the sealer looks stalled; "
                        "un-sealed rows are not deletion-protected.", "unsealed-tail")
            )
            counters.warning += 1

    def _check_row(
        self,
        row: Any,
        boundary: int | None,
        keyring: dict[str, Key],
        counters: _Counters,
        findings: list[Finding],
    ) -> str:
        """Verify one row's Layer-1 MAC and return its per-row verdict.

        ``"ok"`` is a genuine, validly-signed row; ``"unprotected"`` is a legacy NULL-MAC row at or
        below the activation boundary (written before the key existed — not an alarm);
        ``"tampered"`` is a modified row, or a NULL-MAC row past the boundary. The caller folds this
        into a range's late-commit-vs-tamper decision: only a range whose every present row is
        ``"ok"`` may carry surplus rows as valid-MAC late commits. Increments the matching counter
        and appends a finding for a violation."""
        if row.row_mac is None:
            if boundary is None or row.id <= boundary:
                counters.unprotected += 1
                return "unprotected"  # legacy row from before the key existed — not an alarm
            findings.append(
                Finding("tampered", f"row {row.id} has no row_mac but is after the activation "
                        "boundary (forged insert, or an instance writing without the key)",
                        f"row {row.id}")
            )
            counters.tampered += 1
            return "tampered"

        key = keyring.get(row.key_id)
        if key is None:
            raise VerifyError(
                f"row {row.id} was signed by unknown key_id {row.key_id!r} — add its secret to "
                "FIRM_AUDIT_KEYS."
            )
        expected = recompute_row_mac(key, row)
        if not hmac.compare_digest(expected, row.row_mac):
            findings.append(
                Finding("tampered", f"row {row.id} row_mac does not recompute (modified)",
                        f"row {row.id}")
            )
            counters.tampered += 1
            return "tampered"
        counters.ok += 1
        return "ok"

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
        ``checkpoint`` seal legitimately prunes the seals (and rows) at or below its ``to_id``, so
        an anchored seq at or below the checkpoint **floor** was pruned away, not truncated. That is
        safe to accept because the floor is set only by a key-signed checkpoint the chain walk
        already re-verified — an attacker without the key cannot raise the floor over a real
        anchored seal, and a wholesale drop-and-recreate leaves no surviving checkpoint (floor 0).
        Retention also exports the checkpoint to the anchor, so the newest anchor normally names the
        checkpoint itself; this clause only covers the residual case where that write was lost.
        """
        if anchor_path is None:
            return None, False, False
        anchor = _read_newest_anchor(anchor_path)
        if anchor is None:
            if seals:
                findings.append(
                    Finding("warning", f"anchor file {anchor_path!r} is missing or empty but seals "
                            "exist — the external truncation guard is not being written.", "anchor")
                )
                counters.warning += 1
            return None, True, False

        seq, seal_mac, sealed_at = anchor
        by_seq = {s.seq: s for s in seals}
        head_seq = max((s.seq for s in seals), default=0)
        matched = by_seq.get(seq)
        legitimately_pruned = matched is None and seq <= floor
        if not legitimately_pruned and (
            matched is None
            or not hmac.compare_digest(matched.seal_mac, seal_mac)
            or head_seq < seq
        ):
            findings.append(
                Finding("tampered", f"the newest anchor (seq {seq}) is not present at the head of "
                        "the stored chain — tail truncation or a drop-and-recreate.", f"seal {seq}")
            )
            counters.tampered += 1

        force_nonzero = False
        age = (now - sealed_at).total_seconds()
        if age > self.audit._anchor_max_age:
            findings.append(
                Finding("warning", f"the newest anchor is {int(age)}s old (> "
                        f"{int(self.audit._anchor_max_age)}s) — the anchor sink looks stalled; the "
                        "silently-truncatable window is growing.", "anchor")
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
        affected = "; ".join(
            f.identifier for f in findings if f.verdict == "tampered" and f.identifier
        )
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
            affected_identifiers=affected or None,
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
