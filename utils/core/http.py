from __future__ import annotations

import aiohttp

from utils.core.http_session import borrowed


class HTTP:
    def __init__(self) -> None:
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }

    async def read(self, url: str) -> bytes:
        async with borrowed() as session:
            async with session.get(url, headers=self.headers) as response:
                response.raise_for_status()
                return await response.read()
