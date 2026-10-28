# cogs/groupbuystats.py
#
# A single, persistent "Groupbuy Information" card per server. The card is
# posted with ``!gbstats create [#channel]`` and is edited in place from then
# on: every field can be changed without reposting, and the message id lives in
# ``groupbuystats.json`` so a restart does not lose the card.

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands

from config import EMBED_COLOR
from utils.core.prefixes import prefix_for

logger = logging.getLogger("slimey.groupbuystats")

DATA_PATH = Path(__file__).resolve().parent.parent / "groupbuystats.json"

#: Values the card starts with, matching the reference card.
DEFAULT_FIELDS: dict[str, Any] = {
    "name": "Calm Down + OG Files & Session",
    "artist": "Juice WRLD",
    "length": "3:44",
    "price": 20_000,
    "total": 0,
    "info": "2 verse completed JW3. Comes with OG files and the Session of the song.",
    "invite": None,
}

MAX_NAME = 120
MAX_ARTIST = 100
MAX_LENGTH = 32
MAX_INFO = 500
#: Segments in the progress line.
PROGRESS_WIDTH = 12

INTRO = (
    "### Groupbuy Information\n"
    "Below this you find information about the song being purchased. "
    "If you have any questions, ask in general."
)

#: Colours accepted by ``!gbstats color``; ``default`` restores the bot palette.
COLOR_NAMES: dict[str, int] = {
    "red": 0xED4245,
    "crimson": 0xDC143C,
    "orange": 0xE67E22,
    "gold": 0xF1C40F,
    "yellow": 0xFEE75C,
    "lime": 0x2ECC71,
    "green": 0x57F287,
    "teal": 0x1ABC9C,
    "cyan": 0x00D9FF,
    "blue": 0x5865F2,
    "navy": 0x34495E,
    "purple": 0x9B59B6,
    "violet": 0x8E44AD,
    "magenta": 0xD75BF4,
    "pink": 0xEB459E,
    "white": 0xFFFFFF,
    "gray": 0x95A5A6,
    "grey": 0x95A5A6,
    "black": 0x010101,
    "blurple": 0x5865F2,
    "default": EMBED_COLOR,
}


# ============================================================
# HELPERS
# ============================================================

def format_money(amount: Any) -> str:
    """``3500`` -> ``$3,500``; fractional amounts keep two decimals."""
    try:
        value = float(amount)
    except (TypeError, ValueError):
        value = 0.0
    if value.is_integer():
        return f"${int(value):,}"
    return f"${value:,.2f}"


def parse_color(raw: str) -> int | None:
    """Map a colour name (``red``, ``purple`` ...) to a Discord colour int."""
    key = " ".join(raw.split()).lower()
    return COLOR_NAMES.get(key)


#: Anything Discord accepts as an invite link.
INVITE_PATTERN = re.compile(
    r"^(?:https?://)?(?:www\.)?"
    r"(?:discord\.gg|discord(?:app)?\.com/invite)/"
    r"([A-Za-z0-9-]+)\S*$",
    re.IGNORECASE,
)


def parse_invite(raw: str) -> str | None:
    """Read an invite link into ``https://discord.gg/CODE``, or ``None``."""
    match = INVITE_PATTERN.match(raw.strip())
    if match is None:
        return None
    return f"https://discord.gg/{match.group(1)}"


def parse_amount(raw: str) -> float | None:
    """Read ``3,500`` / ``$3500`` / ``20000`` into a number."""
    cleaned = raw.strip().replace("$", "").replace(",", "").replace("_", "")
    if not cleaned:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if value < 0 or value != value or value == float("inf"):
        return None
    return value


def format_percent(total: Any, goal: Any) -> str:
    """``4500`` of ``20000`` -> ``22.5%``; whole values drop the decimal."""
    try:
        raised = max(float(total), 0.0)
        target = float(goal)
    except (TypeError, ValueError):
        raised, target = 0.0, 0.0
    ratio = 0.0 if target <= 0 else raised / target * 100
    text = f"{ratio:.1f}"
    if text.endswith(".0"):
        text = text[:-2]
    return f"{text}%"


