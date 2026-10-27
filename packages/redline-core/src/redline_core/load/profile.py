"""Load profiles and the arrival schedule.

A profile is turned into a list of :class:`StepPlan` – consecutive windows, each with a
constant target rate – before the run starts. Every worker follows the same plan from a
shared start timestamp, so step boundaries line up across processes without any
messaging (see :mod:`redline_core.load.engine`).

``constant``
    one step of ``rps`` for ``duration_s``.
``ramp``
    ``start_rps`` → ``end_rps`` linearly over ``duration_s``, reported in steps of
    ``step_duration_s`` (default 5 s). Each step runs at the ramp's rate at the step's
    midpoint, so the number of arrivals equals the integral of the linear ramp.
``steps``
    ``start_rps``, ``start_rps + step_rps``, … up to ``end_rps`` (inclusive), each for
    ``step_duration_s`` (default 30 s); ``duration_s``, when given, caps the total.

``stop_on`` (``error_rate`` as a fraction, ``p95_ms``) ends the run after the first step
that breaches it; the engine evaluates it when a step has completed.
"""

from __future__ import annotations

import math
import random
import re
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

__all__ = [
    "MODES",
    "Profile",
    "ScenarioError",
    "StepPlan",
    "StopOn",
    "arrival_offsets",
    "parse_duration",
    "parse_profile",
    "parse_rate",
]

MODES = ("constant", "ramp", "steps")
DEFAULT_RAMP_STEP_S = 5.0
DEFAULT_STEPS_STEP_S = 30.0
DEFAULT_DURATION_S = 30.0
MAX_STEPS = 1_000
#: Sanity caps (every mode). A run longer than a day or faster than 100k rps is a typo.
MAX_DURATION_S = 86_400.0
MAX_RPS = 100_000.0


class ScenarioError(ValueError):
    """A scenario or profile is invalid. ``path`` names the offending key (``profile.rps``)."""

    def __init__(self, message: str, path: str | None = None, source: str | None = None) -> None:
        self.path = path
        self.source = source
        self.message = message
        super().__init__(self._render())

    def _render(self) -> str:
        where = f"{self.path}: " if self.path else ""
        src = f"{self.source}: " if self.source else ""
        return f"{src}{where}{self.message}"

    def with_source(self, source: str) -> ScenarioError:
        return ScenarioError(self.message, self.path, source)


_DURATION = re.compile(r"^\s*(\d+(?:\.\d*)?|\.\d+)\s*(ms|s|m|h)?\s*$", re.IGNORECASE)
_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, None: 1.0}


def parse_duration(value: Any, path: str = "duration") -> float:
    """``60``, ``"60s"``, ``"1.5m"``, ``"500ms"``, ``"1h"`` → seconds (> 0)."""
    if isinstance(value, bool):
        raise ScenarioError(f"must be a duration like 30s or 2m (got {value!r})", path)
    if isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str):
        m = _DURATION.match(value)
        if not m:
            raise ScenarioError(f"must be a duration like 30s, 2m or 500ms (got {value!r})", path)
        unit = m.group(2).lower() if m.group(2) else None
        seconds = float(m.group(1)) * _UNITS[unit]
    else:
        raise ScenarioError(f"must be a duration like 30s or 2m (got {value!r})", path)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ScenarioError(f"must be positive (got {value!r})", path)
    return seconds


