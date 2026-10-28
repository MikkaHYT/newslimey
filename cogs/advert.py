"""Broadcasting: the invite advert, and per-server leak posts.

``,advert`` (bot owner) posts the invite card to every server the bot is in,
right away — it prefers an announcements channel and falls back to a general
one, and skips a server entirely when it has neither.

``,customiseadvert`` (bot owner) swaps the built-in advert card for a custom
script, previews the current advert, or resets back to the card. Two script
types are accepted: an embed script (the same ``{key: value}$v...`` syntax as
``,ce``) or a Components V2 script (``{component}`` + ``{container}`` /
``{text}`` / ``{separator}`` / ``{button}`` fields).

``,setleakchannel`` (any server, needs Manage Server) records the channel each
server wants leak posts delivered to. ``,setleakping`` (server owner, Manage
Server, or bot admin) picks a role to ping underneath each leak post.
``,postleaktoall`` (bot owner) fans an embed script — the same
``{key: value}$v...`` syntax as ``,ce`` — or a plain message out to every
server's leak channel, then pings each server's leak role under its post.
Servers without a leak channel are simply skipped.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

import discord
from discord.ext import commands

from config import EMBED_COLORS
from utils.core.prefixes import prefix_for

logger = logging.getLogger("slimey.advert")

#: Per-server: the channel id leak posts are delivered to.
LEAK_CHANNEL_KEY = "leak_channel"
#: Per-server: the role id pinged underneath each leak post, if one is set.
LEAK_PING_KEY = "leak_ping_role"
#: Bot-wide: a custom script for the advert. Unset uses the built-in card.
ADVERT_SCRIPT_KEY = "advert_script"
#: Script fields are separated by this token (same as ``,ce``).
COMPONENT_FIELD_SEP = "$v"
#: Field keys that mark a script as Components V2 rather than a ``,ce`` embed.
COMPONENT_KEYS = frozenset({"component", "container", "text", "separator"})

INVITE_CODE = "discord.gg/outsiders"
INVITE_URL = f"https://{INVITE_CODE}"

ACCENT = EMBED_COLORS.get("gold", 0xF1C40F)

#: How many channels a broadcast talks to at once.
BROADCAST_CONCURRENCY = 5

#: Channel-name hints, tried in order, when ,advert picks its destination.
ANNOUNCEMENT_HINTS = ("announcement", "announcements", "news")
GENERAL_HINTS = ("general", "chat", "main")


def _separator() -> discord.ui.Separator:
    return discord.ui.Separator(spacing=discord.SeparatorSpacing.small)


def advertisement() -> discord.ui.LayoutView:
    """The advertisement card: where to go and what's waiting there."""
    box = discord.ui.Container(accent_color=ACCENT)
    box.add_item(
        discord.ui.TextDisplay(f"# 💰 The second home of TPNE bot\n-# {INVITE_CODE}")
    )
    box.add_item(_separator())
    box.add_item(
        discord.ui.TextDisplay(
            "### 🎁 Regular money drops & other prizes\n"
            f"Join **{INVITE_CODE}** for **regular money drops**, giveaways and "
            "**other prizes** on top — it's the second home of TPNE bot."
        )
    )
    box.add_item(_separator())
    box.add_item(
        discord.ui.ActionRow(
            discord.ui.Button(
                label=f"Join {INVITE_CODE}",
                emoji="🎉",
                style=discord.ButtonStyle.link,
                url=INVITE_URL,
            )
        )
    )
    box.add_item(discord.ui.TextDisplay("-# Free to join · come say hi!"))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(box)
    return view


def _simple_view(content: str, *, timeout: int = 300) -> discord.ui.LayoutView:
    """A one-off Components V2 card for command feedback."""
    box = discord.ui.Container(accent_color=ACCENT)
    box.add_item(discord.ui.TextDisplay(content))
    view = discord.ui.LayoutView(timeout=timeout)
    view.add_item(box)
    return view


def _settings(bot: commands.Bot):
    return getattr(bot, "dash_settings", None)


@commands.check
async def _is_guild_owner(ctx: commands.Context) -> bool:
    """Let a server's own owner through even if their cached perms look odd."""
    guild = getattr(ctx, "guild", None)
    return guild is not None and ctx.author.id == guild.owner_id


