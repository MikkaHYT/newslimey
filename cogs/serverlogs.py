"""Server logging: ``,logsetup`` builds a private set of per-topic log channels.

    ,logsetup     create (or reuse) the logs category and its channels
    ,logstatus    show which channels each topic writes to

The category and every channel are hidden from ``@everyone``: only the bot and
whoever ran the setup can see them, so staff roles have to be granted access
deliberately. One channel per topic keeps each feed readable and easy to mute:

    messages     deleted messages (content, author, channel)
    edits        edited messages (before / after)
    deletions    bulk purges
    roles        role creations, edits and deletions
    moderation   timeouts, kicks, bans and unbans
    jails        jails, releases and expired jail time
    anti-mp3     messages Discord removed because they carried an audio file

Every card is kept deliberately small — a title, a line of description and at
most a couple of fields — so a glance is enough and the channel stays scannable.
Channel ids live in the dashboard settings store, so a restart never loses them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands

from config import EMBED_COLOR, PREFIX
from utils.core.views import SimpleLayout

logger = logging.getLogger("slimey.serverlogs")

#: The category the channels are gathered into.
CATEGORY_NAME = "logs"

#: topic key -> (channel name, one-line purpose).
TOPICS: dict[str, tuple[str, str]] = {
    "messages": ("messages", "Deleted messages"),
    "edits": ("edits", "Edited messages"),
    "deletions": ("deletions", "Bulk message purges"),
    "roles": ("roles", "Role creations, edits and deletions"),
    "moderation": ("moderation", "Timeouts, kicks, bans and unbans"),
    "jails": ("jails", "Jails, releases and expired jail time"),
    "antimp3": ("anti-mp3", "Audio files deleted by the filter"),
}

#: ``bot_settings`` keys, per guild, for the category and each channel.
CATEGORY_KEY = "log_category"
CHANNEL_KEY = "log_channel_{topic}"

#: How fresh an audit-log entry has to be to belong to the event we just saw.
AUDIT_WINDOW = 20.0

#: Audio a deleted message may have carried.
AUDIO_EXTS = frozenset({".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".aac"})

SELF_COLOR = EMBED_COLOR
GREEN = discord.Color.from_str("#57f287")
RED = discord.Color.from_str("#ed4245")
ORANGE = discord.Color.from_str("#e67e22")
BLUE = discord.Color.from_str("#5865f2")


def _trunc(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _channel(channel: Any) -> str:
    """``#name`` with its id, for a card footer."""
    if channel is None:
        return "Unknown"
    name = getattr(channel, "name", None) or str(channel)
    return f"#{name}"


def _duration(seconds: int) -> str:
    days, rem = divmod(max(0, seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs and not days:
        parts.append(f"{secs}s")
    return " ".join(parts) or "0s"


def embed(
    title: str,
    color: discord.Color,
    *,
    description: str | None = None,
    author: Any = None,
    avatar: str | None = None,
    fields: list[tuple[str, Any, bool]] | None = None,
    footer: str | None = None,
) -> discord.Embed:
    """A small, consistent log card."""
    card = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))
    if description:
        card.description = _trunc(description, 4096)
    if author:
        card.set_author(name=_trunc(author, 256), icon_url=avatar)
    for name, value, inline in fields or []:
        card.add_field(name=name, value=_trunc(value, 1024) or "—", inline=inline)
    if footer:
        card.set_footer(text=_trunc(footer, 2048))
    return card


def _audio_attachments(message: Any) -> list[Any]:
    found = []
    for item in getattr(message, "attachments", None) or []:
        name = getattr(item, "filename", "") or ""
        kind = (getattr(item, "content_type", "") or "").lower()
        if Path(name).suffix.lower() in AUDIO_EXTS or kind.startswith("audio/"):
            found.append(item)
    return found


