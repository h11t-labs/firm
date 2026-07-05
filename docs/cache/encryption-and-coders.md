# Encryption & coders

## Coders

A *coder* turns a value into bytes and back. The default is **pickle**, which handles arbitrary
Python objects (the cache stores your own data, not untrusted input). JSON is available for
interop, and you can supply your own.

```python
from firm.cache import Cache, JSONCoder, PickleCoder

Cache(database_url=..., coder=JSONCoder())     # JSON values only (dict/list/str/num/bool/None)
Cache(database_url=..., coder=PickleCoder())   # default
```

A custom coder is anything with `dumps(value) -> bytes` and `loads(bytes) -> value`:

```python
import msgpack   # pip install "firm[cache,msgpack]"

class MsgpackCoder:
    def dumps(self, value): return msgpack.packb(value, use_bin_type=True)
    def loads(self, data):  return msgpack.unpackb(data, raw=False)

Cache(database_url=..., coder=MsgpackCoder())
```

> **Security:** `PickleCoder` deserializes with `pickle`, which can execute arbitrary code. That's
> fine for values your own app wrote, but never point a pickle-coded cache at a database other
> processes can write untrusted bytes into. Use `JSONCoder` (or msgpack) if that's a concern.

## Encryption at rest

Wrap any coder with Fernet encryption by passing an `encrypt_key`:

```python
from cryptography.fernet import Fernet   # pip install "firm[cache,encryption]"

key = Fernet.generate_key()              # store this securely; rotating it invalidates the cache
cache = Cache(database_url=..., encrypt_key=key)

cache.set("secret", "s3kr3t")
# the stored `value` bytes are ciphertext; cache.get("secret") returns "s3kr3t"
```

The serialized value is encrypted before it's written and decrypted on read, so the plaintext never
touches the database. Encryption adds ~170 bytes of overhead per entry, which is accounted for in
`byte_size` (and therefore in `max_size` eviction).
