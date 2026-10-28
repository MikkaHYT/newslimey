# cogs/repost.py
#
# Reposts short-form social videos (TikTok, Instagram, YouTube Shorts, X) into
# the channel. yt-dlp pulls the media plus its metadata, the video is uploaded
# first and an embed with the caption and engagement stats follows underneath.

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import discord
from discord.ext import commands

from config import EMBED_COLOR
from utils.core.errors import USAGE_ERRORS, send_command_error, send_usage_help
from utils.core.helpers import SendHelp
from utils.core.prefixes import prefix_for

try:  # yt-dlp is a declared dependency, but keep the cog importable without it.
    import yt_dlp
except Exception:  # pragma: no cover - yt-dlp missing
    yt_dlp = None

logger = logging.getLogger("slimey.repost")

# Discord's normal upload ceiling. Boosted guilds can be higher, handled below.
MAX_UPLOAD_SIZE = 25 * 1024 * 1024
MAX_DESCRIPTION = 4096
MAX_FOOTER = 2048
MAX_AUTHOR = 256
SOCKET_TIMEOUT = 30

#: Format selectors tried in order. The first is what most posts want; the rest
#: are there so a post the preferred shape can't be served for is still grabbed
#: instead of failing outright.
FORMAT_CHAIN: tuple[str, ...] = (
    "bv*+ba/b",
    "b[ext=mp4]/b",
    "b",
)
#: How often yt-dlp retries a flaky request, fragment or extraction.
NETWORK_RETRIES = 3

# TikTok (and some other sites) reject yt-dlp's default user agent, so pretend
# to be a desktop browser.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# Optional: point at a Netscape cookies file to unlock private/login-walled
# posts (Instagram especially). Set YTDLP_COOKIES in the environment.
COOKIES_PATH = os.environ.get("YTDLP_COOKIES", "").strip() or None

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

PLATFORMS: dict[str, dict[str, Any]] = {
    "tiktok": {"label": "TikTok", "hosts": ("tiktok.com",)},
    "instagram": {"label": "Instagram", "hosts": ("instagram.com", "instagr.am")},
    "youtube": {"label": "YouTube", "hosts": ("youtube.com", "youtu.be")},
    "twitter": {"label": "X", "hosts": ("twitter.com", "x.com")},
}

SUPPORTED_HINT = "TikTok, Instagram, YouTube Shorts or X"


def extract_url(text: Optional[str]) -> Optional[str]:
    """Pull the first http(s) link out of some text."""
    if not text:
        return None
    match = URL_RE.search(text)
    if not match:
        return None
    return match.group(0).rstrip(").,")


def detect_platform(url: str) -> Optional[str]:
    """Return the platform key for a link, or None when it's unsupported."""
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return None
    host = host.rsplit("@", 1)[-1].split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    for name, meta in PLATFORMS.items():
        for candidate in meta["hosts"]:
            if host == candidate or host.endswith("." + candidate):
                return name
    return None


def platform_label(platform: str) -> str:
    return PLATFORMS.get(platform, {}).get("label", platform.title())


def format_count(value: Any) -> Optional[str]:
    """Compact large counts (1500 -> 1.5K) for the footer."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    for threshold, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if number >= threshold:
            scaled = number / threshold
            return f"{scaled:.1f}".rstrip("0").rstrip(".") + suffix
    return str(number)


def format_posted_date(info: dict[str, Any]) -> Optional[str]:
    """Human-readable posted date from yt-dlp's timestamp/upload_date fields."""
    timestamp = info.get("timestamp")
    if timestamp:
        try:
            posted = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
            return posted.strftime("%b %d, %Y")
        except (TypeError, ValueError, OSError):
            pass

    upload_date = info.get("upload_date")
    if isinstance(upload_date, str) and len(upload_date) == 8 and upload_date.isdigit():
        try:
            posted = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
            return posted.strftime("%b %d, %Y")
        except ValueError:
            return None
    return None


def build_stats_footer(info: dict[str, Any]) -> str:
    """Likes / comments / shares followed by the posted date."""
    parts: list[str] = []
    for key, icon in (
        ("like_count", "❤️"),
        ("comment_count", "💬"),
        ("repost_count", "🔁"),
    ):
        value = format_count(info.get(key))
        if value:
            parts.append(f"{icon} {value}")

    posted = format_posted_date(info)
    if posted:
        parts.append(f"📅 {posted}")

    return "  ·  ".join(parts)


