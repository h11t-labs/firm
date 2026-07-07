# firm

```
███████╗██╗██████╗ ███╗   ███╗
██╔════╝██║██╔══██╗████╗ ████║
█████╗  ██║██████╔╝██╔████╔██║
██╔══╝  ██║██╔══██╗██║╚██╔╝██║
██║     ██║██║  ██║██║ ╚═╝ ██║
╚═╝     ╚═╝╚═╝  ╚═╝╚═╝     ╚═╝
```

Pure-Python ports of the Rails 8 **Solid** stack — keep background jobs and caching in your existing
SQL database, no Redis required.

> Inspired by the Rails Solid stack from 37signals
> ([solid_queue](https://github.com/rails/solid_queue), [solid_cache](https://github.com/rails/solid_cache),
> [solid_cable](https://github.com/rails/solid_cable)).

**One namespace, four independent modules** — install only the one you need:

| Module | Install | Ports | Highlights |
|---|---|---|---|
| **[queue](docs/queue/overview.md)** | `pip install firm-queue` | `solid_queue` — background jobs | concurrency controls, recurring tasks, retries, forked/threaded supervisor, crash recovery |
| **[cache](docs/cache/overview.md)** | `pip install firm-cache` | `solid_cache` — cache store | FIFO age/size/count eviction, pluggable coders, at-rest encryption |
| **[channel](docs/channel/overview.md)** | `pip install firm-channel` | `solid_cable` — pub/sub | broadcast/subscribe over your database, polling listener, automatic message trimming |
| **[audit](docs/audit/overview.md)** | `pip install firm-audit` | *(none — original to firm)* | append-only audit log, opt-in retention, `history()` querying |

Add database drivers and features as extras — `firm-queue[postgres]`,
`firm-cache[encryption]`, … (see **[Installation](docs/installation.md)**). All four modules run
on **SQLite**, **PostgreSQL**, and **MySQL/MariaDB** — verified live against all three. The top-level
package imports nothing heavy, so a queue-only process never loads the cache, pub/sub, or audit code.

## Quickstart

```python
import firm.queue as bq
bq.configure(database_url="postgresql://localhost/myapp")

@bq.job()
def greet(name): print(f"hi {name}")

greet.enqueue("Ada")        # then: firm-queue start --import myapp.jobs
```

```python
from firm.cache import Cache
cache = Cache(database_url="postgresql://localhost/myapp")
cache.fetch("k", lambda: expensive())
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

## Documentation

The docs live in [`docs/`](docs/index.md) and are built with
[Zensical](https://zensical.org) (the Material for MkDocs team's successor to MkDocs);
configuration is in [`zensical.toml`](zensical.toml).

```bash
uv run zensical serve     # live preview at http://localhost:8000
uv run zensical build     # render the static site to ./site
```

Key pages: **[Cookbook](docs/cookbook.md)** (lots of examples + combinations) ·
**[API cheatsheet](docs/api.md)** · [queue overview](docs/queue/overview.md) ·
[cache overview](docs/cache/overview.md) · [channel overview](docs/channel/overview.md) ·
[audit overview](docs/audit/overview.md) ·
[framework integration](docs/contrib.md) · [dashboard](docs/ui.md) ·
[database backends](docs/database-backends.md) · [comparison to Rails](docs/comparison-to-rails.md).

Runnable [`examples/`](examples/) cover each module and combinations. For LLM/agent consumption see
[`llms.txt`](llms.txt) (curated index) and [`llms-full.txt`](llms-full.txt) (all docs in one file).

## Development

```bash
uv sync
uv run pre-commit install     # ruff + ty + llms-full regeneration run on every commit
uv run pytest                 # tests (SQLite; set FIRM_TEST_PG_URL / _MYSQL_URL for live PG/MySQL)
uv run ruff check
uv run ty check packages
uv run pre-commit run --all-files   # run every hook manually
```

See [docs/testing-and-contributing.md](docs/testing-and-contributing.md).
