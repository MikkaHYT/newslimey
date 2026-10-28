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

logger = logging.getLogger("slimey.stems")

JUICEVAULT_BASE = "https://api.juicevault.xyz"
DOWNLOAD_ENDPOINT = f"{JUICEVAULT_BASE}/music/download"

CACHE_TTL = 1800
SELECT_TIMEOUT = 120
VIEW_TIMEOUT = 900
MAX_META_LENGTH = 90


def get_stems_file_path() -> Path:
    # Looks one level up from the current file's folder (i.e. ../stems.json)
    parent_path = Path(__file__).resolve().parent.parent / "stems.json"
    if parent_path.is_file():
        return parent_path
    # Fallback to current working directory
    return Path.cwd() / "stems.json"


def clean_match_key(value: str) -> str:
    if not value:
        return ""
    val = str(value)
    val = re.sub(r"\.[a-zA-Z0-9]+$", "", val)
    val = val.replace("&", " and ")
    val = re.sub(r"[_\-./\\+]+", " ", val)
    val = re.sub(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}", "", val)
    val = re.sub(r"\b(?:stems?|edit|session|remake)\b", "", val, flags=re.IGNORECASE)
    val = re.sub(r"[^\w\s]", "", val)
    val = re.sub(r"\s+", " ", val).strip().lower()
    return val if val else str(value).strip().lower()


def truncate(value: str, limit: int = MAX_META_LENGTH) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


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


def resolve_vault_cover(path: str | None) -> str | None:
    if not path or not isinstance(path, str):
        return None
    path = path.strip()
    if path.startswith(("http://", "https://")):
        return path
    if path.startswith("//"):
        return f"https:{path}"
    if path.startswith("/"):
        return f"{JUICEVAULT_BASE}{path}"
    return f"{JUICEVAULT_BASE}/{path}"


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


def download_url(stem: dict[str, Any]) -> str | None:
    """The file link.

    Rows scraped from juicewrldvault.com carry their own ``download_url``
    because those files are not on the JuiceVault API; rows from that API only
    have an id, which is served from the download endpoint.
    """
    url = valid_http_url(stem.get("download_url"))
    if url:
        return url
    stem_id = stem.get("id")
    if stem_id in (None, ""):
        return None
    return f"{DOWNLOAD_ENDPOINT}/{stem_id}"


def build_stem_header(stem: dict[str, Any]) -> str:
    title = stem.get("title") or stem.get("file_name") or "Unknown Stem"
    lines = [f"### **{title}**"]

    alt_names = stem.get("alt_names") or []
    if isinstance(alt_names, str):
        alt_names = [a.strip() for a in alt_names.split(",") if a.strip()]
    if alt_names:
        clean_alts = [a for a in alt_names if a.lower() != title.lower()]
        if clean_alts:
            lines.append(f"-# Alt Name(s): **{', '.join(clean_alts)}**")

    artist = stem.get("artist")
    if artist:
        lines.append(f"-# Artist: **{artist}**")

    category = stem.get("category")
    if category:
        lines.append(f"-# Category: **{str(category).title()}**")

    return "\n".join(lines)


def build_stem_details(stem: dict[str, Any]) -> str:
    fields = []

    file_name = stem.get("file_name")
    if file_name:
        fields.append(f"**File Name**\n{file_name}")

    length = stem.get("length")
    if length:
        fields.append(f"**Length**\n{length}")

    file_size = stem.get("file_size")
    if file_size:
        fields.append(f"**File Size**\n{file_size}")

    if stem.get("is_session_edit"):
        fields.append("**Type**\nStudio Session Edit")

    added_at = format_iso_date(stem.get("archive_added_at"))
    if added_at:
        fields.append(f"**Vault Archive Date**\n{added_at}")

    play_count = stem.get("play_count")
    if play_count is not None:
        fields.append(f"**Vault Plays**\n{play_count:,}")

    if file_name:
        fields.append(f"**Available Files**\nStem: {file_name}")

    return "\n\n".join(fields)


class StemView(discord.ui.LayoutView):
    """The stem card.

    Shaped like the ``!leak`` card: the header always shows, while the metadata
    block stays collapsed behind an **Info** button until it is pressed, and
    pressing it again hides the block once more.
    """

    def __init__(
        self,
        stem: dict[str, Any],
        timeout: int = VIEW_TIMEOUT,
        *,
        bot: Any = None,
        guild_id: int | None = None,
        author_id: int | None = None,
        expanded: bool = False,
    ) -> None:
        super().__init__(timeout=timeout)
        self.stem = stem
        self.bot = bot
        self.guild_id = guild_id
        # The Info button belongs to whoever the card was posted for.
        self.author_id = author_id
        self.expanded = expanded

        container = discord.ui.Container(accent_color=EMBED_COLOR)
        header_text = build_stem_header(stem)
        cover_url = resolve_vault_cover(stem.get("cover"))

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
            details_text = build_stem_details(stem)
            if details_text:
                container.add_item(text_display(details_text))
                container.add_item(small_separator())

        row = discord.ui.ActionRow()
        dl_url = download_url(stem)
        if dl_url:
            title = stem.get("title") or stem.get("file_name") or "stem"
            download_btn = mp3_buttons.make_download_button(
                bot,
                guild_id=guild_id,
                label="Download",
                url=dl_url,
                is_mp3=True,
                filename=mp3_buttons.mp3_filename(
                    str(stem.get("file_name") or title), "stem"
                ),
            )
            if download_btn:
                row.add_item(download_btn)

        info_button = discord.ui.Button(
            label="Info",
            emoji="⬆️" if self.expanded else "⬇️",
            style=discord.ButtonStyle.secondary,
        )
        info_button.callback = self._on_info
        row.add_item(info_button)
        container.add_item(row)

        container.add_item(text_display("-# Use **!stems** to see all stems."))

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
            view=StemView(
                self.stem,
                bot=self.bot,
                guild_id=self.guild_id,
                author_id=self.author_id,
                expanded=not self.expanded,
            )
        )


