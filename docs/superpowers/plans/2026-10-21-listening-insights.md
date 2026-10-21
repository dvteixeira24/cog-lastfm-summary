# Listening Insights Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a TOP TAGS streamgraph and a LISTENING CLOCK to every rendered Last.fm summary image.

**Architecture:** Extend the Last.fm client with sampled recent tracks and artist tags, add pure analytics in `listening.py` and a cached tag resolver in `tags.py`, draw both charts in a new `charts.py`, and let `render_grid` append an optional section. The cog collects insights best-effort so no insights failure blocks a grid.

**Tech Stack:** Python 3.9+, Pillow, aiohttp, pytest, ruff.

---

## File Structure

- Modify `lastfmsummary/schedule.py`: add `PERIOD_DAYS`.
- Modify `lastfmsummary/lastfm.py`: `Scrobble`, `get_recent_tracks`, `get_artist_top_tags`.
- Create `lastfmsummary/tags.py`: cached artist-tag resolution.
- Create `lastfmsummary/listening.py`: pure clock and timeline analytics.
- Create `lastfmsummary/charts.py`: Pillow drawing for both charts.
- Modify `lastfmsummary/render.py`: optional `genres`/`clock` section and layout.
- Modify `lastfmsummary/cog.py`: collect insights and pass them to the renderer.
- Modify `lastfmsummary/serp.py`, `deezer.py`, `render.py`, `lastfm.py`: 1.3.0 user agents.
- Modify `pyproject.toml`, `lastfmsummary/info.json`, `info.json`, `README.md`: version and docs.
- Create `tests/test_tags.py`, `tests/test_listening.py`; extend `tests/test_lastfm.py`, `tests/test_render.py`, `tests/test_cog.py`, `tests/test_schedule.py`.
- Create `docs/sample-insights.png`.

---

### Task 1: Period day counts

**Files:**
- Modify: `lastfmsummary/schedule.py:24-47`
- Test: `tests/test_schedule.py`

- [ ] **Step 1: Write the failing test**

```python
def test_period_days_match_windows():
    from lastfmsummary.schedule import PERIOD_DAYS

    assert PERIOD_DAYS == {
        "weekly": 7,
        "monthly": 30,
        "quarterly": 90,
        "half-yearly": 180,
        "yearly": 365,
    }
```

- [ ] **Step 2: Run it and confirm failure**

Run: `pytest tests/test_schedule.py::test_period_days_match_windows -q`
Expected: ImportError for `PERIOD_DAYS`.

- [ ] **Step 3: Add the constant and export it**

```python
#: Days covered by each Last.fm chart period, used for scrobble windows.
PERIOD_DAYS: dict[str, int] = {
    "weekly": 7,
    "monthly": 30,
    "quarterly": 90,
    "half-yearly": 180,
    "yearly": 365,
}
```

Add `"PERIOD_DAYS"` to `__all__`.

- [ ] **Step 4: Run the test**

Run: `pytest tests/test_schedule.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lastfmsummary/schedule.py tests/test_schedule.py
git commit -m "feat: map chart periods to day windows"
```

---

### Task 2: Recent-track sampling and artist tags in the client

**Files:**
- Modify: `lastfmsummary/lastfm.py`
- Test: `tests/test_lastfm.py`

- [ ] **Step 1: Write failing tests**

Add payload helpers and tests:

```python
def recent_payload(rows, *, total=None, total_pages=1):
    return {
        "recenttracks": {
            "@attr": {
                "page": "1",
                "perPage": "200",
                "totalPages": str(total_pages),
                "total": str(total if total is not None else len(rows)),
            },
            "track": rows,
        }
    }


def recent_row(artist, track, uts):
    return {
        "artist": {"#text": artist},
        "name": track,
        "date": {"uts": str(uts)},
    }


@pytest.mark.asyncio
async def test_recent_tracks_parses_skips_now_playing_and_scales(monkeypatch):
    rows = [
        recent_row("Radiohead", "Airbag", 1000),
        {"artist": {"#text": "Radiohead"}, "name": "Now Playing", "@attr": {"nowplaying": "true"}},
        {"artist": {"#text": "Broken"}, "name": "No Date"},
    ]
    client, session, _ = client_with(
        {"someone": FakeResponse(payload=recent_payload(rows, total=10, total_pages=1))},
        monkeypatch,
    )
    since = datetime.fromtimestamp(900, timezone.utc)
    until = datetime.fromtimestamp(1100, timezone.utc)
    scrobbles = await client.get_recent_tracks("someone", since=since, until=until)

    assert len(scrobbles) == 1
    assert scrobbles[0].artist == "Radiohead"
    assert scrobbles[0].track == "Airbag"
    assert scrobbles[0].played_at == datetime.fromtimestamp(1000, timezone.utc)
    assert scrobbles[0].weight == 10.0
    assert session.requests[0]["method"] == "user.getrecenttracks"
    assert session.requests[0]["from"] == "900"
    assert session.requests[0]["to"] == "1100"


@pytest.mark.asyncio
async def test_recent_tracks_samples_pages_evenly(monkeypatch):
    pages = {
        1: recent_payload([recent_row("A", "1", 1000)], total=2000, total_pages=20),
        5: recent_payload([recent_row("B", "2", 2000)], total=2000, total_pages=20),
        10: recent_payload([recent_row("C", "3", 3000)], total=2000, total_pages=20),
        15: recent_payload([recent_row("D", "4", 4000)], total=2000, total_pages=20),
        20: recent_payload([recent_row("E", "5", 5000)], total=2000, total_pages=20),
    }

    class PageSession(FakeSession):
        def get(self, url, params=None, **kwargs):
            self.requests.append(dict(params or {}))
            return pages[int(params["page"])]

    monkeypatch.setattr("lastfmsummary.lastfm.asyncio.sleep", lambda delay: None)
    session = PageSession({})
    client = LastFMClient("key", session=session)
    scrobbles = await client.get_recent_tracks(
        "someone",
        since=datetime.fromtimestamp(900, timezone.utc),
        until=datetime.fromtimestamp(6000, timezone.utc),
    )

    assert [s.artist for s in scrobbles] == ["A", "B", "C", "D", "E"]
    assert [request["page"] for request in session.requests] == ["1", "5", "10", "15", "20"]
    assert all(s.weight == 400.0 for s in scrobbles)


@pytest.mark.asyncio
async def test_recent_tracks_tolerates_partial_page_failure(monkeypatch):
    first = recent_payload([recent_row("A", "1", 1000)], total=600, total_pages=3)
    second = recent_payload([recent_row("B", "2", 2000)], total=600, total_pages=3)

    class PageSession(FakeSession):
        def get(self, url, params=None, **kwargs):
            page = int(params["page"])
            if page == 3:
                raise aiohttp.ClientConnectionError("offline")
            self.requests.append(dict(params))
            return FakeResponse(payload=first if page == 1 else second)

    monkeypatch.setattr("lastfmsummary.lastfm.asyncio.sleep", lambda delay: None)
    client = LastFMClient("key", session=PageSession({}))
    scrobbles = await client.get_recent_tracks(
        "someone",
        since=datetime.fromtimestamp(900, timezone.utc),
        until=datetime.fromtimestamp(4000, timezone.utc),
    )

    assert [s.artist for s in scrobbles] == ["A", "B"]
    assert all(s.weight == 300.0 for s in scrobbles)


@pytest.mark.asyncio
async def test_artist_top_tags_parses_and_limits(monkeypatch):
    payload = {
        "toptags": {
            "tag": [
                {"name": "shoegaze", "count": "100"},
                {"name": "dream pop", "count": "80"},
                {"name": "seen live", "count": "60"},
                {"name": "zero", "count": "0"},
                {"name": "", "count": "10"},
            ]
        }
    }
    client, session, _ = client_with(
        {"someone": FakeResponse(payload=payload)},
        monkeypatch,
    )
    tags = await client.get_artist_top_tags("Slowdive", limit=3)
    assert tags == (("shoegaze", 100), ("dream pop", 80), ("seen live", 60))
    assert session.requests[0]["method"] == "artist.gettoptags"
    assert session.requests[0]["artist"] == "Slowdive"
```

Import `datetime, timezone` and `Scrobble` if needed.

- [ ] **Step 2: Run the tests and confirm failure**

Run: `pytest tests/test_lastfm.py -q`
Expected: FAIL with missing `Scrobble` / methods.

- [ ] **Step 3: Implement the client additions**

Add imports and the dataclass:

```python
import math
from datetime import datetime, timezone

@dataclass(frozen=True)
class Scrobble:
    """One scrobble from a member's recent listening history."""

    artist: str
    track: str
    played_at: datetime
    weight: float = 1.0
```

Add a parser:

```python
def _parse_scrobble(row: Any) -> Scrobble | None:
    if not isinstance(row, Mapping):
        return None
    raw_artist = row.get("artist")
    if isinstance(raw_artist, Mapping):
        artist = _as_text(raw_artist.get("#text") or raw_artist.get("name"))
    else:
        artist = _as_text(raw_artist)
    track = _as_text(row.get("name"))
    date = row.get("date")
    if not isinstance(date, Mapping):
        return None
    timestamp = _as_int(date.get("uts"), -1)
    if not artist or not track or timestamp < 0:
        return None
    return Scrobble(
        artist=artist,
        track=track,
        played_at=datetime.fromtimestamp(timestamp, timezone.utc),
    )
```

