"""Static N+1 scan: find ``{% for x in y %}`` loops in Django / Jinja templates whose body
reads a relation off the loop variable (``x.customer.name``, ``x.items.all``,
``x.items.count``), each of which costs one query per iteration unless the view fetched the
relation up front.

Dependency free and best-effort. The tokenizer understands comments (``{# #}`` and
``{% comment %}``), ``{% verbatim %}`` / ``{% raw %}``, nested loops, ``{% with %}`` /
``{% set %}`` aliases, filters (``x.a.b|upper``) and Jinja subscripts (``x['a'].b``). A
template it cannot make sense of is skipped with a warning, never an exception.

Adapters pass a :class:`RelationResolver` to keep only chains whose first hop is a real
model relation and to name the right fix (``select_related`` / ``prefetch_related``).
"""

from __future__ import annotations

import bisect
import logging
import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "RelationInfo",
    "RelationResolver",
    "ScanFinding",
    "format_table",
    "jinja_search_paths",
    "scan_jinja_loader",
    "scan_templates",
]

log = logging.getLogger("redline.scan")

TEMPLATE_SUFFIXES = frozenset(
    {".html", ".htm", ".xhtml", ".txt", ".xml", ".svg", ".md", ".tpl", ".djhtml", ".jinja"}
    | {".jinja2", ".j2", ".jinja-html"}
)
_SKIP_DIRS = frozenset({"node_modules", "__pycache__", "site-packages", "venv", ".venv"})
_MAX_BYTES = 5_000_000  # a "template" bigger than this is data, not markup

# Last hops that run a queryset method / evaluate a queryset: always a query per iteration.
_QUERYSET_HOPS = frozenset(
    {
        "all", "count", "first", "last", "exists", "filter", "exclude", "order_by", "get",
        "values", "values_list", "distinct", "aggregate", "annotate", "iterator", "latest",
        "earliest", "in_bulk", "select_related", "prefetch_related", "reverse", "none",
    }
)  # fmt: skip
# Hops that make the iterable "the rows of a relation": stripped when a nested loop's
# origin path is worked out (``o.items.all`` iterates the ``items`` relation).
_ROW_HOPS = frozenset(
    {"all", "filter", "exclude", "order_by", "distinct", "iterator", "reverse", "select_related"}
    | {"prefetch_related", "values", "values_list", "annotate"}
)
# Plain value attributes that are almost never a relation.
_VALUE_HOPS = frozenset(
    {"year", "month", "day", "hour", "minute", "second", "url", "size", "path"}
    | {"pk", "id", "isoformat", "strftime", "upper", "lower", "title", "strip", "split"}
)

_DJANGO_OPENERS = frozenset(
    {"for", "if", "with", "block", "ifchanged", "spaceless", "autoescape", "filter", "ifequal"}
    | {"ifnotequal", "blocktrans", "blocktranslate", "cache", "localize", "language", "timezone"}
)
_JINJA_OPENERS = frozenset(
    {"for", "if", "with", "block", "macro", "call", "filter", "autoescape", "trans", "set"}
    | {"raw"}
)

_TOKEN = {
    "django": re.compile(r"\{#[^\n]*?#\}|\{%.*?%\}|\{\{.*?\}\}", re.S),
    "jinja": re.compile(r"\{#.*?#\}|\{%.*?%\}|\{\{.*?\}\}", re.S),
}
_TAG_NAME = re.compile(r"\s*([A-Za-z_]\w*)\s*(.*)", re.S)
_STR = r"""(?:"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')"""
# A string literal (consumed so identifiers inside it are ignored) or an attribute chain:
# ``a.b.c``, ``a['b'].c`` (Jinja), ``a.0.b`` (Django list index).
_CHAIN_OR_STR = re.compile(
    rf"(?P<str>{_STR})"
    r"|(?P<chain>(?<![\w.])[A-Za-z_]\w*(?:\.\w+|\[\s*(?:'\w+'|\"\w+\")\s*\])*)"
)
_HOP = re.compile(r"\.(\w+)|\[\s*['\"](\w+)['\"]\s*\]")
_FOR = re.compile(r"(.*?)\s+in\s+(.*)", re.S)
_ASSIGN = re.compile(rf"(?<![\w.])([A-Za-z_]\w*)\s*=\s*((?:{_STR}|[^\s,])+)")
_WITH_AS = re.compile(r"(.+?)\s+as\s+([A-Za-z_]\w*)\s*$", re.S)
_IDENT = re.compile(r"[A-Za-z_]\w*")


