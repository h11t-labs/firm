"""Tracked parity gaps: solid_cache API surface firm has not ported.

Each test calls the *intended* firm API for a solid_cache behavior that does not yet
exist in ``firm.cache.store.Cache``. They are marked ``xfail(strict=False)`` so they
collect and run today (reported XFAIL — typically the body raises ``TypeError`` for an
unknown kwarg or ``AttributeError`` for a missing method), and so that removing the
xfail later runs a *real* assertion of the feature.

Each upstream solid_cache test is cited in a comment. Confirmed absent against
``src/firm/cache/store.py`` on 2026-06-30: ``Cache`` exposes
get/set/fetch/delete/exist/get_multi/set_multi/increment/decrement/clear — and none of
the methods/options exercised below.

firm uses get/set/exist/delete naming (not Rails read/write), so these tests target the
firm-native method names with the new options/methods layered on.
"""

from __future__ import annotations

import time

import pytest

from firm.cache import Cache


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


def test_set_with_unless_exist_does_not_overwrite(cache: Cache) -> None:
    # upstream: solid_cache_test.rb::test_write_with_unless_exist
    assert cache.set("foo", "bar", unless_exist=True) is True
    # second write must be rejected because the key already exists
    assert cache.set("foo", "baz", unless_exist=True) is False
    assert cache.get("foo") == "bar"


@pytest.mark.xfail(
    reason="fetch force not ported — decide: implement or document as divergence in "
    "comparison-to-rails.md",
    strict=False,
)
def test_fetch_with_force_recomputes(cache: Cache) -> None:
    # upstream: cache_store_behavior.rb::test_fetch_with_forced_cache_miss
    cache.set("foo", "original")

    # force=True ignores the cached value, runs the block, and stores the new value
    assert cache.fetch("foo", lambda: "recomputed", force=True) == "recomputed"
    assert cache.get("foo") == "recomputed"


@pytest.mark.xfail(
    reason="fetch skip_nil not ported — decide: implement or document as divergence in "
    "comparison-to-rails.md",
    strict=False,
)
def test_fetch_cache_miss_with_skip_nil(cache: Cache) -> None:
    # upstream: cache_store_behavior.rb::test_fetch_cache_miss_with_skip_nil
    # block returns None and skip_nil=True, so nothing is stored for the key.
    assert cache.fetch("foo", lambda: None, skip_nil=True) is None
    assert cache.exist("foo") is False


@pytest.mark.xfail(
    reason="per-entry expires_in not ported — decide: implement or document as divergence "
    "in comparison-to-rails.md",
    strict=False,
)
def test_set_with_expires_in_expires_entry(cache: Cache) -> None:
    # upstream: cache_store_behavior.rb::test_expires_in
    cache.set("foo", "bar", expires_in=0.05)
    assert cache.get("foo") == "bar"

    time.sleep(0.1)
    # past its per-entry TTL the key reads as a miss
    assert cache.get("foo") is None


@pytest.mark.xfail(
    reason="per-entry expires_at not ported — decide: implement or document as divergence "
    "in comparison-to-rails.md",
    strict=False,
)
def test_set_with_expires_at_expires_entry(cache: Cache) -> None:
    # upstream: cache_store_behavior.rb::test_expires_at
    cache.set("foo", "bar", expires_at=time.time() + 0.05)
    assert cache.get("foo") == "bar"

    time.sleep(0.1)
    # once the absolute expiry timestamp passes the key reads as a miss
    assert cache.get("foo") is None
