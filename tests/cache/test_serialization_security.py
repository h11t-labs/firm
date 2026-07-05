"""Coder defaults and encryption robustness (audit S-1 / S-3, PLAN 1.4).

JSON is the default coder — decoding it is safe no matter who managed to write the cache
table, whereas pickle executes code on load. Undecodable/undecryptable entries read as
misses, and key rotation is supported via a key list (MultiFernet).
"""

from __future__ import annotations

import pickle

import pytest

from firm.cache import Cache, JSONCoder, PickleCoder
from firm.cache.entries import write_entry


def test_default_coder_is_json(db_url) -> None:
    with Cache(database_url=db_url, auto_expire=False) as cache:
        assert isinstance(cache.coder, JSONCoder)
        cache.set("k", {"name": "Ada", "admin": True})
        assert cache.get("k") == {"name": "Ada", "admin": True}


def test_pickle_coder_is_an_explicit_opt_in(db_url) -> None:
    with Cache(database_url=db_url, coder=PickleCoder(), auto_expire=False) as cache:
        value = {"tuple": (1, 2)}  # not JSON-representable as-is
        cache.set("k", value)
        assert cache.get("k") == value


def test_undecodable_entry_reads_as_miss_not_error(db_url) -> None:
    """Old pickle rows after switching to the JSON default must degrade to misses."""
    with Cache(database_url=db_url, auto_expire=False) as cache:
        with cache.engine.begin() as conn:
            write_entry(conn, b"legacy", pickle.dumps(object), False)

        assert cache.get("legacy") is None
        assert cache.get_multi(["legacy"]) == {"legacy": None}
        assert cache.fetch("legacy", lambda: "recomputed") == "recomputed"
        assert cache.get("legacy") == "recomputed"


def test_encryption_key_rotation_via_key_list(db_url) -> None:
    fernet = pytest.importorskip("cryptography.fernet")
    old_key = fernet.Fernet.generate_key()
    new_key = fernet.Fernet.generate_key()

    with Cache(database_url=db_url, encrypt_key=old_key, auto_expire=False) as cache:
        cache.set("secret", "s3kr3t")

    # Rotated: new key first (used for writes), old key still readable.
    with Cache(database_url=db_url, encrypt_key=[new_key, old_key], auto_expire=False) as cache:
        assert cache.get("secret") == "s3kr3t"
        cache.set("secret2", "n3w")

    # Old key dropped: its entries read as misses (fetch recomputes), never a crash.
    with Cache(database_url=db_url, encrypt_key=[new_key], auto_expire=False) as cache:
        assert cache.get("secret") is None
        assert cache.get("secret2") == "n3w"
        assert cache.fetch("secret", lambda: "recomputed") == "recomputed"


def test_wrong_key_reads_as_miss(db_url) -> None:
    fernet = pytest.importorskip("cryptography.fernet")
    key_a = fernet.Fernet.generate_key()
    key_b = fernet.Fernet.generate_key()

    with Cache(database_url=db_url, encrypt_key=key_a, auto_expire=False) as cache:
        cache.set("secret", "s3kr3t")
    with Cache(database_url=db_url, encrypt_key=key_b, auto_expire=False) as cache:
        assert cache.get("secret") is None
