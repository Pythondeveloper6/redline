from __future__ import annotations

import json

from redline_core.check.diff import (
    MAX_BODY_BYTES,
    capture,
    compare_captures,
    detect_volatile,
    json_diff,
    text_diff,
    truncate_value,
)
from redline_core.check.suite import parse_path


def cap(body, ctype="application/json", status=200, **headers):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return capture(status, {"Content-Type": ctype, **headers}, raw)


def test_capture_kinds():
    j = cap({"a": 1}, "application/json; charset=UTF-8")
    assert j["body_kind"] == "json" and j["body"] == {"a": 1}
    assert j["content_type"] == "application/json;charset=utf-8"
    assert j["size"] == len(b'{"a": 1}')
    sniffed = capture(200, {"Content-Type": "text/plain"}, b"[1, 2]")
    assert sniffed["body_kind"] == "json"
    t = capture(200, {"Content-Type": "text/html"}, b"<p>hi</p>")
    assert t["body_kind"] == "text" and t["body"] == "<p>hi</p>"
    notjson = capture(200, {"Content-Type": "application/json"}, b"{oops")
    assert notjson["body_kind"] == "text"
    binary = capture(200, {"Content-Type": "image/png"}, b"\x89PNG\xff\xfe")
    assert binary["body_kind"] == "hash" and len(binary["sha256"]) == 64 and "body" not in binary
    big = capture(200, {"Content-Type": "text/plain"}, b"x" * (MAX_BODY_BYTES + 1))
    assert big["body_kind"] == "hash" and big["size"] == MAX_BODY_BYTES + 1
    empty = capture(204, {}, b"")
    assert empty["body_kind"] == "text" and empty["body"] == "" and empty["content_type"] is None


def test_capture_headers_subset_and_redaction():
    c = capture(
        200,
        [
            ("Content-Type", "application/json"),
            ("Cache-Control", "no-store"),
            ("Vary", "Accept"),
            ("Date", "Mon"),
            ("Set-Cookie", "sessionid=abc"),
        ],
        json.dumps({"user": "bob", "token": "s3cret", "nested": [{"password": "x"}]}).encode(),
        ignore_headers=["vary", "content-type"],
    )
    assert c["headers"] == {"content-type": "application/json", "cache-control": "no-store"}
    assert c["body"] == {
        "user": "bob",
        "token": "[redacted]",
        "nested": [{"password": "[redacted]"}],
    }
    long = "y" * 2000
    assert cap({"note": long})["body"]["note"] == long  # no 500-char cut


def test_capture_text_redacted():
    c = capture(200, {"Content-Type": "text/plain"}, b"api_key=abc123 visible")
    assert "abc123" not in c["body"] and "visible" in c["body"]


def test_json_diff_kinds():
    before = {"a": 1, "b": "x", "c": [1, 2, 3], "d": {"e": True}, "gone": 1, "n": 1}
    after = {"a": 2, "b": 5, "c": [1, 9], "d": {"e": True, "f": None}, "new": [], "n": 1.0}
    got = {(d["path"], d["kind"]): (d["before"], d["after"]) for d in json_diff(before, after)}
    assert got == {
        ("a", "changed"): (1, 2),
        ("b", "type"): ("x", 5),
        ("c[1]", "changed"): (2, 9),
        ("c[2]", "removed"): (3, None),
        ("d.f", "added"): (None, None),
        ("gone", "removed"): (1, None),
        ("new", "added"): (None, []),
    }
    assert list(json_diff({"a": [1]}, {"a": [1]})) == []
    assert [d["kind"] for d in json_diff(True, 1)] == ["type"]  # bool is not a number
    assert [d["path"] for d in json_diff({"a.b": 1}, {"a.b": 2})] == ['["a.b"]']
    assert [d["path"] for d in json_diff([1], {"x": 1})] == ["$"]


def test_json_diff_ignore_all_forms():
    before = {
        "data": [{"id": 1, "created_at": "t1"}, {"id": 2, "created_at": "t2"}],
        "meta": {"request_id": "r1", "page": 1},
        "a.b": {"c": 1},
        "x": {"k1": {"ts": 1}, "k2": {"ts": 2}},
    }
    after = {
        "data": [{"id": 1, "created_at": "t3"}, {"id": 2, "created_at": "t4"}],
        "meta": {"request_id": "r2", "page": 1},
        "a.b": {"c": 2},
        "x": {"k1": {"ts": 3}, "k2": {"ts": 4}},
    }
    rules = [
        parse_path(p) for p in ["data[*].created_at", "meta.request_id", '["a.b"].c', "x.*.ts"]
    ]
    assert list(json_diff(before, after, ignore=rules)) == []
    only_first = [parse_path("data[0].created_at")]
    assert [d["path"] for d in json_diff(before["data"], after["data"], ignore=[])] == [
        "[0].created_at",
        "[1].created_at",
    ]
    left = [
        d["path"]
        for d in json_diff({"data": before["data"]}, {"data": after["data"]}, ignore=only_first)
    ]
    assert left == ["data[1].created_at"]
    # a rule covers the subtree, and an added/removed key under it
    assert list(json_diff({"meta": {}}, {"meta": {"x": 1}}, ignore=[parse_path("meta")])) == []
    assert list(json_diff({"a": 1}, {"b": 2}, ignore=[parse_path("$")])) == []