Add the method:

```python
    async def get_recent_tracks(
        self,
        username: str,
        *,
        since: datetime,
        until: datetime,
        max_tracks: int = 1000,
    ) -> tuple[Scrobble, ...]:
        page_size = 200
        max_pages = max(1, math.ceil(max(1, int(max_tracks)) / page_size))
        base = {
            "method": "user.getrecenttracks",
            "user": username,
            "from": int(since.timestamp()),
            "to": int(until.timestamp()),
            "limit": page_size,
        }
        payload = await self._request({**base, "page": 1})
        section = payload.get("recenttracks")
        if not isinstance(section, Mapping):
            return ()
        attr = section.get("@attr")
        total = _as_int(attr.get("total")) if isinstance(attr, Mapping) else 0
        total_pages = max(1, _as_int(attr.get("totalPages"), 1) if isinstance(attr, Mapping) else 1)

        if total_pages <= max_pages:
            pages = list(range(2, total_pages + 1))
        elif max_pages == 1:
            pages = []
        else:
            pages = sorted(
                {
                    max(1, min(total_pages, round(1 + index * (total_pages - 1) / (max_pages - 1))))
                    for index in range(1, max_pages)
                }
            )

        rows: list[Any] = []
        section_rows = section.get("track") or []
        if isinstance(section_rows, Mapping):
            section_rows = [section_rows]
        rows.extend(section_rows if isinstance(section_rows, (list, tuple)) else [])

        for page in pages:
            try:
                extra = await self._request({**base, "page": page})
            except (LastFMError, LastFMUnavailable) as exc:
                log.debug("Partial recent-track sample for %s: %s", username, exc)
                break
            extra_section = extra.get("recenttracks")
            if not isinstance(extra_section, Mapping):
                continue
            extra_rows = extra_section.get("track") or []
            if isinstance(extra_rows, Mapping):
                extra_rows = [extra_rows]
            if isinstance(extra_rows, (list, tuple)):
                rows.extend(extra_rows)

        parsed: list[Scrobble] = []
        seen: set[tuple[str, str, datetime]] = set()
        for row in rows:
            scrobble = _parse_scrobble(row)
            if scrobble is None:
                continue
            key = (scrobble.artist, scrobble.track, scrobble.played_at)
            if key in seen:
                continue
            seen.add(key)
            parsed.append(scrobble)
        parsed.sort(key=lambda item: item.played_at)
        weight = total / len(parsed) if total > len(parsed) and parsed else 1.0
        return tuple(
            Scrobble(item.artist, item.track, item.played_at, weight) for item in parsed
        )

    async def get_artist_top_tags(
        self, artist: str, *, limit: int = 3
    ) -> tuple[tuple[str, int], ...]:
        payload = await self._request(
            {"method": "artist.gettoptags", "artist": artist, "autocorrect": 1}
        )
        section = payload.get("toptags")
        if not isinstance(section, Mapping):
            return ()
        rows = section.get("tag") or []
        if isinstance(rows, Mapping):
            rows = [rows]
        if not isinstance(rows, (list, tuple)):
            return ()
        tags: list[tuple[str, int]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            name = _as_text(row.get("name"))
            count = _as_int(row.get("count"))
            if name and count > 0:
                tags.append((name, count))
            if len(tags) >= max(1, int(limit)):
                break
        return tuple(tags)
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_lastfm.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lastfmsummary/lastfm.py tests/test_lastfm.py
git commit -m "feat: fetch sampled scrobbles and artist tags"
```

---

### Task 3: Cached artist-tag resolution

**Files:**
- Create: `lastfmsummary/tags.py`
- Test: `tests/test_tags.py`

- [ ] **Step 1: Write failing tests**

