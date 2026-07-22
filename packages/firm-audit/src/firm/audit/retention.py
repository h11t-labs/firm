"""Retention — opt-in, age-based pruning.

Unlike :mod:`firm.cache`'s expiry, this is never triggered by writes — ``AuditLog.record`` never
calls into this module. Pruning only happens via an explicit :meth:`Retention.run_once` call,
``firm-audit prune``, or an opted-in :class:`RetentionLoop`. The default ``max_age=None`` means
"keep forever": :meth:`run_once` is then a no-op.

**When sealing is active, pruning aligns to seal boundaries** (design "Retention integration").
Deleting a sealed row would otherwise read as tampering, so :meth:`run_once` only deletes rows in
ranges *fully covered by seals* and, having done so, appends a ``kind="checkpoint"`` seal that
records how far it pruned (``to_id = pruned_through_id``) and carries the chain forward. Verify
then skips row-recomputation at or below the newest checkpoint but still walks the seal chain
across it, and a NULL-MAC/missing row *above* the checkpoint is still a violation. Old covering
seals below the checkpoint are pruned too — the checkpoint's key-signed ``seal_mac`` is what
authorizes their absence.

**Retention refuses to prune what verify would call TAMPERED.** Before deleting a fully-expired
sealed range, :meth:`run_once` classifies it with :func:`~firm.audit.verify.range_is_prunable`,
which runs the *same* classifier the verifier runs (:func:`~firm.audit.verify.classify_range`:
recompute every row's ``row_mac`` from its content *and* the range's ``rows_mac``/``row_count``
against the seal). One classifier, two callers — so a range can never be a WARNING to verify and a
refusal to retention. A **TAMPERED** range (a deletion, a count-preserving swap, an invalid/missing
MAC, or an unverifiable seal) is **refused**: pruning stops at it, the checkpoint never advances
past it, its count lands on :attr:`Retention.last_refused_tampered`, and the refusal is routed
through ``on_error``. This closes the laundering hole where an attacker edits an old sealed row (a
plain ``UPDATE``, no key needed) and waits for it to age past ``max_age`` so a naive prune would
delete the evidence and checkpoint over it, after which verify reports OK. Because the boundary
stops at the first refused range, the tampered rows stay in place and every later run refuses again
until an operator intervenes.

A range whose *only* divergence from its seal is extra rows that **all carry valid row MACs** is a
**late commit**, not tampering (verify's amber WARNING, design 1A): a transaction that outran the
``grace`` window committed a genuine row into an already-sealed range. On real-concurrency backends
that actually happens — a writer racing the sealer's grace window — so retention must **not** refuse
it: a valid MAC in a sealed range is a latecomer, and refusing it would block pruning forever over a
benign event ("false alarms train people to ignore real ones"). Such a range is prunable, and the
late row is expired too (the whole range is past ``max_age``), so deleting it together with the
range and checkpointing over it destroys no evidence. The trade-off is a read cost: pre-prune
classification re-reads (keyset-paginated) every row it is about to delete. Ranges at or below the
checkpoint floor are already pruned and out of scope.

The checkpoint is exported to the anchor like any other seal, so a later ``verify --anchor`` never
mistakes the pruned-away anchored seal for a tail truncation.

**Retention needs the seal key.** The checkpoint it writes is a seal, so it must be signed by the
seal key that owns the chain. In a two-key deployment (a distinct ``FIRM_AUDIT_SEAL_KEY``) this
means pruning runs on a sealer-role host: a host with only the row key would sign the checkpoint
with the wrong key, so :meth:`run_once` detects that (the chain head was signed by a key other than
this host's seal key), refuses the whole aligned prune loudly through ``on_error``, sets
:attr:`last_refused_no_seal_key`, and leaves the table untouched. In single-key mode the seal key
*is* the row key, so this never fires and pruning is unchanged.

This gives retention a hidden dependency on sealer liveness (design outside voice #6): with a
stalled sealer, nothing past the last seal is prunable and the table grows despite ``max_age``.
That is made **loud, not silent** (review D15): :meth:`run_once` records the count of
expired-but-unsealed rows it had to skip on :attr:`last_skipped_unsealed`, and a skip count over
:data:`_SKIP_ALERT_THRESHOLD` is routed through ``on_error``. Without a key (or before the first
seal) sealing is inactive and pruning behaves exactly as it did before tamper-evidence existed.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select

from .._core.clock import now_utc
from .._core.database import snapshot_transaction
from .._core.dialects import get_dialect
from .._core.poller import InterruptiblePoller
from . import integrity, schema
from .verify import range_is_prunable, seal_is_intact

if TYPE_CHECKING:
    from .log import AuditLog

_audits = schema.audit_events
_seals = schema.seals

# Rows deleted per transaction. Batching keeps each delete short (no long-held locks over a
# large table) and lets concurrent pruners interleave instead of fighting.
_BATCH_SIZE = 1000

# An expired-but-unsealed backlog this large means the sealer has stalled badly enough that
# retention can no longer keep the table bounded; route it through ``on_error`` so it is not lost
# in a return value nobody reads (design review D15).
_SKIP_ALERT_THRESHOLD = 1000


class Retention:
    def __init__(self, audit: AuditLog) -> None:
        self.audit = audit
        #: Expired rows the last :meth:`run_once` could not delete because they were not yet
        #: sealed (sealing path). Surfaced by ``firm-audit prune`` and, if large, ``on_error``.
        self.last_skipped_unsealed = 0
        #: Sealed ranges the last :meth:`run_once` refused to prune because they no longer verify
        #: against their seal (a sealed row was edited/deleted/inserted). Pruning stops at the first
        #: such range so its evidence is preserved; each refusal also routes through ``on_error``.
        self.last_refused_tampered = 0
        #: Set when the last :meth:`run_once` refused the *whole* aligned prune because this host
        #: lacks the seal key that signs the chain (a two-key deployment running retention off a
        #: non-sealer host). Writing a checkpoint here would sign it with the wrong key; refuse
        #: loudly instead. Pruning must run on a sealer-role host.
        self.last_refused_no_seal_key = False

    def run_once(self) -> int:
        """Delete rows older than ``max_age`` seconds, in batches; return how many were deleted. A
        no-op (returns 0) when ``max_age`` is ``None`` (the default — keep forever).

        With sealing active (a key is configured and at least one seal exists) this prunes only
        fully-sealed ranges and writes a checkpoint seal (see the module docstring); otherwise it
        deletes by age exactly as before.
        """
        self.last_skipped_unsealed = 0
        self.last_refused_tampered = 0
        self.last_refused_no_seal_key = False
        max_age = self.audit.max_age
        if max_age is None:
            return 0
        cutoff = now_utc() - timedelta(seconds=max_age)
        if self._sealing_active():
            return self._run_aligned(cutoff)
        return self._run_plain(cutoff)

    def _sealing_active(self) -> bool:
        """Sealing is active — and pruning must take the aligned, checkpoint-writing path — whenever
        **any seal exists**, regardless of which keys *this* host happens to carry (Bug #7).

        The old gate returned False when ``audit._key`` (the row key) was None, so a seal-key-only
        host — a two-key sealer/verifier that carries ``FIRM_AUDIT_SEAL_KEY`` but not
        ``FIRM_AUDIT_KEY`` — fell through to :meth:`_run_plain` and destroyed sealed rows by
        age with no checkpoint (a covering seal left short of rows → false TAMPERED). Keying the
        decision on the seals themselves means sealed rows are *never* plain-pruned: a host missing
        the seal key that owns the chain refuses loudly in :meth:`_run_aligned` instead."""
        with self.audit.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(_seals)).scalar_one() > 0

    def _run_plain(self, cutoff) -> int:
        """Delete every row older than ``cutoff``, in ``FOR UPDATE SKIP LOCKED`` batches — the
        pre-tamper-evidence behavior (the same pattern as ``firm.channel.messages.trim_old``) so
        two pruners split the work instead of blocking on each other's rows."""
        engine = self.audit.engine
        dialect = get_dialect(engine)
        total = 0
        while True:
            with dialect.begin_claim_tx(engine) as conn:
                stmt = dialect.with_skip_locked(
                    select(_audits.c.id).where(_audits.c.created_at < cutoff).limit(_BATCH_SIZE)
                )
                ids = [row.id for row in conn.execute(stmt)]
                if ids:
                    conn.execute(delete(_audits).where(_audits.c.id.in_(ids)))
            total += len(ids)
            if len(ids) < _BATCH_SIZE:
                return total

    def _run_aligned(self, cutoff) -> int:
        """Prune only fully-sealed, fully-expired, *non-tampered* ranges, then checkpoint ahead —
        **re-verify, delete, and checkpoint all in one transaction** (Bug #4).

        Finds the highest sealed boundary all of whose ranges predate ``cutoff`` **and are prunable
        against their seal** (verify OK, or benign valid-MAC late commits — never TAMPERED), deletes
        through it, writes a checkpoint recording it, and prunes the now-subsumed covering seals
        below it — atomically, so a crash mid-prune can never leave a covering seal short of rows
        with no checkpoint (which verify would read as a false TAMPERED), and a row modified
        after the pre-prune re-verify but before the delete cannot be laundered (the re-verify and
        the delete see one snapshot / are guarded by one write lock; see
        :func:`~firm._core.database.snapshot_transaction`). A TAMPERED range is refused (design
        "Retention integration"): pruning stops there so a tampered-then-expired row is surfaced,
        never laundered by deletion. Records the expired-but-unsealed rows left behind, and exports
        the checkpoint to the anchor so a pruned seal is not later read as truncation.
        """
        engine = self.audit.engine
        with engine.connect() as conn:
            seals = conn.execute(select(_seals).order_by(_seals.c.seq)).all()

        # The aligned path WRITES a checkpoint seal, which must be signed by the seal key that owns
        # this chain. A host that lacks that key — a two-key deployment's non-sealer host (row key
        # only), or a misconfigured host with no key at all now that :meth:`_sealing_active` reads
        # the seals, not this host's row key (Bug #7) — would sign the checkpoint with the wrong key
        # or have no key to sign with. Detect it by the head seal's signer: if it is not this host's
        # seal key, refuse the whole aligned prune loudly and leave the table untouched, so sealed
        # rows are never destroyed. In single-key mode the head is signed by the one key (== the
        # seal key), so this never fires. Pruning must run on a sealer-role host.
        seal_key = self.audit._seal_key
        if seals and (seal_key is None or seals[-1].key_id != seal_key.id):
            self._refuse_no_seal_key(seals[-1])
            return 0

        floor = max((s.to_id for s in seals if s.kind == "checkpoint"), default=0)
        max_sealed = max((s.to_id for s in seals), default=0)

        self.last_skipped_unsealed = self._count_expired_unsealed(max_sealed, cutoff)
        self._alert_if_over_threshold()

        checkpoint = self._prune_aligned_atomic(seals, floor, max_sealed, cutoff)
        if checkpoint is None:
            return 0  # nothing new is both sealed, fully expired, and still verifying
        deleted, seq, to_id, seal_mac, sealed_at = checkpoint
        # Advance the external anchor to the checkpoint (best-effort, same sink as the sealer): the
        # checkpoint prunes the seals below it, so an anchor still naming a pruned seq would read as
        # tail truncation on the next ``verify --anchor``. Reusing the sealer's emit keeps the
        # on_error routing and the "a broken sink never fails the operation" contract (review 3A).
        # The checkpoint's ``to_id`` is its ``pruned_through`` (== the floor it records), so it
        # never raises the anchor's max-coverage watermark past a still-present covering seal.
        self.audit.sealer._emit_anchor(seq=seq, to_id=to_id, seal_mac=seal_mac, sealed_at=sealed_at)
        return deleted

    def _prune_aligned_atomic(
        self, seals, floor: int, boundary: int, cutoff
    ) -> tuple[int, int, int, str, datetime] | None:
        """One transaction: re-verify the prunable boundary, delete through it, and write the
        checkpoint — the atomic core of :meth:`_run_aligned` (Bug #4). Returns
        ``(deleted, seq, to_id, seal_mac, sealed_at)`` for the checkpoint written, or ``None`` when
        nothing is prunable (no delete, no checkpoint).

        Iterates ranges in id order and stops at the first that either still holds a row newer than
        ``cutoff`` (young — so a young range never lets an older one past it) or is refused by
        :func:`~firm.audit.verify.range_is_prunable` (the *same* classifier the verifier runs: OK or
        benign valid-MAC late commit is prunable, TAMPERED refused). A refused range is counted on
        :attr:`last_refused_tampered` and routed through ``on_error``; because the boundary stops at
        it, the checkpoint never advances past it, so the evidence is preserved and every later run
        refuses it again. Because the re-verify, the delete, and the checkpoint insert (plus the
        subsumed-seal cleanup) all commit or roll back together, a crash leaves the range fully
        intact — verify still reads OK, never a covering seal short of rows. ``boundary`` is the
        activation boundary (highest sealed id) the classifier uses to tell a legacy NULL-MAC row
        from a forged one, matching :meth:`~firm.audit.verify.Verifier._floor_and_boundary`."""
        covering = [s for s in seals if s.kind == "seal" and s.to_id > floor]
        by_seq = {s.seq: s for s in seals}
        has_checkpoint = any(s.kind == "checkpoint" for s in seals)
        keyring = self.audit.verifier.keyring
        seal_keyring = self.audit.verifier.seal_keyring
        seal_key = self.audit._seal_key
        assert seal_key is not None  # guarded by the seal-key refusal in _run_aligned
        with snapshot_transaction(self.audit.engine, write=True) as conn:
            pruned_through = floor
            for seal in covering:
                newest = conn.execute(
                    select(func.max(_audits.c.created_at)).where(
                        _audits.c.id > pruned_through, _audits.c.id <= seal.to_id
                    )
                ).scalar()
                if newest is not None and newest >= cutoff:
                    break  # a young range: nothing at or beyond it is prunable yet
                # The seal's OWN integrity (its ``seal_mac`` + ``prev_mac`` chain linkage), then the
                # rows under it. Checking the seal first (cheap, seals-table only) is what stops a
                # tampered ``seal_mac`` from being laundered by a prune while its rows still
                # reproduce ``rows_mac`` — :func:`range_is_prunable` alone never re-checks the seal
                # (Bug A). Either failing is TAMPERED: refuse, so the checkpoint never advances past
                # the evidence and every later run refuses it again until an operator investigates.
                if not seal_is_intact(
                    seal, by_seq.get(seal.seq - 1), has_checkpoint, seal_keyring
                ) or not range_is_prunable(conn, seal, boundary, keyring, seal_keyring):
                    self._refuse_tampered(seal)
                    break  # refuse to prune tampered evidence; stop so the checkpoint can't skip it
                pruned_through = seal.to_id
            if pruned_through <= floor:
                return None
            deleted = conn.execute(
                delete(_audits).where(_audits.c.id > floor, _audits.c.id <= pruned_through)
            ).rowcount
            seq, seal_mac, sealed_at = self._append_checkpoint(
                conn, seal_key, from_id=floor, to_id=pruned_through
            )
        return deleted, seq, pruned_through, seal_mac, sealed_at

    def _append_checkpoint(
        self, conn, key, *, from_id: int, to_id: int
    ) -> tuple[int, str, datetime]:
        """Insert the checkpoint seal at the head and prune the covering seals it subsumes, on the
        caller's (already-open, atomic) ``conn`` (Bug #4 — this must share the prune's transaction).

        The checkpoint covers ``(from_id, to_id]`` with no live rows left (``rows_mac`` over the
        empty set, no gaps), takes the next ``seq``, and chains ``prev_mac`` to the current head —
        verify reads its ``to_id`` as the new floor. Covering seals with ``to_id <= to_id`` and any
        earlier checkpoint are then deleted (their coverage is subsumed); the checkpoint's
        ``seal_mac`` is what lets verify accept the missing front."""
        head = conn.execute(select(_seals).order_by(_seals.c.seq.desc()).limit(1)).first()
        assert head is not None  # a seal exists (guarded by _sealing_active)
        seq = head.seq + 1
        prev_mac = head.seal_mac
        sealed_at = now_utc()
        rows_mac = integrity.rows_mac(key, [])
        seal_mac = integrity.seal_mac(
            key,
            seq=seq,
            kind="checkpoint",
            from_id=from_id,
            to_id=to_id,
            row_count=0,
            rows_mac=rows_mac,
            prev_mac=prev_mac,
            sealed_at=sealed_at,
            gaps="",
        )
        conn.execute(
            _seals.insert().values(
                seq=seq,
                kind="checkpoint",
                from_id=from_id,
                to_id=to_id,
                row_count=0,
                rows_mac=rows_mac,
                prev_mac=prev_mac,
                seal_mac=seal_mac,
                sealed_at=sealed_at,
                key_id=key.id,
                gap_ranges=None,
            )
        )
        conn.execute(
            delete(_seals).where(
                _seals.c.seq != seq,
                _seals.c.to_id <= to_id,
            )
        )
        return seq, seal_mac, sealed_at

    def _count_expired_unsealed(self, max_sealed: int, cutoff) -> int:
        """Expired rows past the sealed frontier (``id > max_sealed``) — a stalled-sealer backlog,
        measured against the highest sealed id rather than the prune boundary so a range that was
        refused (tampered) or left young is not miscounted as unsealed."""
        with self.audit.engine.connect() as conn:
            return conn.execute(
                select(func.count())
                .select_from(_audits)
                .where(_audits.c.id > max_sealed, _audits.c.created_at < cutoff)
            ).scalar_one()

    def _refuse_tampered(self, seal) -> None:
        """Record and loudly surface a sealed range retention refused to prune because it no longer
        verifies. The refusal (not the deletion) is the alarm — the tampered rows stay in place for
        ``firm-audit verify`` to report and for an operator to investigate (design D15 voice)."""
        self.last_refused_tampered += 1
        self.audit.on_error(
            RuntimeError(
                f"audit retention REFUSED to prune sealed range ({seal.from_id}, {seal.to_id}] "
                f"(seal seq {seal.seq}): it no longer verifies — a sealed row or the seal itself "
                "(its seal_mac / prev_mac chain link) was edited, deleted, or inserted. Retention "
                "will not delete tampered evidence; preserve the database and run "
                "`firm-audit verify --full` to investigate."
            )
        )

    def _refuse_no_seal_key(self, head) -> None:
        """Record and loudly surface an aligned prune refused because this host lacks the seal key
        that signs the chain (a two-key deployment running retention off a non-sealer host). Nothing
        is deleted; the refusal routes through ``on_error`` mirroring :meth:`_refuse_tampered`."""
        self.last_refused_no_seal_key = True
        seal_key = self.audit._seal_key
        self.audit.on_error(
            RuntimeError(
                f"audit retention REFUSED to prune: the seal chain head (seq {head.seq}) was "
                f"signed by key_id {head.key_id!r}, but this host's seal key is "
                f"{seal_key.id if seal_key else None!r}. Retention writes a checkpoint seal and so "
                "needs the seal key (FIRM_AUDIT_SEAL_KEY). In a two-key deployment, run pruning on "
                "a sealer-role host that has it — signing a checkpoint with the row key would "
                "produce a seal verify rejects."
            )
        )

    def _alert_if_over_threshold(self) -> None:
        if self.last_skipped_unsealed > _SKIP_ALERT_THRESHOLD:
            self.audit.on_error(
                RuntimeError(
                    f"audit retention skipped {self.last_skipped_unsealed} expired but UNSEALED "
                    "rows — the sealer looks stalled and the table cannot be pruned past the last "
                    "seal. Check the SealLoop / `firm-audit seal`."
                )
            )


class RetentionLoop(InterruptiblePoller):
    """Optional background loop that runs pruning on a timer. Off by default."""

    def __init__(
        self,
        retention: Retention,
        interval: float = 3600.0,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        super().__init__(interval, name="audit-retention", on_error=on_error)
        self.retention = retention

    def poll(self) -> int:
        return self.retention.run_once()