@dataclass(frozen=True)
class RelationInfo:
    """What a resolver knows about a relation name."""

    kind: str  # "fk" | "o2o" | "m2m" | "reverse_fk"
    model: str  # model the relation points to, e.g. "shop.Customer"
    method: str  # "select_related" | "prefetch_related"


@runtime_checkable
class RelationResolver(Protocol):
    """Adapter hook: is the first hop after a loop variable a real ORM relation?"""

    def is_relation(self, first_hop: str) -> RelationInfo | None: ...


@dataclass
class ScanFinding:
    file: str
    line: int  # line of the access
    loop_var: str
    chain: str  # e.g. "order.customer.name"
    kind: str = "template"  # "template" | "serializer"
    severity: str = "medium"  # "high" | "medium" | "low"
    message: str = ""
    suggestion: str = ""
    for_line: int = 0  # line of the enclosing {% for %} (0 when not applicable)
    first_hop: str = ""  # the relation name the access starts with
    relation: str = ""  # resolver's kind: fk | o2o | m2m | reverse_fk ("" when unresolved)
    model: str = ""  # related model per the resolver

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _Loop:
    var: str
    line: int
    origin: tuple[str, ...]  # relation path leading to the iterated rows (nested loops)


@dataclass
class _Bind:
    """A name bound to ``loop var`` plus ``prefix`` hops (``{% with c=o.customer %}``)."""

    loop: _Loop
    prefix: tuple[str, ...] = ()


@dataclass
class _Frame:
    kind: str
    line: int
    names: dict[str, _Bind | None] = field(default_factory=dict)  # None shadows an outer name


@dataclass
class _Hit:
    line: int
    loop: _Loop
    hops: tuple[str, ...]


def scan_templates(
    paths: Iterable[str | os.PathLike[str]] | str | os.PathLike[str],
    dialect: str = "django",
    *,
    resolver: RelationResolver | None = None,
    warnings: list[str] | None = None,
) -> list[ScanFinding]:
    """Scan template files (or directories, recursively) for N+1 loops.

    ``dialect`` is ``"django"`` or ``"jinja"``. With a ``resolver`` only chains whose first hop
    it recognises are kept (and the suggestion names the right method). Problems (unreadable
    file, unknown path) are appended to ``warnings`` and logged; nothing raises for bad input.
    """
    if dialect not in _TOKEN:
        raise ValueError(f"dialect must be 'django' or 'jinja', not {dialect!r}")
    sink = warnings if warnings is not None else []
    if isinstance(paths, (str, os.PathLike)):
        paths = [paths]
    findings: list[ScanFinding] = []
    for path in _iter_files(paths, sink):
        try:
            if path.stat().st_size > _MAX_BYTES:
                raise ValueError("file too large to be a template")
            text = path.read_text(encoding="utf-8", errors="replace")
            findings.extend(scan_source(text, str(path), dialect, resolver=resolver))
        except Exception as exc:  # a bad template must never take the scan down
            sink.append(f"{path}: skipped ({type(exc).__name__}: {exc})")
    for w in sink if warnings is None else ():
        log.warning(w)
    return findings


