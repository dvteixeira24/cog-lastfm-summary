"""Pure analytics for listening clocks and genre timelines."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .lastfm import Scrobble

NOISE_TAGS = frozenset(
    {
        "seen live",
        "favorites",
        "favourite",
        "favorite",
        "love",
        "loved",
        "awesome",
        "beautiful",
        "spotify",
        "owned",
        "want to see live",
        "under 2000 listeners",
    }
)


@dataclass(frozen=True)
class ListeningClock:
    """Weighted scrobble counts for each local hour of the day."""

    hourly: tuple[float, ...]
    busiest_hour: int
    busiest_count: int
    total: int
    sampled: int


@dataclass(frozen=True)
class TagSeries:
    """One genre band in the timeline."""

    name: str
    values: tuple[float, ...]


@dataclass(frozen=True)
class GenreTimeline:
    """Top tags across evenly spaced buckets of the listening window."""

    labels: tuple[str, ...]
    series: tuple[TagSeries, ...]


def text_key(value: str) -> str:
    """Normalize a tag or artist name for grouping and lookup."""
    return " ".join(str(value or "").split()).casefold()


def _timezone(tz_name: str) -> ZoneInfo | timezone:
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _local_time(value: datetime, tz: ZoneInfo | timezone) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(tz)


def build_listening_clock(
    scrobbles: Sequence[Scrobble], *, tz_name: str
) -> ListeningClock | None:
    """Bucket weighted scrobbles by local hour and find the busiest hour."""
    if not scrobbles:
        return None

    tz = _timezone(tz_name)
    hourly = [0.0] * 24
    total = 0.0
    for item in scrobbles:
        weight = max(0.0, float(item.weight))
        hourly[_local_time(item.played_at, tz).hour] += weight
        total += weight

    peak = max(hourly)
    if peak <= 0 or total <= 0:
        return None
    return ListeningClock(
        hourly=tuple(hourly),
        busiest_hour=hourly.index(peak),
        busiest_count=int(round(peak)),
        total=int(round(total)),
        sampled=len(scrobbles),
    )


def build_genre_timeline(
    scrobbles: Sequence[Scrobble],
    tags_by_artist: Mapping[str, Sequence[tuple[str, int]]],
    *,
    start: datetime,
    end: datetime,
    tz_name: str,
    bucket_count: int = 6,
) -> GenreTimeline | None:
    """Build weighted tag totals for equal time buckets in the window."""
    if not scrobbles or bucket_count < 1:
        return None
    span = (end - start).total_seconds()
    if span <= 0:
        return None

    tz = _timezone(tz_name)
    start_ts = start.timestamp()
    buckets: dict[str, list[float]] = {}
    display: dict[str, str] = {}

    for item in scrobbles:
        tags = tags_by_artist.get(text_key(item.artist)) or ()
        selected = [
            (name, count)
            for name, count in tags
            if count > 0 and text_key(name) not in NOISE_TAGS
        ]
        if not selected:
            continue
        total_count = sum(count for _, count in selected)
        if total_count <= 0:
            continue

        played_at = _local_time(item.played_at, timezone.utc)
        index = int((played_at.timestamp() - start_ts) / span * bucket_count)
        index = max(0, min(bucket_count - 1, index))
        weight = max(0.0, float(item.weight))

        for name, count in selected:
            key = text_key(name)
            display.setdefault(key, name)
            values = buckets.setdefault(key, [0.0] * bucket_count)
            values[index] += weight * count / total_count

    ranked = sorted(buckets.items(), key=lambda item: sum(item[1]), reverse=True)
    ranked = [(key, values) for key, values in ranked if sum(values) > 0][:7]
    if not ranked:
        return None

    labels = tuple(
        (start + (end - start) * index / bucket_count).astimezone(tz).strftime("%d %b")
        for index in range(bucket_count)
    )
    return GenreTimeline(
        labels=labels,
        series=tuple(TagSeries(display[key], tuple(values)) for key, values in ranked),
    )


__all__ = [
    "GenreTimeline",
    "ListeningClock",
    "NOISE_TAGS",
    "TagSeries",
    "build_genre_timeline",
    "build_listening_clock",
    "text_key",
]
