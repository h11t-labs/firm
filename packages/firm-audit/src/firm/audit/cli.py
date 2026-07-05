"""Command-line entry point: ``firm-audit stats|history|prune``."""

from __future__ import annotations

from sqlalchemy import func, select

from .._core.cli import db_option, require_click, require_url
from .._core.database import create_engine_for, dispose_engine, transaction
from . import __version__, schema
from .events import history as query_history
from .log import AuditLog

click = require_click("audit")

_db_option = db_option("FIRM_AUDIT_DATABASE_URL")


def _url(database_url: str | None) -> str:
    return require_url(database_url, "FIRM_AUDIT_DATABASE_URL")


@click.group(help="firm-audit — append-only, database-backed audit log.")
@click.version_option(__version__, prog_name="firm-audit")
def main() -> None:
    pass


@main.command(help="Show the total number of recorded events.")
@_db_option
def stats(database_url: str | None) -> None:
    engine = create_engine_for(_url(database_url))
    try:
        with transaction(engine) as conn:
            count = conn.execute(select(func.count()).select_from(schema.audits)).scalar_one()
    finally:
        dispose_engine(engine)
    click.echo(f"events: {count}")


def _ref_str(kind: str | None, ident: str | None, label: str | None) -> str:
    """Render a reference for the terminal: ``Type:id (name)``, dropping any part that's absent —
    ``Type`` for a label-only ref, ``-`` when nothing was recorded."""
    if kind:
        base: str | None = f"{kind}:{ident}" if ident else kind
    else:
        base = ident
    if label:
        return f"{base} ({label})" if base else label
    return base or "-"


@main.command(help="List recent events, optionally filtered.")
@_db_option
@click.option("--action", default=None, help="Filter by action.")
@click.option("--subject-type", default=None, help="Filter by subject type.")
@click.option("--subject-id", default=None, help="Filter by subject id.")
@click.option("--actor-type", default=None, help="Filter by actor type.")
@click.option("--actor-id", default=None, help="Filter by actor id.")
@click.option("--correlation-id", default=None, help="Filter by correlation id.")
@click.option("--limit", default=20, show_default=True, help="Max rows to show.")
def history(
    database_url: str | None,
    action: str | None,
    subject_type: str | None,
    subject_id: str | None,
    actor_type: str | None,
    actor_id: str | None,
    correlation_id: str | None,
    limit: int,
) -> None:
    engine = create_engine_for(_url(database_url))
    try:
        with transaction(engine) as conn:
            rows = query_history(
                conn,
                action=action,
                subject_type=subject_type,
                subject_id=subject_id,
                actor_type=actor_type,
                actor_id=actor_id,
                correlation_id=correlation_id,
                limit=limit,
            )
    finally:
        dispose_engine(engine)
    for row in rows:
        subject_s = _ref_str(row["subject_type"], row["subject_id"], row["subject_label"])
        actor_s = _ref_str(row["actor_type"], row["actor_id"], row["actor_label"])
        click.echo(f"{row['created_at']}  {row['action']}  subject={subject_s}  actor={actor_s}")


@main.command(help="Delete events older than the audit log's max_age. No-op if unset.")
@_db_option
@click.option(
    "--max-age", default=None, type=float, help="Override max_age in seconds for this run."
)
def prune(database_url: str | None, max_age: float | None) -> None:
    engine = create_engine_for(_url(database_url))
    try:
        with AuditLog(engine=engine, create_schema=False) as audit:
            if max_age is not None:
                audit.max_age = max_age
            click.echo(f"pruned {audit.retention.run_once()} events")
    finally:
        dispose_engine(engine)


if __name__ == "__main__":  # pragma: no cover
    main()
