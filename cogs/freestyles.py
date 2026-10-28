"""``!freestyle`` / ``!freestyles`` — the Juice WRLD Vault freestyle collection.

The collection is scraped from https://juicewrldvault.com/miscellaneous?type=freestyles
into ``freestyles.json`` by ``scripts/scrape_freestyles.py``. This cog mirrors
:mod:`cogs.stem` and :mod:`cogs.remasters`: the same fuzzy search, the same
dropdown for ambiguous matches, and the same layout card — the only difference
is where the download button points.

* ``!freestyle <query>`` searches and shows the card (or a dropdown when more
  than one thing matches). The card only shows its header until the **Info**
  button is pressed, the same way the ``!leak`` card hides its metadata.
* ``!freestyles [query]`` lists the whole collection as a paginated embed so it
  can be browsed instead of searched.
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

try:
    from config import EMBED_COLOR
except ImportError:
    EMBED_COLOR = discord.Color.from_str("#2b2d31")

logger = logging.getLogger("slimey.freestyles")

#: Freestyle files are not hosted on the JuiceVault API; the site streams them
#: itself through this endpoint. ``song["download_url"]`` carries the full URL.
DOWNLOAD_ENDPOINT = "https://juicewrldvault.com/api/download-song"

CACHE_TTL = 1800
SELECT_TIMEOUT = 120
VIEW_TIMEOUT = 900
MAX_META_LENGTH = 90


def get_freestyles_file_path() -> Path:
    parent_path = Path(__file__).resolve().parent.parent / "freestyles.json"
    if parent_path.is_file():
        return parent_path
    return Path.cwd() / "freestyles.json"


def clean_match_key(value: str) -> str:
    """Normalise a title for comparison, keeping the word "freestyle" intact.

    Unlike the stem/remaster cleaners this deliberately does **not** strip the
    collection name, so searching "freestyle" still matches every row while
    "off the rip" still matches "Off The Rip + Freestyle" by token overlap.
    """
    if not value:
        return ""
    val = str(value)
    val = re.sub(r"\.[a-zA-Z0-9]+$", "", val)
    val = val.replace("&", " and ")
    val = re.sub(r"[_\-./\\+]+", " ", val)
    val = re.sub(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}", "", val)
    val = re.sub(r"\b(?:session|edit|remake)\b", "", val, flags=re.IGNORECASE)
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
        return f"https://media.juicewrldvault.com{path}"
    return f"https://media.juicewrldvault.com/{path}"


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
    song_id = item.get("id")
    if song_id in (None, ""):
        return None
    return f"{DOWNLOAD_ENDPOINT}?id={song_id}"


def build_freestyle_header(item: dict[str, Any]) -> str:
    title = item.get("title") or item.get("file_name") or "Unknown Freestyle"
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


def build_freestyle_details(item: dict[str, Any]) -> str:
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

    added_at = format_iso_date(item.get("archive_added_at"))
    if added_at:
        fields.append(f"**Archive Date**\n{added_at}")

    if file_name:
        fields.append(f"**Available Files**\nFreestyle: {file_name}")

    return "\n\n".join(fields)


class FreestyleView(discord.ui.LayoutView):
    """The freestyle card.

    Shaped like the ``!leak`` card: the header always shows, while the metadata
    block stays collapsed behind an **Info** button until it is pressed, and
    pressing it again hides the block once more.
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
    ) -> None:
        super().__init__(timeout=timeout)
        self.item = item
        self.bot = bot
        self.guild_id = guild_id
        # The Info button belongs to whoever the card was posted for.
        self.author_id = author_id
        self.expanded = expanded

        container = discord.ui.Container(accent_color=EMBED_COLOR)
        header_text = build_freestyle_header(item)
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
            details_text = build_freestyle_details(item)
            if details_text:
                container.add_item(text_display(details_text))
                container.add_item(small_separator())

        row = discord.ui.ActionRow()
        button = mp3_buttons.make_download_button(
            bot,
            guild_id=guild_id,
            label="Download",
            url=download_url(item),
            is_mp3=True,
            filename=mp3_buttons.mp3_filename(
                str(item.get("file_name") or item.get("title") or "freestyle"),
                "freestyle",
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

        container.add_item(text_display("-# Use **!freestyles** to see all freestyles."))

        self.add_item(container)

    async def _on_info(self, interaction: discord.Interaction) -> None:
        """Expand or collapse the card's metadata block in place."""
        if self.author_id is not None and interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Only the person who ran the command can use this button.",
                ephemeral=True,
            )
            return

        await interaction.response.edit_message(
            view=FreestyleView(
                self.item,
                bot=self.bot,
                guild_id=self.guild_id,
                author_id=self.author_id,
                expanded=not self.expanded,
            )
        )


class FreestyleSelect(discord.ui.Select):
    def __init__(
        self, candidates: list[dict[str, Any]], author_id: int, cog: Freestyle
    ) -> None:
        self.candidates = candidates
        self.author_id = author_id
        self.cog = cog

        options = []
        for idx, item in enumerate(candidates[:25]):
            title = str(item.get("title") or item.get("file_name") or "Freestyle")[:100]
            length = item.get("length") or ""
            era = item.get("era") or ""
            desc = f"{length} • {era}" if length and era else (length or era or "Freestyle")
            options.append(
                discord.SelectOption(
                    label=title,
                    value=str(idx),
                    description=desc[:100],
                )
            )

        super().__init__(
            placeholder="Choose the correct freestyle...",
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
            "[FreestyleSelect] Selected: %s (ID: %s)", chosen.get("title"), chosen.get("id")
        )

        try:
            view = FreestyleView(
                chosen,
                bot=self.cog.bot,
                guild_id=interaction.guild_id,
                author_id=self.author_id,
            )
            await interaction.edit_original_response(view=view)
        except discord.HTTPException:
            logger.exception("[FreestyleSelect] Layout view rejected.")
            await self.cog.edit_with_error(interaction, "❌ Discord rejected the freestyle card.")
        except Exception:
            logger.exception("[FreestyleSelect] Failed to render selected freestyle.")
            await self.cog.edit_with_error(interaction, "❌ Failed to build the freestyle card.")


class FreestyleSelectView(discord.ui.LayoutView):
    def __init__(
        self, query: str, candidates: list[dict[str, Any]], author_id: int, cog: Freestyle
    ) -> None:
        super().__init__(timeout=SELECT_TIMEOUT)
        self.selected = False
        self.message: Optional[discord.Message] = None

        container = discord.ui.Container(accent_color=EMBED_COLOR)
        container.add_item(
            text_display(
                f"# Command: freestyle\n"
                f"Found **{len(candidates)}** matching freestyles for **{query}**.\n"
                f"-# Select the correct track from the dropdown below:"
            )
        )
        container.add_item(
            discord.ui.ActionRow(FreestyleSelect(candidates, author_id, cog))
        )
        container.add_item(text_display("-# Use **!freestyles** to see all freestyles."))
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


class JuiceWrldVaultManager:
    def __init__(self) -> None:
        self._freestyles_cache: list[dict[str, Any]] = []
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
            for key in ("freestyles", "songs", "data", "results", "items", "files"):
                val = data.get(key)
                if isinstance(val, list):
                    return [item for item in val if isinstance(item, dict)]
            for val in data.values():
                if isinstance(val, list) and val and isinstance(val[0], dict):
                    return val
        return []

    async def _ensure_index(self, force: bool = False) -> list[dict[str, Any]]:
        now = asyncio.get_running_loop().time()
        if not force and self._freestyles_cache and (now - self._cache_timestamp < CACHE_TTL):
            return self._freestyles_cache

        async with self._cache_lock:
            if not force and self._freestyles_cache and (now - self._cache_timestamp < CACHE_TTL):
                return self._freestyles_cache

            path = get_freestyles_file_path()
            logger.info("[Freestyles] Loading freestyles from disk: %s", path)

            if not path.is_file():
                logger.error("[Freestyles] Local freestyles.json does not exist at: %s", path)
                return self._freestyles_cache

            try:
                data = await asyncio.to_thread(self._read_file_sync, path)
                rows = self.extract_results(data)
                if rows:
                    self._freestyles_cache = rows
                    self._cache_timestamp = now
                    logger.info(
                        "[Freestyles] Loaded %d freestyles from %s", len(rows), path.name
                    )
                else:
                    logger.warning("[Freestyles] %s parsed but yielded 0 items.", path.name)
            except Exception as e:
                logger.exception("[Freestyles] Failed to parse %s: %s", path, e)

        return self._freestyles_cache

    async def all_freestyles(self) -> list[dict[str, Any]]:
        rows = await self._ensure_index()
        if not rows:
            rows = await self._ensure_index(force=True)
        return rows

    async def search_freestyles(self, query: str) -> list[dict[str, Any]]:
        freestyles = await self._ensure_index()
        if not freestyles:
            freestyles = await self._ensure_index(force=True)
            if not freestyles:
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

        for item in freestyles:
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
        # query like "off the rip" has several variants and every one of them
        # belongs in the dropdown so the right one can be picked.
        combined: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        pools = [exact_matches, word_boundary_matches, token_matches, partial_matches]
        # Fuzzy hits are only a fallback: once something matched exactly,
        # near-misses would just clutter the dropdown.
        if not exact_matches:
            pools.append(ordered_fuzzy)

        for pool in pools:
            for item in pool:
                key = str(item.get("id") or item.get("file_name") or id(item))
                if key not in seen_ids:
                    seen_ids.add(key)
                    combined.append(item)

        return combined[:25]


class Freestyle(commands.Cog):
    """Search the Juice WRLD Vault freestyle collection or browse all of it."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.manager = JuiceWrldVaultManager()

    async def edit_with_error(self, interaction: discord.Interaction, message: str) -> None:
        try:
            await interaction.edit_original_response(view=simple_view(message))
        except Exception:
            pass

    @commands.command(
        name="freestyle",
        aliases=["freestylesearch", "vaultfreestyle", "freestyledownload"],
    )
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def freestyle_command(self, ctx: commands.Context, *, query: str | None = None) -> None:
        if not query:
            prefix = await prefix_for(ctx)
            help_view = simple_view(
                "# Command: freestyle\n\n"
                "**Syntax**\n"
                f"`{prefix}freestyle <song>`\n"
                f"`{prefix}freestyles` — browse every freestyle\n\n"
                "**Example**\n"
                f"`{prefix}freestyle off the rip`\n"
                f"`{prefix}freestyle keep save`"
            )
            await ctx.send(view=help_view)
            return

        searching_msg = await ctx.send(
            view=simple_view(f"🔍 Searching freestyles for **{query}**...")
        )

        results = await self.manager.search_freestyles(query)
        if not results:
            await searching_msg.edit(
                view=simple_view(f"❌ Couldn't find any freestyles matching **{query}**.")
            )
            return

        # Several hits means the query is ambiguous, so always hand those to
        # the dropdown instead of silently picking one of them.
        if len(results) == 1:
            chosen = results[0]
            try:
                view = FreestyleView(
                    chosen,
                    bot=self.bot,
                    guild_id=ctx.guild.id if ctx.guild else None,
                    author_id=ctx.author.id,
                )
                await searching_msg.edit(view=view)
            except discord.HTTPException:
                logger.exception("[Command:freestyle] Discord layout rejected.")
                await searching_msg.edit(
                    view=simple_view("❌ Discord rejected the freestyle card.")
                )
            except Exception:
                logger.exception("[Command:freestyle] Failed to build direct card.")
                await searching_msg.edit(
                    view=simple_view("❌ Failed to build the freestyle card.")
                )
            return

        dropdown_view = FreestyleSelectView(
            query=query,
            candidates=results,
            author_id=ctx.author.id,
            cog=self,
        )
        await searching_msg.edit(view=dropdown_view)
        dropdown_view.message = searching_msg

    @commands.command(name="freestyles", aliases=["allfreestyles"])
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def freestyles_browse(self, ctx: commands.Context, *, query: str | None = None) -> None:
        """List every freestyle as a paginated embed (optionally filtered)."""
        rows = await self.manager.all_freestyles()
        if rows and query:
            rows = [row for row in rows if matches_query(row, query)]
        await send_file_browser(
            ctx,
            rows,
            title="🕺 Freestyles",
            query=query,
            empty_message="No freestyles match that filter.",
        )

    @freestyle_command.error
    async def freestyle_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏱️ Try again in **{error.retry_after:.1f}s**.")
            return

        logger.exception("[Command:freestyle] Unhandled command error: %s", error)
        await ctx.send(f"❌ **Freestyle error:** `{error}`")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Freestyle(bot))
