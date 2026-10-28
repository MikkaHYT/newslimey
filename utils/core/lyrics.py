"""Finding the right lyric sheet for a scrobbled song.

The radio runs on lrclib through :func:`utils.core.radio.fetch_lyrics`, which
asks for the title exactly as it was given. A Last.fm scrobble carries tags the
site doesn't (``I'm Still (v1)``, ``Bandit - Remastered 2011``), and an exact
lookup for those misses — or worse, finds a leaked alternate recording. This
module strips the tags first, then ranks what lrclib's search returns on title,
artist and length, and prefers the result that carries timed lyrics.

It lives apart from ``radio`` on purpose: the live-lyric cog is reloaded on its
own, and anything new added to a module the running process already imported
would not be there until a restart.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import aiohttp

from utils.core import radio

logger = logging.getLogger("slimey.lyrics")

LRCLIB_API = "https://lrclib.net/api"
LYRICS_OVH_API = "https://api.lyrics.ovh/v1"
USER_AGENT = "TPNEbot/1.0 (+livelyrics)"
SEARCH_TIMEOUT = aiohttp.ClientTimeout(total=20)

#: Version tags a scrobbled title keeps that lrclib's canonical title doesn't.
#: Left in, they make the exact lookup miss and a search can rank a leak or a
#: remix above the song.
_VERSION_WORDS = frozenset(
    {
        "v",
        "version",
        "og",
        "og file",
        "leak",
        "leaked",
        "snippet",
        "demo",
        "remaster",
        "remastered",
        "instrumental",
        "acapella",
        "acappella",
        "sped up",
        "slowed",
        "nightcore",
        "clean",
        "explicit",
        "official",
        "official audio",
        "official video",
        "audio",
        "video",
        "lyric video",
        "lyrics",
        "visualizer",
        "unreleased",
        "extended",
        "radio edit",
        "bonus",
        "bonus track",
        "full song",
    }
)
#: A trailing bracket group, and a trailing ``- Remastered`` style suffix.
_TRAILING_GROUP = re.compile(r"\s*[\(\[\{]\s*([^\(\)\[\]\{\}]{1,50}?)\s*[\)\]\}]\s*$")
_TRAILING_DASH = re.compile(r"\s+[-–—]\s+([^-–—]{1,50})$")
#: A feature or production credit, which lrclib often leaves out of the title.
_CREDIT_START = re.compile(r"^(?:feat|ft|prod|produced by|with)\b", re.IGNORECASE)
_CREDIT_TAIL = re.compile(
    r"\s*[\(\[{]?\s*(?:feat|ft|prod)\.?\s+[^\)\]\}]+[\)\]\}]?\s*$", re.IGNORECASE
)

#: How good a search result has to look before it is trusted as *this* song.
#: A wrong match would print somebody else's lyrics, so a loose one is dropped;
#: an unrelated record still scores for a shared word or two (~60), while the
#: right song with a slightly different title clears this comfortably.
MATCH_FLOOR = 80.0
#: A record shorter than this is a snippet more often than a song, and lrclib
#: carries plenty of those for unreleased tracks. A snippet is still used when
#: nothing longer exists, but it never wins over a full recording of the same
#: song — that is what made a live card end seconds after it started.
MIN_FULL_LENGTH = 60.0


@dataclass(slots=True)
class Lyrics:
    """A lyric sheet, timed or plain, with the recording it belongs to."""

    plain: str
    synced: list[tuple[float, str]] = field(default_factory=list)
    source: str = "lrclib"
    title: str = ""
    artist: str = ""
    duration: float | None = None

    @property
    def is_synced(self) -> bool:
        return bool(self.synced)


async def _get_json(
    session: aiohttp.ClientSession,
    url: str,
    params: dict[str, Any] | None = None,
    *,
    retries: int = 2,
) -> Any:
    """GET JSON, retrying the flaky lrclib search endpoint a couple of times."""
    for attempt in range(retries + 1):
        try:
            async with session.get(
                url,
                params=params,
                timeout=SEARCH_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
            ) as response:
                if response.status == 200:
                    return await response.json(content_type=None)
                if response.status == 404:
                    return None
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            pass
        if attempt < retries:
            await asyncio.sleep(0.6 * (attempt + 1))
    return None


def _normalised(value: str) -> str:
    """A name stripped to letters and digits, for comparing titles."""
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _is_version_tag(text: str) -> bool:
    """Whether a bracketed/suffixed chunk is a version tag rather than a title."""
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return False
    if _CREDIT_START.match(cleaned):
        return True
    for part in (piece.strip() for piece in cleaned.split(",")):
        if not part:
            continue
        # "remastered 2011", "official audio 2020" - the tag leads the group.
        if part in _VERSION_WORDS or part.split(" ")[0] in _VERSION_WORDS:
            continue
        # "v1", "v 2", "version 3" all read as a version.
        if re.match(r"(?:v|version)\s*\d{1,2}\b", part):
            continue
        return False
    return True


def clean_title(title: str) -> str:
    """Drop the version tags from a title: ``I'm Still (v1)`` -> ``I'm Still``.

    Only tags that are entirely version/credit words are removed, so a title
    that happens to carry brackets of its own (``(I Can't Get No) Satisfaction``)
    is left alone.
    """
    text = (title or "").strip()
    changed = True
    while changed and text:
        changed = False
        for pattern in (_TRAILING_GROUP, _TRAILING_DASH):
            match = pattern.search(text)
            if match is None or not _is_version_tag(match.group(1)):
                continue
            stripped = text[: match.start()].strip()
            if stripped:
                text, changed = stripped, True
                break
    return text or (title or "").strip()


def title_candidates(title: str) -> list[str]:
    """The spellings of a title worth asking lrclib about, best first."""
    raw = (title or "").strip()
    cleaned = clean_title(raw)
    candidates = [cleaned, raw]
    # "Song - Artist" style names, and a credit left mid-title.
    candidates.extend(
        part.strip() for part in re.split(r"\s+[-–—]\s+", cleaned) if part.strip()
    )
    without_credit = _CREDIT_TAIL.sub("", cleaned).strip()
    if without_credit:
        candidates.append(without_credit)

    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in candidates:
        key = candidate.casefold()
        if key and key not in seen:
            seen.add(key)
            ordered.append(candidate)
    return ordered


def _similarity(left: str, right: str) -> float:
    """How alike two names are, punctuation and case ignored."""
    first = _normalised(left)
    second = _normalised(right)
    if not first or not second:
        return 0.0
    if first == second:
        return 1.0
    if first in second or second in first:
        return 0.9
    return difflib.SequenceMatcher(None, first, second).ratio()


def _record_score(
    record: dict[str, Any], title: str, artist: str, duration: float | None
) -> float:
    """How likely a lrclib results record is the song that was asked for."""
    score = _similarity(record.get("trackName"), title) * 100.0
    if artist:
        score += _similarity(record.get("artistName"), artist) * 60.0
    if record.get("syncedLyrics"):
        score += 20.0
    elif record.get("plainLyrics"):
        score += 5.0
    try:
        length = float(record.get("duration"))
    except (TypeError, ValueError):
        length = 0.0
    # Same title and artist: the full song is the one being listened to, not
    # the twenty-second snippet of it somebody uploaded separately.
    if length >= MIN_FULL_LENGTH:
        score += 5.0
    # Only a known length is worth scoring against; with nothing to compare
    # to, every record would otherwise look like a perfect length match.
    if duration and duration > 0:
        try:
            gap = abs(length - float(duration))
        except (TypeError, ValueError):
            gap = None
        if gap is not None:
            if gap <= 2:
                score += 25.0
            elif gap <= 5:
                score += 15.0
            elif gap <= 15:
                score += 5.0
            else:
                score -= 10.0
    return score


def _from_record(record: dict[str, Any]) -> Lyrics | None:
    """The lyrics and the recording's own details out of a lrclib record."""
    synced = radio.parse_lrc(record.get("syncedLyrics") or "")
    plain = str(record.get("plainLyrics") or "").strip()
    if synced:
        plain = plain or "\n".join(text for _, text in synced)
    elif not plain:
        return None
    try:
        length = float(record.get("duration"))
    except (TypeError, ValueError):
        length = 0.0
    return Lyrics(
        plain=plain,
        synced=synced,
        source="lrclib",
        title=str(record.get("trackName") or "").strip(),
        artist=str(record.get("artistName") or "").strip(),
        duration=length if length > 0 else None,
    )


async def find(
    session: aiohttp.ClientSession,
    *,
    title: str,
    artist: str = "",
    duration: float | None = None,
) -> Lyrics | None:
    """The best lrclib match for ``title``, rather than only an exact one.

    The tags a scrobble carries are stripped first, then what a search returns
    is ranked by title, artist and length, and the closest recording wins. The
    result names the match it settled on, so a card can show which recording
    the words belong to.
    """
    title = (title or "").strip()
    artist = (artist or "").strip()
    if not title:
        return None

    candidates = title_candidates(title)
    if not candidates:
        return None

    # An exact hit that is only a snippet is held back rather than returned:
    # the search may have the full recording under the same name, and a lyric
    # card built on twenty seconds of a song ends almost as soon as it opens.
    short: Lyrics | None = None
    for candidate in candidates:
        params: dict[str, Any] = {"track_name": candidate}
        if artist:
            params["artist_name"] = artist
        if duration and duration > 0:
            params["duration"] = int(round(duration))
        exact = await _get_json(session, f"{LRCLIB_API}/get", params)
        if isinstance(exact, dict) and not exact.get("instrumental"):
            found = _from_record(exact)
            if found is None:
                continue
            if found.duration is None or found.duration >= MIN_FULL_LENGTH:
                return found
            short = short or found

    queries: list[str] = []
    for candidate in candidates[:2]:
        if artist:
            queries.append(f"{artist} {candidate}".strip())
        queries.append(candidate)
    seen: set[str] = set()
    ordered: list[str] = []
    for query in queries:
        key = query.casefold()
        if key and key not in seen:
            seen.add(key)
            ordered.append(query)

    # Rank against the cleaned name, not the scrobble's spelling: scoring the
    # raw title would let a record with the version tag in its name (a leak)
    # beat the recording the song actually is.
    canonical = candidates[0]
    best: dict[str, Any] | None = None
    best_score = MATCH_FLOOR
    skip: set[Any] = set()
    for query in ordered[:4]:
        results = await _get_json(session, f"{LRCLIB_API}/search", {"q": query})
        if not isinstance(results, list):
            continue
        for record in results:
            if not isinstance(record, dict) or record.get("instrumental"):
                continue
            if not (record.get("syncedLyrics") or record.get("plainLyrics")):
                continue
            identity = (
                record.get("id")
                or (record.get("trackName"), record.get("artistName"), record.get("duration"))
            )
            if identity in skip:
                continue
            skip.add(identity)
            score = _record_score(record, canonical, artist, duration)
            if score > best_score:
                best, best_score = record, score

    if best is not None:
        found = _from_record(best)
        if found is not None:
            return found

    # Only snippets on file: better a short sheet than no card at all.
    if short is not None:
        return short

    # Nothing on lrclib: a plain transcript is better than no card at all.
    if artist:
        cleaned = candidates[0]
        fallback = await _get_json(
            session, f"{LYRICS_OVH_API}/{quote(artist)}/{quote(cleaned)}"
        )
        if isinstance(fallback, dict) and (fallback.get("lyrics") or "").strip():
            return Lyrics(
                plain=fallback["lyrics"].strip(),
                synced=[],
                source="lyrics.ovh",
                title=cleaned,
                artist=artist,
            )
    return None
