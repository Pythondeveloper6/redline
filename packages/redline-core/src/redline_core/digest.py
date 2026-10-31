"""``redline digest``: post newly seen issues to a chat webhook.

Reads the store read-only, picks the issues first seen within ``--since`` that earlier
digests have not reported, and POSTs one Slack-compatible ``{"text": ...}`` JSON payload.
The state file remembers the issue ids that were sent (only after the webhook accepted
them), so running the command from cron or CI never repeats an issue. Issue ids are the
stable fingerprints that deduplicate issues across requests. Text is redacted.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

__all__ = ["build_payload", "command", "configure", "new_issues"]

SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}
MAX_LISTED = 20
STATE_KEEP_S = 30 * 86_400
STATE_FORMAT = "redline-digest-state"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--store", required=True, metavar="DB", help="Redline SQLite store")
    parser.add_argument("--webhook", metavar="URL", help="Slack-compatible incoming webhook")
    parser.add_argument(
        "--since", default="24h", metavar="DURATION", help="look back this far (default 24h)"
    )
    parser.add_argument(
        "--min-severity",
        choices=sorted(SEVERITY_ORDER, key=SEVERITY_ORDER.get),  # type: ignore[arg-type]
        default="info",
        help="only issues at or above this severity",
    )
    parser.add_argument(
        "--state",
        metavar="FILE",
        help="remembers what was sent (default: digest-state.json next to the store)",
    )
    parser.add_argument("--ui-url", metavar="URL", help="link to the Redline UI in the message")
    parser.add_argument("--title", default="Redline", help="first line of the message")
    parser.add_argument("--dry-run", action="store_true", help="print the payload, send nothing")


def _load_state(path: Path) -> dict[str, float]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise ValueError(f"{path}: unreadable digest state") from None
    if not isinstance(data, dict) or data.get("format") != STATE_FORMAT:
        raise ValueError(f"{path}: not a digest state file")
    sent = data.get("sent")
    if not isinstance(sent, dict):
        return {}
    return {str(k): float(v) for k, v in sent.items() if isinstance(v, (int, float))}


def _save_state(path: Path, sent: dict[str, float], now: float) -> None:
    kept = {k: v for k, v in sent.items() if v >= now - STATE_KEEP_S}
    text = json.dumps({"format": STATE_FORMAT, "sent": kept}, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".digest-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def new_issues(
    entries: list[Any], sent: dict[str, float], *, since_ts: float, min_severity: str
) -> list[Any]:
    """Index entries first seen after ``since_ts``, not yet sent, at or above the severity.
    Worst first, then most frequent, then id (deterministic)."""
    floor = SEVERITY_ORDER[min_severity]
    picked = [
        e
        for e in entries
        if e.first_seen >= since_ts
        and e.id not in sent
        and SEVERITY_ORDER.get(e.severity, 0) >= floor
    ]
    picked.sort(key=lambda e: (-SEVERITY_ORDER.get(e.severity, 0), -e.occurrences, e.id))
    return picked


def _slack(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_payload(
    issues: list[Any],
    *,
    since_label: str,
    redactor: Any,
    title: str = "Redline",
    ui_url: str | None,
) -> dict[str, str]:
    lines = [
        f"*{_slack(title)}*: {len(issues)} new issue{'s' if len(issues) != 1 else ''} "
        f"in the last {since_label}"
    ]
    for e in issues[:MAX_LISTED]:
        text = redactor.text(str(e.title))[:200]
        route = redactor.text(str(e.route or ""))[:120]
        where = f" on `{_slack(route)}`" if route else ""
        lines.append(
            f"• [{e.severity}] {_slack(str(e.kind))}: {_slack(text)}{where} (x{e.occurrences})"
        )
    if len(issues) > MAX_LISTED:
        lines.append(f"…and {len(issues) - MAX_LISTED} more")
    if ui_url:
        lines.append(f"{ui_url.rstrip('/')}/#/issues")
    return {"text": "\n".join(lines)}


def _post(url: str, payload: dict[str, Any]) -> None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username:
        raise ValueError("the webhook must be an http(s) URL without credentials")
    request = Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with build_opener(_NoRedirect).open(request, timeout=15) as response:
            response.read(65_537)
    except HTTPError as exc:
        raise OSError(f"the webhook answered HTTP {exc.code}") from None
    except URLError as exc:
        raise OSError(f"the webhook is unreachable ({exc.reason})") from None


def command(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    from .load.profile import ScenarioError, parse_duration
    from .report_html import project_redactor
    from .storage.multiple import TraceReader

    try:
        window = parse_duration(args.since)
    except ScenarioError as exc:
        err.write(f"redline digest: --since: {exc.message}\n")
        return 2
    if not args.webhook and not args.dry_run:
        err.write("redline digest: --webhook is required (or use --dry-run)\n")
        return 2
    state_path = Path(args.state) if args.state else Path(args.store).parent / "digest-state.json"
    try:
        sent = _load_state(state_path)
    except ValueError as exc:
        err.write(f"redline digest: {exc}\n")
        return 2
    try:
        reader = TraceReader(args.store)
    except (OSError, ValueError):
        err.write("redline digest: cannot open the store read-only\n")
        return 1
    now = time.time()
    try:
        entries = reader.issue_index()
    except Exception:
        err.write("redline digest: the store has no issue history\n")
        return 1
    finally:
        reader.close()
    issues = new_issues(entries, sent, since_ts=now - window, min_severity=args.min_severity)
    if not issues:
        out.write("no new issues\n")
        return 0
    payload = build_payload(
        issues,
        since_label=args.since,
        redactor=project_redactor(),
        title=args.title,
        ui_url=args.ui_url,
    )
    if args.dry_run:
        out.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        return 0
    try:
        _post(args.webhook, payload)
    except (OSError, ValueError) as exc:
        err.write(f"redline digest: {exc}; nothing recorded as sent\n")
        return 1
    for e in issues:
        sent[e.id] = now
    try:
        _save_state(state_path, sent, now)
    except OSError as exc:
        err.write(f"redline digest: sent, but the state file could not be written ({exc})\n")
        return 1
    out.write(f"sent {len(issues)} new issue(s)\n")
    return 0
