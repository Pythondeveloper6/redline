"""Observed direct session statements; no probes, automatic resets or inferred baselines."""

from __future__ import annotations

import hashlib
import re
import time

from .sql import _TOKEN

_INERT = {"string", "dollar", "lcomment", "bcomment", "dident", "bident", "sqident"}

_MSSQL_FLAGS = (
    "NOCOUNT|XACT_ABORT|ANSI_NULLS|ANSI_WARNINGS|ANSI_PADDING|ANSI_NULL_DFLT_ON|"
    "ANSI_NULL_DFLT_OFF|ARITHABORT|CONCAT_NULL_YIELDS_NULL|QUOTED_IDENTIFIER|"
    "NUMERIC_ROUNDABORT|IMPLICIT_TRANSACTIONS|CURSOR_CLOSE_ON_COMMIT"
)
_MSSQL_VALUES = "DATEFORMAT|LANGUAGE|DATEFIRST|LOCK_TIMEOUT|DEADLOCK_PRIORITY"
_ISOLATION = re.compile(
    r"SET\s+TRANSACTION\s+ISOLATION\s+LEVEL\s+"
    r"(READ\s+UNCOMMITTED|READ\s+COMMITTED|REPEATABLE\s+READ|SNAPSHOT|SERIALIZABLE)\s*",
    re.I,
)
_CONTEXT = re.compile(r"(?:EXEC(?:UTE)?\s+)?(?:sys\.)?sp_set_session_context\s+(.+)$", re.I | re.S)
_CONTEXT_KEY = re.compile(r"@key\s*=\s*N?'([^']{1,128})'", re.I)
_CONTEXT_VALUE = re.compile(
    r"@value\s*=\s*(.+?)(?=,\s*@(?:key|value|read_only)\b|\s*$)", re.I | re.S
)
_CONTEXT_READ_ONLY = re.compile(r"@read_only\s*=\s*(\d+|N?'(?:true|false|on|off)')", re.I)
_STATE_HINT = re.compile(r"\b(?:SET|EXEC|SET_CONFIG|RESET|DISCARD)\b", re.I)


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


def _entry(span, value, *, digest=None, **extra):
    result = {
        "value": value,
        "span_id": span.id,
        "callsite": span.callsite.to_dict() if span.callsite else None,
        **extra,
    }
    if digest is not None:
        result["value_digest"] = digest
    return result


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _truthy(value):
    return value.strip("Nn'\"").lower() in {"1", "true", "on"}


def _observe_context(changes, span, statement):
    match = _CONTEXT.fullmatch(statement.strip())
    if not match:
        return False
    arguments = match[1]
    key_match = _CONTEXT_KEY.search(arguments)
    if not key_match:
        return True
    key = "CONTEXT:" + key_match[1]
    value_match = _CONTEXT_VALUE.search(arguments)
    value = value_match[1].strip() if value_match else ""
    if not value or value.upper() == "NULL":
        changes.pop(key, None)
        return True
    read_only_match = _CONTEXT_READ_ONLY.search(arguments)
    read_only = _truthy(read_only_match[1]) if read_only_match else False
    changes[key] = _entry(
        span,
        "observed",
        digest=_digest(value),
        read_only=read_only,
    )
    return True


def observe(info, sql, span):
    # Most statements are plain SELECT/DML. Do not tokenize them just to discover they
    # set no session state; the cheap uppercase probe keeps the observation path free.
    upper = sql.upper()
    if not any(marker in upper for marker in ("SET ", "EXEC", "SET_CONFIG", "RESET", "DISCARD")):
        return
    if span.data.get("vendor") != "mssql":
        observe_generic(info, sql, span)
        return
    changes = info.setdefault("redline_session", {})
    flags = rf"SET\s+({_MSSQL_FLAGS})\s+(ON|OFF)\s*"
    for statement in _statements(sql):
        match = re.fullmatch(flags, statement, re.I)
        if match:
            option, value = (v.upper().replace("  ", " ") for v in match.groups())
            changes[option] = _entry(span, value)
            continue
        match = _ISOLATION.fullmatch(statement)
        if match:
            changes["TRANSACTION ISOLATION LEVEL"] = _entry(
                span, re.sub(r"\s+", " ", match[1].upper())
            )
            continue
        match = re.fullmatch(r"SET\s+IDENTITY_INSERT\s+(.+?)\s+(ON|OFF)\s*", statement, re.I | re.S)
        if match:
            table = re.sub(r"[\[\]`\"]", "", re.sub(r"\s+", "", match[1])).upper()
            key = "IDENTITY_INSERT:" + table
            if match[2].upper() == "ON":
                changes[key] = _entry(span, "ON")
            else:
                changes.pop(key, None)
            continue
        match = re.fullmatch(rf"SET\s+({_MSSQL_VALUES})\s+(.+?)\s*", statement, re.I | re.S)
        if match:
            changes[match[1].upper()] = _entry(span, match[2].strip().upper()[:64])
            continue
        _observe_context(changes, span, statement)
    span.data["session_options_observed"] = {k: v["value"] for k, v in changes.items()}


