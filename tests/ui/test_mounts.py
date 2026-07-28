"""Specs for mounting the dashboard in a host application — one adapter per framework, driven
through that framework's own test client.

Each adapter is only a translation, so the checks are the same three questions for all of them:
does the mounted dashboard render, do its links and actions carry the mount prefix, and do the
guards (CSRF origin check, the host's own auth) still hold.
"""

from __future__ import annotations

import base64
import sys
import types
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from firm.ui.auth import BasicAuth

PREFIX = "/firm"


def _lower(headers) -> dict[str, str]:
    return {name.lower(): value for name, value in headers.items()}


@dataclass
class Response:
    status: int
    body: str
    headers: dict[str, str]  # lowercased names — the clients disagree on casing, HTTP does not


@dataclass
class Client:
    get: Callable[..., Response]
    post: Callable[..., Response]


# -- one client per framework ----------------------------------------------------------------------


@pytest.fixture
def flask_client(dashboard) -> Callable[..., Client]:
    flask = pytest.importorskip("flask")
    from firm.ui.contrib.flask import blueprint

    def build(**kwargs) -> Client:
        app = flask.Flask(__name__)
        app.register_blueprint(blueprint(dashboard, **kwargs), url_prefix=PREFIX)
        client = app.test_client()

        def _wrap(resp) -> Response:
            return Response(resp.status_code, resp.get_data(as_text=True), _lower(resp.headers))

        def get(path: str, **headers) -> Response:
            return _wrap(client.get(path, headers=headers))

        def post(path: str, data: dict | None = None, **headers) -> Response:
            return _wrap(client.post(path, data=data or {}, headers=headers))

        return Client(get, post)

    return build


@pytest.fixture
def fastapi_client(dashboard) -> Callable[..., Client]:
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from firm.ui.contrib.fastapi import router

    def build(**kwargs) -> Client:
        app = fastapi.FastAPI()
        app.include_router(router(dashboard, **kwargs), prefix=PREFIX)
        client = TestClient(app, follow_redirects=False)

        def _wrap(resp) -> Response:
            return Response(resp.status_code, resp.text, _lower(resp.headers))

        def get(path: str, **headers) -> Response:
            return _wrap(client.get(path, headers=headers))

        def post(path: str, data: dict | None = None, **headers) -> Response:
            return _wrap(client.post(path, data=data or {}, headers=headers))

        return Client(get, post)

    return build


@pytest.fixture
def django_client(dashboard) -> Callable[..., Client]:
    django = pytest.importorskip("django")
    from django.conf import settings

    if not settings.configured:
        settings.configure(
            DEBUG=False,
            SECRET_KEY="firm-ui-tests",
            ALLOWED_HOSTS=["testserver"],
            ROOT_URLCONF=None,
            DATABASES={},
            MIDDLEWARE=[
                "django.middleware.common.CommonMiddleware",
                "django.middleware.csrf.CsrfViewMiddleware",
            ],
        )
        django.setup()

    from django.test import Client as DjangoClient
    from django.urls import clear_url_caches, include, path

    from firm.ui.contrib.django import dashboard_urls

    def build(**kwargs) -> Client:
        urlconf = types.ModuleType("firm_ui_test_urlconf")
        urlconf.urlpatterns = [  # type: ignore[attr-defined]
            path(f"{PREFIX.lstrip('/')}/", include(dashboard_urls(dashboard, **kwargs)))
        ]
        sys.modules[urlconf.__name__] = urlconf
        settings.ROOT_URLCONF = urlconf.__name__
        clear_url_caches()
        # enforce_csrf_checks: the dashboard's forms carry no Django token, so this is what proves
        # the view's csrf_exempt is doing its job rather than the test client being lenient.
        client = DjangoClient(enforce_csrf_checks=True)

        def _wrap(resp) -> Response:
            body = "" if resp.status_code in (301, 302, 303) else resp.content.decode()
            return Response(resp.status_code, body, _lower(resp.headers))

        def get(path_: str, **headers) -> Response:
            return _wrap(client.get(path_, headers=headers))

        def post(path_: str, data: dict | None = None, **headers) -> Response:
            return _wrap(client.post(path_, data=data or {}, headers=headers))

        return Client(get, post)

    return build


@pytest.fixture(params=["flask", "fastapi", "django"])
def mount(request) -> Callable[..., Client]:
    """A mounted dashboard for each framework, so every spec below runs against all three."""
    return request.getfixturevalue(f"{request.param}_client")


# -- the shared specs ------------------------------------------------------------------------------


def test_mounted_dashboard_renders(mount, seed) -> None:
    seed.ready()
    response = mount(host_auth=True).get(f"{PREFIX}/")
    assert response.status == 200
    assert "Overview" in response.body


def test_mounted_links_carry_the_prefix(mount, seed) -> None:
    job_id = seed.failed()
    client = mount(host_auth=True)
    body = client.get(f"{PREFIX}/jobs?state=failed").body
    assert f'href="{PREFIX}/job/{job_id}"' in body
    assert f'action="{PREFIX}/job/{job_id}/retry"' in body
    assert f'href="{PREFIX}/static/style.css"' in body
    assert f'href="{PREFIX}/cache"' in body
    assert 'href="/cache"' not in body


def test_mounted_stylesheet_is_served(mount) -> None:
    response = mount(host_auth=True).get(f"{PREFIX}/static/style.css")
    assert response.status == 200
    assert response.headers["content-type"].startswith("text/css")


def test_mounted_action_runs_and_redirects_into_the_mount(mount, seed) -> None:
    from firm.queue import queries

    job_id = seed.failed()
    response = mount(host_auth=True).post(f"{PREFIX}/job/{job_id}/retry")
    assert response.status == 303
    assert response.headers["location"] == f"{PREFIX}/jobs?state=failed"
    with seed.engine.connect() as conn:
        assert queries.job_detail(conn, job_id)["state"] == "ready"


def test_mounted_cross_origin_post_is_rejected(mount, seed) -> None:
    job_id = seed.failed()
    response = mount(host_auth=True).post(
        f"{PREFIX}/job/{job_id}/retry", Origin="http://evil.example"
    )
    assert response.status == 403


def test_mounted_unknown_path_is_a_dashboard_404(mount) -> None:
    response = mount(host_auth=True).get(f"{PREFIX}/nope")
    assert response.status == 404


def test_mounted_authenticator_still_guards_when_asked(mount, seed) -> None:
    seed.ready()
    client = mount(authenticator=BasicAuth("admin", password="pw"))
    assert client.get(f"{PREFIX}/").status == 401
    token = base64.b64encode(b"admin:pw").decode()
    assert client.get(f"{PREFIX}/", Authorization=f"Basic {token}").status == 200


def test_mounting_unguarded_is_refused(mount) -> None:
    with pytest.raises(ValueError, match="host_auth=True"):
        mount()


# -- framework-specific ----------------------------------------------------------------------------


def test_django_decorator_guards_the_mount(django_client, seed) -> None:
    """The permission rule stays in the host project: a decorator that refuses must win before any
    dashboard code runs."""
    from django.http import HttpResponseForbidden

    seed.ready()

    def deny_everything(view):
        def wrapper(request, *args, **kwargs):
            return HttpResponseForbidden("nope")

        return wrapper

    client = django_client(host_auth=True, decorator=deny_everything)
    assert client.get(f"{PREFIX}/").status == 403
