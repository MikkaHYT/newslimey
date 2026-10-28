# cogs/vault.py
#
# The Vault -- a Juice WRLD leak/session trading-card game played with the
# server's own coins.
#
# Songs and studio sessions are discovered with the same API/storage logic the
# ,leak and ,session commands use. Two things decide what an item is worth:
#
# * the recording year (newer = rarer) and the category, which is one of the
#   four the API actually serves -- released, unreleased, unsurfaced or
#   recording_session (see :func:`normalize_category`);
# * the economy itself: every item freezes a *share* of what the server's
#   members are holding, and the coin price is that share of the live money
#   supply. As the economy mints, prices rise; as coins are burned, they fall,
#   so a collection's value genuinely rises and falls with the server.
#
# Money is the guild-scoped economy behind ``,balance``: every purchase, payout,
# auction escrow and trade goes through ``bot.database``, which resolves the
# running server's economy scope (a server with ``,guildeconomy on`` keeps its
# own wallet, bank and treasury). Nothing here touches the legacy refbot engine.
# Collections are filed under the same scope, so coins earned in one server can
# only buy that server's vault.
#
# All commands are subcommands of ,vault so they never collide with the
# economy cog's ,inventory / ,buy / ,sell:
#
#   ,vault search <song>     Look a song/session up and price it
#   ,vault buy <song>        Buy a song/session with your wallet
#   ,vault sell [song]       Sell an item back for coins
#   ,vault list [@user]      Show a collection and what it is worth
#   ,vault leak [song]       Leak an owned item as a groupbuy
#   ,vault share <song> @user Send the file to a member who must accept it
#   ,vault trade @user <song> [price]  Trade with another player
#   ,vault auction <song> <start price>  Auction an item to the server
#   ,vault market           Show the economy the prices are measured against
#   ,vault achievements     Vault achievements and their coin rewards
#   ,vault leaderboard      Top collections by value

from __future__ import annotations

import asyncio
import difflib
import html
import json
import logging
import math
import os
import random
import re
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote, urlparse

import aiohttp
import discord
from discord.ext import commands, tasks

from config import EMBED_COLOR
from utils.amount import AmountUtils
from utils.core.economy_scope import current_scope, scope_for_guild, use_scope
from utils.core.prefixes import prefix_for

logger = logging.getLogger(__name__)

#: The coin glyph the native economy shows next to money. Kept in step with
#: ``cogs.economy`` so a vault price reads exactly like a ``,balance`` figure.
CURRENCY = "<:coin:1359823671581085847>"


#: Past this a grouped number stops being readable -- this economy's money runs
#: to sextillions -- so money switches to the suffix ladder instead.
SHORT_FORMAT_THRESHOLD = 1_000_000


def compact_number(amount: int | float) -> str:
    """``compact_number(12_500)`` -> ``12.5k``; ``compact_number(6.5e23)`` -> ``655 sext``.

    The ladder matches the native economy cog's short formatter, which is what
    ``,balance`` uses for large figures, so a vault price and a wallet read the
    same way. Decimal keeps the digits exact, which floats cannot do up here.
    """
    try:
        value = Decimal(str(amount if amount is not None else 0))
    except (InvalidOperation, TypeError, ValueError):
        return "0"
    negative = value < 0
    value = abs(value)
    # Suffix spacing matches the native economy cog exactly: no space for ``k``,
    # a leading space for every word from ``mil`` up (``12.5k``, ``3.3 quin``).
    for limit, suffix in (
        (Decimal("1e33"), " dec"),
        (Decimal("1e30"), " non"),
        (Decimal("1e27"), " oct"),
        (Decimal("1e24"), " sept"),
        (Decimal("1e21"), " sext"),
        (Decimal("1e18"), " quin"),
        (Decimal("1e15"), " quad"),
        (Decimal("1e12"), " tril"),
        (Decimal("1e9"), " bil"),
        (Decimal("1e6"), " mil"),
        (Decimal("1e3"), "k"),
    ):
        if value >= limit:
            text = f"{value / limit:.2f}".rstrip("0").rstrip(".")
            return f"{'-' if negative else ''}{text}{suffix}"
    return f"{'-' if negative else ''}{int(value):,}"


def format_cash(amount: int | float, *, compact: bool = False) -> str:
    """``format_cash(1234)`` -> ``<:coin:…> 1,234``; big sums are abbreviated.

    Mirrors the native economy cog's money display rather than the old refbot
    module this file used to import, so vault figures and ``,balance`` agree.
    Vault prices are shares of the whole economy, so they routinely run past a
    sextillion; past ``SHORT_FORMAT_THRESHOLD`` they print as ``3.28 quin``
    instead of a wall of digits, while a small reward still reads exactly.
    """
    value = abs(float(amount or 0))
    if compact or value >= SHORT_FORMAT_THRESHOLD:
        return f"{CURRENCY} {compact_number(amount)}"
    return f"{CURRENCY} {int(round(float(amount))):,}"


def format_cash_short(amount: int | float) -> str:
    """Just the number, abbreviated when large, for tight spots like a button."""
    if abs(float(amount or 0)) >= SHORT_FORMAT_THRESHOLD:
        return compact_number(amount)
    return f"{int(round(float(amount))):,}"


def achievement_note(awarded: list[dict]) -> str:
    """The block a payout gains when it also unlocked vault achievements."""
    if not awarded:
        return ""
    lines = [
        f"{item['emoji']} **{item['title']}** — +{format_cash(item['reward'])}"
        for item in awarded
    ]
    return "\n\n**🏆 Achievements unlocked**\n" + "\n".join(lines)

API_ROOT = "https://juicewrldapi.com"
API_BASE = f"{API_ROOT}/juicewrld"
DOWNLOAD_BASE = f"{API_BASE}/files/download/"

API_TIMEOUT = 25
MAX_RESULTS = 25
MAX_META_LENGTH = 90
MAX_NOTES_LENGTH = 160
VIEW_TIMEOUT = 900
SELECT_TIMEOUT = 120
LIST_PER_PAGE = 10
OFFER_TIMEOUT = 300

# An auction closes once nobody has bid for this long. With no bids at all the
# item is returned to the seller instead of being sold.
AUCTION_IDLE_SECONDS = 30 * 60
# How often the background loop checks for auctions that have gone quiet.
AUCTION_CHECK_INTERVAL = 30

VAULT_DATA_PATH = Path(__file__).resolve().parent.parent / "vault_data.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}

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

IMAGE_EXTENSIONS = {
    "jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "tif",
    "heic", "svg", "avif", "jfif",
}

# ---------------------------------------------------------------------------
# Price model
# ---------------------------------------------------------------------------

# Prices are never fixed coin figures. A fixed figure is wrong the moment it
# is written: the shared economy here holds ~2.2 sextillion coins in total
# supply and its richest member alone holds hundreds of sextillions, so an item
# priced at "650,000" reads as pocket change. Instead every item freezes a
# *share* of the money members are actually holding, and the coin price is that
# share applied to the scope's live supply. A hyperinflated server therefore
# gets hyperinflated prices, a quiet one gets cheap ones, and minting or burning
# moves the whole market without anyone recalibrating a table.
#
# The floor of the market is a 2014 *released* song (the cheapest thing that can
# exist) and every year and category is a multiple of it, so one number sets the
# whole curve. Against the current circulating supply of 6.55e23 that reads:
#
#     2014 released       3.3 quadrillion
#     2019 released      17.6 quadrillion
#     2019 unreleased    35.2 quintillion
#     2019 session       88.1 quintillion
#     2019 unsurfaced   704.6 quintillion
OLDEST_YEAR = 2014
NEWEST_YEAR = 2019

#: The fraction of circulating supply a 2014 released song costs -- the floor of
#: the market. Everything else scales off this single number.
ECONOMY_SHARE_FLOOR = 5e-6

#: Bounds on the resulting share, so one wild economy can never make a grail
#: free or hand a single item the entire money supply.
ECONOMY_SHARE_MIN = 1e-12
ECONOMY_SHARE_MAX = 2e-2

#: Used only until the market has been priced once in a scope, so an item can
#: never be free before the first ``,vault`` command has run.
FALLBACK_ECONOMY_SIZE = 1_000_000

#: Every year newer than OLDEST_YEAR multiplies the price by this much.
#: 1.4**5 puts a 2019 unreleased track about 5.4x a 2014 one.
YEAR_GROWTH = 1.4

#: The category the floor share is defined against -- the cheapest there is.
#: ``economy_share`` divides by its multiplier so the floor really is the floor
#: of the market.
BASE_CATEGORY = "released"

#: Hard floor for a live price, so nothing is ever actually free.
MIN_PRICE = 1

# Every price also gets a one-off random roll inside this range so two items
# from the same year and category never cost the same (e.g. 5.5k vs 7.2k
# instead of a flat 6.25k every time). The roll happens once, when the price
# is first frozen, and never changes afterwards.
PRICE_RANDOM_RANGE = (0.8, 1.5)

# Category multipliers, keyed by the *only* categories the API serves -- see
# ``/juicewrld/categories/``: released, unreleased, unsurfaced and
# recording_session. The old model invented "leaked", "snippet", "preview",
# "instrumental" and "stem" buckets, none of which the API ever returns, so
# every song was being priced off a made-up category. Unsurfaced material is
# the holy grail of a vault, a session is a hand-made file, and a released song
# is worth least because the audio is already public.
CATEGORY_MULTIPLIERS: tuple[tuple[str, float], ...] = (
    ("unsurfaced", 20.0),
    ("unreleased", 1.0),
    ("recording_session", 2.5),
    ("released", 0.5),
)
DEFAULT_CATEGORY_MULTIPLIER = 1.0

#: Human labels for those four categories, used on cards and in the list.
CATEGORY_LABELS: dict[str, str] = {
    "unsurfaced": "Unsurfaced",
    "unreleased": "Unreleased",
    "recording_session": "Recording Session",
    "released": "Released",
}

#: Emoji per category, so a collection list reads at a glance.
CATEGORY_EMOJI: dict[str, str] = {
    "unsurfaced": "💎",
    "unreleased": "🎵",
    "recording_session": "🎧",
    "released": "📀",
}

# Selling returns most of the value; leaked goods are heavily discounted.
SELL_RATE = 0.85
LEAKED_SELL_RATE = 0.40
# Leaking an item runs a "groupbuy": buyers together pay a random share of
# the item's value inside this range, and the leaker pockets the final pot.
GROUPBUY_MIN_RATE = 0.75
GROUPBUY_MAX_RATE = 2.50
# Retained for any legacy math that wants the old flat bounty rate.
LEAK_PAYOUT_RATE = 0.50

# ---------------------------------------------------------------------------
# Live market: the market *is* the economy
# ---------------------------------------------------------------------------
# A frozen *share* is what an item keeps; its coin price is recomputed from the
# scope's live supply. Inflation and deflation therefore need no fudge factor:
# when the money members hold grows, ``market_size`` grows and every price rises
# with it, and when coins are burned or drained back to the treasury, prices
# fall. An item bought years ago keeps its place in the market automatically.
#
# ``anchor_size`` records the first size a scope was ever priced at (eased
# toward the live size on a long timer) purely so ``,vault market`` can report
# how far the economy has moved since the vault opened there.
MARKET_CACHE_SECONDS = 60
#: How long the anchor holds before it is eased toward the live size. Without
#: this the reported change would only ever grow in one direction.
MARKET_ANCHOR_SECONDS = 45 * 24 * 3600
#: How far the anchor moves toward the live size when it is re-seeded.
MARKET_ANCHOR_DRIFT = 0.5

# Sharing a copy around cheapens it; leaking it tanks its value outright.
SHARE_VALUE_DROP = 0.05
LEAK_VALUE_DROP = 0.75

# An OG (original) file trades at half its song's value and gains a fifth
# more once that song has been leaked anywhere in the vault.
OG_FILE_RATE = 0.50
OG_FILE_LEAK_BONUS = 0.20

SESSION_SOURCES: tuple[tuple[str, str], ...] = (
    ("Studio Sessions", "Studio Session"),
    ("Session Edits", "Session Edit"),
)


# ---------------------------------------------------------------------------
# Generic helpers (shared shape with cogs/leak.py and cogs/sessions.py)
# ---------------------------------------------------------------------------

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


def valid_http_url(value: Any) -> Optional[str]:
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


def normalize_path(path: Any) -> Optional[str]:
    if not path:
        return None
    path = str(path).strip()
    if not path or path.startswith(("http://", "https://")):
        return None
    while path.startswith("/"):
        path = path[1:]
    return path


def make_download_url(path: str) -> Optional[str]:
    normalized = normalize_path(path)
    if not normalized:
        return None
    return f"{DOWNLOAD_BASE}?path={quote(normalized, safe='')}&channel=comp"


def clean_match_key(value: str) -> str:
    value = re.sub(r"\.[a-zA-Z0-9]+$", "", value)
    value = re.sub(r"[_\-\.]+", " ", value)
    value = re.sub(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}", "", value)
    value = re.sub(r"\s+", " ", value).strip().lower()
    return value


