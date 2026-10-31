from __future__ import annotations

import pytest
from flask import Blueprint, Flask

from flask_redline import Redline

from .conftest import ROOT, BufferedClient


@pytest.fixture
def grouped(tmp_path):
    app = Flask(__name__)
    shop = Blueprint("shop", __name__, url_prefix="/shop")
    users = Blueprint("users", __name__, url_prefix="/users")
    admin = Blueprint("admin", __name__, url_prefix="/admin")
    users.register_blueprint(Blueprint("profile", __name__, url_prefix="/profile"))

    @shop.get("/")
    def shop_index():
        return "shop"

    @shop.get("/items/<int:item_id>")
    def shop_item(item_id: int):
        return str(item_id)

    @users.get("/")
    def users_index():
        return "users"

    child = Blueprint("audit", __name__, url_prefix="/audit")

    @child.get("/")
    def audit_index():
        return "audit"

    admin.register_blueprint(child)

    @app.get("/plain")
    def plain():
        return "plain"

    for bp in (shop, users, admin):
        app.register_blueprint(bp)
    app.test_client_class = BufferedClient
    redline = Redline(
        app,
        settings={
            "storage_path": str(tmp_path / "r.db"),
            "project_root": str(ROOT),
            "access": "local",
        },
    )
    client = app.test_client()
    for path in ("/shop/", "/shop/items/1", "/shop/items/2", "/users/", "/admin/audit/", "/plain"):
        assert client.get(path).status_code == 200
    redline.store.flush(5)
    yield app, redline, client
    redline.close()


def test_blueprint_is_a_trace_tag(grouped):
    _, redline, _ = grouped
    items = redline.store.list(None, limit=50).items
    trace = redline.store.get(next(t["id"] for t in items if t["path"] == "/shop/items/1"))
    assert trace.tags["blueprint"] == "shop"
    plain = redline.store.get(next(t["id"] for t in items if t["path"] == "/plain"))
    assert "blueprint" not in plain.tags
    nested = redline.store.get(next(t["id"] for t in items if t["path"] == "/admin/audit/"))
    assert nested.tags["blueprint"] == "admin.audit"


def test_api_groups_endpoints_by_blueprint(grouped):
    _, _, client = grouped
    body = client.get("/_redline/api/endpoints?group_by=blueprint").get_json()
    assert body["group_by"] == "blueprint"
    rows = {r["route"]: r for r in body["items"]}
    assert rows["shop"]["count"] == 3 and rows["shop"]["routes"] == 2
    assert rows["shop"]["method"] == "*"
    assert rows["users"]["count"] == 1
    assert rows["admin.audit"]["count"] == 1
    assert rows["(none)"]["count"] >= 1  # /plain (and any untagged request)
    assert body["items"][0]["route"] == "shop"  # busiest first


def test_ungrouped_endpoints_are_unchanged(grouped):
    _, _, client = grouped
    body = client.get("/_redline/api/endpoints").get_json()
    assert "group_by" not in body
    assert {r["route"] for r in body["items"]} >= {"/shop/items/<int:item_id>", "/plain"}
    assert all("routes" not in r for r in body["items"])


def test_generic_tag_grouping_and_validation(grouped):
    _, _, client = grouped
    ok = client.get("/_redline/api/endpoints?group_by=tag:blueprint").get_json()
    assert {r["route"] for r in ok["items"]} >= {"shop", "users"}
    bad = client.get("/_redline/api/endpoints?group_by=nonsense")
    assert bad.status_code == 400


def test_cli_endpoints_by_blueprint(grouped):
    app, _, _ = grouped
    runner = app.test_cli_runner()
    result = runner.invoke(args=["redline", "endpoints", "--group-by", "blueprint"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].startswith("blueprint")
    assert any(line.startswith("shop") for line in lines)
    by_route = runner.invoke(args=["redline", "endpoints"])
    assert by_route.exit_code == 0 and "/shop/items/<int:item_id>" in by_route.output
    as_json = runner.invoke(args=["redline", "endpoints", "--group-by", "blueprint", "--json"])
    assert '"route": "shop"' in as_json.output
