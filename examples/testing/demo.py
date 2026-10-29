"""Seed and serve one disposable example using the common Compose image."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PORTS = {"fastapi": 8011, "flask": 8012, "litestar": 8013, "django": 8014}


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in PORTS:
        raise SystemExit("Choose fastapi, flask, litestar or django")
    framework = sys.argv[1]
    project = ROOT / "examples" / framework
    if framework == "django":
        for arguments in (("migrate", "--noinput"), ("seed",)):
            subprocess.run([sys.executable, "manage.py", *arguments], cwd=project, check=True)
        command = [sys.executable, "manage.py", "runserver", "0.0.0.0:8014", "--noreload"]
    else:
        subprocess.run([sys.executable, "seed.py"], cwd=project, check=True)
        if framework == "flask":
            command = [
                sys.executable,
                "-m",
                "flask",
                "--app",
                "shop.main",
                "run",
                "--host",
                "0.0.0.0",
                "--port",
                str(PORTS[framework]),
            ]
        else:
            command = [
                sys.executable,
                "-m",
                "uvicorn",
                "shop.main:app",
                "--host",
                "0.0.0.0",
                "--port",
                str(PORTS[framework]),
            ]
    os.chdir(project)
    os.execv(sys.executable, command)


if __name__ == "__main__":
    main()
