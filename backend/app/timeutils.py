"""UTC millisecond time conversion and natural window helpers.

Persistence uses integer UTC epoch milliseconds; intervals are always left-closed
and right-open. Window boundaries follow IANA local wall-clock rules.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from enum import Enum
from typing import Any, Iterator, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC_MS = 1_000
RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[zZ]|[+-]\d{2}:\d{2})$")
RFC3339_Z_RE = re.compile(r"([zZ])$")


class WindowKind(str, Enum):
    """Supported natural-time window granularities."""

    MINUTE = "minute"
    FIVE_MINUTES = "5minute"
    HOUR = "hour"
    DAY = "day"


WindowKindValue = WindowKind | Literal["minute", "5minute", "5m", "five_minute", "hour", "day"]


class TimeValueError(ValueError):
    """Raised when an input time or time-window definition is invalid."""


def resolve_timezone(value: str | tzinfo | None = "Asia/Shanghai") -> tzinfo:
    """Resolve an IANA timezone name or an explicitly supplied tzinfo object."""

    if value is None:
        return ZoneInfo("Asia/Shanghai")
    if isinstance(value, str):
        if not value.strip():
            raise TimeValueError("timezone must not be empty")
        try:
            return ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise TimeValueError(f"Unknown IANA timezone: {value}") from exc
    if isinstance(value, tzinfo):
        return value
    raise TypeError(f"Unsupported timezone type: {type(value)!r}")


def _ensure_aware(value: datetime, *, assume_timezone: str | tzinfo | None = None) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        if assume_timezone is None:
            raise TimeValueError("Naive datetime requires assume_timezone")
        value = value.replace(tzinfo=resolve_timezone(assume_timezone))
    return value


def parse_rfc3339(value: str) -> datetime:
    """Parse an RFC3339 timestamp with an explicit UTC offset and return UTC."""

    if not isinstance(value, str) or not value.strip():
        raise TimeValueError("RFC3339 timestamp must be non-empty")
    candidate = value.strip()
    if not RFC3339_RE.fullmatch(candidate):
        raise TimeValueError(f"Invalid RFC3339 timestamp: {value!r}")
    if RFC3339_Z_RE.search(candidate):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise TimeValueError(f"Invalid RFC3339 timestamp: {value!r}") from exc
    return _ensure_aware(parsed).astimezone(UTC)


def coerce_datetime(
    value: datetime | date | int | float | str,
    *,
    assume_timezone: str | tzinfo | None = None,
) -> datetime:
    """Convert a supported value to an aware datetime; numbers are epoch milliseconds."""

    if isinstance(value, datetime):
        return _ensure_aware(value, assume_timezone=assume_timezone)
    if isinstance(value, date):
        local_tz = resolve_timezone(assume_timezone)
        return datetime(value.year, value.month, value.day, tzinfo=local_tz)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            raise TimeValueError("epoch milliseconds must be finite")
        return epoch_ms_to_datetime(int(value), tz=UTC)
    if isinstance(value, str):
        return parse_rfc3339(value)
    raise TypeError(f"Unsupported time value type: {type(value)!r}")


def datetime_to_epoch_ms(
    value: datetime,
    *,
    assume_timezone: str | tzinfo | None = None,
) -> int:
    """Convert an aware datetime to integer UTC epoch milliseconds."""

    aware = _ensure_aware(value, assume_timezone=assume_timezone).astimezone(UTC)
    # Integer arithmetic avoids floating-point precision loss in timestamp conversion.
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = aware - epoch
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def to_epoch_ms(value: datetime | date | int | float | str, *, assume_timezone: str | tzinfo | None = None) -> int:
    """Convert a datetime or parse an RFC3339 string; integer inputs are already milliseconds."""

    if isinstance(value, datetime):
        return datetime_to_epoch_ms(value, assume_timezone=assume_timezone)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Numeric input is already in UTC epoch milliseconds.
        if not math.isfinite(float(value)):
            raise TimeValueError("epoch milliseconds must be finite")
        return int(value)
    return datetime_to_epoch_ms(coerce_datetime(value, assume_timezone=assume_timezone))


def epoch_ms_to_datetime(value: int | float, *, tz: str | tzinfo | None = UTC) -> datetime:
    """Convert UTC epoch milliseconds to a datetime in the requested timezone."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("epoch milliseconds must be finite")
    if not math.isfinite(float(value)):
        raise TimeValueError("epoch milliseconds must be finite")
    utc_value = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=int(value))
    return utc_value.astimezone(resolve_timezone(tz))


def utc_now() -> datetime:
    """Return the current timezone-aware UTC datetime."""

    return datetime.now(UTC)


