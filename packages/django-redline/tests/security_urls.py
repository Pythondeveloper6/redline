"""URLs for the redaction regression tests (``test_security_redaction``)."""

from __future__ import annotations

from django.http import JsonResponse
from django.urls import path
from rest_framework.authentication import TokenAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response


@api_view(["GET"])
@authentication_classes([TokenAuthentication])
@permission_classes([AllowAny])
def me(request):
    return Response({"user": str(request.user)})


def touch_session(request):
    request.session["x"] = 1
    request.session.save()
    return JsonResponse({"ok": True})


def reset(request, uidb64, token):
    return JsonResponse({"ok": True})


def boom(request):
    secret = "https://bob:pw-in-url@db.internal/x?api_key=sk_live_django"
    raise RuntimeError(f"upstream failed: {secret} card 4242424242424242")


urlpatterns = [
    path("me", me),
    path("s", touch_session),
    path("reset/<uidb64>/<token>/", reset),
    path("boom", boom),
]
