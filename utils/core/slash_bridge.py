"""
Slash command bridge.

Automatically registers an ``app_commands.Command`` for every prefix
command in every cog so that the bot works with ``/`` commands alongside
the regular ``,`` prefix commands.

Each bridged app command builds a synthetic ``Context`` from the
interaction (``Context.from_interaction``) and then invokes the original
prefix command callback untouched. This keeps a single source of truth
for behaviour while giving full slash-command coverage.
"""

from __future__ import annotations

import inspect
import logging
import re
import types
from typing import (
    Any,
    Callable,
    Iterable,
    Iterator,
    Literal,
    Optional,
    Union,
    get_args,
    get_origin,
)

import discord
from discord import app_commands
from discord.ext import commands

from utils.core.context import Context
from utils.core.converters import (
    AttachmentConverter,
    HexConverter,
    MemberConverter,
    MessageConverter,
    RoleConverter,
    Timespan,
    UserConverter,
)
from utils.core.cooldowns import release_cooldown
from utils.core.errors import user_message_for_error
from utils.core.economy_scope import reset_scope, scope_for_guild, set_scope
from utils.core.helpers import SendHelp, fmtseconds
from utils.core.prefixes import render
from utils.core.settings import maintenance_reason

logger = logging.getLogger("slimey.slash_bridge")

NAME_RE = re.compile(r"^[\w-]{1,32}$")

#: Discord allows at most 25 subcommands (and subcommand groups) under a single
#: command. Exceeding it makes Discord reject the *whole* bulk overwrite, which
#: silently takes every slash command down with it — so a full group is capped
#: here and the overflow stays prefix-only rather than breaking the sync.
SUBCOMMAND_LIMIT = 25

#: Groups that are far too big for Discord's 25-subcommand ceiling pick their
#: slash commands from an explicit allowlist instead of taking whatever happens
#: to come last (a group's commands are a ``set``, so that order is arbitrary).
#: Everything not listed — and every other group's subcommands — still works
#: with the prefix, they just don't get a slash version.
SLASH_SUBCOMMAND_ALLOWLIST: dict[str, frozenset[str]] = {
    "lf": frozenset(
        {
            "set",
            "profile",
            "topartists",
            "toptracks",
            "topalbums",
            "topgenres",
            "artist",
            "album",
            "track",
            "artisttracks",
            "artistalbums",
            "artistoverview",
            "trackplays",
            "albumplays",
            "artistplays",
            "trackdetails",
            "cover",
            "recent",
            "loved",
            "streak",
            "tags",
            "similar",
            "compare",
            "charts",
            "crowns",
        }
    ),
}

# Prefix converter -> native app command parameter type
CONVERTER_APP_TYPES: dict[type, type] = {
    MemberConverter: discord.Member,
    UserConverter: discord.User,
    RoleConverter: discord.Role,
    commands.MemberConverter: discord.Member,
    commands.UserConverter: discord.User,
    commands.RoleConverter: discord.Role,
    commands.TextChannelConverter: discord.TextChannel,
    commands.GuildChannelConverter: discord.abc.GuildChannel,
}

# Prefix converters that stay raw strings in the picker and are converted
# manually right before the original callback runs.
STRING_CONVERTERS: dict[type, type] = {
    Timespan: Timespan,
    HexConverter: HexConverter,
    MessageConverter: MessageConverter,
    AttachmentConverter: AttachmentConverter,
}

NATIVE_APP_TYPES: tuple[type, ...] = (
    discord.Member,
    discord.User,
    discord.Role,
    discord.TextChannel,
    discord.VoiceChannel,
    discord.Thread,
    discord.CategoryChannel,
    discord.StageChannel,
    discord.Attachment,
    discord.abc.GuildChannel,
    discord.Object,
)

BASE_TYPES: tuple[type, ...] = (str, int, float, bool)

# Some prefix groups have a main command that also takes parameters.
# Discord cannot attach options to a group, so those mains are exposed as
# a subcommand. The key is the fully qualified prefix path (e.g. "role" or
# "history remove") and the value is the subcommand name used instead.
GROUP_MAIN_RENAMES: dict[str, str] = {
    "rep": "view",
    "role": "update",
    "history": "view",
    "history remove": "case",
    "lockdown": "lock",
    "lockdown remove": "channel",
    "unlock": "remove",
    "untimeout": "remove",
    "unmute": "remove",
    "purge": "standard",
    "notes": "view",
}

