# cogs/snipe.py
#
# ,snipe (alias ,s)       -- show the most recently deleted message, or ,s 2 for
#                            the second most recent, etc.
# ,editsnipe (alias ,es)  -- show what a message said before it was edited, or
#                            ,es 2 for the one before that.
# ,sniperesend (alias ,srs) -- post a sniped message back into the channel
#                            through a webhook, dressed as the member who wrote it.
# ,clearsnipe (alias ,cs) -- wipe the stored snipe history for the channel.
#
# This is the bot's only snipe implementation — the copies of ``snipe``,
# ``editsnipe`` and ``clearsnipe`` that lived in the General cog were removed.

from __future__ import annotations

import logging
from collections import defaultdict, deque
from typing import Deque

import discord
from discord.ext import commands

from cogs.fakemsg import _bot_admin_or_manage_webhooks

try:
    from config import EMBED_COLOR
except ImportError:  # pragma: no cover - config always ships with the bot
    EMBED_COLOR = discord.Color.from_str("#2b2d31")

logger = logging.getLogger(__name__)

# How many deleted messages to remember per channel.
SNIPE_HISTORY = 25
MAX_CONTENT_DISPLAY = 1500

# A webhook post is one message, so the resend has the same ceilings a normal
# message has: 2000 characters of text and a limited payload of files.
MAX_CONTENT_RESEND = 2000
MAX_RESEND_FILES = 10
MAX_RESEND_BYTES = 8 * 1024 * 1024


def _separator() -> discord.ui.Separator:
    return discord.ui.Separator(spacing=discord.SeparatorSpacing.small)


def _clip(text: str) -> str:
    """Trim a stored body of text to something a card can hold."""
    if len(text) > MAX_CONTENT_DISPLAY:
        return text[:MAX_CONTENT_DISPLAY].rstrip() + "…"
    return text


