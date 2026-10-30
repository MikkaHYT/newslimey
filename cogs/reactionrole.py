# cogs/reactionrole.py
#
# ,reactionrole builds an embed and attaches emoji reactions to it. Reacting
# grants the paired role; removing the reaction takes it back. The emoji/role
# pairs are stored in the ``reaction_roles`` table so messages posted before a
# restart keep working -- the raw-reaction listeners read the mapping back out
# of the database rather than holding it in memory.
#
# A stored mapping is only useful while the reactions are actually on the
# message, so on startup a background task re-adds any that are missing from
# messages posted by an earlier run.

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

import discord
from discord.ext import commands

from config import EMBED_COLOR, PREFIX

logger = logging.getLogger("slimey.reactionrole")

VIEW_TIMEOUT = 900
MAX_TITLE_LENGTH = 256
MAX_DESCRIPTION_LENGTH = 4000
MAX_FOOTER_LENGTH = 100
#: Most reactions a single reaction-role message may carry.
MAX_REACTIONS = 12

CHANNEL_TYPES = [
    discord.ChannelType.text,
    discord.ChannelType.news,
    discord.ChannelType.public_thread,
    discord.ChannelType.private_thread,
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def to_int_color(value) -> int:
    """Normalize an int or a discord.Colour into a plain int."""
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


def normalize_emoji(value: Optional[str]) -> Optional[str]:
    """Normalise a user-supplied emoji into the form stored for matching.

    Accepts a unicode emoji or a custom ``<:name:id>`` / ``<a:name:id>`` tag.
    Returns ``None`` for anything that isn't a usable emoji.
    """
    text = (value or "").strip()
    if not text:
        return None
    try:
        partial = discord.PartialEmoji.from_str(text)
    except Exception:
        return None
    # A custom emoji carries an id; a unicode one has a non-ascii name. Plain
    # words would "parse" but Discord rejects them as reactions.
    if partial.id is None and partial.name.isascii():
        return None
    return str(partial) or None


def check_author(interaction: discord.Interaction, author_id: int) -> bool:
    return interaction.user.id == author_id


NOT_AUTHOR_MESSAGE = "❌ Only the person who ran the command can use this."


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class ReactionRoleEntry:
    """One emoji -> role pairing on a reaction-role message."""

    emoji: str
    role_id: int


@dataclass
class ReactionRoleDraft:
    title: str = ""
    footer: str = ""
    color: int = field(default_factory=lambda: to_int_color(EMBED_COLOR))
    channel_id: Optional[int] = None
    entries: list[ReactionRoleEntry] = field(default_factory=list)

    def has_content(self) -> bool:
        return bool(self.title or self.entries)

    def description(self) -> str:
        """The generated emoji -> role list, one pairing per line."""
        return "\n".join(
            f"{entry.emoji}: <@&{entry.role_id}>" for entry in self.entries
        )

    def preview(self) -> discord.Embed:
        """The plain embed that gets posted to the target channel."""
        embed = discord.Embed(color=self.color)
        if self.title:
            embed.title = self.title[:MAX_TITLE_LENGTH]
        description = self.description()
        if description:
            embed.description = description[:4096]
        if self.footer:
            embed.set_footer(text=self.footer[:MAX_FOOTER_LENGTH])
        return embed


def rr_preview_items(container: discord.ui.Container, draft: ReactionRoleDraft) -> None:
    """Render the embed preview, then say how many reactions are set."""
    lines = []
    if draft.title:
        lines.append(f"### **{draft.title}**")
    description = draft.description()
    if description:
        lines.append(description[:MAX_DESCRIPTION_LENGTH])
    header_text = "\n".join(lines) if lines else "*Nothing set yet -- use Edit Text below.*"
    container.add_item(text_display(header_text))

    if draft.footer:
        container.add_item(small_separator())
        container.add_item(text_display(f"-# {draft.footer}"))

    container.add_item(small_separator())
    if draft.entries:
        container.add_item(
            text_display(f"**Reactions** ({len(draft.entries)}/{MAX_REACTIONS})")
        )
    else:
        container.add_item(
            text_display(
                f"**Reactions** (0/{MAX_REACTIONS})\n"
                "*No reactions added yet -- pick a role below.*"
            )
        )


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------

class ReactionRoleTextModal(discord.ui.Modal, title="Edit Text"):
    def __init__(
        self,
        *,
        cog: "ReactionRole",
        author_id: int,
        draft: ReactionRoleDraft,
        parent_view: "ReactionRoleView",
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.author_id = author_id
        self.draft = draft
        self.parent_view = parent_view

        self.title_input = discord.ui.TextInput(
            label="Title", style=discord.TextStyle.short, required=False,
            max_length=MAX_TITLE_LENGTH, default=draft.title,
        )
        self.footer_input = discord.ui.TextInput(
            label="Footer", style=discord.TextStyle.short, required=False,
            max_length=MAX_FOOTER_LENGTH, default=draft.footer,
        )
        self.add_item(self.title_input)
        self.add_item(self.footer_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.draft.title = self.title_input.value.strip()
        self.draft.footer = self.footer_input.value.strip()
        self.parent_view.stop()
        new_view = ReactionRoleView(self.cog, self.author_id, self.draft)
        await interaction.response.edit_message(view=new_view)
        new_view.message = interaction.message


class ReactionRoleEmojiModal(discord.ui.Modal, title="Reaction Emoji"):
    """Asks for the emoji once a role has been picked."""

    def __init__(
        self,
        *,
        cog: "ReactionRole",
        author_id: int,
        draft: ReactionRoleDraft,
        role: discord.Role,
        parent_view: "ReactionRoleView",
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.author_id = author_id
        self.draft = draft
        self.role = role
        self.parent_view = parent_view

        self.emoji_input = discord.ui.TextInput(
            label=f"Emoji for {role.name}",
            style=discord.TextStyle.short,
            required=True,
            max_length=100,
            placeholder="e.g. 📢 or <:name:id>",
        )
        self.add_item(self.emoji_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if len(self.draft.entries) >= MAX_REACTIONS:
            await interaction.response.send_message(
                f"❌ You can only have up to {MAX_REACTIONS} reactions.", ephemeral=True
            )
            return

        emoji = normalize_emoji(self.emoji_input.value)
        if emoji is None:
            await interaction.response.send_message(
                "❌ That isn't an emoji I can use. Try a unicode emoji or a "
                "custom `<:name:id>` emoji.",
                ephemeral=True,
            )
            return
        if any(entry.emoji == emoji for entry in self.draft.entries):
            await interaction.response.send_message(
                f"❌ {emoji} is already used on this message.", ephemeral=True
            )
            return

        self.draft.entries.append(ReactionRoleEntry(emoji=emoji, role_id=self.role.id))
        self.parent_view.stop()
        new_view = ReactionRoleView(self.cog, self.author_id, self.draft)
        await interaction.response.edit_message(view=new_view)
        new_view.message = interaction.message


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------

class ReactionRoleChannelPicker(discord.ui.ChannelSelect):
    def __init__(self, *, cog: "ReactionRole", author_id: int, draft: ReactionRoleDraft) -> None:
        self.cog = cog
        self.author_id = author_id
        self.draft = draft
        default_values = [discord.Object(id=draft.channel_id)] if draft.channel_id else None
        super().__init__(
            placeholder="Choose a channel to post in...",
            channel_types=CHANNEL_TYPES,
            min_values=1,
            max_values=1,
            default_values=default_values,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not check_author(interaction, self.author_id):
            await interaction.response.send_message(NOT_AUTHOR_MESSAGE, ephemeral=True)
            return
        self.draft.channel_id = self.values[0].id
        new_view = ReactionRoleView(self.cog, self.author_id, self.draft)
        await interaction.response.edit_message(view=new_view)
        new_view.message = interaction.message


class ReactionRoleRoleSelect(discord.ui.RoleSelect):
    """Picks the role a new reaction should grant."""

    def __init__(self, *, cog: "ReactionRole", author_id: int, draft: ReactionRoleDraft) -> None:
        self.cog = cog
        self.author_id = author_id
        self.draft = draft
        super().__init__(
            placeholder="Add a reaction role: pick the role...",
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not check_author(interaction, self.author_id):
            await interaction.response.send_message(NOT_AUTHOR_MESSAGE, ephemeral=True)
            return
        if len(self.draft.entries) >= MAX_REACTIONS:
            await interaction.response.send_message(
                f"❌ You can only have up to {MAX_REACTIONS} reactions.", ephemeral=True
            )
            return
        role = interaction.guild.get_role(self.values[0].id) if interaction.guild else None
        if role is None:
            await interaction.response.send_message(
                "❌ That role couldn't be found.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            ReactionRoleEmojiModal(
                cog=self.cog,
                author_id=self.author_id,
                draft=self.draft,
                role=role,
                parent_view=self.view,
            )
        )


class ReactionRoleActionButton(discord.ui.Button):
    """A button that runs one of the builder's actions after an author check."""

    def __init__(self, *, label: str, style: discord.ButtonStyle, author_id: int, action) -> None:
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

class ReactionRoleView(discord.ui.LayoutView):
    """The reaction-role builder: text, a target channel and emoji/role pairs."""

    def __init__(
        self,
        cog: "ReactionRole",
        author_id: int,
        draft: ReactionRoleDraft,
        timeout: int = VIEW_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.author_id = author_id
        self.draft = draft
        self.message: Optional[discord.Message] = None

        container = discord.ui.Container(accent_color=draft.color)
        container.add_item(
            text_display(
                "# Command: reactionrole\n"
                "Build the message, add up to "
                f"{MAX_REACTIONS} reactions, then hit **Post**.\n"
                "-# Members gain the role by reacting and lose it by unreacting."
            )
        )
        container.add_item(small_separator())
        rr_preview_items(container, draft)
        container.add_item(small_separator())
        container.add_item(
            discord.ui.ActionRow(
                ReactionRoleChannelPicker(cog=cog, author_id=author_id, draft=draft)
            )
        )
        container.add_item(
            discord.ui.ActionRow(
                ReactionRoleRoleSelect(cog=cog, author_id=author_id, draft=draft)
            )
        )
        container.add_item(
            discord.ui.ActionRow(
                ReactionRoleActionButton(
                    label="Edit Text", style=discord.ButtonStyle.primary,
                    author_id=author_id, action=self.open_text,
                ),
                ReactionRoleActionButton(
                    label="Remove Last", style=discord.ButtonStyle.secondary,
                    author_id=author_id, action=self.remove_last,
                ),
                ReactionRoleActionButton(
                    label="Post", style=discord.ButtonStyle.success,
                    author_id=author_id, action=self.post,
                ),
                ReactionRoleActionButton(
                    label="Reset", style=discord.ButtonStyle.secondary,
                    author_id=author_id, action=self.reset,
                ),
                ReactionRoleActionButton(
                    label="Cancel", style=discord.ButtonStyle.danger,
                    author_id=author_id, action=self.cancel,
                ),
            )
        )
        self.add_item(container)

    async def open_text(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            ReactionRoleTextModal(
                cog=self.cog,
                author_id=self.author_id,
                draft=self.draft,
                parent_view=self,
            )
        )

    async def remove_last(self, interaction: discord.Interaction) -> None:
        if not self.draft.entries:
            await interaction.response.send_message(
                "❌ There are no reactions to remove.", ephemeral=True
            )
            return
        self.draft.entries.pop()
        self.stop()
        new_view = ReactionRoleView(self.cog, self.author_id, self.draft)
        await interaction.response.edit_message(view=new_view)
        new_view.message = interaction.message

    async def reset(self, interaction: discord.Interaction) -> None:
        self.stop()
        new_view = ReactionRoleView(self.cog, self.author_id, ReactionRoleDraft())
        await interaction.response.edit_message(view=new_view)
        new_view.message = interaction.message

    async def cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(
            view=simple_view("❌ Reaction role builder cancelled.", timeout=10)
        )

    async def post(self, interaction: discord.Interaction) -> None:
        if not self.draft.has_content():
            await interaction.response.send_message(
                "❌ Add a title or at least one reaction before posting.", ephemeral=True
            )
            return
        if not self.draft.channel_id or not interaction.guild:
            await interaction.response.send_message(
                "❌ Pick a channel from the dropdown before posting.", ephemeral=True
            )
            return
        if not self.draft.entries:
            await interaction.response.send_message(
                "❌ Add at least one reaction-role pair before posting.", ephemeral=True
            )
            return

        channel = interaction.guild.get_channel_or_thread(self.draft.channel_id)
        if channel is None:
            await interaction.response.send_message(
                "❌ That channel couldn't be found. Pick a channel again.", ephemeral=True
            )
            return

        me = interaction.guild.me
        permissions = channel.permissions_for(me) if me else None
        if permissions is None or not permissions.send_messages:
            await interaction.response.send_message(
                f"❌ I don't have permission to send messages in {channel.mention}.",
                ephemeral=True,
            )
            return

        # Posting plus adding every reaction can take a moment, so acknowledge
        # before touching the network.
        await interaction.response.defer()
        try:
            message = await channel.send(embed=self.draft.preview())
        except discord.HTTPException:
            logger.exception("Discord rejected the reaction-role embed")
            await interaction.followup.send(
                "❌ Discord rejected that message -- double check the text.", ephemeral=True
            )
            return

        added: list[ReactionRoleEntry] = []
        for entry in self.draft.entries:
            try:
                await message.add_reaction(entry.emoji)
            except discord.HTTPException:
                logger.warning("Could not react with %s", entry.emoji)
                continue
            added.append(entry)

        if added:
            await self.cog.persist_reaction_roles(message, added)

        failed = len(self.draft.entries) - len(added)
        note = f" — {failed} reaction(s) couldn't be added." if failed else ""
        self.stop()
        await interaction.edit_original_response(
            view=simple_view(f"✅ Posted to {channel.mention}.{note}", timeout=10)
        )

    async def on_timeout(self) -> None:
        if not self.message:
            return
        try:
            await self.message.edit(
                view=simple_view(
                    "⏱️ Reaction role builder timed out. Run the command again to keep editing.",
                    timeout=10,
                )
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class ReactionRole(commands.Cog, name="ReactionRole"):
    """Reaction roles: reacting grants a role, unreacting removes it."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        #: Background task that re-adds missing reactions on startup.
        self._restore_task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        # The refbot database ships the table, but cogs can be enabled on a
        # running bot whose file predates it, so make sure it exists.
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS reaction_roles ("
            "message_id INTEGER NOT NULL, "
            "emoji TEXT NOT NULL, "
            "guild_id INTEGER NOT NULL, "
            "channel_id INTEGER NOT NULL, "
            "role_id INTEGER NOT NULL, "
            "PRIMARY KEY (message_id, emoji))"
        )
        # Don't block cog loading on network work; the restore waits for the
        # gateway and then runs in the background.
        self._restore_task = asyncio.create_task(self._restore_reaction_roles())

    def cog_unload(self) -> None:
        if self._restore_task is not None:
            self._restore_task.cancel()
            self._restore_task = None

    async def _restore_reaction_roles(self) -> None:
        """Re-add reactions to reaction-role messages posted before a restart.

        The emoji -> role mapping is read from the database at click time, but
        a message only works while its reactions are present. Reactions can go
        missing (cleared, removed, or a message the bot stopped watching), so
        once the gateway is ready every stored message is checked and anything
        absent is put back. Existing reactions are left untouched, and adds are
        spaced out to stay clear of Discord's reaction rate limit.
        """
        try:
            await self.bot.wait_until_ready()
        except Exception:
            return

        try:
            rows = await self.bot.db.execute(
                "SELECT message_id, channel_id, emoji FROM reaction_roles;"
            )
        except Exception:
            logger.exception("Could not load stored reaction roles for restoration")
            return

        by_message: dict[int, tuple[int, list[str]]] = {}
        for message_id, channel_id, emoji in rows:
            channel_and_emojis = by_message.setdefault(
                int(message_id), (int(channel_id), [])
            )
            channel_and_emojis[1].append(str(emoji))

        if not by_message:
            return

        restored = 0
        for message_id, (channel_id, emojis) in by_message.items():
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except discord.HTTPException:
                    continue

            try:
                message = await channel.fetch_message(message_id)
            except discord.NotFound:
                # The message is gone -- drop its rows rather than retry forever.
                await self._forget_reaction_roles(message_id)
                continue
            except discord.HTTPException:
                # Forbidden / transient: keep the rows so a later run can retry
                # once the bot can see the channel again.
                logger.debug("Could not fetch reaction-role message %s", message_id)
                continue

            existing = {str(reaction.emoji) for reaction in message.reactions}
            added = 0
            for emoji in emojis:
                if emoji in existing:
                    continue
                try:
                    await message.add_reaction(emoji)
                    added += 1
                except discord.HTTPException:
                    logger.warning(
                        "Could not restore reaction %s on message %s",
                        emoji,
                        message_id,
                    )
                await asyncio.sleep(0.25)

            if added:
                restored += 1

        if restored:
            logger.info(
                "Restored reactions on %d reaction-role message(s)", restored
            )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def persist_reaction_roles(
        self, message: discord.Message, entries: list[ReactionRoleEntry]
    ) -> None:
        """Remember which emoji grants which role on a posted message."""
        if message.guild is None:
            return
        for entry in entries:
            try:
                await self.bot.db.execute(
                    "INSERT OR REPLACE INTO reaction_roles "
                    "(message_id, emoji, guild_id, channel_id, role_id) "
                    "VALUES (%s, %s, %s, %s, %s);",
                    message.id,
                    entry.emoji,
                    message.guild.id,
                    message.channel.id,
                    entry.role_id,
                )
            except Exception:
                logger.exception("Could not persist reaction role %s", entry.emoji)

    async def _reaction_roles(self, message_id: int) -> dict[str, int]:
        """``emoji -> role id`` for a message, empty when it has none."""
        try:
            rows = await self.bot.db.execute(
                "SELECT emoji, role_id FROM reaction_roles WHERE message_id = %s;",
                message_id,
            )
        except Exception:
            logger.exception("Could not load reaction roles for %s", message_id)
            return {}
        return {str(row[0]): int(row[1]) for row in rows}

    async def _forget_reaction_roles(self, message_id: int) -> None:
        try:
            await self.bot.db.execute(
                "DELETE FROM reaction_roles WHERE message_id = %s;", message_id
            )
        except Exception:
            logger.exception("Could not clear reaction roles for %s", message_id)

    # ------------------------------------------------------------------
    # Listeners
    # ------------------------------------------------------------------

    async def _toggle_reaction_role(self, payload, *, add: bool) -> None:
        """Grant or revoke the role tied to the reaction a member used."""
        if payload.guild_id is None:
            return
        if self.bot.user is not None and payload.user_id == self.bot.user.id:
            return

        mapping = await self._reaction_roles(payload.message_id)
        if not mapping:
            # A message with no stored mapping can never grant roles -- most
            # often because it was posted before this feature (or before the
            # port) and its emoji -> role pairs were never persisted. Say so,
            # at debug level, so the cause is visible without spamming logs.
            logger.debug(
                "Reaction on message %s in channel %s has no stored reaction-role "
                "mapping; re-post it with %sreactionrole to make it work.",
                payload.message_id,
                payload.channel_id,
                PREFIX,
            )
            return
        role_id = mapping.get(str(payload.emoji))
        if role_id is None:
            logger.debug(
                "Reaction %r on message %s does not match any stored emoji (%s).",
                str(payload.emoji),
                payload.message_id,
                ", ".join(repr(key) for key in mapping),
            )
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        role = guild.get_role(role_id)
        if role is None or role.is_default():
            logger.warning(
                "Reaction role %s for message %s no longer exists or is @everyone.",
                role_id,
                payload.message_id,
            )
            return

        member = getattr(payload, "member", None) or guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except discord.HTTPException:
                return
        if member.bot:
            return

        try:
            if add:
                if role not in member.roles:
                    await member.add_roles(role, reason="Reaction role")
            elif role in member.roles:
                await member.remove_roles(role, reason="Reaction role")
        except discord.Forbidden:
            logger.warning("Missing permissions to toggle role %s", role_id)
        except discord.HTTPException:
            logger.exception("Could not toggle role %s", role_id)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self._toggle_reaction_role(payload, add=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        await self._toggle_reaction_role(payload, add=False)

    @commands.Cog.listener()
    async def on_raw_message_delete(
        self, payload: discord.RawMessageDeleteEvent
    ) -> None:
        await self._forget_reaction_roles(payload.message_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(
        self, payload: discord.RawBulkMessageDeleteEvent
    ) -> None:
        for message_id in payload.message_ids:
            await self._forget_reaction_roles(message_id)

    # ------------------------------------------------------------------
    # Command
    # ------------------------------------------------------------------

    @commands.command(
        name="reactionrole",
        aliases=["reactionroles", "rrole", "createrr"],
    )
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_roles=True, add_reactions=True)
    async def reactionrole(self, ctx: commands.Context) -> None:
        f"""Build an embed where reacting gives a role and unreacting removes it.

        Set a title and footer if you want, then pick the role and the emoji for
        each reaction (up to {MAX_REACTIONS} per message). The description is
        built automatically as `emoji: @role` lines so members can see which role
        each reaction gives. When someone reacts they get the role; when they
        remove the reaction the role is taken back. Usage: `{PREFIX}reactionrole`
        """
        if ctx.guild is None:
            await ctx.send(
                view=simple_view("❌ This command can only be used in a server.")
            )
            return

        draft = ReactionRoleDraft()
        view = ReactionRoleView(self, ctx.author.id, draft)
        view.message = await ctx.send(view=view)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ReactionRole(bot))
