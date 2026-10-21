"""Pure scheduling helpers for the Last.fm summary cog."""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "PERIODS",
    "PERIOD_LABELS",
    "PERIOD_WINDOWS",
    "PERIOD_ALIASES",
    "PERIOD_DAYS",
    "ANCHOR_MONTHS",
    "normalize_period",
    "parse_hhmm",
    "format_hhmm",
    "validate_timezone",
    "compute_next_run",
    "period_window",
]

#: Cadence name -> Last.fm ``period`` query value.
PERIODS: dict[str, str] = {
    "weekly": "7day",
    "monthly": "1month",
    "quarterly": "3month",
    "half-yearly": "6month",
    "yearly": "12month",
}

PERIOD_LABELS: dict[str, str] = {
    "weekly": "Weekly",
    "monthly": "Monthly",
    "quarterly": "Quarterly",
    "half-yearly": "Half-Yearly",
    "yearly": "Yearly",
}

PERIOD_WINDOWS: dict[str, str] = {
    "weekly": "the last 7 days",
    "monthly": "the last month",
    "quarterly": "the last 3 months",
    "half-yearly": "the last 6 months",
    "yearly": "the last 12 months",
}

#: Days covered by each Last.fm chart period, used for scrobble windows.
PERIOD_DAYS: dict[str, int] = {
    "weekly": 7,
    "monthly": 30,
    "quarterly": 90,
    "half-yearly": 180,
    "yearly": 365,
}

PERIOD_ALIASES: dict[str, str] = {
    "week": "weekly",
    "weekly": "weekly",
    "month": "monthly",
    "monthly": "monthly",
    "quarter": "quarterly",
    "quarterly": "quarterly",
    "halfyear": "half-yearly",
    "half-year": "half-yearly",
    "half-yearly": "half-yearly",
    "semiannual": "half-yearly",
    "year": "yearly",
    "yearly": "yearly",
    "annual": "yearly",
}

#: Months in which a non-weekly run may start.
ANCHOR_MONTHS: dict[str, tuple[int, ...]] = {
    "monthly": tuple(range(1, 13)),
    "quarterly": (1, 4, 7, 10),
    "half-yearly": (1, 7),
    "yearly": (1,),
}

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def normalize_period(value: str) -> str:
    """Return the canonical period name for ``value``.

    Raises:
        ValueError: if the value is not a supported period or alias.
    """
    key = str(value).strip().lower().replace("_", "-").replace(" ", "-")
    # Collapse accidental repeated separators such as "half--yearly".
    while "--" in key:
        key = key.replace("--", "-")
    try:
        return PERIOD_ALIASES[key]
    except KeyError as exc:
        supported = ", ".join(PERIODS)
        raise ValueError(f"Unknown period {value!r}. Choose one of: {supported}.") from exc


def parse_hhmm(value: str) -> tuple[int, int]:
    """Parse ``HH:MM`` (24-hour) into ``(hour, minute)``.

    Raises:
        ValueError: if the value is not a valid 24-hour time.
    """
    match = _TIME_RE.match(str(value).strip())
    if match is None:
        raise ValueError(f"Invalid time {value!r}. Use 24-hour HH:MM, for example 09:30.")
    return int(match.group(1)), int(match.group(2))


def format_hhmm(hour: int, minute: int) -> str:
    """Format an hour/minute pair as ``HH:MM``."""
    return f"{int(hour):02d}:{int(minute):02d}"


def validate_timezone(name: str) -> str:
    """Validate and return an IANA timezone name.

    Raises:
        ValueError: if the name is empty or unknown to ``zoneinfo``.
    """
    candidate = str(name).strip()
    if not candidate:
        raise ValueError("A timezone name is required.")
    if candidate.upper() == "UTC":
        return "UTC"
    try:
        ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"Unknown timezone {candidate!r}. Use an IANA name such as Europe/Berlin."
        ) from exc
    return candidate


def _coerce_utc(now_utc: datetime) -> datetime:
    if now_utc.tzinfo is None:
        return now_utc.replace(tzinfo=timezone.utc)
    return now_utc.astimezone(timezone.utc)


def _local_at(day: date, hour: int, minute: int, tz: ZoneInfo) -> datetime:
    # fold=0 picks the first occurrence of an ambiguous wall-clock time and
    # maps nonexistent spring-forward times forward in the same way as Red's
    # other scheduled features.
    return datetime.combine(day, time(hour, minute), tzinfo=tz)


def compute_next_run(
    period: str,
    *,
    now_utc: datetime,
    tz_name: str,
    at: str,
    weekday: int = 0,
    day: int = 1,
) -> datetime:
    """Compute the next scheduled run as an aware UTC datetime.

    Args:
        period: Cadence name (``weekly``, ``monthly``, ``quarterly``,
            ``half-yearly`` or ``yearly``), or an alias.
        now_utc: Current instant. Naive values are treated as UTC.
        tz_name: IANA timezone that anchors the local wall-clock schedule.
        at: Local ``HH:MM`` posting time.
        weekday: Monday=0 .. Sunday=6 anchor for weekly schedules.
        day: Preferred day of month for non-weekly schedules (clamped to the
            month's length, so 31 works in every month).
    """
    canonical = normalize_period(period)
    tz = ZoneInfo(validate_timezone(tz_name))
    hour, minute = parse_hhmm(at)
    now = _coerce_utc(now_utc)
    local_now = now.astimezone(tz)

    if canonical == "weekly":
        anchor = int(weekday) % 7
        offset = (anchor - local_now.weekday()) % 7
        candidate_day = local_now.date() + timedelta(days=offset)
        candidate = _local_at(candidate_day, hour, minute, tz)
        if candidate <= local_now:
            candidate = _local_at(candidate_day + timedelta(days=7), hour, minute, tz)
        return candidate.astimezone(timezone.utc)

    preferred_day = max(1, int(day))
    allowed_months = ANCHOR_MONTHS[canonical]
    year, month = local_now.year, local_now.month
    for _ in range(25):
        if month in allowed_months:
            day_of_month = min(preferred_day, calendar.monthrange(year, month)[1])
            candidate = _local_at(date(year, month, day_of_month), hour, minute, tz)
            if candidate > local_now:
                return candidate.astimezone(timezone.utc)
        month += 1
        if month > 12:
            month = 1
            year += 1
    # The loop is bounded generously; this is defensive only.
    raise RuntimeError("Could not compute the next run within 25 months.")


def period_window(period: str) -> str:
    """Return a human description of the chart window."""
    return PERIOD_WINDOWS[normalize_period(period)]