def utc_now_ms() -> int:
    """Return current UTC epoch milliseconds."""

    return datetime_to_epoch_ms(utc_now())


# Safety fallback for unusual third-party tzinfo implementations.
now_utc_ms = utc_now_ms


def format_rfc3339(
    value: datetime | int | float,
    *,
    timespec: str = "milliseconds",
) -> str:
    """Format a datetime or epoch-millisecond value as UTC RFC3339."""

    if isinstance(value, datetime):
        aware = _ensure_aware(value).astimezone(UTC)
    else:
        aware = epoch_ms_to_datetime(value, tz=UTC)
    return aware.isoformat(timespec=timespec).replace("+00:00", "Z")


def utc_rfc3339(value: datetime | int | float) -> str:
    """Format a value with millisecond precision and a trailing Z."""

    return format_rfc3339(value, timespec="milliseconds")


def _normalise_window_kind(kind: WindowKindValue | str) -> WindowKind:
    if isinstance(kind, WindowKind):
        return kind
    aliases = {
        "m": WindowKind.MINUTE,
        "min": WindowKind.MINUTE,
        "minute": WindowKind.MINUTE,
        "1m": WindowKind.MINUTE,
        "5m": WindowKind.FIVE_MINUTES,
        "5min": WindowKind.FIVE_MINUTES,
        "5minute": WindowKind.FIVE_MINUTES,
        "five_minute": WindowKind.FIVE_MINUTES,
        "five_minutes": WindowKind.FIVE_MINUTES,
        "hour": WindowKind.HOUR,
        "1h": WindowKind.HOUR,
        "h": WindowKind.HOUR,
        "day": WindowKind.DAY,
        "1d": WindowKind.DAY,
        "d": WindowKind.DAY,
    }
    try:
        return aliases[str(kind).strip().lower()]
    except KeyError as exc:
        raise TimeValueError(f"Unsupported window kind: {kind!r}") from exc


def _window_spec(kind: WindowKind) -> tuple[timedelta, str]:
    if kind is WindowKind.MINUTE:
        return timedelta(minutes=1), "minute"
    if kind is WindowKind.FIVE_MINUTES:
        return timedelta(minutes=5), "5minute"
    if kind is WindowKind.HOUR:
        return timedelta(hours=1), "hour"
    if kind is WindowKind.DAY:
        return timedelta(days=1), "day"
    raise AssertionError(f"unhandled window kind: {kind}")


