"""Tamper-evidence primitives: canonicalization, HMAC recipes, keys, and ULIDs.

This module is the single home for every byte that goes *into* a MAC. It is deliberately
pure — no database, no I/O, no globals — so the write path (:mod:`.events`), the sealer
(:mod:`.sealing`), and the verifier all compute identical MACs from identical inputs. A
divergence here would masquerade as tampering, so the recipes live in exactly one place and
are exercised by heavy unit tests rather than reimplemented per caller.

Three ideas carry the whole design:

* **Length-prefixed canonicalization.** Every field is framed as ``present-marker ‖ 8-byte
  big-endian length ‖ utf-8 bytes`` (or a one-byte *absent* marker). There is no delimiter,
  so no value — embedded separators, NUL bytes, 4-byte emoji, a megabyte of text — can be
  arranged to collide with a different field layout. ``None`` and ``""`` are distinct: the
  first is the absent marker, the second is a present field of length zero.

* **Round-trip rule (design review 2A).** A MAC binds values *as the database returns them*,
  not as they sit in memory. ``created_at`` is normalized to naive-UTC ISO-8601 with forced
  microseconds (matching ``dt_type()`` — timezone-naive, ``DATETIME(6)`` on MySQL); scalar
  refs are the ``str``/``None`` that :func:`firm.audit.events._norm` produced; JSON payloads
  are the stored ``Text`` strings verbatim. Anything the DB round-trips lossily would else
  verify as TAMPERED on one dialect and OK on another.

* **HMAC-SHA256 with an external key.** ``"v1"`` prefixes every recipe so it can evolve; the
  key never touches the database, only its ``key_id`` (first 8 hex of ``SHA-256(key)``) does.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from .._core.clock import now_utc

# --- canonicalization ------------------------------------------------------------------

#: Recipe version. Bumping it re-scopes every MAC, so old rows must be verified under the
#: version they were written with; ``"v1"`` is the only one that exists today.
CANON_VERSION = b"v1"

# Framing markers. Any present value is ``_PRESENT`` + length + bytes; ``_ABSENT`` frames a
# ``None`` (distinct from a zero-length present value). The markers are single bytes chosen so no
# present field, whose second byte is a length, can be confused with a bare marker.
_ABSENT = b"\x00"
_PRESENT = b"\x01"

_LENGTH_BYTES = 8


def _field(value: str | None) -> bytes:
    """Frame one optional string as ``present ‖ len ‖ utf-8`` or the absent marker.

    The 8-byte length prefix means the value's own bytes are never scanned for a delimiter,
    so embedded separators, NUL bytes, and 4-byte characters are all carried verbatim.
    """
    if value is None:
        return _ABSENT
    raw = value.encode("utf-8")
    return _PRESENT + len(raw).to_bytes(_LENGTH_BYTES, "big") + raw


def canonical_created_at(value: datetime) -> str:
    """Normalize a ``created_at`` to the naive-UTC ISO-8601 string the MAC binds.

    Aware datetimes are converted to UTC and stripped of tzinfo; microseconds are always
    rendered (``…T12:34:56.000000``) so a whole-second timestamp cannot canonicalize two ways.
    This mirrors what ``dt_type()`` stores and returns on every dialect, so recomputing the
    MAC from a read-back row yields the same string that was signed at insert time.
    """
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.isoformat(timespec="microseconds")


def row_mac_input(
    *,
    entry_id: str,
    action: str,
    subject_type: str | None,
    subject_id: str | None,
    subject_label: str | None,
    actor_type: str | None,
    actor_id: str | None,
    actor_label: str | None,
    correlation_id: str | None,
    data: str | None,
    changes: str | None,
    context: str | None,
    created_at: datetime,
) -> bytes:
    """Assemble the canonical byte string a row's MAC is taken over.

    Field order is fixed and load-bearing; ``data``/``changes``/``context`` are the stored
    JSON strings (not re-serialized dicts), and ``created_at`` is normalized per the
    round-trip rule. The result is ``CANON_VERSION`` followed by the framed fields.
    """
    return CANON_VERSION + b"".join(
        _field(part)
        for part in (
            entry_id,
            action,
            subject_type,
            subject_id,
            subject_label,
            actor_type,
            actor_id,
            actor_label,
            correlation_id,
            data,
            changes,
            context,
            canonical_created_at(created_at),
        )
    )


# --- keys ------------------------------------------------------------------------------

#: A configured key must be at least this many characters. A shorter key silently voids all
#: three layers, so a shorter value is a hard error, never a warning (design review 4A).
KEY_MIN_LENGTH = 32

#: ``key_id`` width in hex characters (``String(16)`` on the row/seal tables leaves room).
KEY_ID_LENGTH = 8


def key_id(secret: bytes) -> str:
    """Public identifier for a key: the first :data:`KEY_ID_LENGTH` hex chars of its SHA-256.

    Stored on rows and seals so verification knows which key signed each without ever putting
    the key itself in the database. A forged row cannot invent a ``key_id`` and still produce
    a MAC that validates under the corresponding secret.
    """
    return hashlib.sha256(secret).hexdigest()[:KEY_ID_LENGTH]


@dataclass(frozen=True)
class Key:
    """A MAC key: its UTF-8 ``secret`` bytes and the derived public :attr:`id` (``key_id``)."""

    secret: bytes
    id: str


class Signer(Protocol):
    """Minimal signing seam; add a new implementation here for a future asymmetric primitive."""

    key_id: str

    def sign(self, message: bytes) -> str:
        """Return the stable textual signature for ``message``."""

    def verify(self, message: bytes, tag: str) -> bool:
        """Return whether ``tag`` authenticates ``message``."""


@dataclass(frozen=True)
class HmacSigner:
    """The current HMAC-SHA256 :class:`Signer`, backed by one configured :class:`Key`."""

    key: Key

    @property
    def key_id(self) -> str:
        return self.key.id

    def sign(self, message: bytes) -> str:
        return _hmac_hex(self.key, message)

    def verify(self, message: bytes, tag: str) -> bool:
        return self.tags_match(self.sign(message), tag)

    @staticmethod
    def tags_match(expected: str, actual: str) -> bool:
        """Constant-time comparison, robust to an attacker-written non-ASCII tag.

        ``hmac.compare_digest`` raises ``TypeError`` when a ``str`` argument holds a non-ASCII
        codepoint. A stored MAC is attacker-controlled (a plain DB/anchor write), so treat that as a
        non-match rather than let the exception escape ``seal_is_intact`` / ``Retention.run_once``
        and break the never-raise-on-tampered-storage contract — a non-ASCII value can never equal a
        hex digest anyway."""
        try:
            return hmac.compare_digest(expected, actual)
        except TypeError:
            return False


def _make_key(secret: bytes) -> Key:
    return Key(secret=secret, id=key_id(secret))


def add_key(ring: dict[str, Key], key: Key, *, source: str) -> None:
    """Insert ``key`` into a ``key_id``-keyed ``ring``, raising on a genuine collision.

    A keyring is indexed by :attr:`Key.id` (the 8-hex ``key_id``), so two **distinct** secrets that
    hash to the same ``key_id`` would silently overwrite one another — collapsing two identities
    into one and making every row/seal signed by the shadowed key verify as a false TAMPERED (for a
    row-vs-seal collision, downgrading the two-key split). A collision is astronomically unlikely,
    but it is a *configuration* error, not tampering, so it is surfaced loudly rather than left to
    masquerade. Re-adding the *same* secret under its id is idempotent (the current seal key is also
    row-eligible in single-key mode, so it is legitimately added to the row ring twice)."""
    existing = ring.get(key.id)
    if existing is not None and not hmac.compare_digest(existing.secret, key.secret):
        raise ValueError(
            f"two configured audit keys share key_id {key.id!r} but have different secrets "
            f"(seen via {source}); indexed by key_id they collide and one would shadow the other, "
            "making objects signed by the shadowed key verify as TAMPERED. Change one of the keys "
            "so their key_ids differ."
        )
    ring[key.id] = key


def _validate_key_length(value: str, *, source: str) -> None:
    if len(value) < KEY_MIN_LENGTH:
        raise ValueError(
            f"{source} must be at least {KEY_MIN_LENGTH} characters (got {len(value)}); "
            "a shorter key silently voids audit tamper-evidence. Use a long random secret."
        )


def load_key(raw: str | None) -> Key | None:
    """Load the writer's key from ``FIRM_AUDIT_KEY`` (or ``mac_key=``).

    ``None`` or empty means the feature is off (columns stay NULL, everything behaves as
    today); any non-empty value shorter than :data:`KEY_MIN_LENGTH` characters is a hard
    :class:`ValueError` at startup.
    """
    if not raw:
        return None
    _validate_key_length(raw, source="FIRM_AUDIT_KEY")
    return _make_key(raw.encode("utf-8"))


def parse_keyring(raw: str | None, *, source: str = "FIRM_AUDIT_RETIRED_KEYS") -> dict[str, Key]:
    """Parse a retired-key archive: ``"id1=secret,id2=secret"`` → ``{label: Key}``.

    Used for both retired-key env vars (:data:`FIRM_AUDIT_RETIRED_KEYS`,
    :data:`FIRM_AUDIT_RETIRED_SEAL_KEYS`); ``source`` names the one being parsed so every error
    points at the right variable. Entries are comma-separated and each splits on its **first** ``=``
    (so a secret may itself contain ``=``). Empty input yields an empty keyring; every label must be
    non-empty and unique, and every secret is length-validated exactly like :func:`load_key` so
    writer and verifier never disagree on what a valid key is.

    **A comma is *always* an entry delimiter — a secret cannot contain one.** The comma-delimited
    format is inherently ambiguous about a comma inside a secret: ``id1=A,id2=B`` is two keys, and a
    single key whose secret were literally ``A,id2=B`` would be byte-identical input, so the two
    cannot be told apart. Rather than pretend otherwise, the rule is simple and enforced as far
    can be: a comma that yields a **malformed** fragment — no ``=``, an empty label, or a too-short
    secret — is a pointed :class:`ValueError` (this is the common accidental case, e.g. a raw comma
    in a secret whose tail is not itself a ``label=secret``); a comma followed by a **well-formed**
    ``label=secret`` is taken as a separate key, exactly as the multi-key form intends. The
    invariant callers can rely on either way is *fail-closed*: parsing never silently merges two
    distinct secrets into one identity, and a genuine ``key_id`` collision between the results
    is caught downstream by :func:`add_key`. **Do not put a comma in a secret** — use a longer
    comma-free random secret.

    The label is the human mnemonic from the config (``id1``/``id2`` during a rotation); the
    authoritative match at verify time is by :attr:`Key.id`, not by this label.
    """
    keyring: dict[str, Key] = {}
    if not raw:
        return keyring
    for entry in raw.split(","):
        if "=" not in entry:
            raise ValueError(
                f"{source} entry {entry!r} has no '='; expected 'label=secret'. "
                "Secrets must not contain a comma (the keyring is comma-delimited)."
            )
        label, secret = entry.split("=", 1)
        if not label:
            raise ValueError(
                f"{source} entry {entry!r} has an empty label; expected 'label=secret'."
            )
        if label in keyring:
            raise ValueError(f"{source} has a duplicate label {label!r}.")
        _validate_key_length(secret, source=f"{source}[{label}]")
        keyring[label] = _make_key(secret.encode("utf-8"))
    return keyring


# --- MAC recipes -----------------------------------------------------------------------


def _hmac_hex(key: Key, message: bytes) -> str:
    return hmac.new(key.secret, message, hashlib.sha256).hexdigest()


def row_mac(
    key: Key,
    *,
    entry_id: str,
    action: str,
    subject_type: str | None,
    subject_id: str | None,
    subject_label: str | None,
    actor_type: str | None,
    actor_id: str | None,
    actor_label: str | None,
    correlation_id: str | None,
    data: str | None,
    changes: str | None,
    context: str | None,
    created_at: datetime,
) -> str:
    """Hex ``HMAC-SHA256`` over :func:`row_mac_input` — Layer 1's per-row MAC."""
    return HmacSigner(key).sign(
        row_mac_input(
            entry_id=entry_id,
            action=action,
            subject_type=subject_type,
            subject_id=subject_id,
            subject_label=subject_label,
            actor_type=actor_type,
            actor_id=actor_id,
            actor_label=actor_label,
            correlation_id=correlation_id,
            data=data,
            changes=changes,
            context=context,
            created_at=created_at,
        ),
    )


