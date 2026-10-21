# Last.fm Summary — Genre Timeline and Listening Clock

Date: 2026-10-21
Status: approved for implementation

## Summary

Add a second visual section below each member's artist or album grid:

1. **TOP TAGS** — a streamgraph of the member's most-listened Last.fm tags
   (genres) across the selected period.
2. **LISTENING CLOCK** — a 24-hour radial chart of scrobble volume, with the
   busiest hour and its scrobble count.

Both charts are drawn into the existing PNG, below the grid and above the
footer. They are computed from a sample of the member's scrobbles in the same
window as the grid. The section is best-effort: when scrobble or tag data is
unavailable, the grid still posts, with the available charts or none.

## Goals

- Show genre movement across the configured period, using Last.fm tags.
- Show when the member listens during the day, in the guild's timezone.
- Keep image generation dependency-free beyond Pillow.
- Bound Last.fm API usage for long periods and heavy listeners.
- Never let insights failures prevent a grid from posting.
- Keep pure analytics testable without Discord, network, or image rendering.

## Non-goals

- Exact per-scrobble accounting for very heavy listeners. Counts are scaled
  from an even sample when the window contains more than the cap.
- Track-level charts or a track list.
- Server-wide aggregate insights. Every linked member gets their own charts.
- New commands, per-guild toggles, or configurable chart themes.
- Persisting scrobble history, timestamps, or listening statistics.

## Data window and sampling

`schedule.py` gains:

```python
PERIOD_DAYS = {
    "weekly": 7,
    "monthly": 30,
    "quarterly": 90,
    "half-yearly": 180,
    "yearly": 365,
}
```

For one run, `end` is the run instant in UTC and
`start = end - timedelta(days=PERIOD_DAYS[period])`.

Sampling rule:

- Request 200 scrobbles per page, with `from`/`to` set to the window.
- Fetch the first page once to read `recenttracks.@attr.total` and
  `totalPages`.
- Allow at most `max(1, ceil(max_tracks / 200))` pages (five pages, 1,000
  rows, by default). If `totalPages` fits the budget, fetch every page.
  Otherwise pick page numbers spread evenly across `1..totalPages`, including
  the first and last page, and deduplicate them.
- Requests are sequential per member. Members are fetched with bounded
  concurrency so the client stays near Last.fm's rate limit.
- A failure after the first page is tolerated: the sample becomes partial and
  the remaining pages are skipped. A failure on the first page means no
  insights for that member.
- Rows without a `date.uts` value (including now-playing rows) are skipped.
  Rows are deduplicated by `(artist, track, timestamp)` and sorted oldest
  first.
- Each parsed scrobble gets `weight = total / parsed_count` when `total` is
  larger than the sample, where `total` is Last.fm's reported count for the
  window. Otherwise every weight is 1.0. The weight lets clock and tag totals
  scale back to the full window.

## Last.fm API additions

`lastfm.py`:

```python
@dataclass(frozen=True)
class Scrobble:
    artist: str
    track: str
    played_at: datetime  # aware, UTC
    weight: float = 1.0

async def get_recent_tracks(
    self,
    username: str,
    *,
    since: datetime,
    until: datetime,
    max_tracks: int = 1000,
) -> tuple[Scrobble, ...]: ...

async def get_artist_top_tags(
    self,
    artist: str,
    *,
    limit: int = 3,
) -> tuple[tuple[str, int], ...]: ...
```

- `user.getRecentTracks` is requested with `user`, `from`, `to`, `limit=200`,
  `page`, and the existing API key/format handling.
- `artist.getTopTags` is requested with `artist`, `autocorrect=1`, and returns
  non-empty tag names with positive integer counts, capped at `limit`.
- Both use the existing `_request` retry, error mapping, and session handling.
- Tags are fetched only for artists that appear in the member's sample.

## Tag resolution and cache

New `tags.py`:

```python
async def resolve_artist_tags(
    names: Sequence[str],
    *,
    client: LastFMClient,
    cache_path: Path | None = None,
    limit: int = 3,
    concurrency: int = 4,
) -> dict[str, tuple[tuple[str, int], ...]]: ...
```

