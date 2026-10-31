"""``blocking_in_async``: the event loop was stalled by synchronous work (``CONTRACT.md`` §9).

Fires for every ``blocking`` span of at least ``blocking_ms`` (recorded by
:mod:`redline_core.blocking`). Spans with the same blocking call and callsite form one
issue, so ``time.sleep`` in a loop is reported once with its count and total.
"""

from __future__ import annotations

from ..conf import Settings
from ..model import Callsite, Issue, IssueKind, Span, Suggestion, Trace

__all__ = ["detect"]

_KIND: IssueKind = "blocking_in_async"
_HTTP = ("requests", "urllib", "http.client", "httpx", "socket")
_DB = ("SQLAlchemy", "sqlite3", "psycopg", "pymysql", "MySQLdb", "redis")


def detect(trace: Trace, settings: Settings) -> list[Issue]:
    from ..detect import issue_id  # here: redline_core.detect imports this package

    limit = settings.blocking_ms
    groups: dict[tuple[str | None, str | None], list[Span]] = {}
    for s in trace.spans:
        if s.kind != "blocking" or s.duration_ms < limit:
            continue
        loc = s.callsite.location if s.callsite else None
        groups.setdefault((s.data.get("call"), loc), []).append(s)
    issues = []
    for (call, loc), spans in groups.items():
        worst = max(s.duration_ms for s in spans)
        total = sum(s.duration_ms for s in spans)
        callsite = next((s.callsite for s in spans if s.callsite), None)
        reason = spans[0].data.get("reason")
        n = len(spans)
        title = f"Event loop blocked {worst:.0f} ms"
        if call:
            title += f" by {call}"
        if callsite:
            title += f" at {callsite.location}"
        if reason == "sync_call":
            what = f"A synchronous {call or 'database'} call ran on the event loop thread"
        else:
            what = f"{call or 'Synchronous code'} held the event loop"
        times = "" if n == 1 else f" ({n} times, {total:.0f} ms in total)"
        issues.append(
            Issue(
                id=issue_id(_KIND, call, loc or f"{trace.method} {trace.route or trace.path}"),
                kind=_KIND,
                severity="critical" if worst >= 5 * limit else "warning",
                title=title,
                detail=(
                    f"{what} for {worst:.1f} ms{times}; every other request on this worker "
                    "waited meanwhile."
                ),
                span_ids=[s.id for s in spans],
                fingerprint=None,
                callsite=callsite,
                count=n,
                total_ms=round(total, 3),
                suggestion=_suggest(call, trace.framework, callsite),
                extra={"call": call, "reason": reason, "max_ms": round(worst, 3)},
            )
        )
    return issues


def _offload(framework: str) -> tuple[str, str]:
    if framework == "django":
        return "sync_to_async", "await sync_to_async(fn)(*args)"
    if framework == "fastapi":
        return "run_in_threadpool", "await run_in_threadpool(fn, *args)"
    return "asyncio.to_thread", "await asyncio.to_thread(fn, *args)"


def _suggest(call: str | None, framework: str, callsite: Callsite | None) -> Suggestion:
    name, code = _offload(framework)
    c = call or ""
    if c in ("time.sleep", "sleep"):
        return Suggestion(
            summary="Replace time.sleep() with await asyncio.sleep() – it pauses this "
            "request without stopping the event loop.",
            code="await asyncio.sleep(seconds)",
            confidence="high",
        )
    if c == "sync ORM":
        if framework == "django":
            return Suggestion(
                summary="Use the async ORM API (aget, afirst, acount, async for …) or wrap "
                "the query in sync_to_async so it runs on a worker thread.",
                code="obj = await Model.objects.aget(pk=pk)",
                confidence="medium",
            )
        return Suggestion(
            summary=f"Move the ORM call off the event loop with {name}, or use an async driver.",
            code=code,
            confidence="medium",
        )
    if c.startswith(_HTTP):
        return Suggestion(
            summary="Use an async HTTP client (httpx.AsyncClient, aiohttp) or run the "
            f"call with {name} so the event loop keeps serving other requests.",
            code="async with httpx.AsyncClient() as client:\n    r = await client.get(url)",
            confidence="medium",
        )
    if c == "SQLAlchemy work":
        return Suggestion(
            summary="Profile SQL compilation and ORM materialization; async SQLAlchemy also "
            "does CPU work on the event loop. Confirm a synchronous driver call before "
            "changing drivers or moving database work to another thread.",
            confidence="low",
        )
    if c.startswith(_DB):
        return Suggestion(
            summary="Use the async driver (SQLAlchemy AsyncSession with asyncpg/aiosqlite, "
            f"redis.asyncio) or run the call with {name}.",
            code=code,
            confidence="medium",
        )
    return Suggestion(
        summary=f"Move the blocking work off the event loop with {name} (or "
        "asyncio.to_thread), use an async library, or make the endpoint a plain def.",
        code=code,
        confidence="low" if callsite is None else "medium",
    )
