from __future__ import annotations

import asyncio
import difflib
import html
import json
import logging
import re
from collections import Counter
from datetime import datetime
from typing import Any, Mapping, Optional
from urllib.parse import quote, urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

try:
    from config import EMBED_COLOR
except ImportError:
    EMBED_COLOR = discord.Color.from_str("#2b2d31")

from utils.core import mp3_buttons
from utils.core.eras import emoji_for_era
from utils.core.prefixes import prefix_for

logger = logging.getLogger(__name__)

API_ROOT = "https://juicewrldapi.com"
API_BASE = f"{API_ROOT}/juicewrld"
SONGS_ENDPOINT = f"{API_BASE}/songs/"
DOWNLOAD_BASE = f"{API_BASE}/files/download/"

API_TIMEOUT = 25
CATALOG_PAGE_SIZE = 100
#: Catalogue pages fetched at once when building the metadata index. The sweep
#: is 28 pages of ~630KB each; asking for all of them in one burst trips the
#: archive's rate limit, and one slow page shouldn't hold up the rest.
CATALOG_CONCURRENCY = 6
CACHE_TTL = 3600
SELECT_TIMEOUT = 120
VIEW_TIMEOUT = 900
MAX_META_LENGTH = 90
MAX_NOTES_LENGTH = 160

COVER_FIELDS = (
    "image_url", "imageUrl", "imageURL", "image",
    "cover_url", "coverUrl", "coverURL",
    "cover_art_url", "coverArtUrl",
    "artwork_url", "artworkUrl",
    "thumbnail_url", "thumbnailUrl", "thumbnail",
)

FILE_PATH_KEYS = (
    "path", "file_path", "filePath", "filepath",
    "download_path", "downloadPath",
)

FILE_URL_KEYS = (
    "download_url", "downloadUrl", "stream_url", "streamUrl",
    "play_url", "playUrl", "audio_url", "audioUrl",
    "url", "href", "link",
)

FILENAME_KEYS = (
    "file_names", "fileNames", "file_name", "fileName", "filename",
)

#: Lines in ``file_names`` that describe sessions rather than a playable file.
FILENAME_SKIP_RE = re.compile(
    r"(?i)^(?:session\s*title|folder\s*title|beat\s*pack|loop\s*kit|sample)\s*:"
)
FILENAME_PREFIX_RE = re.compile(r"(?i)^File\s*Name(?:\s*\(\d+\))?\s*:\s*")

#: Non-audio project/session archives we never offer as a download button.
ARCHIVE_EXTENSIONS = (".ptx", ".als", ".flp", ".zip", ".rar", ".7z")

MONTH_NAMES = (
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

MONTH_NUMBERS: dict[str, int] = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

#: Month names the archive typos often enough that matching the exact spelling
#: would silently drop real recording dates.
MONTH_TYPOS = {
    "febuary": "february", "febraury": "february", "feburary": "february",
    "feburuary": "february", "spetember": "september", "sepember": "september",
    "septmber": "september", "ocotber": "october", "novemeber": "november",
    "decemeber": "december", "janurary": "january", "apirl": "april",
    "augest": "august",
}

#: The recording date ends where the export stamp begins.
FILE_EXPORTED_RE = re.compile(r"(?i)\bfile\s*(?:re-?\s*)?exported\b")

#: A preview entry can carry a later ``First Teased`` stamp. Only the preview
#: itself counts for ``!previewedon`` when both are present.
PREVIEW_TEASED_RE = re.compile(r"(?i)\bfirst\s+tea\w*")

#: The archive labels the preview section itself; without one the field is a
#: tease-only entry (or a bare date) and keeps its date.
PREVIEW_LABEL_RE = re.compile(r"(?i)\bpreview")

#: A release entry can carry a later ``Reissued`` stamp, which isn't the
#: release ``!releasedon`` lists.
REISSUED_RE = re.compile(r"(?i)\breissued\b")

#: Some songs have no ``release_date`` and store the official release in
#: ``date_leaked`` with this label instead.
RELEASED_LABEL_RE = re.compile(r"(?i)^\s*(?:first\s+)?released\b")

#: Month names (full and abbreviated) as a non-capturing alternation.
_MONTH_ALT = (
    r"(?:January|February|March|April|May|June|July|August|September|October"
    r"|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
)

#: A user-typed date: "October 4", "October 4th", "4 October", "4th of October",
#: optionally followed by a year.
INPUT_DATE_RE = re.compile(
    rf"(?:(?P<m1>{_MONTH_ALT})\.?\s+(?P<d1>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"|(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<m2>{_MONTH_ALT}))"
    r"(?:\s*,?\s*(?P<year>\d{4}))?",
    re.IGNORECASE,
)

#: A month name followed by a day (optionally a range) and an optional year.
#: The day may not be part of a longer number, so "October 2018" is skipped
#: rather than read as the 20th.
RECORDED_DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December"
    r"|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\s+"
    r"(\d{1,2})(?!\d)(?:\s*[-\u2013\u2014]\s*(\d{1,2})(?!\d))?(?:st|nd|rd|th)?"
    r"(?:\s*,?\s*(\d{4}))?",
    re.IGNORECASE,
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}


def clean(value: Any) -> Any:
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


def get_value(obj: Any, *names: str, default: Any = None) -> Any:
    if obj is None:
        return default
    for name in names:
        value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
        value = clean(value)
        if value is not None:
            return value
    return default


def recursive_find(obj: Any, names: tuple[str, ...] | list[str]) -> Any:
    wanted = {str(name).casefold() for name in names}
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


def first_value(obj: Any, *names: str, default: Any = None) -> Any:
    value = get_value(obj, *names, default=None)
    if value is not None:
        return value
    found = recursive_find(obj, names)
    return found if found is not None else default


def display(value: Any, default: str = "N/A") -> str:
    value = clean(value)
    if value is None:
        return default
    if isinstance(value, (list, tuple, set)):
        parts = [display(item, "") for item in value]
        joined = ", ".join(part for part in parts if part)
        return joined or default
    if isinstance(value, dict):
        for key in ("name", "title", "value", "text", "label"):
            if key in value:
                result = display(value[key], "")
                if result:
                    return result
        return default
    return str(value)


def display_list(value: Any, default: str = "N/A") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip() or default
    if isinstance(value, (list, tuple, set)):
        parts = [display(item, "") for item in value]
        joined = ", ".join(part for part in parts if part)
        return joined or default
    return display(value, default)


