# cogs/covers.py

import asyncio
import difflib
import hashlib
import html
import json
import logging
import os
import re
import time
from urllib.parse import quote, unquote
from typing import Any, Optional, Sequence

import aiohttp
import discord
from discord.ext import commands

from config import EMBED_COLOR
from utils.core import cache_registry, file_mode, media_cache
from utils.core.cache_registry import CacheProvider, ReloadResult
from utils.core.media_cache import COVER_CACHE_DIR
from utils.core.prefixes import prefix_for

logger = logging.getLogger("slimey.covers")

# ============================================================
# CONFIGURATION & CONSTANTS
# ============================================================

API_ROOT = "https://juicewrldapi.com"
API_BASE = f"{API_ROOT}/juicewrld"
BROWSE_ENDPOINT = f"{API_BASE}/files/browse/"
DOWNLOAD_ENDPOINT = f"{API_BASE}/files/download/"
JUICEVAULT_BASE = "https://api.juicevault.xyz"

EMBED_COLOR = discord.Color.from_str("#2b2d31")
TIMEOUT = aiohttp.ClientTimeout(total=45)
MAX_SEARCH_RESULTS = 50
MAX_ATTACHED_COVERS = 10
COVER_DOWNLOAD_CONCURRENCY = 10
COVER_DOWNLOAD_TIMEOUT = 15
COVER_CACHE_MAX_BYTES = 512 * 1024 * 1024
COVER_FUZZY_MIN = 0.85
# How long the Cover Arts index stays warm before it is rebuilt.
COVERS_INDEX_TTL = 6 * 60 * 60
SEARCH_CACHE_TTL = 300
SONG_CACHE_TTL = 900
# The resolved-per-song cover cache is kept forever, so a repeated request
# (even after a restart) skips the Cover Arts index scan, the JuiceVault
# fallback and the downloads entirely. It lives beside the image directory
# rather than inside it so the media route can never serve the index itself.
COVER_CACHE_MANIFEST = COVER_CACHE_DIR.with_suffix(".json")
# Bumped to 2 when covers started being uploaded to the bucket: the version 1
# index was written with public URLs for files that were never actually put in
# the bucket, so those records have to be re-resolved (and uploaded).
COVER_CACHE_VERSION = 2
# A song with no artwork is re-checked once the index would have been rebuilt.
COVER_MISS_TTL = COVERS_INDEX_TTL
#: Shortest gap between two progress edits of the `cachereload` card. Discord
#: rate-limits message edits, so the reload reports on a timer rather than
#: once per song.
COVER_RELOAD_PROGRESS_SECONDS = 3.0

IMAGE_EXTENSIONS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
)

# Fields on a song record that can carry alternate titles (the API exposes
# e.g. ``track_titles``, where a song may be known under another name).
ALT_TITLE_KEYS = (
    "track_titles",
    "trackTitles",
    "alternative_titles",
    "alternativeTitles",
    "alternate_titles",
    "alternateTitles",
    "alt_names",
    "altNames",
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}


# ============================================================
# HELPERS & SANITIZATION
# ============================================================

def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def clean_title(title: str) -> str:
    """Strips versions, metadata tags, brackets, and features[span_9](start_span)[span_9](end_span)[span_10](start_span)[span_10](end_span)."""
    clean = re.sub(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}", "", title)
    clean = re.sub(r"\s+(?:feat|ft|prod)\.?\s+.*$", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"[()\[\]{}]", "", clean)
    return re.sub(r"\s+", " ", clean).strip() or title


def normalize(value: str) -> str:
    if not value:
        return ""
    val = clean_title(html.unescape(unquote(str(value)))).lower()
    return re.sub(r"[^\w\s]", " ", val).strip()


def cover_match_score(target_norm: str, name_norm: str) -> float:
    """Score how confidently a cover file name matches the searched song title.

    Returns ``0.0`` for anything that isn't a solid match so unrelated tracks
    with vaguely similar names (e.g. searching "on time" and matching "once
    upon a time") never end up in the results.
    """
    if not target_norm or not name_norm:
        return 0.0
    if target_norm == name_norm:
        return 1.0

    target_words = set(target_norm.split())
    name_words = set(name_norm.split())
    if not target_words or not name_words:
        return 0.0
    if target_words == name_words:
        return 0.99

    # The searched title appears as a whole phrase inside the cover name,
    # e.g. "on time" -> "on time og" / "on time remix". Matching the full
    # phrase (not just the word set) keeps look-alikes like "on my time" out.
    if re.search(rf"(?<!\w){re.escape(target_norm)}(?!\w)", name_norm):
        return 0.90 + 0.05 * (len(target_words) / len(name_words))

    # Otherwise only accept a strong fuzzy match (typos / small variations),
    # so a cover named after a *different* shorter song (e.g. searching
    # "on time" and matching "time") is rejected rather than mixed in.
    ratio = difflib.SequenceMatcher(None, target_norm, name_norm).ratio()
    if ratio < COVER_FUZZY_MIN:
        return 0.0
    return ratio


