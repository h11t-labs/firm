"""Parity specs ported from rails/solid_cache.

Each test cites the upstream Rails spec it mirrors and adapts it to firm's API. Per the
TEST-PORTING contract these tests only need to RUN; tests that expose a real firm divergence
from Rails are left red (xfail where the divergence is deliberate, plain-failing where it is a
suspected bug). Source is never modified here.
"""

from __future__ import annotations

import random

import pytest
from sqlalchemy import delete, func, insert, select

from firm._core.clock import now_utc
from firm._core.database import create_engine_for, transaction
from firm.cache import Cache, JSONCoder, schema
from firm.cache.entries import compute_byte_size
from firm.cache.estimate import entry_count, estimate_size
from firm.cache.expiry import EXPIRY_MULTIPLIER
from firm.cache.keys import key_hash, normalize_key

_entries = schema.entries


# --------------------------------------------------------------------------------------------
# 1. entry/size/estimate_test.rb :: "larger sample estimates" (+ gaps / overestimate-when-uniform)
#    Drive the SAMPLING branch of estimate_size: small size_estimate_samples, many uniform rows.
# --------------------------------------------------------------------------------------------


def _insert_uniform_entries(cache: Cache, count: int, value_len: int = 50) -> int:
    """Insert ``count`` rows of identical byte size directly, returning the per-row byte_size."""
    value = b"v" * value_len
    per_row = None
    with transaction(cache.engine) as conn:
        for i in range(count):
            kb = normalize_key(f"sample-key-{i:06d}")
            bs = compute_byte_size(kb, value, encrypted=False)
            per_row = bs
            conn.execute(
                insert(_entries).values(
                    key=kb,
                    value=value,
                    key_hash=key_hash(kb),
                    byte_size=bs,
                    created_at=now_utc(),
                )
            )
    assert per_row is not None
    return per_row


def _insert_varied_entries(cache: Cache, count: int, seed: int) -> int:
    """Insert ``count`` rows of *varied* byte size, returning the exact true total byte_size.

    Varied sizes are what the sampling branch is designed for: the algorithm sums the top
    ``samples`` rows exactly and statistically samples the strictly-smaller remainder.
    """
    rng = random.Random(seed)
    true_total = 0
    with transaction(cache.engine) as conn:
        for i in range(count):
            kb = normalize_key(f"varied-key-{i:06d}")
            value = b"v" * rng.randint(10, 500)
            bs = compute_byte_size(kb, value, encrypted=False)
            true_total += bs
            conn.execute(
                insert(_entries).values(
                    key=kb,
                    value=value,
                    key_hash=key_hash(kb),
                    byte_size=bs,
                    created_at=now_utc(),
                )
            )
    return true_total


def test_larger_sample_estimates_never_underestimate_uniform(db_url: str) -> None:
    """Upstream: estimate_test "larger sample estimates" + "overestimate when all samples same".

    With every row the same byte size the estimator must land in a sane band and must NEVER
    underestimate the true total. (EXPECTED TO FAIL: firm's estimator sums the top ``samples``
    rows exactly then sums only rows with ``byte_size < min_outlier``; when all sizes are equal
    that strict ``<`` excludes the entire non-outlier remainder, so it returns just the outlier
    sum — a gross underestimate. This is the real bug this spec pins down.)"""
    cache = Cache(
        database_url=db_url,
        size_estimate_samples=10,
        max_size=None,
        max_age=None,
        auto_expire=False,
    )
    try:
        n = 200
        per_row = _insert_uniform_entries(cache, n)
        true_total = n * per_row

        with transaction(cache.engine) as conn:
            assert entry_count(conn) == n
            estimates = [estimate_size(conn, samples=10) for _ in range(25)]

        # The estimator must never come in low for a uniform distribution.
        for est in estimates:
            assert est >= true_total, f"estimate {est} underestimated true total {true_total}"
        # ...and it must stay within a sane band (not wildly inflated).
        assert max(estimates) <= true_total * 3
    finally:
        cache.close()