def rows_mac(key: Key, rows: Iterable[tuple[int, str]]) -> str:
    """Hex ``HMAC-SHA256`` over the ``(id, row_mac)`` pairs a seal covers, in id order.

    The caller supplies rows ordered by id and present in ``(from_id, to_id]``. Legacy NULL-MAC
    rows are below the signed activation boundary and are never sealed.
    """
    digest = hmac.new(key.secret, CANON_VERSION, hashlib.sha256)
    for row_id, mac in rows:
        if mac is None:
            raise ValueError("legacy NULL-MAC rows are outside seal coverage")
        digest.update(_field(str(row_id)))
        digest.update(_field(mac))
    return digest.hexdigest()


def seal_mac(
    key: Key,
    *,
    from_id: int,
    to_id: int,
    row_count: int,
    rows_mac: str,
    sealed_at: datetime,
    key_id: str,
) -> str:
    """Hex ``HMAC-SHA256`` over one independent covering seal, including its signer id."""
    return HmacSigner(key).sign(
        seal_mac_input(
            from_id=from_id,
            to_id=to_id,
            row_count=row_count,
            rows_mac=rows_mac,
            sealed_at=sealed_at,
            key_id=key_id,
        )
    )


def seal_mac_input(
    *,
    from_id: int,
    to_id: int,
    row_count: int,
    rows_mac: str,
    sealed_at: datetime,
    key_id: str,
) -> bytes:
    """Canonical message for one independent covering seal."""
    return CANON_VERSION + b"".join(
        _field(part)
        for part in (
            "seal",
            key_id,
            str(from_id),
            str(to_id),
            str(row_count),
            rows_mac,
            canonical_created_at(sealed_at),
        )
    )


