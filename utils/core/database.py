from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    note TEXT NOT NULL,
    note_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id, note_id)
);

CREATE TABLE IF NOT EXISTS taken_roles (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id, role_id)
);

CREATE TABLE IF NOT EXISTS temporary_bans (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    unban_on TEXT NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS hard_banned (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS moderation_history (
    guild_id INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    member_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    reason TEXT,
    created_on TEXT NOT NULL,
    case_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, case_id)
);

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    mute_role_id INTEGER,
    jail_role_id INTEGER,
    jail_channel_id INTEGER
);

CREATE TABLE IF NOT EXISTS muted_user (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    unmute_on TEXT,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS jailed_user (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    unjail_on TEXT,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS sticky_roles (
    guild_id INTEGER NOT NULL,
    role_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, role_id)
);

CREATE TABLE IF NOT EXISTS welcome_settings (
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);

CREATE TABLE IF NOT EXISTS leave_settings (
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);

CREATE TABLE IF NOT EXISTS boost_settings (
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);

CREATE TABLE IF NOT EXISTS unboost_settings (
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);

-- Dashboard-managed per-guild key/value settings (prefix, economy
-- overrides, moderation config, ...). Values are JSON-encoded strings.
CREATE TABLE IF NOT EXISTS bot_settings (
    guild_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (guild_id, key)
);

-- Welcome message content built with ,welcomemessage (channel lives in welcome_settings).
CREATE TABLE IF NOT EXISTS welcome_config (
    guild_id INTEGER PRIMARY KEY,
    title TEXT DEFAULT '',
    description TEXT DEFAULT '',
    footer TEXT DEFAULT '',
    color INTEGER DEFAULT 0,
    image_url TEXT,
    thumbnail_url TEXT,
    ping INTEGER DEFAULT 1,
    enabled INTEGER DEFAULT 1
);

-- Personal roles that server boosters build with ,boosterrole.
CREATE TABLE IF NOT EXISTS booster_roles (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

-- Members a moderator has granted booster-role access to with ,br allow.
CREATE TABLE IF NOT EXISTS booster_role_access (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

-- Message XP tracked by ,rank. XP is per server: a member has one progress
-- row for every guild they earn in, so gaining a level here never moves them
-- up a level somewhere else.
CREATE TABLE IF NOT EXISTS user_levels (
    guild_id INTEGER NOT NULL DEFAULT 0,
    user_id INTEGER NOT NULL,
    level INTEGER NOT NULL DEFAULT 1,
    xp INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

-- Lifetime activity counters shown on the ,rank card (per server too).
CREATE TABLE IF NOT EXISTS level_stats (
    guild_id INTEGER NOT NULL DEFAULT 0,
    user_id INTEGER NOT NULL,
    messages INTEGER NOT NULL DEFAULT 0,
    voice_seconds INTEGER NOT NULL DEFAULT 0,
    last_xp_at TEXT,
    PRIMARY KEY (guild_id, user_id)
);

-- One wallet/bank account per user, shared by the economy, level and gambling
-- cogs. ``daily_claimed_at`` drives the ,daily streak.
CREATE TABLE IF NOT EXISTS economy_accounts (
    user_id INTEGER PRIMARY KEY,
    wallet INTEGER NOT NULL DEFAULT 0,
    bank INTEGER NOT NULL DEFAULT 0,
    daily_streak INTEGER NOT NULL DEFAULT 0,
    daily_claimed_at TEXT,
    lifetime_earned INTEGER NOT NULL DEFAULT 0,
    lifetime_spent INTEGER NOT NULL DEFAULT 0
);

-- Append-only ledger backing the "recent activity" list on ,balance.
CREATE TABLE IF NOT EXISTS economy_transactions (
    transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    description TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Per-user casino record shown by ,casino stats.
CREATE TABLE IF NOT EXISTS gambling_stats (
    user_id INTEGER PRIMARY KEY,
    games INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    draws INTEGER NOT NULL DEFAULT 0,
    wagered INTEGER NOT NULL DEFAULT 0,
    won INTEGER NOT NULL DEFAULT 0,
    lost INTEGER NOT NULL DEFAULT 0,
    biggest_win INTEGER NOT NULL DEFAULT 0
);

-- Repeatable timed rewards (,weekly, ,monthly) with their own streaks.
-- ``claim`` is the command name, so adding another one needs no migration.
CREATE TABLE IF NOT EXISTS economy_claims (
    user_id INTEGER NOT NULL,
    claim TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    streak INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, claim)
);

-- Items owned from ,shop. ``item_key`` matches a key in ``config.SHOP_ITEMS``.
CREATE TABLE IF NOT EXISTS economy_inventory (
    user_id INTEGER NOT NULL,
    item_key TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, item_key)
);

-- The job a member signed up for with ,job apply.
CREATE TABLE IF NOT EXISTS economy_jobs (
    user_id INTEGER PRIMARY KEY,
    job_key TEXT NOT NULL,
    hired_at TEXT NOT NULL,
    shifts INTEGER NOT NULL DEFAULT 0
);

-- Achievements already unlocked, so the reward is only ever paid once.
CREATE TABLE IF NOT EXISTS economy_achievements (
    user_id INTEGER NOT NULL,
    achievement_key TEXT NOT NULL,
    unlocked_at TEXT NOT NULL,
    PRIMARY KEY (user_id, achievement_key)
);

-- Per-game casino records behind ,gamestats and ,gametop.
CREATE TABLE IF NOT EXISTS gambling_games (
    user_id INTEGER NOT NULL,
    game TEXT NOT NULL,
    games INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    wagered INTEGER NOT NULL DEFAULT 0,
    net INTEGER NOT NULL DEFAULT 0,
    biggest_win INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, game)
);

-- Roles handed out for reaching a level, per guild. Managed with
-- ,admin rolelevel. A level may grant several roles at once; `mode` in
-- level_role_settings decides whether lower tiers are swapped out or kept.
CREATE TABLE IF NOT EXISTS level_roles (
    guild_id INTEGER NOT NULL,
    level INTEGER NOT NULL,
    role_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, level, role_id)
);

-- How the level-role ladder behaves, per guild. Managed with ,admin rolesettings.
CREATE TABLE IF NOT EXISTS level_role_settings (
    guild_id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL DEFAULT 'replace',
    strip_stale INTEGER NOT NULL DEFAULT 1,
    announce INTEGER NOT NULL DEFAULT 1,
    dm INTEGER NOT NULL DEFAULT 0
);

-- Per-member preferences the member toggles themselves (level-up popups,
-- ...). Values are JSON-encoded strings; managed through utils.core.userprefs.
CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (user_id, key)
);