class StemSelect(discord.ui.Select):
    def __init__(self, candidates: list[dict[str, Any]], author_id: int, cog: Stem) -> None:
        self.candidates = candidates
        self.author_id = author_id
        self.cog = cog

        options = []
        for idx, item in enumerate(candidates[:25]):
            title = str(item.get("title") or item.get("file_name") or "Stem")[:100]
            size = item.get("file_size") or ""
            length = item.get("length") or ""
            desc = f"{length} • {size}" if length and size else (length or size or "JuiceVault Stem")

            options.append(
                discord.SelectOption(
                    label=title,
                    value=str(idx),
                    description=desc[:100],
                )
            )

        super().__init__(
            placeholder="Choose the correct stem...",
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
        logger.info("[StemSelect] Selected: %s (ID: %s)", chosen.get("title"), chosen.get("id"))

        try:
            view = StemView(
                chosen,
                bot=self.cog.bot,
                guild_id=interaction.guild_id,
                author_id=self.author_id,
            )
            await interaction.edit_original_response(view=view)
        except discord.HTTPException:
            logger.exception("[StemSelect] Layout view rejected.")
            await self.cog.edit_with_error(interaction, "❌ Discord rejected the stem card.")
        except Exception:
            logger.exception("[StemSelect] Failed to render selected stem.")
            await self.cog.edit_with_error(interaction, "❌ Failed to build the stem card.")


class StemSelectView(discord.ui.LayoutView):
    def __init__(self, query: str, candidates: list[dict[str, Any]], author_id: int, cog: Stem) -> None:
        super().__init__(timeout=SELECT_TIMEOUT)
        self.selected = False
        self.message: Optional[discord.Message] = None

        container = discord.ui.Container(accent_color=EMBED_COLOR)
        container.add_item(
            text_display(
                f"# Command: stem\n"
                f"Found **{len(candidates)}** matching stems for **{query}**.\n"
                f"-# Select the correct track from the dropdown below:"
            )
        )
        container.add_item(discord.ui.ActionRow(StemSelect(candidates, author_id, cog)))
        container.add_item(text_display("-# Use **!stems** to see all stems."))
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


class JuiceVaultManager:
    def __init__(self) -> None:
        self._stems_cache: list[dict[str, Any]] = []
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
            for key in ("stems", "data", "songs", "results", "items", "files"):
                val = data.get(key)
                if isinstance(val, list):
                    return [item for item in val if isinstance(item, dict)]
            for val in data.values():
                if isinstance(val, list) and val and isinstance(val[0], dict):
                    return val
        return []

    async def _ensure_stems_index(self, force: bool = False) -> list[dict[str, Any]]:
        now = asyncio.get_running_loop().time()
        if not force and self._stems_cache and (now - self._cache_timestamp < CACHE_TTL):
            return self._stems_cache

        async with self._cache_lock:
            if not force and self._stems_cache and (now - self._cache_timestamp < CACHE_TTL):
                return self._stems_cache

            path = get_stems_file_path()
            logger.info("[JuiceVault] Loading stems from disk: %s", path)

            if not path.is_file():
                logger.error("[JuiceVault] Local stems.json file does not exist at: %s", path)
                return self._stems_cache

            try:
                data = await asyncio.to_thread(self._read_file_sync, path)
                stems = self.extract_results(data)
                if stems:
                    self._stems_cache = stems
                    self._cache_timestamp = now
                    logger.info("[JuiceVault] Successfully loaded %d stems from %s", len(stems), path.name)
                else:
                    logger.warning("[JuiceVault] %s parsed successfully but yielded 0 items.", path.name)
            except Exception as e:
                logger.exception("[JuiceVault] Failed to parse %s: %s", path, e)

        return self._stems_cache

    async def all_stems(self) -> list[dict[str, Any]]:
        """Every stem, for the paginated ``!stems`` browser."""
        stems = await self._ensure_stems_index()
        if not stems:
            stems = await self._ensure_stems_index(force=True)
        return stems

    async def search_stems(self, query: str) -> list[dict[str, Any]]:
        stems = await self._ensure_stems_index()
        if not stems:
            stems = await self._ensure_stems_index(force=True)
            if not stems:
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

        for stem in stems:
            title = stem.get("title") or ""
            file_name = stem.get("file_name") or ""
            clean_title = clean_match_key(title)
            clean_file = clean_match_key(file_name)

            alt_names = stem.get("alt_names") or []
            if isinstance(alt_names, str):
                alt_names = [a.strip() for a in alt_names.split(",") if a.strip()]
            clean_alts = [clean_match_key(a) for a in alt_names if clean_match_key(a)]

            all_clean = [k for k in (clean_title, clean_file, *clean_alts) if k]
            all_raw = [str(k).lower() for k in (title, file_name, *alt_names) if k]

            if clean_q in all_clean or raw_q in all_raw:
                exact_matches.append(stem)
                continue

            matched_wb = False
            for k in all_clean:
                if re.search(rf"\b{re.escape(clean_q)}\b", k):
                    word_boundary_matches.append(stem)
                    matched_wb = True
                    break
            if matched_wb:
                continue

            if q_tokens and all(any(tok in k for k in all_clean) for tok in q_tokens):
                token_matches.append(stem)
                continue

            matched_partial = False
            for k in all_clean:
                if (len(clean_q) >= 2 and clean_q in k) or (len(k) >= 2 and k in clean_q):
                    partial_matches.append(stem)
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
                fuzzy_matches.append((best_score, stem))

        fuzzy_matches.sort(key=lambda x: x[0], reverse=True)
        ordered_fuzzy = [item for _, item in fuzzy_matches]

        # Exact matches come first, but they no longer cut the rest off: a
        # query like "netflix & pills" has several variants and every one of
        # them belongs in the dropdown so the right one can be picked.
        combined: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        pools = [exact_matches, word_boundary_matches, token_matches, partial_matches]
        # Fuzzy hits are only a fallback: once something matched exactly,
        # near-misses would just clutter the dropdown.
        if not exact_matches:
            pools.append(ordered_fuzzy)

        for pool in pools:
            for s in pool:
                s_id = str(s.get("id") or s.get("file_name") or id(s))
                if s_id not in seen_ids:
                    seen_ids.add(s_id)
                    combined.append(s)

        return combined[:25]


class Stem(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.manager = JuiceVaultManager()

    async def send_error(self, ctx: commands.Context, message: str) -> None:
        await ctx.send(view=simple_view(message))

    async def edit_with_error(self, interaction: discord.Interaction, message: str) -> None:
        try:
            await interaction.edit_original_response(view=simple_view(message))
        except Exception:
            pass

    @commands.command(name="stem", aliases=["vaultstem", "stemdownload"])
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def stem_command(self, ctx: commands.Context, *, query: str | None = None) -> None:
        if not query:
            prefix = await prefix_for(ctx)
            help_view = simple_view(
                "# Command: stem\n\n"
                "**Syntax**\n"
                f"`{prefix}stem <song>`\n\n"
                "**Example**\n"
                f"`{prefix}stem 20`\n"
                f"`{prefix}stem netflix & pills`"
            )
            await ctx.send(view=help_view)
            return

        searching_msg = await ctx.send(view=simple_view(f"🔍 Searching JuiceVault stems for **{query}**..."))

        results = await self.manager.search_stems(query)
        if not results:
            await searching_msg.edit(
                view=simple_view(f"❌ Couldn't find any stems matching **{query}**.")
            )
            return

        # Several hits means the query is ambiguous, so always hand those to
        # the dropdown instead of silently picking one of them.
        if len(results) == 1:
            chosen = results[0]
            try:
                view = StemView(
                    chosen,
                    bot=self.bot,
                    guild_id=ctx.guild.id if ctx.guild else None,
                    author_id=ctx.author.id,
                )
                await searching_msg.edit(view=view)
            except discord.HTTPException:
                logger.exception("[Command:stem] Discord layout rejected.")
                await searching_msg.edit(view=simple_view("❌ Discord rejected the stem card."))
            except Exception:
                logger.exception("[Command:stem] Failed to build direct stem card.")
                await searching_msg.edit(view=simple_view("❌ Failed to build the stem card."))
            return

        dropdown_view = StemSelectView(
            query=query,
            candidates=results,
            author_id=ctx.author.id,
            cog=self,
        )
        await searching_msg.edit(view=dropdown_view)
        dropdown_view.message = searching_msg

    @commands.command(name="stems", aliases=["allstems"])
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def stems_browse(self, ctx: commands.Context, *, query: str | None = None) -> None:
        """List every stem as a paginated embed (optionally filtered)."""
        rows = await self.manager.all_stems()
        if rows and query:
            rows = [row for row in rows if matches_query(row, query)]
        await send_file_browser(
            ctx,
            rows,
            title="🎛️ Stems",
            query=query,
            empty_message="No stems match that filter.",
        )

    @stem_command.error
    async def stem_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏱️ Try again in **{error.retry_after:.1f}s**.")
            return

        logger.exception("[Command:stem] Unhandled command error: %s", error)
        await ctx.send(f"❌ **Stem error:** `{error}`")

    @stems_browse.error
    async def stems_browse_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        await self.stem_error(ctx, error)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Stem(bot))
