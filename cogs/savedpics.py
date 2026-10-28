# cogs/savedpics.py

"""Save attached media locally and post it again later.

Each kind of media has its own save/send pair:

``,ss <name>``  stores every image on the command message under ``<name>``
``,sv <name>``  stores every video on the command message under ``<name>``
``,sf <name>``  stores every audio file (mp3) on the command message under ``<name>``

Whatever is attached is written to disk, the message it came in on is deleted so
the channel stays clean, and ``,sendpic`` / ``,sendvid`` / ``,sendfile`` posts
the raw file(s) again later with no embed around them.

Saving under a name that is already taken stacks the new file(s) on top of the
ones saved before instead of replacing them, up to the per-kind limit: once the
stack is full the oldest file(s) drop off so the name always holds the newest.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

import discord
from discord.ext import commands

from config import EMBED_COLOR
from utils.core.prefixes import prefix_for

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
# The indexes live beside the media directories (never inside them), matching
# the other media caches, so one can never be mistaken for a saved file.
SAVED_PICS_DIR = ROOT / "saved_pics"
SAVED_PICS_META_PATH = ROOT / "saved_pics.json"
SAVED_VIDEOS_DIR = ROOT / "saved_videos"
SAVED_VIDEOS_META_PATH = ROOT / "saved_videos.json"
SAVED_FILES_DIR = ROOT / "saved_files"
SAVED_FILES_META_PATH = ROOT / "saved_files.json"

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm", ".mkv", ".m4v")
AUDIO_EXTENSIONS = (".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac")

IMAGE_CONTENT_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}
VIDEO_CONTENT_TYPES = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "video/x-m4v": ".m4v",
}
AUDIO_CONTENT_TYPES = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/aac": ".aac",
}

MAX_NAME_LENGTH = 48
# A discord MediaGallery can only render ten items.
MAX_IMAGES = 10
MAX_VIDEOS = 5
MAX_AUDIO = 5
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_VIDEO_BYTES = 25 * 1024 * 1024
MAX_AUDIO_BYTES = 16 * 1024 * 1024
MAX_LISTED_NAMES = 30


# ============================================================
# MEDIA KINDS
# ============================================================

@dataclass(frozen=True)
class MediaKind:
    """Everything that differs between saved pictures, videos and audio files."""

    key: str
    singular: str
    plural: str
    noun: str
    emoji: str
    directory: Path
    meta_path: Path
    extensions: tuple[str, ...]
    content_types: dict[str, str]
    max_bytes: int
    max_files: int
    # Images render reliably inside a MediaGallery; videos and audio are posted
    # as plain attachments instead.
    gallery: bool
    save_command: str
    send_command: str
    list_command: str
    delete_command: str
    example: str


PICTURES = MediaKind(
    key="pic",
    singular="picture",
    plural="pictures",
    noun="image",
    emoji="🖼️",
    directory=SAVED_PICS_DIR,
    meta_path=SAVED_PICS_META_PATH,
    extensions=IMAGE_EXTENSIONS,
    content_types=IMAGE_CONTENT_TYPES,
    max_bytes=MAX_IMAGE_BYTES,
    max_files=MAX_IMAGES,
    gallery=True,
    save_command="ss",
    send_command="sendpic",
    list_command="piclist",
    delete_command="delpic",
    example="frosty droppin a 2",
)

VIDEOS = MediaKind(
    key="vid",
    singular="video",
    plural="videos",
    noun="video",
    emoji="🎬",
    directory=SAVED_VIDEOS_DIR,
    meta_path=SAVED_VIDEOS_META_PATH,
    extensions=VIDEO_EXTENSIONS,
    content_types=VIDEO_CONTENT_TYPES,
    max_bytes=MAX_VIDEO_BYTES,
    max_files=MAX_VIDEOS,
    gallery=False,
    save_command="sv",
    send_command="sendvid",
    list_command="vidlist",
    delete_command="delvid",
    example="my clip",
)

AUDIO_FILES = MediaKind(
    key="file",
    singular="audio file",
    plural="audio files",
    noun="mp3",
    emoji="🎵",
    directory=SAVED_FILES_DIR,
    meta_path=SAVED_FILES_META_PATH,
    extensions=AUDIO_EXTENSIONS,
    content_types=AUDIO_CONTENT_TYPES,
    max_bytes=MAX_AUDIO_BYTES,
    max_files=MAX_AUDIO,
    gallery=False,
    save_command="sf",
    send_command="sendfile",
    list_command="filelist",
    delete_command="delfile",
    example="my song",
)

KINDS = (PICTURES, VIDEOS, AUDIO_FILES)


def article(word: str) -> str:
    """``a``/``an`` for a kind's noun, so messages read correctly."""
    if word[:1].lower() in "aeiou":
        return "an"
    # "mp3" is read out as "em-pee-three", so it takes "an" as well.
    if re.match(r"^m[pf]\d", word.lower()):
        return "an"
    return "a"


