from enum import member
import os
import re
import io
import json
import base64
import string
import discord
import aiohttp
import secrets
import asyncio
from discord import member
import logging
import humanize
import platform
import humanfriendly
from decimal import Decimal
from sqlalchemy import text
from discord import ui, Interaction, SelectOption, ButtonStyle, app_commands
from discord.ext import commands
from utils.core.prefixes import render
from utils.core.http_session import borrowed
from utils.misc import MiscUtils
from utils.cooldown import unified_cooldown
from utils.core import mp3_buttons
from utils.embeds import Embeds
from utils.guardrails import check_slash_guardrails
from typing import List, Optional
from discord.ext.commands import Context
from datetime import datetime, timedelta, timezone
from PIL import ImageFont, Image, ImageDraw, ImageFilter
import random

logger = logging.getLogger("discord.client")

COINMARKETCAP_API_KEY = os.getenv("COINMARKETCAP_API_KEY")
COINMARKETCAP_API_URL = (
    "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
)

NASA_API_KEY = os.getenv("NASA_API_KEY")

HIDDEN_COGS = {"Owner", "Jishaku"}
COMMANDS_PER_PAGE = 15
#: Groups that publish their own paginated subcommand index (`,lf`) instead of
#: having every child dumped into the help embeds. Listing them there drowns
#: the section and risks overshooting the embed limits.
HELP_SUBCOMMAND_EXCLUDE = {"lf"}
#: Discord renders at most 25 options in one select, so the category menu is
#: paginated once the bot has more cogs than that.
COG_SELECT_LIMIT = 25

#: Help categories, in menu order. Related cogs are folded into one section
#: (both fun cogs, every music/media cog, and so on) so the menu reads by
#: feature rather than one entry per cog.
CATEGORY_ORDER: tuple[str, ...] = (
    "🎵 Music & Media",
    "🎧 Last.fm",
    "🎮 Games & Casino",
    "🕹️ Chat Games",
    "🎉 Fun",
    "💰 Economy",
    "🎖️ Levels",
    "👥 Community",
    "🛡️ Moderation",
    "🔊 Voice",
    "ℹ️ General",
)

#: Which category each cog is filed under.
CATEGORY_FOR_COG: dict[str, str] = {
    # Music & media
    "Music": "🎵 Music & Media",
    "Mixtapes": "🎵 Music & Media",
    "Radio": "🎵 Music & Media",
    "LastFM": "🎧 Last.fm",
    "SongInfo": "🎵 Music & Media",
    "SongOfTheDay": "🎵 Music & Media",
    "Covers": "🎵 Music & Media",
    "Remaster": "🎵 Music & Media",
    "Stem": "🎵 Music & Media",
    "Edits": "🎵 Music & Media",
    "Snippets": "🎵 Music & Media",
    "MakeSnip": "🎵 Music & Media",
    "Repost": "🎵 Music & Media",
    "Unleaked": "🎵 Music & Media",
    "BruxVault": "🎵 Music & Media",
    # Games & casino
    "Casino": "🎮 Games & Casino",
    "Games": "🎮 Games & Casino",
    # Chat games
    "ChatGames": "🕹️ Chat Games",
    # Fun
    "Fun": "🎉 Fun",
    "FunExtra": "🎉 Fun",
    "Misc": "🎉 Fun",
    "TTS": "🎉 Fun",
    # Economy
    "Economy": "💰 Economy",
    "Vault": "💰 Economy",
    # Levels
    "Leveling": "🎖️ Levels",
    "Admin": "🎖️ Levels",
    # Community
    "Community": "👥 Community",
    "Roles": "👥 Community",
    "BoosterRole": "👥 Community",
    # Moderation
    "Moderation": "🛡️ Moderation",
    "Watchdog": "🛡️ Moderation",
    "ServerLogs": "🛡️ Moderation",
    # Voice
    "Voicechat": "🔊 Voice",
    # General
    "General": "ℹ️ General",
    "AI": "ℹ️ General",
    "ImageGen": "ℹ️ General",
    "Advertise": "ℹ️ General",
}

CATEGORY_DESCRIPTIONS: dict[str, str] = {
    "🎵 Music & Media": "Music, the mixtape library, radio, covers, remasters, stems and snippets.",
    "🎧 Last.fm": "Listening stats, now playing, charts, crowns, who-knows and friends, in .fmbot's layout.",
    "🎮 Games & Casino": "Casino tables and the mini-game collection.",
    "🕹️ Chat Games": "Channel-wide games the whole server plays together in chat.",
    "🎉 Fun": "Fun, reaction counters and the meme TTS voices.",
    "💰 Economy": "Your wallet, bank, jobs, loans and the economy dashboard.",
    "🎖️ Levels": "Levels, XP, level roles and the level admin menu.",
    "👥 Community": "Roles, autoroles, boosters and community settings.",
    "🛡️ Moderation": "Moderation, the watchdog and server logging.",
    "🔊 Voice": "Private voice channels and voice controls.",
    "ℹ️ General": "Info, utility and AI commands.",
}

# Raw mention tokens, so quoted text can show `@name` instead of `<@123>`.
MENTION_PATTERN = re.compile(r"<@!?(\d+)>|<@&(\d+)>|<#(\d+)>")

MESSAGE_LINK = re.compile(
    r"https?://(?:canary\.)?discord(?:app)?\.com/channels/"
    r"(?P<guild>\d+)/(?P<channel>\d+)/(?P<message>\d+)"
)

class HelpSelect(ui.Select):
    """One page of command categories (Discord caps a select at 25 options)."""

    def __init__(self, bot, embeds_by_cog, cogs, page: int = 0):
        self.bot = bot
        self.embeds_by_cog = embeds_by_cog
        start = page * COG_SELECT_LIMIT
        page_cogs = cogs[start : start + COG_SELECT_LIMIT]
        options = [
            SelectOption(label=cog, description=f"View commands for {cog}")
            for cog in page_cogs
        ]
        super().__init__(
            placeholder="Select a command category..",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: Interaction):
        cog = self.values[0]
        view = HelpPaginationView(self.embeds_by_cog[cog], self.bot, self.embeds_by_cog)
        view.message = await interaction.response.edit_message(
            embed=self.embeds_by_cog[cog][0], view=view
        )


