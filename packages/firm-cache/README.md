# firm-cache

Database-backed caching for Python — a pure-Python port of Rails'
[Solid Cache](https://github.com/rails/solid_cache). No Redis required: cache entries live in
your existing **SQLite**, **PostgreSQL**, or **MySQL/MariaDB** database.

Part of [firm](https://github.com/h11t-labs/firm), a port of the Rails 8 Solid stack.

```bash
pip install firm-cache
pip install "firm-cache[encryption]"  # with at-rest encryption
```

## Quickstart

```python
from firm.cache import Cache

cache = Cache(database_url="postgresql://localhost/myapp")

cache.set("greeting", "hello")
cache.get("greeting")  # "hello"
cache.fetch("expensive", lambda: compute())  # compute once, then cached
```

## Highlights

- **FIFO eviction** by age, total size, or entry count — tuned in the background, like Solid Cache
- **Pluggable coders** — JSON (default), opt-in pickle, or msgpack via `firm-cache[msgpack]`
- **At-rest encryption** (Fernet) via `firm-cache[encryption]`
- `fetch`, `get_multi`/`set_multi`, `increment`/`decrement`

## Docs

- [Cache overview](https://github.com/h11t-labs/firm/blob/main/docs/cache/overview.md)
- [All firm documentation](https://github.com/h11t-labs/firm#readme)

MIT licensed. Schema and design derived from Solid Cache (© 37signals, MIT); see
[NOTICE](https://github.com/h11t-labs/firm/blob/main/NOTICE).
