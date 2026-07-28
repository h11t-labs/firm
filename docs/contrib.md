# Framework integration

`firm.queue.contrib` has **optional** glue for embedding firm-queue in a web app. Each piece
is opt-in, behind its own extra, and **nothing in core imports it** — if you don't use it, it costs
nothing. You still define jobs the normal way with `@bq.job`.

!!! note "Moved from `firm.contrib`"

    These live under `firm.queue.contrib` as of the next release, so that each module's
    integrations sit under that module's own path — alongside `firm.cache.contrib.django`,
    `firm.channel.contrib.django` and `firm.audit.contrib.django`. `firm.contrib` read like a
    firm-wide namespace while everything in it only ever configured the queue.

    The old `firm.contrib.flask`, `firm.contrib.fastapi` and `firm.contrib.sqlalchemy` keep
    working and emit a `DeprecationWarning`; they are removed in 2.0. They re-export the same
    objects, so `is` and `isinstance` still hold across both spellings.

| Import | Install | What it does |
|---|---|---|
| `firm.queue.contrib.fastapi.lifespan` | `firm-queue[fastapi]` | a FastAPI lifespan that configures the queue (and optionally runs workers) |
| `firm.queue.contrib.flask.Firm` | `firm-queue[flask]` | a Flask extension + a `flask firm worker` command |
| `firm.queue.contrib.django` | `firm-queue[django]` | a Django app config, `manage.py firm_worker`, `enqueue_on_commit`, and a `django.tasks` backend |
| `firm.queue.contrib.sqlalchemy.enqueue_after_commit` | — (SQLAlchemy is core) | enqueue only when a session commits |

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
from firm.queue.contrib.flask import Firm

app = Flask(__name__)
app.config["FIRM_DATABASE_URL"] = "postgresql://localhost/app"
Firm(app)                         # or Firm(app, database_url="...")

@app.post("/welcome/<int:user_id>")
def welcome(user_id):
    send_welcome.enqueue(user_id)
    return "", 202
```

The extension configures the queue and registers a CLI group, so you run workers with:

```bash
flask firm worker --threads 5 --queues default,mailers
```

`Firm(app, embed_workers=True)` runs the worker inside the web process instead (dev /
single-process only — otherwise every web worker starts its own supervisor).

## Django

```python
# settings.py
INSTALLED_APPS = ["myapp", "firm.queue.contrib.django"]
```

That one line configures firm from `DATABASES` in every process, creates its tables from
`manage.py migrate`, imports `<app>/jobs.py` so workers can resolve your jobs, and adds
`manage.py firm_worker`. `firm.queue.contrib.django.enqueue_on_commit` is the transactional enqueue
below in Django's terms; `firm.queue.contrib.django.backend.FirmBackend` puts Django 6's `@task` on the
same queue, and `firm.cache.contrib.django.FirmCache` puts `django.core.cache` in the same
database.

Django has a page of its own, because it has questions of its own — Alembic next to Django
migrations, two connection pools, `TransactionTestCase`: **[Django](django.md)**.

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
or `flask firm worker`) so you scale web and worker capacity independently. See
[Workers & the supervisor](queue/workers-and-supervisor.md).
