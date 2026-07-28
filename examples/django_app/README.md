# Django + firm

A minimal Django project on SQLite: one model, one job, one `django.tasks` task, and no glue
code. Django's ORM owns `demo_order`; firm owns the `firm_queue_*` / `firm_cache_*` /
`firm_channel_*` tables in the same database. No Docker, no Redis.

Everything is wired by three settings entries — `"firm.queue.contrib.django"` in `INSTALLED_APPS`, a
`CACHES` backend, and a `TASKS` backend. There is no `apps.py`, no URL-derivation helper, and no
worker command in this project: they come with `firm-queue[django]`.

The long-form answers — Alembic vs. Django migrations, transaction semantics, the two connection
pools, and how this compares to Django 6 Tasks / steady-queue — are in
[docs/django.md](../../docs/django.md).

## Run it

```bash
pip install "firm-queue[django]" "firm-cache[django]" firm-channel   # Django 6.0 for TASKS
cd examples/django_app

python manage.py migrate           # Django's tables and firm's, in one command
python manage.py runserver         # terminal 1
python manage.py firm_worker       # terminal 2
```

```bash
curl -X POST localhost:8000/orders/ -d "email=ada@example.com&amount_cents=1234"
# {"order_id": 1, "queued": true}

curl localhost:8000/orders/1/
# {"id": 1, "email": "ada@example.com", "charged": true}     ...once the worker has run
```

Tests need no worker at all:

```bash
python manage.py test demo
```

## What's where

| File | Why it's interesting |
|---|---|
| [settings.py](settings.py) | the whole integration: `INSTALLED_APPS`, `CACHES`, `TASKS`, and one `FIRM_QUEUE` key |
| [demo/jobs.py](demo/jobs.py) | a `@bq.job` using the Django ORM, invalidating the cache, and broadcasting on a channel — autodiscovered, so no import wiring |
| [demo/tasks.py](demo/tasks.py) | the same queue through Django 6's `@task` |
| [demo/views.py](demo/views.py) | `enqueue_on_commit(...)` — the pattern that matters — and `django.core.cache` on firm-cache |
| [demo/tests.py](demo/tests.py) | why `TransactionTestCase`, and three ways to test a job |

## Three things that will bite you

1. **Enqueue with `enqueue_on_commit`, not bare.** firm writes on its own connection, so a bare
   `enqueue()` inside `transaction.atomic()` is not part of your transaction. On PostgreSQL/MySQL
   the job survives a rollback and points at a row that never existed; on SQLite it deadlocks
   against Django's write lock and raises `database is locked`. (The `TASKS` backend does this
   for you: `ENQUEUE_ON_COMMIT` defaults to `True`.)
2. **`TransactionTestCase`, not `TestCase`.** `TestCase` never commits, so firm's connection
   cannot see the rows your test made.
3. **Point `TEST['NAME']` at a file.** Django's default in-memory SQLite test database is not
   reachable from firm's separate connection.

Each is explained in [docs/django.md](../../docs/django.md).
