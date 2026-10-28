"""Photo dump command for the JuiceWRLD API's ``Others/Photos`` archive.

Photos are filed as ``Others/Photos/<year> Photos/<Month>/<YYYY-MM-DD ...>``.
The browse search endpoint matches partial dates, so a request for one day is a
single search plus a name check that keeps only the files actually dated that
day (and, when a year is given, only that year). The images are shown in a
paginated :class:`discord.ui.MediaGallery`, with the direct API download URL
used for each image.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Any, Optional
from urllib.parse import quote

import aiohttp
import discord
from discord.ext import commands

try:
    from config import EMBED_COLOR
except ImportError:
    EMBED_COLOR = discord.Color.from_str("#2b2d31")

# Reuse the exact same date parser the recorded/recordedon commands use so
# "October 4", "4 October", "October 4th" and "2019-10-04" all behave.
from cogs.songinfo import MONTH_NAMES, parse_recorded_date

logger = logging.getLogger(__name__)

API_ROOT = "https://juicewrldapi.com"
API_BASE = f"{API_ROOT}/juicewrld"
BROWSE_ENDPOINT = f"{API_BASE}/files/browse/"
DOWNLOAD_BASE = f"{API_BASE}/files/download/"

PHOTOS_ROOT = "Others/Photos"
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif")

PHOTOS_PER_PAGE = 10
SEARCH_PAGE_SIZE = 200
MAX_SEARCH_PAGES = 5
REQUEST_TIMEOUT = 25
VIEW_TIMEOUT = 600

#: The random gallery is capped at one embed with no pagination. Discord allows
#: at most 10 items in a single MediaGallery, which is exactly this ceiling.
RANDOM_PHOTOS_MAX = 10
#: Browse search term whose results span the whole ``Others/Photos`` tree, so
#: the random commands can index every photo in a couple of requests instead
#: of walking every year/month folder.
PHOTO_INDEX_SEARCH = "Photos"
PHOTO_INDEX_TTL = 1800
MAX_INDEX_PAGES = 20

#: File names are prefixed with the date they were taken, e.g.
#: ``2019-10-04 20 - Chris Long.jpg``.
DATE_PREFIX_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}


def make_download_url(path: str) -> str | None:
    normalized = str(path or "").strip().lstrip("/")
    if not normalized:
        return None
    return f"{DOWNLOAD_BASE}?path={quote(normalized, safe='')}&channel=comp"


def format_date_label(month: int, day: int, year: int | None) -> str:
    month_name = MONTH_NAMES[month] if 1 <= month <= 12 else "?"
    return f"{month_name} {day}" + (f", {year}" if year else "")


def photo_date_label(name: str) -> str | None:
    """The ``Month D, YYYY`` label for a photo file name, if it is dated."""
    match = DATE_PREFIX_RE.match(name or "")
    if not match:
        return None
    return format_date_label(int(match.group(2)), int(match.group(3)), int(match.group(1)))


class PhotoGalleryView(discord.ui.LayoutView):
    """A paginated gallery of the photos found for a date."""

    def __init__(
        self,
        photos: list[dict[str, str]],
        *,
        title: str,
        author_id: int,
        timeout: int = VIEW_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)
        self.photos = photos
        self.gallery_title = title
        self.author_id = author_id
        self.page = 0
        self._render()

    def _page_count(self) -> int:
        return max(1, (len(self.photos) + PHOTOS_PER_PAGE - 1) // PHOTOS_PER_PAGE)

    def _render(self) -> None:
        self.clear_items()

        pages = self._page_count()
        self.page = max(0, min(self.page, pages - 1))
        start = self.page * PHOTOS_PER_PAGE
        chunk = self.photos[start:start + PHOTOS_PER_PAGE]

        container = discord.ui.Container(accent_color=EMBED_COLOR)

        count = len(self.photos)
        header = (
            f"### 📸 {self.gallery_title}\n"
            f"-# {count} photo{'s' if count != 1 else ''}"
        )
        if pages > 1:
            header += f" • page {self.page + 1}/{pages}"
        container.add_item(discord.ui.TextDisplay(header))
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))

        container.add_item(
            discord.ui.MediaGallery(
                *[
                    discord.MediaGalleryItem(media=photo["url"], description=photo["name"])
                    for photo in chunk
                ]
            )
        )

        if pages > 1:
            container.add_item(
                discord.ui.ActionRow(
                    self._button("◀ Prev", enabled=self.page > 0, callback=self._prev),
                    self._button("Next ▶", enabled=self.page < pages - 1, callback=self._next),
                )
            )

        self.add_item(container)

    @staticmethod
    def _button(label: str, *, enabled: bool, callback) -> discord.ui.Button:
        button = discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.secondary,
            disabled=not enabled,
        )
        button.callback = callback
        return button

    async def _prev(self, interaction: discord.Interaction) -> None:
        await self._change_page(interaction, -1)

    async def _next(self, interaction: discord.Interaction) -> None:
        await self._change_page(interaction, 1)

    async def _change_page(self, interaction: discord.Interaction, delta: int) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Only the person who ran the command can change pages.",
                ephemeral=True,
            )
            return

        self.page += delta
        self._render()
        await interaction.response.edit_message(view=self)


class Photos(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        #: Every image under ``Others/Photos``, cached for the random commands.
        self._photo_index: list[dict[str, str]] = []
        self._photo_index_ts = 0.0
        self._photo_index_lock = asyncio.Lock()

    async def cog_unload(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                headers=HEADERS,
            )
        return self.session

    async def _browse(self, params: dict[str, Any]) -> dict[str, Any]:
        session = await self.get_session()
        try:
            async with session.get(BROWSE_ENDPOINT, params=params, headers=HEADERS) as resp:
                if resp.status != 200:
                    return {}
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return {}

    @staticmethod
    def _parse_item(
        item: Any, month: int, day: int, year: int | None
    ) -> dict[str, str] | None:
        if not isinstance(item, dict) or item.get("type") != "file":
            return None

        path = str(item.get("path") or "")
        if not path.lower().startswith(f"{PHOTOS_ROOT.lower()}/"):
            return None

        name = str(item.get("name") or "")
        if not name.lower().endswith(IMAGE_EXTENSIONS):
            return None

        match = DATE_PREFIX_RE.match(name)
        if not match:
            return None
        if int(match.group(2)) != month or int(match.group(3)) != day:
            return None
        if year is not None and int(match.group(1)) != year:
            return None

        url = make_download_url(path)
        if not url:
            return None
        return {"name": name, "path": path, "url": url}

    async def search_photos(
        self, month: int, day: int, year: int | None = None
    ) -> list[dict[str, str]]:
        """Every photo dated ``month``/``day`` (optionally only in ``year``)."""
        query = (
            f"{year:04d}-{month:02d}-{day:02d}"
            if year is not None
            else f"{month:02d}-{day:02d}"
        )

        photos: list[dict[str, str]] = []
        seen: set[str] = set()

        for page in range(1, MAX_SEARCH_PAGES + 1):
            data = await self._browse(
                {"search": query, "page": page, "page_size": SEARCH_PAGE_SIZE}
            )
            for item in data.get("items") or []:
                photo = self._parse_item(item, month, day, year)
                if not photo:
                    continue
                key = photo["path"].casefold()
                if key in seen:
                    continue
                seen.add(key)
                photos.append(photo)

            if not data.get("has_more"):
                break

        photos.sort(key=lambda photo: photo["name"].lower())
        return photos

    @staticmethod
    def _index_photo(item: Any) -> dict[str, str] | None:
        """A photo row from a browse item, with no date filter applied."""
        if not isinstance(item, dict) or item.get("type") != "file":
            return None

        path = str(item.get("path") or "")
        if not path.lower().startswith(f"{PHOTOS_ROOT.lower()}/"):
            return None

        name = str(item.get("name") or "")
        if not name.lower().endswith(IMAGE_EXTENSIONS):
            return None

        url = make_download_url(path)
        if not url:
            return None
        return {"name": name, "path": path, "url": url}

    async def fetch_photo_index(self, force: bool = False) -> list[dict[str, str]]:
        """Every photo in the archive, cached so random picks don't re-scrape."""
        now = asyncio.get_running_loop().time()
        if (
            not force
            and self._photo_index
            and (now - self._photo_index_ts < PHOTO_INDEX_TTL)
        ):
            return self._photo_index

        async with self._photo_index_lock:
            now = asyncio.get_running_loop().time()
            if (
                not force
                and self._photo_index
                and (now - self._photo_index_ts < PHOTO_INDEX_TTL)
            ):
                return self._photo_index

            photos: list[dict[str, str]] = []
            seen: set[str] = set()
            for page in range(1, MAX_INDEX_PAGES + 1):
                data = await self._browse(
                    {
                        "search": PHOTO_INDEX_SEARCH,
                        "page": page,
                        "page_size": SEARCH_PAGE_SIZE,
                    }
                )
                items = data.get("items") or []
                if not items:
                    break
                for item in items:
                    photo = self._index_photo(item)
                    if not photo:
                        continue
                    key = photo["path"].casefold()
                    if key in seen:
                        continue
                    seen.add(key)
                    photos.append(photo)
                if not data.get("has_more"):
                    break

            if photos:
                photos.sort(key=lambda photo: photo["name"].lower())
                self._photo_index = photos
                self._photo_index_ts = now
                logger.info("[Photos] Indexed %d photos for the random commands", len(photos))

        return self._photo_index

    async def random_photos(self, count: int) -> list[dict[str, str]]:
        """A random, duplicate-free sample of up to :data:`RANDOM_PHOTOS_MAX`."""
        index = await self.fetch_photo_index()
        if not index:
            index = await self.fetch_photo_index(force=True)
        if not index:
            return []
        count = max(1, min(int(count), RANDOM_PHOTOS_MAX, len(index)))
        return random.sample(index, count)

    @commands.command(
        name="photos",
        aliases=["photo", "photodump"],
        description="Get photos taken on a specific date.",
    )
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def photos_command(self, ctx: commands.Context, *, date: str | None = None) -> None:
        if not date:
            await ctx.send(
                "# Command: photos\n"
                "-# Get Juice WRLD photos taken on a date (from `Others/Photos`).\n\n"
                "**Syntax** · `!photos <date>`\n"
                "**Examples** · `!photos October 4` · `!photos 4 October` · "
                "`!photos October 4th` · `!photos 2018-06-04`"
            )
            return

        parsed = parse_recorded_date(date)
        if not parsed:
            await ctx.send(
                f"❌ I couldn't understand the date `{date}`. Try something like "
                "`!photos October 4`, `!photos 4 October`, `!photos October 4th` "
                "or `!photos 2018-06-04`."
            )
            return

        month, day, year = parsed
        label = format_date_label(month, day, year)

        loading = await ctx.send(f"🔍 Searching photos from **{label}**...")

        photos = await self.search_photos(month, day, year)
        if not photos:
            await loading.edit(
                content=f"❌ No photos found for **{label}**."
            )
            return

        view = PhotoGalleryView(photos, title=label, author_id=ctx.author.id)
        await loading.edit(content=None, view=view)

    @commands.command(
        name="randomphoto",
        aliases=["randompic", "randphoto"],
        description="Show one random photo from the archive.",
    )
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def randomphoto_command(self, ctx: commands.Context) -> None:
        photos = await self.random_photos(1)
        if not photos:
            await ctx.send("❌ I couldn't find any photos right now. Try again in a moment.")
            return

        photo = photos[0]
        label = photo_date_label(photo["name"]) or photo["name"]
        view = PhotoGalleryView(
            [photo], title=f"Random · {label}", author_id=ctx.author.id
        )
        await ctx.send(view=view)

    @commands.command(
        name="randomphotos",
        aliases=["randompics", "randphotos"],
        description="Show a single gallery of random photos (at most 10).",
    )
    @commands.cooldown(1, 8, commands.BucketType.user)
    async def randomphotos_command(
        self, ctx: commands.Context, count: int = RANDOM_PHOTOS_MAX
    ) -> None:
        count = max(1, min(count, RANDOM_PHOTOS_MAX))
        photos = await self.random_photos(count)
        if not photos:
            await ctx.send("❌ I couldn't find any photos right now. Try again in a moment.")
            return

        view = PhotoGalleryView(photos, title="Random photos", author_id=ctx.author.id)
        await ctx.send(view=view)

    @photos_command.error
    async def photos_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ Try again in **{error.retry_after:.1f}s**.")
            return

        logger.exception("[Photos] Command error: %s", error)
        await ctx.send(f"❌ **Photos error:** `{error}`")

    @randomphoto_command.error
    async def randomphoto_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        await self.photos_error(ctx, error)

    @randomphotos_command.error
    async def randomphotos_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        await self.photos_error(ctx, error)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Photos(bot))
