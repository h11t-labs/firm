# S3 storage and archives

`AuditLog` owns event preparation, signing, sealing, verification, retention and background
loops. Select its persistence with `storage=SqlStorage(...)` or `storage=S3Storage(...)`;
the same audit implementation runs above either adapter. SQL remains the default, so existing
`AuditLog(database_url)` and `AuditLog(engine=...)` calls keep working. S3 needs an existing
bucket, without an audit database or persistent volume. The
[tamper-evidence](tamper-evidence.md) page applies to either storage. Archival is a separate,
optional capability for either.

```python
from firm.audit import AuditLog, SqlStorage, S3Storage

sql = AuditLog(storage=SqlStorage(database_url="sqlite:///audit.db"))
s3 = AuditLog(
    storage=S3Storage(
        "my-audit-bucket",
        stream="00000000-0000-0000-0000-000000000001",
    )
)
# The application uses the same methods and integrity options with either storage.
sql.close()
s3.close()
```

Configure bucket/endpoint/stream and lease settings on `S3Storage`; configure keys, grace,
sealing, verification, retention and outbox relay settings on `AuditLog`. The log closes its
adapter on exit, including any owned engine, SDK client or worker pool. An explicitly injected
SQLAlchemy engine or SDK client stays caller-owned. Configure `create_schema` on `SqlStorage`
when supplying it explicitly. The earlier `firm.audit.s3.S3AuditLog(...)` constructor remains a
compatibility shorthand for this composition; it contains no separate audit algorithms.

Install `pip install "firm-audit[s3]"` (or `"firm-stack[audit-s3]"`). Default SQL imports do not
load boto3. The SDK uses its normal credential chain, including workload identity; do not embed
credentials in application code. Configure the bucket yourself: opening the log does not create
buckets, configure versioning or Object Lock, or change lifecycle rules.

## What S3 changes

Three things differ from SQL, and each is a deliberate consequence of having no database:

- **Ids come from one counter per stream.** Every event takes the next integer id with one
  compare-and-swap (`If-Match`) on a small counter object, so ids are sequential like a SQL
  primary key. The counter bounds write throughput per stream (one round trip per event, with
  retries under contention); use several streams for independent high-volume writers.
- **Maintenance runs under one lease.** Sealing, retention and verification take an exclusive
  lease object (compare-and-swap with a time-to-live) instead of SQL transactions and row locks.
  Recording never needs it. A sealer or pruner that finds the lease busy skips that round;
  verification waits for it.
- **`record(conn=...)` uses an outbox.** S3 cannot join your SQL transaction. With `conn`, the
  event is staged in a `firm_audit_s3_outbox` table in *your* database inside your transaction
  and published afterwards: committed means it will appear, rolled back means it never does.

Choose a stable stream UUID per logical log and share it across replicas; prefixes isolate
environments.

## Recording and history

```python
from firm.audit import AuditLog, S3Storage

log = AuditLog(
    storage=S3Storage(
        "my-audit-bucket",
        stream="00000000-0000-0000-0000-000000000001",
        prefix="production/audit",
        region="eu-west-1",
    )
)
log.record("invoice.paid", subject=("Invoice", 42), actor=("User", 7), data={"amount": 12})
events = log.history(subject=("Invoice", 42))
event = log.get(events[0]["id"])
log.close()
```

`history()` takes the same filters as `AuditLog.history()` (subject/actor in either form,
action, correlation id, `since`, `limit`) and returns the same dicts, newest first. Each filter
reads its own index objects, so a filtered query reads only matching rows; `since` is exact even
for events published late through the outbox. `get(id)` reads one event; `get(receipt)` also
checks the stored digest.

### Durable retries

With S3 storage, `record()` returns a receipt. SQL keeps its existing `None` return value. For a
write that must survive a crash or a timeout, prepare the event once, keep its body in your request state, and write it — again and again if needed, from
any process. Every write of the same body lands on the same event and id.

