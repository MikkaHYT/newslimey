"""Per-guild settings mirrored from the ``bot_settings`` table.

The dashboard writes JSON-encoded values into ``bot_settings``; this module keeps
an in-memory copy so the hot paths (prefix resolution, per-command maintenance
checks) never touch the database. Call :meth:`DashboardSettings.preload` once
during ``setup_hook`` and mutate through the helpers below so the cache and the
database stay in sync.
"""

from __future__ import annotations

import json
import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from discord.ext import commands

logger = logging.getLogger("slimey.settings")

PREFIX_KEY = "prefix"
MAINTENANCE_KEY = "maintenance"
DISABLED_KEY = "disabled_commands"
RADIO_CHANNEL_KEY = "radio_channel"
RADIO_VC_KEY = "radio_vc"
STATION_VOICE_KEY = "station_voice"
DJ_ROLE_KEY = "dj_role"

#: Rows stored against this guild id are bot-wide rather than per-server.
GLOBAL_SCOPE = 0

#: Bot-wide: whether level-up cards are posted for anyone at all.
LEVEL_POPUPS_KEY = "level_popups"

#: Per-server: offer the "download the whole project" zip button.
MIXTAPE_ZIP_KEY = "mixtape_zip"

#: Per-server: delete mp3/wav/m4a attachments and warn the sender.
ANTI_MP3_KEY = "anti_mp3"

#: Per-server: role handed to every member the moment they join.
DEFAULT_ROLE_KEY = "default_role"

#: Bot-wide: extra Discord user IDs granted the owner/admin commands, on top of
#: the application's own team members. Managed with `!admin allow` / `!admin deny`.
BOT_ADMINS_KEY = "bot_admins"

#: Bot-wide: guild ids whose commands are kept out of the developer-channel
#: command log, as a JSON list of ids. Missing or empty means every server is
#: logged, which is the default. Managed with `!admin logs exclude|include`.
COMMAND_LOG_EXCLUDED_KEY = "command_log_excluded_guilds"

#: Bot-wide: whether small servers are gated. While on, every server the bot
#: joins that is below :data:`MEMBER_GATING_THRESHOLD_KEY` is warned and then
#: left, unless the owner approved it by hand. Managed with `!membergating`.
MEMBER_GATING_KEY = "member_gating"

#: Bot-wide: the member count below which a server counts as small. Missing or
#: unreadable means the built-in default (100, see ``cogs.membergating``).
MEMBER_GATING_THRESHOLD_KEY = "member_gating_threshold"

#: Bot-wide: guild ids the owner let through by hand, as a JSON list of ids.
#: These are never warned or removed, whatever their size.
MEMBER_GATING_APPROVED_KEY = "member_gating_approved_guilds"

#: Bot-wide: custom title/description/colour for the warning card a gated server
#: gets before the bot leaves, as a JSON object. Missing fields use the built-in
#: default. Managed with `!membergating`.
MEMBER_GATING_WARN_EMBED_KEY = "member_gating_warn_embed"

#: Bot-wide: the same, for the farewell card posted immediately before the bot
#: leaves a gated server.
MEMBER_GATING_LEAVE_EMBED_KEY = "member_gating_leave_embed"

#: Per-server: whether this server keeps its own economy instead of sharing the
#: bot-wide one. Missing means OFF (managed with `!guildeconomy on|off`).
GUILD_ECONOMY_KEY = "guild_economy"

#: Per-server: the channel where every message is sent to the AI with no prefix.
#: Set with `!aichannel <channel>`.
AI_CHANNEL_KEY = "ai_channel"

#: Per-server: extra lines appended to the AI's system prompt. A JSON list of
#: strings, managed with `!aiprompt add/list/remove`.
AI_PROMPT_KEY = "ai_prompt_lines"

#: Per-server: whether the AI may drop a spontaneous message into a channel that
#: has gone quiet. Missing means OFF (managed with `!aipopup on|off`).
AI_POPUP_ENABLED_KEY = "ai_popup_enabled"

