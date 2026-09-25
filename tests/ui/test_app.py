"""Specs for the transport-free dashboard core: requests in, responses out, and every link and
form action carrying the mount prefix."""

from __future__ import annotations

import pytest

from firm.ui.app import DashboardApp, Headers, UIRequest
from firm.ui.contrib import build_app, mount_prefix


def _get(app: DashboardApp, path: str, *, prefix: str = "", query: str = "", cookie: str = ""):
    headers = [("Host", "app.example")] + ([("Cookie", cookie)] if cookie else [])
    return app.handle(UIRequest("GET", path, query=query, headers=Headers(headers), prefix=prefix))


def _post(app: DashboardApp, path: str, body: bytes = b"", *, prefix: str = "", origin: str = ""):
    headers = [("Host", "app.example")] + ([("Origin", origin)] if origin else [])
    return app.handle(UIRequest("POST", path, headers=Headers(headers), body=body, prefix=prefix))


@pytest.fixture
def app(dashboard) -> DashboardApp:
    return DashboardApp(dashboard)


# -- the request/response value pair ---------------------------------------------------------------


def test_headers_are_case_insensitive_and_join_repeats() -> None:
    headers = Headers([("Content-Type", "text/html"), ("Cookie", "a=1"), ("cookie", "b=2")])
    assert headers.get("content-type", "") == "text/html"
    assert headers.get("COOKIE", "") == "a=1, b=2"
    assert headers.get("Missing", "fallback") == "fallback"


def test_request_normalizes_prefix_path_and_host() -> None:
    req = UIRequest("get", "jobs", query="state=failed", headers=Headers([("Host", "h")]))
    assert (req.method, req.path, req.host) == ("GET", "/jobs", "h")
    assert req.full_path == "/jobs?state=failed"
    assert UIRequest("GET", "/", prefix="/firm/").prefix == "/firm"
    assert UIRequest("GET", "/", prefix="firm").prefix == "/firm"
    assert UIRequest("GET", "/", prefix="/").prefix == ""
    # an explicit host wins over the header: the framework in front may resolve it differently
    explicit = UIRequest("GET", "/", headers=Headers([("Host", "raw")]), host="resolved")
    assert explicit.host == "resolved"


def test_mount_prefix_is_the_difference_between_full_and_matched_path() -> None:
    assert mount_prefix("/firm/jobs", "jobs") == "/firm"
    assert mount_prefix("/firm/", "") == "/firm"
    assert mount_prefix("/firm", "") == "/firm"
    assert mount_prefix("/", "") == ""
    assert mount_prefix("/jobs", "jobs") == ""


def test_unsupported_method_is_rejected(app: DashboardApp) -> None:
    response = app.handle(UIRequest("PUT", "/"))
    assert response.status == 405
    assert ("Allow", "GET, POST") in response.headers


# -- mount-point-relative URLs ---------------------------------------------------------------------


def test_root_mount_builds_unprefixed_urls(app: DashboardApp, seed) -> None:
    seed.ready()
    body = _get(app, "/").body.decode()
    assert 'href="/cache"' in body
    assert 'href="/static/style.css"' in body
    assert 'action="/settings/theme"' in body


def test_mounted_urls_all_carry_the_prefix(app: DashboardApp, seed) -> None:
    seed.failed()
    seed.cache_entry()
    body = _get(app, "/", prefix="/firm").body.decode()
    assert 'href="/firm/cache"' in body  # part tabs
    assert 'href="/firm/jobs?state=failed"' in body  # overview cards
    assert 'href="/firm/static/style.css"' in body  # stylesheet
    assert 'action="/firm/settings/theme"' in body  # chrome controls
    assert 'action="/firm/settings/refresh"' in body
    assert 'href="/firm/"' in body  # the brand link home
    # …and nothing links to the unmounted root
    assert 'href="/cache"' not in body
    assert 'action="/settings/theme"' not in body


def test_mounted_job_pages_and_actions_carry_the_prefix(app: DashboardApp, seed) -> None:
    job_id = seed.failed()
    body = _get(app, "/jobs", query="state=failed", prefix="/firm").body.decode()
    assert f'href="/firm/job/{job_id}"' in body
    assert f'action="/firm/job/{job_id}/retry"' in body
    assert 'action="/firm/failed/retry-all"' in body
    assert 'href="/firm/jobs?state=ready"' in body  # the state sub-nav
    assert "/firm/jobs?state=failed&per_page=10" in body  # the page-size dropdown
    detail = _get(app, f"/job/{job_id}", prefix="/firm").body.decode()
    assert f'action="/firm/job/{job_id}/discard"' in detail
    assert 'href="/firm/jobs?state=failed"' in detail  # breadcrumb


def test_mounted_audit_and_channel_pages_carry_the_prefix(app: DashboardApp, seed) -> None:
    event_id = seed.audit_record()
    seed.channel_message()
    audit = _get(app, "/audit", prefix="/firm").body.decode()
    assert 'action="/firm/audit"' in audit  # filter form
    assert f'href="/firm/audit/{event_id}"' in audit
    assert 'href="/firm/audit?action=user.login"' in audit  # filter-by-value link
    channels = _get(app, "/channels", prefix="/firm").body.decode()
    assert 'action="/firm/channels/trim"' in channels


def test_mounted_static_url_can_point_at_the_host_pipeline(dashboard, seed) -> None:
    seed.ready()
    app = DashboardApp(dashboard, static_url="/assets/firm.css")
    body = _get(app, "/", prefix="/firm").body.decode()
    assert 'href="/assets/firm.css"' in body
    assert "/firm/static/style.css" not in body
    # the built-in route keeps working, for a mount that does not publish it itself
    assert _get(app, "/static/style.css", prefix="/firm").status == 200


