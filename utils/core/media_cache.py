"""Cache + host media for the ``snip`` / ``erafind`` / ``cover`` commands.

The reference bot handed cached media out through its web dashboard. This bot
has no dashboard, so the same "local cache" mode is backed by the same
Cloudflare R2 bucket the Mixtapes library reads from: a file is downloaded
once, kept in ``.cache/media`` and uploaded to ``Media/<kind>/<filename>``,
and the bucket's public URL is what the command shows.

Everything stays optional. When the ``R2_*`` variables are missing the media
commands fall back to direct links (see :mod:`utils.core.file_mode`), so
nothing here has to be configured to keep them working.

Configuration:

    R2_PUBLIC_BASE_URL   the bucket's public subdomain — required for hosting
    R2_MEDIA_PREFIX      bucket folder for cached media (default ``Media``)
    R2_MEDIA_CACHE_DIR   where files are cached locally
                         (default ``.cache/media``)
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
from pathlib import Path

from utils.core.r2 import R2Client, R2Config

logger = logging.getLogger("slimey.media_cache")

ROOT = Path(__file__).resolve().parent.parent.parent


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _cache_root() -> Path:
    value = _env("R2_MEDIA_CACHE_DIR")
    if not value:
        return ROOT / ".cache" / "media"
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


#: Local cache directories the media commands write into. The bucket keys
#: mirror these names, so a snippet cached at ``SNIPPET_CACHE_DIR/<hash>.mp4``
#: is served from ``Media/Snippets/<hash>.mp4``.
MEDIA_CACHE_DIR = _cache_root()
SNIPPET_CACHE_DIR = MEDIA_CACHE_DIR / "snippets"
COVER_CACHE_DIR = MEDIA_CACHE_DIR / "covers"

#: Bucket folder everything cached lands under.
MEDIA_PREFIX = (_env("R2_MEDIA_PREFIX", "Media").strip("/") or "Media")

_kinds: dict[str, str] = {"Snippets": "Snippets", "Covers": "Covers"}

_client: R2Client | None = None
_client_checked = False


def _client_instance() -> R2Client | None:
    """The bucket client, built once from the environment.

    Returns ``None`` when R2 isn't configured at all, which is the normal
    state for a bot that only ever uses direct links.
    """
    global _client, _client_checked
    if _client_checked:
        return _client
    _client_checked = True
    config = R2Config.from_env()
    if config is not None:
        _client = R2Client(config)
        logger.info("Cached media is hosted on R2 (%s)", config.label)
    return _client


def _key(kind: str, filename: str) -> str:
    return f"{MEDIA_PREFIX}/{_kinds.get(kind, kind)}/{filename}"


def _content_type(filename: str) -> str:
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def hosting_available() -> bool:
    """Whether cached media can actually be served (bucket + public domain)."""
    client = _client_instance()
    return client is not None and bool(client.config.public_base_url)


def media_url(kind: str, filename: str) -> str | None:
    """The bucket's public URL for a cached file, or ``None`` without hosting."""
    client = _client_instance()
    if client is None:
        return None
    return client.public_url(_key(kind, filename))


def snippet_media_url(filename: str) -> str | None:
    return media_url("Snippets", filename)


def cover_media_url(filename: str) -> str | None:
    return media_url("Covers", filename)


async def ensure_uploaded(kind: str, filename: str, path: Path) -> bool:
    """Upload ``path`` to the bucket under ``kind`` if it isn't already there.

    A ``HEAD`` first keeps a repeat request (or a restart that still has the
    local cache) from re-uploading a file the bucket already holds.
    """
    client = _client_instance()
    if client is None:
        return False

    key = _key(kind, filename)
    if await client.head(key) is not None:
        return True

    try:
        data = await asyncio.to_thread(Path(path).read_bytes)
    except OSError:
        logger.warning("Could not read cached media %s", path, exc_info=True)
        return False

    return await client.put(key, data, _content_type(filename))


async def ensure_media(kind: str, filename: str, path: Path) -> str | None:
    """Upload a cached file if needed and return the URL to show for it.

    Only public-domain URLs are handed back: a presigned link would expire
    while it sat in the snippet/cover manifest, which caches results across
    restarts.
    """
    client = _client_instance()
    if client is None or not client.config.public_base_url:
        return None
    await ensure_uploaded(kind, filename, path)
    return client.public_url(_key(kind, filename))