def test_sample_estimate_with_id_gaps_is_plausible(db_url: str) -> None:
    """Upstream: estimate_test "gaps"/"more gaps" — punching id gaps must not break the sampled
    estimate. Uses varied sizes (the sampling branch's intended input) and deletes ~1/3 of the
    rows; the estimate must remain a plausible, same-order value of the true remainder."""
    cache = Cache(
        database_url=db_url,
        size_estimate_samples=10,
        max_size=None,
        max_age=None,
        auto_expire=False,
    )
    try:
        n = 200
        _insert_varied_entries(cache, n, seed=7)

        # Delete every 3rd row to punch id gaps.
        with transaction(cache.engine) as conn:
            all_ids = [int(r.id) for r in conn.execute(select(_entries.c.id))]
            to_delete = all_ids[::3]
            conn.execute(delete(_entries).where(_entries.c.id.in_(to_delete)))
            remaining = entry_count(conn)
            true_total = int(
                conn.execute(select(func.sum(_entries.c.byte_size))).scalar()  # exact remainder
            )

        assert remaining == n - len(to_delete)

        with transaction(cache.engine) as conn:
            estimates = [estimate_size(conn, samples=10) for _ in range(25)]

        # Same order of magnitude as the true remainder despite the gaps.
        for est in estimates:
            assert est > 0
            assert est <= true_total * 3
        avg = sum(estimates) / len(estimates)
        assert true_total * 0.5 <= avg <= true_total * 2.0
    finally:
        cache.close()


# --------------------------------------------------------------------------------------------
# 2. expiry_test.rb :: "expires records when the cache is full via max_size"
# --------------------------------------------------------------------------------------------


def test_max_size_eviction_keeps_total_bounded(db_url: str) -> None:
    """Upstream: expiry_test "expires records when the cache is full". Writing past the byte
    budget evicts oldest (FIFO) entries so the total stays bounded.

    auto_expire is OFF and run_once() is driven in the foreground so the test is deterministic
    and synchronous (the real probabilistic background trigger is exercised separately, below).
    firm's eviction is *approximately* FIFO — each run pulls the oldest ``batch*3`` rows as
    candidates and randomly samples ``batch`` of them — so the load-bearing invariant is that
    the byte total stays bounded, not that any one specific id is removed."""
    budget = 2000  # holds ~13 small rows; eviction settles well clear of the newest writes
    n_writes = 60
    cache = Cache(
        database_url=db_url,
        max_size=budget,
        max_age=None,
        max_entries=None,
        expiry_batch_size=2,
        size_estimate_samples=10000,
        auto_expire=False,
    )
    try:
        for i in range(n_writes):
            cache.set(f"k{i:03d}", "x")
            cache.expiry.run_once()  # mimic the per-write trigger, deterministically

        # Drain any backlog deterministically.
        for _ in range(n_writes):
            if cache.expiry.run_once() == 0:
                break

        with transaction(cache.engine) as conn:
            total = estimate_size(conn, samples=10000)
            count = entry_count(conn)
        # The total settles within one eviction batch of the budget.
        per_row = compute_byte_size(normalize_key("k000"), cache.coder.dumps("x"), False)
        assert total <= budget + cache.expiry_batch_size * per_row
        # We wrote far more than the budget, so eviction must have happened.
        assert count < n_writes
        # The most-recently written key survives (it stays clear of the oldest-rows candidate
        # window that eviction samples from).
        assert cache.get(f"k{n_writes - 1:03d}") == "x"
    finally:
        cache.close()


# --------------------------------------------------------------------------------------------
# 3. expiry_test.rb :: probabilistic trigger (below / above threshold / many writes)
# --------------------------------------------------------------------------------------------


def test_maybe_trigger_runs_when_random_below_threshold(db_url: str, monkeypatch) -> None:
    """Upstream: expiry_test "expires when random number is below threshold". Force the RNG
    below the fractional threshold and assert a run is submitted."""
    cache = Cache(
        database_url=db_url,
        max_size=None,
        max_age=None,
        auto_expire=True,
        expiry_batch_size=100,
    )
    try:
        runs: list[int] = []
        monkeypatch.setattr(cache.expiry._pool, "submit", lambda fn: runs.append(1))
        # expected = 1 * (1/100)*2 = 0.02; runs=0, fractional part=0.02. random < 0.02 -> +1 run.
        monkeypatch.setattr(random, "random", lambda: 0.0)
        cache.expiry.maybe_trigger(1)
        assert len(runs) == 1
    finally:
        cache.close()


