"""A small Model Context Protocol server over stdio (line-delimited JSON-RPC 2.0).

Implements ``initialize``, ``ping``, ``tools/list`` and ``tools/call`` only, with the
read-only tools of :mod:`redline_core.agent.tools`. The store is opened read-only. Nothing
is written to stdout except protocol messages; a bad line gets an error reply and the loop
carries on.
"""

from __future__ import annotations

import json
from typing import Any, TextIO

from .audit import AuditLog
from .tools import TOOLS, call_tool

__all__ = ["serve"]

PROTOCOL_VERSION = "2024-11-05"
MAX_LINE = 1_048_576


def _error(id_: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _handle(msg: Any, store: Any, audit: AuditLog | None) -> dict[str, Any] | None:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid request")
    method, id_, params = msg["method"], msg.get("id"), msg.get("params")
    if "id" not in msg:  # a notification never gets a reply
        return None
    if method == "initialize":
        from .. import __version__

        return {
            "jsonrpc": "2.0",
            "id": id_,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "redline", "version": __version__},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": id_, "result": {}}
    if method == "tools/list":
        tools = [
            {"name": t.name, "description": t.description, "inputSchema": t.schema}
            for t in TOOLS.values()
        ]
        return {"jsonrpc": "2.0", "id": id_, "result": {"tools": tools}}
    if method == "tools/call":
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return _error(id_, -32602, "tools/call needs params.name")
        args = params.get("arguments", {})
        result = call_tool(params["name"], args, store=store, audit=audit)
        return {
            "jsonrpc": "2.0",
            "id": id_,
            "result": {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                "isError": "error" in result,
            },
        }
    return _error(id_, -32601, f"Method not found: {str(method)[:60]}")


def serve(store_path: str, stdin: TextIO, stdout: TextIO, *, audit_path: str | None = None) -> None:
    """Serve until ``stdin`` closes. ``store_path`` must be an existing redline store."""
    from ..storage.multiple import TraceReader

    store = TraceReader(store_path)
    audit = AuditLog(audit_path) if audit_path else None
    try:
        while True:
            line = stdin.readline(MAX_LINE + 1)
            if not line:
                return
            if len(line) > MAX_LINE:
                while line and not line.endswith("\n"):  # drop the rest of the oversized line
                    line = stdin.readline(MAX_LINE)
                reply: dict[str, Any] | None = _error(None, -32600, "Request too large")
            elif not line.strip():
                continue
            else:
                try:
                    reply = _handle(json.loads(line), store, audit)
                except json.JSONDecodeError:
                    reply = _error(None, -32700, "Parse error")
                except RecursionError:
                    reply = _error(None, -32600, "Request too deeply nested")
            if reply is not None:
                stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
                stdout.flush()
    finally:
        store.close()


def configure(parser: Any) -> None:
    parser.add_argument("--store", required=True, help="path to an existing redline store")
    parser.add_argument("--audit", metavar="FILE", help="append a JSONL record of each tool call")


def command(args: Any, out: TextIO, err: TextIO) -> int:
    import os
    import sys

    if not os.path.isfile(args.store):
        err.write(f"redline mcp: store not found: {args.store}\n")
        return 2
    serve(args.store, sys.stdin, out, audit_path=args.audit)
    return 0
