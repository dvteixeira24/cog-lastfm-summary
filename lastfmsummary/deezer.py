"""Best-effort artist portrait lookup through the public Deezer API."""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import time
import unicodedata
from pathlib import Path
from typing import Any, Mapping, Sequence

import aiohttp

log = logging.getLogger("red.lastfmsummary.deezer")

SEARCH_URL = "https://api.deezer.com/search/artist"
USER_AGENT = "lastfm-summary-cog/1.1 (artist image lookup)"
CACHE_TTL = 30 * 24 * 60 * 60
MISS_CACHE_TTL = 7 * 24 * 60 * 60
MAX_CACHE_ENTRIES = 5000
MATCH_THRESHOLD = 0.88
_MAX_ATTEMPTS = 2


def artist_key(name: str) -> str:
    """Normalize an artist name for matching and cache keys."""
    text = unicodedata.normalize("NFKD", str(name))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.casefold().replace("&", "and").replace("$", "s")
    text = "".join(char for char in text if char.isalnum())
    return text or " ".join(str(name).split()).casefold()


def _load_cache(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, Mapping):
        return {}

    now = time.time()
    cache: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if not isinstance(value, Mapping):
            continue
        expires = value.get("expires")
        url = value.get("url")
        if not isinstance(expires, (int, float)) or expires <= now:
            continue
        if url is not None and not isinstance(url, str):
            continue
        cache[str(key)] = {"url": url, "expires": float(expires)}
    return cache


def _save_cache(path: Path | None, cache: Mapping[str, Mapping[str, Any]]) -> None:
    if path is None:
        return
    now = time.time()
    entries = {
        key: {"url": value.get("url"), "expires": float(value.get("expires", 0))}
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
        log.debug("Could not write Deezer cache %s", path, exc_info=True)


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

    if not unique:
        return {}

    cache = await asyncio.to_thread(_load_cache, cache_path)
    results: dict[str, str | None] = {key: cache[key]["url"] for key in unique if key in cache}
    missing = {key: name for key, name in unique.items() if key not in results}
    if not missing:
        return results

    owned_session = session is None
    if session is None:
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers={"User-Agent": USER_AGENT},
        )

    semaphore = asyncio.Semaphore(4)

    async def one(key: str, name: str) -> None:
        assert session is not None
        async with semaphore:
            url = await _search(session, name, timeout)
        results[key] = url
        cache[key] = {
            "url": url,
            "expires": time.time() + (CACHE_TTL if url else MISS_CACHE_TTL),
        }

    try:
        await asyncio.gather(*(one(key, name) for key, name in missing.items()))
    finally:
        if owned_session:
            await session.close()

    if missing:
        await asyncio.to_thread(_save_cache, cache_path, cache)
    return results


__all__ = ["artist_key", "resolve_artist_images"]