class CategoryMenuView(ui.View):
    """The category picker, paging through cogs 25 at a time."""

    def __init__(self, bot, embeds_by_cog):
        super().__init__(timeout=120)
        self.bot = bot
        self.embeds_by_cog = embeds_by_cog
        self.cogs = list(embeds_by_cog)
        self.page = 0

        self.prev_button = ui.Button(emoji="⬅", style=ButtonStyle.gray, row=1)
        self.next_button = ui.Button(emoji="➡", style=ButtonStyle.gray, row=1)
        self.prev_button.callback = self.go_back
        self.next_button.callback = self.go_forward
        self.add_item(self.prev_button)
        self.add_item(self.next_button)
        self._install_select()

    @property
    def pages(self) -> int:
        return max(1, (len(self.cogs) + COG_SELECT_LIMIT - 1) // COG_SELECT_LIMIT)

    def _install_select(self) -> None:
        for child in list(self.children):
            if isinstance(child, ui.Select):
                self.remove_item(child)
        self.add_item(HelpSelect(self.bot, self.embeds_by_cog, self.cogs, self.page))

        show_nav = len(self.cogs) > COG_SELECT_LIMIT
        self.prev_button.disabled = (not show_nav) or self.page <= 0
        self.next_button.disabled = (not show_nav) or self.page >= self.pages - 1

    async def go_back(self, interaction: Interaction) -> None:
        self.page = max(0, self.page - 1)
        self._install_select()
        await interaction.response.edit_message(view=self)

    async def go_forward(self, interaction: Interaction) -> None:
        self.page = min(self.pages - 1, self.page + 1)
        self._install_select()
        await interaction.response.edit_message(view=self)

class HelpPaginationView(ui.View):
    def __init__(self, embeds, bot, embeds_by_cog):
        super().__init__(timeout=120)
        self.embeds = embeds
        self.bot = bot
        self.embeds_by_cog = embeds_by_cog
        self.current = 0

        self.prev_button = ui.Button(emoji="⬅", style=ButtonStyle.gray)
        self.next_button = ui.Button(emoji="➡", style=ButtonStyle.gray)
        self.back_button = ui.Button(label="Back", style=ButtonStyle.red)

        self.prev_button.callback = self.go_back
        self.next_button.callback = self.go_forward
        self.back_button.callback = self.go_back_to_menu

        if len(embeds) > 1:
            self.add_item(self.prev_button)
            self.add_item(self.next_button)

        self.add_item(self.back_button)
        self.update_buttons()

    def update_buttons(self):
        if hasattr(self, "prev_button"):
            self.prev_button.disabled = self.current == 0
        if hasattr(self, "next_button"):
            self.next_button.disabled = self.current == len(self.embeds) - 1

    async def go_back(self, interaction: Interaction):
        self.current -= 1
        self.update_buttons()
        await interaction.response.edit_message(
            embed=self.embeds[self.current], view=self
        )

    async def go_forward(self, interaction: Interaction):
        self.current += 1
        self.update_buttons()
        await interaction.response.edit_message(
            embed=self.embeds[self.current], view=self
        )

    async def go_back_to_menu(self, interaction: Interaction):
        view = CategoryMenuView(self.bot, self.embeds_by_cog)
        await interaction.response.edit_message(
            content="Please select a command category:", embed=None, view=view
        )


class NamesPaginationView(ui.View):
    """
    Paginated view for the 'names' command.
    Accepts a list of name-history entries (expected dicts) and allows simple
    previous/next navigation. Optionally restricts interaction to the command
    invoker by passing author_id (keeps backward compatibility).
    """

    def __init__(self, names: List[dict], per_page: int = 10, author_id: int = None):
        super().__init__(timeout=120)
        self.names = names or []
        self.per_page = max(1, per_page)
        self.current = 0
        self.author_id = author_id

        # Buttons
        self.first_button = ui.Button(emoji="⏮️", style=ButtonStyle.gray)
        self.prev_button = ui.Button(emoji="⬅️", style=ButtonStyle.gray)
        self.next_button = ui.Button(emoji="➡️", style=ButtonStyle.gray)
        self.last_button = ui.Button(emoji="⏭️", style=ButtonStyle.gray)

        # attach callbacks
        self.first_button.callback = self.go_first
        self.prev_button.callback = self.go_back
        self.next_button.callback = self.go_forward
        self.last_button.callback = self.go_last

        # only add navigation if multiple pages
        if len(self.names) > self.per_page:
            self.add_item(self.first_button)
            self.add_item(self.prev_button)
            self.add_item(self.next_button)
            self.add_item(self.last_button)

        self.update_buttons()

    async def interaction_check(self, interaction: Interaction) -> bool:
        # If an author_id was provided, restrict interactions to that user.
        if self.author_id and interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the command invoker may interact with this menu.", ephemeral=True
            )
            return False
        return True

    def update_buttons(self):
        total = len(self.names)
        pages = max(1, (total - 1) // self.per_page + 1)
        # disable/enable buttons based on current page
        at_first = self.current == 0
        at_last = (self.current + 1) * self.per_page >= total

        if hasattr(self, "first_button"):
            self.first_button.disabled = at_first
        if hasattr(self, "prev_button"):
            self.prev_button.disabled = at_first
        if hasattr(self, "next_button"):
            self.next_button.disabled = at_last
        if hasattr(self, "last_button"):
            self.last_button.disabled = at_last

    def get_current_embed(self) -> discord.Embed:
        total = len(self.names)
        pages = max(1, (total - 1) // self.per_page + 1)
        embed = discord.Embed(title="Names History", color=discord.Color.blue())
        embed.set_footer(text=f"Page {self.current + 1} of {pages} • {total} total")

        start = self.current * self.per_page
        end = start + self.per_page
        page_items = self.names[start:end]

        if not page_items:
            embed.description = "No entries on this page."
            return embed

        now = discord.utils.utcnow()
        for idx, item in enumerate(page_items, start=start + 1):
            # Expected dict structure:
            # {"old_name": ..., "new_name": ..., "change_type": ..., "timestamp": ...}
            try:
                if isinstance(item, dict):
                    old = item.get("old_name", "N/A")
                    new = item.get("new_name", "N/A")
                    ctype = str(item.get("change_type", "change")).title()

                    ts = item.get("timestamp")
                    ts_obj = None
                    if isinstance(ts, datetime):
                        ts_obj = ts
                    elif isinstance(ts, str):
                        try:
                            ts_obj = datetime.fromisoformat(ts)
                        except Exception:
                            try:
                                ts_obj = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                            except Exception:
                                ts_obj = None
                    elif isinstance(ts, (int, float)):
                        try:
                            ts_obj = datetime.fromtimestamp(float(ts))
                        except Exception:
                            ts_obj = None

                    if ts_obj:
                        abs_ts = ts_obj.strftime("%Y-%m-%d %H:%M:%S")
                        rel_ts = humanize.naturaltime(now - ts_obj)
                        ts_display = f"{abs_ts} ({rel_ts})"
                    else:
                        ts_display = "Unknown time"

                    field_name = f"{idx}. {ctype}"
                    field_value = f"From: `{old}`\nTo: `{new}`\nTime: {ts_display}"
                else:
                    field_name = f"{idx}. Entry"
                    field_value = str(item)
            except Exception:
                field_name = f"{idx}. Entry"
                field_value = str(item)

            embed.add_field(name=field_name, value=field_value, inline=False)

        return embed

    async def go_first(self, interaction: Interaction):
        if self.current != 0:
            self.current = 0
            self.update_buttons()
            await interaction.response.edit_message(
                embed=self.get_current_embed(), view=self
            )
        else:
            await interaction.response.defer()

    async def go_back(self, interaction: Interaction):
        if self.current > 0:
            self.current -= 1
            self.update_buttons()
            await interaction.response.edit_message(
                embed=self.get_current_embed(), view=self
            )
        else:
            await interaction.response.defer()

    async def go_forward(self, interaction: Interaction):
        if (self.current + 1) * self.per_page < len(self.names):
            self.current += 1
            self.update_buttons()
            await interaction.response.edit_message(
                embed=self.get_current_embed(), view=self
            )
        else:
            await interaction.response.defer()

    async def go_last(self, interaction: Interaction):
        total = len(self.names)
        last_page = max(0, (total - 1) // self.per_page)
        if self.current != last_page:
            self.current = last_page
            self.update_buttons()
            await interaction.response.edit_message(
                embed=self.get_current_embed(), view=self
            )
        else:
            await interaction.response.defer()


class General(commands.Cog, name="General"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.utils = MiscUtils(self)
        self.start_time = discord.utils.utcnow()
        self.session = aiohttp.ClientSession()
        self.hidden_cogs: List[str] = ["Owner"]
        self.per_page = 1
        self.afk_users = {}
        self.currency_api = "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies"
        self.start_time = discord.utils.utcnow()

        try:
            font_path = "DejaVuSans.ttf"
            self.font_quote = ImageFont.truetype(font_path, 32)
            self.font_author = ImageFont.truetype(font_path, 18)
            self.font_handle = ImageFont.truetype(font_path, 14)
            self.font_date = ImageFont.truetype(font_path, 14)
            self.font_stamp = ImageFont.truetype(font_path, 14)
            self.font_path = font_path
            self.initial_quote_size = 32
            self.min_quote_size = 18
        except OSError:
            default_font = ImageFont.load_default()
            self.font_quote = default_font
            self.font_author = default_font
            self.font_handle = default_font
            self.font_date = default_font
            self.font_stamp = default_font
            self.font_path = None
            self.initial_quote_size = default_font.size
            self.min_quote_size = default_font.size
        self.RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Delegate slash guardrails to the bot's centralized check."""
        return await check_slash_guardrails(self, interaction)

    def generate_password(self, length=16, use_digits=True, use_punctuation=True):
        if length < 4:
            raise ValueError(
                "Password length must be at least 4 characters to ensure security."
            )
        characters = string.ascii_letters
        if use_digits:
            characters += string.digits
        if use_punctuation:
            characters += string.punctuation
        password = []
        password.append(secrets.choice(string.ascii_letters))
        if use_digits:
            password.append(secrets.choice(string.digits))
        if use_punctuation:
            password.append(secrets.choice(string.punctuation))
        password.extend(
            secrets.choice(characters) for _ in range(length - len(password))
        )
        secrets.SystemRandom().shuffle(password)
        return "".join(password)

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info(f"Cog {self.__class__.__name__} is ready!")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """
        Remove AFK status on any message and notify when an AFK user is mentioned.
        This listener intentionally does NOT read message.content; it only needs
        the on_message event and message.mentions, so it does not rely on the
        Message Content privileged intent.
        """
        if message.author.bot or not message.guild:
            return
        user_id = message.author.id
        if user_id in self.afk_users:
            afk_data = self.afk_users.pop(user_id)
            afk_duration = discord.utils.utcnow() - afk_data["start_time"]
            embed = discord.Embed(
                description=f"Welcome back {message.author.mention}! You were AFK for **{humanfriendly.format_timespan(afk_duration, False)}**."
            )
            await message.channel.send(embed=embed)
        for mentioned in message.mentions:
            if mentioned.id in self.afk_users:
                afk_data = self.afk_users[mentioned.id]
                afk_duration = discord.utils.utcnow() - afk_data["start_time"]
                embed = discord.Embed(
                    description=f"{mentioned.display_name} is currently AFK\n\nReason: **{afk_data['reason']}**\n\nDuration: **{humanfriendly.format_timespan(afk_duration, False)}**."
                )
                await message.channel.send(embed=embed)

    @commands.command(
        name="terms", description="View the terms of service."
    )
    async def terms_of_service(self, ctx: commands.Context) -> None:
        url = "https://share.mistrust.dev/s/terms"  # Replace with actual terms URL
        markdown = f"Click [**here**]({url}) to view the terms"
        await ctx.reply(markdown, mention_author=False)

    @commands.command(
        name="privacy", description="View the privacy policy."
    )
    async def privacy_policy(self, ctx: commands.Context) -> None:
        url = "https://share.mistrust.dev/s/privacy"  # Replace with actual privacy policy URL
        markdown = f"Click [**here**]({url}) to view the privacy policy"
        await ctx.reply(markdown, mention_author=False)

    @commands.hybrid_command(
        name="help", description="Displays a list of available commands."
    )
    @unified_cooldown(5)
    async def help(
        self, ctx: commands.Context, query: Optional[str] = None
    ):
        prefix = ctx.prefix or "/"
        try:
            color = (
                ctx.author.top_role.color
                if hasattr(ctx.author, "top_role")
                else discord.Color.blurple()
            )
        except TypeError:
            color = discord.Color.blurple()

        def create_embed(title: str, description: str = None) -> discord.Embed:
            emb = discord.Embed(
                title=title,
                description=description,
                color=color,
                timestamp=discord.utils.utcnow(),
            )
            avatar = self.bot.user.display_avatar.url
            if avatar:
                emb.set_thumbnail(url=avatar)
            return emb

        # If a specific command was requested, try to find and display detailed help.
        if query:
            q = query.strip().lower()

            # Try direct lookup first (supports "group subcommand" style)
            cmd = self.bot.get_command(q)

            # Fallback: search by name, qualified_name or aliases
            if not cmd:
                for c in self.bot.commands:
                    if c.hidden:
                        continue
                    if (
                        c.name.lower() == q
                        or c.qualified_name.lower() == q
                        or q in [a.lower() for a in getattr(c, "aliases", [])]
                    ):
                        cmd = c
                        break

            if not cmd:
                await ctx.reply(f"No command found matching `{query}`.", delete_after=8)
                return

            # Do not show hidden commands
            if getattr(cmd, "hidden", False):
                await ctx.reply("No command found.", delete_after=8)
                return

            embed = create_embed(
                f"{cmd.qualified_name}",
                render(cmd.help, prefix) or "No description available.",
            )
            usage = f"{prefix}{cmd.qualified_name}"
            if getattr(cmd, "signature", None):
                sig = str(cmd.signature).strip()
                if sig:
                    usage = f"{usage} {sig}"
            embed.add_field(name="Usage", value=f"`{usage}`", inline=False)

            if getattr(cmd, "aliases", None):
                aliases = (
                    ", ".join(f"`{a}`" for a in cmd.aliases) if cmd.aliases else "None"
                )
                embed.add_field(name="Aliases", value=aliases, inline=True)

            cog_name = cmd.cog_name or "No Cog"
            embed.add_field(name="Category", value=cog_name, inline=True)

            if isinstance(cmd, commands.Group) and getattr(cmd, "commands", None):
                if cmd.name.lower() in HELP_SUBCOMMAND_EXCLUDE:
                    embed.add_field(
                        name="Subcommands",
                        value=(
                            f"Run `{prefix}{cmd.qualified_name}` to browse them "
                            "page by page."
                        ),
                        inline=False,
                    )
                else:
                    sub_list = [
                        f"`{prefix}{cmd.qualified_name} {sub.name} {sub.signature}`".strip()
                        for sub in cmd.commands
                        if not sub.hidden
                    ]
                    if sub_list:
                        embed.add_field(
                            name="Subcommands",
                            value="\n".join(sub_list[:20]) or "None",
                            inline=False,
                        )

            await ctx.reply(embed=embed)
            return

        # No specific command requested — show interactive category menu.
        # Every cog is folded into a feature category, so related commands sit
        # together instead of one section per cog.
        # A cog can attach commands to the bot after it has loaded - `cogs.lastfm`
        # adds its Last.fm commands once the `lf` group exists - and those never
        # appear in `cog.get_commands()`. Collect what the bot actually holds for
        # each cog so a category can't go missing commands it really owns.
        owned: dict[str, list] = {}
        for command in self.bot.commands:
            if command.cog is not None:
                owned.setdefault(command.cog.qualified_name, []).append(command)

        grouped: dict[str, list] = {}
        for cog_name, cog in sorted(self.bot.cogs.items()):
            if cog_name in HIDDEN_COGS:
                continue
            cmds = []
            seen: set[int] = set()
            for command in (*cog.get_commands(), *owned.get(cog_name, ())):
                if command.hidden or id(command) in seen:
                    continue
                seen.add(id(command))
                cmds.append(command)
            if not cmds:
                continue
            category = CATEGORY_FOR_COG.get(cog_name, "📁 Other")
            grouped.setdefault(category, []).extend(cmds)

        ordered = [name for name in CATEGORY_ORDER if name in grouped]
        ordered += sorted(name for name in grouped if name not in CATEGORY_ORDER)

        embeds_by_cog = {}
        for category in ordered:
            cmds = sorted(grouped[category], key=lambda c: c.qualified_name.lower())

            pages = []
            for i in range(0, len(cmds), COMMANDS_PER_PAGE):
                emb = create_embed(
                    category,
                    CATEGORY_DESCRIPTIONS.get(category, "No description available"),
                )
                for cmd in cmds[i : i + COMMANDS_PER_PAGE]:
                    desc = render(cmd.help, prefix) or "No description available"
                    emb.add_field(
                        name=f"`{prefix}{cmd.qualified_name}{(' '+cmd.signature) if cmd.signature else ''}`",
                        value=f"↳ {desc}",
                        inline=False,
                    )

                    if (
                        isinstance(cmd, commands.Group)
                        and cmd.name.lower() not in HELP_SUBCOMMAND_EXCLUDE
                    ):
                        subs = [
                            f"{prefix}{cmd.name} {sub.name}"
                            for sub in cmd.commands
                            if not sub.hidden
                        ]
                        if subs:
                            emb.add_field(
                                name="Subcommands",
                                value="\n".join(f"↳ {s}" for s in subs),
                                inline=False,
                            )

                pages.append(emb)

            embeds_by_cog[category] = pages

        if not embeds_by_cog:
            return await ctx.reply("No visible commands available.")

        menu = CategoryMenuView(self.bot, embeds_by_cog)
        await ctx.reply("Please select a command category:", view=menu)

    @commands.command(
        name="membercount", description="Shows detailed member statistics."
    )
    async def member_count(self, ctx: commands.Context) -> None:
        """Shows detailed member statistics including online status and role distributions."""

        total_members = ctx.guild.member_count
        humans = sum(not member.bot for member in ctx.guild.members)
        bots = sum(member.bot for member in ctx.guild.members)

        online = sum(
            member.status == discord.Status.online for member in ctx.guild.members
        )
        idle = sum(member.status == discord.Status.idle for member in ctx.guild.members)
        dnd = sum(member.status == discord.Status.dnd for member in ctx.guild.members)
        offline = sum(
            member.status == discord.Status.offline for member in ctx.guild.members
        )

        human_percent = (humans / total_members) * 100
        bot_percent = (bots / total_members) * 100

        milestones = [
            100,
            250,
            500,
            750,
            1000,
            1500,
            2000,
            2500,
            3000,
            3500,
            4000,
            4500,
            5000,
            6000,
            7000,
            8000,
            9000,
            10000,
            15000,
            20000,
        ]
        next_milestone = next((m for m in milestones if m > total_members), None)

        embed = discord.Embed(
            title=f"📊 Member Statistics for {ctx.guild.name}",
            color=ctx.guild.me.color
            if ctx.guild.me.color != discord.Color.default()
            else discord.Color.blurple(),
        )

        embed.add_field(
            name="Total Members",
            value=f"**{total_members:,}** members total",
            inline=False,
        )

        embed.add_field(
            name="Member Distribution",
            value=f"👤 Humans: **{humans:,}** ({human_percent:.1f}%)\n🤖 Bots: **{bots:,}** ({bot_percent:.1f}%)",
            inline=True,
        )

        embed.add_field(
            name="Status Distribution",
            value=f"<:status_online:1336131887604826122> Online: **{online:,}**\n"
            f"<:status_idle:1336131912602882121> Idle: **{idle:,}**\n"
            f"<:status_dnd:1336131924556775437> DND: **{dnd:,}**\n"
            f"<:status_offline:1336131901764665384> Offline: **{offline:,}**",
            inline=True,
        )

        if next_milestone:
            members_needed = next_milestone - total_members
            progress = (total_members / next_milestone) * 100
            embed.add_field(
                name="Next Milestone",
                value=f"🎯 **{next_milestone:,}** members\n"
                f"Need **{members_needed:,}** more members\n"
                f"Progress: **{progress:.1f}%**",
                inline=False,
            )

        embed.set_footer(text=f"Last Updated")
        embed.timestamp = discord.utils.utcnow()

        await ctx.reply(embed=embed)

    @commands.hybrid_command(name="afk", description="Set your AFK status.")
    @unified_cooldown(5)
    async def afk(
        self,
        ctx: commands.Context,
        *,
        reason: Optional[str] = "No reason provided",
    ) -> None:
        """
        Set your AFK status with an optional reason.
        The status will be removed when you send a message.
        """
        if len(reason) > 100:
            await ctx.reply(
                "🚫 AFK reason must be 100 characters or less.", delete_after=5
            )
            return

        user_id = ctx.author.id

        if user_id in self.afk_users:
            previous_reason = self.afk_users[user_id]["reason"]
            previous_time = self.afk_users[user_id]["start_time"]
            duration = discord.utils.utcnow() - previous_time

            await Embeds.warning(ctx, f"You are already AFK:\n**Reason:** {previous_reason}\n**Duration:** {humanfriendly.format_timespan(duration, False)}", delete_after=5, reply=True)
            return

        try:
            self.afk_users[user_id] = {
                "reason": reason,
                "start_time": discord.utils.utcnow(),
                "guild_id": ctx.guild.id if ctx.guild else None,
                "channel_id": ctx.channel.id,
            }

            embed = discord.Embed(
                description=f"{ctx.author.mention} is now AFK for **{reason}**",
                color=ctx.author.top_role.color
                if hasattr(ctx.author, "top_role")
                else discord.Color.blurple(),
            )
            embed.timestamp = discord.utils.utcnow()

            await ctx.reply(embed=embed)

        except Exception as e:
            logger.error(f"Error setting AFK status: {e}")
            await ctx.reply(
                "🚫 An error occurred while setting your AFK status. Please try again later.",
                delete_after=5,
            )

    @commands.command(
        name="cleardms", help="Clear all direct messages from the bot."
    )
    @commands.dm_only()
    async def clear_dms(self, ctx: commands.Context) -> None:
        """Clear all direct messages from the bot."""
        await ctx.send("Clearing all DMs...", delete_after=5)
        channel = ctx.channel
        try:
            # collect all bot-sent messages in DM
            bot_msgs = [
                msg
                async for msg in channel.history(limit=None)
                if msg.author.id == self.bot.user.id
            ]
            if not bot_msgs:
                return await ctx.send(
                    "No bot messages found to delete.", delete_after=5
                )

            deleted = 0
            # Try bulk delete in chunks when supported; fallback to per-message deletion otherwise.
            try:
                for i in range(0, len(bot_msgs), 100):
                    chunk = bot_msgs[i : i + 100]
                    if hasattr(channel, "delete_messages"):
                        # bulk delete may fail for messages older than 14 days or be unsupported in DMs;
                        # wrap per-chunk call to tolerate partial failures.
                        try:
                            await channel.delete_messages(chunk)
                            deleted += len(chunk)
                        except Exception:
                            # fallback to deleting individually for this chunk
                            for m in chunk:
                                try:
                                    await m.delete()
                                    deleted += 1
                                except Exception:
                                    continue
                    else:
                        for m in chunk:
                            try:
                                await m.delete()
                                deleted += 1
                            except Exception:
                                continue
            except Exception:
                # Last-resort fallback: delete messages individually
                deleted = 0
                for m in bot_msgs:
                    try:
                        await m.delete()
                        deleted += 1
                    except Exception:
                        continue

            await ctx.send(f"Deleted {deleted} message(s).", delete_after=5)
        except discord.Forbidden:
            await ctx.send(
                "I do not have permission to delete messages in this DM.",
                delete_after=5,
            )
        except Exception:
            await ctx.send("An error occurred", delete_after=5)

    @commands.command(name="setprefix")
    @commands.has_permissions(administrator=True)
    async def set_prefix(self, ctx: Context, prefix: str):
        try:
            await self.bot.database.set_prefix(ctx.guild.id, prefix)
            await Embeds.success(ctx, f"Prefix set to: `{prefix}`", reply=False)
        except Exception as e:
            await ctx.send(
                f"An error occurred while setting the prefix. Please try again later."
            )
            logger.error(f"Error setting prefix: {e}")

    @commands.command(
        name="mp3buttonmode",
        aliases=["mp3buttons", "mp3mode"],
        description="Toggle whether mp3 buttons attach the file instead of linking it.",
    )
    @commands.guild_only()
    @commands.check_any(commands.is_owner(), commands.has_permissions(manage_guild=True))
    async def mp3_button_mode(self, ctx: Context, state: str = "") -> None:
        """Attach the mp3 instead of linking it, per server.

        With no option this flips the mode; `on` / `off` set it outright and
        `status` shows where it stands. When it is on, pressing the mp3 button
        on a leak / session / stem / freestyle / remaster card downloads the
        file and drops it into an ephemeral reply.
        """
        settings = getattr(self.bot, "dash_settings", None)
        if settings is None:
            await Embeds.error(
                ctx,
                "The settings store isn't ready yet — try again in a moment.",
                reply=False,
                delete_after=None,
            )
            return

        wanted = (state or "").strip().lower()
        current = mp3_buttons.enabled(self.bot, ctx.guild.id)

        if wanted in mp3_buttons.STATUS_VALUES:
            await Embeds.success(
                ctx,
                f"mp3 button mode is currently **{'on' if current else 'off'}** "
                "in this server.",
                reply=False,
                delete_after=None,
            )
            return

        if wanted in ("", "toggle", "flip"):
            enabled = not current
        elif wanted in mp3_buttons.ON_VALUES:
            enabled = True
        elif wanted in mp3_buttons.OFF_VALUES:
            enabled = False
        else:
            await Embeds.error(
                ctx,
                f"Usage: `{ctx.prefix}mp3buttonmode [on|off|status]`",
                reply=False,
                delete_after=None,
            )
            return

        await settings.set_flag(
            mp3_buttons.MP3_BUTTON_MODE_KEY, enabled, guild_id=ctx.guild.id
        )
        note = (
            "mp3 buttons now download the file and attach it as an ephemeral reply."
            if enabled
            else "mp3 buttons are back to linking the file."
        )
        await Embeds.success(ctx, note, reply=False, delete_after=None)

    @commands.command(
        name="listbots", description="List all bots in the server."
    )
    async def list_bots(self, ctx: commands.Context) -> None:
        """List all bots in the server with their IDs."""
        bots = [member for member in ctx.guild.members if member.bot]

        embed = discord.Embed(
            title="Server Bots",
            description="There are no bots in the server."
            if not bots
            else "**List of bots:**\n"
            + "\n".join(f"{bot.mention} (`{bot.id}`)" for bot in bots),
            color=ctx.author.top_role.color or discord.Color.blurple(),
        )
        embed.set_footer(text=f"Total Bots: {len(bots)}")
        await ctx.send(embed=embed)

    @commands.command(
        name="botinfo", help="Get information about the bot."
    )
    async def botinfo(self, ctx: commands.Context) -> None:
        logger.debug("Botinfo command called")

        prefix = ctx.prefix or "/"

        app_info = await self.bot.application_info()
        owner_name = str(app_info.owner)
        owner_id = app_info.owner.id

        if app_info.team:
            dev_team_name = app_info.team.name
            owner_name = ", ".join([str(member) for member in app_info.team.members])
        else:
            dev_team_name = "Individual Developer"
        color = discord.Color.blurple()
        if isinstance(ctx.channel, discord.DMChannel):
            color = discord.Color.blurple()
        else:
            color = (
                ctx.author.top_role.color
                if ctx.author.top_role
                else discord.Color.blurple()
            )
        embed = discord.Embed(
            description="Developed and maintained by `mikk.a`",
            color=color,
        )
        embed.set_author(name="TPNE Bot")
        embed.add_field(name="Bot Admin(s):", value=f"{owner_name}", inline=False)
        embed.add_field(name="Dev Team:", value=f"{dev_team_name}", inline=False)
        embed.add_field(name="Prefix:", value=f"{prefix} for commands", inline=False)
        embed.add_field(
            name="<:python:1339013725168078980>\nPython Version:",
            value=f"{platform.python_version()}",
            inline=True,
        )
        embed.add_field(
            name="<:discord_icon:1339016862045962272>\nDiscord.py Version",
            value=f"{discord.__version__}",
            inline=True,
        )
        embed.add_field(
            name="Commands:",
            value=f"{len(self.bot.commands)}",
            inline=True,
        )
        embed.set_footer(text=f"Bot Version: {self.bot.version}")

        await ctx.reply(embed=embed)
        logger.debug("Botinfo message sent")

    @commands.hybrid_command(
        name="userinfo",
        aliases=["ui"],
        description="Displays information about a user.",
    )
    @unified_cooldown(5)
    async def userinfo(self, ctx: commands.Context, identifier: str):
        member = None

        if re.match(r"^\d+$", identifier):
            try:
                member = ctx.guild.get_member(int(identifier))
                if not member:
                    member = await self.bot.fetch_user(int(identifier))
            except discord.NotFound:
                pass

        elif re.match(r"^<@!?(\d+)>$", identifier):
            mention_match = re.match(r"^<@!?(\d+)>$", identifier)
            mention_id = mention_match.group(1)
            member = ctx.guild.get_member(int(mention_id))
            if not member:
                member = await self.bot.fetch_user(int(mention_id))
        else:
            identifier = identifier.lower()
            member = discord.utils.find(
                lambda m: identifier in m.name.lower(), ctx.guild.members
            )

        if not member:
            await Embeds.error(ctx, f"No user found with the identifier: {identifier}. Please try again.", reply=False)
            return

        # Check if this is a server member or just a User (non-server member)
        is_member = isinstance(member, discord.Member)

        # Member-specific fields
        if is_member:
            roles = [role for role in member.roles if role.name != "@everyone"]
            roles_string = " ".join([role.mention for role in roles]) if roles else "None"
            staff_perms = any(
                role.permissions.administrator
                or role.permissions.manage_guild
                or role.permissions.manage_roles
                or role.permissions.manage_channels
                or role.permissions.moderate_members
                for role in member.roles
            )

            STATUS_EMOJIS = {
                discord.Status.online: "<:status_online:1360657806079823972>",
                discord.Status.idle: "<:status_idle:1360657775822246051>",
                discord.Status.dnd: "<:status_dnd:1360657764744953998>",
                discord.Status.offline: "<:status_offline:1360657787981529148>",
            }

            def get_status_emoji(m):
                """Returns the appropriate emoji for a given member's status."""
                if m.activity and isinstance(m.activity, discord.Streaming):
                    return STATUS_EMOJIS[discord.Status.streaming]
                return STATUS_EMOJIS.get(
                    m.status, STATUS_EMOJIS[discord.Status.offline]
                )

            status_display = get_status_emoji(member)

            if member.premium_since:
                boosting_status = f"<:checkmark:1360657064019365980> since {member.premium_since.strftime('%m/%d/%Y, %I:%M%p')}"
            else:
                boosting_status = "<:crossmark:1360656870305693742>"

            joined_at = member.joined_at.strftime('%m/%d/%Y, %I:%M%p')
        else:
            # Non-server member - set defaults for member-specific fields
            roles_string = "N/A (not in server)"
            staff_perms = False
            status_display = "N/A (not in server)"
            boosting_status = "N/A (not in server)"
            joined_at = "N/A (not in server)"

        flags = member.public_flags
        if flags.hypesquad_bravery:
            hypesquad_house = "<a:bravery:1360659513547424049>"
        elif flags.hypesquad_brilliance:
            hypesquad_house = "<a:brilliance:1360659501199526037>"
        elif flags.hypesquad_balance:
            hypesquad_house = "<a:balance:1360659526117888102>"
        else:
            hypesquad_house = "None"

        rep_info = await self.bot.database.get_reputation_full(member.id)
        sobs_rx, _ = await self.bot.database.get_reaction_stats(member.id, "sobs")
        skulls_rx, _ = await self.bot.database.get_reaction_stats(member.id, "skulls")
        flames_rx, _ = await self.bot.database.get_reaction_stats(member.id, "flames")
        hearts_rx, _ = await self.bot.database.get_reaction_stats(member.id, "hearts")
        clowns_rx, _ = await self.bot.database.get_reaction_stats(member.id, "clowns")
        tomatoes_rx, _ = await self.bot.database.get_reaction_stats(member.id, "tomatoes")
        embed = discord.Embed(
            color=discord.Color.blurple(), timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=self.utils.get_avatar_url(member))

        # Add indicator for non-server members
        user_display = f"{member.name} (`{member.id}`)"
        if not is_member:
            user_display += " *(not in server)*"
        embed.add_field(
            name="__Username/ID__", value=user_display, inline=False
        )

        if staff_perms:
            embed.add_field(name="__Staff Permissions__", value="**Yes**", inline=False)

        embed.add_field(
            name="__Account__",
            value=f"**Status:** {status_display}\n**HypeSquad:** {hypesquad_house}\n**Booster:** {boosting_status}",
            inline=False,
        )
        embed.add_field(
            name="__Dates__",
            value=f"**Created:** {member.created_at.strftime('%m/%d/%Y, %I:%M%p')}\n**Joined:** {joined_at}",
            inline=False,
        )
        embed.add_field(
            name="__Roles__",
            value=f"Total Roles: **{len(roles) if is_member else 'N/A'}**\n{roles_string}",
            inline=False,
        )
        embed.add_field(
            name="__Stats__",
            value=(
                f"**Reputation/Karma:** {rep_info['reputation']:,} — *{rep_info['title']}*\n"
                f"**Sobs:** {sobs_rx:,} :sob:\n"
                f"**Skulls:** {skulls_rx:,} :skull:\n"
                f"**Flames:** {flames_rx:,} :fire:\n"
                f"**Hearts:** {hearts_rx:,} :heart:\n"
                f"**Clowns:** {clowns_rx:,} :clown:\n"
                f"**Tomatoes:** {tomatoes_rx:,} :tomato:\n"
            ),
            inline=False,
        )
        embed.set_author(
            name=f"User Info - {member}", icon_url=self.utils.get_avatar_url(ctx.author)
        )
        await ctx.send(embed=embed)

    @commands.command(
        name="names", help="View the username/nickname history of a user."
    )
    async def names(
        self,
        ctx: commands.Context,
        user: Optional[discord.User] = None,
        per_page: int = 10,
    ):
        """Displays the username and nickname history of a specified user.

        This command expects the database to return a list of dicts with keys:
        old_name, new_name, change_type, timestamp
        """
        user = user or ctx.author
        try:
            name_history = await self.bot.database.get_name_history(user.id)
        except Exception as e:
            logger.error(f"Failed to fetch name history for {user.id}: {e}")
            await ctx.send(
                "An error occurred while fetching name history.", delete_after=10
            )
            return

        if not name_history:
            await Embeds.error(ctx, f"No history found for {user.display_name}.", delete_after=10, reply=False)
            return

        view = NamesPaginationView(name_history, per_page=per_page)
        embed = view.get_current_embed()
        await ctx.send(embed=embed, view=view)

    @commands.command(
        name="clearnames", help="Clear your username/nickname history"
    )
    async def clearnames(self, ctx: commands.Context):
        """Clears the username and nickname history of the command invoker."""

        user = ctx.author
        if not await self.bot.database.has_name_history(user.id):
            await Embeds.error(ctx, f"No history found for {user.display_name}.", delete_after=10, reply=False)
            return

        await self.bot.database.clear_name_history(user.id)

        await Embeds.success(ctx, f"Cleared name history for {user.display_name}.", delete_after=10, reply=False)

    @commands.hybrid_command(
        name="serverinfo", aliases=["serverinf"], description="View information about the server."
    )
    @unified_cooldown(5)
    async def serverinfo(self, ctx: commands.Context) -> None:
        def EmojiBool(bool: bool):
            switch = {
                True: "<:checkmark:1360657064019365980>",
                False: "<:crossmark:1360656870305693742>",
            }
            return switch.get(bool, "N/A")

        guild = ctx.guild
        members = guild.members

        findbots = sum(1 for member in members if member.bot)
        online_members = sum(
            member.status != discord.Status.offline and not member.bot
            for member in members
        )
        total_members = guild.member_count

        features = set(guild.features)
        feature_flags = {
            "VANITY_URL": "Vanity URL",
            "INVITE_SPLASH": "Splash Invite",
            "ANIMATED_ICON": "Animated Icon",
            "DISCOVERY": "Server Discoverable",
            "BANNER": "Banner",
        }

        vanity_url = None
        if "VANITY_URL" in features:
            vanity_url = await guild.vanity_invite()

        color = (
            ctx.author.top_role.color
            if ctx.author.top_role
            else discord.Color.blurple()
        )

        embed = discord.Embed(
            title=f"**{guild.name}**", colour=color, timestamp=discord.utils.utcnow()
        )
        if guild.icon:
            embed.set_thumbnail(url=str(guild.icon.url))

        embed.add_field(
            name="Members",
            value=f"Bots: **{findbots}**\nHumans: **{total_members - findbots}**\nOnline Members: **{online_members}/{total_members}**",
        )

        embed.add_field(
            name="Channels",
            value=f"\U0001f4ac Text Channels: **{len(guild.text_channels)}**\n\U0001f50a Voice Channels: **{len(guild.voice_channels)}**",
        )

        embed.add_field(
            name="Important Info",
            value=f"Owner: {guild.owner.mention}\nVerification Level: **{str(guild.verification_level).title()}**\nGuild ID: **{guild.id}**",
            inline=False,
        )

        embed.add_field(
            name="Other Info",
            value=f"AFK Channel: **{guild.afk_channel}**\nAFK Timeout: **{guild.afk_timeout // 60} minute(s)**\nCustom Emojis: **{len(guild.emojis)}**\nRole Count: **{len(guild.roles)}**\nFilesize Limit: **{humanize.naturalsize(guild.filesize_limit)}**",
            inline=False,
        )

        features_value = "\n".join(
            f"{EmojiBool(feature in features)} {name}"
            + (
                f" ({vanity_url.url[15:]})"
                if feature == "VANITY_URL" and vanity_url
                else ""
            )
            for feature, name in feature_flags.items()
        )

        embed.add_field(name="Server Features", value=features_value)

        embed.add_field(
            name="Boost Info",
            value=f"Number of Boosts: **{guild.premium_subscription_count}**\nBooster Role: **{guild.premium_subscriber_role.mention if guild.premium_subscriber_role else 'None'}**\nBoost Level/Tier: **{guild.premium_tier}**",
        )
        await ctx.reply(embed=embed)

    @commands.command(
        name="servericon", help="View the server icon of the current server."
    )
    async def servericon(self, ctx: commands.Context) -> None:
        embed = discord.Embed(
            title=f"**{ctx.guild.name}** Icon", color=ctx.author.top_role.color if ctx.author.top_role else discord.Color.blurple()
        )

        server_icon_url = ctx.guild.icon.url if ctx.guild.icon else None

        if server_icon_url:
            embed.set_image(url=server_icon_url)
        else:
            embed.description = "This server does not have an icon."

        await ctx.reply(embed=embed)

    @commands.command(
        name="serverbanner", help="View the server banner of the current server."
    )
    async def serverbanner(self, ctx: commands.Context) -> None:
        embed = discord.Embed(
            title=f"**{ctx.guild.name}** Banner", color=ctx.author.top_role.color if ctx.author.top_role else discord.Color.blurple()
        )

        server_banner_url = ctx.guild.banner.url if ctx.guild.banner else None

        if server_banner_url:
            embed.set_image(url=server_banner_url)
        else:
            embed.description = "This server does not have a banner."

        await ctx.reply(embed=embed)

    @commands.hybrid_command(
        name="ping", description="Check the bot's latency and performance."
    )
    @unified_cooldown(5)
    async def ping(self, ctx: commands.Context) -> None:
        websocket_latency = round(self.bot.latency * 1000, 2)

        # Entering the typing context manager is exactly one POST to the typing
        # endpoint, so timing the block measures that round trip. The previous
        # version held the block open for a fixed 100ms and counted that sleep
        # as latency, which reported ~100ms more than the call actually took.
        # The indicator still shows for Discord's own ~10s either way.
        typing_start = discord.utils.utcnow()
        async with ctx.channel.typing():
            pass
        typing_end = discord.utils.utcnow()
        typing_latency = round((typing_end - typing_start).total_seconds() * 1000, 2)

        db_start = discord.utils.utcnow()
        async with self.bot.database.get_session() as session:
            async with session.begin():
                result = await session.execute(text("SELECT 1"))
                _ = result.scalar()
        db_end = discord.utils.utcnow()
        database_latency = round((db_end - db_start).total_seconds() * 1000, 2)

        embed = discord.Embed(
            title="🏓 Pong!",
            color=0xE2765A,
        )
        embed.add_field(
            name="🖧 Websocket Latency", value=f"{websocket_latency} ms", inline=False
        )
        embed.add_field(
            name="••• Typing Latency", value=f"{typing_latency} ms", inline=False
        )
        embed.add_field(
            name="💾 Database Latency", value=f"{database_latency} ms", inline=False
        )

        await ctx.reply(embed=embed)

    @commands.hybrid_command(
        name="avatar",
        aliases=["av", "pfp"],
        description="View the avatar of a user -- yours when no one is given.",
    )
    @unified_cooldown(3)
    async def avatar(
        self, ctx: commands.Context, member: Optional[discord.Member] = None
    ) -> None:
        member = member or ctx.author
        embed = discord.Embed(
            color=0xE2765A,
        )
        embed.set_image(url=self.utils.get_avatar_url(member))
        await ctx.reply(embed=embed)

    @commands.command(
        name="serveravatar",
        aliases=["sav"],
        help="View the server-specific avatar of a user (if they have one). Defaults to your own avatar.",
    )
    async def server_avatar(
        self, ctx: commands.Context, member: Optional[discord.Member] = None
    ):
        member = member or ctx.author

        server_avatar_url = member.display_avatar.url if member.display_avatar else None

        if server_avatar_url:
            embed = discord.Embed(
                color=0xE2765A,
            )
            embed.set_image(url=server_avatar_url)
            await ctx.reply(embed=embed)
        else:
            await Embeds.error(ctx, f"{member.display_name} does not have a server-specific avatar. Here's their global avatar: {member.display_avatar.url}", reply=True)

    @commands.command(
        name="servericon",
        description="View the icon of the current server."
    )
    async def servericon(self, ctx: commands.Context) -> None:
        embed = discord.Embed(
            title=f"**{ctx.guild.name}** Icon", color=ctx.author.top_role.color if ctx.author.top_role else discord.Color.blurple()
        )

        server_icon_url = ctx.guild.icon.url if ctx.guild.icon else None

        if server_icon_url:
            embed.set_image(url=server_icon_url)
        else:
            embed.description = "This server does not have an icon."

        await ctx.reply(embed=embed)

    @commands.command(
        name="invite",
        help="Get the invite link of the bot to be able to invite it.",
    )
    async def invite(self, ctx: commands.Context) -> None:
        logger.debug("Invite command called")
        embed = discord.Embed(
            description=f"Invite me by clicking here!",
            color=0xD75BF4,
        )

        url = discord.utils.oauth_url(
            self.bot.user.id, permissions=discord.Permissions(permissions=8)
        )

        try:
            view = discord.ui.View()
            view.add_item(
                discord.ui.Button(
                    label="Invite me", url=url, style=discord.ButtonStyle.link
                )
            )
            await ctx.author.send(embed=embed, view=view)
            await ctx.reply("I sent you a DM!")
            logger.debug("Invite link sent via DMs")
        except discord.Forbidden:
            await ctx.reply(embed=embed, view=view)
            logger.warning("Failed to send invite link via DMs, sending in channel")

    # ------------------------------------------------------------------
    # Emoji stealing
    # ------------------------------------------------------------------

    #: Discord's emoji CDN. ``PartialEmoji.url`` uses an animated WebP, which is
    #: what re-uploads with a white background, so the URL is built directly.
    EMOJI_CDN = "https://cdn.discordapp.com/emojis"

    #: What one emoji image may weigh. Discord rejects anything larger, and a
    #: phone screenshot is usually a few MB -- so uploads are shrunk first.
    MAX_EMOJI_BYTES = 256 * 1024

    #: What is worth downloading before decoding. The image itself is shrunk to
    #: fit, but pulling a huge upload through the bot's connection is not.
    MAX_EMOJI_SOURCE_BYTES = 10 * 1024 * 1024

    #: Emoji are displayed at 128px, so anything bigger is wasted bytes.
    EMOJI_IMAGE_SIZE = 128

    #: Decoding a huge image costs width * height * 4 bytes of RAM before the
    #: downscale, so very large pictures are refused rather than risked.
    MAX_EMOJI_PIXELS = 40_000_000

    #: Attachment extensions accepted when the MIME type is missing or wrong.
    EMOJI_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")

    @staticmethod
    def _is_emoji_image(attachment) -> bool:
        """Whether an attachment looks like a picture Discord can take."""
        filename = str(getattr(attachment, "filename", "") or "").lower()
        content_type = str(getattr(attachment, "content_type", "") or "").lower()
        return content_type.startswith("image/") or filename.endswith(
            General.EMOJI_IMAGE_SUFFIXES
        )

    @classmethod
    def _emoji_attachment(
        cls, message: Optional[discord.Message]
    ) -> Optional[discord.Attachment]:
        """The first image attachment on ``message``, or ``None``."""
        for attachment in getattr(message, "attachments", None) or []:
            if cls._is_emoji_image(attachment):
                return attachment
        return None

    @classmethod
    def _prepare_emoji_image(cls, data: bytes) -> bytes:
        """Shrink an upload into something Discord accepts as an emoji.

        Static images are re-encoded as an emoji-sized RGBA PNG -- the fix
        ``_emoji_png`` makes for stolen emojis (no white box behind the
        transparency), plus the downscale that lets a phone screenshot fit in
        Discord's 256 KB limit. Animated GIFs are passed through untouched,
        because resizing every frame changes the timing; one that is already
        over the limit cannot be shrunk here and is refused.

        Raises ``ValueError`` with a user-facing message for anything that
        cannot become an emoji.
        """
        try:
            with Image.open(io.BytesIO(data)) as image:
                if getattr(image, "is_animated", False):
                    if len(data) > cls.MAX_EMOJI_BYTES:
                        raise ValueError(
                            "Animated emojis have to be under 256 KB as-is — "
                            "try a smaller GIF or a still image."
                        )
                    return data

                width, height = image.size
                if width * height > cls.MAX_EMOJI_PIXELS:
                    raise ValueError(
                        "That image is too large to turn into an emoji — "
                        "crop or shrink it first."
                    )

                # JPEG only: decode straight to a small version instead of
                # allocating the full-resolution bitmap first.
                image.draft("RGB", (cls.EMOJI_IMAGE_SIZE * 2,) * 2)
                frame = image.convert("RGBA")
        except ValueError:
            raise
        except Exception as error:
            raise ValueError("That attachment doesn't look like an image.") from error

        frame.thumbnail((cls.EMOJI_IMAGE_SIZE, cls.EMOJI_IMAGE_SIZE), Image.LANCZOS)
        out = io.BytesIO()
        frame.save(out, format="PNG", optimize=True)
        return out.getvalue()

    @staticmethod
    def _sanitize_emoji_name(raw: str) -> Optional[str]:
        """Turn a free-text reply into a name Discord will accept."""
        name = re.sub(r"[^A-Za-z0-9_]+", "_", (raw or "").strip()).strip("_")
        name = name[:32]
        return name if len(name) >= 2 else None

    @staticmethod
    def _emoji_png(data: bytes) -> Optional[bytes]:
        """Re-encode a static emoji as a true RGBA PNG.

        Discord's CDN serves palette PNGs, and some save/re-save paths drop the
        alpha channel on the way back in -- which is exactly what leaves a white
        box behind when the emoji is added by hand. Converting to true-colour
        RGBA keeps the transparency intact.
        """
        try:
            image = Image.open(io.BytesIO(data))
            if getattr(image, "is_animated", False):
                return None
            out = io.BytesIO()
            image.convert("RGBA").save(out, format="PNG")
            return out.getvalue()
        except Exception:
            logger.debug("Could not re-encode emoji to RGBA PNG", exc_info=True)
            return None

    async def _download_emoji_asset(
        self, ctx: commands.Context, emoji: str
    ) -> Optional[tuple[bytes, str, str]]:
        """Fetch a custom emoji as ``(bytes, name, extension)``.

        Animated emojis come down as GIF and static ones as PNG, with the PNG
        forced back to straight RGBA. Returns ``None`` after replying when the
        emoji is invalid, un-fetchable, or not a custom emoji.
        """
        try:
            partial_emoji = discord.PartialEmoji.from_str(emoji)
        except Exception:
            partial_emoji = None

        if partial_emoji is None or not partial_emoji.is_custom_emoji():
            await ctx.reply(
                "You provided a standard emoji, which cannot be stolen.",
                delete_after=5,
            )
            return None

        ext = "gif" if partial_emoji.animated else "png"
        emoji_name = partial_emoji.name or "emoji"
        emoji_url = f"{self.EMOJI_CDN}/{partial_emoji.id}.{ext}"

        try:
            async with self.session.get(emoji_url) as response:
                if response.status != 200:
                    await ctx.reply(
                        "Failed to download the emoji.", delete_after=5
                    )
                    return None
                data = await response.read()
        except aiohttp.ClientError:
            logger.warning("Failed to download emoji %s", emoji_name, exc_info=True)
            await ctx.reply("Failed to download the emoji.", delete_after=5)
            return None

        if not partial_emoji.animated:
            png = self._emoji_png(data)
            if png is not None:
                data = png

        return data, emoji_name, ext

    @commands.command(
        name="emojisteal",
        aliases=["stealemoji"],
        help="Steal a custom server emoji",
    )
    async def steal(self, ctx: commands.Context, emoji: str):
        """Steal a custom server emoji"""
        asset = await self._download_emoji_asset(ctx, emoji)
        if asset is None:
            return
        data, emoji_name, ext = asset
        await ctx.reply(
            file=discord.File(fp=io.BytesIO(data), filename=f"{emoji_name}.{ext}")
        )

    @commands.command(
        name="stealaddemoji",
        aliases=["stealemojiadd"],
        help="Steal a custom emoji and add it to this server under a name you reply with.",
    )
    @commands.guild_only()
    @commands.has_permissions(manage_emojis_and_stickers=True)
    @commands.bot_has_permissions(manage_emojis_and_stickers=True)
    async def steal_add_emoji(self, ctx: commands.Context, emoji: str):
        """Steal an emoji, then add it to the server under the name you send next."""
        asset = await self._download_emoji_asset(ctx, emoji)
        if asset is None:
            return
        data, suggested, ext = asset

        prompt = await ctx.reply(
            "What should I name this emoji? Send the name as your next message in "
            "this channel (2–32 letters, numbers or underscores).\n"
            f"-# Original name: `{suggested}`",
            file=discord.File(fp=io.BytesIO(data), filename=f"{suggested}.{ext}"),
        )

        def check(message: discord.Message) -> bool:
            return (
                message.author.id == ctx.author.id
                and message.channel.id == ctx.channel.id
            )

        try:
            name_message = await self.bot.wait_for("message", check=check, timeout=60)
        except asyncio.TimeoutError:
            await ctx.send(
                "⌛ Timed out waiting for a name. Run the command again.",
                delete_after=10,
            )
            return

        name = self._sanitize_emoji_name(name_message.content)
        if name is None:
            await ctx.send(
                "❌ That name won't work. Emoji names need 2–32 letters, numbers "
                "or underscores.",
                delete_after=10,
            )
            return

        # The name is in hand, so the prompt and the reply have done their job.
        for message in (prompt, name_message):
            try:
                await message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass

        try:
            created = await ctx.guild.create_custom_emoji(
                name=name,
                image=data,
                reason=f"Emoji stolen by {ctx.author}",
            )
        except discord.Forbidden:
            await ctx.send(
                "❌ I don't have permission to add emojis to this server.",
                delete_after=10,
            )
            return
        except discord.HTTPException as exc:
            await ctx.send(
                f"❌ Discord rejected that emoji (`{exc.status}`) — the server may "
                "be out of slots, or animated emojis may need a boost level.",
                delete_after=10,
            )
            return

        await ctx.send(f"✅ Added {created} as `:{created.name}:`.")

    @commands.command(
        name="createemoji",
        aliases=["newemoji", "makeemoji"],
        help="Turn an image into a server emoji. Attach the image and give it a name.",
    )
    @commands.guild_only()
    # Bot admins (`admin allow`) and the application team may add emojis
    # without holding Manage Expressions themselves; everyone else still needs
    # it. The bot still needs the permission of its own to upload the image.
    @commands.check_any(
        commands.is_owner(),
        commands.has_permissions(manage_emojis_and_stickers=True),
    )
    @commands.bot_has_permissions(manage_emojis_and_stickers=True)
    async def create_emoji(
        self,
        ctx: commands.Context,
        name: str,
        image: Optional[discord.Attachment] = None,
    ) -> None:
        """Create a server emoji from an attached image.

        Usage: `{PREFIX}createemoji <name>` with the image attached to the same
        message (or to the message being replied to). The slash version takes
        the picture as a file: `/createemoji name:<name> image:<file>`.
        """
        emoji_name = self._sanitize_emoji_name(name)
        if emoji_name is None:
            await ctx.reply(
                "❌ Emoji names need 2–32 letters, numbers or underscores.",
                delete_after=10,
            )
            return

        # discord.py fills an attachment-typed parameter from the invoking
        # message, so the picture normally arrives as `image` already. It only
        # takes the *first* file, though, which may be a video next to the
        # picture -- so fall back to the message, then to the replied-to one.
        attachment = image
        if attachment is None or not self._is_emoji_image(attachment):
            attachment = self._emoji_attachment(ctx.message) or attachment
        if attachment is None:
            reference = getattr(ctx.message, "reference", None)
            if reference is not None:
                attachment = self._emoji_attachment(
                    getattr(reference, "resolved", None)
                )
        if attachment is None:
            await ctx.reply(
                "❌ Attach the image you want as an emoji — "
                f"`{ctx.prefix}createemoji {emoji_name}` with the picture on the "
                "same message, or reply to the picture with that command.",
                delete_after=15,
            )
            return

        size = int(getattr(attachment, "size", 0) or 0)
        if size > self.MAX_EMOJI_SOURCE_BYTES:
            await ctx.reply(
                f"❌ That file is {humanfriendly.format_size(size)} — "
                f"I can only read images up to "
                f"{humanfriendly.format_size(self.MAX_EMOJI_SOURCE_BYTES)}.",
                delete_after=10,
            )
            return
        try:
            data = await attachment.read()
        except discord.HTTPException:
            logger.warning(
                "Could not download emoji attachment %s",
                getattr(attachment, "filename", "?"),
                exc_info=True,
            )
            await ctx.reply(
                "❌ I couldn't download that attachment.", delete_after=10
            )
            return

        if not data:
            await ctx.reply("❌ That image came through empty.", delete_after=10)
            return

        try:
            data = self._prepare_emoji_image(data)
        except ValueError as error:
            await ctx.reply(f"❌ {error}", delete_after=10)
            return

        existing = discord.utils.get(ctx.guild.emojis, name=emoji_name)
        if existing is not None:
            await ctx.reply(
                f"❌ This server already has an emoji called `:{emoji_name}:` — "
                "pick another name.",
                delete_after=10,
            )
            return
        if len(ctx.guild.emojis) >= ctx.guild.emoji_limit:
            await ctx.reply(
                f"❌ This server is out of emoji slots "
                f"({len(ctx.guild.emojis)}/{ctx.guild.emoji_limit}).",
                delete_after=10,
            )
            return

        try:
            created = await ctx.guild.create_custom_emoji(
                name=emoji_name,
                image=data,
                reason=f"Emoji created by {ctx.author} ({ctx.author.id})",
            )
        except discord.Forbidden:
            await ctx.reply(
                "❌ I don't have permission to add emojis to this server.",
                delete_after=10,
            )
            return
        except discord.HTTPException as error:
            logger.warning(
                "Discord rejected emoji %s in guild %s",
                emoji_name,
                getattr(ctx.guild, "id", "?"),
                exc_info=True,
            )
            status = f" (`{error.status}`)" if error.status else ""
            await ctx.reply(
                f"❌ Discord rejected that image{status} — it may be an "
                "unsupported format, or animated emojis may need a boost level.",
                delete_after=10,
            )
            return

        await ctx.send(f"✅ Added {created} as `:{created.name}:`.")

    @commands.command(
        name="convert", help="Convert currency from one type to another."
    )
    async def convert_currency(
        self, ctx: commands.Context, amount: float, from_currency: str, to_currency: str
    ):
        async with borrowed() as session:
            try:
                url = f"{self.currency_api}/{from_currency.lower()}.json"
                async with session.get(url) as response:
                    if response.status != 200:
                        error_msg = (
                            f"Failed to fetch conversion data. Please try again later."
                        )
                        logger.error(error_msg)
                        await ctx.reply(error_msg, delete_after=5)
                        return

                    data = await response.json()
                    rates = data.get(from_currency.lower(), {})

                    if to_currency.lower() not in rates:
                        error_msg = (
                            f"Conversion rate for {to_currency.upper()} not found."
                        )
                        logger.error(error_msg)
                        await ctx.reply(error_msg, delete_after=5)
                        return

                    conversion_rate = rates[to_currency.lower()]
                    converted_amount = Decimal(str(amount)) * Decimal(
                        str(conversion_rate)
                    )

                    embed = discord.Embed(
                        description=f"**{amount:,.2f} {from_currency.upper()}** is equal to **{converted_amount:,.2f} {to_currency.upper()}** as of {data['date']}."
                    )
                    embed.set_author(
                        name="Currency Conversion",
                        icon_url=self.utils.get_avatar_url(ctx.author),
                    )
                    embed.set_footer(text=f"{ctx.author.name}")
                    embed.timestamp = discord.utils.utcnow()
                    await ctx.reply(embed=embed)
            except aiohttp.ClientError as e:
                error_msg = f"An error occurred while fetching data: {str(e)}"
                logger.error(error_msg)
                await ctx.reply(error_msg)
            except json.JSONDecodeError as e:
                error_msg = f"Failed to decode JSON response: {str(e)}"
                logger.error(error_msg)
                await ctx.reply(error_msg)
            except Exception as e:
                error_msg = f"An unexpected error occurred: {str(e)}"
                logger.error(error_msg)
                await ctx.reply(error_msg)

    @commands.command(
        name="password", help="Generate a random password"
    )
    async def generate_password_command(
        self,
        ctx: commands.Context,
        length: int = 16,
        use_digits: bool = True,
        use_punctuation: bool = True,
    ):
        """Securely generate a random password"""
        if length < 4:
            await ctx.reply(
                "Password length must be at least 4 characters.", delete_after=5
            )
            return

        # Use the existing secure generator (uses secrets)
        password = self.generate_password(length, use_digits, use_punctuation)

        try:
            await ctx.author.send(f"{password}")
            await ctx.reply("I've sent you a DM.")
        except discord.Forbidden:
            await ctx.reply(
                "I couldn't DM you. Please enable DMs from server members and try again.",
                delete_after=10,
            )
        except Exception as e:
            logger.error(f"Failed to send password DM to {ctx.author.id}: {e}")
            await ctx.reply(
                "An error occurred while sending the DM. Please try again later.",
                delete_after=10,
            )

    @commands.command(
        name="uptime", help="Check the bot's uptime."
    )
    async def uptime(self, ctx: commands.Context):
        """Shows the bot's uptime."""
        current_time = discord.utils.utcnow()
        uptime_duration = current_time - self.start_time
        days, seconds = uptime_duration.days, uptime_duration.seconds
        weeks, days = divmod(days, 7)
        hours, seconds = divmod(seconds, 3600)
        minutes, seconds = divmod(seconds, 60)
        uptime_str = (
            (f"{weeks} week{'s' if weeks > 1 else ''}, " if weeks > 0 else "")
            + (f"{days} day{'s' if days > 1 else ''}, " if days > 0 else "")
            + (f"{hours} hour{'s' if hours > 1 else ''}, " if hours > 0 else "")
            + (f"{minutes} minute{'s' if minutes > 1 else ''}, " if minutes > 0 else "")
            + (f"{seconds} second{'s' if seconds > 1 else ''}")
        )
        color = (
            ctx.author.top_role.color
            if ctx.author.top_role
            else discord.Color.blurple()
        )
        embed = discord.Embed(
            title="Bot Uptime",
            description=f"The bot has been up for **{uptime_str}**.",
            color=color,
        )
        await ctx.reply(embed=embed)

    @commands.hybrid_command(
        name="quickpoll",
        aliases=["qp"],
        description="Creates a poll by reacting to the user's message.",
    )
    @unified_cooldown(5)
    async def quickpoll(self, ctx: Context, *, question: str):
        """React to the invoking message with upvote/downvote to create a poll."""
        try:
            if ctx.message:
                await ctx.message.add_reaction("👍")
                await ctx.message.add_reaction("👎")
            else:
                poll = await ctx.send(f"📊 {question}")
                await poll.add_reaction("👍")
                await poll.add_reaction("👎")
        except discord.HTTPException:
            return

    async def _resolve_input_from_reply(
        self, ctx: commands.Context, provided: str | None
    ) -> str | None:
        """
        Helper to resolve input: prefer provided string, otherwise attempt to fetch
        the referenced message's content (if the user replied to a message).
        Returns None if no input could be resolved.
        """
        if provided:
            return provided
        ref = ctx.message.reference
        if not ref or not getattr(ref, "message_id", None):
            return None
        try:
            ch = ctx.channel
            if getattr(ref, "channel_id", None):
                ch = self.bot.get_channel(
                    ref.channel_id
                ) or await self.bot.fetch_channel(ref.channel_id)
            ref_msg = await ch.fetch_message(ref.message_id)
            return ref_msg.content
        except Exception:
            return None

    def _clean_input(self, s: str) -> str:
        """Strip code formatting from provided input."""
        if not s:
            return s
        s = s.strip()
        if s.startswith("```") and s.endswith("```"):
            s = s[3:-3]
        s = s.strip("` \n\r\t")
        return s.strip()

    @commands.group(
        name="encode", help="Encoding commands.", invoke_without_command=True
    )
    async def encode(self, ctx: commands.Context):
        prefix = ctx.prefix or "/"

        subcmds = getattr(ctx.command, "commands", []) or []
        lines = []
        for cmd in sorted(subcmds, key=lambda c: c.name):
            name = cmd.name
            aliases = (
                f" (or: {', '.join(cmd.aliases)})"
                if getattr(cmd, "aliases", None)
                else ""
            )
            desc = render((cmd.help or cmd.description or ""), prefix).strip()
            if desc:
                lines.append(f"`{prefix}encode {name}`{aliases} — {desc}")
            else:
                lines.append(f"`{prefix}encode {name}`{aliases}")

        description = "\n".join(lines) if lines else "No subcommands available."

        embed = discord.Embed(
            title="Encode — Available Commands",
            description=description,
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Use {prefix}encode <subcommand> for details.")
        await ctx.reply(embed=embed, mention_author=False)

    @encode.command(name="binary", description="Convert text to binary.")
    @unified_cooldown(5)
    async def binary_encode(
        self, ctx: commands.Context, text: Optional[str] = None
    ):
        binary_pattern = r"^[01\s]+$"
        text = await self._resolve_input_from_reply(ctx, text)

        if not text:
            embed = discord.Embed(
                title="Text to Binary",
                description="Please provide text or reply to a message containing the text.",
                color=discord.Color.blurple(),
            )
            return await ctx.reply(embed=embed)

        if not (re.match(binary_pattern, text) is None):
            embed = discord.Embed(
                title="Text to Binary",
                description="The provided text appears to be binary already. Please provide non-binary text.",
                color=discord.Color.blurple(),
            )
            return await ctx.reply(embed=embed)

        text = self._clean_input(text)
        binary_result = " ".join(format(ord(char), "08b") for char in text)
        embed = discord.Embed(
            title="Text to Binary",
            description=f"```{binary_result}```",
            color=discord.Color.blurple(),
        )
        await ctx.reply(embed=embed)

    @encode.command(name="base64", description="Convert text to base64.")
    @unified_cooldown(5)
    async def base64_encode(
        self, ctx: commands.Context, text: Optional[str] = None
    ):
        base64_pattern = (
            r"^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$"
        )
        text = await self._resolve_input_from_reply(ctx, text)
        if not text:
            embed = discord.Embed(
                title="Text to Base64",
                description="Please provide text or reply to a message containing the text.",
                color=discord.Color.blurple(),
            )
            return await ctx.reply(embed=embed)

        if not (re.match(base64_pattern, text) is None):
            embed = discord.Embed(
                title="Text to Base64",
                description="The provided text appears to be base64 already. Please provide non-base64 text.",
                color=discord.Color.blurple(),
            )
            return await ctx.reply(embed=embed)

        text = self._clean_input(text)
        try:
            encoded = base64.b64encode(text.encode()).decode()
        except Exception as e:
            encoded = f"Error encoding to base64: {e}"
        embed = discord.Embed(
            title="Text to Base64",
            description=f"```{encoded}```",
            color=discord.Color.blurple(),
        )
        await ctx.reply(embed=embed)

    @encode.command(name="rot13", description="Apply ROT13 encoding to text.")
    @unified_cooldown(5)
    async def rot13_encode(
        self, ctx: commands.Context, text: Optional[str] = None
    ):
        text = await self._resolve_input_from_reply(ctx, text)
        if not text:
            embed = discord.Embed(
                title="ROT13 Encode",
                description="Please provide text or reply to a message containing the text.",
                color=discord.Color.blurple(),
            )
            return await ctx.reply(embed=embed)

        text = self._clean_input(text)
        rot13_text = text.translate(
            str.maketrans(
                "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
                "NOPQRSTUVWXYZABCDEFGHIJKLMnopqrstuvwxyzabcdefghijklm",
            )
        )
        embed = discord.Embed(
            title="ROT13 Encode",
            description=f"```{rot13_text}```",
            color=discord.Color.blurple(),
        )
        await ctx.reply(embed=embed)

    @encode.command(name="hex", description="Convert text to hexadecimal.")
    @unified_cooldown(5)
    async def hex_encode(
        self, ctx: commands.Context, text: Optional[str] = None
    ):
        hex_pattern = r"^(0x)?[0-9a-fA-F]+$"
        text = await self._resolve_input_from_reply(ctx, text)
        if not text:
            embed = discord.Embed(
                title="Text to Hexadecimal",
                description="Please provide text or reply to a message containing the text.",
                color=discord.Color.blurple(),
            )
            return await ctx.reply(embed=embed)

        if not (re.match(hex_pattern, text) is None):
            embed = discord.Embed(
                title="Text to Hexadecimal",
                description="The provided text appears to be hexadecimal already. Please provide non-hexadecimal text.",
                color=discord.Color.blurple(),
            )
            return await ctx.reply(embed=embed)

        text = self._clean_input(text)
        try:
            hex_result = text.encode().hex()
        except Exception as e:
            hex_result = f"Error converting to hex: {e}"
        embed = discord.Embed(
            title="Text to Hexadecimal",
            description=f"```{hex_result}```",
            color=discord.Color.blurple(),
        )
        await ctx.reply(embed=embed)

    @encode.command(name="morse", description="Convert text to Morse code.")
    @unified_cooldown(5)
    async def morse_encode(
        self, ctx: commands.Context, text: Optional[str] = None
    ):
        morse_pattern = r"^[\s\.-/]+$"
        text = await self._resolve_input_from_reply(ctx, text)
        if not text:
            embed = discord.Embed(
                title="Text to Morse Code",
                description="Please provide text or reply to a message containing the text.",
                color=discord.Color.blurple(),
            )
            return await ctx.reply(embed=embed)

        if not (re.match(morse_pattern, text) is None):
            embed = discord.Embed(
                title="Text to Morse Code",
                description="The provided text appears to be Morse code already. Please provide non-Morse text.",
                color=discord.Color.blurple(),
            )
            return await ctx.reply(embed=embed)

        MORSE_CODE_DICT = {
            "A": ".-",
            "B": "-...",
            "C": "-.-.",
            "D": "-..",
            "E": ".",
            "F": "..-.",
            "G": "--.",
            "H": "....",
            "I": "..",
            "J": ".---",
            "K": "-.-",
            "L": ".-..",
            "M": "--",
            "N": "-.",
            "O": "---",
            "P": ".--.",
            "Q": "--.-",
            "R": ".-.",
            "S": "...",
            "T": "-",
            "U": "..-",
            "V": "...-",
            "W": ".--",
            "X": "-..-",
            "Y": "-.--",
            "Z": "--..",
            "0": "-----",
            "1": ".----",
            "2": "..---",
            "3": "...--",
            "4": "....-",
            "5": ".....",
            "6": "-....",
            "7": "--...",
            "8": "---..",
            "9": "----.",
            ",": "--..--",
            ".": ".-.-.-",
            "?": "..--..",
            "/": "-..-.",
            "-": "-....-",
            "(": "-.--.",
            ")": "-.--.-",
            " ": "/",
        }
        text = self._clean_input(text)
        morse_result = " ".join(MORSE_CODE_DICT.get(char.upper(), "") for char in text)
        embed = discord.Embed(
            title="Text to Morse Code",
            description=f"**Morse Code:** {morse_result}",
            color=discord.Color.blurple(),
        )
        await ctx.reply(embed=embed)

    @commands.group(
        name="decode", help="Decoding commands.", invoke_without_command=True
    )
    async def decode(self, ctx: commands.Context):
        prefix = ctx.prefix or "/"

        subcmds = getattr(ctx.command, "commands", []) or []
        lines = []
        for cmd in sorted(subcmds, key=lambda c: c.name):
            name = cmd.name
            aliases = (
                f" (or: {', '.join(cmd.aliases)})"
                if getattr(cmd, "aliases", None)
                else ""
            )
            desc = render((cmd.help or cmd.description or ""), prefix).strip()
            if desc:
                lines.append(f"`{prefix}decode {name}`{aliases} — {desc}")
            else:
                lines.append(f"`{prefix}decode {name}`{aliases}")

        description = "\n".join(lines) if lines else "No subcommands available."

        embed = discord.Embed(
            title="Decode — Available Commands",
            description=description,
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Use {prefix}decode <subcommand> for details.")
        await ctx.reply(embed=embed, mention_author=False)

    @decode.command(name="binary", description="Convert binary to text.")
    @unified_cooldown(5)
    async def binary_decode(
        self, ctx: commands.Context, binary: Optional[str] = None
    ):
        binary_pattern = r"^[01\s]+$"
        embed = discord.Embed(title="Binary to Text", color=discord.Color.blurple())
        try:
            binary = await self._resolve_input_from_reply(ctx, binary)
            if not binary:
                embed.description = "Please provide binary text or reply to a message containing binary."
                return await ctx.reply(embed=embed)

            if not re.match(binary_pattern, binary):
                embed.description = "The provided input is not valid binary. Please provide a string of 0s and 1s."
                return await ctx.reply(embed=embed)

            binary = self._clean_input(binary)
            binary_values = binary.split()
            if len(binary_values) == 1 and len(binary) % 8 == 0:
                binary_values = [binary[i : i + 8] for i in range(0, len(binary), 8)]
            text_result = "".join(chr(int(b, 2)) for b in binary_values)
            embed.description = f"```{text_result}```"
        except ValueError:
            embed.description = "Invalid binary input. Ensure it's composed of 0s and 1s in 8-bit chunks."
        await ctx.reply(embed=embed)

    @decode.command(name="base64", description="Convert base64 to text.")
    @unified_cooldown(5)
    async def base64_decode(
        self, ctx: commands.Context, text: Optional[str] = None
    ):
        base64_pattern = (
            r"^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$"
        )
        embed = discord.Embed(title="Base64 to Text", color=discord.Color.blurple())
        try:
            text = await self._resolve_input_from_reply(ctx, text)
            if not text:
                embed.description = (
                    "Please provide base64 text or reply to a message containing it."
                )
                return await ctx.reply(embed=embed)

            if not re.match(base64_pattern, text):
                embed.description = "The provided input is not valid base64. Please provide a valid base64 string."
                return await ctx.reply(embed=embed)

            text = self._clean_input(text)
            decoded = base64.b64decode(text.encode()).decode()
            embed.description = f"```{decoded}```"
        except Exception as e:
            embed.description = (
                f"Error decoding base64: {str(e)}. Make sure the input is valid base64."
            )
        await ctx.reply(embed=embed)

    @decode.command(name="rot13", description="Decode ROT13 encoded text.")
    @unified_cooldown(5)
    async def rot13_decode(
        self, ctx: commands.Context, text: Optional[str] = None
    ):
        text = await self._resolve_input_from_reply(ctx, text)
        if not text:
            embed = discord.Embed(
                title="ROT13 Decode",
                description="Please provide ROT13 text or reply to a message containing it.",
                color=discord.Color.blurple(),
            )
            return await ctx.reply(embed=embed)

        rot13_text = self._clean_input(text).translate(
            str.maketrans(
                "NOPQRSTUVWXYZABCDEFGHIJKLMnopqrstuvwxyzabcdefghijklm",
                "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
            )
        )
        embed = discord.Embed(
            title="ROT13 Decode",
            description=f"```{rot13_text}```",
            color=discord.Color.blurple(),
        )
        await ctx.reply(embed=embed)

    @decode.command(name="hex", description="Convert hexadecimal to text.")
    @unified_cooldown(5)
    async def hex_decode(
        self, ctx: commands.Context, text: Optional[str] = None
    ):
        hex_pattern = r"^(0x)?[0-9a-fA-F]+$"
        embed = discord.Embed(
            title="Hexadecimal to Text", color=discord.Color.blurple()
        )
        try:
            text = await self._resolve_input_from_reply(ctx, text)
            if not text:
                embed.description = (
                    "Please provide a hex string or reply to a message containing it."
                )
                return await ctx.reply(embed=embed)

            if not re.match(hex_pattern, text):
                embed.description = "The provided input is not valid hexadecimal. Please provide a valid hex string."
                return await ctx.reply(embed=embed)

            text = self._clean_input(text).replace(" ", "")
            bytes_object = bytes.fromhex(text)
            ascii_string = bytes_object.decode()
            embed.description = f"```{ascii_string}```"
        except ValueError:
            embed.description = (
                "Invalid hexadecimal input. Ensure it's a valid hex string."
            )
        await ctx.reply(embed=embed)

    @decode.command(name="morse", description="Convert Morse code to text.")
    @unified_cooldown(5)
    async def morse_decode(
        self, ctx: commands.Context, text: Optional[str] = None
    ):
        morse_pattern = r"^[\s\.-/]+$"
        text = await self._resolve_input_from_reply(ctx, text)

        if not text:
            embed = discord.Embed(
                title="Morse Code to Text",
                description="Please provide Morse code or reply to a message containing it.",
                color=discord.Color.blurple(),
            )
            return await ctx.reply(embed=embed)

        if not re.match(morse_pattern, text):
            embed = discord.Embed(
                title="Morse Code to Text",
                description="The provided input is not valid Morse code. Please provide a valid Morse code string.",
                color=discord.Color.blurple(),
            )
            return await ctx.reply(embed=embed)

        MORSE_CODE_DICT = {
            ".-": "A",
            "-...": "B",
            "-.-.": "C",
            "-..": "D",
            ".": "E",
            "..-.": "F",
            "--.": "G",
            "....": "H",
            "..": "I",
            ".---": "J",
            "-.-": "K",
            ".-..": "L",
            "--": "M",
            "-.": "N",
            "---": "O",
            ".--.": "P",
            "--.-": "Q",
            ".-.": "R",
            "...": "S",
            "-": "T",
            "..-": "U",
            "...-": "V",
            ".--": "W",
            "-..-": "X",
            "-.--": "Y",
            "--..": "Z",
            "-----": "0",
            ".----": "1",
            "..---": "2",
            "...--": "3",
            "....-": "4",
            ".....": "5",
            "-....": "6",
            "--...": "7",
            "---..": "8",
            "----.": "9",
            "--..--": ",",
            ".-.-.-": ".",
            "..--..": "?",
            "-..-.": "/",
            "-....-": "-",
            "-.--.": "(",
            "-.--.-": ")",
            "/": " ",
        }
        cleaned = self._clean_input(text)
        morse_words = cleaned.split(" ")
        decoded_message = "".join(MORSE_CODE_DICT.get(code, "") for code in morse_words)
        embed = discord.Embed(
            title="Morse Code to Text",
            description=f"```{decoded_message}```",
            color=discord.Color.blurple(),
        )
        await ctx.reply(embed=embed)

    coinmarketcap = app_commands.Group(
        name="coinmarketcap",
        description="CoinMarketCap command group",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=True
        ),
    )

    @coinmarketcap.command(name="price", description="Get the price of a cryptocurrency.")
    @app_commands.choices(
        coin=[
            app_commands.Choice(name="Bitcoin", value="BTC"),
            app_commands.Choice(name="Ethereum", value="ETH"),
            app_commands.Choice(name="Litecoin", value="LTC"),
            app_commands.Choice(name="Solana", value="SOL"),
            app_commands.Choice(name="Dogecoin", value="DOGE"),
            app_commands.Choice(name="Ripple", value="XRP"),
            app_commands.Choice(name="Monero", value="XMR"),
            app_commands.Choice(name="USD Coin", value="USDC"),
            app_commands.Choice(name="Tether", value="USDT"),
            app_commands.Choice(name="Cardano", value="ADA"),
            app_commands.Choice(name="Polkadot", value="DOT"),
            app_commands.Choice(name="Uniswap", value="UNI"),
        ],
        fiat=[
            app_commands.Choice(name="US Dollar", value="USD"),
            app_commands.Choice(name="Canadian Dollar", value="CAD"),
            app_commands.Choice(name="Great British Pound", value="GBP"),
            app_commands.Choice(name="Euro", value="EUR"),
            app_commands.Choice(name="Australian Dollar", value="AUD"),
            app_commands.Choice(name="Japanese Yen", value="JPY"),
            app_commands.Choice(name="Swiss Franc", value="CHF"),
            app_commands.Choice(name="Russian Ruble", value="RUB"),
            app_commands.Choice(name="Chinese Yuan", value="CNY"),
            app_commands.Choice(name="Indian Rupee", value="INR"),
        ],
    )
    @app_commands.describe(
        coin="The cryptocurrency to check (e.g., BTC, ETH, LTC)",
        fiat="The fiat currency to convert to (e.g., USD, EUR, GBP)",
    )
    @unified_cooldown(5)
    async def price(self, interaction: discord.Interaction, coin: str, fiat: str):
        blacklisted = await self.bot.database.is_user_blacklisted(interaction.user.id)

        if blacklisted:
            await interaction.response.send_message(
                "You are blacklisted from using this bot.", ephemeral=True
            )
            return

        async with borrowed() as session:
            coin = coin.upper()
            params = {
                "symbol": coin,
                "convert": fiat,
            }
            headers = {"X-CMC_PRO_API_KEY": COINMARKETCAP_API_KEY}

            async with session.get(
                COINMARKETCAP_API_URL, params=params, headers=headers
            ) as response:
                if response.status == 429:
                    await interaction.response.send_message(
                        "CoinMarketCap API limit reached. Please try again later.",
                        ephemeral=True,
                    )
                    return

                if response.status == 200:
                    data = await response.json()
                    crypto_data = data["data"][coin]
                    last_updated = crypto_data["last_updated"]
                    quote = crypto_data["quote"][fiat]
                    price = quote["price"]
                    market_cap = quote["market_cap"]
                    day_volume = quote["volume_24h"]

                    icon_url, color = {
                        "BTC": ("https://i.imgur.com/A11xhh5.png", 0xF7931A),
                        "ETH": ("https://i.imgur.com/rsCbciO.png", 0x9EB6B8),
                        "LTC": ("https://i.imgur.com/bP0VcZD.png", 0xFFFFFF),
                        "SOL": ("https://i.imgur.com/oSOUtKq.png", 0xDC1FFF),
                        "DOGE": ("https://i.imgur.com/cxOAlIK.png", 0xBA9F33),
                        "XRP": ("https://i.imgur.com/7N9CMeC.png", 0x23292F),
                        "USDC": ("https://i.imgur.com/dkMDTMy.png", 0x2775CA),
                        "USDT": ("https://i.imgur.com/jkgoDKi.png", 0x26A17B),
                        "XMR": ("https://i.imgur.com/w41aeUR.png", 0xFF6600),
                        "ADA": ("https://i.imgur.com/HtTONy3.png", 0x0D1E30),
                        "DOT": ("https://i.imgur.com/jzTa3hr.png", 0xE6007A),
                        "UNI": ("https://i.imgur.com/HatannT.png", 0xFF007A),
                    }.get(coin, ("https://i.imgur.com/OkcnXTM.png", 0x36393E))

                    try:
                        dt_obj = datetime.strptime(
                            last_updated, "%Y-%m-%dT%H:%M:%S.%fZ"
                        )
                    except ValueError:
                        try:
                            dt_obj = datetime.strptime(
                                last_updated, "%Y-%m-%dT%H:%M:%SZ"
                            )
                        except ValueError:
                            dt_obj = discord.utils.utcnow()

                    unix_timestamp = int(dt_obj.timestamp())
                    discord_timestamp = f"<t:{unix_timestamp}:R>"
                    change_1h = crypto_data["quote"][fiat]["percent_change_1h"]
                    change_24h = crypto_data["quote"][fiat]["percent_change_24h"]
                    change_7d = crypto_data["quote"][fiat]["percent_change_7d"]

                    change_1h = (
                        f"📈 +{change_1h:.2f}%"
                        if change_1h > 0
                        else (
                            f"📉 {change_1h:.2f}%"
                            if change_1h < 0
                            else f"➡️ {change_1h:.2f}%"
                        )
                    )
                    change_24h = (
                        f"📈 +{change_24h:.2f}%"
                        if change_24h > 0
                        else (
                            f"📉 {change_24h:.2f}%"
                            if change_24h < 0
                            else f"➡️ {change_24h:.2f}%"
                        )
                    )
                    change_7d = (
                        f"📈 +{change_7d:.2f}%"
                        if change_7d > 0
                        else (
                            f"📉 {change_7d:.2f}%"
                            if change_7d < 0
                            else f"➡️ {change_7d:.2f}%"
                        )
                    )

                    embed = discord.Embed(
                        title=f"{crypto_data['name']} ({crypto_data['symbol']})",
                        description=f"Last updated: {discord_timestamp}",
                        color=color,
                    )
                    embed.set_author(
                        name=f"{crypto_data['name']} Information ({fiat})",
                        icon_url=icon_url,
                    )
                    embed.add_field(
                        name=f"Price", value=f"{price:,.2f} {fiat}", inline=True
                    )
                    embed.add_field(
                        name=f"Market Cap",
                        value=f"{market_cap:,.2f} {fiat}",
                        inline=True,
                    )
                    embed.add_field(
                        name=f"24h Volume",
                        value=f"{day_volume:,.2f} {fiat}",
                        inline=True,
                    )
                    embed.add_field(
                        name="Percent Change (1h)", value=change_1h, inline=True
                    )
                    embed.add_field(
                        name="Percent Change (24h)", value=change_24h, inline=True
                    )
                    embed.add_field(
                        name="Percent Change (7d)", value=change_7d, inline=True
                    )

                    await interaction.response.send_message(embed=embed)
                else:
                    await interaction.response.send_message(
                        "Failed to fetch data from CoinMarketCap API. Please try again later.",
                        ephemeral=True,
                    )

    @commands.command(
        name="whois", help="Get WHOIS information about an IP address."
    )
    async def whois(self, ctx: commands.Context, ip_address: str):
        """Get information about an IP address."""

        ipv4_pattern = r"^(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$"

        ipv6_pattern = r"^(?:(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}|(?:[0-9a-fA-F]{1,4}:){1,7}:|(?:[0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}|(?:[0-9a-fA-F]{1,4}:){1,5}(?::[0-9a-fA-F]{1,4}){1,2}|(?:[0-9a-fA-F]{1,4}:){1,4}(?::[0-9a-fA-F]{1,4}){1,3}|(?:[0-9a-fA-F]{1,4}:){1,3}(?::[0-9a-fA-F]{1,4}){1,4}|(?:[0-9a-fA-F]{1,4}:){1,2}(?::[0-9a-fA-F]{1,4}){1,5}|[0-9a-fA-F]{1,4}:(?:(?::[0-9a-fA-F]{1,4}){1,6})|:(?:(?::[0-9a-fA-F]{1,4}){1,7}|:)|fe80:(?::[0-9a-fA-F]{0,4}){0,4}%[0-9a-zA-Z]{1,}|::(?:ffff(?::0{1,4}){0,1}:){0,1}(?:(?:25[0-5]|(?:2[0-4]|1{0,1}[0-9]){0,1}[0-9])\.){3,3}(?:25[0-5]|(?:2[0-4]|1{0,1}[0-9]){0,1}[0-9])|(?:[0-9a-fA-F]{1,4}:){1,4}:(?:(?:25[0-5]|(?:2[0-4]|1{0,1}[0-9]){0,1}[0-9])\.){3,3}(?:25[0-5]|(?:2[0-4]|1{0,1}[0-9]){0,1}[0-9]))$"

        if not (
            re.match(ipv4_pattern, ip_address) or re.match(ipv6_pattern, ip_address)
        ):
            await ctx.reply(
                "Invalid IP address format. Please enter a valid IPv4 or IPv6 address."
            )
            return

        async with borrowed() as session:
            async with session.get(f"https://ipapi.co/{ip_address}/json/") as response:
                if response.status == 429:
                    await Embeds.warning(ctx, "⚠️ API Rate limit reached. Please try again later.", delete_after=10, reply=True)
                    return
                if response.status != 200:
                    logger.error(
                        f"ipapi error: HTTP {response.status} for {ip_address}"
                    )
                    await Embeds.error(ctx, f"The IP lookup service returned an error (HTTP {response.status}). Please try again later.", title="⚠️ Lookup Failed", delete_after=10, reply=True)
                    return
                if response.status == 200:
                    data = await response.json()
                    if data.get("error"):
                        await ctx.reply(
                            f"Failed to fetch data for the provided IP address.",
                            delete_after=5,
                        )
                    else:
                        color = discord.Color.blurple()
                        if not isinstance(ctx.channel, discord.DMChannel) and ctx.author.top_role:
                            color = ctx.author.top_role.color
                        
                        embed = discord.Embed(
                            title=f"🌐 WHOIS Information - {ip_address}",
                            description=f"📍 **Location**: {data.get('city', 'N/A')}, {data.get('region', 'N/A')}, {data.get('country_name', 'N/A')}",
                            color=color,
                            timestamp=datetime.now(timezone.utc)
                        )
                        embed.set_thumbnail(url="https://cdn-icons-png.flaticon.com/512/149/149049.png")
                        
                        # Network Information
                        embed.add_field(
                            name="🔹 Network",
                            value=f"**Organization**: {data.get('org', 'N/A')}\n**ASN**: {data.get('asn', 'N/A')}",
                            inline=False
                        )
                        
                        # Location Details
                        embed.add_field(
                            name="📌 Details",
                            value=f"**Postal Code**: {data.get('postal', 'N/A')}\n**Timezone**: {data.get('timezone', 'N/A')}",
                            inline=True
                        )
                        
                        # Coordinates
                        embed.add_field(
                            name="🗺️ Coordinates",
                            value=f"**Latitude**: {data.get('latitude', 'N/A')}\n**Longitude**: {data.get('longitude', 'N/A')}",
                            inline=True
                        )
                        server_avatar_url = member.display_avatar.url if member.display_avatar else None
                        embed.set_footer(text="Data provided by ipapi.co", icon_url="https://ipapi.co/favicon.ico")
                        embed.set_author(name=f"Requested by {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)

                        await ctx.reply(embed=embed)
                else:
                    await ctx.reply(
                        "Failed to fetch WHOIS data. Please try again later.",
                        delete_after=5,
                    )

    @commands.command(
        name="remind", help="Set a reminder for a specific time."
    )
    async def remind(
        self, ctx: commands.Context, time: str, reminder: Optional[str] = None
    ):
        """Set a reminder for a specific time."""
        try:
            time = humanfriendly.parse_timespan(time)
            if time < 30:
                await Embeds.error(ctx, "Reminder time must be at least 30 seconds.", delete_after=5, reply=True)
                return
            if not isinstance(time, (int, float)) or str(time) == "nan":
                await Embeds.error(ctx, "Invalid time period. Please provide a valid time period.", delete_after=5, reply=True)
                return
        except humanfriendly.InvalidTimespan:
            await Embeds.error(ctx, "Invalid time period. Please provide a valid time period.", delete_after=5, reply=True)
            return

        safe_reminder = discord.utils.escape_mentions(reminder)

        embed1 = discord.Embed(
            description=f"Reminder set for {humanfriendly.format_timespan(time)} :saluting_face:"
        )
        await ctx.reply(embed=embed1)

        await asyncio.sleep(time)
        embed2 = discord.Embed(description=f"**REMINDER:**\n\n{safe_reminder}")
        await ctx.reply(embed=embed2)

    @commands.command(
        name="apod", description="Fetch NASA's Astronomy Picture of the Day."
    )
    @unified_cooldown(30)
    async def apod(self, ctx: commands.Context):
        """Fetches NASA's Astronomy Picture of the Day"""
        await ctx.reply(
            "Fetching NASA's Astronomy Picture of the Day...", delete_after=5
        )
        try:
            async with borrowed() as session:
                apod_url = f"https://api.nasa.gov/planetary/apod?api_key={NASA_API_KEY}"
                async with session.get(apod_url) as response:
                    rate_limit = response.headers.get("X-RateLimit-Limit")
                    rate_limit_remaining = response.headers.get("X-RateLimit-Remaining")

                    logger.debug(f"Rate Limit: {rate_limit}")
                    logger.debug(f"Rate Limit Remaining: {rate_limit_remaining}")

                    if (
                        rate_limit_remaining is not None
                        and int(rate_limit_remaining) < 10
                    ):
                        logger.warning(
                            "Approaching API rate limit. Consider slowing down requests."
                        )

                    status = response.status
                    data = await response.json()
            if status != 200:
                logger.error(
                    f"NASA APOD API error: HTTP {status} {data.get('msg', 'Unknown error')}"
                )
                await Embeds.error(ctx, f"The NASA API returned an error (HTTP {status}). Please try again later.", title="⚠️ NASA API Error", delete_after=10, reply=True)
                return

            apod_title = data.get("title", "No Title")
            explanation = data.get("explanation", "No explanation provided.")
            media_type = data.get("media_type", "image")
            url = data.get("url", "")
            hdurl = data.get("hdurl", "")
            date = data.get("date", "Unknown Date")

            color = (
                ctx.author.top_role.color
                if ctx.author.top_role
                else discord.Color.blurple()
            )

            # Components V2 layout: a media gallery handles both images and
            # videos (YouTube/Vimeo embeds unfurl as players), while a container
            # holds the title, explanation and any HD link.
            class APODContainer(discord.ui.Container):
                def __init__(self):
                    super().__init__(accent_color=color)

                    header = discord.ui.Section(
                        accessory=discord.ui.Thumbnail(
                            media="https://www.nasa.gov/wp-content/themes/nasa/assets/images/nasa-logo@2x.png"
                        )
                    )
                    header.add_item(
                        discord.ui.TextDisplay(
                            f"## {apod_title}\n"
                            f"### NASA Astronomy Picture of the Day\n"
                            f"-# {date}"
                        )
                    )
                    self.add_item(header)
                    self.add_item(discord.ui.Separator())

                    if media_type == "video":
                        self.add_item(
                            discord.ui.TextDisplay(
                                "-# Today's APOD is a video. It is shown above if the link is supported."
                            )
                        )

                    if explanation:
                        self.add_item(
                            discord.ui.TextDisplay(explanation[:4000])
                        )

                    if hdurl:
                        self.add_item(discord.ui.Separator())
                        row = discord.ui.ActionRow()
                        row.add_item(
                            discord.ui.Button(
                                label="View HD Image",
                                url=hdurl,
                            )
                        )
                        self.add_item(row)

            layout_view = discord.ui.LayoutView(timeout=None)

            if url:
                gallery = discord.ui.MediaGallery()
                gallery.add_item(media=url)
                layout_view.add_item(gallery)

            layout_view.add_item(APODContainer())

            message = await ctx.reply(view=layout_view)
            await message.add_reaction("👍")
            await asyncio.sleep(1)
            await message.add_reaction("👎")
        except aiohttp.ClientError as e:
            logger.exception(f"Error fetching APOD: {e}")
            await Embeds.error(ctx, "There was an error fetching from the NASA API.", title="Error", delete_after=5, reply=True)
        except Exception as e:
            logger.exception(f"Unexpected error: {e}")
            await Embeds.error(ctx, "An unexpected error occurred.", title="Error", delete_after=5, reply=True)

    @commands.command(
        name="quote", help="Quote a message, message link, or text."
    )
    async def quote(
        self, ctx: commands.Context, payload: Optional[str] = None
    ):
        """Quote a message or text."""
        try:
            msg = None
            quote_text = None
            author_name = None
            avatar_user = None

            # Check if this is a reply to a message (only available for prefix invocations)
            if ctx.message and ctx.message.reference and ctx.message.reference.message_id:
                try:
                    msg = await ctx.channel.fetch_message(
                        ctx.message.reference.message_id
                    )
                except discord.NotFound:
                    msg = None

            # If not a reply, or reply message not found, check for message link/ID
            if not msg and payload:
                raw = payload.strip()
                m = MESSAGE_LINK.match(raw)
                if m:
                    try:
                        ch = self.bot.get_channel(
                            int(m.group("channel"))
                        ) or await self.bot.fetch_channel(int(m.group("channel")))
                        msg = await ch.fetch_message(int(m.group("message")))
                    except (discord.NotFound, discord.Forbidden):
                        msg = None
                elif raw.isdigit():
                    try:
                        msg = await ctx.channel.fetch_message(int(raw))
                    except discord.NotFound:
                        msg = None

            if msg:
                quote_text = msg.content
                author_name = msg.author.display_name
                handle_name = msg.author.name
                avatar_user = msg.author
            elif payload:
                quote_text = payload
                author_name = ctx.author.display_name
                handle_name = ctx.author.name
                avatar_user = ctx.author
            else:
                await Embeds.error(ctx, "Please provide a message link, ID, reply to a message, or provide some text to quote.", title="Quote", delete_after=5, reply=True)
                return

            # `<@123>` renders as a meaningless number in an image, so swap
            # mentions for the names they point at before laying the text out.
            quote_text = await self._render_mentions(ctx, quote_text or "", msg)

            avatar_asset = self.utils.get_avatar_asset(avatar_user)
            avatar_bytes = await avatar_asset.read()
            avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")

            W, H = 800, 360
            AV = 360
            FADE_WIDTH = 130
            PAD = 20
            RECT_X = AV

            bg = Image.new("RGBA", (W, H), (0, 0, 0, 255))
            draw = ImageDraw.Draw(bg)

            avatar = avatar.resize((AV, AV), resample=self.RESAMPLE)
            mask = Image.new("L", (AV, AV), 255)
            mask_draw = ImageDraw.Draw(mask)
            for x in range(AV - FADE_WIDTH, AV):
                alpha = int(255 * (AV - x) / FADE_WIDTH)
                mask_draw.line([(x, 0), (x, AV)], fill=alpha)
            bg.paste(avatar, (0, 0), mask)

            text_area_w = W - RECT_X - PAD
            text_area_h = H - PAD * 2

            def tb_height(txt: str, font: ImageFont.FreeTypeFont) -> int:
                _, t, _, b = draw.textbbox((0, 0), txt, font=font)
                return b - t

            auth_h = tb_height("Ay", self.font_author) + 6
            handle_h = tb_height("Ay", self.font_handle) + 6

            if self.font_path:
                best_font = None
                best_lines = None

                for size in range(self.initial_quote_size, self.min_quote_size - 1, -1):
                    test_font = ImageFont.truetype(self.font_path, size)
                    lines = self.wrap_text(draw, quote_text, test_font, text_area_w)

                    line_h = tb_height("Ay", test_font) + 8
                    total_h = len(lines) * line_h + auth_h + handle_h

                    if total_h <= text_area_h:
                        best_font = test_font
                        best_lines = lines
                        break

                if not best_font:
                    best_font = ImageFont.truetype(self.font_path, self.min_quote_size)
                    best_lines = self.wrap_text(
                        draw, quote_text, best_font, text_area_w
                    )

            else:
                best_font = self.font_quote
                best_lines = self.wrap_text(draw, quote_text, best_font, text_area_w)

            font_quote = best_font
            lines = best_lines

            line_h = tb_height("Ay", font_quote) + 8
            total_text_h = len(lines) * line_h + auth_h + handle_h
            y_current = (H - total_text_h) // 2

            # Helper to detect emoji-ish characters (covers most common emoji ranges)
            def is_emoji_char(ch: str) -> bool:
                o = ord(ch)
                rngs = (
                    (0x1F300, 0x1F5FF),
                    (0x1F600, 0x1F64F),
                    (0x1F680, 0x1F6FF),
                    (0x1F700, 0x1F77F),
                    (0x2600, 0x26FF),
                    (0x2700, 0x27BF),
                    (0x1F900, 0x1F9FF),
                    (0x1FA70, 0x1FAFF),
                    (0x1F1E6, 0x1F1FF),  # regional indicator (flags)
                    (0xFE0F, 0xFE0F),  # variation selector
                    (0x200D, 0x200D),  # zero width joiner
                )
                return any(start <= o <= end for start, end in rngs)

            # Split a line into tokens where each token is either a run of emoji-like chars or non-emoji text
            def split_tokens(s: str):
                if not s:
                    return []
                tokens = []
                cur = s[0]
                cur_is_emoji = is_emoji_char(cur)
                for ch in s[1:]:
                    ch_is_emoji = is_emoji_char(ch)
                    # keep ZWJ/VSFE0 tied to emoji runs
                    if cur_is_emoji and (ch == "\u200d" or ch == "\ufe0f"):
                        cur += ch
                        continue
                    if ch_is_emoji == cur_is_emoji:
                        cur += ch
                    else:
                        tokens.append(cur)
                        cur = ch
                        cur_is_emoji = ch_is_emoji
                tokens.append(cur)
                return tokens

            # Pre-fetch emoji images (Twemoji) for unique emoji tokens found in the text
            unique_emoji_tokens = set()
            for ln in lines:
                for tok in split_tokens(ln):
                    # treat token as emoji token if its first character is emoji-like
                    if tok and is_emoji_char(tok[0]):
                        unique_emoji_tokens.add(tok)

            emoji_images = {}
            if unique_emoji_tokens:
                emoji_size = max(10, int(line_h * 0.95))  # target pixel size for emojis
                for token in unique_emoji_tokens:
                    # Build twemoji codepoint sequence for the token
                    cps = "-".join(f"{ord(c):x}" for c in token)
                    url = f"https://twemoji.maxcdn.com/v/latest/72x72/{cps}.png"
                    try:
                        async with self.session.get(url) as resp:
                            if resp.status == 200:
                                b = await resp.read()
                                try:
                                    img = Image.open(io.BytesIO(b)).convert("RGBA")
                                    img = img.resize(
                                        (emoji_size, emoji_size), resample=self.RESAMPLE
                                    )
                                    emoji_images[token] = img
                                except Exception:
                                    # If PIL can't open, skip this emoji
                                    pass
                    except Exception:
                        pass
                # Fallback: if some emoji tokens failed to fetch, they simply won't be rendered as images.

            # Draw each line, placing text and emoji images inline, center-aligned
            for line_txt in lines:
                # compute line width by summing segments widths
                tokens = split_tokens(line_txt)
                seg_widths = []
                for tok in tokens:
                    if tok and is_emoji_char(tok[0]) and tok in emoji_images:
                        seg_widths.append(emoji_images[tok].width)
                    else:
                        l, t, r, b = draw.textbbox((0, 0), tok, font=font_quote)
                        seg_widths.append(r - l)
                total_line_w = sum(seg_widths) + (len(tokens) - 1) * 2  # small spacing

                x_pos = RECT_X + (text_area_w - total_line_w) // 2

                # For text segments we keep the glow effect similar to before
                for tok, seg_w in zip(tokens, seg_widths):
                    if tok and is_emoji_char(tok[0]) and tok in emoji_images:
                        img = emoji_images[tok]
                        # paste emoji image
                        bg.alpha_composite(img, dest=(x_pos, y_current))
                        x_pos += img.width + 2
                    else:
                        # create glow for text segment
                        if tok.strip():
                            l, t, r, b = draw.textbbox((0, 0), tok, font=font_quote)
                            w_txt, h_txt = r - l, b - t
                            glow_pad = 8
                            glow_layer = Image.new(
                                "RGBA",
                                (w_txt + glow_pad * 2, h_txt + glow_pad * 2),
                                (0, 0, 0, 0),
                            )
                            glow_draw = ImageDraw.Draw(glow_layer)
                            glow_draw.text(
                                (glow_pad, glow_pad),
                                tok,
                                font=font_quote,
                                fill=(255, 255, 255, 255),
                            )
                            glow_layer = glow_layer.filter(
                                ImageFilter.GaussianBlur(radius=2)
                            )
                            bg.alpha_composite(
                                glow_layer,
                                dest=(x_pos - glow_pad, y_current - glow_pad),
                            )
                        draw.text(
                            (x_pos, y_current),
                            tok,
                            font=font_quote,
                            fill=(255, 255, 255),
                        )
                        x_pos += seg_w + 2

                y_current += line_h

            auth_str = f"— {author_name}"
            l, t, r, b = draw.textbbox((0, 0), auth_str, font=self.font_author)
            w_auth = r - l
            x_auth = RECT_X + (text_area_w - w_auth) // 2
            draw.text(
                (x_auth, y_current),
                auth_str,
                font=self.font_author,
                fill=(200, 200, 200),
            )
            y_current += auth_h

            # The account handle sits under the quoted display name, so the
            # card still identifies someone whose nickname doesn't match.
            handle_str = f"@{handle_name}"
            l, t, r, b = draw.textbbox((0, 0), handle_str, font=self.font_handle)
            w_handle = r - l
            x_handle = RECT_X + (text_area_w - w_handle) // 2
            draw.text(
                (x_handle, y_current),
                handle_str,
                font=self.font_handle,
                fill=(140, 140, 140),
            )
            y_current += handle_h

            date_str = discord.utils.utcnow().strftime("%d/%m/%Y")
            l, t, r, b = draw.textbbox((0, 0), date_str, font=self.font_date)
            w_date, h_date = r - l, b - t
            x_date = W - PAD - w_date
            y_date = H - PAD - h_date
            draw.text(
                (x_date, y_date), date_str, font=self.font_date, fill=(120, 120, 120)
            )

            out = io.BytesIO()
            bg.convert("RGB").save(out, format="JPEG", quality=90)
            out.seek(0)
            await ctx.reply(file=discord.File(out, "quote.jpg"))
        except discord.app_commands.errors.CommandOnCooldown as e:
            retry_after = e.retry_after
            await Embeds.warning(ctx, f"Please wait {retry_after:.1f} seconds before using this command again.", title="Cooldown", delete_after=5, reply=True)

    async def _render_mentions(
        self,
        ctx: commands.Context,
        text: str,
        message: Optional[discord.Message] = None,
    ) -> str:
        """Replace raw mention tokens with the names they point at.

        Quoted text arrives as `<@123>` / `<@&123>` / `<#123>`; drawn into an
        image those are just numbers, so this yields `@name`, `@role` and
        `#channel` instead.
        """
        if not text or "<" not in text:
            return text

        guild = getattr(message, "guild", None) or ctx.guild
        mentioned = {
            user.id: user for user in (getattr(message, "mentions", None) or [])
        }

        async def user_name(user_id: int) -> str:
            member = guild.get_member(user_id) if guild is not None else None
            if member is not None:
                return member.display_name
            user = mentioned.get(user_id) or self.bot.get_user(user_id)
            if user is None:
                try:
                    user = await self.bot.fetch_user(user_id)
                except discord.HTTPException:
                    return "user"
            return getattr(user, "display_name", None) or user.name

        pieces: List[str] = []
        cursor = 0
        for match in MENTION_PATTERN.finditer(text):
            pieces.append(text[cursor : match.start()])
            user_id, role_id, channel_id = match.groups()
            if user_id:
                pieces.append(f"@{await user_name(int(user_id))}")
            elif role_id:
                role = guild.get_role(int(role_id)) if guild is not None else None
                pieces.append(f"@{role.name}" if role is not None else "@role")
            else:
                channel = self.bot.get_channel(int(channel_id))
                pieces.append(f"#{channel.name}" if channel is not None else "#channel")
            cursor = match.end()
        pieces.append(text[cursor:])
        return "".join(pieces)

    def wrap_text(
        self,
        draw: ImageDraw.Draw,
        text: str,
        font: ImageFont.FreeTypeFont,
        max_width: int,
    ):
        """
        Word-wraps `text` so that each line's pixel width ≤ max_width,
        using draw.textbbox(...) to measure.
        """
        words = text.split()
        if not words:
            return []

        lines = []
        current_line = words[0]
        for word in words[1:]:
            test_line = current_line + " " + word
            l, t, r, b = draw.textbbox((0, 0), test_line, font=font)
            w = r - l
            if w <= max_width:
                current_line = test_line
            else:
                lines.append(current_line)
                current_line = word

        lines.append(current_line)
        return lines

    @commands.command(name="forgetme")
    async def forget_me(self, ctx):
        """
        Delete all personal/economy/game/social data for the invoking user.
        Requires confirming a PIN sent via DM. Server moderation records are
        retained for community safety and cannot be self-deleted.
        """

        user_id = ctx.author.id
        pin = f"{random.randint(1000, 9999)}"

        confirm_embed = discord.Embed(
            title="🗑️ Data Deletion Request",
            description=(
                "You have requested to delete all data stored by TPNEBOT.\n\n"
                "**This will delete:** your profile, economy balance, inventory, "
                "games history, favorite songs, timezone/location, reaction counts, "
                "role/name history, and any active cooldowns or effects.\n\n"
                "**This will NOT delete:** server moderation records such as bans, "
                "warnings, case notes, jail history, and audit logs. These are kept "
                "for community safety and anti-abuse purposes.\n\n"
                f"Type the following 4-digit PIN to confirm:\n\n`{pin}`\n\n"
                "You have 60 seconds."
            ),
            color=discord.Color.orange(),
        )

        try:
            dm_channel = await ctx.author.create_dm()
            await dm_channel.send(embed=confirm_embed)
        except (discord.Forbidden, discord.HTTPException) as e:
            logger.warning(f"Could not DM {ctx.author.id} for forgetme: {e}")
            await ctx.reply(
                "I couldn't send you a DM. Please enable DMs and try again.",
                delete_after=10,
            )
            return

        await ctx.reply(
            "I have sent you a confirmation message. Please check your DMs.",
            delete_after=10,
        )

        def check(m):
            return m.author.id == user_id and isinstance(m.channel, discord.DMChannel)

        try:
            msg = await self.bot.wait_for("message", timeout=60.0, check=check)
        except asyncio.TimeoutError:
            timeout_embed = discord.Embed(
                title="Timed Out",
                description="Confirmation timed out. Your data was **not** deleted.",
                color=discord.Color.red(),
            )
            try:
                await dm_channel.send(embed=timeout_embed)
            except (discord.Forbidden, discord.HTTPException):
                pass
            return

        if msg.content.strip() != pin:
            fail_embed = discord.Embed(
                title="Incorrect PIN",
                description="The PIN you entered was incorrect. Your data was **not** deleted.",
                color=discord.Color.red(),
            )
            await dm_channel.send(embed=fail_embed)
            return

        # Remove any in-memory AFK state before wiping the database.
        self.afk_users.pop(user_id, None)

        # Zero out economy balances so supply/circulating figures stay consistent
        # after the wallet row is deleted.
        try:
            member_wallet_id = await self.bot.database.get_wallet_id_for_user(user_id)
        except Exception:
            member_wallet_id = None

        if member_wallet_id is not None:
            try:
                bank_balance = Decimal(
                    str(await self.bot.database.get_bank_balance(member_wallet_id))
                )
                if bank_balance > 0:
                    await self.bot.database.withdraw_from_bank(
                        member_wallet_id,
                        bank_balance,
                        "Data deletion - bank balance returned to wallet",
                    )
            except Exception as e:
                logger.warning(f"forgetme bank reset failed for {user_id}: {e}")

            try:
                wallet_balance = Decimal(
                    str(await self.bot.database.get_wallet_balance(member_wallet_id))
                )
                if wallet_balance > 0:
                    await self.bot.database.spend_from_wallet(
                        wallet_id=member_wallet_id,
                        amount=wallet_balance,
                        description="Data deletion - wallet balance returned to treasury",
                    )
            except Exception as e:
                logger.warning(f"forgetme wallet reset failed for {user_id}: {e}")

        try:
            await self.bot.database.delete_all_data_for_user(user_id)
        except Exception as e:
            logger.exception(f"delete_all_data_for_user failed for {user_id}: {e}")
            error_embed = discord.Embed(
                title="Error",
                description="Something went wrong while deleting your data. Please contact a developer.",
                color=discord.Color.red(),
            )
            await dm_channel.send(embed=error_embed)
            return

        try:
            await self.bot.database.validate_economy()
        except Exception as e:
            logger.warning(f"forgetme validate_economy failed for {user_id}: {e}")

        logger.info(f"User {user_id} ({ctx.author.name}) used forgetme; data deleted.")

        success_embed = discord.Embed(
            title="Data Forgotten",
            description=(
                "Your personal, economy, game, and social data has been deleted.\n\n"
                "Server moderation records (bans, warnings, cases, jail history, audit logs) "
                "have been retained for community safety."
            ),
            color=discord.Color.green(),
        )
        await dm_channel.send(embed=success_embed)
        await ctx.reply(
            "Your data has been successfully deleted. Check your DMs for confirmation.",
            delete_after=10,
        )


async def setup(bot) -> None:
    await bot.add_cog(General(bot))
    logger.debug("General cog initialized successfully")
