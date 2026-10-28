from __future__ import annotations

import asyncio
import difflib
import hashlib
import html
import json
import logging
import os
import re
import time
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import quote, unquote

import aiohttp
import discord
from discord.ext import commands

from config import EMBED_COLOR, PREFIX
from utils.core import cache_registry, file_mode, media_cache
from utils.core.cache_registry import CacheProvider, ReloadResult
from utils.core.eras import emoji_for_era
from utils.core.errors import USAGE_ERRORS, send_command_error, send_usage_help
from utils.core.helpers import SendHelp
from utils.core.media_cache import SNIPPET_CACHE_DIR

logger = logging.getLogger(__name__)

API_ROOT = "https://juicewrldapi.com"
API_BASE = f"{API_ROOT}/juicewrld"
BROWSE_ENDPOINT = f"{API_BASE}/files/browse/"
DOWNLOAD_ENDPOINT = f"{API_BASE}/files/download/"

REQUEST_TIMEOUT = 30
FUZZY_THRESHOLD = 0.55
SELECT_TIMEOUT = 120
VIEW_TIMEOUT = 300
MAX_GALLERY_ITEMS = 10
DOWNLOAD_CONCURRENCY = 10
PREFETCH_PAGES = 3
FAILED_DOWNLOAD_TTL = 300
#: Songs endpoint used to fill a snippet folder's card (artwork, alt names,
#: engineers, producers); folders themselves only carry a name.
SONGS_ENDPOINT = f"{API_BASE}/songs/"
MAX_SEARCH_RESULTS = 100
SONG_META_TTL = 30 * 60
# Resolved snippets are cached permanently (like cover art), so a repeated
# request skips the folder listing and the downloads entirely — even across a
# restart. The index lives beside the media directory rather than inside it so
# the dashboard media route can never serve the manifest itself.
SNIPPET_CACHE_MANIFEST = SNIPPET_CACHE_DIR.with_suffix(".json")
SNIPPET_CACHE_VERSION = 1
# A folder with no snippets is re-checked once the Catalog index would be rebuilt.
SNIPPET_INDEX_TTL = 6 * 60 * 60
SNIPPET_MISS_TTL = SNIPPET_INDEX_TTL
#: Shortest gap between two progress edits of the `cachereload` card. Discord
#: rate-limits message edits, so a reload walks folders and reports on a timer
#: rather than once per folder.
RELOAD_PROGRESS_SECONDS = 3.0

VIDEO_EXTENSIONS = (
    ".mp4",
    ".mov",
    ".mkv",
    ".webm",
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def clean_title(title: str) -> str:
    clean = re.sub(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}", "", title)
    clean = re.sub(r"\s+(?:feat|ft|with|prod)\.?\s+.*$", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"[()\[\]{}]", "", clean)
    return re.sub(r"\s+", " ", clean).strip() or title


def normalize(value: str) -> str:
    if not value:
        return ""
    val = clean_title(html.unescape(unquote(str(value)))).lower()
    return re.sub(r"[^\w\s]", " ", val).strip()


#: A parenthesised snippet-folder suffix that is only a version marker
#: (``(v1.1)``, ``(Pt. 2)``), not an alternate title.
_VERSION_SUFFIX_RE = re.compile(
    r"(?:v|ver|version|vol|pt|part)?\.?\s*\d+(?:\.\d+)*", re.IGNORECASE
)


def normalize_raw(value: str) -> str:
    """Normalize while keeping parenthesised text.

    :func:`normalize` runs :func:`clean_title`, which strips brackets — that is
    exactly what hid the alternate titles snippet folders record as
    ``Charcoal (Time) (v1.1)``.
    """
    if not value:
        return ""
    val = html.unescape(unquote(str(value))).lower()
    return re.sub(r"[^a-z0-9\s]", " ", val).strip()


def folder_primary_name(folder_name: str) -> str:
    """The song's main title, without the parenthesised alt names."""
    return clean_title(folder_name)


def folder_alt_names(folder_name: str) -> list[str]:
    """Alternate titles a snippet folder records in parentheses."""
    primary = normalize(folder_primary_name(folder_name))
    alts: list[str] = []
    seen = {primary}
    for part in re.findall(r"\(([^)]*)\)", folder_name):
        text = clean_text(part)
        if not text or _VERSION_SUFFIX_RE.fullmatch(text):
            continue
        norm = normalize(text)
        if norm and norm not in seen:
            seen.add(norm)
            alts.append(text)
    return alts


def song_alt_names(song: dict[str, Any]) -> list[str]:
    """Every alternate title the API reports for a song, minus its main name."""
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
    alts: list[str] = []
    seen = {primary}
    for name in names:
        norm = normalize(name)
        if norm and norm not in seen:
            seen.add(norm)
            alts.append(name)
    return alts


def song_image_url(song: dict[str, Any]) -> str | None:
    """The song's artwork as an absolute URL, or ``None`` without one."""
    value = clean_text(song.get("image_url") or song.get("imageUrl"))
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        return value
    return f"{API_ROOT}{value if value.startswith('/') else '/' + value}"


def folder_as_song(folder_name: str) -> dict[str, Any]:
    """A minimal song record built from a folder when the API has no match."""
    primary = folder_primary_name(folder_name) or folder_name
    return {"name": primary, "track_titles": [primary, *folder_alt_names(folder_name)]}


def folder_match_score(target_norm: str, folder_name: str) -> float:
    """How well a snippet folder matches a query, counting its alt names.

    Snippet folders are named ``Primary (Alt) (vN)``. Each title is scored on
    its own so an exact alternate-title match (``time`` -> ``Charcoal (Time)``)
    ranks above a song whose *main* name merely contains the word (``On Time``).
    """
    if not target_norm:
        return 0.0

    primary = normalize(folder_primary_name(folder_name))
    alts = [normalize(name) for name in folder_alt_names(folder_name)]
    raw = normalize_raw(folder_name)
    phrase = rf"(?<!\w){re.escape(target_norm)}(?!\w)"

    if primary == target_norm:
        return 1.0
    if target_norm in alts:
        return 0.95
    if primary and re.search(phrase, primary):
        return 0.80
    if any(re.search(phrase, alt) for alt in alts if alt):
        return 0.75
    if re.search(phrase, raw):
        return 0.60
    if target_norm in raw:
        return 0.50
    if primary:
        ratio = difflib.SequenceMatcher(None, target_norm, primary).ratio()
        if ratio >= FUZZY_THRESHOLD:
            return ratio * 0.5
    return 0.0


def make_download_url(path: str) -> str:
    clean_p = path.strip().lstrip("/")
    return f"{DOWNLOAD_ENDPOINT}?path={quote(clean_p, safe='')}&channel=comp"


def parse_api_json(text: str) -> Any:
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass

    pre_match = re.search(r"<pre[^>]*>(.*?)</pre>", text, re.DOTALL | re.IGNORECASE)
    if pre_match:
        inner = html.unescape(pre_match.group(1).strip())
        try:
            return json.loads(inner)
        except Exception:
            pass

    return None


def is_video_item(item: dict[str, Any]) -> bool:
    name = clean_text(item.get("name"))
    extension = str(item.get("extension") or "").lower()
    if extension and not extension.startswith("."):
        extension = f".{extension}"
    if not extension and "." in name:
        extension = f".{name.rsplit('.', 1)[-1].lower()}"
    return extension in VIDEO_EXTENSIONS


def folder_has_items(item: dict[str, Any]) -> bool:
    """Whether a directory listing reports the folder as containing files.

    Folders without an ``item_count`` are treated as unknown (kept) rather than
    empty, so a missing field never hides songs from ``erafind``.
    """
    count = item.get("item_count")
    if count in (None, ""):
        return True
    try:
        return int(count) > 0
    except (TypeError, ValueError):
        return True


def text_display(content: str) -> discord.ui.TextDisplay:
    return discord.ui.TextDisplay(content=content)


def small_separator() -> discord.ui.Separator:
    return discord.ui.Separator(spacing=discord.SeparatorSpacing.small)


def simple_view(content: str, *, timeout: int = 60) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=timeout)
    container = discord.ui.Container(accent_color=EMBED_COLOR)
    container.add_item(text_display(content))
    view.add_item(container)
    return view


