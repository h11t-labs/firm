"""Shared fixtures for the audit tests.

Runs against SQLite by default; also against live Postgres/MySQL when ``FIRM_TEST_PG_URL``
/ ``FIRM_TEST_MYSQL_URL`` are set (fresh schema per test).
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from firm._core.database import create_engine_for
from firm.audit import AuditLog, schema


def _backend_params() -> list:
    params = [pytest.param("sqlite", id="sqlite")]
    if pg := os.environ.get("FIRM_TEST_PG_URL"):
        params.append(pytest.param(pg, id="postgres"))
    if my := os.environ.get("FIRM_TEST_MYSQL_URL"):
        params.append(pytest.param(my, id="mysql"))
    return params


@pytest.fixture(params=_backend_params())
def backend(request) -> str:
    return request.param


@pytest.fixture
def db_url(backend: str, tmp_path) -> str:
    if backend == "sqlite":
        return f"sqlite:///{tmp_path / 'audit.db'}"
    eng = create_engine_for(backend)
    schema.drop_all(eng)
    schema.create_all(eng)
    eng.dispose()
    return backend


@pytest.fixture
def is_sqlite(db_url: str) -> bool:
    return db_url.startswith("sqlite")


@pytest.fixture
def audit(db_url: str) -> Iterator[AuditLog]:
    log = AuditLog(database_url=db_url)
    try:
        yield log
    finally:
        log.close()


class S3Error(Exception):
    """SDK-shaped errors for failure injection, without requiring AWS credentials."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class MemoryS3:
    """Immutable/versioned object test double with controllable request failures."""

    def __init__(self) -> None:
        import threading

        self.objects: dict[tuple[str, str], list[bytes]] = {}
        self.lock = threading.Lock()
        self.fail_key: str | None = None
        self.fail_after_put = False
        self.request_count = 0

    def put_object(self, **kwargs):
        with self.lock:
            self.request_count += 1
            key = (kwargs["Bucket"], kwargs["Key"])
            if self.fail_key and self.fail_key in key[1]:
                raise S3Error("ServiceUnavailable")
            if key in self.objects and kwargs.get("IfNoneMatch") == "*":
                raise S3Error("PreconditionFailed")
            self.objects.setdefault(key, []).append(kwargs["Body"])
            if self.fail_after_put:
                self.fail_after_put = False
                raise TimeoutError("injected timeout after accepted PUT")
            return {"VersionId": str(len(self.objects[key]))}

    def get_object(self, **kwargs):
        import io

        with self.lock:
            self.request_count += 1
            key = (kwargs["Bucket"], kwargs["Key"])
            if self.fail_key and self.fail_key in key[1]:
                raise S3Error("ServiceUnavailable")
            if key not in self.objects:
                raise S3Error("NoSuchKey")
            version = int(kwargs.get("VersionId", len(self.objects[key])))
            return {"Body": io.BytesIO(self.objects[key][version - 1]), "VersionId": str(version)}

    def list_objects_v2(self, **kwargs):
        with self.lock:
            self.request_count += 1
            keys = sorted(
                k
                for bucket, k in self.objects
                if bucket == kwargs["Bucket"]
                and k.startswith(kwargs["Prefix"])
                and k > kwargs.get("StartAfter", "")
            )
            limit = kwargs["MaxKeys"]
            return {
                "Contents": [{"Key": k} for k in keys[:limit]],
                "IsTruncated": len(keys) > limit,
            }


@pytest.fixture
def object_client():
    return MemoryS3()


@pytest.fixture
def s3_log(object_client):
    from firm.audit.s3 import S3AuditLog

    return S3AuditLog("events", stream="00000000-0000-0000-0000-000000000001", client=object_client)


@pytest.fixture
def archive(object_client):
    from firm.audit.archive import Archive
    from firm.audit.object_store import ObjectStore

    return Archive(ObjectStore("archives", client=object_client))
