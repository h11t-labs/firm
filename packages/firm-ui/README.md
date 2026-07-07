# firm-ui

Optional web dashboard for [firm](https://github.com/h11t-labs/firm) — watch and operate the
**queue**, **cache**, **channel** (pub/sub), and **audit** log in one place. A standard-library
HTTP server with Jinja2 templates; nothing else in firm imports it.

```bash
pip install firm-ui
```

## Run it

```bash
firm-ui --database-url sqlite:///app.db            # or set FIRM_DATABASE_URL
firm-ui --database-url postgresql://localhost/app --host 127.0.0.1 --port 8787
```

A tab appears for each firm part whose tables exist in the database you point it at. Parts in
separate databases? Pass `--queue-url`, `--cache-url`, `--channel-url`, `--audit-url`
individually.

## Highlights

- **Queue** — job counts per state, per-queue size/latency with pause/resume, live workers,
  recurring schedules, job detail with traceback and retry/discard
- **Cache** — entry count, estimated size, recent entries, clear-all
- **Channels** — buffered messages, busiest channels, trim
- **Audit** — searchable event feed
- **Auth built in** — HTTP Basic (plain or hashed password), reverse-proxy header auth, or a
  custom authenticator; refuses non-loopback binds without auth

## Docs

- [Dashboard guide](https://github.com/h11t-labs/firm/blob/main/docs/ui.md)
- [All firm documentation](https://github.com/h11t-labs/firm#readme)

MIT licensed; see [NOTICE](https://github.com/h11t-labs/firm/blob/main/NOTICE) for third-party
notices.
