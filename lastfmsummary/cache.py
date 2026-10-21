"""Small on-disk cache for provider image URL lookups."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Mapping

log = logging.getLogger("red.lastfmsummary.imagecache")

MAX_CACHE_ENTRIES = 5000


def load_url_cache(path: Path | None) -> dict[str, dict[str, Any]]:
    """Load non-expired URL cache entries, ignoring corrupt files."""
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


def save_url_cache(path: Path | None, cache: Mapping[str, Mapping[str, Any]]) -> None:
    """Atomically persist non-expired entries, capped at the newest 5000."""
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
        log.debug("Could not write image cache %s", path, exc_info=True)


__all__ = ["load_url_cache", "save_url_cache", "MAX_CACHE_ENTRIES"]
