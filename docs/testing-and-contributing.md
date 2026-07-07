# Testing & contributing

## Layout

A [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/): each module is its own
publishable package under `packages/`, all sharing one lockfile and dev environment. They install
into the single `firm` import namespace (PEP 420), so imports are always `firm.queue`, `firm.cache`,
etc. — regardless of which packages you installed.

```
firm/
├── pyproject.toml                 # virtual workspace root: members, dev tools, lint/type config
├── packages/
│   ├── firm-core/    src/firm/_core/    # shared: engine, dialects, poller, clock, config, process
│   ├── firm-queue/   src/firm/queue/     # firm.queue  (jobs)   + firm.contrib + migrations/
│   ├── firm-cache/   src/firm/cache/     # firm.cache  (cache)  + migrations/
│   ├── firm-channel/ src/firm/channel/   # firm.channel (pub/sub) + migrations/
│   ├── firm-audit/   src/firm/audit/     # firm.audit  (audit log) + migrations/
│   ├── firm-ui/      src/firm/ui/        # firm.ui     (dashboard; depends on all four)
│   └── firm/                             # meta-package (no code; installs the four modules)
├── tests/{queue,cache,channel,audit,ui,contrib}/
└── docs/
```

Each module depends only on `firm-core`; `firm-ui` depends on all four; the `firm` meta-package pins
them together. See [Split into a uv workspace](https://github.com/h11t-labs/firm) for the rationale.

## Setup

```bash
uv sync    # venv + all workspace packages (editable) + all dev tools/drivers
```

## The gate

```bash
uv run pytest                                    # tests
uv run ruff check && uv run ruff format --check  # lint + format
uv run ty check packages scripts examples        # types
```

All must pass. The codebase is fully type-annotated (checked with Astral's `ty`) and formatted
with `ruff`.

## Running against Postgres and MySQL

By default the suite runs on SQLite. Point it at live databases and every database-touching test
*also* runs against them (fresh schema per test):

```bash
# spin up servers (any Docker works)
docker run -d --name bb_pg    -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=bb -p 5433:5432 postgres:15
docker run -d --name bb_mysql -e MARIADB_ROOT_PASSWORD=pw -e MARIADB_DATABASE=bb -p 3307:3306 mariadb:11

export FIRM_TEST_PG_URL="postgresql+psycopg://postgres:pw@localhost:5433/bb"
export FIRM_TEST_MYSQL_URL="mysql+pymysql://root:pw@localhost:3307/bb"

uv run pytest -p no:cacheprovider     # sqlite + postgres + mysql
```

Test ids are suffixed with the backend (`...[postgres]`, `...[mysql]`); run one backend with
`-k postgres`.

Notes:

- **Fork-mode** tests (the forking supervisor) are SQLite-only — the fork model is independent of the
  SQL backend, so it's exercised once.
- **Offline dialect-compile** tests (`test_dialect_compile.py`) assert the DDL and `SKIP LOCKED` SQL
  render correctly for Postgres/MySQL with no live database.

## Upstream test parity

firm is a port of the Rails Solid stack (`solid_queue` / `solid_cache` / `solid_cable`), but it is
its own project with its own suite — not a mirror of upstream. The porting rule is simple: **every
upstream test case is handled as one of firm's own behavior tests**, living beside the functionality
it exercises (a claim spec in `test_claim.py`, an eviction spec in `test_expiry.py`, and so on).
Where firm deliberately behaves differently from Rails, that divergence is written up in
[comparison-to-rails.md](comparison-to-rails.md) rather than left as a red test.

Tests ported from upstream keep a short `# upstream: <file>.rb :: <case>` comment so the lineage is
discoverable when reconciling against a newer Solid release — but they are ordinary firm tests: they
assert firm's behavior and go green.

## Conventions

- Tests are spec-style and grouped by behavior. New behavior gets a failing test first.
- Keep the public API small; internal machinery lives under `_core/`.
- Match the surrounding style — concise docstrings, explicit SQL via SQLAlchemy Core, no clever
  metaprogramming.

## Building the docs

The documentation site is built with [Zensical](https://zensical.org) (configured in
`zensical.toml` at the repo root):

```bash
uv run zensical serve     # live preview with hot reload
uv run zensical build     # render the static site to ./site
```
