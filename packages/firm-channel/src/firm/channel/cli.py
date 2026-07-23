"""Command-line entry point: ``firm-channel stats|trim``."""

from __future__ import annotations

from .._core.cli import db_option, require_click, require_url
from .._core.database import create_engine_for, dispose_engine, transaction
from . import __version__, messages
from .channel import Channel

click = require_click("channel")

_db_option = db_option("FIRM_CHANNEL_DATABASE_URL")


def _url(database_url: str | None) -> str:
    return require_url(database_url, "FIRM_CHANNEL_DATABASE_URL")


@click.group(help="firm-channel — database-backed pub/sub.")
@click.version_option(__version__, prog_name="firm-channel")
def main() -> None:
    pass


@main.command(help="Show the number of buffered messages and their estimated payload size.")
@_db_option
def stats(database_url: str | None) -> None:
    engine = create_engine_for(_url(database_url))
    try:
        with transaction(engine) as conn:
            click.echo(f"messages: {messages.message_count(conn)}")
            click.echo(f"payload_size: {messages.payload_bytes(conn)} bytes")
    finally:
        dispose_engine(engine)


@main.command(help="Delete messages older than the retention window and exit.")
@_db_option
@click.option("--retention", default=86400.0, show_default=True, help="Retention in seconds.")
@click.option(
    "--batch-size", default=100, show_default=True, help="Max rows to delete in one pass."
)
def trim(database_url: str | None, retention: float, batch_size: int) -> None:
    engine = create_engine_for(_url(database_url))
    try:
        with Channel(
            engine=engine,
            create_schema=False,
            auto_trim=False,
            message_retention=retention,
            trim_batch_size=batch_size,
        ) as channel:
            click.echo(f"trimmed {channel.trim()} messages")
    finally:
        dispose_engine(engine)


if __name__ == "__main__":  # pragma: no cover
    main()
