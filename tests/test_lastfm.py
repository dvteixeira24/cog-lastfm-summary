from datetime import datetime, timezone

import aiohttp
import pytest

from lastfmsummary.lastfm import (
    Entry,
    LastFMClient,
    LastFMError,
    LastFMUnavailable,
    is_placeholder,
    select_image,
)


class FakeResponse:
    def __init__(self, status=200, payload=None, json_error=None):
        self.status = status
        self.payload = payload
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
    """Minimal aiohttp.ClientSession stand-in with per-user responses."""

    def __init__(self, responses):
        self.responses = responses
        self.requests = []
        self.closed = False

    def get(self, url, params=None, **kwargs):
        self.requests.append(dict(params or {}))
        key = (params or {}).get("user")
        response = self.responses.get(key)
        if isinstance(response, list):
            response = response.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def close(self):
        self.closed = True


def client_with(responses, monkeypatch, sleeps=None):
    recorded = sleeps if sleeps is not None else []

    async def fake_sleep(delay):
        recorded.append(delay)

    monkeypatch.setattr("lastfmsummary.lastfm.asyncio.sleep", fake_sleep)
    session = FakeSession(responses)
    client = LastFMClient("test-key", session=session)
    return client, session, recorded


def artist_payload(*rows):
    return {
        "topartists": {
            "artist": [
                {
                    "name": name,
                    "playcount": str(plays),
                    "image": [
                        {"size": "small", "#text": f"https://img/{name}/small.jpg"},
                        {"size": "extralarge", "#text": f"https://img/{name}/xl.jpg"},
                    ],
                }
                for name, plays in rows
            ]
        }
    }


@pytest.mark.asyncio
async def test_get_user_returns_canonical_name(monkeypatch):
    client, session, _ = client_with(
        {"someone": FakeResponse(payload={"user": {"name": "Someone", "realname": "A Person"}})},
        monkeypatch,
    )
    user = await client.get_user("someone")
    assert user.username == "Someone"
    assert user.display_name == "Someone"
    assert session.requests[0]["method"] == "user.getinfo"
    assert session.requests[0]["api_key"] == "test-key"
    assert session.requests[0]["format"] == "json"


@pytest.mark.asyncio
async def test_permanent_api_error_is_raised(monkeypatch):
    client, session, sleeps = client_with(
        {"ghost": FakeResponse(payload={"error": 6, "message": "User not found"})},
        monkeypatch,
    )
    with pytest.raises(LastFMError) as excinfo:
        await client.get_user("ghost")
    assert excinfo.value.code == 6
    assert len(session.requests) == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_top_artists_parsing_and_image_choice(monkeypatch):
    payload = artist_payload(("Radiohead", 12), ("Portishead", 3))
    payload["topartists"]["artist"].append({"name": "", "playcount": "1"})
    payload["topartists"]["artist"].append("garbage")
    client, _, _ = client_with({"someone": FakeResponse(payload=payload)}, monkeypatch)

    entries = await client.get_top_artists("someone", period="7day", limit=10)
    assert entries == (
        Entry("Radiohead", None, 12, "https://img/Radiohead/xl.jpg"),
        Entry("Portishead", None, 3, "https://img/Portishead/xl.jpg"),
    )


@pytest.mark.asyncio
async def test_top_albums_parsing_includes_artist(monkeypatch):
    payload = {
        "topalbums": {
            "album": [
                {
                    "name": "Kid A",
                    "playcount": "9",
                    "artist": {"name": "Radiohead"},
                    "image": [{"size": "mega", "#text": "https://img/kida.jpg"}],
                },
                {
                    "name": "Dummy",
                    "playcount": "4",
                    "artist": "Portishead",
                    "image": [],
                },
            ]
        }
    }
    client, _, _ = client_with({"someone": FakeResponse(payload=payload)}, monkeypatch)
    entries = await client.get_top_albums("someone", period="1month")
    assert entries[0] == Entry("Kid A", "Radiohead", 9, "https://img/kida.jpg")
    assert entries[1] == Entry("Dummy", "Portishead", 4, None)


@pytest.mark.asyncio
async def test_retries_rate_limit_then_succeeds(monkeypatch):
    client, session, sleeps = client_with(
        {
            "someone": [
                FakeResponse(payload={"error": 29, "message": "Rate limit exceeded"}),
                FakeResponse(payload=artist_payload(("Radiohead", 5))),
            ]
        },
        monkeypatch,
    )
    entries = await client.get_top_artists("someone", period="7day")
    assert [entry.name for entry in entries] == ["Radiohead"]
    assert len(session.requests) == 2
    assert sleeps == [0.5]


