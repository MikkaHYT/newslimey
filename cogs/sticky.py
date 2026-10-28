# cogs/sticky.py
#
# ,sticky keeps a chosen message glued to the bottom of a channel. Every time
# someone posts, the previous sticky copy is deleted and a fresh one is sent, so
# it is always the most recent message. The message text lives in the
# ``sticky_messages`` table, so the behaviour survives restarts.

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord
from discord.ext import commands

from utils.core.prefixes import prefix_for
from utils.embeds import Embeds

logger = logging.getLogger("slimey.sticky")

#: Discord's message length ceiling.
MAX_CONTENT = 2000
#: How long to wait after the last message before moving the sticky. Coalesces
#: bursts of messages into a single delete/resend instead of one per message.
DEBOUNCE_SECONDS = 1.5


class Sticky(commands.Cog, name="Sticky"):
    """Keeps a configured message at the bottom of a channel."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        #: Channels with a sticky, mirrored in memory so the on_message hot path
        #: can bail out without a database read.
        self._active: set[int] = set()
        #: Channels waiting out the debounce window.
        self._pending: set[int] = set()
        #: Background debounce tasks, so they can be cancelled on unload.
        self._tracked: set[asyncio.Task] = set()
        self._locks: dict[int, asyncio.Lock] = {}

    async def cog_load(self) -> None:
        try:
            self._active = set(await self.bot.database.get_sticky_channel_ids())
        except Exception:
            logger.exception("Could not load sticky messages")

    def cog_unload(self) -> None:
        for task in list(self._tracked):
            task.cancel()
        self._tracked.clear()
        self._pending.clear()

    # ------------------------------------------------------------------
    # Moving the sticky
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # Bot messages (including the sticky copy itself) never move the sticky,
        # which is what stops a repost from triggering another repost.
        if message.author.bot or message.guild is None:
            return

        channel_id = message.channel.id
        if channel_id not in self._active or channel_id in self._pending:
            return

        self._pending.add(channel_id)
        task = asyncio.create_task(self._debounced_move(channel_id))
        self._tracked.add(task)
        task.add_done_callback(self._tracked.discard)

    async def _debounced_move(self, channel_id: int) -> None:
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
            await self._move_sticky(channel_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Could not move sticky in channel %s", channel_id)
        finally:
            self._pending.discard(channel_id)

    async def _move_sticky(self, channel_id: int) -> None:
        lock = self._locks.setdefault(channel_id, asyncio.Lock())
        async with lock:
            if channel_id not in self._active:
                return

            sticky = await self.bot.database.get_sticky_message(channel_id)
            if sticky is None:
                self._active.discard(channel_id)
                return

            channel = self.bot.get_channel(channel_id)
            if channel is None:
                return

            if sticky.message_id:
                await self._delete_sticky(channel, sticky.message_id)

            try:
                sent = await channel.send(sticky.content)
            except discord.Forbidden:
                logger.warning(
                    "Missing permissions to repost sticky in channel %s", channel_id
                )
                return
            except discord.HTTPException:
                logger.exception("Could not repost sticky in channel %s", channel_id)
                return

            await self.bot.database.update_sticky_message_id(channel_id, sent.id)

    async def _delete_sticky(self, channel, message_id: int) -> None:
        try:
            message = await channel.fetch_message(message_id)
        except discord.HTTPException:
            # Already gone, or we can no longer see the channel.
            return
        try:
            await message.delete()
        except discord.HTTPException:
            logger.debug("Could not delete sticky message %s", message_id)

    @commands.Cog.listener()
    async def on_raw_message_delete(
        self, payload: discord.RawMessageDeleteEvent
    ) -> None:
        """Forget a sticky copy that was deleted by hand, so it is re-sent."""
        if payload.channel_id not in self._active:
            return
        sticky = await self.bot.database.get_sticky_message(payload.channel_id)
        if sticky is not None and sticky.message_id == payload.message_id:
            await self.bot.database.update_sticky_message_id(payload.channel_id, None)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @commands.hybrid_group(
        name="sticky",
        aliases=["stickymessage"],
        description="Keep a message pinned to the bottom of a channel.",
        invoke_without_command=True,
    )
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    @discord.app_commands.default_permissions(manage_messages=True)
    async def sticky(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand:
            return
        prefix = await prefix_for(ctx)
        await Embeds.info(
            ctx,
            "**Sticky messages**\n"
            f"`{prefix}sticky set <message>` — keep a message at the bottom of "
            "this channel\n"
            f"`{prefix}sticky remove` — stop keeping it\n"
            f"`{prefix}sticky list` — show this server's sticky messages",
            delete_after=20,
        )

    @sticky.command(
        name="set",
        aliases=["add"],
        description="Set the sticky message for this channel.",
    )
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def sticky_set(self, ctx: commands.Context, *, message: Optional[str] = None) -> None:
        content = (message or "").strip()
        if not content:
            await Embeds.error(
                ctx,
                "Give me the message to stick: `sticky set <message>`.",
                delete_after=8,
            )
            return
        if len(content) > MAX_CONTENT:
            await Embeds.error(
                ctx,
                f"That message is too long ({len(content)}/{MAX_CONTENT} characters).",
                delete_after=8,
            )
            return

        # Acknowledging first keeps a slash invocation inside Discord's deadline
        # while the old copy is fetched and deleted.
        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.defer(ephemeral=True)

        channel = ctx.channel
        existing = await self.bot.database.get_sticky_message(channel.id)
        if existing is not None and existing.message_id:
            await self._delete_sticky(channel, existing.message_id)

        try:
            sent = await channel.send(content)
        except discord.Forbidden:
            await Embeds.error(
                ctx,
                f"I don't have permission to send messages in {channel.mention}.",
                ephemeral=True,
                delete_after=8,
            )
            return
        except discord.HTTPException:
            logger.exception("Could not post sticky in channel %s", channel.id)
            await Embeds.error(
                ctx, "Discord rejected that message.", ephemeral=True, delete_after=8
            )
            return

        await self.bot.database.set_sticky_message(
            channel.id, ctx.guild.id, content, sent.id
        )
        self._active.add(channel.id)
        await Embeds.success(
            ctx,
            f"Sticky set. It will stay at the bottom of {channel.mention}.",
            ephemeral=True,
            delete_after=8,
        )

    @sticky.command(
        name="remove",
        aliases=["clear", "delete", "off"],
        description="Remove this channel's sticky message.",
    )
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def sticky_remove(self, ctx: commands.Context) -> None:
        existing = await self.bot.database.get_sticky_message(ctx.channel.id)
        if existing is None:
            await Embeds.warning(
                ctx, "There's no sticky set in this channel.", delete_after=8
            )
            return

        if existing.message_id:
            await self._delete_sticky(ctx.channel, existing.message_id)
        await self.bot.database.remove_sticky_message(ctx.channel.id)
        self._active.discard(ctx.channel.id)
        await Embeds.success(
            ctx, "Sticky removed from this channel.", delete_after=8
        )

    @sticky.command(
        name="list",
        aliases=["show", "status"],
        description="List the sticky messages configured in this server.",
    )
    @commands.has_permissions(manage_messages=True)
    async def sticky_list(self, ctx: commands.Context) -> None:
        rows = await self.bot.database.get_guild_stickies(ctx.guild.id)
        if not rows:
            await Embeds.info(
                ctx, "No sticky messages are set in this server.", delete_after=8
            )
            return

        lines = []
        for row in rows:
            channel = ctx.guild.get_channel(row.channel_id)
            where = channel.mention if channel is not None else f"<#{row.channel_id}>"
            preview = " ".join((row.content or "").split())[:60]
            lines.append(f"• {where} — {preview}")
        await Embeds.info(
            ctx,
            "**Sticky messages**\n" + "\n".join(lines),
            delete_after=20,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Sticky(bot))
