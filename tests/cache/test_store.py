"""Cache store specs."""

from __future__ import annotations

import pytest
from sqlalchemy import delete, insert, select

from firm._core.clock import now_utc
from firm._core.database import create_engine_for, transaction
from firm.cache import Cache, JSONCoder, schema
from firm.cache.entries import compute_byte_size
from firm.cache.estimate import entry_count
from firm.cache.keys import key_hash, normalize_key

_entries = schema.entries


def test_set_get_roundtrip(cache: Cache) -> None:
    cache.set("a", {"x": 1})
    assert cache.get("a") == {"x": 1}
    assert cache.get("missing") is None


def test_overwrite_updates_value(cache: Cache) -> None:
    cache.set("a", 1)
    cache.set("a", 2)
    assert cache.get("a") == 2


def test_set_with_unless_exist_does_not_overwrite(cache: Cache) -> None:
    # upstream: solid_cache_test.rb::test_write_with_unless_exist
    assert cache.set("foo", "bar", unless_exist=True) is True
    # second write must be rejected because the key already exists
    assert cache.set("foo", "baz", unless_exist=True) is False
    assert cache.get("foo") == "bar"


def test_delete(cache: Cache) -> None:
    cache.set("a", 1)
    assert cache.delete("a") is True
    assert cache.get("a") is None
    assert cache.delete("a") is False


def test_exist(cache: Cache) -> None:
    assert cache.exist("a") is False
    cache.set("a", 1)
    assert cache.exist("a") is True


def test_fetch_computes_once(cache: Cache) -> None:
    calls: list[int] = []

    def compute() -> int:
        calls.append(1)
        return 42

    assert cache.fetch("a", compute) == 42
    assert cache.fetch("a", compute) == 42
    assert len(calls) == 1


def test_fetch_with_force_recomputes(cache: Cache) -> None:
    # upstream: cache_store_behavior.rb::test_fetch_with_forced_cache_miss
    cache.set("foo", "original")
    # force=True ignores the cached value, runs the block, and stores the new value
    assert cache.fetch("foo", lambda: "recomputed", force=True) == "recomputed"
    assert cache.get("foo") == "recomputed"


def test_fetch_cache_miss_with_skip_nil(cache: Cache) -> None:
    # upstream: cache_store_behavior.rb::test_fetch_cache_miss_with_skip_nil
    # block returns None and skip_nil=True, so nothing is stored for the key.
    assert cache.fetch("foo", lambda: None, skip_nil=True) is None
    assert cache.exist("foo") is False


def test_get_set_multi(cache: Cache) -> None:
    cache.set_multi({"a": 1, "b": 2})
    assert cache.get_multi(["a", "b", "c"]) == {"a": 1, "b": 2, "c": None}


def test_fetch_multi_returns_cached_and_computed(cache: Cache) -> None:
    # upstream: cache_store_behavior.rb::test_fetch_multi
    # fetch_multi reads the given keys, computes the misses via the block (passed the key),
    # writes the computed values back, and returns a mapping of every requested key.
    cache.set("a", "ay")

    result = cache.fetch_multi(["a", "b", "c"], lambda key: key.upper())

    assert result == {"a": "ay", "b": "B", "c": "C"}
    # the computed misses were persisted
    assert cache.get("b") == "B"
    assert cache.get("c") == "C"


def test_delete_multi_returns_count_deleted(cache: Cache) -> None:
    # upstream: cache_store_behavior.rb::test_delete_multi
    cache.set("a", 1)
    cache.set("b", 2)

    # only existing keys count toward the deleted total
    assert cache.delete_multi(["a", "b", "c"]) == 2
    assert cache.get("a") is None
    assert cache.get("b") is None


def test_delete_multi_empty_list_returns_zero(cache: Cache) -> None:
    # upstream: cache_store_behavior.rb::test_delete_multi_empty_list
    assert cache.delete_multi([]) == 0


def test_increment_and_decrement(cache: Cache) -> None:
    assert cache.increment("n") == 1
    assert cache.increment("n", 5) == 6
    assert cache.decrement("n", 2) == 4
    assert cache.get("n") == 4


def test_clear(cache: Cache) -> None:
    cache.set("a", 1)
    cache.set("b", 2)
    cache.clear()
    assert cache.get("a") is None
    assert cache.get("b") is None


def test_long_key_is_truncated_but_roundtrips(cache: Cache) -> None:
    key = "k" * 5000
    cache.set(key, "v")
    assert cache.get(key) == "v"


def test_byte_size_includes_overhead(cache: Cache) -> None:
    cache.set("a", "x")
    with cache.engine.connect() as conn:
        byte_size = conn.execute(select(schema.entries.c.byte_size)).scalar()
    assert byte_size is not None
    assert byte_size >= 140


def test_json_coder_roundtrip(db_url: str) -> None:
    store = Cache(
        database_url=db_url, coder=JSONCoder(), max_size=None, max_age=None, auto_expire=False
    )
    try:
        store.set("a", {"x": [1, 2, 3]})
        assert store.get("a") == {"x": [1, 2, 3]}
    finally:
        store.close()


def test_encryption_roundtrip_and_at_rest(db_url: str) -> None:
    pytest.importorskip("cryptography")
    from cryptography.fernet import Fernet

    store = Cache(
        database_url=db_url,
        encrypt_key=Fernet.generate_key(),
        max_size=None,
        max_age=None,
        auto_expire=False,
    )
    try:
        store.set("a", "secret-value")
        assert store.get("a") == "secret-value"
        with store.engine.connect() as conn:
            raw = conn.execute(select(schema.entries.c.value)).scalar()
        assert raw is not None
        assert b"secret-value" not in bytes(raw)
    finally:
        store.close()


