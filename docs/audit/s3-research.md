# S3 backend: primary-source research

Researched 10 October 2026. Scope: Amazon S3 general-purpose buckets unless stated otherwise. This note records service capabilities and architectural implications; it is not an implementation or a benchmark. Implementation fit must also be checked against firm's existing APIs.

## Assessment

The implementation is limited to **firm-audit**, with both an optional S3 primary backend
alongside SQL and archival storage; [S3 & archives](s3-and-archives.md) documents its supported
contract. The original recommendations and other-module comparison below are historical research
context; they do not expand implementation scope. The AWS capability findings remain supporting
evidence.

S3 is a reasonable destination for immutable audit archives. A database-free audit writer is also possible, but would need a deliberately narrower contract for queries, ordering, transactions, and retention. A transparent replacement for the SQL backend across audit, queue, cache, and channel would require building substantial database and coordination machinery. Start with audit archival and keep SQL as the operational source of truth.

This conclusion is architectural inference from the following documented capabilities, not a claim that object storage is incapable of hosting databases.

## Capabilities that matter

| Capability | Verified service behavior | Implication for firm |
| --- | --- | --- |
| Consistency | Successful object writes are visible to subsequent GET and LIST operations; individual-key updates are atomic. S3 does not provide atomic updates across multiple keys. | Do not dismiss S3 as eventually consistent. The obstacle is coordinating multiple records, indexes, and SQL application transactions. [S3 consistency model](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Welcome.html#ConsistencyModel) |
| Conditional writes | `If-None-Match: *` prevents replacement of a current object; `If-Match` checks its ETag. Conflicts produce 412 and some races produce 409/404. In versioned buckets, a current delete marker permits a new conditional creation. | Stable event or batch keys can support idempotent publication. CAS enables custom coordination, but is not a multi-object transaction. Handle ambiguous timeouts by reconciling the same key and payload, not generating a new identity. [Conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html) |
| Listing | `ListObjectsV2` returns at most 1,000 entries per request, offers prefix/cursor selection, and orders general-purpose bucket keys lexicographically; directory buckets lack this ordering guarantee. | Actor/action/resource searches need object reads, an explicit index, or an external query engine. Key order does not itself establish database commit order. [ListObjectsV2](https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListObjectsV2.html) |
| Querying | S3 Select is unavailable to new customers and only queries a single object at a time for existing customers. | Do not make S3 Select the basis of a new audit query API. [S3 Select](https://docs.aws.amazon.com/AmazonS3/latest/userguide/selecting-content-from-objects.html) |
| Object Lock | Versioning is required. Locks protect individual versions; new versions and delete markers remain possible. Governance retention can be bypassed with the relevant permission; compliance retention cannot be shortened while active. | Useful archive protection, but use restricted writer permissions and immutable naming too. Keep returned version IDs and explicit content digests. Ordinary key lookup alone is insufficient proof that a locked version is the one being read. [Object Lock](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html) |
| Lifecycle | Expiration removes unversioned objects asynchronously, potentially after their expiration date; versioned expiry can create a delete marker. A locked version cannot be deleted by lifecycle expiration. | Lifecycle is a separate operational policy, not application-controlled transactional pruning or a cache's exact read-time TTL. Audit deletion must remain explicit opt-in. [Expiration behavior](https://docs.aws.amazon.com/AmazonS3/latest/userguide/lifecycle-expire-general-considerations.html), [Object Lock considerations](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock-managing.html) |
| Event notifications | Notifications are at least once and may arrive out of order. | Notifications can wake consumers; they do not supply queue claim/ack/retry semantics or a globally ordered channel. [Event notifications](https://docs.aws.amazon.com/AmazonS3/latest/userguide/notification-how-to-event-types-and-destinations.html) |
| Append | Direct append is available specifically for S3 Express One Zone directory buckets, with up to 10,000 parts before resetting via copy. Each append is billed as a PUT. | General-purpose archive design should publish complete immutable objects, not assume a portable append-to-file operation. [Append support](https://docs.aws.amazon.com/AmazonS3/latest/userguide/directory-buckets-objects-append.html) |

## Cost model and batching

Storage capacity is only one cost component. PUT, GET, LIST, lifecycle transitions, retrieval, and transfer can all matter. Small objects incur a minimum billable size of 128 KB in Standard-IA/One Zone-IA; these classes have a 30-day minimum duration. Glacier classes add other duration/size/metadata considerations. Use actual region, class, encryption, and workload assumptions before quoting a monthly price. [AWS S3 pricing](https://aws.amazon.com/s3/pricing/)

S3 Lifecycle currently prevents objects smaller than 128 KB from transitioning by default; AWS cautions that their per-object transition charges can exceed storage savings. Batching audit records is therefore worth measuring. [Lifecycle transition constraints](https://docs.aws.amazon.com/AmazonS3/latest/userguide/lifecycle-transition-general-considerations.html)

Illustrative request counts, not a price estimate: one million events stored one per object require one million initial PUTs; batches of 1,000 need about 1,000 data PUTs, plus manifests/retries. One LIST per second consumes 2,592,000 requests per 30-day month per poller. Batching trades latency and recovery complexity for fewer requests. Durable SQL staging avoids acknowledging unflushed in-memory events.

## Suggested first design

These are proposed engineering choices, not AWS guarantees:

1. Add an optional audit export/archive facility, preserving the SQL append/query API. Select a bounded immutable set of audit records and persist the exact batch membership before upload. Do not assume an auto-increment ID is a commit-ordered watermark: concurrent transactions can commit out of order.
2. Serialize a versioned, lossless envelope into a compressed immutable batch; preserve all typed payloads, source identity, and event IDs. Use deterministic batch keys, a strong content digest, and conditional creation.
3. After a successful or reconciled upload, persist its receipt: key, version ID when available, digest, and exact exported membership. Crash after PUT but before receipt must resume safely. Never mark success before the remote write is durable.
4. Keep archive export and SQL prune separate by default. If later adding archive-then-prune, delete only the exact verified members under the owning audit API, with explicit retention consent and recovery tests. Avoid a broad timestamp cutoff that could delete late arrivals absent from the batch.
5. Support bounded restore/verification before promising transparent queries across SQL and S3. Keep optional SDK dependencies out of default imports; accept supplied clients/configuration and normal credential-provider chains.

For a later S3-only writer, explicitly decide whether SQL transaction integration, sequential integer IDs, globally ordered cursors, atomic batches, arbitrary filtering, live counts, and dashboard queries are supported. Unknown-length list pagination is not automatically a stable snapshot under concurrent writes. Avoid promising parity before an executable contract and adversarial tests exist.

## Other modules

| Module | Sensible S3 role | Assessment of direct replacement |
| --- | --- | --- |
| Audit | Immutable archives; later a restricted append-only writer | Best candidate, with explicit query and transaction limitations. |
| Cache | Large immutable value blobs with SQL metadata, if measured workloads justify it | Small hot values, counters, conditional mutation, expiry and eviction need additional machinery. |
| Queue | Large immutable input/output payloads, retaining SQL job state | Claims, leases, retry scheduling and multi-record transitions make direct replacement a poor first investment. |
| Channel | Historical export/replay material if useful | Live cursor ordering, polling and retention make direct replacement unattractive. |
| Core/UI | Shared optional object client only after a second real use case; SQL-backed UI initially | S3 is not another SQLAlchemy dialect. A genuine backend-independent UI would need query interfaces first. |

## S3 Tables and compatibility

S3 Tables uses Apache Iceberg in table buckets and is optimized for analytical workloads, with querying through Iceberg-compatible engines such as Athena, Redshift and Spark. It could be a later analytics export target; it introduces a separate table/catalog/query stack, not a drop-in SQLAlchemy operational database. [S3 Tables](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables.html), [Accessing table data](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables-access.html)

The promises above apply to AWS S3. An S3-compatible endpoint needs its own documented and tested capability matrix: conditional writes, consistency, versioning, Object Lock, checksum handling, list ordering/pagination and error semantics. Endpoint compatibility alone establishes none of these guarantees. Before a release, test timeout-after-success, competing writers, overlapping exporters, delayed SQL commits, corruption, recovery, locked-object retention and real provider round-trips. No provider round-trip or performance benchmark was performed in this research.