# Prefix-only commands: staff/owner tooling that would otherwise burn slots in
# Discord's 100-command slash limit. They still work with the prefix and still
# show up in `,help`; they just never get a slash version.
#: Prefix command names that stay prefix-only. Nothing is excluded right now -
#: the whole tree is 50-odd commands, well under Discord's 100-command limit -
#: but this is the lever to pull if the tree ever needs trimming. Add the
#: command's *name* (not its cog) here.
#: Prefix-only commands. The owner tooling (``admin`` / ``cog``) doesn't belong
#: in the picker every member sees, and the Juice WRLD catalog commands were
#: added on top of an already-full tree — keeping them prefix-only stops them
#: from pushing the existing slash commands past Discord's 100-command limit
#: (which silently drops whichever commands come last).
SLASH_EXCLUDED: frozenset[str] = frozenset(
    {
        # Owner tooling — never in the picker.
        "admin",
        "cog",
        # Staff / server-configuration commands: usable, just not worth a slot
        # in the member-facing picker.
        "logsetup",
        "logstatus",
        "setradio",
        "setradiovc",
        "setsotd",
        "togglesotd",
        "sotdstyle",
        "sotdstatus",
        "sendsotd",
        "antimp3",
        "mp3buttonmode",
        "guildeconomy",
        "defaultrole",
        "bc",
        "qp",
        "banroulette",
        "kickroulette",
        "timeoutroulette",
        "createembed",
        "reactionrole",
        "repost",
        # Catalog commands added on top of an already-full tree.
        "leak",
        "instrumental",
        "session",
        "stem",
        "stems",
        "remaster",
        "remasters",
        "freestyle",
        "freestyles",
        "edit",
        "edits",
        "snip",
        "erafind",
        "coverart",
        "boosterrole",
        # The fun metres and fights (``cogs/fun.py``): talk-toy commands nobody
        # needs in the picker, kept off the tree so they can't push a real
        # command past the 100-command ceiling. They still show up in `,help`
        # and run fine with the prefix.
        "gay",
        "pussy",
        "dih",
        "makeout",
        "ship",
        "grope",
        "fuck",
        "fight",
        "requestfight",
        "fighttop",
    }
)

# Raised internally to abort an invocation (e.g. a custom converter failed).
_STOP = object()


def _parameters_config(prefix_cmd: commands.Command) -> dict[str, Any]:
    """Return the ``parameters={...}`` flag configuration for a command."""
    config = getattr(prefix_cmd, "parameters", None)
    if not config:
        config = prefix_cmd.__original_kwargs__.get("parameters")
    return config or {}


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    """Return ``(base_annotation, is_optional)`` for ``Optional``/``| None``."""
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
    return annotation, False


def _is_greedy(annotation: Any) -> bool:
    return isinstance(annotation, commands.Greedy)


def _app_type_for(annotation: Any) -> Any:
    if annotation in CONVERTER_APP_TYPES:
        return CONVERTER_APP_TYPES[annotation]
    if annotation in STRING_CONVERTERS:
        return str
    if annotation in NATIVE_APP_TYPES:
        return annotation
    if annotation in BASE_TYPES:
        return annotation
    return str


def _union_app_type(annotation: Any) -> Any:
    """Pick the widest supported type for ``Union`` annotations."""
    args = [a for a in get_args(annotation) if a is not type(None)]
    mapped = {_app_type_for(a) for a in args}
    if discord.Member in mapped:
        return discord.Member
    if discord.User in mapped:
        return discord.User
    if discord.Role in mapped:
        return discord.Role
    if len(mapped) == 1:
        return mapped.pop()
    return str


def _resolve_param(param: inspect.Parameter) -> dict[str, Any]:
    """Translate a prefix command parameter into a slash parameter spec."""
    annotation = param.annotation
    if annotation is param.empty:
        annotation = str

    is_greedy = _is_greedy(annotation)
    if is_greedy:
        annotation = annotation.converter

    base, optional = _unwrap_optional(annotation)

    literal_args: tuple[Any, ...] | None = None
    if get_origin(base) is Literal:
        literal_args = tuple(get_args(base))
        base = type(literal_args[0]) if literal_args else str
    elif get_origin(base) in (Union, types.UnionType):
        base = _union_app_type(base)

    if base not in STRING_CONVERTERS:
        base = _app_type_for(base)

    required = param.default is param.empty and not optional

    return {
        "name": param.name,
        "annotation": base,
        "optional": optional,
        "required": required,
        "kind": param.kind,
        "literal_args": literal_args,
        "converter": STRING_CONVERTERS.get(base),
        "is_greedy": is_greedy,
        "is_var": param.kind is inspect.Parameter.VAR_POSITIONAL,
        "description": None,
    }


