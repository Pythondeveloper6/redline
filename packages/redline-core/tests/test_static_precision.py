"""The labelled static-analysis corpus keeps its measured precision and recall."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "measure_static_precision.py"
pytestmark = pytest.mark.skipif(not SCRIPT.is_file(), reason="repository scripts not available")

#: Floors sit just under today's measured values; raising them is the goal, lowering is a
#: regression to be justified.
PRECISION_FLOOR = 0.80
RECALL_FLOOR = 0.95


@pytest.fixture(scope="module")
def harness():
    spec = importlib.util.spec_from_file_location("measure_static_precision", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_corpus_precision_and_recall_stay_above_floor(harness):
    report = harness.measure()
    assert report["precision"] >= PRECISION_FLOOR, report["unexpected"]
    assert report["recall"] >= RECALL_FLOOR, report["missed"]
    assert report["false_positive_rate"] <= 1 - PRECISION_FLOOR


def test_known_false_positive_shapes_are_still_visible_not_silently_dropped(harness):
    report = harness.measure()
    assert report["known_false_positive_shapes"] >= 2
    # They count against precision until the analyzer learns to avoid them.
    assert report["false_positive"] >= report["known_shapes_reported"]


def test_clean_shapes_stay_silent(harness):
    report = harness.measure()
    # Only the lines labelled "# fp:" may be reported; clean shapes never are.
    fixture = harness.CORPUS / "loops_clean_and_false_positive_shapes.py"
    labelled = {f"{fixture.name}:{n}:{kind}" for _, n, kind in harness.labels(harness.CORPUS)[1]}
    assert set(report["unexpected"]) <= labelled


def test_reviewing_your_own_code_computes_a_false_positive_rate(harness, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "views.py").write_text(
        "def a(orders):\n    for o in orders:\n        Customer.objects.get(pk=o)\n"
        "def b(orders):\n    for o in orders:\n        Tag.objects.get(pk=o)\n"
    )
    first = harness.review([project], [])
    assert first["findings"] == 2 and first["false_positive_rate"] == 0.0
    wrong = [first["ids"][0]["id"]]
    second = harness.review([project], wrong)
    assert second["false_positive_rate"] == 0.5
    assert second["by_kind"]["query_in_loop"] == {"findings": 2, "false_positive": 1}
    path = tmp_path / "fp.json"
    path.write_text(json.dumps(wrong))
    assert harness.main([str(project), "--false-positives", str(path), "--json"]) == 0
