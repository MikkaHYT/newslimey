"""What the dashboard may change, described once for the API and the UI.

Every setting the cogs read out of ``bot_settings`` is declared here with its
type, so the API can validate writes and the front end can render a labelled
control instead of a raw JSON box. Keys the schema does not know about are still
editable through the advanced editor; they are just stored verbatim.
"""

from __future__ import annotations

from typing import Any

from utils.core import settings as S

#: Bot-wide rows live against guild id 0.
GLOBAL = S.GLOBAL_SCOPE


def _field(key: str, label: str, kind: str = "text", **extra: Any) -> dict[str, Any]:
    field = {"key": key, "label": label, "type": kind}
    field.update(extra)
    return field


def _select(key: str, label: str, options: list[tuple[str, str]], **extra: Any) -> dict[str, Any]:
    field = _field(
        key,
        label,
        "select",
        options=[{"value": value, "label": text} for value, text in options],
        **extra,
    )
    return field


#: Permissions the AI may require before it touches a member's money.
ECONOMY_PERMISSIONS = [
    ("manage_guild", "Manage Server"),
    ("manage_messages", "Manage Messages"),
    ("moderate_members", "Timeout Members"),
    ("kick_members", "Kick Members"),
    ("ban_members", "Ban Members"),
    ("manage_roles", "Manage Roles"),
    ("manage_channels", "Manage Channels"),
    ("administrator", "Administrator"),
]

#: AntiNuke protection modules, in the same order the cog lists them.
ANTINUKE_MODULES = [
    ("channel", "Channels"),
    ("role", "Roles"),
    ("permissions", "Permissions"),
    ("ban", "Bans"),
    ("kick", "Kicks"),
    ("webhook", "Webhooks"),
    ("vanity", "Vanity"),
    ("botadd", "Bot additions"),
    ("emoji", "Emojis"),
]

#: Action key -> label, mirroring ``cogs.antinuke.ACTION_LABELS``.
ANTINUKE_ACTIONS = [
    ("channel_create", "Channels created"),
    ("channel_delete", "Channels deleted"),
    ("role_create", "Roles created"),
    ("role_delete", "Roles deleted"),
    ("role_update", "Dangerous role grants"),
    ("ban", "Members banned"),
    ("kick", "Members kicked"),
    ("webhook_create", "Webhooks created"),
    ("guild_update", "Server edits"),
    ("member_role_update", "Dangerous roles given"),
    ("vanity", "Vanity URL changes"),
    ("bot_add", "Bots added"),
    ("emoji_delete", "Emojis deleted"),
]

#: Topic key -> label, mirroring ``cogs.serverlogs.TOPICS``.
LOG_TOPICS = [
    ("messages", "Deleted messages"),
    ("edits", "Edited messages"),
    ("deletions", "Bulk message purges"),
    ("roles", "Role changes"),
    ("moderation", "Timeouts, kicks, bans"),
    ("jails", "Jails and releases"),
    ("antimp3", "Audio files removed"),
]


