"""Brux Vault — a numbered, shareable list of mp3s hosted in the R2 bucket.

``!bruxvault`` posts an embed titled **Brux Vault** whose entries are numbered
hyperlinks straight to the mp3s. ``!bruxvault add`` uploads the attached audio
to ``Brux Vault/`` in the bucket and appends its public link; ``!bruxvault
remove 1`` drops entry 1 and deletes the object behind it.

The list lives in ``bruxvault.json`` at the project root (gitignored), so it
survives restarts and cog reloads. Storage uses the same ``R2_*`` configuration
as the mixtape library (see :mod:`utils.core.r2`); until
``R2_PUBLIC_BASE_URL`` is set the links are presigned and expire.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
import discord
from discord.ext import commands

from config import EMBED_COLOR
from utils.core.r2 import R2Client, R2Config, config_note

logger = logging.getLogger("discord.client")

ROOT = Path(__file__).resolve().parent.parent
#: Where the numbered list is kept. Gitignored: it is live data, not config.
VAULT_PATH = ROOT / "bruxvault.json"
#: Folder inside the bucket, independent of ``R2_PREFIX`` (which is the
#: mixtape library's own folder).
VAULT_PREFIX = "Brux Vault"
#: Discord's upload ceiling for a bot without boosts.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
#: What counts as something the vault can hold an mp3-style link to.
AUDIO_SUFFIXES = (".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".aac")
#: Leaves room for the "…and N more" line under Discord's 4096-char limit.
DESCRIPTION_BUDGET = 3800


def _sanitize_filename(name: str, fallback: str = "track.mp3") -> str:
    """A filename the bucket and a link label can both hold."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name or "").strip()
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        return fallback
    stem, dot, suffix = name.rpartition(".")
    if dot and 1 <= len(suffix) <= 5:
        return f"{stem[:80]}.{suffix.lower()}"
    return name[:80]


def _link_label(name: str) -> str:
    """A filename safe inside ``[label](url)``."""
    text = discord.utils.escape_markdown(name or "")
    text = re.sub(r"[\[\]()]", "", text).strip()
    return text[:80] or "file"


def _is_audio(attachment) -> bool:
    """Whether an attachment looks like audio the vault can hold."""
    suffix = Path(str(getattr(attachment, "filename", "") or "")).suffix.lower()
    content_type = str(getattr(attachment, "content_type", "") or "").lower()
    return suffix in AUDIO_SUFFIXES or content_type.startswith("audio/")


def _audio_attachment(message) -> Optional[discord.Attachment]:
    """The first audio attachment on ``message``, or ``None``."""
    for attachment in getattr(message, "attachments", None) or []:
        if _is_audio(attachment):
            return attachment
    return None


def _format_size(size: int) -> str:
    megabytes = size / (1024 * 1024)
    return f"{megabytes:.1f} MB" if megabytes >= 1 else f"{size / 1024:.0f} KB"


