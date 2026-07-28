"""Django cache backend — firm-cache behind Django's ``CACHES`` API.

    CACHES = {
        "default": {
            "BACKEND": "firm.cache.contrib.django.FirmCache",
            "LOCATION": "",        # empty: cache in the database Django itself uses
            "TIMEOUT": 3600,       # the cache's only expiry — read on
            "OPTIONS": {"MAX_SIZE": 512 * 1024 * 1024},
        }
    }

    from django.core.cache import cache

    cache.set("greeting", "hello")
    cache.get("greeting")

**``TIMEOUT`` is cache-wide, not per entry.** firm-cache has no expiry column: an entry is alive
for ``max_age`` seconds after it was written, and that number belongs to the cache rather than to
the key (``firm.cache.store`` treats older rows as misses; ``firm.cache.expiry`` evicts them
FIFO). ``TIMEOUT`` maps straight onto ``max_age``, so the *default* timeout is honoured exactly,
entry by entry — but a per-call ``timeout=`` asking for anything else cannot be, and therefore
raises :class:`ValueError` instead of quietly storing the value with a different lifetime. Two
per-call values are exact and always accepted: this cache's own ``TIMEOUT``, and ``0``
("expire immediately", which deletes the key). An omitted ``TIMEOUT`` is Django's default of 300
seconds, not firm-cache's two weeks.

That bites hardest on ``cache_page(60)`` and other Django helpers that pass an explicit timeout.
Give those their own ``CACHES`` alias whose ``TIMEOUT`` *is* that value — pointed at its own
database, because eviction sweeps the whole ``firm_cache_entries`` table and two aliases with
different timeouts on one database would expire each other's entries. If you would rather take
the cache-wide expiry than the exception, set ``OPTIONS={"ON_ENTRY_TIMEOUT": "warn"}``.

Three more divergences from Django's cache contract, all from the same storage model:

- **A stored ``None`` reads back as a miss.** ``Cache.get`` returns ``None`` for both, so
  ``cache.get(key, "fallback")`` yields ``"fallback"`` and ``get_many()`` omits the key.
  (``has_key()`` still answers ``True`` — it asks whether the row exists.)
- **``clear()`` empties the table**, not just this alias' ``KEY_PREFIX`` — like memcached's
  ``flush_all``, and visible to every other firm-cache client on that database.
- **``MAX_ENTRIES`` is unlimited by default**, not Django's 300, and ``CULL_FREQUENCY`` is
  accepted but ignored: eviction is FIFO once ``MAX_SIZE`` or ``MAX_ENTRIES`` is exceeded.

Needs the ``[django]`` extra.
"""

from __future__ import annotations

import contextlib
import threading
import warnings
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

from sqlalchemy import Engine

try:
    from django.core.cache.backends.base import DEFAULT_TIMEOUT, BaseCache
    from django.core.exceptions import ImproperlyConfigured
    from django.utils.module_loading import import_string
except ImportError as exc:  # pragma: no cover - exercised only without the 'django' extra
    raise ImportError(
        'The firm Django cache backend requires "django". Install the django extra: '
        'pip install "firm-cache[django]"'
    ) from exc

from ..._core.contrib.django import sqlalchemy_url_for
from ..._core.database import transaction
from ..entries import read_entry, write_entry
from ..serialization import Coder, JSONCoder, PickleCoder
from ..store import Cache

# OPTIONS that are handed to Cache() unchanged.
_CACHE_OPTIONS = {
    "MAX_SIZE": "max_size",
    "MAX_ENTRIES": "max_entries",
    "MAX_KEY_BYTESIZE": "max_key_bytesize",
    "ENCRYPT_KEY": "encrypt_key",
    "AUTO_EXPIRE": "auto_expire",
    "EXPIRY_BATCH_SIZE": "expiry_batch_size",
    "BACKGROUND_EXPIRY": "background_expiry",
    "EXPIRY_INTERVAL": "expiry_interval",
    "CREATE_SCHEMA": "create_schema",
}
# OPTIONS this module reads itself, plus Django's CULL_FREQUENCY, which has no analogue here
# (eviction is FIFO, not a sampled cull) and is accepted only so a copied CACHES block still boots.
_OWN_OPTIONS = frozenset(
    {"DATABASE_ALIAS", "ENGINE", "CODER", "ON_ENTRY_TIMEOUT", "CULL_FREQUENCY"}
)

