# Last.fm Summary Cog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Build a Red-DiscordBot cog that links Last.fm usernames and periodically posts a configurable top artists/albums grid image for every linked member.

**Architecture:** Six focused modules — `schedule.py` (pure timezone math), `lastfm.py` (async API client + per-member charts), `deezer.py` (last-resort artist/album cover lookup + cache), `serp.py` (SerpApi Google Images fallback + cache), `render.py` (Pillow grid), `cog.py` (Config, hybrid commands, scheduler, per-member Discord posting, fallback orchestration). Tests cover each pure unit without Discord or network.

**Tech Stack:** Python 3.9+, Red-DiscordBot 3.5+, discord.py 2.x, aiohttp, Pillow, zoneinfo/tzdata, pytest.

---

## File Structure

- Create: `lastfmsummary/schedule.py` — period constants, validation, next-run computation.
- Create: `lastfmsummary/lastfm.py` — records, errors, HTTP client, image selection.
- Create: `lastfmsummary/deezer.py` — artist/album cover lookup, matching, disk cache.
- Create: `lastfmsummary/serp.py` — SerpApi Google Images lookup, selection, disk cache.
- Create: `lastfmsummary/render.py` — font/image helpers, cover cache, grid renderer.
- Create: `lastfmsummary/cog.py` — Config schema, commands, scheduler, posting, data APIs.
- Create: `lastfmsummary/__init__.py` — `setup()`.
- Create: `lastfmsummary/info.json`, `info.json`, `README.md`, `LICENSE`, `pyproject.toml`, `.gitignore`.
- Create: `tests/test_schedule.py`, `tests/test_lastfm.py`, `tests/test_deezer.py`, `tests/test_serp.py`, `tests/test_render.py`, `tests/test_cog.py`.

Interfaces locked here (later tasks must use these exact names):

```python
# schedule.py
PERIODS: dict[str, str]                  # period -> Last.fm api period
PERIOD_LABELS: dict[str, str]
def normalize_period(value: str) -> str
def parse_hhmm(value: str) -> tuple[int, int]
def format_hhmm(hour: int, minute: int) -> str
def validate_timezone(name: str) -> str
def compute_next_run(period: str, *, now_utc: datetime, tz_name: str,
                     at: str, weekday: int = 0, day: int = 1) -> datetime
def period_window(period: str) -> str    # human label, e.g. "the last 7 days"

# lastfm.py
@dataclass(frozen=True) class LastFMUser: username: str; display_name: str
@dataclass(frozen=True) class Entry: name: str; artist: str | None; plays: int; image_url: str | None
@dataclass(frozen=True) class MemberChart: username: str; entries: tuple[Entry, ...]
class LastFMError(Exception): code: int; message: str
class LastFMUnavailable(Exception): pass
class LastFMClient:
    def __init__(self, api_key: str, *, session: aiohttp.ClientSession | None = None)
    async def close(self) -> None
    async def get_user(self, username: str) -> LastFMUser
    async def get_top_artists(self, username: str, *, period: str, limit: int) -> tuple[Entry, ...]
    async def get_top_albums(self, username: str, *, period: str, limit: int) -> tuple[Entry, ...]
    async def fetch_charts(self, usernames: Sequence[str], *, source: str, period: str, limit: int) -> list[MemberChart]
def select_image(images: object) -> str | None
def is_placeholder(url: str | None) -> bool

# render.py
@dataclass(frozen=True) class Tile: name: str; subtitle: str | None; image_url: str | None; rank: int
def render_grid(*, tiles: Sequence[Tile], grid_size: int, title: str, subtitle: str,
                footer: str, covers: dict[str, bytes | None]) -> bytes
async def download_covers(urls: Sequence[str | None], *, cache_dir: Path | None = None,
                          session: aiohttp.ClientSession | None = None) -> dict[str, bytes | None]
```

Config keys are exactly those in the spec. Command group names: `lastfm` and `lastfmset`.

---

### Task 1: Scheduling and period math

**Files:** Create `lastfmsummary/schedule.py`, `tests/test_schedule.py`, `pyproject.toml`, `.gitignore`.

- [x] Write tests for: period normalization (`weekly`, `Weekly `, aliases `week`/`month`; unknown raises `ValueError`), `parse_hhmm("09:30") == (9, 30)` and invalid strings, `validate_timezone("Europe/Berlin")` and invalid, and `compute_next_run`:
  - monthly on 2026-10-21 12:00 UTC with day=1, time 09:00 UTC → 2026-11-01 09:00 UTC;
  - monthly with day=21, time 13:00 UTC → 2026-10-21 13:00 UTC;
  - weekly Monday from Wednesday 2026-10-21 → 2026-10-26 09:00 UTC;
  - quarterly (Jan/Apr/Jul/Oct) from 2026-10-21 → 2027-01-01;
  - half-yearly → 2027-01-01; yearly → 2027-01-01;
  - timezone: `America/New_York`, 09:00 local on 2026-11-01 (DST end) → 14:00 UTC;
  - day 31 is clamped to the month's last day.
