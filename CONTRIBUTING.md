# Contributing to firm

Thanks for helping out! The full developer guide — workspace layout, running the test suite
(including against live PostgreSQL/MySQL), and the upstream test-parity rules — lives in
[docs/testing-and-contributing.md](docs/testing-and-contributing.md). The short version:

## Setup & the gate

```bash
uv sync                                          # venv + all workspace packages + dev tools
uv run pytest                                    # tests
uv run ruff check && uv run ruff format --check  # lint + format
uv run ty check packages scripts examples        # types
```

All three must pass — CI runs exactly these, plus the suite against live PostgreSQL and MySQL.
`uv run pre-commit install` sets up the same checks as a pre-commit hook.

## Pull requests

- All changes land through pull requests against `main` — direct pushes are blocked, and the
  full CI matrix must be green before merging. PRs are **squash-merged**, so the PR title
  becomes the commit message: write it as a clear, imperative summary.
- Behavior changes need tests. firm's suite is a superset of the upstream Solid gems' cases;
  if you port or change behavior covered upstream, keep the `# upstream:` lineage comments
  intact (see the [parity rules](docs/testing-and-contributing.md#upstream-test-parity)).
- Deliberate divergences from Rails' behavior are documented in
  [docs/comparison-to-rails.md](docs/comparison-to-rails.md), not left as failing tests.
- Keep PRs focused — one topic per PR merges faster.

## Bugs & ideas

Open a [GitHub issue](https://github.com/h11t-labs/firm/issues). For security problems, please
**don't** open a public issue — see [SECURITY.md](SECURITY.md).

## Releases

Maintainers: see [Releasing](docs/testing-and-contributing.md#releasing).
