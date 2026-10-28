"""Backend for the radio station that ``cogs/station.py`` drives.

Two jobs live here, kept out of the cog so they can be exercised without a
Discord connection:

**Resolving a song.** A query is either a link (SoundCloud, YouTube, ...) or a
name. Links go to cobalt first — the public instance now demands an API key, so
when none is configured that step is skipped and yt-dlp (which speaks
SoundCloud and YouTube natively) takes over. A Spotify link carries no audio at
all, so its public oEmbed metadata is read for the title and artist and the song
is then found on YouTube. Names go to the tracks API that the ``,np`` snippet
feature already uses, and fall back to a SoundCloud search through yt-dlp.
Every resolver returns a fully downloaded file, so the radio never has to stream
an expiring CDN URL.

**Karaoke.** :func:`make_karaoke` renders a vocal-suppressed copy of a track by
subtracting the two stereo channels (which cancels whatever sits dead centre,
where a lead vocal almost always is), mixing the centre's low end back in and
normalising the quiet backing track back up. The subtracted pair is played to
both channels *in phase*, so the render survives a mono device instead of
collapsing to silence, and a track with no usable stereo image is scooped
instead of cancelled. Rendered once and cached, so switching karaoke on mid-song
is cheap.

**Timed lyrics.** lrclib is asked first: its ``/get`` endpoint returns word-level
timed lyrics when they exist and plain text otherwise, and ``/search`` is the
backup for a slightly-off title. ``lyrics.ovh`` is the last resort for a plain
transcript. ``parse_lrc`` turns the ``[mm:ss.xx]`` format into
``(seconds, line)`` pairs the card can highlight against the play clock.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import aiohttp

from utils.core import separate
from utils.core import tracks as snippet_api

logger = logging.getLogger("slimey.radio")

#: Where resolved songs are kept. One folder per track keeps a re-request
#: instant and stops two downloads from colliding on the same filename.
CACHE_DIR = Path("__radio_cache")

#: cobalt may be self-hosted or a keyed instance. Without a key the public API
#: refuses, so the resolver skips straight to yt-dlp unless one is configured.
COBALT_API_URL = os.getenv("COBALT_API_URL", "https://api.cobalt.tools").rstrip("/")
COBALT_API_KEY = (os.getenv("COBALT_API_KEY") or "").strip()

LRCLIB_API = "https://lrclib.net/api"
LYRICS_OVH_API = "https://api.lyrics.ovh/v1"

USER_AGENT = "TPNEbot/1.0 (+radio)"
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=120)
SEARCH_TIMEOUT = aiohttp.ClientTimeout(total=20)

_FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
_FFPROBE = shutil.which("ffprobe") or "ffprobe"

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
#: A Spotify track link in any of its shapes (``/track/<id>``, ``/intl-xx/``,
#: ``/album/<id>/track/<id>``).
_SPOTIFY_RE = re.compile(
    r"open\.spotify\.com/(?:[a-z-]+/)?track/([A-Za-z0-9]+)", re.IGNORECASE
)
#: Hosts yt-dlp can pull audio from directly.
_DIRECT_AUDIO_HOSTS = ("youtube.com", "youtu.be", "soundcloud.com", "music.youtube.com")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.\- ]+")
_TIMESTAMP = re.compile(r"\[(\d{1,2}):(\d{1,2}(?:[.:]\d{1,3})?)\]")
_WORD_TAG = re.compile(r"<\d{1,2}:\d{1,2}(?:[.:]\d{1,3})?>")


def is_url(text: str) -> bool:
    """Whether the query is a link rather than a song name."""
    return bool(_URL_RE.match((text or "").strip()))


def is_spotify_url(text: str) -> bool:
    """Whether the link is a Spotify track (metadata only, no audio stream)."""
    return bool(_SPOTIFY_RE.search(text or ""))


#: A pasted collection rather than one song: a Spotify playlist or album, a
#: YouTube playlist, or a SoundCloud set. Each maps to how it gets read.
_PLAYLISTS = (
    (re.compile(r"open\.spotify\.com/(?:[a-z-]+/)?(playlist|album)/([A-Za-z0-9]+)", re.I), "spotify"),
    (re.compile(r"(?:youtube\.com|music\.youtube\.com)/playlist\?[^\s]*\blist=", re.I), "youtube"),
    (re.compile(r"soundcloud\.com/[^/\s]+/sets/([^/\s?]+)", re.I), "soundcloud"),
)


def playlist_source(text: str) -> str | None:
    """Which platform a pasted playlist belongs to, or ``None`` for one song."""
    for pattern, source in _PLAYLISTS:
        if pattern.search(text or ""):
            return source
    return None


def _spotify_playlist_id(text: str) -> str | None:
    match = _PLAYLISTS[0][0].search(text or "")
    return match.group(2) if match else None


def _json_array_after(page: str, key: str) -> list[Any]:
    """Pull the JSON array stored under ``key`` out of a page.

    Spotify's embed pages are a React document with the data we want as a plain
    JSON array in the markup, so the brackets are matched by hand and the slice
    handed to ``json.loads``.
    """
    marker = f'"{key}":'
    start = page.find(marker)
    if start < 0:
        return []
    start = page.find("[", start + len(marker))
    if start < 0:
        return []
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(page)):
        char = page[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(page[start:index + 1])
                except ValueError:
                    return []
    return []


async def _spotify_playlist(
    session: aiohttp.ClientSession, url: str, limit: int
) -> tuple[str, list[Track]]:
    """Name and tracks of a public Spotify playlist or album, without a key.

    The embed page carries the whole track list, which is what makes a pasted
    playlist work with no credentials. Only names are read from it — the audio
    still comes from the song API, or SoundCloud as the fallback.
    """
    playlist_id = _spotify_playlist_id(url)
    if not playlist_id:
        return "", []
    kind = "album" if "/album/" in url else "playlist"
    try:
        async with session.get(
            f"https://open.spotify.com/embed/{kind}/{playlist_id}",
            timeout=SEARCH_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        ) as response:
            if response.status != 200:
                logger.info("Spotify %s embed returned %s", kind, response.status)
                return "", []
            page = await response.text()
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        logger.info("Spotify %s embed failed: %s", kind, error)
        return "", []

    name = ""
    oembed = await _get_json(session, "https://open.spotify.com/oembed", {"url": url})
    if isinstance(oembed, dict):
        name = str(oembed.get("title") or "").strip()

    entries = _json_array_after(page, "trackList")
    tracks: list[Track] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        if not title:
            continue
        tracks.append(
            Track(
                title=title,
                artist=str(entry.get("subtitle") or "").strip(),
                # A playlist entry carries no link of its own, so the song API
                # is asked for it by name when it is time to play it.
                source="spotify",
            )
        )
        if len(tracks) >= limit:
            break
    return name or f"Spotify {kind}", tracks


def spotify_track_id(text: str) -> str | None:
    match = _SPOTIFY_RE.search(text or "")
    return match.group(1) if match else None


def safe_name(value: str, *, max_length: int = 80) -> str:
    cleaned = _SAFE_NAME.sub("_", value or "").strip(" ._")
    return (cleaned or "track")[:max_length]


def human_duration(seconds: float | None) -> str:
    """``195.6`` -> ``3m 15s`` — the shape the now-playing card uses."""
    if not seconds or seconds <= 0:
        return "?"
    seconds = int(round(seconds))
    minutes, secs = divmod(seconds, 60)
    if minutes and secs:
        return f"{minutes}m {secs}s"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def clock(seconds: float | None) -> str:
    """``145`` -> ``2:25`` for the lyric card's play clock."""
    if seconds is None or seconds < 0:
        seconds = 0
    seconds = int(seconds)
    minutes, secs = divmod(seconds, 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def progress_bar(position: float, total: float | None, *, width: int = 14) -> str:
    """A text scrubber — ``──●─────────`` — for the lyric card."""
    if not total or total <= 0:
        return "─" * width
    ratio = max(0.0, min(1.0, position / total))
    filled = int(round(ratio * (width - 1)))
    return "─" * filled + "●" + "─" * (width - 1 - filled)


# ---------------------------------------------------------------------------
# Tracks
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Track:
    """A song the station can play, once ``path`` points at a local file."""

    title: str
    artist: str = ""
    duration: float | None = None
    artwork: str | None = None
    source_url: str | None = None
    requester_id: int | None = None
    source: str = "unknown"
    path: Path | None = None
    #: Set when the song API catalogued this track, which is what lets
    #: :func:`download` fetch it without a YouTube or SoundCloud link.
    api_id: str | None = None

    @property
    def key(self) -> str:
        """A stable identity, for spotting repeats: its link, or its name."""
        if self.source_url:
            return self.source_url.strip()
        return f"{self.title}|{self.artist}".strip().casefold()

    @property
    def credit(self) -> str:
        return f"{self.title} - {self.artist}" if self.artist else self.title

    @property
    def duration_text(self) -> str:
        return human_duration(self.duration)


def _title_from_url(url: str) -> str:
    tail = (url or "").rstrip("/").rsplit("/", 1)[-1]
    tail = tail.split("?", 1)[0].replace("-", " ").replace("_", " ").strip()
    return tail or "Unknown Track"


def query_from_url(url: str) -> str:
    """Best-effort song name behind a link, for when the link itself won't play.

    SoundCloud serves a lot of major-label tracks as DRM, which yt-dlp can't
    grab, but the link's slug (``polo-g/through-da-storm``) is still a good
    enough search. The last two path segments are kept when the last looks like
    a track, de-slugified into ``through da storm``.
    """
    path = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return ""
    tail = segments[-1]
    # A trailing numeric id (".../629621346") carries no name at all.
    if tail.isdigit() and len(segments) > 1:
        tail = segments[-2]
    return tail.replace("-", " ").replace("_", " ").replace("+", " ").strip()


def _normalised_name(value: str) -> str:
    """A name stripped to letters and digits, for comparing song titles."""
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


async def cached_name_match(
    directory: Path, name: str, expected: float | None
) -> Path | None:
    """An already-downloaded file in ``directory`` that is this song, or ``None``.

    Only an exact name match counts — different songs differ in their titles,
    and a near miss would play the wrong track — and the file has to run at
    least as long as the catalogue said, so a clip or a half-written file from
    a restart is never handed back as the song.
    """
    wanted = _normalised_name(name)
    if not wanted or not directory.is_dir():
        return None
    for candidate in directory.iterdir():
        if not candidate.is_file() or candidate.stat().st_size == 0:
            continue
        if _normalised_name(candidate.stem) != wanted:
            continue
        actual = await probe_duration(candidate)
        if actual is None:
            continue
        if expected and expected > 0 and actual < expected * 0.97:
            continue
        return candidate
    return None


async def probe_duration(path: Path) -> float | None:
    """Length of a downloaded file in seconds, or ``None`` if ffprobe can't tell."""
    try:
        process = await asyncio.create_subprocess_exec(
            _FFPROBE,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
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


async def _download(
    session: aiohttp.ClientSession, url: str, directory: Path, title: str
) -> Path | None:
    """Stream a direct audio URL into the cache."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{safe_name(title)}.audio"
    try:
        async with session.get(
            url, timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}
        ) as response:
            if response.status != 200:
                logger.info("radio download %s returned %s", url, response.status)
                return None
            with path.open("wb") as handle:
                async for chunk in response.content.iter_chunked(64 * 1024):
                    handle.write(chunk)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
        logger.info("radio download failed for %s: %s", url, error)
        return None
    if not path.is_file() or path.stat().st_size == 0:
        return None
    return path


# -- karaoke ---------------------------------------------------------------
async def audio_channels(path: Path) -> int | None:
    """How many channels the first audio stream has, or ``None`` if unknown."""
    try:
        process = await asyncio.create_subprocess_exec(
            _FFPROBE,
            "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=channels",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=20.0)
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        return int(stdout.decode().strip().splitlines()[0])
    except (ValueError, IndexError):
        return None


#: Bumped whenever the karaoke filter chain changes, so an old render in the
#: cache is never handed back for a new recipe.
KARAOKE_VERSION = 6

#: How much stereo a file needs for the centre-cancel strip to be trusted.
#:
#: Subtracting the channels leaves only the side signal, so what comes back is
#: however wide the mix was. Measured over real songs, the side sits around 20 dB
#: under the centre — strip those and the render is a thin residue that a
#: normaliser then blows up into hiss and pops, which is heard as the song going
#: silent. Only a genuinely wide mix, where the side is close to the centre, is
#: stripped; everything else is scooped, which always keeps the music audible.
STRIP_MAX_SIDE_DROP_DB = 12.0
#: And how much quieter than the original a stripped render may come out before
#: it is thrown away and redone with the safe chain.
KARAOKE_MIN_DROP_DB = 12.0
#: A render this quiet is broken whatever the original measured.
KARAOKE_SILENT_DB = -40.0
#: How for the render's mono downmix may fall below its stereo level. An
#: out-of-phase pair sums to nothing in mono, so a render that loses this much
#: is only audible on a stereo device — which is heard as the station going
#: silent on a phone speaker or a single-speaker Bluetooth box.
KARAOKE_MONO_MARGIN_DB = 6.0

#: The two halves of the stereo image, used to measure and to strip.
_MID_FILTER = "pan=mono|c0=0.5*c0+0.5*c1"
_SIDE_FILTER = "pan=mono|c0=0.5*c0-0.5*c1"

#: The vocal band a lead sits in, and the shelves that make the backing track
#: fill the space it leaves. Shared by both chains.
_VOCAL_BAND = "equalizer=f=2600:t=q:w=1.1:g=-8,equalizer=f=1100:t=q:w=1.4:g=-5"
_INSTRUMENT_BOOST = "bass=g=4,treble=g=3"
#: Normalise, then hold the peaks under the ceiling. The gain this replaces
#: clipped loud material, which is heard as pops rather than a louder track.
#: Both numbers sit below full scale so the lossy encode has room to overshoot
#: (a mastered song already peaks at 0 dB, where any overshoot would clip), and
#: ``m=4`` caps the lift at +12 dB so a nearly-empty residue can never be
#: amplified into noise: it stays too quiet, fails the check below, and gets
#: redone with the scoop chain instead.
_LEVEL = "dynaudnorm=f=250:g=15:p=0.8:m=4,alimiter=limit=0.85"

#: Scooping the vocal band works on anything: nothing is cancelled, so a song
#: with no stereo image is quieter in the voice range rather than silent. The
#: three notches straddle the range a lead sits in, which measured 13 dB off the
#: centred vocal band of a real track while the song kept its level. Boosting the
#: side instead of notching was tried and measured as no help at all: the
#: normaliser pulls the total back down and the ratio returns, so the notch is
#: what does the work.
_SCOOP_CHAIN = (
    "equalizer=f=2600:t=q:w=1.2:g=-13,equalizer=f=1100:t=q:w=1.5:g=-9,"
    f"equalizer=f=4200:t=q:w=1.4:g=-7,{_INSTRUMENT_BOOST},{_LEVEL}"
)
#: And the centre-cancel strip, which removes a centred lead cleanly but only
#: when the two channels genuinely differ. Two details decide between a karaoke
#: track and silence:
#:
#: * the side signal is sent to **both channels in phase**. The old
#:   ``c0=c0-c1|c1=c1-c0`` pair was a mirror image, so it summed to nothing the
#:   moment it was heard in mono — a phone speaker, a single-speaker Bluetooth
#:   box, any downmix — which is the "some songs just play silence" failure.
#: * the centre's **low end is mixed back in**. Kick and bass live dead centre
#:   and carry no lyric, so dropping them leaves a thin cymbal wash that gets
#:   heard as silence as well.
#: This one is a branched graph rather than a straight filter chain: the input
#: is split so the centre can be low-passed and mixed back into the side. A
#: branched graph only parses as a complex filtergraph, which is why it starts
#: with ``[0:a]`` and is rendered through ``-filter_complex``.
_STRIP_CHAIN = (
    "[0:a]asplit=2[side_in][mid_in];"
    f"[side_in]{_SIDE_FILTER}[side];"
    f"[mid_in]{_MID_FILTER}[mid];"
    "[mid]lowpass=f=140[low];"
    "[side][low]amix=inputs=2:duration=longest:normalize=0[mix];"
    # The 0.5 pans above leave the side 6 dB under the difference signal the old
    # chain used, and the level stage can only lift a render so far, so the gain
    # is put back here instead of leaning on ``maxgain``.
    "[mix]pan=stereo|c0=c0|c1=c0,volume=2,"
    f"{_VOCAL_BAND},{_INSTRUMENT_BOOST},{_LEVEL}"
)


def karaoke_chain(
    channels: int | None,
    *,
    mid_db: float | None = None,
    side_db: float | None = None,
) -> str:
    """The ffmpeg filter chain that strips a lead vocal and lifts the track.

    A stereo mix carries the vocal as a phantom centre — the same signal in
    both channels — so subtracting one channel from the other removes it. That
    only holds when the channels actually differ: an upload with the same audio
    in both (dual mono) cancels *everything* and plays as silence with a few
    encoder artefacts, which is exactly the "silent with pops" failure. The
    measured centre and side levels decide, and anything unmeasurable takes the
    safe chain.

    The scoop chain digs the vocal band out and lifts the rest, so a track with
    no stereo image comes back audibly karaoke instead of mute.
    """
    if channels is not None and channels < 2:
        return _SCOOP_CHAIN
    if mid_db is None or side_db is None:
        # No measurement to trust: never risk cancelling the whole song.
        return _SCOOP_CHAIN
    if side_db <= mid_db - STRIP_MAX_SIDE_DROP_DB:
        logger.info(
            "Not enough stereo to cancel (side %.1f dB vs centre %.1f dB) — scooping instead",
            side_db,
            mid_db,
        )
        return _SCOOP_CHAIN
    if channels is not None and channels > 2:
        # 5.1 and friends: drop the discrete centre channel as well, which is
        # where a film/music surround mix keeps the lead voice.
        return (
            "pan=stereo|c0=0.8*c0+0.5*c2-0.5*c1|c1=0.8*c1+0.5*c2-0.5*c0,"
            f"{_VOCAL_BAND},{_INSTRUMENT_BOOST},{_LEVEL}"
        )
    return _STRIP_CHAIN


async def _mean_volume_db(path: Path, audio_filter: str = "anull") -> float | None:
    """Mean level of a file in dB, optionally through a filter.

    ``volumedetect`` reports this on stderr, which is the cheapest way to ask
    "how loud is this" without decoding to a file of our own.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            _FFMPEG, "-v", "info", "-nostdin", "-i", str(path),
            "-af", f"{audio_filter},volumedetect",
            "-f", "null", "-",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=180.0)
    except (OSError, asyncio.TimeoutError):
        return None
    match = re.search(
        r"mean_volume:\s*(-?[\d.]+|-inf)\s*dB", stderr.decode(errors="ignore")
    )
    if not match:
        return None
    text = match.group(1)
    if text == "-inf":
        return -120.0
    try:
        return float(text)
    except ValueError:
        return None


async def channel_balance(path: Path) -> tuple[float | None, float | None]:
    """``(centre, side)`` mean levels in dB — a stereo image measurement."""
    mid = await _mean_volume_db(path, _MID_FILTER)
    side = await _mean_volume_db(path, _SIDE_FILTER)
    return mid, side


async def make_karaoke(source: Path, directory: Path | None = None) -> Path | None:
    """Render a vocal-suppressed, instrumental-forward copy of ``source``.

    The chain in :func:`karaoke_chain` does the work; this streams it out once
    and caches the result beside the original, named for
    :data:`KARAOKE_VERSION` so a recipe change never reuses an old render.

    ``None`` now means only that the render could not be produced at all — a
    missing file, or ffmpeg failing — not that the track is unsuitable, since
    a mono source is scooped rather than refused.
    """
    if source is None or not source.exists():
        return None
    # A separated instrumental, when this box can make one, beats anything the
    # chains below can do: the model takes the voice out rather than notching
    # the band it sits in. It is cached beside the song, so this is instant
    # from the second karaoke use onwards.
    ai_render = separate.instrumental_for(source)
    if ai_render is not None:
        logger.info("karaoke using the separated instrumental for %s", source.name)
        return ai_render
    # Beside the download by default, so a render shares the track's folder
    # instead of piling up in the cache root.
    directory = Path(directory) if directory is not None else source.parent
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / f"{source.stem}.karaoke.v{KARAOKE_VERSION}.m4a"
    if output.exists() and output.stat().st_size > 0:
        return output

    channels = await audio_channels(source)
    mid_db, side_db = await channel_balance(source) if (channels or 0) >= 2 else (None, None)
    source_db = await _mean_volume_db(source)

    async def render(chain: str) -> bool:
        # The strip chain is a branched graph over the input's audio, which only
        # parses as a complex filtergraph; the scoop chain is a straight line,
        # which -af takes directly.
        filters = (
            ("-filter_complex", chain)
            if chain.startswith("[0:a]")
            else ("-af", chain)
        )
        command = [
            _FFMPEG, "-y", "-v", "error",
            "-i", str(source),
            *filters,
            "-vn", "-c:a", "aac", "-b:a", "192k",
            str(output),
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=180.0)
        except (OSError, asyncio.TimeoutError) as error:
            logger.info("karaoke render failed for %s: %s", source, error)
            return False
        if process.returncode != 0:
            logger.info("karaoke ffmpeg error: %s", stderr.decode(errors="ignore")[:200])
            return False
        return output.exists() and output.stat().st_size > 0

    # The model takes minutes per song, so it is never waited for: the DSP
    # render plays now and the separated instrumental is picked up the next time
    # karaoke is used on this track. With nothing installed this is a no-op.
    if separate.available():
        separate.prepare(source)

    chain = karaoke_chain(channels, mid_db=mid_db, side_db=side_db)
    if not await render(chain):
        return None

    # Last line of defence against the silent render: a mix whose centre cancel
    # ate the song comes out far quieter than the file it came from, and is
    # redone with the scoop chain rather than played as near-silence. The mono
    # downmix is measured as well, since a render that only exists in stereo is
    # silence for anyone listening on one speaker.
    rendered_db = await _mean_volume_db(output)
    mono_db = await _mean_volume_db(output, _MID_FILTER)
    if chain != _SCOOP_CHAIN and (
        rendered_db is None
        or mono_db is None
        or rendered_db < KARAOKE_SILENT_DB
        or (source_db is not None and rendered_db < source_db - KARAOKE_MIN_DROP_DB)
        or mono_db < rendered_db - KARAOKE_MONO_MARGIN_DB
    ):
        logger.info(
            "Karaoke came out broken (%.1f dB render, %.1f dB in mono vs %.1f dB source) "
            "— redoing it with the scoop chain",
            rendered_db if rendered_db is not None else float("nan"),
            mono_db if mono_db is not None else float("nan"),
            source_db if source_db is not None else float("nan"),
        )
        if not await render(_SCOOP_CHAIN):
            return None
        chain = _SCOOP_CHAIN
        rendered_db = await _mean_volume_db(output)
        mono_db = await _mean_volume_db(output, _MID_FILTER)

    if rendered_db is not None and rendered_db < KARAOKE_SILENT_DB:
        # Nothing audible came out of either chain: the untouched mix is better
        # than silence, so the caller plays the original file instead.
        logger.info(
            "Karaoke render for %s is silent (%.1f dB) — playing the original mix",
            source.name,
            rendered_db,
        )
        return None

    logger.info(
        "karaoke rendered %s (%s channel(s), %s, v%s, %.1f dB)",
        output.name,
        channels,
        "vocal scoop" if chain == _SCOOP_CHAIN else "centre-cancel",
        KARAOKE_VERSION,
        rendered_db if rendered_db is not None else float("nan"),
    )
    return output


# -- playback filters ------------------------------------------------------
#: Named presets ``,radio filter <name>`` puts in front of the music. Each chain
#: is an ffmpeg audio filter run on the voice stream, so a preset stacks with
#: karaoke (which swaps the *file*, not the filters) and with the volume
#: transform (which is applied after ffmpeg, so it can never be clipped here).
#: Every preset that lifts anything ends in the limiter: a master that already
#: peaks at full scale would otherwise clip where the boost pushes it past 0 dB,
#: which is heard as crackle rather than a louder track.
PLAYBACK_FILTERS: dict[str, tuple[str, str]] = {
    "off": ("Off", ""),
    "bass": ("Bass boost", "bass=g=6:f=110:w=0.7,alimiter=limit=0.9:level=false"),
    "deep": ("Deep bass", "bass=g=12:f=85:w=0.6,alimiter=limit=0.9:level=false"),
    "clarity": ("Clarity", "treble=g=5:f=6000,alimiter=limit=0.9:level=false"),
    "vocal": (
        "Vocal forward",
        "equalizer=f=2600:t=q:w=1.2:g=4,bass=g=2,alimiter=limit=0.9:level=false",
    ),
    "club": ("Club", "bass=g=9:f=95:w=0.7,treble=g=4:f=7000,alimiter=limit=0.9:level=false"),
    "loud": ("Loud", "dynaudnorm=f=250:g=15:p=0.9:m=6,alimiter=limit=0.9:level=false"),
}


# -- cobalt ----------------------------------------------------------------
async def _resolve_cobalt(
    session: aiohttp.ClientSession, query: str, directory: Path
) -> Track | None:
    """Ask a cobalt instance for the audio of ``query``."""
    if not is_url(query):
        return None
    # The public instance authenticates every request now; skip it when no key
    # is set rather than burning a round trip on a guaranteed 401.
    if not COBALT_API_KEY and "api.cobalt.tools" in COBALT_API_URL:
        return None

    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if COBALT_API_KEY:
        headers["Authorization"] = f"Api-Key {COBALT_API_KEY}"
    payload = {"url": query, "downloadMode": "audio", "audioFormat": "mp3"}

    try:
        async with session.post(
            COBALT_API_URL + "/", json=payload, headers=headers, timeout=HTTP_TIMEOUT
        ) as response:
            if response.status >= 400:
                logger.info("cobalt returned HTTP %s", response.status)
                return None
            data = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
        logger.info("cobalt request failed: %s", error)
        return None

    if not isinstance(data, dict) or data.get("status") not in ("tunnel", "redirect"):
        return None
    stream = data.get("url")
    if not stream:
        return None

    title = Path(str(data.get("filename") or "")).stem or _title_from_url(query)
    path = await _download(session, stream, directory, title)
    if path is None:
        return None
    return Track(
        title=title,
        source_url=query,
        source="cobalt",
        path=path,
        duration=await probe_duration(path),
    )


# -- yt-dlp ----------------------------------------------------------------
class _QuietLogger:
    """Swallow yt-dlp's console chatter so a failed grab doesn't spam stdout."""

    def debug(self, message: str) -> None:  # pragma: no cover - noise only
        pass

    def warning(self, message: str) -> None:  # pragma: no cover
        pass

    def error(self, message: str) -> None:  # pragma: no cover
        logger.debug("yt-dlp: %s", message)


def _first_entry(info: dict[str, Any] | None) -> dict[str, Any] | None:
    if not info:
        return None
    entries = info.get("entries")
    if entries:
        return next((entry for entry in entries if entry), None)
    return info


def _downloaded_path(info: dict[str, Any], ydl: Any) -> Path | None:
    for download in info.get("requested_downloads") or []:
        candidate = download.get("filepath")
        if candidate and Path(candidate).exists():
            return Path(candidate)
    candidate = info.get("filepath")
    if candidate and Path(candidate).exists():
        return Path(candidate)
    try:
        guess = ydl.prepare_filename(info)
    except Exception:  # pragma: no cover - yt-dlp internals vary by version
        guess = None
    return Path(guess) if guess and Path(guess).exists() else None


def _ytdlp_sync(
    query: str, directory: Path, prefix: str = "scsearch1"
) -> tuple[dict[str, Any], Path] | None:
    """Blocking yt-dlp grab; always run through :func:`asyncio.to_thread`.

    ``prefix`` picks the search backend for a name (``scsearch1`` for a
    SoundCloud match, ``ytsearch1`` when the caller wants YouTube — which is
    what a Spotify link resolves to, since Spotify has no audio to download).
    """
    try:
        import yt_dlp
    except Exception:  # pragma: no cover - declared dependency, but stay importable
        return None

    directory.mkdir(parents=True, exist_ok=True)
    target = query if is_url(query) else f"{prefix}:{query}"
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "outtmpl": str(directory / "%(title).80s.%(ext)s"),
        "default_search": "scsearch5",
        "retries": 3,
        "socket_timeout": 30,
        "nocheckcertificate": True,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) radio",
        "logger": _QuietLogger(),
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(target, download=True)
            entry = _first_entry(info)
            if entry is None:
                return None
            path = _downloaded_path(entry, ydl)
            if path is None:
                return None
    except Exception as error:
        # yt-dlp raises for the routine failures too — a DRM-protected
        # SoundCloud track, a video that has been taken down — so this reports
        # them the same way every caller already expects: as "nothing came
        # back", which lets the resolver chain and autoplay move on.
        logger.info("yt-dlp could not grab %r: %s", query, error)
        return None
    return entry, path


