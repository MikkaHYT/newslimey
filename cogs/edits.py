"""``!edit`` / ``!edits`` — the Juice WRLD Vault studio session edits.

The collection is scraped from https://juicewrldvault.com/studio-sessions into
``edits.json`` by ``scripts/scrape_vault_collections.py``. This cog mirrors
:mod:`cogs.stem`, :mod:`cogs.remasters` and :mod:`cogs.freestyles`: the same
fuzzy search, the same dropdown for ambiguous matches, and the same layout card
— the only difference is where the download button points.

* ``!edit <query>`` searches and shows the card (or a dropdown when more than
  one edit matches). The card shows only its header until the **Info** button
  is pressed, the same way the ``!leak`` card hides its metadata.
* ``!edits [query]`` lists the whole collection as a paginated embed so it can
  be browsed instead of searched.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import discord
from discord.ext import commands

from utils.core import mp3_buttons
from utils.core.filebrowse import matches_query, send_file_browser
from utils.core.prefixes import prefix_for
from utils.core.songlookup import SongLookup, build_song_details

try:
    from config import EMBED_COLOR
except ImportError:
    EMBED_COLOR = discord.Color.from_str("#2b2d31")

logger = logging.getLogger("slimey.edits")

#: Session edits are not hosted on the JuiceVault API; the site streams them
#: itself through this endpoint. ``song["download_url"]`` carries the full URL.
DOWNLOAD_ENDPOINT = "https://juicewrldvault.com/api/download-song"
MEDIA_BASE = "https://media.juicewrldvault.com"

CACHE_TTL = 1800
SELECT_TIMEOUT = 120
VIEW_TIMEOUT = 900
MAX_META_LENGTH = 90


def get_edits_file_path() -> Path:
    parent_path = Path(__file__).resolve().parent.parent / "edits.json"
    if parent_path.is_file():
        return parent_path
    return Path.cwd() / "edits.json"


def clean_match_key(value: str) -> str:
    """Normalise a title for comparison.

    Bracketed qualifiers are dropped and so are the collection words, so
    "calm down" reaches "Calm Down [Sessions]" as well as "[Yawaya Session
    Mix]" — which is what puts those variants in the dropdown together.
    """
    if not value:
        return ""
    val = str(value)
    val = re.sub(r"\.[a-zA-Z0-9]+$", "", val)
    val = val.replace("&", " and ")
    val = re.sub(r"[_\-./\\+]+", " ", val)
    val = re.sub(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}", "", val)
    val = re.sub(
        r"\b(?:sessions?|edits?|remakes?|stems?|remasters?)\b", "", val, flags=re.IGNORECASE
    )
    val = re.sub(r"[^\w\s]", "", val)
    val = re.sub(r"\s+", " ", val).strip().lower()
    return val if val else str(value).strip().lower()


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


def resolve_cover(path: str | None) -> str | None:
    if not path or not isinstance(path, str):
        return None
    path = path.strip()
    if path.startswith(("http://", "https://")):
        return path
    if path.startswith("//"):
        return f"https:{path}"
    if path.startswith("/"):
        return f"{MEDIA_BASE}{path}"
    return f"{MEDIA_BASE}/images/{path}"


def format_iso_date(iso_str: str | None) -> str | None:
    if not iso_str or not isinstance(iso_str, str):
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%B %d, %Y")
    except Exception:
        return iso_str.split("T")[0]


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


def make_link_button(label: str, url: str | None) -> discord.ui.Button | None:
    url = valid_http_url(url)
    if not url:
        return None
    return discord.ui.Button(label=label, style=discord.ButtonStyle.link, url=url)


def _alt_names(item: dict[str, Any]) -> list[str]:
    alt_names = item.get("alt_names") or []
    if isinstance(alt_names, str):
        alt_names = [a.strip() for a in alt_names.split(",") if a.strip()]
    if not isinstance(alt_names, (list, tuple)):
        return []
    return [str(a) for a in alt_names if a]


def download_url(item: dict[str, Any]) -> str | None:
    """The file link: the scraped URL, falling back to the site endpoint."""
    url = valid_http_url(item.get("download_url"))
    if url:
        return url
    edit_id = item.get("id")
    if edit_id in (None, ""):
        return None
    return f"{DOWNLOAD_ENDPOINT}?id={edit_id}"


def build_edit_header(item: dict[str, Any]) -> str:
    title = item.get("title") or item.get("file_name") or "Unknown Session Edit"
    lines = [f"### **{title}**"]

    alts = [a for a in _alt_names(item) if a.lower() != str(title).lower()]
    if alts:
        lines.append(f"-# Alt Name(s): **{', '.join(alts)}**")

    artist = item.get("artist")
    if artist:
        lines.append(f"-# Artist: **{artist}**")

    era = item.get("era")
    if era:
        lines.append(f"-# Era: **{era}**")

    return "\n".join(lines)


def build_edit_details(item: dict[str, Any]) -> str:
    fields = []

    file_name = item.get("file_name")
    if file_name:
        fields.append(f"**File Name**\n{file_name}")

    length = item.get("length")
    if not length and item.get("duration_seconds"):
        seconds = int(item["duration_seconds"])
        length = f"{seconds // 60}:{seconds % 60:02d}"
    if length:
        fields.append(f"**Length**\n{length}")

    era = item.get("era")
    if era:
        fields.append(f"**Era**\n{era}")

    category = item.get("subcategory")
    if category:
        fields.append(f"**Category**\n{category}")

    original = item.get("original_file_name")
    if original and original != file_name:
        fields.append(f"**Original File**\n{original}")

    added_at = format_iso_date(item.get("archive_added_at"))
    if added_at:
        fields.append(f"**Archive Date**\n{added_at}")

    plays = item.get("weekly_plays")
    if plays is not None:
        try:
            fields.append(f"**Vault Plays (weekly)**\n{int(plays):,}")
        except (TypeError, ValueError):
            pass

    added_by = item.get("added_by")
    if added_by:
        fields.append(f"**Added By**\n{added_by}")

    if file_name:
        fields.append(f"**Available Files**\nEdit: {file_name}")

    return "\n\n".join(fields)


class EditView(discord.ui.LayoutView):
    """The session edit card.

    Shaped like the ``!leak`` card: the header always shows, while the metadata
    block stays collapsed behind an **Info** button until it is pressed, and
    pressing it again hides the block once more. The opened block is the song
    as the Juice WRLD API knows it, not the edit's own file details.
    """

    def __init__(
        self,
        item: dict[str, Any],
        timeout: int = VIEW_TIMEOUT,
        *,
        bot: Any = None,
        guild_id: int | None = None,
        author_id: int | None = None,
        expanded: bool = False,
        cog: Any = None,
        song_info: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(timeout=timeout)
        self.item = item
        self.bot = bot
        self.guild_id = guild_id
        # The Info button belongs to whoever the card was posted for.
        self.author_id = author_id
        self.expanded = expanded
        # Needed to look the song up in the Juice WRLD API when Info is pressed.
        self.cog = cog
        self.song_info = song_info

        container = discord.ui.Container(accent_color=EMBED_COLOR)
        header_text = build_edit_header(item)
        cover_url = resolve_cover(item.get("cover"))

        if cover_url:
            container.add_item(
                discord.ui.Section(
                    text_display(header_text),
                    accessory=discord.ui.Thumbnail(media=cover_url),
                )
            )
        else:
            container.add_item(text_display(header_text))

        container.add_item(small_separator())

        # Everything past the header is metadata, and it only shows once the
        # Info button has been pressed so the card stays short by default.
        if self.expanded:
            details_text = (
                build_song_details(song_info)
                if song_info
                else build_edit_details(item)
            )
            if details_text:
                container.add_item(text_display(details_text))
                if not song_info:
                    # Only worth saying when the API had nothing and the card
                    # fell back to the vault's own file details.
                    container.add_item(
                        text_display("-# File details from Juice WRLD Vault")
                    )
                container.add_item(small_separator())

        row = discord.ui.ActionRow()
        button = mp3_buttons.make_download_button(
            bot,
            guild_id=guild_id,
            label="Download",
            url=download_url(item),
            is_mp3=True,
            filename=mp3_buttons.mp3_filename(
                str(item.get("file_name") or item.get("title") or "edit"),
                "edit",
            ),
        )
        if button is not None:
            row.add_item(button)

        info_button = discord.ui.Button(
            label="Info",
            emoji="⬆️" if self.expanded else "⬇️",
            style=discord.ButtonStyle.secondary,
        )
        info_button.callback = self._on_info
        row.add_item(info_button)
        container.add_item(row)

        container.add_item(text_display("-# Use **!edits** to see all session edits."))

        self.add_item(container)

    async def _on_info(self, interaction: discord.Interaction) -> None:
        """Expand or collapse the card's metadata block in place."""
        if self.author_id is not None and interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Only the person who ran the command can use this button.",
                ephemeral=True,
            )
            return

        if self.expanded:
            await interaction.response.edit_message(
                view=EditView(
                    self.item,
                    bot=self.bot,
                    guild_id=self.guild_id,
                    author_id=self.author_id,
                    expanded=False,
                    cog=self.cog,
                )
            )
            return

        # Opening the card means asking the Juice WRLD API about the song, so
        # hold the interaction while that round trip happens (results are
        # cached, so pressing Info again is instant).
        await interaction.response.defer()
        song_info = None
        if self.cog is not None:
            title = str(self.item.get("title") or self.item.get("file_name") or "")
            try:
                song_info = await self.cog.song_metadata(title)
            except Exception:
                logger.exception("[Edit] Song lookup failed for %r", title)

        try:
            await interaction.edit_original_response(
                view=EditView(
                    self.item,
                    bot=self.bot,
                    guild_id=self.guild_id,
                    author_id=self.author_id,
                    expanded=True,
                    cog=self.cog,
                    song_info=song_info,
                )
            )
        except discord.HTTPException:
            logger.exception("[Edit] Could not open the metadata block.")