# ============================================================
# NAME & ATTACHMENT HELPERS
# ============================================================

def slugify_name(raw: Any) -> str:
    """Normalize a user-typed save name into its lookup key.

    Everyone types names differently (``My Pic``, ``my pic``, ``my  pic!``),
    so every lookup goes through the same slug: lowercased, punctuation
    dropped, whitespace collapsed.
    """
    cleaned = re.sub(r"[^a-z0-9]+", " ", str(raw or "").casefold())
    return re.sub(r"\s+", " ", cleaned).strip()[:MAX_NAME_LENGTH]


def clean_display_name(raw: Any) -> str:
    """The name as the user typed it, trimmed and single-spaced."""
    return re.sub(r"\s+", " ", str(raw or "").strip())[:MAX_NAME_LENGTH]


def file_token(key: str) -> str:
    """A filesystem-safe form of a lookup key."""
    token = re.sub(r"[^a-z0-9]+", "-", str(key or "")).strip("-")
    return token or "media"


def attachment_extension(kind: MediaKind, attachment: Any) -> Optional[str]:
    """The matching extension for an attachment, or None when it doesn't fit."""
    name = str(getattr(attachment, "filename", "") or "")
    extension = Path(name).suffix.lower()
    if extension in kind.extensions:
        return extension

    content_type = str(getattr(attachment, "content_type", "") or "")
    return kind.content_types.get(content_type.split(";")[0].strip().lower())


def find_media_attachments(kind: MediaKind, message: Any) -> list[Any]:
    """Every matching attachment on a message, including one it replies to."""
    if message is None:
        return []

    attachments = list(getattr(message, "attachments", None) or [])
    reference = getattr(message, "reference", None)
    referenced = getattr(reference, "resolved", None)
    if referenced is not None:
        # A partial message (unfetched reply) has no attachments to add.
        attachments.extend(getattr(referenced, "attachments", None) or [])

    return [
        attachment
        for attachment in attachments
        if attachment_extension(kind, attachment)
    ]


def upload_name(original: Any, fallback: str) -> str:
    """The filename a saved file is re-uploaded as, trimmed to a safe length."""
    name = Path(str(original or "")).name.strip()
    if not name:
        return fallback
    if len(name) <= 100:
        return name
    stem, suffix = Path(name).stem, Path(name).suffix
    return f"{stem[: 100 - len(suffix)]}{suffix}"


def format_size(num_bytes: int) -> str:
    if num_bytes >= 1024 ** 2:
        return f"{num_bytes / (1024 ** 2):.1f} MiB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.1f} KiB"
    return f"{num_bytes} B"


def format_saved_at(value: Any) -> str:
    """Relative timestamp for a stored ``saved_at`` value."""
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return "unknown"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return discord.utils.format_dt(moment, style="R")


# ============================================================
# LOCAL STORAGE
# ============================================================

