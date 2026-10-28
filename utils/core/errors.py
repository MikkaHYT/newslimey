from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from utils.core.context import Context

logger = logging.getLogger("timezones")

USAGE_ERRORS: tuple[type[commands.CommandError], ...] = (
    commands.MissingRequiredArgument,
    commands.TooManyArguments,
    commands.BadArgument,
)


async def send_usage_help(
    ctx: commands.Context | Context,
    command: commands.Command | str | None = None,
) -> Optional[discord.Message]:
    from cogs.help import send_command_help

    target = command or ctx.command
    if not target:
        return None
    return await send_command_help(ctx, target)


def user_message_for_error(error: commands.CommandError | BaseException) -> str:
    if isinstance(error, commands.CommandInvokeError) and error.original:
        return user_message_for_error(error.original)

    if isinstance(error, commands.MissingRequiredArgument):
        return f"Missing required argument: `{error.param.name}`."
    if isinstance(error, commands.TooManyArguments):
        return "Too many arguments were provided."
    if isinstance(error, commands.BadArgument):
        return str(error) or "One of the arguments was invalid."
    if isinstance(error, commands.MissingPermissions):
        missing = ", ".join(error.missing_permissions)
        return f"You're missing permission(s): `{missing}`."
    if isinstance(error, commands.BotMissingPermissions):
        missing = ", ".join(error.missing_permissions)
        return f"I'm missing permission(s): `{missing}`."
    if isinstance(error, commands.CheckFailure):
        return str(error) or "You can't use this command."
    if isinstance(error, commands.MaxConcurrencyReached):
        return "This command is already running. Try again in a moment."
    if isinstance(error, commands.CommandOnCooldown):
        from utils.core.helpers import fmtseconds

        return f"Slow down — try again in **{fmtseconds(error.retry_after)}**."
    if isinstance(error, commands.CommandNotFound):
        return "That command doesn't exist."
    if isinstance(error, discord.HTTPException):
        return "Discord rejected that request. Try again in a moment."
    if str(error):
        return str(error)
    return "Something went wrong while running that command."


async def send_command_error(
    ctx: commands.Context | Context,
    error: commands.CommandError,
    *,
    log_level: int = logging.ERROR,
) -> Optional[discord.Message]:
    if isinstance(error, commands.CommandNotFound):
        return None

    message = user_message_for_error(error)
    original = error.original if isinstance(error, commands.CommandInvokeError) and error.original else error

    if log_level >= logging.ERROR:
        logger.log(
            log_level,
            "Command error (%s): %s",
            ctx.command.qualified_name if ctx.command else "unknown",
            original,
            exc_info=original,
        )
    else:
        logger.log(
            log_level,
            "Command error (%s): %s",
            ctx.command.qualified_name if ctx.command else "unknown",
            original,
        )

    embed = discord.Embed(
        description=message,
        color=ctx.bot.color,
        timestamp=datetime.now(timezone.utc),
    )
    try:
        return await ctx.reply(embed=embed)
    except discord.HTTPException:
        return await ctx.send(embed=embed)