def floor_mac(key: Key, *, through_id: int, retired_at: datetime, key_id: str) -> str:
    """Sign one append-only retirement-floor advance, including its signer id."""
    return HmacSigner(key).sign(
        floor_mac_input(through_id=through_id, retired_at=retired_at, key_id=key_id)
    )


def floor_mac_input(*, through_id: int, retired_at: datetime, key_id: str) -> bytes:
    """Canonical message for one append-only retirement-floor advance."""
    return CANON_VERSION + b"".join(
        _field(part)
        for part in ("floor", key_id, str(through_id), canonical_created_at(retired_at))
    )


def activation_mac(key: Key, *, boundary_id: int, at: datetime, key_id: str) -> str:
    """Sign the one explicit sealing-activation boundary, including its signer id."""
    return HmacSigner(key).sign(activation_mac_input(boundary_id=boundary_id, at=at, key_id=key_id))


def activation_mac_input(*, boundary_id: int, at: datetime, key_id: str) -> bytes:
    """Canonical message for the one explicit sealing-activation boundary."""
    return CANON_VERSION + b"".join(
        _field(part) for part in ("activation", key_id, str(boundary_id), canonical_created_at(at))
    )


def checkpoint_mac(
    key: Key,
    *,
    coverage_id: int,
    floor_id: int,
    at: datetime,
    key_id: str,
) -> str:
    """Sign one compacted anchor checkpoint containing both monotonic watermarks."""
    return HmacSigner(key).sign(
        checkpoint_mac_input(
            coverage_id=coverage_id,
            floor_id=floor_id,
            at=at,
            key_id=key_id,
        )
    )


