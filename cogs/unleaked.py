"""Browse every song in the ``unsurfaced`` category: ``!unleaked``.

Unsurfaced material exists on the JuiceWRLD API as metadata only — there is no
audio behind it — but people still want the full list. This cog asks the API for
just that category (``/juicewrld/songs/?category=unsurfaced``), walks every
page, and renders the result with the shared paginated-embed browser used by
``!stems`` / ``!remasters`` / ``!freestyles``.

    !unleaked        every unsurfaced (unleaked) song, one page at a time
"""

from __future__ import annotations

import asyncio
import logging
import time

import aiohttp
from discord.ext import commands

from utils.core.filebrowse import send_file_browser
from utils.cooldown import unified_cooldown

logger = logging.getLogger("discord.client")

JUICEWRLD_API = "https://juicewrldapi.com"
#: The API category filter that returns only unsurfaced songs.
UNSURFACED_CATEGORY = "unsurfaced"
#: Rows requested per API page while walking the catalogue.
FETCH_PAGE_SIZE = 100
#: How long the assembled list is reused before it is fetched again.
CACHE_TTL = 1800.0


class Unleaked(commands.Cog, name="Unleaked"):
    """Every unsurfaced (as-yet unleaked) song, in one paginated card."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None
        self._cache: tuple[float, list[dict]] | None = None

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

    async def fetch_unsurfaced(self, *, force: bool = False) -> list[dict]:
        """Every song the API tags ``unsurfaced``, cached for a little while."""
        if not force and self._cache and time.time() - self._cache[0] < CACHE_TTL:
            return self._cache[1]

        songs: list[dict] = []
        page = 1
        while True:
            data = await self._get(
                "/juicewrld/songs/",
                {
                    "category": UNSURFACED_CATEGORY,
                    "page": page,
                    "page_size": FETCH_PAGE_SIZE,
                },
            )
            if data is None:
                break
            results = data.get("results") or []
            songs.extend(results)
            if not results or not data.get("next"):
                break
            page += 1

        if songs:
            self._cache = (time.time(), songs)
        return songs

    @staticmethod
    def _row(song: dict) -> dict:
        """Map one API song onto a row the shared browser understands."""
        era = song.get("era")
        if isinstance(era, dict):
            era_label = era.get("description") or era.get("name")
        else:
            era_label = era
        return {
            "title": song.get("name") or "Unknown",
            "era": era_label,
            "length": song.get("length") or None,
            "artist": song.get("credited_artists") or None,
        }

    @commands.hybrid_command(
        name="unleaked",
        aliases=["unsurfaced"],
        description="Show every song in the unsurfaced (unleaked) category",
    )
    @commands.guild_only()
    @unified_cooldown(10)
    async def unleaked(self, ctx: commands.Context) -> None:
        """List every unsurfaced song across the whole catalogue."""
        async with ctx.typing():
            songs = await self.fetch_unsurfaced()
            if not songs:
                songs = await self.fetch_unsurfaced(force=True)

        if not songs:
            await ctx.reply(
                "I couldn't reach the unsurfaced list right now. Try again soon."
            )
            return

        rows = [self._row(song) for song in songs if song.get("name")]
        await send_file_browser(
            ctx,
            rows,
            title="🕳️ Unleaked — Unsurfaced",
            empty_message="No unsurfaced songs found.",
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Unleaked(bot))
