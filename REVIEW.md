# firm — deep code review

*Date: 2026-06-30. Method: 13-agent fan-out audit (correctness/concurrency, security, architecture,
quality/docs) with adversarial verification — every correctness/security finding was independently
re-checked by a refute-first verifier; critical/high ones went through a 3-way panel (logic / race /
test-coverage). Counts: 66 raw findings → 33 confirmed, 2 uncertain, 10 refuted, 21 low/nit. Plus a
dedicated whole-tree security sweep and a completeness-critic pass.*

## Verdict

This is a **strong, carefully-built codebase** for its age. Architecture is clean (three genuinely
independent modules over a shared `_core` seam), the SQLite concurrency path (BEGIN IMMEDIATE
serialization) is solid and well-tested, the security posture is unusually good (zero string-built
SQL, JSON — not pickle — for job args, a locked-down dashboard), and the docs are accurate (every
fenced `python` block is executed).

The real risk concentrates in two places: **(1) the PostgreSQL/MySQL concurrency paths, which are
reasoned-about but not runtime-tested** (the default suite is SQLite-only; the dialect tests only
assert on compiled SQL strings), and **(2) a handful of genuine correctness bugs** in the
dispatcher, channel listener, and the SQLAlchemy contrib glue. None are data-corrupting on SQLite
today; several would bite under real multi-process Postgres/MySQL load.

## Ground-truth checks (run directly)

