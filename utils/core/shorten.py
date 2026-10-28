"""Short links for bucket URLs, via the key-free is.gd API.

The mixtape commands never upload an mp3 any more — they hand out the track's
link on the Cloudflare R2 bucket (see :mod:`utils.core.r2`) instead, and those
keys are long and ugly. This turns one into an ``is.gd`` short link with a
single GET, remembering the answer so a track that has already been handed out
never pays the round trip again.

Shortening is best-effort: if is.gd is unreachable, rate-limits us, or answers
with something that isn't a link, the caller gets the original URL back. A dead
shortener degrades to a plain (still working) link rather than breaking the
command.
"""

from __future__ import annotations

import logging
from collections import OrderedDict

import aiohttp

logger = logging.getLogger("slimey.shorten")

#: is.gd without an API key. ``format=simple`` returns the bare short link as
#: plain text instead of a JSON body.
IS_GD_ENDPOINT = "https://is.gd/create.php"
TIMEOUT = aiohttp.ClientTimeout(total=8)
#: Only links is.gd itself hands back are accepted, so an error page can never
#: be mistaken for a short link.
SHORT_PREFIXES = ("https://is.gd/", "https://v.gd/")
#: How many links to keep. One per track, so this covers a very large library.
CACHE_LIMIT = 4096

_cache: OrderedDict[str, str] = OrderedDict()


async def shorten(url: str) -> str:
    """A short link to ``url``, or ``url`` itself when shortening fails."""
    if not url:
        return url

    cached = _cache.get(url)
    if cached is not None:
        _cache.move_to_end(url)
        return cached

    short = await _request(url)
    if short:
        _cache[url] = short
        _cache.move_to_end(url)
        while len(_cache) > CACHE_LIMIT:
            _cache.popitem(last=False)
        return short
    return url


async def _request(url: str) -> str | None:
    """Ask is.gd for a short link. ``None`` on any failure."""
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            async with session.get(
                IS_GD_ENDPOINT, params={"format": "simple", "url": url}
            ) as response:
                if response.status != 200:
                    logger.warning("is.gd answered HTTP %s", response.status)
                    return None
                text = (await response.text()).strip()
    except Exception:
        logger.warning("is.gd request failed", exc_info=True)
        return None

    if text.startswith(SHORT_PREFIXES):
        return text
    # is.gd reports problems (unknown URL, rate limit) as a plain-text message.
    logger.warning("is.gd did not return a short link: %s", text[:200])
    return None
