# Last.fm Summary Cog — Design

Date: 2026-10-21
Status: approved for implementation (assumptions recorded below)

## Summary

A Red-DiscordBot cog that lets server members register their Last.fm usernames and
periodically posts a top-artists (or top-albums) grid image for every linked
member into a configured channel. Server admins choose the chart period (weekly,
monthly, quarterly, half-yearly, yearly), posting time and timezone, grid size,
and data source.

## Goals

- Members can link, inspect, and remove their Last.fm username per server.
- Admins can configure channel, period, source (artists/albums), grid size,
  posting time, timezone, and enable/disable automatic posting.
- A background scheduler posts one rendered PNG chart per linked member on the
  configured cadence and survives cog reloads and bot restarts.
- Each image contains cover art, names (artist; or album + artist), ranks, and a
  header/footer with the member, server, period, and that member's total plays.
- Failures (missing key, API outage, no registrations, missing channel) are
  handled without crashing the scheduler and are visible to admins.
- Core logic is testable without a live Discord bot or Last.fm account.

## Non-goals

- A single combined or server-wide aggregate grid. Every linked member is posted
  separately, as clarified by the user.
- Scrobbling, OAuth, or Last.fm writes. Only public read endpoints are used.
- Daily charts: Last.fm has no 1-day top-chart period, and the required
  getRecentTracks pagination is out of scope.
- Backfilling missed posts after bot downtime (a missed run is skipped, never
  posted late in bulk).
- Custom fonts/themes uploaded by admins.

## Assumptions

The brief leaves several details open; these are the choices made and can be
changed later without redesigning the whole cog:

1. Every registered member gets their own chart. Charts are not merged across
   members; the clarification "for every registered user, a chart should be
   posted" defines this behavior.
2. Period is both the chart window and the posting cadence:
   weekly → 7day/7 days, monthly → 1month, quarterly → 3month, half-yearly →
   6month, yearly → 12month.
3. Grids are square N×N with N from 2 to 8 (default 3), so 3×3 is the default.
4. A Last.fm API key is required. It is set once by the bot owner
   (`[p]lastfmset api_key`) or supplied through the `LASTFM_API_KEY`
   environment variable.
5. Registration is per guild. The same person can register different usernames
   in different servers, and removing registration in one server does not affect
   another.
6. Default schedule: Monday 09:00 in UTC for weekly, the 1st at 09:00 UTC for
   the other periods.

## Commands

Both groups are hybrid commands so they work as text commands and slash
commands after `[p]slash sync`.

### Members — `[p]lastfm` (alias `lfm`)

| Command | Description |
| --- | --- |
| `lastfm register <username>` | Validate the username through `user.getInfo`, then store it for the author in this guild. |
| `lastfm unregister` | Remove the author's registration. |
| `lastfm profile [member]` | Show a member's linked username, profile link, and registration date. |
| `lastfm members` | List linked members (up to 25 shown, with a total count). |
| `lastfm status` | Show the guild's schedule and whether the caller is registered. |

`lastfm` without a subcommand prints a short help/status summary.

### Admins — `[p]lastfmset` (alias `lfmset`)

All commands require `manage_guild` (or Red admin).

| Command | Description |
| --- | --- |
| `lastfmset channel [#channel\|none]` | Set or clear the output channel. Enabling requires a channel. |
| `lastfmset period <weekly\|monthly\|quarterly\|half-yearly\|yearly>` | Set chart window and cadence; recomputes the next run. |
| `lastfmset source <artists\|albums>` | Set grid source. |
| `lastfmset grid <2-8>` | Set square grid size. |
| `lastfmset time <HH:MM>` | Set local posting time. |
| `lastfmset timezone <IANA name>` | Set the schedule timezone (e.g. `Europe/Berlin`). |
| `lastfmset day <monday..sunday\|1..31>` | Anchor day: weekday for weekly, day of month otherwise. |
| `lastfmset enable` / `lastfmset disable` | Toggle automatic posting. |
| `lastfmset preview [member]` | Render and post charts now (one member or all linked members) without changing the schedule. |
| `lastfmset status` | Show full configuration, next run, member count, and last error. |
| `lastfmset api_key <key>` | Owner only. Store the Last.fm API key globally. |

## Architecture

```
lastfmsummary/
  __init__.py          setup() entry point
  info.json            Red cog metadata + requirements
  cog.py               LastFMSummary cog: Config, commands, scheduler, posting
  lastfm.py            async Last.fm client, typed records, errors
  schedule.py          pure timezone/period math (next run, period labels)
  render.py            pure Pillow grid renderer and image acquisition helpers
tests/
  test_schedule.py
  test_lastfm.py
  test_render.py
  test_cog.py          skipped when Red is not importable
```

