import asyncio
from enum import member
import os
import random
from datetime import timedelta
import aiohttp
import discord
import logging
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context
from utils.misc import MiscUtils
from utils.core.http_session import borrowed
from utils.cooldown import unified_cooldown
from utils.embeds import Embeds
from utils.guardrails import check_slash_guardrails

logger = logging.getLogger("discord.client")


def timeout_blocked_reason(
    guild: discord.Guild, member: discord.Member
) -> str | None:
    """Why the bot cannot time ``member`` out, or ``None`` when it can.

    Holding Moderate Members is not enough on its own: Discord only lets a bot
    act on members whose highest role sits *below* the bot's own highest role,
    so somebody who outranks the bot is untouchable no matter which permissions
    the bot has (Administrator included). On this server the bot's role sat
    below `Members`, which made every draw look like a missing permission.
    """
    me = guild.me
    if me is None:
        return "I am not in this server"
    if member.id == guild.owner_id:
        return "they own the server"
    if member.id == me.id:
        return "that's me"
    if member.top_role >= me.top_role:
        return f"their {member.top_role.name} role outranks mine"
    return None


def describe_skipped(skipped: list[tuple[str, str]], limit: int = 10) -> str:
    """A footer-ready list of the players the draw could not reach, and why."""
    return ", ".join(f"{name} ({why})" for name, why in skipped[:limit])


def timeout_blocked_note(guild: discord.Guild) -> str:
    """The card shown when nobody who joined can actually be timed out."""
    me = guild.me
    mine = (
        f"**{me.top_role.name}** (position {me.top_role.position})"
        if me is not None and me.top_role is not None
        else "my highest role"
    )
    return (
        "Everyone who joined was safe — I can't time any of them out. It is "
        "not my permissions (I have Moderate Members); it is role order: "
        "Discord only lets me act on members whose highest role sits below "
        f"mine, and my highest role is {mine}.\n\n"
        "Fix it in **Server Settings → Roles**: drag my role above the roles "
        "those players hold, then run the game again."
    )


#: The Gay Time clip handed out by `!gaytime`. This is a Discord CDN link, so
#: it carries a signed expiry (`?ex=...`) - swap it here when it lapses.
GAY_TIME_URL = (
    "https://cdn.discordapp.com/attachments/865920571065892864/"
    "1560757535131050026/Gay_Time.m4a?ex=6ad3e039&is=6ad28eb9"
    "&hm=778b3cc207eae0eaf6bf24dbf2387016a311bc6b466f993a758c94f15aea7868"
)


