"""Levels: message and voice XP, rank cards and the XP leaderboard.

    !rank [member]     your card — level, bar, rank, activity, wallet
    !levels [count]    the leaderboard, with a dropdown to inspect anyone on it
    !profilestats      your last two weeks as an image card, for anyone to see
    !popups on|off     turn your level-up messages on or off

XP comes from two places: chatting (throttled per member, so spamming is
pointless) and genuinely sitting in voice with other people. Levels pay out
through the shared economy engine, and milestone levels pay a bonus on top.

The leaderboard doubles as a browser — the dropdown rebuilds the same message
into the selected member's card, so nobody has to run the command five times.

Level-up cards are optional twice over: a member can mute their own with
``!popups``, and the owner can silence them for the whole bot with
``!admin popups off``. Rewards, roles and achievements are still paid out when
the cards are off — only the announcement is skipped.
"""

from __future__ import annotations

import io
import logging
import random
import time
from datetime import timedelta
from typing import Any, Sequence

import discord
from discord.ext import commands, tasks

from config import EMBED_COLOR, PREFIX, leveling
from utils.core import levelroles, preferences, profilecard
from utils.core.economy import format_cash
from utils.core.helpers import fmtseconds
from utils.core.leveling import (
    apply_xp,
    level_reward,
    position_badge,
    progress_bar,
    total_xp,
    xp_for_level,
)
from utils.core.settings import LEVEL_POPUPS_KEY
from utils.core.views import SimpleLayout, swap_layout_message

logger = logging.getLogger("slimey.levels")

LEADERBOARD_LIMIT = 25
MAX_SELECT_OPTIONS = 25

#: The lookback the `!profilestats` card covers, and how much raw history is
#: kept behind it. Activity is counted as it happens, so a fresh install fills
#: the card in over its first two weeks.
ACTIVITY_WINDOW_DAYS = 14
ACTIVITY_KEEP_DAYS = 95
#: The windows the card breaks messages and voice time out into.
ACTIVITY_WINDOWS = (1, 7, 14)
#: Rows the top-channels panel has room for.
ACTIVITY_CHANNEL_ROWS = 3


def _day_key(value: Any) -> str:
    """A stored ``day`` cell back as a ``YYYY-MM-DD`` string.

    :class:`utils.core.database.Database` parses anything
    ``datetime.fromisoformat`` accepts on read, and a bare calendar date is one
    of them, so the day comes back as a ``datetime`` rather than the string
    that was written.
    """
    if hasattr(value, "date"):
        return value.date().isoformat()
    return str(value)


