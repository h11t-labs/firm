"""Coder defaults and encryption robustness (audit S-1 / S-3, PLAN 1.4).

JSON is the default coder — decoding it is safe no matter who managed to write the cache
table, whereas pickle executes code on load. Undecodable/undecryptable entries read as
misses, and key rotation is supported via a key list (MultiFernet).
"""

from __future__ import annotations

import pickle

import pytest
from sqlalchemy import select

from firm._core.database import transaction
from firm.cache import Cache, JSONCoder, PickleCoder, schema
from firm.cache.entries import compute_byte_size, write_entry
from firm.cache.keys import key_hash, normalize_key

_entries = schema.entries


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


def test_encrypted_with_custom_settings(db_url: str) -> None:
    """Upstream: encryption_test.rb "encrypted with custom settings". A JSON coder + Fernet key
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
