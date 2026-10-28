"""Small, dependency-free statistics for the refactor check (``CONTRACT.md`` section 19.4).

* :func:`summarize` – median, p95, mean, sample standard deviation of timed samples.
* :func:`mann_whitney_u` – two-sided Mann–Whitney U test, normal approximation with tie
  and continuity correction. Matches ``scipy.stats.mannwhitneyu(x, y,
  method="asymptotic")``: the statistic is U of ``x``, the p-value uses ``max(U1, U2)``.
  Timing samples of the same endpoint are rarely normal (long right tail, a few GC
  pauses), which is why a rank test is used rather than a t-test.
* :func:`geometric_mean` – for the summary's overall speed change (ratios multiply, so a
  2× slowdown and a 2× speedup cancel out).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

__all__ = [
    "coefficient_of_variation",
    "geometric_mean",
    "mann_whitney_u",
    "mean",
    "median",
    "percentile",
    "stdev",
    "summarize",
]


def mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def median(values: Sequence[float]) -> float | None:
    return percentile(values, 50)


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear interpolation between closest ranks (NumPy's default method)."""
    if not values:
        return None
    data = sorted(values)
    if len(data) == 1:
        return float(data[0])
    pos = (len(data) - 1) * q / 100.0
    lo = math.floor(pos)
    hi = min(lo + 1, len(data) - 1)
    frac = pos - lo
    return data[lo] + (data[hi] - data[lo]) * frac


def stdev(values: Sequence[float]) -> float | None:
    """Sample standard deviation (n − 1); ``0.0`` for a single value."""
    if not values:
        return None
    if len(values) == 1:
        return 0.0
    m = sum(values) / len(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def coefficient_of_variation(values: Sequence[float]) -> float | None:
    """``stdev / mean``; ``None`` when there are fewer than 2 values or the mean is 0."""
    if len(values) < 2:
        return None
    m = mean(values)
    if not m:
        return None
    s = stdev(values) or 0.0
    return s / m


def summarize(values: Sequence[float]) -> dict[str, float | None]:
    """``{median, p95, mean, stdev, cv}`` (all ``None`` without values)."""
    return {
        "median": _round(median(values)),
        "p95": _round(percentile(values, 95)),
        "mean": _round(mean(values)),
        "stdev": _round(stdev(values)),
        "cv": _round(coefficient_of_variation(values), 4),
    }


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def _ranks(values: Sequence[float]) -> tuple[list[float], list[int]]:
    """Average ranks (1-based) of ``values`` and the sizes of the tie groups."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    ties: list[int] = []
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        if j > i:
            ties.append(j - i + 1)
        i = j + 1
    return ranks, ties


def _normal_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def mann_whitney_u(
    x: Sequence[float], y: Sequence[float], *, continuity: bool = True
) -> tuple[float, float]:
    """``(U of x, two-sided p-value)``.

    Normal approximation with tie correction; ``p = 1.0`` when either side is empty or
    every value is the same (no evidence of a difference).
    """
    n1, n2 = len(x), len(y)
    if not n1 or not n2:
        return 0.0, 1.0
    ranks, ties = _ranks([*x, *y])
    r1 = sum(ranks[:n1])
    u1 = r1 - n1 * (n1 + 1) / 2.0
    u2 = n1 * n2 - u1
    n = n1 + n2
    mu = n1 * n2 / 2.0
    tie_term = sum(t**3 - t for t in ties) / (n * (n - 1)) if n > 1 else 0.0
    var = n1 * n2 / 12.0 * ((n + 1) - tie_term)
    if var <= 0:
        return u1, 1.0
    u = max(u1, u2)
    z = (u - mu - (0.5 if continuity else 0.0)) / math.sqrt(var)
    p = 2.0 * _normal_sf(z)
    return u1, min(1.0, max(0.0, p))


def geometric_mean(values: Iterable[float]) -> float | None:
    """Geometric mean of positive values (non-positive ones are skipped)."""
    logs = [math.log(v) for v in values if v > 0 and math.isfinite(v)]
    if not logs:
        return None
    return math.exp(sum(logs) / len(logs))