def scan_source(
    text: str,
    filename: str = "<string>",
    dialect: str = "django",
    *,
    resolver: RelationResolver | None = None,
) -> list[ScanFinding]:
    """Scan one template's source text."""
    seen: set[tuple[int, int, str]] = set()
    out: list[ScanFinding] = []
    for hit in _tokenize_hits(text, dialect):
        key = (hit.line, hit.loop.line, hit.loop.var + "." + ".".join(hit.hops))
        if key in seen:
            continue
        seen.add(key)
        finding = _finding(hit, filename, resolver, dialect)
        if finding is not None:
            out.append(finding)
    out.sort(key=lambda f: (f.line, f.chain))
    return out


def _iter_files(paths: Iterable[str | os.PathLike[str]], warnings: list[str]) -> Iterator[Path]:
    seen: set[Path] = set()
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            for root, dirs, files in os.walk(p):
                dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in _SKIP_DIRS)
                for name in sorted(files):
                    f = Path(root) / name
                    if f.suffix.lower() in TEMPLATE_SUFFIXES and _once(f, seen):
                        yield f
        elif p.is_file():
            if _once(p, seen):
                yield p
        else:
            warnings.append(f"{p}: no such file or directory")


def _once(path: Path, seen: set[Path]) -> bool:
    try:
        key = path.resolve()
    except OSError:
        key = path
    if key in seen:
        return False
    seen.add(key)
    return True


