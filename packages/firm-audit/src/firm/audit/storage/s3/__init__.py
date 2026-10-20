"""S3 audit storage with object indexes, leases and an optional SQL outbox."""

from .store import S3Storage

__all__ = ["S3Storage"]
