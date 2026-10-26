"""Smoke-test installed wheels: run with the interpreter of a venv that has the three
distributions installed from wheels (not editable). Used by CI's package job.

Checks that each module imports from site-packages, that the bundled UI was included
in the redline-core wheel, and that each package ships its py.typed marker.
"""

from __future__ import annotations

import importlib
import importlib.resources as res
import sys


def main() -> int:
    failures: list[str] = []
    for module in ("redline_core", "django_redline", "fastapi_redline"):
        mod = importlib.import_module(module)
        where = mod.__file__ or ""
        if "site-packages" not in where:
            failures.append(f"{module} imported from {where}, not site-packages")
        print(f"{module} {getattr(mod, '__version__', '?')} from {where}")

    core = res.files("redline_core")
    if not core.joinpath("ui/static/index.html").is_file():
        failures.append("redline_core wheel is missing ui/static/index.html (bundled UI)")

    for module in ("redline_core", "django_redline", "fastapi_redline"):
        if not res.files(module).joinpath("py.typed").is_file():
            failures.append(f"{module} wheel is missing py.typed")

    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