class SavedMediaStore:
    """JSON-backed registry of saved files, keyed by guild and name.

    The index maps ``guild id -> name key -> record``, where a record holds the
    on-disk filename of every saved file. Records whose files have gone missing
    are reported as absent, which keeps the store self-healing.
    """

    def __init__(
        self,
        directory: Path,
        meta_path: Path,
    ) -> None:
        self.directory = Path(directory)
        self.meta_path = Path(meta_path)
        self._records: dict[str, dict[str, dict[str, Any]]] = {}
        self._load()

    # --------------------------------------------------------
    # Index persistence
    # --------------------------------------------------------

    def _load(self) -> None:
        try:
            if not self.meta_path.is_file():
                return
            data = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except Exception as exc:
            # A damaged index only loses names, so start clean rather than crash.
            logger.warning("[SavedMedia] Ignoring unreadable index %s: %s", self.meta_path.name, exc)
            return

        if not isinstance(data, dict):
            return

        for guild_key, entries in data.items():
            if not isinstance(entries, dict):
                continue
            self._records[str(guild_key)] = {
                str(name_key): record
                for name_key, record in entries.items()
                if isinstance(record, dict)
            }

    def _save(self) -> None:
        temporary = self.meta_path.with_suffix(self.meta_path.suffix + ".tmp")
        try:
            self.meta_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(self._records, indent=2), encoding="utf-8")
            os.replace(temporary, self.meta_path)
        except Exception:
            logger.exception("[SavedMedia] Could not write the index at %s", self.meta_path)

    # --------------------------------------------------------
    # Disk helpers
    # --------------------------------------------------------

    @staticmethod
    def _write_bytes(path: Path, data: bytes) -> None:
        temporary = path.with_suffix(path.suffix + ".part")
        temporary.write_bytes(data)
        os.replace(temporary, path)

    @staticmethod
    def _remove(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("[SavedMedia] Could not delete %s", path)

    def files_for(self, record: dict[str, Any]) -> list[tuple[Path, str]]:
        """Every saved file on disk as ``(path, original filename)`` pairs."""
        found: list[tuple[Path, str]] = []
        for entry in record.get("files") or []:
            if not isinstance(entry, dict):
                continue
            filename = str(entry.get("filename") or "")
            if not filename:
                continue
            # ``Path(...).name`` guards against a tampered index escaping the dir.
            path = self.directory / Path(filename).name
            if path.is_file():
                found.append((path, str(entry.get("original") or "")))
        return found

    def paths(self, record: dict[str, Any]) -> list[Path]:
        """Every saved file for a record that still exists on disk."""
        return [path for path, _original in self.files_for(record)]

    # --------------------------------------------------------
    # Lookups
    # --------------------------------------------------------

    def find(self, guild_id: Any, name: str) -> Optional[dict[str, Any]]:
        """The stored record for a name, even if its files are gone."""
        key = slugify_name(name)
        if not key:
            return None
        record = (self._records.get(str(guild_id)) or {}).get(key)
        if not isinstance(record, dict):
            return None
        return {"key": key, **record}

    def get(self, guild_id: Any, name: str) -> Optional[dict[str, Any]]:
        """The record for a name, or None when nothing usable is stored."""
        record = self.find(guild_id, name)
        if record is None or not self.paths(record):
            return None
        return record

    def stored_records(self, guild_id: Any) -> list[dict[str, Any]]:
        """Every usable record for a guild, sorted by display name."""
        records = []
        for key, record in (self._records.get(str(guild_id)) or {}).items():
            if not isinstance(record, dict) or not self.paths(record):
                continue
            records.append({"key": key, **record})
        return sorted(records, key=lambda item: str(item.get("name") or item["key"]).lower())

    def close_names(self, guild_id: Any, raw: str, limit: int = 5) -> list[str]:
        """Saved names close to what the user typed, for typo suggestions."""
        records = self.stored_records(guild_id)
        by_key = {str(record["key"]): str(record.get("name") or record["key"]) for record in records}
        matches = difflib.get_close_matches(slugify_name(raw), list(by_key), n=limit, cutoff=0.4)
        return [by_key[match] for match in matches]

    # --------------------------------------------------------
    # Mutation
    # --------------------------------------------------------

    def append(
        self,
        guild_id: Any,
        name: str,
        files: Sequence[tuple[str, str, bytes]],
        *,
        author_id: Optional[int] = None,
        author_name: str = "",
        max_files: Optional[int] = None,
    ) -> dict[str, Any]:
        """Store ``files`` under ``name``, stacked on top of anything saved before.

        ``files`` is a sequence of ``(original filename, extension, bytes)``.
        New files go to the end of the stack; when ``max_files`` is reached the
        oldest entries drop off (and are deleted from disk), so a name always
        holds the most recently saved files. The returned record carries the
        ``added`` and ``dropped`` entries for the caller's reply.
        """
        key = slugify_name(name)
        if not key:
            raise ValueError("A saved file needs a name containing letters or numbers")
        if not files:
            raise ValueError("A saved file needs at least one attachment")

        guild_key = str(guild_id)
        entries = self._records.setdefault(guild_key, {})
        previous = entries.get(key) or {}

        # Anything saved before stays in the stack, oldest first, but entries
        # whose files vanished from disk are pruned so the index self-heals.
        kept: list[dict[str, Any]] = []
        next_index = 1
        token = file_token(key)
        prefix = f"{guild_key}-{token}-"
        for entry in previous.get("files") or []:
            if not isinstance(entry, dict):
                continue
            filename = str(entry.get("filename") or "")
            if not filename:
                continue
            if not (self.directory / Path(filename).name).is_file():
                continue
            kept.append(entry)
            # Continue numbering past the files already there, so a new save can
            # never land on a filename that is still in use.
            stem = Path(filename).stem
            tail = stem[len(prefix):] if stem.startswith(prefix) else ""
            if tail.isdigit():
                next_index = max(next_index, int(tail) + 1)

        self.directory.mkdir(parents=True, exist_ok=True)

        stored: list[dict[str, Any]] = []
        for offset, (original, extension, data) in enumerate(files):
            filename = f"{prefix}{next_index + offset}{extension}"
            self._write_bytes(self.directory / filename, data)
            stored.append({
                "filename": filename,
                "original": original,
                "size": len(data),
            })

        merged = kept + stored
        dropped: list[dict[str, Any]] = []
        if max_files and len(merged) > max_files:
            dropped = merged[: len(merged) - max_files]
            merged = merged[len(merged) - max_files :]
            for entry in dropped:
                self._remove(self.directory / Path(str(entry.get("filename"))).name)

        now = datetime.now(timezone.utc).isoformat()
        record = {
            "name": clean_display_name(name),
            "files": merged,
            # The stack keeps the person who created the name, so delete rights
            # stay with them even when somebody else adds to it.
            "author_id": previous.get("author_id", author_id),
            "author_name": previous.get("author_name") or author_name,
            "saved_at": previous.get("saved_at") or now,
            "updated_at": now,
        }
        entries[key] = record
        self._save()
        return {"key": key, "added": stored, "dropped": dropped, **record}

    def delete(self, guild_id: Any, name: str) -> Optional[dict[str, Any]]:
        """Remove a record and its files, returning what was removed."""
        key = slugify_name(name)
        entries = self._records.get(str(guild_id)) or {}
        record = entries.get(key)
        if not isinstance(record, dict):
            return None

        for path in self.paths(record):
            self._remove(path)

        entries.pop(key, None)
        self._save()
        return {"key": key, **record}


# ============================================================
# COMPONENTS V2 VIEWS
# ============================================================

def simple_view(content: str, *, timeout: float = 60) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=timeout)
    container = discord.ui.Container(accent_color=EMBED_COLOR)
    container.add_item(discord.ui.TextDisplay(content=content))
    view.add_item(container)
    return view


