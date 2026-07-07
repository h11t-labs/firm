# Framework integration

`firm.contrib` has **optional** glue for embedding firm-queue in a web app. Each piece
is opt-in, behind its own extra, and **nothing in core imports it** — if you don't use it, it costs
nothing. You still define jobs the normal way with `@bq.job`.

| Import | Install | What it does |
|---|---|---|
| `firm.contrib.fastapi.lifespan` | `firm-queue[fastapi]` | a FastAPI lifespan that configures the queue (and optionally runs workers) |
| `firm.contrib.flask.Firm` | `firm-queue[flask]` | a Flask extension + a `flask firm worker` command |
| `firm.contrib.sqlalchemy.enqueue_after_commit` | — (SQLAlchemy is core) | enqueue only when a session commits |

## FastAPI

```python
from fastapi import FastAPI
from firm.contrib.fastapi import lifespan

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
from firm.contrib.flask import Firm

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

## Transactional enqueue

`enqueue_after_commit` defers an enqueue until your SQLAlchemy session commits, and drops it on
rollback — so you never enqueue a job for a request that didn't persist:

```python
from firm.contrib.sqlalchemy import enqueue_after_commit

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