#: How many entries of a pasted playlist are read into the queue at once.
PLAYLIST_LIMIT = 20

#: A file at or under this length is treated as a trailer rather than a song
#: when the source could not say how long it should be — SoundCloud's preview
#: clips are 30 seconds, and this covers the few seconds either side of that.
PREVIEW_MAX_SECONDS = 45.0
#: And a file this much shorter than the source claimed is a preview too, no
#: matter how long it is.
PREVIEW_RATIO = 0.7


def looks_like_preview(expected: float | None, actual: float | None) -> bool:
    """Whether a downloaded file is a clip of the song rather than the song.

    ``expected`` is the length the catalogue advertised. A 30-second file for a
    three-minute song is the SoundCloud preview case — it plays like the track
    stopping early — and a file well under what was promised is the same thing
    with a different number.
    """
    if not actual or actual <= 0:
        return False
    if expected and expected > 0:
        return actual < expected * PREVIEW_RATIO or (
            actual <= PREVIEW_MAX_SECONDS and expected > actual * 2
        )
    # Nothing was advertised to compare against: only the tell-tale 30 seconds.
    return actual <= PREVIEW_MAX_SECONDS


async def _grab(
    session: aiohttp.ClientSession,
    query: str,
    directory: Path,
    prefix: str = "scsearch1",
    *,
    expected: float | None = None,
    title: str = "",
    artist: str = "",
) -> tuple[dict[str, Any], Path] | None:
    """Download with yt-dlp, swapping a preview clip for the full song.

    The file is measured against the length the catalogue advertised; when it
    comes up short the same song is fetched from YouTube instead, and the longer
    of the two is what the caller gets. A failed upgrade keeps the original
    file, so this can only ever improve what plays.

    A song this directory already holds under the same name is reused instead,
    which is what makes asking for something twice instant: the title has to
    match exactly (punctuation and case aside) and the file has to be at least
    as long as the catalogue promised, so an unrelated or half-finished file is
    never mistaken for the song.
    """
    cached = await cached_name_match(directory, title or query, expected)
    if cached is not None:
        logger.info("Radio served %r from the download cache", title or query)
        return {"title": title or query, "duration": expected}, cached

    result = await asyncio.to_thread(_ytdlp_sync, query, directory, prefix)
    if result is None:
        return None
    info, path = result
    actual = await probe_duration(path)
    claimed = expected if expected is not None else info.get("duration")
    if not looks_like_preview(claimed, actual):
        return info, path

    logger.info(
        "Short grab for %r: %ss of %ss — looking for the whole song",
        title or query,
        actual,
        claimed,
    )
    name = f"{title} {artist}".strip() or query
    better = await asyncio.to_thread(_ytdlp_sync, name, directory, "ytsearch1")
    if better is None:
        return info, path
    better_info, better_path = better
    better_actual = await probe_duration(better_path)
    if better_actual is not None and better_actual > (actual or 0.0):
        logger.info("Full version found: %ss", better_actual)
        return better_info, better_path
    return info, path


