from datetime import UTC, datetime

import pytest

from app.timeutils import (
    TimeValueError,
    TimeWindow,
    datetime_to_epoch_ms,
    epoch_ms_to_datetime,
    five_minute_window,
    iter_windows,
    parse_rfc3339,
    utc_rfc3339,
    window_for,
)


def test_utc_millisecond_conversion_and_rfc3339():
    value = datetime.fromisoformat("2026-10-22T12:34:56.789+08:00")
    stamp = datetime_to_epoch_ms(value)
    assert epoch_ms_to_datetime(stamp) == datetime(2026, 10, 22, 4, 34, 56, 789000, tzinfo=UTC)
    assert utc_rfc3339(stamp) == "2026-10-22T04:34:56.789Z"
    assert parse_rfc3339("2026-10-22T12:34:56.789+08:00") == epoch_ms_to_datetime(stamp)
    with pytest.raises(TimeValueError):
        parse_rfc3339("2026-10-22T12:34:56")


def test_natural_windows_use_iana_wall_clock_and_half_open_edges():
    stamp = "2026-10-22T12:37:40+08:00"
    minute = window_for(stamp, "minute", timezone="Asia/Shanghai")
    five = five_minute_window(stamp, timezone="Asia/Shanghai")
    hour = window_for(stamp, "hour", timezone="Asia/Shanghai")
    day = window_for(stamp, "day", timezone="Asia/Shanghai")
    assert minute.start == datetime.fromisoformat("2026-10-22T12:37:00+08:00").astimezone(UTC)
    assert five.start == datetime.fromisoformat("2026-10-22T12:35:00+08:00").astimezone(UTC)
    assert five.duration_ms == 5 * 60_000
    assert hour.start == datetime.fromisoformat("2026-10-22T12:00:00+08:00").astimezone(UTC)
    assert day.start == datetime.fromisoformat("2026-10-22T00:00:00+08:00").astimezone(UTC)
    assert minute.contains(minute.start_ms)
    assert not minute.contains(minute.end_ms)
    assert minute.intersects(minute.end_ms - 1, minute.end_ms + 1)
    assert not minute.intersects(minute.end_ms, minute.end_ms + 1)


def test_split_windows_have_no_overlap_and_clip_is_left_closed_right_open():
    start = "2026-10-22T12:03:30+08:00"
    end = "2026-10-22T12:12:00+08:00"
    windows = list(iter_windows(start, end, "5m", timezone="Asia/Shanghai", clip=True))
    assert len(windows) == 3
    assert windows[0].duration_ms == 90_000
    assert windows[-1].end == datetime.fromisoformat(end).astimezone(UTC)
    assert all(left.end_ms == right.start_ms for left, right in zip(windows, windows[1:]))


def test_dst_fall_back_repeated_hour_is_two_distinct_windows():
    first = window_for("2024-11-03T01:30:00-04:00", "hour", timezone="America/New_York")
    second = window_for("2024-11-03T01:30:00-05:00", "hour", timezone="America/New_York")
    assert first.start_ms != second.start_ms
    assert first.duration_ms == second.duration_ms == 3_600_000
    assert first.end_ms == second.start_ms
    assert first.start.isoformat() == "2024-11-03T05:00:00+00:00"
    assert second.start.isoformat() == "2024-11-03T06:00:00+00:00"


def test_dst_spring_forward_skips_nonexistent_wall_clock_time():
    window = window_for("2024-03-10T01:59:30-05:00", "minute", timezone="America/New_York")
    assert window.duration_ms == 60_000
    assert window.start.isoformat() == "2024-03-10T06:59:00+00:00"
    assert window.end.isoformat() == "2024-03-10T07:00:00+00:00"


def test_date_windows_interpret_midnight_in_requested_timezone():
    window = window_for(datetime(2026, 10, 22).date(), "day", timezone="America/Los_Angeles")
    assert window.start.isoformat() == "2026-10-22T07:00:00+00:00"


def test_time_window_rejects_invalid_edges_before_comparison():
    with pytest.raises(TimeValueError):
        TimeWindow("0", 1, "minute", "Asia/Shanghai")
    with pytest.raises(TimeValueError):
        TimeWindow(2, 1, "minute", "Asia/Shanghai")


def test_naive_window_helpers_use_timezone_but_window_operations_require_aware_times():
    window = window_for(datetime(2026, 10, 22, 12, 37, 40), "minute", timezone="Asia/Shanghai")
    assert window.start.isoformat() == "2026-10-22T04:37:00+00:00"
    with pytest.raises(TimeValueError):
        window.contains(datetime(2026, 10, 22, 12, 37, 30))
    with pytest.raises(TimeValueError):
        window.intersects(datetime(2026, 10, 22, 12, 37), datetime(2026, 10, 22, 12, 38))
    with pytest.raises(TimeValueError):
        window.clip(datetime(2026, 10, 22, 12, 37), datetime(2026, 10, 22, 12, 38))


def test_dst_day_windows_follow_local_midnight():
    spring = window_for("2024-03-10T12:00:00-04:00", "day", timezone="America/New_York")
    fall = window_for("2024-11-03T12:00:00-05:00", "day", timezone="America/New_York")
    assert spring.duration_ms == 23 * 60 * 60 * 1_000
    assert fall.duration_ms == 25 * 60 * 60 * 1_000
