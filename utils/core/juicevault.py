"""Timed lyrics from Juice WRLD Vault for the live-lyric card.

The public site is a Next.js app. Its song search is a plain JSON endpoint::

    GET https://juicewrldvault.com/api/search/songs?q=<title>&limit=25

which returns the song id, title, length, category and (sometimes) a
``source_metadata`` block saying whether a sheet has been aligned. The sheet
itself is not in that response. The site's player reads it from its public
Supabase project with the same anon key its browser bundle carries::

    GET https://supabase.juicewrldvault.com/rest/v1/songs
        ?id=eq.<id>&select=id,title,author,lyrics,duration_seconds,source_metadata

``lyrics`` is an LRC sheet (``[mm:ss.xx] line``), and
``source_metadata.lyricsWordTiming`` is::

    {"version": 1,
     "lyrics": "<the same LRC>",
     "words": [{"text": "word ", "start": 12.34, "end": 12.80}, ...]}

The site matches those words to each line by longest common subsequence of
the normalised tokens, with a 60% match floor. The same alignment is done here,
so the live card can underline the word being sung rather than only the line.

The anon key is public -- it ships in the site's own JavaScript and grants only
the read access the site's row-level security allows. It is not a secret.
``JUICEVAULT_SUPABASE_KEY`` overrides it if the site ever rotates the key.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import math
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

import aiohttp

from utils.core import lyrics as lyricmatch
from utils.core import radio

logger = logging.getLogger("slimey.juicevault")

VAULT_ORIGIN = "https://juicewrldvault.com"
SEARCH_URL = f"{VAULT_ORIGIN}/api/search/songs"
SUPABASE_URL = "https://supabase.juicewrldvault.com"
SONGS_URL = f"{SUPABASE_URL}/rest/v1/songs"

#: The anon JWT the vault's own browser client uses. Public by design.
DEFAULT_SUPABASE_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJyb2xlIjoiYW5vbiIsImlzcyI6InN1cGFiYXNlIiwiaWF0IjoxNzkyNjQ2NDY1LCJleHAiOjE5NTAzMjY0NjV9."
    "Q3KfzErx8Yc7YpPsZrY91mkDdZvdFUxFTMzrNZWBwg0"
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
TIMEOUT = aiohttp.ClientTimeout(total=20)
SEARCH_LIMIT = 25
#: How many of the closest search hits are asked for their sheet. A search
#: usually puts the right recording in the first few, and each hit costs a
#: Supabase round trip.
MAX_CANDIDATES = 4
#: A hit below this is a different song, not a version of this one.
MATCH_FLOOR = 85.0
#: The title itself has to agree this well before length, category or lyrics
#: bonuses are allowed to push a hit over the floor. Without this gate a short
#: title matches the middle of an unrelated one ("Smile" inside "Miles &
#: Miles") and the bonus points buy it a card full of the wrong words.
TITLE_FLOOR = 0.95


@dataclass(slots=True)
class TimedWord:
    """One word of a line, with its own start and end when the vault has them.

    ``text`` keeps the trailing whitespace it had in the line, so a line's
    words can be joined back together exactly while one of them is highlighted.
    """

    text: str
    start: float | None = None
    end: float | None = None


@dataclass(slots=True)
class VaultSheet:
    """A vault lyric sheet plus the per-line word timings that go with it.

    Keeping the words beside the sheet instead of inside
    :class:`utils.core.lyrics.Lyrics` means reloading the lyric cog never needs
    a reload of the shared lyrics module, which the running process caches.
    """

    lyrics: lyricmatch.Lyrics
    words: list[list[TimedWord]]


#: ``Juice WRLD`` in any of the spellings a scrobble or presence uses.
_JUICE = re.compile(r"\bjuice\s*wrld\b", re.IGNORECASE)

#: The LRC ``[offset:+/-ms]`` header, which shifts every stamp in the sheet.
_OFFSET = re.compile(r"^\[offset:([+-]?\d+)\]", re.IGNORECASE | re.MULTILINE)
#: Tokens from a line, keeping their trailing whitespace so the line can be
#: rebuilt exactly when one word is highlighted.
_TOKEN = re.compile(r"\S+\s*")

#: Version/recording words. A hit that carries one of these when the song being
#: listened to does not is probably an alternate recording, so it is pushed
#: below the plain match. The penalty is deliberately large: a remix shares
#: the title and length but has different verses, so it must never beat the
#: plain recording just because the remix happens to have a timed sheet.
VARIANT_PENALTY = 55.0
_VARIANT_TOKENS = frozenset(
    {
        "remix",
        "remaster",
        "remastered",
        "live",
        "instrumental",
        "session",
        "acoustic",
        "sped",
        "slowed",
        "nightcore",
        "cover",
        "edit",
        "mix",
        "mashup",
        "stem",
        "stems",
        "freestyle",
        "raw",
        "demo",
        "snippet",
    }
)


def is_juice(artist: str) -> bool:
    """Whether a track's artist is (or includes) Juice WRLD."""
    return bool(_JUICE.search(artist or ""))


