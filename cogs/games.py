import os
import random
import discord
import aiohttp
import asyncio
import logging
from discord import app_commands
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional
from discord_games import button_games
from discord_games.button_games.hangman_buttons import HangmanView
from discord_games.button_games.wordle_buttons import WordleView
from discord_games.utils import DEFAULT_COLOR as GAMES_DEFAULT_COLOR
from discord.ext import commands
from discord.ext.commands import Context
from discord.ui import View, button, Button
from config import EMBED_COLORS
from utils.core.economy import format_cash, parse_amount
from utils.misc import MiscUtils
from utils.cooldown import unified_cooldown
from utils.embeds import Embeds
from utils.guardrails import check_slash_guardrails

logger = logging.getLogger("discord.client")


class RateLimitError(Exception):
    def __init__(self, retry_after: int):
        super().__init__("Rate limited")
        self.retry_after = retry_after

#: How long the challenger has to send their word over DMs.
HANGMAN_WORD_TIMEOUT = 120.0
#: A word has to be letters only and within these bounds so it stays guessable.
HANGMAN_MIN_LEN = 3
HANGMAN_MAX_LEN = 32

#: How long the challenger has to send their word over DMs.
CUSTOMWORDLE_WORD_TIMEOUT = 120.0
#: Wordle only ever accepts 5-letter words.
CUSTOMWORDLE_WORD_LEN = 5

#: Russian roulette: one live round in a revolver, pulled on alternate turns.
ROULETTE_CHAMBERS = 6
#: Smallest stake, so a duel is never decided over pocket lint.
ROULETTE_MIN_BET = 10
#: Seconds the challenged player has to accept, and each turn has to pull.
ROULETTE_CHALLENGE_SECONDS = 60.0
ROULETTE_TURN_SECONDS = 60.0
#: How long the "click..." beat sits before the result is revealed.
ROULETTE_SUSPENSE_SECONDS = 0.9


def build_roulette_cylinder(chambers: int = ROULETTE_CHAMBERS) -> list[bool]:
    """A fresh cylinder with exactly one live round in it."""
    cylinder = [False] * max(1, int(chambers))
    cylinder[random.randrange(len(cylinder))] = True
    return cylinder


def roulette_pull(cylinder: list[bool], position: int) -> bool:
    """Whether the trigger pull at *position* fires the live round."""
    if not cylinder or position < 0 or position >= len(cylinder):
        return False
    return bool(cylinder[position])


@dataclass
class RouletteGame:
    """One 1v1 Russian roulette duel and the money staked on it."""

    channel_id: int
    challenger: discord.Member
    opponent: discord.Member
    stake: Decimal
    cylinder: list[bool]
    position: int = 0
    turn_id: Optional[int] = None
    message: Optional[discord.Message] = None
    future: Optional[asyncio.Future] = None
    #: Set once the challenge is accepted or declined, so it can't happen twice.
    started: bool = False
    #: Set once the pot is paid out or the stakes are refunded.
    settled: bool = False


class _MultiplayerHangman(button_games.BetaHangman):
    """Two-player Hangman: one member sets the word, the tagged member guesses.

    ``BetaHangman.start`` always deals the game to ``ctx.author``; this deals
    it to the tagged member instead, so the challenger picks the word and the
    other player does all the guessing.
    """

    def __init__(self, word: str | None, guesser: discord.Member) -> None:
        super().__init__(word=word)
        self.guesser = guesser

    async def start(self, ctx, *, embed_color=None, timeout=None, **kwargs):
        self.player = self.guesser
        self.embed_color = embed_color or GAMES_DEFAULT_COLOR
        self.view = HangmanView(self, timeout=timeout)
        self.message = await ctx.send(
            embed=self.initialize_embed(), view=self.view, **kwargs
        )
        await self.view.wait()
        return self.message