def checkpoint_mac_input(*, coverage_id: int, floor_id: int, at: datetime, key_id: str) -> bytes:
    """Canonical message for a compacted anchor coverage/floor checkpoint."""
    return CANON_VERSION + b"".join(
        _field(part)
        for part in (
            "checkpoint",
            key_id,
            str(coverage_id),
            str(floor_id),
            canonical_created_at(at),
        )
    )


# --- ULIDs -----------------------------------------------------------------------------

# Crockford Base32 (no I, L, O, U). The alphabet is in ascending value order, so fixed-width
# encodings sort lexicographically the same way the underlying 128-bit integers do — which is
# what makes a ULID time-ordered as text.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_LENGTH = 26
_TIMESTAMP_BITS = 48
_RANDOM_BITS = 80


def _encode_crockford(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def new_ulid(now: datetime | None = None) -> str:
    """Generate a 26-char Crockford Base32 ULID (48-bit ms timestamp + 80 random bits).

    Time-ordered: two ULIDs from different milliseconds sort in timestamp order as plain
    strings. Collision-resistant: the low 80 bits come from :mod:`secrets`, and the audit
    schema's unique index on ``entry_id`` is the hard backstop against any duplicate (which is
    also how a replayed row is rejected). ``now`` is naive UTC by default (:func:`now_utc`);
    a naive value is read as UTC, an aware one is converted, so the epoch is never mistaken
    for local time.
    """
    moment = now if now is not None else now_utc()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    epoch_ms = int(moment.timestamp() * 1000) & ((1 << _TIMESTAMP_BITS) - 1)
    value = (epoch_ms << _RANDOM_BITS) | secrets.randbits(_RANDOM_BITS)
    return _encode_crockford(value, _ULID_LENGTH)
