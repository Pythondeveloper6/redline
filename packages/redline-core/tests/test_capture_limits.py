from __future__ import annotations

import json
import math

import pytest

from redline_core.capture_limits import bounded_value


def encoded(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()


@pytest.mark.parametrize("value", [None, True, False, 42, -3, 1.5, "hello", [], {}, (1, "x")])
def test_safe_values_preserved(value):
    result, truncated = bounded_value(value, max_bytes=1000)
    assert result == (list(value) if type(value) is tuple else value)
    assert truncated is False


@pytest.mark.parametrize("budget", range(4, 100))
def test_hard_json_budget_including_escaping_and_nesting(budget):
    value = {'quotation"': ["😀" * 40, "\n" * 50, {"nested": [1, False, None]}]}
    result, truncated = bounded_value(value, max_bytes=budget)
    assert len(encoded(result)) <= budget
    assert truncated is True


def test_string_utf8_prefix_and_large_input():
    result, truncated = bounded_value("é" * 1_000_000, max_bytes=100, max_string_bytes=7)
    assert result == "ééé"
    assert truncated and len(result.encode()) <= 7
    result, truncated = bounded_value("a" * 10_000_000, max_bytes=40)
    assert result == "a" * 38
    assert truncated and len(encoded(result)) == 40


def test_lone_surrogate_is_safely_normalized():
    result, truncated = bounded_value("a\ud800b", max_bytes=100)
    assert result == "a?b" and truncated
    result.encode("utf-8")


def test_cycle_and_depth_do_not_recurse_forever():
    cycle = []
    cycle.append(cycle)
    assert bounded_value(cycle, max_bytes=100) == ([None], True)
    assert bounded_value([[1]], max_bytes=100, max_depth=1) == ([None], True)
    assert bounded_value([1], max_bytes=100, max_depth=0) == (None, True)


def test_global_item_budget_prevents_nested_expansion():
    result, truncated = bounded_value([[1, 2, 3], [4, 5, 6]], max_bytes=1000, max_items=3)
    assert result == [[1, 2]]
    assert truncated
    assert bounded_value([1, 2], max_bytes=100, max_items=0) == ([], True)


def test_arbitrary_objects_and_subclasses_are_not_inspected():
    class Untrusted:
        def __repr__(self):
            raise AssertionError("repr must not run")

        def __str__(self):
            raise AssertionError("str must not run")

        def __iter__(self):
            raise AssertionError("iteration must not run")

    class Mapping(dict):
        def items(self):
            raise AssertionError("custom items must not run")

    class Text(str):
        def __getitem__(self, key):
            raise AssertionError("custom slicing must not run")

    for value in (Untrusted(), Mapping(x=1), Text("x")):
        assert bounded_value(value, max_bytes=100) == (None, True)
    result, truncated = bounded_value({"value": Untrusted()}, max_bytes=100)
    assert result == {"value": None} and truncated


def test_unknown_keys_and_shortened_key_collisions_are_omitted():
    result, truncated = bounded_value(
        {object(): "x", "abcdefgh": 1, "abcdXYZ": 2}, max_bytes=100, max_string_bytes=4
    )
    assert result == {"abcd": 1}
    assert truncated


@pytest.mark.parametrize(
    "value",
    [math.nan, math.inf, -math.inf, 1 << 100000],
    ids=["nan", "inf", "negative-inf", "huge-int"],
)
def test_nonfinite_and_huge_numbers_use_neutral_fallback(value):
    assert bounded_value(value, max_bytes=100) == (None, True)


def test_small_budget_fallback_is_explicit():
    assert bounded_value(False, max_bytes=4) == (None, True)
    assert bounded_value(True, max_bytes=4) == (True, False)
    result, truncated = bounded_value({"a": object()}, max_bytes=5)
    assert result == {} and truncated


@pytest.mark.parametrize(
    "options",
    [
        {"max_bytes": 3},
        {"max_bytes": True},
        {"max_bytes": 5, "max_depth": -1},
        {"max_bytes": 5, "max_depth": 65},
        {"max_bytes": 5, "max_items": False},
        {"max_bytes": 5, "max_string_bytes": -1},
    ],
)
def test_invalid_limits_rejected(options):
    with pytest.raises(ValueError):
        bounded_value(None, **options)


def test_concurrent_mapping_change_keeps_a_partial_bounded_value(monkeypatch):
    import redline_core.capture_limits as capture_limits

    original = json.dumps
    value = {"a": 1, "b": 2}
    changed = False

    def encode_with_change(item, **kwargs):
        nonlocal changed
        if item == "a" and not changed:
            changed = True
            value["c"] = 3
        return original(item, **kwargs)

    monkeypatch.setattr(capture_limits.json, "dumps", encode_with_change)
    result, truncated = bounded_value(value, max_bytes=100)
    assert result == {"a": 1}
    assert truncated and len(encoded(result)) <= 100
