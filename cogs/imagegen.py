# cogs/imagegen.py
#
# ,imagegen <prompt> (alias: ,genimage) generates an image from a text prompt
# and posts it in the channel. The same generator backs the AI's generate_image
# tool and the image buttons the embed builder can add.

from __future__ import annotations

import io
import logging

import discord
from discord.ext import commands

from utils.core.images import generate_image
from utils.core.prefixes import prefix_for

logger = logging.getLogger("slimey.imagegen")

#: Longest prompt accepted from the command line.
MAX_PROMPT_LENGTH = 1000


class ImageGen(commands.Cog, name="ImageGen"):
    """Text-to-image generation (Cloudflare Workers AI, then Pollinations)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(
        name="imagegen",
        aliases=["genimage"],
        help="Generate an image from a text prompt.",
    )
    async def imagegen(self, ctx: commands.Context, *, prompt: str | None = None) -> None:
        """Generate an image from a text prompt and post it in the channel.

        Example: `{prefix}imagegen a neon cyberpunk city at night`
        """
        prompt = (prompt or "").strip()
        if not prompt:
            await ctx.reply(
                "❌ Give me a prompt, e.g. "
                f"`{await prefix_for(ctx)}imagegen a cat in space`.",
                mention_author=False,
            )
            return
        if len(prompt) > MAX_PROMPT_LENGTH:
            prompt = prompt[:MAX_PROMPT_LENGTH]

        async with ctx.typing():
            image, error = await generate_image(prompt)

        if image is None:
            await ctx.reply(
                f"❌ I couldn't generate that image: {error}.",
                mention_author=False,
            )
            return

        await ctx.reply(
            file=discord.File(io.BytesIO(image.data), filename=image.filename),
            mention_author=False,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ImageGen(bot))