def amount_left(total: Any, goal: Any) -> float:
    """How much is still needed to reach the price (never negative)."""
    try:
        return max(float(goal) - float(total), 0.0)
    except (TypeError, ValueError):
        return 0.0


def progress_line(total: Any, goal: Any) -> str:
    """The bar drawn above the ``$raised/$goal`` line."""
    try:
        raised = max(float(total), 0.0)
        target = float(goal)
    except (TypeError, ValueError):
        raised, target = 0.0, 0.0
    ratio = 0.0 if target <= 0 else min(raised / target, 1.0)
    filled = int(round(ratio * PROGRESS_WIDTH))
    return "█" * filled + "░" * (PROGRESS_WIDTH - filled)


async def is_staff(ctx: commands.Context) -> bool:
    """Bot admins, the application team, or anyone with Manage Server."""
    admin_ids = getattr(ctx.bot, "bot_admin_ids", None) or set()
    if ctx.author.id in admin_ids:
        return True
    if await ctx.bot.is_owner(ctx.author):
        return True
    permissions = getattr(ctx.author, "guild_permissions", None)
    if permissions is not None and permissions.manage_guild:
        return True
    raise commands.MissingPermissions(["manage_guild"])


def staff_only():
    """Bot admins, the application team, or anyone with Manage Server."""
    return commands.check(is_staff)


# ============================================================
# CARD
# ============================================================

def card_view(guild: discord.Guild, record: dict) -> discord.ui.LayoutView:
    """Build the Components v2 card; only the field values ever change."""
    price = record.get("price") or 0
    total = record.get("total") or 0
    fields = "\n".join(
        [
            "**Name**",
            record.get("name") or "—",
            "",
            "**Artist**",
            record.get("artist") or "—",
            "",
            "**Length**",
            record.get("length") or "—",
            "",
            "**Price**",
            format_money(price),
            "",
            "**Progress**",
            progress_line(total, price),
            f"{format_money(total)}/{format_money(price)} "
            f"({format_percent(total, price)})",
            "",
            "**Amount Left**",
            format_money(amount_left(total, price)),
            "",
            "**Additional Information**",
            record.get("info") or "—",
        ]
    )

    accent = record.get("color")
    if not isinstance(accent, int) or isinstance(accent, bool):
        accent = EMBED_COLOR

    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_color=accent)
    container.add_item(discord.ui.TextDisplay(INTRO))
    container.add_item(discord.ui.Separator())

    # The thumbnail is always the server icon.
    icon = guild.icon.url if guild.icon else None
    if icon:
        section = discord.ui.Section(accessory=discord.ui.Thumbnail(media=icon))
        section.add_item(discord.ui.TextDisplay(fields))
        container.add_item(section)
    else:
        container.add_item(discord.ui.TextDisplay(fields))

    container.add_item(discord.ui.Separator())

    # The invite section sits at the very bottom, so joining is one click.
    invite = record.get("invite")
    if invite:
        container.add_item(
            discord.ui.Section(
                discord.ui.TextDisplay(
                    "**Join the Groupbuy**\n"
                    "Click the button below to join the groupbuy server."
                ),
                accessory=discord.ui.Button(
                    label="Join Groupbuy",
                    style=discord.ButtonStyle.link,
                    url=invite,
                ),
            )
        )

    view.add_item(container)
    return view


def usage_view(prefix: str) -> discord.ui.LayoutView:
    body = (
        "### Groupbuy Stats\n"
        f"**{prefix}gbstats create [#channel]** — post the card (or move it)\n"
        f"**{prefix}gbstats name <name>** — set the name\n"
        f"**{prefix}gbstats artist <artist>** — set the artist\n"
        f"**{prefix}gbstats length <length>** — set the length (`3:44`)\n"
        f"**{prefix}gbstats price <amount>** — set the price/goal\n"
        f"**{prefix}gbstats total <amount>** — set how much has been raised\n"
        f"**{prefix}total <amount>** — shortcut for the same thing\n"
        f"**{prefix}gbstats color <name>** — set the embed colour\n"
        f"**{prefix}gbstats info <text>** — set the additional information\n"
        f"**{prefix}gbstats invite <link>** — set the join link at the bottom\n"
        f"**{prefix}gbstats share <serverid> <channelid>** — post it in another server\n"
        f"**{prefix}gbstats unshare <serverid>** — stop updating it there\n\n"
        "The card stays in the channel it was created in and is edited in place."
    )
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_color=EMBED_COLOR)
    container.add_item(discord.ui.TextDisplay(body))
    view.add_item(container)
    return view


