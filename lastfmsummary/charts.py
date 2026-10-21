"""Pillow drawing for the genre streamgraph and listening clock."""

from __future__ import annotations

import bisect
from typing import Sequence

from PIL import Image, ImageDraw, ImageFont

from .listening import GenreTimeline, ListeningClock

TAG_COLORS = (
    (195, 155, 240, 255),
    (98, 211, 196, 255),
    (74, 159, 245, 255),
    (62, 92, 160, 255),
    (123, 91, 214, 255),
    (78, 159, 134, 255),
    (192, 108, 224, 255),
)
TEXT = (244, 244, 248, 255)
MUTED = (160, 160, 175, 255)
RULE = (48, 48, 58, 255)
CLOCK_TRACK = (36, 36, 45, 255)
CLOCK_LOW = (143, 193, 255, 255)
CLOCK_HIGH = (59, 130, 246, 255)
CLOCK_PEAK = (43, 127, 255, 255)


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


def _fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> str:
    value = " ".join(str(text or "").split())
    if not value:
        return ""
    if draw.textbbox((0, 0), value, font=font)[2] <= max_width:
        return value
    while len(value) > 1:
        value = value[:-1]
        if draw.textbbox((0, 0), value + "\u2026", font=font)[2] <= max_width:
            return value.rstrip() + "\u2026"
    return "\u2026"


def section_title_height(font: ImageFont.ImageFont) -> int:
    """Height consumed by a section title and its rule."""
    return _metric(font) + 18


def draw_section_title(
    draw: ImageDraw.ImageDraw,
    *,
    left: int,
    right: int,
    top: int,
    title: str,
    font: ImageFont.ImageFont,
) -> int:
    """Draw an uppercase section heading and hairline. Returns its height."""
    draw.text((left, top), title.upper(), font=font, fill=TEXT)
    rule_y = top + _metric(font) + 8
    draw.line((left, rule_y, right, rule_y), fill=RULE, width=1)
    return section_title_height(font)


def _catmull(p0: float, p1: float, p2: float, p3: float, t: float) -> float:
    return 0.5 * (
        (2 * p1)
        + (-p0 + p2) * t
        + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t * t
        + (-p0 + 3 * p1 - 3 * p2 + p3) * t * t * t
    )


