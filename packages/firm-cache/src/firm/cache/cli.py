"""Command-line entry point: ``firm-cache stats|clear|trim``."""

from __future__ import annotations

from .._core.cli import db_option, require_click, require_url
from .._core.database import create_engine_for, dispose_engine, transaction
from . import __version__
from .estimate import entry_count, estimate_size
from .store import Cache

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
def trim(database_url: str | None) -> None:
    engine = create_engine_for(_url(database_url))
    try:
        with Cache(engine=engine, create_schema=False, auto_expire=False) as cache:
            click.echo(f"evicted {cache.expiry.run_once()} entries")
    finally:
        dispose_engine(engine)


if __name__ == "__main__":  # pragma: no cover
    main()