def _render_annotation(spec: dict[str, Any]) -> str:
    if spec["literal_args"]:
        rendered = f"Literal[{', '.join(repr(a) for a in spec['literal_args'])}]"
    else:
        rendered = _type_repr(spec["annotation"])
    if spec["optional"]:
        return f"Optional[{rendered}]"
    return rendered


def _type_repr(typ: Any) -> str:
    if typ is str:
        return "str"
    if typ is int:
        return "int"
    if typ is float:
        return "float"
    if typ is bool:
        return "bool"
    if typ is discord.Member:
        return "discord.Member"
    if typ is discord.User:
        return "discord.User"
    if typ is discord.Role:
        return "discord.Role"
    if typ is discord.TextChannel:
        return "discord.TextChannel"
    if typ is discord.VoiceChannel:
        return "discord.VoiceChannel"
    if typ is discord.Thread:
        return "discord.Thread"
    if typ is discord.CategoryChannel:
        return "discord.CategoryChannel"
    if typ is discord.StageChannel:
        return "discord.StageChannel"
    if typ is discord.Attachment:
        return "discord.Attachment"
    if typ is discord.abc.GuildChannel:
        return "discord.abc.GuildChannel"
    if typ is discord.Object:
        return "discord.Object"
    return "str"


def _generate_callback(
    bot: commands.Bot,
    cog: commands.Cog,
    prefix_cmd: commands.Command,
    param_specs: list[dict[str, Any]],
    flag_specs: list[dict[str, Any]],
) -> Callable[..., Any]:
    """Build a callback whose signature matches the slash command options."""
    specs = list(param_specs)
    specs.extend(flag_specs)

    required = [s for s in specs if s["required"]]
    optional = [s for s in specs if not s["required"]]

    signature: list[str] = []
    for spec in (*required, *optional):
        ann = _render_annotation(spec)
        if spec["required"]:
            signature.append(f"{spec['name']}: {ann}")
        else:
            signature.append(f"{spec['name']}: {ann} = None")

    signature_code = ", ".join(signature)
    kwargs_code = ", ".join(f"'{spec['name']}': {spec['name']}" for spec in specs)

    code = (
        "async def __bridged_callback(interaction: discord.Interaction"
        + (f", {signature_code}" if signature_code else "")
        + ") -> None:\n"
        + f"    await _bridged_invoke(_BOT, _PREFIX_CMD, _COG, interaction, **{{{kwargs_code}}})"
    )

    namespace: dict[str, Any] = {
        "discord": discord,
        "Optional": Optional,
        "Union": Union,
        "Literal": Literal,
        "commands": commands,
        "_BOT": bot,
        "_PREFIX_CMD": prefix_cmd,
        "_COG": cog,
        "_bridged_invoke": _bridged_invoke,
    }
    exec(code, namespace)  # noqa: S102 - generated from a fixed template
    return namespace["__bridged_callback"]


async def _convert_flag(config: dict[str, Any], ctx: Context, raw: Any) -> Any:
    if raw is None:
        return None
    if config.get("require_value", True) is False:
        return True
    converter = config.get("converter", str)
    if converter is int:
        return int(raw)
    if isinstance(converter, type) and issubclass(converter, commands.Converter):
        return await converter().convert(ctx, raw)
    if callable(converter):
        return converter(raw)
    return raw


async def _resolve_value(
    ctx: Context,
    prefix_cmd: commands.Command,
    param: inspect.Parameter,
    value: Any,
    interaction: discord.Interaction,
) -> Any:
    if value is None:
        default = param.default
        if default is commands.Author:
            return interaction.user
        if default is not param.empty:
            return default
        if param.kind is inspect.Parameter.VAR_POSITIONAL:
            return ()
        if _is_greedy(param.annotation):
            return []
        return None

    annotation = param.annotation
    if _is_greedy(annotation):
        annotation = annotation.converter
    base, _ = _unwrap_optional(annotation)
    if get_origin(base) in (Union, types.UnionType):
        base = None
    converter_cls = STRING_CONVERTERS.get(base) if base is not None else None

    if converter_cls is not None:
        try:
            return await converter_cls().convert(ctx, value)
        except SendHelp:
            await ctx.send_help(prefix_cmd.qualified_name)
            return _STOP
        except commands.BadArgument as error:
            # Rejected before the command ran, so nothing was played: the same
            # rule the prefix path applies to a usage error.
            release_cooldown(ctx)
            await ctx.error(str(error) or "One of the arguments was invalid.")
            return _STOP

    if _is_greedy(param.annotation):
        return [value]
    if param.kind is inspect.Parameter.VAR_POSITIONAL:
        return (value,)
    return value


