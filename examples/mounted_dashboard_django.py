"""Mount the firm-ui dashboard inside a Django project.

    FIRM_DATABASE_URL=sqlite:///firm-quickstart.db \
      uv run python examples/mounted_dashboard_django.py

Then open http://127.0.0.1:8000/firm/ — it answers 403 until you send the demo credential
(``curl -H 'X-Demo-Admin: yes' http://127.0.0.1:8000/firm/``). Run examples/quickstart.py first if
the database has no firm tables yet. Needs ``firm-ui[django]``.

Everything is in this one file so it runs without a project layout; in a real project only the
``urlpatterns`` entry below belongs in your ``urls.py``:

    urlpatterns = [
        path("firm/", include(dashboard_urls(dash, host_auth=True,
                                             decorator=staff_member_required))),
    ]

``decorator`` is what guards the dashboard — which is what ``host_auth=True`` states. It is
normally ``staff_member_required``; the demo hand-rolls one so it needs no auth tables.

firm's own database (``FIRM_DATABASE_URL``) is separate from Django's ``DATABASES``: the dashboard
reads it through its own engines, not the ORM, so this demo configures none.
"""

from __future__ import annotations

import os
import sys

import django
from django.conf import settings
from django.http import HttpResponse, HttpResponseForbidden
from django.urls import include, path

from firm.ui import build_dashboard
from firm.ui.contrib.django import dashboard_urls

DB = os.environ.get("FIRM_DATABASE_URL", "sqlite:///firm-quickstart.db")

settings.configure(
    DEBUG=True,
    SECRET_KEY="demo-only-not-a-real-secret",  # noqa: S106 - a demo signing key
    ALLOWED_HOSTS=["127.0.0.1", "localhost"],
    ROOT_URLCONF=__name__,
    DATABASES={},
    MIDDLEWARE=[
        "django.middleware.common.CommonMiddleware",
        "django.middleware.csrf.CsrfViewMiddleware",
    ],
)
django.setup()

# It owns database engines: one per process, built at startup.
dashboard = build_dashboard(database_url=DB)


def admin_only(view):
    """Stand-in for ``staff_member_required``, which needs ``django.contrib.auth`` set up."""

    def wrapper(request, *args, **kwargs):
        if request.headers.get("X-Demo-Admin") != "yes":
            return HttpResponseForbidden("admins only")
        return view(request, *args, **kwargs)

    return wrapper


def index(request) -> HttpResponse:
    return HttpResponse("Demo app. Dashboard: /firm/ (send X-Demo-Admin: yes)")


urlpatterns = [
    path("", index),
    path("firm/", include(dashboard_urls(dashboard, host_auth=True, decorator=admin_only))),
]


if __name__ == "__main__":
    from django.core.management import execute_from_command_line

    execute_from_command_line([sys.argv[0], "runserver", "--noreload", *sys.argv[1:]])
