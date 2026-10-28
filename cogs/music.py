import os
import json
import time
import discord
import logging
import aiohttp
import asyncio
from io import BytesIO
from typing import Optional
from zoneinfo import ZoneInfo
from colorthief import ColorThief
from discord.ext import commands, tasks
from discord.ext.commands import Context

from utils.core.prefixes import render
from datetime import datetime, timedelta, timezone
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from urllib.parse import quote

from humanfriendly.terminal import usage
from utils.cache import Cache
from utils.cooldown import unified_cooldown
from utils.core import cache_registry, mp3_buttons
from utils.core.cache_registry import CacheProvider, ReloadResult
from utils.core.economy import format_cash
from utils.core.eras import emoji_for_era, emoji_for_song
from utils.core.groupbuys import (
    CATALOG as GROUPBUY_CATALOG,
    GROUPBUY_SHEET_ID,
    Groupbuy,
    format_price,
    normalise_song_key,
)
from utils.embeds import Embeds
from utils.guardrails import check_slash_guardrails
from cogs.songinfo import SongInfoAPI
from itertools import product
from difflib import SequenceMatcher
from moviepy import *
import re
import random
from moviepy import AudioFileClip, ImageClip

logger = logging.getLogger("discord.client")


JUICEWRLD_API = "https://juicewrldapi.com"

#: Storage resolver shared with the songinfo cog. Used as a fallback when that
#: cog isn't loaded, so the leak card can still find original files.
_FALLBACK_FILE_API: SongInfoAPI | None = None


def _fallback_file_api() -> SongInfoAPI:
    global _FALLBACK_FILE_API
    if _FALLBACK_FILE_API is None:
        _FALLBACK_FILE_API = SongInfoAPI()
    return _FALLBACK_FILE_API

DOWNLOAD_CACHE_FOLDER_NAME = "__download_cache"
HEARDLE_GAME_DURATION = 20
HEARDLE_CLIP_DURATION = 10
#: Cash paid from the treasury to whoever guesses a round of Heardle first.
HEARDLE_PRIZE = 2000
#: How long players have to react with ✅ to join a Heardle tournament.
HEARDLE_TOURNAMENT_JOIN_DURATION = 30
#: Upper bound so a tournament can't run indefinitely.
HEARDLE_TOURNAMENT_MAX_ROUNDS = 20
HEARDLE_TOURNAMENT_MIN_PLAYERS = 2
DEFAULT_SNIPPET_DURATION = 15
LASTFM_API_KEY = os.getenv("LASTFM_API_KEY")
MAX_NAME_TRANSFORMATIONS = 6
GENIUS_API_TOKEN = os.getenv("GENIUS_API_KEY")
#: The songs endpoint pages 20 at a time by default (100 max). A search that
#: matches a common word can rank the intended track well past the first page
#: — e.g. searching "Time" returns "Charcoal" at position 32 — so ask for the
#: full page, then rank the results client-side (including alternate names).
SEARCH_PAGE_SIZE = 100

#: Reverse-lyrics search (``!lyricfind``). Every song record carries its own
#: lyrics, but the API's ``search`` parameter only looks at titles — so the
#: full corpus is swept once into a local cache and searched there.
LYRICS_INDEX_PATH = "data/lyrics_index.json"
LYRICS_INDEX_TTL = 24 * 60 * 60
#: Lines shorter than this are too common to be a useful lyric "fingerprint".
LYRICS_INDEX_MIN_PHRASE = 4

#: How many `lf` subcommands one index page shows. The group carries sixty-odd
#: entries once cogs.lastfm attaches its own, so the index is paged instead of
#: being squeezed into a single embed description.
LF_HELP_PER_PAGE = 10


class LastFMHelpView(discord.ui.View):
    """Pages through the `lf` subcommand index, ten entries at a time."""

    def __init__(self, embeds: list[discord.Embed], *, author_id: int):
        super().__init__(timeout=120)
        self.embeds = embeds
        self.author_id = author_id
        self.current = 0

        self.prev_button = discord.ui.Button(emoji="⬅", style=discord.ButtonStyle.gray)
        self.next_button = discord.ui.Button(emoji="➡", style=discord.ButtonStyle.gray)
        self.prev_button.callback = self.go_back
        self.next_button.callback = self.go_forward

        if len(embeds) > 1:
            self.add_item(self.prev_button)
            self.add_item(self.next_button)
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.prev_button.disabled = self.current <= 0
        self.next_button.disabled = self.current >= len(self.embeds) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "That index belongs to someone else — run `lf` for your own.",
                ephemeral=True,
            )
            return False
        return True

    async def go_back(self, interaction: discord.Interaction) -> None:
        self.current = max(0, self.current - 1)
        self._update_buttons()
        await interaction.response.edit_message(
            embed=self.embeds[self.current], view=self
        )

    async def go_forward(self, interaction: discord.Interaction) -> None:
        self.current = min(len(self.embeds) - 1, self.current + 1)
        self._update_buttons()
        await interaction.response.edit_message(
            embed=self.embeds[self.current], view=self
        )

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True


