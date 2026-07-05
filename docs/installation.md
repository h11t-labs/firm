# Installation

`firm` is a single package with four independent modules — `firm.queue` (jobs),
`firm.cache` (caching), `firm.channel` (publish/subscribe), and `firm.audit` (an append-only
audit log) — plus a shared internal core. **You install only what you use** via extras, so a
queue-only service never pulls cache, pub/sub, or audit dependencies, and vice versa.

```bash
pip install "firm[queue]"     # just the job queue
pip install "firm[cache]"     # just the cache
pip install "firm[channel]"    # just pub/sub
pip install "firm[audit]"      # just the audit log
```

> `firm` is an extras-only meta-package: the bare `pip install firm` (no extras) installs **no
> modules at all** — always name at least one. Each module is also its own package
> (`pip install firm-queue` ≡ `pip install "firm[queue]"`) if you prefer direct dependencies.

## Extras

| Extra | Pulls in | Use it for |
|---|---|---|
| `queue` | `firm-queue` (croniter, click) | the background-job queue (scheduler + the `firm-queue` CLI) |
| `cache` | `firm-cache` (click) | the cache store + the `firm-cache` CLI |
| `channel` | `firm-channel` (click) | pub/sub + the `firm-channel` CLI |
| `audit` | `firm-audit` (click) | the audit log + the `firm-audit` CLI |
| `flask` | `flask` | the Flask integration ([`firm.contrib.flask`](contrib.md)) |
| `fastapi` | `fastapi` | the FastAPI integration ([`firm.contrib.fastapi`](contrib.md)) |
| `postgres` | `psycopg` | running on PostgreSQL |
| `mysql` | `pymysql` | running on MySQL / MariaDB |
| `encryption` | `cryptography` | cache at-rest encryption (Fernet) |
| `msgpack` | `msgpack` | the cache msgpack value coder |
| `all` | everything above | the full kitchen sink |

Combine freely — extras are additive:

```bash
pip install "firm[queue,postgres]"          # queue on PostgreSQL
pip install "firm[cache,encryption]"        # cache with at-rest encryption
pip install "firm[queue,cache,mysql]"       # both modules on MySQL/MariaDB
pip install "firm[all]"                     # everything
```

With `uv`:

```bash
uv add "firm[queue,postgres]"
```

## What each install gives you

- **`firm[queue]`** → `import firm.queue as bq`, the `@bq.job` decorator, enqueuing, the
  worker/dispatcher/scheduler/supervisor, and the `firm-queue` CLI. No cache dependencies.
- **`firm[cache]`** → `from firm.cache import Cache`, all cache operations, and the
  `firm-cache` CLI. No queue dependencies (no `croniter`).
- **`firm[channel]`** → `from firm.channel import Channel`, broadcast/subscribe, the polling
  listener, message trimming, and the `firm-channel` CLI. No queue dependencies (no `croniter`).
- **`firm[audit]`** → `from firm.audit import AuditLog, record`, append-only event recording,
  `history()` querying, opt-in retention, and the `firm-audit` CLI. No queue dependencies (no
  `croniter`).
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
ImportError: At-rest cache encryption requires "cryptography". Install the encryption extra: pip install "firm[cache,encryption]"

>>> bq.configure(database_url="postgresql://…") # without [postgres]
ImportError: The postgres driver "psycopg" is not installed. Install the postgres extra: pip install "firm[postgres]"
```

## Next steps

- [Queue: getting started](queue/getting-started.md)
- [Cache: getting started](cache/getting-started.md)
- [Channel: getting started](channel/getting-started.md)
- [Audit: getting started](audit/getting-started.md)
