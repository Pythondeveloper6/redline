"""Smoke test: every example project seeds, boots and serves requests plus the Redline UI.

Each example is copied to a temporary directory and exercised in its own interpreter
(they all use a package called ``shop``). Run from the repository root after
``uv sync --all-packages``::

    uv run python scripts/smoke_examples.py [django fastapi flask litestar]
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"

SEED = "import seed; seed.main(audit_rows=500)"

PROBES = {
    "fastapi": (
        "shop.main",
        "from fastapi.testclient import TestClient\nc = TestClient(app, base_url='http://localhost')",
    ),
    "litestar": (
        "shop.main",
        "from litestar.testing import TestClient\nc = TestClient(app, base_url='http://localhost')",
    ),
    "flask": ("shop.main", "c = app.test_client()"),
}

PATHS = {
    "fastapi": ["/orders", "/orders/customers", "/orders/1", "/report/duplicates", "/_redline/"],
    "litestar": ["/orders", "/orders/1", "/_redline/"],
    "flask": ["/orders", "/_redline/"],
}

DJANGO_CHILD = """
import os
os.environ["DJANGO_SETTINGS_MODULE"] = "shopsite.settings"
import django
django.setup()
from django.core.management import call_command
call_command("migrate", verbosity=0, interactive=False)
call_command("seed")
from django.test import Client
c = Client(HTTP_HOST="localhost", REMOTE_ADDR="127.0.0.1")
for p in ["/", "/orders/", "/redline/"]:
    r = c.get(p, follow=True)
    assert r.status_code == 200, (p, r.status_code)
print("ok")
"""


def child_code(name: str) -> str:
    if name == "django":
        return DJANGO_CHILD
    module, setup = PROBES[name]
    return "\n".join(
        [
            SEED,
            f"from {module} import app",
            setup,
            f"for p in {PATHS[name]!r}:",
            "    r = c.get(p)",
            "    assert r.status_code == 200, (p, r.status_code)",
            "print('ok')",
        ]
    )


def run(name: str) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / name
        shutil.copytree(
            EXAMPLES / name, work, ignore=shutil.ignore_patterns("__pycache__", "*.db", "*.sqlite3")
        )
        env = {**os.environ, "SHOP_DB": str(work / "shop.db"), "PYTHONPATH": str(work)}
        proc = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(child_code(name))],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
    ok = proc.returncode == 0 and "ok" in proc.stdout
    print(f"{'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        print(proc.stdout[-1500:], proc.stderr[-3000:])
    return ok


def main() -> int:
    names = sys.argv[1:] or ["django", "fastapi", "flask", "litestar"]
    results = [run(n) for n in names]
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
