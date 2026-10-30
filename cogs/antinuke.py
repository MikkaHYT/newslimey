"""Anti-nuke protection and honeypot channels.

This cog watches the destructive server actions a compromised staff account or a
malicious script performs — mass channel/role/webhook creation and deletion,
mass bans/kicks, dangerous role grants and server edits — and punishes the
account that did them once it crosses a configurable threshold inside a short
time window.

    ,antinuke                    command overview
    ,antinuke enable             turn protection on for this server
    ,antinuke disable            turn it off
    ,antinuke status             show every switch and threshold
    ,antinuke config             same, for whoever manages the server
    ,antinuke channel [on|off]   prevent mass channel create/delete
    ,antinuke role [on|off]      prevent mass role create/delete
    ,antinuke permissions [on|off]  watch dangerous permission grants
    ,antinuke ban [on|off]       prevent mass member bans
    ,antinuke kick [on|off]      prevent mass member kicks
    ,antinuke webhook [on|off]   prevent mass webhook creation
    ,antinuke vanity [on|off]    punish vanity / server profile edits
    ,antinuke botadd [on|off]    punish new bot additions
    ,antinuke emoji [on|off]     prevent mass emoji deletion
    ,antinuke list               armed modules and the whitelist
    ,antinuke log #channel       where incidents are posted
    ,antinuke punishment ban     what happens to a nuker (ban/kick/strip)
    ,antinuke threshold ban 5    max actions allowed in the window
    ,antinuke window 10          length of the sliding window, seconds
    ,antinuke admin @user        let a user edit these settings (owner only)
    ,antinuke admins             who can edit these settings
    ,antinuke whitelist @user    exempt a member (or bot) from protection
    ,antinuke trust add @user    exempt a user from protection
    ,antinuke trust role add @r  exempt everyone holding a role
    ,antinuke trustlist          show the exempt users

Honeypot channels bait bots and scripts that blindly post into every channel
they can see. Anybody who is not a configured honeypot moderator and posts in a
honeypot channel is removed from the server. The honeypot channel only ever
holds one message: a bait card whose punishment and ``Accounts Caught`` counter
are edited in place after every catch, so the channel never fills up. Catches
are DM'd to the honeypot moderators and mirrored into the server logs.

    ,honeypot create             make (or adopt) a honeypot channel
    ,honeypot mod add @user      receive honeypot catch notifications
    ,honeypot mod remove @user
    ,honeypot mod list
    ,honeypot action ban         ban / kick / timeout / jail / none
    ,honeypot status
    ,honeypot enable|disable
    ,honeypot reset              reset the caught counter

All state lives in the dashboard settings store (``bot_settings``) so it
survives restarts and is visible to the rest of the bot. Incidents are mirrored
into the server-logs ``moderation`` feed when that cog is set up.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Optional

import discord
from discord.ext import commands

from utils.embeds import Embeds
from utils.guardrails import check_slash_guardrails

logger = logging.getLogger("discord.client")

# ---------------------------------------------------------------------------
# Settings keys (per guild, stored in bot_settings)
# ---------------------------------------------------------------------------
ENABLED_KEY = "antinuke_enabled"
LOG_CHANNEL_KEY = "antinuke_log_channel"
PUNISHMENT_KEY = "antinuke_punishment"
TRUSTED_USERS_KEY = "antinuke_trusted_users"
TRUSTED_ROLES_KEY = "antinuke_trusted_roles"
ADMINS_KEY = "antinuke_admins"
WINDOW_KEY = "antinuke_window"
THRESHOLD_KEY = "antinuke_threshold_{action}"
MODULE_KEY = "antinuke_module_{module}"

HONEYPOT_CHANNEL_KEY = "honeypot_channel"
HONEYPOT_MESSAGE_KEY = "honeypot_message"
HONEYPOT_MODS_KEY = "honeypot_mods"
HONEYPOT_CAUGHT_KEY = "honeypot_caught"
HONEYPOT_ACTION_KEY = "honeypot_action"
HONEYPOT_ENABLED_KEY = "honeypot_enabled"

#: action key -> (max actions allowed per window before punishment, label)
DEFAULT_THRESHOLDS: dict[str, int] = {
    "channel_create": 3,
    "channel_delete": 3,
    "role_create": 3,
    "role_delete": 3,
    "role_update": 2,
    "ban": 5,
    "kick": 5,
    "webhook_create": 2,
    "guild_update": 2,
    "member_role_update": 3,
    "vanity": 0,
    "bot_add": 0,
    "emoji_delete": 3,
}

ACTION_LABELS: dict[str, str] = {
    "channel_create": "Channels created",
    "channel_delete": "Channels deleted",
    "role_create": "Roles created",
    "role_delete": "Roles deleted",
    "role_update": "Dangerous role grants",
    "ban": "Members banned",
    "kick": "Members kicked",
    "webhook_create": "Webhooks created",
    "guild_update": "Server edits",
    "member_role_update": "Dangerous roles given",
    "vanity": "Vanity URL changes",
    "bot_add": "Bots added",
    "emoji_delete": "Emojis deleted",
}

#: Protection modules, the switches ``antinuke <module>`` flips. Order is the
#: order they are listed in help/config output.
MODULES: dict[str, dict[str, Any]] = {
    "channel": {
        "label": "Channels",
        "help": "Prevent mass channel creation and deletion",
        "actions": ("channel_create", "channel_delete"),
    },
    "role": {
        "label": "Roles",
        "help": "Prevent mass role creation and deletion",
        "actions": ("role_create", "role_delete"),
    },
    "permissions": {
        "label": "Permissions",
        "help": "Watch dangerous permissions being granted",
        "actions": ("role_update", "member_role_update"),
    },
    "ban": {
        "label": "Bans",
        "help": "Prevent mass member bans",
        "actions": ("ban",),
    },
    "kick": {
        "label": "Kicks",
        "help": "Prevent mass member kicks",
        "actions": ("kick",),
    },
    "webhook": {
        "label": "Webhooks",
        "help": "Prevent mass webhook creation",
        "actions": ("webhook_create",),
    },
    "vanity": {
        "label": "Vanity",
        "help": "Punish users that change the server vanity or profile",
        "actions": ("vanity", "guild_update"),
    },
    "botadd": {
        "label": "Bot additions",
        "help": "Prevent new bot additions",
        "actions": ("bot_add",),
    },
    "emoji": {
        "label": "Emojis",
        "help": "Prevent mass emoji deletion",
        "actions": ("emoji_delete",),
    },
}

#: action key -> module that has to be armed for it to be enforced.
ACTION_MODULE: dict[str, str] = {
    action: module
    for module, _spec in MODULES.items()
    for action in _spec["actions"]
}

#: Words accepted by the ``antinuke <module>`` switches.
ON_WORDS = frozenset({"on", "enable", "enabled", "true", "yes", "1"})
OFF_WORDS = frozenset({"off", "disable", "disabled", "false", "no", "0"})

PUNISHMENTS = ("ban", "kick", "strip")
HONEYPOT_ACTIONS = ("ban", "kick", "timeout", "jail", "none")

#: honeypot action -> how the bait card words the punishment.
HONEYPOT_ACTION_TEXT: dict[str, str] = {
    "ban": "a **ban**",
    "kick": "a **kick**",
    "timeout": "a **10 minute timeout**",
    "jail": "a **jail**",
    "none": "being **logged**",
}

#: Permissions that make a role dangerous to hand out or newly grant.
DANGEROUS_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("administrator", "Administrator"),
    ("manage_guild", "Manage Server"),
    ("manage_roles", "Manage Roles"),
    ("manage_channels", "Manage Channels"),
    ("manage_webhooks", "Manage Webhooks"),
    ("ban_members", "Ban Members"),
    ("kick_members", "Kick Members"),
)

DEFAULT_WINDOW = 10
#: Fresh enough that an audit-log entry belongs to the gateway event we saw.
AUDIT_WINDOW = 15.0
#: Don't re-punish the same actor within this many seconds.
PUNISH_COOLDOWN = 60.0
#: Ignore repeated honeypot messages from the same user for this long.
HONEYPOT_DEDUP = 30.0

RED = discord.Color.from_str("#ed4245")
GREEN = discord.Color.from_str("#57f287")
ORANGE = discord.Color.from_str("#e67e22")
BLURPLE = discord.Color.from_str("#5865f2")
GOLD = discord.Color.from_str("#f1c40f")


def _dedup_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _antinuke_admin_only() -> Any:
    """Server owner, a stored anti-nuke admin, or anyone with Administrator.

    ``antinuke admin @user`` hands out settings access without needing the
    Administrator permission, mirroring Bleed's antinuke admin list.
    """

    async def predicate(ctx: commands.Context) -> bool:
        cog = ctx.bot.get_cog("AntiNuke")
        if cog is not None and cog.is_settings_admin(ctx):
            return True
        raise commands.MissingPermissions(["administrator"])

    return commands.check(predicate)


def _guild_owner_only() -> Any:
    """Only the server owner (or a bot owner) may hand out anti-nuke admin."""

    async def predicate(ctx: commands.Context) -> bool:
        guild = getattr(ctx, "guild", None)
        author = getattr(ctx, "author", None)
        if guild is not None and getattr(author, "id", None) == guild.owner_id:
            return True
        if author is not None and await ctx.bot.is_owner(author):
            return True
        raise commands.CheckFailure("Only the server owner can do that.")

    return commands.check(predicate)


class AntiNuke(commands.Cog, name="AntiNuke"):
    """Threshold-based anti-nuke protection and honeypot channel bait."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # (guild_id, actor_id, action) -> deque[perf_counter timestamps]
        self._events: dict[tuple[int, int, str], deque[float]] = defaultdict(deque)
        # (guild_id, actor_id) -> last time we punished, to avoid repeats
        self._punished: dict[tuple[int, int], float] = {}
        # (guild_id, user_id) -> last honeypot message, to avoid double counting
        self._honeypot_seen: dict[tuple[int, int], float] = {}

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await check_slash_guardrails(self, interaction)

    # ------------------------------------------------------------------
    # Settings helpers
    # ------------------------------------------------------------------
    @property
    def settings(self):
        return getattr(self.bot, "dash_settings", None)

    def _enabled(self, guild_id: int) -> bool:
        if self.settings is None:
            return False
        return self.settings.flag(ENABLED_KEY, False, guild_id=guild_id)

    def _module_enabled(self, module: str, guild_id: int) -> bool:
        """Whether one protection module is armed (they default to on)."""
        if self.settings is None:
            return False
        return self.settings.flag(
            MODULE_KEY.format(module=module), True, guild_id=guild_id
        )

    def _action_enabled(self, action: str, guild_id: int) -> bool:
        module = ACTION_MODULE.get(action)
        return module is None or self._module_enabled(module, guild_id)

    def is_settings_admin(self, ctx: commands.Context) -> bool:
        """Owner, a stored anti-nuke admin, or anyone with Administrator."""
        guild = ctx.guild
        author = ctx.author
        if guild is None or getattr(author, "id", None) is None:
            return False
        if author.id == guild.owner_id:
            return True
        if author.id in self._stored_list(ADMINS_KEY, guild.id):
            return True
        permissions = getattr(author, "guild_permissions", None)
        return bool(permissions and permissions.administrator)

    def _stored_int(self, key: str, guild_id: int) -> Optional[int]:
        if self.settings is None:
            return None
        return _dedup_int(self.settings.stored(key, guild_id=guild_id))

    def _stored_list(self, key: str, guild_id: int) -> list[int]:
        if self.settings is None:
            return []
        value = self.settings.stored(key, guild_id=guild_id)
        if not isinstance(value, (list, tuple)):
            return []
        found: list[int] = []
        for item in value:
            parsed = _dedup_int(item)
            if parsed is not None:
                found.append(parsed)
        return found

    def _threshold(self, action: str, guild_id: int) -> int:
        value = self._stored_int(THRESHOLD_KEY.format(action=action), guild_id)
        if value is None:
            return DEFAULT_THRESHOLDS.get(action, 3)
        # 0 is meaningful: the very first action crosses the threshold.
        return max(0, value)

    def _window(self, guild_id: int) -> float:
        value = self._stored_int(WINDOW_KEY, guild_id)
        if value is None or value <= 0:
            return float(DEFAULT_WINDOW)
        return float(value)

    def _punishment(self, guild_id: int) -> str:
        if self.settings is None:
            return "ban"
        value = self.settings.stored(PUNISHMENT_KEY, guild_id=guild_id)
        return value if value in PUNISHMENTS else "ban"

    def _is_bot_staff(self, user: Any) -> bool:
        """Bot owners and the bot itself are always exempt."""
        if user is None:
            return False
        if getattr(self.bot, "user", None) is not None and user.id == self.bot.user.id:
            return True
        owners = getattr(self.bot, "owner_ids", None) or set()
        return user.id in owners

    def _is_exempt(self, guild: discord.Guild, user: Any, *, honeypot: bool = False) -> bool:
        """Return True when *user* is allowed to do protected/honeypot actions."""
        if user is None:
            return True
        if self._is_bot_staff(user):
            return True
        if guild.owner_id == getattr(user, "id", None):
            return True
        # Bots are exempt from anti-nuke (they are invited and act through
        # their own account) — but the honeypot exists specifically to catch
        # bots and scripts, so they stay fair game there.
        if getattr(user, "bot", False) and not honeypot:
            return True

        trusted = self._stored_list(TRUSTED_USERS_KEY, guild.id)
        mods = self._stored_list(HONEYPOT_MODS_KEY, guild.id) if honeypot else []
        if user.id in trusted or user.id in mods:
            return True

        roles = getattr(user, "roles", None)
        if roles:
            trusted_roles = set(self._stored_list(TRUSTED_ROLES_KEY, guild.id))
            if trusted_roles and any(role.id in trusted_roles for role in roles):
                return True
        return False

    # ------------------------------------------------------------------
    # Audit-log helpers
    # ------------------------------------------------------------------
    async def _recent_audit(
        self, guild: discord.Guild, action: discord.AuditLogAction, target_id: int | None = None
    ) -> Any:
        me = guild.me
        if me is None or not me.guild_permissions.view_audit_log:
            return None
        try:
            async for entry in guild.audit_logs(limit=8, action=action):
                age = (datetime.now(timezone.utc) - entry.created_at).total_seconds()
                if age > AUDIT_WINDOW:
                    break
                if target_id is not None and getattr(entry.target, "id", None) != target_id:
                    continue
                return entry
        except (discord.Forbidden, discord.HTTPException):
            return None
        return None

    def _record(self, guild_id: int, actor_id: int, action: str) -> int:
        """Add one action to the sliding window and return its count."""
        now = time.perf_counter()
        cutoff = now - self._window(guild_id)
        bucket = self._events[(guild_id, actor_id, action)]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        bucket.append(now)
        return len(bucket)

    def _is_punished_recently(self, guild_id: int, actor_id: int) -> bool:
        last = self._punished.get((guild_id, actor_id), 0.0)
        return (time.perf_counter() - last) < PUNISH_COOLDOWN

    # ------------------------------------------------------------------
    # Output helpers
    # ------------------------------------------------------------------
    def _card(
        self,
        title: str,
        color: discord.Color,
        *,
        description: str | None = None,
        fields: list[tuple[str, Any, bool]] | None = None,
        thumbnail: str | None = None,
        footer: str | None = None,
    ) -> discord.Embed:
        card = discord.Embed(
            title=title, color=color, timestamp=datetime.now(timezone.utc)
        )
        if description:
            card.description = description[:4096]
        for name, value, inline in fields or []:
            card.add_field(name=name, value=str(value)[:1024] or "—", inline=inline)
        if thumbnail:
            card.set_thumbnail(url=thumbnail)
        if footer:
            card.set_footer(text=footer[:2048])
        return card

    async def _mirror_to_serverlogs(self, guild: discord.Guild, card: discord.Embed) -> None:
        """Best-effort mirror into the server-logs moderation feed."""
        cog = self.bot.get_cog("ServerLogs")
        if cog is None:
            return
        log = getattr(cog, "_log", None)
        if log is None:
            return
        try:
            await log(guild, "moderation", card)
        except Exception:  # pragma: no cover - logging must never break protection
            logger.debug("Could not mirror an anti-nuke card to server logs", exc_info=True)

    async def _log_incident(self, guild: discord.Guild, card: discord.Embed) -> None:
        channel_id = self._stored_int(LOG_CHANNEL_KEY, guild.id)
        if channel_id is not None:
            channel = guild.get_channel(channel_id)
            if isinstance(channel, discord.abc.Messageable):
                try:
                    await channel.send(embed=card)
                except (discord.Forbidden, discord.HTTPException):
                    logger.debug("Could not post an anti-nuke card in %s", guild.id)
        await self._mirror_to_serverlogs(guild, card)

    # ------------------------------------------------------------------
    # Enforcement
    # ------------------------------------------------------------------
    async def _punish(
        self, guild: discord.Guild, actor: Any, action: str, count: int
    ) -> str:
        """Apply the configured punishment. Returns a human summary."""
        punishment = self._punishment(guild.id)
        reason = f"AntiNuke: {ACTION_LABELS.get(action, action)} exceeded threshold ({count})"
        member = guild.get_member(getattr(actor, "id", 0))

        try:
            if punishment == "ban":
                if member is not None:
                    await guild.ban(member, reason=reason, delete_message_seconds=0)
                else:
                    await guild.ban(discord.Object(id=actor.id), reason=reason)
                return "Banned"
            if punishment == "kick":
                if member is not None:
                    await member.kick(reason=reason)
                    return "Kicked"
                return "Could not kick (member left)"
            if punishment == "strip":
                if member is None:
                    return "Could not strip roles (member left)"
                me = guild.me
                roles = [
                    role
                    for role in member.roles
                    if role != guild.default_role
                    and not role.managed
                    and (me is None or role < me.top_role)
                ]
                if roles:
                    await member.remove_roles(*roles, reason=reason)
                return "Roles stripped"
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("AntiNuke punishment failed in %s: %s", guild.id, exc)
            return f"Punishment failed ({type(exc).__name__})"
        return "No action"

    async def _enforce(
        self,
        guild: discord.Guild,
        action: str,
        target: Any,
        audit_action: discord.AuditLogAction,
        target_id: int | None,
        *,
        detail: str | None = None,
    ) -> None:
        """Attribute an event to its actor and punish at most once per window."""
        if guild is None or not self._enabled(guild.id):
            return
        if not self._action_enabled(action, guild.id):
            return
        actor = await self._recent_audit(guild, audit_action, target_id)
        actor_user = getattr(actor, "user", None) if actor is not None else None
        if actor_user is None or self._is_exempt(guild, actor_user):
            return

        count = self._record(guild.id, actor_user.id, action)
        if count <= self._threshold(action, guild.id):
            return
        if self._is_punished_recently(guild.id, actor_user.id):
            return

        self._punished[(guild.id, actor_user.id)] = time.perf_counter()
        outcome = await self._punish(guild, actor_user, action, count)
        target_text = getattr(target, "mention", None) or getattr(target, "name", None)
        if target_text is None and hasattr(target, "id"):
            target_text = f"`{target.id}`"

        card = self._card(
            f"🛡️ AntiNuke — {ACTION_LABELS.get(action, action)}",
            RED,
            description=detail or f"{actor_user.mention} (`{actor_user.id}`) triggered the {action} threshold.",
            fields=[
                ("Action", f"`{action}` ×**{count}** in {self._window(guild.id):.0f}s", True),
                ("Punishment", outcome, True),
                ("Target", target_text or "—", True),
            ],
            thumbnail=getattr(getattr(actor_user, "display_avatar", None), "url", None),
            footer=f"User {actor_user.id} · guild {guild.id}",
        )
        await self._log_incident(guild, card)

    # ------------------------------------------------------------------
    # Protection listeners
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        await self._enforce(
            channel.guild,
            "channel_create",
            channel,
            discord.AuditLogAction.channel_create,
            channel.id,
        )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        await self._enforce(
            channel.guild,
            "channel_delete",
            channel,
            discord.AuditLogAction.channel_delete,
            channel.id,
        )

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        await self._enforce(
            role.guild,
            "role_create",
            role,
            discord.AuditLogAction.role_create,
            role.id,
        )

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        await self._enforce(
            role.guild,
            "role_delete",
            role,
            discord.AuditLogAction.role_delete,
            role.id,
        )

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        granted = self._dangerous_grants(
            getattr(before, "permissions", None), getattr(after, "permissions", None)
        )
        if not granted:
            return
        await self._enforce(
            after.guild,
            "role_update",
            after,
            discord.AuditLogAction.role_update,
            after.id,
            detail=(
                f"{after.mention} (`{after.id}`) was granted "
                f"**{', '.join(granted)}**."
            ),
        )

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: Any) -> None:
        await self._enforce(
            guild,
            "ban",
            user,
            discord.AuditLogAction.ban,
            getattr(user, "id", None),
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        # Only kicks carry a kick audit entry; ordinary leaves are ignored.
        await self._enforce(
            member.guild,
            "kick",
            member,
            discord.AuditLogAction.kick,
            member.id,
        )

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel: discord.abc.GuildChannel) -> None:
        guild = channel.guild
        if guild is None or not self._enabled(guild.id):
            return
        if not self._action_enabled("webhook_create", guild.id):
            return
        entry = await self._recent_audit(
            guild, discord.AuditLogAction.webhook_create, None
        )
        actor_user = getattr(entry, "user", None) if entry is not None else None
        if actor_user is None or self._is_exempt(guild, actor_user):
            return
        count = self._record(guild.id, actor_user.id, "webhook_create")
        if count <= self._threshold("webhook_create", guild.id):
            return
        if self._is_punished_recently(guild.id, actor_user.id):
            return
        self._punished[(guild.id, actor_user.id)] = time.perf_counter()
        outcome = await self._punish(guild, actor_user, "webhook_create", count)
        await self._log_incident(
            guild,
            self._card(
                "🛡️ AntiNuke — Webhooks created",
                RED,
                description=f"{actor_user.mention} created webhooks in {channel.mention}.",
                fields=[
                    ("Count", f"**{count}** in {self._window(guild.id):.0f}s", True),
                    ("Punishment", outcome, True),
                ],
                thumbnail=getattr(getattr(actor_user, "display_avatar", None), "url", None),
                footer=f"User {actor_user.id} · guild {guild.id}",
            ),
        )

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        if before.vanity_url_code != after.vanity_url_code:
            await self._enforce(
                after,
                "vanity",
                after,
                discord.AuditLogAction.guild_update,
                None,
                detail=(
                    f"Vanity URL changed from `{before.vanity_url_code or 'none'}` "
                    f"to `{after.vanity_url_code or 'none'}`."
                ),
            )
            return
        changes: list[str] = []
        if before.name != after.name:
            changes.append(f"name `{before.name}` → `{after.name}`")
        if not changes:
            return
        await self._enforce(
            after,
            "guild_update",
            after,
            discord.AuditLogAction.guild_update,
            None,
            detail=f"{after.name} was edited: {', '.join(changes)}.",
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Punish whoever invited a bot when the botadd module is armed."""
        if not member.bot or member.guild is None:
            return
        # A whitelisted bot may be invited without punishing the inviter.
        if member.id in self._stored_list(TRUSTED_USERS_KEY, member.guild.id):
            return
        await self._enforce(
            member.guild,
            "bot_add",
            member,
            discord.AuditLogAction.bot_add,
            member.id,
            detail=f"Bot {member.mention} (`{member.id}`) was added to the server.",
        )

    @commands.Cog.listener()
    async def on_guild_emojis_update(
        self, guild: discord.Guild, before: Any, after: Any
    ) -> None:
        remaining = {getattr(emoji, "id", None) for emoji in after}
        removed = [emoji for emoji in before if getattr(emoji, "id", None) not in remaining]
        if not removed:
            return
        first = removed[0]
        names = ", ".join(f"`{emoji.name}`" for emoji in removed[:5])
        extra = "" if len(removed) <= 5 else f" and {len(removed) - 5} more"
        await self._enforce(
            guild,
            "emoji_delete",
            first,
            discord.AuditLogAction.emoji_delete,
            getattr(first, "id", None),
            detail=f"Emojis deleted: {names}{extra}.",
        )

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        added = [role for role in after.roles if role not in before.roles]
        if not added:
            return
        dangerous = [role for role in added if self._role_is_dangerous(role)]
        if not dangerous:
            return
        names = ", ".join(role.mention for role in dangerous)
        await self._enforce(
            after.guild,
            "member_role_update",
            after,
            discord.AuditLogAction.member_role_update,
            after.id,
            detail=f"{after.mention} (`{after.id}`) was given {names}.",
        )

    @staticmethod
    def _dangerous_grants(before: Any, after: Any) -> list[str]:
        if before is None or after is None:
            return []
        granted: list[str] = []
        for attr, label in DANGEROUS_PERMISSIONS:
            if getattr(after, attr, False) and not getattr(before, attr, False):
                granted.append(label)
        return granted

    @staticmethod
    def _role_is_dangerous(role: discord.Role) -> bool:
        perms = getattr(role, "permissions", None)
        if perms is None:
            return False
        return any(getattr(perms, attr, False) for attr, _ in DANGEROUS_PERMISSIONS)

    # ==================================================================
    # Honeypot
    # ==================================================================
    def _honeypot_channel_id(self, guild_id: int) -> Optional[int]:
        return self._stored_int(HONEYPOT_CHANNEL_KEY, guild_id)

    def _honeypot_action(self, guild_id: int) -> str:
        if self.settings is None:
            return "ban"
        value = self.settings.stored(HONEYPOT_ACTION_KEY, guild_id=guild_id)
        return value if value in HONEYPOT_ACTIONS else "ban"

    def _honeypot_caught(self, guild_id: int) -> int:
        return self._stored_int(HONEYPOT_CAUGHT_KEY, guild_id) or 0

    def _honeypot_message_id(self, guild_id: int) -> Optional[int]:
        return self._stored_int(HONEYPOT_MESSAGE_KEY, guild_id)

    def _honeypot_bait_embed(self, guild_id: int) -> discord.Embed:
        """The one message the honeypot channel holds, kept current in place."""
        punishment = HONEYPOT_ACTION_TEXT.get(
            self._honeypot_action(guild_id), "a **punishment**"
        )
        return discord.Embed(
            title="⚠️ DO NOT SEND MESSAGES HERE! ⚠️",
            color=GOLD,
            description=(
                "> This channel is a trap for compromised accounts.\n"
                f"> Sending any message here will result in {punishment}.\n"
                "\n"
                f"🍯 Accounts Caught: **{self._honeypot_caught(guild_id)}**"
            ),
        )

    async def _honeypot_sync_card(
        self, guild: discord.Guild
    ) -> Optional[discord.Message]:
        """Edit the honeypot's single card; only post when it is genuinely gone.

        Nothing else may be sent into a honeypot channel, so this is the only
        place in the cog that writes there outside of the initial setup.
        """
        channel_id = self._honeypot_channel_id(guild.id)
        if channel_id is None:
            return None

        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        if not isinstance(channel, discord.abc.Messageable):
            return None

        card = self._honeypot_bait_embed(guild.id)
        message_id = self._honeypot_message_id(guild.id)
        if message_id is not None:
            try:
                if hasattr(channel, "get_partial_message"):
                    return await channel.get_partial_message(message_id).edit(embed=card)
                stored = await channel.fetch_message(message_id)
                return await stored.edit(embed=card)
            except discord.NotFound:
                pass
            except (discord.Forbidden, discord.HTTPException):
                logger.debug("Could not edit the honeypot card in %s", guild.id)
                return None

        # First setup, or an admin deleted the card. Post it once and remember it.
        try:
            message = await channel.send(embed=card)
        except (discord.Forbidden, discord.HTTPException):
            logger.debug("Could not post the honeypot card in %s", guild.id)
            return None
        if self.settings is not None:
            try:
                await self.settings.set_value(
                    HONEYPOT_MESSAGE_KEY, message.id, guild_id=guild.id
                )
            except Exception:
                logger.debug("Could not persist the honeypot card id", exc_info=True)
        return message

    def _honeypot_embed(
        self, member: Any, count: int, action: str, channel_id: int
    ) -> discord.Embed:
        name = getattr(member, "display_name", None) or str(member)
        return self._card(
            "🍯 Honeypot Triggered",
            RED,
            description=(
                f"**{name}** (`{getattr(member, 'id', '?')}`) sent a message in "
                f"<#{channel_id}> and was caught.\n\n"
                f"# USERS CAUGHT: {count}"
            ),
            fields=[
                ("User", f"{member.mention} (`{getattr(member, 'id', '?')}`)", True),
                ("Action Taken", action.title(), True),
                ("USERS CAUGHT", str(count), True),
            ],
            thumbnail=getattr(getattr(member, "display_avatar", None), "url", None),
            footer=f"User {getattr(member, 'id', '?')}",
        )

    async def _honeypot_apply(
        self, guild: discord.Guild, member: Any, action: str, reason: str
    ) -> str:
        resolved = guild.get_member(getattr(member, "id", 0))
        try:
            if action == "ban":
                if resolved is not None:
                    await guild.ban(resolved, reason=reason, delete_message_seconds=0)
                else:
                    await guild.ban(discord.Object(id=member.id), reason=reason)
                return "ban"
            if action == "kick":
                if resolved is not None:
                    await resolved.kick(reason=reason)
                return "kick"
            if action == "timeout":
                if resolved is not None:
                    await resolved.timeout(
                        discord.utils.utcnow() + _duration_delta(), reason=reason
                    )
                return "timeout"
            if action == "jail":
                if resolved is None:
                    return "none"
                moderation = self.bot.get_cog("Moderation")
                if moderation is None or not hasattr(moderation, "jail_member"):
                    logger.warning(
                        "Honeypot jail action requested but Moderation is unavailable"
                    )
                    return "none"
                applied, _ = await moderation.jail_member(
                    guild, resolved, moderator=self.bot.user, reason=reason
                )
                return "jail" if applied else "none"
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("Honeypot action failed in %s: %s", guild.id, exc)
        return "none"

    async def _honeypot_notify_mods(
        self, guild: discord.Guild, embed: discord.Embed
    ) -> None:
        for mod_id in self._stored_list(HONEYPOT_MODS_KEY, guild.id):
            if mod_id in (getattr(self.bot, "owner_ids", None) or set()):
                continue
            recipient = guild.get_member(mod_id) or self.bot.get_user(mod_id)
            if recipient is None:
                continue
            try:
                await recipient.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                logger.debug("Could not DM honeypot mod %s", mod_id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or not message.author:
            return
        if self.bot.user is not None and message.author.id == self.bot.user.id:
            return
        guild = message.guild
        if self.settings is None:
            return

        channel_id = self._honeypot_channel_id(guild.id)
        if channel_id is None or message.channel.id != channel_id:
            return
        if not self.settings.flag(HONEYPOT_ENABLED_KEY, True, guild_id=guild.id):
            return
        if self._is_exempt(guild, message.author, honeypot=True):
            return

        # A burst of messages from one compromised account is one catch.
        now = time.perf_counter()
        key = (guild.id, message.author.id)
        if now - self._honeypot_seen.get(key, 0.0) < HONEYPOT_DEDUP:
            try:
                await message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass
            return
        self._honeypot_seen[key] = now

        try:
            await message.delete()
        except (discord.Forbidden, discord.HTTPException):
            logger.debug("Could not delete a honeypot message in %s", guild.id)

        count = self._honeypot_caught(guild.id) + 1
        action = self._honeypot_action(guild.id)
        reason = "Honeypot: posted in a bait channel"
        applied = await self._honeypot_apply(guild, message.author, action, reason)
        if action == "none":
            applied = "logged only"

        # Persist the counter before announcing, so the number is authoritative.
        try:
            await self.settings.set_value(HONEYPOT_CAUGHT_KEY, count, guild_id=guild.id)
        except Exception:
            logger.debug("Could not persist the honeypot counter", exc_info=True)

        card = self._honeypot_embed(message.author, count, applied, channel_id)

        # The channel keeps exactly one message: edit its counter in place rather
        # than posting a catch card into the bait channel.
        await self._honeypot_sync_card(guild)
        await self._honeypot_notify_mods(guild, card)

        if action != "none":
            # _log_incident already mirrors into the server-logs feed.
            await self._log_incident(guild, card)
        else:
            await self._mirror_to_serverlogs(guild, card)

    # ==================================================================
    # Anti-nuke commands
    # ==================================================================
    @commands.hybrid_group(
        name="antinuke",
        aliases=["an"],
        invoke_without_command=True,
        description="Threshold-based server protection.",
    )
    @commands.guild_only()
    @_antinuke_admin_only()
    @discord.app_commands.default_permissions(administrator=True)
    async def antinuke(self, ctx: commands.Context) -> None:
        """Show the anti-nuke command overview."""
        prefix = "/"
        if ctx.message is not None:
            raw = await self.bot.get_prefix(ctx.message)
            prefix = raw[0] if isinstance(raw, list) else raw
        lines = [
            f"`{prefix}antinuke enable|disable` — toggle protection",
            f"`{prefix}antinuke status` · `config` — every switch and threshold",
            "",
            "**Modules** — `on`/`off`, or nothing to see the state",
            f"`{prefix}antinuke channel [on|off]` — channel create and delete",
            f"`{prefix}antinuke role [on|off]` — role create and delete",
            f"`{prefix}antinuke permissions [on|off]` — dangerous grants",
            f"`{prefix}antinuke ban [on|off]` · `kick [on|off]`",
            f"`{prefix}antinuke webhook [on|off]` — mass webhook creation",
            f"`{prefix}antinuke vanity [on|off]` — vanity and profile edits",
            f"`{prefix}antinuke botadd [on|off]` — new bot additions",
            f"`{prefix}antinuke emoji [on|off]` — mass emoji deletion",
            f"`{prefix}antinuke list` — armed modules and the whitelist",
            "",
            "**Punishment**",
            f"`{prefix}antinuke punishment ban|kick|strip`",
            f"`{prefix}antinuke threshold <action> <count>` · `window <seconds>`",
            f"`{prefix}antinuke log #channel` — where incidents are posted",
            "",
            "**Access and exemptions**",
            f"`{prefix}antinuke admin @user` · `admins` — who may edit settings",
            f"`{prefix}antinuke whitelist @user` — exempt a member or bot",
            f"`{prefix}antinuke trust role add|remove <role>` · `trustlist`",
            f"`{prefix}antinuke reset` — clear counters and the trusted list",
        ]
        await Embeds.blurple(
            ctx,
            "\n".join(lines),
            title="🛡️ AntiNuke",
            delete_after=None,
        )

    @antinuke.command(name="enable")
    @_antinuke_admin_only()
    @discord.app_commands.default_permissions(administrator=True)
    async def antinuke_enable(self, ctx: commands.Context) -> None:
        """Turn anti-nuke protection on for this server."""
        if self.settings is None:
            return await Embeds.error(ctx, "The settings store isn't ready yet.", delete_after=10)
        await self.settings.set_flag(ENABLED_KEY, True, guild_id=ctx.guild.id)
        await Embeds.success(
            ctx, "AntiNuke protection is now **enabled**.", delete_after=10
        )

    @antinuke.command(name="disable")
    @_antinuke_admin_only()
    @discord.app_commands.default_permissions(administrator=True)
    async def antinuke_disable(self, ctx: commands.Context) -> None:
        """Turn anti-nuke protection off for this server."""
        if self.settings is None:
            return await Embeds.error(ctx, "The settings store isn't ready yet.", delete_after=10)
        await self.settings.set_flag(ENABLED_KEY, False, guild_id=ctx.guild.id)
        await Embeds.warning(
            ctx, "AntiNuke protection is now **disabled**.", delete_after=10
        )

    @antinuke.command(name="log", aliases=["logchannel"])
    @_antinuke_admin_only()
    @discord.app_commands.default_permissions(administrator=True)
    async def antinuke_log(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Set the channel anti-nuke incidents are posted to."""
        if self.settings is None:
            return await Embeds.error(ctx, "The settings store isn't ready yet.", delete_after=10)
        await self.settings.set_value(LOG_CHANNEL_KEY, channel.id, guild_id=ctx.guild.id)
        await Embeds.success(
            ctx, f"AntiNuke incidents will post in {channel.mention}.", delete_after=10
        )

    @antinuke.command(name="punishment")
    @_antinuke_admin_only()
    @discord.app_commands.default_permissions(administrator=True)
    async def antinuke_punishment(
        self, ctx: commands.Context, punishment: str
    ) -> None:
        """Choose what happens to a nuker: ban, kick or strip."""
        choice = (punishment or "").strip().lower()
        if choice not in PUNISHMENTS:
            return await Embeds.error(
                ctx,
                f"Pick one of: {', '.join(PUNISHMENTS)}.",
                delete_after=10,
            )
        if self.settings is None:
            return await Embeds.error(ctx, "The settings store isn't ready yet.", delete_after=10)
        await self.settings.set_value(PUNISHMENT_KEY, choice, guild_id=ctx.guild.id)
        await Embeds.success(
            ctx, f"Punishment set to **{choice}**.", delete_after=10
        )

    @antinuke.command(name="threshold")
    @_antinuke_admin_only()
    @discord.app_commands.default_permissions(administrator=True)
    async def antinuke_threshold(
        self, ctx: commands.Context, action: str, count: int
    ) -> None:
        """Set how many <action>s are allowed in the window before punishment."""
        key = (action or "").strip().lower()
        if key not in DEFAULT_THRESHOLDS:
            return await Embeds.error(
                ctx,
                "Valid actions: " + ", ".join(f"`{a}`" for a in DEFAULT_THRESHOLDS),
                delete_after=15,
            )
        if count < 0:
            return await Embeds.error(
                ctx,
                "The threshold can't be negative — use `0` to punish the very "
                "first action.",
                delete_after=10,
            )
        if self.settings is None:
            return await Embeds.error(ctx, "The settings store isn't ready yet.", delete_after=10)
        await self.settings.set_value(
            THRESHOLD_KEY.format(action=key), int(count), guild_id=ctx.guild.id
        )
        await Embeds.success(
            ctx,
            f"Threshold for **{ACTION_LABELS.get(key, key)}** set to **{count}** per window.",
            delete_after=10,
        )

    @antinuke.command(name="window")
    @_antinuke_admin_only()
    @discord.app_commands.default_permissions(administrator=True)
    async def antinuke_window(self, ctx: commands.Context, seconds: int) -> None:
        """Set the length of the sliding window, in seconds."""
        if seconds < 1:
            return await Embeds.error(ctx, "The window must be at least 1 second.", delete_after=10)
        if self.settings is None:
            return await Embeds.error(ctx, "The settings store isn't ready yet.", delete_after=10)
        await self.settings.set_value(WINDOW_KEY, int(seconds), guild_id=ctx.guild.id)
        await Embeds.success(
            ctx, f"Protection window set to **{seconds}s**.", delete_after=10
        )

    @antinuke.command(name="trustlist")
    @_antinuke_admin_only()
    @discord.app_commands.default_permissions(administrator=True)
    async def antinuke_trustlist(self, ctx: commands.Context) -> None:
        """Show the users and roles exempt from anti-nuke protection."""
        await self._send_trustlist(ctx)

    async def _send_trustlist(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        users = self._stored_list(TRUSTED_USERS_KEY, guild.id)
        roles = self._stored_list(TRUSTED_ROLES_KEY, guild.id)
        user_text = (
            "\n".join(self._user_line(guild, uid) for uid in users) if users else "*none*"
        )
        role_text = (
            "\n".join(f"• <@&{rid}> (`{rid}`)" for rid in roles) if roles else "*none*"
        )
        await Embeds.info(
            ctx,
            f"**Whitelisted members and bots**\n{user_text}\n\n**Trusted roles**\n{role_text}",
            title="🛡️ AntiNuke whitelist",
            delete_after=None,
        )

    def _user_line(self, guild: discord.Guild, user_id: int) -> str:
        member = guild.get_member(user_id) if guild is not None else None
        bot = " 🤖" if getattr(member, "bot", False) else ""
        return f"• <@{user_id}> (`{user_id}`){bot}"

    @antinuke.command(name="reset")
    @_antinuke_admin_only()
    @discord.app_commands.default_permissions(administrator=True)
    async def antinuke_reset(self, ctx: commands.Context) -> None:
        """Clear the sliding-window counters and trusted lists."""
        self._events.clear()
        self._punished.clear()
        if self.settings is not None:
            await self.settings.set_value(TRUSTED_USERS_KEY, [], guild_id=ctx.guild.id)
            await self.settings.set_value(TRUSTED_ROLES_KEY, [], guild_id=ctx.guild.id)
        await Embeds.success(ctx, "AntiNuke counters and trust list cleared.", delete_after=10)

    @antinuke.command(name="status")
    @_antinuke_admin_only()
    @discord.app_commands.default_permissions(administrator=True)
    async def antinuke_status(self, ctx: commands.Context) -> None:
        """Show the current anti-nuke configuration."""
        await ctx.send(embed=self._status_embed(ctx.guild))

    @antinuke.command(name="config")
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.default_permissions(manage_guild=True)
    async def antinuke_config(self, ctx: commands.Context) -> None:
        """View this server's antinuke configuration."""
        await ctx.send(embed=self._status_embed(ctx.guild))

    def _status_embed(self, guild: discord.Guild) -> discord.Embed:
        enabled = self._enabled(guild.id)
        log_id = self._stored_int(LOG_CHANNEL_KEY, guild.id)
        log_text = f"<#{log_id}>" if log_id else "*using server logs*"
        threshold_lines = []
        for action, label in ACTION_LABELS.items():
            threshold_lines.append(
                f"**{label}** · {self._threshold(action, guild.id)} per {self._window(guild.id):.0f}s"
            )
        honeypot_channel = self._honeypot_channel_id(guild.id)
        honeypot_text = (
            f"<#{honeypot_channel}> · {self._honeypot_caught(guild.id)} caught "
            f"· action `{self._honeypot_action(guild.id)}`"
            if honeypot_channel
            else "*not set up*"
        )
        admins = self._stored_list(ADMINS_KEY, guild.id)
        admin_text = f"{len(admins)} configured" if admins else "*owner only*"
        return self._card(
            "🛡️ AntiNuke Status",
            GREEN if enabled else RED,
            fields=[
                ("Protection", "✅ enabled" if enabled else "❌ disabled", True),
                ("Punishment", self._punishment(guild.id), True),
                ("Window", f"{self._window(guild.id):.0f}s", True),
                ("Log channel", log_text, True),
                ("Honeypot", honeypot_text, True),
                ("Admins", admin_text, True),
                ("Modules", self._module_lines(guild.id), False),
                ("Thresholds", "\n".join(threshold_lines), False),
            ],
        )

    # -- modules ---------------------------------------------------------
    def _module_lines(self, guild_id: int) -> str:
        lines = []
        for module, spec in MODULES.items():
            mark = "✅" if self._module_enabled(module, guild_id) else "❌"
            lines.append(f"{mark} **{spec['label']}** — {spec['help']}")
        return "\n".join(lines)

    async def _module_switch(
        self, ctx: commands.Context, module: str, status: str
    ) -> None:
        spec = MODULES[module]
        if self.settings is None:
            return await Embeds.error(ctx, "The settings store isn't ready yet.", delete_after=10)
        value = (status or "").strip().lower()
        prefix = await _prefix(self.bot, ctx)
        if not value:
            armed = self._module_enabled(module, ctx.guild.id)
            flip = "off" if armed else "on"
            return await Embeds.info(
                ctx,
                f"**{spec['help']}**\n"
                f"Status: {'✅ enabled' if armed else '❌ disabled'}\n"
                f"-# Toggle with `{prefix}antinuke {module} {flip}`.",
                title=f"🛡️ Antinuke · {spec['label']}",
                delete_after=None,
            )
        if value not in ON_WORDS and value not in OFF_WORDS:
            return await Embeds.error(
                ctx, "Use `on` or `off`.", delete_after=10
            )
        armed = value in ON_WORDS
        await self.settings.set_flag(
            MODULE_KEY.format(module=module), armed, guild_id=ctx.guild.id
        )
        await Embeds.success(
            ctx,
            f"🛡️ **{spec['label']}** protection is now "
            f"**{'enabled' if armed else 'disabled'}**.",
            delete_after=10,
        )

    @antinuke.command(name="webhook")
    @_antinuke_admin_only()
    @discord.app_commands.default_permissions(administrator=True)
    async def antinuke_webhook(self, ctx: commands.Context, status: str = "") -> None:
        """Prevent mass webhook creation."""
        await self._module_switch(ctx, "webhook", status)

    @antinuke.command(name="channel")
    @_antinuke_admin_only()
    @discord.app_commands.default_permissions(administrator=True)
    async def antinuke_channel(self, ctx: commands.Context, status: str = "") -> None:
        """Prevent mass channel creation and deletion."""
        await self._module_switch(ctx, "channel", status)

    @antinuke.command(name="role")
    @_antinuke_admin_only()
    @discord.app_commands.default_permissions(administrator=True)
    async def antinuke_role(self, ctx: commands.Context, status: str = "") -> None:
        """Prevent mass role creation and deletion."""
        await self._module_switch(ctx, "role", status)

    @antinuke.command(name="ban")
    @_antinuke_admin_only()
    @discord.app_commands.default_permissions(administrator=True)
    async def antinuke_ban(self, ctx: commands.Context, status: str = "") -> None:
        """Prevent mass member bans."""
        await self._module_switch(ctx, "ban", status)

    @antinuke.command(name="kick")
    @_antinuke_admin_only()
    @discord.app_commands.default_permissions(administrator=True)
    async def antinuke_kick(self, ctx: commands.Context, status: str = "") -> None:
        """Prevent mass member kicks."""
        await self._module_switch(ctx, "kick", status)

    @antinuke.command(name="vanity")
    @_antinuke_admin_only()
    @discord.app_commands.default_permissions(administrator=True)
    async def antinuke_vanity(self, ctx: commands.Context, status: str = "") -> None:
        """Punish users that change the server vanity or profile."""
        await self._module_switch(ctx, "vanity", status)

    @antinuke.command(name="botadd", aliases=["bot"])
    @_antinuke_admin_only()
    @discord.app_commands.default_permissions(administrator=True)
    async def antinuke_botadd(self, ctx: commands.Context, status: str = "") -> None:
        """Prevent new bot additions."""
        await self._module_switch(ctx, "botadd", status)

    @antinuke.command(name="emoji", aliases=["emojis"])
    @_antinuke_admin_only()
    @discord.app_commands.default_permissions(administrator=True)
    async def antinuke_emoji(self, ctx: commands.Context, status: str = "") -> None:
        """Prevent mass emoji deletion."""
        await self._module_switch(ctx, "emoji", status)

    @antinuke.command(name="permissions", aliases=["perms"])
    @_antinuke_admin_only()
    @discord.app_commands.default_permissions(administrator=True)
    async def antinuke_permissions(self, ctx: commands.Context, status: str = "") -> None:
        """Watch dangerous permissions being granted."""
        if (status or "").strip():
            return await self._module_switch(ctx, "permissions", status)
        watched = "\n".join(f"• {label}" for _attr, label in DANGEROUS_PERMISSIONS)
        armed = self._module_enabled("permissions", ctx.guild.id)
        prefix = await _prefix(self.bot, ctx)
        await Embeds.info(
            ctx,
            f"**Watched permissions**\n{watched}\n\n"
            f"Status: {'✅ enabled' if armed else '❌ disabled'}\n"
            f"-# Toggle with `{prefix}antinuke permissions "
            f"{'off' if armed else 'on'}`.",
            title="🛡️ Antinuke · Permissions",
            delete_after=None,
        )

    @antinuke.command(name="list")
    @_antinuke_admin_only()
    @discord.app_commands.default_permissions(administrator=True)
    async def antinuke_list(self, ctx: commands.Context) -> None:
        """View all modules and whitelisted members and bots."""
        guild = ctx.guild
        users = self._stored_list(TRUSTED_USERS_KEY, guild.id)
        whitelisted = (
            "\n".join(self._user_line(guild, uid) for uid in users) if users else "*none*"
        )
        await Embeds.info(
            ctx,
            f"**Modules**\n{self._module_lines(guild.id)}\n\n"
            f"**Whitelisted members and bots**\n{whitelisted}",
            title="🛡️ Antinuke modules",
            delete_after=None,
        )

    @antinuke.command(name="admin")
    @_guild_owner_only()
    @discord.app_commands.default_permissions(administrator=True)
    async def antinuke_admin(self, ctx: commands.Context, *, member: str = "") -> None:
        """Give a user access to the antinuke settings (owner only).

        Run it as `antinuke admin @user`, `antinuke admin remove @user`, or with
        no arguments to see who currently has access.
        """
        tokens = (member or "").split()
        action = "add"
        if tokens and tokens[0].lower() in ("remove", "revoke", "del", "delete"):
            action = "remove"
            tokens = tokens[1:]
        elif tokens and tokens[0].lower() in ("add", "allow"):
            tokens = tokens[1:]
        if not tokens:
            return await self._send_admins(ctx)
        user = await self._resolve_user(ctx, " ".join(tokens))
        if user is None:
            return await Embeds.error(
                ctx, "Mention the user or paste their id.", delete_after=10
            )
        if self.settings is None:
            return await Embeds.error(ctx, "The settings store isn't ready yet.", delete_after=10)
        admins = self._stored_list(ADMINS_KEY, ctx.guild.id)
        if action == "add":
            if user.id == ctx.guild.owner_id:
                return await Embeds.info(
                    ctx,
                    "The server owner already has full access.",
                    delete_after=10,
                )
            if user.id not in admins:
                admins.append(user.id)
            message = f"🛡️ <@{user.id}> can now edit the antinuke settings."
        else:
            admins = [uid for uid in admins if uid != user.id]
            message = f"🛡️ <@{user.id}> can no longer edit the antinuke settings."
        await self.settings.set_value(ADMINS_KEY, admins, guild_id=ctx.guild.id)
        await Embeds.success(ctx, message, delete_after=10)

    @antinuke.command(name="admins")
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.default_permissions(manage_guild=True)
    async def antinuke_admins(self, ctx: commands.Context) -> None:
        """View all antinuke admins."""
        await self._send_admins(ctx)

    async def _send_admins(self, ctx: commands.Context) -> None:
        admins = self._stored_list(ADMINS_KEY, ctx.guild.id)
        text = (
            "\n".join(self._user_line(ctx.guild, uid) for uid in admins)
            if admins
            else "*none besides the server owner*"
        )
        prefix = await _prefix(self.bot, ctx)
        await Embeds.info(
            ctx,
            f"**Owner**\n• <@{ctx.guild.owner_id}>\n\n"
            f"**Antinuke admins**\n{text}\n\n"
            f"-# Add one with `{prefix}antinuke admin @user`.",
            title="🛡️ Antinuke admins",
            delete_after=None,
        )

    @antinuke.command(name="whitelist")
    @_antinuke_admin_only()
    @discord.app_commands.default_permissions(administrator=True)
    async def antinuke_whitelist(self, ctx: commands.Context, *, member: str = "") -> None:
        """Whitelist a member (or a bot, so it may join) from antinuke.

        Run it as `antinuke whitelist @user`, `antinuke whitelist remove
        @user`, or with no arguments to see the list.
        """
        tokens = (member or "").split()
        action = "add"
        if tokens and tokens[0].lower() in ("remove", "revoke", "del", "delete"):
            action = "remove"
            tokens = tokens[1:]
        elif tokens and tokens[0].lower() in ("add", "allow"):
            tokens = tokens[1:]
        if not tokens:
            return await self._send_trustlist(ctx)
        user = await self._resolve_user(ctx, " ".join(tokens))
        if user is None:
            return await Embeds.error(
                ctx, "Mention the user or paste their id.", delete_after=10
            )
        await self._trust_change(ctx, user.id, add=action == "add")

    async def _resolve_user(
        self, ctx: commands.Context, text: str
    ) -> Optional[discord.abc.User]:
        """Resolve a mention or raw id, working for prefix and slash alike."""
        match = re.search(r"\d{15,20}", text or "")
        if match is None:
            return None
        user_id = int(match.group(0))
        guild = getattr(ctx, "guild", None)
        member = guild.get_member(user_id) if guild is not None else None
        if member is not None:
            return member
        cached = ctx.bot.get_user(user_id)
        if cached is not None:
            return cached
        try:
            return await ctx.bot.fetch_user(user_id)
        except (discord.NotFound, discord.HTTPException):
            return None

    # -- trust (users) ---------------------------------------------------
    @antinuke.group(name="trust", invoke_without_command=True)
    @_antinuke_admin_only()
    @discord.app_commands.default_permissions(administrator=True)
    async def antinuke_trust(self, ctx: commands.Context) -> None:
        """Manage users exempt from anti-nuke punishment."""
        await ctx.send(
            f"Use `{(await _prefix(self.bot, ctx))}antinuke trust add|remove <user>` "
            "or `... trust role add|remove <role>`."
        )

    @antinuke_trust.command(name="add")
    @_antinuke_admin_only()
    @discord.app_commands.default_permissions(administrator=True)
    async def antinuke_trust_add(self, ctx: commands.Context, user: discord.User) -> None:
        """Exempt a user from anti-nuke punishment."""
        await self._trust_change(ctx, user.id, add=True)

    @antinuke_trust.command(name="remove")
    @_antinuke_admin_only()
    @discord.app_commands.default_permissions(administrator=True)
    async def antinuke_trust_remove(self, ctx: commands.Context, user: discord.User) -> None:
        """Remove a user's exemption."""
        await self._trust_change(ctx, user.id, add=False)

    @antinuke_trust.command(name="role")
    @_antinuke_admin_only()
    @discord.app_commands.default_permissions(administrator=True)
    async def antinuke_trust_role(
        self, ctx: commands.Context, action: str, role: discord.Role
    ) -> None:
        """Exempt (or re-expose) everyone holding a role."""
        verb = (action or "").strip().lower()
        if verb not in ("add", "remove"):
            return await Embeds.error(ctx, "Use `add` or `remove`.", delete_after=10)
        if self.settings is None:
            return await Embeds.error(ctx, "The settings store isn't ready yet.", delete_after=10)
        roles = self._stored_list(TRUSTED_ROLES_KEY, ctx.guild.id)
        if verb == "add":
            if role.id not in roles:
                roles.append(role.id)
            message = f"✅ {role.mention} is now trusted."
        else:
            roles = [rid for rid in roles if rid != role.id]
            message = f"✅ {role.mention} is no longer trusted."
        await self.settings.set_value(TRUSTED_ROLES_KEY, roles, guild_id=ctx.guild.id)
        await Embeds.success(ctx, message, delete_after=10)

    async def _trust_change(self, ctx: commands.Context, user_id: int, *, add: bool) -> None:
        if self.settings is None:
            return await Embeds.error(ctx, "The settings store isn't ready yet.", delete_after=10)
        users = self._stored_list(TRUSTED_USERS_KEY, ctx.guild.id)
        if add:
            if user_id not in users:
                users.append(user_id)
            message = f"✅ <@{user_id}> is now trusted."
        else:
            users = [uid for uid in users if uid != user_id]
            message = f"✅ <@{user_id}> is no longer trusted."
        await self.settings.set_value(TRUSTED_USERS_KEY, users, guild_id=ctx.guild.id)
        await Embeds.success(ctx, message, delete_after=10)

    # ==================================================================
    # Honeypot commands
    # ==================================================================
    @commands.hybrid_group(
        name="honeypot",
        invoke_without_command=True,
        description="Bait channels that catch bots and scripts.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @discord.app_commands.default_permissions(administrator=True)
    async def honeypot(self, ctx: commands.Context) -> None:
        """Show the honeypot command overview."""
        prefix = await _prefix(self.bot, ctx)
        lines = [
            f"`{prefix}honeypot create [#channel]` — create or adopt a bait channel",
            f"`{prefix}honeypot mod add|remove <user>` — catch notifications",
            f"`{prefix}honeypot mod list`",
            f"`{prefix}honeypot action ban|kick|timeout|jail|none`",
            f"`{prefix}honeypot enable|disable`",
            f"`{prefix}honeypot status`",
            f"`{prefix}honeypot reset` — reset the caught counter",
        ]
        await Embeds.blurple(
            ctx, "\n".join(lines), title="🍯 Honeypot", delete_after=None
        )

    @honeypot.command(name="create")
    @commands.has_permissions(administrator=True)
    @commands.bot_has_permissions(manage_channels=True)
    @discord.app_commands.default_permissions(administrator=True)
    async def honeypot_create(
        self, ctx: commands.Context, channel: discord.TextChannel = None
    ) -> None:
        """Create a bait channel, or adopt an existing one.

        The channel warns that posting in it gets you banned, and the warning is
        pinned so it stays visible.
        """
        if self.settings is None:
            return await Embeds.error(ctx, "The settings store isn't ready yet.", delete_after=10)
        guild = ctx.guild
        created = False
        if channel is None:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                ),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    embed_links=True,
                    manage_messages=True,
                    read_message_history=True,
                ),
            }
            try:
                channel = await guild.create_text_channel(
                    "honeypot",
                    topic="Do not post here.",
                    overwrites=overwrites,
                    reason="Honeypot setup",
                )
                created = True
            except discord.HTTPException as exc:
                logger.exception("Could not create the honeypot channel")
                return await Embeds.error(
                    ctx,
                    f"I couldn't create the channel: {type(exc).__name__}.",
                    delete_after=15,
                )

        previous_channel = self._honeypot_channel_id(guild.id)
        await self.settings.set_value(HONEYPOT_CHANNEL_KEY, channel.id, guild_id=guild.id)
        if previous_channel != channel.id:
            # A different channel means the stored card id belongs to the old one.
            await self.settings.set_value(
                HONEYPOT_MESSAGE_KEY, None, guild_id=guild.id
            )
        await self.settings.set_flag(HONEYPOT_ENABLED_KEY, True, guild_id=guild.id)
        if self._stored_int(HONEYPOT_CAUGHT_KEY, guild.id) is None:
            await self.settings.set_value(HONEYPOT_CAUGHT_KEY, 0, guild_id=guild.id)

        # The setup admin must not accidentally ban themselves while testing.
        if not ctx.author.bot:
            mods = self._stored_list(HONEYPOT_MODS_KEY, guild.id)
            if ctx.author.id not in mods:
                mods.append(ctx.author.id)
                await self.settings.set_value(
                    HONEYPOT_MODS_KEY, mods, guild_id=guild.id
                )

        warning = await self._honeypot_sync_card(guild)
        if warning is not None:
            try:
                if not warning.pinned:
                    await warning.pin(reason="Honeypot warning")
            except (discord.Forbidden, discord.HTTPException):
                logger.debug("Could not pin the honeypot warning in %s", guild.id)

        verb = "created" if created else "adopted"
        await Embeds.success(
            ctx,
            f"🍯 Honeypot {verb}: {channel.mention}. Caught counter starts at "
            f"**{self._honeypot_caught(guild.id)}**.",
            delete_after=15,
        )

    @honeypot.command(name="action")
    @commands.has_permissions(administrator=True)
    @discord.app_commands.default_permissions(administrator=True)
    async def honeypot_action(self, ctx: commands.Context, action: str) -> None:
        """Choose what happens to a caught user: ban, kick, timeout, jail or none."""
        choice = (action or "").strip().lower()
        if choice not in HONEYPOT_ACTIONS:
            return await Embeds.error(
                ctx, "Pick one of: " + ", ".join(HONEYPOT_ACTIONS), delete_after=10
            )
        if self.settings is None:
            return await Embeds.error(ctx, "The settings store isn't ready yet.", delete_after=10)
        await self.settings.set_value(HONEYPOT_ACTION_KEY, choice, guild_id=ctx.guild.id)
        # Keep the bait card's stated punishment in step right away.
        await self._honeypot_sync_card(ctx.guild)
        await Embeds.success(
            ctx, f"Honeypot action set to **{choice}**.", delete_after=10
        )

    @honeypot.command(name="enable")
    @commands.has_permissions(administrator=True)
    @discord.app_commands.default_permissions(administrator=True)
    async def honeypot_enable(self, ctx: commands.Context) -> None:
        """Re-arm the honeypot after disabling it."""
        if self.settings is None:
            return await Embeds.error(ctx, "The settings store isn't ready yet.", delete_after=10)
        await self.settings.set_flag(HONEYPOT_ENABLED_KEY, True, guild_id=ctx.guild.id)
        await Embeds.success(ctx, "Honeypot is now **armed**.", delete_after=10)

    @honeypot.command(name="disable")
    @commands.has_permissions(administrator=True)
    @discord.app_commands.default_permissions(administrator=True)
    async def honeypot_disable(self, ctx: commands.Context) -> None:
        """Disarm the honeypot without deleting its channel."""
        if self.settings is None:
            return await Embeds.error(ctx, "The settings store isn't ready yet.", delete_after=10)
        await self.settings.set_flag(HONEYPOT_ENABLED_KEY, False, guild_id=ctx.guild.id)
        await Embeds.warning(ctx, "Honeypot is now **disarmed**.", delete_after=10)

    @honeypot.command(name="reset")
    @commands.has_permissions(administrator=True)
    @discord.app_commands.default_permissions(administrator=True)
    async def honeypot_reset(self, ctx: commands.Context) -> None:
        """Reset the users-caught counter."""
        if self.settings is None:
            return await Embeds.error(ctx, "The settings store isn't ready yet.", delete_after=10)
        await self.settings.set_value(HONEYPOT_CAUGHT_KEY, 0, guild_id=ctx.guild.id)
        self._honeypot_seen = {
            key: value for key, value in self._honeypot_seen.items() if key[0] != ctx.guild.id
        }
        await self._honeypot_sync_card(ctx.guild)
        await Embeds.success(ctx, "Honeypot counter reset to **0**.", delete_after=10)

    @honeypot.command(name="status")
    @commands.has_permissions(administrator=True)
    @discord.app_commands.default_permissions(administrator=True)
    async def honeypot_status(self, ctx: commands.Context) -> None:
        """Show the honeypot configuration and caught counter."""
        guild = ctx.guild
        channel_id = self._honeypot_channel_id(guild.id)
        mods = self._stored_list(HONEYPOT_MODS_KEY, guild.id)
        mod_text = "\n".join(f"• <@{mid}> (`{mid}`)" for mid in mods) if mods else "*none*"
        armed = bool(
            self.settings
            and self.settings.flag(HONEYPOT_ENABLED_KEY, True, guild_id=guild.id)
        )
        card = self._card(
            "🍯 Honeypot Status",
            GREEN if armed else RED,
            fields=[
                ("Channel", f"<#{channel_id}>" if channel_id else "*not set up*", True),
                ("Armed", "✅ yes" if armed else "❌ no", True),
                ("Action", self._honeypot_action(guild.id), True),
                ("USERS CAUGHT", str(self._honeypot_caught(guild.id)), True),
                ("Moderators", mod_text, False),
            ],
        )
        await ctx.send(embed=card)

    @honeypot.group(name="mod", invoke_without_command=True)
    @commands.has_permissions(administrator=True)
    @discord.app_commands.default_permissions(administrator=True)
    async def honeypot_mod(self, ctx: commands.Context) -> None:
        """Manage who gets notified when the honeypot catches someone."""
        await ctx.send(
            f"Use `{(await _prefix(self.bot, ctx))}honeypot mod add|remove|list`."
        )

    @honeypot_mod.command(name="add")
    @commands.has_permissions(administrator=True)
    @discord.app_commands.default_permissions(administrator=True)
    async def honeypot_mod_add(self, ctx: commands.Context, user: discord.User) -> None:
        """Add a honeypot moderator."""
        await self._mod_change(ctx, user.id, add=True)

    @honeypot_mod.command(name="remove")
    @commands.has_permissions(administrator=True)
    @discord.app_commands.default_permissions(administrator=True)
    async def honeypot_mod_remove(self, ctx: commands.Context, user: discord.User) -> None:
        """Remove a honeypot moderator."""
        await self._mod_change(ctx, user.id, add=False)

    @honeypot_mod.command(name="list")
    @commands.has_permissions(administrator=True)
    @discord.app_commands.default_permissions(administrator=True)
    async def honeypot_mod_list(self, ctx: commands.Context) -> None:
        """List the honeypot moderators."""
        mods = self._stored_list(HONEYPOT_MODS_KEY, ctx.guild.id)
        text = "\n".join(f"• <@{mid}> (`{mid}`)" for mid in mods) if mods else "*none*"
        await Embeds.info(
            ctx,
            f"**Honeypot moderators**\n{text}",
            title="🍯 Honeypot mods",
            delete_after=None,
        )

    async def _mod_change(self, ctx: commands.Context, user_id: int, *, add: bool) -> None:
        if self.settings is None:
            return await Embeds.error(ctx, "The settings store isn't ready yet.", delete_after=10)
        mods = self._stored_list(HONEYPOT_MODS_KEY, ctx.guild.id)
        if add:
            if user_id not in mods:
                mods.append(user_id)
            message = f"🍯 <@{user_id}> will now be notified of honeypot catches."
        else:
            mods = [mid for mid in mods if mid != user_id]
            message = f"🍯 <@{user_id}> is no longer a honeypot mod."
        await self.settings.set_value(HONEYPOT_MODS_KEY, mods, guild_id=ctx.guild.id)
        await Embeds.success(ctx, message, delete_after=10)


def _duration_delta():
    """Ten-minute timeout for honeypot catches."""
    from datetime import timedelta

    return timedelta(minutes=10)


async def _prefix(bot: commands.Bot, ctx: commands.Context) -> str:
    if ctx.message is None:
        return "/"
    try:
        raw = await bot.get_prefix(ctx.message)
    except Exception:
        return "!"
    return raw[0] if isinstance(raw, list) else raw


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AntiNuke(bot))