def _guild_groups() -> list[dict[str, Any]]:
    return [
        {
            "id": "general",
            "label": "General",
            "icon": "sliders",
            "description": "The basics: command prefix, autorole and the shared economy.",
            "scope": "guild",
            "fields": [
                _field("prefix", "Command prefix", "text", placeholder="!", max=8,
                       help="What members type before a command. Leave empty to use the default."),
                _field(S.DEFAULT_ROLE_KEY, "Autorole", "role",
                       help="Handed to every member the moment they join."),
                _field(S.DJ_ROLE_KEY, "DJ role", "role",
                       help="Members with this role may drive the radio and station."),
                _field(S.ANTI_MP3_KEY, "Remove audio attachments", "toggle",
                       help="Delete mp3/wav/m4a uploads and warn the sender."),
                _field(S.GUILD_ECONOMY_KEY, "Separate economy", "toggle",
                       help="Keep this server's wallet apart from the bot-wide one."),
                _field(S.MIXTAPE_ZIP_KEY, "Project zip button", "toggle",
                       help="Offer the download-the-whole-project button on mixtape cards."),
            ],
        },
        {
            "id": "ai",
            "label": "AI",
            "icon": "sparkles",
            "description": "How the AI behaves in this server, from its extra instructions to its money limits.",
            "scope": "guild",
            "fields": [
                _field(S.AI_CHANNEL_KEY, "Always-on AI channel", "channel",
                       help="Every message here is answered with no prefix."),
                _field(S.AI_PROMPT_KEY, "Extra prompt lines", "textlist",
                       help="Appended to the AI's system prompt, one rule per line."),
                _field(S.AI_POPUP_ENABLED_KEY, "Revive quiet channels", "toggle",
                       help="Let the AI drop a message after a channel goes quiet."),
                _field(S.AI_POPUP_CHANNELS_KEY, "Channels to watch", "channels"),
                _field(S.AI_POPUP_TIME_KEY, "Quiet seconds before posting", "number", min=5, max=86400,
                       help="How long a watched channel must stay quiet first."),
                _field(S.AI_POPUP_ROLE_KEY, "Role pinged on revival", "role"),
                _field(S.AI_POPUP_ROLE_PING_KEY, "Ping that role", "toggle"),
                _field(S.AI_MONEY_ENABLED_KEY, "AI may move money", "toggle"),
                _field(S.AI_MONEY_LIMIT_KEY, "Most money per gift", "number", min=0, max=1_000_000),
                _field(S.AI_DRAIN_LIMIT_KEY, "Most money per drain", "number", min=0, max=1_000_000),
                _field(S.AI_MONEY_COOLDOWN_KEY, "Gift cooldown (seconds)", "number", min=0, max=86400),
                _select(S.AI_ECONOMY_PERMISSION_KEY, "Permission needed to move money", ECONOMY_PERMISSIONS),
                _field(S.AI_MODERATION_LIMIT_KEY, "Longest timeout the AI may apply (minutes)",
                       "number", min=0, max=40320),
                _field(S.AI_AUTONOMOUS_MODERATION_KEY, "AI may punish unsupervised", "toggle",
                       help="Off means the AI only moderates when a moderator asks it to."),
            ],
        },
        {
            "id": "voice",
            "label": "Voice AI",
            "icon": "mic",
            "description": "The live voice-call assistant that listens per speaker.",
            "scope": "guild",
            "fields": [
                _field("vcai_wake_words", "Wake words", "textlist",
                       help="Extra names or phrases that make it answer in a voice call."),
            ],
        },
        {
            "id": "welcome",
            "label": "Welcome",
            "icon": "hand",
            "description": "The card posted when someone joins this server.",
            "scope": "guild",
            "fields": [
                _field("welcome_message", "Welcome message", "welcome",
                       help="Placeholders: {user} {name} {username} {server} {membercount} {id}"),
            ],
        },
        {
            "id": "logs",
            "label": "Server logs",
            "icon": "scroll",
            "description": "Where each kind of event is reported. Empty means that topic is off.",
            "scope": "guild",
            "fields": [_field("log_category", "Logs category", "channel", channel_type="category")]
            + [
                _field(f"log_channel_{topic}", label, "channel")
                for topic, label in LOG_TOPICS
            ],
        },
        {
            "id": "music",
            "label": "Music & radio",
            "icon": "music",
            "description": "Channels the station listens to and parks in.",
            "scope": "guild",
            "fields": [
                _field(S.RADIO_CHANNEL_KEY, "Radio request channel", "channel"),
                _field(S.RADIO_VC_KEY, "Radio voice channel", "channel", channel_type="voice"),
                _field(S.STATION_VOICE_KEY, "24/7 station voice channel", "channel",
                       channel_type="voice"),
            ],
        },
        {
            "id": "sotd",
            "label": "Song of the day",
            "icon": "star",
            "description": "Where the daily track is posted and who gets pinged for it.",
            "scope": "guild",
            "fields": [
                _field("sotd_enabled", "Enabled", "toggle"),
                _field("sotd_channel", "Post channel", "channel"),
                _field("sotd_role", "Ping role", "role"),
                _select("sotd_style", "Delivery style",
                        [("button", "Link card"), ("file", "Attach the mp3")]),
            ],
        },
        {
            "id": "antinuke",
            "label": "Anti-nuke",
            "icon": "shield",
            "description": "Limits on destructive actions. Threshold is how many are allowed inside the window.",
            "scope": "guild",
            "fields": [
                _field("antinuke_enabled", "Anti-nuke enabled", "toggle"),
                _field("antinuke_log_channel", "Log channel", "channel"),
                _select("antinuke_punishment", "Punishment",
                        [("ban", "Ban the nuker"), ("kick", "Kick the nuker"),
                         ("strip", "Strip their roles")]),
                _field("antinuke_window", "Window (seconds)", "number", min=1, max=3600),
            ]
            + [
                _field(f"antinuke_module_{module}", label, "toggle")
                for module, label in ANTINUKE_MODULES
            ]
            + [
                _field(f"antinuke_threshold_{action}", label, "number", min=0, max=1000)
                for action, label in ANTINUKE_ACTIONS
            ],
        },
        {
            "id": "honeypot",
            "label": "Honeypot",
            "icon": "target",
            "description": "The trap channel for compromised accounts.",
            "scope": "guild",
            "fields": [
                _field("honeypot_enabled", "Enabled", "toggle"),
                _field("honeypot_channel", "Trap channel", "channel"),
                _select("honeypot_action", "Action on a caught account",
                        [("ban", "Ban"), ("kick", "Kick"), ("timeout", "10 minute timeout"),
                         ("jail", "Jail"), ("none", "Just log it")]),
            ],
        },
    ]