```python
import time

import pytest

from lastfmsummary.tags import resolve_artist_tags


class FakeClient:
    def __init__(self, results):
        self.results = results
        self.calls = []

    async def get_artist_top_tags(self, artist, *, limit=3):
        self.calls.append(artist)
        value = self.results[artist]
        if isinstance(value, BaseException):
            raise value
        return value


@pytest.mark.asyncio
async def test_resolve_artist_tags_caches_hits_and_misses(tmp_path):
    cache = tmp_path / "tags.json"
    first = FakeClient({"Radiohead": (("art rock", 100),), "Unknown": ()})
    result = await resolve_artist_tags(
        ["Radiohead", "Unknown"], client=first, cache_path=cache
    )
    assert result["radiohead"] == (("art rock", 100),)
    assert result["unknown"] == ()
    assert cache.exists()

    second = FakeClient({"Radiohead": pytest.fail, "Unknown": pytest.fail})
    cached = await resolve_artist_tags(
        ["Radiohead", "Unknown"], client=second, cache_path=cache
    )
    assert cached == result
    assert second.calls == []


@pytest.mark.asyncio
async def test_resolve_artist_tags_does_not_cache_errors(tmp_path):
    cache = tmp_path / "tags.json"
    failing = FakeClient({"Radiohead": RuntimeError("down")})
    result = await resolve_artist_tags(
        ["Radiohead"], client=failing, cache_path=cache
    )
    assert result == {"radiohead": ()}

    good = FakeClient({"Radiohead": (("art rock", 100),)})
    retried = await resolve_artist_tags(
        ["Radiohead"], client=good, cache_path=cache
    )
    assert retried["radiohead"] == (("art rock", 100),)
    assert good.calls == ["Radiohead"]


@pytest.mark.asyncio
async def test_resolve_artist_tags_expires_entries(tmp_path):
    cache = tmp_path / "tags.json"
    cache.write_text(
        '{"radiohead":{"tags":[["old",1]],"expires":1}}', encoding="utf-8"
    )
    client = FakeClient({"Radiohead": (("art rock", 100),)})
    result = await resolve_artist_tags(
        ["Radiohead"], client=client, cache_path=cache
    )
    assert result["radiohead"] == (("art rock", 100),)
    assert client.calls == ["Radiohead"]


@pytest.mark.asyncio
async def test_resolve_artist_tags_ignores_corrupt_cache(tmp_path):
    cache = tmp_path / "tags.json"
    cache.write_text("not json", encoding="utf-8")
    client = FakeClient({"Radiohead": (("art rock", 100),)})
    result = await resolve_artist_tags(
        ["Radiohead"], client=client, cache_path=cache
    )
    assert result["radiohead"] == (("art rock", 100),)
```

- [ ] **Step 2: Run the tests and confirm failure**

Run: `pytest tests/test_tags.py -q`
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement `tags.py`**

Create a module with:

```python
CACHE_TTL = 30 * 24 * 60 * 60
MISS_CACHE_TTL = 7 * 24 * 60 * 60
MAX_CACHE_ENTRIES = 5000


def artist_key(name: str) -> str:
    return " ".join(str(name or "").split()).casefold()


def _load_cache(path):
    if path is None:
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, Mapping):
        return {}
    now = time.time()
    cache = {}
    for key, value in raw.items():
        if not isinstance(value, Mapping):
            continue
        expires = value.get("expires")
        rows = value.get("tags")
        if not isinstance(expires, (int, float)) or expires <= now:
            continue
        tags = []
        if isinstance(rows, (list, tuple)):
            for row in rows:
                if not isinstance(row, (list, tuple)) or len(row) < 2:
                    continue
                name = " ".join(str(row[0]).split())
                try:
                    count = int(row[1])
                except (TypeError, ValueError):
                    continue
                if name and count > 0:
                    tags.append((name, count))
        cache[str(key)] = {"tags": tuple(tags), "expires": float(expires)}
    return cache


def _save_cache(path, cache):
    if path is None:
        return
    now = time.time()
    entries = {
        key: {
            "tags": [[name, count] for name, count in value["tags"]],
            "expires": float(value["expires"]),
        }
        for key, value in cache.items()
        if float(value.get("expires", 0)) > now
    }
    if len(entries) > MAX_CACHE_ENTRIES:
        newest = sorted(entries.items(), key=lambda item: item[1]["expires"], reverse=True)
        entries = dict(newest[:MAX_CACHE_ENTRIES])
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp")
        temporary.write_text(json.dumps(entries, separators=(",", ":")), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        log.debug("Could not write artist tag cache %s", path, exc_info=True)


async def resolve_artist_tags(names, *, client, cache_path=None, limit=3, concurrency=4):
    unique = {}
    for name in names:
        clean = " ".join(str(name or "").split())
        key = artist_key(clean)
        if clean and key:
            unique.setdefault(key, clean)
    if not unique:
        return {}

    cache = await asyncio.to_thread(_load_cache, cache_path)
    results = {key: cache[key]["tags"] for key in unique if key in cache}
    missing = {key: name for key, name in unique.items() if key not in results}
    if not missing:
        return results

    semaphore = asyncio.Semaphore(max(1, int(concurrency)))
    changed = False

    async def one(key, name):
        nonlocal changed
        async with semaphore:
            try:
                tags = await client.get_artist_top_tags(name, limit=limit)
            except (LastFMError, LastFMUnavailable) as exc:
                log.debug("Could not fetch tags for %s: %s", name, exc)
                results[key] = ()
                return
            except Exception:
                log.exception("Unexpected tag lookup failure for %s", name)
                results[key] = ()
                return
        results[key] = tags
        cache[key] = {
            "tags": tags,
            "expires": time.time() + (CACHE_TTL if tags else MISS_CACHE_TTL),
        }
        changed = True

    await asyncio.gather(*(one(key, name) for key, name in missing.items()))
    if changed:
        await asyncio.to_thread(_save_cache, cache_path, cache)
    return results
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_tags.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lastfmsummary/tags.py tests/test_tags.py
git commit -m "feat: cache Last.fm artist tag lookups"
```

