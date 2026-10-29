"""Observed direct session statements; no probes, automatic resets or inferred baselines."""

from __future__ import annotations

import hashlib
import re
import time

from .sql import _TOKEN

_INERT = {"string", "dollar", "lcomment", "bcomment", "dident", "bident", "sqident"}


def _statements(sql):
    """Split real semicolon boundaries, preserving value literals and ignoring comments."""
    code = _TOKEN.sub(lambda m: " " * len(m.group()) if m.lastgroup in _INERT else m.group(), sql)
    source = _TOKEN.sub(
        lambda m: " " * len(m.group()) if m.lastgroup in {"lcomment", "bcomment"} else m.group(),
        sql,
    )
    start = 0
    for match in re.finditer(";", code):
        yield source[start : match.start()].strip()
        start = match.end()
    yield source[start:].strip()


def _entry(span, value, *, digest=None):
    result = {
        "value": value,
        "span_id": span.id,
        "callsite": span.callsite.to_dict() if span.callsite else None,
    }
    if digest is not None:
        result["value_digest"] = digest
    return result


def observe(info, sql, span):
    if span.data.get("vendor") != "mssql":
        observe_generic(info, sql, span)
        return
    changes = info.setdefault("redline_session", {})
    pattern = r"SET\s+(NOCOUNT|XACT_ABORT|ANSI_NULLS|ANSI_WARNINGS|ARITHABORT)\s+(ON|OFF)\s*"
    for statement in _statements(sql):
        match = re.fullmatch(pattern, statement, re.I)
        if match:
            option, value = (v.upper() for v in match.groups())
            changes[option] = _entry(span, value)
    span.data["session_options_observed"] = {k: v["value"] for k, v in changes.items()}


def checked_in(info, alias):
    from .recorder import current, record_span

    builder = current()
    changes = info.get("redline_session", {})
    if builder is None or builder.finished or not changes:
        return
    expected = {k.upper(): v for k, v in builder.recorder.settings.session_expected.items()}
    changed = {
        k: v
        for k, v in changes.items()
        if k in expected
        and (
            v["value_digest"] != hashlib.sha256(expected[k].encode()).hexdigest()[:16]
            if v.get("value_digest")
            else v["value"] != expected[k].upper()
        )
    }
    if changed:
        record_span(
            "custom",
            "Observed session options at pool checkin",
            time.perf_counter(),
            0,
            {"session_state_leak": dict(changed), "db_alias": alias, "observation_only": True},
            capture_callsite=False,
        )


def observe_generic(info, sql, span):
    vendor = span.data.get("vendor")
    options = {
        "postgresql": "search_path|statement_timeout|role|timezone",
        "mysql": "sql_mode|time_zone|autocommit",
    }.get(vendor)
    if options is None:
        return
    changes = info.setdefault("redline_session", {})
    for statement in _statements(sql):
        if vendor == "postgresql":
            reset = re.fullmatch(
                r"RESET\s+(ALL|search_path|statement_timeout|role|timezone)\s*", statement, re.I
            )
            if reset:
                if reset[1].upper() == "ALL":
                    changes.clear()
                else:
                    changes.pop(reset[1].upper(), None)
                continue
        match = re.fullmatch(
            rf"SET\s+(?:SESSION\s+)?({options})\s*(?:=|TO)\s*(.+)", statement, re.I | re.S
        )
        if match:
            option, value = match[1].upper(), match[2].strip()
            if value.upper() == "DEFAULT":
                changes.pop(option, None)
            else:
                changes[option] = _entry(
                    span, "observed", digest=hashlib.sha256(value.encode()).hexdigest()[:16]
                )
    span.data["session_options_observed"] = {k: v["value"] for k, v in changes.items()}
