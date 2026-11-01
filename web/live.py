"""Live events for the dashboard.

Discord events arrive on the bot's gateway connection, not on the dashboard's
own HTTP server, so this module listens on the bot and pushes JSON down every
open dashboard websocket. The front end filters by guild/channel, which keeps
the server side stateless: no subscriptions to expire or leak.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import discord
from aiohttp import WSMsgType, web

from web.serializers import (
    channel_payload,
    member_payload,
    message_payload,
    user_payload,
)

logger = logging.getLogger("slimey.dashboard.live")

#: Events that make the chat channel list stale.
_CHANNEL_EVENTS = (
    "on_guild_channel_create",
    "on_guild_channel_delete",
    "on_guild_channel_update",
)
_MEMBER_EVENTS = (
    "on_member_join",
    "on_member_remove",
    "on_member_update",
)


class LiveHub:
    """Fan-out of gateway events to connected dashboard browsers."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.sockets: set[web.WebSocketResponse] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- connections ------------------------------------------------------
    async def add(self, ws: web.WebSocketResponse) -> None:
        self.sockets.add(ws)
        try:
            await ws.send_json(
                {
                    "type": "hello",
                    "bot": {
                        "id": str(self.bot.user.id) if self.bot.user else None,
                        "name": str(self.bot.user) if self.bot.user else None,
                    },
                }
            )
        except (ConnectionResetError, RuntimeError):
            self.sockets.discard(ws)

    def remove(self, ws: web.WebSocketResponse) -> None:
        self.sockets.discard(ws)

    def broadcast(self, payload: dict[str, Any]) -> None:
        """Queue *payload* for every client, from sync or async contexts."""
        if not self.sockets:
            return
        try:
            self._loop = self._loop or asyncio.get_running_loop()
        except RuntimeError:
            self._loop = getattr(self, "_loop", None)
        if self._loop is None or self._loop.is_closed():
            return
        try:
            self._loop.create_task(self._send_all(payload))
        except RuntimeError:
            # The event loop is shutting down; dropping a live update is fine.
            pass

    async def _send_all(self, payload: dict[str, Any]) -> None:
        if not self.sockets:
            return
        text = json.dumps(payload, default=str)
        dead: list[web.WebSocketResponse] = []
        for ws in list(self.sockets):
            try:
                await ws.send_str(text)
            except (ConnectionResetError, RuntimeError, asyncio.CancelledError):
                dead.append(ws)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.sockets.discard(ws)

    # -- websocket endpoint ----------------------------------------------
    async def websocket(self, request: web.Request) -> web.WebSocketResponse:
        # Belt and braces: the auth middleware already rejects unauthenticated
        # upgrades, but a direct call must not open an unaudited feed either.
        auth = request.app.get("auth")
        if auth is not None and not auth.authenticated(request):
            raise web.HTTPUnauthorized(text="Not signed in")
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=1024 * 64)
        await ws.prepare(request)
        await self.add(ws)
        try:
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(message.data)
                    except ValueError:
                        continue
                    if isinstance(data, dict) and data.get("type") == "ping":
                        await ws.send_json({"type": "pong"})
                elif message.type in (WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSED):
                    break
        finally:
            self.remove(ws)
        return ws

    # -- gateway listeners -------------------------------------------------
    def install(self) -> None:
        """Attach listeners to the bot; safe to call once per process."""
        bot = self.bot

        async def on_message(message: discord.Message) -> None:
            if message.guild is None:
                return
            self.broadcast(
                {
                    "type": "message",
                    "guild_id": str(message.guild.id),
                    "channel_id": str(message.channel.id),
                    "message": message_payload(message),
                }
            )

        async def on_message_edit(before: discord.Message, after: discord.Message) -> None:
            if after.guild is None:
                return
            self.broadcast(
                {
                    "type": "message_edit",
                    "guild_id": str(after.guild.id),
                    "channel_id": str(after.channel.id),
                    "message": message_payload(after),
                }
            )

        async def on_message_delete(message: discord.Message) -> None:
            if message.guild is None:
                return
            self.broadcast(
                {
                    "type": "message_delete",
                    "guild_id": str(message.guild.id),
                    "channel_id": str(message.channel.id),
                    "message_id": str(message.id),
                }
            )

        async def on_bulk_message_delete(messages: list[discord.Message]) -> None:
            if not messages:
                return
            first = messages[0]
            if first.guild is None:
                return
            self.broadcast(
                {
                    "type": "messages_bulk_delete",
                    "guild_id": str(first.guild.id),
                    "channel_id": str(first.channel.id),
                    "message_ids": [str(m.id) for m in messages],
                }
            )

        async def on_typing(channel: Any, user: Any, when: Any) -> None:
            guild = getattr(channel, "guild", None)
            if guild is None:
                return
            self.broadcast(
                {
                    "type": "typing",
                    "guild_id": str(guild.id),
                    "channel_id": str(channel.id),
                    "user": user_payload(user),
                }
            )

        async def on_member_join(member: discord.Member) -> None:
            self.broadcast(
                {
                    "type": "member_join",
                    "guild_id": str(member.guild.id),
                    "member": member_payload(member),
                }
            )

        async def on_member_remove(member: discord.Member) -> None:
            self.broadcast(
                {
                    "type": "member_remove",
                    "guild_id": str(member.guild.id),
                    "member_id": str(member.id),
                }
            )

        async def on_member_update(before: discord.Member, after: discord.Member) -> None:
            self.broadcast(
                {
                    "type": "member_update",
                    "guild_id": str(after.guild.id),
                    "member": member_payload(after),
                }
            )

        async def on_guild_channel_create(channel: Any) -> None:
            self.broadcast(
                {
                    "type": "channels_changed",
                    "guild_id": str(channel.guild.id),
                    "channel": channel_payload(channel),
                }
            )

        async def on_guild_channel_delete(channel: Any) -> None:
            self.broadcast(
                {
                    "type": "channels_changed",
                    "guild_id": str(channel.guild.id),
                    "deleted": str(channel.id),
                }
            )

        async def on_guild_channel_update(before: Any, after: Any) -> None:
            self.broadcast(
                {
                    "type": "channels_changed",
                    "guild_id": str(after.guild.id),
                    "channel": channel_payload(after),
                }
            )

        listeners = {
            "on_message": on_message,
            "on_message_edit": on_message_edit,
            "on_message_delete": on_message_delete,
            "on_bulk_message_delete": on_bulk_message_delete,
            "on_typing": on_typing,
            "on_member_join": on_member_join,
            "on_member_remove": on_member_remove,
            "on_member_update": on_member_update,
            "on_guild_channel_create": on_guild_channel_create,
            "on_guild_channel_delete": on_guild_channel_delete,
            "on_guild_channel_update": on_guild_channel_update,
        }
        for name, callback in listeners.items():
            try:
                bot.add_listener(callback, name)
            except Exception:
                logger.exception("Could not attach dashboard listener %s", name)
