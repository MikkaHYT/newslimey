# cogs/createembed.py
#
# ,createembed (aliases: embedbuilder, buildembed, ce) builds a message with
# components v2. Run on its own it opens the interactive builder ported from the
# reference bot. Given a script it builds the embed directly, e.g.
#
#   ,ce {title: Time After Time}$v{description: BINGEDRINKINGMUSIC}$v
#       {footer: {guild.name} && {guild.icon}}$v{thumbnail: {guild.icon}}$v
#       {button: https://example.com && 🎵 WAV File && enabled}
#
# Fields are separated by ``$v`` and written as ``{key: value}``. Button values
# are ``url && label && enabled|disabled``. An image-generation button is
# ``{imagebutton: <prompt> && <label> && <colour>}``, where the colour is one of
# ``primary``/``secondary``/``success``/``danger`` (or a colour name such as
# ``red``/``blue``); clicking it generates an image. ``{placeholder}``
# tokens such as ``{guild.name}`` and ``{guild.icon}`` are resolved against the
# invocation.

from __future__ import annotations

import io
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import discord
from discord.ext import commands

from config import EMBED_COLOR, PREFIX
from utils.core.images import generate_image

logger = logging.getLogger("slimey.createembed")

VIEW_TIMEOUT = 900
MAX_BUTTONS = 5
MAX_TITLE_LENGTH = 256
MAX_DESCRIPTION_LENGTH = 4000
MAX_FOOTER_LENGTH = 100
MAX_BUTTON_LABEL_LENGTH = 80

BUTTON_STYLES = {
    "link": discord.ButtonStyle.link,
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
}

#: Style codes plus friendly colour names, so a script may say `danger` or `red`.
BUTTON_STYLE_ALIASES = {
    "link": "link",
    "primary": "primary",
    "blurple": "primary",
    "blue": "primary",
    "secondary": "secondary",
    "grey": "secondary",
    "gray": "secondary",
    "success": "success",
    "green": "success",
    "danger": "danger",
    "red": "danger",
}

#: Label used by an image button whose script did not give one.
IMAGE_BUTTON_DEFAULT_LABEL = "🖼️ Generate Image"
#: Longest prompt stored on an image button.
IMAGE_BUTTON_MAX_PROMPT_LENGTH = 1000


def normalize_button_style(value: str) -> Optional[str]:
    """Map a button colour code or colour name to a known style, or ``None``."""
    return BUTTON_STYLE_ALIASES.get((value or "").strip().lower())

