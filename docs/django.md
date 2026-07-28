# Django

firm has no Django dependency and does not need one. But "run on the database you already have" is
firm's whole premise, and in Python that database most often belongs to Django — so firm ships a
Django integration, in three independent pieces:

| Piece | Wired up by | Gives you |
|---|---|---|
| The app | `INSTALLED_APPS = [..., "firm.queue.contrib.django"]` | firm configured from `DATABASES` in every process, its tables created by `manage.py migrate`, `<app>/jobs.py` imported automatically, and `manage.py firm_worker` |
| The Tasks backend | `TASKS["default"]["BACKEND"]` | Django 6.0's official `@task` / `Task.enqueue()` interface, stored in firm's tables |
| The cache backend | `CACHES["default"]["BACKEND"]` | `django.core.cache` on `firm-cache`, in that same database |

Take one, two, or all three. This page is the Django-specific detail around them: what the app
config actually does, how firm's Alembic migrations sit next to Django's, what happens to an
enqueue inside `transaction.atomic()`, what having two connection pools costs you, and where firm
sits against Django 6 Tasks, steady-queue and django-tasks-db.

A complete working project is in [`examples/django_app/`](../examples/django_app/).

## Is firm the right choice here?

If you want a database-backed queue and you are staying inside Django, look at
[steady-queue](https://github.com/knifecake/steady-queue) first. It is also a Solid Queue port,
it implements Django 6.0's `django.tasks` interface, and it gives you a Django admin integration.
Being Django-native is its whole point.

firm makes sense in a Django project when the queue is **not only** a Django concern:

- **A shared store between Django and services that aren't Django.** A FastAPI service, a Go-free
  Python daemon, a CLI, an ETL script — anything that can open the same database can enqueue jobs
  for, and consume broadcasts from, your Django app. `@bq.job` has no framework coupling, so the
  producer and the consumer do not have to be the same kind of process, or even the same codebase.
- **You want more than a queue.** `firm-cache` (a cache in the same database), `firm-channel`
  (pub/sub in the same database), and `firm-audit` (an append-only, optionally tamper-evident
  audit log) are separate packages that share the queue's engine and connection pool. Django's
  cache framework is unrelated infrastructure; here it's the same rows, the same backup, the same
  transaction log.
- **You want operational features Django's interface does not define** — concurrency limits,
  recurring cron tasks, a supervisor that reaps and restarts workers, and a dashboard that isn't
  the Django admin.

See [the comparison below](#firm-vs-django-6-tasks-steady-queue-and-django-tasks-db) for what
firm gives up in exchange.

## Install

```bash
pip install "firm-queue[django]"       # the app, the Tasks backend, manage.py firm_worker
pip install "firm-cache[django]"       # the CACHES backend
pip install "firm-queue[postgres]"     # or [mysql]; SQLite needs nothing
```

The `[django]` extra is `django>=5.2`. The Tasks backend additionally needs **Django 6.0**, the
release that introduced `django.tasks`; everything else on this page works on 5.2.

## Setup

```python
# settings.py
INSTALLED_APPS = ["myapp", "firm.queue.contrib.django"]
```

That one line is the entire required configuration. It gets you:

- **firm configured from `DATABASES`, in every process.** The URL is derived from
  `connections["default"].settings_dict`, so firm points at the same database Django does —
  including the *test* database, which is exactly where a hardcoded `FIRM_DATABASE_URL` goes
  silently wrong.
  `AppConfig.ready()` is the one hook Django runs exactly once per process, so `runserver`,
  gunicorn/uwsgi workers, every `manage.py` command and the worker share a single code path.
- **`manage.py migrate` creates firm's tables**, via `post_migrate`, and stamps its Alembic
  version table at head. See [Migrations](#migrations).
- **`<app>/jobs.py` imported from every installed app**, so workers can resolve your jobs. Same
  mechanism `django.contrib.admin` uses for `admin.py`.
- **`manage.py firm_worker`.**

```console
$ python manage.py migrate
Operations to perform:
  Apply all migrations: myapp
Running migrations:
  Applying myapp.0001_initial... OK

$ sqlite3 app.db ".tables"
django_migrations                firm_queue_processes
firm_queue_alembic_version       firm_queue_ready_executions
firm_queue_blocked_executions    firm_queue_recurring_executions
firm_queue_claimed_executions    firm_queue_recurring_tasks
firm_queue_failed_executions     firm_queue_scheduled_executions
firm_queue_jobs                  firm_queue_semaphores
firm_queue_pauses                myapp_order
```

### The `FIRM_QUEUE` settings block

One namespaced dict, every key optional. An unknown key raises `ImproperlyConfigured` rather than
being ignored — a silently-swallowed typo here means firm runs against the wrong database.

```python
FIRM_QUEUE = {
    "DATABASE_ALIAS": "default",
    "CREATE_SCHEMA": True,
}
```

| Key | Default | Meaning |
|---|---|---|
| `DATABASE_ALIAS` | `"default"` | Which `DATABASES` entry firm follows. |
| `DATABASE_URL` | `None` | Skip that derivation entirely and point firm at a database Django doesn't manage. |
| `CREATE_SCHEMA` | `True` | Create firm's tables from `manage.py migrate`. |
| `AUTODISCOVER` | `True` | Import `<app>/jobs.py` from every installed app. |
| `JOBS_MODULE` | `"jobs"` | The module name autodiscovery looks for. |
| `IMPORTS` | `()` | Extra modules to import for their `@job` definitions. |
| `CLOSE_CONNECTIONS` | `True` | Close Django's ORM connections after every job, the way a finished request does. |
| `BUSY_TIMEOUT_MS`, `POOL_SIZE`, `MAX_OVERFLOW`, `DEFAULT_QUEUE`, `PRESERVE_FINISHED_JOBS` | `None` | Passed to `configure()` **only when set**, so firm's own defaults stay the single source of truth and can't drift from a copy kept here. |
| `QUEUES`, `THREADS`, `MODE` | `None` | Defaults for `manage.py firm_worker`; its flags win. |

### Where jobs live

Jobs are ordinary `@bq.job` functions. Put them in `<app>/jobs.py` and nothing else is needed:

```python
# myapp/jobs.py
import firm.queue as bq

from myapp.models import Order


@bq.job(queue="billing", attempts=3)
def charge_order(order_id: int) -> None:
    Order.objects.get(pk=order_id).charge()
```

Jobs elsewhere need naming, because a worker resolves them by `"module.qualname"` and can only do
that for modules it has imported:

```python
FIRM_QUEUE = {"IMPORTS": ("myapp.billing.jobs", "shared.pipeline")}
```

Autodiscovery runs inside `AppConfig.ready()`, which Django calls *after* it has imported every
`models.py` — so a module-level `from myapp.models import Order` in `jobs.py` is fine. (That is
not true of your own `AppConfig.ready()` body, which may run before other apps' models exist.)

## Enqueueing inside a Django transaction

`Job.enqueue()` opens its **own** transaction on firm's SQLAlchemy engine. It is not, and cannot
be, part of Django's transaction — different connection, different pool. Outside `atomic()` a bare
`.enqueue()` is exactly right. *Inside* one it is wrong twice over, and both behaviours below were
measured, not inferred:

- **PostgreSQL and MySQL** — the enqueue commits immediately and **survives the rollback**. The
  job then runs against a row that never existed.
- **SQLite** — it deadlocks: Django holds the single write lock for the duration of the `atomic()`
  block, firm's connection waits on it until `busy_timeout` expires, and you get
  `sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) database is locked`.

`enqueue_on_commit` is the opt-in fix — the Django twin of
[`enqueue_after_commit`](contrib.md#transactional-enqueue) for SQLAlchemy sessions:

```python
from django.db import transaction
from firm.queue.contrib.django import enqueue_on_commit

with transaction.atomic():
    order = Order.objects.create(...)
    enqueue_on_commit(charge_order, order.pk)
```

It hands the enqueue to `django.db.transaction.on_commit`: it runs after Django commits and
releases the lock, and is dropped if the transaction rolls back. Outside `atomic()` Django runs
the callback immediately, so the helper is always safe — you never have to decide per call site.

Two consequences. There is **no return value**: `on_commit` discards its callback's, so the
`job_id` is unavailable. And the guarantee is "enqueue if and only if the transaction committed",
**not one atomic write** — a crash in the window between Django's commit and firm's insert loses
the enqueue. That is the same trade-off firm's SQLAlchemy helper documents. If you need a job that
cannot be lost, write an outbox row inside the Django transaction and enqueue from a sweeper.

`enqueue_on_commit` deliberately takes no `using=` (it would collide with a job argument of that
name) and has no deferred variant. For another database alias, or for `enqueue_at` / `enqueue_in`,
call Django directly:

```python
from datetime import timedelta
from functools import partial

from django.db import transaction

transaction.on_commit(
    partial(charge_order.enqueue_in, timedelta(minutes=5), order.pk), using="shard1"
)
```

> **What firm gives up here.** firm's headline claim is that you can enqueue a job in the same
> transaction as the row it depends on. Against the Django ORM you cannot, because firm never sees
> Django's connection. You keep the single datastore, the single backup, and the single set of
> rows to inspect; you lose transactional atomicity of the enqueue itself.

## Running the worker

```bash
python manage.py firm_worker --mode fork --threads 5 --queues default,billing
```

The flags are `firm-queue start`'s, so an invocation carries over between the two. Two are absent
because Django settings already own them: there is no `--database-url` (`DATABASE_ALIAS` decides),
and `--import` is rarely needed because `<app>/jobs.py` is autodiscovered. Each flag falls back to
`FIRM_QUEUE["QUEUES"]` / `["THREADS"]` / `["MODE"]`, then to `*`, `3`, and `fork`.

Use `--mode fork` in production for real multi-core parallelism and automatic restart of crashed
children (it closes Django's ORM connections before forking, so parent and children never share a
socket); `--mode thread` for development and on Windows. See
[Workers & the supervisor](queue/workers-and-supervisor.md).

A management command is the least surprising way to run a worker in a Django project: job bodies
touch the ORM, so the process needs `django.setup()` to have happened, and this gets that plus
firm's configuration for free. The stock `firm-queue start` CLI still works against the same
database if you point `--import` at a module that calls `django.setup()` itself.

Recurring jobs go in `SupervisorConfig(recurring=[...])`, replacing whatever cron or
`django-celery-beat` you would otherwise run — see [Recurring tasks](queue/recurring.md).

## The Tasks backend (Django 6)

Django 6.0 ships `django.tasks`: `@task`, `Task.enqueue()`, `TaskResult`, and a `BaseTaskBackend`
contract. It defines an interface and executes nothing. firm implements that contract:

```python
# settings.py
TASKS = {
    "default": {
        "BACKEND": "firm.queue.contrib.django.backend.FirmBackend",
        "QUEUES": [],                                   # [] = any queue name; default ["default"]
        "OPTIONS": {"ENQUEUE_ON_COMMIT": True},
    }
}
```

```python
# myapp/tasks.py
from django.tasks import task

@task(queue_name="mailers", priority=10)
def send_welcome(user_id: int) -> None: ...
```

```python
from datetime import timedelta

from django.utils import timezone

send_welcome.enqueue(1)                     # a row in firm_queue_jobs
send_welcome.using(                         # a row in firm_queue_scheduled_executions
    run_after=timezone.now() + timedelta(hours=1)
).enqueue(1)
```

Every enqueued task becomes the same firm job — `firm.queue.contrib.django.backend.run_task`, carrying
the task's `module.qualname` and its arguments. That job has to be in the worker's registry too:
the enqueueing process imports the backend because Django builds it from `TASKS`, but a worker
never reads `TASKS`, and an unregistered `class_name` fails with `UnknownJob`. The app config
handles this for you — it imports the module of any `TASKS` backend under `firm.` at startup, so
there is nothing to declare. (Outside Django, where there is no `TASKS` setting to read:
`firm-queue start --import firm.queue.contrib.django.backend`.)

`@task` and `@bq.job` coexist in one table and are run by one worker. Nothing forces you to pick.

### `OPTIONS`

| Key | Default | Meaning |
|---|---|---|
| `ENQUEUE_ON_COMMIT` | `True` | Insert via `transaction.on_commit`. |
| `DATABASE_ALIAS` | `FIRM_QUEUE["DATABASE_ALIAS"]` | Which connection's commit that waits for. |

`ENQUEUE_ON_COMMIT` defaults the *opposite* way from a plain `@bq.job`, where deferring is opt-in
via `enqueue_on_commit`. The reason is the return value: a deferred `Job.enqueue()` cannot hand
back a `job_id`, while a `TaskResult` id is minted before the insert and costs nothing to defer.
Since Django runs `on_commit` callbacks immediately outside a transaction, leaving it on is free —
and inside `atomic()` it is the difference between correct and the two bugs above.

### What it supports, and what it doesn't

| Flag | Value | Why |
|---|---|---|
| `supports_defer` | yes | `run_after` becomes a scheduled execution; firm's dispatcher promotes it when due. |
| `supports_priority` | yes | Django orders descending (−100..100), firm ascending; the backend negates. |
| `supports_async_task` | yes | `run_task` goes through `Task.call()`, which runs a coroutine function under `async_to_sync`. |
| `supports_get_result` | **no** | firm's worker discards return values and keeps no row keyed by `TaskResult` id. |

The honest list of what you don't get:

- **No results.** `get_result()` / `aget_result()` raise `NotImplementedError`, and so does
  `TaskResult.refresh()`. Have the task write what you need where you can read it.
- **No `takes_context=True`.** It raises `InvalidTask`. A `TaskContext` carries the `TaskResult` —
  its id, its attempt count, its errors — and firm persists none of that; the context would be
  fiction.
- **No per-task retries.** firm looks a retry policy up in its *registry* when a job fails, not on
  the row that was enqueued, so an `ATTEMPTS` option could not reach it. A failed task lands in
  `firm_queue_failed_executions` and stays there. (`@bq.job(attempts=...)` is unaffected — this is
  a limit of the `django.tasks` route only.)
- **Only the `task_enqueued` signal.** `task_started` / `task_finished` are not sent: the firm
  worker knows nothing about Django, and a reconstructed `TaskResult` would carry invented
  `worker_ids` and attempt counts.
- **Under `TestCase`, `on_commit` never fires** — standard Django behaviour. Use
  `TransactionTestCase` or `captureOnCommitCallbacks`.

## The cache backend

`firm-cache` behind Django's own cache API, in the database you already have:

```python
CACHES = {
    "default": {
        "BACKEND": "firm.cache.contrib.django.FirmCache",
        "LOCATION": "",                 # empty: the database DATABASES["default"] names
        "TIMEOUT": 3600,                # cache-wide — read on
        "OPTIONS": {"MAX_SIZE": 512 * 1024 * 1024},
    }
}
```

```python
from django.core.cache import cache

cache.set("greeting", "hello")
cache.get_or_set(f"order:{order_id}", load_order)
```

Which database is said exactly once: an empty `LOCATION` derives it from
`DATABASES[OPTIONS["DATABASE_ALIAS"]]` (so the cache follows Django onto its test database), a
non-empty `LOCATION` is a SQLAlchemy URL, and `OPTIONS["ENGINE"]` hands over an engine you already
have. Saying it twice raises `ImproperlyConfigured`.

| `OPTIONS` | Meaning |
|---|---|
| `DATABASE_ALIAS` | Which `DATABASES` entry to derive the URL from (default `"default"`). |
| `ENGINE` | An `Engine`, a callable returning one, or a dotted path to either — see [below](#one-engine-for-every-module). |
| `CODER` | `"json"` (default), `"pickle"`, or a dotted path to a `Coder`. |
| `ENCRYPT_KEY` | Fernet key (or list, newest first) for at-rest encryption. |
| `ON_ENTRY_TIMEOUT` | `"error"` (default) or `"warn"` — what an unsupported per-call `timeout=` does. |
| `MAX_SIZE`, `MAX_ENTRIES`, `MAX_KEY_BYTESIZE`, `AUTO_EXPIRE`, `EXPIRY_BATCH_SIZE`, `BACKGROUND_EXPIRY`, `EXPIRY_INTERVAL`, `CREATE_SCHEMA` | Passed through to `Cache(...)`. |

### `TIMEOUT` is cache-wide, not per entry

This is the one thing to understand before using it. firm-cache has no expiry column: an entry is
alive for `max_age` seconds after it was written, and that number belongs to the *cache*, not to
the key. `TIMEOUT` maps onto `max_age`, so the default timeout is honoured exactly, entry by
entry — but a per-call `timeout=` asking for anything else cannot be, and **raises `ValueError`**
rather than quietly storing the value with a different lifetime. Two per-call values are exact and
always accepted: this cache's own `TIMEOUT`, and `0` ("expire immediately", which deletes the key).

That bites `cache_page(60)` and anything else that passes an explicit timeout. Give those their
own `CACHES` alias whose `TIMEOUT` *is* that value — pointed at its own database, because eviction
sweeps the whole `firm_cache_entries` table and two aliases with different timeouts on one database
would expire each other's entries. `OPTIONS={"ON_ENTRY_TIMEOUT": "warn"}` downgrades the exception
to a warning and writes with the cache-wide expiry.

Three further divergences from Django's cache contract, all from the same storage model:

- **A stored `None` reads back as a miss**, because `Cache.get` returns `None` for both. `has_key()`
  still answers `True`.
- **`clear()` empties the table**, not just this alias' `KEY_PREFIX` — like memcached's `flush_all`,
  and visible to every other firm-cache client on that database.
- **`MAX_ENTRIES` is unlimited by default**, not Django's 300 (a LocMem default that would quietly
  strangle a database cache), and `CULL_FREQUENCY` is accepted but ignored: eviction is FIFO once
  `MAX_SIZE` or `MAX_ENTRIES` is exceeded.

An omitted `TIMEOUT` means Django's default of 300 seconds, not firm-cache's two weeks. And note
that Django's own system checks instantiate every `CACHES` backend, so firm-cache's tables are
created on the first `manage.py` command, not at first use.

### One engine for every module

`firm-channel` and `firm-audit` ship a ready-made handle each. There is nothing to add to
`INSTALLED_APPS` — neither has startup work to do, so they follow the other Django idiom, the one
`django.core.cache.cache` uses: a module-level object that binds itself on first use.

```python
from firm.audit.contrib.django import audit
from firm.channel.contrib.django import channel

channel.broadcast("orders", b'{"order_id": 1}')
audit.record("invoice.paid", subject=invoice, actor=request.user)
```

Each handle derives its database from `DATABASES` exactly as the queue does, and **reuses the
queue's engine when it is pointed at the same database** — so a process that runs jobs, pub/sub and
an audit log holds one connection pool, not three. It also rebinds if that database changes
underneath it, which is what `manage.py test` does; a plain module-level `Channel(...)` built at
import would keep writing to the development database for the whole test run.

Settings live in `FIRM_CHANNEL` and `FIRM_AUDIT`, same shape as `FIRM_QUEUE` (`DATABASE_ALIAS`,
`DATABASE_URL`, plus that module's own options). For anything they don't expose, construct the
object yourself and pass `engine=`.

!!! warning "Audit background work belongs in one process"

    `AuditLog`'s retention, sealing and verification loops each start a thread **per process**.
    Four gunicorn workers would mean four schedulers competing over the same rows, so the handle
    doesn't expose them. Run them somewhere singular instead: a management command, a recurring
    firm job, or cron.

The cache backend takes the same engine through a dotted path, since Django builds it from
`CACHES`:

```python
# myapp/firm_engine.py
import firm.queue as bq

def engine():
    return bq.current_runtime().engine
```

```python
CACHES = {"default": {
    "BACKEND": "firm.cache.contrib.django.FirmCache",
    "OPTIONS": {"ENGINE": "myapp.firm_engine.engine"},
}}
```

The dotted path is resolved once, when Django first builds the cache — always after
`AppConfig.ready()` configured the queue, so the runtime exists. The trade-off is that the cache
then follows the queue's runtime rather than `DATABASES`; under `manage.py test` that is still the
test database (firm is re-pointed by `post_migrate`, before the test runner's system checks build
the cache), but don't build caches from `ready()` itself.

## Migrations

This is the question every Django reader asks, so here is the short version: **they don't
conflict, because they don't overlap.** Django's migration graph only ever contains models in
`INSTALLED_APPS`. firm's tables belong to no Django model, so `makemigrations` never sees them,
never proposes a migration for them, and never drops them. In the other direction, firm ships one
Alembic version table *per module* (`firm_queue_alembic_version`, `firm_cache_alembic_version`, …)
rather than the default `alembic_version`, so the modules don't overwrite each other's revision
stamps and neither touches `django_migrations`.

What's left is a practical question: what do you actually run?

### Option A — let `manage.py migrate` do both (recommended, and the default)

The app config connects a `post_migrate` receiver, so `manage.py migrate` provisions everything.
Two properties make that safe rather than a hack:

- **`create_all` is idempotent.** It creates only missing tables, so running it on every `migrate`
  costs nothing.
- **`create_all` also stamps.** After creating the tables it writes the current head revision into
  `firm_<module>_alembic_version`, exactly as `alembic stamp head` would. Without that, a later
  `alembic upgrade head` would try to re-run the baseline against tables that already exist.

You can verify the second point on a database provisioned this way — Alembic agrees it is already
at head, and upgrading is a clean no-op:

```console
$ FIRM_QUEUE_DATABASE_URL=postgresql://localhost/myapp alembic -c alembic.queue.ini current
0002 (head)

$ FIRM_QUEUE_DATABASE_URL=postgresql://localhost/myapp alembic -c alembic.queue.ini upgrade head
$   # nothing to do
```

`migrate --database other` is respected: the receiver compares the alias it was handed against
`FIRM_QUEUE["DATABASE_ALIAS"]` and does nothing for the others. `FIRM_QUEUE = {"CREATE_SCHEMA":
False}` turns the whole thing off. `Cache`, `Channel` and `AuditLog` create (and stamp) their own
tables on construction anyway, so this only ever concerns the queue.

### Option B — run Alembic yourself

If your deploy pipeline should own schema changes explicitly, set `CREATE_SCHEMA: False` and run
firm's migrations as their own step next to `manage.py migrate`. Order does not matter; both
orderings were verified to produce exactly one stamp row at head.

```bash
python manage.py migrate
FIRM_QUEUE_DATABASE_URL=postgresql://localhost/myapp alembic -c alembic.queue.ini upgrade head
```

One caveat that catches people: **the `alembic.*.ini` files only exist in a source checkout of
firm, not in the published wheels**, and their paths are relative to the repository root. If you
installed firm with pip — which you did — you have two choices: vendor a small `alembic.ini` of
your own pointing `script_location` at the installed `firm/queue/migrations` directory, or use
Option A, which needs no ini file at all. This is why Option A is the default for Django projects;
see [Database backends → Migrations](database-backends.md#migrations) and
[Deployment → Migrations](deployment.md#migrations) for the non-Django framing.

### What about `migrate --fake`, `flush`, and `sqlmigrate`?

- `makemigrations` / `sqlmigrate` — unaffected; firm's tables aren't models.
- `migrate myapp zero` — reverses Django's migrations only. firm's tables stay. Drop them yourself
  if you want a clean slate.
- `flush` — truncates tables Django knows about. **firm's tables are not truncated**, so queued
  jobs and cache entries survive a flush. Relevant mostly in tests; see below.

## Two connection pools

Django's ORM and firm both talk to the same database, over different client stacks:

| | Django | firm |
|---|---|---|
| Client | Django's own backend (`psycopg`, `mysqlclient`, `sqlite3`) | SQLAlchemy 2.0 Core |
| Pooling | `CONN_MAX_AGE` per-thread persistent connections (or `CONN_HEALTH_CHECKS` / an external pooler) | SQLAlchemy `QueuePool`, default `pool_size=20`, `max_overflow=40` |
| Transactions | `transaction.atomic()`, `ATOMIC_REQUESTS` | firm's own `engine.begin()` per operation |
| Lifecycle | opened/closed at request boundaries | long-lived, checked out per operation |

Three consequences worth planning for.

**Size your database's connection limit for the sum.** A Django web process with `CONN_MAX_AGE`
set holds a connection per thread; a firm worker process holds up to `pool_size + max_overflow`.
Tune firm's down when you run many worker processes:

```python
FIRM_QUEUE = {"POOL_SIZE": 5, "MAX_OVERFLOW": 10}
```

**Django's connections are closed after each job, for you.** Django closes ORM connections at
request boundaries; a firm worker thread has no requests, so a long-running worker would
accumulate connections that are never returned and never health-checked. The app config registers
an [`around_perform`](queue/workers-and-supervisor.md#per-job-middleware) middleware that calls
`close_old_connections()` after every job — including one that raised. Nothing to write in a job
body. To manage those connections yourself:

```python
FIRM_QUEUE = {"CLOSE_CONNECTIONS": False}
```

**Close Django's connections before forking.** `manage.py firm_worker --mode fork` already does
this. If you build a supervisor yourself, `connections.close_all()` before starting it — firm
drops its *own* inherited connections in each child, but knows nothing about Django's.

On SQLite there is a fourth consequence, already covered above: the two pools contend for one
write lock. Anything beyond development should be on PostgreSQL or MySQL.

## Testing

Three Django-specific rules, all consequences of firm using a second connection.

**Point the test database at a file.** Django's default SQLite test database is in memory and
unreachable from firm's connection (firm raises `ImproperlyConfigured` saying exactly that, rather
than silently connecting elsewhere):

```python
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "app.db",
        "TEST": {"NAME": BASE_DIR / "test-app.db"},
    }
}
```

**Nothing else to do about the test database.** `ready()` runs before it exists, but the
`post_migrate` receiver fires again once it does and re-points firm at it. Rebuilding is skipped
when the URL is unchanged, so the repeat `post_migrate` that `flush` emits between
`TransactionTestCase` methods doesn't leak a connection pool.

**Use `TransactionTestCase`, not `TestCase`.** `TestCase` wraps each test in a transaction it
never commits; firm's connection cannot see uncommitted rows, and on SQLite it can't write at all
while that transaction is open. `TransactionTestCase` commits for real — and it's also the only
class where `on_commit` callbacks fire without `captureOnCommitCallbacks`.

```python
from django.db import transaction
from django.test import TransactionTestCase

import firm.queue as bq
from firm.queue.contrib.django import enqueue_on_commit
from firm.queue.queues import clear
from firm.queue.worker import run_ready


class JobTest(TransactionTestCase):
    def setUp(self):
        clear(bq.current_runtime(), "billing")   # Django's flush skips firm's tables

    def test_body_directly(self):
        # @bq.job leaves the function callable — no queue involved at all
        charge_order(order.pk)

    def test_through_the_queue(self):
        with transaction.atomic():
            enqueue_on_commit(charge_order, order.pk)
        self.assertEqual(run_ready(bq.current_runtime(), limit=10), 1)
```

`run_ready()` claims and executes ready jobs once and returns, so tests never need a worker
process — including for `django.tasks` tasks, which are ordinary firm jobs once enqueued.

## firm vs. Django 6 Tasks, steady-queue, and django-tasks-db

Django 6.0 ships `django.tasks`: an *interface* for defining and enqueueing background work —
`@task`, `Task.enqueue()`, `TaskResult`, a `TASKS` setting, and a `BaseTaskBackend` contract. It
executes nothing. The two backends Django ships (`DummyBackend`, `ImmediateBackend`) are for
development and tests; anything real needs a third-party backend, and Django provides no worker.

firm implements that interface ([above](#the-tasks-backend-django-6)) without being built on it:
`@bq.job` remains the native API, and the Tasks backend is a translation layer on top of it.

| | Django 6 Tasks | django-tasks-db | steady-queue | firm |
|---|---|---|---|---|
| What it is | the interface | reference ORM backend | Solid Queue port on `django.tasks` | standalone queue + cache + pub/sub + audit |
| Runs jobs | no | yes | yes | yes |
| Job API | `@task` | `@task` | `@task` | `@bq.job`, or `@task` via the backend |
| Storage | backend-defined | Django ORM | Django ORM | SQLAlchemy, any of SQLite/PG/MySQL |
| Needs Django | yes | yes | yes | no |
| Recurring / cron | not defined | no | cron decorators | cron (5-field) |
| Result retrieval | `TaskResult` (backend opt-in) | `supports_get_result` | no | no |
| Dashboard | — | none | Django admin | `firm-ui` (standalone) |
| Cache / pub-sub / audit | — | — | — | yes, same database |

**Pick Django 6 Tasks + django-tasks-db** when you want the first-party path and minimal surface
area: a queue that is unambiguously "the Django way", and you'll solve cron, concurrency, and
monitoring elsewhere. It is the reference implementation of the interface, not a full job system —
its README documents no recurring tasks, no concurrency controls, and no admin integration, though
its backend does set `supports_get_result = True` and implements `get_result()`.

**Pick steady-queue** when you want Solid Queue's operational model *and* Django-nativeness:
worker/dispatcher/scheduler roles, `SKIP LOCKED` claiming, cron decorators, and a Django admin
integration that gives you pause/resume and bulk retry/discard without running another service.
For a Django-only application this is the closest match to firm, and being in-framework is a real
advantage.

**Pick firm** when the queue is infrastructure your Django app *shares* rather than owns —
non-Django producers or consumers on the same database — or when you want the cache, pub/sub, and
audit log to live in that same database too. You pay for it with the friction on this page: two
connection pools, `on_commit` instead of true transactional enqueue, and a `django.tasks` backend
that cannot return results.

**Where firm and steady-queue are equally weak:** neither retrieves job return values. If result
fetching in Django matters, that points at django-tasks-db.

## See also

- [`examples/django_app/`](../examples/django_app/) — the working project this page describes
- [Framework integration](contrib.md) — FastAPI, Flask and SQLAlchemy sessions
- [Deployment](deployment.md) — running workers in containers and Kubernetes
