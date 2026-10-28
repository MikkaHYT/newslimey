"""The member-facing preference catalogue, and the ping rows behind it.

Everything a member can switch on or off for themselves is declared once, in
:data:`PREFERENCES`, and every consumer reads it from here:

* ``cogs/preferences.py`` renders the ``;prefs`` hub straight off the table, so
  a new switch appears in the menu with no view changes.
* ``cogs/levels.py`` reads the notification switches when it draws a level-up
  card.
* ``cogs/economy.py`` asks :func:`reminder_allowed` before DMing somebody, and
  :func:`schedule` is called whenever a claim or a shift starts a cooldown.

Storage is the generic ``user_settings`` table (``utils.core.userprefs``), so a
preference is a name, a default and a list of options - nothing more. Values are
either booleans (two options) or short strings (three or more), and
:meth:`Preference.coerce` maps anything unexpected back to the default, which
means a hand-edited row can never wedge a command.

The second half of the module is the ready-ping side: :func:`schedule` records
"this member can claim again at T", the loop in the preferences cog picks due
rows up and DMs whoever asked for that ping, and :func:`clear_reminder` drops
the row so it only ever fires once.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import discord

from utils.core import userprefs

logger = logging.getLogger("slimey.preferences")

# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------
#: Registry names. These are what commands take and what :func:`read_all` keys
#: its result by; the *storage* key lives on each :class:`Preference`.
POPUPS = "popups"
LEVELUP_DM = "levelup_dm"
ACHIEVEMENT_NOTICES = "achievements"
CLAIM_REMINDERS = "claim_reminders"
WORK_REMINDERS = "work_reminders"
ROB_ALERTS = "rob_alerts"
COMPACT_MONEY = "compact_money"
HIDE_WORTH = "hide_worth"


@dataclass(frozen=True, slots=True)
class Option:
    """One value a preference can hold, and how its button should look."""

    value: Any
    label: str
    emoji: str
    style: discord.ButtonStyle
    hint: str = ""


#: The two shapes almost every switch has.
ON = Option(value=True, label="On", emoji="🔔", style=discord.ButtonStyle.success)
OFF = Option(
    value=False, label="Off", emoji="🔕", style=discord.ButtonStyle.secondary
)


@dataclass(frozen=True, slots=True)
class Preference:
    """One switch: what it stores, what it means, and how to show it."""

    key: str
    label: str
    emoji: str
    description: str
    default: Any
    options: tuple[Option, ...]
    aliases: tuple[str, ...] = ()

    # -- shape -----------------------------------------------------------
    @property
    def is_flag(self) -> bool:
        """Whether this is a plain on/off switch."""
        return isinstance(self.options[0].value, bool)

    def coerce(self, value: Any) -> Any:
        """Force ``value`` into one of this preference's options."""
        if self.is_flag:
            return userprefs.as_flag(value, default=bool(self.default))
        text = "" if value is None else str(value).strip().lower()
        for option in self.options:
            if str(option.value).lower() == text:
                return option.value
        return self.default

    def option(self, value: Any) -> Option | None:
        """The option matching ``value``, after coercion."""
        wanted = self.coerce(value)
        for option in self.options:
            if option.value == wanted:
                return option
        return None

    def label_for(self, value: Any) -> str:
        """``On``/``Off``/``Follow the server`` - the human word for a value."""
        option = self.option(value)
        return option.label if option is not None else str(value)

    def emoji_for(self, value: Any) -> str:
        option = self.option(value)
        return option.emoji if option is not None else self.emoji

    def next_label(self, value: Any) -> str:
        """The label of some other option, for a "turn it the other way" hint."""
        current = self.coerce(value)
        for option in self.options:
            if option.value != current:
                return option.label
        return self.label_for(current)