def unwrap_named(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("name") or value.get("title") or value.get("value")
    return value


def valid_http_url(value: Any) -> str | None:
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


def normalize_path(path: Any) -> str | None:
    if not path:
        return None
    path = str(path).strip()
    if not path or path.startswith(("http://", "https://")):
        return None
    while path.startswith("/"):
        path = path[1:]
    return path


def make_download_url(path: str) -> str | None:
    normalized = normalize_path(path)
    if not normalized:
        return None
    return f"{DOWNLOAD_BASE}?path={quote(normalized, safe='')}&channel=comp"


def clean_match_key(value: str) -> str:
    value = re.sub(r"\.[a-zA-Z0-9]+$", "", value)
    value = re.sub(r"[_\-.]+", " ", value)
    value = re.sub(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}", "", value)
    value = re.sub(r"\s+", " ", value).strip().lower()
    return value


def clean_meta_text(value: str) -> str:
    value = value.strip()
    value = re.sub(
        r"^(?:recorded|first previewed|previewed|surfaced|released|file exported)[\s,.:-]*",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip()
    value = re.sub(r"^[,.:\s]+", "", value).strip()
    value = re.sub(r"[,.\s]+$", "", value).strip()
    return re.sub(r"\s+", " ", value)


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


def is_unsurfaced(song: Any) -> bool:
    """Unsurfaced material has no real file behind it: the API only carries
    placeholder/decoy file names, so we never offer one for download."""
    for value in (
        first_value(song, "category", "category_name", "categoryName"),
        first_value(song, "leak_type", "leakType", "classification"),
    ):
        if "unsurfaced" in display(unwrap_named(value)).lower():
            return True
    return False


def normalize_month_typos(text: str) -> str:
    """Fix the common month misspellings the catalog contains."""
    for wrong, right in MONTH_TYPOS.items():
        text = re.sub(wrong, right, text, flags=re.IGNORECASE)
    return text


def recorded_date_matches(raw: Any) -> list[tuple[int, int, int, int | None]]:
    """Every ``(month, start_day, end_day, year)`` a record date mentions.

    Only the recorded portion is considered — anything from ``File Exported``
    onward (the export stamp) is ignored, so an exported-on-today song that was
    recorded on another day doesn't leak into the results. Ranges such as
    "February 10-11, 2019" expand to a covered day span, and the year is left
    ``None`` when the catalog didn't include one.
    """
    if not raw:
        return []

    text = FILE_EXPORTED_RE.split(str(raw))[0]
    text = normalize_month_typos(text)

    matches: list[tuple[int, int, int, int | None]] = []
    seen: set[tuple[int, int, int, int | None]] = set()
    for match in RECORDED_DATE_RE.finditer(text):
        month = MONTH_NUMBERS.get(match.group(1).lower())
        if not month:
            continue
        try:
            start = int(match.group(2))
        except (TypeError, ValueError):
            continue
        end = int(match.group(3)) if match.group(3) else start
        if end < start:
            start, end = end, start
        if not 1 <= start <= 31:
            continue
        end = min(end, 31)
        year = int(match.group(4)) if match.group(4) else None
        key = (month, start, end, year)
        if key in seen:
            continue
        seen.add(key)
        matches.append(key)
    return matches


def previewed_date_matches(raw: Any) -> list[tuple[int, int, int, int | None]]:
    """Date matches from a ``preview_date`` field.

    The archive appends ``First Teased`` to some previews. When a preview is
    present the tease is dropped, so a song shown as previewed on June 9 is not
    also listed under its June 7 tease; entries that only ever mention a tease
    keep that date, because it is the first time the archive has for them.
    """
    if not raw:
        return []
    text = str(raw)
    if PREVIEW_LABEL_RE.search(text):
        text = PREVIEW_TEASED_RE.split(text, maxsplit=1)[0]
    return recorded_date_matches(text)


def released_date_matches(raw: Any) -> list[tuple[int, int, int, int | None]]:
    """Date matches from a ``release_date`` field, ignoring reissues."""
    if not raw:
        return []
    return recorded_date_matches(REISSUED_RE.split(str(raw), maxsplit=1)[0])


#: The parser for each catalog date field. Fields without a special case (the
#: recording and surfaced dates) use the plain recording-date parser.
DATE_FIELD_MATCHERS = {
    "preview_date": previewed_date_matches,
    "release_date": released_date_matches,
}


def date_field_value(song: Mapping[str, Any], field: str) -> Any:
    """The catalog value a date command should read for ``song``.

    ``release_date`` is the release field, but when it is empty the archive
    sometimes records the official release in ``date_leaked`` with a leading
    ``Released`` label; that stamp counts as the release too.
    """
    value = song.get(field)
    if field == "release_date" and not value:
        leaked = str(song.get("date_leaked") or "")
        if RELEASED_LABEL_RE.match(leaked):
            return leaked
    return value


def _validate_date(month: int, day: int, year: int | None) -> tuple[int, int, int | None] | None:
    """Return ``(month, day, year)`` when it is a real calendar date."""
    if not 1 <= month <= 12:
        return None
    try:
        datetime(year if year else 2000, month, day)  # rejects e.g. February 30
    except ValueError:
        return None
    return month, day, year


def parse_recorded_date(text: str) -> tuple[int, int, int | None] | None:
    """Parse a user-typed date into ``(month, day, year)`` if possible.

    Accepts the shapes people actually type: ``October 4``, ``October 4th``,
    ``4 October`` and ``4th of October`` (with an optional leading ``on`` and
    an optional year), abbreviations, ``YYYY-MM-DD``, ``M/D`` and ``M/D/YYYY``,
    and the catalog's common month typos.
    """
    if not text:
        return None

    cleaned = re.sub(r"(?i)^\s*on\s+", "", text.strip()).strip()
    cleaned = normalize_month_typos(cleaned)

    match = INPUT_DATE_RE.search(cleaned)
    if match:
        month_token = match.group("m1") or match.group("m2")
        day_token = match.group("d1") or match.group("d2")
        month = MONTH_NUMBERS.get(month_token.lower().rstrip(".")) if month_token else None
        if month and day_token and day_token.isdigit():
            year = int(match.group("year")) if match.group("year") else None
            parsed = _validate_date(month, int(day_token), year)
            if parsed:
                return parsed

    # 2019-10-04
    iso = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", cleaned)
    if iso:
        parsed = _validate_date(int(iso.group(2)), int(iso.group(3)), int(iso.group(1)))
        if parsed:
            return parsed

    # 10/4 or 10/4/2018
    numeric = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", cleaned)
    if numeric:
        year = int(numeric.group(3)) if numeric.group(3) else None
        if year is not None and year < 100:
            year += 2000
        parsed = _validate_date(int(numeric.group(1)), int(numeric.group(2)), year)
        if parsed:
            return parsed

    return None


#: A year typed on its own: ``!leakedon 2022``, "in 2022", "2022 leaks".
INPUT_YEAR_RE = re.compile(
    r"^\s*(?:in\s+|on\s+)?(\d{4})\s*(?:leaks?|songs?|tracks?|files?)?\s*$",
    re.IGNORECASE,
)


def _plausible_year(year: int) -> bool:
    """Whether ``year`` could really be inside the archive.

    The archive holds 2010s–2020s material, so anything outside this window is
    a typo rather than a year the user meant.
    """
    return 1990 <= year <= datetime.now().year + 1


def parse_query_year(text: str) -> int | None:
    """The year in a year-only query, or ``None`` when it isn't just a year.

    ``!leakedon 2022`` asks for everything the archive stamped with 2022, so
    the argument has to be nothing but a year — a bare day number like ``4``
    must keep falling through to the date parser.
    """
    if not text:
        return None

    match = INPUT_YEAR_RE.match(text.strip())
    if not match:
        return None

    year = int(match.group(1))
    return year if _plausible_year(year) else None


#: A month typed on its own, with an optional year after it: ``!leakedon
#: October``, ``!leakedon October 2026``, "in October 2026", "October leaks".
INPUT_MONTH_RE = re.compile(
    rf"^\s*(?:in\s+|on\s+)?(?P<month>{_MONTH_ALT})\.?\s*"
    r"(?:,?\s*(?P<year>\d{4}))?\s*"
    r"(?:leaks?|songs?|tracks?|files?)?\s*$",
    re.IGNORECASE,
)


def parse_query_month(text: str) -> tuple[int, int | None] | None:
    """The ``(month, year)`` in a month-only query, else ``None``.

    ``!leakedon October`` asks for everything the archive stamped with that
    month in any year, and ``!leakedon October 2026`` narrows it to one. The
    argument has to be nothing but a month and an optional year, so a real date
    like ``October 4`` keeps falling through to the date parser, and ``4`` is
    never read as a year.
    """
    if not text:
        return None

    match = INPUT_MONTH_RE.match(normalize_month_typos(text.strip()))
    if not match:
        return None

    month = MONTH_NUMBERS.get(match.group("month").lower().rstrip("."))
    if not month:
        return None

    year = int(match.group("year")) if match.group("year") else None
    if year is not None and not _plausible_year(year):
        return None
    return month, year


def truncate(value: str, limit: int = MAX_META_LENGTH) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def compact_value(value: str, *, limit: int = MAX_META_LENGTH) -> str | None:
    value = display(value)
    if not value or value == "N/A":
        return None
    return truncate(value.replace("\n", ", "), limit)


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


def song_text(song: Any, *keys: str, default: Any = None) -> str:
    return display(first_value(song, *keys, default=default))


def song_list(song: Any, *keys: str) -> str:
    return display_list(first_value(song, *keys, default=None))


def get_song_title(song: Any) -> str:
    return song_text(
        song,
        "name", "title", "song_title", "songTitle",
        default="Unknown Song",
    )


def resolve_cover_url(value: Any) -> str | None:
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


def get_cover_url(song: Any) -> str | None:
    for field in COVER_FIELDS:
        value = first_value(song, field, default=None)
        if isinstance(value, dict):
            value = get_value(value, "url", "src", "href", "image_url", "imageUrl")
        result = resolve_cover_url(value)
        if result:
            return result
    return None


def get_length(song: Any) -> str:
    value = first_value(
        song,
        "length", "duration", "duration_text", "durationText",
        default=None,
    )
    if isinstance(value, (int, float)):
        minutes, seconds = divmod(int(value), 60)
        return f"{minutes}:{seconds:02d}"
    return display(value)


def get_surfaced(song: Any) -> str:
    value = first_value(
        song,
        "surfaced", "surfaced_date", "surfacedDate",
        "surface_date", "surfaceDate", "date_leaked",
        default=None,
    )
    if value is not None:
        return display(value)
    dates = first_value(song, "dates", "important_dates", "importantDates", default=None)
    if isinstance(dates, dict):
        nested = (
            dates.get("surfaced")
            or dates.get("surface")
            or dates.get("surfaced_date")
            or dates.get("surfacedDate")
            or dates.get("date_leaked")
        )
        if nested:
            return display(nested)
    return "N/A"


def filename_list(song: Any) -> list[str]:
    value = first_value(song, *FILENAME_KEYS, default=None)
    if value is None:
        return []
    if isinstance(value, str):
        terms: list[str] = []
        for line in value.splitlines():
            term = line.strip()
            if not term or term.lower() == "n/a":
                continue
            if FILENAME_SKIP_RE.match(term):
                continue
            term = FILENAME_PREFIX_RE.sub("", term).strip()
            if term and term.lower() != "n/a":
                terms.append(term)
        return terms
    if isinstance(value, (list, tuple, set)):
        output: list[str] = []
        for item in value:
            if isinstance(item, dict):
                item = get_value(
                    item,
                    *FILE_PATH_KEYS,
                    "filename", "file_name", "fileName", "name",
                    "url", "download_url", "downloadUrl",
                    default=None,
                )
            if item:
                output.append(str(item).strip())
        return output
    return []


def file_path_from_item(item: Any) -> str | None:
    if isinstance(item, str):
        if item.startswith(("http://", "https://")):
            return None
        return normalize_path(item)
    if isinstance(item, dict):
        return normalize_path(get_value(item, *FILE_PATH_KEYS, default=None))
    return None


def direct_file_url(item: Any) -> str | None:
    if isinstance(item, str):
        return valid_http_url(item)
    if isinstance(item, dict):
        return valid_http_url(get_value(item, *FILE_URL_KEYS, default=None))
    return None


def recursive_file_objects(obj: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    file_keys = {
        "path", "file_path", "filepath", "download_path", "download_url",
        "downloadurl", "stream_url", "streamurl", "play_url", "playurl",
    }
    if isinstance(obj, dict):
        keys = {str(key).casefold() for key in obj}
        if keys.intersection(file_keys):
            found.append(obj)
        for value in obj.values():
            found.extend(recursive_file_objects(value))
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            found.extend(recursive_file_objects(item))
    return found


def find_primary_file(tagged: list[dict], original: list[dict]) -> dict | None:
    for item in (*original, *tagged):
        if valid_http_url(item.get("download")):
            return item
    return None


#: Words that nearly every archive file name carries (``Juice Wrld - ...``,
#: ``... ft JUICE WRLD ...``, ``... Main Mix V1 FNL CLN``). They describe the
#: archive's naming scheme, not the recording, so the fuzzy fallback below
#: ignores them instead of counting them as evidence of a match.
FUZZY_MATCH_NOISE_WORDS = frozenset({
    "juice", "wrld", "juicewrld", "ft", "feat", "featuring",
    "mix", "mixdown", "rough", "ruff", "demo", "open", "main", "final",
    "fnl", "cln", "clean", "explicit", "master", "pass", "session",
    "sessions", "studio", "version", "ver", "edit", "preview",
    "the", "a", "an", "and", "of", "in", "on", "to", "it", "no", "for",
    "with", "from", "at", "is", "be", "my", "me", "all", "out", "up",
    "so", "x", "w",
})

#: File extensions a metadata name may end in. Anything else after a full stop
#: is part of the title (``01. intro``, ``Song v1.2``), and truncating there
#: made the prefix rule match any archive file starting with the same number.
MATCHABLE_EXTENSIONS = frozenset({
    "mp3", "wav", "caf", "m4a", "flac", "aiff", "aif", "ogg", "opus",
    "aac", "alac", "wma", "mp4", "m4v", "mov", "webm", "zip", "rar",
    "7z", "tar", "gz", "jpg", "jpeg", "png", "txt", "pdf", "ptx",
})

#: How similar two names must be before a fuzzy match is allowed, and the
#: higher bars when they only agree on some words or on a spelling.
FUZZY_MATCH_MIN_RATIO = 0.70
FUZZY_MATCH_MIN_PARTIAL_RATIO = 0.85
FUZZY_MATCH_MIN_SPELLING_RATIO = 0.90


def name_without_extension(value: str) -> str:
    """``Song.mp3`` loses its extension; ``01. intro`` keeps its title."""
    if "." in value:
        head, _, tail = value.rpartition(".")
        if tail.casefold() in MATCHABLE_EXTENSIONS:
            return head
    return value


def match_words(value: str) -> list[str]:
    """The words of ``value`` that could identify a recording, in order.

    Formatting (``Juice Wrld``, ``ft``, ``Main Mix FNL CLN``), single letters,
    version markers (``v2``) and 1-2 digit take counters are dropped: those are
    what the archive's names share, not what tells two songs apart.
    """
    words: list[str] = []
    for word in re.split(r"[^0-9a-z']+", value.casefold()):
        if len(word) < 2 or word in FUZZY_MATCH_NOISE_WORDS:
            continue
        if re.fullmatch(r"v\d+", word) or re.fullmatch(r"\d{1,2}", word):
            continue
        words.append(word)
    return words


def fuzzy_match_is_plausible(
    clean_target: str,
    clean_item: str,
    item_name: str,
    word_counts: Mapping[str, int],
) -> bool:
    """Whether a fuzzy (not exact, not a clean prefix) match may be the song's.

    Resolution prefers the exact name and then the archive's alternate-take
    prefixes, and only then guesses. That guess used to be plain string
    similarity, which happily matched ``signs - ft iuice wrld - rough mix 2``
    to ``Ransom ft JUICE WRLD v1.2`` - both are mostly the archive's boilerplate
    - and so offered another song's original file. A guess now has to agree on
    the words that actually name the recording.
    """
    def similarity(left: str, right: str) -> float:
        return difflib.SequenceMatcher(None, left, right).ratio()

    target_words = match_words(clean_target)
    if not target_words:
        return False
    target = set(target_words)
    item_words = set(match_words(clean_item)) | set(match_words(item_name))
    if not item_words:
        return False
    if target <= item_words:
        return True

    # ``Star Struck`` vs ``Starstruck``, ``Traveling`` vs ``Travelling``: compare
    # the identifying words as one run so spelling slips still match. The item
    # name carries the extension and take markers, the group key does not, so
    # both are worth a look.
    squashed_target = "".join(target_words)
    if any(
        words
        and similarity(squashed_target, "".join(words))
        >= FUZZY_MATCH_MIN_SPELLING_RATIO
        for words in (match_words(item_name), match_words(clean_item))
    ):
        return True

    # Otherwise the names may differ in take markers, but they have to agree on
    # the words that identify the song: a word the archive has never seen (a
    # session number like ``1748 moon``, a title it doesn't carry) must be in
    # the file we are about to hand out.
    if not target & item_words:
        return False
    if not {word for word in target if not word_counts.get(word)} <= item_words:
        return False
    return similarity(clean_target, clean_item) >= FUZZY_MATCH_MIN_PARTIAL_RATIO


def is_candidate_match(item_name: str, term: str) -> bool:
    """Whether a stored file name belongs to the metadata name ``term``.

    Exact names match directly, while the archive's alternate-take markers
    (``Song.l.wav``, ``Song (Alt)`` ...) are accepted as prefixes. Ported from
    the reference indexer so every copy of a recording is matched instead of
    only the single closest name. Only a known file extension is stripped from
    ``term``: a full stop inside a title (``01. intro``, ``Song v1.2``) is part
    of the name, and chopping there made every ``01 ...`` file look like it.
    """
    clean_item = clean_match_key(item_name)
    clean_term = clean_match_key(term)
    if not clean_item or not clean_term:
        return False
    if clean_item == clean_term:
        return True

    item_lower = item_name.lower()
    term_base = name_without_extension(term.lower())
    if any(
        item_lower.startswith(f"{term_base}{sep}")
        for sep in (".l.", ".r.", "_l.", "_r.", ".l.wav", ".r.wav")
    ):
        return True
    if item_lower.startswith(term_base):
        rest = item_lower[len(term_base):]
        if rest and rest[0] in (" ", "-", "_", "(", "[", "."):
            return True
    return False


def simple_view(content: str, *, timeout: int = 60) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=timeout)
    container = discord.ui.Container(accent_color=EMBED_COLOR)
    container.add_item(discord.ui.TextDisplay(content))
    view.add_item(container)
    return view


def text_display(content: str) -> discord.ui.TextDisplay:
    return discord.ui.TextDisplay(content=content)


def small_separator() -> discord.ui.Separator:
    return discord.ui.Separator(spacing=discord.SeparatorSpacing.small)


def make_link_button(label: str, url: str | None) -> discord.ui.Button | None:
    url = valid_http_url(url)
    if not url:
        return None
    return discord.ui.Button(label=label, style=discord.ButtonStyle.link, url=url)


def collect_download_files(
    tagged: list[dict],
    original: list[dict],
) -> list[dict]:
    files: list[dict] = []
    for item in original:
        files.append({**item, "kind": "Original"})
    for item in tagged:
        files.append({**item, "kind": "Tagged"})
    return files


def build_info_header(song: Any) -> str:
    title = get_song_title(song)
    lines = [f"### **{title}**"]

    aka = song_list(
        song,
        "track_titles",
        "trackTitles",
        "alternative_titles",
        "alternativeTitles",
        "alternate_titles",
        "alternateTitles",
    )
    if aka and aka != "N/A":
        parts = [p.strip() for p in aka.split(",") if p.strip() and p.strip().casefold() != title.casefold()]
        if parts:
            lines.append(f"-# Alt Name(s): **{', '.join(parts)}**")

    engineers = song_list(song, "engineers", "engineer", "engineer_names", "engineerNames")
    if engineers and engineers != "N/A":
        lines.append(f"-# Engineer(s): **{engineers}**")

    producers = song_list(song, "producers", "producer", "producer_names", "producerNames")
    if producers and producers != "N/A":
        lines.append(f"-# Producer(s): **{producers}**")

    return "\n".join(lines)


def build_info_details(
    song: Any,
    all_files: list[dict] | None = None,
    active_file: dict | None = None,
    matched_field: str | None = None,
) -> str | None:
    fields: list[str] = []

    era = clean_era_display(first_value(song, "era", "era_name", "eraName"))
    if era:
        fields.append(f"**Era**\n{era}")

    unsurfaced = is_unsurfaced(song)
    file_name = None if unsurfaced else clean(first_value(song, *FILENAME_KEYS))
    if file_name and str(file_name) != "N/A":
        fields.append(f"**File Name**\n{str(file_name).strip()}")
    elif not unsurfaced and active_file and active_file.get("name"):
        fields.append(f"**File Name**\n{active_file['name']}")

    instrumentals = song_list(
        song,
        "instrumental_names",
        "instrumentalNames",
        "instrumentals",
        "instrumental",
        "instrumental_files",
        "instrumentalFiles",
    )
    if instrumentals and instrumentals != "N/A":
        fields.append(f"**Instrumentals**\n{instrumentals}")

    location = display(
        unwrap_named(
            first_value(
                song,
                "recording_locations",
                "recordingLocations",
                "recording_location",
                "recordingLocation",
                "location",
            )
        )
    )
    if location and location != "N/A":
        fields.append(f"**Recording Location**\n{location}")

    recorded = song_list(
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
    )
    if recorded and recorded != "N/A":
        raw_rec = clean_meta_text(recorded)
        if re.search(r"\bFile Exported\b", raw_rec, flags=re.IGNORECASE):
            match = re.search(r"^(.*?)(?:[;,]|\s)*\bFile Exported\b[\s,:.-]*(.*)$", raw_rec, flags=re.IGNORECASE)
            if match:
                rec_part = clean_meta_text(match.group(1))
                exp_part = clean_meta_text(match.group(2))
                rec_lines = []
                if rec_part:
                    rec_lines.append(f"**Recorded**\n{rec_part}")
                if exp_part:
                    rec_lines.append(f"**File Exported**\n{exp_part}")
                if rec_lines:
                    fields.append("\n".join(rec_lines))
            else:
                fields.append(f"**Recorded**\n{raw_rec}")
        else:
            fields.append(f"**Recorded**\n{raw_rec}")

    previewed = song_text(
        song,
        "preview_date",
        "previewDate",
        "first_previewed",
        "firstPreviewed",
        "first_preview_date",
        "firstPreviewDate",
    )
    if previewed and previewed != "N/A":
        fields.append(f"**First Previewed**\n{clean_meta_text(previewed)}")

    surfaced = get_surfaced(song)
    if surfaced and surfaced != "N/A":
        fields.append(f"**Surfaced**\n{clean_meta_text(surfaced)}")

    length = get_length(song)
    if length and length != "N/A":
        fields.append(f"**Length**\n{length}")

    category = display(
        unwrap_named(
            first_value(
                song,
                "category",
                "category_name",
                "categoryName",
                "status",
                "leak_type",
            )
        )
    )
    if category and category != "N/A":
        fields.append(f"**Category**\n{category}")

    bitrate = None if unsurfaced else clean(first_value(song, "bitrate", "available_files", "availableFiles"))
    if bitrate and str(bitrate) != "N/A":
        fields.append(f"**Available Files**\n{str(bitrate).strip()}")
    elif all_files:
        lines = [f"{f.get('kind', 'File')}: {f.get('name', 'Audio')}" for f in all_files[:3]]
        if lines:
            fields.append(f"**Available Files**\n" + "\n".join(lines))

    additional_info = song_text(
        song,
        "additional_information",
        "additionalInformation",
        "additional_info",
        "additionalInfo",
        "info",
        "notes",
    )
    if additional_info and additional_info != "N/A":
        fields.append(f"-# {truncate(additional_info, MAX_NOTES_LENGTH)}")

    if matched_field:
        fields.append(f"-# 🔍 Matched query on: **{matched_field}**")

    return "\n\n".join(fields) if fields else None


class DateSongsView(discord.ui.View):
    """A paged embed of the songs that share a date or a year, grouped by year.

    A busy day (Jan 25 has 100+ leaks) can't fit a single embed, so the songs
    are split across pages instead of being cut off with "+N more".
    """

    #: Small enough that a page's single-year field stays under Discord's 1024
    #: character field limit even with the longest titles.
    PER_PAGE = 12

    def __init__(
        self,
        *,
        title: str,
        description: str,
        entries: list[tuple[int | None, str]],
        author_id: int,
        timeout: int = VIEW_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)
        self.title = title
        self.description = description
        self.entries = entries
        self.author_id = author_id
        self.page = 0
        self.page_count = max(1, (len(entries) + self.PER_PAGE - 1) // self.PER_PAGE)

        self.prev_button = discord.ui.Button(
            label="◀ Prev", style=discord.ButtonStyle.secondary
        )
        self.next_button = discord.ui.Button(
            label="Next ▶", style=discord.ButtonStyle.secondary
        )
        self.prev_button.callback = self._on_prev
        self.next_button.callback = self._on_next
        self.add_item(self.prev_button)
        self.add_item(self.next_button)

        self.embed = discord.Embed(color=EMBED_COLOR)
        self._render()

    def _build_embed(self) -> discord.Embed:
        total = len(self.entries)
        embed = discord.Embed(
            title=self.title,
            description=self.description,
            color=EMBED_COLOR,
        )

        start = self.page * self.PER_PAGE
        chunk = self.entries[start:start + self.PER_PAGE]

        current_year: Any = object()
        lines: list[str] = []

        def flush() -> None:
            if not lines:
                return
            embed.add_field(
                name=str(current_year) if current_year is not None else "Year unknown",
                value="\n".join(lines),
                inline=False,
            )
            lines.clear()

        for year, title in chunk:
            if year != current_year:
                flush()
                current_year = year
            lines.append(f"• {truncate(title, 70)}")
        flush()

        embed.set_footer(
            text=(
                f"Page {self.page + 1}/{self.page_count} • "
                f"{total} song{'s' if total != 1 else ''}"
            )
        )
        return embed

    def _render(self) -> None:
        """Rebuild the embed and the button states for the current page.

        Named ``_render`` and not ``_refresh``: :class:`discord.ui.View`
        already owns ``_refresh(self, components)`` and the library calls it
        with a components list every time Discord sends a MESSAGE_UPDATE for
        a message this view is attached to. Shadowing it with a no-argument
        method raises ``TypeError`` inside the gateway's message parser, which
        kills the websocket and takes the whole process down.
        """
        self.prev_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= self.page_count - 1
        self.embed = self._build_embed()

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        await self._change_page(interaction, -1)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        await self._change_page(interaction, 1)

    async def _change_page(self, interaction: discord.Interaction, delta: int) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Only the person who ran the command can change pages.",
                ephemeral=True,
            )
            return

        self.page += delta
        self._render()
        await interaction.response.edit_message(embed=self.embed, view=self)


class SongInfoFileSelect(discord.ui.Select):
    def __init__(
        self,
        *,
        song: Any,
        tagged: list[dict],
        original: list[dict],
        files: list[dict],
        selected_index: int | None = None,
        matched_field: str | None = None,
        bot: Any = None,
        guild_id: int | None = None,
    ) -> None:
        self.song = song
        self.tagged = tagged
        self.original = original
        self.files = files
        self.matched_field = matched_field
        self.bot = bot
        self.guild_id = guild_id

        options: list[discord.SelectOption] = []
        for idx, item in enumerate(files[:25]):
            name = truncate(str(item.get("name", "File")), 90)
            kind = str(item.get("kind", "File"))
            options.append(
                discord.SelectOption(
                    label=name[:100],
                    value=str(idx),
                    description=kind[:100],
                    default=selected_index == idx,
                )
            )

        placeholder = "Choose a file..."
        if selected_index is not None and 0 <= selected_index < len(files):
            placeholder = truncate(str(files[selected_index].get("name", "File")), 80)

        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        item = self.files[int(self.values[0])]
        download = valid_http_url(item.get("download"))
        if not download:
            await interaction.response.send_message(
                "No download link is available for that file.",
                ephemeral=True,
            )
            return

        view = SongInfoView(
            self.song,
            tagged=self.tagged,
            original=self.original,
            selected_index=int(self.values[0]),
            matched_field=self.matched_field,
            bot=self.bot,
            guild_id=self.guild_id,
        )
        await interaction.response.edit_message(view=view)


class SongInfoView(discord.ui.LayoutView):
    def __init__(
        self,
        song: dict[str, Any],
        *,
        tagged: list[dict] | None = None,
        original: list[dict] | None = None,
        selected_index: int | None = None,
        matched_field: str | None = None,
        timeout: int = VIEW_TIMEOUT,
        bot: Any = None,
        guild_id: int | None = None,
    ):
        super().__init__(timeout=timeout)

        tagged = tagged or []
        original = original or []
        cover = get_cover_url(song)
        all_files = collect_download_files(tagged, original)

        if selected_index is not None and 0 <= selected_index < len(all_files):
            active_file = all_files[selected_index]
        else:
            active_file = find_primary_file(tagged, original)

        container = discord.ui.Container(accent_color=EMBED_COLOR)

        header_text = build_info_header(song)
        if cover:
            container.add_item(
                discord.ui.Section(
                    text_display(header_text),
                    accessory=discord.ui.Thumbnail(media=cover),
                )
            )
        else:
            container.add_item(text_display(header_text))

        container.add_item(small_separator())

        details_text = build_info_details(
            song,
            all_files=all_files,
            active_file=active_file,
            matched_field=matched_field,
        )
        if details_text:
            container.add_item(text_display(details_text))

        action_rows: list[discord.ui.ActionRow | SongInfoFileSelect] = []

        if active_file:
            download_button = mp3_buttons.make_download_button(
                bot,
                guild_id=guild_id,
                label="Download",
                url=valid_http_url(active_file.get("download")),
                is_mp3=True,
                filename=mp3_buttons.mp3_filename(
                    str(active_file.get("name") or "song"), "song"
                ),
            )
            if download_button:
                action_rows.append(discord.ui.ActionRow(download_button))

        if len(all_files) > 1:
            action_rows.append(
                SongInfoFileSelect(
                    song=song,
                    tagged=tagged,
                    original=original,
                    files=all_files,
                    selected_index=selected_index,
                    matched_field=matched_field,
                    bot=bot,
                    guild_id=guild_id,
                )
            )

        if action_rows:
            container.add_item(small_separator())
            for row in action_rows:
                if isinstance(row, discord.ui.ActionRow):
                    container.add_item(row)
                else:
                    container.add_item(discord.ui.ActionRow(row))

        self.add_item(container)


class SongSelect(discord.ui.Select):
    def __init__(self, candidates: list[dict[str, Any]], author_id: int, cog: SongInfo):
        self.candidates = candidates
        self.author_id = author_id
        self.cog = cog

        options = []
        for idx, item in enumerate(candidates[:25]):
            song = item["song"]
            matched_field = item["matched_field"]
            title = get_song_title(song)[:100]
            era = display(unwrap_named(first_value(song, "era", "era_name", "eraName")))

            desc = f"Era: {era} · Matched: {matched_field}"[:100]

            options.append(
                discord.SelectOption(
                    label=title,
                    value=str(idx),
                    description=desc,
                    emoji=emoji_for_era(self.cog.bot, era),
                )
            )

        super().__init__(
            placeholder="Select a song to view its info...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Only the person who ran the command can choose.", ephemeral=True)
            return

        if self.view:
            self.view.selected = True
            self.view.stop()

        await interaction.response.defer()

        chosen_entry = self.candidates[int(self.values[0])]
        view = await self.cog.resolve_song_view(
            chosen_entry["song"],
            matched_field=chosen_entry["matched_field"],
            guild_id=interaction.guild_id,
        )
        await interaction.edit_original_response(view=view)


class SongSelectView(discord.ui.LayoutView):
    def __init__(self, query: str, candidates: list[dict[str, Any]], author_id: int, cog: SongInfo):
        super().__init__(timeout=SELECT_TIMEOUT)
        self.selected = False
        self.message: Optional[discord.Message] = None

        container = discord.ui.Container(accent_color=EMBED_COLOR)
        container.add_item(
            text_display(
                f"# Metadata Search\n"
                f"Found **{len(candidates)}** match{'es' if len(candidates) != 1 else ''} for **{query}**.\n"
                f"-# Select a song below to inspect full metadata:"
            )
        )
        container.add_item(discord.ui.ActionRow(SongSelect(candidates, author_id, cog)))
        self.add_item(container)

    async def on_timeout(self):
        if self.selected or not self.message:
            return
        try:
            await self.message.edit(
                view=simple_view("⏱️ Selection timed out. Please run the command again.", timeout=10)
            )
        except Exception:
            pass


class SongInfoAPI:
    def __init__(self) -> None:
        self.session: aiohttp.ClientSession | None = None
        self._storage_files: list[dict[str, str]] = []
        self._storage_word_counts: Counter[str] = Counter()
        self._storage_timestamp: float = 0
        self._storage_lock = asyncio.Lock()

    async def start(self) -> None:
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
            self.session = aiohttp.ClientSession(
                timeout=timeout,
                headers={"User-Agent": "Timezones-Bot/1.0"},
            )

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def request(self, endpoint: str, params: dict | None = None) -> Any:
        await self.start()
        url = f"{API_BASE}/{endpoint.lstrip('/')}"
        try:
            async with self.session.get(url, params=params) as response:
                if response.status != 200:
                    return None
                return await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

    async def browse_directory(self, path: str) -> list[dict]:
        data = await self.request(
            "files/browse/",
            params={"path": path, "channel": "comp", "page_size": 2000},
        )
        if isinstance(data, dict):
            return data.get("items", [])
        return []

    async def _ensure_storage_index(self) -> None:
        now = asyncio.get_running_loop().time()
        if self._storage_files and (now - self._storage_timestamp < 1800):
            return

        async with self._storage_lock:
            if self._storage_files and (now - self._storage_timestamp < 1800):
                return

            collected: list[dict[str, str]] = []
            for base_folder in ("Original Files", "Tagged Files"):
                top_items = await self.browse_directory(base_folder)
                directories = [item for item in top_items if item.get("type") == "directory"]
                for item in top_items:
                    if item.get("type") != "directory" and item.get("path"):
                        collected.append(
                            {"name": item.get("name", ""), "path": item.get("path", "")}
                        )

                if directories:
                    tasks = [self.browse_directory(sub["path"]) for sub in directories]
                    res_list = await asyncio.gather(*tasks, return_exceptions=True)
                    for res in res_list:
                        if isinstance(res, list):
                            for sub_item in res:
                                if sub_item.get("path"):
                                    collected.append(
                                        {
                                            "name": sub_item.get("name", ""),
                                            "path": sub_item.get("path", ""),
                                        }
                                    )

            if collected:
                self._storage_files = collected
                # Which identifying words the archive actually uses. A fuzzy
                # match has to contain every word it has never seen before.
                self._storage_word_counts = Counter(
                    word
                    for item in collected
                    for word in set(match_words(item.get("name", "")))
                )
                self._storage_timestamp = now

    async def resolve_storage_paths(
        self, query: str, era_hint: str | None = None
    ) -> list[str]:
        """Resolve a metadata file name to *every* stored file it maps to.

        A song is frequently archived more than once (for example a ``.wav``
        and an ``.mp3`` sharing a base name), so returning just the single best
        match would hide the download buttons for the other copies. We resolve
        the best-matching base name and then keep all of its extensions.
        """
        await self._ensure_storage_index()
        if not self._storage_files:
            return []

        clean_target = clean_match_key(query)
        if not clean_target:
            return []

        def ordering(paths: list[str]) -> list[str]:
            unique: list[str] = []
            seen: set[str] = set()
            for path in paths:
                key = path.casefold()
                if key not in seen:
                    seen.add(key)
                    unique.append(path)
            if era_hint:
                hint = era_hint.lower()
                return sorted(unique, key=lambda p: hint not in p.lower())
            return unique

        # Group stored files by their cleaned base name so each extension of a
        # recording (``.mp3`` + ``.wav``) stays together.
        groups: dict[str, list[tuple[str, str]]] = {}
        for item in self._storage_files:
            path = item.get("path", "")
            name = item.get("name", "")
            if not path or not name:
                continue
            base = name_without_extension(name)
            clean_item = clean_match_key(base)
            if clean_item:
                groups.setdefault(clean_item, []).append((path, name))

        if clean_target in groups:
            return ordering([path for path, _ in groups[clean_target]])

        candidate_paths = [
            item.get("path", "")
            for item in self._storage_files
            if item.get("path") and is_candidate_match(item.get("name", ""), query)
        ]
        if candidate_paths:
            return ordering(candidate_paths)

        # Nothing matched by name. A last-resort fuzzy pass is still useful when
        # the archive spells a take differently, but only for files that could
        # plausibly be the same recording - see fuzzy_match_is_plausible().
        best_score = 0.0
        best_key: str | None = None
        for clean_item, entries in groups.items():
            if not any(
                fuzzy_match_is_plausible(
                    clean_target, clean_item, name, self._storage_word_counts
                )
                for _, name in entries
            ):
                continue
            score = difflib.SequenceMatcher(None, clean_target, clean_item).ratio()
            if era_hint and any(era_hint.lower() in path.lower() for path, _ in entries):
                score += 0.15
            if score > best_score:
                best_score = score
                best_key = clean_item
        if best_key is not None and best_score >= FUZZY_MATCH_MIN_RATIO:
            return ordering([path for path, _ in groups[best_key]])
        return []

    async def resolve_storage_path(self, query: str, era_hint: str | None = None) -> str | None:
        paths = await self.resolve_storage_paths(query, era_hint)
        return paths[0] if paths else None


async def resolve_one_file(api: SongInfoAPI, item: Any, era_hint: str | None = None) -> list[dict]:
    """Resolve one metadata entry to every download it points at.

    A single metadata file name can map to several stored files (the ``.wav``
    and ``.mp3`` of the same take), so this returns a list rather than the one
    best path it used to.
    """
    path = file_path_from_item(item)
    direct = direct_file_url(item)

    if isinstance(item, str):
        name = item.split("/")[-1]
    else:
        name = display(
            get_value(item, "filename", "file_name", "fileName", "name", "title", default="Song"),
            "Song",
        )

    resolved_paths: list[str] = []
    if path:
        if "/" in path and ("Original Files" in path or "Tagged" in path):
            resolved_paths = [path]
        else:
            resolved_paths = await api.resolve_storage_paths(path, era_hint)
            if not resolved_paths:
                resolved_paths = [path]

    if not resolved_paths and name:
        resolved_paths = await api.resolve_storage_paths(name, era_hint)

    results: list[dict] = []
    if resolved_paths:
        for final_path in resolved_paths:
            download = make_download_url(final_path)
            if not download:
                continue
            display_name = (
                final_path.split("/")[-1] if "/" in final_path else name
            )[:80]
            results.append({"name": display_name, "download": download})
    elif direct:
        results.append({"name": name[:80], "download": direct})
    return results


async def build_file_groups(
    api: SongInfoAPI,
    song: Any,
) -> tuple[list[dict], list[dict], int]:
    if is_unsurfaced(song):
        return [], [], 0

    raw_items = recursive_file_objects(song)
    raw_items.extend(filename_list(song))

    era_hint = display(unwrap_named(first_value(song, "era", "era_name", "eraName")))
    if era_hint == "N/A":
        era_hint = None

    unique: list[Any] = []
    seen: set[str] = set()
    for item in raw_items:
        identity = (file_path_from_item(item) or direct_file_url(item) or str(item)).casefold()
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(item)

    tagged: list[dict] = []
    original: list[dict] = []
    seen_downloads: set[str] = set()
    for item in unique:
        for resolved in await resolve_one_file(api, item, era_hint):
            download_key = resolved["download"].casefold()
            if download_key in seen_downloads:
                continue
            if resolved["name"].lower().endswith(ARCHIVE_EXTENSIONS):
                continue
            seen_downloads.add(download_key)
            text = f"{resolved['name']} {str(item).lower()}"
            if "tag" in text:
                tagged.append(resolved)
            else:
                original.append(resolved)

    if not original and not tagged:
        title = get_song_title(song)
        for fallback_path in await api.resolve_storage_paths(title, era_hint):
            url = make_download_url(fallback_path)
            if not url or url.casefold() in seen_downloads:
                continue
            seen_downloads.add(url.casefold())
            original.append(
                {
                    "name": fallback_path.split("/")[-1][:80],
                    "download": url,
                }
            )

    return tagged, original, max(len(unique), len(tagged) + len(original))


class SongInfo(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self.api = SongInfoAPI()
        self._catalog_cache: list[dict[str, Any]] = []
        self._cache_timestamp: float = 0
        self._index_lock = asyncio.Lock()

    async def get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
                headers=HEADERS,
            )
        return self.session

    def cog_unload(self):
        async def close():
            if self.session and not self.session.closed:
                await self.session.close()
            await self.api.close()

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(close())
        except RuntimeError:
            pass

    async def _fetch_catalog_page(self, page_num: int) -> list[dict[str, Any]]:
        session = await self.get_session()
        url = f"{SONGS_ENDPOINT}?page={page_num}&page_size={CATALOG_PAGE_SIZE}"
        try:
            async with session.get(url, headers=HEADERS, timeout=15) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    data = parse_api_json(text)
                    if isinstance(data, dict):
                        return data.get("results", [])
                    if isinstance(data, list):
                        return data
        except Exception:
            pass
        return []

    async def _ensure_catalog_index(self) -> None:
        now = asyncio.get_running_loop().time()
        if self._catalog_cache and (now - self._cache_timestamp < CACHE_TTL):
            return

        async with self._index_lock:
            if self._catalog_cache and (now - self._cache_timestamp < CACHE_TTL):
                return

            session = await self.get_session()
            total_pages = 28
            try:
                async with session.get(f"{SONGS_ENDPOINT}?page=1&page_size={CATALOG_PAGE_SIZE}", headers=HEADERS) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        data = parse_api_json(text)
                        if isinstance(data, dict):
                            count = data.get("count", 0)
                            if count:
                                total_pages = (count // CATALOG_PAGE_SIZE) + 1
            except Exception:
                pass

            # Fetch a few pages at a time rather than all 28 at once: the
            # archive rate-limits bursts, and a slow page shouldn't hold up the
            # rest of the sweep.
            semaphore = asyncio.Semaphore(CATALOG_CONCURRENCY)

            async def fetch(page_num: int) -> list[dict[str, Any]]:
                async with semaphore:
                    return await self._fetch_catalog_page(page_num)

            started = asyncio.get_running_loop().time()
            pages_results = await asyncio.gather(
                *(fetch(p) for p in range(1, total_pages + 1)),
                return_exceptions=True,
            )

            all_songs = []
            seen_ids = set()
            for page in pages_results:
                if isinstance(page, list):
                    for item in page:
                        sid = item.get("id") or item.get("name")
                        if sid not in seen_ids:
                            seen_ids.add(sid)
                            all_songs.append(item)

            if all_songs:
                self._catalog_cache = all_songs
                self._cache_timestamp = now
                logger.info(
                    "[SongInfo] Indexed %d tracks for metadata search in %.1fs.",
                    len(all_songs),
                    asyncio.get_running_loop().time() - started,
                )

    async def get_song_details(self, song_id: Any) -> Optional[dict[str, Any]]:
        session = await self.get_session()
        for endpoint in (f"songs/{song_id}/", f"songs/{song_id}"):
            url = f"{API_BASE}/{endpoint}"
            try:
                async with session.get(url, headers=HEADERS, timeout=12) as response:
                    if response.status == 200:
                        text = await response.text()
                        data = parse_api_json(text)
                        if isinstance(data, dict):
                            song_obj = data.get("song") or data.get("data") or data
                            return song_obj if isinstance(song_obj, dict) else data
            except Exception:
                continue
        return None

    def search_metadata(self, query: str) -> list[dict[str, Any]]:
        clean_q = query.strip().lower()
        if not clean_q:
            return []

        exact_matches = []
        partial_matches = []
        fuzzy_matches = []

        for song in self._catalog_cache:
            title = str(get_song_title(song)).lower()
            aka = str(song_list(song, "track_titles", "trackTitles", "alternative_titles")).lower()
            instrumentals = str(song_list(song, "instrumental_names", "instrumentals", "instrumental_files")).lower()
            recorded = str(song_list(song, "record_dates", "recordDates", "recording_date")).lower()
            producers = str(song_list(song, "producers", "producer_names")).lower()
            engineers = str(song_list(song, "engineers", "engineer_names")).lower()
            location = str(song_text(song, "recording_locations", "location")).lower()
            era = str(song_text(song, "era")).lower()

            matched_field = None

            if clean_q in instrumentals:
                matched_field = f"Instrumental ({song_list(song, 'instrumental_names', 'instrumentals')})"
            elif clean_q in recorded:
                matched_field = f"Recorded Date ({song_list(song, 'record_dates')})"
            elif clean_q in title:
                matched_field = "Title"
            elif clean_q in aka:
                matched_field = "Alternate Name"
            elif clean_q in producers:
                matched_field = f"Producer ({song_list(song, 'producers')})"
            elif clean_q in engineers:
                matched_field = f"Engineer ({song_list(song, 'engineers')})"
            elif clean_q in location:
                matched_field = f"Location ({song_text(song, 'recording_locations')})"
            elif clean_q in era:
                matched_field = f"Era ({song_text(song, 'era')})"

            if matched_field:
                entry = {"song": song, "matched_field": matched_field}
                if clean_q == title or clean_q == instrumentals:
                    exact_matches.append(entry)
                else:
                    partial_matches.append(entry)
                continue

            score_inst = difflib.SequenceMatcher(None, clean_q, instrumentals).ratio() if len(clean_q) >= 4 else 0
            score_title = difflib.SequenceMatcher(None, clean_q, title).ratio()
            best_score = max(score_inst, score_title)

            if best_score >= 0.70:
                field_label = "Instrumental" if score_inst > score_title else "Title"
                fuzzy_matches.append((best_score, {"song": song, "matched_field": f"Fuzzy {field_label}"}))

        if exact_matches:
            return exact_matches[:25]

        fuzzy_matches.sort(key=lambda x: x[0], reverse=True)
        ordered_fuzzy = [item for _, item in fuzzy_matches]

        combined = partial_matches + [item for item in ordered_fuzzy if item not in partial_matches]
        return combined[:25]

    async def resolve_song_view(
        self,
        song: dict,
        matched_field: str | None = None,
        *,
        guild_id: int | None = None,
    ) -> SongInfoView:
        song_id = first_value(song, "id", "song_id", "public_id")
        if song_id:
            full_song = await self.get_song_details(song_id)
            if full_song:
                song = full_song

        tagged, original, _ = await build_file_groups(self.api, song)
        return SongInfoView(
            song,
            tagged=tagged,
            original=original,
            matched_field=matched_field,
            bot=self.bot,
            guild_id=guild_id,
        )

    @commands.command(name="songinfo", aliases=["si", "info"])
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def songinfo_command(self, ctx: commands.Context, *, query: str | None = None) -> None:
        if not query:
            prefix = await prefix_for(ctx)
            help_view = simple_view(
                "# Command: songinfo\n"
                "-# Search Juice WRLD songs by title, instrumental name, recording date, or credits.\n\n"
                f"**Syntax** · `{prefix}songinfo <query>`\n"
                f"**Examples** · `{prefix}si xr 5 gz` · `{prefix}si june 2019` · "
                f"`{prefix}si gezin`"
            )
            await ctx.send(view=help_view)
            return

        searching_msg = await ctx.send(view=simple_view(f"🔍 Searching metadata for **{query}**..."))

        await self._ensure_catalog_index()
        candidates = self.search_metadata(query)

        if not candidates:
            session = await self.get_session()
            try:
                async with session.get(SONGS_ENDPOINT, params={"search": query, "page_size": 10}, headers=HEADERS) as resp:
                    if resp.status == 200:
                        data = parse_api_json(await resp.text())
                        results = data.get("results", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                        for s in results:
                            candidates.append({"song": s, "matched_field": "API Title Search"})
            except Exception:
                pass

        if not candidates:
            await searching_msg.edit(
                view=simple_view(f"❌ No songs found matching metadata query **{query}**.")
            )
            return

        if len(candidates) == 1:
            chosen_entry = candidates[0]
            view = await self.resolve_song_view(
                chosen_entry["song"],
                matched_field=chosen_entry["matched_field"],
                guild_id=ctx.guild.id if ctx.guild else None,
            )
            await searching_msg.edit(view=view)
            return

        dropdown_view = SongSelectView(
            query=query,
            candidates=candidates,
            author_id=ctx.author.id,
            cog=self,
        )
        await searching_msg.edit(view=dropdown_view)
        dropdown_view.message = searching_msg

    @songinfo_command.error
    async def songinfo_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ Try again in **{error.retry_after:.1f}s**.")
            return

        logger.exception("[SongInfo] Command error: %s", error)
        await ctx.send(f"❌ **SongInfo error:** `{error}`")

    async def _collect_date_matches(
        self,
        month: int,
        day: int,
        year: int | None = None,
        *,
        field: str = "record_dates",
    ) -> dict[int | None, list[Any]]:
        """Group catalog songs with ``field`` on ``month``/``day`` by year."""
        matches: dict[int | None, list[Any]] = {}
        matcher = DATE_FIELD_MATCHERS.get(field, recorded_date_matches)
        for song in self._catalog_cache:
            for (s_month, start, end, s_year) in matcher(date_field_value(song, field)):
                if s_month != month or not (start <= day <= end):
                    continue
                if year is not None and s_year != year:
                    continue
                matches.setdefault(s_year, []).append(song)
        return matches

    async def _collect_month_matches(
        self,
        month: int,
        year: int | None = None,
        *,
        field: str = "record_dates",
    ) -> dict[int | None, list[Any]]:
        """Group catalog songs with ``field`` in ``month`` by year.

        A song whose date carries no year still belongs to the month, so it
        lands under the ``None`` bucket the same way a day-based query does.
        With ``year`` given only that year's bucket is filled.
        """
        matches: dict[int | None, list[Any]] = {}
        matcher = DATE_FIELD_MATCHERS.get(field, recorded_date_matches)
        for song in self._catalog_cache:
            for (s_month, _start, _end, s_year) in matcher(date_field_value(song, field)):
                if s_month != month:
                    continue
                if year is not None and s_year != year:
                    continue
                matches.setdefault(s_year, []).append(song)
        return matches

    async def _collect_year_matches(
        self,
        year: int,
        *,
        field: str = "record_dates",
    ) -> list[Any]:
        """Every catalog song whose ``field`` mentions ``year``.

        Songs whose date carries no year can't be placed in one, so they stay
        out rather than landing in an "unknown" pile.
        """
        matches: list[Any] = []
        matcher = DATE_FIELD_MATCHERS.get(field, recorded_date_matches)
        for song in self._catalog_cache:
            for (_month, _start, _end, s_year) in matcher(date_field_value(song, field)):
                if s_year == year:
                    matches.append(song)
                    break
        return matches

    async def _render_date_query(
        self,
        ctx: commands.Context,
        month: int | None = None,
        day: int | None = None,
        year: int | None = None,
        *,
        field: str = "record_dates",
        verb: str = "Recorded",
        emoji: str = "🎙️",
    ) -> None:
        """Post the paged list of songs matching a date, a month, or a year.

        ``month`` is ``None`` for a year-only query (``!leakedon 2022``), and
        ``day`` is ``None`` for a month-only one (``!leakedon October``,
        ``!leakedon October 2026``), which lists everything the archive stamped
        with that month.
        """
        verb_lower = verb.lower()
        year_only = month is None
        month_only = month is not None and day is None

        if year_only:
            place = f"in **{year}**"
            loading_label = f"in **{year}**"
        elif month_only:
            place = f"in **{MONTH_NAMES[month]}**"
            loading_label = place
            if year is not None:
                place += f" in **{year}**"
        else:
            place = f"on **{MONTH_NAMES[month]} {day}**"
            loading_label = place
            if year is not None:
                place += f" in **{year}**"

        loading = await ctx.send(
            f"🔍 Finding songs {verb_lower} {loading_label}..."
        )

        await self._ensure_catalog_index()

        if not self._catalog_cache:
            await loading.edit(
                content="❌ Couldn't load the song catalog. Please try again later."
            )
            return

        if year_only:
            songs = await self._collect_year_matches(year, field=field)
            matches: dict[int | None, list[Any]] = {year: songs} if songs else {}
        elif month_only:
            matches = await self._collect_month_matches(month, year, field=field)
        else:
            matches = await self._collect_date_matches(month, day, year, field=field)

        if not matches:
            await loading.edit(
                content=(
                    f"❌ No songs are known to have been {verb_lower} {place}."
                )
            )
            return

        entries: list[tuple[int | None, str]] = []
        for match_year in sorted(matches, key=lambda value: (value is None, value or 0)):
            titles = sorted(
                {get_song_title(song) for song in matches[match_year]},
                key=str.lower,
            )
            entries.extend((match_year, title) for title in titles)

        total = len(entries)
        noun = f"song{'s' if total != 1 else ''}"
        if year_only:
            title = f"{emoji} {verb} in {year}"
            description = f"**{total}** {noun} {verb_lower} in **{year}**."
        elif month_only:
            label = MONTH_NAMES[month]
            title = f"{emoji} {verb} in {label}"
            description = f"**{total}** {noun} {verb_lower} in **{label}**"
            if year is not None:
                title += f" {year}"
                description += f" in **{year}**."
            else:
                description += " across every year."
        else:
            label = f"{MONTH_NAMES[month]} {day}"
            if year is not None:
                label += f", {year}"
            scope = f" in **{year}**" if year is not None else " across every year"
            title = f"{emoji} {verb} on {label}"
            description = (
                f"**{total}** {noun} {verb_lower} on "
                f"**{MONTH_NAMES[month]} {day}**{scope}."
            )

        view = DateSongsView(
            title=title,
            description=description,
            entries=entries,
            author_id=ctx.author.id,
        )
        await loading.edit(content=None, embed=view.embed, view=view)

    @commands.command(
        name="recordedtoday",
        aliases=["recordedonthisday", "todayrecorded", "recday"],
        description="List every song recorded on today's date, for every year.",
    )
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def recordedtoday_command(self, ctx: commands.Context) -> None:
        today = datetime.now().date()
        await self._render_date_query(ctx, today.month, today.day)

    @commands.command(
        name="recordedon",
        aliases=["recorded", "songsrecorded"],
        description="List every song recorded on a given date, month, or year.",
    )
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def recordedon_command(self, ctx: commands.Context, *, date: str | None = None) -> None:
        if not date:
            await ctx.send(
                "# Command: recordedon\n"
                "-# List songs recorded on a specific date, month, or year.\n\n"
                "**Syntax** · `!recordedon <date>` · `!recordedon <month>` · "
                "`!recordedon <month year>` · `!recordedon <year>`\n"
                "**Examples** · `!recordedon October 4` · `!recordedon 4 October` · "
                "`!recordedon October 4th` · `!recordedon October` · "
                "`!recordedon October 2019` · `!recordedon 2019`"
            )
            return

        year = parse_query_year(date)
        if year is not None:
            await self._render_date_query(ctx, year=year)
            return

        month_query = parse_query_month(date)
        if month_query is not None:
            month, month_year = month_query
            await self._render_date_query(ctx, month=month, year=month_year)
            return

        parsed = parse_recorded_date(date)
        if not parsed:
            await ctx.send(
                f"❌ I couldn't understand the date `{date}`. Try something like "
                "`!recordedon October 4`, `!recordedon 4 October`, "
                "`!recordedon October 4th`, `!recordedon October`, "
                "`!recordedon October 2019` or `!recordedon 2019`."
            )
            return

        month, day, year = parsed
        await self._render_date_query(ctx, month, day, year)

    @commands.command(
        name="leakedon",
        aliases=["leaked", "surfacedon"],
        description="List every song leaked on a given date, month, or year.",
    )
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def leakedon_command(self, ctx: commands.Context, *, date: str | None = None) -> None:
        if not date:
            await ctx.send(
                "# Command: leakedon\n"
                "-# List songs leaked on a specific date, month, or year.\n\n"
                "**Syntax** · `!leakedon <date>` · `!leakedon <month>` · "
                "`!leakedon <month year>` · `!leakedon <year>`\n"
                "**Examples** · `!leakedon October 4` · `!leakedon 4 October` · "
                "`!leakedon October` · `!leakedon October 2026` · `!leakedon 2022`"
            )
            return

        year = parse_query_year(date)
        if year is not None:
            await self._render_date_query(
                ctx, year=year, field="date_leaked", verb="Leaked", emoji="💧"
            )
            return

        month_query = parse_query_month(date)
        if month_query is not None:
            month, month_year = month_query
            await self._render_date_query(
                ctx,
                month=month,
                year=month_year,
                field="date_leaked",
                verb="Leaked",
                emoji="💧",
            )
            return

        parsed = parse_recorded_date(date)
        if not parsed:
            await ctx.send(
                f"❌ I couldn't understand the date `{date}`. Try something like "
                "`!leakedon October 4`, `!leakedon 4 October`, "
                "`!leakedon October 4th`, `!leakedon October`, "
                "`!leakedon October 2026` or `!leakedon 2022`."
            )
            return

        month, day, year = parsed
        await self._render_date_query(
            ctx, month, day, year, field="date_leaked", verb="Leaked", emoji="💧"
        )

    @commands.command(
        name="previewedon",
        aliases=["previewed", "firstpreviewed"],
        description="List every song first previewed on a given date, month, or year.",
    )
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def previewedon_command(self, ctx: commands.Context, *, date: str | None = None) -> None:
        if not date:
            await ctx.send(
                "# Command: previewedon\n"
                "-# List songs first previewed on a specific date, month, or year.\n\n"
                "**Syntax** · `!previewedon <date>` · `!previewedon <month>` · "
                "`!previewedon <month year>` · `!previewedon <year>`\n"
                "**Examples** · `!previewedon October 4` · `!previewedon 4 October` · "
                "`!previewedon October` · `!previewedon October 2026` · "
                "`!previewedon 2022`"
            )
            return

        year = parse_query_year(date)
        if year is not None:
            await self._render_date_query(
                ctx, year=year, field="preview_date", verb="Previewed", emoji="🎧"
            )
            return

        month_query = parse_query_month(date)
        if month_query is not None:
            month, month_year = month_query
            await self._render_date_query(
                ctx,
                month=month,
                year=month_year,
                field="preview_date",
                verb="Previewed",
                emoji="🎧",
            )
            return

        parsed = parse_recorded_date(date)
        if not parsed:
            await ctx.send(
                f"❌ I couldn't understand the date `{date}`. Try something like "
                "`!previewedon October 4`, `!previewedon 4 October`, "
                "`!previewedon October 4th`, `!previewedon October`, "
                "`!previewedon October 2026` or `!previewedon 2022`."
            )
            return

        month, day, year = parsed
        await self._render_date_query(
            ctx, month, day, year, field="preview_date", verb="Previewed", emoji="🎧"
        )

    @commands.command(
        name="releasedon",
        aliases=["released", "songsreleased"],
        description="List every song released on a given date, month, or year.",
    )
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def releasedon_command(self, ctx: commands.Context, *, date: str | None = None) -> None:
        if not date:
            await ctx.send(
                "# Command: releasedon\n"
                "-# List songs released on a specific date, month, or year.\n\n"
                "**Syntax** · `!releasedon <date>` · `!releasedon <month>` · "
                "`!releasedon <month year>` · `!releasedon <year>`\n"
                "**Examples** · `!releasedon June 15` · `!releasedon 15 June` · "
                "`!releasedon June` · `!releasedon June 2017` · `!releasedon 2017`"
            )
            return

        year = parse_query_year(date)
        if year is not None:
            await self._render_date_query(
                ctx, year=year, field="release_date", verb="Released", emoji="📀"
            )
            return

        month_query = parse_query_month(date)
        if month_query is not None:
            month, month_year = month_query
            await self._render_date_query(
                ctx,
                month=month,
                year=month_year,
                field="release_date",
                verb="Released",
                emoji="📀",
            )
            return

        parsed = parse_recorded_date(date)
        if not parsed:
            await ctx.send(
                f"❌ I couldn't understand the date `{date}`. Try something like "
                "`!releasedon June 15`, `!releasedon 15 June`, "
                "`!releasedon June 15th`, `!releasedon June`, "
                "`!releasedon June 2017` or `!releasedon 2017`."
            )
            return

        month, day, year = parsed
        await self._render_date_query(
            ctx, month, day, year, field="release_date", verb="Released", emoji="📀"
        )

    @recordedtoday_command.error
    @recordedon_command.error
    @leakedon_command.error
    @previewedon_command.error
    @releasedon_command.error
    async def recorded_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ Try again in **{error.retry_after:.1f}s**.")
            return

        logger.exception("[SongInfo] Date command error: %s", error)
        await ctx.send(f"❌ **Date command error:** `{error}`")

    # NOTE: the slash variant is intentionally not registered here. The bot is
    # already at Discord's hard cap of 100 global application commands, so the
    # command is exposed through the prefix ``songinfo`` command only.
    async def songinfo_slash(
        self,
        interaction: discord.Interaction,
        query: str | None = None,
    ) -> None:
        if not query:
            prefix = await prefix_for(ctx)
            help_view = simple_view(
                "# Command: songinfo\n"
                "-# Search Juice WRLD songs by title, instrumental name, recording date, or credits.\n\n"
                f"**Syntax** · `{prefix}songinfo <query>`\n"
                f"**Examples** · `{prefix}si xr 5 gz` · `{prefix}si june 2019` · "
                f"`{prefix}si gezin`"
            )
            await interaction.response.send_message(view=help_view)
            return

        await interaction.response.send_message(
            view=simple_view(f"🔍 Searching metadata for **{query}**...")
        )

        await self._ensure_catalog_index()
        candidates = self.search_metadata(query)

        if not candidates:
            session = await self.get_session()
            try:
                async with session.get(
                    SONGS_ENDPOINT,
                    params={"search": query, "page_size": 10},
                    headers=HEADERS,
                ) as resp:
                    if resp.status == 200:
                        data = parse_api_json(await resp.text())
                        results = (
                            data.get("results", [])
                            if isinstance(data, dict)
                            else (data if isinstance(data, list) else [])
                        )
                        for s in results:
                            candidates.append(
                                {"song": s, "matched_field": "API Title Search"}
                            )
            except Exception:
                pass

        if not candidates:
            await interaction.edit_original_response(
                view=simple_view(
                    f"❌ No songs found matching metadata query **{query}**."
                )
            )
            return

        if len(candidates) == 1:
            chosen_entry = candidates[0]
            view = await self.resolve_song_view(
                chosen_entry["song"],
                matched_field=chosen_entry["matched_field"],
                guild_id=interaction.guild_id,
            )
            await interaction.edit_original_response(view=view)
            return

        dropdown_view = SongSelectView(
            query=query,
            candidates=candidates,
            author_id=interaction.user.id,
            cog=self,
        )
        msg = await interaction.edit_original_response(view=dropdown_view)
        dropdown_view.message = msg

    async def songinfo_slash_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        logger.exception("[SongInfo] Slash command error: %s", error)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                f"❌ **SongInfo error:** `{error}`", ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"❌ **SongInfo error:** `{error}`", ephemeral=True
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SongInfo(bot))