CHANNEL_TYPES = [
    discord.ChannelType.text,
    discord.ChannelType.news,
    discord.ChannelType.public_thread,
    discord.ChannelType.private_thread,
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def valid_http_url(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    try:
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return None
    except Exception:
        return None
    return value


def safe_emoji(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        discord.PartialEmoji.from_str(value)
    except Exception:
        return None
    return value


def to_int_color(value) -> int:
    """Normalize an int or a discord.Colour/Color into a plain int."""
    if isinstance(value, discord.Colour):
        return value.value
    return int(value)


def parse_hex_color(value: str) -> Optional[int]:
    value = value.strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        return None
    return int(value, 16)


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


def apply_url_field(new_value: str, current: Optional[str], label: str, warnings: list[str]) -> Optional[str]:
    new_value = new_value.strip()
    if not new_value:
        return None
    if valid_http_url(new_value):
        return new_value
    warnings.append(label)
    return current


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class ButtonSpec:
    style: str = "link"
    label: str = "Button"
    emoji: Optional[str] = None
    url: Optional[str] = None
    response: Optional[str] = None
    role_id: Optional[int] = None
    #: When set, clicking the button generates and posts an image of this prompt.
    image_prompt: Optional[str] = None

    def is_role_button(self) -> bool:
        return self.role_id is not None

    def is_image_button(self) -> bool:
        return bool(self.image_prompt)


@dataclass
class EmbedDraft:
    title: str = ""
    description: str = ""
    footer: str = ""
    color: int = field(default_factory=lambda: to_int_color(EMBED_COLOR))
    image_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    video_url: Optional[str] = None
    channel_id: Optional[int] = None
    buttons: list[ButtonSpec] = field(default_factory=list)

    def has_content(self) -> bool:
        return bool(self.title or self.description or self.image_url or self.video_url)


# ---------------------------------------------------------------------------
# Shared rendering
# ---------------------------------------------------------------------------

def add_preview_items(container: discord.ui.Container, draft: EmbedDraft) -> None:
    lines = []
    if draft.title:
        lines.append(f"### **{draft.title}**")
    if draft.description:
        lines.append(draft.description[:MAX_DESCRIPTION_LENGTH])
    header_text = "\n".join(lines) if lines else "*Nothing set yet -- use Edit Text below.*"

    if draft.thumbnail_url:
        container.add_item(
            discord.ui.Section(
                text_display(header_text),
                accessory=discord.ui.Thumbnail(media=draft.thumbnail_url),
            )
        )
    else:
        container.add_item(text_display(header_text))

    media_items = []
    if draft.image_url:
        media_items.append(discord.ui.MediaGalleryItem(media=draft.image_url))
    if draft.video_url:
        media_items.append(discord.ui.MediaGalleryItem(media=draft.video_url))
    if media_items:
        container.add_item(discord.ui.MediaGallery(*media_items))

    if draft.footer:
        container.add_item(small_separator())
        container.add_item(text_display(f"-# {draft.footer}"))


def make_button_ids(draft: EmbedDraft) -> dict[str, str]:
    """Deterministic custom_ids for a draft's non-link buttons (string index -> id)."""
    token = uuid.uuid4().hex[:12]
    return {
        str(index): f"createembed:{token}:{index}"
        for index, spec in enumerate(draft.buttons[:MAX_BUTTONS])
        if spec.style != "link"
    }


def build_button_view(entries: list[dict]) -> discord.ui.View:
    """Rebuild a persistent view from stored button rows (for restart recovery)."""
    view: discord.ui.View = discord.ui.View(timeout=None)
    for entry in entries:
        spec = ButtonSpec(
            style=entry.get("style", "secondary"),
            label=entry.get("label", "Button"),
            emoji=entry.get("emoji"),
            response=entry.get("response"),
            role_id=entry.get("role_id"),
            image_prompt=entry.get("image_prompt"),
        )
        view.add_item(build_custom_button(spec, custom_id=entry["custom_id"]))
    return view


def build_custom_button(
    spec: ButtonSpec,
    *,
    preview: bool = False,
    custom_id: Optional[str] = None,
) -> discord.ui.Button:
    """Build the real Discord button for a :class:`ButtonSpec`.

    Role buttons toggle the configured role for whoever clicks them, image
    buttons generate and post an image of their prompt, and any other non-link
    button just replies with its configured text.
    """
    if spec.style == "link":
        return discord.ui.Button(
            label=spec.label,
            style=discord.ButtonStyle.link,
            url=spec.url,
            emoji=spec.emoji,
        )

    # Non-link buttons need a custom_id so they can be dispatched (and
    # re-registered as a persistent view via bot.add_view). Posted buttons get
    # a deterministic id so the same id can be restored after a restart.
    if custom_id is None:
        custom_id = (
            f"createembed:role:{spec.role_id}:{uuid.uuid4().hex[:8]}"
            if spec.is_role_button()
            else f"createembed:{uuid.uuid4().hex}"
        )
    button: discord.ui.Button = discord.ui.Button(
        label=spec.label,
        style=BUTTON_STYLES.get(spec.style, discord.ButtonStyle.secondary),
        emoji=spec.emoji,
        custom_id=custom_id,
    )

    if spec.is_image_button():
        prompt = spec.image_prompt or ""

        async def image_callback(
            interaction: discord.Interaction, image_prompt: str = prompt
        ) -> None:
            if preview:
                await interaction.response.send_message(
                    "This is just a preview -- post the embed to generate the image.",
                    ephemeral=True,
                )
                return

            # Image generation can take a few seconds, so defer before waiting.
            # The defer is public so the finished image posts in the channel for
            # everyone, not just the person who clicked.
            await interaction.response.defer(thinking=True)
            image, error = await generate_image(image_prompt)
            if image is None:
                await interaction.followup.send(
                    f"❌ I couldn't generate that image: {error}.", ephemeral=True
                )
                return
            await interaction.followup.send(
                file=discord.File(io.BytesIO(image.data), filename=image.filename)
            )

        button.callback = image_callback
        return button

    if spec.is_role_button():
        role_id = spec.role_id

        async def role_callback(interaction: discord.Interaction) -> None:
            if preview:
                await interaction.response.send_message(
                    "This is just a preview -- post the embed to use it.",
                    ephemeral=True,
                )
                return

            guild = interaction.guild
            role = guild.get_role(role_id) if guild is not None else None
            if role is None:
                await interaction.response.send_message(
                    "❌ That role no longer exists.", ephemeral=True
                )
                return

            member = interaction.user
            try:
                if role in getattr(member, "roles", []):
                    await member.remove_roles(role, reason="Embed role button")
                    note = f"✅ Removed {role.mention}."
                else:
                    await member.add_roles(role, reason="Embed role button")
                    note = f"✅ Added {role.mention}."
            except discord.Forbidden:
                await interaction.response.send_message(
                    "❌ I can't manage that role -- it may be higher than my own.",
                    ephemeral=True,
                )
                return
            except discord.HTTPException:
                await interaction.response.send_message(
                    "❌ Discord rejected that role change.", ephemeral=True
                )
                return

            await interaction.response.send_message(note, ephemeral=True)

        button.callback = role_callback
        return button

    async def callback(interaction: discord.Interaction, response: Optional[str] = spec.response) -> None:
        await interaction.response.send_message(
            response or "This button has no response configured.",
            ephemeral=True,
        )

    button.callback = callback
    return button


def check_author(interaction: discord.Interaction, author_id: int) -> bool:
    return interaction.user.id == author_id


NOT_AUTHOR_MESSAGE = "❌ Only the person who ran the command can use this."


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------

class EditTextModal(discord.ui.Modal, title="Edit Text"):
    def __init__(self, *, cog: "CreateEmbed", author_id: int, draft: EmbedDraft, parent_view: "CreateEmbedView") -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.author_id = author_id
        self.draft = draft
        self.parent_view = parent_view

        self.title_input = discord.ui.TextInput(
            label="Title", style=discord.TextStyle.short, required=False,
            max_length=MAX_TITLE_LENGTH, default=draft.title,
        )
        self.description_input = discord.ui.TextInput(
            label="Description", style=discord.TextStyle.paragraph, required=False,
            max_length=MAX_DESCRIPTION_LENGTH, default=draft.description,
        )
        self.footer_input = discord.ui.TextInput(
            label="Footer", style=discord.TextStyle.short, required=False,
            max_length=MAX_FOOTER_LENGTH, default=draft.footer,
        )
        self.add_item(self.title_input)
        self.add_item(self.description_input)
        self.add_item(self.footer_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.draft.title = self.title_input.value.strip()
        self.draft.description = self.description_input.value.strip()
        self.draft.footer = self.footer_input.value.strip()

        self.parent_view.stop()
        new_view = CreateEmbedView(self.cog, self.author_id, self.draft)
        await interaction.response.edit_message(view=new_view)
        new_view.message = interaction.message


class EditMediaModal(discord.ui.Modal, title="Edit Images & Video"):
    def __init__(self, *, cog: "CreateEmbed", author_id: int, draft: EmbedDraft, parent_view: "CreateEmbedView") -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.author_id = author_id
        self.draft = draft
        self.parent_view = parent_view

        self.image_input = discord.ui.TextInput(
            label="Image URL (big, main image)", style=discord.TextStyle.short,
            required=False, default=draft.image_url or "",
        )
        self.thumbnail_input = discord.ui.TextInput(
            label="Thumbnail URL (small, top-right)", style=discord.TextStyle.short,
            required=False, default=draft.thumbnail_url or "",
        )
        self.video_input = discord.ui.TextInput(
            label="Video URL", style=discord.TextStyle.short,
            required=False, default=draft.video_url or "",
        )
        self.add_item(self.image_input)
        self.add_item(self.thumbnail_input)
        self.add_item(self.video_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        warnings: list[str] = []
        self.draft.image_url = apply_url_field(self.image_input.value, self.draft.image_url, "Image URL", warnings)
        self.draft.thumbnail_url = apply_url_field(self.thumbnail_input.value, self.draft.thumbnail_url, "Thumbnail URL", warnings)
        self.draft.video_url = apply_url_field(self.video_input.value, self.draft.video_url, "Video URL", warnings)

        self.parent_view.stop()
        new_view = CreateEmbedView(self.cog, self.author_id, self.draft)
        await interaction.response.edit_message(view=new_view)
        new_view.message = interaction.message

        if warnings:
            await interaction.followup.send(
                f"⚠️ Ignored invalid link(s): {', '.join(warnings)}. Links must start with `http://` or `https://`.",
                ephemeral=True,
            )


class SetColorModal(discord.ui.Modal, title="Set Accent Color"):
    def __init__(self, *, cog: "CreateEmbed", author_id: int, draft: EmbedDraft, parent_view: "CreateEmbedView") -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.author_id = author_id
        self.draft = draft
        self.parent_view = parent_view

        self.color_input = discord.ui.TextInput(
            label="Hex color (e.g. #5865F2)", style=discord.TextStyle.short,
            required=True, max_length=7, default=f"#{to_int_color(draft.color):06X}",
        )
        self.add_item(self.color_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        parsed = parse_hex_color(self.color_input.value)
        if parsed is None:
            await interaction.response.send_message(
                "❌ That's not a valid hex color. Try something like `#5865F2`.",
                ephemeral=True,
            )
            return

        self.draft.color = parsed
        self.parent_view.stop()
        new_view = CreateEmbedView(self.cog, self.author_id, self.draft)
        await interaction.response.edit_message(view=new_view)
        new_view.message = interaction.message


class AddButtonModal(discord.ui.Modal, title="Add Button"):
    def __init__(self, *, cog: "CreateEmbed", author_id: int, draft: EmbedDraft, parent_view: "CreateEmbedView") -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.author_id = author_id
        self.draft = draft
        self.parent_view = parent_view

        self.label_input = discord.ui.TextInput(
            label="Button label", style=discord.TextStyle.short,
            required=True, max_length=MAX_BUTTON_LABEL_LENGTH,
        )
        self.style_input = discord.ui.TextInput(
            label="Button Style", style=discord.TextStyle.short, required=True,
            default="link", placeholder="link, primary, secondary, success, or danger",
        )
        self.url_input = discord.ui.TextInput(
            label="URL (only needed for 'link' style)",
            style=discord.TextStyle.short, required=False,
        )
        self.response_input = discord.ui.TextInput(
            label="Response text (only for non-link styles)",
            style=discord.TextStyle.paragraph, required=False,
        )
        self.emoji_input = discord.ui.TextInput(
            label="Emoji (optional)", style=discord.TextStyle.short, required=False,
        )
        self.add_item(self.label_input)
        self.add_item(self.style_input)
        self.add_item(self.url_input)
        self.add_item(self.response_input)
        self.add_item(self.emoji_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if len(self.draft.buttons) >= MAX_BUTTONS:
            await interaction.response.send_message(
                f"❌ You can only have up to {MAX_BUTTONS} buttons.", ephemeral=True
            )
            return

        style = self.style_input.value.strip().lower()
        if style not in BUTTON_STYLES:
            await interaction.response.send_message(
                "❌ Unknown style. Use one of: `link`, `primary`, `secondary`, `success`, `danger`.",
                ephemeral=True,
            )
            return

        label = self.label_input.value.strip() or "Button"
        url = self.url_input.value.strip() or None
        response_text = self.response_input.value.strip() or None
        emoji = safe_emoji(self.emoji_input.value.strip() or None)

        if style == "link":
            if not url or not valid_http_url(url):
                await interaction.response.send_message(
                    "❌ Link buttons need a valid `http(s)://` URL.", ephemeral=True
                )
                return
        else:
            if not response_text:
                await interaction.response.send_message(
                    "❌ Non-link buttons need response text to show when someone clicks it.",
                    ephemeral=True,
                )
                return

        self.draft.buttons.append(
            ButtonSpec(
                style=style,
                label=label[:MAX_BUTTON_LABEL_LENGTH],
                emoji=emoji,
                url=url,
                response=response_text,
            )
        )

        self.parent_view.stop()
        new_view = CreateEmbedView(self.cog, self.author_id, self.draft)
        await interaction.response.edit_message(view=new_view)
        new_view.message = interaction.message


class AddImageButtonModal(discord.ui.Modal, title="Add Image Button"):
    """Adds a button that generates an image from a prompt when clicked."""

    def __init__(self, *, cog: "CreateEmbed", author_id: int, draft: EmbedDraft, parent_view: "CreateEmbedView") -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.author_id = author_id
        self.draft = draft
        self.parent_view = parent_view

        self.prompt_input = discord.ui.TextInput(
            label="Image prompt", style=discord.TextStyle.paragraph, required=True,
            max_length=IMAGE_BUTTON_MAX_PROMPT_LENGTH,
            placeholder="a cat in space, neon cyberpunk city at night...",
        )
        self.label_input = discord.ui.TextInput(
            label="Button label", style=discord.TextStyle.short, required=False,
            max_length=MAX_BUTTON_LABEL_LENGTH, default=IMAGE_BUTTON_DEFAULT_LABEL,
        )
        self.style_input = discord.ui.TextInput(
            label="Button colour", style=discord.TextStyle.short, required=False,
            default="secondary",
            placeholder="primary, secondary, success, or danger",
        )
        self.emoji_input = discord.ui.TextInput(
            label="Emoji (optional)", style=discord.TextStyle.short, required=False,
        )
        self.add_item(self.prompt_input)
        self.add_item(self.label_input)
        self.add_item(self.style_input)
        self.add_item(self.emoji_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if len(self.draft.buttons) >= MAX_BUTTONS:
            await interaction.response.send_message(
                f"❌ You can only have up to {MAX_BUTTONS} buttons.", ephemeral=True
            )
            return

        style_raw = self.style_input.value.strip()
        style = normalize_button_style(style_raw) if style_raw else "secondary"
        if style is None or style == "link":
            await interaction.response.send_message(
                "❌ Image buttons need a non-link colour: `primary`, `secondary`, "
                "`success`, or `danger`.",
                ephemeral=True,
            )
            return

        self.draft.buttons.append(
            ButtonSpec(
                style=style,
                label=(self.label_input.value.strip() or IMAGE_BUTTON_DEFAULT_LABEL)[:MAX_BUTTON_LABEL_LENGTH],
                emoji=safe_emoji(self.emoji_input.value.strip() or None),
                image_prompt=self.prompt_input.value.strip()[:IMAGE_BUTTON_MAX_PROMPT_LENGTH],
            )
        )

        self.parent_view.stop()
        new_view = CreateEmbedView(self.cog, self.author_id, self.draft)
        await interaction.response.edit_message(view=new_view)
        new_view.message = interaction.message


class RoleButtonModal(discord.ui.Modal, title="Role Button"):
    """Labels the reaction-role button for an already-picked role."""

    def __init__(
        self,
        *,
        cog: "CreateEmbed",
        author_id: int,
        draft: EmbedDraft,
        parent_view: "CreateEmbedView",
        role: discord.Role,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.author_id = author_id
        self.draft = draft
        self.parent_view = parent_view
        self.role = role

        self.label_input = discord.ui.TextInput(
            label="Button label", style=discord.TextStyle.short,
            required=True, max_length=MAX_BUTTON_LABEL_LENGTH, default=role.name,
        )
        self.style_input = discord.ui.TextInput(
            label="Button style", style=discord.TextStyle.short, required=True,
            default="secondary", placeholder="primary, secondary, success, or danger",
        )
        self.emoji_input = discord.ui.TextInput(
            label="Emoji (optional)", style=discord.TextStyle.short, required=False,
        )
        self.add_item(self.label_input)
        self.add_item(self.style_input)
        self.add_item(self.emoji_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        style = self.style_input.value.strip().lower()
        if style not in BUTTON_STYLES or style == "link":
            await interaction.response.send_message(
                "❌ Role buttons need a non-link style: `primary`, `secondary`, `success`, or `danger`.",
                ephemeral=True,
            )
            return

        self.draft.buttons.append(
            ButtonSpec(
                style=style,
                label=(self.label_input.value.strip() or self.role.name)[:MAX_BUTTON_LABEL_LENGTH],
                emoji=safe_emoji(self.emoji_input.value.strip() or None),
                role_id=self.role.id,
            )
        )

        self.parent_view.stop()
        new_view = CreateEmbedView(self.cog, self.author_id, self.draft)
        await interaction.response.edit_message(view=new_view)
        new_view.message = interaction.message


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------

class RoleButtonSelect(discord.ui.RoleSelect):
    """Picks the role a new reaction-role button should toggle."""

    def __init__(self, *, cog: "CreateEmbed", author_id: int, draft: EmbedDraft) -> None:
        self.cog = cog
        self.author_id = author_id
        self.draft = draft
        super().__init__(
            placeholder="Add a role button (reaction role)...",
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not check_author(interaction, self.author_id):
            await interaction.response.send_message(NOT_AUTHOR_MESSAGE, ephemeral=True)
            return

        if len(self.draft.buttons) >= MAX_BUTTONS:
            await interaction.response.send_message(
                f"❌ You can only have up to {MAX_BUTTONS} buttons.", ephemeral=True
            )
            return

        role = interaction.guild.get_role(self.values[0].id) if interaction.guild else None
        if role is None:
            await interaction.response.send_message(
                "❌ That role couldn't be found.", ephemeral=True
            )
            return

        modal = RoleButtonModal(
            cog=self.cog,
            author_id=self.author_id,
            draft=self.draft,
            parent_view=self.view,
            role=role,
        )
        await interaction.response.send_modal(modal)


class ChannelPicker(discord.ui.ChannelSelect):
    def __init__(self, *, cog: "CreateEmbed", author_id: int, draft: EmbedDraft) -> None:
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
        if self.view:
            self.view.stop()

        new_view = CreateEmbedView(self.cog, self.author_id, self.draft)
        await interaction.response.edit_message(view=new_view)
        new_view.message = interaction.message


class OpenModalButton(discord.ui.Button):
    """Generic button that just opens one of the editing modals above."""

    def __init__(self, *, label: str, modal_cls: type, cog: "CreateEmbed", author_id: int, draft: EmbedDraft, style: discord.ButtonStyle = discord.ButtonStyle.secondary) -> None:
        super().__init__(label=label, style=style)
        self.modal_cls = modal_cls
        self.cog = cog
        self.author_id = author_id
        self.draft = draft

    async def callback(self, interaction: discord.Interaction) -> None:
        if not check_author(interaction, self.author_id):
            await interaction.response.send_message(NOT_AUTHOR_MESSAGE, ephemeral=True)
            return

        modal = self.modal_cls(cog=self.cog, author_id=self.author_id, draft=self.draft, parent_view=self.view)
        await interaction.response.send_modal(modal)


class AddButtonButton(discord.ui.Button):
    def __init__(self, *, cog: "CreateEmbed", author_id: int, draft: EmbedDraft) -> None:
        super().__init__(label="Add Button", style=discord.ButtonStyle.secondary)
        self.cog = cog
        self.author_id = author_id
        self.draft = draft

    async def callback(self, interaction: discord.Interaction) -> None:
        if not check_author(interaction, self.author_id):
            await interaction.response.send_message(NOT_AUTHOR_MESSAGE, ephemeral=True)
            return

        if len(self.draft.buttons) >= MAX_BUTTONS:
            await interaction.response.send_message(
                f"❌ You can only have up to {MAX_BUTTONS} buttons.", ephemeral=True
            )
            return

        modal = AddButtonModal(cog=self.cog, author_id=self.author_id, draft=self.draft, parent_view=self.view)
        await interaction.response.send_modal(modal)


class RemoveButtonButton(discord.ui.Button):
    def __init__(self, *, cog: "CreateEmbed", author_id: int, draft: EmbedDraft) -> None:
        super().__init__(label="Remove Last Button", style=discord.ButtonStyle.secondary)
        self.cog = cog
        self.author_id = author_id
        self.draft = draft

    async def callback(self, interaction: discord.Interaction) -> None:
        if not check_author(interaction, self.author_id):
            await interaction.response.send_message(NOT_AUTHOR_MESSAGE, ephemeral=True)
            return

        if not self.draft.buttons:
            await interaction.response.send_message("❌ There are no buttons to remove.", ephemeral=True)
            return

        self.draft.buttons.pop()
        if self.view:
            self.view.stop()

        new_view = CreateEmbedView(self.cog, self.author_id, self.draft)
        await interaction.response.edit_message(view=new_view)
        new_view.message = interaction.message


class ResetButton(discord.ui.Button):
    def __init__(self, *, cog: "CreateEmbed", author_id: int, draft: EmbedDraft) -> None:
        super().__init__(label="Reset", style=discord.ButtonStyle.secondary)
        self.cog = cog
        self.author_id = author_id
        self.draft = draft

    async def callback(self, interaction: discord.Interaction) -> None:
        if not check_author(interaction, self.author_id):
            await interaction.response.send_message(NOT_AUTHOR_MESSAGE, ephemeral=True)
            return

        fresh = EmbedDraft()
        if self.view:
            self.view.stop()

        new_view = CreateEmbedView(self.cog, self.author_id, fresh)
        await interaction.response.edit_message(view=new_view)
        new_view.message = interaction.message


class CancelButton(discord.ui.Button):
    def __init__(self, *, author_id: int) -> None:
        super().__init__(label="Cancel", style=discord.ButtonStyle.danger)
        self.author_id = author_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if not check_author(interaction, self.author_id):
            await interaction.response.send_message(NOT_AUTHOR_MESSAGE, ephemeral=True)
            return

        if self.view:
            self.view.stop()

        await interaction.response.edit_message(view=simple_view("❌ Embed builder cancelled.", timeout=10))


class PostButton(discord.ui.Button):
    def __init__(self, *, cog: "CreateEmbed", author_id: int, draft: EmbedDraft) -> None:
        super().__init__(label="Post", style=discord.ButtonStyle.success)
        self.cog = cog
        self.author_id = author_id
        self.draft = draft

    async def callback(self, interaction: discord.Interaction) -> None:
        if not check_author(interaction, self.author_id):
            await interaction.response.send_message(NOT_AUTHOR_MESSAGE, ephemeral=True)
            return

        if not self.draft.has_content():
            await interaction.response.send_message(
                "❌ Add at least a title, description, image, or video before posting.",
                ephemeral=True,
            )
            return

        if not self.draft.channel_id or not interaction.guild:
            await interaction.response.send_message(
                "❌ Pick a channel from the dropdown before posting.",
                ephemeral=True,
            )
            return

        channel = interaction.guild.get_channel_or_thread(self.draft.channel_id)
        if channel is None:
            await interaction.response.send_message(
                "❌ That channel couldn't be found. Pick a channel again.",
                ephemeral=True,
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

        custom_ids = make_button_ids(self.draft)
        final_view = FinalEmbedView(self.draft, custom_ids)
        try:
            message = await channel.send(view=final_view)
        except discord.HTTPException:
            logger.exception("Discord rejected the built embed")
            await interaction.response.send_message(
                "❌ Discord rejected that message -- double check your image/video links.",
                ephemeral=True,
            )
            return

        if final_view.is_persistent():
            self.cog.bot.add_view(final_view)
            # Persist so the buttons are re-registered on the next startup.
            await self.cog.persist_buttons(message, self.draft, custom_ids)

        if self.view:
            self.view.stop()

        await interaction.response.edit_message(
            view=simple_view(f"✅ Posted to {channel.mention}.", timeout=10)
        )


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

class CreateEmbedView(discord.ui.LayoutView):
    """The interactive embed builder opened by `,createembed` with no script."""

    def __init__(self, cog: "CreateEmbed", author_id: int, draft: EmbedDraft, timeout: int = VIEW_TIMEOUT) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.author_id = author_id
        self.draft = draft
        self.message: Optional[discord.Message] = None

        container = discord.ui.Container(accent_color=draft.color)
        container.add_item(
            text_display(
                f"# Command: {PREFIX}createembed\n"
                "Build your message below, then hit **Post** when you're happy with it. "
                "You can also pass a script, e.g. "
                f"`{PREFIX}ce {{title: Hi}}`.\n"
                "-# Role buttons (picked below) let members toggle a role by clicking."
            )
        )
        container.add_item(small_separator())

        add_preview_items(container, draft)
        container.add_item(small_separator())

        container.add_item(
            discord.ui.ActionRow(ChannelPicker(cog=cog, author_id=author_id, draft=draft))
        )
        container.add_item(
            discord.ui.ActionRow(RoleButtonSelect(cog=cog, author_id=author_id, draft=draft))
        )

        if draft.buttons:
            container.add_item(
                discord.ui.ActionRow(
                    *(build_custom_button(spec, preview=True) for spec in draft.buttons[:MAX_BUTTONS])
                )
            )

        container.add_item(
            discord.ui.ActionRow(
                OpenModalButton(label="Edit Text", modal_cls=EditTextModal, cog=cog, author_id=author_id, draft=draft, style=discord.ButtonStyle.primary),
                OpenModalButton(label="Edit Media", modal_cls=EditMediaModal, cog=cog, author_id=author_id, draft=draft),
                OpenModalButton(label="Set Color", modal_cls=SetColorModal, cog=cog, author_id=author_id, draft=draft),
                AddButtonButton(cog=cog, author_id=author_id, draft=draft),
                RemoveButtonButton(cog=cog, author_id=author_id, draft=draft),
            )
        )

        container.add_item(
            discord.ui.ActionRow(
                OpenModalButton(
                    label="Image Button",
                    modal_cls=AddImageButtonModal,
                    cog=cog,
                    author_id=author_id,
                    draft=draft,
                ),
                PostButton(cog=cog, author_id=author_id, draft=draft),
                ResetButton(cog=cog, author_id=author_id, draft=draft),
                CancelButton(author_id=author_id),
            )
        )

        self.add_item(container)

    async def on_timeout(self) -> None:
        if not self.message:
            return
        try:
            await self.message.edit(
                view=simple_view("⏱️ Embed builder timed out. Run the command again to keep editing.", timeout=10)
            )
        except Exception:
            pass


class FinalEmbedView(discord.ui.LayoutView):
    """The view actually posted to the target channel."""

    def __init__(
        self,
        draft: EmbedDraft,
        custom_ids: Optional[dict[str, str]] = None,
    ) -> None:
        self._persistent = any(button.style != "link" for button in draft.buttons)
        super().__init__(timeout=None if self._persistent else VIEW_TIMEOUT)
        ids = custom_ids or {}

        container = discord.ui.Container(accent_color=draft.color)
        add_preview_items(container, draft)

        if draft.buttons:
            container.add_item(small_separator())
            container.add_item(
                discord.ui.ActionRow(
                    *(
                        build_custom_button(spec, custom_id=ids.get(str(index)))
                        for index, spec in enumerate(draft.buttons[:MAX_BUTTONS])
                    )
                )
            )

        self.add_item(container)

    def is_persistent(self) -> bool:
        return self._persistent


# ---------------------------------------------------------------------------
# Script mode
# ---------------------------------------------------------------------------

#: Script fields are separated by this token.
SCRIPT_FIELD_SEP = "$v"
#: ``{guild.name}``-style tokens resolved against the invocation.
PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_.]*)\}")


def _placeholder_value(ctx: commands.Context, key: str) -> Optional[str]:
    guild = ctx.guild
    author = ctx.author
    channel = ctx.channel
    values = {
        "guild.name": guild.name if guild else "",
        "guild.id": str(guild.id) if guild else "",
        "guild.icon": (guild.icon.url if guild and guild.icon else ""),
        "guild.member_count": str(guild.member_count) if guild else "",
        "guild.boost_count": str(guild.premium_subscription_count) if guild else "",
        "user.name": getattr(author, "name", ""),
        "user.display_name": getattr(author, "display_name", getattr(author, "name", "")),
        "user.mention": getattr(author, "mention", ""),
        "user.avatar": author.display_avatar.url if getattr(author, "display_avatar", None) else "",
        "user.id": str(author.id),
        "channel.name": getattr(channel, "name", "") or "",
        "channel.mention": getattr(channel, "mention", "") or "",
    }
    return values.get(key)


def _resolve_placeholders(text: str, ctx: commands.Context) -> str:
    if not text:
        return text

    def repl(match: re.Match) -> str:
        value = _placeholder_value(ctx, match.group(1).lower())
        return value if value is not None else match.group(0)

    return PLACEHOLDER_RE.sub(repl, text)


def _parse_script_field(field: str) -> tuple[Optional[str], str]:
    """Split ``{key: value}`` into ``(key, value)``.

    The value may itself contain braces (``{footer: {guild.name}}``), so only the
    single wrapping pair is stripped.
    """
    body = field.strip()
    if body.startswith("{"):
        body = body[1:]
    if body.endswith("}"):
        body = body[:-1]
    if ":" not in body:
        return None, ""
    key, _, value = body.partition(":")
    return key.strip().lower(), value.strip()


def _build_script_button(value: str, warnings: list[str]) -> Optional[discord.ui.Button]:
    parts = [part.strip() for part in value.split("&&")]
    url = parts[0] if parts else ""
    if not valid_http_url(url):
        warnings.append("button (needs an `http(s)://` URL)")
        return None

    label = parts[1] if len(parts) > 1 and parts[1] else "Button"
    disabled = False
    if len(parts) > 2:
        disabled = parts[2].strip().lower() in {"disabled", "false", "off", "no", "0"}

    return discord.ui.Button(
        style=discord.ButtonStyle.link,
        url=url,
        label=label[:MAX_BUTTON_LABEL_LENGTH],
        disabled=disabled,
    )


def _parse_script_image_button(value: str, warnings: list[str]) -> Optional[ButtonSpec]:
    """Parse ``{imagebutton: <prompt> && <label> && <style>}``.

    The clicker gets the generated image back as an ephemeral reply, so the
    format is prompt first (required), then an optional label and an optional
    colour code (``primary``/``secondary``/``success``/``danger`` or a colour
    name such as ``red``/``blue``).
    """
    parts = [part.strip() for part in value.split("&&")]
    prompt = (parts[0] if parts else "").strip()
    if not prompt:
        warnings.append("imagebutton (needs an image prompt)")
        return None

    label = parts[1] if len(parts) > 1 and parts[1] else IMAGE_BUTTON_DEFAULT_LABEL
    style = "secondary"
    if len(parts) > 2 and parts[2]:
        resolved = normalize_button_style(parts[2])
        if resolved is None or resolved == "link":
            warnings.append(f"imagebutton style '{parts[2]}'")
        else:
            style = resolved

    return ButtonSpec(
        style=style,
        label=label[:MAX_BUTTON_LABEL_LENGTH],
        image_prompt=prompt[:IMAGE_BUTTON_MAX_PROMPT_LENGTH],
    )


class ScriptEmbedView(discord.ui.View):
    """The button view for a scripted embed.

    Carries the image-generating button specs and their custom_ids so the
    caller can persist them and keep the buttons alive across a restart.
    """

    def __init__(self, button_specs: list[ButtonSpec], button_ids: dict[str, str]) -> None:
        super().__init__(timeout=None)
        self.button_specs = button_specs
        self.button_ids = button_ids


def build_script_embed(
    ctx: commands.Context, script: str
) -> tuple[discord.Embed, Optional[discord.ui.View], list[str]]:
    """Turn a ``,ce {key: value}$v...`` script into an embed and button view."""
    color = to_int_color(EMBED_COLOR)
    title = description = thumbnail = image = None
    footer_text: Optional[str] = None
    footer_icon: Optional[str] = None
    author_name: Optional[str] = None
    author_icon: Optional[str] = None
    author_url: Optional[str] = None
    embed_fields: list[tuple[str, str, bool]] = []
    buttons: list[discord.ui.Button] = []
    image_specs: list[ButtonSpec] = []
    image_ids: dict[str, str] = {}
    token = uuid.uuid4().hex[:12]
    warnings: list[str] = []

    for raw_field in script.split(SCRIPT_FIELD_SEP):
        if not raw_field.strip():
            continue
        key, raw_value = _parse_script_field(raw_field)
        if not key:
            warnings.append(f"`{raw_field.strip()[:40]}` (not a `{{key: value}}` field)")
            continue

        value = _resolve_placeholders(raw_value, ctx)

        if key == "title":
            title = value[:MAX_TITLE_LENGTH]
        elif key == "description":
            description = value[:MAX_DESCRIPTION_LENGTH]
        elif key == "footer":
            parts = [part.strip() for part in value.split("&&")]
            footer_text = parts[0] if parts else ""
            if len(parts) > 1 and parts[1]:
                footer_icon = valid_http_url(parts[1])
                if footer_icon is None:
                    warnings.append("footer icon URL")
        elif key == "author":
            parts = [part.strip() for part in value.split("&&")]
            author_name = parts[0] if parts else ""
            if len(parts) > 1 and parts[1]:
                author_icon = valid_http_url(parts[1])
                if author_icon is None:
                    warnings.append("author icon URL")
            if len(parts) > 2 and parts[2]:
                author_url = valid_http_url(parts[2])
                if author_url is None:
                    warnings.append("author URL")
        elif key == "field":
            parts = [part.strip() for part in value.split("&&")]
            field_name = parts[0] if parts else ""
            field_value = parts[1] if len(parts) > 1 else ""
            inline = False
            if len(parts) > 2 and parts[2]:
                inline = parts[2].strip().lower() in {"inline", "true", "yes", "1"}
            if not (field_name or field_value):
                warnings.append("field (needs a name or value)")
            elif len(embed_fields) >= 25:
                warnings.append("extra field (max 25)")
            else:
                embed_fields.append(
                    (field_name[:MAX_TITLE_LENGTH], field_value[:1024], inline)
                )
        elif key in ("thumbnail", "thumb"):
            thumbnail = valid_http_url(value)
            if value and thumbnail is None:
                warnings.append("thumbnail URL")
        elif key in ("image", "image_url"):
            image = valid_http_url(value)
            if value and image is None:
                warnings.append("image URL")
        elif key == "color":
            parsed = parse_hex_color(value)
            if parsed is None:
                warnings.append("color (use a hex like `#5865F2`)")
            else:
                color = parsed
        elif key == "button":
            button = _build_script_button(value, warnings)
            if button is not None:
                if len(buttons) >= MAX_BUTTONS:
                    warnings.append(f"extra button (max {MAX_BUTTONS})")
                else:
                    buttons.append(button)
        elif key in ("imagebutton", "image_button", "genbutton"):
            spec = _parse_script_image_button(value, warnings)
            if spec is not None:
                if len(buttons) >= MAX_BUTTONS:
                    warnings.append(f"extra button (max {MAX_BUTTONS})")
                else:
                    custom_id = f"createembed:{token}:img{len(image_specs)}"
                    image_ids[str(len(image_specs))] = custom_id
                    image_specs.append(spec)
                    buttons.append(build_custom_button(spec, custom_id=custom_id))
        else:
            warnings.append(f"unknown field `{key}`")

    embed = discord.Embed(color=color)
    if title:
        embed.title = title
    if description:
        embed.description = description
    if author_name:
        embed.set_author(
            name=author_name[:MAX_TITLE_LENGTH],
            icon_url=author_icon or None,
            url=author_url or None,
        )
    for field_name, field_value, inline in embed_fields:
        embed.add_field(name=field_name, value=field_value, inline=inline)
    if footer_text:
        embed.set_footer(text=footer_text[:2048], icon_url=footer_icon or None)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    if image:
        embed.set_image(url=image)

    view: Optional[discord.ui.View] = None
    if buttons:
        view = (
            ScriptEmbedView(image_specs, image_ids)
            if image_specs
            else discord.ui.View(timeout=None)
        )
        for button in buttons:
            view.add_item(button)
    return embed, view, warnings


def _embed_has_content(embed: discord.Embed) -> bool:
    return bool(
        embed.title
        or embed.description
        or embed.image
        or embed.thumbnail
        or embed.fields
    )


#: Message link form: ``/channels/<guild>/<channel>/<message>``.
MESSAGE_LINK_RE = re.compile(r"/channels/\d+/(\d+)/(\d+)")


def _embed_script(embed: discord.Embed, message: discord.Message) -> str:
    """Turn an existing embed back into a ``,ce {key: value}$v...`` script."""
    fields: list[str] = []
    author = getattr(embed, "author", None)
    author_name = getattr(author, "name", None)
    if author_name:
        parts = [author_name]
        if getattr(author, "icon_url", None):
            parts.append(author.icon_url)
        if getattr(author, "url", None):
            parts.append(author.url)
        fields.append("{{author: {}}}".format(" && ".join(parts)))
    if embed.title:
        fields.append("{{title: {}}}".format(embed.title))
    if embed.description:
        fields.append("{{description: {}}}".format(embed.description))
    for field in getattr(embed, "fields", []):
        parts = [field.name or "", field.value or ""]
        if field.inline:
            parts.append("inline")
        fields.append("{{field: {}}}".format(" && ".join(parts)))
    if embed.color is not None:
        fields.append("{{color: #{:06X}}}".format(embed.color.value))

    footer = getattr(embed, "footer", None)
    footer_text = getattr(footer, "text", None)
    if footer_text:
        parts = [footer_text]
        footer_icon = getattr(footer, "icon_url", None)
        if footer_icon:
            parts.append(footer_icon)
        fields.append("{{footer: {}}}".format(" && ".join(parts)))

    thumbnail = getattr(getattr(embed, "thumbnail", None), "url", None)
    if thumbnail:
        fields.append("{{thumbnail: {}}}".format(thumbnail))
    image = getattr(getattr(embed, "image", None), "url", None)
    if image:
        fields.append("{{image: {}}}".format(image))

    for row in message.components:
        for child in getattr(row, "children", []):
            if not isinstance(child, discord.ui.Button):
                continue
            if child.style is not discord.ButtonStyle.link or not child.url:
                continue
            label = child.label
            if not label:
                label = str(child.emoji) if child.emoji else "Button"
            state = "disabled" if child.disabled else "enabled"
            fields.append(
                "{{button: {} && {} && {}}}".format(child.url, label, state)
            )

    return SCRIPT_FIELD_SEP.join(fields)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class CreateEmbed(commands.Cog, name="CreateEmbed"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Message ids that currently have persisted buttons, so message-delete
        # cleanups only touch rows that actually exist.
        self._button_messages: set[int] = set()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def cog_load(self) -> None:
        try:
            await self._restore_buttons()
        except Exception:
            logger.exception("Could not restore persisted embed buttons")

    async def persist_buttons(
        self,
        message: discord.Message,
        draft: EmbedDraft,
        custom_ids: dict[str, str],
    ) -> None:
        if message.guild is None:
            return
        stored = 0
        for index, spec in enumerate(draft.buttons[:MAX_BUTTONS]):
            custom_id = custom_ids.get(str(index))
            if not custom_id:
                continue
            try:
                await self.bot.db.execute(
                    "INSERT OR REPLACE INTO embed_buttons "
                    "(custom_id, guild_id, channel_id, message_id, style, label, "
                    "emoji, response, role_id, image_prompt) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);",
                    custom_id,
                    message.guild.id,
                    message.channel.id,
                    message.id,
                    spec.style,
                    spec.label,
                    spec.emoji,
                    spec.response,
                    spec.role_id,
                    spec.image_prompt,
                )
                stored += 1
            except Exception:
                logger.exception("Could not persist embed button %s", custom_id)
        if stored:
            self._button_messages.add(message.id)

    async def _restore_buttons(self) -> None:
        rows = await self.bot.db.execute(
            "SELECT custom_id, style, label, emoji, response, role_id, "
            "image_prompt, message_id FROM embed_buttons;"
        )
        by_message: dict[int, list[dict]] = {}
        for custom_id, style, label, emoji, response, role_id, image_prompt, message_id in rows:
            by_message.setdefault(message_id, []).append(
                {
                    "custom_id": custom_id,
                    "style": style,
                    "label": label,
                    "emoji": emoji,
                    "response": response,
                    "role_id": role_id,
                    "image_prompt": image_prompt,
                }
            )

        for message_id, entries in by_message.items():
            try:
                view = build_button_view(entries)
            except Exception:
                logger.exception("Could not rebuild buttons for message %s", message_id)
                continue
            if view.children:
                self.bot.add_view(view)
                self._button_messages.add(message_id)
        if by_message:
            logger.info(
                "Restored persisted embed buttons for %d message(s)", len(by_message)
            )

    async def _forget_message(self, message_id: int) -> None:
        if message_id not in self._button_messages:
            return
        self._button_messages.discard(message_id)
        try:
            await self.bot.db.execute(
                "DELETE FROM embed_buttons WHERE message_id = %s;", message_id
            )
        except Exception:
            logger.exception("Could not clear embed buttons for message %s", message_id)

    @commands.Cog.listener()
    async def on_raw_message_delete(
        self, payload: discord.RawMessageDeleteEvent
    ) -> None:
        await self._forget_message(payload.message_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(
        self, payload: discord.RawBulkMessageDeleteEvent
    ) -> None:
        for message_id in payload.message_ids:
            await self._forget_message(message_id)

    # ------------------------------------------------------------------
    # Command
    # ------------------------------------------------------------------

    @commands.command(
        name="createembed",
        aliases=["embedbuilder", "buildembed", "ce"],
        help="Build an embed interactively, or from a {key: value}$v... script.",
    )
    @commands.has_permissions(manage_messages=True)
    async def createembed(self, ctx: commands.Context, *, script: Optional[str] = None) -> None:
        f"""Build and post an embed.

        With no arguments this opens the interactive builder. With a script it
        builds the embed directly, for example:
        `{PREFIX}ce {{title: Time After Time}}$v{{description: BINGEDRINKINGMUSIC}}$v`
        `{{footer: {{guild.name}} && {{guild.icon}}}}$v{{thumbnail: {{guild.icon}}}}$v`
        `{{button: https://example.com && 🎵 WAV File && enabled}}`
        `{{imagebutton: a cat in space && Red Cat && danger}}`
        """
        if ctx.guild is None:
            await ctx.send(view=simple_view("❌ This command can only be used in a server."))
            return

        if script and script.strip():
            try:
                embed, view, warnings = build_script_embed(ctx, script.strip())
            except Exception:
                logger.exception("Could not parse embed script")
                await ctx.send(view=simple_view("❌ I couldn't read that script."))
                return

            if not _embed_has_content(embed):
                await ctx.send(
                    view=simple_view(
                        "❌ That script didn't set a title, description, image or thumbnail."
                    )
                )
                return

            try:
                message = await ctx.send(embed=embed, view=view)
            except discord.HTTPException:
                logger.exception("Discord rejected the scripted embed")
                await ctx.send(
                    view=simple_view(
                        "❌ Discord rejected that embed -- double-check the links."
                    )
                )
                return

            # Image buttons carry a prompt, so they are persisted for restart
            # recovery just like the interactive builder's buttons.
            specs = getattr(view, "button_specs", None)
            ids = getattr(view, "button_ids", None)
            if specs and ids:
                try:
                    await self.persist_buttons(message, EmbedDraft(buttons=specs), ids)
                except Exception:
                    logger.exception("Could not persist scripted image buttons")

            if warnings:
                await ctx.send(
                    view=simple_view(
                        "⚠️ Ignored: " + ", ".join(warnings), timeout=15
                    )
                )
            return

        draft = EmbedDraft()
        view = CreateEmbedView(self, ctx.author.id, draft)
        view.message = await ctx.send(view=view)

    @commands.command(
        name="embedcode",
        aliases=["embedscript", "cecode"],
        help="Get the script that rebuilds an existing embed with ,ce.",
    )
    @commands.has_permissions(manage_messages=True)
    async def embedcode(
        self, ctx: commands.Context, *, message: Optional[str] = None
    ) -> None:
        """Print the ``,ce`` script for an embed so it can be sent again.

        Reply to a message containing an embed, or pass its id/link. The script
        can be edited before it is used, then sent with ``,ce <script>``.
        """
        target: Optional[discord.Message] = None
        try:
            if message is None or not message.strip():
                reference = ctx.message.reference
                if reference is None:
                    await ctx.send(
                        view=simple_view(
                            "❌ Reply to a message with an embed, or give me its id/link."
                        )
                    )
                    return
                resolved = reference.resolved
                if isinstance(resolved, discord.Message):
                    target = resolved
                elif resolved is None and reference.message_id:
                    target = await ctx.channel.fetch_message(reference.message_id)
            else:
                raw = message.strip()
                match = MESSAGE_LINK_RE.search(raw)
                if match:
                    channel_id, message_id = int(match.group(1)), int(match.group(2))
                    channel = self.bot.get_channel(channel_id)
                    if channel is None:
                        channel = await self.bot.fetch_channel(channel_id)
                    target = await channel.fetch_message(message_id)
                elif raw.isdigit():
                    target = await ctx.channel.fetch_message(int(raw))
                else:
                    await ctx.send(
                        view=simple_view("❌ Give me a message id or a message link.")
                    )
                    return
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            await ctx.send(
                view=simple_view("❌ I couldn't find that message in this channel.")
            )
            return

        if target is None or not target.embeds:
            await ctx.send(
                view=simple_view("❌ That message doesn't have an embed I can copy.")
            )
            return

        try:
            scripts = [_embed_script(embed, target) for embed in target.embeds]
        except Exception:
            logger.exception("Could not turn an embed into a script")
            await ctx.send(view=simple_view("❌ I couldn't read that embed."))
            return
        scripts = [script for script in scripts if script]
        if not scripts:
            await ctx.send(
                view=simple_view("❌ That embed doesn't have anything I can copy.")
            )
            return

        try:
            prefix = await self.bot.get_prefix(ctx.message)
        except Exception:
            prefix = PREFIX
        if isinstance(prefix, (list, tuple)):
            prefix = prefix[0] if prefix else PREFIX
        prefix = prefix or PREFIX

        code = "\n".join(f"{prefix}ce {script}" for script in scripts)
        if len(code) <= 1900:
            note = ""
            if len(scripts) > 1:
                note = f"\n-# {len(scripts)} embeds: one command per line."
            await ctx.reply(
                f"```\n{code}\n```\n-# Edit it, then send it to post the embed again.{note}"
            )
            return

        # Too long for a single message (a long description will do it), so
        # hand it over as a file the code can be copied out of.
        await ctx.reply(
            "The script is too long for one message, so here it is as a file.",
            file=discord.File(io.BytesIO(code.encode()), filename="embedcode.txt"),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CreateEmbed(bot))
