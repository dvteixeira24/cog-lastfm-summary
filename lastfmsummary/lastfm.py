"""Async Last.fm client and chart records."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import aiohttp

from .schedule import PERIODS

log = logging.getLogger("red.lastfmsummary.lastfm")

API_URL = "https://ws.audioscrobbler.com/2.0/"
USER_AGENT = "lastfm-summary-cog/1.1 (Red-DiscordBot cog)"
PLACEHOLDER_HASH = "2a96cbd8b46e442fc41c2b86b821562f"
IMAGE_SIZE_ORDER = ("mega", "extralarge", "large", "medium", "small")
_RETRYABLE_CODES = frozenset({11, 16, 29})
_MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class LastFMUser:
    """A validated Last.fm profile."""

    username: str
    display_name: str


@dataclass(frozen=True)
class Entry:
    """One artist or album from a member's chart."""

    name: str
    artist: str | None
    plays: int
    image_url: str | None


@dataclass(frozen=True)
class MemberChart:
    """A single member's top list."""

    username: str
    entries: tuple[Entry, ...] = field(default_factory=tuple)


class LastFMError(Exception):
    """A permanent Last.fm API error."""

    def __init__(self, code: int, message: str):
        self.code = int(code)
        self.message = str(message)
        super().__init__(f"Last.fm error {self.code}: {self.message}")


class LastFMUnavailable(Exception):
    """A transient network, HTTP or parsing failure."""


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def is_placeholder(url: str | None) -> bool:
    """Return True for empty or known Last.fm placeholder artwork URLs."""
    candidate = _as_text(url)
    if not candidate or candidate == "#":
        return True
    lowered = candidate.lower()
    return (
        PLACEHOLDER_HASH in lowered
        or "lastfm_img/default" in lowered
        or "/i/u/default" in lowered
        or lowered.endswith("/default.png")
    )


def select_image(images: Any) -> str | None:
    """Pick the largest usable image URL from a Last.fm image list."""
    if not images:
        return None

    ranked: list[tuple[int, str]] = []
    if isinstance(images, Mapping):
        items: Iterable[Any] = [images]
    elif isinstance(images, (list, tuple)):
        items = images
    else:
        return None

    for item in items:
        if not isinstance(item, Mapping):
            continue
        url = _as_text(item.get("#text") or item.get("url") or item.get("text"))
        if not url or is_placeholder(url):
            continue
        size = _as_text(item.get("size")).lower()
        try:
            rank = len(IMAGE_SIZE_ORDER) - IMAGE_SIZE_ORDER.index(size)
        except ValueError:
            rank = 0
        ranked.append((rank, url))

    if not ranked:
        return None
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return ranked[0][1]