def _advert_script(bot: commands.Bot) -> str | None:
    """The stored custom advert script, or ``None`` when using the default card."""
    settings = _settings(bot)
    if settings is None:
        return None
    value = settings.stored(ADVERT_SCRIPT_KEY)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _field_key(raw: str) -> tuple[str, str]:
    """Split a ``{key: value}`` field (or a bare ``{key}``) into its parts."""
    body = raw.strip()
    if body.startswith("{"):
        body = body[1:]
    if body.endswith("}"):
        body = body[:-1]
    if ":" not in body:
        return body.strip().lower(), ""
    key, _, value = body.partition(":")
    return key.strip().lower(), value.strip()


def _component_fields(script: str) -> list[str]:
    return [field for field in script.split(COMPONENT_FIELD_SEP) if field.strip()]


def _is_component_script(script: str) -> bool:
    """True when *script* is a Components V2 script rather than a ``,ce`` embed."""
    keys = {_field_key(field)[0] for field in _component_fields(script)}
    return bool(keys & COMPONENT_KEYS)


def _accent_from(value: str) -> int | None:
    """A container accent colour from a hex string or a palette name."""
    if not value:
        return None
    from cogs.createembed import parse_hex_color

    colour = parse_hex_color(value)
    if colour is not None:
        return colour
    return EMBED_COLORS.get(value.strip().lower())


def _separator_spacing(value: str) -> discord.SeparatorSpacing:
    if value.strip().lower() in {"large", "big"}:
        return discord.SeparatorSpacing.large
    return discord.SeparatorSpacing.small


def _component_button(value: str):
    """Parse ``style && label && url && emoji && enabled`` into a (button, warning)."""
    from cogs.createembed import valid_http_url

    parts = [part.strip() for part in value.split("&&")]
    style = (parts[0] if parts else "link").lower() or "link"
    label = parts[1] if len(parts) > 1 and parts[1] else "Button"
    url = parts[2] if len(parts) > 2 else ""
    emoji = parts[3] if len(parts) > 3 else ""
    disabled = len(parts) > 4 and parts[4].lower() in {
        "disabled",
        "false",
        "off",
        "no",
        "0",
    }

    if style != "link":
        return None, f"button style `{style}` (only `link` buttons work in broadcasts)"
    if not valid_http_url(url):
        return None, "button (needs an `http(s)://` URL)"

    try:
        button = discord.ui.Button(
            style=discord.ButtonStyle.link,
            url=url,
            label=label[:80],
            emoji=emoji or None,
            disabled=disabled,
        )
    except Exception:
        button = discord.ui.Button(
            style=discord.ButtonStyle.link,
            url=url,
            label=label[:80],
            disabled=disabled,
        )
    return button, None


def _build_component_layout(ctx: commands.Context, script: str):
    """Build a Components V2 ``LayoutView`` from a component script.

    Returns ``(view, warnings)``; ``view`` is ``None`` when the script produced
    no visible content.
    """
    from cogs.createembed import _resolve_placeholders

    view = discord.ui.LayoutView(timeout=None)
    container: discord.ui.Container | None = None
    warnings: list[str] = []

    def active_container() -> discord.ui.Container:
        nonlocal container
        if container is None:
            container = discord.ui.Container()
            view.add_item(container)
        return container

    for raw in _component_fields(script):
        key, value = _field_key(raw)
        value = _resolve_placeholders(value, ctx)

        if key == "component":
            continue
        if key == "container":
            container = discord.ui.Container(accent_color=_accent_from(value))
            view.add_item(container)
        elif key == "text":
            active_container().add_item(discord.ui.TextDisplay(value))
        elif key == "separator":
            active_container().add_item(
                discord.ui.Separator(spacing=_separator_spacing(value))
            )
        elif key == "button":
            button, warning = _component_button(value)
            if warning:
                warnings.append(warning)
            if button is not None:
                active_container().add_item(discord.ui.ActionRow(button))
        else:
            warnings.append(f"unknown field `{key}`")

    if container is None:
        return None, warnings
    return view, warnings


class _TargetContext:
    """A view of the invoking context pointed at one target server.

    ``build_script_embed`` resolves ``{guild.*}`` and ``{channel.*}``
    placeholders against the context, so during a broadcast each channel gets a
    context whose ``guild`` and ``channel`` are that server's while ``author``
    still falls through to the invoker.
    """

    def __init__(self, ctx: commands.Context, channel) -> None:
        self._ctx = ctx
        self.guild = getattr(channel, "guild", None) or getattr(ctx, "guild", None)
        self.channel = channel

    def __getattr__(self, name: str):
        return getattr(self._ctx, name)


