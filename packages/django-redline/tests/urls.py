from django.contrib import admin
from django.urls import include, path

from tests.testapp import drf_views, views, views_data

urlpatterns = [
    path("admin/", admin.site.urls),
    path("redline/", include("django_redline.urls")),
    path("", include(views.urlpatterns)),
    path("", include(views_data.urlpatterns)),
    path("", include(drf_views.urlpatterns)),
]
