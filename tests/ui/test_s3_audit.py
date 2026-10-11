"""Read-only S3 dashboard queries use bounded storage capabilities without SQL engines."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen
from uuid import UUID, uuid4

import pytest

from firm.audit.archive import Archive
from firm.audit.object_store import ObjectStore
from firm.audit.s3 import S3AuditLog
from firm.ui import build_dashboard, create_server
from tests.audit.conftest import MemoryS3


@pytest.fixture
def s3_dashboard_url():
    client = MemoryS3()
    log = S3AuditLog("events", stream=str(UUID(int=1)), client=client)
    receipt = log.record("<script>unsafe</script>", created_at=datetime(2026, 1, 1, tzinfo=UTC))
    archive = Archive(ObjectStore("archives", client=client))
    archived = archive.export_s3(
        log,
        start=datetime(2026, 1, 1).date(),
        end=datetime(2026, 1, 2).date(),
        batch_id=str(uuid4()),
    )
    dash = build_dashboard(audit_s3=log, audit_archive=archive)
    assert dash.parts == ["audit"] and dash.audit is None
    server = create_server(dash, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", receipt.key, archived.key
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        dash.close()


def test_s3_history_and_detail_are_escaped(s3_dashboard_url):
    base, key, _ = s3_dashboard_url
    with urlopen(base + "/audit?start=2026-01-01&end=2026-01-02") as response:
        body = response.read().decode()
        assert "1 matching events" in body
        assert "&lt;script&gt;unsafe&lt;/script&gt;" in body
        assert "<script>unsafe</script>" not in body
        assert "End of selected range" in body
    with urlopen(base + "/audit/object?" + urlencode({"key": key})) as response:
        assert "&lt;script&gt;unsafe&lt;/script&gt;" in response.read().decode()


def test_archive_view_and_bad_cursor(s3_dashboard_url):
    base, _, key = s3_dashboard_url
    with urlopen(base + "/audit/archive?" + urlencode({"key": key})) as response:
        assert "1 verified events" in response.read().decode()
    with pytest.raises(HTTPError) as caught:
        urlopen(base + "/audit?start=2026-01-01&end=2026-01-02&continuation=invalid")
    assert caught.value.code == 400
