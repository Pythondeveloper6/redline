from __future__ import annotations

from redline_core.check.verify import verify_report


def entry(
    *,
    before_issues=(("n_plus_one", "fp1"),),
    after_issues=(),
    calls=10,
    response="same",
    perf="same",
    note=None,
):
    def issues(items):
        return [{"kind": k, "fingerprint": f} for k, f in items]

    server = {
        "before": {"issues": issues(before_issues)},
        "after": {"issues": issues(after_issues), "complete_spans": True},
        "new_issues": [],
        "fixed_issues": [],
    }
    if note:
        server["note"] = note
    return {
        "name": "list",
        "calls": [10, calls],
        "server": server,
        "response": {"verdict": response},
        "perf": {"verdict": perf},
    }


def test_gone_with_enough_samples():
    out = verify_report([entry(perf="faster")], "n_plus_one", min_samples=5)
    assert out["finding"] == "gone" and out["responses"] == "equal" and out["speed"] == "faster"


def test_still_present():
    out = verify_report([entry(after_issues=(("n_plus_one", "fp1"),))], "n_plus_one")
    assert out["finding"] == "still_present"


def test_inconclusive_when_too_few_samples():
    out = verify_report([entry(calls=2)], "n_plus_one", min_samples=5)
    assert out["finding"] == "inconclusive" and "samples" in out["reason"]


def test_inconclusive_when_server_metrics_missing():
    out = verify_report([entry(note="server-side metrics unavailable")], "n_plus_one")
    assert out["finding"] == "inconclusive"


def test_not_observed_before_is_inconclusive():
    out = verify_report([entry(before_issues=())], "n_plus_one")
    assert out["finding"] == "inconclusive" and "baseline" in out["reason"]


def test_changed_response_is_reported():
    out = verify_report([entry(response="changed")], "n_plus_one")
    assert out["finding"] == "gone" and out["responses"] == "changed"


def test_fingerprint_filter_and_slower():
    e = entry(after_issues=(("n_plus_one", "other"),), perf="slower")
    out = verify_report([e], "n_plus_one", fingerprint="fp1")
    assert out["finding"] == "gone" and out["speed"] == "slower"


def test_empty_requests():
    assert verify_report([], "n_plus_one")["finding"] == "inconclusive"


def _report_file(tmp_path, entries):
    import json

    from redline_core.check.report import FORMAT, VERSION

    p = tmp_path / "r.json"
    p.write_text(
        json.dumps(
            {
                "format": FORMAT,
                "version": VERSION,
                "id": "r1",
                "summary": {},
                "requests": entries,
            }
        )
    )
    return str(p)


def test_cli_verify_exit_codes(tmp_path, capsys):
    from redline_core import cli

    gone = _report_file(tmp_path, [entry()])
    assert cli.main(["check", "verify", gone, "--kind", "n_plus_one"]) == 0
    assert "gone" in capsys.readouterr().out
    bad = _report_file(tmp_path, [entry(after_issues=(("n_plus_one", "fp1"),))])
    assert cli.main(["check", "verify", bad, "--kind", "n_plus_one"]) == 1
    assert cli.main(["check", "verify", str(tmp_path / "nope.json"), "--kind", "x"]) == 2
    assert cli.main(["check", "verify", gone, "--kind", "n_plus_one", "--min-samples", "-1"]) == 2


def test_cli_verify_json(tmp_path, capsys):
    import json

    from redline_core import cli

    code = cli.main(
        ["check", "verify", _report_file(tmp_path, [entry()]), "--kind", "n_plus_one", "--json"]
    )
    assert code == 0 and json.loads(capsys.readouterr().out)["finding"] == "gone"