def build_lastfm_help_pages(
    subcmds, prefix: str, group: str
) -> list[discord.Embed]:
    """Build the paged index of ``lf`` subcommands, ten per embed.

    ``subcmds`` is whatever ends up on the group (Discord's ``Command`` s or the
    prefix-only commands :mod:`cogs.lastfm` attaches), so only the attributes
    shared by both are read.
    """
    chunk = max(1, LF_HELP_PER_PAGE)
    total_pages = max(1, (len(subcmds) + chunk - 1) // chunk)
    pages: list[discord.Embed] = []

    for index in range(0, len(subcmds), chunk):
        page_number = index // chunk + 1
        embed = discord.Embed(
            title="Last.fm — Available Commands",
            description=(
                f"`{prefix}{group} <subcommand>` for details · "
                f"page {page_number} of {total_pages}"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Use {prefix}{group} <subcommand> for details.")

        for cmd in subcmds[index : index + chunk]:
            aliases = (
                f" (or: {', '.join(cmd.aliases)})"
                if getattr(cmd, "aliases", None)
                else ""
            )
            desc = render(
                getattr(cmd, "short_doc", None)
                or getattr(cmd, "description", "")
                or "No description available.",
                prefix,
            ).strip()
            if len(desc) > 200:
                desc = desc[:197].rstrip() + "…"
            embed.add_field(
                name=f"`{prefix}{group} {cmd.name}`{aliases}",
                value=f"↳ {desc}",
                inline=False,
            )

        pages.append(embed)

    return pages


class Music(commands.Cog, name="Music"):
    async def cog_load(self) -> None:
        cache_registry.register(
            CacheProvider(
                name="lyrics",
                label="Lyric search index",
                aliases=("lyric", "lyricfind", "lyrics_index"),
                describe=self._describe_lyrics_cache,
                reload=self._reload_lyrics_cache,
            )
        )
        # The lyric corpus takes a while to sweep, so warm it in the background
        # rather than making the first `!lyricfind` wait on it.
        self._lyrics_warm_task = asyncio.create_task(self._warm_lyrics_index())

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session = aiohttp.ClientSession()
        self.default_avatar_url = "https://cdn.discordapp.com/embed/avatars/1.png"
        self.font_path = self._resolve_font_path("DejaVuSans-ExtraLight.ttf")
        self.default_font = ImageFont.truetype(self.font_path, 24)
        self.font_small = ImageFont.truetype(self.font_path, 20)
        self.font_large = ImageFont.truetype(self.font_path, 40)

        self.latest_surfaces = []
        self.cache_songs.start()
        self.is_blacktea_synced = False

        self.cover_thumb_cache: dict[str, bytes] = {}
        self._cover_inflight: dict[str, asyncio.Task] = {}
        self._cover_bg_tasks: set[asyncio.Task] = set()
        self.COVER_THUMB_SIZE = 1024

        self._lyrics_cache: list[dict] = []
        self._lyrics_ready = False
        self._lyrics_index_lock = asyncio.Lock()
        self._groupbuy_api_index_cache: dict[str, dict] | None = None

        self.valid_names = set()
        self.producer_counts = {}
        self.ongoing_blacktea = []
        self.ongoing_higherlower = []
        self.ongoing_heardle = []
        self.heardle_answers = {}
        self.snippet_debounce = {}
        self._auto_cache_clean.start()
        self.standard_colors = {
            "black": "#000000",
            "white": "#FFFFFF",
            "red": "#FF0000",
            "green": "#008000",
            "blue": "#0000FF",
            "yellow": "#FFFF00",
            "cyan": "#00FFFF",
            "magenta": "#FF00FF",
            "gray": "#808080",
            "purple": "#800080",
            "orange": "#FFA500",
            "pink": "#FFC0CB",
        }
        self.testing_ids = [
            1095747082599530627,  
            1099696209637167145,  
        ]
        self.ALBUMS = {
            "jute": {"name": "JUICED UP THE EP", "color": "#FFE602"},
            "LND": {"name": "Legends Never Die", "color": "#F700FF"},
            "afflictions": {"name": "affliction", "color": "#000000"},
            "bdm": {"name": "BINGEDRINKINGMUSIC", "color": "#000000"},
            "HIH 999": {
                "name": "Heartbroken In Hollywood 9 9 9",
                "color": "#FF653E",
            },
            "jw 999": {"name": "JuiceWRLD 9 9 9", "color": "#FF2C2C"},
            "ND": {"name": "NOTHINGS DIFFERENT </3", "color": "#FF8800"},
            "GB&GR": {"name": "Goodbye & Good Riddance", "color": "#008CFF"},
            "GB&GR (AE)": {
                "name": "Goodbye & Good Riddance (Anniversary Edition)",
                "color": "#008CFF",
            },
            "GB&GR (5YAE)": {
                "name": "Goodbye & Good Riddance (5 Year Anniversary Edition)",
                "color": "#008CFF",
            },
            "WOD": {"name": "WRLD ON DRUGS", "color": "#00FF94"},
            "DRFL": {"name": "Death Race For Love", "color": "#FF9900"},
            "DRFL (BTV)": {
                "name": "Death Race For Love (Bonus Track Version)",
                "color": "#FF9900",
            },
            "OUT": {"name": "Outsiders", "color": "#2B2B2B"},
            "POST": {"name": "Posthumous", "color": "#00CCFF"},
            "TPP": {"name": "The Pre-Party", "color": "#EA00FF"},
            "TPP (EE)": {
                "name": "The Pre-Party (Extended Edition)",
                "color": "#EA00FF",
            },
            "FD": {"name": "Fighting Demons", "color": "#2E2E2E"},
            "FD (CE)": {
                "name": "Fighting Demons (Complete Edition)",
                "color": "#2E2E2E",
            },
            "FD (EE)": {
                "name": "Fighting Demons (Extended Edition)",
                "color": "#2E2E2E",
            },
            "FD (DDE)": {
                "name": "Fighting Demons (Digital Deluxe Edition)",
                "color": "#2E2E2E",
            },
            "TPNE": {"name": "The Party Never Ends", "color": "#CC00FF"},
        }

        punctuation_rules = [
            (r"\(.*?\)", [(r"\(.*?\)", "")]),
            (r"\[.*?\]", [(r"\[.*?\]", "")]),
            (r"\{.*?\}", [(r"\{.*?\}", "")]),
            (r"'", [(r"'", "")]),
            (r"\.", [(r"\.", ""), (r"\.", " ")]),
            (r",", [(r",", ""), (r",", " ")]),
            (r"\?", [(r"\?", ""), (r"\?", " ")]),
            (r"-", [(r"-", ""), (r"-", " ")]),
        ]

        self.track_name_transformations = {}
        for detect, subs in punctuation_rules:
            detect_re = re.compile(detect)
            self.track_name_transformations[
                lambda s, dr=detect_re: bool(dr.search(s))
            ] = [
                lambda s, sr=re.compile(search), repl=repl: sr.sub(repl, s).strip()
                for search, repl in subs
            ]

    @staticmethod
    def _resolve_font_path(filename: str) -> str:
        """Return a usable path for a TrueType font, checking CWD then system dirs."""
        if os.path.exists(filename):
            return filename
        system_candidates = [
            f"/usr/share/fonts/truetype/dejavu/{filename}",
            f"/usr/share/fonts/truetype/{filename}",
            f"/usr/share/fonts/{filename}",
            f"/usr/local/share/fonts/{filename}",
        ]
        for path in system_candidates:
            if os.path.exists(path):
                return path
        raise FileNotFoundError(f"Font file not found: {filename}")

    def dynamic_font(self, text: str, max_width: int, font_path: str, max_font_size: int):
        """Return a PIL ImageFont that fits `text` within `max_width`."""
        if not text:
            return ImageFont.truetype(font_path, max_font_size)
        try:
            test_font = ImageFont.truetype(font_path, max_font_size)
            if test_font.getlength(text) <= max_width:
                return test_font
        except Exception:
            pass
        for size in range(max_font_size, 8, -1):
            try:
                font = ImageFont.truetype(font_path, size)
                if font.getlength(text) <= max_width:
                    return font
            except Exception:
                continue
        return ImageFont.truetype(font_path, 8)

    async def cog_unload(self):
        cache_registry.unregister("lyrics")
        self._auto_cache_clean.cancel()
        for task in self._cover_bg_tasks:
            task.cancel()
        self._cover_bg_tasks.clear()
        await self.session.close()
        if _FALLBACK_FILE_API is not None:
            await _FALLBACK_FILE_API.close()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Delegate slash guardrails to the bot's centralized check."""
        return await check_slash_guardrails(self, interaction)

    # ------------------------------------------------------------------
    # Cover art helpers
    # ------------------------------------------------------------------

    def _cover_spawn(self, coro):
        task = asyncio.create_task(coro)
        self._cover_bg_tasks.add(task)
        task.add_done_callback(self._cover_bg_tasks.discard)

    @staticmethod
    def _cover_resize(raw: bytes, size: int) -> bytes:
        with Image.open(BytesIO(raw)) as im:
            im.draft("RGB", (size, size))
            im = im.convert("RGB")
            im.thumbnail((size, size))
            out = BytesIO()
            im.save(out, format="JPEG", quality=85)
            return out.getvalue()

    async def fetch_cover_thumb(self, url: str):
        """GET a cover, downscale it, and cache the bytes, returns None on failure."""
        cached = self.cover_thumb_cache.get(url)
        if cached is not None:
            return cached
        if url not in self._cover_inflight:
            self._cover_inflight[url] = asyncio.ensure_future(self._download_cover_thumb(url))
        try:
            return await self._cover_inflight[url]
        except Exception:
            return None

    async def _download_cover_thumb(self, url: str):
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    return None
                raw = await resp.read()
            data = await asyncio.to_thread(self._cover_resize, raw, self.COVER_THUMB_SIZE)
            self.cover_thumb_cache[url] = data
            return data
        except Exception:
            return None
        finally:
            self._cover_inflight.pop(url, None)

    async def prefetch_cover_thumbs(self, urls):
        sem = asyncio.Semaphore(6)

        async def warm(u):
            async with sem:
                await self.fetch_cover_thumb(u)

        await asyncio.gather(*[warm(u) for u in urls], return_exceptions=True)

    # The ``cover`` / ``covers`` command now lives in the ported
    # :mod:`cogs.covers` cog, which is the reference bot's richer gallery
    # implementation.

    def can_test(ctx: commands.Context, cog=None):
        if cog is None:
            cog = ctx.cog

        return ctx.author.id in cog.testing_ids

    def assert_download_cache(self):
        if not os.path.exists(DOWNLOAD_CACHE_FOLDER_NAME):
            os.makedirs(DOWNLOAD_CACHE_FOLDER_NAME)

    @tasks.loop(hours=6)
    async def _auto_cache_clean(self):
        """Purge old __download_cache files automatically every 6 hours."""
        await asyncio.to_thread(self._clean_download_cache)

    def _clean_download_cache(self, max_age_hours: float = 1.0):
        """Delete files in the download cache older than `max_age_hours`.

        Defaults to 1 hour so temp mp3/mp4/png files don't accumulate.
        """
        self.assert_download_cache()
        now = datetime.now()
        cutoff = timedelta(hours=max_age_hours)
        removed = 0
        for file in os.listdir(DOWNLOAD_CACHE_FOLDER_NAME):
            file_path = os.path.join(DOWNLOAD_CACHE_FOLDER_NAME, file)
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(file_path))
                if now - mtime > cutoff:
                    os.remove(file_path)
                    removed += 1
            except Exception as e:
                logger.error(f"Error cleaning cache file {file_path}: {e}")
        if removed:
            logger.info(f"Auto-cleaned {removed} stale file(s) from {DOWNLOAD_CACHE_FOLDER_NAME}")

    @staticmethod
    def _blur_to_file(image: Image.Image, target_path: str, radius: int) -> bool:
        """Blur an already-decoded image and save to disk. Returns True on success."""
        try:
            image.filter(ImageFilter.GaussianBlur(radius=radius)).save(target_path)
            return True
        except Exception:
            return False

    async def _fetch_best_cover(
        self,
        cover_url: str,
        album_art_url: str,
        album_art_params: dict,
        avatar_url: str,
        target_path: str,
    ) -> bool:
        """Fetch a cover image concurrently from multiple sources and save to disk.

        Tries (in order): the dedicated cover URL, the album-art endpoint,
        and the user avatar. Saves the first one that decodes successfully,
        or a 200x200 gray placeholder if all three fail.
        """
        self.assert_download_cache()

        async def fetch_image_bytes(url: str, params: dict | None = None) -> bytes | None:
            try:
                async with self.session.get(url, params=params) as resp:
                    if resp.status != 200:
                        return None
                    if not resp.headers.get("content-type", "").startswith("image"):
                        return None
                    return await resp.read()
            except Exception:
                return None

        async def fetch_decoded_image(url: str, params: dict | None = None) -> Image.Image | None:
            data = await fetch_image_bytes(url, params)
            if not data:
                return None
            try:
                return Image.open(BytesIO(data)).copy()
            except Exception:
                return None

        cover_task = asyncio.create_task(fetch_decoded_image(cover_url))
        album_task = asyncio.create_task(fetch_decoded_image(album_art_url, album_art_params))
        avatar_task = asyncio.create_task(fetch_decoded_image(avatar_url))

        try:
            cover_img = await cover_task
        except Exception:
            cover_img = None
        if cover_img is not None and self._blur_to_file(cover_img, target_path, 15):
            album_task.cancel()
            avatar_task.cancel()
            return True

        try:
            album_img = await album_task
        except Exception:
            album_img = None
        if album_img is not None and self._blur_to_file(album_img, target_path, 5):
            avatar_task.cancel()
            return True

        try:
            avatar_img = await avatar_task
        except Exception:
            avatar_img = None
        if avatar_img is not None:
            try:
                avatar_img.save(target_path)
                return True
            except Exception:
                pass

        try:
            Image.new("RGBA", (200, 200), (50, 50, 50)).save(target_path)
            return True
        except Exception:
            return False

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info(f"Cog {self.__class__.__name__} is ready!")
        await self.sync_blacktea()

    async def _safe_lastfm_request(
        self, url: str, error_context: str = "Last.fm API request"
    ) -> dict | None:
        """Make a safe Last.fm API request with error handling.

        Args:
            url: The API endpoint URL
            error_context: Description for logging purposes

        Returns:
            dict: The JSON response data, or None if request failed
        """
        try:
            async with self.session.get(url) as response:
                if response.status != 200:
                    logger.warning(
                        f"{error_context}: Last.fm API returned status {response.status}"
                    )
                    return None

                data = await response.json()
                return data

        except (aiohttp.ClientError, aiohttp.ContentTypeError) as e:
            logger.error(f"{error_context}: {type(e).__name__} - {e}")
            return None
        except (KeyError, TypeError, ValueError) as e:
            logger.error(
                f"{error_context}: Response parsing error - {type(e).__name__} - {e}"
            )
            return None

    async def update_user_index(self, lastfm_username: str):
        """Fetch and index recent listening data for a user."""
        url_recent = (
            f"http://ws.audioscrobbler.com/2.0/?method=user.getrecenttracks"
            f"&user={quote(lastfm_username)}&api_key={LASTFM_API_KEY}&format=json"
        )

        result = await self._safe_lastfm_request(url_recent)
        if result is not None:
            recent_tracks = result.get("recenttracks", {})
            tracks = recent_tracks.get("track", [])

            if not isinstance(tracks, list):
                logger.debug(f"Invalid tracks format for user {lastfm_username}")
                return []

            return tracks
        else:
            logger.warning(f"Last.fm API request failed for user {lastfm_username}")
            return []

    @commands.hybrid_group(name="lf", invoke_without_command=True, description="Last.fm command group.")
    @unified_cooldown(3)
    async def lastfm(self, ctx: Context) -> None:
        """Last.fm command group"""
        if ctx.interaction is not None:
            prefix = "/"
        else:
            prefix = await self.bot.get_prefix(ctx.message)
            if isinstance(prefix, list):
                prefix = prefix[0]

        group = ctx.command.qualified_name
        subcmds = sorted(
            getattr(ctx.command, "commands", []) or [], key=lambda c: c.name
        )

        if not subcmds:
            await ctx.reply("No subcommands available.", mention_author=False)
            return

        # cogs.lastfm attaches its own commands to this group once the bot is
        # up, so the index is built at invoke time rather than import time. The
        # list is far past one embed's worth, so it is paged ten at a time.
        pages = build_lastfm_help_pages(subcmds, prefix, group)
        view = LastFMHelpView(pages, author_id=ctx.author.id)
        await ctx.reply(embed=pages[0], view=view, mention_author=False)

    @lastfm.command(name="set", description="Set your Last.fm username.")
    @unified_cooldown(10)
    async def set_lastfm(self, ctx: Context, username: str) -> None:
        """Set your Last.fm username"""
        user_id = int(ctx.author.id)
        await self.bot.database.set_lastfm_username(user_id, username)
        await Embeds.custom(
            ctx,
            f"Your Last.fm username has been set to `{username}`.",
            title="Last.fm Username Set",
            color=None,
            reply=True,
        )

    @lastfm.command(name="update", description="Manually update your Last.fm recent listening index.")
    @unified_cooldown(30)
    async def manual_update_index(self, ctx: Context):
        """Allow users to manually update their recent listening data index."""
        user_id = int(ctx.author.id)
        lastfm_username = await self.bot.database.get_lastfm_username(user_id)

        if not lastfm_username:
            await ctx.reply("You haven't set your Last.fm username.")
            return

        await self.update_user_index(lastfm_username)
        await ctx.reply("Your recent listening data has been updated.")

    @lastfm.command(name="color", description="Set your Last.fm embed color.")
    @unified_cooldown(10)
    async def set_color(self, ctx: Context, color: str) -> None:
        """Set your Last.fm embed color with HEX codes or standard color names."""
        color_bank = {
            "black": "#000000",
            "white": "#FFFFFF",
            "red": "#FF0000",
            "green": "#008000",
            "blue": "#0000FF",
            "yellow": "#FFFF00",
            "cyan": "#00FFFF",
            "magenta": "#FF00FF",
            "gray": "#808080",
            "purple": "#800080",
            "orange": "#FFA500",
            "pink": "#FFC0CB",
            "brown": "#A52A2A",
            "lime": "#00FF00",
            "navy": "#000080",
            "teal": "#008080",
            "maroon": "#800000",
            "olive": "#808000",
            "silver": "#C0C0C0",
            "gold": "#FFD700",
            "beige": "#F5F5DC",
            "coral": "#FF7F50",
            "indigo": "#4B0082",
            "violet": "#EE82EE",
        }

        if color.lower() in color_bank:
            color = color_bank[color.lower()]
        elif not (
            color.startswith("#")
            and len(color) == 7
            and all(c in "0123456789ABCDEFabcdef" for c in color[1:])
        ):
            await Embeds.custom(
                ctx,
                "Please provide a valid color. Use HEX format (e.g., `#FF5733`) or a color name (e.g., `red`).",
                title="Error",
                color=0x36393E,
                reply=True,
            )
            return

        user_id = int(ctx.author.id)
        try:
            await self.bot.database.set_lastfm_embed_color(user_id, color)
            await Embeds.custom(
                ctx,
                f"Your embed color has been set to `{color}`.",
                title="Color Set",
                color=int(color.replace("#", "0x"), 16),
                reply=True,
            )
        except ValueError as e:
            await Embeds.error(ctx, str(e), title="Error", reply=True)

    @lastfm.command(name="votes", description="See your Last.fm vote statistics.")
    @unified_cooldown(15)
    async def lastfm_stats(self, ctx: Context):
        """See your Last.fm vote statistics"""
        user_id = int(ctx.author.id)
        np_upvotes, np_downvotes = await self.bot.database.get_vote_stats(user_id, "np")
        snp_upvotes, snp_downvotes = await self.bot.database.get_vote_stats(
            user_id, "snp"
        )
        jnp_upvotes, jnp_downvotes = await self.bot.database.get_vote_stats(
            user_id, "jnp"
        )
        embed_color = await self.bot.database.get_lastfm_embed_color(user_id)

        np_ratio = (
            (np_upvotes - np_downvotes) / max(np_downvotes, 1)
            if np_downvotes >= np_downvotes
            else -(np_downvotes - np_downvotes) / max(np_downvotes, 1)
        )
        snp_ratio = (
            (snp_upvotes - snp_downvotes) / max(snp_downvotes, 1)
            if snp_upvotes >= snp_downvotes
            else -(snp_downvotes - snp_upvotes) / max(snp_upvotes, 1)
        )
        jnp_ratio = (
            (jnp_upvotes - jnp_downvotes) / max(jnp_downvotes, 1)
            if jnp_upvotes >= jnp_downvotes
            else -(jnp_downvotes - jnp_upvotes) / max(jnp_upvotes, 1)
        )

        description = (
            f"**Now Playing (np)**:\n"
            f"Upvotes: {np_upvotes}\nDownvotes: {np_downvotes}\nRatio: {np_ratio:.3g}\n\n"
            f"**Spotify Now Playing (snp)**:\n"
            f"Upvotes: {snp_upvotes}\nDownvotes: {snp_downvotes}\nRatio: {snp_ratio:.3g}\n\n"
            f"**JuiceWRLD Now Playing (jnp)**:\n"
            f"Upvotes: {jnp_upvotes}\nDownvotes: {jnp_downvotes}\nRatio: {jnp_ratio:.3g}"
        )

        await Embeds.custom(
            ctx,
            description,
            title="Last.fm Vote Statistics",
            color=embed_color,
            reply=True,
        )

    @lastfm.command(name="plays", description="Display the play count for the current song on Last.fm.")
    @unified_cooldown(15)
    async def song_plays(self, ctx: Context):
        """Display the play count for the current song on Last.fm."""
        user_id = int(ctx.author.id)
        prefix = await self.bot.database.get_prefix(ctx.guild.id)
        try:
            lastfm_username = await self.bot.database.get_lastfm_username(user_id)
        except ValueError:
            lastfm_username = None
        embed_color = await self.bot.database.get_lastfm_embed_color(user_id)

        async with ctx.typing():
            if not lastfm_username:
                await Embeds.custom(
                    ctx,
                    f"You have not set your Last.fm username. Use `{prefix}lf set <username>` to set it.",
                    title="Error",
                    color=0x36393E,
                    reply=True,
                )
                return

        recent_tracks = await self.update_user_index(lastfm_username)
        if not recent_tracks:
            await ctx.reply("No recent tracks found.")
            return

        track = recent_tracks[0] if recent_tracks else {}
        track_name = track.get("name", "Unknown Track")
        artist_name = track.get("artist", {}).get("#text", "Unknown Artist")

        async def get_track_playcount(lastfm_username, artist_name, track_name):
            """Fetch track playcount using the safe Last.fm request helper."""
            url = f"http://ws.audioscrobbler.com/2.0/?method=track.getInfo&api_key={LASTFM_API_KEY}&artist={quote(artist_name)}&track={quote(track_name)}&username={lastfm_username}&format=json"
            response_data = await self._safe_lastfm_request(url, "Get track playcount")
            if response_data:
                return response_data.get("track", {}).get("userplaycount", "N/A")
            return "N/A"

        playcount = await get_track_playcount(lastfm_username, artist_name, track_name)

        await Embeds.custom(
            ctx,
            f"You've listened to **{track_name}** by **{artist_name}** {playcount} times.",
            title="Current Song Plays",
            color=embed_color,
            reply=True,
        )

    @lastfm.command(name="toptentracks", aliases=["ttt"], description="Display your top ten tracks on Last.fm.")
    @unified_cooldown(15)
    async def top_tracks(self, ctx: Context):
        """Display the user's top ten tracks on Last.fm."""
        user_id = int(ctx.author.id)
        prefix = await self.bot.database.get_prefix(ctx.guild.id)
        try:
            lastfm_username = await self.bot.database.get_lastfm_username(user_id)
        except ValueError:
            lastfm_username = None
        embed_color = await self.bot.database.get_lastfm_embed_color(user_id)

        async with ctx.typing():
            if not lastfm_username:
                await Embeds.custom(
                    ctx,
                    f"You have not set your Last.fm username. Use `{prefix}lf set <username>` to set it.",
                    title="Error",
                    color=0x36393E,
                    reply=True,
                )
                return

        url = f"http://ws.audioscrobbler.com/2.0/?method=user.gettoptracks&user={quote(lastfm_username)}&api_key={LASTFM_API_KEY}&format=json&limit=10"

        response_data = await self._safe_lastfm_request(url, "Get top tracks")
        if not response_data:
            await ctx.reply("Couldn't retrieve top tracks. Please try again later.")
            return

        tracks = response_data.get("toptracks", {}).get("track", [])
        if not tracks:
            await ctx.reply("Couldn't retrieve top tracks. Please try again later.")
            return

        description = "\n".join(
            [
                f"{i+1}. [{track.get('name', 'Unknown')}]({track.get('url', '#')}) - `{track.get('playcount', '0')}` plays"
                for i, track in enumerate(tracks)
            ]
        )

        embed = discord.Embed(
            title=f"{lastfm_username}'s Top 10 Tracks",
            description=description,
            color=embed_color,
        )
        embed.set_footer(text="Data from Last.fm")
        await ctx.reply(embed=embed)

    @lastfm.command(name="topartists", aliases=["tar"], description="Display your top artists on Last.fm.")
    @unified_cooldown(15)
    async def top_artists(self, ctx: Context):
        """Display the user's top artists on Last.fm."""
        user_id = int(ctx.author.id)
        prefix = await self.bot.database.get_prefix(ctx.guild.id)
        try:
            lastfm_username = await self.bot.database.get_lastfm_username(user_id)
        except ValueError:
            lastfm_username = None
        embed_color = await self.bot.database.get_lastfm_embed_color(user_id)

        async with ctx.typing():
            if not lastfm_username:
                await Embeds.custom(
                    ctx,
                    f"You have not set your Last.fm username. Use `{prefix}lf set <username>` to set it.",
                    title="Error",
                    color=0x36393E,
                    reply=True,
                )
                return

            url = f"http://ws.audioscrobbler.com/2.0/?method=user.gettopartists&user={quote(lastfm_username)}&api_key={LASTFM_API_KEY}&format=json&limit=10"

            try:
                async with self.session.get(url) as response:
                    if response.status != 200:
                        logger.warning(f"Last.fm API returned status {response.status} for top artists")
                        await ctx.reply("Couldn't retrieve top artists. Please try again later.")
                        return
                    
                    top_artists_data = await response.json()

                    artists = top_artists_data.get("topartists", {}).get("artist", [])
                    if not artists or not isinstance(artists, list):
                        logger.debug(f"No top artists found for {lastfm_username}")
                        await ctx.reply("Couldn't retrieve top artists. Please try again later.")
                        return

                    description_lines = []
                    for i, artist in enumerate(artists):
                        if not isinstance(artist, dict):
                            continue
                        artist_name = artist.get("name", "Unknown Artist")
                        playcount = artist.get("playcount", 0)
                        artist_url = f"https://www.last.fm/music/{artist_name.replace(' ', '+')}"
                        description_lines.append(
                            f"{i+1}. [{artist_name}]({artist_url}) - `{playcount}` plays"
                        )
                    
                    description = "\n".join(description_lines)

                    embed = discord.Embed(
                        title=f"{lastfm_username}'s Top 10 Artists",
                        description=description,
                        color=embed_color,
                    )
                    embed.set_footer(text="Data from Last.fm")
                    await ctx.reply(embed=embed)
                    
            except (aiohttp.ClientError, KeyError, TypeError, ValueError) as e:
                logger.error(f"Error fetching top artists for {lastfm_username}: {type(e).__name__} - {e}")
                await ctx.reply("An error occurred while fetching your top artists. Please try again later.")

    @lastfm.command(name="whoknows", aliases=["wk"], description="Show who in the server has listened to an artist the most.")
    @unified_cooldown(30)
    async def who_knows(self, ctx: Context, artist_name: Optional[str] = None):
        """Show who in the server has listened to the specified artist the most."""
        if not artist_name:
            await Embeds.custom(
                ctx,
                "Please provide an artist name.",
                title="Error",
                color=0x36393E,
                reply=True,
            )
            return
        user_id = int(ctx.author.id)
        embed_color = await self.bot.database.get_lastfm_embed_color(user_id)
        listening_users = []

        async def fetch_playcount(lastfm_username):
            """Fetch user playcount for an artist from Last.fm."""
            url = f"https://ws.audioscrobbler.com/2.0/?method=artist.getinfo&artist={quote(artist_name)}&username={lastfm_username}&api_key={LASTFM_API_KEY}&format=json"
            response_data = await self._safe_lastfm_request(url, f"Fetch playcount for {lastfm_username}")
            if response_data:
                try:
                    return int(response_data.get("artist", {}).get("stats", {}).get("userplaycount", 0))
                except (ValueError, TypeError):
                    return 0
            return 0

        async def fetch_artist_image(artist_name, lastfm_username):
            """Fetch artist image from Last.fm."""
            url = f"https://ws.audioscrobbler.com/2.0/?method=artist.getinfo&artist={quote(artist_name)}&username={lastfm_username}&api_key={LASTFM_API_KEY}&format=json"

            try:
                async with self.session.get(url) as response:
                    if response.status != 200:
                        logger.warning(f"Last.fm API returned status {response.status} for artist {artist_name}")
                        return None

                    data = await response.json()
                    
                    artist_data = data.get("artist", {})
                    image_list = artist_data.get("image", [])
                    
                    if not isinstance(image_list, list) or len(image_list) < 4:
                        logger.debug(f"No large image available for artist {artist_name}")
                        for image_size in image_list:
                            if isinstance(image_size, dict) and image_size.get("#text"):
                                return image_size.get("#text")
                        return None
                    
                    image_url = image_list[3].get("#text", "") if isinstance(image_list[3], dict) else ""
                    
                    if not image_url:
                        logger.debug(f"No image URL available for artist {artist_name}")
                        for i in range(len(image_list)):
                            if isinstance(image_list[i], dict) and image_list[i].get("#text"):
                                return image_list[i].get("#text")
                    
                    return image_url if image_url else None
                    
            except (aiohttp.ClientError, KeyError, IndexError, TypeError, ValueError) as e:
                logger.error(f"Error fetching artist image for {artist_name}: {type(e).__name__} - {e}")
                return None

        tasks = []
        valid_members = []
        usernames = await self.bot.database.get_lastfm_usernames()

        for lastfm_username in usernames:
            if not lastfm_username:
                continue
            valid_members.append(lastfm_username)
            tasks.append(fetch_playcount(lastfm_username))

        results = await asyncio.gather(*tasks)

        listening_users = []
        for lfm_user, playcount in zip(valid_members, results):
            if playcount and playcount > 0:
                listening_users.append((lfm_user, playcount))

        listening_users.sort(key=lambda x: x[1], reverse=True)
        top_listening_users = listening_users[:10]

        if listening_users:
            description = "\n".join(
                [
                    f"**[{lfm_user}](https://www.last.fm/user/{lfm_user})**: `{playcount}`"
                    for lfm_user, playcount in top_listening_users
                ]
            )
        else:
            description = f"No one in the server has listened to **__{artist_name}__**."

        embed = discord.Embed(
            title=f"Who Knows {artist_name}?",
            description=description,
            color=embed_color,
        )
        artist_image_url = await fetch_artist_image(lastfm_username)
        if artist_image_url:
            embed.set_thumbnail(url=artist_image_url)
        
        try:
            user_playcount = await fetch_playcount(lastfm_username)
            embed.add_field(
                name="Your Playcount",
                value=f"`{user_playcount}`",
                inline=False,
            )
        except Exception:
            embed.add_field(
                name="Your Playcount",
                value="`N/A`",
                inline=False,
            )
        await ctx.reply(embed=embed)

    @commands.hybrid_command(name="np", description="View what you are currently playing on Last.fm.")
    @unified_cooldown(15)
    async def now_playing(self, ctx: Context) -> None:
        """View what you are currently playing on Last.fm"""
        user_id = int(ctx.author.id)
        prefix = await self.bot.database.get_prefix(ctx.guild.id)
        try:
            lastfm_username = await self.bot.database.get_lastfm_username(user_id)
        except ValueError:
            lastfm_username = None
        embed_color = await self.bot.database.get_lastfm_embed_color(user_id)
        async with ctx.typing():
            if not lastfm_username:
                await Embeds.custom(
                    ctx,
                    f"You have not set your Last.fm username. Use `{prefix}lf set <username>` to set it.",
                    title="Error",
                    color=0x36393E,
                    reply=True,
                )
                return

            async def get_user_data(lastfm_username: str):
                url = f"https://ws.audioscrobbler.com/2.0/?method=user.getinfo&user={lastfm_username}&api_key={LASTFM_API_KEY}&format=json"

                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as response:
                        if response.status != 200 or "application/json" not in response.headers.get("Content-Type", ""):
                            logger.error(f"Last.fm user.getinfo returned {response.status} for {lastfm_username}")
                            return None
                        data = await response.json()
                        return data

            async def get_image_url(data, size="medium"):
                images = data.get("user", {}).get("image", [])
                for image in images:
                    if image.get("size") == size:
                        return image.get("#text")
                return None

            async def get_recent_tracks(lastfm_username: str):
                url_recent = f"http://ws.audioscrobbler.com/2.0/?method=user.getrecenttracks&user={lastfm_username}&api_key={LASTFM_API_KEY}&format=json"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url_recent) as response:
                        if response.status != 200 or "application/json" not in response.headers.get("Content-Type", ""):
                            logger.error(f"Last.fm user.getrecenttracks returned {response.status} for {lastfm_username}")
                            return None
                        recent_tracks = await response.json()
                        return recent_tracks

            async def get_track_info(
                artist_name: str, track_name: str, lastfm_username: str
            ):
                url_info = f"http://ws.audioscrobbler.com/2.0/?method=track.getInfo&api_key={LASTFM_API_KEY}&artist={artist_name}&track={track_name}&username={lastfm_username}&format=json"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url_info) as response:
                        if response.status != 200 or "application/json" not in response.headers.get("Content-Type", ""):
                            logger.error(f"Last.fm track.getInfo returned {response.status} for {artist_name} - {track_name}")
                            return None
                        recent_track_info = await response.json()

                        if "track" not in recent_track_info:
                            logger.error(
                                f"Last.fm API did not return track info: {recent_track_info}"
                            )
                            return None

                        return recent_track_info

            user_data = await get_user_data(lastfm_username)

            lastfm_avatar_url = await get_image_url(user_data, "large")

            recent_tracks = await get_recent_tracks(lastfm_username)
            if not recent_tracks or not recent_tracks.get("recenttracks", {}).get("track"):
                await Embeds.custom(
                    ctx,
                    "No recent tracks found for the user.",
                    title="Error",
                    color=0x36393E,
                    reply=True,
                )
                return

            track = recent_tracks.get("recenttracks", {}).get("track", [{}])[0]
            track_name = track.get("name", "Unknown Track")
            artist_name = track.get("artist", {}).get("#text", "Unknown Artist")
            album_name = track.get("album", {}).get("#text", "Unknown Album")
            track_url = track.get("url", "#")

            track_info = await get_track_info(artist_name, track_name, lastfm_username)

            if not track_info or "track" not in track_info:
                await Embeds.custom(
                    ctx,
                    "Could not retrieve track information from Last.fm.",
                    title="Error",
                    color=0x36393E,
                    reply=True,
                )
                return

            playcount = track_info.get("track", {}).get("userplaycount", "N/A") if track_info else "N/A"
            total_scrobbles = (
                user_data.get("user", {}).get("playcount", "N/A")
                if user_data
                else "N/A"
            )

            embed = discord.Embed(
                title="Now Playing",
                color=embed_color,
            )

            if "image" in track and track["image"]:
                thumbnail_url = track.get("image", [{}])[-1].get("#text", None)
                if thumbnail_url:
                    embed.set_thumbnail(url=thumbnail_url)

            embed.add_field(
                name="Track:",
                value=f"[{track_name}]({track_url}) by {artist_name}",
                inline=False,
            )
            embed.add_field(name="Album:", value=f"{album_name}", inline=False)
            embed.set_footer(
                text=f"Playcount: {playcount} - Total Scrobbles: {total_scrobbles}"
            )
            embed.set_author(
                name=f"{lastfm_username} on Last.fm:",
                url=f"https://www.last.fm/user/{lastfm_username}",
                icon_url=lastfm_avatar_url,
            )

        message = await ctx.reply(embed=embed)
        await message.add_reaction("👍")
        await asyncio.sleep(0.5)  
        await message.add_reaction("👎")

        def check(reaction, user):
            return user == ctx.author and str(reaction.emoji) in ["👍", "👎"]

        try:
            reaction, user = await self.bot.wait_for(
                "reaction_add", timeout=15.0, check=check
            )
            if str(reaction.emoji) == "👍":
                await self.bot.database.log_vote(user.id, "np", "up")
            elif str(reaction.emoji) == "👎":
                await self.bot.database.log_vote(user.id, "np", "down")
        except asyncio.TimeoutError:
            logger.debug("No reaction received within the timeout period.")
        except asyncio.CancelledError:
            logger.debug("Reaction task was cancelled.")

    def draw_glowing_text(
        self,
        base_image: Image.Image,
        text: str,
        position: tuple[int, int],
        font,
        text_color: str,
        glow_color: str,
        glow_radius: int = 8,
    ):
        """
        Draws text onto base_image with a glow effect,
        ensuring the *glyphs* start exactly at `position`, not the glow outline.
        """
        x, y = position

        glow_layer = Image.new("RGBA", base_image.size, (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(glow_layer)

        glow_draw.text((x, y), text, font=font, fill=glow_color)

        blurred = glow_layer.filter(ImageFilter.GaussianBlur(glow_radius))

        offset = glow_radius // 2

        base_image.paste(blurred, (-offset, -offset), blurred)

        draw = ImageDraw.Draw(base_image)
        draw.text((x, y), text, font=font, fill=text_color)

    def draw_glowing_rectangle(
        self,
        base_image,
        xy,
        radius,
        outline,
        fill,
        width=1,
        glow_color=None,
        glow_radius=8,
    ):
        """
        Draws a rounded rectangle with a glow effect onto base_image.
        xy = [x1, y1, x2, y2], the bounding box
        """

        glow_layer = Image.new("RGBA", base_image.size, (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(glow_layer)

        if glow_color:
            glow_draw.rounded_rectangle(
                xy, radius=radius, fill=glow_color, outline=glow_color
            )

        blurred_layer = glow_layer.filter(ImageFilter.GaussianBlur(glow_radius))

        base_image.alpha_composite(blurred_layer)

        final_draw = ImageDraw.Draw(base_image)
        final_draw.rounded_rectangle(
            xy, radius=radius, fill=fill, outline=outline, width=width
        )

    def dynamic_font(self, text, max_width, font_path, max_font_size):
        font_size = max_font_size
        font = ImageFont.truetype(font_path, font_size)
        draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))

        while draw.textbbox((0, 0), text, font=font)[2] > max_width and font_size > 8:
            font_size -= 1
            font = ImageFont.truetype(font_path, font_size)

        return font

    def glow_behind_image(
        self, base_image, cover_image, cover_mask, position, glow_color, glow_radius=10
    ):
        """
        Pastes a copy of cover_image's shape in glow_color behind it (blurred)
        to create a glow effect, then pastes the actual cover_image on top.

        :param base_image:  The main RGBA Image where you draw everything.
        :param cover_image: The (already rounded or cropped) cover image.
        :param cover_mask:  The same mask used to shape the cover (rounded corners, etc).
        :param position:    (x, y) coords where the cover should be placed.
        :param glow_color:  The color of the glow (e.g. "white").
        :param glow_radius: How “strong” or wide the glow should be (integer).
        """

        base_image = base_image.convert("RGBA")

        temp_layer = Image.new("RGBA", base_image.size, (0, 0, 0, 0))

        glow_shape = Image.new("RGBA", cover_image.size, glow_color)
        temp_layer.paste(glow_shape, position, cover_mask)

        blurred_layer = temp_layer.filter(ImageFilter.GaussianBlur(glow_radius))

        base_image.alpha_composite(blurred_layer)

        base_image.paste(cover_image, position, cover_mask)

        return base_image

    def create_vertical_gradient(self, width, height, color1, color2):
        """
        Returns a new Pillow Image (RGB) of size (width, height),
        with a vertical gradient from color1 (top) to color2 (bottom).
        """
        from PIL import Image, ImageDraw

        gradient = Image.new("RGB", (width, height), color=0)
        draw = ImageDraw.Draw(gradient)

        r1, g1, b1 = color1
        r2, g2, b2 = color2

        for y in range(height):
            ratio = y / float(height)
            r = int(r1 * (1 - ratio) + r2 * ratio)
            g = int(g1 * (1 - ratio) + g2 * ratio)
            b = int(b1 * (1 - ratio) + b2 * ratio)

            draw.line([(0, y), (width, y)], fill=(r, g, b))

        return gradient

    def get_text_color(self, background_color):
        r, g, b = background_color
        luminance = 0.299 * r + 0.587 * g + 0.114 * b

        return "black" if luminance > 150 else "white"

    async def find_spotify_activity(self, member, retries=3, delay=2):
        for _ in range(retries):
            activity = next(
                (
                    activity
                    for activity in member.activities
                    if isinstance(activity, discord.Spotify)
                ),
                None,
            )
            if activity:
                return activity
            await asyncio.sleep(delay)
        return None

    @commands.hybrid_command(name="snp", description="View what you are currently playing on Spotify.")
    @unified_cooldown(30)
    async def spotify_now_playing(
        self, ctx: commands.Context, member: Optional[discord.Member] = None
    ) -> None:
        """View what you are currently playing on Spotify."""
        member = member or ctx.author

        try:
            async with ctx.typing():
                activity = await self.find_spotify_activity(member)
                if activity is None:
                    await Embeds.custom(
                        ctx,
                        "No Spotify activity found.\n\nMake sure your Spotify is connected to Discord and you are sharing activity status in privacy settings.\n\n**Note:** This command only works if you are listening to a song that is on Spotify. __Not local files.__",
                        title="Error",
                        color=0x36393E,
                        reply=True,
                    )
                    return

                cover_url = activity.album_cover_url
                if cover_url:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(cover_url) as response:
                            cover_data = await response.read()

                    cover_image = Image.open(BytesIO(cover_data)).resize((200, 200))
                    color_thief = ColorThief(BytesIO(cover_data))

                    palette = color_thief.get_palette(color_count=2)
                    if len(palette) < 2:
                        palette = [color_thief.get_color(quality=10)] * 2
                    color1, color2 = palette[0], palette[1]

                    width, height = 800, 400
                    gradient_bg = self.create_vertical_gradient(
                        width, height, color1, color2
                    )
                    img = gradient_bg.convert("RGBA")
                else:
                    img = Image.new("RGBA", (800, 400), (30, 215, 96))

                if cover_url:
                    cover_image = Image.open(BytesIO(cover_data)).resize((200, 200))
                    rounded_cover = rounded_cover = Image.new(
                        "RGBA", cover_image.size, (0, 0, 0, 0)
                    )
                    mask = Image.new("L", cover_image.size, 0)
                    ImageDraw.Draw(mask).rounded_rectangle(
                        (0, 0, *cover_image.size), radius=20, fill=255
                    )

                    text_color = self.get_text_color(color1)
                    glow_color = "white" if text_color == "black" else "black"
                    rounded_cover.paste(cover_image, (0, 0), mask)

                    img = self.glow_behind_image(
                        base_image=img,
                        cover_image=rounded_cover,
                        cover_mask=mask,
                        position=(50, 100),
                        glow_color=glow_color,
                        glow_radius=12,
                    )
                else:
                    text_color = "white"

                max_text_width = width - 350

                title_font = self.dynamic_font(
                    activity.title, max_text_width, self.font_path, max_font_size=40
                )
                artist_font = self.dynamic_font(
                    activity.artist, max_text_width, self.font_path, max_font_size=30
                )
                album_font = self.dynamic_font(
                    activity.album, max_text_width, self.font_path, max_font_size=24
                )
                img = img.convert("RGBA")
                glow_color = "white" if text_color == "black" else "black"
                text_glow_radius = 6

                self.draw_glowing_text(
                    base_image=img,
                    text=activity.title,
                    position=(300, 100),
                    font=title_font,
                    text_color=text_color,
                    glow_color=glow_color,
                    glow_radius=text_glow_radius,
                )
                self.draw_glowing_text(
                    base_image=img,
                    text=activity.artist,
                    position=(300, 150),
                    font=artist_font,
                    text_color=text_color,
                    glow_color=glow_color,
                    glow_radius=text_glow_radius,
                )
                self.draw_glowing_text(
                    base_image=img,
                    text=activity.album,
                    position=(300, 200),
                    font=album_font,
                    text_color=text_color,
                    glow_color=glow_color,
                    glow_radius=text_glow_radius,
                )

                total_duration = activity.duration.total_seconds()
                elapsed_duration = (
                    discord.utils.utcnow() - activity.start
                ).total_seconds()
                progress = (
                    min(1, elapsed_duration / total_duration)
                    if total_duration > 0
                    else 0
                )

                bar_x, bar_y, bar_width, bar_height = 300, 250, 400, 20
                rectangle_glow_radius = 8
                rectangle_radius = 10
                rectangle_width = 2

                self.draw_glowing_rectangle(
                    base_image=img,
                    xy=[bar_x, bar_y, bar_x + bar_width, bar_y + bar_height],
                    radius=rectangle_radius,
                    outline=text_color,
                    fill=None,
                    width=rectangle_width,
                    glow_color=text_color,
                    glow_radius=rectangle_glow_radius,
                )

                filled_width = int(bar_width * progress)
                if filled_width < 0:
                    filled_width = 0

                filled_x1 = bar_x + filled_width

                if filled_x1 > bar_x:
                    self.draw_glowing_rectangle(
                        base_image=img,
                        xy=[bar_x, bar_y, filled_x1, bar_y + bar_height],
                        radius=rectangle_radius,
                        outline=text_color,
                        fill=text_color,
                        width=rectangle_width,
                        glow_color=text_color,
                        glow_radius=rectangle_glow_radius,
                    )

                minutes_elapsed, seconds_elapsed = divmod(int(elapsed_duration), 60)
                minutes_total, seconds_total = divmod(int(total_duration), 60)
                duration_text = f"{minutes_elapsed}:{seconds_elapsed:02} / {minutes_total}:{seconds_total:02}"
                time_font = self.dynamic_font(
                    duration_text, max_text_width, self.font_path, max_font_size=20
                )
                self.draw_glowing_text(
                    base_image=img,
                    text=duration_text,
                    position=(300, 280),
                    font=time_font,
                    text_color=text_color,
                    glow_color=glow_color,
                    glow_radius=text_glow_radius,
                )

                async def get_track_playcount(lastfm_username, artist_name, track_name):
                    url = f"http://ws.audioscrobbler.com/2.0/?method=track.getInfo&api_key={LASTFM_API_KEY}&artist={artist_name}&track={track_name}&username={lastfm_username}&format=json"
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url) as response:
                            track_info = await response.json()
                            return track_info.get("track", {}).get(
                                "userplaycount", "N/A"
                            )

                user_id = int(member.id)
                try:
                    lastfm_username = await self.bot.database.get_lastfm_username(
                        user_id
                    )

                    if lastfm_username:
                        playcount = await get_track_playcount(
                            lastfm_username, activity.artist, activity.title
                        )
                    else:
                        playcount = "N/A"
                except ValueError:
                    playcount = "N/A"

                playcount_text = f"Plays: {playcount}"
                playcount_font = self.dynamic_font(
                    playcount_text, max_text_width, self.font_path, max_font_size=20
                )
                self.draw_glowing_text(
                    base_image=img,
                    text=playcount_text,
                    position=(300, 320),
                    font=playcount_font,
                    text_color=text_color,
                    glow_color=glow_color,
                    glow_radius=text_glow_radius,
                )

                image_bytes = BytesIO()
                img.save(image_bytes, format="PNG")
                image_bytes.seek(0)
                file = discord.File(fp=image_bytes, filename="now_playing.png")
                embed = discord.Embed(
                    title="Now Playing on Spotify",
                    description=f"[{activity.title}]({activity.track_url})",
                    color=discord.Color.green(),
                )
                embed.set_image(url="attachment://now_playing.png")
            message = await ctx.reply(embed=embed, file=file)
            await message.add_reaction("👍")
            await asyncio.sleep(
                0.5
            )  
            await message.add_reaction("👎")

            def check(reaction, user):
                return user == ctx.author and str(reaction.emoji) in ["👍", "👎"]

            try:
                reaction, user = await self.bot.wait_for(
                    "reaction_add", timeout=15.0, check=check
                )
                if str(reaction.emoji) == "👍":
                    await self.bot.database.log_vote(user.id, "np", "up")
                elif str(reaction.emoji) == "👎":
                    await self.bot.database.log_vote(user.id, "np", "down")
            except asyncio.TimeoutError:
                logger.debug("No reaction received within the timeout period.")
            except asyncio.CancelledError:
                logger.debug("Reaction task was cancelled.")

        except aiohttp.ClientError:
            await Embeds.custom(
                ctx,
                "There was an error fetching the cover art for the currently playing song. This should not happen. Please try again later.",
                title="Error",
                color=0x36393E,
                reply=True,
            )

    @commands.hybrid_group(name="jwapi", description="JuiceWRLD API command group.")
    @unified_cooldown(3)
    async def juicewrld_api(self, ctx: commands.Context) -> None:
        if ctx.interaction is not None:
            prefix = "/"
        else:
            prefix = await self.bot.get_prefix(ctx.message)
            if isinstance(prefix, list):
                prefix = prefix[0]

        subcmds = getattr(ctx.command, "commands", []) or []
        lines = []
        for cmd in sorted(subcmds, key=lambda c: c.name):
            name = cmd.name
            aliases = (
                f" (or: {', '.join(cmd.aliases)})"
                if getattr(cmd, "aliases", None)
                else ""
            )
            desc = render((cmd.help or cmd.description or ""), prefix).strip()
            if desc:
                lines.append(f"`{prefix}jwapi {name}`{aliases} — {desc}")
            else:
                lines.append(f"`{prefix}jwapi {name}`{aliases}")

        description = "\n".join(lines) if lines else "No subcommands available."

        embed = discord.Embed(
            title="JuiceWRLD API — Available Commands",
            description=description,
            color=discord.Color.blurple(),
        )
        await ctx.reply(embed=embed, mention_author=False)

    @juicewrld_api.command(name="np", description="View what you are currently playing on JuiceWRLD API desktop app.")
    @unified_cooldown(15)
    async def juicewrld_now_playing(
        self, ctx: commands.Context, member: Optional[discord.Member] = None
    ) -> None:
        """View what you are currently playing on JuiceWRLD API desktop app."""
        member = member or ctx.author

        try:
            async with ctx.typing():
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{JUICEWRLD_API}/analytics/now-playing/discord",
                        params={"discord_user_id": member.id},
                    ) as response:
                        if response.status == 404:
                            try:
                                error_data = await response.json()
                                if not error_data.get("is_linked"):
                                    await Embeds.custom(
                                        ctx,
                                        f"{member.mention}'s Discord account is not linked to JuiceWRLD API.\n\n**To link your account:**\n1. Open the JuiceWRLD API desktop app\n2. Go to account and generate a device pairing code\n3. Then use the jlink command with your code!",
                                        title="Account Not Linked",
                                        color=0x36393E,
                                        reply=True,
                                    )
                                    return
                            except:
                                pass

                        if response.status != 200:
                            await Embeds.custom(
                                ctx,
                                "Failed to fetch now playing data from JuiceWRLD API.",
                                title="Error",
                                color=0x36393E,
                                reply=True,
                            )
                            return

                        data = await response.json()

                        if not data.get("is_linked"):
                            await Embeds.custom(
                                ctx,
                                f"🚫 {member.mention}'s Discord account is not linked to JuiceWRLD API.\n\n**To link your account:**\n1. Open the JuiceWRLD API desktop app\n2. Go to settings and connect your Discord account\n3. Then try this command again!",
                                title="Account Not Linked",
                                color=0x36393E,
                                reply=True,
                            )
                            return

                        now_playing = data.get("now_playing")
                        if not now_playing or not now_playing.get("is_playing"):
                            await Embeds.custom(
                                ctx,
                                "No music currently playing.\n\nStart playing music on your desktop app to see it here!",
                                title="Now Playing",
                                color=0x808080,
                                reply=True,
                            )
                            return

                        title = now_playing.get("track") or now_playing.get(
                            "title", "Unknown Title"
                        )
                        artist = now_playing.get("artist", "Unknown Artist")
                        album = now_playing.get("album", "Unknown Album")
                        cover_url = (
                            now_playing.get("album_art_url")
                            or now_playing.get("cover_url")
                            or now_playing.get("image_url")
                        )
                        track_url = now_playing.get("track_url") or now_playing.get(
                            "url", ""
                        )
                        duration_raw = now_playing.get("duration", 0)
                        duration_as_seconds = duration_raw
                        duration_as_milliseconds = duration_raw / 1000.0
                        if duration_raw > 600 and duration_as_milliseconds <= 600:
                            duration_seconds = duration_as_milliseconds
                        else:
                            duration_seconds = duration_as_seconds
                        start_time_str = now_playing.get(
                            "timestamp")
                        position = now_playing.get("position", 0)

                        if cover_url:
                            async with aiohttp.ClientSession() as cover_session:
                                async with cover_session.get(
                                    cover_url
                                ) as cover_response:
                                    cover_data = await cover_response.read()

                            cover_image = Image.open(BytesIO(cover_data)).resize(
                                (200, 200)
                            )
                            color_thief = ColorThief(BytesIO(cover_data))

                            palette = color_thief.get_palette(color_count=2)
                            if len(palette) < 2:
                                palette = [color_thief.get_color(quality=10)] * 2
                            color1, color2 = palette[0], palette[1]

                            width, height = 800, 400
                            gradient_bg = self.create_vertical_gradient(
                                width, height, color1, color2
                            )
                            img = gradient_bg.convert("RGBA")
                        else:
                            width, height = 800, 400
                            img = Image.new("RGBA", (800, 400), (30, 215, 96))
                            color1 = (30, 215, 96)

                        if cover_url:
                            cover_image = Image.open(BytesIO(cover_data)).resize(
                                (200, 200)
                            )
                            rounded_cover = Image.new(
                                "RGBA", cover_image.size, (0, 0, 0, 0)
                            )
                            mask = Image.new("L", cover_image.size, 0)
                            ImageDraw.Draw(mask).rounded_rectangle(
                                (0, 0, *cover_image.size), radius=20, fill=255
                            )

                            text_color = self.get_text_color(color1)
                            glow_color = "white" if text_color == "black" else "black"
                            rounded_cover.paste(cover_image, (0, 0), mask)

                            img = self.glow_behind_image(
                                base_image=img,
                                cover_image=rounded_cover,
                                cover_mask=mask,
                                position=(50, 100),
                                glow_color=glow_color,
                                glow_radius=12,
                            )
                        else:
                            text_color = "white"

                        max_text_width = width - 350

                        title_font = self.dynamic_font(
                            title, max_text_width, self.font_path, max_font_size=40
                        )
                        artist_font = self.dynamic_font(
                            artist, max_text_width, self.font_path, max_font_size=30
                        )
                        album_font = self.dynamic_font(
                            album, max_text_width, self.font_path, max_font_size=24
                        )
                        img = img.convert("RGBA")
                        glow_color = "white" if text_color == "black" else "black"
                        text_glow_radius = 6

                        self.draw_glowing_text(
                            base_image=img,
                            text=title,
                            position=(300, 100),
                            font=title_font,
                            text_color=text_color,
                            glow_color=glow_color,
                            glow_radius=text_glow_radius,
                        )
                        self.draw_glowing_text(
                            base_image=img,
                            text=artist,
                            position=(300, 150),
                            font=artist_font,
                            text_color=text_color,
                            glow_color=glow_color,
                            glow_radius=text_glow_radius,
                        )
                        self.draw_glowing_text(
                            base_image=img,
                            text=album,
                            position=(300, 200),
                            font=album_font,
                            text_color=text_color,
                            glow_color=glow_color,
                            glow_radius=text_glow_radius,
                        )

                        elapsed_duration = 0
                        if duration_seconds > 0:
                            if position > 0:
                                elapsed_duration = position / 1000.0
                            elif start_time_str:
                                try:
                                    start_time = datetime.fromisoformat(
                                        start_time_str.replace("Z", "+00:00")
                                    )
                                    if start_time.tzinfo is None:
                                        start_time = start_time.replace(
                                            tzinfo=timezone.utc
                                        )
                                    elapsed_duration = (
                                        discord.utils.utcnow() - start_time
                                    ).total_seconds()
                                except:
                                    elapsed_duration = 0
                            progress = (
                                min(1, elapsed_duration / duration_seconds)
                                if duration_seconds > 0
                                else 0
                            )
                        else:
                            progress = 0

                        bar_x, bar_y, bar_width, bar_height = 300, 250, 400, 20
                        rectangle_glow_radius = 8
                        rectangle_radius = 10
                        rectangle_width = 2

                        self.draw_glowing_rectangle(
                            base_image=img,
                            xy=[bar_x, bar_y, bar_x + bar_width, bar_y + bar_height],
                            radius=rectangle_radius,
                            outline=text_color,
                            fill=None,
                            width=rectangle_width,
                            glow_color=text_color,
                            glow_radius=rectangle_glow_radius,
                        )

                        filled_width = int(bar_width * progress)
                        if filled_width < 0:
                            filled_width = 0

                        filled_x1 = bar_x + filled_width

                        if filled_x1 > bar_x:
                            self.draw_glowing_rectangle(
                                base_image=img,
                                xy=[bar_x, bar_y, filled_x1, bar_y + bar_height],
                                radius=rectangle_radius,
                                outline=text_color,
                                fill=text_color,
                                width=rectangle_width,
                                glow_color=text_color,
                                glow_radius=rectangle_glow_radius,
                            )

                        if duration_seconds > 0:
                            minutes_elapsed, seconds_elapsed = divmod(
                                int(elapsed_duration), 60
                            )
                            minutes_total, seconds_total = divmod(
                                int(duration_seconds), 60
                            )
                            duration_text = f"{minutes_elapsed}:{seconds_elapsed:02} / {minutes_total}:{seconds_total:02}"
                        else:
                            duration_text = "0:00 / 0:00"

                        time_font = self.dynamic_font(
                            duration_text,
                            max_text_width,
                            self.font_path,
                            max_font_size=20,
                        )
                        self.draw_glowing_text(
                            base_image=img,
                            text=duration_text,
                            position=(300, 280),
                            font=time_font,
                            text_color=text_color,
                            glow_color=glow_color,
                            glow_radius=text_glow_radius,
                        )

                        image_bytes = BytesIO()
                        img.save(image_bytes, format="PNG")
                        image_bytes.seek(0)
                        file = discord.File(fp=image_bytes, filename="now_playing.png")
                        embed_color = int(
                            "{:02x}{:02x}{:02x}".format(
                                color1[0], color1[1], color1[2]
                            ),
                            16,
                        )
                        embed = discord.Embed(
                            title="Now Playing on JuiceWRLD API",
                            description=f"[{title}]({track_url})"
                            if track_url
                            else title,
                            color=embed_color,
                        )
                        embed.set_image(url="attachment://now_playing.png")

            message = await ctx.reply(embed=embed, file=file)
            await message.add_reaction("👍")
            await asyncio.sleep(0.5)
            await message.add_reaction("👎")

            def check(reaction, user):
                return user == ctx.author and str(reaction.emoji) in ["👍", "👎"]

            try:
                reaction, user = await self.bot.wait_for(
                    "reaction_add", timeout=15.0, check=check
                )
                if str(reaction.emoji) == "👍":
                    await self.bot.database.log_vote(user.id, "jnp", "up")
                elif str(reaction.emoji) == "👎":
                    await self.bot.database.log_vote(user.id, "jnp", "down")
            except asyncio.TimeoutError:
                logger.debug("No reaction received within the timeout period.")
            except asyncio.CancelledError:
                logger.debug("Reaction task was cancelled.")

        except aiohttp.ClientError:
            await Embeds.custom(
                ctx,
                "There was an error fetching data from JuiceWRLD API. Please try again later.",
                title="Error",
                color=0x36393E,
                reply=True,
            )
        except Exception as e:
            logger.error(f"Error in jnp command: {e}")
            await Embeds.custom(
                ctx,
                "An unexpected error occurred. Please try again later.",
                title="Error",
                color=0x36393E,
                reply=True,
            )

    @juicewrld_api.command(name="link", description="Link your Discord account to JuiceWRLD API using a pairing code.")
    @unified_cooldown(10)
    async def juicewrld_link(self, ctx: commands.Context, code: Optional[str] = None) -> None:
        """Link your Discord account to JuiceWRLD API using a pairing code."""
        prefix = await self.bot.database.get_prefix(ctx.guild.id) if ctx.guild else "!"

        if code and code.lower() == "help":
            embed = discord.Embed(
                title="Link Account Command",
                description="Link your Discord account to your JuiceWRLDAPI account using a pairing code",
                color=0x5865F2,
            )
            embed.add_field(
                name="Usage",
                value=f"`{prefix}jlink <code>` - Link account with pairing code",
                inline=False,
            )
            embed.add_field(
                name="Examples",
                value=f"`{prefix}jlink ABC12345` - Link using code ABC12345",
                inline=False,
            )
            embed.add_field(
                name="How to get a code",
                value="Generate a pairing code from your JuiceWRLDAPI desktop app\nCodes expire after 10 minutes",
                inline=False,
            )
            await ctx.reply(embed=embed)
            return

        if not code:
            await Embeds.error(
                ctx,
                "Please provide a pairing code.",
                title="Missing Code",
                delete_after=None,
                reply=True,
            )
            return

        pairing_code = code.upper()

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{JUICEWRLD_API}/auth/discord/bot-link",
                    json={"code": pairing_code, "discord_user_id": ctx.author.id},
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data and data.get("user"):
                            user_data = data.get("user", {})
                            embed = discord.Embed(
                                title="Account Linked Successfully",
                                description=f'Your Discord account has been linked to **{user_data.get("username", "Unknown")}**',
                                color=0x00FF00,
                                footer=f"You can now use {prefix}jnp to show your currently playing song!",
                            )
                            embed.add_field(
                                name="Username",
                                value=user_data.get("username", "Unknown"),
                                inline=True,
                            )
                            embed.add_field(
                                name="Status", value="Active ✓", inline=True
                            )
                            await ctx.reply(embed=embed)
                            return

                    error_message = "Invalid or expired pairing code"
                    try:
                        error_data = await response.json()
                        if error_data and error_data.get("error"):
                            error_message = error_data.get("error", "Unknown error")
                    except:
                        if response.status == 400:
                            error_message = "Bad request. Please check your pairing code and try again."
                        elif response.status >= 500:
                            error_message = "Server error. Please try again later."

                    embed = discord.Embed(
                        title="Link Failed",
                        description=error_message,
                        color=discord.Color.red(),
                    )
                    embed.add_field(
                        name="Troubleshooting",
                        value="• Make sure the code is correct\n• Codes expire after 10 minutes\n• Generate a new code from your desktop app",
                        inline=False,
                    )
                    await ctx.reply(embed=embed)

        except aiohttp.ClientError:
            await Embeds.error(
                ctx,
                "There was an error connecting to JuiceWRLD API. Please try again later.",
                title="Link Failed",
                delete_after=None,
                reply=True,
            )
        except Exception as e:
            logger.error(f"Error in jlink command: {e}")
            await Embeds.error(
                ctx,
                "An unexpected error occurred. Please try again later.",
                title="Link Failed",
                delete_after=None,
                reply=True,
            )

    async def fetch_song(self, ctx: commands.Context, query: str, allow_unsurfaced: bool = True):
        query = query.replace('’', "'")
        song_list = await self._search_songs(query, allow_unsurfaced=allow_unsurfaced)
        if song_list is None:
            await Embeds.error(
                ctx,
                "Request failed. Please try again later.",
                delete_after=5,
                reply=True,
            )
            return None
        return song_list

    async def _search_songs(
        self,
        query: str,
        *,
        allow_unsurfaced: bool = True,
        session_only: bool = False,
    ) -> list[dict] | None:
        """Search the Juice WRLD API, falling back to broader strategies.

        The API's search parameter can be picky about punctuation,
        leading words, and word order. Try the query directly, then retry
        with cleaned-up variants, and finally score results client-side.
        Returns None only when the API request itself fails.
        """

        def normalize(text: str) -> str:
            return re.sub(r"[^a-z0-9\s]", "", text.lower()).strip()

        def alt_names(song: dict) -> list[str]:
            """Every alternate title the song is known by, minus its main name."""
            names: list[str] = []
            for key in (
                "track_titles",
                "trackTitles",
                "alt_names",
                "altNames",
                "alternative_titles",
                "alternativeTitles",
                "alternate_titles",
                "alternateTitles",
            ):
                value = song.get(key)
                if not value:
                    continue
                entries = value if isinstance(value, (list, tuple, set)) else [value]
                for entry in entries:
                    if isinstance(entry, str) and entry.strip():
                        names.append(entry.strip())
            primary = normalize(song.get("name", ""))
            return [name for name in names if normalize(name) != primary]

        def score(song: dict, query_norm: str) -> float:
            """How well a song matches the query, counting alternate names.

            Each title is scored on its own (instead of joining every
            ``track_titles`` entry into one string and diluting the match), so
            searching an alternate name such as "Stomp 'Em Out" still ranks
            "Burberry Timbs" at the top.
            """
            if not query_norm:
                return 0.0

            candidates = [normalize(song.get("name", ""))]
            candidates.extend(normalize(name) for name in alt_names(song))

            phrase = rf"(?<!\w){re.escape(query_norm)}(?!\w)"
            best = 0.0
            for cand in candidates:
                if not cand:
                    continue
                if cand == query_norm:
                    return 1.0
                if re.search(phrase, cand):
                    best = max(best, 0.9)
                best = max(best, SequenceMatcher(None, query_norm, cand).ratio())
            return best

        def keep(song: dict) -> bool:
            leak_type = (song.get("leak_type") or "").lower()
            if not leak_type:
                return False
            is_session = "session" in leak_type
            if session_only:
                return is_session
            return not is_session

        queries = [query]
        stripped = query.strip("'")
        if stripped != query:
            queries.append(stripped)
        # Drop a leading single letter word like "k" and retry
        parts = query.split()
        if len(parts) > 1 and len(parts[0]) <= 2 and parts[0] != "go":
            queries.append(" ".join(parts[1:]))
            queries.append(" ".join(parts[1:]) + " " + parts[0])
        # Try without parentheses content
        no_parens = re.sub(r"\s*\([^)]*\)", "", query).strip()
        if no_parens and no_parens != query:
            queries.append(no_parens)

        seen_ids = set()
        all_results: list[dict] = []
        for q in queries:
            async with self.session.get(
                JUICEWRLD_API + "/juicewrld/songs/",
                params={"search": q, "page_size": SEARCH_PAGE_SIZE},
            ) as response:
                if response.status != 200:
                    return None
                data = await response.json()

            for song in data.get("results", []):
                if keep(song):
                    if song.get("id") not in seen_ids:
                        seen_ids.add(song.get("id"))
                        all_results.append(song)

        if not allow_unsurfaced:
            all_results = [s for s in all_results if not self._is_unsurfaced(s)]

        query_norm = normalize(query)
        scored = sorted(
            ((score(s, query_norm), s) for s in all_results),
            key=lambda x: x[0],
            reverse=True,
        )

        # If the direct query returned nothing useful, return the best fuzzy
        # matches so the user still gets a selection. Require a modest threshold
        # so completely unrelated songs don't appear.
        if scored and scored[0][0] >= 0.4:
            return [s for score_val, s in scored if score_val >= 0.25]
        # No confident match: still hand back every candidate, best-first, so an
        # alternate-name match is not buried after unrelated results.
        return [s for _, s in scored] if scored else all_results

    async def fetch_random_playable_song(self, max_retries: int = 8) -> dict | None:
        """Hit /juicewrld/radio/random/ until we get a song with audio.

        The radio endpoint occasionally returns `unsurfaced` tracks that have
        metadata only — those 404 on the download endpoint. Retry a few times
        before giving up.
        """
        for _ in range(max_retries):
            async with self.session.get(
                f"{JUICEWRLD_API}/juicewrld/radio/random/"
            ) as response:
                if response.status != 200:
                    return None
                data = await response.json()

            song_data = data.get("song")
            if not song_data:
                continue
            if self._is_unsurfaced(song_data):
                continue
            return data
        return None

    async def fetch_session(self, ctx: commands.Context, query: str, allow_unsurfaced: bool = True):
        query = query.replace('’', "'")
        song_list = await self._search_songs(
            query, allow_unsurfaced=allow_unsurfaced, session_only=True
        )
        if song_list is None:
            await Embeds.error(
                ctx,
                "Request failed. Please try again later.",
                delete_after=5,
                reply=True,
            )
            return None
        return song_list

    async def fetch_session_files(self, song: dict) -> tuple[list[str], list[str]]:
        """Browse for a song's session archives and session edits in one request.

        Returns ``(session_downloads, session_edits)`` where ``session_downloads``
        are the ``Studio Sessions/{Era}/{Song}.zip`` archives and ``session_edits``
        are the ``Session Edits/{Song}.mp3`` files.
        """
        name = song.get("name") or ""
        if not name:
            return [], []

        async with self.session.get(
            JUICEWRLD_API + "/juicewrld/files/browse/", params={"search": name}
        ) as response:
            if response.status != 200:
                return [], []

            data = await response.json()

        files = [
            item.get("path", "")
            for item in data.get("items", [])
            if item.get("type") == "file"
        ]

        session_paths = [p for p in files if p.startswith("Studio Sessions/")]
        session_edits = [p for p in files if p.startswith("Session Edits/")]

        era_name = song.get("era", {}).get("name", "")
        album = self.ALBUMS.get(era_name)
        era_match = (album["name"] if album else era_name).lower()
        if era_match:
            scoped = [p for p in session_paths if era_match in p.lower()]
            if scoped:
                session_paths = scoped

        return session_paths, session_edits

    def get_file_names(self, file_names: str) -> list[str]:
        lines = [line.strip() for line in file_names.splitlines() if line.strip()]

        if not lines or "N/A" in lines:
            return []

        if len(lines) == 1 and ":" not in lines[0]:
            return [lines[0]]

        return [
            line.split(":", 1)[1].strip()
            for line in lines
            if ":" in line
            and any(x in line for x in ("File Name", "Clean", "Explicit"))
        ]
    
    async def get_downloads(
        self, file_names: list[str], length: str, era_hint: str | None = None
    ) -> list[str]:
        """Resolve the original (OG) file paths behind a song's metadata names.

        Shares the songinfo storage resolver so *every* stored copy of a take is
        found. The old exact-search plus ±1s duration filter missed files whose
        metadata name/version differed from the archive (No Hands) or whose
        length didn't line up with the entry (GoPro).
        """
        if not file_names:
            return []

        cog = self.bot.get_cog("SongInfo")
        api = getattr(cog, "api", None) if cog is not None else None
        if api is None:
            api = _fallback_file_api()

        results: list[str] = []
        seen: set[str] = set()
        for file_name in file_names:
            for path in await api.resolve_storage_paths(file_name, era_hint):
                if "original files" not in path.lower():
                    continue
                key = path.casefold()
                if key in seen:
                    continue
                seen.add(key)
                results.append(path)

        return results

    def duration_to_seconds(self, duration: str):
        try:
            m, s = duration.split(":")
            return int(m) * 60 + int(s)
        except Exception:
            return 0
        
    def parse_dates(self, text: str) -> datetime | None:
        if not text:
            return None

        match = re.search(r'([A-Za-z]+ \d{1,2}(?:st|nd|rd|th)?, \d{4})', text)

        if not match:
            return None

        date_str = match.group(1)
        date_str = re.sub(r'(\d{1,2})(st|nd|rd|th)', r'\1', date_str)

        try:
            dt = datetime.strptime(date_str, '%B %d, %Y')
            dt = dt.replace(hour=14, minute=0, second=0, tzinfo=ZoneInfo("America/New_York"))
            return dt
        except ValueError:
            return None

    async def create_song_view(
        self,
        song_data: dict,
        random_leak: bool = False,
        session_downloads: list[str] | None = None,
        session_edits: list[str] | None = None,
        guild_id: int | None = None,
        author_id: int | None = None,
    ):
        class SongContainer(discord.ui.Container):
            ALBUMS = {
                'jute':                 {'name': 'JUICED UP THE EP', 'color': '#FFE602'},
                'LND':                  {'name': 'Legends Never Die', 'color': '#F700FF'},
                'afflictions':          {'name': 'affliction', 'color': '#000000'},
                'bdm':                  {'name': 'BINGEDRINKINGMUSIC', 'color': '#000000'},
                'HIH 999':              {'name': 'Heartbroken In Hollywood 9 9 9', 'color': '#FF653E'},
                'jw 999':               {'name': 'JuiceWRLD 9 9 9', 'color': '#FF2C2C'},
                'ND':                   {'name': 'NOTHINGS DIFFERENT </3', 'color': '#FF8800'},
                'GB&GR':                {'name': 'Goodbye & Good Riddance', 'color': '#008CFF'},
                'GB&GR (AE)':           {'name': 'Goodbye & Good Riddance (Anniversary Edition)', 'color': '#008CFF'},
                'GB&GR (5YAE)':         {'name': 'Goodbye & Good Riddance (5 Year Anniversary Edition)', 'color': '#008CFF'},
                'WOD':                  {'name': 'WRLD ON DRUGS', 'color': '#00FF94'},
                'DRFL':                 {'name': 'Death Race For Love', 'color': '#FF9900'},
                'DRFL (BTV)':           {'name': 'Death Race For Love (Bonus Track Version)', 'color': '#FF9900'},
                'OUT':                  {'name': 'Outsiders', 'color': '#2B2B2B'},
                'POST':                 {'name': 'Posthumous', 'color': '#00CCFF'},
                'TPP':                  {'name': 'The Pre-Party', 'color': '#EA00FF'},
                'TPP (EE)':             {'name': 'The Pre-Party (Extended Edition)', 'color': '#EA00FF'},
                'FD':                   {'name': 'Fighting Demons', 'color': '#2E2E2E'},
                'FD (CE)':              {'name': 'Fighting Demons (Complete Edition)', 'color': '#2E2E2E'},
                'FD (EE)':              {'name': 'Fighting Demons (Extended Edition)', 'color': '#2E2E2E'},
                'FD (DDE)':             {'name': 'Fighting Demons (Digital Deluxe Edition)', 'color': '#2E2E2E'},
                'TPNE':                 {'name': 'The Party Never Ends', 'color': '#CC00FF'},
            }

            FIELDS = {
                'file_names':           '**File Name**',
                'session_titles':       '**Session Title**',
                'session_tracking':     '**Session Tracking**',
                'instrumentals':        '**Instrumentals**',
                'recording_locations':  '**Recording Location**',
                'record_dates':         '**Recorded**',
                'preview_date':         '**Previewed**',
                'date_leaked':          '**Surfaced**',
                'release_date':         '**Released**',
                'length':               '**Length**',
                'leak_type':            '**Category**',
                'bitrate':              '**Available Files**',
            }

            RANDOM_LEAK_FIELDS = {
                'record_dates':         '**Recorded**',
                'preview_date':         '**Previewed**',
                'date_leaked':          '**Surfaced**',
                'release_date':         '**Released**',
                'length':               '**Length**',
                'bitrate':              '**Available Files**',
            }

            def __init__(
                self,
                song: dict,
                downloads = None,
                random_leak: bool = False,
                session_downloads = None,
                session_edits = None,
                bot = None,
                guild_id = None,
                unsurfaced: bool = False,
                expanded: bool = False,
                author_id: int | None = None,
            ):
                name = song.get('name')
                track_titles = [t for t in song.get('track_titles', []) if t != name]
                producers = song.get('producers')
                engineers = song.get('engineers')
                era_name = song.get('era', {}).get('name', 'N/A')
                _image_url = song.get('image_url')
                image_url = f'{JUICEWRLD_API}{_image_url}' if _image_url != '' else 'https://discord.com/example.png'

                album = self.ALBUMS.get(era_name)
                accent_color = int(album['color'].lstrip('#'), 16) if album else 0x2B2D31
                
                super().__init__(accent_color=accent_color)

                # The mp3 button mode is read per server when the buttons are
                # built, so the container has to know where it was posted.
                self.bot = bot
                self.guild_id = guild_id
                self.unsurfaced = unsurfaced
                # The Info button belongs to whoever the card was posted for;
                # anyone else pressing it is turned away.
                self.author_id = author_id
                # The metadata block is hidden behind the Info button by default
                # so the card stays short; these hold everything needed to
                # rebuild it once that button is pressed.
                self._expanded = expanded
                self._song = song
                self._downloads = downloads
                self._random_leak = random_leak
                self._session_downloads = session_downloads
                self._session_edits = session_edits

                self._build_container(
                    song, downloads, random_leak,
                    name, track_titles, producers, engineers,
                    era_name, album, image_url, session_downloads, session_edits
                )

            def _build_container(
                self, song, downloads, random_leak, name, track_titles,
                producers, engineers, era_name, album, image_url,
                session_downloads=None, session_edits=None
            ):
                thumbnail = discord.ui.Section(
                    accessory=discord.ui.Thumbnail(media=image_url)
                )
                thumbnail.add_item(
                    discord.ui.TextDisplay(
                        f'### {name}\n'
                        f'-# Alt Name(s): **{', '.join(track_titles) if track_titles else 'N/A'}**\n'
                        f'-# Engineer(s): **{engineers}**\n'
                        f'-# Producer(s): **{producers}**'
                    )
                )
                self.add_item(thumbnail)
                self._add_separator()

                # Everything below the header is metadata, and it only shows
                # once the Info button has been pressed.
                if self._expanded:
                    if era_name != '':
                        self.add_item(
                            discord.ui.TextDisplay(
                                f'**Era**\n{album['name'] if album else era_name}\n'
                            )
                        )

                    field_map = self.FIELDS if not random_leak else self.RANDOM_LEAK_FIELDS
                    self._add_song_fields(song, field_map)

                MAIN_URL = f'{JUICEWRLD_API}/juicewrld/files/download/?path='
                TRACKER = discord.ui.Button(
                    label='Tracker', 
                    emoji='\U0001F517', 
                    url=f'{JUICEWRLD_API}/'
                )

                if self.unsurfaced:
                    # Unsurfaced tracks are metadata only and have no audio
                    # on the server; their file rows point at whatever else
                    # the archive matched, so hide every download button.
                    self.add_item(
                        discord.ui.TextDisplay(
                            '-# No files available — this track is **unsurfaced** (metadata only).'
                        )
                    )
                else:
                    download_budget = max(0, 40 - self._total_count - 4)
                    rows, omitted = self._create_download_rows(
                        downloads, song, MAIN_URL, download_budget,
                        session_downloads, session_edits
                    )

                    for row in rows:
                        self._add(row)

                    if omitted:
                        self.add_item(
                            discord.ui.TextDisplay(
                                f'-# +{omitted} more file(s) not shown — '
                                f'[view all on the tracker]({JUICEWRLD_API}/)'
                            )
                        )

                self._add_separator()
                action_row = discord.ui.ActionRow()
                action_row.add_item(TRACKER)
                info_button = discord.ui.Button(
                    label='Info',
                    emoji='⬆️' if self._expanded else '⬇️',
                    style=discord.ButtonStyle.secondary,
                )
                info_button.callback = self._on_info
                action_row.add_item(info_button)
                self.add_item(action_row)

            def _add(self, item):
                """Add ``item``, skipping a divider that would double up."""
                if isinstance(item, discord.ui.Separator):
                    if self.children and isinstance(
                        self.children[-1], discord.ui.Separator
                    ):
                        return
                self.add_item(item)

            def _add_separator(self):
                self._add(discord.ui.Separator())

            async def _on_info(self, interaction: discord.Interaction):
                """Expand or collapse the card's metadata block in place."""
                if self.author_id is not None and interaction.user.id != self.author_id:
                    await interaction.response.send_message(
                        '❌ Only the person who ran the command can use this button.',
                        ephemeral=True,
                    )
                    return
                expanded = not self._expanded
                layout = discord.ui.LayoutView(timeout=None)
                layout.add_item(
                    self.__class__(
                        self._song,
                        self._downloads,
                        self._random_leak,
                        self._session_downloads,
                        self._session_edits,
                        bot=self.bot,
                        guild_id=self.guild_id,
                        unsurfaced=self.unsurfaced,
                        expanded=expanded,
                        author_id=self.author_id,
                    )
                )
                await interaction.response.edit_message(view=layout)

            def _add_song_fields(self, song: dict, field_map: dict):
                for field_key, field_label in field_map.items():
                    value = song.get(field_key)
                    if value:
                        value = (
                            value
                            .replace('Recorded', '')
                            .replace('First Previewed', '')
                            .replace('Surfaced', '')
                            .replace('Released', '')
                            .strip()
                        )
                        if value in ('N/A', 'Unavailable'):
                            continue

                        self.add_item(discord.ui.TextDisplay(f'{field_label}\n{value}'))

            def _create_download_rows(self, downloads, song, main_url, budget,
                                      session_downloads=None, session_edits=None):
                rows = []
                used = 0
                omitted = 0

                def remaining():
                    return budget - used

                if session_downloads:
                    brows, bused, bomit = self._file_button_rows(
                        session_downloads, main_url, max(0, remaining() - 2)
                    )
                    if brows:
                        rows.append(discord.ui.Separator())
                        rows.append(discord.ui.TextDisplay('**Session Download(s)**'))
                        rows.extend(brows)
                        used += 2 + bused
                    omitted += bomit
                else:
                    download = song.get('path')
                    if download != '':
                        ext = download.rsplit('.', 1)[-1].upper()
                        if remaining() >= 4:
                            rows.append(discord.ui.Separator())
                            if ext.lower() in ['zip', 'rar', '7z']:
                                rows.append(discord.ui.TextDisplay('**Session Download(s)**'))
                            else:
                                rows.append(discord.ui.TextDisplay('**Tagged File(s)**'))
                            main_row = discord.ui.ActionRow()
                            main_button = mp3_buttons.make_download_button(
                                self.bot,
                                guild_id=self.guild_id,
                                label=ext,
                                url=main_url + quote(download),
                                is_mp3=ext.lower() == "mp3",
                                filename=mp3_buttons.mp3_filename(
                                    os.path.basename(download), "song"
                                ),
                            )
                            if main_button:
                                main_row.add_item(main_button)
                            rows.append(main_row)
                            used += 4
                        else:
                            omitted += 1

                if session_edits:
                    brows, bused, bomit = self._file_button_rows(
                        session_edits, main_url, max(0, remaining() - 1)
                    )
                    if brows:
                        rows.append(discord.ui.TextDisplay('**Session Edit(s)**'))
                        rows.extend(brows)
                        used += 1 + bused
                    omitted += bomit

                if downloads:
                    brows, bused, bomit = self._file_button_rows(
                        downloads, main_url, max(0, remaining() - 1),
                        lambda path, ext: f'OG {ext}' if 'Original Files' in path else ext
                    )
                    if brows:
                        rows.append(discord.ui.TextDisplay('**Original File(s)**'))
                        rows.extend(brows)
                        used += 1 + bused
                    omitted += bomit

                return rows, omitted

            def _file_button_rows(self, paths, main_url, budget, label_func=None):
                rows = []
                used = 0
                idx = 0
                while idx < len(paths):
                    chunk = paths[idx:idx + 5]
                    cost = 1 + len(chunk)
                    if used + cost > budget:
                        break
                    row = discord.ui.ActionRow()
                    for path in chunk:
                        ext = path.rsplit('.', 1)[-1].upper()
                        label = label_func(path, ext) if label_func else ext
                        button = mp3_buttons.make_download_button(
                            self.bot,
                            guild_id=self.guild_id,
                            label=label,
                            url=main_url + quote(path),
                            is_mp3=path.rsplit('.', 1)[-1].lower() == "mp3",
                            filename=mp3_buttons.mp3_filename(
                                os.path.basename(path), "song"
                            ),
                        )
                        if button:
                            row.add_item(button)
                    rows.append(row)
                    used += cost
                    idx += len(chunk)
                return rows, used, len(paths) - idx

        unsurfaced = self._is_unsurfaced(song_data)
        file_names = self.get_file_names(song_data.get('file_names'))
        era_raw = song_data.get('era')
        era_hint = era_raw.get('name') if isinstance(era_raw, dict) else None
        # Unsurfaced tracks have no audio, so skip the storage sweep entirely
        # rather than risk matching a different song's files.
        downloads = [] if unsurfaced else await self.get_downloads(
            file_names, song_data.get('length', ''), era_hint
        )

        layout_view = discord.ui.LayoutView(timeout=None)
        layout_view.add_item(
            SongContainer(
                song_data, downloads, random_leak, session_downloads, session_edits,
                bot=self.bot, guild_id=guild_id,
                unsurfaced=unsurfaced,
                author_id=author_id,
            )
        )

        return layout_view

    @commands.hybrid_command(name='groupbuy', aliases=['gb', 'gbinfo', 'groupbuyinfo'], description='Look up a track in the GB/PB tracker')
    @commands.guild_only()
    @unified_cooldown(10)
    async def groupbuy(self, ctx: commands.Context, *, query: str = None):
        async with ctx.typing():
            try:
                entries = await GROUPBUY_CATALOG.load(self.session)
            except Exception:
                logger.warning("Could not load the GB/PB tracker", exc_info=True)
                entries = []

        if not entries:
            return await Embeds.send_warning_embed(
                ctx.channel,
                ctx.author,
                'The **GB/PB tracker** is unavailable right now — try again shortly.',
            )

        if not query:
            view = GroupbuyView(
                self,
                ctx.author,
                entries,
                years=GROUPBUY_CATALOG.years(entries),
                hint='### GB/PB Tracker\n-# Choose a song or year below.',
            )
            view.message = await ctx.send(view=view)
            return

        matches = GROUPBUY_CATALOG.search(entries, query)
        if not matches:
            return await Embeds.send_warning_embed(
                ctx.channel,
                ctx.author,
                f'I couldnt find a track, era or year matching: `{query}`',
            )

        # A year or era is a browse request, so open on the whole list instead
        # of on whichever track happens to sort first.
        browsing = (query.isdigit() and len(query) == 4) or bool(
            GROUPBUY_CATALOG.for_era(entries, query)
        )
        if browsing:
            view = GroupbuyView(
                self,
                ctx.author,
                entries,
                candidates=matches,
                hint=f'### {query} Group Buys / Personal Buys\n-# Choose a track below.',
            )
        else:
            view = GroupbuyView(self, ctx.author, entries, current=matches[0], candidates=matches)

        view.message = await ctx.send(view=view)

    # --- GB/PB tracker helpers ---------------------------------------------
    #: Sheet era codes that don't line up with the album keys in ``ALBUMS``.
    _GROUPBUY_ERA_ALBUMS = {
        'JUTE': 'jute',
        'AFF': 'afflictions',
        'BDM': 'bdm',
        'ND </3': 'ND',
        'JW 9 9 9': 'jw 999',
        'HIH 999': 'HIH 999',
    }

    def groupbuy_era_colour(self, era: str) -> int:
        album = self.ALBUMS.get(era) or self.ALBUMS.get(self._GROUPBUY_ERA_ALBUMS.get(era, ''))
        if album:
            return int(album['color'].lstrip('#'), 16)
        return 0x2B2D31

    def groupbuy_era_emoji(self, era: str):
        try:
            return emoji_for_era(self.bot, self._GROUPBUY_ERA_ALBUMS.get(era, era))
        except Exception:
            return None

    def groupbuy_option_label(self, entry: Groupbuy) -> str:
        label = entry.name
        if entry.akas:
            label = f"{entry.name} ({', '.join(entry.akas)})"
        return label[:100] or 'Unknown'

    def _groupbuy_api_index(self) -> dict[str, dict]:
        """Map normalised API titles to songs so a sheet row can borrow its art."""
        if self._groupbuy_api_index_cache is None:
            index: dict[str, dict] = {}
            for song in Cache.get_songs() or []:
                titles = [song.get('name'), *(song.get('track_titles') or [])]
                for title in titles:
                    key = normalise_song_key(title)
                    if key and key not in index:
                        index[key] = song
            self._groupbuy_api_index_cache = index
        return self._groupbuy_api_index_cache

    def _resolve_groupbuy_song(self, entry: Groupbuy) -> dict | None:
        return self._groupbuy_api_index().get(normalise_song_key(entry.name))

    def groupbuy_card_view(self, entry: Groupbuy) -> discord.ui.LayoutView:
        """A one-off view holding just the card, used for other users' copies."""
        view = discord.ui.LayoutView(timeout=300)
        container = discord.ui.Container(accent_color=self.groupbuy_era_colour(entry.era))
        self.build_groupbuy_card(container, entry)
        view.add_item(container)
        return view

    def build_groupbuy_card(self, container: discord.ui.Container, entry: Groupbuy) -> None:
        """Render one tracker row in the layout the tracker bot's card uses."""
        song = self._resolve_groupbuy_song(entry)
        # The sheet's AKA names win; when a row has none, borrow the API's.
        akas = list(entry.akas)
        if not akas and song:
            akas = [t for t in (song.get('track_titles') or []) if t and t != song.get('name')]

        header = [f'### {entry.name}']
        if akas:
            header.append(f'AKA: {", ".join(akas)}')
        if entry.price:
            header.append(f'Price: **{format_price(entry.price)}**')
        if entry.sold_with:
            header.append(f'Sold with: {", ".join(entry.sold_with)}')
        header_text = "\n".join(header)

        image_url = None
        if song:
            raw = song.get('image_url')
            if raw:
                image_url = raw if str(raw).startswith('http') else f'{JUICEWRLD_API}{raw}'

        if image_url:
            section = discord.ui.Section(accessory=discord.ui.Thumbnail(media=image_url))
            section.add_item(discord.ui.TextDisplay(header_text))
            container.add_item(section)
        else:
            container.add_item(discord.ui.TextDisplay(header_text))

        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(f'**Era**\n{entry.era}'))
        if entry.start_date:
            container.add_item(discord.ui.TextDisplay(f'**Start Date**\n{entry.start_date}'))
        if entry.end_date:
            container.add_item(discord.ui.TextDisplay(f'**End Date**\n{entry.end_date}'))
        container.add_item(discord.ui.TextDisplay(
            '**Info**\n'
            f'Finished: {_groupbuy_flag(entry.finished)}\n'
            f'OG File: {_groupbuy_flag(entry.og_file)}\n'
            f'Blind: {_groupbuy_flag(entry.blind)}'
        ))
        if entry.notes:
            container.add_item(discord.ui.TextDisplay(f'**Notes**\n{entry.notes}'))

    @commands.hybrid_command("leak", description="Search for a Juice WRLD leak by name")
    @commands.guild_only()
    @unified_cooldown(10)
    async def leak(self, ctx: commands.Context, *, query: str = None):
        if not query:
            await Embeds.error(
                ctx,
                f"**Missing argument:** query\n**Usage:** {usage}",
                title="Error!",
                delete_after=5,
                reply=True,
            )
            return
        song_list = await self.fetch_song(ctx, query, allow_unsurfaced=True)
        if song_list is None:
            return

        if len(song_list) == 1:
            layout_view = await self.create_song_view(
                song_list[0],
                guild_id=ctx.guild.id if ctx.guild else None,
                author_id=ctx.author.id,
            )
            await ctx.reply(view=layout_view)

        elif len(song_list) > 1:
            # ``_search_songs`` already returns the closest match first, so the
            # order is kept rather than re-sorted by track title (which could
            # push an alternate-name match past the 25-option limit).
            results = song_list[:25]
            song_map = {str(song["id"]): song for song in results}

            options = [
                discord.SelectOption(
                    label=(
                        lambda t: f"{t[0]} ({', '.join(t[1:])})" if len(t) > 1 else (t[0] if t else "Unknown")
                    )(song.get("track_titles", []))[:100],
                    value=str(song["id"]),
                    emoji=emoji_for_song(self.bot, song),
                )
                for song in results
            ]

            class SongSelect(discord.ui.Select):
                def __init__(self, options, song_map, author, cog):
                    self.song_map = song_map
                    self.author = author
                    self.cog = cog
                    super().__init__(
                        placeholder="Select a song...",
                        min_values=1,
                        max_values=1,
                        options=options,
                    )

                async def callback(self, itn: discord.Interaction):
                    song_id = self.values[0]
                    song = self.song_map[song_id]

                    view = await self.cog.create_song_view(
                        song, guild_id=itn.guild_id, author_id=itn.user.id
                    )

                    if self.author != itn.user:
                        return await itn.response.send_message(
                            view=view, ephemeral=True
                        )

                    await itn.response.edit_message(embed=None, view=view)

            view = discord.ui.View(timeout=None)
            view.add_item(SongSelect(options, song_map, ctx.author, self))

            embed = discord.Embed(
                description=f"{ctx.author.mention}: Multiple **selections** found with your **search**",
                color=None,
            )
            await ctx.reply(embed=embed, view=view)

        else:
            return await Embeds.send_warning_embed(
                ctx.channel,
                ctx.author,
                f"I couldnt find a song with the name: `{query}`",
            )

    @commands.hybrid_command("session", aliases=["sessions"], description="Search for a Juice WRLD session by name")
    @commands.guild_only()
    @unified_cooldown(10)
    async def session(self, ctx: commands.Context, *, query: str = None):
        if not query:
            await Embeds.error(
                ctx,
                f"**Missing argument:** query\n**Usage:** {usage}",
                title="Error!",
                delete_after=5,
                reply=True,
            )
            return
        song_list = await self.fetch_session(ctx, query)
        if song_list is None:
            return

        if len(song_list) == 1:
            session_downloads, session_edits = await self.fetch_session_files(
                song_list[0]
            )
            layout_view = await self.create_song_view(
                song_list[0],
                session_downloads=session_downloads,
                session_edits=session_edits,
                guild_id=ctx.guild.id if ctx.guild else None,
                author_id=ctx.author.id,
            )
            await ctx.reply(view=layout_view)

        elif len(song_list) > 1:
            results = sorted(song_list, key=lambda s: s.get("track_titles"))[:25]
            song_map = {str(song["id"]): song for song in results}

            options = [
                discord.SelectOption(
                    label=(
                        lambda t: f"{t[0]} ({', '.join(t[1:])})" if len(t) > 1 else (t[0] if t else "Unknown")
                    )(song.get("track_titles", []))[:100],
                    value=str(song["id"]),
                    emoji=emoji_for_song(self.bot, song),
                )
                for song in results
            ]

            class SongSelect(discord.ui.Select):
                def __init__(self, options, song_map, author, cog):
                    self.song_map = song_map
                    self.author = author
                    self.cog = cog
                    super().__init__(
                        placeholder="Select a song...",
                        min_values=1,
                        max_values=1,
                        options=options,
                    )

                async def callback(self, itn: discord.Interaction):
                    song_id = self.values[0]
                    song = self.song_map[song_id]

                    session_downloads, session_edits = (
                        await self.cog.fetch_session_files(song)
                    )
                    view = await self.cog.create_song_view(
                        song,
                        session_downloads=session_downloads,
                        session_edits=session_edits,
                        guild_id=itn.guild_id,
                        author_id=itn.user.id,
                    )

                    if self.author != itn.user:
                        return await itn.response.send_message(
                            view=view, ephemeral=True
                        )

                    await itn.response.edit_message(embed=None, view=view)

            view = discord.ui.View(timeout=None)
            view.add_item(SongSelect(options, song_map, ctx.author, self))

            embed = discord.Embed(
                description=f"{ctx.author.mention}: Multiple **selections** found with your **search**",
                color=None,
            )
            await ctx.reply(embed=embed, view=view)

        else:
            return await Embeds.send_warning_embed(
                ctx.channel,
                ctx.author,
                f"I couldnt find a song with the name: `{query}`",
            )

    def build_lyrics_embeds(self, song: dict) -> list[discord.Embed] | None:
        """build one embed per <=4000 character chunk of a song's lyrics

        returns ``None`` when the song has no lyrics
        """
        lyrics = (song.get("lyrics") or "").strip()
        if not lyrics:
            return None

        name = song.get("name") or "Unknown"
        image_url = song.get("image_url") or ""
        thumbnail = JUICEWRLD_API + image_url if image_url else None

        chunks: list[str] = []
        current = ""
        for line in lyrics.split("\n"):
            if current and len(current) + len(line) + 1 > 4000:
                chunks.append(current.rstrip("\n"))
                current = ""
            current += line + "\n"
        if current.strip():
            chunks.append(current.rstrip("\n"))

        embeds = []
        total = len(chunks)
        for i, chunk in enumerate(chunks):
            embed = discord.Embed(
                title=f"{name} — Lyrics" if i == 0 else None,
                description=chunk[:4096],
                color=discord.Color.blurple(),
            )
            if thumbnail:
                embed.set_thumbnail(url=thumbnail)
            if total > 1:
                embed.set_footer(text=f"Part {i + 1}/{total}")
            embeds.append(embed)

        return embeds

    @commands.hybrid_command(name="lyrics", aliases=["ly"], description="Get the lyrics of a Juice WRLD song")
    @commands.guild_only()
    @unified_cooldown(10)
    async def lyrics(self, ctx: commands.Context, *, query: Optional[str] = None):
        if not query:
            await Embeds.custom(
                ctx,
                "Please provide a song name to get lyrics for.",
                title="Error",
                color=0x36393E,
                reply=True,
            )
            return
        try:
            song_list = await self.fetch_song(ctx, query)
            if song_list is None:
                return

            if len(song_list) == 1:
                song = song_list[0]
                embeds = self.build_lyrics_embeds(song)
                if embeds is None:
                    return await Embeds.send_warning_embed(
                        ctx.channel,
                        ctx.author,
                        f"**{song.get('name', 'That song')}** has no **lyrics** available",
                    )

                await ctx.reply(embed=embeds[0])
                for embed in embeds[1:]:
                    await ctx.send(embed=embed)
                return

            if len(song_list) > 1:
                results = sorted(
                    song_list,
                    key=lambda s: (s.get("track_titles") or [""])[0].lower(),
                )[:25]
                song_map = {str(song["id"]): song for song in results}

                def _label(song: dict) -> str:
                    titles = song.get("track_titles") or []
                    if not titles:
                        return (song.get("name") or "Unknown")[:100]
                    if len(titles) == 1:
                        return titles[0][:100]
                    return f"{titles[0]} ({', '.join(titles[1:])})"[:100]

                options = [
                    discord.SelectOption(
                        label=_label(song),
                        value=str(song["id"]),
                        emoji=emoji_for_song(self.bot, song),
                    )
                    for song in results
                ]

                class SongSelect(discord.ui.Select):
                    def __init__(self, options, song_map, author, cog):
                        self.song_map = song_map
                        self.author = author
                        self.cog = cog
                        super().__init__(
                            placeholder="Select a song...",
                            min_values=1,
                            max_values=1,
                            options=options,
                        )

                    async def callback(self, itn: discord.Interaction):
                        try:
                            song_id = self.values[0]
                            song = self.song_map[song_id]

                            embeds = self.cog.build_lyrics_embeds(song)
                            if embeds is None:
                                return await Embeds.custom(
                                    itn,
                                    f"**{song.get('name', 'That song')}** has no **lyrics** available",
                                    color=discord.Color.yellow(),
                                    ephemeral=True,
                                )

                            await itn.response.send_message(
                                embed=embeds[0], ephemeral=True
                            )
                            for embed in embeds[1:]:
                                await itn.followup.send(embed=embed, ephemeral=True)
                        except Exception as e:
                            if not itn.response.is_done():
                                await itn.response.send_message(
                                    f"Something broke while sending lyrics: `{e}`",
                                    ephemeral=True,
                                )
                            else:
                                await itn.followup.send(
                                    f"Something broke while sending lyrics: `{e}`",
                                    ephemeral=True,
                                )

                view = discord.ui.View(timeout=None)
                view.add_item(SongSelect(options, song_map, ctx.author, self))

                embed = discord.Embed(
                    title="Multiple songs found",
                    description=f"{ctx.author.mention}: I found **{len(results)}** songs matching `{query}`. Pick one below.",
                    color=discord.Color.blurple(),
                )
                await ctx.reply(embed=embed, view=view)
                return

            return await Embeds.send_warning_embed(
                ctx.channel,
                ctx.author,
                f"I couldn't find a song with the name: `{query}`",
            )
        except Exception as e:
            await Embeds.send_error_embed(
                ctx.channel,
                ctx.author,
                f"The lyrics command crashed: `{type(e).__name__}: {e}`",
            )
            raise

    # ------------------------------------------------------------------
    # Reverse lyrics search (!lyricfind)
    # ------------------------------------------------------------------
    @staticmethod
    def _normalise_lyrics(text: str) -> str:
        text = (text or "").lower().replace("’", "'").replace("‘", "'")
        text = re.sub(r"[^a-z0-9\s']", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    async def _fetch_song_by_id(self, song_id: int) -> dict | None:
        try:
            async with self.session.get(
                f"{JUICEWRLD_API}/juicewrld/songs/{int(song_id)}/"
            ) as response:
                if response.status != 200:
                    return None
                return await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None

    def _read_lyrics_index_file(self) -> list[dict] | None:
        try:
            with open(LYRICS_INDEX_PATH, "r", encoding="utf-8") as handle:
                blob = json.load(handle)
        except (OSError, ValueError):
            return None
        built = float(blob.get("built_at") or 0)
        if time.time() - built > LYRICS_INDEX_TTL:
            return None
        songs = blob.get("songs")
        return songs if isinstance(songs, list) and songs else None

    def _write_lyrics_index_file(self, songs: list[dict]) -> None:
        try:
            directory = os.path.dirname(LYRICS_INDEX_PATH)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(LYRICS_INDEX_PATH, "w", encoding="utf-8") as handle:
                json.dump({"built_at": time.time(), "songs": songs}, handle)
        except OSError:
            logger.warning("Could not write the lyrics index cache")

    def _describe_lyrics_cache(self) -> str:
        """What the lyric corpus looks like right now, for the opening card."""
        try:
            age = max(0.0, time.time() - os.path.getmtime(LYRICS_INDEX_PATH)) / 3600
        except OSError:
            return "not built yet"
        loaded = (
            f"{len(self._lyrics_cache)} song(s) in memory"
            if self._lyrics_cache
            else "on disk, not loaded"
        )
        return f"{loaded} · file written {age:.1f}h ago"

    async def _reload_lyrics_cache(
        self,
        target: Optional[str],
        full: bool,
        progress: cache_registry.ProgressFn,
    ) -> ReloadResult:
        """Delete the lyric corpus and sweep the API for it again.

        The corpus is one blob rather than per-song records, so a reload is
        all-or-nothing and a targeted query has nothing smaller to work with.
        """
        result = ReloadResult(label="Lyric search index")
        if target is not None:
            result.note = "the corpus is one blob; nothing to target"
            return result
        warm = getattr(self, "_lyrics_warm_task", None)
        if warm is not None and not warm.done():
            result.note = "the startup sweep is still running; try again shortly"
            return result

        result.scope = "the whole lyric corpus"
        self._lyrics_cache = []
        self._lyrics_ready = False
        try:
            os.remove(LYRICS_INDEX_PATH)
            result.forgotten = 1
        except OSError:
            pass

        await progress("Sweeping every song for lyrics (this one is slow)...")
        corpus = await self._lyrics_index()
        result.restored = len(corpus)
        if not corpus:
            result.note = "the sweep came back with no lyrics"
        logger.info("[Music] Lyrics index reloaded (%d songs)", result.restored)
        return result

    async def _build_lyrics_index(self) -> list[dict]:
        """Sweep every song once, keeping the ones that have lyrics."""
        songs: list[dict] = []
        page = 1
        while True:
            async with self.session.get(
                JUICEWRLD_API + "/juicewrld/songs/",
                params={"page": page, "page_size": SEARCH_PAGE_SIZE},
            ) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")
                data = await response.json()
            results = data.get("results") or []
            for song in results:
                lyrics = song.get("lyrics")
                if lyrics and lyrics.strip():
                    songs.append(
                        {
                            "id": song.get("id"),
                            "name": song.get("name") or "Unknown",
                            "lyrics": lyrics,
                        }
                    )
            if not results or not data.get("next"):
                break
            page += 1
        return songs

    async def _lyrics_index(self) -> list[dict]:
        """The cached lyric corpus, rebuilt lazily (once a day at most)."""
        if self._lyrics_ready and self._lyrics_cache:
            return self._lyrics_cache
        async with self._lyrics_index_lock:
            if self._lyrics_ready and self._lyrics_cache:
                return self._lyrics_cache
            corpus = self._read_lyrics_index_file()
            if corpus is None:
                corpus = await self._build_lyrics_index()
                self._write_lyrics_index_file(corpus)
            for song in corpus:
                song["_norm"] = self._normalise_lyrics(song.get("lyrics", ""))
            self._lyrics_cache = corpus
            self._lyrics_ready = True
            return corpus

    async def _warm_lyrics_index(self) -> None:
        try:
            await self._lyrics_index()
            logger.info("Lyrics index ready (%d songs)", len(self._lyrics_cache))
        except Exception:
            logger.warning("Could not pre-build the lyrics index", exc_info=True)

    @staticmethod
    def _score_lyrics(phrases: list[str], normalised: str) -> int:
        score = 0
        for phrase in phrases:
            if phrase and phrase in normalised:
                score += 1000 + min(normalised.count(phrase), 50)
        return score

    async def _search_lyrics(self, query: str, limit: int = 25) -> list[dict]:
        """Songs whose lyrics contain the query, best match first."""
        corpus = await self._lyrics_index()
        lines = [self._normalise_lyrics(line) for line in (query or "").splitlines()]
        lines = [line for line in lines if len(line) >= LYRICS_INDEX_MIN_PHRASE]
        whole = self._normalise_lyrics(query)
        if whole and whole not in lines:
            lines.append(whole)
        # Longest lines first: they carry the most signal and are far less
        # likely to appear in a hundred different songs.
        phrases = sorted(set(lines), key=len, reverse=True)[:5]
        if not phrases:
            return []
        scored = [
            (self._score_lyrics(phrases, song.get("_norm", "")), song)
            for song in corpus
        ]
        scored = [pair for pair in scored if pair[0] > 0]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [song for _, song in scored[:limit]]

    @commands.hybrid_command(
        name="lyricfind",
        aliases=["findlyrics", "lyricsearch", "whatsong"],
        description="Find a Juice WRLD song from a line of its lyrics",
    )
    @commands.guild_only()
    @unified_cooldown(15)
    async def lyricfind(self, ctx: commands.Context, *, query: str = None):
        """Reverse-search Juice WRLD's catalogue by a line of lyrics."""
        if not query or not query.strip():
            await Embeds.error(
                ctx,
                "**Missing argument:** lyrics\n"
                "Give me a line or two of the song's lyrics.",
                title="Error!",
                delete_after=5,
                reply=True,
            )
            return
        try:
            async with ctx.typing():
                matches = await self._search_lyrics(query)
        except Exception as error:
            logger.exception("lyricfind search failed")
            await Embeds.send_error_embed(
                ctx.channel,
                ctx.author,
                f"The lyric search crashed: `{type(error).__name__}: {error}`",
            )
            return

        if not matches:
            await Embeds.send_warning_embed(
                ctx.channel,
                ctx.author,
                "I couldn't find a song with those lyrics.",
            )
            return

        if len(matches) == 1:
            song = await self._fetch_song_by_id(matches[0]["id"])
            if song is None:
                await Embeds.send_error_embed(
                    ctx.channel,
                    ctx.author,
                    "I found a match but couldn't load the song details.",
                )
                return
            view = await self.create_song_view(
                song, guild_id=ctx.guild.id if ctx.guild else None, author_id=ctx.author.id
            )
            await ctx.reply(view=view)
            return

        results = matches[:25]

        class LyricsSongSelect(discord.ui.Select):
            def __init__(self, options, author, cog):
                self.author = author
                self.cog = cog
                super().__init__(
                    placeholder="Select a song...",
                    min_values=1,
                    max_values=1,
                    options=options,
                )

            async def callback(self, itn: discord.Interaction):
                song = await self.cog._fetch_song_by_id(int(self.values[0]))
                if song is None:
                    return await itn.response.send_message(
                        "I couldn't load that song.", ephemeral=True
                    )
                view = await self.cog.create_song_view(
                    song, guild_id=itn.guild_id, author_id=itn.user.id
                )
                if self.author != itn.user:
                    return await itn.response.send_message(view=view, ephemeral=True)
                await itn.response.edit_message(embed=None, view=view)

        options = [
            discord.SelectOption(
                label=(song.get("name") or "Unknown")[:100],
                value=str(song["id"]),
            )
            for song in results
        ]
        view = discord.ui.View(timeout=None)
        view.add_item(LyricsSongSelect(options, ctx.author, self))
        embed = discord.Embed(
            description=(
                f"{ctx.author.mention}: I found **{len(results)}** songs matching "
                "those lyrics. Pick one below."
            ),
            color=discord.Color.blurple(),
        )
        await ctx.reply(embed=embed, view=view)

    @tasks.loop(hours=1)
    async def cache_songs(self):
        songs = Cache.get_songs()
        if songs is not None:
            await self.store_latest_surfaces()
            await self.sync_blacktea()

    async def store_latest_surfaces(self, days: int = 30):
        cutoff = discord.utils.utcnow() - timedelta(days=days)

        hydrated = []

        songs = Cache.get_songs()
        for song in songs:
            dt = self.parse_dates(song.get('date_leaked', ''))
            if not dt:
                continue

            if dt.astimezone(timezone.utc) < cutoff:
                continue

            file_names = self.get_file_names(song['file_names'])
            downloads = await self.get_downloads(file_names, song['length'])
            _downloads = song['path']

            if _downloads and not downloads:
                downloads = [_downloads]

            song['date_leaked_dt'] = dt
            song['downloads'] = downloads

            hydrated.append(song)

        self.latest_surfaces = sorted(hydrated, key=lambda s: s['date_leaked_dt'], reverse=True)
    
    async def build_song_items(self, container: discord.ui.Container, songs: list[dict]):
        MAIN_URL = f'{JUICEWRLD_API}/juicewrld/files/download/?path='

        for ii, song in enumerate(songs):
            name = song.get('name', 'N/A')
            engineers = song.get('engineers', 'N/A')
            producers = song.get('producers', 'N/A')
            track_titles = [t for t in song.get('track_titles', []) if t != name]
            image_url = song.get('image_url')
            era_name = song.get('era', {}).get('name', 'N/A')
            album = self.ALBUMS.get(era_name)

            timestamp = f'<t:{int(song['date_leaked_dt'].timestamp())}:R>'

            section = discord.ui.Section(
                accessory=discord.ui.Thumbnail(media=JUICEWRLD_API + image_url)
            )
            section.add_item(discord.ui.TextDisplay(
                f'**{name}** - {timestamp}\n'
                f'-# Era: **{album['name'] if album else era_name}**\n'
                f'-# Alt Name(s): **{', '.join(track_titles) if track_titles else 'N/A'}**\n'
                f'-# Engineer(s): **{engineers}**\n'
                f'-# Producer(s): **{producers}**'
            ))

            container.add_item(section)

            downloads = song.get('downloads')

            rows = []
            
            if downloads:
                for i in range(0, len(downloads), 5):
                    row = discord.ui.ActionRow()
                    for path in downloads[i:i + 5]:
                        ext = path.rsplit('.', 1)[-1].upper()
                        label = f'OG {ext}' if 'Original Files' in path else ext
                        row.add_item(
                            discord.ui.Button(
                                label=label, 
                                url=MAIN_URL + quote(path)
                            )
                        )
                    rows.append(row)
                    
                for row in rows:
                    container.add_item(row)

            if ii < len(songs) - 1:
                container.add_item(discord.ui.Separator())

    @commands.command(name='syncsurfaces', aliases=['syncleaks'], description='Sync the Juice WRLD surfaces cache (owner only).', hidden=True)
    @commands.is_owner()
    async def sync_surfaces(self, ctx: commands.Context):
        if self.cache_songs.is_running():
            self.cache_songs.cancel()

        if ctx.message is not None:
            await ctx.message.add_reaction('🔄')
        status = await Cache.fetch_songs()
        if status != 200:
            logger.error(f"syncsurfaces cache fetch failed: HTTP {status}")
            return await Embeds.error(
                ctx,
                f"Surface cache sync failed (HTTP {status}). Please try again later.",
                title="⚠️ Sync Failed",
                delete_after=10,
                reply=True,
            )

        await self.store_latest_surfaces()
        if ctx.message is not None:
            await ctx.message.add_reaction('✅')
        else:
            await Embeds.success(ctx, 'Surfaces cache synced successfully.', reply=True)

    @commands.command(name='surfaces', aliases=['leaks'], description='Browse the latest Juice WRLD surfaces.')
    async def surfaces(self, ctx: commands.Context):
        try:

            songs = Cache.get_songs()
            if not songs:
                await ctx.reply('no songs? what did envy do')
                return
            
            if not self.latest_surfaces:
                await self.store_latest_surfaces()

                if not self.latest_surfaces:
                    await ctx.reply('no latest surfaces? what did envy do')
                    return

            view = LatestSurfacesView(self, self.latest_surfaces, ctx.author)
            await view.build_page()
            msg = await ctx.reply(view=view)
            view.message = msg
        except Exception as e:
            await ctx.send(e)
        
    @commands.hybrid_command(
        name="randomleak", aliases=["rleak"], description="Get a random Juice WRLD leak"
    )
    @unified_cooldown(15)
    async def randomleak(self, ctx: commands.Context):
        data = await self.fetch_random_playable_song()
        if data is None:
            return await Embeds.error(
                ctx,
                "Request failed. Please try again later.",
                delete_after=5,
            )

        song_data = data.get("song")

        layout_view = await self.create_song_view(
            song_data, True, guild_id=ctx.guild.id if ctx.guild else None,
            author_id=ctx.author.id,
        )

        message = await ctx.send(view=layout_view)

        try:
            await message.add_reaction("👍")
            await message.add_reaction("👎")
        except discord.Forbidden:
            # Missing Add Reactions permission; the view still works.
            pass


    def clear_user_cache(self, user_id: int, identifiers: list[str] = []):
        self.assert_download_cache()
        for file in os.listdir(DOWNLOAD_CACHE_FOLDER_NAME):
            is_valid = str(user_id) in file
            if is_valid:
                for identifier in identifiers:
                    if identifier.lower() not in file.lower():
                        is_valid = False
                        break

            if is_valid:
                file_path = os.path.join(DOWNLOAD_CACHE_FOLDER_NAME, file)
                try:
                    os.remove(file_path)
                except Exception as e:
                    logger.error(f"Error deleting file {file_path}: {e}")

    def remove_file(self, file_path: str) -> None:
        if not file_path:
            return
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception as e:
            logger.error(f"Error deleting file {file_path}: {e}")

    @staticmethod
    async def _safe_delete(message: discord.Message | None):
        if message is None:
            return
        try:
            await message.delete()
        except Exception:
            pass

    def get_most_acceptable_track_name(self, orig_name: str):
        name = orig_name.strip()
        for funcs in self.track_name_transformations.values():
            for func in funcs:
                name = func(name)
        return name

    @staticmethod
    def _is_unsurfaced(song: dict) -> bool:
        """`unsurfaced` songs have metadata only — no audio on the server.

        Trying to download or snippet them 404s, so callers must filter them
        out before letting the user pick one.
        """
        return (song.get("category") or "").lower() == "unsurfaced"

    def get_acceptable_track_names(self, orig_name: str):
        name = orig_name.lower().strip()

        required_transformations = []

        for check_func, transform_funcs in self.track_name_transformations.items():
            if check_func(name):
                for func in transform_funcs:
                    if len(required_transformations) >= MAX_NAME_TRANSFORMATIONS:
                        break
                    required_transformations.append(func)

        results = set()
        results.add(name)
        for r in range(1, len(required_transformations) + 1):
            for combo in product(required_transformations, repeat=r):
                temp = name
                for func in combo:
                    temp = func(temp)
                results.add(temp.strip())

        return sorted(results)

    def handle_channel_done_heardle(self, channel_id: int):
        """Release the per-channel Heardle lock once a round is finished."""
        self.heardle_answers.pop(channel_id, None)
        if channel_id in self.ongoing_heardle:
            self.ongoing_heardle.remove(channel_id)

    async def _collect_tournament_players(
        self, message: discord.Message
    ) -> list[discord.User]:
        """Return the users who reacted ✅ to a tournament join message."""
        try:
            fresh = await message.channel.fetch_message(message.id)
        except discord.DiscordException:
            fresh = message

        guild = message.guild
        players: dict[int, discord.User] = {}
        for reaction in fresh.reactions:
            if str(reaction.emoji) != "✅":
                continue
            try:
                async for user in reaction.users():
                    if user.bot:
                        continue
                    # Prefer the guild member (display name/colour), but never
                    # drop someone just because they are missing from the cache.
                    member = guild.get_member(user.id) if guild else None
                    players[user.id] = member or user
            except discord.DiscordException:
                logger.exception("Could not read tournament join reactions")
        return list(players.values())

    @staticmethod
    def _normalize_heardle_guess(text: str) -> str:
        """Fold a guess to lowercase words so casing/punctuation don't matter."""
        text = (text or "").lower()
        # Drop apostrophes entirely ("ain't" -> "aint") before turning other
        # punctuation into spaces, so both spellings compare equal.
        text = re.sub(r"['’]", "", text)
        text = re.sub(r"[^\w\s]", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def handle_user_done_snippet(self, user_id: int):
        self.snippet_debounce[user_id] = False

        self.clear_user_cache(user_id, ["_snippet"])

    async def make_snippet(
        self,
        image_path: str,
        download_url: str,
        file_name: str,
        duration: int = DEFAULT_SNIPPET_DURATION,
        start_point: int = None,
        path: str = None,
    ):
        """Create a short mp4 snippet from a static image and a downloaded audio file.

        Downloads the audio once, picks a random start point if none is provided,
        and does the heavy moviepy work in a thread so the bot stays responsive.
        """
        temp_file_path = None
        output_path = None

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(download_url, params={"path": path} if path else None) as download_response:
                    if download_response.status != 200:
                        return False, download_response.status

                    self.assert_download_cache()
                    song_bytes = await download_response.read()
                    temp_file_path = DOWNLOAD_CACHE_FOLDER_NAME + f"/{file_name}.mp3"
                    with open(temp_file_path, "wb") as f:
                        f.write(song_bytes)

            def _build():
                orig_clip = AudioFileClip(temp_file_path)
                actual_start = start_point
                if actual_start is None:
                    max_start = max(0, int(orig_clip.duration) - duration * 2)
                    actual_start = random.randint(0, max_start)

                sub_clip = orig_clip.subclipped(actual_start, actual_start + duration)
                out = f"{DOWNLOAD_CACHE_FOLDER_NAME}/{file_name}.mp4"
                final_clip = ImageClip(image_path).with_audio(sub_clip)
                final_clip.duration = duration
                final_clip.fps = 1
                final_clip.write_videofile(
                    out,
                    codec="libx264",
                    audio_codec="aac",
                    logger=None,
                    threads=2,
                    preset="ultrafast",
                    ffmpeg_params=["-tune", "stillimage", "-pix_fmt", "yuv420p"],
                )
                final_clip.close()
                sub_clip.close()
                orig_clip.close()
                return out

            output_path = await asyncio.to_thread(_build)

        finally:
            if temp_file_path:
                self.remove_file(temp_file_path)
            if image_path and image_path.startswith(DOWNLOAD_CACHE_FOLDER_NAME):
                self.remove_file(image_path)

        return True, output_path

    @commands.command(name="heardlestats", aliases=["hstats"], description="View Heardle statistics for a user.")
    async def heardlestats(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        member = member or ctx.author

        stats = await self.bot.database.get_heardle_stats(member.id)

        wins = stats.wins if stats else 0
        losses = stats.losses if stats else 0
        streak = stats.streak if stats else 0
        embed = discord.Embed(
            description=f"Heardle Stats for {member.display_name}", color=member.color
        )
        embed.add_field(name="Wins", value=wins, inline=True)
        embed.add_field(name="Losses", value=losses, inline=True)
        embed.add_field(
            name="Winstreak",
            value=streak if streak < 5 else f"**{streak}** 🔥",
            inline=True,
        )
        wl = round(wins / (wins + losses) * 100, 2) if (wins + losses) > 0 else 0.0
        embed.add_field(
            name="Win Rate", value=f"{wl} %" if stats else "N/A", inline=True
        )
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)

        await ctx.reply(embed=embed)

    async def sync_blacktea(self):
        self.valid_names = set()
        self.producer_counts = {}

        self._blacktea_name_to_songs = {}
        self._blacktea_name_to_producers = {}
        self._blacktea_name_to_category_era = {}
        self._blacktea_name_to_year = {}
        self._blacktea_name_to_max_price = {}

        songs = Cache.get_songs()
        for song in songs:
            producers = song.get("producers", "N/A")
            producers = [p.strip() for p in re.split(r"&|,| and ", producers) if p.strip()]

            era = song.get("era", {})
            era_name = era.get("name", "N/A")
            #if era_name == "POST": # ignore posthumous releases
            #    continue
            #if len(producers) > 5: # ignore groups with more than 5 producers
            #    continue

            for producer in producers:
                self.producer_counts[producer] = self.producer_counts.get(producer, 0) + 1

            category = song.get("category", "")
            date_leaked = song.get("date_leaked", "")
            end_line_index = date_leaked.rfind("\n")
            real_date_leaked = date_leaked[end_line_index:date_leaked.find(".", end_line_index)].strip().replace(",", "").split()
            year = real_date_leaked[2].strip() if real_date_leaked and len(real_date_leaked) >= 3 else ""

            groupbuy_info = song.get("groupbuy_info", {})
            price = groupbuy_info.get("price", "")
            numerical_price = "".join(filter(str.isdigit, price))
            min_price = int(numerical_price) if numerical_price else None

            track_titles = song.get("track_titles", [])
            seen_names_for_song = set()
            for title in track_titles:
                for acceptable_name in self.get_acceptable_track_names(title):
                    if acceptable_name in seen_names_for_song:
                        continue
                    seen_names_for_song.add(acceptable_name)

                    self.valid_names.add(acceptable_name)

                    self._blacktea_name_to_songs.setdefault(acceptable_name, []).append(song)

                    name_producers = self._blacktea_name_to_producers.setdefault(acceptable_name, set())
                    name_producers.update(producers)

                    if category and era_name:
                        self._blacktea_name_to_category_era.setdefault(acceptable_name, set()).add(f"{category}{era_name}")

                    if year:
                        self._blacktea_name_to_year.setdefault(acceptable_name, set()).add(year.lower())

                    if min_price is not None:
                        current_max = self._blacktea_name_to_max_price.get(acceptable_name)
                        if current_max is None or min_price > current_max:
                            self._blacktea_name_to_max_price[acceptable_name] = min_price

        self.is_blacktea_synced = True

    def get_random_song_for_blacktea(self):
        songs = Cache.get_songs()
        if not songs:
            return None
        return random.choice(songs)

    def find_songs_by_name(self, name):
        return self._blacktea_name_to_songs.get(name, [])

    def blacktea_check_producer(self, song_name, producer):
        return producer in self._blacktea_name_to_producers.get(song_name, set())

    def blacktea_check_category(self, song_name, category_era):
        return category_era in self._blacktea_name_to_category_era.get(song_name, set())

    def blacktea_check_leaked(self, song_name, leaked_date):
        return leaked_date.lower() in self._blacktea_name_to_year.get(song_name, set())

    def blacktea_check_groupbuy_price(self, song_name, leaked_date):
        max_price = self._blacktea_name_to_max_price.get(song_name)
        return max_price is not None and max_price >= int(leaked_date)

    def _get_3l_for_song(self, song):
        """Return a valid random 3-letter slice for a song, or None if impossible."""
        main_name = self.get_most_acceptable_track_name(song.get("name", ""))
        names = self.get_acceptable_track_names(song.get("name", ""))

        for name in (main_name, *names):
            valid_slices = [
                name[i:i+3]
                for i in range(len(name) - 2)
                if all(c.isalpha() for c in name[i:i+3])
            ]
            if valid_slices:
                return random.choice(valid_slices)
        return None

    def _build_default_category(self, song):
        """Build a default 3-letter category, picking a new song if needed."""
        for _ in range(8):
            random_3l = self._get_3l_for_song(song)
            if random_3l:
                break
            song = self.get_random_song_for_blacktea()
        else:
            random_3l = "xxx"
        return {
            "description": f"Name a **Juice WRLD** song that contains **{random_3l.lower()}**",
            "check_func": lambda song_name: random_3l.lower() in song_name.lower()
        }

    def get_random_blacktea_category_data(self, song):
        for attempt in range(8):
            random_index = random.randint(0, 4)

            if random_index == 0:
                producers_raw = song.get("producers", "N/A")
                producers = [p.strip() for p in re.split(r"&|,| and ", producers_raw) if p.strip()]
                producer = random.choice(producers) if producers else None
                if producer and self.producer_counts.get(producer, 0) >= 6:
                    return {
                        "description": f"Name a **Juice WRLD** song produced by **{producer}**",
                        "check_func": lambda song_name, p=producer: self.blacktea_check_producer(song_name, p)
                    }

            elif random_index == 1:
                category = song.get("category", "")
                era_name = song.get("era", {}).get("name", "")
                if category and era_name and category != "recording_session" and era_name not in ("GB&GR (AE)", "GB&GR (5YAE)", "MAINSTREAM"):
                    era_full = self.ALBUMS.get(era_name, {}).get("name", era_name)
                    return {
                        "description": f"Name a **Juice WRLD** song that is **{category}** and made during **{era_full.upper()}**",
                        "check_func": lambda song_name, ce=f"{category}{era_name}": self.blacktea_check_category(song_name, ce)
                    }

            elif random_index == 2:
                date_leaked = song.get("date_leaked", "")
                end_line_index = date_leaked.rfind("\n")
                real_date_leaked = date_leaked[end_line_index:date_leaked.find(".", end_line_index)].strip().replace(",", "").split()
                if real_date_leaked and len(real_date_leaked) >= 3:
                    year = real_date_leaked[2].strip()
                    if year:
                        return {
                            "description": f"Name a **Juice WRLD** song that leaked in **{year}**",
                            "check_func": lambda song_name, y=year.lower(): self.blacktea_check_leaked(song_name, y)
                        }

            elif random_index == 3:
                groupbuy_info = song.get("groupbuy_info", {})
                price = groupbuy_info.get("price", "")
                numerical_price = "".join(filter(str.isdigit, price))
                if numerical_price:
                    return {
                        "description": f"Name a **Juice WRLD** song that was groupbuyed for **{price}** or higher",
                        "check_func": lambda song_name, np=numerical_price: self.blacktea_check_groupbuy_price(song_name, np)
                    }

            # Default path: try a fresh song for the next attempt.
            song = self.get_random_song_for_blacktea()

        return self._build_default_category(song)

    @commands.command(name="syncblacktea", aliases=["sbt"], description="Sync the Blacktea valid names cache (owner only).", hidden=True)
    @commands.is_owner()
    async def syncblacktea(self, ctx: commands.Context):
        if ctx.message is not None:
            await ctx.message.add_reaction('🔄')

        songs = Cache.get_songs()
        old_names_length = len(self.valid_names)
        old_prods_length = len(self.producer_counts)
        await self.sync_blacktea()
        await Embeds.send_info_embed(ctx, ctx.author, f"Synced valid track names. Total songs: **{len(songs)}**. Total valid names: **{old_names_length}** -> **{len(self.valid_names)}**")
        await Embeds.send_info_embed(ctx, ctx.author, f"Synced producer counts. Total producers: **{old_prods_length}** -> **{len(self.producer_counts)}**")

        if ctx.message is not None:
            await ctx.message.add_reaction('✅')
        else:
            await Embeds.success(ctx, 'Blacktea cache synced successfully.', reply=True)

    @commands.hybrid_command(name="blacktea", description="Play blacktea with Juice WRLD songs.")
    @unified_cooldown(60)
    async def blacktea(self, ctx: commands.Context):
        if ctx.author.id in self.ongoing_blacktea:
            await Embeds.send_error_embed(ctx, ctx.author, "You already have an ongoing game of Blacktea!")
            return

        if not self.is_blacktea_synced:
            await self.sync_blacktea()

        players = []
        player_ids = set()
        created_messages = []
        self.ongoing_blacktea.append(ctx.author.id)

        blacktea_embed = discord.Embed(
            description=":alarm_clock: Waiting for **players**, react with ✅ to join. The game will begin in **30** seconds (or as soon as 2 players join).\n\n`GOAL:` You have **10** seconds to say a **Juice WRLD** song that fits the **given category**. Failure to do so within the **15** seconds will lose a life. Each player has **2** lives to begin with.\n\n`NOTES:` A song can only be used **once** through the course of the game.",
            color=discord.Color.green(),
        )
        blacktea_embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        join_message = await ctx.send(embed=blacktea_embed)
        await join_message.add_reaction("✅")
        created_messages.append(join_message)

        def reaction_check(reaction, user):
            return (
                reaction.message.id == join_message.id
                and str(reaction.emoji) == "✅"
                and not user.bot
            )

        start_time = asyncio.get_event_loop().time()
        while True:
            remaining = 30.0 - (asyncio.get_event_loop().time() - start_time)
            if remaining <= 0:
                break
            try:
                reaction, user = await self.bot.wait_for(
                    "reaction_add", check=reaction_check, timeout=remaining
                )
            except asyncio.TimeoutError:
                break

            if user.id in player_ids:
                continue

            member = ctx.guild.get_member(user.id)
            player_ids.add(user.id)
            players.append({
                "id": user.id,
                "display_name": member.display_name if member else user.display_name,
                "avatar_url": member.display_avatar.url if member else user.display_avatar.url,
                "color": member.color if member else discord.Color.default(),
                "mention": member.mention if member else user.mention,
                "lives": 2,
            })

        if len(players) <= 1:
            self.ongoing_blacktea.remove(ctx.author.id)
            await Embeds.send_warning_embed(ctx, ctx.author, "Not enough players joined the game. At least 2 players are required.")
            return

        await self.bot.database.set_cooldown(
            ctx.author.id, ctx.command.qualified_name, 30
        )

        # Pre-shuffle a pool of candidate songs so each round can pop a fresh song
        # without repeated random sampling or recursion.
        all_songs = Cache.get_songs() or []
        candidate_songs = [s for s in all_songs if s.get("name")]
        random.shuffle(candidate_songs)
        used_names = set()

        alive_players = [p for p in players if p["lives"] > 0]
        while len(alive_players) > 1:
            for player in alive_players:
                song = None
                while candidate_songs:
                    candidate = candidate_songs.pop()
                    main_name = candidate.get("name", "").lower()
                    if main_name and main_name not in used_names:
                        used_names.add(main_name)
                        song = candidate
                        break

                if song is None:
                    await Embeds.send_error_embed(ctx, ctx.author, "Ran out of unique songs for the game. Ending early.")
                    self.ongoing_blacktea.remove(ctx.author.id)
                    alive_players = []
                    break

                category_data = self.get_random_blacktea_category_data(song)
                description = category_data.get("description", "No description available")
                check_func = category_data["check_func"]

                embed = discord.Embed(
                    description=description,
                    color=player.get("color", discord.Color.default()).value if player.get("color") else discord.Color.default().value,
                )
                embed.set_author(name=player.get("display_name", "Unknown Player"), icon_url=player.get("avatar_url", ""))
                round_message = await ctx.send(player["mention"], embed=embed)
                created_messages.append(round_message)

                def check(m):
                    if m.author.id != player["id"] or m.channel != ctx.channel:
                        return False
                    content = m.content.lower().strip()
                    return content in self.valid_names and check_func(content)

                try:
                    guess = await self.bot.wait_for("message", check=check, timeout=15)
                    await guess.add_reaction("✅")
                except asyncio.TimeoutError:
                    player["lives"] -= 1
                    loss_message = await Embeds.error(
                        ctx,
                        f"💥 {player['mention']} you now have **{player['lives']}** lives. One correct answer was **{song.get('name', 'N/A')}**",
                        delete_after=None,
                    )
                    created_messages.append(loss_message)
                    alive_players = [p for p in players if p["lives"] > 0]
                    if len(alive_players) <= 1:
                        break

        if len(alive_players) == 1:
            winner = alive_players[0]
            await Embeds.custom(
                ctx,
                f"🏆 {winner['mention']} is the winner of this game of Blacktea with **{winner['lives']}** lives remaining!",
                color=discord.Color.gold(),
                delete_after=15,
            )

        for msg in created_messages:
            try:
                await msg.delete()
            except Exception:
                pass
        self.ongoing_blacktea.remove(ctx.author.id)

    @commands.hybrid_command(name="heardle", description="Play a game of Heardle. Juice WRLD songs only.")
    @unified_cooldown(60)
    async def heardle(self, ctx: commands.Context):
        if ctx.channel.id in self.ongoing_heardle:
            await Embeds.send_error_embed(
                ctx.channel,
                ctx.author,
                "A Heardle round is already running in this channel!",
            )
            return

        async def handle_request_failed(ctx, code=None):
            embed = discord.Embed(
                description="Request failed. Please try again later.",
                color=discord.Color.red(),
            )
            if code:
                embed.add_field(
                    name="Status", value=f"HTTP {code}", inline=False
                )
            await ctx.reply(embed=embed, delete_after=5)

        data = await self.fetch_random_playable_song()
        if data is None:
            await handle_request_failed(ctx)
            return

        song_data = data.get("song", None)
        if not song_data:
            await handle_request_failed(ctx)
            return

        self.ongoing_heardle.append(ctx.channel.id)
        track_titles = song_data.get("track_titles", []) or []
        path = data.get("path", "")
        raw_track_title = song_data.get("name", "Unknown Title")
        best_track_title = self.get_most_acceptable_track_name(raw_track_title)

        # Pre-compute all acceptable answers so the game loop is O(1) lookup.
        acceptable_answers = set(self.get_acceptable_track_names(raw_track_title))
        for title in track_titles:
            acceptable_answers.update(self.get_acceptable_track_names(title))
        self.heardle_answers[ctx.channel.id] = best_track_title

        image_file_name = f"{DOWNLOAD_CACHE_FOLDER_NAME}/{ctx.author.id}_temp_image_heardle.png"
        message = None
        update_task = None
        try:
            async with ctx.typing():
                cover_slug = best_track_title.lower().replace(" ", "")
                await self._fetch_best_cover(
                    cover_url=f"{JUICEWRLD_API}/juicewrld/cover/{cover_slug}.png",
                    album_art_url=f"{JUICEWRLD_API}/juicewrld/files/cover-art/",
                    album_art_params={"path": path},
                    avatar_url=str(ctx.author.display_avatar.url),
                    target_path=image_file_name,
                )

                download_url = f"{JUICEWRLD_API}/juicewrld/files/download-compressed/"
                result, payload = await self.make_snippet(
                    image_file_name,
                    download_url,
                    f"{ctx.author.id}_heardle",
                    HEARDLE_CLIP_DURATION,
                    path=path,
                )
                if result == False:
                    await handle_request_failed(ctx, payload)
                    return

                message = await ctx.send(
                    content=(
                        "🎧 **Heardle** — name this Juice WRLD song!\n"
                        "Anyone in this channel can answer — the first correct "
                        f"guess wins {format_cash(HEARDLE_PRIZE)}."
                    ),
                    file=discord.File(payload),
                )
                self.remove_file(payload)

            has_guessed = False
            attempts = 0
            winner = None
            last_hint = None
            start_time = asyncio.get_event_loop().time()

            async def update_timer_message(
                message: discord.Message, full_name, start_time
            ):
                nonlocal last_hint
                try:
                    while True:
                        elapsed = asyncio.get_event_loop().time() - start_time
                        hint_chars = int(elapsed // 3)
                        if hint_chars > 3:
                            return

                        hint_chars = min(hint_chars, len(full_name))
                        hint = ""
                        for i, ch in enumerate(full_name):
                            if i < hint_chars:
                                hint += ch
                            elif ch == " ":
                                hint += " "
                            else:
                                hint += "?"
                        if hint != last_hint:
                            last_hint = hint
                            await message.edit(
                                embed=discord.Embed(
                                    title="Heardle",
                                    description=f"Hint ({hint_chars}/3): {hint}",
                                    color=discord.Color.blurple(),
                                )
                            )
                        await asyncio.sleep(1)
                except asyncio.CancelledError:
                    return

            update_task = asyncio.create_task(
                update_timer_message(message, best_track_title, start_time)
            )

            def check_guess(m):
                return m.channel == ctx.channel and not m.author.bot

            while not has_guessed:
                elapsed = asyncio.get_event_loop().time() - start_time
                remaining_time = HEARDLE_GAME_DURATION - elapsed
                if remaining_time <= 0:
                    await Embeds.send_warning_embed(
                        ctx.channel,
                        ctx.author,
                        f"Time's up! Nobody guessed the song ({best_track_title} [{raw_track_title}]).",
                    )
                    await self.bot.database.add_heardle_loss(ctx.author.id)
                    return

                try:
                    guess_msg = await self.bot.wait_for(
                        "message", check=check_guess, timeout=remaining_time
                    )
                except TimeoutError:
                    await Embeds.send_warning_embed(
                        ctx.channel,
                        ctx.author,
                        f"Time's up! Nobody guessed the song ({best_track_title} [{raw_track_title}]).",
                    )
                    await self.bot.database.add_heardle_loss(ctx.author.id)
                    return

                guess = guess_msg.content.strip().lower()
                if guess == "exit" and guess_msg.author.id == ctx.author.id:
                    await guess_msg.add_reaction("👋")
                    await self.bot.database.add_heardle_loss(ctx.author.id)
                    return
                elif guess in acceptable_answers:
                    has_guessed = True
                    winner = guess_msg.author
                else:
                    attempts += 1

            elapsed = asyncio.get_event_loop().time() - start_time
            await Embeds.send_success_embed(
                ctx.channel,
                winner,
                f"You guessed the song correctly: **{best_track_title}** "
                f"in {round(elapsed)} seconds ({attempts + 1} attempts)! "
                f"Prize: {format_cash(HEARDLE_PRIZE)}.",
            )
            await self.bot.database.add_heardle_win(winner.id)
            try:
                await self.bot.economy.add(
                    winner.id, HEARDLE_PRIZE, description="Heardle win"
                )
            except Exception:
                logger.exception("Could not pay the Heardle prize to %s", winner.id)
        finally:
            if update_task is not None:
                update_task.cancel()
                try:
                    await update_task
                except asyncio.CancelledError:
                    pass
            if image_file_name.startswith(DOWNLOAD_CACHE_FOLDER_NAME):
                self.remove_file(image_file_name)
            await self._safe_delete(message)
            self.clear_user_cache(ctx.author.id, ["_heardle"])
            self.handle_channel_done_heardle(ctx.channel.id)

    @commands.group(
        name="heardletournament",
        aliases=["heardletourney", "htournament"],
        invoke_without_command=True,
        description="Start a multi-round Heardle tournament.",
    )
    async def heardletournament(self, ctx: commands.Context):
        await Embeds.error(
            ctx,
            f"Usage: `!heardletournament rounds <1-{HEARDLE_TOURNAMENT_MAX_ROUNDS}>`",
            reply=True,
            delete_after=10,
        )

    @heardletournament.command(
        name="rounds",
        description="Start a Heardle tournament with the given number of rounds.",
    )
    @unified_cooldown(60)
    async def heardletournament_rounds(self, ctx: commands.Context, rounds: int):
        if not 1 <= rounds <= HEARDLE_TOURNAMENT_MAX_ROUNDS:
            await Embeds.send_error_embed(
                ctx,
                ctx.author,
                f"Rounds must be between 1 and {HEARDLE_TOURNAMENT_MAX_ROUNDS}.",
            )
            return

        if ctx.channel.id in self.ongoing_heardle:
            await Embeds.send_error_embed(
                ctx,
                ctx.author,
                "A Heardle game or tournament is already running in this channel!",
            )
            return

        self.ongoing_heardle.append(ctx.channel.id)
        join_message = None
        round_message = None
        try:
            join_embed = discord.Embed(
                title="🎧 Heardle Tournament",
                description=(
                    "React with ✅ to join! The host is automatically in.\n\n"
                    f"The tournament starts in **{HEARDLE_TOURNAMENT_JOIN_DURATION} "
                    f"seconds** and runs for **{rounds}** "
                    f"round{'s' if rounds != 1 else ''}.\n"
                    "Each round plays a short Juice WRLD clip — everyone who "
                    "names the song before the timer runs out scores a point. "
                    "Most points at the end wins."
                ),
                color=discord.Color.blurple(),
            )
            join_embed.set_footer(text=f"Hosted by {ctx.author.display_name}")
            join_message = await ctx.send(embed=join_embed)
            await join_message.add_reaction("✅")
            await asyncio.sleep(HEARDLE_TOURNAMENT_JOIN_DURATION)

            players = await self._collect_tournament_players(join_message)
            players_by_id = {member.id: member for member in players}
            players_by_id[ctx.author.id] = ctx.author
            players = list(players_by_id.values())
            if len(players) < HEARDLE_TOURNAMENT_MIN_PLAYERS:
                await Embeds.send_warning_embed(
                    ctx.channel,
                    ctx.author,
                    f"Not enough players joined — at least "
                    f"{HEARDLE_TOURNAMENT_MIN_PLAYERS} are required.",
                )
                return

            players.sort(key=lambda member: member.display_name.lower())
            scores = {member.id: 0 for member in players}

            await ctx.send(
                f"🎧 **Heardle Tournament** — **{len(players)}** players, "
                f"**{rounds}** round{'s' if rounds != 1 else ''}. "
                "Everyone who gets a round right scores a point!"
            )

            used_songs = set()

            for round_number in range(1, rounds + 1):
                data = None
                for _ in range(3):
                    candidate = await self.fetch_random_playable_song()
                    if not candidate or not candidate.get("song"):
                        continue
                    song = candidate["song"]
                    key = (
                        song.get("id")
                        or song.get("name")
                        or candidate.get("path")
                    )
                    if key in used_songs:
                        continue
                    used_songs.add(key)
                    data = candidate
                    break

                if data is None:
                    await Embeds.warning(
                        ctx,
                        f"⚠️ Could not load a song for round {round_number} — skipping.",
                    )
                    continue

                song_data = data["song"]
                path = data.get("path", "")
                raw_track_title = song_data.get("name", "Unknown Title")
                best_track_title = self.get_most_acceptable_track_name(
                    raw_track_title
                )

                acceptable_answers = {
                    self._normalize_heardle_guess(name)
                    for name in self.get_acceptable_track_names(raw_track_title)
                }
                for title in song_data.get("track_titles") or []:
                    acceptable_answers.update(
                        self._normalize_heardle_guess(name)
                        for name in self.get_acceptable_track_names(title)
                    )
                acceptable_answers.discard("")

                image_file_name = (
                    f"{DOWNLOAD_CACHE_FOLDER_NAME}/"
                    f"{ctx.author.id}_temp_image_tournament_{round_number}.png"
                )
                clip_name = f"{ctx.author.id}_tournament_{round_number}"
                update_task = None
                payload = None

                try:
                    async with ctx.typing():
                        cover_slug = best_track_title.lower().replace(" ", "")
                        await self._fetch_best_cover(
                            cover_url=f"{JUICEWRLD_API}/juicewrld/cover/{cover_slug}.png",
                            album_art_url=f"{JUICEWRLD_API}/juicewrld/files/cover-art/",
                            album_art_params={"path": path},
                            avatar_url=self.default_avatar_url,
                            target_path=image_file_name,
                        )
                        success, payload = await self.make_snippet(
                            image_file_name,
                            f"{JUICEWRLD_API}/juicewrld/files/download-compressed/",
                            clip_name,
                            HEARDLE_CLIP_DURATION,
                            path=path,
                        )

                    if not success:
                        await Embeds.warning(
                            ctx,
                            f"⚠️ Could not load audio for round {round_number} — skipping.",
                        )
                        continue

                    round_message = await ctx.send(
                        embed=discord.Embed(
                            title=(
                                f"🎧 Heardle Tournament — Round "
                                f"{round_number}/{rounds}"
                            ),
                            description=(
                                "Name this Juice WRLD song! Everyone who gets it "
                                "right before the timer ends scores a point.\n\n"
                                "*Hint appears after a few seconds.*"
                            ),
                            color=discord.Color.blurple(),
                        ),
                        file=discord.File(payload),
                    )
                    self.remove_file(payload)
                    payload = None

                    start_time = asyncio.get_event_loop().time()
                    last_hint = None

                    async def update_timer_message(
                        message: discord.Message, full_name: str, start_time: float
                    ):
                        nonlocal last_hint
                        try:
                            while True:
                                elapsed = (
                                    asyncio.get_event_loop().time() - start_time
                                )
                                hint_chars = int(elapsed // 3)
                                if hint_chars > 3:
                                    return

                                hint_chars = min(hint_chars, len(full_name))
                                hint = "".join(
                                    ch if i < hint_chars or ch == " " else "?"
                                    for i, ch in enumerate(full_name)
                                )
                                if hint != last_hint:
                                    last_hint = hint
                                    await message.edit(
                                        embed=discord.Embed(
                                            title=(
                                                f"🎧 Heardle Tournament — Round "
                                                f"{round_number}/{rounds}"
                                            ),
                                            description=(
                                                f"Hint ({hint_chars}/3): `{hint}`\n\n"
                                                "Everyone who gets it right scores a point!"
                                            ),
                                            color=discord.Color.blurple(),
                                        )
                                    )
                                await asyncio.sleep(1)
                        except asyncio.CancelledError:
                            return
                        except discord.DiscordException:
                            return

                    update_task = asyncio.create_task(
                        update_timer_message(
                            round_message, best_track_title, start_time
                        )
                    )

                    round_scorers: list[discord.User] = []
                    round_scorer_ids: set[int] = set()

                    def check_guess(m: discord.Message) -> bool:
                        return (
                            m.channel == ctx.channel
                            and not m.author.bot
                            and m.author.id in scores
                            and m.author.id not in round_scorer_ids
                        )

                    while len(round_scorer_ids) < len(scores):
                        remaining_time = HEARDLE_GAME_DURATION - (
                            asyncio.get_event_loop().time() - start_time
                        )
                        if remaining_time <= 0:
                            break
                        try:
                            guess_message = await self.bot.wait_for(
                                "message",
                                check=check_guess,
                                timeout=remaining_time,
                            )
                        except (asyncio.TimeoutError, TimeoutError):
                            break

                        normalized_guess = self._normalize_heardle_guess(
                            guess_message.content
                        )
                        if normalized_guess not in acceptable_answers:
                            continue

                        round_scorer_ids.add(guess_message.author.id)
                        round_scorers.append(guess_message.author)
                        scores[guess_message.author.id] += 1
                        try:
                            await guess_message.add_reaction("✅")
                        except discord.DiscordException:
                            pass

                    if round_scorers:
                        await Embeds.success(
                            ctx,
                            f"Round {round_number} — the song was "
                            f"**{best_track_title}** [{raw_track_title}].\n"
                            f"Point{'s' if len(round_scorers) != 1 else ''} to: "
                            + ", ".join(
                                f"{member.mention} (**{scores[member.id]}**)"
                                for member in round_scorers
                            ),
                        )
                        for member in round_scorers:
                            try:
                                await self.bot.database.add_heardle_win(
                                    member.id
                                )
                            except Exception:
                                logger.exception(
                                    "Could not record Heardle tournament win "
                                    "for %s",
                                    member.id,
                                )
                    else:
                        await Embeds.send_warning_embed(
                            ctx.channel,
                            ctx.author,
                            f"Round {round_number} over! Nobody guessed "
                            f"**{best_track_title}** [{raw_track_title}].",
                        )
                finally:
                    if update_task is not None:
                        update_task.cancel()
                        try:
                            await update_task
                        except asyncio.CancelledError:
                            pass
                    if payload:
                        self.remove_file(payload)
                    self.remove_file(image_file_name)
                    await self._safe_delete(round_message)
                    round_message = None

            ranked = sorted(
                players,
                key=lambda member: (
                    -scores[member.id],
                    member.display_name.lower(),
                ),
            )
            lines = []
            previous_score = None
            rank = 0
            medals = {1: "🥇", 2: "🥈", 3: "🥉"}
            for index, member in enumerate(ranked, start=1):
                score = scores[member.id]
                if score != previous_score:
                    rank = index
                    previous_score = score
                marker = medals.get(rank, f"**{rank}.**")
                lines.append(
                    f"{marker} {member.mention} — **{score}** "
                    f"point{'s' if score != 1 else ''}"
                )

            leaderboard = discord.Embed(
                title="🏆 Heardle Tournament — Final Leaderboard",
                description="\n".join(lines),
                color=discord.Color.gold(),
            )
            top_score = scores[ranked[0].id]
            top_players = [
                member for member in ranked if scores[member.id] == top_score
            ]
            if top_score == 0:
                leaderboard.description += (
                    "\n\n*Nobody scored — better luck next time!*"
                )
            elif len(top_players) == 1:
                leaderboard.description = (
                    f"👑 {top_players[0].mention} wins!\n\n"
                    + leaderboard.description
                )
            else:
                leaderboard.description = (
                    "👑 Tie between "
                    + ", ".join(member.mention for member in top_players)
                    + "!\n\n"
                    + leaderboard.description
                )
            leaderboard.set_footer(
                text=(
                    f"{rounds} round{'s' if rounds != 1 else ''} • "
                    f"Hosted by {ctx.author.display_name}"
                )
            )
            await ctx.send(embed=leaderboard)
        finally:
            await self._safe_delete(join_message)
            await self._safe_delete(round_message)
            self.clear_user_cache(ctx.author.id, ["_tournament"])
            self.handle_channel_done_heardle(ctx.channel.id)

    @commands.command(name="makesnippet", aliases=["makesnip"], description="Create a snippet from a Juice WRLD song.")
    async def makesnippet(self, ctx: commands.Context, *, query: Optional[str] = None):
        if not query:
            await Embeds.custom(
                ctx,
                "Please provide a song name to make a snippet from.",
                title="Error",
                color=0x36393E,
                reply=True,
            )
            return
        debounce = self.snippet_debounce.get(ctx.author.id, False)
        if debounce:
            await Embeds.send_warning_embed(
                ctx.channel,
                ctx.author,
                "Please wait a bit before making another snippet.",
            )
            return

        self.snippet_debounce[ctx.author.id] = True
        snippet_file_name = f"{ctx.author.id}_snippet"
        image_file_name = f"{DOWNLOAD_CACHE_FOLDER_NAME}/{ctx.author.id}_temp_image_snippet.png"

        async def handle_request_failed(ctx, code=None):
            embed = discord.Embed(
                description="Request failed. Please try again later.",
                color=discord.Color.red(),
            )
            if code:
                embed.add_field(
                    name="Status", value=f"HTTP {code}", inline=False
                )
            await ctx.reply(embed=embed, delete_after=5)

        try:
            async with ctx.typing():
                normalized_query = query.replace("’", "'")
                matches = await self._search_songs(
                    normalized_query, allow_unsurfaced=False
                )
                if matches is None:
                    await handle_request_failed(ctx, 500)
                    return

                safe_items: dict[str, dict] = {}
                for song in matches:
                    name = song.get("name", "Unknown Title")
                    best_name = self.get_most_acceptable_track_name(name)
                    if best_name in safe_items:
                        continue
                    safe_items[best_name] = song

                count = len(safe_items)
                safe_items_list = list(safe_items.values())

                song = None
                if count == 0:
                    await Embeds.send_error_embed(
                        ctx.channel,
                        ctx.author,
                        f"I couldn't find a song with the name: `{query}`",
                    )
                    return
                if count > 1:

                    class SongSelect(discord.ui.Select):
                        def __init__(self, view, author, songs):
                            options = []
                            self.real_view = view
                            self.author = author
                            self.chosen_song = None
                            self.avail_options_map = {}
                            for name, song in songs.items():
                                self.avail_options_map[name] = song
                                options.append(discord.SelectOption(label=name))
                            super().__init__(
                                placeholder="Select a song...",
                                min_values=1,
                                max_values=1,
                                options=options,
                            )
                            self.songs = songs

                        async def callback(self, interaction: discord.Interaction):
                            if interaction.user.id != self.author:
                                return await interaction.response.send_message(
                                    "You cannot use this select menu.", ephemeral=True
                                )

                            self.chosen_song = self.avail_options_map.get(
                                self.values[0], None
                            )
                            self.real_view.stop()

                    class SongView(discord.ui.View):
                        def __init__(self, author, songs):
                            super().__init__(timeout=30)
                            self.add_item(SongSelect(self, author, songs))

                    embed = discord.Embed(
                        description=f"{ctx.author.mention}: Multiple **songs** found with your **search**. Please select one from the dropdown below.",
                        color=None,
                    )
                    view = SongView(ctx.author.id, safe_items)
                    message = await ctx.reply(embed=embed, view=view)
                    await view.wait()
                    await self._safe_delete(message)

                    select = view.children[0]
                    if not select or not select.chosen_song:
                        return
                    song = select.chosen_song
                    await self._safe_delete(message)
                else:
                    song = safe_items_list[0]

                path = song.get("path", "")
                download_url = f"{JUICEWRLD_API}/juicewrld/files/download-compressed/"

                best_track_title = self.get_most_acceptable_track_name(
                    song.get("name", "Unknown Title").replace(".mp3", "")
                )

                cover_slug = best_track_title.lower().replace(" ", "")
                await self._fetch_best_cover(
                    cover_url=f"{JUICEWRLD_API}/juicewrld/cover/{cover_slug}.png",
                    album_art_url=f"{JUICEWRLD_API}/juicewrld/files/cover-art/",
                    album_art_params={"path": path},
                    avatar_url=str(ctx.author.display_avatar.url),
                    target_path=image_file_name,
                )

                result, payload = await self.make_snippet(
                    image_file_name,
                    download_url,
                    snippet_file_name,
                    DEFAULT_SNIPPET_DURATION,
                    path=path,
                )
                if result == False:
                    await handle_request_failed(ctx, payload)
                    return

                message = await ctx.channel.send(file=discord.File(payload))
                self.remove_file(payload)

            # Clean up the cached cover image if it was saved to disk.
            if image_file_name.startswith(DOWNLOAD_CACHE_FOLDER_NAME):
                self.remove_file(image_file_name)

            # Re-arm the snippet cooldown once the user has actually received the file.
            async def debounce_delay(author_id: int):
                await asyncio.sleep(15)
                self.handle_user_done_snippet(author_id)

            asyncio.create_task(debounce_delay(ctx.author.id))

        except Exception as e:
            logger.exception(f"makesnippet failed for {ctx.author.id}: {e}")
            await Embeds.send_error_embed(
                ctx.channel,
                ctx.author,
                "Something went wrong while making the snippet. Please try again.",
            )
        finally:
            self.handle_user_done_snippet(ctx.author.id)

    def find_pledges_channel(self, guild: discord.Guild):
        channels = []
        for channel in guild.text_channels:
            if "pledge" in channel.name.lower():
                channels.append(channel)
        return channels

    @commands.command(name="countpledge", aliases=["pledges", "pledged", "countpledges"], description="Count pledges in a pledge channel.", hidden=True)
    @commands.check_any(commands.has_permissions(administrator=True), commands.check(can_test))
    async def countpledge(self, ctx: commands.Context, after_message_id: int = 0):
        pledges_channels = self.find_pledges_channel(ctx.guild)
        count = len(pledges_channels)
        selected_channel = None
        if count == 0:
            await Embeds.send_warning_embed(
                ctx.channel,
                ctx.author,
                "No pledge channels found in this server.",
            )
            return
        elif count > 1:
            role_list = "\n".join(
                [
                    f"{index + 1}. {role.mention}"
                    for index, role in enumerate(pledges_channels)
                ]
            )
            embed = discord.Embed(
                description=f"Multiple pledge channels found':\n{role_list}\nPlease reply with the number of the role you want.",
                color=None,
            )
            msg = await ctx.send(embed=embed)

            def check(m):
                return (
                    m.author == ctx.author
                    and m.channel == ctx.channel
                    and m.content.isdigit()
                )

            try:
                response = await self.bot.wait_for("message", check=check, timeout=30.0)
                selected_index = int(response.content) - 1

                if selected_index < 0 or selected_index >= len(pledges_channels):
                    return None, "Invalid selection. Command cancelled."
                await msg.delete()
                selected_channel = pledges_channels[selected_index]
                await response.delete()
            except (ValueError, IndexError):
                return None, "Invalid selection. Command cancelled."
            except asyncio.TimeoutError:
                return None, "You took too long to respond. Command cancelled."
        else:
            selected_channel = pledges_channels[0]
        
        pledge_count = 0
        async with ctx.typing():
            async for message in selected_channel.history(after=discord.Object(id=after_message_id), limit=None):
                if message.author.bot:
                    continue
                
                numbers = re.findall(r'\d+', message.content)
                numbers = list(map(int, numbers))

                usable_number = None
                while usable_number is None and len(numbers) > 0:
                    candidate = numbers.pop(0)
                    if candidate > 0 and candidate < 10000:  
                        usable_number = candidate
                
                pledge_count += usable_number if usable_number is not None else 0
        
        await Embeds.send_success_embed(
            ctx.channel,
            ctx.author,
            f"Total pledges counted ({selected_channel.mention}): **${pledge_count:,}**",
            delete_after=None
        )
        
    async def get_genius_data(self, song_name: str):
        """Fetch song data from Genius API."""
        search_url = "https://api.genius.com/search"
        headers = {"Authorization": f"Bearer {GENIUS_API_TOKEN}"}
        params = {"q": song_name + " Juice WRLD"}

        async with self.session.get(search_url, headers=headers, params=params) as response:
            if response.status != 200:
                return None
            data = await response.json()
            hits = data.get("response", {}).get("hits", [])

            valid_hits = []
            for hit in hits:
                result = hit.get("result", {})
                if result.get("primary_artist", {}).get("is_verified", False) == True:
                    valid_hits.append(hit)

            if len(hits) == 0:
                return None
            api_path = hits[0].get("result", {}).get("api_path", "")
            if not api_path:
                return None
            song_url = f"https://api.genius.com{api_path}"
            async with self.session.get(song_url, headers=headers) as song_response:
                if song_response.status != 200:
                    return None
                song_response_json = await song_response.json()
                return song_response_json.get("response", {}).get("song", {})

        return None

    @commands.command(name="higherlower", description="Play a game of Higher or Lower with Juice WRLD song streams.")
    async def higherlower(self, ctx: commands.Context):
        if ctx.author.id in self.ongoing_higherlower:
            await Embeds.send_error_embed(ctx, ctx.author, "You already have an ongoing game of Higher or Lower!")
            return

        response1_data = await self.fetch_random_playable_song()
        if response1_data is None:
            await Embeds.send_error_embed(ctx, ctx.author, "Failed to fetch song data. Please try again later.")
            return

        song1_genius_data = await self.get_genius_data(response1_data.get("song", {}).get("name", ""))
        if not song1_genius_data:
            await Embeds.send_error_embed(ctx, ctx.author, "Failed to fetch song data. Please try again later.")
            return
        song1_id = song1_genius_data.get("id", None)
        if not song1_id:
            await Embeds.send_error_embed(ctx, ctx.author, "Failed to extract song ID. Please try again later.")
            return

        song2_id = song1_id
        retry = 0
        while song2_id == song1_id:
            response2_data = await self.fetch_random_playable_song()
            if response2_data is None:
                await Embeds.send_error_embed(ctx, ctx.author, "Failed to fetch second song data. Please try again later.")
                return

            song2_genius_data = await self.get_genius_data(response2_data.get("song", {}).get("name", ""))
            if not song2_genius_data:
                await Embeds.send_error_embed(ctx, ctx.author, "Failed to extract second song ID. Please try again later.")
                return
            song2_id = song2_genius_data.get("id", None)
            if not song2_id:
                await Embeds.send_error_embed(ctx, ctx.author, "Failed to fetch second song data. Please try again later.")
                return
            if not song2_id and retry > 5:
                await Embeds.send_error_embed(ctx, ctx.author, "Failed to fetch song data. Please try again later.")
                return
            retry += 1

        self.ongoing_higherlower.append(ctx.author.id)
            
        song1_title = self.get_most_acceptable_track_name(song1_genius_data.get("full_title", "Unknown Title"))
        song2_title = self.get_most_acceptable_track_name(song2_genius_data.get("full_title", "Unknown Title"))
        song1_pageviews = song1_genius_data.get("stats", {}).get("pageviews", 0)
        song2_pageviews = song2_genius_data.get("stats", {}).get("pageviews", 0)

        song1_title = song1_title[:song1_title.rfind("by")].strip() if "by" in song1_title else song1_title
        song2_title = song2_title[:song2_title.rfind("by")].strip() if "by" in song2_title else song2_title
        
        embed = discord.Embed(
            description=f"Do you think **{song1_title}** has *more* or *less* views than **{song2_title}**?",
            color=ctx.author.color
        )
        embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        message = await ctx.send(embed=embed)
        await message.add_reaction("⬆️")
        await message.add_reaction("⬇️")
        def check(reaction, user):
            return user == ctx.author and str(reaction.emoji) in ["⬆️", "⬇️"] and reaction.message.id == message.id
        try:    

            reaction, user = await self.bot.wait_for('reaction_add', check=check, timeout=30)
            if (reaction.emoji == "⬆️" and song1_pageviews > song2_pageviews) or (reaction.emoji == "⬇️" and song1_pageviews < song2_pageviews):
                await Embeds.send_success_embed(ctx, ctx.author, f"Correct! **{song1_title}** has {song1_pageviews:,} views while **{song2_title}** has {song2_pageviews:,} views.")
            else:
                await Embeds.send_error_embed(ctx, ctx.author, f"Wrong! **{song1_title}** has {song1_pageviews:,} views while **{song2_title}** has {song2_pageviews:,} views.")
        except asyncio.TimeoutError:
            await Embeds.send_warning_embed(ctx, ctx.author, "You took too long to react! Please try again.")
            self.ongoing_higherlower.remove(ctx.author.id)
            return

class CoverArtistView(discord.ui.LayoutView):
    PER_PAGE = 9
    ARTISTS_PER_PAGE = 22  
    ALL = "__all__"
    PREV_ARTISTS = "__artists_prev__"
    NEXT_ARTISTS = "__artists_next__"

    def __init__(self, cog: "Music", song_name: str, covers_by_artist: dict, author_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.song_name = song_name
        self.covers_by_artist = covers_by_artist
        self.artists = sorted(covers_by_artist, key=lambda a: (-len(covers_by_artist[a]), a.lower()))
        self.all_covers = [cover for artist in self.artists for cover in covers_by_artist[artist]]
        self.author_id = author_id
        self.selected = None  
        self.page = 0
        self.artist_page = 0
        self.expired = False
        self.message = None
        self._media_names = []  
        self._build()

    def _current_covers(self):
        if self.selected == self.ALL:
            return self.all_covers
        return self.covers_by_artist.get(self.selected, [])

    def _total_pages(self):
        return max((len(self._current_covers()) + self.PER_PAGE - 1) // self.PER_PAGE, 1)

    def _build(self):
        self.clear_items()

        container = discord.ui.Container(accent_color=0xffffff)
        container.add_item(discord.ui.TextDisplay(f"### 🎨 Covers for: {self.song_name}"))
        if self.selected is None:
            container.add_item(discord.ui.TextDisplay("Which artist's covers would you like to see?"))
        container.add_item(discord.ui.Separator())

        placeholder = "Select an artist..."
        options = [discord.SelectOption(
            label="All",
            value=self.ALL,
            description=f"{len(self.all_covers)} cover(s) from {len(self.artists)} artist(s)"[:100],
            default=(self.selected == self.ALL),
        )]

        paging = len(self.artists) > 24
        if paging:
            total_apages = (len(self.artists) + self.ARTISTS_PER_PAGE - 1) // self.ARTISTS_PER_PAGE
            self.artist_page = max(0, min(self.artist_page, total_apages - 1))
            placeholder = f"Select an artist... (page {self.artist_page + 1}/{total_apages})"
            start = self.artist_page * self.ARTISTS_PER_PAGE
            page_artists = self.artists[start:start + self.ARTISTS_PER_PAGE]
            if self.artist_page > 0:
                options.append(discord.SelectOption(
                    label="◀ More artists",
                    value=self.PREV_ARTISTS,
                    description=f"Page {self.artist_page + 1}/{total_apages}",
                ))
        else:
            page_artists = self.artists

        for artist in page_artists:
            options.append(discord.SelectOption(
                label=artist[:100],
                value=artist[:100],
                description=f"{len(self.covers_by_artist[artist])} cover(s)",
                default=(self.selected == artist),
            ))

        if paging and self.artist_page < total_apages - 1:
            options.append(discord.SelectOption(
                label="▶ More artists",
                value=self.NEXT_ARTISTS,
                description=f"Page {self.artist_page + 1}/{total_apages}",
            ))

        select = discord.ui.Select(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            custom_id="cover_artist_select",
            disabled=self.expired,
        )
        select_row = discord.ui.ActionRow()
        select_row.add_item(select)
        container.add_item(select_row)

        if self.selected is not None:
            covers = self._current_covers()
            total = self._total_pages()
            self.page = max(0, min(self.page, total - 1))

            container.add_item(discord.ui.Separator())
            if self._media_names:
                container.add_item(discord.ui.MediaGallery(*[
                    discord.MediaGalleryItem(media=discord.UnfurledMediaItem(url=f"attachment://{fname}"))
                    for fname in self._media_names
                ]))
            else:
                container.add_item(discord.ui.TextDisplay("⚠️ Couldn't load these covers."))
            label = "All artists" if self.selected == self.ALL else self.selected
            container.add_item(discord.ui.TextDisplay(
                f"-# {label} • {len(covers)} cover(s) • Page {self.page + 1}/{total}"
            ))

            if total > 1:
                prev_btn = discord.ui.Button(
                    label="◀ Previous",
                    style=discord.ButtonStyle.secondary,
                    custom_id="cover_prev",
                    disabled=self.expired,
                )
                next_btn = discord.ui.Button(
                    label="Next ▶",
                    style=discord.ButtonStyle.secondary,
                    custom_id="cover_next",
                    disabled=self.expired,
                )
                nav = discord.ui.ActionRow()
                nav.add_item(prev_btn)
                nav.add_item(next_btn)
                container.add_item(nav)

        self.add_item(container)

    async def render(self) -> list[discord.File]:
        """fetch + downscale the current page's covers and return them as attachments using containers (s/o flow)"""
        if self.selected is None:
            self._media_names = []
            self._build()
            return []

        covers = self._current_covers()
        total = self._total_pages()
        self.page = max(0, min(self.page, total - 1))
        start = self.page * self.PER_PAGE
        page_covers = covers[start:start + self.PER_PAGE]

        results = await asyncio.gather(*[self.cog.fetch_cover_thumb(url) for url, _name in page_covers])

        files = []
        media_names = []
        for idx, ((url, _name), data) in enumerate(zip(page_covers, results)):
            if data is None:
                continue
            fname = f"cover_{idx}.jpg"
            files.append(discord.File(BytesIO(data), filename=fname))
            media_names.append(fname)

        self._media_names = media_names
        self._build()
        
        neighbours = []
        if total > 1:
            for pidx in {(self.page - 1) % total, (self.page + 1) % total}:
                s = pidx * self.PER_PAGE
                neighbours.extend(url for url, _name in covers[s:s + self.PER_PAGE])
        neighbours = [u for u in neighbours if u not in self.cog.cover_thumb_cache]
        if neighbours:
            self.cog._cover_spawn(self.cog.prefetch_cover_thumbs(neighbours))

        return files

    def _clone(self, author_id: int) -> "CoverArtistView":
        clone = CoverArtistView(self.cog, self.song_name, self.covers_by_artist, author_id)
        clone.selected = self.selected
        clone.page = self.page
        clone.artist_page = self.artist_page
        return clone

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        is_owner = interaction.user.id == self.author_id
        target = self if is_owner else self._clone(interaction.user.id)

        custom_id = interaction.data.get("custom_id")
        refresh_media = False
        if custom_id == "cover_artist_select":
            value = interaction.data["values"][0]
            if value == self.PREV_ARTISTS:
                target.artist_page -= 1
            elif value == self.NEXT_ARTISTS:
                target.artist_page += 1
            else:
                target.selected = value
                target.page = 0
                refresh_media = True
        elif custom_id == "cover_prev":
            target.page = (target.page - 1) % target._total_pages()  
            refresh_media = True
        elif custom_id == "cover_next":
            target.page = (target.page + 1) % target._total_pages()  
            refresh_media = True
        else:
            return False

        if not is_owner:
            await interaction.response.defer(thinking=True, ephemeral=True)
            files = await target.render()
            target.message = await interaction.followup.send(view=target, files=files, ephemeral=True)
            return False

        await interaction.response.defer()
        if refresh_media:
            files = await target.render()
            await interaction.edit_original_response(view=target, attachments=files)
        else:
            target._build()
            await interaction.edit_original_response(view=target)
        return False

    async def on_timeout(self):
        self.expired = True
        self._build()
        try:
            if self.message is not None:
                await self.message.edit(view=self)
        except discord.NotFound:
            pass
        except discord.HTTPException:
            # Message edit failed due to a Discord API issue (e.g. 503).
            # The view has still timed out; leave the message as-is.
            pass

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
    logger.debug("Music cog initialized successfully")

class LatestSurfacesView(discord.ui.LayoutView):
    def __init__(self, cog, songs, author, per_page: int = 4):
        super().__init__(timeout=30)
        self.cog = cog
        self.author = author
        self.songs = songs
        self.per_page = per_page
        self.page = 0
        self.building = False

    def max_page(self):
        return max((len(self.songs) - 1) // self.per_page, 0)

    def slice(self):
        start = self.page * self.per_page
        return self.songs[start:start + self.per_page]

    async def build_page(self):
        if self.building:
            return
        self.building = True

        self.clear_items()

        container = discord.ui.Container(accent_color=0xffffff)
        container.add_item(discord.ui.TextDisplay('### Latest Surfaces (Last 30 Days)'))
        container.add_item(discord.ui.Separator())

        await self.cog.build_song_items(container, self.slice())
        nav = discord.ui.ActionRow()
        nav.add_item(discord.ui.Button(label='Previous', style=discord.ButtonStyle.grey, custom_id='latest_prev'))
        nav.add_item(discord.ui.Button(label='Next', style=discord.ButtonStyle.grey, custom_id='latest_next'))
        nav.add_item(discord.ui.Button(label='Tracker', emoji='\U0001F517', url=f'{JUICEWRLD_API}/'))
        
        container.add_item(discord.ui.Separator())
        container.add_item(nav)

        self.add_item(container)

        self.building = False

    async def interaction_check(self, itn: discord.Interaction):
        if itn.user != self.author:
            return await itn.response.send_message("this aint ur shit bro", ephemeral=True)

        cid = itn.data.get('custom_id')
        max_page = self.max_page()

        if cid == 'latest_prev':
            self.page = (self.page - 1) % (max_page + 1)
        elif cid == 'latest_next':
            self.page = (self.page + 1) % (max_page + 1)
        else:
            return True

        await self.build_page()
        await itn.response.edit_message(view=self)
        return False
    
    async def on_timeout(self):
        action_row = self.children[0].children[-1]
        action_row.remove_item(action_row.children[0])
        action_row.remove_item(action_row.children[0])

        try:
            await self.message.edit(view=self)
        except discord.NotFound:
            pass
        except discord.HTTPException:
            pass

def _groupbuy_flag(value: bool | None) -> str:
    """👍/👎/❔ for the Info section — the tracker mirrors the plain boolean."""
    if value is None:
        return '\u2754'
    return '\U0001F44D' if value else '\U0001F44E'


class GroupbuySelect(discord.ui.Select):
    """Dropdown that browses the tracker by song or year."""

    def __init__(self, view: "GroupbuyView", options: list[discord.SelectOption]):
        self.groupbuy_view = view
        super().__init__(
            placeholder='Choose a song or year...',
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, itn: discord.Interaction):
        view = self.groupbuy_view
        value = self.values[0]

        if itn.user != view.author:
            # Anyone else gets their own copy instead of hijacking the card.
            if value.startswith('song:'):
                entry = view.candidates[int(value.split(':', 1)[1])]
                return await itn.response.send_message(
                    view=view.cog.groupbuy_card_view(entry), ephemeral=True
                )
            return await itn.response.send_message(
                'Run `gb` to browse the tracker yourself.', ephemeral=True
            )

        if value.startswith('year:'):
            year = int(value.split(':', 1)[1])
            view.current = None
            view.years = []
            view.candidates = GROUPBUY_CATALOG.for_year(view.entries, year)
            view.hint = f'### {year} Group Buys / Personal Buys\n-# Choose a track below.'
        else:
            view.current = view.candidates[int(value.split(':', 1)[1])]
            view.hint = None

        view._build()
        await itn.response.edit_message(view=view)


class GroupbuyView(discord.ui.LayoutView):
    """A tracker card, plus a dropdown to jump between songs and years."""

    def __init__(
        self,
        cog: Music,
        author: discord.abc.User,
        entries: list[Groupbuy],
        *,
        current: Groupbuy | None = None,
        candidates: list[Groupbuy] | None = None,
        years: list[int] | None = None,
        hint: str | None = None,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.author = author
        self.entries = entries
        self.current = current
        self.candidates = list(candidates or [])
        self.years = list(years or [])
        self.hint = hint
        self.message = None
        self._build()

    def _accent(self) -> int:
        if self.current is None:
            return 0x2B2D31
        return self.cog.groupbuy_era_colour(self.current.era)

    def _options(self) -> list[discord.SelectOption]:
        if self.years and not self.candidates:
            return [
                discord.SelectOption(label=str(year), value=f'year:{year}')
                for year in self.years[:25]
            ]
        options: list[discord.SelectOption] = []
        for index, entry in enumerate(self.candidates[:25]):
            option = discord.SelectOption(
                label=self.cog.groupbuy_option_label(entry),
                value=f'song:{index}',
            )
            if entry.price:
                option.description = format_price(entry.price)
            emoji = self.cog.groupbuy_era_emoji(entry.era)
            if emoji is not None:
                option.emoji = emoji
            options.append(option)
        return options

    def _build(self) -> None:
        self.clear_items()

        container = discord.ui.Container(accent_color=self._accent())
        if self.current is not None:
            self.cog.build_groupbuy_card(container, self.current)
        else:
            container.add_item(discord.ui.TextDisplay(self.hint or '### GB/PB Tracker'))
        self.add_item(container)

        options = self._options()
        if options:
            row = discord.ui.ActionRow()
            row.add_item(GroupbuySelect(self, options))
            self.add_item(row)

        link_row = discord.ui.ActionRow()
        link_row.add_item(discord.ui.Button(
            label='Tracker',
            emoji='\U0001F517',
            url=f'https://docs.google.com/spreadsheets/d/{GROUPBUY_SHEET_ID}/',
        ))
        self.add_item(link_row)

    async def on_timeout(self) -> None:
        if self.message is None:
            return
        self.years = []
        self.candidates = []
        self._build()
        try:
            await self.message.edit(view=self)
        except (discord.NotFound, discord.HTTPException):
            pass
