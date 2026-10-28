# cogs/say.py

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from config import EMBED_COLOR
from utils.core.prefixes import prefix_for

logger = logging.getLogger(__name__)


def simple_view(content: str, *, timeout: int = 60) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=timeout)
    container = discord.ui.Container(accent_color=EMBED_COLOR)
    container.add_item(discord.ui.TextDisplay(content))
    view.add_item(container)
    return view


class Say(commands.Cog):
    """Utility say command."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def send_error(self, ctx: commands.Context, message: str) -> None:
        await ctx.send(view=simple_view(message))

    @commands.command(
        name="say",
        aliases=["echo"],
        description="Repeats the given text as the bot.",
        help="Deletes your message and sends the given text as the bot.",
        extras={"example": "say Hello everyone!"},
    )
    async def say(self, ctx: commands.Context, *, text: str | None = None) -> None:
        if not text:
            prefix = await prefix_for(ctx)
            help_view = simple_view(
                "# Command: say\n\n"
                "**Syntax**\n"
                f"`{prefix}say <text>`\n\n"
                "**Example**\n"
                f"`{prefix}say Hello everyone!`"
            )
            await ctx.send(view=help_view)
            return

        # Send the repeated message first so we never lose the text if the
        # delete fails or we lack permissions for it.
        try:
            await ctx.send(
                text,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, users=True
                ),
            )
        except discord.HTTPException:
            logger.exception("Discord rejected the say message")
            await self.send_error(ctx, "❌ Discord rejected that message.")
            return

        try:
            await ctx.message.delete()
        except discord.Forbidden:
            # Missing Manage Messages permission - not fatal, just leave it.
            pass
        except discord.HTTPException:
            logger.exception("Failed to delete the original say message")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Say(bot))