def test_maybe_trigger_skips_when_random_above_threshold(db_url: str, monkeypatch) -> None:
    """Upstream: expiry_test "doesn't expire when above threshold"."""
    cache = Cache(
        database_url=db_url,
        max_size=None,
        max_age=None,
        auto_expire=True,
        expiry_batch_size=100,
    )
    try:
        runs: list[int] = []
        monkeypatch.setattr(cache.expiry._pool, "submit", lambda fn: runs.append(1))
        # random >= fractional part (0.02) -> no extra run, and expected<1 so 0 base runs.
        monkeypatch.setattr(random, "random", lambda: 0.99)
        cache.expiry.maybe_trigger(1)
        assert runs == []
    finally:
        cache.close()


def test_maybe_trigger_scales_with_write_count(db_url: str, monkeypatch) -> None:
    """Upstream: expiry_test "triggers multiple expiry tasks when there are many writes". A bulk
    write count triggers proportionally (writes * 2 / batch_size base runs)."""
    batch = 100
    cache = Cache(
        database_url=db_url,
        max_size=None,
        max_age=None,
        auto_expire=True,
        expiry_batch_size=batch,
    )
    try:
        runs: list[int] = []
        monkeypatch.setattr(cache.expiry._pool, "submit", lambda fn: runs.append(1))
        # No fractional rounding contribution: keep random high so only the integer base counts.
        monkeypatch.setattr(random, "random", lambda: 0.99)
        writes = 5000
        cache.expiry.maybe_trigger(writes)
        expected_runs = int(writes * (1.0 / batch) * EXPIRY_MULTIPLIER)  # 5000*0.02 = 100
        assert len(runs) == expected_runs
        assert expected_runs > 1
    finally:
        cache.close()


# --------------------------------------------------------------------------------------------
# 4. entry_test.rb :: "handles key_hash collisions" (read-side guard in entries.read_entry)
# --------------------------------------------------------------------------------------------


