"""Per-server ``mp3 button mode``: link the audio, or attach it.

The catalog commands (``leak`` / ``session`` / ``stem`` / ``freestyle`` /
``remaster`` ...) normally hand out an mp3 as a *link* button that opens the
file in a browser tab. With this mode on, pressing one of those mp3 buttons
instead downloads the file and drops it straight into an ephemeral reply, so
the member never leaves Discord. Every button that isn't an mp3 (a zip, a
cover, the tracker) stays a normal link button either way.

The switch is per-server, stored in the shared ``bot_settings`` table under
:data:`MP3_BUTTON_MODE_KEY` and flipped with ``!mp3buttonmode``.
"""

from __future__ import annotations

import io
import logging
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

import aiohttp
import discord

logger = logging.getLogger("slimey.mp3buttons")

#: Per-server: pressing an mp3 download button attaches the file instead of
#: opening its link.
MP3_BUTTON_MODE_KEY = "mp3_button_mode"

#: Extensions the attach mode treats as an mp3. Only these become buttons that
#: download and attach; every other file keeps its plain link button.
MP3_EXTENSIONS = frozenset({".mp3"})

#: Extensions replaced before the ``.mp3`` suffix is appended, so an original
#: ``Song.wav`` becomes ``Song.mp3`` rather than ``Song.wav.mp3``.
AUDIO_EXTENSIONS = frozenset(
    {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".aac"}
)

#: Refuse anything bigger than this, even if the server would allow it.
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024

_DEFAULT_FILENAME = "audio.mp3"

#: Anything that would be awkward in a filename goes.
_UNSAFE_FILENAME = re.compile(r"[^\w.\- ]+")

#: ``!mp3buttonmode on`` / ``off`` spellings accepted on the command line.
ON_VALUES = frozenset({"on", "enable", "enabled", "true", "yes", "attach"})
OFF_VALUES = frozenset({"off", "disable", "disabled", "false", "no", "link"})
STATUS_VALUES = frozenset({"status", "state", "?"})


def enabled(bot: Any, guild_id: Optional[int]) -> bool:
    """Whether attach mode is on for ``guild_id``.

    Missing store or a direct-message context both mean "off", so the catalog
    commands keep their plain link buttons.
    """
    if guild_id is None:
        return False
    settings = getattr(bot, "dash_settings", None)
    if settings is None:
        return False
    try:
        return settings.flag(MP3_BUTTON_MODE_KEY, False, guild_id=int(guild_id))
    except Exception:
        logger.exception("[mp3buttons] Could not read the mp3 button mode")
        return False


def is_mp3_url(url: Optional[str]) -> bool:
    """Whether a URL points at an mp3, judged by its path extension."""
    if not url:
        return False
    try:
        suffix = Path(urlparse(str(url)).path).suffix.lower()
    except Exception:
        return False
    return suffix in MP3_EXTENSIONS


def sanitize_filename(name: str, fallback: str = _DEFAULT_FILENAME) -> str:
    """Make ``name`` safe to hand to Discord as an attachment name."""
    candidate = _UNSAFE_FILENAME.sub("_", str(name or "").strip())
    candidate = candidate.strip("._ ")
    if not candidate:
        candidate = fallback
    return candidate[:120]


def mp3_filename(name: str, fallback: str = "audio") -> str:
    """``name`` as an ``.mp3`` attachment name, keeping its original stem.

    A trailing audio extension is replaced rather than doubled up, so
    ``Song.wav`` becomes ``Song.mp3`` and ``Song.mp3`` stays put.
    """
    base = sanitize_filename(name, fallback)
    lowered = base.lower()
    for extension in AUDIO_EXTENSIONS:
        if lowered.endswith(extension):
            base = base[: -len(extension)]
            break
    return f"{sanitize_filename(base, fallback)}.mp3"


def filename_from_url(url: str, fallback: str = _DEFAULT_FILENAME) -> str:
    """The basename of ``url``, cleaned up for use as an attachment name."""
    try:
        raw = unquote(urlparse(str(url)).path)
        name = Path(raw).name
    except Exception:
        name = ""
    return sanitize_filename(name, fallback)


class _TooLarge(Exception):
    """Raised when a file is bigger than we are willing to pull down."""


async def _download(url: str) -> bytes:
    timeout = aiohttp.ClientTimeout(total=180)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url) as response:
            response.raise_for_status()
            if response.content_length and response.content_length > MAX_DOWNLOAD_BYTES:
                raise _TooLarge
            data = await response.read()
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise _TooLarge
    return data


class Mp3DownloadButton(discord.ui.Button):
    """An mp3 download button that attaches the file in an ephemeral reply.

    On press it defers an ephemeral "thinking" message, fetches the file, and
    then replaces that message with the mp3 itself.
    """

    def __init__(
        self,
        *,
        url: str,
        filename: Optional[str] = None,
        label: str = "Download",
        emoji: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=label,
            emoji=emoji or "📥",
            **kwargs,
        )
        # Deliberately *not* named ``url``: a non-link button that carried a
        # URL would be serialised to Discord as style 2 *and* a url, which
        # Discord rejects outright.
        self.file_url = str(url)
        # Always land on an ``.mp3`` name: a download URL like ``.../download/?
        # path=Song.mp3`` has no usable basename (it would be ``download``).
        self.filename = filename or mp3_filename(
            filename_from_url(self.file_url), "audio"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        # Answer instantly so the press never looks dead: the user gets an
        # ephemeral "thinking" reply the moment they click, and that same reply
        # turns into the mp3 once the download finishes.
        await interaction.response.send_message(
            "⏳ **Thinking…** fetching the mp3, hang tight.",
            ephemeral=True,
        )

        try:
            data = await _download(self.file_url)
        except _TooLarge:
            await interaction.edit_original_response(
                content=(
                    f"❌ That file is too big to attach.\n"
                    f"[Download it here]({self.file_url})"
                )
            )
            return
        except Exception:
            logger.exception("[mp3buttons] Could not fetch %s", self.file_url)
            await interaction.edit_original_response(
                content=(
                    f"❌ I couldn't fetch that file.\n"
                    f"[Download it here]({self.file_url})"
                )
            )
            return

        try:
            await interaction.edit_original_response(
                content=None,
                attachments=[
                    discord.File(io.BytesIO(data), filename=self.filename)
                ],
            )
        except discord.HTTPException:
            logger.exception(
                "[mp3buttons] Discord rejected the upload for %s", self.file_url
            )
            await interaction.edit_original_response(
                content=(
                    f"❌ Discord wouldn't accept that file.\n"
                    f"[Download it here]({self.file_url})"
                )
            )


def make_download_button(
    bot: Any,
    *,
    guild_id: Optional[int],
    label: str,
    url: Optional[str],
    is_mp3: Optional[bool] = None,
    filename: Optional[str] = None,
    emoji: Optional[str] = None,
) -> Optional[discord.ui.Button]:
    """Build a download button, or ``None`` when ``url`` is empty.

    Normally this is a plain link button. When the server has mp3 button mode
    on *and* the target is an mp3 (``is_mp3``, inferred from the URL when not
    given), it is an :class:`Mp3DownloadButton` instead.
    """
    if not url:
        return None
    url = str(url)
    if is_mp3 is None:
        is_mp3 = is_mp3_url(url)
    if is_mp3 and enabled(bot, guild_id):
        return Mp3DownloadButton(
            url=url,
            filename=filename,
            label=label,
            emoji=emoji,
        )
    return discord.ui.Button(label=label, style=discord.ButtonStyle.link, url=url)
