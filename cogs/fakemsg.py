# cogs/fakemsg.py

from typing import Any

import discord
from discord.ext import commands


def _bot_admin_or_manage_webhooks() -> Any:
    """Allow bot admins anywhere, or anyone with Manage Webhooks.

    IDs added with ``!admin allow`` (and the application team) can fake a
    message in any server without holding Manage Webhooks: the webhook is
    created with the bot's own permissions. Everyone else still needs the
    permission in that channel.
    """

    async def predicate(ctx: commands.Context) -> bool:
        # Check the stored admin list directly, then fall back to the bot's
        # is_owner override (which also covers the application team).
        admin_ids = getattr(ctx.bot, "bot_admin_ids", None) or set()
        if ctx.author.id in admin_ids:
            return True
        if await ctx.bot.is_owner(ctx.author):
            return True
        if ctx.channel.permissions_for(ctx.author).manage_webhooks:
            return True
        raise commands.MissingPermissions(["manage_webhooks"])

    return commands.check(predicate)


class FakeMessage(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="fakemsg", aliases=("fake", "impersonate"))
    @_bot_admin_or_manage_webhooks()
    # Only the webhook matters: deleting the trigger message below is a
    # best-effort nicety, so a bot without Manage Messages can still post.
    @commands.bot_has_permissions(manage_webhooks=True)
    async def fakemsg(
        self,
        ctx: commands.Context,
        member: discord.Member,
        *,
        message: str,
    ) -> None:
        # Delete the trigger command message
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass

        # Reuse an existing webhook owned by the bot or create a new one
        webhooks = await ctx.channel.webhooks()
        webhook = discord.utils.get(webhooks, user=self.bot.user)

        if webhook is None:
            webhook = await ctx.channel.create_webhook(name="FakeMsg Webhook")

        # Send the payload as the target member
        await webhook.send(
            content=message,
            username=member.display_name,
            avatar_url=member.display_avatar.url,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @fakemsg.error
    async def fakemsg_error(self, ctx: commands.Context, error: Exception) -> None:
        prefix = ctx.prefix or "!"
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(
                f"Usage: `{prefix}fakemsg <@user|userID> <message>`", delete_after=5
            )
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send("Couldn't find that member in this server.", delete_after=5)
        elif isinstance(
            error, (commands.MissingPermissions, commands.CheckAnyFailure)
        ):
            # Neither a bot admin nor holding Manage Webhooks here.
            await ctx.send(
                "You need **Manage Webhooks** in this server to fake a message.",
                delete_after=5,
            )
        elif isinstance(error, commands.BotMissingPermissions):
            await ctx.send(
                "I need **Manage Webhooks** to do that.", delete_after=5
            )
        elif isinstance(error, discord.Forbidden):
            await ctx.send(
                "I couldn't create a webhook here — check that I still have "
                "**Manage Webhooks**.",
                delete_after=5,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FakeMessage(bot))