async def _resolve_ytdlp(
    session: aiohttp.ClientSession, query: str, directory: Path, *, prefix: str = "scsearch1"
) -> Track | None:
    result = await _grab(session, query, directory, prefix)
    if result is None:
        return None
    info, path = result
    return Track(
        title=info.get("track") or info.get("title") or path.stem,
        artist=info.get("artist") or info.get("uploader") or "",
        duration=info.get("duration") or await probe_duration(path),
        artwork=info.get("thumbnail"),
        source_url=info.get("webpage_url") or (query if is_url(query) else None),
        source="yt-dlp",
        path=path,
    )


# -- spotify ---------------------------------------------------------------
async def _spotify_metadata(
    session: aiohttp.ClientSession, url: str
) -> tuple[str, str, str | None]:
    """``(title, artist, artwork)`` for a Spotify track link, without a key.

    oEmbed is public and carries the title and cover; the artist names only
    live in the embed page's JSON, so that is parsed with one regex.
    """
    track_id = spotify_track_id(url)
    if not track_id:
        return "", "", None

    title = ""
    artwork: str | None = None
    oembed = await _get_json(session, "https://open.spotify.com/oembed", {"url": url})
    if isinstance(oembed, dict):
        title = str(oembed.get("title") or "").strip()
        artwork = oembed.get("thumbnail_url") or None

    artist = ""
    try:
        async with session.get(
            f"https://open.spotify.com/embed/track/{track_id}",
            timeout=SEARCH_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        ) as response:
            if response.status == 200:
                page = await response.text()
                match = re.search(r'"artists":\[\{"name":"([^"]+)"', page)
                if match:
                    artist = match.group(1)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        pass
    return title, artist, artwork


