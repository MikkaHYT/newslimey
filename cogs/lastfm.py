"""Last.fm — the reference bot's Last.fm integration, running on the main bot.

The reference bot owned its own ``lf`` group. The main bot's ``lf`` group lives
in :mod:`cogs.music`, so the commands here are collected with :func:`lf_sub` and
added to that existing group when the bot loads (see
:meth:`LastFM.attach_commands`). Anything the main bot already answers to the
same name is left alone.

Links are the main bot's own: usernames come from its SQLite database (via
:class:`database.managers.music.MusicMixin`), while the reference bot's local
scrobble store lives in its separate SQLite file on ``bot.db``.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import aiohttp
import discord
from discord.ext import commands, tasks

from config import EMBED_COLOR, PREFIX

from utils.core import charts
from utils.core import lastfm_data as store

logger = logging.getLogger("discord.client")

LASTFM_API_KEY = os.getenv("LASTFM_API_KEY", "f63c88e0962384991836abffb7957c30")
LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"
#: Only needed for the OAuth (scrobbling) half. Without it every write command
#: reports that scrobbling isn't set up instead of failing opaquely.
LASTFM_API_SECRET = os.getenv("LASTFM_API_SECRET", "").strip()
LASTFM_AUTH_URL = "https://www.last.fm/api/auth/"

DATA_DIR = Path("data")
DATA_FILE = DATA_DIR / "lastfm.json"
PER_PAGE = 10

#: ``!lf plays`` windows -> how many days back they reach. Last.fm spells its
#: own periods ``7day``/``1month``/... so a bare number of days is all we need.
PLAY_PERIODS = {
    "7day": 7,
    "1month": 30,
    "3month": 90,
    "6month": 180,
    "12month": 365,
}

#: How many linked members a server-wide command will look up before stopping.
#: Each one costs a Last.fm call, and the API rate-limits bursts.
SERVER_MEMBER_LIMIT = 15

#: How long a "this member has no Last.fm link" answer is trusted before the
#: database is asked again (seconds). Positive links are cached forever.
LINK_MISS_TTL = 120

#: How long the full list of linked accounts is reused before it is re-read
#: from the database (seconds).
LINKED_ACCOUNTS_TTL = 120

# Presences are only pushed when they change, so a member who started listening
# before the bot came online can sit without one; this is how long to wait
# before asking the gateway to re-send a member's presence again (seconds).
PRESENCE_REFRESH_COOLDOWN = 20

#: Scrobble polling: how often it runs, how much recent history it pulls per
#: member, and how many members one cycle touches (the Last.fm API is rate
#: limited, so a big bot is served over several cycles rather than one burst).
SCROBBLE_POLL_MINUTES = 5
SCROBBLE_BACKFILL = 50
SCROBBLE_USER_LIMIT = 60

#: Scrobble counts worth naming as a milestone, smallest first.
MILESTONES = (
    1, 10, 25, 50, 100, 250, 500, 1_000, 2_500, 5_000, 10_000, 25_000, 50_000, 100_000,
)

#: Iceberg tiers as ``(label, minimum global listeners)``, widest first. An
#: artist lands in the first tier whose threshold it clears.
ICEBERG_TIERS = (
    ("Surface", 50_000_000),
    ("Mainstream", 10_000_000),
    ("Well known", 2_000_000),
    ("Underground", 500_000),
    ("Deep underground", 100_000),
    ("Unknown", 0),
)

#: Tiers that float above the waterline in the rendered iceberg; the rest sink
#: below it, so how much of an artist's list is "above ground" matches how
#: mainstream the artists are.
ICEBERG_ABOVE_TIERS = frozenset({"Surface", "Mainstream", "Well known"})

#: The judge's brief: a short, fair review of a member's taste.
JUDGE_SYSTEM_INSTRUCTION = (
    "You are a sharp but fair music critic. The user gives you a Discord member's top "
    "artists, top tracks, top genres and a few recent scrobbles from last.fm. Write a "
    "short review of their taste in 3-5 sentences: what it says about them, what is "
    "strong about it, and one gentle suggestion for something new. Name real artists and "
    "tracks from the list to stay specific. Be warm and witty, never insult the person. "
    "Plain sentences only - no headings, no bullet points, no lists. Keep it under 900 "
    "characters."
)

# ============================================================
# DATA PERSISTENCE
# ============================================================
#
# Only per-member settings live here now (the custom now-playing command set
# with `lf cc`). Links come from the main bot's own database instead.

def ensure_data() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not DATA_FILE.exists():
        DATA_FILE.write_text("{}", encoding="utf-8")


def load_data() -> dict:
    ensure_data()
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_data(data: dict) -> None:
    ensure_data()
    DATA_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


# ============================================================
# HELPERS
# ============================================================

def text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def shorten(value: str, limit: int = 70) -> str:
    value = text(value)
    return value if len(value) <= limit else value[: limit - 3] + "..."


def image_from_list(images: Any) -> Optional[str]:
    if not isinstance(images, list):
        return None

    preferred = ["extralarge", "large", "medium", "small"]
    for wanted in preferred:
        for image in images:
            if not isinstance(image, dict) or image.get("size") != wanted:
                continue
            url = text(image.get("#text"))
            # Filter out Last.fm default missing-artwork star placeholder
            if url.startswith("http") and "2a96cbd8b46e442fc41c2b86b821562f" not in url:
                return url
    return None


def format_number(value: Any) -> str:
    try:
        return f"{int(str(value).replace(',', '')):,}"
    except Exception:
        return "0"


def format_clock(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes}:{secs:02d}"


def activity_progress(activity: Any) -> Optional[str]:
    """Elapsed/total playtime for a listening activity, if it has timestamps."""
    start = getattr(activity, "start", None)
    end = getattr(activity, "end", None)

    # Fallback to reading timestamps dict directly if present
    timestamps = getattr(activity, "timestamps", {}) or {}
    if not start and "start" in timestamps:
        s_val = timestamps["start"]
        if isinstance(s_val, (int, float)):
            start = datetime.fromtimestamp(s_val / (1000 if s_val > 1e11 else 1), tz=timezone.utc)
    if not end and "end" in timestamps:
        e_val = timestamps["end"]
        if isinstance(e_val, (int, float)):
            end = datetime.fromtimestamp(e_val / (1000 if e_val > 1e11 else 1), tz=timezone.utc)

    if not start or not end:
        return None
    try:
        total = (end - start).total_seconds()
        if total <= 0:
            return None
        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    except Exception:
        return None
    elapsed = max(0.0, min(elapsed, total))
    return f"{format_clock(elapsed)} / {format_clock(total)}"


def asset_url(activity: Any) -> Optional[str]:
    """Resolve an activity's large image into a URL the embed can actually use.

    Music apps other than Spotify (Apple Music, Deezer, ...) ship their cover as
    an ``mp:external/...`` proxy asset with the real URL percent-encoded inside.
    """
    assets = getattr(activity, "assets", None)
    asset = text(assets.get("large_image")) if isinstance(assets, dict) else ""
    if not asset:
        return None

    if asset.startswith("spotify:"):
        return "https://i.scdn.co/image/" + asset[len("spotify:"):]
    if asset.startswith(("http://", "https://")):
        return asset

    if asset.startswith("mp:external/"):
        for scheme in ("https", "http"):
            _, _, tail = asset.partition(f"/{scheme}/")
            if not tail:
                continue
            url = tail.replace("%2F", "/").replace("%2f", "/").lstrip("/")
            if url.startswith(("http://", "https://")):
                return url
            if url:
                return "https://" + url
        return None

    application_id = getattr(activity, "application_id", None)
    if application_id:
        return f"https://cdn.discordapp.com/app-assets/{application_id}/{asset}.png"
    return None


def is_listening(activity: Any) -> bool:
    """Whether an activity is a song rather than a game, stream or status."""
    if isinstance(activity, discord.Spotify):
        return bool(text(activity.title))

    act_type = getattr(activity, "type", None)
    act_name = text(getattr(activity, "name", "")).lower()

    # Match standard listening type or Windows Media Player activities
    is_wmp = "windows media player" in act_name or act_name == "wmp"
    if act_type is discord.ActivityType.listening or is_wmp:
        return any(text(getattr(activity, field, "")) for field in ("details", "state", "title"))

    return False


def listening_activity(member: Any) -> Optional[Any]:
    """The member's Spotify activity if there is one, else any other music app."""
    playing = [a for a in getattr(member, "activities", ()) or () if is_listening(a)]
    if not playing:
        return None
    return next((a for a in playing if isinstance(a, discord.Spotify)), playing[0])


def describe_listening(activity: Any) -> tuple[str, str, str, str, Optional[str]]:
    """Split an activity into (title, artist, album, platform, cover art)."""
    if isinstance(activity, discord.Spotify):
        artists = ", ".join(a for a in activity.artists if text(a))
        return (
            text(activity.title) or "Unknown Track",
            artists or "Unknown Artist",
            text(activity.album),
            "Spotify",
            text(activity.album_cover_url) or None,
        )

    platform = text(getattr(activity, "name", "")) or "Listening"
    raw_title = text(getattr(activity, "details", "")) or text(getattr(activity, "title", "")) or "Unknown Track"
    raw_artist = text(getattr(activity, "state", "")) or "Unknown Artist"
    album = text(getattr(activity, "large_image_text", ""))

    title = raw_title
    artist = raw_artist

    # If artist is empty/generic or matches platform name, check if details has "Artist - Title"
    if (artist in {"Unknown Artist", platform}) and " - " in title:
        parts = title.split(" - ", 1)
        artist = parts[0].strip()
        title = parts[1].strip()

    if album and album.casefold() in {title.casefold(), artist.casefold(), platform.casefold()}:
        album = ""

    return title, artist, album, platform, asset_url(activity)


