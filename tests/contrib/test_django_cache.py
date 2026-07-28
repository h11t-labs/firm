"""Specs for the Django ``CACHES`` backend (``firm.cache.contrib.django.FirmCache``).

Django itself is booted once per session by the ``django_project`` fixture in ``conftest.py``
(settings.configure() + django.setup() are process-global and one-shot). Only the specs that
read ``DATABASES`` or go through ``django.core.cache.caches`` actually need it, but the whole
module takes it so no ordering makes a second configure() attempt.
"""

from __future__ import annotations

import subprocess
import sys
import warnings
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from django.core.cache import caches
from django.core.cache.backends.base import DEFAULT_TIMEOUT
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from sqlalchemy import select, update

from firm._core.clock import now_utc
from firm._core.database import create_engine_for
from firm.cache import Cache, PickleCoder, schema
from firm.cache.contrib.django import EntryTimeoutWarning, FirmCache, close_shared_caches

pytestmark = pytest.mark.usefixtures("django_project")

_entries = schema.entries


@pytest.fixture(autouse=True)
def _close_shared_caches() -> Iterator[None]:
    """Every test builds its own Cache (its own tmp database); none may outlive the test."""
    try:
        yield
    finally:
        close_shared_caches()


@pytest.fixture
def url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'cache.db'}"


@pytest.fixture
def make_cache(url: str):
    def build(**params: Any) -> FirmCache:
        return FirmCache(params.pop("LOCATION", url), params)

    return build


@pytest.fixture
def cache(make_cache) -> FirmCache:
    return make_cache()


def _backdate(cache: FirmCache, seconds: float) -> None:
    """Age every row, so TTL behaviour is deterministic without sleeping."""
    with cache.store.engine.begin() as conn:
        conn.execute(update(_entries).values(created_at=now_utc() - timedelta(seconds=seconds)))


def _stored_keys(cache: FirmCache) -> list[str]:
    with cache.store.engine.begin() as conn:
        return [bytes(row.key).decode() for row in conn.execute(select(_entries.c.key))]


# --- the Django cache API ----------------------------------------------------------------


def test_set_and_get(cache: FirmCache) -> None:
    cache.set("greeting", "hello")
    assert cache.get("greeting") == "hello"


def test_get_returns_the_default_on_a_miss(cache: FirmCache) -> None:
    assert cache.get("nope") is None
    assert cache.get("nope", "fallback") == "fallback"


def test_add_only_stores_when_absent(cache: FirmCache) -> None:
    assert cache.add("k", "first") is True
    assert cache.add("k", "second") is False
    assert cache.get("k") == "first"


def test_add_treats_an_expired_entry_as_absent(make_cache) -> None:
    # The row is still there but reads as a miss, so add() must be able to claim the key —
    # otherwise get_or_set() and add()-based locks would jam until eviction caught up.
    cache = make_cache(TIMEOUT=300)
    cache.add("k", "old")
    _backdate(cache, 600)

    assert cache.get("k") is None
    assert cache.add("k", "new") is True
    assert cache.get("k") == "new"


def test_get_or_set(cache: FirmCache) -> None:
    assert cache.get_or_set("k", "computed") == "computed"
    assert cache.get_or_set("k", "ignored") == "computed"
    assert cache.get_or_set("callable", lambda: 42) == 42


def test_delete(cache: FirmCache) -> None:
    cache.set("k", 1)
    assert cache.delete("k") is True
    assert cache.delete("k") is False
    assert cache.get("k") is None


def test_has_key_and_contains(cache: FirmCache) -> None:
    assert cache.has_key("k") is False
    cache.set("k", 1)
    assert cache.has_key("k") is True
    assert "k" in cache


def test_clear_empties_the_table(cache: FirmCache) -> None:
    cache.set_many({"a": 1, "b": 2})
    cache.clear()
    assert cache.get_many(["a", "b"]) == {}
    assert _stored_keys(cache) == []


