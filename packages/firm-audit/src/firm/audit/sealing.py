"""Layer 2 — asynchronous seals over ranges of sealed rows.

Layer 1 (:mod:`.events`) makes every row self-authenticate, but a *deleted* row simply isn't
there to check. Sealing closes that hole without reintroducing the coordination a synchronous
hash chain would need (see the design's "Why not the obvious design"): a background
:class:`SealLoop` periodically walks the rows committed since the last seal and writes one
chained ``firm_audit_seals`` block over them. Existence and ordering are protected by the seal;
the write path stays lock-free and multi-writer.

Three properties carry the design:

* **No election.** Every instance may run the sealer. The unique index on ``seq`` is the
  arbiter — if two sealers race, one INSERT wins and the loser catches the violation, rolls
  back, and retries next tick. Nothing coordinates beyond that one constraint, so it is
  portable to SQLite/MySQL/Postgres with no extra infrastructure.

* **The grace window handles out-of-order commits.** A row's ``created_at`` is stamped at
  insert, so only rows older than ``grace`` are eligible to seal; as long as ``grace`` exceeds
  the longest audit-recording transaction (plus inter-instance clock skew), every row is
  committed and visible before its id range is sealed. Ordering is by ``id``, never by
  ``created_at`` — clock skew widens the needed ``grace`` but can never corrupt the chain.

* **Two-phase rollout (design review D13).** Key presence and sealing are separate switches
  because key-enablement is never atomic across a fleet. Phase 1 deploys ``FIRM_AUDIT_KEY``
  everywhere (rows start carrying MACs); phase 2 enables sealing once every writer carries the
  key. The initial backlog drain seals everything from the beginning of the table (``from_id``
  0), so pre-existing legacy rows — written before the key existed and carrying no ``row_mac`` —
  are sealed too, hashed with the explicit ``nomac`` marker so their deletion is still
  detectable (review 5A). That drain is *batched* (below), so it becomes several seals rather
  than one: the **activation boundary** is therefore the highest sealed id, not seq 1's
  ``to_id`` (:meth:`~firm.audit.verify.Verifier._floor_and_boundary` reads it as
  ``max(to_id)``). Verify treats a NULL-MAC row at or below the boundary as *unprotected
  (legacy)* and one above it as *tampered* (a configured writer never emits a NULL MAC, so a
  missing one after activation is config drift or a forged insert).
  :class:`~firm.audit.log.AuditLog` restates this deploy-key-first order when sealing is enabled.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import Connection, func, select
from sqlalchemy.exc import IntegrityError

from .._core.clock import now_utc
from .._core.dialects import get_dialect
from .._core.poller import InterruptiblePoller
from . import integrity, schema
from .integrity import Key, canonical_created_at

if TYPE_CHECKING:
    from .log import AuditLog

_audits = schema.audit_events
_seals = schema.seals

#: The ``prev_mac`` of seal ``seq == 1``; there is no earlier seal to chain to.
_GENESIS = "genesis"


class Sealer:
    """Writes chained seals over the rows committed since the last seal.

    Reads its configuration from the owning :class:`~firm.audit.log.AuditLog`
    (:attr:`~firm.audit.log.AuditLog.grace`, :attr:`~firm.audit.log.AuditLog.seal_batch_size`,
    the key, and the anchor sink), exactly as :class:`~firm.audit.retention.Retention` reads
    ``max_age`` — so :meth:`run_once`, ``firm-audit seal``, and :class:`SealLoop` all share one
    implementation.
    """

    def __init__(self, audit: AuditLog) -> None:
        self.audit = audit

    def run_once(self) -> int:
        """Seal the backlog of committed, past-grace rows and return how many rows were sealed.

        A no-op (returns 0) when no seal key is configured — sealing needs a key to sign, and
        without one there is nothing to protect (design review D13; a startup hint is emitted when
        sealing is enabled but the key is missing). The seal key is the two-key split's signer
        (:attr:`~firm.audit.log.AuditLog._seal_key`): a distinct ``FIRM_AUDIT_SEAL_KEY`` when
        configured, otherwise the row key, so single-key deployments are unchanged.

        Drains the whole backlog in successive seals of at most ``seal_batch_size`` rows, so a
        sealer that has been down does not build one monster transaction (review 7A). If a
        concurrent sealer takes the next ``seq`` first, the resulting :class:`IntegrityError` is
        swallowed and the work resumes on the next tick — the loser never crashes and never
        double-seals.
        """
        key = self.audit._seal_key
        if key is None:
            return 0
        batch_size = self.audit.seal_batch_size
        total = 0
        while True:
            try:
                sealed = self._seal_next_batch(key)
            except IntegrityError:
                # A concurrent sealer already took this ``seq`` and advanced the chain; roll back
                # (done by the transaction context) and let the next tick resume from the new hwm.
                return total
            if sealed is None:
                return total
            seq, seal_mac, sealed_at, row_count = sealed
            self._emit_anchor(seq=seq, seal_mac=seal_mac, sealed_at=sealed_at)
            total += row_count
            if row_count < batch_size:
                return total

    def _seal_next_batch(self, key: Key) -> tuple[int, str, datetime, int] | None:
        """Seal one batch in a single short transaction; ``None`` when there is nothing to seal.

        Reads the high-water mark (the latest seal's ``to_id``, or 0 for the very first seal —
        which therefore starts at the beginning of the table, sealing pre-existing legacy rows),
        selects up to ``seal_batch_size`` eligible rows in id order, and inserts the chained
        seal. Raises :class:`IntegrityError` when the chosen ``seq`` was already taken by a racing
        sealer. Returns ``(seq, seal_mac, sealed_at, row_count)`` on success so the caller can
        emit the anchor *after* the seal has committed.
        """
        cutoff = now_utc() - timedelta(seconds=self.audit.grace)
        engine = self.audit.engine
        # ``begin_claim_tx`` takes SQLite's write lock up front (``BEGIN IMMEDIATE``) so two
        # sealers that read-then-write never deadlock on lock promotion; on Postgres/MySQL it is
        # an ordinary transaction and the ``seq`` unique constraint is what arbitrates the race
        # (mirrors :mod:`.retention`'s claim/delete loop).
        with get_dialect(engine).begin_claim_tx(engine) as conn:
            last = conn.execute(select(_seals).order_by(_seals.c.seq.desc()).limit(1)).first()
            if last is None:
                hwm, seq, prev_mac = 0, 1, _GENESIS
            else:
                # The high-water mark is the highest *sealed id*, read as ``max(to_id)`` — not the
                # head seal's own ``to_id``. Retention writes a ``checkpoint`` seal at the head
                # (highest ``seq``) whose ``to_id`` is the low ``pruned_through_id``, so trusting
                # the head's ``to_id`` would regress the mark and re-seal already-sealed ranges.
                # ``seq``/``prev_mac`` still come from the head so the chain stays dense and linked.
                # Without checkpoints ``max(to_id)`` equals the head's ``to_id``, so this is inert
                # for the ordinary sealing path.
                max_to_id = conn.execute(select(func.max(_seals.c.to_id))).scalar_one()
                hwm, seq, prev_mac = max_to_id, last.seq + 1, last.seal_mac

            rows = conn.execute(
                select(_audits.c.id, _audits.c.row_mac)
                .where(_audits.c.id > hwm, _audits.c.created_at <= cutoff)
                .order_by(_audits.c.id)
                .limit(self.audit.seal_batch_size)
            ).all()
            if not rows:
                return None

            return self._insert_seal(
                conn,
                key,
                seq=seq,
                from_id=hwm,
                to_id=rows[-1].id,
                rows=[(row.id, row.row_mac) for row in rows],
                prev_mac=prev_mac,
            )

    def _insert_seal(
        self,
        conn: Connection,
        key: Key,
        *,
        seq: int,
        from_id: int,
        to_id: int,
        rows: list[tuple[int, str | None]],
        prev_mac: str,
        kind: str = "seal",
    ) -> tuple[int, str, datetime, int]:
        """Compute ``rows_mac``/``seal_mac`` and insert one seal row; return its identity.

        ``rows`` are the ``(id, row_mac)`` pairs actually present in ``(from_id, to_id]`` in id
        order — rollback id-gaps are harmless because the seal hashes what exists rather than
        assuming id continuity, and a NULL ``row_mac`` is folded in with the ``nomac`` marker so
        even an unsigned row's deletion changes the seal (review 5A).
        """
        sealed_at = now_utc()
        rows_mac = integrity.rows_mac(key, rows)
        row_count = len(rows)
        seal_mac = integrity.seal_mac(
            key,
            seq=seq,
            kind=kind,
            from_id=from_id,
            to_id=to_id,
            row_count=row_count,
            rows_mac=rows_mac,
            prev_mac=prev_mac,
            sealed_at=sealed_at,
        )
        conn.execute(
            _seals.insert().values(
                seq=seq,
                kind=kind,
                from_id=from_id,
                to_id=to_id,
                row_count=row_count,
                rows_mac=rows_mac,
                prev_mac=prev_mac,
                seal_mac=seal_mac,
                sealed_at=sealed_at,
                key_id=key.id,
            )
        )
        return seq, seal_mac, sealed_at, row_count

    def _emit_anchor(self, *, seq: int, seal_mac: str, sealed_at: datetime) -> None:
        """Ship the freshly committed chain head to the external anchor sink — best effort.

        Appends ``"<sealed_at> <seq> <seal_mac>"`` to the anchor file (if a path is configured)
        and/or hands the same tuple to the ``on_anchor`` callback (design Layer 3). This runs
        *after* the seal has committed, and every sink is wrapped so a failure — disk full, a
        vanished path, a callback that raises — routes to ``on_error`` and never crashes or rolls
        back the seal (review 3A). A stalled sink stays visible: verify reports the newest
        anchor's age.
        """
        path = self.audit._anchor_path
        if path is not None:
            try:
                line = f"{canonical_created_at(sealed_at)} {seq} {seal_mac}\n"
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(line)
            except Exception as exc:  # best-effort: a broken sink must not lose the seal
                self.audit.on_error(exc)
        callback = self.audit._on_anchor
        if callback is not None:
            try:
                callback(seq, seal_mac, sealed_at)
            except Exception as exc:
                self.audit.on_error(exc)


class SealLoop(InterruptiblePoller):
    """Optional background loop that runs the sealer on a timer. Off by default.

    The same :class:`~firm.audit.retention.RetentionLoop` pattern: a third
    :class:`~firm._core.poller.InterruptiblePoller`. Enable it with
    ``AuditLog(..., background_sealing=True)`` or run it out of ``firm-audit seal``.
    """

    def __init__(
        self,
        sealer: Sealer,
        interval: float = 60.0,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        super().__init__(interval, name="audit-sealer", on_error=on_error)
        self.sealer = sealer

    def poll(self) -> int:
        return self.sealer.run_once()
