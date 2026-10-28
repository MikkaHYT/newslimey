import sqlite3
from pathlib import Path
from typing import Any


class VoiceDBError(Exception):
    """Raised by voice.cog DB helpers when voice.db is unavailable or broken."""


VOICE_DB_PATH = "voice.db"

VOICE_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS guild (
    guildID INTEGER PRIMARY KEY,
    ownerID INTEGER,
    voiceChannelID INTEGER,
    voiceCategoryID INTEGER
);

CREATE TABLE IF NOT EXISTS voiceChannel (
    userID INTEGER PRIMARY KEY,
    voiceID INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS guildSettings (
    guildID INTEGER PRIMARY KEY,
    channelName TEXT NOT NULL,
    channelLimit INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS userSettings (
    userID INTEGER PRIMARY KEY,
    channelName TEXT NOT NULL,
    channelLimit INTEGER NOT NULL DEFAULT 0
);
"""


def _ensure_voice_tables(logger: Any) -> None:
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(VOICE_DB_PATH)
        try:
            conn.executescript(VOICE_DB_SCHEMA)
            conn.commit()
        except Exception:
            _log_exception(logger, "[voice.db] Failed to initialize voice schema")
    except Exception:
        _log_exception(
            logger,
            "[voice.db] Failed to open voice database at %s",
            VOICE_DB_PATH,
        )
    finally:
        if conn is not None:
            conn.close()



def _ensure_voice_tables_on_connection(conn: sqlite3.Connection, logger: Any) -> None:
    # Ping the connection first so we fail fast if it was never opened or is broken.
    conn.execute("SELECT 1")
    try:
        conn.executescript(VOICE_DB_SCHEMA)
        conn.commit()
    except Exception:
        _log_exception(logger, "[voice.db] Failed to initialize voice schema via existing connection")


def _voice_tables_exist(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='guild'"
    ).fetchone()
    return row is not None


def _voice_db_path_exists() -> bool:
    try:
        return Path(VOICE_DB_PATH).is_file()
    except Exception:
        return False


def _get_voice_connection() -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(VOICE_DB_PATH)
    except Exception as exc:
        raise VoiceDBError(
            f"voice.db is unavailable or unreadable: {exc}"
        ) from exc

    conn.row_factory = sqlite3.Row
    return conn


def _log_exception(logger: Any, message: str, *args: Any) -> None:
    if hasattr(logger, "exception"):
        logger.exception(message, *args)
    else:
        print(message % args if args else message)
