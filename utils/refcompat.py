"""Adapter that lets the ported refcogs run on the main bot.

The reference bot (``refcogs`` + ``refutils``) assumed a handful of attributes
on the bot object (``bot.db``, ``bot.economy``, ``bot.mixtapes`` ...) and a
``Context`` with extra reply helpers. This module supplies both so those cogs
can be loaded unchanged:

* :func:`install_context_helpers` adds the refbot's ``respond`` / ``success`` /
  ``error`` / ``paginate`` / ``send_help`` / ``can_moderate`` methods to the
  standard :class:`commands.Context`. ``send`` is intentionally *not* patched,
  so the main bot's existing response behaviour is untouched.
* :func:`setup_refbot` opens the reference bot's own local SQLite database and
  builds the shared objects the ported cogs read off ``bot``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from discord.ext import commands

from config import EMBED_COLOR
from utils.core import file_mode
from utils.core.cache import BotCache
from utils.core.context import Context as RefContext
from utils.core.database import Database
from utils.core.economy import EconomyEngine
from utils.core.http import HTTP
from utils.core.mixtapes import MixtapeLibrary
from utils.core.settings import DashboardSettings

logger = logging.getLogger("discord.client")

#: Reply helpers from the refbot context that the ported cogs call on ``ctx``.
_CONTEXT_HELPERS = (
    "respond",
    "success",
    "error",
    "paginate",
    "send_help",
    "can_moderate",
)


def install_context_helpers() -> None:
    """Attach the refbot reply helpers to the standard context class."""
    for name in _CONTEXT_HELPERS:
        if not hasattr(commands.Context, name):
            setattr(commands.Context, name, getattr(RefContext, name))


async def setup_refbot(bot: commands.Bot) -> None:
    """Open the refbot SQLite DB and expose the shared objects on ``bot``."""
    install_context_helpers()

    # Palette / emoji the refbot context helpers expect on the bot object.
    bot.color = EMBED_COLOR
    bot.done = "✅"
    bot.warn = "⚠️"

    db_file = Path(getattr(bot, "ref_db_path", "data/refbot.db"))
    db_file.parent.mkdir(parents=True, exist_ok=True)
    bot.db = Database(db_file)
    await bot.db.connect()
    logger.info(f"Refbot SQLite database ready at {db_file}")

    bot.cache = BotCache(bot)
    bot.economy = EconomyEngine(bot)
    bot.mixtapes = MixtapeLibrary()
    bot.proxied_session = HTTP()
    bot.dash_settings = DashboardSettings(bot)

    try:
        await bot.dash_settings.preload()
    except Exception as e:
        logger.warning(f"Failed to preload refbot settings: {e}")

    #: Per-guild command disables, shared by reference so the dashboard can edit
    #: them live. The slash bridge already reads this mapping.
    bot.dash_disabled = bot.dash_settings.disabled

    try:
        await bot.cache.initialize_settings_cache()
    except Exception as e:
        logger.warning(f"Failed to initialize refbot cache: {e}")

    try:
        bot.file_mode = await file_mode.load(bot)
    except Exception as e:
        logger.warning(f"Failed to load refbot file mode: {e}")


async def teardown_refbot(bot: commands.Bot) -> None:
    """Close the refbot database, if it was opened."""
    db = getattr(bot, "db", None)
    if db is not None:
        try:
            await db.close()
        except Exception:
            pass
