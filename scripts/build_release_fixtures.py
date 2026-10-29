"""Generate fixed, explicitly labeled development examples using the core engines."""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import create_engine

from redline_core import Recorder, Settings, job, stage
from redline_core.check_sql import analyze_sql_source
from redline_core.contrib.sqlalchemy import instrument_engine

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = (
    "from sqlalchemy import text\n\n"
    'conn.execute(text("INSERT INTO dbo.items (name) OUTPUT inserted.id '
    'VALUES (:name) -- bind :name"), {"name": "example"})\n'
)
report = analyze_sql_source(EXAMPLE, file="input.py", dialect="mssql")
report["note"] += " Fixed generated example in demo mode; arbitrary source needs a backend."
(ROOT / "frontend/src/lib/sqlExample.ts").write_text(
    "// Fixed offline example generated with scripts/build_release_fixtures.py.\n"
    + "export const SQL_EXAMPLE = "
    + json.dumps(EXAMPLE)
    + "\n"
    + "export const SQL_EXAMPLE_REPORT = "
    + json.dumps(report, indent=2)
    + "\n"
)
recorder = Recorder(Settings(project_root=str(ROOT), capture_logs=False, capture_locals=False))
engine = create_engine("sqlite://")
instrument_engine(engine)
try:
    with job("example-import", recorder=recorder, task_id="example-task"):
        with stage("write"):
            with engine.begin() as conn:
                conn.exec_driver_sql("CREATE TABLE items (id INTEGER)")
                conn.exec_driver_sql("INSERT INTO items VALUES (1)")
        with stage("summary"):
            raise ValueError("Example summary failed after the write committed")
except ValueError:
    pass
finally:
    engine.dispose()
trace = recorder.last_trace
trace.id = "example_job_failure"
trace.tags["synthetic_demo"] = "true"
trace.tags["demo_case"] = "job_failure"
(ROOT / "frontend/src/mocks/jobExample.ts").write_text(
    "// Fixed synthetic workflow example; durations are illustrative.\n"
    + "export const JOB_EXAMPLE = "
    + json.dumps({**trace.to_dict(), "has_profile": trace.has_profile}, indent=2)
    + "\n"
)
