"""Which economy the current piece of code should work against.

Every server shares one bot-wide economy by default. A server can opt into its
own with `!guildeconomy on`; from then on that server's commands run against its
own scope, and switching back to global keeps the scoped rows so flipping it on
again resumes where it left off.

The active scope travels on a :class:`contextvars.ContextVar` so the data layer
can consult it without every call site threading a guild id through. It is set
once per command invocation (:meth:`bot.DiscordBot._apply_economy_scope`, wired
through discord.py's ``before_invoke`` hook) and defaults to ``GLOBAL_SCOPE``
(0) everywhere else, so background tasks and any path that never runs a command
keep using the bot-wide economy.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
from typing import TYPE_CHECKING, Any, Iterator

from utils.core.settings import GLOBAL_SCOPE, GUILD_ECONOMY_KEY

if TYPE_CHECKING:
    from discord.ext import commands

_CURRENT_SCOPE: contextvars.ContextVar[int] = contextvars.ContextVar(
    "economy_scope", default=GLOBAL_SCOPE
)


def current_scope() -> int:
    """The economy scope the current task works against (``0`` = bot-wide)."""
    return _CURRENT_SCOPE.get()


def set_scope(scope: int) -> contextvars.Token:
    """Point the current task at ``scope``.

    Returns the token needed to undo it with :func:`reset_scope`. Called from the
    ``before_invoke`` hook, where the value stays put for the whole command.
    """
    return _CURRENT_SCOPE.set(int(scope))


def reset_scope(token: contextvars.Token) -> None:
    """Undo a previous :func:`set_scope`."""
    _CURRENT_SCOPE.reset(token)


@contextlib.contextmanager
def use_scope(scope: int) -> Iterator[None]:
    """Run a block against ``scope``, restoring the previous one afterwards."""
    token = _CURRENT_SCOPE.set(int(scope))
    try:
        yield
    finally:
        _CURRENT_SCOPE.reset(token)


def guild_economy_enabled(bot: Any, guild_id: int | None) -> bool:
    """Whether ``guild_id`` has asked for its own economy. Missing means OFF."""
    if not guild_id:
        return False
    settings = getattr(bot, "dash_settings", None)
    if settings is None:
        return False
    return bool(settings.flag(GUILD_ECONOMY_KEY, False, guild_id=int(guild_id)))


def scope_for_guild(bot: Any, guild_id: int | None) -> int:
    """The scope a guild's commands should run under."""
    if not guild_id or not guild_economy_enabled(bot, guild_id):
        return GLOBAL_SCOPE
    return int(guild_id)


def scope_for_context(ctx: "commands.Context") -> int:
    """The scope the command in ``ctx`` should run under."""
    guild = getattr(ctx, "guild", None)
    return scope_for_guild(getattr(ctx, "bot", None), getattr(guild, "id", None))


def scope_for_interaction(bot: Any, interaction: Any) -> int:
    """The scope a component or modal interaction belongs to."""
    guild_id = getattr(interaction, "guild_id", None)
    if not guild_id:
        guild_id = getattr(getattr(interaction, "guild", None), "id", None)
    return scope_for_guild(bot, guild_id)


def _make_scoped_entry(bot: Any, original: Any) -> Any:
    """Wrap a callback entry point so it runs in its interaction's scope.

    Only the interaction object carries a ``guild_id``; the item or payloads
    that sit next to it in the argument list do not, so it is easy to pick out.
    """

    @functools.wraps(original)
    async def _scoped_entry(self: Any, *args: Any, **kwargs: Any) -> Any:
        interaction = next((arg for arg in args if hasattr(arg, "guild_id")), None)
        if interaction is None:
            return await original(self, *args, **kwargs)
        token = set_scope(scope_for_interaction(bot, interaction))
        try:
            return await original(self, *args, **kwargs)
        finally:
            reset_scope(token)

    return _scoped_entry


def install_interaction_scope(bot: Any) -> None:
    """Run interaction callbacks in the economy scope of their server.

    A command typed in chat gets its scope from ``before_invoke``, but Discord
    hands the bot three other kinds of callback, each running in a task of its
    own, none of which passes through that hook: bridged slash commands (the
    slash bridge applies the scope itself), native application commands such as
    ``/shop`` (through ``CommandTree._call``), and buttons, selects and modals
    (through ``_scheduled_task`` on the view classes). Without this a server
    with its own economy would answer ``/shop``, a casino button or a modal
    from the shared purse while its text commands used the right one.
    """
    import discord

    if getattr(bot, "_econ_interaction_scope_installed", False):
        return
    bot._econ_interaction_scope_installed = True

    # ``View`` and ``LayoutView`` inherit ``BaseView``'s implementation while
    # ``Modal`` has its own, so wrapping each concrete class keeps the shared
    # base untouched and gives every kind of callback its own wrapper.
    owners = [discord.ui.View, discord.ui.Modal]
    layout_view = getattr(discord.ui, "LayoutView", None)
    if layout_view is not None:
        owners.append(layout_view)

    for owner in owners:
        original = getattr(owner, "_scheduled_task", None)
        if original is not None:
            owner._scheduled_task = _make_scoped_entry(bot, original)

    tree = getattr(bot, "tree", None)
    if tree is not None:
        original_call = getattr(type(tree), "_call", None)
        if original_call is not None:
            type(tree)._call = _make_scoped_entry(bot, original_call)
