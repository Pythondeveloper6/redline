from .conftest import client


def test_profiler_health_uses_native_access_policy(make_env):
    token = "redline-production-health-test-token"
    env = make_env(settings={"access": "token", "token": token})
    with client(env) as c:
        assert c.get("/_redline/api/health").status_code == 401
        result = c.get("/_redline/api/health", headers={"Authorization": f"Bearer {token}"})
        assert result.status_code == 200
        assert result.json()["recorder"]["available"] is True
        assert result.json()["storage"]["scope"] == "process"
