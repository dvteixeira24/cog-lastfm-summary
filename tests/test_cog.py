import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("redbot", reason="Red-DiscordBot is required for cog wiring tests")


import discord  # noqa: E402

from lastfmsummary.cog import (  # noqa: E402
    LastFMSummary,
    PostReport,
    SummaryError,
    _clamp_int,
    _clean_username,
    _iso,
    _parse_iso,
    _registration_usernames,
)
from lastfmsummary.lastfm import Entry, LastFMUser, MemberChart  # noqa: E402


class FakeBot:
    pass


@pytest.mark.asyncio
async def test_setup_adds_cog():
    from lastfmsummary import setup

    class LoaderBot:
        def __init__(self):
            self.cog = None

        async def add_cog(self, cog):
            self.cog = cog

    bot = LoaderBot()
    await setup(bot)
    assert isinstance(bot.cog, LastFMSummary)


@pytest.fixture()
def cog():
    return LastFMSummary(FakeBot())


def test_groups_and_member_commands_exist(cog):
    groups = {command.name: command for command in cog.get_commands()}
    assert set(groups) == {"lastfm", "lastfmset"}
    assert set(groups["lastfm"].aliases) == {"lfm"}
    assert {command.name for command in groups["lastfm"].commands} == {
        "register",
        "unregister",
        "profile",
        "members",
        "status",
    }


def test_admin_commands_exist_and_are_protected(cog):
    group = {command.name: command for command in cog.get_commands()}["lastfmset"]
    assert set(group.aliases) == {"lfmset"}
    assert {command.name for command in group.commands} == {
        "channel",
        "period",
        "source",
        "grid",
        "time",
        "timezone",
        "day",
        "enable",
        "disable",
        "preview",
        "status",
        "api_key",
    }
    admin_commands = [command for command in group.commands if command.name != "api_key"]
    for command in admin_commands:
        assert command.checks, f"{command.name} has no checks"
        names = " ".join(getattr(check, "__qualname__", "") for check in command.checks)
        assert "guild_only" in names
        assert command.requires.privilege_level.name == "ADMIN"
    api_key = next(command for command in group.commands if command.name == "api_key")
    assert api_key.requires.privilege_level.name == "BOT_OWNER"


def test_member_commands_are_guild_only(cog):
    group = {command.name: command for command in cog.get_commands()}["lastfm"]
    for command in group.commands:
        names = " ".join(getattr(check, "__qualname__", "") for check in command.checks)
        assert "guild_only" in names


def test_user_data_statement_is_present(cog):
    assert "Last.fm username" in cog.red_end_user_data_statement
    assert "my" in cog.red_end_user_data_statement.lower()


def test_clean_username_accepts_profile_urls_and_handles():
    assert _clean_username("Alice") == "Alice"
    assert _clean_username("@alice") == "alice"
    assert _clean_username("https://www.last.fm/user/Alice_Music") == "Alice_Music"
    assert _clean_username("http://last.fm/user/bob?foo=bar") == "bob"
    assert _clean_username("   ") == ""
    assert len(_clean_username("x" * 100)) == 64


def test_clamp_int():
    assert _clamp_int("4", 3, 2, 8) == 4
    assert _clamp_int(99, 3, 2, 8) == 8
    assert _clamp_int(None, 3, 2, 8) == 3
    assert _clamp_int("nope", 3, 2, 8) == 3


def test_iso_round_trip():
    value = datetime(2026, 10, 21, 12, 30, tzinfo=timezone.utc)
    assert _parse_iso(_iso(value)) == value
    assert _parse_iso(None) is None
    assert _parse_iso("not a date") is None
    assert _parse_iso("2026-10-21T12:30:00") == value


def test_registration_usernames_skips_malformed_rows():
    rows = {
        "1": {"username": "Alice"},
        "2": "Bob",
        "3": {"username": "   "},
        "4": {"nope": True},
    }
    assert _registration_usernames(rows) == {"1": "Alice", "2": "Bob"}


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class FakeChannel(discord.TextChannel):
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(id=1, **kwargs)


