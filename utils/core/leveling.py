"""Pure level maths, shared by the level and economy cogs.

The curve is quadratic: level ``n`` costs
``base + growth * (n - 1) + quadratic * (n - 1)^2`` XP, so early levels fly by
and later ones turn into real grinds without ever hitting a hard cap. The three
coefficients live in :data:`config.leveling`, so the whole curve can be retuned
without touching this module. Everything in here is side-effect free and easy to
unit test.
"""

from __future__ import annotations

from dataclasses import dataclass

from config import leveling

#: Characters used by the progress bars.
BAR_FILLED = "▰"
BAR_EMPTY = "▱"
DEFAULT_BAR_LENGTH = 12


def xp_for_level(level: int) -> int:
    """XP needed to walk from ``level`` to ``level + 1``."""
    if level < 1:
        level = 1
    step = level - 1
    return (
        leveling.base_level_xp
        + leveling.level_xp_growth * step
        + leveling.level_xp_quadratic * step * step
    )


def total_xp_for_level(level: int) -> int:
    """Total XP needed to reach ``level`` from scratch."""
    if level <= 1:
        return 0
    # Closed form of the sum above, kept as a loop-sized formula for clarity.
    n = level - 1
    base = leveling.base_level_xp
    growth = leveling.level_xp_growth
    quadratic = leveling.level_xp_quadratic
    return (
        base * n
        + growth * n * (n - 1) // 2
        + quadratic * n * (n - 1) * (2 * n - 1) // 6
    )


def total_xp(level: int, xp: int) -> int:
    """A member's lifetime XP from their level and progress inside it."""
    return total_xp_for_level(level) + max(0, int(xp))


def level_reward(level: int) -> tuple[int, bool]:
    """Coins paid for reaching ``level``, and whether it was a milestone.

    The payout climbs with the level instead of being flat: level 2 pays
    ``level_reward``, and every level after that adds ``level_reward_growth``,
    so the jump from level 40 to 41 is worth far more than 2 to 3. A milestone
    bonus lands on top every ``milestone_every`` levels.
    """
    level = max(1, int(level))
    reward = leveling.level_reward + leveling.level_reward_growth * (level - 1)
    milestone = leveling.milestone_every > 0 and level % leveling.milestone_every == 0
    if milestone:
        reward += leveling.milestone_reward
    return reward, milestone


def level_reward_only(level: int) -> int:
    """Just the cash a level pays, ignoring the milestone bonus."""
    return level_reward(level)[0]


def progress_bar(current: int, needed: int, length: int = DEFAULT_BAR_LENGTH) -> str:
    """A filled/empty bar for ``current`` out of ``needed``."""
    if needed <= 0:
        return BAR_FILLED * length
    filled = max(0, min(length, round(length * current / needed)))
    return BAR_FILLED * filled + BAR_EMPTY * (length - filled)


@dataclass(slots=True)
class LevelUp:
    """One level crossing, ready to be announced and paid out."""

    level: int
    reward: int
    milestone: bool


def apply_xp(level: int, xp: int, gained: int, *, max_levels: int = 500) -> tuple[int, int, list[LevelUp]]:
    """Add ``gained`` XP on top of a level's progress.

    Returns ``(new_level, xp_into_level, level_ups)``. ``max_levels`` only
    guards against absurd single grants (an admin handing out 10^9 XP) looping
    forever - it is not a real level cap.
    """
    level = max(1, int(level))
    xp = max(0, int(xp)) + max(0, int(gained))

    level_ups: list[LevelUp] = []
    while len(level_ups) < max_levels:
        needed = xp_for_level(level)
        if xp < needed:
            break
        xp -= needed
        level += 1
        reward, milestone = level_reward(level)
        level_ups.append(LevelUp(level=level, reward=reward, milestone=milestone))

    return level, xp, level_ups


def position_badge(position: int) -> str:
    """Medals for the podium, a plain badge for everyone else."""
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(position, f"`#{position}`")
