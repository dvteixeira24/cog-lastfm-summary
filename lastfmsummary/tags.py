"""Cached Last.fm artist-tag lookup used by the listening insights."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Mapping, Sequence

from .lastfm import LastFMClient, LastFMError, LastFMUnavailable

log = logging.getLogger("red.lastfmsummary.tags")

CACHE_TTL = 30 * 24 * 60 * 60
MISS_CACHE_TTL = 7 * 24 * 60 * 60
MAX_CACHE_ENTRIES = 5000


def artist_key(name: str) -> str:
    """Normalize an artist name for tag cache keys."""
    return " ".join(str(name or "").split()).casefold()


def _load_cache(path: Path | None) -> dict[str, dict[str, object]]:
    if path is None:
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, Mapping):
        return {}

    now = time.time()
    cache: dict[str, dict[str, object]] = {}
    for key, value in raw.items():
        if not isinstance(value, Mapping):
            continue
        expires = value.get("expires")
        rows = value.get("tags")
        if not isinstance(expires, (int, float)) or expires <= now:
            continue
        tags: list[tuple[str, int]] = []
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


def _save_cache(path: Path | None, cache: Mapping[str, Mapping[str, object]]) -> None:
    if path is None:
        return
    now = time.time()
    entries = {
        key: {
            "tags": [[name, count] for name, count in value.get("tags", ())],
            "expires": float(value.get("expires", 0)),
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


async def resolve_artist_tags(
    names: Sequence[str],
    *,
    client: LastFMClient,
    cache_path: Path | None = None,
    limit: int = 3,
    concurrency: int = 4,
) -> dict[str, tuple[tuple[str, int], ...]]:
    """Resolve artist names to their top Last.fm tags, using a disk cache.

    Results are keyed by :func:`artist_key`. Successful lookups are cached
    for 30 days and empty results for 7 days. API errors are not cached, so a
    later run retries them.
    """
    unique: dict[str, str] = {}
    for name in names:
        clean = " ".join(str(name or "").split())
        key = artist_key(clean)
        if clean and key:
            unique.setdefault(key, clean)
    if not unique:
        return {}

    cache = await asyncio.to_thread(_load_cache, cache_path)
    results: dict[str, tuple[tuple[str, int], ...]] = {
        key: tuple(cache[key]["tags"]) for key in unique if key in cache
    }
    missing = {key: name for key, name in unique.items() if key not in results}
    if not missing:
        return results

    semaphore = asyncio.Semaphore(max(1, int(concurrency)))
    changed = False

    async def one(key: str, name: str) -> None:
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


__all__ = ["artist_key", "resolve_artist_tags"]
