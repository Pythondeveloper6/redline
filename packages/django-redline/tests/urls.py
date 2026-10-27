from django.contrib import admin
from django.urls import include, path

from tests.testapp import drf_views, views, views_async_tx, views_data
from tests.testapp.ninja_api import api as ninja_api

urlpatterns = [
    path("admin/", admin.site.urls),
    path("redline/", include("django_redline.urls")),
    path("api/", ninja_api.urls),
    path("", include(views.urlpatterns)),
    path("", include(views_data.urlpatterns)),
    path("", include(drf_views.urlpatterns)),
    path("", include(views_async_tx.urlpatterns)),
]
