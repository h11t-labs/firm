"""End-to-end smoke test: start the real stdlib server and drive it over HTTP."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

import pytest

from firm._core.database import create_engine_for
from firm.audit import schema as audit_schema
from firm.cache import schema as cache_schema
from firm.queue import schema as queue_schema
from firm.ui.context import build_dashboard
from firm.ui.server import create_server


@contextmanager
def _running(dash) -> Iterator[str]:
    server = create_server(dash, "127.0.0.1", 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def base_url(dashboard) -> Iterator[str]:
    server = create_server(dashboard, "127.0.0.1", 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _get(url: str) -> tuple[int, str]:
    with urlopen(url) as resp:  # localhost test server
        return resp.status, resp.read().decode("utf-8")


def _get_with_cookie(url: str, cookie: str) -> tuple[int, str]:
    with urlopen(Request(url, headers={"Cookie": cookie})) as resp:
        return resp.status, resp.read().decode("utf-8")


def _post(url: str) -> tuple[int, str]:
    with urlopen(url, data=b"") as resp:  # POST; follows the 303 back to the page
        return resp.status, resp.read().decode("utf-8")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # don't follow the 303; urllib then raises it as an HTTPError instead


def _post_form(url: str, fields: dict[str, str], cookie: str = "") -> tuple[int, dict[str, str]]:
    """POST form-encoded ``fields`` without following a redirect; returns (status, headers) for
    both a plain response and the 303/404 cases, which urllib raises as ``HTTPError``."""
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if cookie:
        headers["Cookie"] = cookie
    request = Request(url, data=urlencode(fields).encode(), headers=headers)
    try:
        with build_opener(_NoRedirect).open(request) as resp:
            return resp.status, dict(resp.headers.items())
    except HTTPError as exc:
        return exc.code, dict(exc.headers.items())


def test_overview_renders_with_all_four_tabs(base_url, seed) -> None:
    seed.ready()
    seed.failed()
    status, body = _get(base_url + "/")
    assert status == 200
    assert "Overview" in body
    # the top nav exposes every enabled part
    for tab in ("Queue", "Cache", "Channels", "Audit"):
        assert f">{tab}<" in body


def test_jobs_page_lists_failed(base_url, seed) -> None:
    seed.failed(error="ValueError: kaboom")
    status, body = _get(base_url + "/jobs?state=failed")
    assert status == 200
    assert "Retry" in body


def test_job_detail_and_404(base_url, seed) -> None:
    job_id = seed.failed(error="ValueError: detail-me")
    status, body = _get(f"{base_url}/job/{job_id}")
    assert status == 200
    assert "detail-me" in body
    with pytest.raises(Exception):  # noqa: B017 - urlopen raises HTTPError on 404
        _get(f"{base_url}/job/999999")


def test_jobs_list_shows_refresh_control(base_url, seed) -> None:
    seed.ready()
    status, body = _get(base_url + "/jobs?state=ready")
    assert status == 200
    assert "auto-refresh 5s" in body
    assert '<meta http-equiv="refresh" content="5">' in body


def test_job_detail_page_has_no_refresh_control(base_url, seed) -> None:
    job_id = seed.failed(error="boom")
    status, body = _get(f"{base_url}/job/{job_id}")
    assert status == 200
    assert "auto-refresh" not in body
    assert "<meta http-equiv=" not in body


def test_queue_overview_and_jobs_list_share_one_refresh_setting(base_url, seed) -> None:
    seed.ready()
    fields = {"part": "queue", "seconds": "0", "return": "/"}
    _, headers = _post_form(base_url + "/settings/refresh", fields)
    cookie = headers["Set-Cookie"].split(";")[0]
    # turning refresh off from the overview also turns it off on the jobs list -- one
    # preference per part, not per exact page
    _, body = _get_with_cookie(base_url + "/jobs?state=ready", cookie)
    assert "auto-refresh off" in body
    assert "<meta http-equiv=" not in body


def test_overview_queue_name_links_to_filtered_jobs(base_url, seed) -> None:
    seed.ready(queue="mailers")
    status, body = _get(base_url + "/")
    assert status == 200
    assert 'href="/jobs?state=ready&queue=mailers"' in body


def test_overview_recurring_schedule_shows_readable_tooltip(base_url, seed) -> None:
    seed.recurring_task(key="auto-sync", schedule="*/10 * * * *")
    status, body = _get(base_url + "/")
    assert status == 200
    assert 'title="Every 10 minutes"' in body
    assert ">*/10 * * * *<" in body  # the raw expression still shows as the visible text


def test_overview_recurring_unrecognized_schedule_has_no_tooltip(base_url, seed) -> None:
    # a weekday range isn't one of the summarized shapes -- must not guess a description
    seed.recurring_task(key="weekdays", schedule="0 9 * * 1-5")
    status, body = _get(base_url + "/")
    assert status == 200
    assert '<span class="pill mono">0 9 * * 1-5</span>' in body


def test_jobs_page_scoped_to_queue_preserves_filter(base_url, seed) -> None:
    seed.ready(queue="mailers")
    seed.ready(queue="mailers")
    seed.ready(queue="default")
    status, body = _get(base_url + "/jobs?state=ready&queue=mailers")
    assert status == 200
    assert "queue: mailers" in body
    # the badge count and sub-nav pill both reflect the queue-scoped count, not the global one
    assert ">2<" in body
    assert ">3<" not in body
    # switching state pills while filtered keeps the queue param
    assert 'href="/jobs?state=failed&queue=mailers"' in body
    # the filter-clear chip drops the queue but keeps the current state
    assert 'href="/jobs?state=ready"' in body


def test_jobs_page_unfiltered_has_no_filter_chip(base_url, seed) -> None:
    seed.ready(queue="mailers")
    status, body = _get(base_url + "/jobs?state=ready")
    assert status == 200
    assert 'class="pill accent"' not in body  # no active filter -> no filter chip rendered
    # each row's queue chip links back into the filtered view
    assert 'href="/jobs?state=ready&queue=mailers"' in body


def test_jobs_list_page_size_selector_present(base_url, seed) -> None:
    seed.ready()
    status, body = _get(base_url + "/jobs?state=ready")
    assert status == 200
    assert "Show 50" in body  # jobs list defaults to 50, unlike audit's 25
    assert 'class="dropdown-opt active" href="/jobs?state=ready">50' in body
    assert 'href="/jobs?state=ready&per_page=10">10' in body


def test_jobs_list_pagination_with_many_jobs(base_url, seed) -> None:
    for _ in range(60):
        seed.ready()
    status, body = _get(base_url + "/jobs?state=ready")
    assert status == 200
    assert body.count('<td><a href="/job/') == 50  # default page size
    assert "Showing 1-50 of 60" in body
    assert "Page 1 of 2" in body
    assert "Next →" in body
    assert "← Prev" not in body

    status, body = _get(base_url + "/jobs?state=ready&page=2")
    assert status == 200
    assert body.count('<td><a href="/job/') == 10  # the remaining 10 jobs
    assert "Showing 51-60 of 60" in body
    assert "Page 2 of 2" in body
    assert "Next →" not in body
    assert "← Prev" in body


def test_jobs_list_per_page_selector_changes_row_count(base_url, seed) -> None:
    for _ in range(15):
        seed.ready()
    status, body = _get(base_url + "/jobs?state=ready&per_page=10")
    assert status == 200
    assert body.count('<td><a href="/job/') == 10
    assert "Next →" in body
    assert 'class="dropdown-opt active" href="/jobs?state=ready&per_page=10">10' in body


def test_jobs_list_invalid_per_page_falls_back_to_default(base_url, seed) -> None:
    seed.ready()
    status, body = _get(base_url + "/jobs?state=ready&per_page=999")
    assert status == 200
    assert "Show 50" in body  # invalid value -> default, not a crash


def test_job_detail_crumb_preserves_queue_filter(base_url, seed) -> None:
    job_id = seed.failed(error="boom")
    status, body = _get(f"{base_url}/job/{job_id}?queue=default")
    assert status == 200
    assert 'href="/jobs?state=failed&queue=default"' in body


def test_pause_action_takes_effect(base_url, seed) -> None:
    seed.ready(queue="default")
    status, body = _post(base_url + "/queue/default/pause")  # 303 -> overview
    assert status == 200
    assert "paused" in body


def test_cache_page_and_clear(base_url, seed) -> None:
    seed.cache_entry(key=b"homepage", value=b"<html>")
    status, body = _get(base_url + "/cache")
    assert status == 200
    assert "homepage" in body
    assert "Clear all" in body
    # clearing redirects back to an empty cache page
    status, body = _post(base_url + "/cache/clear")
    assert status == 200
    assert "No cache entries" in body


def test_channels_page_and_trim(base_url, seed) -> None:
    seed.channel_message(channel=b"room:42", payload=b"ping")
    status, body = _get(base_url + "/channels")
    assert status == 200
    assert "room:42" in body
    assert "ping" in body
    status, _ = _post(base_url + "/channels/trim")  # nothing old enough -> still 200
    assert status == 200


def test_cache_page_size_selector_and_pagination(base_url, seed) -> None:
    for i in range(60):
        seed.cache_entry(key=f"key-{i:02d}".encode())
    status, body = _get(base_url + "/cache")
    assert status == 200
    assert "Show 50" in body  # cache defaults to 50, like the jobs list
    assert 'href="/cache?per_page=10">10' in body
    assert body.count('<td class="mono">key-') == 50
    assert "Showing 1-50 of 60" in body
    assert "Page 1 of 2" in body
    assert "Next →" in body

    status, body = _get(base_url + "/cache?page=2")
    assert status == 200
    assert body.count('<td class="mono">key-') == 10
    assert "Showing 51-60 of 60" in body
    assert "← Prev" in body


def test_channel_top_and_messages_paginate_independently(base_url, seed) -> None:
    for i in range(30):
        seed.channel_message(channel=f"room:{i:02d}".encode(), payload=b"x")
    status, body = _get(base_url + "/channels")
    assert status == 200
    # busiest channels defaults to 25, recent messages to 50 -- distinct defaults, same as before
    assert "Show 25" in body
    assert "Show 50" in body
    assert "Showing 1-25 of 30" in body  # busiest channels: 30 distinct channels
    assert "Showing 1-30 of 30" in body  # recent messages: only 30 messages total, all fit

    # paginating "busiest channels" alone must not disturb the (default) messages pagination
    status, body = _get(base_url + "/channels?top_page=2")
    assert status == 200
    assert "Showing 26-30 of 30" in body  # page 2 of busiest channels: the remaining 5
    # messages' own size options still work, correctly carrying the active top_page along
    assert 'href="/channels?top_page=2&per_page=10">10' in body


def test_payload_is_html_escaped(base_url, seed) -> None:
    seed.channel_message(channel=b"room", payload=b"<script>alert(1)</script>")
    status, body = _get(base_url + "/channels")
    assert status == 200
    assert "&lt;script&gt;" in body
    assert "<script>alert(1)</script>" not in body


def test_audit_page_lists_recorded_events(base_url, seed) -> None:
    seed.audit_record(
        action="invoice.paid",
        subject_type="Invoice",
        subject_id="42",
        actor_type="User",
        actor_id="7",
        correlation_id="req-1",
    )
    status, body = _get(base_url + "/audit")
    assert status == 200
    assert "invoice.paid" in body
    assert "Invoice:42" in body
    assert "User:7" in body
    assert "req-1" in body


def test_audit_page_filters_by_action(base_url, seed) -> None:
    seed.audit_record(action="invoice.paid")
    seed.audit_record(action="invoice.voided")
    status, body = _get(base_url + "/audit?action=invoice.paid")
    assert status == 200
    assert "invoice.paid" in body
    assert "invoice.voided" not in body


def test_audit_page_filters_by_subject(base_url, seed) -> None:
    seed.audit_record(action="kept", subject_type="Invoice", subject_id="1")
    seed.audit_record(action="dropped", subject_type="Invoice", subject_id="2")
    status, body = _get(base_url + "/audit?subject=Invoice:1")
    assert status == 200
    assert "kept" in body
    assert "dropped" not in body


def test_audit_page_renders_label_actor_without_none(base_url, seed) -> None:
    seed.audit_record(
        action="sync.ran", subject_type=None, subject_id=None, actor_type="cron", actor_id=None
    )
    status, body = _get(base_url + "/audit")
    assert status == 200
    assert "cron" in body
    assert "cron:None" not in body


def test_audit_page_filters_by_label_actor_type_only(base_url, seed) -> None:
    seed.audit_record(action="kept", actor_type="cron", actor_id=None)
    seed.audit_record(action="dropped", actor_type="User", actor_id="7")
    status, body = _get(base_url + "/audit?actor=cron")
    assert status == 200
    assert "kept" in body
    assert "dropped" not in body


def test_audit_page_shows_display_name(base_url, seed) -> None:
    seed.audit_record(
        action="invoice.paid", actor_type="User", actor_id="7", actor_label="alice@example.com"
    )
    status, body = _get(base_url + "/audit")
    assert status == 200
    assert "alice@example.com" in body


def test_audit_payload_is_html_escaped(base_url, seed) -> None:
    seed.audit_record(action="x", data='{"note": "<script>alert(1)</script>"}')
    status, body = _get(base_url + "/audit")
    assert status == 200
    assert "&lt;script&gt;" in body
    assert "<script>alert(1)</script>" not in body


def test_audit_page_action_link_navigates_to_filtered_view(base_url, seed) -> None:
    seed.audit_record(action="invoice.paid")
    seed.audit_record(action="invoice.voided")
    status, body = _get(base_url + "/audit?action=invoice.paid")
    assert status == 200
    assert "invoice.paid" in body
    assert "invoice.voided" not in body
    # the (now-active) filter is reflected back into the form field
    assert 'name="action" value="invoice.paid"' in body


def test_audit_page_links_to_detail(base_url, seed) -> None:
    event_id = seed.audit_record(action="invoice.paid")
    status, body = _get(base_url + "/audit")
    assert status == 200
    assert f'href="/audit/{event_id}"' in body


def test_audit_detail_page_has_no_refresh_control(base_url, seed) -> None:
    event_id = seed.audit_record(action="invoice.paid")
    status, body = _get(f"{base_url}/audit/{event_id}")
    assert status == 200
    assert "auto-refresh" not in body
    assert "<meta http-equiv=" not in body


def test_audit_detail_shows_full_event(base_url, seed) -> None:
    event_id = seed.audit_record(
        action="invoice.paid",
        subject_type="Invoice",
        subject_id="42",
        actor_type="User",
        actor_id="7",
        correlation_id="req-1",
        data='{"amount": 4200}',
    )
    status, body = _get(f"{base_url}/audit/{event_id}")
    assert status == 200
    assert "invoice.paid" in body
    assert "Invoice:42" in body
    assert "User:7" in body
    assert "req-1" in body
    assert "amount" in body and "4200" in body  # full payload shown, not truncated


def test_audit_detail_404_for_missing_id(base_url, seed) -> None:
    seed.audit_record(action="x")
    with pytest.raises(Exception):  # noqa: B017 - urlopen raises HTTPError on 404
        _get(f"{base_url}/audit/999999")


def test_audit_detail_payload_is_html_escaped(base_url, seed) -> None:
    event_id = seed.audit_record(action="x", data='{"note": "<script>alert(1)</script>"}')
    status, body = _get(f"{base_url}/audit/{event_id}")
    assert status == 200
    assert "&lt;script&gt;" in body
    assert "<script>alert(1)</script>" not in body


def test_audit_page_shows_kpi_cards(base_url, seed) -> None:
    seed.audit_record(action="invoice.paid")
    seed.audit_record(action="invoice.voided")
    status, body = _get(base_url + "/audit")
    assert status == 200
    assert "events" in body and ">2<" in body
    assert "actions" in body  # 2 distinct actions
    assert "last event" in body


def test_audit_filter_button_is_primary_reset_is_secondary(base_url) -> None:
    status, body = _get(base_url + "/audit")
    assert status == 200
    assert '<button type="submit" class="primary">' in body
    assert "Filter</button>" in body
    assert '<a class="btn" href="/audit">' in body
    assert "Reset</a>" in body


def test_audit_filter_and_reset_buttons_have_icons(base_url) -> None:
    status, body = _get(base_url + "/audit")
    assert status == 200
    filter_btn = body[body.index('class="primary"') : body.index("Filter</button>")]
    reset_start = body.index('<a class="btn" href="/audit">')
    reset_btn = body[reset_start : body.index("Reset</a>", reset_start)]
    assert "<svg" in filter_btn
    assert "<svg" in reset_btn


def test_audit_page_size_selector_present(base_url) -> None:
    status, body = _get(base_url + "/audit")
    assert status == 200
    assert "Show 25" in body  # collapsed dropdown shows the current page size
    assert 'class="dropdown-opt active" href="/audit">25' in body
    assert 'href="/audit?per_page=10">10' in body  # the other choices are one click away
    assert 'href="/audit?per_page=50">50' in body
    assert 'href="/audit?per_page=100">100' in body


def test_audit_columns_are_sortable(base_url, seed) -> None:
    seed.audit_record(action="invoice.paid")
    status, body = _get(base_url + "/audit")
    assert status == 200
    # non-default sort keys spell themselves out explicitly in their column link
    for key in ("id", "action", "subject", "actor", "correlation_id"):
        assert f"sort={key}" in body
    # "When" (created_at) is the default sort, so it's already the active column without
    # needing "sort=created_at" spelled out in its own link
    assert '<th><a class="active" href="/audit?dir=asc">When' in body
    assert "<th>Data</th>" in body  # Data itself is not a sort link


def test_audit_sort_by_action_changes_order(base_url, seed) -> None:
    seed.audit_record(action="z.last")
    seed.audit_record(action="a.first")
    status, body = _get(base_url + "/audit?sort=action&dir=asc")
    assert status == 200
    assert body.index("a.first") < body.index("z.last")
    status, body = _get(base_url + "/audit?sort=action&dir=desc")
    assert status == 200
    assert body.index("z.last") < body.index("a.first")


def test_audit_sort_link_toggles_direction_when_active(base_url, seed) -> None:
    seed.audit_record(action="x")
    status, body = _get(base_url + "/audit?sort=action&dir=asc")
    assert status == 200
    # clicking the active "Action" column again flips back to desc, the default direction, so
    # "dir=" is omitted from the link entirely -- absence resolves to desc server-side
    assert 'href="/audit?sort=action"' in body


def test_audit_pagination_with_many_events(base_url, seed) -> None:
    for i in range(30):
        seed.audit_record(action=f"event.{i:02d}")
    status, body = _get(base_url + "/audit")
    assert status == 200
    assert body.count('<td class="mono"><a href="/audit/') == 25  # default page size
    assert "Showing 1-25 of 30" in body
    assert "Page 1 of 2" in body
    assert "Next →" in body
    assert "← Prev" not in body

    status, body = _get(base_url + "/audit?page=2")
    assert status == 200
    assert body.count('<td class="mono"><a href="/audit/') == 5  # the remaining 5 events
    assert "Showing 26-30 of 30" in body
    assert "Page 2 of 2" in body
    assert "Next →" not in body
    assert "← Prev" in body


def test_audit_per_page_selector_changes_row_count(base_url, seed) -> None:
    for i in range(15):
        seed.audit_record(action=f"event.{i:02d}")
    status, body = _get(base_url + "/audit?per_page=10")
    assert status == 200
    assert body.count('<td class="mono"><a href="/audit/') == 10
    assert "Next →" in body
    assert 'class="dropdown-opt active" href="/audit?per_page=10">10' in body


def test_audit_invalid_per_page_falls_back_to_default(base_url, seed) -> None:
    seed.audit_record(action="x")
    status, body = _get(base_url + "/audit?per_page=999")
    assert status == 200
    assert "Show 25" in body  # invalid value -> default, not a crash


def test_audit_invalid_sort_falls_back_to_default(base_url, seed) -> None:
    seed.audit_record(action="x")
    status, _ = _get(base_url + "/audit?sort=not-a-column")
    assert status == 200  # no 500 on an unrecognised sort key


def test_audit_only_database_lands_on_audit(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'audit_only.db'}"
    engine = create_engine_for(url)
    audit_schema.create_all(engine)
    engine.dispose()
    dash = build_dashboard(database_url=url)
    try:
        assert dash.parts == ["audit"]
        with _running(dash) as base:
            status, body = _get(base + "/")  # 303 -> /audit, urlopen follows it
            assert status == 200
            assert ">Audit<" in body
            assert ">Cache<" not in body
    finally:
        dash.close()


def test_overview_shows_refresh_control_with_default_interval(base_url) -> None:
    status, body = _get(base_url + "/")
    assert status == 200
    assert "auto-refresh 5s" in body
    assert 'action="/settings/refresh"' in body
    assert 'name="part" value="queue"' in body


def test_set_refresh_updates_cookie_and_meta_tag(base_url) -> None:
    fields = {"part": "queue", "seconds": "30", "return": "/"}
    status, headers = _post_form(base_url + "/settings/refresh", fields)
    assert status == 303
    assert headers["Location"] == "/"
    cookie = headers["Set-Cookie"].split(";")[0]
    assert cookie == "firm_refresh_queue=30"
    _, body = _get_with_cookie(base_url + "/", cookie)
    assert '<meta http-equiv="refresh" content="30">' in body
    assert "auto-refresh 30s" in body


def test_set_refresh_off_omits_meta_tag(base_url) -> None:
    fields = {"part": "queue", "seconds": "0", "return": "/"}
    _, headers = _post_form(base_url + "/settings/refresh", fields)
    cookie = headers["Set-Cookie"].split(";")[0]
    _, body = _get_with_cookie(base_url + "/", cookie)
    assert "<meta http-equiv=" not in body
    assert "auto-refresh off" in body


def test_set_refresh_rejects_invalid_values(base_url) -> None:
    bad_seconds = {"part": "queue", "seconds": "999", "return": "/"}
    status, _ = _post_form(base_url + "/settings/refresh", bad_seconds)
    assert status == 404
    bad_part = {"part": "bogus", "seconds": "10", "return": "/"}
    status, _ = _post_form(base_url + "/settings/refresh", bad_part)
    assert status == 404


def test_set_refresh_rejects_unsafe_return(base_url) -> None:
    status, headers = _post_form(
        base_url + "/settings/refresh",
        {"part": "queue", "seconds": "10", "return": "https://evil.example/steal"},
    )
    assert status == 303
    assert headers["Location"] == "/"  # falls back to a safe default, not the attacker's URL


def test_overview_shows_theme_control_defaulting_to_system(base_url) -> None:
    status, body = _get(base_url + "/")
    assert status == 200
    assert '<html data-theme="system">' in body  # unset cookie -> follow the OS
    assert 'action="/settings/theme"' in body
    assert 'name="theme" value="light"' in body


def test_set_theme_updates_cookie_and_html_attribute(base_url) -> None:
    status, headers = _post_form(base_url + "/settings/theme", {"theme": "dark", "return": "/"})
    assert status == 303
    assert headers["Location"] == "/"
    cookie = headers["Set-Cookie"].split(";")[0]
    assert cookie == "firm_theme=dark"
    _, body = _get_with_cookie(base_url + "/", cookie)
    assert '<html data-theme="dark">' in body


def test_set_theme_rejects_invalid_value(base_url) -> None:
    status, _ = _post_form(base_url + "/settings/theme", {"theme": "neon", "return": "/"})
    assert status == 404


def test_set_theme_rejects_unsafe_return(base_url) -> None:
    status, headers = _post_form(
        base_url + "/settings/theme",
        {"theme": "dark", "return": "https://evil.example/steal"},
    )
    assert status == 303
    assert headers["Location"] == "/"  # falls back to a safe default, not the attacker's URL


def test_theme_control_present_on_detail_page(base_url, seed) -> None:
    # Unlike the refresh control, the theme toggle also appears on detail pages.
    job_id = seed.failed(error="boom")
    status, body = _get(f"{base_url}/job/{job_id}")
    assert status == 200
    assert 'action="/settings/theme"' in body
    assert 'action="/settings/refresh"' not in body


def test_cross_origin_post_is_rejected(base_url, seed) -> None:
    seed.cache_entry(key=b"keep-me")
    request = Request(
        base_url + "/cache/clear", data=b"", headers={"Origin": "http://evil.example"}
    )
    with pytest.raises(HTTPError) as exc:
        urlopen(request)
    assert exc.value.code == 403
    # the destructive action did not run
    _, body = _get(base_url + "/cache")
    assert "keep-me" in body


def test_cache_only_database_lands_on_cache(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'cache_only.db'}"
    engine = create_engine_for(url)
    cache_schema.create_all(engine)
    engine.dispose()
    dash = build_dashboard(database_url=url)
    try:
        assert dash.parts == ["cache"]
        with _running(dash) as base:
            status, body = _get(base + "/")  # 303 -> /cache, urlopen follows it
            assert status == 200
            assert "Cache" in body
            assert ">Queue<" not in body  # no queue tab when there's no queue table
    finally:
        dash.close()


def test_queue_and_audit_only_hides_other_tabs(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'queue_audit_only.db'}"
    engine = create_engine_for(url)
    queue_schema.create_all(engine)
    audit_schema.create_all(engine)
    engine.dispose()
    dash = build_dashboard(database_url=url)
    try:
        assert dash.parts == ["queue", "audit"]
        with _running(dash) as base:
            # queue takes priority for "/", and only the enabled parts get a tab
            status, body = _get(base + "/")
            assert status == 200
            assert "Overview" in body
            assert ">Queue<" in body
            assert ">Audit<" in body
            assert ">Cache<" not in body
            assert ">Channels<" not in body

            # the jobs list and the other enabled part both render cleanly
            status, body = _get(base + "/jobs?state=ready")
            assert status == 200
            status, body = _get(base + "/audit")
            assert status == 200
            assert "Audit" in body

            # a disabled part's routes are a clean 404, not a crash
            with pytest.raises(HTTPError) as exc:
                _get(base + "/cache")
            assert exc.value.code == 404
            with pytest.raises(HTTPError) as exc:
                _get(base + "/channels")
            assert exc.value.code == 404
    finally:
        dash.close()


def test_no_tables_renders_empty_page(tmp_path) -> None:
    dash = build_dashboard(database_url=f"sqlite:///{tmp_path / 'empty.db'}")
    try:
        assert dash.parts == []
        with _running(dash) as base:
            status, body = _get(base + "/")
            assert status == 200
            assert "Nothing to show" in body
    finally:
        dash.close()


def test_out_of_range_id_is_404_not_500(base_url, seed) -> None:
    """UL-4: /job/<huge digits> passed the route regex, int() succeeded, and the BIGINT
    overflow surfaced as a DBAPI error -> 500. It is a 404 now."""
    seed.ready()
    with pytest.raises(HTTPError) as exc:
        urlopen(base_url + "/job/99999999999999999999999")
    assert exc.value.code == 404


def test_page_overshoot_clamps_query_to_last_page(base_url, seed) -> None:
    """UL-2: the pager clamped for display while the query used the raw offset, rendering an
    empty table under a pager claiming rows exist. The offset now follows the clamp."""
    seed.cache_entry(key=b"clamp-me", value=b"v")
    status, body = _get(base_url + "/cache?page=999")
    assert status == 200
    assert "clamp-me" in body  # the row is on the (clamped) last page, not an empty page 999