- Keys are normalized with collapsed whitespace and `casefold()`.
- Only the top 30 artists per member (by weighted scrobble count) are
  requested; the union across members is resolved once per run.
- Disk cache: `cog_data_path()/artist-tags.json`, entries
  `{"tags": [["pop", 100], ...], "expires": <epoch>}`.
- Successful non-empty results expire after 30 days; empty results after
  7 days. API errors are not cached, so a later run retries.
- Corrupt or unreadable cache files are treated as empty. Writes are atomic
  (temporary file then replace) and capped at 5,000 entries.
- Lookups run with four concurrent requests. A failed artist is omitted from
  the result and logged at debug; other artists continue.

## Analytics

New `listening.py` (pure; no HTTP, Discord, or Pillow):

### Listening clock

`build_listening_clock(scrobbles, *, tz_name) -> ListeningClock | None`

- Convert each `played_at` to the guild timezone and bucket by local hour
  `0..23`, summing `weight`.
- `busiest_hour` is the first hour with the maximum value (earliest on a tie).
- `busiest_count` is `round(max(hourly))`.
- `sampled` is `len(scrobbles)`; `total` is `round(sum(weights))`.
- Returns `None` when there are no scrobbles.

```python
@dataclass(frozen=True)
class ListeningClock:
    hourly: tuple[float, ...]  # 24 weighted counts
    busiest_hour: int
    busiest_count: int
    total: int
    sampled: int
```

### Genre timeline

`build_genre_timeline(scrobbles, tags_by_artist, *, start, end, tz_name,
bucket_count) -> GenreTimeline | None`

- The cog passes 7 buckets for `weekly` (one per day) and 6 otherwise.
  Buckets are equal slices of `[start, end)`; a scrobble on the boundary
  belongs to the later bucket. Buckets are assigned from UTC timestamps.
- Labels are bucket start dates formatted `%d %b` in the guild timezone.
- Each artist's selected tags split one unit of affinity: for tags
  `t1..tn` with counts `c1..cn`, tag `ti` contributes
  `count_i / sum(counts)`. A scrobble adds
  `weight * affinity` to that tag's bucket.
- Tags are grouped case-insensitively with collapsed whitespace. Display uses
  the first spelling seen.
- Noise tags are dropped: `seen live`, `favorites`, `favourite`, `favorite`,
  `love`, `loved`, `awesome`, `beautiful`, `spotify`, `owned`,
  `want to see live`, `under 2000 listeners`.
- The seven tags with the largest total weight are kept, ordered largest
  first, and must have a positive total.
- Returns `None` when no tag has any weight.

```python
@dataclass(frozen=True)
class TagSeries:
    name: str
    values: tuple[float, ...]

@dataclass(frozen=True)
class GenreTimeline:
    labels: tuple[str, ...]
    series: tuple[TagSeries, ...]
```

## Rendering

`render.py` keeps `render_grid` as the entry point and adds two optional
keyword arguments:

```python
render_grid(..., genres: GenreTimeline | None = None,
            clock: ListeningClock | None = None) -> bytes
```

The existing layout is unchanged when both are `None`, so old dimensions and
tests remain valid.

When present, the new section is stacked below the grid and above the footer:

- Section gap and a hairline divider.
- **TOP TAGS**: uppercase title, title rule, streamgraph, date ticks.
  - Plot height `clamp(int(canvas_width * 0.28), 190, 360)`.
  - Centered streamgraph (stack midpoint is the plot center), smooth
    Catmull-Rom interpolation through one value per bucket.
  - Seven-color palette: lavender `#C39BF0`, teal `#62D3C4`, blue `#4A9FF5`,
    navy `#3E5CA0`, purple `#7B5BD6`, green `#4E9F86`, magenta `#C06CE0`.
  - Band labels are white bold text, placed at the widest of three horizontal
    positions, skipped when the band is too thin, and omitted when they would
    overlap an already drawn label.
  - Date labels are muted, centered under each bucket.