def test_get_many_omits_missing_keys(cache: FirmCache) -> None:
    cache.set("a", 1)
    assert cache.get_many(["a", "b"]) == {"a": 1}


def test_set_many_and_delete_many(cache: FirmCache) -> None:
    assert cache.set_many({"a": 1, "b": 2, "c": 3}) == []
    cache.delete_many(["a", "c"])
    assert cache.get_many(["a", "b", "c"]) == {"b": 2}


def test_many_variants_use_the_multi_queries(cache: FirmCache, monkeypatch) -> None:
    # A per-key loop would work but would cost one round trip per key; keep the batch APIs wired.
    calls: list[str] = []
    for name in ("get_multi", "set_multi", "delete_multi"):
        original = getattr(Cache, name)
        monkeypatch.setattr(
            Cache,
            name,
            lambda self, arg, _n=name, _o=original: (calls.append(_n), _o(self, arg))[1],
        )
    cache.set_many({"a": 1})
    cache.get_many(["a"])
    cache.delete_many(["a"])
    assert calls == ["set_multi", "get_multi", "delete_multi"]


def test_incr_and_decr(cache: FirmCache) -> None:
    cache.set("n", 1)
    assert cache.incr("n") == 2
    assert cache.incr("n", 10) == 12
    assert cache.decr("n", 2) == 10
    assert cache.get("n") == 10


def test_incr_on_a_missing_key_raises(cache: FirmCache) -> None:
    # Django's contract; firm's increment would happily materialize the key at zero.
    with pytest.raises(ValueError, match="not found"):
        cache.incr("nope")


def test_incr_on_an_expired_key_raises(make_cache) -> None:
    cache = make_cache(TIMEOUT=300)
    cache.set("n", 1)
    _backdate(cache, 600)
    with pytest.raises(ValueError, match="not found"):
        cache.incr("n")


def test_touch_restarts_the_entrys_clock(make_cache) -> None:
    cache = make_cache(TIMEOUT=300)
    cache.set("k", "v")
    _backdate(cache, 299)  # still alive, but only just

    assert cache.touch("k") is True

    with cache.store.engine.begin() as conn:
        created_at = conn.execute(select(_entries.c.created_at)).scalar_one()
    assert (now_utc() - created_at) < timedelta(seconds=5)
    assert cache.get("k") == "v"


def test_touch_reports_a_missing_or_expired_key(make_cache) -> None:
    cache = make_cache(TIMEOUT=300)
    assert cache.touch("nope") is False

    cache.set("k", "v")
    _backdate(cache, 600)
    assert cache.touch("k") is False  # expired entries are not resurrected
    assert cache.get("k") is None


def test_close_does_not_dispose_the_engine(cache: FirmCache) -> None:
    # Django closes every cache on request_finished; that must not tear down the pool.
    cache.set("k", "v")
    cache.close()
    assert cache.get("k") == "v"


# --- TIMEOUT: cache-wide, honoured as such -----------------------------------------------


def test_timeout_becomes_the_cache_wide_max_age(make_cache) -> None:
    cache = make_cache(TIMEOUT=300)
    assert cache.store.max_age == 300

    cache.set("k", "v")
    _backdate(cache, 299)
    assert cache.get("k") == "v"
    _backdate(cache, 301)
    assert cache.get("k") is None
    assert cache.has_key("k") is False


def test_default_timeout_is_djangos_300(cache: FirmCache) -> None:
    assert cache.store.max_age == 300


def test_timeout_none_never_expires(make_cache) -> None:
    cache = make_cache(TIMEOUT=None)
    assert cache.store.max_age is None

    cache.set("k", "v")
    _backdate(cache, 10 * 365 * 24 * 3600)
    assert cache.get("k") == "v"


