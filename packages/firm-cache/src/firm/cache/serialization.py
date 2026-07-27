"""Value coders + optional encryption.

A coder turns a cached value into bytes and back. **JSON is the default**: it covers typical
cache payloads and is safe to decode no matter who managed to write the table. ``MsgpackCoder``
covers the same value shapes in a more compact binary form (and is likewise safe to decode).
``PickleCoder`` handles arbitrary Python objects but executes code on load — opt in only when
every writer to the cache table is fully trusted. Wrapping a coder with
:func:`build_encrypted_coder` encrypts
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


# Every msgpack payload carries this one-byte tag. 0xc1 is the single byte msgpack itself
# never emits ("never used" in the spec), so a tagged payload can't be mistaken for a bare
# msgpack one — and no UTF-8 text, hence no JSON row, can start with it either. Without the
# tag, switching a live cache from the JSON default silently *misreads* rather than missing:
# JSON writes the int 1 as b"1", whose single byte 0x31 is a perfectly valid msgpack fixint 49.
# Counters are exactly the values small enough to hit that, and `increment` would persist the
# corruption. Costs one byte per entry; readers outside firm must strip it.
_MSGPACK_TAG = b"\xc1"


class MsgpackCoder:
    """Binary alternative to :class:`JSONCoder`: the same value shapes (dict/list/str/number/
    bool/None, plus raw ``bytes``) in smaller rows, and likewise no code execution on load.

    Two differences from JSON worth knowing: ``bytes`` round-trip as ``bytes`` instead of
    needing an encoding, and dict keys must be ``str``/``bytes`` — msgpack's unpacker rejects
    other key types by default (a hash-flooding guard that matters precisely because the cache
    table may be writable by others), so a dict keyed by ints writes fine but reads back as a
    miss. Requires the ``msgpack`` extra.
    """

    def __init__(self) -> None:
        try:
            import msgpack
        except ImportError as exc:
            raise ImportError(
                'The msgpack cache coder requires "msgpack". Install the msgpack extra: '
                'pip install "firm-cache[msgpack]"'
            ) from exc
        self._msgpack = msgpack

    def dumps(self, value: Any) -> bytes:
        return _MSGPACK_TAG + bytes(self._msgpack.packb(value))

    def loads(self, data: bytes) -> Any:
        if data[:1] != _MSGPACK_TAG:
            raise ValueError("not a msgpack payload written by MsgpackCoder")
        return self._msgpack.unpackb(data[1:])


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
