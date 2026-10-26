from django.contrib import admin
from django.urls import include, path

from tests.testapp import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("redline/", include("django_redline.urls")),
    path("", include(views.urlpatterns)),
]
