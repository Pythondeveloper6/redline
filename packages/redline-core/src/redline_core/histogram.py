"""A small mergeable latency histogram (``CONTRACT.md`` section 11).

Values are milliseconds. Buckets are logarithmic with a growth factor of 1.02 between
bucket edges, so a percentile read from a bucket's geometric midpoint is within about 1 %
of the true value. The range is 1 microsecond (``0.001`` ms) to 1 hour (``3_600_000`` ms);
values outside it are clamped, ``NaN`` is ignored. Only non-empty buckets are stored, so a
histogram of a typical endpoint is a few dozen counters, and two histograms merge by
adding counters (merging is exact, commutative and associative).

Exact ``min``, ``max`` and ``sum`` are tracked alongside, so ``percentile(0)`` and
``percentile(100)`` are exact and no estimate leaves ``[min, max]``.

``to_bytes()`` is a versioned, zlib-compressed sparse encoding (varint deltas of bucket
indexes and counts); ``from_bytes()`` reads it back and rejects anything malformed with
``ValueError``. No dependencies beyond the standard library.
"""

from __future__ import annotations

import math
import struct
import zlib
from collections.abc import Iterable

__all__ = ["MAX_MS", "MIN_MS", "Histogram"]

MIN_MS = 0.001  # 1 microsecond
MAX_MS = 3_600_000.0  # 1 hour
_GROWTH = 1.02  # bucket edge ratio -> <= ~1 % relative error at the midpoint
_LOG_GROWTH = math.log(_GROWTH)
_MAGIC = b"RLH"
_VERSION = 1
_TAIL = struct.Struct("<ddd")  # min, max, sum
_MAX_BUCKET = int(math.log(MAX_MS / MIN_MS) / _LOG_GROWTH)  # index of the last bucket


def _index(value: float) -> int:
    return int(math.log(value / MIN_MS) / _LOG_GROWTH)


def _midpoint(index: int) -> float:
    # geometric middle of [MIN * g**i, MIN * g**(i+1))
    return MIN_MS * _GROWTH ** (index + 0.5)


class Histogram:
    """Log-bucketed histogram of durations in milliseconds."""

    __slots__ = ("_buckets", "_count", "_max", "_min", "_sum")

    def __init__(self, values: Iterable[float] = ()) -> None:
        self._buckets: dict[int, int] = {}
        self._count = 0
        self._min = math.inf
        self._max = 0.0
        self._sum = 0.0
        if values:
            self.record_many(values)

    # recording -------------------------------------------------------------
    def record(self, ms: float, count: int = 1) -> None:
        """Add ``count`` observations of ``ms`` milliseconds (clamped to the range)."""
        if count <= 0 or ms != ms:  # NaN or nothing to add
            return
        if ms < MIN_MS:
            ms = MIN_MS
        elif ms > MAX_MS:
            ms = MAX_MS
        idx = _index(ms)
        buckets = self._buckets
        buckets[idx] = buckets.get(idx, 0) + count
        self._count += count
        self._sum += ms * count
        if ms < self._min:
            self._min = ms
        if ms > self._max:
            self._max = ms

    def record_many(self, values: Iterable[float]) -> None:
        for v in values:
            self.record(v)

    def merge(self, other: Histogram) -> Histogram:
        """Add ``other``'s observations to this histogram (returns ``self``)."""
        if other._count == 0:
            return self
        buckets = self._buckets
        for idx, n in other._buckets.items():
            buckets[idx] = buckets.get(idx, 0) + n
        self._count += other._count
        self._sum += other._sum
        self._min = min(self._min, other._min)
        self._max = max(self._max, other._max)
        return self

    def copy(self) -> Histogram:
        return Histogram().merge(self)

    # reading ---------------------------------------------------------------
    @property
    def count(self) -> int:
        return self._count

    @property
    def max(self) -> float:
        """The largest recorded value (exact, after clamping); ``0.0`` when empty."""
        return self._max if self._count else 0.0

    @property
    def min(self) -> float:
        """The smallest recorded value (exact, after clamping); ``0.0`` when empty."""
        return self._min if self._count else 0.0

    @property
    def total(self) -> float:
        """The sum of all recorded values in ms."""
        return self._sum

    @property
    def mean(self) -> float:
        return self._sum / self._count if self._count else 0.0

    def percentile(self, p: float) -> float:
        """The ``p``-th percentile (``0``-``100``) in ms; ``0.0`` when empty.

        Uses the nearest-rank definition on bucket midpoints, clamped to ``[min, max]``.
        """
        if not self._count:
            return 0.0
        if not 0.0 <= p <= 100.0:
            raise ValueError(f"percentile must be between 0 and 100, got {p!r}")
        if p == 0.0:
            return self._min
        if p == 100.0:
            return self._max
        rank = max(1, math.ceil(p / 100.0 * self._count))
        seen = 0
        for idx in sorted(self._buckets):
            seen += self._buckets[idx]
            if seen >= rank:
                return min(max(_midpoint(idx), self._min), self._max)
        return self._max  # pragma: no cover - unreachable, counts always add up

    def __len__(self) -> int:
        return self._count

    def __bool__(self) -> bool:
        return True  # an empty histogram is still a histogram

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Histogram):
            return NotImplemented
        return self._buckets == other._buckets and self._count == other._count

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return (
            f"<Histogram n={self._count} p50={self.percentile(50):.3f}ms "
            f"p95={self.percentile(95):.3f}ms max={self.max:.3f}ms>"
        )

    # serialisation ---------------------------------------------------------
    def to_bytes(self) -> bytes:
        """A compact, versioned encoding (``b"RLH"`` + version + zlib payload)."""
        out = bytearray()
        _put_varint(out, len(self._buckets))
        prev = 0
        for idx in sorted(self._buckets):
            _put_varint(out, idx - prev)
            _put_varint(out, self._buckets[idx])
            prev = idx
        if self._count:
            out += _TAIL.pack(self._min, self._max, self._sum)
        return _MAGIC + bytes((_VERSION,)) + zlib.compress(bytes(out), 6)

    @classmethod
    def from_bytes(cls, data: bytes) -> Histogram:
        """Inverse of :meth:`to_bytes`; raises ``ValueError`` on malformed input."""
        data = bytes(data)
        if data[:3] != _MAGIC or len(data) < 4:
            raise ValueError("not a Redline histogram")
        if data[3] != _VERSION:
            raise ValueError(f"unsupported histogram version {data[3]}")
        try:
            raw = zlib.decompress(data[4:])
        except zlib.error as exc:
            raise ValueError(f"corrupt histogram: {exc}") from None
        hist = cls()
        pos = 0
        try:
            n, pos = _get_varint(raw, pos)
            idx = 0
            for _ in range(n):
                delta, pos = _get_varint(raw, pos)
                cnt, pos = _get_varint(raw, pos)
                idx += delta
                if idx > _MAX_BUCKET or cnt <= 0:
                    raise ValueError("bucket out of range")
                hist._buckets[idx] = cnt
                hist._count += cnt
            if hist._count:
                if len(raw) - pos != _TAIL.size:
                    raise ValueError("bad trailer")
                hist._min, hist._max, hist._sum = _TAIL.unpack_from(raw, pos)
            elif pos != len(raw):
                raise ValueError("trailing bytes")
        except (IndexError, struct.error) as exc:
            raise ValueError(f"corrupt histogram: {exc}") from None
        return hist


def _put_varint(out: bytearray, n: int) -> None:
    while n > 0x7F:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)


def _get_varint(buf: bytes, pos: int) -> tuple[int, int]:
    shift = 0
    result = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")