- [x] Run `python -m pytest tests/test_schedule.py -q`; expected import failure.
- [x] Implement with `zoneinfo`, `calendar.monthrange`, and an aware-local → UTC conversion; compute candidate months by iterating forward from the current local date (bounded to 25 months), then choose the first candidate strictly after `now_utc`.
- [x] Run tests; expected PASS. Commit `feat: add scheduling math`.

### Task 2: Last.fm client records, parsing, retries

**Files:** Create `lastfmsummary/lastfm.py`, `tests/test_lastfm.py`.

- [x] Write tests using a fake `aiohttp.ClientSession` (object with `get()` returning an async context manager whose `json()` returns canned dicts):
  - `get_user` returns canonical `name` and raises `LastFMError(6)` on `{"error": 6, ...}`;
  - `get_top_artists` parses rank/playcount/image and skips malformed rows;
  - `get_top_albums` parses `artist.name`;
  - retry on `{"error": 29}` and on HTTP 500, succeeding on the second attempt; permanent `{"error": 10}` raises immediately;
  - `select_image` prefers `extralarge`/`mega`, ignores `#` and empty URLs, rejects placeholder hashes;
  - network exception raises `LastFMUnavailable`.
- [x] Run; expected failure.
- [x] Implement `LastFMClient` with one owned session when none is passed (`close()` only closes owned), 15 s timeout, 2 retries, `asyncio.sleep(0.5 * 2**attempt)` (monkeypatched in tests), params `format=json`, and typed errors. `fetch_charts` runs `get_top_artists`/`get_top_albums` with `asyncio.Semaphore(4)` and returns per-member charts; failed members are omitted but their usernames are logged via `logging`.
- [x] Run; expected PASS. Commit `feat: add Last.fm API client`.

### Task 3: Per-member chart preparation

**Files:** Modify `lastfmsummary/lastfm.py`, `tests/test_lastfm.py`.

- [x] Tests: `fetch_charts` returns one `MemberChart` per successful user, preserves request order, omits failed users, maps canonical periods to Last.fm periods, and keeps artist and album parse results separate.
- [x] Run; expected failure.
- [x] Implement bounded-concurrency fetching with per-user failure accounting. Do not merge members: each member's chart stays independent and is truncated to N×N by the poster.
- [x] Run; expected PASS. Commit `feat: add per-member chart fetching`.

### Task 4: Rendering primitives and grid

**Files:** Create `lastfmsummary/render.py`, `tests/test_render.py`.

- [x] Tests: `render_grid` with 4 tiles/2×2 returns PNG magic bytes and expected `(width, height)`; 9 tiles/3×3; long names and missing covers do not raise; 10 tiles but grid 3 truncates to 9; placeholder bytes produce a non-empty image; `download_covers` with a fake session dedupes URLs, caches to `tmp_path`, and maps failures to `None`.
- [x] Run; expected failure.
- [x] Implement:
  - `load_font(size, bold)` candidates including `DejaVuSans-Bold.ttf`, `arialbd.ttf`, `segoeuib.ttf`, `NotoSans-Bold.ttf`, then `ImageFont.load_default(size=...)`.
  - `_cover_image(data, size)` center-crop, rounded mask.
  - `_placeholder(name, size)` deterministic hue from SHA-256, initials + music note text.
  - `render_grid` layout: pad 24, cover sizes 300/220/170 by N, caption 20% min 44, header 76 px, footer 44 px; rank badge; wrapped 2-line names with ellipsis; empty tiles say "Not enough data".
  - `download_covers` with 10 s timeout, 8 MB cap, content-type check, disk cache TTL 7 days and opportunistic pruning to 200 files.
- [x] Run; expected PASS. Inspect one generated PNG manually. Commit `feat: render summary grid images`.

### Task 4b: Deezer artist portraits

**Files:** Create `lastfmsummary/deezer.py`, `tests/test_deezer.py`; modify `lastfmsummary/cog.py`.

- [x] Tests: exact and accent-insensitive matching, rejection of weak matches, `picture_xl` preference, network failures becoming misses, and cache hit/miss behavior.
- [x] Implement normalized artist keys, Deezer search, a 0.88 similarity floor, four concurrent lookups, 30-day success cache and 7-day miss cache.
- [x] In `_post_charts`, resolve only missing artist images, pass the mapping to `_post_chart`, and keep Deezer failures non-fatal.
- [x] Add the Deezer credit to image/embed footers only when a fallback portrait is used.
- [x] Run; expected PASS. Commit `feat: add Deezer artist portraits`.

### Task 4c: SERP API visual fallback

**Files:** Create `lastfmsummary/serp.py`, `tests/test_serp.py`; modify `lastfmsummary/cog.py`, `lastfmsummary/deezer.py`.