class BruxVault(commands.Cog, name="BruxVault"):
    """The Brux Vault: numbered links to a pile of mp3s, add and remove by number."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        config = R2Config.from_env()
        self.client = R2Client(config) if config is not None else None
        self._entries = self._load_entries()
        self._lock = asyncio.Lock()

    def cog_unload(self) -> None:
        if self.client is not None:
            asyncio.create_task(self.client.close())

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _load_entries(self) -> list[dict]:
        try:
            raw = VAULT_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError:
            logger.exception("[BruxVault] Could not read %s", VAULT_PATH)
            return []

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.error("[BruxVault] %s is not valid JSON; starting empty", VAULT_PATH)
            return []

        if isinstance(data, dict):
            data = data.get("entries")
        if not isinstance(data, list):
            logger.error("[BruxVault] %s has no entry list; starting empty", VAULT_PATH)
            return []

        entries = []
        for item in data:
            if isinstance(item, dict) and item.get("key"):
                entries.append(item)
        return entries

    def _save_entries(self) -> None:
        tmp = VAULT_PATH.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps({"entries": self._entries}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, VAULT_PATH)
        except OSError:
            logger.exception("[BruxVault] Could not save %s", VAULT_PATH)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    async def _url_for(self, entry: dict) -> Optional[str]:
        """The bucket's link for an entry, preferring a fresh (non-expiring) one."""
        key = entry.get("key")
        if self.client is not None and key:
            url = await self.client.download_url(key, entry.get("filename"))
            if url:
                return url
        return entry.get("url")

    async def _vault_embed(self, prefix: str = "!") -> discord.Embed:
        embed = discord.Embed(title="Brux Vault", color=EMBED_COLOR)
        if not self._entries:
            embed.description = (
                "The vault is empty.\n"
                f"-# Add a file with `{prefix}bruxvault add` and an mp3 attached."
            )
            return embed

        lines: list[str] = []
        used = 0
        for index, entry in enumerate(self._entries, start=1):
            filename = (
                entry.get("filename")
                or Path(str(entry.get("key") or "")).name
                or f"file {index}"
            )
            label = _link_label(filename)
            url = await self._url_for(entry)
            line = f"**{index}.** [{label}]({url})" if url else f"**{index}.** {label}"
            if lines and used + len(line) + 1 > DESCRIPTION_BUDGET:
                hidden = len(self._entries) - (index - 1)
                lines.append(f"…and **{hidden}** more.")
                break
            lines.append(line)
            used += len(line) + 1

        embed.description = "\n".join(lines)
        embed.set_footer(
            text=f"{len(self._entries)} file{'s' if len(self._entries) != 1 else ''}"
            f" · add with {prefix}bruxvault add"
            f" · remove with {prefix}bruxvault remove <number>"
        )
        return embed

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    @commands.group(name="bruxvault", invoke_without_command=True)
    async def bruxvault(self, ctx: commands.Context) -> None:
        """Show the Brux Vault as an embed of numbered mp3 links."""
        if ctx.invoked_subcommand is not None:
            return
        await ctx.send(embed=await self._vault_embed(ctx.prefix or "!"))

    @bruxvault.command(name="add")
    @commands.check_any(
        commands.is_owner(),
        commands.has_permissions(manage_guild=True),
    )
    async def add(
        self,
        ctx: commands.Context,
        attachment: Optional[discord.Attachment] = None,
    ) -> None:
        """Add an mp3 to the vault: `!bruxvault add` with the file attached."""
        if self.client is None:
            note = config_note() or (
                "Set the `R2_*` environment variables so the vault has "
                "somewhere to store the files."
            )
            await ctx.send(f"❌ The vault's storage isn't configured. {note}")
            return

        # discord.py fills an attachment-typed parameter from the invoking
        # message, but it takes the first file whatever it is, so look for the
        # first actual audio file before giving up.
        source = attachment if attachment is not None and _is_audio(attachment) else None
        if source is None:
            source = _audio_attachment(ctx.message)
        if source is None:
            reference = getattr(ctx.message, "reference", None)
            if reference is not None:
                source = _audio_attachment(getattr(reference, "resolved", None))
        if source is None:
            await ctx.reply(
                "❌ Attach an mp3 (or another audio file) to the message — "
                f"`{ctx.prefix}bruxvault add` with the file on the same "
                "message, or reply to the file with that command.",
                delete_after=15,
            )
            return

        size = int(getattr(source, "size", 0) or 0)
        if size > MAX_UPLOAD_BYTES:
            await ctx.reply(
                f"❌ That file is {_format_size(size)} — the upload limit is "
                f"{_format_size(MAX_UPLOAD_BYTES)}.",
                delete_after=10,
            )
            return

        try:
            data = await source.read()
        except discord.HTTPException:
            logger.warning(
                "[BruxVault] Could not download %s",
                getattr(source, "filename", "?"),
                exc_info=True,
            )
            await ctx.reply("❌ I couldn't download that attachment.", delete_after=10)
            return
        if not data:
            await ctx.reply("❌ That file came through empty.", delete_after=10)
            return

        filename = _sanitize_filename(
            str(getattr(source, "filename", "") or ""), "track.mp3"
        )
        content_type = str(getattr(source, "content_type", "") or "")
        if not content_type.startswith("audio/"):
            content_type = mimetypes.guess_type(filename)[0] or "audio/mpeg"

        try:
            async with self._lock:
                key = await self._unique_key(filename)
                if not await self.client.put(key, data, content_type=content_type):
                    await ctx.send(
                        "❌ The upload to the bucket failed — try again in a moment."
                    )
                    return

                url = await self.client.download_url(key, filename)
                if not url:
                    await self.client.delete(key)
                    await ctx.send(
                        "❌ Uploaded, but I couldn't build a link for it. Check "
                        "`R2_PUBLIC_BASE_URL` and try again."
                    )
                    return

                self._entries.append(
                    {
                        "key": key,
                        "filename": filename,
                        "url": url,
                        "added_by": getattr(ctx.author, "id", None),
                        "added_by_name": str(ctx.author),
                        "added_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                self._save_entries()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            logger.exception("[BruxVault] Could not upload %s", filename)
            await ctx.send(
                "❌ The upload to the bucket failed — try again in a moment."
            )
            return

        await ctx.send(
            f"✅ Added **#{len(self._entries)}** — [{_link_label(filename)}]({url})"
        )

    @bruxvault.command(name="remove", aliases=["delete"])
    @commands.check_any(
        commands.is_owner(),
        commands.has_permissions(manage_guild=True),
    )
    async def remove(self, ctx: commands.Context, number: int) -> None:
        """Remove an entry by its number: `!bruxvault remove 1`."""
        async with self._lock:
            if not self._entries:
                await ctx.reply("❌ The vault is empty.", delete_after=10)
                return
            if not 1 <= number <= len(self._entries):
                await ctx.reply(
                    f"❌ There's no **#{number}** — the vault has "
                    f"**{len(self._entries)}** file(s).",
                    delete_after=10,
                )
                return

            entry = self._entries.pop(number - 1)
            self._save_entries()

        deleted: Optional[bool] = None
        if self.client is not None and entry.get("key"):
            try:
                deleted = await self.client.delete(entry["key"])
            except (aiohttp.ClientError, asyncio.TimeoutError):
                logger.exception(
                    "[BruxVault] Could not delete %s", entry.get("key")
                )
                deleted = False

        filename = entry.get("filename") or entry.get("key") or "file"
        message = f"🗑️ Removed **#{number}** — {_link_label(filename)}."
        if deleted is False:
            message += (
                "\n-# It was removed from the list, but the file couldn't be "
                "deleted from the bucket."
            )
        await ctx.send(message)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _unique_key(self, filename: str) -> str:
        """A bucket key for ``filename`` that doesn't overwrite an older file."""
        assert self.client is not None
        stem = Path(filename).stem
        suffix = Path(filename).suffix
        key = f"{VAULT_PREFIX}/{filename}"
        counter = 2
        while await self.client.head(key):
            key = f"{VAULT_PREFIX}/{stem} ({counter}){suffix}"
            counter += 1
            if counter > 500:
                break
        return key


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BruxVault(bot))