@pytest.mark.asyncio
async def test_retries_http_500_then_succeeds(monkeypatch):
    client, session, sleeps = client_with(
        {
            "someone": [
                FakeResponse(status=500),
                FakeResponse(payload=artist_payload(("Radiohead", 5))),
            ]
        },
        monkeypatch,
    )
    entries = await client.get_top_artists("someone", period="7day")
    assert entries[0].name == "Radiohead"
    assert len(session.requests) == 2
    assert sleeps == [0.5]


@pytest.mark.asyncio
async def test_network_failure_becomes_unavailable_after_retries(monkeypatch):
    client, session, sleeps = client_with(
        {"someone": aiohttp.ClientConnectionError("no network")}, monkeypatch
    )
    with pytest.raises(LastFMUnavailable):
        await client.get_top_artists("someone", period="7day")
    assert len(session.requests) == 3
    assert sleeps == [0.5, 1.0]


@pytest.mark.asyncio
async def test_invalid_json_retries(monkeypatch):
    client, _, sleeps = client_with(
        {
            "someone": [
                FakeResponse(json_error=ValueError("bad json")),
                FakeResponse(payload=artist_payload(("Radiohead", 1))),
            ]
        },
        monkeypatch,
    )
    entries = await client.get_top_artists("someone", period="7day")
    assert entries[0].name == "Radiohead"
    assert sleeps == [0.5]


def test_select_image_prefers_largest_and_ignores_placeholders():
    images = [
        {"size": "small", "#text": "https://img/small.jpg"},
        {"size": "large", "#text": "https://img/large.jpg"},
        {
            "size": "mega",
            "#text": "https://lastfm.freetls.fastly.net/i/u/300x300/2a96cbd8b46e442fc41c2b86b821562f.png",
        },
        {"size": "extralarge", "#text": "https://img/xl.jpg"},
        {"size": "medium", "#text": "#"},
    ]
    assert select_image(images) == "https://img/xl.jpg"
    assert select_image([]) is None
    assert select_image(None) is None
    assert select_image([{"size": "large", "#text": ""}]) is None


def test_is_placeholder():
    assert is_placeholder(None)
    assert is_placeholder("")
    assert is_placeholder("#")
    assert is_placeholder("https://img/2A96CBD8B46E442FC41C2B86B821562F.png")
    assert is_placeholder("https://example/lastfm_img/default.png")
    assert not is_placeholder("https://img/real-cover.jpg")


@pytest.mark.asyncio
async def test_fetch_charts_maps_period_and_reports_failures(monkeypatch):
    payload = artist_payload(("Radiohead", 5))
    client, session, _ = client_with(
        {
            "alice": FakeResponse(payload=payload),
            "bob": FakeResponse(payload={"error": 6, "message": "User not found"}),
            "cara": FakeResponse(status=500),
        },
        monkeypatch,
    )
    charts = await client.fetch_charts(
        ["alice", "bob", "cara"], source="artists", period="weekly", limit=25
    )
    assert [chart.username for chart in charts] == ["alice"]
    periods = {request.get("period") for request in session.requests}
    assert periods == {"7day"}
    assert all(request["limit"] == 25 for request in session.requests)


@pytest.mark.asyncio
async def test_fetch_charts_raises_on_invalid_api_key(monkeypatch):
    client, _, _ = client_with(
        {"alice": FakeResponse(payload={"error": 10, "message": "Invalid API key"})},
        monkeypatch,
    )
    with pytest.raises(LastFMError) as excinfo:
        await client.fetch_charts(["alice"], source="artists", period="weekly")
    assert excinfo.value.code == 10


@pytest.mark.asyncio
async def test_fetch_charts_uses_album_method(monkeypatch):
    payload = {"topalbums": {"album": []}}
    client, session, _ = client_with({"alice": FakeResponse(payload=payload)}, monkeypatch)
    await client.fetch_charts(["alice"], source="albums", period="monthly")
    assert session.requests[0]["method"] == "user.gettopalbums"


def recent_payload(rows, *, total=None, total_pages=1):
    return {
        "recenttracks": {
            "@attr": {
                "page": "1",
                "perPage": "200",
                "totalPages": str(total_pages),
                "total": str(total if total is not None else len(rows)),
            },
            "track": rows,
        }
    }


def recent_row(artist, track, uts):
    return {
        "artist": {"#text": artist},
        "name": track,
        "date": {"uts": str(uts)},
    }


