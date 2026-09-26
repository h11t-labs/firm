"""Command-line entry point: ``firm-cache stats|clear|trim``."""

from __future__ import annotations

from .._core.cli import db_option, require_click, require_url
from .._core.database import create_engine_for, dispose_engine, transaction
from . import __version__
from .estimate import entry_count, estimate_size
from .store import DEFAULT_MAX_SIZE, TWO_WEEKS_SECONDS, Cache

click = require_click("cache")

_db_option = db_option("FIRM_CACHE_DATABASE_URL")


def _url(database_url: str | None) -> str:
    return require_url(database_url, "FIRM_CACHE_DATABASE_URL")


@click.group(help="firm-cache — database-backed cache store.")
@click.version_option(__version__, prog_name="firm-cache")
def main() -> None:
    pass


@main.command(help="Show entry count and estimated size.")
@_db_option
def stats(database_url: str | None) -> None:
    engine = create_engine_for(_url(database_url))
    try:
        with transaction(engine) as conn:
            click.echo(f"entries: {entry_count(conn)}")
            click.echo(f"estimated_size: {estimate_size(conn)} bytes")
    finally:
        dispose_engine(engine)


@main.command(help="Delete every cache entry.")
@_db_option
def clear(database_url: str | None) -> None:
    engine = create_engine_for(_url(database_url))
    try:
        with Cache(engine=engine, create_schema=False, auto_expire=False) as cache:
            click.echo(f"cleared {cache.clear()} entries")
    finally:
        dispose_engine(engine)


@main.command(help="Run one eviction pass and exit.")
@_db_option
@click.option("--max-age", type=float, default=None, help="Override max entry age (seconds).")
@click.option("--max-size", type=int, default=None, help="Override max total size (bytes).")
@click.option("--max-entries", type=int, default=None, help="Override max entry count.")
@click.option("--batch-size", type=int, default=None, help="Max rows to evict in one pass.")
def trim(
    database_url: str | None,
    max_age: float | None,
    max_size: int | None,
    max_entries: int | None,
    batch_size: int | None,
) -> None:
    # A bare `trim` evicts against the Cache defaults; each option that is given overrides one
    # limit so the one-shot command can target a specific eviction (e.g. down to --max-entries).
    engine = create_engine_for(_url(database_url))
    try:
        with Cache(
            engine=engine,
            create_schema=False,
            auto_expire=False,
            max_age=max_age if max_age is not None else TWO_WEEKS_SECONDS,
            max_size=max_size if max_size is not None else DEFAULT_MAX_SIZE,
            max_entries=max_entries,
            expiry_batch_size=batch_size if batch_size is not None else 100,
        ) as cache:
            click.echo(f"evicted {cache.expiry.run_once()} entries")
    finally:
        dispose_engine(engine)


if __name__ == "__main__":  # pragma: no cover
    main()
