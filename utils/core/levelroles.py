"""The level -> role ladder: storage, policy and the grant itself.

The ladder lives in two tables. ``level_roles`` holds ``level -> role`` rows, and
because the primary key includes ``role_id`` a single level can hand out as many
roles as you like. ``level_role_settings`` holds the four switches that decide
*how* the ladder is applied:

``mode``
    ``replace`` grants only the highest tier the member has reached and takes
    the older badges back. ``stack`` grants every tier up to their level, so a
    level 10 member wears the 1, 5 and 10 rewards together.

``strip_stale``
    Whether roles the ladder is responsible for but no longer grants are
    removed. Turn it off and the ladder only ever adds.

``announce``
    Whether the level-up card mentions the role it just handed over.

``dm``
    Whether the member also gets a direct message about it.

Both cogs use this module rather than talking to the tables themselves, so
``;admin rolelevels`` and the level-up handler can never disagree about the
ladder — and so the dry run a moderator previews is computed by exactly the same
code that performs the real grant.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import discord

logger = logging.getLogger("slimey.levelroles")

MODES: tuple[str, ...] = ("replace", "stack")
DEFAULT_MODE = "replace"

FLAG_NAMES: tuple[str, ...] = ("strip_stale", "announce", "dm")

#: How many members ``syncroles all`` will touch in one run. The walk is paced
#: at about one member edit a second, so this caps the command near two minutes.
BULK_LIMIT = 100


@dataclass(slots=True)
class Ladder:
    """A guild's ladder plus the switches that decide how it is applied."""

    #: level -> role ids granted at that level.
    grants: dict[int, list[int]] = field(default_factory=dict)
    mode: str = DEFAULT_MODE
    strip_stale: bool = True
    announce: bool = True
    dm: bool = False

    # -- reads -----------------------------------------------------------
    @property
    def empty(self) -> bool:
        return not self.grants

    def tiers(self) -> list[int]:
        """Every configured level, lowest first."""
        return sorted(self.grants)

    def roles_at(self, level: int) -> list[int]:
        return list(self.grants.get(int(level), ()))

    def ladder_roles(self) -> set[int]:
        """Every role id the ladder knows about, across all levels."""
        return {role for roles in self.grants.values() for role in roles}

    def earned_level(self, level: int) -> int | None:
        """The highest configured tier ``level`` has reached, if any."""
        reached = [value for value in self.grants if value <= int(level)]
        return max(reached) if reached else None

    def earned_roles(self, level: int) -> list[int]:
        """The roles ``level`` should be wearing, in ladder order."""
        reached = [value for value in self.grants if value <= int(level)]
        if not reached:
            return []
        if self.mode == "stack":
            return [role for value in sorted(reached) for role in self.grants[value]]
        return list(self.grants[max(reached)])

    def stale_roles(self, level: int, held: Iterable[int]) -> set[int]:
        """Ladder roles the member holds that the ladder no longer grants them."""
        if not self.strip_stale:
            return set()
        return (set(held) & self.ladder_roles()) - set(self.earned_roles(level))


@dataclass(slots=True)
class Outcome:
    """What a grant did, or would do. Also doubles as the dry-run preview."""

    granted: list[discord.Role] = field(default_factory=list)
    removed: list[discord.Role] = field(default_factory=list)
    blocked: list[discord.Role] = field(default_factory=list)
    reason: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.granted or self.removed)

    def summary(self) -> str:
        if self.reason:
            return self.reason
        if not self.changed:
            return "nothing to change"
        bits: list[str] = []
        if self.granted:
            bits.append("gave " + ", ".join(role.mention for role in self.granted))
        if self.removed:
            bits.append("took back " + ", ".join(role.mention for role in self.removed))
        return " · ".join(bits)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
async def load(db: Any, guild_id: int) -> Ladder:
    """Read one guild's ladder and settings."""
    rows = await db.execute(
        "SELECT level, role_id FROM level_roles WHERE guild_id = %s ORDER BY level",
        int(guild_id),
    )
    grants: dict[int, list[int]] = {}
    for level, role_id in rows:
        grants.setdefault(int(level), []).append(int(role_id))

    setting = await db.fetchrow(
        "SELECT mode, strip_stale, announce, dm FROM level_role_settings "
        "WHERE guild_id = %s",
        int(guild_id),
    )
    if setting is None:
        return Ladder(grants=grants)

    mode, strip_stale, announce, dm = setting
    mode = str(mode or DEFAULT_MODE).lower()
    return Ladder(
        grants=grants,
        mode=mode if mode in MODES else DEFAULT_MODE,
        strip_stale=bool(strip_stale),
        announce=bool(announce),
        dm=bool(dm),
    )


async def _write_setting(db: Any, guild_id: int, column: str, value: Any) -> None:
    # ``column`` is one of our own literals, never user input.
    assert column in {"mode", *FLAG_NAMES}
    await db.execute(
        f"INSERT INTO level_role_settings (guild_id, {column}) VALUES (%s, %s) "
        f"ON CONFLICT(guild_id) DO UPDATE SET {column} = excluded.{column}",
        int(guild_id),
        value,
    )


async def set_mode(db: Any, guild_id: int, mode: str) -> None:
    await _write_setting(db, guild_id, "mode", mode)


