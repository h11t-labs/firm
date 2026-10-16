# firm-queue

Database-backed background jobs for Python — a pure-Python port of Rails'
[Solid Queue](https://github.com/rails/solid_queue). No Redis required: jobs live in your
existing **SQLite**, **PostgreSQL**, or **MySQL/MariaDB** database.

Part of [firm](https://github.com/h11t-labs/firm), a port of the Rails 8 Solid stack.

```bash
pip install firm-queue
pip install "firm-queue[postgres]"  # with the PostgreSQL driver
```

## Quickstart

```python
import firm.queue as bq

bq.configure(database_url="postgresql://localhost/myapp")


@bq.job()
def greet(name):
    print(f"hi {name}")


greet.enqueue("Ada")
```

Then run the workers:

```bash
firm-queue start --import myapp.jobs
```

(Create the schema first with `schema.create_all()` or the bundled Alembic migrations — see
[Getting started](https://github.com/h11t-labs/firm/blob/main/docs/queue/getting-started.md).)

## Highlights

- **Concurrency controls** — limit how many jobs with the same key run at once
- **Recurring tasks** — cron-style schedules, enqueued exactly once per tick
- **Retries & failure handling** — configurable retry/discard policies with backoff
- **Forked or threaded supervisor** — with heartbeats and crash recovery
- Flask and FastAPI integrations via `firm-queue[flask]` / `firm-queue[fastapi]`

## Docs

- [Queue overview](https://github.com/h11t-labs/firm/blob/main/docs/queue/overview.md)
- [Defining jobs](https://github.com/h11t-labs/firm/blob/main/docs/queue/jobs.md)
- [Workers & the supervisor](https://github.com/h11t-labs/firm/blob/main/docs/queue/workers-and-supervisor.md)
- [All firm documentation](https://github.com/h11t-labs/firm#readme)

MIT licensed. Schema and design derived from Solid Queue (© 37signals, MIT); see
[NOTICE](https://github.com/h11t-labs/firm/blob/main/NOTICE).