- **LISTENING CLOCK**: uppercase title, title rule, then a row with the dial
  on the left and stats on the right.
  - Dial diameter `clamp(int(canvas_width * 0.30), 190, 340)`.
  - Drawn at 4x and downsampled for smooth edges. Each hour is a 13° wedge
    with 2° gaps. A dark full-radius track sits behind the active wedges.
  - Wedge length is `hourly[h] / max(hourly)` of the ring width. The busiest
    hour is `#2B7FFF`; other non-zero hours blend from `#8FC1FF` to `#3B82F6`.
  - `00`, `06`, `12`, `18` are muted labels inside the hole, at top, right,
    bottom and left.
  - Stats: `Busiest hour` (muted) over `5:00PM` (large bold, 12-hour clock),
    then `Scrobbles in busiest hour` over the rounded count.
  - When `total > sampled`, a small muted note reports
    `Estimated from N of M scrobbles`.
- Section titles use the existing bundled Work Sans bold face.
- If only one dataset is present, only that chart is drawn. If both are
  absent, no section height is added.

## Cog integration

`cog.py`:

- After `fetch_charts` succeeds, with the client still open, call
  `_collect_insights(client, charts, period, timezone)`.
- `_collect_insights` returns `{username: (GenreTimeline | None,
  ListeningClock | None)}`.
- It fetches scrobbles for each chart with `asyncio.Semaphore(3)`, selects up
  to 30 artists per member by weighted plays, resolves the union of tags once,
  and builds the two datasets per member.
- Per-member scrobble or timeline failures are logged and produce `None` for
  that member. A tag-resolution failure still allows clocks to render.
  `_post_charts` wraps the whole collection so an unexpected failure produces
  an empty mapping and never blocks posting.
- `_post_chart` accepts `genres` and `clock` and forwards them to `render_grid`.
- No new Config keys. The feature follows the existing period and timezone.
- The window matches the grid's period; the date range text in the footer is
  unchanged.

## Privacy and API impact

- Scrobbles and timestamps are processed in memory and never written to disk.
- The only new persisted data is the artist-tag cache: artist names plus
  their Last.fm tags, with no Discord IDs or Last.fm usernames.
- The cog makes up to five recent-track requests per member per run and up to
  one tag request per unique top artist (cached for 30 days).
- No third-party service receives listening data; tag lookups go to Last.fm's
  existing public API.

## Error handling

- No scrobbles in the window: grid only.
- Fewer than two usable tags: clock only.
- Tag lookup failure: clock only.
- Scrobble lookup failure for one member: that member's grid posts without the
  section; other members are unaffected.
- Invalid stored timezone: existing settings sanitization falls back to UTC
  before insights are built.
- More than 1,000 scrobbles: sample is scaled; the image states the estimate.
- Partial page failures: the remaining sample is still scaled and used.
- Rendering must not fail on one tag, one scrobble, all-equal hours, or very
  long tag names (labels are skipped or ellipsized).

## Testing

- `test_schedule.py`: `PERIOD_DAYS` matches the configured windows.
- `test_lastfm.py`: recent-track parsing (including now-playing and malformed
  rows), even page selection, scaling weights, partial page failures, tag
  parsing and limits.
- `test_listening.py`: timezone hour bucketing, busiest-hour tie-breaking,
  weighted counts, bucket boundaries, tag affinity, top-seven selection, noise
  filtering, empty inputs.
- `test_tags.py`: cache hit/miss, expiry, empty-result caching, errors not
  cached, concurrency-safe results, corrupt cache recovery.
- `test_render.py`: dimensions are unchanged without extras and grow with
  extras; 2x2 and 8x8; streamgraph-only, clock-only, both; single tag;
  deterministic output.
- `test_cog.py`: insights reach the renderer; scrobble or tag failure still
  posts grids; existing posting behavior is unchanged.
- `ruff check` and `ruff format --check` pass.

## Documentation and versioning

- README: new feature bullets, a "Genres and listening clock" section,
  sampling and privacy notes, and troubleshooting entries for missing
  insights.
- Update both `info.json` descriptions.
- Bump `pyproject.toml`, `LastFMSummary.__version__`, and user-agent version
  strings to `1.3.0`.
- Add `docs/sample-insights.png` rendered from representative sample data.