async def _bridged_invoke(
    bot: commands.Bot,
    prefix_cmd: commands.Command,
    cog: commands.Cog,
    interaction: discord.Interaction,
    **values: Any,
) -> None:
    """Run a bridged slash command under the economy scope of its server.

    Slash commands skip ``bot.invoke`` (and with it ``before_invoke``), so the
    scope has to be applied here as well - otherwise ``/balance`` in a server
    with its own economy would read the shared purse.
    """
    token = set_scope(scope_for_guild(bot, interaction.guild_id))
    try:
        await _bridged_invoke_in_scope(bot, prefix_cmd, cog, interaction, **values)
    finally:
        reset_scope(token)


async def _bridged_invoke_in_scope(
    bot: commands.Bot,
    prefix_cmd: commands.Command,
    cog: commands.Cog,
    interaction: discord.Interaction,
    **values: Any,
) -> None:
    ctx = await Context.from_interaction(interaction)
    ctx.command = prefix_cmd

    # Acknowledge straight away. Prefix commands were written without the three
    # second interaction deadline in mind, and some of them (downloading and
    # transcoding a song, uploading artwork) do real work before their first
    # ``ctx.send``. Deferring here keeps Discord from giving up on them while
    # leaving every response path in the command itself untouched.
    if not interaction.response.is_done():
        await interaction.response.defer(thinking=True)

    # Global maintenance / per-command maintenance blocks (owner-run
    # `,botdisable` / `,botenable`).
    reason = maintenance_reason(bot, prefix_cmd.qualified_name)
    if reason:
        await ctx.send(f"❌ {reason}")
        return

    # Respect commands disabled per guild from the web dashboard.
    if ctx.guild is not None:
        disabled = getattr(bot, "dash_disabled", {}).get(ctx.guild.id, ())
        if prefix_cmd.qualified_name in disabled:
            await ctx.send(f"❌ `{prefix_cmd.qualified_name}` is disabled on this server.")
            return

    # The same per-member gate the prefix path enforces through a bot-level
    # check. Calling the callback directly skips every check, so without this
    # "/slots" would be free to spam while ";slots" is rate limited.
    gate = getattr(bot, "cooldown_gate", None)
    if gate is not None:
        try:
            await gate.check(ctx)
        except commands.CommandOnCooldown as error:
            await ctx.send(
                f"⏳ Slow down — **/{prefix_cmd.qualified_name}** is ready again in "
                f"**{fmtseconds(error.retry_after)}**."
            )
            return

    # Flags from the prefix command's custom "--flag" system become plain
    # optional options here and are handed to the callback via ctx.parameters.
    ctx.parameters = {}
    params_config = _parameters_config(prefix_cmd)
    signature_names = {name for name in prefix_cmd.clean_params if name not in ("self", "ctx")}
    for flag, config in params_config.items():
        if flag in signature_names:
            continue
        ctx.parameters[flag] = await _convert_flag(config, ctx, values.get(flag))

    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    for name, param in prefix_cmd.clean_params.items():
        if name in ("self", "ctx"):
            continue
        value = await _resolve_value(ctx, prefix_cmd, param, values.get(name), interaction)
        if value is _STOP:
            return
        if param.kind is inspect.Parameter.VAR_POSITIONAL:
            # ``_resolve_value`` returns a tuple for var-positional params,
            # which must be unpacked into the positional arguments.
            args.extend(value)
        elif param.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD:
            args.append(value)
        else:
            kwargs[name] = value

    try:
        await prefix_cmd.callback(cog, ctx, *args, **kwargs)
    except Exception as error:
        handler = getattr(prefix_cmd, "on_error", None)
        if handler is None:
            raise
        wrapped = (
            error
            if isinstance(error, commands.CommandError)
            else commands.CommandInvokeError(prefix_cmd, error)
        )
        if hasattr(handler, "__get__"):
            handler = handler.__get__(cog, type(cog))
        await handler(ctx, wrapped)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def _bucket_key(bucket_type: Any) -> Callable[[discord.Interaction], Any]:
    if bucket_type is commands.BucketType.user:
        return lambda i: i.user.id
    if bucket_type is commands.BucketType.guild:
        return lambda i: (i.guild or i.user).id
    if bucket_type is commands.BucketType.channel:
        return lambda i: i.channel_id
    if bucket_type is commands.BucketType.member:
        return lambda i: (i.guild_id, i.user.id)
    if bucket_type is commands.BucketType.category:
        return lambda i: getattr(i.channel, "category_id", None) or i.channel_id
    if bucket_type is commands.BucketType.role:
        return lambda i: (getattr(i.user, "top_role", None) or i.user).id
    return lambda i: None


