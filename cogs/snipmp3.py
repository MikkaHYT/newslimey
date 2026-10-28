"""SnipMP3: cut a 30 second clip out of an uploaded track and DM it to you.

    !snipmp3 [start]          cut a 30 second mp3 out of an uploaded file

``!snipmp3`` works on a file the member attaches themselves (or replies to a
message carrying one). With no start point it cuts a random window; with one —
``!snipmp3 1:30`` or ``!snipmp3 90`` — the snippet begins there instead.

With no image the clip comes back as an mp3; attach an image as well (or reply
to one) and it comes back as an mp4 with that image held behind the audio. The
clip is sent to the member's DMs, never the channel, so the server never has a
video dumped into it.

The other half of the reference cog (``makesnip``, which renders a track out of
the mixtapes library) already exists here as ``!makesnip`` in ``cogs/music.py``,
so only the upload-driven command is ported.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import tempfile
from pathlib import Path

import discord
from discord.ext import commands
from discord.utils import DEFAULT_FILE_SIZE_LIMIT_BYTES

from config import EMBED_COLOR, PREFIX
from utils.core.views import SimpleLayout

logger = logging.getLogger("slimey.snipmp3")

#: Seconds of the song that end up in the clip.
SNIPPET_DURATION = 30

#: What ``!snipmp3`` accepts as the track to cut, and as an optional background.
AUDIO_EXTS = frozenset({".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".aac"})
IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def format_timestamp(seconds: int) -> str:
    minutes, secs = divmod(max(0, int(seconds)), 60)
    return f"{minutes}:{secs:02d}"


def parse_timestamp(value: str) -> int:
    """Seconds out of ``90``, ``1:30`` or ``1:02:03``.

    Raises :class:`ValueError` for anything that isn't a plain time, so the
    command can point the member back at the usage line.
    """
    value = (value or "").strip()
    parts = value.split(":")
    if not value or len(parts) > 3 or not all(part.strip().isdigit() for part in parts):
        raise ValueError(value)

    numbers = [int(part) for part in parts]
    if len(numbers) == 1:
        return numbers[0]
    if len(numbers) == 2:
        minutes, secs = numbers
        if secs >= 60:
            raise ValueError(value)
        return minutes * 60 + secs
    hours, minutes, secs = numbers
    if minutes >= 60 or secs >= 60:
        raise ValueError(value)
    return hours * 3600 + minutes * 60 + secs


def audio_attachment(message) -> discord.Attachment | None:
    """The first audio attachment on ``message``, or ``None``."""
    for attachment in getattr(message, "attachments", None) or []:
        suffix = Path(getattr(attachment, "filename", "") or "").suffix.lower()
        if suffix in AUDIO_EXTS:
            return attachment
        if (getattr(attachment, "content_type", "") or "").lower().startswith("audio/"):
            return attachment
    return None


def image_attachment(message) -> discord.Attachment | None:
    """The first image attachment on ``message``, or ``None``."""
    for attachment in getattr(message, "attachments", None) or []:
        suffix = Path(getattr(attachment, "filename", "") or "").suffix.lower()
        if suffix in IMAGE_EXTS:
            return attachment
        if (getattr(attachment, "content_type", "") or "").lower().startswith("image/"):
            return attachment
    return None


# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------
async def get_audio_duration_ffprobe(audio_path: str) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        audio_path,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        return float(stdout.decode().strip())
    except Exception as error:
        logger.warning("Could not probe the audio duration (%s); assuming 180s", error)
        return 180.0


async def render_snippet_video(
    audio_path: str,
    cover_path: str | None,
    start_offset: int,
    output_path: str,
) -> None:
    """Cut ``SNIPPET_DURATION`` seconds out of the audio, over the artwork.

    The image is looped for the length of the clip and scaled to a square with
    black bars rather than cropped, so nothing of the cover is ever cut off.
    """
    has_cover = bool(cover_path) and os.path.exists(cover_path) and os.path.getsize(cover_path) > 500
    if has_cover:
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start_offset),
            "-t", str(SNIPPET_DURATION),
            "-i", audio_path,
            "-loop", "1",
            "-i", cover_path,
            "-c:v", "libx264",
            "-tune", "stillimage",
            "-preset", "ultrafast",
            "-c:a", "aac",
            "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            "-vf", (
                "scale=720:720:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                "pad=720:720:(ow-iw)/2:(oh-ih)/2:color=black"
            ),
            "-r", "30",
            "-t", str(SNIPPET_DURATION),
            "-shortest",
            "-movflags", "+faststart",
            output_path,
        ]
    else:
        # No artwork: a plain dark square keeps the clip playable anyway.
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start_offset),
            "-t", str(SNIPPET_DURATION),
            "-i", audio_path,
            "-f", "lavfi",
            "-i", "color=c=0x14121d:s=720x720:r=30",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-c:a", "aac",
            "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            "-r", "30",
            "-t", str(SNIPPET_DURATION),
            "-shortest",
            "-movflags", "+faststart",
            output_path,
        ]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=180.0)
    except asyncio.TimeoutError:
        proc.kill()
        raise TimeoutError("ffmpeg timed out while encoding the snippet") from None

    if proc.returncode != 0:
        logger.error(
            "ffmpeg exited with code %d: %s",
            proc.returncode,
            stderr.decode(errors="replace"),
        )
        raise RuntimeError("ffmpeg failed to encode the snippet")

    logger.info("Snippet written to %s (%d bytes)", output_path, os.path.getsize(output_path))


async def cut_audio_snippet(
    audio_path: str,
    start_offset: int,
    output_path: str,
) -> None:
    """Cut ``SNIPPET_DURATION`` seconds out of the audio as an mp3.

    ``-ss`` sits before ``-i`` so ffmpeg seeks instead of decoding the whole
    file, which is what keeps this quick on a long upload.
    """
    cmd = [
        "ffmpeg", "-y",
        "-v", "error",
        "-ss", str(start_offset),
        "-t", str(SNIPPET_DURATION),
        "-i", audio_path,
        "-vn",
        "-c:a", "libmp3lame",
        "-b:a", "192k",
        output_path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
    except asyncio.TimeoutError:
        proc.kill()
        raise TimeoutError("ffmpeg timed out while cutting the snippet") from None

    if proc.returncode != 0:
        logger.error(
            "ffmpeg exited with code %d: %s",
            proc.returncode,
            stderr.decode(errors="replace"),
        )
        raise RuntimeError("ffmpeg failed to cut the snippet")

    logger.info("Snippet written to %s (%d bytes)", output_path, os.path.getsize(output_path))


def status_card(content: str) -> SimpleLayout:
    """A progress card: repainted in place as the pipeline moves along."""
    box = discord.ui.Container(accent_color=EMBED_COLOR)
    box.add_item(discord.ui.TextDisplay(content))
    return SimpleLayout(box)


class MakeSnip(commands.Cog):
    """Cut a 30 second mp3 (or mp4) out of an upload and DM it to the member."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        #: One render per member at a time — ffmpeg is not something to stack.
        self._busy: set[int] = set()

    @staticmethod
    async def _paint(message: discord.Message, content: str) -> None:
        """Repaint a status card, ignoring a message that is already gone."""
        try:
            await message.edit(view=status_card(content), attachments=[])
        except discord.HTTPException:
            pass

    async def _dm(
        self,
        user_id: int,
        *,
        embed: discord.Embed,
        files: list[discord.File],
        view: discord.ui.View,
    ) -> None:
        """Send one DM, resolving the user from cache or the API."""
        user = self.bot.get_user(user_id)
        if user is None:
            user = await self.bot.fetch_user(user_id)
        await user.send(embed=embed, files=files, view=view)

    @commands.command(name="snipmp3", aliases=("mp3snip", "snipsong"))
    async def snipmp3(self, ctx: commands.Context, start: str = "") -> None:
        """Cut a 30 second clip out of an uploaded track and DM it to you.

        Attach an mp3 (or reply to a message carrying one). With no start point
        a random 30 seconds is taken: `snipmp3`.
        Give one to begin the snippet there: `snipmp3 1:30` or `snipmp3 90`.

        Attach an image as well and the clip comes back as an mp4 with that
        image standing in as the background.
        """
        start_seconds: int | None = None
        if start.strip():
            try:
                start_seconds = parse_timestamp(start)
            except ValueError:
                await ctx.send(
                    view=status_card(
                        f"❌ `{start}` isn't a time — use `1:30`, `1:02:03` or `90`."
                    )
                )
                return

        # The audio, and any background, come from this message or the one it
        # replies to. ``source`` is whichever message actually carried the
        # audio, so it is the one cleaned up afterwards.
        source = ctx.message
        audio = audio_attachment(source)
        background = image_attachment(source)
        if audio is None and source.reference is not None:
            resolved = source.reference.resolved
            if not isinstance(resolved, discord.Message):
                try:
                    resolved = await ctx.channel.fetch_message(
                        source.reference.message_id
                    )
                except (discord.NotFound, discord.HTTPException):
                    resolved = None
            if resolved is not None:
                replied_audio = audio_attachment(resolved)
                if replied_audio is not None:
                    audio = replied_audio
                    source = resolved
                background = image_attachment(resolved) or background

        if audio is None:
            await ctx.send(
                view=status_card(
                    "❌ Attach an mp3 to the command (or reply to a message with "
                    "one).\n"
                    f"-# `{PREFIX}snipmp3` picks a random 30 seconds · "
                    f"`{PREFIX}snipmp3 1:30` starts at 1:30 · add an image for "
                    "an mp4."
                )
            )
            return

        if ctx.author.id in self._busy:
            await ctx.send(
                view=status_card("⏳ Your previous snippet is still rendering — hang on.")
            )
            return

        self._busy.add(ctx.author.id)
        title = Path(audio.filename or "song").stem or "snippet"
        status = await ctx.send(view=status_card(f"⬇️ Downloading **{title}**..."))
        try:
            sent = await self._build_upload(
                status=status,
                audio=audio,
                background=background,
                start_seconds=start_seconds,
                user_id=ctx.author.id,
            )
        finally:
            self._busy.discard(ctx.author.id)

        # Only the member's own message is touched, so replying to someone
        # else's track never deletes theirs.
        if sent and source.guild is not None and source.author.id == ctx.author.id:
            try:
                await source.delete()
            except discord.HTTPException:
                pass

    async def _build_upload(
        self,
        *,
        status: discord.Message,
        audio: discord.Attachment,
        background: discord.Attachment | None,
        start_seconds: int | None,
        user_id: int,
    ) -> bool:
        """Download the upload, cut the window and DM the clip.

        Returns whether a clip was actually sent, which is what decides if the
        source message is cleaned up afterwards.
        """
        title = Path(audio.filename or "song").stem or "snippet"
        clean_name = re.sub(r"[^\w\s-]", "", title).strip()
        clean_name = re.sub(r"\s+", "_", clean_name) or "snippet"

        try:
            data = await audio.read()
        except discord.HTTPException:
            await self._paint(status, "❌ I couldn't download that attachment.")
            return False
        suffix = Path(audio.filename or "").suffix.lower() or ".mp3"

        wants_video = background is not None
        kind = "video" if wants_video else "mp3"
        await self._paint(
            status, f"🎬 Cutting a {SNIPPET_DURATION} second {kind} of **{title}**..."
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            audio_path = os.path.join(tmp_dir, f"input{suffix}")
            with open(audio_path, "wb") as handle:
                handle.write(data)

            duration = await get_audio_duration_ffprobe(audio_path)
            if duration <= 0:
                duration = 180.0

            if start_seconds is not None:
                if start_seconds >= duration:
                    await self._paint(
                        status,
                        f"❌ The track is only `{format_timestamp(int(duration))}` "
                        f"long — `{format_timestamp(start_seconds)}` is past the end.",
                    )
                    return False
                start_sec = start_seconds
            elif duration > SNIPPET_DURATION:
                start_sec = random.randint(0, int(duration) - SNIPPET_DURATION)
            else:
                start_sec = 0
            end_sec = start_sec + SNIPPET_DURATION
            logger.info(
                "Upload snippet window %ss - %ss of %.2fs", start_sec, end_sec, duration
            )

            cover_path: str | None = None
            if wants_video:
                bg_suffix = Path(background.filename or "").suffix.lower() or ".png"
                cover_path = os.path.join(tmp_dir, f"background{bg_suffix}")
                try:
                    bg_data = await background.read()
                except discord.HTTPException:
                    bg_data = b""
                # An unreadable image just falls back to the plain dark square.
                with open(cover_path, "wb") as handle:
                    handle.write(bg_data)

            if wants_video:
                out_path = os.path.join(tmp_dir, f"{clean_name}_snippet.mp4")
                try:
                    await render_snippet_video(
                        audio_path=audio_path,
                        cover_path=cover_path,
                        start_offset=start_sec,
                        output_path=out_path,
                    )
                except Exception:
                    logger.exception("Rendering the uploaded snippet for '%s' failed", title)
                    await self._paint(status, "❌ ffmpeg couldn't render the snippet.")
                    return False
            else:
                out_path = os.path.join(tmp_dir, f"{clean_name}_snippet.mp3")
                try:
                    await cut_audio_snippet(audio_path, start_sec, out_path)
                except Exception:
                    logger.exception("Cutting the uploaded snippet for '%s' failed", title)
                    await self._paint(status, "❌ ffmpeg couldn't cut the snippet.")
                    return False

            if not os.path.exists(out_path) or not os.path.getsize(out_path):
                await self._paint(status, "❌ The rendered snippet came out empty.")
                return False

            limit = DEFAULT_FILE_SIZE_LIMIT_BYTES
            size = os.path.getsize(out_path)
            if size > limit:
                await self._paint(
                    status,
                    f"❌ The snippet is {size / (1024 * 1024):.1f} MB, over Discord's "
                    f"{limit / (1024 * 1024):.0f} MB DM upload limit.",
                )
                return False

            embed = discord.Embed(
                title=f"🎬 {title}",
                description=(
                    f"**Snippet**\n`{format_timestamp(start_sec)} - "
                    f"{format_timestamp(end_sec)}` of `{format_timestamp(int(duration))}`"
                ),
                color=EMBED_COLOR,
            )
            embed.set_footer(
                text=(
                    f"{SNIPPET_DURATION} seconds · from your upload"
                    if start_seconds is not None
                    else f"{SNIPPET_DURATION} seconds · cut at a random point"
                )
            )

            files = [discord.File(out_path, filename=os.path.basename(out_path))]
            if wants_video and cover_path and os.path.exists(cover_path):
                cover_name = f"cover{Path(cover_path).suffix.lower()}"
                embed.set_thumbnail(url=f"attachment://{cover_name}")
                files.append(discord.File(cover_path, filename=cover_name))

            try:
                await self._dm(
                    user_id, embed=embed, files=files, view=discord.ui.View()
                )
            except discord.Forbidden:
                await self._paint(
                    status,
                    "❌ I couldn't DM you the clip — open your privacy settings so "
                    "server members can message you, then run it again.",
                )
                return False
            except discord.HTTPException:
                logger.exception("Could not DM the uploaded '%s' snippet", title)
                await self._paint(status, "❌ Discord rejected that DM.")
                return False

        await self._paint(
            status,
            f"📬 Sent the **{title}** {kind} snippet to your DMs "
            f"(`{format_timestamp(start_sec)} - {format_timestamp(end_sec)}`).",
        )
        logger.info("DMed the uploaded '%s' %s snippet to %s", title, kind, user_id)
        return True


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MakeSnip(bot))
