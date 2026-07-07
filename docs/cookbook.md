# Cookbook

The firm library bundles four database-backed primitives — a job **queue**, a key/value **cache**, a publish/subscribe **channel**, and an append-only **audit** log — that share one storage engine and compose freely. This cookbook walks each primitive on its own, then shows recipes that combine them, web-framework integrations for FastAPI and Flask, and the day-two operations you need to run firm in production. Every example uses the real `firm.*` API and is copy-pasteable; swap the `sqlite://` URLs for `postgresql://…` in production.

- [Queue: define, enqueue, schedule, run](#queue)
- [Cache: store, fetch, counters, coders, encryption, eviction](#cache)
- [Channel: broadcast and subscribe](#channel)
- [Audit: record events, query history, retention](#audit)
- [Queue + Cache: cache-warming jobs & read-through](#queue--cache)
- [Queue + Channel: progress updates & notifications](#queue--channel)
- [Cache + Channel: cross-process cache invalidation](#cache--channel)
- [Queue + Audit: log job lifecycle events](#queue--audit)
- [All three together: an end-to-end pipeline](#all-three-together)
- [FastAPI app (full)](#fastapi)
- [Flask app (full)](#flask)
- [Operations & production](#operations--production)

---

## Queue: define, enqueue, schedule, run {#queue}

Configure once at process startup, decorate plain functions with `@bq.job`, then enqueue them now or later. A worker (CLI process, in-process supervisor, or a single `run_ready` drain) claims ready jobs and runs them.

```python
# jobs.py — define jobs
import firm.queue as bq

# Configure the process-global runtime once, before enqueuing or working.
# Returns a Runtime; keyword args shown are the defaults.
bq.configure(
    database_url="postgresql://localhost/app",  # bare pg:// / mysql:// auto-normalize
    busy_timeout_ms=5000,                        # SQLite busy timeout
    default_queue="default",
    preserve_finished_jobs=True,                 # keep finished rows for the UI
)

# @bq.job turns a function into a Job. It stays directly callable (great for tests),
# while gaining .enqueue / .enqueue_at / .enqueue_in.
@bq.job(queue="emails", priority=10, attempts=3, backoff=2.0)
def send_welcome(user_id: int, *, locale: str = "en") -> None:
    ...  # do the work

# priority: LOWER numbers are claimed first (0 = default). attempts: total attempts (1 = no retry).
# backoff: base seconds for exponential backoff between attempts.

send_welcome(42, locale="nl")  # runs inline — no queue involved
```

```python
# Enqueue — args/kwargs are forwarded to the job and serialized.
from datetime import datetime, timedelta, timezone

send_welcome.enqueue(42, locale="nl")                  # ready now -> job_id (int)
send_welcome.enqueue_in(timedelta(minutes=5), 42)      # run after a delay
send_welcome.enqueue_at(                                # run at an absolute time (UTC)
    datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc), 42
)
# Each returns the new job_id (int), or None if discarded by a concurrency conflict.
```

Run workers as a separate process via the CLI (production default forks one process per component):

```bash
# Start the full stack (workers + dispatcher). --import loads modules so @job defs register.
firm-queue start --database-url postgresql://localhost/app --import myapp.jobs
firm-queue start --import myapp.jobs --queues emails,default --threads 5
firm-queue start --import myapp.jobs --mode thread   # threads instead of forked processes

# Or run just one worker, or drain ready jobs once and exit:
firm-queue work  --import myapp.jobs --queues emails --threads 5
firm-queue drain --import myapp.jobs --limit 100
# (--database-url is optional if FIRM_QUEUE_DATABASE_URL is set)
```

Or embed workers in your own process with `ThreadSupervisor` (non-blocking `.start()`, `.stop()` drains; also a context manager):

```python
import firm.queue as bq
from firm.queue import current_runtime
from firm.queue.supervisor import (
    ThreadSupervisor, SupervisorConfig, WorkerConfig, DispatcherConfig,
)
from firm.queue.scheduler import RecurringTask
import myapp.jobs  # import so @job definitions register
from myapp.jobs import nightly_report

bq.configure(database_url="postgresql://localhost/app")
runtime = current_runtime()

config = SupervisorConfig(
    workers=[WorkerConfig(queues=("*",), threads=3)],  # ("*",) = all queues
    dispatchers=[DispatcherConfig()],                  # promotes scheduled -> ready
    recurring=[                                         # adds a scheduler if non-empty
        RecurringTask(key="nightly", schedule="0 3 * * *", job=nightly_report),
    ],
)

# Context-manager form: starts on enter, drains on exit.
with ThreadSupervisor(runtime, config):
    ...  # serve requests, run your app

# Or manage the lifecycle explicitly:
supervisor = ThreadSupervisor(runtime, config)
supervisor.start()  # non-blocking
# ...
supervisor.stop()   # drains in-flight jobs
```

Drain once, in-process, without any background threads — handy in tests or a cron-driven one-shot:

```python
from firm.queue.worker import run_ready
from firm.queue.dispatcher import dispatch_once, run_maintenance
from firm.queue import current_runtime

rt = current_runtime()
dispatch_once(rt)                              # promote due scheduled jobs -> ready
run_ready(rt, queues=("*",), limit=100)        # claim + run ready jobs inline; returns count
run_maintenance(rt)                            # expire stale semaphores, unblock jobs
```

---

## Cache: store, fetch, counters, coders, encryption, eviction {#cache}

`firm.cache.Cache` is a database-backed key/value store: arbitrary Python values (pickled by default), atomic counters, optional at-rest encryption, and FIFO eviction by age/size/count. Construct it with a `database_url` (or a shared `engine`) and close it when done — it's a context manager.

```python
from firm.cache import Cache

# create the cache; create_schema=True auto-creates the firm_entries table
cache = Cache(database_url="sqlite:///cache.db")

# store / fetch / probe
cache.set("user:1", {"name": "Ada", "admin": True})  # value can be any picklable object
cache.get("user:1")                                   # -> {"name": "Ada", "admin": True}; None if missing
cache.exist("user:1")                                 # -> True
cache.delete("user:1")                                # -> True if a row was removed, else False

# fetch: return cached value, else compute + store it (read-through)
cache.fetch("report", lambda: build_report())         # callable is only invoked on a miss
cache.fetch("flag", "default")                         # a non-callable is used verbatim

cache.clear()                                          # drop every entry
cache.close()                                          # stop background loop + dispose owned engine
```

Keys are `str` or `bytes`; values are arbitrary objects. Prefer the context-manager form so the engine and any background expiry thread are always cleaned up:

```python
with Cache(database_url="sqlite:///cache.db") as cache:
    cache.set("k", "v")
    # cache.close() runs automatically on exit
```

### Batch get / set

```python
cache.set_multi({"a": 1, "b": 2, "c": 3})       # one transaction for all writes
cache.get_multi(["a", "b", "x"])                # -> {"a": 1, "b": 2, "x": None}; misses map to None
```

### Atomic counters

`increment`/`decrement` do a serialized read-modify-write (BEGIN IMMEDIATE on SQLite, `SELECT ... FOR UPDATE` on Postgres/MySQL), so they're safe under concurrency. A missing key starts at 0.

```python
cache.increment("hits")            # -> 1 (created at 0, then +1)
cache.increment("hits", by=10)     # -> 11
cache.decrement("hits")            # -> 10
cache.decrement("hits", by=4)      # -> 6
```

### Coders: JSON vs default pickle

The default `PickleCoder` handles any Python object. Use `JSONCoder` for interop / human-readable bytes (values must be JSON-serializable).

```python
from firm.cache import Cache, JSONCoder, PickleCoder

cache = Cache(database_url="sqlite:///cache.db", coder=JSONCoder())
cache.set("config", {"theme": "dark", "limit": 50})   # stored as UTF-8 JSON
cache.get("config")                                    # -> {"theme": "dark", "limit": 50}

# PickleCoder() is the default; passing it explicitly is equivalent to coder=None
cache = Cache(database_url="sqlite:///cache.db", coder=PickleCoder())
```

### Encryption at rest (Fernet)

Pass `encrypt_key` to encrypt the serialized bytes with Fernet. This needs the `encryption` extra (`cryptography`). The key is a standard Fernet key (str or bytes).

```bash
pip install "firm-cache[encryption]"
```

```python
from cryptography.fernet import Fernet
from firm.cache import Cache

key = Fernet.generate_key()   # persist this somewhere safe; you need it to read the data back
cache = Cache(database_url="sqlite:///cache.db", encrypt_key=key)
cache.set("secret", "token-123")   # stored encrypted; get() transparently decrypts
cache.get("secret")                # -> "token-123"
```

Encryption wraps whichever coder you choose, so it composes with `JSONCoder`:

```python
cache = Cache(database_url="sqlite:///cache.db", coder=JSONCoder(), encrypt_key=key)
```

### Eviction: max_age / max_size / max_entries + background expiry

Eviction is FIFO (oldest entries by insertion order go first). Defaults: `max_age=14 days`, `max_size=256 MiB`, `max_entries=None`. By default expiry runs probabilistically on writes (`auto_expire=True`); set `background_expiry=True` to also run it on a timer.

```python
from firm.cache import Cache

cache = Cache(
    database_url="sqlite:///cache.db",
    max_age=3600,             # evict entries older than 1 hour (seconds; None disables age expiry)
    max_size=64 * 1024**2,    # evict oldest once estimated size exceeds 64 MiB (None disables)
    max_entries=10_000,       # evict oldest once row count exceeds this (None = unbounded)
    expiry_batch_size=100,    # rows considered per eviction pass
    auto_expire=True,         # run eviction probabilistically on writes (default)
    background_expiry=True,   # also run eviction on a background timer
    expiry_interval=60.0,     # seconds between background passes
)
# background_expiry starts a thread; close() stops it cleanly
cache.close()
```

---

## Channel: broadcast and subscribe {#channel}

`Channel` is database-backed publish/subscribe: `broadcast` inserts a message row, `subscribe` registers a callback and spins up a background polling listener that delivers every *future* message on that channel. Delivery is per-process — every process running a `Channel` sees every broadcast.

```python
from firm.channel import Channel

# Either a database_url (Channel owns the engine) or a shared engine= is required.
ch = Channel(database_url="sqlite:///channel.db")
# create_schema=True (default) auto-creates the firm_messages table.
```

```python
# Callback receives the payload as bytes. A subscriber only sees messages
# broadcast AFTER it subscribes — no backlog replay. The first subscribe()
# starts the background listener thread.
def on_message(payload: bytes) -> None:
    print("got", payload)

ch.subscribe("room:42", on_message)

ch.broadcast("room:42", b"hello")   # bytes pass through unchanged
ch.broadcast("room:42", "héllo")    # str is UTF-8 encoded -> b"h\xc3\xa9llo"

ch.unsubscribe("room:42", on_message)  # remove this callback; listener keeps running
```

Payloads are opaque bytes. Serialize structured data yourself:

```python
import json
ch.broadcast("room:42", json.dumps({"user": "ada", "text": "hi"}).encode())
```

Tuning and cleanup. `polling_interval` (default `0.1`s) sets how often the listener checks for new rows; `trim()` deletes one batch of messages older than `message_retention` and returns the count deleted (this also happens automatically when `auto_trim=True`):

```python
ch = Channel(
    database_url="sqlite:///channel.db",
    polling_interval=0.1,      # listener poll cadence, seconds
    message_retention=86400.0, # keep messages this long (seconds)
    auto_trim=True,             # trim opportunistically on broadcast
    trim_batch_size=100,
)

deleted = ch.trim()  # -> int: rows removed this batch
ch.close()           # stops the listener; disposes the engine if Channel created it
```

Use it as a context manager so the listener thread and engine are cleaned up for you:

```python
import time
from firm.channel import Channel

with Channel(database_url="sqlite:///channel.db") as ch:
    received: list[bytes] = []
    ch.subscribe("news", received.append)  # callback gets bytes
    ch.broadcast("news", b"first")
    ch.broadcast("news", b"second")
    time.sleep(0.3)            # let the background listener poll (~0.1s/cycle)
    print(received)            # [b"first", b"second"]
# listener stopped, engine disposed
```

---

## Audit: record events, query history, retention {#audit}

`firm.audit` is an append-only, database-backed audit log — not a Solid port (there's no `solid_audit`), but the same "you already have a database" idea. The headline feature is the **same-transaction guarantee**: record the event inside the same transaction as the business change, and it can never exist without it (or vice versa).

```python
from firm.audit import AuditLog, record

# shared DB, atomic with a business change — join the caller's own transaction:
def mark_invoice_paid(engine, invoice, user, amount):
    with engine.begin() as conn:
        conn.execute(invoices.update().where(invoices.c.id == invoice.id).values(paid=True))
        record(conn, "invoice.paid", subject=invoice, actor=user, data={"amount": amount})
    # both the update and the audit row commit together, or neither does
```

```python
# standalone (or a separate audit database) — AuditLog manages its own connection
audit = AuditLog(database_url="sqlite:///audit.db")   # create_schema=True auto-creates firm_audits

audit.record("user.login", actor=("User", 7), context={"ip": "127.0.0.1"})
audit.record(
    "invoice.paid",
    subject=("Invoice", 42),
    actor=("User", 7),
    data={"amount": 4200},
    correlation_id=request_id,   # ties every event from one request together
)
```

Querying:

```python
audit.history(action="invoice.paid")               # most recent first
audit.history(subject=("Invoice", 42), limit=10)    # this invoice's history
audit.history(correlation_id=request_id)            # everything from one request
```

`subject`/`actor` accept a domain object with `.id` (`subject=invoice`) or an explicit `("Type", id)` tuple — `history()` only ever filters on the indexed scalar columns, never inside `data`/`changes`/`context` (they're JSON text, not queryable JSONB — see [Internals](audit/internals.md)).

Events are kept **forever by default** — pruning is opt-in, not automatic, and never triggered by `record()`:

```python
audit = AuditLog(database_url="sqlite:///audit.db", max_age=90 * 24 * 3600.0)  # 90 days
audit.retention.run_once()   # -> int: events deleted
```

```bash
firm-audit prune --database-url sqlite:///audit.db --max-age 7776000
```

See **[firm-audit overview](audit/overview.md)** for the full module.

---

## Queue + Cache: cache-warming jobs & read-through {#queue--cache}

Point the queue and the cache at the same database and share one `Engine` so warming jobs and read-through reads hit the same store. A `@bq.job` recomputes the expensive value and writes it with `cache.set()`; everything else reads through `cache.fetch()`, which recomputes-and-stores only on a miss.

```python
# shared.py — one engine, one database, shared by queue and cache
import firm.queue as bq
from firm.cache import Cache
from firm.queue import current_runtime

# Configure the queue first; its Runtime owns the Engine for this process.
bq.configure(database_url="postgresql://localhost/myapp")

# Reuse the queue's Engine for the cache (same DB, same connection pool).
# engine= means the Cache does NOT own/dispose it — the Runtime does.
cache = Cache(engine=current_runtime().engine)
```

```python
# jobs.py — the cache-warming job
import firm.queue as bq
from shared import cache

def _recompute_dashboard(account_id: int) -> dict:
    # ... the slow query / aggregation you don't want on the request path ...
    return {"account_id": account_id, "revenue": 12345}

@bq.job(queue="cache", attempts=3, backoff=5.0)
def warm_dashboard(account_id: int) -> None:
    # Recompute the expensive value and overwrite the cache entry.
    cache.set(f"dashboard:{account_id}", _recompute_dashboard(account_id))

# The decorated function is still directly callable (handy for tests):
#   warm_dashboard(42)            # runs the body inline, no enqueue
#   _recompute_dashboard(42)      # the raw computation
```

```python
# web.py — enqueue a warm from your request handler
from jobs import warm_dashboard

def on_account_changed(account_id: int) -> None:
    # Fire-and-forget; returns the new job_id (or None if discarded on a concurrency conflict).
    warm_dashboard.enqueue(account_id)
```

```python
# reader.py — read-through anywhere (web handler, another worker, a script)
from shared import cache
from jobs import _recompute_dashboard

def get_dashboard(account_id: int) -> dict:
    # fetch() returns the cached value, or calls the callable on a miss,
    # stores the result, and returns it. The callable takes no arguments.
    return cache.fetch(
        f"dashboard:{account_id}",
        lambda: _recompute_dashboard(account_id),
    )
```

```bash
# Run the workers that drain the warming jobs (production default: fork mode).
firm-queue start --import jobs --queues cache,default
```

To keep entries fresh on a schedule rather than on change, enqueue the same job from a `RecurringTask`:

```python
# recurring.py — re-warm every 5 minutes via the supervisor
from firm.queue.scheduler import RecurringTask
from firm.queue.supervisor import (
    DispatcherConfig, SupervisorConfig, ThreadSupervisor, WorkerConfig,
)
from firm.queue import current_runtime
from jobs import warm_dashboard

config = SupervisorConfig(
    workers=[WorkerConfig(queues=("*",), threads=3)],
    dispatchers=[DispatcherConfig()],
    recurring=[
        RecurringTask(key="warm-dashboard-1", schedule="*/5 * * * *",
                      job=warm_dashboard, args=(1,)),  # 5-field cron
    ],
)

with ThreadSupervisor(current_runtime(), config):  # .start() non-blocking; context mgr drains on exit
    ...
```

---

## Queue + Channel: progress updates & notifications {#queue--channel}

A long-running `@bq.job` can stream progress and completion events over a `Channel`. The job `broadcast`s to a channel (the queue and the channel just need to point at the same database); a separate process (websocket bridge, log tailer) `subscribe`s and reacts. Payloads are opaque bytes, so serialize structured data yourself (e.g. JSON).

```python
# jobs.py — the producer side
import json
import firm.queue as bq
from firm.channel import Channel

bq.configure(database_url="postgresql://localhost/app")

# One Channel per process is enough; reuse it across job runs.
channel = Channel(database_url="postgresql://localhost/app")


@bq.job(queue="exports", attempts=3)
def export_report(report_id: int, row_count: int) -> None:
    topic = f"export:{report_id}"  # channel name is any str (or bytes)
    for i in range(row_count):
        # ... do a unit of work ...
        if i % 100 == 0:
            pct = round(100 * i / row_count)
            # broadcast(channel, payload): payload is str|bytes (str is UTF-8 encoded)
            channel.broadcast(topic, json.dumps({"report_id": report_id, "percent": pct}))

    # fan-out completion: a per-report topic AND a shared "all exports" topic
    done = json.dumps({"report_id": report_id, "percent": 100, "status": "done"})
    channel.broadcast(topic, done)
    channel.broadcast("exports:completed", done)  # subscribers watching every export
```

```python
# enqueue it (the decorated function is still directly callable for unit tests)
export_report.enqueue(42, row_count=5000)   # -> job_id: int | None
# export_report(42, row_count=10)           # runs inline, no queue, for testing
```

Run a worker so the job actually executes (separate process):

```bash
# starts workers + dispatcher; --import loads the module that defines the job
firm-queue start --import jobs --queues exports
```

The subscriber reacts to messages. A subscriber only receives messages broadcast *after* it subscribes, and the callback always receives `bytes`:

```python
# bridge.py — the consumer side (e.g. a websocket relay or a log)
import json
import time
from firm.channel import Channel

with Channel(database_url="postgresql://localhost/app") as channel:
    def on_progress(payload: bytes) -> None:  # callback signature: (bytes) -> None
        event = json.loads(payload)           # decode your own format
        print(f"report {event['report_id']}: {event['percent']}%")
        # e.g. await ws.send_json(event) in a real websocket bridge

    def on_completion(payload: bytes) -> None:
        event = json.loads(payload)
        print(f"COMPLETED report {event['report_id']}")

    channel.subscribe("export:42", on_progress)            # one specific report
    channel.subscribe("exports:completed", on_completion)  # fan-out: every completion

    # subscribe() spins up one background polling thread; just keep the process alive.
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        channel.unsubscribe("export:42", on_progress)  # remove a single callback
```

Notes that matter for correctness:
- **Delivery is per-process**: every process running a `Channel` against the same DB sees every `broadcast`. The websocket bridge and the worker are different processes — that is the intended split.
- **No backlog replay**: messages sent before `subscribe` are not delivered. Subscribe before you start broadcasting if you need the first event.
- **Polling latency**: subscribers fire on the next poll, governed by `polling_interval` (default `0.1`s). Tune via `Channel(..., polling_interval=0.05)`.
- **Channel names** are `str` or `bytes`; **payloads** are `str` or `bytes` (a `str` is UTF-8 encoded). There is no built-in JSON codec — serialize/deserialize yourself.
- A subscriber callback that raises is suppressed: it never breaks the listener or other subscribers.

---

## Cache + Channel: cross-process cache invalidation {#cache--channel}

Keep a hot value in the `Cache` (write-through on update) and use a `Channel` to tell every *other* process to drop its copy. The writer broadcasts the changed key; each process subscribes once at startup and calls `cache.delete(key)` on receipt so the next read re-fetches fresh.

```python
# shared.py — one Engine shared by Cache and Channel (both can ride the same DB connection pool)
from firm._core.database import create_engine_for
from firm.cache import Cache
from firm.channel import Channel

engine = create_engine_for("postgresql://localhost/app")

cache = Cache(engine=engine)        # write-through store; pickle coder by default
channel = Channel(engine=engine)    # pub/sub for invalidation signals

INVALIDATE = "cache:invalidate"     # the channel name we publish key names on
```

```python
# subscriber side — run once per process at startup
def _evict(payload: bytes) -> None:
    # callback always receives bytes; the key was broadcast as a UTF-8 string
    key = payload.decode("utf-8")
    cache.delete(key)               # returns True if a row was removed; safe if already gone

# only messages broadcast AFTER this call are delivered; delivery is per-process
channel.subscribe(INVALIDATE, _evict)
```

```python
# publisher side — call on every update
def update_user(user_id: int, name: str) -> None:
    key = f"user:{user_id}"
    save_to_db(user_id, name)       # your system of record

    cache.set(key, {"id": user_id, "name": name})   # 1. write-through: this process is fresh
    channel.broadcast(INVALIDATE, key)              # 2. tell other processes to evict their copy
```

Note: `broadcast` reaches subscribers in *every* process (including this one), but since the publisher already did `cache.set`, its own `_evict` just deletes the value it just wrote — the next `get` re-fetches. If you want the writer to skip self-eviction, tag the payload with a process id and have `_evict` ignore its own. Reads stay simple:

```python
# any process — fetch-through fills the cache on a miss (e.g. right after an eviction)
def get_user(user_id: int) -> dict:
    key = f"user:{user_id}"
    return cache.fetch(key, lambda: load_from_db(user_id))  # computes + caches if absent
```

---

## Queue + Audit: log job lifecycle events {#queue--audit}

Point the queue and the audit log at the same database, and record lifecycle events as part of the job itself — start, completion, failure — each tagged with the job's id as a `correlation_id` so `firm-audit history --correlation-id <id>` (or `audit.history(correlation_id=...)`) shows everything that happened during one run.

```python
# jobs.py
import firm.queue as bq
from firm.audit import AuditLog

bq.configure(database_url="postgresql://localhost/app")
audit = AuditLog(database_url="postgresql://localhost/app")  # same DB, separate table


@bq.job(queue="exports", attempts=3)
def export_report(report_id: int) -> None:
    cid = f"export:{report_id}"
    audit.record("export.started", subject=("Report", report_id), correlation_id=cid)
    try:
        # ... do the work ...
        audit.record("export.finished", subject=("Report", report_id), correlation_id=cid)
    except Exception as exc:
        audit.record(
            "export.failed",
            subject=("Report", report_id),
            correlation_id=cid,
            data={"error": str(exc)},
        )
        raise
```

```python
# inspect a run's full timeline
for event in audit.history(correlation_id=f"export:{report_id}"):
    print(event["created_at"], event["action"])
```

This is `audit.record()` in its own transaction (the job and the audit write are two separate commits) — fine for an activity trail. If the audit row needs to be atomic with something the job itself writes, pass that write's `conn` to `record(conn, ...)` instead, inside the same `engine.begin()` block — see [Audit: record events, query history, retention](#audit).

---

## All three together: an end-to-end pipeline {#all-three-together}

Here's how queue, cache, and channel compose into one realistic flow: an HTTP request enqueues a `generate_report` job; the worker reads its inputs from the cache, does the work, writes the result back to the cache, and broadcasts `"done"` on a channel so a subscriber can notify the user. All three share one database, so there's nothing extra to run.

```python
# app/pipeline.py
import json

import firm.queue as bq
from firm.cache import Cache, JSONCoder
from firm.channel import Channel

DATABASE_URL = "sqlite:///app.db"  # use postgresql://… in production

# One configure() call wires up the queue runtime for this process.
bq.configure(database_url=DATABASE_URL)

# Cache and Channel are plain objects; JSONCoder keeps cached values portable.
cache = Cache(database_url=DATABASE_URL, coder=JSONCoder())
channel = Channel(database_url=DATABASE_URL)


@bq.job(queue="reports", attempts=3, backoff=5.0)
def generate_report(report_id: str) -> None:
    # 1. Read inputs the request stashed in the cache.
    inputs = cache.get(f"report:{report_id}:inputs")  # -> dict | None
    if inputs is None:
        return  # nothing to do

    # 2. Do the work.
    result = {"rows": len(inputs["records"]), "title": inputs["title"]}

    # 3. Write the result back to the cache for whoever needs it.
    cache.set(f"report:{report_id}:result", result)

    # 4. Broadcast "done" so subscribers can react. Payload is str/bytes only.
    channel.broadcast("reports:done", json.dumps({"report_id": report_id}))
```

The producer side — e.g. inside a request handler — stages the inputs in the cache and enqueues the job:

```python
# Inside an HTTP request handler
from app.pipeline import cache, generate_report

cache.set(f"report:{report_id}:inputs", {"title": "Q2", "records": [...]})
job_id = generate_report.enqueue(report_id)  # -> int (the job_id)
```

The subscriber side runs wherever you want to notify the user. The callback receives raw `bytes`, and a subscription only sees messages broadcast *after* it subscribes:

```python
# app/notifier.py
import json

from app.pipeline import cache, channel


def on_report_done(payload: bytes) -> None:
    report_id = json.loads(payload)["report_id"]
    result = cache.get(f"report:{report_id}:result")  # fetch the finished result
    print(f"report {report_id} ready: {result}")  # ...or email/push the user

# Register the callback; the first subscribe starts a background polling listener.
channel.subscribe("reports:done", on_report_done)
```

Run the workers that actually execute `generate_report` with the CLI — point `--import` at the module where the job is defined:

```bash
# Workers pick up the "reports" queue and run the job body
firm-queue start --import app.pipeline --queues reports
```

To keep everything in one process (handy for tests or a dev server), embed a worker instead of the CLI:

```python
from firm.queue.supervisor import (
    SupervisorConfig,
    ThreadSupervisor,
    WorkerConfig,
    DispatcherConfig,
)

runtime = bq.configure(database_url=DATABASE_URL)
config = SupervisorConfig(
    workers=[WorkerConfig(queues=("reports",), threads=3)],
    dispatchers=[DispatcherConfig()],
)
# .start() is non-blocking; the supervisor is also a context manager that drains on exit.
with ThreadSupervisor(runtime, config):
    channel.subscribe("reports:done", on_report_done)
    generate_report.enqueue("r-123")
    ...  # wait for the job to run and the notification to fire
```

---

## FastAPI app (full) {#fastapi}

A single-file FastAPI app: firm's `lifespan` configures the queue on startup, routes enqueue jobs, `enqueue_after_commit` ties an enqueue to a SQLAlchemy commit, and a route reads from the cache. In dev, `embed_workers=True` runs a worker in-process; in prod you leave it `False` and run `firm-queue start` as a separate process.

```python
# app.py
from datetime import timedelta

from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import firm.queue as bq
from firm.cache import Cache
from firm.contrib.fastapi import lifespan
from firm.contrib.sqlalchemy import enqueue_after_commit

DATABASE_URL = "postgresql://localhost/app"

# --- jobs: plain functions decorated into enqueueable Jobs ---
@bq.job(queue="emails", attempts=3, backoff=5.0)
def send_welcome(user_id: int) -> None:
    ...  # the function stays directly callable too: send_welcome(1)

@bq.job  # queue="default", priority=0, attempts=1, backoff=3.0
def reindex(user_id: int) -> None:
    ...

# --- shared resources ---
# embed_workers=True runs a worker+dispatcher in-process (dev / single-process).
# In prod set embed_workers=False and run `firm-queue start` separately.
app = FastAPI(lifespan=lifespan(database_url=DATABASE_URL, embed_workers=True))

cache = Cache(DATABASE_URL)                 # pickle values by default
engine = create_engine(DATABASE_URL)        # your app's own SQLAlchemy engine


# --- enqueue directly from a route ---
@app.post("/welcome/{user_id}")
def welcome(user_id: int) -> dict:
    job_id = send_welcome.enqueue(user_id)          # returns int | None
    return {"job_id": job_id}

# enqueue_at / enqueue_in for scheduled work
@app.post("/welcome/{user_id}/later")
def welcome_later(user_id: int) -> dict:
    send_welcome.enqueue_in(timedelta(hours=1), user_id)
    return {"scheduled": True}


# --- enqueue tied to a SQLAlchemy commit (drops on rollback) ---
@app.post("/users/{user_id}/reindex")
def reindex_user(user_id: int) -> dict:
    with Session(engine) as session:
        # ... mutate rows on `session` ...
        enqueue_after_commit(session, reindex, user_id)  # fires iff commit succeeds
        session.commit()                                  # rollback => job never enqueued
    return {"ok": True}


# --- read a cache value ---
@app.get("/users/{user_id}/profile")
def profile(user_id: int) -> dict:
    # fetch(key, default_or_callable): compute + store on miss
    data = cache.fetch(f"profile:{user_id}", lambda: {"id": user_id})
    return data
```

Run it (dev — workers embedded in the app process):

```bash
uvicorn app:app
```

In production, set `embed_workers=False` in the `lifespan(...)` call and run workers as their own process:

```bash
# API process
uvicorn app:app
# worker process(es) — point --import at the module that defines your @bq.job functions
firm-queue start --import app --mode fork
```

---

## Flask app (full) {#flask}

A complete single-file Flask app: `Firm(app)` configures the queue from `app.config["FIRM_DATABASE_URL"]`, routes enqueue jobs, and a cached endpoint reuses computed results. Run workers out-of-process with `flask firm worker`; flip `embed_workers=True` only for local dev.

```python
# app.py
from flask import Flask, jsonify
import firm.queue as bq
from firm.cache import Cache
from firm.contrib.flask import Firm

app = Flask(__name__)
app.config["FIRM_DATABASE_URL"] = "postgresql://localhost/app"

# Reads app.config["FIRM_DATABASE_URL"] and calls bq.configure() for you.
# embed_workers=False (default) -> run workers separately via `flask firm worker`.
# Set embed_workers=True to run an in-process ThreadSupervisor (dev / single process only).
Firm(app)  # or: Firm(app, embed_workers=True, queues=("*",), threads=3)

# A cache backed by the same database (its own tables; create_schema=True auto-creates them).
cache = Cache(database_url=app.config["FIRM_DATABASE_URL"])


# A plain function that's still directly callable; `.enqueue()` queues it.
@bq.job(queue="default", attempts=3, backoff=5.0)
def send_welcome(user_id: int) -> None:
    print(f"welcomed {user_id}")


@app.post("/welcome/<int:user_id>")
def welcome(user_id):
    send_welcome.enqueue(user_id)  # returns job_id (int) or None if discarded
    return "", 202


@app.get("/stats/<int:user_id>")
def stats(user_id):
    # fetch() returns the cached value, or calls the factory, stores it, and returns it.
    data = cache.fetch(f"stats:{user_id}", lambda: compute_stats(user_id))
    return jsonify(data)


def compute_stats(user_id: int) -> dict:
    return {"user_id": user_id, "score": 42}
```

Run the web server and a worker process side by side:

```bash
# Terminal 1 — the web app
flask --app app run

# Terminal 2 — a worker + dispatcher, running until Ctrl-C.
# `firm` is the CLI group, `worker` the command (registered by Firm(app)).
flask --app app firm worker --queues "*" --threads 3
```

---

## Operations & production {#operations--production}

Day-two operations: keeping queues healthy, recovering failures, scheduling recurring work, and running workers as real processes. All of the admin functions take a `Runtime` (from `bq.configure(...)` or `current_runtime()`); the worker fleet runs separately via `ForkSupervisor` or the `firm-queue` CLI.

### Concurrency controls

Limit how many copies of a job run at once, keyed on the arguments. Declared on the decorator; the dispatcher enforces it.

```python
import firm.queue as bq

# At most ONE sync per account at a time; extra enqueues wait their turn ("block").
@bq.job(concurrency={
    "key": lambda account_id: account_id,  # variable part of the lock key (*args, **kwargs)
    "to": 1,                                # max simultaneous executions per key (default 1)
    "duration": 300,                        # failsafe TTL in seconds before the lock self-expires
    "group": None,                          # share a key namespace across jobs; default = this job's name
    "on_conflict": "block",                 # "block" = queue it; "discard" = drop it silently
})
def sync_account(account_id: int) -> None:
    ...

# Coalesce duplicate refreshes: while one is running/queued, drop the rest.
@bq.job(concurrency={"key": lambda url: url, "to": 1, "on_conflict": "discard"})
def warm_cache(url: str) -> None:
    ...
```

Blocked jobs are promoted by the dispatcher's maintenance pass (`run_maintenance`, run automatically by `DispatcherConfig.maintenance_interval`).

### Recurring tasks

Pair a 5-field cron expression with a job and hand the list to the supervisor — it starts a scheduler automatically when `recurring` is non-empty. Each `(key, fire-time)` is enqueued exactly once across the whole fleet.

```python
from firm.queue.scheduler import RecurringTask, Scheduler
from firm.queue.supervisor import (
    ThreadSupervisor, SupervisorConfig, WorkerConfig, DispatcherConfig,
)
from firm.queue import current_runtime

tasks = [
    RecurringTask(key="nightly-cleanup", schedule="0 3 * * *", job=cleanup),
    RecurringTask(key="hourly-sync", schedule="0 * * * *", job=sync, args=(42,)),
]

config = SupervisorConfig(
    workers=[WorkerConfig()],
    dispatchers=[DispatcherConfig()],
    recurring=tasks,            # non-empty -> a scheduler process/thread is started
)
with ThreadSupervisor(current_runtime(), config):
    ...

# Drive it manually (tests / cron-of-last-resort):
scheduler = Scheduler(current_runtime(), tasks)
scheduler.sync_tasks()         # persist definitions to recurring_tasks (for visibility)
scheduler.tick()               # enqueue anything due for the current period
```

### Pause / resume / clear queues

```python
from firm.queue import queues
from firm.queue import current_runtime

rt = current_runtime()

queues.all_queues(rt)            # -> ["default", "mail", ...] (queues with ready jobs)
queues.size(rt, "mail")          # -> int: ready jobs waiting
queues.latency(rt, "mail")       # -> float: seconds the oldest ready job has waited

queues.pause(rt, "mail")         # workers stop claiming from "mail" (jobs stay put)
queues.is_paused(rt, "mail")     # -> bool
queues.resume(rt, "mail")        # un-pause

queues.clear(rt, "mail")         # -> int: discards ALL ready jobs in the queue (destructive)
```

### Retrying failures

```python
from firm.queue import maintenance

maintenance.retry_failed(rt, job_id)     # -> bool: one failed job back to ready (attempts reset)
maintenance.retry_all_failed(rt)         # -> int: re-enqueue every failed job
```

### Finished-job retention

By default finished jobs are preserved (`bq.configure(..., preserve_finished_jobs=True)`). Trim them on a schedule so the table doesn't grow unbounded:

```python
from datetime import timedelta
from firm.queue import maintenance

# Delete finished jobs older than 7 days, 500 rows per batch (the default).
maintenance.clear_finished(rt, older_than=timedelta(days=7), batch_size=500)

# Or all finished jobs, regardless of age:
maintenance.clear_finished(rt)
```

Good fit for a `RecurringTask` wrapping a `@bq.job` that calls `clear_finished`.

### The firm-ui dashboard

A small read/act web dashboard. It binds to `127.0.0.1` and exposes tracebacks plus destructive actions (retry / discard / pause / clear) — put it behind a reverse proxy + auth before exposing it. Tabs appear only for the parts (queue / cache / channel / audit) whose tables exist.

```bash
# One shared database for all parts
firm-ui --database-url postgresql://localhost/myapp        # http://127.0.0.1:8787

# Per-part databases (any subset); --host/--port to change the bind
firm-ui --queue-url postgresql://db1/jobs \
         --cache-url  postgresql://db2/cache \
         --host 127.0.0.1 --port 8787
# or set FIRM_DATABASE_URL instead of --database-url
```

### Alembic migrations per module

Each module ships its own Alembic config and baseline migration. Run them independently — point the right `alembic.<module>.ini` at the right database via the module's env var (or `-x url=...`):

```bash
# queue
FIRM_QUEUE_DATABASE_URL=postgresql://localhost/myapp \
  alembic -c alembic.queue.ini upgrade head

# cache
FIRM_CACHE_DATABASE_URL=postgresql://localhost/myapp \
  alembic -c alembic.cache.ini upgrade head

# channel
FIRM_CHANNEL_DATABASE_URL=postgresql://localhost/myapp \
  alembic -c alembic.channel.ini upgrade head

# audit
FIRM_AUDIT_DATABASE_URL=postgresql://localhost/myapp \
  alembic -c alembic.audit.ini upgrade head

# alternatively, pass the URL inline:
alembic -c alembic.queue.ini -x url=postgresql://localhost/myapp upgrade head
```

In development you can skip Alembic — `Cache(create_schema=True)` (the default) builds its table, and the queue schema can be created directly. Use Alembic for production.

### ThreadSupervisor vs ForkSupervisor

Both take `(runtime, SupervisorConfig)`. `ThreadSupervisor` runs every role as a thread in one process — `.start()` is non-blocking, `.stop()` drains, and it's a context manager; ideal for dev, tests, and embedding (the only option on Windows). `ForkSupervisor` forks a child process per role, reaps and restarts dead children, and recovers their in-flight jobs — the POSIX production default; `.start()` **blocks**, supervising until it gets a shutdown signal.

```python
from firm.queue.supervisor import ForkSupervisor, SupervisorConfig, WorkerConfig
from firm.queue import current_runtime

# Production: blocks, supervising, until TERM/INT (drain) or QUIT (immediate).
ForkSupervisor(
    current_runtime(),
    SupervisorConfig(
        workers=[WorkerConfig(queues=("*",), threads=5)],
        alive_threshold=300.0,     # prune a process whose heartbeat is this stale
        shutdown_timeout=5.0,      # grace period for an in-flight drain on TERM
        heartbeat_interval=60.0,
    ),
).start()
```

> CPU-bound work scales by running **more worker processes** (more forks / more boxes), not more threads — the GIL serializes threads. Threads parallelize I/O-bound jobs.

### Running workers as separate processes

The `firm-queue` CLI is the usual production entrypoint. `--import` loads the modules whose `@job`s must register; the URL comes from `--database-url` or `FIRM_QUEUE_DATABASE_URL`.

```bash
# Full stack (workers + dispatcher), forked — production default
firm-queue start --import myapp.jobs --mode fork --threads 5 --queues "*"

# Pin a process to specific queues (e.g. a dedicated mail box)
firm-queue start --import myapp.jobs --queues "mail,notifications*" --threads 2

# Windows / containers without fork:
firm-queue start --import myapp.jobs --mode thread

# Single-shot variants (cron, CI, debugging) — drain once and exit, no polling:
firm-queue drain       --import myapp.jobs --queues "*" --limit 100
firm-queue dispatch    --import myapp.jobs     # promote due scheduled jobs once
firm-queue maintenance --import myapp.jobs     # promote blocked (concurrency) jobs once
```

The single-shot commands map to `firm.queue.worker.run_ready(rt, queues=(...), limit=N)`, `dispatcher.dispatch_once(rt)`, and `dispatcher.run_maintenance(rt)` if you'd rather drive them in-process.

### Pointing modules at separate databases

Nothing forces queue, cache, and channel to share one database — each is configured independently, so you can isolate hot tables or scale them separately. Match each one's migrations and its `firm-ui` tab to the same URL.

```python
import firm.queue as bq
from firm.cache import Cache
from firm.channel import Channel

bq.configure(database_url="postgresql://jobs-db/myapp")     # queue
cache = Cache(database_url="postgresql://cache-db/myapp")    # cache on its own box
channel = Channel(database_url="postgresql://bus-db/myapp")  # channel on a third

# Migrate each against its matching database:
#   FIRM_QUEUE_DATABASE_URL=postgresql://jobs-db/myapp  alembic -c alembic.queue.ini upgrade head
#   FIRM_CACHE_DATABASE_URL=postgresql://cache-db/myapp alembic -c alembic.cache.ini upgrade head

# To share one Engine between cache and channel on the same DB:
from firm._core.database import create_engine_for
engine = create_engine_for("postgresql://localhost/myapp")
cache = Cache(engine=engine)
channel = Channel(engine=engine)
```
