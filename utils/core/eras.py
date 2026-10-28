"""Juice WRLD era helpers: alias resolution and Discord emoji lookup.

The JuiceWRLD API tags every song with an ``era`` (``OUT``, ``DRFL``, ``GB&GR``
...). This module maps the names people actually type to those tags and finds
the matching Discord emoji for an era so song dropdowns can show a small logo
next to each result.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

logger = logging.getLogger("discord.client")

#: Attribute on the bot holding ``{emoji name (lower): discord.Emoji}`` for the
#: application's own emojis, filled once at startup by
#: :func:`load_application_emojis`.
_APP_EMOJIS_ATTR = "_era_application_emojis"

#: Canonical era name -> the Discord emoji name that stands in for it. The
#: emoji lives in the server (or as an application emoji); if it isn't there
#: the caller simply shows no emoji.
ERA_EMOJI_NAMES: dict[str, str] = {
    "OUT": "jw3",
    "GB&GR": "gbgr",
    "GB&GR (AE)": "gbgr",
    "GB&GR (5YAE)": "gbgr",
    "WOD": "wod",
    "DRFL": "drfl",
    "DRFL (BTV)": "drfl",
    "LND": "lnd",
    "LND (5YAE)": "lnd",
    "FD": "fd",
    "FD (CE)": "fd",
    "FD (EE)": "fd",
    "FD (DDE)": "fd",
    "TPNE": "tpne",
    "TPNE 2.0": "tpne",
    "TPP": "tpp",
    "TPP (EE)": "tpp",
    "POST": "post",
    "ND": "nd",
    "bdm": "bdm",
    "jw 999": "jw999",
    "HIH 999": "hih999",
    "jute": "jute",
    "afflictions": "afflictions",
    "Mainstream": "mainstream",
}

#: What people type -> the canonical era name. Every entry is optional sugar;
#: the resolver also matches the era's own name/description.
ERA_ALIASES: dict[str, str] = {
    # Outsiders (the user also calls it `jw3`)
    "outsiders": "OUT",
    "outsider": "OUT",
    "jw3": "OUT",
    "jw 3": "OUT",
    "out": "OUT",
    # Goodbye & Good Riddance
    "gbgr": "GB&GR",
    "gb&gr": "GB&GR",
    "goodbye": "GB&GR",
    "goodbye and good riddance": "GB&GR",
    "goodbye & good riddance": "GB&GR",
    # Death Race For Love
    "drfl": "DRFL",
    "death race": "DRFL",
    "deathrace": "DRFL",
    "death race for love": "DRFL",
    # WRLD On Drugs
    "wod": "WOD",
    "wrld": "WOD",
    "wrld on drugs": "WOD",
    "world on drugs": "WOD",
    # Legends Never Die
    "lnd": "LND",
    "legends never die": "LND",
    # Fighting Demons
    "fd": "FD",
    "fighting demons": "FD",
    # The Party Never Ends / The Pre-Party
    "tpne": "TPNE",
    "the party never ends": "TPNE",
    "tpp": "TPP",
    "the pre party": "TPP",
    "the pre-party": "TPP",
    "the preparty": "TPP",
    # Posthumous
    "post": "POST",
    "posthumous": "POST",
    # Earlier eras
    "nd": "ND",
    "nothings different": "ND",
    "nothing's different": "ND",
    "bdm": "bdm",
    "bingedrinkingmusic": "bdm",
    "jute": "jute",
    "juiced up the ep": "jute",
    "999": "jw 999",
    "jw 999": "jw 999",
    "juice wrld 999": "jw 999",
    "hih 999": "HIH 999",
    "heartbroken in hollywood": "HIH 999",
    "afflictions": "afflictions",
    "mainstream": "Mainstream",
}


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def resolve_era(query: str, eras: Iterable[dict[str, Any]] | None = None) -> str | None:
    """Turn a typed era into the canonical era name.

    Tries the hand-written aliases first, then the era's own name and
    description from the API, then a loose substring either way.
    """
    key = _normalize(query)
    if not key:
        return None

    for alias, name in ERA_ALIASES.items():
        if _normalize(alias) == key:
            return name

    era_list = list(eras or [])
    if era_list:
        for era in era_list:
            name = str(era.get("name") or "")
            description = str(era.get("description") or "")
            if _normalize(name) == key or _normalize(description) == key:
                return name
        for era in era_list:
            name = str(era.get("name") or "")
            description = str(era.get("description") or "")
            if key in _normalize(name) or key in _normalize(description):
                return name
        for alias, name in ERA_ALIASES.items():
            if _normalize(alias).startswith(key) or key.startswith(_normalize(alias)):
                return name

    return None


def song_era_name(song: Any) -> str:
    """The era name carried on a song dict (or ``""``)."""
    if not isinstance(song, dict):
        return ""
    era = song.get("era")
    if isinstance(era, dict):
        return str(era.get("name") or era.get("description") or "")
    return str(era or "")


async def load_application_emojis(bot: Any) -> dict[str, Any]:
    """Cache the bot application's own emojis on ``bot``, keyed by name.

    These are the emojis uploaded to the app's profile on the Discord
    Developer Portal. They belong to the application rather than one guild, so
    they render everywhere the bot does - including DMs - which makes them the
    right source for dropdown icons. Called once from ``setup_hook``; a failure
    is non-fatal and simply leaves :func:`emoji_for_era` on its guild fallback.
    """
    cache: dict[str, Any] = {}
    try:
        for emoji in await bot.fetch_application_emojis():
            name = getattr(emoji, "name", None)
            if name:
                cache.setdefault(str(name).lower(), emoji)
    except Exception:
        # Not logged in yet, or the app has no emoji scope.
        logger.debug("Application emoji lookup unavailable", exc_info=True)
    setattr(bot, _APP_EMOJIS_ATTR, cache)
    if cache:
        logger.info("Cached %d application emoji(s) for era dropdowns", len(cache))
    return cache


def application_emoji(bot: Any, name: str) -> Any:
    """The application's own emoji called *name*, or ``None``.

    The same cache :func:`emoji_for_era` reads, for callers that want an app
    emoji by name rather than by era. A guild emoji with that name is the
    fallback, so a server that happens to have one still renders it.
    """
    if bot is None or not name:
        return None
    cache = getattr(bot, _APP_EMOJIS_ATTR, None)
    if cache:
        emoji = cache.get(name.lower())
        if emoji is not None:
            return emoji
    for emoji in getattr(bot, "emojis", ()) or ():
        if getattr(emoji, "name", "").lower() == name.lower():
            return emoji
    return None


def application_emoji_text(bot: Any, name: str, fallback: str = "") -> str:
    """*name* as text a card can render (``<:radio:1234>``), or *fallback*.

    ``str`` of an application emoji is exactly the markdown a message accepts,
    so a card can show the app's own artwork inline; when the app has no such
    emoji the caller's fallback keeps the line readable.
    """
    emoji = application_emoji(bot, name)
    return str(emoji) if emoji is not None else fallback


def emoji_for_era(bot: Any, era_name: str | None):
    """The Discord emoji for an era, or ``None`` when it isn't available.

    The bot application's own emojis are preferred, then guild emojis are
    checked as a fallback. Accepts an :class:`discord.Emoji` /
    :class:`discord.PartialEmoji`, which is exactly what a
    :class:`discord.SelectOption` wants.
    """
    if bot is None or not era_name:
        return None
    emoji_name = ERA_EMOJI_NAMES.get(str(era_name))
    if not emoji_name:
        return None

    # Application emojis first: they belong to the app profile, so they show up
    # in every server (and in DMs), unlike a guild upload.
    cache = getattr(bot, _APP_EMOJIS_ATTR, None)
    if cache:
        emoji = cache.get(emoji_name.lower())
        if emoji is not None:
            return emoji

    for emoji in getattr(bot, "emojis", ()) or ():
        if getattr(emoji, "name", "").lower() == emoji_name.lower():
            return emoji
    return None


def emoji_for_song(bot: Any, song: Any):
    """Convenience: the era emoji for a song dict."""
    return emoji_for_era(bot, song_era_name(song))
