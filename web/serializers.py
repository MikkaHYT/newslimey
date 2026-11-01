"""JSON shapes the dashboard front end consumes.

discord.py objects are rich and not JSON serialisable, and the chat view needs
one consistent shape whether a message arrives from history, from a REST send or
from a live gateway event. Everything here is pure and cache-only: no function
performs an HTTP call.
"""

from __future__ import annotations

from typing import Any, Iterable

import discord

#: Extensions we inline as images even when Discord reports no content type.
IMAGE_EXTS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp", ".apng"}
)
VIDEO_EXTS = frozenset({".mp4", ".webm", ".mov", ".m4v", ".mkv", ".gifv"})
AUDIO_EXTS = frozenset({".mp3", ".wav", ".ogg", ".m4a", ".flac", ".opus", ".aac"})


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


def _asset_url(asset: Any, size: int | None = None) -> str | None:
    if asset is None:
        return None
    try:
        if size:
            asset = asset.replace(size=size)
        return asset.url
    except (AttributeError, ValueError):
        return None


def _snowflake(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def display_name(user: Any) -> str:
    """Nickname first, then the server-wide name, then the account name.

    The dashboard deliberately shows how a person is known *here*: the nickname
    if they have one, otherwise their global display name, and only then the
    handle. The raw handle is still included separately so the UI can show it as
    a secondary line, like Discord does.
    """
    if user is None:
        return "Unknown"
    nick = getattr(user, "nick", None)
    if nick:
        return nick
    global_name = getattr(user, "global_name", None)
    if global_name:
        return global_name
    return getattr(user, "name", None) or getattr(user, "display_name", None) or "Unknown"


def top_role_color(member: Any) -> int | None:
    role = getattr(member, "top_role", None)
    if role is None:
        return None
    value = getattr(getattr(role, "color", None), "value", 0)
    return int(value) or None


def user_payload(user: Any, member: Any = None) -> dict[str, Any]:
    """Identity of a user/member, with the member-specific bits when known."""
    if user is None:
        return {
            "id": "0",
            "name": "Unknown",
            "display": "Unknown",
            "username": "unknown",
            "nickname": None,
            "global_name": None,
            "avatar": None,
            "color": None,
            "bot": False,
        }
    if member is None:
        member = user if isinstance(user, discord.Member) else None
    return {
        "id": str(user.id),
        "name": getattr(user, "name", "unknown"),
        "username": getattr(user, "name", "unknown"),
        "display": display_name(member or user),
        "nickname": getattr(member, "nick", None),
        "global_name": getattr(user, "global_name", None),
        "avatar": _asset_url(getattr(user, "display_avatar", None), 128),
        "color": top_role_color(member) if member is not None else None,
        "bot": bool(getattr(user, "bot", False)),
    }


def role_payload(role: discord.Role) -> dict[str, Any]:
    return {
        "id": str(role.id),
        "name": role.name,
        "color": int(role.color.value) or None,
        "position": role.position,
        "mentionable": role.mentionable,
        "hoist": role.hoist,
        "managed": role.managed,
        "is_default": role.is_default(),
        "members": len(role.members),
    }


def _account(member: Any) -> Any:
    """The underlying user of a member.

    Upstream discord.py exposes ``Member.user``; the build this bot runs on
    does not (it only keeps ``_user`` and flattens the rest), so both spellings
    are supported rather than assuming one.
    """
    return (
        getattr(member, "user", None)
        or getattr(member, "_user", None)
        or member
    )


def member_payload(member: discord.Member) -> dict[str, Any]:
    data = user_payload(_account(member), member)
    data.update(
        {
            "roles": [str(role.id) for role in member.roles if not role.is_default()],
            "top_role": str(member.top_role.id) if member.top_role else None,
            "joined_at": _iso(getattr(member, "joined_at", None)),
            "created_at": _iso(getattr(member, "created_at", None)),
            "premium_since": _iso(getattr(member, "premium_since", None)),
            "bot": member.bot,
            "status": str(getattr(member, "status", "offline")),
            "avatar": _asset_url(member.display_avatar, 128),
        }
    )
    return data


def channel_payload(channel: Any) -> dict[str, Any]:
    category = getattr(channel, "category", None)
    return {
        "id": str(channel.id),
        "name": getattr(channel, "name", str(channel)),
        "type": str(getattr(channel, "type", "text")),
        "position": getattr(channel, "position", 0),
        "category_id": _snowflake(getattr(channel, "category_id", None)),
        "category": getattr(category, "name", None),
        "topic": getattr(channel, "topic", None),
        "nsfw": bool(getattr(channel, "nsfw", False)),
        "slowmode": int(getattr(channel, "slowmode_delay", 0) or 0),
        "parent_id": _snowflake(getattr(channel, "parent_id", None)),
    }


def guild_payload(guild: discord.Guild) -> dict[str, Any]:
    return {
        "id": str(guild.id),
        "name": guild.name,
        "icon": _asset_url(guild.icon, 128),
        "banner": _asset_url(guild.banner, 512),
        "owner_id": _snowflake(guild.owner_id),
        "member_count": guild.member_count or len(guild.members),
        "channel_count": len(guild.channels),
        "role_count": len(guild.roles),
        "description": getattr(guild, "description", None),
        "created_at": _iso(getattr(guild, "created_at", None)),
        "boost_level": int(getattr(guild, "premium_tier", 0) or 0),
        "boosts": int(getattr(guild, "premium_subscription_count", 0) or 0),
        "verification": str(getattr(guild, "verification_level", "none")),
        "features": sorted(getattr(guild, "features", []) or []),
        "bot_is_admin": bool(
            guild.me and guild.me.guild_permissions.administrator
        ),
    }


# ---------------------------------------------------------------------------
# Message parts
# ---------------------------------------------------------------------------
def _attachment_kind(attachment: Any) -> str:
    name = (getattr(attachment, "filename", "") or "").lower()
    content_type = (getattr(attachment, "content_type", "") or "").lower()
    extension = name[name.rfind(".") :] if "." in name else ""
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("video/"):
        return "video"
    if content_type.startswith("audio/"):
        return "audio"
    if extension in IMAGE_EXTS:
        return "image"
    if extension in VIDEO_EXTS:
        return "video"
    if extension in AUDIO_EXTS:
        return "audio"
    return "file"


def attachment_payload(attachment: Any) -> dict[str, Any]:
    return {
        "id": str(getattr(attachment, "id", "")),
        "filename": getattr(attachment, "filename", "file"),
        "url": getattr(attachment, "url", None),
        "proxy_url": getattr(attachment, "proxy_url", None),
        "size": int(getattr(attachment, "size", 0) or 0),
        "kind": _attachment_kind(attachment),
        "width": getattr(attachment, "width", None),
        "height": getattr(attachment, "height", None),
        "description": getattr(attachment, "description", None),
    }


def embed_payload(embed: discord.Embed) -> dict[str, Any]:
    data = embed.to_dict()
    # Embed images are sometimes relative attachments (attachment://name);
    # the chat view cannot resolve those, so drop them rather than show a
    # broken image box.
    for key in ("image", "thumbnail", "video"):
        part = data.get(key)
        if isinstance(part, dict):
            url = str(part.get("url") or "")
            if url.startswith("attachment:"):
                data.pop(key, None)
    return data


def sticker_payload(sticker: Any) -> dict[str, Any]:
    return {
        "id": str(getattr(sticker, "id", "")),
        "name": getattr(sticker, "name", "sticker"),
        "url": _asset_url(getattr(sticker, "url", None)),
        "format": str(getattr(sticker, "format", "png")),
    }


def _emoji_payload(emoji: Any, count: int, me: bool) -> dict[str, Any]:
    emoji_id = getattr(emoji, "id", None)
    return {
        "id": str(emoji_id) if emoji_id else None,
        "name": getattr(emoji, "name", None) or str(emoji),
        "animated": bool(getattr(emoji, "animated", False)),
        "url": _asset_url(emoji, None) if emoji_id else None,
        "count": int(count),
        "me": bool(me),
    }


def reaction_payload(reaction: Any) -> dict[str, Any]:
    return _emoji_payload(reaction.emoji, reaction.count, reaction.me)


def _mention_maps(message: discord.Message) -> tuple[dict, dict, dict]:
    users: dict[str, Any] = {}
    for user in list(getattr(message, "mentions", []) or []):
        users[str(user.id)] = user_payload(user)
    roles: dict[str, Any] = {}
    for role in list(getattr(message, "role_mentions", []) or []):
        roles[str(role.id)] = role_payload(role)
    channels: dict[str, Any] = {}
    for channel in list(getattr(message, "channel_mentions", []) or []):
        channels[str(channel.id)] = {"name": getattr(channel, "name", "channel")}
    return users, roles, channels


def _reply_payload(message: discord.Message) -> dict[str, Any] | None:
    reference = getattr(message, "reference", None)
    if reference is None:
        return None
    resolved = getattr(reference, "resolved", None)
    payload: dict[str, Any] = {
        "message_id": _snowflake(getattr(reference, "message_id", None)),
        "channel_id": _snowflake(getattr(reference, "channel_id", None)),
        "deleted": resolved is None
        or isinstance(resolved, discord.DeletedReferencedMessage),
    }
    if isinstance(resolved, discord.Message):
        payload.update(
            {
                "author": user_payload(resolved.author, getattr(resolved, "member", None)),
                "content": resolved.clean_content[:280],
                "attachment": bool(resolved.attachments),
            }
        )
    return payload


def message_payload(message: discord.Message, *, with_reference: bool = True) -> dict[str, Any]:
    """The full shape the chat view renders, for history and live events alike."""
    users, roles, channels = _mention_maps(message)
    # discord.py's MessageType is a plain Enum, so int() needs its value.
    message_type = getattr(message, "type", 0)
    message_type = getattr(message_type, "value", message_type)
    member = getattr(message, "member", None)
    return {
        "id": str(message.id),
        "channel_id": str(message.channel.id),
        "guild_id": _snowflake(getattr(message.guild, "id", None)),
        "content": message.content or "",
        "clean_content": getattr(message, "clean_content", message.content or ""),
        "timestamp": _iso(message.created_at),
        "edited": _iso(getattr(message, "edited_at", None)),
        "author": user_payload(message.author, member),
        "attachments": [attachment_payload(a) for a in message.attachments],
        "embeds": [embed_payload(e) for e in message.embeds],
        "stickers": [sticker_payload(s) for s in getattr(message, "stickers", [])],
        "reactions": [reaction_payload(r) for r in getattr(message, "reactions", [])],
        "mentions": users,
        "role_mentions": roles,
        "channel_mentions": channels,
        "reply": _reply_payload(message) if with_reference else None,
        "pinned": bool(getattr(message, "pinned", False)),
        "tts": bool(getattr(message, "tts", False)),
        "type": int(message_type or 0),
        "webhook": getattr(message, "webhook_id", None) is not None,
        "system": bool(getattr(message, "is_system", lambda: False)()),
    }


# ---------------------------------------------------------------------------
# History helpers
# ---------------------------------------------------------------------------
def messages_payload(messages: Iterable[discord.Message]) -> list[dict[str, Any]]:
    """Oldest-first, which is the order the chat column renders in."""
    return [message_payload(message) for message in reversed(list(messages))]


def channel_permissions(channel: Any, me: Any) -> dict[str, bool]:
    """What the bot may do in *channel*, so the UI can disable its buttons."""
    try:
        permissions = channel.permissions_for(me)
    except Exception:
        return {
            "send": False,
            "read": False,
            "attach": False,
            "manage": False,
            "history": False,
            "react": False,
        }
    return {
        "send": bool(permissions.send_messages),
        "read": bool(permissions.read_messages),
        "attach": bool(permissions.attach_files),
        "manage": bool(permissions.manage_messages),
        "history": bool(permissions.read_message_history),
        "react": bool(permissions.add_reactions),
    }


def selectable_channels(guild: discord.Guild) -> list[dict[str, Any]]:
    """Text-like channels the bot can read, newest-friendly order."""
    allowed = {
        discord.ChannelType.text,
        discord.ChannelType.news,
        discord.ChannelType.public_thread,
        discord.ChannelType.private_thread,
        discord.ChannelType.news_thread,
    }
    me = guild.me
    payloads: list[dict[str, Any]] = []
    for channel in guild.channels:
        if channel.type not in allowed:
            continue
        permissions = channel_permissions(channel, me) if me is not None else {"read": True}
        if not permissions.get("read", True):
            continue
        data = channel_payload(channel)
        data["permissions"] = permissions
        payloads.append(data)
    payloads.sort(key=lambda c: (c["category"] or "", c["position"], c["name"]))
    return payloads
