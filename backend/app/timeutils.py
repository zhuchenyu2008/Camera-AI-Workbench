from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def iso(ms: int | None) -> str | None:
    return (
        datetime.fromtimestamp(ms / 1000, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
        if ms is not None
        else None
    )


def timestamp(value: str | datetime) -> int:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("时间必须带时区")
    return int(dt.timestamp() * 1000)


def day_range(local_date: str) -> tuple[int, int]:
    dt = datetime.strptime(local_date, "%Y-%m-%d").replace(tzinfo=BEIJING)
    return timestamp(dt), timestamp(dt + timedelta(days=1))


def union(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for a, b in sorted((a, b) for a, b in intervals if a < b):
        if result and a <= result[-1][1]:
            result[-1] = (result[-1][0], max(b, result[-1][1]))
        else:
            result.append((a, b))
    return result


def intersect(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> list[tuple[int, int]]:
    aa, bb, result, i, j = union(a), union(b), [], 0, 0
    while i < len(aa) and j < len(bb):
        start, end = max(aa[i][0], bb[j][0]), min(aa[i][1], bb[j][1])
        if start < end:
            result.append((start, end))
        if aa[i][1] <= bb[j][1]:
            i += 1
        else:
            j += 1
    return result


def schedule_intervals(rules: list[dict], start: int, end: int) -> list[tuple[int, int]]:
    first = datetime.fromtimestamp(start / 1000, BEIJING).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=1)
    last = datetime.fromtimestamp(end / 1000, BEIJING)
    spans = []
    while first <= last:
        for rule in rules:
            if first.isoweekday() not in rule["weekdays"]:
                continue
            sh, sm = map(int, rule["start"].split(":"))
            eh, em = map(int, rule["end"].split(":"))
            a = timestamp(first + timedelta(hours=sh, minutes=sm))
            b = timestamp(first + timedelta(days=rule["end_day_offset"], hours=eh, minutes=em))
            if max(a, start) < min(b, end):
                spans.append((max(a, start), min(b, end)))
        first += timedelta(days=1)
    return union(spans)
