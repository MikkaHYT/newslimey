from __future__ import annotations

import logging
from typing import Sequence, Union

import discord

from config import EMBED_COLOR

logger = logging.getLogger("slimey.views")

AttachmentPayload = Union[discord.Attachment, discord.File]


class SimpleLayout(discord.ui.LayoutView):
    """A layout view holding one or more containers."""

    def __init__(self, *containers: discord.ui.Container, timeout: float | None = 300.0) -> None:
        super().__init__(timeout=timeout)
        for container in containers:
            self.add_item(container)


class UsageLayout(SimpleLayout):
    """A layout that only explains a command instead of playing one.

    The marker is what tells the cooldown gate nothing happened here, so a
    member who was told how to use a command isn't charged for it. See
    ``utils.core.cooldowns``.
    """

    usage_only = True


def usage_card(message: str, *, emoji: str = "❌") -> UsageLayout:
    """The shared "here's what this command wants" card."""
    box = discord.ui.Container(accent_color=EMBED_COLOR)
    box.add_item(discord.ui.TextDisplay(f"{emoji} {message}"))
    return UsageLayout(box)


async def swap_layout_message(
    interaction: discord.Interaction,
    view: discord.ui.LayoutView,
    *,
    attachments: Sequence[AttachmentPayload] | None = None,
) -> discord.Message | None:
    """Replace the message a component sits on with a freshly built layout.

    Component interactions only have three seconds to answer, and uploading a
    song can take longer than that, so the interaction is always deferred first
    and the message is edited afterwards. ``@original`` is the documented edit
    target for components; if Discord rejects it the message is patched
    directly instead so a dropdown click never looks broken.

    ``attachments`` mirrors the ``edit_message`` parameter: pass a list to keep
    existing attachments (the same :class:`discord.Attachment` objects) and add
    new ones (:class:`discord.File`). Leaving it as ``None`` leaves whatever is
    already on the message untouched.
    """
    if not interaction.response.is_done():
        await interaction.response.defer()

    kwargs: dict[str, object] = {"view": view}
    if attachments is not None:
        kwargs["attachments"] = list(attachments)

    try:
        return await interaction.edit_original_response(**kwargs)
    except (discord.NotFound, discord.HTTPException) as error:
        logger.debug("edit_original_response failed (%s); patching the message", error)
        if interaction.message is None:
            return None
        return await interaction.message.edit(**kwargs)


class Confirm(discord.ui.View):
    def __init__(self, message: discord.Message, invoker: discord.Member | None = None) -> None:
        super().__init__(timeout=60)
        self.value = False
        self.message = message
        self.invoker = invoker

    async def on_timeout(self) -> None:
        await self.message.edit(view=None)

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._finish(interaction, True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._finish(interaction, False)

    async def _finish(self, interaction: discord.Interaction, value: bool) -> None:
        if self.invoker and interaction.user.id != self.invoker.id:
            return

        await interaction.response.defer()
        await self.message.edit(view=None)
        self.value = value
        self.stop()


class ResetConfirm(discord.ui.View):
    """A green-confirm / red-cancel prompt for a destructive reset.

    ``value`` stays ``None`` when the prompt times out, which the caller reads
    as "do nothing". Callers are expected to set ``view.message`` so the card
    can be edited into its cancelled / running / finished state.
    """

    def __init__(self, invoker_id: int, *, timeout: float = 30.0) -> None:
        super().__init__(timeout=timeout)
        self.invoker_id = invoker_id
        self.value: bool | None = None
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Only the person who ran the command can confirm this.",
                ephemeral=True,
            )
            return False
        return True

    def disable(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="✖️")
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.value = False
        await interaction.response.defer()
        self.stop()