async def _resolve_spotify(
    session: aiohttp.ClientSession, query: str, directory: Path
) -> Track | None:
    """Turn a Spotify track link into the audio of that song.

    Spotify serves no downloadable audio, so the link is read for its title and
    artist and the song is then found on YouTube (the tracks API is the
    backstop). The card keeps pointing at the Spotify link.
    """
    if not is_spotify_url(query):
        return None
    title, artist, artwork = await _spotify_metadata(session, query)
    if not title:
        return None

    search_query = f"{title} {artist}".strip()
    track = await _resolve_ytdlp(session, search_query, directory, prefix="ytsearch1")
    if track is None:
        track = await _resolve_tracks(session, search_query, directory)
    if track is None:
        return None

    track.title = title
    if artist:
        track.artist = artist
    track.artwork = artwork or track.artwork
    track.source_url = query
    track.source = "spotify"
    return track


# -- tracks API (the ``,np`` / ``getsong`` snippet grabber) --------------
async def _resolve_tracks(
    session: aiohttp.ClientSession, query: str, directory: Path
) -> Track | None:
    """Resolve through the client the snippet feature is built on.

    This is the API behind ``refsonggrab.js``'s ``snippet.deliver``: search the
    catalogue, pick the closest title, then stream the whole song down. Reusing
    ``utils.core.tracks`` keeps the ranking and the endpoint in one place
    instead of re-implementing them here.
    """
    if is_url(query):
        return None
    results = await snippet_api.search(session, query)
    if not results:
        return None
    track = snippet_api.best_match(results, query, "") or results[0]
    path = await snippet_api.download(session, track, directory)
    if path is None:
        return None
    actual = await probe_duration(path)
    if looks_like_preview(track.duration, actual):
        # The catalogue served a clip; the full song is almost always on
        # YouTube, so that is what gets played instead.
        grabbed = await _grab(
            session,
            f"{track.title} {track.artist}".strip(),
            directory,
            "ytsearch1",
            expected=track.duration,
            title=track.title,
            artist=track.artist,
        )
        if grabbed is not None:
            info, better = grabbed
            better_actual = await probe_duration(better)
            if better_actual is not None and better_actual > (actual or 0.0):
                return Track(
                    title=track.title or query,
                    artist=track.artist,
                    duration=better_actual,
                    artwork=track.artwork or info.get("thumbnail"),
                    source="yt-dlp",
                    path=better,
                )
    return Track(
        title=track.title or query,
        artist=track.artist,
        duration=actual or track.duration,
        artwork=track.artwork,
        source="tracks",
        path=path,
        api_id=track.id,
    )


