"""Secret redaction – on by default, applied before anything reaches storage.

What gets masked (replaced by :data:`MASK`):

* **headers** whose name is in ``Settings.redact_headers`` (case-insensitive);
* **query string, form and JSON body keys** matching any ``Settings.redact_keys`` regex,
  at any nesting depth;
* **URLs** (outbound HTTP spans): the same key rule on their query string;
* **free text** (captured log messages): ``key=value`` and ``"key": "value"`` pairs whose
  key matches ``redact_keys`` have the value masked (:meth:`Redactor.text`);
* **SQL parameters** bound to a column whose name matches ``redact_keys``
  (``… SET "password" = %s`` or ``INSERT … ("token") VALUES (%s)``).

Bodies are capped at ``Settings.max_body_bytes`` (``body_truncated`` says so) and binary
payloads are replaced by a size marker. Everything returned is JSON-safe.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from functools import lru_cache
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .conf import Settings

__all__ = ["MASK", "Redactor", "json_safe"]

MASK = "[redacted]"
_MAX_PARAM_CHARS = 500

#: ``password=hunter2``, ``"token": "abc"``, ``Authorization: Bearer abc`` inside free text.
_TEXT_PAIR = re.compile(
    r"""(?P<q>["']?)(?P<key>[A-Za-z_][\w.\-]*)(?P=q)(?P<sep>\s*[:=]\s*)"""
    r"""(?P<val>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'"""
    r"""|(?:(?:Bearer|Basic|Token)\s+)?[^\s,;&)}\]"']+)""",
    re.IGNORECASE,
)

_PLACEHOLDER = re.compile(r"%\([^)]*\)s|%s|\?|\$\d+|(?<![:\w]):[A-Za-z_]\w*")
_COLUMN_BEFORE = re.compile(
    r'(?:"?(\w+)"?\s*\.\s*)?[`"\[]?(\w+)[`"\]]?\s*(?:=|<>|!=|<=|>=|<|>|\bLIKE|\bILIKE|\bIN\s*\()\s*\(?\s*$',
    re.IGNORECASE,
)
_INSERT = re.compile(
    r"\bINSERT\s+INTO\s+\S+\s*\((?P<cols>[^)]*)\)\s*VALUES\s*(?P<rows>.*)$",
    re.IGNORECASE | re.DOTALL,
)


def json_safe(value: Any, *, limit: int = _MAX_PARAM_CHARS) -> Any:
    """A JSON-serialisable rendition of a driver parameter."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    if isinstance(value, Mapping):
        return {str(k): json_safe(v, limit=limit) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(v, limit=limit) for v in value]
    text = str(value)  # dates, decimals, UUIDs, enums…
    return text if len(text) <= limit else text[:limit] + "…"


class Redactor:
    """Applies the redaction rules of one :class:`~redline_core.conf.Settings`."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self._headers = frozenset(h.lower() for h in self.settings.redact_headers)
        self._keys = [re.compile(p) for p in self.settings.redact_keys]
        # Column and field names repeat endlessly (every query binds the same few), so the
        # regex scan is memoised per distinct name.
        self.is_secret_key = lru_cache(maxsize=2048)(self._is_secret_key)

    # keys ------------------------------------------------------------------
    def _is_secret_key(self, key: str) -> bool:
        return any(p.search(key) for p in self._keys)

    def text(self, text: str) -> str:
        """``text`` with the values of secret-looking ``key=value`` / ``"key": "v"`` masked."""

        def mask(m: re.Match[str]) -> str:
            if not self.is_secret_key(m.group("key")):
                return m.group(0)
            val = m.group("val")
            quote = val[0] if len(val) > 1 and val[0] in "\"'" and val[-1] == val[0] else ""
            q = m.group("q")
            return f"{q}{m.group('key')}{q}{m.group('sep')}{quote}{MASK}{quote}"

        return _TEXT_PAIR.sub(mask, text)

    def headers(self, headers: Mapping[str, Any] | Iterable[tuple[Any, Any]] | None) -> dict:
        """Header dict with sensitive values masked. Accepts ASGI ``[(b"k", b"v")]`` too."""
        out: dict[str, str] = {}
        if not headers:
            return out
        items = headers.items() if isinstance(headers, Mapping) else headers
        for key, value in items:
            k = key.decode("latin-1") if isinstance(key, bytes) else str(key)
            v = value.decode("latin-1") if isinstance(value, bytes) else str(value)
            if k.lower() in self._headers:
                v = MASK
            if k in out:  # repeated header (e.g. several Set-Cookie)
                out[k] = f"{out[k]}, {v}"
            else:
                out[k] = v
        return out

    def mapping(self, data: Any) -> Any:
        """Recursively mask values whose key matches ``redact_keys``."""
        if isinstance(data, Mapping):
            return {
                str(k): MASK if self.is_secret_key(str(k)) else self.mapping(v)
                for k, v in data.items()
            }
        if isinstance(data, (list, tuple)):
            return [self.mapping(v) for v in data]
        return json_safe(data)

    def query(self, query: Mapping[str, Any] | str | bytes | None) -> dict[str, Any]:
        """A query-string mapping (or raw ``a=1&b=2``) with secrets masked.

        Repeated keys become lists.
        """
        if not query:
            return {}
        if isinstance(query, (str, bytes)):
            text = query.decode("latin-1") if isinstance(query, bytes) else query
            pairs: dict[str, Any] = {}
            for k, v in parse_qsl(text, keep_blank_values=True):
                if k in pairs:
                    prev = pairs[k]
                    pairs[k] = [*prev, v] if isinstance(prev, list) else [prev, v]
                else:
                    pairs[k] = v
            query = pairs
        return self.mapping(dict(query))

    def url(self, url: str) -> str:
        """``url`` with secret query-string values masked."""
        try:
            parts = urlsplit(url)
        except ValueError:
            return url
        if not parts.query:
            return url
        pairs = [
            (k, MASK if self.is_secret_key(k) else v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
        ]
        return urlunsplit(parts._replace(query=urlencode(pairs, safe="[]")))

    # bodies ----------------------------------------------------------------
    def body(
        self, body: bytes | str | None, content_type: str | None = None
    ) -> tuple[str | None, bool]:
        """``(text, truncated)`` for a request/response body.

        JSON and form bodies have secret keys masked *before* truncation, so a cut never
        exposes half a secret. Undecodable bytes become ``"<N bytes binary>"``.
        """
        if body is None:
            return None, False
        raw = body.encode("utf-8") if isinstance(body, str) else bytes(body)
        if not raw:
            return "", False
        ctype = (content_type or "").split(";")[0].strip().lower()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return f"<{len(raw)} bytes binary>", False
        if ctype.endswith("json") or ctype.endswith("+json") or (not ctype and text[:1] in "{["):
            try:
                text = json.dumps(self.mapping(json.loads(text)), ensure_ascii=False)
            except (ValueError, RecursionError):
                pass
        elif ctype == "application/x-www-form-urlencoded":
            pairs = parse_qsl(text, keep_blank_values=True)
            text = urlencode([(k, MASK if self.is_secret_key(k) else v) for k, v in pairs])
        limit = self.settings.max_body_bytes
        encoded = text.encode("utf-8")
        if len(encoded) > limit:
            return encoded[:limit].decode("utf-8", errors="ignore"), True
        return text, False

    # sql -------------------------------------------------------------------
    def sql_params(self, sql: str, params: Any) -> Any:
        """JSON-safe ``params`` with values bound to secret-looking columns masked.

        Positional params are matched to placeholders in order; for ``executemany`` pass
        the list of rows and each row is handled. Named params (dicts) use their key.
        """
        if params is None:
            return None
        if isinstance(params, Mapping):
            return self.mapping(params)
        if not isinstance(params, Sequence) or isinstance(params, (str, bytes, bytearray)):
            return json_safe(params)
        names = self._param_columns(sql)
        out = []
        for i, value in enumerate(params):
            column = names[i] if i < len(names) else None
            if column is not None and self.is_secret_key(column):
                out.append(MASK)
            elif isinstance(value, Mapping):
                out.append(self.mapping(value))
            else:
                out.append(json_safe(value))
        return out

    @staticmethod
    def _param_columns(sql: str) -> tuple[str | None, ...]:
        return _param_columns(sql)


def _column_at(sql: str, pos: int) -> str | None:
    m = _COLUMN_BEFORE.search(sql[max(0, pos - 120) : pos])
    return m.group(2) if m else None


@lru_cache(maxsize=1024)
def _param_columns(sql: str) -> tuple[str | None, ...]:
    """Column each placeholder of ``sql`` is bound to; memoised as SQL text repeats."""
    insert = _INSERT.search(sql)
    if insert:
        cols = [c.strip().strip('"`[]') for c in insert.group("cols").split(",")]
        n = len(_PLACEHOLDER.findall(insert.group("rows")))
        prefix = sql[: insert.start("rows")]
        head = [_column_at(prefix, m.start()) for m in _PLACEHOLDER.finditer(prefix)]
        return tuple(head + [cols[i % len(cols)] if cols else None for i in range(n)])
    return tuple(_column_at(sql, m.start()) for m in _PLACEHOLDER.finditer(sql))
