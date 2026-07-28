"""Mount the dashboard in a Django project (``firm-ui[django]``).

``urls.py``::

    from django.contrib.admin.views.decorators import staff_member_required
    from django.urls import include, path
    from firm.ui import build_dashboard
    from firm.ui.contrib.django import dashboard_urls

    dash = build_dashboard(database_url="sqlite:///app.db")

    urlpatterns = [
        path("firm/", include(dashboard_urls(dash, host_auth=True,
                                             decorator=staff_member_required))),
    ]

``decorator`` wraps the view, so the permission rule stays in the host project (any view
decorator works — ``staff_member_required``, ``permission_required``, ``login_required``);
``host_auth=True`` records that it is the one guarding the dashboard. Pass ``authenticator=``
instead to let firm-ui check the request itself.

The view is CSRF-exempt: the dashboard renders its own forms and carries no Django CSRF token, so
Django's token check would reject every action. Its own same-origin ``Origin``/``Referer`` guard
runs on every POST instead — see :mod:`firm.ui.contrib`.

The stylesheet is served by the mount itself. To route it through ``staticfiles`` instead, add
``firm.ui.app.static_dir()`` to ``STATICFILES_DIRS`` and pass the published URL as ``static_url``.
"""

from __future__ import annotations

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse
from django.urls import URLPattern, re_path
from django.views.decorators.csrf import csrf_exempt

from ..app import Headers, UIRequest
from ..auth import Authenticator
from ..context import Dashboard
from . import build_app, mount_prefix

DashboardView = Callable[..., HttpResponse]


def dashboard_view(
    dashboard: Dashboard,
    *,
    authenticator: Authenticator | None = None,
    host_auth: bool = False,
    channel_trim_retention: float | None = None,
    static_url: str | None = None,
) -> DashboardView:
    """The dashboard as one Django view. It takes the rest of the URL as a ``subpath`` argument,
    so route it with a catch-all pattern — :func:`dashboard_urls` does that for you."""
    app = build_app(
        dashboard,
        authenticator=authenticator,
        host_auth=host_auth,
        channel_trim_retention=channel_trim_retention,
        static_url=static_url,
    )

    @csrf_exempt
    def view(request: HttpRequest, subpath: str = "") -> HttpResponse:
        ui_request = UIRequest(
            method=request.method or "GET",
            path=f"/{subpath}",
            query=request.META.get("QUERY_STRING", ""),
            headers=Headers(request.headers.items()),
            peer=request.META.get("REMOTE_ADDR", ""),
            # request.path carries the script prefix; request.path_info would not.
            prefix=mount_prefix(request.path, subpath),
            host=request.get_host(),  # validated against ALLOWED_HOSTS
        )
        # Lazily: reading request.body applies Django's own upload limits, and it only happens
        # once the request has authenticated and passed the dashboard's size limit.
        result = app.handle(ui_request, read_body=lambda: request.body)
        response = HttpResponse(result.body, status=result.status)
        for name, value in result.headers:
            response[name] = value
        return response

    return view


def dashboard_urls(
    dashboard: Dashboard,
    *,
    decorator: Callable[[DashboardView], DashboardView] | None = None,
    authenticator: Authenticator | None = None,
    host_auth: bool = False,
    channel_trim_retention: float | None = None,
    static_url: str | None = None,
) -> list[URLPattern]:
    """URL patterns for the whole dashboard, to ``include()`` under any prefix. ``decorator`` is
    applied to the view — that is where the host project's permission rule goes."""
    view = dashboard_view(
        dashboard,
        authenticator=authenticator,
        host_auth=host_auth,
        channel_trim_retention=channel_trim_retention,
        static_url=static_url,
    )
    if decorator is not None:
        view = decorator(view)
    # Again on the outside: CsrfViewMiddleware reads the flag off the callback it resolves to, and
    # a caller's decorator need not preserve the attribute the inner csrf_exempt set.
    return [re_path(r"^(?P<subpath>.*)$", csrf_exempt(view), name="firm-ui")]