def test_key_hash_collision_read_guard(cache: Cache) -> None:
    """Upstream: entry_test "handles key_hash collisions". Force two distinct keys to share a
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


# --------------------------------------------------------------------------------------------
# 5. cache_increment_decrement_behavior.rb :: test_decrement (missing key)
# --------------------------------------------------------------------------------------------


def test_decrement_missing_key_returns_negative_one(cache: Cache) -> None:
    """Upstream: cache_increment_decrement_behavior test_decrement. Decrementing an absent key
    starts from 0 -> returns -1."""
    assert cache.decrement("nonexistent-counter") == -1
    assert cache.get("nonexistent-counter") == -1


# --------------------------------------------------------------------------------------------
# 6. cache_store_behavior.rb :: test_should_read_and_write_nil + test_fetch_with_cached_nil
#    EXPECTED TO FAIL: firm treats a stored None as a miss (the bug this exposes).
# --------------------------------------------------------------------------------------------


def test_read_and_write_nil_then_fetch_does_not_recompute(cache: Cache) -> None:
    """Upstream: cache_store_behavior test_should_read_and_write_nil + test_fetch_with_cached_nil.
    A stored None must register as present: exist() is True and fetch() returns the cached None
    WITHOUT recomputing. firm currently treats a stored None as a miss, so this fails red."""
    cache.set("nil-key", None)
    assert cache.exist("nil-key") is True

    calls: list[int] = []

    def recompute() -> str:
        calls.append(1)
        return "recomputed"

    result = cache.fetch("nil-key", recompute)
    assert calls == [], "fetch recomputed despite a cached None (treated the None as a miss)"
    assert result is None


# --------------------------------------------------------------------------------------------
# 7. cache_store_behavior.rb :: test_should_read_and_write_false
# --------------------------------------------------------------------------------------------


def test_read_and_write_false(cache: Cache) -> None:
    """Upstream: cache_store_behavior test_should_read_and_write_false. False is a real, stored
    value distinct from None/missing."""
    cache.set("false-key", False)
    assert cache.get("false-key") is False
    assert cache.exist("false-key") is True


# --------------------------------------------------------------------------------------------
# 8. cache_store_behavior.rb :: test_read_multi_empty_list + test_write_multi_empty_hash
# --------------------------------------------------------------------------------------------


def test_get_multi_empty_list_returns_empty(cache: Cache) -> None:
    """Upstream: cache_store_behavior test_read_multi_empty_list."""
    assert cache.get_multi([]) == {}


def test_set_multi_empty_mapping_is_noop(cache: Cache) -> None:
    """Upstream: cache_store_behavior test_write_multi_empty_hash. A no-op that must not crash."""
    cache.set_multi({})  # must not raise
    with transaction(cache.engine) as conn:
        assert entry_count(conn) == 0


# --------------------------------------------------------------------------------------------
# 9. cache_store_behavior.rb :: case sensitivity / blank key / absurd characters / max-key-size
# --------------------------------------------------------------------------------------------


def test_keys_are_case_sensitive(cache: Cache) -> None:
    """Upstream: cache_store_behavior test_keys_are_case_sensitive."""
    cache.set("A", 1)
    cache.set("a", 2)
    assert cache.get("A") == 1
    assert cache.get("a") == 2


def test_blank_key(cache: Cache) -> None:
    """Upstream: cache_store_behavior test_blank_key. An empty key works."""
    cache.set("", "blank-value")
    assert cache.get("") == "blank-value"
    assert cache.exist("") is True


def test_absurd_key_characters(cache: Cache) -> None:
    """Upstream: cache_store_behavior test_absurd_key_characters. Binary / odd-byte keys work."""
    weird = b"\x00\x01\xfe\xff\n\t key with spaces \xc3\x28"
    cache.set(weird, "weird-value")
    assert cache.get(weird) == "weird-value"


def test_max_key_size_disabled_short_key_not_truncated(cache: Cache) -> None:
    """Upstream: cache_store_behavior test_max_key_size_disabled. A key under max_key_bytesize is
    stored verbatim (not truncated/hashed)."""
    key = "k" * 100  # well under the default 1024-byte limit
    cache.set(key, "v")
    assert cache.get(key) == "v"
    with transaction(cache.engine) as conn:
        stored = conn.execute(
            select(_entries.c.key).where(_entries.c.key_hash == key_hash(normalize_key(key)))
        ).scalar()
    assert bytes(stored) == key.encode("utf-8")


# --------------------------------------------------------------------------------------------
# 10. encryption_test.rb :: "encrypted with custom settings"
# --------------------------------------------------------------------------------------------


def test_encrypted_with_custom_settings(db_url: str) -> None:
    """Upstream: encryption_test "encrypted with custom settings". A JSON coder + Fernet key
    round-trips, the plaintext is absent from the raw DB bytes, and the encrypted byte_size
    overhead differs from the unencrypted overhead."""
    pytest.importorskip("cryptography")
    from cryptography.fernet import Fernet

    key = Fernet.generate_key()
    store = Cache(
        database_url=db_url,
        coder=JSONCoder(),
        encrypt_key=key,
        max_size=None,
        max_age=None,
        auto_expire=False,
    )
    try:
        secret = {"password": "super-secret-token"}
        store.set("creds", secret)
        assert store.get("creds") == secret

        with transaction(store.engine) as conn:
            row = conn.execute(
                select(_entries.c.value, _entries.c.byte_size).where(
                    _entries.c.key_hash == key_hash(normalize_key("creds"))
                )
            ).first()
        assert row is not None
        raw = bytes(row.value)
        assert b"super-secret-token" not in raw

        # The recorded byte_size uses the encryption overhead (170) not the plain overhead (140).
        kb = normalize_key("creds")
        encrypted_size = compute_byte_size(kb, raw, encrypted=True)
        plain_size = compute_byte_size(kb, raw, encrypted=False)
        assert encrypted_size != plain_size
        assert int(row.byte_size) == encrypted_size
    finally:
        store.close()


# --------------------------------------------------------------------------------------------
# 11. execution_test.rb :: failure-safety when the DB is unavailable.
#     firm MAY intend to raise (a divergence from Rails' degrade-to-miss). Mark xfail.
# --------------------------------------------------------------------------------------------


@pytest.mark.xfail(
    reason="decide: degrade-to-miss vs raise — see comparison-to-rails.md",
    strict=False,
)
def test_get_on_broken_engine_degrades_to_miss(tmp_path) -> None:
    """Upstream: execution_test failure-safety. Rails degrades a failed cache read to a miss
    (returns nil). firm may instead raise; xfail records that open decision."""
    # Build a cache on a real engine (so the schema exists), then point it at a dead engine.
    good_url = f"sqlite:///{tmp_path / 'real.db'}"
    cache = Cache(database_url=good_url, max_size=None, max_age=None, auto_expire=False)
    try:
        cache.set("k", "v")
        # Dispose the engine out from under the store to simulate an unavailable DB.
        broken = create_engine_for(f"sqlite:////nonexistent-dir-{id(cache)}/cannot.db")
        cache.engine = broken
        assert cache.get("k") is None  # Rails would return a miss; firm likely raises -> xfail
    finally:
        cache.close()