#: Every switch, in the order the hub lists them. A new preference only needs an
#: entry here to be listed, described, toggled and reset.
PREFERENCES: dict[str, Preference] = {
    POPUPS: Preference(
        key="level_popups",
        label="Level-up messages",
        emoji="🎉",
        description="Post a card in the channel when you level up",
        default=True,
        options=(ON, OFF),
        aliases=("levelups", "levelup", "announce"),
    ),
    LEVELUP_DM: Preference(
        key="levelup_dm",
        label="Level-up card in DMs",
        emoji="📬",
        description="Send your level-up card to you instead of the channel",
        default=False,
        options=(ON, OFF),
        aliases=("dm", "dms", "levelupdm"),
    ),
    ACHIEVEMENT_NOTICES: Preference(
        key="achievement_notices",
        label="Achievement notices",
        emoji="🏆",
        description="Show which achievements a payout just unlocked",
        default=True,
        options=(ON, OFF),
        aliases=("achievement", "ach", "achievements"),
    ),
    CLAIM_REMINDERS: Preference(
        key="claim_reminders",
        label="Reward-ready pings",
        emoji="📅",
        description="DM you the moment daily, weekly or monthly is claimable",
        default=False,
        options=(ON, OFF),
        aliases=("claims", "claimreminders", "rewards", "daily"),
    ),
    WORK_REMINDERS: Preference(
        key="work_reminders",
        label="Shift-end ping",
        emoji="🛠️",
        description="DM you when your work cooldown runs out",
        default=False,
        options=(ON, OFF),
        aliases=("work", "shift", "shifts"),
    ),
    ROB_ALERTS: Preference(
        key="rob_alerts",
        label="Rob alerts",
        emoji="🥷",
        description="DM you when somebody successfully robs you",
        default=True,
        options=(ON, OFF),
        aliases=("rob", "robs", "steal", "steals"),
    ),
    COMPACT_MONEY: Preference(
        key="compact_money",
        label="Compact money",
        emoji="🔢",
        description="Show 12.5k instead of 12,500 on your own cards",
        default=False,
        options=(ON, OFF),
        aliases=("compact", "shortmoney", "numbers"),
    ),
    HIDE_WORTH: Preference(
        key="hide_worth",
        label="Hide your money",
        emoji="🙈",
        description="Leave money off your rank and level-up cards",
        default=False,
        options=(ON, OFF),
        aliases=("worth", "networth", "hideworth", "privacy"),
    ),
}

#: Words that mean "back to the defaults" on the command line.
RESET_WORDS = frozenset({"reset", "clear", "default", "defaults", "wipe"})
_TOGGLE_WORDS = frozenset({"toggle", "flip", "switch", "invert"})


def _norm(text: str) -> str:
    """Lowercase and strip punctuation so inputs can be typed loosely."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def names() -> tuple[str, ...]:
    return tuple(PREFERENCES)


def spec(pref: Preference | str) -> Preference:
    """Look a preference up by registry name, or hand back what we got."""
    if isinstance(pref, Preference):
        return pref
    found = PREFERENCES.get(str(pref))
    if found is None:
        raise KeyError(f"Unknown preference {pref!r}")
    return found


def resolve(query: str) -> Preference | None:
    """Match typed text against a preference's name, key, label or aliases."""
    wanted = _norm(query)
    if not wanted:
        return None
    for name, pref in PREFERENCES.items():
        candidates = {name, pref.key, pref.label, *pref.aliases}
        if wanted in {_norm(candidate) for candidate in candidates}:
            return pref
    # Last resort: a unique prefix, so `;prefs level` is enough.
    matches = [
        pref
        for name, pref in PREFERENCES.items()
        if _norm(name).startswith(wanted) or _norm(pref.label).startswith(wanted)
    ]
    return matches[0] if len(matches) == 1 else None


def is_toggle_token(token: str) -> bool:
    """Whether ``token`` means "flip it" rather than naming a value."""
    return _norm(token) in _TOGGLE_WORDS


def is_reset_token(token: str) -> bool:
    """Whether ``token`` means "put it back to the default"."""
    return _norm(token) in RESET_WORDS


def token_value(pref: Preference, token: str) -> Any | None:
    """Turn a typed value into one of ``pref``'s options, or ``None``."""
    wanted = _norm(token)
    if not wanted:
        return None
    for option in pref.options:
        if wanted in {_norm(option.label), _norm(str(option.value))}:
            return option.value
    if pref.is_flag:
        return userprefs.parse_flag(wanted)
    return None


# ---------------------------------------------------------------------------
# Reading and writing
# ---------------------------------------------------------------------------
async def read(db: Any, user_id: int, pref: Preference | str) -> Any:
    """This member's value for one preference, defaulted and coerced."""
    found = spec(pref)
    return found.coerce(await userprefs.get(db, user_id, found.key, None))


async def read_all(db: Any, user_id: int) -> dict[str, Any]:
    """Every preference for one member, in a single query.

    Keyed by registry name and never missing an entry, so callers can index
    ``values[POPUPS]`` without a fallback.
    """
    stored = await userprefs.load(db, user_id)
    return {
        name: pref.coerce(stored.get(pref.key, pref.default))
        for name, pref in PREFERENCES.items()
    }


async def write(db: Any, user_id: int, pref: Preference | str, value: Any) -> Any:
    """Store one preference, coerced. Returns what was actually written."""
    found = spec(pref)
    chosen = found.coerce(value)
    await userprefs.set(db, user_id, found.key, chosen)
    return chosen


async def toggle(db: Any, user_id: int, pref: Preference | str) -> Any:
    """Flip a two-option switch and return its new value."""
    found = spec(pref)
    current = await read(db, user_id, found)
    for option in found.options:
        if option.value != current:
            return await write(db, user_id, found, option.value)
    return current


