"""Secret redaction – on by default, applied before anything reaches storage.

What gets masked (replaced by :data:`MASK`):

* **headers** whose name is in ``Settings.redact_headers`` (case-insensitive) or matches
  ``Settings.redact_keys`` (``X-Auth-Token``, ``X-CSRFToken``, ``X-Session-Id``…);
* **query string, form, multipart and JSON body keys** matching any ``Settings.redact_keys``
  regex, at any nesting depth (multipart file parts become ``[file: name, N bytes]``); any
  other text request body goes through the free-text rule;
* **URLs** (outbound HTTP spans): the same key rule on their query string, and the
  password of ``scheme://user:password@host``;
* **free text** (captured log messages, exception messages and tracebacks, exception
  locals, non-JSON request bodies): ``key=value``, ``key: value``, ``"key": "value"`` and
  ``<key>value</key>`` whose key matches ``redact_keys`` have the value masked, URL-looking
  tokens go through :meth:`Redactor.url`, and JWTs and Luhn-valid card numbers are masked
  (:meth:`Redactor.text`);
* **SQL parameters** bound to a column whose name matches ``redact_keys``
  (``… SET "password" = %s``, ``INSERT … ("token") VALUES (%s)``, ``password = crypt(%s)``)
  or to a table whose name does (``"authtoken_token"."key" = %s``);
* **cache keys** after a session-store prefix or a secret-looking part
  (:meth:`Redactor.cache_key`);
* **path segments** bound to a secret-looking route parameter, or, for an unresolved route,
  long high-entropy segments (:meth:`Redactor.path`).

Bodies are capped at ``Settings.max_body_bytes`` (``body_truncated`` says so) and binary
payloads are replaced by a size marker. Everything returned is JSON-safe.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from functools import lru_cache
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from .conf import Settings

__all__ = ["MASK", "TOKEN_PATH_PARAMS", "Redactor", "json_safe"]

MASK = "[redacted]"
_MAX_PARAM_CHARS = 500

#: ``password=hunter2``, ``"token": "abc"``, ``Authorization: Bearer abc`` inside free text.
#: An unquoted value also stops at ``= ? & :``, so a harmless ``url=…?token=x``,
#: ``Cookie: sessionid=x`` or ``user=bob:password=x`` cannot swallow the secret pair that
#: follows; a *secret* key's value is then extended by :data:`_SECRET_TAIL`.
#: (The leading look-behind only skips attempts from inside a word: the leftmost match always
#: starts at the word's first character, and it keeps the scan linear.)
_TEXT_PAIR = re.compile(
    r"""(?<![\w.\-])(?P<q>["']?)(?P<key>[A-Za-z_][\w.\-]*)(?P=q)(?P<sep>\s*[:=]\s*)"""
    r"""(?P<val>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'"""
    r"""|(?:(?:Bearer|Basic|Token|Digest)\s+)?[^\s,;&)}\]"'=?:]+)""",
    re.IGNORECASE,
)
_SECRET_TAIL = re.compile(r"""[^\s,;&)}\]"']*""")
#: ``scheme://…`` inside free text (run through :meth:`Redactor.url` for userinfo/query).
_URL_TOKEN = re.compile(r"""\b[A-Za-z][A-Za-z0-9+.\-]*://[^\s"'<>`]+""")
#: The password of ``scheme://user:password@host``, percent-encoded so the URL still parses.
_USERINFO_MASK = "%5Bredacted%5D"
#: ``<password>x</password>`` (XML bodies, SOAP faults quoted in exception messages).
_XML_ELEMENT = re.compile(r"<(?P<tag>[A-Za-z_][\w:.\-]*)(?:\s[^<>]*)?>(?P<val>[^<]*)</(?P=tag)\s*>")
#: A JSON Web Token (``header.payload.signature``; both JSON parts start with ``{"``).
_JWT = re.compile(r"\beyJ[\w-]{4,}\.eyJ[\w-]{4,}\.[\w-]*")
#: 13–19 digits, optionally grouped by single spaces or dashes; kept when not Luhn-valid.
_CARD = re.compile(r"(?<![\w.\-])\d(?:[ \-]?\d){12,18}(?![\w])")

#: Cache-key prefixes of server-side session stores: the rest of the key is the session id.
_SESSION_KEY = re.compile(
    r"django\.contrib\.sessions\.cache(?:d_db)?|(?<![A-Za-z0-9])(?:session|sess):"
)
_KEY_SEPARATORS = re.compile(r"[:._\-/|=#]*")

#: Route parameters whose value is a credential whatever ``redact_keys`` says.
TOKEN_PATH_PARAMS = frozenset({"token", "uidb64", "key", "signature", "code"})
#: A path segment that may be a token (unresolved routes only, see :meth:`Redactor.path`).
_TOKENISH = re.compile(r"[A-Za-z0-9_\-.~+=%]{20,}")

_BOUNDARY = re.compile(r"""boundary\s*=\s*(?:"([^"]+)"|([^\s;]+))""", re.IGNORECASE)
_PART_NAME = re.compile(r"""(?<![\w*])name\s*=\s*(?:"([^"]*)"|([^\s;]+))""", re.IGNORECASE)
_PART_FILE = re.compile(r"""filename\*?\s*=\s*(?:"([^"]*)"|([^\s;]+))""", re.IGNORECASE)

_PLACEHOLDER = re.compile(r"%\([^)]*\)s|%s|\?|\$\d+|(?<![:\w]):[A-Za-z_]\w*")
#: ``[qualifier.]column <op> `` right before a placeholder; ``lower(col) = `` included.
_COLUMN_BEFORE = re.compile(
    r'(?:[`"\[]?(\w+)[`"\]]?\s*\.\s*)?[`"\[]?(\w+)[`"\]]?\)?\s*'
    r"(?:=|<>|!=|<=|>=|<|>|\bLIKE|\bILIKE|\bIN\s*\()\s*\(?\s*$",
    re.IGNORECASE,
)
#: The tail ``func(`` / ``func(a, `` of the text before a placeholder passed to a call.
_CALL_TAIL = re.compile(r"\b\w+\s*\((?:[^()]*,)?\s*$")
_IN_LIST = re.compile(r"\bIN\s*\([^()]*,\s*$", re.IGNORECASE)
_SQL_IDENTIFIER = r'(?:`[^`]+`|"[^"]+"|\[[^\]]+\]|\w+)'
_SQL_TABLE = rf"{_SQL_IDENTIFIER}(?:\s*\.\s*{_SQL_IDENTIFIER})*"
_INSERT = re.compile(
    rf"\bINSERT\s+INTO\s+(?P<table>{_SQL_TABLE})\s*\((?P<cols>[^)]*)\)\s*VALUES\s*"
    r"(?P<rows>.*?)(?=\bON\s+(?:CONFLICT|DUPLICATE)\b|\bRETURNING\b|$)",
    re.IGNORECASE | re.DOTALL,
)
_TABLE_REF = re.compile(
    rf"\b(?:FROM|JOIN|UPDATE|INTO)\s+(?:ONLY\s+)?(?P<table>{_SQL_TABLE})",
    re.IGNORECASE,
)
_TABLE_ALIAS = re.compile(rf"\s+(?:AS\s+)?(?P<alias>{_SQL_IDENTIFIER})", re.IGNORECASE)
_NOT_ALIAS = frozenset(
    "where set on inner left right full cross join values group order limit using natural "
    "outer union as select default having window returning for offset fetch except "
    "intersect lateral straight_join partition".split()
)
#: Django's ``django.contrib.auth`` app label: ``auth_user`` is not a secret table.
_AUTH_APP_PREFIX = re.compile(r"^auth_", re.IGNORECASE)


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


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _mask_card(m: re.Match[str]) -> str:
    text = m.group(0)
    digits = text.replace(" ", "").replace("-", "")
    return MASK if 13 <= len(digits) <= 19 and _luhn(digits) else text


def _looks_like_token(segment: str) -> bool:
    """A long segment of token characters that mixes letters and digits a lot (random ids,
    signed tokens, hashes); slugs like ``introducing-our-2024-roadmap`` do not."""
    if len(segment) < 20 or not _TOKENISH.fullmatch(segment):
        return False
    switches = 0
    prev = ""
    for ch in segment:
        kind = "d" if ch.isdigit() else "a" if ch.isalpha() else ""
        if kind:
            if prev and kind != prev:
                switches += 1
            prev = kind
    return switches >= 4


class Redactor:
    """Applies the redaction rules of one :class:`~redline_core.conf.Settings`."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self._headers = frozenset(h.lower() for h in self.settings.redact_headers)
        from .privacy import profile_keys

        self._keys = [
            re.compile(p)
            for p in (*self.settings.redact_keys, *profile_keys(self.settings.redact_profiles))
        ]
        # Column and field names repeat endlessly (every query binds the same few), so the
        # regex scan is memoised per distinct name.
        self.is_secret_key = lru_cache(maxsize=2048)(self._is_secret_key)
        # Which placeholders of a statement are secret: memoised per SQL text.
        self._sql_mask = lru_cache(maxsize=1024)(self._sql_mask_uncached)

    # keys ------------------------------------------------------------------
    def _is_secret_key(self, key: str) -> bool:
        return any(p.search(key) for p in self._keys)

    def _is_secret_table(self, table: str) -> bool:
        from .privacy import profile_tables

        basename = table.rsplit(".", 1)[-1]
        return self.is_secret_key(_AUTH_APP_PREFIX.sub("", basename)) or any(
            re.search(pattern, name)
            for pattern in (
                *self.settings.redact_tables,
                *profile_tables(self.settings.redact_profiles),
            )
            for name in (table, basename)
        )

    def text(self, text: str) -> str:
        """``text`` with secrets masked.

        URL-looking tokens go through :meth:`url` first (userinfo password, query keys);
        then the values of secret-looking ``key=value`` / ``key: value`` / ``"key": "v"``
        pairs and ``<key>value</key>`` elements, JWTs and Luhn-valid card numbers
        (13–19 digits, optionally grouped by spaces or dashes) are masked.
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
        if pos:
            out.append(text[pos:])
            text = "".join(out)
        if "</" in text:
            text = _XML_ELEMENT.sub(self._mask_element, text)
        if "eyJ" in text:
            text = _JWT.sub(MASK, text)
        return _CARD.sub(_mask_card, text)

    def _mask_element(self, m: re.Match[str]) -> str:
        tag = m.group("tag").rpartition(":")[2]
        if not m.group("val") or not self.is_secret_key(tag):
            return m.group(0)
        return m.group(0)[: m.start("val") - m.start()] + MASK + f"</{m.group('tag')}>"

    def headers(self, headers: Mapping[str, Any] | Iterable[tuple[Any, Any]] | None) -> dict:
        """Header dict with sensitive values masked. Accepts ASGI ``[(b"k", b"v")]`` too.

        A header is masked when its name is in ``redact_headers`` or matches ``redact_keys``.
        """
        out: dict[str, str] = {}
        if not headers:
            return out
        items = headers.items() if isinstance(headers, Mapping) else headers
        for key, value in items:
            k = key.decode("latin-1") if isinstance(key, bytes) else str(key)
            v = value.decode("latin-1") if isinstance(value, bytes) else str(value)
            if k.lower() in self._headers or self.is_secret_key(k):
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

    # cache keys and paths ----------------------------------------------------
    def cache_key(self, key: str) -> str:
        """``key`` with the part after a session-store prefix or a secret-looking word masked.

        ``django.contrib.sessions.cache<sid>`` → ``django.contrib.sessions.cache[redacted]``
        (also ``…cached_db``, ``session:`` and ``sess:``); ``user:1:api_key:<k>`` →
        ``user:1:api_key:[redacted]``. The prefix is kept so every session key still
        groups into one cache-efficiency pattern.
        """
        if not key:
            return key
        m = _SESSION_KEY.search(key)
        if m is not None:
            return key[: m.end()] + MASK if m.end() < len(key) else key
        end = -1
        for p in self._keys:
            hit = p.search(key)
            if hit is not None and (end < 0 or hit.end() < end):
                end = hit.end()
        if end < 0:
            return key
        sep = _KEY_SEPARATORS.match(key, end)
        end = sep.end() if sep is not None else end
        return key[:end] + MASK if end < len(key) else key

    def path(self, path: str, params: Mapping[str, Any] | None, *, resolved: bool) -> str:
        """``path`` with secret route-parameter values replaced by :data:`MASK`.

        A parameter is secret when its name matches ``redact_keys`` or is one of
        :data:`TOKEN_PATH_PARAMS` (``token``, ``uidb64``, ``key``, ``signature``, ``code``).
        For an unresolved route (``resolved=False``, e.g. a 404) every segment of at least
        20 token characters that mixes letters and digits is masked instead.
        """
        if params:
            values: list[str] = []
            for name, value in params.items():
                if value is None or isinstance(value, bool):
                    continue
                n = str(name)
                if n.lower() in TOKEN_PATH_PARAMS or self.is_secret_key(n):
                    text = str(value)
                    if text:
                        values.append(text)
            return _mask_path_values(path, values) if values else path
        if resolved or len(path) < 21:
            return path
        segments = path.split("/")
        changed = False
        for i, seg in enumerate(segments):
            if _looks_like_token(seg):
                segments[i] = MASK
                changed = True
        return "/".join(segments) if changed else path

    # bodies ----------------------------------------------------------------
    def body(
        self, body: bytes | str | None, content_type: str | None = None, *, free_text: bool = True
    ) -> tuple[str | None, bool]:
        """``(text, truncated)`` for a request/response body.

        JSON, form and multipart bodies have secret keys masked *before* truncation, so a
        cut never exposes half a secret; multipart file parts become ``[file: name, N
        bytes]``. Text that is not JSON (or does not parse) goes through :meth:`text` when
        ``free_text`` is true. Undecodable bytes become ``"<N bytes binary>"``.
        """
        if body is None:
            return None, False
        raw = body.encode("utf-8") if isinstance(body, str) else bytes(body)
        if not raw:
            return "", False
        ctype = (content_type or "").split(";")[0].strip().lower()
        if ctype.startswith("multipart/"):
            text = self._multipart(raw, content_type or "")
        else:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return f"<{len(raw)} bytes binary>", False
            if ctype == "application/x-www-form-urlencoded":
                pairs = parse_qsl(text, keep_blank_values=True)
                text = urlencode([(k, MASK if self.is_secret_key(k) else v) for k, v in pairs])
            elif ctype.endswith("json") or text.lstrip()[:1] in ("{", "["):
                try:
                    text = json.dumps(self._json_body(json.loads(text)), ensure_ascii=False)
                except (ValueError, RecursionError):
                    if free_text:
                        text = self.text(text)
            elif free_text:
                text = self.text(text)
        limit = self.settings.max_body_bytes
        encoded = text.encode("utf-8")
        if len(encoded) > limit:
            return encoded[:limit].decode("utf-8", errors="ignore"), True
        return text, False

    def _json_body(self, data: Any) -> Any:
        """``mapping(data)``, plus the free-text rule on a GraphQL ``query`` string (inline
        literals such as ``login(password: "x")``); ``variables`` are masked by key."""
        masked = self.mapping(data)
        ops = masked if isinstance(masked, list) else [masked]
        for op in ops:
            if isinstance(op, dict) and isinstance(op.get("query"), str):
                op["query"] = self.text(op["query"])
        return masked

    def _multipart(self, raw: bytes, content_type: str) -> str:
        """A ``multipart/form-data`` body as JSON ``{field: value}``: secret fields masked,
        file parts replaced by ``[file: name, N bytes]``. Never the raw body."""
        m = _BOUNDARY.search(content_type)
        boundary = (m.group(1) or m.group(2)) if m else None
        if not boundary:
            return f"<multipart body, {len(raw)} bytes>"
        out: dict[str, Any] = {}
        for part in raw.split(b"--" + boundary.encode("latin-1"))[1:]:
            if part.startswith(b"--"):
                break  # closing delimiter
            head, sep, value = part.lstrip(b"\r\n").partition(b"\r\n\r\n")
            if not sep:
                continue  # cut inside the part headers
            headers = head.decode("latin-1")
            name_m = _PART_NAME.search(headers)
            if name_m is None:
                continue
            name = name_m.group(1) if name_m.group(1) is not None else name_m.group(2)
            if value.endswith(b"\r\n"):
                value = value[:-2]
            file_m = _PART_FILE.search(headers)
            shown: Any
            if file_m is not None:
                filename = file_m.group(1) if file_m.group(1) is not None else file_m.group(2)
                shown = f"[file: {filename[:100]}, {len(value)} bytes]"
            elif self.is_secret_key(name):
                shown = MASK
            else:
                try:
                    shown = json_safe(self.text(value.decode("utf-8")), limit=2_000)
                except UnicodeDecodeError:
                    shown = f"<{len(value)} bytes binary>"
            if name in out:
                prev = out[name]
                out[name] = [*prev, shown] if isinstance(prev, list) else [prev, shown]
            else:
                out[name] = shown
        return json.dumps(out, ensure_ascii=False)

    # sql -------------------------------------------------------------------
    def sql_params(self, sql: str, params: Any) -> Any:
        """JSON-safe ``params`` with values bound to secret-looking columns masked.

        Positional params are matched to placeholders in order; for ``executemany`` pass
        each row separately. Named params (dicts) use both their key and the inferred column. A
        placeholder is secret when its column matches ``redact_keys``, or its table does
        (the qualifier, an alias of it, or – for an unqualified column – any table of the
        statement; the ``auth_`` prefix of Django's auth tables is ignored).
        """
        if params is None:
            return None
        if isinstance(params, Mapping):
            masked = self.mapping(params)
            tables, _ = _tables(sql)
            if any(self._is_secret_table(table) for table in tables):
                return {str(key): MASK for key in params}
            secret = self._sql_mask(sql)
            for match, hide in zip(_PLACEHOLDER.finditer(sql), secret, strict=False):
                if not hide:
                    continue
                placeholder = match.group()
                key = placeholder[2:-2] if placeholder.startswith("%(") else placeholder[1:]
                if key in masked:
                    masked[key] = MASK
            return masked
        if not isinstance(params, Sequence) or isinstance(params, (str, bytes, bytearray)):
            return json_safe(params)
        secret = self._sql_mask(sql)
        out = []
        for i, value in enumerate(params):
            if i < len(secret) and secret[i]:
                out.append(MASK)
            elif isinstance(value, Mapping):
                out.append(self.sql_params(sql, value))
            else:
                out.append(json_safe(value))
        return out

    def bounded_sql_params(self, sql: str, params: Any, *, max_bytes: int) -> tuple[Any, bool]:
        """Redact parameters after bounding their original traversal and scalar conversion.

        Only exact built-in containers and known database scalar types are inspected.
        Truncation remains explicit, including normalization before the redaction walk.
        """
        from .capture_limits import bounded_value

        if type(max_bytes) is not int or max_bytes < 4:
            raise ValueError("max_bytes must be an integer >= 4")
        fast = _fast_json_value(
            params, max_bytes=max_bytes, max_items=200, max_depth=8, max_string_bytes=2000
        )
        if fast is _NO_FAST:
            normal, input_cut = _bounded_sql_input(params, max_bytes=max_bytes)
        else:
            normal, input_cut = fast, False
        redacted = self.sql_params(sql, normal)
        fast_output = _fast_json_value(
            redacted, max_bytes=max_bytes, max_items=200, max_depth=8, max_string_bytes=500
        )
        if fast_output is not _NO_FAST:
            return fast_output, input_cut
        safe, output_cut = bounded_value(
            redacted, max_bytes=max_bytes, max_items=200, max_depth=8, max_string_bytes=500
        )
        return safe, input_cut or output_cut

    def _sql_mask_uncached(self, sql: str) -> tuple[bool, ...]:
        return tuple(
            (column is not None and self.is_secret_key(column))
            or any(self._is_secret_table(t) for t in tables)
            for column, tables in _param_slots(sql)
        )

    @staticmethod
    def _param_columns(sql: str) -> tuple[str | None, ...]:
        """Column each placeholder of ``sql`` is bound to (``None`` when unknown)."""
        return tuple(column for column, _ in _param_slots(sql))


_NO_FAST: Any = object()


def _fast_json_value(
    value: Any,
    *,
    max_bytes: int,
    max_items: int,
    max_depth: int,
    max_string_bytes: int,
) -> Any:
    """The value itself when it is already small, exact-builtin and JSON-safe.

    This is the common path for ``{"id": 7}`` and avoids multiple bounded traversals.
    Anything unusual returns ``_NO_FAST`` so the bounding/redaction path handles it.
    """
    remaining = [max_items]

    def visit(item: Any, depth: int) -> bool:
        kind = type(item)
        if kind is str:
            return len(item) <= max_string_bytes
        if kind in (type(None), bool):
            return True
        if kind is int:
            return item.bit_length() <= 64
        if kind is float:
            return item == item and item not in (float("inf"), float("-inf"))
        if kind not in (dict, list, tuple) or depth >= max_depth:
            return False
        if remaining[0] < 1:
            return False
        for key, child in item.items() if kind is dict else enumerate(item):
            if kind is dict and (type(key) is not str or len(key) > max_string_bytes):
                return False
            remaining[0] -= 1
            if remaining[0] < 0 or not visit(child, depth + 1):
                return False
        return True

    if not visit(value, 0):
        return _NO_FAST
    import json

    try:
        encoded = json.dumps(
            value, ensure_ascii=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        return _NO_FAST
    return value if len(encoded) <= max_bytes else _NO_FAST


def _bounded_sql_input(value: Any, *, max_bytes: int) -> tuple[Any, bool]:
    """Normalize a bounded prefix without calling user-defined conversion methods."""
    import datetime
    from decimal import Decimal
    from uuid import UUID
    from zoneinfo import ZoneInfo

    from .capture_limits import bounded_value

    remaining_bytes = max_bytes
    remaining_items = 200
    active: set[int] = set()
    truncated = False

    def visit(item, depth):
        nonlocal remaining_bytes, remaining_items, truncated
        kind = type(item)
        if kind in (dict, list, tuple):
            if depth >= 8 or id(item) in active or remaining_bytes < 2:
                truncated = True
                return None
            active.add(id(item))
            remaining_bytes -= 2
            result = {} if kind is dict else []
            try:
                entries = item.items() if kind is dict else enumerate(item)
                for key, child in entries:
                    if remaining_items <= 0 or remaining_bytes < 5:
                        truncated = True
                        break
                    remaining_items -= 1
                    if kind is dict:
                        # Omit oversized keys rather than changing their privacy meaning.
                        if type(key) is not str or len(key) > min(2000, remaining_bytes):
                            truncated = True
                            continue
                        cost = len(json.dumps(key, ensure_ascii=True)) + 2
                        if cost + 4 > remaining_bytes:
                            truncated = True
                            break
                        remaining_bytes -= cost
                        result[key] = visit(child, depth + 1)
                    else:
                        remaining_bytes -= 1
                        result.append(visit(child, depth + 1))
                return result
            except RuntimeError:
                truncated = True
                return result
            finally:
                active.remove(id(item))
        if kind in (datetime.datetime, datetime.time):
            # A custom tzinfo could run application code while formatting a timestamp.
            if item.tzinfo is not None and type(item.tzinfo) not in (datetime.timezone, ZoneInfo):
                truncated = True
                item = None
            elif kind is datetime.datetime:
                item = datetime.datetime.isoformat(item, sep=" ")
            else:
                item = datetime.time.isoformat(item)
        elif kind is datetime.date:
            item = datetime.date.isoformat(item)
        elif kind is Decimal:
            # Decimal strings can contain arbitrarily many coefficient digits. Reject
            # large allocations before formatting; do not materialize as_tuple().
            if Decimal.__sizeof__(item) > max(256, remaining_bytes):
                truncated = True
                item = None
            else:
                item = Decimal.__str__(item)
        elif kind is UUID:
            item = UUID.__str__(item)
        budget = max(4, remaining_bytes)
        safe, cut = bounded_value(item, max_bytes=budget, max_string_bytes=500)
        remaining_bytes -= len(json.dumps(safe, ensure_ascii=True, separators=(",", ":")))
        truncated |= cut
        return safe

    result = visit(value, 0)
    return result, truncated


def _mask_path_values(path: str, values: list[str]) -> str:
    segments = path.split("/")
    changed = False
    for value in values:
        hit = False
        for i, seg in enumerate(segments):
            if seg == value or (("%" in seg) and unquote(seg) == value):
                segments[i] = MASK
                hit = changed = True
        if not hit and "/" in value:  # a ``path:`` converter spanning several segments
            joined = "/".join(segments)
            for form in (value, quote(value)):
                if form in joined:
                    segments = joined.replace(form, MASK).split("/")
                    changed = True
                    break
    return "/".join(segments) if changed else path


def _column_at(sql: str, pos: int) -> tuple[str | None, str | None]:
    """``(column, qualifier)`` the placeholder at ``pos`` is compared or assigned to.

    A placeholder passed to a function (``crypt(%s, salt)``, ``lower(%s)``) is attributed
    to the column before the call.
    """
    window = sql[max(0, pos - 160) : pos]
    if "," in window:  # every item of ``col IN (%s, %s, …)`` belongs to ``col``
        window = _IN_LIST.sub("IN (", window)
    for _ in range(3):
        m = _COLUMN_BEFORE.search(window)
        if m is not None:
            return m.group(2), m.group(1)
        call = _CALL_TAIL.search(window)
        if call is None:
            break
        window = window[: call.start()]
    return None, None


def _table_name(value: str) -> str:
    """Keep schema/catalog components while removing identifier quoting and spacing."""
    return ".".join(part.strip('"`[]') for part in re.findall(_SQL_IDENTIFIER, value))


def _tables(sql: str) -> tuple[tuple[str, ...], dict[str, str]]:
    """Tables named after ``FROM``/``JOIN``/``UPDATE``/``INTO`` and their aliases."""
    tables: list[str] = []
    aliases: dict[str, str] = {}
    for m in _TABLE_REF.finditer(sql):
        table = _table_name(m.group("table"))
        if table.lower() in _NOT_ALIAS:
            continue
        if table not in tables:
            tables.append(table)
        aliases[table.rsplit(".", 1)[-1].lower()] = table
        alias_match = _TABLE_ALIAS.match(sql, m.end())
        if alias_match:
            alias = _table_name(alias_match.group("alias"))
            if alias.lower() not in _NOT_ALIAS:
                aliases[alias.lower()] = table
    return tuple(tables), aliases


@lru_cache(maxsize=1024)
def _param_slots(sql: str) -> tuple[tuple[str | None, tuple[str, ...]], ...]:
    """For each placeholder of ``sql``: its column (or ``None``) and the table(s) it may
    belong to. Memoised as SQL text repeats."""
    tables, aliases = _tables(sql)

    def slot(pos: int) -> tuple[str | None, tuple[str, ...]]:
        column, qualifier = _column_at(sql, pos)
        if qualifier:
            return column, (aliases.get(qualifier.lower(), qualifier),)
        return column, tables

    insert = _INSERT.search(sql)
    if insert is None:
        return tuple(slot(m.start()) for m in _PLACEHOLDER.finditer(sql))
    table = _table_name(insert.group("table"))
    cols = [c.strip().split(".")[-1].strip('"`[]') for c in insert.group("cols").split(",")]
    n = len(_PLACEHOLDER.findall(insert.group("rows")))
    head = [slot(m.start()) for m in _PLACEHOLDER.finditer(sql, 0, insert.start("rows"))]
    rows = [(cols[i % len(cols)] if cols else None, (table,)) for i in range(n)]
    tail = [slot(m.start()) for m in _PLACEHOLDER.finditer(sql, insert.end("rows"))]
    return tuple(head + rows + tail)
