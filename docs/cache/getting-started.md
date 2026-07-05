# Getting started

## Install

```bash
pip install "firm[cache]"              # or: uv add "firm[cache]"
pip install "firm[cache,postgres]"     # psycopg, for PostgreSQL
pip install "firm[cache,mysql]"        # PyMySQL, for MySQL/MariaDB
pip install "firm[cache,encryption]"   # cryptography, for at-rest encryption
```

See **[Installation](../installation.md)** for the full list of extras.

## Create a cache

```python
from firm.cache import Cache

cache = Cache(database_url="sqlite:///cache.db")
```

By default `Cache(...)` creates the `firm_entries` table if it's missing
(`create_schema=True`). For production schema management, use the bundled Alembic migration and pass
`create_schema=False` — see [Database backends](../database-backends.md#migrations).

## Use it

```python
cache.set("user:42", {"name": "Ada", "admin": True})
cache.get("user:42")                 # -> {"name": "Ada", "admin": True}
cache.get("missing")                 # -> None

# read-through: compute and store on a miss, return the cached value on a hit
value = cache.fetch("report:2026", lambda: build_expensive_report())

cache.delete("user:42")              # -> True
cache.exist("user:42")               # -> False
```

## Clean up

A `Cache` owns a connection pool (and, optionally, a background expiry thread). Close it when you're
done, or use it as a context manager:

```python
with Cache(database_url="sqlite:///cache.db") as cache:
    cache.set("k", "v")
# closed automatically
```

## A complete example

```python
from firm.cache import Cache

with Cache(database_url="sqlite:///cache.db") as cache:
    cache.set("greeting", "hello")
    print(cache.get("greeting"))                       # hello
    print(cache.fetch("pi", lambda: 3.14159))          # computes -> 3.14159
    print(cache.fetch("pi", lambda: 0))                # cached  -> 3.14159
    cache.increment("hits")
    cache.increment("hits", 4)
    print(cache.get("hits"))                           # 5
```

Next: **[Operations](operations.md)** and **[Eviction & expiry](eviction.md)**.