def _xp_gain() -> int:
    """A small random spread so two people chatting the same amount differ."""
    base = max(1, leveling.xp_per_message)
    return base + random.randint(0, max(0, base // 2))


def _panel(*items: discord.ui.Item) -> SimpleLayout:
    container = discord.ui.Container(accent_color=EMBED_COLOR)
    for item in items:
        container.add_item(item)
    return SimpleLayout(container)


def _text(message: str) -> SimpleLayout:
    return _panel(discord.ui.TextDisplay(message))


def heading(emoji: str, text: str, *, sub: str | None = None) -> str:
    """``# 🏅 **Name**`` plus an optional ``-# `` line — the house card header."""
    line = f"# {emoji} **{text}**"
    return f"{line}\n-# {sub}" if sub else line


def field(label: str, value: str) -> str:
    """A label line with its value underneath it."""
    return f"**{label}**\n{value}"


def money_field(label: str, amount: int) -> str:
    return f"**{label}**\n{format_cash(amount)}"


class Leveling(commands.Cog):
    """Message and voice XP, ranks and the leaderboard."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # user id -> monotonic timestamp of their last message XP award
        self._last_award: dict[int, float] = {}

    async def cog_load(self) -> None:
        if not self.voice_tick.is_running():
            self.voice_tick.start()
        if not self.activity_prune.is_running():
            self.activity_prune.start()

    async def cog_unload(self) -> None:
        self.voice_tick.cancel()
        self.activity_prune.cancel()

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------
    async def progress(self, user_id: int, *, guild_id: int) -> tuple[int, int]:
        """The stored ``(level, xp into level)`` for a member in one server.

        Progress is per server, so ``guild_id`` is required: earning XP in one
        guild never moves a member up the ladder in another.
        """
        row = await self.bot.db.fetchrow(
            "SELECT level, xp FROM user_levels "
            "WHERE guild_id = %s AND user_id = %s",
            int(guild_id),
            int(user_id),
        )
        if not row:
            return 1, 0
        return max(1, int(row[0] or 1)), max(0, int(row[1] or 0))

    async def activity(self, user_id: int, *, guild_id: int) -> tuple[int, int]:
        """``(messages, voice_seconds)`` counted in this server so far."""
        row = await self.bot.db.fetchrow(
            "SELECT messages, voice_seconds FROM level_stats "
            "WHERE guild_id = %s AND user_id = %s",
            int(guild_id),
            int(user_id),
        )
        if not row:
            return 0, 0
        return int(row[0] or 0), int(row[1] or 0)

    # ------------------------------------------------------------------
    # Windowed activity (behind !profilestats)
    # ------------------------------------------------------------------
    @staticmethod
    def _utc_day() -> str:
        """Today as a UTC ``YYYY-MM-DD`` key, which is what the card labels."""
        return discord.utils.utcnow().date().isoformat()

    @staticmethod
    def _cutoff(days: int) -> str:
        """The first day of an ``days``-long window that ends today (UTC)."""
        span = max(1, int(days)) - 1
        return (discord.utils.utcnow().date() - timedelta(days=span)).isoformat()

    async def record_activity(
        self,
        *,
        guild_id: int,
        user_id: int,
        channel_id: int,
        kind: str,
        amount: int,
    ) -> None:
        """Add ``amount`` to one member's activity in one channel today.

        Called for every message and every voice minute. One row carries the
        member, the day, the channel and the kind, so the daily windows and the
        top-channels panel are both answered from the same table.
        """
        if int(amount) <= 0:
            return
        try:
            await self.bot.db.execute(
                "INSERT INTO activity_events "
                "(guild_id, user_id, day, channel_id, kind, amount) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT(guild_id, user_id, day, channel_id, kind) "
                "DO UPDATE SET amount = amount + excluded.amount",
                int(guild_id),
                int(user_id),
                self._utc_day(),
                int(channel_id),
                str(kind),
                int(amount),
            )
        except Exception:
            # Counting is best-effort: a failed upsert should never cost the
            # member their message or their XP.
            logger.exception("Could not record %s activity for %s", kind, user_id)

    async def activity_window(
        self, guild_id: int, user_id: int, days: int
    ) -> tuple[int, int]:
        """``(messages, voice_seconds)`` logged in the last ``days`` days."""
        row = await self.bot.db.fetchrow(
            "SELECT COALESCE(SUM(CASE WHEN kind = 'message' THEN amount END), 0), "
            "       COALESCE(SUM(CASE WHEN kind = 'voice' THEN amount END), 0) "
            "FROM activity_events "
            "WHERE guild_id = %s AND user_id = %s AND day >= %s",
            int(guild_id),
            int(user_id),
            self._cutoff(days),
        )
        if not row:
            return 0, 0
        return int(row[0] or 0), int(row[1] or 0)

    async def window_ranks(
        self, guild: discord.Guild, user_id: int, days: int
    ) -> tuple[int | None, int | None]:
        """The member's ``(message, voice)`` rank on this server in the window.

        The board is built over the same window the card shows, so it reflects
        who was actually around rather than who has been here longest. Members
        who have since left are ignored, as are the two kinds' different units -
        messages and seconds are ranked separately.
        """
        rows = await self.bot.db.execute(
            "SELECT user_id, "
            "       SUM(CASE WHEN kind = 'message' THEN amount ELSE 0 END), "
            "       SUM(CASE WHEN kind = 'voice' THEN amount ELSE 0 END) "
            "FROM activity_events WHERE guild_id = %s AND day >= %s "
            "GROUP BY user_id",
            int(guild.id),
            self._cutoff(days),
        )
        message_board: list[tuple[int, int]] = []
        voice_board: list[tuple[int, int]] = []
        for row in rows:
            member_id = int(row[0])
            if guild.get_member(member_id) is None:
                continue
            messages, voice = int(row[1] or 0), int(row[2] or 0)
            if messages > 0:
                message_board.append((member_id, messages))
            if voice > 0:
                voice_board.append((member_id, voice))
        message_board.sort(key=lambda item: item[1], reverse=True)
        voice_board.sort(key=lambda item: item[1], reverse=True)

        def rank(board: Sequence[tuple[int, int]]) -> int | None:
            return next(
                (place for place, (mid, _) in enumerate(board, 1) if mid == int(user_id)),
                None,
            )

        return rank(message_board), rank(voice_board)

    async def daily_series(
        self, guild_id: int, user_id: int, days: int
    ) -> list[tuple[str, int, int]]:
        """One ``(day, messages, voice_seconds)`` per day, oldest first."""
        rows = await self.bot.db.execute(
            "SELECT day, "
            "       SUM(CASE WHEN kind = 'message' THEN amount ELSE 0 END), "
            "       SUM(CASE WHEN kind = 'voice' THEN amount ELSE 0 END) "
            "FROM activity_events "
            "WHERE guild_id = %s AND user_id = %s AND day >= %s GROUP BY day",
            int(guild_id),
            int(user_id),
            self._cutoff(days),
        )
        buckets = {
            _day_key(row[0]): (int(row[1] or 0), int(row[2] or 0)) for row in rows
        }
        today = discord.utils.utcnow().date()
        series: list[tuple[str, int, int]] = []
        for offset in range(int(days) - 1, -1, -1):
            day = (today - timedelta(days=offset)).isoformat()
            messages, voice = buckets.get(day, (0, 0))
            series.append((day, messages, voice))
        return series

    async def top_channels(
        self, guild_id: int, user_id: int, limit: int = ACTIVITY_CHANNEL_ROWS
    ) -> dict[str, list[tuple[int, int]]]:
        """The member's busiest channels in the window, split by kind.

        Messages and voice seconds are ranked separately because they are not
        comparable units; the card then interleaves the two lists.
        """
        rows = await self.bot.db.execute(
            "SELECT kind, channel_id, SUM(amount) FROM activity_events "
            "WHERE guild_id = %s AND user_id = %s AND day >= %s "
            "GROUP BY kind, channel_id ORDER BY SUM(amount) DESC",
            int(guild_id),
            int(user_id),
            self._cutoff(ACTIVITY_WINDOW_DAYS),
        )
        boards: dict[str, list[tuple[int, int]]] = {"message": [], "voice": []}
        for kind, channel_id, amount in rows:
            bucket = boards.get(str(kind))
            if bucket is None:
                continue
            bucket.append((int(channel_id), int(amount or 0)))
        for bucket in boards.values():
            del bucket[int(limit) :]
        return boards

    async def _save(
        self,
        user_id: int,
        level: int,
        xp: int,
        *,
        guild_id: int,
        messages: int = 0,
        voice: int = 0,
    ) -> None:
        await self.bot.db.execute(
            "INSERT INTO user_levels (guild_id, user_id, level, xp) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
            "level = excluded.level, xp = excluded.xp",
            int(guild_id),
            int(user_id),
            int(level),
            int(xp),
        )
        if messages or voice:
            await self.bot.db.execute(
                "INSERT INTO level_stats "
                "(guild_id, user_id, messages, voice_seconds) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT(guild_id, user_id) DO UPDATE SET "
                "messages = messages + excluded.messages, "
                "voice_seconds = voice_seconds + excluded.voice_seconds",
                int(guild_id),
                int(user_id),
                int(messages),
                int(voice),
            )

    async def grant(
        self,
        user_id: int,
        amount: int,
        *,
        guild_id: int,
        messages: int = 0,
        voice: int = 0,
    ):
        """Award XP in one server and return ``(level, xp, level_ups)``."""
        level, xp = await self.progress(user_id, guild_id=guild_id)
        new_level, new_xp, level_ups = apply_xp(level, xp, amount)
        await self._save(
            user_id,
            new_level,
            new_xp,
            guild_id=guild_id,
            messages=messages,
            voice=voice,
        )
        return new_level, new_xp, level_ups

    async def position(self, level: int, xp: int, *, guild_id: int) -> int:
        """1-based rank on this server's XP board for a given level/progress."""
        above = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM user_levels "
            "WHERE guild_id = %s AND (level > %s OR (level = %s AND xp > %s))",
            int(guild_id),
            level,
            level,
            xp,
        )
        return int(above or 0) + 1

    # ------------------------------------------------------------------
    # Level-up announcements
    # ------------------------------------------------------------------
    def globally_announcing(self) -> bool:
        """Whether level-up cards are on for the bot as a whole."""
        settings = getattr(self.bot, "dash_settings", None)
        if settings is None:
            return leveling.announce_level_ups
        return leveling.announce_level_ups and settings.flag(LEVEL_POPUPS_KEY, True)

    async def popups_enabled(self, user_id: int) -> bool:
        """Whether a level-up card should be posted for ``user_id``.

        Two switches have to agree: the owner's bot-wide one and the member's
        own preference. Both default to on, so a fresh install announces every
        level up until someone says otherwise.
        """
        if not self.globally_announcing():
            return False
        return preferences.is_on(
            await preferences.read(self.bot.db, user_id, preferences.POPUPS)
        )

    # ------------------------------------------------------------------
    # Level roles
    # ------------------------------------------------------------------
    async def ladder(self, guild_id: int) -> levelroles.Ladder:
        """This guild's level -> role ladder, with its behaviour switches."""
        return await levelroles.load(self.bot.db, guild_id)

    async def sync_level_roles(
        self, member: discord.Member, level: int
    ) -> levelroles.Outcome:
        """Apply the guild's ladder to ``member`` at ``level``.

        The policy lives in :mod:`utils.core.levelroles` so the preview a
        moderator asks for and the grant that actually happens are computed by
        the same code. A mis-configured role is reported in the outcome instead
        of raising, which keeps it from breaking the level-up card.
        """
        ladder = await self.ladder(member.guild.id)
        return await levelroles.apply(
            member, level, ladder, reason=f"Reached level {level}"
        )

    # ------------------------------------------------------------------
    # Earning XP
    # ------------------------------------------------------------------
    async def _is_command(self, message: discord.Message) -> bool:
        content = message.content or ""
        if not content:
            return False
        # The bot's prefix is resolved through its async ``get_prefix``; the raw
        # ``command_prefix`` attribute is ``when_mentioned_or(get_prefix)`` and
        # so returns the bound method rather than a usable string.
        try:
            prefixes = await self.bot.get_prefix(message)
        except Exception:
            prefixes = ()
        if isinstance(prefixes, str):
            prefixes = (prefixes,)
        return any(
            isinstance(prefix, str) and prefix and content.startswith(prefix)
            for prefix in prefixes
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        if not (message.content or "").strip() or await self._is_command(message):
            return

        # Every message is counted, not just the ones that earn XP: the stats
        # card is about how much someone talks, not how fast they level.
        await self.record_activity(
            guild_id=message.guild.id,
            user_id=message.author.id,
            channel_id=message.channel.id,
            kind="message",
            amount=1,
        )

        user_id = message.author.id
        now = time.monotonic()
        if now - self._last_award.get(user_id, 0.0) < max(0, leveling.xp_cooldown):
            return
        self._last_award[user_id] = now

        try:
            _, _, level_ups = await self.grant(
                user_id, _xp_gain(), guild_id=message.guild.id, messages=1
            )
        except Exception:
            logger.exception("Could not award XP to %s", user_id)
            return

        for level_up in level_ups:
            await self._celebrate(message, level_up)

    @tasks.loop(minutes=1)
    async def voice_tick(self) -> None:
        """Pay voice XP every minute to anyone genuinely hanging out."""
        if leveling.xp_per_voice_minute <= 0:
            return
        for guild in list(self.bot.guilds):
            for channel in guild.voice_channels:
                if channel.id == getattr(guild.afk_channel, "id", None):
                    continue
                humans = [member for member in channel.members if not member.bot]
                if len(humans) < 2:
                    # Sitting alone (or with only music bots) earns nothing.
                    continue
                for member in humans:
                    try:
                        await self.grant(
                            member.id,
                            leveling.xp_per_voice_minute,
                            guild_id=guild.id,
                            voice=60,
                        )
                    except Exception:
                        logger.exception("Voice XP failed for %s", member.id)
                    await self.record_activity(
                        guild_id=guild.id,
                        user_id=member.id,
                        channel_id=channel.id,
                        kind="voice",
                        amount=60,
                    )

    @tasks.loop(hours=24)
    async def activity_prune(self) -> None:
        """Drop activity older than the keep window so the table stays small."""
        cutoff = (
            discord.utils.utcnow().date() - timedelta(days=ACTIVITY_KEEP_DAYS)
        ).isoformat()
        try:
            await self.bot.db.execute(
                "DELETE FROM activity_events WHERE day < %s", cutoff
            )
        except Exception:
            logger.exception("Could not prune old activity rows")

    @activity_prune.before_loop
    async def _wait_ready_for_prune(self) -> None:
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            logger.warning("Activity prune loop started before login; skipping until ready")

    @voice_tick.before_loop
    async def _wait_ready(self) -> None:
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            # Only possible when the client was never logged in (offline
            # tooling). Nothing to pay out, and the loop keeps its slot.
            logger.warning("Voice XP loop started before login; skipping until ready")

    async def _celebrate(self, message: discord.Message, level_up) -> None:
        """Pay out and announce a level up with a full card.

        One read of the member's preferences decides everything about the
        announcement: whether it happens at all, whether it goes to the channel
        or their DMs, and what it is allowed to show.
        """
        member = message.author
        values = await preferences.read_all(self.bot.db, member.id)
        try:
            account = await self.bot.economy.add(
                member.id,
                level_up.reward,
                description=f"Level {level_up.level} reward",
            )
        except Exception:
            logger.exception(
                "Could not pay the level %s reward to %s", level_up.level, member.id
            )
            account = None

        # Crossing a level can also cross an achievement target (level 10), so
        # this is checked here rather than only when a command is run.
        try:
            unlocked = await self.bot.economy.grant_achievements(member.id)
        except Exception:
            logger.exception("Achievement check failed for %s", member.id)
            unlocked = []

        level, xp = await self.progress(member.id, guild_id=member.guild.id)
        needed = xp_for_level(level)
        next_reward, next_milestone = level_reward(level + 1)

        # Roles are handled before the announcement check: `;admin rolelevel`
        # wiring should keep working even with the chatter turned off.
        ladder = levelroles.Ladder()
        role_outcome: levelroles.Outcome | None = None
        if isinstance(member, discord.Member):
            ladder = await self.ladder(member.guild.id)
            try:
                role_outcome = await self.sync_level_roles(member, level_up.level)
            except Exception:
                logger.exception("Level role sync failed for %s", member.id)
            if role_outcome.granted and ladder.dm:
                await self._dm_role(member, level, role_outcome)

        if not self.globally_announcing() or not preferences.is_on(
            values[preferences.POPUPS]
        ):
            return

        channel = message.channel
        if leveling.level_up_channel:
            override = self.bot.get_channel(leveling.level_up_channel)
            if override is not None:
                channel = override

        hide_money = preferences.is_on(values[preferences.HIDE_WORTH])
        show_achievements = preferences.is_on(values[preferences.ACHIEVEMENT_NOTICES])
        wants_dm = preferences.is_on(values[preferences.LEVELUP_DM]) and isinstance(
            member, discord.Member
        )

        marker = "🏆" if level_up.milestone else "🎉"
        label = "Milestone reached" if level_up.milestone else "Level up"
        body = (
            heading(marker, label, sub=f"{member.mention} reached level **{level}**")
            + "\n\n"
            + progress_bar(xp, needed)
            + f" `{xp:,} / {needed:,} XP`"
            + "\n\n"
            + money_field("Paid out", level_up.reward)
        )
        if level_up.milestone:
            body += "\n\n" + money_field("Milestone bonus", leveling.milestone_reward)
        body += "\n\n" + field("Total XP", f"**{total_xp(level, xp):,}**")
        if account is not None and not hide_money:
            body += "\n\n" + money_field("Wallet", account.wallet)
        if role_outcome is not None and role_outcome.changed and ladder.announce:
            role_note = ", ".join(role.mention for role in role_outcome.granted)
            if not role_note:
                role_note = "—"
            if role_outcome.removed:
                role_note += " *(took back " + ", ".join(
                    role.mention for role in role_outcome.removed
                ) + ")*"
            body += "\n\n" + field("Role", role_note)
        if role_outcome is not None and role_outcome.blocked:
            body += "\n\n" + field(
                "Role trouble",
                ", ".join(role.mention for role in role_outcome.blocked)
                + " *(I can't hand that one out — check my role position)*",
            )

        container = discord.ui.Container(accent_color=EMBED_COLOR)
        container.add_item(
            discord.ui.Section(
                discord.ui.TextDisplay(body),
                accessory=discord.ui.Thumbnail(media=member.display_avatar.url),
            )
        )
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))

        footer = f"Next level pays {format_cash(next_reward)}"
        if next_milestone:
            footer += " *(milestone)*"
        if leveling.milestone_every > 0:
            remaining = leveling.milestone_every - (level % leveling.milestone_every)
            footer += (
                f" · {remaining} level(s) to the next milestone bonus"
                if remaining != leveling.milestone_every
                else " · the next level is a milestone"
            )

        if unlocked and show_achievements:
            container.add_item(
                discord.ui.TextDisplay(
                    "**Achievements unlocked**\n"
                    + "\n\n".join(
                        f"• {item.emoji} **{item.title}** — +{format_cash(item.reward)}\n"
                        f"  {item.description}"
                        for item in unlocked
                    )
                )
            )
            container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))

        container.add_item(discord.ui.TextDisplay(f"-# {footer}"))
        view = SimpleLayout(container)

        if wants_dm:
            try:
                await member.send(view=view)
                return
            except (discord.Forbidden, discord.HTTPException):
                # Closed DMs are common and not worth losing the announcement
                # over, so it falls back to the channel it was headed for.
                logger.debug("Could not DM the level card to %s", member.id)

        try:
            await channel.send(view=view)
        except discord.DiscordException:
            pass

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    async def card(self, member: discord.Member) -> SimpleLayout:
        """A full rank card for ``member``."""
        level, xp = await self.progress(member.id, guild_id=member.guild.id)
        messages, voice_seconds = await self.activity(
            member.id, guild_id=member.guild.id
        )
        needed = xp_for_level(level)
        position = await self.position(level, xp, guild_id=member.guild.id)
        # The card belongs to the member, so it reads their own display
        # preferences rather than those of whoever asked for it.
        values = await preferences.read_all(self.bot.db, member.id)
        hide_money = preferences.is_on(values[preferences.HIDE_WORTH])
        compact = preferences.is_on(values[preferences.COMPACT_MONEY])

        try:
            account = await self.bot.economy.account(member.id)
            wallet = format_cash(account.total, compact=compact)
        except Exception:
            wallet = "n/a"

        next_reward, next_milestone = level_reward(level + 1)
        if leveling.milestone_every > 0:
            remaining = leveling.milestone_every - (level % leveling.milestone_every)
            if remaining == leveling.milestone_every:
                remaining = 0
            when = (
                f"{remaining} level(s) until the next milestone bonus"
                if remaining
                else "the next level is a milestone"
            )
            milestone_note = f"\n-# Milestone bonus every {leveling.milestone_every} levels — {when}"
        else:
            milestone_note = ""

        container = discord.ui.Container(accent_color=EMBED_COLOR)
        container.add_item(
            discord.ui.Section(
                discord.ui.TextDisplay(
                    heading(
                        position_badge(position),
                        member.display_name,
                        sub=f"server rank #{position} · level {level}",
                    )
                    + "\n\n"
                    + progress_bar(xp, needed)
                    + f" `{xp:,} / {needed:,} XP`"
                ),
                accessory=discord.ui.Thumbnail(media=member.display_avatar.url),
            )
        )
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        body = (
            field("Total XP", f"**{total_xp(level, xp):,}**")
            + "\n\n"
            + field(
                "Next level pays",
                format_cash(next_reward, compact=compact)
                + (" *(milestone)*" if next_milestone else ""),
            )
            + "\n\n"
            + field(
                "Activity",
                f"**{messages:,}** messages · "
                f"**{fmtseconds(voice_seconds or 0, unit='seconds')}** in voice",
            )
        )
        if not hide_money:
            body += "\n\n" + field("Net worth", wallet)
        body += milestone_note + await self._role_note(member, level)
        container.add_item(discord.ui.TextDisplay(body))
        return SimpleLayout(container)

    async def _role_note(self, member: discord.Member, level: int) -> str:
        """What the ladder is giving this member, and what the next tier is."""
        ladder = await self.ladder(member.guild.id)
        if ladder.empty:
            return ""

        guild = member.guild
        bits: list[str] = []
        earned = [
            role
            for role in (guild.get_role(rid) for rid in ladder.earned_roles(level))
            if role is not None
        ]
        if earned:
            handed = [role for role in earned if role.is_assignable()]
            blocked = [role for role in earned if not role.is_assignable()]
            if handed:
                bits.append("earning " + ", ".join(role.mention for role in handed))
            if blocked:
                bits.append(
                    "⚠️ can't hand out " + ", ".join(role.mention for role in blocked)
                )
        elif ladder.mode == "stack":
            pass
        else:
            bits.append(f"no role until level {ladder.tiers()[0]}")

        upcoming = levelroles.next_tier(ladder, level)
        if upcoming is not None:
            next_level, role_ids = upcoming
            roles = [
                role
                for role in (guild.get_role(rid) for rid in role_ids)
                if role is not None
            ]
            if roles:
                bits.append(
                    f"level {next_level} adds "
                    + ", ".join(role.mention for role in roles)
                )
        return "\n-# Role · " + " · ".join(bits) if bits else ""

    async def _dm_role(
        self, member: discord.Member, level: int, outcome: levelroles.Outcome
    ) -> None:
        """Tell the member about the badges they just picked up."""
        listed = ", ".join(role.name for role in outcome.granted)
        try:
            await member.send(
                f"🎖️ Reaching level **{level}** in **{member.guild.name}** "
                f"earned you: {listed}"
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.debug("Could not DM %s about their level roles", member.id)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    @commands.command(name="rank", aliases=("lvl", "level"))
    @commands.guild_only()
    async def rank(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        f"""Show a member's level, progress bar, rank and activity.

        Usage: `{PREFIX}rank` for yourself, or `{PREFIX}rank @member` for
        someone else.
        """
        target = member or ctx.author
        if target.bot:
            await ctx.send(view=_text("Bots don't earn XP."))
            return
        await ctx.send(view=await self.card(target))

    @commands.command(name="profilestats", aliases=("pstats", "activitycard"))
    @commands.guild_only()
    async def profilestats(
        self, ctx: commands.Context, member: discord.Member | None = None
    ) -> None:
        """Show a member's recent activity as an image card.

        Usage: `{PREFIX}profilestats` for yourself, or `{PREFIX}profilestats
        @member` for someone else. The card covers the last 14 days in UTC:
        messages, voice time, server ranks, the channels used most and a daily
        chart of both. Counting starts from when the bot began recording it, so
        a freshly added bot fills the card in over its first two weeks.
        """
        target = member or ctx.author
        if target.bot:
            await ctx.send(view=_text("Bots don't have activity to show."))
            return

        buffer = await self.profile_card(target)
        if buffer is None:
            await ctx.send(view=await self._profile_card_text(target))
            return
        await ctx.send(
            file=discord.File(buffer, filename=f"profilestats-{target.id}.png")
        )

    async def profile_card(self, member: discord.Member) -> io.BytesIO | None:
        """Render ``member``'s activity card, or ``None`` if it can't be drawn."""
        if not profilecard.PIL_AVAILABLE:
            return None
        guild = member.guild

        windows = [
            (f"{days}d", *await self.activity_window(guild.id, member.id, days))
            for days in ACTIVITY_WINDOWS
        ]
        message_rank, voice_rank = await self.window_ranks(
            guild, member.id, ACTIVITY_WINDOW_DAYS
        )
        series = await self.daily_series(guild.id, member.id, ACTIVITY_WINDOW_DAYS)
        boards = await self.top_channels(guild.id, member.id)
        level, _xp = await self.progress(member.id, guild_id=guild.id)

        # Interleave the two boards so the panel reads text channel, voice
        # channel, text channel - messages and seconds have no common scale.
        picks: list[tuple[str, int, bool]] = []
        text_board, voice_board = boards["message"], boards["voice"]
        text_index = voice_index = 0
        while len(picks) < ACTIVITY_CHANNEL_ROWS and (
            text_index < len(text_board) or voice_index < len(voice_board)
        ):
            if text_index < len(text_board):
                channel_id, amount = text_board[text_index]
                picks.append((self._channel_label(guild, channel_id, True), amount, True))
                text_index += 1
            if len(picks) < ACTIVITY_CHANNEL_ROWS and voice_index < len(voice_board):
                channel_id, amount = voice_board[voice_index]
                picks.append((self._channel_label(guild, channel_id, False), amount, False))
                voice_index += 1

        try:
            avatar = await member.display_avatar.with_format("png").with_size(128).read()
        except Exception:
            logger.debug("Could not fetch the avatar for %s", member.id, exc_info=True)
            avatar = None

        return profilecard.render(
            name=member.display_name,
            handle=f"@{member.name}",
            level=level,
            created_at=self._date_text(member.created_at),
            joined_at=self._date_text(getattr(member, "joined_at", None)),
            message_rank=message_rank,
            voice_rank=voice_rank,
            windows=windows,
            top_channels=picks,
            series=[(messages, voice) for _day, messages, voice in series],
            avatar=avatar,
            lookback_days=ACTIVITY_WINDOW_DAYS,
        )

    @staticmethod
    def _date_text(value: Any) -> str:
        """A timestamp as ``May 5, 2025``; ``—`` when it isn't known."""
        if value is None:
            return "—"
        return f"{value.strftime('%B')} {value.day}, {value.year}"

    @staticmethod
    def _channel_label(guild: discord.Guild, channel_id: int, is_message: bool) -> str:
        """A channel's name for the card, or a note when it has been deleted."""
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            return "# deleted-channel" if is_message else "deleted channel"
        return f"# {channel.name}" if is_message else channel.name

    async def _profile_card_text(self, member: discord.Member) -> SimpleLayout:
        """The text version of the card, for when Pillow isn't installed."""
        guild = member.guild
        level, _xp = await self.progress(member.id, guild_id=guild.id)
        message_rank, voice_rank = await self.window_ranks(
            guild, member.id, ACTIVITY_WINDOW_DAYS
        )
        lines: list[str] = []
        for days in ACTIVITY_WINDOWS:
            messages, voice = await self.activity_window(guild.id, member.id, days)
            lines.append(
                f"**{days}d** · {messages:,} messages · "
                f"{fmtseconds(voice, unit='seconds')} in voice"
            )
        body = (
            heading(
                "📊",
                member.display_name,
                sub=f"level {level} · last {ACTIVITY_WINDOW_DAYS} days",
            )
            + "\n\n"
            + field(
                "Server rank",
                f"messages **#{message_rank or '—'}** · voice **#{voice_rank or '—'}**",
            )
            + "\n\n"
            + "\n".join(lines)
        )
        return _text(body)

    @commands.command(
        name="popups", aliases=("levelups", "levelpopups", "levelup")
    )
    @commands.guild_only()
    async def popups(self, ctx: commands.Context, state: str | None = None) -> None:
        f"""Turn your level-up messages on or off.

        Usage: `{PREFIX}popups` to see the current setting, `{PREFIX}popups off`
        to stop the cards, `{PREFIX}popups on` to bring them back. Rewards and
        level roles keep coming either way — this only controls the announce.
        """
        wanted = str(state or "").strip()
        current = await preferences.read(self.bot.db, ctx.author.id, preferences.POPUPS)

        if not wanted:
            await ctx.send(view=self._popup_card(preferences.is_on(current), saved=False))
            return

        if preferences.is_toggle_token(wanted):
            flag = not preferences.is_on(current)
        else:
            flag = preferences.token_value(preferences.spec(preferences.POPUPS), wanted)

        if flag is None:
            await ctx.send(
                view=_text(
                    "Give me `on` or `off` — or run it with nothing for the status."
                )
            )
            return

        await preferences.write(self.bot.db, ctx.author.id, preferences.POPUPS, flag)
        await ctx.send(view=self._popup_card(flag, saved=True))

    def _popup_card(self, enabled: bool, *, saved: bool) -> SimpleLayout:
        """The little confirmation card the toggle answers with."""
        if enabled:
            body = (
                "# 🔔 Level-up messages **on**\n"
                f"-# You'll get a card every time you level up. Turn them off with "
                f"`{PREFIX}popups off`."
            )
        else:
            body = (
                "# 🔕 Level-up messages **off**\n"
                "-# No more level-up cards for you — your XP, coins and level "
                f"roles are unaffected. `{PREFIX}popups on` brings them back."
            )
        if not saved:
            body += "\n\n-# This is your current setting."
        if not self.globally_announcing():
            body += (
                "\n\n-# Note: the owner has switched level-up messages off for "
                "everyone right now."
            )
        body += f"\n\n-# Everything else you can switch lives in `{PREFIX}prefs`."
        return _text(body)

    @commands.command(name="levels", aliases=("xpboard", "xptop"))
    @commands.guild_only()
    async def levels(self, ctx: commands.Context, count: int = 10) -> None:
        f"""Show the XP leaderboard, with a dropdown to inspect anyone.

        Usage: `{PREFIX}levels` for the top 10, `{PREFIX}levels 20` for more.
        This is the board for *this server* — XP never crosses over from
        another one. `{PREFIX}lb` is the money board.
        """
        count = max(1, min(LEADERBOARD_LIMIT, count))
        rows = await self.bot.db.execute(
            "SELECT user_id, level, xp FROM user_levels WHERE guild_id = %s "
            "ORDER BY level DESC, xp DESC LIMIT %s",
            ctx.guild.id,
            count,
        )

        members: list[tuple[discord.Member, int, int]] = []
        for user_id, level, xp in rows:
            member = ctx.guild.get_member(int(user_id))
            if member is not None:
                members.append((member, int(level), int(xp)))

        if not members:
            await ctx.send(
                view=_text("Nobody has earned XP yet — a few messages is all it takes.")
            )
            return

        focus = next((row for row in members if row[0].id == ctx.author.id), members[0])
        await ctx.send(
            view=LeaderboardView(
                self, members, focus_id=focus[0].id, invoker=ctx.author.id
            )
        )


class _InspectSelect(discord.ui.Select):
    """Jump to any member on the board without re-running the command."""

    def __init__(self, view: "LeaderboardView", rows: Sequence[tuple]) -> None:
        self.owner = view
        super().__init__(
            placeholder="Inspect a member…",
            options=[
                discord.SelectOption(
                    label=member.display_name[:100],
                    value=str(member.id),
                    description=f"Level {level} · {total_xp(level, xp):,} XP",
                    default=member.id == view.focus_id,
                )
                for member, level, xp in rows[:MAX_SELECT_OPTIONS]
            ],
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.owner.inspect(interaction, int(self.values[0]))


class LeaderboardView(discord.ui.LayoutView):
    """The XP leaderboard with the highlighted member's card underneath."""

    def __init__(
        self,
        cog: Leveling,
        rows: Sequence[tuple],
        *,
        focus_id: int,
        invoker: int,
        timeout: float = 600.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.rows = list(rows)
        self.focus_id = focus_id
        self.invoker = invoker
        self.add_item(self._build())

    def _build(self) -> discord.ui.Container:
        container = discord.ui.Container(accent_color=EMBED_COLOR)

        lines: list[str] = []
        for position, (member, level, xp) in enumerate(self.rows, start=1):
            lines.append(
                f"{position_badge(position)} **{member.display_name}** — "
                f"**level {level}** · {total_xp(level, xp):,} XP"
            )
        container.add_item(
            discord.ui.TextDisplay(
                heading(
                    "🏅",
                    "XP Leaderboard",
                    sub=f"top {len(self.rows)} by XP · pick a name below to inspect",
                )
                + "\n\n"
                + ("\n".join(lines) or "Nobody has earned XP yet — go talk!")
            )
        )

        focus = next((row for row in self.rows if row[0].id == self.focus_id), None)
        if focus is not None:
            member, level, xp = focus
            needed = xp_for_level(level)
            container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
            container.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay(
                        heading(
                            position_badge(position),
                            member.display_name,
                            sub=f"level {level} · {total_xp(level, xp):,} XP lifetime",
                        )
                        + "\n\n"
                        + progress_bar(xp, needed)
                        + f" `{xp:,} / {needed:,} XP`"
                    ),
                    accessory=discord.ui.Thumbnail(media=member.display_avatar.url),
                )
            )

        if self.rows:
            container.add_item(discord.ui.ActionRow(_InspectSelect(self, self.rows)))
        return container

    async def inspect(self, interaction: discord.Interaction, user_id: int) -> None:
        if interaction.user.id != self.invoker:
            await interaction.response.send_message(
                f"Run {PREFIX}levels yourself to browse the board.", ephemeral=True
            )
            return

        member = next((row[0] for row in self.rows if row[0].id == user_id), None)
        if member is None:
            await interaction.response.send_message(
                "That member is no longer on the board.", ephemeral=True
            )
            return

        if not interaction.response.is_done():
            await interaction.response.defer()

        self.focus_id = user_id
        # Rebuild in place so the cached select stays the one Discord dispatches.
        self.clear_items()
        self.add_item(self._build())
        await swap_layout_message(interaction, self)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Leveling(bot))
