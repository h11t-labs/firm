# Encryption & coders

## Which coder?

A *coder* turns a value into bytes and back. The default covers the common case; reach for another
when one of these lines describes you:

| You want to… | Coder | What it costs |
|---|---|---|
| cache dicts, lists, strings, numbers | `JSONCoder` — the default | nothing: readable rows, portable to any language |
| cache **`bytes`** (thumbnails, protobuf, compressed blobs), or fit **more entries** under the same `max_size` | `MsgpackCoder` | `pip install "firm-cache[msgpack]"`; rows are no longer human-readable |
| cache arbitrary Python objects (tuples, dataclasses, sets) | `PickleCoder` | executes code on load — only when every writer to the table is trusted |

```python
from firm.cache import Cache, JSONCoder, MsgpackCoder, PickleCoder

Cache(database_url=...)                        # JSON by default
Cache(database_url=..., coder=MsgpackCoder())  # compact binary; bytes stay bytes
Cache(database_url=..., coder=PickleCoder())   # arbitrary objects — read the warning below
```

**Switching coders on an existing cache is safe but not free.** Rows written by the old coder can't
be decoded by the new one, so they read as misses until they age out — plan for a cold cache, not a
migration. This is also why the default stays JSON across releases.

### More on `MsgpackCoder`

Same value shapes as JSON in a compact binary form, and just as safe to decode (no code execution on
load). Typical payloads land around a third smaller, which matters because `byte_size` drives
[eviction](eviction.md): smaller values mean more entries survive under `max_size`.

Two differences from JSON: `bytes` values round-trip as `bytes` instead of needing an encoding, and
dict keys must be `str`/`bytes` — msgpack's unpacker rejects other key types by default as a
hash-flooding guard, so a dict keyed by ints writes fine but reads back as a miss. Constructing the
coder without the extra installed raises `ImportError` naming it.

### Your own coder

Anything with `dumps(value) -> bytes` and `loads(bytes) -> value` works — a coder may return any
bytes it likes, so binary formats and compression are fair game:

```python
import zlib

from firm.cache import Cache, JSONCoder

class GzipJSONCoder:
    def __init__(self): self._inner = JSONCoder()
    def dumps(self, value): return zlib.compress(self._inner.dumps(value))
    def loads(self, data):  return self._inner.loads(zlib.decompress(data))

Cache(database_url=..., coder=GzipJSONCoder())
```

> **Security:** `PickleCoder` deserializes with `pickle`, which executes code on load — anyone
> who can write the cache table gains code execution in every process that reads it. That's why
> it is not the default: opt in only when the database is fully trusted. An entry the current
> coder can't decode (e.g. old pickle rows after switching to JSON) reads as a **miss**, so
> changing coders degrades gracefully instead of raising on every read.

## Encryption at rest

Wrap any coder with Fernet encryption by passing an `encrypt_key`:

```python
from cryptography.fernet import Fernet   # pip install "firm-cache[encryption]"

key = Fernet.generate_key()              # store this securely
cache = Cache(database_url=..., encrypt_key=key)

cache.set("secret", "s3kr3t")
# the stored `value` bytes are ciphertext; cache.get("secret") returns "s3kr3t"
```

To **rotate keys** without invalidating the cache, pass a list: values are encrypted with the
first key and decrypted with whichever matches. Prepend the new key, keep the old one until its
entries have aged out, then drop it:

```python
cache = Cache(database_url=..., encrypt_key=[new_key, old_key])
```

An entry that no configured key can decrypt reads as a miss (and `fetch` recomputes it), so
dropping a key too early costs recomputation, never a crash.

The serialized value is encrypted before it's written and decrypted on read, so the plaintext never
touches the database. Encryption adds ~170 bytes of overhead per entry, which is accounted for in
`byte_size` (and therefore in `max_size` eviction).
