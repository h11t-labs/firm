# Installation

firm is four independent modules — `firm.queue` (jobs), `firm.cache` (caching), `firm.channel`
(publish/subscribe), and `firm.audit` (an append-only audit log) — plus a shared internal core.
Each module is its own package on PyPI, all sharing the `firm.*` import namespace. **You install
only what you use**, so a queue-only service never pulls cache, pub/sub, or audit dependencies,
and vice versa.

```bash
pip install firm-queue      # just the job queue
pip install firm-cache      # just the cache
pip install firm-channel    # just pub/sub
pip install firm-audit      # just the audit log
pip install firm-ui         # the web dashboard (pulls all four modules)
```

> A `firm` meta-package (`pip install "firm[queue]"` and friends) is planned; the PyPI name is
> pending a [name transfer](https://peps.python.org/pep-0541/). The per-package installs above
> are the canonical form and will keep working either way.

## Extras

Database drivers and optional features are extras on the module packages:

| Install | Pulls in | Use it for |
|---|---|---|
| `firm-queue[postgres]` / `firm-cache[postgres]` / … | `psycopg` | running on PostgreSQL |
| `firm-queue[mysql]` / `firm-cache[mysql]` / … | `pymysql` | running on MySQL / MariaDB |
| `firm-queue[flask]` | `flask` | the Flask integration ([`firm.contrib.flask`](contrib.md)) |
| `firm-queue[fastapi]` | `fastapi` | the FastAPI integration ([`firm.contrib.fastapi`](contrib.md)) |
| `firm-cache[encryption]` | `cryptography` | cache at-rest encryption (Fernet) |
| `firm-cache[msgpack]` | `msgpack` | the cache msgpack value coder |

The driver extras (`postgres`, `mysql`) exist on every module (they resolve to
`firm-core[postgres]` / `firm-core[mysql]` under the hood), so add them to whichever module you
install. Combine freely — extras are additive:

```bash
pip install "firm-queue[postgres]"            # queue on PostgreSQL
pip install "firm-cache[encryption]"          # cache with at-rest encryption
pip install "firm-queue[mysql]" firm-cache    # both modules on MySQL/MariaDB
pip install firm-ui "firm-queue[flask,fastapi]" "firm-core[postgres,mysql]" "firm-cache[encryption,msgpack]"   # everything
```

With `uv`:

```bash
uv add "firm-queue[postgres]"
```

## What each install gives you

- **`firm-queue`** → `import firm.queue as bq`, the `@bq.job` decorator, enqueuing, the
  worker/dispatcher/scheduler/supervisor, and the `firm-queue` CLI. No cache dependencies.
- **`firm-cache`** → `from firm.cache import Cache`, all cache operations, and the
  `firm-cache` CLI. No queue dependencies (no `croniter`).
- **`firm-channel`** → `from firm.channel import Channel`, broadcast/subscribe, the polling
  listener, message trimming, and the `firm-channel` CLI. No queue dependencies (no `croniter`).
- **`firm-audit`** → `from firm.audit import AuditLog, record`, append-only event recording,
  `history()` querying, opt-in retention, and the `firm-audit` CLI. No queue dependencies (no
  `croniter`).
- **`firm-ui`** → the [web dashboard](ui.md) and its `firm-ui` command; depends on all four
  modules.
- **SQLite** needs no driver extra. **PostgreSQL/MySQL** need `postgres`/`mysql`. Bare
  `postgresql://` / `mysql://` URLs are auto-normalized to the shipped drivers — see
  [Database backends](database-backends.md).

> **Note:** each module is its own wheel sharing the `firm.*` namespace, so an uninstalled
> module truly isn't there — a queue-only process can never import (or pay for) the cache,
> pub/sub, or audit code.

## Missing an extra?

If you reach for a feature whose extra isn't installed, you get a clear, actionable error that
names the extra to add — not a bare `ModuleNotFoundError`:

```text
>>> Cache(..., encrypt_key=key)                # without [encryption]
ImportError: At-rest cache encryption requires "cryptography". Install the encryption extra: pip install "firm-cache[encryption]"

>>> bq.configure(database_url="postgresql://…") # without [postgres]
ImportError: The postgres driver "psycopg" is not installed. Install the postgres extra: pip install "firm-core[postgres]"
```

## Next steps

- [Queue: getting started](queue/getting-started.md)
- [Cache: getting started](cache/getting-started.md)
- [Channel: getting started](channel/getting-started.md)
- [Audit: getting started](audit/getting-started.md)
