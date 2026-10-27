from __future__ import annotations

import pytest

from redline_core import Recorder, Settings
from redline_core.load import generate
from redline_core.load.scenario import parse_scenario
from redline_core.storage import SQLiteStore


def test_from_routes_plain_and_filled(tmp_path):
    store = SQLiteStore(tmp_path / "r.db", background=False)
    rec = Recorder(Settings(), store)
    b = rec.start_trace(method="GET", path="/orders/7", route="/orders/<int:id>")
    rec.finish_trace(b, status=200)
    data = generate.from_routes(
        [
            ("GET", "/", "/"),
            ("GET", "/", "/"),
            ("GET", "/orders/<int:id>", None),
            ("GET", "/users/<int:id>", None),
            ("GET", "/_redline/", "/_redline/"),
        ],
        store=store,
        base_url="http://localhost:5000",
    )
    paths = {r["path"]: r["weight"] for r in data["requests"]}
    assert paths == {"/": 1, "/orders/7": 1}
    parse_scenario(data)
    assert generate.dump(data, "x.json").startswith("{")
    assert "[[requests]]" in generate.dump(data, None)


def test_from_routes_nothing_usable():
    with pytest.raises(ValueError):
        generate.from_routes([("GET", "/a/<id>", None)])