def settings(**overrides):
    base = {
        "channel_id": 10,
        "enabled": True,
        "period": "monthly",
        "source": "artists",
        "grid_size": 3,
        "time": "09:00",
        "timezone": "UTC",
        "weekday": 0,
        "day": 1,
        "next_run_at": None,
        "last_run_at": None,
        "last_error": None,
        "registrations": {
            "1": {"username": "alice", "registered_at": "2026-10-01T00:00:00+00:00"},
            "2": {"username": "bob", "registered_at": "2026-10-01T00:00:00+00:00"},
        },
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_post_charts_posts_one_grid_per_member(cog, monkeypatch, tmp_path):
    charts = [
        MemberChart(
            "alice",
            (
                Entry("Radiohead", None, 10, "https://img/rh.jpg"),
                Entry("Portishead", None, 4, None),
            ),
        ),
        MemberChart(
            "bob",
            tuple(Entry(f"Artist {index}", None, index, None) for index in range(1, 13)),
        ),
    ]

    class FakeClient:
        def __init__(self, key):
            assert key == "test-key"

        async def fetch_charts(self, usernames, *, source, period, limit):
            assert usernames == ["alice", "bob"]
            assert source == "artists"
            assert period == "monthly"
            assert limit == 50
            return charts

        async def close(self):
            return None

    async def fake_key():
        return "test-key"

    async def fake_covers(urls, **kwargs):
        return {url: None for url in urls if url}

    captured = []

    def fake_render(**kwargs):
        captured.append(kwargs)
        return PNG

    monkeypatch.setattr("lastfmsummary.cog.LastFMClient", FakeClient)
    monkeypatch.setattr(cog, "_api_key", fake_key)
    monkeypatch.setattr(LastFMSummary, "_cache_dir", property(lambda self: tmp_path))
    monkeypatch.setattr("lastfmsummary.cog.download_covers", fake_covers)
    monkeypatch.setattr("lastfmsummary.cog.render_grid", fake_render)

    channel = FakeChannel()
    report = await cog._post_charts(fake_guild(channel), channel, settings())

    assert report.posted == 2
    assert report.failed == ()
    assert len(channel.sent) == 2
    assert len(captured) == 2

    alice, bob = captured
    assert alice["title"] == "Top Artists — Monthly"
    assert alice["subtitle"] == "Alice • @alice • Test Guild"
    assert alice["footer"] == "14 plays • the last month • Data from Last.fm"
    assert [tile.name for tile in alice["tiles"]] == ["Radiohead", "Portishead"]
    assert [tile.rank for tile in alice["tiles"]] == [1, 2]
    assert bob["subtitle"] == "Bob • @bob • Test Guild"
    assert len(bob["tiles"]) == 9  # only the top 3x3 entries

    first = channel.sent[0]
    assert first["embed"].title == "Alice's Top Artists — Monthly"
    assert first["embed"].image.url == "attachment://lastfm-alice.png"
    assert first["file"].filename == "lastfm-alice.png"
    assert first["allowed_mentions"].everyone is False
    assert channel.sent[1]["embed"].title == "Bob's Top Artists — Monthly"


@pytest.mark.asyncio
async def test_post_charts_uses_album_names_and_artists(cog, monkeypatch, tmp_path):
    charts = [
        MemberChart("alice", (Entry("Kid A", "Radiohead", 7, None),)),
    ]

    class FakeClient:
        def __init__(self, key):
            pass

        async def fetch_charts(self, usernames, **kwargs):
            assert kwargs["source"] == "albums"
            return charts

        async def close(self):
            return None

    async def fake_key():
        return "key"

    async def fake_covers(urls, **kwargs):
        return {}

    captured = {}

    def fake_render(**kwargs):
        captured.update(kwargs)
        return PNG

    monkeypatch.setattr("lastfmsummary.cog.LastFMClient", FakeClient)
    monkeypatch.setattr(cog, "_api_key", fake_key)
    monkeypatch.setattr(LastFMSummary, "_cache_dir", property(lambda self: tmp_path))
    monkeypatch.setattr("lastfmsummary.cog.download_covers", fake_covers)
    monkeypatch.setattr("lastfmsummary.cog.render_grid", fake_render)

    channel = FakeChannel()
    report = await cog._post_charts(fake_guild(channel), channel, settings(source="albums"))

    assert report.posted == 1
    assert captured["title"] == "Top Albums — Monthly"
    assert captured["tiles"][0].name == "Kid A"
    assert captured["tiles"][0].subtitle == "Radiohead"
    assert channel.sent[0]["embed"].title == "Alice's Top Albums — Monthly"


@pytest.mark.asyncio
async def test_post_charts_skips_failed_members_but_posts_others(cog, monkeypatch, tmp_path):
    class FakeClient:
        def __init__(self, key):
            pass

        async def fetch_charts(self, usernames, **kwargs):
            return [MemberChart("alice", (Entry("Radiohead", None, 3, None),))]

        async def close(self):
            return None

    async def fake_key():
        return "key"

    async def fake_covers(urls, **kwargs):
        return {}

    monkeypatch.setattr("lastfmsummary.cog.LastFMClient", FakeClient)
    monkeypatch.setattr(cog, "_api_key", fake_key)
    monkeypatch.setattr(LastFMSummary, "_cache_dir", property(lambda self: tmp_path))
    monkeypatch.setattr("lastfmsummary.cog.download_covers", fake_covers)
    monkeypatch.setattr("lastfmsummary.cog.render_grid", lambda **kwargs: PNG)

    channel = FakeChannel()
    report = await cog._post_charts(fake_guild(channel), channel, settings())

    assert report.posted == 1
    assert report.failed == ("bob",)
    assert len(channel.sent) == 1


@pytest.mark.asyncio
async def test_post_charts_continues_when_a_member_has_no_data(cog, monkeypatch, tmp_path):
    charts = [
        MemberChart("alice", ()),
        MemberChart("bob", (Entry("Radiohead", None, 3, None),)),
    ]

    class FakeClient:
        def __init__(self, key):
            pass

        async def fetch_charts(self, usernames, **kwargs):
            return charts

        async def close(self):
            return None

    async def fake_key():
        return "key"

    async def fake_covers(urls, **kwargs):
        return {}

    monkeypatch.setattr("lastfmsummary.cog.LastFMClient", FakeClient)
    monkeypatch.setattr(cog, "_api_key", fake_key)
    monkeypatch.setattr(LastFMSummary, "_cache_dir", property(lambda self: tmp_path))
    monkeypatch.setattr("lastfmsummary.cog.download_covers", fake_covers)
    monkeypatch.setattr("lastfmsummary.cog.render_grid", lambda **kwargs: PNG)

    channel = FakeChannel()
    report = await cog._post_charts(fake_guild(channel), channel, settings())

    assert report.posted == 1
    assert report.failed == ("alice",)
    assert "no listening data" in report.errors[0]
    assert len(channel.sent) == 1


@pytest.mark.asyncio
async def test_post_charts_requires_api_key(cog, monkeypatch):
    async def fake_key():
        return ""

    monkeypatch.setattr(cog, "_api_key", fake_key)
    with pytest.raises(SummaryError, match="API key"):
        await cog._post_charts(fake_guild(FakeChannel()), FakeChannel(), settings())


@pytest.mark.asyncio
async def test_post_charts_requires_registrations(cog, monkeypatch):
    async def fake_key():
        return "key"

    monkeypatch.setattr(cog, "_api_key", fake_key)
    with pytest.raises(SummaryError, match="No members"):
        await cog._post_charts(fake_guild(FakeChannel()), FakeChannel(), settings(registrations={}))


@pytest.mark.asyncio
async def test_post_charts_handles_all_members_failing(cog, monkeypatch, tmp_path):
    class FakeClient:
        def __init__(self, key):
            pass

        async def fetch_charts(self, usernames, **kwargs):
            return []

        async def close(self):
            return None

    async def fake_key():
        return "key"

    monkeypatch.setattr("lastfmsummary.cog.LastFMClient", FakeClient)
    monkeypatch.setattr(cog, "_api_key", fake_key)
    monkeypatch.setattr(LastFMSummary, "_cache_dir", property(lambda self: tmp_path))
    with pytest.raises(SummaryError, match="Could not fetch any"):
        await cog._post_charts(fake_guild(FakeChannel()), FakeChannel(), settings())


class FakeField:
    def __init__(self, parent, name):
        self.parent = parent
        self.name = name

    async def set(self, value):
        self.parent.data[self.name] = value


class FakeGuildConfig:
    def __init__(self, data):
        self.data = data

    async def all(self):
        return dict(self.data)

    def __getattr__(self, name):
        return FakeField(self, name)


class FakeConfig:
    def __init__(self, data):
        self.data = data

    def guild(self, guild):
        return FakeGuildConfig(self.data)


def fake_guild(channel):
    members = {
        1: SimpleNamespace(display_name="Alice"),
        2: SimpleNamespace(display_name="Bob"),
    }
    return SimpleNamespace(
        id=42,
        name="Test Guild",
        get_channel=lambda channel_id: channel if channel_id == 10 else None,
        get_member=lambda user_id: members.get(user_id),
    )


@pytest.mark.asyncio
async def test_cog_can_be_added_to_red(red):
    cog = LastFMSummary(red)
    await red.add_cog(cog)
    try:
        assert red.get_cog("LastFMSummary") is cog
        assert red.get_command("lastfm") is not None
        assert red.get_command("lastfmset") is not None
    finally:
        await red.remove_cog("LastFMSummary")


@pytest.mark.asyncio
async def test_config_defaults(cog):
    data = await cog.config.guild_from_id(9001).all()
    assert data == {
        "channel_id": None,
        "enabled": False,
        "period": "monthly",
        "source": "artists",
        "grid_size": 3,
        "time": "09:00",
        "timezone": "UTC",
        "weekday": 0,
        "day": 1,
        "next_run_at": None,
        "last_run_at": None,
        "last_error": None,
        "registrations": {},
    }


class FakeCtx:
    def __init__(self, guild, author):
        self.guild = guild
        self.author = author
        self.interaction = None
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))
        return SimpleNamespace(id=1)