def build_media_view(
    kind: MediaKind,
    *,
    title: str,
    body: str,
    files: Sequence[discord.File] = (),
    timeout: float = 300,
) -> discord.ui.LayoutView:
    """A card with the given header/body, plus a gallery for saved pictures."""
    view = discord.ui.LayoutView(timeout=timeout)
    container = discord.ui.Container(accent_color=EMBED_COLOR)
    container.add_item(discord.ui.TextDisplay(content=f"# {title}\n{body}"))
    if files and kind.gallery:
        container.add_item(
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small)
        )
        container.add_item(
            discord.ui.MediaGallery(
                *(
                    discord.MediaGalleryItem(media=f"attachment://{file.filename}")
                    for file in files
                )
            )
        )
    view.add_item(container)
    return view


def build_usage_view(kind: MediaKind, prefix: str) -> discord.ui.LayoutView:
    return simple_view(
        f"# Command: {kind.save_command}\n"
        f"**Usage:** `{prefix}{kind.save_command} <name>` with {article(kind.noun)} {kind.noun} "
        "attached (or replying to one)\n\n"
        f"The {kind.noun} is saved on the bot's disk under `<name>` and the message "
        "you attached it to is deleted.\n"
        f"Saving under a name that already exists stacks the new {kind.noun}(s) on "
        f"top of the old ones instead of replacing them (up to {kind.max_files} "
        "per name).\n"
        f"**Send it again with:** `{prefix}{kind.send_command} <name>`\n\n"
        f"**Example:** `{prefix}{kind.save_command} {kind.example}`"
    )


def _bot_admin_or_administrator() -> Any:
    """Allow bot admins anywhere, or anyone with Administrator in the server.

    IDs added with ``!admin allow`` (and the application team) can delete a
    saved picture from any server without holding Administrator there. Everyone
    else needs the Administrator permission in the server they run the command
    in.
    """

    async def predicate(ctx: commands.Context) -> bool:
        # Stored admin list first, then the bot's is_owner override (which
        # also covers the application team).
        admin_ids = getattr(ctx.bot, "bot_admin_ids", None) or set()
        if ctx.author.id in admin_ids:
            return True
        if await ctx.bot.is_owner(ctx.author):
            return True
        permissions = getattr(ctx.author, "guild_permissions", None)
        if permissions is not None and permissions.administrator:
            return True
        raise commands.MissingPermissions(["administrator"])

    return commands.check(predicate)


# ============================================================
# COG
# ============================================================

