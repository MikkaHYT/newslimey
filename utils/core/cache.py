from __future__ import annotations

from datetime import datetime
from typing import Any


class BotCache:
    def __init__(self, bot: Any = None) -> None:
        self.bot = bot
        self._dict: dict[Any, Any] = {}
        self._rl: dict[Any, int] = {}
        self._delete: dict[Any, dict[str, float]] = {}
        self.sticky_roles: dict[int, list[int]] = {}

    async def sadd(self, key: Any, *values: Any) -> int:
        if key not in self._dict:
            self._dict[key] = set()

        bucket = self._dict[key]
        if not isinstance(bucket, set):
            bucket = set()
            self._dict[key] = bucket

        added = 0
        for value in values:
            if value not in bucket:
                bucket.add(value)
                added += 1
        return added

    async def smembers(self, key: Any) -> tuple[Any, ...]:
        value = self._dict.get(key, set())
        if not isinstance(value, set):
            return ()
        return tuple(value)

    async def srem(self, key: Any, *members: Any) -> int:
        bucket = self._dict.get(key)
        if not isinstance(bucket, set):
            return 0

        removed = 0
        for member in members:
            if member in bucket:
                bucket.remove(member)
                removed += 1

        if not bucket:
            del self._dict[key]

        return removed

    def is_ratelimited(self, key: Any) -> bool:
        parts = str(key).split(":", 1)
        bucket_key = f"{parts[0]}:{hash(parts[1] if len(parts) == 2 else key)}"
        if bucket_key in self._dict and bucket_key in self._rl:
            return self._dict[bucket_key] >= self._rl[bucket_key]
        return False

    async def ratelimited(self, key: str, amount: int, bucket: int) -> float:
        parts = key.split(":", 1) if ":" in key else ("undefined", key)
        bucket_key = f"{parts[0]}:{hash(parts[1])}"

        if bucket_key not in self._dict:
            self._dict[bucket_key] = 1
            self._rl[bucket_key] = amount
            self._delete[bucket_key] = {
                "bucket": bucket,
                "last": datetime.now().timestamp(),
            }
            return 0

        try:
            if self._delete[bucket_key]["last"] + bucket <= datetime.now().timestamp():
                del self._dict[bucket_key]
                self._delete[bucket_key]["last"] = datetime.now().timestamp()
                self._dict[bucket_key] = 0

            self._dict[bucket_key] += 1
            if self._dict[bucket_key] > self._rl[bucket_key]:
                return round(
                    bucket - (datetime.now().timestamp() - self._delete[bucket_key]["last"]),
                    3,
                )
            return 0
        except Exception:
            return await self.ratelimited(key, amount, bucket)

    async def initialize_settings_cache(self) -> None:
        if self.bot is None:
            return

        rows = await self.bot.db.execute("SELECT guild_id, role_id FROM sticky_roles;")
        self.sticky_roles.clear()
        for guild_id, role_id in rows:
            self.sticky_roles.setdefault(guild_id, []).append(role_id)
