"""``redline load``: an open-model HTTP load tester (``CONTRACT.md`` section 13).

Requires the ``load`` extra (``pip install 'redline-core[load]'``, i.e. httpx). Never
imported by ``redline_core`` itself.

::

    from redline_core.load import Profile, load_scenario, run, MemorySink

    if __name__ == "__main__":
        result = run(
            load_scenario("scenario.toml"),
            profile=Profile.steps_of(10, 100, 10, step_duration_s=20),
            sink=MemorySink(),
        )
        print(result.summary["verdict"], result.summary["max_ok_rps"])

See :mod:`redline_core.load.engine` for the process model, the clocking and the
coordinated-omission correction, and :mod:`redline_core.load.control` for background runs.
"""

from __future__ import annotations

from .analysis import StepResult, analyze, check_thresholds, find_knee
from .control import StopFile, launch_background, runs_dir, stop_run
from .engine import RunResult, default_workers, run
from .profile import Profile, ScenarioError, StepPlan, StopOn, arrival_offsets, parse_profile
from .scenario import RequestSpec, Scenario, load_scenario, parse_scenario
from .sinks import JsonSink, MemorySink, MultiSink, RunMeta, RunSink, StoreSink

__all__ = [
    "JsonSink",
    "MemorySink",
    "MultiSink",
    "Profile",
    "RequestSpec",
    "RunMeta",
    "RunResult",
    "RunSink",
    "Scenario",
    "ScenarioError",
    "StepPlan",
    "StepResult",
    "StopFile",
    "StopOn",
    "StoreSink",
    "analyze",
    "arrival_offsets",
    "check_thresholds",
    "default_workers",
    "find_knee",
    "launch_background",
    "load_scenario",
    "parse_profile",
    "parse_scenario",
    "run",
    "runs_dir",
    "stop_run",
]
