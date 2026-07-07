# 🪨 firm

**Pure-Python ports of the Rails 8 "Solid" stack.** Keep background jobs and caching in the SQL
database you already run — no Redis, no extra broker, no new infrastructure.

| Package | Ports | Replaces |
|---|---|---|
| **[firm-queue](queue/overview.md)** | [`solid_queue`](https://github.com/rails/solid_queue) | Sidekiq / Celery / RQ |
| **[firm-cache](cache/overview.md)** | [`solid_cache`](https://github.com/rails/solid_cache) | Redis / Memcached cache |
| **[firm-channel](channel/overview.md)** | [`solid_cable`](https://github.com/rails/solid_cable) | Redis Pub/Sub (Action Cable) |
| **[firm-audit](audit/overview.md)** | *(none — original to firm)* | Hand-rolled audit logging |

> Inspired by the Rails Solid stack from 37signals.

The four packages are **independent** — install only what you need — but share a design: your
database is the single source of truth, accessed through SQLAlchemy with per-dialect locking.

## Why database-backed?

- **One fewer moving part.** If you already run PostgreSQL, MySQL, or SQLite, you already have
  everything you need. Nothing else to deploy, monitor, or secure.
- **Transactional.** Enqueue a job in the same transaction as the row it depends on.
- **Inspectable.** Jobs and cache entries are just rows — query them, back them up, debug them.

## Quick taste

```python
import firm.queue as bq

bq.configure(database_url="postgresql://localhost/myapp")

@bq.job(queue="mailers")
def send_welcome(user_id: int) -> None:
    ...

send_welcome.enqueue(42)            # enqueue from your app
# $ firm-queue start         # run workers + dispatcher
```

```python
from firm.cache import Cache

cache = Cache(database_url="postgresql://localhost/myapp")
cache.fetch("homepage", lambda: render_homepage())
```

```python
from firm.channel import Channel

ps = Channel(database_url="postgresql://localhost/myapp")
ps.subscribe("room:42", lambda payload: print(payload))
ps.broadcast("room:42", b'{"msg": "hi"}')
```

```python
from firm.audit import AuditLog

audit = AuditLog(database_url="postgresql://localhost/myapp")
audit.record("invoice.paid", subject=invoice, actor=user, data={"amount": 4200})
```

## Databases

SQLite (default), **PostgreSQL**, and **MySQL/MariaDB** are all supported and tested live. See
[Database backends](database-backends.md) for drivers, locking semantics, and when to use which.

## Where to next

- New to it? Start with **[queue: getting started](queue/getting-started.md)**,
  **[cache: getting started](cache/getting-started.md)**,
  **[channel: getting started](channel/getting-started.md)**, or
  **[audit: getting started](audit/getting-started.md)**.
- Coming from Rails? See **[Comparison to Rails](comparison-to-rails.md)**.
- Contributing? See **[Testing & contributing](testing-and-contributing.md)**.
