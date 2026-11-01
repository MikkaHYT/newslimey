"""JSON API behind the dashboard.

Everything the front end can see or change goes through here. The bot object is
put on the aiohttp application at startup, so handlers can reach live guilds,
channels and members without a second connection to Discord.
"""

from __future__ import annotations

import functools
import io
import logging
import platform
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import discord
from aiohttp import web
from yarl import URL

from utils.core import settings as S
from utils.core.http_session import shared_session

from . import serializers as ser
from .auth import (
    _client_address,
    clear_session_cookie,
    set_session_cookie,
)
from .settings_schema import FIELDS, GLOBAL, coerce, schema_payload

logger = logging.getLogger("slimey.dashboard.api")

#: Hosts the media proxy will fetch from. Anything else is refused so the
#: dashboard cannot be turned into an open proxy.
MEDIA_HOSTS = (
    "discordapp.com",
    "discordapp.net",
    "discord.com",
    "discord.co",
)

MAX_UPLOAD_BYTES = 48 * 1024 * 1024
MAX_FILES = 10


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def endpoint(func):
    """Turn API failures into JSON instead of an HTML error page."""

    @functools.wraps(func)
    async def wrapper(request: web.Request):
        try:
            return await func(request)
        except ApiError as error:
            return web.json_response({"error": str(error)}, status=error.status)
        except discord.Forbidden as error:
            return web.json_response(
                {"error": f"Discord refused that: {error.text or error}"}, status=403
            )
        except discord.NotFound:
            return web.json_response({"error": "That no longer exists on Discord"}, status=404)
        except discord.HTTPException as error:
            return web.json_response(
                {"error": f"Discord error: {error.text or error}"}, status=400
            )
        except Exception:
            logger.exception("Dashboard API failure on %s %s", request.method, request.path)
            return web.json_response({"error": "Something went wrong on our side"}, status=500)

    return wrapper


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------
def _bot(request: web.Request) -> Any:
    return request.app["bot"]


def _guild(request: web.Request) -> discord.Guild:
    bot = _bot(request)
    try:
        guild_id = int(request.match_info["guild_id"])
    except (KeyError, ValueError):
        raise ApiError("Bad server id")
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise ApiError("The bot is not in that server", status=404)
    return guild


def _channel(request: web.Request) -> Any:
    bot = _bot(request)
    try:
        channel_id = int(request.match_info["channel_id"])
    except (KeyError, ValueError):
        raise ApiError("Bad channel id")
    channel = bot.get_channel(channel_id)
    if channel is None:
        raise ApiError("The bot cannot see that channel", status=404)
    if not (hasattr(channel, "history") and hasattr(channel, "get_partial_message")):
        raise ApiError("That channel cannot hold messages")
    return channel


def _int_param(raw: Any, name: str) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ApiError(f"{name} must be a number")