def dedupe_titles(titles: Sequence[str]) -> list[str]:
    """Keep the first occurrence of each title after normalization."""
    unique: list[str] = []
    seen: set[str] = set()
    for raw in titles:
        text = clean_text(raw)
        if not text or text.upper() == "N/A":
            continue
        key = normalize(text)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(text)
    return unique


def song_alt_names(song: dict[str, Any]) -> list[str]:
    """Every alternate title a song is known by, excluding its primary name."""
    names: list[str] = []
    for key in ALT_TITLE_KEYS:
        value = song.get(key)
        if not value:
            continue
        values = value if isinstance(value, (list, tuple, set)) else [value]
        for entry in values:
            if isinstance(entry, str) and entry.strip():
                names.append(entry.strip())

    primary = normalize(clean_text(song.get("name") or song.get("title") or ""))
    return [name for name in dedupe_titles(names) if normalize(name) != primary]


def song_search_score(song: dict[str, Any], query_norm: str) -> int:
    """Rank a search result against the query; lower is a better match.

    Exact primary-title matches come first, then exact alternate-title
    matches, then partial matches — so searching an alt name (e.g. "time"
    for "Charcoal") still surfaces the song near the top of the dropdown.
    """
    if not query_norm:
        return 4

    primary = normalize(clean_text(song.get("name") or song.get("title") or ""))
    alts = [normalize(name) for name in song_alt_names(song)]

    if primary == query_norm:
        return 0
    if query_norm in alts:
        return 1

    phrase = rf"(?<!\w){re.escape(query_norm)}(?!\w)"
    if primary and re.search(phrase, primary):
        return 2
    if any(re.search(phrase, alt) for alt in alts if alt):
        return 3
    return 4


class CoverCache:
    """Persistent per-song cover cache kept as one JSON file beside the images.

    Each record holds the resolved cover entries, the file mode they were
    resolved under and when, so a lookup can be answered without touching the
    Cover Arts index, JuiceVault or the network. Records whose local files have
    gone missing are ignored, which makes the cache self-healing.
    """

    def __init__(self, path: Any = COVER_CACHE_MANIFEST) -> None:
        self.path = path
        self._records: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        try:
            if self.path.is_file():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("version") != COVER_CACHE_VERSION:
                    logger.info(
                        "[Covers] Discarding the cover cache index from an older version (%s)",
                        data.get("version"),
                    )
                    return
                if isinstance(data, dict) and isinstance(data.get("songs"), dict):
                    self._records = {
                        str(key): value
                        for key, value in data["songs"].items()
                        if isinstance(value, dict)
                    }
        except Exception:
            logger.exception("[Covers] Could not read the cover cache index")
            self._records = {}

    def get(self, key: str) -> Optional[dict[str, Any]]:
        record = self._records.get(key)
        return dict(record) if isinstance(record, dict) else None

    @property
    def size(self) -> int:
        return len(self._records)

    async def set(
        self,
        key: str,
        covers: list[dict[str, Any]],
        mode: str,
        *,
        titles: Optional[Sequence[str]] = None,
        path: str = "",
    ) -> None:
        async with self._lock:
            self._records[key] = {
                "fetched_at": time.time(),
                "mode": mode,
                "covers": covers,
                # Remembering what the key was built from lets a reload
                # re-resolve the record exactly, rather than guessing the
                # titles back out of the joined key.
                "titles": [str(title) for title in (titles or ())],
                "path": str(path or ""),
            }
            self._save_locked()

    def keys(self) -> list[str]:
        """Every cached song key, for a reload."""
        return list(self._records)

    async def forget(self, key: str) -> bool:
        """Drop one song's record, leaving the rest of the cache alone."""
        async with self._lock:
            if key not in self._records:
                return False
            del self._records[key]
            self._save_locked()
            return True

    async def clear(self) -> int:
        """Drop every record in one write, returning how many there were.

        Used by ``,cachereload``: the cover images themselves are left on disk
        and in the bucket, so re-resolving re-uses them and only downloads
        what is actually missing.
        """
        async with self._lock:
            count = len(self._records)
            self._records = {}
            self._save_locked()
            return count

    def _save_locked(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(
                    {"version": COVER_CACHE_VERSION, "songs": self._records},
                    indent=2,
                ),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        except Exception:
            logger.exception("[Covers] Could not write the cover cache index")


def make_download_url(path: str) -> str:
    clean_p = path.strip().lstrip("/")
    return f"{DOWNLOAD_ENDPOINT}?path={quote(clean_p, safe='')}"


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


def format_era(value: Any) -> str:
    if not value:
        return "N/A"
    if isinstance(value, dict):
        name = value.get("name") or value.get("title") or value.get("value")
        if name:
            return clean_text(name)
        desc = value.get("description")
        if desc:
            return clean_text(desc)
        return "N/A"
    if isinstance(value, str):
        val = value.strip()
        return val if val else "N/A"
    return str(value)


def format_artist(song: dict[str, Any]) -> str:
    val = (
        song.get("credited_artists")
        or song.get("CreditedArtists")
        or song.get("artist")
        or song.get("artists")
    )
    if isinstance(val, list):
        items = []
        for item in val:
            if isinstance(item, dict):
                name = item.get("name") or item.get("title")
                if name:
                    items.append(str(name).strip())
            elif item:
                items.append(str(item).strip())
        if items:
            return ", ".join(items)
    elif isinstance(val, dict):
        return str(val.get("name") or val.get("title") or "Juice WRLD").strip()
    elif isinstance(val, str) and val.strip():
        return val.strip()

    return "Juice WRLD"


def format_producers(song: dict[str, Any]) -> str:
    val = (
        song.get("producers")
        or song.get("Producers")
        or song.get("producer")
        or song.get("producer_names")
        or song.get("producerNames")
    )
    if isinstance(val, list):
        items = []
        for item in val:
            if isinstance(item, dict):
                name = item.get("name") or item.get("title")
                if name:
                    items.append(str(name).strip())
            elif item:
                items.append(str(item).strip())
        if items:
            return ", ".join(items)
    elif isinstance(val, dict):
        return str(val.get("name") or val.get("title") or "N/A").strip()
    elif isinstance(val, str) and val.strip():
        return val.strip()

    return "N/A"


# ============================================================
# COMPONENTS V2: COVER RESULT VIEW
# ============================================================

UNKNOWN_ARTIST = "Unknown Artist"


class CoverResultView(discord.ui.LayoutView):
    def __init__(
        self,
        song: dict[str, Any],
        covers: list[dict[str, Any]],
        timeout: float = 300,
    ):
        super().__init__(timeout=timeout)
        self.song = song
        self.covers = covers
        self._render()

    def _render(self) -> None:
        song = self.song
        title = clean_text(song.get("name") or song.get("title") or "Unknown Song")
        era = format_era(song.get("era") or song.get("Era"))
        artist = format_artist(song)
        producers = format_producers(song)

        container = discord.ui.Container(accent_color=EMBED_COLOR)

        if not self.covers:
            container.add_item(
                discord.ui.TextDisplay(
                    f"# {title}\n\n"
                    f"**Era:** {era}\n"
                    f"**Artist:** {artist}\n"
                    f"**Producer(s):** {producers}\n\n"
                    f"❌ No cover art found for this track."
                )
            )
            self.add_item(container)
            return

        header_text = (
            f"# {title}\n\n"
            f"**Era:** {era}\n"
            f"**Artist:** {artist}\n"
            f"**Producer(s):** {producers}\n"
            f"**Covers Found:** {len(self.covers)}"
        )

        container.add_item(discord.ui.TextDisplay(header_text))
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))

        gallery_urls = [
            discord.MediaGalleryItem(
                media=cover["media_url"],
                description=cover.get("name"),
            )
            for cover in self.covers
            if cover.get("media_url")
        ]
        container.add_item(discord.ui.MediaGallery(*gallery_urls[:10]))

        self.add_item(container)


