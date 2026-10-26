"""A DRF-shaped viewset + serializer pair (DRF itself is not a test dependency: the
suggester recognises serializers by their base class name)."""

from django.views import View

from .models import Order


class Serializer:
    def __init__(self, instance=None, many=False):
        self.instance = instance


class OrderSerializer(Serializer):
    def item_count(self, obj):
        return len(obj.items.all())


class OrderViewSet(View):
    serializer_class = OrderSerializer

    def get_queryset(self):
        return Order.objects.filter(status="new")
