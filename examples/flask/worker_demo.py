"""Record a generic worker job into a separate explicitly chosen SQLite trace store."""

from __future__ import annotations

import argparse
import json

from shop.workflows import record_example_job

from redline_core import Recorder, Settings
from redline_core.storage import SQLiteStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True, help="trace database path outside the checkout")
    parser.add_argument(
        "--succeed", action="store_true", help="finish the summary without an error"
    )
    args = parser.parse_args()
    settings = Settings(
        storage_path=args.store,
        storage_role="worker",
        instrument_engines="all",
        capture_logs=False,
        capture_locals=False,
    )
    store = SQLiteStore.from_settings(settings)
    try:
        trace = record_example_job(Recorder(settings, store), fail_summary=not args.succeed)
        store.flush()
        print(json.dumps({"trace_id": trace.id, "outcome": trace.tags["job.outcome"]}))
    finally:
        store.close()


if __name__ == "__main__":
    main()