@pytest.mark.parametrize("timeout", [DEFAULT_TIMEOUT, 300])
def test_a_timeout_equal_to_the_cache_wide_one_is_accepted(make_cache, timeout: Any) -> None:
    cache = make_cache(TIMEOUT=300)
    cache.set("k", "v", timeout)
    assert cache.add("other", "v", timeout) is True
    assert cache.touch("k", timeout) is True
    assert cache.set_many({"m": 1}, timeout) == []
    assert cache.get("k") == "v"


def test_a_timeout_of_none_is_accepted_when_the_cache_never_expires(make_cache) -> None:
    cache = make_cache(TIMEOUT=None)
    cache.set("k", "v", None)
    assert cache.get("k") == "v"


# --- TIMEOUT: a different per-entry timeout is refused, not faked -------------------------


def _timeout_calls(cache: FirmCache) -> list:
    return [
        lambda: cache.set("k", "v", 60),
        lambda: cache.add("k", "v", 60),
        lambda: cache.touch("k", 60),
        lambda: cache.set_many({"k": "v"}, 60),
        lambda: cache.get_or_set("k", "v", 60),
    ]


@pytest.mark.parametrize("index", range(5))
def test_an_unsupported_per_entry_timeout_raises(make_cache, index: int) -> None:
    cache = make_cache(TIMEOUT=300)
    with pytest.raises(ValueError) as exc:
        _timeout_calls(cache)[index]()
    message = str(exc.value)
    assert "300" in message  # what this cache does do
    assert "60" in message  # ... and what was asked for
    assert "TIMEOUT" in message  # ... and the knob that would fix it
    assert _stored_keys(cache) == []  # nothing was written behind a rejected timeout


def test_the_refusal_names_a_never_expiring_cache_too(make_cache) -> None:
    cache = make_cache(TIMEOUT=None)
    with pytest.raises(ValueError, match="never expires entries"):
        cache.set("k", "v", 60)


def test_warn_policy_writes_with_the_cache_wide_expiry(make_cache) -> None:
    cache = make_cache(TIMEOUT=300, OPTIONS={"ON_ENTRY_TIMEOUT": "warn"})
    with pytest.warns(EntryTimeoutWarning, match="no per-entry expiry"):
        cache.set("k", "v", 60)

    assert cache.get("k") == "v"
    _backdate(cache, 120)
    assert cache.get("k") == "v"  # the 60s it asked for did not apply; the cache's 300 did


def test_warn_policy_covers_every_write(make_cache) -> None:
    cache = make_cache(TIMEOUT=300, OPTIONS={"ON_ENTRY_TIMEOUT": "warn"})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cache.set("a", 1, 60)
        cache.add("b", 1, 60)
        cache.set_many({"c": 1}, 60)
        cache.touch("a", 60)
    assert len(caught) == 4
    assert cache.get_many(["a", "b", "c"]) == {"a": 1, "b": 1, "c": 1}


# --- TIMEOUT: zero is exactly representable, so it is honoured ----------------------------


def test_set_with_timeout_zero_stores_nothing(cache: FirmCache) -> None:
    cache.set("k", "v", 0)
    assert cache.get("k") is None
    assert _stored_keys(cache) == []


def test_set_with_timeout_zero_removes_an_existing_entry(cache: FirmCache) -> None:
    cache.set("k", "v")
    cache.set("k", "v", 0)
    assert cache.has_key("k") is False


def test_add_with_timeout_zero_claims_the_key_without_storing(cache: FirmCache) -> None:
    assert cache.add("k", "v", 0) is True
    assert cache.get("k") is None

    cache.set("k", "kept")
    assert cache.add("k", "v", 0) is False
    assert cache.get("k") == "kept"  # a live entry is never dropped by a losing add


def test_touch_with_timeout_zero_expires_the_key(cache: FirmCache) -> None:
    cache.set("k", "v")
    assert cache.touch("k", 0) is True
    assert cache.has_key("k") is False
    assert cache.touch("k", 0) is False


