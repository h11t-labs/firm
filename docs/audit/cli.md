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

With sealing enabled, `prune` deletes only rows in ranges already covered by a seal, and prints the
count of expired-but-**unsealed** rows it had to skip — a nonzero skip count means the sealer is
behind. See [Tamper-evidence › Retention and checkpoints](tamper-evidence.md#retention-and-checkpoints).

### `seal` — run the seal loop (tamper-evidence)

Seals settled rows into the chain (Layer 2), then exports the chain head to the anchor (Layer 3).
Requires `FIRM_AUDIT_KEY`. Run it on a timer (cron, or `background_sealing=True` in-process); it is
idempotent and safe to run on more than one host at once — the loser of a race simply retries.

```bash
firm-audit seal --database-url sqlite:///audit.db
# sealed 3 blocks (seq 41..43), 2841 rows
```

### `verify` — check the audit trail for tampering

Recomputes row MACs, walks the seal chain, and (with `--anchor`) checks the external anchor. Reads
the key from `FIRM_AUDIT_KEY`, or a labelled keyring from `FIRM_AUDIT_KEYS` for rotation. Read-only.

```bash
firm-audit verify --database-url sqlite:///audit.db --anchor /var/lib/firm/audit.anchor
# OK · 12040 rows · 43 seals · full coverage 2 h ago (cycle 3/7) · anchor 41 s old · tail 6 rows
```

Options: `--anchor PATH` (also enforces anchor freshness), `--from-seq N` (start the seal walk at
a given `seq`), `--full` (re-read every sealed range from genesis rather than the rolling slice).

**Exit codes:** `0` for `OK`/`UNPROTECTED`/`WARNING`; **non-zero** for any `TAMPERED` finding, and
non-zero when `--anchor` is given but the newest anchor is older than `anchor_max_age`. This makes
`verify` a usable cron/CI gate. Verdict classes and the anchor-age rule are described in
[Tamper-evidence › Verifying](tamper-evidence.md#verifying).

> **Tip:** set `FIRM_AUDIT_DATABASE_URL` in your environment to omit `--database-url`.
