"""One rate limit per member per game, shared by every entry point.

Discord.py's ``@commands.cooldown`` only fires when a command is dispatched
normally. Bridged slash commands call the callback directly, so a decorator
would rate-limit ``;slots`` and leave ``/slots`` free to spam. This gate is
checked in both places instead: once as a bot-level check for the prefix path
and once inside the bridge for the slash path, so the two can't drift apart.

Only the games pay it. :data:`COOLDOWN_GAMES` is the entire list of commands
that wait between plays, so a leaderboard, a menu, a help page or one of the
media commands runs as often as the member likes. A game that never started is
free as well: whatever only explains the command hands the time straight back
(see :func:`settle_cooldown`), so being told how to play never costs a wait.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from discord.ext import commands

from config import casino

logger = logging.getLogger("slimey.cooldowns")

#: Seconds between two rounds of the same game.
DEFAULT_COOLDOWN = 15.0

#: Every command the gate charges for, by root command name. These are the
#: games — the ones that take a bet, plus the fights, which pay a flat prize for
#: a win. Add a qualified name (``"casino top"``) rather than a root name to
#: gate a single subcommand of a group.
COOLDOWN_GAMES: frozenset[str] = frozenset(
    {
        "gamble",
        "slots",
        "dice",
        "coinflip",
        "roulette",
        "wheel",
        "plinko",
        "keno",
        "baccarat",
        "crash",
        "blackjack",
        "mines",
        "ladder",
        "double",
        "hilo",
        "supergamble",
        "duel",
        # Fights hand the winner 1,000 cash, so they wait like a game even
        # though there is no stake to place.
        "fight",
        "requestfight",
    }
)

#: Games that wait longer than one round between plays. ``supergamble`` reads
#: its wait out of the casino config so the two can't drift apart.
GAME_COOLDOWNS: dict[str, float] = {
    "supergamble": float(casino.supergamble_cooldown),
}


def cooldown_error(command: commands.Command, retry_after: float) -> commands.CommandOnCooldown:
    """A ``CommandOnCooldown`` the existing error handler already knows how to
    render, built by hand because nothing raised it through a real bucket."""
    return commands.CommandOnCooldown(
        commands.Cooldown(1, 1.0), retry_after, commands.BucketType.user
    )


class CooldownGate:
    """A per-member, per-game stopwatch."""

    def __init__(
        self,
        default: float = DEFAULT_COOLDOWN,
        games: frozenset[str] | None = None,
        overrides: dict[str, float] | None = None,
    ) -> None:
        self.default = float(default)
        self.games = COOLDOWN_GAMES if games is None else frozenset(games)
        self.overrides = dict(GAME_COOLDOWNS if overrides is None else overrides)
        # (member id, qualified command name) -> monotonic expiry
        self._until: dict[tuple[int, str], float] = {}

    # -- limits ----------------------------------------------------------
    def root_name(self, command: commands.Command) -> str:
        return (command.qualified_name or command.name or "").split(" ", 1)[0].lower()

    def limit_for(self, command: commands.Command) -> float:
        """Seconds between uses, or ``0`` when the command isn't charged.

        Only the games are charged, so anything not named in ``games`` or
        ``overrides`` is free.
        """
        root = self.root_name(command)
        qualified = (command.qualified_name or "").lower()
        for key in (qualified, root):
            if key in self.overrides:
                return float(self.overrides[key] or 0.0)
        if qualified in self.games or root in self.games:
            return self.default
        return 0.0

    # -- state -----------------------------------------------------------
    def remaining(self, user_id: int, command: commands.Command) -> float:
        if not self.limit_for(command):
            return 0.0
        expiry = self._until.get((int(user_id), command.qualified_name), 0.0)
        return max(0.0, expiry - time.monotonic())

    def start(self, user_id: int, command: commands.Command) -> float:
        limit = self.limit_for(command)
        if not limit:
            return 0.0
        self._until[(int(user_id), command.qualified_name)] = time.monotonic() + limit
        return limit

    def release(self, ctx: commands.Context) -> bool:
        """Hand the charged time back for one invocation.

        Only the invocation that armed the gate can release it. Without that
        check a member already waiting out a cooldown could clear it just by
        running the command again — the one thing the gate exists to stop.
        """
        if not getattr(ctx, "cooldown_armed", False):
            return False
        ctx.cooldown_armed = False
        command = ctx.command
        if command is None:
            return False
        key = (int(ctx.author.id), command.qualified_name)
        return self._until.pop(key, None) is not None

    def clear(self, user_id: int, command: commands.Command | None = None) -> int:
        """Drop the gate for one member (all commands when ``command`` is None)."""
        if command is None:
            stale = [key for key in self._until if key[0] == int(user_id)]
        else:
            stale = [(int(user_id), command.qualified_name)] if (
                int(user_id),
                command.qualified_name,
            ) in self._until else []
        for key in stale:
            self._until.pop(key, None)
        return len(stale)

    def sweep(self) -> int:
        """Forget expired entries. Cheap enough to call after every start."""
        now = time.monotonic()
        stale = [key for key, expiry in self._until.items() if expiry <= now]
        for key in stale:
            self._until.pop(key, None)
        return len(stale)

    # -- entry points ----------------------------------------------------
    async def check(self, ctx: commands.Context) -> bool:
        """Bot-level check: consume the gate, or raise ``CommandOnCooldown``.

        Runs for the prefix path. The bridge calls this directly for the slash
        path, where bot-level checks aren't evaluated.
        """
        command = ctx.command
        if command is None or not self.limit_for(command):
            return True

        wait = self.remaining(ctx.author.id, command)
        if wait:
            # This invocation charged nothing of its own, so it must not be able
            # to hand anything back either.
            ctx.cooldown_armed = False
            raise cooldown_error(command, wait)

        self.start(ctx.author.id, command)
        # Remember that this invocation is the charged one, so a command that
        # only explained itself can hand the time back through ``release``.
        ctx.cooldown_armed = True
        if len(self._until) > 512:
            self.sweep()
        return True


def is_usage_card(response: Any) -> bool:
    """True for a card that only explains a command rather than playing one."""
    return bool(getattr(response, "usage_only", False))


def release_cooldown(ctx: commands.Context) -> bool:
    """Give the gate back for this invocation, when it armed one."""
    gate = getattr(getattr(ctx, "bot", None), "cooldown_gate", None)
    if gate is None:
        return False
    return gate.release(ctx)


def settle_cooldown(ctx: commands.Context, response: Any) -> bool:
    """Hand a cooldown back when the response only explained the command.

    Called for every ``ctx.send``: a game that got going answers with a result
    card and keeps its cooldown, while a "here's how to use this" card — the
    same one the notice/help pages are built from — gives the wait back.
    """
    if not is_usage_card(response):
        return False
    return release_cooldown(ctx)