def _compile_advert(ctx: commands.Context, script: str):
    """Compile an advert script into ``channel.send`` kwargs.

    Returns ``(payload, warnings)``; ``payload`` is ``None`` when the script is
    unusable. Payloads are either ``{"embed": ..., "view": ...}`` (embed script)
    or ``{"view": <LayoutView>}`` (component script).
    """
    if _is_component_script(script):
        view, warnings = _build_component_layout(ctx, script)
        if view is None:
            return None, warnings
        return {"view": view}, warnings

    from cogs.createembed import _embed_has_content, build_script_embed

    try:
        embed, view, warnings = build_script_embed(ctx, script)
    except Exception:
        logger.exception("Could not parse the advert script")
        return None, ["could not read that script"]
    if not _embed_has_content(embed):
        return None, warnings
    return {"embed": embed, "view": view}, warnings


def _can_send(channel: discord.abc.GuildChannel, me: discord.Member) -> bool:
    perms = channel.permissions_for(me)
    return perms.send_messages and perms.embed_links


def _advert_channel(guild: discord.Guild) -> discord.TextChannel | None:
    """The announcements channel, else a general one, else ``None``."""
    me = guild.me
    if me is None:
        return None
    sendable = [channel for channel in guild.text_channels if _can_send(channel, me)]
    for hints in (ANNOUNCEMENT_HINTS, GENERAL_HINTS):
        for channel in sendable:
            name = channel.name.lower()
            if any(hint in name for hint in hints):
                return channel
    return None