class EditSelect(discord.ui.Select):
    def __init__(
        self, candidates: list[dict[str, Any]], author_id: int, cog: Edits
    ) -> None:
        self.candidates = candidates
        self.author_id = author_id
        self.cog = cog

        options = []
        for idx, item in enumerate(candidates[:25]):
            title = str(item.get("title") or item.get("file_name") or "Edit")[:100]
            length = item.get("length") or ""
            era = item.get("era") or ""
            desc = f"{length} • {era}" if length and era else (length or era or "Session Edit")
            options.append(
                discord.SelectOption(
                    label=title,
                    value=str(idx),
                    description=desc[:100],
                )
            )

        super().__init__(
            placeholder="Choose the correct session edit...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Only the person who ran the command can choose.",
                ephemeral=True,
            )
            return

        if self.view:
            self.view.selected = True
            self.view.stop()

        await interaction.response.defer()
        chosen = self.candidates[int(self.values[0])]
        logger.info(
            "[EditSelect] Selected: %s (ID: %s)", chosen.get("title"), chosen.get("id")
        )

        try:
            view = EditView(
                chosen,
                bot=self.cog.bot,
                guild_id=interaction.guild_id,
                author_id=self.author_id,
                cog=self.cog,
            )
            await interaction.edit_original_response(view=view)
        except discord.HTTPException:
            logger.exception("[EditSelect] Layout view rejected.")
            await self.cog.edit_with_error(interaction, "❌ Discord rejected the edit card.")
        except Exception:
            logger.exception("[EditSelect] Failed to render selected edit.")
            await self.cog.edit_with_error(interaction, "❌ Failed to build the edit card.")