---

### Task 4: Pure listening analytics

**Files:**
- Create: `lastfmsummary/listening.py`
- Test: `tests/test_listening.py`

- [ ] **Step 1: Write failing tests**

```python
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
    clock = build_listening_clock(
        [scrobble("A", 4), scrobble("B", 9)], tz_name="UTC"
    )
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
        Scrobble("Radiohead", "Airbag", start + timedelta(days=3), 1.0),
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
```

- [ ] **Step 2: Run the tests and confirm failure**

Run: `pytest tests/test_listening.py -q`
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement `listening.py`**

Include:

```python
NOISE_TAGS = frozenset(
    {
        "seen live", "favorites", "favourite", "favorite", "love", "loved",
        "awesome", "beautiful", "spotify", "owned", "want to see live",
        "under 2000 listeners",
    }
)


def text_key(value: str) -> str:
    return " ".join(str(value or "").split()).casefold()


def _timezone(tz_name: str):
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


@dataclass(frozen=True)
class ListeningClock:
    hourly: tuple[float, ...]
    busiest_hour: int
    busiest_count: int
    total: int
    sampled: int


@dataclass(frozen=True)
class TagSeries:
    name: str
    values: tuple[float, ...]


@dataclass(frozen=True)
class GenreTimeline:
    labels: tuple[str, ...]
    series: tuple[TagSeries, ...]


def build_listening_clock(scrobbles, *, tz_name):
    if not scrobbles:
        return None
    tz = _timezone(tz_name)
    hourly = [0.0] * 24
    total = 0.0
    for item in scrobbles:
        played_at = item.played_at
        if played_at.tzinfo is None:
            played_at = played_at.replace(tzinfo=timezone.utc)
        weight = max(0.0, float(item.weight))
        hourly[played_at.astimezone(tz).hour] += weight
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


def build_genre_timeline(scrobbles, tags_by_artist, *, start, end, tz_name, bucket_count=6):
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
        selected = [(name, count) for name, count in tags if text_key(name) not in NOISE_TAGS and count > 0]
        if not selected:
            continue
        total_count = sum(count for _, count in selected)
        played_at = item.played_at
        if played_at.tzinfo is None:
            played_at = played_at.replace(tzinfo=timezone.utc)
        index = int((played_at.timestamp() - start_ts) / span * bucket_count)
        index = max(0, min(bucket_count - 1, index))
        for name, count in selected:
            key = text_key(name)
            display.setdefault(key, name)
            values = buckets.setdefault(key, [0.0] * bucket_count)
            values[index] += max(0.0, float(item.weight)) * count / total_count

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
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_listening.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lastfmsummary/listening.py tests/test_listening.py
git commit -m "feat: derive listening clock and genre timeline"
```

---

### Task 5: Draw the two charts and integrate the renderer

**Files:**
- Create: `lastfmsummary/charts.py`
- Modify: `lastfmsummary/render.py`
- Test: `tests/test_render.py`

- [ ] **Step 1: Write failing render tests**

Add sample helpers and tests:

