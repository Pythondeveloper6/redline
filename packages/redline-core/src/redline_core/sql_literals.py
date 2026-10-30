"""Static, non-executing evaluation of narrowly defined SQL literal expressions."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any

from .analyze import MAX_SOURCE_BYTES


@dataclass(frozen=True)
class SQLLiteral:
    """A literal SQL string wrapped by a known SQLAlchemy text constructor."""

    sql: str


def _bounded(value: Any) -> Any:
    if isinstance(value, str) and len(value.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ValueError("constant SQL exceeds source limit")
    return value


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
        raise ValueError("constant expression too deep")
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
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = literal_value(node.left, constants, helpers, text_names, depth=depth + 1)
        right = literal_value(node.right, constants, helpers, text_names, depth=depth + 1)
        if isinstance(left, str) and isinstance(right, str):
            return _bounded(left + right)
    if isinstance(node, ast.JoinedStr):
        pieces = []
        size = 0
        for part in node.values:
            if isinstance(part, ast.FormattedValue):
                if part.format_spec is not None or part.conversion != -1:
                    raise ValueError("formatted dynamic SQL")
                part = part.value
            value = literal_value(part, constants, helpers, text_names, depth=depth + 1)
            if not isinstance(value, (str, int, float)):
                raise ValueError("dynamic SQL")
            piece = str(value)
            size += len(piece.encode("utf-8"))
            if size > MAX_SOURCE_BYTES:
                raise ValueError("constant SQL exceeds source limit")
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
        if isinstance(node.func, ast.Name) and name in helpers:
            return _call_helper(node, helpers[name], constants, helpers, text_names, depth)
    raise ValueError("dynamic SQL")


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
        raise ValueError("unsafe SQL helper")
    body = list(function.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        if isinstance(body[0].value.value, str):
            body.pop(0)
    if len(body) != 1 or not isinstance(body[0], ast.Return) or body[0].value is None:
        raise ValueError("unsafe SQL helper")
    positional = [*function.args.posonlyargs, *function.args.args]
    if len(call.args) > len(positional) or any(arg is None for arg in function.args.defaults):
        raise ValueError("SQL helper arguments must be literal")
    bound: dict[str, Any] = dict(constants)
    assigned: set[str] = set()
    for parameter, argument in zip(positional, call.args, strict=False):
        bound[parameter.arg] = literal_value(
            argument, constants, helpers, text_names, depth=depth + 1
        )
        assigned.add(parameter.arg)
    for keyword in call.keywords:
        if keyword.arg is None:
            raise ValueError("SQL helper arguments must be literal")
        parameter = next((arg for arg in function.args.args if arg.arg == keyword.arg), None)
        if parameter is None or parameter.arg in assigned:
            raise ValueError("SQL helper arguments must be literal")
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
        raise ValueError("SQL helper arguments must be literal")
    return literal_value(body[0].value, bound, helpers, text_names, depth=depth + 1)


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""