class Advertise(commands.Cog):
    """Broadcast the advert, and route leak posts per server."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _broadcast(
        self,
        channels: list,
        payload_for: Callable[[object], dict],
    ) -> tuple[list, int]:
        """Send ``payload_for(channel)`` to each channel.

        Returns ``(posted, failed)``, where ``posted`` is the list of
        ``(channel, message)`` pairs that actually went out.
        """
        semaphore = asyncio.Semaphore(BROADCAST_CONCURRENCY)
        posted: list = []
        failed = 0

        async def worker(channel) -> None:
            nonlocal failed
            async with semaphore:
                try:
                    message = await channel.send(**payload_for(channel))
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    failed += 1
                    return
                posted.append((channel, message))

        await asyncio.gather(*(worker(channel) for channel in channels))
        return posted, failed

    async def _broadcast_custom(
        self, ctx: commands.Context, channels: list, script: str
    ) -> tuple[list, int]:
        """Post a ``,ce``-style embed script to each channel.

        Returns ``(posted, failed)``, where ``posted`` is the list of
        ``(channel, message)`` pairs that actually went out.
        """
        from cogs.createembed import EmbedDraft

        createembed = self.bot.get_cog("CreateEmbed")
        semaphore = asyncio.Semaphore(BROADCAST_CONCURRENCY)
        posted: list = []
        failed = 0

        async def worker(channel) -> None:
            nonlocal failed
            async with semaphore:
                # Recompile per target so {guild.name} / {guild.icon} and the
                # other {guild.*} / {channel.*} placeholders reflect that server.
                payload, _warnings = _compile_advert(
                    _TargetContext(ctx, channel), script
                )
                if payload is None:
                    failed += 1
                    return
                try:
                    message = await channel.send(**payload)
                    view = payload.get("view")
                    specs = getattr(view, "button_specs", None)
                    ids = getattr(view, "button_ids", None)
                    if createembed is not None and specs and ids:
                        try:
                            await createembed.persist_buttons(
                                message, EmbedDraft(buttons=specs), ids
                            )
                        except Exception:
                            logger.exception("Could not persist broadcast buttons")
                    posted.append((channel, message))
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    failed += 1

        await asyncio.gather(*(worker(channel) for channel in channels))
        return posted, failed

    def _leak_channels(self) -> list:
        """Every channel any server has set as its leak channel."""
        settings = _settings(self.bot)
        if settings is None:
            return []
        channels = []
        for guild in self.bot.guilds:
            raw = settings.stored(LEAK_CHANNEL_KEY, guild_id=guild.id)
            if raw in (None, ""):
                continue
            try:
                channel = guild.get_channel(int(raw))
            except (TypeError, ValueError):
                continue
            if isinstance(channel, discord.abc.Messageable):
                channels.append(channel)
        return channels

    async def _ping_leak_roles(self, posted: list) -> int:
        """Ping each server's leak role beneath the message just posted there.

        Runs after a broadcast so the ping lands in the same channel, directly
        under the leak post. Servers with no role set, or a role that has since
        been deleted, are skipped silently.
        """
        settings = _settings(self.bot)
        if settings is None or not posted:
            return 0

        semaphore = asyncio.Semaphore(BROADCAST_CONCURRENCY)
        pinged = 0

        async def worker(channel, message) -> None:
            nonlocal pinged
            guild = getattr(channel, "guild", None)
            if guild is None:
                return
            raw = settings.stored(LEAK_PING_KEY, guild_id=guild.id)
            if raw in (None, ""):
                return
            try:
                role = guild.get_role(int(raw))
            except (TypeError, ValueError):
                return
            if role is None:
                return
            async with semaphore:
                try:
                    await channel.send(
                        role.mention,
                        reference=message,
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions(
                            roles=True, everyone=False
                        ),
                    )
                    pinged += 1
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    logger.exception(
                        "Could not ping leak role %s in guild %s", role.id, guild.id
                    )

        await asyncio.gather(
            *(worker(channel, message) for channel, message in posted)
        )
        return pinged

    # ------------------------------------------------------------------
    # Per-server leak channel
    # ------------------------------------------------------------------

    @commands.command(name="setleakchannel", aliases=["leakchannel", "setleaks"])
    @commands.guild_only()
    @commands.check_any(
        commands.is_owner(), commands.has_permissions(manage_guild=True)
    )
    async def setleakchannel(
        self, ctx: commands.Context, channel: discord.TextChannel = None
    ) -> None:
        """Set the channel this server receives leak posts in.

        Usage: `{prefix}setleakchannel #channel` — omit the channel to use the
        current one.
        """
        settings = _settings(self.bot)
        if settings is None:
            await ctx.reply("The settings store is unavailable right now.")
            return

        target = channel or ctx.channel
        try:
            await settings.set_value(
                LEAK_CHANNEL_KEY, target.id, guild_id=ctx.guild.id
            )
        except Exception:
            logger.exception("Could not save leak channel for %s", ctx.guild.id)
            await ctx.reply("I couldn't save that channel — try again in a moment.")
            return

        await ctx.reply(
            f"✅ Leak posts for this server will now go to {target.mention}."
        )

    @setleakchannel.error
    async def setleakchannel_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply(
                "You need the **Manage Server** permission to set the leak channel."
            )
            return
        if isinstance(error, commands.ChannelNotFound):
            await ctx.reply("I couldn't find that channel — mention a text channel.")
            return
        logger.exception("[setleakchannel] Unhandled error: %s", error)

    # ------------------------------------------------------------------
    # Per-server leak ping role
    # ------------------------------------------------------------------

    @commands.command(name="setleakping", aliases=["leakping", "setleakrole"])
    @commands.guild_only()
    @commands.check_any(
        commands.is_owner(),
        commands.has_permissions(manage_guild=True),
        _is_guild_owner,
    )
    async def setleakping(self, ctx: commands.Context, *, role: str = None) -> None:
        """Choose the role pinged underneath this server's leak posts.

        Usage: `{prefix}setleakping @role` sets the role, `{prefix}setleakping off`
        stops the ping, and `{prefix}setleakping` shows the current setting.
        """
        settings = _settings(self.bot)
        if settings is None:
            await ctx.reply("The settings store is unavailable right now.")
            return

        raw = (role or "").strip()

        if raw.lower() in {"off", "none", "clear", "reset", "disable", "remove"}:
            try:
                await settings.set_value(LEAK_PING_KEY, None, guild_id=ctx.guild.id)
            except Exception:
                logger.exception("Could not clear leak ping role for %s", ctx.guild.id)
                await ctx.reply("I couldn't save that — try again in a moment.")
                return
            await ctx.reply("✅ Leak posts will no longer ping a role here.")
            return

        if not raw:
            current = settings.stored(LEAK_PING_KEY, guild_id=ctx.guild.id)
            role_obj = None
            if current not in (None, ""):
                try:
                    role_obj = ctx.guild.get_role(int(current))
                except (TypeError, ValueError):
                    role_obj = None
            prefix = await prefix_for(ctx)
            body = (
                "# Command: setleakping\n"
                "Pick the role pinged **under** every leak post this server "
                "receives.\n\n"
                f"`{prefix}setleakping @role` — set the role\n"
                f"`{prefix}setleakping off` — stop pinging\n\n"
            )
            if role_obj is not None:
                body += f"-# **Current:** {role_obj.mention}"
            else:
                body += "-# **Current:** no role set"
            await ctx.send(view=_simple_view(body))
            return

        try:
            target = await commands.RoleConverter().convert(ctx, raw)
        except commands.BadArgument:
            await ctx.reply(
                "I couldn't find that role — mention it or give its name/id."
            )
            return

        try:
            await settings.set_value(LEAK_PING_KEY, target.id, guild_id=ctx.guild.id)
        except Exception:
            logger.exception("Could not save leak ping role for %s", ctx.guild.id)
            await ctx.reply("I couldn't save that — try again in a moment.")
            return

        await ctx.reply(f"✅ Leak posts here will ping {target.mention} from now on.")

    @setleakping.error
    async def setleakping_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.NoPrivateMessage):
            await ctx.reply("Set the leak ping role inside a server.")
            return
        if isinstance(error, commands.CheckAnyFailure):
            await ctx.reply(
                "You need the **Manage Server** permission, or to own this "
                "server, to set the leak ping role."
            )
            return
        logger.exception("[setleakping] Unhandled error: %s", error)

    # ------------------------------------------------------------------
    # Owner broadcasts
    # ------------------------------------------------------------------

    @commands.command(name="postleaktoall", aliases=["leaktoall"])
    @commands.is_owner()
    async def postleaktoall(
        self, ctx: commands.Context, *, content: str = None
    ) -> None:
        """Post an embed script or a plain message to every leak channel.

        Usage: `{prefix}postleaktoall <embed script>` or
        `{prefix}postleaktoall <message>`. The embed script uses the same
        `{key: value}$v...` syntax as `{prefix}ce`.
        """
        if not content or not content.strip():
            prefix = await prefix_for(ctx)
            await ctx.send(
                view=_simple_view(
                    "# Command: postleaktoall\n"
                    "Sends a message to **every** server's leak channel.\n\n"
                    f"**Embed script** (like `{prefix}ce`):\n"
                    f"`{prefix}postleaktoall {{title: New leak}}$v{{description: ...}}`\n\n"
                    "**Plain message:**\n"
                    f"`{prefix}postleaktoall hey everyone`\n\n"
                    "-# Servers without a leak channel are skipped. Servers that "
                    f"set a role with `{prefix}setleakping` get it pinged under the post."
                )
            )
            return

        channels = self._leak_channels()
        if not channels:
            await ctx.reply(
                "No server has a leak channel set yet. Servers set one with "
                f"`{await prefix_for(ctx)}setleakchannel`."
            )
            return

        content = content.strip()
        use_script = _compile_advert(ctx, content)[0] is not None

        status = await ctx.reply(
            f"📣 Posting to **{len(channels)}** leak channel(s)..."
        )

        if use_script:
            posted, failed = await self._broadcast_custom(ctx, channels, content)
        else:
            posted, failed = await self._broadcast(
                channels, lambda _channel: {"content": content}
            )

        # The ping is a second message, so it lands *under* each leak post.
        pinged = await self._ping_leak_roles(posted)

        summary = f"✅ Posted to **{len(posted)}** leak channel(s)."
        if failed:
            summary += f" ⚠️ **{failed}** failed."
        if pinged:
            summary += f" 🔔 Pinged the leak role in **{pinged}**."
        try:
            await status.edit(content=summary)
        except discord.HTTPException:
            await ctx.send(summary)

    @commands.command(name="ad", aliases=["advert", "advertise"])
    @commands.is_owner()
    async def ad(self, ctx: commands.Context) -> None:
        """Post the advert to every server, immediately.

        Usage: `{prefix}advert` — posts to an announcements channel in every
        server, falling back to a general channel. Servers with neither are
        skipped. Customise the advert with `{prefix}customiseadvert`.
        """
        targets = []
        for guild in self.bot.guilds:
            channel = _advert_channel(guild)
            if channel is not None:
                targets.append(channel)

        if not targets:
            await ctx.reply(
                "I couldn't find an announcements or general channel in any server."
            )
            return

        status = await ctx.reply(
            f"📣 Posting the advert to **{len(targets)}** server(s)..."
        )

        script = _advert_script(self.bot)
        if script:
            posted, failed = await self._broadcast_custom(ctx, targets, script)
        else:
            posted, failed = await self._broadcast(
                targets, lambda _channel: {"view": advertisement()}
            )

        summary = f"✅ Advert posted to **{len(posted)}** server(s)."
        if failed:
            summary += f" ⚠️ **{failed}** failed."
        try:
            await status.edit(content=summary)
        except discord.HTTPException:
            await ctx.send(summary)

    @commands.command(
        name="customiseadvert",
        aliases=["customizeadvert", "setadvert", "advertcustomise"],
    )
    @commands.is_owner()
    async def customiseadvert(
        self, ctx: commands.Context, *, script: str = None
    ) -> None:
        """Customise the advert that `{prefix}advert` posts everywhere.

        Accepts either an embed script (same syntax as `{prefix}ce`) or a
        Components V2 script (`{component}` with `{container}` / `{text}` /
        `{separator}` / `{button}` fields, separated by `$v`).
        `{prefix}customiseadvert preview` shows the current advert here.
        `{prefix}customiseadvert reset` restores the built-in card.
        """
        settings = _settings(self.bot)
        if settings is None:
            await ctx.reply("The settings store is unavailable right now.")
            return

        if not script or not script.strip():
            current = _advert_script(self.bot)
            prefix = await prefix_for(ctx)
            body = (
                "# Command: customiseadvert\n"
                f"Set the advert `{prefix}advert` posts to **every** server.\n\n"
                f"**Embed script** (same syntax as `{prefix}ce`):\n"
                f"`{prefix}customiseadvert {{title: ...}}$v{{description: ...}}$v"
                "{color: #F1C40F}$v{button: https://... && Join && enabled}`\n\n"
                "**Component script** (Components V2):\n"
                f"`{prefix}customiseadvert {{component}}$v{{container: #f1c40f}}$v"
                "{text: # Title}$v{separator: small}$v"
                "{button: link && Join && https://... && 🎉 && enabled}`\n\n"
                f"`{prefix}customiseadvert preview` — show the current advert here\n"
                f"`{prefix}customiseadvert reset` — go back to the built-in card\n\n"
            )
            if current:
                body += "-# **Current:** custom script set\n```\n" + current[:900] + "\n```"
            else:
                body += "-# **Current:** built-in card (not customised)"
            await ctx.send(view=_simple_view(body))
            return

        arg = script.strip()
        lowered = arg.lower()

        if lowered in {"reset", "clear", "default", "none"}:
            try:
                await settings.set_value(ADVERT_SCRIPT_KEY, None)
            except Exception:
                logger.exception("Could not clear the advert script")
                await ctx.reply("I couldn't save that — try again in a moment.")
                return
            await ctx.reply("✅ The advert is back to the built-in card.")
            return

        if lowered == "preview":
            current = _advert_script(self.bot)
            payload = _compile_advert(ctx, current)[0] if current else None
            try:
                if payload is not None:
                    await ctx.send(**payload)
                else:
                    await ctx.send(view=advertisement())
            except discord.HTTPException:
                logger.exception("Discord rejected the advert preview")
                await ctx.reply("I couldn't post the preview right now.")
            return

        payload, warnings = _compile_advert(ctx, arg)
        if payload is None:
            await ctx.reply(
                "❌ That script didn't produce a title, description, text or image, "
                "so it wouldn't render as an advert."
            )
            return

        try:
            await settings.set_value(ADVERT_SCRIPT_KEY, arg)
        except Exception:
            logger.exception("Could not save the advert script")
            await ctx.reply("I couldn't save that — try again in a moment.")
            return

        summary = (
            f"✅ Advert customised — `{await prefix_for(ctx)}advert` will now post it "
            "to every server."
        )
        if warnings:
            summary += "\n⚠️ Ignored: " + ", ".join(warnings)
        await ctx.reply(summary)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Advertise(bot))