- [x] Tests: image scoring/dimension filtering, thumbnail fallback, SerpApi params, non-caching of API errors, query builders, cache hits/misses.
- [x] Implement owner-only `lastfmset serp_api_key` and `SERPAPI_API_KEY` fallback.
- [x] Resolve missing artwork in order: Last.fm → SerpApi → Deezer → generated tile. Use artist and album query builders.
- [x] Add Deezer album-cover lookup as the last fallback and separate caches for SerpApi/Deezer results.
- [x] Credit providers in the footer only when their fallback is used.
- [x] Run; expected PASS. Commit `feat: add SerpApi artwork fallback`.

### Task 5: Cog config, member commands, data APIs

**Files:** Create `lastfmsummary/cog.py`, `lastfmsummary/__init__.py`, `tests/test_cog.py`.

- [x] `test_cog.py` imports Red inside a `pytest.importorskip("redbot")`; asserts Config defaults, that both groups exist, and that admin commands carry the guild-only permission checks.
- [x] Implement Config defaults exactly as specified; helper methods `_guild_settings(guild)` that sanitize values; `_api_key()` from config then env; `register/unregister/profile/members/status` hybrid subcommands with `ctx.guild` guards; `respond()` helper using `ephemeral=ctx.interaction is not None`; `red_get_data_for_user`, `red_delete_data_for_user`, `red_end_user_data_statement`.
- [x] Register username after `get_user` validation; store canonical display name in `username`; reject duplicates within the same guild with a clear message.
- [x] Run cog tests if Red is importable, otherwise record the skip. Commit `feat: add cog configuration and member commands`.

### Task 6: Admin commands and scheduler

**Files:** Modify `lastfmsummary/cog.py`.

- [x] Implement `lastfmset` group (hybrid, `admin_or_permissions(manage_guild=True)`, `guild_only`): channel (mention/ID/none), period, source, grid (2–8), time, timezone, day (weekday name or 1-31, clamped), enable/disable, preview [member], status, api_key (owner only via `is_owner`).
- [x] Every schedule-affecting setter recomputes and stores `next_run_at` from now; `channel` + `enable` validate a configured channel; `preview` uses the current channel and a per-guild lock.
- [x] Implement `tasks.loop(seconds=30)` started in `cog_load`, cancelled in `cog_unload`; tick selects due guilds via `bot.guilds` + config, skips runs >6 h late, posts, persists result. Catch and store every per-guild error.
- [x] Implement `_post_charts(guild, channel)`: gather registrations, fetch charts, then for each member build tiles (`name`, album artist subtitle, rank), download covers, render in `asyncio.to_thread`, and send a `discord.File` + embed. Continue after a member's fetch/render failure; return `PostReport(posted, failed, errors)`. Implement `_post_chart` for the single-member path. Handle missing key/no registrations/channel errors with distinct messages.
- [x] Commit `feat: add admin configuration and scheduler`.

### Task 7: Packaging and documentation

**Files:** Create `info.json`, `lastfmsummary/info.json`, `README.md`, `LICENSE`, `pyproject.toml`, `.github/workflows/tests.yml`.

- [x] Root info.json: author, name, short, description. Cog info.json: author, name, short, description, `requirements: ["Pillow>=10.0", "tzdata"]`, `min_bot_version: "3.5.0"`, `min_python_version: [3, 9, 0]`, tags, `end_user_data_statement`.
- [x] README: features, install (`[p]repo add`, `[p]cog install`), API key setup (Last.fm API account, owner command/env), command tables, scheduling semantics, permissions, data/privacy, screenshots placeholder-free wording, development/test instructions.
- [x] `pyproject.toml`: pytest config (`asyncio_mode = "auto"`, testpaths), ruff config (line-length 100, target py39), project metadata; dev extras `pytest`, `pytest-asyncio`, `ruff`.
- [x] CI: Python 3.9/3.11 matrix, `pip install -e .[dev]`, `ruff check`, `pytest`.
- [x] Commit `docs: add packaging and usage documentation`.

### Task 8: Verification

**Files:** none.

- [x] Run `ruff check lastfmsummary tests` and `ruff format --check lastfmsummary tests`; fix all findings.
- [x] Run `python -m pytest -q`; all non-Red tests pass, and the Red-backed cog tests pass under Python 3.11.
- [x] Under Python 3.11 with Red installed, import the cog and run `test_cog.py`; verify command registration and config defaults.
- [x] Generate a sample 3×3 PNG from fake data with actual covers unavailable to confirm layout; open it and check legibility.
- [x] `git status` clean; final summary lists supported commands, assumptions, and any environment limitation.

---

## Self-Review Notes

- Spec coverage: registration/privacy (Task 5), admin config (Task 6), scheduling/DST (Task 1, 6), Last.fm errors/retries (Task 2), per-member chart handling (Task 3), rendering/cache/placeholders (Task 4), Deezer portraits (Task 4b), SerpApi artwork (Task 4c), packaging/docs/CI (Task 7), verification (Task 8).
- No daily cadence by design; documented in spec.
- `Entry.artist` is `None` for artists and a string for albums throughout.
- `PostReport.failed` is fed into `last_error` and the preview response.
