from django.urls import path

from . import views

urlpatterns = [
    path("", views.home),
    path("orders/", views.order_list),
    path("orders/page/", views.order_page),
    path("items/", views.item_list),
    path("dashboard/", views.dashboard),
    path("reviews/search/", views.review_search),
    path("slow/", views.slow),
    path("boom/", views.boom),
    path("api/products/", views.api_products),
    path("async/summary/", views.async_summary),
    path("cached/", views.cached),
    path("status/", views.status),
]
