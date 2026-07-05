"""Value coders + optional encryption.

A coder turns a cached value into bytes and back. Pickle is the default (handles arbitrary
Python objects — the cache stores your own data); JSON is available for interop. Wrapping a coder
with :func:`build_encrypted_coder` encrypts the serialized bytes at rest with Fernet.
"""

from __future__ import annotations

import json
import pickle  # caching the app's own values, not untrusted input
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Coder(Protocol):
    def dumps(self, value: Any) -> bytes: ...
    def loads(self, data: bytes) -> Any: ...


class PickleCoder:
    def dumps(self, value: Any) -> bytes:
        return pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)

    def loads(self, data: bytes) -> Any:
        return pickle.loads(data)  # data is the app's own cached values


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


def build_encrypted_coder(inner: Coder, key: str | bytes) -> EncryptedCoder:
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise ImportError(
            'At-rest cache encryption requires "cryptography". Install the encryption extra: '
            'pip install "firm[cache,encryption]"'
        ) from exc

    return EncryptedCoder(inner, Fernet(key))