```python
from dataclasses import asdict
import json
from firm.audit import AuditLog, S3Storage, PreparedEvent, UnknownEventOutcome

log = AuditLog(storage=S3Storage("my-audit-bucket", stream="00000000-0000-0000-0000-000000000001"))
prepared = log.prepare("invoice.paid", subject=("Invoice", 42))
retry_token = json.dumps(asdict(prepared))
try:
    receipt = log.write(prepared)
except UnknownEventOutcome as exc:
    retry_token = json.dumps(asdict(exc.prepared))
prepared_again = PreparedEvent(**json.loads(retry_token))
log.close()
```

The body must keep the exact bytes `prepare()` produced; a reformatted body is rejected. The
event's signature and timestamp are fixed at prepare time; its id is taken when it is first
written. A timeout or outage raises `UnknownEventOutcome` carrying the prepared event;
permission and configuration errors surface as they are. If a writer dies after claiming an id
but before writing the event, a retry within `grace` finishes that id; a later retry takes a
fresh one (the old id stays a gap, like a rolled-back SQL insert).

### Recording inside your transaction

```python
from sqlalchemy import create_engine
from firm.audit import AuditLog, S3Storage
from firm.audit.storage.s3.outbox import create_outbox

engine = create_engine("sqlite:///app.db")
create_outbox(engine)
log = AuditLog(storage=S3Storage("my-audit-bucket", stream="00000000-0000-0000-0000-000000000001"))
with engine.begin() as conn:
    log.record("order.created", subject=("Order", 1), conn=conn)
published = log.flush_outbox(engine)
log.close()
```

`flush_outbox(engine)` publishes staged events oldest first and deletes each outbox row after
its write; pass `outbox_engine=engine` to the constructor to run that relay in the background
(`outbox_interval`, default 5 s). Publication is at least once and idempotent, so concurrent
relays are safe. `firm-audit s3 ... flush-outbox --database-url ...` does the same from a cron.

## Tamper-evidence, retention and verification

Everything on the [tamper-evidence](tamper-evidence.md) page works the same way on S3:
`mac_key`/`FIRM_AUDIT_KEY` signs every row, sealing writes independent range seals, the
anchor records coverage outside the storage, `verify()` reports the same findings, and
retention retires whole verified seal ranges behind a signed floor.

```python
from firm.audit import AuditLog, S3Storage

log = AuditLog(
    storage=S3Storage("my-audit-bucket", stream="00000000-0000-0000-0000-000000000001"),
    mac_key="a-32+char-row-secret-................",
    max_age=90 * 86400,
    background_sealing=True,
    background_verification=True,
)
report = log.verify(full=True)
pruned = log.retention.run_once()
log.close()
```

Rows are stored with the SQL columns verbatim (entry id, stored JSON, timestamp, row MAC, key
id), so a row MAC is the same recipe as in SQL. The sealer's `grace` assumption carries over:
a write must finish within `grace` of taking its id, or a sealed range may already cover it.
Retiring a range touches many objects, so the floor advance is recorded as an intent first
and finished by whichever process next holds the lease; a crash mid-way never leaves a false
TAMPERED behind.

Two things an attacker with write access to the bucket can do that SQL's constraints would stop:
write an event at an existing entry id (reported as a replay by the same duplicate check SQL
has), or edit an index object, which can hide an event from `history()`. Verification never
reads indexes: it reads every event object directly. Deleting objects is detected exactly as in
SQL (seals, and an anchor for whole ranges); Object Lock and versioning prevent it.

## Storage layout and costs

Everything lives under `{prefix}/audit/v2/{stream}/` in your bucket:

| Objects | Holds |
| --- | --- |
| `seq.json` | the id counter |
| `ev/{id}-{entry}.json` | one event row each |
| `ix/...` | index entries: by id, by created-at (both directions), one per filter value, unsigned rows, each write's id claim |
| `seal/...` | activation, seal and floor records |
| `status.json`, `lease.json`, `prune.json` | verify status, the maintenance lease, a floor advance in progress |
| `archive/...` | the archive-before-prune ledger, when enabled |

Request costs, so you can size them against your volumes:

- **Recording:** about 10–12 requests per event (counter read and swap, id claim, the event
  object, and one index object per indexed value).
- **History:** one LIST per 1,000 index entries plus one GET per row read. A filter with its own
  index reads only matching rows.