class EditSelectView(discord.ui.LayoutView):
    def __init__(
        self, query: str, candidates: list[dict[str, Any]], author_id: int, cog: Edits
    ) -> None:
        super().__init__(timeout=SELECT_TIMEOUT)
        self.selected = False
        self.message: Optional[discord.Message] = None

        container = discord.ui.Container(accent_color=EMBED_COLOR)
        container.add_item(
            text_display(
                f"# Command: edit\n"
                f"Found **{len(candidates)}** matching session edits for **{query}**.\n"
                f"-# Select the correct track from the dropdown below:"
            )
        )
        container.add_item(discord.ui.ActionRow(EditSelect(candidates, author_id, cog)))
        container.add_item(text_display("-# Use **!edits** to see all session edits."))
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


class JuiceWrldVaultEditManager:
    def __init__(self) -> None:
        self._edits_cache: list[dict[str, Any]] = []
        self._cache_timestamp: float = 0
        self._cache_lock = asyncio.Lock()

    @staticmethod
    def _read_file_sync(path: Path) -> Any:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def extract_results(data: Any) -> list[dict[str, Any]]:
        if not data:
            return []
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("edits", "sessions", "songs", "data", "results", "items", "files"):
                val = data.get(key)
                if isinstance(val, list):
                    return [item for item in val if isinstance(item, dict)]
            for val in data.values():
                if isinstance(val, list) and val and isinstance(val[0], dict):
                    return val
        return []

    async def _ensure_index(self, force: bool = False) -> list[dict[str, Any]]:
        now = asyncio.get_running_loop().time()
        if not force and self._edits_cache and (now - self._cache_timestamp < CACHE_TTL):
            return self._edits_cache

        async with self._cache_lock:
            if not force and self._edits_cache and (now - self._cache_timestamp < CACHE_TTL):
                return self._edits_cache

            path = get_edits_file_path()
            logger.info("[Edits] Loading session edits from disk: %s", path)

            if not path.is_file():
                logger.error("[Edits] Local edits.json does not exist at: %s", path)
                return self._edits_cache

            try:
                data = await asyncio.to_thread(self._read_file_sync, path)
                rows = self.extract_results(data)
                if rows:
                    self._edits_cache = rows
                    self._cache_timestamp = now
                    logger.info("[Edits] Loaded %d session edits from %s", len(rows), path.name)
                else:
                    logger.warning("[Edits] %s parsed but yielded 0 items.", path.name)
            except Exception as e:
                logger.exception("[Edits] Failed to parse %s: %s", path, e)

        return self._edits_cache

    async def all_edits(self) -> list[dict[str, Any]]:
        rows = await self._ensure_index()
        if not rows:
            rows = await self._ensure_index(force=True)
        return rows

    async def search_edits(self, query: str) -> list[dict[str, Any]]:
        edits = await self._ensure_index()
        if not edits:
            edits = await self._ensure_index(force=True)
            if not edits:
                return []

        clean_q = clean_match_key(query)
        raw_q = query.strip().lower()
        if not clean_q and not raw_q:
            return []

        q_tokens = [tok for tok in clean_q.split() if tok]

        exact_matches: list[dict[str, Any]] = []
        word_boundary_matches: list[dict[str, Any]] = []
        token_matches: list[dict[str, Any]] = []
        partial_matches: list[dict[str, Any]] = []
        fuzzy_matches: list[tuple[float, dict[str, Any]]] = []

        for item in edits:
            title = item.get("title") or ""
            file_name = item.get("file_name") or ""
            clean_title = clean_match_key(title)
            clean_file = clean_match_key(file_name)

            alts = _alt_names(item)
            clean_alts = [clean_match_key(a) for a in alts if clean_match_key(a)]

            all_clean = [k for k in (clean_title, clean_file, *clean_alts) if k]
            all_raw = [str(k).lower() for k in (title, file_name, *alts) if k]

            if clean_q in all_clean or raw_q in all_raw:
                exact_matches.append(item)
                continue

            matched_wb = False
            for k in all_clean:
                if re.search(rf"\b{re.escape(clean_q)}\b", k):
                    word_boundary_matches.append(item)
                    matched_wb = True
                    break
            if matched_wb:
                continue

            if q_tokens and all(any(tok in k for k in all_clean) for tok in q_tokens):
                token_matches.append(item)
                continue

            matched_partial = False
            for k in all_clean:
                if (len(clean_q) >= 2 and clean_q in k) or (len(k) >= 2 and k in clean_q):
                    partial_matches.append(item)
                    matched_partial = True
                    break
            if matched_partial:
                continue

            best_score = 0.0
            for k in all_clean:
                score = difflib.SequenceMatcher(None, clean_q, k).ratio()
                if score > best_score:
                    best_score = score

            if best_score >= 0.60:
                fuzzy_matches.append((best_score, item))

        fuzzy_matches.sort(key=lambda x: x[0], reverse=True)
        ordered_fuzzy = [item for _, item in fuzzy_matches]

        # Exact matches come first, but they no longer cut the rest off: a
        # query like "starstruck" has several variants and every one of them
        # belongs in the dropdown so the right one can be picked.
        combined: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        pools = [exact_matches, word_boundary_matches, token_matches, partial_matches]
        # Fuzzy hits are only a fallback: once something matched exactly,
        # near-misses like "Stuck" for "starstruck" would just clutter the
        # dropdown.
        if not exact_matches:
            pools.append(ordered_fuzzy)

        for pool in pools:
            for item in pool:
                key = str(item.get("id") or item.get("file_name") or id(item))
                if key not in seen_ids:
                    seen_ids.add(key)
                    combined.append(item)

        return combined[:25]