```python
from lastfmsummary.listening import GenreTimeline, ListeningClock, TagSeries


def sample_genres():
    return GenreTimeline(
        labels=("10 Sep", "17 Sep", "24 Sep", "01 Oct", "08 Oct", "15 Oct"),
        series=(
            TagSeries("electronic", (4, 6, 5, 7, 6, 8)),
            TagSeries("indie pop", (2, 3, 4, 2, 5, 3)),
            TagSeries("shoegaze", (1, 2, 1, 3, 2, 1)),
        ),
    )


def sample_clock():
    hourly = tuple(0.0 if hour != 17 else 96.0 for hour in range(24))
    return ListeningClock(
        hourly=hourly,
        busiest_hour=17,
        busiest_count=96,
        total=1000,
        sampled=1000,
    )


def test_render_extras_grow_the_image_and_stay_deterministic():
    base = render(sample_tiles(9), 3)
    with_extras = render(
        sample_tiles(9), 3, genres=sample_genres(), clock=sample_clock()
    )
    again = render(
        sample_tiles(9), 3, genres=sample_genres(), clock=sample_clock()
    )
    with Image.open(io.BytesIO(base)) as image:
        base_size = image.size
    with Image.open(io.BytesIO(with_extras)) as image:
        assert image.size[0] == base_size[0]
        assert image.size[1] > base_size[1]
    assert with_extras == again


def test_render_extras_support_one_chart_and_small_grids():
    only_tags = render(sample_tiles(4), 2, genres=sample_genres())
    only_clock = render(sample_tiles(4), 2, clock=sample_clock())
    both = render(sample_tiles(4), 2, genres=sample_genres(), clock=sample_clock())
    assert only_tags.startswith(b"\x89PNG")
    assert only_clock.startswith(b"\x89PNG")
    assert both.startswith(b"\x89PNG")


def test_render_extras_handle_a_single_tag_and_sampled_note():
    genres = GenreTimeline(
        labels=("01 Oct", "02 Oct"),
        series=(TagSeries("a very long genre name that should still fit", (3, 5)),),
    )
    clock = ListeningClock(
        hourly=tuple(1.0 for _ in range(24)),
        busiest_hour=0,
        busiest_count=2,
        total=5000,
        sampled=1000,
    )
    data = render(sample_tiles(4), 2, genres=genres, clock=clock)
    assert data.startswith(b"\x89PNG")
```

Update the `render()` helper signature to forward `genres` and `clock`.

- [ ] **Step 2: Run the tests and confirm failure**

Run: `pytest tests/test_render.py -q`
Expected: TypeError for unexpected keyword arguments.

- [ ] **Step 3: Create `charts.py` and wire `render.py`**

`charts.py` responsibilities:

- `draw_section_title(draw, *, left, right, top, title, font) -> int` draws the
  uppercase title, a 1px rule below it, and returns the consumed height
  (`font.getmetrics()` sum + 16).
- `draw_genre_chart(draw, timeline, *, left, top, right, bottom, tick_y,
  label_font, tick_font)`:
  - compute one x anchor per bucket;
  - interpolate each series with Catmull-Rom, clamp at zero;
  - draw a centered streamgraph band per series with the palette;
  - place white band labels at the best of 40%, 55%, 70% width, skipping
    labels that do not fit or overlap;
  - draw `%d %b` tick labels centered below the plot.
- `draw_listening_clock(canvas, draw, *, left, top, diameter, stats_left,
  stats_right, clock, value_font, label_font, small_font)`:
  - render a 4x dial with a dark track and 13-degree wedges with 2-degree
    gaps, downsample, and alpha-composite;
  - draw `00/06/12/18` inside the hole;
  - draw the busiest-hour and scrobble-count stats in 12-hour format;
  - add `Estimated from N of M scrobbles` when `clock.total > clock.sampled`.

`render.py` changes:

- import the chart helpers;
- add `genres=None, clock=None` keyword parameters;
- load section fonts sized from the canvas width;
- add section heights before the footer height calculation;
- draw the sections after the grid and before the footer.

Concrete sizes:

```python
insights_gap = max(24, int(cover * 0.12))
plot_h = max(190, min(360, int(width * 0.28)))
dial = max(190, min(340, int(width * 0.30)))
section_font = load_font(max(16, min(30, int(width * 0.024))), bold=True)
band_font = load_font(max(11, min(18, int(width * 0.015))), bold=True)
tick_font = load_font(max(10, min(15, int(width * 0.012))))
stat_label_font = load_font(max(13, min(18, int(width * 0.016))))
stat_value_font = load_font(max(26, min(46, int(width * 0.045))), bold=True)
note_font = load_font(max(10, min(14, int(width * 0.012))))
```

Use `_draw_centered`-style centering for band and tick labels. Bump the
user-agent in `render.py` to 1.3.

- [ ] **Step 4: Run the render tests and inspect an image**

Run: `pytest tests/test_render.py -q`
Expected: PASS.

Also render a local sample to `C:\Users\daniel\AppData\Local\Temp\opencode\insights-preview.png`
and open it in an image viewer to confirm the static layout.

- [ ] **Step 5: Commit**

```bash
git add lastfmsummary/charts.py lastfmsummary/render.py tests/test_render.py
git commit -m "feat: draw genre timeline and listening clock"
```

