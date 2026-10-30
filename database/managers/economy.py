from .base import BaseManager

from sqlalchemy.future import select
from sqlalchemy import text, distinct, case, delete, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import func
from typing import List, Optional, Tuple
import secrets
from ..models import (
    Transaction,
    CommandCooldown,
    CryptoAsset,
    CryptoPrice,
    ItemCooldown,
    Supply,
    EconomicMetricsHistory,
    UserEconomicPreferences,
    Wallet,
    Loan,
    LoanPayment,
    Job,
    CharityFund,
)
from datetime import datetime, timedelta, timezone
import discord
import uuid
import logging
from decimal import Decimal, ROUND_HALF_UP
from utils.amount import AmountUtils

logger = logging.getLogger("discord.client")

ADMIN_IDS = {284439598422163476, 538773310704582666, 657182369240973312, 217389696043450368}  # Owner IDs

# Sentinel hash representing the treasury/system account. The raw value 0 is
# used historically; after hashing user IDs we store its hash instead so the
# column stays uniformly VARCHAR(64).
TREASURY_USER_ID = 0


def _treasury_hash() -> str:
    """The treasury's user key for the current economy scope.

    Scope-aware so a server's own economy refers to its own treasury rows (and
    the "exclude the treasury" filters still line up there).
    """
    from utils.core.economy_scope import current_scope
    from utils.security import scoped_user_hash

    return scoped_user_hash(TREASURY_USER_ID, current_scope())


# Sentinel hash for the charity pot, mirroring the treasury's: donations and
# claims land in the ledger without belonging to any real member.
CHARITY_USER_ID = -1


def _charity_hash() -> str:
    """The charity pot's user key for the current economy scope."""
    from utils.core.economy_scope import current_scope
    from utils.security import scoped_user_hash

    return scoped_user_hash(CHARITY_USER_ID, current_scope())


# These memos are keyed by economy scope so each server's own economy gets its
# own cooldown and daily mint/burn budgets. One server minting can therefore
# never exhaust the shared economy's budget, or hold its rebalance cooldown.
_LAST_REBALANCE_AT: dict = {}  # scope -> last rebalance at
_DAILY_MINT_TOTAL: dict = {}  # scope -> minted today
_MINT_DAY: dict = {}  # scope -> day the mint total belongs to
_DAILY_BURN_TOTAL: dict = {}  # scope -> burned today
_BURN_DAY: dict = {}

# Volume-linked minting tracks its own daily budget so it doesn't starve the
# scheduled auto-rebalance mint (and vice versa).
_DAILY_VOLUME_MINT_TOTAL: dict = {}
_VOLUME_MINT_DAY: dict = {}


def _memo_scope() -> int:
    """Economy scope the module-level bookkeeping memos belong to."""
    from utils.core.economy_scope import current_scope

    return current_scope()


def _daily_memo(totals: dict, days: dict, today) -> Decimal:
    """Today's running total for the current scope, reset when the day rolls."""
    scope = _memo_scope()
    if days.get(scope) != today:
        days[scope] = today
        totals[scope] = Decimal("0")
    return totals.setdefault(scope, Decimal("0"))

# Treasury floor: treasury must never drop below this fraction of total_supply
TREASURY_FLOOR_RATIO = Decimal("0.10")

# Wealth tier thresholds (percentage of total supply)
WEALTH_TIERS = {
    "tier_1": {"threshold": Decimal("0.005"), "label": "mild"},  # 0.5%
    "tier_2": {"threshold": Decimal("0.01"), "label": "moderate"},  # 1%
    "tier_3": {"threshold": Decimal("0.02"), "label": "severe"},  # 2%
    "tier_4": {"threshold": Decimal("0.05"), "label": "extreme"},  # 5%
}

# Penalty multipliers per tier (applied to different transaction types)
TIER_PENALTIES = {
    0: {
        "bet": Decimal("1.0"),
        "loan": Decimal("1.0"),
        "fee": Decimal("1.0"),
        "transfer": Decimal("1.0"),
    },
    1: {
        "bet": Decimal("0.8"),
        "loan": Decimal("0.7"),
        "fee": Decimal("1.5"),
        "transfer": Decimal("0.8"),
    },
    2: {
        "bet": Decimal("0.5"),
        "loan": Decimal("0.4"),
        "fee": Decimal("2.0"),
        "transfer": Decimal("0.5"),
    },
    3: {
        "bet": Decimal("0.25"),
        "loan": Decimal("0.2"),
        "fee": Decimal("3.0"),
        "transfer": Decimal("0.3"),
    },
    4: {
        "bet": Decimal("0.1"),
        "loan": Decimal("0.05"),
        "fee": Decimal("5.0"),
        "transfer": Decimal("0.1"),
    },
}

# Transaction types that should not incur dynamic/wealth fees. These represent
# legitimate treasury-to-wallet credits (game wins, refunds, rewards, rakeback)
# where the displayed/gross amount must equal the amount received.
_FEE_EXEMPT_TYPES = {"game_payout", "rakeback", "reward"}


