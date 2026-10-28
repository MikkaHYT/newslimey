# cogs/boosterrole.py

from __future__ import annotations

import logging
import re

import discord
from discord.ext import commands

from config import EMBED_COLOR
from utils.core.prefixes import prefix_for

logger = logging.getLogger("slimey.boosterrole")

MAX_ROLE_NAME = 100
# Discord's booster pink, used as the starting colour for a new role.
DEFAULT_COLOUR = 0xFF73FA
# Icons and gradient colours are Level 2 perks.
BOOST_HINT = "Role icons and gradient colours need your server to be **Boost Level 2**."

NAMED_COLOURS: dict[str, int] = {
    "red": 0xED4245,
    "green": 0x57F287,
    "blue": 0x5865F2,
    "yellow": 0xFEE75C,
    "purple": 0x9B59B6,
    "pink": 0xEB459E,
    "orange": 0xE67E22,
    "black": 0x000000,
    "white": 0xFFFFFF,
    "gray": 0x99AAB5,
    "grey": 0x99AAB5,
    "cyan": 0x1ABC9C,
    "teal": 0x1ABC9C,
    "lime": 0x2ECC71,
    "gold": 0xF1C40F,
    "magenta": 0xE91E63,
    "blurple": 0x5865F2,
    "fuchsia": 0xEB459E,
    "lavender": 0xB57EDC,
    "mint": 0x98FF98,
    "coral": 0xFF7F50,
    "peach": 0xFFB07C,
}


def text_display(content: str) -> discord.ui.TextDisplay:
    return discord.ui.TextDisplay(content=content)


def small_separator() -> discord.ui.Separator:
    return discord.ui.Separator(spacing=discord.SeparatorSpacing.small)


def simple_view(content: str, *, timeout: int = 60) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=timeout)
    container = discord.ui.Container(accent_color=EMBED_COLOR)
    container.add_item(text_display(content))
    view.add_item(container)
    return view


def parse_colour(raw: str | None) -> int | None:
    """Parse a hex (``#ff0000``), named (``red``) or decimal colour into an int."""
    if raw is None:
        return None

    text = str(raw).strip()
    if not text:
        return None

    key = text.lower().lstrip("#")
    key = key[2:] if key.startswith("0x") else key

    if key in NAMED_COLOURS:
        return NAMED_COLOURS[key]

    # Shorthand hex like #f0a -> #ff00aa.
    if re.fullmatch(r"[0-9a-f]{3}", key) and any(ch in "abcdef" for ch in key):
        key = "".join(ch * 2 for ch in key)

    if re.fullmatch(r"[0-9a-f]{6}", key):
        return int(key, 16)

    if text.isdigit():
        value = int(text)
        if 0 <= value <= 0xFFFFFF:
            return value

    return None