def test_set_many_with_timeout_zero_deletes_the_keys(cache: FirmCache) -> None:
    cache.set_many({"a": 1, "b": 2})
    cache.set_many({"a": 9}, 0)
    assert cache.get_many(["a", "b"]) == {"b": 2}


def test_a_negative_timeout_also_means_expired(cache: FirmCache) -> None:
    cache.set("k", "v")
    cache.set("k", "v", -1)
    assert cache.has_key("k") is False


# --- KEY_PREFIX, VERSION, KEY_FUNCTION ---------------------------------------------------


def test_keys_are_stored_with_prefix_and_version(make_cache) -> None:
    cache = make_cache(KEY_PREFIX="site", VERSION=2)
    cache.set("k", "v")
    assert _stored_keys(cache) == ["site:2:k"]
    assert cache.get("k") == "v"


def test_versions_are_isolated(cache: FirmCache) -> None:
    cache.set("k", "v1", version=1)
    cache.set("k", "v2", version=2)
    assert cache.get("k", version=1) == "v1"
    assert cache.get("k", version=2) == "v2"
    assert cache.get_many(["k"], version=2) == {"k": "v2"}

    cache.delete("k", version=1)
    assert cache.get("k", version=1) is None
    assert cache.get("k", version=2) == "v2"


def test_incr_version_moves_the_entry(cache: FirmCache) -> None:
    cache.set("k", "v")
    assert cache.incr_version("k") == 2
    assert cache.get("k") is None
    assert cache.get("k", version=2) == "v"


def test_prefixes_isolate_two_caches_on_one_database(url: str) -> None:
    a = FirmCache(url, {"KEY_PREFIX": "a"})
    b = FirmCache(url, {"KEY_PREFIX": "b"})
    a.set("k", "from-a")
    b.set("k", "from-b")
    assert a.get("k") == "from-a"
    assert b.get("k") == "from-b"

    # ... but clear() is table-wide, as documented: it is not scoped to a prefix.
    a.clear()
    assert b.get("k") is None


def test_key_function_is_honoured(make_cache) -> None:
    cache = make_cache(KEY_FUNCTION=lambda key, prefix, version: f"{key}|{version}")
    cache.set("k", "v")
    assert _stored_keys(cache) == ["k|1"]


def test_a_long_key_warns_like_every_other_backend(cache: FirmCache) -> None:
    from django.core.cache.backends.base import CacheKeyWarning

    with pytest.warns(CacheKeyWarning):
        cache.set("x" * 300, "v")
        assert cache.get("x" * 300) == "v"  # warned about, then stored and read back anyway


# --- values ------------------------------------------------------------------------------


def test_values_are_json_by_default(cache: FirmCache) -> None:
    cache.set("k", {"a": [1, 2], "b": None})
    assert cache.get("k") == {"a": [1, 2], "b": None}
    with Cache(engine=cache.store.engine, max_age=None) as plain:
        assert plain.get(":1:k") == {"a": [1, 2], "b": None}  # a plain firm-cache client agrees


def test_an_unserializable_value_points_at_the_coder_option(cache: FirmCache) -> None:
    with pytest.raises(TypeError, match="CODER"):
        cache.set("k", object())


@pytest.mark.parametrize("coder", ["pickle", "firm.cache.serialization.PickleCoder"])
def test_pickle_coder_stores_arbitrary_objects(make_cache, coder: str) -> None:
    cache = make_cache(OPTIONS={"CODER": coder})
    assert isinstance(cache.store.coder, PickleCoder)

    moment = now_utc()
    cache.set("k", moment)
    assert cache.get("k") == moment


def test_a_cached_none_reads_as_a_miss(cache: FirmCache) -> None:
    # Documented divergence: firm's Cache.get cannot tell a stored None from an absent row.
    cache.set("k", None)
    assert cache.get("k") is None
    assert cache.get("k", "fallback") == "fallback"
    assert cache.get_many(["k"]) == {}
    assert cache.has_key("k") is True  # the row is there, and that is what exist() asks


