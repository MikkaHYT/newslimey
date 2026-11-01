from sqlalchemy import text, event, inspect
# The bot runs on a local SQLite database, so upserts use the SQLite dialect's
# INSERT ... ON CONFLICT support (PostgreSQL's pg_insert is no longer used).
from sqlalchemy.dialects.sqlite import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.exc import (
    SQLAlchemyError,
    OperationalError,
    DBAPIError,
    DisconnectionError,
    TimeoutError as SATimeoutError,
)
from sqlalchemy.future import select
from sqlalchemy.orm import sessionmaker
import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
import time
import functools
from typing import Optional
from ..models import (
    Base,
    CommandRoleRestriction,
    CommandStatus,
    EconomyIdentity,
    UserIdentity,
)
import logging

#: Returned by the hot-path cache when a key is missing or expired.
_CACHE_MISS = object()


def _hash_user_id(user_id) -> str:
    """Centralized user ID hashing used by all managers."""
    from utils.security import hash_user_id

    return hash_user_id(user_id)


try:
    import asyncpg.exceptions as _apg_exc
except Exception:
    _apg_exc = None

logger = logging.getLogger("discord.client")

# Exceptions treated as transient database connection failures.  These are retried
# with exponential backoff so brief outages (restart, network blip, failover)
# don't crash commands or background tasks.
_RETRYABLE_DB_ERRORS: tuple[type[Exception], ...] = (
    OperationalError,
    DBAPIError,
    DisconnectionError,
    SATimeoutError,
    ConnectionResetError,
    ConnectionRefusedError,
    OSError,
)

if _apg_exc is not None:
    _RETRYABLE_DB_ERRORS += (
        _apg_exc.ConnectionDoesNotExistError,
        _apg_exc.ConnectionFailureError,
        _apg_exc.PostgresConnectionError,
        _apg_exc.InterfaceError,
        _apg_exc.TooManyConnectionsError,
    )


def retry_db(max_retries: int = None, base_delay: float = None):
    """Decorator that retries a DatabaseManager coroutine on transient DB errors."""

    def decorator(coro):
        @functools.wraps(coro)
        async def wrapper(self: "BaseManager", *args, **kwargs):
            return await self._db_retry(
                coro, self, *args, retries=max_retries, base_delay=base_delay, **kwargs
            )

        return wrapper

    return decorator


def db_safe(default=None):
    """Decorator for manager coroutines that should return *default* on DB errors.

    Unlike @retry_db, this fast-fails when the DB circuit breaker is open so a
    Postgres outage can't starve the asyncio event loop with reconnect attempts.
    Use this for hot-path event handlers and read-only checks where a safe
    default is acceptable during a brief database blip.
    """

    def decorator(coro):
        @functools.wraps(coro)
        async def wrapper(self: "BaseManager", *args, **kwargs):
            if not await self.db_ready():
                return default
            try:
                return await coro(self, *args, **kwargs)
            except Exception as exc:
                if self._is_retryable_db_error(exc):
                    await self._mark_db_unhealthy()
                    return default
                raise

        return wrapper

    return decorator


