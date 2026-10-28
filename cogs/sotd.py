"""Song of the Day: one track a day, chosen by the date it was recorded.

    ,setsotd [#channel] [@role]   point the daily drop at a channel
    ,sotdstyle button|file        link the mp3, or attach it
    ,togglesotd                   turn the automatic drop on or off
    ,sotdstatus                   what this server has configured
    ,sendsotd                     post today's song here and now
    ,sotdsearch <song>            post one specific song from the catalog
    ,postsong [era]               upload an mp3 and post it as a SOTD card

Every command here needs Manage Messages (administrators pass too). `,postsong`
is gated the same way, plus a per-server poster list so staff can hand it to
members who aren't staff: `,admin sotdallow @member` adds someone and
`,admin sotddeny @member` takes them back off. `,postsong` is the manual upload:
the attached mp3 goes to the bucket, its title and cover are read out of the
ID3 tag, and it is posted as the usual card with the era (free text) and a link
button that downloads the mp3 from the bucket — the file itself is never
attached to the post.

This is the reference daily broadcaster ported into a single cog. The catalog
on ``juicewrldapi.com`` is indexed once into ``(month, day, year)`` buckets,
with posthumous eras left out, and today's song is picked by walking the pools
in order: a song recorded on today's date in 2019, else 2018, else 2017, then
the closest date among 2019 records and after that 2018 ones. A twenty-deep
history rules out the
songs that went out recently, and the closest-date pools are walked outward
from today rather than stopping at the single nearest date, so two days in a row
never get the same song. That memory lives in the settings store, so a restart
doesn't reset it. Only when nothing at all is dated does it fall back to a
random track.

The drop runs once a day at ``SOTD_HOUR:SOTD_MINUTE`` (UTC, 00:00 unless the
environment says otherwise) and is shared: the cover — and, when at least one
server asks for the audio, the mp3 — is fetched once and handed to every
server's post. Each server's channel, ping role, on/off switch and format live
in the dashboard settings, so they survive a restart.
"""

from __future__ import annotations

import asyncio
import datetime
import difflib
import html
import io
import json
import logging
import os
import random
import re
import urllib.parse
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

import aiohttp
import discord
from discord.ext import commands, tasks

from config import EMBED_COLOR, PREFIX
from utils.core.id3 import read_tags
from utils.core.r2 import content_disposition
from utils.core.settings import ANTI_MP3_KEY
from utils.core.views import SimpleLayout

logger = logging.getLogger("slimey.sotd")

# ---------------------------------------------------------------------------
# JuiceWRLD API
# ---------------------------------------------------------------------------
API_ROOT = "https://juicewrldapi.com"
API_BASE = f"{API_ROOT}/juicewrld"
JUICEVAULT_BASE = "https://api.juicevault.xyz"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}
TIMEOUT = aiohttp.ClientTimeout(total=45)

MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

#: Discord's usual upload ceiling, and how many titles to remember so the same
#: song doesn't land twice in a row.
MAX_ATTACHMENT_SIZE = 25 * 1024 * 1024
HISTORY_SIZE = 20