async def _json(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        raise ApiError("Expected a JSON body")
    if not isinstance(data, dict):
        raise ApiError("Expected a JSON object")
    return data


def _user_stub(bot: Any, user_id: Any) -> dict[str, Any]:
    """A cached user if Discord has one, otherwise a readable placeholder."""
    try:
        numeric = int(user_id)
    except (TypeError, ValueError):
        return ser.user_payload(None)
    user = bot.get_user(numeric)
    if user is None:
        return {
            "id": str(numeric),
            "name": f"user {numeric}",
            "username": f"user {numeric}",
            "display": f"user {numeric}",
            "nickname": None,
            "global_name": None,
            "avatar": None,
            "color": None,
            "bot": False,
        }
    return ser.user_payload(user)


def _session_payload(bot: Any) -> dict[str, Any]:
    user = bot.user
    return {
        "id": str(user.id) if user else None,
        "name": str(user) if user else "starting",
        "avatar": ser._asset_url(getattr(user, "display_avatar", None), 128),
        "version": getattr(bot, "version", "0"),
    }


def _disabled_for(bot: Any, guild_id: int) -> set[str]:
    settings = getattr(bot, "dash_settings", None)
    if settings is None:
        return set()
    return set(settings.disabled.get(int(guild_id), set()))


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------
@endpoint
async def session(request: web.Request) -> web.Response:
    bot = _bot(request)
    return web.json_response({"authenticated": True, "bot": _session_payload(bot)})


@endpoint
async def login(request: web.Request) -> web.Response:
    auth = request.app["auth"]
    address = _client_address(request)
    if auth.is_locked_out(address):
        raise ApiError(
            f"Too many wrong passwords. Try again in {auth.retry_after(address)}s.", 429
        )

    data = await _json(request)
    if not auth.check_password(data.get("password")):
        auth.record_failure(address)
        raise ApiError("That password is not right", 401)

    auth.clear_failures(address)
    response = web.json_response(
        {"authenticated": True, "bot": _session_payload(_bot(request))}
    )
    set_session_cookie(response, auth.issue_token())
    return response


@endpoint
async def logout(request: web.Request) -> web.Response:
    response = web.json_response({"ok": True})
    clear_session_cookie(response)
    return response


@endpoint
async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


# ---------------------------------------------------------------------------
# Overview and servers
# ---------------------------------------------------------------------------
@endpoint
async def overview(request: web.Request) -> web.Response:
    bot = _bot(request)
    started = float(request.app.get("started_at") or time.time())

    try:
        import psutil

        process = psutil.Process()
        memory_mb = round(process.memory_info().rss / (1024 * 1024), 1)
        cpu = round(process.cpu_percent(interval=None), 1)
    except Exception:
        memory_mb = None
        cpu = None

    guilds = sorted(
        bot.guilds, key=lambda g: (g.member_count or 0), reverse=True
    )
    voice = []
    for guild in bot.guilds:
        client = guild.voice_client
        if client and client.channel:
            voice.append(
                {
                    "guild_id": str(guild.id),
                    "guild": guild.name,
                    "channel": client.channel.name,
                    "playing": bool(client.is_playing()),
                    "paused": bool(client.is_paused()),
                }
            )

    payload = {
        "bot": {
            **_session_payload(bot),
            "guilds": len(bot.guilds),
            "users": sum(g.member_count or 0 for g in bot.guilds),
            "channels": sum(len(g.channels) for g in bot.guilds),
            "commands": len(bot.commands),
            "slash_commands": len(list(bot.tree.walk_commands())),
            "latency_ms": round(bot.latency * 1000),
            "uptime": int(time.time() - started),
            "memory_mb": memory_mb,
            "cpu": cpu,
            "python": platform.python_version(),
            "discord": discord.__version__,
            "shards": bot.shard_count or 1,
        },
        "guilds": [ser.guild_payload(guild) for guild in guilds],
        "voice": voice,
        "clients": len(request.app["hub"].sockets),
    }
    return web.json_response(payload)


@endpoint
async def guild_detail(request: web.Request) -> web.Response:
    guild = _guild(request)
    data = ser.guild_payload(guild)
    channels = sorted(guild.channels, key=lambda c: (c.position, c.name or ""))
    data["channels"] = [ser.channel_payload(c) for c in channels]
    data["chat_channels"] = ser.selectable_channels(guild)
    data["categories"] = [
        ser.channel_payload(c)
        for c in channels
        if c.type is discord.ChannelType.category
    ]
    data["roles"] = [
        ser.role_payload(role)
        for role in sorted(guild.roles, key=lambda r: r.position, reverse=True)
    ]
    data["emojis"] = [
        {
            "id": str(emoji.id),
            "name": emoji.name,
            "animated": emoji.animated,
            "url": ser._asset_url(emoji, 48),
        }
        for emoji in guild.emojis
    ]
    data["owner"] = _user_stub(request.app["bot"], guild.owner_id)
    data["me"] = ser.member_payload(guild.me) if guild.me else None
    return web.json_response(data)


@endpoint
async def channel_detail(request: web.Request) -> web.Response:
    channel = _channel(request)
    guild = getattr(channel, "guild", None)
    data = ser.channel_payload(channel)
    data["guild_id"] = str(guild.id) if guild else None
    data["permissions"] = (
        ser.channel_permissions(channel, guild.me) if guild and guild.me else {}
    )
    return web.json_response(data)


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
@endpoint
async def messages(request: web.Request) -> web.Response:
    channel = _channel(request)
    guild = getattr(channel, "guild", None)
    if guild is None or guild.me is None:
        raise ApiError("That channel is not in a server")
    permissions = ser.channel_permissions(channel, guild.me)
    if not permissions["read"]:
        raise ApiError("The bot cannot read that channel", status=403)

    limit = min(max(_int_param(request.query.get("limit", 50), "limit"), 1), 100)
    before_raw = request.query.get("before")
    before = discord.Object(id=int(before_raw)) if before_raw else None

    history = [
        message
        async for message in channel.history(limit=limit, before=before, oldest_first=False)
    ]
    return web.json_response(
        {
            "channel": ser.channel_payload(channel),
            "messages": ser.messages_payload(history),
            "has_more": len(history) == limit,
            "permissions": permissions,
        }
    )


async def _read_upload(request: web.Request) -> tuple[str, list[discord.File], str | None]:
    content = ""
    files: list[discord.File] = []
    reply_to: str | None = None
    total = 0
    reader = await request.multipart()
    async for part in reader:
        if part.name == "content":
            content = (await part.text()).strip()
        elif part.name == "reply_to":
            reply_to = (await part.text()).strip() or None
        elif part.name in ("files", "file", "attachment"):
            data = await part.read()
            if not data:
                continue
            total += len(data)
            if total > MAX_UPLOAD_BYTES:
                raise ApiError("Those files are too big to upload together", 413)
            if len(files) >= MAX_FILES:
                raise ApiError(f"A message can carry at most {MAX_FILES} files", 413)
            filename = Path(part.filename or "upload.bin").name or "upload.bin"
            files.append(discord.File(io.BytesIO(data), filename=filename))
    return content, files, reply_to


@endpoint
async def send_message(request: web.Request) -> web.Response:
    channel = _channel(request)
    guild = getattr(channel, "guild", None)
    if guild is not None and guild.me is not None:
        permissions = ser.channel_permissions(channel, guild.me)
        if not permissions["send"]:
            raise ApiError("The bot cannot send messages in that channel", status=403)

    content = ""
    files: list[discord.File] = []
    reply_to: str | None = None
    if (request.content_type or "").startswith("multipart/"):
        content, files, reply_to = await _read_upload(request)
    else:
        data = await _json(request)
        content = str(data.get("content") or "").strip()
        reply_to = data.get("reply_to")
        reply_to = str(reply_to) if reply_to else None

    if not content and not files:
        raise ApiError("Type something or attach a file first")
    if len(content) > 2000:
        raise ApiError("Discord messages cannot be longer than 2000 characters")

    if reply_to:
        try:
            target = channel.get_partial_message(int(reply_to))
        except (TypeError, ValueError):
            raise ApiError("Bad reply target")
        message = await target.reply(
            content or None, files=files or None, mention_author=False
        )
    else:
        message = await channel.send(content=content or None, files=files or None)

    return web.json_response({"message": ser.message_payload(message)})


@endpoint
async def edit_message(request: web.Request) -> web.Response:
    channel = _channel(request)
    message_id = _int_param(request.match_info.get("message_id"), "message id")
    data = await _json(request)
    content = str(data.get("content") or "").strip()
    if not content:
        raise ApiError("A message cannot be empty")
    if len(content) > 2000:
        raise ApiError("Discord messages cannot be longer than 2000 characters")
    message = await channel.get_partial_message(message_id).edit(content=content)
    return web.json_response({"message": ser.message_payload(message)})


@endpoint
async def delete_message(request: web.Request) -> web.Response:
    channel = _channel(request)
    message_id = _int_param(request.match_info.get("message_id"), "message id")
    await channel.get_partial_message(message_id).delete()
    return web.json_response({"ok": True, "message_id": str(message_id)})


@endpoint
async def react_message(request: web.Request) -> web.Response:
    channel = _channel(request)
    message_id = _int_param(request.match_info.get("message_id"), "message id")
    data = await _json(request)
    emoji = str(data.get("emoji") or "").strip()
    if not emoji or len(emoji) > 100:
        raise ApiError("Pick an emoji first")
    partial = channel.get_partial_message(message_id)
    if data.get("remove"):
        await partial.remove_reaction(emoji, request.app["bot"].user)
    else:
        await partial.add_reaction(emoji)
    fresh = await channel.fetch_message(message_id)
    return web.json_response({"reactions": [ser.reaction_payload(r) for r in fresh.reactions]})


@endpoint
async def media(request: web.Request) -> web.StreamResponse:
    raw = request.query.get("url")
    if not raw:
        raise ApiError("No media url given")
    url = URL(raw)
    if url.scheme not in ("http", "https") or not url.host:
        raise ApiError("Only http(s) media can be proxied")
    host = (url.host or "").lower()
    if not any(host == allowed or host.endswith("." + allowed) for allowed in MEDIA_HOSTS):
        raise ApiError("That host is not allowed")

    session = shared_session()
    try:
        upstream = await session.get(
            url, headers={"User-Agent": "SlimeyDashboard/1.0"}, allow_redirects=True
        )
    except Exception:
        raise ApiError("Could not reach that file", 502)

    try:
        if upstream.status != 200:
            raise ApiError("That file is no longer available", upstream.status)
        length = upstream.headers.get("Content-Length")
        if length and int(length) > MAX_UPLOAD_BYTES:
            raise ApiError("That file is too large to preview", 413)
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": upstream.headers.get(
                    "Content-Type", "application/octet-stream"
                ),
                "Cache-Control": "public, max-age=3600",
            },
        )
        if length:
            response.content_length = int(length)
        await response.prepare(request)
        async for chunk in upstream.content.iter_chunked(64 * 1024):
            await response.write(chunk)
        await response.write_eof()
        return response
    finally:
        upstream.release()


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------
@endpoint
async def members(request: web.Request) -> web.Response:
    guild = _guild(request)
    query = (request.query.get("query") or "").strip().lower()
    offset = max(_int_param(request.query.get("offset", 0), "offset"), 0)
    limit = min(max(_int_param(request.query.get("limit", 60), "limit"), 1), 200)

    members = list(guild.members)
    if query:
        def matches(member: discord.Member) -> bool:
            haystack = " ".join(
                filter(
                    None,
                    [
                        member.name,
                        member.nick,
                        getattr(member, "global_name", None),
                        str(member.id),
                    ],
                )
            ).lower()
            return query in haystack

        members = [member for member in members if matches(member)]

    members.sort(key=lambda m: ser.display_name(m).lower())
    window = members[offset : offset + limit]
    return web.json_response(
        {
            "members": [ser.member_payload(m) for m in window],
            "total": len(members),
            "offset": offset,
            "limit": limit,
        }
    )


