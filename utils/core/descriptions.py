"""Recover command descriptions that Python threw away.

Commands in this bot are documented with f-strings::

    @commands.command(name="slots")
    async def slots(self, ctx, *, bet: str) -> None:
        f\"\"\"Spin three reels.

        Usage: `{PREFIX}slots 100` ...
        \"\"\"

The f-string is there so the usage line shows the configured prefix, but
Python only stores a *constant* string as ``__doc__``. A ``JoinedStr`` is not
one, so ``Command.help`` comes out empty and both the ``;help`` page and the
slash command picker lose the description.

This module reads the leading expression back out of the source, evaluates it
in the command's own globals (which is where ``PREFIX`` lives) and hands the
text to ``Command.help``, so the description shows up in both places without
every command having to be rewritten.
"""

from __future__ import annotations

import ast
import inspect
import logging
import textwrap
from typing import Any, Callable

from discord.ext import commands

logger = logging.getLogger("slimey.descriptions")


def describe(callback: Callable[..., Any]) -> str | None:
    """The command's documented summary, or ``None`` if it has none.

    Handles three shapes: a real docstring, an f-string summary (evaluated with
    the prefix filled in) and a summary assembled from string concatenation.
    """
    try:
        raw = inspect.getsource(callback)
    except (OSError, TypeError):  # pragma: no cover - builtins, interactives
        return None

    try:
        module = ast.parse(textwrap.dedent(raw))
    except (SyntaxError, ValueError, IndentationError):
        return None

    if not module.body:
        return None
    function = module.body[0]
    if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    if not function.body:
        return None

    first = function.body[0]
    if not isinstance(first, ast.Expr):
        return None

    node = first.value
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            # A plain docstring: discord.py already stored it, so there is
            # nothing to recover and re-setting it would only risk drift.
            return None
        return None

    if isinstance(node, ast.JoinedStr):
        return _evaluate(node, callback)

    # ``"text " "more"`` or ``"a" + "b"``, both of which the compiler folds
    # into a single constant -- so again nothing to do.
    return None


def _evaluate(node: ast.JoinedStr, callback: Callable[..., Any]) -> str | None:
    """Render an f-string summary using the module globals it references."""
    try:
        expression = ast.Expression(body=node)
        ast.fix_missing_locations(expression)
        code = compile(expression, filename="<command description>", mode="eval")
        value = eval(code, getattr(callback, "__globals__", {}))  # noqa: S307
    except Exception:  # pragma: no cover - a summary that needs runtime state
        logger.debug("Could not render the description for %r", callback, exc_info=True)
        return None

    text = inspect.cleandoc(str(value or ""))
    return text or None


def attach_descriptions(bot: commands.Bot) -> int:
    """Fill in ``Command.help`` for every command still missing one.

    Safe to call again after loading or reloading a cog: a command that already
    has text is skipped, so nothing is overwritten.
    """
    filled = 0
    for command in bot.walk_commands():
        if (command.help or "").strip():
            continue
        text = describe(command.callback)
        if not text:
            continue
        command.help = text
        filled += 1
    if filled:
        logger.debug("Recovered %d command description(s) from source", filled)
    return filled