async def reset(db: Any, user_id: int, pref: Preference | str | None = None) -> None:
    """Clear one preference, or every one, back to the defaults."""
    if pref is None:
        await userprefs.clear(db, user_id, [p.key for p in PREFERENCES.values()])
        return
    await userprefs.clear(db, user_id, [spec(pref).key])


def is_on(value: Any) -> bool:
    """Whether a stored value reads as "yes"."""
    return userprefs.as_flag(value, False)


def differs(values: dict[str, Any], name: str) -> bool:
    """Whether the member has moved one switch off its default.

    ``values`` is what :func:`read_all` returned, so it is keyed by registry
    name rather than by storage key.
    """
    pref = spec(name)
    return values.get(name, pref.default) != pref.default


def changed_names(values: dict[str, Any]) -> list[str]:
    """Registry names the member has moved off their defaults."""
    return [name for name in PREFERENCES if differs(values, name)]


# ---------------------------------------------------------------------------
# Ready pings
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Ping:
    """A cooldown that is worth telling somebody about."""

    kind: str
    pref: str
    emoji: str
    body: str


#: ``kind`` is what ``cogs/economy.py`` schedules; the loop here reads it back.
PINGS: dict[str, Ping] = {
    "daily": Ping(
        kind="daily",
        pref=CLAIM_REMINDERS,
        emoji="📅",
        body="your **daily** is ready",
    ),
    "weekly": Ping(
        kind="weekly",
        pref=CLAIM_REMINDERS,
        emoji="🗓️",
        body="your **weekly** is ready",
    ),
    "monthly": Ping(
        kind="monthly",
        pref=CLAIM_REMINDERS,
        emoji="📆",
        body="your **monthly** is ready",
    ),
    "work": Ping(
        kind="work",
        pref=WORK_REMINDERS,
        emoji="🛠️",
        body="your shift is over — `work` pays again",
    ),
}


async def schedule(
    db: Any, user_id: int, kind: str, ready_at: datetime
) -> None:
    """Remember that ``user_id``'s ``kind`` cooldown ends at ``ready_at``.

    The timestamp is handed to the database as a datetime and never formatted
    here: ``Database._prepare_params`` turns it into the same ``isoformat()``
    string the comparison in :func:`due_reminders` binds, and a hand-rolled
    format would sort differently against it (``2026-01-01 12:00`` before
    ``2026-01-01T12:00``), which would make same-day cooldowns look due.

    Truncated to whole seconds so every stored stamp has the same shape, which
    is what lets SQLite compare these as plain strings.
    """
    if kind not in PINGS:
        raise KeyError(f"Unknown ping kind {kind!r}")
    stamp = ready_at.astimezone(timezone.utc).replace(microsecond=0)
    await db.execute(
        "INSERT INTO user_reminders (user_id, kind, ready_at) VALUES (%s, %s, %s) "
        "ON CONFLICT(user_id, kind) DO UPDATE SET ready_at = excluded.ready_at",
        int(user_id),
        str(kind),
        stamp,
    )


async def clear_reminder(db: Any, user_id: int, kind: str) -> None:
    """Drop a ping row, so it can never fire twice."""
    await db.execute(
        "DELETE FROM user_reminders WHERE user_id = %s AND kind = %s",
        int(user_id),
        str(kind),
    )


async def due_reminders(
    db: Any, *, now: datetime | None = None, limit: int = 100
) -> list[tuple[int, str]]:
    """``(user_id, kind)`` pairs whose cooldown has run out.

    The cutoff goes to the database as a datetime, so it is bound in exactly the
    format :func:`schedule` stored - see the note there.
    """
    moment = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    rows = await db.execute(
        "SELECT user_id, kind FROM user_reminders WHERE ready_at <= %s "
        "ORDER BY ready_at LIMIT %s",
        moment,
        int(limit),
    )
    return [(int(user_id), str(kind)) for user_id, kind in rows]


async def reminder_allowed(db: Any, user_id: int, kind: str) -> bool:
    """Whether the member asked for the ping that goes with ``kind``."""
    ping = PINGS.get(str(kind))
    if ping is None:
        return False
    return is_on(await read(db, user_id, ping.pref))


def ping_for(kind: str) -> Ping | None:
    return PINGS.get(str(kind))


def summarise(values: dict[str, Any]) -> str:
    """A one-line ``on``/``off`` digest, for the hub header."""
    if not values:
        return "loading…"
    changed = sum(
        1
        for name, pref in PREFERENCES.items()
        if values.get(name, pref.default) != pref.default
    )
    if not changed:
        return "everything is on its default"
    return f"{changed} of {len(PREFERENCES)} changed from the defaults"


def option_labels(pref: Preference) -> str:
    """``On``/``Off`` - for the error message when a value makes no sense."""
    return ", ".join(f"`{option.label}`" for option in pref.options)
