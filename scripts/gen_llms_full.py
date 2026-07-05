"""Generate llms.txt (curated index) and llms-full.txt (everything) from the docs.

    python scripts/gen_llms_full.py        # rewrites both llms.txt and llms-full.txt

Both derive their doc set + order from the `nav` in zensical.toml (the single source of truth), so
a page or whole module added to the site shows up automatically:

* llms-full.txt is every doc concatenated, in nav order.
* llms.txt is a curated index. Its prose (the intro, "Start here", "Integration & operations",
  "Runnable examples", "Optional") lives in the constants below and is edited by hand; the
  per-module sections (Queue/Cache/Channel/Audit/…) are generated from the nav, so adding a module
  to the nav gives it a section here without touching this file. A new module renders with just its
  nav name unless you add a heading suffix / blurb to ``MODULE_META``.

A test (tests/test_docs.py) fails if either committed file is stale, so this can't silently drift —
run this script after editing docs or the nav.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# -- llms-full.txt -----------------------------------------------------------------------------

FULL_HEADER = (
    "# firm — full documentation\n\n"
    "> The complete firm docs concatenated into one file for LLMs/agents. firm is a pure-Python "
    "port of the Rails Solid stack: database-backed background jobs (queue), caching (cache), and "
    "pub/sub (channel), plus an original append-only audit log (audit), no Redis. See llms.txt for "
    "a curated index.\n"
)

# -- llms.txt (curated index) ------------------------------------------------------------------

# Nav top-level groups that the curated prose already covers — everything else is a "module" and
# gets an auto-generated section between "Start here" and "Integration & operations".
_NON_MODULE_SECTIONS = {"Home", "Get started", "Guides", "Reference"}

# Optional per-module heading suffix and blurb. A module missing here still renders (name only).
MODULE_META: dict[str, tuple[str | None, str | None]] = {
    "Queue": ("(background jobs)", None),
    "Cache": (None, None),
    "Channel": ("(pub/sub)", None),
    "Audit": (
        "(append-only event log)",
        "Not a Solid port — an original firm module. Record who did what, to what, in the same "
        "database\n(and optionally the same transaction) as the change. Subject/actor references "
        'are flexible: a\ndomain object, a `("Type", id)` tuple, a bare `"label"` role string, '
        "or a `Ref(type, id, name)` —\ntype and id are each optional.",
    ),
}

INDEX_PREAMBLE = """\
# firm

> Pure-Python ports of the Rails "Solid" stack: database-backed background jobs (queue), caching (cache), and publish/subscribe (channel) — plus an original append-only audit log (audit) — no Redis or extra broker. Runs on SQLite, PostgreSQL, and MySQL/MariaDB via SQLAlchemy with per-dialect locking. Plus an optional web dashboard and Flask/FastAPI integration.

firm has three ported modules — queue, cache, channel — plus `audit`, an original append-only
audit log; install only what you need (`pip install "firm[queue]"`, `[cache]`, `[channel]`,
`[audit]`). Jobs are plain functions decorated with `@bq.job`; the cache is a `Cache` object;
pub/sub is a `Channel` object; the audit log is an `AuditLog` object. The database is the single
source of truth. Start with the Cookbook and the API cheatsheet, which together cover the whole
surface with copy-pasteable examples. (Paths below are repo-relative; replace with the published
docs URLs once the site is hosted.)

## Start here

- [Cookbook](docs/cookbook.md): copy-pasteable examples for each module and for combinations (queue+cache, queue+channel, cache+channel, all three, Flask, FastAPI, production)
- [API cheatsheet](docs/api.md): every public function/class with signatures, in one flat page
- [Overview](docs/index.md): what firm is and how the modules relate
- [Installation](docs/installation.md): the extras (queue/cache/channel/audit/flask/fastapi/postgres/mysql/encryption/msgpack)"""

INDEX_TAIL = """\
## Integration & operations

- [Framework integration (Flask & FastAPI)](docs/contrib.md)
- [Dashboard (UI)](docs/ui.md)
- [Database backends (drivers, locking, migrations)](docs/database-backends.md)

## Runnable examples

- [examples/quickstart.py](examples/quickstart.py): queue + cache + channel in one script
- [examples/combined_worker.py](examples/combined_worker.py): a job that reads/writes the cache and broadcasts on a channel
- [examples/fastapi_app.py](examples/fastapi_app.py): a FastAPI app · [examples/flask_app.py](examples/flask_app.py): a Flask app
- [examples/audit_logging.py](examples/audit_logging.py): same-transaction audit events, label/`Ref` actors, and querying history

## Optional

- [Comparison to Rails](docs/comparison-to-rails.md): how firm relates to solid_queue/solid_cache/solid_cable and where it diverges
- [Testing & contributing](docs/testing-and-contributing.md)
- [Roadmap](IMPROVEMENTS.md)"""


def _nav(root: Path) -> list:
    return tomllib.loads((root / "zensical.toml").read_text())["project"]["nav"]


def _nav_docs(nav: list) -> list[str]:
    """Flatten the zensical nav to an ordered list of doc paths (relative to docs/)."""
    out: list[str] = []
    for item in nav:
        for value in item.values():
            if isinstance(value, str) and value.endswith(".md"):
                out.append(value)
            elif isinstance(value, list):
                out.extend(_nav_docs(value))
    return out


def _nav_sections(nav: list) -> list[tuple[str, list[tuple[str, str]]]]:
    """Top-level nav groups as ``(name, [(page_title, page_path), ...])``."""
    out: list[tuple[str, list[tuple[str, str]]]] = []
    for item in nav:
        for name, value in item.items():
            if isinstance(value, list):
                pages = [(t, p) for sub in value for t, p in sub.items()]
                out.append((name, pages))
            elif isinstance(value, str):
                out.append((name, [(name, value)]))
    return out


def _module_block(name: str, pages: list[tuple[str, str]]) -> str:
    suffix, desc = MODULE_META.get(name, (None, None))
    heading = f"## {name} {suffix}" if suffix else f"## {name}"
    links = " · ".join(f"[{title}](docs/{path})" for title, path in pages)
    body = f"{desc}\n\n" if desc else ""
    return f"{heading}\n\n{body}- {links}"


def build_llms_index(root: Path = ROOT) -> str:
    """The curated index: hand-written prose + per-module sections generated from the nav."""
    modules = [
        _module_block(name, pages)
        for name, pages in _nav_sections(_nav(root))
        if name not in _NON_MODULE_SECTIONS
    ]
    return "\n\n".join([INDEX_PREAMBLE, *modules, INDEX_TAIL]) + "\n"


def build_llms_full(root: Path = ROOT) -> str:
    parts = [FULL_HEADER]
    bar = "=" * 80
    for rel in _nav_docs(_nav(root)):
        body = (root / "docs" / rel).read_text().rstrip()
        parts.append(f"\n\n\n{bar}\n# Source: docs/{rel}\n{bar}\n\n{body}")
    return "\n".join(parts) + "\n"


if __name__ == "__main__":
    (ROOT / "llms.txt").write_text(build_llms_index())
    (ROOT / "llms-full.txt").write_text(build_llms_full())
    print("wrote llms.txt and llms-full.txt")