def _make_prefix_checks_check(checks: list[Any]) -> Callable[[discord.Interaction], Any]:
    async def app_check(interaction: discord.Interaction) -> bool:
        ctx = await Context.from_interaction(interaction)
        for check in checks:
            predicate = getattr(check, "predicate", check)
            try:
                result = predicate(ctx)
                if inspect.isawaitable(result):
                    result = await result
            except commands.CommandError as error:
                raise app_commands.CheckFailure(
                    str(error) or "You can't use this command."
                ) from None
            if not result:
                raise app_commands.CheckFailure("You can't use this command.")
        return True

    return app_check


def _make_cog_check_app(cog: commands.Cog) -> Callable[[discord.Interaction], Any]:
    async def app_check(interaction: discord.Interaction) -> bool:
        ctx = await Context.from_interaction(interaction)
        try:
            result = await cog.cog_check(ctx)
        except commands.CommandError as error:
            raise app_commands.CheckFailure(
                str(error) or "You can't use this command."
            ) from None
        if not result:
            raise app_commands.CheckFailure("You can't use this command.")
        return True

    return app_check


def _apply_checks(
    bot: commands.Bot,
    cog: commands.Cog,
    prefix_cmd: commands.Command,
    app_cmd: app_commands.Command,
) -> None:
    user_perms: dict[str, bool] = {}
    runtime_checks: list[Any] = []

    for check in prefix_cmd.checks:
        predicate = getattr(check, "predicate", check)
        qualname = getattr(predicate, "__qualname__", "") or ""
        outer = qualname.split(".", 1)[0]

        try:
            closure = inspect.getclosurevars(predicate)
        except TypeError:
            closure = None
        perms = closure.nonlocals.get("perms") if closure else None

        if outer == "has_permissions" and isinstance(perms, dict):
            user_perms.update(perms)
        elif outer == "guild_only":
            app_cmd.guild_only = True
        else:
            runtime_checks.append(check)

    if user_perms:
        app_cmd.default_permissions = discord.Permissions(**user_perms)
    if runtime_checks:
        app_cmd.add_check(_make_prefix_checks_check(runtime_checks))

    # Prefix cooldowns translate to app command cooldowns. Applying the
    # returned decorator to the app command registers the real predicate.
    buckets = getattr(prefix_cmd, "_buckets", None)
    if buckets is not None and getattr(buckets, "valid", False):
        cooldown = getattr(buckets, "_cooldown", None)
        if cooldown is not None:
            app_commands.checks.cooldown(
                cooldown.rate,
                cooldown.per,
                key=_bucket_key(getattr(buckets, "type", None)),
            )(app_cmd)

    # Cog-wide checks (e.g. Moderation requires the bot to have admin).
    cog_check = getattr(type(cog), "cog_check", None)
    if cog_check is not None and cog_check is not commands.Cog.cog_check:
        app_cmd.add_check(_make_cog_check_app(cog))


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------

def _apply_autocompletes(
    prefix_cmd: commands.Command,
    app_cmd: app_commands.Command,
    param_specs: list[dict[str, Any]],
) -> None:
    """Wire up ``command.autocompletes = {"param": callback}`` mappings.

    Prefix commands can't declare autocomplete on their own, so they hang a
    mapping off the command object and the bridge hands the callbacks to
    Discord. Callbacks must be ``async def (interaction, current)``.
    """
    mapping = getattr(prefix_cmd, "autocompletes", None)
    if not mapping:
        return

    known = {spec["name"] for spec in param_specs}
    for param, callback in mapping.items():
        if param not in known:
            logger.warning(
                "%s declares an autocomplete for unknown parameter %r",
                prefix_cmd.qualified_name,
                param,
            )
            continue
        try:
            app_cmd.autocomplete(param)(callback)
        except (TypeError, ValueError) as error:
            logger.warning(
                "Failed to attach autocomplete for %s.%s: %s",
                prefix_cmd.qualified_name,
                param,
                error,
            )


