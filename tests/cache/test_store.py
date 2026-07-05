"""Cache store specs."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from firm.cache import Cache, JSONCoder, schema


def test_set_get_roundtrip(cache: Cache) -> None:
    cache.set("a", {"x": 1})
    assert cache.get("a") == {"x": 1}
    assert cache.get("missing") is None


def test_overwrite_updates_value(cache: Cache) -> None:
    cache.set("a", 1)
    cache.set("a", 2)
    assert cache.get("a") == 2


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


def test_get_set_multi(cache: Cache) -> None:
    cache.set_multi({"a": 1, "b": 2})
    assert cache.get_multi(["a", "b", "c"]) == {"a": 1, "b": 2, "c": None}


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
