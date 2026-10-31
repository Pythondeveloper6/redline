// Fixed synthetic workflow example; durations are illustrative.
export const JOB_EXAMPLE = {
  "id": "example_job_failure",
  "framework": "other",
  "method": "TASK",
  "path": "example-import",
  "route": "example-import",
  "view": null,
  "status": 500,
  "started_at": 1793200105.64995,
  "duration_ms": 2.526,
  "query_count": 2,
  "query_ms": 0.14,
  "request": {
    "headers": {},
    "query": {},
    "body": null,
    "body_truncated": false,
    "client": null
  },
  "response": {
    "headers": {},
    "body": null,
    "body_truncated": false,
    "size": null
  },
  "spans": [
    {
      "id": "cec52d8f950e49389ffdd01609705062",
      "parent_id": null,
      "kind": "custom",
      "name": "write",
      "start_ms": 0.063,
      "duration_ms": 2.44,
      "callsite": null,
      "data": {
        "job_stage": true
      }
    },
    {
      "id": "09b853dedbf94be88097d8455dae7187",
      "parent_id": "cec52d8f950e49389ffdd01609705062",
      "kind": "transaction",
      "name": "transaction default",
      "start_ms": 0.946,
      "duration_ms": 1.518,
      "callsite": {
        "file": "scripts/build_release_fixtures.py",
        "line": 37,
        "function": "<module>",
        "code": "with engine.begin() as conn:",
        "stack": [
          {
            "file": "scripts/build_release_fixtures.py",
            "line": 37,
            "function": "<module>"
          }
        ]
      },
      "data": {
        "db_alias": "default",
        "outcome": "commit",
        "queries": 2,
        "http_out": 0
      }
    },
    {
      "id": "b707bec1b14e452c9e11d8f64a66b2d9",
      "parent_id": "cec52d8f950e49389ffdd01609705062",
      "kind": "sql",
      "name": "CREATE TABLE items (id INTEGER)",
      "start_ms": 0.98,
      "duration_ms": 0.126,
      "callsite": {
        "file": "scripts/build_release_fixtures.py",
        "line": 38,
        "function": "<module>",
        "code": "conn.exec_driver_sql(\"CREATE TABLE items (id INTEGER)\")",
        "stack": [
          {
            "file": "scripts/build_release_fixtures.py",
            "line": 38,
            "function": "<module>"
          }
        ]
      },
      "data": {
        "sql": "CREATE TABLE items (id INTEGER)",
        "params": [],
        "fingerprint": "CREATE TABLE items (id INTEGER)",
        "db_alias": "default",
        "vendor": "sqlite",
        "rows": null,
        "many": false,
        "explainable": false,
        "params_captured": true,
        "read_query": false,
        "has_limit": false,
        "rowcount_raw": -1,
        "statement_kinds": [
          "CREATE"
        ],
        "comment_placeholders": 0,
        "connection": {
          "age_s": 0.0,
          "statement_number": 1,
          "pool_recycle": -1,
          "pool_pre_ping": false,
          "observed_connects_hour": 1,
          "observed_fresh_failures_hour": 0,
          "pid": 77857
        },
        "in_transaction": true
      }
    },
    {
      "id": "badb923fc704470d8948847f0a678ab6",
      "parent_id": "cec52d8f950e49389ffdd01609705062",
      "kind": "sql",
      "name": "INSERT INTO items VALUES (...)",
      "start_ms": 2.363,
      "duration_ms": 0.014,
      "callsite": {
        "file": "scripts/build_release_fixtures.py",
        "line": 39,
        "function": "<module>",
        "code": "conn.exec_driver_sql(\"INSERT INTO items VALUES (1)\")",
        "stack": [
          {
            "file": "scripts/build_release_fixtures.py",
            "line": 39,
            "function": "<module>"
          }
        ]
      },
      "data": {
        "sql": "INSERT INTO items VALUES (1)",
        "params": [],
        "fingerprint": "INSERT INTO items VALUES (...)",
        "db_alias": "default",
        "vendor": "sqlite",
        "rows": 1,
        "many": false,
        "explainable": false,
        "params_captured": true,
        "read_query": false,
        "has_limit": false,
        "rowcount_raw": 1,
        "statement_kinds": [
          "INSERT"
        ],
        "comment_placeholders": 0,
        "connection": {
          "age_s": 0.002,
          "statement_number": 2,
          "pool_recycle": -1,
          "pool_pre_ping": false,
          "observed_connects_hour": 1,
          "observed_fresh_failures_hour": 0,
          "pid": 77857
        },
        "in_transaction": true
      }
    },
    {
      "id": "69654ba7905247f99c2871e0dd879094",
      "parent_id": null,
      "kind": "custom",
      "name": "summary",
      "start_ms": 2.511,
      "duration_ms": 0.001,
      "callsite": null,
      "data": {
        "job_stage": true,
        "error": "ValueError"
      }
    }
  ],
  "issues": [
    {
      "id": "3a773d7eacaaf18e",
      "kind": "application_error",
      "severity": "critical",
      "title": "Application recording failed",
      "detail": "An exception escaped this request or job. Inspect its traceback and observed transaction outcomes. Grouping uses exception type and source location, not parameter values.",
      "span_ids": [],
      "fingerprint": null,
      "callsite": null,
      "count": 1,
      "total_ms": 0.0,
      "suggestion": null,
      "extra": {
        "error_type": "ValueError",
        "origin": {
          "file": "scripts/build_release_fixtures.py",
          "line": 41,
          "function": "<module>"
        },
        "commit_observed_before_failure": true,
        "rule_kind": "application_error",
        "trace_spans_complete": true
      }
    }
  ],
  "tags": {
    "job.name": "example-import",
    "job.id": "example-task",
    "job.attempt": "0",
    "job.outcome": "failed",
    "commit_observed_before_failure": "true",
    "sampled_by": "all",
    "synthetic_demo": "true",
    "demo_case": "job_failure"
  },
  "error": {
    "type": "ValueError",
    "message": "Example summary failed after the write committed",
    "traceback": "Traceback (most recent call last):\n  File \"/app/.venv/lib/python3.13/site-packages/redline_core/recorder.py\", line 575, in recording\n    yield builder\n  File \"/app/.venv/lib/python3.13/site-packages/redline_core/jobs.py\", line 46, in job\n    yield builder\n  File \"/app/scripts/build_release_fixtures.py\", line 41, in <module>\n    raise ValueError(\"Example summary failed after the write committed\")\nValueError: Example summary failed after the write committed\n",
    "origin": {
      "file": "scripts/build_release_fixtures.py",
      "line": 41,
      "function": "<module>"
    }
  },
  "truncated": null,
  "logs": [],
  "logs_dropped": 0,
  "release": "dc8bc50",
  "runtime": {
    "python": "3.13.15",
    "framework": "other"
  },
  "profile": null,
  "has_profile": false
}
