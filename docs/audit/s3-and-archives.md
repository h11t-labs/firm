# S3 backend and archival storage

`firm-audit` supports its existing SQL backend and an optional S3 primary backend. S3 mode
runs in stateless Kubernetes replicas against an existing bucket, without a SQL database,
persistent volume, leader process, or exporter. Archival is a separate optional capability for
both SQL and S3 sources.

Install `pip install "firm-audit[s3]"` (or `"firm-stack[audit-s3]"`). Default SQL imports do not load
boto3. The SDK uses its normal credential chain, including workload identity; do not embed
credentials in application code. Configure the bucket yourself. Opening the log does not create
buckets, configure Object Lock, change lifecycle, or delete events.

## Database-free recording

Choose a stable stream UUID for a logical log and share it across application replicas.
Independent or diverging logs need distinct streams. Prefixes isolate environments.

```python
from firm.audit.s3 import S3AuditLog

log = S3AuditLog(
    "my-audit-bucket",
    stream="00000000-0000-0000-0000-000000000001",
    prefix="production/audit",
    region="eu-west-1",
)
receipt = log.record("invoice.paid", subject=("Invoice", 42), data={"amount": 12})
event = log.get(receipt)
log.close()
```

Each event is an immutable, versioned JSON envelope in one object. A successful return means
S3 acknowledged the write or an identical prior write was verified. References and tagged
payload types have the same meaning as SQL audit events. Receipts contain key, SHA-256 digest,
and version ID where the provider supplies one. Reading a receipt pins/verifies that version.
A plain key reads the current object.

S3 writes cannot join a SQL transaction: passing `conn=` fails explicitly. The existing
`AuditLog` and module-level `record(conn, ...)` retain their SQL transaction guarantees, integer
IDs, and indexed history. A standalone S3 write can succeed independently of application work.

### Durable retries

Prepare an event once and retain **both its key and body** before writing. Reuse the same
prepared envelope on every retry, including from another pod. The body must retain the exact
canonical bytes emitted by `prepare()`; reformatting its inner JSON is rejected before writing.
Serializing the outer retry token is safe. Recreating it with a new
timestamp, partition, or ID is a new event, even if its action/data look the same.

```python
from dataclasses import asdict
import json

from firm.audit.s3 import PreparedEvent, S3AuditLog, UnknownEventOutcome

log = S3AuditLog("my-audit-bucket", stream="00000000-0000-0000-0000-000000000001")
prepared = log.prepare("invoice.paid", subject=("Invoice", 42))
retry_token = json.dumps(asdict(prepared))  # retain in the application's durable request state
try:
    receipt = log.write(prepared)
except UnknownEventOutcome as exc:
    retry_token = json.dumps(asdict(exc.prepared))
# A later process can reconstruct precisely the same write:
prepared_again = PreparedEvent(**json.loads(retry_token))
log.close()
```

Conditional creation prevents overwriting an existing key. An identical retry converges on
its receipt; different content at that key raises `ObjectConflict`. A timeout that cannot be
reconciled raises `UnknownEventOutcome` with `.prepared`. Permission/configuration errors and
outages surface to the caller. SDK retry attempts and network timeouts are bounded.

If a pod dies before acknowledgement and also loses the prepared envelope, the library cannot
recover that request. There is no hidden local spool. Deleting an event (or creating a delete
marker) removes the guarantee of deduplication at that key; indefinite dedupe after retention
is not supported.

## Bounded history

S3 provides object/prefix access rather than SQL indexes. `scan()` traverses UTC date partitions
in ascending object-key order, applying scalar filters as it reads candidates. It exposes
scan progress rather than presenting an incomplete SQL-style history list.

```python
from datetime import date
from firm.audit.s3 import S3AuditLog

log = S3AuditLog("my-audit-bucket", stream="00000000-0000-0000-0000-000000000001")
page = log.scan(
    start=date(2026, 1, 1), end=date(2026, 1, 2),  # end is exclusive
    action="invoice.paid", limit=25, max_objects=100, max_requests=150,
)
# page.items, page.scanned, page.requests, page.exhausted, page.continuation
log.close()
```