def _tokenize_hits(text: str, dialect: str) -> Iterator[_Hit]:
    openers = _JINJA_OPENERS if dialect == "jinja" else _DJANGO_OPENERS
    starts = [0]
    for m in re.finditer("\n", text):
        starts.append(m.end())

    def line_of(offset: int) -> int:
        return bisect.bisect_right(starts, offset)

    stack: list[_Frame] = [_Frame("root", 1)]

    def lookup(name: str) -> _Bind | None:
        for fr in reversed(stack):
            if name in fr.names:
                return fr.names[name]
        return None

    def hits_in(expr: str, base: int) -> Iterator[_Hit]:
        for m in _CHAIN_OR_STR.finditer(expr):
            if m.group("chain") is None:
                continue
            parts = _chain_parts(m.group("chain"))
            bind = lookup(parts[0])
            if bind is None:
                continue
            hops = bind.prefix + tuple(parts[1:])
            if len(hops) >= 2:
                yield _Hit(line_of(base + m.start()), bind.loop, hops)

    def resolve(expr: str) -> _Bind | None:
        """Binding for ``expr`` if its first chain (before any filter) hangs off a tracked name."""
        m = next((m for m in _CHAIN_OR_STR.finditer(expr) if m.group("chain")), None)
        if m is None or expr[: m.start()].strip():
            return None
        parts = _chain_parts(m.group("chain"))
        bind = lookup(parts[0])
        if bind is None:
            return None
        return _Bind(bind.loop, bind.prefix + tuple(parts[1:]))

    skip_until: str | None = None
    for tok in _TOKEN[dialect].finditer(text):
        raw = tok.group()
        if raw.startswith("{#"):
            continue
        kind = raw[1]
        inner = raw[2:-2]
        lead = len(inner) - len(inner.lstrip("-+ \t\r\n"))
        content = inner.strip("-+ \t\r\n") if lead < len(inner) else ""
        base = tok.start() + 2 + lead
        if kind == "{":
            if skip_until is None:
                yield from hits_in(content, base)
            continue
        m = _TAG_NAME.match(content)
        if m is None:
            continue
        name, rest = m.group(1), m.group(2)
        rest_base = base + m.start(2)
        if skip_until is not None:
            if name == skip_until:
                skip_until = None
            continue
        line = line_of(tok.start())
        if name == "comment" and dialect == "django":
            skip_until = "endcomment"
            continue
        if name == "verbatim" and dialect == "django":
            skip_until = "endverbatim"
            continue
        if name == "raw" and dialect == "jinja":
            skip_until = "endraw"
            continue
        if name.startswith("end") and name[3:] in openers:
            target = name[3:]
            for i in range(len(stack) - 1, 0, -1):
                if stack[i].kind == target:
                    del stack[i:]
                    break
            continue
        if name == "for":
            fm = _FOR.fullmatch(rest.strip())
            if fm is None:
                stack.append(_Frame("for", line))
                continue
            targets, iterable = fm.group(1), fm.group(2)
            it_base = rest_base + (len(rest) - len(rest.lstrip())) + fm.start(2)
            cond = ""
            if dialect == "jinja":
                parts = re.split(r"\s+if\s+", iterable, maxsplit=1)
                iterable = parts[0]
                cond = parts[1] if len(parts) > 1 else ""
            iterable = re.sub(r"\s+(?:reversed|recursive)\s*$", "", iterable)
            yield from hits_in(iterable, it_base)
            origin: tuple[str, ...] = ()
            src = resolve(iterable)
            if src is not None:
                eff = src.prefix
                while eff and eff[-1] in _ROW_HOPS:
                    eff = eff[:-1]
                origin = src.loop.origin + eff
            frame = _Frame("for", line)
            for var in _IDENT.findall(targets):
                frame.names[var] = _Bind(_Loop(var, line, origin))
            stack.append(frame)
            if cond:
                yield from hits_in(cond, it_base + len(iterable) + 4)
            continue
        if name == "with" or (name == "set" and "=" in rest):
            frame = stack[-1] if name == "set" else _Frame("with", line)
            exprs: list[tuple[str, str, int]] = []  # (alias, expression, offset)
            legacy = _WITH_AS.fullmatch(rest.strip()) if name == "with" else None
            if legacy is not None and "=" not in legacy.group(1):
                exprs.append((legacy.group(2), legacy.group(1), rest_base))
            elif name == "set":
                lhs, _, rhs = rest.partition("=")
                for var in _IDENT.findall(lhs):
                    exprs.append((var, rhs.strip(), rest_base + len(lhs) + 1))
            else:
                for am in _ASSIGN.finditer(rest):
                    exprs.append((am.group(1), am.group(2), rest_base + am.start(2)))
            for alias, expr, off in exprs:
                yield from hits_in(expr, off)
                frame.names[alias] = resolve(expr)
            if name == "with":
                stack.append(frame)
            continue
        if name == "macro":
            frame = _Frame("macro", line)
            for arg in _IDENT.findall(rest.partition("(")[2]):
                frame.names[arg] = None
            stack.append(frame)
            continue
        if name in openers and not (name == "trans" and "endtrans" not in text):
            if name != "set":
                # ``{% block x %}`` names are not expressions; everything else may be
                if name != "block":
                    yield from hits_in(rest, rest_base)
                stack.append(_Frame(name, line))
                continue
            stack.append(_Frame("set", line))
            continue
        if name in ("load", "extends", "elif", "elseif", "else", "empty"):
            if name in ("elif", "elseif"):
                yield from hits_in(rest, rest_base)
            continue
        yield from hits_in(rest, rest_base)
    # Unclosed loops are tolerated: templates being edited, or Django includes/extends halves.


def _chain_parts(chain: str) -> list[str]:
    head = _IDENT.match(chain)
    assert head is not None
    parts = [head.group()]
    for m in _HOP.finditer(chain[head.end() :]):
        parts.append(m.group(1) or m.group(2))
    return parts


def _severity(hops: tuple[str, ...]) -> str:
    last = hops[-1]
    if last in _QUERYSET_HOPS:
        return "high"
    if last in _VALUE_HOPS and len(hops) == 2:
        return "low"
    return "medium"