# Failure safety: a read against an unavailable database degrades to a miss (Rails'
# execution_test contract) rather than raising, so a dead cache DB never 500s the caller.
# Writes deliberately still raise — see docs/comparison-to-rails.md.


def test_get_on_broken_engine_degrades_to_miss(tmp_path) -> None:
    # upstream: execution_test.rb failure-safety (Rails degrades a failed read to nil).
    good_url = f"sqlite:///{tmp_path / 'real.db'}"
    cache = Cache(database_url=good_url, max_size=None, max_age=None, auto_expire=False)
    try:
        cache.set("k", "v")
        # Point the store at a dead engine to simulate an unavailable DB mid-flight.
        cache.engine = create_engine_for(f"sqlite:////nonexistent-dir-{id(cache)}/cannot.db")
        assert cache.get("k") is None
        assert cache.exist("k") is False
        assert cache.get_multi(["k"]) == {"k": None}
    finally:
        cache.close()


def test_key_hash_collision_read_guard(cache: Cache) -> None:
    """Upstream: entry_test.rb "handles key_hash collisions". Force two distinct keys to share a
    key_hash; the read must return the row only for the key whose bytes actually match."""
    k1 = "collide-one"
    k2 = "collide-two"
    cache.set(k1, "value-one")

    kb1 = normalize_key(k1)
    kb2 = normalize_key(k2)
    shared_hash = key_hash(kb1)
    assert key_hash(kb2) != shared_hash  # genuinely different natural hashes

    # We cannot have two rows on the unique key_hash index, so replace K1's row with a row that
    # carries K1's key_hash but K2's key bytes — exactly the collision the read guard defends.
    with transaction(cache.engine) as conn:
        conn.execute(delete(_entries).where(_entries.c.key_hash == shared_hash))
        value_bytes = cache.coder.dumps("value-two")
        conn.execute(
            insert(_entries).values(
                key=kb2,
                value=value_bytes,
                key_hash=shared_hash,  # collides with K1's hash on purpose
                byte_size=compute_byte_size(kb2, value_bytes, False),
                created_at=now_utc(),
            )
        )

    # K1 looks up by shared_hash, finds the row, but the stored key bytes are K2's -> guard
    # rejects it -> miss.
    assert cache.get(k1) is None
    # K2 hashes to a DIFFERENT value than shared_hash, so it never even finds this row -> miss.
    assert cache.get(k2) is None


def test_decrement_missing_key_returns_negative_one(cache: Cache) -> None:
    """Upstream: cache_increment_decrement_behavior.rb test_decrement. Decrementing an absent key
    starts from 0 -> returns -1."""
    assert cache.decrement("nonexistent-counter") == -1
    assert cache.get("nonexistent-counter") == -1


def test_read_and_write_nil_then_fetch_does_not_recompute(cache: Cache) -> None:
    """Upstream: cache_store_behavior.rb :: read_and_write_nil + fetch_with_cached_nil.
    A stored None registers as present: exist() is True and fetch() returns the cached None
    WITHOUT recomputing (firm decides hit/miss on row presence, not on the value being non-None)."""
    cache.set("nil-key", None)
    assert cache.exist("nil-key") is True

    calls: list[int] = []

    def recompute() -> str:
        calls.append(1)
        return "recomputed"

    result = cache.fetch("nil-key", recompute)
    assert calls == [], "fetch recomputed despite a cached None (treated the None as a miss)"
    assert result is None


def test_read_and_write_false(cache: Cache) -> None:
    """Upstream: cache_store_behavior.rb test_should_read_and_write_false. False is a real, stored
    value distinct from None/missing."""
    cache.set("false-key", False)
    assert cache.get("false-key") is False
    assert cache.exist("false-key") is True


def test_get_multi_empty_list_returns_empty(cache: Cache) -> None:
    """Upstream: cache_store_behavior.rb test_read_multi_empty_list."""
    assert cache.get_multi([]) == {}


def test_set_multi_empty_mapping_is_noop(cache: Cache) -> None:
    """Upstream: cache_store_behavior.rb test_write_multi_empty_hash (empty write is a no-op)."""
    cache.set_multi({})  # must not raise
    with transaction(cache.engine) as conn:
        assert entry_count(conn) == 0


def test_keys_are_case_sensitive(cache: Cache) -> None:
    """Upstream: cache_store_behavior.rb test_keys_are_case_sensitive."""
    cache.set("A", 1)
    cache.set("a", 2)
    assert cache.get("A") == 1
    assert cache.get("a") == 2


def test_blank_key(cache: Cache) -> None:
    """Upstream: cache_store_behavior.rb test_blank_key. An empty key works."""
    cache.set("", "blank-value")
    assert cache.get("") == "blank-value"
    assert cache.exist("") is True


def test_absurd_key_characters(cache: Cache) -> None:
    """Upstream: cache_store_behavior.rb test_absurd_key_characters. Binary / odd-byte keys work."""
    weird = b"\x00\x01\xfe\xff\n\t key with spaces \xc3\x28"
    cache.set(weird, "weird-value")
    assert cache.get(weird) == "weird-value"


def test_max_key_size_disabled_short_key_not_truncated(cache: Cache) -> None:
    """Upstream: cache_store_behavior.rb test_max_key_size_disabled. A key under max_key_bytesize is
    stored verbatim (not truncated/hashed)."""
    key = "k" * 100  # well under the default 1024-byte limit
    cache.set(key, "v")
    assert cache.get(key) == "v"
    with transaction(cache.engine) as conn:
        stored = conn.execute(
            select(_entries.c.key).where(_entries.c.key_hash == key_hash(normalize_key(key)))
        ).scalar()
    assert bytes(stored) == key.encode("utf-8")