# ============================================================
# COMPONENTS V2: SELECTION DROPDOWN VIEW
# ============================================================

class SongSelect(discord.ui.Select):
    def __init__(self, songs: list[dict[str, Any]], author_id: int, cog: "Covers"):
        self.songs = songs
        self.author_id = author_id
        self.cog = cog

        options = []
        for idx, song in enumerate(songs[:25]):
            title = clean_text(song.get("name") or song.get("title") or "Unknown Song")[:100]
            era = format_era(song.get("era") or song.get("Era"))
            alt = ", ".join(song_alt_names(song))

            parts = []
            if era and era != "N/A":
                parts.append(f"Era: {era}")
            if alt:
                parts.append(f"Alt: {alt}")

            desc = " • ".join(parts)[:100] or None

            options.append(
                discord.SelectOption(
                    label=title,
                    value=str(idx),
                    description=desc,
                )
            )

        super().__init__(
            placeholder="Choose the correct song...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Only the person who ran the command can choose a song.",
                ephemeral=True,
            )
            return

        if self.view:
            self.view.selected = True
            self.view.stop()

        await interaction.response.defer()

        chosen_song = self.songs[int(self.values[0])]
        song_id = chosen_song.get("id") or chosen_song.get("song_id") or chosen_song.get("public_id")

        if song_id is not None:
            full_song = await self.cog.get_song_details(song_id)
            if full_song and isinstance(full_song, dict):
                chosen_song = full_song

        title = chosen_song.get("name") or chosen_song.get("title") or ""
        path = chosen_song.get("path") or ""

        covers = await self.cog.fetch_song_covers(
            title=title,
            path=path,
            alt_names=song_alt_names(chosen_song),
        )
        view = CoverResultView(chosen_song, covers)
        await interaction.edit_original_response(view=view)