Pass `page.continuation` with the same dates/filters/stream/destination to resume. Budgets and
result limit can change between calls. Object traversal is not global creation/commit order.
Concurrent or backdated writes inserted behind a cursor may require a fresh scan. Scans are
not snapshot queries or reliable tailing. Dates select the prepared event's timestamp partition;
backdated events must be scanned in their original date range or retrieved by receipt.

Ranges are limited to 366 days, result limits to 1,000, object budgets to 10,000, and logical
request budgets to 20,000 per call. Request counts are library GET/LIST operations; SDK retries
can make actual service requests higher. A filtered scan can return zero results while still
having a continuation. Follow continuations to establish traversal exhaustion. No global
counts, arbitrary column sorting, secondary index service, or transparent SQL history parity
are claimed. Measure filtered-query costs against your actual volumes before rollout.

## Copy-only archival

Use a separate bucket or prefix, with its own permissions and retention. Archives contain
bounded compressed JSON Lines batches plus version-aware completion manifests, checksums,
counts, source identities, event time bounds, original timestamps, and tagged payloads.

Publication freezes input first, writes data next, verifies it, and publishes the manifest last.
Readers accept only complete manifests. Interrupted publication can resume using the same
batch ID. Staged input is retained for recovery and counts toward storage cost; it can contain
uncompressed payloads. Source objects/rows are untouched by copy-only export.

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

`export_sql` copies a bounded selection; optional `since`/`until` constrain timestamps. It
is not a full database backup. `archive.inspect(receipt)["scope"]` exposes `continuation` and
`traversal_exhausted`; pass the continuation with a new batch ID to copy the next bounded
selection. ID traversal can miss concurrent lower-ID commits behind its cursor, so pause
independent pruning/quiesce the source when a complete historical snapshot is required. Reusing a batch ID resumes its frozen selection and rejects a
changed query. An empty selection raises `ValueError` and publishes no batch.

`export_s3(source, start=..., end=..., batch_id=..., continuation=..., max_objects=100)` copies
one bounded observed selection. The manifest's `scope.continuation` resumes source traversal
in the next batch. Concurrent LIST pagination is not a completeness guarantee for a changing
partition. S3 source compaction/deletion is deliberately deferred; originals remain. This mode
never requires a database or PVC.

Each batch holds 1–1,000 events and up to 16 MiB decompressed data. Individual S3 objects are
limited to 16 MiB. Choose smaller batches for large events; SQL continuous export additionally
limits frozen input to 8 MiB. Overlapping exports may contain identical source events. `archive.read_many(receipts, max_events=10000)` and SQL restore deduplicate by identity and
reject conflicts. Multi-manifest reads accept at most 100 manifests and an explicit event budget. SQL source identity includes a row
fingerprint because SQLite can reuse an integer ID after all rows are pruned.

### Restore and protection

`archive.resume(batch_id)` resumes durable staged input. `archive.read(receipt_or_manifest_key)`
verifies an archive. A receipt pins/verifies the manifest version; plain keys use the current
manifest. Missing/corrupt data fails visibly. Cold storage requiring a provider restore operation
raises `ObjectUnavailable` rather than silently returning an empty archive.

`archive.batches(limit=25, continuation=None)` returns one page of batches as
`(summaries, continuation)`: batch ID, manifest key, status (`complete`, `pending` for staged
input without a manifest, or `unreadable`), event count, and event time range. It costs one LIST
plus one manifest GET per published batch and verifies nothing; pages follow batch-ID order,
not creation order. Open a batch with `read()` to verify it.

```python
from firm.audit import AuditLog
from firm.audit.archive import Archive
from firm.audit.object_store import ObjectStore
from firm.audit.sql_archive import restore_sql

archive = Archive(ObjectStore("my-archive-bucket"))
with AuditLog("sqlite:///isolated-restored.db") as target:
    # Only use an isolated empty target, with no concurrent writers.
    restored_count = restore_sql(archive, ["firm-audit/archives/v1/BATCH_UUID/manifest.json"], target)
archive.close()
```

SQL restore preserves original integer IDs/timestamps and reconciles generated-ID state.
It accepts one SQL source stream and an empty target. Batches from separate incarnations of
reused SQL IDs must be restored separately. Merge into nonempty targets is not supported. S3-to-SQL restore assigns destination integer
IDs and retains original provenance in `schema.archive_imports`; query it with
`import_provenance(conn, event_id)`. SQL-to-S3 restore assigns deterministic UUIDs and stores
original provenance in the event envelope. Typed payloads and timestamps are retained in both
directions. S3-source archives restore through
`archive.restore_s3(receipt, target_s3_log)`, preserving the original stream and event identity;
retries are idempotent, and conflicts fail rather than overwrite.

