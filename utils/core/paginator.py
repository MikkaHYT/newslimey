from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

import discord

if TYPE_CHECKING:
    from utils.core.context import Context


class Paginator(discord.ui.View):
    def __init__(
        self,
        bot: discord.Client,
        embeds: Iterable[discord.Embed],
        destination: Context,
        *,
        invoker: int | None = None,
    ) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.embeds = list(embeds)
        self.page = 0
        self.destination = destination
        self.invoker = invoker
        self.message: discord.Message | None = None
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        if len(self.embeds) <= 1:
            for child in self.children:
                child.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.invoker and interaction.user.id != self.invoker:
            await interaction.response.send_message(
                "This paginator belongs to someone else.",
                ephemeral=True,
            )
            return False
        return True

    async def edit_embed(self, interaction: discord.Interaction) -> None:
        if self.message:
            await interaction.response.edit_message(
                embed=self.embeds[self.page],
                view=self,
            )

    @discord.ui.button(label="First", style=discord.ButtonStyle.secondary)
    async def first_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = 0
        await self.edit_embed(interaction)

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = (self.page - 1) % len(self.embeds)
        await self.edit_embed(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = (self.page + 1) % len(self.embeds)
        await self.edit_embed(interaction)

    @discord.ui.button(label="Last", style=discord.ButtonStyle.secondary)
    async def last_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = len(self.embeds) - 1
        await self.edit_embed(interaction)

    async def start(self) -> discord.Message:
        self.message = await self.destination.send(
            embed=self.embeds[self.page],
            view=self,
        )
        return self.message
