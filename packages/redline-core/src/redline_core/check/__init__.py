"""``redline check``: prove a refactor is safe (``CONTRACT.md`` section 19).

Record a **baseline** of a suite of read-only requests against the old code, run the same
suite against the new code, and get per-request performance and response verdicts plus an
overall summary. Needs the ``load`` extra (httpx) to run; loading suites, baselines and
reports does not. Never imported by ``redline_core`` itself.

::

    from redline_core.check import Baseline, Suite, run_suite

    suite = Suite.from_file("check.toml")
    baseline = run_suite(suite, base_url="http://127.0.0.1:8000", kind="baseline")
    baseline.write("check.baseline.json")
    # … deploy the refactor …
    report = run_suite(suite, base_url="http://127.0.0.1:8000", kind="compare",
                       baseline=Baseline.from_file("check.baseline.json"))
    print(report.to_terminal())
    raise SystemExit(report.exit_code())
"""

from __future__ import annotations

from .baseline import Baseline, BaselineError, default_baseline_path, git_info
from .report import (
    DEFAULT_FAIL_ON,
    Report,
    ReportError,
    build_report,
    compare_request,
    parse_fail_on,
)
from .runner import (
    CheckResult,
    CheckSink,
    CheckUnreachable,
    ServerMetrics,
    check_header,
    run_suite,
    schedule,
)
from .suite import CheckRequest, Suite, SuiteError, Thresholds, parse_path

__all__ = [
    "DEFAULT_FAIL_ON",
    "Baseline",
    "BaselineError",
    "CheckRequest",
    "CheckResult",
    "CheckSink",
    "CheckUnreachable",
    "Report",
    "ReportError",
    "ServerMetrics",
    "Suite",
    "SuiteError",
    "Thresholds",
    "build_report",
    "check_header",
    "compare_request",
    "default_baseline_path",
    "git_info",
    "parse_fail_on",
    "parse_path",
    "run_suite",
    "schedule",
]