#: Per-server: the channels the AI watches for quiet spells. A JSON list of
#: channel ids, managed with `!aipopup channels add/remove/list/clear`.
AI_POPUP_CHANNELS_KEY = "ai_popup_channels"

#: Per-server: seconds a watched channel may stay quiet before the AI posts
#: (managed with `!aipopup time`). Missing means the built-in default.
AI_POPUP_TIME_KEY = "ai_popup_time"

#: Per-server: the role pinged when the AI revives a channel (managed with
#: `!aipopup roleping`).
AI_POPUP_ROLE_KEY = "ai_popup_role"

#: Per-server: whether that saved role is pinged at all, so the ping can be
#: switched off without forgetting the role (managed with
#: `!aipopup roleping on|off`). Missing means ON.
AI_POPUP_ROLE_PING_KEY = "ai_popup_role_ping"

#: Per-server: seconds between AI money drops for the same requester (managed
#: with `!aisettings cooldown`). Missing means the built-in default.
AI_MONEY_COOLDOWN_KEY = "ai_money_cooldown"

#: Per-server: whether the AI may move money at all. Missing means enabled
#: (managed with `!aisettings economy on|off`).
AI_MONEY_ENABLED_KEY = "ai_money_enabled"

#: Per-server: most money the AI may give in one action. Missing or 0 means no
#: cap (managed with `!aisettings give`).
AI_MONEY_LIMIT_KEY = "ai_money_limit"

#: Per-server: most money the AI may take/drain in one action. Missing or 0
#: means no cap (managed with `!aisettings drain`).
AI_DRAIN_LIMIT_KEY = "ai_drain_limit"

#: Per-server: the guild permission a member needs before the AI will move money
#: for them, e.g. "manage_guild". Missing means the built-in default
#: (managed with `!aisettings perm`).
AI_ECONOMY_PERMISSION_KEY = "ai_economy_permission"

#: Per-server: longest timeout/jail the AI may apply, in minutes (managed with
#: `!aisettings timeout`). Missing means the built-in default.
AI_MODERATION_LIMIT_KEY = "ai_moderation_limit"

#: Per-server: whether the AI may timeout/jail a member on its own, with no
#: member who can moderate asking for it. Missing means OFF, so the AI never
#: punishes anyone unsupervised unless a server opts in (managed with
#: `!aisettings autonomous`).
AI_AUTONOMOUS_MODERATION_KEY = "ai_autonomous_moderation"

#: Bot-wide: the Fish Audio reference id the AI cog speaks with when it answers
#: in TTS mode. Missing means the built-in default (see ``cogs.tts.AI_VOICE_ID``),
#: managed with ``!aitts``.
AI_VOICE_KEY = "ai_voice"

#: Bot-wide: which Gemini API key the AI cog uses. Missing means the primary
#: (environment) key; the string ``"alt"`` switches to the built-in backup key,
#: managed with ``!aiapikey``.
AI_API_KEY_KEY = "ai_api_key"

#: A member must hold at least this Discord permission before the AI will move
#: money for them. Any ``discord.Permissions`` flag name works.
DEFAULT_AI_ECONOMY_PERMISSION = "manage_guild"

MAX_PREFIX_LEN = 8

TRUTHY = frozenset({"1", "true", "yes", "on", "enable", "enabled", "y"})
FALSY = frozenset({"0", "false", "no", "off", "disable", "disabled", "n"})


