"""Local storage for the Last.fm integration.

The scrobbles here are the bot's own copy: rows are recorded once per member
(polled from Last.fm's recent tracks and captured from listening presences) and
every server-scoped command filters them by that server's linked members. That
keeps a single source of truth instead of duplicating a member's history into
each server.

Names are matched on a normalised ``key`` column (lowercased, whitespace
collapsed) so ``Juice WRLD`` and ``juice  wrld`` count together.

Everything in here takes the bot's :class:`~utils.core.database.Database` and
uses ``%s`` placeholders, which that layer rewrites to SQLite's ``?``.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Sequence

#: Where a scrobble came from.
SOURCE_LASTFM = "lastfm"
SOURCE_PRESENCE = "presence"


def key(value: Any) -> str:
    """The normalised matching key for an artist/album/track name."""
    return " ".join(str(value or "").lower().split())


def _placeholders(count: int) -> str:
    return ", ".join(["%s"] * count)


def _chunks(values: Sequence[Any], size: int = 500) -> Iterable[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


# ---------------------------------------------------------------------------
# Scrobbles
# ---------------------------------------------------------------------------
async def record_scrobble(
    db: Any,
    user_id: int,
    played_at: int,
    artist: str,
    track: str,
    album: str = "",
    *,
    source: str = SOURCE_LASTFM,
) -> None:
    """Store one scrobble. Duplicates are ignored, so re-polling is safe."""
    artist = str(artist or "").strip()
    track = str(track or "").strip()
    if not artist or not track:
        return
    await db.execute(
        "INSERT OR IGNORE INTO lastfm_scrobbles "
        "(user_id, played_at, artist, artist_key, track, track_key, album, album_key, source) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        int(user_id),
        int(played_at),
        artist,
        key(artist),
        track,
        key(track),
        str(album or "").strip(),
        key(album or ""),
        source,
    )


async def record_many(db: Any, rows: Iterable[tuple[Any, ...]]) -> int:
    """Store a batch of ``(user_id, played_at, artist, track, album, source)``."""
    stored = 0
    for user_id, played_at, artist, track, album, source in rows:
        await record_scrobble(
            db, user_id, played_at, artist, track, album or "", source=source
        )
        stored += 1
    return stored


async def latest_played_at(db: Any, user_id: int) -> int:
    value = await db.fetchval(
        "SELECT MAX(played_at) FROM lastfm_scrobbles WHERE user_id = %s",
        int(user_id),
    )
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


async def total_scrobbles(db: Any, user_id: int) -> int:
    value = await db.fetchval(
        "SELECT COUNT(*) FROM lastfm_scrobbles WHERE user_id = %s", int(user_id)
    )
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


async def scrobble_totals(db: Any, user_ids: Sequence[int]) -> dict[int, int]:
    """``{user_id: scrobbles}`` for the given members."""
    ids = [int(uid) for uid in user_ids]
    if not ids:
        return {}
    totals: dict[int, int] = {}
    for chunk in _chunks(ids):
        rows = await db.execute(
            f"SELECT user_id, COUNT(*) FROM lastfm_scrobbles "
            f"WHERE user_id IN ({_placeholders(len(chunk))}) GROUP BY user_id",
            *chunk,
        )
        for user_id, count in rows:
            totals[int(user_id)] = int(count)
    return totals


async def nth_scrobble(db: Any, user_id: int, n: int) -> tuple[Any, ...] | None:
    """The ``n``-th scrobble of a member (1-based), oldest first."""
    if n < 1:
        return None
    return await db.fetchrow(
        "SELECT played_at, artist, track, album, source FROM lastfm_scrobbles "
        "WHERE user_id = %s ORDER BY played_at ASC LIMIT 1 OFFSET %s",
        int(user_id),
        int(n - 1),
    )


async def recent_scrobbles(db: Any, user_id: int, limit: int = 10) -> list[tuple[Any, ...]]:
    rows = await db.execute(
        "SELECT played_at, artist, track, album, source FROM lastfm_scrobbles "
        "WHERE user_id = %s ORDER BY played_at DESC LIMIT %s",
        int(user_id),
        int(limit),
    )
    return list(rows)


# ---------------------------------------------------------------------------
# Ranking across a set of members
# ---------------------------------------------------------------------------
async def artist_plays_by_user(
    db: Any, user_ids: Sequence[int], artist_key: str
) -> list[tuple[int, int]]:
    """``(user_id, plays)`` for one artist, most plays first."""
    ids = [int(uid) for uid in user_ids]
    if not ids:
        return []
    rows = await db.execute(
        f"SELECT user_id, COUNT(*) FROM lastfm_scrobbles "
        f"WHERE artist_key = %s AND user_id IN ({_placeholders(len(ids))}) "
        f"GROUP BY user_id ORDER BY COUNT(*) DESC",
        key(artist_key),
        *ids,
    )
    return [(int(uid), int(count)) for uid, count in rows]


async def track_crowns_by_user(
    db: Any, user_ids: Sequence[int], *, min_contenders: int = 2
) -> tuple[list[tuple[int, int]], int, int]:
    """Who holds the most track crowns across a set of members.

    A crown is the most plays for one ``(artist, track)``. Returns the ranking
    as ``(user_id, crowns)``, how many tracks were crowned, and how many
    distinct tracks the members scrobbled at all.

    ``min_contenders`` is how many members must have scrobbled a track before
    it can be won: ``2`` makes every crown a track someone actually beat another
    member on, while ``1`` also counts tracks a single member has to themselves.
    Two members level at the top share the crown, so both are counted.
    """
    ids = [int(uid) for uid in user_ids]
    if not ids:
        return [], 0, 0

    rows = await db.execute(
        f"SELECT user_id, artist_key, track_key, COUNT(*) FROM lastfm_scrobbles "
        f"WHERE user_id IN ({_placeholders(len(ids))}) "
        f"GROUP BY user_id, artist_key, track_key",
        *ids,
    )

    best: dict[tuple[str, str], tuple[int, list[int]]] = {}
    contenders: dict[tuple[str, str], int] = {}
    for user_id, artist_key, track_key, plays in rows:
        spot = (str(artist_key), str(track_key))
        contenders[spot] = contenders.get(spot, 0) + 1
        plays = int(plays)
        holder = best.get(spot)
        if holder is None or plays > holder[0]:
            best[spot] = (plays, [int(user_id)])
        elif plays == holder[0]:
            holder[1].append(int(user_id))

    counts: dict[int, int] = {}
    crowned = 0
    for spot, (_plays, holders) in best.items():
        if contenders.get(spot, 0) < min_contenders:
            continue
        crowned += 1
        for user_id in holders:
            counts[user_id] = counts.get(user_id, 0) + 1

    ranked = sorted(counts.items(), key=lambda pair: pair[1], reverse=True)
    return ranked, crowned, len(best)


async def track_plays_by_user(
    db: Any, user_ids: Sequence[int], artist_key: str, track_key: str
) -> list[tuple[int, int]]:
    ids = [int(uid) for uid in user_ids]
    if not ids:
        return []
    rows = await db.execute(
        f"SELECT user_id, COUNT(*) FROM lastfm_scrobbles "
        f"WHERE artist_key = %s AND track_key = %s AND user_id IN ({_placeholders(len(ids))}) "
        f"GROUP BY user_id ORDER BY COUNT(*) DESC",
        key(artist_key),
        key(track_key),
        *ids,
    )
    return [(int(uid), int(count)) for uid, count in rows]


async def album_plays_by_user(
    db: Any, user_ids: Sequence[int], album_key: str
) -> list[tuple[int, int]]:
    ids = [int(uid) for uid in user_ids]
    if not ids:
        return []
    rows = await db.execute(
        f"SELECT user_id, COUNT(*) FROM lastfm_scrobbles "
        f"WHERE album_key = %s AND album_key != '' AND user_id IN ({_placeholders(len(ids))}) "
        f"GROUP BY user_id ORDER BY COUNT(*) DESC",
        key(album_key),
        *ids,
    )
    return [(int(uid), int(count)) for uid, count in rows]


async def top_artists(
    db: Any, user_ids: Sequence[int], limit: int = 10
) -> list[tuple[str, str, int, int]]:
    """``(artist, artist_key, plays, listeners)`` for a set of members."""
    ids = [int(uid) for uid in user_ids]
    if not ids:
        return []
    rows = await db.execute(
        f"SELECT artist_key, MIN(artist), COUNT(*), COUNT(DISTINCT user_id) "
        f"FROM lastfm_scrobbles WHERE user_id IN ({_placeholders(len(ids))}) "
        f"GROUP BY artist_key ORDER BY COUNT(*) DESC LIMIT %s",
        *ids,
        int(limit),
    )
    return [(str(name), str(k), int(plays), int(listeners)) for k, name, plays, listeners in rows]


async def top_tracks(
    db: Any, user_ids: Sequence[int], limit: int = 10
) -> list[tuple[str, str, str, int, int]]:
    """``(track, artist, track_key, plays, listeners)`` for a set of members."""
    ids = [int(uid) for uid in user_ids]
    if not ids:
        return []
    rows = await db.execute(
        f"SELECT track_key, MIN(track), MIN(artist), COUNT(*), COUNT(DISTINCT user_id) "
        f"FROM lastfm_scrobbles WHERE user_id IN ({_placeholders(len(ids))}) "
        f"GROUP BY artist_key, track_key ORDER BY COUNT(*) DESC LIMIT %s",
        *ids,
        int(limit),
    )
    return [
        (str(track), str(artist), str(k), int(plays), int(listeners))
        for k, track, artist, plays, listeners in rows
    ]


async def top_albums(
    db: Any, user_ids: Sequence[int], limit: int = 10
) -> list[tuple[str, str, int, int]]:
    ids = [int(uid) for uid in user_ids]
    if not ids:
        return []
    rows = await db.execute(
        f"SELECT album_key, MIN(album), COUNT(*), COUNT(DISTINCT user_id) "
        f"FROM lastfm_scrobbles "
        f"WHERE album_key != '' AND user_id IN ({_placeholders(len(ids))}) "
        f"GROUP BY album_key ORDER BY COUNT(*) DESC LIMIT %s",
        *ids,
        int(limit),
    )
    return [(str(name), str(k), int(plays), int(listeners)) for k, name, plays, listeners in rows]


async def crowns_for_member(
    db: Any, server_user_ids: Sequence[int], user_id: int, limit: int = 10
) -> list[tuple[str, str, int, int]]:
    """Artists this member is the top listener for, in this server.

    ``(artist, artist_key, plays, runner_up_plays)``, the member's biggest
    crowns first. Only the member's own top 100 artists are checked, which keeps
    the query bounded on a big server.
    """
    ids = [int(uid) for uid in server_user_ids]
    if not ids:
        return []

    mine = await db.execute(
        "SELECT artist_key, MIN(artist), COUNT(*) FROM lastfm_scrobbles "
        "WHERE user_id = %s GROUP BY artist_key ORDER BY COUNT(*) DESC LIMIT 100",
        int(user_id),
    )
    mine_map = {str(k): (str(name), int(plays)) for k, name, plays in mine}
    if not mine_map:
        return []

    keys = list(mine_map)
    held: list[tuple[str, str, int, int]] = []
    for chunk in _chunks(keys):
        rows = await db.execute(
            f"SELECT artist_key, user_id, COUNT(*) FROM lastfm_scrobbles "
            f"WHERE artist_key IN ({_placeholders(len(chunk))}) "
            f"AND user_id IN ({_placeholders(len(ids))}) "
            f"GROUP BY artist_key, user_id",
            *chunk,
            *ids,
        )
        # One artist can have several listeners, so keep every (member, plays)
        # row to find both the holder and the runner-up behind them.
        per_artist: dict[str, list[tuple[int, int]]] = {}
        for artist_key, holder, plays in rows:
            per_artist.setdefault(str(artist_key), []).append((int(holder), int(plays)))

        for artist_key, listeners in per_artist.items():
            listeners.sort(key=lambda pair: pair[1], reverse=True)
            if listeners[0][0] != int(user_id):
                continue
            name, my_plays = mine_map[artist_key]
            runner_up = listeners[1][1] if len(listeners) > 1 else 0
            held.append((name, artist_key, my_plays, runner_up))

    held.sort(key=lambda row: row[2], reverse=True)
    return held[:limit]


# ---------------------------------------------------------------------------
# Friends (global, mutual)
# ---------------------------------------------------------------------------
STATUS_PENDING = "pending"
STATUS_ACCEPTED = "accepted"


async def friend_request(db: Any, requester_id: int, target_id: int) -> bool:
    """Create a pending request unless the pair is already linked."""
    existing = await friend_status(db, requester_id, target_id)
    if existing is not None:
        return False
    await db.execute(
        "INSERT INTO lastfm_friends (requester_id, target_id, status, created_at) "
        "VALUES (%s, %s, %s, %s)",
        int(requester_id),
        int(target_id),
        STATUS_PENDING,
        int(time.time()),
    )
    return True


async def friend_status(db: Any, a: int, b: int) -> str | None:
    """The relationship between two members, whichever way it was requested."""
    row = await db.fetchrow(
        "SELECT status FROM lastfm_friends "
        "WHERE (requester_id = %s AND target_id = %s) "
        "   OR (requester_id = %s AND target_id = %s)",
        int(a),
        int(b),
        int(b),
        int(a),
    )
    return str(row[0]) if row else None


async def accept_friend(db: Any, requester_id: int, target_id: int) -> bool:
    """Accept a pending request addressed to ``target_id``."""
    row = await db.fetchrow(
        "SELECT status FROM lastfm_friends WHERE requester_id = %s AND target_id = %s",
        int(requester_id),
        int(target_id),
    )
    if row is None or str(row[0]) != STATUS_PENDING:
        return False
    await db.execute(
        "UPDATE lastfm_friends SET status = %s "
        "WHERE requester_id = %s AND target_id = %s",
        STATUS_ACCEPTED,
        int(requester_id),
        int(target_id),
    )
    return True


async def remove_friend(db: Any, a: int, b: int) -> bool:
    await db.execute(
        "DELETE FROM lastfm_friends "
        "WHERE (requester_id = %s AND target_id = %s) "
        "   OR (requester_id = %s AND target_id = %s)",
        int(a),
        int(b),
        int(b),
        int(a),
    )
    return True


async def friend_ids(db: Any, user_id: int) -> list[int]:
    """Every accepted friend's id."""
    rows = await db.execute(
        "SELECT CASE WHEN requester_id = %s THEN target_id ELSE requester_id END "
        "FROM lastfm_friends WHERE status = %s "
        "  AND (requester_id = %s OR target_id = %s)",
        int(user_id),
        STATUS_ACCEPTED,
        int(user_id),
        int(user_id),
    )
    return [int(row[0]) for row in rows]


