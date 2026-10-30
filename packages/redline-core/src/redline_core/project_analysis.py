"""Bounded import-aware query summaries; project code is parsed, never imported."""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

MAX_IMPORT_PASSES = 8


def _bindings(node):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [alias.asname or alias.name.split(".")[0] for alias in node.names]
    return [
        part.id
        for part in ast.walk(node)
        if isinstance(part, ast.Name) and isinstance(part.ctx, (ast.Store, ast.Del))
    ]


def analyze_imports(sources: dict[str, str], root: Path):
    from .analyze import analyze_source

    prefix = root.name if (root / "__init__.py").is_file() else ""
    modules, imports, reports, summaries, previous = {}, {}, {}, {}, {}
    for file, source in sources.items():
        parts = list(Path(file).with_suffix("").parts)
        package_file = parts[-1] == "__init__"
        if package_file:
            parts.pop()
        module = ".".join(([prefix] if prefix else []) + parts)
        if not module:
            continue
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError, RecursionError):
            reports[file] = analyze_source(source, file)
            continue
        modules[module] = file
        counts = Counter(name for node in tree.body for name in _bindings(node))
        dynamic = {name for n in ast.walk(tree) if isinstance(n, ast.Global) for name in n.names}
        package = module.split(".") if package_file else module.split(".")[:-1]
        refs = {}
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    if counts[local] == 1 and local not in dynamic:
                        refs[local] = alias.name if alias.asname else local
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    if node.level > len(package):
                        continue
                    base = package[: len(package) - node.level + 1]
                else:
                    base = []
                if node.module:
                    base += node.module.split(".")
                for alias in node.names:
                    local = alias.asname or alias.name
                    if alias.name != "*" and counts[local] == 1 and local not in dynamic:
                        refs[local] = ".".join([*base, alias.name])
        imports[module] = refs

    # A source file with no module name is still analyzed normally.
    for file, source in sources.items():
        if file not in reports:
            summaries[file] = set()
            reports[file] = analyze_source(source, file, _summary=summaries[file])
            previous[file] = frozenset()
    changed = False
    forwarded = set()
    passes = 0
    for _ in range(MAX_IMPORT_PASSES):
        passes += 1
        exports = {
            f"{module}.{name}"
            for module, file in modules.items()
            for name in summaries.get(file, ())
        } | forwarded
        next_forwarded = {
            f"{module}.{local}"
            for module, refs in imports.items()
            for local, canonical in refs.items()
            if canonical in exports
        }
        suffixes = {}
        for symbol in exports:
            parts = symbol.split(".")
            for length in range(1, len(parts) + 1):
                canonical = ".".join(parts[:length])
                suffix = "." + ".".join(parts[length:]) if length < len(parts) else ""
                suffixes.setdefault(canonical, []).append(suffix)
        changed = next_forwarded != forwarded
        forwarded = next_forwarded
        for module, file in modules.items():
            available = set()
            for local, canonical in imports.get(module, {}).items():
                for suffix in suffixes.get(canonical, ()):
                    available.add(local + suffix)
            available = frozenset(available)
            if previous.get(file) == available:
                continue
            previous[file] = available
            found = set()
            reports[file] = analyze_source(
                sources[file], file, _imported_helpers=available, _summary=found
            )
            if found != summaries.get(file):
                summaries[file] = found
                changed = True
        if not changed:
            break
    resolved = sum(
        canonical in modules or canonical.rpartition(".")[0] in modules
        for refs in imports.values()
        for canonical in refs.values()
    )
    return reports, {
        "mode": "bounded project imports",
        "passes": passes,
        "resolved_imports": resolved,
        "helper_exports": sum(len(names) for names in summaries.values()),
        "limited": changed,
        "limitations": [
            "Only files included in this scan are followed; external modules are never imported.",
            "Dynamic imports, wildcard imports, dynamic factories "
            "and class dispatch may be missed.",
            "Warnings describe possible queries, including identity-map hits "
            "and conditional paths.",
        ],
    }