def checked_in(info, alias):
    from .recorder import current, record_span

    builder = current()
    changes = info.get("redline_session", {})
    if builder is None or builder.finished or not changes:
        return
    settings = builder.recorder.settings
    expected = {k.upper(): v for k, v in settings.session_expected.items()}
    expected_context = {key.upper() for key in settings.session_expected_context}
    changed = {}
    context_only = True
    for key, entry in changes.items():
        if key.startswith("CONTEXT:"):
            name = key[8:]
            read_only = bool(entry.get("read_only"))
            if expected_context and name.upper() in expected_context and read_only:
                continue
            if expected_context and name.upper() not in expected_context:
                changed[key] = entry
            elif not read_only:
                changed[key] = entry
            continue
        context_only = False
        if key in expected and (
            entry["value_digest"] != _digest(expected[key])
            if entry.get("value_digest")
            else entry["value"] != expected[key].upper()
        ):
            changed[key] = entry
    if changed:
        record_span(
            "custom",
            "Observed session options at pool checkin",
            time.perf_counter(),
            0,
            {
                "session_state_leak": dict(changed),
                "session_state_severity": "info" if context_only else "warning",
                "db_alias": alias,
                "observation_only": True,
            },
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
            config = re.fullmatch(
                r"SELECT\s+set_config\s*\(\s*'([^']{1,64})'\s*,\s*(.+?)\s*,\s*(false|true)\s*\)\s*",
                statement,
                re.I | re.S,
            )
            if config:
                name, value, local = config.groups()
                key = name.upper()
                if local.lower() == "true":
                    continue
                if value.strip().upper() == "NULL":
                    changes.pop(key, None)
                else:
                    changes[key] = _entry(span, "observed", digest=_digest(value.strip()))
                continue
            if re.fullmatch(r"SET\s+LOCAL\s+.+", statement, re.I | re.S):
                continue
            reset = re.fullmatch(
                r"(?:RESET\s+(ALL|search_path|statement_timeout|role|timezone)"
                r"|DISCARD\s+ALL)\s*",
                statement,
                re.I,
            )
            if reset:
                if reset[1] is None or reset[1].upper() == "ALL":
                    changes.clear()
                else:
                    changes.pop(reset[1].upper(), None)
                continue
        elif vendor == "mysql":
            variable = re.fullmatch(
                r"SET\s+(?:SESSION\s+|@@SESSION\.)?([A-Za-z_]\w*)\s*(?:=|:=)\s*(.+)",
                statement,
                re.I | re.S,
            )
            if variable:
                key, value = variable[1].upper(), variable[2].strip()
                if value.upper() == "DEFAULT":
                    changes.pop(key, None)
                else:
                    changes[key] = _entry(span, "observed", digest=_digest(value))
                continue
            user_variable = re.fullmatch(
                r"SET\s+(@[A-Za-z_]\w*)\s*(?:=|:=)\s*(.+)", statement, re.I | re.S
            )
            if user_variable:
                changes["USER_VAR:" + user_variable[1]] = _entry(
                    span, "observed", digest=_digest(user_variable[2].strip())
                )
                continue
        match = re.fullmatch(
            rf"SET\s+(?:SESSION\s+)?({options})\s*(?:=|TO)\s*(.+)", statement, re.I | re.S
        )
        if match:
            option, value = match[1].upper(), match[2].strip()
            if value.upper() == "DEFAULT":
                changes.pop(option, None)
            else:
                changes[option] = _entry(span, "observed", digest=_digest(value))
    span.data["session_options_observed"] = {k: v["value"] for k, v in changes.items()}
