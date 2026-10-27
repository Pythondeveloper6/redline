from __future__ import annotations

import os
import re
from collections.abc import Iterator
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.urls import URLPattern, URLResolver, get_resolver
from django.urls.resolvers import RegexPattern, RoutePattern

from django_redline.middleware import _ninja_operation, mount_prefix
from django_redline.runtime import get_runtime

#: URL namespaces never put in a scenario (login-protected admin, Redline itself).
SKIP_NAMESPACES = frozenset({"admin", "redline"})
_REGEX_META = re.compile(r"[\\.*+?()\[\]{}|]")


def _join(route1: str, route2: str) -> str:
    # Same rule as Django's URLResolver._join_route (what ResolverMatch.route is built with).
    if route2.startswith("^"):
        route2 = route2[1:]
    return route1 + route2


def _template(route: str) -> str:
    # Same normalisation as the middleware applies to ResolverMatch.route.
    return "/" + route.lstrip("^/") if not route.startswith("/") else route


def _concrete(parts: list[Any]) -> str | None:
    """The literal URL when no pattern level has a parameter, else ``None``."""
    out = ""
    for pattern in parts:
        if isinstance(pattern, RoutePattern):
            text = str(pattern)
            if "<" in text:
                return None
        elif isinstance(pattern, RegexPattern):
            text = str(pattern).lstrip("^")
            text = text[:-1] if text.endswith("$") else text
            if _REGEX_META.search(text.replace("\\/", "/").replace("\\-", "-")):
                return None
            text = text.replace("\\/", "/").replace("\\-", "-")
        else:  # pragma: no cover - custom pattern classes
            return None
        out += text
    return "/" + out.lstrip("/")


def _methods(callback: Any) -> set[str] | None:
    """HTTP methods a view answers, or ``None`` when that cannot be told (assume GET)."""

    if _ninja_operation(callback, "GET") is not None:
        return {"GET"}
    if getattr(callback, "__module__", None) == "ninja.operation":
        return set()  # a Ninja path without a GET operation
    actions = getattr(callback, "actions", None)  # DRF ViewSet routes
    if isinstance(actions, dict):
        return {m.upper() for m in actions}
    cls = getattr(callback, "view_class", None) or getattr(callback, "cls", None)
    if cls is not None:
        names = getattr(cls, "http_method_names", ())
        return {m.upper() for m in names if hasattr(cls, m)}
    return None


def iter_routes(
    patterns: Any = None, prefix: str = "", parts: list[Any] | None = None
) -> Iterator[tuple[str, str, str | None]]:
    """``(method, template, path_or_None)`` for every GET-able URL of the URLconf."""
    if patterns is None:
        patterns = get_resolver().url_patterns
    parts = parts or []
    for entry in patterns:
        if isinstance(entry, URLResolver):
            if entry.namespace in SKIP_NAMESPACES:
                continue
            yield from iter_routes(
                entry.url_patterns, _join(prefix, str(entry.pattern)), [*parts, entry.pattern]
            )
        elif isinstance(entry, URLPattern):
            methods = _methods(entry.callback)
            if methods is not None and "GET" not in methods:
                continue
            route = _join(prefix, str(entry.pattern))
            yield "GET", _template(route), _concrete([*parts, entry.pattern])


class Command(BaseCommand):
    help = (
        "Write a load-test scenario from the URLconf: GET routes without parameters, plus "
        "parameterised ones filled with a concrete path from recent traces."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("-o", "--output", help="Write here (.json or .toml); default stdout.")
        parser.add_argument(
            "--base-url", default=None, help="Target URL (default http://127.0.0.1:8000)."
        )
        parser.add_argument("--force", action="store_true", help="Overwrite --output.")

    def handle(self, *args: Any, **options: Any) -> None:

        from redline_core.load import generate
        from redline_core.load.scenario import parse_scenario

        store = get_runtime().store
        try:
            store.flush(5.0)
        except Exception:  # pragma: no cover - a custom store
            pass
        mount = mount_prefix() or "/_redline"
        try:
            data = generate.from_routes(
                iter_routes(),
                store=store,
                base_url=options["base_url"],
                name="django routes",
                mount_prefix=mount,
            )
            parse_scenario(data)
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        out = options["output"]
        text = generate.dump(data, out)
        if not out or out == "-":
            self.stdout.write(text, ending="")
            return
        if os.path.exists(out) and not options["force"]:
            raise CommandError(f"{out} exists (use --force to overwrite)")
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.stdout.write(self.style.SUCCESS(f"Wrote {out}: {len(data['requests'])} request(s)."))
