from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("redline/", include("django_redline.urls")),
    path("", include("shop.urls")),
]
