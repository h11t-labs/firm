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

**Retention only prunes what verifies.** Before deleting a fully-expired sealed range,
:meth:`run_once` re-verifies it (:func:`~firm.audit.verify.range_is_intact`: recompute every row's
``row_mac`` from its content *and* the range's ``rows_mac``/``row_count`` against the seal) — the
same recompute the verifier runs. A range that no longer verifies is **refused**: pruning stops at
it, the checkpoint never advances past it, its count lands on
:attr:`Retention.last_refused_tampered`, and the refusal is routed through ``on_error``. This closes
the laundering hole where an attacker edits an old sealed row (a plain ``UPDATE``, no key needed)
and waits for it to age past ``max_age`` so a naive prune would delete the evidence and checkpoint
over it, after which verify reports OK. Because the boundary stops at the first refused range, the
tampered rows stay in place and every later run refuses again until an operator intervenes. The
trade-off is a read cost: pre-prune verification re-reads (keyset-paginated) every row it is about
to delete. Ranges at or below the checkpoint floor are already pruned and out of scope.

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
from .._core.dialects import get_dialect
from .._core.poller import InterruptiblePoller
from . import integrity, schema
from .verify import range_is_intact

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
        if self.audit._key is None:
            return False
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
        """Prune only fully-sealed, fully-expired, *still-verifying* ranges, then checkpoint ahead.

        Finds the highest sealed boundary all of whose ranges predate ``cutoff`` **and still
        verify against their seal**, deletes through it, writes a checkpoint recording it, and
        prunes the now-subsumed covering seals below it. A range that no longer verifies is refused
        (design "Retention integration"): pruning stops there so a tampered-then-expired row is
        surfaced, never laundered by deletion. Records the expired-but-unsealed rows left behind,
        and exports the checkpoint to the anchor so a pruned seal is not later read as truncation.
        """
        engine = self.audit.engine
        with engine.connect() as conn:
            seals = conn.execute(select(_seals).order_by(_seals.c.seq)).all()

        # The aligned path WRITES a checkpoint seal, which must be signed by the seal key that owns
        # this chain. In a two-key deployment, retention on a non-sealer host (row key only) would
        # sign the checkpoint with the row key — a checkpoint a proper verifier rejects (the row
        # key is not a seal signer), laundering nothing but also pruning nothing safely. Detect it
        # by the head seal's signer: if it is not this host's seal key, refuse the whole aligned
        # prune loudly and leave the table untouched. In single-key mode the head is signed by the
        # one key (== the seal key), so this never fires. Pruning must run on a sealer-role host.
        seal_key = self.audit._seal_key
        if seals and seal_key is not None and seals[-1].key_id != seal_key.id:
            self._refuse_no_seal_key(seals[-1])
            return 0

        floor = max((s.to_id for s in seals if s.kind == "checkpoint"), default=0)
        max_sealed = max((s.to_id for s in seals), default=0)
        pruned_through = self._highest_prunable_boundary(seals, floor, cutoff)

        self.last_skipped_unsealed = self._count_expired_unsealed(max_sealed, cutoff)
        self._alert_if_over_threshold()

        if pruned_through <= floor:
            return 0  # nothing new is both sealed, fully expired, and still verifying

        deleted = self._delete_through(floor, pruned_through)
        seq, seal_mac, sealed_at = self._write_checkpoint(
            seals, from_id=floor, to_id=pruned_through
        )
        # Advance the external anchor to the checkpoint (best-effort, same sink as the sealer): the
        # checkpoint prunes the seals below it, so an anchor still naming a pruned seq would read as
        # tail truncation on the next ``verify --anchor``. Reusing the sealer's emit keeps the
        # on_error routing and the "a broken sink never fails the operation" contract (review 3A).
        self.audit.sealer._emit_anchor(seq=seq, seal_mac=seal_mac, sealed_at=sealed_at)
        return deleted

    def _highest_prunable_boundary(self, seals, floor: int, cutoff) -> int:
        """The highest covering-seal ``to_id`` such that every range from the floor up to it is both
        fully older than ``cutoff`` *and* still verifies against its seal.

        Iterates ranges in id order and stops at the first that either still holds a row newer than
        ``cutoff`` (young — so a young range never lets an older one past it) or fails
        re-verification (tampered — a sealed row was edited/deleted/inserted). Re-verification is
        :func:`~firm.audit.verify.range_is_intact`, the same recompute the verifier runs, so
        "retention only prunes what verifies." A refused range is counted on
        :attr:`last_refused_tampered` and routed through ``on_error``; because the boundary stops at
        it, the checkpoint never advances past it, so the evidence is preserved and every later run
        refuses it again (it is never laundered by deletion). Ranges at or below ``floor`` are
        already pruned and out of scope."""
        covering = [s for s in seals if s.kind == "seal" and s.to_id > floor]
        keyring = self.audit.verifier.keyring
        seal_keyring = self.audit.verifier.seal_keyring
        pruned_through = floor
        with self.audit.engine.connect() as conn:
            for seal in covering:
                newest = conn.execute(
                    select(func.max(_audits.c.created_at)).where(
                        _audits.c.id > pruned_through, _audits.c.id <= seal.to_id
                    )
                ).scalar()
                if newest is not None and newest >= cutoff:
                    break  # a young range: nothing at or beyond it is prunable yet
                if not range_is_intact(conn, seal, keyring, seal_keyring):
                    self._refuse_tampered(seal)
                    break  # refuse to prune tampered evidence; stop so the checkpoint can't skip it
                pruned_through = seal.to_id
        return pruned_through

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
                f"(seal seq {seal.seq}): it no longer verifies against its seal — a sealed row was "
                "edited, deleted, or inserted. Retention will not delete tampered evidence; "
                "preserve the database and run `firm-audit verify --full` to investigate."
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

    def _delete_through(self, floor: int, pruned_through: int) -> int:
        engine = self.audit.engine
        dialect = get_dialect(engine)
        total = 0
        while True:
            with dialect.begin_claim_tx(engine) as conn:
                stmt = dialect.with_skip_locked(
                    select(_audits.c.id)
                    .where(_audits.c.id > floor, _audits.c.id <= pruned_through)
                    .limit(_BATCH_SIZE)
                )
                ids = [row.id for row in conn.execute(stmt)]
                if ids:
                    conn.execute(delete(_audits).where(_audits.c.id.in_(ids)))
            total += len(ids)
            if len(ids) < _BATCH_SIZE:
                return total

    def _write_checkpoint(self, seals, *, from_id: int, to_id: int) -> tuple[int, str, datetime]:
        """Append the checkpoint seal at the head and prune the covering seals it subsumes.

        The checkpoint covers ``(from_id, to_id]`` with no live rows left (``rows_mac`` over the
        empty set), takes the next ``seq``, and chains ``prev_mac`` to the current head — verify
        reads its ``to_id`` as the new floor. Covering seals with ``to_id <= to_id`` and any earlier
        checkpoint are then deleted (their coverage is subsumed); the checkpoint's ``seal_mac`` is
        what lets verify accept the missing front. Returns ``(seq, seal_mac, sealed_at)`` so the
        caller can export the new head to the anchor.
        """
        key = self.audit._seal_key
        assert key is not None  # guarded by _sealing_active (seal key defaults to the row key)
        engine = self.audit.engine
        with get_dialect(engine).begin_claim_tx(engine) as conn:
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
                )
            )
            conn.execute(
                delete(_seals).where(
                    _seals.c.seq != seq,
                    _seals.c.to_id <= to_id,
                )
            )
        return seq, seal_mac, sealed_at

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
