"""Every persistent cache the bot writes, and how to rebuild each one.

Several commands hand out media they resolved earlier: a song's snippet videos,
its cover art, the mixtape listing, the lyric search corpus. Each keeps a
manifest on disk so a repeat request -- or the first one after a restart --
never has to touch the network. Deleting the files behind a manifest is
invisible to it, so the bot happily keeps handing out entries that no longer
exist on disk or in the bucket.

Every one of those caches registers a :class:`CacheProvider` here, and
``!cachereload`` walks the registry: each provider forgets its records and
re-resolves them, which re-downloads whatever is missing and re-hosts every
entry. Media files themselves are never deleted, so a reload re-uses what is
still there instead of starting over.

A cog registers on load and removes itself on unload, so a cache owned by a cog
that isn't running can never make the command fail -- it simply isn't listed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("slimey.cache_registry")

#: How a provider reports progress: an awaitable that renders a line of text.
ProgressFn = Callable[[str], Awaitable[None]]


@dataclass
class ReloadResult:
    """What one cache did during a reload, for the summary card."""

    #: Shown on the summary card, e.g. ``"Snippet videos"``.
    label: str
    #: Which records the reload covered, e.g. ``"the cached folders"``.
    scope: str = ""
    #: Records dropped from the cache before re-resolving.
    forgotten: int = 0
    #: Records that came back with content.
    restored: int = 0
    #: Records that re-resolved to nothing (a cached "no result" answer).
    empty: int = 0
    #: Records whose re-resolution raised; details are in the log.
    failed: int = 0
    #: A short reason the reload did nothing or only did part of the job.
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.failed == 0


@dataclass(frozen=True)
class CacheProvider:
    """One rebuildable cache.

    ``reload`` is called with the query the user typed (``None`` for the whole
    cache), whether ``all`` was asked for, and a progress callback:

    * ``target is None`` -- drop every cached record, then re-resolve them.
    * ``target is None, full=True`` -- the same, but over the cache's whole
      catalog rather than only what is cached right now.
    * ``target is "some song"`` -- drop and re-resolve just the records whose
      names match, leaving the rest of the cache alone.
    """

    name: str
    label: str
    describe: Callable[[], str]
    reload: Callable[[Optional[str], bool, ProgressFn], Awaitable[ReloadResult]]
    #: Extra names the command accepts for this cache, e.g. ``cover``.
    aliases: tuple[str, ...] = ()


_providers: dict[str, CacheProvider] = {}


def register(provider: CacheProvider) -> None:
    """Add ``provider`` (or replace the one of the same name)."""
    if provider.name in _providers:
        logger.debug("Cache provider %r was already registered", provider.name)
    _providers[provider.name] = provider


def unregister(name: str) -> None:
    """Drop a provider, e.g. when its cog unloads."""
    _providers.pop(name, None)


def providers() -> list[CacheProvider]:
    """Every registered cache, in registration order."""
    return list(_providers.values())


def get(name: str) -> CacheProvider | None:
    return _providers.get(name)


def match(name: str) -> CacheProvider | None:
    """The provider a command target names, by its id or one of its aliases."""
    key = (name or "").strip().casefold()
    if not key:
        return None
    for provider in _providers.values():
        if key == provider.name or key in provider.aliases:
            return provider
    return None


def summary() -> str:
    """A one-line-per-cache description for the command's opening card."""
    lines = []
    for provider in _providers.values():
        try:
            detail = provider.describe()
        except Exception:
            logger.exception("Could not describe the %s cache", provider.name)
            detail = "unavailable"
        lines.append(f"> **{provider.label}** — {detail}")
    return "\n".join(lines)
