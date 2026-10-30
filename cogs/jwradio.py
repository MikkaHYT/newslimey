import asyncio
import html
import json
import logging
import os
import random
import re
import subprocess
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlparse

import aiohttp
import discord
from discord.ext import commands

from utils.core.prefixes import prefix_for


logger = logging.getLogger("slimey.jwradio")

API_ROOT = "https://juicewrldapi.com"
API_BASE = f"{API_ROOT}/juicewrld"
SONGS_ENDPOINT = f"{API_BASE}/songs/"
BROWSE_ENDPOINT = f"{API_BASE}/files/browse/"
FILE_INFO_ENDPOINT = f"{API_BASE}/files/info/"
DOWNLOAD_ENDPOINT = f"{API_BASE}/files/download/"
RADIO_RANDOM_ENDPOINT = f"{API_BASE}/radio/random/"

API_TIMEOUT = 30
MAX_RESULTS = 25

EMBED_COLOR = discord.Color.from_str("#2b2d31")

RADIO_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "jwradio_settings.json"
UPLOADS_DIR = Path(__file__).resolve().parent.parent / "jwradio_uploads"
UPLOADS_META_PATH = Path(__file__).resolve().parent.parent / "jwradio_uploads.json"
#: The host answers a bot-looking User-Agent with a gzip'd error page that
#: refuses byte ranges, so streaming uses a normal browser agent.
AUDIO_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
#: Byte ranges pulled at once while a track streams. The host throttles each
#: connection below the bitrate of the MP3s it serves, so one plain read starves
#: ffmpeg and the audio stutters; several ranges run well above realtime.
STREAM_CONNECTIONS = 4
STREAM_WINDOW = 256 * 1024
#: ffmpeg's stdin buffer, raised so a short network hiccup doesn't starve it.
STREAM_PIPE_BYTES = 1024 * 1024

FFMPEG = "ffmpeg"

AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac")

#: The radio's filters, as ffmpeg ``-af`` chains: name -> (label, what it does,
#: chain). Chains are comma-separated with no spaces anywhere, because
#: discord.py splits the player's option string on whitespace. ``None`` means
#: the track plays exactly as it was uploaded.
FILTERS: dict[str, tuple[str, str, Optional[str]]] = {
    "off": ("Off", "the track exactly as it was uploaded", None),
    "bass": ("Bass boost", "the low end pushed up about 12 dB", "bass=g=12"),
    "nightcore": (
        "Nightcore",
        "sped up 25%, with the pitch dragged up to match",
        "asetrate=48000*1.25,aresample=48000,atempo=1.25",
    ),
    "slowed": (
        "Slowed",
        "dragged down to 85% speed, pitch dropping with it",
        "asetrate=48000*0.85,aresample=48000,atempo=0.85",
    ),
    "vaporwave": (
        "Vaporwave",
        "slowed, bass-heavy and echoing",
        "asetrate=48000*0.8,aresample=48000,atempo=0.8,bass=g=6,"
        "aecho=0.8:0.9:50:0.3",
    ),
    "8d": ("8D", "the track circling around your head", "apulsator=hz=0.08"),
    "lofi": ("Lo-fi", "muffled like a tape copy", "lowpass=f=3200"),
    "reverb": (
        "Reverb",
        "a room-sized tail on every hit",
        "aecho=0.8:0.88:60:0.4",
    ),
}


QUEUE_HINT = (
    "-# **Queue a song:** just type the song name in this channel -- "
    "or drop an MP3 file here and it'll be queued up next!"
)


def text_display(content: str) -> discord.ui.TextDisplay:
    return discord.ui.TextDisplay(content=content)


def small_separator() -> discord.ui.Separator:
    return discord.ui.Separator(spacing=discord.SeparatorSpacing.small)


def simple_view(content: str, *, timeout: int = 60) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=timeout)
    container = discord.ui.Container(accent_color=EMBED_COLOR)
    container.add_item(text_display(content))
    view.add_item(container)
    return view


def clean(value: Any):
    if value is None:
        return None
    if callable(value):
        try:
            value = value()
        except TypeError:
            return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    return value


def get_value(obj: Any, *names: str, default=None):
    if obj is None:
        return default
    for name in names:
        if isinstance(obj, dict):
            value = obj.get(name)
        else:
            value = getattr(obj, name, None)
        value = clean(value)
        if value is not None:
            return value
    return default


def recursive_find(obj: Any, names):
    wanted = {str(x).casefold() for x in names}
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).casefold() in wanted:
                value = clean(value)
                if value is not None:
                    return value
        for value in obj.values():
            found = recursive_find(value, names)
            if found is not None:
                return found
    elif isinstance(obj, (list, tuple, set)):
        for item in obj:
            found = recursive_find(item, names)
            if found is not None:
                return found
    return None


def first_value(obj: Any, *names: str, default=None):
    value = get_value(obj, *names, default=None)
    if value is not None:
        return value
    value = recursive_find(obj, names)
    if value is not None:
        return value
    return default


def display(value: Any, default="N/A"):
    value = clean(value)
    if value is None:
        return default
    if isinstance(value, (list, tuple, set)):
        values = []
        for item in value:
            result = display(item, "")
            if result:
                values.append(result)
        return ", ".join(values) if values else default
    if isinstance(value, dict):
        for key in ("name", "title", "value", "text", "label"):
            if key in value:
                result = display(value[key], "")
                if result:
                    return result
        return default
    return str(value)


def display_list(value: Any, default="N/A"):
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip() or default
    if isinstance(value, (list, tuple, set)):
        values = []
        for item in value:
            result = display(item, "")
            if result:
                values.append(result)
        return ", ".join(values) if values else default
    return display(value, default)