class SongSelectView(discord.ui.LayoutView):
    def __init__(self, query: str, songs: list[dict[str, Any]], author_id: int, cog: "Covers"):
        super().__init__(timeout=120)
        self.selected = False
        self.message: Optional[discord.Message] = None

        shown = min(len(songs), 25)
        overflow = (
            f"\nOnly the top **{shown}** are shown — add more of the title to narrow it down."
            if len(songs) > shown
            else ""
        )
        container = discord.ui.Container(accent_color=EMBED_COLOR)
        container.add_item(
            discord.ui.TextDisplay(
                f"# Cover Art Search\n"
                f"Found **{len(songs)}** matches for **{query}**.{overflow}\n"
                f"Select the exact song from the menu below:"
            )
        )
        container.add_item(
            discord.ui.ActionRow(
                SongSelect(songs, author_id, cog)
            )
        )
        self.add_item(container)

    async def on_timeout(self):
        if self.selected:
            return

        if self.message:
            try:
                timeout_view = discord.ui.LayoutView(timeout=10)
                container = discord.ui.Container(accent_color=EMBED_COLOR)
                container.add_item(
                    discord.ui.TextDisplay("⏱️ Selection timed out. Please run the command again.")
                )
                timeout_view.add_item(container)
                await self.message.edit(view=timeout_view)
            except Exception:
                pass


# ============================================================
# COVERS COG
# ============================================================