def test_mounted_landing_redirects_within_the_mount(dashboard, seed) -> None:
    dashboard.queue = None  # cache is then the first enabled part
    response = _get(DashboardApp(dashboard), "/", prefix="/firm")
    assert response.status == 303
    assert ("Location", "/firm/cache") in response.headers


def test_mounted_action_redirects_within_the_mount(app: DashboardApp, seed) -> None:
    job_id = seed.failed()
    response = _post(app, f"/job/{job_id}/retry", prefix="/firm")
    assert response.status == 303
    assert ("Location", "/firm/jobs?state=failed") in response.headers


# -- preferences -----------------------------------------------------------------------------------


def test_preference_cookie_is_scoped_to_the_mount(app: DashboardApp) -> None:
    response = _post(app, "/settings/theme", b"theme=dark&return=%2Ffirm%2Fcache", prefix="/firm")
    assert response.status == 303
    assert ("Location", "/firm/cache") in response.headers
    cookie = dict(response.headers)["Set-Cookie"]
    assert "firm_theme=dark" in cookie
    assert "Path=/firm/" in cookie


@pytest.mark.parametrize("hostile", ["//evil.example", "/\\evil.example", "https://evil.example"])
def test_return_field_cannot_redirect_off_site(app: DashboardApp, hostile: str) -> None:
    response = _post(
        app, "/settings/theme", f"theme=dark&return={hostile}".encode(), prefix="/firm"
    )
    assert ("Location", "/firm/") in response.headers


def test_saved_theme_is_read_from_the_cookie(app: DashboardApp, seed) -> None:
    seed.ready()
    body = _get(app, "/", cookie="firm_theme=dark").body.decode()
    assert 'data-theme="dark"' in body


# -- guards ----------------------------------------------------------------------------------------


def test_cross_origin_post_is_rejected(app: DashboardApp, seed) -> None:
    seed.cache_entry()
    assert _post(app, "/cache/clear", origin="http://evil.example").status == 403
    assert _post(app, "/cache/clear", origin="http://app.example").status == 303


def test_oversized_body_is_rejected(app: DashboardApp) -> None:
    huge = b"x" * (1 << 21)
    assert _post(app, "/settings/theme", huge).status == 413


def test_ambiguous_content_length_is_refused(app: DashboardApp) -> None:
    """Two conflicting Content-Length lines (Headers joins repeats) is the shape request smuggling
    is built on: refuse it rather than pick a reading the transport below may not share."""
    for value in ("12, 4096", "-1", "0x10"):
        request = UIRequest(
            "POST",
            "/settings/theme",
            headers=Headers([("Host", "app.example"), ("Content-Length", value)]),
            body=b"theme=dark",
        )
        assert app.handle(request).status == 400


def test_two_framings_at_once_are_refused(app: DashboardApp) -> None:
    """RFC 9112: a request framed by both Content-Length and Transfer-Encoding must be rejected —
    a proxy in front may frame it by the header this side ignores."""
    request = UIRequest(
        "POST",
        "/settings/theme",
        headers=Headers(
            [("Host", "app.example"), ("Content-Length", "10"), ("Transfer-Encoding", "chunked")]
        ),
        body=b"theme=dark",
    )
    assert app.handle(request).status == 400


def test_https_dashboard_refuses_a_plain_http_origin(dashboard, seed) -> None:
    """A page served over HTTP on the same host must not be able to post to an HTTPS dashboard.
    The reverse stays permissive: behind a TLS-terminating proxy the app often sees ``http`` while
    the browser's origin is ``https``."""
    seed.cache_entry()
    app = DashboardApp(dashboard)

    def clear(scheme: str, origin: str) -> int:
        headers = Headers([("Host", "app.example"), ("Origin", origin)])
        return app.handle(UIRequest("POST", "/cache/clear", headers=headers, scheme=scheme)).status

    assert clear("https", "http://app.example") == 403
    assert clear("https", "https://app.example") == 303
    assert clear("http", "https://app.example") == 303  # proxy-terminated TLS
    assert clear("http", "http://app.example") == 303


@pytest.mark.parametrize(
    ("hostile", "expected"),
    [
        ("//evil.example", "/evil.example"),  # protocol-relative -> not an open redirect
        ('/t/x" onmouseover=alert(1) x="', "/t/x%22%20onmouseover=alert(1)%20x=%22"),
        ("/t/<script>", "/t/%3Cscript%3E"),
        ("/t/a%20b", "/t/a%20b"),  # already encoded: left alone, not double-encoded
    ],
)
def test_hostile_mount_prefix_is_neutralized(dashboard, hostile: str, expected: str) -> None:
    """A host application can mount the dashboard under a URL segment of its own
    (``/tenants/<name>/firm``), so the prefix is not automatically trustworthy: it has to be safe
    to put in a Location header and in an HTML attribute."""
    assert UIRequest("GET", "/", prefix=hostile).prefix == expected
    body = _get(DashboardApp(dashboard), "/", prefix=hostile).body.decode()
    assert '" onmouseover' not in body
    assert "<script>" not in body


def test_mounting_without_stating_who_authenticates_is_refused(dashboard) -> None:
    with pytest.raises(ValueError, match="host_auth=True"):
        build_app(dashboard)
    assert build_app(dashboard, host_auth=True).authenticator is None