@pytest.mark.asyncio
async def test_register_and_unregister_round_trip(cog, monkeypatch):
    guild = SimpleNamespace(id=9002)
    author = SimpleNamespace(id=100, display_name="Alice")
    other = SimpleNamespace(id=200, display_name="Bob")

    class FakeClient:
        def __init__(self, key):
            pass

        async def get_user(self, username):
            return LastFMUser(username="Alice", display_name="Alice")

        async def close(self):
            return None

    async def fake_key():
        return "key"

    monkeypatch.setattr("lastfmsummary.cog.LastFMClient", FakeClient)
    monkeypatch.setattr(cog, "_api_key", fake_key)

    ctx = FakeCtx(guild, author)
    await cog.lastfm_register.callback(cog, ctx, "alice")
    rows = await cog.config.guild_from_id(9002).registrations()
    assert rows["100"]["username"] == "Alice"
    assert _parse_iso(rows["100"]["registered_at"]) is not None
    assert "linked as" in ctx.sent[-1][0]

    # The same Last.fm account cannot be linked by a second member.
    other_ctx = FakeCtx(guild, other)
    await cog.lastfm_register.callback(cog, other_ctx, "Alice")
    assert "already linked" in other_ctx.sent[-1][0]
    assert "200" not in await cog.config.guild_from_id(9002).registrations()

    # The author can update their own link.
    await cog.lastfm_register.callback(cog, ctx, "Alice")
    assert "updated as" in ctx.sent[-1][0]

    await cog.lastfm_unregister.callback(cog, ctx)
    assert await cog.config.guild_from_id(9002).registrations() == {}


