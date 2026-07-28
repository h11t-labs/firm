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
  and a **Trim** action (deletes messages older than the retention; 1 day by default — pass
  `--channel-trim-retention SECONDS` to match your app's `Channel(message_retention=...)` so a
  click never deletes messages the app still keeps).
- **Audit** — total event count, a search/feed over recorded events filterable by subject, actor,
  action, and correlation id, and a detail page per event with the full (pretty-printed)
  `data`/`changes`/`context` payloads. Read-only — there's no delete action in the dashboard;
  pruning is a deliberate CLI/cron operation, not a click away.

Queue actions reuse the library's own helpers (`queues.pause/resume`, `maintenance.retry_failed`),
so the UI applies exactly the same semantics as the library and CLIs.

## Mount it in your application

If you already run a web application, the dashboard doesn't need a second process: mount it in
your own routing, on your own domain, behind the permissions you already have. One adapter per
framework, each behind its own extra (`firm-ui[django]`, `firm-ui[flask]`, `firm-ui[fastapi]`).
Build the `Dashboard` once at startup — it owns database engines — and close it on shutdown.

```python
# Django — urls.py
from django.contrib.admin.views.decorators import staff_member_required
from django.urls import include, path
from firm.ui import build_dashboard
from firm.ui.contrib.django import dashboard_urls

dash = build_dashboard(database_url="sqlite:///app.db")

urlpatterns = [
    path("firm/", include(dashboard_urls(dash, host_auth=True,
                                         decorator=staff_member_required))),
]
```

```python
# Flask
from firm.ui.contrib.flask import blueprint

app.register_blueprint(blueprint(dash, host_auth=True), url_prefix="/firm")
```

```python
# FastAPI
from fastapi import Depends
from firm.ui.contrib.fastapi import router

app.include_router(router(dash, host_auth=True), prefix="/firm",
                   dependencies=[Depends(require_admin)])
```

Every mount has to say **who authenticates it**, and neither answer is a default: pass
`host_auth=True` when your application guards the route (the decorator, dependency, or middleware
above), or `authenticator=` to have firm-ui check the request itself with any of the backends
below. Setting neither raises `ValueError` at mount time, so a mount can't silently mean "no auth
at all" — the same rule as the standalone server's refusal to bind a public address unguarded.

Everything else keeps working as it does standalone: links and form actions carry the mount prefix,
the destructive actions stay behind the same-origin `Origin`/`Referer` guard, and the preference
cookies are scoped to the mount path. The dashboard renders its own forms and carries no framework
CSRF token, so the adapters exempt these routes from a host's token check (Django's) — the
same-origin guard is what protects them.

The stylesheet is served by the mount at `<prefix>/static/style.css`. To publish it through your
own static pipeline instead, add `firm.ui.static_dir()` to that pipeline (Django's
`STATICFILES_DIRS`, an nginx `alias`, …) and pass `static_url="/assets/firm.css"` — pages then link
there and no request for it reaches your application.

Under the adapters is a plain, transport-free `DashboardApp`: `handle(UIRequest) -> UIResponse`,
where `UIRequest` carries the method, the mount-relative path, query, headers, body, peer, and the
mount `prefix`. Writing a mount for a framework not listed here is that translation and nothing
more.

## Building your own

Everything the dashboard reads comes from each part's own read-query module
(`from firm.queue import queries`, and the same for `firm.cache` / `firm.channel` /
`firm.audit`) — a supported `Connection`-in / dicts-out surface, so your own dashboard, exporter,
or health check can use exactly what this one uses. The signatures are listed per part in the
[API cheatsheet](api.md).

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

`--hash-password` prompts for a password and prints a self-describing
`pbkdf2_sha256$<rounds>$<salt-b64>$<hash-b64>` string (PBKDF2-HMAC-SHA256, 200k rounds, random
salt — the same format `firm.ui.hash_password()` produces). Store that string in
`FIRM_UI_PASSWORD_HASH`; verification is constant-time.

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
