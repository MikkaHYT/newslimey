"""Public preview pages for bucket files, from Slimey Drive.

``!upload`` puts a member's file on the Cloudflare R2 bucket under
``uploads/<user>/<file>``. The drive (see ``API.md``) publishes that folder, so
asking its bot API about the key returns a page wrapped around the very same
object: an inline player, the file's size and type, a download button, and
OpenGraph tags so the link previews in Discord.

The drive never receives the bytes — it checks the object exists with a
``HeadObject`` and builds the URLs — so the bot keeps uploading to R2 exactly
as before and only the link it hands back changes.

Configuration is by environment variable, and the whole integration is optional:

    DRIVE_BASE_URL     public origin of the drive, e.g. ``https://drive.example.com``
    DRIVE_BOT_API_KEY  the shared secret from the drive's ``BOT_API_KEY``

With either unset the drive is off and callers keep the plain bucket link, so
a bot with no drive behaves as it always did. Every failure is logged and
answered with ``None`` for the same reason: a drive that is down must not cost
someone the upload that already succeeded.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger("slimey.drive")

TIMEOUT = aiohttp.ClientTimeout(total=10)
#: The API allows 300 calls a minute per key, so a 429 means something upstream
#: is off; one short retry covers a blip. 5xx is retried for the same reason —
#: Cloudflare answers a sleeping tunnel with a 502 before the drive sees it.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
RETRY_DELAY = 2.0


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


@dataclass(slots=True, frozen=True)
class DriveConfig:
    """Where the drive is and how to prove we may ask it for links."""

    base_url: str
    api_key: str

    @classmethod
    def from_env(cls) -> "DriveConfig | None":
        """The config, or ``None`` when the drive isn't set up."""
        base_url = _env("DRIVE_BASE_URL").rstrip("/")
        api_key = _env("DRIVE_BOT_API_KEY")
        if not (base_url and api_key):
            if base_url or api_key:
                # Half a configuration is worth saying out loud: the command
                # silently keeps the bucket link and looks like it worked.
                logger.warning(
                    "Drive is only half configured — set both `DRIVE_BASE_URL` "
                    "and `DRIVE_BOT_API_KEY` to hand out drive links."
                )
            return None
        return cls(base_url=base_url, api_key=api_key)


@dataclass(slots=True, frozen=True)
class DriveLink:
    """The drive's answer for one bucket key (see ``API.md`` §5)."""

    key: str
    name: str
    size: int
    mime: str
    category: str
    #: The link to post: the drive page, with player, info and a Download button.
    page_url: str
    #: The same bytes as an attachment, and as an inline stream (Range-capable).
    download_url: str
    raw_url: str
    #: 1200×630 JPEG preview — the image itself for pictures, else a card.
    thumbnail_url: str
    inline_viewable: bool = False
    embeddable_image: bool = False
    embeddable_media: bool = False


def _text(data: dict, key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


def _flag(data: dict, key: str) -> bool:
    return data.get(key) is True


def _as_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


async def link_for(key: str) -> DriveLink | None:
    """The drive page for bucket ``key``, or ``None`` when it is unavailable.

    ``key`` is sent exactly as uploaded — the API is case-sensitive about
    everything past the ``Uploads/`` folder, and the bot's files live under a
    lower-case ``uploads/`` the drive accepts as the same public folder.
    """
    config = DriveConfig.from_env()
    if config is None or not key:
        return None

    for attempt in range(2):
        link, status = await _request(config, key)
        if link is not None:
            return link
        if status not in RETRY_STATUSES or attempt:
            return None
        await asyncio.sleep(RETRY_DELAY)
    return None


async def _request(
    config: DriveConfig, key: str
) -> "tuple[DriveLink | None, int | None]":
    """One ``POST /api/bot/links`` call: the link (or ``None``), and status."""
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            async with session.post(
                f"{config.base_url}/api/bot/links",
                json={"path": key},
                headers={
                    "Authorization": f"Bearer {config.api_key}",
                    "Content-Type": "application/json",
                },
            ) as response:
                status = response.status
                # Read the body as text first: a proxy error (Cloudflare's 502
                # page) is HTML, and ``response.json()`` would raise instead of
                # reporting the failure.
                body = await response.text()
    except Exception:
        logger.warning("Drive link request failed for %s", key, exc_info=True)
        return None, None

    try:
        data = json.loads(body)
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}

    if status != 200:
        # Collapse whitespace: a proxy error page is a whole HTML document, and
        # one log line is enough to name the problem.
        detail = " ".join(str(data.get("error") or body).split())[:200]
        logger.warning(
            "Drive link failed for %s (%s): %s", key, status, detail or "no detail"
        )
        return None, status

    page_url = _text(data, "pageUrl") or _text(data, "url")
    if not page_url:
        logger.warning("Drive link for %s carried no page URL", key)
        return None, status

    return (
        DriveLink(
            key=_text(data, "key") or key,
            name=_text(data, "name") or key.rsplit("/", 1)[-1],
            size=_as_int(data.get("size")),
            mime=_text(data, "mime"),
            category=_text(data, "category"),
            page_url=page_url,
            download_url=_text(data, "downloadUrl"),
            raw_url=_text(data, "rawUrl"),
            thumbnail_url=_text(data, "thumbnailUrl"),
            inline_viewable=_flag(data, "inlineViewable"),
            embeddable_image=_flag(data, "embeddableImage"),
            embeddable_media=_flag(data, "embeddableMedia"),
        ),
        status,
    )
