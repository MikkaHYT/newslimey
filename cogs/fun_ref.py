"""Fun: the silly member metres.

    ,gay [@member]      how gay they are - the AI judges it from their recent chat
    ,pussy [@member]    how much of a pussy they are - AI judged from their chat
    ,dih [@member]      their dih - sized by the AI from their recent chat
    ,makeout @member    you made out with them, tongue or not, at random
    ,grope @member      you groped them - where is picked at random
    ,fuck @member       you fucked them - the AI writes the scene, fresh each time
    ,ship @a [@b]       how compatible two members are - AI judged from their chat
    ,fight @member      challenge them - they accept, then you brawl
    ,requestfight @a @b let two tagged members fight without typing anything
    ,fighttop [count]   the members with the most fight wins

The metres are judged live: every use re-reads as much of the member's recent
chat as it can find and asks the AI for a new number, so the verdict follows the
conversation instead of freezing the first time somebody asked. Run ``,gay`` twice
and you can get two different answers - that is the point, not a bug.
The ``8==D`` drawing is built from whatever number came back - nothing is
remembered between runs.
When the AI is unavailable the number falls back to a plain random draw.

``,ship`` hands the AI both members' recent chat at once, so the score reacts to
the *pair* rather than to one of them - and it is re-judged on every run too.

``,makeout`` is drawn fresh each time as well: a kiss either has tongue or it
doesn't, and it is funnier that way.

``,fuck`` is not stored either, but it is no longer assembled from separate
random pools - the AI writes the whole scene in one go, so the place, the pose,
the noise and the aftermath all belong together (one location, only objects that
actually live there) and recently used scenes are avoided. The old pools are kept
as the fallback for when the AI gives nothing back.

``,fight`` is a duel played out on the message itself: whoever is named answers
with a button, then each round both fighters pick punch, block or parry and the
round only resolves once the second pick lands. A block stops a punch outright,
so a guarded fighter is safe rather than chipped at — every now and then the
guard slips anyway and the punch lands in full. A parry slips past a block but
finds nothing solid behind it, and a punch lands before a parry can set. Two
blocks or two parries are nothing at all; trading punches hurts both, so the
only real way to hurt somebody is to swing while they are not covering.
The survivor takes 1,000 cash. ``,requestfight`` sets the same duel up between
two members a third person names, so neither fighter has to run a command at
all; they just press Accept. Every finished fight is written to both fighters'
records — a win, a loss, or a draw apiece — which ``,fighttop`` ranks.
"""

from __future__ import annotations

import logging
import random
from collections import deque
from typing import Deque, Optional, Sequence

import discord
from discord.ext import commands

from config import EMBED_COLOR, EMBED_COLORS, PREFIX
from utils.core.economy import format_cash
from utils.core.helpers import fmtseconds
from utils.core.views import SimpleLayout, swap_layout_message, usage_card

logger = logging.getLogger("slimey.fun")

#: What each judged kind is measured inside.
PERCENT_RANGE = (0, 100)
DIH_RANGE = (1, 14)

#: Width of the ██ bar drawn under the percentage metres.
BAR_WIDTH = 12

#: Accent for the ship card - Discord's own pink.
SHIP_COLOR = discord.Color.from_str("#eb459e")

#: Accent for the grope card.
GROPE_COLOR = discord.Color.from_str("#f0a020")

#: Where a ``,grope`` lands, picked fresh every time.
GROPE_SPOTS: tuple[str, ...] = ("arm", "shoulder", "waist", "thigh", "butt", "cheek")

#: Accent for the fuck card.
FUCK_COLOR = discord.Color.from_str("#d6385f")

#: Fallback for when the AI gives nothing back: where a ``,fuck`` happens, picked
#: fresh every time. Every one is somewhere with a reason to be caught.
FUCK_PLACES: tuple[str, ...] = (
    "**the kitchen**, against the counter",
    "**the bedroom**, the door not even shut",
    "**the shower**, water still running",
    "**the hallway**, on the coat rack",
    "**the living room**, on the sofa, the telly left on",
    "**the bathroom**, the bath abandoned, water long cold",
    "**the back of the car**, in a lay-by",
    "**the office**, Friday afternoon, lights off",
    "**the store cupboard**, which is genuinely too small for this",
    "**the laundry room**, among the clean towels",
    "**the staircase**, which everyone warns you about",
    "**the garage**, midnight, freezing, absolutely ridiculous",
    "**the great outdoors**, in the rain",
    "**the changing room**, which was locked from the inside",
    "**the dining room**, on the table, which will be missed at breakfast",
)

