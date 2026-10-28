"""Cached target-process metadata. No database queries or environment secrets."""

from __future__ import annotations

import platform
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version


@lru_cache(maxsize=8)
def target_runtime(framework: str) -> dict[str, str]:
    result = {"python": platform.python_version(), "framework": framework}
    package = {
        "django": "Django",
        "fastapi": "fastapi",
        "flask": "Flask",
        "litestar": "litestar",
    }.get(framework)
    if package:
        try:
            result["framework_version"] = version(package)
        except PackageNotFoundError:
            pass
    return result