class ServerLogs(commands.Cog):
    """Private per-topic logging, driven entirely by Discord's gateway."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    @property
    def settings(self):
        return getattr(self.bot, "dash_settings", None)

    def channel_for(self, guild_id: int, topic: str) -> int | None:
        settings = self.settings
        if settings is None:
            return None
        value = settings.stored(CHANNEL_KEY.format(topic=topic), guild_id=guild_id)
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def category_for(self, guild_id: int) -> int | None:
        settings = self.settings
        if settings is None:
            return None
        value = settings.stored(CATEGORY_KEY, guild_id=guild_id)
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def log_channel_ids(self, guild_id: int) -> set[int]:
        return {
            channel_id
            for topic in TOPICS
            if (channel_id := self.channel_for(guild_id, topic)) is not None
        }

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------
    async def _log(self, guild: discord.Guild, topic: str, card: discord.Embed) -> None:
        if guild is None:
            return
        channel_id = self.channel_for(guild.id, topic)
        if channel_id is None:
            return
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return
        try:
            await channel.send(embed=card)
        except discord.Forbidden:
            logger.debug("No access to the %s log in guild %s", topic, guild.id)
        except discord.HTTPException:
            logger.exception("Could not post the %s log in guild %s", topic, guild.id)

    async def _audit(
        self, guild: discord.Guild, action: Any, target_id: int | None = None
    ) -> Any:
        me = guild.me if guild is not None else None
        if me is None or not me.guild_permissions.view_audit_log:
            return None
        # Discord can write the audit-log entry a moment after the gateway
        # event that announced it, so one immediate lookup often comes back
        # empty (and every card then said "Unknown"). Look, wait, look again.
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(0.6)
            try:
                async for entry in guild.audit_logs(limit=8, action=action):
                    age = (
                        datetime.now(timezone.utc) - entry.created_at
                    ).total_seconds()
                    if age > AUDIT_WINDOW:
                        break
                    if (
                        target_id is not None
                        and getattr(entry.target, "id", None) != target_id
                    ):
                        continue
                    return entry
            except (discord.Forbidden, discord.HTTPException):
                return None
        return None

    #: How long an in-memory "who ran this command" hint stays usable.
    HINT_TTL = 120.0

    def _mod_hint(
        self, guild: discord.Guild | None, target_id: int | None
    ) -> tuple[int, str, float] | None:
        """The moderator recorded by our own commands, if there is a fresh one."""
        if guild is None or target_id is None:
            return None
        hints = getattr(self.bot, "mod_action_hints", None)
        if not hints:
            return None
        key = (guild.id, int(target_id))
        hint = hints.get(key)
        if hint is None:
            return None
        if time.monotonic() - hint[2] > self.HINT_TTL:
            hints.pop(key, None)
            return None
        return hint

    def _actor(
        self,
        entry: Any,
        guild: discord.Guild | None = None,
        target_id: int | None = None,
    ) -> str:
        """Who did it: our command's invoker, or the audit-log actor."""
        user = getattr(entry, "user", None) if entry is not None else None
        hint = self._mod_hint(guild, target_id)
        me = getattr(self.bot, "user", None)
        # When our own command bans/kicks/timeouts someone the audit log can
        # only name the bot. The moderation cog records the invoker, and that
        # is the answer people want.
        if hint is not None and (
            user is None or getattr(user, "id", None) == getattr(me, "id", None)
        ):
            return f"{hint[1]} (`{hint[0]}`)"
        if user is None:
            return "Unknown"
        return f"{getattr(user, 'display_name', user)} (`{user.id}`)"

    async def log_jail(
        self,
        guild: discord.Guild,
        member: Any,
        *,
        moderator: Any,
        reason: str = "No reason provided",
        duration_seconds: int | None = None,
        released: bool = False,
        automatic: bool = False,
    ) -> None:
        """Post a jail or release card to the dedicated ``jails`` channel."""
        if guild is None:
            return
        avatar = getattr(getattr(member, "display_avatar", None), "url", None)
        description = f"{getattr(member, 'mention', member)} (`{getattr(member, 'id', 0)}`)"
        by = (
            f"{getattr(moderator, 'display_name', moderator)} "
            f"(`{getattr(moderator, 'id', 0)}`)"
        )
        fields: list[tuple[str, Any, bool]] = [("By", by, True)]
        if released:
            title, color = ("🔓 Jail Expired" if automatic else "🔓 Member Released"), GREEN
        else:
            title, color = "⛓️ Member Jailed", RED
            if duration_seconds:
                fields.append(("Duration", _duration(duration_seconds), True))
        fields.append(("Reason", reason or "*no reason given*", False))
        if released and automatic:
            fields[0] = ("By", "*jail time expired*", True)
        await self._log(
            guild,
            "jails",
            embed(
                title,
                color,
                description=description,
                avatar=avatar,
                fields=fields,
            ),
        )

    @staticmethod
    def _reason(entry: Any) -> str:
        reason = getattr(entry, "reason", None) if entry is not None else None
        return str(reason) if reason else "*no reason given*"

    def _is_log_channel(self, guild: discord.Guild, channel_id: int) -> bool:
        return channel_id in self.log_channel_ids(guild.id)

    def _is_self(self, user: Any) -> bool:
        me = getattr(self.bot, "user", None)
        return me is not None and getattr(user, "id", None) == me.id

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if message.guild is None or self._is_self(message.author):
            return
        if self._is_log_channel(message.guild, message.channel.id):
            return

        author = getattr(message.author, "display_name", None) or str(message.author)
        avatar = getattr(getattr(message.author, "display_avatar", None), "url", None)
        footer = f"{_channel(message.channel)} · {message.id}"

        audio = _audio_attachments(message)
        if audio:
            # An audio deletion belongs in the anti-mp3 feed rather than the
            # general one, so the two channels stay distinct.
            names = "\n".join(
                f"• `{getattr(item, 'filename', 'file')}` "
                f"({_readable_size(getattr(item, 'size', 0))})"
                for item in audio
            )
            await self._log(
                message.guild,
                "antimp3",
                embed(
                    "🎵 Audio Deleted",
                    RED,
                    description=names,
                    author=author,
                    avatar=avatar,
                    fields=[("Channel", _channel(message.channel), True)],
                    footer=footer,
                ),
            )
            return

        content = (message.content or "").strip()
        card = embed(
            "🗑️ Message Deleted",
            RED,
            description=content or "*no text content*",
            author=author,
            avatar=avatar,
            footer=footer,
        )
        if message.attachments:
            card.add_field(
                name="Attachments",
                value=", ".join(
                    f"`{getattr(a, 'filename', 'file')}`" for a in message.attachments
                ),
                inline=False,
            )
        await self._log(message.guild, "messages", card)

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[discord.Message]) -> None:
        if not messages:
            return
        guild = messages[0].guild
        if guild is None or self._is_log_channel(guild, messages[0].channel.id):
            return

        authors: list[str] = []
        for message in messages:
            name = getattr(message.author, "display_name", None) or str(message.author)
            if name not in authors:
                authors.append(name)
        await self._log(
            guild,
            "deletions",
            embed(
                "🧹 Messages Purged",
                RED,
                description=f"**{len(messages)}** message(s) in {_channel(messages[0].channel)}",
                fields=[("Authors", _trunc(", ".join(authors[:20]), 1024), False)],
            ),
        )

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if before.guild is None or self._is_self(before.author):
            return
        if self._is_log_channel(before.guild, before.channel.id):
            return
        if (before.content or "") == (after.content or ""):
            return

        author = getattr(after.author, "display_name", None) or str(after.author)
        avatar = getattr(getattr(after.author, "display_avatar", None), "url", None)
        card = embed(
            "✏️ Message Edited",
            ORANGE,
            author=author,
            avatar=avatar,
            fields=[
                ("Before", (before.content or "*empty*").strip(), False),
                ("After", (after.content or "*empty*").strip(), False),
            ],
            footer=f"{_channel(after.channel)} · {after.id}",
        )
        if after.jump_url:
            card.url = after.jump_url
        await self._log(before.guild, "edits", card)

    # ------------------------------------------------------------------
    # Roles
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        entry = await self._audit(role.guild, discord.AuditLogAction.role_create, role.id)
        await self._log(
            role.guild,
            "roles",
            embed(
                "🆕 Role Created",
                GREEN,
                description=f"{role.mention} (`{role.id}`)",
                fields=[("By", self._actor(entry, role.guild, role.id), True)],
            ),
        )

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        changes: list[str] = []
        if before.name != after.name:
            changes.append(f"**Name** {before.name} → {after.name}")
        if before.color != after.color:
            changes.append(f"**Color** {before.color} → {after.color}")
        if before.hoist != after.hoist:
            changes.append(f"**Hoisted** {before.hoist} → {after.hoist}")
        if before.mentionable != after.mentionable:
            changes.append(f"**Mentionable** {before.mentionable} → {after.mentionable}")
        if getattr(before, "permissions", None) != getattr(after, "permissions", None):
            changes.append("**Permissions** updated")
        if not changes:
            return

        entry = await self._audit(after.guild, discord.AuditLogAction.role_update, after.id)
        await self._log(
            after.guild,
            "roles",
            embed(
                "🔧 Role Updated",
                ORANGE,
                description=f"{after.mention} (`{after.id}`)",
                fields=[
                    ("Changes", "\n".join(changes), False),
                    ("By", self._actor(entry, after.guild, after.id), True),
                ],
            ),
        )

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        entry = await self._audit(role.guild, discord.AuditLogAction.role_delete, role.id)
        await self._log(
            role.guild,
            "roles",
            embed(
                "🗑️ Role Deleted",
                RED,
                description=f"**{role.name}** (`{role.id}`)",
                fields=[("By", self._actor(entry, role.guild, role.id), True)],
            ),
        )

    # ------------------------------------------------------------------
    # Moderation
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        before_timeout = getattr(before, "timed_out_until", None)
        after_timeout = getattr(after, "timed_out_until", None)
        if before_timeout == after_timeout:
            return

        entry = await self._audit(
            after.guild, discord.AuditLogAction.member_update, after.id
        )
        avatar = getattr(getattr(after, "display_avatar", None), "url", None)
        if after_timeout is not None:
            start = before_timeout or datetime.now(timezone.utc)
            seconds = max(0, int((after_timeout - start).total_seconds()))
            fields = [
                ("Duration", _duration(seconds), True),
                ("Expires", f"<t:{int(after_timeout.timestamp())}:R>", True),
                ("By", self._actor(entry, after.guild, after.id), True),
                ("Reason", self._reason(entry), False),
            ]
            title, color = "⏳ Member Timed Out", RED
        else:
            fields = [
                ("By", self._actor(entry, after.guild, after.id), True),
            ]
            title, color = "✅ Timeout Removed", GREEN

        await self._log(
            after.guild,
            "moderation",
            embed(
                title,
                color,
                description=f"{after.mention} (`{after.id}`)",
                avatar=avatar,
                fields=fields,
            ),
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        entry = await self._audit(member.guild, discord.AuditLogAction.kick, member.id)
        if entry is None:
            return
        avatar = getattr(getattr(member, "display_avatar", None), "url", None)
        await self._log(
            member.guild,
            "moderation",
            embed(
                "👢 Member Kicked",
                RED,
                description=f"{member.mention} (`{member.id}`)",
                avatar=avatar,
                fields=[
                    ("By", self._actor(entry, member.guild, member.id), True),
                    ("Reason", self._reason(entry), False),
                ],
            ),
        )

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: Any) -> None:
        entry = await self._audit(guild, discord.AuditLogAction.ban, getattr(user, "id", None))
        avatar = getattr(getattr(user, "display_avatar", None), "url", None)
        await self._log(
            guild,
            "moderation",
            embed(
                "🔨 Member Banned",
                RED,
                description=f"{user.mention} (`{user.id}`)",
                avatar=avatar,
                fields=[
                    ("By", self._actor(entry, guild, getattr(user, "id", None)), True),
                    ("Reason", self._reason(entry), False),
                ],
            ),
        )

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: Any) -> None:
        entry = await self._audit(guild, discord.AuditLogAction.unban, getattr(user, "id", None))
        avatar = getattr(getattr(user, "display_avatar", None), "url", None)
        await self._log(
            guild,
            "moderation",
            embed(
                "🕊️ Member Unbanned",
                GREEN,
                description=f"{user.mention} (`{user.id}`)",
                avatar=avatar,
                fields=[("By", self._actor(entry, guild, user.id), True)],
            ),
        )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    @commands.command(name="logsetup", aliases=("logs", "serverlogs"))
    @commands.guild_only()
    @commands.check_any(commands.is_owner(), commands.has_permissions(manage_guild=True))
    @commands.bot_has_permissions(manage_channels=True)
    async def logsetup(self, ctx: commands.Context) -> None:
        f"""Create the private logs category and every log channel.

        Usage: `{PREFIX}logsetup` — builds (or reuses) a `logs` category with
        one hidden channel per topic: messages, edits, deletions, roles,
        moderation, jails and anti-mp3. Nobody but me and you can see them, so
        grant any staff roles access afterwards.
        """
        guild = ctx.guild
        if guild is None:
            return

        overwrites: dict[Any, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                embed_links=True,
                attach_files=True,
                read_message_history=True,
                manage_channels=True,
            ),
        }
        if isinstance(ctx.author, discord.Member) and not ctx.author.guild_permissions.administrator:
            overwrites[ctx.author] = discord.PermissionOverwrite(view_channel=True)

        # -- category ------------------------------------------------------
        category: discord.CategoryChannel | None = None
        saved = self.category_for(guild.id)
        if saved is not None:
            existing = guild.get_channel(saved)
            if isinstance(existing, discord.CategoryChannel):
                category = existing
        if category is None:
            category = discord.utils.get(guild.categories, name=CATEGORY_NAME)
        if category is None:
            try:
                category = await guild.create_category(
                    CATEGORY_NAME, overwrites=overwrites, reason="Log setup"
                )
            except discord.HTTPException:
                logger.exception("Could not create the logs category")
                await ctx.send(
                    view=self._card("❌ I couldn't create the **logs** category.", ok=False)
                )
                return

        # -- channels ------------------------------------------------------
        created: list[str] = []
        reused: list[str] = []
        for topic, (name, purpose) in TOPICS.items():
            channel: discord.TextChannel | None = None
            saved_id = self.channel_for(guild.id, topic)
            if saved_id is not None:
                candidate = guild.get_channel(saved_id)
                if isinstance(candidate, discord.TextChannel):
                    channel = candidate
            if channel is None:
                channel = discord.utils.get(category.text_channels, name=name)
            if channel is None:
                try:
                    channel = await category.create_text_channel(
                        name, topic=purpose, reason="Log setup"
                    )
                except discord.HTTPException:
                    logger.exception("Could not create the #%s log channel", name)
                    continue
                created.append(name)
            else:
                reused.append(name)

            settings = self.settings
            if settings is not None:
                await settings.set_value(
                    CHANNEL_KEY.format(topic=topic), channel.id, guild_id=guild.id
                )

        settings = self.settings
        if settings is not None:
            await settings.set_value(CATEGORY_KEY, category.id, guild_id=guild.id)

        lines = [f"# 🗂️ Logging set up", f"Category · {category.mention}"]
        if created:
            lines.append(f"**Created** · {', '.join('#' + n for n in created)}")
        if reused:
            lines.append(f"**Reused** · {', '.join('#' + n for n in reused)}")
        lines.append(
            "-# Hidden from @everyone — grant access to any staff roles that "
            "should read the logs."
        )
        lines.append(
            "-# Give me **View Audit Log** if the `By` lines on moderation and "
            "role cards should name people."
        )
        await ctx.send(view=self._card("\n".join(lines), ok=True))

    @commands.command(name="logstatus", aliases=("logsettings",))
    @commands.guild_only()
    @commands.check_any(commands.is_owner(), commands.has_permissions(manage_guild=True))
    async def logstatus(self, ctx: commands.Context) -> None:
        f"""Show where each log topic writes.

        Usage: `{PREFIX}logstatus`.
        """
        guild = ctx.guild
        if guild is None:
            return
        lines = ["# 🗂️ Logging status"]
        configured = False
        for topic, (name, _purpose) in TOPICS.items():
            channel_id = self.channel_for(guild.id, topic)
            channel = guild.get_channel(channel_id) if channel_id else None
            if channel is not None:
                configured = True
            lines.append(f"> **{name}** · {channel.mention if channel else '*not set*'}")
        if not configured:
            lines.append(f"-# Not set up yet — run `{PREFIX}logsetup`.")
        await ctx.send(view=self._card("\n".join(lines), ok=configured))

    @staticmethod
    def _card(message: str, *, ok: bool) -> SimpleLayout:
        box = discord.ui.Container(
            accent_color=GREEN if ok else discord.Color.red()
        )
        box.add_item(discord.ui.TextDisplay(message))
        return SimpleLayout(box)


def _readable_size(size: int) -> str:
    """Bytes as a short human figure, for the attachment lines."""
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ServerLogs(bot))