def test_truncate_value():
    assert truncate_value("short") == "short"
    long = truncate_value("x" * 500)
    assert isinstance(long, str) and len(long) == 201 and long.endswith("…")
    assert truncate_value({"a": 1}) == {"a": 1}


def test_text_diff():
    assert text_diff("a\nb", "a\nb") == []
    lines = text_diff("a\nb\nc", "a\nB\nc")
    assert lines[0] == "--- before" and lines[1] == "+++ after"
    assert "-b" in lines and "+B" in lines
    assert text_diff("x\nts=1\ny", "x\nts=2\ny", volatile_lines=[2]) == []
    long = text_diff("\n".join(map(str, range(1000))), "\n".join(f"{i}!" for i in range(1000)))
    assert len(long) == 200 and long[-1].startswith("… ")


def test_detect_volatile():
    a = cap({"id": 1, "now": "t1", "items": [1, 2]}, ETag="1")
    b = cap({"id": 1, "now": "t2", "items": [1, 2, 3]}, ETag="1")
    v = detect_volatile(a, b)
    assert v == {"volatile": ["now", "items[2]"], "volatile_headers": []}
    t1 = capture(200, {"Content-Type": "text/html", "Cache-Control": "a"}, b"x\nnow 1\ny")
    t2 = capture(200, {"Content-Type": "text/html", "Cache-Control": "b"}, b"x\nnow 2\ny")
    assert detect_volatile(t1, t2) == {
        "volatile": ["line:2"],
        "volatile_headers": ["cache-control"],
    }
    h1 = capture(200, {}, b"\xff" * 10)
    h2 = capture(200, {}, b"\xfe" * 10)
    assert detect_volatile(h1, h2)["volatile"] == ["$"]
    assert detect_volatile(h1, h1)["volatile"] == []
    assert detect_volatile(a, None) == {"volatile": [], "volatile_headers": []}
    assert detect_volatile(a, t1)["volatile"] == ["$"]


def test_compare_captures_json():
    before = cap({"id": 1, "now": "t1", "name": "a"})
    after = cap({"id": 1, "now": "t9", "name": "b", "extra": True})
    out = compare_captures(before, after, ignore=["extra"], volatile=["now"])
    assert out["diff"] == [{"path": "name", "kind": "changed", "before": "a", "after": "b"}]
    assert out["diff_total"] == 1 and out["text_diff"] is None
    assert out["ignored"] == [
        {"path": "extra", "reason": "ignore"},
        {"path": "now", "reason": "volatile"},
    ]
    same = compare_captures(before, cap({"id": 1, "now": "t2", "name": "a"}), volatile=["now"])
    assert same["diff_total"] == 0


def test_compare_captures_limits_and_truncation():
    before = cap({f"k{i}": i for i in range(80)})
    after = cap({f"k{i}": i + 1 for i in range(80)})
    out = compare_captures(before, after)
    assert len(out["diff"]) == 50 and out["diff_total"] == 80
    big = compare_captures(cap({"a": "x" * 300}), cap({"a": "y" * 300}))
    assert big["diff"][0]["before"].endswith("…") and len(big["diff"][0]["before"]) == 201


def test_compare_captures_headers_and_content_type():
    before = cap({"a": 1}, **{"Cache-Control": "max-age=60", "Location": "/x"})
    after = cap({"a": 1}, "application/problem+json", **{"Cache-Control": "no-store"})
    paths = {(d["path"], d["kind"]) for d in compare_captures(before, after)["diff"]}
    assert paths == {
        ("header:content-type", "changed"),
        ("header:cache-control", "changed"),
        ("header:location", "removed"),
    }
    quiet = compare_captures(before, after, volatile_headers=["cache-control", "location"])
    assert [d["path"] for d in quiet["diff"]] == ["header:content-type"]  # never volatile


def test_compare_captures_text_and_hash():
    t1 = capture(200, {"Content-Type": "text/html"}, b"<h1>Hi</h1>\n<p>1</p>")
    t2 = capture(200, {"Content-Type": "text/html"}, b"<h1>Hello</h1>\n<p>1</p>")
    out = compare_captures(t1, t2)
    assert out["diff_total"] == 1 and "-<h1>Hi</h1>" in out["text_diff"]
    assert compare_captures(t1, t2, volatile=["line:1"])["diff_total"] == 0
    h1 = capture(200, {"Content-Type": "application/zip"}, b"\xff" * 10)
    h2 = capture(200, {"Content-Type": "application/zip"}, b"\xfe" * 10)
    hd = compare_captures(h1, h2)
    assert hd["diff"][0]["path"] == "$" and hd["diff"][0]["before"].startswith("sha256:")
    assert compare_captures(h1, dict(h1))["diff_total"] == 0
    assert compare_captures(h1, h2, volatile=["$"])["diff_total"] == 0
    kinds = compare_captures(cap({"a": 1}, "text/plain"), t1)["diff"]
    assert {"path": "$", "kind": "type", "before": "json", "after": "text"} in kinds
