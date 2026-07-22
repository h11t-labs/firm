"""Asynchronous independent seals and the explicit activation marker.

Layer 1 authenticates each keyed event but cannot prove that a row still exists. Layer 2 closes
that gap with independent seals over settled id ranges. Each seal signs only its own range and the
``(id, row_mac)`` pairs inside it; ordering comes from id-contiguity and deletion memory comes from
the optional append-only anchor, not from a predecessor chain.

The first sealer pass writes one signed ``activation`` record whose boundary is the highest event
id already outside the grace window. Younger rows stay above the boundary and are sealed later.
Racing sealers choose the same ``from_id``; the unique index on that coordinate is the portable
arbiter.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import Connection, func, or_, select
from sqlalchemy.exc import IntegrityError

from .._core.clock import now_utc
from .._core.dialects import get_dialect
from .._core.poller import InterruptiblePoller
from . import integrity, schema
from .integrity import Key
from .verify import (
    _format_anchor_event,
    _read_anchor,
    _record_matches_anchor,
    load_seal_records,
    seal_is_intact,
)

if TYPE_CHECKING:
    from .log import AuditLog

_audits = schema.audit_events
_seals = schema.seals

# A real event id is positive. Reserving this unique ``from_id`` gives concurrent activation
# attempts the same database arbiter as concurrent range seals without adding another table/index.
_ACTIVATION_FROM_ID = -1


class Sealer:
    """Activate sealing once, then write independent seals over settled rows."""

    def __init__(self, audit: AuditLog) -> None:
        self.audit = audit

    def run_once(self) -> int:
        """Activate if needed, seal the eligible backlog, and return the sealed row count.

        Activation and each covering seal commit in their own short transaction. Before writing,
        an anchor-heal pass re-emits any intact committed record absent from the configured file.
        New anchor emission follows the commit and is best-effort; a sink failure is routed through
        ``on_error`` and leaves the signed database record intact. A racing insert loses via the
        unique ``from_id`` constraint and is retried on a later poll.
        """
        key = self.audit._seal_key
        if key is None:
            return 0
        if not self._signer_matches_existing_records(key):
            return 0

        if self.audit._anchor_path is not None:
            self._heal_anchor()

        try:
            activation = self._ensure_activation(key)
        except IntegrityError:
            return 0
        if activation is not None:
            boundary, mac, at = activation
            self._emit_anchor(kind="activation", from_id=None, to_id=boundary, mac=mac, at=at)

        total = 0
        while True:
            try:
                sealed = self._seal_next_batch(key)
            except IntegrityError:
                return total
            if sealed is None:
                return total
            from_id, to_id, mac, at, row_count = sealed
            self._emit_anchor(kind="seal", from_id=from_id, to_id=to_id, mac=mac, at=at)
            total += row_count
            if row_count < self.audit.seal_batch_size:
                return total

    def _heal_anchor(self) -> None:
        """Re-append every intact committed record missing from the configured anchor.

        Malformed input, including a partial tail, is deliberately ignored here: exact parsed
        events are enough to identify what is absent, and appending the canonical replacement turns
        a partial tail into a reconstructible benign fragment without rewriting prior bytes.
        """
        path = self.audit._anchor_path
        assert path is not None
        try:
            anchor = _read_anchor(path)
            seal_keyring = self.audit.verifier.seal_keyring
            with self.audit.engine.connect() as conn:
                records = load_seal_records(conn)
        except Exception as exc:
            self.audit.on_error(exc)
            return

        if records.capped:
            self.audit.on_error(
                RuntimeError("audit sealer refused anchor healing: seal-record scan hit its cap")
            )
            return

        for record in records:
            if not seal_is_intact(record, seal_keyring):
                continue
            if any(_record_matches_anchor(record, event) for event in anchor.events):
                continue
            assert record.to_id is not None
            assert record.seal_mac is not None
            assert record.sealed_at is not None
            self._emit_anchor(
                kind=record.kind,
                from_id=record.from_id,
                to_id=record.to_id,
                mac=record.seal_mac,
                at=record.sealed_at,
            )

    def _signer_matches_existing_records(self, key: Key) -> bool:
        """Refuse a mixed-signer Layer-2 history before writing anything."""
        try:
            with self.audit.engine.connect() as conn:
                mismatch = conn.execute(
                    select(_seals.c.key_id)
                    .where(or_(_seals.c.key_id != key.id, _seals.c.key_id.is_(None)))
                    .limit(1)
                ).first()
        except Exception as exc:
            self.audit.on_error(exc)
            return False
        if mismatch is None:
            return True
        self.audit.on_error(
            RuntimeError(
                "audit sealer REFUSED to write mixed-signer Layer-2 history: existing "
                f"key_id={mismatch.key_id!r}, configured signer key_id={key.id!r}"
            )
        )
        return False

    def _ensure_activation(self, key: Key) -> tuple[int, str, datetime] | None:
        """Insert activation and return its anchor payload, or ``None`` if already present."""
        engine = self.audit.engine
        with get_dialect(engine).begin_claim_tx(engine) as conn:
            exists = conn.execute(
                select(_seals.c.id).where(_seals.c.kind == "activation").limit(1)
            ).first()
            if exists is not None:
                return None
            at = now_utc()
            cutoff = at - timedelta(seconds=self.audit.grace)
            boundary = (
                conn.execute(
                    select(func.max(_audits.c.id)).where(_audits.c.created_at <= cutoff)
                ).scalar_one()
                or 0
            )
            mac = integrity.activation_mac(key, boundary_id=boundary, at=at, key_id=key.id)
            conn.execute(
                _seals.insert().values(
                    kind="activation",
                    from_id=_ACTIVATION_FROM_ID,
                    to_id=boundary,
                    row_count=None,
                    rows_mac=None,
                    seal_mac=mac,
                    sealed_at=at,
                    key_id=key.id,
                )
            )
        return boundary, mac, at

    def _seal_next_batch(self, key: Key) -> tuple[int, int, str, datetime, int] | None:
        """Insert one independent covering seal, or return ``None`` when no rows are eligible."""
        cutoff = now_utc() - timedelta(seconds=self.audit.grace)
        engine = self.audit.engine
        with get_dialect(engine).begin_claim_tx(engine) as conn:
            hwm = (
                conn.execute(
                    select(func.max(_seals.c.to_id)).where(
                        _seals.c.kind.in_(("activation", "floor", "seal"))
                    )
                ).scalar_one()
                or 0
            )
            rows = conn.execute(
                select(_audits.c.id, _audits.c.row_mac)
                .where(_audits.c.id > hwm, _audits.c.created_at <= cutoff)
                .order_by(_audits.c.id)
                .limit(self.audit.seal_batch_size)
            ).all()
            if not rows:
                return None
            if any(row.row_mac is None for row in rows):
                self.audit.on_error(
                    RuntimeError(
                        "audit sealer refused to seal an unsigned row above the activation "
                        "boundary; verify will report it as TAMPERED"
                    )
                )
                return None
            pairs = [(row.id, row.row_mac) for row in rows]
            return self._insert_seal(conn, key, from_id=hwm, to_id=rows[-1].id, rows=pairs)

    def _insert_seal(
        self,
        conn: Connection,
        key: Key,
        *,
        from_id: int,
        to_id: int,
        rows: list[tuple[int, str]],
    ) -> tuple[int, int, str, datetime, int]:
        """Compute and insert one range seal, returning its anchor payload and row count."""
        at = now_utc()
        aggregate = integrity.rows_mac(key, rows)
        row_count = len(rows)
        mac = integrity.seal_mac(
            key,
            from_id=from_id,
            to_id=to_id,
            row_count=row_count,
            rows_mac=aggregate,
            sealed_at=at,
            key_id=key.id,
        )
        conn.execute(
            _seals.insert().values(
                kind="seal",
                from_id=from_id,
                to_id=to_id,
                row_count=row_count,
                rows_mac=aggregate,
                seal_mac=mac,
                sealed_at=at,
                key_id=key.id,
            )
        )
        return from_id, to_id, mac, at, row_count

    def _emit_anchor(
        self,
        *,
        kind: str,
        from_id: int | None,
        to_id: int,
        mac: str,
        at: datetime,
    ) -> bool:
        """Append one new-format anchor event and call the optional sink.

        Returns whether every configured sink accepted the event. Sealing ignores the result
        (best-effort after commit); retention uses it as a hard gate before advancing a floor.
        """
        line = _format_anchor_event(kind=kind, from_id=from_id, to_id=to_id, mac=mac, at=at) + "\n"

        accepted = True
        path = self.audit._anchor_path
        if path is not None:
            try:
                encoded = line.encode("utf-8")
                with open(path, "ab+") as handle:
                    handle.seek(0, os.SEEK_END)
                    separator = b""
                    if handle.tell() > 0:
                        handle.seek(-1, os.SEEK_END)
                        if handle.read(1) not in {b"\n", b"\r"}:
                            separator = b"\n"
                    handle.write(separator + encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception as exc:
                accepted = False
                self.audit.on_error(exc)
        callback = self.audit._on_anchor
        if callback is not None:
            try:
                callback(kind, from_id, to_id, mac, at)
            except Exception as exc:
                accepted = False
                self.audit.on_error(exc)
        return accepted


class SealLoop(InterruptiblePoller):
    """Optional background loop that activates and seals on a timer."""

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