def _supabase_key() -> str:
    return (os.getenv("JUICEVAULT_SUPABASE_KEY") or "").strip() or DEFAULT_SUPABASE_KEY


def _headers() -> dict[str, str]:
    key = _supabase_key()
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "apikey": key,
        "Authorization": f"Bearer {key}",
    }


def _normalise(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).lower()
    return re.sub(r"[^a-z0-9]+", "", text)


def _tokens(value: Any) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def _words(value: Any) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(value or "").lower())


#: Words that can sit between the words of a title without changing it.
_FILLER_WORDS = frozenset({"and", "the", "a", "of"})
#: A credit marker: everything after it is an artist, not the title.
_CREDIT_WORDS = frozenset({"with", "feat", "ft", "featuring", "prod", "produced", "by"})
#: Version tags that may trail a title without making it a different song.
_TAIL_VERSION_WORDS = frozenset(
    {
        "v",
        "version",
        "remaster",
        "remastered",
        "clean",
        "explicit",
        "og",
        "unreleased",
        "bonus",
        "deluxe",
    }
)


def _is_version_token(token: str) -> bool:
    return token in _TAIL_VERSION_WORDS or bool(re.fullmatch(r"v\d{1,2}", token))


def _starts_with(short: list[str], long: list[str]) -> bool:
    """Whether ``short`` is the title at the front of ``long``.

    Filler words may appear between title words (``Armed & Dangerous`` against
    ``Armed And Dangerous``), and a title may trail off into version tags, the
    vault's ``999`` suffix, or a credit -- but not into a variant such as
    ``Remix`` or ``Session Mix``, and not into a number that could be a sequel
    (``Bandit`` must not claim ``Bandit 2``).
    """
    if not short or len(short) > len(long):
        return False
    index = 0
    for token in long:
        if index < len(short):
            if token == short[index]:
                index += 1
            elif token in _FILLER_WORDS:
                continue
            else:
                return False
            continue
        if token in _CREDIT_WORDS:
            return True
        if _is_version_token(token) or token == "999":
            continue
        return False
    return index == len(short)


def _ratio(left: str, right: str) -> float:
    """A plain character similarity, punctuation and case ignored."""
    first = _normalise(left)
    second = _normalise(right)
    if not first or not second:
        return 0.0
    if first == second:
        return 1.0
    return difflib.SequenceMatcher(None, first, second).ratio()


def _title_similarity(candidate: str, title: str) -> float:
    """How alike two titles are, without matching across word boundaries."""
    candidate_words = _words(candidate)
    title_words = _words(title)
    if not candidate_words or not title_words:
        return 0.0
    if candidate_words == title_words:
        return 1.0
    if _starts_with(title_words, candidate_words):
        return 0.97
    if _starts_with(candidate_words, title_words):
        return 0.97
    # No word-aware match: fall back to characters, without the old
    # substring shortcut ("Smile" sits inside "Miles & Miles", and
    # "Lucid Dreams" inside "Lucid Dreams (Remix) feat. ..."). A high floor
    # keeps a sequel such as "Bandit 2" from passing as "Bandit".
    ratio = _ratio(candidate, title)
    return ratio if ratio >= TITLE_FLOOR else 0.0