def parse_rate(value: Any, path: str, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ScenarioError(f"must be a number of requests per second (got {value!r})", path)
    try:
        rate = float(value)
    except ValueError:
        raise ScenarioError(
            f"must be a number of requests per second (got {value!r})", path
        ) from None
    if not math.isfinite(rate) or rate < 0 or (rate == 0 and not allow_zero):
        raise ScenarioError(f"must be {'≥' if allow_zero else '>'} 0 (got {value!r})", path)
    return rate


@dataclass(frozen=True)
class StepPlan:
    """One window of the schedule: ``target_rps`` from ``start_s`` for ``duration_s``."""

    index: int  # 1-based, as in ``X-Redline-Run: <id>;step=<n>``
    target_rps: float
    start_s: float
    duration_s: float

    @property
    def end_s(self) -> float:
        return self.start_s + self.duration_s


@dataclass(frozen=True)
class StopOn:
    error_rate: float | None = None  # fraction, 0.05 = 5 %
    p95_ms: float | None = None

    def breached(self, error_rate: float, p95_ms: float) -> str | None:
        if self.error_rate is not None and error_rate > self.error_rate:
            return f"error rate {error_rate:.1%} > {self.error_rate:.1%}"
        if self.p95_ms is not None and p95_ms > self.p95_ms:
            return f"p95 {p95_ms:.0f} ms > {self.p95_ms:g} ms"
        return None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass(frozen=True)
class Profile:
    mode: str = "constant"
    rps: float | None = None
    start_rps: float | None = None
    end_rps: float | None = None
    step_rps: float | None = None
    duration_s: float | None = None
    step_duration_s: float | None = None
    stop_on: StopOn = field(default_factory=StopOn)
    poisson: bool = False

    # ------------------------------------------------------------------ factories
    @classmethod
    def constant(cls, rps: float, duration_s: float = DEFAULT_DURATION_S, **kw: Any) -> Profile:
        return parse_profile({"mode": "constant", "rps": rps, "duration_s": duration_s, **kw})

    @classmethod
    def ramp(cls, start_rps: float, end_rps: float, duration_s: float, **kw: Any) -> Profile:
        return parse_profile(
            {"mode": "ramp", "start_rps": start_rps, "end_rps": end_rps, "duration_s": duration_s}
            | kw
        )

    @classmethod
    def steps_of(
        cls, start_rps: float, end_rps: float, step_rps: float, step_duration_s: float, **kw: Any
    ) -> Profile:
        return parse_profile(
            {
                "mode": "steps",
                "start_rps": start_rps,
                "end_rps": end_rps,
                "step_rps": step_rps,
                "step_duration_s": step_duration_s,
            }
            | kw
        )

    # ------------------------------------------------------------------ plan
    def step_count(self) -> int:
        """How many steps :meth:`plan` yields, computed arithmetically (never builds it)."""
        if self.mode == "constant":
            return 1
        if self.mode == "ramp":
            total = float(self.duration_s or DEFAULT_DURATION_S)
            step = float(self.step_duration_s or DEFAULT_RAMP_STEP_S)
            return max(1, math.ceil(total / step - 1e-9))
        assert self.start_rps is not None and self.end_rps is not None
        step_rps = self.step_rps or self.start_rps
        step = float(self.step_duration_s or DEFAULT_STEPS_STEP_S)
        n = math.floor((self.end_rps - self.start_rps) / step_rps + 1e-9) + 1 if step_rps else 1
        if self.duration_s is not None:
            n = min(n, max(0, math.ceil(self.duration_s / step - 1e-9)))
        return max(0, n)

    def plan(self) -> list[StepPlan]:
        if self.step_count() > MAX_STEPS:
            raise ScenarioError(f"produces more than {MAX_STEPS} steps", "profile")
        if self.mode == "constant":
            assert self.rps is not None and self.duration_s is not None
            return [StepPlan(1, self.rps, 0.0, self.duration_s)]
        if self.mode == "ramp":
            assert self.start_rps is not None and self.end_rps is not None
            total = float(self.duration_s or DEFAULT_DURATION_S)
            step = float(self.step_duration_s or DEFAULT_RAMP_STEP_S)
            n = self.step_count()
            plans = []
            for i in range(n):
                start = i * step
                dur = min(step, total - start)
                mid = start + dur / 2
                rate = self.start_rps + (self.end_rps - self.start_rps) * (mid / total)
                plans.append(StepPlan(i + 1, round(rate, 6), start, dur))
            return plans
        # steps
        assert self.start_rps is not None and self.end_rps is not None
        step_rps = self.step_rps or self.start_rps
        step = float(self.step_duration_s or DEFAULT_STEPS_STEP_S)
        plans = []
        rate, start = self.start_rps, 0.0
        while rate <= self.end_rps + 1e-9 and len(plans) < MAX_STEPS:
            dur = step
            if self.duration_s is not None:
                dur = min(step, self.duration_s - start)
                if dur <= 1e-9:
                    break
            plans.append(StepPlan(len(plans) + 1, round(rate, 6), start, dur))
            start += dur
            rate += step_rps
        return plans

    @property
    def total_duration_s(self) -> float:
        """Sum of the step durations, computed without building the plan."""
        if self.mode == "constant":
            return float(self.duration_s or 0.0)
        if self.mode == "ramp":
            return float(self.duration_s or DEFAULT_DURATION_S)
        n = self.step_count()
        total = n * float(self.step_duration_s or DEFAULT_STEPS_STEP_S)
        return min(total, self.duration_s) if self.duration_s is not None else total

    @property
    def max_rps(self) -> float:
        """The highest target rate, computed without building the plan."""
        if self.mode == "constant":
            return float(self.rps or 0.0)
        assert self.start_rps is not None and self.end_rps is not None
        if self.mode == "ramp":
            total = float(self.duration_s or DEFAULT_DURATION_S)
            step = float(self.step_duration_s or DEFAULT_RAMP_STEP_S)
            n = self.step_count()
            first = self.start_rps + (self.end_rps - self.start_rps) * (
                min(step, total) / 2 / total
            )
            last_start = (n - 1) * step
            mid = last_start + min(step, total - last_start) / 2
            last = self.start_rps + (self.end_rps - self.start_rps) * (mid / total)
            return round(max(first, last), 6)
        n = self.step_count()
        if n <= 0:
            return 0.0
        return round(self.start_rps + (n - 1) * (self.step_rps or self.start_rps), 6)

    def describe(self) -> str:
        if self.mode == "constant":
            text = f"constant {self.rps:g} rps for {self.duration_s:g}s"
        elif self.mode == "ramp":
            text = (
                f"ramp {self.start_rps:g} → {self.end_rps:g} rps over "
                f"{self.duration_s or DEFAULT_RAMP_STEP_S:g}s "
                f"(steps of {self.step_duration_s or DEFAULT_RAMP_STEP_S:g}s)"
            )
        else:
            text = (
                f"steps {self.start_rps:g} → {self.end_rps:g} rps "
                f"(+{self.step_rps or self.start_rps:g} every "
                f"{self.step_duration_s or DEFAULT_STEPS_STEP_S:g}s)"
            )
        if self.poisson:
            text += ", poisson arrivals"
        return text

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"mode": self.mode}
        for key in ("rps", "start_rps", "end_rps", "step_rps", "duration_s", "step_duration_s"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        stop = self.stop_on.to_dict()
        if stop:
            out["stop_on"] = stop
        if self.poisson:
            out["poisson"] = True
        return out


_PROFILE_KEYS = {
    "mode",
    "rps",
    "start_rps",
    "end_rps",
    "step_rps",
    "duration_s",
    "step_duration_s",
    "stop_on",
    "poisson",
}


def _unknown(data: Mapping[str, Any], allowed: set[str], path: str) -> None:
    import difflib

    for key in data:
        if not isinstance(key, str):
            raise ScenarioError(f"keys must be strings (got {key!r})", path)
        if key in allowed or key.startswith("_"):
            continue
        hint = difflib.get_close_matches(key, sorted(allowed), n=1)
        extra = (
            f"; did you mean {hint[0]!r}?" if hint else f"; allowed: {', '.join(sorted(allowed))}"
        )
        raise ScenarioError(f"unknown key{extra}", f"{path}.{key}" if path else key)


def parse_profile(data: Any, path: str = "profile") -> Profile:
    """Validate a profile mapping (defaults applied) and return a :class:`Profile`."""
    if data is None:
        data = {}
    if isinstance(data, Profile):
        return data
    if not isinstance(data, Mapping):
        raise ScenarioError("must be a table/object", path)
    _unknown(data, _PROFILE_KEYS, path)
    mode = data.get("mode")
    if mode is None:  # infer
        if "start_rps" in data or "end_rps" in data:
            mode = "steps" if "step_rps" in data else "ramp"
        else:
            mode = "constant"
    if mode not in MODES:
        raise ScenarioError(f"must be one of {', '.join(MODES)} (got {mode!r})", f"{path}.mode")

    def rate(key: str, *, required: bool = False, default: float | None = None) -> float | None:
        if data.get(key) is None:
            if required:
                raise ScenarioError(f"is required for mode {mode!r}", f"{path}.{key}")
            return default
        return parse_rate(data[key], f"{path}.{key}", allow_zero=key == "start_rps")

    def dur(key: str, default: float | None = None) -> float | None:
        if data.get(key) is None:
            return default
        return parse_duration(data[key], f"{path}.{key}")

    stop_raw = data.get("stop_on") or {}
    if not isinstance(stop_raw, Mapping):
        raise ScenarioError("must be a table/object", f"{path}.stop_on")
    _unknown(stop_raw, {"error_rate", "p95_ms"}, f"{path}.stop_on")
    stop_on = StopOn(
        error_rate=_fraction(stop_raw.get("error_rate"), f"{path}.stop_on.error_rate"),
        p95_ms=_positive(stop_raw.get("p95_ms"), f"{path}.stop_on.p95_ms"),
    )
    poisson = data.get("poisson", False)
    if not isinstance(poisson, bool):
        raise ScenarioError(f"must be true or false (got {poisson!r})", f"{path}.poisson")

    if mode == "constant":
        profile = Profile(
            mode="constant",
            rps=rate("rps", default=10.0),
            duration_s=dur("duration_s", DEFAULT_DURATION_S),
            stop_on=stop_on,
            poisson=poisson,
        )
    elif mode == "ramp":
        profile = Profile(
            mode="ramp",
            start_rps=rate("start_rps", required=True),
            end_rps=rate("end_rps", required=True),
            duration_s=dur("duration_s", DEFAULT_DURATION_S),
            step_duration_s=dur("step_duration_s", DEFAULT_RAMP_STEP_S),
            stop_on=stop_on,
            poisson=poisson,
        )
    else:
        start = rate("start_rps", required=True)
        end = rate("end_rps", required=True)
        step_rps = rate("step_rps", default=start or None)
        if step_rps is None:
            raise ScenarioError("is required when start_rps is 0", f"{path}.step_rps")
        if end is not None and start is not None and end < start:
            raise ScenarioError(f"must be ≥ start_rps ({start:g})", f"{path}.end_rps")
        profile = Profile(
            mode="steps",
            start_rps=start,
            end_rps=end,
            step_rps=step_rps,
            duration_s=dur("duration_s"),
            step_duration_s=dur("step_duration_s", DEFAULT_STEPS_STEP_S),
            stop_on=stop_on,
            poisson=poisson,
        )
    _check_limits(profile, path)
    return profile


def _check_limits(profile: Profile, path: str) -> None:
    """Reject absurd profiles before anything is planned (arithmetic only)."""
    if profile.step_count() >= MAX_STEPS:
        key = "step_rps" if profile.mode == "steps" else "step_duration_s"
        raise ScenarioError(
            f"produces {MAX_STEPS} or more steps (use a larger step)", f"{path}.{key}"
        )
    if profile.total_duration_s > MAX_DURATION_S:
        raise ScenarioError(f"runs longer than {MAX_DURATION_S:g}s", f"{path}.duration_s")
    if profile.max_rps > MAX_RPS:
        raise ScenarioError(f"exceeds the {MAX_RPS:g} rps limit", f"{path}.rps")
    if profile.max_rps <= 0:
        raise ScenarioError("the profile never sends a request (all rates are 0)", path)


def _fraction(value: Any, path: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip().endswith("%"):
        try:
            value = float(value.strip()[:-1]) / 100.0
        except ValueError:
            raise ScenarioError(
                f"must be a fraction like 0.01 or '1%' (got {value!r})", path
            ) from None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScenarioError(f"must be a fraction like 0.01 or '1%' (got {value!r})", path)
    if not 0 <= value <= 1:
        raise ScenarioError(f"must be between 0 and 1 (got {value!r})", path)
    return float(value)


def _positive(value: Any, path: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ScenarioError(f"must be a positive number (got {value!r})", path)
    return float(value)


def arrival_offsets(
    rate: float,
    duration_s: float,
    *,
    worker: int = 0,
    workers: int = 1,
    poisson: bool = False,
    rng: random.Random | None = None,
) -> Iterator[float]:
    """Offsets (seconds from the step start) at which ``worker`` sends during one step.

    Each of ``workers`` processes gets ``rate / workers``. With fixed intervals worker ``k``
    is phase-shifted by ``k / workers`` of its interval, so the union over all workers is
    exactly one arrival every ``1 / rate`` seconds (``ceil(rate * duration)`` in total).
    With ``poisson`` the gaps are exponential with mean ``workers / rate``.
    """
    if rate <= 0 or duration_s <= 0 or workers <= 0:
        return
    if poisson:
        rng = rng or random.Random()
        share = rate / workers
        t = rng.expovariate(share)
        while t < duration_s:
            yield t
            t += rng.expovariate(share)
        return
    j = 0
    while True:
        t = (worker + j * workers) / rate
        if t >= duration_s - 1e-9:
            return
        yield t
        j += 1
