"""Django settings for the firm demo project.

`"firm.queue.contrib.django"` in `INSTALLED_APPS` is the whole wiring: it configures firm-queue from
`DATABASES` (so firm follows Django onto the test database), creates firm's tables from
`manage.py migrate`, imports `demo/jobs.py` so workers can resolve those jobs, and adds
`manage.py firm_worker`. Django's ORM owns `demo_order`; firm owns the `firm_queue_*` /
`firm_cache_*` / `firm_channel_*` tables in the same file.
"""

from __future__ import annotations

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

SECRET_KEY = "demo-only-not-a-secret"  # noqa: S105
DEBUG = True
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = ["demo", "firm.queue.contrib.django"]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "firm-django.db",
        # firm connects with SQLAlchemy, in its own pool. Django's default in-memory test
        # database is unreachable from there, so point tests at a file.
        "TEST": {"NAME": BASE_DIR / "test-firm-django.db"},
    }
}

CACHES = {
    "default": {
        "BACKEND": "firm.cache.contrib.django.FirmCache",
        # Empty: cache in the database DATABASES["default"] already names.
        "LOCATION": "",
        # Cache-wide, not per entry — firm-cache has no expiry column, so a per-call `timeout=`
        # asking for anything else raises. See docs/django.md § The cache backend.
        "TIMEOUT": 300,
    }
}

TASKS = {
    "default": {
        "BACKEND": "firm.queue.contrib.django.backend.FirmBackend",
        "QUEUES": [],  # [] = accept any queue_name
    }
}

ROOT_URLCONF = "urls"
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGGING_CONFIG = None

# No middleware: this demo has no sessions or CSRF, which keeps `curl -d` working.
MIDDLEWARE: list[str] = []
