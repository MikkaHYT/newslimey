from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, Union

import discord
import humanize
from discord.ext import commands
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from utils.core.context import Context
from durations_nlp import Duration

from utils.core import regex as regex_module
from utils.core.views import Confirm

TUPLE = ()


class SendHelp(Exception):
    pass


def is_guild_owner() -> Any:
    async def predicate(ctx: "Context") -> bool:
        if ctx.author.id != ctx.guild.owner_id:
            await ctx.error("You're missing the **Server Owner** permission.")
            return False
        return True

    return commands.check(predicate)


def fmtseconds(seconds: Union[int, float], unit: str = "microseconds") -> str:
    return humanize.naturaldelta(timedelta(seconds=seconds), minimum_unit=unit)


def multi_replace(text: str, replacements: dict[str, str]) -> str:
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text.strip()


def strip_flags(text: str, ctx: Any) -> str:
    if not getattr(ctx.command, "parameters", None):
        return text.strip()

    flag_dict = {}
    for key, config in ctx.command.parameters.items():
        flag_dict[key] = ctx.parameters.get(key)
        for alias in config.get("aliases", TUPLE):
            flag_dict[alias] = ctx.parameters.get(key)

    return multi_replace(
        text,
        {
            **{f" --{key} {value}": "" for key, value in flag_dict.items() if value},
            **{f" -{key}": "" for key in flag_dict},
            **{f"--{key} {value}": "" for key, value in flag_dict.items() if value},
            **{f"-{key}": "" for key in flag_dict},
        },
    )


class ParameterParser:
    def __init__(self, context: Any, parameters: dict) -> None:
        self.context = context
        self.parameters = parameters

    def get_args(self, text: str, args: list[str], pattern: re.Pattern[str]) -> dict[str, str]:
        ret = {arg: "" for arg in args}
        ret.update({k: v for k, v in pattern.findall(text)})
        return ret

    def get_params_and_aliases(self, data: dict) -> list[str]:
        ret = []
        for parameter, config in data.items():
            ret.append(parameter)
            for alias in config.get("aliases", TUPLE):
                ret.append(alias)
        return ret

    async def parse(self) -> dict[str, Any]:
        ret = {parameter: None for parameter in self.parameters}

        all_params = self.get_params_and_aliases(self.parameters)
        parsed = self.get_args(
            self.context.message.content,
            all_params,
            regex_module.parameter_parser(all_params),
        )

        for parameter, config in self.parameters.items():
            if config.get("require_value", True) is False:
                for flag in (parameter, *config.get("aliases", TUPLE)):
                    if f"-{flag}" in set(self.context.message.content.split()):
                        ret[parameter] = True
                        break
            else:
                converter = config.get("converter", str)
                for flag in (parameter, *config.get("aliases", TUPLE)):
                    if parsed.get(flag):
                        try:
                            if isinstance(converter, type) and issubclass(converter, commands.Converter):
                                ret[parameter] = await converter().convert(
                                    self.context,
                                    parsed.get(flag).strip(),
                                )
                            elif converter is int:
                                ret[parameter] = int(parsed.get(flag).strip())
                            else:
                                ret[parameter] = converter(parsed.get(flag))
                            break
                        except Exception:
                            await self.context.error(
                                f"Failed to convert value for parameter **{parameter}**"
                            )
                            raise

        return ret


async def confirm(ctx: Any, message: discord.Message) -> bool:
    view = Confirm(message=message, invoker=ctx.author)
    await message.edit(view=view)
    await view.wait()
    return view.value
