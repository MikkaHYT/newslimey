"""Chat-wide games the whole server plays together.

These games all live here, driven from a normal text channel:

- **Count to 100** — the channel counts upward one number at a time. A wrong
  number, or the same member counting twice in a row, resets the run. Only bot
  admins and members with **Manage Messages** can start or stop it.
- **Emoji reaction race** — a random emoji is announced and the first member to
  react with it wins. The bot never adds the reaction for you, so everyone has
  to find the emoji in their own picker.
- **Hot potato** — players react to join, then pass a ticking potato by simply
  mentioning another player before it explodes. Last one standing wins.
- **Social deduction night** — players react to join, get their role by DM
  (impostor, detective, medic or crew), act at night, then discuss and vote out
  the impostor. Timers tick live and the end screen reveals every role.
- **Chameleon** — players react to join and get one shared secret word by DM
  (or a private thread when their DMs are closed), except the Chameleon who
  never sees it. Everyone gives a one-word clue in
  turn, then votes on the odd one out; a caught Chameleon gets one guess at the
  word to steal the win.
- **One word story** — the channel builds a story one word per member; when it
  ends, the AI weaves every collected word into a real story.

Only one of each game may run per channel, and every lobby/loop is tracked so
reloading the cog cancels cleanly instead of leaking background tasks.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import random
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

import discord
from discord.ext import commands
from discord.ext.commands import Context

from config import EMBED_COLORS
from utils.core.prefixes import prefix_for
from utils.cooldown import unified_cooldown
from utils.guardrails import check_slash_guardrails

logger = logging.getLogger("slimey.chatgames")

#: How high the count game goes before the run is won.
COUNT_TARGET = 100
#: Seconds the reaction lobbies stay open.
JOIN_WINDOW = 15.0
#: Hot potato fuse, in seconds, picked fresh for each round.
POTATO_MIN_FUSE = 8.0
POTATO_MAX_FUSE = 18.0
#: Seconds the reaction race waits for a winner.
RACE_TIMEOUT = 30.0
#: Story-word coverage retries before the best attempt is posted anyway.
STORY_AI_ATTEMPTS = 3
#: Output budget for one story; enough for a few paragraphs.
STORY_AI_MAX_OUTPUT_TOKENS = 1400
STORY_AI_TEMPERATURE = 1.15
#: Seconds of discussion before the vote opens.
DISCUSSION_WINDOW = 60.0
#: Bounds for a custom `deduction start <seconds>` discussion timer.
DISCUSSION_MIN_SECONDS = 30.0
DISCUSSION_MAX_SECONDS = 300.0
#: Seconds members have to vote.
VOTE_WINDOW = 30.0
#: Seconds members have to send their secret night action by DM.
NIGHT_WINDOW = 25.0
#: How often a visible countdown is edited.
COUNTDOWN_STEP = 5.0
#: Chameleon: everyone shares a secret word except one player.
CHAMELEON_MIN_PLAYERS = 3
CHAMELEON_MAX_PLAYERS = 12
#: Seconds each player gets to post their one-word clue, and to vote.
CHAMELEON_CLUE_SECONDS = 45.0
CHAMELEON_VOTE_SECONDS = 30.0
#: The caught Chameleon gets one guess at the word to steal the win.
CHAMELEON_GUESS_SECONDS = 30.0
#: Longest clue a player may post.
CHAMELEON_MAX_CLUE_LENGTH = 24
#: A story ends automatically at this many words.
STORY_MAX_WORDS = 150
#: Longest single word a story accepts.
STORY_MAX_WORD_LENGTH = 40
#: Social deduction player bounds, plus a safety cap so an inactive table
#: cannot keep a background loop alive forever.
DEDUCTION_MIN_PLAYERS = 4
DEDUCTION_MAX_PLAYERS = 10
DEDUCTION_MAX_ROUNDS = 12
#: The detective needs a big enough table that the extra role is not a giveaway.
DETECTIVE_MIN_PLAYERS = 5
#: The medic is only dealt into the larger games.
MEDIC_MIN_PLAYERS = 6
#: Completed-game stats, used by `deductionstats`.
DEDUCTION_STATS_FILE = (
    Path(__file__).resolve().parent.parent / "data" / "chatgames_stats.json"
)
ROLE_NAMES = {
    "impostor": "Impostor",
    "detective": "Detective",
    "medic": "Medic",
    "crew": "Crewmate",
}
#: Hot potato is a free-for-all, but the lobby stays readable at ten players.
POTATO_MAX_PLAYERS = 10

#: Race emojis, paired with their names so members can search for them in the
#: picker. A fresh one is drawn per race, and the bot never reacts with it.
RACE_EMOJIS: tuple[tuple[str, str], ...] = (
    ("🍕", "pizza"),
    ("🐸", "frog"),
    ("🌮", "taco"),
    ("🚀", "rocket"),
    ("🦆", "duck"),
    ("🍩", "doughnut"),
    ("🎲", "game die"),
    ("🐙", "octopus"),
    ("🌈", "rainbow"),
    ("🔥", "fire"),
    ("🍄", "mushroom"),
    ("🧊", "ice cube"),
    ("🐝", "honeybee"),
    ("🎩", "top hat"),
    ("🍉", "watermelon"),
    ("👾", "alien monster"),
    ("⛵", "sailboat"),
    ("🦴", "bone"),
    ("🧦", "socks"),
    ("🪐", "ringed planet"),
)
POTATO_EMOJI = "🥔"
DEDUCTION_EMOJI = "🕵️"
CHAMELEON_EMOJI = "🦎"
#: Words the table can be given. Plain, concrete nouns: easy to hint at
#: without the clue giving it away outright.
CHAMELEON_WORDS: tuple[str, ...] = (
    "airport", "alligator", "arrow", "avalanche", "bakery", "banana",
    "barbecue", "bicycle", "birthday", "blizzard", "bowling", "bridge",
    "cactus", "campfire", "carnival", "castle", "cemetery", "cheeseburger",
    "chocolate", "church", "circus", "coffee", "comet", "dentist",
    "detective", "diamond", "dinosaur", "doctor", "dragon", "dumpling",
    "elevator", "elephant", "escalator", "firework", "fishing", "flamingo",
    "football", "garden", "ghost", "guitar", "hammock", "helicopter",
    "hospital", "hotel", "hurricane", "iceberg", "igloo", "jellyfish",
    "jungle", "karaoke", "keyboard", "ladder", "lava", "library",
    "lighthouse", "magician", "mansion", "marathon", "mermaid", "microwave",
    "mosquito", "museum", "nightclub", "ninja", "ocean", "octopus",
    "opera", "pancake", "parachute", "parrot", "penguin", "picnic",
    "pizza", "planet", "playground", "popcorn", "prison", "puppet",
    "pyramid", "rainbow", "reptile", "robot", "rollercoaster", "sandcastle",
    "sandwich", "saxophone", "school", "scissors", "sculpture", "shark",
    "skateboard", "skeleton", "skyscraper", "snowman", "spaceship", "spider",
    "stadium", "submarine", "sunburn", "sunflower", "superhero", "swamp",
    "taxi", "telephone", "telescope", "thunderstorm", "toothbrush", "tornado",
    "trampoline", "treasure", "trophy", "umbrella", "vampire", "volcano",
    "vulture", "waiter", "waterfall", "wedding", "werewolf", "windmill",
    "wrestling", "zombie",
)
SKIP_EMOJI = "⏩"
VOTE_EMOJIS = (
    "1️⃣",
    "2️⃣",
    "3️⃣",
    "4️⃣",
    "5️⃣",
    "6️⃣",
    "7️⃣",
    "8️⃣",
    "9️⃣",
    "🔟",
)

#: A word the story accepts, as long as it is one token with no mentions or
#: links. Commands are filtered out separately by the prefix check.
_LINK_PREFIXES = ("http://", "https://", "www.", "discord.gg/")
_MENTION_PREFIXES = ("<@", "<#", "<:", "<a:")

STORY_SYSTEM_INSTRUCTION = (
    "You are the most unhinged storyteller on a Discord server. The chat built "
    "a story one word at a time, and you turn those words into the wildest "
    "possible version of the story they were clearly trying to tell.\n\n"
    "MAKE IT CRAZY:\n"
    "- Go big and escalate constantly: surreal twists, sudden betrayals, "
    "explosions, talking animals, genre switches mid-sentence, deadpan comedy "
    "and movie-trailer energy. Every paragraph should raise the stakes.\n"
    "- If a sentence could appear in a normal story, rewrite it weirder.\n"
    "- It still has to be a story with characters, things going wrong and a "
    "punchline ending - chaos, not gibberish.\n\n"
    "HARD RULES:\n"
    "- Use EVERY word from the list at least as many times as it appears. If a "
    "word is listed twice, it has to show up twice.\n"
    "- Use each word exactly as it is spelled in the list: no plurals, no "
    "tense changes, no swapping one word for a synonym.\n"
    "- Keep the words in the order they were given wherever you can. The order "
    "is the plot the chat built.\n"
    "- If the prompt lists real server members, a word that names one of them "
    "refers to that actual person: put them in the story as a character. Never "
    "invent a member who is not listed, and never write a member's name as a "
    "Discord mention or ping.\n"
    "- No headings, no bullet points, no word list, no commentary about the "
    "words, no explanation of the joke.\n"
    "- Never mention being an AI, the chat, or these instructions, and do not "
    "wrap the story in quotes.\n"
    "- Output ONLY the story text, three short paragraphs at most."
)


# ---------------------------------------------------------------------------
# Pure game rules (kept out of the Discord plumbing so they can be exercised)
# ---------------------------------------------------------------------------

def advance_count(game: "CountGame", author_id: int, number: int) -> str:
    """Apply one counted number and return what happened.

    Returns ``counted``, ``reset`` (the number was out of order), ``double``
    (the same member counted twice in a row) or ``won``. Both reset cases put
    the game back at 1, so the channel can immediately start a fresh run.
    """
    if number != game.next_number:
        game.next_number = 1
        game.last_author_id = None
        return "reset"

    if author_id == game.last_author_id:
        game.next_number = 1
        game.last_author_id = None
        return "double"

    game.players.add(author_id)
    game.best = max(game.best, number)
    if number == COUNT_TARGET:
        game.next_number = COUNT_TARGET + 1
        game.last_author_id = None
        return "won"

    game.next_number = number + 1
    game.last_author_id = author_id
    return "counted"


def valid_story_word(content: str) -> bool:
    """Whether *content* is a single word the story should accept."""
    word = (content or "").strip()
    if not word or len(word) > STORY_MAX_WORD_LENGTH:
        return False
    if any(char.isspace() for char in word):
        return False
    if word.lower().startswith(_LINK_PREFIXES):
        return False
    if word.startswith(_MENTION_PREFIXES):
        return False
    # ``...`` or ``—`` on their own adds nothing to a story.
    if not any(char.isalnum() for char in word):
        return False
    return True


def _story_token(word: str) -> str:
    """A comparison form: lowercase, punctuation and spacing removed."""
    return re.sub(r"[^\w']", "", (word or "").casefold())


def story_word_counts(words: Sequence[str]) -> dict[str, int]:
    """How many times each distinct story word has to appear in the story."""
    counts: Counter[str] = Counter()
    for word in words:
        token = _story_token(word)
        if token:
            counts[token] += 1
    return dict(counts)


def missing_story_words(words: Sequence[str], text: str) -> list[str]:
    """The words the AI story left out, in the order they were given.

    A word counts as used when its token shows up in *text* at least as often
    as the channel added it, so a word added twice has to appear twice.
    """
    needed = story_word_counts(words)
    if not needed:
        return []
    used: Counter[str] = Counter()
    for token in re.findall(r"[\w']+", (text or "").casefold()):
        used[token] += 1
        # "sesh's" also counts as using "sesh", which is how names usually show
        # up once the story starts writing about real members.
        for part in re.split(r"['\u2019]", token):
            if part and part != token:
                used[part] += 1
    missing: list[str] = []
    for word in words:
        token = _story_token(word)
        if not token:
            continue
        have = used.get(token, 0)
        if have < needed[token]:
            missing.append(word)
            used[token] = have + 1
    return missing


def build_story_roster(
    guild: Any,
    words: Sequence[str],
    author_ids: set[int],
    *,
    limit: int = 20,
) -> list[tuple[str, str]]:
    """Members the story is allowed to name, as ``(display name, username)``.

    Everyone who added a word is included, plus any member whose username or
    display name is one of the words - so a word like ``sesh`` turns into the
    real person instead of a random noun. The list is capped so the prompt stays
    small on big servers.
    """
    named = {_story_token(word) for word in words if _story_token(word)}
    author_ids = set(author_ids)
    mentioned: list[tuple[str, str]] = []
    writers: list[tuple[str, str]] = []
    for member in getattr(guild, "members", None) or []:
        display = (getattr(member, "display_name", "") or "").strip()
        username = (getattr(member, "name", "") or "").strip()
        entry = (display or username, username)
        # A member named by a word goes in first so a busy story never pushes
        # the person everyone is talking about past the cap.
        if _story_token(display) in named or _story_token(username) in named:
            mentioned.append(entry)
        elif getattr(member, "id", None) in author_ids:
            writers.append(entry)
    return (mentioned + writers)[:limit]


def format_story_roster(roster: Sequence[tuple[str, str]]) -> str:
    """Render the roster as prompt lines."""
    lines: list[str] = []
    for display, username in roster:
        if username and display.casefold() != username.casefold():
            lines.append(f"- {display} (username: {username})")
        else:
            lines.append(f"- {display}")
    return "\n".join(lines)


def build_story_prompt(
    words: Sequence[str], roster: Sequence[tuple[str, str]] = ()
) -> str:
    """Ask for a story that uses *words* in the order they were added."""
    listed = "\n".join(f"{index}. {word}" for index, word in enumerate(words, 1))
    prompt = (
        f"The channel added {len(words)} word(s), in this order:\n\n{listed}\n\n"
    )
    if roster:
        prompt += (
            "Real members of this server, fair game as characters:\n"
            f"{format_story_roster(roster)}\n\n"
            "If one of the words is one of these names (or something they are "
            "called), it means that actual person - make them a character. Keep "
            "their name spelled as it appears in the word list and write it as "
            "plain text, never as a mention. Do not invent anyone not listed.\n\n"
        )
    return prompt + (
        "Write that story now. Every word above has to appear in it, spelled "
        "exactly as listed, in this order where the story allows. Make it as "
        "wild and unhinged as you can get away with."
    )


def clean_story_output(text: str) -> str:
    """Tidy the model's reply into something worth posting in an embed."""
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    # Models like to introduce the story or wrap it in quotes/code fences.
    cleaned = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", cleaned).strip()
    cleaned = re.sub(
        r"^(?:sure[,!]?\s*|here(?:'s| is) (?:the|your) story[:!]?\s*|"
        r"story[:!]\s*)",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in "\"'":
        cleaned = cleaned[1:-1].strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned[:3900]


def render_story(words: list[tuple[int, str]]) -> str:
    """Join a story's words into readable prose."""
    text = " ".join(word for _, word in words).strip()
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    if text[-1] not in ".!?\"'":
        text += "."
    return text[:4000]


def decide_ejection(tally: dict[int, int]) -> tuple[Optional[int], bool]:
    """Pick the member with the most votes.

    Returns ``(member_id, had_votes)``. ``member_id`` is ``None`` when nobody
    voted or when the top spot is tied — a tied vote ejects nobody, which is
    what keeps the deduction game from being decided by reaction order.
    """
    positive = {member_id: votes for member_id, votes in tally.items() if votes > 0}
    if not positive:
        return None, False
    top = max(positive.values())
    leaders = [member_id for member_id, votes in positive.items() if votes == top]
    if len(leaders) != 1:
        return None, True
    return leaders[0], True


def deduction_winner(alive: list[int], impostors: set[int]) -> str | None:
    """``"crew"``, ``"impostors"`` or ``None`` while the game continues."""
    impostors_alive = sum(1 for member_id in alive if member_id in impostors)
    crew_alive = len(alive) - impostors_alive
    if impostors_alive == 0:
        return "crew"
    if impostors_alive >= crew_alive:
        return "impostors"
    return None


def chameleon_clue_is_valid(content: str) -> bool:
    """Whether *content* is an acceptable one-word clue."""
    clue = (content or "").strip()
    if not clue or len(clue) > CHAMELEON_MAX_CLUE_LENGTH:
        return False
    if any(char.isspace() for char in clue):
        return False
    if clue.lower().startswith(_LINK_PREFIXES):
        return False
    if clue.startswith(_MENTION_PREFIXES):
        return False
    return any(char.isalnum() for char in clue)


def chameleon_tally(
    votes: dict[int, int], players: Sequence[int]
) -> tuple[int | None, dict[int, int]]:
    """The accused player (``None`` on a tie) and how many votes each got.

    A tie, or a round where nobody votes, leaves the Chameleon uncaught - the
    room had its chance.
    """
    counted: Counter[int] = Counter(
        target for target in votes.values() if target in set(players)
    )
    if not counted:
        return None, {}
    best = max(counted.values())
    leaders = [member_id for member_id, count in counted.items() if count == best]
    if len(leaders) != 1:
        return None, dict(counted)
    return leaders[0], dict(counted)


def chameleon_guess_ok(guess: str, word: str) -> bool:
    """Whether a caught Chameleon's guess matches the word."""
    def _normalize(text: str) -> str:
        return re.sub(r"[^a-z0-9]", "", (text or "").casefold())

    cleaned = _normalize(guess)
    return bool(cleaned) and cleaned == _normalize(word)


# ---------------------------------------------------------------------------
# Game state
# ---------------------------------------------------------------------------

@dataclass
class CountGame:
    channel_id: int
    guild_id: int
    prefix: str
    next_number: int = 1
    last_author_id: int | None = None
    best: int = 0
    players: set[int] = field(default_factory=set)


@dataclass
class StoryGame:
    channel_id: int
    guild_id: int
    starter_id: int
    prefix: str
    words: list[tuple[int, str]] = field(default_factory=list)
    authors: set[int] = field(default_factory=set)
    last_author_id: int | None = None


@dataclass
class PotatoGame:
    channel_id: int
    guild_id: int
    prefix: str
    players: dict[int, discord.Member] = field(default_factory=dict)
    alive: list[int] = field(default_factory=list)
    holder_id: int | None = None
    future: asyncio.Future[int] | None = None


@dataclass
class DeductionGame:
    channel_id: int
    guild_id: int
    prefix: str
    players: dict[int, discord.Member] = field(default_factory=dict)
    alive: list[int] = field(default_factory=list)
    impostors: set[int] = field(default_factory=set)
    detective_id: int | None = None
    medic_id: int | None = None
    #: Everyone's role, kept even after an impostor is ejected for the end screen.
    roles: dict[int, str] = field(default_factory=dict)
    #: The medic's shield for tonight, and who they shielded last night.
    protected_id: int | None = None
    last_protected_id: int | None = None
    #: Who each player voted for this round, and their running correct-read count.
    votes: dict[int, int] = field(default_factory=dict)
    correct_votes: dict[int, int] = field(default_factory=dict)
    discussion_seconds: float = DISCUSSION_WINDOW
    round: int = 0


@dataclass
class ChameleonGame:
    """One Chameleon round: a shared word, one player who never got it."""

    channel_id: int
    guild_id: int
    prefix: str
    players: dict[int, discord.Member] = field(default_factory=dict)
    #: The secret word everyone but the Chameleon was handed.
    word: str = ""
    chameleon_id: int | None = None
    #: Fixed, shuffled clue order so nobody can dodge the spotlight.
    order: list[int] = field(default_factory=list)
    clues: dict[int, str] = field(default_factory=dict)
    #: Voter id -> who they voted for.
    voted: dict[int, int] = field(default_factory=dict)
    turn_index: int = 0
    clue_future: asyncio.Future[str] | None = None
    vote_future: asyncio.Future[None] | None = None
    #: Player id -> private thread used when their DMs are closed.
    fallback_threads: dict[int, discord.Thread] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Social deduction stats
# ---------------------------------------------------------------------------

def load_deduction_stats() -> dict:
    """Read the stats file; a missing or broken file is just an empty store."""
    try:
        data = json.loads(DEDUCTION_STATS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        pass
    except Exception:
        logger.exception("Could not read social deduction stats")
    return {}


def save_deduction_stats(store: dict) -> None:
    """Write the stats atomically so a crash can't truncate the file."""
    DEDUCTION_STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DEDUCTION_STATS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=2), encoding="utf-8")
    os.replace(tmp, DEDUCTION_STATS_FILE)


