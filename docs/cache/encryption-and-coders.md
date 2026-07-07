# Encryption & coders

## Coders

A *coder* turns a value into bytes and back. The default is **JSON** (dict/list/str/num/bool/
None) — safe to decode no matter who wrote the row. `PickleCoder` handles arbitrary Python
objects and is available as an explicit opt-in, and you can supply your own coder.

```python
from firm.cache import Cache, JSONCoder, PickleCoder

Cache(database_url=...)                        # JSON by default
Cache(database_url=..., coder=PickleCoder())   # arbitrary objects — read the warning below
```

A custom coder is anything with `dumps(value) -> bytes` and `loads(bytes) -> value`:

```python
import msgpack   # pip install "firm-cache[msgpack]"

class MsgpackCoder:
    def dumps(self, value): return msgpack.packb(value, use_bin_type=True)
    def loads(self, data):  return msgpack.unpackb(data, raw=False)

Cache(database_url=..., coder=MsgpackCoder())
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