- **Dashboard totals:** one LIST per 1,000 events (keys only). The default `created_at`/`id`
  sorts stream from an index; any other sort, or more than one filter, reads the newest 10,000
  matching rows and sorts those.
- **Sealing:** one GET per row sealed. **Full verification:** one GET per event, read in
  parallel (`workers`, default 16). Incremental verification reads the unsealed tail plus its
  share of sealed ranges, like SQL.

## Copy-only archival

Use a separate bucket or prefix, with its own permissions and retention. Archives contain
bounded compressed JSON Lines batches plus version-aware completion manifests, checksums,
counts, source identities, event time bounds, original timestamps, tagged payloads, and each
row's signature.

Publication freezes input first, writes data next, verifies it, and publishes the manifest last.
Readers accept only complete manifests. Interrupted publication can resume using the same
batch ID. Staged input is retained for recovery and counts toward storage cost; it can contain
uncompressed payloads. Copy-only export leaves the source untouched.

```python
from uuid import uuid4
from firm.audit import AuditLog
from firm.audit.archive import Archive
from firm.audit.object_store import ObjectStore

archive = Archive(ObjectStore("my-archive-bucket", prefix="production/audit-cold"))
with AuditLog("sqlite:///audit.db") as audit:
    receipt = archive.export_sql(
        audit, stream="00000000-0000-0000-0000-000000000001",
        batch_id=str(uuid4()), limit=100,
    )
entries = archive.read(receipt)  # also verifies every referenced object
archive.close()
```

`export_sql` and `export_s3(source, batch_id=..., limit=100, since=None, until=None,
continuation=None)` copy a bounded selection by id, in the same entry shape (id plus a
fingerprint of the exact row, and its signature). They are not full backups.
`archive.inspect(receipt)["scope"]` exposes `continuation` and `traversal_exhausted`; pass the
continuation with a new batch ID to copy the next selection. Id traversal can miss a
late-committing lower id behind its cursor, so pause pruning when a complete historical snapshot
is required. Reusing a batch ID resumes its frozen selection and rejects a changed query; an
empty selection raises `ValueError`.

Each batch holds 1–1,000 events and up to 16 MiB decompressed. Overlapping exports may contain
identical events; `archive.read_many(receipts, max_events=10000)` and the restores deduplicate
by identity and reject conflicts.

### Reading, restoring, and protecting archives

`archive.resume(batch_id)` resumes durable staged input. `archive.read(receipt_or_manifest_key)`
verifies an archive; a receipt pins the manifest version. Missing or corrupt data fails visibly,
and cold storage that needs a provider restore raises `ObjectUnavailable`.

`archive.batches(limit=25, continuation=None)` returns one page of batches as
`(summaries, continuation)`: batch ID, manifest key, status (`complete`, `pending` for staged
input without a manifest, or `unreadable`), event count, event time range, and how many events
carry a row signature (`signed`, with `sources`). Manifests record that summary and `read()`
rejects one that disagrees with its data. It costs one LIST plus one manifest GET per batch and
verifies nothing; pages follow batch-ID order, not creation order.

```python
from firm.audit import AuditLog
from firm.audit.archive import Archive
from firm.audit.object_store import ObjectStore
from firm.audit.storage.sql.archive import restore_sql

archive = Archive(ObjectStore("my-archive-bucket"))
with AuditLog("sqlite:///isolated-restored.db") as target:
    # Only use an isolated empty target, with no concurrent writers.
    restored_count = restore_sql(archive, ["firm-audit/archives/v1/BATCH_UUID/manifest.json"], target)
archive.close()
```

Both restores require an empty target. `restore_sql` into SQL and `archive.restore_s3(receipts,
target_s3_log)` into S3 keep original ids, timestamps and row signatures when the archive comes
from the same kind of storage (and, for S3, the same stream), so rows still verify under the
original key. A cross-source or cross-stream restore into S3 gets fresh ids, uses the target's
configured row key when signing is enabled, and retains the source identity as provenance.
A restore from S3 into SQL currently gets fresh SQL ids and preserves provenance but does **not**
re-sign the imported rows; configuring a key on the SQL target does not change this restore path.
Read provenance through `import_provenance(conn, id)` in SQL or `log.import_provenance(id)` on S3.
Seals, floors, verify status and anchors belong to the original log and are not archived; keep
a separate backup when that evidence must be recoverable.

