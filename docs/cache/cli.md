# CLI

The `firm-cache` command operates a cache database. Pass the URL with `--database-url` or the
`FIRM_CACHE_DATABASE_URL` env var.

```bash
firm-cache --help
```

## Commands

### `stats` — entry count and estimated size

```bash
firm-cache stats --database-url postgresql://localhost/myapp
# entries: 1240
# estimated_size: 5012345 bytes
```

### `clear` — delete every entry

```bash
firm-cache clear --database-url sqlite:///cache.db
# cleared
```

### `trim` — run one eviction pass

Evicts a batch according to the cache's `max_age`/`max_size`/`max_entries` limits and exits. Useful
as a cron job if you run with `auto_expire=False`.

```bash
firm-cache trim
# evicted 100 entries
```

> **Tip:** set `FIRM_CACHE_DATABASE_URL` in your environment to omit `--database-url`.
