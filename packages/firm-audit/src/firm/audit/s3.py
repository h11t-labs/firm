"""Compatibility constructor for AuditLog(storage=S3Storage(...)).

Signing, recording, queries, sealing, verification and retention belong to AuditLog. This
module retains the pre-release S3 constructor and retry imports for existing callers.
"""

from __future__ import annotations

from typing import Any

from .log import AuditLog
from .object_store import ObjectStore
from .prepared import PreparedEvent, UnknownEventOutcome
from .storage import StorageBusy
from .storage.s3 import S3Storage

__all__ = ["PreparedEvent", "S3AuditLog", "S3Storage", "StorageBusy", "UnknownEventOutcome"]


class S3AuditLog(AuditLog):
    """Compatibility shorthand; new code can select S3Storage on AuditLog directly."""

    def __init__(
        self,
        bucket: str,
        *,
        stream: str,
        prefix: str = "firm-audit",
        region: str | None = None,
        endpoint_url: str | None = None,
        client: Any = None,
        workers: int = 16,
        lease_ttl: float = 300.0,
        lease_wait: float = 5.0,
        **options: Any,
    ) -> None:
        storage = S3Storage(
            bucket,
            stream=stream,
            prefix=prefix,
            region=region,
            endpoint_url=endpoint_url,
            client=client,
            workers=workers,
            lease_ttl=lease_ttl,
            lease_wait=lease_wait,
        )
        try:
            super().__init__(storage=storage, **options)
        except BaseException:
            storage.close()
            raise

    @property
    def store(self) -> ObjectStore:
        return self._s3_storage.store

    @property
    def stream(self) -> str:
        return self._s3_storage.stream

    @property
    def _s3_storage(self) -> S3Storage:
        assert isinstance(self.storage, S3Storage)
        return self.storage