class Snipe(commands.Cog):
    """Keeps a short, per-channel history of deleted messages."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._history: dict[int, Deque[dict]] = defaultdict(
            lambda: deque(maxlen=SNIPE_HISTORY)
        )
        #: Edits are kept apart so ``,es`` doesn't page through deletions.
        self._edits: dict[int, Deque[dict]] = defaultdict(
            lambda: deque(maxlen=SNIPE_HISTORY)
        )

    def _record(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        # The bot's own self-deleting status messages would just be noise.
        if self.bot.user is not None and message.author.id == self.bot.user.id:
            return

        content = message.content or ""
        attachments = list(message.attachments)
        stickers = [sticker.name for sticker in message.stickers]
        if not content and not attachments and not stickers and not message.embeds:
            return

        history = self._history[message.channel.id]
        if any(entry["message_id"] == message.id for entry in history):
            return

        author = message.author
        history.appendleft(
            {
                "message_id": message.id,
                "author_id": author.id,
                "author_name": author.display_name,
                # Kept so `,sniperesend` can dress a webhook as the author even
                # if they leave the server before the snipe is resent.
                "author_avatar": getattr(
                    getattr(author, "display_avatar", None), "url", None
                ),
                "content": content,
                "attachments": [attachment.url for attachment in attachments],
                # The live objects are only used to re-upload files on resend.
                "attachment_objects": attachments,
                "stickers": stickers,
                "created_at": message.created_at,
            }
        )

    def _record_edit(self, before: discord.Message, after: discord.Message) -> None:
        """Remember what a message said before it was rewritten, newest first."""
        if before.guild is None or before.author.bot:
            return
        if self.bot.user is not None and before.author.id == self.bot.user.id:
            return
        # Discord fires an edit when a link preview finishes embedding, with the
        # text untouched — only a real rewrite is worth remembering.
        if (before.content or "") == (after.content or ""):
            return

        self._edits[before.channel.id].appendleft(
            {
                "message_id": before.id,
                "author_name": before.author.display_name,
                "before": before.content or "",
                "after": after.content or "",
                "edited_at": after.edited_at or discord.utils.utcnow(),
            }
        )

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        self._record(message)

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[discord.Message]) -> None:
        for message in messages:
            self._record(message)

    @commands.Cog.listener()
    async def on_message_edit(
        self, before: discord.Message, after: discord.Message
    ) -> None:
        self._record_edit(before, after)

    @commands.command(name="snipe", aliases=("s",))
    async def snipe(self, ctx: commands.Context, index: int = 1) -> None:
        """Show the most recently deleted message (or the nth most recent)."""
        history = self._history.get(ctx.channel.id)
        if not history:
            return await ctx.error("There's nothing to snipe in this channel.")

        if index < 1:
            return await ctx.error("Please provide a number of `1` or higher.")
        if index > len(history):
            return await ctx.error(
                f"There have only been `{len(history)}` sniped message(s) in this channel."
            )

        entry = history[index - 1]

        container = discord.ui.Container(accent_color=EMBED_COLOR)
        header = "🎯 **Sniped Message**"
        if len(history) > 1:
            header += f" `{index}/{len(history)}`"
        container.add_item(discord.ui.TextDisplay(header))

        meta = (
            f"**{discord.utils.escape_markdown(entry['author_name'])}** · "
            f"{discord.utils.format_dt(entry['created_at'], 'R')}"
        )
        container.add_item(_separator())
        container.add_item(discord.ui.TextDisplay(meta))

        content = entry["content"] or "*No text content*"
        if len(content) > MAX_CONTENT_DISPLAY:
            content = content[:MAX_CONTENT_DISPLAY].rstrip() + "…"
        container.add_item(discord.ui.TextDisplay(content))

        extras: list[str] = [
            f"[Attachment {position}]({url})"
            for position, url in enumerate(entry["attachments"], start=1)
        ]
        extras.extend(f"-# Sticker: {sticker}" for sticker in entry["stickers"])
        if extras:
            container.add_item(_separator())
            container.add_item(discord.ui.TextDisplay("\n".join(extras)))

        view = discord.ui.LayoutView(timeout=180)
        view.add_item(container)
        await ctx.reply(view=view)

    @commands.command(name="editsnipe", aliases=("es", "snipeedit"))
    async def editsnipe(self, ctx: commands.Context, index: int = 1) -> None:
        """Show what a message said before it was edited (or the nth most recent)."""
        history = self._edits.get(ctx.channel.id)
        if not history:
            return await ctx.error("There's nothing to snipe in this channel.")

        if index < 1:
            return await ctx.error("Please provide a number of `1` or higher.")
        if index > len(history):
            return await ctx.error(
                f"There have only been `{len(history)}` sniped edit(s) in this channel."
            )

        entry = history[index - 1]

        container = discord.ui.Container(accent_color=EMBED_COLOR)
        header = "🎯 **Sniped Edit**"
        if len(history) > 1:
            header += f" `{index}/{len(history)}`"
        container.add_item(discord.ui.TextDisplay(header))

        meta = (
            f"**{discord.utils.escape_markdown(entry['author_name'])}** · "
            f"{discord.utils.format_dt(entry['edited_at'], 'R')}"
        )
        container.add_item(_separator())
        container.add_item(discord.ui.TextDisplay(meta))

        container.add_item(
            discord.ui.TextDisplay(f"**Before**\n{_clip(entry['before']) or '*nothing*'}")
        )
        container.add_item(
            discord.ui.TextDisplay(f"**After**\n{_clip(entry['after']) or '*nothing*'}")
        )

        view = discord.ui.LayoutView(timeout=180)
        view.add_item(container)
        await ctx.reply(view=view)

    @commands.command(name="sniperesend", aliases=("srs", "resendsnipe"))
    @_bot_admin_or_manage_webhooks()
    @commands.bot_has_permissions(manage_webhooks=True)
    async def sniperesend(self, ctx: commands.Context, index: int = 1) -> None:
        """Post a sniped message back into the channel as its original author."""
        history = self._history.get(ctx.channel.id)
        if not history:
            return await ctx.error("There's nothing to snipe in this channel.")

        if index < 1:
            return await ctx.error("Please provide a number of `1` or higher.")
        if index > len(history):
            return await ctx.error(
                f"There have only been `{len(history)}` sniped message(s) in this channel."
            )

        entry = history[index - 1]

        # Prefer the live member (fresh name/avatar); fall back to what was
        # stored when the message was deleted if they've left the server.
        member = None
        author_id = entry.get("author_id")
        if author_id is not None and ctx.guild is not None:
            member = ctx.guild.get_member(author_id)
            if member is None:
                try:
                    member = await ctx.guild.fetch_member(author_id)
                except discord.HTTPException:
                    member = None

        if member is not None:
            username = member.display_name
            avatar_url = member.display_avatar.url
        else:
            username = entry["author_name"]
            avatar_url = entry.get("author_avatar")

        # Re-upload the files while the CDN links are still warm; anything that
        # can't be fetched (or would blow the payload limit) becomes a link.
        files: list[discord.File] = []
        lost: list[str] = []
        budget = MAX_RESEND_BYTES
        for attachment in entry.get("attachment_objects") or []:
            if len(files) >= MAX_RESEND_FILES or attachment.size > budget:
                lost.append(f"[{attachment.filename}]({attachment.url})")
                continue
            try:
                files.append(await attachment.to_file())
            except (discord.HTTPException, OSError):
                lost.append(f"[{attachment.filename}]({attachment.url})")
            else:
                budget -= attachment.size

        body = (entry["content"] or "").strip()
        if lost:
            body = "\n".join(part for part in (body, "\n".join(lost)) if part)
        if not body and not files:
            return await ctx.error(
                "That sniped message has no text or files left to resend."
            )
        if len(body) > MAX_CONTENT_RESEND:
            body = body[: MAX_CONTENT_RESEND - 1].rstrip() + "…"

        # The trigger message only exists for prefix invocations.
        if ctx.message is not None:
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass

        # A thread can't own a webhook (discord.py only puts `create_webhook`
        # on guild channels), so one on the parent posts into the thread.
        thread = None
        target = ctx.channel
        if not hasattr(target, "create_webhook"):
            parent = getattr(target, "parent", None)
            if parent is not None and hasattr(parent, "create_webhook"):
                thread, target = target, parent

        webhooks = await target.webhooks()
        webhook = discord.utils.get(webhooks, user=self.bot.user)
        if webhook is None:
            webhook = await target.create_webhook(name="Snipe Resend Webhook")

        payload: dict = {
            "content": body,
            "username": username,
            "files": files,
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if avatar_url:
            payload["avatar_url"] = avatar_url
        if thread is not None:
            payload["thread"] = thread
        await webhook.send(**payload)

    @sniperesend.error
    async def sniperesend_error(
        self, ctx: commands.Context, error: Exception
    ) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.error(
                f"Usage: `{ctx.prefix or '!'}sniperesend [position]` — try `1`."
            )
        elif isinstance(
            error, (commands.MissingPermissions, commands.CheckAnyFailure)
        ):
            await ctx.error(
                "You need **Manage Webhooks** in this server to resend a snipe."
            )
        elif isinstance(error, commands.BotMissingPermissions):
            await ctx.error("I need **Manage Webhooks** to do that.")
        elif isinstance(error, discord.Forbidden):
            await ctx.error(
                "I couldn't post here — check that I still have **Manage Webhooks**."
            )
        else:
            logger.exception("sniperesend failed", exc_info=error)
            await ctx.error("I couldn't resend that message.")

    @commands.command(name="clearsnipe", aliases=("cs", "clearsnipes"))
    async def clearsnipe(self, ctx: commands.Context) -> None:
        """Clear the stored snipe history for this channel."""
        deleted = self._history.pop(ctx.channel.id, None)
        edits = self._edits.pop(ctx.channel.id, None)
        await ctx.success(
            f"Cleared `{len(deleted) if deleted else 0}` sniped message(s) and "
            f"`{len(edits) if edits else 0}` sniped edit(s) from this channel."
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Snipe(bot))
