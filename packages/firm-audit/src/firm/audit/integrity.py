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

from .._core.clock import now_utc

# --- canonicalization ------------------------------------------------------------------

#: Recipe version. Bumping it re-scopes every MAC, so old rows must be verified under the
#: version they were written with; ``"v1"`` is the only one that exists today.
CANON_VERSION = b"v1"

# Framing markers. Any present value is ``_PRESENT`` + length + bytes; ``_ABSENT`` frames a
# ``None`` (distinct from a zero-length present value); ``_NOMAC`` marks a row that carried no
# ``row_mac`` when it was sealed (design review 5A) so its deletion is still detectable. The
# three markers are single bytes chosen so no present field, whose second byte is a length,
# can be confused with a bare marker.
_ABSENT = b"\x00"
_PRESENT = b"\x01"
_NOMAC = b"\x02"

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


def _make_key(secret: bytes) -> Key:
    return Key(secret=secret, id=key_id(secret))


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


def parse_keyring(raw: str | None) -> dict[str, Key]:
    """Parse a verification keyring: ``"id1=secret,id2=secret"`` → ``{label: Key}``.

    Entries are comma-separated; each splits on its **first** ``=`` so a secret may itself
    contain ``=``. A secret must not contain a comma — the format is comma-delimited, so a
    comma inside a secret shows up as a fragment with no ``=`` and is rejected with a pointed
    error rather than silently splitting the key. Empty input yields an empty keyring; every
    label must be non-empty and unique, and every secret is length-validated exactly like
    :func:`load_key` so writer and verifier never disagree on what a valid key is.

    The label is the human mnemonic from the config (``id1``/``id2`` during a rotation); the
    authoritative match at verify time is by :attr:`Key.id`, not by this label.
    """
    keyring: dict[str, Key] = {}
    if not raw:
        return keyring
    for entry in raw.split(","):
        if "=" not in entry:
            raise ValueError(
                f"FIRM_AUDIT_KEYS entry {entry!r} has no '='; expected 'label=secret'. "
                "Secrets must not contain a comma (the keyring is comma-delimited)."
            )
        label, secret = entry.split("=", 1)
        if not label:
            raise ValueError(
                f"FIRM_AUDIT_KEYS entry {entry!r} has an empty label; expected 'label=secret'."
            )
        if label in keyring:
            raise ValueError(f"FIRM_AUDIT_KEYS has a duplicate label {label!r}.")
        _validate_key_length(secret, source=f"FIRM_AUDIT_KEYS[{label}]")
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
    return _hmac_hex(
        key,
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


def rows_mac(key: Key, rows: Iterable[tuple[int, str | None]]) -> str:
    """Hex ``HMAC-SHA256`` over the ``(id, row_mac)`` pairs a seal covers, in id order.

    Each pair frames the id and then the row's MAC, or the :data:`_NOMAC` marker when the row
    carried no ``row_mac`` — so deleting a NULL-MAC row still changes the seal (design review
    5A). The caller supplies the rows already ordered by id and present in ``(from_id, to_id]``.
    """
    parts = [CANON_VERSION]
    for row_id, mac in rows:
        parts.append(_field(str(row_id)))
        parts.append(_NOMAC if mac is None else _field(mac))
    return _hmac_hex(key, b"".join(parts))


def seal_mac(
    key: Key,
    *,
    seq: int,
    kind: str,
    from_id: int,
    to_id: int,
    row_count: int,
    rows_mac: str,
    prev_mac: str,
    sealed_at: datetime,
) -> str:
    """Hex ``HMAC-SHA256`` over a seal's fields — Layer 2's per-seal MAC.

    Integers are framed as their decimal strings; ``sealed_at`` follows the same round-trip
    normalization as row timestamps. ``prev_mac`` chains to seal ``seq-1`` (``"genesis"`` for
    the first), so editing, deleting, or reordering a seal breaks the chain.
    """
    message = CANON_VERSION + b"".join(
        _field(part)
        for part in (
            str(seq),
            kind,
            str(from_id),
            str(to_id),
            str(row_count),
            rows_mac,
            prev_mac,
            canonical_created_at(sealed_at),
        )
    )
    return _hmac_hex(key, message)


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
