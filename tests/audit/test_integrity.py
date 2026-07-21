"""Unit tests for :mod:`firm.audit.integrity` — the pure MAC/key/ULID primitives.

No database here (the per-dialect round-trip property test lives with the schema work); this
file pins the canonicalization edge cases, datetime normalization, MAC stability, keyring
parsing, ``key_id`` derivation, and ULID ordering/uniqueness that everything else builds on.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta, timezone

import pytest

from firm.audit import integrity
from firm.audit.integrity import (
    KEY_MIN_LENGTH,
    Key,
    canonical_created_at,
    key_id,
    load_key,
    new_ulid,
    parse_keyring,
    row_mac,
    row_mac_input,
    rows_mac,
    seal_mac,
)

# A valid throwaway key (>= 32 chars) for MAC stability tests.
_SECRET = "x" * KEY_MIN_LENGTH
_KEY = load_key(_SECRET)
assert _KEY is not None
KEY: Key = _KEY

# A canonical set of row fields; individual tests override one field at a time.
_ROW: dict = {
    "entry_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
    "action": "invoice.paid",
    "subject_type": "Invoice",
    "subject_id": "42",
    "subject_label": "ACME #42",
    "actor_type": "User",
    "actor_id": "7",
    "actor_label": "root@example.com",
    "correlation_id": "req-abc",
    "data": '{"amount": 100}',
    "changes": None,
    "context": None,
    "created_at": datetime(2026, 7, 20, 12, 34, 56, 123456),
}


# --- canonicalization edge cases -------------------------------------------------------


def test_none_and_empty_string_are_distinct() -> None:
    """``None`` (absent) and ``""`` (present, zero length) must not collide."""
    with_none = row_mac_input(**{**_ROW, "subject_label": None})
    with_empty = row_mac_input(**{**_ROW, "subject_label": ""})
    assert with_none != with_empty


def test_embedded_separators_do_not_shift_fields() -> None:
    """A value containing what looks like a delimiter cannot impersonate a field boundary:
    moving text across a field boundary changes the length prefixes, so the MAC differs."""
    a = row_mac_input(**{**_ROW, "subject_id": "42", "subject_label": "x=1,y=2"})
    b = row_mac_input(**{**_ROW, "subject_id": "42x=1,y=2", "subject_label": ""})
    assert a != b


def test_field_shift_is_detected() -> None:
    """The classic canonicalization bug: concatenating adjacent fields differently must not
    produce the same bytes. Length-prefixing prevents ``ab`` + ``c`` == ``a`` + ``bc``."""
    a = row_mac_input(**{**_ROW, "subject_type": "ab", "subject_id": "c"})
    b = row_mac_input(**{**_ROW, "subject_type": "a", "subject_id": "bc"})
    assert a != b


def test_unicode_and_four_byte_emoji_round_trip() -> None:
    value = "héllo \U0001f680 世界"  # combining-ish, 4-byte emoji, CJK
    m1 = row_mac(KEY, **{**_ROW, "actor_label": value})
    m2 = row_mac(KEY, **{**_ROW, "actor_label": value})
    assert m1 == m2
    assert m1 != row_mac(KEY, **_ROW)


def test_nul_bytes_are_carried_verbatim() -> None:
    """Embedded NUL must not truncate the field (a C-string bug); it changes the MAC."""
    a = row_mac_input(**{**_ROW, "data": "a\x00b"})
    b = row_mac_input(**{**_ROW, "data": "a"})
    c = row_mac_input(**{**_ROW, "data": "ab"})
    assert a != b
    assert a != c


def test_very_long_string_is_handled() -> None:
    big = "z" * 2_000_000
    a = row_mac(KEY, **{**_ROW, "context": big})
    b = row_mac(KEY, **{**_ROW, "context": big})
    assert a == b
    assert a != row_mac(KEY, **{**_ROW, "context": big + "z"})


def test_version_prefix_present() -> None:
    assert row_mac_input(**_ROW).startswith(integrity.CANON_VERSION)


# --- datetime normalization ------------------------------------------------------------


def test_aware_utc_normalizes_to_naive_micros() -> None:
    aware = datetime(2026, 7, 20, 12, 34, 56, 123456, tzinfo=UTC)
    assert canonical_created_at(aware) == "2026-07-20T12:34:56.123456"


def test_aware_offset_is_converted_to_utc() -> None:
    plus_two = timezone(timedelta(hours=2))
    aware = datetime(2026, 7, 20, 14, 34, 56, 123456, tzinfo=plus_two)
    naive = datetime(2026, 7, 20, 12, 34, 56, 123456)
    assert canonical_created_at(aware) == canonical_created_at(naive)


def test_naive_equals_aware_utc_same_instant() -> None:
    """The round-trip rule: an aware-UTC value signed at write time and the naive value the
    DB returns at verify time must canonicalize identically."""
    aware = datetime(2026, 7, 20, 12, 34, 56, 123456, tzinfo=UTC)
    naive = datetime(2026, 7, 20, 12, 34, 56, 123456)
    assert canonical_created_at(aware) == canonical_created_at(naive)


def test_microseconds_are_forced_when_zero() -> None:
    """A whole-second timestamp must still render six microsecond digits, or a row written at
    an exact second would canonicalize two ways."""
    assert canonical_created_at(datetime(2026, 7, 20, 12, 0, 0)) == "2026-07-20T12:00:00.000000"


def test_created_at_difference_changes_mac() -> None:
    a = row_mac(KEY, **_ROW)
    b = row_mac(KEY, **{**_ROW, "created_at": datetime(2026, 7, 20, 12, 34, 56, 123457)})
    assert a != b


# --- MAC stability ---------------------------------------------------------------------


def test_row_mac_is_deterministic_and_hex64() -> None:
    m = row_mac(KEY, **_ROW)
    assert m == row_mac(KEY, **_ROW)
    assert len(m) == 64
    assert all(c in "0123456789abcdef" for c in m)


def test_row_mac_depends_on_key() -> None:
    other = load_key("y" * KEY_MIN_LENGTH)
    assert other is not None
    assert row_mac(KEY, **_ROW) != row_mac(other, **_ROW)


@pytest.mark.parametrize("field", list(_ROW))
def test_every_field_is_bound(field: str) -> None:
    """Changing any single field changes the MAC — no field is silently dropped."""
    if field == "created_at":
        mutated = {field: datetime(2000, 1, 1, 0, 0, 0)}
    elif _ROW[field] is None:
        mutated = {field: "now-present"}
    else:
        mutated = {field: str(_ROW[field]) + "!"}
    assert row_mac(KEY, **{**_ROW, **mutated}) != row_mac(KEY, **_ROW)


def test_seal_mac_stable_and_field_sensitive() -> None:
    base: dict = {
        "seq": 1,
        "kind": "seal",
        "from_id": 0,
        "to_id": 10,
        "row_count": 10,
        "rows_mac": "a" * 64,
        "prev_mac": "genesis",
        "sealed_at": datetime(2026, 7, 20, 12, 0, 0),
    }
    m = seal_mac(KEY, **base)
    assert m == seal_mac(KEY, **base)
    assert len(m) == 64
    assert seal_mac(KEY, **{**base, "seq": 2}) != m
    assert seal_mac(KEY, **{**base, "kind": "checkpoint"}) != m
    assert seal_mac(KEY, **{**base, "prev_mac": "b" * 64}) != m


def test_rows_mac_order_sensitive_and_nomac_distinct() -> None:
    a = rows_mac(KEY, [(1, "a" * 64), (2, "b" * 64)])
    assert a == rows_mac(KEY, [(1, "a" * 64), (2, "b" * 64)])
    # Reordering the rows changes the MAC.
    assert rows_mac(KEY, [(2, "b" * 64), (1, "a" * 64)]) != a
    # A NULL-MAC row is distinct from both a present MAC and an absent field, and a
    # NULL-MAC row still contributes (so its later deletion is detectable).
    with_nomac = rows_mac(KEY, [(1, None)])
    assert with_nomac != rows_mac(KEY, [(1, "")])
    assert rows_mac(KEY, [(1, None), (2, None)]) != rows_mac(KEY, [(1, None)])


# --- keyring parsing -------------------------------------------------------------------


def test_parse_keyring_basic() -> None:
    ring = parse_keyring(f"id1={'a' * 32},id2={'b' * 40}")
    assert set(ring) == {"id1", "id2"}
    assert ring["id1"].secret == b"a" * 32
    assert ring["id2"].secret == b"b" * 40


def test_parse_keyring_splits_on_first_equals_only() -> None:
    """A secret may contain ``=`` — only the first one separates label from secret."""
    secret = "abc==def===" + "z" * 30
    ring = parse_keyring(f"id1={secret}")
    assert ring["id1"].secret == secret.encode("utf-8")


def test_parse_keyring_comma_in_secret_is_rejected() -> None:
    """A comma inside a secret surfaces as a fragment with no '=' and is rejected pointedly."""
    # As if the intended secret were "aaaa…,bbbb…": the tail after the comma has no '='.
    with pytest.raises(ValueError, match="comma"):
        parse_keyring(f"id1={'a' * 40},{'b' * 40}")


def test_parse_keyring_short_secret_hard_errors() -> None:
    with pytest.raises(ValueError, match="at least 32 characters"):
        parse_keyring("id1=tooshort")


def test_parse_keyring_empty_is_off() -> None:
    assert parse_keyring(None) == {}
    assert parse_keyring("") == {}


def test_parse_keyring_rejects_empty_label_and_duplicates() -> None:
    with pytest.raises(ValueError, match="empty label"):
        parse_keyring(f"={'a' * 32}")
    with pytest.raises(ValueError, match="duplicate label"):
        parse_keyring(f"id1={'a' * 32},id1={'b' * 32}")


# --- key loading + validation ----------------------------------------------------------


def test_load_key_off_when_absent_or_empty() -> None:
    assert load_key(None) is None
    assert load_key("") is None


def test_load_key_short_is_hard_error() -> None:
    with pytest.raises(ValueError, match="at least 32 characters"):
        load_key("x" * (KEY_MIN_LENGTH - 1))


def test_load_key_boundary_length_accepted() -> None:
    k = load_key("x" * KEY_MIN_LENGTH)
    assert k is not None
    assert k.secret == b"x" * KEY_MIN_LENGTH


def test_key_min_length_counts_characters_not_bytes() -> None:
    """32 emoji are 32 characters but 128 bytes; the char count is what the rule checks."""
    k = load_key("\U0001f680" * KEY_MIN_LENGTH)
    assert k is not None


# --- key_id derivation -----------------------------------------------------------------


def test_key_id_matches_sha256_prefix() -> None:
    secret = b"some-secret-key-material-of-length"
    assert key_id(secret) == hashlib.sha256(secret).hexdigest()[:8]
    assert len(key_id(secret)) == 8


def test_key_id_stored_on_key_matches_helper() -> None:
    k = load_key("q" * 40)
    assert k is not None
    assert k.id == key_id(b"q" * 40)


def test_distinct_keys_have_distinct_key_ids() -> None:
    a = load_key("a" * 40)
    b = load_key("b" * 40)
    assert a is not None and b is not None
    assert a.id != b.id


# --- ULID ordering + uniqueness --------------------------------------------------------


def test_ulid_shape() -> None:
    u = new_ulid()
    assert len(u) == 26
    assert all(c in "0123456789ABCDEFGHJKMNPQRSTVWXYZ" for c in u)


def test_ulid_time_ordered() -> None:
    """Later timestamp → lexicographically greater string, regardless of the random tail."""
    earlier = new_ulid(datetime(2026, 1, 1, tzinfo=UTC))
    later = new_ulid(datetime(2026, 1, 1, 0, 0, 0, 1000, tzinfo=UTC))  # +1 ms
    assert earlier < later


def test_ulid_monotonic_across_timestamps() -> None:
    base = datetime(2026, 7, 20, tzinfo=UTC)
    ulids = [new_ulid(base + timedelta(milliseconds=i)) for i in range(100)]
    assert ulids == sorted(ulids)


def test_ulid_naive_read_as_utc() -> None:
    """A naive value is interpreted as UTC, matching ``now_utc()`` on the write path."""
    naive = datetime(2026, 7, 20, 12, 0, 0)
    aware = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
    # Same 48-bit timestamp prefix (first 10 chars encode the millisecond timestamp).
    assert new_ulid(naive)[:10] == new_ulid(aware)[:10]


def test_ulid_uniqueness() -> None:
    ulids = {new_ulid() for _ in range(10_000)}
    assert len(ulids) == 10_000