@endpoint
async def member_detail(request: web.Request) -> web.Response:
    guild = _guild(request)
    member = guild.get_member(_int_param(request.match_info.get("user_id"), "user id"))
    if member is None:
        try:
            member = await guild.fetch_member(int(request.match_info["user_id"]))
        except discord.HTTPException:
            raise ApiError("That member is not in the server", status=404)

    data = ser.member_payload(member)
    data["roles_detail"] = [
        ser.role_payload(role)
        for role in sorted(member.roles, key=lambda r: r.position, reverse=True)
        if not role.is_default()
    ]
    data["is_owner"] = guild.owner_id == member.id
    data["cases"] = await _guild_cases(request.app["bot"], guild.id, limit=25, member_id=member.id)
    return web.json_response(data)


async def _guild_cases(
    bot: Any, guild_id: int, *, limit: int = 100, member_id: int | None = None
) -> list[dict[str, Any]]:
    sql = (
        "SELECT case_id, moderator_id, member_id, type, reason, created_on "
        "FROM moderation_history WHERE guild_id = ?"
    )
    params: list[Any] = [int(guild_id)]
    if member_id is not None:
        sql += " AND member_id = ?"
        params.append(int(member_id))
    sql += " ORDER BY case_id DESC LIMIT ?;"
    params.append(int(limit))
    try:
        rows = await bot.db.fetch(sql, *params)
    except Exception:
        logger.exception("Could not read moderation history")
        return []
    cases = []
    for case_id, moderator_id, target_id, action, reason, created_on in rows:
        cases.append(
            {
                "case_id": case_id,
                "type": action,
                "reason": reason,
                "created_on": created_on,
                "moderator": _user_stub(bot, moderator_id),
                "target": _user_stub(bot, target_id),
            }
        )
    return cases


