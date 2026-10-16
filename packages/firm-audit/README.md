# firm-audit

Database-backed, append-only audit log for Python. No Redis required: events are recorded in
your existing **SQLite**, **PostgreSQL**, or **MySQL/MariaDB** database.

Part of [firm](https://github.com/h11t-labs/firm), a port of the Rails 8 Solid stack — the
audit module is original to firm (it has no Rails counterpart).

```bash
pip install firm-audit
```

## Quickstart

```python
from firm.audit import AuditLog

audit = AuditLog(database_url="postgresql://localhost/myapp")

audit.record("user.login", subject=("User", 42), actor=("User", 42), data={"ip": "10.0.0.1"})

for event in audit.history(subject=("User", 42)):
    print(event["action"], event["created_at"])
```

## Highlights

- **Append-only** — no update or delete API on recorded events
- **Structured querying** with `history()` — filter by action, subject, actor, time range
- **Opt-in retention** — nothing is trimmed unless you ask for it
- **Opt-in tamper-evidence** — HMAC-signed rows, independent range seals, and a read-only `verify`
  that detects modification, deletion, insertion, or truncation by a keyless attacker

## Docs

- [Audit overview](https://github.com/h11t-labs/firm/blob/main/docs/audit/overview.md)
- [Tamper-evidence](https://github.com/h11t-labs/firm/blob/main/docs/audit/tamper-evidence.md)
- [All firm documentation](https://github.com/h11t-labs/firm#readme)

MIT licensed; see [NOTICE](https://github.com/h11t-labs/firm/blob/main/NOTICE) for third-party
notices.