def _finding(
    hit: _Hit, filename: str, resolver: RelationResolver | None, dialect: str = "django"
) -> ScanFinding | None:
    loop, hops = hit.loop, hit.hops
    first = hops[0]
    info: RelationInfo | None = None
    if resolver is not None:
        info = resolver.is_relation(first)
        if info is None:
            return None
    chain = f"{loop.var}.{'.'.join(hops)}"
    path = "__".join((*loop.origin, first))
    if info is not None:
        method = "prefetch_related" if loop.origin else info.method
        suggestion = f'{method}("{path}")'
        why = f"{info.kind.replace('_', '-')} to {info.model}"
    elif dialect == "jinja":
        # Jinja templates usually render SQLAlchemy models (Flask), so name its loaders.
        rel = ".".join((*loop.origin, first))
        suggestion = f"selectinload(<Model>.{rel}) or joinedload(<Model>.{rel})"
        why = "a relationship, if it is one"
    else:
        suggestion = f'select_related("{path}") or prefetch_related("{path}")'
        why = "a relation, if it is one"
    return ScanFinding(
        file=filename,
        line=hit.line,
        loop_var=loop.var,
        chain=chain,
        kind="template",
        severity=_severity(hops),
        message=(
            f"`{chain}` inside the loop at line {loop.line} reads `{first}` ({why}), "
            "one query per iteration unless it is fetched up front"
        ),
        suggestion=suggestion,
        for_line=loop.line,
        first_hop=first,
        relation=info.kind if info else "",
        model=info.model if info else "",
    )


def jinja_search_paths(loader: Any) -> list[str]:
    """Template directories behind a Jinja loader (or an ``Environment``, or a list of either).

    Duck-typed (no jinja2 import): ``FileSystemLoader.searchpath``, ``PackageLoader``'s template
    root, and the children of ``ChoiceLoader`` / ``PrefixLoader`` / ``DictLoader``-likes.
    """
    out: list[str] = []

    def walk(obj: Any) -> None:
        if obj is None:
            return
        if isinstance(obj, (list, tuple, set)):
            for item in obj:
                walk(item)
            return
        if hasattr(obj, "jinja_loader") and not hasattr(obj, "loaders"):
            walk(obj.jinja_loader)  # a Flask app or blueprint
            return
        if hasattr(obj, "loader") and hasattr(obj, "globals"):
            walk(obj.loader)  # jinja2.Environment
            return
        sp = getattr(obj, "searchpath", None)
        if sp:
            for p in [sp] if isinstance(sp, (str, os.PathLike)) else sp:
                out.append(os.fspath(p))
        root = getattr(obj, "_template_root", None)  # PackageLoader
        if root:
            out.append(os.fspath(root))
        for child in getattr(obj, "loaders", None) or ():
            walk(child)
        mapping = getattr(obj, "mapping", None)
        if isinstance(mapping, dict) and not isinstance(obj, type):
            for child in mapping.values():
                if not isinstance(child, (str, bytes)):
                    walk(child)

    walk(loader)
    return list(dict.fromkeys(p for p in out if os.path.isdir(p)))


def scan_jinja_loader(
    loader: Any,
    *,
    resolver: RelationResolver | None = None,
    warnings: list[str] | None = None,
) -> list[ScanFinding]:
    """``scan_templates(jinja_search_paths(loader), "jinja")`` in one call."""
    return scan_templates(jinja_search_paths(loader), "jinja", resolver=resolver, warnings=warnings)


def format_table(findings: Iterable[ScanFinding], *, relative_to: str | None = None) -> str:
    """Aligned ``file:line  kind  chain  suggestion`` table (empty string for no findings)."""
    rows = []
    for f in findings:
        file = f.file
        if relative_to:
            try:
                file = os.path.relpath(file, relative_to)
            except ValueError:
                pass
        rows.append((f"{file}:{f.line}", f.kind, f.severity, f.chain, f.suggestion))
    if not rows:
        return ""
    head = ("LOCATION", "KIND", "SEVERITY", "CHAIN", "SUGGESTION")
    widths = [max(len(r[i]) for r in [head, *rows]) for i in range(4)]
    lines = []
    for r in [head, *rows]:
        cells = [r[i].ljust(widths[i]) for i in range(4)] + [r[4]]
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines)
