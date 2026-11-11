"""Regression tests for the 0.5.0b1 surfaces: access gating, output hygiene, redirects."""

from __future__ import annotations

import pytest

from redline_core.finding_explain import explain_issue

from .test_access import TOKEN, gate, req


@pytest.mark.parametrize("path", ["api/instrumentation", "api/issues"])
def test_new_api_routes_need_credentials(path):
    g = gate(access="token", token=TOKEN)
    d = g.check(req(path, ip="203.0.113.9", host="redline.example"))
    assert (d.status, d.code) == (401, "login_required")
    ok = g.check(req(path, ip="203.0.113.9", authorization=f"Bearer {TOKEN}"))
    assert ok.allowed


def test_explanation_never_echoes_non_numeric_evidence():
    hostile = "<script>alert(1)</script>"
    why = explain_issue(
        "deep_offset",
        {"offset": hostile, "threshold": hostile, "bytes": hostile, "threshold_ms": hostile},
        1,
    )
    blob = " ".join(str(v) for v in why.values())
    assert "<script>" not in blob


def test_live_compare_does_not_follow_redirects():
    httpx = pytest.importorskip("httpx")
    from redline_core.check.live import run_live_compare
    from redline_core.check.suite import _parse_suite

    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})

    suite = _parse_suite(
        {"name": "s", "requests": [{"name": "a", "path": "/a"}]}, default_name="s", source=None
    )
    transport = httpx.MockTransport(handler)
    run_live_compare(
        suite,
        old_url="http://old.test",
        new_url="http://new.test",
        rounds=1,
        transport_old=transport,
        transport_new=transport,
    )
    assert all("169.254" not in url for url in seen) and len(seen) == 2


def test_verify_cli_rejects_non_report_files(tmp_path):
    from redline_core import cli

    p = tmp_path / "x.json"
    p.write_text('{"hello": 1}')
    assert cli.main(["check", "verify", str(p), "--kind", "n_plus_one"]) == 2