class _MultiplayerWordle(button_games.BetaWordle):
    """Two-player Wordle: one member sets the word, the tagged member guesses.

    ``BetaWordle.start`` always deals the game to ``ctx.author``; this deals
    it to the tagged member instead, so the challenger picks the word and the
    other player does all the guessing.
    """

    def __init__(self, word: str | None, guesser: discord.Member) -> None:
        super().__init__(word=word)
        self.guesser = guesser
        # A custom word may not be in the built-in dictionary, so accept it as
        # a guess — otherwise the challenger's own word would be rejected.
        if word is not None:
            self._valid_words = self._valid_words + (word.lower(),)

    async def start(self, ctx, *, embed_color=None, timeout=None, **kwargs):
        self.embed_color = embed_color or GAMES_DEFAULT_COLOR
        self.player = self.guesser

        buf = await self.render_image()
        embed = discord.Embed(title="Wordle!", color=self.embed_color)
        embed.set_image(url="attachment://wordle.png")

        self.view = WordleView(self, timeout=timeout)
        self.message = await ctx.send(
            embed=embed,
            file=discord.File(buf, "wordle.png"),
            view=self.view,
            **kwargs,
        )
        await self.view.wait()
        return self.message


class _RouletteChallengeView(discord.ui.View):
    """Accept / decline buttons on a roulette challenge."""

    def __init__(self, cog: "Games", game: RouletteGame) -> None:
        super().__init__(timeout=ROULETTE_CHALLENGE_SECONDS)
        self.cog = cog
        self.game = game

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await check_slash_guardrails(self.cog, interaction)

    @discord.ui.button(label="Accept the bet", style=discord.ButtonStyle.success, emoji="🔫")
    async def accept(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await self.cog._accept_roulette(interaction, self.game, self)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.secondary)
    async def decline(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await self.cog._decline_roulette(interaction, self.game, self)

    async def on_timeout(self) -> None:
        game = self.game
        if game.started:
            return
        game.started = True
        self.cog.roulette_games.pop(game.channel_id, None)
        if game.message is None:
            return
        try:
            await game.message.edit(
                embed=discord.Embed(
                    title="Russian Roulette — no answer",
                    description=(
                        f"{game.opponent.mention} didn't accept in time, so "
                        "nothing was staked."
                    ),
                    color=EMBED_COLORS["warning"],
                ),
                view=None,
            )
        except discord.DiscordException:
            pass


class _RoulettePullView(discord.ui.View):
    """The single button whoever is on the spot has to press."""

    def __init__(self, cog: "Games", game: RouletteGame) -> None:
        super().__init__(timeout=ROULETTE_TURN_SECONDS + 5)
        self.cog = cog
        self.game = game

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await check_slash_guardrails(self.cog, interaction)

    @discord.ui.button(
        label="Pull the trigger", style=discord.ButtonStyle.danger, emoji="🔫"
    )
    async def pull(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        game = self.game
        if interaction.user.id != game.turn_id:
            return await interaction.response.send_message(
                "It's not your turn — wait for the other player.", ephemeral=True
            )
        future = game.future
        if future is None or future.done():
            return await interaction.response.send_message(
                "The cylinder is already turning.", ephemeral=True
            )
        await interaction.response.defer()
        future.set_result(interaction)


class Games(commands.Cog, name="Games"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.utils = MiscUtils(self)
        #: Live roulette duels, one per channel, plus their background loops.
        self.roulette_games: dict[int, RouletteGame] = {}
        self._tasks: set[asyncio.Task] = set()

    def cog_unload(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self.roulette_games.clear()

    def _spawn(self, coro) -> asyncio.Task:
        """Run *coro* in the background and keep it alive until the cog unloads."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)

        def _done(finished: asyncio.Task) -> None:
            self._tasks.discard(finished)
            if finished.cancelled():
                return
            error = finished.exception()
            if error is not None:
                logger.error("Game task failed", exc_info=error)

        task.add_done_callback(_done)
        return task

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await check_slash_guardrails(self, interaction)

    @commands.hybrid_command(name="rps", description="Play Rock Paper Scissors.")
    @unified_cooldown(5)
    async def rps(self, ctx: Context[commands.Bot], player: discord.User = None):
        game = button_games.BetaRockPaperScissors(player)
        await game.start(ctx)

    @commands.hybrid_command(
        name="tictactoe", aliases=["ttt"], description="Play Tic-Tac-Toe with another user."
    )
    @unified_cooldown(5)
    async def tictactoe(self, ctx: Context[commands.Bot], member: discord.User):
        await ctx.defer()
        game = button_games.BetaTictactoe(cross=ctx.author, circle=member)
        await game.start(ctx)

    @commands.hybrid_command(name="wordle", description="Play a game of Wordle.")
    @unified_cooldown(5)
    async def wordle(self, ctx: Context[commands.Bot]):
        await ctx.defer()
        game = button_games.BetaWordle()
        await game.start(ctx)

    @commands.hybrid_command(
        name="customwordle",
        aliases=["cwordle"],
        description="Challenge someone to Wordle — pick the word in DMs, they guess.",
    )
    @commands.guild_only()
    @unified_cooldown(5)
    async def customwordle(
        self, ctx: Context[commands.Bot], member: discord.Member = None
    ):
        """Two-player Wordle against the member you tag.

        You pick the secret 5-letter word over DMs so the other player never
        sees it, and the tagged member has to guess it on the message. Send
        `random` in DMs to let the bot choose instead.
        """
        await ctx.defer()

        if member is None:
            await ctx.reply(
                "Tag the member you want to guess: `!customwordle @member` — "
                "I'll DM you to set the 5-letter word."
            )
            return
        if member.bot:
            await ctx.reply("Bots can't play Wordle. Pick a member.")
            return
        if member.id == ctx.author.id:
            await ctx.reply("You can't challenge yourself. Tag someone else.")
            return

        try:
            dm = await ctx.author.create_dm()
            await dm.send(
                f"🎯 **Wordle** — pick the {CUSTOMWORDLE_WORD_LEN}-letter word "
                f"for **{member.display_name}** to guess in {ctx.channel.mention}.\n"
                f"Reply with one {CUSTOMWORDLE_WORD_LEN}-letter word, or `random` "
                f"for a bot-chosen word. "
                f"You have {int(CUSTOMWORDLE_WORD_TIMEOUT)} seconds."
            )
        except discord.Forbidden:
            await ctx.reply(
                "I couldn't DM you, so the word would be public. Allow DMs from "
                "server members and try again."
            )
            return

        def is_word_dm(message: discord.Message) -> bool:
            return (
                message.author.id == ctx.author.id
                and isinstance(message.channel, discord.DMChannel)
            )

        word: str | None = None
        for _ in range(3):
            try:
                message = await self.bot.wait_for(
                    "message",
                    check=is_word_dm,
                    timeout=CUSTOMWORDLE_WORD_TIMEOUT,
                )
            except asyncio.TimeoutError:
                await dm.send("⏱️ Timed out — no word was picked, so nothing started.")
                return

            choice = message.content.strip()
            if choice.lower() in {"random", "skip", "bot"}:
                word = None
                break
            if choice.isalpha() and len(choice) == CUSTOMWORDLE_WORD_LEN:
                word = choice.lower()
                break
            await dm.send(
                f"That's not usable — send a single "
                f"{CUSTOMWORDLE_WORD_LEN}-letter word, or `random`."
            )
        else:
            await dm.send("Too many unusable words — nothing started.")
            return

        await dm.send("✅ Got it. Starting the round now — good luck!")
        await ctx.send(
            "# 🟩 Wordle\n"
            f"{ctx.author.mention} picked a word — {member.mention}, it's all on you!"
        )

        game = _MultiplayerWordle(word, member)
        await game.start(ctx)

    @commands.command(name="memory", description="Play the memory matching game.")
    async def memory_game(self, ctx: Context[commands.Bot]):
        game = button_games.MemoryGame()
        await game.start(ctx)

    @commands.hybrid_command(
        name="hangman",
        aliases=["hm"],
        description="Challenge someone to Hangman — pick the word in DMs, they guess.",
    )
    @commands.guild_only()
    @unified_cooldown(5)
    async def hangman(self, ctx: Context[commands.Bot], member: discord.Member = None):
        """Two-player Hangman against the member you tag.

        You pick the secret word over DMs so the other player never sees it, and
        the tagged member has to guess it on the message. Send `random` in DMs
        to let the bot choose instead.
        """
        await ctx.defer()

        if member is None:
            await ctx.reply(
                "Tag the member you want to guess: `!hangman @member` — I'll DM "
                "you to set the word."
            )
            return
        if member.bot:
            await ctx.reply("Bots can't play Hangman. Pick a member.")
            return
        if member.id == ctx.author.id:
            await ctx.reply("You can't challenge yourself. Tag someone else.")
            return

        try:
            dm = await ctx.author.create_dm()
            await dm.send(
                f"🎯 **Hangman** — pick the word for **{member.display_name}** to "
                f"guess in {ctx.channel.mention}.\n"
                f"Reply with one word ({HANGMAN_MIN_LEN}-{HANGMAN_MAX_LEN} letters "
                f"only), or `random` for a bot-chosen word. "
                f"You have {int(HANGMAN_WORD_TIMEOUT)} seconds."
            )
        except discord.Forbidden:
            await ctx.reply(
                "I couldn't DM you, so the word would be public. Allow DMs from "
                "server members and try again."
            )
            return

        def is_word_dm(message: discord.Message) -> bool:
            return (
                message.author.id == ctx.author.id
                and isinstance(message.channel, discord.DMChannel)
            )

        word: str | None = None
        for _ in range(3):
            try:
                message = await self.bot.wait_for(
                    "message", check=is_word_dm, timeout=HANGMAN_WORD_TIMEOUT
                )
            except asyncio.TimeoutError:
                await dm.send("⏱️ Timed out — no word was picked, so nothing started.")
                return

            choice = message.content.strip()
            if choice.lower() in {"random", "skip", "bot"}:
                word = None
                break
            if choice.isalpha() and HANGMAN_MIN_LEN <= len(choice) <= HANGMAN_MAX_LEN:
                word = choice.lower()
                break
            await dm.send(
                f"That's not usable — send a single word of {HANGMAN_MIN_LEN}-"
                f"{HANGMAN_MAX_LEN} letters, or `random`."
            )
        else:
            await dm.send("Too many unusable words — nothing started.")
            return

        await dm.send("✅ Got it. Starting the round now — good luck!")
        await ctx.send(
            "# 🎯 Hangman\n"
            f"{ctx.author.mention} picked a word — {member.mention}, it's all on you!"
        )

        game = _MultiplayerHangman(word, member)
        await game.start(ctx)

        if not game.game_over:
            await ctx.send(f"# ⏱️ Hangman ended\n> The word was **{game.word}**.")
        elif r"\_" not in game.correct:
            await ctx.send(
                f"# 🏆 {member.mention} guessed it!\n> The word was **{game.word}**."
            )
        else:
            await ctx.send(
                f"# 💀 {member.mention} ran out of lives\n> The word was **{game.word}**."
            )

    @commands.hybrid_command(
        name="connect4", aliases=["c4"], description="Play a game of Connect 4."
    )
    @unified_cooldown(5)
    async def connect4(self, ctx: Context[commands.Bot], member: discord.User):
        await ctx.defer()
        game = button_games.BetaConnectFour(red=ctx.author, blue=member)
        await game.start(ctx)

    @commands.hybrid_command(
        name="battleship", aliases=["bs"], description="Play a game of Battleship."
    )
    @unified_cooldown(5)
    async def battleship(self, ctx: Context[commands.Bot], member: discord.User):
        await ctx.defer()
        game = button_games.BetaBattleShip(player1=ctx.author, player2=member)
        await game.start(ctx)

    @commands.hybrid_command(
        name="coinflip", aliases=["cf"], description="Flip a coin!"
    )
    @unified_cooldown(3)
    async def coinflip(self, ctx: Context):
        """Flip a coin, not really much else to it."""
        frames = [
            "<:coin:1359823671581085847>", "🪙",
            "<:coin:1359823671581085847>", "🪙",
            "<:coin:1359823671581085847>", "🪙",
            "<:coin:1359823671581085847>", "🪙",
            "<:coin:1359823671581085847>", "🪙",
            "<:coin:1359823671581085847>", "🪙",
        ]
        embed = discord.Embed(description="Flipping...", color=discord.Color.gold())
        msg = await ctx.reply(embed=embed)

        for i, frame in enumerate(frames):
            # slow down towards the end to simulate the coin losing momentum cuz thats fkn tuff
            delay = 0.2 + (i * 0.07)
            await asyncio.sleep(delay)
            embed.description = f"{frame} Flipping..."
            await msg.edit(embed=embed)

        result = random.choice(["Heads", "Tails"])
        emoji = "<:coin:1359823671581085847>" if result == "Heads" else "🪙"
        embed.description = f"{emoji} **{result}!**"
        embed.color = discord.Color.green()
        await msg.edit(embed=embed)

    # ==================================================================
    # Russian roulette
    # ==================================================================

    @commands.hybrid_command(
        name="russianroulette",
        aliases=["russianrou", "rr"],
        description="1v1 Russian roulette — both players stake, the winner takes the pot.",
    )
    @app_commands.describe(
        member="The member you want to duel",
        amount="What to stake — 500, 1.5k, 25% or all",
    )
    @commands.guild_only()
    @unified_cooldown(10)
    async def russianroulette(
        self,
        ctx: Context[commands.Bot],
        member: discord.Member = None,
        amount: str = None,
    ):
        """Challenge one member to a duel: one live round, alternate pulls.

        Both wallets are staked the moment the challenge is accepted and the
        survivor takes the whole pot. Whoever runs out of time on their turn
        forfeits, so a duel can't sit open forever.
        """
        if member is None or amount is None:
            return await ctx.reply(
                "Usage: `!russianroulette @member <bet>` — e.g. "
                "`!russianroulette @sesh 500`."
            )
        if member.bot:
            return await ctx.reply("Bots don't have a head to risk. Pick a member.")
        if member.id == ctx.author.id:
            return await ctx.reply("You can't duel yourself. Tag someone else.")
        if ctx.channel.id in self.roulette_games:
            return await ctx.reply(
                "A duel is already running in this channel — finish it first."
            )

        balance = int(
            await self.bot.database.get_wallet_balance_by_user_id(ctx.author.id)
        )
        stake = parse_amount(amount, balance) or 0
        requested = parse_amount(amount, 0) or 0
        if stake <= 0:
            return await ctx.reply(
                "That's not a real bet — try `500`, `1.5k`, `all` or `25%`."
            )
        if stake < ROULETTE_MIN_BET:
            return await ctx.reply(
                f"The smallest bet is {format_cash(ROULETTE_MIN_BET)}."
            )
        if requested > balance:
            return await ctx.reply(
                f"You only have {format_cash(balance)} in your wallet, so you "
                f"can't stake {format_cash(requested)}."
            )

        game = RouletteGame(
            channel_id=ctx.channel.id,
            challenger=ctx.author,
            opponent=member,
            stake=Decimal(stake),
            cylinder=build_roulette_cylinder(),
        )
        self.roulette_games[ctx.channel.id] = game
        try:
            game.message = await ctx.send(
                embed=self._roulette_challenge_embed(game),
                view=_RouletteChallengeView(self, game),
            )
        except discord.DiscordException:
            self.roulette_games.pop(ctx.channel.id, None)
            logger.exception("Could not post the roulette challenge")

    def _roulette_challenge_embed(self, game: RouletteGame) -> discord.Embed:
        return discord.Embed(
            title="Russian Roulette — 1v1",
            description=(
                f"{game.challenger.mention} challenges {game.opponent.mention}!\n\n"
                f"**Stake:** {format_cash(game.stake)} each\n"
                f"**Pot:** {format_cash(game.stake * 2)} to the survivor\n\n"
                f"{game.opponent.mention}, accept within "
                f"{int(ROULETTE_CHALLENGE_SECONDS)}s and both stakes are "
                "locked in.\n"
                f"-# {ROULETTE_CHAMBERS} chambers, 1 live round, alternate pulls."
            ),
            color=EMBED_COLORS["info"],
        )

    @staticmethod
    def _roulette_cylinder_line(
        game: RouletteGame, *, suspense: bool = False, reveal: bool = False
    ) -> str:
        pips: list[str] = []
        for index in range(len(game.cylinder)):
            if index < game.position:
                pips.append("💥" if reveal and game.cylinder[index] else "⚫")
            elif index == game.position:
                pips.append("❔" if not suspense else "💨")
            else:
                pips.append("⚪")
        return " ".join(pips)

    def _roulette_turn_embed(
        self, game: RouletteGame, *, suspense: bool = False, note: str = ""
    ) -> discord.Embed:
        current = game.opponent if game.turn_id == game.opponent.id else game.challenger
        description = (
            f"{game.challenger.mention} **vs** {game.opponent.mention}\n"
            f"**Pot:** {format_cash(game.stake * 2)}  ·  "
            f"**Pulls:** {game.position}\n\n"
            f"`{self._roulette_cylinder_line(game, suspense=suspense)}`\n\n"
            f"{current.mention}, it's on you."
        )
        if note:
            description += f"\n\n{note}"
        return discord.Embed(
            title="Russian Roulette — click…" if suspense else "Russian Roulette",
            description=description,
            color=EMBED_COLORS["warning"],
        )

    @staticmethod
    def _roulette_other(game: RouletteGame, player_id: int) -> discord.Member:
        return game.opponent if player_id == game.challenger.id else game.challenger

    async def _accept_roulette(
        self,
        interaction: discord.Interaction,
        game: RouletteGame,
        view: _RouletteChallengeView,
    ) -> None:
        if interaction.user.id != game.opponent.id:
            return await interaction.response.send_message(
                "This challenge isn't yours to accept.", ephemeral=True
            )
        if game.started:
            return await interaction.response.send_message(
                "This duel has already been answered.", ephemeral=True
            )
        game.started = True
        view.stop()
        await interaction.response.defer()

        error = await self._roulette_take_stakes(game)
        if error is not None:
            self.roulette_games.pop(game.channel_id, None)
            if game.message is not None:
                try:
                    await game.message.edit(
                        embed=discord.Embed(
                            title="Russian Roulette — cancelled",
                            description=error,
                            color=EMBED_COLORS["warning"],
                        ),
                        view=None,
                    )
                except discord.DiscordException:
                    pass
            return

        game.turn_id = random.choice([game.challenger.id, game.opponent.id])
        self._spawn(self._run_roulette(game))

    async def _decline_roulette(
        self,
        interaction: discord.Interaction,
        game: RouletteGame,
        view: _RouletteChallengeView,
    ) -> None:
        if interaction.user.id != game.opponent.id:
            return await interaction.response.send_message(
                "This challenge isn't yours to decline.", ephemeral=True
            )
        if game.started:
            return await interaction.response.send_message(
                "This duel has already been answered.", ephemeral=True
            )
        game.started = True
        view.stop()
        self.roulette_games.pop(game.channel_id, None)
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Russian Roulette — declined",
                description=(
                    f"{game.opponent.mention} passed on the duel. "
                    "No money moved."
                ),
                color=EMBED_COLORS["neutral"],
            ),
            view=None,
        )

    async def _roulette_take_stakes(self, game: RouletteGame) -> str | None:
        """Lock both stakes; returns an error message if that isn't possible."""
        db = self.bot.database
        taken: list[str] = []
        error: str | None = None
        for player in (game.challenger, game.opponent):
            try:
                wallet_id = await db.get_wallet_id_for_user(player.id)
                await db.spend_from_wallet(
                    wallet_id=wallet_id,
                    amount=game.stake,
                    description="Russian Roulette stake",
                )
            except ValueError:
                error = (
                    f"{player.mention} doesn't have "
                    f"{format_cash(game.stake)} in their wallet, so the duel "
                    "can't start."
                )
                break
            except Exception:
                logger.exception("Could not take a roulette stake")
                error = (
                    "The duel couldn't be set up, so the stakes were handed "
                    "back."
                )
                break
            taken.append(wallet_id)

        if error is None:
            return None
        for paid_wallet in taken:
            try:
                await db.process_treasury_transaction(
                    wallet_id=paid_wallet,
                    amount=game.stake,
                    description="Russian Roulette refund",
                    transaction_type="game_payout",
                )
            except Exception:
                logger.exception("Could not refund a roulette stake")
        return error

    async def _roulette_release(self, game: RouletteGame) -> None:
        """Hand both stakes back after a duel that never paid out."""
        if game.settled:
            return
        game.settled = True
        db = self.bot.database
        for player in (game.challenger, game.opponent):
            try:
                wallet_id = await db.get_wallet_id_for_user(player.id)
                await db.process_treasury_transaction(
                    wallet_id=wallet_id,
                    amount=game.stake,
                    description="Russian Roulette refund",
                    transaction_type="game_payout",
                )
            except Exception:
                logger.exception(
                    "Could not refund %s's roulette stake", getattr(player, "id", "?")
                )

    async def _roulette_award(self, game: RouletteGame, winner: discord.Member) -> bool:
        """Pay the whole pot to *winner*; refund both if that fails."""
        if game.settled:
            return False
        db = self.bot.database
        try:
            wallet_id = await db.get_wallet_id_for_user(winner.id)
            await db.process_treasury_transaction(
                wallet_id=wallet_id,
                amount=game.stake * 2,
                description="Russian Roulette payout",
                transaction_type="game_payout",
            )
        except Exception:
            logger.exception("Could not pay out the roulette pot")
            await self._roulette_release(game)
            return False
        game.settled = True
        return True

    async def _run_roulette(self, game: RouletteGame) -> None:
        message = game.message
        if message is None:
            self.roulette_games.pop(game.channel_id, None)
            return
        try:
            while True:
                shooter = (
                    game.opponent
                    if game.turn_id == game.opponent.id
                    else game.challenger
                )
                view = _RoulettePullView(self, game)
                try:
                    await message.edit(
                        embed=self._roulette_turn_embed(
                            game,
                            note=f"-# {int(ROULETTE_TURN_SECONDS)}s to pull.",
                        ),
                        view=view,
                    )
                except discord.DiscordException:
                    logger.exception("Could not update the roulette message")

                game.future = asyncio.get_running_loop().create_future()
                try:
                    interaction = await asyncio.wait_for(
                        game.future, timeout=ROULETTE_TURN_SECONDS
                    )
                except asyncio.TimeoutError:
                    interaction = None
                game.future = None
                view.stop()

                if interaction is None:
                    winner = self._roulette_other(game, shooter.id)
                    paid = await self._roulette_award(game, winner)
                    await message.edit(
                        embed=discord.Embed(
                            title="Russian Roulette — forfeit",
                            description=(
                                f"{shooter.mention} took too long to pull, so "
                                "they lose by default.\n\n"
                                + (
                                    f"**{winner.mention} wins "
                                    f"{format_cash(game.stake * 2)}.**"
                                    if paid
                                    else "The pot couldn't be paid out, so "
                                    "both stakes were refunded."
                                )
                            ),
                            color=EMBED_COLORS["warning"],
                        ),
                        view=None,
                    )
                    return

                fired = roulette_pull(game.cylinder, game.position)
                await message.edit(
                    embed=self._roulette_turn_embed(game, suspense=True),
                    view=None,
                )
                await asyncio.sleep(ROULETTE_SUSPENSE_SECONDS)
                game.position += 1

                if fired:
                    winner = self._roulette_other(game, shooter.id)
                    paid = await self._roulette_award(game, winner)
                    await message.edit(
                        embed=discord.Embed(
                            title="Russian Roulette — 💥 bang",
                            description=(
                                f"`{self._roulette_cylinder_line(game, reveal=True)}`"
                                f"\n\n{shooter.mention} clicked on the live "
                                "round.\n\n"
                                + (
                                    f"**{winner.mention} wins "
                                    f"{format_cash(game.stake * 2)}.**"
                                    if paid
                                    else "The pot couldn't be paid out, so "
                                    "both stakes were refunded."
                                )
                            ),
                            color=EMBED_COLORS["error"]
                            if paid
                            else EMBED_COLORS["warning"],
                        ),
                        view=None,
                    )
                    return

                game.turn_id = self._roulette_other(game, shooter.id).id
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Russian roulette failed")
            await self._roulette_release(game)
            try:
                await message.edit(
                    embed=discord.Embed(
                        title="Russian Roulette — stopped",
                        description=(
                            "The duel hit an error, so both stakes were "
                            "refunded."
                        ),
                        color=EMBED_COLORS["error"],
                    ),
                    view=None,
                )
            except discord.DiscordException:
                pass
        finally:
            # A reload or shutdown cancels the loop mid-duel; the stakes are
            # already out of both wallets, so they have to go back.
            if not game.settled:
                try:
                    await self._roulette_release(game)
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("Could not refund a stopped roulette duel")
            self.roulette_games.pop(game.channel_id, None)


async def setup(bot):
    await bot.add_cog(Games(bot))