@endpoint
async def member_roles(request: web.Request) -> web.Response:
    guild = _guild(request)
    member = guild.get_member(_int_param(request.match_info.get("user_id"), "user id"))
    if member is None:
        raise ApiError("That member is not in the server", status=404)
    data = await _json(request)

    to_add = []
    for raw in data.get("add") or []:
        role = guild.get_role(_int_param(raw, "role id"))
        if role is not None:
            to_add.append(role)
    to_remove = []
    for raw in data.get("remove") or []:
        role = guild.get_role(_int_param(raw, "role id"))
        if role is not None:
            to_remove.append(role)

    if not to_add and not to_remove:
        raise ApiError("No roles were changed")

    if to_add:
        await member.add_roles(*to_add, reason="Changed from the web dashboard")
    if to_remove:
        await member.remove_roles(*to_remove, reason="Changed from the web dashboard")
    return web.json_response({"member": ser.member_payload(member)})


@endpoint
async def member_nickname(request: web.Request) -> web.Response:
    guild = _guild(request)
    member = guild.get_member(_int_param(request.match_info.get("user_id"), "user id"))
    if member is None:
        raise ApiError("That member is not in the server", status=404)
    data = await _json(request)
    nick = (data.get("nickname") or "").strip() or None
    await member.edit(nick=nick, reason="Changed from the web dashboard")
    return web.json_response({"member": ser.member_payload(member)})


