"""Pillow rendering and cover-art acquisition for Last.fm summaries."""

from __future__ import annotations

import asyncio
import colorsys
import contextlib
import hashlib
import io
import logging
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping, Sequence

import aiohttp
from PIL import Image, ImageDraw, ImageFont

from .charts import (
    draw_genre_chart,
    draw_listening_clock,
    draw_section_title,
    section_title_height,
)
from .lastfm import is_placeholder
from .listening import GenreTimeline, ListeningClock

log = logging.getLogger("red.lastfmsummary.render")

MAX_GRID = 8
MIN_GRID = 2
CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
CACHE_MAX_FILES = 200
MAX_IMAGE_BYTES = 8 * 1024 * 1024

BACKGROUND = (16, 16, 20, 255)
SURFACE = (27, 27, 34, 255)
SURFACE_LIGHT = (38, 38, 47, 255)
TEXT = (244, 244, 248, 255)
MUTED = (160, 160, 175, 255)
ACCENT = (72, 199, 142, 255)
PLACEHOLDER_TEXT = (235, 235, 245, 255)
EMPTY_TEXT = (120, 120, 138, 255)

_FONT_DIR = Path(__file__).resolve().parent / "fonts"
_BUNDLED_FONT = _FONT_DIR / "WorkSans-Variable.ttf"

