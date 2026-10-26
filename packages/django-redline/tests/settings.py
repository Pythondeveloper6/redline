from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "django-redline-tests"
DEBUG = False
ALLOWED_HOSTS = ["*"]
USE_TZ = True
ROOT_URLCONF = "tests.urls"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django_redline",
    "tests.testapp",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django_redline.middleware.RedlineMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
if os.environ.get("REDLINE_TEST_PG"):
    # e.g. REDLINE_TEST_PG="postgresql://postgres:postgres@localhost:5432/redline"
    from urllib.parse import urlparse

    _pg = urlparse(os.environ["REDLINE_TEST_PG"])
    DATABASES["pg"] = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": _pg.path.lstrip("/") or "redline",
        "USER": _pg.username or "",
        "PASSWORD": _pg.password or "",
        "HOST": _pg.hostname or "localhost",
        "PORT": str(_pg.port or 5432),
    }

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

STATIC_URL = "/static/"

# The project root is the test app, so frames of the test functions themselves (which
# drive the client) are "library" code, as the server would be in a real deployment.
REDLINE = {
    "STORAGE_PATH": ":memory:",
    "SAMPLE_RATE": 1.0,
    "PROJECT_ROOT": str(BASE_DIR / "tests" / "testapp"),
}