Object Lock/versioning, legal holds, encryption, and lifecycle rules are optional deployment
policies. Protect manifests as well as data. Batch together only events with compatible hold
and deletion requirements; one event cannot be removed from an immutable batch. Hashes detect
corruption but are not signatures. Lifecycle expiry is asynchronous; do not treat it as an
exact TTL or a substitute for verified archival. Same-account/bucket copies are not independent
backups, and asynchronous archives do not protect unexported events lost with the primary.

## Continuous SQL export with archive-before-prune

Copy-only SQL export also supports the pre-archive `0002` schema. Upgrade to audit migration
`0003` before using `SQLArchiver` or restoring into a SQL target. Auto-create also creates archival
tables; Alembic upgrades existing `0002` installations without modifying audit event columns.
**Upgrade every pruning process and pause pruning during policy activation/backfill.** Old
library versions do not honor the new persisted guard.

```python
from firm.audit import AuditLog
from firm.audit.archive import Archive
from firm.audit.object_store import ObjectStore
from firm.audit.sql_archive import SQLArchiver

with AuditLog("sqlite:///audit.db", max_age=30 * 86400) as audit:
    archive = Archive(ObjectStore("my-archive-bucket"))
    worker = SQLArchiver(audit, archive, stream="00000000-0000-0000-0000-000000000001")
    worker.enable()  # explicit, persisted archive-before-prune policy; call once during cutover
    receipt = worker.run_once(batch_size=100)
    status = worker.status()  # events, unarchived, pending_batches; scans the SQL source
    pruned_count = audit.retention.run_once()
    archive.close()
```

`enable()` permanently binds the source to this stream/destination and makes all upgraded
pruners require exact verified membership. It has no public disable/switch operation. Copies
produced by `export_sql` do not satisfy this policy: the continuous exporter needs its durable
membership ledger. Application `AuditLog.record()` and module-level `record(conn, ...)` need
no exporter callback. Discovery selects committed, unreserved rows without a maximum-ID cursor,
so late-committing lower IDs are included.

Short SQL transactions freeze membership and input; all S3 work happens outside row locks.
Competing workers can publish the same immutable pending batch. They do not reassign or modify
membership, so a stale worker can only retry the same content. A crash after manifest publication
but before acknowledgement reconciles that batch on the next run. Delivery is at least once
with idempotent publication, not a distributed transaction.

Acknowledgement clears frozen payloads from SQL while retaining receipts. Pruning deletes only
verified eligible rows older than `max_age`, and removes their live membership references.
Unexported/failed rows remain. Monitor backlog and SQL capacity during S3 outages; there is no
silent dropping: after each run `audit.retention.last_blocked_unarchived` reports how many expired
rows were kept because they are not yet in a verified archive batch, and above 1,000 the run
routes an "archiver looks stalled" alert through `on_error`, like the unsealed-row alert.
Eligibility costs one query per pruning batch or seal range. Independent deletion/expiry of the
archive invalidates the guard's storage assumption. Archive retention must cover your total
required retention period.

## CLI and dashboard

S3 commands:

```bash
firm-audit s3 --bucket events --stream STREAM_UUID prepare invoice.paid --event-file retry.json
firm-audit s3 --bucket events --stream STREAM_UUID write retry.json
firm-audit s3 --bucket events --stream STREAM_UUID get OBJECT_KEY
firm-audit s3 --bucket events --stream STREAM_UUID scan --start 2026-01-01 --end 2026-01-02
```

Archive commands:

```bash
firm-audit archive --bucket cold export-sql --database-url sqlite:///audit.db --stream STREAM_UUID --batch-id BATCH_UUID
firm-audit archive --bucket cold resume BATCH_UUID
firm-audit archive --bucket cold read MANIFEST_KEY
firm-audit archive --bucket cold inspect MANIFEST_KEY
firm-audit archive --bucket cold read-many MANIFEST_KEY OTHER_MANIFEST_KEY
firm-audit archive --bucket cold restore-sql MANIFEST_KEY --target-url sqlite:///isolated.db
firm-audit archive --bucket cold run-sql --database-url sqlite:///audit.db --stream STREAM_UUID --enable-prune-guard
```

