"""firm-audit — an append-only, storage-independent audit log for Python.

from firm.audit import AuditLog, record

log = AuditLog(database_url="sqlite:///audit.db")
log.record("invoice.paid", subject=invoice, actor=user, data={"amount": 4200})
log.history(action="invoice.paid")

# shared-DB, same-transaction (atomic with the business change):
with engine.begin() as conn:
    mark_invoice_paid(conn, invoice_id)
    record(conn, "invoice.paid", subject=invoice, actor=user, data={"amount": 4200})
"""

from __future__ import annotations

from .events import Ref
from .log import AuditLog, record
from .prepared import PreparedEvent, UnknownEventOutcome
from .storage import SqlStorage, Storage
from .storage.s3 import S3Storage
from .verify import IntegrityAlert

__version__ = "1.0.0"

__all__ = [
    "AuditLog",
    "IntegrityAlert",
    "PreparedEvent",
    "Ref",
    "S3Storage",
    "SqlStorage",
    "Storage",
    "UnknownEventOutcome",
    "__version__",
    "record",
]
