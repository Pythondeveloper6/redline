from __future__ import annotations

import math

import pytest

from redline_core.check.stats import (
    coefficient_of_variation,
    geometric_mean,
    mann_whitney_u,
    mean,
    median,
    percentile,
    stdev,
    summarize,
)

# scipy.stats.mannwhitneyu(x, y, alternative="two-sided", method="asymptotic",
# use_continuity=True), scipy 1.x – (x, y, statistic, pvalue)
SCIPY = [
    ([1, 2, 3, 4, 5], [6, 7, 8, 9, 10], 0.0, 0.01218578035534481),
    ([1.1, 2.2, 3.3, 4.4], [1.5, 2.5, 3.5, 4.5, 5.5], 6.0, 0.3912672792826394),
    ([1, 2, 2, 3, 3, 3, 4], [2, 3, 3, 4, 4, 5, 5, 6], 11.0, 0.04988443697992589),  # ties
    (
        [5.0, 5.1, 5.2, 4.9, 5.0, 5.3, 5.1, 4.8, 5.0, 5.2],
        [6.0, 6.1, 5.9, 6.2, 6.0, 5.8, 6.3, 6.1, 6.0, 5.9],
        0.0,
        0.0001707002945198613,
    ),
]


@pytest.mark.parametrize(("x", "y", "u", "p"), SCIPY)
def test_mann_whitney_matches_scipy(x, y, u, p):
    got_u, got_p = mann_whitney_u(x, y)
    assert got_u == pytest.approx(u)
    assert got_p == pytest.approx(p, rel=1e-9)
    # symmetric p-value, complementary U
    u2, p2 = mann_whitney_u(y, x)
    assert u2 == pytest.approx(len(x) * len(y) - u)
    assert p2 == pytest.approx(p, rel=1e-9)


def test_mann_whitney_degenerate():
    assert mann_whitney_u([10, 10, 10], [10, 10, 10]) == (4.5, 1.0)  # scipy: nan
    assert mann_whitney_u([], [1, 2]) == (0.0, 1.0)
    assert mann_whitney_u([1], [2])[1] == 1.0
    _, p = mann_whitney_u(list(range(30)), list(range(30)))
    assert p > 0.9


def test_summary_stats():
    values = [1.0, 2.0, 3.0, 4.0, 10.0]
    assert median(values) == 3.0
    assert median([1, 2, 3, 4]) == 2.5
    assert mean(values) == 4.0
    assert percentile(values, 95) == pytest.approx(8.8)  # numpy linear
    assert percentile([7], 95) == 7
    assert stdev([2, 4, 4, 4, 5, 5, 7, 9]) == pytest.approx(2.13808993)
    assert stdev([3]) == 0.0
    assert coefficient_of_variation([1, 1, 1]) == 0.0
    assert coefficient_of_variation([5]) is None
    assert median([]) is None and mean([]) is None and stdev([]) is None
    s = summarize(values)
    assert s["median"] == 3.0 and s["p95"] == 8.8 and s["mean"] == 4.0
    assert s["cv"] == pytest.approx(stdev(values) / 4.0, abs=1e-4)
    assert summarize([]) == {"median": None, "p95": None, "mean": None, "stdev": None, "cv": None}


def test_geometric_mean():
    assert geometric_mean([2.0, 0.5]) == pytest.approx(1.0)  # 2× slower and 2× faster cancel
    assert geometric_mean([0.5, 0.5, 0.5]) == pytest.approx(0.5)
    assert geometric_mean([1.0, 4.0]) == pytest.approx(2.0)
    assert geometric_mean([0.0, -1.0, math.inf]) is None
    assert geometric_mean([]) is None