#: Audio the manual `postsong` upload accepts.
POST_AUDIO_EXTS = frozenset({".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".aac"})
#: Where an uploaded song lands in the bucket.
POST_DIR = "SOTD"

#: Years a daily song may come from, most favoured first. All three are used for
#: an exact calendar-date match, 2019 first and 2017 last.
RECORDING_YEARS = (2019, 2018, 2017)
#: Years the closest-date fallback may use, so 2017 songs only ever appear on
#: their own date rather than being dragged in from nearby.
NEAREST_YEARS = (2019, 2018)

#: When the drop goes out, in UTC. Overridable per deployment.
SCHEDULE_HOUR = int(os.getenv("SOTD_HOUR", "0") or 0)
SCHEDULE_MINUTE = int(os.getenv("SOTD_MINUTE", "0") or 0)

#: Dashboard settings keys. Kept as plain strings so the whole feature lives in
#: this one file; the settings store itself is generic.
CHANNEL_KEY = "sotd_channel"
ROLE_KEY = "sotd_role"
ENABLED_KEY = "sotd_enabled"
STYLE_KEY = "sotd_style"
#: Bot-wide: the titles that went out recently. Kept in the settings store so a
#: restart can't wipe the memory that stops the same song landing twice.
RECENT_KEY = "sotd_recent"
#: Per-server: members allowed to run `,postsong` on top of staff. Managed with
#: `,admin sotdallow` and `,admin sotddeny`.
POSTERS_KEY = "sotd_posters"

STYLES = ("button", "file")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def song_title(song: dict) -> str:
    """A song's display title, whichever field the catalog filled in."""
    if not isinstance(song, dict):
        return ""
    return str(song.get("name") or song.get("title") or "").strip()


def is_post_era(era_name: str) -> bool:
    """Whether an era is posthumous, which the daily picks skip."""
    if not era_name:
        return False
    norm = era_name.strip().lower()
    if norm in ("post", "post-era", "post era"):
        return True
    return bool(re.search(r"\bpost(?:[- ]?mortem|[- ]?death|humous)?\b", norm))


def extract_date_info(date_text: str) -> tuple[int, int, int | None] | None:
    """``(month, day, year)`` out of the many shapes a record date arrives in."""
    if not date_text:
        return None

    year = None
    year_match = re.search(r"\b(201\d|202\d)\b", date_text)
    if year_match:
        year = int(year_match.group(1))

    # "January 4th", "Jan 4"
    month_first = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|October"
        r"|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
        r"\b\.?\s*(\d{1,2})(?:st|nd|rd|th)?",
        date_text,
        re.IGNORECASE,
    )
    if month_first:
        month = MONTH_MAP.get(month_first.group(1).lower())
        day = int(month_first.group(2))
        if month and 1 <= day <= 31:
            return month, day, year

    # "4th of January"
    day_first = re.search(
        r"(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?\b(January|February|March|April"
        r"|May|June|July|August|September|October|November|December|Jan|Feb|Mar"
        r"|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\b",
        date_text,
        re.IGNORECASE,
    )
    if day_first:
        day = int(day_first.group(1))
        month = MONTH_MAP.get(day_first.group(2).lower())
        if month and 1 <= day <= 31:
            return month, day, year

    # "2019-01-04"
    iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", date_text)
    if iso:
        iso_year, month, day = int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return month, day, iso_year

    return None


def day_distance(month_a: int, day_a: int, month_b: int, day_b: int) -> int:
    """How far apart two calendar dates are, wrapping around the new year."""
    try:
        first = datetime.date(2024, month_a, day_a)
        second = datetime.date(2024, month_b, day_b)
        diff = abs((first - second).days)
        return min(diff, 366 - diff)
    except ValueError:
        return 999


def clean_title(title: str) -> str:
    """Drop brackets, features and production credits from a song title."""
    clean = re.sub(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}", "", title)
    clean = re.sub(r"\s+(?:feat|ft|with|prod)\.?\s+.*$", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"[()\[\]{}]", "", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean or title


def clean_date(raw_date: str) -> str:
    """A record date with the leading label trimmed off."""
    if not raw_date or raw_date == "Unknown Date":
        return "Unknown"
    cleaned = re.sub(
        r"^(Recorded|Released|File Re-exported|First Previewed)[:\s\r\n]*",
        "",
        raw_date,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "Unknown"


def sanitize_filename(name: str) -> str:
    """A song title safe to hand to Discord as an attachment name."""
    name = re.sub(r'[\\/*?:"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:80] or "song"


def parse_api_json(text: str) -> Any:
    """The API sometimes wraps its JSON in an HTML ``<pre>`` block."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    pre_match = re.search(r"<pre[^>]*>(.*?)</pre>", text, re.DOTALL | re.IGNORECASE)
    if pre_match:
        inner = html.unescape(pre_match.group(1).strip())
        return json.loads(inner)

    raise ValueError("Could not parse JSON from the Juice WRLD API")


def audio_attachment(message: discord.Message | None) -> discord.Attachment | None:
    """The first audio attachment on ``message``, or ``None``."""
    for attachment in getattr(message, "attachments", None) or []:
        suffix = Path(getattr(attachment, "filename", "") or "").suffix.lower()
        if suffix in POST_AUDIO_EXTS:
            return attachment
        if (getattr(attachment, "content_type", "") or "").lower().startswith("audio/"):
            return attachment
    return None


def _as_id(value: Any) -> int | None:
    """A channel/role id out of whatever the settings store handed back."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def poster_ids(settings: Any, guild_id: int) -> set[int]:
    """The member ids allowed to run `,postsong` in one server.

    Shared with ``cogs.admin``'s ``,admin sotdallow`` / ``,admin sotddeny``, so
    both sides read the stored list the same way.
    """
    if settings is None:
        return set()
    value = settings.stored(POSTERS_KEY, guild_id=guild_id)
    if not isinstance(value, (list, tuple, set)):
        return set()
    found = {_as_id(item) for item in value}
    return {user_id for user_id in found if user_id is not None}


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------
class JuiceAPI:
    """The daily picker, exactly as the reference broadcaster did it.

    The catalog is indexed once into ``(month, day, year)`` buckets and kept in
    memory for the lifetime of the process, which is what makes the four-pool
    walk below cheap after the first run.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self._era_cache: dict[int, str] = {}
        self._date_song_map: dict[tuple[int, int], list[dict]] = {}
        self._all_dated_songs: list[tuple[int, int, int | None, dict]] = []
        self._indexed_database = False
        self._history_queue: deque[str] = deque(maxlen=HISTORY_SIZE)

    # -- URLs ------------------------------------------------------------
    def get_audio_url(self, path: str) -> str:
        encoded = urllib.parse.quote(path, safe="")
        return f"{API_BASE}/files/download/?path={encoded}"

    def remember(self, title: str) -> None:
        """Note a title that has gone out, so it isn't picked again soon."""
        if title:
            self._history_queue.append(title)

    @property
    def history(self) -> list[str]:
        """Recently used titles, oldest first."""
        return list(self._history_queue)

    def seed_history(self, titles: Iterable[str]) -> None:
        """Restore recently used titles, e.g. after a restart."""
        for title in titles:
            self.remember(str(title))

    async def get_eras(self) -> dict[int, str]:
        """Era id -> name, cached for the life of the process."""
        if self._era_cache:
            return self._era_cache

        try:
            async with self.session.get(
                f"{API_BASE}/eras/", headers=HEADERS, timeout=TIMEOUT
            ) as response:
                if response.status == 200:
                    data = parse_api_json(await response.text())
                    if isinstance(data, list):
                        for era in data:
                            era_id, era_name = era.get("id"), era.get("name")
                            if era_id is not None and era_name:
                                self._era_cache[era_id] = era_name
        except Exception as error:
            logger.warning("[SOTD] Could not load the era cache: %s", error)

        return self._era_cache

    # -- catalog index ---------------------------------------------------
    async def _fetch_page(self, page: int) -> list[dict]:
        try:
            async with self.session.get(
                f"{API_BASE}/songs/?page={page}&page_size=100",
                headers=HEADERS,
                timeout=TIMEOUT,
            ) as response:
                if response.status == 200:
                    data = parse_api_json(await response.text())
                    if isinstance(data, dict):
                        return data.get("results", []) or []
                    if isinstance(data, list):
                        return data
        except Exception:
            pass
        return []

    async def _fetch_song_detail(self, song_id: int | str) -> dict | None:
        try:
            async with self.session.get(
                f"{API_BASE}/songs/{song_id}/", headers=HEADERS, timeout=TIMEOUT
            ) as response:
                if response.status == 200:
                    return parse_api_json(await response.text())
        except Exception:
            pass
        return None

    async def ensure_database_index(self) -> None:
        """Build the ``(month, day)`` index of every dated, non-posthumous song."""
        if self._indexed_database:
            return

        logger.info("[SOTD] Indexing the catalog's recording dates...")
        eras = await self.get_eras()

        total_pages = 28
        try:
            async with self.session.get(
                f"{API_BASE}/songs/?page=1&page_size=100",
                headers=HEADERS,
                timeout=TIMEOUT,
            ) as response:
                if response.status == 200:
                    data = parse_api_json(await response.text())
                    if isinstance(data, dict):
                        count = data.get("count", 0)
                        if count:
                            total_pages = (count // 100) + 1
        except Exception as error:
            logger.warning("[SOTD] Could not read the catalog size: %s", error)

        pages = await asyncio.gather(
            *(self._fetch_page(page) for page in range(1, total_pages + 1)),
            return_exceptions=True,
        )

        for page in pages:
            if not isinstance(page, list):
                continue
            for item in page:
                if not isinstance(item, dict):
                    continue

                era_info = item.get("era")
                if isinstance(era_info, dict):
                    era_name = era_info.get("name", "Unknown Era")
                elif isinstance(era_info, int):
                    era_name = eras.get(era_info, f"Era {era_info}")
                else:
                    era_name = str(era_info) if era_info else "Unknown Era"

                if is_post_era(era_name):
                    continue

                date_str = (
                    item.get("record_dates")
                    or item.get("release_date")
                    or item.get("preview_date")
                    or item.get("dates")
                )
                if not date_str:
                    continue

                date_info = extract_date_info(str(date_str))
                if not date_info:
                    continue

                month, day, year = date_info
                item["_resolved_era"] = era_name
                item["_resolved_date"] = str(date_str).strip()
                item["_resolved_year"] = year

                self._date_song_map.setdefault((month, day), []).append(item)
                self._all_dated_songs.append((month, day, year, item))

        self._indexed_database = True
        logger.info(
            "[SOTD] Indexed %d dated songs across %d calendar dates",
            len(self._all_dated_songs),
            len(self._date_song_map),
        )

    # -- selection -------------------------------------------------------
    def _pick_unseen(self, pool: list[dict]) -> dict | None:
        """A song from ``pool`` that hasn't come up recently, if there is one.

        Returning ``None`` rather than repeating a recent song is what lets the
        caller widen its search. Repeating is only ever reached when a single
        pool holds nothing fresh at all.
        """
        available = [
            song for song in pool or [] if song_title(song) not in self._history_queue
        ]
        return random.choice(available) if available else None

    def _pick_nearest(self, candidates: list[tuple[int, dict]]) -> dict | None:
        """The closest dated song that hasn't been used recently.

        Neighbouring days usually share the same nearest date -- a quiet patch
        of the catalog stays closest for days on end -- so stopping at a single
        distance would hand out that date's songs, one a day, and then start
        over on the first of them. Walking outward one distance at a time means
        tomorrow's show takes the next-closest date that still has something
        fresh, which is what keeps consecutive days apart.
        """
        groups: dict[int, list[dict]] = {}
        for distance, song in candidates:
            groups.setdefault(distance, []).append(song)

        for distance in sorted(groups):
            chosen = self._pick_unseen(groups[distance])
            if chosen:
                if distance:
                    logger.info("[SOTD] Closest unseen date: %d day(s) away", distance)
                return chosen
        return None

    async def get_song_for_today(
        self, target_date: datetime.date | None = None
    ) -> dict:
        """Today's song: an exact 2019 date, else 2018, else 2017, else the
        closest recorded date among 2019 and 2018.
        """
        if target_date is None:
            target_date = datetime.date.today()

        await self.ensure_database_index()

        month, day = target_date.month, target_date.day
        logger.info(
            "[SOTD] Finding a song for %s (exact 2019 -> 2018 -> 2017, "
            "then closest 2019 -> 2018)",
            target_date.strftime("%B %d"),
        )

        exact_pool = self._date_song_map.get((month, day), [])
        chosen = None

        # Songs actually recorded on today's date, newest era first.
        for year in RECORDING_YEARS:
            chosen = self._pick_unseen(
                [song for song in exact_pool if song.get("_resolved_year") == year]
            )
            if chosen:
                logger.info("[SOTD] Exact %d match: %r", year, song_title(chosen))
                break

        # Nothing on today's date, so take the closest one instead -- still
        # newest era first, and 2017 is left out here on purpose.
        if not chosen:
            for year in NEAREST_YEARS:
                chosen = self._pick_nearest(
                    [
                        (day_distance(month, day, m, d), song)
                        for m, d, y, song in self._all_dated_songs
                        if y == year
                    ]
                )
                if chosen:
                    logger.info("[SOTD] Closest %d date", year)
                    break

        # Long shot before a fully random track: the closest date of any year
        # at all. Only reachable if 2019 and 2018 have nothing left to offer.
        if not chosen:
            chosen = self._pick_nearest(
                [
                    (day_distance(month, day, m, d), song)
                    for m, d, _y, song in self._all_dated_songs
                ]
            )
            if chosen:
                logger.warning("[SOTD] Long shot: closest date of any year")

        if not chosen:
            logger.warning(
                "[SOTD] No fresh dated song anywhere; falling back to a random one"
            )
            song = await self.get_random_song()
            self._history_queue.append(song["title"])
            return song

        title = chosen.get("name") or chosen.get("title", "Unknown Title")
        self._history_queue.append(title)

        return {
            "title": title,
            "path": chosen.get("path") or "",
            "era": chosen.get("_resolved_era", "Unknown Era"),
            "date_recorded": chosen.get("_resolved_date") or "Unknown Date",
        }

    async def get_random_song(self) -> dict:
        """A true random track, skipping posthumous eras and recent picks."""
        url = f"{API_BASE}/radio/random/"
        data: dict = {}
        era_name = "Unknown Era"
        recent_pick: dict | None = None

        for _ in range(12):
            async with self.session.get(url, headers=HEADERS, timeout=TIMEOUT) as response:
                response.raise_for_status()
                data = parse_api_json(await response.text())

            song = data.get("song", {}) or {}
            era = song.get("era", {}) or {}
            era_name = era.get("name", "Unknown Era") if isinstance(era, dict) else str(era)
            if is_post_era(era_name):
                continue

            record_dates = song.get("record_dates") or song.get("release_date") or "Unknown Date"
            if isinstance(record_dates, str):
                record_dates = record_dates.strip()

            result = {
                "title": data.get("title") or song.get("name", "Unknown Title"),
                "path": data.get("path") or song.get("path", ""),
                "era": era_name,
                "date_recorded": record_dates,
            }

            # The endpoint takes no filters, so keep drawing until something
            # unused comes back; a recent pick is only kept as a last resort.
            if result["title"] not in self._history_queue:
                return result
            recent_pick = recent_pick or result

        if recent_pick is not None:
            return recent_pick

        return {
            "title": data.get("title") or "Unknown Title",
            "path": data.get("path") or "",
            "era": era_name,
            "date_recorded": "Unknown Date",
        }

    async def get_song_by_name(self, query: str) -> dict | None:
        """The closest song to a free-text search."""
        await self.ensure_database_index()
        eras = await self.get_eras()

        wanted = clean_title(query).strip().lower()
        results: list[dict] = []

        try:
            async with self.session.get(
                f"{API_BASE}/songs/",
                params={"search": query, "page_size": 20},
                headers=HEADERS,
                timeout=TIMEOUT,
            ) as response:
                if response.status != 200:
                    return None
                data = parse_api_json(await response.text())
                if isinstance(data, dict):
                    results = data.get("results", []) or []
                elif isinstance(data, list):
                    results = data
        except Exception as error:
            logger.error("[SOTD] Search failed for %r: %s", query, error)
            return None

        if not results:
            for _m, _d, _y, song in self._all_dated_songs:
                title = (song.get("name") or song.get("title") or "").lower()
                if wanted in title or title in wanted:
                    results.append(song)

        if not results:
            return None

        best: dict | None = None
        best_score = 0.0
        for item in results:
            name = clean_title(item.get("name") or item.get("title") or "").lower()
            if name == wanted:
                best = item
                break
            score = difflib.SequenceMatcher(None, wanted, name).ratio()
            if score > best_score:
                best_score, best = score, item

        best = best or results[0]

        era_info = best.get("era")
        if isinstance(era_info, dict):
            era_name = era_info.get("name", "Unknown Era")
        elif isinstance(era_info, int):
            era_name = eras.get(era_info, f"Era {era_info}")
        else:
            era_name = str(era_info) if era_info else "Unknown Era"

        record_dates = (
            best.get("record_dates")
            or best.get("release_date")
            or best.get("preview_date")
            or "Unknown Date"
        )

        path = best.get("path") or ""
        if not path and best.get("id"):
            detail = await self._fetch_song_detail(best["id"])
            if detail:
                path = detail.get("path", "")

        return {
            "title": best.get("name") or best.get("title", "Unknown Title"),
            "path": path,
            "era": era_name,
            "date_recorded": str(record_dates).strip(),
        }

    # -- media -----------------------------------------------------------
    async def download_cover(
        self, title: str, path: str = ""
    ) -> tuple[io.BytesIO | None, str, str | None]:
        """Cover art: JuiceVault by title first, then the API by file path."""
        target = clean_title(title).strip().lower()
        juicevault_uuid = None

        try:
            async with self.session.get(
                f"{JUICEVAULT_BASE}/music/search",
                params={"q": clean_title(title)},
                headers=HEADERS,
                timeout=TIMEOUT,
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    results = data.get("results", [])
                    best = None

                    for item in results:
                        if clean_title(item.get("title", "")).strip().lower() == target:
                            best = item
                            break

                    if not best:
                        for item in results:
                            stem = re.sub(r"\.[a-zA-Z0-9]+$", "", item.get("file_name", ""))
                            if clean_title(stem).strip().lower() == target:
                                best = item
                                break

                    if not best and results:
                        best_score = 0.0
                        for item in results:
                            candidate = clean_title(item.get("title", "")).strip().lower()
                            score = difflib.SequenceMatcher(None, target, candidate).ratio()
                            if score > best_score:
                                best_score, best = score, item
                        if best_score < 0.60:
                            best = None

                    if best:
                        juicevault_uuid = best.get("id")
                        if juicevault_uuid:
                            async with self.session.get(
                                f"{JUICEVAULT_BASE}/cdn/music/covers/{juicevault_uuid}",
                                headers=HEADERS,
                                timeout=TIMEOUT,
                            ) as cdn:
                                if cdn.status == 200:
                                    payload = await cdn.read()
                                    if len(payload) > 100 and payload[:5] not in {b"<!DOC", b"<html"}:
                                        return io.BytesIO(payload), "cover.webp", juicevault_uuid
        except Exception as error:
            logger.warning("[SOTD] JuiceVault cover search failed: %s", error)

        if path:
            try:
                async with self.session.get(
                    f"{API_BASE}/files/cover-art/",
                    params={"path": path},
                    headers=HEADERS,
                    allow_redirects=True,
                    timeout=TIMEOUT,
                ) as response:
                    if response.status == 200:
                        payload = await response.read()
                        if len(payload) > 100 and payload[:5] not in {b"<!DOC", b"<html"}:
                            return io.BytesIO(payload), "cover.png", juicevault_uuid
            except Exception:
                pass

        return None, "cover.png", juicevault_uuid

    async def download_audio(
        self, path: str, juicevault_uuid: str | None = None
    ) -> io.BytesIO | None:
        """The mp3 itself: the API by file path, else JuiceVault by id."""
        if path:
            try:
                async with self.session.get(
                    f"{API_BASE}/files/download/",
                    params={"path": path},
                    headers=HEADERS,
                    allow_redirects=True,
                    timeout=TIMEOUT,
                ) as response:
                    if response.status == 200:
                        payload = await response.read()
                        if len(payload) > 1000 and payload[:5] not in {b"<!DOC", b"<html"}:
                            return io.BytesIO(payload)
            except Exception as error:
                logger.warning("[SOTD] API audio download failed: %s", error)

        if juicevault_uuid:
            try:
                async with self.session.get(
                    f"{JUICEVAULT_BASE}/music/download/{juicevault_uuid}",
                    headers=HEADERS,
                    allow_redirects=True,
                    timeout=TIMEOUT,
                ) as response:
                    if response.status == 200:
                        payload = await response.read()
                        if len(payload) > 1000 and payload[:5] not in {b"<!DOC", b"<html"}:
                            return io.BytesIO(payload)
            except Exception:
                pass

        return None


# ---------------------------------------------------------------------------
# Per-guild configuration
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class DailyConfig:
    """One server's Song of the Day settings."""

    channel_id: int | None = None
    enabled: bool = False
    role_id: int | None = None
    style: str = "button"



def song_card(
    *,
    title: str,
    subtitle: str,
    cover_name: str | None = None,
    url: str | None = None,
) -> SimpleLayout:
    """The post itself: title, era/date, cover, and a link when there is one."""
    box = discord.ui.Container(accent_color=EMBED_COLOR)
    body = discord.ui.TextDisplay(f"# {title}\n-# {subtitle}")

    if cover_name:
        box.add_item(
            discord.ui.Section(
                body,
                accessory=discord.ui.Thumbnail(media=f"attachment://{cover_name}"),
            )
        )
    else:
        box.add_item(body)

    if url:
        box.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        box.add_item(
            discord.ui.ActionRow(
                discord.ui.Button(
                    label="Listen / Download MP3",
                    emoji="🎵",
                    url=url,
                    style=discord.ButtonStyle.link,
                )
            )
        )
    return SimpleLayout(box)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------
class SongOfTheDay(commands.Cog):
    """Posts a Juice WRLD song to each server once a day."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None
        self.api: JuiceAPI | None = None
        #: One drop at a time, shared by the schedule and the manual command.
        self._lock = asyncio.Lock()

    # -- lifecycle -------------------------------------------------------
    async def cog_load(self) -> None:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        self.api = JuiceAPI(self.session)
        # The "don't repeat" memory outlives the process, so a restart can't
        # reset it and hand out the song that already went out yesterday.
        self.api.seed_history(self._recent_titles())
        # The automatic daily broadcaster is intentionally not started: only the
        # manual ``postsong`` upload from this cog was ported. ``daily_drop``
        # remains defined so ``cog_unload`` can still cancel it safely.

    async def cog_unload(self) -> None:
        self.daily_drop.cancel()
        if self.session is not None and not self.session.closed:
            await self.session.close()
        self.session = None
        self.api = None

    @tasks.loop(time=datetime.time(hour=SCHEDULE_HOUR, minute=SCHEDULE_MINUTE))
    async def daily_drop(self) -> None:
        """Post today's song to every server that wants it."""
        if self._lock.locked():
            logger.warning("[SOTD] Previous drop still running; skipping this cycle")
            return
        async with self._lock:
            await self._broadcast()

    # -- settings --------------------------------------------------------
    def config_for(self, guild_id: int) -> DailyConfig:
        """This server's settings, read from the shared dashboard store."""
        settings = getattr(self.bot, "dash_settings", None)
        if settings is None:
            return DailyConfig()
        style = str(settings.stored(STYLE_KEY, guild_id=guild_id) or "button")
        return DailyConfig(
            channel_id=_as_id(settings.stored(CHANNEL_KEY, guild_id=guild_id)),
            enabled=settings.flag(ENABLED_KEY, False, guild_id=guild_id),
            role_id=_as_id(settings.stored(ROLE_KEY, guild_id=guild_id)),
            style=style if style in STYLES else "button",
        )

    async def _store(self, guild_id: int, key: str, value: Any) -> None:
        settings = getattr(self.bot, "dash_settings", None)
        if settings is not None:
            await settings.set_value(key, value, guild_id=guild_id)

    async def _store_flag(self, guild_id: int, key: str, value: bool) -> None:
        settings = getattr(self.bot, "dash_settings", None)
        if settings is not None:
            await settings.set_flag(key, value, guild_id=guild_id)

    def posters_for(self, guild_id: int) -> set[int]:
        """Members this server has assigned `,postsong`, on top of staff."""
        return poster_ids(getattr(self.bot, "dash_settings", None), guild_id)

    async def may_post_song(self, ctx: commands.Context) -> bool:
        """Whether this author is allowed to run `,postsong`.

        Staff keep the access they have to every other command here, and the
        poster list is how everyone else is handed it.
        """
        if ctx.guild is None:
            return False
        if ctx.author.id in self.posters_for(ctx.guild.id):
            return True
        if getattr(ctx.author, "guild_permissions", None) and ctx.author.guild_permissions.manage_messages:
            return True
        return await self.bot.is_owner(ctx.author)

    def _recent_titles(self) -> list[str]:
        """Titles that went out recently, as remembered by the settings store."""
        settings = getattr(self.bot, "dash_settings", None)
        if settings is None:
            return []
        value = settings.stored(RECENT_KEY)
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value if item]
        return []

    async def _save_history(self) -> None:
        """Persist the picker's memory, so tomorrow's song is a different one."""
        if self.api is None:
            return
        settings = getattr(self.bot, "dash_settings", None)
        if settings is not None:
            await settings.set_value(RECENT_KEY, self.api.history[-HISTORY_SIZE:])

    # -- delivery --------------------------------------------------------
    async def send_song_payload(
        self,
        song: dict,
        target_channel: discord.abc.Messageable,
        *,
        ping_role_id: int | None = None,
        style: str = "button",
        raw_cover: bytes | None = None,
        cover_filename: str = "cover.webp",
        raw_audio: bytes | None = None,
        juicevault_uuid: str | None = None,
    ) -> bool:
        """Post one song.

        ``button`` sends the card with a link to the mp3; ``file`` attaches the
        mp3 itself. Cover bytes and audio are accepted from the caller so a
        daily run fetches them once for every server instead of once per server.
        """
        if self.api is None:
            logger.warning("[SOTD] Not ready yet; skipping a post")
            return False

        if raw_cover is None:
            cover_bytes, cover_filename, found_uuid = await self.api.download_cover(
                song["title"], song.get("path", "")
            )
            raw_cover = cover_bytes.getvalue() if cover_bytes else None
            juicevault_uuid = found_uuid or juicevault_uuid

        download_url = None
        if song.get("path"):
            download_url = self.api.get_audio_url(song["path"])
        elif juicevault_uuid:
            download_url = f"{JUICEVAULT_BASE}/music/download/{juicevault_uuid}"

        subtitle = f"{song.get('era', 'Unknown Era')} · Recorded {clean_date(song.get('date_recorded', ''))}"

        files: list[discord.File] = []
        if raw_cover:
            files.append(discord.File(fp=io.BytesIO(raw_cover), filename=cover_filename))

        # Only the button style links out; the file style hands over the mp3.
        card_url = download_url if style != "file" else None
        card = song_card(
            title=song["title"],
            subtitle=subtitle,
            cover_name=cover_filename if raw_cover else None,
            url=card_url,
        )

        if style == "file":
            if raw_audio is None and (song.get("path") or juicevault_uuid):
                audio = await self.api.download_audio(song.get("path", ""), juicevault_uuid)
                raw_audio = audio.getvalue() if audio else None
            if raw_audio and len(raw_audio) <= MAX_ATTACHMENT_SIZE:
                files.append(
                    discord.File(
                        fp=io.BytesIO(raw_audio),
                        filename=f"{sanitize_filename(song['title'])}.mp3",
                    )
                )

        content = f"<@&{ping_role_id}>" if ping_role_id else None

        try:
            await target_channel.send(content=content, view=card, files=files)
        except discord.HTTPException:
            logger.exception(
                "[SOTD] Discord rejected '%s' for %s",
                song.get("title"),
                getattr(target_channel, "id", target_channel),
            )
            return False

        logger.info(
            "[SOTD] Posted '%s' to %s",
            song.get("title"),
            getattr(target_channel, "name", getattr(target_channel, "id", target_channel)),
        )
        return True

    async def _broadcast(self) -> None:
        """Fetch today's song once and hand it to every subscribed server."""
        if self.api is None:
            return

        try:
            song = await self.api.get_song_for_today()
        except Exception:
            logger.exception("[SOTD] Could not fetch today's song")
            return

        # Remember the pick straight away: if this run dies half way through,
        # tomorrow still gets a different song rather than the same one again.
        await self._save_history()

        cover_bytes, cover_filename, juicevault_uuid = await self.api.download_cover(
            song["title"], song.get("path", "")
        )
        raw_cover = cover_bytes.getvalue() if cover_bytes else None

        configs = {guild.id: self.config_for(guild.id) for guild in self.bot.guilds}
        wants_audio = any(cfg.enabled and cfg.style == "file" for cfg in configs.values())

        raw_audio = None
        if wants_audio and (song.get("path") or juicevault_uuid):
            audio = await self.api.download_audio(song.get("path", ""), juicevault_uuid)
            raw_audio = audio.getvalue() if audio else None

        for guild in list(self.bot.guilds):
            cfg = configs.get(guild.id)
            if cfg is None or not cfg.enabled or not cfg.channel_id:
                continue
            channel = guild.get_channel(cfg.channel_id)
            if channel is None:
                logger.warning("[SOTD] Configured channel %s is gone", cfg.channel_id)
                continue
            asyncio.create_task(
                self.send_song_payload(
                    song,
                    channel,
                    ping_role_id=cfg.role_id,
                    style=cfg.style,
                    raw_cover=raw_cover,
                    cover_filename=cover_filename,
                    raw_audio=raw_audio,
                    juicevault_uuid=juicevault_uuid,
                )
            )

    # -- commands --------------------------------------------------------
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def setsotd(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
        role: Optional[discord.Role] = None,
    ) -> None:
        f"""Point the daily drop at a channel, and switch it on.

        Usage: `{PREFIX}setsotd #songs @ping` — the channel defaults to this
        one, and leaving the role out clears any ping that was set before.
        """
        target = channel or ctx.channel
        if not isinstance(target, discord.TextChannel):
            await ctx.send(view=self._notice("Pick a regular text channel."))
            return

        await self._store(ctx.guild.id, CHANNEL_KEY, target.id)
        await self._store_flag(ctx.guild.id, ENABLED_KEY, True)
        await self._store(ctx.guild.id, ROLE_KEY, role.id if role else None)

        ping = f" with a ping for {role.mention}" if role else " (no role ping)"
        await ctx.send(
            view=self._notice(
                f"Daily Song is set to {target.mention}{ping} and switched **on**.\n"
                f"-# It drops every day at {SCHEDULE_HOUR:02d}:{SCHEDULE_MINUTE:02d} UTC · "
                f"`{PREFIX}togglesotd` pauses it.",
                emoji="📅",
            )
        )

    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def togglesotd(self, ctx: commands.Context) -> None:
        f"""Switch the automatic daily drop on or off.

        Usage: `{PREFIX}togglesotd`.
        """
        cfg = self.config_for(ctx.guild.id)
        if not cfg.channel_id:
            await ctx.send(
                view=self._notice(
                    f"No channel is set yet — run `{PREFIX}setsotd #channel` first."
                )
            )
            return

        enabled = not cfg.enabled
        await self._store_flag(ctx.guild.id, ENABLED_KEY, enabled)
        await ctx.send(
            view=self._notice(
                f"Daily Song posts are now "
                f"{'**on**' if enabled else '**off**'} for this server.",
                emoji="📅",
            )
        )

    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def sotdstyle(self, ctx: commands.Context, style: str = "") -> None:
        f"""Choose whether the drop links the mp3 or attaches it.

        Usage: `{PREFIX}sotdstyle button` posts a card with a download link ·
        `{PREFIX}sotdstyle file` attaches the mp3 to the message.
        """
        wanted = style.strip().lower()
        if wanted in ("embed", "link", "card"):
            wanted = "button"
        elif wanted in ("mp3", "attach", "audio", "upload"):
            wanted = "file"
        if wanted not in STYLES:
            await ctx.send(
                view=self._notice(
                    f"Pick one of `button` (a download link) or `file` "
                    f"(the mp3 attached)."
                )
            )
            return

        await self._store(ctx.guild.id, STYLE_KEY, wanted)
        await ctx.send(
            view=self._notice(
                f"The daily drop will now post as **{wanted}**.", emoji="📅"
            )
        )

    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def sotdstatus(self, ctx: commands.Context) -> None:
        f"""Show this server's Song of the Day settings.

        Usage: `{PREFIX}sotdstatus`.
        """
        cfg = self.config_for(ctx.guild.id)
        channel = f"<#{cfg.channel_id}>" if cfg.channel_id else "*not set*"
        role = f"<@&{cfg.role_id}>" if cfg.role_id else "*none*"
        style = "📁 mp3 attached" if cfg.style == "file" else "🔗 link card"
        state = "🟢 enabled" if cfg.enabled else "🔴 disabled"

        await ctx.send(
            view=self._notice(
                f"**Channel** · {channel}\n"
                f"**Ping role** · {role}\n"
                f"**Automatic** · {state}\n"
                f"**Format** · {style}\n"
                f"**Schedule** · every day at "
                f"{SCHEDULE_HOUR:02d}:{SCHEDULE_MINUTE:02d} UTC",
                emoji="📅",
            )
        )

    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def sendsotd(self, ctx: commands.Context) -> None:
        f"""Post today's song right now.

        Usage: `{PREFIX}sendsotd` — posts to the configured channel, or this one
        when nothing is configured yet.
        """
        cfg = self.config_for(ctx.guild.id)
        target = ctx.guild.get_channel(cfg.channel_id) if cfg.channel_id else None
        target = target or ctx.channel

        if self.api is None:
            await ctx.send(view=self._notice("Still starting up — try again in a moment."))
            return

        status = await ctx.send(view=self._notice("Fetching today's song...", emoji="⏳"))
        try:
            song = await self.api.get_song_for_today()
            async with self._lock:
                sent = await self.send_song_payload(
                    song,
                    target,
                    ping_role_id=cfg.role_id,
                    style=cfg.style,
                )
        except Exception:
            logger.exception("[SOTD] Manual post failed")
            sent = False

        if sent:
            await self._save_history()

        message = (
            f"Posted **{song['title']}** to {target.mention}."
            if sent
            else "That didn't go through — check the logs."
        )
        try:
            await status.edit(view=self._notice(message, emoji="📅" if sent else "❌"))
        except discord.HTTPException:
            pass

    async def _audio_source(
        self, ctx: commands.Context
    ) -> tuple[discord.Attachment | None, discord.Message]:
        """The audio attachment to post, from this message or one it replies to."""
        attachment = audio_attachment(ctx.message)
        if attachment is not None or ctx.message.reference is None:
            return attachment, ctx.message

        resolved = ctx.message.reference.resolved
        if not isinstance(resolved, discord.Message):
            try:
                resolved = await ctx.channel.fetch_message(
                    ctx.message.reference.message_id
                )
            except (discord.NotFound, discord.HTTPException):
                return None, ctx.message
        return audio_attachment(resolved), resolved

    async def _anti_audio_on(self, guild: discord.Guild) -> bool:
        """Whether this server deletes audio files the moment they are posted.

        Two switches can say so: the audio filter's own — the one the
        ``antimp3`` command drives, and the one the filter itself reads — and
        the setting the ported cogs were written against.
        """
        settings = getattr(self.bot, "dash_settings", None)
        if settings is not None and settings.flag(
            ANTI_MP3_KEY, False, guild_id=guild.id
        ):
            return True

        database = getattr(self.bot, "database", None)
        if database is None:
            return False
        try:
            return bool(await database.get_antiaudio_status(guild.id))
        except Exception:
            logger.exception("[SOTD] Could not read the audio filter state")
            return False

    async def _cleanup_source(
        self, ctx: commands.Context, source: discord.Message, *, posted: bool
    ) -> None:
        """Take the uploaded mp3 back down once the command is done with it.

        The audio filter is told to leave `postsong` messages alone — it would
        otherwise delete the attachment before the command could read it — so
        the cleanup happens here, after the file has been read and posted.
        With the filter on the message goes however the attempt turned out;
        with it off, only a successful post takes the attachment off the
        channel. Nothing but a message that actually carries audio is touched.
        """
        if source.guild is None or source.author.id != ctx.author.id:
            return
        if audio_attachment(source) is None:
            return
        if not posted and not await self._anti_audio_on(source.guild):
            return
        try:
            await source.delete()
        except discord.HTTPException:
            pass

    @commands.command(name="postsong", aliases=("sotdpost",))
    @commands.guild_only()
    async def postsong(self, ctx: commands.Context, *, era: str = "") -> None:
        f"""Post an uploaded mp3 as a Song of the Day card.

        Attach an mp3 (or reply to a message carrying one). The file is
        uploaded to the bucket, its title and cover are read from the tag, and
        it is posted with a link button that downloads the mp3 from the bucket
        (the file itself is not attached to the post). Add an era as the first
        word to label it, e.g. `{PREFIX}postsong WOD`.

        Staff with Manage Messages can always use this; everyone else has to be
        put on the poster list with `{PREFIX}admin sotdallow @member`.

        The command is exempt from the audio filter so it can read the file, and
        the message is only cleaned up once the upload has finished — with the
        filter on it goes either way, so nothing is left sitting in the channel.
        """
        if not await self.may_post_song(ctx):
            await ctx.send(
                view=self._notice(
                    f"You're not on the list of members allowed to post songs "
                    f"here. Ask staff to run `{PREFIX}admin sotdallow @you`."
                )
            )
            # The filter steps aside for `postsong`, so an attachment that was
            # never going to be posted still gets taken down.
            await self._cleanup_source(ctx, ctx.message, posted=False)
            return

        cfg = self.config_for(ctx.guild.id)
        target = ctx.guild.get_channel(cfg.channel_id) if cfg.channel_id else None
        target = target or ctx.channel

        client = getattr(getattr(self.bot, "mixtapes", None), "client", None)
        if client is None:
            await ctx.send(
                view=self._notice(
                    "No bucket is configured, so uploaded songs aren't available "
                    "right now."
                )
            )
            await self._cleanup_source(ctx, ctx.message, posted=False)
            return

        attachment, source = await self._audio_source(ctx)
        if attachment is None:
            await ctx.send(
                view=self._notice(
                    f"Attach an mp3 to the command (or reply to a message with "
                    f"one). Usage: `{PREFIX}postsong [era]` with the file attached."
                )
            )
            return

        posted = False
        try:
            size = int(attachment.size or 0)
            if size > MAX_ATTACHMENT_SIZE:
                await ctx.send(
                    view=self._notice(
                        f"That file is **{size / (1024 * 1024):.1f} MB** — the limit is "
                        f"**{MAX_ATTACHMENT_SIZE // (1024 * 1024)} MB**."
                    )
                )
                return

            status = await ctx.send(
                view=self._notice("Uploading and reading the mp3...", emoji="⏳")
            )
            try:
                data = await attachment.read()
            except discord.HTTPException:
                await status.edit(
                    view=self._notice("I couldn't download that attachment.")
                )
                return

            tags = read_tags(data)
            title = tags.title or Path(attachment.filename or "song").stem
            suffix = Path(attachment.filename or "").suffix.lower() or ".mp3"
            filename = f"{sanitize_filename(title)}{suffix}"
            key = f"{POST_DIR}/{filename}"

            uploaded = await client.put(
                key,
                data,
                content_type=attachment.content_type or "audio/mpeg",
                # Stored as an attachment, so the card's link saves the mp3
                # instead of opening a player in a browser tab.
                content_disposition=content_disposition(filename),
            )
            if not uploaded:
                await status.edit(
                    view=self._notice(
                        "The upload to the bucket failed — try again in a moment."
                    )
                )
                return

            url = await client.download_url(key, filename)
            # A link button rejects URLs longer than 512 characters, and takes
            # the whole card with it, so an over-long presigned link is simply
            # dropped.
            card_url = url if url and len(url) <= 512 else None

            label = era.strip()
            # The tag's artist takes the place the uploader used to: it
            # describes the song rather than who happened to post it.
            # ``getattr`` keeps a reloaded cog working against a stale cached
            # id3 module.
            artist = getattr(tags, "artist", "") or "Unknown Artist"
            subtitle = " · ".join(part for part in (label, artist) if part)
            card = song_card(
                title=title,
                subtitle=subtitle,
                cover_name=tags.cover_name if tags.cover else None,
                url=card_url,
            )

            # Only the cover rides along: the song is posted as a download
            # link to the bucket copy, so the mp3 never sits in the channel.
            files: list[discord.File] = []
            if tags.cover:
                files.append(
                    discord.File(io.BytesIO(tags.cover), filename=tags.cover_name)
                )

            try:
                await target.send(view=card, files=files)
            except discord.HTTPException:
                logger.exception("[SOTD] Uploaded post failed")
                await status.edit(view=self._notice("Discord rejected that post."))
                return
            posted = True
        finally:
            # The card links to the bucket copy now, so the message the mp3 was
            # attached to has nothing left to point at. This runs however the
            # attempt turned out: with the audio filter on nothing is left
            # behind, and the file was only ever kept this long so it could be
            # uploaded first.
            await self._cleanup_source(ctx, source, posted=posted)

        message = f"Posted **{title}** to {target.mention}."
        try:
            await status.edit(view=self._notice(message, emoji="📅"))
        except discord.HTTPException:
            pass

    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def sotdsearch(self, ctx: commands.Context, *, query: str = "") -> None:
        f"""Post one specific song from the catalog.

        Usage: `{PREFIX}sotdsearch Lucid Dreams` — searches the catalog and
        posts the closest match to the configured channel.
        """
        if not query.strip():
            await ctx.send(view=self._notice("Give me a song title to search for."))
            return

        cfg = self.config_for(ctx.guild.id)
        target = ctx.guild.get_channel(cfg.channel_id) if cfg.channel_id else None
        target = target or ctx.channel

        if self.api is None:
            await ctx.send(view=self._notice("Still starting up — try again in a moment."))
            return

        status = await ctx.send(view=self._notice(f"Searching for **{query}**...", emoji="⏳"))
        song = await self.api.get_song_by_name(query)
        if not song:
            await status.edit(
                view=self._notice(f"I couldn't find anything matching **{query}**.")
            )
            return

        async with self._lock:
            sent = await self.send_song_payload(
                song,
                target,
                ping_role_id=cfg.role_id,
                style=cfg.style,
            )

        message = (
            f"Posted **{song['title']}** to {target.mention}."
            if sent
            else "That didn't go through — check the logs."
        )
        try:
            await status.edit(view=self._notice(message, emoji="📅" if sent else "❌"))
        except discord.HTTPException:
            pass

    @staticmethod
    def _notice(message: str, *, emoji: str = "📅") -> SimpleLayout:
        box = discord.ui.Container(accent_color=EMBED_COLOR)
        box.add_item(discord.ui.TextDisplay(f"{emoji} {message}"))
        return SimpleLayout(box)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SongOfTheDay(bot))