---

### Task 6: Cog integration

**Files:**
- Modify: `lastfmsummary/cog.py`
- Test: `tests/test_cog.py`

- [ ] **Step 1: Write failing tests**

Add one success test and one failure test:

```python
@pytest.mark.asyncio
async def test_post_charts_passes_insights_to_renderer(cog, monkeypatch, tmp_path):
    chart = MemberChart("alice", (Entry("Slowdive", None, 10, None),))

    class FakeClient:
        def __init__(self, key):
            pass

        async def fetch_charts(self, usernames, **kwargs):
            return [chart]

        async def get_recent_tracks(self, username, **kwargs):
            return (
                Scrobble("Slowdive", "Alison", datetime(2026, 10, 1, 17, tzinfo=timezone.utc), 1.0),
                Scrobble("Slowdive", "When the Sun Hits", datetime(2026, 10, 2, 18, tzinfo=timezone.utc), 1.0),
            )

        async def close(self):
            return None

    async def fake_key():
        return "key"

    async def fake_tags(names, **kwargs):
        return {"slowdive": (("shoegaze", 100),)}

    async def fake_covers(urls, **kwargs):
        return {}

    captured = {}

    def fake_render(**kwargs):
        captured.update(kwargs)
        return PNG

    monkeypatch.setattr("lastfmsummary.cog.LastFMClient", FakeClient)
    monkeypatch.setattr(cog, "_api_key", fake_key)
    monkeypatch.setattr("lastfmsummary.cog.resolve_artist_tags", fake_tags)
    monkeypatch.setattr(LastFMSummary, "_cache_dir", property(lambda self: tmp_path))
    monkeypatch.setattr("lastfmsummary.cog.download_covers", fake_covers)
    monkeypatch.setattr("lastfmsummary.cog.render_grid", fake_render)

    report = await cog._post_charts(fake_guild(FakeChannel()), FakeChannel(), settings())
    assert report.posted == 1
    assert captured["genres"] is not None
    assert captured["clock"] is not None
    assert captured["clock"].busiest_hour == 15  # 17 UTC in default UTC settings


@pytest.mark.asyncio
async def test_post_charts_survives_insight_failure(cog, monkeypatch, tmp_path):
    chart = MemberChart("alice", (Entry("Slowdive", None, 10, None),))

    class FakeClient:
        def __init__(self, key):
            pass

        async def fetch_charts(self, usernames, **kwargs):
            return [chart]

        async def get_recent_tracks(self, username, **kwargs):
            raise RuntimeError("history down")

        async def close(self):
            return None

    async def fake_key():
        return "key"

    async def fake_covers(urls, **kwargs):
        return {}

    captured = {}

    def fake_render(**kwargs):
        captured.update(kwargs)
        return PNG

    monkeypatch.setattr("lastfmsummary.cog.LastFMClient", FakeClient)
    monkeypatch.setattr(cog, "_api_key", fake_key)
    monkeypatch.setattr(LastFMSummary, "_cache_dir", property(lambda self: tmp_path))
    monkeypatch.setattr("lastfmsummary.cog.download_covers", fake_covers)
    monkeypatch.setattr("lastfmsummary.cog.render_grid", fake_render)

    report = await cog._post_charts(fake_guild(FakeChannel()), FakeChannel(), settings())
    assert report.posted == 1
    assert captured["genres"] is None
    assert captured["clock"] is None
```

Import `Scrobble` at the top.

- [ ] **Step 2: Run the tests and confirm failure**

Run: `pytest tests/test_cog.py::test_post_charts_passes_insights_to_renderer -q`
Expected: FAIL because `genres`/`clock` are not passed.

- [ ] **Step 3: Implement insights collection**

- import `PERIOD_DAYS`, `GenreTimeline`, `ListeningClock`,
  `build_genre_timeline`, `build_listening_clock`, `resolve_artist_tags`,
  and `text_key`;
- add `MAX_INSIGHT_SCROBBLES = 1000` and `MAX_TAG_ARTISTS = 30`;
- add `_collect_insights(client, charts, *, period, tz_name)`:

