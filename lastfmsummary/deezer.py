"""Best-effort artist portrait lookup through the public Deezer API."""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

import aiohttp

from .cache import load_url_cache, save_url_cache

log = logging.getLogger("red.lastfmsummary.deezer")

SEARCH_URL = "https://api.deezer.com/search/artist"
ALBUM_SEARCH_URL = "https://api.deezer.com/search/album"
USER_AGENT = "lastfm-summary-cog/1.3 (image lookup)"
CACHE_TTL = 30 * 24 * 60 * 60
MISS_CACHE_TTL = 7 * 24 * 60 * 60
MATCH_THRESHOLD = 0.88
_MAX_ATTEMPTS = 2


def artist_key(name: str) -> str:
    """Normalize an artist name for matching and cache keys."""
    text = unicodedata.normalize("NFKD", str(name))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.casefold().replace("&", "and").replace("$", "s")
    text = "".join(char for char in text if char.isalnum())
    return text or " ".join(str(name).split()).casefold()


def album_key(artist: str, album: str) -> str:
    """Normalize an artist/album pair for matching and cache keys."""
    return f"{artist_key(artist)}::{artist_key(album)}"


def _album_title_key(title: str) -> str:
    text = unicodedata.normalize("NFKD", str(title))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[\(\[].*?[\)\]]", " ", text)
    text = re.sub(
        r"\b(deluxe|remaster(?:ed)?|expanded|anniversary|special|bonus)\b.*$",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    return artist_key(text)


def _picture_url(row: Mapping[str, Any]) -> str | None:
    for field in ("picture_xl", "picture_big", "picture_medium", "picture_small"):
        url = row.get(field)
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            return url
    return None


def _pick_match(query: str, rows: Sequence[Any]) -> Mapping[str, Any] | None:
    query_key = artist_key(query)
    best: tuple[int, int] | None = None
    chosen: Mapping[str, Any] | None = None

    for row in rows:
        if not isinstance(row, Mapping):
            continue
        candidate_key = artist_key(str(row.get("name", "")))
        if not candidate_key or not _picture_url(row):
            continue
        if candidate_key == query_key:
            score = 2
        elif difflib.SequenceMatcher(None, query_key, candidate_key).ratio() >= MATCH_THRESHOLD:
            score = 1
        else:
            continue
        try:
            fans = int(row.get("nb_fan") or 0)
        except (TypeError, ValueError):
            fans = 0
        candidate = (score, fans)
        if best is None or candidate > best:
            best = candidate
            chosen = row
    return chosen


async def _search(session: aiohttp.ClientSession, name: str, timeout: float) -> str | None:
    for attempt in range(_MAX_ATTEMPTS):
        try:
            async with session.get(
                SEARCH_URL,
                params={"q": name, "limit": 5},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                if response.status == 429 or response.status >= 500:
                    if attempt + 1 < _MAX_ATTEMPTS:
                        await asyncio.sleep(1.0)
                        continue
                    return None
                if response.status != 200:
                    return None
                payload = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            if attempt + 1 < _MAX_ATTEMPTS:
                await asyncio.sleep(0.5)
                continue
            return None

        if not isinstance(payload, Mapping):
            return None
        rows = payload.get("data")
        if not isinstance(rows, (list, tuple)):
            return None
        match = _pick_match(name, rows)
        return _picture_url(match) if match is not None else None
    return None


async def _search_album(
    session: aiohttp.ClientSession, album: tuple[str, str], timeout: float
) -> str | None:
    artist, title = album
    query = " ".join(part for part in (artist, title) if part)
    for attempt in range(_MAX_ATTEMPTS):
        try:
            async with session.get(
                ALBUM_SEARCH_URL,
                params={"q": query, "limit": 5},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                if response.status == 429 or response.status >= 500:
                    if attempt + 1 < _MAX_ATTEMPTS:
                        await asyncio.sleep(1.0)
                        continue
                    return None
                if response.status != 200:
                    return None
                payload = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            if attempt + 1 < _MAX_ATTEMPTS:
                await asyncio.sleep(0.5)
                continue
            return None

        if not isinstance(payload, Mapping):
            return None
        rows = payload.get("data")
        if not isinstance(rows, (list, tuple)):
            return None
        match = _pick_album_match(artist, title, rows)
        return _album_cover_url(match) if match is not None else None
    return None


def _album_cover_url(row: Mapping[str, Any]) -> str | None:
    for field in ("cover_xl", "cover_big", "cover_medium"):
        url = row.get(field)
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            return url
    return None


def _pick_album_match(artist: str, album: str, rows: Sequence[Any]) -> Mapping[str, Any] | None:
    album_query = _album_title_key(album)
    artist_query = artist_key(artist)
    best: tuple[int, int] | None = None
    chosen: Mapping[str, Any] | None = None

    for row in rows:
        if not isinstance(row, Mapping):
            continue
        title_key = _album_title_key(str(row.get("title") or row.get("name") or ""))
        if not title_key or not _album_cover_url(row):
            continue
        raw_artist = row.get("artist")
        if isinstance(raw_artist, Mapping):
            row_artist = str(raw_artist.get("name", ""))
        else:
            row_artist = str(raw_artist or "")
        row_artist_key = artist_key(row_artist)

        album_score = (
            2
            if title_key == album_query
            else 1
            if difflib.SequenceMatcher(None, album_query, title_key).ratio() >= MATCH_THRESHOLD
            else 0
        )
        if not album_score:
            continue
        if artist_query:
            artist_score = (
                2
                if row_artist_key == artist_query
                else 1
                if row_artist_key
                and difflib.SequenceMatcher(None, artist_query, row_artist_key).ratio()
                >= MATCH_THRESHOLD
                else 0
            )
            if not artist_score:
                continue
        else:
            artist_score = 0
        try:
            fans = int(row.get("nb_fan") or row.get("rank") or 0)
        except (TypeError, ValueError):
            fans = 0
        candidate = (album_score * 2 + artist_score, fans)
        if best is None or candidate > best:
            best = candidate
            chosen = row
    return chosen


async def _resolve_cached(
    unique: Mapping[str, Any],
    lookup: Callable[[aiohttp.ClientSession, Any, float], Awaitable[str | None]],
    *,
    cache_path: Path | None,
    session: aiohttp.ClientSession | None,
    timeout: float,
) -> dict[str, str | None]:
    if not unique:
        return {}

    cache = await asyncio.to_thread(load_url_cache, cache_path)
    results: dict[str, str | None] = {key: cache[key]["url"] for key in unique if key in cache}
    missing = {key: payload for key, payload in unique.items() if key not in results}
    if not missing:
        return results

    owned_session = session is None
    if session is None:
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers={"User-Agent": USER_AGENT},
        )

    semaphore = asyncio.Semaphore(4)

    async def one(key: str, payload: Any) -> None:
        assert session is not None
        async with semaphore:
            url = await lookup(session, payload, timeout)
        results[key] = url
        cache[key] = {
            "url": url,
            "expires": time.time() + (CACHE_TTL if url else MISS_CACHE_TTL),
        }

    try:
        await asyncio.gather(*(one(key, payload) for key, payload in missing.items()))
    finally:
        if owned_session:
            await session.close()

    await asyncio.to_thread(save_url_cache, cache_path, cache)
    return results


async def resolve_artist_images(
    names: Sequence[str],
    *,
    cache_path: Path | None = None,
    session: aiohttp.ClientSession | None = None,
    timeout: float = 10.0,
) -> dict[str, str | None]:
    """Resolve artist names to Deezer portrait URLs.

    The returned mapping is keyed by :func:`artist_key`. Failed lookups are
    cached as ``None`` for a shorter period than successful ones.
    """
    unique: dict[str, str] = {}
    for name in names:
        cleaned = " ".join(str(name or "").split())
        key = artist_key(cleaned)
        if cleaned and key:
            unique.setdefault(key, cleaned)
    return await _resolve_cached(
        unique,
        lambda sess, name, timeout_: _search(sess, name, timeout_),
        cache_path=cache_path,
        session=session,
        timeout=timeout,
    )


async def resolve_album_covers(
    albums: Sequence[tuple[str, str]],
    *,
    cache_path: Path | None = None,
    session: aiohttp.ClientSession | None = None,
    timeout: float = 10.0,
) -> dict[str, str | None]:
    """Resolve ``(artist, album)`` pairs to Deezer cover URLs."""
    unique: dict[str, tuple[str, str]] = {}
    for artist, album in albums:
        clean_artist = " ".join(str(artist or "").split())
        clean_album = " ".join(str(album or "").split())
        if not clean_album:
            continue
        unique.setdefault(album_key(clean_artist, clean_album), (clean_artist, clean_album))
    return await _resolve_cached(
        unique,
        lambda sess, pair, timeout_: _search_album(sess, pair, timeout_),
        cache_path=cache_path,
        session=session,
        timeout=timeout,
    )


__all__ = [
    "album_key",
    "artist_key",
    "resolve_album_covers",
    "resolve_artist_images",
]