def _as_int(value: Any) -> int | None:
    """Best-effort integer, since channel ids can arrive as strings."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _decode(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


class DashboardSettings:
    """In-memory mirror of the dashboard-managed ``bot_settings`` rows."""

    def __init__(self, bot: "commands.Bot") -> None:
        self.bot = bot
        self.prefixes: dict[int, str] = {}
        # Every ``bot_settings`` row, decoded: ``(guild_id, key) -> value``.
        # The named maps below are the hot paths that need a faster shape;
        # this is the general store behind :meth:`flag`.
        self.values: dict[tuple[int, str], Any] = {}
        self.maintenance: str | None = None
        self.command_maintenance: dict[str, str] = {}
        # guild id -> qualified command names disabled on that guild
        self.disabled: dict[int, set[str]] = {}
        # guild id -> channel members type song names into (the radio's inbox)
        self.radio_channel: dict[int, int] = {}
        # guild id -> voice channel the station parks in 24/7
        self.radio_voice: dict[int, int] = {}
        # guild id -> voice channel the station cog parks in 24/7
        self.station_voice: dict[int, int] = {}
        # guild id -> the role allowed to drive the station (`!dj`)
        self.dj_role: dict[int, int] = {}

    # -- loading ---------------------------------------------------------
    async def preload(self) -> None:
        """Populate the cache from the database. Safe to call repeatedly."""
        try:
            rows = await self.bot.db.execute(
                "SELECT guild_id, key, value FROM bot_settings;"
            )
        except Exception:
            logger.exception("Failed to preload guild settings")
            return

        self.prefixes.clear()
        self.disabled.clear()
        self.radio_channel.clear()
        self.radio_voice.clear()
        self.station_voice.clear()
        self.dj_role.clear()
        self.values.clear()
        self.maintenance = None
        self.command_maintenance.clear()

        for guild_id, key, raw_value in rows:
            value = _decode(raw_value)
            try:
                self.values[(int(guild_id), str(key))] = value
            except (TypeError, ValueError):
                logger.warning("Ignoring setting %r with a bad guild id", key)

            if key == PREFIX_KEY and isinstance(value, str) and value:
                self.prefixes[int(guild_id)] = value
            elif key == MAINTENANCE_KEY:
                if value in (None, "", False):
                    continue
                if isinstance(value, dict):
                    self.command_maintenance.update(
                        {str(name): str(reason) for name, reason in value.items()}
                    )
                elif isinstance(value, str):
                    self.maintenance = value
            elif key == DISABLED_KEY and isinstance(value, (list, tuple)):
                self.disabled[int(guild_id)] = {str(name) for name in value}
            elif key == RADIO_CHANNEL_KEY and _as_int(value) is not None:
                self.radio_channel[int(guild_id)] = _as_int(value)  # type: ignore[assignment]
            elif key == RADIO_VC_KEY and _as_int(value) is not None:
                self.radio_voice[int(guild_id)] = _as_int(value)  # type: ignore[assignment]
            elif key == STATION_VOICE_KEY and _as_int(value) is not None:
                self.station_voice[int(guild_id)] = _as_int(value)  # type: ignore[assignment]
            elif key == DJ_ROLE_KEY and _as_int(value) is not None:
                self.dj_role[int(guild_id)] = _as_int(value)  # type: ignore[assignment]

        logger.info(
            "Guild settings loaded (%d prefix override(s), %d guild(s) with disabled commands)",
            len(self.prefixes),
            len(self.disabled),
        )

    # -- prefix ----------------------------------------------------------
    def guild_prefix(self, bot: "commands.Bot", guild_id: int) -> str | None:
        return self.prefixes.get(int(guild_id))

    async def set_prefix(self, guild_id: int, prefix: str | None) -> None:
        guild_id = int(guild_id)
        if prefix:
            prefix = prefix[:MAX_PREFIX_LEN]
            self.prefixes[guild_id] = prefix
        else:
            self.prefixes.pop(guild_id, None)

        await self.bot.db.execute(
            "INSERT INTO bot_settings (guild_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value;",
            guild_id,
            PREFIX_KEY,
            json.dumps(prefix) if prefix else None,
        )

    # -- generic toggles ---------------------------------------------------
    def flag(
        self, key: str, default: bool = False, *, guild_id: int = GLOBAL_SCOPE
    ) -> bool:
        """Whether the switch ``key`` is on for this scope.

        Reads straight out of the in-memory mirror, so it is safe to call on a
        hot path. A missing row (or an unreadable one) returns ``default``.
        """
        value = self.values.get((int(guild_id), str(key)))
        if value is None or value == "":
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if text in TRUTHY:
            return True
        if text in FALSY:
            return False
        return default

    def stored(self, key: str, *, guild_id: int = GLOBAL_SCOPE) -> Any:
        """The raw value of ``key`` at this scope, or ``None`` when unset."""
        return self.values.get((int(guild_id), str(key)))

    async def set_flag(
        self,
        key: str,
        value: bool | None,
        *,
        guild_id: int = GLOBAL_SCOPE,
    ) -> None:
        """Flip a switch. ``None`` clears it, restoring the default."""
        scope = int(guild_id)
        if value is None:
            self.values.pop((scope, str(key)), None)
        else:
            self.values[(scope, str(key))] = bool(value)

        await self.bot.db.execute(
            "INSERT INTO bot_settings (guild_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value;",
            scope,
            str(key),
            None if value is None else json.dumps(bool(value)),
        )

    async def set_value(
        self, key: str, value: Any, *, guild_id: int = GLOBAL_SCOPE
    ) -> None:
        """Store any JSON-serialisable value under ``key`` for this scope.

        ``None`` clears it. The generic counterpart to :meth:`set_flag`, for
        settings that are not booleans — a channel or role id, say.
        """
        scope = int(guild_id)
        if value is None:
            self.values.pop((scope, str(key)), None)
        else:
            self.values[(scope, str(key))] = value

        await self.bot.db.execute(
            "INSERT INTO bot_settings (guild_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value;",
            scope,
            str(key),
            None if value is None else json.dumps(value),
        )

    # -- radio channels ---------------------------------------------------
    def radio_channel_id(self, guild_id: int) -> int | None:
        """The channel members type song names into, if one is configured."""
        return self.radio_channel.get(int(guild_id))

    def radio_voice_id(self, guild_id: int) -> int | None:
        """The voice channel the station stays parked in, if one is configured."""
        return self.radio_voice.get(int(guild_id))

    async def set_radio_channel(self, guild_id: int, channel_id: int | None) -> None:
        self._remember(self.radio_channel, guild_id, channel_id)
        await self._store_channel(RADIO_CHANNEL_KEY, int(guild_id), channel_id)

    async def set_radio_voice(self, guild_id: int, channel_id: int | None) -> None:
        self._remember(self.radio_voice, guild_id, channel_id)
        await self._store_channel(RADIO_VC_KEY, int(guild_id), channel_id)

    # -- station 24/7 channel ---------------------------------------------
    def station_voice_id(self, guild_id: int) -> int | None:
        """The voice channel the station stays parked in, if any."""
        return self.station_voice.get(int(guild_id))

    async def set_station_voice(self, guild_id: int, channel_id: int | None) -> None:
        self._remember(self.station_voice, guild_id, channel_id)
        await self._store_channel(STATION_VOICE_KEY, int(guild_id), channel_id)

    # -- the station's DJ role --------------------------------------------
    def dj_role_id(self, guild_id: int) -> int | None:
        """The role allowed to drive the station, if the server set one."""
        return self.dj_role.get(int(guild_id))

    async def set_dj_role(self, guild_id: int, role_id: int | None) -> None:
        self._remember(self.dj_role, guild_id, role_id)
        await self._store_channel(DJ_ROLE_KEY, int(guild_id), role_id)

    @staticmethod
    def _remember(
        table: dict[int, int], guild_id: int, channel_id: int | None
    ) -> None:
        guild_id = int(guild_id)
        if channel_id:
            table[guild_id] = int(channel_id)
        else:
            table.pop(guild_id, None)

    async def _store_channel(
        self, key: str, guild_id: int, channel_id: int | None
    ) -> None:
        await self.bot.db.execute(
            "INSERT INTO bot_settings (guild_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value;",
            guild_id,
            key,
            json.dumps(int(channel_id)) if channel_id else None,
        )

    # -- per-command maintenance -----------------------------------------
    def reason_for(self, command_name: str) -> str | None:
        if self.maintenance:
            return f"The bot is in maintenance: {self.maintenance}"
        reason = self.command_maintenance.get(command_name)
        if reason:
            return f"`{command_name}` is disabled: {reason}"
        return None


def maintenance_reason(bot: "commands.Bot", command_name: str) -> str | None:
    """Return why ``command_name`` cannot run right now, if it cannot."""
    settings = getattr(bot, "dash_settings", None)
    if settings is None:
        return None
    return settings.reason_for(command_name)
