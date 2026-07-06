# Getting started

## Install

```bash
pip install "firm[queue]"            # or: uv add "firm[queue]"
# add a database driver as needed (SQLite needs nothing):
pip install "firm[queue,postgres]"   # psycopg
pip install "firm[queue,mysql]"      # PyMySQL
```

See **[Installation](../installation.md)** for the full list of extras.

## 1. Configure

Point the library at a database. This is process-global; call it once at startup.

```python
import firm.queue as bq

bq.configure(database_url="sqlite:///queue.db")
# or "postgresql://localhost/myapp", or "mysql://user:pw@localhost/myapp"
```

## 2. Create the schema

For development, create the tables directly:

```python
from firm.queue import schema
from firm.queue import current_runtime

schema.create_all(current_runtime().engine)
```

For production, use the bundled Alembic migration instead (`alembic upgrade head`) — see
[Database backends](../database-backends.md#migrations).

## 3. Define a job

`@bq.job` turns a plain function into something you can enqueue. It stays directly callable, so you
can unit-test the body without a database.

```python
@bq.job(queue="mailers", priority=5)
def send_welcome(user_id: int, locale: str = "en") -> None:
    user = load_user(user_id)
    deliver_welcome_email(user, locale)
```

## 4. Enqueue

```python
from datetime import datetime, timedelta

send_welcome.enqueue(42)                          # run as soon as a worker is free
send_welcome.enqueue(42, locale="fr")             # keyword args work too
send_welcome.enqueue_in(timedelta(hours=1), 42)   # run later
send_welcome.enqueue_at(datetime(2026, 7, 1, 9, 0), 42)  # run at a specific time (naive UTC)
```

Arguments are stored as JSON. Plain JSON types plus `datetime`, `date`, `Decimal`, and `UUID`
round-trip; anything else raises **at enqueue time** (see [Defining jobs](jobs.md)).

## 5. Run it

### Quick, in-process (great for a test)

```python
from firm.queue.worker import run_ready

send_welcome.enqueue(42)
processed = run_ready(current_runtime())   # claim + run up to `limit` (default 10) ready jobs inline
```

### The real thing — a worker process

```bash
firm-queue start \
  --database-url sqlite:///queue.db \
  --import myapp.jobs            # import the module(s) that define your @job functions
```

`start` runs a supervisor with a worker (thread pool) **and** a dispatcher (so scheduled jobs fire).
Press `Ctrl-C` for a graceful drain. See the [CLI](cli.md) and
[Workers & the supervisor](workers-and-supervisor.md) for all the options.

## Full example

```python
# myapp/jobs.py
from datetime import timedelta
import firm.queue as bq

bq.configure(database_url="sqlite:///queue.db")

_sent: list[int] = []

@bq.job()
def greet(user_id: int) -> None:
    _sent.append(user_id)

if __name__ == "__main__":
    from firm.queue import schema
    from firm.queue import current_runtime
    from firm.queue.worker import run_ready

    schema.create_all(current_runtime().engine)
    greet.enqueue(1)
    greet.enqueue(2)
    print("processed:", run_ready(current_runtime()))   # -> processed: 2
    print("sent:", _sent)                                # -> sent: [1, 2]
```