def clean_session_key(value: str) -> str:
    value = clean_match_key(value)
    value = re.sub(
        r"\s+(?:protools|session\s*edit|session|studio|edit|v\d+(?:\.\d+)?)\b",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", value).strip().lower()


def clean_meta_text(value: str) -> str:
    value = value.strip()
    value = re.sub(
        r"^(?:recorded|first previewed|previewed|surfaced|released|file exported)[\s,.:-]*",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip()
    value = re.sub(r"^[,.:\s]+", "", value).strip()
    value = re.sub(r"[.,\s]+$", "", value).strip()
    return re.sub(r"\s+", " ", value)


def clean_era_display(era_raw: Any) -> Optional[str]:
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


def truncate(value: str, limit: int = MAX_META_LENGTH) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


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


def resolve_cover_url(value: Any) -> Optional[str]:
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
        return f"{API_ROOT}{value}"
    if value.startswith(("images/", "media/")):
        return f"{API_ROOT}/{value}"
    return None


def get_cover_url(song: Any) -> Optional[str]:
    for field in COVER_FIELDS:
        value = first_value(song, field, default=None)
        if isinstance(value, dict):
            value = get_value(value, "url", "src", "href", "image_url", "imageUrl")
        result = resolve_cover_url(value)
        if result:
            return result
    return None


def get_category(song: Any) -> str:
    """The song's category, folded onto one of the API's four real values."""
    value = first_value(
        song,
        "category", "category_name", "categoryName",
        "status", "leak_type", "classification",
    )
    return normalize_category(display(unwrap_named(value)))


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


def get_recorded_text(song: Any) -> str:
    return song_list(
        song,
        "record_dates", "recordDates",
        "recording_dates", "recordingDates",
        "recording_date", "recordingDate",
        "recorded", "recorded_date", "recordedDate",
    )


def year_from_text(value: Any) -> Optional[int]:
    text = display(value)
    if not text or text == "N/A":
        return None
    match = re.search(r"\b(19\d{2}|20\d{2})\b", clean_meta_text(text))
    return int(match.group(1)) if match else None


def get_record_year(song: Any) -> Optional[int]:
    for value in (
        get_recorded_text(song),
        get_surfaced(song),
        song_text(
            song,
            "preview_date", "previewDate",
            "first_previewed", "firstPreviewed",
            "first_preview_date", "firstPreviewDate",
        ),
    ):
        year = year_from_text(value)
        if year:
            return year
    return None


def filename_list(song: Any) -> list[str]:
    value = first_value(song, *FILENAME_KEYS, default=None)
    if value is None:
        return []
    if isinstance(value, str):
        return [line.strip() for line in value.split("\n") if line.strip()]
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


def file_path_from_item(item: Any) -> Optional[str]:
    if isinstance(item, str):
        if item.startswith(("http://", "https://")):
            return None
        return normalize_path(item)
    if isinstance(item, dict):
        return normalize_path(get_value(item, *FILE_PATH_KEYS, default=None))
    return None


def direct_file_url(item: Any) -> Optional[str]:
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


def is_image_path(path: Optional[str]) -> bool:
    if not path:
        return False
    name = str(path).split("/")[-1]
    if "." not in name:
        return False
    return name.rsplit(".", 1)[1].lower() in IMAGE_EXTENSIONS


def is_audio_path(path: Optional[str]) -> bool:
    return bool(path) and not is_image_path(path)


# ---------------------------------------------------------------
# Session-name matching (so "Lean" never matches "Lean Wit Me")
# ---------------------------------------------------------------

GENERIC_SESSION_TOKENS = {
    "session", "studio", "edit", "edits", "demo", "demos", "acapella",
    "instrumental", "og", "original", "alt", "alternate", "alternative",
    "v2", "v3", "v4", "v5", "take", "takes", "part", "pt", "full",
    "cdq", "clean", "explicit", "remaster", "remastered", "remix",
    "leak", "leaked", "chop", "chopped", "sped", "slowed", "reverb",
    "pitched", "stem", "stems", "vocals", "final", "finished",
    "unreleased", "released", "preview", "snippet", "master", "masters",
    "outro", "intro", "interlude", "feat", "featuring", "ft",
    "juice", "wrld", "juicewrld",
}


def _name_candidates(name: str) -> list[str]:
    base = name.rsplit(".", 1)[0] if "." in name else name
    keys = [clean_session_key(base)]
    if " - " in base:
        keys.append(clean_session_key(base.rsplit(" - ", 1)[1]))

    output: list[str] = []
    for key in keys:
        if key and key not in output:
            output.append(key)
        stripped = key
        if stripped.startswith("juice wrld"):
            stripped = stripped[len("juice wrld"):].strip()
        elif stripped.endswith(" juice wrld"):
            stripped = stripped[: -len(" juice wrld")].strip()
        if stripped and stripped not in output:
            output.append(stripped)
    return output


def _title_phrase_match(name: str, target: str) -> bool:
    target_tokens = target.split()
    if not target_tokens:
        return False

    for candidate in _name_candidates(name):
        cand_tokens = candidate.split()
        if len(cand_tokens) < len(target_tokens):
            continue
        for start in range(len(cand_tokens) - len(target_tokens) + 1):
            if cand_tokens[start:start + len(target_tokens)] != target_tokens:
                continue
            leftover = cand_tokens[:start] + cand_tokens[start + len(target_tokens):]
            if all(t in GENERIC_SESSION_TOKENS or t.isdigit() for t in leftover):
                return True
    return False


def _name_matches_song(name: str, target_keys: set[str]) -> bool:
    if any(c in target_keys for c in _name_candidates(name)):
        return True
    return any(_title_phrase_match(name, k) for k in target_keys)


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def text_display(content: str) -> discord.ui.TextDisplay:
    return discord.ui.TextDisplay(content=content)


# discord.py exposes SeparatorSpacing at the top level in newer versions but
# under ``discord.ui`` in older ones; support both so the cog never blows up.
_SEPARATOR_SMALL = getattr(
    getattr(discord, "SeparatorSpacing", None), "small", None
) or getattr(discord.ui, "SeparatorSpacing").small


def small_separator() -> discord.ui.Separator:
    return discord.ui.Separator(spacing=_SEPARATOR_SMALL)


def simple_view(content: str, *, timeout: int = 60) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=timeout)
    container = discord.ui.Container(accent_color=EMBED_COLOR)
    container.add_item(text_display(content))
    view.add_item(container)
    return view


def make_link_button(label: str, url: Optional[str]) -> Optional[discord.ui.Button]:
    url = valid_http_url(url)
    if not url:
        return None
    return discord.ui.Button(label=label, style=discord.ButtonStyle.link, url=url)


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

def normalize_category(value: Any) -> str:
    """Fold arbitrary category text onto one of the API's four real categories.

    ``/juicewrld/categories/`` serves exactly ``released``, ``unreleased``,
    ``unsurfaced`` and ``recording_session``. Anything unrecognised is treated
    as unreleased material rather than being priced off an invented bucket, so
    a change on the API's side can never silently mis-price the market.

    Note the plain strip/lower here: :func:`clean_meta_text` is a *date line*
    cleaner that strips a leading "released", which would turn the category
    "released" into nothing at all.
    """
    text = str(value or "").strip().lower()
    if not text or text in {"n/a", "none", "unknown", "null"}:
        return "unreleased"
    for name, _multiplier in CATEGORY_MULTIPLIERS:
        if name in text:
            return name
    # Free-text forms the API's other fields use for the same four buckets.
    if "surface" in text:
        return "unsurfaced"
    if "session" in text:
        return "recording_session"
    if "release" in text:
        return "released"
    return "unreleased"


def category_label(category: Any) -> str:
    """The display name for a category (``recording_session`` -> ``Recording
    Session``)."""
    return CATEGORY_LABELS.get(normalize_category(category), "Unreleased")


def category_emoji(category: Any) -> str:
    return CATEGORY_EMOJI.get(normalize_category(category), "🎵")


def category_multiplier(category: str) -> float:
    wanted = normalize_category(category)
    for name, multiplier in CATEGORY_MULTIPLIERS:
        if name == wanted:
            return multiplier
    return DEFAULT_CATEGORY_MULTIPLIER


def economy_share(
    *,
    year: Optional[int],
    category: str,
    kind: str = "song",
    roll: Optional[float] = None,
) -> float:
    """The fraction of the scope's economy one item is worth.

    This is the part frozen in ``vault_data.json``, not a coin amount. Newer
    recordings cost more (``YEAR_GROWTH`` per year after ``OLDEST_YEAR``) and
    the category multiplies the floor (unsurfaced 20x, session 2.5x, unreleased
    1x, released 0.5x -- all relative to the floor category). Applying the share
    to the scope's live supply gives today's coin price, which is what lets a
    collection gain and lose value as the economy inflates and deflates.

    ``roll`` pins the one-off random spread between two items of the same year
    and category (``1.0`` gives the plain price); ``None`` draws it from
    ``PRICE_RANDOM_RANGE``.

    ``kind`` is accepted for backwards compatibility. Sessions used to be
    priced by kind, but ``recording_session`` is a real category now, so the
    multiplier comes from the category alone and the two can never disagree.
    """
    try:
        reference = int(year) if year is not None else NEWEST_YEAR
    except (TypeError, ValueError):
        reference = NEWEST_YEAR
    reference = min(max(reference, OLDEST_YEAR), NEWEST_YEAR)
    year_factor = YEAR_GROWTH ** (reference - OLDEST_YEAR)

    floor_multiplier = category_multiplier(BASE_CATEGORY) or 1.0
    cat_factor = category_multiplier(category) / floor_multiplier
    if roll is None:
        roll = random.uniform(*PRICE_RANDOM_RANGE)

    share = ECONOMY_SHARE_FLOOR * year_factor * cat_factor * roll
    return max(ECONOMY_SHARE_MIN, min(ECONOMY_SHARE_MAX, share))


def compute_price(
    *,
    year: Optional[int],
    category: str,
    kind: str = "song",
    economy_size: float,
    roll: Optional[float] = None,
) -> int:
    """Today's price of an item, in coins: its share of the economy.

    ``economy_size`` is the scope's live money supply (see
    ``VaultStore.market_size``), so the same song costs radically different
    coins on a server holding a few million and one holding a few sextillion.
    """
    share = economy_share(year=year, category=category, kind=kind, roll=roll)
    return price_for_share(share, economy_size)


def price_for_share(share: int | float, economy_size: int | float) -> int:
    """Apply a frozen share to a scope's supply to get a coin price."""
    try:
        amount = float(share) * max(0.0, float(economy_size or 0))
    except (TypeError, ValueError):
        return MIN_PRICE
    return max(MIN_PRICE, int(round(amount)))


def price_movement(base_price: int | float, price: int | float) -> str:
    """A short ``▲ 12%`` / ``▼ 8%`` marker against the frozen base.

    Empty when the market is effectively flat, so a card only mentions the
    market when it has actually moved the price.
    """
    base = float(base_price or 0)
    current = float(price or 0)
    if base <= 0 or current <= 0:
        return ""
    change = (current - base) / base * 100
    if abs(change) < 0.5:
        return ""
    arrow = "\u25B2" if change > 0 else "\u25BC"
    return f"{arrow} {abs(change):.1f}%"


def song_key(song: Any) -> str:
    song_id = first_value(
        song,
        "id", "song_id", "songId", "public_id", "publicId",
        default=None,
    )
    if song_id is not None:
        return f"song:{song_id}"
    return f"song:{clean_match_key(get_song_title(song))}"


def item_display_name(item: dict) -> str:
    return str(item.get("title") or item.get("key") or "Unknown")


def is_session_item(item: dict) -> bool:
    """Whether an item belongs in the vault's sessions tally.

    Dedicated session files carry ``kind == "session"``, while API tracks the
    server files under sessions carry the ``recording_session`` category. The
    inventory must count both as sessions instead of only the former.
    """
    if str(item.get("kind") or "").strip().lower() == "session":
        return True
    category = str(item.get("category") or "").strip().lower()
    return "session" in category


# ---------------------------------------------------------------------------
# Persistence: frozen prices + owned collections
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_user() -> dict:
    """A fresh member record: items, lifetime counters and unlocked badges.

    The counters are part of the saved record rather than being recomputed from
    the item list, because "sold five items" cannot be read back off a
    collection that no longer holds them.
    """
    return {"items": {}, "stats": {}, "achievements": {}}


def scoped_user_key(user_id: int, scope: int | None = None) -> str:
    """The store key for a member inside one economy scope.

    A server running its own economy (``!guildeconomy on``) has its own coins,
    so it gets its own vault: the same member can hold one collection per
    server, and coins earned in one server can never buy items in another.
    """
    scope = current_scope() if scope is None else int(scope)
    return f"{int(scope)}:{int(user_id)}"


# Keys copied into the saved snapshot; everything else (files, covers) is
# transient and can be rebuilt from the API. ``base_price`` rides along so an
# owned item can be revalued without another catalog lookup.
ITEM_FIELDS = (
    "key", "kind", "title", "category", "year", "era",
    "path", "download", "cover", "og_of", "song_price", "share", "base_price",
)

# Bump this whenever the price curve or the category model changes. Frozen
# prices are then recalculated from their stored year/category so the whole
# market moves together instead of old entries keeping stale prices.
# v5: priced in coins, off the API's four real categories.
# v6: priced as a share of the scope's economy, so the market scales with the
#     money members actually hold instead of a stale six-figure table.
PRICING_VERSION = 6


#: Vault achievements. Each one pays a coin reward out of the server's own
#: treasury the moment its target is met, so the vault feeds back into the
#: economy instead of being a closed side-game.
#:
#: ``weight`` is in units of ``REWARD_SHARE_UNIT`` (see
#: :func:`achievement_reward`): a badge's payout is a slice of the server's
#: market, not a fixed sum. A fixed figure is wrong twice over on an economy
#: like this one -- 500 coins is nothing next to a sextillion-coin balance, and
#: the balance columns are stored as floats, so a payout smaller than the
#: floating-point step at that size is credited as *nothing at all* while still
#: writing a ledger row. A market share lands above that step on every server.
#: The weights keep the proportions of the original coin table (500 = 1).
VAULT_ACHIEVEMENTS: dict[str, dict] = {
    "first_card": {
        "title": "First Pressing",
        "emoji": "🃏",
        "description": "Own your first vault item.",
        "weight": 1,
    },
    "session_head": {
        "title": "Session Head",
        "emoji": "🎧",
        "description": "Own a recording session.",
        "weight": 3,
    },
    "holy_grail": {
        "title": "Holy Grail",
        "emoji": "💎",
        "description": "Own an unsurfaced song.",
        "weight": 10,
    },
    "og_file": {
        "title": "Originals",
        "emoji": "📼",
        "description": "Own an OG file.",
        "weight": 6,
    },
    "collector_10": {
        "title": "Crate Digger",
        "emoji": "📦",
        "description": "Own ten items at once.",
        "weight": 5,
    },
    "collector_25": {
        "title": "Archivist",
        "emoji": "🗄️",
        "description": "Own twenty-five items at once.",
        "weight": 15,
    },
    "flipper": {
        "title": "Flipper",
        "emoji": "🔁",
        "description": "Sell five items.",
        "weight": 4,
    },
    "leaker": {
        "title": "The Leaker",
        "emoji": "💧",
        "description": "Leak three items to a groupbuy.",
        "weight": 10,
    },
    "tycoon": {
        "title": "Vault Tycoon",
        "emoji": "🏆",
        "description": "Hold a collection worth 1% of the server's money supply.",
        "weight": 40,
    },
}

#: The share of the market the lightest badge pays -- about half of a 2014
#: released song, which is the cheapest item the vault can hold.
REWARD_SHARE_UNIT = 2.5e-6


def achievement_reward(weight: int, market_size: int | float) -> int:
    """The coins a badge pays on a market of ``market_size``.

    Deliberately a share of the economy rather than a fixed sum: a small fixed
    payout is both meaningless and, at a whale's balance, below the step of the
    float the balance is stored in, which silently swallows it.
    """
    try:
        size = max(0.0, float(market_size or 0))
    except (TypeError, ValueError):
        size = 0.0
    return max(1, int(round(size * REWARD_SHARE_UNIT * max(1, int(weight or 1)))))

def _snapshot(item: dict) -> dict:
    return {field: item.get(field) for field in ITEM_FIELDS if item.get(field) is not None}


def stored_item_price(record: dict) -> int:
    """The catalog base an owned record was frozen at, as a display fallback.

    A collection view should always be built with the cog's live
    :meth:`VaultStore.item_value`, which re-prices an item off today's market.
    This is only what a view falls back to when it is built without one, and
    deliberately reads the catalog base rather than ``price_paid`` so it never
    quotes an item at the figure its owner paid for it.
    """
    item = record.get("item") if isinstance(record.get("item"), dict) else {}
    for value in (item.get("base_price"), item.get("song_price"), item.get("price")):
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return 0


#: What each achievement needs, as ``achievement key -> (stats field, target)``.
#: Kept beside the catalogue so the two can't drift apart, and named separately
#: from the stat it reads because several achievements watch the same field.
VAULT_ACHIEVEMENT_RULES: dict[str, tuple[str, int]] = {
    "first_card": ("items", 1),
    "session_head": ("sessions", 1),
    "holy_grail": ("unsurfaced", 1),
    "og_file": ("og_files", 1),
    "collector_10": ("items", 10),
    "collector_25": ("items", 25),
    "flipper": ("sold", 5),
    "leaker": ("leaked", 3),
    # Measured against the server's money supply, not a coin figure: on an
    # economy this size a fixed target is either free or impossible.
    "tycoon": ("tycoon", 1),
}

#: Share of the market a collection must reach to earn Vault Tycoon. A single
#: grail is about 0.1% of the economy, so this takes a few serious pieces.
TYCOON_MARKET_SHARE = 0.01


class VaultStore:
    """JSON-backed store for frozen prices and player collections."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {
            "catalog": {},
            "users": {},
            "leaked_songs": {},
            "auctions": {},
            "markets": {},
        }
        self._lock = asyncio.Lock()
        self._load()

    # -- disk --------------------------------------------------------

    def _load(self) -> None:
        try:
            if VAULT_DATA_PATH.exists():
                with open(VAULT_DATA_PATH, encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict):
                    self._data = {
                        "pricing_version": data.get("pricing_version"),
                        "catalog": data.get("catalog", {}) or {},
                        "users": data.get("users", {}) or {},
                        "leaked_songs": data.get("leaked_songs", {}) or {},
                        "auctions": data.get("auctions", {}) or {},
                        "markets": data.get("markets", {}) or {},
                    }
        except Exception:
            logger.exception("[Vault] Could not read %s", VAULT_DATA_PATH)
        self._migrate_users()
        self._migrate_markets()
        self._migrate_pricing()

    def _migrate_users(self) -> None:
        """Move pre-scope collections onto the bot-wide scope key.

        Older files keyed every member by bare id. Those collections were built
        in the shared economy, so that is the scope they belong to now.
        """
        users = self._data.get("users", {})
        if not isinstance(users, dict):
            self._data["users"] = {}
            return
        moved = 0
        for raw_key in list(users):
            if ":" in str(raw_key):
                continue
            if not str(raw_key).lstrip("-").isdigit():
                continue
            record = users.pop(raw_key)
            users.setdefault(f"0:{raw_key}", record)
            moved += 1
        if moved:
            logger.info("[Vault] Moved %d collections onto the shared economy scope.", moved)
            self._save_locked()

    def _migrate_markets(self) -> None:
        """Bring a market block written by an older model onto the new shape.

        Markets used to store a ``supply`` and a ``index``. They now store the
        ``size`` every price is a share of, so the size is seeded from whatever
        supply was last seen -- the same order of magnitude, and the next
        refresh replaces it with members' money. The old anchor measured a
        different quantity in different units, so it is dropped rather than
        compared against: the reported change starts again from the first price
        of the new model instead of reading as a crash.
        """
        rebased = 0
        for market in (self._data.get("markets") or {}).values():
            if not isinstance(market, dict):
                continue
            if market.get("anchor_supply") is not None or market.get("index") is not None:
                market["anchor_size"] = 0.0
                market["anchor_at"] = None
                market.pop("anchor_supply", None)
                market.pop("index", None)
                rebased += 1
            if market.get("size") is None:
                try:
                    market["size"] = float(market.get("supply") or 0)
                except (TypeError, ValueError):
                    market["size"] = 0.0
                rebased += 1
        if rebased:
            self._save_locked()

    def _migrate_pricing(self) -> None:
        """Re-price the frozen catalog when the price model version changes."""
        try:
            current = int(self._data.get("pricing_version") or 0)
        except (TypeError, ValueError):
            current = 0
        if current == PRICING_VERSION:
            return

        changed = 0
        for entry in self._data.get("catalog", {}).values():
            if not isinstance(entry, dict):
                continue
            # Fold any invented category onto one of the four real ones as part
            # of the move, so a stale "leaked" or "preview" row is corrected.
            entry["category"] = normalize_category(entry.get("category"))
            # A frozen *price* becomes a frozen *share*, derived from the stored
            # year and category. ``roll=1.0`` keeps the move deterministic; the
            # coin price is recomputed from the scope's supply on every read, so
            # the stored absolute below is only a "when it was priced" note and
            # ``freeze_price`` re-seeds it the first time a scope touches the
            # row under the new model.
            share = economy_share(
                year=entry.get("year"),
                category=entry["category"],
                kind=entry.get("kind") or "song",
                roll=1.0,
            )
            if entry.get("share") != share:
                entry["share"] = share
                entry["repriced_at"] = _now()
                changed += 1

        # Owned snapshots carry a category too, and those are what the cards and
        # the collection list print.
        for user in self._data.get("users", {}).values():
            items = user.get("items") if isinstance(user, dict) else None
            if not isinstance(items, dict):
                continue
            for record in items.values():
                item = record.get("item") if isinstance(record, dict) else None
                if isinstance(item, dict):
                    item["category"] = normalize_category(item.get("category"))

        self._data["pricing_version"] = PRICING_VERSION
        self._save_locked()
        if changed:
            logger.info(
                "[Vault] Repriced %d catalog entries for pricing model v%d.",
                changed,
                PRICING_VERSION,
            )

    def _save_locked(self) -> None:
        tmp = VAULT_DATA_PATH.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, indent=2)
            os.replace(tmp, VAULT_DATA_PATH)
        except Exception:
            logger.exception("[Vault] Could not save %s", VAULT_DATA_PATH)

    # -- catalog -----------------------------------------------------

    def get_price(self, key: str) -> Optional[int]:
        """The stored absolute price of a catalogued item, if it is known.

        This is deliberately the figure recorded when the row was first priced
        in a scope, not the live price: the market moves on its own, so a stored
        row stays readable as "what this cost when it was priced". Everything
        that shows or charges a price goes through ``live_price``/``price_of``.
        """
        entry = self._data.get("catalog", {}).get(key)
        if isinstance(entry, dict):
            base = entry.get("base_price")
            if base is None:
                base = entry.get("price")
            if base is not None:
                return int(base)
        return None

    def get_share(self, key: str) -> Optional[float]:
        """The frozen share of the economy a catalogued item is worth."""
        entry = self._data.get("catalog", {}).get(key)
        if not isinstance(entry, dict):
            return None
        share = entry.get("share")
        if share is None:
            # A row written before shares existed: derive one from its year and
            # category so the item still prices.
            share = economy_share(
                year=entry.get("year"),
                category=normalize_category(entry.get("category")),
                kind=entry.get("kind") or "song",
                roll=1.0,
            )
        try:
            return float(share)
        except (TypeError, ValueError):
            return None

    def get_catalog_entry(self, key: str) -> Optional[dict]:
        entry = self._data.get("catalog", {}).get(key)
        return dict(entry) if isinstance(entry, dict) else None

    def find_catalog_by_title(self, query: str) -> Optional[dict]:
        wanted = query.strip().casefold()
        if not wanted:
            return None
        for entry in self._data.get("catalog", {}).values():
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("title") or "").casefold()
            if title == wanted or entry.get("key", "").casefold() == wanted:
                return dict(entry)
        return None

    async def freeze_price(self, item: dict) -> dict:
        """Freeze an item's *share* of the economy the first time it is seen.

        The share is what is recorded once and never rewritten; the coin price
        is that share applied to the scope's current supply, which is what lets
        a collection gain and lose value as the economy inflates and deflates.
        ``item`` comes back carrying the share plus today's price.
        """
        async with self._lock:
            catalog = self._data.setdefault("catalog", {})
            entry = catalog.get(item["key"])
            if not isinstance(entry, dict):
                entry = {
                    **_snapshot(item),
                    "share": self._share_for(item),
                    "pricing_version": PRICING_VERSION,
                    "frozen_at": _now(),
                }
                catalog[item["key"]] = entry
                entry["base_price"] = price_for_share(entry["share"], self.market_size())
                entry["price"] = entry["base_price"]
                self._save_locked()

            share = entry.get("share")
            if share is None:
                share = self._share_for(item)
                entry["share"] = share
            price = price_for_share(share, self.market_size())
            # A row priced under an older model has no figure worth comparing
            # against in this scope's market; re-seed it once so the card's
            # "since it was priced" marker stays honest.
            if entry.get("pricing_version") != PRICING_VERSION:
                entry["pricing_version"] = PRICING_VERSION
                entry["base_price"] = price
                self._save_locked()
            entry["price"] = price

        item["share"] = share
        item["base_price"] = int(entry.get("base_price") or price)
        item["price"] = price
        return entry

    @staticmethod
    def _share_for(item: dict) -> float:
        """This item's share of the economy, drawn once when it is first priced."""
        share = item.get("share")
        if share is not None:
            try:
                return float(share)
            except (TypeError, ValueError):
                pass
        return economy_share(
            year=item.get("year"),
            category=normalize_category(item.get("category")),
            kind=item.get("kind") or "song",
        )

    # -- collections (per economy scope) ------------------------------

    def _user(self, user_id: int, scope: int | None = None) -> dict:
        """This member's record in the active economy scope, created on demand."""
        users = self._data.setdefault("users", {})
        key = scoped_user_key(user_id, scope)
        user = users.get(key)
        if not isinstance(user, dict):
            user = _empty_user()
            users[key] = user
        user.setdefault("items", {})
        user.setdefault("stats", {})
        user.setdefault("achievements", {})
        return user

    def get_user_items(self, user_id: int, scope: int | None = None) -> dict[str, dict]:
        user = self._data.get("users", {}).get(scoped_user_key(user_id, scope))
        if not isinstance(user, dict):
            return {}
        items = user.get("items")
        return items if isinstance(items, dict) else {}

    def get_user_item(
        self, user_id: int, key: str, scope: int | None = None
    ) -> Optional[dict]:
        record = self.get_user_items(user_id, scope).get(key)
        return dict(record) if isinstance(record, dict) else None

    def user_owns(self, user_id: int, key: str, scope: int | None = None) -> bool:
        return key in self.get_user_items(user_id, scope)

    def owner_of(self, key: str) -> Optional[int]:
        """The id of the first member in *this scope* who owns ``key``.

        An item can end up with several owners through sharing, so buying is
        blocked as soon as anyone in the scope owns it rather than only the
        buyer. Ownership is per economy scope: the same song can be held by one
        member per server without the two collections colliding.
        """
        prefix = f"{current_scope()}:"
        for raw_key, user in self._data.get("users", {}).items():
            if not str(raw_key).startswith(prefix):
                continue
            if not isinstance(user, dict):
                continue
            items = user.get("items")
            if isinstance(items, dict) and key in items:
                raw_id = str(raw_key)[len(prefix):]
                try:
                    return int(raw_id)
                except (TypeError, ValueError):
                    continue
        return None

    def owned_list(self, user_id: int, scope: int | None = None) -> list[tuple[str, dict]]:
        return list(self.get_user_items(user_id, scope).items())

    async def add_item(self, user_id: int, item: dict, price: int) -> None:
        async with self._lock:
            user = self._user(user_id)
            items = user.setdefault("items", {})
            items[item["key"]] = {
                "item": _snapshot(item),
                "price_paid": int(price),
                "acquired_at": _now(),
                "leaked": False,
                "value_multiplier": 1.0,
            }
            user.setdefault("stats", {})["bought"] = (
                int(user["stats"].get("bought") or 0) + 1
            )
            self._save_locked()

    async def remove_item(self, user_id: int, key: str) -> Optional[dict]:
        async with self._lock:
            user = self._data.get("users", {}).get(scoped_user_key(user_id))
            if not isinstance(user, dict):
                return None
            items = user.get("items")
            if not isinstance(items, dict) or key not in items:
                return None
            record = items.pop(key)
            self._save_locked()
            return dict(record)

    async def mark_leaked(self, user_id: int, key: str) -> bool:
        async with self._lock:
            user = self._data.get("users", {}).get(scoped_user_key(user_id))
            if not isinstance(user, dict):
                return False
            items = user.get("items")
            if not isinstance(items, dict) or key not in items:
                return False
            items[key]["leaked"] = True
            self._save_locked()
            return True

    async def apply_value_drop(self, user_id: int, key: str, drop: float) -> bool:
        """Permanently shave ``drop`` (a 0-1 fraction) off an owned item."""
        async with self._lock:
            user = self._data.get("users", {}).get(scoped_user_key(user_id))
            if not isinstance(user, dict):
                return False
            items = user.get("items")
            if not isinstance(items, dict) or key not in items:
                return False
            record = items[key]
            record["value_multiplier"] = max(
                0.0, self.value_multiplier(record) * (1.0 - drop)
            )
            self._save_locked()
            return True

    # -- market: inflation and deflation ------------------------------

    def market(self, scope: int | None = None, *, create: bool = True) -> dict:
        """This scope's market record.

        ``create=False`` is for reads: merely asking for a price must not make a
        scope look like one the bot has priced, or ``market_scopes`` (which the
        background loop walks) would fill up with scopes nobody traded in.
        """
        scope = current_scope() if scope is None else int(scope)
        markets = self._data.setdefault("markets", {})
        entry = markets.get(str(scope))
        if isinstance(entry, dict):
            return entry
        if not create:
            return {}
        entry = {}
        markets[str(scope)] = entry
        return entry

    def market_size(self, scope: int | None = None) -> float:
        """The money this scope's market is priced against.

        Every price is a frozen share multiplied by this, so the number *is* the
        market: circulating supply when members hold something, total supply as
        a fallback for a scope whose treasury holds everything, and a small
        constant only until the first command prices the scope.
        """
        entry = self.market(scope, create=False)
        for field in ("size", "supply"):
            try:
                size = float(entry.get(field) or 0)
            except (TypeError, ValueError):
                size = 0.0
            if size > 0:
                return size
        return float(FALLBACK_ECONOMY_SIZE)

    def market_change(self, scope: int | None = None) -> float:
        """How far this scope's market has moved since it was first priced.

        ``1.0`` is flat and ``1.25`` means the economy is a quarter larger, so
        every price is a quarter higher. This is a *reporting* figure (cards and
        ``,vault market``); prices themselves follow the live size directly.
        """
        entry = self.market(scope, create=False)
        try:
            anchor = float(entry.get("anchor_size") or 0)
        except (TypeError, ValueError):
            anchor = 0.0
        size = self.market_size(scope)
        if anchor <= 0 or size <= 0:
            return 1.0
        return size / anchor

    def market_scopes(self) -> list[int]:
        scopes: list[int] = []
        for raw in self._data.get("markets", {}):
            try:
                scopes.append(int(raw))
            except (TypeError, ValueError):
                continue
        return scopes

    def market_is_fresh(self, scope: int | None = None) -> bool:
        entry = self.market(scope, create=False)
        try:
            refreshed = float(entry.get("refreshed_at") or 0)
        except (TypeError, ValueError):
            refreshed = 0.0
        return (time.time() - refreshed) < MARKET_CACHE_SECONDS

    def market_anchor(self, scope: int | None, size: float) -> float:
        """The size this scope's market is reported against.

        Seeded the first time the market is priced, then eased toward the live
        size once it is ``MARKET_ANCHOR_SECONDS`` old, so the reported change
        reflects recent inflation instead of ratcheting in one direction
        forever.
        """
        entry = self.market(scope)
        now = time.time()
        try:
            anchor = float(entry.get("anchor_size") or 0)
        except (TypeError, ValueError):
            anchor = 0.0
        try:
            seeded = float(entry.get("anchor_at") or 0)
        except (TypeError, ValueError):
            seeded = 0.0

        if anchor <= 0 or size <= 0:
            entry["anchor_size"] = float(size or 0)
            entry["anchor_at"] = now
            return float(entry["anchor_size"])

        if now - seeded > MARKET_ANCHOR_SECONDS:
            anchor = anchor + (size - anchor) * MARKET_ANCHOR_DRIFT
            entry["anchor_size"] = anchor
            entry["anchor_at"] = now
        return anchor

    def set_market(
        self,
        scope: int | None,
        size: float,
        *,
        supply: float = 0.0,
        circulating: float = 0.0,
        treasury: float = 0.0,
    ) -> None:
        entry = self.market(scope)
        entry["size"] = max(0.0, float(size))
        entry["supply"] = max(0.0, float(supply or size))
        entry["circulating"] = max(0.0, float(circulating))
        entry["treasury"] = max(0.0, float(treasury))
        entry["refreshed_at"] = time.time()
        self._save_locked()

    def live_price(self, key: str, *, fallback: int = 0) -> int:
        """The current live price of a catalogued item."""
        share = self.get_share(key)
        if share is None:
            return int(fallback or 0)
        return price_for_share(share, self.market_size())

    def og_share(self, item: dict) -> Optional[float]:
        """An OG file's share: half its song's, plus a fifth once leaked."""
        song_key = item.get("og_of")
        share = self.get_share(song_key) if song_key else None
        if share is None:
            share = item.get("share")
        if not share:
            return None
        bonus = 1.0 + (OG_FILE_LEAK_BONUS if self.song_has_leaked(song_key) else 0.0)
        return float(share) * OG_FILE_RATE * bonus

    def price_of(self, item: dict) -> int:
        """The live price of an item that is being bought right now.

        A normal song prices from its frozen catalog share; an OG file tracks
        its song's live share instead, which is why it carries its own.
        """
        if item.get("kind") == "ogfile":
            share = self.og_share(item)
            if share:
                return price_for_share(share, self.market_size())
            return int(item.get("base_price") or item.get("price") or 0)
        share = self.get_share(item.get("key"))
        if share is None:
            share = item.get("share")
        if share is None:
            return int(item.get("base_price") or item.get("price") or 0)
        return price_for_share(share, self.market_size())

    # -- achievements -------------------------------------------------

    def vault_stats(self, user_id: int, scope: int | None = None) -> dict:
        """Everything an achievement rule can key off, in one read."""
        user = self._data.get("users", {}).get(scoped_user_key(user_id, scope)) or {}
        items = user.get("items") if isinstance(user.get("items"), dict) else {}
        counters = user.get("stats") if isinstance(user.get("stats"), dict) else {}
        owned = [
            record.get("item") or {}
            for record in items.values()
            if isinstance(record, dict)
        ]
        portfolio = self.portfolio_value(user_id, scope)
        return {
            "items": len(owned),
            "sessions": sum(1 for item in owned if is_session_item(item)),
            "unsurfaced": sum(
                1
                for item in owned
                if normalize_category(item.get("category")) == "unsurfaced"
            ),
            "og_files": sum(1 for item in owned if item.get("kind") == "ogfile"),
            "sold": int(counters.get("sold") or 0),
            "leaked": int(counters.get("leaked") or 0),
            "bought": int(counters.get("bought") or 0),
            "portfolio": portfolio,
            # 1 once the collection is worth its slice of the market (see
            # TYCOON_MARKET_SHARE); a plain counter so the rule table above
            # stays uniform.
            "tycoon": int(portfolio >= self.market_size(scope) * TYCOON_MARKET_SHARE),
        }

    @staticmethod
    def earned_achievements(stats: dict) -> list[str]:
        """Achievement keys a stats snapshot already satisfies."""
        return [
            key
            for key, (field, target) in VAULT_ACHIEVEMENT_RULES.items()
            if key in VAULT_ACHIEVEMENTS and int(stats.get(field) or 0) >= target
        ]

    def unlocked_achievements(
        self, user_id: int, scope: int | None = None
    ) -> dict[str, str]:
        user = self._data.get("users", {}).get(scoped_user_key(user_id, scope)) or {}
        unlocked = user.get("achievements") if isinstance(user, dict) else None
        return unlocked if isinstance(unlocked, dict) else {}

    async def record_stat(self, user_id: int, name: str, amount: int = 1) -> None:
        """Bump a lifetime counter (items sold, items leaked, ...)."""
        async with self._lock:
            stats = self._user(user_id).setdefault("stats", {})
            stats[name] = int(stats.get(name) or 0) + int(amount)
            self._save_locked()

    async def mark_achievement(self, user_id: int, key: str) -> None:
        async with self._lock:
            unlocked = self._user(user_id).setdefault("achievements", {})
            unlocked[key] = _now()
            self._save_locked()

    # -- song-wide leak state (drives the OG file bonus) -------------

    def song_has_leaked(self, song_key: Optional[str]) -> bool:
        return bool(song_key and self._data.get("leaked_songs", {}).get(song_key))

    async def record_song_leak(self, song_key: str) -> None:
        async with self._lock:
            leaked = self._data.setdefault("leaked_songs", {})
            if not leaked.get(song_key):
                leaked[song_key] = True
                self._save_locked()

    # -- valuation / trading -----------------------------------------

    @staticmethod
    def value_multiplier(record: dict) -> float:
        try:
            return float(record.get("value_multiplier") or 1.0)
        except (TypeError, ValueError):
            return 1.0

    def base_item_value(self, key: str, record: dict) -> int:
        """Live value before the owned-item multiplier, in today's coins.

        An item's worth is its frozen share of the market, so this already
        follows the economy; OG files track their song's share instead.
        """
        item = record.get("item") if isinstance(record.get("item"), dict) else record
        size = self.market_size()
        if item.get("kind") == "ogfile":
            og_share = self.og_share(item)
            if og_share:
                return price_for_share(og_share, size)

        share = self.get_share(key)
        if share is None:
            share = item.get("share")
        if share is not None:
            try:
                return price_for_share(float(share), size)
            except (TypeError, ValueError):
                pass
        return int(record.get("price_paid") or 0)

    def item_value(self, key: str, record: dict) -> int:
        """The live value of an owned item, in today's coins.

        Prices are a share of the scope's money supply, so this moves with the
        economy on its own; only the owned-item multiplier (sharing and leaking)
        is applied on top. The collection list, the leaderboard, selling and the
        groupbuy payout all read this.
        """
        base = self.base_item_value(key, record) * self.value_multiplier(record)
        return max(0, int(round(base)))

    def portfolio_value(self, user_id: int, scope: int | None = None) -> int:
        return sum(
            self.item_value(key, record)
            for key, record in self.get_user_items(user_id, scope).items()
        )

    def leaderboard(self, limit: int = 10) -> list[tuple[int, int, int]]:
        """``(user_id, total_value, item_count)`` for this scope, best first."""
        prefix = f"{current_scope()}:"
        scores: list[tuple[int, int, int]] = []
        for raw_key, user in self._data.get("users", {}).items():
            if not str(raw_key).startswith(prefix) or not isinstance(user, dict):
                continue
            items = user.get("items")
            if not isinstance(items, dict) or not items:
                continue
            try:
                member_id = int(str(raw_key)[len(prefix):])
            except (TypeError, ValueError):
                continue
            total = sum(self.item_value(key, record) for key, record in items.items())
            scores.append((member_id, total, len(items)))
        scores.sort(key=lambda row: row[1], reverse=True)
        return scores[:limit]

    async def transfer_item(
        self,
        from_id: int,
        to_id: int,
        key: str,
        *,
        price_paid: Optional[int] = None,
    ) -> bool:
        """Move an owned item between two users in a single write."""
        async with self._lock:
            source = self._user(from_id)
            source_items = source.get("items")
            if not isinstance(source_items, dict) or key not in source_items:
                return False

            record = source_items.pop(key)
            if price_paid is not None:
                record["price_paid"] = int(price_paid)

            target = self._user(to_id)
            target.setdefault("items", {})[key] = record
            self._save_locked()
            return True

    # -- auctions ----------------------------------------------------

    def list_auctions(self, *, guild_id: Optional[int] = None) -> list[dict]:
        """Open auctions, oldest first. ``guild_id`` narrows to one server."""
        auctions: list[dict] = []
        for auction in self._data.get("auctions", {}).values():
            if not isinstance(auction, dict):
                continue
            if guild_id is not None and int(auction.get("guild_id") or 0) != int(guild_id):
                continue
            auctions.append(dict(auction))
        auctions.sort(key=lambda row: float(row.get("created_at") or 0))
        return auctions

    def get_auction(self, auction_id: str) -> Optional[dict]:
        auction = self._data.get("auctions", {}).get(auction_id)
        return dict(auction) if isinstance(auction, dict) else None

    def latest_auction(self, channel_id: int) -> Optional[dict]:
        """The most recently started open auction in a channel, if any."""
        candidates = [
            auction
            for auction in self.list_auctions()
            if int(auction.get("channel_id") or 0) == int(channel_id)
        ]
        return candidates[-1] if candidates else None

    async def save_auction(self, auction: dict) -> None:
        async with self._lock:
            self._data.setdefault("auctions", {})[auction["id"]] = auction
            self._save_locked()

    async def delete_auction(self, auction_id: str) -> Optional[dict]:
        async with self._lock:
            return self._data.setdefault("auctions", {}).pop(auction_id, None)


# ---------------------------------------------------------------------------
# API (songs via juicewrldapi.com, sessions via the files browser)
# ---------------------------------------------------------------------------

class VaultAPI:
    def __init__(self) -> None:
        self.session: Optional[aiohttp.ClientSession] = None
        self._storage_files: list[dict[str, str]] = []
        self._storage_timestamp: float = 0.0
        self._storage_lock = asyncio.Lock()
        self._session_files: list[dict[str, Any]] = []
        self._session_timestamp: float = 0.0
        self._session_lock = asyncio.Lock()

    async def start(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
            self.session = aiohttp.ClientSession(timeout=timeout, headers=HEADERS)
        return self.session

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def request(self, endpoint: str, params: Optional[dict] = None) -> Any:
        session = await self.start()
        url = f"{API_BASE}/{endpoint.lstrip('/')}"
        try:
            async with session.get(url, params=params) as response:
                if response.status != 200:
                    return None
                text = await response.text()
                return parse_api_json(text)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None
        except Exception:
            return None

    async def browse_directory(self, path: str) -> list[dict]:
        data = await self.request(
            "files/browse/",
            params={"path": path, "channel": "comp", "page_size": 2000},
        )
        if isinstance(data, dict):
            return data.get("items", [])
        return []

    # -- songs -------------------------------------------------------

    async def search_songs(self, query: str) -> list[dict]:
        data = await self.request(
            "songs/",
            params={
                "search": query,
                "page": 1,
                "page_size": MAX_RESULTS,
                "limit": MAX_RESULTS,
            },
        )
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("results", "songs", "data", "items"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
        return []

    async def get_song(self, song_id: Any) -> Any:
        for endpoint in (f"songs/{song_id}/", f"songs/{song_id}"):
            data = await self.request(endpoint)
            if data:
                return data
        return None

    # -- storage index (for resolving a song to a downloadable file) --

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
                self._storage_timestamp = now

    async def resolve_storage_path(self, query: str, era_hint: Optional[str] = None) -> Optional[str]:
        await self._ensure_storage_index()
        if not self._storage_files:
            return None

        clean_target = clean_match_key(query)
        if not clean_target:
            return None

        exact_match: Optional[str] = None
        best_match: Optional[str] = None
        best_score = 0.0

        for item in self._storage_files:
            path = item.get("path", "")
            name = item.get("name", "")
            base = name.rsplit(".", 1)[0] if "." in name else name
            clean_item = clean_match_key(base)

            if clean_item == clean_target:
                if era_hint and era_hint.lower() in path.lower():
                    return path
                if not exact_match:
                    exact_match = path

            score = difflib.SequenceMatcher(None, clean_target, clean_item).ratio()
            if era_hint and era_hint.lower() in path.lower():
                score += 0.15

            if score > best_score and score >= 0.70:
                best_score = score
                best_match = path

        return exact_match or best_match

    async def _resolve_one_file(self, item: Any, era_hint: Optional[str] = None) -> Optional[dict]:
        path = file_path_from_item(item)
        direct = direct_file_url(item)

        if isinstance(item, str):
            name = item.split("/")[-1]
        else:
            name = display(
                get_value(item, "filename", "file_name", "fileName", "name", "title", default="Song"),
                "Song",
            )

        resolved_path: Optional[str] = None
        if path:
            if "/" in path and ("Original Files" in path or "Tagged" in path):
                resolved_path = path
            else:
                resolved_path = await self.resolve_storage_path(path, era_hint)
                if not resolved_path:
                    resolved_path = path

        if not resolved_path and name:
            resolved_path = await self.resolve_storage_path(name, era_hint)

        final_path = resolved_path or path
        download = direct or (make_download_url(final_path) if final_path else None)
        if not download:
            return None

        display_name = (final_path.split("/")[-1] if final_path and "/" in final_path else name)[:80]
        return {"name": display_name, "download": download, "path": final_path}

    async def primary_file_for_song(self, song: Any) -> Optional[dict]:
        if is_unsurfaced(song):
            return None

        era_hint = clean_era_display(first_value(song, "era", "era_name", "eraName"))

        raw_items = recursive_file_objects(song)
        raw_items.extend(filename_list(song))

        seen: set[str] = set()
        for item in raw_items:
            identity = (file_path_from_item(item) or direct_file_url(item) or str(item)).casefold()
            if identity in seen:
                continue
            seen.add(identity)
            resolved = await self._resolve_one_file(item, era_hint)
            if resolved and is_audio_path(resolved.get("path") or resolved.get("name")):
                return resolved

        fallback_path = await self.resolve_storage_path(get_song_title(song), era_hint)
        if fallback_path:
            return {
                "name": fallback_path.split("/")[-1][:80],
                "download": make_download_url(fallback_path),
                "path": fallback_path,
            }
        return None

    # -- studio sessions --------------------------------------------

    async def _collect_session_dir(
        self,
        path: str,
        kind: str,
        era: Optional[str],
        depth: int,
        out: list[dict[str, Any]],
    ) -> None:
        if depth > 3:
            return
        items = await self.browse_directory(path)
        for item in items:
            item_path = item.get("path")
            if not item_path:
                continue
            name = item.get("name", "")
            if item.get("type") == "directory":
                await self._collect_session_dir(
                    item_path, kind, era or name, depth + 1, out
                )
            elif not is_image_path(item_path):
                out.append(
                    {
                        "name": name,
                        "path": item_path,
                        "kind": kind,
                        "era": era,
                        "size_human": item.get("size_human", ""),
                    }
                )

    async def _ensure_session_index(self) -> None:
        now = asyncio.get_running_loop().time()
        if self._session_files and (now - self._session_timestamp < 1800):
            return

        async with self._session_lock:
            if self._session_files and (now - self._session_timestamp < 1800):
                return

            collected: list[dict[str, Any]] = []
            for folder, kind in SESSION_SOURCES:
                await self._collect_session_dir(folder, kind, None, 0, collected)

            if collected:
                self._session_files = collected
                self._session_timestamp = now
                logger.info("[Vault] Indexed %d studio session files.", len(collected))

    async def search_sessions(self, query: str) -> list[dict[str, Any]]:
        await self._ensure_session_index()
        target = clean_session_key(query)
        if not target:
            return []

        matches: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in self._session_files:
            path = item.get("path", "")
            if path in seen:
                continue
            if _name_matches_song(item.get("name", ""), {target}):
                seen.add(path)
                matches.append(item)
        return matches[:MAX_RESULTS]


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def build_card_body(item: dict, song: Any = None) -> Optional[str]:
    fields: list[str] = []

    kind = {"session": "Session", "ogfile": "OG File"}.get(item.get("kind"), "Song")
    fields.append(f"**Type**\n{kind}")

    if item.get("category"):
        fields.append(
            f"**Category**\n"
            f"{category_emoji(item['category'])} {category_label(item['category'])}"
        )

    if item.get("kind") == "ogfile" and item.get("song_price"):
        fields.append(f"**Song Value**\n{format_cash(int(item['song_price']))}")

    if item.get("year"):
        fields.append(f"**Recorded**\n{item['year']}")

    if item.get("era"):
        fields.append(f"**Era**\n{item['era']}")

    if song is not None:
        recorded = get_recorded_text(song)
        if recorded and recorded != "N/A":
            fields.append(f"**Record Date(s)**\n{clean_meta_text(recorded)}")

        previewed = song_text(
            song,
            "preview_date", "previewDate",
            "first_previewed", "firstPreviewed",
            "first_preview_date", "firstPreviewDate",
        )
        if previewed and previewed != "N/A":
            fields.append(f"**First Previewed**\n{clean_meta_text(previewed)}")

        surfaced = get_surfaced(song)
        if surfaced and surfaced != "N/A":
            fields.append(f"**Surfaced**\n{clean_meta_text(surfaced)}")

        length = get_length(song)
        if length and length != "N/A":
            fields.append(f"**Length**\n{length}")

        producers = song_list(song, "producers", "producer", "producer_names", "producerNames")
        if producers and producers != "N/A":
            fields.append(f"**Producer(s)**\n{producers}")

        engineers = song_list(song, "engineers", "engineer", "engineer_names", "engineerNames")
        if engineers and engineers != "N/A":
            fields.append(f"**Engineer(s)**\n{engineers}")

        additional = song_text(
            song,
            "additional_information", "additionalInformation",
            "additional_info", "additionalInfo", "info", "notes",
        )
        if additional and additional != "N/A":
            fields.append(f"-# {truncate(additional, MAX_NOTES_LENGTH)}")

    return "\n\n".join(fields) if fields else None


class VaultSearchSelect(discord.ui.Select):
    """Dropdown used when a search returns several candidates."""

    def __init__(
        self,
        candidates: list[dict[str, Any]],
        author_id: int,
        cog: "Vault",
        *,
        source: str,
    ) -> None:
        self.candidates = candidates
        self.author_id = author_id
        self.cog = cog
        self.source = source

        options: list[discord.SelectOption] = []
        for index, result in enumerate(candidates[:25]):
            if source in ("song", "ogfile"):
                label = get_song_title(result)
                era = clean_era_display(first_value(result, "era", "era_name", "eraName"))
                description = f"Era: {era}"[:100] if era else None
            else:
                label = str(result.get("name", "Session")).rsplit(".", 1)[0]
                kind = result.get("kind", "Session")
                description = str(kind)[:100]
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(index),
                    description=description,
                )
            )

        super().__init__(
            placeholder="Choose the correct result...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Only the person who ran the command can choose.", ephemeral=True
            )
            return

        if self.view:
            self.view.selected = True  # type: ignore[attr-defined]
            self.view.stop()

        await interaction.response.defer()
        chosen = self.candidates[int(self.values[0])]
        try:
            if self.source == "song":
                item, song = await self.cog.prepare_song_item(chosen)
            elif self.source == "ogfile":
                item, song = await self.cog.prepare_ogfile_item(chosen)
            else:
                item, song = await self.cog.prepare_session_item(chosen)
            view = self.cog.build_card_view(
                item, song=song, owned=False, buyer_id=self.author_id, show_buy=True
            )
            await interaction.edit_original_response(view=view)
        except discord.HTTPException:
            logger.exception("[Vault] Discord rejected vault card after selection")
            await self.cog.edit_with_error(interaction, "❌ Discord rejected the vault card.")
        except Exception:
            logger.exception("[Vault] Failed to build vault card after selection")
            await self.cog.edit_with_error(interaction, "❌ Failed to build the vault card.")


class VaultSearchSelectView(discord.ui.LayoutView):
    def __init__(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        author_id: int,
        cog: "Vault",
        *,
        source: str,
    ) -> None:
        super().__init__(timeout=SELECT_TIMEOUT)
        self.selected = False
        self.message: Optional[discord.Message] = None

        container = discord.ui.Container(accent_color=EMBED_COLOR)
        container.add_item(
            text_display(
                f"# Command: vault search\n"
                f"Found **{len(candidates)}** matching results for **{query}**.\n"
                f"Select the one you want to price:"
            )
        )
        container.add_item(
            discord.ui.ActionRow(VaultSearchSelect(candidates, author_id, cog, source=source))
        )
        self.add_item(container)

    async def on_timeout(self) -> None:
        if self.selected or not self.message:
            return
        try:
            await self.message.edit(
                view=simple_view("⏱️ Selection timed out. Please run the command again.", timeout=10)
            )
        except Exception:
            pass


class VaultItemSelect(discord.ui.Select):
    """Dropdown used to pick an owned item for an action."""

    def __init__(
        self,
        items: list[tuple[str, dict]],
        author_id: int,
        cog: "Vault",
        action: str,
        target: Optional[discord.abc.User] = None,
    ) -> None:
        self.items = items
        self.author_id = author_id
        self.cog = cog
        self.action = action
        self.target = target

        options: list[discord.SelectOption] = []
        for index, (key, record) in enumerate(items[:25]):
            data = record.get("item", {})
            title = item_display_name(data)
            price = cog.item_value(key, record)
            description = f"{format_cash_short(price)}"
            if record.get("leaked"):
                description += " · LEAKED"
            options.append(
                discord.SelectOption(
                    label=title[:100],
                    value=str(index),
                    description=description[:100],
                )
            )

        verbs = {"sell": "sell", "leak": "leak", "share": "share"}
        super().__init__(
            placeholder=f"Choose an item to {verbs.get(action, action)}...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Only the person who ran the command can choose.", ephemeral=True
            )
            return

        if self.view:
            self.view.selected = True  # type: ignore[attr-defined]
            self.view.stop()

        await interaction.response.defer()
        key, _record = self.items[int(self.values[0])]
        try:
            await self.cog.run_action(interaction, self.action, key, target=self.target)
        except discord.HTTPException:
            logger.exception("[Vault] Discord error during action %s", self.action)
            await self.cog.edit_with_error(interaction, "❌ Discord rejected the request.")
        except Exception:
            logger.exception("[Vault] Failed during action %s", self.action)
            await self.cog.edit_with_error(interaction, "❌ Failed to process the action.")


class VaultBuyButton(discord.ui.Button):
    def __init__(self, cog: "Vault", item: dict, buyer_id: int, song: Any = None) -> None:
        price = int(item.get("price") or 0)
        super().__init__(label=f"Buy · {format_cash_short(price)}", style=discord.ButtonStyle.success)
        self.cog = cog
        self.item = item
        self.buyer_id = buyer_id
        self.song = song

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.buyer_id:
            await interaction.response.send_message(
                "❌ Only the person who ran the command can buy this.", ephemeral=True
            )
            return

        await interaction.response.defer()
        ok, message, item = await self.cog.process_buy(interaction.user.id, self.item)
        if ok:
            view = self.cog.build_card_view(
                item, song=self.song, owned=True, buyer_id=self.buyer_id
            )
            await interaction.edit_original_response(view=view)
        else:
            await interaction.edit_original_response(view=simple_view(message))


class VaultCardView(discord.ui.LayoutView):
    """The card shown by ,vault search (buyable) and ,vault list (owned)."""

    def __init__(
        self,
        item: dict,
        *,
        cog: "Vault",
        song: Any = None,
        owned: bool = False,
        shared: bool = False,
        buyer_id: Optional[int] = None,
        show_buy: bool = False,
        footer: Optional[str] = None,
        timeout: int = VIEW_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog

        # Unsurfaced material has no real file, even if an old catalog entry
        # froze a decoy path before this rule existed.
        if is_unsurfaced(item):
            item = {**item, "path": None, "download": None}

        container = discord.ui.Container(accent_color=EMBED_COLOR)

        title = item_display_name(item)
        price = int(item.get("price") or 0)
        # How the live market has moved this item away from its frozen base.
        movement = price_movement(int(item.get("base_price") or price), price)
        move_suffix = f" · `{movement}`" if movement else ""

        header_lines = [f"### **{title}**"]
        if item.get("leaked"):
            header_lines.append("**💧 LEAKED**")
        if shared:
            header_lines.append("**📤 Shared with you**")
            if price:
                header_lines.append(f"-# Value: {format_cash_short(price)}{move_suffix}")
        elif owned:
            paid = int(item.get("price_paid") or price)
            header_lines.append(f"**Owned · Value: {format_cash(price)}**{move_suffix}")
            if paid and paid != price:
                header_lines.append(f"-# Purchased for {format_cash_short(paid)}")
        else:
            header_lines.append(f"**💰 Vault Value: {format_cash(price)}**{move_suffix}")
        header_text = "\n".join(header_lines)

        cover = valid_http_url(item.get("cover"))
        if cover:
            container.add_item(
                discord.ui.Section(
                    text_display(header_text),
                    accessory=discord.ui.Thumbnail(media=cover),
                )
            )
        else:
            container.add_item(text_display(header_text))

        body = build_card_body(item, song if song is not None else None)
        if body:
            container.add_item(small_separator())
            container.add_item(text_display(body))

        if footer:
            container.add_item(small_separator())
            container.add_item(text_display(footer))

        rows: list[discord.ui.ActionRow] = []
        download = make_link_button("Download", valid_http_url(item.get("download")))
        if (owned or shared) and download:
            rows.append(discord.ui.ActionRow(download))
        elif not owned and not shared and show_buy and buyer_id is not None:
            rows.append(discord.ui.ActionRow(VaultBuyButton(self.cog, item, buyer_id, song)))

        if rows:
            container.add_item(small_separator())
            for row in rows:
                container.add_item(row)

        self.add_item(container)


class VaultListPaginationView(discord.ui.LayoutView):
    """Scrollable collection view used by ,vault list / inventory."""

    def __init__(
        self,
        items: list[tuple[str, dict]],
        *,
        user: discord.abc.User,
        author_id: int,
        prefix: str,
        total_value: int,
        song_count: int,
        session_count: int,
        leaked_count: int,
        value_fn: Optional[Callable[[str, dict], int]] = None,
        per_page: int = LIST_PER_PAGE,
    ) -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        self.items = items
        self.user = user
        self.author_id = author_id
        self.prefix = prefix
        self.total_value = total_value
        self.song_count = song_count
        self.session_count = session_count
        self.leaked_count = leaked_count
        self.per_page = per_page
        self.page = 1
        self.total_pages = max(1, math.ceil(len(items) / per_page))
        self.message: Optional[discord.Message] = None
        # The cog passes its own ``item_value`` so the first render is already
        # priced off today's market, exactly like the portfolio total. Without
        # it the view would quote the stored base instead of a live price.
        self._value_fn: Callable[[str, dict], int] = value_fn or (
            lambda key, record: stored_item_price(record)
        )

        self.container = discord.ui.Container(accent_color=EMBED_COLOR)
        self.header = text_display(self._header_text())
        self.body = text_display(self._body_text())

        self.container.add_item(self.header)
        self.container.add_item(small_separator())
        self.container.add_item(self.body)
        self.container.add_item(small_separator())

        self.row = discord.ui.ActionRow()
        self.previous = discord.ui.Button(label="Previous", style=discord.ButtonStyle.secondary)
        self.next = discord.ui.Button(label="Next", style=discord.ButtonStyle.secondary)
        self.previous.callback = self.previous_callback
        self.next.callback = self.next_callback
        self.row.add_item(self.previous)
        self.row.add_item(self.next)
        self.container.add_item(self.row)

        self.add_item(self.container)
        self.update_buttons()

    # -- rendering ---------------------------------------------------

    def _header_text(self) -> str:
        return (
            f"# 📦 {self.user.display_name}'s Vault\n"
            f"-# Page {self.page}/{self.total_pages} · {len(self.items)} items"
        )

    def _body_text(self) -> str:
        start = (self.page - 1) * self.per_page
        chunk = self.items[start:start + self.per_page]

        lines: list[str] = []
        for key, record in chunk:
            item = record.get("item", {})
            title = item_display_name(item)
            price = self.cog_value(key, record)
            leaked_mark = " 💧" if record.get("leaked") else ""
            icon = category_emoji(item.get("category"))
            lines.append(
                f"{icon} **{title}**{leaked_mark}\n"
                f"> `{category_label(item.get('category'))}` · "
                f"{item.get('year', '????')} · {format_cash_short(price)}"
            )

        footer_bits = [f"**Portfolio Value:** {format_cash(self.total_value)}"]
        footer_bits.append(
            f"-# Songs: {self.song_count} · Sessions: {self.session_count} · "
            f"Leaked: {self.leaked_count}"
        )
        if self.author_id == self.user.id:
            footer_bits.append(
                f"-# Leak: `{self.prefix}vault gb` · Share: `{self.prefix}vault share` · "
                f"OG: `{self.prefix}vault ogfile` · Sell: `{self.prefix}vault sell`"
            )
        else:
            footer_bits.append(f"-# View your own with `{self.prefix}vault list`.")

        if lines:
            return "\n".join(lines) + "\n\n" + "\n".join(footer_bits)
        return "\n".join(footer_bits)

    def cog_value(self, key: str, record: dict) -> int:
        return self._value_fn(key, record)

    # -- navigation --------------------------------------------------

    def update_buttons(self) -> None:
        self.previous.disabled = self.page <= 1
        self.next.disabled = self.page >= self.total_pages

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This menu isn't yours.", ephemeral=True
            )
            return False
        return True

    async def previous_callback(self, interaction: discord.Interaction) -> None:
        if self.page <= 1:
            return await interaction.response.defer()
        self.page -= 1
        await interaction.response.defer()
        await self.refresh(interaction)

    async def next_callback(self, interaction: discord.Interaction) -> None:
        if self.page >= self.total_pages:
            return await interaction.response.defer()
        self.page += 1
        await interaction.response.defer()
        await self.refresh(interaction)

    async def refresh(self, interaction: discord.Interaction) -> None:
        self.header.content = self._header_text()
        self.body.content = self._body_text()
        self.update_buttons()
        try:
            if interaction.message:
                await interaction.message.edit(view=self)
                self.message = interaction.message
        except discord.HTTPException:
            pass

    async def on_timeout(self) -> None:
        self.previous.disabled = True
        self.next.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass


class VaultOfferView(discord.ui.LayoutView):
    """A generic accept/decline offer addressed to a single member."""

    def __init__(
        self,
        cog: "Vault",
        offer: dict,
        *,
        headline: str,
        lines: list[str],
        timeout: int = OFFER_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.offer = offer
        self.resolved = False
        self.message: Optional[discord.Message] = None

        container = discord.ui.Container(accent_color=EMBED_COLOR)
        container.add_item(text_display("\n\n".join([headline, *lines])))
        container.add_item(small_separator())
        container.add_item(
            discord.ui.ActionRow(
                VaultOfferButton(self, accept=True),
                VaultOfferButton(self, accept=False),
            )
        )
        self.add_item(container)

    async def settle_offer(self) -> discord.ui.LayoutView:
        raise NotImplementedError

    async def on_timeout(self) -> None:
        if self.resolved or not self.message:
            return
        try:
            await self.message.edit(
                view=simple_view("⏱️ This offer expired.", timeout=10)
            )
        except Exception:
            pass


class VaultOfferButton(discord.ui.Button):
    def __init__(self, offer_view: VaultOfferView, *, accept: bool) -> None:
        super().__init__(
            label="Accept" if accept else "Decline",
            style=discord.ButtonStyle.success if accept else discord.ButtonStyle.danger,
        )
        self.offer_view = offer_view
        self.accept = accept

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.offer_view
        if interaction.user.id != view.offer["to_id"]:
            await interaction.response.send_message(
                "❌ This offer isn't for you.", ephemeral=True
            )
            return
        if view.resolved:
            await interaction.response.send_message(
                "❌ This offer has already been resolved.", ephemeral=True
            )
            return

        view.resolved = True
        if self.view:
            self.view.stop()
        await interaction.response.defer()

        if not self.accept:
            await interaction.edit_original_response(
                view=simple_view(f"❌ **{view.offer['to_name']}** declined.")
            )
            return

        try:
            result_view = await view.settle_offer()
        except discord.HTTPException:
            logger.exception("[Vault] Discord error while settling an offer")
            result_view = simple_view("❌ Discord rejected the offer.")
        except Exception:
            logger.exception("[Vault] Failed to settle an offer")
            result_view = simple_view("❌ Failed to process the offer.")
        await interaction.edit_original_response(view=result_view)


class VaultTradeView(VaultOfferView):
    """An interactive player-to-player trade offer."""

    def __init__(self, cog: "Vault", offer: dict, *, timeout: int = OFFER_TIMEOUT) -> None:
        lines = [f"**{offer['from_name']}** → **{offer['to_name']}**"]
        if offer.get("receive_key"):
            lines.append(f"**{offer['from_name']} gives**\n{offer['give_title']}")
            lines.append(f"**{offer['to_name']} gives**\n{offer['receive_title']}")
        else:
            price = int(offer.get("price") or 0)
            pay_text = format_cash(price) if price > 0 else "Nothing (gift)"
            lines.append(f"**{offer['from_name']} gives**\n{offer['give_title']}")
            lines.append(f"**{offer['to_name']} pays**\n{pay_text}")
        super().__init__(cog, offer, headline="# 🤝 Vault Trade", lines=lines, timeout=timeout)

    async def settle_offer(self) -> discord.ui.LayoutView:
        return simple_view(await self.cog.execute_trade(self.offer))


def auction_text(auction: dict) -> str:
    """The live auction card body."""
    last_activity = float(auction.get("last_activity") or auction.get("created_at") or 0)
    remaining = max(0, int(AUCTION_IDLE_SECONDS - (time.time() - last_activity)))
    minutes, seconds = divmod(remaining, 60)

    item = (auction.get("record") or {}).get("item") or {}
    current_bid = int(auction.get("current_bid") or 0)
    if current_bid:
        bid_line = (
            f"**Current bid:** {format_cash(current_bid)} "
            f"by **{auction.get('current_bidder_name')}**"
        )
    else:
        bid_line = "**Current bid:** none yet"

    return (
        f"# 🔨 Vault Auction\n"
        f"### {item_display_name(item)}\n"
        f"**Seller:** {auction.get('seller_name')}\n"
        f"**Starting price:** {format_cash(int(auction.get('start_price') or 0))}\n"
        f"{bid_line}\n"
        f"**Bids:** {int(auction.get('bids') or 0)}\n\n"
        f"⏳ Ends **{minutes}m {seconds}s** after the last bid. "
        f"No bids at all means the item returns to the seller."
    )


class AuctionView(discord.ui.LayoutView):
    """The persistent auction message: a card plus a Place Bid button."""

    def __init__(self, cog: "Vault", auction: dict) -> None:
        # No timeout: the background loop closes the auction, not Discord.
        super().__init__(timeout=None)
        self.cog = cog
        self.auction = auction

        container = discord.ui.Container(accent_color=EMBED_COLOR)
        container.add_item(text_display(auction_text(auction)))
        container.add_item(small_separator())
        container.add_item(discord.ui.ActionRow(AuctionBidButton(self)))
        self.add_item(container)


class AuctionBidButton(discord.ui.Button):
    def __init__(self, auction_view: AuctionView) -> None:
        super().__init__(label="Place Bid", style=discord.ButtonStyle.success, emoji="🔨")
        self.auction_view = auction_view

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            AuctionBidModal(self.auction_view.cog, self.auction_view.auction["id"])
        )


class AuctionBidModal(discord.ui.Modal, title="Place a Bid"):
    amount = discord.ui.TextInput(
        label="Bid amount",
        placeholder="e.g. 25000",
        max_length=20,
    )

    def __init__(self, cog: "Vault", auction_id: str) -> None:
        super().__init__()
        self.cog = cog
        self.auction_id = auction_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_auction_bid(interaction, self.auction_id, str(self.amount.value))


class VaultShareView(VaultOfferView):
    """An offer to send a file to a member, who must accept to receive it."""

    def __init__(self, cog: "Vault", offer: dict, *, timeout: int = OFFER_TIMEOUT) -> None:
        lines = [
            f"**{offer['from_name']}** wants to share a file with "
            f"{offer.get('to_mention') or offer['to_name']}",
            f"### {offer['give_title']}",
            "-# Accept to receive the file.",
        ]
        super().__init__(cog, offer, headline="# 📤 Vault Share", lines=lines, timeout=timeout)

    async def settle_offer(self) -> discord.ui.LayoutView:
        text, item = await self.cog.execute_share(self.offer)
        if item is None:
            return simple_view(text)
        return self.cog.build_card_view(
            item,
            owned=False,
            shared=True,
            buyer_id=self.offer["to_id"],
            footer=text,
        )


# ---------------------------------------------------------------------------
# Money: the server's own economy
# ---------------------------------------------------------------------------

class VaultBank:
    """The vault's only route to coins: the running server's economy.

    Every call goes through ``bot.database``, which resolves the *active economy
    scope* itself. A server on ``!guildeconomy on`` therefore spends and earns
    from its own wallet, bank and treasury, and the descriptions below show up
    in that server's ``,balance`` history like any other transaction.
    """

    #: Transaction types the economy treats as a plain reward (no fee).
    REWARD = "reward"

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def db(self) -> Any:
        return self.bot.database

    async def wallet_id(self, user_id: int) -> str:
        return await self.db.get_wallet_id_for_user(int(user_id))

    async def balances(self, user_id: int) -> tuple[Decimal, Decimal]:
        """``(wallet, bank)`` for a member, both rounded like the economy does."""
        wallet_id = await self.wallet_id(user_id)
        wallet = Decimal(await self.db.get_wallet_balance_by_user_id(int(user_id)) or 0)
        bank = Decimal(await self.db.get_bank_balance(wallet_id) or 0)
        return (
            AmountUtils.round_currency(wallet),
            AmountUtils.round_currency(bank),
        )

    async def total(self, user_id: int) -> Decimal:
        wallet, bank = await self.balances(user_id)
        return wallet + bank

    async def charge(
        self,
        user_id: int,
        amount: int | Decimal,
        description: str,
        *,
        allow_bank: bool = False,
    ) -> bool:
        """Spend coins, wallet first. ``False`` when the member can't afford it.

        The bank is only dipped into when ``allow_bank`` is set, which is how
        the vault's auctions behave: they take a full wallet-then-bank payment
        like the rest of the bot's bidding games, while a plain purchase comes
        out of the wallet alone.
        """
        amount = AmountUtils.round_currency(Decimal(amount or 0))
        if amount <= 0:
            return True

        wallet_id = await self.wallet_id(user_id)
        wallet, bank = await self.balances(user_id)
        affordable = (wallet + bank) if allow_bank else wallet
        if affordable < amount:
            return False

        if allow_bank and wallet < amount:
            shortfall = AmountUtils.round_currency(amount - wallet)
            try:
                await self.db.withdraw_from_bank(
                    wallet_id, shortfall, f"{description} (from bank)"
                )
            except ValueError:
                logger.info("[Vault] Could not draw %s from the bank", user_id)
                return False

        try:
            await self.db.spend_from_wallet(wallet_id, amount, description)
        except ValueError as error:
            logger.info("[Vault] Charge failed for %s: %s", user_id, error)
            return False
        return True

    async def pay(self, user_id: int, amount: int | Decimal, description: str) -> bool:
        """Pay coins out of the server's treasury as a fee-free reward."""
        amount = AmountUtils.round_currency(Decimal(amount or 0))
        if amount <= 0:
            return False
        try:
            await self.db.process_treasury_transaction(
                wallet_id=await self.wallet_id(user_id),
                amount=amount,
                description=description,
                transaction_type=self.REWARD,
            )
        except ValueError as error:
            # The treasury protects its floor, and the circuit breaker can veto
            # payouts outright; either way the caller keeps a sane message.
            logger.warning("[Vault] Payout to %s failed: %s", user_id, error)
            return False
        return True

    async def pay_between(
        self, sender_id: int, receiver_id: int, amount: int | Decimal, description: str
    ) -> bool:
        """Pay one member from another through the economy's own P2P path."""
        amount = AmountUtils.round_currency(Decimal(amount or 0))
        if amount <= 0:
            return True

        wallet, _bank = await self.balances(sender_id)
        if wallet < amount:
            return False

        try:
            await self.db.process_p2p_transaction(
                sender_wallet_id=await self.wallet_id(sender_id),
                receiver_wallet_id=await self.wallet_id(receiver_id),
                amount=amount,
                description=description,
                fee_from_amount=True,
            )
        except ValueError as error:
            logger.info("[Vault] P2P payment failed: %s", error)
            return False
        return True


# ---------------------------------------------------------------------------
# The cog
# ---------------------------------------------------------------------------

class Vault(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.api = VaultAPI()
        self.store = VaultStore()
        self.bank = VaultBank(bot)
        self._market_lock = asyncio.Lock()

    async def cog_load(self) -> None:
        self._auction_loop.start()
        self._market_loop.start()

    def cog_unload(self) -> None:
        self._auction_loop.cancel()
        self._market_loop.cancel()
        asyncio.create_task(self.api.close())

    # -- market ------------------------------------------------------

    async def refresh_market(
        self, scope: int | None = None, *, force: bool = False
    ) -> float:
        """Re-read the economy this scope's prices are measured against.

        Frozen prices are *shares* of the money members hold, so this is what
        keeps them honest: the scope's circulating supply (total supply if a
        fresh server's treasury is holding everything) becomes the
        ``market_size`` every price is multiplied by. Cached briefly so a page
        of cards doesn't hit the database per render, which is also what lets
        ``,vault market`` report how far the economy has moved since the vault
        opened here.
        """
        scope = current_scope() if scope is None else int(scope)
        if not force and self.store.market_is_fresh(scope):
            return self.store.market_size(scope)

        async with self._market_lock:
            if not force and self.store.market_is_fresh(scope):
                return self.store.market_size(scope)
            try:
                record = await self.bot.database.get_supply_record()
            except Exception:
                logger.exception("[Vault] Could not read the money supply")
                return self.store.market_size(scope)

            circulating = float(record.circulating or 0)
            treasury = float(record.treasury or 0)
            total = float(record.total_supply or 0) or (circulating + treasury)
            # Members' money is the market. When circulating is empty the total
            # stands in, so even a brand new scope prices items above zero.
            size = circulating if circulating > 0 else total
            self.store.market_anchor(scope, size)
            self.store.set_market(
                scope,
                size,
                supply=total,
                circulating=circulating,
                treasury=treasury,
            )
            return size

    async def ensure_market(self) -> float:
        """Make sure the active scope's prices reflect today's economy."""
        try:
            return await self.refresh_market()
        except Exception:
            logger.exception("[Vault] Market refresh failed")
            return self.store.market_size()

    @tasks.loop(minutes=10)
    async def _market_loop(self) -> None:
        """Keep the index of every scope we have seen reasonably current."""
        for scope in self.store.market_scopes() or [0]:
            with use_scope(scope):
                try:
                    await self.refresh_market(scope, force=True)
                except Exception:
                    logger.exception("[Vault] Market refresh failed for scope %s", scope)

    @_market_loop.before_loop
    async def _before_market_loop(self) -> None:
        await self.bot.wait_until_ready()

    # -- achievements ------------------------------------------------

    async def check_achievements(self, user_id: int) -> list[dict]:
        """Unlock anything the member just earned and pay its reward.

        Rewards come out of the server's treasury, so a vault milestone pays
        real coins. Every call is safe: one check only ever unlocks a badge
        once, and a payout the treasury has to cap just logs and moves on.
        """
        try:
            stats = self.store.vault_stats(user_id)
            unlocked = self.store.unlocked_achievements(user_id)
            fresh = [
                key
                for key in self.store.earned_achievements(stats)
                if key not in unlocked
            ]
            awarded: list[dict] = []
            for key in fresh:
                spec = VAULT_ACHIEVEMENTS[key]
                # Priced off the market at the moment it is earned, like every
                # other figure in the vault.
                reward = achievement_reward(spec["weight"], self.store.market_size())
                await self.store.mark_achievement(user_id, key)
                paid = await self.bank.pay(
                    user_id, reward, f"Vault achievement: {spec['title']}"
                )
                awarded.append({**spec, "key": key, "reward": reward, "paid": paid})
            return awarded
        except Exception:
            logger.exception("[Vault] Achievement check failed for %s", user_id)
            return []

    # -- auctions ----------------------------------------------------

    @tasks.loop(seconds=AUCTION_CHECK_INTERVAL)
    async def _auction_loop(self) -> None:
        """Close every auction that has gone ``AUCTION_IDLE_SECONDS`` without a bid.

        An auction settles money, so it has to run under the scope it was
        started in rather than whatever the loop's task happens to default to.
        """
        now = time.time()
        for auction in self.store.list_auctions():
            last_activity = float(
                auction.get("last_activity") or auction.get("created_at") or 0
            )
            if now - last_activity < AUCTION_IDLE_SECONDS:
                continue
            with use_scope(auction.get("scope") or 0):
                try:
                    await self.finalize_auction(auction["id"])
                except Exception:
                    logger.exception(
                        "[Vault] Failed to finalize auction %s", auction.get("id")
                    )

    @_auction_loop.before_loop
    async def _before_auction_loop(self) -> None:
        await self.bot.wait_until_ready()

    def build_auction_view(self, auction: dict) -> AuctionView:
        return AuctionView(self, auction)

    async def _edit_auction_message(self, auction: dict, text: str) -> None:
        channel = self.bot.get_channel(int(auction.get("channel_id") or 0))
        message_id = auction.get("message_id")
        if channel is None or message_id is None:
            return
        try:
            message = await channel.fetch_message(int(message_id))
            await message.edit(view=simple_view(text, timeout=10))
        except Exception:
            logger.debug("[Vault] Could not update auction message %s", message_id)

    async def refresh_auction_message(self, auction_id: str) -> None:
        auction = self.store.get_auction(auction_id)
        if auction is None:
            return
        channel = self.bot.get_channel(int(auction.get("channel_id") or 0))
        message_id = auction.get("message_id")
        if channel is None or message_id is None:
            return
        try:
            message = await channel.fetch_message(int(message_id))
            await message.edit(view=self.build_auction_view(auction))
        except Exception:
            logger.debug("[Vault] Could not refresh auction message %s", message_id)

    async def _auction_pay(self, user_id: int, amount: int) -> bool:
        """Take ``amount`` from the wallet first, then the bank. False if poor."""
        return await self.bank.charge(
            user_id, amount, "Vault auction bid", allow_bank=True
        )

    async def place_auction_bid(
        self,
        auction_id: str,
        bidder_id: int,
        bidder_name: str,
        amount: int,
    ) -> tuple[bool, str]:
        """Validate, escrow and record a bid. Returns ``(ok, message)``."""
        auction = self.store.get_auction(auction_id)
        if auction is None:
            return False, "❌ This auction is no longer running."
        if bidder_id == int(auction.get("seller_id") or 0):
            return False, "❌ You can't bid on your own auction."

        start_price = int(auction.get("start_price") or 0)
        current_bid = int(auction.get("current_bid") or 0)
        if amount < start_price:
            return False, f"❌ The starting price is **{format_cash(start_price)}**."
        if current_bid and amount <= current_bid:
            return (
                False,
                f"❌ The current bid is **{format_cash(current_bid)}** — bid higher than that.",
            )

        if not await self._auction_pay(bidder_id, amount):
            return False, "❌ You don't have enough cash (wallet + bank) for that bid."

        previous_bidder = auction.get("current_bidder")
        previous_amount = current_bid

        auction["current_bid"] = amount
        auction["current_bidder"] = bidder_id
        auction["current_bidder_name"] = bidder_name
        auction["bids"] = int(auction.get("bids") or 0) + 1
        auction["last_activity"] = time.time()
        await self.store.save_auction(auction)

        if previous_bidder and previous_amount:
            # The outbid member's escrow is already in the treasury, so the
            # refund comes back out of it.
            await self.bank.pay(
                int(previous_bidder),
                int(previous_amount),
                f"Auction outbid refund: {auction.get('title')}",
            )

        return (
            True,
            f"✅ Bid **{format_cash(amount)}** placed on "
            f"**{auction.get('title')}**.",
        )

    async def handle_auction_bid(
        self,
        interaction: discord.Interaction,
        auction_id: str,
        raw_amount: str,
    ) -> None:
        """Shared bid entry point for the modal button."""
        try:
            amount = int(re.sub(r"[,_\s]", "", str(raw_amount)))
        except (TypeError, ValueError):
            await interaction.response.send_message(
                "❌ Enter the bid as a whole number of cash.", ephemeral=True
            )
            return
        if amount <= 0:
            await interaction.response.send_message(
                "❌ Bid at least 1 cash.", ephemeral=True
            )
            return

        ok, message = await self.place_auction_bid(
            auction_id, interaction.user.id, interaction.user.display_name, amount
        )
        await interaction.response.send_message(message, ephemeral=not ok)
        if ok:
            await self.refresh_auction_message(auction_id)

    async def finalize_auction(self, auction_id: str) -> Optional[str]:
        """Award (or return) the escrowed item and post the result on the card."""
        auction = self.store.get_auction(auction_id)
        if auction is None:
            return None

        record = auction.get("record") or {}
        item = record.get("item") or {}
        seller_id = int(auction.get("seller_id") or 0)
        winner_id = auction.get("current_bidder")
        winning_bid = int(auction.get("current_bid") or 0)

        if winner_id:
            await self.store.add_item(int(winner_id), item, price=winning_bid)
            await self.bank.pay(
                seller_id, winning_bid, f"Vault auction sale: {auction.get('title')}"
            )
            await self.check_achievements(int(winner_id))
            text = (
                f"# 🔨 Auction ended\n"
                f"**{auction.get('title')}** sold to "
                f"**{auction.get('current_bidder_name')}** for "
                f"**{format_cash(winning_bid)}**."
            )
        else:
            await self.store.add_item(
                seller_id, item, price=int(record.get("price_paid") or 0)
            )
            text = (
                f"# ⌛ Auction cancelled\n"
                f"Nobody bid on **{auction.get('title')}**, so it has been returned "
                f"to **{auction.get('seller_name')}**."
            )

        await self.store.delete_auction(auction_id)
        await self._edit_auction_message(auction, text)
        return text

    # -- Discord helpers --------------------------------------------

    async def send_error(self, ctx: commands.Context, message: str) -> None:
        await ctx.send(view=simple_view(message))

    async def edit_with_error(self, interaction: discord.Interaction, message: str) -> None:
        try:
            await interaction.edit_original_response(view=simple_view(message))
        except Exception:
            pass

    def build_card_view(
        self,
        item: dict,
        *,
        song: Any = None,
        owned: bool,
        shared: bool = False,
        buyer_id: Optional[int] = None,
        footer: Optional[str] = None,
        show_buy: bool = False,
    ) -> VaultCardView:
        view = VaultCardView(
            item,
            cog=self,
            song=song,
            owned=owned,
            shared=shared,
            buyer_id=buyer_id,
            show_buy=show_buy,
            footer=footer,
        )
        return view

    # -- item construction ------------------------------------------

    async def _resolve_full_song(self, song: Any) -> Any:
        song_id = first_value(
            song, "id", "song_id", "songId", "public_id", "publicId", default=None
        )
        if song_id is None:
            return song
        full = await self.api.get_song(song_id)
        if not full:
            return song
        if isinstance(full, dict):
            wrapped = full.get("song") or full.get("data")
            return wrapped if isinstance(wrapped, dict) else full
        return full

    async def prepare_song_item(self, song: Any) -> tuple[dict, Any]:
        song = await self._resolve_full_song(song)
        file_info = await self.api.primary_file_for_song(song)
        # ``get_category`` already folded this onto one of the four real API
        # categories, so there is no invented bucket left to fall back to.
        category = get_category(song)
        year = get_record_year(song)
        await self.ensure_market()

        item = {
            "key": song_key(song),
            "kind": "song",
            "title": get_song_title(song),
            "category": category,
            "year": year,
            "era": clean_era_display(first_value(song, "era", "era_name", "eraName")),
            "path": (file_info or {}).get("path"),
            "download": (file_info or {}).get("download"),
            "cover": get_cover_url(song),
        }
        # Freezing records the share once and hands back today's price.
        await self.store.freeze_price(item)
        return item, song

    async def prepare_session_item(self, session: dict) -> tuple[dict, Any]:
        name = str(session.get("name", "Session"))
        title = name.rsplit(".", 1)[0] if "." in name else name
        era = clean_era_display(session.get("era"))
        # The API calls these ``recording_session``; the old code tagged them
        # "Session", which no multiplier ever matched.
        category = "recording_session"
        year = year_from_text(session.get("era")) or None
        await self.ensure_market()
        path = session.get("path")
        download = session.get("download") or (make_download_url(path) if path else None)

        item = {
            "key": f"session:{path or clean_match_key(title)}",
            "kind": "session",
            "title": title,
            "category": category,
            "year": year,
            "era": era,
            "path": path,
            "download": download,
            "cover": None,
        }
        await self.store.freeze_price(item)
        return item, None

    async def prepare_ogfile_item(self, song: Any) -> tuple[dict, Any]:
        """Price a song's OG (original) file as its own ownable vault item.

        An OG file is worth half of its song, plus a fifth more once that song
        has been leaked anywhere in the vault.
        """
        song_item, song_obj = await self.prepare_song_item(song)
        song_key = song_item["key"]
        bonus = OG_FILE_LEAK_BONUS if self.store.song_has_leaked(song_key) else 0.0
        song_share = float(song_item.get("share") or self.store.get_share(song_key) or 0)
        og_share = song_share * OG_FILE_RATE * (1.0 + bonus)

        item = {
            "key": f"og:{song_key}",
            "kind": "ogfile",
            "title": f"{item_display_name(song_item)} (OG File)",
            "category": song_item.get("category") or "OG File",
            "year": song_item.get("year"),
            "era": song_item.get("era"),
            "path": song_item.get("path"),
            "download": song_item.get("download"),
            "cover": song_item.get("cover"),
            "og_of": song_key,
            "song_price": song_item.get("price") or 0,
            "share": og_share,
        }
        # An OG file has no catalog row of its own: it tracks its song's share,
        # which is what ``price_of`` and ``base_item_value`` read back.
        og_price = price_for_share(og_share, self.store.market_size()) if og_share else 0
        item["base_price"] = max(MIN_PRICE, og_price)
        item["price"] = item["base_price"]
        return item, song_obj

    # -- buying ------------------------------------------------------

    async def process_buy(self, user_id: int, item: dict) -> tuple[bool, str, dict]:
        key = item["key"]
        title = item_display_name(item)

        if self.store.user_owns(user_id, key):
            return False, f"❌ You already own **{title}**.", item

        other_owner = self.store.owner_of(key)
        if other_owner is not None:
            return (
                False,
                f"❌ **{title}** is already owned by <@{other_owner}> and can only "
                f"change hands through a trade.",
                item,
            )

        # The price is settled at the moment of the click: the share is frozen,
        # the economy is not, so this is where a growing or shrinking supply is
        # actually felt.
        await self.ensure_market()
        if item.get("kind") != "ogfile" and self.store.get_share(key) is None:
            await self.store.freeze_price(item)
        price = self.store.price_of(item)
        item["price"] = price

        wallet, _bank = await self.bank.balances(user_id)
        if wallet < price:
            return (
                False,
                f"❌ **{title}** costs {format_cash(price)} but your wallet holds "
                f"only {format_cash(wallet)}.",
                item,
            )

        if not await self.bank.charge(user_id, price, f"Vault purchase: {title}"):
            return (
                False,
                f"❌ The purchase of **{title}** was declined by the economy.",
                item,
            )
        await self.store.add_item(user_id, item, price)

        record = self.store.get_user_item(user_id, key) or {}
        owned_item = {**item, "price_paid": price, "leaked": False}
        if record:
            owned_item["price_paid"] = record.get("price_paid", price)
        awarded = await self.check_achievements(user_id)
        return (
            True,
            f"✅ Bought **{title}** for {format_cash(price)}."
            f"{achievement_note(awarded)}",
            owned_item,
        )

    # -- selling / leaking ------------------------------------------

    def item_value(self, key: str, record: dict) -> int:
        return self.store.item_value(key, record)

    async def process_sell(self, user_id: int, key: str) -> tuple[bool, str]:
        record = self.store.get_user_item(user_id, key)
        if not record:
            return False, "❌ You don't own that item."

        item = record.get("item", {})
        title = item_display_name(item)
        price = self.item_value(key, record)

        # A leaked item can't be sold normally; it has already been "groupbought"
        # and its vault value dropped by 75%, so cashing out happens through the
        # leak flow instead.
        if record.get("leaked"):
            return (
                False,
                f"❌ **{title}** has already been leaked and can't be sold.",
            )

        rate = SELL_RATE
        payout = max(1, int(price * rate))

        removed = await self.store.remove_item(user_id, key)
        if not removed:
            return False, "❌ Could not sell that item."

        await self.store.record_stat(user_id, "sold")
        paid = await self.bank.pay(user_id, payout, f"Vault sale: {title}")
        awarded = await self.check_achievements(user_id)
        note = "" if paid else "\n-# The treasury is protected right now, so the payout was held back."
        return True, (
            f"💰 Sold **{title}** for {format_cash(payout)}.\n"
            f"-# The item has been removed from your vault.{note}"
            f"{achievement_note(awarded)}"
        )

    async def process_leak(
        self, user_id: int, key: str
    ) -> tuple[bool, str, Optional[discord.ui.LayoutView]]:
        """Leak an item as a "groupbuy" and return (ok, message, result view).

        Instead of a flat bounty, the leak resolves as a groupbuy: buyers
        together pay somewhere between ``GROUPBUY_MIN_RATE`` and
        ``GROUPBUY_MAX_RATE`` of the song's value, and the leaker pockets the
        final amount. The result is announced in a dedicated embed view.
        """
        record = self.store.get_user_item(user_id, key)
        if not record:
            return False, "❌ You don't own that item.", None
        if record.get("leaked"):
            return False, "❌ That item has already been leaked.", None

        item = record.get("item", {})
        title = item_display_name(item)
        price = self.item_value(key, record)

        # The groupbuy lands anywhere between 75% and 250% of the item's
        # value; 100% means buyers paid exactly what the vault thinks it's
        # worth.
        rate = random.uniform(GROUPBUY_MIN_RATE, GROUPBUY_MAX_RATE)
        payout = max(1, int(price * rate))

        if not await self.store.mark_leaked(user_id, key):
            return False, "❌ Could not leak that item.", None

        if item.get("kind") == "song":
            await self.store.record_song_leak(key)
        await self.store.apply_value_drop(user_id, key, LEAK_VALUE_DROP)

        await self.store.record_stat(user_id, "leaked")
        await self.bank.pay(user_id, payout, f"Vault groupbuy: {title}")
        awarded = await self.check_achievements(user_id)
        view = self.groupbuy_view(item, price, payout, rate)
        return True, (
            f"💧 Leaked **{title}** — the groupbuy paid out {format_cash(payout)}!\n"
            f"-# Its vault value dropped by {int(LEAK_VALUE_DROP * 100)}%."
            f"{achievement_note(awarded)}"
        ), view

    def groupbuy_view(
        self, item: dict, base_value: int, payout: int, rate: float
    ) -> discord.ui.LayoutView:
        """The "groupbuy reached ..." embed shown after a successful leak."""
        title = item_display_name(item)
        cover = valid_http_url(item.get("cover"))
        leaked_before = bool(item.get("leaked"))

        lines = [
            "# 📣 Groupbuy Completed",
            f"You ran a groupbuy for **{title}**.",
            "",
            f"It reached {format_cash(payout)} ",
            "",
            f"💰 Payout: {format_cash(payout)} "
            f"({rate * 100:.1f}% of its {format_cash(base_value)} value)",
        ]
        if leaked_before:
            lines.append("-# This item was already marked as leaked.")

        container = discord.ui.Container(accent_color=EMBED_COLOR)
        if cover:
            container.add_item(
                discord.ui.Section(
                    text_display("\n".join(lines)),
                    accessory=discord.ui.Thumbnail(media=cover),
                )
            )
        else:
            container.add_item(text_display("\n".join(lines)))

        container.add_item(small_separator())
        container.add_item(
            text_display(
                "-# Vault value dropped by "
                f"{int(LEAK_VALUE_DROP * 100)}% and the groupbuy price is final."
            )
        )

        view = discord.ui.LayoutView(timeout=VIEW_TIMEOUT)
        view.add_item(container)
        return view

    # -- sharing -----------------------------------------------------

    def build_share_offer(
        self,
        sender: discord.abc.User,
        key: str,
        target: discord.abc.User,
    ) -> tuple[Optional[dict], Optional[str]]:
        """Build the offer dict for a share.

        Returns ``(None, error)`` when the share can't happen because the
        sender no longer owns the item or the target already owns it.
        """
        record = self.store.get_user_item(sender.id, key)
        if record is None:
            return None, "❌ You don't own that item anymore."

        item = record.get("item", {})
        title = item_display_name(item)
        if self.store.user_owns(target.id, key):
            return None, (
                f"❌ **{target.display_name}** already owns **{title}**, "
                f"so there's no need to share it."
            )

        return {
            "from_id": sender.id,
            "to_id": target.id,
            "from_name": sender.display_name,
            "to_name": target.display_name,
            "to_mention": target.mention,
            "give_key": key,
            "give_title": title,
        }, None

    async def open_share(self, ctx: commands.Context, key: str, target: discord.abc.User) -> None:
        """Post a share offer for ``target`` to accept or decline."""
        offer, error = self.build_share_offer(ctx.author, key, target)
        if offer is None:
            await ctx.send(view=simple_view(error or "❌ You can't share that item."))
            return
        view = VaultShareView(self, offer)
        view.message = await ctx.send(view=view)

    async def execute_share(self, offer: dict) -> tuple[str, Optional[dict]]:
        """Deliver a file to a member who accepted.

        The sharer keeps their copy, and the recipient receives one that now
        shows up in their vault as well.
        """
        from_id = offer["from_id"]
        to_id = offer["to_id"]
        key = offer["give_key"]
        store = self.store

        record = store.get_user_item(from_id, key)
        if record is None:
            return (
                f"❌ **{offer['from_name']}** no longer owns **{offer['give_title']}**.",
                None,
            )

        # Re-check on accept: the recipient may have acquired it since the
        # offer was posted. Sharing to an owner is prevented, not skipped.
        if store.user_owns(to_id, key):
            return (
                f"❌ **{offer['to_name']}** already owns "
                f"**{offer['give_title']}**, so it can't be shared with them.",
                None,
            )

        # Every share puts the file in more hands, shaving its value.
        await store.apply_value_drop(from_id, key, SHARE_VALUE_DROP)
        record = store.get_user_item(from_id, key) or record

        item = dict(record.get("item", {}))
        item["price"] = store.item_value(key, record)
        item["leaked"] = bool(record.get("leaked"))
        await store.add_item(to_id, item, 0)

        text = (
            f"📤 **{offer['from_name']}** shared **{offer['give_title']}** with "
            f"**{offer['to_name']}**.\n"
            f"-# Sharing dropped its value by {int(SHARE_VALUE_DROP * 100)}%."
            f"\n-# It was added to their vault."
        )
        return text, item

    async def execute_trade(self, offer: dict) -> str:
        """Validate and settle a trade offer. Returns a human-readable result."""
        from_id = offer["from_id"]
        to_id = offer["to_id"]
        give_key = offer["give_key"]
        receive_key = offer.get("receive_key")
        price = int(offer.get("price") or 0)
        store = self.store

        if not store.user_owns(from_id, give_key):
            return f"❌ **{offer['from_name']}** no longer owns **{offer['give_title']}**."
        if store.user_owns(to_id, give_key):
            return f"❌ **{offer['to_name']}** already owns **{offer['give_title']}**."

        # Item-for-item swap.
        if receive_key:
            if not store.user_owns(to_id, receive_key):
                return f"❌ **{offer['to_name']}** no longer owns **{offer['receive_title']}**."
            if store.user_owns(from_id, receive_key):
                return f"❌ **{offer['from_name']}** already owns **{offer['receive_title']}**."

            if not await store.transfer_item(from_id, to_id, give_key):
                return "❌ Trade failed while moving the first item."
            if not await store.transfer_item(to_id, from_id, receive_key):
                await store.transfer_item(to_id, from_id, give_key)
                return "❌ Trade failed while moving the second item. Nothing was exchanged."
            return (
                f"🤝 Trade complete! **{offer['from_name']}** and **{offer['to_name']}** "
                f"swapped **{offer['give_title']}** for **{offer['receive_title']}**."
            )

        # Cash sale, paid member-to-member through the economy's own P2P path
        # (so the economy's transfer fee applies to it like any other payment).
        if price > 0:
            wallet, _bank = await self.bank.balances(to_id)
            if wallet < price:
                return (
                    f"❌ **{offer['to_name']}** only has {format_cash(wallet)} "
                    f"and can't pay {format_cash(price)}."
                )

            if not await self.bank.pay_between(
                to_id, from_id, price, f"Vault trade: {offer['give_title']}"
            ):
                return "❌ The payment could not be sent — nothing was exchanged."
            if not await store.transfer_item(from_id, to_id, give_key, price_paid=price):
                await self.bank.pay_between(
                    from_id, to_id, price, "Vault trade refund"
                )
                return "❌ Trade failed and the payment was refunded."
            await self.check_achievements(from_id)
            await self.check_achievements(to_id)
            return (
                f"🤝 Trade complete! **{offer['to_name']}** bought "
                f"**{offer['give_title']}** from **{offer['from_name']}** for "
                f"{format_cash(price)}.\n"
                f"-# The economy's transfer fee is taken out of that payment."
            )

        # Free gift.
        if not await store.transfer_item(from_id, to_id, give_key):
            return "❌ Trade failed while transferring the item."
        return (
            f"🎁 **{offer['from_name']}** gifted **{offer['give_title']}** "
            f"to **{offer['to_name']}**."
        )

    async def dispatch_action(
        self,
        ctx: commands.Context,
        action: str,
        key: str,
        *,
        target: Optional[discord.abc.User] = None,
    ) -> None:
        user_id = ctx.author.id

        if action == "share":
            if target is None:
                await ctx.send(
                    view=simple_view(
                        f"❌ Mention who to share with: `{ctx.prefix}vault share <song> @user`"
                    )
                )
                return
            await self.open_share(ctx, key, target)
            return

        if action == "sell":
            ok, message = await self.process_sell(user_id, key)
        elif action == "leak":
            ok, message, result_view = await self.process_leak(user_id, key)
        else:
            ok, message, result_view = False, "❌ Unknown action.", None

        if ok and result_view is not None:
            await ctx.send(view=result_view)
        else:
            await ctx.send(view=simple_view(message))

    async def run_action(
        self,
        interaction: discord.Interaction,
        action: str,
        key: str,
        *,
        target: Optional[discord.abc.User] = None,
    ) -> None:
        user_id = interaction.user.id

        if action == "share":
            if target is None:
                await interaction.edit_original_response(
                    view=simple_view("❌ Mention who to share with.")
                )
                return
            offer, error = self.build_share_offer(interaction.user, key, target)
            if offer is None:
                await interaction.edit_original_response(
                    view=simple_view(error or "❌ You can't share that item.")
                )
                return
            view = VaultShareView(self, offer)
            view.message = await interaction.edit_original_response(view=view)
            return

        if action == "sell":
            ok, message = await self.process_sell(user_id, key)
        elif action == "leak":
            ok, message, result_view = await self.process_leak(user_id, key)
        else:
            ok, message = False, "❌ Unknown action."

        if ok and result_view is not None:
            await interaction.edit_original_response(view=result_view)
        else:
            await interaction.edit_original_response(view=simple_view(message))

    # -- lookups -----------------------------------------------------

    async def resolve_item(self, query: str) -> Optional[dict]:
        """Resolve a query to a priced item, freezing the price if needed."""
        entry = self.store.find_catalog_by_title(query)
        if entry is not None:
            entry.setdefault("kind", "song")
            if is_unsurfaced(entry):
                entry["path"] = None
                entry["download"] = None
            return entry

        results = await self.api.search_songs(query)
        if results:
            wanted = query.strip().casefold()
            exact = next(
                (r for r in results if get_song_title(r).strip().casefold() == wanted),
                None,
            )
            item, _song = await self.prepare_song_item(exact or results[0])
            return item

        sessions = await self.api.search_sessions(query)
        if sessions:
            item, _song = await self.prepare_session_item(sessions[0])
            return item
        return None

    async def select_owned_item(
        self,
        ctx: commands.Context,
        action: str,
        query: str,
        *,
        exclude_leaked: bool = False,
        target: Optional[discord.abc.User] = None,
    ) -> None:
        # Every item this opens on is priced off the market, so read the economy
        # before showing (or paying out) anything.
        await self.ensure_market()
        if query:
            key = self.find_owned_key(ctx.author.id, query)
            if key is None:
                await ctx.send(view=simple_view(f"❌ You don't have **{query}** in your vault."))
                return
            await self.dispatch_action(ctx, action, key, target=target)
            return

        items = self.store.owned_list(ctx.author.id)
        if exclude_leaked:
            items = [(k, r) for k, r in items if not r.get("leaked")]
        if not items:
            prefix = await prefix_for(ctx)
            await ctx.send(
                view=simple_view(
                    "❌ Your vault is empty. Use "
                    f"`{prefix}vault search` and `{prefix}vault buy` first."
                )
            )
            return

        view = discord.ui.LayoutView(timeout=SELECT_TIMEOUT)
        container = discord.ui.Container(accent_color=EMBED_COLOR)
        container.add_item(
            text_display(f"# Select an item to {action}\n\nChoose from your vault collection:")
        )
        container.add_item(
            discord.ui.ActionRow(
                VaultItemSelect(items, ctx.author.id, self, action, target=target)
            )
        )
        view.add_item(container)

        message = await ctx.send(view=view)
        view.message = message  # type: ignore[attr-defined]

    def find_owned_key(self, user_id: int, query: str) -> Optional[str]:
        wanted = query.strip().casefold()
        if not wanted:
            return None
        for key, record in self.store.owned_list(user_id):
            item = record.get("item", {})
            if key.casefold() == wanted or item_display_name(item).casefold() == wanted:
                return key
        for key, record in self.store.owned_list(user_id):
            item = record.get("item", {})
            if wanted in item_display_name(item).casefold():
                return key
        return None

    # ---------------------------------------------------------------
    # ,vault  (group root / help)
    # ---------------------------------------------------------------

    @commands.group(name="vault", aliases=["vlt"], invoke_without_command=True)
    async def vault_group(self, ctx: commands.Context) -> None:
        prefix = ctx.prefix
        view = simple_view(
            f"# 🗄️ The Vault\n"
            f"A Juice WRLD leak/session trading-card game played with the coins in "
            f"your `{prefix}balance`. A price comes from the recording year (newer "
            f"costs more) and the song's category — `Unsurfaced` is the holy grail, "
            f"`Released` is the cheapest. Every value then moves with the server's "
            f"economy, so a collection gains during inflation and loses during "
            f"deflation.\n\n"
            f"**Commands**\n"
            f"`{prefix}vault search <song>` — price a song/session\n"
            f"`{prefix}vault buy <song>` — buy it with your wallet\n"
            f"`{prefix}vault ogfile <song>` — buy a song's OG file\n"
            f"`{prefix}vault sell [song]` — sell an item back\n"
            f"`{prefix}vault list [@user]` — a collection and its value\n"
            f"`{prefix}vault leak [song]` — leak an item as a groupbuy\n"
            f"`{prefix}vault share <song> @user` — send the file to someone\n"
            f"`{prefix}vault trade @user <song> [price]` — trade with another player\n"
            f"`{prefix}vault auction <song> <start price>` — auction an item\n"
            f"`{prefix}vault bid <amount>` — bid on this channel's auction\n"
            f"`{prefix}vault auctions` — open auctions in this server\n"
            f"`{prefix}vault market` — the live market index\n"
            f"`{prefix}vault achievements` — badges that pay coins\n"
            f"`{prefix}vault leaderboard` — top collections by value\n\n"
            f"-# Omit the song to pick from a dropdown. Your vault is kept per "
            f"server, alongside that server's economy."
        )
        await ctx.send(view=view)

    # ---------------------------------------------------------------
    # ,vault search
    # ---------------------------------------------------------------

    @vault_group.command(name="search", aliases=["find", "price"])
    async def vault_search(self, ctx: commands.Context, *, query: Optional[str] = None) -> None:
        if not query:
            await ctx.send(
                view=simple_view(
                    f"# Command: vault search\n\n"
                    f"**Syntax**\n`{ctx.prefix}vault search <song>`\n\n"
                    f"**Example**\n`{ctx.prefix}vault search hop out`\n\n"
                    f"**Pricing**\n"
                    f"• Newer recordings cost more\n"
                    f"• `Unsurfaced` is worth 20x unreleased\n"
                    f"• `Recording Session` carries a 2.5x premium\n"
                    f"• `Released` is worth half of unreleased\n"
                    f"• The base is frozen the first time an item is seen, then "
                    f"moves with the live market index"
                )
            )
            return

        message = await ctx.send(view=simple_view(f"🔍 Searching the vault for **{query}**..."))

        try:
            results = await self.api.search_songs(query)
            if results:
                wanted = query.strip().casefold()
                exact = next(
                    (r for r in results if get_song_title(r).strip().casefold() == wanted),
                    None,
                )
                if len(results) == 1 or exact is not None:
                    item, song = await self.prepare_song_item(exact or results[0])
                    view = self.build_card_view(
                        item, song=song, owned=False, buyer_id=ctx.author.id, show_buy=True
                    )
                    await message.edit(view=view)
                    return

                dropdown = VaultSearchSelectView(
                    query=query,
                    candidates=results,
                    author_id=ctx.author.id,
                    cog=self,
                    source="song",
                )
                await message.edit(view=dropdown)
                dropdown.message = message
                return

            sessions = await self.api.search_sessions(query)
            if sessions:
                if len(sessions) == 1:
                    item, _song = await self.prepare_session_item(sessions[0])
                    view = self.build_card_view(
                        item, owned=False, buyer_id=ctx.author.id, show_buy=True
                    )
                    await message.edit(view=view)
                    return

                dropdown = VaultSearchSelectView(
                    query=query,
                    candidates=sessions,
                    author_id=ctx.author.id,
                    cog=self,
                    source="session",
                )
                await message.edit(view=dropdown)
                dropdown.message = message
                return

            await message.edit(
                view=simple_view(f"❌ Couldn't find a song or session matching **{query}**.")
            )
        except discord.HTTPException:
            logger.exception("[Vault] Discord rejected search card")
            await self.send_error(ctx, "❌ Discord rejected the vault card.")
        except Exception:
            logger.exception("[Vault] Search failed")
            await self.send_error(ctx, "❌ Failed to search the vault.")

    # ---------------------------------------------------------------
    # ,vault ogfile
    # ---------------------------------------------------------------

    @vault_group.command(name="ogfile", aliases=["og"])
    async def vault_ogfile(self, ctx: commands.Context, *, query: Optional[str] = None) -> None:
        if not query:
            await ctx.send(
                view=simple_view(
                    f"# Command: vault ogfile\n\n"
                    f"**Syntax**\n`{ctx.prefix}vault ogfile <song>`\n\n"
                    f"**Example**\n`{ctx.prefix}vault ogfile hop out`\n\n"
                    f"**OG files**\n"
                    f"• Worth 50% of the song's value\n"
                    f"• +20% once the song has leaked in the vault\n"
                    f"-# You own it as a separate item in your collection."
                )
            )
            return

        message = await ctx.send(view=simple_view(f"🔍 Searching the vault for **{query}**..."))

        try:
            results = await self.api.search_songs(query)
            if not results:
                await message.edit(
                    view=simple_view(f"❌ Couldn't find a song matching **{query}**.")
                )
                return

            wanted = query.strip().casefold()
            exact = next(
                (r for r in results if get_song_title(r).strip().casefold() == wanted),
                None,
            )
            if len(results) == 1 or exact is not None:
                item, song = await self.prepare_ogfile_item(exact or results[0])
                view = self.build_card_view(
                    item, song=song, owned=False, buyer_id=ctx.author.id, show_buy=True
                )
                await message.edit(view=view)
                return

            dropdown = VaultSearchSelectView(
                query=query,
                candidates=results,
                author_id=ctx.author.id,
                cog=self,
                source="ogfile",
            )
            await message.edit(view=dropdown)
            dropdown.message = message
        except discord.HTTPException:
            logger.exception("[Vault] Discord rejected OG file card")
            await self.send_error(ctx, "❌ Discord rejected the OG file card.")
        except Exception:
            logger.exception("[Vault] OG file search failed")
            await self.send_error(ctx, "❌ Failed to search the vault.")

    # ---------------------------------------------------------------
    # ,vault buy
    # ---------------------------------------------------------------

    @vault_group.command(name="buy", aliases=["purchase"])
    async def vault_buy(self, ctx: commands.Context, *, query: Optional[str] = None) -> None:
        if not query:
            await ctx.send(
                view=simple_view(
                    f"❌ Usage: `{ctx.prefix}vault buy <song>`\n"
                    f"Use `{ctx.prefix}vault search <song>` first to price it."
                )
            )
            return

        message = await ctx.send(view=simple_view(f"💰 Buying **{query}**..."))
        try:
            item = await self.resolve_item(query)
            if item is None:
                await message.edit(
                    view=simple_view(f"❌ Couldn't find **{query}** in the vault.")
                )
                return

            ok, text, owned_item = await self.process_buy(ctx.author.id, item)
            if ok:
                view = self.build_card_view(owned_item, owned=True, buyer_id=ctx.author.id)
                await message.edit(view=view)
            else:
                await message.edit(view=simple_view(text))
        except discord.HTTPException:
            logger.exception("[Vault] Discord rejected buy card")
            await self.send_error(ctx, "❌ Discord rejected the purchase.")
        except Exception:
            logger.exception("[Vault] Buy failed")
            await self.send_error(ctx, "❌ Failed to process the purchase.")

    # ---------------------------------------------------------------
    # ,vault sell
    # ---------------------------------------------------------------

    @vault_group.command(name="sell", aliases=["cashout"])
    async def vault_sell(self, ctx: commands.Context, *, query: str = "") -> None:
        await self.select_owned_item(ctx, "sell", query)

    # ---------------------------------------------------------------
    # ,vault list / inventory
    # ---------------------------------------------------------------

    @vault_group.command(name="list", aliases=["inventory", "inv", "collection"])
    async def vault_list(
        self, ctx: commands.Context, member: Optional[discord.Member] = None
    ) -> None:
        user = member or ctx.author
        # Valuing a collection is a market read, so make sure the index behind
        # those numbers is today's rather than whatever was cached at boot.
        await self.ensure_market()
        items = self.store.owned_list(user.id)
        if not items:
            if user.id == ctx.author.id:
                await ctx.send(
                    view=simple_view(
                        "❌ Your vault is empty.\n\n"
                        f"Use `{ctx.prefix}vault search <song>` to find items and "
                        f"`{ctx.prefix}vault buy` to collect them."
                    )
                )
            else:
                await ctx.send(
                    view=simple_view(f"❌ **{user.display_name}**'s vault is empty.")
                )
            return

        items.sort(key=lambda pair: self.item_value(pair[0], pair[1]), reverse=True)

        total_value = 0
        song_count = 0
        session_count = 0
        leaked_count = 0
        for key, record in items:
            total_value += self.item_value(key, record)
            if record.get("leaked"):
                leaked_count += 1
            if is_session_item(record.get("item", {})):
                session_count += 1
            else:
                song_count += 1

        view = VaultListPaginationView(
            items,
            user=user,
            author_id=ctx.author.id,
            prefix=ctx.prefix,
            total_value=total_value,
            song_count=song_count,
            session_count=session_count,
            leaked_count=leaked_count,
            # Price every row exactly like the portfolio total above, so the
            # list can't show a bought-at figure next to a live collection
            # value.
            value_fn=self.item_value,
        )
        await ctx.send(view=view)

    # ---------------------------------------------------------------
    # ,vault leak
    # ---------------------------------------------------------------

    @vault_group.command(name="gb")
    async def vault_leak(self, ctx: commands.Context, *, query: str = "") -> None:
        await self.select_owned_item(ctx, "leak", query, exclude_leaked=True)

    # ---------------------------------------------------------------
    # ,vault share
    # ---------------------------------------------------------------

    @vault_group.command(name="share")
    async def vault_share(self, ctx: commands.Context, *, args: str = "") -> None:
        member = ctx.message.mentions[0] if ctx.message.mentions else None
        # Strip any mentions so a song name containing spaces is kept intact.
        query = re.sub(r"<@!?\d+>", " ", args)
        query = re.sub(r"\s+", " ", query).strip()

        if member is None:
            await ctx.send(
                view=simple_view(
                    f"# Command: vault share\n\n"
                    f"**Syntax**\n`{ctx.prefix}vault share <song> @user`\n\n"
                    f"**Example**\n`{ctx.prefix}vault share hop out @friend`\n\n"
                    f"-# The member must accept before the file is delivered."
                )
            )
            return

        if member.id == ctx.author.id:
            await ctx.send(view=simple_view("❌ You can't share an item with yourself."))
            return
        if member.bot:
            await ctx.send(view=simple_view("❌ You can't share an item with a bot."))
            return

        await self.select_owned_item(ctx, "share", query, target=member)

    # ---------------------------------------------------------------
    # ,vault trade
    # ---------------------------------------------------------------

    @vault_group.command(name="trade", aliases=["offer"])
    async def vault_trade(
        self,
        ctx: commands.Context,
        member: Optional[discord.Member] = None,
        *,
        args: str = "",
    ) -> None:
        if member is None or not args.strip():
            await ctx.send(
                view=simple_view(
                    f"# Command: vault trade\n\n"
                    f"Trade an item from your vault with another player.\n\n"
                    f"**Sell for cash**\n"
                    f"`{ctx.prefix}vault trade @user <your item> <price>`\n"
                    f"-# e.g. `{ctx.prefix}vault trade @friend hop out 5000`\n\n"
                    f"**Swap items**\n"
                    f"`{ctx.prefix}vault trade @user <your item> for <their item>`\n"
                    f"-# e.g. `{ctx.prefix}vault trade @friend hop out for lucid dreams`\n\n"
                    f"**Gift** — leave the price off, or write `free`."
                )
            )
            return

        if member.id == ctx.author.id:
            await ctx.send(view=simple_view("❌ You can't trade with yourself."))
            return
        if member.bot:
            await ctx.send(view=simple_view("❌ You can't trade with a bot."))
            return

        receive_query: Optional[str] = None
        price = 0
        low = args.lower()
        if " for " in low:
            split = low.index(" for ")
            give_query = args[:split].strip()
            receive_query = args[split + len(" for "):].strip()
        else:
            parts = args.rsplit(None, 1)
            if len(parts) == 2 and parts[1].lstrip("-").isdigit():
                give_query = parts[0].strip()
                price = max(0, int(parts[1]))
            elif len(parts) == 2 and parts[1].lower() in ("free", "gift"):
                give_query = parts[0].strip()
            else:
                give_query = args.strip()

        if not give_query:
            await ctx.send(view=simple_view("❌ Say which of your items to trade."))
            return

        give_key = self.find_owned_key(ctx.author.id, give_query)
        if give_key is None:
            await ctx.send(
                view=simple_view(f"❌ You don't have **{give_query}** in your vault.")
            )
            return

        give_record = self.store.get_user_item(ctx.author.id, give_key) or {}
        give_item = give_record.get("item", {})

        offer: dict[str, Any] = {
            "from_id": ctx.author.id,
            "to_id": member.id,
            "from_name": ctx.author.display_name,
            "to_name": member.display_name,
            "give_key": give_key,
            "give_title": item_display_name(give_item),
            "price": price,
            "receive_key": None,
            "receive_title": None,
        }

        # A trade only makes sense if the target doesn't already own the
        # offered item, whether the trade is for cash, a gift, or a swap.
        if self.store.user_owns(member.id, give_key):
            await ctx.send(
                view=simple_view(
                    f"❌ **{member.display_name}** already owns "
                    f"**{item_display_name(give_item)}**, so there's no need to trade it."
                )
            )
            return

        if receive_query:
            receive_key = self.find_owned_key(member.id, receive_query)
            if receive_key is None:
                await ctx.send(
                    view=simple_view(
                        f"❌ **{member.display_name}** doesn't have **{receive_query}**."
                    )
                )
                return
            receive_record = self.store.get_user_item(member.id, receive_key) or {}
            receive_item = receive_record.get("item", {})
            offer["receive_key"] = receive_key
            offer["receive_title"] = item_display_name(receive_item)

        view = VaultTradeView(self, offer)
        message = await ctx.send(view=view)
        view.message = message

    # ---------------------------------------------------------------
    # ,vault auction
    # ---------------------------------------------------------------

    @vault_group.command(name="auction", aliases=["auctionitem"])
    async def vault_auction(self, ctx: commands.Context, *, args: str = "") -> None:
        parts = args.rsplit(None, 1)
        price_text = parts[1] if len(parts) == 2 else ""
        if not price_text or not re.sub(r"[,_]", "", price_text).isdigit():
            await ctx.send(
                view=simple_view(
                    f"# Command: vault auction\n\n"
                    f"**Syntax**\n`{ctx.prefix}vault auction <song> <start price>`\n\n"
                    f"**Example**\n`{ctx.prefix}vault auction hop out 25000`\n\n"
                    f"-# The auction ends 30 minutes after the last bid. With no bids "
                    f"at all it is cancelled and the item comes back to you."
                )
            )
            return

        query = parts[0].strip()
        start_price = int(re.sub(r"[,_]", "", price_text))
        if start_price <= 0:
            await ctx.send(view=simple_view("❌ The starting price must be at least 1."))
            return

        key = self.find_owned_key(ctx.author.id, query)
        if key is None:
            await ctx.send(
                view=simple_view(f"❌ You don't have **{query}** in your vault.")
            )
            return

        # Escrow the item so it can be handed to the winner (or returned) later.
        record = await self.store.remove_item(ctx.author.id, key)
        if record is None:
            await ctx.send(
                view=simple_view(f"❌ You don't have **{query}** in your vault.")
            )
            return

        item = record.get("item") or {}
        now = time.time()
        auction = {
            "id": uuid.uuid4().hex[:8],
            # The scope is remembered so the settlement loop can pay the right
            # server's economy even though it runs outside any command.
            "scope": current_scope(),
            "guild_id": ctx.guild.id if ctx.guild else 0,
            "channel_id": ctx.channel.id,
            "message_id": None,
            "seller_id": ctx.author.id,
            "seller_name": ctx.author.display_name,
            "key": key,
            "title": item_display_name(item),
            "record": record,
            "start_price": start_price,
            "current_bid": 0,
            "current_bidder": None,
            "current_bidder_name": None,
            "bids": 0,
            "created_at": now,
            "last_activity": now,
        }

        try:
            message = await ctx.send(view=self.build_auction_view(auction))
        except discord.HTTPException:
            # Put the item back if Discord refused the card.
            await self.store.add_item(
                ctx.author.id, item, price=int(record.get("price_paid") or 0)
            )
            logger.exception("[Vault] Discord rejected an auction card")
            await self.send_error(ctx, "❌ Discord rejected the auction card.")
            return

        auction["message_id"] = message.id
        await self.store.save_auction(auction)

    # ---------------------------------------------------------------
    # ,vault bid
    # ---------------------------------------------------------------

    @vault_group.command(name="bid")
    async def vault_bid(self, ctx: commands.Context, *, amount: str = "") -> None:
        auction = self.store.latest_auction(ctx.channel.id)
        if auction is None:
            await ctx.send(
                view=simple_view(
                    "❌ There's no open auction in this channel. "
                    f"Start one with `{ctx.prefix}vault auction <song> <start price>`."
                )
            )
            return

        if not amount.strip() or not re.sub(r"[,_]", "", amount.strip()).isdigit():
            await ctx.send(
                view=simple_view(f"**Syntax:** `{ctx.prefix}vault bid <amount>`")
            )
            return

        ok, message = await self.place_auction_bid(
            auction["id"],
            ctx.author.id,
            ctx.author.display_name,
            int(re.sub(r"[,_]", "", amount.strip())),
        )
        await ctx.send(view=simple_view(message))
        if ok:
            await self.refresh_auction_message(auction["id"])

    # ---------------------------------------------------------------
    # ,vault auctions
    # ---------------------------------------------------------------

    @vault_group.command(name="auctions", aliases=["auctionlist", "bids"])
    async def vault_auctions(self, ctx: commands.Context) -> None:
        auctions = self.store.list_auctions(
            guild_id=ctx.guild.id if ctx.guild else None
        )
        if not auctions:
            await ctx.send(
                view=simple_view(
                    "📭 No auctions are running right now.\n\n"
                    f"Start one: `{ctx.prefix}vault auction <song> <start price>`"
                )
            )
            return

        now = time.time()
        lines = ["# 🔨 Open Auctions", "-# Ends 30 minutes after the last bid\n"]
        for auction in auctions:
            last_activity = float(
                auction.get("last_activity") or auction.get("created_at") or 0
            )
            remaining = max(0, int(AUCTION_IDLE_SECONDS - (now - last_activity)))
            minutes, seconds = divmod(remaining, 60)
            current_bid = int(auction.get("current_bid") or 0)
            top_bid = (
                f"{format_cash(current_bid)} by {auction.get('current_bidder_name')}"
                if current_bid
                else f"no bids · starts at {format_cash(int(auction.get('start_price') or 0))}"
            )
            lines.append(
                f"**{auction.get('title')}** — {top_bid}\n"
                f"-# seller {auction.get('seller_name')} · ends in {minutes}m {seconds}s"
            )

        lines.append(f"\n-# Bid with `{ctx.prefix}vault bid <amount>` in that channel.")
        await ctx.send(view=simple_view("\n".join(lines)))

    # ---------------------------------------------------------------
    # ,vault market
    # ---------------------------------------------------------------

    @vault_group.command(name="market", aliases=["index", "inflation"])
    async def vault_market(self, ctx: commands.Context) -> None:
        """Show the economy the vault's prices are measured against."""
        await self.ensure_market()
        size = self.store.market_size()
        change = self.store.market_change()
        entry = self.store.market(create=False)
        try:
            anchor = float(entry.get("anchor_size") or 0)
            supply = float(entry.get("supply") or 0)
            circulating = float(entry.get("circulating") or 0)
            treasury = float(entry.get("treasury") or 0)
        except (TypeError, ValueError):
            anchor = supply = circulating = treasury = 0.0

        if change > 1.005:
            arrow, trend = "▲", "inflating"
        elif change < 0.995:
            arrow, trend = "▼", "deflating"
        else:
            arrow, trend = "▬", "steady"

        where = "this server" if current_scope() else "the shared bot-wide economy"
        lines = [
            "# 📈 Vault Market",
            f"**Market size:** {format_cash(size)} — {arrow} `{change:.3f}×` "
            f"since the vault opened, so prices are **{trend}**",
            "",
            f"**Money supply** ({where}): {format_cash(supply)}",
            f"**Circulating:** {format_cash(circulating)} · "
            f"**Treasury:** {format_cash(treasury)}",
            f"-# Anchored at {format_cash(anchor)}",
            "-# Every item freezes a *share* of this market, so its price is "
            "that share of the money members are holding right now: as the "
            "economy mints, collections gain value, and as coins are burned, "
            "they lose it.",
        ]
        await ctx.send(view=simple_view("\n".join(lines)))

    # ---------------------------------------------------------------
    # ,vault achievements
    # ---------------------------------------------------------------

    @vault_group.command(name="achievements", aliases=["ach", "awards", "badges"])
    async def vault_achievements(
        self, ctx: commands.Context, member: Optional[discord.Member] = None
    ) -> None:
        """Vault achievements and their coin rewards."""
        await self.ensure_market()
        user = member or ctx.author
        unlocked = self.store.unlocked_achievements(user.id)
        stats = self.store.vault_stats(user.id)

        lines = [
            f"# 🏆 {user.display_name}'s Vault Achievements",
            f"-# {len(unlocked)}/{len(VAULT_ACHIEVEMENTS)} unlocked · "
            f"collection worth {format_cash(stats.get('portfolio', 0))}",
            "",
        ]
        for key, spec in VAULT_ACHIEVEMENTS.items():
            field, target = VAULT_ACHIEVEMENT_RULES[key]
            have = int(stats.get(field) or 0)
            if key in unlocked:
                lines.append(
                    f"✅ {spec['emoji']} **{spec['title']}** — {spec['description']}"
                )
                continue
            if key == "tycoon":
                # This one is measured against the server's money supply, so the
                # bar is a coin figure rather than a count of items.
                needed = int(self.store.market_size() * TYCOON_MARKET_SHARE)
                progress = (
                    f"{format_cash(int(stats.get('portfolio', 0)))}/"
                    f"{format_cash(needed)}"
                )
            else:
                progress = f"{min(have, target):,}/{target:,}"
            lines.append(
                f"⬜ {spec['emoji']} **{spec['title']}** — {spec['description']}\n"
                f"> {format_cash(achievement_reward(spec['weight'], self.store.market_size()))} "
                f"· progress {progress}"
            )
        lines.append("-# Achievements pay out of the server's own treasury.")
        await ctx.send(view=simple_view("\n".join(lines)))

    # ---------------------------------------------------------------
    # ,vault leaderboard
    # ---------------------------------------------------------------

    @vault_group.command(name="leaderboard", aliases=["lb", "top"])
    async def vault_leaderboard(self, ctx: commands.Context) -> None:
        await self.ensure_market()
        rows = self.store.leaderboard(10)
        if not rows:
            await ctx.send(
                view=simple_view(
                    "📭 No vaults have been built yet.\n\n"
                    f"Be the first: `{ctx.prefix}vault search <song>` then "
                    f"`{ctx.prefix}vault buy <song>`."
                )
            )
            return

        lines = ["# 🏆 Vault Leaderboard", "-# Ranked by total collection value\n"]
        for rank, (user_id, total, count) in enumerate(rows, start=1):
            uid = int(user_id)
            member = ctx.guild.get_member(uid) if ctx.guild else None
            user = member or self.bot.get_user(uid)
            name = user.display_name if user else f"User {user_id}"
            crown = "👑 " if rank == 1 else f"**{rank}.** "
            plural = "item" if count == 1 else "items"
            lines.append(f"{crown}{name} — {format_cash(total)} · {count} {plural}")

        lines.append(
            f"\n-# Check your own value with `{ctx.prefix}vault list`."
        )
        await ctx.send(view=simple_view("\n".join(lines)))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Vault(bot))
