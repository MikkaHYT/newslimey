from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Iterable, Optional, Sequence

import discord
from discord.ext import commands
from durations_nlp import Duration

from utils.core import regex as regex_module
from utils.core.helpers import SendHelp

if TYPE_CHECKING:
    from utils.core.context import Context


class MemberConverter(commands.MemberConverter):
    async def convert(self, ctx: Context, argument: str) -> discord.Member:
        if (
            argument.isdigit()
            and ctx.bot.cache.is_ratelimited(f"rl:invalid_id{ctx.guild.id}-{argument}")
        ):
            raise commands.MemberNotFound(argument)

        try:
            return await super().convert(ctx, argument)
        except commands.MemberNotFound:
            if argument.isdigit():
                await ctx.bot.cache.ratelimited(
                    f"rl:invalid_id{ctx.guild.id}-{argument}",
                    1,
                    86400,
                )
            raise


class UserConverter(commands.UserConverter):
    async def convert(self, ctx: Context, argument: str) -> discord.User:
        if (
            argument.isdigit()
            and ctx.bot.cache.is_ratelimited(f"rl:invalid_id{ctx.guild.id}-{argument}")
        ):
            raise commands.UserNotFound(argument)

        try:
            return await super().convert(ctx, argument)
        except commands.UserNotFound:
            if argument.isdigit():
                await ctx.bot.cache.ratelimited(
                    f"rl:invalid_id{ctx.guild.id}-{argument}",
                    1,
                    86400,
                )
            raise


class RoleConverter(commands.RoleConverter):
    async def convert(self, ctx: Context, argument: str) -> discord.Role:
        return await super().convert(ctx, argument)


class MessageConverter(commands.MessageConverter):
    async def convert(self, ctx: Context, argument: str) -> discord.Message:
        return await super().convert(ctx, argument)


class Timespan(commands.Converter):
    async def convert(self, ctx: Context, argument: str) -> Duration:
        ret = Duration(argument)
        if not ret.seconds and argument not in {
            "0",
            "0 seconds",
            "0s",
            "0h",
            "0w",
            "0m",
            "0 hours",
            "0 weeks",
            "0 months",
        }:
            raise SendHelp()
        return ret


class AttachmentConverter(commands.Converter):
    async def convert(self, ctx: Context, argument: Optional[str] = None) -> str:
        if argument and (links := regex_module.link.findall(argument)):
            return links[0]

        if not argument:
            async for message in ctx.channel.history(limit=50):
                if message.attachments:
                    return message.attachments[0].url

        await ctx.send_help(ctx.command.qualified_name)
        raise SendHelp()


class HexConverter(commands.Converter):
    async def convert(self, ctx: Context, argument: Optional[str] = None) -> int:
        if argument is None:
            raise SendHelp()

        try:
            return int(argument)
        except ValueError:
            try:
                return int(argument.strip("#"), 16)
            except ValueError:
                raise SendHelp()


# ---------------------------------------------------------------------------
# Loose member matching
# ---------------------------------------------------------------------------
#: ``<@123>`` / ``<@!123>`` and a bare snowflake. Deliberately loose on the
#: length: a wrong guess just falls through to the name match two lines later.
_MENTION_RE = re.compile(r"^<?@?!?\s*(\d{5,25})>?$")

#: Below this, a name is too far from the query to claim it is the answer. Low
#: on purpose: staff pass a half-remembered nickname and want the nearest match,
#: not a refusal, so this only filters out people who share nothing at all.
MIN_MEMBER_SCORE = 0.3


def _flatten(text: str) -> str:
    """Lowercase, strip punctuation and collapse spaces."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _names(member: discord.Member) -> list[str]:
    """Every name a member can be recognised by."""
    return [
        str(value)
        for value in (
            member.name,
            member.global_name,
            member.nick,
            member.display_name,
        )
        if value
    ]


def is_reference(query: str) -> bool:
    """Whether ``query`` is a mention or a bare user ID, i.e. an exact lookup."""
    return _MENTION_RE.match((query or "").strip()) is not None


def member_score(member: discord.Member, query: str) -> float:
    """How well ``query`` matches ``member``, from ``0.0`` to ``1.0``."""
    wanted = _flatten(query)
    if not wanted:
        return 0.0
    wanted_tokens = wanted.split()

    best = 0.0
    for name in _names(member):
        haystack = _flatten(name)
        if not haystack:
            continue
        if haystack == wanted:
            return 1.0
        if haystack.startswith(wanted):
            score = 0.95
        elif wanted in haystack:
            score = 0.9
        elif wanted_tokens and all(
            any(token in word for word in haystack.split()) for token in wanted_tokens
        ):
            # Every word the query asked for shows up in the name somewhere,
            # which is how "slime world" finds "SlimeWRLD Official".
            score = 0.85
        else:
            # Fuzzy: "jhonny" still lands on "Johnny".
            score = 0.8 * SequenceMatcher(None, wanted, haystack).ratio()
        best = max(best, score)
    return best


def rank_members(
    members: Iterable[discord.Member], query: str, limit: int = 5
) -> list[tuple[discord.Member, float]]:
    """Members that look like ``query``, best match first."""
    scored = [(member, member_score(member, query)) for member in members]
    scored = [(member, score) for member, score in scored if score >= MIN_MEMBER_SCORE]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:limit]


def closest_member(
    members: Sequence[discord.Member], query: str
) -> discord.Member | None:
    """The single best match for ``query``, or ``None`` when nobody is close."""
    ranked = rank_members(members, query, limit=1)
    return ranked[0][0] if ranked else None


async def find_member(
    guild: Optional[discord.Guild], query: str, *, fetch: bool = True
) -> Optional[discord.Member]:
    """Find a member from a mention, an ID, or the closest name match.

    ``@member`` and a bare user ID resolve exactly (falling back to a REST
    lookup when the member isn't cached). Anything else is matched against every
    member's username, display name and nickname, and the nearest one wins - so
    ``;admin panel jhonny`` finds ``Johnny`` instead of refusing outright.
    """
    if guild is None:
        return None
    text = (query or "").strip()
    if not text:
        return None

    match = _MENTION_RE.match(text)
    if match is not None:
        user_id = int(match.group(1))
        member = guild.get_member(user_id)
        if member is None and fetch:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                member = None
        if member is not None:
            return member
        # Not a user ID after all; treat it as a name like anything else.

    return closest_member(list(guild.members), text)

