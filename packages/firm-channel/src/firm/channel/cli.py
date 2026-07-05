"""Command-line entry point: ``firm-channel stats|trim``."""

from __future__ import annotations

import os

try:
    import click
except ImportError as exc:  # pragma: no cover - exercised only without the 'channel' extra
    raise ImportError(
        'The firm-channel CLI requires "click". Install the channel extra: '
        'pip install "firm[channel]"'
    ) from exc

from .._core.database import create_engine_for, dispose_engine, transaction
from . import __version__, messages
from .channel import Channel

_db_option = click.option(
    "--database-url",
    default=None,
    help="SQLAlchemy URL (or set FIRM_CHANNEL_DATABASE_URL).",
)


def _url(database_url: str | None) -> str:
    url = database_url or os.environ.get("FIRM_CHANNEL_DATABASE_URL")
    if not url:
        raise click.UsageError(
            "No database URL: pass --database-url or set FIRM_CHANNEL_DATABASE_URL."
        )
    return url


@click.group(help="firm-channel — database-backed pub/sub.")
@click.version_option(__version__, prog_name="firm-channel")
def main() -> None:
    pass


@main.command(help="Show the number of buffered messages.")
@_db_option
def stats(database_url: str | None) -> None:
    engine = create_engine_for(_url(database_url))
    try:
        with transaction(engine) as conn:
            click.echo(f"messages: {messages.message_count(conn)}")
    finally:
        dispose_engine(engine)


@main.command(help="Delete messages older than the retention window and exit.")
@_db_option
@click.option("--retention", default=86400.0, help="Retention in seconds (default 86400 = 1 day).")
@click.option("--batch-size", default=100, help="Max rows to delete in one pass (default 100).")
def trim(database_url: str | None, retention: float, batch_size: int) -> None:
    engine = create_engine_for(_url(database_url))
    try:
        with Channel(
            engine=engine,
            create_schema=False,
            message_retention=retention,
            trim_batch_size=batch_size,
        ) as channel:
            click.echo(f"trimmed {channel.trim()} messages")
    finally:
        dispose_engine(engine)


if __name__ == "__main__":  # pragma: no cover
    main()