def record_deduction_game(
    store: dict,
    guild_id: int,
    *,
    winner: Optional[str],
    roles: dict[int, str],
    correct_votes: dict[int, int],
    names: dict[int, str],
) -> None:
    """Fold one finished game into *store* (mutated in place)."""
    board = store.setdefault("guilds", {}).setdefault(str(guild_id), {})
    for member_id, role in roles.items():
        entry = board.setdefault(str(member_id), {})
        entry["name"] = names.get(member_id, entry.get("name", ""))
        entry["games"] = int(entry.get("games", 0)) + 1
        if role == "impostor":
            entry["impostor_games"] = int(entry.get("impostor_games", 0)) + 1
        won = (winner == "crew" and role != "impostor") or (
            winner == "impostors" and role == "impostor"
        )
        if won:
            entry["wins"] = int(entry.get("wins", 0)) + 1
            if role == "impostor":
                entry["impostor_wins"] = int(entry.get("impostor_wins", 0)) + 1
        entry["correct_votes"] = int(entry.get("correct_votes", 0)) + int(
            correct_votes.get(member_id, 0)
        )


def deduction_stats_entry(store: dict, guild_id: int, member_id: int) -> dict:
    """One member's record in this guild, or an empty dict."""
    board = store.get("guilds", {}).get(str(guild_id), {})
    entry = board.get(str(member_id)) if isinstance(board, dict) else None
    return dict(entry) if isinstance(entry, dict) else {}


def deduction_leaderboard(
    store: dict, guild_id: int, *, limit: int = 10
) -> list[tuple[int, dict]]:
    """The guild's best players, most wins first."""
    board = store.get("guilds", {}).get(str(guild_id), {})
    rows: list[tuple[int, dict]] = []
    if not isinstance(board, dict):
        return rows
    for key, entry in board.items():
        if not isinstance(entry, dict):
            continue
        try:
            member_id = int(key)
        except (TypeError, ValueError):
            continue
        rows.append((member_id, entry))
    rows.sort(
        key=lambda item: (
            -int(item[1].get("wins", 0)),
            -int(item[1].get("games", 0)),
            str(item[1].get("name", "")),
        )
    )
    return rows[:limit]


# ---------------------------------------------------------------------------
# Permission helpers
# ---------------------------------------------------------------------------

async def _is_chat_moderator(ctx: Context) -> bool:
    """Bot admins, or a member who can manage messages in this server."""
    admin_ids = getattr(ctx.bot, "bot_admin_ids", None) or set()
    if ctx.author.id in admin_ids:
        return True
    if await ctx.bot.is_owner(ctx.author):
        return True
    permissions = getattr(ctx.author, "guild_permissions", None)
    return bool(permissions and permissions.manage_messages)


