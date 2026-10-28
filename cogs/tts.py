# cogs/tts.py

from __future__ import annotations

import io
import json
import logging
import re
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands

from config import EMBED_COLOR
from utils.core.prefixes import prefix_for
from utils.core.settings import AI_VOICE_KEY

logger = logging.getLogger(__name__)

FISH_TTS_URL = "https://api.fish.audio/v1/tts"
FISH_MODEL = "s2.1-pro-free"  # free tier model for testing/prototyping
MAX_CHARS = 1000  # keep requests reasonable / avoid abuse

#: Default voice the AI cog speaks with when replying in TTS mode
#: (_,aimode tts_). Overridable at runtime with _,_aitts_ (stored bot-wide).
AI_VOICE_ID = "6ea3c15f427e402399022da1c96f0b70"

#: Custom TTS commands are persisted here so a restart doesn't lose them.
DATA_DIR = Path("data")
CUSTOM_TTS_FILE = DATA_DIR / "custom_tts.json"

#: Same shape Discord's own command names follow.
COMMAND_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
#: Names the manager itself owns, plus the built-in TTS commands.
RESERVED_NAMES = {"customtts", "tts", "thugtts", "gunnatts", "help"}


def load_custom_commands() -> dict[str, str]:
    """Read the persisted ``name -> reference_id`` map, dropping bad entries."""
    try:
        raw = json.loads(CUSTOM_TTS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        logger.exception("Could not read %s", CUSTOM_TTS_FILE)
        return {}

    if not isinstance(raw, dict):
        return {}

    commands: dict[str, str] = {}
    for name, reference_id in raw.items():
        name = str(name).strip().lower()
        reference_id = str(reference_id).strip()
        if COMMAND_NAME_RE.match(name) and reference_id:
            commands[name] = reference_id
    return commands


def save_custom_commands(commands: dict[str, str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CUSTOM_TTS_FILE.write_text(json.dumps(commands, indent=4), encoding="utf-8")


def resolve_ai_voice(bot: commands.Bot) -> str:
    """The Fish Audio reference id the AI cog speaks with in TTS mode.

    Reads the bot-wide ``,aitts`` override when one is set, otherwise falls
    back to the built-in :data:`AI_VOICE_ID`.
    """
    settings = getattr(bot, "dash_settings", None)
    if settings is not None:
        value = settings.stored(AI_VOICE_KEY)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return AI_VOICE_ID


def simple_view(content: str, *, timeout: int = 60) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=timeout)
    container = discord.ui.Container(accent_color=EMBED_COLOR)
    container.add_item(discord.ui.TextDisplay(content))
    view.add_item(container)
    return view


class TTS(commands.Cog):
    """Fish Audio text-to-speech commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None
        #: ``command name -> Fish Audio reference id`` for the dynamic commands.
        self.custom_commands: dict[str, str] = {}

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession()
        self.custom_commands = load_custom_commands()
        for name, reference_id in self.custom_commands.items():
            self._add_custom_command(name, reference_id)
        if self.custom_commands:
            logger.info(
                "Loaded %d custom tts command(s): %s",
                len(self.custom_commands),
                ", ".join(sorted(self.custom_commands)),
            )

    async def cog_unload(self) -> None:
        if self.session:
            await self.session.close()
        for name in list(self.custom_commands):
            self._remove_custom_command(name)

    # ----------------------------------------------------------------
    # Dynamic command registry
    # ----------------------------------------------------------------

    def _add_custom_command(self, name: str, reference_id: str) -> None:
        """Register ``,<name> <text>`` as a Fish Audio voice command."""

        # A command with ``cog`` set is invoked as ``callback(cog, ctx)``, so the
        # callback has to look like a cog method: a leading ``self`` plus a
        # dotted ``__qualname__``. discord.py then strips ``self`` and ``ctx``
        # from the signature and leaves only ``text``.
        async def callback(self, ctx: commands.Context, *, text: str | None = None) -> None:
            await self.speak(
                ctx,
                text,
                command_name=name,
                reference_id=reference_id,
            )

        callback.__name__ = name
        callback.__qualname__ = f"TTS.{name}"

        command = commands.Command(
            callback,
            name=name,
            help=f"Generates a spoken mp3 of the given text using the `{name}` voice.",
            description=f"Converts text to speech using the custom `{name}` voice.",
            extras={"example": f"{name} Hello! Welcome to Fish Audio."},
        )
        command.cog = self

        # Re-registering (an update) has to replace the old name first.
        if self.bot.get_command(name) is not None:
            self.bot.remove_command(name)
        self.bot.add_command(command)

    def _remove_custom_command(self, name: str) -> None:
        existing = self.bot.get_command(name)
        if existing is not None and existing.cog is self:
            self.bot.remove_command(name)

    def _reserved(self, name: str) -> str | None:
        """Return a reason if *name* can't be used, otherwise ``None``."""
        if not COMMAND_NAME_RE.match(name):
            return (
                "Command names must be 1-32 characters of letters, numbers, "
                "`-` or `_`."
            )
        if name in RESERVED_NAMES:
            return f"`{name}` is reserved."
        existing = self.bot.get_command(name)
        if existing is not None and existing.cog is not self:
            return f"`{name}` is already a command."
        return None

    async def send_error(self, ctx: commands.Context, message: str) -> None:
        await ctx.send(view=simple_view(message))

    async def render(self, text: str, *, reference_id: str) -> bytes | None:
        """Synthesise ``text`` to mp3 bytes, or ``None`` when Fish Audio fails.

        Kept separate from :meth:`speak` so other cogs (the AI cog's TTS reply
        mode) can reuse the same request without a command context.
        """
        if not text or self.session is None:
            return None
        try:
            async with self.session.post(
                FISH_TTS_URL,
                headers={
                    "Authorization": f"Bearer sk-fish-JlQ51EW0V2ffREBG6fRzVD0us0bUHj6RqLAbq9ew-jQ",
                    "Content-Type": "application/json",
                    "model": FISH_MODEL,
                },
                json={
                    "text": text,
                    "reference_id": reference_id,
                    "format": "mp3",
                },
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(
                        "Fish Audio TTS request failed (%s): %s",
                        resp.status,
                        body[:500],
                    )
                    return None
                return await resp.read() or None
        except aiohttp.ClientError:
            logger.exception("Fish Audio TTS request failed")
            return None

    async def speak(
        self,
        ctx: commands.Context,
        text: str | None,
        *,
        command_name: str,
        reference_id: str,
    ) -> None:
        if not text:
            prefix = await prefix_for(ctx)
            help_view = simple_view(
                f"# Command: {command_name}\n\n"
                "**Syntax**\n"
                f"`{prefix}{command_name} <text>`\n\n"
                "**Example**\n"
                f"`{prefix}{command_name} Hello! Welcome to Fish Audio.`"
            )
            await ctx.send(view=help_view)
            return

        if len(text) > MAX_CHARS:
            await self.send_error(
                ctx, f"❌ That's too long. Keep it under {MAX_CHARS} characters."
            )
            return

        async with ctx.typing():
            data = await self.render(text, reference_id=reference_id)

        if not data:
            await self.send_error(ctx, "❌ Failed to generate speech.")
            return

        try:
            await ctx.send(file=discord.File(io.BytesIO(data), filename=f"{command_name}.mp3"))
        except discord.HTTPException:
            logger.exception("Discord rejected the tts audio file")
            await self.send_error(ctx, "❌ Discord rejected the audio file.")

    @commands.command(
        name="tts",
        description="Converts text to speech using Fish Audio and sends it as an mp3.",
        help="Generates a spoken mp3 of the given text.",
        extras={"example": "tts Hello! Welcome to Fish Audio."},
    )
    async def tts(self, ctx: commands.Context, *, text: str | None = None) -> None:
        await self.speak(
            ctx,
            text,
            command_name="tts",
            reference_id="6ea3c15f427e402399022da1c96f0b70",
        )

    @commands.command(
        name="thugtts",
        description="Converts text to speech in a thug voice and sends it as an mp3.",
        help="Generates a spoken mp3 of the given text in a thug voice.",
        extras={"example": "thugtts Hello! Welcome to Fish Audio."},
    )
    async def thugtts(self, ctx: commands.Context, *, text: str | None = None) -> None:
        await self.speak(
            ctx,
            text,
            command_name="thugtts",
            reference_id="592d9287aa5245d79b5bd46a8c25d052",
        )

    @commands.command(
        name="gunnatts",
        description="Converts text to speech in a gunna voice and sends it as an mp3.",
        help="Generates a spoken mp3 of the given text in a gunna voice.",
        extras={"example": "gunnatts Hello! Welcome to Fish Audio."},
    )
    async def gunnatts(self, ctx: commands.Context, *, text: str | None = None) -> None:
        await self.speak(
            ctx,
            text,
            command_name="gunnatts",
            reference_id="5a84a128e08d4093b0537f5f0ad1c81b",
        )

    @commands.command(
        name="customtts",
        description="Manage custom Fish Audio TTS commands.",
        help=(
            "Create your own TTS commands. "
            "`customtts <name> <reference_id>` creates `,<name> <text>`; "
            "`customtts remove <name>` deletes it; `customtts list` shows them."
        ),
        extras={"example": "customtts keeftts 6ea3c15f427e402399022da1c96f0b70"},
    )
    @commands.is_owner()
    async def customtts(
        self,
        ctx: commands.Context,
        name: str | None = None,
        reference_id: str | None = None,
    ) -> None:
        if not name:
            prefix = await prefix_for(ctx)
            await ctx.send(
                view=simple_view(
                    "# Custom TTS\n\n"
                    "**Create / update**\n"
                    f"`{prefix}customtts <name> <reference_id>`\n\n"
                    "**Remove**\n"
                    f"`{prefix}customtts remove <name>`\n\n"
                    "**List**\n"
                    f"`{prefix}customtts list`\n\n"
                    f"Example: `{prefix}customtts keeftts "
                    "6ea3c15f427e402399022da1c96f0b70` "
                    f"then use `{prefix}keeftts hello`."
                )
            )
            return

        action = name.strip().lower().lstrip(",!")

        if action in {"list", "ls"}:
            if not self.custom_commands:
                await ctx.send(view=simple_view("No custom TTS commands yet."))
                return
            prefix = await prefix_for(ctx)
            lines = "\n".join(
                f"`{prefix}{cmd} <text>` → `{ref}`"
                for cmd, ref in sorted(self.custom_commands.items())
            )
            await ctx.send(view=simple_view(f"# Custom TTS Commands\n{lines}"))
            return

        if action in {"remove", "delete", "del"}:
            target = (reference_id or "").strip().lower().lstrip(",!")
            if target not in self.custom_commands:
                await ctx.send(
                    view=simple_view(f"`{target or '<name>'}` is not a custom TTS command.")
                )
                return
            self.custom_commands.pop(target, None)
            save_custom_commands(self.custom_commands)
            self._remove_custom_command(target)
            await ctx.send(
                view=simple_view(f"Removed `{await prefix_for(ctx)}{target}`.")
            )
            return

        reason = self._reserved(action)
        if reason:
            await ctx.send(view=simple_view(f"❌ {reason}"))
            return

        reference_id = (reference_id or "").strip()
        if not reference_id or any(char.isspace() for char in reference_id):
            await ctx.send(
                view=simple_view(
                    "❌ Provide the Fish Audio reference id: "
                    f"`{await prefix_for(ctx)}customtts <name> <reference_id>`."
                )
            )
            return

        updating = action in self.custom_commands
        self.custom_commands[action] = reference_id
        save_custom_commands(self.custom_commands)
        self._add_custom_command(action, reference_id)

        await ctx.send(
            view=simple_view(
                f"# Custom TTS {'Updated' if updating else 'Created'}\n"
                f"Use `{await prefix_for(ctx)}{action} <text>` to speak with "
                f"`{reference_id}`."
            )
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TTS(bot))
