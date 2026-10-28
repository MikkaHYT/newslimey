"""Client for the public Tracks API at ``tracks.monochrome.st``.

Only two endpoints are needed here:

``GET /search?q=<query>``
    JSON with a ``tracks`` list: id, title, artist names, artwork, duration.
``GET /track/<id>``
    The full song itself, streamed as an audio file.

``,np`` uses this to put a real preview behind a now-playing card: the best
matching track is searched for, the whole song is pulled down, and a random
30 second window is cut out with ffmpeg and encoded as Ogg Opus — the only
thing Discord accepts for a voice message. Everything is best-effort; a search
that finds nothing or an API that is down simply means no preview, never a
broken command.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import shutil
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional

import aiohttp

logger = logging.getLogger("slimey.tracks")

API_BASE = "https://tracks.monochrome.st"
SEARCH_TIMEOUT = 15.0
#: Pulling a whole song down takes longer than a normal API call.
DOWNLOAD_TIMEOUT = 180.0
#: The window cut out of the song, and how the voice note is encoded.
SNIPPET_SECONDS = 30
OPUS_BITRATE = "96k"

_FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
_FFPROBE = shutil.which("ffprobe") or "ffprobe"

#: How close a search result has to be before it is trusted as the right song.
_MIN_SCORE = 0.55


@dataclass(slots=True)
class Track:
    """One result from the API's search endpoint."""

    id: str
    title: str
    artists: list[str] = field(default_factory=list)
    duration: float | None = None
    artwork: str | None = None

    @property
    def artist(self) -> str:
        return ", ".join(self.artists)


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def normalize(value: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace."""
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _similarity(left: str, right: str) -> float:
    left, right = normalize(left), normalize(right)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        return 0.9
    return SequenceMatcher(None, left, right).ratio()


def parse_tracks(payload: Any) -> list[Track]:
    """Turn a raw search response into :class:`Track` objects."""
    if not isinstance(payload, dict):
        return []
    tracks: list[Track] = []
    for raw in payload.get("tracks") or []:
        if not isinstance(raw, dict):
            continue
        track_id = _text(raw.get("id") or raw.get("trackId"))
        if not track_id:
            continue
        artists = [
            _text(name) for name in (raw.get("artistNames") or []) if _text(name)
        ]
        duration = raw.get("duration")
        tracks.append(
            Track(
                id=track_id,
                title=_text(raw.get("title")),
                artists=artists,
                duration=(
                    float(duration) / 1000.0
                    if isinstance(duration, (int, float))
                    else None
                ),
                artwork=_text(raw.get("artwork")) or None,
            )
        )
    return tracks


async def search(session: aiohttp.ClientSession, query: str) -> list[Track]:
    """Search the API for ``query``. Returns an empty list on any failure."""
    query = (query or "").strip()
    if not query:
        return []
    try:
        async with session.get(
            f"{API_BASE}/search",
            params={"q": query},
            timeout=aiohttp.ClientTimeout(total=SEARCH_TIMEOUT),
        ) as response:
            if response.status != 200:
                logger.info("Tracks search returned %s for %r", response.status, query)
                return []
            payload = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
        logger.info("Tracks search failed for %r: %s", query, error)
        return []
    return parse_tracks(payload)


def best_match(tracks: list[Track], title: str, artist: str) -> Optional[Track]:
    """Pick the result that looks most like ``title`` by ``artist``.

    The title carries most of the weight so a cover of the right song still
    wins, and the artist breaks the ties between tracks with the same name.
    """
    if not tracks:
        return None

    wanted_title = normalize(title)
    wanted_artist = normalize(artist)
    best: Optional[Track] = None
    best_score = 0.0

    for track in tracks:
        title_score = _similarity(title, track.title)
        artist_score = max(
            (_similarity(artist, name) for name in track.artists), default=0.0
        )
        score = title_score * 0.75 + artist_score * 0.25
        if wanted_title and normalize(track.title) == wanted_title:
            score += 0.15
        if wanted_artist and artist_score >= 0.8:
            score += 0.10
        if score > best_score:
            best, best_score = track, score

    if best is None or best_score < _MIN_SCORE:
        return None
    return best


def audio_url(track: Track) -> str:
    """The endpoint that streams the whole song."""
    return f"{API_BASE}/track/{track.id}"


async def _usable_cache(path: Path, expected: Optional[float]) -> Optional[Path]:
    """An already-downloaded copy of the song, or ``None`` to fetch it again.

    A missing or empty file is a miss, and so is one whose length does not
    match what the catalogue advertises: a download cut off by a restart would
    otherwise play as a song that stops early. When the catalogue gave no
    length there is nothing to check against, so any non-empty file counts.
    """
    if not path.is_file() or path.stat().st_size == 0:
        return None
    if not expected or expected <= 0:
        return path
    actual = await _duration(path)
    if actual is None:
        # Unmeasurable: trust the file rather than paying for it again.
        return path
    if actual < expected * 0.97:
        return None
    return path


async def download(
    session: aiohttp.ClientSession, track: Track, directory: Path
) -> Optional[Path]:
    """Stream the full song into ``directory`` and return the file.

    A file already cached for this id is handed straight back: pulling the whole
    song down again takes seconds, and the same song comes up over and over (a
    re-request, or autoplay circling an artist), so the second ask is instant.
    The cached copy is measured against the length the catalogue advertised -
    the same check a fresh download gets - so a truncated file left by a crash
    is re-fetched instead of played.
    """
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", track.id) or "track"
    path = directory / f"{name}.audio"
    cached = await _usable_cache(path, track.duration)
    if cached is not None:
        logger.info("Tracks download for %s served from cache", track.id)
        return cached
    try:
        async with session.get(
            audio_url(track),
            timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT),
        ) as response:
            if response.status != 200:
                logger.info(
                    "Tracks download returned %s for %s", response.status, track.id
                )
                return None
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as handle:
                async for chunk in response.content.iter_chunked(64 * 1024):
                    handle.write(chunk)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
        logger.info("Tracks download failed for %s: %s", track.id, error)
        return None

    if not path.is_file() or path.stat().st_size == 0:
        return None
    return path


async def _duration(path: Path) -> Optional[float]:
    """Length of the file in seconds, or ``None`` when ffprobe can't tell."""
    try:
        process = await asyncio.create_subprocess_exec(
            _FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=20.0)
    except (OSError, asyncio.TimeoutError):
        return None

    try:
        value = float(stdout.decode().strip())
    except ValueError:
        return None
    return value if value > 0 else None


async def cut_snippet(
    source: Path, directory: Path, *, seconds: int = SNIPPET_SECONDS
) -> Optional[Path]:
    """Cut a random ``seconds`` window out of ``source`` as Ogg Opus.

    ``-ss`` sits before ``-i`` so ffmpeg seeks instead of decoding the whole
    song, which is what keeps this quick on a long track.
    """
    duration = await _duration(source)
    start = 0.0
    if duration and duration > seconds:
        start = random.uniform(0, duration - seconds)

    output = directory / "voice-message.ogg"
    command = [
        _FFMPEG,
        "-y",
        "-v",
        "error",
        "-ss",
        f"{start:.2f}",
        "-t",
        str(seconds),
        "-i",
        str(source),
        "-vn",
        "-c:a",
        "libopus",
        "-b:a",
        OPUS_BITRATE,
        "-ar",
        "48000",
        "-ac",
        "1",
        str(output),
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=120.0)
    except (OSError, asyncio.TimeoutError) as error:
        logger.warning("Could not cut a snippet: %s", error)
        return None

    if process.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        logger.warning(
            "ffmpeg failed to cut a snippet: %s",
            stderr.decode(errors="replace")[:400],
        )
        return None
    return output


async def song_preview(
    session: aiohttp.ClientSession,
    title: str,
    artist: str,
    directory: Path,
) -> Optional[Path]:
    """Search, download and cut a voice-note preview of one song.

    ``None`` means the preview isn't available — nothing found, the API is
    unreachable, or ffmpeg wouldn't play along.
    """
    title = (title or "").strip()
    if not title:
        return None

    tracks = await search(session, f"{artist} {title}".strip())
    if not tracks:
        # A title with "feat." clutter or a stray suffix searches better alone.
        tracks = await search(session, title)

    track = best_match(tracks, title, artist)
    if track is None:
        return None

    source = await download(session, track, directory)
    if source is None:
        return None
    return await cut_snippet(source, directory)
