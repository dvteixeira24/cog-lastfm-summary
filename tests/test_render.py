import io

import pytest
from PIL import Image

from lastfmsummary.listening import GenreTimeline, ListeningClock, TagSeries
from lastfmsummary.render import Tile, download_covers, load_font, render_grid


def test_load_font_uses_bundled_work_sans():
    assert load_font(24).getname()[0] == "Work Sans"
    bold = load_font(24, bold=True)
    assert bold.getname() == ("Work Sans", "Bold")


def png_bytes(color=(120, 90, 200), size=(64, 64)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def sample_tiles(count=9):
    return [
        Tile(
            name=f"Artist Number {index}",
            subtitle="Some Album" if index % 2 else None,
            image_url=f"https://img/{index}.jpg" if index % 3 else None,
            rank=index + 1,
        )
        for index in range(count)
    ]


def render(tiles, grid_size, **kwargs):
    data = render_grid(
        tiles=tiles,
        grid_size=grid_size,
        title=kwargs.get("title", "Top Artists \u2014 Monthly"),
        subtitle=kwargs.get("subtitle", "Test Server \u2022 1\u201321 October"),
        footer=kwargs.get("footer", "4 listeners \u2022 1,234 plays \u2022 the last month"),
        covers=kwargs.get("covers", {}),
        genres=kwargs.get("genres"),
        clock=kwargs.get("clock"),
    )
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    return data


def sample_genres():
    return GenreTimeline(
        labels=("10 Sep", "17 Sep", "24 Sep", "01 Oct", "08 Oct", "15 Oct"),
        series=(
            TagSeries("electronic", (4, 6, 5, 7, 6, 8)),
            TagSeries("indie pop", (2, 3, 4, 2, 5, 3)),
            TagSeries("shoegaze", (1, 2, 1, 3, 2, 1)),
        ),
    )


def sample_clock():
    hourly = tuple(0.0 if hour != 17 else 96.0 for hour in range(24))
    return ListeningClock(
        hourly=hourly,
        busiest_hour=17,
        busiest_count=96,
        total=1000,
        sampled=1000,
    )


def test_render_extras_grow_the_image_and_stay_deterministic():
    base = render(sample_tiles(9), 3)
    with_extras = render(
        sample_tiles(9), 3, genres=sample_genres(), clock=sample_clock()
    )
    again = render(
        sample_tiles(9), 3, genres=sample_genres(), clock=sample_clock()
    )
    with Image.open(io.BytesIO(base)) as image:
        base_size = image.size
    with Image.open(io.BytesIO(with_extras)) as image:
        assert image.size[0] == base_size[0]
        assert image.size[1] > base_size[1]
    assert with_extras == again


def test_render_extras_support_one_chart_and_small_grids():
    only_tags = render(sample_tiles(4), 2, genres=sample_genres())
    only_clock = render(sample_tiles(4), 2, clock=sample_clock())
    both = render(sample_tiles(4), 2, genres=sample_genres(), clock=sample_clock())
    assert only_tags.startswith(b"\x89PNG")
    assert only_clock.startswith(b"\x89PNG")
    assert both.startswith(b"\x89PNG")


def test_render_extras_handle_a_single_tag_and_sampled_note():
    genres = GenreTimeline(
        labels=("01 Oct", "02 Oct"),
        series=(TagSeries("a very long genre name that should still fit", (3, 5)),),
    )
    clock = ListeningClock(
        hourly=tuple(1.0 for _ in range(24)),
        busiest_hour=0,
        busiest_count=2,
        total=5000,
        sampled=1000,
    )
    data = render(sample_tiles(4), 2, genres=genres, clock=clock)
    assert data.startswith(b"\x89PNG")


def test_render_caption_height_adapts_to_text():
    short = render([Tile("A", None, None, index + 1) for index in range(4)], 2)
    long = render(
        [Tile("A very long artist name that wraps", None, None, index + 1) for index in range(4)],
        2,
    )
    with Image.open(io.BytesIO(short)) as image:
        short_height = image.size[1]
    with Image.open(io.BytesIO(long)) as image:
        long_height = image.size[1]
    assert long_height > short_height


def test_render_2x2_dimensions():
    data = render(sample_tiles(4), 2)
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (665, 939)
        assert image.mode == "RGB"


def test_render_3x3_dimensions_and_cover():
    covers = {"https://img/1.jpg": png_bytes()}
    data = render(sample_tiles(9), 3, covers=covers)
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (980, 1322)


def test_render_handles_missing_covers_and_long_names():
    tiles = [
        Tile("A" * 400, "B" * 400, None, 1),
        Tile("Portishead", None, "https://img/missing.jpg", 2),
    ]
    data = render(tiles, 3)
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (980, 1322)


def test_render_truncates_extra_tiles_and_fills_empty_slots():
    data = render(sample_tiles(12), 3)
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (980, 1322)
    # No exception when there are fewer tiles than slots.
    small = render(sample_tiles(2), 2)
    assert small.startswith(b"\x89PNG")


def test_render_is_deterministic_for_placeholders():
    assert render(sample_tiles(9), 3) == render(sample_tiles(9), 3)


def test_render_clamps_grid_size():
    data = render(sample_tiles(4), 99)
    with Image.open(io.BytesIO(data)) as image:
        # Clamped to 8 -> 170 px covers.
        assert image.size == (1452, 1952)


class FakeContent:
    def __init__(self, data, chunk_size=None):
        self.data = data
        self.offset = 0
        self.chunk_size = chunk_size

    async def read(self, limit):
        size = limit if self.chunk_size is None else min(limit, self.chunk_size)
        chunk = self.data[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class FakeResponse:
    def __init__(self, data=b"", status=200, content_type="image/png", chunk_size=None):
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.content = FakeContent(data, chunk_size)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.requested = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        response = self.responses[url]
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.mark.asyncio
async def test_download_covers_dedupes_and_handles_failures(tmp_path):
    session = FakeSession(
        {
            "https://img/a.png": FakeResponse(b"aaa"),
            "https://img/b.png": FakeResponse(status=404),
            "https://img/c.png": FakeResponse(b"ccc", content_type="text/html"),
        }
    )
    urls = [
        "https://img/a.png",
        "https://img/a.png",
        "https://img/b.png",
        "https://img/c.png",
        None,
    ]
    result = await download_covers(urls, cache_dir=tmp_path, session=session)
    assert result["https://img/a.png"] == b"aaa"
    assert result["https://img/b.png"] is None
    assert result["https://img/c.png"] is None
    assert session.requested.count("https://img/a.png") == 1
    assert len(list(tmp_path.glob("*.png"))) == 1


@pytest.mark.asyncio
async def test_download_covers_uses_cache_on_second_call(tmp_path):
    session = FakeSession({"https://img/a.png": FakeResponse(b"aaa")})
    first = await download_covers(["https://img/a.png"], cache_dir=tmp_path, session=session)
    assert first["https://img/a.png"] == b"aaa"

    offline = FakeSession({"https://img/a.png": RuntimeError("should not fetch")})
    second = await download_covers(["https://img/a.png"], cache_dir=tmp_path, session=offline)
    assert second["https://img/a.png"] == b"aaa"
    assert offline.requested == []


@pytest.mark.asyncio
async def test_download_covers_reads_streamed_bodies(tmp_path):
    payload = png_bytes(size=(16, 16))
    session = FakeSession({"https://img/a.png": FakeResponse(payload, chunk_size=7)})
    result = await download_covers(["https://img/a.png"], cache_dir=tmp_path, session=session)
    assert result["https://img/a.png"] == payload


@pytest.mark.asyncio
async def test_download_covers_skips_placeholders():
    session = FakeSession({})
    result = await download_covers(
        [
            None,
            "",
            "https://lastfm.freetls.fastly.net/i/u/300x300/2a96cbd8b46e442fc41c2b86b821562f.png",
        ],
        session=session,
    )
    assert result == {}
    assert session.requested == []