_CODERS = {"json": JSONCoder, "pickle": PickleCoder}
_TIMEOUT_POLICIES = ("error", "warn")


class EntryTimeoutWarning(RuntimeWarning):
    """Raised as a warning (instead of an error) for an unsupported per-entry timeout when
    ``OPTIONS={"ON_ENTRY_TIMEOUT": "warn"}``."""


class FirmCache(BaseCache):
    """Django ``CACHES`` backend on top of :class:`firm.cache.store.Cache`.

    Which database the cache lives in is said exactly once, in one of three ways: a ``LOCATION``
    holding a SQLAlchemy URL, ``OPTIONS['ENGINE']``, or neither — in which case it is derived
    from ``DATABASES[OPTIONS['DATABASE_ALIAS']]``. That last one is the default because it
    follows Django onto its test database instead of quietly caching in production.

    ``OPTIONS``:

    ``DATABASE_ALIAS``
        Which ``DATABASES`` entry to derive the URL from (default ``"default"``), exactly as
        ``FIRM_QUEUE['DATABASE_ALIAS']`` does for firm-queue's Django app.
    ``ENGINE``
        Reuse an existing SQLAlchemy ``Engine`` instead of opening a second pool — an ``Engine``,
        a callable returning one, or a dotted path to either. This is how the cache shares the
        queue's connection pool: point it at a ``def firm_engine(): return
        firm.queue.current_runtime().engine``.
    ``CODER``
        ``"json"`` (default), ``"pickle"``, or a dotted path to a :class:`~..serialization.Coder`.
        JSON is the default here for the same reason it is in firm-cache: ``pickle.loads``
        executes code, so anyone who can write the table would gain it in every reader. Django
        features that cache arbitrary objects (``cache_page``, cached sessions) need
        ``"pickle"`` — writing an unJSONable value under the default says so.
    ``ENCRYPT_KEY``
        Fernet key (or list of keys, newest first) for at-rest encryption of values.
    ``ON_ENTRY_TIMEOUT``
        ``"error"`` (default) or ``"warn"`` — what an unsupported per-call ``timeout=`` does.
    ``MAX_SIZE``, ``MAX_ENTRIES``, ``MAX_KEY_BYTESIZE``, ``AUTO_EXPIRE``, ``EXPIRY_BATCH_SIZE``,
    ``BACKGROUND_EXPIRY``, ``EXPIRY_INTERVAL``, ``CREATE_SCHEMA``
        Passed through to ``Cache(...)``; see its docstring. Anything else is a typo as far as
        this backend is concerned and raises ``ImproperlyConfigured``.

    The underlying ``Cache`` is available as ``.store`` for the parts of firm-cache Django has no
    name for (``fetch``, ``fetch_multi``, ``exist``).
    """

    def __init__(self, location: str, params: dict[str, Any]) -> None:
        super().__init__(params)
        options = params.get("OPTIONS") or {}
        _reject_unknown_options(options)
        self._on_entry_timeout = _timeout_policy(options)
        # Django builds one backend instance per thread (CacheHandler stores them in an
        # asgiref Local), so building a Cache here would mean an engine, a pool and an expiry
        # thread per web thread. Instances with the same CACHES entry share one.
        self.store = _shared_cache(location, params, self.default_timeout)

    # --- reads ---------------------------------------------------------------------------

    def get(self, key: Any, default: Any = None, version: int | None = None) -> Any:
        value = self.store.get(self.make_and_validate_key(key, version=version))
        # A stored None is indistinguishable from a miss here; see the module docstring.
        return default if value is None else value

    def get_many(self, keys: Iterable[Any], version: int | None = None) -> dict[Any, Any]:
        made = {self.make_and_validate_key(key, version=version): key for key in keys}
        found = self.store.get_multi(made.keys())  # one SELECT ... WHERE key_hash IN (...)
        return {made[made_key]: value for made_key, value in found.items() if value is not None}

    def has_key(self, key: Any, version: int | None = None) -> bool:
        return self.store.exist(self.make_and_validate_key(key, version=version))

    # --- writes --------------------------------------------------------------------------

    def add(
        self,
        key: Any,
        value: Any,
        timeout: Any = DEFAULT_TIMEOUT,
        version: int | None = None,
    ) -> bool:
        made = self.make_and_validate_key(key, version=version)
        if self._expire_now(timeout):
            # Nothing to store, but "did this call claim the key?" still has to be answered.
            return not self.store.exist(made)
        with self._encoding_hint():
            if self.store.set(made, value, unless_exist=True):
                return True
            # unless_exist keys off the row being there, while an entry past the cache-wide
            # expiry already reads as a miss — so "taken" has to mean "taken by a live entry".
            if self.store.exist(made):
                return False
            self.store.set(made, value)
        return True

    def set(
        self,
        key: Any,
        value: Any,
        timeout: Any = DEFAULT_TIMEOUT,
        version: int | None = None,
    ) -> None:
        made = self.make_and_validate_key(key, version=version)
        if self._expire_now(timeout):
            self.store.delete(made)
            return
        with self._encoding_hint():
            self.store.set(made, value)

    def set_many(
        self,
        data: Mapping[Any, Any],
        timeout: Any = DEFAULT_TIMEOUT,
        version: int | None = None,
    ) -> list[Any]:
        if self._expire_now(timeout):
            self.delete_many(data, version=version)
            return []
        mapping = {self.make_and_validate_key(k, version=version): v for k, v in data.items()}
        with self._encoding_hint():
            self.store.set_multi(mapping)
        return []  # nothing can fail per-key: set_multi is one transaction, all or nothing

    def touch(self, key: Any, timeout: Any = DEFAULT_TIMEOUT, version: int | None = None) -> bool:
        made = self.make_and_validate_key(key, version=version)
        if self._expire_now(timeout):
            return self.store.delete(made)
        # Row level on purpose (and from inside the package that owns those rows): expiry is
        # measured from created_at, so there is nothing to update but that.
        store = self.store
        key_bytes = store._kb(made)
        with transaction(store.engine) as conn:
            data = read_entry(conn, key_bytes, min_created_at=store._min_created_at())
            if data is None:  # missing, or already past the cache-wide expiry: do not resurrect
                return False
            # Rewriting the same bytes restarts that clock in one transaction, with no
            # decode/encode round trip of the value.
            write_entry(conn, key_bytes, data, store.encrypted)
        return True

    def delete(self, key: Any, version: int | None = None) -> bool:
        return self.store.delete(self.make_and_validate_key(key, version=version))

    def delete_many(self, keys: Iterable[Any], version: int | None = None) -> None:
        self.store.delete_multi([self.make_and_validate_key(k, version=version) for k in keys])

    def clear(self) -> None:
        # Table-wide, KEY_PREFIX and all — see the module docstring.
        self.store.clear()

    def incr(self, key: Any, delta: int = 1, version: int | None = None) -> int:
        made = self.make_and_validate_key(key, version=version)
        # firm's increment materializes a missing key at zero; Django's contract is to raise.
        # (``decr`` is BaseCache's, which routes through here with a negative delta.)
        if not self.store.exist(made):
            raise ValueError(f"Key '{key}' not found")
        return self.store.increment(made, delta)

    def close(self, **kwargs: Any) -> None:
        """Deliberately nothing.

        Django closes every cache on ``request_finished``; disposing the engine and the expiry
        pool there would rebuild both on the next request. The ``Cache`` lives as long as the
        process — call :func:`close_shared_caches` if you really need it gone.
        """

    # --- timeouts ------------------------------------------------------------------------

    def _expire_now(self, timeout: Any) -> bool:
        """Vet a per-call ``timeout``; return whether the write must expire immediately."""
        if timeout is DEFAULT_TIMEOUT:
            return False
        if timeout is not None and timeout <= 0:
            return True  # Django's "expire immediately"; exactly representable as a delete
        if timeout == self.default_timeout:
            return False  # asked for what this cache already does
        message = (
            f"This cache {_describe(self.default_timeout)} (its CACHES TIMEOUT), and firm-cache "
            f"has no per-entry expiry to give this one a timeout of {timeout!r}. "
            f"Drop the timeout argument to accept the cache-wide one, pass timeout=0 to skip "
            f"caching, or add a CACHES alias (on its own database) whose TIMEOUT is "
            f"{timeout!r}. OPTIONS={{'ON_ENTRY_TIMEOUT': 'warn'}} downgrades this to a warning "
            f"and writes with the cache-wide expiry."
        )
        if self._on_entry_timeout == "warn":
            # 4 frames up: warn -> _expire_now -> the cache method -> the caller worth blaming.
            warnings.warn(message, EntryTimeoutWarning, stacklevel=4)
            return False
        raise ValueError(message)

    @contextlib.contextmanager
    def _encoding_hint(self) -> Iterator[None]:
        """Point an unserializable value at the CODER option rather than at json's internals."""
        try:
            yield
        except TypeError as exc:
            if not isinstance(self.store.coder, JSONCoder):
                raise
            raise TypeError(
                f"{exc} — this cache stores values as JSON. Set "
                f'OPTIONS={{"CODER": "pickle"}} to cache arbitrary Python objects (pickle '
                f"executes code on load, so only where every writer to the cache table is "
                f"trusted), or cache a JSON-serializable representation instead."
            ) from exc


