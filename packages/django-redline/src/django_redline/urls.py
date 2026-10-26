"""Mount with ``path("redline/", include("django_redline.urls"))``.

The UI uses hash routing and relative URLs, so any prefix works; the trailing slash of
the mount matters (``/redline`` redirects to ``/redline/`` through ``APPEND_SLASH``).
"""

from __future__ import annotations

from django.urls import path

from . import views

app_name = "redline"

urlpatterns = [
    path("", views.index, name="index"),
    path("assets/<path:path>", views.asset, name="asset"),
    path("api/<path:path>", views.api, name="api"),
]
