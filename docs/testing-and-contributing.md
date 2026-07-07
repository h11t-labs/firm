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

## Releasing

Every package under `packages/` is published to PyPI independently (they share the `firm.*`
namespace but carry their own versions). Publishing is fully automated through
[`.github/workflows/release.yml`](../.github/workflows/release.yml) using
[PyPI trusted publishing](https://docs.pypi.org/trusted-publishers/) — there are no PyPI tokens
to manage. The workflow checks the changelog, builds, runs the test suite, `twine check`s,
uploads, and finishes by creating a GitHub Release for the tag (the changelog section as the
body, with auto-generated notes appended).

### Changelogs

Each package keeps a `packages/<name>/CHANGELOG.md` in
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format. PRs that make a user-visible
change add a line under the affected package's `## [Unreleased]` section — a review convention,
not CI-enforced. At release time it **is** enforced: the workflow runs
`scripts/check_changelog.py` and refuses to publish a version that has no matching
`## [<version>]` section. The changelog ships in the sdist, and PyPI links it in each package's
sidebar via the `Changelog` project URL.

### Releasing one package

To release **one package** (the common case):

1. In one PR: bump `version` in `packages/<name>/pyproject.toml` and retitle the package's
   `## [Unreleased]` section to `## [<version>] - <YYYY-MM-DD>` (leave a fresh, empty
   `## [Unreleased]` above it). If other packages should pick up the new version, adjust their
   `~=` pins in the same PR.
2. Tag the merged commit `<name>-v<version>` and push the tag:

   ```bash
   git tag firm-queue-v0.2.0 && git push origin firm-queue-v0.2.0
   ```

   The workflow refuses to publish if the tag version doesn't match the package's
   `pyproject.toml`, or if the changelog has no section for it.

### Releasing everything

To release **everything at once** (e.g. a coordinated bump), tag `v<version>`. That builds all
packages and uploads whatever isn't already on PyPI — previously published files are skipped, so
the tag is safe even when some packages didn't change (their changelogs already carry the
section for their current, released version).

Versions follow semver-ish pre-1.0 rules: breaking changes bump the minor version. The `firm`
meta-package's extras pin modules with `~=`, so a meta-package release is only needed when those
pins change.

> **Meta-package status:** the PyPI name `firm` is held by a dormant, release-less project and a
> [PEP 541 name-transfer request](https://github.com/pypi/support/issues/11384) is pending.
> Until it's granted, the release workflow excludes the meta-package from `v*` all-package tags
> (see the `rm dist/firm-[0-9]*` line in `release.yml`), docs use the per-package install form
> (`pip install firm-queue`), and the extras form is provided by the interim
> [`firm-stack`](https://pypi.org/project/firm-stack/) meta-package (`packages/firm-stack/` —
> keep its extras/pins in lockstep with `packages/firm/`). Once the name is ours: remove that
> `rm` line, publish the meta with a `firm-v<ver>` tag, and the `firm[queue]` extras form starts
> working; `firm-stack` then stays as a compatible alias.

## Building the docs

The documentation site is built with [Zensical](https://zensical.org) (configured in
`zensical.toml` at the repo root):

```bash
uv run zensical serve     # live preview with hot reload
uv run zensical build     # render the static site to ./site
```
