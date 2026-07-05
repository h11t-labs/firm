# PLAN.md — remaining known issues, ready for execution

*Written 2026-07-03. Self-contained execution plan for the open findings from REVIEW.md
(2026-06-30, 13-agent audit) and the 2026-07-02 architecture review. Every item below was
re-verified against the current tree on 2026-07-03 unless marked **[verify first]**. Line
numbers are anchors, not gospel — re-locate the code before editing.*

## How to work this plan

- Read `AGENTS.md` first; its conventions are binding (no stdlib `logging`; errors surface via
  `firm.queue.hooks`; never branch on `engine.dialect.name` in a feature module — extend the
  `Dialect` seam in `firm._core.dialects`; all tables/indexes are `firm_*`).
- Gate for **every** item: `uv run python -m pytest` (511+ passing, 0 failures),
  `uv run ruff check packages tests scripts examples`, `uv run ty check packages`,
  `uv run pre-commit run --all-files`. After touching any `docs/*.md`:
  `uv run python scripts/gen_llms_full.py` (pre-commit checks it's current).
- Every behavior change ships with a test (see the port-parity policy in
  `docs/testing-and-contributing.md`). Tests live in `tests/<module>/`; multi-backend fixtures
  are already parametrized in each `tests/<module>/conftest.py`.
- Items are ordered by priority within each phase and are independent unless a dependency is
  noted. One item per commit. Phases 1–2 are the ones that matter; 3–4 are polish.
- **Already fixed — do not redo:** discard-at-dispatch (REVIEW §1.1), the `__bb__` tagged-JSON
  collision (§1.4), the core→queue layering inversion, Alembic version-table collision and
  auto-create/upgrade reconciliation, UI raw-SQL mutations, audit prune locking, meta-package
  extras, pool-size exposure, `ty` errors, stale llms files.

---

## Phase 1 — Correctness

### 1.1 Channel listener can permanently drop concurrent broadcasts — HIGH
**Files:** `packages/firm-channel/src/firm/channel/channel.py` (listener cursor: `_global_last_id`
at ~:64,132; per-channel cursors `_channel_last_id` ~:63,87–91),
`packages/firm-channel/src/firm/channel/messages.py` (`fetch_since`).
**Problem:** the listener advances a max-id watermark and re-queries `id > after_id`.
Autoincrement ids are assigned at INSERT but visible at COMMIT, and commit order ≠ id order:
with two concurrent broadcasters, if id N+1 commits and is polled before id N commits, the
cursor moves past N and N is never delivered. Contradicts the documented at-least-once claim.
**Fix:** don't advance the cursor past possibly-uncommitted ids. Simplest robust approach: keep
a short re-scan window — advance the cursor only to `max(seen id, cursor)` but re-query from
`cursor - K` (or track a small set of "seen" ids in the last grace period, e.g. 2× polling
interval) and de-duplicate before dispatch. Alternative: only advance past an id once every id
below it has been seen (gap tracking with a bounded wait). Pick one, document the delivery
guarantee that results in `docs/channel/internals.md`.
**Tests:** simulate out-of-order commits with two raw connections: begin tx A, insert (id N,
uncommitted); commit a later insert (id N+1) on tx B; poll; commit A; poll again — the message
with id N must be delivered. Runs on SQLite (use two engines/connections; note SQLite immediate
tx may serialize inserts — if unreproducible there, mark the test PG/MySQL-only via the
existing `backend` fixture and add a logic-level unit test for the cursor policy).

### 1.2 `enqueue_after_commit` leaks deferred jobs across transactions — HIGH
**File:** `packages/firm-queue/src/firm/contrib/sqlalchemy.py` (`_PENDING`/`_WIRED` in
`session.info`, listeners wired at :34–37, verified present).
**Problem:** pending `(job, args, kwargs)` tuples are cleared only by `after_commit` /
`after_rollback`. `session.close()` without a commit fires neither, and `session.info` survives
close — on a reused/scoped session (Flask-SQLAlchemy, FastAPI dependency), the stale tuple is
flushed by the **next unrelated commit**, enqueuing a job for work that never persisted.
**Fix:** also listen for the session lifecycle that ends a transaction without commit and clear
pending state — e.g. `after_soft_rollback`, and clear on `after_transaction_end` when the
outermost transaction closed without a commit (guard so an inner savepoint end doesn't wipe
state prematurely). Verify against both a plain `Session` and `scoped_session`.
**Tests (extend `tests/contrib/`):** defer an enqueue, `session.close()` without commit, run an
unrelated commit on the same session object → no job row. Also: rollback clears; commit still
enqueues exactly once; nested transaction (savepoint) doesn't clear early.

### 1.3 Recurring tasks and manual retry bypass concurrency controls — MEDIUM
**Files:** `packages/firm-queue/src/firm/queue/scheduler.py` (`_record_and_enqueue` inserts
straight into `ready_executions`, ~:121, with no `concurrency_key`),
`packages/firm-queue/src/firm/queue/maintenance.py` (`retry_failed` inserts into
`ready_executions`, ~:49–53).
**Problem:** a job whose `@job(concurrency=...)` declares a limit runs unlimited when fired by
the scheduler or retried from the dashboard/CLI, and (key = NULL for recurring) never counts
against normal instances.
**Fix:** route both paths through the same semaphore flow the dispatcher uses
(`dispatcher._spec_for` pattern: registry lookup by `class_name`; `spec.key_for(args, kwargs)`
→ `semaphore.acquire` → ready-or-blocked, honoring `on_conflict="discard"`). The scheduler has
the `Job` object so this is direct. `retry_failed` runs where the registry may be empty (the
dashboard process): if the class isn't registered, keep today's direct-to-ready behavior but
say so in the docstring (the semaphore-expiry failsafe still bounds it).
**Tests:** recurring task with `limit=1` and a slow first instance → second tick lands in
`blocked_executions` (or is discarded if `on_conflict="discard"`); retried failed job with
`limit=1` while another instance holds the slot → blocked, and promoted after release.

### 1.4 Cache decrypt failure crashes `get()` instead of missing; no key rotation — MEDIUM
**File:** `packages/firm-cache/src/firm/cache/serialization.py` (`EncryptedCoder.loads` is a
bare `self._inner.loads(self._fernet.decrypt(data))`, :45–46; single `Fernet(key)` at :58).
**Problem:** `cryptography.fernet.InvalidToken` (rotated key, corrupt ciphertext) propagates out
of `Cache.get/get_multi/fetch`; every read of an affected key raises. A cache must treat an
undecryptable entry as a miss. Also no way to rotate keys without invalidating everything.
**Fix:** (a) catch `InvalidToken` (and inner-coder deserialization errors) in the store's read
path and return a miss — deleting the poisoned row is optional but tidy; (b) accept
`encrypt_key: str | bytes | list[...]` and build `MultiFernet` when a list is given (encrypts
with the first key, decrypts with any), so rotation = prepend new key. Document in
`docs/cache/encryption-and-coders.md`.
**Tests:** write with key A, read with key B → `get` returns None and `fetch` recomputes;
`MultiFernet([B, A])` reads old entries; non-encrypted corrupt value → miss, not exception.

### 1.5 Worker shutdown has no timeout — MEDIUM
**File:** `packages/firm-queue/src/firm/queue/worker.py` (`self._pool.shutdown(wait=True)` :61;
unbounded `future.result()` :73).
**Problem:** one long/hung job makes `Worker.stop()` (and `ThreadSupervisor.stop()`) block
forever; `shutdown_timeout` is only enforced by ForkSupervisor's SIGKILL. Embedded/thread mode
never returns.
**Fix:** bound the drain: `future.result(timeout=...)` derived from the supervisor's
`shutdown_timeout` (thread through `WorkerConfig`/constructor, default e.g. 30s), and
`pool.shutdown(wait=False, cancel_futures=True)` after the deadline. A job that outlives the
deadline is abandoned in `claimed_executions` — that's correct: recovery will reclaim it (note
this in the docstring; it's the at-least-once contract).
**Tests:** enqueue a job that sleeps longer than a small `shutdown_timeout`; `worker.stop()`
returns within ~timeout; the claim row is still present (recoverable), no hang.

### 1.6 Crashed supervisor children respawn with no backoff — LOW/MEDIUM
**File:** `packages/firm-queue/src/firm/queue/supervisor.py` (`_reap_and_restart` → `_spawn`
immediately, ~:264–275).
**Fix:** per-child restart backoff (e.g. exponential 0.5s→30s, reset after the child survives
60s). Keep it simple: track `(restarts, last_spawn)` per child config.
**Tests:** fork-mode test (SQLite/POSIX-only, follow `tests/queue/test_fork.py` conventions)
with a child that exits immediately → observe spawn spacing grows / respawn count over a fixed
window is bounded.

### 1.7 Background-thread errors are invisible (poller default, heartbeat) — MEDIUM
**Files:** `packages/firm-core/src/firm/_core/poller.py` (`except Exception: ... if
self._on_error is not None` — else silently dropped, :47–52),
`packages/firm-core/src/firm/_core/process.py` (`HeartbeatPoller.__init__` passes no
`on_error`).
**Problem:** a heartbeat that keeps failing silently stops liveness refresh → the supervisor
prunes the "dead" process and recovers its claims (possible duplicate execution) with no signal
anywhere. AGENTS.md forbids stdlib logging; hooks live in firm-queue, which core cannot import.
**Fix:** (a) `InterruptiblePoller` default `on_error`: write the traceback to `sys.stderr`
(core-safe, no logging dependency) — keep the injected-callback override; (b) everywhere the
queue builds pollers (worker/dispatcher/scheduler/supervisor heartbeats), pass an `on_error`
that fires `HOOKS.fire("thread_error", exc)` so operators can hook it.
**Tests:** poller whose `poll` raises with no on_error → stderr contains the traceback (capsys);
queue-built HeartbeatPoller failure → `@on_thread_error` hook fires.

### 1.8 `ThreadSupervisor.start()` partial-start leak — LOW
**File:** `packages/firm-queue/src/firm/queue/supervisor.py` (`ThreadSupervisor.start`,
~:127–150: registers process row, fires hook, starts loops — no try/except).
**Fix:** wrap loop construction/starts; on failure stop already-started loops, deregister the
process row, then re-raise. **Test:** make one loop's `on_start` raise → no daemon threads left
(compare `threading.enumerate()`), no `firm_processes` row.

### 1.9 SIGTERM after SIGQUIT downgrades immediate shutdown — LOW
**File:** `packages/firm-queue/src/firm/queue/supervisor.py` (`_on_terminate` sets
`self._immediate = False` unconditionally, :281–283).
**Fix:** latch: `_on_terminate` must not reset `_immediate` once True (drop the assignment; set
`_immediate = False` only in `start()` init). **Test:** unit-level — call `_on_quit()` then
`_on_terminate()`; `_immediate` stays True.

### 1.10 Channel size limits declared but not enforced — LOW
**File:** `packages/firm-channel/src/firm/channel/messages.py` (`insert_message` does no length
check; schema's MySQL `VARBINARY(1024)` truncates/raises while SQLite/PG accept anything).
**Fix:** validate `len(channel_bytes) <= 1024` in `Channel.broadcast`/`subscribe` (raise
`ValueError`) so behavior is backend-independent. **Test:** 1025-byte channel name raises on
SQLite (i.e. everywhere).

### 1.11 `execute_claimed` fragility: `.one()` race and UnknownJob retry policy — LOW
**File:** `packages/firm-queue/src/firm/queue/results.py` (`.one()` at ~:47; UnknownJob
finalized with default `RetryPolicy()` at ~:53).
**Fix:** (a) `.first()`; if the row is gone (concurrent discard/maintenance), delete any claim
row and return False instead of raising `NoResultFound` into the worker thread. (b) UnknownJob:
keep fail-fast (no retry — retrying can't fix an unregistered class) but make it explicit: pass
`RetryPolicy(max_attempts=1)`-equivalent with a comment, and fire the 1.12 failure hook.
**Tests:** claim a job, delete the job row, `execute_claimed` returns False without raising;
unknown class lands in `failed_executions` with the traceback naming the class.

### 1.12 No "final failure" (dead-letter) hook — LOW
**Files:** `packages/firm-queue/src/firm/queue/results.py` (`_finalize_failure`, the
else-branch writing `failed_executions`), `packages/firm-queue/src/firm/queue/hooks.py`.
**Fix:** add a `job_failed` event fired when a job exhausts retries (payload: job_id,
class_name, exception). Registered via the existing `@on("job_failed")` mechanism. Document in
`docs/queue/retries-and-failures.md`.
**Test:** failing job with attempts=1 → hook fires once with the right payload; retried-then-
failing job → fires only on the final attempt.

### 1.13 `run_maintenance` picks an arbitrary spec for shared concurrency groups — LOW
**File:** `packages/firm-queue/src/firm/queue/dispatcher.py` (`func.min(_jobs.c.class_name)`
per blocked key, ~:115–120; the comment assumes all classes sharing a key share a spec).
**Fix:** cheapest correct behavior: resolve the spec for **every distinct class_name** on the
key and use the one with the largest `limit` (promotion is capacity-bounded by `acquire`
anyway, so over-estimating the limit is safe; under-estimating strands). Add a note to
`docs/queue/concurrency.md` that `group`-shared keys should declare identical limits.
**Test:** two classes share `group` with limits 1 and 2; maintenance promotes using limit 2.

---

## Phase 2 — PostgreSQL/MySQL hardening

*Context: REVIEW §2's core warning stands — the PG/MySQL claim paths are reasoned-about but the
default suite runs SQLite; live tests only run when `FIRM_TEST_PG_URL`/`FIRM_TEST_MYSQL_URL`
are set, and there are no dedicated concurrency stress tests. Item 2.6 is the highest-leverage
item in this phase; do it first and the rest become verifiable.*

### 2.1 Joined `FOR UPDATE SKIP LOCKED` over-locks `firm_jobs` — MEDIUM
**Files:** `packages/firm-queue/src/firm/queue/dispatcher.py` (dispatch join :54–59 and
maintenance join :115–120), `packages/firm-queue/src/firm/queue/recovery.py` (:36–46),
`packages/firm-core/src/firm/_core/dialects/` (the seam).
**Problem:** `with_skip_locked(stmt)` on a `scheduled⋈jobs` / `claimed⋈jobs` select locks the
joined `jobs` rows too (no `of=`); a jobs row locked by results/maintenance causes its
execution row to be **skipped** — worst on recovery, where it delays crash recovery.
**Fix:** extend the seam: `Dialect.with_skip_locked(stmt, *, of: Table | None = None)` →
`stmt.with_for_update(skip_locked=True, of=of)` on PG/MySQL, no-op on SQLite. Pass the
execution table at the three call sites (also check `semaphore.promote_one` — single-table, no
`of=` needed). MySQL note: `OF` requires MySQL 8.0+ — same floor as SKIP LOCKED itself (2.4).
**Tests:** extend `tests/queue/test_dialect_compile.py` — compiled PG/MySQL SQL contains
`FOR UPDATE OF firm_scheduled_executions SKIP LOCKED` (PG spells table name, MySQL alias);
behavioral coverage arrives with 2.6.

### 2.2 `_IMMEDIATE_KEY` leaks across pooled SQLite connections — MEDIUM
**File:** `packages/firm-core/src/firm/_core/database.py` (`immediate_transaction` sets
`conn.info[_IMMEDIATE_KEY] = True` at :120 and never clears; `Connection.info` is backed by the
pooled record).
**Problem:** after one claim/increment, every later plain `transaction()` on that pooled
connection emits `BEGIN IMMEDIATE`, needlessly serializing reads (live path: `cache.increment`
vs `cache.get` sharing an engine).
**Fix:** clear the flag in a `finally` inside `immediate_transaction` (pop from `conn.info`).
**Test:** on SQLite, run an `immediate_transaction`, then a plain `transaction()` on the same
engine and assert the emitted BEGIN is plain — the begin-event listener can be spied via an
`event.listens_for(engine, "begin")` capture, or assert `conn.info` no longer carries the key.

### 2.3 Semaphore acquire/release interleaving — MEDIUM **[verify first]**
**File:** `packages/firm-queue/src/firm/queue/semaphore.py` (atomic UPDATE-based
acquire/release, no row lock — verified; the questioned interleavings are acquire-fails vs
concurrent-release parking a job in `blocked` while a slot is free, and `promote_one`'s
acquire-after-skip-lock failing and dropping the freed slot).
**Fix:** decide with a live test, not by reading: under 2.6's harness, hammer
acquire/release/promote on one key from N sessions on PG and MySQL and assert no lost slots and
no stranded blocked rows beyond the maintenance interval. If stranding reproduces, serialize
per-key with `SELECT ... FOR UPDATE` on the semaphore row inside `begin_claim_tx` (IMPROVEMENTS
§1 sketch) — measure before/after.

### 2.4 MySQL/MariaDB `SKIP LOCKED` has no version floor — LOW
**Files:** `packages/firm-core/src/firm/_core/database.py` (`create_engine_for`),
`docs/database-backends.md`.
**Fix:** on first connect (pool `connect` event or a one-time check in `create_engine_for`),
read `engine.dialect.server_version_info`; MySQL < 8.0 or MariaDB < 10.6 → raise a clear
RuntimeError naming the floor. Document the floors.
**Test:** unit-test the version-check function directly with fake version tuples.

### 2.5 Blocked jobs can strand up to the maintenance interval — MEDIUM **[verify first]**
**Files:** `packages/firm-queue/src/firm/queue/dispatcher.py` (maintenance interval default —
check `SupervisorConfig.maintenance_interval` in `supervisor.py`), `semaphore.py`.
**Problem (REVIEW):** on READ COMMITTED, a dispatch parking a job can interleave with a release
that sees no blocked row yet; recovery is only the periodic maintenance pass (default 600s).
**Fix:** first reproduce under 2.6. Regardless of outcome: lower the default
`maintenance_interval` to ≤120s and make it prominent in `docs/queue/concurrency.md`. If the
race reproduces, add the worker-side re-promote after release (release_and_promote already
promotes; the gap is a release that ran *before* the blocked insert committed — a second
promote attempt after a short delay, or 2.3's row lock, closes it).

### 2.6 Live PG/MySQL concurrency stress tests — the highest-leverage item — MEDIUM
**Files:** new `tests/queue/test_live_concurrency.py`, `tests/cache/test_live_concurrency.py`.
**What:** barrier-synchronized multi-thread (each thread its own engine/connection) tests,
skipped unless `FIRM_TEST_PG_URL`/`FIRM_TEST_MYSQL_URL` are set (reuse the conftest pattern):
- N claimers × M ready jobs → every job claimed exactly once (no double-claim, none lost);
- one key, limit L, N enqueuers + releasers → semaphore `value` never < 0 or > L, no job left
  blocked when capacity is free after a full settle + maintenance pass;
- N concurrent `cache.increment` on a **brand-new** key → final value == N (this is the known
  first-write race; if it fails, fix per REVIEW: advisory lock on `key_hash` (PG
  `pg_advisory_xact_lock`, MySQL `GET_LOCK` — behind the Dialect seam) or an integer-only
  SQL-arithmetic fast path in `entries.py`);
- also cover the write-path `key_hash` collision guard: `write_entry` currently overwrites
  whatever row matches `key_hash` without comparing raw key bytes (read side checks;
  write/increment side doesn't — REVIEW residual §3). Add the raw-key comparison to the locked
  read → treat a colliding row as "different key": for increment, fall back to error or
  namespaced retry; document the chosen behavior.
**Also:** document how to run these locally (docker run commands for PG/MySQL) in
`docs/testing-and-contributing.md`; wire into CI when 4.4 lands.

### 2.7 Eviction/trim executor queues are unbounded and duplicate work — MEDIUM
**Files:** `packages/firm-cache/src/firm/cache/expiry.py` (`maybe_trigger` submits to the pool
unconditionally, :46), `packages/firm-channel/src/firm/channel/trim.py` (same pattern, :39–40).
**Problem:** under sustained writes, submissions outpace the single worker → unbounded queue
growth and redundant back-to-back runs.
**Fix:** coalesce to at most one queued run: a `_pending` flag (set before submit, cleared at
run start) so `maybe_trigger` is a no-op while a run is already queued. Apply identically to
both classes (they're siblings — keep them symmetric).
**Test:** call `maybe_trigger(writes=10_000)` with a blocked runner → executor queue holds ≤1
pending item; after unblocking, entries still get evicted.

---

## Phase 3 — Security & operations

### 3.1 UI: POST body read before auth, uncapped — LOW (cheap DoS)
**File:** `packages/firm-ui/src/firm/ui/server.py` (`do_POST` reads
`Content-Length` bytes at :220–221 **before** `_check_auth()` at :222).
**Fix:** run `_check_auth()` (and `_origin_ok()`) first; only then read the body, and cap it
(e.g. 64 KiB → respond 413). Only `/settings/refresh` uses the body — everything else can drain
lazily. Mind keep-alive: after a denied request either read+discard up to the cap or send
`Connection: close`.
**Tests (extend `tests/ui/`):** unauthenticated POST with a huge Content-Length → 401 without
reading the body (assert via a socket-level test or a small content check); oversized body with
auth → 413.

### 3.2 UI: no security headers — LOW
**File:** `packages/firm-ui/src/firm/ui/server.py` (`_html`/`_static_css`/`_redirect`,
:62–83).
**Fix:** one helper adding `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
`Referrer-Policy: no-referrer`, and a strict CSP (`default-src 'none'; style-src 'self'
'unsafe-inline'; img-src 'self' data:; form-action 'self'` — the dashboard is fully
self-contained; check the inline JS confirm() dialogs and refresh meta tag still work, adjust
`script-src` if templates inline any JS).
**Test:** every response (page, css, redirect, 404, error) carries the headers.

### 3.3 Destructive CLI commands have no guard — MEDIUM
**Files:** `packages/firm-cache/src/firm/cache/cli.py` (`clear`),
`packages/firm-channel/src/firm/channel/cli.py` (`trim`),
`packages/firm-audit/src/firm/audit/cli.py` (`prune`).
**Fix:** add `--yes/-y`; without it, `click.confirm("... this deletes N rows from <db>",
abort=True)` — which auto-aborts when stdin isn't a TTY, so scripts must pass `--yes`
(that's the point; call it out in `docs/*/cli.md`). `firm-cache trim`/eviction is
retention-bounded — guard only `clear` and `prune`; `firm-channel trim` deletes per policy —
leave unguarded but print the count.
**Tests:** CliRunner: `clear` without `--yes` aborts, with `--yes` clears.

### 3.4 `firm-queue work`/`start --mode thread` ignore SIGTERM; `--import` errors are raw — MEDIUM
**File:** `packages/firm-queue/src/firm/queue/cli.py` (KeyboardInterrupt-only loops :81–85,
:97–101; bare `importlib.import_module` :51–53).
**Fix:** (a) in both loops install a SIGTERM handler that sets a `threading.Event`; wait on the
event instead of `while True: sleep(1)`; on wake run the same `stop()` path as Ctrl-C (drain,
deregister process rows). (b) wrap the import: `raise click.UsageError(f"--import {module}
failed: {exc}")`.
**Tests:** `--import nonexistent.module` → clean UsageError, exit code 2, no traceback. SIGTERM:
spawn `firm-queue work` as a subprocess against a SQLite db, send SIGTERM, assert exit 0 and no
leftover `firm_processes` row (POSIX-only test, mirror `test_fork.py` guards).

### 3.5 UI read-only mode — MEDIUM (auth design Phase 3 leftover)
**Files:** `packages/firm-ui/src/firm/ui/cli.py`, `server.py`, `render.py`, `context.py`.
**Fix:** `--read-only` flag (and `FIRM_UI_READ_ONLY=1`): `do_POST` returns 403 for every
mutating route (allow `/settings/refresh` — it's a cookie), and templates hide the mutation
buttons (pass a `read_only` flag through the render context; components already receive
computed data, so this is a conditional in the page templates).
**Tests:** with read-only: POST `/cache/clear` → 403 and cache untouched; GET pages render
without action buttons; `/settings/refresh` still works.

### 3.6 `firm_recurring_executions` grows unbounded — LOW
**Files:** `packages/firm-queue/src/firm/queue/scheduler.py`, `maintenance.py`.
**Fix:** piggyback on the existing maintenance: delete recurring_executions rows older than N
days (default 30, configurable on `SupervisorConfig`), preserving enough history for the
`(task_key, run_at)` dedupe (anything older than the widest plausible schedule gap is safe;
30 days ≫ any cron interval). Wire into `run_maintenance` or `clear_finished`.
**Test:** old rows pruned, recent rows kept, dedupe still prevents double-fire for current
`run_at`.

### 3.7 Channel poll lacks a composite index — MEDIUM
**File:** `packages/firm-channel/src/firm/channel/schema.py`.
**Problem:** the hot query (`channel_hash IN (...) AND id > :after ORDER BY id`, every 0.1s)
has only single-column indexes.
**Fix:** add `Index("index_firm_messages_on_channel_hash_and_id", "channel_hash", "id")` and
drop the now-redundant single-column `channel_hash` index. Schema is pre-publication: the
Alembic baseline creates from `schema.metadata`, so editing `schema.py` is the whole change —
but call out in the commit message that existing dev databases need `drop_all`/recreate or a
manual index swap.
**Test:** the compile-level DDL test in `tests/` (mirror `test_dialect_compile.py` for channel
if none exists) asserts the composite index is emitted.

### 3.8 Cache TTL semantics: every write resets `created_at` — LOW (document, don't change)
**Files:** `packages/firm-cache/src/firm/cache/entries.py` (`_CONFLICT_COLS` includes
`created_at`, so upserts refresh it), `docs/cache/eviction.md`.
**Decision:** keep "write refreshes TTL" (it matches the upsert design and is a defensible
LRU-ish behavior) but **document it** explicitly in eviction.md, and add a test pinning the
semantics so a future change is deliberate: write, age the row artificially, write again →
`created_at` refreshed and the entry survives an age-based eviction pass.

---

## Phase 4 — Validation, hygiene, CI

### 4.1 Public constructors accept nonsense — LOW
**Files:** `firm/queue/config.py` (`configure`), `firm-cache .../store.py` (`Cache.__init__`),
`firm-channel .../channel.py`, `firm-audit .../log.py`.
**Fix:** validate at construction: empty/None URL *and* no engine → error (queue already does);
negative/zero `max_size`/`max_entries`/`max_key_bytesize`/batch sizes, non-positive
`polling_interval`/`expiry_interval`/`retention_interval`/`busy_timeout_ms` → `ValueError` with
the parameter name. Keep messages one-line and specific.
**Tests:** one parametrized test per package hitting each bad value.

### 4.2 Duplicate job registration silently overwrites — NIT
**File:** `packages/firm-queue/src/firm/queue/registry.py`.
**Fix:** registering an already-present `class_name` with a *different* Job object raises
`ValueError` (re-import of the same module re-registering the same object must stay silent —
that happens under test reruns and `--import`). **Test:** two distinct `@job` functions forced
to the same class_name → error; re-register same object → fine.

### 4.3 CI matrix — SMALL but unlocks Phase 2's value
**Files:** new `.github/workflows/ci.yml` **[verify first: repo currently has no CI config]**.
**What:** jobs: (a) lint+type (`ruff check`, `ruff format --check`, `ty check packages`,
`pre-commit run --all-files`); (b) pytest on Python 3.11–3.14 × SQLite; (c) pytest with
Postgres and MySQL service containers setting `FIRM_TEST_PG_URL`/`FIRM_TEST_MYSQL_URL` (this is
what makes 2.6's stress tests actually run). Use `astral-sh/setup-uv`; cache by `uv.lock`.

### 4.4 Hygiene sweep — NIT **[verify first — Bash was unavailable during planning]**
- `git ls-files | grep -i coverage` → if `.coverage` is tracked, `git rm --cached` it
  (`.gitignore` already covers it — verified).
- `grep -rn "solid_queue\|solid_cable\|solid_cache" docs/ | grep -v comparison-to-rails` →
  policy allows those names only in `README.md` and `docs/comparison-to-rails.md`; reword any
  other hits (REVIEW flagged `docs/index.md`). llms*.txt are generated — fix the source doc and
  regenerate.
- `git ls-files examples/` → ensure `examples/secured_dashboard.py` (referenced by docs) is
  tracked.
- Confirm no remaining references to the removed `firm._core.config.configure` in docs/examples
  (`grep -rn "_core.config import configure\|_core.config import current_runtime" docs examples`).

---

## Out of scope (deliberately)

`IMPROVEMENTS.md`'s feature roadmap (LISTEN/NOTIFY wake-ups, bulk enqueue, per-exception
retry_on/discard_on, job middleware, fugit schedules, unique jobs, continuations, cache
sharding/compression, metrics/tracing, PyPI publish) — those are features, not defects. If any
are wanted, plan them separately after this list is green.

## Suggested execution order

1. **2.6 + 4.3 first** (test harness + CI): they turn Phase 2 from "reasoned" into "verified"
   and protect everything else.
2. Phase 1 top-down (1.1–1.4 are the user-visible bugs; 1.5–1.13 are contained).
3. Phase 2 (2.1, 2.2, 2.4, 2.7 are mechanical; 2.3/2.5 are verify-then-fix under the harness).
4. Phase 3, then 4.

Each item: read the current code first (anchors may have drifted), implement, add the tests
named above, run the full gate, commit with a message naming the item id (e.g. `fix(queue):
bound worker shutdown (PLAN 1.5)`).
