import io

import pytest
from PIL import Image

from lastfmsummary.render import Tile, download_covers, render_grid


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
    )
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    return data


def test_render_2x2_dimensions():
    data = render(sample_tiles(4), 2)
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (665, 935)
        assert image.mode == "RGB"


def test_render_3x3_dimensions_and_cover():
    covers = {"https://img/1.jpg": png_bytes()}
    data = render(sample_tiles(9), 3, covers=covers)
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (980, 1316)


def test_render_handles_missing_covers_and_long_names():
    tiles = [
        Tile("A" * 400, "B" * 400, None, 1),
        Tile("Portishead", None, "https://img/missing.jpg", 2),
    ]
    data = render(tiles, 3)
    with Image.open(io.BytesIO(data)) as image:
        assert image.size[0] == 980


def test_render_truncates_extra_tiles_and_fills_empty_slots():
    data = render(sample_tiles(12), 3)
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (980, 1316)
    # No exception when there are fewer tiles than slots.
    small = render(sample_tiles(2), 2)
    assert small.startswith(b"\x89PNG")


def test_render_is_deterministic_for_placeholders():
    assert render(sample_tiles(9), 3) == render(sample_tiles(9), 3)


def test_render_clamps_grid_size():
    data = render(sample_tiles(4), 99)
    with Image.open(io.BytesIO(data)) as image:
        # Clamped to 8 -> 170 px covers.
        assert image.size == (1452, 1928)


class FakeContent:
    def __init__(self, data):
        self.data = data

    async def read(self, limit):
        return self.data[:limit]


class FakeResponse:
    def __init__(self, data=b"", status=200, content_type="image/png"):
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.content = FakeContent(data)

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
