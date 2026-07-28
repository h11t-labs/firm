# Framework integration

`firm.queue.contrib` has **optional** glue for embedding firm-queue in a web app. Each piece
is opt-in, behind its own extra, and **nothing in core imports it** — if you don't use it, it costs
nothing. You still define jobs the normal way with `@bq.job`.

!!! note "Renamed from `firm.contrib` / `Firm`"

    These live under `firm.queue.contrib` as of the next release, so that each module's
    integrations sit under that module's own path. `firm.contrib` read like a firm-wide namespace
    while everything in it only ever configured the queue — and it squats the name a genuinely
    suite-wide integration would want. As integrations land for the other modules they grow their
    own `contrib` too, so the queue must not be the odd one out.

    The Flask extension is renamed `Firm` → `FirmQueue` for the same reason, along with
    `app.extensions["firm"]` → `app.extensions["firm_queue"]` and `flask firm` →
    `flask firm-queue`.

    Everything above shipped in 1.0.0, so every old spelling keeps working, and all of them are
    removed in 2.0. The old import paths re-export the same objects, so `is` and `isinstance`
    still hold across both.

    The old import paths, the `Firm` name and `flask firm` raise a `DeprecationWarning` you can
    grep a migration for. **`app.extensions["firm"]` cannot** — it is an ordinary dict key that
    both spellings point at, and reading it runs no code of ours. Search for it by hand.

| Import | Install | What it does |
|---|---|---|
| `firm.queue.contrib.fastapi.lifespan` | `firm-queue[fastapi]` | a FastAPI lifespan that configures the queue (and optionally runs workers) |
| `firm.queue.contrib.flask.FirmQueue` | `firm-queue[flask]` | a Flask extension + a `flask firm-queue worker` command |
| `firm.queue.contrib.sqlalchemy.enqueue_after_commit` | — (SQLAlchemy is core) | enqueue only when a session commits |

There is no `firm.cache.contrib.flask` or `firm.channel.contrib.fastapi`, and that is deliberate.
The queue needs framework glue because it has process-global state and background threads that
must be tied to the app lifecycle; `Cache`, `Channel` and `audit.record` are plain objects you
construct yourself, so there is nothing to hook. Django is the exception — it has pluggable
backend contracts to fill — which is why the integrations for the other modules start there.

## FastAPI

```python
from fastapi import FastAPI
from firm.queue.contrib.fastapi import lifespan

app = FastAPI(lifespan=lifespan(database_url="postgresql://localhost/app"))

@app.post("/welcome/{user_id}")
def welcome(user_id: int):
    send_welcome.enqueue(user_id)      # a normal @bq.job
    return {"queued": True}
```

The lifespan calls `configure(...)` on startup so your handlers can enqueue. Pass
`embed_workers=True` (with `queues=`, `threads=`) to also run a worker + dispatcher **inside the app
process** — convenient for development or a single-process deploy; it's stopped on shutdown.

## Flask

```python
from flask import Flask
from firm.queue.contrib.flask import FirmQueue

app = Flask(__name__)
app.config["FIRM_QUEUE_DATABASE_URL"] = "postgresql://localhost/app"
FirmQueue(app)                    # or FirmQueue(app, database_url="...")

@app.post("/welcome/<int:user_id>")
def welcome(user_id):
    send_welcome.enqueue(user_id)
    return "", 202
```

The URL is looked up in `database_url=`, then `app.config`, then the environment; within each of
those the queue's own `FIRM_QUEUE_DATABASE_URL` wins over the suite-wide `FIRM_DATABASE_URL`, so
one shared setting still configures every module while a single module can override it.

The extension configures the queue and registers a CLI group, so you run workers with:

```bash
flask firm-queue worker --threads 5 --queues default,mailers
```

`FirmQueue(app, embed_workers=True)` runs the worker inside the web process instead (dev /
single-process only — otherwise every web worker starts its own supervisor).

## Transactional enqueue

`enqueue_after_commit` defers an enqueue until your SQLAlchemy session commits, and drops it on
rollback — so you never enqueue a job for a request that didn't persist:

```python
from firm.queue.contrib.sqlalchemy import enqueue_after_commit

def create_order(session, payload):
    order = Order(**payload)
    session.add(order)
    enqueue_after_commit(session, charge_card, order.id)   # fires iff the commit succeeds
    session.commit()
```

> The job is enqueued in firm's own transaction just **after** your commit — so it's
> "enqueue iff the request committed," not one atomic transaction. A crash in the narrow window
> between the two commits could still drop the enqueue; for most apps that's the right trade.

## Production shape

Embed workers for dev; in production run them as **separate processes** (`firm-queue start`
or `flask firm-queue worker`) so you scale web and worker capacity independently. See
[Workers & the supervisor](queue/workers-and-supervisor.md).
