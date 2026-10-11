"""Explicit S3 recording, bounded scans, and archive operations for firm-audit."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime
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
    output({"prepared": str(event_file), "key": prepared.key})


@s3.command(help="Write/retry the exact prepared event, then print a receipt.")
@click.argument("event_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_obj
def write(log: Any, event_file: Path) -> None:
    from .s3 import PreparedEvent

    output(asdict(log.write(PreparedEvent(**json.loads(event_file.read_text())))))


@s3.command(help="Retrieve an event by key; use receipt fields to pin and verify a version.")
@click.argument("key")
@click.option("--sha256", default=None)
@click.option("--version-id", default=None)
@click.pass_obj
def get(log: Any, key: str, sha256: str | None, version_id: str | None) -> None:
    from .object_store import Receipt

    if version_id and not sha256:
        raise click.BadParameter("--version-id requires --sha256")
    output(log.get(Receipt(key, sha256, version_id) if sha256 else key))


@s3.command(help="Scan UTC date partitions [start,end) within explicit budgets.")
@click.option("--start", required=True)
@click.option("--end", required=True)
@click.option("--limit", default=100, type=int)
@click.option("--max-objects", default=1000, type=int)
@click.option("--max-requests", default=1100, type=int)
@click.option("--continuation", default=None)
@click.option("--action", default=None)
@click.pass_obj
def scan(log: Any, start: str, end: str, **options: Any) -> None:
    output(
        asdict(log.scan(start=date.fromisoformat(start), end=date.fromisoformat(end), **options))
    )


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


@archive.command("export-s3", help="Copy one bounded S3 selection; original events remain.")
@click.option("--source-bucket", required=True)
@click.option("--source-prefix", default="firm-audit")
@click.option("--stream", required=True)
@click.option("--batch-id", required=True)
@click.option("--start", required=True)
@click.option("--end", required=True)
@click.option("--continuation", default=None)
@click.option("--max-objects", default=100, type=int)
@click.pass_obj
def export_s3(
    storage: Any,
    source_bucket: str,
    source_prefix: str,
    stream: str,
    batch_id: str,
    start: str,
    end: str,
    continuation: str | None,
    max_objects: int,
) -> None:
    from .s3 import S3AuditLog

    # Reuse the configured endpoint/client so compatible providers work on both sides.
    with S3AuditLog(
        source_bucket, stream=stream, prefix=source_prefix, client=storage.store.client
    ) as source:
        receipt = storage.export_s3(
            source,
            start=date.fromisoformat(start),
            end=date.fromisoformat(end),
            batch_id=batch_id,
            continuation=continuation,
            max_objects=max_objects,
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
        output([asdict(receipt) for receipt in storage.restore_s3(manifest, target)])
