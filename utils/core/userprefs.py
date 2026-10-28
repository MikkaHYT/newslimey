"""Per-member preferences: the switches a member flips for themselves.

Everything lives in the ``user_settings`` table as ``user_id, key, value``
rows, so adding another preference is a constant here plus one call site. The
values are JSON-encoded strings, which keeps a preference free to hold a bool
today and a number or text tomorrow without a schema change.

Only the member the row belongs to decides these - an owner can change someone
else's through ``;admin``, but nothing else in the bot should write here.

This module is deliberately dumb storage: what each key means, what it defaults
to and how to lay it out is declared in ``utils.core.preferences``, and the
commands read it from there rather than from a string literal here.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Sequence

logger = logging.getLogger("slimey.userprefs")

#: The storage key behind :data:`utils.core.preferences.POPUPS` - whether
#: level-up cards are posted for this member. The bot-wide switch that turns
#: them off for everybody lives in ``bot_settings`` instead, under
#: ``utils.core.settings.LEVEL_POPUPS_KEY``.
LEVEL_POPUPS = "level_popups"

#: The storage key behind :data:`cogs.timezone` - an IANA zone name
#: (``Europe/London``). Set with ``;tz set <place>``.
TIMEZONE = "timezone"

_TRUTHY = frozenset({"1", "true", "yes", "on", "enable", "enabled", "y"})
_FALSY = frozenset({"0", "false", "no", "off", "disable", "disabled", "n"})


def as_flag(value: Any, default: bool = False) -> bool:
    """Read a stored value as a boolean, falling back to ``default``."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    return default


def parse_flag(text: str) -> bool | None:
    """Turn ``on``/``off`` (and friends) into a bool, or ``None`` if unclear."""
    wanted = (text or "").strip().lower()
    if wanted in _TRUTHY:
        return True
    if wanted in _FALSY:
        return False
    return None


def _decode(raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


async def load(db: Any, user_id: int) -> dict[str, Any]:
    """Every preference a member has stored, keyed by name.

    One query rather than one per switch, for the callers that want the whole
    set (a level-up card, the preference hub).
    """
    try:
        rows = await db.execute(
            "SELECT key, value FROM user_settings WHERE user_id = %s",
            int(user_id),
        )
    except Exception:
        logger.exception("Could not read preferences for %s", user_id)
        return {}
    return {str(key): _decode(value) for key, value in rows}


async def clear(db: Any, user_id: int, keys: Sequence[str] | None = None) -> None:
    """Delete this member's preferences - one key, several, or all of them."""
    if keys is None:
        await db.execute(
            "DELETE FROM user_settings WHERE user_id = %s", int(user_id)
        )
        return

    wanted = [str(key) for key in keys]
    if not wanted:
        return
    # The keys are our own literals, never member input; only the values are
    # bound, which keeps this a single statement instead of a loop of deletes.
    placeholders = ", ".join("%s" for _ in wanted)
    await db.execute(
        f"DELETE FROM user_settings WHERE user_id = %s AND key IN ({placeholders})",
        int(user_id),
        *wanted,
    )


async def get(db: Any, user_id: int, key: str, default: Any = None) -> Any:
    """Read one preference. Missing rows fall back to ``default``."""
    try:
        raw = await db.fetchval(
            "SELECT value FROM user_settings WHERE user_id = %s AND key = %s",
            int(user_id),
            str(key),
        )
    except Exception:
        # A preference is never worth breaking a command over.
        logger.exception("Could not read preference %s for %s", key, user_id)
        return default
    if raw is None:
        return default
    return _decode(raw)


async def set(db: Any, user_id: int, key: str, value: Any) -> None:
    """Write one preference (``None`` stores an explicit null)."""
    await db.execute(
        "INSERT INTO user_settings (user_id, key, value) VALUES (%s, %s, %s) "
        "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
        int(user_id),
        str(key),
        json.dumps(value),
    )


async def get_flag(db: Any, user_id: int, key: str, default: bool = False) -> bool:
    """Read one preference as a boolean."""
    return as_flag(await get(db, user_id, key, None), default)


async def set_flag(db: Any, user_id: int, key: str, value: bool) -> None:
    """Write one preference as a boolean."""
    await set(db, user_id, key, bool(value))
