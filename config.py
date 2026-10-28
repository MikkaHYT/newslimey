"""Shared configuration for the ported refcogs / refutils code.

The reference bot kept its tunables in a single ``config`` module. Those cogs
import constants from here (``EMBED_COLOR``, ``PREFIX``, ``leveling`` ...), so
this file provides the same names for the features that were ported over. Only
the values the ported cogs actually read are defined; everything else is kept as
a sensible default so imports never fail.
"""

from __future__ import annotations

from types import SimpleNamespace

# ---------------------------------------------------------------------------
# General / prefix
# ---------------------------------------------------------------------------
PREFIX = "!"

# ---------------------------------------------------------------------------
# Colours (matched to the main bot's Component/Embed palette)
# ---------------------------------------------------------------------------
EMBED_COLOR = 0x2B2D31

EMBED_COLORS = {
    "success": 0x57F287,
    "error": 0xED4245,
    "warning": 0xFEE75C,
    "info": 0x5865F2,
    "gold": 0xF1C40F,
    "purple": 0xD75BF4,
    "neutral": 0x2B2D31,
}

# ---------------------------------------------------------------------------
# Mixtapes
# ---------------------------------------------------------------------------
#: Project names whose Discord emoji does not match the slug of their name.
PROJECT_EMOJI_NAMES: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
#: Ping replies used by the info cog of the reference bot.
PING_RESPONSES: dict[str, str] = {}

SHOP_ITEMS: dict = {}
JOBS: dict = {}

# ---------------------------------------------------------------------------
# Leveling
# ---------------------------------------------------------------------------
#: Per-level XP is ``base + growth * (n - 1) + quadratic * (n - 1)^2``. Every
#: coefficient is doubled relative to the previous 125 / 55 / 12, so each level
#: costs exactly twice the XP it used to - the whole curve, not just the tail.
leveling = SimpleNamespace(
    base_level_xp=250,
    level_xp_growth=110,
    level_xp_quadratic=24,
    xp_per_message=15,
    xp_per_voice_minute=10,
    xp_cooldown=60,
    announce_level_ups=True,
    level_up_channel=None,
    level_reward=100,
    level_reward_growth=25,
    milestone_every=10,
    milestone_reward=1000,
)

# ---------------------------------------------------------------------------
# Economy
# ---------------------------------------------------------------------------
economy = SimpleNamespace(
    currency_name="coins",
    currency_emoji="🪙",
    starting_balance=0,
    transfer_fee_percent=0,
    daily_cooldown=86400,
    daily_bonus=250,
    daily_streak_bonus=50,
    max_daily_streak=30,
    weekly_cooldown=604800,
    weekly_bonus=1500,
    monthly_cooldown=2592000,
    monthly_bonus=6000,
)

# ---------------------------------------------------------------------------
# Casino
# ---------------------------------------------------------------------------
casino = SimpleNamespace(
    minimum_bet=10,
    coinflip_payout=2.0,
    challenge_timeout=60,
    crash_edge=0.01,
    crash_max=100.0,
    plinko_rows=12,
    supergamble_chance=0.05,
    supergamble_cooldown=3600,
    supergamble_min=100,
    supergamble_max=10000,
)

# ---------------------------------------------------------------------------
# Moderation
# ---------------------------------------------------------------------------
moderation = SimpleNamespace(
    mute_role="Muted",
    jail_role="Jailed",
)

# ---------------------------------------------------------------------------
# Achievements
# ---------------------------------------------------------------------------
ACHIEVEMENT_TARGETS = {
    "streak_7": 7,
    "thousandaire": 1000,
    "high_roller": 10000,
    "jackpot": 1000,
    "level_10": 10,
}

ACHIEVEMENTS: dict[str, dict] = {
    "first_daily": {
        "title": "First Payday",
        "emoji": "📅",
        "description": "Claim your first daily reward.",
        "reward": 100,
    },
    "streak_7": {
        "title": "Week Streak",
        "emoji": "🔥",
        "description": "Claim the daily reward 7 days in a row.",
        "reward": 500,
    },
    "thousandaire": {
        "title": "Thousandaire",
        "emoji": "💰",
        "description": "Hold 1,000 coins at once.",
        "reward": 500,
    },
    "collector": {
        "title": "Collector",
        "emoji": "🎒",
        "description": "Own at least one item from the shop.",
        "reward": 250,
    },
    "high_roller": {
        "title": "High Roller",
        "emoji": "🎲",
        "description": "Wager 10,000 coins in total.",
        "reward": 1000,
    },
    "jackpot": {
        "title": "Jackpot",
        "emoji": "💎",
        "description": "Win 1,000 coins in a single game.",
        "reward": 750,
    },
    "level_10": {
        "title": "Rising Star",
        "emoji": "⭐",
        "description": "Reach level 10.",
        "reward": 1000,
    },
}
