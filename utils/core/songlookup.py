"""Song metadata from the regular Juice WRLD API.

The vault collections (``!remaster``, ``!edit``, ...) are files, not catalog
entries: their own rows only say which upload the site streams, so a card built
from one says nothing about the song itself. The cards therefore look the
*song* up on juicewrldapi.com — the catalog ``!leak`` and ``!songinfo`` read —
and show that behind their Info button instead.

Every bracketed qualifier (``[Sessions]``, ``(v2)``, ``{Remaster}``) is stripped
before searching, because the API only knows the plain song name; an unbracketed
``v1``/``v2`` in the title is kept as a tie-break so the matching recording wins.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
import time
from typing import Any

import aiohttp

logger = logging.getLogger("slimey.songlookup")

SONGS_ENDPOINT = "https://juicewrldapi.com/juicewrld/songs/"
API_TIMEOUT = aiohttp.ClientTimeout(total=15)
#: How long a fetched song row is reused before it is looked up again.
SONG_TTL = 3600.0
#: Longest field value shown on a card before it is elided.
MAX_META_LENGTH = 90

#: Leading labels the catalog puts inside a value, e.g. ``Recorded\nJune 4``.
_CATALOG_LABEL_RE = re.compile(
    r"(?im)^\s*(?:recorded|first previewed|previewed|surfaced|released|file exported)\s*:?\s*"
)


def truncate(value: str, limit: int = MAX_META_LENGTH) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def song_title_for_lookup(value: Any) -> str:
    """The plain song name behind a vault title.

    ``Two Phones (with G Herbo) [Remaster]`` becomes ``Two Phones``, and so
    does ``Baller Of The Year [Ai Remaster]`` — which is what the Juice WRLD
    API's search understands best.
    """
    text = re.sub(r"[\[\({][^\[\({]*[\]\)}]", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip(" -–")
    return text or str(value or "").strip()


def _norm_title(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _title_tokens(value: Any) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(value or "").lower())


def version_token(value: Any) -> str | None:
    """The ``v1``/``v2`` marker in a title, if it has one."""
    match = re.search(r"\bv(\d{1,2})\b", str(value or ""), re.IGNORECASE)
    return f"v{match.group(1).lower()}" if match else None


def _search_queries(query: str) -> list[str]:
    """The spellings worth asking the API for, most specific first.

    The API's search matches whole words, so a title it spells differently
    (``Glo' Limit``, ``Monsters In My Basement``, ``Mask On My Face (with
    Future)``) often only shows up for a shorter tail of the same query. The
    row returned is still scored against the full title, so a loose query
    cannot land the card on an unrelated song.
    """
    words = [word for word in query.split() if word]
    candidates = [query.strip()]
    if len(words) > 2:
        candidates.append(" ".join(words[-2:]))
    if len(words) > 1:
        candidates.append(words[-1])

    ordered: list[str] = []
    for candidate in candidates:
        candidate = candidate.strip(" -–'")
        if candidate and candidate.lower() not in {seen.lower() for seen in ordered}:
            ordered.append(candidate)
    return ordered


def _api_text(value: Any) -> str:
    """A catalog field as one compact line, without its leading label."""
    text = _CATALOG_LABEL_RE.sub("", str(value or "")).strip()
    text = re.sub(r"\s*\n\s*", ", ", text)
    text = re.sub(r"\.\s*,\s*", ", ", text).strip(" ,")
    if text.lower() in ("", "n/a", "na", "none", "unavailable"):
        return ""
    return text


def build_song_details(song: dict[str, Any]) -> str:
    """The Info block for a song, built from its Juice WRLD API row."""
    fields: list[str] = []

    era = song.get("era")
    if isinstance(era, dict):
        era_text = str(era.get("description") or "").strip()
        name = str(era.get("name") or "").strip()
        if era_text and name and name.lower() not in era_text.lower():
            era_text = f"{era_text} ({name})"
        era_text = era_text or name
        if era_text:
            fields.append(f"**Era**\n{era_text}")

    category = _api_text(song.get("category") or song.get("leak_type"))
    if category:
        fields.append(f"**Category**\n{category.replace('_', ' ').title()}")

    length = _api_text(song.get("length"))
    if length:
        fields.append(f"**Length**\n{length}")

    producers = _api_text(song.get("producers"))
    if producers:
        fields.append(f"**Producer(s)**\n{truncate(producers)}")

    engineers = _api_text(song.get("engineers"))
    if engineers:
        fields.append(f"**Engineer(s)**\n{truncate(engineers)}")

    title = str(song.get("name") or "").strip()
    alts: list[str] = []
    for name in song.get("track_titles") or []:
        text = str(name or "").strip()
        if text and text.lower() != title.lower() and text not in alts:
            alts.append(text)
    if alts:
        fields.append(f"**Alt Name(s)**\n{truncate(', '.join(alts))}")

    recorded = _api_text(song.get("record_dates"))
    if recorded:
        fields.append(f"**Recorded**\n{truncate(recorded)}")

    previewed = _api_text(song.get("preview_date"))
    if previewed:
        fields.append(f"**Previewed**\n{truncate(previewed)}")

    released = _api_text(song.get("release_date"))
    if released:
        fields.append(f"**Released**\n{truncate(released)}")

    location = _api_text(song.get("recording_locations"))
    if location:
        fields.append(f"**Recorded At**\n{truncate(location)}")

    notes = _api_text(song.get("additional_information"))
    if notes:
        fields.append(f"**Notes**\n{truncate(notes, 200)}")

    return "\n\n".join(fields)


def pick_song(
    results: list[dict[str, Any]], query: str, version: str | None = None
) -> dict[str, Any] | None:
    """The API row that really is the song behind ``query``.

    The API's own search already narrows the list, so this only has to keep a
    near-miss (``Calm Down`` against ``Calm Down (Remix)``) from winning over
    the plain recording. Rows are compared on their name and alternate titles,
    and a row whose title merely adds a credit (``Mask On My Face`` against
    ``Mask On My Face (with Future)``) still counts as the same song.
    """
    want = _norm_title(query)
    if not want:
        return None
    want_words = _title_tokens(query)

    best: dict[str, Any] | None = None
    best_key: tuple[float, int] = (0.0, -1)
    for song in results:
        # The row's own name counts for more than an alternate title: the
        # "999" cut of a song lists the plain title as an alt name, and it
        # must not beat the plain recording itself.
        names = [
            (song.get("name"), 0.0),
            *((alias, 0.04) for alias in song.get("track_titles") or []),
        ]
        score = 0.0
        for name, penalty in names:
            candidate = _norm_title(name)
            if not candidate:
                continue
            row_score = 0.0
            if candidate == want:
                row_score = 1.0
            elif _title_tokens(name)[: len(want_words)] == want_words:
                row_score = 0.96
            else:
                row_score = difflib.SequenceMatcher(None, want, candidate).ratio()
            # Several API rows are the same song at different versions, so a
            # ``v1``/``v2`` in the title picks between them when it has one.
            if version and row_score:
                row_version = version_token(name)
                if row_version == version:
                    row_score += 0.02
                elif row_version:
                    row_score -= 0.02
            score = max(score, row_score - penalty)
        # Ties go to the row that actually carries metadata to show.
        filled = sum(1 for value in song.values() if value not in (None, "", [], {}))
        key = (round(score, 3), filled)
        if key > best_key:
            best, best_key = song, key

    return best if best_key[0] >= 0.7 else None


class SongLookup:
    """A cached lookup of songs on the Juice WRLD API.

    One instance is shared by a cog: it owns the HTTP session and remembers
    each answer for :data:`SONG_TTL`, so opening a card again is instant.
    """

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[str, tuple[float, dict[str, Any] | None]] = {}

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "SlimeyMusicHub/1.0"}, timeout=API_TIMEOUT
            )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def find(self, title: str) -> dict[str, Any] | None:
        """The API row for the song behind ``title``, if it has one."""
        query = song_title_for_lookup(title)
        if not query or self._session is None:
            return None

        version = version_token(title)
        key = f"{query.lower()}|{version or ''}"
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None and now - cached[0] < SONG_TTL:
            return cached[1]

        song: dict[str, Any] | None = None
        for attempt in _search_queries(query):
            try:
                async with self._session.get(
                    SONGS_ENDPOINT, params={"search": attempt, "page_size": 10}
                ) as response:
                    if response.status != 200:
                        logger.warning(
                            "[SongLookup] Juice WRLD API answered HTTP %s for %r",
                            response.status,
                            attempt,
                        )
                        break
                    data = await response.json()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                logger.debug(
                    "[SongLookup] Juice WRLD API lookup failed for %r",
                    attempt,
                    exc_info=True,
                )
                break

            results = [
                row for row in (data.get("results") or []) if isinstance(row, dict)
            ]
            song = pick_song(results, query, version)
            if song is not None:
                break

        self._cache[key] = (now, song)
        return song
