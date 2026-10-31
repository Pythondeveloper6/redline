"""Static, non-executing evaluation of narrowly defined SQL literal expressions."""

from __future__ import annotations

import ast
import string
from dataclasses import dataclass
from typing import Any

from .analyze import MAX_SOURCE_BYTES


@dataclass(frozen=True)
class SQLLiteral:
    """A literal SQL string wrapped by a known SQLAlchemy text constructor."""

    sql: str


class Unresolved(ValueError):
    """An expression that cannot be read statically; ``category`` says why."""

    def __init__(self, message: str, category: str = "dynamic"):
        super().__init__(message)
        self.category = category


def _bounded(value: Any) -> Any:
    if isinstance(value, str) and len(value.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise Unresolved("constant SQL exceeds source limit", "too_large")
    return value


_SCALARS = (str, int, float)


def _is_scalar(value: Any) -> bool:
    return isinstance(value, _SCALARS) and not isinstance(value, bool)


def _format(template: str, args: list, kwargs: dict) -> str:
    """``str.format`` restricted to plain fields; attribute and index access is refused."""
    try:
        for _, field, spec, _conv in string.Formatter().parse(template):
            if field is not None and ("." in field or "[" in field):
                raise Unresolved("unsafe format field", "unsafe_format")
            if spec and "{" in spec:
                raise Unresolved("nested format spec", "unsafe_format")
        return template.format(*args, **kwargs)
    except Unresolved:
        raise
    except (IndexError, KeyError, ValueError, TypeError):
        raise Unresolved("format arguments do not match", "unsafe_format") from None


def _classify(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return "unknown_name"
    if isinstance(node, ast.Call):
        return "call"
    if isinstance(node, (ast.Attribute, ast.Subscript)):
        return "attribute_or_subscript"
    if isinstance(node, ast.BinOp):
        return "operand"
    return "unsupported_expression"


def literal_value(
    node: ast.AST,
    constants: dict[str, Any],
    helpers: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    text_names: set[str],
    *,
    depth: int = 0,
) -> Any:
    """Read safe literals and deliberately small pure SQL helper functions from an AST."""
    if depth > 12:
        raise Unresolved("constant expression too deep", "too_deep")
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float)):
        return _bounded(node.value)
    if isinstance(node, ast.Name) and node.id in constants:
        return constants[node.id]
    if isinstance(node, ast.Attribute):
        parts = []
        current: ast.AST = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            key = ".".join([current.id, *reversed(parts)])
            if key in constants:
                return constants[key]
    if isinstance(node, (ast.Tuple, ast.List)):
        items = tuple(
            literal_value(item, constants, helpers, text_names, depth=depth + 1)
            for item in node.elts
        )
        if all(_is_scalar(item) for item in items):
            return items
        raise Unresolved("non-literal sequence element", "operand")
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        left = literal_value(node.left, constants, helpers, text_names, depth=depth + 1)
        right = literal_value(node.right, constants, helpers, text_names, depth=depth + 1)
        if isinstance(node.op, ast.Add):
            if isinstance(left, str) and isinstance(right, str):
                return _bounded(left + right)
        elif isinstance(left, str):
            if isinstance(right, tuple) and not isinstance(node.right, ast.Tuple):
                raise Unresolved("ambiguous %-format operand", "operand")
            if _is_scalar(right) or isinstance(right, tuple):
                try:
                    return _bounded(left % right)
                except (TypeError, ValueError, KeyError):
                    raise Unresolved("%-format arguments do not match", "unsafe_format") from None
    if isinstance(node, ast.JoinedStr):
        pieces = []
        size = 0
        for part in node.values:
            if isinstance(part, ast.FormattedValue):
                if part.format_spec is not None or part.conversion != -1:
                    raise Unresolved("formatted dynamic SQL", "unsafe_format")
                part = part.value
            value = literal_value(part, constants, helpers, text_names, depth=depth + 1)
            if not _is_scalar(value):
                raise Unresolved("dynamic SQL", _classify(part))
            piece = str(value)
            size += len(piece.encode("utf-8"))
            if size > MAX_SOURCE_BYTES:
                raise Unresolved("constant SQL exceeds source limit", "too_large")
            pieces.append(piece)
        return "".join(pieces)
    if isinstance(node, ast.Call):
        name = _call_name(node.func)
        if name in text_names and node.args and not node.keywords:
            value = literal_value(node.args[0], constants, helpers, text_names, depth=depth + 1)
            if isinstance(value, str):
                return SQLLiteral(value)
        if isinstance(node.func, ast.Attribute) and node.func.attr in {
            "bindparams",
            "execution_options",
        }:
            base = literal_value(node.func.value, constants, helpers, text_names, depth=depth + 1)
            if isinstance(base, SQLLiteral):
                return base
        if isinstance(node.func, ast.Attribute) and node.func.attr in {"format", "join"}:
            base = literal_value(node.func.value, constants, helpers, text_names, depth=depth + 1)
            if isinstance(base, str) and node.func.attr == "format":
                if any(isinstance(a, ast.Starred) for a in node.args) or any(
                    k.arg is None for k in node.keywords
                ):
                    raise Unresolved("starred format arguments", "unsafe_format")
                args = [
                    literal_value(a, constants, helpers, text_names, depth=depth + 1)
                    for a in node.args
                ]
                kwargs = {
                    k.arg: literal_value(k.value, constants, helpers, text_names, depth=depth + 1)
                    for k in node.keywords
                }
                if all(_is_scalar(v) for v in [*args, *kwargs.values()]):
                    return _bounded(_format(base, args, kwargs))
            elif isinstance(base, str) and len(node.args) == 1 and not node.keywords:
                parts = literal_value(node.args[0], constants, helpers, text_names, depth=depth + 1)
                if isinstance(parts, tuple) and all(isinstance(p, str) for p in parts):
                    return _bounded(base.join(parts))
        if isinstance(node.func, ast.Name) and name in helpers:
            return _call_helper(node, helpers[name], constants, helpers, text_names, depth)
    raise Unresolved("dynamic SQL", _classify(node))