| Check | Result |
|---|---|
| `pytest` (SQLite, Py 3.14) | ✅ all pass |
| `ruff check` | ✅ clean |
| `ty check src/firm` | ❌ **1 error** — `ui/auth.py:43` (`self.headers.get` on `object`; the `# type: ignore[attr-defined]` is mypy-style and `ty` doesn't honor it). AGENTS.md says ty "must pass". This is in the **in-progress, untracked** `auth.py`. |
| `test_llms_full_is_current` | ⚠️ The **staged** `llms-full.txt` is 636 lines stale vs the generator. The working-tree copy was current at session start; regenerate with `uv run python scripts/gen_llms_full.py` and re-stage before committing. |

> Note: `FIRM_TEST_PG_URL` / `FIRM_TEST_MYSQL_URL` were unset, so **no test exercised live
> Postgres or MySQL.** Every PG/MySQL finding below is unverified at runtime — see §2.

---

## 1. Correctness — must-fix

### 1.1 `on_conflict="discard"` is silently ignored for delayed jobs `[HIGH]`
[`dispatcher.py:64-69`](src/firm/queue/dispatcher.py:64) · panel-confirmed (3/3)

Discard is honored only at immediate-enqueue time ([`enqueue.py:98`](src/firm/queue/enqueue.py:98)).
When a **future-scheduled** job (`enqueue_in`/`enqueue_at`) becomes due and its concurrency key is
full, `dispatch_once` calls `_to_blocked()` unconditionally and never consults `spec.on_conflict`.
`run_maintenance` then promotes any blocked key with free capacity, so the job **eventually runs** —
the exact opposite of `discard`. Behavior changes based purely on whether the job was delayed.
**Fix:** in `dispatch_once`, when `acquire()` fails, branch on `spec.on_conflict`; for `"discard"`
delete the scheduled (and jobs) row instead of inserting into `blocked_executions`.

### 1.2 Concurrent broadcasts can be permanently dropped `[HIGH]`
[`channel.py:157`](src/firm/channel/channel.py:157) / [`messages.py`](src/firm/channel/messages.py) · panel-confirmed (3/3, all "high")

The listener advances a single watermark `_global_last_id = max(id seen)` and re-queries `id >
after_id`. Autoincrement ids are assigned at INSERT but become visible at COMMIT, and **commit order
≠ id order**. With two concurrent broadcasters, B (id=N+1) can commit before A (id=N); a poll between
the commits delivers N+1 and sets the watermark to N+1; when A commits, the next poll filters `id >
N+1` and **never sees N** → silent message loss. Contradicts the documented at-least-once guarantee;
realistic under any multi-writer load. **Fix:** don't advance the cursor past possibly-uncommitted
ids — keep a short safety lag (e.g. only advance past ids older than a grace window), or re-scan a
bounded recent range, or serialize id assignment+visibility through the claim/immediate-tx seam.
Then document the actual delivery guarantee.

### 1.3 `enqueue_after_commit` leaks deferred jobs across transactions `[HIGH impact / MED likelihood]`
[`contrib/sqlalchemy.py:32`](src/firm/contrib/sqlalchemy.py:32) · panel-confirmed (3/3)

Deferred `(job, args, kwargs)` tuples live in `session.info[_PENDING]`, cleared only by the
`after_commit`/`after_rollback` listeners. But `session.info` **survives `session.close()`**, and
`close()` without a commit fires **neither** listener. On a reused/scoped/long-lived session
(Flask-SQLAlchemy `db.session`, FastAPI dependency sessions) a request that defers an enqueue then
closes without committing leaves the tuple behind — and it flushes on the **next unrelated commit**,
enqueuing a job for work that never persisted, bound to the wrong request. Defeats the module's whole
purpose ("never enqueue a job for work that didn't persist"). **Fix:** also clear pending state on
`after_transaction_end`/`after_soft_rollback` for the outermost transaction when no commit happened.

### 1.4 Tagged-serializer reserved key `__bb__` collides with user dicts `[MEDIUM]`
[`queue/serialization.py:35`](src/firm/queue/serialization.py:35) · verified

`object_hook=_decode` runs on **every** decoded dict. A job arg like
`{"__bb__":"datetime","v":"..."}` round-trips as a `datetime` (silent corruption); `{"__bb__":"x"}`
(no `v`) raises `KeyError` **on the worker** — defeating the "bad args fail at enqueue, not hours
later" guarantee. Reachable with ordinary JSON-shaped user data. **Fix:** in `_decode`, only treat a
dict as tagged when it has exactly the expected shape (known tag + `v` + `len==2`); reject/escape the
reserved key at serialize time so the failure is the caller's.

### 1.5 Recurring tasks bypass the job's concurrency controls `[MEDIUM]`
[`queue/scheduler.py:96`](src/firm/queue/scheduler.py:96) · verified

`_record_and_enqueue` inserts with no `concurrency_key` and pushes straight to `ready_executions`,
ignoring `task.job.concurrency` entirely. A recurring task whose `@job` declares
`concurrency=(limit/duration/on_conflict)` runs with **no limiting**, and (key being NULL) never
counts against non-recurring instances. The same gap exists for **manual retry**
([`maintenance.py` `retry_failed`](src/firm/queue/maintenance.py)). **Fix:** route recurring (and
retried) enqueues through the same `spec.key_for`→acquire path, or document the exemption loudly.

### 1.6 Decrypt failure throws out of `cache.get()` instead of being a miss `[MEDIUM]`
[`cache/serialization.py:46`](src/firm/cache/serialization.py:46) · verified

`EncryptedCoder.loads` lets `InvalidToken` (wrong/rotated key, corrupted ciphertext) propagate
straight out of `get`/`get_multi`/`fetch`. A cache should treat an undecryptable entry as a miss and
recompute; instead every read of an affected key crashes the caller (e.g. after a partial key
change). **Fix:** catch `InvalidToken` (and deserialization errors) → return `None`; optionally
surface the event so silent corruption is observable.

### 1.7 Lower-severity correctness (verified)
- **Worker shutdown has no timeout** — `Worker.poll` blocks on `future.result()` for every job and
  `shutdown(wait=True)` has no timeout/`cancel_futures`; one long job makes the worker ignore
  `shutdown_timeout`. Fork mode is bounded by the parent's SIGKILL; **ThreadSupervisor (embedded) is
  not** — `stop()` returns while jobs still run. [`worker.py:64`](src/firm/queue/worker.py:64)
- **Crash-looping child respawned with no backoff** — `_reap_and_restart` re-forks every cycle
  (~5/sec cap); a child that dies on startup busy-loops and masks the root cause.
  [`supervisor.py:257`](src/firm/queue/supervisor.py:257)
- **Heartbeat errors are swallowed** — `HeartbeatPoller` has no `on_error`; repeated heartbeat
  failures silently stop liveness refresh → supervisor prunes the process and recovers its claims
  (possible duplicate execution) with no hook fired. [`_core/process.py:92`](src/firm/_core/process.py:92)
- **`ThreadSupervisor.start()` partial-start leak** — no try/except around loop starts; a failure
  partway leaves daemon threads running and a stale `processes` row.
  [`supervisor.py:120`](src/firm/queue/supervisor.py:120)
- **Cache `created_at` resets on every write/increment** → hot keys never age out under `max_age`.
  Defensible "write refreshes TTL" but undocumented. [`cache/entries.py:27`](src/firm/cache/entries.py:27)
- **`run_maintenance` picks an arbitrary spec** via `func.min(class_name)` when classes share a
  `group` key but declare different `limit`/`duration`. [`dispatcher.py:111`](src/firm/queue/dispatcher.py:111)
- **SIGQUIT immediacy can be downgraded** by a following SIGTERM (`_immediate` doesn't latch).
  [`supervisor.py:274`](src/firm/queue/supervisor.py:274)
- **Channel size limits declared but not enforced** — `channel ≤ 1024 bytes` per docs/schema, but
  `insert_message` never validates; MySQL `VARBINARY(1024)` truncates/raises while SQLite/PG store
  full bytes → backend-dependent. [`channel/messages.py:18`](src/firm/channel/messages.py:18)

---

## 2. PostgreSQL / MySQL — reasoned, not runtime-tested ⚠️

The single biggest **residual risk**: every PG/MySQL-specific issue below rests on code reading, not
a passing concurrent test. The default suite is SQLite-only; `test_dialect_compile.py` only asserts
that `FOR UPDATE SKIP LOCKED` appears in a *compiled string*. **Stand up real PG + MySQL in CI with
barrier-synchronized, multi-session tests for claim / semaphore acquire+promote / cache increment
before trusting these paths in production.**

- **Joined `FOR UPDATE SKIP LOCKED` over-locks `firm_jobs`** `[MEDIUM]` — dispatcher & recovery lock
  the joined `jobs` rows (no `of=` clause), so a job row write-locked by results/maintenance causes
  its scheduled/claimed row to be **skipped**; worst on recovery (delays crash recovery). **Fix:**
  `with_for_update(skip_locked=True, of=<execution_table>)`. [`dispatcher.py:59`](src/firm/queue/dispatcher.py:59), [`recovery.py`](src/firm/queue/recovery.py)
- **Free slot can strand a blocked job for up to the maintenance interval (default 600s)** `[MEDIUM]`
  — on READ COMMITTED, a dispatch parking a job can interleave with a release that finds no blocked
  row yet; recovery is only the 600s maintenance pass. **Fix:** lower the default interval
  substantially and/or have the worker re-promote the just-released key. [`dispatcher.py:94`](src/firm/queue/dispatcher.py:94)
- **`cache.increment` first-write race on a brand-new key** — acknowledged in IMPROVEMENTS.md; the
  cache reviewer believes `ON CONFLICT DO NOTHING`/`INSERT IGNORE` blocks the in-flight inserter and
  closes it, but **this is exactly the kind of claim that needs a live concurrent test.**
- **MySQL/MariaDB `SKIP LOCKED` has no version floor/probe** `[LOW]` — needs MySQL 8.0+ / MariaDB
  10.6+; older servers throw a raw SQL syntax error on first claim. Document + probe
  `server_version_info` at engine setup. [`_core/dialects/mysql.py:21`](src/firm/_core/dialects/mysql.py:21)
- **`_IMMEDIATE_KEY` leaks across pooled SQLite connections** `[MEDIUM]` — `immediate_transaction`
  sets `conn.info[_IMMEDIATE_KEY]=True` and never clears it; `Connection.info` is backed by the
  pooled record, so after the first claim/increment **every** later plain `transaction()` on that
  connection emits `BEGIN IMMEDIATE`, needlessly grabbing SQLite's write lock and serializing reads.
  Concrete live path: `cache.increment` (immediate) vs `cache.get/set` (plain) share an engine.
  **Fix:** pop the key in a `finally`, or reset it on pool checkout. [`_core/database.py:120`](src/firm/_core/database.py:120)

---

## 3. Security — strong posture, few real items

A dedicated whole-tree sweep found **no SQL injection** (every statement is SQLAlchemy Core; the only
literal SQL is the constant `PRAGMA`/`BEGIN` strings, with `busy_timeout` `int()`-wrapped), **no**
`eval`/`exec`/`subprocess`/`os.system`/`yaml`, **no** path-traversal or SSRF surface. The dashboard
defaults to loopback bind, **refuses a non-loopback host without an authenticator** unless
`--insecure`, runs auth before routing, uses PBKDF2-200k + `hmac.compare_digest`, guards POSTs with
an Origin/Referer CSRF check, and HTML-escapes **every** dynamic value (job args, tracebacks, cache
keys, channel payloads, incl. the non-UTF-8 `repr()` fallback). The job registry only ever uses a
DB-stored `class_name` as a dict key (unknown → `UnknownJob`, never `import_module`), so a
DB-write attacker can't get code execution through it.

- **`pickle.loads` of cache values** `[LOW — documented design choice]`
  [`cache/serialization.py:26`](src/firm/cache/serialization.py:26) · `PickleCoder` is the default
  ([`store.py:56`](src/firm/cache/store.py:56)); encryption is off unless `encrypt_key` is set.
  Anyone who can write the `value` column gets RCE on any reader. **But:** this requires DB write
  access (already inside the trust boundary), it's the same trade-off Rails Solid Cache makes (firm
  is *better* — ships & documents `JSONCoder`, and the **queue** uses JSON, not pickle), it's warned
  about explicitly in [`docs/cache/encryption-and-coders.md`](docs/cache/encryption-and-coders.md),
  and the dashboard never deserializes values. *Recommendation:* consider making `JSONCoder` the
  default (or louder docs) for shared/multi-tenant DBs. Not a bug.
- **`.coverage` is tracked and not gitignored** `[LOW]` — a binary SQLite artifact embedding absolute
  home-dir paths. `git rm --cached .coverage` and add `.coverage`/`.coverage.*`/`htmlcov/` to
  `.gitignore`.
- **Unauthenticated POST body read before auth** `[LOW]` — `do_POST` reads the full
  client-`Content-Length` body before `_check_auth` → cheap memory/bandwidth DoS. Cap the drained
  length and/or read after auth. [`ui/server.py:137`](src/firm/ui/server.py:137)
- **No response security headers** `[LOW]` — add `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, and a tight CSP (the dashboard is fully self-contained, so CSP is
  essentially free). [`ui/server.py:51`](src/firm/ui/server.py:51)
- **No Fernet key rotation** `[LOW/uncertain]` — single key, no `MultiFernet`; rotating invalidates
  the whole cache (documented). Consider accepting old+new keys. [`cache/serialization.py:49`](src/firm/cache/serialization.py:49)

---

## 4. Architecture & schema

Module independence holds (queue/cache/channel never import each other; core never imports
ui/contrib), all DB access routes through `_core`, the `firm_` prefix is consistent, and the
Alembic baselines delegate to the schema metadata (no drift). Notable items:

- **Layering inversion** `[LOW]` — `_core/process.py` imports `firm.queue.schema`; the shared layer
  depends on a feature module. [`_core/process.py:17`](src/firm/_core/process.py:17)
- **`firm_recurring_executions` grows unbounded** — no trimming of recurring-fire records.
  [`queue/scheduler.py:116`](src/firm/queue/scheduler.py:116)
- **Channel poll lacks a composite `(channel_hash, id)` index** `[MEDIUM]` — its only hot query is
  `channel_hash IN (...) AND id > after ORDER BY id`, run every `polling_interval` (0.1s); today it
  filters by hash then scans/sorts by id. [`channel/schema.py:46`](src/firm/channel/schema.py:46)
- **Eviction executor queue is unbounded & not de-duplicated** `[MEDIUM]` — under sustained writes,
  submit rate can outpace the single worker → latent memory growth + redundant runs. Coalesce to ≤1
  pending run. [`cache/expiry.py:37`](src/firm/cache/expiry.py:37)
- **Engine `pool_size`/`max_overflow` exist but aren't reachable through `configure()`** `[LOW]`.
  [`_core/config.py:40`](src/firm/_core/config.py:40)

---

## 5. Quality, docs & hygiene

- **`ty` is red** (§ground-truth) — `ui/auth.py:43`; the mypy-style `# type: ignore[attr-defined]`
  isn't honored by `ty`. Use `getattr`/a cast/`Mapping` typing instead.
- **Staged `llms-full.txt` is stale** (§ground-truth) — regenerate + re-stage before committing.
- **`examples/secured_dashboard.py` is untracked but referenced** by README / examples/README /
  docs/ui — `git add` it (part of the in-progress auth work, alongside untracked `ui/auth.py`,
  `tests/ui/test_auth.py`).
- **Destructive CLIs have no guard** `[MEDIUM]` — `firm-cache clear/trim`, `firm-channel trim`
  wipe data with no `--yes`/confirmation; a mistyped `--database-url` is unrecoverable. Add a `--yes`
  flag (or `click.confirm(abort=True)` on a TTY). [`cache/cli.py:50`](src/firm/cache/cli.py:50)
- **`firm-queue work` / `start --mode thread` ignore SIGTERM** `[MEDIUM]` — the loop only catches
  `KeyboardInterrupt` (SIGINT); `docker stop`/systemd/k8s send **SIGTERM** → killed with no graceful
  drain or process deregistration. Fork mode installs handlers correctly; the thread/work paths
  don't. Install a SIGTERM handler that triggers the same shutdown. [`queue/cli.py:97`](src/firm/queue/cli.py:97)
- **`--import` failures dump a raw traceback** `[LOW]` — wrap `importlib.import_module` and raise
  `click.UsageError`, mirroring the ui authenticator handling. [`queue/cli.py:51`](src/firm/queue/cli.py:51)
- **Misc nits** — `.gitignore` lists `*.db` twice; `IMPROVEMENTS.md` lists several done items (LICENSE,
  examples, mkdocs); `solid_*` lineage names leak into `docs/index.md:8` (policy says README +
  comparison-to-rails.md only); `gen_llms_full.py` header references a nonexistent `make llms` target;
  duplicate job-class registration silently overwrites; pyproject `[project.urls]` still point at
  `yourname/firm` placeholders.

---

## 6. Residual risk — recommended follow-up (from the completeness critic)

The audit itself flagged these **under-examined** areas — worth a focused second pass:

1. **`results.py` / `execute_claimed`** got no dedicated slice. It reads the job row with `.one()`
   *before* running; a concurrent maintenance/retry deleting the row → unhandled `NoResultFound` with
   no hook. `UnknownJob` is finalized with a **default** `RetryPolicy()`, so an unregistered class is
   hard-failed even if it would have been retried. [`results.py:38-52`](src/firm/queue/results.py:38)
2. **Live PG/MySQL concurrency tests** (§2) — the highest-leverage gap.
3. **Cache `key_hash` collision in the increment path** — `ensure_entry` (INSERT…ON CONFLICT DO
   NOTHING on the `key_hash` unique index) + `read_entry_locked` mismatch + `write_entry(overwrite)`
   can **clobber a colliding key's row** under a 64-bit-truncated SHA256 collision. The read-side
   guard exists; the write/increment side doesn't. [`store.py:132`](src/firm/cache/store.py:132)
4. **Public-constructor input validation** — `configure`, `Cache.__init__`, `Channel.__init__` accept
   negative sizes, zero intervals (busy-spin), empty/in-memory URLs with no checks; failures surface
   late on first use.
5. **Alembic `migrations/env.py` vs auto-create** — cache/channel auto-create *and* ship migrations;
   nobody checked whether `alembic upgrade` against an auto-created schema conflicts.
6. **`semaphore.promote_one`** — `acquire()` is attempted *after* SKIP-LOCKing the blocked row; if
   acquire fails the freed slot is lost until the next release (inverse of the dispatcher stranding
   bug, on the release path).

---

## 7. Checked and dismissed (refuted by verification)

For confidence, these plausible-sounding findings were investigated and **ruled out**: semaphore
`expires_at` failsafe deleting a live holder's slot; `acquire`'s non-locking existence check being
unsafe on PG; supervisor `waitpid(-1)` reaping unrelated children; `clear_finished` "only one batch"
vs docs; eviction worker swallowing exceptions (it's intentional + self-healing); size-estimate
mis-scaling; **pickle being an undisclosed risk** (it's documented); CSRF "fail-open" when
Origin+Referer both absent (acceptable); error pages leaking exception repr (post-auth only);
`firm_recurring_executions` unbounded growth (real but lower impact than first rated).

---

*Full machine-readable findings (with per-finding verifier reasoning) were produced by the audit; the
items above are the verified, de-duplicated set. SQL injection was explicitly confirmed closed
tree-wide and need not be re-litigated.*