def _build_leaf(
    bot: commands.Bot,
    cog: commands.Cog,
    prefix_cmd: commands.Command,
    *,
    parent: app_commands.Group | None = None,
    slash_name: str | None = None,
) -> app_commands.Command:
    name = slash_name or prefix_cmd.name

    param_specs: list[dict[str, Any]] = []
    for param_name, param in prefix_cmd.clean_params.items():
        if param_name in ("self", "ctx"):
            continue
        spec = _resolve_param(param)
        spec["name"] = param_name
        param_specs.append(spec)

    flag_specs: list[dict[str, Any]] = []
    params_config = _parameters_config(prefix_cmd)
    signature_names = {spec["name"] for spec in param_specs}
    for flag, config in params_config.items():
        if flag in signature_names:
            continue
        require_value = config.get("require_value", True)
        flag_specs.append(
            {
                "name": flag,
                "annotation": bool if not require_value else str,
                "optional": True,
                "required": False,
                "kind": inspect.Parameter.KEYWORD_ONLY,
                "literal_args": None,
                "converter": None,
                "is_greedy": False,
                "is_var": False,
                "description": config.get("description"),
            }
        )

    callback = _generate_callback(bot, cog, prefix_cmd, param_specs, flag_specs)
    description = _description_for(prefix_cmd)

    app_cmd = app_commands.Command(
        name=name,
        description=description,
        callback=callback,
        parent=parent,
        extras={
            "bridged": True,
            "cog": cog.qualified_name,
            "module": prefix_cmd.module,
            "prefix_command": prefix_cmd.qualified_name,
        },
    )
    _apply_checks(bot, cog, prefix_cmd, app_cmd)
    _apply_autocompletes(prefix_cmd, app_cmd, param_specs)
    return app_cmd


def _build_group_main(
    bot: commands.Bot,
    cog: commands.Cog,
    prefix_cmd: commands.Group,
    group: app_commands.Group,
) -> app_commands.Command | None:
    has_own_params = any(
        name not in ("self", "ctx") for name in prefix_cmd.clean_params
    ) or bool(_parameters_config(prefix_cmd))
    if not has_own_params:
        return None

    parts = [parent.name for parent in getattr(prefix_cmd, "parents", ())]
    qualified = " ".join([*parts, prefix_cmd.name])

    slash_name = GROUP_MAIN_RENAMES.get(qualified)
    if slash_name is None:
        logger.warning(
            "Group %r has its own parameters but no slash name mapping; "
            "skipping its main command",
            qualified,
        )
        return None

    return _build_leaf(bot, cog, prefix_cmd, parent=group, slash_name=slash_name)


def _build_command(
    bot: commands.Bot,
    cog: commands.Cog,
    prefix_cmd: commands.Command,
    *,
    parent: app_commands.Group | None = None,
) -> app_commands.Command | app_commands.Group | None:
    if isinstance(prefix_cmd, commands.Group) and prefix_cmd.commands:
        # NOTE: Group.__init__ auto-registers itself into ``parent`` when
        # ``parent=`` is passed, so it must be constructed without it and
        # added via ``Group.add_command`` below instead.
        group = app_commands.Group(
            name=prefix_cmd.name,
            description=_description_for(prefix_cmd),
            extras={
                "bridged": True,
                "cog": cog.qualified_name,
                "module": prefix_cmd.module,
                "prefix_command": prefix_cmd.qualified_name,
            },
        )
        main = _build_group_main(bot, cog, prefix_cmd, group)
        if main is not None:
            group.add_command(main)
        added = 1 if main is not None else 0
        allowlist = SLASH_SUBCOMMAND_ALLOWLIST.get(prefix_cmd.qualified_name)
        for child in prefix_cmd.commands:
            if allowlist is not None and child.name not in allowlist:
                logger.debug(
                    "Keeping ,%s prefix-only (not in the group's slash allowlist)",
                    child.qualified_name,
                )
                continue
            if added >= SUBCOMMAND_LIMIT:
                logger.warning(
                    "Skipping /%s %s: Discord allows only %d subcommands per "
                    "command and /%s is full; `%s` stays prefix-only.",
                    prefix_cmd.name,
                    child.name,
                    SUBCOMMAND_LIMIT,
                    prefix_cmd.name,
                    child.qualified_name,
                )
                continue
            built = _build_command(bot, cog, child, parent=group)
            if built is not None:
                group.add_command(built)
                added += 1
        return group

    return _build_leaf(bot, cog, prefix_cmd, parent=parent)