#: Fallback: how it's actually happening - the position and the pace. These are
#: paired with a place at random, so nothing here may name furniture or a room
#: of its own: "back arched off the mattress" cannot follow "in the shower".
FUCK_ACTIONS: tuple[str, ...] = (
    "bent over it, taken from behind, hands braced flat and head down",
    "pinned face-down, hips in the air, legs spread wide and shaking",
    "on their back, knees hooked over your shoulders, ankles crossed tight",
    "riding you slow and steady, both hands flat on your chest, watching you",
    "leaning back into your chest, rolling their hips, hair in their face",
    "splayed out and open, back arched, unable to look away",
    "pressed up against the wall, legs wrapped around you, nowhere to go",
    "on their knees, looking up the whole time, absolutely not quiet about it",
    "wrists held above their head, arching into every bit of it",
    "sideways-on, one leg hooked over yours, no room to build up a rhythm",
    "on their front with it taken from behind, face buried to muffle every sound",
    "flat on their back, legs up and trembling, nails digging half-moons into your shoulders",
)

#: Fallback: what came out of their mouths. Kept as narration on purpose - a
#: wall of asterisks is not funnier than a sentence.
FUCK_NOISES: tuple[str, ...] = (
    "loud enough that the neighbours absolutely heard",
    "whining into a pillow that was not doing its job",
    "breathing gone ragged before a word was ever said",
    "moaning loud and shameless, zero volume control whatsoever",
    "half-covering their own mouth and failing completely",
    "making noises neither of them knew they could make",
    "a low, ruined sound on every single stroke",
    "screaming into the cushion, claws down your back",
    "gasping your name like it was the only word left in the language",
    "so loud the dog started barking at the door",
    "crying out, echoing off everything around them",
    "entirely incoherent, just noise and nodding",
)

#: Fallback: the closing note - what the room looked like afterwards.
FUCK_AFTERMATH: tuple[str, ...] = (
    "afterwards, they lay there completely wrecked, staring at the ceiling",
    "afterwards, neither of them could move for a few minutes",
    "afterwards, they made tea and pretended it hadn't happened",
    "afterwards, the whole thing had to be wiped down, twice",
    "afterwards, they lay there, sweaty and stupidly happy",
    "afterwards, one of them asked \"so... again?\"",
    "afterwards, they fell asleep in the exact position they were left in",
    "afterwards, three rounds, and a fourth neither admitted to starting",
    "afterwards, slower than the first time and more tender for it",
    "afterwards, faster than either of them planned - under four minutes, total",
    "afterwards, legs completely gone, walking anywhere firmly out of the question",
    "afterwards, a handprint on one hip and no explanation offered",
)

#: Judged kind -> the range the AI may answer in.
KINDS: dict[str, tuple[int, int]] = {
    "gay": PERCENT_RANGE,
    "pussy": PERCENT_RANGE,
    "dih": DIH_RANGE,
}

#: How many recent ``,fuck`` scenes are kept in memory so the AI never repeats
#: one. This only lives for as long as the process does, which is enough when
#: the pool of places it draws from is enormous.
FUCK_SCENE_MEMORY = 15

#: Cash the winner of a fight walks away with.
FIGHT_PRIZE = 1000

#: Health both fighters start on, and how wide the bar is drawn.
FIGHT_MAX_HP = 100
FIGHT_BAR_WIDTH = 10

#: How long an unanswered fight request sits before it expires.
FIGHT_TIMEOUT = 180.0

#: How many rounds of the fight stay on the card.
FIGHT_LOG_LINES = 4

#: The three moves: ``(id, label, emoji)``.
FIGHT_MOVES: tuple[tuple[str, str, str], ...] = (
    ("punch", "Punch", "🥊"),
    ("block", "Block", "🛡️"),
    ("parry", "Parry", "⚔️"),
)
FIGHT_EMOJI: dict[str, str] = {key: emoji for key, _, emoji in FIGHT_MOVES}

#: Which move gets the better of which: the key beats the value. Punch is the
#: only move that can hurt anybody — it beats a parry outright, stops against a
#: block, and a block or parry between them is just two guards bouncing off
#: each other, so nobody lands anything.
FIGHT_BEATS: dict[str, str] = {"punch": "parry"}

#: What a landed punch deals, ``(low, high)``. Nothing else in the fight hurts,
#: so a fighter who never swings cannot win — they can only stall.
FIGHT_DAMAGE: dict[str, tuple[int, int]] = {
    "punch": (12, 18),
}

#: Chance a block holds absolutely. Usually the guard takes the punch on the
#: arm and nobody is hurt; this often the fist slips past it and the punch
#: lands in full, which is the only chip damage a block ever concedes.
FIGHT_BLOCK_SLIP = 0.25

#: How each exchange reads in the round log, keyed by the two moves picked.
FIGHT_BLOW: dict[tuple[str, str], str] = {
    ("punch", "block"): "the block absorbs the punch cleanly — nothing lands",
    ("block", "punch"): "the block absorbs the punch cleanly — nothing lands",
    ("parry", "block"): "the parry slips past the block, but finds nothing behind it",
    ("block", "parry"): "the parry slips past the block, but finds nothing behind it",
    ("punch", "parry"): "a punch landing before the parry can set",
    ("parry", "punch"): "a punch landing before the parry can set",
}

