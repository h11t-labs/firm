"""Bounded, immutable S3 operations shared by audit events and archives."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

MAX_OBJECT_BYTES = 16 * 1024 * 1024


class ObjectConflict(ValueError):
    """The immutable key already contains different bytes."""


class ObjectUnavailable(RuntimeError):
    """The object exists but is not currently retrievable (for example Glacier)."""


class UnknownWriteOutcome(RuntimeError):
    """A write could not be reconciled; retry the same key and bytes."""


@dataclass(frozen=True)
class Receipt:
    """A version-aware locator and digest; safe to persist as JSON."""

    key: str
    sha256: str
    version_id: str | None = None


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def error_code(exc: Exception) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))


class ObjectStore:
    """Use an existing bucket. No provisioning, deletion, or credential persistence."""

    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = "firm-audit",
        region: str | None = None,
        endpoint_url: str | None = None,
        client: Any = None,
    ) -> None:
        if not bucket or not prefix.strip("/") or ".." in prefix.split("/"):
            raise ValueError("bucket and a nonempty, safe prefix are required")
        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/"
        self._owns_client = client is None
        if client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:
                raise ImportError('Install "firm-audit[s3]" to use S3 storage') from exc
            client = boto3.client(
                "s3",
                region_name=region,
                endpoint_url=endpoint_url,
                config=Config(
                    connect_timeout=5,
                    read_timeout=15,
                    retries={"mode": "standard", "total_max_attempts": 3},
                ),
            )
        self.client = client
        self.endpoint_url = endpoint_url or str(
            getattr(getattr(client, "meta", None), "endpoint_url", "injected")
        )

    @property
    def identity(self) -> str:
        """Bind persisted archive policy/cursors to this destination."""
        return f"{self.endpoint_url}|{self.bucket}|{self.prefix}"

    def _check_key(self, key: str) -> None:
        if not key.startswith(self.prefix) or ".." in key.split("/"):
            raise ValueError("object key is outside the configured prefix")

    def read(self, key: str, *, version_id: str | None = None) -> tuple[bytes, str | None]:
        self._check_key(key)
        kwargs: dict[str, Any] = {"Bucket": self.bucket, "Key": key}
        if version_id is not None:
            kwargs["VersionId"] = version_id
        try:
            response = self.client.get_object(**kwargs)
        except Exception as exc:
            if error_code(exc) in {"NoSuchKey", "NoSuchVersion", "404"}:
                raise FileNotFoundError(key) from exc
            if error_code(exc) == "InvalidObjectState":
                raise ObjectUnavailable("Restore the archived object before reading it") from exc
            raise
        body = response["Body"]
        try:
            data = body.read(MAX_OBJECT_BYTES + 1)
        finally:
            body.close()
        if len(data) > MAX_OBJECT_BYTES:
            raise ValueError("object exceeds the audit size limit")
        return data, response.get("VersionId")

    def verify(self, receipt: Receipt) -> bytes:
        data, _ = self.read(receipt.key, version_id=receipt.version_id)
        if digest(data) != receipt.sha256:
            raise ObjectConflict("object digest does not match receipt")
        return data

    def put(self, key: str, data: bytes, *, content_type: str = "application/json") -> Receipt:
        self._check_key(key)
        if len(data) > MAX_OBJECT_BYTES:
            raise ValueError("object exceeds the audit size limit")
        try:
            result = self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                IfNoneMatch="*",
                ContentType=content_type,
            )
            return Receipt(key, digest(data), result.get("VersionId"))
        except Exception as exc:
            code = error_code(exc)
            if code in {
                "AccessDenied",
                "InvalidAccessKeyId",
                "SignatureDoesNotMatch",
                "NoSuchBucket",
                "InvalidRequest",
                "NotImplemented",
            }:
                raise
            try:
                previous, version = self.read(key)
            except Exception as read_exc:
                raise UnknownWriteOutcome(f"Unable to reconcile write to {key}") from read_exc
            if previous != data:
                raise ObjectConflict(f"Different content already exists at {key}") from exc
            return Receipt(key, digest(data), version)

    def list_keys(
        self, prefix: str, *, after: str = "", limit: int = 100
    ) -> tuple[list[str], bool]:
        self._check_key(prefix)
        if after and not after.startswith(prefix):
            raise ValueError("cursor is outside the requested prefix")
        result = self.client.list_objects_v2(
            Bucket=self.bucket,
            Prefix=prefix,
            StartAfter=after,
            MaxKeys=min(limit, 1000),
        )
        keys = [entry["Key"] for entry in result.get("Contents", [])]
        if any(not k.startswith(prefix) or k <= after for k in keys) or keys != sorted(keys):
            raise ValueError("provider returned unsupported LIST ordering")
        if not keys and result.get("IsTruncated"):
            raise ValueError("provider returned an empty truncated page")
        return keys, not result.get("IsTruncated", False)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()
