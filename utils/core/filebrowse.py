"""A shared paginated-embed browser for the archive collections.

``!stem`` / ``!remaster`` / ``!freestyle`` answer "find me this one file". The
companion ``!stems`` / ``!remasters`` / ``!freestyles`` commands answer "show me
everything", which is what this module renders: a page of ``discord.Embed``\\ s
with the existing :class:`utils.core.paginator.Paginator` underneath (First /
Prev / Next / Last).

It stays deliberately format-agnostic. A row may carry ``title``/``file_name``,
``length``, ``file_size``, ``era``, ``artist`` and ``alt_names`` — whichever are
present get shown, so the same helper serves every collection.
"""

from __future__ import annotations

from typing import Any, Iterable

import discord

from config import EMBED_COLOR
from utils.core.paginator import Paginator

#: Rows per embed. Twenty keeps every page comfortably under Discord's 4096
#: character description cap even when titles are long.
PER_PAGE = 20

#: Longest title rendered inline before it is truncated with an ellipsis.
MAX_LABEL = 72
MAX_META = 64

#: Fields searched by the optional query filter, in priority order.
SEARCH_FIELDS = ("title", "file_name", "artist", "era", "category")


def _truncate(value: str, limit: int) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _label(item: dict[str, Any]) -> str:
    return _truncate(
        str(item.get("title") or item.get("file_name") or "Unknown"), MAX_LABEL
    )


def _meta(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("length", "file_size", "era"):
        value = item.get(key)
        if value:
            parts.append(str(value))
    size = item.get("file_size_bytes")
    if not item.get("file_size") and size:
        try:
            parts.append(f"{int(size) / (1024 * 1024):.1f} MB")
        except (TypeError, ValueError):
            pass
    return _truncate(" · ".join(parts), MAX_META)


def _alt_names(item: dict[str, Any]) -> list[str]:
    value = item.get("alt_names") or []
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",")]
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(name) for name in value if name]


def matches_query(item: dict[str, Any], query: str) -> bool:
    """Case-insensitive substring match across the row's searchable fields."""
    term = (query or "").strip().lower()
    if not term:
        return True
    haystack = " ".join(str(item.get(key) or "") for key in SEARCH_FIELDS)
    haystack += " " + " ".join(_alt_names(item))
    return term in haystack.lower()


def build_embeds(
    items: Iterable[dict[str, Any]],
    *,
    title: str,
    query: str | None = None,
    per_page: int = PER_PAGE,
) -> list[discord.Embed]:
    """Render *items* into one embed per page."""
    rows = list(items)
    embeds: list[discord.Embed] = []
    pages = max(1, (len(rows) + per_page - 1) // per_page)
    for page in range(pages):
        chunk = rows[page * per_page : (page + 1) * per_page]
        embed = discord.Embed(color=EMBED_COLOR)
        embed.title = title
        if chunk:
            lines = []
            for index, item in enumerate(chunk, start=page * per_page + 1):
                meta = _meta(item)
                line = f"`{index:>3}.` **{_label(item)}**"
                if meta:
                    line += f"\n-# {meta}"
                lines.append(line)
            embed.description = "\n".join(lines)
        else:
            embed.description = "No files match that filter."

        total = len(rows)
        footer = f"Page {page + 1}/{pages} · {total} file{'s' if total != 1 else ''}"
        if query:
            footer = f"Filter: {query} · " + footer
        embed.set_footer(text=footer)
        embeds.append(embed)
    return embeds


async def send_file_browser(
    ctx,
    items: Iterable[dict[str, Any]],
    *,
    title: str,
    query: str | None = None,
    empty_message: str = "No files found.",
    per_page: int = PER_PAGE,
) -> None:
    """Post the browsable list, or *empty_message* when there is nothing to show."""
    rows = list(items)
    if not rows:
        embed = discord.Embed(
            description=empty_message,
            color=EMBED_COLOR,
        )
        await ctx.send(embed=embed)
        return

    embeds = build_embeds(rows, title=title, query=query, per_page=per_page)
    paginator = Paginator(ctx.bot, embeds, ctx, invoker=ctx.author.id)
    await paginator.start()