async def resolve(
    session: aiohttp.ClientSession,
    query: str,
    directory: Path = CACHE_DIR,
    *,
    requester_id: int | None = None,
) -> Track | None:
    """Find and download ``query``, trying each backend until one lands.

    Links try cobalt then yt-dlp; names try the tracks API then yt-dlp. A
    resolver that raises is logged and skipped, so one dead backend never takes
    the whole command down.
    """
    query = (query or "").strip()
    if not query:
        return None

    if is_url(query):
        # Spotify first: its links are metadata only, so the real audio comes
        # from a YouTube search, and skipping cobalt/yt-dlp's direct attempt on
        # the Spotify URL saves two guaranteed failures.
        resolvers: tuple[Any, ...] = (_resolve_spotify, _resolve_cobalt, _resolve_ytdlp)
    else:
        resolvers = (_resolve_tracks, _resolve_ytdlp)

    for resolver in resolvers:
        try:
            track = await resolver(session, query, directory)
        except Exception as error:
            # A resolver failing is routine (a dead link, a DRM track), so this
            # stays a one-liner rather than a full traceback.
            logger.info("radio %s could not grab %r: %s", resolver.__name__, query, error)
            continue
        if track is not None and track.path is not None:
            track.requester_id = requester_id
            return track

    # The link itself is unplayable (SoundCloud DRM, a dead post, ...). Fall
    # back to the name hidden in the URL and search for it like any other query.
    if is_url(query):
        name = query_from_url(query)
        if name:
            for resolver in (_resolve_tracks, _resolve_ytdlp):
                try:
                    track = await resolver(session, name, directory)
                except Exception:
                    continue
                if track is not None and track.path is not None:
                    track.requester_id = requester_id
                    logger.info("radio played %r via its name (%s)", name, track.source)
                    return track
    return None