class Covers(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self.session_lock = asyncio.Lock()

        # Cache for Cover Arts directory index
        self._covers_cache: list[dict[str, Any]] = []
        self._covers_cache_timestamp: float = 0
        self._indexing_lock = asyncio.Lock()
        self._cover_download_semaphore = asyncio.Semaphore(COVER_DOWNLOAD_CONCURRENCY)
        self._cover_cache_locks: dict[str, asyncio.Lock] = {}

        # Exact normalized-title lookup over the index so a normal search never
        # has to fuzzy-score every file.
        self._covers_by_norm: dict[str, list[dict[str, Any]]] = {}
        # Short-lived result caches so repeating a command is instant.
        self._search_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._song_cache: dict[str, tuple[float, Optional[dict[str, Any]]]] = {}
        # Resolved covers survive restarts; see CoverCache.
        self._cover_cache = CoverCache()

    async def cog_load(self):
        self._register_cache_provider()
        # Pre-warm the cover arts index in the background on startup
        asyncio.create_task(self._ensure_covers_index())

    async def get_session(self) -> aiohttp.ClientSession:
        async with self.session_lock:
            if self.session and not self.session.closed:
                return self.session

            self.session = aiohttp.ClientSession(
                timeout=TIMEOUT,
                headers=HEADERS,
            )
            return self.session

    def cog_unload(self):
        cache_registry.unregister("covers")

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
        params = {"path": path, "page_size": 2000}

        try:
            async with session.get(BROWSE_ENDPOINT, params=params, headers=HEADERS) as resp:
                if resp.status != 200:
                    return []
                text = await resp.text()
                data = parse_api_json(text)
                if isinstance(data, dict):
                    return data.get("items", [])
                return []
        except Exception as exc:
            logger.warning("[Covers] Browse failed on path '%s': %s", path, exc)
            return []

    async def _ensure_covers_index(self):
        """Indexes all artist directories inside 'Cover Arts' and collects every image file[span_12](start_span)[span_12](end_span)."""
        now = asyncio.get_running_loop().time()
        if self._covers_cache and (now - self._covers_cache_timestamp < COVERS_INDEX_TTL):
            return

        async with self._indexing_lock:
            if self._covers_cache and (now - self._covers_cache_timestamp < COVERS_INDEX_TTL):
                return

            logger.info("[Cover Arts] Indexing all subfolders in 'Cover Arts'...")
            top_items = await self.browse_path("Cover Arts")
            artist_folders = [item for item in top_items if item.get("type") == "directory"]

            semaphore = asyncio.Semaphore(15)

            async def fetch_artist(artist_item):
                async with semaphore:
                    items = await self.browse_path(artist_item["path"])
                    return artist_item.get("name", "Unknown"), items

            tasks = [fetch_artist(a) for a in artist_folders]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            all_covers = []
            for res in results:
                if isinstance(res, tuple):
                    artist_name, items = res
                    if isinstance(items, list):
                        for file_item in items:
                            name = file_item.get("name", "")
                            ext = str(file_item.get("extension") or "").lower()
                            if not ext and "." in name:
                                ext = f".{name.rsplit('.', 1)[-1].lower()}"

                            if ext in IMAGE_EXTENSIONS:
                                base_name = name.rsplit(".", 1)[0]
                                all_covers.append({
                                    "name": name,
                                    "path": file_item.get("path", ""),
                                    "artist": artist_name,
                                    "extension": ext.lstrip("."),
                                    "name_norm": normalize(base_name),
                                })

            if all_covers:
                by_norm: dict[str, list[dict[str, Any]]] = {}
                for item in all_covers:
                    norm = item.get("name_norm") or ""
                    if norm:
                        by_norm.setdefault(norm, []).append(item)

                self._covers_cache = all_covers
                self._covers_by_norm = by_norm
                self._covers_cache_timestamp = now
                logger.info("[Cover Arts] Indexed %d cover art files across %d artist folders.", len(all_covers), len(artist_folders))

    def _score_cover_candidates(
        self,
        target_norm: str,
        *,
        exact_only: bool = False,
    ) -> list[tuple[float, dict[str, Any]]]:
        """Score every indexed cover file against one normalized title."""
        # Fast path: an exact title match is by far the common case, and the
        # prebuilt map answers it without touching the whole index.
        exact = self._covers_by_norm.get(target_norm)
        if exact:
            return [(1.0, item) for item in exact]
        if exact_only:
            return []

        scored: list[tuple[float, dict[str, Any]]] = []
        for item in self._covers_cache:
            name_norm = item.get("name_norm")
            if not name_norm:
                base_name = str(item.get("name", "")).rsplit(".", 1)[0]
                name_norm = normalize(base_name)
            score = cover_match_score(target_norm, name_norm)
            if score > 0:
                scored.append((score, item))
        return scored

    async def find_covers_in_folders(
        self, titles: str | Sequence[str]
    ) -> list[dict[str, Any]]:
        """Find 'Cover Arts' files for a song's name and its alternate names.

        The primary title uses the full exact/phrase/fuzzy matcher. Alternate
        names (e.g. ``Charcoal`` -> ``Time``) only accept an exact match, since
        they are often common words that appear inside other songs' titles.
        """
        await self._ensure_covers_index()

        title_list = [titles] if isinstance(titles, str) else list(titles)

        # path -> (score, title rank, item); the best score wins, and ties are
        # resolved in favour of the primary title.
        found: dict[str, tuple[float, int, dict[str, Any]]] = {}
        for rank, title in enumerate(title_list):
            target_norm = normalize(title)
            if not target_norm:
                continue

            for score, item in self._score_cover_candidates(
                target_norm, exact_only=rank > 0
            ):
                key = str(item.get("path") or item.get("name"))
                current = found.get(key)
                if (
                    current is None
                    or score > current[0]
                    or (score == current[0] and rank < current[1])
                ):
                    found[key] = (score, rank, item)

        if not found:
            return []

        # Best matches first so the correct artwork is never crowded out by
        # lower-confidence look-alikes when only the top N are shown.
        ordered = sorted(found.values(), key=lambda entry: (-entry[0], entry[1]))
        return [item for _, _, item in ordered]

    async def search_songs(self, query: str) -> list[dict[str, Any]]:
        key = (query or "").strip().lower()
        now = asyncio.get_running_loop().time()
        cached = self._search_cache.get(key)
        if cached and now - cached[0] < SEARCH_CACHE_TTL:
            return cached[1]

        results = await self._search_songs_request(query)
        if results:
            # The API also searches alternate titles, which pushes a song
            # further down the list; re-rank so the closest title wins.
            query_norm = normalize(query)
            results = sorted(results, key=lambda song: song_search_score(song, query_norm))
            self._search_cache[key] = (now, results)
        return results

    async def _search_songs_request(self, query: str) -> list[dict[str, Any]]:
        session = await self.get_session()
        url = f"{API_BASE}/songs/"
        params = {"search": query, "page_size": MAX_SEARCH_RESULTS}

        try:
            async with session.get(url, params=params, headers=HEADERS, timeout=TIMEOUT) as response:
                if response.status != 200:
                    return []
                text = await response.text()
                data = parse_api_json(text)

                if isinstance(data, list):
                    return [x for x in data if isinstance(x, dict)]
                if isinstance(data, dict):
                    for key in ("results", "songs", "data", "items"):
                        items = data.get(key)
                        if isinstance(items, list):
                            return [x for x in items if isinstance(x, dict)]
                return []
        except Exception as exc:
            logger.error("[Search] Error searching songs: %s", exc)
            return []

    async def get_song_details(self, song_id: Any) -> Optional[dict[str, Any]]:
        key = str(song_id)
        now = asyncio.get_running_loop().time()
        cached = self._song_cache.get(key)
        if cached and now - cached[0] < SONG_CACHE_TTL:
            return cached[1]

        details = await self._get_song_details_request(song_id)
        if details is not None:
            self._song_cache[key] = (now, details)
        return details

    async def _get_song_details_request(self, song_id: Any) -> Optional[dict[str, Any]]:
        session = await self.get_session()
        for endpoint in (f"songs/{song_id}/", f"songs/{song_id}"):
            url = f"{API_BASE}/{endpoint}"
            try:
                async with session.get(url, headers=HEADERS, timeout=15) as response:
                    if response.status == 200:
                        text = await response.text()
                        data = parse_api_json(text)
                        if isinstance(data, dict):
                            song_obj = data.get("song") or data.get("data") or data
                            if isinstance(song_obj, dict):
                                if "path" not in song_obj and "path" in data:
                                    song_obj["path"] = data["path"]
                                return song_obj
                            return data
            except Exception:
                continue
        return None

    async def _cache_cover(
        self,
        source_url: str,
        *,
        ext: str,
        artist: str,
        name: str,
    ) -> dict[str, Any] | None:
        """Download a cover, host it, and return its public URL.

        The file has to actually be uploaded to the bucket before its public
        URL means anything — the snippet cache does the same via
        ``media_cache.ensure_media``. Skipping the upload left every cover link
        pointing at an object that was never written, so all of them 404'd.
        """
        filename = hashlib.sha256(source_url.encode("utf-8")).hexdigest() + f".{ext}"
        cached_path = COVER_CACHE_DIR / filename
        file_lock = self._cover_cache_locks.setdefault(filename, asyncio.Lock())
        async with file_lock:
            if not cached_path.is_file():
                try:
                    COVER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    async with self._cover_download_semaphore:
                        async with (await self.get_session()).get(
                            source_url,
                            headers=HEADERS,
                            timeout=aiohttp.ClientTimeout(total=COVER_DOWNLOAD_TIMEOUT),
                        ) as resp:
                            if resp.status != 200:
                                logger.warning(
                                    "[Covers] Cover download failed for '%s' (HTTP %s)",
                                    source_url,
                                    resp.status,
                                )
                                return None
                            raw = await resp.read()
                    if len(raw) <= 100 or raw[:5].lower() in {b"<!doc", b"<html"}:
                        logger.warning("[Covers] Cover download returned invalid media for '%s'", source_url)
                        return None
                    temporary_path = cached_path.with_suffix(cached_path.suffix + ".part")
                    temporary_path.write_bytes(raw)
                    temporary_path.replace(cached_path)
                except Exception as exc:
                    logger.warning("[Covers] Cover download failed for '%s': %s", source_url, exc)
                    return None

        local_url = await media_cache.ensure_media("Covers", filename, cached_path)
        if not local_url:
            logger.warning("[Covers] Could not host cover '%s'", source_url)
            return None

        return {
            "local_url": local_url,
            "media_url": local_url,
            "ext": ext,
            "artist": artist or UNKNOWN_ARTIST,
            "name": name,
            # Recorded so the cover cache can verify the file is still there.
            "file": filename,
        }

    def _direct_cover(
        self,
        source_url: str,
        *,
        ext: str,
        artist: str,
        name: str,
    ) -> dict[str, Any]:
        """Use the original source URL without downloading or caching it."""
        return {
            "media_url": source_url,
            "ext": ext,
            "artist": artist or UNKNOWN_ARTIST,
            "name": name,
        }

    async def _cover_entry(
        self,
        source_url: str,
        *,
        ext: str,
        artist: str,
        name: str,
    ) -> dict[str, Any] | None:
        """Resolve one cover according to the active file mode.

        Falls back to the original direct links whenever the bucket can't host
        the file, so the command keeps working on a bot with no R2 setup.
        """
        if file_mode.get_mode(self.bot) != file_mode.LOCAL:
            return self._direct_cover(source_url, ext=ext, artist=artist, name=name)
        if not media_cache.hosting_available():
            return self._direct_cover(source_url, ext=ext, artist=artist, name=name)
        return await self._cache_cover(source_url, ext=ext, artist=artist, name=name)

    def _current_mode(self) -> str:
        return file_mode.get_mode(getattr(self, "bot", None))

    def cached_song_covers(self, key: str) -> Optional[list[dict[str, Any]]]:
        """Cached covers for a normalized title key, or None on a cache miss.

        Returns an empty list for a cached "no artwork" answer while that miss
        is still fresh.
        """
        record = self._cover_cache.get(key)
        if record is None:
            return None

        covers = record.get("covers")
        if not isinstance(covers, list):
            return None

        if not covers:
            age = time.time() - float(record.get("fetched_at") or 0)
            return [] if age < COVER_MISS_TTL else None

        # Re-resolve if the file mode changed (direct URLs vs cached files).
        if record.get("mode") != self._current_mode():
            return None

        for cover in covers:
            filename = cover.get("file")
            if filename and not (COVER_CACHE_DIR / str(filename)).is_file():
                logger.info("[Covers] Cached file for '%s' is gone; re-resolving", key)
                return None
        return [dict(cover) for cover in covers]

    async def fetch_song_covers(
        self,
        title: str,
        path: str = "",
        alt_names: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        """Return covers for a song, answering from the permanent cache when possible.

        A song may only have artwork filed under an alternate title (e.g.
        "Charcoal" is also known as "Time"), so every known name is searched,
        and the result is stored under all of them for future requests.
        """
        titles = dedupe_titles([title, *alt_names])
        key = "|".join(normalize(name) for name in titles)
        if not key:
            return await self._fetch_song_covers_uncached(title, path, titles[1:])

        cached = self.cached_song_covers(key)
        if cached is not None:
            logger.debug("[Covers] Cache hit for '%s'", key)
            return cached

        covers = await self._fetch_song_covers_uncached(title, path, titles[1:])
        await self._cover_cache.set(
            key, covers, self._current_mode(), titles=titles, path=path
        )
        return covers

    async def _fetch_song_covers_uncached(
        self,
        title: str,
        path: str = "",
        alt_names: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        """Search for covers, cache valid results, and return public local URLs."""
        session = await self.get_session()
        titles = dedupe_titles([title, *alt_names])

        # ----------------------------------------------------
        # 1. Primary: Search every subfolder in 'Cover Arts'
        # ----------------------------------------------------
        matched_items = await self.find_covers_in_folders(titles)

        if matched_items:
            logger.info("[Covers] Found %d matching artwork file(s) in 'Cover Arts' subfolders for '%s'", len(matched_items), ", ".join(titles))

            async def cache_one(cover_meta):
                return await self._cover_entry(
                    make_download_url(cover_meta["path"]),
                    ext=cover_meta["extension"],
                    artist=clean_text(cover_meta.get("artist")) or UNKNOWN_ARTIST,
                    name=clean_text(cover_meta.get("name")) or title,
                )

            downloaded = await asyncio.gather(
                *(cache_one(item) for item in matched_items[:MAX_ATTACHED_COVERS]),
                return_exceptions=True,
            )
            valid_covers = [item for item in downloaded if isinstance(item, dict)]

            if valid_covers:
                return valid_covers

        # ----------------------------------------------------
        # 2. Secondary: JuiceVault Search Fallback
        # ----------------------------------------------------
        target_norms = {normalize(name) for name in titles}
        search_url = f"{JUICEVAULT_BASE}/music/search"
        for search_title in titles:
            try:
                async with session.get(
                    search_url,
                    params={"q": clean_title(search_title)},
                    headers=HEADERS,
                    timeout=TIMEOUT,
                ) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json(content_type=None)
                    results = data.get("results", []) if isinstance(data, dict) else []
                    for item in results:
                        cand_title = normalize(clean_text(item.get("title", "")))
                        if not cand_title or cand_title not in target_norms:
                            continue
                        uuid = item.get("id")
                        if not uuid:
                            continue
                        cached = await self._cover_entry(
                            f"{JUICEVAULT_BASE}/cdn/music/covers/{uuid}",
                            ext="webp",
                            artist=UNKNOWN_ARTIST,
                            name=search_title,
                        )
                        if cached:
                            return [cached]
            except Exception as exc:
                logger.debug("[Covers] JuiceVault fallback failed for '%s': %s", search_title, exc)

        # ----------------------------------------------------
        # 3. Tertiary: juicewrldapi Embedded Cover Fallback
        # ----------------------------------------------------
        if path:
            url = f"{API_BASE}/files/cover-art/"
            source_url = f"{url}?path={quote(path.strip().lstrip('/'), safe='')}"
            try:
                cached = await self._cover_entry(
                    source_url,
                    ext="png",
                    artist=UNKNOWN_ARTIST,
                    name=title,
                )
                if cached:
                    return [cached]
            except Exception as exc:
                logger.debug("[Covers] Embedded cover fallback failed: %s", exc)

        return []

    # ---------------------------------------------------------------
    # ,cachereload support
    # ---------------------------------------------------------------

    def _register_cache_provider(self) -> None:
        """Offer the resolved-cover manifest to ``,cachereload``."""
        cache_registry.register(
            CacheProvider(
                name="covers",
                label="Cover art",
                aliases=("cover", "art", "artwork"),
                describe=self._describe_cover_cache,
                reload=self._reload_cover_cache,
            )
        )

    def _describe_cover_cache(self) -> str:
        return (
            f"{self._cover_cache.size} cached song(s) · "
            f"{len(self._covers_cache)} indexed file(s)"
        )

    @staticmethod
    def _cover_key_matches(key: str, query: str) -> bool:
        """Whether a cached song key mentions ``query`` in any of its names."""
        norm = normalize(query)
        if not norm:
            return False
        for part in str(key).split("|"):
            if part and (norm in part or part in norm):
                return True
        return False

    async def _resolve_cached_cover(self, key: str, record: dict[str, Any]) -> bool:
        """Re-resolve one cached song the way it was resolved the first time."""
        titles = [
            clean_text(title)
            for title in (record.get("titles") or [])
            if clean_text(title)
        ] or [part for part in str(key).split("|") if part]
        if not titles:
            return False
        path = clean_text(record.get("path"))
        covers = await self._fetch_song_covers_uncached(titles[0], path, titles[1:])
        await self._cover_cache.set(
            key, covers, self._current_mode(), titles=titles, path=path
        )
        return bool(covers)

    async def _reload_cover_cache(
        self,
        target: Optional[str],
        full: bool,
        progress: cache_registry.ProgressFn,
    ) -> ReloadResult:
        """Forget the requested cover records and re-resolve them.

        A reload re-runs the search and re-uploads every image, which is what
        fixes a bucket object that was deleted while its manifest entry stayed
        behind. The images already on disk are reused, not downloaded again.
        """
        result = ReloadResult(label="Cover art")

        # Snapshot the records first: forgetting them is the point, but the
        # titles and path each one was resolved from are needed to redo it.
        records: dict[str, dict[str, Any]] = {}
        if target is None:
            for key in self._cover_cache.keys():
                records[key] = self._cover_cache.get(key) or {}
            result.forgotten = await self._cover_cache.clear()
            result.scope = "the songs that were cached"
        else:
            for key in self._cover_cache.keys():
                if self._cover_key_matches(key, target):
                    records[key] = self._cover_cache.get(key) or {}
            if not records:
                result.note = f"no cached cover art matches {target}"
                return result
            for key in records:
                await self._cover_cache.forget(key)
            result.forgotten = len(records)
            result.scope = f"the song(s) matching {target}"

        # Drop this process's short-lived copies too, so the re-resolve cannot
        # be answered out of the very results it is replacing.
        self._search_cache.clear()
        self._song_cache.clear()
        if full:
            result.scope = "every cached song, over a freshly scanned Cover Arts index"
            self._covers_cache = []
            self._covers_by_norm = {}
            self._covers_cache_timestamp = 0
            await progress("Re-indexing the Cover Arts folder...")
            await self._ensure_covers_index()

        total = len(records)
        if not total:
            result.note = (
                f"{len(self._covers_cache)} cover file(s) indexed; there was "
                "nothing cached to reload"
            )
            return result

        last_edit = 0.0
        for index, (key, record) in enumerate(records.items(), start=1):
            try:
                found = await self._resolve_cached_cover(key, record)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[Covers] Reload failed for '%s': %s", key, exc)
                result.failed += 1
            else:
                if found:
                    result.restored += 1
                else:
                    result.empty += 1

            now = time.time()
            if index == total or now - last_edit >= COVER_RELOAD_PROGRESS_SECONDS:
                last_edit = now
                await progress(
                    f"Reloaded **{index}/{total}** song(s) · "
                    f"**{result.restored}** with artwork"
                )

        logger.info(
            "[Covers] Cache reload finished: %d restored, %d empty, %d failed (%s)",
            result.restored,
            result.empty,
            result.failed,
            result.scope,
        )
        return result

    @commands.command(
        name="cover",
        aliases=("covers",),
    )
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def cover(self, ctx: commands.Context, *, song: str = ""):
        if not song.strip():
            prefix = await prefix_for(ctx)
            view = discord.ui.LayoutView(timeout=60)
            container = discord.ui.Container(accent_color=EMBED_COLOR)
            container.add_item(
                discord.ui.TextDisplay(
                    "# Command: cover\n"
                    f"**Usage:** `{prefix}cover <song>`\n\n"
                    f"**Example:** `{prefix}cover on time`"
                )
            )
            view.add_item(container)
            await ctx.send(view=view)
            return

        searching_view = discord.ui.LayoutView(timeout=120)
        searching_container = discord.ui.Container(accent_color=EMBED_COLOR)
        searching_container.add_item(
            discord.ui.TextDisplay(f"🔍 Searching for covers for **{song}**...")
        )
        searching_view.add_item(searching_container)
        searching_message = await ctx.send(view=searching_view)

        results = await self.search_songs(song)

        if not results:
            view = discord.ui.LayoutView(timeout=60)
            container = discord.ui.Container(accent_color=EMBED_COLOR)
            container.add_item(
                discord.ui.TextDisplay(f"❌ Couldn't find any songs matching **{song}**.")
            )
            view.add_item(container)
            await searching_message.edit(view=view)
            return
        if len(results) == 1:
            chosen_song = results[0]
            song_id = chosen_song.get("id") or chosen_song.get("song_id") or chosen_song.get("public_id")
            if song_id is not None:
                full_song = await self.get_song_details(song_id)
                if full_song and isinstance(full_song, dict):
                    chosen_song = full_song

            title = chosen_song.get("name") or chosen_song.get("title") or ""
            path = chosen_song.get("path") or ""

            covers = await self.fetch_song_covers(
                title,
                path,
                alt_names=song_alt_names(chosen_song),
            )
            view = CoverResultView(chosen_song, covers)
            await searching_message.edit(view=view)
            return

        # Multiple matches: show dropdown selection
        view = SongSelectView(
            query=song,
            songs=results,
            author_id=ctx.author.id,
            cog=self,
        )
        await searching_message.edit(view=view)
        return
    # ========================================================

    @cover.error
    async def cover_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ Try again in **{error.retry_after:.1f}s**.")
            return

        logger.exception("Cover command error: %s", error)
        await ctx.send(f"❌ **Cover error:** `{error}`")


# ============================================================
# SETUP
# ============================================================

async def setup(bot: commands.Bot):
    await bot.add_cog(Covers(bot))