async def incoming_requests(db: Any, user_id: int) -> list[tuple[int, int]]:
    """``(requester_id, created_at)`` for pending requests sent to this member."""
    rows = await db.execute(
        "SELECT requester_id, created_at FROM lastfm_friends "
        "WHERE target_id = %s AND status = %s ORDER BY created_at DESC",
        int(user_id),
        STATUS_PENDING,
    )
    return [(int(rid), int(created)) for rid, created in rows]


async def outgoing_requests(db: Any, user_id: int) -> list[int]:
    rows = await db.execute(
        "SELECT target_id FROM lastfm_friends "
        "WHERE requester_id = %s AND status = %s ORDER BY created_at DESC",
        int(user_id),
        STATUS_PENDING,
    )
    return [int(row[0]) for row in rows]


# ---------------------------------------------------------------------------
# OAuth sessions
# ---------------------------------------------------------------------------
async def save_session(db: Any, user_id: int, session_key: str, username: str) -> None:
    await db.execute(
        "INSERT INTO lastfm_auth (user_id, session_key, username, linked_at) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT(user_id) DO UPDATE SET session_key = excluded.session_key, "
        "username = excluded.username, linked_at = excluded.linked_at",
        int(user_id),
        str(session_key),
        str(username or ""),
        int(time.time()),
    )