class EconomyMixin(BaseManager):
    # ---- economy scope -------------------------------------------------
    @staticmethod
    def _supply_pk() -> int:
        """Primary key of the supply row belonging to the current scope.

        The shared economy keeps the original row (id 1); a server with its own
        economy gets its own row, so its treasury and supply stay independent.
        """
        from utils.core.economy_scope import current_scope

        return current_scope() or 1

    @staticmethod
    def _scope_filter(column):
        """Condition matching only the current scope's rows in ``column``.

        Per-user lookups already resolve to a scope-qualified key, so this is
        only needed by the aggregates that sweep every user: leaderboards,
        supply totals and the economy snapshot. Scope 0 selects the unmarked
        keys; any other scope selects rows carrying its ``:g`` marker.
        """
        from sqlalchemy import or_
        from utils.core.economy_scope import current_scope

        scope = current_scope()
        if scope:
            return column.like(f"%:g{scope}")
        # NULL is SQL's "neither", and some ledgers leave a party column empty
        # (treasury burns, for instance). Those rows belong to the shared
        # economy, so a bare NOT LIKE - which is NULL for them - would drop
        # them from the shared totals.
        return or_(column.is_(None), ~column.like("%:g%"))

    @staticmethod
    def _fresh_supply(pk: int) -> "Supply":
        """A brand-new supply row, seeded the first time a scope is touched.

        A server's own economy starts with empty user balances but the same
        starting treasury as the shared one, so treasury-funded payouts work.
        """
        amount = Decimal("1000000000000.00")
        return Supply(
            id=pk,
            total_supply=amount,
            circulating=Decimal("0.00"),
            treasury=amount,
        )

    async def _ensure_supply_row(self, session) -> "Supply":
        """This scope's supply row, created on first use.

        Takes an open session so money flows can seed their own scope's supply
        without a second connection (which would deadlock on SQLite).
        """
        pk = self._supply_pk()
        supply = await session.get(Supply, pk)
        if supply is None:
            supply = self._fresh_supply(pk)
            session.add(supply)
            await session.flush()
        return supply

    async def _credit_treasury(self, session, amount: Decimal) -> None:
        """Add ``amount`` to this scope's treasury (fees, fines, deposits).

        The supply row is the scoped one, so a fee earned in a server's own
        economy can never inflate the shared treasury.
        """
        supply = await self._ensure_supply_row(session)
        await self._atomic_balance_change(
            session, "supply", "id", supply.id, amount, balance_col="treasury"
        )

    @staticmethod
    def _scope_filter_either(first, second):
        """Scope condition for rows that carry a party in either column.

        Transactions name two users, and in a scoped economy both are scoped,
        so a row belongs to the scope when either side is marked with it.
        """
        from sqlalchemy import and_, or_
        from utils.core.economy_scope import current_scope

        scope = current_scope()
        if scope:
            return or_(first.like(f"%:g{scope}"), second.like(f"%:g{scope}"))

        def _unscoped(column):
            return or_(column.is_(None), ~column.like("%:g%"))

        # A row belongs to the shared economy when neither party is marked - and
        # an empty party counts as "not marked" rather than as unknown.
        return and_(_unscoped(first), _unscoped(second))

    def _get_latest_metrics(self) -> dict:
        """Cached economy snapshot for the current scope (empty if none yet)."""
        from utils.core.economy_scope import current_scope

        cache = getattr(self, "_latest_metrics", None)
        if not isinstance(cache, dict):
            return {}
        return cache.get(current_scope()) or {}

    def _set_latest_metrics(self, metrics: dict) -> None:
        """Cache the current scope's economy snapshot, keyed by scope."""
        from utils.core.economy_scope import current_scope

        cache = getattr(self, "_latest_metrics", None)
        if not isinstance(cache, dict):
            cache = {}
            self._latest_metrics = cache
        cache[current_scope()] = metrics

    async def initialize_supply_record(self):
        """
        Creates a default supply record if it doesn't exist.
        This is useful for a fresh economy where no supply record is present.
        """
        async with self.async_sessionmaker() as session:
            async with session.begin():
                supply = await session.get(Supply, self._supply_pk())
                if not supply:
                    session.add(self._fresh_supply(self._supply_pk()))
                    logging.info("Created default supply record.")

    async def _atomic_balance_change(
        self,
        session,
        table,
        pk_field,
        pk_value,
        delta: Decimal,
        *,
        balance_col: str = "balance",
        frozen_field: str | None = None,
    ):
        """
        Atomically add `delta` (can be negative) to `balance` column of `table`
        and guarantee non-negative result. Raises on race or freeze.
        """
        # Build this through SQLAlchemy Core rather than raw SQL so the column
        # types coerce the bound values: SQLite's driver cannot bind Decimal or
        # UUID objects directly the way asyncpg could.
        from sqlalchemy import update as _sa_update
        from ..models import Base

        table_obj = Base.metadata.tables.get(table)
        if table_obj is None:
            raise ValueError(f"Unknown table {table!r}")
        balance_attr = table_obj.c[balance_col]

        # SQLite keeps these money columns as REAL, so a balance that reads back
        # as exactly ``X.XX`` can sit a hair below that in storage (float drift).
        # Betting/depositing "all" rounds up to the cent, so the strict
        # ``>= 0`` guard would reject a spend of someone's entire balance with a
        # confusing "you have X but this requires X" error. Tolerate that
        # sub-cent gap and write the result back rounded, which also scrubs the
        # accumulated drift so it can't build up over time.
        new_balance = balance_attr + delta
        money_tolerance = Decimal("0.01")

        conditions = [
            table_obj.c[pk_field] == pk_value,
            new_balance >= -money_tolerance,
        ]
        if frozen_field:
            # ``IS NOT TRUE`` keeps NULL (unset) rows eligible, matching the old
            # raw SQL on both Postgres and SQLite.
            conditions.append(table_obj.c[frozen_field].isnot(True))

        normalized = case((new_balance < 0, Decimal("0.00")), else_=new_balance)
        stmt = (
            _sa_update(table_obj)
            .where(*conditions)
            .values({balance_attr: func.round(normalized, 2)})
            .returning(balance_attr)
        )

        result = await session.execute(stmt)
        # SQLite does not populate ``rowcount`` reliably for UPDATE ... RETURNING,
        # so treat a missing returned row as the failure signal.
        updated = result.scalar_one_or_none()
        if updated is None:
            raise ValueError(
                f"Race or insufficient funds on {table}.{pk_field}={pk_value}"
            )
        return updated

    async def wipe_economy(
        self, caller_id: int, *, confirm: bool = False, dry_run: bool = False
    ) -> dict:
        """
        Comprehensive economy wipe - resets ALL economy-related tables.

        This completely wipes the economy and starts fresh:
        - All wallets, transactions, and supply reset
        - All items, shops, and trade logs cleared
        - All loans, bounties, and payments cleared
        - All game history and sessions cleared
        - All VIP/rakeback data cleared
        - All social currency (reputation, sobs, etc.) cleared
        - All crypto assets and prices cleared
        - All transfer tracking and suspicious activity cleared

        Preserved (NOT wiped):
        - Bot configuration and server settings
        - Moderation data (punishments, jails, watchdog)
        - Command statistics and cooldowns
        - User preferences (timezones, locations)
        - Music/LastFM data
        - Role management data

        Args:
            caller_id: Discord ID of the caller (must be in ADMIN_IDS)
            confirm: Safety flag - must be True to execute
            dry_run: If True, returns what would be wiped without actually wiping

        Returns:
            dict with 'wiped_tables' list and 'dry_run' boolean
        """
        if caller_id not in ADMIN_IDS:
            raise PermissionError("You do not have permission to wipe the economy.")
        caller_id = self.econ_hash_user_id(caller_id)

        if not confirm:
            raise ValueError("`confirm=True` is required as a safety flag.")

        # All economy-related tables to wipe, organized by category
        economy_tables = [
            # Core economy
            "wallets",
            "transactions",
            "supply",
            # Items and trading (``items`` is the per-user inventory; the
            # ``shop_items`` catalogue is left intact so the shop survives)
            "items",
            "item_cooldowns",
            "active_effects",
            "trade_logs",
            "shop_purchase_logs",
            # Loans and bounties
            "bounties",
            "loans",
            "loan_payments",
            # Jobs system
            "jobs",
            # Games and gambling
            "game_history",
            "game_sessions",
            "game_session_events",
            # Crypto
            "crypto_assets",
            "crypto_prices",
            # User economy data
            "user_economic_preferences",
            # Social currencies
            "reputation",
            "sobs",
            "skulls",
            "flames",
            "hearts",
            "clowns",
            "tomatoes",
            # VIP system
            "user_vip",
            "rakeback_balances",
            "rakeback_transactions",
            # Economic metrics
            "economic_metrics_history",
        ]

        if dry_run:
            return {
                "dry_run": True,
                "wiped_tables": economy_tables,
                "message": f"Would wipe {len(economy_tables)} economy tables",
            }

        async with self.async_sessionmaker() as session:
            async with session.begin():
                # SQLite has no TRUNCATE; delete each table's rows instead.
                # Foreign-key checks are deferred so wipe order cannot trip them.
                await session.execute(text("PRAGMA defer_foreign_keys=ON"))
                for table_name in economy_tables:
                    await session.execute(text(f"DELETE FROM {table_name}"))

            # Re-initialize supply record
            await self.initialize_supply_record()

        return {
            "dry_run": False,
            "wiped_tables": economy_tables,
            "message": f"Successfully wiped {len(economy_tables)} economy tables",
        }

    async def reset_all_economy_cooldowns(self) -> dict[str, int]:
        """Clear every member's economy cooldowns, for all users at once.

        Backs ``!bank resetcooldowns``. Three separate stores gate economy
        actions, so all three are cleared together:

        - ``command_cooldowns``: the shared per-user cooldown table behind
          daily / weekly / monthly, job work and every other command.
        - ``item_cooldowns``: per-user per-item cooldowns (e.g. a used shield).
        - ``jobs.last_worked``: the timestamp ``work_job`` uses to enforce its
          own 24h job-work cooldown independently of ``command_cooldowns``.

        Returns a ``source -> rows affected`` mapping for reporting. Balance,
        progress and inventory rows are untouched; only the timers go.
        """
        async with self.async_sessionmaker() as session:
            async with session.begin():
                # SQLite has no TRUNCATE; delete each table's rows instead.
                commands = await session.execute(delete(CommandCooldown))
                items = await session.execute(delete(ItemCooldown))
                jobs = await session.execute(
                    update(Job)
                    .where(Job.last_worked.is_not(None))
                    .values(last_worked=None)
                )
        self._cache_invalidate("cooldown")
        return {
            "command_cooldowns": commands.rowcount or 0,
            "item_cooldowns": items.rowcount or 0,
            "job_work_cooldowns": jobs.rowcount or 0,
        }

    async def get_supply_record(self) -> Supply:
        """
        Retrieve the supply record from the database or create one if it does not exist.
        """
        async with self.async_sessionmaker() as session:
            supply = await session.get(Supply, self._supply_pk())
            if not supply:
                await self.initialize_supply_record()
                supply = await session.get(Supply, self._supply_pk())
            return supply

    async def get_treasury_balance(self) -> Decimal:
        """
        Retrieve the current balance of the treasury.
        """
        supply = await self.get_supply_record()
        return supply.treasury

    # ---- charity -------------------------------------------------------
    @staticmethod
    def _charity_pk() -> int:
        """Primary key of the charity row belonging to the current scope.

        Mirrors :meth:`_supply_pk`: the shared economy keeps id 1, while a
        server with its own economy gets a row of its own so its pot is private.
        """
        from utils.core.economy_scope import current_scope

        return current_scope() or 1

    @staticmethod
    def _fresh_charity_fund(pk: int) -> "CharityFund":
        """A brand-new charity row with the built-in claim rules."""
        return CharityFund(
            id=pk,
            balance=Decimal("0.00"),
            enabled=True,
            claim_threshold=Decimal("10000.00"),
            min_claim=Decimal("100.00"),
            max_claim=Decimal("1000.00"),
            cooldown_seconds=3600,
            total_donated=Decimal("0.00"),
            total_claimed=Decimal("0.00"),
        )

    async def _ensure_charity_fund(self, session) -> "CharityFund":
        """This scope's charity row, created with defaults on first use."""
        pk = self._charity_pk()
        fund = await session.get(CharityFund, pk)
        if fund is None:
            fund = self._fresh_charity_fund(pk)
            session.add(fund)
            await session.flush()
        return fund

    async def get_charity_fund(self) -> dict:
        """This scope's charity pot and its rules, as plain values.

        Values are read while the session is open so callers can use the result
        after the connection is returned to the pool.
        """
        async with self.async_sessionmaker() as session:
            async with session.begin():
                fund = await self._ensure_charity_fund(session)
                return {
                    "balance": Decimal(str(fund.balance or 0)),
                    "enabled": bool(fund.enabled),
                    "claim_threshold": Decimal(str(fund.claim_threshold or 0)),
                    "min_claim": Decimal(str(fund.min_claim or 0)),
                    "max_claim": Decimal(str(fund.max_claim or 0)),
                    "cooldown_seconds": int(fund.cooldown_seconds or 0),
                    "total_donated": Decimal(str(fund.total_donated or 0)),
                    "total_claimed": Decimal(str(fund.total_claimed or 0)),
                }

    async def get_charity_balance(self) -> Decimal:
        """How much the current scope's charity pot is holding."""
        fund = await self.get_charity_fund()
        return fund["balance"]

    async def set_charity_config(self, **fields) -> dict:
        """Update the charity rules and return the fresh fund.

        Only the known rule fields are accepted; unknown or ``None`` values are
        ignored so callers can pass whichever subset they are changing.
        """
        allowed = {
            "enabled",
            "claim_threshold",
            "min_claim",
            "max_claim",
            "cooldown_seconds",
        }
        async with self.async_sessionmaker() as session:
            async with session.begin():
                fund = await self._ensure_charity_fund(session)
                for key, value in fields.items():
                    if key not in allowed or value is None:
                        continue
                    setattr(fund, key, value)
        return await self.get_charity_fund()

    async def charity_donate(
        self, wallet_id: str, amount: Decimal, description: str
    ) -> str:
        """Move ``amount`` from a wallet into the charity pot, fee-free.

        The move happens beside the treasury rather than through it, so a gift
        neither earns nor pays a fee. ``update_supply`` keeps the stored supply
        totals in step with the wallets afterwards.
        """
        amount = AmountUtils.round_currency(amount)
        if amount <= 0:
            raise ValueError("Amount must be greater than zero.")

        async with self.async_sessionmaker() as session:
            async with session.begin():
                wallet = await session.get(Wallet, wallet_id)
                if not wallet:
                    raise ValueError("Wallet not found.")
                if wallet.wallet_frozen:
                    raise ValueError("Wallet is frozen.")

                fund = await self._ensure_charity_fund(session)
                await self._atomic_balance_change(
                    session,
                    "wallets",
                    "wallet_id",
                    wallet_id,
                    -amount,
                    frozen_field="wallet_frozen",
                )
                await self._atomic_balance_change(
                    session, "charity_funds", "id", fund.id, +amount
                )
                fund.total_donated = AmountUtils.round_currency(
                    Decimal(str(fund.total_donated or 0)) + amount
                )

                txid = str(uuid.uuid4())
                session.add(
                    Transaction(
                        id=txid,
                        from_user_id=wallet.user_id,
                        to_user_id=_charity_hash(),
                        amount=amount,
                        description=description,
                        timestamp=discord.utils.utcnow(),
                    )
                )

            await self.update_supply()

        return txid

    async def charity_claim(
        self, wallet_id: str, amount: Decimal, description: str
    ) -> str:
        """Move ``amount`` from the charity pot into a wallet.

        The pot's balance is guarded by the same atomic decrement used for
        wallets, so concurrent claims can never overdraw it.
        """
        amount = AmountUtils.round_currency(amount)
        if amount <= 0:
            raise ValueError("Amount must be greater than zero.")

        async with self.async_sessionmaker() as session:
            async with session.begin():
                wallet = await session.get(Wallet, wallet_id)
                if not wallet:
                    raise ValueError("Wallet not found.")
                if wallet.wallet_frozen:
                    raise ValueError("Wallet is frozen.")

                fund = await self._ensure_charity_fund(session)
                await self._atomic_balance_change(
                    session, "charity_funds", "id", fund.id, -amount
                )
                await self._atomic_balance_change(
                    session,
                    "wallets",
                    "wallet_id",
                    wallet_id,
                    +amount,
                    frozen_field="wallet_frozen",
                )
                fund.total_claimed = AmountUtils.round_currency(
                    Decimal(str(fund.total_claimed or 0)) + amount
                )

                txid = str(uuid.uuid4())
                session.add(
                    Transaction(
                        id=txid,
                        from_user_id=_charity_hash(),
                        to_user_id=wallet.user_id,
                        amount=amount,
                        description=description,
                        timestamp=discord.utils.utcnow(),
                    )
                )

            await self.update_supply()

        return txid

    async def create_wallet(self, user_id: int) -> None:
        raw_user_id = user_id
        await self.ensure_user_identity(raw_user_id)
        user_id = self.econ_hash_user_id(raw_user_id)
        from utils.core.economy_scope import current_scope

        if current_scope():
            # A scope-qualified key can't be derived back from the raw id on its
            # own, so record the mapping too or the balance/history views can't
            # name the user.
            await self.ensure_user_identity_hash(user_id, raw_user_id)
        async with self.async_sessionmaker() as session:
            async with session.begin():
                # Seed this scope's supply row too, so a fresh server economy
                # can pay out from its own treasury.
                await self._ensure_supply_row(session)
                result = await session.execute(
                    select(Wallet).where(Wallet.user_id == user_id)
                )
                existing = result.scalar_one_or_none()
                if existing:
                    return

                new_wallet = Wallet(
                    user_id=user_id,
                    balance=Decimal("0.00"),
                    bank_balance=Decimal("0.00"),
                    client_seed=secrets.token_hex(16),
                    nonce=0,
                )
                session.add(new_wallet)
                await session.flush()

                logging.info(f"Created wallet for user {user_id}.")

    async def get_wallet_by_user_id(self, user_id: int) -> Wallet:
        """
        Return the Wallet row for the given user_id, creating one if needed.
        """
        raw_user_id = user_id
        user_id = self.econ_hash_user_id(user_id)
        async with self.async_sessionmaker() as session:
            result = await session.execute(
                select(Wallet).where(Wallet.user_id == user_id)
            )
            wallet = result.scalar_one_or_none()

            if not wallet:
                await self.create_wallet(raw_user_id)

                result = await session.execute(
                    select(Wallet).where(Wallet.user_id == user_id)
                )
                wallet = result.scalar_one_or_none()

            return wallet

    async def get_wallet_id_for_user(self, user_id: int) -> uuid.UUID:
        """
        Return just the wallet_id (the UUID primary key) for the given user_id,
        creating a wallet if needed.
        """
        raw_user_id = user_id
        user_id = self.econ_hash_user_id(raw_user_id)
        wallet = await self.get_wallet_by_user_id(raw_user_id)
        return f"{wallet.wallet_id}"

    async def freeze_wallet(self, wallet_id: str):
        """Freeze a wallet to block outgoing transactions."""
        async with self.async_sessionmaker() as session:
            async with session.begin():
                wallet = await session.get(Wallet, wallet_id)
                if not wallet:
                    raise ValueError(f"Wallet {wallet_id} not found.")
                wallet.wallet_frozen = True

    async def unfreeze_wallet(self, wallet_id: str):
        """Unfreeze a wallet, allowing transactions again."""
        async with self.async_sessionmaker() as session:
            async with session.begin():
                wallet = await session.get(Wallet, wallet_id)
                if not wallet:
                    raise ValueError(f"Wallet {wallet_id} not found.")
                wallet.wallet_frozen = False

    async def get_wallet_balance(self, wallet_id: str) -> Decimal:
        async with self.async_sessionmaker() as session:
            wallet = await session.get(Wallet, wallet_id)
            return wallet.balance if wallet else Decimal("0.00")

    async def get_wallet_balance_by_user_id(self, user_id: int) -> Decimal:
        """
        Return the wallet balance for a given user, creating wallet if it doesn't exist.
        """
        raw_user_id = user_id
        user_id = self.econ_hash_user_id(raw_user_id)
        wallet = await self.get_wallet_by_user_id(raw_user_id)
        return wallet.balance

    async def get_bank_balance(self, wallet_id: str) -> Decimal:
        async with self.async_sessionmaker() as session:
            wallet = await session.get(Wallet, wallet_id)
            return wallet.bank_balance if wallet else Decimal("0.00")

    async def admin_set_balances(
        self,
        user_id: int,
        *,
        wallet: Decimal | int | float | None = None,
        bank: Decimal | int | float | None = None,
    ) -> None:
        """Owner-only: force a wallet's and/or bank's balance to absolute values.

        Unlike the normal transaction helpers this ignores the treasury (an
        owner edit is not an economy event), which is exactly what
        ``!admin panel`` needs to set a figure outright.
        """
        wallet_id = await self.get_wallet_id_for_user(user_id)
        async with self.async_sessionmaker() as session:
            async with session.begin():
                record = await session.get(Wallet, wallet_id)
                if record is None:
                    raise ValueError("Wallet not found.")
                if wallet is not None:
                    record.balance = AmountUtils.round_currency(
                        max(Decimal("0.00"), Decimal(str(wallet)))
                    )
                if bank is not None:
                    record.bank_balance = AmountUtils.round_currency(
                        max(Decimal("0.00"), Decimal(str(bank)))
                    )
            await session.commit()
        await self.update_supply()

    async def spend_from_wallet(
        self,
        wallet_id: str,
        amount: Decimal,
        description: str,
        *,
        transaction_type: str = "standard",
    ) -> tuple[str, Decimal]:
        """
        Atomically spend an exact amount from a wallet into the treasury.

        Re-reads the wallet balance when the atomic deduction fails so the caller
        can present a clean "insufficient funds" message based on current funds,
        not a stale value. Returns (txid, amount_spent).
        """
        amount = AmountUtils.round_currency(amount)
        if amount <= 0:
            raise ValueError("Amount must be greater than zero.")

        try:
            txid = await self.process_treasury_transaction(
                wallet_id=wallet_id,
                amount=-amount,
                description=description,
                transaction_type=transaction_type,
            )
        except ValueError as e:
            if "Race or insufficient funds" in str(e):
                current_balance = await self.get_wallet_balance(wallet_id)
                raise ValueError(
                    f"Insufficient funds. You have {current_balance}, "
                    f"but this action requires {amount}."
                ) from e
            raise

        return txid, amount

    async def fine_wallet(
        self,
        wallet_id: str,
        percentage: Decimal,
        description: str,
    ) -> Decimal:
        """
        Atomically fine a wallet by a percentage of its current balance.

        Re-reads the balance inside the transaction so the fine reflects the
        actual available funds and avoids stale-balance race failures.
        Returns the actual amount fined.
        """
        if percentage <= 0 or percentage > 1:
            raise ValueError("Percentage must be between 0 and 1.")

        async with self.async_sessionmaker() as session:
            async with session.begin():
                wallet = await session.get(Wallet, wallet_id)
                if not wallet:
                    raise ValueError("Wallet not found.")
                if wallet.wallet_frozen:
                    raise ValueError("Wallet is frozen.")

                fresh_balance = AmountUtils.round_currency(Decimal(str(wallet.balance)))
                fine_amount = AmountUtils.round_currency(fresh_balance * percentage)
                if fine_amount <= 0:
                    raise ValueError("Fine amount is too small.")

                await self._atomic_balance_change(
                    session,
                    "wallets",
                    "wallet_id",
                    wallet_id,
                    -fine_amount,
                    frozen_field="wallet_frozen",
                )
                await self._credit_treasury(session, +fine_amount)

                txid = str(uuid.uuid4())
                session.add(
                    Transaction(
                        id=txid,
                        from_user_id=wallet.user_id,
                        to_user_id=_treasury_hash(),
                        amount=fine_amount,
                        description=description,
                        timestamp=discord.utils.utcnow(),
                    )
                )

            await self.update_supply()

        return fine_amount

    async def deposit_to_bank(self, wallet_id: str, amount: Decimal, description: str):
        """Transfer funds from wallet to bank without affecting treasury."""
        async with self.async_sessionmaker() as session:
            async with session.begin():
                amount = AmountUtils.round_currency(amount)

                # 1) load + gate
                wallet = await session.get(Wallet, wallet_id)
                if not wallet:
                    raise ValueError("Wallet not found.")
                if wallet.wallet_frozen:
                    raise ValueError("Wallet is frozen.")

                # 2) ensure bank row exists
                bank = await session.get(Wallet, wallet_id)
                if not bank:
                    bank = Wallet(wallet_id=wallet_id, bank_balance=Decimal("0.00"))
                    session.add(bank)

                # 3) atomic balance moves
                await self._atomic_balance_change(
                    session,
                    "wallets",
                    "wallet_id",
                    wallet_id,
                    -amount,
                    frozen_field="wallet_frozen",
                )
                await self._atomic_balance_change(
                    session,
                    "wallets",
                    "wallet_id",
                    wallet_id,
                    +amount,
                    balance_col="bank_balance",
                )

                # 4) record TX
                txid = str(uuid.uuid4())
                session.add(
                    Transaction(
                        id=txid,
                        from_user_id=wallet.user_id,
                        to_user_id=wallet.user_id,
                        amount=amount,
                        description=description,
                        timestamp=discord.utils.utcnow(),
                    )
                )

            return txid

    async def withdraw_from_bank(
        self, wallet_id: str, amount: Decimal, description: str
    ):
        """Transfer funds from bank to wallet without affecting treasury."""
        async with self.async_sessionmaker() as session:
            async with session.begin():
                amount = AmountUtils.round_currency(amount)

                wallet = await session.get(Wallet, wallet_id)
                if not wallet:
                    raise ValueError("Wallet not found.")
                if wallet.wallet_frozen:
                    raise ValueError("Wallet is frozen.")

                bank = await session.get(Wallet, wallet_id)
                if not bank:
                    raise ValueError("Bank account missing.")

                await self._atomic_balance_change(
                    session,
                    "wallets",
                    "wallet_id",
                    wallet_id,
                    -amount,
                    balance_col="bank_balance",
                )
                await self._atomic_balance_change(
                    session,
                    "wallets",
                    "wallet_id",
                    wallet_id,
                    +amount,
                    frozen_field="wallet_frozen",
                )

                txid = str(uuid.uuid4())
                session.add(
                    Transaction(
                        id=txid,
                        from_user_id=wallet.user_id,
                        to_user_id=wallet.user_id,
                        amount=amount,
                        description=description,
                        timestamp=discord.utils.utcnow(),
                    )
                )

            return txid

    async def process_p2p_transaction(
        self,
        sender_wallet_id: str,
        receiver_wallet_id: str,
        amount: Decimal,
        description: str,
        guild_id: int = None,
        fee_from_amount: bool = False,
    ):
        # Check economic circuit breaker before processing
        circuit_breaker = await self.check_economic_circuit_breaker()
        if circuit_breaker["triggered"]:
            reasons = ", ".join(circuit_breaker["reasons"])
            raise ValueError(f"Economic circuit breaker triggered: {reasons}")

        # Calculate wealth-adjusted fee for P2P transfer
        # Base fee rate from dynamic economic factors
        base_fee_rate = await self.get_enhanced_fee_rate("standard")
        base_fee = AmountUtils.round_currency(amount * base_fee_rate)

        async with self.async_sessionmaker() as session:
            async with session.begin():
                sender = await session.get(Wallet, sender_wallet_id)
                receiver = await session.get(Wallet, receiver_wallet_id)
                if not sender or not receiver:
                    raise ValueError("Invalid sender or receiver wallet.")
                if sender.wallet_frozen:
                    raise ValueError("Sender's wallet is frozen.")
                if receiver.wallet_frozen:
                    raise ValueError("Receiver's wallet is frozen.")

                raw_sender_id = await self.resolve_user_hash(sender.user_id)

                # Apply wealth-adjusted fee based on sender's tier
                adjusted_fee = await self.calculate_wealth_adjusted_fee(
                    raw_sender_id, base_fee
                )
                net_amt = AmountUtils.round_currency(amount)

                if fee_from_amount:
                    # Fee deducted from transfer amount (sender pays exact amount, receiver gets less)
                    receiver_gets = net_amt - adjusted_fee
                    if receiver_gets <= 0:
                        raise ValueError("Transfer amount too small to cover fees.")
                    total_deduction = net_amt
                else:
                    # Fee added on top (sender pays amount + fee, receiver gets full amount)
                    receiver_gets = net_amt
                    total_deduction = net_amt + adjusted_fee

                # 1) atomic moves
                await self._atomic_balance_change(
                    session,
                    "wallets",
                    "wallet_id",
                    sender_wallet_id,
                    -total_deduction,
                    frozen_field="wallet_frozen",
                )
                await self._atomic_balance_change(
                    session,
                    "wallets",
                    "wallet_id",
                    receiver_wallet_id,
                    +receiver_gets,
                )
                # Fee goes to treasury
                await self._credit_treasury(session, +adjusted_fee)

                # 2) record DB txs - main transfer and fee
                txid_main = str(uuid.uuid4())
                txid_fee = str(uuid.uuid4())
                session.add_all(
                    [
                        Transaction(
                            id=txid_main,
                            from_user_id=sender.user_id,
                            to_user_id=receiver.user_id,
                            amount=receiver_gets,
                            description=description,
                            timestamp=discord.utils.utcnow(),
                        ),
                        Transaction(
                            id=txid_fee,
                            from_user_id=sender.user_id,
                            to_user_id=_treasury_hash(),  # Treasury
                            amount=adjusted_fee,
                            description=f"P2P transfer fee (base: {base_fee_rate:.2%}, wealth-adjusted)",
                            timestamp=discord.utils.utcnow(),
                        ),
                    ]
                )

            await self.update_supply()

        return txid_main

    async def drain_wallet(
        self,
        target_wallet_id: str,
        robber_wallet_id: str,
        target_user_id: int,
        robber_user_id: int,
        description: str,
        robbery_debuff: Decimal,
        fee_from_amount: bool = True,
    ) -> tuple[str, Decimal]:
        """
        Atomically drain the target's wallet into the robber's wallet.

        The target's balance is re-read inside the transaction so the drain
        succeeds even if the balance changed between the command's initial read
        and the actual transfer. Returns (txid, actual_drained_amount).
        """

        # Check economic circuit breaker before processing
        circuit_breaker = await self.check_economic_circuit_breaker()
        if circuit_breaker["triggered"]:
            reasons = ", ".join(circuit_breaker["reasons"])
            raise ValueError(f"Economic circuit breaker triggered: {reasons}")

        base_fee_rate = await self.get_enhanced_fee_rate("standard")
        adjusted_fee: Decimal | None = None

        async with self.async_sessionmaker() as session:
            async with session.begin():
                target = await session.get(Wallet, target_wallet_id)
                robber = await session.get(Wallet, robber_wallet_id)
                if not target or not robber:
                    raise ValueError("Invalid target or robber wallet.")
                if target.wallet_frozen:
                    raise ValueError("Target's wallet is frozen.")
                if robber.wallet_frozen:
                    raise ValueError("Robber's wallet is frozen.")

                # Re-read balance inside the transaction to avoid race conditions.
                fresh_target_balance = AmountUtils.round_currency(
                    Decimal(str(target.balance))
                )
                if fresh_target_balance <= 0:
                    raise ValueError("Target has no funds to drain.")

                # Calculate fee based on the actual current balance.
                raw_target_id = await self.resolve_user_hash(target.user_id)
                drain_amount = AmountUtils.round_currency(
                    fresh_target_balance * robbery_debuff
                )
                base_fee = AmountUtils.round_currency(drain_amount * base_fee_rate)
                adjusted_fee = await self.calculate_wealth_adjusted_fee(
                    raw_target_id, base_fee
                )

                if fee_from_amount:
                    receiver_gets = drain_amount - adjusted_fee
                    if receiver_gets <= 0:
                        raise ValueError("Drain amount too small to cover fees.")
                    total_deduction = drain_amount
                else:
                    receiver_gets = drain_amount
                    total_deduction = drain_amount + adjusted_fee

                if fresh_target_balance < total_deduction:
                    raise ValueError("Target balance is insufficient to cover drain.")

                # Atomic balance changes
                await self._atomic_balance_change(
                    session,
                    "wallets",
                    "wallet_id",
                    target_wallet_id,
                    -total_deduction,
                    frozen_field="wallet_frozen",
                )
                await self._atomic_balance_change(
                    session,
                    "wallets",
                    "wallet_id",
                    robber_wallet_id,
                    +receiver_gets,
                )
                await self._credit_treasury(session, +adjusted_fee)

                txid_main = str(uuid.uuid4())
                txid_fee = str(uuid.uuid4())
                session.add_all(
                    [
                        Transaction(
                            id=txid_main,
                            from_user_id=target.user_id,
                            to_user_id=robber.user_id,
                            amount=receiver_gets,
                            description=description,
                            timestamp=discord.utils.utcnow(),
                        ),
                        Transaction(
                            id=txid_fee,
                            from_user_id=target.user_id,
                            to_user_id=_treasury_hash(),
                            amount=adjusted_fee,
                            description=f"Drain fee (base: {base_fee_rate:.2%}, wealth-adjusted)",
                            timestamp=discord.utils.utcnow(),
                        ),
                    ]
                )

            await self.update_supply()

        return txid_main, drain_amount

    async def rob_wallet(
        self,
        target_wallet_id: str,
        robber_wallet_id: str,
        description: str,
        percentage: Decimal,
        robbery_debuff: Decimal,
        *,
        counter_loss_percentage: Decimal = Decimal("0"),
        fee_from_amount: bool = True,
    ) -> tuple[str, Decimal, Decimal]:
        """
        Atomically rob a percentage of the target's wallet into the robber's wallet.

        The target's balance is re-read inside the transaction so the robbery
        succeeds even if the balance changed between the command's initial read
        and the actual transfer. Returns (txid, amount_stolen, counter_loss).
        """

        # Check economic circuit breaker before processing
        circuit_breaker = await self.check_economic_circuit_breaker()
        if circuit_breaker["triggered"]:
            reasons = ", ".join(circuit_breaker["reasons"])
            raise ValueError(f"Economic circuit breaker triggered: {reasons}")

        base_fee_rate = await self.get_enhanced_fee_rate("standard")
        adjusted_fee: Decimal | None = None

        async with self.async_sessionmaker() as session:
            async with session.begin():
                target = await session.get(Wallet, target_wallet_id)
                robber = await session.get(Wallet, robber_wallet_id)
                if not target or not robber:
                    raise ValueError("Invalid target or robber wallet.")
                if target.wallet_frozen:
                    raise ValueError("Target's wallet is frozen.")
                if robber.wallet_frozen:
                    raise ValueError("Robber's wallet is frozen.")

                # Re-read balance inside the transaction to avoid race conditions.
                fresh_target_balance = AmountUtils.round_currency(
                    Decimal(str(target.balance))
                )
                if fresh_target_balance <= 0:
                    raise ValueError("Target has no funds to rob.")

                amount_stolen = AmountUtils.round_currency(
                    fresh_target_balance * percentage * robbery_debuff
                )
                counter_loss = AmountUtils.round_currency(
                    fresh_target_balance * counter_loss_percentage * robbery_debuff
                )
                total_theft = amount_stolen + counter_loss

                if total_theft <= 0:
                    raise ValueError("Robbery amount is too small.")

                raw_target_id = await self.resolve_user_hash(target.user_id)
                base_fee = AmountUtils.round_currency(total_theft * base_fee_rate)
                adjusted_fee = await self.calculate_wealth_adjusted_fee(
                    raw_target_id, base_fee
                )

                if fee_from_amount:
                    receiver_gets = total_theft - adjusted_fee
                    if receiver_gets <= 0:
                        raise ValueError("Robbery amount too small to cover fees.")
                    total_deduction = total_theft
                else:
                    receiver_gets = total_theft
                    total_deduction = total_theft + adjusted_fee

                if fresh_target_balance < total_deduction:
                    raise ValueError("Target balance is insufficient to cover robbery.")

                # Atomic balance changes
                await self._atomic_balance_change(
                    session,
                    "wallets",
                    "wallet_id",
                    target_wallet_id,
                    -total_deduction,
                    frozen_field="wallet_frozen",
                )
                await self._atomic_balance_change(
                    session,
                    "wallets",
                    "wallet_id",
                    robber_wallet_id,
                    +receiver_gets,
                )
                await self._credit_treasury(session, +adjusted_fee)

                txid_main = str(uuid.uuid4())
                txid_fee = str(uuid.uuid4())
                session.add_all(
                    [
                        Transaction(
                            id=txid_main,
                            from_user_id=target.user_id,
                            to_user_id=robber.user_id,
                            amount=receiver_gets,
                            description=description,
                            timestamp=discord.utils.utcnow(),
                        ),
                        Transaction(
                            id=txid_fee,
                            from_user_id=target.user_id,
                            to_user_id=_treasury_hash(),
                            amount=adjusted_fee,
                            description=f"Robbery fee (base: {base_fee_rate:.2%}, wealth-adjusted)",
                            timestamp=discord.utils.utcnow(),
                        ),
                    ]
                )

            await self.update_supply()

        return txid_main, amount_stolen, counter_loss

    async def process_treasury_transaction(
        self,
        wallet_id: str,
        amount: Decimal,
        description: str,
        transaction_type: str = "standard",
        guild_id: int = None,
    ):
        # Check economic circuit breaker before processing
        circuit_breaker = await self.check_economic_circuit_breaker()
        if circuit_breaker["triggered"]:
            reasons = ", ".join(circuit_breaker["reasons"])
            raise ValueError(f"Economic circuit breaker triggered: {reasons}")

        # Get wallet to determine user for wealth-adjusted fee
        async with self.async_sessionmaker() as session:
            wallet = await session.get(Wallet, wallet_id)
            if not wallet:
                raise ValueError("Wallet missing.")
            user_id = wallet.user_id
            raw_user_id = await self.resolve_user_hash(user_id)

        # Calculate base fee rate and apply wealth adjustment, unless this is a
        # fee-exempt payout type (game wins, rakeback claims, rewards).
        if transaction_type in _FEE_EXEMPT_TYPES:
            base_fee_rate = Decimal("0")
            adjusted_fee = Decimal("0")
        else:
            base_fee_rate = await self.get_enhanced_fee_rate(transaction_type)
            base_fee = AmountUtils.round_currency(abs(amount) * base_fee_rate)
            adjusted_fee = await self.calculate_wealth_adjusted_fee(raw_user_id, base_fee)

        amount = AmountUtils.round_currency(amount)
        if amount == 0:
            raise ValueError("Cannot process zero-amount transaction.")

        gross = abs(amount)
        fee = adjusted_fee
        net = gross - fee

        fee_rate = fee / gross if gross > 0 else Decimal("0.00")

        async with self.async_sessionmaker() as session:
            async with session.begin():
                wallet = await session.get(Wallet, wallet_id)
                if not wallet:
                    raise ValueError("Wallet missing.")
                if wallet.wallet_frozen:
                    raise ValueError("Wallet is frozen.")

                # This scope's supply row: every treasury move below must hit it,
                # not the shared row.
                supply = await self._ensure_supply_row(session)

                if amount > 0:  # payout: treasury → wallet
                    # Enforce treasury floor: cap payout to protect reserves
                    if supply and supply.total_supply > 0:
                        treasury_floor = AmountUtils.round_currency(
                            supply.total_supply * TREASURY_FLOOR_RATIO
                        )
                        max_payout = supply.treasury - treasury_floor

                        # Soft landing: as health drops from 20% toward the 10%
                        # hard floor, payout capacity scales linearly from 100%
                        # down to 50%. This avoids the sudden "no payouts" cliff.
                        health = (
                            supply.treasury / supply.total_supply
                            if supply.total_supply > 0
                            else Decimal("0")
                        )
                        soft_mult = Decimal("1.0")
                        if health <= Decimal("0.10"):
                            soft_mult = Decimal("0.5")
                        elif health < Decimal("0.20"):
                            t = (health - Decimal("0.10")) / Decimal("0.10")
                            soft_mult = Decimal("0.5") + t * Decimal("0.5")

                        if soft_mult < Decimal("1.0"):
                            max_payout = AmountUtils.round_currency(max_payout * soft_mult)
                            logger.warning(
                                f"[TREASURY SOFT CAP] health={health:.2%}, "
                                f"soft_multiplier={soft_mult:.2%}"
                            )

                        if max_payout < gross:
                            if max_payout <= 0:
                                raise ValueError(
                                    "Treasury reserves are protected — payout unavailable."
                                )
                            # Cap the payout to stay above floor
                            gross = AmountUtils.round_currency(max_payout)
                            net = gross - fee
                            if net <= 0:
                                raise ValueError(
                                    "Treasury reserves are too low for this payout after fees."
                                )
                            logger.warning(
                                f"[TREASURY FLOOR] Payout capped from {abs(amount)} to {gross} "
                                f"(floor={treasury_floor}, treasury={supply.treasury})"
                            )

                    await self._atomic_balance_change(
                        session, "supply", "id", supply.id, -gross, balance_col="treasury"
                    )
                    await self._atomic_balance_change(
                        session,
                        "wallets",
                        "wallet_id",
                        wallet_id,
                        +net,
                        frozen_field="wallet_frozen",
                    )
                    await self._atomic_balance_change(
                        session, "supply", "id", supply.id, +fee, balance_col="treasury"
                    )
                    from_uid, to_uid = _treasury_hash(), wallet.user_id
                else:  # deposit: wallet → treasury
                    await self._atomic_balance_change(
                        session,
                        "wallets",
                        "wallet_id",
                        wallet_id,
                        -gross,
                        frozen_field="wallet_frozen",
                    )
                    await self._atomic_balance_change(
                        session,
                        "supply",
                        "id",
                        supply.id,
                        +net + fee,
                        balance_col="treasury",
                    )
                    from_uid, to_uid = wallet.user_id, _treasury_hash()

                # DB tx rows
                tid_main = str(uuid.uuid4())
                tx_rows = [
                    Transaction(
                        id=tid_main,
                        from_user_id=from_uid,
                        to_user_id=to_uid,
                        amount=net,
                        description=description,
                        timestamp=discord.utils.utcnow(),
                    )
                ]
                if fee > 0:
                    tid_fee = str(uuid.uuid4())
                    tx_rows.append(
                        Transaction(
                            id=tid_fee,
                            from_user_id=from_uid,
                            to_user_id=_treasury_hash(),  # Fee always goes to treasury
                            amount=fee,
                            description=f"{description} (fee @ {fee_rate:.2%})",
                            timestamp=discord.utils.utcnow(),
                        )
                    )
                session.add_all(tx_rows)

            await self.update_supply()

        return tid_main

    async def refund_transaction(
        self, txid: str, reason: str, treasury_fallback: bool = True
    ):
        """
        Refund a transaction.

        Args:
            txid: The transaction ID to refund
            reason: The reason for the refund
            treasury_fallback: If True, treasury will cover if receiver has insufficient funds

        Returns:
            The refund transaction ID
        """
        async with self.async_sessionmaker() as session:
            async with session.begin():
                tx = await session.get(Transaction, txid)
                if not tx:
                    raise ValueError("Transaction not found.")
                if tx.amount == 0:
                    raise ValueError("Cannot refund zero-amount transaction.")

                raw_from_id = await self.resolve_user_hash(tx.from_user_id)
                raw_to_id = await self.resolve_user_hash(tx.to_user_id)
                from_wallet_id = await self.get_wallet_id_for_user(raw_from_id)
                to_wallet_id = await self.get_wallet_id_for_user(raw_to_id)

                # Always give money back to the original sender
                await self._atomic_balance_change(
                    session,
                    "wallets",
                    "wallet_id",
                    from_wallet_id,
                    +tx.amount,
                )

                # Try to take money from the receiver
                try:
                    await self._atomic_balance_change(
                        session,
                        "wallets",
                        "wallet_id",
                        to_wallet_id,
                        -tx.amount,
                    )
                except ValueError as e:
                    # Receiver has insufficient funds
                    if treasury_fallback:
                        # Treasury will cover the shortfall
                        supply = await session.get(Supply, self._supply_pk())
                        if supply and supply.treasury >= tx.amount:
                            supply.treasury -= tx.amount
                            logger.warning(
                                f"Refund {txid}: Receiver had insufficient funds. "
                                f"Treasury covered {tx.amount}. Reason: {reason}"
                            )
                        else:
                            # Create money (mint) if treasury is also insufficient
                            logger.warning(
                                f"Refund {txid}: Treasury insufficient. Minting {tx.amount}. "
                                f"Reason: {reason}"
                            )
                    else:
                        raise ValueError(
                            f"Cannot refund: receiver has insufficient funds and treasury fallback is disabled."
                        ) from e

                # Record refund transaction
                refund_txid = str(uuid.uuid4())
                session.add(
                    Transaction(
                        id=refund_txid,
                        from_user_id=tx.to_user_id,
                        to_user_id=tx.from_user_id,
                        amount=-tx.amount,
                        description=f"Refund for {txid}: {reason}",
                        timestamp=discord.utils.utcnow(),
                    )
                )

            await self.update_supply()
        return refund_txid

    async def get_transaction_by_id(self, txid: str) -> Transaction:
        """
        Fetch a single transaction record by its Transaction ID (UUID).

        :param txid: The UUID string of the transaction you want to look up.
        :return: A single Transaction object, or None if not found.
        """
        async with self.async_sessionmaker() as session:
            try:
                result = await session.execute(
                    select(Transaction).where(Transaction.id == txid)
                )
                transaction = result.scalar_one_or_none()
                return transaction
            except SQLAlchemyError as e:
                logging.error(f"Error retrieving transaction by id {txid}: {str(e)}")
                return None

    async def get_transactions_by_user_id(
        self, user_id: int, limit: Optional[int] = 10
    ) -> list:
        """
        Retrieve the latest transactions for a specific user.
        A transaction is included if the user is either the sender or the receiver.

        :param user_id: The Discord ID (int) of the user.
        :param limit: How many of the most recent transactions to retrieve
            (default=10). Pass ``None`` to retrieve the user's entire history.
        :return: A list of Transaction objects sorted by timestamp descending.
        """
        user_id = self.econ_hash_user_id(user_id)
        async with self.async_sessionmaker() as session:
            try:
                stmt = (
                    select(Transaction)
                    .where(
                        (Transaction.from_user_id == user_id)
                        | (Transaction.to_user_id == user_id)
                    )
                    .order_by(Transaction.timestamp.desc())
                )
                if limit is not None:
                    stmt = stmt.limit(limit)
                result = await session.execute(stmt)
                transactions = result.scalars().all()
                return transactions
            except SQLAlchemyError as e:
                logging.error(f"Error retrieving user {user_id} transactions: {str(e)}")
                return []

    async def update_supply(self):
        async with self.async_sessionmaker() as session:
            async with session.begin():
                supply = await self._ensure_supply_row(session)

                # Ensure supply fields are initialized (handle NULL values)
                if supply.treasury is None:
                    supply.treasury = Decimal("0.00")
                if supply.circulating is None:
                    supply.circulating = Decimal("0.00")
                if supply.total_supply is None:
                    supply.total_supply = Decimal("0.00")

                wallet_total_result = await session.execute(
                    select(func.sum(Wallet.balance)).where(
                        self._scope_filter(Wallet.user_id)
                    )
                )
                wallet_total = wallet_total_result.scalar() or Decimal("0.00")

                cryptocurrency_total_result = await session.execute(
                    select(func.sum(CryptoAsset.amount * CryptoPrice.price))
                    .join(CryptoPrice, CryptoAsset.symbol == CryptoPrice.symbol)
                    .where(self._scope_filter(CryptoAsset.user_id))
                )
                cryptocurrency_total = cryptocurrency_total_result.scalar() or Decimal(
                    "0.00"
                )

                bank_total_result = await session.execute(
                    select(func.sum(Wallet.bank_balance)).where(
                        self._scope_filter(Wallet.user_id)
                    )
                )
                bank_total = bank_total_result.scalar() or Decimal("0.00")

                circulating_supply = AmountUtils.round_currency(
                    wallet_total + bank_total + cryptocurrency_total
                )

                total_supply = AmountUtils.round_currency(
                    circulating_supply + supply.treasury
                )

                treasury_health = (
                    supply.treasury / total_supply
                    if total_supply > 0
                    else Decimal("0.00")
                )

                if treasury_health < Decimal("0.25"):
                    logger.warning(
                        "⚠️ Treasury health is low! Consider minting more currency."
                    )

                supply.circulating = circulating_supply
                supply.total_supply = total_supply

            await session.commit()

    async def get_top_balance_users(self, limit: int = 10) -> list[tuple[int, Decimal]]:
        """
        Retrieve the top users by total balance (wallet + bank + crypto holdings at current prices).

        Args:
            limit: Number of top users to retrieve (default=10)

        Returns:
            List of tuples containing (user_id, total_balance) sorted by total balance descending
        """
        async with self.async_sessionmaker() as session:
            try:
                # Subquery to get the latest price for each crypto symbol
                latest_prices = (
                    select(CryptoPrice.symbol, CryptoPrice.price)
                    .distinct(CryptoPrice.symbol)
                    .order_by(CryptoPrice.symbol, CryptoPrice.timestamp.desc())
                ).subquery()

                # Main query: wallet + bank + crypto holdings at current prices
                stmt = (
                    select(
                        Wallet.user_id,
                        (
                            Wallet.balance
                            + func.coalesce(Wallet.bank_balance, Decimal("0"))
                            + func.coalesce(
                                func.sum(CryptoAsset.amount * latest_prices.c.price),
                                Decimal("0"),
                            )
                        ).label("total_balance"),
                    )
                    .outerjoin(CryptoAsset, CryptoAsset.user_id == Wallet.user_id)
                    .outerjoin(
                        latest_prices, latest_prices.c.symbol == CryptoAsset.symbol
                    )
                    .where(self._scope_filter(Wallet.user_id))
                    .group_by(Wallet.user_id, Wallet.balance, Wallet.bank_balance)
                    .order_by(
                        (
                            Wallet.balance
                            + func.coalesce(Wallet.bank_balance, Decimal("0"))
                            + func.coalesce(
                                func.sum(CryptoAsset.amount * latest_prices.c.price),
                                Decimal("0"),
                            )
                        ).desc()
                    )
                    .limit(limit)
                )
                result = await session.execute(stmt)
                rows = result.all()
                mapping = await self.resolve_user_hashes([h for h, _ in rows])
                return [(mapping.get(h), bal) for h, bal in rows]
            except SQLAlchemyError as e:
                logging.error(f"Error retrieving top balance users: {str(e)}")
                return []

    async def get_balance_user_rank(self, user_id: int) -> Optional[int]:
        """
        Retrieve the supplied users leaderboard position by total balance (wallet + bank combined).
        Args:
            user_id: The Discord ID (int) of the user.
        Returns:
            The rank of the user (1-based), or None if the user has no wallet.
        """
        user_id = self.econ_hash_user_id(user_id)
        async with self.async_sessionmaker() as session:
            try:
                subquery = (
                    select(
                        Wallet.user_id,
                        (Wallet.balance + func.coalesce(Wallet.bank_balance, 0)).label(
                            "total_balance"
                        ),
                    )
                    .where(self._scope_filter(Wallet.user_id))
                    .subquery()
                )

                ranked_query = select(
                    subquery.c.user_id,
                    func.rank()
                    .over(order_by=subquery.c.total_balance.desc())
                    .label("rank"),
                ).subquery()

                stmt = select(ranked_query.c.rank).where(
                    ranked_query.c.user_id == user_id
                )
                result = await session.execute(stmt)
                rank = result.scalar_one_or_none()
                return rank
            except SQLAlchemyError as e:
                logging.error(f"Error retrieving user {user_id} rank: {str(e)}")
                return None

    async def get_top_wallet_users(self, limit: int = 10) -> list[tuple[int, Decimal]]:
        """
        Retrieve the top users by wallet balance.

        Args:
            limit: Number of top users to retrieve (default=10)

        Returns:
            List of tuples containing (user_id, wallet_balance) sorted by wallet balance descending
        """
        async with self.async_sessionmaker() as session:
            try:
                stmt = (
                    select(Wallet.user_id, Wallet.balance)
                    .where(self._scope_filter(Wallet.user_id))
                    .order_by(Wallet.balance.desc())
                    .limit(limit)
                )
                result = await session.execute(stmt)
                rows = result.all()
                mapping = await self.resolve_user_hashes([h for h, _ in rows])
                return [(mapping.get(h), bal) for h, bal in rows]
            except SQLAlchemyError as e:
                logging.error(f"Error retrieving top wallet users: {str(e)}")
                return []

    async def get_top_bank_users(self, limit: int = 10) -> list[tuple[int, Decimal]]:
        """
        Retrieve the top users by bank balance.

        Args:
            limit: Number of top users to retrieve (default=10)

        Returns:
            List of tuples containing (user_id, bank_balance) sorted by bank balance descending
        """
        async with self.async_sessionmaker() as session:
            try:
                stmt = (
                    select(Wallet.user_id, Wallet.bank_balance)
                    .where(self._scope_filter(Wallet.user_id))
                    .order_by(Wallet.bank_balance.desc())
                    .limit(limit)
                )
                result = await session.execute(stmt)
                rows = result.all()
                mapping = await self.resolve_user_hashes([h for h, _ in rows])
                return [(mapping.get(h), bal) for h, bal in rows]
            except SQLAlchemyError as e:
                logging.error(f"Error retrieving top bank users: {str(e)}")
                return []

    async def mint_currency(self, amount: Decimal, description: str):
        """
        Mint new currency and add it to the treasury balance.
        """
        async with self.async_sessionmaker() as session:
            async with session.begin():
                amount = AmountUtils.round_currency(amount)
                supply = await session.get(Supply, self._supply_pk())
                if not supply:
                    raise ValueError("Supply record missing!")

                supply.treasury += amount

                txid = str(uuid.uuid4())
                transaction_db = Transaction(
                    id=txid,
                    from_user_id=None,
                    to_user_id=_treasury_hash(),
                    amount=amount,
                    description=description,
                    timestamp=discord.utils.utcnow(),
                )
                session.add(transaction_db)

            await self.update_supply()

    async def burn_currency(self, amount: Decimal, description: str):
        """
        Burns currency by removing it from treasury, effectively reducing total supply.
        Enforces a treasury floor: will not burn below TREASURY_FLOOR_RATIO of total_supply.
        """
        async with self.async_sessionmaker() as session:
            async with session.begin():
                amount = AmountUtils.round_currency(amount)
                supply = await session.get(Supply, self._supply_pk())
                if not supply:
                    raise ValueError("Supply record missing!")

                if supply.treasury < amount:
                    raise ValueError("Not enough treasury balance to burn.")

                # Enforce treasury floor
                total_after_burn = supply.circulating + (supply.treasury - amount)
                floor = AmountUtils.round_currency(
                    total_after_burn * TREASURY_FLOOR_RATIO
                )
                if (supply.treasury - amount) < floor:
                    # Cap the burn to stay above floor
                    max_burnable = supply.treasury - floor
                    if max_burnable <= 0:
                        raise ValueError("Cannot burn: treasury is at or below floor.")
                    amount = AmountUtils.round_currency(max_burnable)

                supply.treasury -= amount

                txid = str(uuid.uuid4())
                transaction_db = Transaction(
                    id=txid,
                    from_user_id=_treasury_hash(),
                    to_user_id=None,
                    amount=amount,
                    description=description,
                    timestamp=discord.utils.utcnow(),
                )
                session.add(transaction_db)

            await self.update_supply()

    async def set_treasury_balance(self, amount: Decimal, description: str) -> Decimal:
        """
        Force the treasury to an exact balance instead of minting into it.

        The total supply is derived from ``circulating + treasury`` (see
        ``update_supply``), so setting the treasury directly keeps the books
        balanced without creating new money out of nowhere. Returns the new
        treasury balance.
        """
        amount = AmountUtils.round_currency(amount)
        if amount < 0:
            raise ValueError("Treasury amount cannot be negative.")

        async with self.async_sessionmaker() as session:
            async with session.begin():
                supply = await session.get(Supply, self._supply_pk())
                if not supply:
                    raise ValueError("Supply record missing!")

                current = AmountUtils.round_currency(supply.treasury or Decimal("0.00"))
                delta = amount - current
                supply.treasury = amount

                if delta != 0:
                    session.add(
                        Transaction(
                            id=str(uuid.uuid4()),
                            from_user_id=_treasury_hash() if delta < 0 else None,
                            to_user_id=_treasury_hash() if delta > 0 else None,
                            amount=abs(delta),
                            description=description,
                            timestamp=discord.utils.utcnow(),
                        )
                    )

            await self.update_supply()

        return amount

    async def validate_economy(self):
        """
        Cross-check that:
        - sum of all wallets & banks & crypto == supply.circulating
        - supply.circulating + supply.treasury == supply.total_supply
        Returns True if all checks out, False otherwise.
        """
        # Use a small tolerance for Decimal comparisons; strict equality is
        # too sensitive to minute rounding differences and concurrent transactions.
        TOLERANCE = Decimal("0.01")

        async with self.async_sessionmaker() as session:
            async with session.begin():
                # Refresh supply from within the same transaction as the sums
                # so we get a consistent snapshot.
                supply = await session.get(Supply, self._supply_pk())
                if not supply:
                    return False

                if supply.treasury is None:
                    supply.treasury = Decimal("0.00")
                if supply.circulating is None:
                    supply.circulating = Decimal("0.00")
                if supply.total_supply is None:
                    supply.total_supply = Decimal("0.00")

                result_wallet = await session.execute(
                    select(func.sum(Wallet.balance)).where(
                        self._scope_filter(Wallet.user_id)
                    )
                )
                total_wallet = result_wallet.scalar() or Decimal("0.00")

                result_bank = await session.execute(
                    select(func.sum(Wallet.bank_balance)).where(
                        self._scope_filter(Wallet.user_id)
                    )
                )
                total_bank = result_bank.scalar() or Decimal("0.00")

                cryptocurrency_total_result = await session.execute(
                    select(func.sum(CryptoAsset.amount * CryptoPrice.price))
                    .join(CryptoPrice, CryptoAsset.symbol == CryptoPrice.symbol)
                    .where(self._scope_filter(CryptoAsset.user_id))
                )
                cryptocurrency_total = cryptocurrency_total_result.scalar() or Decimal(
                    "0.00"
                )

                actual_circulating = AmountUtils.round_currency(
                    total_wallet + total_bank + cryptocurrency_total
                )
                expected_circulating = supply.circulating
                circulating_diff = (
                    actual_circulating - expected_circulating
                ).copy_abs()

                actual_total = AmountUtils.round_currency(
                    actual_circulating + supply.treasury
                )
                expected_total = supply.total_supply
                total_diff = (actual_total - expected_total).copy_abs()

                ok = True
                if circulating_diff > TOLERANCE:
                    logging.error(
                        "Circulating mismatch: actual=%s, stored=%s, diff=%s",
                        actual_circulating,
                        expected_circulating,
                        circulating_diff,
                    )
                    ok = False

                if total_diff > TOLERANCE:
                    logging.error(
                        "Total supply mismatch: actual=%s, stored=%s, diff=%s",
                        actual_total,
                        expected_total,
                        total_diff,
                    )
                    ok = False

                # Refresh stored supply values if validation passed and the
                # snapshot is still consistent. This prevents the background
                # task from constantly reporting stale supply values.
                if ok and actual_circulating != expected_circulating:
                    supply.circulating = actual_circulating
                    supply.total_supply = actual_total

        return ok

    def _calculate_dynamic_target(self, supply) -> tuple[Decimal, Decimal, Decimal]:
        """
        Calculate the dynamic treasury target and thresholds based on economy maturity.

        In a young economy (low circulation), the treasury is expected to hold most of the supply.
        As users accumulate currency and circulating ratio rises, the target tightens toward 50%.

        Returns:
            (target, min_hw, max_hw) — dynamic treasury health target and thresholds
        """
        total_supply = supply.total_supply
        circulating = supply.circulating

        if total_supply <= 0:
            return Decimal("0.50"), Decimal("0.20"), Decimal("0.95")

        circulation_ratio = (circulating / total_supply).quantize(Decimal("0.0001"))

        # The treasury target must be realistic for how much currency is already
        # in player hands. Aiming for 70% treasury when 80% of supply is
        # circulating forces the rebalancer to mint impossibly large amounts and
        # constantly hit its daily cap. Instead, target tapers with circulation:
        #   0% circulating → 80% treasury
        #   50% circulating → 50% treasury
        #   80% circulating → 32% treasury
        #   >95% circulating → floor at 20% treasury
        target = max(
            Decimal("0.20"), Decimal("0.80") - (circulation_ratio * Decimal("0.60"))
        )
        target = target.quantize(Decimal("0.0001"))

        # Thresholds stay close to the target so rebalancing starts early.
        max_hw = min(Decimal("0.95"), target + Decimal("0.10"))
        min_hw = max(Decimal("0.10"), target - Decimal("0.10"))

        return target, min_hw, max_hw

    async def perform_economic_rebalance(self) -> dict:
        """
        Perform a single economic rebalance cycle with dynamic maturity-aware targets.

        Uses a quadratic intensity curve: gentle corrections near the target,
        aggressive corrections at extremes. Includes:
        - Dynamic target based on circulation ratio (economy maturity)
        - Treasury floor protection (never burn below 10% of total_supply)
        - Faster auto-mint when treasury is stressed (30-minute cooldown, higher cap)
        - Reduced dampening in emergencies
        - Smaller dead zone when treasury is stressed

        Returns a dict describing what action was taken (or why none was taken).
        """
        STEP = Decimal("0.10")
        COOLDOWN = timedelta(hours=1)
        DEAD_ZONE = Decimal("0.03")
        EMERGENCY_DISTANCE = Decimal("0.15")
        MINT_DAMPENER = Decimal("0.5")  # Default mint at 50% of burn rate
        DAILY_MINT_CAP_RATIO = Decimal("0.010")  # Default 1.0% of total_supply per day

        now = discord.utils.utcnow()
        today = now.date()

        # This scope's daily counters, reset when the day changes
        scope = _memo_scope()
        daily_minted = _daily_memo(_DAILY_MINT_TOTAL, _MINT_DAY, today)
        daily_burned = _daily_memo(_DAILY_BURN_TOTAL, _BURN_DAY, today)

        supply = await self.get_supply_record()
        treasury, total_supply = supply.treasury, supply.total_supply

        if total_supply <= 0:
            return {"action": "skipped", "reason": "No supply exists"}

        treasury_health = (treasury / total_supply).quantize(Decimal("0.0001"))
        target, min_hw, max_hw = self._calculate_dynamic_target(supply)

        # Emergency tuning: faster, stronger rebalancing when treasury is stressed.
        # Cap is fixed at 3.0% of total supply per day to refill quickly but avoid
        # runaway inflation.
        is_stressed = treasury_health < min_hw
        if is_stressed:
            COOLDOWN = timedelta(minutes=15)
            DEAD_ZONE = Decimal("0.015")
            MINT_DAMPENER = Decimal("1.0")  # Full computed adjustment when low
            DAILY_MINT_CAP_RATIO = Decimal("0.030")

        # Volume-linked refill runs independently of the normal rebalance cooldown
        # so that active gambling can help replenish a stressed treasury quickly.
        # It activates as soon as health drops below target, not only in crisis.
        volume_result = None
        if treasury_health < target:
            volume_result = await self.mint_from_gambling_volume(window_hours=24)

        # Enforce cooldown
        last_rebalance = _LAST_REBALANCE_AT.get(scope)
        if last_rebalance is not None and now - last_rebalance < COOLDOWN:
            remaining = COOLDOWN - (now - last_rebalance)
            if volume_result and volume_result.get("action") == "volume_mint":
                return volume_result
            return {
                "action": "skipped",
                "reason": f"Cooldown active ({remaining.seconds}s remaining)",
            }

        treasury_floor = AmountUtils.round_currency(total_supply * TREASURY_FLOOR_RATIO)
        base_cap = AmountUtils.round_currency(total_supply * Decimal("0.02"))

        distance = abs(treasury_health - target)

        # Dead zone: skip if within the active dead zone
        if distance < DEAD_ZONE:
            return {
                "action": "skipped",
                "reason": f"Within dead zone (health={treasury_health:.2%}, target={target:.2%}, distance={distance:.2%})",
                "treasury_health": treasury_health,
                "target": target,
            }

        # Quadratic intensity: gentle near target, aggressive at extremes
        intensity = min(Decimal("1"), (distance / Decimal("0.15")) ** 2)

        # Emergency mode: double cap at extreme distances
        cap = base_cap * 2 if distance > EMERGENCY_DISTANCE else base_cap

        gap = (target * total_supply) - treasury
        adj = AmountUtils.round_currency(intensity * STEP * abs(gap))
        adj = min(adj, cap)

        if adj <= 0:
            return {"action": "skipped", "reason": "Calculated adjustment is zero"}

        action_taken = "none"
        amount_adjusted = Decimal("0")
        health_before = treasury_health

        try:
            if gap < 0:
                # Treasury too high → burn
                # Enforce treasury floor: don't burn below floor
                max_burnable = treasury - treasury_floor
                if max_burnable <= 0:
                    return {
                        "action": "skipped",
                        "reason": f"Treasury at floor (treasury={treasury}, floor={treasury_floor})",
                        "treasury_health": treasury_health,
                        "target": target,
                    }
                adj = min(adj, max_burnable)
                await self.burn_currency(
                    adj,
                    f"Auto-burn {adj} (health {treasury_health:.2%}, target {target:.2%})",
                )
                logger.info(
                    f"[AUTO-REBALANCE] Burned {adj} (health {treasury_health:.2%} → target {target:.2%})"
                )
                daily_burned += adj
                _DAILY_BURN_TOTAL[scope] = daily_burned
                action_taken = "burn"
                amount_adjusted = adj

            elif gap > 0:
                # Treasury too low → mint
                mint_adj = AmountUtils.round_currency(adj * MINT_DAMPENER)

                # Enforce daily mint cap (already scaled with deficit when stressed)
                daily_mint_cap = AmountUtils.round_currency(
                    total_supply * DAILY_MINT_CAP_RATIO
                )
                remaining_daily = daily_mint_cap - daily_minted
                if remaining_daily <= 0:
                    return {
                        "action": "skipped",
                        "reason": f"Daily mint cap reached ({daily_minted} / {daily_mint_cap})",
                        "treasury_health": treasury_health,
                        "target": target,
                    }
                mint_adj = min(mint_adj, remaining_daily)

                if mint_adj <= 0:
                    return {
                        "action": "skipped",
                        "reason": "Mint adjustment too small after dampening",
                    }

                # Guard against runaway minting when currency is already widely
                # distributed. In a healthy economy (health >= target) we only
                # expand the treasury when circulation is below 80%. When the
                # treasury is stressed, the circulation check is relaxed so the
                # economy can self-heal back to the target health band.
                circulation_ratio = (
                    supply.circulating / total_supply
                    if total_supply > 0
                    else Decimal("0")
                )
                if treasury_health >= target and circulation_ratio > Decimal("0.80"):
                    return {
                        "action": "skipped",
                        "reason": f"Circulation too high for minting ({circulation_ratio:.2%})",
                        "treasury_health": treasury_health,
                        "target": target,
                    }

                await self.mint_currency(
                    mint_adj,
                    f"Auto-mint {mint_adj} (health {treasury_health:.2%}, target {target:.2%})",
                )
                logger.info(
                    f"[AUTO-REBALANCE] Minted {mint_adj} (health {treasury_health:.2%} → target {target:.2%})"
                )
                daily_minted += mint_adj
                _DAILY_MINT_TOTAL[scope] = daily_minted
                action_taken = "mint"
                amount_adjusted = mint_adj

            _LAST_REBALANCE_AT[scope] = now

            # Recalculate health after action
            supply = await self.get_supply_record()
            health_after = (
                (supply.treasury / supply.total_supply).quantize(Decimal("0.0001"))
                if supply.total_supply > 0
                else Decimal("0")
            )

            result = {
                "action": action_taken,
                "amount": amount_adjusted,
                "health_before": health_before,
                "health_after": health_after,
                "target": target,
                "min_hw": min_hw,
                "max_hw": max_hw,
                "daily_minted": daily_minted,
                "daily_burned": daily_burned,
            }
            if volume_result:
                result["volume_mint"] = volume_result
            return result

        except Exception as e:
            logger.error(f"[AUTO-REBALANCE ERROR]: {e}")
            return {"action": "error", "reason": str(e)}

    async def mint_from_gambling_volume(
        self, window_hours: int = 24
    ) -> dict:
        """
        Mint a fraction of recent player deposits back into the treasury.

        This is a volume-linked refill: the more currency flows into the
        treasury (gambling losses, fees, transfers to house), the more the
        treasury can safely expand when it is below its target. It uses a
        separate daily mint budget from the scheduled auto-rebalance so that
        active gambling can refill the treasury without starving the main
        rebalancer.
        """
        now = discord.utils.utcnow()
        today = now.date()
        scope = _memo_scope()
        daily_volume_minted = _daily_memo(
            _DAILY_VOLUME_MINT_TOTAL, _VOLUME_MINT_DAY, today
        )

        supply = await self.get_supply_record()
        treasury, total_supply = supply.treasury, supply.total_supply
        if total_supply <= 0:
            return {"action": "skipped", "reason": "No supply exists"}

        treasury_health = (treasury / total_supply).quantize(Decimal("0.0001"))
        target, min_hw, max_hw = self._calculate_dynamic_target(supply)

        # Activate whenever health is below the dynamic target, not only when
        # it has already fallen below the minimum threshold. This turns active
        # gambling into an earlier refill mechanism.
        if treasury_health >= target:
            return {
                "action": "skipped",
                "reason": "Treasury at or above target",
                "treasury_health": treasury_health,
                "target": target,
            }

        since = now - timedelta(hours=window_hours)
        async with self.async_sessionmaker() as session:
            deposit_stmt = select(func.sum(Transaction.amount)).where(
                Transaction.to_user_id == _treasury_hash(),
                Transaction.timestamp >= since,
            )
            deposit_result = await session.execute(deposit_stmt)
            deposits_to_treasury = deposit_result.scalar() or Decimal("0")

        if deposits_to_treasury <= 0:
            return {
                "action": "skipped",
                "reason": "No treasury deposits in window",
                "treasury_health": treasury_health,
                "target": target,
            }

        # Fraction scales with distance below target: 1.0% near target up to
        # 5.0% at deep stress so that active gambling helps refill reserves
        # without printing wildly. Volume-mint has its own 3.0% daily budget
        # separate from auto-rebalance.
        stress = max(
            Decimal("0"),
            min(
                Decimal("1"),
                (target - treasury_health) / Decimal("0.10"),
            ),
        )
        fraction = Decimal("0.010") + stress * Decimal("0.040")
        mint_amount = AmountUtils.round_currency(deposits_to_treasury * fraction)

        VOLUME_DAILY_CAP_RATIO = Decimal("0.030")
        daily_cap = AmountUtils.round_currency(total_supply * VOLUME_DAILY_CAP_RATIO)
        remaining = daily_cap - daily_volume_minted
        if remaining <= 0:
            return {
                "action": "skipped",
                "reason": "Daily volume-mint cap reached",
                "treasury_health": treasury_health,
                "daily_volume_minted": daily_volume_minted,
                "daily_cap": daily_cap,
            }

        # Cap this single refill at the smaller of remaining cap or 1.0% of supply
        max_single = min(remaining, AmountUtils.round_currency(total_supply * Decimal("0.010")))
        mint_amount = min(mint_amount, max_single)
        if mint_amount <= 0:
            return {
                "action": "skipped",
                "reason": "Mint amount too small",
                "treasury_health": treasury_health,
            }

        await self.mint_currency(
            mint_amount,
            f"Volume-linked mint {mint_amount} from {deposits_to_treasury} treasury deposits "
            f"(health {treasury_health:.2%})",
        )
        _DAILY_VOLUME_MINT_TOTAL[scope] = daily_volume_minted + mint_amount
        logger.info(
            f"[VOLUME-MINT] Minted {mint_amount} from {deposits_to_treasury} deposits "
            f"(health {treasury_health:.2%}, fraction={fraction:.2%})"
        )

        return {
            "action": "volume_mint",
            "amount": mint_amount,
            "deposits": deposits_to_treasury,
            "fraction": fraction,
            "treasury_health": treasury_health,
            "daily_volume_minted": _DAILY_VOLUME_MINT_TOTAL[scope],
            "daily_cap": daily_cap,
        }

    async def get_economic_factors(self) -> dict:
        """
        Pure-read function: calculates and returns economic metrics and fee rates.
        Does NOT perform any rebalancing or mutations. Use perform_economic_rebalance() for that.
        """
        supply = await self.get_supply_record()
        treasury, total_supply = supply.treasury, supply.total_supply

        if total_supply <= 0:
            return {
                "treasury_health": Decimal("0"),
                "risk_scalar": Decimal("0"),
                "fee_rate": Decimal("0"),
                "passive_income_rate": Decimal("0"),
                "treasury_balance": Decimal("0"),
                "total_supply": Decimal("0"),
                "velocity_of_money": Decimal("0"),
                "liquidity_ratio": Decimal("0"),
                "volatility_index": Decimal("0"),
                "target_ratio": Decimal("0.50"),
                "min_health_threshold": Decimal("0.20"),
                "max_health_threshold": Decimal("0.95"),
            }

        treasury_health = (treasury / total_supply).quantize(Decimal("0.0001"))

        # Load cached/latest metrics (per economy scope)
        metrics = self._get_latest_metrics()
        # Calculate liquidity ratio
        liquidity_ratio = (
            (supply.circulating / total_supply).quantize(Decimal("0.0001"))
            if total_supply > 0
            else Decimal("0")
        )

        # Wealth-inequality Gini is not a useful signal for this economy because
        # a small number of active players hold multi-hundred-trillion balances
        # while most users hold little or nothing. It stays pinned near 1.0 and
        # skews fees/health scores without indicating real instability. Keep the
        # value at 0 so existing consumers and the DB column stay compatible.
        volatility_index = Decimal("0")

        # Calculate velocity of money with protection against division by zero
        async with self.async_sessionmaker() as session:
            # Transaction volume (last 24 hours, this economy scope only)
            yesterday = discord.utils.utcnow() - timedelta(days=1)
            volume_stmt = select(func.sum(Transaction.amount)).where(
                self._scope_filter_either(
                    Transaction.from_user_id, Transaction.to_user_id
                ),
                Transaction.timestamp >= yesterday,
            )
            volume_result = await session.execute(volume_stmt)
            transaction_volume = volume_result.scalar() or Decimal("0.00")

            if supply.circulating > 0:
                velocity_of_money = (
                    transaction_volume / supply.circulating
                ).quantize(Decimal("0.0001"))
            else:
                velocity_of_money = Decimal("0")

        # Dynamic target based on economy maturity
        TARGET, MIN_HW, MAX_HW = self._calculate_dynamic_target(supply)

        BASE_FEE = Decimal("0.01")
        BASE_PASS = Decimal("0.005")
        MAX_FEE_RATE = Decimal("0.10")
        MIN_FEE_RATE = Decimal("0.001")

        if treasury_health < TARGET:
            d = TARGET - treasury_health
            # Linear growth instead of quadratic: still rises as health falls,
            # but avoids the punishing spike that made low-health fees feel
            # exorbitant when combined with wealth-tier multipliers.
            fee_base = (BASE_FEE * (1 + d)).quantize(Decimal("0.0001"))
            passive = (BASE_PASS * (1 - d)).quantize(Decimal("0.0001"))
            risk = (treasury_health / TARGET).quantize(Decimal("0.0001"))
        else:
            fee_base = BASE_FEE
            passive = BASE_PASS
            risk = (Decimal("1.0") + (treasury_health - TARGET)).quantize(
                Decimal("0.0001")
            )

        # Adjust fee based on volatility
        fee = fee_base * (1 + volatility_index * Decimal("0.5"))
        fee = max(MIN_FEE_RATE, min(MAX_FEE_RATE, fee))

        return {
            "treasury_health": treasury_health,
            "risk_scalar": risk,
            "fee_rate": fee,
            "passive_income_rate": passive,
            "treasury_balance": treasury,
            "total_supply": total_supply,
            "target_ratio": TARGET,
            "min_health_threshold": MIN_HW,
            "max_health_threshold": MAX_HW,
            "velocity_of_money": velocity_of_money,
            "liquidity_ratio": liquidity_ratio,
            "volatility_index": volatility_index,
        }

    async def _calculate_wealth_gini(self) -> Decimal:
        """
        Compute the Gini coefficient across all user wealth.

        Wealth includes wallet balance, bank balance, and crypto holdings at the
        latest known prices. The treasury wallet is excluded. Returns a value in
        [0, 1] where 0 is perfect equality and 1 is maximum inequality.
        """
        async with self.async_sessionmaker() as session:
            latest_prices = (
                select(CryptoPrice.symbol, CryptoPrice.price)
                .distinct(CryptoPrice.symbol)
                .order_by(CryptoPrice.symbol, CryptoPrice.timestamp.desc())
            ).subquery()

            wealth_stmt = (
                select(
                    (
                        Wallet.balance
                        + func.coalesce(Wallet.bank_balance, Decimal("0"))
                        + func.coalesce(
                            func.sum(CryptoAsset.amount * latest_prices.c.price),
                            Decimal("0"),
                        )
                    ).label("wealth")
                )
                .outerjoin(CryptoAsset, CryptoAsset.user_id == Wallet.user_id)
                .outerjoin(
                    latest_prices, latest_prices.c.symbol == CryptoAsset.symbol
                )
                .where(
                    Wallet.user_id != _treasury_hash(),
                    self._scope_filter(Wallet.user_id),
                )
                .group_by(Wallet.user_id, Wallet.balance, Wallet.bank_balance)
            )

            result = await session.execute(wealth_stmt)
            balances = [float(r[0]) for r in result.fetchall()]

        if len(balances) <= 1:
            return Decimal("0.00")

        sorted_balances = sorted(balances)
        n = len(sorted_balances)
        total = sum(sorted_balances)
        if total <= 0:
            return Decimal("0.00")

        # Gini = sum((2i - n - 1) * x_i) / (n^2 * mean)
        cumsum = sum(
            (2 * (i + 1) - n - 1) * x for i, x in enumerate(sorted_balances)
        )
        gini = cumsum / (n * n * (total / n))
        gini = max(0.0, min(1.0, gini))
        return Decimal(str(gini)).quantize(Decimal("0.0001"))

    async def get_user_net_worth(self, user_id: int) -> Decimal:
        """
        Calculate a user's net worth for limit and tier calculations.

        Net worth is defined as: wallet balance + bank balance + crypto holdings
        valued at the latest known prices. Shop items, inventory, and other
        virtual goods are explicitly excluded from this value.

        Args:
            user_id: The user's Discord ID (int)

        Returns:
            Decimal: Total net worth (always non-negative)
        """
        raw_user_id = user_id
        user_id = self.econ_hash_user_id(raw_user_id)

        async with self.async_sessionmaker() as session:
            # Wallet + bank balances
            result = await session.execute(
                select(Wallet.balance, Wallet.bank_balance).where(
                    Wallet.user_id == user_id
                )
            )
            row = result.one_or_none()
            wallet_bal = row[0] if row else Decimal("0.00")
            bank_bal = row[1] if row else Decimal("0.00")

            net_worth = wallet_bal + bank_bal

            # Crypto holdings at latest prices
            latest_prices = (
                select(CryptoPrice.symbol, CryptoPrice.price)
                .distinct(CryptoPrice.symbol)
                .order_by(CryptoPrice.symbol, CryptoPrice.timestamp.desc())
            ).subquery()

            crypto_value_result = await session.execute(
                select(
                    func.coalesce(
                        func.sum(CryptoAsset.amount * latest_prices.c.price),
                        Decimal("0.00"),
                    )
                )
                .select_from(CryptoAsset)
                .outerjoin(
                    latest_prices, latest_prices.c.symbol == CryptoAsset.symbol
                )
                .where(CryptoAsset.user_id == user_id)
            )
            crypto_value = crypto_value_result.scalar() or Decimal("0.00")

            return AmountUtils.round_currency(net_worth + crypto_value)

    async def get_user_wealth_tier(self, user_id: int) -> int:
        """
        Calculate the user's wealth tier based on their percentage of total supply.

        Wealth is calculated on-demand as: wallet + bank + crypto at current prices.

        Returns:
            int: Tier 0-4 (0 = no penalty, 1-4 = escalating penalties)
        """
        user_wealth = await self.get_user_net_worth(user_id)

        # Get total supply from economy snapshot
        snapshot = await self.get_economy_snapshot()
        total_supply = snapshot["total_supply"]

        if total_supply <= 0:
            return 0

        # Calculate user's percentage of total supply
        user_ratio = user_wealth / total_supply

        # Determine tier (check from highest to lowest)
        if user_ratio >= WEALTH_TIERS["tier_4"]["threshold"]:
            return 4
        elif user_ratio >= WEALTH_TIERS["tier_3"]["threshold"]:
            return 3
        elif user_ratio >= WEALTH_TIERS["tier_2"]["threshold"]:
            return 2
        elif user_ratio >= WEALTH_TIERS["tier_1"]["threshold"]:
            return 1
        else:
            return 0

    def get_wealth_penalty_multipliers(self, tier: int) -> dict:
        """
        Get penalty multipliers for a given wealth tier.

        Args:
            tier: Wealth tier (0-4)

        Returns:
            dict: Multipliers for 'bet', 'loan', 'fee', 'transfer'
        """
        return TIER_PENALTIES.get(tier, TIER_PENALTIES[0])

    async def get_wealth_tier_info(self, user_id: int) -> dict:
        """
        Get comprehensive wealth tier information for a user.

        Args:
            user_id: The user's ID

        Returns:
            dict: Contains tier, wealth, percentage, thresholds, and multipliers
        """
        raw_user_id = user_id
        user_wealth = await self.get_user_net_worth(raw_user_id)

        # Get total supply from economy snapshot
        snapshot = await self.get_economy_snapshot()
        total_supply = snapshot["total_supply"]

        if total_supply <= 0:
            return {
                "tier": 0,
                "wealth": user_wealth,
                "percentage": Decimal("0"),
                "next_tier_threshold": WEALTH_TIERS["tier_1"]["threshold"],
                "multipliers": TIER_PENALTIES[0],
            }

        # Calculate user's percentage of total supply
        user_ratio = user_wealth / total_supply

        # Determine tier
        tier = await self.get_user_wealth_tier(raw_user_id)

        # Determine next tier threshold
        next_tier_threshold = None
        if tier == 0:
            next_tier_threshold = WEALTH_TIERS["tier_1"]["threshold"]
        elif tier == 1:
            next_tier_threshold = WEALTH_TIERS["tier_2"]["threshold"]
        elif tier == 2:
            next_tier_threshold = WEALTH_TIERS["tier_3"]["threshold"]
        elif tier == 3:
            next_tier_threshold = WEALTH_TIERS["tier_4"]["threshold"]
        # tier 4 has no next tier

        return {
            "tier": tier,
            "wealth": user_wealth,
            "percentage": user_ratio,
            "next_tier_threshold": next_tier_threshold,
            "multipliers": self.get_wealth_penalty_multipliers(tier),
        }

    async def calculate_wealth_adjusted_fee(
        self, user_id: int, base_fee: Decimal
    ) -> Decimal:
        """
        Calculate a wealth-adjusted fee for high-wealth users.

        Args:
            user_id: The user's ID
            base_fee: The base fee amount

        Returns:
            Decimal: Adjusted fee based on user's wealth tier
        """
        raw_user_id = user_id
        user_id = self.econ_hash_user_id(raw_user_id)
        user_tier = await self.get_user_wealth_tier(raw_user_id)
        if user_tier == 0:
            return base_fee

        multipliers = self.get_wealth_penalty_multipliers(user_tier)
        return AmountUtils.round_currency(base_fee * multipliers["fee"])

    async def is_bet_cap_disabled(self, user_id: int) -> bool:
        """Whether the dynamic risk-management bet cap is off for this user.

        A per-user override stored in the refbot ``user_settings`` table wins;
        otherwise the bot-wide ``bet_cap_disabled`` flag in ``bot_settings``
        applies. When no bot is attached (unit tests, one-off scripts) the cap
        stays on.
        """
        bot = getattr(self, "bot", None)
        if bot is None:
            return False

        # Per-user override takes priority over the bot-wide switch.
        db = getattr(bot, "db", None)
        if db is not None:
            try:
                from utils.core import userprefs

                raw = await userprefs.get(db, int(user_id), "bet_cap_disabled", None)
                if raw is not None:
                    return userprefs.as_flag(raw)
            except Exception:
                logger.exception("Could not read bet cap preference for %s", user_id)

        settings = getattr(bot, "dash_settings", None)
        if settings is None:
            return False
        return bool(settings.flag("bet_cap_disabled", False))

    async def get_max_gamble_amount(
        self,
        user_id: int,
        raise_if_limited: bool = False,
        max_payout_multiplier: Decimal = Decimal("1.0"),
    ) -> Decimal:
        """
        Calculates the maximum amount a user can gamble based on dynamic risk controls.

        Risk model includes:
        - Dynamic base bet size scaled to treasury health.
        - Absolute cap on treasury exposure.
        - Whale mitigation (players exceeding 1% of total supply).
        - Minimum floor for newcomers.
        - Adaptive adjustments based on:
            - Market volatility
            - Liquidity levels
            - Recent transaction volume
            - Active user engagement
        - Max payout multiplier adjustment for high-payout games.

        Args:
            user_id: The user's ID
            raise_if_limited: If True, raises ValueError when user exceeds limit
            max_payout_multiplier: Maximum payout multiplier for the game (e.g., 50.0 for crash)
        """
        raw_user_id = user_id

        # ---- constants -------------------------------------------------------
        MAX_TREASURY_EXPOSURE_NORMAL = Decimal("0.02")  # 2% of treasury at/above target
        MIN_ABSOLUTE_FLOOR = Decimal("100.00")  # Floor value for small players

        # ---- fetch user data -------------------------------------------------
        # Net worth = wallet + bank + crypto at current prices.
        # Shop items and other inventory are intentionally excluded.
        user_total = await self.get_user_net_worth(raw_user_id)

        # ---- explicit cap override ------------------------------------------
        # When an owner has disabled the dynamic cap (bot-wide or for this one
        # user), the only remaining limit is what the user actually owns.
        if await self.is_bet_cap_disabled(raw_user_id):
            return AmountUtils.round_currency(max(user_total, Decimal("0")))

        # ---- economy snapshot -----------------------------------------------
        supply = await self.get_supply_record()
        treasury = supply.treasury
        total_supply = supply.total_supply
        target, min_hw, _ = self._calculate_dynamic_target(supply)

        if treasury <= 0 or total_supply <= 0:
            return Decimal("0.00")  # Economy not initialized or broken

        # ---- dynamic economic factors ----------------------------------------
        factors = await self.get_economic_factors()
        health = factors["treasury_health"]
        liquidity_ratio = factors.get("liquidity_ratio", Decimal("0.50"))
        transaction_volume = factors.get("transaction_volume", Decimal("0.00"))
        active_users = factors.get("active_users", 1)

        # ---- dynamic base coefficient ---------------------------------------
        # Bet limits scale directly off treasury health so that heavy gambling
        # cannot drain a stressed treasury. The curve tightens aggressively as
        # health drops, with a hard ceiling from MAX_TREASURY_EXPOSURE below.
        if health >= Decimal("0.80"):
            base_coeff = Decimal("0.0030")  # 0.30%
        elif health >= Decimal("0.60"):
            t = (health - Decimal("0.60")) / Decimal("0.20")
            base_coeff = Decimal("0.0015") + (Decimal("0.0030") - Decimal("0.0015")) * t
        elif health >= Decimal("0.40"):
            t = (health - Decimal("0.40")) / Decimal("0.20")
            base_coeff = Decimal("0.0005") + (Decimal("0.0015") - Decimal("0.0005")) * t
        elif health >= Decimal("0.20"):
            t = (health - Decimal("0.20")) / Decimal("0.20")
            base_coeff = Decimal("0.0002") + (Decimal("0.0005") - Decimal("0.0002")) * t
        else:
            base_coeff = Decimal("0.0001")  # 0.01%

        # ---- apply wealth tier penalty --------------------------------------
        user_tier = await self.get_user_wealth_tier(raw_user_id)
        if user_tier > 0:
            multipliers = self.get_wealth_penalty_multipliers(user_tier)
            base_coeff *= multipliers["bet"]  # Apply tier-based bet multiplier

        # ---- adjust for liquidity scarcity ----------------------------------
        LIQUIDITY_ADJUSTMENT_FACTOR = Decimal("0.8")
        if liquidity_ratio < Decimal("0.3"):  # Less than 30% circulating
            base_coeff *= LIQUIDITY_ADJUSTMENT_FACTOR  # Tighten betting limits

        # ---- adjust for surge in activity -----------------------------------
        AVG_VOLUME_EXPECTED = Decimal("50000")  # Expected daily volume baseline
        if transaction_volume > AVG_VOLUME_EXPECTED * Decimal("1.5"):
            base_coeff *= Decimal("0.9")  # Lower bet size during high traffic

        # ---- adjust for low engagement --------------------------------------
        MIN_ACTIVE_USERS = 10
        if active_users < MIN_ACTIVE_USERS:
            base_coeff *= Decimal("0.9")  # Discourage gambling during low participation

        # ---- scale hard exposure cap with health ---------------------------
        # At or above the dynamic target, allow the full 2% exposure cap. Below
        # target it shrinks linearly, floored at 25% of normal (0.5%) so the
        # treasury never faces a single bet that could move it materially.
        exposure_ratio = MAX_TREASURY_EXPOSURE_NORMAL * min(
            Decimal("1"), max(Decimal("0.25"), health / target)
        )

        # ---- calculate tentative limit --------------------------------------
        by_treasury = AmountUtils.round_currency(treasury * base_coeff)
        hard_cap = AmountUtils.round_currency(treasury * exposure_ratio)
        # Adjust hard cap by max payout multiplier to limit treasury exposure
        # For games with 50x max payout, this ensures max_bet * 50 <= hard_cap
        adjusted_hard_cap = (
            hard_cap / max_payout_multiplier
            if max_payout_multiplier > Decimal("1.0")
            else hard_cap
        )
        provisional = min(by_treasury, adjusted_hard_cap, user_total)

        # ---- enforce adaptive minimum floor ---------------------------------
        adaptive_floor = min(
            MIN_ABSOLUTE_FLOOR,
            AmountUtils.round_currency(user_total * Decimal("0.02")),
        )
        final_limit = max(provisional, adaptive_floor)

        # ---- optional enforcement -------------------------------------------
        if raise_if_limited and user_total > final_limit:
            raise ValueError(
                f"You’re limited to **{final_limit} {self.currency_name}** "
                f"this hand by risk management."
            )

        return final_limit

    async def get_max_loan_amount(self, user_id: int) -> Decimal:
        """
        Calculate the maximum safe loan amount a user can take based on their balance and economic factors.
        Uses a similar risk model to gambling limits but with more leniency.
        """
        raw_user_id = user_id
        # Net worth = wallet + bank + crypto at current prices.
        # Shop items and other inventory are intentionally excluded.
        user_total = await self.get_user_net_worth(raw_user_id)

        supply = await self.get_supply_record()
        treasury = supply.treasury
        total_supply = supply.total_supply

        if treasury <= 0 or total_supply <= 0:
            return Decimal("0.00")

        factors = await self.get_economic_factors()
        health = factors["treasury_health"]

        if health < Decimal("0.25"):
            base_coeff = Decimal("0.0005")  # 0.05% of treasury
        elif health < Decimal("0.50"):
            base_coeff = Decimal("0.001")  # 0.1% of treasury
        else:
            base_coeff = Decimal("0.002")  # 0.2% of treasury

        # ---- apply wealth tier penalty --------------------------------------
        user_tier = await self.get_user_wealth_tier(raw_user_id)
        if user_tier > 0:
            multipliers = self.get_wealth_penalty_multipliers(user_tier)
            base_coeff *= multipliers["loan"]  # Apply tier-based loan multiplier

        max_loan = AmountUtils.round_currency(treasury * base_coeff)
        return min(
            max_loan, user_total * Decimal("1.5")
        )  # Cap at 1.5x user's total balance

    async def get_max_transfer_amount(self, user_id: int) -> Decimal:
        """
        Calculate the maximum amount a user can transfer in a single transaction.
        Uses wealth tier penalties to limit high-wealth users' transfer capabilities.

        Args:
            user_id: The user's ID

        Returns:
            Decimal: Maximum transfer amount
        """
        raw_user_id = user_id
        # Net worth = wallet + bank + crypto at current prices.
        # Shop items and other inventory are intentionally excluded.
        user_total = await self.get_user_net_worth(raw_user_id)

        supply = await self.get_supply_record()
        treasury = supply.treasury
        total_supply = supply.total_supply

        if treasury <= 0 or total_supply <= 0:
            return Decimal("0.00")

        factors = await self.get_economic_factors()
        health = factors["treasury_health"]

        # Base transfer coefficient based on treasury health
        if health < Decimal("0.25"):
            base_coeff = Decimal("0.05")  # 5% of user's wealth
        elif health < Decimal("0.50"):
            base_coeff = Decimal("0.10")  # 10% of user's wealth
        elif health < Decimal("0.75"):
            base_coeff = Decimal("0.15")  # 15% of user's wealth
        else:
            base_coeff = Decimal("0.20")  # 20% of user's wealth

        # Apply wealth tier penalty
        user_tier = await self.get_user_wealth_tier(raw_user_id)
        if user_tier > 0:
            multipliers = self.get_wealth_penalty_multipliers(user_tier)
            base_coeff *= multipliers[
                "transfer"
            ]  # Apply tier-based transfer multiplier

        # Calculate max transfer as percentage of user's wealth
        max_transfer = AmountUtils.round_currency(user_total * base_coeff)

        # Absolute cap based on treasury (max 5% of treasury in single transfer)
        treasury_cap = AmountUtils.round_currency(treasury * Decimal("0.05"))

        return min(max_transfer, treasury_cap)

    async def collect_daily_economy_snapshot(self):
        """
        Collects a snapshot of key economy metrics using existing tables.
        Can be used for logging, alerting, or feeding into predictive models.
        """
        today = discord.utils.utcnow().date()
        # Transaction timestamps are stored as timezone-aware datetimes, so the
        # window bounds must be too: passing date objects here made every
        # collection fail to bind. `today` itself is only used as the history
        # row's date.
        yesterday_start = discord.utils.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0
        ) - timedelta(days=1)
        yesterday_end = yesterday_start + timedelta(days=1)

        async with self.async_sessionmaker() as session:
            # Supply info
            supply = await self._ensure_supply_row(session)
            treasury_balance = supply.treasury
            total_supply = supply.total_supply
            circulating_supply = supply.circulating

            # Wallet summary
            wallet_sum_result = await session.execute(
                select(func.sum(Wallet.balance)).where(
                    self._scope_filter(Wallet.user_id)
                )
            )
            wallet_total = wallet_sum_result.scalar() or Decimal("0.00")

            bank_sum_result = await session.execute(
                select(func.sum(Wallet.bank_balance)).where(
                    self._scope_filter(Wallet.user_id)
                )
            )
            bank_total = bank_sum_result.scalar() or Decimal("0.00")

            avg_wallet_balance = Decimal("0.00")
            wallet_count_result = await session.execute(
                select(func.count(Wallet.wallet_id)).where(
                    self._scope_filter(Wallet.user_id)
                )
            )
            wallet_count = wallet_count_result.scalar()
            if wallet_count and wallet_count > 0:
                avg_wallet_balance = AmountUtils.round_currency(
                    wallet_total / wallet_count
                )

            # Transaction volume (yesterday)
            volume_stmt = select(func.sum(Transaction.amount)).where(
                self._scope_filter_either(
                    Transaction.from_user_id, Transaction.to_user_id
                ),
                Transaction.timestamp >= yesterday_start,
                Transaction.timestamp < yesterday_end,
            )
            volume_result = await session.execute(volume_stmt)
            transaction_volume = volume_result.scalar() or Decimal("0.00")

            # Active users (distinct senders/receivers yesterday)
            active_users_stmt = select(
                func.count(distinct(Transaction.from_user_id)).label("senders"),
                func.count(distinct(Transaction.to_user_id)).label("receivers"),
            ).where(
                self._scope_filter_either(
                    Transaction.from_user_id, Transaction.to_user_id
                ),
                Transaction.timestamp >= yesterday_start,
                Transaction.timestamp < yesterday_end,
            )
            active_users_result = await session.execute(active_users_stmt)
            row = active_users_result.fetchone()
            active_users = (row.senders or 0) + (row.receivers or 0)

            # Wealth-inequality Gini is kept at 0 for this economy; see
            # get_economic_factors() for the rationale.
            volatility_index = Decimal("0")

            # Calculate additional economic metrics
            liquidity_ratio = (
                (circulating_supply / total_supply).quantize(Decimal("0.0001"))
                if total_supply > 0
                else Decimal("0")
            )

            # Calculate velocity of money with protection against division by zero
            if circulating_supply > 0:
                velocity_of_money = (transaction_volume / circulating_supply).quantize(
                    Decimal("0.0001")
                )
            else:
                velocity_of_money = Decimal("0")

            # Get current economic factors for additional metrics
            economic_factors = await self.get_economic_factors()

            scope = _memo_scope()

            # The history table is keyed by date alone and has no scope column,
            # so only the shared economy records history. A server's own economy
            # would otherwise overwrite the shared row for the day; its snapshot
            # stays in the per-scope cache below.
            if not scope:
                historical_record = EconomicMetricsHistory(
                    date=today,
                    total_supply=total_supply,
                    circulating_supply=circulating_supply,
                    treasury_balance=treasury_balance,
                    treasury_health=economic_factors.get(
                        "treasury_health", Decimal("0")
                    ),
                    liquidity_ratio=liquidity_ratio,
                    velocity_of_money=velocity_of_money,
                    volatility_index=volatility_index,
                    transaction_volume=transaction_volume,
                    active_users=active_users,
                    avg_wallet_balance=avg_wallet_balance,
                    fee_rate=economic_factors.get("fee_rate", Decimal("0")),
                    passive_income_rate=economic_factors.get(
                        "passive_income_rate", Decimal("0")
                    ),
                    auto_minted_today=_DAILY_MINT_TOTAL.get(scope, Decimal("0")),
                    auto_burned_today=_DAILY_BURN_TOTAL.get(scope, Decimal("0")),
                    rebalance_target=economic_factors.get(
                        "target_ratio", Decimal("0.50")
                    ),
                )

                # Check if record already exists for today
                existing_stmt = select(EconomicMetricsHistory).where(
                    EconomicMetricsHistory.date == today
                )
                existing_result = await session.execute(existing_stmt)
                existing_record = existing_result.scalar_one_or_none()

                if existing_record:
                    # Update existing record
                    for key, value in historical_record.__dict__.items():
                        if not key.startswith("_") and key != "id":
                            setattr(existing_record, key, value)
                else:
                    # Insert new record
                    session.add(historical_record)

                await session.commit()

            # Cache this scope's snapshot for use in dynamic adjustments. The
            # cache is keyed by scope so one economy's numbers can never be
            # served to another.
            self._set_latest_metrics(
                {
                    "date": today,
                    "treasury_balance": treasury_balance,
                    "total_supply": total_supply,
                    "circulating_supply": circulating_supply,
                    "avg_wallet_balance": avg_wallet_balance,
                    "transaction_volume": transaction_volume,
                    "active_users": active_users,
                    "volatility_index": volatility_index,
                    "liquidity_ratio": liquidity_ratio,
                    "velocity_of_money": velocity_of_money,
                }
            )

        logger.info("[DAILY SNAPSHOT] Economy metrics collected.")

    async def get_economy_snapshot(self) -> dict:
        """
        Get the current economy snapshot.
        Returns cached metrics if available, otherwise calculates fresh snapshot.

        Returns:
            Dictionary with economy metrics including total_supply, circulating_supply, etc.
        """
        # Return this scope's cached metrics if available
        cached = self._get_latest_metrics()
        if cached:
            return cached

        # Otherwise calculate fresh snapshot
        async with self.async_sessionmaker() as session:
            # Get supply info
            supply = await session.get(Supply, self._supply_pk())
            if not supply:
                return {
                    "total_supply": Decimal("0"),
                    "circulating_supply": Decimal("0"),
                    "treasury_balance": Decimal("0"),
                }

            return {
                "total_supply": supply.total_supply,
                "circulating_supply": supply.circulating,
                "treasury_balance": supply.treasury,
            }

    async def get_or_create_user_economic_preferences(
        self, user_id: int
    ) -> UserEconomicPreferences:
        """
        Get user economic preferences, creating default ones if they don't exist.

        Args:
            user_id: Discord user ID

        Returns:
            UserEconomicPreferences object
        """
        await self.ensure_user_identity(user_id)
        user_id = self.econ_hash_user_id(user_id)
        async with self.async_sessionmaker() as session:
            # Try to get existing preferences
            stmt = select(UserEconomicPreferences).where(
                UserEconomicPreferences.user_id == user_id
            )
            result = await session.execute(stmt)
            preferences = result.scalar_one_or_none()

            # If no preferences exist, create default ones
            if not preferences:
                preferences = UserEconomicPreferences(user_id=user_id)
                session.add(preferences)
                await session.commit()

            return preferences

    async def update_user_economic_preferences(self, user_id: int, **kwargs) -> None:
        """
        Update user economic preferences.

        Args:
            user_id: Discord user ID
            **kwargs: Preference fields to update
        """
        await self.ensure_user_identity(user_id)
        user_id = self.econ_hash_user_id(user_id)
        async with self.async_sessionmaker() as session:
            async with session.begin():
                stmt = select(UserEconomicPreferences).where(
                    UserEconomicPreferences.user_id == user_id
                )
                result = await session.execute(stmt)
                preferences = result.scalar_one_or_none()

                if not preferences:
                    preferences = UserEconomicPreferences(user_id=user_id)
                    session.add(preferences)

                # Update provided fields
                for key, value in kwargs.items():
                    if hasattr(preferences, key):
                        setattr(preferences, key, value)

                await session.commit()

    async def get_users_for_economic_alerts(self) -> list:
        """
        Get all users who have economic alerts enabled.

        Returns:
            List of user IDs
        """
        async with self.async_sessionmaker() as session:
            stmt = select(UserEconomicPreferences.user_id).where(
                UserEconomicPreferences.economic_alerts_enabled == True,
                self._scope_filter(UserEconomicPreferences.user_id),
            )
            result = await session.execute(stmt)
            hashes = [row[0] for row in result.fetchall()]
            mapping = await self.resolve_user_hashes(hashes)
            return [mapping.get(h) for h in hashes]

    async def check_and_send_economic_alerts(self) -> dict:
        """
        Check economic conditions and send alerts to users who have them enabled.

        Returns:
            Dictionary with alert statistics
        """
        # Get current economic factors
        factors = await self.get_economic_factors()
        velocity = float(factors.get("velocity_of_money", 0))
        liquidity = float(factors.get("liquidity_ratio", 0))
        volatility = float(factors.get("volatility_index", 0))

        # Get users with alerts enabled
        user_ids = await self.get_users_for_economic_alerts()

        alerts_sent = {
            "velocity": 0,
            "liquidity": 0,
            "volatility": 0,
            "total_users": len(user_ids),
        }

        # For now, we'll just return the stats without actually sending DMs
        # In a real implementation, we would send DMs to users through the bot

        return alerts_sent

    async def get_personalized_economic_recommendations(self, user_id: int) -> dict:
        """
        Get personalized economic recommendations based on user preferences and current conditions.

        Args:
            user_id: Discord user ID

        Returns:
            Dictionary with recommendations
        """
        raw_user_id = user_id
        user_id = self.econ_hash_user_id(raw_user_id)
        # Get user preferences
        preferences = await self.get_or_create_user_economic_preferences(raw_user_id)

        # Get current economic factors
        factors = await self.get_economic_factors()
        health_score = await self.get_economic_health_score()

        recommendations = {
            "timestamp": discord.utils.utcnow().isoformat(),
            "user_risk_profile": preferences.risk_tolerance,
            "user_investment_style": preferences.investment_style,
            "economic_health_score": health_score["score"],
            "economic_health_status": health_score["status"],
            "current_conditions": {
                "treasury_health": float(factors.get("treasury_health", 0)),
                "liquidity_ratio": float(factors.get("liquidity_ratio", 0)),
                "velocity_of_money": float(factors.get("velocity_of_money", 0)),
                "volatility_index": float(factors.get("volatility_index", 0)),
                "fee_rate": float(factors.get("fee_rate", 0)),
            },
            "recommendations": [],
        }

        # Generate recommendations based on conditions
        liquidity_ratio = float(factors.get("liquidity_ratio", 0))
        velocity_of_money = float(factors.get("velocity_of_money", 0))

        # Liquidity-based recommendations (tiered to match scoring)
        # Scoring: 0.3-0.8 = 25pts (perfect), <0.3 scales down: 25 * (ratio/0.3)
        # So: 0.25 → 21pts (yellow), 0.15 → 12.5pts (orange), <0.15 → red
        if liquidity_ratio < 0.15:
            recommendations["recommendations"].append(
                {
                    "type": "liquidity",
                    "priority": "high",
                    "message": "Critical liquidity shortage. Economy is tight - cash is valuable.",
                    "action": "hold_cash",
                }
            )
        elif liquidity_ratio < 0.3:
            recommendations["recommendations"].append(
                {
                    "type": "liquidity",
                    "priority": "medium",
                    "message": "Low liquidity detected. Consider holding cash as opportunities may arise.",
                    "action": "hold_cash",
                }
            )
        elif liquidity_ratio > 0.85:
            recommendations["recommendations"].append(
                {
                    "type": "liquidity",
                    "priority": "medium",
                    "message": "Very high liquidity. Consider investments or large transactions.",
                    "action": "consider_investing",
                }
            )

        # Velocity-based recommendations (tiered to match scoring)
        # Scoring: velocity * 250, capped at 25 points
        # So: 0.1 → 25pts, 0.06 → 15pts, 0.02 → 5pts
        if velocity_of_money < 0.1:
            recommendations["recommendations"].append(
                {
                    "type": "velocity",
                    "priority": "high",
                    "message": "Very low economic activity. Transaction volumes are critically low.",
                    "action": "reduce_trading_activity",
                }
            )
        elif velocity_of_money < 0.2:
            recommendations["recommendations"].append(
                {
                    "type": "velocity",
                    "priority": "medium",
                    "message": "Low economic activity. Transaction volumes are below normal.",
                    "action": "reduce_trading_activity",
                }
            )
        elif velocity_of_money > 0.6:
            recommendations["recommendations"].append(
                {
                    "type": "velocity",
                    "priority": "info",
                    "message": "High economic activity. Markets are very active.",
                    "action": "increase_activity",
                }
            )

        # Risk tolerance based recommendations
        if preferences.risk_tolerance == "low":
            recommendations["recommendations"].append(
                {
                    "type": "risk",
                    "priority": "info",
                    "message": "Conservative risk profile detected. Focus on stable assets and avoid speculative trades.",
                    "action": "focus_stable_assets",
                }
            )
        elif preferences.risk_tolerance == "high":
            recommendations["recommendations"].append(
                {
                    "type": "risk",
                    "priority": "info",
                    "message": "Aggressive risk profile detected. You may consider higher-risk opportunities.",
                    "action": "consider_opportunities",
                }
            )

        return recommendations

    async def get_economic_trends(self, days: int = 30) -> dict:
        """
        Get economic trends over the specified number of days.

        Args:
            days: Number of days to analyze (default: 30)

        Returns:
            Dictionary containing trend analysis for key metrics
        """
        cutoff_date = discord.utils.utcnow().date() - timedelta(days=days)

        async with self.async_sessionmaker() as session:
            stmt = (
                select(EconomicMetricsHistory)
                .where(EconomicMetricsHistory.date >= cutoff_date)
                .order_by(EconomicMetricsHistory.date)
            )

            result = await session.execute(stmt)
            records = result.scalars().all()

            if not records:
                return {"error": "No historical data available"}

            # Calculate trends
            trends = {"period_days": days, "data_points": len(records), "metrics": {}}

            # Define metrics to analyze
            metrics_to_analyze = [
                "treasury_health",
                "liquidity_ratio",
                "velocity_of_money",
                "volatility_index",
                "transaction_volume",
                "active_users",
                "fee_rate",
                "passive_income_rate",
            ]

            for metric in metrics_to_analyze:
                values = [
                    getattr(record, metric)
                    for record in records
                    if getattr(record, metric) is not None
                ]
                if values:
                    # Calculate basic statistics
                    current = values[-1] if values else Decimal("0")
                    previous = values[-2] if len(values) > 1 else Decimal("0")

                    if len(values) > 1:
                        avg = sum(values) / len(values)
                        # Calculate trend (slope approximation)
                        trend_slope = (values[-1] - values[0]) / len(values)

                        trends["metrics"][metric] = {
                            "current": float(current),
                            "average": float(avg),
                            "change_from_previous": float(current - previous)
                            if previous != 0
                            else 0,
                            "change_percent": float(
                                ((current - previous) / previous) * 100
                            )
                            if previous != 0
                            else 0,
                            "trend_slope": float(trend_slope),
                            "min": float(min(values)),
                            "max": float(max(values)),
                        }
                    else:
                        trends["metrics"][metric] = {
                            "current": float(current),
                            "average": float(current),
                            "change_from_previous": 0,
                            "change_percent": 0,
                            "trend_slope": 0,
                            "min": float(current),
                            "max": float(current),
                        }

            return trends

    async def get_economic_health_score(self) -> dict:
        """
        Calculate an overall economic health score based on multiple factors.

        Returns:
            Dictionary containing health score and contributing factors
        """
        factors = await self.get_economic_factors()

        # Extract key metrics
        treasury_health = float(factors.get("treasury_health", 0))
        liquidity_ratio = float(factors.get("liquidity_ratio", 0))
        velocity_of_money = float(factors.get("velocity_of_money", 0))

        # Weighted scoring system
        # Volatility/wealth-inequality is not used; its 20% weight is redistributed
        # proportionally to the remaining three components:
        #   treasury: 30% → 37.5%
        #   liquidity: 25% → 31.25%
        #   velocity: 25% → 31.25%
        treasury_score = Decimal(str(treasury_health)) * Decimal("37.5")

        # Liquidity ratio (31.25% weight) - ideal is around 0.5-0.8
        if 0.3 <= liquidity_ratio <= 0.8:
            liquidity_score = Decimal("31.25")  # Perfect score for healthy liquidity
        elif liquidity_ratio > 0.8:
            liquidity_score = Decimal("31.25") * (
                Decimal("0.8") / Decimal(str(liquidity_ratio))
            )  # Decrease score for too much liquidity
        else:
            liquidity_score = Decimal("31.25") * (
                Decimal(str(liquidity_ratio)) / Decimal("0.3")
            )  # Decrease score for too little liquidity

        # Velocity of money (31.25% weight) - higher is generally better.
        # Daily velocity of 0.1 (10% of circulating supply transacted per day)
        # is a healthy, active Discord economy; scale so that reaches full points.
        velocity_score = min(Decimal("31.25"), Decimal(str(velocity_of_money)) * Decimal("312.5"))  # Cap at 31.25 points

        # Calculate total score (0-100)
        total_score = treasury_score + liquidity_score + velocity_score

        # Normalize to 0-100 range
        health_score = max(0, min(100, float(total_score)))

        # Determine health status
        if health_score >= 80:
            status = "Excellent"
        elif health_score >= 60:
            status = "Good"
        elif health_score >= 40:
            status = "Fair"
        elif health_score >= 20:
            status = "Poor"
        else:
            status = "Critical"

        return {
            "score": round(health_score, 2),
            "status": status,
            "components": {
                "treasury_health": {
                    "value": round(treasury_health * 100, 2),
                    "score": round(float(treasury_score), 2),
                    "weight": Decimal("37.5"),
                },
                "liquidity_ratio": {
                    "value": round(liquidity_ratio * 100, 2),
                    "score": round(float(liquidity_score), 2),
                    "weight": Decimal("31.25"),
                },
                "velocity_of_money": {
                    "value": round(velocity_of_money, 4),
                    "score": round(float(velocity_score), 2),
                    "weight": Decimal("31.25"),
                },
            },
        }

    async def get_historical_economic_metrics(self, days: int = 30) -> list:
        """
        Get raw historical economic metrics for the specified number of days.

        Args:
            days: Number of days to retrieve (default: 30)

        Returns:
            List of historical metrics dictionaries
        """
        cutoff_date = discord.utils.utcnow().date() - timedelta(days=days)

        async with self.async_sessionmaker() as session:
            stmt = (
                select(EconomicMetricsHistory)
                .where(EconomicMetricsHistory.date >= cutoff_date)
                .order_by(EconomicMetricsHistory.date.desc())
            )

            result = await session.execute(stmt)
            records = result.scalars().all()

            # Convert to dictionary format for easy consumption
            metrics_list = []
            for record in records:
                metrics_list.append(
                    {
                        "date": record.date.isoformat(),
                        "treasury_health": float(record.treasury_health),
                        "liquidity_ratio": float(record.liquidity_ratio),
                        "velocity_of_money": float(record.velocity_of_money),
                        "volatility_index": float(record.volatility_index),
                        "transaction_volume": float(record.transaction_volume),
                        "active_users": record.active_users,
                        "fee_rate": float(record.fee_rate),
                        "passive_income_rate": float(record.passive_income_rate),
                    }
                )

            return metrics_list

    async def add_loan_record(
        self,
        user_id: int,
        principal: Decimal,
        interest_rate: Decimal,
        total_repay: Decimal,
        due_date: datetime,
        status: str = "active",
    ):
        """
        Add a new loan record to the database for a user. Ensure one loan per user for simplicity.
        """
        await self.ensure_user_identity(user_id)
        user_id = self.econ_hash_user_id(user_id)
        async with self.async_sessionmaker() as session:
            async with session.begin():
                existing_loan = await session.execute(
                    select(Loan).where(Loan.user_id == user_id, Loan.status == "active")
                )
                if existing_loan.scalar_one_or_none():
                    raise ValueError("User already has an active loan.")

                new_loan = Loan(
                    user_id=user_id,
                    principal=principal,
                    interest_rate=interest_rate,
                    total_repay=total_repay,
                    due_date=due_date,
                    status=status,
                )
                session.add(new_loan)
            await session.commit()

    async def get_active_loans_for_user(self, user_id: int) -> list[Loan]:
        user_id = self.econ_hash_user_id(user_id)
        async with self.async_sessionmaker() as session:
            result = await session.execute(
                select(Loan).where(
                    Loan.user_id == user_id,
                    Loan.status.in_(["active", "overdue", "defaulted"]),
                )
            )
            return result.scalars().all()

    async def update_loan_for_user(
        self,
        user_id: int,
        new_status: str,
        new_principal: Decimal = None,
        new_interest_rate: Decimal = None,
        new_total_repay: Decimal = None,
        new_due_date: datetime = None,
    ):
        user_id = self.econ_hash_user_id(user_id)
        async with self.async_sessionmaker() as session:
            async with session.begin():
                result = await session.execute(
                    select(Loan).where(Loan.user_id == user_id, Loan.status == "active")
                )
                active_loan = result.scalar_one_or_none()
                if not active_loan:
                    raise ValueError("No active loan found for user.")
                active_loan.status = new_status
                if new_principal is not None:
                    active_loan.principal = new_principal
                if new_interest_rate is not None:
                    active_loan.interest_rate = new_interest_rate
                if new_total_repay is not None:
                    active_loan.total_repay = new_total_repay
                if new_due_date is not None:
                    active_loan.due_date = new_due_date
            await session.commit()

    async def make_loan_payment(
        self, user_id: int, payment_amount: Decimal, notes: str = None
    ):
        """
        Record a partial or full loan payment. Updates the loan's amount_paid field
        and creates a LoanPayment record. Automatically marks loan as 'paid' if fully repaid.

        Args:
            user_id: Discord user ID
            payment_amount: Amount to pay (must be > 0 and <= remaining balance)
            notes: Optional notes about the payment

        Returns:
            dict with payment details and new balance

        Raises:
            ValueError: If no active loan exists or payment amount is invalid
        """
        await self.ensure_user_identity(user_id)
        user_id = self.econ_hash_user_id(user_id)

        if payment_amount <= 0:
            raise ValueError("Payment amount must be greater than 0.")

        async with self.async_sessionmaker() as session:
            async with session.begin():
                result = await session.execute(
                    select(Loan).where(
                        Loan.user_id == user_id, Loan.status.in_(["active", "overdue"])
                    )
                )
                active_loan = result.scalar_one_or_none()
                if not active_loan:
                    raise ValueError("No active loan found for user.")

                remaining_balance = active_loan.total_repay - active_loan.amount_paid
                if payment_amount > remaining_balance:
                    raise ValueError(
                        f"Payment amount exceeds remaining balance of {remaining_balance}."
                    )

                # Create payment record
                payment = LoanPayment(
                    loan_id=active_loan.id,
                    user_id=user_id,
                    payment_method="manual",  # Could be extended to support different methods
                    payment_amount=payment_amount,
                    notes=notes,
                )
                session.add(payment)

                # Update loan amount paid
                active_loan.amount_paid += payment_amount

                # Check if fully paid
                if active_loan.amount_paid >= active_loan.total_repay:
                    active_loan.status = "paid"
                    active_loan.amount_paid = (
                        active_loan.total_repay
                    )  # Ensure exact match

            await session.commit()

            return {
                "payment_amount": payment_amount,
                "previous_paid": active_loan.amount_paid - payment_amount,
                "new_amount_paid": active_loan.amount_paid,
                "remaining_balance": active_loan.total_repay - active_loan.amount_paid,
                "loan_status": active_loan.status,
            }

    async def get_loan_payment_history(self, user_id: int) -> list[LoanPayment]:
        """
        Retrieve all payment records for a user's loans.

        Args:
            user_id: Discord user ID

        Returns:
            List of LoanPayment records ordered by payment_date descending
        """
        user_id = self.econ_hash_user_id(user_id)
        async with self.async_sessionmaker() as session:
            result = await session.execute(
                select(LoanPayment)
                .join(Loan, LoanPayment.loan_id == Loan.id)
                .where(Loan.user_id == user_id)
                .order_by(LoanPayment.payment_date.desc())
            )
            return result.scalars().all()

    async def get_loan_remaining_balance(self, user_id: int) -> Decimal:
        """
        Calculate the remaining balance on a user's active loan.

        Args:
            user_id: Discord user ID

        Returns:
            Decimal representing remaining balance (total_repay - amount_paid)

        Raises:
            ValueError: If no active loan exists
        """
        user_id = self.econ_hash_user_id(user_id)
        async with self.async_sessionmaker() as session:
            result = await session.execute(
                select(Loan).where(
                    Loan.user_id == user_id, Loan.status.in_(["active", "overdue"])
                )
            )
            active_loan = result.scalar_one_or_none()
            if not active_loan:
                raise ValueError("No active loan found for user.")

            return active_loan.total_repay - active_loan.amount_paid

    async def date_check_loans(self):
        """
        Check all active loans and mark those past due as 'defaulted'.
        Add penalty of 10% of loan amount to the total_repay amount for defaulted loans each day it is not repaid.
        If the loan is not paid back in 7 days after the due date, freeze the user's wallet.
        Automatically unfreeze the wallet 7 days after defaulting.
        This can be scheduled to run periodically (e.g., every hour).
        """
        async with self.async_sessionmaker() as session:
            async with session.begin():
                now = discord.utils.utcnow()

                # Process overdue loans
                result = await session.execute(
                    select(Loan).where(
                        Loan.status == "active",
                        Loan.due_date < now,
                        self._scope_filter(Loan.user_id),
                    )
                )
                overdue_loans = result.scalars().all()
                for loan in overdue_loans:
                    days_overdue = (now - loan.due_date).days
                    if days_overdue > 0:
                        loan.status = "overdue"
                        loan.total_repay += AmountUtils.quantize(
                            loan.principal * Decimal("0.1") * days_overdue,
                            Decimal("0.1"),
                        )
                    if days_overdue >= 7:
                        loan.status = "defaulted"
                        loan.defaulted_date = now
                        raw_uid = await self.resolve_user_hash(loan.user_id)
                        wallet = await self.get_wallet_by_user_id(raw_uid)
                        if wallet and not wallet.wallet_frozen:
                            await self.freeze_wallet(wallet.wallet_id)

                # Unfreeze wallets 7 days after defaulting
                result = await session.execute(
                    select(Loan)
                    .join(Wallet, Loan.user_id == Wallet.user_id)
                    .where(
                        Loan.status == "defaulted",
                        Loan.defaulted_date != None,
                        Wallet.wallet_frozen == True,
                        self._scope_filter(Loan.user_id),
                    )
                )
                defaulted_loans = result.scalars().all()
                for loan in defaulted_loans:
                    days_defaulted = (now - loan.defaulted_date).days
                    if days_defaulted >= 7:
                        raw_uid = await self.resolve_user_hash(loan.user_id)
                        wallet = await self.get_wallet_by_user_id(raw_uid)
                        if wallet and wallet.wallet_frozen:
                            await self.unfreeze_wallet(wallet.wallet_id)

    async def get_treasury_reward_multiplier(self) -> Decimal:
        """
        Return a multiplier that scales rewards/faucets based on treasury health.

        - Treasury health at or above target + 0.20 → 1.5x (flush, boost progress)
        - Treasury health at target → 1.0x
        - Treasury health at or below target - 0.20 → 0.2x (stressed, slow drain)
        - Linear between.
        """
        factors = await self.get_economic_factors()
        treasury_health = factors.get("treasury_health", Decimal("0.5"))
        target_ratio = factors.get("target_ratio", Decimal("0.50"))

        delta = treasury_health - target_ratio
        if delta >= Decimal("0.20"):
            return Decimal("1.5")
        if delta <= Decimal("-0.20"):
            return Decimal("0.2")

        # Linear interpolation from 0.2 at -0.20 to 1.5 at +0.20
        t = (delta + Decimal("0.20")) / Decimal("0.40")
        return (Decimal("0.2") + t * Decimal("1.3")).quantize(Decimal("0.01"))

    async def get_dynamic_reward_multiplier(self) -> Decimal:
        """
        Calculate dynamic reward multiplier based on economic conditions.

        Uses linear interpolation for smooth transitions instead of step tiers:
          liquidity <= 0.20 → 1.5x (max boost)
          liquidity  = 0.50 → 1.0x (neutral)
          liquidity >= 0.80 → 0.85x (slight reduction)

        Also factors in treasury health: if treasury is below its dynamic target,
        rewards are reduced to slow outflow; if above, rewards are boosted.
        Final multiplier is clamped to [0.10, 1.60].
        """
        factors = await self.get_economic_factors()
        liquidity_ratio = factors.get("liquidity_ratio", Decimal("0.5"))

        # Linear interpolation for liquidity-based multiplier
        if liquidity_ratio <= Decimal("0.20"):
            liquidity_mult = Decimal("1.5")
        elif liquidity_ratio <= Decimal("0.50"):
            # Interpolate from 1.5 (at 0.20) to 1.0 (at 0.50)
            t = (liquidity_ratio - Decimal("0.20")) / Decimal("0.30")
            liquidity_mult = Decimal("1.5") - (t * Decimal("0.5"))
        elif liquidity_ratio <= Decimal("0.80"):
            # Interpolate from 1.0 (at 0.50) to 0.85 (at 0.80)
            t = (liquidity_ratio - Decimal("0.50")) / Decimal("0.30")
            liquidity_mult = Decimal("1.0") - (t * Decimal("0.15"))
        else:
            liquidity_mult = Decimal("0.85")

        # Treasury health directly scales rewards/faucets
        treasury_mult = await self.get_treasury_reward_multiplier()

        multiplier = liquidity_mult * treasury_mult

        # Clamp to safe range
        return max(Decimal("0.10"), min(Decimal("1.60"), multiplier))

    async def check_economic_circuit_breaker(self) -> dict:
        """
        Check if economic circuit breakers should be triggered based on velocity and other metrics.

        Returns a dictionary with breaker status and reason.
        """
        factors = await self.get_economic_factors()
        velocity = factors.get("velocity_of_money", Decimal("0"))
        liquidity_ratio = factors.get("liquidity_ratio", Decimal("0.5"))
        # volatility = factors.get("volatility_index", Decimal("0.02"))

        # Define thresholds
        VELOCITY_CRISIS_THRESHOLD = Decimal("0.001")  # Near-zero money velocity
        LIQUIDITY_CRISIS_THRESHOLD = Decimal("0.005")  # Less than 0.5% circulating

        # Bootstrap guard: a fresh/empty database has no circulating currency and
        # therefore no velocity. The circuit breaker is meant to detect crises in
        # an active economy, not to prevent the economy from starting up.
        if liquidity_ratio <= 0:
            return {
                "triggered": False,
                "reasons": [],
                "velocity": velocity,
                "liquidity_ratio": liquidity_ratio,
            }

        reason = []

        if velocity < VELOCITY_CRISIS_THRESHOLD:
            reason.append("Low velocity of money")

        if liquidity_ratio < LIQUIDITY_CRISIS_THRESHOLD:
            reason.append("Low liquidity")

        # Both conditions must be met to trigger the circuit breaker
        circuit_breaker_triggered = (
            velocity < VELOCITY_CRISIS_THRESHOLD
            and liquidity_ratio < LIQUIDITY_CRISIS_THRESHOLD
        )

        return {
            "triggered": circuit_breaker_triggered,
            "reasons": reason,
            "velocity": velocity,
            "liquidity_ratio": liquidity_ratio,
        }

    async def get_enhanced_fee_rate(
        self, transaction_type: str = "standard"
    ) -> Decimal:
        """
        Calculate enhanced fee rate based on multiple economic factors.

        Args:
            transaction_type: Type of transaction ('standard', 'high_value', 'gambling')
        """
        factors = await self.get_economic_factors()
        base_fee = factors.get("fee_rate", Decimal("0.01"))
        volatility = factors.get("volatility_index", Decimal("0.02"))
        liquidity_ratio = factors.get("liquidity_ratio", Decimal("0.5"))
        velocity = factors.get("velocity_of_money", Decimal("0.1"))

        # Adjust fee based on transaction type
        if transaction_type == "high_value":
            # Higher fees for high-value transactions during volatile times
            fee = base_fee * (1 + volatility * Decimal("2.0"))
        elif transaction_type == "gambling":
            # Special fee structure for gambling
            fee = base_fee * (1 + (Decimal("1.0") - liquidity_ratio))
        else:
            # Standard transaction fees
            fee = base_fee * (1 + volatility * Decimal("0.5"))

        # Additional adjustment based on velocity
        if velocity < Decimal("0.05"):
            # Low velocity indicates economic slowdown - reduce fees to stimulate activity
            fee *= Decimal("0.8")
        elif velocity > Decimal("0.5"):
            # High velocity indicates hot market - increase fees to cool down
            fee *= Decimal("1.2")

        # Ensure fee stays within reasonable bounds
        MAX_FEE = Decimal("0.20")  # 20%
        MIN_FEE = Decimal("0.001")  # 0.1%

        return max(MIN_FEE, min(MAX_FEE, fee))

    async def get_job(self, user_id: int) -> Optional[Job]:
        """Get a user's current job, if any."""
        user_id = self.econ_hash_user_id(user_id)
        async with self.async_sessionmaker() as session:
            result = await session.execute(select(Job).where(Job.user_id == user_id))
            return result.scalar_one_or_none()

    async def apply_for_job(
        self, user_id: int, job_title: str, base_salary: Decimal
    ) -> Job:
        """Apply for a job. Creates a new job record for the user."""
        await self.ensure_user_identity(user_id)
        user_id = self.econ_hash_user_id(user_id)
        async with self.async_sessionmaker() as session:
            async with session.begin():
                # Check if user already has a job
                existing = await session.execute(
                    select(Job).where(Job.user_id == user_id)
                )
                if existing.scalar_one_or_none():
                    raise ValueError(
                        "You already have a job. Quit your current job first."
                    )

                job = Job(
                    user_id=user_id,
                    title=job_title,
                    base_salary=base_salary,
                    days_employed=1,
                    streak=0,
                    hired_at=discord.utils.utcnow(),
                )
                session.add(job)
            await session.commit()
            return job

    async def quit_job(self, user_id: int) -> bool:
        """Remove a user's job record."""
        user_id = self.econ_hash_user_id(user_id)
        async with self.async_sessionmaker() as session:
            async with session.begin():
                result = await session.execute(
                    select(Job).where(Job.user_id == user_id)
                )
                job = result.scalar_one_or_none()
                if not job:
                    raise ValueError("You don't have a job to quit.")
                await session.delete(job)
            await session.commit()
            return True

    async def work_job(self, user_id: int) -> Tuple[Job, Decimal]:
        """
        Process a user's work action.
        Returns the updated job and the calculated salary.
        Raises ValueError if cooldown hasn't expired, no job found, or user was fired for absence.
        """
        user_id = self.econ_hash_user_id(user_id)
        async with self.async_sessionmaker() as session:
            async with session.begin():
                result = await session.execute(
                    select(Job).where(Job.user_id == user_id)
                )
                job = result.scalar_one_or_none()
                if not job:
                    raise ValueError("You don't have a job. Apply for one first!")

                now = discord.utils.utcnow()

                # Check if user missed a day (more than 48 hours since last work)
                # They get fired for not showing up
                if job.last_worked:
                    time_since_last = (now - job.last_worked).total_seconds()
                    if time_since_last > 172800:  # 48 hours = 172800 seconds
                        # Fire the employee for missing work
                        await session.delete(job)
                        await session.commit()
                        raise ValueError(
                            "You were fired for missing work! You didn't show up for more than 48 hours. "
                            "You'll need to apply for a new job."
                        )

                    # Check if 24 hours have passed since last work (normal cooldown)
                    cooldown_remaining = (
                        86400 - time_since_last
                    )  # 24 hours = 86400 seconds
                    if cooldown_remaining > 0:
                        hours = int(cooldown_remaining // 3600)
                        minutes = int((cooldown_remaining % 3600) // 60)
                        raise ValueError(
                            f"You need to wait {hours}h {minutes}m before working again."
                        )

                # Calculate salary with tenure bonus
                weeks_employed = job.days_employed / 7
                salary_multiplier = min(2.0, 1.0 + (weeks_employed * 0.05))
                current_salary = AmountUtils.round_currency(
                    job.base_salary * Decimal(str(salary_multiplier))
                )

                # Update job record
                job.last_worked = now
                job.days_employed += 1
                job.streak += 1

            await session.commit()
            return job, current_salary

    async def calculate_salary(self, job: Job) -> Decimal:
        """Calculate current salary based on tenure."""
        weeks_employed = job.days_employed / 7
        salary_multiplier = min(2.0, 1.0 + (weeks_employed * 0.05))
        current_salary = job.base_salary * Decimal(str(salary_multiplier))
        return AmountUtils.round_currency(current_salary)

    async def get_employees_for_firing(self) -> List[Job]:
        """
        Get employees who haven't worked in 48+ hours.
        These users should be fired.
        """
        threshold = discord.utils.utcnow() - timedelta(hours=48)
        async with self.async_sessionmaker() as session:
            result = await session.execute(
                select(Job).where(
                    (Job.last_worked != None) & (Job.last_worked < threshold),
                    self._scope_filter(Job.user_id),
                )
            )
            return result.scalars().all()

    async def fire_employee(self, user_id: int) -> bool:
        """Remove a job record for an inactive employee."""
        user_id = self.econ_hash_user_id(user_id)
        async with self.async_sessionmaker() as session:
            async with session.begin():
                result = await session.execute(
                    select(Job).where(Job.user_id == user_id)
                )
                job = result.scalar_one_or_none()
                if job:
                    await session.delete(job)
            await session.commit()
            return True

    async def get_all_jobs(self) -> List[Job]:
        """Get all job records for this economy scope (for admin purposes)."""
        async with self.async_sessionmaker() as session:
            result = await session.execute(
                select(Job).where(self._scope_filter(Job.user_id))
            )
            return result.scalars().all()