def build_embed(info: dict[str, Any], url: str, platform: str) -> discord.Embed:
    """Embed with the post's description and engagement stats."""
    description = str(info.get("description") or info.get("title") or "").strip()
    if not description:
        description = f"Reposted from {platform_label(platform)}"

    embed = discord.Embed(
        description=description[:MAX_DESCRIPTION],
        color=EMBED_COLOR,
        url=url,
    )

    author = (
        info.get("uploader")
        or info.get("channel")
        or info.get("uploader_id")
        or platform_label(platform)
    )
    embed.set_author(
        name=str(author)[:MAX_AUTHOR],
        url=str(info.get("uploader_url") or url),
    )

    footer = build_stats_footer(info)
    if footer:
        embed.set_footer(text=footer[:MAX_FOOTER])
    return embed


# ----------------------------------------------------------------------
# yt-dlp helpers (blocking, run in a thread)
# ----------------------------------------------------------------------


def _ydl_options(
    download: bool,
    outtmpl: Optional[str],
    *,
    format_selector: str = FORMAT_CHAIN[0],
) -> dict[str, Any]:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "socket_timeout": SOCKET_TIMEOUT,
        # Social sites drop connections constantly; these make yt-dlp try again
        # instead of handing the command back a half-read response.
        "retries": NETWORK_RETRIES,
        "fragment_retries": NETWORK_RETRIES,
        "extractor_retries": NETWORK_RETRIES,
        "file_access_retries": NETWORK_RETRIES,
        "geo_bypass": True,
        "http_headers": {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if COOKIES_PATH:
        options["cookiefile"] = COOKIES_PATH
    if download:
        options.update(
            {
                "format": format_selector,
                "merge_output_format": "mp4",
                "outtmpl": outtmpl,
                "restrictfilenames": True,
                "continuedl": True,
                "overwrites": True,
            }
        )
    return options


def _select_entry(info: dict[str, Any]) -> dict[str, Any]:
    """Flatten a one-item playlist/carousel down to its first entry."""
    entries = info.get("entries")
    if isinstance(entries, (list, tuple)):
        for entry in entries:
            if entry:
                return entry
    return info


def _find_downloaded_path(info: dict[str, Any], ydl: Any) -> Optional[str]:
    candidates: list[Optional[str]] = []
    for target in (info, *(info.get("entries") or [])):
        if not target:
            continue
        for entry in target.get("requested_downloads") or []:
            candidates.append(entry.get("filepath"))

    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate

    for target in (info, *(info.get("entries") or [])):
        if not target:
            continue
        try:
            candidate = ydl.prepare_filename(target)
        except Exception:
            candidate = None
        if candidate and Path(candidate).exists():
            return candidate
    return None


def fetch_metadata(url: str) -> dict[str, Any]:
    if yt_dlp is None:
        raise RuntimeError("yt-dlp is not installed")
    with yt_dlp.YoutubeDL(_ydl_options(False, None)) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        return {}
    return _select_entry(info)


def download_media(url: str, outtmpl: str) -> tuple[dict[str, Any], Optional[str]]:
    """Download ``url``, walking the format chain until one attempt lands.

    Each attempt writes into its own folder, so a half-finished file left by an
    earlier selector can never be mistaken for the finished download.
    """
    if yt_dlp is None:
        raise RuntimeError("yt-dlp is not installed")

    template = Path(outtmpl)
    last_error: Optional[Exception] = None
    for index, selector in enumerate(FORMAT_CHAIN):
        attempt_dir = template.parent / f"try{index}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        attempt_template = str(attempt_dir / template.name)
        try:
            with yt_dlp.YoutubeDL(
                _ydl_options(True, attempt_template, format_selector=selector)
            ) as ydl:
                info = ydl.extract_info(url, download=True)
                if not info:
                    continue
                path = _find_downloaded_path(info, ydl)
                if path:
                    return _select_entry(info), path
        except Exception as error:
            last_error = error
            logger.warning("[Repost] '%s' failed for %s: %s", selector, url, error)

    if last_error is not None:
        raise last_error
    return {}, None


class Repost(commands.Cog):
    """Reposts a TikTok, Instagram, YouTube Shorts or X post into the channel."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @staticmethod
    def _view(content: str, *, timeout: int = 60) -> discord.ui.LayoutView:
        view = discord.ui.LayoutView(timeout=timeout)
        container = discord.ui.Container(accent_color=EMBED_COLOR)
        container.add_item(discord.ui.TextDisplay(content))
        view.add_item(container)
        return view

    @commands.command(
        name="repost",
        aliases=["rp"],
        description="Reposts a TikTok, Instagram, YouTube Shorts or X post.",
        help=(
            "Downloads a social post and re-uploads it here, followed by an "
            "embed with its description, likes, comments, shares and posted "
            "date.\n\n"
            "**Usage**\n"
            "`repost <link>` — repost the linked post.\n"
            "You can also reply to a message containing a link.\n\n"
            "**Supported**\n"
            f"{SUPPORTED_HINT}."
        ),
        extras={"example": "repost https://www.tiktok.com/t/ZP83yCXMW/"},
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    @commands.bot_has_permissions(attach_files=True, embed_links=True)
    async def repost(self, ctx: commands.Context, *, link: str | None = None) -> None:
        url = extract_url(link)
        if url is None:
            reference = ctx.message.reference
            replied = reference.resolved if reference else None
            if isinstance(replied, discord.Message):
                url = extract_url(replied.content)

        if url is None:
            prefix = await prefix_for(ctx)
            await ctx.send(
                view=self._view(
                    "# Command: repost\n"
                    f"-# Repost a {SUPPORTED_HINT} post with its caption and stats.\n\n"
                    f"**Syntax** · `{prefix}repost <link>`\n"
                    f"**Example** · `{prefix}repost "
                    "https://www.tiktok.com/t/ZP83yCXMW/`\n"
                    "-# You can also reply to a message that contains a link."
                )
            )
            return

        platform = detect_platform(url)
        if platform is None:
            await ctx.send(
                view=self._view(
                    f"❌ That link isn't supported. Try a {SUPPORTED_HINT} post."
                )
            )
            return

        if yt_dlp is None:
            await ctx.send(view=self._view("❌ The downloader isn't available right now."))
            return

        label = platform_label(platform)
        status = await ctx.send(view=self._view(f"🔗 Fetching the {label} post..."))

        try:
            metadata = await asyncio.to_thread(fetch_metadata, url)
        except Exception:
            logger.exception("[Repost] Metadata extraction failed for %s", url)
            await self._edit(status, "❌ Couldn't read that post. It may be private or deleted.")
            return

        if not metadata:
            await self._edit(status, "❌ Couldn't read that post. It may be private or deleted.")
            return

        await self._edit(status, "⬇️ Downloading the video...")

        with tempfile.TemporaryDirectory() as tmp_dir:
            outtmpl = str(Path(tmp_dir) / "%(id)s.%(ext)s")
            try:
                download_info, path = await asyncio.to_thread(download_media, url, outtmpl)
            except Exception:
                logger.exception("[Repost] Download failed for %s", url)
                await self._edit(status, "❌ Failed to download that video.")
                return

            info = {**metadata, **(download_info or {})}
            embed = build_embed(info, url, platform)

            if not path or not Path(path).exists():
                logger.error("[Repost] No media file produced for %s", url)
                await self._edit(status, "❌ No downloadable video was found in that post.")
                return

            size = Path(path).stat().st_size
            limit = max(MAX_UPLOAD_SIZE, getattr(ctx.guild, "filesize_limit", 0) or 0)
            if size > limit:
                await self._delete(status)
                await ctx.send(
                    view=self._view(
                        f"⚠️ That video is **{size / (1024 * 1024):.1f} MB**, over this "
                        "channel's upload limit — here's the post instead."
                    )
                )
                await ctx.send(embed=embed)
                await self._delete(ctx.message)
                return

            file = discord.File(path, filename=Path(path).name)

            await self._delete(status)
            try:
                await ctx.send(file=file)
            except discord.HTTPException:
                logger.exception("[Repost] Discord rejected the video upload for %s", url)
                await ctx.send(view=self._view("❌ Discord rejected the video upload."))
                return

            await ctx.send(embed=embed)
            # The command message has done its job; drop it so the channel is
            # left with only the reposted media and its embed.
            await self._delete(ctx.message)
            logger.info("[Repost] Reposted %s (%s)", url, platform)

    @staticmethod
    async def _edit(message: discord.Message, content: str) -> None:
        try:
            await message.edit(view=Repost._view(content))
        except discord.HTTPException:
            pass

    @staticmethod
    async def _delete(message: discord.Message) -> None:
        try:
            await message.delete()
        except discord.HTTPException:
            pass

    @repost.error
    async def repost_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, (SendHelp, *USAGE_ERRORS)):
            if ctx.command:
                return await send_usage_help(ctx)

        await send_command_error(
            ctx,
            error,
            log_level=logging.INFO
            if isinstance(error, commands.CommandOnCooldown)
            else logging.ERROR,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Repost(bot))