def test_encrypt_key_option(make_cache) -> None:
    from cryptography.fernet import Fernet

    cache = make_cache(OPTIONS={"ENCRYPT_KEY": Fernet.generate_key().decode()})
    cache.set("k", "secret")
    assert cache.get("k") == "secret"
    with cache.store.engine.begin() as conn:
        assert b"secret" not in conn.execute(select(_entries.c.value)).scalar_one()


# --- OPTIONS -----------------------------------------------------------------------------


def test_options_reach_the_cache(make_cache) -> None:
    cache = make_cache(OPTIONS={"MAX_SIZE": 1234, "MAX_ENTRIES": 5, "MAX_KEY_BYTESIZE": 64})
    assert cache.store.max_size == 1234
    assert cache.store.max_entries == 5
    assert cache.store.max_key_bytesize == 64


def test_max_entries_is_unlimited_unless_asked_for(cache: FirmCache) -> None:
    # BaseCache defaults MAX_ENTRIES to 300 for LocMemCache's benefit; inheriting that would
    # silently cap a database-backed cache at 300 rows.
    assert cache.store.max_entries is None


def test_cull_frequency_is_accepted_and_ignored(make_cache) -> None:
    cache = make_cache(OPTIONS={"CULL_FREQUENCY": 2})
    cache.set("k", "v")
    assert cache.get("k") == "v"


def test_an_unknown_option_is_rejected(make_cache) -> None:
    with pytest.raises(ImproperlyConfigured, match="MAX_SIZ"):
        make_cache(OPTIONS={"MAX_SIZ": 10})


def test_max_age_option_points_at_timeout(make_cache) -> None:
    with pytest.raises(ImproperlyConfigured, match="TIMEOUT"):
        make_cache(OPTIONS={"MAX_AGE": 60})


def test_an_invalid_timeout_policy_is_rejected(make_cache) -> None:
    with pytest.raises(ImproperlyConfigured, match="ON_ENTRY_TIMEOUT"):
        make_cache(OPTIONS={"ON_ENTRY_TIMEOUT": "shrug"})


def test_an_invalid_coder_is_rejected(make_cache) -> None:
    with pytest.raises(ImproperlyConfigured, match="CODER"):
        make_cache(OPTIONS={"CODER": "firm.cache.store.Cache"})


def test_engine_option_shares_an_existing_engine(url: str, make_cache) -> None:
    engine = create_engine_for(url)
    try:
        cache = make_cache(LOCATION="", OPTIONS={"ENGINE": engine})
        assert cache.store.engine is engine
        cache.set("k", "v")
        assert cache.get("k") == "v"
    finally:
        close_shared_caches()
        engine.dispose()


def test_engine_option_accepts_a_dotted_path(url: str, monkeypatch, make_cache) -> None:
    # The documented shape: a factory handing over firm-queue's engine, named by dotted path.
    engine = create_engine_for(url)
    monkeypatch.setattr("firm.cache.contrib.django._test_engine", lambda: engine, raising=False)
    try:
        path = "firm.cache.contrib.django._test_engine"
        assert make_cache(LOCATION="", OPTIONS={"ENGINE": path}).store.engine is engine
    finally:
        close_shared_caches()
        engine.dispose()


def test_naming_the_database_twice_is_rejected(url: str, make_cache) -> None:
    engine = create_engine_for(url)
    try:
        with pytest.raises(ImproperlyConfigured, match="more than once"):
            make_cache(OPTIONS={"ENGINE": engine})  # ... on top of the LOCATION fixture
        with pytest.raises(ImproperlyConfigured, match="more than once"):
            make_cache(OPTIONS={"DATABASE_ALIAS": "other"})
    finally:
        engine.dispose()


