import json

import aiohttp
import pytest

from lastfmsummary.deezer import (
    album_key,
    artist_key,
    resolve_album_covers,
    resolve_artist_images,
)


class FakeResponse:
    def __init__(self, payload=None, status=200, json_error=None):
        self.payload = payload
        self.status = status
        self.json_error = json_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def json(self, content_type=None):
        if self.json_error is not None:
            raise self.json_error
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


def artist_payload(*names):
    return {
        "data": [
            {
                "name": name,
                "picture_xl": f"https://cdn/{index}/1000x1000.jpg",
                "picture_big": f"https://cdn/{index}/500x500.jpg",
                "nb_fan": 100 - index,
            }
            for index, name in enumerate(names)
        ]
    }


def test_artist_key_normalizes_case_accents_and_punctuation():
    assert artist_key("Björk") == "bjork"
    assert artist_key("BJORK") == "bjork"
    assert artist_key("Simon & Garfunkel") == "simonandgarfunkel"
    assert artist_key("FKA twigs") == "fkatwigs"
    assert artist_key("A$AP Rocky") == "asaprocky"
    assert artist_key("   ") == ""


@pytest.mark.asyncio
async def test_resolve_prefers_exact_match_and_xl_image(tmp_path):
    session = FakeSession(
        {
            "Radiohead": FakeResponse(
                artist_payload("DJ Radiohead", "Radiohead"),
            )
        }
    )
    result = await resolve_artist_images(
        ["Radiohead"], cache_path=tmp_path / "deezer.json", session=session
    )
    assert result == {"radiohead": "https://cdn/1/1000x1000.jpg"}
    assert session.requests == [{"q": "Radiohead", "limit": 5}]


@pytest.mark.asyncio
async def test_resolve_matches_accents_and_case(tmp_path):
    session = FakeSession({"björk": FakeResponse(artist_payload("Bjork"))})
    result = await resolve_artist_images(
        ["björk"], cache_path=tmp_path / "deezer.json", session=session
    )
    assert result == {"bjork": "https://cdn/0/1000x1000.jpg"}


@pytest.mark.asyncio
async def test_resolve_rejects_unrelated_first_result(tmp_path):
    session = FakeSession({"Home": FakeResponse(artist_payload("Homeboy"))})
    result = await resolve_artist_images(
        ["Home"], cache_path=tmp_path / "deezer.json", session=session
    )
    assert result == {"home": None}


@pytest.mark.asyncio
async def test_resolve_handles_missing_picture_and_empty_results(tmp_path):
    session = FakeSession(
        {
            "No Picture": FakeResponse({"data": [{"name": "No Picture", "picture_xl": ""}]}),
            "Nothing": FakeResponse({"data": []}),
        }
    )
    result = await resolve_artist_images(
        ["No Picture", "Nothing"], cache_path=tmp_path / "deezer.json", session=session
    )
    assert result == {"nopicture": None, "nothing": None}


@pytest.mark.asyncio
async def test_resolve_treats_network_errors_as_missing(tmp_path):
    session = FakeSession({"Offline": aiohttp.ClientConnectionError("down")})
    result = await resolve_artist_images(
        ["Offline"], cache_path=tmp_path / "deezer.json", session=session
    )
    assert result == {"offline": None}


@pytest.mark.asyncio
async def test_resolve_caches_hits_and_misses(tmp_path):
    cache_path = tmp_path / "deezer.json"
    first = FakeSession(
        {
            "Radiohead": FakeResponse(artist_payload("Radiohead")),
            "Unknown": FakeResponse({"data": []}),
        }
    )
    result = await resolve_artist_images(
        ["Radiohead", "Unknown"], cache_path=cache_path, session=first
    )
    assert result == {"radiohead": "https://cdn/0/1000x1000.jpg", "unknown": None}
    assert cache_path.exists()

    offline = FakeSession({})
    second = await resolve_artist_images(
        ["Radiohead", "Unknown"], cache_path=cache_path, session=offline
    )
    assert second == result
    assert offline.requests == []
    saved = json.loads(cache_path.read_text(encoding="utf-8"))
    assert set(saved) == {"radiohead", "unknown"}


@pytest.mark.asyncio
async def test_resolve_ignores_expired_cache_entries(tmp_path):
    cache_path = tmp_path / "deezer.json"
    cache_path.write_text(
        json.dumps({"radiohead": {"url": "https://old/image.jpg", "expires": 1}}),
        encoding="utf-8",
    )
    session = FakeSession({"Radiohead": FakeResponse(artist_payload("Radiohead"))})
    result = await resolve_artist_images(["Radiohead"], cache_path=cache_path, session=session)
    assert result["radiohead"] == "https://cdn/0/1000x1000.jpg"
    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_resolve_skips_blank_names(tmp_path):
    session = FakeSession({})
    result = await resolve_artist_images(
        ["", "   "], cache_path=tmp_path / "deezer.json", session=session
    )
    assert result == {}
    assert session.requests == []


def test_album_key_includes_artist_and_album():
    assert album_key("Radiohead", "Kid A") == "radiohead::kida"
    assert album_key("radiohead", "kid a") == "radiohead::kida"
    assert album_key("", "Kid A") == "::kida"


def album_payload(artist, title, cover="https://cdn/cover.jpg"):
    return {
        "data": [
            {
                "title": title,
                "artist": {"name": artist},
                "cover_xl": cover,
                "cover_big": "https://cdn/cover-500.jpg",
            }
        ]
    }


@pytest.mark.asyncio
async def test_resolve_album_cover_exact_match(tmp_path):
    session = FakeSession({"Radiohead Kid A": FakeResponse(album_payload("Radiohead", "Kid A"))})
    result = await resolve_album_covers(
        [("Radiohead", "Kid A")], cache_path=tmp_path / "deezer-albums.json", session=session
    )
    assert result == {"radiohead::kida": "https://cdn/cover.jpg"}
    assert session.requests == [{"q": "Radiohead Kid A", "limit": 5}]


@pytest.mark.asyncio
async def test_resolve_album_rejects_wrong_artist(tmp_path):
    session = FakeSession(
        {"Radiohead Kid A": FakeResponse(album_payload("Some Cover Band", "Kid A"))}
    )
    result = await resolve_album_covers(
        [("Radiohead", "Kid A")], cache_path=tmp_path / "deezer-albums.json", session=session
    )
    assert result == {"radiohead::kida": None}


@pytest.mark.asyncio
async def test_resolve_album_accepts_deluxe_suffix(tmp_path):
    session = FakeSession(
        {"Radiohead Kid A": FakeResponse(album_payload("Radiohead", "Kid A (Deluxe Edition)"))}
    )
    result = await resolve_album_covers(
        [("Radiohead", "Kid A")], cache_path=tmp_path / "deezer-albums.json", session=session
    )
    assert result["radiohead::kida"] == "https://cdn/cover.jpg"
