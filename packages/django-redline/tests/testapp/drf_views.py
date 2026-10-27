"""Real DRF serializers and views: ``serializer`` spans are recorded for the outermost one."""

import time

from django.urls import path
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Order, OrderItem


class ItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderItem
        fields = ["id", "qty"]


class OrderSerializer(serializers.ModelSerializer):
    items = ItemSerializer(many=True, read_only=True)
    total = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = ["id", "status", "items", "total"]

    def get_total(self, obj):
        if self.context.get("slow"):
            time.sleep(0.06)
        return sum(i.qty for i in obj.items.all())


class OrderList(APIView):
    def get(self, request):
        orders = Order.objects.prefetch_related("items")
        return Response(OrderSerializer(orders, many=True).data)


class OrderOne(APIView):
    def get(self, request):
        order = Order.objects.prefetch_related("items").first()
        slow = bool(request.GET.get("slow"))
        return Response(OrderSerializer(order, context={"slow": slow}).data)


urlpatterns = [
    path("drf/orders/", OrderList.as_view()),
    path("drf/order/", OrderOne.as_view()),
]
