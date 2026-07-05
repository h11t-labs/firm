# Dashboard (UI)

`firm-ui` is a small, **optional** web dashboard for inspecting and operating a firm
database — the **queue**, the **cache**, the **channel** (pub/sub) store, and the **audit** log.
It's a stdlib HTTP server (Jinja2 for templates), and nothing else in firm imports it — it's a
pure add-on you can ignore entirely.

## Run it

```bash
firm-ui --database-url sqlite:///app.db            # or set FIRM_DATABASE_URL
firm-ui --database-url postgresql://localhost/app --host 127.0.0.1 --port 8787
python -m firm.ui --database-url sqlite:///app.db  # equivalent
```

A tab appears for each part whose table exists in the database you point it at, so one
`--database-url` lights up whatever's present. If your parts live in **separate databases**, give
each its own URL (any omitted one falls back to `--database-url`):

```bash
firm-ui --queue-url postgresql://db/jobs \
              --cache-url postgresql://db/cache \
              --channel-url postgresql://db/cable \
              --audit-url postgresql://db/audit
```

It points at existing databases — it never creates or migrates a schema.

## What you get

- **Queue** — job counts per state (ready / scheduled / blocked / claimed / failed / finished),
  per-queue size + latency with **pause/resume**, live worker/dispatcher processes (with a
  stale-heartbeat badge), and recurring schedules; jobs-by-state lists; and a job detail page with
  arguments, the full traceback, and **retry** / **discard** (plus retry-all). Auto-refreshes.
- **Cache** — entry count, estimated total size, recent entries, and a **Clear all** action.
- **Channels** — buffered-message count, distinct channels, the busiest channels, recent messages,
  and a **Trim** action (deletes messages older than the 1-day default retention).
- **Audit** — total event count, a search/feed over recorded events filterable by subject, actor,
  action, and correlation id, and a detail page per event with the full (pretty-printed)
  `data`/`changes`/`context` payloads. Read-only — there's no delete action in the dashboard;
  pruning is a deliberate CLI/cron operation, not a click away.

Queue actions reuse the library's own helpers (`queues.pause/resume`, `maintenance.retry_failed`),
so the UI applies exactly the same semantics as the library and CLIs.

## Security

It's an **internal ops tool**: it exposes tracebacks and destructive actions (retry / discard /
pause / clear / trim, each behind a confirm dialog). It **binds to `127.0.0.1` by default**, and it
**refuses to bind a non-loopback `--host` unless you configure authentication** (or pass
`--insecure` to override). Destructive actions are POSTs guarded by a same-origin `Origin`/`Referer`
check (a basic CSRF defense, so another site can't auto-submit a form to your dashboard); that guard
stays on no matter how you authenticate.

## Authentication

Auth is one pluggable chokepoint with three backends — choose whichever fits your deployment. All of
them still run the CSRF guard, and Basic credentials travel in clear text, so keep the bind on
loopback or put TLS in front.

### HTTP Basic auth (built-in)

The secret comes from the environment (kept out of `argv`/`ps`), as plaintext or a hash:

```bash
# plaintext secret
FIRM_UI_PASSWORD=s3cret firm-ui --database-url sqlite:///app.db --basic-auth-user admin

# or a hash, so no plaintext is stored
firm-ui --hash-password                       # prompts, prints a "pbkdf2_sha256$…" string
FIRM_UI_PASSWORD_HASH='pbkdf2_sha256$…' \
  firm-ui --database-url sqlite:///app.db --basic-auth-user admin
```

The browser shows its native sign-in dialog; no login page or cookies are involved.

### Tie into an upstream auth proxy

If you already run oauth2-proxy, Cloudflare Access, or nginx `auth_request`, let it authenticate and
forward the user in a header:

```bash
firm-ui --database-url sqlite:///app.db \
         --trust-auth-header X-Forwarded-User --trusted-proxy 127.0.0.1
```

The header is trusted **only** when the request's immediate peer is a `--trusted-proxy` (default
loopback), so a direct client can't spoof it — bind the dashboard where only the proxy can reach it.

### Your own authentication

An `Authenticator` is any object with `authenticate(req) -> Allow | Deny`. Load one by import path:

```bash
firm-ui --database-url sqlite:///app.db --authenticator myapp.security:dashboard_auth
```

…or run the dashboard from your own process and pass it in:

```python
from firm.ui import Allow, Deny, build_dashboard, serve

class SessionAuth:
    def authenticate(self, req):
        user = my_store.user_for(req.header("Cookie"))
        return Allow(user) if user else Deny(302, {"Location": "https://sso/login"})

dashboard = build_dashboard(database_url="sqlite:///app.db")
serve(dashboard, host="0.0.0.0", port=8787, authenticator=SessionAuth())
```

`req` exposes `.method`, `.path`, `.header(name, default="")`, and `.client_addr`; `Deny(status,
headers, message)` decides the response (a `401` challenge, a `403`, or a redirect to your own
login). A runnable version is in [examples/secured_dashboard.py](../examples/secured_dashboard.py).

## How it stays optional

The dashboard ships in the wheel but is only reached via the `firm-ui` command. It imports
the standard library, SQLAlchemy (already required), Jinja2 (for templates), and the parts'
read/maintenance functions — so skipping it costs nothing, and the rest of firm never loads it.
