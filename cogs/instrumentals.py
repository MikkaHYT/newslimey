from __future__ import annotations

import asyncio
import difflib
import html
import json
import logging
import re
from typing import Any, Optional
from urllib.parse import quote, urlparse

import aiohttp
import discord
from discord.ext import commands

from utils.core import mp3_buttons
from utils.core.prefixes import prefix_for

try:
    from config import EMBED_COLOR
except ImportError:
    EMBED_COLOR = discord.Color.from_str("#2b2d31")

logger = logging.getLogger("slimey.instrumentals")

API_ROOT = "https://juicewrldapi.com"
API_BASE = f"{API_ROOT}/juicewrld"
SONGS_ENDPOINT = f"{API_BASE}/songs/"
BROWSE_ENDPOINT = f"{API_BASE}/files/browse/"
DOWNLOAD_BASE = f"{API_BASE}/files/download/"

API_TIMEOUT = 25
MAX_RESULTS = 25
MAX_META_LENGTH = 90
MAX_NOTES_LENGTH = 160
VIEW_TIMEOUT = 900
SELECT_TIMEOUT = 120

COVER_FIELDS = (
    "image_url", "imageUrl", "imageURL", "image",
    "cover_url", "coverUrl", "coverURL",
    "cover_art_url", "coverArtUrl",
    "artwork_url", "artworkUrl",
    "thumbnail_url", "thumbnailUrl", "thumbnail",
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}


def clean(value: Any) -> Any:
    if value is None:
        return None
    if callable(value):
        try:
            value = value()
        except TypeError:
            return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    return value


def get_value(obj: Any, *names: str, default: Any = None) -> Any:
    if obj is None:
        return default
    for name in names:
        value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
        value = clean(value)
        if value is not None:
            return value
    return default


def recursive_find(obj: Any, names: tuple[str, ...] | list[str]) -> Any:
    wanted = {str(name).casefold() for name in names}
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).casefold() in wanted:
                value = clean(value)
                if value is not None:
                    return value
        for value in obj.values():
            found = recursive_find(value, names)
            if found is not None:
                return found
    elif isinstance(obj, (list, tuple, set)):
        for item in obj:
            found = recursive_find(item, names)
            if found is not None:
                return found
    return None


def first_value(obj: Any, *names: str, default: Any = None) -> Any:
    value = get_value(obj, *names, default=None)
    if value is not None:
        return value
    found = recursive_find(obj, names)
    return found if found is not None else default


def display(value: Any, default: str = "N/A") -> str:
    value = clean(value)
    if value is None:
        return default
    if isinstance(value, (list, tuple, set)):
        parts = [display(item, "") for item in value]
        joined = ", ".join(part for part in parts if part)
        return joined or default
    if isinstance(value, dict):
        for key in ("name", "title", "value", "text", "label"):
            if key in value:
                result = display(value[key], "")
                if result:
                    return result
        return default
    return str(value)


def display_list(value: Any, default: str = "N/A") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip() or default
    if isinstance(value, (list, tuple, set)):
        parts = [display(item, "") for item in value]
        joined = ", ".join(part for part in parts if part)
        return joined or default
    return display(value, default)