# ============================================================
# COG
# ============================================================

class GroupbuyStats(commands.Cog, name="GroupbuyStats"):
    """The editable "Groupbuy Information" card."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._data: dict[str, Any] = self._load()

    async def cog_check(self, ctx: commands.Context) -> bool:
        """Subcommands don't inherit the group's checks on their own."""
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return await is_staff(ctx)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            with open(DATA_PATH, encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and isinstance(data.get("guilds"), dict):
                return data
        except FileNotFoundError:
            pass
        except Exception:
            logger.exception("Could not load groupbuy stats")
        return {"guilds": {}}

    def _save(self) -> None:
        temp = DATA_PATH.with_suffix(".tmp")
        try:
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, indent=2)
            os.replace(temp, DATA_PATH)
        except Exception:
            logger.exception("Could not save groupbuy stats")

    def _record(self, guild_id: int) -> dict:
        guilds = self._data.setdefault("guilds", {})
        record = guilds.get(str(guild_id))
        if not isinstance(record, dict):
            record = {}
            guilds[str(guild_id)] = record
        for key, value in DEFAULT_FIELDS.items():
            record.setdefault(key, value)
        record.setdefault("color", EMBED_COLOR)
        record.setdefault("invite", None)
        record.setdefault("channel_id", None)
        record.setdefault("message_id", None)
        if not isinstance(record.get("shares"), list):
            record["shares"] = []
        return record

    # ------------------------------------------------------------------
    # Rendering / editing
    # ------------------------------------------------------------------

    async def _refresh(self, guild: discord.Guild, record: dict) -> bool:
        """Edit the tracked card and every shared copy."""
        updated = await self._refresh_main(guild, record)
        await self._refresh_shares(record)
        return updated

    async def _refresh_main(self, guild: discord.Guild, record: dict) -> bool:
        """Edit the tracked card, or post it again if the message is gone."""
        channel_id = record.get("channel_id")
        if not channel_id:
            return False

        channel = guild.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
            except discord.HTTPException:
                return False

        view = card_view(guild, record)
        message_id = record.get("message_id")
        if message_id:
            try:
                message = await channel.fetch_message(int(message_id))
            except discord.HTTPException:
                message = None
            if message is not None:
                await message.edit(view=view)
                return True

        message = await channel.send(view=view)
        record["message_id"] = message.id
        record["channel_id"] = channel.id
        self._save()
        return True

    async def _refresh_shares(self, record: dict) -> None:
        """Push the card to every server it was shared to."""
        shares = record.get("shares")
        if not isinstance(shares, list) or not shares:
            return

        changed = False
        for share in list(shares):
            if not isinstance(share, dict):
                continue
            try:
                if await self._sync_share(record, share):
                    changed = True
            except discord.Forbidden:
                logger.warning(
                    "Groupbuy share: no permission in %s/%s",
                    share.get("guild_id"),
                    share.get("channel_id"),
                )
            except discord.HTTPException:
                logger.exception(
                    "Groupbuy share: could not update %s/%s",
                    share.get("guild_id"),
                    share.get("channel_id"),
                )
        if changed:
            self._save()

    async def _sync_share(self, record: dict, share: dict) -> bool:
        """Edit one shared copy; ``True`` when its stored message id changed."""
        try:
            guild_id = int(share["guild_id"])
            channel_id = int(share["channel_id"])
        except (KeyError, TypeError, ValueError):
            return False

        target = self.bot.get_guild(guild_id)
        if target is None:
            logger.warning("Groupbuy share: bot is no longer in guild %s", guild_id)
            return False

        channel = target.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                logger.warning("Groupbuy share: channel %s is gone", channel_id)
                return False

        # Each copy uses the icon of the server it was posted in.
        view = card_view(target, record)
        message_id = share.get("message_id")
        if message_id:
            try:
                message = await channel.fetch_message(int(message_id))
            except discord.HTTPException:
                message = None
            if message is not None:
                await message.edit(view=view)
                return False

        message = await channel.send(view=view)
        share["message_id"] = message.id
        return True

    async def _shared_channel(
        self, guild_id: str, channel_id: str
    ) -> tuple[discord.Guild | None, Any, str | None]:
        """Resolve a target server/channel, or explain what is wrong."""
        try:
            target_id = int(guild_id.strip("<>#"))
            channel_id_int = int(channel_id.strip("<>#"))
        except (AttributeError, TypeError, ValueError):
            return None, None, "Those need to be server and channel ids, e.g. `123456789012345678 987654321098765432`."

        target = self.bot.get_guild(target_id)
        if target is None:
            return None, None, f"I'm not in the server `{target_id}`."

        channel = target.get_channel(channel_id_int)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id_int)
            except discord.HTTPException:
                return None, None, f"I can't find channel `{channel_id_int}` in **{target.name}**."

        if not isinstance(channel, discord.abc.Messageable):
            return None, None, f"{channel.mention} can't take messages."

        me = target.me
        if me is not None:
            permissions = channel.permissions_for(me)
            if not (permissions.view_channel and permissions.send_messages):
                return None, None, (
                    f"I need **View Channel** and **Send Messages** in "
                    f"{channel.mention} on **{target.name}**."
                )
        return target, channel, None

    async def _apply(
        self,
        ctx: commands.Context,
        key: str,
        value: Any,
        label: str,
        shown: str,
        *,
        silent: bool = False,
        confirm: str | None = None,
    ) -> None:
        assert ctx.guild is not None
        record = self._record(ctx.guild.id)
        record[key] = value
        if not record.get("channel_id"):
            record["channel_id"] = ctx.channel.id
        self._save()

        if silent:
            # The card updating is the confirmation, so drop the invocation
            # right away instead of replying.
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass

        try:
            updated = await self._refresh(ctx.guild, record)
        except discord.Forbidden:
            await ctx.reply(
                "I can't edit the card there — I'm missing **View Channel**, "
                "**Send Messages** or **Embed Links** in that channel.",
                mention_author=False,
            )
            return
        except discord.HTTPException:
            logger.exception("Could not update the groupbuy card")
            await ctx.reply(
                "Something went wrong while updating the card.",
                mention_author=False,
            )
            return

        if updated:
            if silent:
                return
            await ctx.reply(
                confirm or f"✅ Set **{label}** to `{shown}`.",
                mention_author=False,
            )
        else:
            await ctx.reply(
                "The card's channel is gone — run "
                f"`{await prefix_for(ctx)}gbstats create` to post it again.",
                mention_author=False,
            )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @commands.group(name="gbstats", invoke_without_command=True)
    @commands.guild_only()
    @staff_only()
    async def gbstats(self, ctx: commands.Context) -> None:
        """Manage this server's groupbuy card."""
        await ctx.reply(view=usage_view(await prefix_for(ctx)), mention_author=False)

    @gbstats.command(name="create", aliases=["spawn"])
    async def create(
        self, ctx: commands.Context, channel: discord.TextChannel = None
    ) -> None:
        """Post the card here, or move it to the given channel."""
        assert ctx.guild is not None
        target = channel or ctx.channel
        me = ctx.guild.me
        permissions = target.permissions_for(me)
        if not (permissions.view_channel and permissions.send_messages):
            await ctx.reply(
                f"I need **View Channel** and **Send Messages** in {target.mention}.",
                mention_author=False,
            )
            return

        record = self._record(ctx.guild.id)
        old_channel_id = record.get("channel_id")
        old_message_id = record.get("message_id")
        moved = old_channel_id is not None and int(old_channel_id) != target.id

        record["channel_id"] = target.id
        if moved:
            record["message_id"] = None
        self._save()

        try:
            await self._refresh(ctx.guild, record)
        except discord.Forbidden:
            record["channel_id"] = old_channel_id
            record["message_id"] = old_message_id
            self._save()
            await ctx.reply(
                f"I can't post in {target.mention} — check my permissions there.",
                mention_author=False,
            )
            return
        except discord.HTTPException:
            logger.exception("Could not create the groupbuy card")
            await ctx.reply(
                "Something went wrong while posting the card.",
                mention_author=False,
            )
            return

        if moved and old_channel_id and old_message_id:
            old_channel = ctx.guild.get_channel(int(old_channel_id))
            if old_channel is not None:
                try:
                    old_message = await old_channel.fetch_message(int(old_message_id))
                    await old_message.delete()
                except discord.HTTPException:
                    pass

        await ctx.reply(
            f"✅ Groupbuy card is live in {target.mention}.",
            mention_author=False,
        )

    @gbstats.command(name="share")
    async def share(
        self, ctx: commands.Context, guild_id: str, channel_id: str
    ) -> None:
        """Post this card in another server and keep it in sync."""
        assert ctx.guild is not None
        target, channel, error = await self._shared_channel(guild_id, channel_id)
        if error is not None:
            await ctx.reply(error, mention_author=False)
            return

        record = self._record(ctx.guild.id)
        if not record.get("channel_id"):
            record["channel_id"] = ctx.channel.id

        shares = record.setdefault("shares", [])
        already = any(
            isinstance(entry, dict)
            and entry.get("guild_id") == target.id
            and entry.get("channel_id") == channel.id
            for entry in shares
        )
        if not already:
            shares.append(
                {"guild_id": target.id, "channel_id": channel.id, "message_id": None}
            )
        self._save()

        try:
            await self._refresh(ctx.guild, record)
        except discord.Forbidden:
            await ctx.reply(
                f"I couldn't post the card in {channel.mention} — check my permissions.",
                mention_author=False,
            )
            return
        except discord.HTTPException:
            logger.exception("Could not share the groupbuy card")
            await ctx.reply(
                "Something went wrong while sharing the card.", mention_author=False
            )
            return

        action = "refreshed" if already else "shared"
        await ctx.reply(
            f"✅ Card {action} in {channel.mention} on **{target.name}** — "
            "it updates there whenever it changes here.",
            mention_author=False,
        )

    @gbstats.command(name="unshare", aliases=["removeshare"])
    async def unshare(
        self, ctx: commands.Context, guild_id: str, channel_id: str = None
    ) -> None:
        """Stop updating the shared card in a server (or one channel)."""
        assert ctx.guild is not None
        try:
            target_id = int(guild_id.strip("<>#"))
            channel_id_int = (
                int(channel_id.strip("<>#")) if channel_id is not None else None
            )
        except (AttributeError, TypeError, ValueError):
            await ctx.reply(
                "Give me the server id (and optionally the channel id), "
                "e.g. `123456789012345678 987654321098765432`.",
                mention_author=False,
            )
            return

        record = self._record(ctx.guild.id)
        shares = record.get("shares") or []
        removed = [
            entry
            for entry in shares
            if isinstance(entry, dict)
            and entry.get("guild_id") == target_id
            and (channel_id_int is None or entry.get("channel_id") == channel_id_int)
        ]
        if not removed:
            await ctx.reply(
                "That server isn't on the share list.", mention_author=False
            )
            return

        record["shares"] = [entry for entry in shares if entry not in removed]
        self._save()

        # Remove the copies too, so they don't linger with stale values.
        for entry in removed:
            await self._delete_share(entry)

        places = ", ".join(f"`{entry.get('channel_id')}`" for entry in removed)
        await ctx.reply(
            f"✅ Stopped sharing to {places}. The messages there were removed.",
            mention_author=False,
        )

    async def _delete_share(self, share: dict) -> None:
        try:
            guild = self.bot.get_guild(int(share["guild_id"]))
            channel = guild.get_channel(int(share["channel_id"])) if guild else None
            message_id = share.get("message_id")
            if channel is not None and message_id:
                message = await channel.fetch_message(int(message_id))
                await message.delete()
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
        except discord.HTTPException:
            pass

    @gbstats.command(name="name")
    async def name(self, ctx: commands.Context, *, value: str) -> None:
        """Set the name field."""
        text = " ".join(value.split())
        if not text:
            await ctx.reply("Give me a name to use.", mention_author=False)
            return
        await self._apply(ctx, "name", text[:MAX_NAME], "Name", text[:MAX_NAME])

    @gbstats.command(name="artist")
    async def artist(self, ctx: commands.Context, *, value: str) -> None:
        """Set the artist field."""
        text = " ".join(value.split())
        if not text:
            await ctx.reply("Give me an artist to use.", mention_author=False)
            return
        await self._apply(ctx, "artist", text[:MAX_ARTIST], "Artist", text[:MAX_ARTIST])

    @gbstats.command(name="length")
    async def length(self, ctx: commands.Context, *, value: str) -> None:
        """Set the length field."""
        text = " ".join(value.split())
        if not text:
            await ctx.reply("Give me a length to use, e.g. `3:44`.", mention_author=False)
            return
        await self._apply(ctx, "length", text[:MAX_LENGTH], "Length", text[:MAX_LENGTH])

    @gbstats.command(name="price")
    async def price(self, ctx: commands.Context, amount: str) -> None:
        """Set the price, which also becomes the progress goal."""
        value = parse_amount(amount)
        if value is None:
            prefix = await prefix_for(ctx)
            await ctx.reply(
                f"Give me a number, e.g. `{prefix}gbstats price 20000`.",
                mention_author=False,
            )
            return
        await self._apply(ctx, "price", value, "Price", format_money(value))

    async def _set_total(
        self, ctx: commands.Context, amount: str, path: str, *, silent: bool = False
    ) -> None:
        value = parse_amount(amount)
        if value is None:
            prefix = await prefix_for(ctx)
            await ctx.reply(
                f"Give me a number, e.g. `{prefix}{path} 3500`.",
                mention_author=False,
            )
            return
        await self._apply(
            ctx, "total", value, "Progress", format_money(value), silent=silent
        )

    @gbstats.command(name="total")
    async def total(self, ctx: commands.Context, amount: str) -> None:
        """Set how much of the price has been raised so far."""
        await self._set_total(ctx, amount, "gbstats total")

    @commands.command(name="total")
    @commands.guild_only()
    @staff_only()
    async def total_shortcut(self, ctx: commands.Context, amount: str) -> None:
        """Set how much of the groupbuy has been raised so far."""
        await self._set_total(ctx, amount, "total", silent=True)

    @gbstats.command(name="color", aliases=["colour"])
    async def color(self, ctx: commands.Context, *, value: str) -> None:
        """Set the card colour by name (red, blue, purple ...)."""
        chosen = parse_color(value)
        if chosen is None:
            names = ", ".join(sorted(COLOR_NAMES))
            await ctx.reply(
                f"Pick a colour name: {names}.",
                mention_author=False,
            )
            return
        await self._apply(
            ctx, "color", chosen, "Color", " ".join(value.split()).title()
        )

    @gbstats.command(name="invite", aliases=["link", "server"])
    async def invite(self, ctx: commands.Context, *, value: str) -> None:
        """Set the invite link shown at the bottom of the card."""
        cleaned = value.strip()
        if cleaned.lower() in {"clear", "remove", "off", "none"}:
            await self._apply(
                ctx,
                "invite",
                None,
                "Invite",
                "Removed",
                confirm="✅ Invite removed from the card.",
            )
            return

        link = parse_invite(cleaned)
        if link is None:
            prefix = await prefix_for(ctx)
            await ctx.reply(
                "Give me a Discord invite, e.g. "
                f"`{prefix}gbstats invite https://discord.gg/abc123` "
                f"(or `{prefix}gbstats invite clear` to remove it).",
                mention_author=False,
            )
            return
        await self._apply(ctx, "invite", link, "Invite", link)

    @gbstats.command(name="info", aliases=["additional"])
    async def info(self, ctx: commands.Context, *, value: str) -> None:
        """Set the additional information field."""
        text = value.strip()
        if not text:
            await ctx.reply("Give me some text to use.", mention_author=False)
            return
        await self._apply(
            ctx,
            "info",
            text[:MAX_INFO],
            "Additional Information",
            text[:MAX_INFO],
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GroupbuyStats(bot))
