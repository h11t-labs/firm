# firm-audit

Append-only audit log for Python. Store events in **SQLite**, **PostgreSQL**, or
**MySQL/MariaDB**, or use optional **S3 storage** without an audit database or persistent volume.
One `AuditLog` shares event preparation, signing, sealing, verification and retention across
both storage backends.

Part of [firm](https://github.com/h11t-labs/firm), a port of the Rails 8 Solid stack — the
audit module is original to firm (it has no Rails counterpart).

```bash
pip install firm-audit          # SQL storage
pip install "firm-audit[s3]"     # S3 storage or archives
```

## Quickstart

```python
from firm.audit import AuditLog

audit = AuditLog(database_url="postgresql://localhost/myapp")

audit.record("user.login", subject=("User", 42), actor=("User", 42), data={"ip": "10.0.0.1"})

for event in audit.history(subject=("User", 42)):
    print(event["action"], event["created_at"])
```

## S3 storage and archives

```python
from firm.audit import AuditLog, S3Storage

with AuditLog(
    storage=S3Storage("audit-events", stream="00000000-0000-0000-0000-000000000001")
) as audit:
    audit.record("user.login", actor=("User", 42))
```

Provide an existing bucket and SDK credentials; replicas share the stream UUID. Optional
`Archive` and `Archiver` copy events from either backend into verified batches. Opt-in retention
can require archival before deletion. Schedule the archiver separately; it does not delete
source events itself. See [S3 storage and archives](https://github.com/h11t-labs/firm/blob/main/docs/audit/s3-and-archives.md).

## Highlights

- **Append-only** — no update or delete API on recorded events
- **Structured querying** with `history()` — filter by action, subject, actor, time range
- **Opt-in retention** — nothing is trimmed unless you ask for it
- **Opt-in tamper-evidence** — HMAC-signed rows, independent range seals, and a read-only `verify`
  that detects modification, deletion, insertion, or truncation by a keyless attacker

## Docs

- [Audit overview](https://github.com/h11t-labs/firm/blob/main/docs/audit/overview.md)
- [S3 storage and archives](https://github.com/h11t-labs/firm/blob/main/docs/audit/s3-and-archives.md)
- [Tamper-evidence](https://github.com/h11t-labs/firm/blob/main/docs/audit/tamper-evidence.md)
- [All firm documentation](https://github.com/h11t-labs/firm#readme)

MIT licensed; see [NOTICE](https://github.com/h11t-labs/firm/blob/main/NOTICE) for third-party
notices.