class BaseManager:
    """Base manager with connection resilience and user ID hashing helpers."""

    @staticmethod
    def hash_user_id(user_id) -> str:
        """Hash a raw Discord user ID for storage or lookup."""
        return _hash_user_id(user_id)

    @staticmethod
    def econ_hash_user_id(user_id) -> str:
        """Hash a raw Discord user ID for the economy scope this task is in.

        Scope 0 (the default, and every background task) is the plain user hash,
        so servers on the shared economy behave exactly as before. A server with
        its own economy runs its commands under that scope, so its economy rows
        are keyed separately and stay isolated - and because the scope is part
        of the key, the same user keeps a different balance per server.
        """
        from utils.core.economy_scope import current_scope
        from utils.security import scoped_user_hash

        return scoped_user_hash(user_id, current_scope())

    async def ensure_user_identity_hash(self, user_hash: str, user_id: int) -> None:
        """Record an explicit ``user_hash -> user_id`` mapping.

        :meth:`ensure_user_identity` derives the hash itself, so it can't record
        a scope-qualified key - and ``user_identities.user_id`` is unique, so
        scoped keys can't share that table anyway. Call this when a scoped
        economy row is created so balance/history views can still name the user.
        """
        async with self.async_sessionmaker() as session:
            async with session.begin():
                await session.execute(
                    pg_insert(EconomyIdentity)
                    .values(user_hash=str(user_hash), user_id=int(user_id))
                    .on_conflict_do_nothing(index_elements=["user_hash"])
                )

    async def resolve_user_hash(self, user_hash: str) -> int | None:
        """Look up the raw Discord ID for a stored hash, if it exists.

        Checks the ordinary identity table first, then the scope-qualified
        economy one, so a server's own economy can still name its users.
        """
        if user_hash is None:
            return None
        async with self.async_sessionmaker() as session:
            row = await session.get(UserIdentity, user_hash)
            if row is not None:
                return row.user_id
            row = await session.get(EconomyIdentity, user_hash)
            return row.user_id if row else None

    async def resolve_user_hashes(
        self, user_hashes: list[str]
    ) -> dict[str, int | None]:
        """Batch-resolve hashes to raw Discord IDs."""
        if not user_hashes:
            return {}
        async with self.async_sessionmaker() as session:
            rows = await session.execute(
                select(UserIdentity).where(UserIdentity.user_hash.in_(user_hashes))
            )
            mapping = {r.user_hash: r.user_id for r in rows.scalars().all()}
            missing = [h for h in user_hashes if h not in mapping]
            if missing:
                rows = await session.execute(
                    select(EconomyIdentity).where(
                        EconomyIdentity.user_hash.in_(missing)
                    )
                )
                mapping.update(
                    {r.user_hash: r.user_id for r in rows.scalars().all()}
                )
        return {h: mapping.get(h) for h in user_hashes}

    async def ensure_user_identity(self, user_id: int) -> str:
        """Hash a user and make sure the raw ID is recorded in user_identities.

        Call this whenever the bot sees a Discord user for the first time in a
        session, so later resolve_user_hash() calls can recover the raw ID.

        This runs for every message the bot receives, but the insert is
        idempotent, so the IDs already known to this process are remembered and
        skipped. The memo is bounded; an evicted user simply writes the row
        again on their next message, which is harmless.
        """
        try:
            cache_key = int(user_id)
        except (TypeError, ValueError):
            cache_key = user_id

        cached = self._known_identities.get(cache_key)
        if cached is not None:
            self._known_identities.move_to_end(cache_key)
            return cached

        user_hash = _hash_user_id(user_id)
        async with self.async_sessionmaker() as session:
            async with session.begin():
                await session.execute(
                    pg_insert(UserIdentity)
                    .values(user_hash=user_hash, user_id=user_id)
                    .on_conflict_do_nothing(index_elements=["user_hash"])
                )
        self._remember_identity(cache_key, user_hash)
        return user_hash

    def _remember_identity(self, cache_key, user_hash: str) -> None:
        """Memoise a known ``user_id -> user_hash`` pair, keeping the memo bounded."""
        self._known_identities[cache_key] = user_hash
        self._known_identities.move_to_end(cache_key)
        while len(self._known_identities) > self._known_identities_limit:
            self._known_identities.popitem(last=False)

    # -- hot-path cache primitives -----------------------------------------
    def _cache_get(self, key):
        """Return a cached value, or :data:`_CACHE_MISS` when absent/expired."""
        entry = self._hot_cache.get(key)
        if entry is None:
            return _CACHE_MISS
        expires, value = entry
        if expires <= time.monotonic():
            self._hot_cache.pop(key, None)
            return _CACHE_MISS
        return value

    def _cache_set(self, key, value) -> None:
        if len(self._hot_cache) >= self._hot_cache_max_entries:
            # Coarse but cheap: every entry is rebuildable from one query.
            self._hot_cache.clear()
        self._hot_cache[key] = (time.monotonic() + self._hot_cache_ttl, value)

    def _cache_invalidate(self, prefix: str) -> None:
        """Drop every cached value stored under *prefix* (the key's first item)."""
        for key in [k for k in self._hot_cache if k[0] == prefix]:
            self._hot_cache.pop(key, None)

    async def _command_status_snapshot(self) -> dict[tuple, bool]:
        """All explicit command-status overrides, indexed by scope.

        Only admins create these rows, so the table is small and a single read
        can answer every per-channel/per-guild/global lookup.
        """
        now = time.monotonic()
        if self._command_status_rows is not None and now < self._command_status_expires:
            return self._command_status_rows
        async with self._snapshot_lock:
            now = time.monotonic()
            if (
                self._command_status_rows is not None
                and now < self._command_status_expires
            ):
                return self._command_status_rows
            generation = self._command_status_generation
            async with self.async_sessionmaker() as session:
                rows = await session.execute(
                    select(
                        CommandStatus.command_name,
                        CommandStatus.channel_id,
                        CommandStatus.guild_id,
                        CommandStatus.enabled,
                    ).order_by(CommandStatus.id)
                )
                # Old databases can hold more than one row for a scope (the
                # write used to compare NULL with ``=`` and always insert).
                # The newest row is the admin's latest intent, so it wins.
                snapshot: dict[tuple, bool] = {}
                for command_name, channel_id, guild_id, enabled in rows.all():
                    snapshot[(command_name, channel_id, guild_id)] = bool(enabled)
            if generation == self._command_status_generation:
                self._command_status_rows = snapshot
                self._command_status_expires = time.monotonic() + self._hot_cache_ttl
            return snapshot

    def _invalidate_command_status(self, command_name: str | None = None) -> None:
        """Drop cached command statuses, for one command or all of them."""
        self._command_status_generation += 1
        self._command_status_expires = 0.0
        if command_name is None or self._command_status_rows is None:
            self._command_status_rows = None
            return
        for key in [k for k in self._command_status_rows if k[0] == command_name]:
            self._command_status_rows.pop(key, None)

    async def _role_restriction_snapshot(self) -> dict[tuple, frozenset[int]]:
        """All command role restrictions, indexed by ``(guild_id, command)``."""
        now = time.monotonic()
        if (
            self._role_restriction_rows is not None
            and now < self._role_restriction_expires
        ):
            return self._role_restriction_rows
        async with self._snapshot_lock:
            now = time.monotonic()
            if (
                self._role_restriction_rows is not None
                and now < self._role_restriction_expires
            ):
                return self._role_restriction_rows
            generation = self._role_restriction_generation
            async with self.async_sessionmaker() as session:
                rows = await session.execute(
                    select(
                        CommandRoleRestriction.guild_id,
                        CommandRoleRestriction.command_name,
                        CommandRoleRestriction.role_id,
                    )
                )
                grouped: dict[tuple, set[int]] = {}
                for guild_id, command_name, role_id in rows.all():
                    grouped.setdefault((guild_id, command_name), set()).add(role_id)
                snapshot = {key: frozenset(value) for key, value in grouped.items()}
            if generation == self._role_restriction_generation:
                self._role_restriction_rows = snapshot
                self._role_restriction_expires = (
                    time.monotonic() + self._hot_cache_ttl
                )
            return snapshot

    def _invalidate_role_restrictions(self) -> None:
        self._role_restriction_generation += 1
        self._role_restriction_expires = 0.0
        self._role_restriction_rows = None

    def __init__(self, database_url: str):
        # pool_pre_ping validates connections before checkout, which eliminates
        # most "connection closed unexpectedly" errors. pool_recycle forces old
        # connections to be replaced, and the connect_args set a sane busy
        # timeout. This is a local SQLite database, so no asyncpg server settings
        # or oversized pool are needed.
        self.engine = create_async_engine(
            database_url,
            echo=False,
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args={"timeout": 30},
        )
        # SQLite needs WAL mode and a busy timeout so concurrent reads and writes
        # do not raise "database is locked", and foreign keys are off by default.
        if self.engine.dialect.name == "sqlite":

            @event.listens_for(self.engine.sync_engine, "connect")
            def _sqlite_pragmas(dbapi_connection, _connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.close()
        self.async_sessionmaker = sessionmaker(
            bind=self.engine, class_=AsyncSession, expire_on_commit=False
        )
        self._latest_metrics = {}
        self._db_retry_count = 5
        self._db_retry_base_delay = 1.0
        self._db_retry_max_delay = 30.0
        # Circuit breaker: after a retryable failure, treat the DB as down for a
        # cooldown so event handlers and commands fail fast instead of piling
        # up connection attempts that can freeze the asyncio event loop.
        self._db_healthy = True
        self._db_last_failure = 0.0
        self._db_failure_cooldown = 30.0

        # ------------------------------------------------------------------
        # Hot-path caches.
        #
        # Every prefix command used to make eight sequential reads before its
        # callback ran (blacklist, three command-status scopes, one restriction
        # query per alias), and discord.py resolved the guild prefix with
        # another read on *every* message in every guild. These values are
        # admin settings that change rarely, so they are mirrored in memory and
        # invalidated by the writes that change them. A 5 minute TTL is only a
        # safety net for a write path that might be missed; the caches are
        # otherwise refreshed the moment an admin toggles them.
        # ------------------------------------------------------------------
        self._hot_cache: dict[tuple, tuple[float, object]] = {}
        self._hot_cache_ttl = 300.0
        self._hot_cache_max_entries = 20_000

        #: Whole-table snapshots: only rows that were explicitly overridden are
        #: stored, so these stay small, and one query answers every lookup.
        self._command_status_rows: dict[tuple, bool] | None = None
        self._command_status_expires = 0.0
        self._command_status_generation = 0
        self._role_restriction_rows: dict[tuple, frozenset[int]] | None = None
        self._role_restriction_expires = 0.0
        self._role_restriction_generation = 0
        self._snapshot_lock = asyncio.Lock()

        #: User IDs whose identity row is known to exist, so the idempotent
        #: upsert that ran for every message becomes a one-off per user.
        self._known_identities: OrderedDict[int, str] = OrderedDict()
        self._known_identities_limit = 50_000

    @staticmethod
    def _is_retryable_db_error(exc: Exception) -> bool:
        """Return True if *exc* looks like a transient connection failure."""
        if isinstance(exc, _RETRYABLE_DB_ERRORS):
            return True
        msg = str(exc).lower()
        markers = (
            "connection",
            "connect",
            "closed",
            "reset",
            "refused",
            "timeout",
            "broken pipe",
            "network",
            "08",  # SQLState connection exception class
            "cannot connect",
            "unexpectedly closed",
            "peer closed",
        )
        return any(marker in msg for marker in markers)

    async def db_ready(self) -> bool:
        """Return True if the database is believed reachable right now.

        After a retryable failure this returns False for a cooldown period.  A
        cheap health check is attempted once per cooldown to re-close the breaker.
        """
        if self._db_healthy:
            return True
        elapsed = time.monotonic() - self._db_last_failure
        if elapsed < self._db_failure_cooldown:
            return False
        self._db_healthy = await self.health_check()
        if not self._db_healthy:
            self._db_last_failure = time.monotonic()
        return self._db_healthy

    async def _mark_db_healthy(self) -> None:
        self._db_healthy = True
        self._db_last_failure = 0.0

    async def _mark_db_unhealthy(self) -> None:
        self._db_healthy = False
        self._db_last_failure = time.monotonic()

    async def _db_retry(
        self,
        coro,
        *args,
        retries: int = None,
        base_delay: float = None,
        **kwargs,
    ):
        """Run *coro* with exponential backoff on transient DB errors."""
        retries = retries if retries is not None else self._db_retry_count
        base_delay = base_delay if base_delay is not None else self._db_retry_base_delay
        last_exc: Optional[Exception] = None

        for attempt in range(retries):
            try:
                result = await coro(*args, **kwargs)
                if not self._db_healthy:
                    await self._mark_db_healthy()
                return result
            except Exception as exc:
                last_exc = exc
                if not self._is_retryable_db_error(exc):
                    raise
                if attempt == retries - 1:
                    break
                delay = min(base_delay * (2**attempt), self._db_retry_max_delay)
                logger.warning(
                    f"Database operation failed (attempt {attempt + 1}/{retries}): {exc}. "
                    f"Retrying in {delay:.1f}s..."
                )
                await asyncio.sleep(delay)

        logger.error(f"Database operation failed after {retries} attempts: {last_exc}")
        await self._mark_db_unhealthy()
        raise last_exc

    @asynccontextmanager
    async def retrying_session(self, retries: int = None, base_delay: float = None):
        """Yield an AsyncSession, retrying acquisition on transient errors.

        Note: this only retries getting the session/connection.  It does not
        retry work performed inside the ``async with`` block; for that, wrap the
        operation with ``@retry_db`` or ``self._db_retry``.
        """

        async def _acquire():
            return self.async_sessionmaker()

        session = await self._db_retry(_acquire, retries=retries, base_delay=base_delay)
        try:
            yield session
        finally:
            await session.close()

    async def initialize(self):
        """Create tables, retrying on transient connection failures."""

        async def _init():
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            await self.create_tables()
            await self._repair_postgres_enums()
            await self._repair_daily_user_hash_columns()
            await self._repair_command_cooldowns_constraint()
            await self._repair_item_shop_columns()
            await self._repair_game_history_columns()
            await self._repair_server_settings_columns()
            await self._repair_command_status_columns()
            await self._repair_friends_guild_scope()

        try:
            await self._db_retry(_init, retries=self._db_retry_count, base_delay=2.0)
        except SQLAlchemyError as e:
            logger.error(f"Error initializing database: {e}")

    async def _repair_postgres_enums(self):
        """
        Add any missing labels to PostgreSQL enums created by SQLAlchemy Enum
        columns. This prevents errors like 'invalid input value for enum itemtype'
        when a Python enum gains new members but the database enum was created
        by an older Base.metadata.create_all().
        """
        if self.engine.dialect.name != "postgresql":
            return

        enum_fixes = {
            "itemtype": {"collectible", "redeemable", "consumable", "defensive", "offensive"},
            "itemcategory": {
                "consumable", "defensive", "offensive", "utility",
                "cosmetic", "redeemable", "collectible",
            },
            "itemrarity": {"common", "uncommon", "rare", "epic", "legendary"},
        }

        async with self.engine.begin() as conn:
            for enum_name, expected_values in enum_fixes.items():
                try:
                    for value in expected_values:
                        # Add the canonical lowercase value...
                        await conn.execute(
                            text(
                                f"ALTER TYPE {enum_name} ADD VALUE IF NOT EXISTS '{value}'"
                            )
                        )
                        # ...and the uppercase name form. SQLAlchemy sometimes
                        # binds enum members by name depending on how the type
                        # was originally created, so accepting both avoids startup
                        # failures after enum expansion.
                        await conn.execute(
                            text(
                                f"ALTER TYPE {enum_name} ADD VALUE IF NOT EXISTS '{value.upper()}'"
                            )
                        )
                    logger.info(f"Repaired enum: {enum_name}")
                except SQLAlchemyError as e:
                    logger.warning(f"Enum repair for {enum_name} failed: {e}")

    async def _repair_daily_user_hash_columns(self):
        """
        Ensure the daily analytics tables have the user_hash column and the
        matching unique constraint that includes user_hash. Older deployments
        created these tables before user_hash existed, so Base.metadata.create_all()
        skips both the column and the constraint update.
        """
        if self.engine.dialect.name != "postgresql":
            return

        repairs = [
            {
                "table": "command_usage_daily",
                "column_ddl": "ALTER TABLE command_usage_daily ADD COLUMN IF NOT EXISTS user_hash VARCHAR(64)",
                "index_ddl": "CREATE INDEX IF NOT EXISTS ix_command_usage_daily_user_hash ON command_usage_daily(user_hash)",
                "drop_uq": "ALTER TABLE command_usage_daily DROP CONSTRAINT IF EXISTS uq_command_usage_daily",
                "create_uq": (
                    "ALTER TABLE command_usage_daily ADD CONSTRAINT uq_command_usage_daily "
                    "UNIQUE (bucket_date, command_name, guild_id, user_hash, is_slash)"
                ),
            },
            {
                "table": "command_latency_daily",
                "column_ddl": "ALTER TABLE command_latency_daily ADD COLUMN IF NOT EXISTS user_hash VARCHAR(64)",
                "index_ddl": "CREATE INDEX IF NOT EXISTS ix_command_latency_daily_user_hash ON command_latency_daily(user_hash)",
                "drop_uq": "ALTER TABLE command_latency_daily DROP CONSTRAINT IF EXISTS uq_command_latency_daily",
                "create_uq": (
                    "ALTER TABLE command_latency_daily ADD CONSTRAINT uq_command_latency_daily "
                    "UNIQUE (bucket_date, command_name, guild_id, user_hash, is_slash)"
                ),
            },
            {
                "table": "command_error_daily",
                "column_ddl": "ALTER TABLE command_error_daily ADD COLUMN IF NOT EXISTS user_hash VARCHAR(64)",
                "index_ddl": "CREATE INDEX IF NOT EXISTS ix_command_error_daily_user_hash ON command_error_daily(user_hash)",
                "drop_uq": "ALTER TABLE command_error_daily DROP CONSTRAINT IF EXISTS uq_command_error_daily",
                "create_uq": (
                    "ALTER TABLE command_error_daily ADD CONSTRAINT uq_command_error_daily "
                    "UNIQUE (bucket_date, command_name, guild_id, user_hash, is_slash, error_type)"
                ),
            },
            {
                "table": "daily_user_exposure",
                "column_ddl": "ALTER TABLE daily_user_exposure ADD COLUMN IF NOT EXISTS user_hash VARCHAR(64)",
                "index_ddl": "CREATE INDEX IF NOT EXISTS ix_daily_user_exposure_user_hash ON daily_user_exposure(user_hash)",
                "drop_uq": "ALTER TABLE daily_user_exposure DROP CONSTRAINT IF EXISTS uq_daily_user_exposure",
                "create_uq": (
                    "ALTER TABLE daily_user_exposure ADD CONSTRAINT uq_daily_user_exposure "
                    "UNIQUE (bucket_date, guild_id, user_hash)"
                ),
            },
        ]

        async with self.engine.begin() as conn:
            for repair in repairs:
                table = repair["table"]
                try:
                    await conn.execute(text(repair["column_ddl"]))
                    await conn.execute(text(repair["index_ddl"]))
                    await conn.execute(text(repair["drop_uq"]))
                    await conn.execute(text(repair["create_uq"]))
                    logger.info(f"Repaired schema for table: {table}")
                except SQLAlchemyError as e:
                    logger.warning(f"Schema repair for {table} failed (may be expected): {e}")

    async def _repair_command_cooldowns_constraint(self):
        """
        Ensure command_cooldowns has a unique index on (user_id, command_name).
        Older code updated-then-inserted without a constraint, so duplicate rows
        are possible. Keep the latest expiry per user/command and add a unique
        index so set_cooldown() can use an atomic upsert via ON CONFLICT.
        """
        if self.engine.dialect.name != "postgresql":
            return

        async with self.engine.begin() as conn:
            try:
                # 1. Keep only the latest expiry per user/command.
                await conn.execute(
                    text(
                        """
                        DELETE FROM command_cooldowns a
                        USING command_cooldowns b
                        WHERE a.id < b.id
                          AND a.user_id = b.user_id
                          AND a.command_name = b.command_name
                        """
                    )
                )

                # 2. Create a unique index. PostgreSQL supports ON CONFLICT on a
                #    unique index even when no named constraint exists.
                await conn.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS "
                        "ix_command_cooldowns_user_command "
                        "ON command_cooldowns (user_id, command_name)"
                    )
                )

                # 3. Also create the named constraint for completeness.
                await conn.execute(
                    text(
                        "ALTER TABLE command_cooldowns "
                        "DROP CONSTRAINT IF EXISTS uq_command_cooldowns_user_command"
                    )
                )
                await conn.execute(
                    text(
                        "ALTER TABLE command_cooldowns "
                        "ADD CONSTRAINT uq_command_cooldowns_user_command "
                        "UNIQUE (user_id, command_name)"
                    )
                )
                logger.info("Repaired command_cooldowns unique constraint/index")
            except SQLAlchemyError as e:
                logger.warning(f"command_cooldowns constraint repair failed: {e}")

    async def _repair_item_shop_columns(self):
        """
        Add item-shop expansion columns if they are missing on an older schema.
        New deployments get these from Base.metadata.create_all().
        """
        if self.engine.dialect.name != "postgresql":
            return


        async with self.engine.begin() as conn:
            item_columns = [
                ("category", "VARCHAR(50)"),
                ("rarity", "VARCHAR(50)"),
                ("targetable", "BOOLEAN DEFAULT FALSE"),
                ("daily_limit", "INTEGER"),
                ("global_daily_limit", "INTEGER"),
                ("tradable", "BOOLEAN DEFAULT TRUE"),
                ("durability", "INTEGER"),
                ("max_uses", "INTEGER"),
            ]
            shop_columns = [
                ("category", "VARCHAR(50)"),
                ("rarity", "VARCHAR(50)"),
                ("targetable", "BOOLEAN DEFAULT FALSE"),
                ("daily_limit", "INTEGER"),
                ("global_daily_limit", "INTEGER"),
                ("tradable", "BOOLEAN DEFAULT TRUE"),
            ]

            for column, col_type in item_columns:
                try:
                    await conn.execute(
                        text(
                            f"ALTER TABLE items ADD COLUMN IF NOT EXISTS {column} {col_type}"
                        )
                    )
                except SQLAlchemyError as e:
                    logger.warning(f"Schema repair for items.{column} failed: {e}")

            for column, col_type in shop_columns:
                try:
                    await conn.execute(
                        text(
                            f"ALTER TABLE shop_items ADD COLUMN IF NOT EXISTS {column} {col_type}"
                        )
                    )
                except SQLAlchemyError as e:
                    logger.warning(
                        f"Schema repair for shop_items.{column} failed: {e}"
                    )

            logger.info("Repaired item/shop item expansion columns")

    async def _repair_game_history_columns(self):
        """
        Add payout columns to game_history if they are missing on an older schema.
        New deployments get these from Base.metadata.create_all().
        """
        if self.engine.dialect.name != "postgresql":
            return

        async with self.engine.begin() as conn:
            history_columns = [
                ("payout_multiplier", "NUMERIC(38, 10)"),
                ("payout_amount", "NUMERIC(38, 2)"),
            ]
            for column, col_type in history_columns:
                try:
                    await conn.execute(
                        text(
                            f"ALTER TABLE game_history ADD COLUMN IF NOT EXISTS {column} {col_type}"
                        )
                    )
                except SQLAlchemyError as e:
                    logger.warning(f"Schema repair for game_history.{column} failed: {e}")
            logger.info("Repaired game_history payout columns")

    async def _repair_server_settings_columns(self):
        """
        Add the snipe_enabled column to server_settings if it is missing on an
        older schema. New deployments get it from Base.metadata.create_all().
        Snipe is off by default (opt-in per guild).
        """
        if self.engine.dialect.name != "postgresql":
            return

        async with self.engine.begin() as conn:
            settings_columns = [
                ("snipe_enabled", "BOOLEAN DEFAULT FALSE"),
                ("spam_message", "VARCHAR"),
            ]
            for column, col_type in settings_columns:
                try:
                    await conn.execute(
                        text(
                            f"ALTER TABLE server_settings ADD COLUMN IF NOT EXISTS {column} {col_type}"
                        )
                    )
                except SQLAlchemyError as e:
                    logger.warning(
                        f"Schema repair for server_settings.{column} failed: {e}"
                    )
            logger.info("Repaired server_settings expansion columns")

    async def _repair_command_status_columns(self):
        """
        Add the guild_id column to command_status if it is missing on an older
        schema. New deployments get it from Base.metadata.create_all().
        guild_id scopes a per-guild serverwide disable (channel_id NULL,
        guild_id set); NULL preserves the existing bot-wide / channel-scoped
        rows.
        """
        if self.engine.dialect.name != "postgresql":
            return

        async with self.engine.begin() as conn:
            try:
                await conn.execute(
                    text(
                        "ALTER TABLE command_status ADD COLUMN IF NOT EXISTS guild_id BIGINT"
                    )
                )
            except SQLAlchemyError as e:
                logger.warning(
                    f"Schema repair for command_status.guild_id failed: {e}"
                )
            logger.info("Repaired command_status guild_id column")

    async def _repair_friends_guild_scope(self):
        """Rebuild ``friends`` as a per-server table on the older global schema.

        The first version of the table had no guild column, so its unique
        constraint also spanned servers. The table is recreated with the same
        name and the old rows are kept under the sentinel guild ``0``, which no
        real server can match: they stay recoverable but are no longer visible
        in anyone's list.
        """
        try:
            async with self.engine.begin() as conn:
                columns = await conn.run_sync(
                    lambda sync_conn: {
                        column["name"]
                        for column in inspect(sync_conn).get_columns("friends")
                    }
                )
        except SQLAlchemyError as e:
            logger.debug(f"Could not inspect friends table: {e}")
            return

        if "guild_id" in columns:
            return

        try:
            async with self.engine.begin() as conn:
                await conn.execute(text("ALTER TABLE friends RENAME TO friends_legacy"))
                await conn.run_sync(Base.metadata.create_all)
                await conn.execute(
                    text(
                        "INSERT INTO friends (guild_id, user_id, friend_id, created_at) "
                        "SELECT 0, user_id, friend_id, created_at FROM friends_legacy"
                    )
                )
                await conn.execute(text("DROP TABLE friends_legacy"))
            logger.info("Rebuilt friends table with per-server scoping")
        except SQLAlchemyError as e:
            logger.warning(f"Schema repair for friends.guild_id failed: {e}")

    def get_session(self):
        """Provide a transactional scope around a series of operations."""
        return self.async_sessionmaker()

    async def create_tables(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def health_check(self) -> bool:
        """Return True if the database is reachable right now."""
        try:
            async with self.async_sessionmaker() as session:
                await session.execute(text("SELECT 1"))
            return True
        except Exception as exc:
            logger.warning(f"Database health check failed: {exc}")
            return False