def _already_registered(bot: commands.Bot, name: str) -> bool:
    return bot.tree.get_command(name) is not None


def _is_owner_check(check: Any) -> bool:
    predicate = getattr(check, "predicate", check)
    name = (
        getattr(predicate, "__qualname__", "") or getattr(predicate, "__name__", "")
    ).lower()
    return "owner" in name


def _cog_is_owner_gated(cog: commands.Cog) -> bool:
    """Whether a cog's ``cog_check`` restricts it to the bot owner.

    Read off the check's bytecode rather than its name: a cog called
    ``OwnerCog`` whose check allows everyone must not be treated as owner-only,
    while any check that reaches for ``is_owner`` (directly or through an
    ``owner_check`` helper) is an owner gate.
    """
    cog_check = getattr(type(cog), "cog_check", None)
    if cog_check is None or cog_check is commands.Cog.cog_check:
        return False

    func = getattr(cog_check, "__func__", cog_check)
    names = getattr(getattr(func, "__code__", None), "co_names", ()) or ()
    if any("owner" in str(name).lower() for name in names):
        return True
    qualname = (getattr(func, "__qualname__", "") or "").lower()
    return "owner_check" in qualname


def _is_owner_only(prefix_cmd: commands.Command, cog: commands.Cog) -> bool:
    """Whether a command is restricted to the bot owner.

    Covers both ``@commands.is_owner()`` and a cog whose ``cog_check`` is an
    owner gate, so owner tooling can never leak into the member-facing picker
    even when nobody remembers to list it in :data:`SLASH_EXCLUDED`.
    """
    if any(_is_owner_check(check) for check in prefix_cmd.checks):
        return True
    return _cog_is_owner_gated(cog)


def _description_for(prefix_cmd: commands.Command) -> str:
    """A one-line description Discord will accept.

    ``short_doc`` is the docstring's first line, which is empty for every
    command documented with an f-string -- which is most of this bot, because
    the usage lines interpolate the prefix. ``attach_descriptions`` fills those
    in from source; this last-resort summary keeps a bare "…" from ever
    reaching the picker if a future command slips through.
    """
    text = render(prefix_cmd.short_doc).strip()
    if not text:
        text = prefix_cmd.qualified_name.replace("_", " ").title()
    return text.splitlines()[0][:100]


def _iter_app_commands(
    tree_commands: Iterable[Any],
) -> Iterator[Any]:
    """Yield every app command in the tree, groups included."""
    for command in tree_commands:
        yield command
        for child in getattr(command, "commands", ()) or ():
            yield from _iter_app_commands((child,))


def unbridge_extension(bot: commands.Bot, extension: str) -> int:
    """Drop every slash command that was bridged from ``extension``.

    Called when a cog is unloaded or reloaded so the tree never keeps pointing
    at callbacks that no longer exist.
    """
    removed = 0
    for app_cmd in list(_iter_app_commands(bot.tree.get_commands())):
        extras = getattr(app_cmd, "extras", None) or {}
        if not extras.get("bridged") or extras.get("module") != extension:
            continue

        parent = getattr(app_cmd, "parent", None)
        if parent is None:
            bot.tree.remove_command(app_cmd.name)
        else:
            parent.remove_command(app_cmd.name)
        removed += 1

    # A group whose children were all unbridged has nothing left to show.
    for group in list(bot.tree.get_commands()):
        if isinstance(group, app_commands.Group) and not group.commands:
            bot.tree.remove_command(group.name)

    if removed:
        logger.info("Removed %d bridged slash command(s) from %s", removed, extension)
    return removed