Object Lock, versioning, legal holds, encryption, and lifecycle rules are deployment policies.
Protect manifests as well as data. Batch together only events with compatible hold and deletion
requirements. Lifecycle expiry is asynchronous; do not treat it as an exact TTL or a substitute
for verified archival. Same-account copies are not independent backups.

## Archive-before-prune

`Archiver` continuously archives an `AuditLog` with either storage and makes retention wait until
each expired row sits in a verified archive batch. (`SQLArchiver` is the same class.) The
archiver itself never deletes events and archives all unreserved events, including recent ones;
`max_age` controls the separate retention step.

```python
from firm.audit.archive import Archive
from firm.audit.object_store import ObjectStore
from firm.audit import AuditLog, S3Storage
from firm.audit.archive import Archiver

with AuditLog(
    storage=S3Storage("my-audit-bucket", stream="00000000-0000-0000-0000-000000000001"),
    max_age=30 * 86400,
) as log:
    archive = Archive(ObjectStore("my-archive-bucket"))
    worker = Archiver(log, archive)
    worker.enable()
    receipt = worker.run_once(batch_size=100)
    status = worker.status()
    pruned_count = log.retention.run_once()
    archive.close()
```

One `run_once()` does the following:

1. Under the source's transaction or S3 lease, resume a pending batch or select up to
   `batch_size` unreserved events (default 100, maximum 1,000). Freeze their exact payloads,
   identities and row signatures, and persist batch membership with content fingerprints.
2. Release that source scope and publish `input.json`, then `data.jsonl.gz`, then
   `manifest.json` under one batch UUID. Verify stored payload hashes before completing the
   manifest. Retrying a batch uses the same frozen input and conditional object writes.
3. Read the completed batch back and compare it with the reserved input. Only then acknowledge
   the batch in the source's ledger. A crash before acknowledgement leaves a pending batch
   that a later run resumes; it does not make those events prunable.
4. A separate retention run checks age, exact content fingerprints and acknowledged membership.
   With signing enabled it also applies the usual seal verification and floor rules.

Schedule `run_once()` yourself, for example with a Kubernetes CronJob or a worker loop. It
returns `None` when there is no work. There is no automatic background archiver on `AuditLog`:
`background_retention` starts only pruning, and `background_sealing` starts only sealing.
The CLI commands `archive run-sql` and `archive run-s3` each process one batch per invocation;
size the schedule or drain loop to exceed the event arrival rate. `status()` reports current
source events, unarchived events and pending batches.

Archival verification here means checksums, envelope structure and comparison with the source
batch. It preserves row MACs but does **not** authenticate them with signing keys. Use the audit
verifier on a restored log to validate row MACs, with the required original keys. Seals and
anchors are not part of an event archive.

The pruning guard consults the source ledger; it does not download every archived batch again
before deletion. Protect and retain acknowledged archive objects separately: deleting an archive
later does not revoke its recorded acknowledgement.

For a SQL log pass `stream=`, and upgrade to audit migration `0003` first (auto-create includes
it). **Upgrade every pruning process and pause pruning during activation and backfill.**
`enable()` permanently binds the log to one stream and destination. Copies made with
`export_sql`/`export_s3` do not satisfy the policy: the archiver keeps its own membership
ledger (SQL tables, or objects under `archive/` on S3). Discovery selects every unreserved row,
so late-committing lower ids are included. Delivery is at least once with idempotent
publication. After each run `log.retention.last_blocked_unarchived` reports how many expired
rows the guard kept, and above 1,000 retention routes an "archiver looks stalled" alert through
`on_error`. With sealing active, one unarchived row holds back its whole seal range.

## CLI and dashboard

`firm-audit s3` has the SQL commands' S3 counterparts, plus the write and outbox commands:

```bash
firm-audit s3 --bucket events --stream STREAM_UUID stats
firm-audit s3 --bucket events --stream STREAM_UUID history --subject-type Invoice --limit 20
firm-audit s3 --bucket events --stream STREAM_UUID seal
firm-audit s3 --bucket events --stream STREAM_UUID verify --full
firm-audit s3 --bucket events --stream STREAM_UUID prune
firm-audit s3 --bucket events --stream STREAM_UUID prepare invoice.paid --event-file retry.json
firm-audit s3 --bucket events --stream STREAM_UUID write retry.json
firm-audit s3 --bucket events --stream STREAM_UUID get 42
firm-audit s3 --bucket events --stream STREAM_UUID flush-outbox --database-url sqlite:///app.db
```

Archive commands:

```bash
firm-audit archive --bucket cold export-sql --database-url sqlite:///audit.db --stream STREAM_UUID --batch-id BATCH_UUID
firm-audit archive --bucket cold export-s3 --source-bucket events --stream STREAM_UUID --batch-id BATCH_UUID
firm-audit archive --bucket cold run-sql --database-url sqlite:///audit.db --stream STREAM_UUID --enable-prune-guard
firm-audit archive --bucket cold run-s3 --source-bucket events --stream STREAM_UUID --enable-prune-guard
firm-audit archive --bucket cold read MANIFEST_KEY
firm-audit archive --bucket cold inspect MANIFEST_KEY
firm-audit archive --bucket cold restore-sql MANIFEST_KEY --target-url sqlite:///isolated.db
firm-audit archive --bucket cold restore-s3 MANIFEST_KEY --target-bucket restored --stream STREAM_UUID
```

Store group flags precede the subcommand: `--bucket`, `--prefix`, `--region`, `--endpoint-url`.
`run-sql`/`run-s3` export one batch; schedule them to drain a backlog.

For the dashboard, `firm-ui --audit-s3-bucket events --audit-s3-stream STREAM_UUID` (with
optional `--audit-s3-prefix`, `--audit-s3-region`, `--audit-s3-endpoint`) shows the S3 log on
the same audit page as SQL — cards, filters, sorting, pagination, integrity strip and per-row
status — at `/audit/s3`, under a source strip naming the bucket, prefix and stream.
`--audit-archive-bucket cold` adds the archive view: a page of batches (pending and unreadable
ones flagged, signed rows per batch) and, per batch, the verified events with their recorded
signatures. `--audit-archive-prefix` defaults to `firm-audit`, the same as `ObjectStore`. A SQL
and an S3 log can be shown together: with more than one of Database, S3 and Archive configured,
the audit tab gets a switcher. `build_dashboard(audit_s3=log, audit_archive=archive)` does the
same programmatically, alongside any SQL URLs.

## Kubernetes and provider validation

Replicas need SDK credentials, the bucket/stream/prefix configuration, and `FIRM_AUDIT_KEY` from
a secret store when rows are signed. Use workload identity and prefix-scoped permissions:
recording needs GET and PUT on the stream prefix; history, sealing, verification and the
dashboard also need LIST; only the process that runs retention needs DELETE; exporters need read
access to the source plus PUT, GET and LIST on the archive destination. Persist retry tokens in application request state,
never only on the pod filesystem.

The provider must support lexicographic LIST with `StartAfter`, conditional creation
(`If-None-Match: *`) and compare-and-swap (`If-Match`) on PutObject, with S3-compatible error
codes. Directory buckets (Express One Zone) are not supported. See AWS's
[conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)
and [ListObjectsV2](https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListObjectsV2.html)
documentation for these capabilities; verify any other endpoint by testing, not by its URL.
CI runs the live tests against RustFS.

Opt-in tests use `FIRM_TEST_S3_BUCKET` and optionally `FIRM_TEST_S3_ENDPOINT`, SDK credentials,
and `FIRM_TEST_PG_URL`/`FIRM_TEST_MYSQL_URL` (setup in
[testing and contributing](../testing-and-contributing.md)). They isolate and clean their own
prefix in the supplied **test** bucket. Workload latency and cost, rollout behaviour, and
production IAM and Object Lock policies need deployment-specific validation.