# The cog name drives the ,help category (keyword matched), and "media" would
# file these commands under Juice WRLD, so keep the neutral name they had before.
class SavedMedia(commands.Cog, name="SavedFiles"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.stores = {
            kind.key: SavedMediaStore(kind.directory, kind.meta_path)
            for kind in KINDS
        }

    # --------------------------------------------------------
    # Shared helpers
    # --------------------------------------------------------

    @staticmethod
    def guild_id(ctx: commands.Context) -> int:
        return ctx.guild.id if ctx.guild else 0

    def store(self, kind: MediaKind) -> SavedMediaStore:
        return self.stores[kind.key]

    @staticmethod
    def byte_limit(ctx: commands.Context, kind: MediaKind) -> int:
        """The largest file that can still be re-uploaded in this guild.

        Discords's per-guild upload limit decides whether a saved file could ever
        be sent back, so a file over it is refused up front rather than stored.
        """
        limit = getattr(ctx.guild, "filesize_limit", None) if ctx.guild else None
        if isinstance(limit, int) and limit > 0:
            return min(kind.max_bytes, limit)
        return kind.max_bytes

    def upload_files(self, kind: MediaKind, record: dict[str, Any]) -> list[discord.File]:
        """Upload-ready files, named after the attachments they were saved as."""
        files: list[discord.File] = []
        used: set[str] = set()
        for index, (path, original) in enumerate(self.store(kind).files_for(record), start=1):
            name = upload_name(original, path.name)
            if name in used:
                stem, suffix = Path(name).stem, Path(name).suffix
                name = f"{stem}-{index}{suffix}"
            used.add(name)
            files.append(discord.File(str(path), filename=name))
        return files

    @staticmethod
    async def delete_trigger(ctx: commands.Context) -> None:
        """Delete the message that invoked the command, if we can."""
        # Slash invocations have no real message to delete.
        if ctx.interaction is not None:
            return
        message = getattr(ctx, "message", None)
        if message is None:
            return
        try:
            await message.delete()
        except discord.DiscordException:
            pass

    def build_list_view(
        self,
        kind: MediaKind,
        guild_id: int,
        *,
        prefix: str,
        heading: Optional[str] = None,
    ) -> discord.ui.LayoutView:
        heading = heading or f"# Saved {kind.plural.title()}"
        records = self.store(kind).stored_records(guild_id)
        if not records:
            return simple_view(
                f"{heading}\n"
                f"No {kind.plural} have been saved in this server yet.\n"
                f"-# Save one with `{prefix}{kind.save_command} <name>` and "
                f"{article(kind.noun)} {kind.noun} attached."
            )

        lines = [heading, f"**{len(records)}** {kind.singular}(s) saved here.", ""]
        for record in records[:MAX_LISTED_NAMES]:
            file_count = len(self.store(kind).paths(record))
            # ``updated_at`` moves whenever something is stacked on the name, so
            # the list reflects the newest save rather than the first one.
            saved = format_saved_at(record.get("updated_at") or record.get("saved_at"))
            author = record.get("author_name") or "unknown"
            lines.append(
                f"> **{record['name']}** — `{file_count}` file(s) · saved {saved} by {author}"
            )
        if len(records) > MAX_LISTED_NAMES:
            lines.append(f"> _...and {len(records) - MAX_LISTED_NAMES} more._")

        lines.append("")
        lines.append(
            f"-# Send one with `{prefix}{kind.send_command} <name>` · "
            f"delete one with `{prefix}{kind.delete_command} <name>`"
        )
        return simple_view("\n".join(lines), timeout=180)

    # --------------------------------------------------------
    # Saving
    # --------------------------------------------------------

    async def save_media(self, ctx: commands.Context, kind: MediaKind, name: str) -> None:
        prefix = await prefix_for(ctx)
        display = clean_display_name(name)
        attachments = find_media_attachments(kind, getattr(ctx, "message", None))

        if not display and not attachments:
            await ctx.send(view=build_usage_view(kind, prefix))
            return
        if not display:
            await ctx.send(
                view=simple_view(
                    f"❌ Give the {kind.singular} a name.\n"
                    f"**Usage:** `{prefix}{kind.save_command} <name>` with "
                    f"{article(kind.noun)} {kind.noun} attached."
                )
            )
            return
        if not slugify_name(display):
            await ctx.send(
                view=simple_view("❌ That name needs at least one letter or number in it.")
            )
            return
        if not attachments:
            await ctx.send(
                view=simple_view(
                    f"❌ No {kind.noun} found.\n"
                    f"Attach {article(kind.noun)} {kind.noun} to your message, or reply "
                    "to one that already has it."
                )
            )
            return

        byte_limit = self.byte_limit(ctx, kind)
        accepted: list[tuple[str, str, bytes]] = []
        skipped: list[str] = []
        if len(attachments) > kind.max_files:
            skipped.append(
                f"{len(attachments) - kind.max_files} extra file(s) "
                f"(only {kind.max_files} per name)"
            )

        for attachment in attachments[: kind.max_files]:
            # Attachments are pre-filtered by extension; the fallback just keeps a
            # stray content type from blowing up the save.
            extension = (
                attachment_extension(kind, attachment)
                or Path(attachment.filename).suffix
                or kind.extensions[0]
            )

            if int(getattr(attachment, "size", 0) or 0) > byte_limit:
                skipped.append(
                    f"`{attachment.filename}` ({format_size(attachment.size)} over the "
                    f"{format_size(byte_limit)} limit)"
                )
                continue

            try:
                data = await attachment.read()
            except Exception:
                logger.exception("[SavedMedia] Could not download %s", attachment.filename)
                skipped.append(f"`{attachment.filename}` (download failed)")
                continue

            if not data:
                skipped.append(f"`{attachment.filename}` (empty file)")
                continue
            if len(data) > byte_limit:
                skipped.append(
                    f"`{attachment.filename}` ({format_size(len(data))} over the "
                    f"{format_size(byte_limit)} limit)"
                )
                continue

            accepted.append((attachment.filename, extension, data))

        if not accepted:
            await ctx.send(
                view=simple_view(
                    "❌ Nothing was saved.\n" + "\n".join(f"> {entry}" for entry in skipped)
                )
            )
            return

        store = self.store(kind)
        previous = store.find(self.guild_id(ctx), display)
        # Files that were already stacked under this name, which the new ones
        # land on top of instead of replacing.
        before_count = len(store.paths(previous)) if previous is not None else 0
        record = store.append(
            self.guild_id(ctx),
            display,
            accepted,
            author_id=ctx.author.id,
            author_name=ctx.author.display_name,
            max_files=kind.max_files,
        )

        # The file lives on disk now, so the message it came in on can go.
        await self.delete_trigger(ctx)

        # Only the files this save added are echoed back; the rest of the stack
        # is already on the bot from earlier and does not need re-uploading.
        files = self.upload_files(kind, {"files": record.get("added") or []})
        total_files = len(store.paths(record))
        dropped = record.get("dropped") or []
        total_bytes = sum(len(data) for _, _, data in accepted)
        body = (
            f"**Name:** `{record['name']}`\n"
            f"**Files:** {len(files)} added · {total_files} total · "
            f"{format_size(total_bytes)}\n"
            f"-# Send it later with `{prefix}{kind.send_command} {record['key']}`"
        )
        if before_count:
            body = (
                f"➕ Stacked on the {before_count} already saved under this name.\n"
                + body
            )
        if dropped:
            body += (
                f"\n-# Dropped the oldest {len(dropped)} to stay at "
                f"{kind.max_files} per name."
            )
        if skipped:
            body += "\n-# Skipped: " + ", ".join(skipped)

        await ctx.send(
            view=build_media_view(
                kind,
                title=f"{kind.singular.title()} Saved",
                body=body,
                files=files,
            ),
            files=files,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # --------------------------------------------------------
    # Sending
    # --------------------------------------------------------

    async def send_media(self, ctx: commands.Context, kind: MediaKind, name: str) -> None:
        """Post a saved file back as a plain attachment, with no embed."""
        guild_id = self.guild_id(ctx)
        prefix = await prefix_for(ctx)
        display = clean_display_name(name)
        store = self.store(kind)

        if not display:
            await ctx.send(
                view=self.build_list_view(
                    kind,
                    guild_id,
                    prefix=prefix,
                    heading=f"# Command: {kind.send_command}",
                )
            )
            return

        record = store.get(guild_id, display)
        if record is None:
            suggestion = store.close_names(guild_id, display)
            lines = [f"❌ Nothing is saved under **{display}**."]
            if suggestion:
                lines.append("Did you mean: " + ", ".join(f"`{entry}`" for entry in suggestion))
            lines.append(
                f"-# See everything saved here with `{prefix}{kind.list_command}`."
            )
            await ctx.send(view=simple_view("\n".join(lines), timeout=120))
            return

        files = self.upload_files(kind, record)
        await ctx.send(
            files=files,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # --------------------------------------------------------
    # Listing & deleting
    # --------------------------------------------------------

    async def list_media(self, ctx: commands.Context, kind: MediaKind) -> None:
        await ctx.send(
            view=self.build_list_view(
                kind, self.guild_id(ctx), prefix=await prefix_for(ctx)
            )
        )

    async def delete_media(self, ctx: commands.Context, kind: MediaKind, name: str) -> None:
        guild_id = self.guild_id(ctx)
        prefix = await prefix_for(ctx)
        display = clean_display_name(name)
        store = self.store(kind)

        if not display:
            await ctx.send(
                view=self.build_list_view(
                    kind,
                    guild_id,
                    prefix=prefix,
                    heading=f"# Command: {kind.delete_command}",
                )
            )
            return

        record = store.find(guild_id, display)
        if record is None:
            await ctx.send(
                view=simple_view(
                    f"❌ Nothing is saved under **{display}**.\n"
                    f"-# See everything saved here with `{prefix}{kind.list_command}`.",
                    timeout=120,
                )
            )
            return

        permissions = getattr(ctx.author, "guild_permissions", None)
        can_manage = bool(permissions and permissions.manage_messages)
        # Bot admins can clean up anything without Manage Messages in the
        # server, matching the Administrator bypass on `,delpic`.
        admin_ids = getattr(ctx.bot, "bot_admin_ids", None) or set()
        is_bot_admin = ctx.author.id in admin_ids or await ctx.bot.is_owner(ctx.author)
        if (
            ctx.author.id != record.get("author_id")
            and not can_manage
            and not is_bot_admin
        ):
            await ctx.send(
                view=simple_view(
                    "❌ Only the person who saved it (or someone with **Manage Messages**) "
                    f"can delete this {kind.singular}."
                )
            )
            return

        removed = store.delete(guild_id, display)
        file_count = len(removed.get("files") or []) if removed else 0
        await ctx.send(
            view=simple_view(
                f"🗑️ Deleted **{display}** ({file_count} file(s))."
            )
        )

    # --------------------------------------------------------
    # ,ss / ,sendpic / ,piclist / ,delpic — pictures
    # --------------------------------------------------------

    @commands.command(
        name="ss",
        aliases=["savepic", "savepics"],
        description="Save an attached image locally under a name so it can be sent again later.",
        help=(
            "Attach an image (or reply to a message that has one) with a name.\n"
            "The image is stored on the bot's disk and the message you sent it in "
            "is deleted, then `{prefix}sendpic <name>` posts it again later.\n"
            "Saving under a name that is already taken stacks the new image on top "
            "of the old ones instead of replacing them, up to 10 per name.\n"
            "Example: {prefix}ss frosty droppin a 2"
        ),
        extras={"example": "ss my picture  (with an image attached)"},
    )
    @commands.cooldown(2, 15, commands.BucketType.user)
    async def save_screenshot(self, ctx: commands.Context, *, name: str = "") -> None:
        await self.save_media(ctx, PICTURES, name)

    @commands.command(
        name="sendpic",
        aliases=["pic", "sendss"],
        description="Send a picture that was saved with {prefix}ss.",
        help=(
            "Posts the image(s) saved under the given name, directly as attachments.\n"
            "Example: {prefix}sendpic frosty"
        ),
        extras={"example": "sendpic my picture"},
    )
    @commands.cooldown(3, 15, commands.BucketType.user)
    async def sendpic(self, ctx: commands.Context, *, name: str = "") -> None:
        await self.send_media(ctx, PICTURES, name)

    @commands.command(
        name="piclist",
        aliases=["savedpics", "sslist", "pics"],
        description="List the pictures saved in this server.",
        help="Shows every name saved with `{prefix}ss` in this server.",
        extras={"example": "piclist"},
    )
    @commands.cooldown(2, 10, commands.BucketType.user)
    async def piclist(self, ctx: commands.Context) -> None:
        await self.list_media(ctx, PICTURES)

    @commands.command(
        name="delpic",
        aliases=["delpics", "deletess", "ssdel"],
        description="Delete a picture saved with {prefix}ss.",
        help=(
            "Deletes the saved image(s) under a name and removes them from disk.\n"
            "Only bot admins and server members with the Administrator role can.\n"
            "Example: {prefix}delpic frosty"
        ),
        extras={"example": "delpic my picture"},
    )
    @_bot_admin_or_administrator()
    @commands.cooldown(2, 15, commands.BucketType.user)
    async def delpic(self, ctx: commands.Context, *, name: str = "") -> None:
        await self.delete_media(ctx, PICTURES, name)

    # --------------------------------------------------------
    # ,sv / ,sendvid / ,vidlist / ,delvid — videos
    # --------------------------------------------------------

    @commands.command(
        name="sv",
        aliases=["savevid", "savevideo", "savevids"],
        description="Save an attached video locally under a name so it can be sent again later.",
        help=(
            "Attach a video (or reply to a message that has one) with a name.\n"
            "The video is stored on the bot's disk and the message you sent it in "
            "is deleted, then `{prefix}sendvid <name>` posts it again later.\n"
            "Saving under a name that is already taken stacks the new video on top "
            "of the old ones instead of replacing them, up to 5 per name.\n"
            "Example: {prefix}sv my clip"
        ),
        extras={"example": "sv my clip  (with a video attached)"},
    )
    @commands.cooldown(2, 15, commands.BucketType.user)
    async def save_video(self, ctx: commands.Context, *, name: str = "") -> None:
        await self.save_media(ctx, VIDEOS, name)

    @commands.command(
        name="sendvid",
        aliases=["vid", "sendvideo", "sendvids"],
        description="Send a video that was saved with {prefix}sv.",
        help=(
            "Posts the video(s) saved under the given name, directly as attachments.\n"
            "Example: {prefix}sendvid my clip"
        ),
        extras={"example": "sendvid my clip"},
    )
    @commands.cooldown(3, 15, commands.BucketType.user)
    async def sendvid(self, ctx: commands.Context, *, name: str = "") -> None:
        await self.send_media(ctx, VIDEOS, name)

    @commands.command(
        name="vidlist",
        aliases=["savedvids", "svlist", "vids"],
        description="List the videos saved in this server.",
        help="Shows every name saved with `{prefix}sv` in this server.",
        extras={"example": "vidlist"},
    )
    @commands.cooldown(2, 10, commands.BucketType.user)
    async def vidlist(self, ctx: commands.Context) -> None:
        await self.list_media(ctx, VIDEOS)

    @commands.command(
        name="delvid",
        aliases=["delvids", "deletevid", "svdel"],
        description="Delete a video saved with {prefix}sv.",
        help=(
            "Deletes the saved video(s) under a name and removes them from disk.\n"
            "Only the person who saved it (or someone with Manage Messages) can.\n"
            "Example: {prefix}delvid my clip"
        ),
        extras={"example": "delvid my clip"},
    )
    @commands.cooldown(2, 15, commands.BucketType.user)
    async def delvid(self, ctx: commands.Context, *, name: str = "") -> None:
        await self.delete_media(ctx, VIDEOS, name)

    # --------------------------------------------------------
    # ,sf / ,savefile / ,sendfile / ,filelist / ,delfile — audio
    # --------------------------------------------------------

    @commands.command(
        name="sf",
        aliases=["savefile", "savefiles", "saveaudio", "savemp3"],
        description="Save an attached audio file (mp3) locally under a name so it can be sent again later.",
        help=(
            "Attach an audio file (or reply to a message that has one) with a name.\n"
            "The file is stored on the bot's disk and the message you sent it in "
            "is deleted, then `{prefix}sendfile <name>` posts it again later.\n"
            "Saving under a name that is already taken stacks the new file on top "
            "of the old ones instead of replacing them, up to 5 per name.\n"
            "Example: {prefix}sf my song"
        ),
        extras={"example": "sf my song  (with an mp3 attached)"},
    )
    @commands.cooldown(2, 15, commands.BucketType.user)
    async def save_file(self, ctx: commands.Context, *, name: str = "") -> None:
        await self.save_media(ctx, AUDIO_FILES, name)

    @commands.command(
        name="sendfile",
        aliases=["sendaudio", "sendmp3"],
        description="Send an audio file that was saved with {prefix}sf.",
        help=(
            "Posts the audio file(s) saved under the given name, directly as attachments.\n"
            "Example: {prefix}sendfile my song"
        ),
        extras={"example": "sendfile my song"},
    )
    @commands.cooldown(3, 15, commands.BucketType.user)
    async def sendfile(self, ctx: commands.Context, *, name: str = "") -> None:
        await self.send_media(ctx, AUDIO_FILES, name)

    @commands.command(
        name="filelist",
        aliases=["savedfiles", "sflist", "audiofiles", "audiolist"],
        description="List the audio files saved in this server.",
        help="Shows every name saved with `{prefix}sf` in this server.",
        extras={"example": "filelist"},
    )
    @commands.cooldown(2, 10, commands.BucketType.user)
    async def filelist(self, ctx: commands.Context) -> None:
        await self.list_media(ctx, AUDIO_FILES)

    @commands.command(
        name="delfile",
        aliases=["delfiles", "deletefile", "sfdel"],
        description="Delete an audio file saved with {prefix}sf.",
        help=(
            "Deletes the saved audio file(s) under a name and removes them from disk.\n"
            "Only the person who saved it (or someone with Manage Messages) can.\n"
            "Example: {prefix}delfile my song"
        ),
        extras={"example": "delfile my song"},
    )
    @commands.cooldown(2, 15, commands.BucketType.user)
    async def delfile(self, ctx: commands.Context, *, name: str = "") -> None:
        await self.delete_media(ctx, AUDIO_FILES, name)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SavedMedia(bot))
