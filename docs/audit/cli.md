# CLI

The `firm-audit` command operates an audit database. Pass the URL with `--database-url` or the
`FIRM_AUDIT_DATABASE_URL` env var.

```bash
firm-audit --help
```

## Commands

### `stats` — total event count

```bash
firm-audit stats --database-url postgresql://localhost/myapp
# events: 1240
```

### `history` — list recent events

```bash
firm-audit history --database-url sqlite:///audit.db --action invoice.paid --limit 10
# 2026-06-30 12:00:00  invoice.paid  subject=Invoice:42  actor=User:7 (alice@example.com)
```

References render as `Type:id`, with a display name in parentheses when one was recorded; a
role/label actor (no id) shows just its type, and an absent actor/subject shows `-`:

```
2026-06-30 12:00:05  sync.ran         subject=-            actor=cron
2026-06-30 12:00:04  system.boot      subject=-            actor=-
2026-06-30 12:00:00  invoice.paid     subject=Invoice:42   actor=User:7 (alice@example.com)
```

Filters: `--action`, `--subject-type`, `--subject-id`, `--actor-type`, `--actor-id`,
`--correlation-id`, `--limit` (default 20). Each is independent — use any one alone, any
combination, or all of them together.

```bash
firm-audit history --database-url sqlite:///audit.db --actor-type model   # everything a model actor did
firm-audit history --database-url sqlite:///audit.db --actor-type cron    # a role/label actor
```

### `prune` — delete events older than `max_age`

A no-op unless you pass `--max-age` (seconds) — pruning is opt-in, matching the library's
keep-forever default. See [Retention & querying](retention-and-querying.md).

```bash
firm-audit prune --database-url sqlite:///audit.db --max-age 7776000   # 90 days
# pruned 12 events
```

> **Tip:** set `FIRM_AUDIT_DATABASE_URL` in your environment to omit `--database-url`.
