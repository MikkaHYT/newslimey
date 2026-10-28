"""Per-server reply mode for the AI cog (``,ai`` / replies to the bot).

``text``
    Answer in the channel as a normal message (the default).

``tts``
    Speak the answer with Fish Audio and reply with the generated mp3 instead
    of a wall of text.

The mode is a per-server switch stored in the shared ``bot_settings`` table
under :data:`KEY`, one row per guild (guild id ``0`` covers DMs and any other
context with no server). A server that has never set one answers as ``text``.
Like :mod:`utils.core.mp3_buttons`, it is read synchronously through the
in-memory :class:`~utils.core.settings.DashboardSettings` mirror, so the AI cog
never waits on the database to decide how to answer.
"""

from __future__ import annotations

from typing import Any

from utils.core.settings import GLOBAL_SCOPE

TEXT = "text"
TTS = "tts"
MODES: tuple[str, ...] = (TEXT, TTS)
DEFAULT_MODE = TEXT
KEY = "ai_mode"


_LABELS = {
    TEXT: "Text replies",
    TTS: "Spoken (TTS) replies",
}


def normalize(mode: Any) -> str | None:
    """Return a valid mode string, or ``None`` when ``mode`` is unknown."""
    value = str(mode or "").strip().lower()
    return value if value in MODES else None


def label(mode: str) -> str:
    return _LABELS.get(mode, _LABELS[DEFAULT_MODE])


def _scope(guild_id: int | None) -> int:
    """The ``bot_settings`` row a guild's mode lives under (0 outside a guild)."""
    return GLOBAL_SCOPE if guild_id is None else int(guild_id)


def get_mode(bot, guild_id: int | None = None) -> str:
    """Synchronously read the active mode for ``guild_id`` (default ``text``)."""
    settings = getattr(bot, "dash_settings", None)
    if settings is not None:
        mode = normalize(settings.stored(KEY, guild_id=_scope(guild_id)))
        if mode:
            return mode
    return DEFAULT_MODE


async def set_mode(bot, guild_id: int | None, mode: Any) -> str:
    """Persist ``mode`` for ``guild_id`` and return the resolved mode."""
    resolved = normalize(mode) or DEFAULT_MODE
    settings = getattr(bot, "dash_settings", None)
    if settings is not None:
        await settings.set_value(KEY, resolved, guild_id=_scope(guild_id))
    else:
        await bot.db.execute(
            "INSERT INTO bot_settings (guild_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value;",
            _scope(guild_id),
            KEY,
            resolved,
        )
    return resolved


async def toggle(bot, guild_id: int | None) -> str:
    """Flip ``guild_id`` between ``text`` and ``tts`` and persist the new mode."""
    return await set_mode(
        bot, guild_id, TTS if get_mode(bot, guild_id) == TEXT else TEXT
    )
