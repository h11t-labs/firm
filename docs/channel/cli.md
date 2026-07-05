# CLI

The `firm-channel` command operates a pub/sub database. Pass the URL with `--database-url` or
the `FIRM_CHANNEL_DATABASE_URL` env var.

```bash
firm-channel --help
```

## Commands

### `stats` — number of buffered messages

```bash
firm-channel stats --database-url postgresql://localhost/myapp
# messages: 312
```

### `trim` — delete old messages and exit

Deletes a batch of messages older than the retention window and exits. Useful as a cron job, or if
you run with `autotrim=False`.

```bash
firm-channel trim --retention 86400 --batch-size 100
# trimmed 100 messages
```

> **Tip:** set `FIRM_CHANNEL_DATABASE_URL` in your environment to omit `--database-url`.
