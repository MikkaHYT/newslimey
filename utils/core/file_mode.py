"""Global file-delivery mode for the media commands (``snip`` / ``erafind`` / ``cover``).

``local``
    Download the media once, cache it on disk, upload it to the R2 bucket and
    show the bucket's public URL (fast, and the link never changes).

``direct``
    Skip downloading/caching entirely and show the original direct file links.

The mode is a single bot-wide switch persisted in the ``bot_settings`` table
under ``guild_id 0`` (the same convention maintenance state uses) and mirrored
onto ``bot.file_mode`` so commands can read it synchronously.

The reference bot served ``local`` media through its web dashboard. This bot
has no dashboard, so the bucket takes its place — see
:mod:`utils.core.media_cache`. When the bucket is not configured the media
commands quietly fall back to ``direct`` links.
"""

from __future__ import annotations

from typing import Any

LOCAL = "local"
DIRECT = "direct"
MODES: tuple[str, ...] = (LOCAL, DIRECT)
DEFAULT_MODE = LOCAL
KEY = "file_mode"
_GUILD_ID = 0  # global rows live under guild_id 0

_LABELS = {
    LOCAL: "Local cache (bucket URLs)",
    DIRECT: "Direct links",
}


def normalize(mode: Any) -> str | None:
    """Return a valid mode string, or ``None`` when ``mode`` is unknown."""
    value = str(mode or "").strip().lower()
    return value if value in MODES else None


def label(mode: str) -> str:
    return _LABELS.get(mode, _LABELS[DEFAULT_MODE])


def get_mode(bot) -> str:
    """Synchronously read the active mode (falls back to the default)."""
    return normalize(getattr(bot, "file_mode", None)) or DEFAULT_MODE


async def load(bot) -> str:
    """Load the persisted mode into ``bot.file_mode`` and return it."""
    try:
        raw = await bot.db.fetchval(
            "SELECT value FROM bot_settings WHERE guild_id = ? AND key = ?;",
            _GUILD_ID,
            KEY,
        )
    except Exception:
        raw = None
    mode = normalize(raw) or DEFAULT_MODE
    bot.file_mode = mode
    return mode


async def set_mode(bot, mode: Any) -> str:
    """Persist ``mode`` and update the in-memory mirror."""
    resolved = normalize(mode) or DEFAULT_MODE
    bot.file_mode = resolved
    await bot.db.execute(
        "INSERT INTO bot_settings (guild_id, key, value) VALUES (?, ?, ?) "
        "ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value;",
        _GUILD_ID,
        KEY,
        resolved,
    )
    return resolved


async def toggle(bot) -> str:
    """Flip between ``local`` and ``direct`` and persist the new mode."""
    return await set_mode(bot, DIRECT if get_mode(bot) == LOCAL else LOCAL)