Units and boundaries:

- `schedule.py` has no Discord/Last.fm imports; it converts config into a UTC
  `datetime` and describes the period window.
- `lastfm.py` owns HTTP, JSON validation and retries. It returns
  plain dataclasses. It does not know about guilds or config.
- `render.py` owns downloading/caching cover art and drawing. It accepts plain
  `GridEntry` records and returns PNG bytes.
- `cog.py` glues config, commands, scheduler, and Discord posting. This is the
  only module that imports Red.

Data flow for a scheduled post:

```
scheduler tick → due guilds → read config + registrations → Last.fm top
lists per member (bounded concurrency) → take each member's top N² →
download covers → render one PNG per member → post embed+file each →
persist run times
```

## Configuration

Global (bot-wide):

```python
api_key: str = ""     # fallback: LASTFM_API_KEY env var
```

Guild:

```python
{
    "channel_id": None,
    "enabled": False,
    "period": "monthly",
    "source": "artists",
    "grid_size": 3,
    "time": "09:00",
    "timezone": "UTC",
    "weekday": 0,        # Monday=0 for weekly
    "day": 1,            # 1-31 for other periods, clamped to month length
    "next_run_at": None, # ISO-8601 UTC
    "last_run_at": None,
    "last_error": None,
    "registrations": {
        "<user_id>": {"username": "...", "registered_at": "ISO-8601 UTC"}
    },
}
```

`force_registration=True` is used so every key is explicit. Config writes update
`next_run_at` whenever a scheduling input changes.

## Scheduling

- Pure functions in `schedule.py`:
  - `parse_time("HH:MM") -> time`
  - `compute_next_run(period, *, now_utc, tz_name, at, weekday, day) -> datetime`
  - `period_label(period) -> str` and `period_api_value(period) -> str`
- `timezone` is resolved with `zoneinfo`; `tzdata` is a requirement for hosts
  without a system timezone database (notably Windows).