# --- construction ------------------------------------------------------------------------

_INSTANCES: dict[str, Cache] = {}
_LOCK = threading.Lock()


def close_shared_caches() -> None:
    """Close every ``Cache`` built for a ``CACHES`` entry and forget it.

    Backend instances hold these for the life of the process (``close()`` is a no-op), so this
    is the escape hatch: a test teardown, or just before forking a process.
    """
    with _LOCK:
        instances = list(_INSTANCES.values())
        _INSTANCES.clear()
    for cache in instances:
        cache.close()


def _shared_cache(location: str, params: Mapping[str, Any], max_age: float | None) -> Cache:
    key = _config_key({"LOCATION": location, **params})
    with _LOCK:
        cache = _INSTANCES.get(key)
        if cache is None:
            cache = _INSTANCES[key] = _build_cache(location, params, max_age)
        return cache


def _config_key(config: Any) -> str:
    """A stable string for one CACHES entry. Objects (an ``Engine``, a ``Coder``) key off
    identity — two engines for the same URL are still two pools."""
    if isinstance(config, Mapping):
        items = ",".join(f"{k!r}:{_config_key(v)}" for k, v in sorted(config.items()))
        return "{" + items + "}"
    if isinstance(config, list | tuple):
        return "[" + ",".join(_config_key(v) for v in config) + "]"
    if isinstance(config, str | bytes | int | float | bool | None):
        return repr(config)
    return f"<{type(config).__name__} {id(config):#x}>"


