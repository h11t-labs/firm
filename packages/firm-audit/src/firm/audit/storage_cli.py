"""The S3 audit log and archive commands for firm-audit (the SQL commands' S3 counterparts)."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .._core.cli import require_click
from .event_format import canonical

click = require_click("audit")


def storage_options(function: Any) -> Any:
    for decorator in (
        click.option("--bucket", required=True, envvar="FIRM_AUDIT_S3_BUCKET"),
        click.option("--prefix", default="firm-audit", show_default=True),
        click.option("--region", default=None),
        click.option("--endpoint-url", default=None, envvar="FIRM_AUDIT_S3_ENDPOINT"),
    ):
        function = decorator(function)
    return function


def output(value: Any) -> None:
    click.echo(json.dumps(value, default=str, ensure_ascii=True))


@click.group(help="Database-free S3 audit operations. Existing bucket required.")
@storage_options
@click.option("--stream", required=True, envvar="FIRM_AUDIT_S3_STREAM")
@click.pass_context
def s3(ctx: Any, stream: str, **config: Any) -> None:
    from .s3 import S3AuditLog

    ctx.obj = S3AuditLog(stream=stream, **config)
    ctx.call_on_close(ctx.obj.close)


@s3.command(help="Prepare an event without writing to S3. Keep this file for durable retries.")
@click.argument("action")
@click.option("--event-file", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--data", default="{}", help="JSON event data.")
@click.pass_obj
def prepare(log: Any, action: str, event_file: Path, data: str) -> None:
    payload = json.loads(data)
    if not isinstance(payload, dict):
        raise click.BadParameter("--data must be a JSON object")
    prepared = log.prepare(action, data=payload)
    # Exclusive create avoids overwriting the retry identity after an unknown result.
    with event_file.open("x", encoding="utf-8") as handle:
        handle.write(canonical(asdict(prepared)).decode() + "\n")
    output({"prepared": str(event_file), "entry_id": prepared.entry_id})


@s3.command(help="Write/retry the exact prepared event, then print a receipt.")
@click.argument("event_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_obj
def write(log: Any, event_file: Path) -> None:
    from .s3 import PreparedEvent

    output(asdict(log.write(PreparedEvent(**json.loads(event_file.read_text())))))


@s3.command(help="Retrieve one event by id or object key; receipt fields verify a version.")
@click.argument("locator")
@click.option("--sha256", default=None)
@click.option("--version-id", default=None)
@click.pass_obj
def get(log: Any, locator: str, sha256: str | None, version_id: str | None) -> None:
    from .object_store import Receipt

    if version_id and not sha256:
        raise click.BadParameter("--version-id requires --sha256")
    if locator.isdigit():
        output(log.get(int(locator)))
    else:
        output(log.get(Receipt(locator, sha256, version_id) if sha256 else locator))


@s3.command(help="Show the total number of recorded events.")
@click.pass_obj
def stats(log: Any) -> None:
    click.echo(f"events: {log.storage.count_events()}")


@s3.command("history", help="List recent events, optionally filtered.")
@click.option("--action", default=None, help="Filter by action.")
@click.option("--subject-type", default=None, help="Filter by subject type.")
@click.option("--subject-id", default=None, help="Filter by subject id.")
@click.option("--actor-type", default=None, help="Filter by actor type.")
@click.option("--actor-id", default=None, help="Filter by actor id.")
@click.option("--correlation-id", default=None, help="Filter by correlation id.")
@click.option("--since", default=None, help="Only events created at or after this ISO time.")
@click.option("--limit", default=20, show_default=True, help="Max rows to show.")
@click.pass_obj
def s3_history(log: Any, since: str | None, **filters: Any) -> None:
    from .cli import print_history

    print_history(log.history(since=datetime.fromisoformat(since) if since else None, **filters))


@s3.command(help="Delete events older than max_age, exactly as `firm-audit prune` does.")
@click.option(
    "--max-age", default=None, type=float, help="Override max_age in seconds for this run."
)
@click.pass_obj
def prune(log: Any, max_age: float | None) -> None:
    from .cli import run_prune

    run_prune(log, max_age)


@s3.command(help="Seal the backlog of past-grace events (Layer 2). Needs a key.")
@click.pass_obj
def seal(log: Any) -> None:
    from .cli import run_seal

    run_seal(log)


@s3.command("anchor-compact", help="Compact a mutable anchor to one signed CHECKPOINT.")
@click.option("--anchor", "anchor_path", default=None, help="Defaults to FIRM_AUDIT_ANCHOR_PATH.")
@click.pass_obj
def anchor_compact(log: Any, anchor_path: str | None) -> None:
    from .cli import run_anchor_compact

    run_anchor_compact(log, anchor_path)


@s3.command(help="Verify row MACs, independent seals, markers, and anchor.")
@click.option("--anchor", "anchor_path", default=None, help="Anchor file to check (Layer 3).")
@click.option("--full", is_flag=True, help="Recompute every sealed range.")
@click.pass_obj
def verify(log: Any, anchor_path: str | None, full: bool) -> None:
    from .cli import print_verify

    print_verify(log.verify(anchor_path=anchor_path, full=full))


@s3.command("flush-outbox", help="Publish events staged by record(conn=...) in a database.")
@click.option("--database-url", required=True, envvar="FIRM_AUDIT_DATABASE_URL")
@click.option("--limit", default=1000, show_default=True)
@click.pass_obj
def flush_outbox(log: Any, database_url: str, limit: int) -> None:
    from .._core.database import create_engine_for, dispose_engine

    engine = create_engine_for(database_url)
    try:
        click.echo(f"published {log.flush_outbox(engine, limit=limit)} events")
    finally:
        dispose_engine(engine)


@click.group(help="Copy, verify, restore, and explicitly enable archive-before-prune.")
@storage_options
@click.pass_context
def archive(ctx: Any, **config: Any) -> None:
    from .archive import Archive
    from .object_store import ObjectStore

    ctx.obj = Archive(ObjectStore(**config))
    ctx.call_on_close(ctx.obj.close)


@archive.command("export-sql", help="Copy a bounded SQL selection without changing retention.")
@click.option("--database-url", required=True, envvar="FIRM_AUDIT_DATABASE_URL")
@click.option("--stream", required=True)
@click.option("--batch-id", required=True)
@click.option("--limit", default=100, type=int)
@click.option("--since", default=None)
@click.option("--until", default=None)
@click.option("--continuation", default=None)
@click.pass_obj
def export_sql(
    storage: Any,
    database_url: str,
    stream: str,
    batch_id: str,
    limit: int,
    since: str | None,
    until: str | None,
    continuation: str | None,
) -> None:
    from .log import AuditLog

    with AuditLog(database_url, create_schema=False) as audit:
        receipt = storage.export_sql(
            audit,
            stream=stream,
            batch_id=batch_id,
            limit=limit,
            since=datetime.fromisoformat(since) if since else None,
            until=datetime.fromisoformat(until) if until else None,
            continuation=continuation,
        )
    output(asdict(receipt))


@archive.command("run-sql", help="Publish one durable SQL batch, optionally activate prune guard.")
@click.option("--database-url", required=True, envvar="FIRM_AUDIT_DATABASE_URL")
@click.option("--stream", required=True)
@click.option("--batch-size", default=100, type=int)
@click.option(
    "--enable-prune-guard",
    is_flag=True,
    help="Persist policy; all future pruning requires verified archival. Upgrade pruners first.",
)
@click.pass_obj
def run_sql(
    storage: Any, database_url: str, stream: str, batch_size: int, enable_prune_guard: bool
) -> None:
    from .log import AuditLog
    from .sql_archive import SQLArchiver

    with AuditLog(database_url, create_schema=False) as audit:
        worker = SQLArchiver(audit, storage, stream=stream)
        if enable_prune_guard:
            worker.enable()
        receipt = worker.run_once(batch_size=batch_size)
        output({"receipt": asdict(receipt) if receipt else None, "status": worker.status()})


@archive.command("run-s3", help="Publish one durable batch of an S3 audit log's events.")
@click.option("--source-bucket", required=True)
@click.option("--source-prefix", default="firm-audit")
@click.option("--stream", required=True)
@click.option("--batch-size", default=100, type=int)
@click.option(
    "--enable-prune-guard",
    is_flag=True,
    help="Persist policy; all future pruning requires verified archival. Upgrade pruners first.",
)
@click.pass_obj
def run_s3(
    storage: Any,
    source_bucket: str,
    source_prefix: str,
    stream: str,
    batch_size: int,
    enable_prune_guard: bool,
) -> None:
    from .s3 import S3AuditLog
    from .sql_archive import Archiver

    with S3AuditLog(
        source_bucket, stream=stream, prefix=source_prefix, client=storage.store.client
    ) as source:
        worker = Archiver(source, storage)
        if enable_prune_guard:
            worker.enable()
        receipt = worker.run_once(batch_size=batch_size)
        output({"receipt": asdict(receipt) if receipt else None, "status": worker.status()})


@archive.command("export-s3", help="Copy a bounded S3 selection by id; original events remain.")
@click.option("--source-bucket", required=True)
@click.option("--source-prefix", default="firm-audit")
@click.option("--stream", required=True)
@click.option("--batch-id", required=True)
@click.option("--limit", default=100, type=int)
@click.option("--since", default=None)
@click.option("--until", default=None)
@click.option("--continuation", default=None)
@click.pass_obj
def export_s3(
    storage: Any,
    source_bucket: str,
    source_prefix: str,
    stream: str,
    batch_id: str,
    limit: int,
    since: str | None,
    until: str | None,
    continuation: str | None,
) -> None:
    from .s3 import S3AuditLog

    # Reuse the configured endpoint/client so compatible providers work on both sides.
    with S3AuditLog(
        source_bucket, stream=stream, prefix=source_prefix, client=storage.store.client
    ) as source:
        receipt = storage.export_s3(
            source,
            batch_id=batch_id,
            limit=limit,
            since=datetime.fromisoformat(since) if since else None,
            until=datetime.fromisoformat(until) if until else None,
            continuation=continuation,
        )
    output(asdict(receipt))


@archive.command(help="Resume the exact persisted input for a batch.")
@click.argument("batch_id")
@click.pass_obj
def resume(storage: Any, batch_id: str) -> None:
    output(asdict(storage.resume(batch_id)))


@archive.command(help="Verify and read a completed manifest (source envelopes as JSON).")
@click.argument("manifest")
@click.option("--sha256", default=None)
@click.option("--version-id", default=None)
@click.pass_obj
def read(storage: Any, manifest: str, sha256: str | None, version_id: str | None) -> None:
    from .object_store import Receipt

    if version_id and not sha256:
        raise click.BadParameter("--version-id requires --sha256")
    output(storage.read(Receipt(manifest, sha256, version_id) if sha256 else manifest))


@archive.command("inspect", help="Verify the manifest and show source scope/continuation metadata.")
@click.argument("manifest")
@click.pass_obj
def inspect_archive(storage: Any, manifest: str) -> None:
    output(storage.inspect(manifest))


@archive.command("read-many", help="Read overlapping manifests, deduplicating source identities.")
@click.argument("manifests", nargs=-1, required=True)
@click.option("--max-events", default=10000, type=int)
@click.pass_obj
def read_many(storage: Any, manifests: tuple[str, ...], max_events: int) -> None:
    output(storage.read_many(list(manifests), max_events=max_events))


@archive.command("restore-sql", help="Restore archived events into an empty isolated SQL database.")
@click.argument("manifests", nargs=-1, required=True)
@click.option("--target-url", required=True)
@click.pass_obj
def restore_sql(storage: Any, manifests: tuple[str, ...], target_url: str) -> None:
    from .log import AuditLog
    from .sql_archive import restore_sql as restore

    with AuditLog(target_url) as target:
        output({"restored": restore(storage, list(manifests), target)})


@archive.command(
    "restore-s3", help="Restore an archived batch to S3, preserving source provenance."
)
@click.argument("manifest")
@click.option("--target-bucket", required=True)
@click.option("--target-prefix", default="firm-audit")
@click.option("--stream", required=True)
@click.pass_obj
def restore_s3(
    storage: Any, manifest: str, target_bucket: str, target_prefix: str, stream: str
) -> None:
    from .s3 import S3AuditLog

    with S3AuditLog(
        target_bucket, stream=stream, prefix=target_prefix, client=storage.store.client
    ) as target:
        output({"restored": storage.restore_s3(manifest, target)})