def parse_query_and_artist(raw_query: str) -> tuple[str, str]:
    query = raw_query.strip()
    if "|" in query:
        parts = query.split("|", 1)
        return parts[0].strip(), parts[1].strip()
    if " - " in query:
        parts = query.split(" - ", 1)
        return parts[0].strip(), parts[1].strip()
    if "," in query:
        parts = query.split(",", 1)
        return parts[0].strip(), parts[1].strip()
    parts = query.rsplit(" ", 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return query, ""


# Markdown characters that would otherwise break out of a bold link label.
_MARKDOWN_ESCAPES = str.maketrans({char: f"\\{char}" for char in "\\*_~`|[]"})


def md_escape(value: Any) -> str:
    """Make a name safe to drop inside ``**bold**`` or a link label."""
    return str(value or "").translate(_MARKDOWN_ESCAPES)


def track_label(name: str, artist: str = "", url: str = "") -> str:
    """``**Artist** - [**Track**](url)`` - how one track is written here."""
    label = f"[**{md_escape(name)}**]({url})" if url else f"**{md_escape(name)}**"
    return f"**{md_escape(artist)}** - {label}" if artist else label


def entry_line(
    rank: int,
    name: str,
    *,
    artist: str = "",
    url: str = "",
    suffix: str = "",
    marker: str = "",
) -> str:
    """One numbered, single-line list entry: ``1. Artist - Track - 5 plays``.

    Every list in this cog is built from this, so a card of ten songs is ten
    short lines rather than ten two-line blocks.
    """
    line = f"{rank}. {marker}{track_label(name, artist, url)}"
    return f"{line} - {suffix}" if suffix else line


def plays_suffix(count: Any) -> str:
    """``5 plays`` - the trailing count charts and rankings end with."""
    total = format_number(count)
    return f"{total} play" + ("" if str(total) == "1" else "s")


def join_stats(*pairs: tuple[str, Any]) -> str:
    """``**Scrobbles:** 12,345 · **Country:** US`` on a single line."""
    return " · ".join(
        f"**{label}:** {value}" for label, value in pairs if value not in (None, "")
    )


# ============================================================
# UI COMPONENTS
# ============================================================

class LastFMView(discord.ui.LayoutView):
    """The shared Last.fm card: a small heading, then tight body lines.

    Deliberately compact - no separators between the body blocks and a third-
    level heading instead of a big title - so a card of ten songs stays a short
    list rather than a wall of text.
    """

    def __init__(
        self,
        *,
        title: str,
        sections: list[str],
        thumbnail: Optional[str] = None,
        thumbnail_index: Optional[int] = None,
        buttons: Optional[list[discord.ui.Button]] = None,
        media: Optional[str] = None,
    ):
        super().__init__(timeout=180)
        container = discord.ui.Container(accent_color=EMBED_COLOR)

        blocks = [str(section).strip() for section in sections if str(section).strip()]
        container.add_item(discord.ui.TextDisplay(f"### {title}"))

        if thumbnail_index is None:
            thumbnail_index = 1 if len(blocks) > 1 else 0
        thumbnail_index = min(max(thumbnail_index, 0), max(len(blocks) - 1, 0))

        for index, block in enumerate(blocks):
            if thumbnail and index == thumbnail_index:
                try:
                    container.add_item(
                        discord.ui.Section(
                            discord.ui.TextDisplay(block),
                            accessory=discord.ui.Thumbnail(media=thumbnail),
                        )
                    )
                    continue
                except Exception:
                    pass
            container.add_item(discord.ui.TextDisplay(block))

        # Components V2 only renders an attachment a component points at, so a
        # `file=` on its own would upload the image and show nothing.
        if media:
            try:
                container.add_item(
                    discord.ui.MediaGallery(discord.MediaGalleryItem(media=media))
                )
            except Exception:
                logger.warning("[LASTFM] Could not attach the rendered image", exc_info=True)

        if buttons:
            container.add_item(discord.ui.ActionRow(*buttons))

        self.add_item(container)


def _arrow(emoji: str) -> discord.ui.Button:
    """A compact, emoji-only pagination arrow button."""
    return discord.ui.Button(emoji=emoji, style=discord.ButtonStyle.secondary)


class PaginationView(discord.ui.LayoutView):
    """A paged chart card with first / prev / next / last arrows."""

    def __init__(
        self,
        cog: LastFM,
        ctx: commands.Context,
        mode: str,
        username: str,
        page: int,
        total_pages: int,
        total: int,
        title: str,
        body: str,
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.ctx = ctx
        self.mode = mode
        self.username = username
        self.page = page
        self.total_pages = max(total_pages, 1)
        self.total = max(total, 0)
        self.message: Optional[discord.Message] = None

        self.container = discord.ui.Container(accent_color=EMBED_COLOR)
        self.header = discord.ui.TextDisplay(f"### {title}")
        self.body = discord.ui.TextDisplay(body)
        self.footer = discord.ui.TextDisplay(self._footer_text())
        self.container.add_item(self.header)
        self.container.add_item(self.body)
        self.container.add_item(self.footer)

        self.first = _arrow("\u23ee\ufe0f")
        self.previous = _arrow("\u25c0\ufe0f")
        self.next = _arrow("\u25b6\ufe0f")
        self.last = _arrow("\u23ed\ufe0f")
        self.first.callback = self.first_callback
        self.previous.callback = self.previous_callback
        self.next.callback = self.next_callback
        self.last.callback = self.last_callback
        self.container.add_item(
            discord.ui.ActionRow(self.first, self.previous, self.next, self.last)
        )
        self.add_item(self.container)
        self.update_buttons()

    def _footer_text(self) -> str:
        noun = {"artists": "artists", "tracks": "tracks", "albums": "albums"}.get(
            self.mode, "entries"
        )
        return f"Page {self.page}/{self.total_pages} - {self.total:,} different {noun}"

    def update_buttons(self) -> None:
        at_start = self.page <= 1
        at_end = self.page >= self.total_pages
        self.first.disabled = at_start
        self.previous.disabled = at_start
        self.next.disabled = at_end
        self.last.disabled = at_end

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This menu isn't yours.", ephemeral=True)
            return False
        return True

    async def _move(self, interaction: discord.Interaction, target: int) -> None:
        target = max(1, min(target, self.total_pages))
        if target == self.page:
            return await interaction.response.defer()
        self.page = target
        await interaction.response.defer()
        await self.refresh(interaction)

    async def first_callback(self, interaction: discord.Interaction) -> None:
        await self._move(interaction, 1)

    async def previous_callback(self, interaction: discord.Interaction) -> None:
        await self._move(interaction, self.page - 1)

    async def next_callback(self, interaction: discord.Interaction) -> None:
        await self._move(interaction, self.page + 1)

    async def last_callback(self, interaction: discord.Interaction) -> None:
        await self._move(interaction, self.total_pages)

    async def refresh(self, interaction: discord.Interaction) -> None:
        try:
            items, total_pages, total = await self.cog.get_chart(
                self.mode, self.username, self.page
            )
            self.total_pages = max(total_pages, 1)
            self.total = max(total, 0)
            self.update_buttons()

            title, body = self.cog.format_chart(
                self.mode, self.username, items, self.page, self.total_pages, self.total
            )
            self.header.content = f"### {title}"
            self.body.content = body
            self.footer.content = self._footer_text()

            if interaction.message:
                await interaction.message.edit(view=self)
                self.message = interaction.message
        except Exception as exc:
            logger.warning(f"[LASTFM] Pagination error: {exc}")

    async def on_timeout(self) -> None:
        for button in (self.first, self.previous, self.next, self.last):
            button.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass


# ============================================================
# COMMAND REGISTRATION
# ============================================================

def lf_sub(name: str, aliases: tuple[str, ...] = ()) -> Callable:
    """Mark a cog method as an ``lf`` subcommand.

    The main bot already owns the ``lf`` group (see :mod:`cogs.music`), so these
    can't declare ``@group.command`` themselves. The methods are tagged here and
    turned into real commands by :meth:`LastFM.attach_commands`, which adds them
    to that group once it exists.
    """

    def decorator(func: Callable) -> Callable:
        func.__lf_command__ = (name, aliases)  # type: ignore[attr-defined]
        return func

    return decorator


# ============================================================
# COG
# ============================================================

class LastFM(commands.Cog):
    """The reference bot's Last.fm commands, attached to the main bot's ``lf``."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        #: Per-member custom now-playing command names (`lf cc`).
        self.data = load_data()
        #: Resolved links, raw Discord id -> Last.fm username.
        self._usernames: dict[int, str] = {}
        #: Recent "no link" answers, so member sweeps don't repeat lookups.
        self._misses: dict[int, float] = {}
        #: Cached ``(raw id, username)`` pairs across every server.
        self._accounts: list[tuple[int, str]] = []
        self._accounts_at = 0.0
        #: Members whose presence was last re-requested, and when.
        self._presence_refreshed: dict[int, float] = {}
        #: In-flight OAuth tokens, keyed by user id, until the authorise step
        #: is confirmed and turned into a session key.
        self._auth_tokens: dict[int, str] = {}
        #: Last.fm profile country per member, cached between cycles.
        self._countries: dict[int, str] = {}
        #: Set once the commands have been added to the main bot's `lf` group.
        self._attached: Optional[commands.Group] = None
        self._attach_task: Optional[asyncio.Task] = None

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession(
            headers={
                "User-Agent": "TPNEBOT Last.fm/1.0",
                "Accept": "application/json",
            }
        )
        if not self.scrobble_poller.is_running():
            self.scrobble_poller.start()
        # The `lf` group belongs to cogs.music, which may or may not have been
        # loaded before this cog, so fall back to waiting for it to appear.
        if not self.attach_commands():
            self._attach_task = asyncio.create_task(self._attach_when_ready())

    async def cog_unload(self) -> None:
        if self._attach_task is not None and not self._attach_task.done():
            self._attach_task.cancel()
        self.detach_commands()
        if self.scrobble_poller.is_running():
            self.scrobble_poller.cancel()
        if self.session and not self.session.closed:
            await self.session.close()

    # ----------------------------------------------------------------
    # Attaching to the main bot's `lf` group
    # ----------------------------------------------------------------

    @classmethod
    def command_specs(cls) -> list[tuple[str, tuple[str, ...], Callable]]:
        """Every ``lf_sub``-tagged method, as ``(name, aliases, function)``."""
        specs: list[tuple[str, tuple[str, ...], Callable]] = []
        for attribute in sorted(dir(cls)):
            func = getattr(cls, attribute)
            meta = getattr(func, "__lf_command__", None)
            if meta is None:
                continue
            specs.append((meta[0], tuple(meta[1]), func))
        return specs

    def attach_commands(self) -> bool:
        """Add the tagged commands to the main bot's `lf` group.

        Names and aliases the group already answers are skipped - those are the
        main bot's own ``lf`` commands, which keep their implementations.
        """
        group = self.bot.get_command("lf")
        if not isinstance(group, commands.Group):
            return False

        taken = set(group.all_commands)
        added = 0
        for name, aliases, func in self.command_specs():
            if name in taken:
                logger.info(
                    f"[LASTFM] `lf {name}` already exists on the main bot; keeping that one."
                )
                continue
            kept = tuple(alias for alias in aliases if alias not in taken)
            # Prefix-only hybrid commands: the group is a HybridGroup, and the
            # slash tree it already synced shouldn't grow by forty subcommands.
            command = commands.HybridCommand(
                func,
                name=name,
                aliases=kept,
                help=inspect.cleandoc(func.__doc__ or "").strip() or None,
                with_app_command=False,
            )
            command.cog = self  # type: ignore[assignment]
            try:
                group.add_command(command)
            except commands.CommandRegistrationError as exc:
                logger.warning(f"[LASTFM] Could not add `lf {name}`: {exc}")
                continue
            taken.add(name)
            taken.update(kept)
            added += 1

        self._attached = group
        logger.info(f"[LASTFM] Added {added} subcommand(s) to the `lf` group.")
        return True

    def detach_commands(self) -> None:
        """Take this cog's commands back off the `lf` group (on reload)."""
        group = self._attached
        if group is None:
            return
        for name, aliases, _func in self.command_specs():
            for key in (name, *aliases):
                command = group.all_commands.get(key)
                if command is None or command.cog is not self:
                    continue
                try:
                    group.remove_command(command.name)
                except commands.CommandNotFound:
                    pass
                break
        self._attached = None

    async def _attach_when_ready(self, timeout: float = 20.0) -> None:
        """Wait for the main bot's `lf` group to load, then attach to it."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.attach_commands():
                return
            await asyncio.sleep(0.1)
        logger.error(
            "[LASTFM] The main bot's `lf` group never appeared; "
            "the lastfm subcommands were not added."
        )

    async def api(self, method: str, **params) -> dict:
        if not LASTFM_API_KEY:
            raise RuntimeError("Last.fm API key isn't configured.")

        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={
                    "User-Agent": "TPNEBOT Last.fm/1.0",
                    "Accept": "application/json",
                }
            )

        query = {"method": method, "api_key": LASTFM_API_KEY, "format": "json", **params}

        async with self.session.get(
            LASTFM_API_URL, params=query, timeout=aiohttp.ClientTimeout(total=20)
        ) as response:
            raw = await response.text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                raise RuntimeError("Last.fm returned invalid data.")

            if "error" in data:
                raise RuntimeError(data.get("message", "Last.fm API error."))
            return data

    def remember(self, user_id: Any, username: Any) -> None:
        """Cache a resolved link, so later lookups skip the database."""
        name = text(username)
        if not name:
            return
        key = int(user_id)
        self._usernames[key] = name
        self._misses.pop(key, None)

    async def username_for(self, user_id: Any) -> Optional[str]:
        """Resolve a member's Last.fm username from the main bot's database.

        The main bot stores links in its own SQLite database, keyed by a hash of
        the Discord id, so that is the single source of truth for who is linked.
        Resolved names are cached here (and misses briefly) so member sweeps and
        presence events don't hammer the database.
        """
        try:
            key = int(user_id)
        except (TypeError, ValueError):
            return None

        cached = self._usernames.get(key)
        if cached:
            return cached

        now = time.monotonic()
        missed = self._misses.get(key)
        if missed is not None and now - missed < LINK_MISS_TTL:
            return None

        found: Optional[str] = None
        try:
            found = await self.bot.database.get_lastfm_username(key)
        except Exception:
            found = None

        if found:
            self.remember(key, found)
            return self._usernames[key]
        self._misses[key] = now
        return None

    async def linked_accounts(
        self, *, max_age: float = LINKED_ACCOUNTS_TTL
    ) -> list[tuple[int, str]]:
        """Every linked member the bot knows, as ``(raw id, username)``.

        The main bot's database stores links against hashed user ids; its
        ``user_identities`` map turns those back into the raw ids that the local
        scrobble store is keyed by.
        """
        now = time.monotonic()
        if self._accounts and now - self._accounts_at < max_age:
            return self._accounts

        accounts: list[tuple[int, str]] = []
        try:
            pairs = await self.bot.database.get_lastfm_accounts()
            mapping = await self.bot.database.resolve_user_hashes(
                [hashed for hashed, _name in pairs]
            )
        except Exception as exc:
            logger.warning(f"[LASTFM] Could not read linked accounts: {exc}")
            # Keep whatever was read last rather than caching an empty list.
            return self._accounts

        for hashed, username in pairs:
            raw_id = mapping.get(hashed)
            if raw_id is None:
                continue
            accounts.append((int(raw_id), text(username)))
            self.remember(raw_id, username)

        self._accounts = accounts
        self._accounts_at = now
        return accounts

    def error_view(self, message: str) -> LastFMView:
        return LastFMView(title="Last.fm", sections=[message])

    async def edit_card(self, message: Optional[discord.Message], view: LastFMView) -> None:
        """Swap a placeholder card for the finished one, or resend if it's gone."""
        if message is None:
            return
        try:
            await message.edit(view=view)
        except Exception:
            try:
                await message.channel.send(view=view)
            except Exception:
                pass

    async def vote_reactions(self, message: Optional[discord.Message]) -> None:
        """React with 👍/👎 so people can vote on a now-playing card."""
        if message is None:
            return
        for emoji in ("👍", "👎"):
            try:
                await message.add_reaction(emoji)
            except Exception:
                return

    async def resolve_target(
        self, ctx: commands.Context, member: Optional[discord.Member]
    ) -> tuple[discord.Member | discord.User, Optional[str]]:
        """The member a command is about, plus their linked Last.fm name."""
        target = member or ctx.author
        return target, await self.username_for(target.id)

    def resolve_member(
        self, ctx: commands.Context, member: Optional[discord.Member]
    ) -> Optional[discord.Member]:
        """Best-effort lookup of a member in this guild, for status commands."""
        target: discord.Member | discord.User = member or ctx.author
        if ctx.guild is not None and not isinstance(target, discord.Member):
            target = ctx.guild.get_member(target.id) or target
        return target if isinstance(target, discord.Member) else None

    async def refresh_presence(
        self, guild: discord.Guild, member: discord.Member
    ) -> discord.Member:
        """Re-request a member's presence from the gateway, at most every so often."""
        cached = guild.get_member(member.id) or member
        now = time.monotonic()
        if now - self._presence_refreshed.get(member.id, 0.0) < PRESENCE_REFRESH_COOLDOWN:
            return cached
        self._presence_refreshed[member.id] = now
        try:
            found = await guild.query_members(user_ids=[member.id], presences=True)
        except Exception as exc:
            logger.warning(f"[LASTFM] Presence refresh failed: {exc}")
            return cached
        for candidate in found:
            if candidate.id == member.id:
                return guild.get_member(member.id) or candidate
        return cached

    async def fetch_lastfm_metadata(
        self, title: str, artist: str = ""
    ) -> tuple[str, str, str, Optional[str]]:
        """Fetch closest match cover art, title, artist, and album from Last.fm."""
        try:
            # 1. Try track.getInfo if artist is known
            if title and artist and artist.lower() != "unknown artist":
                try:
                    data = await self.api("track.getInfo", track=title, artist=artist)
                    info = data.get("track", {})
                    f_title = text(info.get("name")) or title
                    f_artist = text(info.get("artist", {}).get("name") if isinstance(info.get("artist"), dict) else info.get("artist")) or artist
                    album_data = info.get("album", {})
                    f_album = text(album_data.get("title")) if isinstance(album_data, dict) else ""
                    artwork = image_from_list(album_data.get("image")) if isinstance(album_data, dict) else None

                    if artwork or f_album:
                        return f_title, f_artist, f_album, artwork
                except Exception:
                    pass

            # 2. Fall back to track.search
            query = f"{artist} {title}".strip() if artist and artist.lower() != "unknown artist" else title
            if not query:
                return title, artist, "", None

            data = await self.api("track.search", track=query, limit=1)
            matches = data.get("results", {}).get("trackmatches", {}).get("track", [])
            if isinstance(matches, dict):
                matches = [matches]

            if matches:
                first = matches[0]
                m_title = text(first.get("name")) or title
                m_artist = text(first.get("artist")) or artist
                artwork = image_from_list(first.get("image"))
                f_album = ""

                # Query getInfo with the matched details for full album art if missing
                if m_title and m_artist:
                    try:
                        info_data = await self.api("track.getInfo", track=m_title, artist=m_artist)
                        info = info_data.get("track", {})
                        album_data = info.get("album", {})
                        f_album = text(album_data.get("title")) if isinstance(album_data, dict) else ""
                        if not artwork and isinstance(album_data, dict):
                            artwork = image_from_list(album_data.get("image"))
                    except Exception:
                        pass

                return m_title, m_artist, f_album, artwork

        except Exception as exc:
            logger.warning(f"[LASTFM] Metadata lookup error: {exc}")

        return title, artist, "", None

    # ========================================================
    # NOW PLAYING (any music app)
    # ========================================================

    @commands.command(name="applemusic", aliases=("am", "amnp", "wmp", "spotify"))
    async def applemusic(self, ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        """Show what a member is listening to on Apple Music, Spotify or WMP.

        Unlike `snp`, this reads any music app's presence rather than Spotify
        only, and fills in missing cover art and album details from Last.fm.
        """
        target = self.resolve_member(ctx, member)
        if target is None:
            return await ctx.send(
                view=self.error_view("Couldn't read that member's status in this server.")
            )

        activity = listening_activity(target)
        if activity is None and ctx.guild is not None:
            target = await self.refresh_presence(ctx.guild, target)
            activity = listening_activity(target)

        if activity is None:
            err = (
                "You aren't listening to anything right now."
                if target.id == ctx.author.id
                else f"**{target.display_name}** isn't listening to anything right now."
            )
            return await ctx.send(view=self.error_view(err))

        title, artist, album, platform, cover = describe_listening(activity)

        # Attempt to grab missing cover art and album/artist matches from Last.fm
        if not cover or artist == "Unknown Artist":
            f_title, f_artist, f_album, f_cover = await self.fetch_lastfm_metadata(title, artist)
            if f_title:
                title = f_title
            if f_artist and artist in {"Unknown Artist", ""}:
                artist = f_artist
            if f_album and not album:
                album = f_album
            if f_cover and not cover:
                cover = f_cover

        header = f"**▶ Currently Playing** · {platform}" if platform else "**▶ Currently Playing**"
        lines = [
            f"**{target.display_name}**",
            header,
            track_label(title, artist),
        ]
        if album:
            lines.append(f"**Album:** {md_escape(album)}")
        progress = activity_progress(activity)
        if progress:
            lines.append(f"`{progress}`")

        message = await ctx.send(
            view=LastFMView(
                title="Now Playing",
                sections=["\n".join(lines)],
                thumbnail=cover,
            )
        )
        await self.vote_reactions(message)

    # ========================================================
    # COMMANDS
    # ========================================================
    #
    # Each command below is tagged with @lf_sub and registered onto the main
    # bot's own `lf` group when the cog loads. `lf set` is deliberately absent:
    # the main bot already ships one backed by its database, which is also where
    # the links used here are read from.

    async def get_chart(self, mode: str, username: str, page: int) -> tuple[list, int, int]:
        """One page of a chart, plus its page count and total entry count."""
        endpoints = {
            "artists": ("user.getTopArtists", "topartists", "artist"),
            "tracks": ("user.getTopTracks", "toptracks", "track"),
            "albums": ("user.getTopAlbums", "topalbums", "album"),
        }
        method, root_key, item_key = endpoints.get(mode, endpoints["artists"])

        data = await self.api(method, user=username, period="overall", page=page, limit=PER_PAGE)
        root = data.get(root_key, {})
        items = root.get(item_key, [])
        if isinstance(items, dict):
            items = [items]

        attrs = root.get("@attr", {})
        try:
            total_pages = int(attrs.get("totalPages", 1))
        except Exception:
            total_pages = 1
        try:
            total = int(attrs.get("total", 0))
        except Exception:
            total = 0

        return items, max(total_pages, 1), max(total, 0)

    def format_chart(
        self,
        mode: str,
        username: str,
        items: list,
        page: int,
        total_pages: int,
        total: int = 0,
    ) -> tuple[str, str]:
        """The heading and the one-line list, matching the paged card."""
        noun = {"artists": "artists", "tracks": "tracks", "albums": "albums"}.get(
            mode, "chart"
        )
        title = f"Top {noun} for {username}"
        lines = []
        start = ((page - 1) * PER_PAGE) + 1

        for index, item in enumerate(items):
            rank = start + index
            name = text(item.get("name"))
            url = text(item.get("url"))
            plays = plays_suffix(item.get("playcount"))

            if mode == "artists":
                lines.append(entry_line(rank, name, url=url, suffix=plays))
            else:
                artist_data = item.get("artist", {})
                artist = text(
                    artist_data.get("name") if isinstance(artist_data, dict) else artist_data
                )
                lines.append(entry_line(rank, name, artist=artist, url=url, suffix=plays))

        body = "\n".join(lines) if lines else "No listening data found."
        return title, body

    async def chart_command(self, ctx: commands.Context, mode: str) -> None:
        username = await self.username_for(ctx.author.id)
        if not username:
            return await ctx.send(
                view=self.error_view(
                    "You haven't connected a Last.fm account.\n\n"
                    f"Use `{PREFIX}lf set <username>` first."
                )
            )

        try:
            items, total_pages, total = await self.get_chart(mode, username, 1)
            title, body = self.format_chart(mode, username, items, 1, total_pages, total)

            view = PaginationView(
                self, ctx, mode, username, 1, total_pages, total, title, body
            )
            message = await ctx.send(view=view)
            view.message = message
        except Exception as exc:
            logger.warning(f"[LASTFM] Chart error: {exc}")
            await ctx.send(view=self.error_view("Last.fm couldn't load that chart."))

    # `lf topartists` already exists on the main bot, so only its track and
    # album charts are added here.
    @lf_sub(name="toptracks", aliases=("tt", "ttracks"))
    async def lf_toptracks(self, ctx: commands.Context) -> None:
        """Your all-time top tracks."""
        await self.chart_command(ctx, "tracks")

    @lf_sub(name="topalbums", aliases=("tal", "talbums"))
    async def lf_topalbums(self, ctx: commands.Context) -> None:
        """Your all-time top albums."""
        await self.chart_command(ctx, "albums")

    @lf_sub(name="recent")
    async def lf_recent(self, ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        target, username = await self.resolve_target(ctx, member)
        if not username:
            err = "Use `!lf set <username>` first." if target == ctx.author else f"{target.display_name} hasn't connected Last.fm."
            return await ctx.send(view=self.error_view(err))

        try:
            data = await self.api("user.getRecentTracks", user=username, limit=10)
            tracks = data.get("recenttracks", {}).get("track", [])
            if isinstance(tracks, dict):
                tracks = [tracks]

            lines = []
            for index, track in enumerate(tracks, 1):
                name = text(track.get("name"))
                artist_data = track.get("artist", {})
                artist = text(
                    artist_data.get("#text") or artist_data.get("name")
                    if isinstance(artist_data, dict)
                    else artist_data
                )
                now = text(track.get("@attr", {}).get("nowplaying")).lower() == "true"
                lines.append(
                    entry_line(
                        index,
                        name,
                        artist=artist,
                        url=text(track.get("url")),
                        marker="▶ " if now else "",
                    )
                )

            await ctx.send(
                view=LastFMView(
                    title=f"Recent tracks for {username}",
                    sections=["\n".join(lines) if lines else "No tracks found."],
                )
            )
        except Exception as exc:
            logger.warning(f"[LASTFM] Recent error: {exc}")
            await ctx.send(view=self.error_view("Couldn't load recent tracks."))

    @lf_sub(name="profile")
    async def lf_profile(self, ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        target, username = await self.resolve_target(ctx, member)
        if not username:
            err = "Use `!lf set <username>` first." if target == ctx.author else f"{target.display_name} hasn't connected Last.fm."
            return await ctx.send(view=self.error_view(err))

        try:
            data = await self.api("user.getInfo", user=username)
            user = data.get("user", {})
            real_name = text(user.get("realname"))
            country = text(user.get("country"))
            registered = text(user.get("registered", {}).get("unixtime")) if isinstance(user.get("registered"), dict) else ""
            scrobbles = format_number(user.get("playcount"))
            url = text(user.get("url"))
            artwork = image_from_list(user.get("image"))

            lines = [f"**{username}**"]
            stats = join_stats(
                ("Scrobbles", scrobbles), ("Name", real_name), ("Country", country)
            )
            if stats:
                lines.append(stats)
            if registered:
                lines.append(f"**Registered:** <t:{registered}:D>")
            if url:
                lines.append(f"[Open last.fm profile]({url})")

            await ctx.send(
                view=LastFMView(
                    title="Last.fm Profile", sections=["\n".join(lines)], thumbnail=artwork
                )
            )
        except Exception as exc:
            logger.warning(f"[LASTFM] Profile error: {exc}")
            await ctx.send(view=self.error_view("Couldn't load Last.fm profile."))

    @lf_sub(name="artist")
    async def lf_artist(self, ctx: commands.Context, *, artist: str = "") -> None:
        if not artist:
            return await ctx.send(view=self.error_view("Usage: `!lf artist <artist>`"))

        try:
            data = await self.api("artist.getInfo", artist=artist)
            info = data.get("artist", {})
            name = text(info.get("name")) or artist
            stats = info.get("stats", {})
            listeners = format_number(stats.get("listeners"))
            plays = format_number(stats.get("playcount"))
            bio = text(info.get("bio", {}).get("summary")) if isinstance(info.get("bio"), dict) else ""
            artwork = image_from_list(info.get("image"))

            lines = [
                f"**{shorten(name)}**",
                join_stats(("Listeners", listeners), ("Playcount", plays)),
            ]
            if bio:
                bio = bio.split("<a href=")[0].strip()
                lines.append(shorten(bio, 600))

            await ctx.send(
                view=LastFMView(
                    title=f"Artist \u2014 {shorten(name)}", sections=["\n".join(lines)], thumbnail=artwork
                )
            )
        except Exception as exc:
            logger.warning(f"[LASTFM] Artist error: {exc}")
            await ctx.send(view=self.error_view(f"Couldn't find artist **{artist}**."))

    @lf_sub(name="album")
    async def lf_album(self, ctx: commands.Context, *, query: str = "") -> None:
        if not query:
            return await ctx.send(view=self.error_view("Usage: `!lf album <album> | <artist>`"))

        album, artist = parse_query_and_artist(query)
        try:
            data = await self.api("album.getInfo", album=album, artist=artist)
            info = data.get("album", {})
            name = text(info.get("name")) or album
            actual_artist = text(info.get("artist")) or artist
            listeners = format_number(info.get("listeners"))
            plays = format_number(info.get("playcount"))
            artwork = image_from_list(info.get("image"))
            url = text(info.get("url"))

            lines = [
                f"**{shorten(name)}**",
                f"**{shorten(actual_artist) if actual_artist else 'Unknown'}**",
                join_stats(("Listeners", listeners), ("Playcount", plays)),
            ]
            if url:
                lines.append(f"[Open album]({url})")

            await ctx.send(
                view=LastFMView(
                    title=f"Album \u2014 {shorten(name)}", sections=["\n".join(lines)], thumbnail=artwork
                )
            )
        except Exception as exc:
            logger.warning(f"[LASTFM] Album error: {exc}")
            await ctx.send(view=self.error_view("Couldn't find that album."))

    @lf_sub(name="track")
    async def lf_track(self, ctx: commands.Context, *, query: str = "") -> None:
        if not query:
            return await ctx.send(view=self.error_view("Usage: `!lf track <track> | <artist>`"))

        track, artist = parse_query_and_artist(query)
        try:
            data = await self.api("track.getInfo", track=track, artist=artist)
            info = data.get("track", {})
            name = text(info.get("name")) or track
            actual_artist = text(
                info.get("artist", {}).get("name")
                if isinstance(info.get("artist"), dict)
                else info.get("artist") or artist
            )
            listeners = format_number(info.get("listeners"))
            plays = format_number(info.get("playcount"))
            album_data = info.get("album", {})
            album = text(album_data.get("title")) if isinstance(album_data, dict) else ""
            artwork = image_from_list(album_data.get("image")) if isinstance(album_data, dict) else None
            url = text(info.get("url"))

            lines = [
                track_label(name, actual_artist or "Unknown", url),
                join_stats(("Album", shorten(album) if album else ""), ("Listeners", listeners), ("Playcount", plays)),
            ]

            await ctx.send(
                view=LastFMView(
                    title=f"Track \u2014 {shorten(name)}", sections=["\n".join(lines)], thumbnail=artwork
                )
            )
        except Exception as exc:
            logger.warning(f"[LASTFM] Track error: {exc}")
            await ctx.send(view=self.error_view("Couldn't find that track."))

    @lf_sub(name="loved")
    async def lf_loved(self, ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        target, username = await self.resolve_target(ctx, member)
        if not username:
            err = "Use `!lf set <username>` first." if target == ctx.author else f"{target.display_name} hasn't connected Last.fm."
            return await ctx.send(view=self.error_view(err))

        try:
            data = await self.api("user.getLovedTracks", user=username, limit=10)
            tracks = data.get("lovedtracks", {}).get("track", [])
            if isinstance(tracks, dict):
                tracks = [tracks]

            lines = []
            for index, item in enumerate(tracks, 1):
                name = text(item.get("name"))
                artist_data = item.get("artist", {})
                artist = text(artist_data.get("name") if isinstance(artist_data, dict) else artist_data)
                lines.append(entry_line(index, name, artist=artist, url=text(item.get("url"))))

            await ctx.send(
                view=LastFMView(
                    title=f"Loved tracks for {username}",
                    sections=["\n".join(lines) if lines else "No loved tracks found."],
                )
            )
        except Exception as exc:
            logger.warning(f"[LASTFM] Loved error: {exc}")
            await ctx.send(view=self.error_view("Couldn't load loved tracks."))

    @lf_sub(name="tags")
    async def lf_tags(self, ctx: commands.Context, *, artist: str = "") -> None:
        if not artist:
            return await ctx.send(view=self.error_view("Usage: `!lf tags <artist>`"))

        try:
            data = await self.api("artist.getTopTags", artist=artist)
            tags = data.get("toptags", {}).get("tag", [])
            if isinstance(tags, dict):
                tags = [tags]

            names = [text(t.get("name")) for t in tags if text(t.get("name"))]
            await ctx.send(
                view=LastFMView(
                    title=f"{artist} — Tags",
                    sections=[", ".join(f"`{n}`" for n in names[:20]) if names else "No tags found."],
                )
            )
        except Exception as exc:
            logger.warning(f"[LASTFM] Tags error: {exc}")
            await ctx.send(view=self.error_view("Couldn't load artist tags."))

    @lf_sub(name="similar")
    async def lf_similar(self, ctx: commands.Context, *, artist: str = "") -> None:
        if not artist:
            return await ctx.send(view=self.error_view("Usage: `!lf similar <artist>`"))

        try:
            data = await self.api("artist.getSimilar", artist=artist, limit=10)
            artists = data.get("similarartists", {}).get("artist", [])
            if isinstance(artists, dict):
                artists = [artists]

            lines = [
                entry_line(index, text(item.get("name")), url=text(item.get("url")))
                for index, item in enumerate(artists, 1)
                if text(item.get("name"))
            ]

            await ctx.send(
                view=LastFMView(
                    title=f"Artists similar to {shorten(artist)}",
                    sections=["\n".join(lines) if lines else "No similar artists found."],
                )
            )
        except Exception as exc:
            logger.warning(f"[LASTFM] Similar error: {exc}")
            await ctx.send(view=self.error_view("Couldn't find similar artists."))

    @lf_sub(name="compare")
    async def lf_compare(self, ctx: commands.Context, user: str = "") -> None:
        mine = await self.username_for(ctx.author.id)
        if not mine:
            return await ctx.send(view=self.error_view("Use `!lf set <username>` first."))

        if not user:
            return await ctx.send(view=self.error_view("Usage: `!lf compare <@user|username>`"))

        # Resolve mention or username
        if ctx.message.mentions:
            target_user = ctx.message.mentions[0]
            target_username = await self.username_for(target_user.id)
            if not target_username:
                return await ctx.send(
                    view=self.error_view(f"{target_user.display_name} hasn't connected a Last.fm account.")
                )
        else:
            target_username = user.strip()

        try:
            first = await self.api("user.getTopArtists", user=mine, period="overall", limit=50, page=1)
            second = await self.api("user.getTopArtists", user=target_username, period="overall", limit=50, page=1)

            first_artists = first.get("topartists", {}).get("artist", [])
            second_artists = second.get("topartists", {}).get("artist", [])

            first_names = {text(x.get("name")).lower() for x in (first_artists if isinstance(first_artists, list) else [first_artists])}
            second_names = {text(x.get("name")).lower() for x in (second_artists if isinstance(second_artists, list) else [second_artists])}

            common = first_names & second_names
            lines = [
                entry_line(index, name.title())
                for index, name in enumerate(sorted(common)[:15], 1)
            ]

            await ctx.send(
                view=LastFMView(
                    title="Taste comparison",
                    sections=[
                        f"**{mine}** vs **{target_username}**",
                        join_stats(
                            (mine, f"{len(first_names)} artists"),
                            (target_username, f"{len(second_names)} artists"),
                        ),
                        f"**{len(common)}** artist(s) in common"
                        + ("\n" + "\n".join(lines) if lines else ""),
                    ],
                )
            )
        except Exception as exc:
            logger.warning(f"[LASTFM] Compare error: {exc}")
            await ctx.send(view=self.error_view("Couldn't compare those Last.fm accounts."))

    @lf_sub(name="charts")
    async def lf_charts(self, ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        target, username = await self.resolve_target(ctx, member)
        if not username:
            err = "Use `!lf set <username>` first." if target == ctx.author else f"{target.display_name} hasn't connected Last.fm."
            return await ctx.send(view=self.error_view(err))

        try:
            artists, _p, _t = await self.get_chart("artists", username, 1)
            tracks, _p, _t = await self.get_chart("tracks", username, 1)
            albums, _p, _t = await self.get_chart("albums", username, 1)

            artist_lines = [
                entry_line(index, text(item.get("name")), url=text(item.get("url")))
                for index, item in enumerate(artists[:5], 1)
            ]
            track_lines = []
            for index, item in enumerate(tracks[:5], 1):
                adata = item.get("artist", {})
                art = text(adata.get("name") if isinstance(adata, dict) else adata)
                track_lines.append(
                    entry_line(
                        index,
                        text(item.get("name")),
                        artist=art,
                        url=text(item.get("url")),
                        suffix=plays_suffix(item.get("playcount")),
                    )
                )
            album_lines = [
                entry_line(
                    index,
                    text(item.get("name")),
                    url=text(item.get("url")),
                    suffix=plays_suffix(item.get("playcount")),
                )
                for index, item in enumerate(albums[:5], 1)
            ]

            await ctx.send(
                view=LastFMView(
                    title=f"{username}'s charts",
                    sections=[
                        "**Top artists**\n" + ("\n".join(artist_lines) if artist_lines else "None"),
                        "**Top tracks**\n" + ("\n".join(track_lines) if track_lines else "None"),
                        "**Top albums**\n" + ("\n".join(album_lines) if album_lines else "None"),
                    ],
                )
            )
        except Exception as exc:
            logger.warning(f"[LASTFM] Charts error: {exc}")
            await ctx.send(view=self.error_view("Couldn't load charts."))

    @lf_sub(name="streak")
    async def lf_streak(self, ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        target, username = await self.resolve_target(ctx, member)
        if not username:
            err = "Use `!lf set <username>` first." if target == ctx.author else f"{target.display_name} hasn't connected Last.fm."
            return await ctx.send(view=self.error_view(err))

        try:
            data = await self.api("user.getRecentTracks", user=username, limit=200)
            tracks = data.get("recenttracks", {}).get("track", [])
            if isinstance(tracks, dict):
                tracks = [tracks]

            days = set()
            for tr in tracks:
                if text(tr.get("@attr", {}).get("nowplaying")).lower() == "true":
                    continue
                date_uts = tr.get("date", {}).get("uts") if isinstance(tr.get("date"), dict) else None
                if date_uts:
                    try:
                        days.add(datetime.fromtimestamp(int(date_uts), tz=timezone.utc).date())
                    except Exception:
                        pass

            today = datetime.now(timezone.utc).date()
            yesterday = today - timedelta(days=1)

            current = today if today in days else (yesterday if yesterday in days else None)
            streak = 0
            while current and current in days:
                streak += 1
                current -= timedelta(days=1)

            await ctx.send(
                view=LastFMView(
                    title="Listening streak",
                    sections=[
                        f"**{username}** · 🔥 **{streak} day{'s' if streak != 1 else ''}**",
                        "-# Consecutive daily scrobbles.",
                    ],
                )
            )
        except Exception as exc:
            logger.warning(f"[LASTFM] Streak error: {exc}")
            await ctx.send(view=self.error_view("Couldn't calculate listening streak."))

    @lf_sub(name="cc")
    async def lf_cc(self, ctx: commands.Context, command_name: str = "") -> None:
        command_name = command_name.strip().lower().lstrip(",")
        if not command_name:
            return await ctx.send(
                view=self.error_view("Usage: `!lf cc <command>`\n\nReset with `!lf cc reset`.")
            )

        user_id = str(ctx.author.id)
        account = self.data.get(user_id, {})

        if command_name == "reset":
            account.pop("custom_command", None)
            self.data[user_id] = account
            save_data(self.data)
            return await ctx.send(
                view=LastFMView(title="Last.fm", sections=["Your custom now-playing command has been reset."])
            )

        if not command_name.isalnum():
            return await ctx.send(
                view=self.error_view("Custom commands can only contain letters and numbers.")
            )

        if command_name in {"lf", "np", "fm", "help"}:
            return await ctx.send(view=self.error_view(f"`!{command_name}` is already reserved."))

        account["custom_command"] = command_name
        self.data[user_id] = account
        save_data(self.data)

        await ctx.send(
            view=LastFMView(
                title="Last.fm",
                sections=[f"Your custom now-playing command is now `!{command_name}`."],
            )
        )

    # ========================================================
    # SHARED HELPERS
    # ========================================================

    async def _account(
        self, ctx: commands.Context, member: Optional[discord.Member] = None
    ) -> tuple[Optional[Any], Optional[str]]:
        """Resolve an account, or post the "connect first" card and stop."""
        target, username = await self.resolve_target(ctx, member)
        if username:
            return target, username
        err = (
            "You haven't connected a Last.fm account.\n\nUse `!lf set <username>` first."
            if target == ctx.author
            else f"{target.display_name} hasn't connected a Last.fm account."
        )
        await ctx.send(view=self.error_view(err))
        return None, None

    async def _now_playing(self, username: str) -> tuple[str, str]:
        """The track a user scrobbled most recently, as ``(title, artist)``."""
        try:
            data = await self.api("user.getRecentTracks", user=username, limit=1)
        except Exception:
            return "", ""
        entries = data.get("recenttracks", {}).get("track", [])
        if isinstance(entries, dict):
            entries = [entries]
        if not entries:
            return "", ""
        first = entries[0]
        artist_data = first.get("artist", {})
        artist = text(
            artist_data.get("#text") or artist_data.get("name")
            if isinstance(artist_data, dict)
            else artist_data
        )
        return text(first.get("name")), artist

    async def _artist_tags(self, artist: str, limit: int = 10) -> list[str]:
        try:
            data = await self.api("artist.getTopTags", artist=artist)
        except Exception:
            return []
        tags = data.get("toptags", {}).get("tag", [])
        if isinstance(tags, dict):
            tags = [tags]
        return [text(tag.get("name")) for tag in tags if text(tag.get("name"))][:limit]

    async def _top_artist_names(self, username: str, limit: int = 100) -> set[str]:
        """The lowercased names of a user's top artists, for comparison."""
        try:
            data = await self.api(
                "user.getTopArtists", user=username, period="overall", limit=limit
            )
        except Exception:
            return set()
        artists = data.get("topartists", {}).get("artist", [])
        if isinstance(artists, dict):
            artists = [artists]
        return {text(a.get("name")).lower() for a in artists if text(a.get("name"))}

    async def _artist_top_tracks(
        self, username: str, artist: str, limit: int = 10
    ) -> list[tuple[str, int, str]]:
        """A user's most played tracks for one artist, as ``(name, plays, url)``.

        ``user.getArtistTracks`` is a scrobble log rather than a chart, so the
        plays are counted here and sorted, which is what the command promises.
        """
        try:
            data = await self.api(
                "user.getArtistTracks", user=username, artist=artist, limit=200
            )
        except Exception:
            return []
        entries = data.get("artisttracks", {}).get("track", [])
        if isinstance(entries, dict):
            entries = [entries]
        counts: dict[str, tuple[str, int, str]] = {}
        for entry in entries:
            name = text(entry.get("name"))
            if not name:
                continue
            key = name.lower()
            link = text(entry.get("url"))
            display, plays, url = counts.get(key, (name, 0, link))
            counts[key] = (display, plays + 1, url or link)
        ranked = sorted(counts.values(), key=lambda row: row[1], reverse=True)
        return ranked[:limit]

    async def _artist_albums(
        self, username: str, artist: str, limit: int = 10
    ) -> list[dict]:
        """A user's top albums that belong to one artist."""
        try:
            data = await self.api(
                "user.getTopAlbums", user=username, period="overall", limit=200
            )
        except Exception:
            return []
        albums = data.get("topalbums", {}).get("album", [])
        if isinstance(albums, dict):
            albums = [albums]
        wanted = artist.strip().lower()
        matched = []
        for item in albums:
            raw = item.get("artist", {})
            name = text(raw.get("name") if isinstance(raw, dict) else raw)
            if name.lower() == wanted:
                matched.append(item)
        return matched[:limit]

    async def _linked_members(
        self, guild: Optional[discord.Guild]
    ) -> list[tuple[discord.Member, str]]:
        """Members of this guild with a linked Last.fm account, capped."""
        if guild is None:
            return []
        found: list[tuple[discord.Member, str]] = []
        for member in guild.members:
            if member.bot:
                continue
            username = await self.username_for(member.id)
            if username:
                found.append((member, username))
                if len(found) >= SERVER_MEMBER_LIMIT:
                    break
        return found

    async def _member_chart(
        self, username: str, mode: str, limit: int = 50
    ) -> list[str]:
        """One member's top names for a server-wide ranking."""
        try:
            if mode == "artists":
                data = await self.api(
                    "user.getTopArtists", user=username, period="overall", limit=limit
                )
                items = data.get("topartists", {}).get("artist", [])
            elif mode == "albums":
                data = await self.api(
                    "user.getTopAlbums", user=username, period="overall", limit=limit
                )
                items = data.get("topalbums", {}).get("album", [])
            elif mode == "genres":
                data = await self.api("user.getTopTags", user=username, limit=limit)
                items = data.get("toptags", {}).get("tag", [])
            else:
                data = await self.api(
                    "user.getTopTracks", user=username, period="overall", limit=limit
                )
                items = data.get("toptracks", {}).get("track", [])
        except Exception:
            return []
        if isinstance(items, dict):
            items = [items]
        return [text(item.get("name")) for item in items if text(item.get("name"))]

    async def _server_names(
        self, guild: Optional[discord.Guild], mode: str, limit: int = 10
    ) -> tuple[list[tuple[str, int]], int]:
        """The most charted names across this guild's linked accounts.

        Every linked member's top 50 is fetched once, in small parallel bursts,
        and counted by how many members charted it. The API never exposes other
        people's playcounts, so this ranks by how widely shared a name is.
        """
        members = await self._linked_members(guild)
        if not members:
            return [], 0

        gate = asyncio.Semaphore(4)

        async def fetch(username: str) -> list[str]:
            async with gate:
                return await self._member_chart(username, mode)

        results = await asyncio.gather(*(fetch(username) for _m, username in members))
        counts: dict[str, int] = {}
        for names in results:
            for name in names:
                counts[name] = counts.get(name, 0) + 1
        ranked = sorted(counts.items(), key=lambda pair: pair[1], reverse=True)
        return ranked[:limit], len(members)

    def _server_view(
        self, entries: list[tuple[str, int]], members: int, noun: str
    ) -> LastFMView:
        lines = [
            entry_line(
                index,
                name,
                suffix=f"{count} member" + ("" if count == 1 else "s"),
            )
            for index, (name, count) in enumerate(entries, 1)
        ]
        return LastFMView(
            title=f"Server {noun}",
            sections=[
                f"**{members}** linked account(s) in this server",
                "\n".join(lines) if lines else "No listening data found.",
            ],
        )

    # ========================================================
    # CHARTS & GENRES
    # ========================================================

    @lf_sub(name="topgenres", aliases=("tg", "genres"))
    async def lf_topgenres(self, ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        """Your most scrobbled genres, from your top tags."""
        _target, username = await self._account(ctx, member)
        if not username:
            return
        try:
            data = await self.api("user.getTopTags", user=username, limit=20)
            tags = data.get("toptags", {}).get("tag", [])
            if isinstance(tags, dict):
                tags = [tags]
            lines = [
                entry_line(
                    index,
                    text(tag.get("name")),
                    url=text(tag.get("url")),
                    suffix=f"{format_number(tag.get('count'))} scrobbles",
                )
                for index, tag in enumerate(tags, 1)
                if text(tag.get("name"))
            ]
            await ctx.send(
                view=LastFMView(
                    title=f"Top genres for {username}",
                    sections=["\n".join(lines) if lines else "No genre data found."],
                )
            )
        except Exception as exc:
            logger.warning(f"[LASTFM] Top genres error: {exc}")
            await ctx.send(view=self.error_view("Couldn't load top genres."))

    # ========================================================
    # PER-ARTIST
    # ========================================================

    @lf_sub(name="artisttracks", aliases=("at",))
    async def lf_artisttracks(self, ctx: commands.Context, *, artist: str = "") -> None:
        """Your top tracks for an artist (their most played, by your scrobbles).

        Leave the artist out to use whatever you are playing right now.
        """
        _target, username = await self._account(ctx)
        if not username:
            return
        artist = artist.strip()
        if not artist:
            _title, artist = await self._now_playing(username)
        if not artist:
            return await ctx.send(
                view=self.error_view("Usage: `!lf artisttracks <artist>`")
            )
        try:
            ranked = await self._artist_top_tracks(username, artist, limit=10)
            lines = [
                entry_line(
                    index, name, artist=artist, url=url, suffix=plays_suffix(plays)
                )
                for index, (name, plays, url) in enumerate(ranked, 1)
            ]
            await ctx.send(
                view=LastFMView(
                    title=f"Top {shorten(artist)} tracks for {username}",
                    sections=[
                        "\n".join(lines)
                        if lines
                        else f"No scrobbles of **{artist}** found.",
                    ],
                )
            )
        except Exception as exc:
            logger.warning(f"[LASTFM] Artist tracks error: {exc}")
            await ctx.send(view=self.error_view("Couldn't load that artist's tracks."))

    @lf_sub(name="artistalbums", aliases=("aa",))
    async def lf_artistalbums(self, ctx: commands.Context, *, artist: str = "") -> None:
        """Your top albums for an artist."""
        _target, username = await self._account(ctx)
        if not username:
            return
        artist = artist.strip()
        if not artist:
            _title, artist = await self._now_playing(username)
        if not artist:
            return await ctx.send(view=self.error_view("Usage: `!lf artistalbums <artist>`"))
        try:
            albums = await self._artist_albums(username, artist, limit=10)
            lines = [
                entry_line(
                    index,
                    text(item.get("name")),
                    url=text(item.get("url")),
                    suffix=plays_suffix(item.get("playcount")),
                )
                for index, item in enumerate(albums, 1)
            ]
            await ctx.send(
                view=LastFMView(
                    title=f"Top {shorten(artist)} albums for {username}",
                    sections=[
                        "\n".join(lines) if lines else f"No albums by **{artist}** found.",
                    ],
                )
            )
        except Exception as exc:
            logger.warning(f"[LASTFM] Artist albums error: {exc}")
            await ctx.send(view=self.error_view("Couldn't load that artist's albums."))

    @lf_sub(name="artistoverview", aliases=("ao",))
    async def lf_artistoverview(self, ctx: commands.Context, *, artist: str = "") -> None:
        """A one-card overview of an artist: stats, your plays and top tracks."""
        _target, username = await self._account(ctx)
        if not username:
            return
        artist = artist.strip()
        if not artist:
            _title, artist = await self._now_playing(username)
        if not artist:
            return await ctx.send(view=self.error_view("Usage: `!lf artistoverview <artist>`"))
        try:
            data = await self.api("artist.getInfo", artist=artist, username=username)
            info = data.get("artist", {})
            name = text(info.get("name")) or artist
            stats = info.get("stats", {})
            artwork = image_from_list(info.get("image"))
            ranked = await self._artist_top_tracks(username, artist, limit=5)
            tags = await self._artist_tags(artist, limit=8)

            track_lines = [
                entry_line(index, track, artist=name, url=url, suffix=plays_suffix(plays))
                for index, (track, plays, url) in enumerate(ranked, 1)
            ]
            sections = [
                f"**{shorten(name)}**",
                join_stats(
                    ("Your plays", format_number(stats.get("userplaycount"))),
                    ("Listeners", format_number(stats.get("listeners"))),
                    ("Global plays", format_number(stats.get("playcount"))),
                ),
                "**Your top tracks**\n"
                + ("\n".join(track_lines) if track_lines else "None"),
            ]
            if tags:
                sections.append("**Tags**\n" + " · ".join(f"`{tag}`" for tag in tags))
            await ctx.send(
                view=LastFMView(title=f"Artist \u2014 {shorten(name)}", sections=sections, thumbnail=artwork)
            )
        except Exception as exc:
            logger.warning(f"[LASTFM] Artist overview error: {exc}")
            await ctx.send(view=self.error_view(f"Couldn't find artist **{artist}**."))

    # ========================================================
    # PLAYCOUNTS
    # ========================================================

    async def _playcount_line(
        self, ctx: commands.Context, kind: str, query: str
    ) -> None:
        _target, username = await self._account(ctx)
        if not username:
            return
        query = query.strip()

        # An artist name is taken verbatim — splitting it on spaces the way the
        # ``track | artist`` parser does would chop a name like "Juice WRLD".
        if kind == "artist":
            artist = query
            if not artist:
                _title, artist = await self._now_playing(username)
            if not artist:
                return await ctx.send(view=self.error_view("Usage: `!lf artistplays <artist>`"))
            try:
                data = await self.api("artist.getInfo", artist=artist, username=username)
                info = data.get("artist", {})
                name = text(info.get("name")) or artist
                plays = format_number(info.get("stats", {}).get("userplaycount"))
                await ctx.send(
                    view=LastFMView(
                        title=f"Artist \u2014 {shorten(name)}",
                        sections=[f"**Your plays:** {plays}"],
                    )
                )
            except Exception as exc:
                logger.warning(f"[LASTFM] Artist plays error: {exc}")
                await ctx.send(view=self.error_view(f"Couldn't look up **{artist}**'s plays."))
            return

        name_part, artist_part = parse_query_and_artist(query) if query else ("", "")
        if not name_part or not artist_part:
            now_title, now_artist = await self._now_playing(username)
            name_part = name_part or now_title
            artist_part = artist_part or now_artist
        if not name_part or not artist_part:
            return await ctx.send(
                view=self.error_view(f"Usage: `!lf {kind}plays <{kind}> | <artist>`")
            )

        method = "track.getInfo" if kind == "track" else "album.getInfo"
        try:
            data = await self.api(
                method, username=username, **{kind: name_part, "artist": artist_part}
            )
            info = data.get(kind, {})
            name = text(info.get("name")) or name_part
            raw_artist = info.get("artist", {})
            actual_artist = text(
                raw_artist.get("name") if isinstance(raw_artist, dict) else raw_artist
            ) or artist_part
            plays = format_number(info.get("stats", {}).get("userplaycount"))

            if kind == "track":
                album_data = info.get("album", {})
                album = text(album_data.get("title")) if isinstance(album_data, dict) else ""
                lines = [
                    track_label(name, actual_artist, text(info.get("url"))),
                    join_stats(("Album", shorten(album) if album else ""), ("Your plays", plays)),
                ]
            else:
                lines = [
                    f"**{shorten(name)}**",
                    f"**{shorten(actual_artist) if actual_artist else 'Unknown'}**",
                    join_stats(("Your plays", plays)),
                ]
            await ctx.send(
                view=LastFMView(
                    title=f"{kind.title()} \u2014 {shorten(name)}", sections=lines
                )
            )
        except Exception as exc:
            logger.warning(f"[LASTFM] {kind} plays error: {exc}")
            await ctx.send(view=self.error_view(f"Couldn't look up that {kind}'s plays."))

    @lf_sub(name="trackplays", aliases=("tp",))
    async def lf_trackplays(self, ctx: commands.Context, *, query: str = "") -> None:
        """How many times you've scrobbled a track."""
        await self._playcount_line(ctx, "track", query)

    @lf_sub(name="albumplays", aliases=("alp", "alplays"))
    async def lf_albumplays(self, ctx: commands.Context, *, query: str = "") -> None:
        """How many times you've scrobbled an album."""
        await self._playcount_line(ctx, "album", query)

    @lf_sub(name="artistplays", aliases=("ap", "arplays"))
    async def lf_artistplays(self, ctx: commands.Context, *, artist: str = "") -> None:
        """How many times you've scrobbled an artist."""
        await self._playcount_line(ctx, "artist", artist)

    # ========================================================
    # DETAILS
    # ========================================================

    @lf_sub(name="trackdetails", aliases=("td",))
    async def lf_trackdetails(self, ctx: commands.Context, *, query: str = "") -> None:
        """The metadata Last.fm holds for a track."""
        _target, username = await self._account(ctx)
        if not username:
            return
        track, artist = parse_query_and_artist(query) if query.strip() else ("", "")
        if not track or not artist:
            now_title, now_artist = await self._now_playing(username)
            track = track or now_title
            artist = artist or now_artist
        if not track or not artist:
            return await ctx.send(
                view=self.error_view("Usage: `!lf trackdetails <track> | <artist>`")
            )
        try:
            data = await self.api("track.getInfo", track=track, artist=artist, username=username)
            info = data.get("track", {})
            name = text(info.get("name")) or track
            album_data = info.get("album", {})
            album = text(album_data.get("title")) if isinstance(album_data, dict) else ""
            artwork = image_from_list(album_data.get("image")) if isinstance(album_data, dict) else None
            try:
                length = format_clock(int(info.get("duration") or 0) / 1000)
            except Exception:
                length = "0:00"
            tags = await self._artist_tags(artist, limit=6)
            url = text(info.get("url"))
            sections = [
                track_label(name, artist, url),
                join_stats(
                    ("Album", shorten(album) if album else "Unknown"),
                    ("Duration", length),
                ),
                join_stats(
                    ("Your plays", format_number(info.get("stats", {}).get("userplaycount"))),
                    ("Listeners", format_number(info.get("listeners"))),
                    ("Global plays", format_number(info.get("playcount"))),
                ),
            ]
            if tags:
                sections.append(" · ".join(f"`{tag}`" for tag in tags))
            await ctx.send(
                view=LastFMView(
                    title=f"Track \u2014 {shorten(name)}", sections=sections, thumbnail=artwork
                )
            )
        except Exception as exc:
            logger.warning(f"[LASTFM] Track details error: {exc}")
            await ctx.send(view=self.error_view("Couldn't find that track."))

    @lf_sub(name="cover", aliases=("cv",))
    async def lf_cover(self, ctx: commands.Context, *, query: str = "") -> None:
        """The cover art for an album, or for whatever you're playing."""
        _target, username = await self._account(ctx)
        if not username:
            return
        title = "Cover"
        artwork = None
        try:
            if query.strip():
                album, artist = parse_query_and_artist(query)
                data = await self.api("album.getInfo", album=album, artist=artist)
                info = data.get("album", {})
                title = text(info.get("name")) or album
                artwork = image_from_list(info.get("image"))
            else:
                track, artist = await self._now_playing(username)
                if not track:
                    return await ctx.send(view=self.error_view("Nothing is playing right now."))
                data = await self.api("track.getInfo", track=track, artist=artist, username=username)
                info = data.get("track", {})
                title = text(info.get("name")) or track
                album_data = info.get("album", {})
                artwork = image_from_list(album_data.get("image")) if isinstance(album_data, dict) else None
        except Exception as exc:
            logger.warning(f"[LASTFM] Cover error: {exc}")
            return await ctx.send(view=self.error_view("Couldn't find cover art for that."))

        if not artwork:
            return await ctx.send(view=self.error_view("No cover art available for that."))
        await ctx.send(
            view=LastFMView(title=f"Cover — {title}", sections=[f"**{title}**"], thumbnail=artwork)
        )

    @lf_sub(name="albumtracks", aliases=("alt",))
    async def lf_albumtracks(self, ctx: commands.Context, *, query: str = "") -> None:
        """The tracklist for an album, with each track's length."""
        if not query.strip():
            return await ctx.send(
                view=self.error_view("Usage: `!lf albumtracks <album> | <artist>`")
            )
        album, artist = parse_query_and_artist(query)
        try:
            data = await self.api("album.getInfo", album=album, artist=artist)
            info = data.get("album", {})
            name = text(info.get("name")) or album
            tracks = info.get("tracks", {}).get("track", [])
            if isinstance(tracks, dict):
                tracks = [tracks]
            lines = []
            for index, item in enumerate(tracks, 1):
                raw = str(item.get("duration") or "")
                suffix = (
                    format_clock(int(raw)) if raw.isdigit() and int(raw) > 0 else ""
                )
                lines.append(
                    entry_line(
                        index, text(item.get("name")), url=text(item.get("url")), suffix=suffix
                    )
                )
            await ctx.send(
                view=LastFMView(
                    title=f"Tracklist \u2014 {shorten(name)}",
                    sections=[
                        "\n".join(lines) if lines else "No tracks listed.",
                    ],
                    thumbnail=image_from_list(info.get("image")),
                )
            )
        except Exception as exc:
            logger.warning(f"[LASTFM] Album tracks error: {exc}")
            await ctx.send(view=self.error_view("Couldn't find that album."))

    @lf_sub(name="genre", aliases=("g",))
    async def lf_genre(self, ctx: commands.Context, *, genre: str = "") -> None:
        """What a genre is, and its biggest artists."""
        if not genre.strip():
            return await ctx.send(view=self.error_view("Usage: `!lf genre <genre>`"))
        try:
            info_data = await self.api("tag.getInfo", tag=genre)
            info = info_data.get("tag", {})
            name = text(info.get("name")) or genre
            wiki = info.get("wiki", {})
            summary = text(wiki.get("summary")) if isinstance(wiki, dict) else ""
            summary = summary.split("<a href=")[0].strip()

            top_data = await self.api("tag.getTopArtists", tag=genre, limit=10)
            artists = top_data.get("topartists", {}).get("artist", [])
            if isinstance(artists, dict):
                artists = [artists]
            lines = [
                entry_line(index, text(item.get("name")), url=text(item.get("url")))
                for index, item in enumerate(artists, 1)
                if text(item.get("name"))
            ]

            sections = [
                f"**{shorten(name)}**",
                join_stats(
                    ("Artists", format_number(info.get("total"))),
                    ("Reach", format_number(info.get("reach"))),
                ),
                "**Top artists**\n" + ("\n".join(lines) if lines else "None"),
            ]
            if summary:
                sections.append(shorten(summary, 600))
            await ctx.send(
                view=LastFMView(title=f"Genre \u2014 {shorten(name)}", sections=sections)
            )
        except Exception as exc:
            logger.warning(f"[LASTFM] Genre error: {exc}")
            await ctx.send(view=self.error_view(f"Couldn't find genre **{genre}**."))

    @lf_sub(name="overview", aliases=("ov",))
    async def lf_overview(self, ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        """A daily overview: what's playing and what you've played today."""
        target, username = await self._account(ctx, member)
        if not username:
            return
        start = int(
            datetime.now(timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        try:
            data = await self.api(
                "user.getRecentTracks", **{"user": username, "limit": 200, "from": start}
            )
            root = data.get("recenttracks", {})
            entries = root.get("track", [])
            if isinstance(entries, dict):
                entries = [entries]
            total = format_number(root.get("@attr", {}).get("total"))

            track_counts: dict[str, int] = {}
            artist_counts: dict[str, int] = {}
            for entry in entries:
                name = text(entry.get("name"))
                if name:
                    track_counts[name] = track_counts.get(name, 0) + 1
                raw = entry.get("artist", {})
                artist = text(raw.get("#text") or raw.get("name") if isinstance(raw, dict) else raw)
                if artist:
                    artist_counts[artist] = artist_counts.get(artist, 0) + 1

            top_track = max(track_counts.items(), key=lambda pair: pair[1]) if track_counts else None
            top_artist = max(artist_counts.items(), key=lambda pair: pair[1]) if artist_counts else None
            now_title, now_artist = await self._now_playing(username)

            sections = [
                f"**{target.display_name}** · **{username}**",
                f"**Today:** {total} scrobbles\n"
                f"**Top artist:** {shorten(top_artist[0]) if top_artist else '—'}\n"
                f"**Top track:** {shorten(top_track[0]) if top_track else '—'}",
            ]
            if now_title:
                sections.append("**Now playing**\n" + track_label(now_title, now_artist))
            await ctx.send(
                view=LastFMView(title="Daily overview", sections=sections)
            )
        except Exception as exc:
            logger.warning(f"[LASTFM] Overview error: {exc}")
            await ctx.send(view=self.error_view("Couldn't load your overview."))

    # `lf plays` already exists on the main bot (it counts the current song),
    # so this period-based scrobble count is added under its own alias instead.
    @lf_sub(name="scrobbles", aliases=("pl",))
    async def lf_plays(self, ctx: commands.Context, period: str = "overall") -> None:
        """Your total scrobble count for a time period.

        Periods: `overall`, `7day`, `1month`, `3month`, `6month`, `12month`
        (short forms like `7d` or `1m` work too).
        """
        _target, username = await self._account(ctx)
        if not username:
            return
        wanted = (period or "overall").strip().lower()
        shorthand = {
            "7d": "7day",
            "1m": "1month",
            "3m": "3month",
            "6m": "6month",
            "12m": "12month",
            "1y": "12month",
            "year": "12month",
            "month": "1month",
            "week": "7day",
            "all": "overall",
            "": "overall",
        }
        wanted = shorthand.get(wanted, wanted)
        try:
            if wanted == "overall":
                data = await self.api("user.getInfo", user=username)
                total = format_number(data.get("user", {}).get("playcount"))
                label = "All-time scrobbles"
            elif wanted in PLAY_PERIODS:
                days = PLAY_PERIODS[wanted]
                start = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
                data = await self.api(
                    "user.getRecentTracks", **{"user": username, "from": start, "limit": 1}
                )
                total = format_number(
                    data.get("recenttracks", {}).get("@attr", {}).get("total")
                )
                label = f"Scrobbles in the last {days} day(s)"
            else:
                return await ctx.send(
                    view=self.error_view(
                        "Unknown period. Try `overall`, `7day`, `1month`, `3month`, "
                        "`6month` or `12month`."
                    )
                )
            await ctx.send(
                view=LastFMView(
                    title="Scrobble count",
                    sections=[f"**{username}**", f"**{label}:** {total}"],
                )
            )
        except Exception as exc:
            logger.warning(f"[LASTFM] Plays error: {exc}")
            await ctx.send(view=self.error_view("Couldn't count your scrobbles."))

    # ========================================================
    # SERVER-WIDE
    # ========================================================

    @lf_sub(name="serverartists", aliases=("sa",))
    async def lf_serverartists(self, ctx: commands.Context) -> None:
        """The artists this server's linked members chart the most."""
        if ctx.guild is None:
            return await ctx.send(view=self.error_view("That only works inside a server."))
        entries, members = await self._server_names(ctx.guild, "artists")
        await ctx.send(view=self._server_view(entries, members, "Artists"))

    @lf_sub(name="servertracks", aliases=("st",))
    async def lf_servertracks(self, ctx: commands.Context) -> None:
        """The tracks this server's linked members chart the most."""
        if ctx.guild is None:
            return await ctx.send(view=self.error_view("That only works inside a server."))
        entries, members = await self._server_names(ctx.guild, "tracks")
        await ctx.send(view=self._server_view(entries, members, "Tracks"))

    @lf_sub(name="serveralbums", aliases=("sal",))
    async def lf_serveralbums(self, ctx: commands.Context) -> None:
        """The albums this server's linked members chart the most."""
        if ctx.guild is None:
            return await ctx.send(view=self.error_view("That only works inside a server."))
        entries, members = await self._server_names(ctx.guild, "albums")
        await ctx.send(view=self._server_view(entries, members, "Albums"))

    @lf_sub(name="servergenres", aliases=("sg",))
    async def lf_servergenres(self, ctx: commands.Context) -> None:
        """The genres this server's linked members chart the most."""
        if ctx.guild is None:
            return await ctx.send(view=self.error_view("That only works inside a server."))
        entries, members = await self._server_names(ctx.guild, "genres")
        await ctx.send(view=self._server_view(entries, members, "Genres"))

    @lf_sub(name="affinity", aliases=("aff",))
    async def lf_affinity(self, ctx: commands.Context) -> None:
        """The members in this server with the taste closest to yours."""
        _target, username = await self._account(ctx)
        if not username:
            return
        if ctx.guild is None:
            return await ctx.send(view=self.error_view("That only works inside a server."))

        mine = await self._top_artist_names(username, limit=100)
        if not mine:
            return await ctx.send(view=self.error_view("Couldn't read your top artists."))

        others = [
            (member, name)
            for member, name in await self._linked_members(ctx.guild)
            if member.id != ctx.author.id
        ]
        if not others:
            return await ctx.send(
                view=self.error_view("Nobody else here has connected a Last.fm account.")
            )

        gate = asyncio.Semaphore(4)

        async def score(pair: tuple[discord.Member, str]) -> tuple[discord.Member, int, float]:
            member, name = pair
            async with gate:
                theirs = await self._top_artist_names(name, limit=100)
            if not theirs:
                return member, 0, 0.0
            shared = len(mine & theirs)
            ratio = shared / max(1, min(len(mine), len(theirs)))
            return member, shared, ratio

        ranked = sorted(
            await asyncio.gather(*(score(pair) for pair in others)),
            key=lambda row: (row[2], row[1]),
            reverse=True,
        )
        lines = [
            entry_line(
                index,
                member.display_name,
                suffix=f"{shared} shared · {ratio * 100:.0f}%",
            )
            for index, (member, shared, ratio) in enumerate(ranked[:5], 1)
            if shared
        ]
        await ctx.send(
            view=LastFMView(
                title="Taste affinity",
                sections=[
                    f"**{ctx.author.display_name}** · {len(mine)} top artists",
                    "\n".join(lines)
                    if lines
                    else "No shared artists with anyone here yet.",
                ],
            )
        )

    # ========================================================
    # SCROBBLE COLLECTION
    # ========================================================

    @tasks.loop(minutes=SCROBBLE_POLL_MINUTES)
    async def scrobble_poller(self) -> None:
        await self.poll_scrobbles()

    @scrobble_poller.before_loop
    async def before_scrobble_poller(self) -> None:
        await self.bot.wait_until_ready()

    async def poll_scrobbles(self) -> None:
        """Pull new scrobbles for every linked account into the local store.

        Each member is asked for everything since their newest stored row, so
        repeat cycles are cheap and nothing is missed across a restart. The
        cycle is deliberately serial with a small pause: the Last.fm API rate
        limits bursts, and a missed cycle simply catches up on the next one.
        """
        accounts = (await self.linked_accounts())[:SCROBBLE_USER_LIMIT]

        stored = 0
        for user_id, username in accounts:
            try:
                since = await store.latest_played_at(self.bot.db, user_id)
                params: dict[str, Any] = {"user": username, "limit": SCROBBLE_BACKFILL}
                if since:
                    params["from"] = since + 1
                data = await self.api("user.getRecentTracks", **params)
                entries = data.get("recenttracks", {}).get("track", [])
                if isinstance(entries, dict):
                    entries = [entries]

                rows: list[tuple[Any, ...]] = []
                for entry in entries:
                    if text(entry.get("@attr", {}).get("nowplaying")).lower() == "true":
                        continue
                    date = entry.get("date", {})
                    uts = date.get("uts") if isinstance(date, dict) else None
                    if not uts:
                        continue
                    artist_data = entry.get("artist", {})
                    artist = text(
                        artist_data.get("#text") or artist_data.get("name")
                        if isinstance(artist_data, dict)
                        else artist_data
                    )
                    name = text(entry.get("name"))
                    if not artist or not name:
                        continue
                    album_data = entry.get("album", {})
                    album = text(album_data.get("#text")) if isinstance(album_data, dict) else ""
                    rows.append((user_id, int(uts), artist, name, album, store.SOURCE_LASTFM))

                stored += await store.record_many(self.bot.db, rows)
            except Exception as exc:
                logger.warning(f"[LASTFM] Scrobble poll failed for {username}: {exc}")
            await asyncio.sleep(0.3)

        if stored:
            logger.info(f"[LASTFM] Recorded {stored} new scrobble(s)")

    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member) -> None:
        """Capture listening presences for members Last.fm can't cover.

        Polling already records linked accounts, so a presence is only stored
        for an unlinked member — otherwise the same song would be counted twice
        under two different timestamps.
        """
        if getattr(after, "bot", False) or await self.username_for(after.id):
            return
        activity = listening_activity(after)
        if activity is None:
            return
        previous = listening_activity(before) if before is not None else None
        title, artist, album, _platform, _cover = describe_listening(activity)
        if not title or artist == "Unknown Artist":
            return
        if previous is not None:
            prev_title, prev_artist, *_rest = describe_listening(previous)
            if prev_title == title and prev_artist == artist:
                return
        try:
            await store.record_scrobble(
                self.bot.db,
                after.id,
                int(time.time()),
                artist,
                title,
                album,
                source=store.SOURCE_PRESENCE,
            )
        except Exception:
            logger.warning("[LASTFM] Could not record a presence scrobble")

    # ========================================================
    # LOCAL-STORE HELPERS
    # ========================================================

    async def _linked_ids(self, guild: Optional[discord.Guild]) -> list[int]:
        """Ids of this server's linked members."""
        if guild is None:
            return []
        found: list[int] = []
        for member in guild.members:
            if member.bot:
                continue
            if await self.username_for(member.id):
                found.append(member.id)
        return found[:500]

    async def _all_linked_ids(self) -> list[int]:
        """Every linked member the bot knows, across all its servers."""
        return [user_id for user_id, _name in await self.linked_accounts()][:2000]

    def _member_name(self, guild: Optional[discord.Guild], user_id: int) -> str:
        """A display name for a scrobble owner, wherever they were seen."""
        member = guild.get_member(user_id) if guild is not None else None
        if member is not None:
            return member.display_name
        user = self.bot.get_user(user_id)
        if user is not None:
            return user.name
        return f"user {user_id}"

    @staticmethod
    def _split_parts(query: str, count: int) -> list[str]:
        """``a | b | c`` into exactly ``count`` trimmed parts."""
        parts = [part.strip() for part in str(query or "").split("|")]
        return (parts + [""] * count)[:count]

    def _listen_block(self, guild: Optional[discord.Guild], rows: list[tuple[int, int]]) -> str:
        """A one-line-per-member ranking: ``1. Name - 42 plays``."""
        lines = [
            entry_line(index, self._member_name(guild, user_id), suffix=plays_suffix(plays))
            for index, (user_id, plays) in enumerate(rows, 1)
        ]
        return "\n".join(lines)

    # ========================================================
    # CROWNS & WHO KNOWS (local scrobble store)
    # ========================================================

    @lf_sub(name="crowns", aliases=("cr",))
    async def lf_crowns(self, ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        """The artists you hold the server crown for."""
        if ctx.guild is None:
            return await ctx.send(view=self.error_view("That only works inside a server."))
        target, username = await self._account(ctx, member)
        if not username:
            return
        rows = await store.crowns_for_member(
            self.bot.db, await self._linked_ids(ctx.guild), target.id, limit=10
        )
        lines = [
            entry_line(
                index,
                name,
                suffix=plays_suffix(plays) + (f" · next {runner:,}" if runner else ""),
            )
            for index, (name, _key, plays, runner) in enumerate(rows, 1)
        ]
        await ctx.send(
            view=LastFMView(
                title=f"{target.display_name}'s crowns",
                sections=[
                    f"**{len(rows)}** artist crown(s) in this server",
                    "\n".join(lines) if lines else "No crowns yet - scrobble more and check back.",
                ],
            )
        )

    @lf_sub(name="crown")
    async def lf_crown(self, ctx: commands.Context, *, artist: str = "") -> None:
        """Who holds the server crown for an artist, and can you steal it."""
        if ctx.guild is None:
            return await ctx.send(view=self.error_view("That only works inside a server."))
        if not artist.strip():
            return await ctx.send(view=self.error_view("Usage: `!lf crown <artist>`"))
        rows = await store.artist_plays_by_user(
            self.bot.db, await self._linked_ids(ctx.guild), artist
        )
        if not rows:
            return await ctx.send(
                view=self.error_view(f"Nobody in this server has scrobbled **{artist}** yet.")
            )
        holder_id, holder_plays = rows[0]
        mine = next((plays for uid, plays in rows if uid == ctx.author.id), 0)
        if holder_id == ctx.author.id:
            status = "You hold this crown."
        elif mine:
            status = f"You need **{holder_plays - mine + 1}** more to take it."
        else:
            status = "You haven't scrobbled them yet."
        await ctx.send(
            view=LastFMView(
                title=f"Crown \u2014 {shorten(artist)}",
                sections=[
                    f"**{self._member_name(ctx.guild, holder_id)}** holds it with "
                    f"{plays_suffix(holder_plays)}",
                    self._listen_block(ctx.guild, rows[:10]),
                    status,
                ],
            )
        )

    @lf_sub(name="trackcrowns", aliases=("tcrowns", "trackcrown"))
    async def lf_trackcrowns(self, ctx: commands.Context, mode: str = "") -> None:
        """Who holds the most track crowns in this server.

        A crown is the most plays for a track in this server. Pass `all` to also
        count tracks that only one member has scrobbled.
        """
        if ctx.guild is None:
            return await ctx.send(view=self.error_view("That only works inside a server."))
        solo = mode.strip().lower() in {"all", "solo", "everyone"}
        ranked, crowned, distinct = await store.track_crowns_by_user(
            self.bot.db, await self._linked_ids(ctx.guild), min_contenders=1 if solo else 2
        )
        if not ranked:
            return await ctx.send(
                view=self.error_view(
                    "No track crowns recorded yet - the poller needs a few cycles to fill the store."
                )
            )

        lines = [
            entry_line(
                index,
                self._member_name(ctx.guild, user_id),
                suffix=f"{crowns:,} crown" + ("" if crowns == 1 else "s"),
            )
            for index, (user_id, crowns) in enumerate(ranked[:10], 1)
        ]
        my_rank = next(
            (
                index
                for index, (user_id, _crowns) in enumerate(ranked, 1)
                if user_id == ctx.author.id
            ),
            None,
        )
        my_crowns = next((crowns for user_id, crowns in ranked if user_id == ctx.author.id), 0)

        summary = f"**{crowned:,}** crowned track(s) out of **{distinct:,}** scrobbled here"
        note = (
            "-# A crown is the most plays for a track; members level at the top share it."
            if solo
            else "-# Only tracks somebody else also scrobbled count. Add `all` for the rest."
        )
        await ctx.send(
            view=LastFMView(
                title="Track crowns",
                sections=[
                    summary,
                    "\n".join(lines),
                    f"**You:** {my_crowns:,} crown(s)"
                    + (f" \u00b7 #{my_rank}" if my_rank else " \u00b7 unranked"),
                    note,
                ],
            )
        )

    async def _who_knows(
        self, ctx: commands.Context, artist: str, *, everywhere: bool
    ) -> None:
        if not artist.strip():
            return await ctx.send(view=self.error_view("Give me an artist to look up."))
        if everywhere:
            ids, guild, scope = await self._all_linked_ids(), None, "bot-wide"
        else:
            if ctx.guild is None:
                return await ctx.send(view=self.error_view("That only works inside a server."))
            ids, guild, scope = await self._linked_ids(ctx.guild), ctx.guild, "this server"
        rows = await store.artist_plays_by_user(self.bot.db, ids, artist)
        await ctx.send(
            view=LastFMView(
                title=f"Who knows {shorten(artist)}",
                sections=[
                    f"**{len(rows)}** member(s) ({scope})",
                    self._listen_block(guild, rows[:10]) if rows else "No scrobbles recorded yet.",
                ],
            )
        )

    async def _who_knows_track(
        self, ctx: commands.Context, query: str, *, everywhere: bool
    ) -> None:
        track, artist = parse_query_and_artist(query) if query.strip() else ("", "")
        if not track or not artist:
            return await ctx.send(
                view=self.error_view("Usage: `!lf wktrack <track> | <artist>`")
            )
        if everywhere:
            ids, guild, scope = await self._all_linked_ids(), None, "bot-wide"
        else:
            if ctx.guild is None:
                return await ctx.send(view=self.error_view("That only works inside a server."))
            ids, guild, scope = await self._linked_ids(ctx.guild), ctx.guild, "this server"
        rows = await store.track_plays_by_user(self.bot.db, ids, artist, track)
        await ctx.send(
            view=LastFMView(
                title=f"Who knows {shorten(track)}",
                sections=[
                    f"**{artist}** \u00b7 {len(rows)} member(s) ({scope})",
                    self._listen_block(guild, rows[:10]) if rows else "No scrobbles recorded yet.",
                ],
            )
        )

    # `lf wk` is skipped as well: the main bot's `lf whoknows` already answers
    # to the `wk` alias. The rest of the local-store family is added below.
    @lf_sub(name="gwk")
    async def lf_gwk(self, ctx: commands.Context, *, artist: str = "") -> None:
        """Who across every server the bot shares listens to an artist."""
        await self._who_knows(ctx, artist, everywhere=True)

    @lf_sub(name="wktrack")
    async def lf_wktrack(self, ctx: commands.Context, *, query: str = "") -> None:
        """Who in this server listens to a track."""
        await self._who_knows_track(ctx, query, everywhere=False)

    @lf_sub(name="gwktrack")
    async def lf_gwktrack(self, ctx: commands.Context, *, query: str = "") -> None:
        """Who across every server the bot shares listens to a track."""
        await self._who_knows_track(ctx, query, everywhere=True)

    @lf_sub(name="fwk")
    async def lf_fwk(self, ctx: commands.Context, *, artist: str = "") -> None:
        """Which of your friends listen to an artist."""
        if not artist.strip():
            return await ctx.send(view=self.error_view("Usage: `!lf fwk <artist>`"))
        friends = await store.friend_ids(self.bot.db, ctx.author.id)
        if not friends:
            return await ctx.send(
                view=self.error_view(f"You have no friends added yet - try `!lf addfriend @member`.")
            )
        rows = await store.artist_plays_by_user(self.bot.db, friends, artist)
        await ctx.send(
            view=LastFMView(
                title=f"Friends who know {shorten(artist)}",
                sections=[
                    f"**{len(rows)}/{len(friends)}** friend(s)",
                    self._listen_block(ctx.guild, rows[:10]) if rows else "None of your friends scrobble them yet.",
                ],
            )
        )

    @lf_sub(name="fwktrack")
    async def lf_fwktrack(self, ctx: commands.Context, *, query: str = "") -> None:
        """Which of your friends listen to a track."""
        track, artist = parse_query_and_artist(query) if query.strip() else ("", "")
        if not track or not artist:
            return await ctx.send(
                view=self.error_view("Usage: `!lf fwktrack <track> | <artist>`")
            )
        friends = await store.friend_ids(self.bot.db, ctx.author.id)
        if not friends:
            return await ctx.send(
                view=self.error_view(f"You have no friends added yet - try `!lf addfriend @member`.")
            )
        rows = await store.track_plays_by_user(self.bot.db, friends, artist, track)
        await ctx.send(
            view=LastFMView(
                title=f"Friends who know {shorten(track)}",
                sections=[
                    f"**{artist}** \u00b7 {len(rows)}/{len(friends)} friend(s)",
                    self._listen_block(ctx.guild, rows[:10]) if rows else "None of your friends scrobble it yet.",
                ],
            )
        )

    @lf_sub(name="milestone", aliases=("ms",))
    async def lf_milestone(self, ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        """The most recent round-number scrobble you've reached."""
        target, username = await self._account(ctx, member)
        if not username:
            return
        total = await store.total_scrobbles(self.bot.db, target.id)
        if total < 1:
            return await ctx.send(
                view=self.error_view("No scrobbles recorded for them yet.")
            )
        reached = max((m for m in MILESTONES if m <= total), default=1)
        row = await store.nth_scrobble(self.bot.db, target.id, reached)
        if row is None:
            return await ctx.send(view=self.error_view("Couldn't find that scrobble."))
        played_at, artist, track, album, _source = row
        next_up = next((m for m in MILESTONES if m > total), None)
        note = f"-# {next_up - total:,} to the {next_up:,} milestone" if next_up else "-# Every milestone passed."
        await ctx.send(
            view=LastFMView(
                title=f"{reached:,}th Scrobble",
                sections=[
                    f"**{target.display_name}** \u00b7 **{total:,}** stored scrobbles",
                    track_label(track, artist)
                    + (f"\n-# {shorten(album)}" if album else ""),
                    note,
                ],
            )
        )

    @lf_sub(name="topcountries", aliases=("tc",))
    async def lf_topcountries(self, ctx: commands.Context) -> None:
        """Which countries this server's listening comes from.

        Last.fm has no per-scrobble country, so this is approximated from each
        member's own profile country, weighted by their stored scrobbles.
        """
        if ctx.guild is None:
            return await ctx.send(view=self.error_view("That only works inside a server."))
        ids = await self._linked_ids(ctx.guild)
        totals = await store.scrobble_totals(self.bot.db, ids)
        if not totals:
            return await ctx.send(view=self.error_view("No scrobbles recorded for this server yet."))
        countries: dict[str, int] = {}
        for user_id, count in totals.items():
            country = await self._country_for(user_id)
            if country:
                countries[country] = countries.get(country, 0) + count
        ranked = sorted(countries.items(), key=lambda pair: pair[1], reverse=True)[:10]
        lines = [
            entry_line(index, name, suffix=f"{count:,} scrobbles")
            for index, (name, count) in enumerate(ranked, 1)
        ]
        await ctx.send(
            view=LastFMView(
                title="Top countries",
                sections=[
                    f"**{len(ranked)}** countr(ies)",
                    "\n".join(lines) if lines else "No profile countries found.",
                    "-# Approximated from each member's Last.fm profile country.",
                ],
            )
        )

    async def _country_for(self, user_id: int) -> str:
        if user_id in self._countries:
            return self._countries[user_id]
        username = await self.username_for(user_id)
        if not username:
            return ""
        try:
            data = await self.api("user.getInfo", user=username)
            country = text(data.get("user", {}).get("country"))
        except Exception:
            country = ""
        self._countries[user_id] = country
        return country

    # ========================================================
    # FRIENDS (global, mutual)
    # ========================================================

    @lf_sub(name="addfriend", aliases=("fadd",))
    async def lf_addfriend(self, ctx: commands.Context, member: discord.Member) -> None:
        """Send a friend request to another member."""
        if member.bot or member.id == ctx.author.id:
            return await ctx.send(view=self.error_view("Pick somebody else to add."))
        status = await store.friend_status(self.bot.db, ctx.author.id, member.id)
        if status == store.STATUS_ACCEPTED:
            return await ctx.send(view=self.error_view(f"You're already friends with {member.display_name}."))
        if status == store.STATUS_PENDING:
            return await ctx.send(
                view=self.error_view(
                    f"There's already a pending request between you and {member.display_name}."
                )
            )
        await store.friend_request(self.bot.db, ctx.author.id, member.id)
        await ctx.send(
            view=LastFMView(
                title="Friend request sent",
                sections=[
                    f"**{ctx.author.display_name}** \u2192 **{member.display_name}**",
                    f"-# {member.mention} can accept with `!lf acceptfriend @{ctx.author.display_name}`,"
                    " or decline with `!lf declinefriend`.",
                ],
            )
        )

    @lf_sub(name="acceptfriend", aliases=("faccept",))
    async def lf_acceptfriend(self, ctx: commands.Context, member: discord.Member) -> None:
        """Accept a friend request from another member."""
        if await store.accept_friend(self.bot.db, member.id, ctx.author.id):
            return await ctx.send(
                view=LastFMView(
                    title="Friends",
                    sections=[f"✅ You and **{member.display_name}** are now friends."],
                )
            )
        await ctx.send(
            view=self.error_view(f"There's no pending request from {member.display_name}.")
        )

    @lf_sub(name="declinefriend", aliases=("fdecline",))
    async def lf_declinefriend(self, ctx: commands.Context, member: discord.Member) -> None:
        """Decline (or cancel) a friend request."""
        await store.remove_friend(self.bot.db, member.id, ctx.author.id)
        await ctx.send(
            view=LastFMView(
                title="Friends",
                sections=[f"Removed the request between you and **{member.display_name}**."],
            )
        )

    @lf_sub(name="removefriend", aliases=("funfriend", "unfriend"))
    async def lf_removefriend(self, ctx: commands.Context, member: discord.Member) -> None:
        """Remove a friend."""
        status = await store.friend_status(self.bot.db, ctx.author.id, member.id)
        if status is None:
            return await ctx.send(
                view=self.error_view(f"You and {member.display_name} aren't connected.")
            )
        await store.remove_friend(self.bot.db, ctx.author.id, member.id)
        await ctx.send(
            view=LastFMView(
                title="Friends",
                sections=[f"Removed **{member.display_name}** from your friends."],
            )
        )

    @lf_sub(name="friends", aliases=("flist",))
    async def lf_friends(self, ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        """List your friends (or another member's)."""
        target = member or ctx.author
        ids = await store.friend_ids(self.bot.db, target.id)
        if not ids:
            return await ctx.send(
                view=LastFMView(
                    title=f"{target.display_name}'s Friends",
                    sections=["No friends added yet."],
                )
            )
        lines = [
            entry_line(index, self._member_name(ctx.guild, user_id))
            for index, user_id in enumerate(ids, 1)
        ]
        await ctx.send(
            view=LastFMView(
                title=f"{target.display_name}'s friends",
                sections=[f"**{len(lines)}** friend(s)", "\n".join(lines)],
            )
        )

    @lf_sub(name="friendrequests", aliases=("frequests",))
    async def lf_friendrequests(self, ctx: commands.Context) -> None:
        """Requests waiting on you."""
        pending = await store.incoming_requests(self.bot.db, ctx.author.id)
        outgoing = await store.outgoing_requests(self.bot.db, ctx.author.id)
        incoming_lines = [f"**{self._member_name(ctx.guild, rid)}**" for rid, _at in pending]
        outgoing_lines = [f"**{self._member_name(ctx.guild, rid)}**" for rid in outgoing]
        sections = [
            f"**Incoming** ({len(incoming_lines)})\n"
            + ("\n".join(incoming_lines) if incoming_lines else "None"),
            f"**Sent** ({len(outgoing_lines)})\n"
            + ("\n".join(outgoing_lines) if outgoing_lines else "None"),
            "-# Accept with `!lf acceptfriend @member`, decline with `!lf declinefriend @member`.",
        ]
        await ctx.send(view=LastFMView(title="Friend Requests", sections=sections))

    # ========================================================
    # OAUTH & SCROBBLING
    # ========================================================

    def _signature(self, params: dict[str, Any]) -> str:
        """Last.fm's ``api_sig``: md5 of the sorted params plus the secret."""
        payload = "".join(f"{name}{params[name]}" for name in sorted(params))
        return hashlib.md5((payload + LASTFM_API_SECRET).encode("utf-8")).hexdigest()

    async def _signed_request(self, method: str, *, post: bool, **params) -> dict:
        if not LASTFM_API_SECRET:
            raise RuntimeError(
                "Scrobbling isn't set up: no `LASTFM_API_SECRET` is configured."
            )
        payload = {"method": method, "api_key": LASTFM_API_KEY, **params}
        payload["api_sig"] = self._signature(payload)
        payload["format"] = "json"
        if self.session is None or self.session.closed:
            raise RuntimeError("Last.fm session isn't ready yet.")
        timeout = aiohttp.ClientTimeout(total=20)
        if post:
            async with self.session.post(LASTFM_API_URL, data=payload, timeout=timeout) as response:
                data = await response.json(content_type=None)
        else:
            async with self.session.get(LASTFM_API_URL, params=payload, timeout=timeout) as response:
                data = await response.json(content_type=None)
        if "error" in data:
            raise RuntimeError(data.get("message", "Last.fm API error."))
        return data

    async def _session_for(self, user_id: int) -> Optional[str]:
        row = await store.get_session(self.bot.db, user_id)
        return row[0] if row else None

    @lf_sub(name="auth", aliases=("login", "authorize"))
    async def lf_auth(self, ctx: commands.Context, action: str = "") -> None:
        """Connect your Last.fm account for scrobbling.

        Run `!lf auth`, open the link, authorise the bot, then run
        `!lf auth confirm`. Reading commands only need `!lf set`.
        """
        if not LASTFM_API_SECRET:
            return await ctx.send(
                view=self.error_view(
                    "Scrobbling isn't configured yet - a `LASTFM_API_SECRET` needs "
                    "to be set in the environment."
                )
            )

        if action.strip().lower() in {"confirm", "done", "finish"}:
            token = self._auth_tokens.get(ctx.author.id)
            if not token:
                return await ctx.send(
                    view=self.error_view(f"Start with `!lf auth` first, then confirm.")
                )
            try:
                data = await self._signed_request("auth.getSession", post=False, token=token)
            except Exception as exc:
                return await ctx.send(view=self.error_view(f"Couldn't finish connecting: {exc}"))
            session = data.get("session", {})
            session_key = text(session.get("key"))
            username = text(session.get("name"))
            if not session_key or not username:
                return await ctx.send(
                    view=self.error_view("Last.fm didn't return a session - try `!lf auth` again.")
                )
            await store.save_session(self.bot.db, ctx.author.id, session_key, username)
            # Keep the main bot's link table in step: that is where every other
            # command here looks a member up.
            try:
                await self.bot.database.set_lastfm_username(ctx.author.id, username)
            except Exception as exc:
                logger.warning(f"[LASTFM] Could not store the link: {exc}")
            else:
                self.remember(ctx.author.id, username)
            self._auth_tokens.pop(ctx.author.id, None)
            return await ctx.send(
                view=LastFMView(
                    title="Last.fm connected",
                    sections=[
                        f"Connected as **{username}**.",
                        f"You can now use `!lf scrobble <track> | <artist>` and `!lf nowplaying`.",
                    ],
                )
            )

        try:
            data = await self._signed_request("auth.getToken", post=False)
        except Exception as exc:
            return await ctx.send(view=self.error_view(f"Couldn't start the login: {exc}"))
        token = text(data.get("token"))
        if not token:
            return await ctx.send(view=self.error_view("Last.fm didn't return a token."))
        self._auth_tokens[ctx.author.id] = token
        url = f"{LASTFM_AUTH_URL}?api_key={LASTFM_API_KEY}&token={token}"
        await ctx.send(
            view=LastFMView(
                title="Connect Last.fm",
                sections=[
                    "1. Open the link and authorise the bot.",
                    "2. Come back and run `!lf auth confirm`.",
                    f"[Authorise on Last.fm]({url})",
                ],
            )
        )

    @lf_sub(name="logout")
    async def lf_logout(self, ctx: commands.Context) -> None:
        """Forget your stored scrobbling session."""
        await store.clear_session(self.bot.db, ctx.author.id)
        await ctx.send(view=LastFMView(title="Last.fm", sections=["Scrobbling session removed."]))

    @lf_sub(name="scrobble")
    async def lf_scrobble(self, ctx: commands.Context, *, query: str = "") -> None:
        """Scrobble a track: `!lf scrobble <track> | <artist> [| <album>]`."""
        session_key = await self._session_for(ctx.author.id)
        if not session_key:
            return await ctx.send(
                view=self.error_view(f"Connect first with `!lf auth`.")
            )
        track, artist, album = self._split_parts(query, 3)
        if not track or not artist:
            return await ctx.send(
                view=self.error_view("Usage: `!lf scrobble <track> | <artist> [| <album>]`")
            )
        timestamp = int(time.time())
        params: dict[str, Any] = {
            "sk": session_key,
            "artist": artist,
            "track": track,
            "timestamp": timestamp,
        }
        if album:
            params["album"] = album
        try:
            data = await self._signed_request("track.scrobble", post=True, **params)
        except Exception as exc:
            return await ctx.send(view=self.error_view(f"Last.fm refused that scrobble: {exc}"))
        accepted = text(data.get("scrobbles", {}).get("@attr", {}).get("accepted"))
        await store.record_scrobble(
            self.bot.db, ctx.author.id, timestamp, artist, track, album, source=store.SOURCE_LASTFM
        )
        await ctx.send(
            view=LastFMView(
                title="Scrobbled",
                sections=[
                    track_label(track, artist) + (f"\n-# {shorten(album)}" if album else "")
                ],
            )
        )

    @lf_sub(name="nowplaying", aliases=("npupdate",))
    async def lf_nowplaying(self, ctx: commands.Context, *, query: str = "") -> None:
        """Set what you're playing now: `!lf nowplaying <track> | <artist>`."""
        session_key = await self._session_for(ctx.author.id)
        if not session_key:
            return await ctx.send(view=self.error_view(f"Connect first with `!lf auth`."))
        track, artist, _album = self._split_parts(query, 3)
        if not track or not artist:
            return await ctx.send(
                view=self.error_view("Usage: `!lf nowplaying <track> | <artist>`")
            )
        try:
            await self._signed_request(
                "track.updateNowPlaying", post=True, sk=session_key, artist=artist, track=track
            )
        except Exception as exc:
            return await ctx.send(view=self.error_view(f"Last.fm refused that update: {exc}"))
        await ctx.send(
            view=LastFMView(
                title="Now playing set",
                sections=[track_label(track, artist)],
            )
        )

    async def _download_image(self, url: Optional[str]) -> Optional[bytes]:
        """Fetch a cover for a chart, silently giving up on any failure."""
        if not url or self.session is None or self.session.closed:
            return None
        try:
            async with self.session.get(
                url, timeout=aiohttp.ClientTimeout(total=15)
            ) as response:
                if response.status != 200:
                    return None
                return await response.read()
        except Exception:
            return None

    # ========================================================
    # AI JUDGE
    # ========================================================

    @lf_sub(name="judge")
    async def lf_judge(self, ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        """An AI review of a member's music taste."""
        target, username = await self._account(ctx, member)
        if not username:
            return

        # The judge makes several Last.fm calls and then waits on the model, so
        # post the card up front and update it at each stage - something is
        # always visibly happening while you wait.
        header = f"**{target.display_name}** · {username}"

        def status_view(note: str) -> LastFMView:
            return LastFMView(title="The Judge", sections=[header, note])

        card = await ctx.send(
            view=status_view("⏳ Pulling your top artists, tracks and genres...")
        )

        async def status(note: str) -> None:
            await self.edit_card(card, status_view(note))

        try:
            artists_data = await self.api("user.getTopArtists", user=username, period="overall", limit=10)
            tracks_data = await self.api("user.getTopTracks", user=username, period="overall", limit=10)
            genres_data = await self.api("user.getTopTags", user=username, limit=10)
            recent_data = await self.api("user.getRecentTracks", user=username, limit=10)
        except Exception as exc:
            return await self.edit_card(card, self.error_view(f"Couldn't read that taste: {exc}"))

        def names(data: dict, root: str, key: str, limit: int = 10) -> list[str]:
            items = data.get(root, {}).get(key, [])
            if isinstance(items, dict):
                items = [items]
            return [text(item.get("name")) for item in items[:limit] if text(item.get("name"))]

        artists = names(artists_data, "topartists", "artist")
        tracks = [
            f"{text(t.get('name'))} - {text(t.get('artist', {}).get('name') if isinstance(t.get('artist'), dict) else t.get('artist'))}"
            for t in (tracks_data.get("toptracks", {}).get("track", []) or [])[:10]
        ]
        genres = names(genres_data, "toptags", "tag")
        recent = [text(t.get("name")) for t in (recent_data.get("recenttracks", {}).get("track", []) or [])[:10]]

        try:
            from cogs.ai import MODEL_ID, client as ai_client, types
        except Exception:
            return await self.edit_card(card, self.error_view("The AI cog isn't available."))

        await status(
            "⏳ Judging your music taste, give me a minute...\n"
            f"-# {len(artists)} artists · {len(genres)} genres · {len(recent)} recent scrobbles"
        )

        prompt = (
            f"Member: {target.display_name} (last.fm: {username})\n"
            f"Top artists: {', '.join(artists) or 'unknown'}\n"
            f"Top tracks: {'; '.join(tracks) or 'unknown'}\n"
            f"Top genres: {', '.join(genres) or 'unknown'}\n"
            f"Recently played: {', '.join(recent) or 'unknown'}"
        )
        config = types.GenerateContentConfig(
            system_instruction=JUDGE_SYSTEM_INSTRUCTION,
            temperature=0.9,
            max_output_tokens=600,
        )
        contents = [
            types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
        ]
        try:
            typing = ctx.channel.typing()
            async with typing:
                response = await asyncio.to_thread(
                    ai_client.models.generate_content,
                    model=MODEL_ID,
                    contents=contents,
                    config=config,
                )
            review = (response.text or "").strip()
        except Exception as exc:
            return await self.edit_card(card, self.error_view(f"The judge couldn't think: {exc}"))
        if not review:
            return await self.edit_card(
                card, self.error_view("The judge came back empty - try again.")
            )

        await self.edit_card(
            card,
            LastFMView(
                title=f"The Judge \u2014 {target.display_name}",
                sections=[f"**{username}**", shorten(review, 1500)],
            ),
        )

    # ========================================================
    # IMAGE CHARTS
    # ========================================================

    @staticmethod
    def _tier_index(listeners: int) -> int:
        for index, (_label, threshold) in enumerate(ICEBERG_TIERS):
            if listeners >= threshold:
                return index
        return len(ICEBERG_TIERS) - 1

    async def _cover_chart(self, ctx: commands.Context, mode: str, member: Optional[discord.Member]) -> None:
        target, username = await self._account(ctx, member)
        if not username:
            return
        try:
            if mode == "albums":
                data = await self.api("user.getTopAlbums", user=username, period="overall", limit=9)
                items = data.get("topalbums", {}).get("album", [])
                title = "Album Chart"
            else:
                data = await self.api("user.getTopArtists", user=username, period="overall", limit=9)
                items = data.get("topartists", {}).get("artist", [])
                title = "Artist Chart"
        except Exception as exc:
            return await ctx.send(view=self.error_view(f"Couldn't load that chart: {exc}"))
        if isinstance(items, dict):
            items = [items]
        if not items:
            return await ctx.send(view=self.error_view("No listening data to chart yet."))

        entries: list[tuple[Optional[bytes], str, str]] = []
        for item in items[:9]:
            name = text(item.get("name")) or "Unknown"
            sublabel = f"{format_number(item.get('playcount'))} plays"
            raw = await self._download_image(image_from_list(item.get("image")))
            entries.append((raw, name, sublabel))

        buffer = charts.grid(entries)
        if buffer is None:
            return await ctx.send(
                view=self.error_view("Image charts need **Pillow** installed.")
            )
        await ctx.send(
            file=discord.File(buffer, filename="chart.png"),
            view=LastFMView(
                title=f"{username}'s {title}",
                sections=[f"**Top {len(entries)}**"],
                media="attachment://chart.png",
            ),
        )

    @lf_sub(name="chartartists")
    async def lf_chartartists(self, ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        """A 3x3 image chart of your top artists."""
        await self._cover_chart(ctx, "artists", member)

    @lf_sub(name="chartalbums")
    async def lf_chartalbums(self, ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        """A 3x3 image chart of your top albums."""
        await self._cover_chart(ctx, "albums", member)

    @lf_sub(name="iceberg")
    async def lf_iceberg(self, ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        """Your taste as an iceberg, from mainstream down to deep cuts."""
        target, username = await self._account(ctx, member)
        if not username:
            return
        top = await store.top_artists(self.bot.db, [target.id], limit=30)
        if not top:
            return await ctx.send(
                view=self.error_view("No scrobbles recorded for them yet - the poller needs a cycle.")
            )

        cache = await store.cached_artists(
            self.bot.db, [artist_key for _n, artist_key, _p, _l in top]
        )
        fetched: list[tuple[str, str, int, int]] = []
        for name, artist_key, _plays, _listeners in top:
            if artist_key in cache:
                continue
            listeners = playcount = 0
            try:
                data = await self.api("artist.getInfo", artist=name)
                stats = data.get("artist", {}).get("stats", {})
                listeners = int(str(stats.get("listeners") or 0).replace(",", "") or 0)
                playcount = int(str(stats.get("playcount") or 0).replace(",", "") or 0)
            except Exception:
                pass
            cache[artist_key] = (name, listeners, playcount, 0)
            fetched.append((artist_key, name, listeners, playcount))
            await asyncio.sleep(0.2)
        if fetched:
            await store.store_artists(self.bot.db, fetched)

        tiers: list[tuple[str, list[str]]] = [(label, []) for label, _t in ICEBERG_TIERS]
        for name, artist_key, _plays, _listeners in top:
            index = self._tier_index(cache.get(artist_key, (name, 0, 0, 0))[1])
            tiers[index][1].append(name)
        shown = [(label, names) for label, names in tiers if names]

        ordered = [name for _label, names in shown for name in names]
        above_names = sum(
            len(names) for label, names in shown if label in ICEBERG_ABOVE_TIERS
        )
        buffer = charts.iceberg(
            ordered,
            title=target.display_name,
            subtitle="Overall",
            above=(above_names + 2) // 3 if above_names else None,
        )
        sections = [f"**{username}** \u00b7 {len(top)} artist(s)"]
        if buffer is None:
            sections += [f"**{label}**\n" + ", ".join(names) for label, names in shown]
            return await ctx.send(view=LastFMView(title="Taste Iceberg", sections=sections))
        sections.append("-# Sorted by each artist's global Last.fm listeners.")
        await ctx.send(
            file=discord.File(buffer, filename="iceberg.png"),
            view=LastFMView(
                title="Taste Iceberg",
                sections=sections,
                media="attachment://iceberg.png",
            ),
        )

    # ========================================================
    # LISTENER
    # ========================================================

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return

        content = message.content.strip()
        try:
            prefix = await self.bot.get_prefix(message)
        except Exception:
            prefix = PREFIX
        if isinstance(prefix, list):
            prefix = prefix[0] if prefix else PREFIX
        if not isinstance(prefix, str) or not prefix or not content.startswith(prefix):
            return

        parts = content[len(prefix):].split()
        if not parts:
            return

        command_name = parts[0].lower()
        if command_name in {"lf", "np", "fm", "help"}:
            return

        account = self.data.get(str(message.author.id), {})
        custom = text(account.get("custom_command")).lower()
        if not custom or command_name != custom:
            return

        # A custom now-playing command runs the main bot's own `np`, so the
        # member gets the card (and vote buttons) they are already used to.
        command = self.bot.get_command("np")
        if command is None:
            return
        ctx = await self.bot.get_context(message)
        try:
            await ctx.invoke(command)
        except commands.CommandError as exc:
            # A cooldown or a failed check on `np` shouldn't spill out of a
            # message listener; the main bot's error handler reports it if it
            # came from a real invocation.
            logger.debug(f"[LASTFM] custom command `{command_name}` did not run: {exc}")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LastFM(bot))
