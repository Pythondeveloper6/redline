from __future__ import annotations

import time
import typing

from redline_core.detect import detect
from redline_core.finding_explain import explain_issue
from redline_core.model import ISSUE_KINDS

from .test_api import api, db, seeded  # noqa: F401  (fixtures)

KEYS = {"rule", "observed", "threshold", "grouping", "limits", "sampled"}


def test_every_issue_kind_has_explanation():
    assert ISSUE_KINDS
    for kind in ISSUE_KINDS:
        why = explain_issue(kind, {}, 1)
        assert set(why) == KEYS, kind
        assert why["rule"] and why["grouping"] and why["limits"], kind
        assert why["sampled"] is True


def test_n_plus_one_mentions_count_and_threshold():
    why = explain_issue("n_plus_one", {"threshold": 5}, 12)
    assert "12" in why["observed"]
    assert "5" in why["threshold"]


def test_deep_offset_uses_extra_evidence():
    why = explain_issue("deep_offset", {"offset": 90000, "threshold": 10000}, 1)
    assert "90000" in why["observed"] and "10000" in why["threshold"]


def test_unknown_kind_and_bad_extra_are_generic():
    why = explain_issue("zzz", None, 0)  # type: ignore[arg-type]
    assert set(why) == KEYS and why["threshold"] is None


def test_incomplete_trace_is_called_out():
    why = explain_issue("slow_query", {"incomplete_trace": True}, 1)
    assert "incomplete" in why["limits"].lower()


def test_api_issues_item_has_why(api, seeded):  # noqa: F811
    status, data = api.dispatch("GET", "api/issues", {"window": "7d", "kind": "n_plus_one"})
    assert status == 200
    why = data["items"][0]["why"]
    assert set(why) == KEYS and "5" in why["observed"]