def _build_cache(location: str, params: Mapping[str, Any], max_age: float | None) -> Cache:
    options = params.get("OPTIONS") or {}
    kwargs: dict[str, Any] = {
        name: options[option] for option, name in _CACHE_OPTIONS.items() if option in options
    }
    coder = _resolve_coder(options.get("CODER"))
    if coder is not None:
        kwargs["coder"] = coder

    engine = _resolve_engine(options.get("ENGINE"))
    alias = options.get("DATABASE_ALIAS")
    said = [
        name
        for name, value in (
            ("LOCATION", location),
            ("OPTIONS['ENGINE']", engine),
            ("OPTIONS['DATABASE_ALIAS']", alias),
        )
        if value
    ]
    if len(said) > 1:
        raise ImproperlyConfigured(
            f"This cache says which database to use more than once ({', '.join(said)}). Keep "
            f"one of them."
        )

    if engine is not None:
        return Cache(engine=engine, max_age=max_age, **kwargs)
    return Cache(database_url=location or _django_database_url(alias), max_age=max_age, **kwargs)


def _django_database_url(alias: str | None) -> str:
    """Derive the URL from ``DATABASES``, so the cache follows Django onto its test database —
    the whole point of firm-cache is that it lives in the database you already have."""
    from django.db import DEFAULT_DB_ALIAS, connections
    from django.db.utils import ConnectionDoesNotExist

    alias = alias or DEFAULT_DB_ALIAS
    try:
        settings_dict = connections[alias].settings_dict
    except ConnectionDoesNotExist as exc:
        raise ImproperlyConfigured(
            f"This cache's OPTIONS['DATABASE_ALIAS'] is {alias!r}, which is not in DATABASES."
        ) from exc
    try:
        return sqlalchemy_url_for(settings_dict)
    except ValueError as exc:
        raise ImproperlyConfigured(
            f"{exc} Alternatively give this cache a LOCATION of its own."
        ) from exc


