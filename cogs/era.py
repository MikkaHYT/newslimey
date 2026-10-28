"""Browse a whole Juice WRLD era: ``!era outsiders``, ``!era drfl``, ...

Each song the JuiceWRLD API knows about is tagged with an era (``OUT``,
``DRFL``, ``GB&GR`` and so on). This cog lists every song in one of them in a
paginated embed, and accepts the shorthand people actually type — ``drfl``,
``death race``, ``gbgr``, ``goodbye``, ``wod``, ``wrld``, ``outsiders``,
``jw3``.

    !era                 every era and what to type for it
    !era drfl            Death Race For Love
    !era gbgr            Goodbye & Good Riddance
    !era wod             WRLD On Drugs
    !era outsiders       Outsiders (also ``jw3``)
"""

from __future__ import annotations

import asyncio
import logging
import time

import aiohttp
import discord
from discord.ext import commands

from config import EMBED_COLOR
from utils.core.eras import emoji_for_era, resolve_era
from utils.core.paginator import Paginator
from utils.cooldown import unified_cooldown

logger = logging.getLogger("discord.client")

JUICEWRLD_API = "https://juicewrldapi.com"
#: Songs shown per page. Keeps each embed well clear of Discord's 4096-char cap.
PER_PAGE = 18
#: How long a fetched era's song list is reused before it is fetched again.
ERA_TTL = 3600.0


class Era(commands.Cog, name="Era"):
    """Every song in a Juice WRLD era, in one paginated card."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None
        self._eras: list[dict] | None = None
        self._songs: dict[str, tuple[float, list[dict]]] = {}

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession(
            headers={"User-Agent": "SlimeyMusicHub/1.0"}
        )

    async def cog_unload(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def _get(self, path: str, params: dict | None = None):
        if self.session is None:
            return None
        try:
            async with self.session.get(JUICEWRLD_API + path, params=params) as response:
                if response.status != 200:
                    return None
                return await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

    async def fetch_eras(self) -> list[dict]:
        if self._eras is not None:
            return self._eras
        data = await self._get("/juicewrld/eras/", {"page_size": 100})
        self._eras = (data or {}).get("results") or []
        return self._eras

    async def fetch_era_songs(self, era_name: str) -> list[dict]:
        cached = self._songs.get(era_name)
        if cached and time.time() - cached[0] < ERA_TTL:
            return cached[1]

        songs: list[dict] = []
        page = 1
        while True:
            data = await self._get(
                "/juicewrld/songs/",
                {"era": era_name, "page": page, "page_size": 100},
            )
            if data is None:
                break
            results = data.get("results") or []
            songs.extend(results)
            if not results or not data.get("next"):
                break
            page += 1
        self._songs[era_name] = (time.time(), songs)
        return songs

    def build_embeds(
        self, era_name: str, era_description: str, songs: list[dict]
    ) -> list[discord.Embed]:
        emoji = emoji_for_era(self.bot, era_name)
        badge = f"{emoji} " if emoji else ""
        title = f"{badge}{era_name}"
        if era_description:
            title += f" — {era_description}"

        total_pages = max(1, (len(songs) + PER_PAGE - 1) // PER_PAGE)
        embeds: list[discord.Embed] = []
        for index in range(0, len(songs), PER_PAGE):
            chunk = songs[index : index + PER_PAGE]
            lines: list[str] = []
            for song in chunk:
                name = (song.get("name") or "Unknown").strip()
                bits = [
                    str(song.get("leak_type") or song.get("category") or "").strip(),
                    str(song.get("date_leaked") or song.get("release_date") or "").strip(),
                ]
                detail = " · ".join(bit for bit in bits if bit)
                line = f"• **{name}**"
                if detail:
                    line += f"\n-# {detail}"
                lines.append(line)
            embed = discord.Embed(
                title=title if index == 0 else f"{badge}{era_name} (cont.)",
                description="\n".join(lines)[:4096] or "No songs found.",
                color=EMBED_COLOR,
            )
            embed.set_footer(
                text=f"Page {index // PER_PAGE + 1}/{total_pages} · {len(songs)} song(s)"
            )
            embeds.append(embed)
        return embeds

    @commands.hybrid_command(
        name="era",
        aliases=["eras", "findera"],
        description="Show every song from a Juice WRLD era",
    )
    @commands.guild_only()
    @unified_cooldown(10)
    async def era(self, ctx: commands.Context, *, query: str = None) -> None:
        """List a whole era, or every era when no name is given."""
        async with ctx.typing():
            eras = await self.fetch_eras()

        if not query or not query.strip():
            if not eras:
                await ctx.reply("I couldn't reach the era list right now.")
                return
            lines = []
            for era in eras:
                name = str(era.get("name") or "").strip()
                description = str(era.get("description") or "").strip()
                emoji = emoji_for_era(self.bot, name)
                badge = f"{emoji} " if emoji else ""
                line = f"{badge}**{name}**"
                if description:
                    line += f" — {description}"
                lines.append(line)
            embed = discord.Embed(
                title="🎵 Juice WRLD eras",
                description="\n".join(lines)[:4096],
                color=EMBED_COLOR,
            )
            embed.set_footer(
                text="Try !era drfl · !era gbgr · !era wod · !era outsiders"
            )
            await ctx.reply(embed=embed)
            return

        era_name = resolve_era(query, eras)
        if era_name is None:
            await ctx.reply(
                f"I don't know an era called `{query.strip()}`. "
                "Run `!era` to see the list."
            )
            return

        async with ctx.typing():
            songs = await self.fetch_era_songs(era_name)
        if not songs:
            await ctx.reply(f"I couldn't find any songs for the **{era_name}** era.")
            return

        description = next(
            (
                str(era.get("description") or "")
                for era in eras
                if era.get("name") == era_name
            ),
            "",
        )
        embeds = self.build_embeds(era_name, description, songs)
        paginator = Paginator(self.bot, embeds, ctx, invoker=ctx.author.id)
        await paginator.start()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Era(bot))
