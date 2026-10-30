# cogs/welcome.py
#
# ,welcome opens an embed builder for the server's welcome message. The saved
# message is posted when someone joins: the member is pinged in the message
# content and the embed rides in the same message.
#
# Text supports {user} (mention), {name}, {username}, {server}, {id} and
# {membercount}, so a footer can read ``Member: {membercount}`` and come out as
# ``Member: 503``.

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import discord
from discord.ext import commands

from config import EMBED_COLOR, PREFIX

logger = logging.getLogger("slimey.welcome")

#: ``bot_settings`` key holding the per-guild welcome config (JSON).
SETTINGS_KEY = "welcome_message"

VIEW_TIMEOUT = 900
MAX_TITLE_LENGTH = 256
MAX_DESCRIPTION_LENGTH = 4000
MAX_FOOTER_LENGTH = 100

CHANNEL_TYPES = [
    discord.ChannelType.text,
    discord.ChannelType.news,
    discord.ChannelType.public_thread,
    discord.ChannelType.private_thread,
]

PLACEHOLDER_HELP = (
    "`{user}` mention · `{name}` display name · `{username}` username · "
    "`{server}` server name · `{membercount}` join position · `{id}` user id"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def to_int_color(value) -> int:
    if isinstance(value, discord.Colour):
        return value.value
    return int(value)


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


def parse_hex_color(value: str) -> Optional[int]:
    value = (value or "").strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        return None
    return int(value, 16)


def render_placeholders(text: str, member: discord.abc.User, guild: discord.Guild) -> str:
    """Swap the supported `{...}` tokens for values for this member."""
    if not text:
        return ""
    count = guild.member_count or 0
    mention = getattr(member, "mention", None) or member.display_name
    return (
        text.replace("{user}", mention)
        .replace("{mention}", mention)
        .replace("{name}", member.display_name)
        .replace("{display_name}", member.display_name)
        .replace("{username}", member.name)
        .replace("{server}", guild.name)
        .replace("{membercount}", f"{count:,}")
        .replace("{count}", f"{count:,}")
        .replace("{id}", str(member.id))
    )


@dataclass
class WelcomeConfig:
    channel_id: Optional[int] = None
    title: str = ""
    description: str = ""
    footer: str = ""
    color: int = field(default_factory=lambda: to_int_color(EMBED_COLOR))
    ping: bool = True

    def to_dict(self) -> dict:
        return {
            "channel_id": self.channel_id,
            "title": self.title,
            "description": self.description,
            "footer": self.footer,
            "color": self.color,
            "ping": self.ping,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WelcomeConfig":
        return cls(
            channel_id=data.get("channel_id"),
            title=data.get("title") or "",
            description=data.get("description") or "",
            footer=data.get("footer") or "",
            color=int(data.get("color") or to_int_color(EMBED_COLOR)),
            ping=bool(data.get("ping", True)),
        )

    def has_content(self) -> bool:
        return bool(self.title or self.description)


def build_welcome_embed(
    config: WelcomeConfig, member: discord.abc.User, guild: discord.Guild
) -> discord.Embed:
    """The embed that is posted with the ping when a member joins."""
    embed = discord.Embed(color=config.color)
    if config.title:
        embed.title = render_placeholders(config.title, member, guild)[:MAX_TITLE_LENGTH]
    if config.description:
        embed.description = render_placeholders(
            config.description, member, guild
        )[:MAX_DESCRIPTION_LENGTH]
    if config.footer:
        embed.set_footer(
            text=render_placeholders(config.footer, member, guild)[:MAX_FOOTER_LENGTH]
        )
    return embed


def preview_items(
    container: discord.ui.Container,
    config: WelcomeConfig,
    member: discord.abc.User,
    guild: discord.Guild,
) -> None:
    """Render a preview of the ping + embed inside the builder."""
    if config.ping:
        container.add_item(text_display(member.mention))
    lines = []
    if config.title:
        lines.append(f"### **{render_placeholders(config.title, member, guild)}**")
    if config.description:
        lines.append(
            render_placeholders(config.description, member, guild)[:MAX_DESCRIPTION_LENGTH]
        )
    container.add_item(
        text_display(
            "\n".join(lines) if lines else "*Nothing set yet — use **Edit Text** below.*"
        )
    )
    if config.footer:
        container.add_item(small_separator())
        container.add_item(
            text_display(f"-# {render_placeholders(config.footer, member, guild)}")
        )


def check_author(interaction: discord.Interaction, author_id: int) -> bool:
    return interaction.user.id == author_id


NOT_AUTHOR_MESSAGE = "❌ Only the person who ran the command can use this."


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------

class WelcomeTextModal(discord.ui.Modal, title="Edit Welcome Text"):
    def __init__(
        self,
        *,
        cog: "Welcome",
        guild: discord.Guild,
        author: discord.abc.User,
        author_id: int,
        config: WelcomeConfig,
        parent_view: "WelcomeView",
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.guild = guild
        self.author = author
        self.author_id = author_id
        self.config = config
        self.parent_view = parent_view

        self.title_input = discord.ui.TextInput(
            label="Title", style=discord.TextStyle.short, required=False,
            max_length=MAX_TITLE_LENGTH, default=config.title,
        )
        self.description_input = discord.ui.TextInput(
            label="Description", style=discord.TextStyle.paragraph, required=False,
            max_length=MAX_DESCRIPTION_LENGTH, default=config.description,
        )
        self.footer_input = discord.ui.TextInput(
            label="Footer", style=discord.TextStyle.short, required=False,
            max_length=MAX_FOOTER_LENGTH, default=config.footer,
            placeholder="e.g. Member: {membercount}",
        )
        self.add_item(self.title_input)
        self.add_item(self.description_input)
        self.add_item(self.footer_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.config.title = self.title_input.value.strip()
        self.config.description = self.description_input.value.strip()
        self.config.footer = self.footer_input.value.strip()
        self.parent_view.stop()
        new_view = WelcomeView(
            self.cog, self.guild, self.author, self.author_id, self.config
        )
        await interaction.response.edit_message(view=new_view)
        new_view.message = interaction.message


class WelcomeColorModal(discord.ui.Modal, title="Welcome Embed Colour"):
    def __init__(
        self,
        *,
        cog: "Welcome",
        guild: discord.Guild,
        author: discord.abc.User,
        author_id: int,
        config: WelcomeConfig,
        parent_view: "WelcomeView",
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.guild = guild
        self.author = author
        self.author_id = author_id
        self.config = config
        self.parent_view = parent_view

        self.color_input = discord.ui.TextInput(
            label="Hex colour", style=discord.TextStyle.short, required=True,
            max_length=7, default=f"#{config.color:06X}", placeholder="#5865F2",
        )
        self.add_item(self.color_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        parsed = parse_hex_color(self.color_input.value)
        if parsed is None:
            await interaction.response.send_message(
                "❌ Use a hex colour like `#5865F2`.", ephemeral=True
            )
            return
        self.config.color = parsed
        self.parent_view.stop()
        new_view = WelcomeView(
            self.cog, self.guild, self.author, self.author_id, self.config
        )
        await interaction.response.edit_message(view=new_view)
        new_view.message = interaction.message


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------

class WelcomeChannelPicker(discord.ui.ChannelSelect):
    def __init__(
        self,
        *,
        cog: "Welcome",
        guild: discord.Guild,
        author: discord.abc.User,
        author_id: int,
        config: WelcomeConfig,
    ) -> None:
        self.cog = cog
        self.guild = guild
        self.author = author
        self.author_id = author_id
        self.config = config
        default_values = (
            [discord.Object(id=config.channel_id)] if config.channel_id else None
        )
        super().__init__(
            placeholder="Choose the welcome channel...",
            channel_types=CHANNEL_TYPES,
            min_values=1,
            max_values=1,
            default_values=default_values,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not check_author(interaction, self.author_id):
            await interaction.response.send_message(NOT_AUTHOR_MESSAGE, ephemeral=True)
            return
        self.config.channel_id = self.values[0].id
        new_view = WelcomeView(
            self.cog, self.guild, self.author, self.author_id, self.config
        )
        await interaction.response.edit_message(view=new_view)
        new_view.message = interaction.message


class WelcomeActionButton(discord.ui.Button):
    """A builder button that checks the author before running its action."""

    def __init__(
        self, *, label: str, style: discord.ButtonStyle, author_id: int, action
    ) -> None:
        super().__init__(label=label, style=style)
        self.author_id = author_id
        self._action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        if not check_author(interaction, self.author_id):
            await interaction.response.send_message(NOT_AUTHOR_MESSAGE, ephemeral=True)
            return
        await self._action(interaction)


# ---------------------------------------------------------------------------
# View
# ---------------------------------------------------------------------------

class WelcomeView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: "Welcome",
        guild: discord.Guild,
        author: discord.abc.User,
        author_id: int,
        config: WelcomeConfig,
        timeout: int = VIEW_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.guild = guild
        self.author = author
        self.author_id = author_id
        self.config = config
        self.message: Optional[discord.Message] = None

        channel = (
            guild.get_channel_or_thread(config.channel_id)
            if config.channel_id
            else None
        )
        container = discord.ui.Container(accent_color=config.color)
        container.add_item(
            text_display(
                "# Welcome message\n"
                "The member is pinged and this embed is posted with it.\n"
                f"-# {PLACEHOLDER_HELP}"
            )
        )
        container.add_item(small_separator())
        preview_items(container, config, author, guild)
        container.add_item(small_separator())
        container.add_item(
            text_display(
                f"**Channel** · {channel.mention if channel else '*not set*'}\n"
                f"**Ping** · {'on' if config.ping else 'off'}"
            )
        )
        container.add_item(
            discord.ui.ActionRow(
                WelcomeChannelPicker(
                    cog=cog,
                    guild=guild,
                    author=author,
                    author_id=author_id,
                    config=config,
                )
            )
        )
        container.add_item(
            discord.ui.ActionRow(
                WelcomeActionButton(
                    label="Edit Text", style=discord.ButtonStyle.primary,
                    author_id=author_id, action=self.open_text,
                ),
                WelcomeActionButton(
                    label="Colour", style=discord.ButtonStyle.secondary,
                    author_id=author_id, action=self.open_color,
                ),
                WelcomeActionButton(
                    label="Ping: On" if config.ping else "Ping: Off",
                    style=discord.ButtonStyle.secondary,
                    author_id=author_id, action=self.toggle_ping,
                ),
                WelcomeActionButton(
                    label="Save", style=discord.ButtonStyle.success,
                    author_id=author_id, action=self.save,
                ),
                WelcomeActionButton(
                    label="Cancel", style=discord.ButtonStyle.danger,
                    author_id=author_id, action=self.cancel,
                ),
            )
        )
        self.add_item(container)

    async def open_text(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            WelcomeTextModal(
                cog=self.cog,
                guild=self.guild,
                author=self.author,
                author_id=self.author_id,
                config=self.config,
                parent_view=self,
            )
        )

    async def open_color(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            WelcomeColorModal(
                cog=self.cog,
                guild=self.guild,
                author=self.author,
                author_id=self.author_id,
                config=self.config,
                parent_view=self,
            )
        )

    async def toggle_ping(self, interaction: discord.Interaction) -> None:
        self.config.ping = not self.config.ping
        self.stop()
        new_view = WelcomeView(
            self.cog, self.guild, self.author, self.author_id, self.config
        )
        await interaction.response.edit_message(view=new_view)
        new_view.message = interaction.message

    async def save(self, interaction: discord.Interaction) -> None:
        if not self.config.channel_id:
            await interaction.response.send_message(
                "❌ Pick a channel before saving.", ephemeral=True
            )
            return
        if not self.config.has_content():
            await interaction.response.send_message(
                "❌ Add a title or description before saving.", ephemeral=True
            )
            return
        await self.cog.save_config(self.guild.id, self.config)
        self.stop()
        channel = self.guild.get_channel_or_thread(self.config.channel_id)
        await interaction.response.edit_message(
            view=simple_view(
                "✅ Welcome message saved.\n"
                f"It will post in {channel.mention if channel else 'the chosen channel'} "
                "when someone joins.",
                timeout=10,
            )
        )

    async def cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(
            view=simple_view("❌ Welcome builder cancelled.", timeout=10)
        )

    async def on_timeout(self) -> None:
        if not self.message:
            return
        try:
            await self.message.edit(
                view=simple_view(
                    "⏱️ Welcome builder timed out. Run the command again to keep editing.",
                    timeout=10,
                )
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class Welcome(commands.Cog, name="Welcome"):
    """Ping plus embed posted when a member joins."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def load_config(self, guild_id: int) -> Optional[WelcomeConfig]:
        settings = getattr(self.bot, "dash_settings", None)
        if settings is None:
            return None
        raw = settings.stored(SETTINGS_KEY, guild_id=guild_id)
        if not raw:
            return None
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                return None
        return WelcomeConfig.from_dict(raw) if isinstance(raw, dict) else None

    async def save_config(self, guild_id: int, config: WelcomeConfig) -> None:
        settings = getattr(self.bot, "dash_settings", None)
        payload = config.to_dict()
        if settings is not None:
            await settings.set_value(SETTINGS_KEY, payload, guild_id=guild_id)
            return
        await self.bot.db.execute(
            "INSERT INTO bot_settings (guild_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value;",
            guild_id,
            SETTINGS_KEY,
            json.dumps(payload),
        )

    # ------------------------------------------------------------------
    # Join
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        guild = member.guild
        if guild is None:
            return
        config = self.load_config(guild.id)
        if config is None or not config.channel_id or not config.has_content():
            return

        channel = guild.get_channel_or_thread(config.channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return

        embed = build_welcome_embed(config, member, guild)
        content = member.mention if config.ping else None
        try:
            await channel.send(
                content=content,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.Forbidden:
            logger.debug("No permission to welcome in guild %s", guild.id)
        except discord.HTTPException:
            logger.exception("Could not post the welcome message in guild %s", guild.id)

    # ------------------------------------------------------------------
    # Command
    # ------------------------------------------------------------------

    @commands.command(
        name="welcome",
        aliases=["welcomemessage", "welcomebuilder", "welcomemsg"],
        description="Build the welcome message posted when someone joins.",
    )
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @commands.bot_has_permissions(send_messages=True, embed_links=True)
    async def welcome(self, ctx: commands.Context) -> None:
        f"""Build the welcome ping + embed.

        The member is pinged in the message and the embed is sent in the same
        message. Title, description and footer support `{{user}}`, `{{name}}`,
        `{{username}}`, `{{server}}` and `{{membercount}}`; a footer of
        ``Member: {{membercount}}`` shows which member they are.
        Usage: `{PREFIX}welcome`
        """
        if ctx.guild is None:
            await ctx.send(
                view=simple_view("❌ This command can only be used in a server.")
            )
            return

        config = self.load_config(ctx.guild.id) or WelcomeConfig(
            channel_id=ctx.channel.id
        )
        view = WelcomeView(self, ctx.guild, ctx.author, ctx.author.id, config)
        view.message = await ctx.send(view=view)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Welcome(bot))
