"""A self-contained brief an AI agent can act on to fix one finding.

Built from fields redline already stores (callsite, normalised SQL fingerprint with literals
replaced by ``?``, evidence, suggestion). Prompt and completion text of LLM calls never
appears. The result is passed through the redactor again before it is returned.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["build_fix_prompt"]

MAX_CHARS = 12_000


def _line(label: str, value: Any) -> str | None:
    return f"- **{label}:** {value}" if value not in (None, "", [], {}) else None


def build_fix_prompt(group: Mapping[str, Any]) -> str:
    """Markdown brief for one issue group (an item of ``GET api/issues``)."""
    site = group.get("callsite") or {}
    where = f"{site['file']}:{site['line']}" if site.get("file") else None
    why = group.get("why") or {}
    suggestion = group.get("suggestion") or {}
    facts = [
        _line("Kind", f"`{group.get('kind')}` ({group.get('severity', 'warning')})"),
        _line("Where", f"`{where}` in `{site.get('function')}`" if where else None),
        _line("Routes", ", ".join(f"`{r}`" for r in group.get("routes") or [])),
        _line("Seen", f"{group.get('occurrences')} sampled request(s)"),
        _line("Observed time", f"{group.get('observed_total_ms')} ms (sampled, overlapping)"),
        _line("SQL shape", f"`{group.get('fingerprint')}`" if group.get("fingerprint") else None),
        _line("Why it fired", why.get("rule")),
        _line("Observed", why.get("observed")),
        _line("Threshold", why.get("threshold")),
        _line("Limits", why.get("limits")),
        _line("Suggested direction", suggestion.get("summary")),
    ]
    lines = [
        f"# Fix performance finding `{group.get('id')}`",
        "",
        str(group.get("title") or ""),
        "",
        str(group.get("detail") or ""),
        "",
        "## Evidence",
        *[f for f in facts if f],
        "",
    ]
    diff = suggestion.get("diff")
    if diff:
        lines += [
            "## Suggested diff (starting point, not a guarantee)",
            "```diff",
            str(diff),
            "```",
            "",
        ]
    lines += [
        "## Constraints",
        "- Keep the response of every affected route byte-for-byte equivalent in meaning;"
        " do not change its shape, ordering or status codes.",
        "- Do not weaken tests or thresholds to make the finding disappear.",
        "- Prefer the smallest change at the callsite above.",
        "",
        "## Verify",
        "1. Record a baseline before the change: `redline check record SUITE`.",
        "2. Make the change, then: `redline check compare SUITE --json report.json`.",
        f"3. `redline check verify report.json --kind {group.get('kind')}`"
        " must print `finding: gone` with responses `equal`.",
        "If it prints `inconclusive`, collect more samples (raise `repeat`) instead of "
        "declaring success.",
    ]
    from ..redact import Redactor

    text = Redactor().text("\n".join(lines))
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + "\n…(truncated)"


def configure_fix_prompt(parser: Any) -> None:
    parser.add_argument("issue_id", help="issue id from `redline mcp` list_issues or the UI")
    parser.add_argument("--store", required=True, help="path to the redline store")
    parser.add_argument("--window", default="7d", help="look-back window (default 7d)")
    parser.add_argument("--out", metavar="FILE", help="write the brief to FILE")


def command_fix_prompt(args: Any, out: Any, err: Any) -> int:
    import os

    from .tools import call_tool

    if not os.path.isfile(args.store):
        err.write(f"redline fix-prompt: store not found: {args.store}\n")
        return 2
    from ..storage.multiple import TraceReader

    store = TraceReader(args.store)
    try:
        group = call_tool(
            "explain_issue", {"issue_id": args.issue_id, "window": args.window}, store=store
        )
    finally:
        store.close()
    if "error" in group:
        err.write(f"redline fix-prompt: {group['error']}\n")
        return 2
    text = build_fix_prompt(group) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        out.write(text)
    return 0