- Weekly: next occurrence of `weekday` at local `time`, strictly after `now`.
- Monthly: next month whose `day` (clamped to the month's last day) is at or
  after today, at local `time`.
- Quarterly: same, restricted to January/April/July/October.
- Half-yearly: same, restricted to January/July.
- Yearly: next January.
- DST: local wall-clock time is chosen, then converted to UTC. Nonexistent or
  ambiguous times resolve with `fold=0` and the converted instant is used;
  no duplicate run occurs because `next_run_at` must be in the past to fire.
- The cog runs a `discord.ext.tasks.loop(seconds=30)` (started in `cog_load`,
  cancelled in `cog_unload`). Each tick:
  - selects enabled guilds with a channel and registrations and
    `next_run_at <= now`;
  - posts, then stores `last_run_at`, recomputes `next_run_at` from now, and
    records `last_error=None`;
  - if a post failed, stores the error and still advances `next_run_at` so one
    broken run cannot loop forever.
- Overdue runs are only posted if they are at most 6 hours late. Older overdue
  runs are skipped and rescheduled, avoiding a burst after downtime.
- A per-guild `asyncio.Lock` prevents a manual preview and a scheduled run from
  posting concurrently. The scheduler tick tolerates one guild failing without
  affecting others.

## Last.fm integration

Endpoints (JSON, `format=json`):

- `user.getInfo` — username validation and canonical display name.
- `user.getTopArtists` — `user`, `period`, `limit`, `api_key`.
- `user.getTopAlbums` — same parameters; album records include artist name.

Implementation details:

- One `aiohttp.ClientSession` per generation, 15 s timeout, custom User-Agent.
- Bounded concurrency (`asyncio.Semaphore(4)`) across members; Last.fm allows
  roughly 5 requests/second.
- Each member requests enough entries to fill the grid for that member:
  `min(200, max(50, grid_size**2 * 5))`.
- Retry twice with exponential backoff (0.5 s, 1.0 s) on HTTP 429/5xx and
  Last.fm error codes 11/16/29. No retry on invalid key/user.
- Error mapping: `LastFMError(code, message)`; codes 6/7 → unknown user,
  10/26 → invalid key, 29 → rate limited. HTTP/JSON failures become
  `LastFMUnavailable`.
- Chart preparation:
  - each member's chart keeps Last.fm's rank order;
  - only the top N² entries are rendered, ranked 1…N²;
  - the play total shown in the footer covers all fetched entries;
  - a failed member does not affect any other member's chart.
- Last.fm placeholder art is detected (empty URL, the known
  `2a96cbd8b46e442fc41c2b86b821562f` hash, or a URL containing
  `lastfm_img/default`). Placeholder entries render a generated tile.

## Rendering

- Pillow only, no external image binaries.
- Canvas: dark background `#101014`; subtle tile surface `#1B1B22`; white/grey
  text; an accent derived from the artwork is intentionally avoided to keep the
  output consistent.
- Layout: fixed padding, header (title + subtitle), N×N covers, footer.
  - Cover size: 300 px for N≤4, 220 px for N≤6, 170 px for N≤8.
  - Caption height: 20% of cover height, minimum 44 px.
  - Header: title (e.g. "Top Artists — Monthly"), subtitle with the member,
    their Last.fm handle, and the server name.
  - Footer: "X plays • <date range> • Data from Last.fm" and "last.fm".
- Each tile: rounded rectangle mask, center-crop cover, rank badge, caption bar
  with name (wrapped to at most 2 lines, ellipsized) and, for albums, artist
  name in smaller muted text.
- Font discovery: try bundled-free system fonts (DejaVu Sans / Arial / Segoe UI
  / Noto Sans) in regular and bold weights; fall back to
  `ImageFont.load_default(size=...)`. No font binary is vendored.
- Image download: async, 10 s timeout, 8 MB cap, JPEG/PNG/WebP only, decoded
  with Pillow from bytes and converted to RGB. Failures and invalid images
  become generated placeholder tiles using initials and a deterministic color.
- Disk cache under `cog_data_path()/covers`: SHA-256(filename+URL) + extension,
  7-day TTL, best-effort writes, pruned opportunistically to 200 files.
- Render in a thread (`asyncio.to_thread`) because Pillow is synchronous.
- Output: PNG `BytesIO`, filename `lastfm-<member>.png`, posted with an
  embed (`embed.set_image(url="attachment://...")`) so the Discord client shows
  a preview.

## Error handling and edge cases

- No API key: scheduled run records "Last.fm API key is not configured" and
  logs; `register`/`preview` reply with setup guidance. No crash.
- No registered members: scheduled run is skipped (no empty grid spam) and
  next run is scheduled normally.
- Unknown/invalid username: friendly error in `register`; scheduled runs count
  that member as failed and continue.
- Channel deleted or missing permissions: store the error for `status`, log it,
  and keep scheduling; a later fix resumes posting.
- Fewer entries than N²: render empty tiles with a subtle "Not enough data"
  caption rather than stretch.
- All members failed: skip posting, store a combined error.
- Partial member failures: post the successful charts; store and report the
  failed member names.
- Config validation: clamp invalid stored values at read time (grid 2–8,
  unknown period/source/timezone fall back to defaults) so corrupt config cannot
  break the loop.
- Long names: wrapped/ellipsized; no image-size overflow.

## Privacy

- Stored per guild: Discord user ID, Last.fm username, registration timestamp.
- `end_user_data_statement` explains this and how to remove it.
- Implement `red_get_data_for_user` and `red_delete_data_for_user`
  (`requester="user"|"discord_deleted_user"`) to integrate with `[p]mydata`.
- No Last.fm credentials beyond the shared read-only API key, which is stored
  globally, never echoed, and can be supplied via env var.

## Testing

- `test_schedule.py`: exact next-run values for each period, weekday handling,
  month/quarter boundaries, DST transitions, overdue/timezone edge cases.
- `test_lastfm.py`: JSON parsing via a fake transport (no network), image size
  selection and placeholder detection, retry policy, error mapping, and
  per-member fetch failure accounting.
- `test_render.py`: generated art for 2×2 and 3×3, exact canvas dimensions,
  deterministic placeholders, long names, missing covers, PNG signature.
- `test_cog.py`: command/config wiring if Red imports; otherwise skipped with a
  clear reason. Core tests must never require a Discord token or network.
- `ruff check` and `ruff format --check` over the package and tests.

## Deployment

- Repo root `info.json` + `lastfmsummary/info.json` follow Red's downloader
  format; requirements: `Pillow>=10`, `tzdata`.
- Setup: `[p]load lastfmsummary`, owner sets `[p]lastfmset api_key`, admin sets
  channel/period/etc., members register, then `[p]lastfmset enable`.
- Minimum bot version: Red 3.5.0; Python 3.9+ (uses `zoneinfo`, dataclasses,
  `asyncio.to_thread`).

## Future ideas (explicitly out of scope)

- A combined server-wide ranking in addition to per-member charts.
- Monthly/yearly recap images with top tracks and listening time.
- Slash autocomplete for timezone/period.
- Per-guild weighted mode (mute members, weight by plays).
- Alternative themes and non-square grids.
