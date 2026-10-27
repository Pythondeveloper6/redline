"""Static N+1 scan for Django: template loops (via :mod:`redline_core.scan`) filtered through
the app registry, plus a check of DRF serializers against their views' querysets.

DRF is optional and never imported at module level: serializers and views are recognised by
the *names* of their base classes, like :mod:`django_redline.suggest` does.
"""

from __future__ import annotations

import inspect
import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from django.apps import apps
from django.conf import settings
from django.db.models import ForeignObjectRel

from redline_core.scan import RelationInfo, ScanFinding, scan_templates

__all__ = [
    "RegistryResolver",
    "drf_findings",
    "scan_project",
    "template_dirs",
]

_SITE = re.compile(r"[/\\](site|dist)-packages[/\\]")


def _relation_info(field: Any) -> tuple[str, RelationInfo] | None:
    """``(accessor name, info)`` for a relation field of a model, or ``None``."""
    if not getattr(field, "is_relation", False):
        return None
    target = getattr(field, "related_model", None)
    if target is None or isinstance(target, str):  # GenericForeignKey / unresolved
        return None
    name = field.get_accessor_name() if isinstance(field, ForeignObjectRel) else field.name
    if not name:  # related_name="+" hides the accessor
        return None
    if field.one_to_one:
        kind, method = "o2o", "select_related"
    elif field.many_to_one:
        kind, method = "fk", "select_related"
    elif field.many_to_many:
        kind, method = "m2m", "prefetch_related"
    else:  # one_to_many: a reverse FK or a GenericRelation
        kind, method = "reverse_fk", "prefetch_related"
    return name, RelationInfo(kind, target._meta.label, method)


class RegistryResolver:
    """Every relation name on every installed model; a first hop matching any of them is kept."""

    def __init__(self) -> None:
        self.relations: dict[str, RelationInfo] = {}
        for model in apps.get_models():
            for field in model._meta.get_fields():
                got = _relation_info(field)
                if got is None:
                    continue
                # The same name on several models: prefetch_related works for every kind, so
                # it wins over select_related when they disagree.
                known = self.relations.get(got[0])
                if known is None or (
                    known.method != got[1].method and got[1].method == "prefetch_related"
                ):
                    self.relations[got[0]] = got[1]

    def is_relation(self, first_hop: str) -> RelationInfo | None:
        return self.relations.get(first_hop)


def template_dirs() -> list[tuple[str, str]]:
    """``[(dialect, directory)]`` for every template dir: TEMPLATES ``DIRS`` plus app dirs.

    Directories inside ``site-packages`` (the admin, third-party apps) are left out: the scan
    is about code you can fix.
    """
    out: list[tuple[str, str]] = []
    for conf in getattr(settings, "TEMPLATES", []):
        backend = conf.get("BACKEND", "")
        if backend.endswith("DjangoTemplates"):
            dialect, sub = "django", "templates"
        elif backend.endswith("Jinja2"):
            dialect, sub = "jinja", "jinja2"
        else:
            continue
        dirs = [os.fspath(d) for d in conf.get("DIRS", [])]
        if conf.get("APP_DIRS"):
            dirs += [os.path.join(cfg.path, sub) for cfg in apps.get_app_configs()]
        for d in dirs:
            if os.path.isdir(d) and not _SITE.search(d + os.sep) and (dialect, d) not in out:
                out.append((dialect, d))
    return out


def scan_project(
    paths: Iterable[str] | None = None,
    *,
    dialect: str = "django",
    drf: bool = True,
    warnings: list[str] | None = None,
) -> list[ScanFinding]:
    """Template scan (given ``paths``, or every template dir) plus the DRF serializer check.

    The DRF check only runs on the default whole-project scan.
    """
    sink = warnings if warnings is not None else []
    resolver = RegistryResolver()
    findings: list[ScanFinding] = []
    if paths:
        findings += scan_templates(list(paths), dialect, resolver=resolver, warnings=sink)
    else:
        for dia, d in template_dirs():
            findings += scan_templates([d], dia, resolver=resolver, warnings=sink)
        if drf:
            findings += drf_findings(warnings=sink)
    findings.sort(key=lambda f: (f.file, f.line, f.chain))
    return findings


# ---- DRF -----------------------------------------------------------------------------


def _mro_names(obj: Any) -> set[str]:
    cls = obj if inspect.isclass(obj) else type(obj)
    return {c.__name__ for c in cls.__mro__}


def _is_serializer(obj: Any) -> bool:
    return bool(_mro_names(obj) & {"BaseSerializer", "Serializer", "ListSerializer"})


def _all_subclasses(cls: type) -> Iterator[type]:
    seen: set[type] = set()
    todo = [cls]
    while todo:
        for sub in todo.pop().__subclasses__():
            if sub not in seen:
                seen.add(sub)
                todo.append(sub)
                yield sub


def _discover_views() -> list[type]:
    try:  # make sure the URLconf (and so the views) is imported
        from importlib import import_module

        import_module(settings.ROOT_URLCONF)
    except Exception:
        pass
    try:
        from rest_framework.generics import GenericAPIView
    except ImportError:
        return []
    out = []
    for view in _all_subclasses(GenericAPIView):
        path = _source_file(view) or ""
        if not _SITE.search(path):
            out.append(view)
    return out


def _source_file(obj: Any) -> str | None:
    try:
        return inspect.getsourcefile(obj) or inspect.getfile(obj)
    except (TypeError, OSError):
        return None


def _class_line(cls: type, attr: str | None = None) -> int:
    """Line of ``attr = ...`` inside the class body, falling back to the ``class`` line."""
    try:
        lines, start = inspect.getsourcelines(cls)
    except (TypeError, OSError):
        return 0
    if attr:
        pat = re.compile(rf"^\s+{re.escape(attr)}\s*[:=]")
        for i, line in enumerate(lines):
            if pat.match(line):
                return start + i
    return start


