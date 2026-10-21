from datetime import datetime, timezone

import pytest

from lastfmsummary.schedule import (
    PERIODS,
    compute_next_run,
    normalize_period,
    parse_hhmm,
    period_window,
    validate_timezone,
)


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def test_period_aliases_and_normalization():
    assert normalize_period("Weekly ") == "weekly"
    assert normalize_period("week") == "weekly"
    assert normalize_period("month") == "monthly"
    assert normalize_period("half_year") == "half-yearly"
    assert normalize_period("annual") == "yearly"
    assert set(PERIODS) == {"weekly", "monthly", "quarterly", "half-yearly", "yearly"}


def test_unknown_period_is_rejected():
    with pytest.raises(ValueError, match="Unknown period"):
        normalize_period("daily")


def test_parse_hhmm():
    assert parse_hhmm("09:30") == (9, 30)
    assert parse_hhmm("9:05") == (9, 5)
    assert parse_hhmm("23:59") == (23, 59)
    for invalid in ("24:00", "9:5", "nine", "", "12:60"):
        with pytest.raises(ValueError):
            parse_hhmm(invalid)


def test_validate_timezone():
    assert validate_timezone("Europe/Berlin") == "Europe/Berlin"
    assert validate_timezone("utc") == "UTC"
    with pytest.raises(ValueError):
        validate_timezone("Mars/Olympus_Mons")


def test_period_window_labels():
    assert period_window("weekly") == "the last 7 days"
    assert period_window("year") == "the last 12 months"


def test_monthly_next_run_is_next_first():
    result = compute_next_run(
        "monthly",
        now_utc=utc(2026, 10, 21, 12, 0),
        tz_name="UTC",
        at="09:00",
        day=1,
    )
    assert result == utc(2026, 11, 1, 9, 0)


def test_monthly_same_day_later_time_runs_today():
    result = compute_next_run(
        "monthly",
        now_utc=utc(2026, 10, 21, 12, 0),
        tz_name="UTC",
        at="13:00",
        day=21,
    )
    assert result == utc(2026, 10, 21, 13, 0)


def test_monthly_exact_time_moves_to_next_month():
    result = compute_next_run(
        "monthly",
        now_utc=utc(2026, 10, 21, 13, 0),
        tz_name="UTC",
        at="13:00",
        day=21,
    )
    assert result == utc(2026, 11, 21, 13, 0)


def test_weekly_next_monday():
    result = compute_next_run(
        "weekly",
        now_utc=utc(2026, 10, 21, 12, 0),  # Wednesday
        tz_name="UTC",
        at="09:00",
        weekday=0,
    )
    assert result == utc(2026, 10, 26, 9, 0)


def test_weekly_same_weekday_before_time_runs_today():
    result = compute_next_run(
        "weekly",
        now_utc=utc(2026, 10, 21, 8, 0),
        tz_name="UTC",
        at="09:00",
        weekday=2,
    )
    assert result == utc(2026, 10, 21, 9, 0)


def test_weekly_sunday_anchor():
    result = compute_next_run(
        "weekly",
        now_utc=utc(2026, 10, 21, 12, 0),
        tz_name="UTC",
        at="18:30",
        weekday=6,
    )
    assert result == utc(2026, 10, 25, 18, 30)


def test_quarterly_next_quarter_start():
    result = compute_next_run(
        "quarterly",
        now_utc=utc(2026, 10, 21, 12, 0),
        tz_name="UTC",
        at="09:00",
        day=1,
    )
    assert result == utc(2027, 1, 1, 9, 0)


def test_quarterly_inside_anchor_month():
    result = compute_next_run(
        "quarterly",
        now_utc=utc(2026, 10, 2, 12, 0),
        tz_name="UTC",
        at="09:00",
        day=1,
    )
    assert result == utc(2027, 1, 1, 9, 0)


def test_half_yearly_and_yearly():
    half = compute_next_run(
        "half-yearly",
        now_utc=utc(2026, 10, 21, 12, 0),
        tz_name="UTC",
        at="09:00",
        day=1,
    )
    year = compute_next_run(
        "yearly",
        now_utc=utc(2026, 10, 21, 12, 0),
        tz_name="UTC",
        at="09:00",
        day=1,
    )
    assert half == utc(2027, 1, 1, 9, 0)
    assert year == utc(2027, 1, 1, 9, 0)


def test_day_is_clamped_to_month_length():
    result = compute_next_run(
        "monthly",
        now_utc=utc(2026, 1, 31, 12, 0),
        tz_name="UTC",
        at="09:00",
        day=31,
    )
    assert result == utc(2026, 2, 28, 9, 0)


def test_timezone_dst_end_uses_local_wall_clock():
    # 2026-11-01 09:00 America/New_York is EST (UTC-5), after the DST change.
    result = compute_next_run(
        "weekly",
        now_utc=utc(2026, 10, 28, 12, 0),
        tz_name="America/New_York",
        at="09:00",
        weekday=6,  # Sunday
    )
    assert result == utc(2026, 11, 1, 14, 0)


def test_naive_now_is_treated_as_utc():
    result = compute_next_run(
        "monthly",
        now_utc=datetime(2026, 10, 21, 12, 0),
        tz_name="UTC",
        at="09:00",
        day=1,
    )
    assert result == utc(2026, 11, 1, 9, 0)