def unwrap_named(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("name") or value.get("title") or value.get("value")
    return value


def valid_http_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    try:
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return None
    except Exception:
        return None
    return value


def normalize_path(path: Any) -> str | None:
    if not path:
        return None
    path = str(path).strip()
    if not path or path.startswith(("http://", "https://")):
        return None
    while path.startswith("/"):
        path = path[1:]
    return path


def make_download_url(path: str) -> str | None:
    normalized = normalize_path(path)
    if not normalized:
        return None
    return f"{DOWNLOAD_BASE}?path={quote(normalized, safe='')}&channel=comp"


def clean_match_key(value: str) -> str:
    value = re.sub(r"\.[a-zA-Z0-9]+$", "", value)
    value = re.sub(r"[_\-.]", " ", value)
    value = re.sub(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}", "", value)
    value = re.sub(r"\s+(?:instrumental|inst|beat|prod|reprod|remake)\b", "", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).strip().lower()


def clean_meta_text(value: str) -> str:
    value = value.strip()
    value = re.sub(
        r"^(?:recorded|first previewed|previewed|surfaced|released|file exported)[\s,.:-]*",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip()
    value = re.sub(r"^[,.:\s]+", "", value).strip()
    value = re.sub(r"[,.\s]+$", "", value).strip()
    return re.sub(r"\s+", " ", value)


def clean_era_display(era_raw: Any) -> str | None:
    if not era_raw:
        return None
    val = ""
    if isinstance(era_raw, dict):
        val = era_raw.get("description") or era_raw.get("name") or era_raw.get("title") or ""
    elif isinstance(era_raw, str):
        val = era_raw
    if not val or val == "N/A":
        return None
    val = re.sub(r"\s*\([^)]*\)", "", val)
    val = re.sub(r"\s+era$", "", val, flags=re.IGNORECASE)
    return val.strip() or None


def truncate(value: str, limit: int = MAX_META_LENGTH) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def compact_value(value: str, *, limit: int = MAX_META_LENGTH) -> str | None:
    value = display(value)
    if not value or value == "N/A":
        return None
    return truncate(value.replace("\n", ", "), limit)


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


def song_text(song: Any, *keys: str, default: Any = None) -> str:
    return display(first_value(song, *keys, default=default))


def song_list(song: Any, *keys: str) -> str:
    return display_list(first_value(song, *keys, default=None))


def get_song_title(song: Any) -> str:
    return song_text(
        song,
        "name", "title", "song_title", "songTitle",
        default="Unknown Song",
    )


def resolve_cover_url(value: Any) -> str | None:
    direct = valid_http_url(value)
    if direct:
        return direct
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if value.startswith("//"):
        return f"https:{value}"
    if value.startswith("/"):
        return f"https://juicewrldapi.com{value}"
    if value.startswith(("images/", "media/")):
        return f"https://juicewrldapi.com/{value}"
    return None


def get_cover_url(song: Any) -> str | None:
    for field in COVER_FIELDS:
        value = first_value(song, field, default=None)
        if isinstance(value, dict):
            value = get_value(value, "url", "src", "href", "image_url", "imageUrl")
        result = resolve_cover_url(value)
        if result:
            return result
    return None


def get_length(song: Any) -> str:
    value = first_value(
        song,
        "length", "duration", "duration_text", "durationText",
        default=None,
    )
    if isinstance(value, (int, float)):
        minutes, seconds = divmod(int(value), 60)
        return f"{minutes}:{seconds:02d}"
    return display(value)


def get_surfaced(song: Any) -> str:
    value = first_value(
        song,
        "surfaced", "surfaced_date", "surfacedDate",
        "surface_date", "surfaceDate",
        default=None,
    )
    if value is not None:
        return display(value)
    dates = first_value(song, "dates", "important_dates", "importantDates", default=None)
    if isinstance(dates, dict):
        nested = (
            dates.get("surfaced")
            or dates.get("surface")
            or dates.get("surfaced_date")
            or dates.get("surfacedDate")
        )
        if nested:
            return display(nested)
    return "N/A"


def build_instrumental_header(song: Any) -> str:
    title = get_song_title(song)
    lines = [f"### **{title}**"]

    aka = song_list(
        song,
        "track_titles",
        "trackTitles",
        "alternative_titles",
        "alternativeTitles",
        "alternate_titles",
        "alternateTitles",
    )
    if aka and aka != "N/A":
        parts = [p.strip() for p in aka.split(",") if p.strip() and p.strip().casefold() != title.casefold()]
        if parts:
            lines.append(f"-# Alt Name(s): **{', '.join(parts)}**")

    engineers = song_list(song, "engineers", "engineer", "engineer_names", "engineerNames")
    if engineers and engineers != "N/A":
        lines.append(f"-# Engineer(s): **{engineers}**")

    producers = song_list(song, "producers", "producer", "producer_names", "producerNames")
    if producers and producers != "N/A":
        lines.append(f"-# Producer(s): **{producers}**")

    return "\n".join(lines)


def build_instrumental_details(
    song: Any,
    files: list[dict[str, Any]] | None = None,
    active_file: dict[str, Any] | None = None,
) -> str | None:
    fields: list[str] = []

    era = clean_era_display(first_value(song, "era", "era_name", "eraName"))
    if era:
        fields.append(f"**Era**\n{era}")

    if active_file and active_file.get("name"):
        file_name = active_file["name"]
        size = active_file.get("size_human")
        display_name = f"{file_name} ({size})" if size else file_name
        fields.append(f"**File Name**\n{display_name}")

    instrumentals = song_list(
        song,
        "instrumental_names",
        "instrumentalNames",
        "instrumentals",
        "instrumental",
        "instrumental_files",
        "instrumentalFiles",
    )
    if instrumentals and instrumentals != "N/A":
        fields.append(f"**Instrumentals**\n{instrumentals}")

    location = display(
        unwrap_named(
            first_value(
                song,
                "recording_locations",
                "recordingLocations",
                "recording_location",
                "recordingLocation",
                "location",
            )
        )
    )
    if location and location != "N/A":
        fields.append(f"**Recording Location**\n{location}")

    recorded = song_list(
        song,
        "record_dates",
        "recordDates",
        "recording_dates",
        "recordingDates",
        "recording_date",
        "recordingDate",
        "recorded",
        "recorded_date",
        "recordedDate",
    )
    if recorded and recorded != "N/A":
        raw_rec = clean_meta_text(recorded)
        if re.search(r"\bFile Exported\b", raw_rec, flags=re.IGNORECASE):
            match = re.search(r"^(.*?)(?:[;,]|\s)*\bFile Exported\b[\s,:.-]*(.*)$", raw_rec, flags=re.IGNORECASE)
            if match:
                rec_part = clean_meta_text(match.group(1))
                exp_part = clean_meta_text(match.group(2))
                rec_lines = []
                if rec_part:
                    rec_lines.append(f"**Recorded**\n{rec_part}")
                if exp_part:
                    rec_lines.append(f"**File Exported**\n{exp_part}")
                if rec_lines:
                    fields.append("\n".join(rec_lines))
            else:
                fields.append(f"**Recorded**\n{raw_rec}")
        else:
            fields.append(f"**Recorded**\n{raw_rec}")

    previewed = song_text(
        song,
        "preview_date",
        "previewDate",
        "first_previewed",
        "firstPreviewed",
        "first_preview_date",
        "firstPreviewDate",
    )
    if previewed and previewed != "N/A":
        fields.append(f"**First Previewed**\n{clean_meta_text(previewed)}")

    surfaced = get_surfaced(song)
    if surfaced and surfaced != "N/A":
        fields.append(f"**Surfaced**\n{clean_meta_text(surfaced)}")

    length = get_length(song)
    if length and length != "N/A":
        fields.append(f"**Length**\n{length}")

    category = display(
        unwrap_named(
            first_value(
                song,
                "category",
                "category_name",
                "categoryName",
                "status",
                "leak_type",
            )
        )
    )
    if category and category != "N/A":
        fields.append(f"**Category**\n{category}")

    if files:
        lines = [f"Instrumental: {f.get('name', 'Audio')}" for f in files[:3]]
        if lines:
            fields.append(f"**Available Files**\n" + "\n".join(lines))
    else:
        fields.append("-# ❌ *No matching instrumental file found in the library.*")

    additional_info = song_text(
        song,
        "additional_information",
        "additionalInformation",
        "additional_info",
        "additionalInfo",
        "info",
        "notes",
    )
    if additional_info and additional_info != "N/A":
        fields.append(f"-# {truncate(additional_info, MAX_NOTES_LENGTH)}")

    return "\n\n".join(fields) if fields else None


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


def make_link_button(label: str, url: str | None) -> discord.ui.Button | None:
    url = valid_http_url(url)
    if not url:
        return None
    return discord.ui.Button(label=label, style=discord.ButtonStyle.link, url=url)


class InstrumentalFileSelect(discord.ui.Select):
    def __init__(
        self,
        *,
        song: Any,
        files: list[dict[str, Any]],
        selected_index: int = 0,
        bot: Any = None,
        guild_id: int | None = None,
    ) -> None:
        self.song = song
        self.files = files
        self.bot = bot
        self.guild_id = guild_id

        options: list[discord.SelectOption] = []
        for idx, item in enumerate(files[:25]):
            name = truncate(str(item.get("name", "Instrumental")), 85)
            size = item.get("size_human")
            desc = f"Instrumental • {size}" if size else "Instrumental File"

            options.append(
                discord.SelectOption(
                    label=name[:100],
                    value=str(idx),
                    description=desc[:100],
                    default=(selected_index == idx),
                )
            )

        placeholder = "Choose an instrumental..."
        if selected_index is not None and 0 <= selected_index < len(files):
            placeholder = truncate(str(files[selected_index].get("name", "Instrumental")), 80)

        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        selected_idx = int(self.values[0])
        item = self.files[selected_idx]
        download = valid_http_url(item.get("download"))
        if not download:
            await interaction.response.send_message(
                "❌ No download link is available for that file.",
                ephemeral=True,
            )
            return

        view = InstrumentalView(
            self.song,
            files=self.files,
            selected_index=selected_idx,
            bot=self.bot,
            guild_id=self.guild_id,
        )
        await interaction.response.edit_message(view=view)


class InstrumentalView(discord.ui.LayoutView):
    def __init__(
        self,
        song: Any,
        *,
        files: list[dict[str, Any]] | None = None,
        selected_index: int = 0,
        timeout: int = VIEW_TIMEOUT,
        bot: Any = None,
        guild_id: int | None = None,
    ) -> None:
        super().__init__(timeout=timeout)

        files = files or []
        cover = get_cover_url(song)
        active_file = files[selected_index] if files and 0 <= selected_index < len(files) else None

        container = discord.ui.Container(accent_color=EMBED_COLOR)

        header_text = build_instrumental_header(song)
        if cover:
            container.add_item(
                discord.ui.Section(
                    text_display(header_text),
                    accessory=discord.ui.Thumbnail(media=cover),
                )
            )
        else:
            container.add_item(text_display(header_text))

        container.add_item(small_separator())

        details_text = build_instrumental_details(song, files=files, active_file=active_file)
        if details_text:
            container.add_item(text_display(details_text))

        action_rows: list[discord.ui.ActionRow | InstrumentalFileSelect] = []

        if active_file:
            download_button = mp3_buttons.make_download_button(
                bot,
                guild_id=guild_id,
                label="Download",
                url=valid_http_url(active_file.get("download")),
                is_mp3=True,
                filename=mp3_buttons.mp3_filename(
                    str(active_file.get("name") or "instrumental"), "instrumental"
                ),
            )
            if download_button:
                action_rows.append(discord.ui.ActionRow(download_button))

        if len(files) > 1:
            action_rows.append(
                InstrumentalFileSelect(
                    song=song,
                    files=files,
                    selected_index=selected_index,
                    bot=bot,
                    guild_id=guild_id,
                )
            )

        if action_rows:
            container.add_item(small_separator())
            for row in action_rows:
                if isinstance(row, discord.ui.ActionRow):
                    container.add_item(row)
                else:
                    container.add_item(discord.ui.ActionRow(row))

        self.add_item(container)


class InstrumentalSongSelect(discord.ui.Select):
    def __init__(self, candidates: list[dict], author_id: int, cog: "Instrumentals") -> None:
        self.candidates = candidates
        self.author_id = author_id
        self.cog = cog

        options = []
        for idx, result in enumerate(candidates[:25]):
            title = get_song_title(result)
            era = clean_era_display(first_value(result, "era", "era_name", "eraName"))
            description = f"Era: {era}"[:100] if era and era != "N/A" else None
            options.append(
                discord.SelectOption(
                    label=title[:100],
                    value=str(idx),
                    description=description,
                )
            )

        super().__init__(
            placeholder="Choose the correct song...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Only the person who ran the command can choose.",
                ephemeral=True,
            )
            return

        if self.view:
            self.view.selected = True
            self.view.stop()

        await interaction.response.defer()
        chosen = self.candidates[int(self.values[0])]

        try:
            view = await self.cog.resolve_song_view(
                chosen, guild_id=interaction.guild_id
            )
            await interaction.edit_original_response(view=view)
        except discord.HTTPException:
            logger.exception("[Instrumentals] Discord rejected card after selection")
            await interaction.edit_original_response(view=simple_view("❌ Discord rejected the instrumental card."))
        except Exception:
            logger.exception("[Instrumentals] Failed to build card after selection")
            await interaction.edit_original_response(view=simple_view("❌ Failed to build the instrumental card."))


class InstrumentalSongSelectView(discord.ui.LayoutView):
    def __init__(self, query: str, candidates: list[dict], author_id: int, cog: "Instrumentals") -> None:
        super().__init__(timeout=SELECT_TIMEOUT)
        self.selected = False
        self.message: discord.Message | None = None

        container = discord.ui.Container(accent_color=EMBED_COLOR)
        container.add_item(
            text_display(
                f"# Command: instrumental\n"
                f"Found **{len(candidates)}** matching songs for **{query}**.\n"
                f"Select the correct song from the dropdown below:"
            )
        )
        container.add_item(
            discord.ui.ActionRow(InstrumentalSongSelect(candidates, author_id, cog))
        )
        self.add_item(container)

    async def on_timeout(self) -> None:
        if self.selected or not self.message:
            return
        try:
            await self.message.edit(
                view=simple_view("⏱️ Selection timed out. Please run the command again.", timeout=10)
            )
        except Exception:
            pass


class InstrumentalAPI:
    def __init__(self) -> None:
        self.session: aiohttp.ClientSession | None = None
        self._song_folders: list[dict[str, Any]] = []
        self._direct_files: list[dict[str, Any]] = []
        self._cache_timestamp: float = 0
        self._index_lock = asyncio.Lock()

    async def start(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
            self.session = aiohttp.ClientSession(
                timeout=timeout,
                headers=HEADERS,
            )
        return self.session

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def request(self, endpoint: str, params: dict | None = None) -> Any:
        session = await self.start()
        url = f"{API_BASE}/{endpoint.lstrip('/')}"
        try:
            async with session.get(url, params=params) as response:
                if response.status != 200:
                    return None
                text = await response.text()
                return parse_api_json(text)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

    async def browse_directory(self, path: str) -> list[dict]:
        data = await self.request(
            "files/browse/",
            params={"path": path, "channel": "comp", "page_size": 2000},
        )
        if isinstance(data, dict):
            return data.get("items", [])
        return []

    async def _ensure_instrumental_index(self) -> None:
        now = asyncio.get_running_loop().time()
        if self._song_folders and (now - self._cache_timestamp < 1800):
            return

        async with self._index_lock:
            if self._song_folders and (now - self._cache_timestamp < 1800):
                return

            collected_folders: list[dict[str, Any]] = []
            collected_files: list[dict[str, Any]] = []

            top_items = await self.browse_directory("Instrumentals")
            era_dirs = [item for item in top_items if item.get("type") == "directory"]

            for item in top_items:
                if item.get("type") != "directory" and item.get("path"):
                    collected_files.append({
                        "name": item.get("name", ""),
                        "path": item.get("path", ""),
                        "size_human": item.get("size_human", ""),
                        "era": "Instrumentals",
                    })

            if era_dirs:
                tasks = [self.browse_directory(era["path"]) for era in era_dirs]
                res_list = await asyncio.gather(*tasks, return_exceptions=True)
                for era_dir, res in zip(era_dirs, res_list):
                    if not isinstance(res, list):
                        continue

                    era_name = era_dir.get("name", "")
                    for item in res:
                        if item.get("type") == "directory" and item.get("path"):
                            collected_folders.append({
                                "name": item.get("name", ""),
                                "path": item.get("path", ""),
                                "era": era_name,
                            })
                        elif item.get("path"):
                            collected_files.append({
                                "name": item.get("name", ""),
                                "path": item.get("path", ""),
                                "size_human": item.get("size_human", ""),
                                "era": era_name,
                            })

            self._song_folders = collected_folders
            self._direct_files = collected_files
            self._cache_timestamp = now
            logger.info(
                "[Instrumentals] Indexed %d song folders and %d loose files across %d categories.",
                len(collected_folders),
                len(collected_files),
                len(era_dirs),
            )

    async def fetch_folder_files(self, folder_path: str) -> list[dict[str, Any]]:
        items = await self.browse_directory(folder_path)
        files: list[dict[str, Any]] = []
        for item in items:
            if item.get("type") == "directory":
                sub_items = await self.browse_directory(item["path"])
                for sub in sub_items:
                    if sub.get("path"):
                        files.append({
                            "name": sub.get("name", ""),
                            "path": sub.get("path", ""),
                            "size_human": sub.get("size_human", ""),
                            "download": make_download_url(sub["path"]),
                        })
            elif item.get("path"):
                files.append({
                    "name": item.get("name", ""),
                    "path": item.get("path", ""),
                    "size_human": item.get("size_human", ""),
                    "download": make_download_url(item["path"]),
                })
        return files

    async def find_instrumentals_for_song(
        self,
        title: str,
        alt_titles: list[str] | None = None,
        era_hint: str | None = None,
    ) -> list[dict[str, Any]]:
        await self._ensure_instrumental_index()

        target_keys = {clean_match_key(title)}
        if alt_titles:
            for alt in alt_titles:
                k = clean_match_key(alt)
                if k:
                    target_keys.add(k)

        matched_folder: Optional[dict[str, Any]] = None
        best_folder_score = 0.0

        for folder in self._song_folders:
            folder_name = folder.get("name", "")
            clean_folder = clean_match_key(folder_name)

            if clean_folder in target_keys:
                if era_hint and era_hint.lower() in folder.get("era", "").lower():
                    matched_folder = folder
                    break
                if not matched_folder:
                    matched_folder = folder

            for k in target_keys:
                if len(k) >= 4 and (k in clean_folder or clean_folder in k):
                    if not matched_folder:
                        matched_folder = folder
                    break

            for k in target_keys:
                score = difflib.SequenceMatcher(None, k, clean_folder).ratio()
                if era_hint and era_hint.lower() in folder.get("era", "").lower():
                    score += 0.15
                if score > best_folder_score:
                    best_folder_score = score
                    if score >= 0.70:
                        matched_folder = folder

        if matched_folder and matched_folder.get("path"):
            files = await self.fetch_folder_files(matched_folder["path"])
            if files:
                return files

        matched_files: list[dict[str, Any]] = []
        for file in self._direct_files:
            file_name = file.get("name", "")
            clean_file = clean_match_key(file_name)

            is_match = clean_file in target_keys
            if not is_match:
                for k in target_keys:
                    if len(k) >= 4 and (k in clean_file or clean_file in k):
                        is_match = True
                        break

            if not is_match:
                for k in target_keys:
                    score = difflib.SequenceMatcher(None, k, clean_file).ratio()
                    if score >= 0.75:
                        is_match = True
                        break

            if is_match and file.get("path"):
                matched_files.append({
                    "name": file_name,
                    "path": file["path"],
                    "size_human": file.get("size_human", ""),
                    "download": make_download_url(file["path"]),
                })

        return matched_files

    async def search_songs(self, query: str) -> list[dict]:
        data = await self.request(
            "songs/",
            params={
                "search": query,
                "page": 1,
                "page_size": MAX_RESULTS,
                "limit": MAX_RESULTS,
            },
        )
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("results", "songs", "data", "items"):
                val = data.get(key)
                if isinstance(val, list):
                    return val
        return []

    async def get_song(self, song_id: Any) -> Any:
        for endpoint in (f"songs/{song_id}/", f"songs/{song_id}"):
            data = await self.request(endpoint)
            if data:
                return data
        return None


class Instrumentals(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.api = InstrumentalAPI()

    def cog_unload(self) -> None:
        asyncio.create_task(self.api.close())

    async def resolve_song_view(
        self, song: dict, *, guild_id: int | None = None
    ) -> InstrumentalView:
        song_id = first_value(
            song,
            "id", "song_id", "songId", "public_id", "publicId",
            default=None,
        )

        if song_id is not None:
            full_song = await self.api.get_song(song_id)
            if full_song:
                if isinstance(full_song, dict):
                    wrapped = full_song.get("song") or full_song.get("data")
                    song = wrapped if isinstance(wrapped, dict) else full_song
                else:
                    song = full_song

        title = get_song_title(song)
        era_hint = clean_era_display(first_value(song, "era", "era_name", "eraName"))

        alt_titles = []
        raw_aka = song_list(song, "track_titles", "trackTitles", "alternative_titles", "alternate_titles")
        if raw_aka and raw_aka != "N/A":
            alt_titles = [t.strip() for t in raw_aka.split(",") if t.strip()]

        raw_insts = song_list(song, "instrumental_names", "instrumentalNames", "instrumentals")
        if raw_insts and raw_insts != "N/A":
            alt_titles.extend([t.strip() for t in raw_insts.split(",") if t.strip()])

        files = await self.api.find_instrumentals_for_song(title, alt_titles=alt_titles, era_hint=era_hint)
        return InstrumentalView(
            song, files=files, selected_index=0, bot=self.bot, guild_id=guild_id
        )

    @commands.command(name="instrumental", aliases=["inst", "instrumentals", "beat"])
    @commands.cooldown(1, 4, commands.BucketType.user)
    async def instrumental_command(self, ctx: commands.Context, *, query: str | None = None) -> None:
        if not query:
            prefix = await prefix_for(ctx)
            help_view = simple_view(
                "# Command: instrumental\n\n"
                "**Syntax**\n"
                f"`{prefix}instrumental <song>`\n\n"
                "**Example**\n"
                f"`{prefix}instrumental lucid dreams`"
            )
            await ctx.send(view=help_view)
            return

        searching_msg = await ctx.send(
            view=simple_view(f"🔍 Searching instrumentals for **{query}**...")
        )

        results = await self.api.search_songs(query)

        if not results:
            files = await self.api.find_instrumentals_for_song(query)
            if not files:
                await searching_msg.edit(
                    view=simple_view(f"❌ Couldn't find any instrumentals matching **{query}**.")
                )
                return

            synthetic_song = {
                "name": query.title(),
                "era": "N/A",
            }
            view = InstrumentalView(
                synthetic_song,
                files=files,
                selected_index=0,
                bot=self.bot,
                guild_id=ctx.guild.id if ctx.guild else None,
            )
            await searching_msg.edit(view=view)
            return

        wanted = query.strip().casefold()
        exact_match = next(
            (
                result
                for result in results
                if get_song_title(result).strip().casefold() == wanted
            ),
            None,
        )

        if len(results) == 1 or exact_match is not None:
            song = exact_match or results[0]
            try:
                view = await self.resolve_song_view(
                    song, guild_id=ctx.guild.id if ctx.guild else None
                )
                await searching_msg.edit(view=view)
            except discord.HTTPException:
                logger.exception("[Instrumentals] Discord rejected card")
                await searching_msg.edit(view=simple_view("❌ Discord rejected the instrumental card."))
            except Exception:
                logger.exception("[Instrumentals] Failed to build card")
                await searching_msg.edit(view=simple_view("❌ Failed to build the instrumental card."))
            return

        dropdown_view = InstrumentalSongSelectView(
            query=query,
            candidates=results,
            author_id=ctx.author.id,
            cog=self,
        )
        await searching_msg.edit(view=dropdown_view)
        dropdown_view.message = searching_msg

    @instrumental_command.error
    async def instrumental_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏱️ Try again in **{error.retry_after:.1f}s**.")
            return

        logger.exception("[Instrumentals] Command error: %s", error)
        await ctx.send(f"❌ **Instrumental error:** `{error}`")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Instrumentals(bot))
