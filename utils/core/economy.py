"""Wallet/bank engine behind ``,balance``, ``,daily``, ``,work`` and the casino.

Every money change funnels through here so the ledger, the lifetime counters and
the wallet/bank split can never drift apart. Mutations take an ``asyncio.Lock``
because a read-modify-write that spans two users (a transfer, a robbery, a bet
payout) has to be atomic against other commands running at the same moment.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from config import ACHIEVEMENTS, ACHIEVEMENT_TARGETS, economy

logger = logging.getLogger("slimey.economy")

CURRENCY_NAME: str = economy.currency_name
CURRENCY_EMOJI: str = economy.currency_emoji


def compact_number(amount: int | float) -> str:
    """``compact_number(12_500)`` -> ``12.5k``.

    Used wherever a member has asked for short money (their own cards), so a
    long wallet doesn't wrap the line it sits on.
    """
    value = float(amount)
    for limit, suffix in (
        (1_000_000_000_000, "t"),
        (1_000_000_000, "b"),
        (1_000_000, "m"),
        (1_000, "k"),
    ):
        if abs(value) >= limit:
            scaled = value / limit
            text = f"{scaled:.1f}".rstrip("0").rstrip(".")
            return f"{text}{suffix}"
    return f"{int(value):,}"


def format_cash(amount: int | float, *, compact: bool = False) -> str:
    """``format_cash(1234)`` -> ``🟢 1,234``.

    ``compact=True`` gives ``🟢 1.2k`` instead, which is what the
    ``compact_money`` preference asks for on a member's own cards.
    """
    if compact:
        return f"{CURRENCY_EMOJI} {compact_number(amount)}"
    return f"{CURRENCY_EMOJI} {int(amount):,}"


def format_cash_short(amount: int | float) -> str:
    """Just the grouped number, for tight spots like a bet line."""
    return f"{int(amount):,}"


def parse_amount(raw: str | None, available: int) -> int | None:
    """Read a user-typed bet or transfer amount.

    Understands plain numbers, ``all``, ``half``, percentages (``25%``) and
    short suffixes (``1.5k``, ``2m``). Returns ``None`` when the text makes no
    sense, and clamps to ``available`` so "all" can never overdraw.
    """
    if raw is None:
        return None
    text = str(raw).strip().lower().replace(",", "").replace("_", "")
    if not text:
        return None

    if text in {"all", "max", "everything"}:
        return max(0, int(available))
    if text in {"half", "1/2"}:
        return max(0, int(available) // 2)

    if text.endswith("%"):
        try:
            percent = float(text[:-1])
        except ValueError:
            return None
        if percent <= 0:
            return None
        return max(0, min(int(available), int(available * percent / 100)))

    multiplier = 1
    for suffix, factor in (("k", 1_000), ("m", 1_000_000), ("b", 1_000_000_000)):
        if text.endswith(suffix):
            multiplier = factor
            text = text[: -len(suffix)]
            break

    try:
        value = float(text)
    except ValueError:
        return None
    if value <= 0:
        return None

    amount = int(value * multiplier)
    return max(0, min(int(available), amount)) if available else max(0, amount)


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


@dataclass(slots=True)
class Account:
    user_id: int
    wallet: int = 0
    bank: int = 0
    daily_streak: int = 0
    daily_claimed_at: datetime | None = None
    lifetime_earned: int = 0
    lifetime_spent: int = 0

    @property
    def total(self) -> int:
        return self.wallet + self.bank


@dataclass(slots=True)
class Claim:
    """One ``!daily`` / ``!weekly`` / ``!monthly`` crossing."""

    kind: str
    amount: int
    streak: int
    streak_broken: bool


@dataclass(slots=True)
class Achievement:
    """An unlockable achievement, resolved from ``config.ACHIEVEMENTS``."""

    key: str
    title: str
    emoji: str
    description: str
    reward: int


def _achievement(key: str) -> Achievement:
    data = ACHIEVEMENTS[key]
    return Achievement(
        key=key,
        title=str(data["title"]),
        emoji=str(data["emoji"]),
        description=str(data["description"]),
        reward=int(data["reward"]),
    )


#: Timed rewards the engine can hand out, as ``kind -> (cooldown, base amount)``.
def claim_spec(kind: str) -> tuple[int, int] | None:
    if kind == "daily":
        return economy.daily_cooldown, economy.daily_bonus
    if kind == "weekly":
        return economy.weekly_cooldown, economy.weekly_bonus
    if kind == "monthly":
        return economy.monthly_cooldown, economy.monthly_bonus
    return None


@dataclass(slots=True)
class TransferResult:
    ok: bool
    reason: str | None = None
    amount: int = 0
    fee: int = 0
    sender: Account | None = None
    receiver: Account | None = None


class EconomyEngine:
    """Single source of truth for balances."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Storage primitives
    # ------------------------------------------------------------------
    @staticmethod
    def _build(row: tuple[Any, ...] | None, user_id: int) -> Account:
        if not row:
            return Account(user_id=user_id)
        return Account(
            user_id=int(row[0]),
            wallet=int(row[1] or 0),
            bank=int(row[2] or 0),
            daily_streak=int(row[3] or 0),
            daily_claimed_at=_as_datetime(row[4]),
            lifetime_earned=int(row[5] or 0),
            lifetime_spent=int(row[6] or 0),
        )

    async def _ensure(self, user_id: int) -> None:
        await self.bot.db.execute(
            "INSERT INTO economy_accounts (user_id, wallet, bank) VALUES (%s, %s, 0) "
            "ON CONFLICT(user_id) DO NOTHING",
            int(user_id),
            int(economy.starting_balance),
        )

    async def account(self, user_id: int) -> Account:
        """Read (and lazily create) an account."""
        user_id = int(user_id)
        row = await self._account_row(user_id)
        if row is None:
            async with self._lock:
                await self._ensure(user_id)
            row = await self._account_row(user_id)
        return self._build(row, user_id)

    async def _account_row(self, user_id: int) -> tuple[Any, ...] | None:
        return await self.bot.db.fetchrow(
            "SELECT user_id, wallet, bank, daily_streak, daily_claimed_at, "
            "lifetime_earned, lifetime_spent FROM economy_accounts WHERE user_id = %s",
            int(user_id),
        )

    async def _record(self, user_id: int, amount: int, description: str, after: int) -> None:
        if amount == 0:
            return
        await self.bot.db.execute(
            "INSERT INTO economy_transactions "
            "(user_id, amount, balance_after, description, created_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            int(user_id),
            int(amount),
            int(after),
            description[:120],
            datetime.now(timezone.utc),
        )

    async def _mutate(
        self,
        user_id: int,
        *,
        wallet: int = 0,
        bank: int = 0,
        description: str,
        earned: int = 0,
        spent: int = 0,
    ) -> Account:
        """Apply a wallet/bank delta and write the matching ledger row."""
        user_id = int(user_id)
        async with self._lock:
            await self._ensure(user_id)
            await self.bot.db.execute(
                "UPDATE economy_accounts SET "
                "wallet = wallet + %s, bank = bank + %s, "
                "lifetime_earned = lifetime_earned + %s, "
                "lifetime_spent = lifetime_spent + %s "
                "WHERE user_id = %s",
                int(wallet),
                int(bank),
                int(earned),
                int(spent),
                user_id,
            )
            row = await self._account_row(user_id)

        account = self._build(row, user_id)
        await self._record(user_id, wallet, description, account.total)
        return account

    # ------------------------------------------------------------------
    # Public money movements
    # ------------------------------------------------------------------
    async def add(
        self,
        user_id: int,
        amount: int,
        *,
        to_bank: bool = False,
        description: str = "Payout",
    ) -> Account:
        amount = max(0, int(amount))
        return await self._mutate(
            user_id,
            bank=amount if to_bank else 0,
            wallet=0 if to_bank else amount,
            description=description,
            earned=amount,
        )

    async def remove(
        self,
        user_id: int,
        amount: int,
        *,
        from_bank: bool = False,
        description: str = "Payment",
    ) -> tuple[bool, Account]:
        """Spend ``amount``. Returns ``(succeeded, account)``."""
        amount = max(0, int(amount))
        account = await self.account(user_id)
        available = account.bank if from_bank else account.wallet
        if amount > available:
            return False, account

        updated = await self._mutate(
            user_id,
            bank=-amount if from_bank else 0,
            wallet=0 if from_bank else -amount,
            description=description,
            spent=amount,
        )
        return True, updated

    async def move(
        self, user_id: int, amount: int, *, to_bank: bool, description: str
    ) -> tuple[bool, Account]:
        """Shift money between the wallet and the bank."""
        amount = max(0, int(amount))
        account = await self.account(user_id)
        if amount <= 0:
            return False, account
        available = account.wallet if to_bank else account.bank
        if amount > available:
            return False, account

        direction = 1 if to_bank else -1
        updated = await self._mutate(
            user_id,
            wallet=-amount * direction,
            bank=amount * direction,
            description=description,
        )
        return True, updated

    async def transfer(self, sender_id: int, receiver_id: int, amount: int) -> TransferResult:
        amount = int(amount)
        if sender_id == receiver_id:
            return TransferResult(False, "You can't pay yourself.")
        if amount <= 0:
            return TransferResult(False, "Pick an amount above zero.")

        fee = amount * max(0, economy.transfer_fee_percent) // 100
        delivered = amount - fee

        async with self._lock:
            await self._ensure(sender_id)
            await self._ensure(receiver_id)
            sender = self._build(await self._account_row(sender_id), sender_id)
            if sender.wallet < amount:
                return TransferResult(
                    False,
                    f"You only have {format_cash(sender.wallet)} in your wallet — "
                    "the bank doesn't pay for this one.",
                    sender=sender,
                )

            await self.bot.db.execute(
                "UPDATE economy_accounts SET wallet = wallet - %s, "
                "lifetime_spent = lifetime_spent + %s WHERE user_id = %s",
                amount,
                amount,
                int(sender_id),
            )
            await self.bot.db.execute(
                "UPDATE economy_accounts SET wallet = wallet + %s, "
                "lifetime_earned = lifetime_earned + %s WHERE user_id = %s",
                delivered,
                delivered,
                int(receiver_id),
            )
            sender_row = await self._account_row(sender_id)
            receiver_row = await self._account_row(receiver_id)

        sender = self._build(sender_row, sender_id)
        receiver = self._build(receiver_row, receiver_id)
        await self._record(sender_id, -amount, f"Sent {delivered:,} to {receiver_id}", sender.total)
        await self._record(receiver_id, delivered, f"Received from {sender_id}", receiver.total)
        return TransferResult(
            True, amount=amount, fee=fee, sender=sender, receiver=receiver
        )

    # ------------------------------------------------------------------
    # Daily streak
    # ------------------------------------------------------------------
    def daily_ready_in(self, account: Account, *, now: datetime | None = None) -> float:
        """Seconds until ``!daily`` is available again (0 when ready)."""
        if account.daily_claimed_at is None:
            return 0.0
        now = now or datetime.now(timezone.utc)
        ready_at = account.daily_claimed_at + timedelta(seconds=economy.daily_cooldown)
        return max(0.0, (ready_at - now).total_seconds())

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    async def recent(self, user_id: int, limit: int = 4) -> list[tuple[int, str]]:
        """The newest ledger rows as ``(amount, description)``."""
        rows = await self.bot.db.execute(
            "SELECT amount, description FROM economy_transactions "
            "WHERE user_id = %s ORDER BY transaction_id DESC LIMIT %s",
            int(user_id),
            int(limit),
        )
        return [(int(amount), str(description)) for amount, description in rows]

    async def leaderboard(self, limit: int = 10) -> list[Account]:
        rows = await self.bot.db.execute(
            "SELECT user_id, wallet, bank, daily_streak, daily_claimed_at, "
            "lifetime_earned, lifetime_spent FROM economy_accounts "
            "ORDER BY (wallet + bank) DESC, wallet DESC LIMIT %s",
            int(limit),
        )
        return [self._build(tuple(row), int(row[0])) for row in rows]

    async def rank(self, user_id: int) -> int:
        """1-based wealth rank for a user (everyone starts at #1 when broke)."""
        account = await self.account(user_id)
        row = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM economy_accounts WHERE (wallet + bank) > %s",
            account.total,
        )
        return int(row or 0) + 1

    # ------------------------------------------------------------------
    # Timed rewards
    # ------------------------------------------------------------------
    async def _last_claim(self, user_id: int, kind: str) -> tuple[datetime, int]:
        """``(claimed_at, streak)`` for a timed reward, or ``(epoch, 0)``."""
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        row = await self.bot.db.fetchrow(
            "SELECT claimed_at, streak FROM economy_claims "
            "WHERE user_id = %s AND claim = %s",
            int(user_id),
            kind,
        )
        if row:
            return _as_datetime(row[0]) or epoch, int(row[1] or 0)

        # Accounts written before ``economy_claims`` existed only tracked the
        # daily, so honour that stamp rather than letting a stale streak reset
        # or an extra claim slip through on the first run after an upgrade.
        if kind == "daily":
            account = await self.account(user_id)
            if account.daily_claimed_at is not None:
                return account.daily_claimed_at, account.daily_streak
        return epoch, 0

    async def claim_ready_in(self, user_id: int, kind: str) -> float:
        """Seconds until ``kind`` is claimable again (0 when it is ready)."""
        spec = claim_spec(kind)
        if spec is None:
            return 0.0
        cooldown, _ = spec
        claimed_at, _ = await self._last_claim(user_id, kind)
        if claimed_at.timestamp() <= 0:
            return 0.0
        ready_at = claimed_at + timedelta(seconds=cooldown)
        return max(0.0, (ready_at - datetime.now(timezone.utc)).total_seconds())

    async def claim(
        self, user_id: int, kind: str, *, now: datetime | None = None
    ) -> Claim | None:
        """Claim a timed reward, or ``None`` when it is not ready yet.

        ``daily`` keeps the streak behaviour it always had (miss a day and it
        restarts) and is scaled by the streak; ``weekly`` and ``monthly`` pay
        their flat bonus. Every kind is stored in ``economy_claims`` so adding
        another needs no schema change.
        """
        spec = claim_spec(kind)
        if spec is None:
            raise KeyError(f"Unknown claim {kind!r}")
        cooldown, base = spec
        now = now or datetime.now(timezone.utc)
        user_id = int(user_id)
        window = timedelta(seconds=max(1, cooldown))

        async with self._lock:
            await self._ensure(user_id)
            claimed_at, previous = await self._last_claim(user_id, kind)
            if claimed_at.timestamp() > 0 and now - claimed_at < window:
                return None

            missed = claimed_at.timestamp() > 0 and now - claimed_at >= window * 2
            streak = 1 if missed else previous + 1

            amount = base
            if kind == "daily":
                streak = min(streak, max(1, economy.max_daily_streak))
                amount += economy.daily_streak_bonus * (streak - 1)
                if streak >= economy.max_daily_streak:
                    # A full streak pays the base reward again on top.
                    amount += economy.daily_bonus

            await self.bot.db.execute(
                "INSERT INTO economy_claims (user_id, claim, claimed_at, streak) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT(user_id, claim) DO UPDATE SET "
                "claimed_at = excluded.claimed_at, streak = excluded.streak",
                user_id,
                kind,
                now,
                streak,
            )
            if kind == "daily":
                # ``!balance`` and the level card read the streak from here.
                await self.bot.db.execute(
                    "UPDATE economy_accounts SET daily_streak = %s, "
                    "daily_claimed_at = %s WHERE user_id = %s",
                    streak,
                    now,
                    user_id,
                )

            await self.bot.db.execute(
                "UPDATE economy_accounts SET wallet = wallet + %s, "
                "lifetime_earned = lifetime_earned + %s WHERE user_id = %s",
                amount,
                amount,
                user_id,
            )
            row = await self._account_row(user_id)

        account = self._build(row, user_id)
        await self._record(user_id, amount, f"{kind.title()} reward (streak {streak})", account.total)
        return Claim(
            kind=kind, amount=amount, streak=streak, streak_broken=missed and kind == "daily"
        )

    # ------------------------------------------------------------------
    # Inventory
    # ------------------------------------------------------------------
    async def inventory(self, user_id: int) -> dict[str, int]:
        """``item_key -> quantity``, skipping anything fully sold off."""
        rows = await self.bot.db.execute(
            "SELECT item_key, quantity FROM economy_inventory "
            "WHERE user_id = %s AND quantity > 0 ORDER BY item_key",
            int(user_id),
        )
        return {str(key): int(quantity) for key, quantity in rows}

    async def add_item(self, user_id: int, item_key: str, quantity: int = 1) -> int:
        """Add to the inventory and return the new quantity."""
        quantity = max(1, int(quantity))
        await self.bot.db.execute(
            "INSERT INTO economy_inventory (user_id, item_key, quantity) "
            "VALUES (%s, %s, %s) ON CONFLICT(user_id, item_key) DO UPDATE SET "
            "quantity = quantity + excluded.quantity",
            int(user_id),
            item_key,
            quantity,
        )
        owned = await self.bot.db.fetchval(
            "SELECT quantity FROM economy_inventory WHERE user_id = %s AND item_key = %s",
            int(user_id),
            item_key,
        )
        return int(owned or 0)

    async def remove_item(self, user_id: int, item_key: str, quantity: int = 1) -> bool:
        """Take items out of the inventory; ``False`` when they don't have them."""
        quantity = max(1, int(quantity))
        async with self._lock:
            owned = await self.bot.db.fetchval(
                "SELECT quantity FROM economy_inventory "
                "WHERE user_id = %s AND item_key = %s",
                int(user_id),
                item_key,
            )
            if int(owned or 0) < quantity:
                return False
            await self.bot.db.execute(
                "UPDATE economy_inventory SET quantity = quantity - %s "
                "WHERE user_id = %s AND item_key = %s",
                quantity,
                int(user_id),
                item_key,
            )
        return True

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------
    async def job(self, user_id: int) -> tuple[str, int] | None:
        """``(job_key, shifts worked)``, or ``None`` when unemployed."""
        row = await self.bot.db.fetchrow(
            "SELECT job_key, shifts FROM economy_jobs WHERE user_id = %s",
            int(user_id),
        )
        if not row:
            return None
        return str(row[0]), int(row[1] or 0)

    async def set_job(self, user_id: int, job_key: str | None) -> None:
        """Hire (``job_key``) or fire (``None``) a member."""
        if job_key is None:
            await self.bot.db.execute(
                "DELETE FROM economy_jobs WHERE user_id = %s", int(user_id)
            )
            return
        await self.bot.db.execute(
            "INSERT INTO economy_jobs (user_id, job_key, hired_at, shifts) "
            "VALUES (%s, %s, %s, 0) ON CONFLICT(user_id) DO UPDATE SET "
            "job_key = excluded.job_key, hired_at = excluded.hired_at, shifts = 0",
            int(user_id),
            job_key,
            datetime.now(timezone.utc),
        )

    async def record_shift(self, user_id: int) -> None:
        await self.bot.db.execute(
            "UPDATE economy_jobs SET shifts = shifts + 1 WHERE user_id = %s",
            int(user_id),
        )

    # ------------------------------------------------------------------
    # Achievements
    # ------------------------------------------------------------------
    async def unlocked(self, user_id: int) -> set[str]:
        rows = await self.bot.db.fetch(
            "SELECT achievement_key FROM economy_achievements WHERE user_id = %s",
            int(user_id),
        )
        return {str(key) for key in rows}

    async def snapshot(self, user_id: int) -> dict[str, int]:
        """Everything an achievement rule can key off, in one round of reads."""
        user_id = int(user_id)
        account = await self.account(user_id)
        claims = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM economy_claims WHERE user_id = %s AND claim = 'daily'",
            user_id,
        )
        items = await self.bot.db.fetchval(
            "SELECT COUNT(*) FROM economy_inventory "
            "WHERE user_id = %s AND quantity > 0",
            user_id,
        )
        wagered = await self.bot.db.fetchval(
            "SELECT SUM(wagered) FROM gambling_stats WHERE user_id = %s", user_id
        )
        biggest = await self.bot.db.fetchval(
            "SELECT MAX(biggest_win) FROM gambling_stats WHERE user_id = %s", user_id
        )
        # Levelling is per server while the economy is not, so the level rule
        # reads the member's best level anywhere rather than an arbitrary guild.
        level = await self.bot.db.fetchval(
            "SELECT MAX(level) FROM user_levels WHERE user_id = %s", user_id
        )
        return {
            "balance": account.total,
            "daily_streak": account.daily_streak,
            "daily_claims": max(int(claims or 0), 1 if account.daily_claimed_at else 0),
            "items": int(items or 0),
            "wagered": int(wagered or 0),
            "biggest_win": int(biggest or 0),
            "level": int(level or 1),
        }

    @staticmethod
    def earned_achievements(snapshot: dict[str, int]) -> list[str]:
        """Achievement keys a snapshot already satisfies, in catalogue order."""
        earned: list[str] = []
        if snapshot.get("daily_claims", 0) > 0:
            earned.append("first_daily")
        if snapshot.get("daily_streak", 0) >= ACHIEVEMENT_TARGETS["streak_7"]:
            earned.append("streak_7")
        if snapshot.get("balance", 0) >= ACHIEVEMENT_TARGETS["thousandaire"]:
            earned.append("thousandaire")
        if snapshot.get("items", 0) > 0:
            earned.append("collector")
        if snapshot.get("wagered", 0) >= ACHIEVEMENT_TARGETS["high_roller"]:
            earned.append("high_roller")
        if snapshot.get("biggest_win", 0) >= ACHIEVEMENT_TARGETS["jackpot"]:
            earned.append("jackpot")
        if snapshot.get("level", 1) >= ACHIEVEMENT_TARGETS["level_10"]:
            earned.append("level_10")
        return [key for key in earned if key in ACHIEVEMENTS]

    async def grant_achievements(self, user_id: int) -> list[Achievement]:
        """Unlock and pay for anything newly earned. Safe to call anywhere.

        Called after every payout worth checking, which means a member earns an
        achievement the moment the underlying counter crosses its target rather
        than only when they happen to run a command.
        """
        user_id = int(user_id)
        snapshot = await self.snapshot(user_id)
        wanted = self.earned_achievements(snapshot)
        if not wanted:
            return []

        held = await self.unlocked(user_id)
        fresh = [key for key in wanted if key not in held]
        if not fresh:
            return []

        now = datetime.now(timezone.utc)
        for key in fresh:
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO economy_achievements "
                "(user_id, achievement_key, unlocked_at) VALUES (%s, %s, %s)",
                user_id,
                key,
                now,
            )

        unlocked = [_achievement(key) for key in fresh]
        reward = sum(item.reward for item in unlocked)
        if reward:
            await self.add(user_id, reward, description="Achievement rewards")
        return unlocked

    @staticmethod
    def catalogue() -> list[Achievement]:
        """Every achievement in the catalogue, unlocked or not."""
        return [_achievement(key) for key in ACHIEVEMENTS]

    # ------------------------------------------------------------------
    # Admin surface
    # ------------------------------------------------------------------
    async def set_balances(
        self,
        user_id: int,
        *,
        wallet: int | None = None,
        bank: int | None = None,
        streak: int | None = None,
        description: str = "Admin adjustment",
    ) -> Account:
        """Force balances to absolute values. Used by the owner menu only."""
        user_id = int(user_id)
        async with self._lock:
            await self._ensure(user_id)
            before = self._build(await self._account_row(user_id), user_id)
            await self.bot.db.execute(
                "UPDATE economy_accounts SET wallet = COALESCE(%s, wallet), "
                "bank = COALESCE(%s, bank), "
                "daily_streak = COALESCE(%s, daily_streak) WHERE user_id = %s",
                None if wallet is None else max(0, int(wallet)),
                None if bank is None else max(0, int(bank)),
                None if streak is None else max(0, int(streak)),
                user_id,
            )
            row = await self._account_row(user_id)

        account = self._build(row, user_id)
        delta = account.total - before.total
        if delta:
            if delta > 0:
                await self.bot.db.execute(
                    "UPDATE economy_accounts SET lifetime_earned = lifetime_earned + %s "
                    "WHERE user_id = %s",
                    delta,
                    user_id,
                )
            else:
                await self.bot.db.execute(
                    "UPDATE economy_accounts SET lifetime_spent = lifetime_spent + %s "
                    "WHERE user_id = %s",
                    abs(delta),
                    user_id,
                )
        await self._record(user_id, delta, description, account.total)
        return account
