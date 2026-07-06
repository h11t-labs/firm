# Database backends

Both packages run on **SQLite**, **PostgreSQL**, and **MySQL/MariaDB**. Pick with the
`database_url` you pass to `bq.configure(...)` or `Cache(...)`.

## Drivers

| Database        | URL                                    | Driver                                     | Install       |
|-----------------|----------------------------------------|--------------------------------------------|---------------|
| SQLite          | `sqlite:///path.db`                    | stdlib `sqlite3`                           | (built in)    |
| PostgreSQL      | `postgresql+psycopg://user:pw@host/db` | [psycopg 3](https://www.psycopg.org/)      | `…[postgres]` |
| MySQL / MariaDB | `mysql+pymysql://user:pw@host/db`      | [PyMySQL](https://pymysql.readthedocs.io/) | `…[mysql]`    |

```bash
pip install "firm[queue,postgres]"   # queue on PostgreSQL  (or [queue,mysql])
pip install "firm[cache,postgres]"   # cache on PostgreSQL  (or [cache,mysql])
```

**Bare URLs are normalized for you:** `postgresql://…` becomes `postgresql+psycopg://…` and
`mysql://…` becomes `mysql+pymysql://…`, so you don't have to remember the driver suffix. An
explicit `+driver` is always respected.

## How concurrency stays correct on each

This is the one place the databases genuinely differ, and it's fully abstracted (see
[queue internals](queue/internals.md)):

| Database           | Claim transaction            | Row locking                    |
|--------------------|------------------------------|--------------------------------|
| PostgreSQL / MySQL | ordinary transaction         | `FOR UPDATE SKIP LOCKED`       |
| SQLite             | `BEGIN IMMEDIATE` write lock | not needed (writers serialize) |

The same guarantee — two workers never claim the same job, two writers never lose a cache update —
holds on all three.

## Choosing one

- **SQLite** — zero setup, perfect for development, tests, single-process apps, and low-to-moderate
  volume on one host. Its one writer at a time means write-heavy, multi-worker, multi-host
  deployments will serialize on the write lock. WAL + `busy_timeout` keep that smooth up to a point.
  On Kubernetes/shared storage it needs special care — see [SQLite on Kubernetes](#sqlite-on-kubernetes-and-other-shared-network-storage).
- **PostgreSQL** — the recommended default for production and anything multi-host: real
  `SKIP LOCKED`, no global writer lock, scales horizontally.
- **MySQL / MariaDB** — equally supported; same `SKIP LOCKED` path. MariaDB 10.6+ supports
  `SKIP LOCKED`.

## SQLite on Kubernetes (and other shared/network storage)

SQLite on firm relies on two OS-level mechanisms for correctness: **WAL mode** (which uses a
sidecar `-wal` file plus a `-shm` shared-memory file) and **byte-range file locks** on the database
file. The job-claim path leans on this directly — on SQLite, `BEGIN IMMEDIATE` takes the single
write lock so two workers never claim the same job. Both mechanisms assume **one host, one local
filesystem**. Container orchestrators quietly violate that assumption, so read this before reaching
for a `PersistentVolumeClaim`.

**The core problem:** SQLite's file locking is unreliable on network filesystems, and WAL is worse —
the `-shm` file coordinates processes through shared memory that only works when they are on the same
host. The SQLite project states outright that WAL "does not work over a network filesystem." What
this means for the common PVC storage classes:

- **NFS-backed / `ReadWriteMany` volumes** (EFS, Filestore, most `nfs` classes) — `fcntl` locks over
  NFS are racy, and WAL's `-shm` coordination isn't there at all. Expect `database is locked`
  errors, silently lost writes, or outright **corruption**. `busy_timeout` (default 5000 ms) masks
  contention on a local disk but cannot fix broken locking semantics. **Avoid.**
- **Block-based / `ReadWriteOnce` volumes** (EBS, GCE PD, Ceph RBD, most default classes) — these are
  real block devices with a normal filesystem, so SQLite + WAL works **correctly, but only from one
  pod**. The volume can only be mounted read-write by a single node at a time.

**Multiple pods sharing one SQLite database is not supported.** `BEGIN IMMEDIATE` only serializes
claimers that see the same file with working locks — which, per the above, they can't do safely over
a shared PVC. A `ReadWriteOnce` PVC won't even let two pods on different nodes mount it. This is a
fundamental SQLite limitation, not a firm bug.

| Deployment shape                       | Works? | Notes                                                                                                   |
|----------------------------------------|:------:|---------------------------------------------------------------------------------------------------------|
| 1 pod, `ReadWriteOnce` block PVC       |   ✅   | Correct SQLite + WAL. Fine for dev, low volume, single-tenant.                                           |
| 1 pod, NFS / `ReadWriteMany` PVC       |   ⚠️   | Locking may misbehave; corruption possible even with one writer. Avoid.                                 |
| 2+ pods, shared PVC                    |   ❌   | Broken locking (NFS) or can't mount (RWO). The no-double-claim guarantee does not hold.                 |
| Any multi-replica / HA setup           |   ❌   | Use PostgreSQL.                                                                                          |

**If you must run SQLite in Kubernetes,** treat it as a single-writer appliance:

- Use a **`StatefulSet` with a single replica** and a block-backed (`ReadWriteOnce`) volume, not a
  `Deployment`.
- Set the update strategy so a rolling update never runs two pods against the volume at once —
  `Recreate` (Deployment) or the `StatefulSet` default `OnDelete`/rolling behavior with
  `replicas: 1`. Overlapping pods during a deploy is the most common way people corrupt the file.
- Keep the `-wal` and `-shm` sidecar files on the **same volume** as the `.db` file (they will be, as
  long as the whole directory lives on the PVC).

**For anything multi-pod, HA, or that you expect to scale — use PostgreSQL.** Point firm at it with
the same `database_url` you'd pass for SQLite; the dialect layer switches to real
`FOR UPDATE SKIP LOCKED` and workers scale horizontally across pods with no shared-filesystem
requirement. This is the recommended production path.

## MySQL specifics

The schema is tuned so MySQL behaves like the others:

- `value` (cache) and large text columns map to **`LONGBLOB`/`LONGTEXT`** — plain `BLOB`/`TEXT` would
  silently cap at 64 KiB.
- Timestamps use **`DATETIME(6)`** for sub-second precision (needed for correct ordering and the
  recurring `(task_key, run_at)` dedupe).

## Migrations

Each package ships an Alembic baseline migration that creates its schema. Point Alembic at the
package's `alembic.ini` (or pass the URL via env / `-x`):

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
```

Each package keeps its own Alembic version table (`firm_<module>_alembic_version`), so all four
can migrate one shared database independently.

For development you can skip Alembic and call `schema.create_all(engine)` (queue) or let
`Cache(create_schema=True)` (the default) build the table. Either way the schema stays
upgradable: auto-creation also stamps the package's version table at the migration head, so a
later `alembic upgrade` picks up from the baseline instead of trying to re-run it.

## What's tested

The full test suite runs against **SQLite, PostgreSQL, and MySQL/MariaDB** — the same correctness
specs (no-double-claim under contention, concurrency block/promote, dispatch, recovery, cache
upsert/increment/eviction) execute on every backend. See
[Testing & contributing](testing-and-contributing.md).