def _seconds(value: Any) -> float | None:
    """A duration from ``241``, ``"241"`` or ``"4:01"``."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    text = str(value).strip()
    if not text:
        return None
    if ":" in text:
        parts = text.split(":")
        try:
            total = 0.0
            for part in parts:
                total = total * 60 + float(part)
        except ValueError:
            return None
        return total if total > 0 else None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if number > 0 else None


async def _get_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    retries: int = 1,
) -> Any:
    """GET a JSON document, retrying once on a transport or server error."""
    for attempt in range(retries + 1):
        try:
            async with session.get(
                url,
                params=params,
                headers=headers or _headers(),
                timeout=TIMEOUT,
            ) as response:
                if response.status == 200:
                    return await response.json(content_type=None)
                if response.status in (400, 404):
                    return None
                logger.debug(
                    "Juice WRLD Vault answered HTTP %s for %s", response.status, url
                )
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            logger.debug("Juice WRLD Vault request failed for %s", url, exc_info=True)
        if attempt < retries:
            await asyncio.sleep(0.6)
    return None


async def _search(session: aiohttp.ClientSession, query: str) -> list[dict[str, Any]]:
    data = await _get_json(
        session,
        SEARCH_URL,
        params={"q": query, "limit": str(SEARCH_LIMIT)},
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    if not isinstance(data, dict):
        return []
    songs = data.get("songs")
    if not isinstance(songs, list):
        return []
    return [
        song
        for song in songs
        if isinstance(song, dict)
        and song.get("id") is not None
        and song.get("title")
    ]


def _score(song: dict[str, Any], title: str, cleaned: str, duration: float | None) -> float:
    """How likely a vault hit is the recording that is being listened to."""
    name = str(song.get("title") or "").strip()
    if not name:
        return float("-inf")

    if _normalise(name) == _normalise(title):
        match = 1.0
        bonus = 40.0
    elif _normalise(name) == _normalise(cleaned):
        # The scrobble keeps version tags ("(v1)", "- Remastered") that the
        # vault's canonical title does not.
        match = _title_similarity(name, title)
        bonus = 15.0
    else:
        match = _title_similarity(name, cleaned)
        bonus = 0.0
    if match < TITLE_FLOOR:
        return float("-inf")
    score = match * 100.0 + bonus

    requested = _tokens(title) | _tokens(cleaned)
    provided = _tokens(name)
    for token in _VARIANT_TOKENS:
        if token in provided and token not in requested:
            score -= VARIANT_PENALTY

    length = _seconds(song.get("duration_seconds"))
    if duration and length:
        gap = abs(length - duration)
        if gap <= 2:
            score += 25.0
        elif gap <= 5:
            score += 15.0
        elif gap <= 15:
            score += 5.0
        elif gap > 45:
            score -= 15.0

    if "released" in str(song.get("source_category") or "").lower():
        score += 5.0

    metadata = song.get("source_metadata")
    if isinstance(metadata, dict):
        # The search view says whether a sheet exists even though it does not
        # carry the sheet itself. Prefer the hits that already have one, and
        # strongly enough that the official recording wins over unlyricked
        # alternate versions of the same song.
        if metadata.get("lyricsSource") or metadata.get("lyrics_source"):
            score += 25.0
        if metadata.get("lyricsAlignment") or metadata.get("lyrics_alignment"):
            score += 5.0
    return score


def _offset_seconds(text: str) -> float:
    match = _OFFSET.search(text or "")
    if match is None:
        return 0.0
    return int(match.group(1)) / 1000.0


def _normalise_word(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", text or "").lower())


def _align_words(
    synced: list[tuple[float, str]],
    timing: Any,
    offset: float,
) -> list[list[TimedWord]]:
    """Match the vault's word stamps onto their LRC lines.

    This mirrors the site's own alignment: the words inside a line's time span
    are compared with the line's tokens by longest common subsequence of their
    normalised forms, and a line is only word-timed when at least 60% of its
    tokens line up. A line that fails keeps its plain text.
    """
    if not synced or not isinstance(timing, dict) or timing.get("version") != 1:
        return []
    raw_words = timing.get("words")
    if not isinstance(raw_words, list) or not raw_words:
        return []

    words: list[tuple[float, float, str]] = []
    for item in raw_words:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item.get("start"))
            end = float(item.get("end"))
        except (TypeError, ValueError):
            continue
        text = str(item.get("text") or "")
        if not text or end < start:
            continue
        start = max(0.0, start + offset)
        end = max(start, end + offset)
        words.append((start, end, text))
    if not words:
        return []

    lines: list[list[TimedWord]] = []
    for index, (line_time, line_text) in enumerate(synced):
        next_time = synced[index + 1][0] if index + 1 < len(synced) else float("inf")
        # The site allows a word to start up to 25ms before its line.
        low = line_time - 0.025
        high = next_time - 0.025
        spoken = [word for word in words if low <= word[0] < high]
        tokens = _TOKEN.findall(line_text)
        if not tokens or not spoken or len(tokens) > 300 or len(spoken) > 300:
            lines.append([])
            continue

        wanted = [_normalise_word(token) for token in tokens]
        heard = [_normalise_word(word[2]) for word in spoken]
        if not any(wanted):
            lines.append([])
            continue

        # Longest common subsequence: table[i][j] is the best match using
        # tokens[i:] and spoken[j:].
        rows, columns = len(wanted), len(heard)
        table = [[0] * (columns + 1) for _ in range(rows + 1)]
        for i in range(rows - 1, -1, -1):
            for j in range(columns - 1, -1, -1):
                if wanted[i] and wanted[i] == heard[j]:
                    table[i][j] = 1 + table[i + 1][j + 1]
                else:
                    table[i][j] = max(table[i + 1][j], table[i][j + 1])

        required = math.ceil(0.6 * sum(1 for token in wanted if token))
        if table[0][0] < required:
            lines.append([])
            continue

        timed = [TimedWord(text=token) for token in tokens]
        i = j = 0
        while i < rows and j < columns:
            if wanted[i] and wanted[i] == heard[j]:
                timed[i].start, timed[i].end = spoken[j][0], spoken[j][1]
                i += 1
                j += 1
            elif table[i + 1][j] > table[i][j + 1]:
                i += 1
            else:
                j += 1
        lines.append(timed)
    return lines


async def _fetch_lyrics(
    session: aiohttp.ClientSession, song: dict[str, Any]
) -> VaultSheet | None:
    """The sheet and word timing for one search hit, if the vault has them."""
    identifier = song.get("id")
    if identifier is None:
        return None
    data = await _get_json(
        session,
        SONGS_URL,
        params={
            "id": f"eq.{identifier}",
            "select": "id,title,author,lyrics,duration_seconds,source_metadata",
            "limit": "1",
        },
    )
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return None
    record = data[0]

    metadata = record.get("source_metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}

    raw = str(record.get("lyrics") or "").strip()
    meta_lyrics = str(metadata.get("lyrics") or "").strip()
    if not raw:
        raw = meta_lyrics
    timing = metadata.get("lyricsWordTiming")
    if isinstance(timing, str):
        try:
            timing = json.loads(timing)
        except ValueError:
            timing = None
    if isinstance(timing, dict):
        timed_lyrics = str(timing.get("lyrics") or "").strip()
        # The column is normally the LRC sheet, but a row can carry plain text
        # there and the stamps only in the alignment block.
        if timed_lyrics and not radio.parse_lrc(raw) and radio.parse_lrc(timed_lyrics):
            raw = timed_lyrics
    if not raw:
        return None

    offset = _offset_seconds(raw)
    synced = [
        (max(0.0, when + offset), text) for when, text in radio.parse_lrc(raw)
    ]
    plain = "\n".join(text for _, text in synced).strip() if synced else raw
    word_lines = _align_words(synced, timing, offset)

    lyrics = lyricmatch.Lyrics(
        plain=plain,
        synced=synced,
        source="Juice WRLD Vault",
        title=str(record.get("title") or song.get("title") or "").strip(),
        artist=str(record.get("author") or "Juice WRLD").strip(),
        duration=_seconds(record.get("duration_seconds")),
    )
    return VaultSheet(lyrics=lyrics, words=word_lines)


def _queries(title: str) -> list[str]:
    """The spellings worth searching the vault for, most specific first.

    The full title goes first so a versioned track ("Robbery (v1)") still finds
    its own recording; the cleaned title then finds the canonical one.
    """
    candidates = [title.strip(), lyricmatch.clean_title(title)]
    candidates.extend(lyricmatch.title_candidates(title))
    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in candidates:
        key = candidate.casefold()
        if key and key not in seen:
            seen.add(key)
            ordered.append(candidate)
    return ordered[:3]


async def find(
    session: aiohttp.ClientSession,
    *,
    title: str,
    artist: str = "",
    duration: float | None = None,
) -> VaultSheet | None:
    """The closest Juice WRLD Vault sheet for a track, if it has one.

    Only Juice WRLD tracks are looked up. The result carries line timings from
    the vault's LRC and word timings when the site has aligned them. A plain
    sheet is returned only when no timed hit exists, so the caller can still
    fall back to another source for a timed version.
    """
    title = (title or "").strip()
    if not title or not is_juice(artist):
        return None

    cleaned = lyricmatch.clean_title(title)
    hits: dict[str, dict[str, Any]] = {}
    for query in _queries(title):
        for song in await _search(session, query):
            hits.setdefault(str(song.get("id")), song)
    if not hits:
        logger.debug("Juice WRLD Vault has no hit for %r", title)
        return None

    ranked = sorted(
        hits.values(),
        key=lambda song: _score(song, title, cleaned, duration),
        reverse=True,
    )
    ranked = [
        song for song in ranked if _score(song, title, cleaned, duration) >= MATCH_FLOOR
    ]

    plain: VaultSheet | None = None
    for song in ranked[:MAX_CANDIDATES]:
        sheet = await _fetch_lyrics(session, song)
        if sheet is None:
            continue
        if sheet.lyrics.is_synced:
            logger.info(
                "Juice WRLD Vault matched %r for %r (%s lines, %s word-timed)",
                sheet.lyrics.title,
                title,
                len(sheet.lyrics.synced),
                sum(1 for line in sheet.words if line),
            )
            return sheet
        plain = plain or sheet
    if plain is not None:
        logger.info(
            "Juice WRLD Vault matched plain %r for %r", plain.lyrics.title, title
        )
    return plain
