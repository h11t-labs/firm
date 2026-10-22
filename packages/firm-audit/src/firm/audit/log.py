"""One append-only audit log, with interchangeable storage adapters."""

from __future__ import annotations

import hmac
import os
import warnings
from collections.abc import Callable
from datetime import datetime
from functools import lru_cache
from typing import Any

from sqlalchemy import Connection, Engine

from .._core.poller import default_on_error
from . import events
from .event_format import uuid_text
from .events import Reference
from .integrity import Key, load_key
from .object_store import Receipt
from .prepared import PreparedEvent, freeze_values
from .retention import Retention, RetentionLoop
from .sealing import Sealer, SealLoop
from .storage import SqlStorage, Storage
from .verify import IntegrityAlert, Verifier, VerifyLoop, VerifyReport, default_on_finding

#: Anchor callback: ``(kind, from_id, to_id, mac, at)`` for a seal/floor/activation event.
AnchorCallback = Callable[[str, int | None, int, str, datetime], None]

#: The integrity-alert callback's signature: one :class:`~firm.audit.verify.IntegrityAlert` per
#: verify run that detected tampering (critical) or a warning. Defaults to a one-line stderr sink.
FindingCallback = Callable[[IntegrityAlert], None]


@lru_cache(maxsize=8)
def _load_key_cached(raw: str | None) -> Key | None:
    # Parsing derives a SHA-256; cache it per distinct raw value so the module-level ``record``
    # path doesn't re-hash on every event. Keyed on the raw string, so a test (or a live config
    # reload) that changes ``FIRM_AUDIT_KEY`` still gets a fresh parse. A too-short key raises
    # (design review 4A) and is deliberately not cached.
    return load_key(raw)


def _env_key() -> Key | None:
    """The writer key from ``FIRM_AUDIT_KEY``, or ``None`` when unset/empty (feature off)."""
    return _load_key_cached(os.environ.get("FIRM_AUDIT_KEY"))