def _search_prefix(source: str) -> str:
    """yt-dlp's search backend for a source name."""
    return "ytsearch" if str(source).lower().startswith("you") else "scsearch"


def _ytdlp_playlist_sync(url: str, limit: int) -> tuple[str, list[dict[str, Any]]]:
    """Name and entries of a YouTube playlist or SoundCloud set.

    yt-dlp walks the collection itself, so this stays metadata-only: the
    entries come back as ordinary track records and are downloaded one at a
    time as they reach the front of the queue.
    """
    try:
        import yt_dlp
    except Exception:  # pragma: no cover - declared dependency, but stay importable
        return "", []

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "noplaylist": False,
        "playlistend": max(1, limit),
        "ignoreerrors": True,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) radio",
        "logger": _QuietLogger(),
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as error:
        logger.info("Could not read the playlist %r: %s", url, error)
        return "", []
    if not isinstance(info, dict):
        return "", []
    entries = [entry for entry in (info.get("entries") or []) if entry][:limit]
    return str(info.get("title") or "").strip(), entries


async def _resolve_playlist(
    session: aiohttp.ClientSession, query: str, limit: int
) -> tuple[str, list[Track]]:
    """Read a pasted playlist into a name and playable track records.

    Spotify playlists are read from their public embed page — no credentials —
    while YouTube playlists and SoundCloud sets are walked by yt-dlp. Every
    entry comes back as metadata; the audio is fetched as it plays, so pasting
    a playlist never downloads one before anyone hears it.
    """
    source = playlist_source(query)
    if source is None:
        return "", []
    if source == "spotify":
        return await _spotify_playlist(session, query, limit)

    name, entries = await asyncio.to_thread(_ytdlp_playlist_sync, query, limit)
    tracks: list[Track] = []
    for entry in entries:
        url = entry.get("url") or entry.get("webpage_url")
        if not url:
            continue
        tracks.append(
            Track(
                title=entry.get("track") or entry.get("title") or _title_from_url(url),
                artist=entry.get("artist") or entry.get("uploader") or "",
                duration=entry.get("duration"),
                artwork=entry.get("thumbnail"),
                source_url=url,
                source="yt-dlp",
            )
        )
    return name or "Playlist", tracks