-- Ready pings behind the notification preferences: the moment a member can
-- claim or work again, so the reminder loop knows who to DM. Written when the
-- command runs and cleared once the DM has gone out.
CREATE TABLE IF NOT EXISTS user_reminders (
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    ready_at TEXT NOT NULL,
    PRIMARY KEY (user_id, kind)
);

-- Warnings handed out by ,warn, shown by ,warnings.
CREATE TABLE IF NOT EXISTS user_warnings (
    warning_id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL
);

-- AFK status set with ,afk: who is away, why, and since when. ``timestamp``
-- is epoch seconds, so "away for 12 minutes" is a subtraction.
CREATE TABLE IF NOT EXISTS afk_users (
    user_id INTEGER PRIMARY KEY,
    reason TEXT,
    timestamp REAL
);

-- Reputation handed out to other members with ,rep good / ,rep bad.
-- One row per hand-out, so the score is the sum of the deltas and the history
-- comes straight off the table.
CREATE TABLE IF NOT EXISTS reputation (
    rep_id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    giver_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    delta INTEGER NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL
);

-- Buttons posted by the createembed builder, so their custom_ids keep working
-- after a bot restart (link buttons don't need persisting).
CREATE TABLE IF NOT EXISTS embed_buttons (
    custom_id TEXT PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER,
    message_id INTEGER NOT NULL,
    style TEXT NOT NULL,
    label TEXT NOT NULL,
    emoji TEXT,
    response TEXT,
    role_id INTEGER,
    image_prompt TEXT
);

-- Reaction roles applied to a message posted by the reaction-role builder.
-- Adding the reaction grants the role, removing it takes the role back.
CREATE TABLE IF NOT EXISTS reaction_roles (
    message_id INTEGER NOT NULL,
    emoji TEXT NOT NULL,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    role_id INTEGER NOT NULL,
    PRIMARY KEY (message_id, emoji)
);

-- Last.fm integration. Scrobbles are stored once per user (not per server) and
-- every server-scoped command filters by that server's linked members. Rows
-- arrive two ways: polled from Last.fm's recent tracks, and captured live from
-- a member's Spotify/Apple Music presence. The source column says which.
CREATE TABLE IF NOT EXISTS lastfm_scrobbles (
    user_id INTEGER NOT NULL,
    played_at INTEGER NOT NULL,
    artist TEXT NOT NULL,
    artist_key TEXT NOT NULL,
    track TEXT NOT NULL,
    track_key TEXT NOT NULL,
    album TEXT,
    album_key TEXT,
    source TEXT NOT NULL,
    PRIMARY KEY (user_id, played_at, artist_key, track_key)
);
CREATE INDEX IF NOT EXISTS idx_lastfm_scrobbles_user
    ON lastfm_scrobbles (user_id, played_at);
CREATE INDEX IF NOT EXISTS idx_lastfm_scrobbles_artist
    ON lastfm_scrobbles (artist_key);
CREATE INDEX IF NOT EXISTS idx_lastfm_scrobbles_track
    ON lastfm_scrobbles (artist_key, track_key);

-- Mutual friendships between members, global across every server the bot is in.
-- One row per pair; ``status`` flips from ``pending`` to ``accepted``.
CREATE TABLE IF NOT EXISTS lastfm_friends (
    requester_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (requester_id, target_id)
);

-- Last.fm OAuth session keys, for the scrobbling commands.
CREATE TABLE IF NOT EXISTS lastfm_auth (
    user_id INTEGER PRIMARY KEY,
    session_key TEXT NOT NULL,
    username TEXT,
    linked_at INTEGER NOT NULL
);

-- Global Last.fm stats per artist, cached so the iceberg doesn't re-ask the
-- API for the same names on every run.
CREATE TABLE IF NOT EXISTS lastfm_artist_cache (
    artist_key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    listeners INTEGER NOT NULL DEFAULT 0,
    playcount INTEGER NOT NULL DEFAULT 0,
    fetched_at INTEGER NOT NULL DEFAULT 0
);

-- Per-member fight record behind ,fighttop. A draw counts for both fighters,
-- so wins + losses + draws is every fight a member has been in. The metres
-- keep no records of their own, so this table stands alone.
CREATE TABLE IF NOT EXISTS fight_stats (
    user_id INTEGER PRIMARY KEY,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    draws INTEGER NOT NULL DEFAULT 0
);

-- Per-member, per-day, per-channel activity behind ,profilestats. One row is
-- upserted per message and per voice minute, so a single table answers both
-- the daily windows (1d/7d/14d) and the top-channels panel. ``day`` is the
-- UTC date, which is what the card's "Timezone: UTC" line promises.
CREATE TABLE IF NOT EXISTS activity_events (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    amount INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, day, channel_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_activity_events_lookup
    ON activity_events (guild_id, day, kind);
"""


def _parse_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value

    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return value


def _normalize_row(row: tuple[Any, ...]) -> tuple[Any, ...]:
    return tuple(_parse_value(value) for value in row)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        await self._apply_pragmas()
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._conn.commit()

    async def _apply_pragmas(self) -> None:
        """Put this connection in WAL mode with a busy timeout.

        The main bot database sets both on every connection it opens; this
        second file needs them too. Without a busy timeout the driver gives
        up after five seconds with "database is locked" the moment another
        writer is mid-transaction, and in rollback-journal mode a reader
        blocks every writer as well.
        """
        assert self._conn is not None
        await self._conn.execute("PRAGMA busy_timeout=30000")
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.commit()

    async def _migrate(self) -> None:
        """Bring an existing database up to the current schema.

        ``SCHEMA`` is all ``IF NOT EXISTS``, so it creates missing tables but
        never changes one that already exists. The level-role table used to
        allow a single role per level (primary key ``guild_id, level``); it now
        needs ``guild_id, level, role_id`` so a level can grant several. Rebuild
        it in place when the old shape is detected.

        The fun-metre table from when a metre was rolled once and stored forever
        is not rebuilt - nothing reads it any more, so it is dropped outright.
        """
        assert self._conn is not None

        await self._conn.execute("DROP TABLE IF EXISTS fun_stats")
        await self._migrate_level_guild_scope()
        await self._migrate_embed_buttons_image_prompt()
        cursor = await self._conn.execute("PRAGMA table_info(level_roles)")
        columns = await cursor.fetchall()
        await cursor.close()
        if not columns:
            return

        # ``pk`` is the 1-based position within the primary key, and 0 for a
        # column that is not part of it. A single-role table leaves role_id at 0.
        by_name = {str(row[1]): int(row[5]) for row in columns}
        if by_name.get("role_id"):
            return

        await self._conn.executescript(
            "CREATE TABLE level_roles_v2 ("
            "    guild_id INTEGER NOT NULL,"
            "    level INTEGER NOT NULL,"
            "    role_id INTEGER NOT NULL,"
            "    PRIMARY KEY (guild_id, level, role_id)"
            ");"
            "INSERT OR IGNORE INTO level_roles_v2 (guild_id, level, role_id) "
            "    SELECT guild_id, level, role_id FROM level_roles;"
            "DROP TABLE level_roles;"
            "ALTER TABLE level_roles_v2 RENAME TO level_roles;"
        )

    async def _migrate_level_guild_scope(self) -> None:
        """Rebuild ``user_levels`` / ``level_stats`` when XP was bot-wide.

        Both tables used to have ``user_id`` as the whole primary key, so a
        member carried one level across every server. They are per server now,
        which means ``guild_id`` has to join the key — and SQLite cannot change
        a primary key in place. Legacy rows have no guild to belong to, so they
        are preserved under ``guild_id = 0`` rather than being dropped.
        """
        assert self._conn is not None

        cursor = await self._conn.execute("PRAGMA table_info(user_levels)")
        columns = await cursor.fetchall()
        await cursor.close()
        if columns:
            names = {str(row[1]) for row in columns}
            if "guild_id" not in names:
                await self._conn.executescript(
                    "CREATE TABLE user_levels_v2 ("
                    "    guild_id INTEGER NOT NULL DEFAULT 0,"
                    "    user_id INTEGER NOT NULL,"
                    "    level INTEGER NOT NULL DEFAULT 1,"
                    "    xp INTEGER NOT NULL DEFAULT 0,"
                    "    PRIMARY KEY (guild_id, user_id)"
                    ");"
                    "INSERT OR IGNORE INTO user_levels_v2 "
                    "    (guild_id, user_id, level, xp) "
                    "    SELECT 0, user_id, level, xp FROM user_levels;"
                    "DROP TABLE user_levels;"
                    "ALTER TABLE user_levels_v2 RENAME TO user_levels;"
                )

        cursor = await self._conn.execute("PRAGMA table_info(level_stats)")
        stats_columns = await cursor.fetchall()
        await cursor.close()
        if stats_columns:
            stat_names = {str(row[1]) for row in stats_columns}
            if "guild_id" not in stat_names:
                await self._conn.executescript(
                    "CREATE TABLE level_stats_v2 ("
                    "    guild_id INTEGER NOT NULL DEFAULT 0,"
                    "    user_id INTEGER NOT NULL,"
                    "    messages INTEGER NOT NULL DEFAULT 0,"
                    "    voice_seconds INTEGER NOT NULL DEFAULT 0,"
                    "    last_xp_at TEXT,"
                    "    PRIMARY KEY (guild_id, user_id)"
                    ");"
                    "INSERT OR IGNORE INTO level_stats_v2 "
                    "    (guild_id, user_id, messages, voice_seconds, last_xp_at) "
                    "    SELECT 0, user_id, messages, voice_seconds, last_xp_at "
                    "    FROM level_stats;"
                    "DROP TABLE level_stats;"
                    "ALTER TABLE level_stats_v2 RENAME TO level_stats;"
                )

    async def _migrate_embed_buttons_image_prompt(self) -> None:
        """Add ``image_prompt`` to ``embed_buttons`` on pre-existing databases.

        Image-generating buttons keep their prompt in this column so the button
        still works after a restart. New databases get it straight from
        ``SCHEMA``; an older one only needs the column added.
        """
        assert self._conn is not None

        cursor = await self._conn.execute("PRAGMA table_info(embed_buttons)")
        columns = await cursor.fetchall()
        await cursor.close()
        if not columns:
            return
        names = {str(row[1]) for row in columns}
        if "image_prompt" not in names:
            await self._conn.execute(
                "ALTER TABLE embed_buttons ADD COLUMN image_prompt TEXT"
            )

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _convert(self, statement: str) -> str:
        statement = statement.replace(
            "ON DUPLICATE KEY UPDATE mute_role_id = VALUES(mute_role_id), jail_role_id = VALUES(jail_role_id), jail_channel_id = VALUES(jail_channel_id)",
            (
                "ON CONFLICT(guild_id) DO UPDATE SET "
                "mute_role_id = excluded.mute_role_id, "
                "jail_role_id = excluded.jail_role_id, "
                "jail_channel_id = excluded.jail_channel_id"
            ),
        )
        return statement.replace("%s", "?")

    def _prepare_params(self, params: tuple[Any, ...]) -> tuple[Any, ...]:
        prepared = []
        for value in params:
            if isinstance(value, datetime):
                prepared.append(value.isoformat())
            else:
                prepared.append(value)
        return tuple(prepared)

    async def execute(
        self,
        statement: str,
        *params: Any,
        one_row: bool = False,
        one_value: bool = False,
        as_list: bool = False,
    ) -> Any:
        if self._conn is None:
            raise RuntimeError("Database is not connected")

        statement = self._convert(statement).strip()
        params = self._prepare_params(params)

        if ";" in statement and not statement.upper().startswith("SELECT"):
            parts = [part.strip() for part in statement.split(";") if part.strip()]
            if len(parts) > 1:
                offset = 0
                for part in parts:
                    count = part.count("?")
                    chunk = params[offset : offset + count]
                    offset += count
                    await self._conn.execute(part, chunk)
                await self._conn.commit()
                return ()

        cursor = await self._conn.execute(statement, params)

        if statement.upper().startswith(("INSERT", "UPDATE", "DELETE")):
            await self._conn.commit()
            return ()

        rows = await cursor.fetchall()
        if not rows:
            return ()

        rows = [_normalize_row(row) for row in rows]

        if one_value:
            return rows[0][0]

        if one_row:
            return rows[0]

        if as_list:
            return tuple(row[0] for row in rows)

        return rows

    async def fetchrow(self, statement: str, *params: Any) -> tuple[Any, ...] | None:
        result = await self.execute(statement, *params, one_row=True)
        return result if result else None

    async def fetchval(self, statement: str, *params: Any) -> Any:
        return await self.execute(statement, *params, one_value=True)

    async def fetch(self, statement: str, *params: Any) -> tuple[Any, ...]:
        result = await self.execute(statement, *params, as_list=True)
        return result if result else ()