class BoosterRole(commands.Cog, name="BoosterRole"):
    """Lets server boosters build and customize their own personal role."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        # Ensure the tables exist even when the cog is enabled without a restart.
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS booster_roles ("
            "guild_id INTEGER NOT NULL, "
            "user_id INTEGER NOT NULL, "
            "role_id INTEGER NOT NULL, "
            "PRIMARY KEY (guild_id, user_id))"
        )
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS booster_role_access ("
            "guild_id INTEGER NOT NULL, "
            "user_id INTEGER NOT NULL, "
            "PRIMARY KEY (guild_id, user_id))"
        )

    # ==========================================
    # HELPERS
    # ==========================================

    async def _is_allowed(self, guild_id: int, user_id: int) -> bool:
        """Whether a moderator has granted ``user_id`` access via `,br allow`."""
        row = await self.bot.db.fetchval(
            "SELECT 1 FROM booster_role_access WHERE guild_id = %s AND user_id = %s",
            guild_id,
            user_id,
        )
        return bool(row)

    async def _can_use(self, ctx: commands.Context) -> bool:
        """Boosters, and members granted access with `,br allow`, may manage a
        custom role."""
        member = ctx.author
        if isinstance(member, discord.Member):
            if member.premium_since is not None:
                return True
            if await self._is_allowed(ctx.guild.id, member.id):
                return True

        await ctx.send(
            view=simple_view(
                "❌ This is for **server boosters**. Boost the server — or ask a "
                f"moderator to run `{ctx.prefix}br allow @you` — to unlock your own "
                "custom role!"
            )
        )
        return False

    async def _get_role(self, ctx: commands.Context) -> discord.Role | None:
        """Fetch the caller's custom role, cleaning up rows for deleted roles."""
        role_id = await self.bot.db.fetchval(
            "SELECT role_id FROM booster_roles WHERE guild_id = %s AND user_id = %s",
            ctx.guild.id,
            ctx.author.id,
        )
        if not role_id:
            return None

        role = ctx.guild.get_role(int(role_id))
        if role is None:
            await self.bot.db.execute(
                "DELETE FROM booster_roles WHERE guild_id = %s AND user_id = %s",
                ctx.guild.id,
                ctx.author.id,
            )
            return None

        return role

    async def _require_role(self, ctx: commands.Context) -> discord.Role | None:
        role = await self._get_role(ctx)
        if role is None:
            await ctx.send(
                view=simple_view(
                    "❌ You don't have a custom role yet. Create one with "
                    f"`{await prefix_for(ctx)}br create`."
                )
            )
        return role

    async def _move_to_top(self, guild: discord.Guild, role: discord.Role) -> None:
        """Raise *role* as high as the bot is allowed to place it.

        A member's displayed colour comes from their highest role, so sitting
        near the top stops lower roles from overriding the booster role's
        colour. A role can only go below the bot's own highest role, so that is
        the ceiling. Failures are logged, not fatal: the role still exists.
        """
        me = guild.me
        if me is None or me.top_role is None:
            return

        target = me.top_role.position - 1
        if target < 1 or role.position >= target:
            return

        try:
            await role.edit(position=target, reason="Booster role raised above other roles")
        except discord.HTTPException:
            logger.debug("Could not raise booster role %s in guild %s", role.id, guild.id)

    async def _edit(self, ctx: commands.Context, role: discord.Role, **fields) -> bool:
        """Edit the role, reporting Discord's limits in plain language."""
        try:
            await role.edit(reason=f"Booster role [{ctx.author}]", **fields)
            return True
        except discord.Forbidden:
            await ctx.send(
                view=simple_view("❌ I can't edit that role — make sure my role sits above it.")
            )
        except discord.HTTPException as exc:
            if exc.status == 429:
                hint = "Discord only allows a couple of role updates every 10 minutes — try again shortly."
            else:
                hint = BOOST_HINT
            await ctx.send(view=simple_view(f"❌ Discord rejected that change (`{exc.status}`). {hint}"))
        return False

    # ==========================================
    # COMMANDS
    # ==========================================

    @commands.group(name="boosterrole", aliases=["brole", "br"], invoke_without_command=True)
    @commands.guild_only()
    async def br_group(self, ctx: commands.Context) -> None:
        """Create and customize your own booster role."""
        view = discord.ui.LayoutView(timeout=180)
        container = discord.ui.Container(accent_color=EMBED_COLOR)
        container.add_item(
            text_display(
                "# ✦ Booster Roles\n"
                "Boosters get their own personal role to show off.\n\n"
                "**Commands**\n"
                f"`{ctx.prefix}br create` — create your custom role\n"
                f"`{ctx.prefix}br name <name>` — rename it\n"
                f"`{ctx.prefix}br icon <emoji>` — set its icon (or `remove`)\n"
                f"`{ctx.prefix}br colour <colour> [colour2]` — one colour, or two for a gradient\n"
                f"`{ctx.prefix}br info` — view your role\n"
                f"`{ctx.prefix}br share <user>` — let someone wear your role\n"
                f"`{ctx.prefix}br unshare <user>` — take your role back\n"
                f"`{ctx.prefix}br delete` — delete your role\n\n"
                "**Moderators**\n"
                f"`{ctx.prefix}br allow <user>` — let someone use this without boosting\n"
                f"`{ctx.prefix}br deny <user>` — revoke that access\n\n"
                "-# Colours accept hex (`#ff0000`), `<name>` like `red`, or a plain number. "
                "Icons & gradients need Boost Level 2."
            )
        )
        view.add_item(container)
        await ctx.send(view=view)

    @br_group.command(name="create", aliases=["make"])
    @commands.guild_only()
    @commands.bot_has_permissions(manage_roles=True)
    async def br_create(self, ctx: commands.Context) -> None:
        """Create your personal booster role."""
        if not await self._can_use(ctx):
            return

        if await self._get_role(ctx) is not None:
            prefix = await prefix_for(ctx)
            return await ctx.send(
                view=simple_view(
                    "❌ You already have a custom role. "
                    f"Use `{prefix}br name`, `{prefix}br colour`, "
                    f"`{prefix}br icon`, `{prefix}br info`, or "
                    f"`{prefix}br delete`."
                )
            )

        name = f"✦ {ctx.author.display_name}"[:MAX_ROLE_NAME]
        try:
            role = await ctx.guild.create_role(
                name=name,
                colour=DEFAULT_COLOUR,
                permissions=discord.Permissions.none(),
                mentionable=False,
                hoist=False,
                reason=f"Booster role for {ctx.author}",
            )
            await self._move_to_top(ctx.guild, role)
            await ctx.author.add_roles(role, reason="Booster role")
        except discord.Forbidden:
            return await ctx.send(
                view=simple_view("❌ I don't have permission to create or assign roles here.")
            )
        except discord.HTTPException as exc:
            return await ctx.send(
                view=simple_view(
                    f"❌ Discord rejected that (`{exc.status}`). The server may be at its role limit."
                )
            )

        await self.bot.db.execute(
            "INSERT INTO booster_roles (guild_id, user_id, role_id) VALUES (%s, %s, %s) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET role_id = excluded.role_id",
            ctx.guild.id,
            ctx.author.id,
            role.id,
        )

        await ctx.send(
            view=simple_view(
                f"✦ Created your custom role {role.mention}!\n\n"
                f"Customize it with `{ctx.prefix}br name`, `{ctx.prefix}br colour`, and `{ctx.prefix}br icon`."
            )
        )

    @br_group.command(name="name")
    @commands.guild_only()
    @commands.bot_has_permissions(manage_roles=True)
    async def br_name(self, ctx: commands.Context, *, name: str) -> None:
        """Rename your personal role."""
        if not await self._can_use(ctx):
            return

        role = await self._require_role(ctx)
        if role is None:
            return

        clean = name.strip()
        if not clean:
            return await ctx.send(view=simple_view("❌ Give me a name to use."))
        if len(clean) > MAX_ROLE_NAME:
            return await ctx.send(
                view=simple_view(f"❌ Role names can be at most **{MAX_ROLE_NAME} characters**.")
            )

        if await self._edit(ctx, role, name=clean):
            await ctx.send(view=simple_view(f"✦ Renamed your role to **{clean}**."))

    @br_group.command(name="icon", aliases=["emoji"])
    @commands.guild_only()
    @commands.bot_has_permissions(manage_roles=True)
    async def br_icon(self, ctx: commands.Context, *, icon: str) -> None:
        """Set (or remove) your role icon."""
        if not await self._can_use(ctx):
            return

        role = await self._require_role(ctx)
        if role is None:
            return

        icon = icon.strip()
        if icon.lower() in {"remove", "none", "clear", "reset", "delete"}:
            if await self._edit(ctx, role, display_icon=None):
                await ctx.send(view=simple_view("✦ Removed your role icon."))
            return

        try:
            partial = discord.PartialEmoji.from_str(icon)
        except Exception:
            partial = None

        if partial is not None and partial.id is not None:
            # Custom server emoji: download it as a PNG.
            try:
                data = await self.bot.proxied_session.read(
                    f"https://cdn.discordapp.com/emojis/{partial.id}.png?size=128"
                )
            except Exception:
                return await ctx.send(view=simple_view("❌ I couldn't download that emoji."))
            if await self._edit(ctx, role, display_icon=data):
                await ctx.send(view=simple_view(f"✦ Set your role icon to {icon}."))
            return

        if not any(ord(ch) > 127 for ch in icon):
            return await ctx.send(
                view=simple_view(
                    "❌ That doesn't look like an emoji. Use a unicode emoji or a custom server emoji."
                )
            )

        if await self._edit(ctx, role, display_icon=icon):
            await ctx.send(view=simple_view(f"✦ Set your role icon to {icon}."))

    @br_group.command(name="colour", aliases=["color"])
    @commands.guild_only()
    @commands.bot_has_permissions(manage_roles=True)
    async def br_colour(
        self,
        ctx: commands.Context,
        colour_one: str,
        colour_two: str | None = None,
    ) -> None:
        """Give your role a solid colour, or two for a gradient."""
        if not await self._can_use(ctx):
            return

        role = await self._require_role(ctx)
        if role is None:
            return

        first = parse_colour(colour_one)
        if first is None:
            return await ctx.send(
                view=simple_view(
                    f"❌ `{colour_one}` isn't a valid colour. Try `#ff0000`, `red`, or a number."
                )
            )

        second = None
        if colour_two is not None:
            second = parse_colour(colour_two)
            if second is None:
                return await ctx.send(
                    view=simple_view(f"❌ `{colour_two}` isn't a valid colour.")
                )

        if second is None:
            if await self._edit(ctx, role, colour=first, secondary_colour=None):
                await ctx.send(
                    view=simple_view(f"✦ Set your role to a solid colour (`#{first:06X}`).")
                )
            return

        if await self._edit(ctx, role, colour=first, secondary_colour=second):
            await ctx.send(
                view=simple_view(
                    f"✦ Set your role to a gradient (`#{first:06X}` → `#{second:06X}`)."
                )
            )

    @br_group.command(name="info", aliases=["view", "show"])
    @commands.guild_only()
    async def br_info(self, ctx: commands.Context) -> None:
        """Show your custom role."""
        role = await self._get_role(ctx)
        if role is None:
            return await ctx.send(
                view=simple_view(
                    "❌ You don't have a custom role yet. Create one with "
                    f"`{await prefix_for(ctx)}br create`."
                )
            )

        colour_line = f"`#{role.colour.value:06X}`"
        secondary = getattr(role, "secondary_colour", None)
        if secondary is not None:
            colour_line += f" → `#{secondary.value:06X}`"

        view = discord.ui.LayoutView(timeout=180)
        container = discord.ui.Container(accent_color=role.colour if role.colour.value else EMBED_COLOR)
        container.add_item(
            text_display(
                f"# ✦ {role.name}\n\n"
                f"**Colour**\n{colour_line}\n"
            )
        )
        container.add_item(small_separator())
        container.add_item(
            text_display(
                f"**Icon**\n{'Set' if role.display_icon else 'Not set'}\n\n"
                f"**Role**\n{role.mention}"
            )
        )
        view.add_item(container)
        await ctx.send(view=view)

    @br_group.command(name="share", aliases=["lend"])
    @commands.guild_only()
    @commands.bot_has_permissions(manage_roles=True)
    async def br_share(self, ctx: commands.Context, member: discord.Member) -> None:
        """Give another member your personal role so they can wear it too."""
        if not await self._can_use(ctx):
            return

        role = await self._require_role(ctx)
        if role is None:
            return

        if member.id == ctx.author.id:
            return await ctx.send(view=simple_view("❌ That's your own role — it's already yours!"))
        if member.bot:
            return await ctx.send(view=simple_view("❌ Bots can't wear booster roles."))
        if role in member.roles:
            return await ctx.send(
                view=simple_view(f"❌ {member.mention} already has your role.")
            )

        try:
            await member.add_roles(role, reason=f"Booster role shared by {ctx.author}")
        except discord.Forbidden:
            return await ctx.send(
                view=simple_view(
                    "❌ I can't assign that role — make sure my role sits above it and I have "
                    "**Manage Roles**."
                )
            )
        except discord.HTTPException as exc:
            return await ctx.send(view=simple_view(f"❌ Discord rejected that (`{exc.status}`)."))

        await ctx.send(
            view=simple_view(
                f"✦ Shared {role.mention} with {member.mention}!\n\n"
                f"Take it back any time with `{ctx.prefix}br unshare {member.mention}`."
            )
        )

    @br_group.command(name="unshare", aliases=["reclaim"])
    @commands.guild_only()
    @commands.bot_has_permissions(manage_roles=True)
    async def br_unshare(self, ctx: commands.Context, member: discord.Member) -> None:
        """Take your shared role back from a member."""
        if not await self._can_use(ctx):
            return

        role = await self._require_role(ctx)
        if role is None:
            return

        if role not in member.roles:
            return await ctx.send(
                view=simple_view(f"❌ {member.mention} doesn't have your role.")
            )

        try:
            await member.remove_roles(role, reason=f"Booster role unshared by {ctx.author}")
        except discord.Forbidden:
            return await ctx.send(
                view=simple_view("❌ I can't remove that role — make sure my role sits above it.")
            )
        except discord.HTTPException as exc:
            return await ctx.send(view=simple_view(f"❌ Discord rejected that (`{exc.status}`)."))

        await ctx.send(
            view=simple_view(f"✦ Took {role.mention} back from {member.mention}.")
        )

    @br_group.command(name="allow", aliases=["permit", "grant"])
    @commands.guild_only()
    async def br_allow(self, ctx: commands.Context, member: discord.Member) -> None:
        """Let a member create a booster role without boosting."""
        if not (
            await self.bot.is_owner(ctx.author)
            or ctx.author.guild_permissions.manage_guild
        ):
            return await ctx.send(
                view=simple_view(
                    "You're missing the **Manage Server** permission to manage "
                    "booster role access."
                )
            )
        if member.bot:
            return await ctx.send(view=simple_view("❌ Bots can't use booster roles."))

        await self.bot.db.execute(
            "INSERT INTO booster_role_access (guild_id, user_id) VALUES (%s, %s) "
            "ON CONFLICT(guild_id, user_id) DO NOTHING",
            ctx.guild.id,
            member.id,
        )
        await ctx.send(
            view=simple_view(
                f"✦ {member.mention} can now create and manage their own booster "
                f"role with `{ctx.prefix}br create`."
            )
        )

    @br_group.command(name="deny", aliases=["revoke"])
    @commands.guild_only()
    async def br_deny(self, ctx: commands.Context, member: discord.Member) -> None:
        """Revoke a member's granted booster role access."""
        if not (
            await self.bot.is_owner(ctx.author)
            or ctx.author.guild_permissions.manage_guild
        ):
            return await ctx.send(
                view=simple_view(
                    "You're missing the **Manage Server** permission to manage "
                    "booster role access."
                )
            )

        await self.bot.db.execute(
            "DELETE FROM booster_role_access WHERE guild_id = %s AND user_id = %s",
            ctx.guild.id,
            member.id,
        )
        await ctx.send(
            view=simple_view(
                f"✦ {member.mention} can no longer create a booster role without boosting."
            )
        )

    @br_group.command(name="delete", aliases=["remove", "del"])
    @commands.guild_only()
    @commands.bot_has_permissions(manage_roles=True)
    async def br_delete(self, ctx: commands.Context) -> None:
        """Delete your personal role."""
        if not await self._can_use(ctx):
            return

        role = await self._require_role(ctx)
        if role is None:
            return

        try:
            await role.delete(reason=f"Booster role removed [{ctx.author}]")
        except discord.Forbidden:
            return await ctx.send(
                view=simple_view("❌ I can't delete that role — make sure my role sits above it.")
            )
        except discord.HTTPException as exc:
            return await ctx.send(view=simple_view(f"❌ Discord rejected that (`{exc.status}`)."))

        await self.bot.db.execute(
            "DELETE FROM booster_roles WHERE guild_id = %s AND user_id = %s",
            ctx.guild.id,
            ctx.author.id,
        )
        await ctx.send(view=simple_view("✦ Deleted your custom role."))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BoosterRole(bot))