class SnippetCache:
    """Persistent snippet cache kept as one JSON file beside the media directory.

    Two things are stored: the resolved snippets for every folder that has been
    requested, and the Catalog song-folder index. Both let a repeat request (or
    the first request after a restart) answer without listing the API or
    downloading anything. Records whose cached files have gone missing are
    ignored, which makes the cache self-healing.
    """

    def __init__(self, path: Any = SNIPPET_CACHE_MANIFEST) -> None:
        self.path = path
        self._folders: dict[str, dict[str, Any]] = {}
        self._eras: dict[str, Any] | None = None
        self._index: dict[str, Any] | None = None
        self._lock = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        try:
            if not self.path.is_file():
                return
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            folders = data.get("folders")
            if isinstance(folders, dict):
                self._folders = {
                    str(key): value
                    for key, value in folders.items()
                    if isinstance(value, dict)
                }
            if isinstance(data.get("eras"), dict):
                self._eras = data["eras"]
            if isinstance(data.get("index"), dict):
                self._index = data["index"]
        except Exception:
            logger.exception("[Snippets] Could not read the snippet cache index")
            self._folders = {}
            self._eras = None
            self._index = None

    @property
    def size(self) -> int:
        return len(self._folders)

    def get_folder(self, path: str, mode: str) -> list[dict[str, Any]] | None:
        """Cached snippets for a folder, or ``None`` on a cache miss."""
        record = self._folders.get(str(path))
        if not isinstance(record, dict):
            return None

        expires_at = record.get("expires_at")
        if expires_at is not None and time.time() >= float(expires_at):
            return None

        # Re-resolve if the file mode changed (direct links vs cached files).
        if record.get("mode") != mode:
            return None

        entries = record.get("entries")
        if not isinstance(entries, list):
            return None

        if mode != file_mode.DIRECT:
            for entry in entries:
                filename = entry.get("file")
                if filename and not (SNIPPET_CACHE_DIR / str(filename)).is_file():
                    logger.info("[Snippets] Cached media for '%s' is gone; re-resolving", path)
                    return None
        return [dict(entry) for entry in entries]

    async def set_folder(
        self,
        path: str,
        entries: list[dict[str, Any]],
        mode: str,
        *,
        ttl: float | None = None,
    ) -> None:
        async with self._lock:
            self._folders[str(path)] = {
                "fetched_at": time.time(),
                "mode": mode,
                "expires_at": None if ttl is None else time.time() + ttl,
                "entries": entries,
            }
            self._save_locked()

    def get_eras(self, ttl: float) -> list[dict[str, Any]] | None:
        record = self._eras
        if not isinstance(record, dict):
            return None
        if time.time() - float(record.get("fetched_at") or 0) >= ttl:
            return None
        items = record.get("items")
        if not isinstance(items, list):
            return None
        return [dict(item) for item in items]

    async def set_eras(self, items: list[dict[str, Any]]) -> None:
        async with self._lock:
            self._eras = {"fetched_at": time.time(), "items": items}
            self._save_locked()

    def get_index(self, ttl: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
        record = self._index
        if not isinstance(record, dict):
            return None
        if time.time() - float(record.get("fetched_at") or 0) >= ttl:
            return None
        eras = record.get("eras")
        folders = record.get("folders")
        if not isinstance(eras, list) or not isinstance(folders, list):
            return None
        return [dict(item) for item in eras], [dict(item) for item in folders]

    async def set_index(
        self,
        eras: list[dict[str, Any]],
        folders: list[dict[str, Any]],
    ) -> None:
        async with self._lock:
            now = time.time()
            self._index = {"fetched_at": now, "eras": eras, "folders": folders}
            self._eras = {"fetched_at": now, "items": eras}
            self._save_locked()

    # -- reloading ---------------------------------------------------

    def cached_folders(self) -> list[str]:
        """Every folder path with a stored result, for a targeted reload."""
        return [str(path) for path in self._folders]

    async def clear(self) -> None:
        """Drop every resolved folder, the era list and the index in one write.

        Used by ``,cachereload``: the media files themselves are left alone, so
        re-resolving re-uses (and re-hosts) whatever is still on disk and only
        downloads what is actually missing.
        """
        async with self._lock:
            self._folders = {}
            self._eras = None
            self._index = None
            self._save_locked()

    async def forget_folder(self, path: str) -> bool:
        """Forget one folder's result, leaving the rest of the cache alone."""
        async with self._lock:
            if str(path) not in self._folders:
                return False
            del self._folders[str(path)]
            self._save_locked()
            return True

    def _save_locked(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "version": SNIPPET_CACHE_VERSION,
                        "folders": self._folders,
                        "eras": self._eras,
                        "index": self._index,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        except Exception:
            logger.exception("[Snippets] Could not write the snippet cache index")


class SnippetView(discord.ui.LayoutView):
    """Components V2 media gallery with local-media pagination."""

    def __init__(
        self,
        song_title: str,
        era_name: str,
        files: list[dict[str, Any]],
        *,
        author_id: int | None = None,
        title_prefix: str = "",
        page_loader: Callable[[int], Awaitable[list[dict[str, Any]]]] | None = None,
        total_items: int | None = None,
        title_from_page: bool = False,
        page_namer: Callable[[int], str | None] | None = None,
        mode: str = file_mode.DEFAULT_MODE,
        song: Optional[dict[str, Any]] = None,
    ):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.song_title = clean_text(song_title) or "Unknown Song"
        self.era_name = clean_text(era_name) or "Unknown Era"
        self.song = song
        self.files = files
        self.author_id = author_id
        self.title_prefix = title_prefix
        self.page_loader = page_loader
        # When True each page belongs to a different song, so the heading is
        # taken from the current page's entries instead of ``song_title``.
        self.title_from_page = title_from_page
        self.page_namer = page_namer
        self.mode = mode
        self.total_items = len(files) if total_items is None else total_items
        self.page = 1
        self.total_pages = max(1, (self.total_items + MAX_GALLERY_ITEMS - 1) // MAX_GALLERY_ITEMS)
        self.loading = False
        self.page_cache: dict[int, list[dict[str, Any]]] = {1: files}
        self.page_tasks: dict[int, asyncio.Task] = {}
        self.message: Optional[discord.Message] = None
        self._render()
        self._schedule_prefetch()

    def _schedule_prefetch(self) -> None:
        if self.page_loader is None:
            return
        for page in range(
            self.page + 1,
            min(self.total_pages, self.page + PREFETCH_PAGES) + 1,
        ):
            if page not in self.page_cache and page not in self.page_tasks:
                self.page_tasks[page] = asyncio.create_task(self._load_page(page))

    async def _load_page(self, page: int) -> list[dict[str, Any]]:
        try:
            if self.page_loader is None:
                return []
            result = await self.page_loader(page)
            self.page_cache[page] = result
            return result
        except Exception as exc:
            logger.warning("[Snippets] Prefetch failed for page %d: %s", page, exc)
            self.page_cache[page] = []
            return []
        finally:
            self.page_tasks.pop(page, None)

    async def _get_page(self, page: int) -> list[dict[str, Any]]:
        cached = self.page_cache.get(page)
        if cached is not None:
            return cached
        task = self.page_tasks.get(page)
        if task is None:
            task = asyncio.create_task(self._load_page(page))
            self.page_tasks[page] = task
        return await task

    def _page_files(self) -> list[dict[str, Any]]:
        # Local mode: ``files`` holds every snippet for one song, so a page is
        # simply its slice. Loader mode (one song per page) supplies the page
        # contents instead.
        if self.page_loader is not None:
            return self.page_cache.get(self.page, self.files)
        start = (self.page - 1) * MAX_GALLERY_ITEMS
        return self.files[start : start + MAX_GALLERY_ITEMS]

    def _page_title(self) -> str | None:
        """Title for the current page, from ``page_namer`` or its ``song_name``."""
        if not self.title_from_page:
            return None
        if self.page_namer is not None:
            try:
                named = clean_text(self.page_namer(self.page))
            except Exception:
                named = ""
            if named:
                return named
        for item in self._page_files():
            name = clean_text(item.get("song_name"))
            if name:
                return name
        return None

    def _render(self) -> None:
        self.clear_items()
        container = discord.ui.Container(accent_color=EMBED_COLOR)

        display_title = self._page_title() or self.song_title
        shown = self._page_files()

        if self.song:
            # The song card header the rest of the bot uses (and the old snippet
            # embed had): artwork plus alt names / engineers / producers.
            name = clean_text(self.song.get("name")) or display_title
            alts = ", ".join(song_alt_names(self.song))
            engineers = clean_text(self.song.get("engineers")) or "N/A"
            producers = clean_text(self.song.get("producers")) or "N/A"
            header = (
                f"### {name}\n"
                f"-# Alt Name(s): **{alts or 'N/A'}**\n"
                f"-# Engineer(s): **{engineers}**\n"
                f"-# Producer(s): **{producers}**"
            )
            image_url = song_image_url(self.song)
            if image_url:
                section = discord.ui.Section(
                    accessory=discord.ui.Thumbnail(media=image_url)
                )
                section.add_item(text_display(header))
                container.add_item(section)
            else:
                container.add_item(text_display(header))
            container.add_item(small_separator())
            if self.era_name and self.era_name != "Unknown Era":
                container.add_item(text_display(f"**Era**\n{self.era_name}"))
                container.add_item(small_separator())
        else:
            # The same ``# Title`` + ``-# small print`` header the rest of the
            # bot's cards use, with the era and the count underneath.
            meta = [self.era_name, f"{self.total_items} snippet(s)"]
            if self.total_pages > 1:
                meta.append(f"page {self.page}/{self.total_pages}")
            heading = f"# {self.title_prefix}{display_title}"
            heading += "\n-# " + " · ".join(part for part in meta if part)
            container.add_item(text_display(heading))
            container.add_item(small_separator())

        if not self.files:
            container.add_item(text_display("❌ No video snippets found."))
        else:
            # A page already holds at most ``MAX_GALLERY_ITEMS`` snippets, so
            # everything fits in the gallery — the rest live on later pages.
            gallery_items = [
                discord.MediaGalleryItem(
                    media=item["media_url"],
                    description=item.get("name"),
                )
                for item in shown
                if item.get("media_url")
            ]
            if gallery_items:
                meta = [f"{self.total_items} snippet(s)"]
                if self.total_pages > 1:
                    meta.append(f"page {self.page}/{self.total_pages}")
                container.add_item(text_display("> **Clips**\n-# " + " · ".join(meta)))
                container.add_item(small_separator())
                container.add_item(discord.ui.MediaGallery(*gallery_items))
            elif self.mode == file_mode.LOCAL:
                container.add_item(
                    text_display(
                        "❌ The snippets were found, but there is no public media "
                        "URL to serve them from.\n"
                        "-# Set `DASHBOARD_PUBLIC_URL` — or turn off local caching so "
                        "the links point at the source — and try again."
                    )
                )
            else:
                container.add_item(
                    text_display("❌ No playable snippet links were found for this folder.")
                )

        if self.total_pages > 1:
            container.add_item(small_separator())
            previous = discord.ui.Button(
                label="◀ Previous",
                style=discord.ButtonStyle.secondary,
                disabled=self.page <= 1,
            )
            next_button = discord.ui.Button(
                label="Next ▶",
                style=discord.ButtonStyle.secondary,
                disabled=self.page >= self.total_pages,
            )
            previous.callback = self.previous_page
            next_button.callback = self.next_page
            container.add_item(discord.ui.ActionRow(previous, next_button))

        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.author_id is not None and interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This paginator belongs to someone else.", ephemeral=True
            )
            return False
        return True

    async def _change_page(self, interaction: discord.Interaction, page: int) -> None:
        if self.loading:
            return await interaction.response.defer()
        self.loading = True
        await interaction.response.defer()
        try:
            if self.page_loader is not None:
                self.files = await self._get_page(page)
            self.page = page
            self._render()
            self._schedule_prefetch()
            if interaction.message:
                await interaction.message.edit(view=self)
                self.message = interaction.message
        finally:
            self.loading = False

    async def previous_page(self, interaction: discord.Interaction) -> None:
        if self.page <= 1:
            return await interaction.response.defer()
        await self._change_page(interaction, self.page - 1)

    async def next_page(self, interaction: discord.Interaction) -> None:
        if self.page >= self.total_pages:
            return await interaction.response.defer()
        await self._change_page(interaction, self.page + 1)

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Container):
                for item in child.children:
                    if isinstance(item, discord.ui.ActionRow):
                        for button in item.children:
                            if isinstance(button, discord.ui.Button):
                                button.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass


class SongSelect(discord.ui.Select):
    def __init__(self, candidate_folders: list[dict[str, Any]], author_id: int, cog: "Snippets"):
        self.candidate_folders = candidate_folders
        self.author_id = author_id
        self.cog = cog

        options = []
        for idx, item in enumerate(candidate_folders[:25]):
            raw_name = clean_text(item.get("name"))
            era = clean_text(item.get("era", "Unknown Era"))
            count = item.get("item_count", 0)
            alts = folder_alt_names(raw_name)
            parts = [f"Era {era}"]
            if alts:
                parts.append(f"Alt: {', '.join(alts)}")
            parts.append(f"{count} item{'s' if count != 1 else ''}")
            options.append(
                discord.SelectOption(
                    label=(raw_name or "Unknown Song")[:100],
                    value=str(idx),
                    description=" · ".join(parts)[:100],
                    emoji=emoji_for_era(self.cog.bot, era),
                )
            )

        super().__init__(
            placeholder="Choose the correct snippet folder...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Only the person who ran the command can choose.", ephemeral=True
            )
            return

        if self.view:
            self.view.selected = True
            self.view.stop()

        await interaction.response.defer()
        chosen = self.candidate_folders[int(self.values[0])]
        folder_name = chosen.get("name", "Unknown Song")
        files = await self.cog.fetch_folder_snippets(chosen.get("path", ""))
        song = await self.cog.fetch_song_metadata(folder_name)
        view = SnippetView(
            song_title=folder_name,
            era_name=chosen.get("era", ""),
            files=files,
            author_id=self.author_id,
            song=song or folder_as_song(folder_name),
            mode=file_mode.get_mode(self.cog.bot),
        )
        view.message = interaction.message
        await interaction.edit_original_response(view=view)


class SongSelectView(discord.ui.LayoutView):
    def __init__(self, query: str, candidate_folders: list[dict[str, Any]], author_id: int, cog: "Snippets"):
        super().__init__(timeout=SELECT_TIMEOUT)
        self.selected = False
        self.message: Optional[discord.Message] = None

        container = discord.ui.Container(accent_color=EMBED_COLOR)
        container.add_item(
            text_display(
                f"# 🔍 snip\n"
                f"> **Query** {query}\n"
                f"> **Matches** {len(candidate_folders)}\n"
                f"-# Pick the correct folder below."
            )
        )
        container.add_item(discord.ui.ActionRow(SongSelect(candidate_folders, author_id, cog)))
        self.add_item(container)

    async def on_timeout(self):
        if self.selected or not self.message:
            return
        try:
            await self.message.edit(
                view=simple_view("⏱️ Selection timed out. Please run the command again.", timeout=10)
            )
        except Exception:
            pass


class Snippets(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self.session_lock = asyncio.Lock()
        self.download_semaphore = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)
        self.cache_locks: dict[str, asyncio.Lock] = {}

        self._eras_cache: list[dict[str, Any]] = []
        self._song_folders_cache: list[dict[str, Any]] = []
        self._cache_timestamp: float = 0
        self._snippet_cache = SnippetCache()
        #: folder primary name -> (fetched_at, song record or None)
        self._song_meta_cache: dict[str, tuple[float, Optional[dict[str, Any]]]] = {}
        #: The `cachereload` run in flight, so a second one can't start on top.
        self._reload_task: Optional[asyncio.Task] = None
        self._register_cache_provider()

    async def get_session(self) -> aiohttp.ClientSession:
        async with self.session_lock:
            if self.session and not self.session.closed:
                return self.session
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                headers=HEADERS,
            )
            return self.session

    def cog_unload(self):
        cache_registry.unregister("snippets")
        if self._reload_task and not self._reload_task.done():
            self._reload_task.cancel()

        async def close():
            if self.session and not self.session.closed:
                await self.session.close()

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(close())
        except RuntimeError:
            pass

    async def browse_path(self, path: str) -> list[dict[str, Any]]:
        session = await self.get_session()
        try:
            async with session.get(
                BROWSE_ENDPOINT,
                params={"path": path},
                headers=HEADERS,
            ) as resp:
                if resp.status != 200:
                    return []
                data = parse_api_json(await resp.text())
                if isinstance(data, dict):
                    return data.get("items", [])
                return []
        except Exception as exc:
            logger.warning("[Snippets] Browse failed on path '%s': %s", path, exc)
            return []

    async def _ensure_folder_index(self):
        now = time.time()
        if self._song_folders_cache and (now - self._cache_timestamp < SNIPPET_INDEX_TTL):
            return

        # Restoring the persisted index means a restart (or the 6-hour refresh)
        # no longer has to browse every era before `snip` can search at all.
        restored = self._snippet_cache.get_index(SNIPPET_INDEX_TTL)
        if restored is not None:
            self._eras_cache, self._song_folders_cache = restored
            self._cache_timestamp = now
            logger.info(
                "[Snippets] Restored %d song snippet folders from the persistent index.",
                len(self._song_folders_cache),
            )
            return

        if not self._eras_cache:
            self._eras_cache = await self._load_eras()

        tasks = [self.browse_path(era["path"]) for era in self._eras_cache]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_song_folders = []
        for era_item, era_result in zip(self._eras_cache, results):
            if isinstance(era_result, list):
                for item in era_result:
                    if item.get("type") == "directory":
                        all_song_folders.append(
                            {
                                "name": item.get("name", ""),
                                "path": item.get("path", ""),
                                "era": era_item.get("name", "Unknown Era"),
                                "item_count": item.get("item_count", 0),
                            }
                        )

        if all_song_folders:
            self._song_folders_cache = all_song_folders
            self._cache_timestamp = now
            await self._snippet_cache.set_index(self._eras_cache, all_song_folders)
            logger.info(
                "[Snippets] Indexed %d song snippet folders across %d eras.",
                len(all_song_folders),
                len(self._eras_cache),
            )

    async def _load_eras(self) -> list[dict[str, Any]]:
        """Era folders, from the persistent cache first, then the API."""
        cached = self._snippet_cache.get_eras(SNIPPET_INDEX_TTL)
        if cached:
            return cached
        top_items = await self.browse_path("Snippets")
        eras = [item for item in top_items if item.get("type") == "directory"]
        if eras:
            await self._snippet_cache.set_eras(eras)
        return eras

    async def find_matching_folders(self, query: str) -> list[dict[str, Any]]:
        await self._ensure_folder_index()
        target_norm = normalize(query)
        if not target_norm:
            return []

        # Folders are matched on their main title *and* the alternate titles
        # they record in parentheses, best match first, so an alt-name search
        # (``time`` -> ``Charcoal (Time) (v1.1)``) is not pushed out by the
        # folders whose main name happens to contain the word.
        scored = [
            (folder_match_score(target_norm, folder.get("name", "")), folder)
            for folder in self._song_folders_cache
        ]
        scored = [entry for entry in scored if entry[0] > 0]
        scored.sort(key=lambda entry: entry[0], reverse=True)
        return [folder for _, folder in scored[:25]]

    async def fetch_song_metadata(self, folder_name: str) -> Optional[dict[str, Any]]:
        """The API's song record for a snippet folder, for the card header.

        Snippet folders only carry a name, so the artwork, alternate titles,
        engineers and producers are looked up on the songs endpoint and cached.
        """
        primary = folder_primary_name(folder_name)
        key = normalize(primary)
        if not key:
            return None

        now = time.time()
        cached = self._song_meta_cache.get(key)
        if cached and now - cached[0] < SONG_META_TTL:
            return cached[1]

        best: Optional[dict[str, Any]] = None
        session = await self.get_session()
        try:
            async with session.get(
                SONGS_ENDPOINT,
                params={"search": primary, "page_size": MAX_SEARCH_RESULTS},
                headers=HEADERS,
            ) as resp:
                if resp.status == 200:
                    data = parse_api_json(await resp.text())
                    results = data.get("results", []) if isinstance(data, dict) else []
                    best = self._best_song_match(key, results)
        except Exception as exc:
            logger.warning("[Snippets] Song lookup failed for '%s': %s", primary, exc)

        self._song_meta_cache[key] = (now, best)
        return best

    @staticmethod
    def _best_song_match(
        key: str, results: list[dict[str, Any]]
    ) -> Optional[dict[str, Any]]:
        """The API song whose main title (or an alt) matches the folder name."""
        best: Optional[dict[str, Any]] = None
        best_score = 0.0
        for song in results:
            if not isinstance(song, dict):
                continue
            names = [normalize(song.get("name", ""))]
            names.extend(normalize(t) for t in (song.get("track_titles") or []))
            names = [name for name in names if name]
            if not names:
                continue
            if key in names:
                return song
            score = max(difflib.SequenceMatcher(None, key, name).ratio() for name in names)
            if score > best_score:
                best_score, best = score, song
        if best is None or best_score < 0.8:
            return None
        return best

    async def _cache_snippet(self, item: dict[str, Any]) -> dict[str, Any] | None:
        """Download one API file, cache it, and return its hosted URL."""
        source_path = clean_text(item.get("path"))
        if not source_path:
            return None

        ext = str(item.get("extension") or "").lower()
        if not ext and "." in item.get("name", ""):
            ext = f".{item['name'].rsplit('.', 1)[-1].lower()}"
        elif ext and not ext.startswith("."):
            ext = f".{ext}"
        if ext not in VIDEO_EXTENSIONS:
            return None

        public_filename = hashlib.sha256(source_path.encode("utf-8")).hexdigest() + ext
        cached_path = SNIPPET_CACHE_DIR / public_filename
        file_lock = self.cache_locks.setdefault(public_filename, asyncio.Lock())
        async with file_lock:
            if not cached_path.is_file():
                session = await self.get_session()
                try:
                    SNIPPET_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    async with self.download_semaphore:
                        async with session.get(
                            make_download_url(source_path),
                            headers=HEADERS,
                            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                        ) as resp:
                            if resp.status != 200:
                                logger.warning(
                                    "[Snippets] Download failed for '%s' (HTTP %s)",
                                    source_path,
                                    resp.status,
                                )
                                return None
                            raw = await resp.read()
                    if not raw or raw[:5].lower() in {b"<!doc", b"<html"}:
                        logger.warning("[Snippets] Download returned invalid media for '%s'", source_path)
                        return None
                    temporary_path = cached_path.with_suffix(cached_path.suffix + ".part")
                    temporary_path.write_bytes(raw)
                    temporary_path.replace(cached_path)
                except Exception as exc:
                    logger.warning("[Snippets] Download failed for '%s': %s", source_path, exc)
                    return None

        local_url = await media_cache.ensure_media("Snippets", public_filename, cached_path)
        if not local_url:
            logger.warning("[Snippets] Could not host '%s'", source_path)
            return None

        return {
            "name": item.get("name", "Snippet"),
            "path": source_path,
            "size_human": item.get("size_human", ""),
            "file": public_filename,
            "local_url": local_url,
            "media_url": local_url,
        }

    def _direct_snippet(self, item: dict[str, Any]) -> dict[str, Any] | None:
        """Point straight at the API download URL without downloading anything."""
        source_path = clean_text(item.get("path"))
        if not source_path or not is_video_item(item):
            return None
        return {
            "name": item.get("name", "Snippet"),
            "path": source_path,
            "size_human": item.get("size_human", ""),
            "media_url": make_download_url(source_path),
        }

    async def _media_entry(self, item: dict[str, Any]) -> dict[str, Any] | None:
        """Resolve one snippet according to the active file mode.

        Falls back to direct links whenever the bucket can't host the file, so
        the commands keep working on a bot with no R2 setup.
        """
        if file_mode.get_mode(self.bot) != file_mode.LOCAL:
            return self._direct_snippet(item)
        if not media_cache.hosting_available():
            return self._direct_snippet(item)
        return await self._cache_snippet(item)

    async def fetch_folder_snippets(self, folder_path: str) -> list[dict[str, Any]]:
        mode = file_mode.get_mode(self.bot)
        cached = self._snippet_cache.get_folder(folder_path, mode)
        if cached is not None:
            logger.debug("[Snippets] Cache hit for folder '%s'", folder_path)
            return cached

        items = await self.browse_path(folder_path)
        video_items = []
        for item in items:
            if is_video_item(item):
                video_items.append(item)

        if not video_items:
            await self._snippet_cache.set_folder(
                folder_path, [], mode, ttl=SNIPPET_MISS_TTL
            )
            return []

        results = await asyncio.gather(
            *(self._media_entry(item) for item in video_items),
            return_exceptions=True,
        )
        snippets = [item for item in results if isinstance(item, dict)]
        # A fully resolved folder is cached permanently. If some videos failed
        # to resolve, only cache briefly so a transient error cannot stick.
        ttl = FAILED_DOWNLOAD_TTL if len(snippets) < len(video_items) else None
        await self._snippet_cache.set_folder(folder_path, snippets, mode, ttl=ttl)
        return snippets

    async def find_era(self, query: str) -> dict[str, Any] | None:
        # Do not build the complete song-folder index here. That requires
        # browsing every era, which made erafind wait minutes before it could
        # even start its own search.
        if not self._eras_cache:
            self._eras_cache = await self._load_eras()

        target = normalize(query)
        if not target:
            return None

        candidates = []
        for era in self._eras_cache:
            name = normalize(era.get("name", ""))
            score = 1.0 if name == target else 0.0
            if not score and (target in name or name in target):
                score = 0.9
            if not score:
                score = difflib.SequenceMatcher(None, target, name).ratio()
            candidates.append((score, era))
        candidates.sort(key=lambda entry: entry[0], reverse=True)
        if candidates and candidates[0][0] >= FUZZY_THRESHOLD:
            return candidates[0][1]
        return None

    async def fetch_era_song_page(
        self,
        song_dirs: list[dict[str, Any]],
        page: int,
    ) -> list[dict[str, Any]]:
        """Load every video snippet for a single song (one song per message)."""
        index = page - 1
        if index < 0 or index >= len(song_dirs):
            return []

        folder = song_dirs[index]
        song_name = clean_text(folder.get("name")) or "Unknown Song"
        entries = await self.fetch_folder_snippets(folder.get("path", ""))
        for entry in entries:
            entry["song_name"] = song_name
        return entries

    @commands.command(name="snip", aliases=["snippets", "snippet"])
    @commands.cooldown(1, 4, commands.BucketType.user)
    async def snip(self, ctx: commands.Context, *, song: str = ""):
        if not song.strip():
            await ctx.send(
                view=simple_view(
                    f"# 🔍 snip\n"
                    f"-# Browse Juice WRLD video snippets by song.\n\n"
                    f"> **Syntax** `{PREFIX}snip <song>`\n"
                    f"> **Example** `{PREFIX}snip wraith`"
                )
            )
            return

        searching_msg = await ctx.send(view=simple_view(f"🔍 Searching snippets for **{song}**..."))
        matching_folders = await self.find_matching_folders(song)
        if not matching_folders:
            await searching_msg.edit(view=simple_view(f"❌ No snippet folders found matching **{song}**."))
            return

        if len(matching_folders) == 1:
            chosen = matching_folders[0]
            folder_name = chosen["name"]
            files = await self.fetch_folder_snippets(chosen["path"])
            song = await self.fetch_song_metadata(folder_name)
            result_view = SnippetView(
                song_title=folder_name,
                era_name=chosen["era"],
                files=files,
                author_id=ctx.author.id,
                song=song or folder_as_song(folder_name),
                mode=file_mode.get_mode(self.bot),
            )
            result_view.message = searching_msg
            await searching_msg.edit(view=result_view)
            return

        dropdown_view = SongSelectView(
            query=song,
            candidate_folders=matching_folders,
            author_id=ctx.author.id,
            cog=self,
        )
        await searching_msg.edit(view=dropdown_view)
        dropdown_view.message = searching_msg

    @commands.command(name="erafind")
    @commands.cooldown(1, 4, commands.BucketType.user)
    async def erafind(self, ctx: commands.Context, *, era: str = ""):
        if not era.strip():
            await ctx.send(
                view=simple_view(
                    f"# 🔍 erafind\n"
                    f"-# Post every song's snippets for an era as its own message.\n"
                    f"-# Each song gets a full snippet page, just like `{PREFIX}snip`.\n\n"
                    f"> **Syntax** `{PREFIX}erafind <era>`\n"
                    f"> **Example** `{PREFIX}erafind goodbye and good riddance`"
                )
            )
            return

        searching_msg = await ctx.send(view=simple_view(f"🔍 Searching snippets in **{era}**..."))
        era_item = await self.find_era(era)
        if not era_item:
            await searching_msg.edit(view=simple_view(f"❌ No snippet era found matching **{era}**."))
            return

        era_items = await self.browse_path(era_item.get("path", ""))
        era_name = era_item.get("name", era)
        # Drop folders the listing already reports as empty so every song we
        # post actually has something to show.
        song_dirs = sorted(
            (
                item
                for item in era_items
                if item.get("type") == "directory" and folder_has_items(item)
            ),
            key=lambda item: clean_text(item.get("name")).lower(),
        )
        if not song_dirs:
            await searching_msg.edit(
                view=simple_view(f"❌ No snippet songs found in **{era_name}**.")
            )
            return

        mode = file_mode.get_mode(self.bot)
        await searching_msg.edit(
            view=simple_view(
                f"# 🎬 {era_name}\n"
                f"-# Posting snippets for **{len(song_dirs)}** song(s), one message each..."
            )
        )

        posted = 0
        skipped = 0
        stopped = False
        for index in range(1, len(song_dirs) + 1):
            try:
                entries = await self.fetch_era_song_page(song_dirs, index)
            except Exception as exc:
                logger.warning("[Snippets] erafind failed for page %d: %s", index, exc)
                skipped += 1
                continue

            if not entries:
                skipped += 1
                continue

            song_name = clean_text(song_dirs[index - 1].get("name")) or "Unknown Song"
            view = SnippetView(
                song_title=song_name,
                era_name=era_name,
                files=entries,
                author_id=ctx.author.id,
                mode=mode,
            )
            try:
                await ctx.send(view=view)
            except discord.HTTPException as exc:
                logger.warning(
                    "[Snippets] erafind stopped after posting %d song(s): %s", posted, exc
                )
                stopped = True
                break
            posted += 1

        summary = f"# 🎬 {era_name}\n-# Posted **{posted}** song page(s)"
        if skipped:
            summary += f" · skipped **{skipped}** with no snippets"
        summary += (
            ".\n-# Stopped early — check my send permissions in this channel."
            if stopped
            else "."
        )
        try:
            await searching_msg.edit(view=simple_view(summary))
        except discord.HTTPException:
            pass

    # ---------------------------------------------------------------
    # ,cachereload
    # ---------------------------------------------------------------

    def _register_cache_provider(self) -> None:
        """Offer the snippet manifest to ``,cachereload``."""
        cache_registry.register(
            CacheProvider(
                name="snippets",
                label="Snippet videos",
                aliases=("snip", "videos", "snippet"),
                describe=self._describe_snippet_cache,
                reload=self._reload_snippet_cache,
            )
        )

    def _describe_snippet_cache(self) -> str:
        files = 0
        try:
            files = sum(1 for path in SNIPPET_CACHE_DIR.iterdir() if path.is_file())
        except OSError:
            pass
        return f"{self._snippet_cache.size} cached folder(s) · {files} file(s)"

    @commands.command(
        name="cachereload",
        aliases=("reloadcache", "cacheclear", "recache"),
    )
    @commands.check_any(
        commands.is_owner(), commands.has_permissions(manage_guild=True)
    )
    async def cachereload(self, ctx: commands.Context, *, target: str = "") -> None:
        """Rebuild the bot's caches, re-downloading whatever has gone missing.

        ``{PREFIX}cachereload`` forgets and re-resolves every cache the bot
        keeps: snippet videos, cover art, the mixtape listing and the lyric
        index. ``{PREFIX}cachereload all`` rebuilds the whole catalog behind the
        caches rather than only the entries that are cached right now, and
        ``{PREFIX}cachereload <name>`` re-resolves just the entries matching
        that name (or names one cache outright: ``covers``, ``snippets``,
        ``mixtapes``, ``lyrics``).

        Those caches are kept forever, so media deleted behind them goes on
        being handed out -- including a bucket object that no longer exists.
        This forgets the entries (in memory and in the manifest) and
        re-resolves them, which downloads whatever is missing and re-hosts
        every entry. The media files already on disk are never deleted, so a
        reload re-uses them instead of starting over.
        """
        if self._reload_task and not self._reload_task.done():
            await ctx.send(
                view=simple_view("⏳ A cache reload is already running. Let it finish first.")
            )
            return

        available = cache_registry.providers()
        if not available:
            await ctx.send(
                view=simple_view("# ❌ No cache is registered right now — nothing to reload.")
            )
            return

        wanted = target.strip()
        lowered = wanted.casefold()
        single = cache_registry.match(wanted)
        full = lowered in {"all", "everything", "full", "catalog"}
        if single is not None:
            selected = [single]
            query: Optional[str] = None
            title = f"♻️ Reloading the {single.label} cache"
            scope_line = "-# Only the cached entries are dropped, then re-resolved."
        elif not wanted or full:
            selected = available
            query = None
            title = (
                "♻️ Rebuilding every cache"
                if full
                else "♻️ Reloading every cache"
            )
            scope_line = (
                "-# Every cache is rebuilt over its whole catalog."
                if full
                else "-# Only what is cached right now is dropped and re-resolved."
            )
        else:
            selected = available
            query = wanted
            title = f"♻️ Reloading cached entries for **{wanted}**"
            scope_line = "-# Only the entries matching that name are re-resolved."

        headline = (
            f"# {title}\n"
            f"{cache_registry.summary()}\n"
            f"{scope_line}\n"
            "-# This runs in the background and can take a while."
        )

        message = await ctx.send(view=simple_view(headline))
        self._reload_task = asyncio.create_task(
            self._run_cache_reload(message, selected, full, query),
            name="cache-reload",
        )
        self._reload_task.add_done_callback(self._reload_finished)

    def _reload_finished(self, task: asyncio.Task) -> None:
        if self._reload_task is task:
            self._reload_task = None
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("[Snippets] Cache reload task died", exc_info=error)

    async def _edit_reload_card(
        self, message: Optional[discord.Message], content: str
    ) -> None:
        if message is None:
            return
        try:
            await message.edit(view=simple_view(content))
        except discord.HTTPException:
            pass

    async def _run_cache_reload(
        self,
        message: Optional[discord.Message],
        selected: list[CacheProvider],
        full: bool,
        query: Optional[str],
    ) -> None:
        """Reload each selected cache in the background, one after another.

        Serial on purpose: a reload is download- and upload-heavy, so running
        the caches side by side would only make every one of them slower.
        """
        results: list[ReloadResult] = []
        try:
            for provider in selected:
                async def progress(text: str, provider: CacheProvider = provider) -> None:
                    await self._edit_reload_card(
                        message,
                        f"# ♻️ {provider.label}\n"
                        f"{cache_registry.summary()}\n"
                        f"-# {text}",
                    )

                await progress("Starting...")
                try:
                    results.append(await provider.reload(query, full, progress))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("[Cache] The %s cache failed to reload", provider.name)
                    results.append(
                        ReloadResult(
                            label=provider.label,
                            failed=1,
                            note="raised an error; see the log",
                        )
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[Cache] Cache reload failed")
            results.append(ReloadResult(label="reload", failed=1, note="see the log"))
        await self._edit_reload_card(message, self._reload_summary(results))

    @staticmethod
    def _reload_summary(results: list[ReloadResult]) -> str:
        """One line per cache: what it forgot, what came back, what didn't."""
        if not results:
            return "# ❌ Nothing was reloaded\n-# See the log for the traceback."
        failed = sum(result.failed for result in results)
        lines = []
        for result in results:
            parts = [f"**{result.forgotten}** forgotten"]
            if result.restored or result.empty or result.failed:
                parts.append(f"**{result.restored}** restored")
            if result.empty:
                parts.append(f"**{result.empty}** empty")
            if result.failed:
                parts.append(f"**{result.failed}** failed")
            if result.scope:
                parts.append(f"scope: {result.scope}")
            line = f"> **{result.label}** — " + " · ".join(parts)
            if result.note:
                line += f"\n> -# {result.note}"
            lines.append(line)
        header = (
            "# ✅ Cache reload finished"
            if not failed
            else "# ⚠️ Cache reload finished with errors"
        )
        body = header + "\n" + "\n".join(lines)
        if failed:
            body += "\n-# The failed entries are logged; run the reload again to retry them."
        return body

    async def _reload_snippet_cache(
        self,
        target: Optional[str],
        full: bool,
        progress: cache_registry.ProgressFn,
    ) -> ReloadResult:
        """Forget the requested snippet entries, rebuild the index, re-resolve."""
        result = ReloadResult(label="Snippet videos")

        # 1. Work out what gets re-resolved, forgetting those entries first so
        #    the reload cannot be answered out of the very cache it is fixing.
        if target is None:
            folders: list[str] = self._snippet_cache.cached_folders()
            result.forgotten = len(folders)
            result.scope = (
                "every song folder in the catalog"
                if full
                else "the folders that were cached"
            )
            await self._snippet_cache.clear()
        else:
            matches = await self.find_matching_folders(target)
            if not matches:
                result.note = f"no snippet folder matches {target}"
                return result
            folders = []
            for folder in matches:
                path = clean_text(folder.get("path"))
                if path and await self._snippet_cache.forget_folder(path):
                    folders.append(path)
            result.forgotten = len(folders)
            result.scope = f"the folder(s) matching {target}"
            if not folders:
                result.note = f"nothing cached for {target}"
                return result

        # 2. Drop the in-memory copies as well, so the next read asks the API
        #    instead of replaying what this process already had.
        self._eras_cache = []
        self._song_folders_cache = []
        self._cache_timestamp = 0
        self._song_meta_cache = {}

        await progress("Rebuilding the era and song-folder index...")
        await self._ensure_folder_index()

        if full:
            folders = [
                clean_text(folder.get("path"))
                for folder in self._song_folders_cache
                if clean_text(folder.get("path"))
            ]

        total = len(folders)
        if not total:
            result.note = (
                f"index rebuilt over {len(self._song_folders_cache)} song "
                "folder(s); there was nothing cached to reload"
            )
            return result

        last_edit = 0.0
        for index, path in enumerate(folders, start=1):
            try:
                entries = await self.fetch_folder_snippets(path)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[Snippets] Reload failed for '%s': %s", path, exc)
                result.failed += 1
            else:
                if entries:
                    result.restored += 1
                else:
                    result.empty += 1

            now = time.time()
            if index == total or now - last_edit >= RELOAD_PROGRESS_SECONDS:
                last_edit = now
                await progress(
                    f"Reloaded **{index}/{total}** folder(s) · "
                    f"**{result.restored}** with snippets"
                )

        logger.info(
            "[Snippets] Cache reload finished: %d restored, %d empty, %d failed (%s)",
            result.restored,
            result.empty,
            result.failed,
            result.scope,
        )
        result.note = (
            f"index: {len(self._song_folders_cache)} song folder(s) across "
            f"{len(self._eras_cache)} era(s)"
        )
        return result

    @snip.error
    async def snip_error(self, ctx: commands.Context, error: commands.CommandError):
        await self._command_error(ctx, error)

    @erafind.error
    async def erafind_error(self, ctx: commands.Context, error: commands.CommandError):
        await self._command_error(ctx, error)

    async def _command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, (SendHelp, *USAGE_ERRORS)):
            if ctx.command:
                return await send_usage_help(ctx)
        await send_command_error(
            ctx,
            error,
            log_level=logging.INFO if isinstance(error, commands.CommandOnCooldown) else logging.ERROR,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Snippets(bot))