class Edits(commands.Cog):
    """Search the Juice WRLD Vault studio session edits or browse all of them."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.manager = JuiceWrldVaultEditManager()
        #: Looks the song itself up on the Juice WRLD API for the Info button.
        self.lookup = SongLookup()

    async def cog_load(self) -> None:
        await self.lookup.start()

    async def cog_unload(self) -> None:
        await self.lookup.close()

    async def song_metadata(self, title: str) -> dict[str, Any] | None:
        """The Juice WRLD API row for the song an edit is of, if it has one."""
        return await self.lookup.find(title)

    async def edit_with_error(self, interaction: discord.Interaction, message: str) -> None:
        try:
            await interaction.edit_original_response(view=simple_view(message))
        except Exception:
            pass

    @commands.command(
        name="edit",
        aliases=["vaultedit", "editsearch", "editdownload"],
    )
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def edit_command(self, ctx: commands.Context, *, query: str | None = None) -> None:
        if not query:
            prefix = await prefix_for(ctx)
            help_view = simple_view(
                "# Command: edit\n\n"
                "**Syntax**\n"
                f"`{prefix}edit <song>`\n"
                f"`{prefix}edits` — browse every session edit\n\n"
                "**Example**\n"
                f"`{prefix}edit calm down`\n"
                f"`{prefix}edit moonlight`"
            )
            await ctx.send(view=help_view)
            return

        searching_msg = await ctx.send(
            view=simple_view(f"🔍 Searching session edits for **{query}**...")
        )

        results = await self.manager.search_edits(query)
        if not results:
            await searching_msg.edit(
                view=simple_view(f"❌ Couldn't find any session edits matching **{query}**.")
            )
            return

        # Several hits means the query is ambiguous, so always hand those to
        # the dropdown instead of silently picking one of them.
        if len(results) == 1:
            chosen = results[0]
            try:
                view = EditView(
                    chosen,
                    bot=self.bot,
                    guild_id=ctx.guild.id if ctx.guild else None,
                    author_id=ctx.author.id,
                    cog=self,
                )
                await searching_msg.edit(view=view)
            except discord.HTTPException:
                logger.exception("[Command:edit] Discord layout rejected.")
                await searching_msg.edit(
                    view=simple_view("❌ Discord rejected the session edit card.")
                )
            except Exception:
                logger.exception("[Command:edit] Failed to build direct card.")
                await searching_msg.edit(
                    view=simple_view("❌ Failed to build the session edit card.")
                )
            return

        dropdown_view = EditSelectView(
            query=query,
            candidates=results,
            author_id=ctx.author.id,
            cog=self,
        )
        await searching_msg.edit(view=dropdown_view)
        dropdown_view.message = searching_msg

    @commands.command(name="edits", aliases=["alledits", "sessionedits"])
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def edits_browse(self, ctx: commands.Context, *, query: str | None = None) -> None:
        """List every session edit as a paginated embed (optionally filtered)."""
        rows = await self.manager.all_edits()
        if rows and query:
            rows = [row for row in rows if matches_query(row, query)]
        await send_file_browser(
            ctx,
            rows,
            title="🎬 Session Edits",
            query=query,
            empty_message="No session edits match that filter.",
        )

    @edit_command.error
    async def edit_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏱️ Try again in **{error.retry_after:.1f}s**.")
            return

        logger.exception("[Command:edit] Unhandled command error: %s", error)
        await ctx.send(f"❌ **Session edit error:** `{error}`")

    @edits_browse.error
    async def edits_browse_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        await self.edit_error(ctx, error)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Edits(bot))
