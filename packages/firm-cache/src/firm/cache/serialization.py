"""Value coders + optional encryption.

A coder turns a cached value into bytes and back. **JSON is the default**: it covers typical
cache payloads and is safe to decode no matter who managed to write the table. ``PickleCoder``
handles arbitrary Python objects but executes code on load — opt in only when every writer to
the cache table is fully trusted. Wrapping a coder with :func:`build_encrypted_coder` encrypts
the serialized bytes at rest with Fernet (pass a list of keys to rotate: encrypts with the
first, decrypts with any).
"""

from __future__ import annotations

import json
import pickle  # opt-in only; see PickleCoder
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Coder(Protocol):
    def dumps(self, value: Any) -> bytes: ...
    def loads(self, data: bytes) -> Any: ...


class PickleCoder:
    """Serializes arbitrary Python objects — at a price: ``pickle.loads`` executes code, so
    anyone who can write the cache table gains code execution in every process that reads it.
    Not the default for that reason; opt in only when the database is fully trusted."""

    def dumps(self, value: Any) -> bytes:
        return pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)

    def loads(self, data: bytes) -> Any:
        return pickle.loads(data)  # noqa: S301 -- opt-in coder; trusted-by-contract, see class docstring


class JSONCoder:
    def dumps(self, value: Any) -> bytes:
        return json.dumps(value).encode("utf-8")

    def loads(self, data: bytes) -> Any:
        return json.loads(data.decode("utf-8"))


class EncryptedCoder:
    def __init__(self, inner: Coder, fernet: Any) -> None:
        self._inner = inner
        self._fernet = fernet

    def dumps(self, value: Any) -> bytes:
        return self._fernet.encrypt(self._inner.dumps(value))

    def loads(self, data: bytes) -> Any:
        return self._inner.loads(self._fernet.decrypt(data))


def build_encrypted_coder(inner: Coder, key: str | bytes | Sequence[str | bytes]) -> EncryptedCoder:
    """Wrap ``inner`` with Fernet encryption.

    Pass a sequence of keys to rotate without invalidating the cache: values are encrypted
    with the first key and decrypted with whichever matches — prepend the new key, keep the
    old one until its entries have aged out, then drop it.
    """
    try:
        from cryptography.fernet import Fernet, MultiFernet
    except ImportError as exc:
        raise ImportError(
            'At-rest cache encryption requires "cryptography". Install the encryption extra: '
            'pip install "firm-cache[encryption]"'
        ) from exc

    if isinstance(key, str | bytes):
        return EncryptedCoder(inner, Fernet(key))
    if not key:
        raise ValueError("encrypt_key sequence must contain at least one key")
    return EncryptedCoder(inner, MultiFernet([Fernet(k) for k in key]))