`inspect` exposes the next export continuation; `export-sql`/`export-s3` accept `--continuation`.
`export-s3` and `restore-s3` accept explicit source/target bucket and prefix. `read`/`get`
accept `--sha256` and `--version-id` to verify/pin receipt versions. Store group flags precede
the subcommand: `--bucket`, `--prefix`, `--region`, `--endpoint-url`. `run-sql` exports one batch;
repeat/schedule it to drain backlog. Activation is explicit via `--enable-prune-guard`.

For an S3 dashboard, use `firm-ui --audit-s3-bucket events --audit-s3-stream STREAM_UUID`.
Optional `--audit-s3-prefix`, `--audit-s3-region`, and `--audit-s3-endpoint` select the store.
The S3 view shows bounded date/filter scans (the "To" date is inclusive) and event details, with
continuation and scan counts, under a source strip naming the bucket, prefix, and stream. It
does not display misleading global counts or unsupported sorting. `--audit-archive-bucket cold`
adds the archive view: it lists the archive's batches (with pending and unreadable ones
flagged) and verifies a batch when you open it or paste its manifest key. `--audit-archive-prefix`
defaults to `firm-audit`, the same as `ObjectStore` and `firm-audit archive`.

SQL and S3 audit logs can be shown together: pass `--database-url` (or `--audit-url`) next to
the S3 flags and the audit tab gets a Database / S3 / Archive switcher. With only one view
configured there is no switcher and the tab looks as it always has.

Programmatic `build_dashboard(audit_s3=log, audit_archive=archive)` accepts these capabilities,
alongside any SQL URLs. It owns their lifecycle when closed. Authentication requirements are the
same as for existing dashboards.

## Kubernetes and provider validation

Application replicas need only SDK credentials and the bucket/stream/prefix configuration.
Use workload identity and least-privilege prefix-scoped permissions: primary writers need
conditional PUT and reconciliation GET; readers need GET/GET-version and LIST; exporters need
read access to the source plus PUT/GET to their archive destination. No delete or provisioning
permission is required by normal recording/export. A CronJob can call `run-sql` or bounded
copy-only exports; archival is not required in primary S3 mode. Persist retry envelopes in
application request state, never only on the pod filesystem.

The provider must support general-purpose-bucket style lexicographic LIST/StartAfter, conditional
creation, and compatible error handling. Directory-bucket/Express traversal is not supported.
Version IDs, Object Lock, credentials, and checksums must be verified per provider. AWS S3
capabilities are documented in the primary sources linked in the [research note](s3-research.md);
compatibility of another endpoint must be tested, not inferred from its URL.

Opt-in tests use `FIRM_TEST_S3_BUCKET` and optionally `FIRM_TEST_S3_ENDPOINT`, SDK credentials,
and `FIRM_TEST_PG_URL`/`FIRM_TEST_MYSQL_URL`. They isolate and clean their own object prefix in
the supplied **test** bucket. Mock failure tests exercise unknown outcomes and interrupted
publication; real-provider tests exercise conditional writes, pagination, versions, export,
and restore. Actual workload latency/cost, Kubernetes rollout behavior, and production IAM/
Object Lock policies need deployment-specific validation before rollout.

### SQL integrity compatibility

SQL archives preserve the stored JSON bytes and row signature columns (`entry_id`, `row_mac`,
`key_id`). Native SQL restores retain those columns so row MACs remain verifiable with the
original key. Cross-backend conversions retain original signature metadata in provenance;
they do not create a new SQL signature. Seals, activation/floor records, verification status,
and external anchors belong to the original database and are not included in event archives.
An isolated restore therefore does not recreate the original sealed history or anchor chain.
Use a separate database/anchor backup when that evidence must be recovered.

With SQL sealing active, archive-before-prune applies to complete verified seal ranges,
including any pre-activation rows covered by deletion. One unarchived row blocks retirement
of its whole range. This keeps both the archive policy and signed-floor invariants intact.
S3 primary storage does not implement SQL row signing, sealing, or external anchor verification.
Its receipt digests detect corruption; they are not authentication signatures.