class LastFMClient:
    """Small async wrapper around the public Last.fm read endpoints."""

    def __init__(
        self,
        api_key: str,
        *,
        session: aiohttp.ClientSession | None = None,
        timeout: float = 15.0,
    ) -> None:
        if not _as_text(api_key):
            raise ValueError("A Last.fm API key is required.")
        self.api_key = _as_text(api_key)
        self._session = session
        self._owns_session = session is None
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def close(self) -> None:
        """Close the internally created session, if any."""
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> "LastFMClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={"User-Agent": USER_AGENT},
            )
            self._owns_session = True
        return self._session

    async def _request(self, params: Mapping[str, Any]) -> dict[str, Any]:
        query = dict(params)
        query["api_key"] = self.api_key
        query["format"] = "json"
        session = self._get_session()
        last_error: BaseException | None = None

        for attempt in range(_MAX_ATTEMPTS):
            retry = False
            try:
                async with session.get(API_URL, params=query, timeout=self._timeout) as response:
                    if response.status == 429 or response.status >= 500:
                        last_error = LastFMUnavailable(f"Last.fm returned HTTP {response.status}.")
                        retry = True
                    elif response.status != 200:
                        raise LastFMUnavailable(f"Last.fm returned HTTP {response.status}.")
                    else:
                        try:
                            payload = await response.json(content_type=None)
                        except (aiohttp.ContentTypeError, ValueError) as exc:
                            last_error = LastFMUnavailable("Last.fm returned invalid JSON.")
                            last_error.__cause__ = exc
                            retry = True
                        else:
                            if not isinstance(payload, dict):
                                last_error = LastFMUnavailable(
                                    "Last.fm returned an unexpected response."
                                )
                                retry = True
                            elif payload.get("error"):
                                code = _as_int(payload.get("error"), -1)
                                message = _as_text(payload.get("message")) or "Unknown error"
                                if code in _RETRYABLE_CODES:
                                    last_error = LastFMUnavailable(
                                        f"Last.fm temporarily unavailable ({message})."
                                    )
                                    retry = True
                                else:
                                    raise LastFMError(code, message)
                            else:
                                return payload
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                last_error = LastFMUnavailable(f"Could not reach Last.fm: {exc}")
                retry = True

            if not retry or attempt == _MAX_ATTEMPTS - 1:
                break
            await asyncio.sleep(0.5 * (2**attempt))

        if last_error is not None:
            raise last_error
        raise LastFMUnavailable("Last.fm request failed.")

    async def get_user(self, username: str) -> LastFMUser:
        payload = await self._request({"method": "user.getinfo", "user": username})
        user = payload.get("user")
        if not isinstance(user, Mapping) or not _as_text(user.get("name")):
            raise LastFMUnavailable("Last.fm returned an incomplete user profile.")
        canonical = _as_text(user.get("name"))
        return LastFMUser(username=canonical, display_name=canonical)

    async def _get_top(
        self,
        method: str,
        container: str,
        item_key: str,
        username: str,
        *,
        period: str,
        limit: int,
    ) -> tuple[Entry, ...]:
        payload = await self._request(
            {
                "method": method,
                "user": username,
                "period": period,
                "limit": max(1, int(limit)),
            }
        )
        section = payload.get(container)
        if not isinstance(section, Mapping):
            return ()
        rows = section.get(item_key) or []
        if isinstance(rows, Mapping):
            rows = [rows]
        if not isinstance(rows, (list, tuple)):
            return ()

        entries: list[Entry] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            name = _as_text(row.get("name"))
            if not name:
                continue
            artist: str | None = None
            if item_key == "album":
                raw_artist = row.get("artist")
                if isinstance(raw_artist, Mapping):
                    artist = _as_text(raw_artist.get("name")) or None
                else:
                    artist = _as_text(raw_artist) or None
            entries.append(
                Entry(
                    name=name,
                    artist=artist,
                    plays=_as_int(row.get("playcount")),
                    image_url=select_image(row.get("image")),
                )
            )
        return tuple(entries)

    async def get_top_artists(
        self, username: str, *, period: str, limit: int = 50
    ) -> tuple[Entry, ...]:
        return await self._get_top(
            "user.gettopartists", "topartists", "artist", username, period=period, limit=limit
        )

    async def get_top_albums(
        self, username: str, *, period: str, limit: int = 50
    ) -> tuple[Entry, ...]:
        return await self._get_top(
            "user.gettopalbums", "topalbums", "album", username, period=period, limit=limit
        )

    async def fetch_charts(
        self,
        usernames: Sequence[str],
        *,
        source: str,
        period: str,
        limit: int = 50,
    ) -> list[MemberChart]:
        """Fetch charts for several members with bounded concurrency.

        Individual failures are logged and omitted; callers can compare the
        returned usernames with the requested list to build a failure list.
        """
        api_period = PERIODS.get(period, period)
        semaphore = asyncio.Semaphore(4)
        fetch = self.get_top_albums if source == "albums" else self.get_top_artists

        async def one(username: str) -> MemberChart | None:
            async with semaphore:
                try:
                    entries = await fetch(username, period=api_period, limit=limit)
                except LastFMError as exc:
                    if exc.code in {10, 26}:
                        # The key is unusable for every member; fail the whole run.
                        raise
                    log.warning("Last.fm rejected %s: %s", username, exc)
                except LastFMUnavailable as exc:
                    log.warning("Could not fetch %s: %s", username, exc)
                except Exception:
                    log.exception("Unexpected error fetching Last.fm user %s", username)
                else:
                    return MemberChart(username=username, entries=entries)
            return None

        results = await asyncio.gather(*(one(name) for name in usernames))
        return [chart for chart in results if chart is not None]
