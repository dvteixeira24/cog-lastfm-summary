"""Red-DiscordBot cog: periodic Last.fm listening summaries."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import quote

import discord
from discord.ext import tasks
from redbot.core import Config, commands, data_manager
from redbot.core.bot import Red

from .deezer import (
    album_key,
    artist_key,
    resolve_album_covers,
    resolve_artist_images,
)
from .lastfm import (
    LastFMClient,
    LastFMError,
    LastFMUnavailable,
    MemberChart,
    is_placeholder,
)
from .render import Tile, download_covers, render_grid
from .schedule import (
    PERIOD_LABELS,
    compute_next_run,
    format_hhmm,
    normalize_period,
    parse_hhmm,
    period_window,
    validate_timezone,
)
from .serp import album_query, artist_query, resolve_serp_images

log = logging.getLogger("red.lastfmsummary")

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MAX_LATE = timedelta(hours=6)
DEFAULT_GRID = 3
DEFAULT_TIME = "09:00"
DEFAULT_TIMEZONE = "UTC"
DEFAULT_PERIOD = "monthly"
DEFAULT_SOURCE = "artists"
_USERNAME_URL_RE = re.compile(r"(?:https?://)?(?:www\.)?last\.fm/user/([^/?#\s]+)", re.I)


class SummaryError(Exception):
    """A user-facing failure while building or posting a chart."""


@dataclass(frozen=True)
class PostReport:
    """Outcome of posting the charts for one guild run."""

    posted: int
    failed: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


def _safe_filename(username: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", username).strip("-")
    return f"lastfm-{slug or 'summary'}.png"[:100]


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _clean_username(value: str) -> str:
    candidate = str(value or "").strip()
    match = _USERNAME_URL_RE.search(candidate)
    if match is not None:
        candidate = match.group(1)
    candidate = candidate.lstrip("@").strip()
    return candidate[:64]


def _format_source(source: str) -> str:
    return "Top Albums" if source == "albums" else "Top Artists"


def _registration_usernames(registrations: Mapping[str, Any]) -> dict[str, str]:
    """Map Discord user ID -> Last.fm username, skipping malformed rows."""
    clean: dict[str, str] = {}
    for user_id, row in registrations.items():
        if isinstance(row, Mapping):
            username = _clean_username(row.get("username", ""))
        else:
            username = _clean_username(str(row))
        if username:
            clean[str(user_id)] = username
    return clean


class LastFMSummary(commands.Cog):
    """Periodic Last.fm top artists or albums charts for every linked member."""

    __author__ = "OpenCode"
    __version__ = "1.2.0"

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=728394615, force_registration=True)
        self.config.register_global(api_key="", serp_api_key="")
        self.config.register_guild(
            channel_id=None,
            enabled=False,
            period=DEFAULT_PERIOD,
            source=DEFAULT_SOURCE,
            grid_size=DEFAULT_GRID,
            time=DEFAULT_TIME,
            timezone=DEFAULT_TIMEZONE,
            weekday=0,
            day=1,
            next_run_at=None,
            last_run_at=None,
            last_error=None,
            registrations={},
        )
        self._summary_locks: dict[int, asyncio.Lock] = {}

    @property
    def _cache_dir(self):
        return data_manager.cog_data_path(self) / "covers"

    red_end_user_data_statement = (
        "This cog stores, per Discord server, a member's Discord user ID, the "
        "Last.fm username they registered, and the registration timestamp. The "
        "data is used only to build server listening summaries. Members can "
        "remove their entry with the lastfm unregister command, and server data "
        "can be removed with Red's mydata commands."
    )

    async def cog_load(self) -> None:
        self._scheduler.start()

    def cog_unload(self) -> None:
        self._scheduler.cancel()

    # ------------------------------------------------------------------ config

    async def _guild_settings(self, guild: discord.Guild) -> dict[str, Any]:
        raw = await self.config.guild(guild).all()
        try:
            period = normalize_period(raw.get("period", DEFAULT_PERIOD))
        except ValueError:
            period = DEFAULT_PERIOD
        source = (
            "albums" if str(raw.get("source", DEFAULT_SOURCE)).lower() == "albums" else "artists"
        )
        try:
            time_value = format_hhmm(*parse_hhmm(raw.get("time", DEFAULT_TIME)))
        except ValueError:
            time_value = DEFAULT_TIME
        try:
            tz_name = validate_timezone(raw.get("timezone", DEFAULT_TIMEZONE))
        except ValueError:
            tz_name = DEFAULT_TIMEZONE
        registrations = raw.get("registrations")
        if not isinstance(registrations, Mapping):
            registrations = {}
        return {
            "channel_id": _clamp_int(raw.get("channel_id"), 0, 0, 2**63 - 1) or None,
            "enabled": bool(raw.get("enabled", False)),
            "period": period,
            "source": source,
            "grid_size": _clamp_int(raw.get("grid_size"), DEFAULT_GRID, 2, 8),
            "time": time_value,
            "timezone": tz_name,
            "weekday": _clamp_int(raw.get("weekday"), 0, 0, 6),
            "day": _clamp_int(raw.get("day"), 1, 1, 31),
            "next_run_at": _parse_iso(raw.get("next_run_at")),
            "last_run_at": _parse_iso(raw.get("last_run_at")),
            "last_error": str(raw.get("last_error") or "") or None,
            "registrations": dict(registrations),
        }

    async def _api_key(self) -> str:
        stored = str(await self.config.api_key() or "").strip()
        return stored or os.environ.get("LASTFM_API_KEY", "").strip()

    async def _serp_api_key(self) -> str:
        stored = str(await self.config.serp_api_key() or "").strip()
        return stored or os.environ.get("SERPAPI_API_KEY", "").strip()

    async def _store_next_run(
        self,
        guild: discord.Guild,
        settings: Mapping[str, Any] | None = None,
        *,
        from_now: datetime | None = None,
    ) -> datetime:
        settings = settings or await self._guild_settings(guild)
        now = from_now or datetime.now(timezone.utc)
        next_run = compute_next_run(
            settings["period"],
            now_utc=now,
            tz_name=settings["timezone"],
            at=settings["time"],
            weekday=settings["weekday"],
            day=settings["day"],
        )
        await self.config.guild(guild).next_run_at.set(_iso(next_run))
        return next_run

    def _lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._summary_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._summary_locks[guild_id] = lock
        return lock

    async def _respond(self, ctx: commands.Context, content: str | None = None, **kwargs: Any):
        kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
        if ctx.interaction is not None:
            try:
                return await ctx.send(content, ephemeral=True, **kwargs)
            except TypeError:
                pass
        return await ctx.send(content, **kwargs)

    async def _require_api_key(self) -> str:
        key = await self._api_key()
        if not key:
            raise SummaryError(
                "No Last.fm API key is configured. Ask the bot owner to run "
                "`lastfmset api_key`, or set the `LASTFM_API_KEY` environment variable."
            )
        return key

    # --------------------------------------------------------------- commands

    @commands.hybrid_group(
        name="lastfm",
        aliases=["lfm"],
        invoke_without_command=True,
    )
    @commands.guild_only()
    async def lastfm(self, ctx: commands.Context) -> None:
        """Link your Last.fm account and view the server's summary settings."""
        settings = await self._guild_settings(ctx.guild)
        registrations = _registration_usernames(settings["registrations"])
        registered = registrations.get(str(ctx.author.id))
        embed = discord.Embed(
            title="Last.fm Summary",
            color=discord.Color(0x48C78E),
            description=(
                f"**Your account:** {discord.utils.escape_markdown(registered) if registered else 'not linked'}\n"
                f"**Schedule:** {PERIOD_LABELS[settings['period']]} at {settings['time']} "
                f"({settings['timezone']})\n"
                f"**Grid:** {settings['grid_size']}×{settings['grid_size']} "
                f"of {settings['source']}"
            ),
        )
        embed.set_footer(text="lastfm register <username> • lastfm status")
        await self._respond(ctx, embed=embed)

    @lastfm.command(name="register", aliases=["link", "login"])
    @commands.guild_only()
    @commands.cooldown(3, 60, commands.BucketType.user)
    async def lastfm_register(self, ctx: commands.Context, username: str) -> None:
        """Link your Last.fm username to this server."""
        cleaned = _clean_username(username)
        if not cleaned:
            await self._respond(ctx, "Please provide your Last.fm username.")
            return
        try:
            key = await self._require_api_key()
        except SummaryError as exc:
            await self._respond(ctx, str(exc))
            return

        client = LastFMClient(key)
        try:
            user = await client.get_user(cleaned)
        except LastFMError as exc:
            if exc.code in {6, 7}:
                message = f"Last.fm has no user named `{discord.utils.escape_markdown(cleaned)}`."
            elif exc.code in {10, 26}:
                message = "The configured Last.fm API key is invalid. Ask the bot owner to fix it."
            else:
                message = f"Last.fm rejected the request: {exc.message}"
            await self._respond(ctx, message)
            return
        except LastFMUnavailable as exc:
            await self._respond(ctx, f"Could not reach Last.fm right now: {exc}")
            return
        finally:
            await client.close()

        settings = await self._guild_settings(ctx.guild)
        registrations = _registration_usernames(settings["registrations"])
        for user_id, linked in registrations.items():
            if user_id != str(ctx.author.id) and linked.casefold() == user.username.casefold():
                await self._respond(
                    ctx,
                    f"The Last.fm user `{discord.utils.escape_markdown(user.username)}` "
                    "is already linked by another member in this server.",
                )
                return

        user_id = str(ctx.author.id)
        updated = user_id in settings["registrations"]
        rows = dict(settings["registrations"])
        rows[user_id] = {
            "username": user.username,
            "registered_at": _iso(datetime.now(timezone.utc)),
        }
        await self.config.guild(ctx.guild).registrations.set(rows)
        verb = "updated" if updated else "linked"
        await self._respond(
            ctx,
            f"Your Last.fm account is {verb} as "
            f"**[{discord.utils.escape_markdown(user.username)}]"
            f"(https://www.last.fm/user/{quote(user.username)})**.",
        )

    @lastfm.command(name="unregister", aliases=["unlink", "logout"])
    @commands.guild_only()
    async def lastfm_unregister(self, ctx: commands.Context) -> None:
        """Remove your Last.fm username from this server."""
        settings = await self._guild_settings(ctx.guild)
        rows = dict(settings["registrations"])
        user_id = str(ctx.author.id)
        if user_id not in rows:
            await self._respond(ctx, "You do not have a Last.fm account linked in this server.")
            return
        rows.pop(user_id, None)
        await self.config.guild(ctx.guild).registrations.set(rows)
        await self._respond(ctx, "Your Last.fm account has been unlinked from this server.")

    @lastfm.command(name="profile", aliases=["whois"])
    @commands.guild_only()
    async def lastfm_profile(
        self, ctx: commands.Context, member: Optional[discord.Member] = None
    ) -> None:
        """Show a member's linked Last.fm account."""
        target = member or ctx.author
        target_name = discord.utils.escape_markdown(target.display_name)
        settings = await self._guild_settings(ctx.guild)
        registrations = _registration_usernames(settings["registrations"])
        username = registrations.get(str(target.id))
        if not username:
            await self._respond(
                ctx,
                f"{target_name} has not linked a Last.fm account in this server.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        row = settings["registrations"].get(str(target.id), {})
        registered_at = _parse_iso(row.get("registered_at")) if isinstance(row, Mapping) else None
        embed = discord.Embed(
            title=f"{target_name} on Last.fm",
            url=f"https://www.last.fm/user/{quote(username)}",
            color=discord.Color(0xD63450),
        )
        embed.add_field(
            name="Username",
            value=f"[{discord.utils.escape_markdown(username)}]"
            f"(https://www.last.fm/user/{quote(username)})",
        )
        if registered_at is not None:
            embed.add_field(name="Linked", value=discord.utils.format_dt(registered_at, "R"))
        await self._respond(ctx, embed=embed)

    @lastfm.command(name="members")
    @commands.guild_only()
    async def lastfm_members(self, ctx: commands.Context) -> None:
        """List members who linked a Last.fm account."""
        settings = await self._guild_settings(ctx.guild)
        registrations = _registration_usernames(settings["registrations"])
        if not registrations:
            await self._respond(ctx, "No members have linked a Last.fm account yet.")
            return
        lines = []
        for user_id, username in list(registrations.items())[:25]:
            display = username if len(username) <= 32 else username[:31] + "…"
            lines.append(
                f"• <@{user_id}> — [{discord.utils.escape_markdown(display)}]"
                f"(https://www.last.fm/user/{quote(username)})"
            )
        hidden = len(registrations) - len(lines)
        if hidden > 0:
            lines.append(f"…and {hidden} more.")
        embed = discord.Embed(
            title=f"Linked members ({len(registrations)})",
            description="\n".join(lines),
            color=discord.Color(0x48C78E),
        )
        await self._respond(ctx, embed=embed)

    @lastfm.command(name="status")
    @commands.guild_only()
    async def lastfm_status(self, ctx: commands.Context) -> None:
        """Show this server's Last.fm summary settings."""
        await self._send_status(ctx, admin=False)

    # ------------------------------------------------------------------ admin

    @commands.hybrid_group(
        name="lastfmset",
        aliases=["lfmset"],
        invoke_without_command=True,
    )
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def lastfmset(self, ctx: commands.Context) -> None:
        """Configure automatic Last.fm summaries (server admins)."""
        embed = discord.Embed(
            title="Last.fm summary settings",
            color=discord.Color(0x48C78E),
            description=(
                "`lastfmset channel [#channel|none]`\n"
                "`lastfmset period weekly|monthly|quarterly|half-yearly|yearly`\n"
                "`lastfmset source artists|albums`\n"
                "`lastfmset grid 2-8`\n"
                "`lastfmset time HH:MM`\n"
                "`lastfmset timezone Europe/Berlin`\n"
                "`lastfmset day monday|1-31`\n"
                "`lastfmset enable|disable`\n"
                "`lastfmset preview [member]`\n"
                "`lastfmset status`\n"
                "`lastfmset api_key`\n"
                "`lastfmset serp_api_key`"
            ),
        )
        await self._respond(ctx, embed=embed)

    async def _send_status(self, ctx: commands.Context, *, admin: bool) -> None:
        settings = await self._guild_settings(ctx.guild)
        channel = ctx.guild.get_channel(settings["channel_id"] or 0)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            channel = None
        registrations = _registration_usernames(settings["registrations"])
        anchor = (
            f"Every {WEEKDAYS[settings['weekday']]}"
            if settings["period"] == "weekly"
            else f"Day {settings['day']} of the month"
        )
        next_run = settings["next_run_at"]
        next_text = discord.utils.format_dt(next_run, "F") if next_run else "not scheduled"
        embed = discord.Embed(title="Last.fm summary", color=discord.Color(0x48C78E))
        embed.add_field(name="Status", value="enabled" if settings["enabled"] else "disabled")
        embed.add_field(name="Channel", value=channel.mention if channel else "not set")
        embed.add_field(name="Period", value=PERIOD_LABELS[settings["period"]])
        embed.add_field(name="Source", value=settings["source"].title())
        embed.add_field(name="Grid", value=f"{settings['grid_size']}×{settings['grid_size']}")
        embed.add_field(
            name="Schedule", value=f"{anchor} at {settings['time']} ({settings['timezone']})"
        )
        embed.add_field(name="Next run", value=next_text)
        embed.add_field(name="Linked members", value=str(len(registrations)))
        key = await self._api_key()
        embed.add_field(name="API key", value="configured" if key else "missing")
        serp_key = await self._serp_api_key()
        embed.add_field(name="SerpApi key", value="configured" if serp_key else "missing")
        if admin:
            if settings["last_run_at"]:
                embed.add_field(
                    name="Last run",
                    value=discord.utils.format_dt(settings["last_run_at"], "R"),
                )
            if settings["last_error"]:
                embed.add_field(
                    name="Last error", value=settings["last_error"][:1024], inline=False
                )
        await self._respond(ctx, embed=embed)

    @lastfmset.command(name="channel")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def lastfmset_channel(self, ctx: commands.Context, channel: Optional[str] = None) -> None:
        """Set the summary channel, or pass `none` to clear it."""
        if channel is None:
            target = ctx.channel
        elif channel.strip().lower() in {"none", "off", "clear"}:
            await self.config.guild(ctx.guild).channel_id.set(None)
            await self.config.guild(ctx.guild).enabled.set(False)
            await self.config.guild(ctx.guild).next_run_at.set(None)
            await self._respond(ctx, "Summary channel cleared and automatic posting disabled.")
            return
        else:
            match = re.fullmatch(r"<#(\d+)>", channel.strip()) or re.fullmatch(
                r"(\d+)", channel.strip()
            )
            target = None
            if match:
                target = ctx.guild.get_channel(int(match.group(1)))
            if not isinstance(target, (discord.TextChannel, discord.Thread)):
                await self._respond(ctx, "Please mention a text channel in this server.")
                return

        permissions = target.permissions_for(ctx.guild.me)
        if (
            not permissions.send_messages
            or not permissions.attach_files
            or not permissions.embed_links
        ):
            await self._respond(
                ctx,
                f"I need Send Messages, Attach Files and Embed Links in {target.mention}.",
            )
            return
        await self.config.guild(ctx.guild).channel_id.set(target.id)
        await self._respond(ctx, f"Summary channel set to {target.mention}.")

    @lastfmset.command(name="period")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def lastfmset_period(self, ctx: commands.Context, period: str) -> None:
        """Set the cadence and chart window."""
        try:
            normalized = normalize_period(period)
        except ValueError as exc:
            await self._respond(ctx, str(exc))
            return
        await self.config.guild(ctx.guild).period.set(normalized)
        settings = await self._guild_settings(ctx.guild)
        next_run = await self._store_next_run(ctx.guild, settings)
        await self._respond(
            ctx,
            f"Period set to **{PERIOD_LABELS[normalized]}** "
            f"({period_window(normalized)}). Next run: {discord.utils.format_dt(next_run, 'F')}.",
        )

    @lastfmset.command(name="source")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def lastfmset_source(self, ctx: commands.Context, source: str) -> None:
        """Set the grid source: artists or albums."""
        normalized = source.strip().lower()
        if normalized in {"album", "albums"}:
            value = "albums"
        elif normalized in {"artist", "artists"}:
            value = "artists"
        else:
            await self._respond(ctx, "Source must be `artists` or `albums`.")
            return
        await self.config.guild(ctx.guild).source.set(value)
        await self._respond(ctx, f"Summary source set to **{value}**.")

    @lastfmset.command(name="grid")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def lastfmset_grid(self, ctx: commands.Context, size: int) -> None:
        """Set the square grid size (2 to 8)."""
        if not 2 <= size <= 8:
            await self._respond(ctx, "Grid size must be between 2 and 8.")
            return
        await self.config.guild(ctx.guild).grid_size.set(size)
        await self._respond(ctx, f"Grid size set to **{size}×{size}** ({size * size} entries).")

    @lastfmset.command(name="time")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def lastfmset_time(self, ctx: commands.Context, value: str) -> None:
        """Set the local posting time in 24-hour HH:MM."""
        try:
            hour, minute = parse_hhmm(value)
        except ValueError as exc:
            await self._respond(ctx, str(exc))
            return
        formatted = format_hhmm(hour, minute)
        await self.config.guild(ctx.guild).time.set(formatted)
        settings = await self._guild_settings(ctx.guild)
        next_run = await self._store_next_run(ctx.guild, settings)
        await self._respond(
            ctx,
            f"Posting time set to **{formatted}**. Next run: "
            f"{discord.utils.format_dt(next_run, 'F')}.",
        )

    @lastfmset.command(name="timezone")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def lastfmset_timezone(self, ctx: commands.Context, timezone_name: str) -> None:
        """Set the IANA timezone used for scheduling."""
        try:
            tz_name = validate_timezone(timezone_name)
        except ValueError as exc:
            await self._respond(ctx, str(exc))
            return
        await self.config.guild(ctx.guild).timezone.set(tz_name)
        settings = await self._guild_settings(ctx.guild)
        next_run = await self._store_next_run(ctx.guild, settings)
        await self._respond(
            ctx,
            f"Timezone set to **{tz_name}**. Next run: {discord.utils.format_dt(next_run, 'F')}.",
        )

    @lastfmset.command(name="day")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def lastfmset_day(self, ctx: commands.Context, value: str) -> None:
        """Set the anchor: weekday name for weekly, day 1-31 otherwise."""
        settings = await self._guild_settings(ctx.guild)
        text = value.strip().lower()
        if settings["period"] == "weekly":
            if text.isdigit():
                weekday = int(text)
                if not 0 <= weekday <= 6:
                    await self._respond(ctx, "Weekday must be 0 (Monday) to 6 (Sunday).")
                    return
            else:
                matches = [index for index, name in enumerate(WEEKDAYS) if name.lower() == text]
                if not matches:
                    await self._respond(ctx, f"Choose one of: {', '.join(WEEKDAYS)}.")
                    return
                weekday = matches[0]
            await self.config.guild(ctx.guild).weekday.set(weekday)
            anchor_text = f"Every {WEEKDAYS[weekday]}"
        else:
            if not text.isdigit() or not 1 <= int(text) <= 31:
                await self._respond(ctx, "Day of month must be between 1 and 31.")
                return
            day = int(text)
            await self.config.guild(ctx.guild).day.set(day)
            anchor_text = f"Day {day} of the month"
        settings = await self._guild_settings(ctx.guild)
        next_run = await self._store_next_run(ctx.guild, settings)
        await self._respond(
            ctx,
            f"Anchor set to **{anchor_text}**. Next run: {discord.utils.format_dt(next_run, 'F')}.",
        )

    @lastfmset.command(name="enable")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def lastfmset_enable(self, ctx: commands.Context) -> None:
        """Enable automatic member chart posts."""
        settings = await self._guild_settings(ctx.guild)
        channel = ctx.guild.get_channel(settings["channel_id"] or 0)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await self._respond(ctx, "Set a summary channel first with `lastfmset channel`.")
            return
        if not settings["registrations"]:
            await self._respond(
                ctx,
                "No members have linked a Last.fm account yet. Posts will be skipped "
                "until someone registers.",
            )
        await self.config.guild(ctx.guild).enabled.set(True)
        await self.config.guild(ctx.guild).last_error.set(None)
        next_run = await self._store_next_run(ctx.guild, settings)
        await self._respond(
            ctx,
            f"Automatic member charts enabled in {channel.mention}. Next run: "
            f"{discord.utils.format_dt(next_run, 'F')}.",
        )

    @lastfmset.command(name="disable")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def lastfmset_disable(self, ctx: commands.Context) -> None:
        """Disable automatic member chart posts."""
        await self.config.guild(ctx.guild).enabled.set(False)
        await self.config.guild(ctx.guild).next_run_at.set(None)
        await self._respond(ctx, "Automatic member charts disabled.")

    @lastfmset.command(name="preview")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    @commands.cooldown(2, 60, commands.BucketType.guild)
    async def lastfmset_preview(
        self, ctx: commands.Context, member: Optional[discord.Member] = None
    ) -> None:
        """Post charts now: for one member, or for every linked member."""
        channel = ctx.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await self._respond(ctx, "I cannot post images in this channel.")
            return
        settings = await self._guild_settings(ctx.guild)
        if member is not None:
            row = settings["registrations"].get(str(member.id))
            if not row:
                await self._respond(ctx, f"{member.display_name} has not linked a Last.fm account.")
                return
            settings = {**settings, "registrations": {str(member.id): row}}
        lock = self._lock(ctx.guild.id)
        if lock.locked():
            await self._respond(ctx, "Charts are already being generated for this server.")
            return
        async with lock:
            try:
                report = await self._post_charts(ctx.guild, channel, settings)
            except SummaryError as exc:
                await self._respond(ctx, str(exc))
            except discord.Forbidden:
                await self._respond(ctx, f"I do not have permission to post in {channel.mention}.")
            except discord.HTTPException as exc:
                await self._respond(ctx, f"Discord rejected the upload: {exc}")
            else:
                text = f"Posted {report.posted} chart{'s' if report.posted != 1 else ''}."
                if report.failed:
                    preview = ", ".join(report.failed[:10])
                    more = "…" if len(report.failed) > 10 else ""
                    text += f" Could not fetch or post: {preview}{more}"
                await self._respond(ctx, text)

    @lastfmset.command(name="status")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def lastfmset_status(self, ctx: commands.Context) -> None:
        """Show full summary configuration and the last error."""
        await self._send_status(ctx, admin=True)

    @lastfmset.command(name="api_key")
    @commands.is_owner()
    async def lastfmset_api_key(self, ctx: commands.Context, key: str) -> None:
        """Set the bot-wide Last.fm API key (bot owner only)."""
        candidate = key.strip()
        if len(candidate) < 8:
            await self._respond(ctx, "That does not look like a Last.fm API key.")
            return
        warning = ""
        client = LastFMClient(candidate)
        try:
            await client.get_user("RJ")
        except LastFMError as exc:
            if exc.code in {10, 26}:
                await self._respond(ctx, "Last.fm rejected that API key.")
                return
            warning = f" (validation returned: {exc.message})"
        except LastFMUnavailable as exc:
            warning = f" (could not validate right now: {exc})"
        finally:
            await client.close()
        await self.config.api_key.set(candidate)
        await self._respond(
            ctx,
            f"Last.fm API key saved (…{candidate[-4:]}){warning}. "
            "Try `lastfmset preview` to test it.",
        )

    @lastfmset.command(name="serp_api_key")
    @commands.is_owner()
    async def lastfmset_serp_api_key(self, ctx: commands.Context, key: str) -> None:
        """Set the bot-wide SerpApi key used for Google Images (bot owner only)."""
        candidate = key.strip()
        if len(candidate) < 8:
            await self._respond(ctx, "That does not look like a SerpApi key.")
            return
        await self.config.serp_api_key.set(candidate)
        await self._respond(
            ctx,
            f"SerpApi key saved (…{candidate[-4:]}). "
            "Artist and album art will be tried via SerpApi before Deezer. "
            "Try `lastfmset preview @member` to test it.",
        )

    # ---------------------------------------------------------------- posting

    @staticmethod
    def _display_name(guild: discord.Guild, user_id: str, fallback: str) -> str:
        getter = getattr(guild, "get_member", None)
        if callable(getter):
            try:
                member = getter(int(user_id))
            except (TypeError, ValueError):
                member = None
            if member is not None:
                name = getattr(member, "display_name", None)
                if name:
                    return str(name)
        return fallback

    async def _resolve_fallbacks(
        self, charts: Sequence[MemberChart], source: str, grid_size: int
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Resolve SERP then Deezer images for entries missing Last.fm art."""
        missing: dict[str, tuple[str, str, str]] = {}
        for chart in charts:
            for entry in chart.entries[: grid_size * grid_size]:
                if not is_placeholder(entry.image_url):
                    continue
                if source == "albums":
                    artist = entry.artist or ""
                    key = album_key(artist, entry.name)
                    query = album_query(artist, entry.name)
                    missing.setdefault(key, (entry.name, artist, query))
                else:
                    key = artist_key(entry.name)
                    missing.setdefault(key, (entry.name, "", artist_query(entry.name)))

        if not missing:
            return {}, {}

        serp_key = await self._serp_api_key()
        serp_images: dict[str, str] = {}
        if serp_key:
            try:
                resolved = await resolve_serp_images(
                    {key: value[2] for key, value in missing.items()},
                    api_key=serp_key,
                    cache_path=self._cache_dir / "serp-images.json",
                )
                serp_images = {key: url for key, url in resolved.items() if url}
            except Exception:
                log.exception("SerpApi image lookup failed")

        remaining = {key: value for key, value in missing.items() if key not in serp_images}
        deezer_images: dict[str, str] = {}
        if remaining:
            try:
                if source == "albums":
                    resolved = await resolve_album_covers(
                        [(value[1], value[0]) for value in remaining.values()],
                        cache_path=self._cache_dir / "deezer-albums.json",
                    )
                else:
                    resolved = await resolve_artist_images(
                        [value[0] for value in remaining.values()],
                        cache_path=self._cache_dir / "deezer-artists.json",
                    )
                deezer_images = {key: url for key, url in resolved.items() if url}
            except Exception:
                log.exception("Deezer image lookup failed")

        return serp_images, deezer_images

    async def _post_chart(
        self,
        guild: discord.Guild,
        channel: discord.abc.Messageable,
        chart: MemberChart,
        *,
        display_name: str,
        grid_size: int,
        source: str,
        period: str,
        serp_images: Mapping[str, str | None] | None = None,
        deezer_images: Mapping[str, str | None] | None = None,
    ) -> discord.Message:
        """Render and post a single member's chart."""
        entries = chart.entries[: grid_size * grid_size]
        if not entries:
            raise SummaryError(f"{display_name} has no listening data for this period.")

        tiles: list[Tile] = []
        providers: set[str] = set()
        for rank, entry in enumerate(entries, start=1):
            image_url = entry.image_url
            if is_placeholder(image_url):
                if source == "albums":
                    fallback_key = album_key(entry.artist or "", entry.name)
                else:
                    fallback_key = artist_key(entry.name)
                fallback = (serp_images or {}).get(fallback_key)
                if fallback:
                    image_url = fallback
                    providers.add("SerpApi")
                else:
                    fallback = (deezer_images or {}).get(fallback_key)
                    if fallback:
                        image_url = fallback
                        providers.add("Deezer")
            tiles.append(
                Tile(
                    name=entry.name,
                    subtitle=entry.artist if source == "albums" else None,
                    image_url=image_url,
                    rank=rank,
                )
            )
        covers = await download_covers(
            [tile.image_url for tile in tiles],
            cache_dir=self._cache_dir,
        )
        total_plays = sum(max(0, entry.plays) for entry in chart.entries)
        safe_display = discord.utils.escape_markdown(display_name)
        title = f"{_format_source(source)} — {PERIOD_LABELS[period]}"
        subtitle_parts = [display_name, f"@{chart.username}", guild.name]
        subtitle = " • ".join(part for part in subtitle_parts if part)
        provider_list = [name for name in ("SerpApi", "Deezer") if name in providers]
        credit = (
            "Data: Last.fm & " + " & ".join(provider_list) if provider_list else "Data from Last.fm"
        )
        footer = f"{total_plays:,} plays • {period_window(period)} • {credit}"
        image_bytes = await asyncio.to_thread(
            render_grid,
            tiles=tiles,
            grid_size=grid_size,
            title=title,
            subtitle=subtitle,
            footer=footer,
            covers=covers,
        )

        filename = _safe_filename(chart.username)
        profile_url = f"https://www.last.fm/user/{quote(chart.username)}"
        embed = discord.Embed(
            title=f"{safe_display}'s {_format_source(source)} — {PERIOD_LABELS[period]}",
            description=(
                f"{_format_source(source)} for **{safe_display}** "
                f"({period_window(period)}).\n[Last.fm profile]({profile_url})"
            ),
            color=discord.Color(0x48C78E),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_image(url=f"attachment://{filename}")
        embed_footer = f"{total_plays:,} plays • {period_window(period)}"
        if provider_list:
            embed_footer += " • Images via " + ", ".join(provider_list)
        embed.set_footer(text=embed_footer)
        return await channel.send(
            embed=embed,
            file=discord.File(io.BytesIO(image_bytes), filename=filename),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _post_charts(
        self,
        guild: discord.Guild,
        channel: discord.abc.Messageable,
        settings: Mapping[str, Any] | None = None,
    ) -> PostReport:
        """Fetch and post one chart per linked member, continuing past failures."""
        settings = settings or await self._guild_settings(guild)
        key = await self._require_api_key()
        registrations = _registration_usernames(settings["registrations"])
        if not registrations:
            raise SummaryError(
                "No members have linked a Last.fm account yet. Use `lastfm register` first."
            )

        usernames = list(registrations.values())
        grid_size = _clamp_int(settings["grid_size"], DEFAULT_GRID, 2, 8)
        source = "albums" if settings["source"] == "albums" else "artists"
        period = settings["period"]
        limit = min(200, max(50, grid_size * grid_size * 5))

        client = LastFMClient(key)
        try:
            charts = await client.fetch_charts(usernames, source=source, period=period, limit=limit)
        except LastFMError as exc:
            if exc.code in {10, 26}:
                raise SummaryError(
                    "Last.fm rejected the configured API key. The bot owner must set a valid key."
                ) from exc
            raise SummaryError(f"Last.fm rejected the request: {exc.message}") from exc
        finally:
            await client.close()

        by_username = {chart.username: chart for chart in charts}
        failed = [username for username in usernames if username not in by_username]
        if not charts:
            details = f" ({len(failed)} member(s) failed)" if failed else ""
            raise SummaryError(f"Could not fetch any Last.fm charts right now{details}.")

        serp_images, deezer_images = await self._resolve_fallbacks(charts, source, grid_size)

        posted = 0
        errors: list[str] = []
        for user_id, username in registrations.items():
            chart = by_username.get(username)
            if chart is None:
                continue
            display_name = self._display_name(guild, user_id, username)
            try:
                await self._post_chart(
                    guild,
                    channel,
                    chart,
                    display_name=display_name,
                    grid_size=grid_size,
                    source=source,
                    period=period,
                    serp_images=serp_images,
                    deezer_images=deezer_images,
                )
            except discord.Forbidden:
                raise
            except (SummaryError, discord.HTTPException) as exc:
                failed.append(username)
                errors.append(f"{username}: {exc}")
                log.warning("Could not post Last.fm chart for %s: %s", username, exc)
            else:
                posted += 1

        if posted == 0:
            detail = f" {len(failed)} member(s) failed." if failed else ""
            raise SummaryError(f"Could not post any member charts.{detail}")
        return PostReport(posted=posted, failed=tuple(failed), errors=tuple(errors))

    # -------------------------------------------------------------- scheduler

    @tasks.loop(seconds=30)
    async def _scheduler(self) -> None:
        try:
            now = datetime.now(timezone.utc)
            for guild in list(self.bot.guilds):
                try:
                    await self._tick_guild(guild, now)
                except Exception:
                    log.exception("Last.fm summary tick failed for guild %s", guild.id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Last.fm summary scheduler failed")

    @_scheduler.before_loop
    async def _before_scheduler(self) -> None:
        await self.bot.wait_until_ready()

    async def _tick_guild(self, guild: discord.Guild, now: datetime) -> None:
        settings = await self._guild_settings(guild)
        if not settings["enabled"] or not settings["channel_id"]:
            return
        next_run = settings["next_run_at"]
        if not _registration_usernames(settings["registrations"]):
            if next_run is not None and next_run <= now:
                await self._store_next_run(guild, settings, from_now=now)
            return
        if next_run is None:
            await self._store_next_run(guild, settings, from_now=now)
            return
        if next_run > now:
            return

        lock = self._lock(guild.id)
        if lock.locked():
            return
        async with lock:
            if now - next_run > MAX_LATE:
                log.info("Skipping overdue Last.fm summary for guild %s", guild.id)
                await self._store_next_run(guild, settings, from_now=now)
                return
            channel = guild.get_channel(settings["channel_id"])
            error: str | None = None
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                error = "The configured summary channel no longer exists."
            else:
                try:
                    report = await self._post_charts(guild, channel, settings)
                except SummaryError as exc:
                    error = str(exc)
                except discord.Forbidden:
                    error = f"Missing permissions to post in {channel.mention}."
                except discord.HTTPException as exc:
                    error = f"Discord rejected the upload: {exc}"
                except Exception as exc:
                    error = f"Unexpected error: {exc}"
                    log.exception("Last.fm summary failed for guild %s", guild.id)
                else:
                    if report.failed:
                        names = ", ".join(report.failed[:10])
                        more = "…" if len(report.failed) > 10 else ""
                        error = (
                            f"Posted {report.posted} chart(s); "
                            f"{len(report.failed)} member(s) failed: {names}{more}"
                        )

            if error:
                log.warning("Last.fm summary for guild %s failed: %s", guild.id, error)
            await self.config.guild(guild).last_run_at.set(_iso(now))
            await self.config.guild(guild).last_error.set(error)
            await self._store_next_run(guild, settings, from_now=datetime.now(timezone.utc))

    # --------------------------------------------------------------- data API

    async def red_get_data_for_user(self, *, user_id: int) -> dict[str, io.BytesIO]:
        all_guilds = await self.config.all_guilds()
        entries = []
        for guild_id, data in all_guilds.items():
            registrations = data.get("registrations") or {}
            row = registrations.get(str(user_id))
            if row:
                entries.append(
                    {
                        "guild_id": guild_id,
                        "username": row.get("username") if isinstance(row, Mapping) else row,
                        "registered_at": (
                            row.get("registered_at") if isinstance(row, Mapping) else None
                        ),
                    }
                )
        payload = json.dumps({"lastfm_summary": entries}, indent=2).encode("utf-8")
        return {"lastfmsummary-user-data.json": io.BytesIO(payload)}

    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        all_guilds = await self.config.all_guilds()
        for guild_id, data in all_guilds.items():
            registrations = data.get("registrations") or {}
            if str(user_id) in registrations:
                registrations = dict(registrations)
                registrations.pop(str(user_id), None)
                await self.config.guild_from_id(guild_id).registrations.set(registrations)