def _call_helper(
    call: ast.Call,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    constants: dict[str, Any],
    helpers: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    text_names: set[str],
    depth: int,
) -> Any:
    if (
        isinstance(function, ast.AsyncFunctionDef)
        or function.decorator_list
        or function.args.vararg
        or function.args.kwarg
        or function.args.kwonlyargs
    ):
        raise Unresolved("unsafe SQL helper", "unsafe_helper")
    body = list(function.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        if isinstance(body[0].value.value, str):
            body.pop(0)
    if len(body) != 1 or not isinstance(body[0], ast.Return) or body[0].value is None:
        raise Unresolved("unsafe SQL helper", "unsafe_helper")
    positional = [*function.args.posonlyargs, *function.args.args]
    if len(call.args) > len(positional) or any(arg is None for arg in function.args.defaults):
        raise Unresolved("SQL helper arguments must be literal", "operand")
    bound: dict[str, Any] = dict(constants)
    assigned: set[str] = set()
    for parameter, argument in zip(positional, call.args, strict=False):
        bound[parameter.arg] = literal_value(
            argument, constants, helpers, text_names, depth=depth + 1
        )
        assigned.add(parameter.arg)
    for keyword in call.keywords:
        if keyword.arg is None:
            raise Unresolved("SQL helper arguments must be literal", "operand")
        parameter = next((arg for arg in function.args.args if arg.arg == keyword.arg), None)
        if parameter is None or parameter.arg in assigned:
            raise Unresolved("SQL helper arguments must be literal", "operand")
        bound[parameter.arg] = literal_value(
            keyword.value, constants, helpers, text_names, depth=depth + 1
        )
        assigned.add(parameter.arg)
    default_start = len(positional) - len(function.args.defaults)
    for index, default in enumerate(function.args.defaults, start=default_start):
        parameter = positional[index]
        if parameter.arg not in assigned:
            bound[parameter.arg] = literal_value(
                default, constants, helpers, text_names, depth=depth + 1
            )
            assigned.add(parameter.arg)
    if any(parameter.arg not in assigned for parameter in positional):
        raise Unresolved("SQL helper arguments must be literal", "operand")
    return literal_value(body[0].value, bound, helpers, text_names, depth=depth + 1)


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""
