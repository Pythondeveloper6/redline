import pytest
from django.test import Client

pytestmark = pytest.mark.django_db


def test_profiler_health_uses_native_access_policy(staff_client):
    assert Client().get("/redline/api/health").status_code == 403
    result = staff_client.get("/redline/api/health")
    assert result.status_code == 200
    assert result.json()["recorder"]["available"] is True
    assert result.json()["storage"]["scope"] == "process"
