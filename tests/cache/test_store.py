"""Cache store specs."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from firm._core.database import create_engine_for
from firm.cache import Cache, JSONCoder, schema


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
