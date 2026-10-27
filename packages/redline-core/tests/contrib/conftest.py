from __future__ import annotations

import os

import pytest

from redline_core.conf import Settings
from redline_core.recorder import Recorder


class Tracer:
    """Runs code inside a trace and returns the finished trace's spans."""

    def __init__(self):
        self.recorder = Recorder(
            Settings.from_mapping(
                {"project_root": os.path.dirname(os.path.dirname(__file__)), "sample_rate": 1.0}
            )
        )

    def start(self):
        return self.recorder.start_trace(method="GET", path="/")

    def finish(self, builder):
        return self.recorder.finish_trace(builder, status=200).spans


@pytest.fixture
def tracer():
    return Tracer()
