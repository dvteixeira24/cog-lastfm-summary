from datetime import datetime, timedelta, timezone

from lastfmsummary.lastfm import Scrobble
from lastfmsummary.listening import (
    ListeningClock,
    build_genre_timeline,
    build_listening_clock,
)


def scrobble(artist, hour, weight=1.0, day=1):
    return Scrobble(
        artist=artist,
        track="Track",
        played_at=datetime(2026, 10, day, hour, 30, tzinfo=timezone.utc),
        weight=weight,
    )


def test_clock_uses_local_hours_and_scales_weights():
    rows = [
        scrobble("A", 16, weight=2.0),
        scrobble("B", 17, weight=5.0),
        scrobble("C", 17, weight=5.0),
        scrobble("D", 18, weight=1.0),
    ]
    clock = build_listening_clock(rows, tz_name="Europe/Berlin")
    assert isinstance(clock, ListeningClock)
    assert clock.busiest_hour == 19
    assert clock.busiest_count == 10
    assert clock.hourly[18] == 2.0
    assert clock.total == 13
    assert clock.sampled == 4


def test_clock_breaks_ties_toward_earlier_hour():
    clock = build_listening_clock([scrobble("A", 4), scrobble("B", 9)], tz_name="UTC")
    assert clock.busiest_hour == 4


def test_clock_returns_none_without_scrobbles():
    assert build_listening_clock([], tz_name="UTC") is None


def test_genre_timeline_splits_affinity_and_drops_noise():
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=6)
    tags = {
        "slowdive": (("shoegaze", 80), ("dream pop", 20)),
        "radiohead": (("art rock", 100), ("seen live", 50)),
    }
    rows = [
        Scrobble("Slowdive", "Alison", start + timedelta(hours=1), 2.0),
        Scrobble("Radiohead", "Airbag", start + timedelta(days=5), 1.0),
    ]
    timeline = build_genre_timeline(
        rows,
        tags,
        start=start,
        end=end,
        tz_name="UTC",
        bucket_count=3,
    )
    assert timeline is not None
    assert timeline.labels == ("01 Oct", "03 Oct", "05 Oct")
    names = [series.name for series in timeline.series]
    assert names == ["shoegaze", "art rock", "dream pop"]
    values = {series.name: series.values for series in timeline.series}
    assert values["shoegaze"] == (1.6, 0.0, 0.0)
    assert values["dream pop"] == (0.4, 0.0, 0.0)
    assert values["art rock"] == (0.0, 0.0, 1.0)


def test_genre_timeline_returns_none_without_tags():
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert (
        build_genre_timeline(
            [Scrobble("Unknown", "Track", start, 1.0)],
            {},
            start=start,
            end=start + timedelta(days=7),
            tz_name="UTC",
            bucket_count=7,
        )
        is None
    )
