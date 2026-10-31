"""Measure precision, recall and false-positive rate of the static analyzers.

Default: run ``analyze`` (performance patterns) and ``check-sql`` over the labelled corpus in
``packages/redline-core/tests/fixtures/static_precision``. A source line may carry:

* ``# expect: <kind>``  a true positive that should be reported;
* ``# fp: <kind>``      a known false-positive shape (reported today, but the code is fine).

Every finding not on an ``expect`` line counts as a false positive; every ``expect`` line with no
matching finding is a false negative.

Your own codebase: ``measure_static_precision.py PATH [PATH ...]`` lists findings with ids.
Review them, put the ids of the ones that were wrong into a JSON list and run again with
``--false-positives reviewed.json`` to get the false-positive rate for your code.

    uv run python scripts/measure_static_precision.py [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "packages" / "redline-core" / "tests" / "fixtures" / "static_precision"
_MARK = re.compile(r"#\s*(expect|fp):\s*([a-z_-]+)")


def collect(paths):
    """All findings of both analyzers as ``{file, line, kind, id, source}`` dicts."""
    from redline_core.analyze import analyze_project
    from redline_core.check_sql import scan

    found = []
    for path in paths:
        path = Path(path).resolve()
        base = path if path.is_dir() else path.parent
        for finding in analyze_project(base)["findings"]:
            found.append({**finding, "file": str(base / finding["file"]), "source": "analyze"})
        for finding in scan([str(path)], dialect="mssql")["findings"]:
            found.append({**finding, "source": "check-sql"})
    return found


def labels(root):
    expected, known_fp = set(), set()
    for file in sorted(Path(root).rglob("*.py")):
        for number, line in enumerate(file.read_text().splitlines(), 1):
            for tag, kind in _MARK.findall(line):
                (expected if tag == "expect" else known_fp).add((str(file), number, kind))
    return expected, known_fp


def _ratio(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else None


def measure(root=CORPUS):
    findings = collect([root])
    expected, known_fp = labels(root)
    reported = {(f["file"], f["line"], f["kind"]) for f in findings}
    true_positive = reported & expected
    false_positive = reported - expected
    false_negative = expected - reported
    by_kind = {}
    for kind in sorted({k for _, _, k in reported | expected}):
        tp = sum(1 for t in true_positive if t[2] == kind)
        fp = sum(1 for t in false_positive if t[2] == kind)
        fn = sum(1 for t in false_negative if t[2] == kind)
        by_kind[kind] = {
            "true_positive": tp,
            "false_positive": fp,
            "false_negative": fn,
            "precision": _ratio(tp, tp + fp),
            "recall": _ratio(tp, tp + fn),
        }
    return {
        "corpus": str(root),
        "findings": len(reported),
        "true_positive": len(true_positive),
        "false_positive": len(false_positive),
        "false_negative": len(false_negative),
        "known_false_positive_shapes": len(known_fp),
        "known_shapes_reported": len(known_fp & reported),
        "precision": _ratio(len(true_positive), len(reported)),
        "recall": _ratio(len(true_positive), len(expected)),
        "false_positive_rate": _ratio(len(false_positive), len(reported)),
        "by_kind": by_kind,
        "unexpected": sorted(f"{Path(f).name}:{n}:{k}" for f, n, k in false_positive),
        "missed": sorted(f"{Path(f).name}:{n}:{k}" for f, n, k in false_negative),
    }


def review(paths, false_positive_ids):
    findings = collect(paths)
    wrong = set(false_positive_ids)
    unknown = wrong - {f["id"] for f in findings}
    kinds = Counter(f["kind"] for f in findings)
    wrong_kinds = Counter(f["kind"] for f in findings if f["id"] in wrong)
    return {
        "findings": len(findings),
        "reviewed_false_positives": len(wrong & {f["id"] for f in findings}),
        "unknown_ids": sorted(unknown),
        "false_positive_rate": _ratio(len(wrong) - len(unknown), len(findings)),
        "precision": None
        if not findings
        else _ratio(len(findings) - len(wrong) + len(unknown), len(findings)),
        "by_kind": {
            k: {"findings": n, "false_positive": wrong_kinds.get(k, 0)}
            for k, n in sorted(kinds.items())
        },
        "ids": [
            {"id": f["id"], "kind": f["kind"], "where": f"{f['file']}:{f['line']}"}
            for f in findings
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("paths", nargs="*", help="your own code (default: the bundled corpus)")
    parser.add_argument("--false-positives", help="JSON list of finding ids judged wrong")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    if args.paths:
        ids = json.loads(Path(args.false_positives).read_text()) if args.false_positives else []
        report = review(args.paths, ids)
    else:
        report = measure()
    if args.json:
        print(json.dumps(report, indent=2))
    elif args.paths:
        print(
            f"{report['findings']} findings; reviewed false positives: "
            f"{report['reviewed_false_positives']}; FP rate: {report['false_positive_rate']}"
        )
        for item in report["ids"]:
            print(f"  {item['id']}  {item['kind']:24} {item['where']}")
    else:
        print(
            f"findings {report['findings']}  TP {report['true_positive']}  "
            f"FP {report['false_positive']}  FN {report['false_negative']}"
        )
        print(
            f"precision {report['precision']}  recall {report['recall']}  "
            f"false-positive rate {report['false_positive_rate']}"
        )
        for kind, row in report["by_kind"].items():
            print(f"  {kind:28} P={row['precision']}  R={row['recall']}")
        for label in ("unexpected", "missed"):
            for item in report[label]:
                print(f"  {label}: {item}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