def _resolve_engine(value: Any) -> Engine | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = import_string(value)
    # A class is never called here: a dotted path that resolves to the wrong thing must fail as
    # a configuration error, not by constructing whatever it happened to name.
    if callable(value) and not isinstance(value, type):
        value = value()
    if not isinstance(value, Engine):
        raise ImproperlyConfigured(
            f"OPTIONS['ENGINE'] must be a SQLAlchemy Engine, a callable returning one, or a "
            f"dotted path to either; got {value!r}."
        )
    return value


def _resolve_coder(value: Any) -> Coder | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = _CODERS[value] if value in _CODERS else import_string(value)
    # runtime_checkable, so this is a dumps()/loads() check — and it answers for a class as well
    # as an instance, which is what keeps a wrong dotted path from being constructed at all.
    if not isinstance(value, Coder):
        raise ImproperlyConfigured(
            f"OPTIONS['CODER'] must be {' or '.join(sorted(_CODERS))}, or a dotted path to a "
            f"firm.cache.serialization.Coder; got {value!r}."
        )
    return value() if isinstance(value, type) else value


def _timeout_policy(options: Mapping[str, Any]) -> str:
    policy = options.get("ON_ENTRY_TIMEOUT", "error")
    if policy not in _TIMEOUT_POLICIES:
        raise ImproperlyConfigured(
            f"OPTIONS['ON_ENTRY_TIMEOUT'] must be one of {_TIMEOUT_POLICIES}; got {policy!r}."
        )
    return str(policy)


def _reject_unknown_options(options: Mapping[str, Any]) -> None:
    unknown = sorted(set(options) - set(_CACHE_OPTIONS) - _OWN_OPTIONS)
    if not unknown:
        return
    known = ", ".join(sorted(set(_CACHE_OPTIONS) | _OWN_OPTIONS))
    message = f"Unknown OPTIONS for this cache: {', '.join(unknown)}. Supported: {known}."
    if "MAX_AGE" in unknown:
        message += " firm-cache's max_age is this cache's TIMEOUT — set that instead."
    raise ImproperlyConfigured(message)


def _describe(timeout: float | None) -> str:
    if timeout is None:
        return "never expires entries"
    return f"expires entries {timeout} seconds after they are written"
