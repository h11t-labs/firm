"""An S3 audit log renders as the SQL audit page does: same cards, filters, sorting, integrity."""

from __future__ import annotations

import html
import re
import threading
from datetime import timedelta
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen
from uuid import UUID, uuid4

import pytest

from firm._core.clock import now_utc
from firm.audit import AuditLog
from firm.audit.archive import Archive
from firm.audit.object_store import ObjectStore
from firm.audit.s3 import S3AuditLog
from firm.ui import build_dashboard, create_server
from firm.ui.app import DashboardApp, Headers, UIRequest
from tests.audit.conftest import MemoryS3

STREAM = str(UUID(int=1))
KEY = "dashboard-s3-row-and-seal-key-0123456789ab"


def _log(client, **kwargs) -> S3AuditLog:
    return S3AuditLog("events", stream=STREAM, client=client, **kwargs)


def _get(app: DashboardApp, path: str, query: str = "", prefix: str = "") -> tuple[int, str]:
    response = app.handle(UIRequest("GET", path, query=query, prefix=prefix))
    return response.status, response.body.decode()


@pytest.fixture
def s3_dashboard_url():
    client = MemoryS3()
    log = _log(client)
    log.record("<script>unsafe</script>", subject=("Invoice", 42))
    archive = Archive(ObjectStore("archives", client=client))
    archived = archive.export_s3(log, batch_id=str(uuid4()))
    dash = build_dashboard(audit_s3=log, audit_archive=archive)
    assert dash.parts == ["audit"] and dash.audit is None
    server = create_server(dash, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", archived.key
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        dash.close()


def test_s3_page_is_the_audit_page_and_escapes(s3_dashboard_url):
    base, _ = s3_dashboard_url
    with urlopen(base + "/audit/s3") as response:
        body = response.read().decode()
    assert "&lt;script&gt;unsafe&lt;/script&gt;" in body and "<script>unsafe" not in body
    assert '<div class="card"><div class="l">events</div><div class="n">1</div>' in body
    assert 'Source</span> <span class="source-word"' in body and ">S3<" in body
    assert 'action="/audit/s3"' in body and 'href="/audit/s3/1"' in body
    assert "sort=action" in body  # the same sortable columns
    with urlopen(base + "/audit/s3/1") as response:
        detail = response.read().decode()
    assert "&lt;script&gt;unsafe&lt;/script&gt;" in detail
    assert 'class="crumb" href="/audit/s3"' in detail
    with pytest.raises(HTTPError) as caught:
        urlopen(base + "/audit/s3/99")
    assert caught.value.code == 404


def test_s3_page_filters_sorts_and_pages_like_sql(tmp_path):
    client = MemoryS3()
    log = _log(client)
    url = f"sqlite:///{tmp_path / 'audit.db'}"
    with AuditLog(url) as audit:
        for index in range(30):
            for target in (log, audit):
                target.record(
                    "invoice.paid" if index % 3 else "invoice.sent",
                    subject=("Invoice", index % 5),
                    actor=("User", index % 2),
                )
    dash_s3 = build_dashboard(audit_s3=log)
    dash_sql = build_dashboard(audit_url=url)
    try:
        apps = DashboardApp(dash_sql), DashboardApp(dash_s3)

        def ids(app, path, query):
            _, body = _get(app, path, query)
            return re.findall(r'href="/audit(?:/s3)?/(\d+)"', body)

        for query in (
            "",
            "per_page=10&page=2",
            "action=invoice.sent",
            "subject=Invoice:3",
            "actor=User:1&sort=subject&dir=asc",
            "sort=action&dir=desc&per_page=10&page=3",
            "sort=id&dir=asc&per_page=10",
            "sort=created_at&dir=asc&per_page=10&page=2",
        ):
            assert ids(apps[1], "/audit/s3", query) == ids(apps[0], "/audit", query), query
        _, body = _get(apps[1], "/audit/s3", "action=invoice.sent")
        assert "Showing 1-10 of 10" in body
    finally:
        dash_s3.close()
        dash_sql.close()


def test_s3_integrity_strip_and_row_shields(monkeypatch):
    client = MemoryS3()
    log = _log(client, mac_key=KEY, grace=0.0, seal_batch_size=2)
    old = now_utc() - timedelta(hours=1)
    with patch("firm.audit.s3.now_utc", lambda: old):
        for index in range(3):
            log.record(f"e{index}")
    log.sealer.run_once()
    log.verify(full=True)
    monkeypatch.setenv("FIRM_AUDIT_KEY", KEY)
    dash = build_dashboard(audit_s3=log)
    try:
        _, body = _get(DashboardApp(dash), "/audit/s3")
        assert 'class="integrity-word">OK<' in body
        assert body.count('class="row-status-cell"') == 3
    finally:
        dash.close()


def test_s3_only_audit_tab_lands_on_s3_with_its_query():
    dash = build_dashboard(audit_s3=_log(MemoryS3()))
    try:
        response = DashboardApp(dash).handle(
            UIRequest("GET", "/audit", query="action=x", prefix="/ops")
        )
        assert response.status == 303
        assert dict(response.headers)["Location"] == "/ops/audit/s3?action=x"
        status, body = _get(DashboardApp(dash), "/audit/s3")
        assert status == 200 and '<nav class="sub">' not in body
        assert "No audit events match." in body
    finally:
        dash.close()


def test_archive_view_batch_list_and_bad_cursors(s3_dashboard_url):
    base, key = s3_dashboard_url
    with urlopen(base + "/audit/archive?" + urlencode({"key": key})) as response:
        body = response.read().decode()
        assert "1 verified event<" in body
        assert 'Source</span> <span class="source-word"' in body and ">Archive<" in body
        assert '<span class="sig unsigned"' in body
    with urlopen(base + "/audit/archive") as response:
        listing = response.read().decode()
        assert 'href="/audit/archive?' + urlencode({"key": key}) + '"' in listing
        assert "1 batch on this page" in listing and ">complete<" in listing
    with pytest.raises(HTTPError) as caught:
        urlopen(base + "/audit/archive?continuation=bogus")
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
    log = _log(client)
    log.record("invoice.paid", subject=("Invoice", 42))
    archive = Archive(ObjectStore("archives", client=client))
    dash = build_dashboard(audit_url=url, audit_s3=log, audit_archive=archive)
    try:
        assert dash.parts == ["audit"]
        app = DashboardApp(dash)
        _, sql = _get(app, "/audit")
        assert "order.shipped" in sql and "invoice.paid" not in sql
        assert re.search(r'class="active" href="/audit" aria-current="page">Database<', sql)
        assert 'href="/audit/s3">S3<' in sql and 'href="/audit/archive">Archive<' in sql
        assert 'class="source"' not in sql
        _, s3 = _get(app, "/audit/s3")
        assert "invoice.paid" in s3 and "order.shipped" not in s3
        assert 'aria-current="page">S3<' in s3 and 'class="source"' in s3
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
    log = _log(client)
    for _ in range(30):
        log.record("mounted")
    archive = Archive(ObjectStore("archives", client=client))
    archived = archive.export_s3(log, batch_id=str(uuid4()))
    dash = build_dashboard(audit_s3=log, audit_archive=archive)
    try:
        app = DashboardApp(dash)
        response = app.handle(
            UIRequest(
                "GET", "/audit/s3", prefix="/ops", headers=Headers([("Cookie", "firm_theme=dark")])
            )
        )
        assert response.status == 200
        body = response.body.decode()
        assert '<html data-theme="dark">' in body
        assert 'href="/ops/audit/s3/30"' in body and 'action="/ops/audit/s3"' in body
        assert 'href="/ops/audit/archive">Archive<' in body
        match = re.search(r'href="/ops(/audit/s3)\?([^"]*page=2[^"]*)"', body)
        assert match is not None
        status, page_two = _get(app, match.group(1), html.unescape(match.group(2)), "/ops")
        assert status == 200 and "Showing 26-30 of 30" in page_two
        detail = app.handle(UIRequest("GET", "/audit/s3/1", prefix="/ops"))
        assert detail.status == 200 and 'href="/ops/audit/s3"' in detail.body.decode()
        response = app.handle(
            UIRequest(
                "GET", "/audit/archive", prefix="/ops", query=urlencode({"key": archived.key})
            )
        )
        assert response.status == 200
        protected = DashboardApp(dash, authenticator=BasicAuth("ops", password="test-password"))
        for path in ("/audit", "/audit/s3", "/audit/s3/1", "/audit/archive"):
            assert protected.handle(UIRequest("GET", path)).status == 401
    finally:
        dash.close()


def test_archive_batch_list_pages_and_flags_pending_batches():
    client = MemoryS3()
    log = _log(client)
    log.record("paged")
    archive = Archive(ObjectStore("archives", client=client))
    for _ in range(26):
        archive.export_s3(log, batch_id=str(uuid4()))
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
    client = MemoryS3()
    archive = Archive(ObjectStore("archives", client=client))
    with AuditLog(f"sqlite:///{tmp_path / 'signed.db'}", mac_key=KEY) as signed:
        signed.record("signed.one")
        sql_batch = archive.export_sql(signed, stream=str(UUID(int=2)), batch_id=str(uuid4()))
    s3_signed = _log(client, mac_key=KEY)
    s3_signed.record("signed.s3")
    s3_batch = archive.export_s3(s3_signed, batch_id=str(uuid4()))
    plain = S3AuditLog("events", stream=str(UUID(int=3)), client=client)
    plain.record("plain.one")
    plain_batch = archive.export_s3(plain, batch_id=str(uuid4()))
    dash = build_dashboard(audit_archive=archive)
    try:
        app = DashboardApp(dash)
        for batch in (sql_batch, s3_batch):
            _, body = _get(app, "/audit/archive", urlencode({"key": batch.key}))
            assert '<span class="sig signed"' in body and "signed · key " in body
        _, body = _get(app, "/audit/archive", urlencode({"key": plain_batch.key}))
        assert '<span class="sig unsigned"' in body
        _, listing = _get(app, "/audit/archive")
        assert listing.count('<span class="sig-all">1 / 1</span>') == 2
        assert '<span class="muted">0 / 1</span>' in listing
    finally:
        dash.close()
