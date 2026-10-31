"""Run every distribution's test suite from the repository root.

Each package keeps its own pytest configuration (pytest-django needs a settings module,
the others must not see it), so the suites cannot share one pytest session. This runs
them one after another and prints one summary:

    uv run python scripts/run_tests.py                 # all five packages
    uv run python scripts/run_tests.py core flask      # a subset (short names)
    uv run python scripts/run_tests.py -- -x -k n_plus # extra pytest arguments after --
    uv run python scripts/run_tests.py --cov           # with coverage per package

Exit status is non-zero if any suite fails.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {
    "core": ("redline-core", "redline_core"),
    "django": ("django-redline", "django_redline"),
    "fastapi": ("fastapi-redline", "fastapi_redline"),
    "flask": ("flask-redline", "flask_redline"),
    "litestar": ("litestar-redline", "litestar_redline"),
}


def command(short: str, *, cov: bool, extra: list[str]) -> list[str]:
    """The pytest command line for one package (run from its directory)."""
    _, module = PACKAGES[short]
    cmd = [sys.executable, "-m", "pytest", "-q"]
    if cov:
        cmd += [f"--cov={module}", "--cov-report=term:skip-covered"]
    return cmd + extra


def main(argv: list[str] | None = None) -> int:
    args_in = list(sys.argv[1:] if argv is None else argv)
    extra: list[str] = []
    if "--" in args_in:
        cut = args_in.index("--")
        args_in, extra = args_in[:cut], args_in[cut + 1 :]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("packages", nargs="*", metavar="PACKAGE", help=", ".join(PACKAGES))
    parser.add_argument("--cov", action="store_true", help="measure coverage per package")
    args = parser.parse_args(args_in)
    unknown = [p for p in args.packages if p not in PACKAGES]
    if unknown:
        parser.error(f"unknown package(s) {', '.join(unknown)}; choose from {', '.join(PACKAGES)}")

    results: list[tuple[str, int, float]] = []
    for short in args.packages or list(PACKAGES):
        dist, _ = PACKAGES[short]
        print(f"\n=== {dist}", flush=True)
        start = time.monotonic()
        code = subprocess.call(
            command(short, cov=args.cov, extra=extra), cwd=ROOT / "packages" / dist
        )
        results.append((dist, code, time.monotonic() - start))

    print("\n=== summary")
    for dist, code, seconds in results:
        print(f"{'ok  ' if code == 0 else 'FAIL'} {dist} ({seconds:.0f}s)")
    return 0 if all(code == 0 for _, code, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
