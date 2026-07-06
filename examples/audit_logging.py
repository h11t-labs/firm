"""Audit logging: same-transaction events, a queue job's lifecycle, and querying history.

uv run python examples/audit_logging.py
"""

from __future__ import annotations

from sqlalchemy import Column, Integer, MetaData, String, Table

import firm.queue as bq
from firm.audit import AuditLog, Ref, record
from firm.queue import current_runtime
from firm.queue import schema as queue_schema
from firm.queue.worker import run_ready

DB = "sqlite:///firm-audit.db"

bq.configure(database_url=DB)
audit = AuditLog(database_url=DB)  # same database -> the same-transaction path is available

# a tiny "business" table to demonstrate the same-transaction guarantee against
metadata = MetaData()
invoices = Table(
    "demo_invoices",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("status", String(20)),
)


def mark_invoice_paid(invoice_id: int) -> None:
    # the audit row commits (or rolls back) together with the invoice update -- one
    # transaction, so a recorded event can never exist without the change it describes.
    with audit.engine.begin() as conn:
        conn.execute(invoices.update().where(invoices.c.id == invoice_id).values(status="paid"))
        # actor as a Ref carrying a display name: the "alice@example.com" label is captured now,
        # so the row stays legible even if user 7 is later deleted or renamed.
        record(
            conn,
            "invoice.paid",
            subject=("Invoice", invoice_id),
            actor=Ref("User", 7, name="alice@example.com"),
            changes={"status": ["pending", "paid"]},
            data={"amount": 4200},
        )


@bq.job()
def export_report(report_id: int) -> None:
    # logging a job's own lifecycle: each event tagged with a correlation_id so the whole
    # run shows up together in audit.history(correlation_id=...). The actor is a bare "worker"
    # label -- a non-entity actor (a role), so it needs no id.
    cid = f"export:{report_id}"
    audit.record(
        "export.started", subject=("Report", report_id), actor="worker", correlation_id=cid
    )
    audit.record(
        "export.finished",
        subject=("Report", report_id),
        actor="worker",
        correlation_id=cid,
        data={"rows": 100},
    )


def main() -> None:
    queue_schema.create_all(current_runtime().engine)  # demo only; use Alembic in production
    metadata.create_all(audit.engine)

    mark_invoice_paid(101)
    export_report.enqueue(7)
    run_ready(current_runtime(), limit=10)  # run the job inline (drains the queue once)

    print("--- invoice.paid (recorded inside the invoice's own transaction) ---")
    for event in audit.history(action="invoice.paid"):
        print(event["created_at"], event["subject_type"], event["subject_id"], event["data"])

    print("--- export job timeline (one correlation_id, oldest first) ---")
    for event in reversed(audit.history(correlation_id="export:7")):
        print(event["created_at"], event["action"], event["data"])

    audit.close()


if __name__ == "__main__":
    main()