class GayTimeView(discord.ui.View):
    """The single link button that ships with `!gaytime`.

    Link buttons are handled entirely by Discord's client, so the view holds
    no state and never needs to expire.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                style=discord.ButtonStyle.link,
                label="🎵 Gay Time",
                url=GAY_TIME_URL,
            )
        )


class Fun(commands.Cog, name="Fun"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.utils = MiscUtils(self)
        self.ban_roulette_history = {}

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await check_slash_guardrails(self, interaction)

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info(f"Cog {self.__class__.__name__} is ready!")

    @commands.hybrid_command(
        name="randomfact", aliases=["rfact"], description="Get a random fact."
    )
    @unified_cooldown(10)
    async def randomfact(self, ctx: Context) -> None:
        """
        Get a random fact from API Ninjas.
        """
        self.api_key = os.getenv("API_NINJAS_KEY")
        if not self.api_key:
            await Embeds.error(ctx, "API key for API Ninjas is not set.", title="Error!", reply=False)
            return
        url = "https://api.api-ninjas.com/v1/facts"
        headers = {"X-Api-Key": self.api_key}

        async with borrowed() as session:
            async with session.get(url, headers=headers) as request:
                if request.status == 200:
                    data = await request.json()

                    fact_text = (
                        data[0].get("fact") if isinstance(data, list) and data else None
                    )

                    if fact_text:
                        color = (
                            discord.Color.blurple()
                            if isinstance(ctx.channel, discord.DMChannel)
                            else (ctx.author.top_role.color or discord.Color.blurple())
                        )
                        embed = discord.Embed(
                            title="Random Fact",
                            description=fact_text,
                            color=color,
                            timestamp=discord.utils.utcnow(),
                        )
                    else:
                        embed = discord.Embed(
                            title="Oops!",
                            description="Couldn't parse a fact from the API.",
                            color=discord.Color.red(),
                        )
                else:
                    await Embeds.error(ctx, "There was a problem contacting the API. Please try again later.", title="Error!", reply=False)

    @commands.hybrid_command(
        name="fotd",
        aliases=["factoftheday"],
        description="Get a random fact of the day.",
    )
    @unified_cooldown(10)
    async def fotd(self, ctx: Context) -> None:
        """Get a random fact of the day."""
        url = "https://uselessfacts.jsph.pl/api/v2/facts/today?language=en"

        async with borrowed() as session:
            async with session.get(url) as response:
                if response.status != 200:
                    await Embeds.error(ctx, "There was a problem contacting the Useless Facts API. Please try again later.", title="Error!", reply=True)

                data = await response.json()

                if isinstance(data, dict):
                    item = data
                elif isinstance(data, list) and data:
                    item = data[0]
                else:
                    item = None

                if not item or not item.get("text"):
                    await Embeds.error(ctx, "Couldn't parse a fact of the day from the API response.", title="Oops!", reply=True)

                fact_text = item["text"]
                source = item.get("source") or item.get("source_url") or "unknown"

                color = (
                    discord.Color.blurple()
                    if isinstance(ctx.channel, discord.DMChannel)
                    else (ctx.author.top_role.color or discord.Color.blurple())
                )

                embed = discord.Embed(
                    title=f"Fact of the Day – {discord.utils.utcnow().strftime('%Y-%m-%d')}",
                    description=fact_text,
                    color=color,
                )
                embed.set_footer(text=f"Source: {source}")

                await ctx.reply(embed=embed)

    @commands.hybrid_command(name="dog", description="Get a random dog photo.")
    @unified_cooldown(10)
    async def dog(self, ctx: commands.Context):
        """Get a random dog photo."""
        url = "https://random.dog/woof.json"
        async with borrowed() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    await ctx.reply(data["url"])
                else:
                    await Embeds.error(ctx, "There was a problem contacting the Dog API. Please try again later.", title="Error!", reply=True)

    @commands.hybrid_command(name="cat", description="Get a random cat photo.")
    @unified_cooldown(10)
    async def cat(self, ctx: commands.Context):
        """Get a random cat photo."""
        url = "https://api.thecatapi.com/v1/images/search"
        async with borrowed() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    await ctx.reply(data[0]["url"])
                else:
                    await Embeds.error(ctx, "There was a problem contacting the Cat API. Please try again later.", title="Error!", reply=True)

    @commands.hybrid_command(
        name="bunny",
        aliases=["rabbit"],
        description="Get a random bunny photo.",
    )
    @unified_cooldown(10)
    async def bunny(self, ctx: commands.Context):
        """Get a random bunny photo."""
        url = "https://rabbit-api-two.vercel.app/api/random"
        async with borrowed() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    image = data.get("url")
                    if image:
                        await ctx.reply(image)
                    else:
                        await Embeds.error(ctx, "The Bunny API sent back nothing to show. Please try again later.", title="Error!", reply=True)
                else:
                    await Embeds.error(ctx, "There was a problem contacting the Bunny API. Please try again later.", title="Error!", reply=True)

    @commands.hybrid_command(name="fox", description="Get a random fox photo.")
    @unified_cooldown(10)
    async def fox(self, ctx: commands.Context):
        """Get a random fox photo."""
        url = "https://randomfox.ca/floof/"
        async with borrowed() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    await ctx.reply(data["image"])
                else:
                    await Embeds.error(ctx, "There was a problem contacting the Fox API. Please try again later.", title="Error!", reply=True)

    @commands.command(name="penguin", description="Get a random penguin photo.")
    async def penguin(self, ctx: commands.Context):
        """Get a random penguin photo."""
        url = "https://penguin.sjsharivker.workers.dev/api"
        async with borrowed() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    await ctx.reply(data["img"])
                if resp.status == 201:
                    data = await resp.json()
                    await ctx.reply(data["img"])
                else:
                    await Embeds.error(ctx, "There was a problem contacting the Penguin API. Please try again later.", title="Error!", reply=True)

    @commands.command(
        name="duck", description="Get a random duck photo.", hidden=True
    )
    async def duck(self, ctx: commands.Context):
        """Get a random duck photo."""
        url = "https://random-d.uk/api/v2/random"
        async with borrowed() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    await ctx.reply(data["url"])
                else:
                    await Embeds.error(ctx, "There was a problem contacting the Duck API. Please try again later.", title="Error!", reply=True)

    @commands.command(name="boom", aliases=["kaboom"], description="boom.")
    async def pow(self, ctx: Context):
        await ctx.reply("pow :boom:")

    @commands.command(name="gaytime", description="Hand out the Gay Time clip.")
    async def gaytime(self, ctx: Context):
        """Post the Gay Time clip behind a button."""
        embed = discord.Embed(
            title="from pookie brux",
            description="here you go",
            color=discord.Color.blurple(),
        )
        await ctx.reply(embed=embed, view=GayTimeView())

    @commands.command(
        name="gay", aliases=["gayrate"], description="Estimates how homosexual a user is"
    )
    async def random_percentage(self, ctx: Context, member: discord.Member = None):
        """Estimates how homosexual a user is"""
        percentage = random.randrange(100)
        color = discord.Color.blurple()
        if isinstance(ctx.channel, discord.DMChannel):
            color = discord.Color.blurple()
        else:
            color = (
                ctx.author.top_role.color
                if ctx.author.top_role
                else discord.Color.blurple()
            )
        if member is None:
            if percentage > 50:
                embed = discord.Embed(
                    description=f"are {percentage}% gay :rainbow:", color=color
                )
                embed.set_author(
                    name=f"You", icon_url=self.utils.get_avatar_url(ctx.author)
                )
                await ctx.reply(embed=embed)
            else:
                embed = discord.Embed(description=f"are {percentage}% gay", color=color)
                embed.set_author(
                    name=f"You", icon_url=self.utils.get_avatar_url(ctx.author)
                )
                await ctx.reply(embed=embed)
        elif member == ctx.author:
            if percentage > 50:
                embed = discord.Embed(
                    description=f"are {percentage}% gay :rainbow:", color=color
                )
                embed.set_author(
                    name=f"You", icon_url=self.utils.get_avatar_url(ctx.author)
                )
                await ctx.reply(embed=embed)
            else:
                embed = discord.Embed(description=f"are {percentage}% gay", color=color)
                embed.set_author(
                    name=f"You", icon_url=self.utils.get_avatar_url(ctx.author)
                )
                await ctx.reply(embed=embed)
        elif member != ctx.author:
            if percentage > 50:
                embed = discord.Embed(
                    description=f"is {percentage}% gay :rainbow:", color=color
                )
                embed.set_author(
                    name=f"{member.display_name}",
                    icon_url=self.utils.get_avatar_url(member),
                )
                await ctx.reply(embed=embed)
            else:
                embed = discord.Embed(description=f"is {percentage}% gay", color=color)
                embed.set_author(
                    name=f"{member.display_name}",
                    icon_url=self.utils.get_avatar_url(member),
                )
                await ctx.reply(embed=embed)

    @commands.command(
        name="penis", aliases=["dih"], description="Find out how large your penis is."
    )
    async def size(self, ctx: Context, member: discord.Member = None):
        member = member or ctx.author

        if member.id in [1479952126967812187, 881692219260665867]:
            size = random.randrange(start=0, stop=3)
        else:
            size = random.randrange(start=0, stop=21)

        if size == 0:
            penis = "No penis detected"
        else:
            penis = f"8{'=' * size}D"
        embed = discord.Embed(description=f"{penis}")
        embed.set_author(
            name=f"{member.display_name}'s Penis Size",
            icon_url=self.utils.get_avatar_url(member),
        )
        await ctx.reply(embed=embed)

    @commands.command(
        name="weight", description="Find out how much you weigh"
    )
    async def weight(self, ctx: Context, member: discord.Member = None):
        """Find out how much you weigh"""
        member = member or ctx.author

        units = ["kg", "lb", "tons"]
        selected_unit = random.choice(units)

        if selected_unit == "kg":
            if random.random() < 0.85:
                weight = random.randint(50, 150)
            else:
                weight = random.randint(151, 1000)
        elif selected_unit == "lb":
            if random.random() < 0.85:
                weight = random.randint(110, 330)
            else:
                weight = random.randint(331, 2200)
        else:
            if random.random() < 0.85:
                weight = round(random.uniform(0.1, 0.5), 2)
            else:
                weight = round(random.uniform(0.51, 50), 2)

        color = discord.Color.blurple()
        if not isinstance(ctx.channel, discord.DMChannel) and ctx.author.top_role:
            color = ctx.author.top_role.color

        embed = discord.Embed(
            description=f"weighs {weight} {selected_unit}", color=color
        )
        embed.set_author(
            name=member.display_name, icon_url=self.utils.get_avatar_url(member)
        )
        await ctx.reply(embed=embed)

    @commands.command(
        name="height", description="Find out how tall you are"
    )
    async def height(self, ctx: Context, member: discord.Member = None):
        """Find out how tall you are (for comedic purposes)."""
        member = member or ctx.author

        if random.random() < 0.85:
            feet = random.randint(3, 7)
            inches = random.randint(0, 11)
        else:
            if random.random() < 0.5:
                feet = random.randint(8, 17)
                inches = random.randint(0, 11)
            else:
                feet = random.randint(0, 2)
                inches = random.randint(0, 11)

        height_str = f"{feet} ft {inches} in"

        color = discord.Color.blurple()
        if not isinstance(ctx.channel, discord.DMChannel) and ctx.author.top_role:
            color = ctx.author.top_role.color

        embed = discord.Embed(description=f"has a height of {height_str}", color=color)
        embed.set_author(
            name=member.display_name, icon_url=self.utils.get_avatar_url(member)
        )
        await ctx.reply(embed=embed)

    @commands.command(name="iq", description="Find out your IQ")
    async def iq(self, ctx: Context, member: discord.Member = None):
        """Find out your IQ."""
        member = member or ctx.author

        if random.random() < 0.85:
            iq = random.randint(90, 130)
        else:
            if random.random() < 0.5:
                iq = random.randint(131, 200)
            else:
                iq = random.randint(1, 89)

        color = discord.Color.blurple()
        if not isinstance(ctx.channel, discord.DMChannel) and ctx.author.top_role:
            color = ctx.author.top_role.color

        embed = discord.Embed(description=f"has an IQ of {iq}", color=color)
        embed.set_author(
            name=member.display_name, icon_url=self.utils.get_avatar_url(member)
        )
        await ctx.reply(embed=embed)

    @commands.command(
        name="banroulette", aliases=["banrou"], description="Play a game of banroulette"
    )
    @commands.has_guild_permissions(ban_members=True)
    async def banroulette(self, ctx: Context):
        """Play a game of banroulette. Players react to join the game and one player is randomly selected to be 'banned'."""
        embed = discord.Embed(
            title="Banroulette",
            description="React with 🔫 to join the game! You have 15 seconds.",
            color=discord.Color.blurple(),
        )
        message = await ctx.reply(embed=embed)
        await message.add_reaction("🔫")

        await asyncio.sleep(15)

        message = await ctx.fetch_message(message.id)
        users = set()
        for reaction in message.reactions:
            if str(reaction.emoji) == "🔫":
                async for user in reaction.users():
                    if not user.bot:
                        users.add(user)

        if len(users) < 2:
            embed = discord.Embed(
                title="Ban Roulette",
                description="Not enough players joined the game. Need at least 2 players.",
                color=discord.Color.red(),
            )
            await message.edit(embed=embed)
            return

        banned_user = random.choice(list(users))

        victim = await ctx.guild.fetch_member(banned_user.id)
        if not victim:
            embed = discord.Embed(
                title="Ban Roulette",
                description="Could not find the selected user in the guild.",
                color=discord.Color.red(),
            )
            await message.edit(embed=embed)
            return

        try:
            await victim.ban(reason="Lost Ban Roulette", delete_message_days=0)
            self.ban_roulette_history[ctx.author.id] = victim.id
            embed = discord.Embed(
                title="Ban Roulette",
                description=f"{victim.mention} has been banned! :hammer:",
                color=discord.Color.green(),
            )
        except discord.Forbidden:
            embed = discord.Embed(
                title="Ban Roulette",
                description=f"{victim.mention} would have been banned, but I don't have permission! :hammer:",
                color=discord.Color.red(),
            )
        except Exception as e:
            await Embeds.error(ctx, f"Failed to ban {victim.mention}: {str(e)}", title="Ban Roulette", reply=False)

    @commands.command(
        name="timeoutroulette", aliases=["tr"], description="Play a game of timeout roulette"
    )
    @commands.guild_only()
    @commands.has_guild_permissions(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    async def timeoutroulette(self, ctx: Context):
        """Play a game of timeout roulette. Players react to join the game and one player is randomly selected to be timed out."""
        embed = discord.Embed(
            title="Timeout Roulette",
            description="React with ⏰ to join the game! You have 15 seconds.",
            color=discord.Color.blurple(),
        )
        message = await ctx.reply(embed=embed)
        await message.add_reaction("⏰")

        await asyncio.sleep(15)

        message = await ctx.fetch_message(message.id)
        users = set()
        for reaction in message.reactions:
            if str(reaction.emoji) == "⏰":
                async for user in reaction.users():
                    if not user.bot:
                        users.add(user)

        if len(users) < 2:
            embed = discord.Embed(
                title="Timeout Roulette",
                description="Not enough players joined the game. Need at least 2 players.",
                color=discord.Color.red(),
            )
            await message.edit(embed=embed)
            return

        # Work out who the draw can actually touch before drawing. A member
        # whose highest role outranks the bot's cannot be timed out whatever
        # permissions the bot holds, so drawing them and reporting "no
        # permission" was both wrong and untraceable.
        candidates: list[discord.Member] = []
        skipped: list[tuple[str, str]] = []
        for candidate in users:
            member = ctx.guild.get_member(candidate.id)
            if member is None:
                try:
                    member = await ctx.guild.fetch_member(candidate.id)
                except (discord.NotFound, discord.HTTPException):
                    skipped.append((candidate.display_name, "they left the server"))
                    continue

            reason = timeout_blocked_reason(ctx.guild, member)
            if reason:
                skipped.append((member.display_name, reason))
                continue
            candidates.append(member)

        timeout_duration = random.randint(60, 600)  # 1-10 minutes
        victim = None

        random.shuffle(candidates)

        # Draw from the players the bot can act on. If Discord still refuses
        # (a channel-level denial, or a role moved mid-game) try the next one.
        for member in candidates:
            try:
                await member.timeout(
                    discord.utils.utcnow() + timedelta(seconds=timeout_duration),
                    reason="Lost Timeout Roulette",
                )
            except discord.HTTPException as e:
                refused = getattr(e, "text", None) or str(e)
                logger.warning(
                    f"Timeout Roulette could not time out {member.id}: {e}"
                )
                skipped.append((member.display_name, f"Discord refused it ({refused})"))
                continue

            victim = member
            break

        if victim is None:
            embed = discord.Embed(
                title="Timeout Roulette",
                description=timeout_blocked_note(ctx.guild),
                color=discord.Color.red(),
            )
            if skipped:
                embed.set_footer(text="Passed over: " + describe_skipped(skipped))
            await message.edit(embed=embed)
            return

        embed = discord.Embed(
            title="Timeout Roulette",
            description=(
                f"{victim.mention} has been timed out for "
                f"{timeout_duration // 60} minutes and "
                f"{timeout_duration % 60} seconds! ⏰"
            ),
            color=discord.Color.orange(),
        )
        if skipped:
            embed.set_footer(
                text="Passed over (out of reach): " + describe_skipped(skipped)
            )
        await message.edit(embed=embed)

    @commands.hybrid_command(
        name="randompick",
        aliases=["pick", "choose"],
        description="Pick one option at random from a comma-separated list.",
    )
    @unified_cooldown(3)
    async def random_pick(self, ctx: Context, *, options: str = "") -> None:
        """Pick one option at random from a comma-separated list.

        Usage: `!random one,two,three,four,five`.
        """
        items = [part.strip() for part in options.split(",") if part.strip()]
        if len(items) < 2:
            await Embeds.error(
                ctx,
                "Give me at least two options separated by commas, "
                "e.g. `!random one,two,three`.",
                title="Not enough options",
                reply=True,
            )
            return

        chosen = random.choice(items)
        color = (
            discord.Color.blurple()
            if isinstance(ctx.channel, discord.DMChannel)
            else (ctx.author.top_role.color or discord.Color.blurple())
        )
        embed = discord.Embed(
            title="🎲 Random pick",
            description=f"**{chosen}**",
            color=color,
        )
        embed.set_footer(text=f"Chosen from {len(items)} option(s)")
        await ctx.reply(embed=embed)


async def setup(bot) -> None:
    await bot.add_cog(Fun(bot))
    logger.debug("Fun cog initialized successfully")