#: Both fighters picked the same move: ``(low, high, how it reads)``. Trading
#: punches is the only matchup where both sides come off hurt.
FIGHT_TIE: dict[str, tuple[int, int, str]] = {
    "punch": (10, 16, "they trade punches"),
    "block": (0, 0, "both block — nothing lands"),
    "parry": (0, 0, "their parries clang off one another — nothing lands"),
}

#: What the block says when it slips and the punch gets through.
FIGHT_SLIP = "the fist slips past the block and the punch lands anyway"

#: Rounds before a fight with nobody still standing is called a stalemate. Only
#: punches hurt and a block stops three in four, so it takes roughly 27 swings
#: to break a fighter who never does anything else — this is set just past that
#: so a committed puncher always gets the chance to win, while two fighters who
#: just cover up call it a draw instead of sitting on the card forever.
FIGHT_MAX_ROUNDS = 30

#: Most members ``,fighttop`` will show at once.
FIGHT_LEADERBOARD_LIMIT = 25

#: Position badges for the top of the fight leaderboard.
FIGHT_MEDALS = ("🥇", "🥈", "🥉")


def panel(*items: discord.ui.Item, accent: discord.Color = EMBED_COLOR) -> SimpleLayout:
    box = discord.ui.Container(accent_color=accent)
    for item in items:
        box.add_item(item)
    return SimpleLayout(box)


def notice(message: str, *, emoji: str = "❌") -> SimpleLayout:
    """A card that only explains a command, so it doesn't spend a cooldown."""
    return usage_card(message, emoji=emoji)


def container(*items: discord.ui.Item, accent: discord.Color = EMBED_COLOR) -> discord.ui.Container:
    """A bare container for a card that stays on the message (buttons included)."""
    box = discord.ui.Container(accent_color=accent)
    for item in items:
        box.add_item(item)
    return box


def divider() -> discord.ui.Separator:
    return discord.ui.Separator(spacing=discord.SeparatorSpacing.small)


def sentence_case(value: str) -> str:
    """Capitalise the first letter, leaving any leading markdown intact."""
    for index, char in enumerate(value):
        if char.isalpha():
            return value[:index] + char.upper() + value[index + 1 :]
    return value


def without_stop(value: str) -> str:
    """The text with any trailing full stop taken off, so the card adds one."""
    return value.strip().rstrip(".").rstrip()


