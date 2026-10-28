"""The prefix a server actually uses, for command examples and help text.

Command help and decorator metadata are built when a cog is imported, long
before a server's prefix is known. Those strings keep a ``{prefix}`` placeholder
instead of a hard-coded ``,`` and :func:`render` fills it in when the help is
shown. Runtime messages can call :func:`prefix_for` directly.
"""

from __future__ import annotations

from typing import Any

from config import PREFIX as DEFAULT_PREFIX

#: Placeholder used in help/usage strings until a server's prefix is known.
PLACEHOLDER = "{prefix}"

#: Prefixes the slash bridge stamps onto a synthetic context. They are not
#: something a member can type in front of a prefix command.
_SYNTHETIC_PREFIXES = frozenset({"", "/", "\u200b"})


async def prefix_for(ctx: Any) -> str:
    """The prefix to show in command examples for *ctx*.

    A prefix invocation already carries the exact prefix in ``ctx.prefix``. A
    slash invocation has a synthetic ``/`` instead, so this asks the database
    for the server's configured prefix — the same value ``Bot.get_prefix``
    hands discord.py. DMs and a database that is not ready yet fall back to the
    bot's default prefix.
    """
    prefix = getattr(ctx, "prefix", None)
    if isinstance(prefix, str) and prefix not in _SYNTHETIC_PREFIXES:
        return prefix

    guild = getattr(ctx, "guild", None)
    database = getattr(getattr(ctx, "bot", None), "database", None)
    if guild is not None and database is not None:
        try:
            return await database.get_prefix(guild.id) or DEFAULT_PREFIX
        except Exception:
            return DEFAULT_PREFIX
    return DEFAULT_PREFIX


def render(text: str | None, prefix: str | None = None) -> str:
    """Fill the ``{prefix}`` placeholder in *text* with *prefix*."""
    if not text:
        return text or ""
    return text.replace(PLACEHOLDER, prefix or DEFAULT_PREFIX)
