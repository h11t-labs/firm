# AGENTS.md

Guidance for AI agents working in this repo. Make small, verified changes that match the
conventions below. (For *using* the library, see [`llms.txt`](llms.txt); this file is for
*modifying* the repo.)

## What this is

**firm** — pure-Python ports of the Rails Solid stack: `firm.queue` (background jobs),
`firm.cache` (caching), `firm.channel` (pub/sub), and `firm.audit` (append-only audit log,
original to firm), plus an optional `firm.ui` dashboard and `firm.contrib` (Flask/FastAPI
glue). No Redis; runs on SQLite/PostgreSQL/MySQL via SQLAlchemy.

## Commands

```bash
uv sync                                      # installs all workspace packages (editable)
uv run python -m pytest                       # full suite (must pass)
uv run ruff check packages tests scripts examples # lint
uv run ruff format                           # format
uv run ty check packages                     # type check (must pass)
uv run pre-commit run --all-files            # ruff + ty + llms-full + hygiene
```

Before finishing any change, run the suite + `ruff check` + `ty check`. Live PG/MySQL tests run
when `FIRM_TEST_PG_URL` / `FIRM_TEST_MYSQL_URL` are set.

## Layout

A **uv workspace**: each module is its own publishable package under `packages/`, sharing one
lockfile + dev env. All install into the single `firm` import namespace (PEP 420) — imports stay
`firm.queue`, `firm.cache`, … regardless of which packages are installed. The virtual workspace root
(`pyproject.toml`, no `[project]`) holds members, dev deps, and lint/type config. **No distribution
ships a `firm/__init__.py`** (would break the namespace); there is no `firm.__version__` — each
module carries its own.

- `packages/firm-{queue,cache,channel,audit}/src/firm/<m>/` — four **independent** modules; each a
  self-contained class/API (`@bq.job`/`Job`, `Cache`, `Channel`, `AuditLog`) with its own
  `schema.py`, `cli.py`, `migrations/`. Each depends only on `firm-core`. The queue's
  process-global `configure()`/`current_runtime()` singleton lives in `firm/queue/config.py`
  (cache/channel/audit are instance-based — no globals).
- `packages/firm-core/src/firm/_core/` — shared internals (ships as `firm-core`): `clock.now_utc`,
  `config` (`Settings`/`Runtime` — engine+dialect ownership, **no global state**), `database`
  (`create_engine_for`, `transaction`, `immediate_transaction`), `dialects/` (per-DB seam: claim
  locking + native upsert/insert-ignore), `poller.InterruptiblePoller`, `process` + `schema`
  (the `firm_queue_processes` table — copied into the queue's metadata via `to_metadata`),
  `tagged_json` (the queue/audit JSON envelope), `alembic_env` (shared migrations runner),
  `schema_setup` (create_all + version-table stamping). **Core never imports a feature module.**
- `packages/firm-ui/src/firm/ui/` — optional dashboard (`firm-ui`; depends on all four modules).
  `packages/firm-queue/src/firm/contrib/` — optional Flask/FastAPI/SQLAlchemy glue, ships **inside
  firm-queue** (it only depends on queue). **Nothing in core imports `ui` or `contrib`.**
- `packages/firm/` — meta-package (no code; installs the four modules; `firm[ui]`/`firm[all]`).
- `tests/<module>/`, `docs/`, `examples/`, `scripts/` stay at the repo root.

## Conventions

- **Lineage:** name `solid_queue`/`solid_cache`/`solid_cable` ONLY in `README.md`,
  `docs/index.md` (the lineage table), `docs/comparison-to-rails.md`,
  `docs/testing-and-contributing.md` (the upstream-parity discussion), and the packages'
  PyPI `keywords`/`description` metadata (discoverability). Elsewhere — code, comments,
  every other doc — use firm's own voice. All tables/indexes are `firm_*`.
- **Independence:** queue/cache/channel never import each other; importing `firm.queue` pulls no
  heavy deps. Keep `ui`/`contrib` optional and isolated (lazy framework imports, behind extras).
- **DB access:** go through `_core` (`transaction`/`immediate_transaction`/the `Dialect` seam);
  use `now_utc()` for timestamps. Never branch on `engine.dialect.name` in a feature module —
  extend the `Dialect` seam instead. All four modules support both schema paths: auto-create
  (`schema.create_all` / `create_schema=True`, which also stamps the module's
  `firm_<m>_alembic_version` table at head) and `alembic upgrade`.
- **Schema surface:** each module's `schema.py` Table objects are a supported *read* surface
  (the dashboard queries them); renames are breaking. All *writes* go through the owning
  module's API — `firm-ui` must never issue raw INSERT/UPDATE/DELETE against sibling tables.
- **Retention patterns (pick deliberately):** cache and channel trim probabilistically on
  write; audit prunes only on explicit opt-in (never delete audit data silently). Concurrent
  delete sweeps select victims with the skip-locked seam (see `channel/trim.py`,
  `audit/retention.py`).
- **Style:** ruff (`E,F,I,UP,B,SIM,C4,RUF`, line-length 100) + ruff format; full type hints; a
  module docstring per file. `migrations/*` are ruff-excluded.
- **New sibling module:** create `packages/firm-<m>/` mirroring `firm-cache` (its `pyproject.toml`
  with the namespaced `[tool.uv.build-backend]` block, `src/firm/<m>/` + `py.typed`), add it to the
  root `[tool.uv.sources]`, then wire into `zensical.toml` nav, a new `alembic.<m>.ini` (its
  `migrations/env.py` delegates to `firm._core.alembic_env` with its own
  `firm_<m>_alembic_version` table), the README/index/installation tables, `firm-ui`'s deps (if
  the dashboard should surface it), and a new extra in the `firm` meta-package.

## Docs

- Examples → `docs/cookbook.md`; signatures → `docs/api.md`. **Every fenced `python` block is tested**
  (`tests/test_docs.py`) — keep imports/symbols real.
- `llms.txt` and `llms-full.txt` are both generated by `scripts/gen_llms_full.py` from the
  `zensical.toml` nav. `llms.txt`'s per-module sections come from the nav (add a module there and it
  appears); its intro/curated prose lives in constants in that script. After editing docs, the nav,
  or the script, run `uv run python scripts/gen_llms_full.py` (tests fail if either file is stale).

## Gotchas

- The shell is **zsh** — unquoted vars don't word-split. For bulk edits use
  `find … -exec sed -i '' …`, not `for f in $LIST`. macOS sed needs the `''` after `-i`.
- No stdlib `logging` is used; background errors/events surface via `firm.queue.hooks`
  (`@on_thread_error`, `@on("worker_start")`). See the logging notes if adding diagnostics.
- Verify, don't assume — run `examples/*.py` and the suite to confirm behavior.
- Big changes have been verified with adversarial review workflows; prefer that for risky work.