def meter_bar(percent: int, width: int = BAR_WIDTH) -> str:
    filled = max(0, min(width, round(percent / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def hp_bar(hp: int, maximum: int = FIGHT_MAX_HP, width: int = FIGHT_BAR_WIDTH) -> str:
    """The health bar a fight card draws under each fighter."""
    filled = max(0, min(width, round(max(0, hp) / maximum * width)))
    return "█" * filled + "░" * (width - filled)


def fight_round(first_move: str, second_move: str) -> tuple[int, int, str]:
    """``(first_taken, second_taken, note)`` — the damage each fighter takes.

    Punching into a block is a wasted swing: the guard takes it and nobody is
    hurt, unless the block slips (``FIGHT_BLOCK_SLIP``), in which case the
    punch lands in full. A parry slipping past a block buys nothing, because
    there is nothing left open behind it, and neither move ever beats another
    guard. The same move on both sides is its own outcome — two blocks or two
    parries are nothing at all, and trading punches hurts both fighters.
    """
    if first_move == second_move:
        low, high, note = FIGHT_TIE[first_move]
        return random.randint(low, high), random.randint(low, high), note

    key = (first_move, second_move)

    if set(key) == {"punch", "block"}:
        if random.random() >= FIGHT_BLOCK_SLIP:
            return 0, 0, FIGHT_BLOW[key]
        damage = random.randint(*FIGHT_DAMAGE["punch"])
        if first_move == "punch":
            return 0, damage, FIGHT_SLIP
        return damage, 0, FIGHT_SLIP

    if first_move == "punch":
        return 0, random.randint(*FIGHT_DAMAGE["punch"]), FIGHT_BLOW[key]
    if second_move == "punch":
        return random.randint(*FIGHT_DAMAGE["punch"]), 0, FIGHT_BLOW[key]
    # A block and a parry facing off: the parry slips past, but the only thing
    # behind the block was another block, so neither of them lands anything.
    return 0, 0, FIGHT_BLOW[key]


def dih_art(signs: int) -> str:
    """Render a judged ``=`` count as an ``8==D``."""
    return "8" + "=" * signs + "D"


def rating_label(percent: int) -> str:
    if percent >= 90:
        return "certified"
    if percent >= 70:
        return "very"
    if percent >= 40:
        return "kinda"
    if percent >= 10:
        return "a little"
    return "barely"


def ship_label(percent: int) -> str:
    if percent >= 90:
        return "soulmates 💍"
    if percent >= 70:
        return "a great match 💕"
    if percent >= 40:
        return "worth a shot 💛"
    if percent >= 10:
        return "just friends 🫂"
    return "run 💀"


class _FightButton(discord.ui.Button):
    """A button that hands the click straight back to the fight card."""

    def __init__(self, owner: "FightView", action: str, **kwargs) -> None:
        self.owner = owner
        self.action = action
        super().__init__(**kwargs)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.owner.press(interaction, self.action)


class FightView(discord.ui.LayoutView):
    """The challenge card and the fight itself, both on one message.

    Whoever runs ``,fight`` is taken as already willing, so only the member they
    named has to answer. ``,requestfight`` names two fighters instead, and it is
    a third person who runs it — in that case *both* fighters accept before a
    punch is thrown, and if the requester happens to be one of them they are
    taken as already in.

    Once it starts, both fighters pick a move each round. The picks are secret —
    the card only says who has locked in — and the round resolves when the
    second one lands, so neither fighter gets to react to the other's choice.
    """

    def __init__(
        self,
        cog: "Fun",
        *,
        fighters: Sequence[discord.Member],
        requester: discord.Member,
        timeout: float = FIGHT_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.fighters = list(fighters)
        self.requester = requester
        self.stage = "challenge"
        self.outcome: str | None = None
        self.declined_by: discord.Member | None = None
        # The requester never has to accept their own challenge; a third party
        # running ``,requestfight`` has to win both fighters over instead.
        self.accepted: set[int] = {
            fighter.id for fighter in self.fighters if fighter.id == requester.id
        }
        self.hp = {fighter.id: FIGHT_MAX_HP for fighter in self.fighters}
        self.picks: dict[int, str] = {}
        self.round = 0
        self.log: list[str] = []
        self.winner: discord.Member | None = None
        self.message: discord.Message | None = None
        self.add_item(self.board())

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------
    def fighter(self, user_id: int) -> discord.Member | None:
        return next((f for f in self.fighters if f.id == user_id), None)

    def waiting_on(self) -> list[discord.Member]:
        return [f for f in self.fighters if f.id not in self.accepted]

    def hit_lines(self) -> list[str]:
        return [
            f"> **{fighter.display_name}** {hp_bar(self.hp[fighter.id])} "
            f"`{max(0, self.hp[fighter.id])}/{FIGHT_MAX_HP}`"
            for fighter in self.fighters
        ]

    # ------------------------------------------------------------------
    # Cards
    # ------------------------------------------------------------------
    def board(self) -> discord.ui.Container:
        if self.stage == "challenge":
            return self.challenge_board()
        if self.stage == "fight":
            return self.fight_board()
        return self.result_board()

    def challenge_board(self) -> discord.ui.Container:
        waiting = self.waiting_on()
        first, second = self.fighters
        if any(fighter.id == self.requester.id for fighter in self.fighters):
            heading = f"# ⚔️ {self.requester.mention} wants a fight"
        else:
            heading = f"# ⚔️ {self.requester.mention} wants to see a fight"
        box = container(
            discord.ui.TextDisplay(
                f"{heading}\n"
                f"### {first.mention} vs {second.mention}\n"
                f"> The winner walks away with {format_cash(FIGHT_PRIZE)}"
            ),
            divider(),
            discord.ui.ActionRow(
                _FightButton(
                    self,
                    "accept",
                    style=discord.ButtonStyle.success,
                    label="Accept",
                    emoji="✅",
                ),
                _FightButton(
                    self,
                    "decline",
                    style=discord.ButtonStyle.secondary,
                    label="Decline",
                    emoji="✖️",
                ),
            ),
            accent=EMBED_COLORS["warning"],
        )
        if waiting:
            footer = "Waiting on " + " & ".join(f.mention for f in waiting)
        else:
            footer = "Both fighters are in"
        box.add_item(
            discord.ui.TextDisplay(f"-# {footer} · expires in {fmtseconds(FIGHT_TIMEOUT)}")
        )
        return box

    def fight_board(self) -> discord.ui.Container:
        body = "# ⚔️ Fight\n" + "\n".join(self.hit_lines())
        if self.log:
            body += "\n\n" + "\n".join(self.log[-FIGHT_LOG_LINES:])

        locked = [f.display_name for f in self.fighters if f.id in self.picks]
        if locked:
            footer = "Locked in: " + " & ".join(locked)
            pending = [f.display_name for f in self.fighters if f.id not in self.picks]
            if pending:
                footer += " · waiting on " + " & ".join(pending)
        else:
            footer = "Both fighters pick a move — the round resolves on the second pick"
        remaining = FIGHT_MAX_ROUNDS - self.round
        if remaining > 0:
            footer += f" · {remaining} round{'s' if remaining != 1 else ''} left"

        return container(
            discord.ui.TextDisplay(body),
            divider(),
            discord.ui.ActionRow(
                *(
                    _FightButton(
                        self,
                        key,
                        style=discord.ButtonStyle.primary,
                        label=label,
                        emoji=emoji,
                    )
                    for key, label, emoji in FIGHT_MOVES
                )
            ),
            discord.ui.TextDisplay(f"-# {footer}"),
            accent=EMBED_COLORS["error"],
        )

    def result_board(self) -> discord.ui.Container:
        if self.outcome == "declined":
            who = self.declined_by.display_name if self.declined_by else "Someone"
            return container(
                discord.ui.TextDisplay(
                    f"# ⚔️ Fight called off\n"
                    f"## ❌ {who} declined\n"
                    f"> No punches thrown, no prize paid."
                ),
                accent=EMBED_COLORS["error"],
            )
        if self.outcome == "expired":
            note = (
                "> The fight timed out mid-brawl — nobody took the prize."
                if self.round
                else "> The request timed out before anyone threw a punch."
            )
            return container(
                discord.ui.TextDisplay(f"# ⏱️ Fight expired\n{note}"),
                accent=EMBED_COLORS["warning"],
            )

        lines = self.hit_lines()
        if self.log:
            lines += ["", *self.log[-FIGHT_LOG_LINES:]]
        if self.outcome == "stalemate":
            lines += [
                "",
                "## 😴 Stalemate",
                f"> {FIGHT_MAX_ROUNDS} rounds and nobody ever swung — a draw, and "
                f"nobody takes the {format_cash(FIGHT_PRIZE)}",
            ]
            return container(
                discord.ui.TextDisplay("# ⚔️ Fight\n" + "\n".join(lines)),
                accent=EMBED_COLORS["warning"],
            )
        if self.winner is not None:
            lines += [
                "",
                f"## 🏆 {self.winner.mention} wins",
                f"> Prize {format_cash(FIGHT_PRIZE)}",
            ]
            accent = EMBED_COLORS["success"]
        else:
            lines += [
                "",
                "## 🤝 Both fighters went down at once",
                f"> A draw — nobody takes the {format_cash(FIGHT_PRIZE)}",
            ]
            accent = EMBED_COLOR
        return container(discord.ui.TextDisplay("# ⚔️ Fight\n" + "\n".join(lines)), accent=accent)

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------
    async def render(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer()
        self.clear_items()
        self.add_item(self.board())
        await swap_layout_message(interaction, self)

    async def press(self, interaction: discord.Interaction, action: str) -> None:
        if self.stage == "challenge":
            await self._answer(interaction, action)
        elif self.stage == "fight":
            await self._move(interaction, action)
        else:
            await interaction.response.send_message("This fight is over.", ephemeral=True)

    async def _answer(self, interaction: discord.Interaction, action: str) -> None:
        member = self.fighter(interaction.user.id)
        if member is None:
            await interaction.response.send_message(
                "Only the tagged fighters can answer this.", ephemeral=True
            )
            return
        if member.id in self.accepted:
            await interaction.response.send_message(
                "You're already in — waiting on the other fighter.", ephemeral=True
            )
            return

        if action == "decline":
            self.declined_by = member
            self.outcome = "declined"
            self.stage = "done"
            self.stop()
            await self.render(interaction)
            return

        self.accepted.add(member.id)
        if self.waiting_on():
            await self.render(interaction)
            return
        self.stage = "fight"
        await self.render(interaction)

    async def _move(self, interaction: discord.Interaction, action: str) -> None:
        member = self.fighter(interaction.user.id)
        if member is None:
            await interaction.response.send_message("This fight isn't yours.", ephemeral=True)
            return
        if member.id in self.picks:
            await interaction.response.send_message(
                "You've already locked in this round.", ephemeral=True
            )
            return

        self.picks[member.id] = action
        if len(self.picks) < len(self.fighters):
            await self.render(interaction)
            return

        self._resolve()
        if self.stage == "done":
            await self._settle()
        await self.render(interaction)

    async def _settle(self) -> None:
        """Record the result for both fighters and pay the prize, once."""
        winner_id = self.winner.id if self.winner is not None else None
        try:
            await self.cog.log_fight([f.id for f in self.fighters], winner_id)
        except Exception:
            logger.exception("Could not record the result of a fight")
        if winner_id is None:
            return
        try:
            await self.cog.bot.economy.add(
                winner_id, FIGHT_PRIZE, description="Fight win"
            )
        except Exception:
            logger.exception("Could not pay the fight prize to %s", winner_id)

    def _resolve(self) -> None:
        """Settle one fully-picked round, and the fight if it ends it."""
        first, second = self.fighters
        first_move, second_move = self.picks[first.id], self.picks[second.id]
        first_taken, second_taken, note = fight_round(first_move, second_move)
        self.hp[first.id] -= first_taken
        self.hp[second.id] -= second_taken
        self.round += 1
        self.log.append(
            f"**Round {self.round}** · {first.display_name} "
            f"{FIGHT_EMOJI[first_move]} vs {second.display_name} "
            f"{FIGHT_EMOJI[second_move]} — {note}."
        )
        self.picks.clear()

        downed = [f for f in self.fighters if self.hp[f.id] <= 0]
        if not downed:
            if self.round < FIGHT_MAX_ROUNDS:
                return
            # Nobody ever swung — call it a stalemate rather than let the card
            # sit on a fight that can no longer end.
            self.stage = "done"
            self.stop()
            self.winner = None
            self.outcome = "stalemate"
            return
        self.stage = "done"
        self.stop()
        if len(downed) == len(self.fighters):
            # Both went down in the same round — the healthier fighter edges it.
            higher = max(self.fighters, key=lambda f: self.hp[f.id])
            lower = min(self.fighters, key=lambda f: self.hp[f.id])
            self.winner = higher if self.hp[higher.id] > self.hp[lower.id] else None
            self.outcome = "draw" if self.winner is None else "ko"
            return
        self.winner = next(f for f in self.fighters if f is not downed[0])
        self.outcome = "ko"

    async def on_timeout(self) -> None:
        if self.stage == "done":
            return
        self.stage = "done"
        self.outcome = "expired"
        if self.message is None:
            return
        try:
            await self.message.edit(view=SimpleLayout(self.result_board()))
        except discord.HTTPException:
            pass


class FunExtra(commands.Cog, name="FunExtra"):
    """Refcog fun commands (fuck / fight / grope and friends).

    Registered as a separate cog from the main bot's ``Fun`` so the metres it
    shares a name with (``gay``, ``dih``) stay owned by the existing cog; only
    the commands that do not clash are exposed here.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        #: Recently used ``,fuck`` scenes, shown to the AI so it never repeats one.
        self._fuck_scenes: Deque[str] = deque(maxlen=FUCK_SCENE_MEMORY)

    # ------------------------------------------------------------------
    # AI
    # ------------------------------------------------------------------
    def ai(self):
        """The AI cog that decides the metres and writes the fuck scene."""
        return self.bot.get_cog("AI")

    async def ai_number(
        self,
        kind: str,
        members: Sequence[discord.Member],
        low: int,
        high: int,
        channel: Optional[discord.abc.Messageable] = None,
    ) -> Optional[int]:
        """The AI's verdict for a metre, or ``None`` to fall back to random."""
        cog = self.ai()
        if cog is None:
            return None
        try:
            return await cog.decide_meter_value(kind, list(members), low, high, channel)
        except Exception:
            logger.exception("The AI could not decide the %s metre", kind)
            return None

    async def ai_fuck_scene(
        self, actor: discord.Member, target: discord.Member
    ) -> Optional[dict]:
        """A coherent AI-written scene for ``,fuck``, or ``None`` to fall back."""
        cog = self.ai()
        if cog is None:
            return None
        try:
            scene = await cog.generate_fuck_scene(actor, target, list(self._fuck_scenes))
        except Exception:
            logger.exception("The AI could not write a fuck scene")
            return None
        if scene:
            self._fuck_scenes.append(
                " — ".join(
                    scene[field] for field in ("place", "action", "noise", "aftermath")
                )
            )
        return scene

    # ------------------------------------------------------------------
    # Metres
    # ------------------------------------------------------------------
    async def meter(
        self,
        kind: str,
        *,
        member: Optional[discord.Member] = None,
        channel: Optional[discord.abc.Messageable] = None,
    ) -> int:
        """Judge a member's number from their chat, fresh on every run.

        Nothing is stored: the AI reads the member's recent messages each time
        the command runs, so the number moves with the conversation instead of
        being frozen the first time somebody asked. If the AI gives nothing
        usable the number falls back to a plain random draw, so the metre always
        answers.
        """
        low, high = KINDS[kind]
        decided = await self.ai_number(
            kind, [member] if member is not None else [], low, high, channel
        )
        if decided is None:
            decided = random.randint(low, high)
        return decided

    async def pair(
        self,
        *,
        members: Sequence[discord.Member] = (),
        channel: Optional[discord.abc.Messageable] = None,
    ) -> int:
        """Judge how compatible two members are, fresh from the current chat.

        The AI is handed both members' recent chat together, so the score reacts
        to the pair rather than to just one of them. Nothing is stored, so the
        score is re-decided on every run.
        """
        low, high = PERCENT_RANGE
        decided = await self.ai_number("ship", list(members), low, high, channel)
        if decided is None:
            decided = random.randint(low, high)
        return decided

    # ------------------------------------------------------------------
    # Fight records
    # ------------------------------------------------------------------
    async def record_fight(
        self, user_id: int, *, won: bool = False, lost: bool = False, drew: bool = False
    ) -> None:
        """Bump one member's fight tally by a single result.

        ``excluded`` carries the inserted counters into an existing row, so the
        same statement both creates the record and adds to it.
        """
        await self.bot.db.execute(
            "INSERT INTO fight_stats (user_id, wins, losses, draws) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "wins = wins + excluded.wins, losses = losses + excluded.losses, "
            "draws = draws + excluded.draws",
            int(user_id),
            int(won),
            int(lost),
            int(drew),
        )

    async def log_fight(self, fighters: Sequence[int], winner: int | None) -> None:
        """Write one finished fight to every fighter who was in it.

        ``winner`` is ``None`` for a draw, which racks up a draw for both.
        """
        for user_id in fighters:
            await self.record_fight(
                user_id,
                won=winner == user_id,
                lost=winner is not None and winner != user_id,
                drew=winner is None,
            )

    async def fight_leaderboard(
        self, limit: int = FIGHT_LEADERBOARD_LIMIT
    ) -> list[tuple[int, int, int, int]]:
        """``(user_id, wins, losses, draws)``, most wins first.

        Ties break on the fewest losses, so a 5-1 record outranks a 5-4 one.
        """
        rows = await self.bot.db.execute(
            "SELECT user_id, wins, losses, draws FROM fight_stats "
            "WHERE wins + losses + draws > 0 "
            "ORDER BY wins DESC, losses ASC, draws DESC, user_id ASC LIMIT %s",
            int(limit),
        )
        return [
            (int(user_id), int(wins), int(losses), int(draws))
            for user_id, wins, losses, draws in rows
        ]

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    @commands.guild_only()
    async def gay(
        self, ctx: commands.Context, member: Optional[discord.Member] = None
    ) -> None:
        f"""Check how gay someone is — judged from their chat, fresh every time.

        Usage: `{PREFIX}gay @member` (or `{PREFIX}gay` for yourself).
        """
        target = member or ctx.author
        async with ctx.typing():
            percent = await self.meter("gay", member=target, channel=ctx.channel)
        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    f"# 🌈 Gay Meter\n"
                    f"{target.mention} is **{percent}%** gay — {rating_label(percent)}.\n"
                    f"-# `{meter_bar(percent)}` {percent}%"
                )
            )
        )

    @commands.command(name="pussy", aliases=("pussymeter",))
    @commands.guild_only()
    async def pussy(
        self, ctx: commands.Context, member: Optional[discord.Member] = None
    ) -> None:
        f"""Check how much of a pussy someone is — judged from their chat every time.

        Usage: `{PREFIX}pussy @member` (or `{PREFIX}pussy` for yourself).
        """
        target = member or ctx.author
        async with ctx.typing():
            percent = await self.meter("pussy", member=target, channel=ctx.channel)
        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    f"# 😹 Pussy Meter\n"
                    f"{target.mention} is **{percent}%** a pussy — "
                    f"{rating_label(percent)}.\n"
                    f"-# `{meter_bar(percent)}` {percent}%"
                )
            )
        )

    @commands.guild_only()
    async def dih(
        self, ctx: commands.Context, member: Optional[discord.Member] = None
    ) -> None:
        f"""Measure someone's dih — the AI sizes it from their chat every time.

        Usage: `{PREFIX}dih @member` (or `{PREFIX}dih` for yourself).
        """
        target = member or ctx.author
        async with ctx.typing():
            signs = await self.meter("dih", member=target, channel=ctx.channel)
        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    f"# 🍆 Dih Meter\n"
                    f"{target.mention}'s dih:\n"
                    f"# `{dih_art(signs)}`"
                )
            )
        )

    @commands.command(name="makeout", aliases=("kiss",))
    @commands.guild_only()
    async def makeout(self, ctx: commands.Context, member: discord.Member) -> None:
        f"""Make out with someone — tongue or not, at random.

        Usage: `{PREFIX}makeout @member`.
        """
        if member.bot:
            await ctx.send(view=notice("Bots don't have lips. Pick a member."))
            return

        tongue = random.choice((True, False))
        if member.id == ctx.author.id:
            action = "made out with themselves. Bold."
        else:
            action = f"made out with {member.mention}"

        detail = "**with tongue** 🫦" if tongue else "**without tongue** 😗"
        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    f"# 💋 Makeout\n{ctx.author.mention} {action} {detail}"
                ),
                accent=EMBED_COLORS["purple"],
            )
        )


    @commands.command(name="grope", aliases=("fondle",))
    @commands.guild_only()
    async def grope(self, ctx: commands.Context, member: discord.Member) -> None:
        f"""Grope someone — where it lands is picked at random.

        Usage: `{PREFIX}grope @member`.
        """
        if member.bot:
            await ctx.send(view=notice("Bots are un-gropeable. Pick a member."))
            return

        spot = random.choice(GROPE_SPOTS)
        if member.id == ctx.author.id:
            action = f"groped their own **{spot}**. Slightly concerning."
        else:
            action = f"groped {member.mention}'s **{spot}**"
        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(f"# ✋ Grope\n{ctx.author.mention} {action}"),
                accent=GROPE_COLOR,
            )
        )

    @commands.command(name="fuck", aliases=("sex",))
    @commands.guild_only()
    async def fuck(self, ctx: commands.Context, member: discord.Member) -> None:
        f"""Fuck someone - the AI writes one coherent scene, fresh each time.

        Usage: `{PREFIX}fuck @member`.
        """
        if member.bot:
            await ctx.send(view=notice("Bots don't have a body. Pick a member."))
            return

        async with ctx.typing():
            scene = await self.ai_fuck_scene(ctx.author, member)
        if scene is None:
            place, action, noise, after = (
                random.choice(FUCK_PLACES),
                random.choice(FUCK_ACTIONS),
                random.choice(FUCK_NOISES),
                random.choice(FUCK_AFTERMATH),
            )
        else:
            place, action, noise, after = (
                scene["place"],
                scene["action"],
                scene["noise"],
                scene["aftermath"],
            )

        if member.id == ctx.author.id:
            headline = f"{ctx.author.mention} fucked themselves in {place}. Bold."
        else:
            headline = f"{ctx.author.mention} fucked {member.mention} in {place}."

        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    f"# 🔥 Sex\n"
                    f"{headline}\n"
                    f"-# {sentence_case(without_stop(action))} - "
                    f"{without_stop(noise)}.\n"
                    f"-# {sentence_case(without_stop(after))}."
                ),
                accent=FUCK_COLOR,
            )
        )

    @commands.command(name="ship", aliases=("compat",))
    @commands.guild_only()
    async def ship(
        self,
        ctx: commands.Context,
        first: discord.Member,
        second: Optional[discord.Member] = None,
    ) -> None:
        f"""Check how compatible two members are — judged from their chat every time.

        Usage: `{PREFIX}ship @member` (you and them) or
        `{PREFIX}ship @member @other`.
        """
        other = second or ctx.author
        if first.bot or other.bot:
            await ctx.send(view=notice("Bots don't date. Pick a member."))
            return

        async with ctx.typing():
            percent = await self.pair(members=(first, other), channel=ctx.channel)
        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    f"# 💞 Ship Meter\n"
                    f"{first.mention} ❤️ {other.mention}\n"
                    f"**{percent}%** compatible — {ship_label(percent)}\n"
                    f"-# `{meter_bar(percent)}` {percent}%"
                ),
                accent=SHIP_COLOR,
            )
        )

    # ------------------------------------------------------------------
    # Fights
    # ------------------------------------------------------------------
    @commands.command(name="fight", aliases=("throwdown",))
    @commands.guild_only()
    async def fight(self, ctx: commands.Context, member: discord.Member) -> None:
        f"""Challenge someone to a fight — the winner takes {format_cash(FIGHT_PRIZE)}.

        Usage: `{PREFIX}fight @member`. They accept or decline, then you trade
        punches, blocks and parries until one of you is out of health.
        """
        if member.bot:
            await ctx.send(view=notice("Bots don't fight. Pick a member."))
            return
        if member.id == ctx.author.id:
            await ctx.send(view=notice("You can't fight yourself. Pick someone else."))
            return

        view = FightView(self, fighters=(ctx.author, member), requester=ctx.author)
        view.message = await ctx.send(view=view)

    @commands.command(name="requestfight", aliases=("reqfight", "fightrequest"))
    @commands.guild_only()
    async def requestfight(
        self, ctx: commands.Context, first: discord.Member, second: discord.Member
    ) -> None:
        f"""Tag two members and let them fight each other.

        Usage: `{PREFIX}requestfight @member @other`. Both of them answer with a
        button — nobody has to type the command themselves — and whoever wins
        takes {format_cash(FIGHT_PRIZE)}.
        """
        if first.id == second.id:
            await ctx.send(view=notice("Pick two different members."))
            return
        if first.bot or second.bot:
            await ctx.send(view=notice("Bots don't fight. Pick two members."))
            return

        view = FightView(self, fighters=(first, second), requester=ctx.author)
        view.message = await ctx.send(view=view)

    @commands.command(name="fighttop", aliases=("fightlb", "fightleaderboard"))
    @commands.guild_only()
    async def fighttop(self, ctx: commands.Context, count: int = 10) -> None:
        f"""Show the members with the most fight wins.

        Usage: `{PREFIX}fighttop [count]` — ranked by wins, then fewest losses.
        """
        count = max(1, min(FIGHT_LEADERBOARD_LIMIT, count))
        rows = await self.fight_leaderboard(count)
        if not rows:
            await ctx.send(
                view=notice(
                    "Nobody has fought yet — start one with "
                    f"`{ctx.prefix or PREFIX}fight @member`.",
                    emoji="⚔️",
                )
            )
            return

        lines = []
        for position, (user_id, wins, losses, draws) in enumerate(rows, start=1):
            badge = (
                FIGHT_MEDALS[position - 1]
                if position <= len(FIGHT_MEDALS)
                else f"`#{position}`"
            )
            detail = f"**{wins}**W · **{losses}**L"
            if draws:
                detail += f" · **{draws}**D"
            lines.append(f"{badge} <@{user_id}> — {detail}")

        box = discord.ui.Container(accent_color=EMBED_COLORS["gold"])
        box.add_item(
            discord.ui.TextDisplay(
                f"# ⚔️ Fight leaderboard\n"
                f"-# top {len(rows)} in {ctx.guild.name}"
            )
        )
        box.add_item(divider())
        box.add_item(discord.ui.TextDisplay("\n".join(lines)))
        await ctx.send(view=SimpleLayout(box))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FunExtra(bot))