@pytest.mark.asyncio
async def test_register_rejects_missing_api_key(cog, monkeypatch):
    async def fake_key():
        return ""

    monkeypatch.setattr(cog, "_api_key", fake_key)
    ctx = FakeCtx(SimpleNamespace(id=9003), SimpleNamespace(id=100, display_name="Alice"))
    await cog.lastfm_register.callback(cog, ctx, "alice")
    assert "No Last.fm API key" in ctx.sent[-1][0]
    assert await cog.config.guild_from_id(9003).registrations() == {}


@pytest.mark.asyncio
async def test_data_export_and_deletion(cog):
    rows = {
        "100": {"username": "alice", "registered_at": "2026-10-01T00:00:00+00:00"},
        "200": {"username": "bob", "registered_at": "2026-10-01T00:00:00+00:00"},
    }
    await cog.config.guild_from_id(9004).registrations.set(rows)

    export = await cog.red_get_data_for_user(user_id=100)
    payload = json.loads(export["lastfmsummary-user-data.json"].read().decode("utf-8"))
    assert payload["lastfm_summary"] == [
        {
            "guild_id": 9004,
            "username": "alice",
            "registered_at": "2026-10-01T00:00:00+00:00",
        }
    ]

    await cog.red_delete_data_for_user(requester="user", user_id=100)
    remaining = await cog.config.guild_from_id(9004).registrations()
    assert remaining == {"200": rows["200"]}


@pytest.mark.asyncio
async def test_preview_can_target_one_member(cog, monkeypatch):
    guild = SimpleNamespace(id=9005)
    author = SimpleNamespace(id=100, display_name="Alice")
    member = SimpleNamespace(id=200, display_name="Bob")
    ctx = FakeCtx(guild, author)
    ctx.channel = FakeChannel()
    await cog.config.guild_from_id(9005).registrations.set(
        {
            "100": {"username": "alice", "registered_at": "2026-10-01T00:00:00+00:00"},
            "200": {"username": "bob", "registered_at": "2026-10-01T00:00:00+00:00"},
        }
    )
    captured = {}

    async def fake_post(guild_, channel_, settings_):
        captured.update(settings_["registrations"])
        return PostReport(posted=1)

    monkeypatch.setattr(cog, "_post_charts", fake_post)
    await cog.lastfmset_preview.callback(cog, ctx, member)

    assert set(captured) == {"200"}
    assert "Posted 1 chart." in ctx.sent[-1][0]