def _ytdlp_search_sync(query: str, limit: int, source: str = "soundcloud") -> list[dict[str, Any]]:
    try:
        import yt_dlp
    except Exception:  # pragma: no cover
        return []
    prefix = _search_prefix(source)
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "default_search": prefix,
        "noplaylist": True,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) radio",
        "logger": _QuietLogger(),
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(f"{prefix}{limit}:{query}", download=False)
    except Exception:
        return []
    entries = (info or {}).get("entries") or []
    # A search can hand back a channel or a playlist alongside its videos; they
    # carry no audio of their own, so dropping them here keeps `search` (and
    # the autoplay that leans on it) from trying to download a page.
    unwelcome = ("/channel/", "/user/", "/playlist", "/c/")
    return [
        entry
        for entry in entries
        if entry
        and not any(part in (entry.get("url") or "") for part in unwelcome)
    ][:limit]


async def search(
    session: aiohttp.ClientSession,
    query: str,
    *,
    limit: int = 12,
    source: str = "soundcloud",
) -> list[Track]:
    """Metadata-only search, used for autoplay and ``search``.

    ``source`` picks the backend: SoundCloud by default, YouTube when autoplay
    needs a wider net than SoundCloud can cast for a small artist.
    """
    entries = await asyncio.to_thread(_ytdlp_search_sync, query, limit, source)
    tracks: list[Track] = []
    for entry in entries:
        tracks.append(
            Track(
                title=entry.get("track") or entry.get("title") or _title_from_url(entry.get("url") or ""),
                artist=entry.get("artist") or entry.get("uploader") or "",
                duration=entry.get("duration"),
                artwork=entry.get("thumbnail"),
                source_url=entry.get("webpage_url") or entry.get("url"),
                source="yt-dlp",
            )
        )
    return tracks


async def _download_catalogue(
    session: aiohttp.ClientSession, track: Track, directory: Path
) -> Path | None:
    """Fetch a song the API knows by its id — the whole track, not a clip."""
    if not track.api_id:
        return None
    artists = [name.strip() for name in (track.artist or "").split(",") if name.strip()]
    api_track = snippet_api.Track(
        id=track.api_id,
        title=track.title,
        artists=artists,
        duration=track.duration,
        artwork=track.artwork,
    )
    return await snippet_api.download(session, api_track, directory)


async def search_catalogue(
    session: aiohttp.ClientSession, query: str, *, limit: int = 10
) -> list[Track]:
    """Search the song API — the one behind the ``,np`` getsong grabber.

    These are whole songs, so they are the first thing the station reaches for;
    the ids they carry let :func:`download` fetch them without a web link.
    """
    query = (query or "").strip()
    if not query or is_url(query):
        return []
    try:
        found = await snippet_api.search(session, query)
    except Exception as error:
        logger.info("Song API search failed for %r: %s", query, error)
        return []
    return [
        Track(
            title=item.title,
            artist=item.artist,
            duration=item.duration,
            artwork=item.artwork,
            api_id=item.id,
            source="tracks",
        )
        for item in found[:limit]
    ]


