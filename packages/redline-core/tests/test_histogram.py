from __future__ import annotations

import math
import random

import pytest

from redline_core.histogram import MAX_MS, MIN_MS, Histogram


def exact(values: list[float], p: float) -> float:
    s = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(s)))
    return s[rank - 1]


def test_empty():
    h = Histogram()
    assert h.count == 0 and h.max == 0 and h.min == 0 and h.mean == 0
    assert h.percentile(50) == 0
    assert Histogram.from_bytes(h.to_bytes()).count == 0


def test_basic_stats():
    h = Histogram([1, 2, 3, 4, 100])
    assert h.count == 5
    assert h.min == 1 and h.max == 100
    assert h.mean == pytest.approx(22.0)
    assert h.percentile(0) == 1 and h.percentile(100) == 100


@pytest.mark.parametrize("dist", ["uniform", "lognormal", "bimodal", "wide"])
def test_percentile_accuracy_vs_exact(dist):
    rng = random.Random(42)
    if dist == "uniform":
        values = [rng.uniform(1, 500) for _ in range(20_000)]
    elif dist == "lognormal":
        values = [rng.lognormvariate(3, 1.2) for _ in range(20_000)]
    elif dist == "bimodal":
        values = [
            rng.gauss(10, 1) if rng.random() < 0.9 else rng.gauss(800, 50) for _ in range(20_000)
        ]
        values = [max(v, 0.01) for v in values]
    else:
        values = [10 ** rng.uniform(-3, 6) for _ in range(20_000)]
    h = Histogram()
    h.record_many(values)
    for p in (1, 10, 25, 50, 75, 90, 95, 99, 99.9):
        want = exact(values, p)
        got = h.percentile(p)
        assert got == pytest.approx(want, rel=0.011), (dist, p)


def test_clamping_and_nan():
    h = Histogram()
    h.record(0)
    h.record(-5)
    h.record(10**9)
    h.record(float("nan"))
    assert h.count == 3
    assert h.min == MIN_MS and h.max == MAX_MS
    assert h.percentile(50) >= MIN_MS


def test_percentile_bounds_checked():
    with pytest.raises(ValueError):
        Histogram([1]).percentile(101)


def test_estimates_stay_inside_min_max():
    h = Histogram([5.0, 5.0, 5.0])
    for p in (1, 50, 99):
        assert 5.0 == pytest.approx(h.percentile(p), rel=0.0)


def test_record_with_count():
    h = Histogram()
    h.record(10, count=99)
    h.record(1000)
    assert h.count == 100
    assert h.percentile(50) == pytest.approx(10, rel=0.011)
    assert h.percentile(100) == 1000


def test_merge_equals_recording_everything():
    rng = random.Random(1)
    a_vals = [rng.lognormvariate(2, 1) for _ in range(3000)]
    b_vals = [rng.lognormvariate(4, 0.5) for _ in range(2000)]
    a, b, both = Histogram(a_vals), Histogram(b_vals), Histogram(a_vals + b_vals)
    merged = a.copy().merge(b)
    assert merged == both
    assert merged.count == both.count and merged.max == both.max and merged.min == both.min
    assert merged.mean == pytest.approx(both.mean)
    assert merged.percentile(95) == both.percentile(95)
    assert a.count == 3000  # copy() left the original alone


def test_merge_associative_and_commutative():
    rng = random.Random(7)
    parts = [Histogram([rng.uniform(0.1, 900) for _ in range(500)]) for _ in range(3)]
    a, b, c = parts
    left = a.copy().merge(b).merge(c)
    right = a.copy().merge(b.copy().merge(c))
    swapped = c.copy().merge(a).merge(b)
    assert left == right == swapped
    for p in (50, 95, 99):
        assert left.percentile(p) == right.percentile(p) == swapped.percentile(p)


def test_merge_empty_is_noop():
    h = Histogram([1, 2, 3])
    before = h.to_bytes()
    h.merge(Histogram())
    assert h.to_bytes() == before
    e = Histogram().merge(h)
    assert e == h and e.min == 1


def test_bytes_roundtrip():
    rng = random.Random(3)
    h = Histogram([10 ** rng.uniform(-2, 5) for _ in range(5000)])
    blob = h.to_bytes()
    back = Histogram.from_bytes(blob)
    assert back == h
    assert (back.count, back.min, back.max) == (h.count, h.min, h.max)
    assert back.mean == pytest.approx(h.mean)
    assert back.percentile(99) == h.percentile(99)
    assert len(blob) < 4000  # sparse: far smaller than one number per sample


def test_bytes_are_compact_for_typical_endpoint():
    rng = random.Random(5)
    h = Histogram([rng.lognormvariate(3, 0.6) for _ in range(100_000)])
    assert len(h.to_bytes()) < 1000


@pytest.mark.parametrize("bad", [b"", b"xx", b"RLH", b"RLH\x09abc", b"RLH\x01notzlib"])
def test_from_bytes_rejects_garbage(bad):
    with pytest.raises(ValueError):
        Histogram.from_bytes(bad)


def test_from_bytes_rejects_truncated_payload():
    blob = Histogram([1, 2, 3]).to_bytes()
    with pytest.raises(ValueError):
        Histogram.from_bytes(blob[:-3])


def test_range_extremes_roundtrip():
    h = Histogram([MIN_MS, MAX_MS])
    back = Histogram.from_bytes(h.to_bytes())
    assert back == h and back.max == MAX_MS