def unwrap_named(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("name") or value.get("title") or value.get("value")
    return value


def normalize_title(value: str):
    if not value:
        return ""
    value = str(value).lower()
    value = re.sub(r"\([^)]*\)", "", value)
    value = re.sub(r"\[[^\]]*\]", "", value)
    value = re.sub(r"\{[^}]*\}", "", value)
    value = re.sub(r"\.(mp3|m4a|wav|flac|ogg|aac)$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"[_\-]+", " ", value)
    value = re.sub(r"[^\w\s]", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def title_matches(wanted: str, filename: str):
    a = normalize_title(wanted)
    b = normalize_title(filename)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    return False


def valid_http_url(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    try:
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return None
    except Exception:
        return None
    return value


def normalize_path(path):
    if not path:
        return None
    path = str(path).strip()
    if not path or path.startswith(("http://", "https://")):
        return None
    while path.startswith("/"):
        path = path[1:]
    return path


def make_download_url(path):
    path = normalize_path(path)
    if not path:
        return None
    return f"{DOWNLOAD_ENDPOINT}?path={quote(path, safe='')}&channel=comp"


def clean_era_display(era_raw: Any) -> str | None:
    if not era_raw:
        return None
    val = ""
    if isinstance(era_raw, dict):
        val = era_raw.get("description") or era_raw.get("name") or era_raw.get("title") or ""
    elif isinstance(era_raw, str):
        val = era_raw
    if not val or val == "N/A":
        return None
    val = re.sub(r"\s*\([^)]*\)", "", val)
    val = re.sub(r"\s+era$", "", val, flags=re.IGNORECASE)
    return val.strip() or None


def is_post_era(era_name: str) -> bool:
    if not era_name:
        return False
    norm = era_name.strip().lower()
    if norm in ("post", "post-era", "post era"):
        return True
    return bool(re.search(r"\bpost(?:[- ]?mortem|[- ]?death|humous)?\b", norm))


def song_title(song):
    return display(
        first_value(
            song,
            "name",
            "title",
            "song_title",
            "songTitle",
            default="Unknown Song",
        ),
        "Unknown Song",
    )


def song_category(song):
    value = first_value(
        song,
        "category",
        "category_name",
        "categoryName",
        "status",
        "leak_type",
        default=None,
    )
    if isinstance(value, dict):
        value = value.get("name") or value.get("title") or value.get("value")
    return display(value)


def song_era(song):
    value = first_value(
        song,
        "era",
        "era_name",
        "eraName",
        default=None,
    )
    return clean_era_display(value) or display(value)


def song_producers(song):
    return display_list(
        first_value(
            song,
            "producers",
            "producer",
            "producer_names",
            "producerNames",
            default=None,
        )
    )


def song_engineers(song):
    return display_list(
        first_value(
            song,
            "engineers",
            "engineer",
            "engineer_names",
            "engineerNames",
            default=None,
        )
    )


def song_length(song):
    value = first_value(
        song,
        "length",
        "duration",
        "duration_text",
        "durationText",
        default=None,
    )
    if isinstance(value, (int, float)):
        seconds = int(value)
        minutes, seconds = divmod(seconds, 60)
        return f"{minutes}:{seconds:02d}"
    return display(value)


def song_location(song):
    value = first_value(
        song,
        "recording_locations",
        "recordingLocations",
        "recording_location",
        "recordingLocation",
        "location",
        default=None,
    )
    if isinstance(value, dict):
        value = value.get("name") or value.get("location") or value.get("value")
    return display(value)


def song_recorded(song):
    value = display_list(
        first_value(
            song,
            "record_dates",
            "recordDates",
            "recording_dates",
            "recordingDates",
            "recording_date",
            "recordingDate",
            "recorded",
            "recorded_date",
            "recordedDate",
            default=None,
        )
    )
    if value == "N/A":
        return value
    return strip_recorded_prefix(value)


def song_alt_names(song):
    value = display_list(
        first_value(
            song,
            "track_titles",
            "trackTitles",
            "alternative_titles",
            "alternativeTitles",
            "alternate_titles",
            "alternateTitles",
            default=None,
        )
    )
    if value == "N/A":
        return value
    title = song_title(song).casefold()
    parts = [part.strip() for part in value.split(",")]
    filtered = [part for part in parts if part and part.casefold() != title]
    return ", ".join(filtered) if filtered else "N/A"


def strip_recorded_prefix(value: str) -> str:
    cleaned = re.sub(
        r"^recorded[:\\s-]*",
        "",
        value.strip(),
        count=1,
        flags=re.IGNORECASE,
    ).strip()
    return cleaned or value


def parse_api_json(text: str) -> Any:
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass

    pre_match = re.search(r"<pre[^>]*>(.*?)</pre>", text, re.DOTALL | re.IGNORECASE)
    if pre_match:
        inner = html.unescape(pre_match.group(1).strip())
        try:
            return json.loads(inner)
        except Exception:
            pass

    return None


def resolve_cover_url(value):
    direct = valid_http_url(value)
    if direct:
        return direct
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if value.startswith("//"):
        return f"https:{value}"
    if value.startswith("/"):
        return f"https://juicewrldapi.com{value}"
    if value.startswith(("images/", "media/")):
        return f"https://juicewrldapi.com/{value}"
    return None


def get_cover_url(song):
    fields = (
        "image_url", "imageUrl", "imageURL", "image",
        "cover_url", "coverUrl", "coverURL",
        "cover_art_url", "coverArtUrl",
        "artwork_url", "artworkUrl",
        "thumbnail_url", "thumbnailUrl", "thumbnail",
    )
    for field in fields:
        value = first_value(song, field, default=None)
        if isinstance(value, dict):
            value = get_value(value, "url", "src", "href", "image_url", "imageUrl", default=None)
        result = resolve_cover_url(value)
        if result:
            return result
    return None


def filename_list(song):
    value = first_value(
        song,
        "file_names", "fileNames", "file_name", "fileName", "filename", "files",
        default=None,
    )
    if value is None:
        return []
    if isinstance(value, str):
        return [x.strip() for x in value.splitlines() if x.strip()]
    if isinstance(value, (list, tuple, set)):
        output = []
        for item in value:
            if isinstance(item, dict):
                item = get_value(
                    item,
                    "path", "file_path", "filePath", "filepath",
                    "filename", "file_name", "fileName", "name",
                    "url", "download_url", "downloadUrl",
                    default=None,
                )
            if item:
                output.append(str(item).strip())
        return output
    return []


def recursive_file_objects(obj):
    found = []
    if isinstance(obj, dict):
        keys = {str(k).casefold() for k in obj.keys()}
        file_keys = {
            "path", "file_path", "filepath", "download_path",
            "download_url", "downloadurl", "stream_url", "streamurl",
            "play_url", "playurl", "audio_url", "audiourl",
        }
        if keys.intersection(file_keys):
            found.append(obj)
        for value in obj.values():
            found.extend(recursive_file_objects(value))
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            found.extend(recursive_file_objects(item))
    return found


def file_path_from_item(item):
    if isinstance(item, str):
        if item.startswith(("http://", "https://")):
            return None
        return normalize_path(item)
    if not isinstance(item, dict):
        return None
    return normalize_path(
        get_value(
            item,
            "path", "file_path", "filePath", "filepath",
            "download_path", "downloadPath",
            default=None,
        )
    )


def direct_file_url(item):
    if isinstance(item, str):
        return valid_http_url(item)
    if not isinstance(item, dict):
        return None
    return valid_http_url(
        get_value(
            item,
            "download_url", "downloadUrl", "stream_url", "streamUrl",
            "play_url", "playUrl", "audio_url", "audioUrl",
            "url", "href", "link",
            default=None,
        )
    )


class RangeStreamSource(discord.AudioSource):
    """Streams a remote track into ffmpeg over parallel byte-range requests.

    Reading the URL directly gives ffmpeg one throttled connection -- a few
    KB/s, less than the bitrate of the file -- so it starves between frames and
    the audio stutters. Ranges over several connections run far above
    realtime; they are fetched in order and piped into ffmpeg, which lets
    playback start almost immediately while the rest of the track is still on
    its way.
    """

    FRAME = 3840  # 20ms of 48kHz stereo s16le

    def __init__(
        self,
        session: aiohttp.ClientSession,
        loop,
        url: str,
        filter_chain: Optional[str] = None,
    ):
        self.session = session
        self.loop = loop
        self.url = url
        #: The server's ffmpeg ``-af`` chain, or ``None`` for an untouched track.
        self.filter_chain = filter_chain
        self.total: int | None = None
        self._proc: subprocess.Popen | None = None
        self._task: asyncio.Task | None = None
        self._running = True

    def start(self) -> "RangeStreamSource":
        """Spawn ffmpeg and begin pulling ranges. Call from the event loop."""
        args = [
            FFMPEG,
            "-hide_banner",
            "-loglevel", "error",
            # ffmpeg must not wait for megabytes before emitting audio.
            "-probesize", "131072",
            "-analyzeduration", "1000000",
            "-i", "pipe:0",
            "-vn",
        ]
        if self.filter_chain:
            args.extend(["-af", self.filter_chain])
        args.extend(["-f", "s16le", "-ar", "48000", "-ac", "2", "pipe:1"])
        self._proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        widen_pipe(self._proc.stdin)
        self._task = asyncio.create_task(self._feed())
        return self

    # -- AudioSource -----------------------------------------------------

    def read(self) -> bytes:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return b""
        try:
            return proc.stdout.read(self.FRAME) or b""
        except (OSError, ValueError):
            return b""

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        self._running = False
        task, self._task = self._task, None
        if task is not None and not task.done():
            try:
                self.loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass
        proc, self._proc = self._proc, None
        if proc is not None:
            # Kill first: closing stdin flushes into a full pipe and would
            # otherwise wait for ffmpeg to drain minutes of audio.
            try:
                proc.kill()
            except OSError:
                pass
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass

    # -- pulling ---------------------------------------------------------

    async def _feed(self) -> None:
        offset = 0
        pending: deque = deque()
        try:
            while self._running:
                # Keep a few ranges in flight, always in file order.
                while len(pending) < STREAM_CONNECTIONS and (
                    self.total is None or offset < self.total
                ):
                    end = offset + STREAM_WINDOW - 1
                    if self.total is not None:
                        end = min(end, self.total - 1)
                    queue: asyncio.Queue = asyncio.Queue()
                    task = asyncio.create_task(
                        self._fetch(offset, end, queue)
                    )
                    pending.append((queue, task))
                    offset = end + 1
                if not pending:
                    break

                queue, task = pending.popleft()
                received = 0
                while True:
                    chunk = await queue.get()
                    if chunk is None:
                        break
                    if not self._running:
                        for _, other in pending:
                            other.cancel()
                        task.cancel()
                        return
                    # Blocking write, off the loop: it paces itself against
                    # ffmpeg's consumption and keeps a full pipe buffered.
                    await asyncio.to_thread(self._write, chunk)
                    received += len(chunk)
                await task
                if not received and offset > STREAM_WINDOW:
                    # Past the end of a file whose size was never reported.
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[Radio] Streaming %s stopped", self.url)
        finally:
            for _, other in pending:
                other.cancel()
            proc = self._proc
            if proc is not None and proc.stdin is not None and proc.poll() is None:
                try:
                    await asyncio.to_thread(proc.stdin.close)
                except (OSError, ValueError, asyncio.CancelledError):
                    pass

    async def _fetch(self, start: int, end: int, queue: asyncio.Queue) -> None:
        """Stream one byte range into ``queue``, a chunk at a time."""
        headers = {"Range": f"bytes={start}-{end}", "User-Agent": AUDIO_UA}
        # No overall deadline: a slow-but-moving range must not be cut off.
        # ``sock_read`` still catches a connection that stops delivering.
        timeout = aiohttp.ClientTimeout(
            total=None, sock_connect=20, sock_read=60
        )
        try:
            async with self.session.get(
                self.url, headers=headers, timeout=timeout
            ) as response:
                if response.status != 206:
                    raise RuntimeError(
                        f"range request refused ({response.status})"
                    )
                if self.total is None:
                    match = re.search(
                        r"/\s*(\d+)\s*$",
                        response.headers.get("Content-Range", ""),
                    )
                    if match:
                        self.total = int(match.group(1))
                # Small chunks so the first of them reaches ffmpeg quickly.
                async for chunk in response.content.iter_chunked(16384):
                    if chunk:
                        await queue.put(chunk)
        finally:
            await queue.put(None)

    def _write(self, data: bytes) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or not data:
            return
        try:
            proc.stdin.write(data)
            proc.stdin.flush()
        except (OSError, ValueError):
            self._running = False


def widen_pipe(handle, size: int = STREAM_PIPE_BYTES) -> None:
    """Give ffmpeg's stdin a bigger kernel buffer, best effort."""
    try:
        import fcntl

        fcntl.fcntl(handle.fileno(), fcntl.F_SETPIPE_SZ, size)
    except Exception:
        pass


def build_radio_header(song: Any) -> str:
    title = song_title(song)
    lines = [f"### **{title}**"]

    alt_names = song_alt_names(song)
    if alt_names != "N/A":
        lines.append(f"-# Alt Name(s): **{alt_names}**")

    engineers = song_engineers(song)
    if engineers != "N/A":
        lines.append(f"-# Engineer(s): **{engineers}**")

    producers = song_producers(song)
    if producers != "N/A":
        lines.append(f"-# Producer(s): **{producers}**")

    return "\n".join(lines)


def build_radio_details(song: Any, player: "JWRadioPlayer") -> str:
    fields = []

    era = song_era(song)
    if era != "N/A":
        fields.append(f"**Era**\n{era}")

    location = song_location(song)
    if location != "N/A":
        fields.append(f"**Recording Location**\n{location}")

    recorded = song_recorded(song)
    if recorded != "N/A":
        fields.append(f"**Recorded**\n{recorded}")

    length = song_length(song)
    if length != "N/A":
        fields.append(f"**Length**\n{length}")

    category = song_category(song)
    if category != "N/A":
        fields.append(f"**Category**\n{category}")

    queue_count = len(player.queue)
    queue_label = f"{queue_count} song" if queue_count == 1 else f"{queue_count} songs"
    mode_text = "Paused" if player.paused else "Playing"
    loop_text = "On" if player.loop else "Off"
    fields.append(f"**Radio Status**\n{mode_text} · Loop: {loop_text} · Queue: {queue_label}")

    return "\n\n".join(fields)


class JuiceWRLDAPI:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
            self.session = aiohttp.ClientSession(
                timeout=timeout,
                headers={"User-Agent": "Timezones-Bot/1.0"},
            )

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None

    async def request(self, endpoint, params=None):
        await self.start()
        url = endpoint if endpoint.startswith("http") else f"{API_BASE}/{endpoint.lstrip('/')}"
        try:
            async with self.session.get(url, params=params) as response:
                if response.status != 200:
                    return None
                text = await response.text()
                return parse_api_json(text)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None
        except Exception:
            return None

    async def get_random_song(self) -> dict | None:
        """Retrieves a true random song across all eras using juicewrldapi's radio endpoint."""
        for _ in range(12):
            data = await self.request(RADIO_RANDOM_ENDPOINT)
            if not data or not isinstance(data, dict):
                continue

            song_data = data.get("song") if isinstance(data.get("song"), dict) else data
            era = song_data.get("era", {})
            era_name = era.get("name", "") if isinstance(era, dict) else str(era)

            if is_post_era(era_name):
                continue

            if data.get("path") and not song_data.get("path"):
                song_data["path"] = data["path"]
            if data.get("title") and not song_data.get("name") and not song_data.get("title"):
                song_data["title"] = data["title"]

            return song_data

        # Fallback across all 28+ catalog pages instead of sticking to page 1
        try:
            random_page = random.randint(1, 28)
            page_data = await self.request(SONGS_ENDPOINT, params={"page": random_page, "page_size": 50})
            results = self.extract_results(page_data)
            if results:
                filtered = [
                    s for s in results
                    if not is_post_era(display(unwrap_named(first_value(s, "era", "era_name"))))
                ]
                return random.choice(filtered) if filtered else random.choice(results)
        except Exception:
            pass

        return None

    async def search_songs(self, query):
        data = await self.request(
            SONGS_ENDPOINT,
            params={
                "search": query,
                "page": 1,
                "page_size": MAX_RESULTS,
                "limit": MAX_RESULTS,
            },
        )
        return self.extract_results(data)

    async def get_song(self, song_id):
        endpoint = f"songs/{song_id}/"
        return await self.request(endpoint)

    async def file_info(self, path):
        path = normalize_path(path)
        if not path:
            return None
        return await self.request(FILE_INFO_ENDPOINT, params={"path": path})

    async def browse(self, path="", search=None):
        params = {"path": path}
        if search:
            params["search"] = search
        return await self.request(BROWSE_ENDPOINT, params=params)

    @staticmethod
    def extract_results(data):
        if not data:
            return []
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for key in ("results", "songs", "data", "items"):
                value = data.get(key)
                if isinstance(value, list):
                    return [x for x in value if isinstance(x, dict)]
        return []

    @staticmethod
    def extract_items(data):
        if not data:
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            value = data.get("items")
            if isinstance(value, list):
                return value
        return []


class JWRadioPlayer:
    def __init__(self, cog, guild_id):
        self.cog = cog
        self.guild_id = guild_id
        self.voice: Optional[discord.VoiceClient] = None
        self.current_song = None
        self.current_url = None
        self.queue = []
        self.loop = False
        self.paused = False
        self.message = None
        self.last_song = None
        self._advance_lock = asyncio.Lock()
        self._skip_requested = False
        self.history = []
        self._next_song = None
        self._prefetched_song = None
        self._prefetched_playable = None
        self._prefetch_task: asyncio.Task | None = None

    def add_to_queue(self, song):
        self.queue.append(song)

    async def get_next_song(self):
        """Pop the next track: queued requests first, then a prefetched one."""
        if self.queue:
            return self.queue.pop(0)
        if self._next_song is not None:
            song, self._next_song = self._next_song, None
            return song
        return await self.cog.api.get_random_song()

    def _schedule_prefetch(self):
        """Resolve the next track while this one plays, so the handover is quick."""
        if self.queue or self.loop:
            # An explicit request always wins; a loop replay is the same song.
            return
        if self._prefetch_task is not None and not self._prefetch_task.done():
            return
        self._prefetch_task = asyncio.create_task(self._prefetch_next())

    async def _prefetch_next(self):
        try:
            song = self._next_song or await self.cog.api.get_random_song()
            if not song:
                return
            song = await self.enrich_song(song)
            self._next_song = song
            self._prefetched_song = song
            playable = await self.cog.resolve_playable_file(song)
            if not playable:
                return
            self._prefetched_playable = playable
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[Radio] Could not prefetch the next track")

    async def enrich_song(self, song):
        """Replace a search result with the full song record from the API.

        That round trip is slow, so prefetching does it ahead of time and
        ``play_song`` reuses the finished result instead of paying for it in
        the silence between tracks.
        """
        if song.get("is_custom"):
            return song
        song_id = first_value(
            song,
            "id", "song_id", "songId", "public_id", "publicId",
            default=None,
        )
        if song_id is None:
            return song
        full_song = await self.cog.api.get_song(song_id)
        if not isinstance(full_song, dict):
            return song
        wrapped = full_song.get("song") or full_song.get("data")
        if isinstance(wrapped, dict):
            if song.get("path") and not wrapped.get("path"):
                wrapped["path"] = song["path"]
            return wrapped
        return full_song

    async def _advance(self):
        """Pick and play the next track. Serialized so two triggers can never
        interleave and drop or skip queue entries."""
        async with self._advance_lock:
            if not self.voice or not self.voice.is_connected():
                return
            if self.voice.is_playing() or self.voice.is_paused():
                return

            replay = self.loop and self.current_song is not None and not self._skip_requested
            self._skip_requested = False
            if replay:
                await self.play_song(self.current_song)
                return

            attempts = 0
            while attempts < 5:
                attempts += 1
                song = await self.get_next_song()
                if not song:
                    await self.update_message()
                    return
                if await self.play_song(song):
                    return
            await self.update_message()

    async def play_song(self, song, push_history=True):
        if not self.voice or not self.voice.is_connected():
            return False
        source = None
        started = False
        try:
            prefetched = song is self._prefetched_song
            if not prefetched:
                song = await self.enrich_song(song)

            if prefetched and self._prefetched_playable is not None:
                playable = self._prefetched_playable
            else:
                playable = await self.cog.resolve_playable_file(song)
            if not playable:
                logger.warning("[Radio] No playable file found for: %s", song_title(song))
                return False

            play_url = playable.get("url")
            if not play_url:
                return False
            is_remote = bool(valid_http_url(play_url))
            if not (is_remote or Path(play_url).is_file()):
                return False
            filter_chain = self.cog.filter_chain(self.guild_id)

            previous_song = self.current_song
            self.current_song = song
            self.current_url = play_url
            self.paused = False
            source = None
            if is_remote:
                # Feed ffmpeg from parallel byte ranges instead of letting it
                # open one slow connection to the host.
                try:
                    await self.cog.api.start()
                    source = RangeStreamSource(
                        self.cog.api.session,
                        self.cog.bot.loop,
                        play_url,
                        filter_chain,
                    ).start()
                except Exception:
                    logger.exception("[Radio] Could not range-stream %s", play_url)
                    source = None
            if source is None:
                # Local upload, or the range stream could not start: hand
                # ffmpeg the path/URL directly.
                before_options = (
                    "-reconnect 1 "
                    "-reconnect_streamed 1 "
                    "-reconnect_delay_max 5 "
                    "-reconnect_on_network_error 1"
                    if is_remote
                    else ""
                )
                source = discord.FFmpegPCMAudio(
                    play_url,
                    before_options=before_options,
                    options=f"-vn -af {filter_chain}" if filter_chain else "-vn",
                )

            def after_play(error):
                asyncio.run_coroutine_threadsafe(
                    self.song_finished(error),
                    self.cog.bot.loop,
                )

            self.voice.play(source, after=after_play)
            started = True
            if push_history and previous_song is not None and previous_song is not song:
                self.history.append(previous_song)
                if len(self.history) > 50:
                    self.history.pop(0)
            await self.update_message()
            self._schedule_prefetch()
            return True
        except Exception as exc:
            logger.exception("[Radio Play Error] %s", exc)
            if not started and source is not None:
                # Don't leave a half-opened stream behind.
                source.cleanup()
            return False

    async def song_finished(self, error=None):
        await asyncio.sleep(0.5)
        if not self.voice or not self.voice.is_connected():
            return
        await self._advance()

    async def skip(self):
        if not self.voice:
            return
        if not self.voice.is_playing() and not self.voice.is_paused():
            return
        self._skip_requested = True
        self.voice.stop()

    async def previous(self):
        """Go back to the last played track (works for API songs and uploads)."""
        if not self.history:
            return
        previous_song = self.history.pop()
        self._skip_requested = True
        if self.voice and (self.voice.is_playing() or self.voice.is_paused()):
            if self.voice.is_paused():
                self.voice.resume()
            self.voice.stop()
        async with self._advance_lock:
            played = await self.play_song(previous_song, push_history=False)
            if played:
                self._skip_requested = False

    async def play(self):
        """Resume a paused stream, or start the next track if idle."""
        if self.voice and self.voice.is_paused():
            self.voice.resume()
            self.paused = False
            await self.update_message()
            return
        await self._advance()

    async def pause(self):
        if self.voice and self.voice.is_playing():
            self.voice.pause()
            self.paused = True
            await self.update_message()

    async def resume(self):
        if self.voice and self.voice.is_paused():
            self.voice.resume()
            self.paused = False
            await self.update_message()

    async def update_message(self):
        try:
            view = JWRadioView(self)
            if self.message is not None:
                await self.message.edit(view=view)
            else:
                channel = self.cog.get_radio_channel(self.guild_id)
                if channel is None:
                    return
                self.message = await channel.send(view=view)
                await self.cog.update_settings_message(self.guild_id, self.message.id)
        except discord.NotFound:
            self.message = None
            channel = self.cog.get_radio_channel(self.guild_id)
            if channel is None:
                return
            try:
                self.message = await channel.send(view=JWRadioView(self))
                await self.cog.update_settings_message(self.guild_id, self.message.id)
            except Exception:
                logger.exception("[Radio] Could not repost the radio UI")
        except Exception:
            logger.exception("[Radio View Error]")


class JWRadioView(discord.ui.LayoutView):
    def __init__(self, player):
        super().__init__(timeout=None)
        song = player.current_song
        container = discord.ui.Container(accent_color=EMBED_COLOR)

        if not song:
            container.add_item(
                text_display(
                    "# Juice WRLD Radio\n"
                    "-# Nothing is playing right now.\n\n"
                    + QUEUE_HINT
                )
            )
            container.add_item(small_separator())
            container.add_item(
                discord.ui.ActionRow(
                    PreviousButton(player),
                    PlayButton(player),
                    PauseButton(player),
                    NextButton(player),
                    LoopButton(player),
                )
            )
            self.add_item(container)
            return

        cover = get_cover_url(song)
        header = build_radio_header(song)

        if cover:
            container.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay(header),
                    accessory=discord.ui.Thumbnail(media=cover),
                )
            )
        else:
            container.add_item(discord.ui.TextDisplay(header))

        container.add_item(small_separator())

        details = build_radio_details(song, player)
        if details:
            container.add_item(discord.ui.TextDisplay(details))

        container.add_item(small_separator())

        container.add_item(
            discord.ui.ActionRow(
                PreviousButton(player),
                PlayButton(player),
                PauseButton(player),
                NextButton(player),
                LoopButton(player),
            )
        )

        uploads = player.cog.get_uploads(player.guild_id)
        if uploads:
            container.add_item(
                discord.ui.ActionRow(UploadsSelect(uploads, player))
            )

        container.add_item(text_display(QUEUE_HINT))
        self.add_item(container)


class PlayButton(discord.ui.Button):
    def __init__(self, player):
        self.player = player
        playing = bool(player.voice and player.voice.is_playing())
        super().__init__(
            emoji="▶️",
            style=discord.ButtonStyle.success,
            disabled=playing,
            custom_id="radio_play",
        )

    async def callback(self, interaction):
        await interaction.response.defer()
        await self.player.play()


class PauseButton(discord.ui.Button):
    def __init__(self, player):
        self.player = player
        playing = bool(player.voice and player.voice.is_playing())
        super().__init__(
            emoji="⏸️",
            style=discord.ButtonStyle.secondary,
            disabled=not playing,
            custom_id="radio_pause",
        )

    async def callback(self, interaction):
        await interaction.response.defer()
        await self.player.pause()


class NextButton(discord.ui.Button):
    def __init__(self, player):
        self.player = player
        active = bool(player.voice and (player.voice.is_playing() or player.voice.is_paused()))
        super().__init__(
            emoji="⏭️",
            style=discord.ButtonStyle.secondary,
            disabled=not active,
            custom_id="radio_next",
        )

    async def callback(self, interaction):
        await interaction.response.defer()
        await self.player.skip()


class LoopButton(discord.ui.Button):
    def __init__(self, player):
        self.player = player
        super().__init__(
            emoji="🔂" if player.loop else "🔁",
            style=discord.ButtonStyle.success if player.loop else discord.ButtonStyle.secondary,
            custom_id="radio_loop",
        )

    async def callback(self, interaction):
        await interaction.response.defer()
        self.player.loop = not self.player.loop
        await self.player.update_message()


class PreviousButton(discord.ui.Button):
    def __init__(self, player):
        self.player = player
        super().__init__(
            emoji="⏮️",
            style=discord.ButtonStyle.secondary,
            disabled=not player.history,
            custom_id="radio_previous",
        )

    async def callback(self, interaction):
        await interaction.response.defer()
        await self.player.previous()


class UploadsSelect(discord.ui.Select):
    def __init__(self, uploads: list[dict], player: JWRadioPlayer):
        self.player = player
        self.uploads = uploads

        options = []
        for upload in uploads[:25]:
            label = upload.get("original_name") or upload.get("filename") or "Unknown"
            uploader = upload.get("uploader_name") or "Unknown"
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(upload.get("filename")),
                    description=f"Uploaded by {uploader}"[:100],
                )
            )

        super().__init__(
            placeholder="🎵 Play a previously uploaded MP3...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        filename = self.values[0]
        upload = next(
            (u for u in self.uploads if u.get("filename") == filename),
            None,
        )
        if upload is None:
            return

        local_path = UPLOADS_DIR / filename
        if not local_path.is_file():
            await interaction.followup.send(
                "❌ That file is no longer available on disk.",
                ephemeral=True,
            )
            return

        base_name = Path(filename).stem.replace("_", " ")
        song = {
            "is_custom": True,
            "title": base_name,
            "name": base_name,
            "filename": filename,
            "local_path": str(local_path),
            "era": "Uploaded Audio",
            "category": "Custom",
            "uploader_name": upload.get("uploader_name"),
        }
        self.player.add_to_queue(song)
        await self.player._advance()
        await self.player.update_message()


class RadioQueueSelect(discord.ui.Select):
    def __init__(self, candidates: list[dict], author_id: int, player: JWRadioPlayer):
        self.candidates = candidates
        self.author_id = author_id
        self.player = player

        options = []
        for idx, result in enumerate(candidates[:25]):
            title = song_title(result)
            era = song_era(result)
            description = f"Era: {era}"[:100] if era != "N/A" else None
            options.append(
                discord.SelectOption(
                    label=title[:100],
                    value=str(idx),
                    description=description,
                )
            )

        super().__init__(
            placeholder="Choose the correct song...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Only the person who requested the song can choose.",
                ephemeral=True,
            )
            return

        if self.view:
            self.view.selected = True
            self.view.stop()

        await interaction.response.defer()
        chosen = self.candidates[int(self.values[0])]
        self.player.add_to_queue(chosen)
        await self.player._advance()
        await self.player.update_message()

        try:
            await interaction.message.delete()
        except discord.DiscordException:
            pass


class RadioQueueSelectView(discord.ui.LayoutView):
    def __init__(self, query: str, candidates: list[dict], author_id: int, player: JWRadioPlayer):
        super().__init__(timeout=60)
        self.selected = False
        self.message: discord.Message | None = None

        container = discord.ui.Container(accent_color=EMBED_COLOR)
        container.add_item(
            text_display(
                f"Found **{len(candidates)}** matching songs for **{query}**. "
                f"Select the correct song from the dropdown below:"
            )
        )
        container.add_item(
            discord.ui.ActionRow(RadioQueueSelect(candidates, author_id, player))
        )
        self.add_item(container)

    async def on_timeout(self):
        if self.selected or not self.message:
            return
        try:
            await self.message.edit(
                view=simple_view("⏱️ Selection timed out. Send the song name again to queue it.")
            )
        except Exception:
            pass


class JWRadio(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.api = JuiceWRLDAPI()
        self.players = {}
        self._settings = self._load_settings()
        self._uploads = self._load_uploads()
        self._ready_done = False

    # ------------------------------------------------------------------
    # Settings persistence
    # ------------------------------------------------------------------

    def _load_settings(self) -> dict:
        try:
            with open(RADIO_SETTINGS_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    def _save_settings(self):
        tmp = RADIO_SETTINGS_PATH.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._settings, f, indent=2)
            os.replace(tmp, RADIO_SETTINGS_PATH)
        except Exception:
            logger.exception("[Radio] Could not save radio settings")

    # ------------------------------------------------------------------
    # Uploaded MP3 persistence
    # ------------------------------------------------------------------

    def _load_uploads(self) -> dict:
        try:
            with open(UPLOADS_META_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    def _save_uploads(self):
        tmp = UPLOADS_META_PATH.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._uploads, f, indent=2)
            os.replace(tmp, UPLOADS_META_PATH)
        except Exception:
            logger.exception("[Radio] Could not save upload metadata")

    def get_uploads(self, guild_id: int) -> list[dict]:
        entries = self._uploads.get(str(guild_id)) or []
        if not isinstance(entries, list):
            return []
        return [
            upload
            for upload in entries
            if isinstance(upload, dict)
            and upload.get("filename")
            and (UPLOADS_DIR / upload["filename"]).is_file()
        ]

    def add_upload(self, guild_id, filename, original_name, uploader_id, uploader_name):
        entry = {
            "filename": filename,
            "original_name": original_name,
            "uploader_id": uploader_id,
            "uploader_name": uploader_name,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        }
        entries = self._uploads.setdefault(str(guild_id), [])
        if not isinstance(entries, list):
            entries = []
        entries = [u for u in entries if u.get("filename") != filename]
        entries.append(entry)
        self._uploads[str(guild_id)] = entries
        self._save_uploads()

    async def download_attachment(self, attachment) -> Path | None:
        try:
            UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
            filename = Path(attachment.filename).name or "audio.mp3"
            dest = UPLOADS_DIR / filename
            stem, suffix = dest.stem, dest.suffix
            counter = 2
            while dest.exists():
                dest = UPLOADS_DIR / f"{stem} ({counter}){suffix}"
                counter += 1

            await self.api.start()
            async with self.api.session.get(attachment.url) as response:
                if response.status != 200:
                    return None
                data = await response.read()
            dest.write_bytes(data)
            return dest
        except Exception:
            logger.exception("[Radio] Could not download uploaded audio")
            return None

    def set_radio_channel(self, guild_id: int, channel_id: int):
        key = str(guild_id)
        entry = self._settings.setdefault(key, {})
        entry["channel_id"] = channel_id
        entry.pop("message_id", None)
        entry.pop("voice_channel_id", None)
        self._save_settings()

    def update_settings_message(self, guild_id: int, message_id: int):
        entry = self._settings.setdefault(str(guild_id), {})
        entry["message_id"] = message_id
        self._save_settings()

    def update_settings_voice(self, guild_id: int, voice_channel_id: int):
        entry = self._settings.setdefault(str(guild_id), {})
        entry["voice_channel_id"] = voice_channel_id
        self._save_settings()

    def get_radio_channel(self, guild_id: int):
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return None
        entry = self._settings.get(str(guild_id)) or {}
        channel = guild.get_channel(entry.get("channel_id") or 0)
        if isinstance(channel, discord.TextChannel):
            return channel
        return None

    def get_filter(self, guild_id) -> str:
        """The filter this server is on; ``"off"`` when unset or unrecognised."""
        entry = self._settings.get(str(guild_id)) or {}
        name = str(entry.get("filter") or "off").casefold()
        return name if name in FILTERS else "off"

    def filter_chain(self, guild_id) -> Optional[str]:
        """The ffmpeg ``-af`` chain for this server, or ``None`` for untouched."""
        return FILTERS[self.get_filter(guild_id)][2]

    def set_filter(self, guild_id, name: str):
        entry = self._settings.setdefault(str(guild_id), {})
        entry["filter"] = name
        self._save_settings()

    # ------------------------------------------------------------------
    # Player helpers
    # ------------------------------------------------------------------

    def get_player(self, guild_id):
        if guild_id not in self.players:
            self.players[guild_id] = JWRadioPlayer(self, guild_id)
        return self.players[guild_id]

    async def _auto_delete(self, message, delay: float = 8.0):
        await asyncio.sleep(delay)
        try:
            await message.delete()
        except discord.DiscordException:
            pass

    async def resolve_playable_file(self, song):
        if song.get("is_custom"):
            local_path = song.get("local_path")
            if local_path and Path(local_path).is_file():
                return {
                    "url": str(local_path),
                    "name": song.get("title", "Uploaded Song"),
                }
            return {
                "url": song.get("url"),
                "name": song.get("title", "Uploaded Song"),
            }

        if song.get("path"):
            path = normalize_path(song["path"])
            if path:
                return {"url": make_download_url(path), "name": song_title(song)}

        candidates = []
        for item in recursive_file_objects(song):
            candidates.append(item)
        for filename in filename_list(song):
            candidates.append(filename)

        seen = set()
        for candidate in candidates:
            direct = direct_file_url(candidate)
            if direct:
                identity = direct.casefold()
                if identity in seen:
                    continue
                seen.add(identity)
                return {"url": direct, "name": song_title(song)}

            path = file_path_from_item(candidate)
            if not path:
                continue

            identity = path.casefold()
            if identity in seen:
                continue
            seen.add(identity)

            info = await self.api.file_info(path)
            if not info:
                continue

            info_path = normalize_path(
                first_value(
                    info,
                    "path", "file_path", "filePath", "filepath",
                    default=path,
                )
            ) or path

            stream_url = valid_http_url(
                first_value(
                    info,
                    "stream_url", "streamUrl", "play_url", "playUrl",
                    "audio_url", "audioUrl", "download_url", "downloadUrl",
                    "url", "href",
                    default=None,
                )
            ) or make_download_url(info_path)

            if stream_url:
                return {
                    "url": stream_url,
                    "name": display(
                        first_value(
                            info,
                            "name", "filename", "file_name", "fileName",
                            default=song_title(song),
                        ),
                        song_title(song),
                    ),
                }

        title = song_title(song)
        browse_data = await self.api.browse(path="", search=title)
        items = self.api.extract_items(browse_data)
        file_items = []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type", "")).casefold()
            path = file_path_from_item(item)
            name = display(
                get_value(
                    item,
                    "name", "filename", "file_name", "fileName", "title",
                    default="",
                ),
                "",
            )
            if path and (item_type in ("file", "audio") or "." in path.split("/")[-1]):
                file_items.append((name, path))

        file_items.sort(key=lambda x: (not title_matches(title, x[0]), len(x[1])))
        for name, path in file_items:
            if not title_matches(title, name):
                continue
            url = make_download_url(path)
            if url:
                return {"url": url, "name": name or title}

        return None

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @commands.group(
        name="jwradio", aliases=["jwrd", "wrldradio"], invoke_without_command=True
    )
    async def radio(self, ctx):
        if not ctx.author.voice:
            return await ctx.send(view=simple_view("❌ You need to be in a voice channel first."))

        channel = ctx.author.voice.channel
        player = self.get_player(ctx.guild.id)

        try:
            if player.voice and player.voice.is_connected():
                if player.voice.channel.id != channel.id:
                    await player.voice.move_to(channel)
            else:
                player.voice = await channel.connect()
        except Exception:
            logger.exception("[Radio Voice Connect Error]")
            return await ctx.send(view=simple_view("❌ Could not connect to your voice channel."))

        self.update_settings_voice(ctx.guild.id, channel.id)

        configured = self.get_radio_channel(ctx.guild.id)
        target = configured or ctx.channel
        if player.message is None or player.message.channel.id != target.id:
            try:
                message = await target.send(view=JWRadioView(player))
                player.message = message
                if configured is not None and target.id == configured.id:
                    self.update_settings_message(ctx.guild.id, message.id)
            except Exception:
                logger.exception("[Radio] Could not send the radio UI")

        await player._advance()
        confirmation = await ctx.send(
            view=simple_view(f"✅ Radio connected to **{channel.name}** -- it'll keep playing 24/7.")
        )
        asyncio.create_task(self._auto_delete(confirmation))

    @radio.command(name="filter", aliases=["filters", "fx"])
    async def radio_filter(self, ctx, name: Optional[str] = None):
        """List the radio's filters, or switch the radio to one.

        With no name every filter is listed, with the one in use marked. With a
        name the server switches to it; the chain is read when the next track
        starts, so the song that is playing is never interrupted. `off` plays
        tracks untouched.
        """
        if ctx.guild is None:
            return
        prefix = await prefix_for(ctx)
        active = self.get_filter(ctx.guild.id)

        if name is None:
            lines = [
                "# 🎛️ Radio filters",
                f"-# In use: **{FILTERS[active][0]}**"
                + ("" if active == "off" else f" (`{active}`)"),
                "",
            ]
            for key, (label, description, _chain) in FILTERS.items():
                mark = "✅" if key == active else "•"
                lines.append(f"{mark} `{key}` — **{label}** — {description}")
            lines.append("")
            lines.append(
                f"-# Switch with `{prefix}jwradio filter <name>`; "
                f"`{prefix}jwradio filter off` plays tracks untouched."
            )
            return await ctx.send(view=simple_view("\n".join(lines)))

        wanted = name.strip().casefold().lstrip("!.,")
        if wanted not in FILTERS:
            listed = ", ".join(f"`{key}`" for key in FILTERS)
            return await ctx.send(
                view=simple_view(
                    f"❌ There's no **{name}** filter. Pick one of: {listed}."
                )
            )

        if wanted != active:
            allowed = ctx.author.guild_permissions.manage_guild
            if not allowed and not await self.bot.is_owner(ctx.author):
                return await ctx.send(
                    view=simple_view(
                        "❌ Only server managers can change the radio's filter."
                    )
                )
            # Changing a filter never interrupts what is playing: the chain is
            # read when the next track starts, so the change is simply heard then.
            self.set_filter(ctx.guild.id, wanted)

        label, description, _chain = FILTERS[wanted]
        if wanted == active:
            note = " (already on)"
        else:
            note = " — applies from the next track, so nothing gets cut off."
        confirmation = await ctx.send(
            view=simple_view(f"🎛️ Radio filter: **{label}** — {description}.{note}")
        )
        asyncio.create_task(self._auto_delete(confirmation))

    @commands.command(name="jwsetradio", aliases=["jwradiochannel"])
    @commands.check_any(commands.is_owner(), commands.has_permissions(manage_guild=True))
    async def setradio(self, ctx, channel: commands.TextChannelConverter = None):
        target = channel or ctx.channel
        if not isinstance(target, discord.TextChannel):
            return await ctx.send(view=simple_view("❌ Please pick a text channel."))

        player = self.get_player(ctx.guild.id)

        if ctx.author.voice:
            voice_channel = ctx.author.voice.channel
            try:
                if player.voice and player.voice.is_connected():
                    if player.voice.channel.id != voice_channel.id:
                        await player.voice.move_to(voice_channel)
                else:
                    player.voice = await voice_channel.connect()
                self.update_settings_voice(ctx.guild.id, voice_channel.id)
            except Exception:
                logger.exception("[Radio Voice Connect Error]")
                return await ctx.send(view=simple_view("❌ Could not connect to your voice channel."))

        self.set_radio_channel(ctx.guild.id, target.id)

        old_message = player.message
        try:
            message = await target.send(view=JWRadioView(player))
        except Exception:
            logger.exception("[Radio] Could not send the radio UI")
            return await ctx.send(view=simple_view(f"❌ I couldn't send the radio UI to {target.mention}."))

        player.message = message
        self.update_settings_message(ctx.guild.id, message.id)

        if old_message is not None and old_message.id != message.id:
            try:
                await old_message.delete()
            except discord.DiscordException:
                pass

        await player._advance()

        confirmation = await ctx.send(
            view=simple_view(
                f"✅ Radio UI set to {target.mention}.\n"
                "Type a song name in that channel to queue it -- or drop an MP3 file!"
            )
        )
        asyncio.create_task(self._auto_delete(confirmation))

    # ------------------------------------------------------------------
    # Channel message queueing
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if not message.guild or not isinstance(message.channel, discord.TextChannel):
            return

        entry = self._settings.get(str(message.guild.id))
        if not entry or message.channel.id != entry.get("channel_id"):
            return

        # Leave real commands alone. Resolving the context is the only reliable
        # way to tell: this bot's prefix is an async, mention-aware callable,
        # not the static string the reference bot checked against.
        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return

        player = self.get_player(message.guild.id)

        audio = [
            a for a in message.attachments
            if a.filename.lower().endswith(AUDIO_EXTS)
        ]

        if audio:
            attachment = audio[0]
            saved = await self.download_attachment(attachment)
            if saved is not None:
                self.add_upload(
                    message.guild.id,
                    filename=saved.name,
                    original_name=attachment.filename,
                    uploader_id=message.author.id,
                    uploader_name=message.author.display_name,
                )
            base_name = attachment.filename.rsplit(".", 1)[0].replace("_", " ")
            custom_song = {
                "is_custom": True,
                "title": base_name,
                "name": base_name,
                "url": attachment.url,
                "filename": (saved or Path(attachment.filename)).name,
                "local_path": str(saved) if saved is not None else None,
                "era": "Uploaded Audio",
                "category": "Custom",
                "uploader_name": message.author.display_name,
            }
            player.add_to_queue(custom_song)
            await self._try_delete(message)
            await player._advance()
            await player.update_message()
            return

        query = message.content.strip()
        if not query:
            return

        results = await self.api.search_songs(query)
        if not results:
            await self._try_delete(message)
            try:
                await message.channel.send(
                    view=simple_view(f"❌ Couldn't find **{query}**. Queue it again with the right name!")
                )
            except discord.DiscordException:
                pass
            return

        wanted = query.casefold()
        exact_match = next(
            (
                result
                for result in results
                if song_title(result).strip().casefold() == wanted
            ),
            None,
        )

        if len(results) == 1 or exact_match is not None:
            song = exact_match or results[0]
            player.add_to_queue(song)
            await self._try_delete(message)
            await player._advance()
            await player.update_message()
            return

        await self._try_delete(message)
        dropdown_view = RadioQueueSelectView(
            query=query,
            candidates=results,
            author_id=message.author.id,
            player=player,
        )
        try:
            dropdown_view.message = await message.channel.send(view=dropdown_view)
        except discord.DiscordException:
            pass

    @staticmethod
    async def _try_delete(message: discord.Message):
        try:
            await message.delete()
        except discord.DiscordException:
            pass

    # ------------------------------------------------------------------
    # Startup restore
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_ready(self):
        if self._ready_done:
            return
        self._ready_done = True
        await self._restore_uis()

    async def _restore_uis(self):
        changed = False
        for guild_id_str, entry in list(self._settings.items()):
            guild = self.bot.get_guild(int(guild_id_str))
            if guild is None:
                continue
            channel = guild.get_channel(entry.get("channel_id") or 0)
            if not isinstance(channel, discord.TextChannel):
                continue

            player = self.get_player(guild.id)

            # Clear the stale UI message from the previous session.
            old_message_id = entry.get("message_id")
            if old_message_id:
                try:
                    old = await channel.fetch_message(old_message_id)
                    await old.delete()
                except discord.DiscordException:
                    pass
            player.message = None

            # Reconnect voice so the radio keeps running after a restart.
            voice_channel_id = entry.get("voice_channel_id")
            if voice_channel_id:
                voice_channel = guild.get_channel(voice_channel_id)
                if isinstance(voice_channel, (discord.VoiceChannel, discord.StageChannel)):
                    try:
                        player.voice = await voice_channel.connect()
                    except Exception:
                        logger.exception("[Radio] Could not reconnect voice in %s", guild_id_str)

            # Put the reworked UI there.
            try:
                message = await channel.send(view=JWRadioView(player))
                player.message = message
                entry["message_id"] = message.id
                changed = True
            except Exception:
                logger.exception("[Radio] Could not repost the UI in %s", guild_id_str)

            if player.voice and player.voice.is_connected():
                await player._advance()

        if changed:
            self._save_settings()

    def cog_unload(self):
        asyncio.create_task(self.api.close())
        for player in self.players.values():
            if player.voice:
                asyncio.create_task(player.voice.disconnect())


async def setup(bot):
    await bot.add_cog(JWRadio(bot))