def _same_song(left: str, right: str) -> bool:
    """Loose title match: case, punctuation and spacing ignored."""
    clean = lambda value: re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()
    left, right = clean(left), clean(right)
    return bool(left) and (left == right or left in right or right in left)


async def _catalogue_match(
    session: aiohttp.ClientSession, track: Track
) -> Track | None:
    """The song API's version of ``track``, matched on its name."""
    query = f"{track.title} {track.artist}".strip()
    if not query:
        return None
    matches = await search_catalogue(session, query, limit=5)
    for candidate in matches:
        if _same_song(track.title, candidate.title):
            return candidate
    return matches[0] if matches else None


async def resolve_playlist(
    session: aiohttp.ClientSession,
    query: str,
    *,
    limit: int = PLAYLIST_LIMIT,
    requester_id: int | None = None,
) -> tuple[str, list[Track]]:
    """``(name, tracks)`` for a pasted playlist, or ``("", [])`` if unreadable."""
    query = (query or "").strip()
    if not query or playlist_source(query) is None:
        return "", []
    name, tracks = await _resolve_playlist(session, query, limit)
    for track in tracks:
        track.requester_id = requester_id
    return name, tracks


async def download(session: aiohttp.ClientSession, track: Track, directory: Path = CACHE_DIR) -> Track | None:
    """Download a metadata-only track (as returned by :func:`search`).

    The song API comes first and SoundCloud is the fallback: an API entry is a
    whole song, whereas a SoundCloud one may be a 30-second preview. Either way
    the file is measured against the length the catalogue advertised, so a clip
    can never end up in the queue looking like the song.
    """
    if track.path is not None and track.path.exists():
        return track

    if track.api_id:
        path = await _download_catalogue(session, track, directory)
        if path is not None:
            actual = await probe_duration(path)
            if not looks_like_preview(track.duration, actual):
                track.path = path
                track.duration = actual or track.duration
                return track
            logger.info(
                "Song API served a short cut of %r — falling back to SoundCloud",
                track.title,
            )

    if not track.source_url:
        # A track with no link of its own — a Spotify playlist entry, say. The
        # song API is asked for it by name before falling back to SoundCloud.
        match = await _catalogue_match(session, track)
        if match is not None:
            path = await _download_catalogue(session, match, directory)
            if path is not None:
                actual = await probe_duration(path)
                if not looks_like_preview(match.duration, actual):
                    track.path = path
                    track.duration = actual or match.duration or track.duration
                    track.artwork = track.artwork or match.artwork
                    return track
                logger.info("Song API served a short cut of %r too", track.title)

    query = (track.source_url or f"{track.title} {track.artist}").strip()
    if not query:
        return None
    result = await _grab(
        session,
        query,
        directory,
        expected=track.duration,
        title=track.title,
        artist=track.artist,
    )
    if result is None:
        return None
    info, path = result
    actual = await probe_duration(path)
    track.path = path
    # The file is the truth, not the catalogue entry: a song that plays for a
    # minute should say a minute on the card.
    track.duration = actual or track.duration or info.get("duration")
    track.artwork = track.artwork or info.get("thumbnail")
    return track


# ---------------------------------------------------------------------------
# Timed lyrics
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Lyrics:
    plain: str
    synced: list[tuple[float, str]]
    source: str

    @property
    def is_synced(self) -> bool:
        return bool(self.synced)


def parse_lrc(text: str) -> list[tuple[float, str]]:
    """Turn ``[mm:ss.xx] line`` stamps into sorted ``(seconds, line)`` pairs.

    A line can carry several stamps (a repeated chorus), and word-level tags
    (``<mm:ss.xx>``) are dropped so only the line text remains.
    """
    parsed: list[tuple[float, str]] = []
    for raw in (text or "").splitlines():
        raw = _WORD_TAG.sub("", raw)
        stamps = list(_TIMESTAMP.finditer(raw))
        if not stamps:
            continue
        content = raw[stamps[-1].end():].strip()
        for stamp in stamps:
            minutes = int(stamp.group(1))
            seconds = float(stamp.group(2).replace(":", "."))
            parsed.append((minutes * 60 + seconds, content))
    parsed.sort(key=lambda item: item[0])
    return parsed


def line_index(synced: list[tuple[float, str]], position: float) -> int:
    """Index of the line playing at ``position``, or ``-1`` before the first."""
    if not synced:
        return -1
    low, high, best = 0, len(synced) - 1, -1
    while low <= high:
        middle = (low + high) // 2
        if synced[middle][0] <= position:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return best


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


def _lyrics_from_record(record: dict[str, Any], source: str) -> Lyrics | None:
    synced = parse_lrc(record.get("syncedLyrics") or "")
    plain = (record.get("plainLyrics") or "").strip()
    if synced:
        return Lyrics(plain=plain or "\n".join(text for _, text in synced), synced=synced, source=source)
    if plain:
        return Lyrics(plain=plain, synced=[], source=source)
    return None


async def fetch_lyrics(
    session: aiohttp.ClientSession,
    *,
    title: str,
    artist: str = "",
    duration: float | None = None,
) -> Lyrics | None:
    """Timed lyrics from lrclib, falling back to a plain transcript."""
    title = (title or "").strip()
    artist = (artist or "").strip()
    if not title:
        return None

    params: dict[str, Any] = {"track_name": title}
    if artist:
        params["artist_name"] = artist
    if duration and duration > 0:
        params["duration"] = int(round(duration))
    exact = await _get_json(session, f"{LRCLIB_API}/get", params)
    if isinstance(exact, dict):
        found = _lyrics_from_record(exact, "lrclib")
        if found is not None:
            return found

    # `/get` needs an exact duration; a search tolerates a slightly off title.
    query = f"{artist} {title}".strip()
    results = await _get_json(session, f"{LRCLIB_API}/search", {"q": query})
    if isinstance(results, list) and results:
        best = next(
            (item for item in results if item.get("syncedLyrics")),
            results[0],
        )
        found = _lyrics_from_record(best, "lrclib")
        if found is not None:
            return found

    if artist:
        fallback = await _get_json(
            session, f"{LYRICS_OVH_API}/{quote(artist)}/{quote(title)}"
        )
        if isinstance(fallback, dict) and (fallback.get("lyrics") or "").strip():
            return Lyrics(plain=fallback["lyrics"].strip(), synced=[], source="lyrics.ovh")
    return None