```python
now = datetime.now(timezone.utc)
start = now - timedelta(days=PERIOD_DAYS.get(period, 30))
semaphore = asyncio.Semaphore(3)
scrobbles = {}
display = {}

async def one(chart):
    async with semaphore:
        try:
            tracks = await client.get_recent_tracks(
                chart.username,
                since=start,
                until=now,
                max_tracks=MAX_INSIGHT_SCROBBLES,
            )
        except (LastFMError, LastFMUnavailable) as exc:
            log.warning("Could not fetch recent tracks for %s: %s", chart.username, exc)
            return
        except Exception:
            log.exception("Unexpected recent-track failure for %s", chart.username)
            return
    if tracks:
        scrobbles[chart.username] = tracks

await asyncio.gather(*(one(chart) for chart in charts))
if not scrobbles:
    return {}

top_artists = {}
for username, tracks in scrobbles.items():
    weights = {}
    for item in tracks:
        key = text_key(item.artist)
        weights[key] = weights.get(key, 0.0) + max(0.0, float(item.weight))
        display.setdefault(key, item.artist)
    top_artists[username] = {
        key for key, _ in sorted(weights.items(), key=lambda pair: pair[1], reverse=True)[:MAX_TAG_ARTISTS]
    }

wanted = {key: display[key] for keys in top_artists.values() for key in keys}
try:
    tags_by_artist = await resolve_artist_tags(
        wanted.values(),
        client=client,
        cache_path=self._cache_dir / "artist-tags.json",
    )
except Exception:
    log.exception("Could not resolve artist tags")
    tags_by_artist = {}

bucket_count = 7 if period == "weekly" else 6
insights = {}
for username, tracks in scrobbles.items():
    clock = None
    timeline = None
    try:
        clock = build_listening_clock(tracks, tz_name=tz_name)
    except Exception:
        log.exception("Could not build listening clock for %s", username)
    if tags_by_artist:
        try:
            timeline = build_genre_timeline(
                tracks,
                tags_by_artist,
                start=start,
                end=now,
                tz_name=tz_name,
                bucket_count=bucket_count,
            )
        except Exception:
            log.exception("Could not build genre timeline for %s", username)
    insights[username] = (timeline, clock)
return insights
```

- in `_post_charts`, collect insights inside the client `try` block and wrap
  the call in its own `try/except Exception` that logs and uses `{}`;
- in `_post_chart`, add `genres=None, clock=None` parameters and pass them to
  `render_grid`;
- in the posting loop, unpack `insights.get(username, (None, None))`.

- [ ] **Step 4: Run the cog tests**

Run: `pytest tests/test_cog.py -q`
Expected: PASS (existing fakes simply return no scrobbles).

- [ ] **Step 5: Commit**

```bash
git add lastfmsummary/cog.py tests/test_cog.py
git commit -m "feat: attach listening insights to member charts"
```

---

### Task 7: Docs, versioning, and sample image

**Files:**
- Modify: `README.md`, `pyproject.toml`, `lastfmsummary/info.json`, `info.json`
- Modify: `lastfmsummary/cog.py`, `lastfmsummary/lastfm.py`, `lastfmsummary/render.py`, `lastfmsummary/serp.py`, `lastfmsummary/deezer.py`
- Create: `docs/sample-insights.png`

- [ ] **Step 1: Update versions and user agents**

Set `1.3.0` in `pyproject.toml`, `LastFMSummary.__version__`, and every
`lastfm-summary-cog/1.2` user-agent string.

- [ ] **Step 2: Update the README**

- Add feature bullets for the genre streamgraph and listening clock.
- Add `![Example insights](docs/sample-insights.png)` and a
  "Genres and listening clock" section explaining tags, sampling, timezone,
  and graceful degradation.
- Add privacy notes: timestamps are memory-only; artist names and tags are
  cached.
- Add troubleshooting entries for missing insights.
- Update the test count if it changes.

- [ ] **Step 3: Update `info.json` descriptions**

Mention the genre timeline and listening clock.

- [ ] **Step 4: Generate the sample image**

Use a temporary Python script (not committed) that calls `render_grid` with
nine generated tiles and representative `GenreTimeline`/`ListeningClock`
objects. Save it as `docs/sample-insights.png`.

- [ ] **Step 5: Run the full verification**

Run:

```bash
pytest -q
ruff check .
ruff format --check .
```

Expected: all tests pass, ruff clean.

- [ ] **Step 6: Commit**

```bash
git add README.md pyproject.toml info.json lastfmsummary docs/sample-insights.png
git commit -m "docs: document listening insights and bump version"
```

---

## Self-Review Checklist

- [ ] Every spec section maps to a task: period days (1), scrobbles/tags (2),
      tag cache (3), analytics (4), rendering (5), cog (6), docs/version (7).
- [ ] No TBD/TODO placeholders.
- [ ] `ListeningClock`, `GenreTimeline`, `TagSeries`, `Scrobble`, and all
      function signatures match across tasks.
- [ ] Sampling weights, timezone conversion, noise filtering, top-seven
      selection, and graceful degradation are covered by tests.
- [ ] Existing rendering without extras keeps its dimensions.
