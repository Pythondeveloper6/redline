"""Reproduce check for "server-side metrics: unavailable" after `redline check record`.

Copies the FastAPI demo to a temporary directory, serves it with uvicorn, then runs
`redline check record` and `redline check compare` against it ``N`` times (default 30). Every
run must report server-side metrics; any run that does not is printed with its output.
Run from the repository root after ``uv sync --all-packages``::

    uv run python scripts/check_metrics_loop.py [N]
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "fastapi"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main(argv: list[str]) -> int:
    runs = int(argv[0]) if argv else 30
    tmp = Path(tempfile.mkdtemp(prefix="redline-metrics-loop-"))
    work = tmp / "fastapi"
    shutil.copytree(EXAMPLE, work, ignore=shutil.ignore_patterns(".redline", "*.db", "__pycache__"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    store = work / ".redline" / "redline.db"
    suite = work / "check.toml"
    subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(ROOT),
            "python",
            "-c",
            "import seed; seed.main(audit_rows=200)",
        ],
        cwd=work,
        check=True,
        capture_output=True,
    )
    server = subprocess.Popen(
        ["uv", "run", "--project", str(ROOT), "uvicorn", "shop.main:app", "--port", str(port)],
        cwd=work,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(base + "/orders/1", timeout=1).read()
                break
            except OSError:
                time.sleep(0.2)
        else:
            print("app did not start")
            return 2
        bad = 0
        baseline = tmp / "baseline.json"
        for i in range(1, runs + 1):
            cli = ["uv", "run", "--package", "redline-core", "--extra", "load", "redline", "check"]
            common = [str(suite), "--base-url", base, "--store", str(store)]
            rec = subprocess.run(
                [*cli, "record", *common, "--out", str(baseline)],
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            cmp_ = subprocess.run(
                [*cli, "compare", *common, "--baseline", str(baseline)],
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            for name, proc, ok in (("record", rec, (0,)), ("compare", cmp_, (0, 1))):
                text = proc.stdout + proc.stderr
                if proc.returncode not in ok:
                    bad += 1
                    print(f"run {i} {name}: exit {proc.returncode}\n{text}")
                    continue
                if "server-side metrics: unavailable" in text or "metrics unavailable" in text:
                    bad += 1
                    print(f"run {i} {name}: metrics unavailable\n{text}")
        print(f"{runs} run(s): {bad} without server-side metrics")
        return 1 if bad else 0
    finally:
        server.terminate()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