@endpoint
async def member_timeout(request: web.Request) -> web.Response:
    guild = _guild(request)
    member = guild.get_member(_int_param(request.match_info.get("user_id"), "user id"))
    if member is None:
        raise ApiError("That member is not in the server", status=404)
    data = await _json(request)
    minutes = _int_param(data.get("minutes", 0), "minutes")
    reason = str(data.get("reason") or "Timed out from the web dashboard")[:512]
    if minutes <= 0:
        await member.timeout(None, reason=reason)
    else:
        await member.timeout(timedelta(minutes=min(minutes, 40320)), reason=reason)
    return web.json_response({"member": ser.member_payload(member)})


@endpoint
async def member_kick(request: web.Request) -> web.Response:
    guild = _guild(request)
    member = guild.get_member(_int_param(request.match_info.get("user_id"), "user id"))
    if member is None:
        raise ApiError("That member is not in the server", status=404)
    data = await _json(request)
    reason = str(data.get("reason") or "Kicked from the web dashboard")[:512]
    await member.kick(reason=reason)
    return web.json_response({"ok": True})


@endpoint
async def member_ban(request: web.Request) -> web.Response:
    guild = _guild(request)
    user_id = _int_param(request.match_info.get("user_id"), "user id")
    data = await _json(request)
    reason = str(data.get("reason") or "Banned from the web dashboard")[:512]
    days = min(max(_int_param(data.get("delete_days", 0), "delete_days"), 0), 7)
    member = guild.get_member(user_id)
    await guild.ban(
        member or discord.Object(id=user_id), reason=reason, delete_message_days=days
    )
    return web.json_response({"ok": True})


@endpoint
async def member_unban(request: web.Request) -> web.Response:
    guild = _guild(request)
    user_id = _int_param(request.match_info.get("user_id"), "user id")
    data = await _json(request)
    reason = str(data.get("reason") or "Unbanned from the web dashboard")[:512]
    await guild.unban(discord.Object(id=user_id), reason=reason)
    return web.json_response({"ok": True})


@endpoint
async def bans(request: web.Request) -> web.Response:
    guild = _guild(request)
    entries = []
    async for entry in guild.bans(limit=200):
        entries.append(
            {
                "user": ser.user_payload(entry.user),
                "reason": entry.reason,
            }
        )
    return web.json_response({"bans": entries})


