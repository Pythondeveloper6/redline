"""Secret redaction – on by default, applied before anything reaches storage.

What gets masked (replaced by :data:`MASK`):

* **headers** whose name is in ``Settings.redact_headers`` (case-insensitive);
* **query string, form and JSON body keys** matching any ``Settings.redact_keys`` regex,
  at any nesting depth;
* **URLs** (outbound HTTP spans): the same key rule on their query string, and the
  password of ``scheme://user:password@host``;
* **free text** (captured log messages, exception locals): ``key=value``, ``key: value``
  and ``"key": "value"`` pairs whose key matches ``redact_keys`` have the value masked,
  and URL-looking tokens go through :meth:`Redactor.url` (:meth:`Redactor.text`);
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
#: An unquoted value also stops at ``= ? & :``, so a harmless ``url=…?token=x``,
#: ``Cookie: sessionid=x`` or ``user=bob:password=x`` cannot swallow the secret pair that
#: follows; a *secret* key's value is then extended by :data:`_SECRET_TAIL`.
_TEXT_PAIR = re.compile(
    r"""(?P<q>["']?)(?P<key>[A-Za-z_][\w.\-]*)(?P=q)(?P<sep>\s*[:=]\s*)"""
    r"""(?P<val>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'"""
    r"""|(?:(?:Bearer|Basic|Token|Digest)\s+)?[^\s,;&)}\]"'=?:]+)""",
    re.IGNORECASE,
)
_SECRET_TAIL = re.compile(r"""[^\s,;&)}\]"']*""")
#: ``scheme://…`` inside free text (run through :meth:`Redactor.url` for userinfo/query).
_URL_TOKEN = re.compile(r"""\b[A-Za-z][A-Za-z0-9+.\-]*://[^\s"'<>`]+""")
#: The password of ``scheme://user:password@host``, percent-encoded so the URL still parses.
_USERINFO_MASK = "%5Bredacted%5D"

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
        """``text`` with the values of secret-looking ``key=value`` / ``"key": "v"`` masked.

        URL-looking tokens go through :meth:`url` first (userinfo password, query keys).
        """
        if not text:
            return text
        if "://" in text:
            text = _URL_TOKEN.sub(lambda m: self.url(m.group(0)), text)
        out: list[str] = []
        pos = 0
        while True:
            m = _TEXT_PAIR.search(text, pos)
            if m is None:
                break
            val_start = m.start("val")
            if not self.is_secret_key(m.group("key")) or text.startswith(MASK, val_start):
                # The value may itself start the next pair (``Cookie: sessionid=x``).
                out.append(text[pos:val_start])
                pos = val_start
                continue
            val = m.group("val")
            end = m.end()
            if val[0] in "\"'" and len(val) > 1 and val[-1] == val[0]:
                masked = f"{val[0]}{MASK}{val[0]}"
            else:
                masked = MASK
                tail = _SECRET_TAIL.match(text, end)
                if tail is not None:
                    end = tail.end()
            out.append(text[pos:val_start])
            out.append(masked)
            pos = end
        out.append(text[pos:])
        return "".join(out)

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
        """``url`` with a userinfo password and secret query-string values masked.

        ``https://bob:pw@host`` becomes ``https://bob:%5Bredacted%5D@host`` (the mask is
        percent-encoded so the result still parses). A URL with nothing to mask is
        returned unchanged.
        """
        try:
            parts = urlsplit(url)
        except ValueError:
            return url
        netloc, query = parts.netloc, parts.query
        changed = False
        if "@" in netloc:
            userinfo, _, host = netloc.rpartition("@")
            user, colon, password = userinfo.partition(":")
            if colon and password != _USERINFO_MASK:
                netloc = f"{user}:{_USERINFO_MASK}@{host}"
                changed = True
        if query:
            pairs = parse_qsl(query, keep_blank_values=True)
            if any(self.is_secret_key(k) for k, _ in pairs):
                masked = [(k, MASK if self.is_secret_key(k) else v) for k, v in pairs]
                query = urlencode(masked, safe="[]")
                changed = True
        if not changed:
            return url
        return urlunsplit(parts._replace(netloc=netloc, query=query))

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
