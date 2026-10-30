"""One shared aiohttp session for the whole process.

Every ``async with aiohttp.ClientSession()`` inside a command handler pays for a
fresh TCP and TLS handshake before it sees its first byte. Measured on a
simulated 100ms round trip, that is ~108ms of dead time per call, and the media
commands open several in a row while the user watches a "typing..." indicator.

This module hands out one pooled, DNS-caching session instead, so a command's
first request reuses a connection that is already warm. Per-call options
(timeouts, headers) still belong on the individual request.

The session is created lazily on first use and closed by
:func:`close_shared_session` at shutdown. :func:`borrowed` yields it without
closing it, so it drops straight into an ``async with`` at the call site.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import aiohttp

#: Cap on simultaneous connections. 100 is aiohttp's own default, kept explicit
#: so a burst of parallel lookups cannot open an unbounded number of sockets.
CONNECTION_LIMIT = 100
#: How long a resolved host name is reused. This is the part that stops a run of
#: commands against the same few API hosts from re-resolving on every request.
DNS_CACHE_SECONDS = 300

_session: aiohttp.ClientSession | None = None


def shared_session() -> aiohttp.ClientSession:
    """The process-wide session, created on first use (and after a close)."""
    global _session
    session = _session
    if session is None or session.closed:
        session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(
                limit=CONNECTION_LIMIT,
                ttl_dns_cache=DNS_CACHE_SECONDS,
            ),
        )
        _session = session
    return session


@asynccontextmanager
async def borrowed() -> AsyncIterator[aiohttp.ClientSession]:
    """Yield the shared session and leave it open for the next caller.

    A drop-in replacement for ``async with aiohttp.ClientSession() as session``:
    the same shape at the call site, but no handshake and no close.
    """
    yield shared_session()


async def close_shared_session() -> None:
    """Close the shared session, if one was ever created."""
    global _session
    session, _session = _session, None
    if session is not None and not session.closed:
        await session.close()
