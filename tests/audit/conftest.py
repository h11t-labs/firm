"""Shared fixtures for the audit tests.

Runs against SQLite by default; also against live Postgres/MySQL when ``FIRM_TEST_PG_URL``
/ ``FIRM_TEST_MYSQL_URL`` are set (fresh schema per test).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime
from unittest.mock import patch

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


@pytest.fixture(autouse=True)
def _no_ambient_audit_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a developer's shell FIRM_AUDIT_KEY / retired keys out of every test; a test that
    wants a key sets it explicitly (AuditLog reads it independently of storage)."""
    monkeypatch.delenv("FIRM_AUDIT_KEY", raising=False)
    monkeypatch.delenv("FIRM_AUDIT_RETIRED_KEYS", raising=False)


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


@pytest.fixture
def at_time() -> Callable[[datetime], AbstractContextManager[None]]:
    """Insert genuinely-old signed rows by controlling the *write* clock.

    Aging used to be faked by ``UPDATE``ing ``created_at`` after insert — but that is itself a
    MAC-invalidating edit, so now that retention re-verifies a range before pruning it ("retention
    only prunes what verifies") a mutated row reads as tampering and pruning refuses. The clean
    convention is to make the row old *at insert time*: ``events.append`` is the sole insert path
    and stamps ``created_at`` from :func:`now_utc`, feeding that same value into the row's MAC.
    Patching it makes the stored timestamp and the signed timestamp one past value, so the row is
    genuinely old and still verifies. The sealer and retention read the real clock, so a past-dated
    row is immediately eligible to seal (``created_at <= now - grace``) and expired against
    ``max_age``.

    Usage::

        with at_time(now_utc() - timedelta(hours=2)):
            audit.record("old")  # signed at, and stored with, the past timestamp
    """

    @contextmanager
    def _at(when: datetime) -> Iterator[None]:
        with patch("firm.audit.events.now_utc", lambda: when):
            yield

    return _at


class S3Error(Exception):
    """SDK-shaped errors for failure injection, without requiring AWS credentials."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class MemoryS3:
    """Versioned object test double with S3's conditional-write semantics (``If-None-Match``,
    ``If-Match`` on ETags), delete markers, and controllable request failures."""

    def __init__(self) -> None:
        import threading

        # Each key maps to its versions; ``None`` is a delete marker.
        self.objects: dict[tuple[str, str], list[bytes | None]] = {}
        self.lock = threading.Lock()
        self.fail_key: str | None = None
        self.fail_after_put = False
        self.request_count = 0

    @staticmethod
    def _etag(body: bytes) -> str:
        import hashlib

        return '"' + hashlib.md5(body, usedforsecurity=False).hexdigest() + '"'

    def _current(self, key: tuple[str, str]) -> bytes | None:
        versions = self.objects.get(key)
        return versions[-1] if versions else None

    def put_object(self, **kwargs):
        with self.lock:
            self.request_count += 1
            key = (kwargs["Bucket"], kwargs["Key"])
            if self.fail_key and self.fail_key in key[1]:
                raise S3Error("ServiceUnavailable")
            current = self._current(key)
            if kwargs.get("IfNoneMatch") == "*" and current is not None:
                raise S3Error("PreconditionFailed")
            if "IfMatch" in kwargs and (
                current is None or self._etag(current) != kwargs["IfMatch"]
            ):
                raise S3Error("PreconditionFailed")
            self.objects.setdefault(key, []).append(kwargs["Body"])
            if self.fail_after_put:
                self.fail_after_put = False
                raise TimeoutError("injected timeout after accepted PUT")
            return {"VersionId": str(len(self.objects[key])), "ETag": self._etag(kwargs["Body"])}

    def get_object(self, **kwargs):
        import io

        with self.lock:
            self.request_count += 1
            key = (kwargs["Bucket"], kwargs["Key"])
            if self.fail_key and self.fail_key in key[1]:
                raise S3Error("ServiceUnavailable")
            versions = self.objects.get(key)
            if "VersionId" in kwargs:
                index = int(kwargs["VersionId"])
                if not versions or index > len(versions) or versions[index - 1] is None:
                    raise S3Error("NoSuchVersion")
                body = versions[index - 1]
                version = index
            else:
                body = versions[-1] if versions else None
                if body is None:
                    raise S3Error("NoSuchKey")
                version = len(versions)
            assert body is not None
            return {"Body": io.BytesIO(body), "VersionId": str(version), "ETag": self._etag(body)}

    def head_object(self, **kwargs):
        with self.lock:
            self.request_count += 1
            if self._current((kwargs["Bucket"], kwargs["Key"])) is None:
                raise S3Error("404")
            return {}

    def delete_objects(self, **kwargs):
        with self.lock:
            self.request_count += 1
            for item in kwargs["Delete"]["Objects"]:
                key = (kwargs["Bucket"], item["Key"])
                if self._current(key) is not None:
                    self.objects[key].append(None)
            return {}

    def list_objects_v2(self, **kwargs):
        with self.lock:
            self.request_count += 1
            keys = sorted(
                k
                for (bucket, k), versions in self.objects.items()
                if bucket == kwargs["Bucket"]
                and versions[-1] is not None
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
    from firm.audit import S3Storage

    with AuditLog(
        storage=S3Storage(
            "events",
            stream="00000000-0000-0000-0000-000000000001",
            client=object_client,
        )
    ) as log:
        yield log


@pytest.fixture
def archive(object_client):
    from firm.audit.archive import Archive
    from firm.audit.object_store import ObjectStore

    return Archive(ObjectStore("archives", client=object_client))