def record(
    conn: Connection,
    action: str,
    *,
    subject: Reference = None,
    actor: Reference = None,
    data: dict[str, Any] | None = None,
    changes: dict[str, Any] | None = None,
    correlation_id: str | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    """Record an event inside the caller's transaction (the shared-DB, atomic path).

    The row joins ``conn``'s transaction, so it commits or rolls back together with whatever else
    that transaction does — the same-transaction guarantee. This only holds when
    ``firm_audit_events`` lives in the database ``conn`` belongs to.

    This standalone path has no ``AuditLog`` to carry a key, so it picks the writer key up from
    ``FIRM_AUDIT_KEY`` directly; with no key set it behaves exactly as before.
    """
    events.append(
        conn,
        action=action,
        subject=subject,
        actor=actor,
        data=data,
        changes=changes,
        correlation_id=correlation_id,
        context=context,
        key=_env_key(),
    )


class AuditLog:
    """Audit policy shared by SQL and object storage.

    Pass storage= to select an adapter, or keep the existing database_url/engine shorthand.
    The log owns the adapter's lifetime; injected engines and SDK clients remain caller-owned.
    """

    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: Engine | None = None,
        storage: Storage | None = None,
        create_schema: bool = True,
        max_age: float | None = None,
        background_retention: bool = False,
        retention_interval: float = 3600.0,
        mac_key: str | None = None,
        seal_key: str | None = None,
        background_sealing: bool = False,
        seal_interval: float = 60.0,
        grace: float = 60.0,
        seal_batch_size: int = 10_000,
        anchor_path: str | None = None,
        on_anchor: AnchorCallback | None = None,
        verify_cycle: int = 7,
        background_verification: bool = False,
        verify_interval: float = 3600.0,
        verify_full_every: int = 24,
        anchor_max_age: float | None = None,
        unsealed_tail_max_age: float | None = None,
        on_error: Callable[[BaseException], None] | None = None,
        on_finding: FindingCallback | None = None,
        outbox_engine: Engine | None = None,
        outbox_interval: float = 5.0,
        create_outbox: bool = True,
    ) -> None:
        if storage is not None and (database_url is not None or engine is not None):
            raise ValueError("pass storage= or database_url/engine, not both")
        if storage is not None and not create_schema:
            raise ValueError("configure create_schema on SqlStorage when passing storage=")
        try:
            self._configure_integrity(
                max_age=max_age,
                mac_key=mac_key,
                seal_key=seal_key,
                seal_interval=seal_interval,
                grace=grace,
                seal_batch_size=seal_batch_size,
                anchor_path=anchor_path,
                on_anchor=on_anchor,
                verify_cycle=verify_cycle,
                anchor_max_age=anchor_max_age,
                unsealed_tail_max_age=unsealed_tail_max_age,
            )
        except BaseException:
            if storage is not None:
                storage.close()
            raise
        self.storage: Storage = (
            storage
            if storage is not None
            else SqlStorage(
                engine,
                database_url=database_url,
                create_schema=create_schema,
            )
        )
        self._closed = False
        self._outbox_loop = None
        self._loop = None
        self._seal_loop = None
        self._verify_loop = None
        try:
            if outbox_engine is not None and self.storage.stream is None:
                raise ValueError("this storage does not use a transactional outbox")
            self._start_maintenance(
                background_retention=background_retention,
                retention_interval=retention_interval,
                background_sealing=background_sealing,
                seal_interval=seal_interval,
                background_verification=background_verification,
                verify_interval=verify_interval,
                verify_full_every=verify_full_every,
                on_error=on_error,
                on_finding=on_finding,
            )

            if outbox_engine is not None:
                from .storage.s3.outbox import OutboxLoop
                from .storage.s3.outbox import create_outbox as create_outbox_table

                if create_outbox:
                    create_outbox_table(outbox_engine)
                self._outbox_loop = OutboxLoop(
                    self, outbox_engine, outbox_interval, on_error=self.on_error
                )
                self._outbox_loop.start()
        except BaseException:
            self.close()
            raise

    @property
    def engine(self) -> Engine:
        """The SQL engine, retained for existing SQL integrations."""
        if not isinstance(self.storage, SqlStorage):
            raise AttributeError("this audit log has no SQL engine")
        return self.storage.engine

    def record(
        self,
        action: str,
        *,
        subject: Reference = None,
        actor: Reference = None,
        data: dict[str, Any] | None = None,
        changes: dict[str, Any] | None = None,
        correlation_id: str | None = None,
        context: dict[str, Any] | None = None,
        conn: Connection | None = None,
    ) -> Receipt | None:
        """Sign an event and persist it through the configured storage.

        SQL joins a supplied connection's transaction. S3 stages in that transaction's outbox;
        without conn it publishes immediately and returns an object receipt.
        """
        values = events.build_values(
            action=action,
            subject=subject,
            actor=actor,
            data=data,
            changes=changes,
            correlation_id=correlation_id,
            context=context,
            key=self._key,
            identity=self.storage.requires_entry_id,
        )
        return self.storage.record(values, conn=conn, grace=self.grace)

    def prepare(
        self,
        action: str,
        *,
        subject: Reference = None,
        actor: Reference = None,
        data: dict[str, Any] | None = None,
        changes: dict[str, Any] | None = None,
        correlation_id: str | None = None,
        context: dict[str, Any] | None = None,
        created_at: datetime | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> PreparedEvent:
        """Freeze and sign once, before storage assigns a numeric id; persist body for retries."""
        values = events.build_values(
            action=action,
            subject=subject,
            actor=actor,
            data=data,
            changes=changes,
            correlation_id=correlation_id,
            context=context,
            key=self._key,
            created_at=created_at,
            identity=True,
        )
        if provenance is not None:
            if provenance.get("source") not in {"sql", "s3"} or not isinstance(
                provenance.get("identity"), str
            ):
                raise ValueError("invalid source provenance")
            uuid_text(provenance["stream"])
            values["provenance"] = provenance
        return freeze_values(values)

    def write(self, prepared: PreparedEvent) -> int | Receipt:
        """Persist frozen bytes idempotently; return the SQL id or S3 object receipt."""
        return self.storage.write(prepared, grace=self.grace)

    def get(self, locator: int | Receipt | str) -> dict[str, Any] | None:
        """Fetch an event by id, or an S3 receipt/key when using object storage."""
        return self.storage.get(locator)

    def import_provenance(self, event_id: int) -> dict[str, Any] | None:
        """The original identity of an imported event, when present."""
        return self.storage.import_provenance(event_id)

    def flush_outbox(self, engine: Engine, *, limit: int = 1000) -> int:
        """Publish committed events staged for this storage's transactional outbox."""
        from .storage.s3.outbox import flush

        return flush(engine, self, limit=limit)

    def history(
        self,
        *,
        subject: Reference = None,
        subject_type: str | None = None,
        subject_id: Any | None = None,
        actor: Reference = None,
        actor_type: str | None = None,
        actor_id: Any | None = None,
        action: str | None = None,
        correlation_id: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Matching events, newest id first, using the same filters for every storage."""
        if subject is not None and (subject_type is not None or subject_id is not None):
            raise ValueError("pass either subject= or subject_type=/subject_id=, not both")
        if actor is not None and (actor_type is not None or actor_id is not None):
            raise ValueError("pass either actor= or actor_type=/actor_id=, not both")
        if subject is not None:
            subject_type, subject_id, _ = events._ref(subject)
        if actor is not None:
            actor_type, actor_id, _ = events._ref(actor)
        # Match the stored text: reference parts are stored via str(), while action and
        # correlation_id are stored as given, so a (str, Enum) member is its value, not str().
        wanted = {
            name: str(value)
            for name, value in (
                ("subject_type", subject_type),
                ("subject_id", subject_id),
                ("actor_type", actor_type),
                ("actor_id", actor_id),
            )
            if value is not None
        }
        for name, value in (("action", action), ("correlation_id", correlation_id)):
            if value is not None:
                wanted[name] = str.__str__(value) if isinstance(value, str) else str(value)
        if limit <= 0:
            return []
        return self.storage.history(wanted, since, limit)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._outbox_loop is not None:
                self._outbox_loop.stop()
            self._stop_maintenance()
        finally:
            self.storage.close()

    def __enter__(self) -> AuditLog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _configure_integrity(
        self,
        *,
        max_age: float | None,
        mac_key: str | None,
        seal_key: str | None,
        seal_interval: float,
        grace: float,
        seal_batch_size: int,
        anchor_path: str | None,
        on_anchor: AnchorCallback | None,
        verify_cycle: int,
        anchor_max_age: float | None,
        unsealed_tail_max_age: float | None,
    ) -> None:
        self.max_age = max_age

        # Tamper-evidence key: explicit ``mac_key=`` wins (pass "" to force it off and ignore
        # the environment), otherwise ``FIRM_AUDIT_KEY``. ``load_key`` hard-fails a too-short
        # secret here at construction (design review 4A); ``None`` means the feature is off and
        # every write behaves exactly as it did before tamper-evidence existed.
        self._key = load_key(mac_key) if mac_key is not None else _env_key()

        # Seal key (Layer 2 signer — the two-key split). ``seal_key=`` wins, else
        # ``FIRM_AUDIT_SEAL_KEY``; when neither is set the seal key *defaults to the row key*, so a
        # deployment with no seal key behaves exactly as before (every instance may seal, one key
        # signs everything). Set it — on the designated sealer/verifier hosts only — to shrink the
        # blast radius: a compromised app instance then holds only the row key and can forge at most
        # individual unsealed rows; independent seals stay out of reach. It signs ``rows_mac`` and
        # ``seal_mac`` (everything in ``firm_audit_seals``); row MACs keep using the row key. If the
        # configured seal key equals the row key, that is simply single-key mode.
        # ``_seal_key_split`` records whether a *distinct* seal key is in force (drives verify's
        # seal-keyring narrowing and retention's signed-floor gate).
        _seal_raw = seal_key if seal_key is not None else os.environ.get("FIRM_AUDIT_SEAL_KEY")
        _loaded_seal = load_key(_seal_raw)  # None if unset/empty; hard-fails a too-short secret
        self._seal_key = _loaded_seal if _loaded_seal is not None else self._key
        # Whether a *distinct* seal key is in force is decided by the secret, not ``key_id``: two
        # different secrets that collide on the 8-hex key_id must still count as a split (comparing
        # ``.id`` would silently downgrade them to single-key and mis-scope the seal keyring). A
        # collision between the row and seal keys is a config error, surfaced loudly at startup —
        # left unchecked it would shadow one key by the other and flag its objects as TAMPERED.
        if (
            self._key is not None
            and self._seal_key is not None
            and self._key.id == self._seal_key.id
            and not hmac.compare_digest(self._key.secret, self._seal_key.secret)
        ):
            raise ValueError(
                f"the audit row key and seal key share key_id {self._key.id!r} but have different "
                "secrets; indexed by key_id they collide, one shadowing the other. Change one "
                "of FIRM_AUDIT_KEY / FIRM_AUDIT_SEAL_KEY so their key_ids differ."
            )
        self._seal_key_split = (
            self._key is not None
            and self._seal_key is not None
            and not hmac.compare_digest(self._seal_key.secret, self._key.secret)
        )

        # Sealing config (Layer 2). ``grace`` must exceed the longest audit-recording transaction
        # plus inter-instance clock skew; the anchor path falls back to ``FIRM_AUDIT_ANCHOR_PATH``.
        self.grace = grace
        self.seal_interval = seal_interval
        self.seal_batch_size = seal_batch_size
        self._anchor_path = (
            anchor_path if anchor_path is not None else os.environ.get("FIRM_AUDIT_ANCHOR_PATH")
        )
        self._on_anchor = on_anchor

        # Verification config. ``anchor_max_age`` defaults to 3x the
        # seal interval (design D16); the unsealed-tail liveness threshold to a generous multiple
        # of the seal cadence (grace + interval is the normal ceiling, so well past means stalled).
        self.verify_cycle = verify_cycle
        self._anchor_max_age = anchor_max_age if anchor_max_age is not None else 3.0 * seal_interval
        self._unsealed_tail_max_age = (
            unsealed_tail_max_age
            if unsealed_tail_max_age is not None
            else max(10.0 * seal_interval, grace + seal_interval)
        )

    def _start_maintenance(
        self,
        *,
        background_retention: bool,
        retention_interval: float,
        background_sealing: bool,
        seal_interval: float,
        background_verification: bool,
        verify_interval: float,
        verify_full_every: int,
        on_error: Callable[[BaseException], None] | None,
        on_finding: FindingCallback | None,
    ) -> None:
        # Background pruning / sealing failures are routed here (default: traceback to stderr).
        self.on_error = on_error if on_error is not None else default_on_error
        # Integrity alerts (a verify run that detected tampering/warning) are routed here — a
        # dedicated hook, because a TAMPERED verdict is a signal, not an exception. Default: one
        # high-severity stderr line per detection, so a stock deployment's logstream shows it. Pass
        # a no-op to mute it, or a forwarder to ship it to Datadog/Loki/JSON logs.
        self.on_finding = on_finding if on_finding is not None else default_on_finding
        self.retention = Retention(self)
        self._loop = (
            RetentionLoop(self.retention, retention_interval, on_error=self.on_error)
            if background_retention
            else None
        )
        if self._loop is not None:
            self._loop.start()

        self.sealer = Sealer(self)
        self.verifier = Verifier(self)
        self._seal_loop: SealLoop | None = None
        if background_sealing:
            self._warn_sealing_enabled(seal_interval)
            self._seal_loop = SealLoop(self.sealer, seal_interval, on_error=self.on_error)
            self._seal_loop.start()

        # The in-process continuous watch (opt-in), mirroring the seal/retention loops: a tail
        # verify every ``verify_interval`` and a ``--full`` recompute every ``verify_full_every``
        # ticks, each persisting the status row and firing ``on_finding`` on a tampered/warning
        # outcome. A separate verifier host is stronger, but this makes "keep checking" one flag.
        self._verify_loop: VerifyLoop | None = None
        if background_verification:
            self._verify_loop = VerifyLoop(
                self.verifier,
                verify_interval,
                anchor_path=self._anchor_path,
                full_every=verify_full_every,
                on_error=self.on_error,
            )
            self._verify_loop.start()

    def _warn_sealing_enabled(self, seal_interval: float) -> None:
        """Restate the two-phase rollout and grace-sizing rules when sealing is switched on
        (design review 1A/D13). A stderr-only startup line would vanish; a warning is visible and
        testable, and the no-key case is loud because it silently produces no seals at all."""
        if self._seal_key is None:
            warnings.warn(
                "audit sealing is enabled but no seal key is configured — no seals will be "
                "written. Sealing signs with the seal key (FIRM_AUDIT_SEAL_KEY / seal_key=), which "
                "defaults to the row key (FIRM_AUDIT_KEY / mac_key=) when unset — so this means no "
                "key at all. Two-phase rollout: deploy the row key to every instance first "
                "(phase 1), then enable sealing on a host that has a seal key (phase 2).",
                stacklevel=4,
            )
            return
        warnings.warn(
            f"audit sealing is enabled (grace={self.grace}s, interval={seal_interval}s). Grace "
            "must exceed your longest audit-recording transaction plus inter-instance clock "
            "skew; a row that appears inside an already sealed range is TAMPERED. Record "
            "long-running jobs via the own-transaction path (omit conn=).",
            stacklevel=4,
        )

    def verify(self, *, anchor_path: str | None = None, full: bool = False) -> VerifyReport:
        """Verify rows, independent seals, markers, and anchor; persist the outcome.

        ``anchor_path`` defaults to the configured anchor (:data:`FIRM_AUDIT_ANCHOR_PATH`) so a
        deployment that writes an anchor also checks it; pass a path to override, or ``full=True``
        to recompute every sealed range. Only a full run guarantees complete range coverage.
        The evidence scan is read-only; status persistence needs a write. Raises
        :class:`~firm.audit.verify.VerifyError` for an unknown row ``key_id`` only when no tampered
        finding exists, after writing the ``error`` outcome and routing through ``on_error``.
        """
        return self.verifier.run(
            anchor_path=anchor_path if anchor_path is not None else self._anchor_path,
            full=full,
        )

    def _stop_maintenance(self) -> None:
        if self._loop is not None:
            self._loop.stop()
        if self._seal_loop is not None:
            self._seal_loop.stop()
        if self._verify_loop is not None:
            self._verify_loop.stop()
