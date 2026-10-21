import pytest

from lastfmsummary.tags import resolve_artist_tags


class FakeClient:
    def __init__(self, results):
        self.results = results
        self.calls = []

    async def get_artist_top_tags(self, artist, *, limit=3):
        self.calls.append(artist)
        value = self.results[artist]
        if isinstance(value, BaseException):
            raise value
        return value


@pytest.mark.asyncio
async def test_resolve_artist_tags_caches_hits_and_misses(tmp_path):
    cache = tmp_path / "tags.json"
    first = FakeClient({"Radiohead": (("art rock", 100),), "Unknown": ()})
    result = await resolve_artist_tags(["Radiohead", "Unknown"], client=first, cache_path=cache)
    assert result["radiohead"] == (("art rock", 100),)
    assert result["unknown"] == ()
    assert cache.exists()

    second = FakeClient({"Radiohead": pytest.fail, "Unknown": pytest.fail})
    cached = await resolve_artist_tags(["Radiohead", "Unknown"], client=second, cache_path=cache)
    assert cached == result
    assert second.calls == []


@pytest.mark.asyncio
async def test_resolve_artist_tags_does_not_cache_errors(tmp_path):
    cache = tmp_path / "tags.json"
    failing = FakeClient({"Radiohead": RuntimeError("down")})
    result = await resolve_artist_tags(["Radiohead"], client=failing, cache_path=cache)
    assert result == {"radiohead": ()}

    good = FakeClient({"Radiohead": (("art rock", 100),)})
    retried = await resolve_artist_tags(["Radiohead"], client=good, cache_path=cache)
    assert retried["radiohead"] == (("art rock", 100),)
    assert good.calls == ["Radiohead"]


@pytest.mark.asyncio
async def test_resolve_artist_tags_expires_entries(tmp_path):
    cache = tmp_path / "tags.json"
    cache.write_text('{"radiohead":{"tags":[["old",1]],"expires":1}}', encoding="utf-8")
    client = FakeClient({"Radiohead": (("art rock", 100),)})
    result = await resolve_artist_tags(["Radiohead"], client=client, cache_path=cache)
    assert result["radiohead"] == (("art rock", 100),)
    assert client.calls == ["Radiohead"]


@pytest.mark.asyncio
async def test_resolve_artist_tags_ignores_corrupt_cache(tmp_path):
    cache = tmp_path / "tags.json"
    cache.write_text("not json", encoding="utf-8")
    client = FakeClient({"Radiohead": (("art rock", 100),)})
    result = await resolve_artist_tags(["Radiohead"], client=client, cache_path=cache)
    assert result["radiohead"] == (("art rock", 100),)


@pytest.mark.asyncio
async def test_resolve_artist_tags_normalizes_names_and_skips_blanks(tmp_path):
    client = FakeClient({"Radiohead": (("art rock", 100),)})
    result = await resolve_artist_tags(
        ["  Radiohead  ", "radiohead", "", "   "], client=client, cache_path=None
    )
    assert result == {"radiohead": (("art rock", 100),)}
    assert client.calls == ["Radiohead"]