@pytest.mark.asyncio
async def test_recent_tracks_parses_skips_now_playing_and_scales(monkeypatch):
    rows = [
        recent_row("Radiohead", "Airbag", 1000),
        {
            "artist": {"#text": "Radiohead"},
            "name": "Now Playing",
            "@attr": {"nowplaying": "true"},
        },
        {"artist": {"#text": "Broken"}, "name": "No Date"},
    ]
    client, session, _ = client_with(
        {"someone": FakeResponse(payload=recent_payload(rows, total=10, total_pages=1))},
        monkeypatch,
    )
    since = datetime.fromtimestamp(900, timezone.utc)
    until = datetime.fromtimestamp(1100, timezone.utc)
    scrobbles = await client.get_recent_tracks("someone", since=since, until=until)

    assert len(scrobbles) == 1
    assert scrobbles[0].artist == "Radiohead"
    assert scrobbles[0].track == "Airbag"
    assert scrobbles[0].played_at == datetime.fromtimestamp(1000, timezone.utc)
    assert scrobbles[0].weight == 10.0
    assert session.requests[0]["method"] == "user.getrecenttracks"
    assert session.requests[0]["from"] == 900
    assert session.requests[0]["to"] == 1100


@pytest.mark.asyncio
async def test_recent_tracks_samples_pages_evenly(monkeypatch):
    pages = {
        1: recent_payload([recent_row("A", "1", 1000)], total=2000, total_pages=20),
        6: recent_payload([recent_row("B", "2", 2000)], total=2000, total_pages=20),
        10: recent_payload([recent_row("C", "3", 3000)], total=2000, total_pages=20),
        15: recent_payload([recent_row("D", "4", 4000)], total=2000, total_pages=20),
        20: recent_payload([recent_row("E", "5", 5000)], total=2000, total_pages=20),
    }

    class PageSession(FakeSession):
        def get(self, url, params=None, **kwargs):
            self.requests.append(dict(params or {}))
            return FakeResponse(payload=pages[int(params["page"])])

    async def no_sleep(delay):
        return None

    monkeypatch.setattr("lastfmsummary.lastfm.asyncio.sleep", no_sleep)
    session = PageSession({})
    client = LastFMClient("key", session=session)
    scrobbles = await client.get_recent_tracks(
        "someone",
        since=datetime.fromtimestamp(900, timezone.utc),
        until=datetime.fromtimestamp(6000, timezone.utc),
    )

    assert [item.artist for item in scrobbles] == ["A", "B", "C", "D", "E"]
    assert [request["page"] for request in session.requests] == [1, 6, 10, 15, 20]
    assert all(item.weight == 400.0 for item in scrobbles)


@pytest.mark.asyncio
async def test_recent_tracks_tolerates_partial_page_failure(monkeypatch):
    first = recent_payload([recent_row("A", "1", 1000)], total=600, total_pages=3)
    second = recent_payload([recent_row("B", "2", 2000)], total=600, total_pages=3)

    class PageSession(FakeSession):
        def get(self, url, params=None, **kwargs):
            page = int(params["page"])
            if page == 3:
                raise aiohttp.ClientConnectionError("offline")
            self.requests.append(dict(params))
            return FakeResponse(payload=first if page == 1 else second)

    async def no_sleep(delay):
        return None

    monkeypatch.setattr("lastfmsummary.lastfm.asyncio.sleep", no_sleep)
    client = LastFMClient("key", session=PageSession({}))
    scrobbles = await client.get_recent_tracks(
        "someone",
        since=datetime.fromtimestamp(900, timezone.utc),
        until=datetime.fromtimestamp(4000, timezone.utc),
    )

    assert [item.artist for item in scrobbles] == ["A", "B"]
    assert all(item.weight == 300.0 for item in scrobbles)


@pytest.mark.asyncio
async def test_artist_top_tags_parses_and_limits(monkeypatch):
    payload = {
        "toptags": {
            "tag": [
                {"name": "shoegaze", "count": "100"},
                {"name": "dream pop", "count": "80"},
                {"name": "seen live", "count": "60"},
                {"name": "zero", "count": "0"},
                {"name": "", "count": "10"},
            ]
        }
    }
    client, session, _ = client_with(
        {None: FakeResponse(payload=payload)},
        monkeypatch,
    )
    tags = await client.get_artist_top_tags("Slowdive", limit=3)
    assert tags == (("shoegaze", 100), ("dream pop", 80), ("seen live", 60))
    assert session.requests[0]["method"] == "artist.gettoptags"
    assert session.requests[0]["artist"] == "Slowdive"


def test_client_requires_api_key():
    with pytest.raises(ValueError):
        LastFMClient("")
    with pytest.raises(ValueError):
        LastFMClient("   ")


@pytest.mark.asyncio
async def test_close_only_closes_owned_sessions(monkeypatch):
    session = FakeSession({})
    client = LastFMClient("key", session=session)
    await client.close()
    assert session.closed is False