def _floor_local_wall(local: datetime, kind: WindowKind) -> datetime:
    """Return the naive local wall-clock start for the containing window."""

    if kind is WindowKind.MINUTE:
        return local.replace(second=0, microsecond=0, tzinfo=None)
    if kind is WindowKind.FIVE_MINUTES:
        return local.replace(minute=(local.minute // 5) * 5, second=0, microsecond=0, tzinfo=None)
    if kind is WindowKind.HOUR:
        return local.replace(minute=0, second=0, microsecond=0, tzinfo=None)
    if kind is WindowKind.DAY:
        return datetime(local.year, local.month, local.day)
    raise AssertionError(f"unhandled window kind: {kind}")


def _wall_candidates(wall: datetime, timezone_: tzinfo) -> list[datetime]:
    """Return valid timezone-aware representations of a wall time in UTC order.

    During a fall-back transition an ambiguous wall time has two candidates. A wall
    time in a spring-forward gap has none and is handled by ``_localise_wall``.
    """

    candidates: list[datetime] = []
    for fold in (0, 1):
        candidate = wall.replace(tzinfo=timezone_, fold=fold)
        round_trip = candidate.astimezone(UTC).astimezone(timezone_)
        if round_trip.replace(tzinfo=None) == wall:
            if not any(candidate.astimezone(UTC) == item.astimezone(UTC) for item in candidates):
                candidates.append(candidate)
    return candidates


def _localise_wall(
    wall: datetime,
    timezone_: tzinfo,
    *,
    fold_hint: int = 0,
    allow_nonexistent: bool = True,
) -> datetime:
    """Attach a timezone to a wall time, honoring a fold or advancing through a gap."""

    candidates = _wall_candidates(wall, timezone_)
    if candidates:
        for candidate in candidates:
            if candidate.fold == fold_hint:
                return candidate
        return candidates[0]
    if not allow_nonexistent:
        raise TimeValueError(f"Nonexistent local time: {wall.isoformat()} ({timezone_})")

    # For a nonexistent wall boundary, use the post-transition interpretation.
    raw = [wall.replace(tzinfo=timezone_, fold=0), wall.replace(tzinfo=timezone_, fold=1)]
    return max(raw, key=lambda item: item.astimezone(UTC))


def _window_end(
    start: datetime,
    end_wall: datetime,
    kind: WindowKind,
    timezone_: tzinfo,
) -> datetime:
    """Resolve a natural end boundary while keeping sub-day windows monotonic."""

    desired = _localise_wall(end_wall, timezone_, fold_hint=start.fold)
    if kind is WindowKind.DAY:
        return desired

    # A natural wall boundary can span an anomalous elapsed duration at a DST
    # transition. Sub-day capture windows remain fixed-duration and contiguous.
    unit_ms = int(_window_spec(kind)[0].total_seconds() * 1_000)
    start_ms = datetime_to_epoch_ms(start)
    desired_ms = datetime_to_epoch_ms(desired)
    if desired_ms - start_ms != unit_ms:
        desired = epoch_ms_to_datetime(start_ms + unit_ms, tz=timezone_)
    return desired


@dataclass(frozen=True, slots=True)
class TimeWindow:
    """A UTC-millisecond interval with left-closed, right-open semantics.

    ``timezone`` records the IANA timezone used to calculate the natural boundary;
    the stored endpoints themselves are absolute UTC epoch milliseconds.
    """

    start_ms: int
    end_ms: int
    kind: str
    timezone: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.start_ms, bool)
            or isinstance(self.end_ms, bool)
            or not isinstance(self.start_ms, int)
            or not isinstance(self.end_ms, int)
        ):
            raise TimeValueError("Window edges must be integer epoch milliseconds")
        if not isinstance(self.kind, str) or not self.kind:
            raise TimeValueError("Window kind must be a non-empty string")
        if not isinstance(self.timezone, str) or not self.timezone:
            raise TimeValueError("Window timezone must be a non-empty string")
        if self.start_ms >= self.end_ms:
            raise TimeValueError("Window must satisfy start < end")

    @property
    def start(self) -> datetime:
        return epoch_ms_to_datetime(self.start_ms, tz=UTC)

    @property
    def end(self) -> datetime:
        return epoch_ms_to_datetime(self.end_ms, tz=UTC)

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def duration_seconds(self) -> float:
        return self.duration_ms / 1_000

    def contains(self, value: datetime | int | float | str, *, include_end: bool = False) -> bool:
        """Test membership in the half-open interval; datetime values must be aware."""

        timestamp = to_epoch_ms(value)
        return self.start_ms <= timestamp <= self.end_ms if include_end else self.start_ms <= timestamp < self.end_ms

    def intersects(self, start: datetime | int | float | str, end: datetime | int | float | str) -> bool:
        """Test overlap with another left-closed, right-open interval."""

        start_ms = to_epoch_ms(start)
        end_ms = to_epoch_ms(end)
        if start_ms >= end_ms:
            return False
        return self.start_ms < end_ms and start_ms < self.end_ms

    def clip(self, start: datetime | int | float | str, end: datetime | int | float | str) -> "TimeWindow | None":
        """Return the intersection with another interval, or ``None`` if empty."""

        start_ms = max(self.start_ms, to_epoch_ms(start))
        end_ms = min(self.end_ms, to_epoch_ms(end))
        if start_ms >= end_ms:
            return None
        return TimeWindow(start_ms, end_ms, self.kind, self.timezone)

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": utc_rfc3339(self.start_ms),
            "end": utc_rfc3339(self.end_ms),
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "kind": self.kind,
            "timezone": self.timezone,
        }

    def __iter__(self) -> Iterator[int]:
        """Allow unpacking a window as ``(start_ms, end_ms)``."""

        yield self.start_ms
        yield self.end_ms


# Timezone and daylight-saving boundary handling.
Window = TimeWindow


def window_for(
    value: datetime | date | int | float | str,
    kind: WindowKindValue | str,
    *,
    timezone: str | tzinfo = "Asia/Shanghai",
) -> TimeWindow:
    """Return the natural IANA-local window containing ``value``.

    Minute, five-minute, and hour windows use fixed elapsed durations, even around
    DST transitions; day windows follow local midnight and may be 23 or 25 hours.
    The returned endpoints are UTC epoch milliseconds and the interval is ``[start,
    end)``. Naive datetimes are interpreted in ``timezone``.
    """

    kind_value = _normalise_window_kind(kind)
    timezone_ = resolve_timezone(timezone)
    instant = coerce_datetime(value, assume_timezone=timezone_).astimezone(timezone_)
    wall_start = _floor_local_wall(instant, kind_value)
    start = _localise_wall(wall_start, timezone_, fold_hint=instant.fold)
    step, kind_name = _window_spec(kind_value)
    wall_end = wall_start + step
    end = _window_end(start, wall_end, kind_value, timezone_)
    start_ms = datetime_to_epoch_ms(start)
    end_ms = datetime_to_epoch_ms(end)
    if end_ms <= start_ms:
        # Natural window boundaries should always advance.
        end_ms = start_ms + int(step.total_seconds() * 1_000)
    timezone_name = getattr(timezone_, "key", str(timezone))
    return TimeWindow(start_ms, end_ms, kind_name, timezone_name)


