"""Group buy / personal buy data from the public GB/PB tracker spreadsheet.

The tracker is a published Google Sheet where every row is a track that was
bought in a group buy (GB) or personal buy (PB). A single purchase spans a
*block* of consecutive rows: the price, dates and finished flag live only on the
block's first row (the sheet merges those cells), while each track keeps its own
notes and "surfaced with OG file" flag. Tracks that share a block were sold
together, which is what the card shows as "Sold with".

Nothing here talks to Discord — the parser is a pure function over the CSV text
so it can be exercised without a network or a bot.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
import time
from dataclasses import dataclass, field

import aiohttp

logger = logging.getLogger("discord.client")

#: Public CSV export of the tracker. The ``htmlview``/``pubhtml`` pages need a
#: signed-in browser, but the visualization endpoint serves the first sheet to
#: anyone holding the link.
GROUPBUY_SHEET_ID = "1qWCsoTTGMiXxymTui319zFwMtpZE7a5SYqmybz6mkBY"
GROUPBUY_CSV_URL = (
    f"https://docs.google.com/spreadsheets/d/{GROUPBUY_SHEET_ID}/gviz/tq?tqx=out:csv"
)

#: How long a downloaded copy of the tracker stays fresh before it is re-fetched.
GROUPBUY_CACHE_TTL = 60 * 60

#: Cell markers that only mean "the buyer didn't know what they were buying".
#: The sheet is inconsistent about the brackets: ``(Blind)``, ``[Blind]`` and
#: ``{Blind}`` all appear.
_BLIND_MARKER = re.compile(r"\s*[\(\[\{]\s*blind\s*[\)\]\}]", re.IGNORECASE)

#: Minimum fuzzy ratio before a name is offered as a match. Below this
#: unrelated titles ("Sick", "Slide") leak into the results of a real search.
GROUPBUY_MATCH_THRESHOLD = 0.75
_YEAR_IN_TEXT = re.compile(r"\b(19|20)\d{2}\b")
_NORMALISE = re.compile(r"[^a-z0-9]+")


def _cell(row: list[str], index: int) -> str:
    return (row[index] if len(row) > index else "").strip()


def _parse_bool(value: str) -> bool | None:
    text = (value or "").strip().upper()
    if text in ("TRUE", "YES"):
        return True
    if text in ("FALSE", "NO"):
        return False
    return None


def _strip_date_prefix(value: str, prefix: str) -> str:
    """``"Start Date\\nApril 17, 2025."`` -> ``"April 17, 2025"``."""
    text = (value or "").replace(prefix, "").strip()
    return text.rstrip(".").strip()


def format_price(value: str) -> str:
    """``" $ 15,000.00 "`` -> ``"$15,000"``; anything non-numeric is kept as-is."""
    digits = re.sub(r"[^0-9.]", "", value or "")
    if not digits:
        return (value or "").strip()
    try:
        amount = float(digits)
    except ValueError:
        return (value or "").strip()
    return f"${amount:,.0f}"


def _split_title(cell: str) -> tuple[str, list[str]]:
    """Split the "song" cell into its display name and alternate names.

    The first line is the track; the lines below are the AKA names, separated by
    ``|``. A ``(Blind)``/``[Blind]`` marker is pulled out of the name because it
    is surfaced in the card's Info section instead.
    """
    lines = [line.strip() for line in (cell or "").splitlines() if line.strip()]
    if not lines:
        return "", []
    name = _BLIND_MARKER.sub("", lines[0]).strip()
    akas: list[str] = []
    for line in lines[1:]:
        for part in line.split("|"):
            part = part.strip()
            if part:
                akas.append(part)
    return name, akas


def _is_blind(cell: str) -> bool:
    return bool(_BLIND_MARKER.search(cell or ""))


def _normalise(text: str) -> str:
    return _NORMALISE.sub(" ", (text or "").lower()).strip()


def normalise_song_key(title: str) -> str:
    """A loose key for matching a sheet title against an API song title.

    Parenthetical tags (versions, features, ``(ProTools Session)``) and a
    trailing ``v1``/``v2`` are dropped so ``"Sidekick (v3)"`` and ``"Sidekick"``
    collapse to the same key.
    """
    text = re.sub(r"[\(\[]\s*[^\)\]]*[\)\]]", " ", title or "")
    text = re.sub(r"\bv\d+\b", " ", text, flags=re.IGNORECASE)
    return _normalise(text)


@dataclass
class Groupbuy:
    """One track in the tracker, enriched with the purchase it belonged to."""

    name: str
    era: str
    notes: str = ""
    price: str = ""
    start_date: str = ""
    end_date: str = ""
    finished: bool | None = None
    og_file: bool | None = None
    blind: bool = False
    akas: list[str] = field(default_factory=list)
    year: int | None = None
    block: int = -1
    sold_with: list[str] = field(default_factory=list)

    @property
    def search_text(self) -> str:
        parts = [self.name, self.era, *self.akas]
        if self.year:
            parts.append(str(self.year))
        return _normalise(" ".join(parts))


def parse_groupbuy_csv(text: str) -> list[Groupbuy]:
    """Parse the tracker CSV into :class:`Groupbuy` rows with block context.

    Rows are grouped into blocks (a new block starts whenever a row carries a
    price), section headers such as ``2025 - Groupbuys / Personal Buys`` set the
    current year, and block-level price/date/finished values are copied onto the
    members that don't carry their own.
    """
    rows = list(csv.reader(io.StringIO(text or "")))
    entries: list[Groupbuy] = []
    blocks: list[list[Groupbuy]] = []
    current_year: int | None = None

    for row in rows:
        era = _cell(row, 1)
        song_cell = _cell(row, 3)

        if "Groupbuys / Personal Buys" in song_cell:
            found = _YEAR_IN_TEXT.search(song_cell)
            if found:
                current_year = int(found.group(0))
            continue
        # Real rows are short era codes; anything longer is a summary banner.
        if not era or len(era) > 12:
            continue
        name, akas = _split_title(song_cell)
        if not name:
            continue
        if not row or len(row) < 10:
            continue

        price = _cell(row, 5)
        entry = Groupbuy(
            name=name,
            era=era,
            notes="" if _cell(row, 4) == "-" else _cell(row, 4),
            price=price,
            start_date=_strip_date_prefix(_cell(row, 6), "Start Date"),
            end_date=_strip_date_prefix(_cell(row, 7), "End Date"),
            finished=_parse_bool(_cell(row, 8)),
            og_file=_parse_bool(_cell(row, 9)),
            blind=_is_blind(song_cell),
            akas=akas,
            year=current_year,
        )

        # A row with a price starts a new purchase; rows without one belong to
        # the block above them (their price/dates were merged into that row).
        if price or not blocks:
            blocks.append([])
        entry.block = len(blocks) - 1
        blocks[entry.block].append(entry)
        entries.append(entry)

    for block in blocks:
        if not block:
            continue
        price = next((e.price for e in block if e.price), "")
        start = next((e.start_date for e in block if e.start_date), "")
        end = next((e.end_date for e in block if e.end_date), "")
        finished = next((e.finished for e in block if e.finished is not None), None)
        block_year = next(
            (int(match.group(0)) for e in block if (match := _YEAR_IN_TEXT.search(e.start_date))),
            None,
        )
        for entry in block:
            if not entry.price:
                entry.price = price
            if not entry.start_date:
                entry.start_date = start
            if not entry.end_date:
                entry.end_date = end
            if entry.finished is None:
                entry.finished = finished
            if block_year:
                entry.year = block_year
            entry.sold_with = [other.name for other in block if other is not entry]

    return entries


class GroupbuyCatalog:
    """Fetches and caches the tracker, then answers name/era/year searches."""

    def __init__(self) -> None:
        self._entries: list[Groupbuy] = []
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    def loaded(self) -> bool:
        return bool(self._entries)

    async def load(self, session: aiohttp.ClientSession, *, force: bool = False) -> list[Groupbuy]:
        """The tracker rows, re-downloading when the cached copy is stale."""
        if not force and self._entries and time.time() - self._fetched_at < GROUPBUY_CACHE_TTL:
            return self._entries
        async with self._lock:
            if not force and self._entries and time.time() - self._fetched_at < GROUPBUY_CACHE_TTL:
                return self._entries
            async with session.get(GROUPBUY_CSV_URL) as response:
                if response.status != 200:
                    raise RuntimeError(f"tracker returned HTTP {response.status}")
                text = await response.text()
            entries = parse_groupbuy_csv(text)
            if entries:
                self._entries = entries
                self._fetched_at = time.time()
            return self._entries

    @staticmethod
    def years(entries: list[Groupbuy]) -> list[int]:
        return sorted({e.year for e in entries if e.year}, reverse=True)

    @staticmethod
    def for_year(entries: list[Groupbuy], year: int) -> list[Groupbuy]:
        return [e for e in entries if e.year == year]

    @staticmethod
    def for_era(entries: list[Groupbuy], era: str) -> list[Groupbuy]:
        wanted = _normalise(era)
        return [e for e in entries if _normalise(e.era) == wanted]

    @classmethod
    def search(cls, entries: list[Groupbuy], query: str, limit: int = 25) -> list[Groupbuy]:
        """Best matches for a query that may be a song, era, or year."""
        query = (query or "").strip()
        if not query:
            return []

        if query.isdigit() and len(query) == 4:
            return cls.for_year(entries, int(query))[:limit]

        era_hits = cls.for_era(entries, query)
        if era_hits:
            return era_hits[:limit]

        normalised = _normalise(query)
        scored: list[tuple[float, Groupbuy]] = []
        for entry in entries:
            name = _normalise(entry.name)
            candidates = [name, *(_normalise(aka) for aka in entry.akas)]
            best = 0.0
            for candidate in candidates:
                if not candidate:
                    continue
                if candidate == normalised:
                    best = max(best, 1.0)
                elif candidate.startswith(normalised):
                    best = max(best, 0.95)
                elif normalised in candidate:
                    best = max(best, 0.9)
                else:
                    ratio = _ratio(normalised, candidate)
                    if ratio > best:
                        best = ratio
            if best >= GROUPBUY_MATCH_THRESHOLD:
                scored.append((best, entry))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [entry for _, entry in scored[:limit]]


def _ratio(left: str, right: str) -> float:
    from difflib import SequenceMatcher

    return SequenceMatcher(None, left, right).ratio()


#: Shared catalog instance so every cog sees the same cached copy.
CATALOG = GroupbuyCatalog()