@pytest.mark.asyncio
async def test_guild_settings_sanitizes_corrupt_values(cog):
    data = settings(
        period="daily",
        source="tracks",
        grid_size=99,
        time="25:99",
        timezone="Mars/Olympus_Mons",
        weekday=99,
        day=99,
        registrations="not-a-mapping",
    )
    cog.config = FakeConfig(data)
    result = await cog._guild_settings(SimpleNamespace())
    assert result["period"] == "monthly"
    assert result["source"] == "artists"
    assert result["grid_size"] == 8
    assert result["time"] == "09:00"
    assert result["timezone"] == "UTC"
    assert result["weekday"] == 6
    assert result["day"] == 31
    assert result["registrations"] == {}


@pytest.mark.asyncio
async def test_tick_posts_due_summary_and_reschedules(cog, monkeypatch):
    now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    data = settings(next_run_at=(now - timedelta(minutes=5)).isoformat())
    cog.config = FakeConfig(data)
    calls = []

    async def fake_post(guild, channel, settings_):
        calls.append((guild.id, channel, settings_["period"]))
        return PostReport(posted=2)

    monkeypatch.setattr(cog, "_post_charts", fake_post)
    channel = FakeChannel()
    await cog._tick_guild(fake_guild(channel), now)

    assert calls == [(42, channel, "monthly")]
    assert data["last_error"] is None
    assert _parse_iso(data["last_run_at"]) == now
    assert _parse_iso(data["next_run_at"]) > now


@pytest.mark.asyncio
async def test_tick_skips_runs_more_than_six_hours_late(cog, monkeypatch):
    now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    data = settings(next_run_at=(now - timedelta(hours=7)).isoformat())
    cog.config = FakeConfig(data)
    calls = []

    async def fake_post(*args):
        calls.append(args)
        return PostReport(posted=1)

    monkeypatch.setattr(cog, "_post_charts", fake_post)
    await cog._tick_guild(fake_guild(FakeChannel()), now)

    assert calls == []
    assert _parse_iso(data["next_run_at"]) > now
    assert "last_run_at" not in data or data["last_run_at"] is None


@pytest.mark.asyncio
async def test_tick_does_nothing_when_disabled_or_unconfigured(cog, monkeypatch):
    now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(cog, "_post_charts", pytest.fail)

    for overrides in (
        {"enabled": False},
        {"channel_id": None},
        {"registrations": {}},
        {"next_run_at": (now + timedelta(days=1)).isoformat()},
    ):
        data = settings(**overrides)
        cog.config = FakeConfig(data)
        await cog._tick_guild(fake_guild(FakeChannel()), now)


@pytest.mark.asyncio
async def test_tick_records_channel_error_and_reschedules(cog, monkeypatch):
    now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    data = settings(next_run_at=(now - timedelta(minutes=1)).isoformat())
    cog.config = FakeConfig(data)
    monkeypatch.setattr(cog, "_post_charts", pytest.fail)

    await cog._tick_guild(fake_guild(None), now)
    assert "no longer exists" in data["last_error"]
    assert _parse_iso(data["next_run_at"]) > now


@pytest.mark.asyncio
async def test_tick_records_partial_failures(cog, monkeypatch):
    now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    data = settings(next_run_at=(now - timedelta(minutes=1)).isoformat())
    cog.config = FakeConfig(data)

    async def partial_post(*args):
        return PostReport(posted=1, failed=("bob",))

    monkeypatch.setattr(cog, "_post_charts", partial_post)
    await cog._tick_guild(fake_guild(FakeChannel()), now)
    assert "Posted 1 chart(s)" in data["last_error"]
    assert "bob" in data["last_error"]
    assert _parse_iso(data["next_run_at"]) > now


@pytest.mark.asyncio
async def test_tick_records_summary_errors_and_reschedules(cog, monkeypatch):
    now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    data = settings(next_run_at=(now - timedelta(minutes=1)).isoformat())
    cog.config = FakeConfig(data)

    async def failing_post(*args):
        raise SummaryError("Last.fm is down")

    monkeypatch.setattr(cog, "_post_charts", failing_post)
    await cog._tick_guild(fake_guild(FakeChannel()), now)
    assert data["last_error"] == "Last.fm is down"
    assert _parse_iso(data["next_run_at"]) > now