def natural_window(
    value: datetime | date | int | float | str,
    kind: WindowKindValue | str,
    *,
    timezone: str | tzinfo = "Asia/Shanghai",
) -> TimeWindow:
    """General-purpose alias for :func:`window_for`. See it for boundary semantics."""

    return window_for(value, kind, timezone=timezone)


def minute_window(value: datetime | date | int | float | str, *, timezone: str | tzinfo = "Asia/Shanghai") -> TimeWindow:
    """Return the natural one-minute window containing ``value``."""

    return window_for(value, WindowKind.MINUTE, timezone=timezone)


def five_minute_window(
    value: datetime | date | int | float | str,
    *,
    timezone: str | tzinfo = "Asia/Shanghai",
) -> TimeWindow:
    """Return the natural five-minute window containing ``value``."""

    return window_for(value, WindowKind.FIVE_MINUTES, timezone=timezone)


def hour_window(value: datetime | date | int | float | str, *, timezone: str | tzinfo = "Asia/Shanghai") -> TimeWindow:
    """Return the natural one-hour window containing ``value``."""

    return window_for(value, WindowKind.HOUR, timezone=timezone)


def day_window(value: datetime | date | int | float | str, *, timezone: str | tzinfo = "Asia/Shanghai") -> TimeWindow:
    """Return the local-calendar-day window containing ``value``."""

    return window_for(value, WindowKind.DAY, timezone=timezone)


def iter_windows(
    start: datetime | date | int | float | str,
    end: datetime | date | int | float | str,
    kind: WindowKindValue | str,
    *,
    timezone: str | tzinfo = "Asia/Shanghai",
    clip: bool = False,
) -> Iterator[TimeWindow]:
    """Yield windows intersecting ``[start, end)`` in chronological order.

    Set ``clip=True`` to trim the first and last windows to the requested interval.
    Naive datetimes are interpreted in ``timezone``. An empty interval yields no
    windows; a reversed interval raises :class:`TimeValueError`.
    """

    start_ms = to_epoch_ms(start, assume_timezone=timezone)
    end_ms = to_epoch_ms(end, assume_timezone=timezone)
    if start_ms > end_ms:
        raise TimeValueError("Interval must satisfy start <= end")
    if start_ms == end_ms:
        return

    kind_value = _normalise_window_kind(kind)
    current = window_for(start_ms, kind_value, timezone=timezone)
    max_iterations = max(2, (end_ms - start_ms) // 1_000 + 10)
    iterations = 0
    while current.start_ms < end_ms:
        item = current.clip(start_ms, end_ms) if clip else current
        if item is not None:
            yield item
        iterations += 1
        if iterations > max_iterations:
            raise RuntimeError("Window iteration failed to advance for tzinfo")
        current = window_for(current.end_ms, kind_value, timezone=timezone)


def split_into_windows(
    start: datetime | date | int | float | str,
    end: datetime | date | int | float | str,
    kind: WindowKindValue | str,
    *,
    timezone: str | tzinfo = "Asia/Shanghai",
    clip: bool = True,
) -> list[TimeWindow]:
    """Return :func:`iter_windows` as a list, clipping edges by default."""

    return list(iter_windows(start, end, kind, timezone=timezone, clip=clip))


# Timezone and daylight-saving boundary handling.
get_window = window_for
iter_natural_windows = iter_windows
parse_utc_rfc3339 = parse_rfc3339
ms_to_datetime = epoch_ms_to_datetime
datetime_to_ms = datetime_to_epoch_ms


__all__ = [
    "UTC_MS",
    "TimeValueError",
    "TimeWindow",
    "Window",
    "WindowKind",
    "WindowKindValue",
    "coerce_datetime",
    "datetime_to_epoch_ms",
    "datetime_to_ms",
    "day_window",
    "epoch_ms_to_datetime",
    "five_minute_window",
    "format_rfc3339",
    "get_window",
    "hour_window",
    "iter_natural_windows",
    "iter_windows",
    "minute_window",
    "ms_to_datetime",
    "natural_window",
    "now_utc_ms",
    "parse_rfc3339",
    "parse_utc_rfc3339",
    "resolve_timezone",
    "split_into_windows",
    "to_epoch_ms",
    "utc_now",
    "utc_now_ms",
    "utc_rfc3339",
    "window_for",
]