def _moderator_only():
    """Allow bot admins and members with Manage Messages, nobody else."""

    async def predicate(ctx: Context) -> bool:
        if await _is_chat_moderator(ctx):
            return True
        raise commands.MissingPermissions(["manage_messages"])

    return commands.check(predicate)


class _ChameleonVoteView(discord.ui.View):
    """One button per player: point at whoever you think is the Chameleon."""

    def __init__(self, game: ChameleonGame, render: Any) -> None:
        super().__init__(timeout=CHAMELEON_VOTE_SECONDS)
        self.game = game
        self.render = render
        self.message: Optional[discord.Message] = None
        for member_id in game.order:
            member = game.players.get(member_id)
            if member is None:
                continue
            item = discord.ui.Button(
                label=member.display_name[:80],
                style=discord.ButtonStyle.secondary,
                custom_id=f"chameleon:{game.channel_id}:{member_id}",
            )
            item.callback = self._vote_callback(member_id)
            self.add_item(item)

    def _vote_callback(self, target_id: int) -> Any:
        async def callback(interaction: discord.Interaction) -> None:
            game = self.game
            future = game.vote_future
            if interaction.user.id not in game.players:
                return await interaction.response.send_message(
                    "You're not in this game.", ephemeral=True
                )
            if future is None or future.done():
                return await interaction.response.send_message(
                    "Voting has closed.", ephemeral=True
                )

            previous = game.voted.get(interaction.user.id)
            game.voted[interaction.user.id] = target_id
            target = game.players.get(target_id)
            label = target.display_name if target is not None else str(target_id)
            note = ""
            if previous is not None and previous != target_id:
                previous_member = game.players.get(previous)
                if previous_member is not None:
                    note = f" (was **{previous_member.display_name}**)"
            await interaction.response.send_message(
                f"🗳️ You voted for **{label}**{note}.", ephemeral=True
            )

            if self.message is not None:
                try:
                    await self.message.edit(embed=self.render(game))
                except discord.DiscordException:
                    pass
            if len(game.voted) >= len(game.players) and not future.done():
                future.set_result(None)

        return callback


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class ChatGames(commands.Cog, name="ChatGames"):
    """Games that involve the whole channel instead of one or two players."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.count_games: dict[int, CountGame] = {}
        self.story_games: dict[int, StoryGame] = {}
        self.potato_games: dict[int, PotatoGame] = {}
        self.deduction_games: dict[int, DeductionGame] = {}
        self.chameleon_games: dict[int, ChameleonGame] = {}
        self._races: set[int] = set()
        self._tasks: set[asyncio.Task[Any]] = set()

    def cog_unload(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        for game in self.potato_games.values():
            if game.future is not None and not game.future.done():
                game.future.cancel()
        self.count_games.clear()
        self.story_games.clear()
        self.potato_games.clear()
        self.deduction_games.clear()
        self.chameleon_games.clear()
        self._races.clear()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await check_slash_guardrails(self, interaction)

    # -- background task plumbing ----------------------------------------

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        """Run *coro* in the background and keep it alive until the cog unloads."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)

        def _done(finished: asyncio.Task[Any]) -> None:
            self._tasks.discard(finished)
            if finished.cancelled():
                return
            error = finished.exception()
            if error is not None:
                logger.error("Chat game task failed", exc_info=error)

        task.add_done_callback(_done)
        return task

    # -- shared helpers ---------------------------------------------------

    async def _prefixes(self, message: discord.Message) -> tuple[str, ...]:
        """The prefixes this server accepts, so listeners can ignore commands."""
        getter = getattr(self.bot, "get_prefix", None)
        if getter is None:
            return ()
        try:
            value = getter(message)
            if inspect.isawaitable(value):
                value = await value
        except Exception:  # pragma: no cover - a broken prefix callback
            return ()
        if isinstance(value, str):
            return (value,)
        if isinstance(value, (list, tuple)):
            return tuple(item for item in value if isinstance(item, str) and item)
        return ()

    async def _is_command(self, message: discord.Message) -> bool:
        content = message.content or ""
        prefixes = await self._prefixes(message)
        return any(content.startswith(prefix) for prefix in prefixes)

    async def _collect_players(
        self,
        message: discord.Message,
        emoji: str,
        *,
        limit: int,
    ) -> dict[int, discord.Member]:
        """Read the join reactions on *message*, one entry per member."""
        try:
            fresh = await message.channel.fetch_message(message.id)
        except discord.DiscordException:
            fresh = message

        guild = message.guild
        players: dict[int, discord.Member] = {}
        for reaction in fresh.reactions:
            if str(reaction.emoji) != emoji:
                continue
            try:
                async for user in reaction.users():
                    if user.bot or len(players) >= limit:
                        continue
                    member = guild.get_member(user.id) if guild is not None else None
                    if member is not None:
                        players[member.id] = member
            except discord.DiscordException:
                logger.exception("Could not read join reactions")
        return players

    @staticmethod
    def _embed(
        title: str,
        description: str,
        *,
        color: int | discord.Color | None = None,
        footer: str | None = None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=title,
            description=description,
            color=color if color is not None else EMBED_COLORS["info"],
        )
        if footer:
            embed.set_footer(text=footer)
        return embed

    # ==================================================================
    # Count to 100
    # ==================================================================

    @commands.group(
        name="count100",
        aliases=["countto100", "countgame"],
        invoke_without_command=True,
        description="Count to 100 together as a channel.",
    )
    @commands.guild_only()
    async def count100(self, ctx: Context) -> None:
        """Count to 100 as a channel, one number at a time.

        Bot admins and members with **Manage Messages** start and stop the run.
        Example: `{prefix}count100 start`
        """
        game = self.count_games.get(ctx.channel.id)
        if game is None:
            prefix = await prefix_for(ctx)
            return await ctx.send(
                embed=self._embed(
                    "Count to 100",
                    "No run is active in this channel.\n\n"
                    f"Staff start one with `{prefix}count100 start`, then the "
                    "channel counts **1, 2, 3...** one message at a time. A "
                    "wrong number, or the same member counting twice in a row, "
                    f"resets the run to 1. Reach **{COUNT_TARGET}** to win.",
                )
            )

        return await ctx.send(
            embed=self._embed(
                "Count to 100",
                f"The count is at **{game.next_number - 1}**. Next number: "
                f"**{game.next_number}**.\n"
                f"Best this run: **{game.best}** · "
                f"Members counted: **{len(game.players)}**",
            )
        )

    @count100.command(name="start")
    @commands.guild_only()
    @_moderator_only()
    async def count100_start(self, ctx: Context) -> None:
        """Start a count-to-100 run in this channel."""
        if ctx.channel.id in self.count_games:
            return await ctx.send(
                embed=self._embed(
                    "Count to 100",
                    "A run is already active here — the count is at "
                    f"**{self.count_games[ctx.channel.id].next_number - 1}**.",
                    color=EMBED_COLORS["warning"],
                )
            )

        prefix = await prefix_for(ctx)
        game = CountGame(
            channel_id=ctx.channel.id,
            guild_id=ctx.guild.id,
            prefix=prefix,
        )
        self.count_games[ctx.channel.id] = game
        await ctx.send(
            embed=self._embed(
                "Count to 100",
                "The run starts now. Count **1, 2, 3...** one number per "
                "member, in order.\n\n"
                "• A wrong number resets the run to 1.\n"
                "• The same member counting twice in a row resets the run.\n"
                f"• Reach **{COUNT_TARGET}** to win.\n\n"
                f"Staff can stop the run with `{prefix}count100 stop`.",
                color=EMBED_COLORS["success"],
                footer="Only this channel counts — one number per message.",
            )
        )

    @count100.command(name="stop")
    @commands.guild_only()
    @_moderator_only()
    async def count100_stop(self, ctx: Context) -> None:
        """End the active count-to-100 run."""
        game = self.count_games.pop(ctx.channel.id, None)
        if game is None:
            return await ctx.send(
                embed=self._embed(
                    "Count to 100",
                    "There is no active run in this channel.",
                    color=EMBED_COLORS["warning"],
                )
            )

        await ctx.send(
            embed=self._embed(
                "Count to 100 — stopped",
                f"The run ended at **{game.next_number - 1}**. Best this run: "
                f"**{game.best}**, from **{len(game.players)}** member(s).",
                color=EMBED_COLORS["neutral"],
                footer="Start a new run with the start command.",
            )
        )

    @count100.command(name="status")
    @commands.guild_only()
    async def count100_status(self, ctx: Context) -> None:
        """Show the current count-to-100 progress."""
        game = self.count_games.get(ctx.channel.id)
        if game is None:
            return await ctx.send(
                embed=self._embed(
                    "Count to 100",
                    "No run is active in this channel.",
                    color=EMBED_COLORS["warning"],
                )
            )
        await ctx.send(
            embed=self._embed(
                "Count to 100",
                f"Next number: **{game.next_number}** · best: **{game.best}** · "
                f"players: **{len(game.players)}**",
            )
        )

    # ==================================================================
    # Emoji reaction race
    # ==================================================================

    @commands.command(
        name="emojirace",
        aliases=["reactionrace", "racestart"],
        description="Race to react with a random emoji.",
    )
    @commands.guild_only()
    @unified_cooldown(10)
    async def emojirace(self, ctx: Context) -> None:
        """First member to react with the announced random emoji wins.

        The bot deliberately does not react with the emoji itself, so nobody can
        win by tapping a ready-made button — you have to find it in the picker.
        """
        if ctx.channel.id in self._races:
            return await ctx.send(
                embed=self._embed(
                    "Emoji Race",
                    "A race is already running in this channel — react to it!",
                    color=EMBED_COLORS["warning"],
                )
            )

        race_emoji, race_name = random.choice(RACE_EMOJIS)
        self._races.add(ctx.channel.id)
        message = await ctx.send(
            embed=self._embed(
                "Emoji Race",
                f"First to react with {race_emoji} wins!\n"
                f"-# Find **{race_name}** in your emoji picker and add it yourself — "
                f"I won't hand you the reaction. You have {int(RACE_TIMEOUT)} seconds.",
                color=EMBED_COLORS["info"],
            )
        )

        def check(reaction: discord.Reaction, user: discord.User) -> bool:
            return (
                not user.bot
                and reaction.message.id == message.id
                and str(reaction.emoji) == race_emoji
            )

        try:
            _reaction, winner = await self.bot.wait_for(
                "reaction_add", check=check, timeout=RACE_TIMEOUT
            )
        except asyncio.TimeoutError:
            await message.edit(
                embed=self._embed(
                    "Emoji Race — nobody won",
                    f"No one reacted with {race_emoji} in time.",
                    color=EMBED_COLORS["warning"],
                )
            )
            return
        finally:
            self._races.discard(ctx.channel.id)

        await message.edit(
            embed=self._embed(
                "Emoji Race — winner!",
                f"{winner.mention} reacted first! {race_emoji}",
                color=EMBED_COLORS["success"],
            )
        )
        await ctx.send(f"🏆 {winner.mention} wins the emoji race!")

    # ==================================================================
    # Hot potato
    # ==================================================================

    @commands.group(
        name="hotpotato",
        aliases=["potato", "hp"],
        invoke_without_command=True,
        description="Pass a ticking potato by tagging another player.",
    )
    @commands.guild_only()
    async def hotpotato(self, ctx: Context) -> None:
        """Open a hot-potato lobby in this channel."""
        await self._start_potato(ctx)

    @hotpotato.command(name="start")
    @commands.guild_only()
    async def hotpotato_start(self, ctx: Context) -> None:
        """Open a hot-potato lobby in this channel."""
        await self._start_potato(ctx)

    async def _start_potato(self, ctx: Context) -> None:
        if ctx.channel.id in self.potato_games:
            return await ctx.send(
                embed=self._embed(
                    "Hot Potato",
                    "A game is already running here — pass the potato!",
                    color=EMBED_COLORS["warning"],
                )
            )

        prefix = await prefix_for(ctx)
        game = PotatoGame(
            channel_id=ctx.channel.id,
            guild_id=ctx.guild.id,
            prefix=prefix,
        )
        self.potato_games[ctx.channel.id] = game

        message = await ctx.send(
            embed=self._embed(
                "Hot Potato",
                f"React with {POTATO_EMOJI} to join!\n"
                f"-# You have {int(JOIN_WINDOW)} seconds — at least 2 players.",
                color=EMBED_COLORS["info"],
            )
        )
        try:
            await message.add_reaction(POTATO_EMOJI)
        except discord.DiscordException:
            self.potato_games.pop(ctx.channel.id, None)
            logger.exception("Could not add the hot-potato reaction")
            return

        self._spawn(self._run_potato(ctx, game, message))

    @hotpotato.command(name="pass")
    @commands.guild_only()
    async def hotpotato_pass(
        self, ctx: Context, member: discord.Member
    ) -> None:
        """Pass the potato to another player (just @mentioning them works too)."""
        game = self.potato_games.get(ctx.channel.id)
        if game is None:
            return await ctx.send(
                embed=self._embed(
                    "Hot Potato",
                    "No game is running in this channel.",
                    color=EMBED_COLORS["warning"],
                )
            )

        error = self._potato_pass_error(game, ctx.author.id, member.id)
        if error is not None:
            return await ctx.send(
                embed=self._embed(
                    "Hot Potato", error, color=EMBED_COLORS["warning"]
                )
            )

        future = game.future
        if future is not None and not future.done():
            future.set_result(member.id)
        try:
            await ctx.message.add_reaction(POTATO_EMOJI)
        except discord.DiscordException:
            pass
        # Prefix players get the reaction above; a slash invocation has a
        # synthetic message, so acknowledge the interaction instead.
        if ctx.interaction is not None:
            await ctx.send(f"🥔 Passed to {member.mention}.")

    @staticmethod
    def _potato_pass_error(
        game: PotatoGame, author_id: int, target_id: int
    ) -> str | None:
        """Why this pass is not allowed, or ``None`` when it is."""
        if author_id != game.holder_id:
            return "You don't have the potato — wait for your turn."
        if target_id == author_id:
            return "You can't pass the potato to yourself."
        if target_id not in game.alive:
            return f"<@{target_id}> isn't in this game."
        if game.future is None or game.future.done():
            return "The potato is being handed over — try again."
        return None

    async def _run_potato(
        self,
        ctx: Context,
        game: PotatoGame,
        message: discord.Message,
    ) -> None:
        """Collect the lobby, then run rounds until one player is left."""
        try:
            await asyncio.sleep(JOIN_WINDOW)
            players = await self._collect_players(
                message, POTATO_EMOJI, limit=POTATO_MAX_PLAYERS
            )
            if len(players) < 2:
                await message.edit(
                    embed=self._embed(
                        "Hot Potato — cancelled",
                        "Not enough players joined. Need at least 2.",
                        color=EMBED_COLORS["warning"],
                    )
                )
                return

            game.players = players
            game.alive = list(players)
            await message.edit(
                embed=self._embed(
                    "Hot Potato — starting",
                    "Players: "
                    + ", ".join(member.mention for member in players.values())
                    + "\n\nMention another player to pass the potato!",
                    color=EMBED_COLORS["success"],
                )
            )
            await self._run_potato_rounds(ctx.channel, game)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Hot potato game failed")
            try:
                await ctx.channel.send(
                    embed=self._embed(
                        "Hot Potato",
                        "The game hit an error and has been stopped.",
                        color=EMBED_COLORS["error"],
                    )
                )
            except discord.DiscordException:
                pass
        finally:
            self.potato_games.pop(game.channel_id, None)
            if game.future is not None and not game.future.done():
                game.future.cancel()
            game.future = None

    async def _run_potato_rounds(
        self, channel: discord.abc.Messageable, game: PotatoGame
    ) -> None:
        loop = asyncio.get_running_loop()

        while len(game.alive) > 1:
            holder_id = random.choice(game.alive)
            game.holder_id = holder_id
            fuse = random.uniform(POTATO_MIN_FUSE, POTATO_MAX_FUSE)
            deadline = loop.time() + fuse
            await channel.send(
                embed=self._embed(
                    "🥔 Hot Potato",
                    f"{self._mention(game, holder_id)} has the potato! "
                    f"Fuse: **{int(fuse)}s**.\n"
                    "Mention another player to pass it.",
                    color=EMBED_COLORS["warning"],
                )
            )

            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                future: asyncio.Future[int] = loop.create_future()
                game.future = future
                try:
                    target_id = await asyncio.wait_for(future, timeout=remaining)
                except asyncio.TimeoutError:
                    break
                finally:
                    game.future = None

                if target_id not in game.alive:
                    continue
                holder_id = target_id
                game.holder_id = holder_id
                remaining = max(0.0, deadline - loop.time())
                await channel.send(
                    embed=self._embed(
                        "🥔 Hot Potato",
                        f"Passed to {self._mention(game, holder_id)}! "
                        f"**{int(remaining)}s** left on the fuse.",
                        color=EMBED_COLORS["warning"],
                    )
                )

            exploded_id = game.holder_id
            game.holder_id = None
            if exploded_id is not None and exploded_id in game.alive:
                game.alive.remove(exploded_id)
            await channel.send(
                embed=self._embed(
                    "💥 Hot Potato",
                    f"The potato exploded on {self._mention(game, exploded_id)}! "
                    f"**{len(game.alive)}** player(s) left.",
                    color=EMBED_COLORS["error"],
                )
            )
            if len(game.alive) > 1:
                await asyncio.sleep(2)

        if not game.alive:
            return
        winner_id = game.alive[0]
        await channel.send(
            embed=self._embed(
                "🏆 Hot Potato — winner!",
                f"{self._mention(game, winner_id)} survived the potato!",
                color=EMBED_COLORS["success"],
            )
        )

    @staticmethod
    def _mention(game: PotatoGame, member_id: int | None) -> str:
        if member_id is None:
            return "someone"
        member = game.players.get(member_id)
        return member.mention if member is not None else f"<@{member_id}>"

    # ==================================================================
    # Social deduction night
    # ==================================================================

    @commands.group(
        name="deduction",
        aliases=["socialdeduction", "mafia"],
        invoke_without_command=True,
        description="Open a social deduction lobby — find the impostor.",
    )
    @commands.guild_only()
    @unified_cooldown(30)
    async def deduction(self, ctx: Context) -> None:
        """Open a social-deduction lobby; players join by reacting."""
        await self._start_deduction(ctx)

    @deduction.command(name="start")
    @commands.guild_only()
    @unified_cooldown(30)
    async def deduction_start(
        self, ctx: Context, seconds: int = int(DISCUSSION_WINDOW)
    ) -> None:
        """Open a lobby with a custom discussion timer, e.g. `deduction start 90`."""
        await self._start_deduction(ctx, seconds)

    async def _start_deduction(
        self, ctx: Context, discussion_seconds: float = DISCUSSION_WINDOW
    ) -> None:
        if ctx.channel.id in self.deduction_games:
            return await ctx.send(
                embed=self._embed(
                    "Social Deduction",
                    "A game is already running in this channel.",
                    color=EMBED_COLORS["warning"],
                )
            )

        seconds = max(
            DISCUSSION_MIN_SECONDS, min(DISCUSSION_MAX_SECONDS, float(discussion_seconds))
        )
        prefix = await prefix_for(ctx)
        game = DeductionGame(
            channel_id=ctx.channel.id,
            guild_id=ctx.guild.id,
            prefix=prefix,
            discussion_seconds=seconds,
        )
        self.deduction_games[ctx.channel.id] = game

        message = await ctx.send(
            embed=self._embed(
                "Social Deduction Night",
                f"React with {DEDUCTION_EMOJI} to join!\n"
                f"-# {int(JOIN_WINDOW)}s to join · "
                f"{DEDUCTION_MIN_PLAYERS}–{DEDUCTION_MAX_PLAYERS} players · "
                f"{int(seconds)}s discussion · roles arrive by DM.",
                color=EMBED_COLORS["info"],
            )
        )
        try:
            await message.add_reaction(DEDUCTION_EMOJI)
        except discord.DiscordException:
            self.deduction_games.pop(ctx.channel.id, None)
            logger.exception("Could not add the deduction reaction")
            return

        self._spawn(self._run_deduction(ctx, game, message))

    async def _run_deduction(
        self,
        ctx: Context,
        game: DeductionGame,
        message: discord.Message,
    ) -> None:
        try:
            await asyncio.sleep(JOIN_WINDOW)
            players = await self._collect_players(
                message, DEDUCTION_EMOJI, limit=DEDUCTION_MAX_PLAYERS
            )
            if len(players) < DEDUCTION_MIN_PLAYERS:
                await message.edit(
                    embed=self._embed(
                        "Social Deduction — cancelled",
                        f"Not enough players joined. Need at least "
                        f"{DEDUCTION_MIN_PLAYERS}.",
                        color=EMBED_COLORS["warning"],
                    )
                )
                return

            game.players = players
            game.alive = list(players)
            impostor_count = 1 if len(players) < 7 else 2
            if not await self._assign_roles(game, impostor_count):
                await message.edit(
                    embed=self._embed(
                        "Social Deduction — cancelled",
                        "I couldn't DM the roles. Make sure members accept DMs "
                        "from this bot, then start again.",
                        color=EMBED_COLORS["error"],
                    )
                )
                return

            await message.edit(
                embed=self._embed(
                    "Social Deduction — starting",
                    f"**{len(players)}** players are in. Roles have been sent "
                    "by DM.\n\n"
                    "Each round: secret night actions in DMs, then discuss and "
                    "vote out an impostor. Everyone gets one vote per round.",
                    color=EMBED_COLORS["success"],
                )
            )
            await self._run_deduction_rounds(ctx.channel, game)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Social deduction game failed")
            try:
                await ctx.channel.send(
                    embed=self._embed(
                        "Social Deduction",
                        "The game hit an error and has been stopped.",
                        color=EMBED_COLORS["error"],
                    )
                )
            except discord.DiscordException:
                pass
        finally:
            self.deduction_games.pop(game.channel_id, None)

    async def _assign_roles(self, game: DeductionGame, impostor_count: int) -> bool:
        """DM every player their role; ``False`` if the impostors can't be told."""
        candidates = list(game.players)
        random.shuffle(candidates)

        for member_id in candidates:
            if len(game.impostors) >= impostor_count:
                break
            member = game.players[member_id]
            if await self._send_role_dm(member, role="impostor"):
                game.impostors.add(member_id)
                game.roles[member_id] = "impostor"

        if len(game.impostors) < impostor_count:
            return False

        remaining = [mid for mid in candidates if mid not in game.impostors]

        if len(game.players) >= DETECTIVE_MIN_PLAYERS and remaining:
            member_id = remaining.pop(0)
            if await self._send_role_dm(game.players[member_id], role="detective"):
                game.detective_id = member_id
                game.roles[member_id] = "detective"
            else:
                remaining.append(member_id)

        if len(game.players) >= MEDIC_MIN_PLAYERS and remaining:
            member_id = remaining.pop(0)
            if await self._send_role_dm(game.players[member_id], role="medic"):
                game.medic_id = member_id
                game.roles[member_id] = "medic"
            else:
                remaining.append(member_id)

        for member_id in remaining:
            game.roles[member_id] = "crew"
            if not await self._send_role_dm(game.players[member_id], role="crew"):
                logger.info(
                    "Could not DM %s their crewmate role in guild %s",
                    member_id,
                    game.guild_id,
                )
        return True

    @staticmethod
    async def _send_role_dm(member: discord.Member, *, role: str) -> bool:
        if role == "impostor":
            title = "You are the Impostor"
            body = (
                "Every night you get a DM with the living crew and pick who "
                "doesn't wake up. Blend in during the day, steer the vote and "
                "survive. You win once the impostors match the crew."
            )
            color = EMBED_COLORS["error"]
        elif role == "detective":
            title = "You are the Detective"
            body = (
                "Each night you can inspect one player and learn whether they "
                "are an impostor. Share the results, sit on them or bluff - "
                "your call."
            )
            color = EMBED_COLORS["info"]
        elif role == "medic":
            title = "You are the Medic"
            body = (
                "Each night you can shield one player from the impostor's "
                "attack. You can't shield the same person twice in a row."
            )
            color = EMBED_COLORS["success"]
        else:
            title = "You are a Crewmate"
            body = (
                "Work out who the impostors are and vote them out. Don't "
                "reveal your role in the channel."
            )
            color = EMBED_COLORS["success"]
        try:
            await member.send(
                embed=discord.Embed(
                    title=title,
                    description=body,
                    color=color,
                )
            )
        except discord.DiscordException as error:
            logger.warning(
                "Could not DM %s their %s card: %s", member.id, role, error
            )
            return False
        return True

    async def _run_deduction_rounds(
        self, channel: discord.abc.Messageable, game: DeductionGame
    ) -> None:
        stalled_rounds = 0
        while True:
            game.round += 1
            if not game.alive or game.round > DEDUCTION_MAX_ROUNDS:
                await self._finish_deduction(channel, game, winner=None)
                return

            await self._run_night(channel, game)
            winner = deduction_winner(game.alive, game.impostors)
            if winner is not None or not game.alive:
                await self._finish_deduction(channel, game, winner=winner)
                return

            await self._run_discussion(channel, game)

            ejected_id = await self._run_vote(channel, game)
            stalled_rounds = 0 if ejected_id is not None else stalled_rounds + 1
            winner = await self._resolve_ejection(channel, game, ejected_id)
            if winner is not None:
                await self._finish_deduction(channel, game, winner=winner)
                return
            if stalled_rounds >= 2:
                await self._finish_deduction(channel, game, winner=None)
                return

    async def _run_night(
        self, channel: discord.abc.Messageable, game: DeductionGame
    ) -> None:
        """Run the secret night actions and apply the kill.

        Impostors pick a victim, the detective inspects a player and the medic
        shields someone. A shielded victim survives; everyone else is removed
        without their role being revealed.
        """
        alive_impostors = [mid for mid in game.alive if mid in game.impostors]
        targets = [mid for mid in game.alive if mid not in game.impostors]
        if not alive_impostors or not targets:
            return

        await channel.send(
            embed=self._embed(
                f"🌙 Night {game.round}",
                "Secret actions are in your DMs. Actions close in up to "
                f"**{int(NIGHT_WINDOW)}s**.",
                color=EMBED_COLORS["neutral"],
            )
        )

        target_members = [game.players[mid] for mid in targets if mid in game.players]
        jobs: list[Any] = []
        kinds: list[tuple[str, int]] = []

        for impostor_id in alive_impostors:
            member = game.players.get(impostor_id)
            if member is None:
                continue
            jobs.append(
                self._dm_pick(
                    member,
                    title="Night — pick your target",
                    body="Who does the crew lose tonight?",
                    options=[
                        target for target in target_members if target.id != impostor_id
                    ],
                    timeout=NIGHT_WINDOW,
                )
            )
            kinds.append(("impostor", impostor_id))

        detective_id = game.detective_id if game.detective_id in game.alive else None
        if detective_id is not None and detective_id in game.players:
            jobs.append(
                self._dm_pick(
                    game.players[detective_id],
                    title="Night — investigate",
                    body="Who do you want to inspect?",
                    options=[
                        game.players[mid]
                        for mid in game.alive
                        if mid != detective_id and mid in game.players
                    ],
                    timeout=NIGHT_WINDOW,
                )
            )
            kinds.append(("detective", detective_id))

        medic_id = game.medic_id if game.medic_id in game.alive else None
        if medic_id is not None and medic_id in game.players:
            jobs.append(
                self._dm_pick(
                    game.players[medic_id],
                    title="Night — protect",
                    body="Who do you want to shield tonight?",
                    options=[
                        game.players[mid]
                        for mid in game.alive
                        if mid not in (medic_id, game.last_protected_id)
                        and mid in game.players
                    ],
                    timeout=NIGHT_WINDOW,
                )
            )
            kinds.append(("medic", medic_id))

        results = await asyncio.gather(*jobs) if jobs else []

        game.protected_id = None
        for (kind, actor_id), choice in zip(kinds, results):
            if kind == "detective" and choice is not None and choice in game.alive:
                is_impostor = choice in game.impostors
                try:
                    await game.players[actor_id].send(
                        embed=self._embed(
                            "Investigation result",
                            f"{self._player_mention(game, choice)} is "
                            f"**{'an impostor' if is_impostor else 'not an impostor'}**.",
                            color=(
                                EMBED_COLORS["error"]
                                if is_impostor
                                else EMBED_COLORS["success"]
                            ),
                        )
                    )
                except discord.DiscordException:
                    logger.info("Could not DM the investigation to %s", actor_id)
            elif kind == "medic" and choice is not None and choice in game.alive:
                game.protected_id = choice

        if game.protected_id is not None:
            game.last_protected_id = game.protected_id

        kills = [
            choice
            for (kind, _actor), choice in zip(kinds, results)
            if kind == "impostor" and choice in targets
        ]
        if not kills:
            await channel.send(
                embed=self._embed(
                    "🌙 Night",
                    "The night passed quietly. Nobody was attacked.",
                    color=EMBED_COLORS["neutral"],
                )
            )
            return

        tally = Counter(kills)
        top = max(tally.values())
        kill_id = random.choice(
            [member_id for member_id, count in tally.items() if count == top]
        )
        if game.protected_id == kill_id:
            await channel.send(
                embed=self._embed(
                    "🛡️ Night",
                    "Someone was attacked — but the medic got there first. "
                    "Nobody died.",
                    color=EMBED_COLORS["success"],
                )
            )
            return

        if kill_id in game.alive:
            game.alive.remove(kill_id)
        await channel.send(
            embed=self._embed(
                "🌙 Night",
                f"{self._player_mention(game, kill_id)} didn't make it through "
                f"the night. **{len(game.alive)}** player(s) left.",
                color=EMBED_COLORS["error"],
            )
        )

    async def _dm_pick(
        self,
        member: discord.Member,
        *,
        title: str,
        body: str,
        options: Sequence[discord.Member],
        timeout: float,
    ) -> Optional[int]:
        """DM a numbered list and wait for the member to reply with a number."""
        if not options:
            return None
        listing = "\n".join(
            f"**{index}.** {option.display_name}"
            for index, option in enumerate(options, 1)
        )
        try:
            await member.send(
                embed=self._embed(
                    title,
                    f"{body}\n\n{listing}\n\n-# Reply with a number "
                    f"within {int(timeout)}s.",
                    color=EMBED_COLORS["info"],
                )
            )
        except discord.DiscordException:
            return None

        def check(message: discord.Message) -> bool:
            return (
                message.author.id == member.id
                and message.guild is None
                and (message.content or "").strip().rstrip(".").isdigit()
            )

        try:
            reply = await self.bot.wait_for("message", check=check, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        index = int((reply.content or "").strip().rstrip("."))
        if 1 <= index <= len(options):
            return options[index - 1].id
        return None

    async def _run_discussion(
        self, channel: discord.abc.Messageable, game: DeductionGame
    ) -> None:
        """Open the day phase with a countdown that skips on request."""
        title = f"Social Deduction — Round {game.round}"
        body = "Discuss who the impostors are. Voting starts when the timer ends."

        def render(remaining: float) -> discord.Embed:
            return self._embed(
                title,
                f"{body}\n\n-# {max(1, int(remaining))}s left · "
                f"React with {SKIP_EMOJI} to vote early.",
                color=EMBED_COLORS["info"],
            )

        message = await channel.send(
            embed=render(game.discussion_seconds)
        )
        try:
            await message.add_reaction(SKIP_EMOJI)
        except discord.DiscordException:
            pass
        await self._tick_embed(
            message,
            render,
            game.discussion_seconds,
            skip_emoji=SKIP_EMOJI,
            skip_allowed=lambda user: user.id in game.alive,
        )

    async def _tick_embed(
        self,
        message: discord.Message,
        render: Any,
        seconds: float,
        *,
        skip_emoji: Optional[str] = None,
        skip_allowed: Any = None,
    ) -> None:
        """Edit *message* every few seconds until *seconds* run out or it's skipped."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(1.0, seconds)

        def check(reaction: discord.Reaction, user: discord.User) -> bool:
            return (
                not user.bot
                and reaction.message.id == message.id
                and str(reaction.emoji) == skip_emoji
                and (skip_allowed is None or skip_allowed(user))
            )

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            if skip_emoji is not None:
                try:
                    await self.bot.wait_for(
                        "reaction_add",
                        check=check,
                        timeout=min(COUNTDOWN_STEP, remaining),
                    )
                    return
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(min(COUNTDOWN_STEP, remaining))

            remaining = max(0.0, deadline - loop.time())
            if remaining <= 0:
                return
            try:
                await message.edit(embed=render(remaining))
            except discord.DiscordException:
                return

    async def _run_vote(
        self, channel: discord.abc.Messageable, game: DeductionGame
    ) -> Optional[int]:
        alive_ids = [member_id for member_id in game.alive if member_id in game.players]
        if not alive_ids:
            return None

        lines = [
            f"{VOTE_EMOJIS[index]} {game.players[member_id].mention}"
            for index, member_id in enumerate(alive_ids[: len(VOTE_EMOJIS)])
        ]
        title = f"Vote — Round {game.round}"

        def render(remaining: float) -> discord.Embed:
            return self._embed(
                title,
                "Who is the impostor?\n\n"
                + "\n".join(lines)
                + f"\n\n-# {max(1, int(remaining))}s left. Ties eject nobody.",
                color=EMBED_COLORS["warning"],
            )

        vote_message = await channel.send(embed=render(VOTE_WINDOW))
        for index in range(min(len(alive_ids), len(VOTE_EMOJIS))):
            try:
                await vote_message.add_reaction(VOTE_EMOJIS[index])
            except discord.DiscordException:
                logger.exception("Could not add a vote reaction")

        await self._tick_embed(vote_message, render, VOTE_WINDOW)

        try:
            vote_message = await channel.fetch_message(vote_message.id)
        except discord.DiscordException:
            return None

        by_emoji = {
            VOTE_EMOJIS[index]: member_id
            for index, member_id in enumerate(alive_ids[: len(VOTE_EMOJIS)])
        }
        # A member who reacts to more than one candidate has not made up their
        # mind, so their vote is dropped rather than decided by reaction order.
        choices: dict[int, set[int]] = {}
        for reaction in vote_message.reactions:
            target_id = by_emoji.get(str(reaction.emoji))
            if target_id is None:
                continue
            try:
                async for user in reaction.users():
                    if user.bot or user.id not in alive_ids:
                        continue
                    choices.setdefault(user.id, set()).add(target_id)
            except discord.DiscordException:
                logger.exception("Could not read a vote reaction")

        tally: dict[int, int] = {member_id: 0 for member_id in alive_ids}
        for voter_id, targets in choices.items():
            if len(targets) != 1:
                continue
            target_id = next(iter(targets))
            tally[target_id] += 1
            game.votes[voter_id] = target_id
            # Track who read the room right; impostors voting an impostor
            # count too, they are just rarely the ones being graded on it.
            if target_id in game.impostors:
                game.correct_votes[voter_id] = (
                    game.correct_votes.get(voter_id, 0) + 1
                )

        ejected_id, had_votes = decide_ejection(tally)
        if ejected_id is None:
            await channel.send(
                embed=self._embed(
                    f"Vote — Round {game.round}",
                    "The vote was tied — nobody was ejected."
                    if had_votes
                    else "Nobody voted — nobody was ejected.",
                    color=EMBED_COLORS["warning"],
                )
            )
        return ejected_id

    async def _resolve_ejection(
        self,
        channel: discord.abc.Messageable,
        game: DeductionGame,
        ejected_id: Optional[int],
    ) -> Optional[str]:
        """Announce the ejection and return the winning side (or ``None``)."""
        if ejected_id is not None:
            if ejected_id in game.alive:
                game.alive.remove(ejected_id)
            role = game.roles.get(ejected_id, "crew")
            if role == "impostor":
                game.impostors.discard(ejected_id)
            await channel.send(
                embed=self._embed(
                    "Ejection",
                    f"{self._player_mention(game, ejected_id)} was ejected — "
                    f"they were **{ROLE_NAMES.get(role, 'Crewmate')}**.",
                    color=(
                        EMBED_COLORS["error"]
                        if role == "impostor"
                        else EMBED_COLORS["neutral"]
                    ),
                )
            )

        return deduction_winner(game.alive, game.impostors)

    async def _finish_deduction(
        self,
        channel: discord.abc.Messageable,
        game: DeductionGame,
        *,
        winner: Optional[str],
    ) -> None:
        """Post the end screen with every role and record the stats."""
        if winner == "crew":
            headline = "Every impostor is out — **the Crewmates win!**"
            color = EMBED_COLORS["success"]
        elif winner == "impostors":
            headline = "The impostors now match the crew — **the Impostors win!**"
            color = EMBED_COLORS["error"]
        else:
            headline = "The table ran out of road — **no winner**."
            color = EMBED_COLORS["neutral"]

        role_lines = [
            f"• {self._player_mention(game, member_id)} — "
            f"**{ROLE_NAMES.get(role, 'Crewmate')}**"
            for member_id, role in sorted(game.roles.items())
        ]
        description = headline + "\n\n**Roles**\n" + "\n".join(role_lines)

        reads = [
            (member_id, count)
            for member_id, count in game.correct_votes.items()
            if count
        ]
        if reads:
            reads.sort(key=lambda item: -item[1])
            description += "\n\n**Reads** (votes against an impostor)\n" + "\n".join(
                f"• {self._player_mention(game, member_id)} — **{count}**"
                for member_id, count in reads[:10]
            )

        await channel.send(
            embed=self._embed(
                "Social Deduction — game over",
                description,
                color=color,
                footer=f"{len(game.players)} players · {game.round} round(s)",
            )
        )
        self._record_deduction(game, winner)

    def _record_deduction(
        self, game: DeductionGame, winner: Optional[str]
    ) -> None:
        """Persist the finished game so `deductionstats` can show a board."""
        names = {
            member_id: (
                member.display_name
                if member is not None
                else str(member_id)
            )
            for member_id, member in game.players.items()
        }
        try:
            store = load_deduction_stats()
            record_deduction_game(
                store,
                game.guild_id,
                winner=winner,
                roles=game.roles,
                correct_votes=game.correct_votes,
                names=names,
            )
            save_deduction_stats(store)
        except Exception:
            logger.exception("Could not save social deduction stats")

    @commands.command(
        name="deductionstats",
        aliases=["mafiastats"],
        description="Social deduction leaderboard.",
    )
    @commands.guild_only()
    async def deductionstats(
        self, ctx: Context, member: Optional[discord.Member] = None
    ) -> None:
        """Show this server's deduction leaderboard, or one member's record."""
        store = load_deduction_stats()
        if member is not None:
            entry = deduction_stats_entry(store, ctx.guild.id, member.id)
            if not entry:
                return await ctx.send(
                    embed=self._embed(
                        "Social Deduction Stats",
                        f"{member.mention} hasn't finished a game yet.",
                        color=EMBED_COLORS["warning"],
                    )
                )
            games = int(entry.get("games", 0))
            wins = int(entry.get("wins", 0))
            impostor_games = int(entry.get("impostor_games", 0))
            impostor_wins = int(entry.get("impostor_wins", 0))
            await ctx.send(
                embed=self._embed(
                    "Social Deduction Stats",
                    f"{member.mention} — **{wins}/{games}** wins "
                    f"({int(wins * 100 / games) if games else 0}%).\n"
                    f"• Impostor: **{impostor_wins}/{impostor_games}** wins\n"
                    f"• Correct reads: **{int(entry.get('correct_votes', 0))}** "
                    "vote(s) against an impostor",
                    color=EMBED_COLORS["info"],
                    footer="Every completed game is counted.",
                )
            )
            return

        board = deduction_leaderboard(store, ctx.guild.id)
        if not board:
            return await ctx.send(
                embed=self._embed(
                    "Social Deduction Stats",
                    "No completed games in this server yet.",
                    color=EMBED_COLORS["warning"],
                )
            )
        lines = [
            f"**{index}.** {entry.get('name') or f'<@{member_id}>'} — "
            f"**{int(entry.get('wins', 0))}** win(s) in "
            f"{int(entry.get('games', 0))} game(s)"
            for index, (member_id, entry) in enumerate(board, 1)
        ]
        await ctx.send(
            embed=self._embed(
                "Social Deduction — leaderboard",
                "\n".join(lines),
                color=EMBED_COLORS["info"],
                footer="Wins, then games played.",
            )
        )

    @staticmethod
    def _player_mention(game: DeductionGame, member_id: int) -> str:
        member = game.players.get(member_id)
        return member.mention if member is not None else f"<@{member_id}>"

    # ==================================================================
    # One word story
    # ==================================================================

    @commands.group(
        name="story",
        aliases=["onewordstory", "wordstory"],
        invoke_without_command=True,
        description="Build a story one word at a time.",
    )
    @commands.guild_only()
    async def story(self, ctx: Context) -> None:
        """Build a story together, one word per member."""
        game = self.story_games.get(ctx.channel.id)
        prefix = await prefix_for(ctx)
        if game is None:
            return await ctx.send(
                embed=self._embed(
                    "One Word Story",
                    "No story is running in this channel.\n\n"
                    f"Start one with `{prefix}story start` — everyone adds one "
                    "word per message, and the starter or staff can end it with "
                    f"`{prefix}story end`.",
                )
            )

        await ctx.send(
            embed=self._embed(
                "One Word Story",
                f"**{len(game.words)}** word(s) from "
                f"**{len(game.authors)}** member(s).\n"
                f"Last word: **{game.words[-1][1] if game.words else '—'}**",
            )
        )

    @story.command(name="start")
    @commands.guild_only()
    @unified_cooldown(10)
    async def story_start(self, ctx: Context) -> None:
        """Start a one-word story in this channel."""
        if ctx.channel.id in self.story_games:
            return await ctx.send(
                embed=self._embed(
                    "One Word Story",
                    "A story is already running in this channel.",
                    color=EMBED_COLORS["warning"],
                )
            )

        prefix = await prefix_for(ctx)
        game = StoryGame(
            channel_id=ctx.channel.id,
            guild_id=ctx.guild.id,
            starter_id=ctx.author.id,
            prefix=prefix,
        )
        self.story_games[ctx.channel.id] = game
        await ctx.send(
            embed=self._embed(
                "One Word Story",
                "The story starts now. Each member adds **one word per "
                "message** in this channel.\n\n"
                "• No links, mentions or multi-word messages.\n"
                "• The same member can't add two words in a row.\n"
                f"• The starter or staff can finish it with `{prefix}story end`.\n"
                f"• It ends automatically at {STORY_MAX_WORDS} words.\n"
                "• At the end, the AI writes a real story that uses every word "
                "the channel came up with.",
                color=EMBED_COLORS["success"],
                footer="Everything else in this channel is ignored while the story runs.",
            )
        )

    @story.command(name="end")
    @commands.guild_only()
    async def story_end(self, ctx: Context) -> None:
        """Finish the story and post the result."""
        game = self.story_games.get(ctx.channel.id)
        if game is None:
            return await ctx.send(
                embed=self._embed(
                    "One Word Story",
                    "There is no active story in this channel.",
                    color=EMBED_COLORS["warning"],
                )
            )
        if ctx.author.id != game.starter_id and not await _is_chat_moderator(ctx):
            return await ctx.send(
                embed=self._embed(
                    "One Word Story",
                    "Only the member who started the story or a moderator can "
                    "end it.",
                    color=EMBED_COLORS["warning"],
                )
            )
        await self._finish_story(ctx.channel, game)

    async def _finish_story(
        self,
        channel: discord.abc.Messageable,
        game: StoryGame,
        *,
        automatic: bool = False,
    ) -> None:
        self.story_games.pop(game.channel_id, None)
        title = "One Word Story — " + ("finished" if automatic else "ended")
        if not game.words:
            return await channel.send(
                embed=self._embed(
                    title,
                    "The story ended before anyone added a word.",
                    color=EMBED_COLORS["warning"],
                )
            )

        attribution = f"{len(game.words)} words from {len(game.authors)} member(s)"
        notice = await channel.send(
            embed=self._embed(
                "One Word Story — writing",
                f"Turning **{len(game.words)}** words into a story, give me a "
                "moment...",
                footer=attribution,
            )
        )

        words = [word for _, word in game.words]
        roster = build_story_roster(
            getattr(channel, "guild", None), words, game.authors
        )
        story = await self._write_story(words, roster)
        footer = attribution + " · woven by AI"
        if not story:
            # The model was unavailable or came back empty; the raw words are
            # still better than losing the channel's story entirely.
            story = render_story(game.words)
            footer = attribution + " · AI unavailable, raw words shown"
        else:
            # Every retry was spent and a few words still did not fit; say so
            # rather than quietly dropping what the channel wrote.
            leftover = list(dict.fromkeys(missing_story_words(words, story)))
            if leftover:
                footer += " · couldn't fit: " + ", ".join(leftover)[:180]

        embed = self._embed(title, story, color=EMBED_COLORS["success"], footer=footer)
        try:
            await notice.edit(embed=embed)
        except discord.DiscordException:
            await channel.send(embed=embed)

    async def _write_story(
        self,
        words: Sequence[str],
        roster: Sequence[tuple[str, str]] = (),
    ) -> str | None:
        """Ask the AI to weave *words* into a story, or ``None`` on failure.

        The words are checked back against the reply, and any the model skipped
        are handed back to it for another try. The best attempt is returned so
        an uncooperative model never loses the channel's work. *roster* names the
        server members the model may use as characters.
        """
        try:
            from cogs.ai import MODEL_ID, client as ai_client, types
        except Exception:
            logger.exception("The AI cog is unavailable for story generation")
            return None

        prompt = build_story_prompt(words, roster)
        best: tuple[int, str] | None = None
        for _attempt in range(STORY_AI_ATTEMPTS):
            config = types.GenerateContentConfig(
                system_instruction=STORY_SYSTEM_INSTRUCTION,
                temperature=STORY_AI_TEMPERATURE,
                max_output_tokens=STORY_AI_MAX_OUTPUT_TOKENS,
            )
            contents = [
                types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
            ]
            try:
                response = await asyncio.to_thread(
                    ai_client.models.generate_content,
                    model=MODEL_ID,
                    contents=contents,
                    config=config,
                )
                reply = clean_story_output(getattr(response, "text", "") or "")
            except Exception:
                logger.exception("Story generation failed")
                break
            if not reply:
                break

            missing = missing_story_words(words, reply)
            if not missing:
                return reply
            if best is None or len(missing) < best[0]:
                best = (len(missing), reply)
            prompt += (
                "\n\nYour previous attempt left these words out: "
                + ", ".join(missing)
                + ". Rewrite the WHOLE story and use every single one of them, "
                "spelled exactly as listed."
            )

        return best[1] if best is not None else None

    # ==================================================================
    # Chameleon
    # ==================================================================

    @commands.command(
        name="chameleon",
        aliases=["spyfall", "cham"],
        description="Everyone gets a secret word except one — find the Chameleon.",
    )
    @commands.guild_only()
    @unified_cooldown(20)
    async def chameleon(self, ctx: Context) -> None:
        """Open a Chameleon lobby in this channel."""
        if ctx.channel.id in self.chameleon_games:
            return await ctx.send(
                embed=self._embed(
                    "Chameleon",
                    "A game is already running in this channel.",
                    color=EMBED_COLORS["warning"],
                )
            )

        prefix = await prefix_for(ctx)
        game = ChameleonGame(
            channel_id=ctx.channel.id,
            guild_id=ctx.guild.id,
            prefix=prefix,
        )
        self.chameleon_games[ctx.channel.id] = game

        message = await ctx.send(
            embed=self._embed(
                "Chameleon",
                f"React with {CHAMELEON_EMOJI} to join!\n"
                f"-# {int(JOIN_WINDOW)}s to join · "
                f"{CHAMELEON_MIN_PLAYERS}–{CHAMELEON_MAX_PLAYERS} players · "
                "everyone gets a secret word except one. Roles arrive by DM.",
                color=EMBED_COLORS["info"],
            )
        )
        try:
            await message.add_reaction(CHAMELEON_EMOJI)
        except discord.DiscordException:
            self.chameleon_games.pop(ctx.channel.id, None)
            logger.exception("Could not add the chameleon reaction")
            return

        self._spawn(self._run_chameleon(ctx, game, message))

    async def _run_chameleon(
        self,
        ctx: Context,
        game: ChameleonGame,
        message: discord.Message,
    ) -> None:
        try:
            await asyncio.sleep(JOIN_WINDOW)
            players = await self._collect_players(
                message, CHAMELEON_EMOJI, limit=CHAMELEON_MAX_PLAYERS
            )
            if len(players) < CHAMELEON_MIN_PLAYERS:
                await message.edit(
                    embed=self._embed(
                        "Chameleon — cancelled",
                        f"Not enough players joined. Need at least "
                        f"{CHAMELEON_MIN_PLAYERS}.",
                        color=EMBED_COLORS["warning"],
                    )
                )
                return

            game.players = players
            lobby_size = len(players)
            dropped = await self._deal_chameleon_roles(game, message.channel)
            for member_id in dropped:
                game.players.pop(member_id, None)
            if (
                game.chameleon_id is None
                or len(game.players) < CHAMELEON_MIN_PLAYERS
            ):
                await message.edit(
                    embed=self._embed(
                        "Chameleon — cancelled",
                        self._chameleon_failure(game, dropped, lobby_size),
                        color=EMBED_COLORS["error"],
                    )
                )
                return

            game.order = list(game.players)
            random.shuffle(game.order)
            note = ""
            if dropped:
                names = ", ".join(f"<@{member_id}>" for member_id in dropped)
                note = (
                    f"\n\n-# I couldn't reach {names} at all, so they sat this "
                    "round out."
                )
            if game.fallback_threads:
                names = ", ".join(
                    f"<@{member_id}>" for member_id in game.fallback_threads
                )
                note += (
                    f"\n-# {names}: your DMs are closed, so your card is in a "
                    "private thread here instead."
                )
            await message.edit(
                embed=self._embed(
                    "Chameleon — starting",
                    f"**{len(game.players)}** players are in. Your role and "
                    "the secret word (if you have one) are in your DMs (or in "
                    "a private thread if DMs are closed).\n\n"
                    "One player never got a word — that's the Chameleon. Go "
                    "round in the order below with **one-word clues**, then "
                    "vote for the odd one out.\n\n"
                    "**Clue order:** "
                    + " → ".join(
                        game.players[member_id].display_name
                        for member_id in game.order
                    )
                    + note,
                    color=EMBED_COLORS["success"],
                )
            )

            await self._run_chameleon_clues(ctx.channel, game)
            accused = await self._run_chameleon_vote(ctx.channel, game)
            await self._finish_chameleon(ctx.channel, game, accused)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Chameleon game failed")
            try:
                await ctx.channel.send(
                    embed=self._embed(
                        "Chameleon",
                        "The game hit an error and has been stopped.",
                        color=EMBED_COLORS["error"],
                    )
                )
            except discord.DiscordException:
                pass
        finally:
            self.chameleon_games.pop(game.channel_id, None)
            for future in (game.clue_future, game.vote_future):
                if future is not None and not future.done():
                    future.cancel()
            # Last, so a cancelled task still clears its own state first.
            await self._close_chameleon_threads(game)

    async def _close_chameleon_threads(self, game: ChameleonGame) -> None:
        """Delete any private threads used to hand out role cards."""
        for thread in list(game.fallback_threads.values()):
            try:
                await thread.delete()
            except discord.DiscordException:
                logger.info("Could not delete chameleon thread %s", thread.id)
        game.fallback_threads.clear()

    async def _deal_chameleon_roles(
        self,
        game: ChameleonGame,
        channel: Optional[discord.abc.GuildChannel] = None,
    ) -> list[int]:
        """Hand out roles and the word; returns anyone who couldn't be reached.

        The Chameleon is picked from the players who can actually receive a
        card, because a Chameleon who never sees one doesn't know they are it.
        """
        word = random.choice(CHAMELEON_WORDS)
        candidates = list(game.players)
        random.shuffle(candidates)

        for member_id in candidates:
            member = game.players[member_id]
            if await self._send_chameleon_card(
                game, member, word=word, is_chameleon=True, channel=channel
            ):
                game.chameleon_id = member_id
                break
        if game.chameleon_id is None:
            return candidates

        game.word = word
        dropped: list[int] = []
        for member_id in candidates:
            if member_id == game.chameleon_id:
                continue
            member = game.players[member_id]
            if not await self._send_chameleon_card(
                game, member, word=word, is_chameleon=False, channel=channel
            ):
                dropped.append(member_id)
            # Space the cards out so a big lobby never trips a DM rate limit.
            await asyncio.sleep(0.2)
        return dropped

    @staticmethod
    def _chameleon_card_embed(word: str, is_chameleon: bool) -> discord.Embed:
        if is_chameleon:
            return discord.Embed(
                title="You are the Chameleon",
                description=(
                    "Everyone else was given a secret word and you were not. "
                    "Give one-word clues that could plausibly fit any word, "
                    "and bluff your way through the round.\n\n"
                    "If the room votes for you, you still get one guess at the "
                    "word — get it right and you win anyway."
                ),
                color=EMBED_COLORS["purple"],
            )
        return discord.Embed(
            title="Your secret word",
            description=(
                f"The word is **{word}**.\n\n"
                "Don't say it in the channel. Give a one-word clue that "
                "fits it, then vote for whoever looks like they're "
                "guessing — the Chameleon never saw this word."
            ),
            color=EMBED_COLORS["info"],
        )

    async def _send_chameleon_card(
        self,
        game: ChameleonGame,
        member: discord.Member,
        *,
        word: str,
        is_chameleon: bool,
        channel: Optional[discord.abc.GuildChannel] = None,
    ) -> bool:
        """Send one role card, falling back to a private thread.

        A member with DMs closed can never be reached that way, so the card
        goes into a private thread in the game channel that only they can
        read. That keeps the round playable without anyone seeing another
        player's word.
        """
        embed = self._chameleon_card_embed(word, is_chameleon)
        for attempt in (1, 2):
            try:
                await member.send(embed=embed)
                return True
            except discord.Forbidden as error:
                # DMs are switched off for this member; retrying won't help.
                logger.info(
                    "DMs closed for %s, trying a private thread (%s)",
                    member.id,
                    error,
                )
                break
            except discord.DiscordException as error:
                logger.warning(
                    "Could not DM %s a chameleon card (attempt %s): %s",
                    member.id,
                    attempt,
                    error,
                )
                if attempt == 2:
                    break
                await asyncio.sleep(1.0)

        if channel is None or not hasattr(channel, "create_thread"):
            return False
        try:
            thread = await channel.create_thread(
                name=f"🦎 {member.display_name}"[:95],
                type=discord.ChannelType.private_thread,
                invitable=False,
                auto_archive_duration=60,
                reason="Chameleon role card (DMs closed)",
            )
            await thread.add_user(member)
            await thread.send(content=member.mention, embed=embed)
        except discord.DiscordException as error:
            logger.warning(
                "Could not open a private thread for %s: %s", member.id, error
            )
            return False
        game.fallback_threads[member.id] = thread
        logger.info(
            "Delivered the chameleon card to %s through thread %s",
            member.id,
            thread.id,
        )
        return True

    @staticmethod
    def _chameleon_failure(
        game: ChameleonGame, dropped: list[int], lobby_size: int
    ) -> str:
        """Explain exactly who could not be handed a card, and why."""
        if game.chameleon_id is None:
            return (
                "I couldn't hand **anyone** a role — I couldn't DM them and I "
                "wasn't allowed to open a private thread, so nobody could be "
                "told their word.\n\n-# Ask everyone to allow *Direct Messages* "
                "from server members, or give me **Create Private Threads** in "
                "this channel, then start again."
            )
        names = ", ".join(f"<@{member_id}>" for member_id in dropped)
        return (
            f"I could only reach **{len(game.players)}** of **{lobby_size}** "
            f"players. {names} couldn't be told their word — DMs are closed "
            "for them and I wasn't allowed to open a private thread. I need at "
            f"least {CHAMELEON_MIN_PLAYERS} reachable players.\n\n-# Ask them "
            "to enable *Allow direct messages from server members*, or give me "
            "**Create Private Threads** here."
        )

    def _render_chameleon_turn(
        self, game: ChameleonGame, remaining: float
    ) -> discord.Embed:
        try:
            member_id = game.order[game.turn_index]
        except IndexError:
            member_id = None
        member = game.players.get(member_id) if member_id is not None else None
        mention = member.mention if member is not None else "someone"
        lines = [
            f"**{game.players[member_id].display_name}**: {clue}"
            for member_id, clue in game.clues.items()
            if member_id in game.players
        ]
        body = f"{mention}, post your **one-word clue** for the word.\n"
        if lines:
            body += "\n" + "\n".join(lines) + "\n"
        body += (
            f"\n-# Clue {min(game.turn_index + 1, len(game.order))} of "
            f"{len(game.order)} · {max(1, int(remaining))}s left."
        )
        return self._embed("Chameleon — clues", body, color=EMBED_COLORS["info"])

    async def _await_chameleon_clue(
        self, game: ChameleonGame, message: discord.Message
    ) -> str | None:
        """Wait for the current player's clue, ticking the timer down."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CHAMELEON_CLUE_SECONDS
        while True:
            remaining = deadline - loop.time()
            future = game.clue_future
            if remaining <= 0 or future is None:
                return None
            # ``wait`` leaves the future pending when it times out, so the
            # same one keeps taking this player's message while the clock runs.
            done, _ = await asyncio.wait(
                {future}, timeout=min(COUNTDOWN_STEP, remaining)
            )
            if done:
                try:
                    return future.result()
                except (asyncio.CancelledError, asyncio.InvalidStateError):
                    return None
            remaining = max(0.0, deadline - loop.time())
            if remaining <= 0:
                return None
            try:
                await message.edit(
                    embed=self._render_chameleon_turn(game, remaining)
                )
            except discord.DiscordException:
                return None

    async def _run_chameleon_clues(
        self, channel: discord.abc.Messageable, game: ChameleonGame
    ) -> None:
        order = [member_id for member_id in game.order if member_id in game.players]
        game.order = order
        message = await channel.send(
            embed=self._render_chameleon_turn(game, CHAMELEON_CLUE_SECONDS)
        )

        for index, member_id in enumerate(order):
            game.turn_index = index
            member = game.players.get(member_id)
            if member is None:
                continue
            game.clue_future = asyncio.get_running_loop().create_future()
            clue = await self._await_chameleon_clue(game, message)
            game.clue_future = None
            if clue is None:
                await channel.send(
                    embed=self._embed(
                        "Chameleon — no clue",
                        f"{member.mention} didn't post a clue in time, so the "
                        "round moves on without one.",
                        color=EMBED_COLORS["warning"],
                    )
                )
                continue
            game.clues[member_id] = clue

        await message.edit(
            embed=self._embed(
                "Chameleon — clues are in",
                "\n".join(
                    f"**{game.players[member_id].display_name}**: {clue}"
                    for member_id, clue in game.clues.items()
                    if member_id in game.players
                )
                or "No clues this round.",
                color=EMBED_COLORS["success"],
                footer="Voting opens below.",
            )
        )

    def _chameleon_vote_embed(
        self, game: ChameleonGame, *, counts: Optional[dict[int, int]] = None
    ) -> discord.Embed:
        lines = [
            f"**{game.players[member_id].display_name}**: {clue}"
            for member_id, clue in game.clues.items()
            if member_id in game.players
        ]
        body = "Who is the Chameleon?\n\n"
        body += "\n".join(lines) + "\n\n" if lines else "\n"
        if counts is not None:
            tally = " · ".join(
                f"<@{member_id}> {count}"
                for member_id, count in sorted(
                    counts.items(), key=lambda item: -item[1]
                )
            )
            body += f"**Votes:** {tally}"
        else:
            body += (
                f"-# {len(game.voted)}/{len(game.players)} votes in · ties and "
                "no-votes leave the Chameleon uncaught."
            )
        return self._embed("Chameleon — vote", body, color=EMBED_COLORS["warning"])

    async def _run_chameleon_vote(
        self, channel: discord.abc.Messageable, game: ChameleonGame
    ) -> Optional[int]:
        view = _ChameleonVoteView(game, self._chameleon_vote_embed)
        game.vote_future = asyncio.get_running_loop().create_future()
        message = await channel.send(
            embed=self._chameleon_vote_embed(game), view=view
        )
        view.message = message
        await asyncio.wait({game.vote_future}, timeout=CHAMELEON_VOTE_SECONDS)
        game.vote_future = None
        view.stop()

        accused, counts = chameleon_tally(game.voted, list(game.players))
        try:
            await message.edit(
                embed=self._chameleon_vote_embed(game, counts=counts), view=None
            )
        except discord.DiscordException:
            pass
        return accused

    async def _chameleon_last_guess(
        self, game: ChameleonGame, chameleon: discord.Member
    ) -> Optional[str]:
        """The caught Chameleon's one shot at the word, over DMs."""
        prompt = discord.Embed(
            title="Caught — one guess left",
            description=(
                "The vote landed on you. Reply here with the secret "
                f"word within {int(CHAMELEON_GUESS_SECONDS)}s and you "
                "still win.\n\n-# One guess, no hints."
            ),
            color=EMBED_COLORS["warning"],
        )
        # A player whose DMs are closed got their card in a private thread.
        target: Any = chameleon
        thread = game.fallback_threads.get(game.chameleon_id or 0)
        if thread is not None:
            target = thread
        try:
            await target.send(embed=prompt)
        except discord.DiscordException:
            logger.info("Could not send the chameleon their last guess")
            return None

        guess_channel_id = getattr(target, "id", None)

        def check(message: discord.Message) -> bool:
            if message.author.id != game.chameleon_id:
                return False
            if isinstance(message.channel, discord.DMChannel):
                return True
            return message.channel.id == guess_channel_id

        try:
            reply = await self.bot.wait_for(
                "message", check=check, timeout=CHAMELEON_GUESS_SECONDS
            )
        except asyncio.TimeoutError:
            return None
        except Exception:
            logger.exception("Waiting for the chameleon's guess failed")
            return None
        return (reply.content or "").strip() or None

    async def _finish_chameleon(
        self,
        channel: discord.abc.Messageable,
        game: ChameleonGame,
        accused: Optional[int],
    ) -> None:
        chameleon = game.players.get(game.chameleon_id or 0)
        mention = chameleon.mention if chameleon is not None else "the Chameleon"
        _, counts = chameleon_tally(game.voted, list(game.players))

        if accused is None:
            verdict = (
                "The vote didn't single anyone out, so nobody was caught. "
                "**The Chameleon wins.**"
            )
            chameleon_won = True
        elif accused == game.chameleon_id:
            await channel.send(
                embed=self._embed(
                    "Chameleon — caught!",
                    f"{mention} was voted out. They get "
                    f"{int(CHAMELEON_GUESS_SECONDS)}s in DMs to guess the word "
                    "and steal the win…",
                    color=EMBED_COLORS["warning"],
                )
            )
            guess = (
                await self._chameleon_last_guess(game, chameleon)
                if chameleon is not None
                else None
            )
            if guess is None:
                verdict = (
                    f"{mention} was caught but never guessed the word. "
                    "**The crew wins.**"
                )
                chameleon_won = False
            elif chameleon_guess_ok(guess, game.word):
                verdict = (
                    f"{mention} got caught, then guessed **{guess}** — correct. "
                    "**The Chameleon steals the win.**"
                )
                chameleon_won = True
            else:
                verdict = (
                    f"{mention} got caught and guessed **{guess}** — wrong. "
                    "**The crew wins.**"
                )
                chameleon_won = False
        else:
            accused_mention = f"<@{accused}>"
            verdict = (
                f"The room voted for {accused_mention}, who was not the "
                "Chameleon. **The Chameleon wins.**"
            )
            chameleon_won = True

        clue_lines = [
            f"**{game.players[member_id].display_name}**: {clue}"
            for member_id, clue in game.clues.items()
            if member_id in game.players
        ]
        vote_line = " · ".join(
            f"<@{member_id}> {count}"
            for member_id, count in sorted(counts.items(), key=lambda item: -item[1])
        )
        description = "\n".join(
            [
                verdict,
                "",
                f"**Word:** {game.word}",
                f"**Chameleon:** {mention}",
                "",
                "**Clues**",
                "\n".join(clue_lines) if clue_lines else "-# no clues were given",
                "",
                f"**Votes:** {vote_line}" if vote_line else "**Votes:** none",
            ]
        )
        await channel.send(
            embed=self._embed(
                "Chameleon — the Chameleon wins"
                if chameleon_won
                else "Chameleon — the crew wins",
                description,
                color=EMBED_COLORS["purple"]
                if chameleon_won
                else EMBED_COLORS["success"],
            )
        )

    # ==================================================================
    # Message listener for count, story, potato and chameleon clues
    # ==================================================================

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        try:
            if message.channel.id in self.count_games:
                await self._handle_count_message(message)
            if message.channel.id in self.story_games:
                await self._handle_story_message(message)
            if message.channel.id in self.potato_games:
                await self._handle_potato_message(message)
            if message.channel.id in self.chameleon_games:
                await self._handle_chameleon_message(message)
        except Exception:  # pragma: no cover - a listener must never crash the bot
            logger.exception("Chat game message handling failed")

    async def _handle_count_message(self, message: discord.Message) -> None:
        game = self.count_games.get(message.channel.id)
        if game is None:
            return
        content = (message.content or "").strip()
        if not content.isdigit():
            return

        number = int(content)
        expected = game.next_number
        result = advance_count(game, message.author.id, number)

        if result == "counted":
            if number % 10 == 0 or number == 1:
                try:
                    await message.add_reaction("✅")
                except discord.DiscordException:
                    pass
            return

        if result == "reset":
            await message.channel.send(
                embed=self._embed(
                    "Count to 100 — back to 1",
                    f"{message.author.mention} said **{number}**, but the next "
                    f"number was **{expected}**. The count resets.",
                    color=EMBED_COLORS["error"],
                )
            )
            return

        if result == "double":
            await message.channel.send(
                embed=self._embed(
                    "Count to 100 — back to 1",
                    f"{message.author.mention} counted twice in a row. The "
                    "count resets.",
                    color=EMBED_COLORS["error"],
                )
            )
            return

        # result == "won"
        self.count_games.pop(message.channel.id, None)
        await message.channel.send(
            embed=self._embed(
                "🎉 Count to 100 — complete!",
                f"The channel reached **{COUNT_TARGET}** with "
                f"**{len(game.players)}** member(s) taking part!",
                color=EMBED_COLORS["success"],
            )
        )

    async def _handle_potato_message(self, message: discord.Message) -> None:
        """Pass the potato when the holder simply @mentions another player."""
        game = self.potato_games.get(message.channel.id)
        if game is None or not message.raw_mentions:
            return
        if await self._is_command(message):
            # ``hotpotato pass @member`` is handled by the command itself.
            return
        author_id = message.author.id
        if author_id != game.holder_id:
            # Only the holder can hand the potato off, and only by tagging a
            # player who is still in the game.
            return

        target_id = next(
            (
                user_id
                for user_id in message.raw_mentions
                if user_id != author_id and user_id in game.alive
            ),
            None,
        )
        if target_id is None:
            return
        if self._potato_pass_error(game, author_id, target_id) is not None:
            return

        future = game.future
        if future is None or future.done():
            return
        future.set_result(target_id)
        try:
            await message.add_reaction(POTATO_EMOJI)
        except discord.DiscordException:
            pass

    async def _handle_chameleon_message(self, message: discord.Message) -> None:
        """Take the current player's message as a clue when it's a single word."""
        game = self.chameleon_games.get(message.channel.id)
        if game is None or game.clue_future is None or game.clue_future.done():
            return
        if await self._is_command(message):
            return
        if not (0 <= game.turn_index < len(game.order)):
            return
        if message.author.id != game.order[game.turn_index]:
            return
        if message.author.id not in game.players:
            return
        content = (message.content or "").strip()
        if not chameleon_clue_is_valid(content):
            return

        future = game.clue_future
        if future is not None and not future.done():
            future.set_result(content)
        try:
            await message.add_reaction("✅")
        except discord.DiscordException:
            pass

    async def _handle_story_message(self, message: discord.Message) -> None:
        game = self.story_games.get(message.channel.id)
        if game is None:
            return
        if await self._is_command(message):
            return
        if message.author.id == game.last_author_id:
            return

        content = message.content or ""
        # A number that belongs to a count run is not a story word.
        if content.strip().isdigit() and message.channel.id in self.count_games:
            return
        if not valid_story_word(content):
            return

        word = content.strip()
        game.words.append((message.author.id, word))
        game.authors.add(message.author.id)
        game.last_author_id = message.author.id

        if len(game.words) >= STORY_MAX_WORDS:
            await self._finish_story(message.channel, game, automatic=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ChatGames(bot))