async def get_session(db: Any, user_id: int) -> tuple[str, str] | None:
    row = await db.fetchrow(
        "SELECT session_key, username FROM lastfm_auth WHERE user_id = %s",
        int(user_id),
    )
    return (str(row[0]), str(row[1] or "")) if row else None


async def clear_session(db: Any, user_id: int) -> None:
    await db.execute("DELETE FROM lastfm_auth WHERE user_id = %s", int(user_id))


# ---------------------------------------------------------------------------
# Global artist cache (for the iceberg)
# ---------------------------------------------------------------------------
async def cached_artists(db: Any, keys: Sequence[str]) -> dict[str, tuple[str, int, int, int]]:
    """``{artist_key: (name, listeners, playcount, fetched_at)}``."""
    wanted = [key(k) for k in keys if k]
    if not wanted:
        return {}
    found: dict[str, tuple[str, int, int, int]] = {}
    for chunk in _chunks(wanted):
        rows = await db.execute(
            f"SELECT artist_key, name, listeners, playcount, fetched_at "
            f"FROM lastfm_artist_cache WHERE artist_key IN ({_placeholders(len(chunk))})",
            *chunk,
        )
        for artist_key, name, listeners, playcount, fetched_at in rows:
            found[str(artist_key)] = (
                str(name),
                int(listeners),
                int(playcount),
                int(fetched_at),
            )
    return found


async def store_artists(db: Any, rows: Iterable[tuple[str, str, int, int]]) -> None:
    """``(artist_key, name, listeners, playcount)`` rows, upserted."""
    now = int(time.time())
    for artist_key, name, listeners, playcount in rows:
        await db.execute(
            "INSERT INTO lastfm_artist_cache (artist_key, name, listeners, playcount, fetched_at) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT(artist_key) DO UPDATE SET name = excluded.name, "
            "listeners = excluded.listeners, playcount = excluded.playcount, "
            "fetched_at = excluded.fetched_at",
            key(artist_key),
            str(name or artist_key),
            int(listeners or 0),
            int(playcount or 0),
            now,
        )
