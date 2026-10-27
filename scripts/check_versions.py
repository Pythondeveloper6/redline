"""Fail unless every distribution shares one version and the adapters pin it.

The packages are released in lockstep: an adapter is only ever tested against the exact
core release beside it, so it pins ``redline-core==<same version>``. This check
also compares each package's ``__version__`` with its ``pyproject.toml``.

    uv run python scripts/check_versions.py
    uv run python scripts/check_versions.py --expect 0.2.0a1   # the release workflow's tag check
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {
    "redline-core": "redline_core",
    "django-redline": "django_redline",
    "fastapi-redline": "fastapi_redline",
    "flask-redline": "flask_redline",
    "litestar-redline": "litestar_redline",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--expect",
        metavar="VERSION",
        help="also require every package to be exactly this version (the tag, without 'v')",
    )
    args = parser.parse_args()
    errors: list[str] = []
    versions: dict[str, str] = {}
    for dist, module in PACKAGES.items():
        pyproject = (ROOT / "packages" / dist / "pyproject.toml").read_text()
        version = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)
        versions[dist] = version
        init = (ROOT / "packages" / dist / "src" / module / "__init__.py").read_text()
        dunder = re.search(r'^__version__ = "([^"]+)"', init, re.M)
        if not dunder or dunder.group(1) != version:
            errors.append(f"{module}.__version__ does not match {dist} {version}")
        pin = re.search(r'"redline-core==([^"]+)"', pyproject)
        if dist != "redline-core":
            if not pin:
                errors.append(f"{dist} must pin redline-core==<version>")
            elif pin.group(1) != versions["redline-core"]:
                core = versions["redline-core"]
                errors.append(f"{dist} pins core {pin.group(1)}, core is {core}")
    if len(set(versions.values())) != 1:
        errors.append(f"versions differ: {versions}")
    if args.expect is not None:
        wrong = {d: v for d, v in versions.items() if v != args.expect}
        if wrong:
            errors.append(f"expected version {args.expect}, found {wrong}")
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    if not errors:
        print(f"ok: all at {next(iter(versions.values()))}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