def _value_at(values: Sequence[float], xs: Sequence[float], x: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1 or x <= xs[0]:
        return max(0.0, float(values[0]))
    if x >= xs[-1]:
        return max(0.0, float(values[-1]))
    index = min(bisect.bisect_right(xs, x) - 1, len(values) - 2)
    span = xs[index + 1] - xs[index]
    if span <= 0:
        return max(0.0, float(values[index]))
    t = (x - xs[index]) / span
    p0 = values[max(0, index - 1)]
    p1 = values[index]
    p2 = values[index + 1]
    p3 = values[min(len(values) - 1, index + 2)]
    return max(0.0, _catmull(p0, p1, p2, p3, t))


def draw_genre_chart(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    timeline: GenreTimeline,
    *,
    left: int,
    top: int,
    right: int,
    bottom: int,
    tick_y: int,
    label_font: ImageFont.ImageFont,
    tick_font: ImageFont.ImageFont,
) -> None:
    """Draw a smooth, centered streamgraph for the top tags."""
    series = [item for item in timeline.series if item.values]
    labels = list(timeline.labels)
    if not series or not labels:
        return

    count = len(labels)
    width = max(1, right - left)
    height = max(1, bottom - top)
    values = [
        [max(0.0, float(value)) for value in item.values[:count]]
        + [0.0] * max(0, count - len(item.values))
        for item in series
    ]
    if count == 1:
        xs = [left + width / 2]
    else:
        xs = [left + width * index / (count - 1) for index in range(count)]

    totals = [sum(item[index] for item in values) for index in range(count)]
    max_total = max(totals, default=0.0)
    if max_total <= 0:
        return

    scale = max(0.0, height - 16) / max_total
    center = top + height / 2
    steps = max(2, int(width / 2))
    sample_xs = [left + width * index / (steps - 1) for index in range(steps)]
    interpolated = [[_value_at(item, xs, x) for x in sample_xs] for item in values]

    supersample = 3
    band_layer = Image.new(
        "RGBA", (max(1, width * supersample), max(1, height * supersample)), (0, 0, 0, 0)
    )
    band_draw = ImageDraw.Draw(band_layer)
    for series_index in range(len(interpolated)):
        upper: list[tuple[float, float]] = []
        lower: list[tuple[float, float]] = []
        for step, x in enumerate(sample_xs):
            total = sum(series_values[step] for series_values in interpolated)
            stack_top = center - total * scale / 2
            before = sum(interpolated[index][step] for index in range(series_index))
            band_top = stack_top + before * scale
            band_bottom = band_top + interpolated[series_index][step] * scale
            upper.append(((x - left) * supersample, (band_top - top) * supersample))
            lower.append(((x - left) * supersample, (band_bottom - top) * supersample))
        polygon = upper + list(reversed(lower))
        if len(polygon) >= 3:
            band_draw.polygon(polygon, fill=TAG_COLORS[series_index % len(TAG_COLORS)])
    smoothed = band_layer.resize((width, height), Image.Resampling.LANCZOS)
    canvas.alpha_composite(smoothed, (left, top))

    placed: list[tuple[float, float, float, float]] = []
    for series_index in range(len(interpolated)):
        best: tuple[float, float, float, float] | None = None
        for fraction in (0.4, 0.55, 0.7):
            x = left + width * fraction
            nearest = _nearest(sample_xs, x)
            total = sum(series_values[nearest] for series_values in interpolated)
            stack_top = center - total * scale / 2
            before = sum(interpolated[index][nearest] for index in range(series_index))
            band_top = stack_top + before * scale
            band_bottom = band_top + interpolated[series_index][nearest] * scale
            band_height = band_bottom - band_top
            if best is None or band_height > best[3]:
                best = (x, band_top, band_bottom, band_height)

        if best is None or best[3] < _metric(label_font) + 4:
            continue
        text = series[series_index].name
        box = draw.textbbox((0, 0), text, font=label_font)
        text_width = box[2] - box[0]
        text_height = box[3] - box[1]
        text_left = best[0] - text_width / 2
        text_top = (best[1] + best[2]) / 2 - text_height / 2 - box[1]
        candidate = (text_left, text_top, text_left + text_width, text_top + text_height)
        if any(_overlaps(candidate, other) for other in placed):
            continue
        placed.append(candidate)
        draw.text((text_left, text_top), text, font=label_font, fill=TEXT)

    for x, label in zip(xs, labels):
        _draw_centered(draw, (x, tick_y + _metric(tick_font) / 2), label, tick_font, MUTED)


def _nearest(xs: Sequence[float], x: float) -> int:
    index = bisect.bisect_left(xs, x)
    if index <= 0:
        return 0
    if index >= len(xs):
        return len(xs) - 1
    before = xs[index - 1]
    after = xs[index]
    return index - 1 if x - before <= after - x else index


def _overlaps(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
    padding: float = 4.0,
) -> bool:
    return not (
        first[2] + padding <= second[0]
        or second[2] + padding <= first[0]
        or first[3] + padding <= second[1]
        or second[3] + padding <= first[1]
    )


def _blend(
    start: tuple[int, int, int, int],
    end: tuple[int, int, int, int],
    amount: float,
) -> tuple[int, int, int, int]:
    amount = max(0.0, min(1.0, amount))
    return (
        int(round(start[0] + (end[0] - start[0]) * amount)),
        int(round(start[1] + (end[1] - start[1]) * amount)),
        int(round(start[2] + (end[2] - start[2]) * amount)),
        255,
    )


def draw_listening_clock(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    *,
    left: int,
    top: int,
    diameter: int,
    stats_left: int,
    stats_right: int,
    clock: ListeningClock,
    value_font: ImageFont.ImageFont,
    label_font: ImageFont.ImageFont,
    small_font: ImageFont.ImageFont,
) -> None:
    """Draw the 24-hour radial dial, inner hour labels, and summary stats."""
    diameter = max(20, int(diameter))
    supersample = 4
    size = diameter * supersample
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    layer_draw = ImageDraw.Draw(layer)
    outer = size / 2 - supersample
    inner = outer * 0.62
    box = (size / 2 - outer, size / 2 - outer, size / 2 + outer, size / 2 + outer)

    for hour in range(24):
        start = -90 + hour * 15 + 1
        layer_draw.pieslice(box, start=start, end=start + 13, fill=CLOCK_TRACK)

    peak = max(clock.hourly) if clock.hourly else 0.0
    if peak > 0:
        for hour, value in enumerate(clock.hourly[:24]):
            if value <= 0:
                continue
            share = max(0.0, min(1.0, value / peak))
            radius = inner + (outer - inner) * share
            angle = -90 + hour * 15 + 1
            color = (
                CLOCK_PEAK if hour == clock.busiest_hour else _blend(CLOCK_LOW, CLOCK_HIGH, share)
            )
            layer_draw.pieslice(
                (
                    size / 2 - radius,
                    size / 2 - radius,
                    size / 2 + radius,
                    size / 2 + radius,
                ),
                start=angle,
                end=angle + 13,
                fill=color,
            )

    hole = (
        size / 2 - inner,
        size / 2 - inner,
        size / 2 + inner,
        size / 2 + inner,
    )
    layer_draw.ellipse(hole, fill=(0, 0, 0, 0))
    dial = layer.resize((diameter, diameter), Image.Resampling.LANCZOS)
    canvas.alpha_composite(dial, (left, top))

    center = (left + diameter / 2, top + diameter / 2)
    inner_scaled = diameter * 0.26
    _draw_centered(draw, (center[0], center[1] - inner_scaled), "00", small_font, MUTED)
    _draw_centered(draw, (center[0] + inner_scaled, center[1]), "06", small_font, MUTED)
    _draw_centered(draw, (center[0], center[1] + inner_scaled), "12", small_font, MUTED)
    _draw_centered(draw, (center[0] - inner_scaled, center[1]), "18", small_font, MUTED)

    hour = clock.busiest_hour % 12 or 12
    suffix = "AM" if clock.busiest_hour < 12 else "PM"
    busiest = f"{hour}:00{suffix}"

    x = stats_left
    max_width = max(1, stats_right - stats_left)
    y = top + max(
        0.0,
        (diameter - _stats_height(clock, diameter, value_font, label_font, small_font)) / 2,
    )
    draw.text((x, y), "Busiest hour", font=label_font, fill=MUTED)
    y += _metric(label_font) + 4
    draw.text((x, y), busiest, font=value_font, fill=TEXT)
    y += _metric(value_font) + max(18, int(diameter * 0.08))
    draw.text(
        (x, y),
        _fit_text(draw, "Scrobbles in busiest hour", label_font, max_width),
        font=label_font,
        fill=MUTED,
    )
    y += _metric(label_font) + 4
    draw.text((x, y), f"{clock.busiest_count:,}", font=value_font, fill=TEXT)
    y += _metric(value_font)

    if clock.total > clock.sampled:
        y += 8
        note = f"Estimated from {clock.sampled:,} of {clock.total:,} scrobbles"
        draw.text(
            (x, y),
            _fit_text(draw, note, small_font, max_width),
            font=small_font,
            fill=MUTED,
        )


def _stats_height(
    clock: ListeningClock,
    diameter: int,
    value_font: ImageFont.ImageFont,
    label_font: ImageFont.ImageFont,
    small_font: ImageFont.ImageFont,
) -> int:
    height = (
        _metric(label_font)
        + 4
        + _metric(value_font)
        + max(18, int(diameter * 0.08))
        + _metric(label_font)
        + 4
        + _metric(value_font)
    )
    if clock.total > clock.sampled:
        height += 8 + _metric(small_font)
    return height


__all__ = [
    "draw_genre_chart",
    "draw_listening_clock",
    "draw_section_title",
    "section_title_height",
]