@endpoint
async def cases(request: web.Request) -> web.Response:
    guild = _guild(request)
    limit = min(max(_int_param(request.query.get("limit", 100), "limit"), 1), 500)
    return web.json_response(
        {"cases": await _guild_cases(request.app["bot"], guild.id, limit=limit)}
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def _normalise_stored(key: str, value: Any) -> Any:
    """Turn what the store holds into the type the UI expects."""
    field = FIELDS.get(key)
    if field is None:
        return value
    kind = field["type"]
    if kind in ("channel", "role") and isinstance(value, str) and value.isdigit():
        return int(value)
    if kind in ("channels", "roles", "idlist") and isinstance(value, (list, tuple)):
        return [int(v) for v in value if str(v).isdigit()]
    if kind in ("number",) and isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return value


def _scope_for(request: web.Request) -> int:
    raw = request.match_info.get("guild_id")
    if raw is None:
        return GLOBAL
    return _int_param(raw, "guild id")


async def _settings_payload(request: web.Request, scope: int) -> dict[str, Any]:
    bot = _bot(request)
    settings = getattr(bot, "dash_settings", None)
    if settings is None:
        raise ApiError("Settings are not ready yet", 503)

    values = {
        key: _normalise_stored(key, settings.stored(key, guild_id=scope))
        for key in FIELDS
    }
    if scope != GLOBAL:
        values["prefix"] = settings.guild_prefix(bot, scope)

    stored = [
        {"key": key, "value": value}
        for (guild_id, key), value in sorted(settings.values.items())
        if guild_id == scope and key not in FIELDS
    ]
    return {
        "scope": "guild" if scope != GLOBAL else "global",
        "guild_id": str(scope),
        "schema": schema_payload("guild" if scope != GLOBAL else "global"),
        "values": values,
        "advanced": stored,
    }


@endpoint
async def get_settings(request: web.Request) -> web.Response:
    return web.json_response(await _settings_payload(request, _scope_for(request)))


@endpoint
async def put_settings(request: web.Request) -> web.Response:
    bot = _bot(request)
    scope = _scope_for(request)
    settings = getattr(bot, "dash_settings", None)
    if settings is None:
        raise ApiError("Settings are not ready yet", 503)

    data = await _json(request)
    updates = data.get("updates") or {}
    deletes = data.get("deletes") or []
    if not isinstance(updates, dict) or not isinstance(deletes, (list, tuple)):
        raise ApiError("updates must be an object and deletes a list")

    errors: dict[str, str] = {}
    changed = []
    for key, value in updates.items():
        name = str(key)
        ok, coerced, error = coerce(name, value)
        if not ok:
            errors[name] = error or "Invalid value"
            continue
        if name == "prefix" and scope != GLOBAL:
            await settings.set_prefix(scope, coerced)
        else:
            await settings.set_value(name, coerced, guild_id=scope)
        changed.append(name)

    for key in deletes:
        name = str(key)
        if name == "prefix" and scope != GLOBAL:
            await settings.set_prefix(scope, None)
        else:
            await settings.set_value(name, None, guild_id=scope)
        changed.append(name)

    if errors and not changed:
        raise ApiError("; ".join(f"{k}: {v}" for k, v in errors.items()))

    # Derived hot-path maps (disabled commands, radio channels, maintenance)
    # are rebuilt from the rows, so nothing goes stale after a dashboard write.
    try:
        await settings.preload()
    except Exception:
        logger.exception("Could not refresh the settings cache")

    payload = await _settings_payload(request, scope)
    payload["errors"] = errors
    payload["changed"] = changed
    return web.json_response(payload)


# ---------------------------------------------------------------------------
# Commands and maintenance
# ---------------------------------------------------------------------------
@endpoint
async def commands(request: web.Request) -> web.Response:
    bot = _bot(request)
    guild_id = request.query.get("guild_id")
    disabled = _disabled_for(bot, int(guild_id)) if guild_id else set()

    prefix_commands = []
    for command in sorted(bot.commands, key=lambda c: c.qualified_name):
        if getattr(command, "hidden", False):
            continue
        prefix_commands.append(
            {
                "name": command.name,
                "qualified": command.qualified_name,
                "aliases": list(command.aliases),
                "help": (command.help or "").strip().splitlines()[0][:300]
                if command.help
                else "",
                "cog": command.cog_name or "Other",
                "signature": str(command.signature or ""),
                "disabled": command.qualified_name in disabled,
            }
        )

    slash_commands = []
    for command in bot.tree.walk_commands():
        slash_commands.append(
            {
                "qualified": command.qualified_name,
                "description": (command.description or "")[:300],
                "group": command.parent.qualified_name if command.parent else None,
                "disabled": command.qualified_name in disabled,
            }
        )

    settings = getattr(bot, "dash_settings", None)
    return web.json_response(
        {
            "commands": prefix_commands,
            "slash": sorted(slash_commands, key=lambda c: c["qualified"]),
            "maintenance": {
                "global": getattr(settings, "maintenance", None),
                "commands": dict(getattr(settings, "command_maintenance", {}) or {}),
            },
        }
    )


@endpoint
async def toggle_command(request: web.Request) -> web.Response:
    bot = _bot(request)
    guild = _guild(request)
    settings = getattr(bot, "dash_settings", None)
    if settings is None:
        raise ApiError("Settings are not ready yet", 503)

    name = (request.match_info.get("name") or "").strip()
    if not name:
        raise ApiError("Which command?")
    known = {command.qualified_name for command in bot.commands}
    known.update(command.qualified_name for command in bot.tree.walk_commands())
    if name not in known:
        raise ApiError(f"There is no command called {name}", 404)

    data = await _json(request)
    enabled = bool(data.get("enabled"))

    disabled = set(settings.disabled.get(guild.id, set()))
    if enabled:
        disabled.discard(name)
    else:
        disabled.add(name)
    await settings.set_value(S.DISABLED_KEY, sorted(disabled) or None, guild_id=guild.id)
    settings.disabled[guild.id] = disabled

    return web.json_response({"enabled": enabled, "name": name})


@endpoint
async def maintenance(request: web.Request) -> web.Response:
    bot = _bot(request)
    settings = getattr(bot, "dash_settings", None)
    if settings is None:
        raise ApiError("Settings are not ready yet", 503)

    data = await _json(request)
    reason = str(data.get("reason") or "").strip() or None
    command = str(data.get("command") or "").strip() or None
    guild_raw = data.get("guild_id")

    if command is None and guild_raw in (None, "", 0, "0"):
        # Bot-wide switch: a plain string row at guild 0.
        await settings.set_value(S.MAINTENANCE_KEY, reason, guild_id=GLOBAL)
        settings.maintenance = reason
        await settings.preload()
        return web.json_response({"ok": True, "maintenance": {"global": reason}})

    scope = GLOBAL if guild_raw in (None, "", 0, "0") else _int_param(guild_raw, "guild id")
    current = dict(settings.stored(S.MAINTENANCE_KEY, guild_id=scope) or {})
    if not isinstance(current, dict):
        current = {}
    if command:
        if reason:
            current[command] = reason
        else:
            current.pop(command, None)
    else:
        current = {}
    await settings.set_value(S.MAINTENANCE_KEY, current or None, guild_id=scope)
    await settings.preload()
    return web.json_response(
        {
            "ok": True,
            "maintenance": {
                "global": settings.maintenance,
                "commands": dict(settings.command_maintenance),
            },
        }
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def setup_routes(app: web.Application) -> None:
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/logout", logout)
    app.router.add_get("/api/session", session)
    app.router.add_get("/api/health", health)

    app.router.add_get("/api/overview", overview)
    app.router.add_get("/api/guilds/{guild_id}", guild_detail)
    app.router.add_get("/api/channels/{channel_id}", channel_detail)
    app.router.add_get("/api/channels/{channel_id}/messages", messages)
    app.router.add_post("/api/channels/{channel_id}/messages", send_message)
    app.router.add_patch("/api/channels/{channel_id}/messages/{message_id}", edit_message)
    app.router.add_delete("/api/channels/{channel_id}/messages/{message_id}", delete_message)
    app.router.add_post(
        "/api/channels/{channel_id}/messages/{message_id}/react", react_message
    )
    app.router.add_get("/api/media", media)

    app.router.add_get("/api/guilds/{guild_id}/members", members)
    app.router.add_get("/api/guilds/{guild_id}/members/{user_id}", member_detail)
    app.router.add_post("/api/guilds/{guild_id}/members/{user_id}/roles", member_roles)
    app.router.add_post("/api/guilds/{guild_id}/members/{user_id}/nickname", member_nickname)
    app.router.add_post("/api/guilds/{guild_id}/members/{user_id}/timeout", member_timeout)
    app.router.add_post("/api/guilds/{guild_id}/members/{user_id}/kick", member_kick)
    app.router.add_post("/api/guilds/{guild_id}/members/{user_id}/ban", member_ban)
    app.router.add_post("/api/guilds/{guild_id}/members/{user_id}/unban", member_unban)
    app.router.add_get("/api/guilds/{guild_id}/bans", bans)
    app.router.add_get("/api/guilds/{guild_id}/cases", cases)

    app.router.add_get("/api/guilds/{guild_id}/settings", get_settings)
    app.router.add_put("/api/guilds/{guild_id}/settings", put_settings)
    app.router.add_get("/api/settings/global", get_settings)
    app.router.add_put("/api/settings/global", put_settings)

    app.router.add_get("/api/commands", commands)
    app.router.add_post("/api/guilds/{guild_id}/commands/{name}", toggle_command)
    app.router.add_post("/api/maintenance", maintenance)