def test_a_bad_engine_option_is_rejected(make_cache) -> None:
    with pytest.raises(ImproperlyConfigured, match="Engine"):
        make_cache(LOCATION="", OPTIONS={"ENGINE": "firm.cache.store.Cache"})


# --- LOCATION ----------------------------------------------------------------------------


def test_an_empty_location_caches_in_djangos_own_database(django_project) -> None:
    # No LOCATION and no ENGINE: the cache lands in DATABASES["default"], which is what makes it
    # follow Django onto its test database.
    cache = FirmCache("", {})
    cache.set("k", "v")
    assert cache.get("k") == "v"
    assert cache.store.engine.url.database == str(django_project / "django.db")


def test_database_alias_picks_another_databases_entry(django_project) -> None:
    cache = FirmCache("", {"OPTIONS": {"DATABASE_ALIAS": "other"}})
    cache.set("k", "v")
    assert cache.store.engine.url.database == str(django_project / "other.db")


def test_an_unknown_database_alias_is_rejected() -> None:
    with pytest.raises(ImproperlyConfigured, match="not in DATABASES"):
        FirmCache("", {"OPTIONS": {"DATABASE_ALIAS": "replica"}})


# --- one Cache per process, not per thread -----------------------------------------------


def test_instances_of_the_same_alias_share_one_cache(url: str) -> None:
    # Django builds a backend per thread; an engine + pool + expiry pool per thread would not do.
    params = {"TIMEOUT": 60, "OPTIONS": {"MAX_SIZE": 10}}
    assert FirmCache(url, dict(params)).store is FirmCache(url, dict(params)).store


def test_different_settings_get_different_caches(url: str) -> None:
    assert FirmCache(url, {"TIMEOUT": 60}).store is not FirmCache(url, {"TIMEOUT": 120}).store


def test_close_shared_caches_closes_them(url: str) -> None:
    cache = FirmCache(url, {})
    close_shared_caches()
    assert FirmCache(url, {}).store is not cache.store


# --- wired up as Django sees it ----------------------------------------------------------


def test_through_the_caches_registry(url: str) -> None:
    entry = {
        "BACKEND": "firm.cache.contrib.django.FirmCache",
        "LOCATION": url,
        "TIMEOUT": 900,
        "KEY_PREFIX": "app",
        "OPTIONS": {"CODER": "json"},
    }
    with override_settings(CACHES={"firm": entry}):
        cache = caches["firm"]  # imported by dotted path and handed (LOCATION, params)
        assert isinstance(cache, FirmCache)
        cache.set("k", {"v": 1})
        assert cache.get("k") == {"v": 1}
        assert cache.store.max_age == 900
        assert _stored_keys(cache) == ["app:1:k"]


def test_a_bad_backend_path_is_djangos_problem_not_ours(url: str) -> None:
    entry = {"BACKEND": "firm.cache.contrib.django.NoSuchCache", "LOCATION": url}
    with override_settings(CACHES={"firm": entry}), pytest.raises(ImproperlyConfigured):
        caches["firm"]


# --- firm-cache stays importable without Django -------------------------------------------


def _run(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell, no external input
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )


def test_importing_firm_cache_does_not_import_django() -> None:
    """Django is an optional extra: only this contrib module may reach for it."""
    result = _run("import sys, firm.cache\nassert 'django' not in sys.modules\n")
    assert result.returncode == 0, result.stderr


def test_importing_the_backend_without_django_says_which_extra_to_install() -> None:
    """Unlike the rest of firm's contrib, this module needs Django at import time — it subclasses
    BaseCache. Run out-of-process with ``django`` poisoned; this session has it installed."""
    result = _run(
        "import sys; sys.modules['django'] = None\n"
        "try:\n"
        "    import firm.cache.contrib.django\n"
        "except ImportError as exc:\n"
        "    assert 'firm-cache[django]' in str(exc), exc\n"
        "else:\n"
        "    raise AssertionError('expected an ImportError')\n"
    )
    assert result.returncode == 0, result.stderr
