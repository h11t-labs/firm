"""Read-only S3 dashboard queries use bounded storage capabilities without SQL engines."""

from __future__ import annotations

import html
import re
import threading
from datetime import UTC, date, datetime
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen
from uuid import UUID, uuid4

import pytest

from firm.audit import AuditLog
from firm.audit.archive import Archive
from firm.audit.object_store import ObjectStore
from firm.audit.s3 import S3AuditLog
from firm.ui import build_dashboard, create_server
from firm.ui.app import DashboardApp, Headers, UIRequest
from tests.audit.conftest import MemoryS3

DAY = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def s3_dashboard_url():
    client = MemoryS3()
    log = S3AuditLog("events", stream=str(UUID(int=1)), client=client)
    receipt = log.record("<script>unsafe</script>", created_at=DAY)
    archive = Archive(ObjectStore("archives", client=client))
    archived = archive.export_s3(
        log, start=date(2026, 1, 1), end=date(2026, 1, 2), batch_id=str(uuid4())
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


def _get(app: DashboardApp, path: str, query: str = "", prefix: str = "") -> tuple[int, str]:
    response = app.handle(UIRequest("GET", path, query=query, prefix=prefix))
    return response.status, response.body.decode()


def test_s3_history_and_detail_are_escaped(s3_dashboard_url):
    base, key, _ = s3_dashboard_url
    # "To" is inclusive in the form: one day selects that day's partition.
    with urlopen(base + "/audit/s3?start=2026-01-01&end=2026-01-01") as response:
        body = response.read().decode()
        assert "1 matching event ·" in body
        assert "&lt;script&gt;unsafe&lt;/script&gt;" in body
        assert "<script>unsafe</script>" not in body
        assert "end of selected range" in body
    with urlopen(base + "/audit/object?" + urlencode({"key": key})) as response:
        detail = response.read().decode()
        assert "&lt;script&gt;unsafe&lt;/script&gt;" in detail
        assert 'class="crumb" href="/audit/s3"' in detail


def test_s3_only_audit_tab_lands_on_s3_history_with_its_query():
    client = MemoryS3()
    log = S3AuditLog("events", stream=str(UUID(int=1)), client=client)
    dash = build_dashboard(audit_s3=log)
    try:
        response = DashboardApp(dash).handle(
            UIRequest("GET", "/audit", query="start=2026-01-01&end=2026-01-01", prefix="/ops")
        )
        assert response.status == 303
        assert dict(response.headers)["Location"] == "/ops/audit/s3?start=2026-01-01&end=2026-01-01"
        status, body = _get(DashboardApp(dash), "/audit/s3")
        assert status == 200
        assert '<nav class="sub">' not in body  # one view: nothing to switch between
        assert "No matching events in this scan." in body
    finally:
        dash.close()


def test_archive_view_batch_list_and_bad_cursors(s3_dashboard_url):
    base, _, key = s3_dashboard_url
    with urlopen(base + "/audit/archive?" + urlencode({"key": key})) as response:
        body = response.read().decode()
        assert "1 verified event<" in body
        assert 'Source</span> <span class="source-word">Archive' in body
        assert '<span class="sig receipt"' in body and "SHA-256 receipt" in body
    with urlopen(base + "/audit/archive") as response:
        # Without a key the view lists batches, each linking to its verified view.
        listing = response.read().decode()
        assert 'href="/audit/archive?' + urlencode({"key": key}) + '"' in listing
        assert "1 batch on this page" in listing and ">complete<" in listing
        assert ">S3 receipts<" in listing
    with pytest.raises(HTTPError) as caught:
        urlopen(base + "/audit/archive?continuation=bogus")
    assert caught.value.code == 400
    with pytest.raises(HTTPError) as caught:
        urlopen(base + "/audit/s3?start=2026-01-01&end=2026-01-01&continuation=invalid")
    assert caught.value.code == 400


def test_cli_archive_prefix_matches_the_library_default(monkeypatch):
    # An archive written with ObjectStore's defaults must open without extra dashboard flags.
    import inspect

    from firm.ui import cli

    monkeypatch.delenv("FIRM_AUDIT_ARCHIVE_PREFIX", raising=False)
    args = cli._build_parser().parse_args(["--audit-archive-bucket", "cold"])
    assert args.audit_archive_prefix == inspect.signature(ObjectStore).parameters["prefix"].default


def test_sql_and_s3_audit_side_by_side(tmp_path):
    url = f"sqlite:///{tmp_path / 'audit.db'}"
    with AuditLog(url) as audit:
        audit.record("order.shipped", subject=("Order", 7))
    client = MemoryS3()
    log = S3AuditLog("events", stream=str(UUID(int=1)), client=client)
    log.record("invoice.paid", subject=("Invoice", 42), actor=("User", 3), created_at=DAY)
    log.record("invoice.sent", subject=("Invoice", 43), created_at=DAY)
    archive = Archive(ObjectStore("archives", client=client))
    dash = build_dashboard(audit_url=url, audit_s3=log, audit_archive=archive)
    try:
        assert dash.parts == ["audit"]
        assert dash.audit is not None and dash.audit_s3 is not None
        app = DashboardApp(dash)
        status, sql = _get(app, "/audit")
        assert status == 200 and "order.shipped" in sql
        assert re.search(r'class="active" href="/audit" aria-current="page">Database<', sql)
        assert 'href="/audit/s3">S3<' in sql and 'href="/audit/archive">Archive<' in sql
        assert 'class="source"' not in sql  # the SQL view keeps its own integrity strip

        status, s3 = _get(app, "/audit/s3", "start=2026-01-01&end=2026-01-01")
        assert status == 200 and "invoice.paid" in s3 and "order.shipped" not in s3
        assert 'aria-current="page">S3<' in s3
        assert "firm-audit/" in s3 and "…0001" in s3  # prefix and stream in the source strip
        assert "SHA-256 per object, not signed" in s3
        # Type:id filters mean the same as on the SQL page, and row links build them.
        assert 'href="/audit/s3?start=2026-01-01&end=2026-01-01&subject=Invoice%3A42"' in s3
        status, filtered = _get(
            app, "/audit/s3", "start=2026-01-01&end=2026-01-01&subject=Invoice:42"
        )
        assert "invoice.paid" in filtered and "invoice.sent" not in filtered
        status, by_actor = _get(app, "/audit/s3", "start=2026-01-01&end=2026-01-01&actor=User:3")
        assert "invoice.paid" in by_actor and "invoice.sent" not in by_actor
    finally:
        dash.close()


def test_sql_only_audit_page_has_no_source_switcher(tmp_path):
    url = f"sqlite:///{tmp_path / 'audit.db'}"
    with AuditLog(url) as audit:
        audit.record("order.shipped")
    dash = build_dashboard(audit_url=url)
    try:
        status, body = _get(DashboardApp(dash), "/audit")
        assert status == 200
        assert '<nav class="sub">' not in body and "Read an archive" not in body
    finally:
        dash.close()


def test_mounted_s3_routes_keep_prefix_theme_and_authentication():
    from firm.ui.auth import BasicAuth

    client = MemoryS3()
    log = S3AuditLog("events", stream=str(UUID(int=1)), client=client)
    for _ in range(26):
        receipt = log.record("mounted", created_at=DAY)
    archive = Archive(ObjectStore("archives", client=client))
    archived = archive.export_s3(
        log, start=date(2026, 1, 1), end=date(2026, 1, 2), batch_id=str(uuid4())
    )
    dash = build_dashboard(audit_s3=log, audit_archive=archive)
    try:
        app = DashboardApp(dash)
        headers = Headers([("Cookie", "firm_theme=dark")])
        response = app.handle(
            UIRequest(
                "GET",
                "/audit/s3",
                prefix="/ops",
                query="start=2026-01-01&end=2026-01-01",
                headers=headers,
            )
        )
        assert response.status == 200
        body = response.body.decode()
        assert '<html data-theme="dark">' in body
        assert 'href="/ops/audit/object?' in body
        assert 'action="/ops/audit/s3"' in body
        assert 'href="/ops/audit/archive">Archive<' in body
        # The continuation link resumes the same scan: the inclusive "To" maps to the same
        # exclusive end on every page, so the library accepts the token.
        match = re.search(r'href="/ops(/audit/s3)\?([^"]*continuation=[^"]*)">Continue scan', body)
        assert match is not None
        status, page_two = _get(app, match.group(1), html.unescape(match.group(2)), "/ops")
        assert status == 200 and "1 matching event ·" in page_two
        detail = app.handle(
            UIRequest("GET", "/audit/object", prefix="/ops", query=urlencode({"key": receipt.key}))
        )
        assert detail.status == 200
        assert 'href="/ops/audit/s3"' in detail.body.decode()
        response = app.handle(
            UIRequest(
                "GET", "/audit/archive", prefix="/ops", query=urlencode({"key": archived.key})
            )
        )
        assert response.status == 200
        assert 'action="/ops/audit/archive"' in response.body.decode()
        protected = DashboardApp(dash, authenticator=BasicAuth("ops", password="test-password"))
        for path in ("/audit", "/audit/s3", "/audit/object", "/audit/archive"):
            assert protected.handle(UIRequest("GET", path)).status == 401
    finally:
        dash.close()


def test_archive_batch_list_pages_and_flags_pending_batches():
    client = MemoryS3()
    log = S3AuditLog("events", stream=str(UUID(int=1)), client=client)
    log.record("paged", created_at=DAY)
    archive = Archive(ObjectStore("archives", client=client))
    for _ in range(26):
        archive.export_s3(log, start=date(2026, 1, 1), end=date(2026, 1, 2), batch_id=str(uuid4()))
    archive.store.put(f"{archive.prefix}{'f' * 8}-ffff-4fff-8fff-{'f' * 12}/input.json", b"{}")
    dash = build_dashboard(audit_archive=archive)
    try:
        app = DashboardApp(dash)
        status, first = _get(app, "/audit/archive")
        assert status == 200 and "25 batches on this page" in first
        match = re.search(r'href="(/audit/archive)\?(continuation=[^"]*)">Next', first)
        assert match is not None
        status, second = _get(app, match.group(1), html.unescape(match.group(2)))
        assert status == 200 and "2 batches on this page" in second and "Next" not in second
        assert ">pending<" in second  # the staged batch sorts last ("ffffffff…")
    finally:
        dash.close()


def test_archive_shows_recorded_row_signatures(tmp_path):
    secret = "dashboard-archive-row-secret-0123456789"  # noqa: S105
    client = MemoryS3()
    archive = Archive(ObjectStore("archives", client=client))
    with AuditLog(f"sqlite:///{tmp_path / 'signed.db'}", mac_key=secret) as signed:
        signed.record("signed.one")
        signed_batch = archive.export_sql(signed, stream=str(UUID(int=2)), batch_id=str(uuid4()))
    with AuditLog(f"sqlite:///{tmp_path / 'plain.db'}") as plain:
        plain.record("plain.one")
        plain_batch = archive.export_sql(plain, stream=str(UUID(int=3)), batch_id=str(uuid4()))
    dash = build_dashboard(audit_archive=archive)
    try:
        app = DashboardApp(dash)
        _, body = _get(app, "/audit/archive", urlencode({"key": signed_batch.key}))
        assert '<span class="sig signed"' in body and "signed · key " in body
        _, body = _get(app, "/audit/archive", urlencode({"key": plain_batch.key}))
        assert '<span class="sig unsigned"' in body
        _, listing = _get(app, "/audit/archive")
        assert '<span class="sig-all">1 / 1</span>' in listing
        assert '<span class="muted">0 / 1</span>' in listing
    finally:
        dash.close()