def bridge_bot(bot: commands.Bot) -> None:
    """Register slash commands for the prefix commands shown in `,help`.

    Only commands that are visible in the help menu get a slash version, and
    staff/owner tooling (``SLASH_EXCLUDED``) stays prefix-only so the bot never
    blows past Discord's 100-command limit.

    Safe to call again after loading or reloading a cog: commands that are
    already in the tree are skipped, so only new ones get added.
    """
    # Imported lazily: ``cogs.help`` pulls in this module through
    # ``utils.core``, and a module-level import here would deadlock the pair.
    from cogs.help import is_help_visible_cog, is_help_visible_command

    bridged = 1
    skipped = 0

    for cog in bot.cogs.values():
        if not is_help_visible_cog(cog):
            continue
        for prefix_cmd in cog.get_commands():
            if not is_help_visible_command(prefix_cmd):
                skipped += 1
                continue
            if prefix_cmd.name in SLASH_EXCLUDED:
                logger.debug(
                    "Keeping ,%s prefix-only (staff command, no slash version)",
                    prefix_cmd.qualified_name,
                )
                skipped += 1
                continue
            if _is_owner_only(prefix_cmd, cog):
                logger.debug(
                    "Keeping ,%s prefix-only (owner-only command)",
                    prefix_cmd.qualified_name,
                )
                skipped += 1
                continue
            if not NAME_RE.match(prefix_cmd.name):
                logger.warning(
                    "Skipping %s.%s: name is not a valid slash command name",
                    cog.qualified_name,
                    prefix_cmd.name,
                )
                skipped += 1
                continue
            if _already_registered(bot, prefix_cmd.name):
                skipped += 1
                continue

            built = _build_command(bot, cog, prefix_cmd)
            if built is None:
                skipped += 1
                continue

            try:
                bot.tree.add_command(built)
            except app_commands.errors.CommandLimitReached:
                # Discord only allows 100 top-level commands. This should never
                # happen now that the bridge is scoped, but if it does we want a
                # clear pointer instead of a dropped command.
                logger.error(
                    "/%s from %s.%s was NOT registered: Discord's 100-command "
                    "limit is full. Add its prefix name to SLASH_EXCLUDED in "
                    "utils/core/slash_bridge.py.",
                    built.name,
                    cog.qualified_name,
                    prefix_cmd.name,
                )
                skipped += 1
                continue
            except Exception:
                logger.exception(
                    "Failed to register /%s from %s.%s",
                    built.name,
                    cog.qualified_name,
                    prefix_cmd.name,
                )
                skipped += 1
                continue

            bridged += 1
            logger.info(
                "Bridged %s.%s -> /%s",
                cog.qualified_name,
                prefix_cmd.name,
                built.name,
            )

    total = len(bot.tree.get_commands())
    logger.info(
        "Slash bridge complete: %d bridged, %d skipped (%d/%d slash commands used)",
        bridged,
        skipped,
        total,
        100,
    )
    if total >= 95:
        logger.warning(
            "Slash commands are close to Discord's 100-command limit (%d used). "
            "Move more prefix commands into SLASH_EXCLUDED if you need room.",
            total,
        )


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

def _error_message(error: Any) -> str:
    if isinstance(error, app_commands.CommandOnCooldown):
        return f"Slow down — try again in **{error.retry_after:.1f}s**."
    if isinstance(error, app_commands.CheckFailure):
        return str(error) or "You can't use this command."
    if isinstance(
        error,
        (app_commands.errors.CommandNotFound, app_commands.errors.CommandSignatureMismatch),
    ):
        return "That command is out of date. Reopen the picker and try again."
    return user_message_for_error(error)


async def handle_application_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    """Global handler for application command errors."""
    original = error.original if isinstance(error, app_commands.CommandInvokeError) else error

    if isinstance(
        original,
        (app_commands.errors.CommandNotFound, app_commands.errors.CommandSignatureMismatch),
    ):
        # Discord still has a command the bot no longer knows about (stale sync).
        # This is harmless, so log it briefly instead of dumping a traceback.
        logger.warning("Stale application command interaction: %s", original)
    elif isinstance(original, (app_commands.CheckFailure, app_commands.CommandOnCooldown)):
        logger.info(
            "Application command check failure (%s): %s",
            getattr(interaction.command, "name", "?"),
            original,
        )
    else:
        logger.exception(
            "Application command error (%s): %s",
            getattr(interaction.command, "name", "?"),
            original,
        )

    embed = discord.Embed(
        description=_error_message(original),
        color=getattr(interaction.client, "color", discord.Color.red()),
    )
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.HTTPException:
        pass
