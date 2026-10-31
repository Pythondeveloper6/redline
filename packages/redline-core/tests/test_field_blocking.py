import sys
from types import SimpleNamespace

from redline_core.blocking import call_name
from redline_core.detectors.blocking import _suggest


def test_missing_project_frame_does_not_name_outer_test_runner():
    capturer = SimpleNamespace(classify=lambda _: None)
    assert call_name(sys._getframe(), capturer) is None


def test_sampled_sqlalchemy_frame_does_not_imply_sync_driver():
    capturer = SimpleNamespace(classify=lambda filename: filename == __file__)
    namespace = {"__name__": "sqlalchemy.orm.loading", "sys": sys, "call_name": call_name}
    exec(
        compile(
            "def load(capturer):\n    return call_name(sys._getframe(), capturer)",
            "library.py",
            "exec",
        ),
        namespace,
    )
    name = namespace["load"](capturer)
    assert name == "SQLAlchemy work"
    advice = _suggest(name, "fastapi", None)
    assert "async SQLAlchemy" in advice.summary
    assert advice.confidence == "low"
    assert "Use the async driver" not in advice.summary


def test_confirmed_sync_driver_retains_specific_advice():
    assert "Use the async driver" in _suggest("SQLAlchemy (sync)", "fastapi", None).summary