async def set_flag(db: Any, guild_id: int, name: str, value: bool) -> None:
    await _write_setting(db, guild_id, name, 1 if value else 0)


async def add_grant(db: Any, guild_id: int, level: int, role_id: int) -> None:
    await db.execute(
        "INSERT INTO level_roles (guild_id, level, role_id) VALUES (%s, %s, %s) "
        "ON CONFLICT(guild_id, level, role_id) DO NOTHING",
        int(guild_id),
        int(level),
        int(role_id),
    )


async def remove_grant(db: Any, guild_id: int, level: int, role_id: int) -> None:
    await db.execute(
        "DELETE FROM level_roles WHERE guild_id = %s AND level = %s AND role_id = %s",
        int(guild_id),
        int(level),
        int(role_id),
    )


async def clear_level(db: Any, guild_id: int, level: int) -> None:
    await db.execute(
        "DELETE FROM level_roles WHERE guild_id = %s AND level = %s",
        int(guild_id),
        int(level),
    )


async def clear_all(db: Any, guild_id: int) -> None:
    await db.execute("DELETE FROM level_roles WHERE guild_id = %s", int(guild_id))


async def move_level(db: Any, guild_id: int, source: int, target: int) -> None:
    """Move every role at ``source`` onto ``target``, then empty ``source``."""
    for role_id in (await load(db, guild_id)).roles_at(source):
        await add_grant(db, guild_id, target, role_id)
    await clear_level(db, guild_id, source)


async def copy_level(db: Any, guild_id: int, source: int, target: int) -> None:
    """Duplicate ``source``'s roles onto ``target``, keeping both levels."""
    for role_id in (await load(db, guild_id)).roles_at(source):
        await add_grant(db, guild_id, target, role_id)


# ---------------------------------------------------------------------------
# Applying the ladder
# ---------------------------------------------------------------------------
def _assignable(guild: discord.Guild, role: discord.Role | None) -> bool:
    """Whether the bot is allowed to hand this role out at all."""
    if role is None:
        return False
    return role.is_assignable()


def plan(
    member: discord.Member,
    level: int,
    ladder: Ladder,
    *,
    guild: discord.Guild | None = None,
) -> Outcome:
    """Work out what applying the ladder would do, without touching Discord.

    Used for both the preview a moderator asks for and (via :func:`apply`) the
    real thing, so the preview can never promise something different.
    """
    guild = guild or member.guild
    outcome = Outcome()
    if ladder.empty:
        outcome.reason = "no level roles are configured"
        return outcome

    wanted = ladder.earned_roles(level)
    if not wanted and not ladder.strip_stale:
        outcome.reason = f"no tier configured at or below level {level}"
        return outcome

    held = {role.id for role in member.roles}
    for role_id in wanted:
        role = guild.get_role(role_id)
        if role is None:
            continue
        if not _assignable(guild, role):
            outcome.blocked.append(role)
            continue
        if role.id not in held:
            outcome.granted.append(role)

    for role_id in ladder.stale_roles(level, held):
        role = guild.get_role(role_id)
        if role is None or not _assignable(guild, role):
            continue
        if role.id in held:
            outcome.removed.append(role)

    if (
        not outcome.changed
        and not outcome.blocked
        and ladder.earned_level(level) is None
    ):
        outcome.reason = f"no tier configured at or below level {level}"
    return outcome


async def apply(
    member: discord.Member,
    level: int,
    ladder: Ladder,
    *,
    reason: str,
) -> Outcome:
    """Grant what the member has earned and take back what they haven't.

    Returns the same shape as :func:`plan`, so callers can report precisely what
    happened. Permission problems are reported rather than raised: a
    mis-configured role should never break the level-up card or a bulk sync.
    """
    outcome = plan(member, level, ladder)
    guild = member.guild
    if outcome.reason is not None and not outcome.changed:
        return outcome

    if outcome.changed and not guild.me.guild_permissions.manage_roles:
        outcome.reason = "I need the **Manage Roles** permission to hand roles out"
        outcome.blocked.extend(outcome.granted)
        outcome.granted.clear()
        outcome.removed.clear()
        return outcome

    try:
        # Remove first: in ``replace`` mode the new tier can sit above the old
        # one, and Discord rejects roles the bot can't manage, so ordering the
        # two calls keeps a failed removal from stranding the member.
        if outcome.removed:
            await member.remove_roles(*outcome.removed, reason=reason)
        if outcome.granted:
            await member.add_roles(*outcome.granted, reason=reason)
    except discord.Forbidden:
        logger.warning(
            "Forbidden while applying the level ladder to %s in guild %s",
            member.id,
            guild.id,
        )
        outcome.reason = "Discord refused the change (check my role position)"
    except discord.HTTPException:
        logger.exception("HTTP error while applying the level ladder to %s", member.id)
        outcome.reason = "Discord rejected the change"

    return outcome


def missing_levels(ladder: Ladder, level: int) -> list[int]:
    """Ladder tiers above ``level`` — what the member still has to climb for."""
    return [value for value in ladder.tiers() if value > int(level)]


def next_tier(ladder: Ladder, level: int) -> tuple[int, Sequence[int]] | None:
    """The next configured tier above ``level`` and the roles it will grant."""
    upcoming = missing_levels(ladder, level)
    if not upcoming:
        return None
    return upcoming[0], ladder.roles_at(upcoming[0])
