"""Google Images lookup through SerpApi, with a small disk cache."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Mapping

import aiohttp

from .cache import load_url_cache, save_url_cache

log = logging.getLogger("red.lastfmsummary.serp")

SEARCH_URL = "https://serpapi.com/search.json"
USER_AGENT = "lastfm-summary-cog/1.2 (SerpApi image lookup)"
CACHE_TTL = 30 * 24 * 60 * 60
MISS_CACHE_TTL = 7 * 24 * 60 * 60
MATCH_MIN_DIMENSION = 300
MATCH_MIN_RATIO = 0.6
MATCH_MAX_RATIO = 1.7
_MAX_ATTEMPTS = 2


def artist_query(name: str) -> str:
    return f"{name} music artist"


def album_query(artist: str, album: str) -> str:
    return " ".join(part for part in (artist, album, "album cover") if part)


def _valid_image_url(url: Any) -> str | None:
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return url
    return None


def _image_score(row: Mapping[str, Any]) -> int:
    """Prefer large, roughly square originals over tiny or panoramic images."""
    url = _valid_image_url(row.get("original"))
    if not url:
        return 0
    width = row.get("original_width")
    height = row.get("original_height")
    try:
        width = int(width)
        height = int(height)
    except (TypeError, ValueError):
        return 1
    if width <= 0 or height <= 0:
        return 1
    if width < MATCH_MIN_DIMENSION or height < MATCH_MIN_DIMENSION:
        return 0
    ratio = width / height
    if not MATCH_MIN_RATIO <= ratio <= MATCH_MAX_RATIO:
        return 0
    return 2


def _pick_image(rows: Any) -> str | None:
    if not isinstance(rows, (list, tuple)):
        return None
    best_url: str | None = None
    best_score = 0
    fallback: str | None = None

    for row in rows:
        if not isinstance(row, Mapping):
            continue
        original = _valid_image_url(row.get("original"))
        if original and fallback is None:
            fallback = original
        score = _image_score(row)
        if score > best_score:
            best_score = score
            best_url = original
        if best_score == 2:
            break

    if best_url is not None:
        return best_url
    if fallback is not None:
        return fallback
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        thumbnail = _valid_image_url(row.get("thumbnail"))
        if thumbnail:
            return thumbnail
    return None


async def _search(
    session: aiohttp.ClientSession, query: str, api_key: str, timeout: float
) -> tuple[str | None, bool]:
    params = {
        "engine": "google_images",
        "q": query,
        "api_key": api_key,
        "safe": "active",
        "hl": "en",
        "imgar": "s",
    }
    for attempt in range(_MAX_ATTEMPTS):
        try:
            async with session.get(
                SEARCH_URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                if response.status in {429, 500, 502, 503, 504} and attempt + 1 < _MAX_ATTEMPTS:
                    await asyncio.sleep(1.0)
                    continue
                if response.status != 200:
                    return None, False
                payload = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            if attempt + 1 < _MAX_ATTEMPTS:
                await asyncio.sleep(0.5)
                continue
            return None, False

        if not isinstance(payload, Mapping) or payload.get("error"):
            return None, False
        rows = payload.get("images_results")
        return _pick_image(rows), True
    return None, False


async def resolve_serp_images(
    queries: Mapping[str, str],
    *,
    api_key: str,
    cache_path: Path | None = None,
    session: aiohttp.ClientSession | None = None,
    timeout: float = 20.0,
) -> dict[str, str | None]:
    """Resolve cache keys to Google Images URLs via SerpApi.

    ``queries`` maps a stable key (for example ``artist:radiohead``) to the
    search query. Errors and rate limits are not cached, so a later preview can
    retry after fixing the key or waiting.
    """
    unique: dict[str, str] = {}
    for key, query in queries.items():
        clean = " ".join(str(query or "").split())
        if key and clean:
            unique[str(key)] = clean
    if not unique:
        return {}
    if not api_key:
        return {key: None for key in unique}

    cache = await asyncio.to_thread(load_url_cache, cache_path)
    results: dict[str, str | None] = {key: cache[key]["url"] for key in unique if key in cache}
    missing = {key: query for key, query in unique.items() if key not in results}
    if not missing:
        return results

    owned_session = session is None
    if session is None:
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers={"User-Agent": USER_AGENT},
        )
    semaphore = asyncio.Semaphore(4)
    cache_changed = False

    async def one(key: str, query: str) -> None:
        nonlocal cache_changed
        assert session is not None
        async with semaphore:
            url, cacheable = await _search(session, query, api_key, timeout)
        results[key] = url
        if cacheable:
            cache[key] = {
                "url": url,
                "expires": time.time() + (CACHE_TTL if url else MISS_CACHE_TTL),
            }
            cache_changed = True

    try:
        await asyncio.gather(*(one(key, query) for key, query in missing.items()))
    finally:
        if owned_session:
            await session.close()

    if cache_changed:
        await asyncio.to_thread(save_url_cache, cache_path, cache)
    return results


__all__ = [
    "album_query",
    "artist_query",
    "resolve_serp_images",
]