_FONT_CANDIDATES = {
    False: (
        "DejaVuSans.ttf",
        "arial.ttf",
        "segoeui.ttf",
        "NotoSans-Regular.ttf",
        "LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ),
    True: (
        "DejaVuSans-Bold.ttf",
        "arialbd.ttf",
        "segoeuib.ttf",
        "NotoSans-Bold.ttf",
        "LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ),
}


@dataclass(frozen=True)
class Tile:
    """One cell in the summary grid."""

    name: str
    subtitle: str | None
    image_url: str | None
    rank: int


@lru_cache(maxsize=64)
def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load the bundled Work Sans font, falling back to system fonts, then Pillow."""
    size = max(1, int(size))

    try:
        bundled = ImageFont.truetype(str(_BUNDLED_FONT), size)
    except (OSError, ValueError):
        bundled = None
    if bundled is not None:
        if bold:
            with contextlib.suppress(OSError, ValueError, AttributeError):
                bundled.set_variation_by_name("Bold")
        return bundled

    for candidate in _FONT_CANDIDATES[bool(bold)]:
        try:
            return ImageFont.truetype(candidate, size)
        except (OSError, ValueError):
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _rounded_mask(size: tuple[int, int], radius: int, *, bottom: bool = True) -> Image.Image:
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    if not bottom:
        draw.rectangle((0, size[1] - radius, size[0], size[1]), fill=255)
    return mask


def _cover_image(data: bytes | None, size: int) -> Image.Image | None:
    if not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as opened:
            opened.load()
            image = opened.convert("RGB")
    except Exception:
        return None

    width, height = image.size
    if width <= 0 or height <= 0:
        return None
    edge = min(width, height)
    left = (width - edge) // 2
    top = (height - edge) // 2
    image = image.crop((left, top, left + edge, top + edge)).resize(
        (size, size), Image.Resampling.LANCZOS
    )
    return image


def _placeholder(name: str, size: int) -> Image.Image:
    digest = hashlib.sha256((name or "unknown").encode("utf-8", "replace")).digest()
    hue = int.from_bytes(digest[:2], "big") / 65535
    red, green, blue = colorsys.hsv_to_rgb(hue, 0.42, 0.38)
    base = (int(red * 255), int(green * 255), int(blue * 255))
    image = Image.new("RGB", (size, size), base)
    draw = ImageDraw.Draw(image, "RGBA")

    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse(
        (size * 0.12, size * 0.12, size * 0.88, size * 0.88),
        fill=(255, 255, 255, 22),
    )
    image = Image.alpha_composite(image.convert("RGBA"), glow).convert("RGB")
    draw = ImageDraw.Draw(image, "RGBA")

    words = [word for word in (name or "").split() if word]
    initials = "".join(word[0] for word in words[:2]).upper()
    if not initials:
        initials = "\u266a"
    font = load_font(int(size * 0.34), bold=True)
    box = draw.textbbox((0, 0), initials, font=font)
    draw.text(
        ((size - (box[2] - box[0])) / 2, (size - (box[3] - box[1])) / 2 - box[1]),
        initials,
        font=font,
        fill=PLACEHOLDER_TEXT,
    )
    return image


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    max_lines: int = 2,
) -> list[str]:
    text = " ".join((text or "").split())
    if not text:
        return []

    def width_of(value: str) -> int:
        box = draw.textbbox((0, 0), value, font=font)
        return box[2] - box[0]

    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        candidate = f"{current} {word}".strip()
        if not current or width_of(candidate) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
            if len(lines) == max_lines:
                break
    if len(lines) < max_lines and current:
        lines.append(current)
    if not lines:
        lines = [text]

    if len(lines) > max_lines:
        lines = lines[:max_lines]

    # Ellipsize the final line if it still overflows or text was dropped.
    final = lines[-1]
    dropped = len(" ".join(lines)) < len(text)
    while width_of(final) > max_width and len(final) > 1:
        final = final[:-1]
        dropped = True
    if dropped:
        while final and width_of(final + "\u2026") > max_width:
            final = final[:-1]
        final = final.rstrip() + "\u2026"
    lines[-1] = final
    return lines


def _ellipsize(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int
) -> str:
    value = " ".join((text or "").split())
    if not value:
        return ""
    box = draw.textbbox((0, 0), value, font=font)
    if box[2] - box[0] <= max_width:
        return value
    while len(value) > 1:
        value = value[:-1]
        if draw.textbbox((0, 0), value + "\u2026", font=font)[2] <= max_width:
            return value.rstrip() + "\u2026"
    return "\u2026"


def _metric(font: ImageFont.ImageFont) -> int:
    ascent, descent = font.getmetrics()
    return ascent + descent


def _draw_centered(
    draw: ImageDraw.ImageDraw,
    center: tuple[float, float],
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int, int],
) -> None:
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(
        (
            center[0] - (box[2] - box[0]) / 2 - box[0],
            center[1] - (box[3] - box[1]) / 2 - box[1],
        ),
        text,
        font=font,
        fill=fill,
    )


def render_grid(
    *,
    tiles: Sequence[Tile],
    grid_size: int,
    title: str,
    subtitle: str,
    footer: str,
    covers: Mapping[str, bytes | None],
    genres: GenreTimeline | None = None,
    clock: ListeningClock | None = None,
) -> bytes:
    """Render a square grid of entries and return PNG bytes.

    Missing covers and empty slots render as styled placeholders, so the layout
    stays consistent regardless of cover availability. Caption strips are sized
    to the text they hold. When provided, the genre timeline and listening clock
    are appended below the grid.
    """
    size = max(MIN_GRID, min(MAX_GRID, int(grid_size)))
    cover = 300 if size <= 4 else 220 if size <= 6 else 170
    gap = max(8, cover // 20)
    pad = max(18, cover // 12)
    header_h = max(78, int(cover * 0.27))
    footer_h = max(38, int(cover * 0.14))
    radius = max(8, cover // 18)

    width = pad * 2 + size * cover + (size - 1) * gap

    title_font = load_font(max(24, int(cover * 0.12)), bold=True)
    subtitle_font = load_font(max(15, int(cover * 0.06)))
    name_font = load_font(max(14, int(cover * 0.068)), bold=True)
    sub_font = load_font(max(12, int(cover * 0.052)))
    footer_font = load_font(max(12, int(cover * 0.048)))
    empty_font = load_font(max(13, int(cover * 0.058)), bold=True)

    has_tags = genres is not None and bool(genres.series)
    has_clock = clock is not None
    insights_gap = max(24, int(cover * 0.12))
    plot_h = max(190, min(360, int(width * 0.28)))
    dial = max(190, min(340, int(width * 0.30)))
    section_font = load_font(max(16, min(30, int(width * 0.024))), bold=True)
    band_font = load_font(max(11, min(18, int(width * 0.015))), bold=True)
    tick_font = load_font(max(10, min(15, int(width * 0.012))))
    stat_label_font = load_font(max(13, min(18, int(width * 0.016))))
    stat_value_font = load_font(max(26, min(46, int(width * 0.045))), bold=True)
    note_font = load_font(max(10, min(14, int(width * 0.012))))

    section_title_h = section_title_height(section_font)
    tick_h = _metric(tick_font) + 8
    stat_h = (
        _metric(stat_label_font)
        + 4
        + _metric(stat_value_font)
        + max(18, int(dial * 0.08))
        + _metric(stat_label_font)
        + 4
        + _metric(stat_value_font)
    )
    if has_clock and clock is not None and clock.total > clock.sampled:
        stat_h += 8 + _metric(note_font)
    clock_content_h = max(dial, stat_h)
    extra_height = 0
    if has_tags:
        extra_height += insights_gap + section_title_h + plot_h + tick_h
    if has_clock:
        extra_height += insights_gap + section_title_h + clock_content_h
    if has_tags or has_clock:
        extra_height += insights_gap

    visible = list(tiles)[: size * size]
    inner_width = cover - 20
    name_line_h = _metric(name_font) + 1
    sub_line_h = _metric(sub_font) + 1
    caption_pad = max(6, int(cover * 0.04))

    measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    layouts: list[tuple[list[str], int]] = []
    for index in range(size * size):
        tile = visible[index] if index < len(visible) else None
        if tile is None:
            layouts.append(([], 0))
            continue
        name_lines = _wrap_text(
            measure,
            tile.name,
            name_font,
            inner_width,
            max_lines=1 if tile.subtitle else 2,
        )
        content_h = len(name_lines) * name_line_h
        if tile.subtitle:
            content_h += sub_line_h
        layouts.append((name_lines, content_h))

    caption_h = max((content_h for _, content_h in layouts), default=0) + caption_pad * 2
    tile_h = cover + caption_h
    height = pad + header_h + gap + size * tile_h + (size - 1) * gap + extra_height + footer_h + pad

    canvas = Image.new("RGBA", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(canvas)

    draw.text(
        (pad, pad), _ellipsize(draw, title, title_font, width - pad * 2), font=title_font, fill=TEXT
    )
    draw.text(
        (pad, pad + _metric(title_font) + 4),
        _ellipsize(draw, subtitle, subtitle_font, width - pad * 2),
        font=subtitle_font,
        fill=MUTED,
    )
    accent_y = pad + header_h - 8
    draw.rectangle((pad, accent_y, pad + min(width - pad * 2, 84), accent_y + 3), fill=ACCENT)

    for index in range(size * size):
        row, column = divmod(index, size)
        tile = visible[index] if index < len(visible) else None
        x = pad + column * (cover + gap)
        y = pad + header_h + gap + row * (tile_h + gap)

        draw.rounded_rectangle(
            (x, y, x + cover, y + tile_h),
            radius=radius,
            fill=SURFACE,
        )

        if tile is None:
            _draw_centered(
                draw,
                (x + cover / 2, y + cover / 2),
                "Not enough data",
                empty_font,
                EMPTY_TEXT,
            )
            continue

        image = None
        if tile.image_url and not is_placeholder(tile.image_url):
            image = _cover_image(covers.get(tile.image_url), cover)
        if image is None:
            image = _placeholder(tile.name, cover)

        mask = _rounded_mask((cover, cover), radius, bottom=False)
        canvas.paste(image.convert("RGBA"), (x, y), mask)

        # Caption bar.
        caption_top = y + cover
        draw.rounded_rectangle(
            (x, caption_top, x + cover, y + tile_h),
            radius=radius,
            fill=SURFACE_LIGHT,
        )
        draw.rectangle((x, caption_top, x + cover, caption_top + radius), fill=SURFACE_LIGHT)

        name_lines, content_h = layouts[index]
        content_top = caption_top + (caption_h - content_h) / 2
        for line_index, line in enumerate(name_lines):
            _draw_centered(
                draw,
                (x + cover / 2, content_top + name_line_h * (line_index + 0.5)),
                line,
                name_font,
                TEXT,
            )
        if tile.subtitle:
            _draw_centered(
                draw,
                (
                    x + cover / 2,
                    content_top + len(name_lines) * name_line_h + sub_line_h / 2,
                ),
                _ellipsize(draw, tile.subtitle, sub_font, inner_width),
                sub_font,
                MUTED,
            )

    grid_bottom = pad + header_h + gap + size * tile_h + (size - 1) * gap
    section_y = grid_bottom

    if has_tags and genres is not None:
        section_y += insights_gap
        consumed = draw_section_title(
            draw,
            left=pad,
            right=width - pad,
            top=section_y,
            title="Top tags",
            font=section_font,
        )
        plot_top = section_y + consumed
        plot_bottom = plot_top + plot_h
        tick_y = plot_bottom + 8
        draw_genre_chart(
            canvas,
            draw,
            genres,
            left=pad,
            top=plot_top,
            right=width - pad,
            bottom=plot_bottom,
            tick_y=tick_y,
            label_font=band_font,
            tick_font=tick_font,
        )
        section_y = tick_y + _metric(tick_font)

    if has_clock and clock is not None:
        section_y += insights_gap
        consumed = draw_section_title(
            draw,
            left=pad,
            right=width - pad,
            top=section_y,
            title="Listening clock",
            font=section_font,
        )
        content_top = section_y + consumed
        stats_left = pad + dial + max(24, int(width * 0.04))
        draw_listening_clock(
            canvas,
            draw,
            left=pad,
            top=content_top,
            diameter=dial,
            stats_left=stats_left,
            stats_right=width - pad,
            clock=clock,
            value_font=stat_value_font,
            label_font=stat_label_font,
            small_font=note_font,
        )

    footer_y = height - pad - footer_h
    draw.line((pad, footer_y, width - pad, footer_y), fill=(48, 48, 58, 255), width=1)
    draw.text(
        (pad, footer_y + 12),
        _ellipsize(draw, footer, footer_font, width - pad * 2 - 80),
        font=footer_font,
        fill=MUTED,
    )
    brand = "last.fm"
    brand_box = draw.textbbox((0, 0), brand, font=footer_font)
    draw.text(
        (width - pad - (brand_box[2] - brand_box[0]), footer_y + 12),
        brand,
        font=footer_font,
        fill=(214, 52, 80, 255),
    )

    output = io.BytesIO()
    canvas.convert("RGB").save(output, format="PNG", optimize=True)
    return output.getvalue()


def _cache_path(cache_dir: Path, url: str, content_type: str = "") -> Path:
    digest = hashlib.sha256(url.encode("utf-8", "replace")).hexdigest()
    suffix = ".bin"
    lowered = content_type.lower()
    if "png" in lowered:
        suffix = ".png"
    elif "webp" in lowered:
        suffix = ".webp"
    elif "gif" in lowered:
        suffix = ".gif"
    elif "jpeg" in lowered or "jpg" in lowered:
        suffix = ".jpg"
    return cache_dir / f"{digest}{suffix}"


async def _read_cache(path: Path) -> bytes | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    if time.time() - stat.st_mtime > CACHE_TTL_SECONDS or stat.st_size > MAX_IMAGE_BYTES:
        with contextlib.suppress(OSError):
            path.unlink()
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


async def _write_cache(path: Path, data: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, data)
    except OSError:
        log.debug("Could not write cover cache file %s", path, exc_info=True)


async def _prune_cache(cache_dir: Path) -> None:
    try:
        files = [entry for entry in cache_dir.iterdir() if entry.is_file()]
    except OSError:
        return
    if len(files) <= CACHE_MAX_FILES:
        return

    def sort_key(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    for path in sorted(files, key=sort_key)[: len(files) - CACHE_MAX_FILES]:
        with contextlib.suppress(OSError):
            path.unlink()


async def download_covers(
    urls: Sequence[str | None],
    *,
    cache_dir: Path | None = None,
    session: aiohttp.ClientSession | None = None,
) -> dict[str, bytes | None]:
    """Download and cache usable cover images.

    The returned mapping contains every unique non-empty URL, with ``None`` for
    downloads that failed or were rejected. Placeholder URLs are not fetched.
    """
    unique: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if not url or is_placeholder(url) or url in seen:
            continue
        seen.add(url)
        unique.append(url)

    results: dict[str, bytes | None] = {}
    if not unique:
        return results

    if cache_dir is not None:
        for url in unique:
            for cached in cache_dir.glob(f"{hashlib.sha256(url.encode()).hexdigest()}.*"):
                data = await _read_cache(cached)
                if data:
                    results[url] = data
                    break

    pending = [url for url in unique if url not in results]
    owned_session = False
    if pending and session is None:
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"User-Agent": "lastfm-summary-cog/1.2 (cover fetch)"},
        )
        owned_session = True

    semaphore = asyncio.Semaphore(6)

    async def fetch(url: str) -> None:
        assert session is not None
        async with semaphore:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status != 200:
                        results[url] = None
                        return
                    content_type = response.headers.get("Content-Type", "")
                    if content_type and not (
                        content_type.startswith("image/")
                        or content_type.startswith("application/octet-stream")
                    ):
                        results[url] = None
                        return
                    chunks: list[bytes] = []
                    total = 0
                    while True:
                        chunk = await response.content.read(64 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_IMAGE_BYTES:
                            results[url] = None
                            return
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    if not data:
                        results[url] = None
                        return
                    results[url] = data
                    if cache_dir is not None:
                        await _write_cache(_cache_path(cache_dir, url, content_type), data)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                results[url] = None

    try:
        if pending:
            await asyncio.gather(*(fetch(url) for url in pending))
    finally:
        if owned_session and session is not None:
            await session.close()

    if cache_dir is not None:
        await _prune_cache(cache_dir)

    for url in unique:
        results.setdefault(url, None)
    return results