def _global_groups() -> list[dict[str, Any]]:
    return [
        {
            "id": "bot",
            "label": "Bot-wide",
            "icon": "globe",
            "description": "Settings that affect every server the bot is in.",
            "scope": "global",
            "fields": [
                _field(S.MAINTENANCE_KEY, "Maintenance notice", "text",
                       help="When set, every command is refused with this reason."),
                _field(S.LEVEL_POPUPS_KEY, "Level-up cards", "toggle",
                       help="Whether level-ups are announced for anyone at all."),
                _field(S.AI_VOICE_KEY, "AI voice id", "text",
                       help="The Fish Audio reference the AI speaks with."),
                _select(S.AI_API_KEY_KEY, "Gemini key",
                        [("primary", "Primary (environment)"), ("alt", "Backup key")]),
                _field(S.BOT_ADMINS_KEY, "Extra bot admins", "idlist",
                       help="Discord user ids granted the owner/admin commands."),
                _field(S.COMMAND_LOG_EXCLUDED_KEY, "Servers hidden from the command log", "idlist",
                       help="Guild ids whose commands never reach the developer channel."),
            ],
        }
    ]


GROUPS = {"guild": _guild_groups(), "global": _global_groups()}

#: Every declared field, by key, for validation lookups.
FIELDS = {
    field["key"]: field
    for group in GROUPS["guild"] + GROUPS["global"]
    for field in group["fields"]
}

MAX_TEXT = 2000
MAX_LIST = 200


def schema_payload(scope: str = "guild") -> list[dict[str, Any]]:
    return GROUPS.get(scope, [])


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
class InvalidValue(ValueError):
    """A submitted setting could not be converted to its declared type."""


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise InvalidValue("Expected a number, not a switch")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise InvalidValue(f"{value!r} is not a whole number")


def _clean_text(value: Any, *, limit: int = MAX_TEXT) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]


def _int_list(value: Any) -> list[int] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise InvalidValue("Expected a list")
    cleaned: list[int] = []
    for item in list(value)[:MAX_LIST]:
        number = _as_int(item)
        if number is not None and number not in cleaned:
            cleaned.append(number)
    return cleaned or None


def _text_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, (list, tuple)):
        raise InvalidValue("Expected a list of lines")
    cleaned: list[str] = []
    for item in list(value)[:MAX_LIST]:
        text = _clean_text(item, limit=500)
        if text:
            cleaned.append(text)
    return cleaned or None


def _welcome(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise InvalidValue("Welcome settings must be an object")
    color = value.get("color")
    try:
        color = int(color or 0)
    except (TypeError, ValueError):
        color = 0
    return {
        "channel_id": _as_int(value.get("channel_id")),
        "title": _clean_text(value.get("title"), limit=256) or "",
        "description": _clean_text(value.get("description"), limit=4000) or "",
        "footer": _clean_text(value.get("footer"), limit=100) or "",
        "color": max(0, min(color, 0xFFFFFF)),
        "ping": bool(value.get("ping", True)),
    }


def coerce(key: str, value: Any) -> tuple[bool, Any, str | None]:
    """Turn a submitted value into what should be stored.

    Returns ``(ok, value, error)``. ``None`` always means "clear this setting",
    which every cog treats as its default.
    """
    field = FIELDS.get(key)
    if field is None:
        # Advanced editor: accept any JSON-serialisable value as-is.
        if isinstance(value, (dict, list, str, int, float, bool)) or value is None:
            return True, value, None
        return False, None, "Unsupported value type"

    kind = field["type"]
    try:
        if kind == "toggle":
            if value is None:
                return True, None, None
            return True, bool(value), None
        if kind == "number":
            return True, _as_int(value), None
        if kind in ("channel", "role"):
            return True, _as_int(value), None
        if kind in ("channels", "roles", "idlist"):
            return True, _int_list(value), None
        if kind == "textlist":
            return True, _text_list(value), None
        if kind == "welcome":
            return True, _welcome(value), None
        if kind == "select":
            if value is None or value == "":
                return True, None, None
            allowed = {option["value"] for option in field.get("options", [])}
            if str(value) not in allowed:
                return False, None, f"{value!r} is not one of {', '.join(sorted(allowed))}"
            return True, str(value), None
        if kind == "json":
            return True, value, None
        # Plain text last.
        if key == "prefix" and value is not None:
            text = _clean_text(value, limit=8) or None
            return True, text, None
        return True, _clean_text(value), None
    except InvalidValue as error:
        return False, None, str(error)
