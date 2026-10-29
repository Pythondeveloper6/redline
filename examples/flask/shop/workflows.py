"""Disposable real-work examples for SQL failures, jobs and privacy controls."""

from __future__ import annotations

import logging
import uuid

from flask import Blueprint, current_app, request
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from redline_core import job, row_outcome, stage
from redline_core.jobs import propagation_headers

from .db import sync_engine

bp = Blueprint("workflows", __name__)


def record_example_job(recorder, *, fail_summary=True, task_id=None, parent=None):
    """Commit to an isolated in-memory database, then optionally fail the summary."""
    engine = create_engine("sqlite://")
    try:
        try:
            with job(
                "demo.product-import",
                recorder=recorder,
                task_id=task_id or uuid.uuid4().hex,
                queue="demo-imports",
                parent=parent,
            ):
                with stage("write"):
                    with engine.begin() as conn:
                        conn.exec_driver_sql("CREATE TABLE imported_items (id INTEGER)")
                        conn.exec_driver_sql("INSERT INTO imported_items VALUES (1)")
                    row_outcome("succeeded")
                with stage("summary"):
                    if fail_summary:
                        row_outcome("failed", "summary unavailable", row_id="never-recorded")
                        raise ValueError("Demo summary failed after its isolated write committed")
        except ValueError:
            # The job recording retains the exception; this driver returns its trace for inspection.
            pass
        return recorder.last_trace
    finally:
        engine.dispose()


@bp.get("/workflows")
def workflows():
    return {
        "database": "SQLite; the job database is disposable and in memory",
        "requests": {
            "GET /workflows/handled-sql": "caught SQL failure with HTTP 200",
            "GET /workflows/dynamic-engine": "a late-created SQLAlchemy engine",
            "POST /workflows/private": "JSON/SQL/log value redaction",
            "POST /workflows/job": "job, stages, row outcomes and commit-before-failure",
        },
        "ui": {
            "requests": "/_redline/#/requests",
            "jobs": "/_redline/#/jobs",
            "issues": "/_redline/#/issues",
            "analysis": "/_redline/#/analysis",
        },
        "correlation": "Send X-Request-ID and optional numeric X-Demo-User-ID headers",
    }


@bp.get("/workflows/handled-sql")
def handled_sql():
    with sync_engine.connect() as conn:
        try:
            conn.execute(text("SELECT unavailable_column FROM unavailable_demo_table"))
        except SQLAlchemyError:
            return {"ok": True, "degraded": True, "reason": "demo SQL failure was caught"}
    raise AssertionError("The deliberately absent table unexpectedly exists")


@bp.get("/workflows/dynamic-engine")
def dynamic_engine():
    engine = create_engine("sqlite://")
    try:
        with engine.connect() as conn:
            value = conn.exec_driver_sql("SELECT 42").scalar_one()
        return {"value": value, "engine": "created inside this request", "vendor": "sqlite"}
    finally:
        engine.dispose()


@bp.post("/workflows/private")
def private_values():
    payload = request.get_json(force=True)
    if not isinstance(payload, dict):
        return {"error": "send a JSON object"}, 400
    with sync_engine.connect() as conn:
        conn.execute(text("SELECT :value"), {"value": payload.get("notes", "invented sample")})
    logging.getLogger("shop.workflows").warning("Private demo note: %s", payload.get("notes"))
    # The app returns its own response normally. Redline's persisted copy is masked by route policy.
    return {"accepted": True, "payload": payload}


@bp.post("/workflows/job")
def run_job():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict) or not isinstance(payload.get("fail_summary", True), bool):
        return {"error": "fail_summary must be a boolean"}, 400
    recorder = current_app.extensions["redline"].recorder
    trace = record_example_job(
        recorder,
        fail_summary=payload.get("fail_summary", True),
        parent=propagation_headers(),
    )
    return {
        "trace_id": trace.id,
        "outcome": trace.tags.get("job.outcome", "not_recorded"),
        "commit_observed_before_failure": trace.tags.get("commit_observed_before_failure")
        == "true",
        "storage": "The write used an isolated in-memory database, never shop.db",
        "open": f"/_redline/#/requests/{trace.id}",
    }