@dataclass(frozen=True)
class _Need:
    path: str  # "customer", "items__product"
    method: str  # select_related | prefetch_related
    field: str  # serializer field (or source) that triggers it
    serializer: type
    info: RelationInfo


def _model_relations(model: Any) -> dict[str, tuple[Any, RelationInfo]]:
    out: dict[str, tuple[Any, RelationInfo]] = {}
    for f in model._meta.get_fields():
        got = _relation_info(f)
        if got is not None:
            out[got[0]] = (f, got[1])
    return out


def _serializer_needs(
    ser: Any, model: Any, prefix: str = "", via_prefetch: bool = False, depth: int = 0
) -> list[_Need]:
    """Relations a serializer reads per object, as lookups on the view's queryset."""
    cls = ser if inspect.isclass(ser) else type(ser)
    if model is None or depth > 3:
        return []
    rels = _model_relations(model)
    needs: list[_Need] = []

    def add(name: str, field_name: str, nested: Any) -> None:
        if name not in rels:
            return
        _, info = rels[name]
        path = f"{prefix}__{name}" if prefix else name
        method = "prefetch_related" if via_prefetch else info.method
        needs.append(_Need(path, method, field_name, cls, info))
        if nested is not None:
            target = rels[name][0].related_model
            needs.extend(
                _serializer_needs(
                    nested, target, path, via_prefetch or method == "prefetch_related", depth + 1
                )
            )

    declared = dict(getattr(cls, "_declared_fields", None) or {})
    for fname, field in declared.items():
        source = getattr(field, "source", None)
        if source in (None, "*", ""):
            source = fname
        head = str(source).split(".")[0]
        if _is_serializer(field):
            child = getattr(field, "child", None) or field  # ListSerializer wraps its child
            add(head, fname, child)
        elif "RelatedField" in _mro_names(field):
            # PK-only related fields skip the query for forward keys (DRF optimisation),
            # but a to-many field iterates the manager.
            plain_pk = type(field).__name__ == "PrimaryKeyRelatedField"
            many_field = type(field).__name__ == "ManyRelatedField" or getattr(field, "many", False)
            if many_field or not plain_pk:
                add(head, fname, None)
        elif "." in str(source):
            add(head, fname, None)  # CharField(source="customer.name")
    meta = getattr(cls, "Meta", None)
    names = getattr(meta, "fields", None)
    if names == "__all__":
        names = [f.name for f in model._meta.get_fields() if getattr(f, "many_to_many", False)]
        names = [n for n in names if n in rels]
    for n in names if isinstance(names, (list, tuple)) else ():
        if n not in declared and n in rels and rels[n][1].method == "prefetch_related":
            add(n, n, None)  # auto-built ManyRelatedField iterates the manager
    return needs


def _covers(queryset: Any, need: _Need) -> bool:
    """Does the queryset already select/prefetch ``need.path`` (or something below it)?"""
    node: Any = getattr(queryset.query, "select_related", False)
    if node is True and need.method == "select_related" and "__" not in need.path:
        return True
    if isinstance(node, dict):
        cur: Any = node
        for part in need.path.split("__"):
            if not isinstance(cur, dict) or part not in cur:
                break
            cur = cur[part]
        else:
            return True
    for lookup in getattr(queryset, "_prefetch_related_lookups", ()) or ():
        to = getattr(lookup, "prefetch_to", lookup)
        if isinstance(to, str) and (to == need.path or to.startswith(need.path + "__")):
            return True
    return False


def _is_queryset(obj: Any) -> bool:
    return hasattr(obj, "query") and hasattr(obj, "_prefetch_related_lookups")


def drf_findings(
    views: Iterable[type] | None = None, *, warnings: list[str] | None = None
) -> list[ScanFinding]:
    """Serializer fields that cost a query per object because the view's queryset lacks the
    matching ``select_related`` / ``prefetch_related``.

    ``views`` defaults to every ``GenericAPIView`` subclass outside site-packages (empty when
    DRF is not installed). Only views with a class-level ``queryset`` can be inspected.
    """
    sink = warnings if warnings is not None else []
    found: list[ScanFinding] = []
    for view in _discover_views() if views is None else views:
        try:
            found.extend(_check_view(view))
        except Exception as exc:  # never let one odd view stop the scan
            sink.append(f"{getattr(view, '__qualname__', view)}: skipped ({exc})")
    return found


def _check_view(view: type) -> list[ScanFinding]:
    ser = getattr(view, "serializer_class", None)
    queryset = getattr(view, "queryset", None)
    if ser is None or not _is_queryset(queryset):
        return []
    model = getattr(queryset, "model", None)
    out: list[ScanFinding] = []
    view_file = _source_file(view) or ""
    view_line = _class_line(view, "queryset")
    for need in _serializer_needs(ser, model):
        if _covers(queryset, need):
            continue
        ser_cls = need.serializer
        chain = f"{ser_cls.__name__}.{need.field}"
        out.append(
            ScanFinding(
                file=_source_file(ser_cls) or view_file,
                line=_class_line(ser_cls, need.field),
                loop_var="",
                chain=chain,
                kind="serializer",
                severity="high" if need.method == "prefetch_related" else "medium",
                message=(
                    f"`{chain}` reads `{need.path.replace('__', '.')}` for every object, but "
                    f"{view.__name__}.queryset ({view_file}:{view_line}) does not "
                    f"{need.method.replace('_', ' ')} it"
                ),
                suggestion=f'{view.__name__}.queryset = ....{need.method}("{need.path}")',
                first_hop=need.path.split("__")[0],
                relation=need.info.kind,
                model=need.info.model,
            )
        )
    return out
