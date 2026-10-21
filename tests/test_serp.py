import json

import aiohttp
import pytest

from lastfmsummary.serp import (
    album_query,
    artist_query,
    resolve_serp_images,
)


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def json(self, content_type=None):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def get(self, url, params=None, **kwargs):
        self.requests.append(dict(params or {}))
        query = (params or {}).get("q")
        response = self.responses.get(query)
        if isinstance(response, BaseException):
            raise response
        return response


def image_payload(*rows):
    return {"images_results": list(rows)}


def image(original, width=1000, height=1000, thumbnail="https://thumb/image.jpg"):
    return {
        "original": original,
        "original_width": width,
        "original_height": height,
        "thumbnail": thumbnail,
    }


def test_query_builders():
    assert artist_query("Radiohead") == "Radiohead music artist"
    assert album_query("Radiohead", "Kid A") == "Radiohead Kid A album cover"
    assert album_query("", "Kid A") == "Kid A album cover"


@pytest.mark.asyncio
async def test_resolve_prefers_large_square_original(tmp_path):
    session = FakeSession(
        {
            "Radiohead music artist": FakeResponse(
                image_payload(
                    image("https://img/tiny.jpg", 100, 100),
                    image("https://img/wide.jpg", 2000, 500),
                    image("https://img/good.jpg", 1200, 1200),
                )
            )
        }
    )
    result = await resolve_serp_images(
        {"artist:radiohead": "Radiohead music artist"},
        api_key="serp-key",
        cache_path=tmp_path / "serp.json",
        session=session,
    )
    assert result == {"artist:radiohead": "https://img/good.jpg"}
    request = session.requests[0]
    assert request["engine"] == "google_images"
    assert request["api_key"] == "serp-key"
    assert request["safe"] == "active"
    assert request["imgar"] == "s"


@pytest.mark.asyncio
async def test_resolve_falls_back_to_first_original_then_thumbnail(tmp_path):
    session = FakeSession(
        {
            "A": FakeResponse(image_payload(image("https://img/small.jpg", 50, 50))),
            "B": FakeResponse(image_payload({"original": "", "thumbnail": "https://thumb/b.jpg"})),
        }
    )
    result = await resolve_serp_images(
        {"a": "A", "b": "B"}, api_key="key", cache_path=tmp_path / "serp.json", session=session
    )
    assert result == {"a": "https://img/small.jpg", "b": "https://thumb/b.jpg"}


@pytest.mark.asyncio
async def test_resolve_caches_hits_and_misses(tmp_path):
    cache_path = tmp_path / "serp.json"
    first = FakeSession(
        {
            "A": FakeResponse(image_payload(image("https://img/a.jpg"))),
            "B": FakeResponse(image_payload()),
        }
    )
    result = await resolve_serp_images(
        {"a": "A", "b": "B"}, api_key="key", cache_path=cache_path, session=first
    )
    assert result == {"a": "https://img/a.jpg", "b": None}
    assert cache_path.exists()

    offline = FakeSession({})
    second = await resolve_serp_images(
        {"a": "A", "b": "B"}, api_key="key", cache_path=cache_path, session=offline
    )
    assert second == result
    assert offline.requests == []
    assert set(json.loads(cache_path.read_text(encoding="utf-8"))) == {"a", "b"}


@pytest.mark.asyncio
async def test_resolve_does_not_cache_api_errors(tmp_path):
    cache_path = tmp_path / "serp.json"
    first = FakeSession({"A": FakeResponse({"error": "Invalid API key"})})
    result = await resolve_serp_images(
        {"a": "A"}, api_key="bad", cache_path=cache_path, session=first
    )
    assert result == {"a": None}

    second = FakeSession({"A": FakeResponse(image_payload(image("https://img/a.jpg")))})
    retry = await resolve_serp_images(
        {"a": "A"}, api_key="good", cache_path=cache_path, session=second
    )
    assert retry == {"a": "https://img/a.jpg"}
    assert len(second.requests) == 1


@pytest.mark.asyncio
async def test_resolve_handles_http_and_network_errors(tmp_path):
    session = FakeSession(
        {
            "A": FakeResponse(status=401),
            "B": aiohttp.ClientConnectionError("offline"),
        }
    )
    result = await resolve_serp_images(
        {"a": "A", "b": "B"}, api_key="key", cache_path=tmp_path / "serp.json", session=session
    )
    assert result == {"a": None, "b": None}


@pytest.mark.asyncio
async def test_resolve_without_api_key_makes_no_requests(tmp_path):
    session = FakeSession({})
    result = await resolve_serp_images(
        {"a": "A"}, api_key="", cache_path=tmp_path / "serp.json", session=session
    )
    assert result == {"a": None}
    assert session.requests == []


@pytest.mark.asyncio
async def test_resolve_skips_blank_queries(tmp_path):
    session = FakeSession({})
    result = await resolve_serp_images(
        {"a": "  ", "": "A"}, api_key="key", cache_path=tmp_path / "serp.json", session=session
    )
    assert result == {}
    assert session.requests == []
