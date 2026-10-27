from django.contrib import admin
from django.urls import include, path

from tests.testapp import views
from tests.testapp.ninja_api import api as ninja_api

urlpatterns = [
    path("admin/", admin.site.urls),
    path("redline/", include("django_redline.urls")),
    path("api/", ninja_api.urls),
    path("", include(views.urlpatterns)),
]
