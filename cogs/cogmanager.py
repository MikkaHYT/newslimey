"""Runtime cog management, restricted to the bot owner.

    !cog list                 what is available / loaded
    !cog load <cog>           load an unloaded cog
    !cog enable <cog>         alias for load (reloads it if already running)
    !cog unload <cog>         unload a cog
    !cog disable <cog>        alias for unload
    !cog reload <cog>         reload a cog after editing it
    !cog sync                 push the slash command tree to Discord

Load, enable and disable persist to the database so the choice survives a
restart; reload is a live-only operation.
"""

from __future__ import annotations

import difflib
import logging
import os
import time

import discord
from discord.ext import commands

from config import EMBED_COLOR, PREFIX
from utils.core.views import SimpleLayout

logger = logging.getLogger("discord.client")

COG_DIR = "cogs"

#: Files touched within this many seconds of the recorded load time still count
#: as already-loaded (guards against coarse filesystem timestamps).
UPDATE_SLACK = 1.0


def _layout(title: str, body: str) -> SimpleLayout:
    container = discord.ui.Container(accent_color=EMBED_COLOR)
    container.add_item(discord.ui.TextDisplay(f"# {title}"))
    container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
    container.add_item(discord.ui.TextDisplay(body))
    return SimpleLayout(container)


def _clean_name(name: str) -> str:
    """Normalise user input like ``cogs/foo.py`` down to ``foo``."""
    name = name.strip().rsplit("/", 1)[-1]
    if name.lower().endswith(".py"):
        name = name[:-3]
    if name.lower().startswith("cogs."):
        name = name[5:]
    return name.strip().lower()


class CogManager(commands.Cog):
    """Load, unload and reload cogs without restarting the bot."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Update-detection state lives on the bot so it survives this cog being
        # reloaded (the baseline for the reloaded cog itself is then kept too).
        state = getattr(bot, "_cog_update_state", None)
        if state is None:
            # ``started`` is the fallback baseline for cogs the startup loader
            # attached (their exact load time isn't exposed by discord.py).
            state = {"started": time.time(), "baselines": {}}
            bot._cog_update_state = state
        self._state: dict = state

    async def cog_check(self, ctx: commands.Context) -> bool:
        if not await self.bot.is_owner(ctx.author):
            raise commands.NotOwner("Only the bot owner can manage cogs.")
        return True

    # -- helpers ---------------------------------------------------------
    def _extensions(self) -> dict[str, str]:
        """Lowercase cog name -> importable extension path."""
        found: dict[str, str] = {}
        try:
            names = {
                file[:-3].lower()
                for file in os.listdir(COG_DIR)
                if file.endswith(".py") and not file.startswith("_")
            }
        except FileNotFoundError:
            names = set()
        for name in names:
            found[name] = f"cogs.{name}"
        for extension in self.bot.extensions:
            if extension.startswith("cogs."):
                found.setdefault(extension.split(".", 1)[1].lower(), extension)
        return dict(sorted(found.items()))

    def _resolve(self, name: str) -> tuple[str | None, list[str]]:
        extensions = self._extensions()
        key = _clean_name(name)
        if key in extensions:
            return extensions[key], []
        close = difflib.get_close_matches(key, list(extensions), n=3, cutoff=0.55)
        return None, [extensions[match] for match in close]

    def _cog_for(self, extension: str) -> commands.Cog | None:
        return next(
            (
                cog
                for cog in self.bot.cogs.values()
                if type(cog).__module__ == extension
            ),
            None,
        )

    # -- update detection ------------------------------------------------
    def _source_path(self, name: str) -> str:
        return os.path.join(COG_DIR, f"{name}.py")

    def _mtime(self, name: str) -> float | None:
        try:
            return os.path.getmtime(self._source_path(name))
        except OSError:
            return None

    def _baseline(self, extension: str) -> float:
        """When ``extension`` is considered to have started running.

        A cog reloaded through this manager records its source mtime; everything
        else (the startup-loaded cogs) falls back to when this cog was attached,
        which is early enough that any edit made afterwards is detected.
        """
        return self._state["baselines"].get(extension, self._state["started"])

    def _has_update(self, name: str, extension: str) -> bool:
        """True when a loaded cog's file is newer than its recorded start."""
        if extension not in self.bot.extensions:
            return False
        mtime = self._mtime(name)
        if mtime is None:
            return False
        return mtime > self._baseline(extension) + UPDATE_SLACK

    def _record_baseline(self, name: str, extension: str) -> None:
        mtime = self._mtime(name)
        if mtime is not None:
            self._state["baselines"][extension] = mtime

    async def _say(self, ctx: commands.Context, title: str, body: str) -> None:
        await ctx.send(view=_layout(title, body))

    def _db(self):
        db = getattr(self.bot, "database", None)
        if db and hasattr(db, "load_cog") and hasattr(db, "unload_cog"):
            return db
        return None

    async def _persist(self, name: str, *, loaded: bool) -> None:
        db = self._db()
        if not db:
            return
        try:
            if loaded:
                await db.load_cog(name)
            else:
                await db.unload_cog(name)
        except Exception:
            logger.exception("Failed to persist cog state for %s", name)

    async def _publish(self, ctx: commands.Context) -> str:
        """Re-publish slash commands after the tree changed."""
        try:
            if ctx.guild is not None:
                await self.bot.tree.sync(guild=ctx.guild)
                return "\n-# Slash commands synced to this server."
            count = await self.bot.tree.sync()
            return f"\n-# {len(count)} slash command(s) synced globally."
        except Exception:
            logger.exception("Slash command sync failed")
            return "\n-# Slash command sync failed; see logs."

    async def _unknown(
        self, ctx: commands.Context, name: str, close: list[str]
    ) -> None:
        hint = ""
        if close:
            hint = "\n\nDid you mean " + ", ".join(f"`{item}`" for item in close) + "?"
        await self._say(ctx, "Unknown cog", f"I don't know a cog called `{name}`.{hint}")

    # -- commands --------------------------------------------------------
    @commands.group(name="cog", aliases=["cogmgr"], invoke_without_command=True)
    async def cog(self, ctx: commands.Context) -> None:
        """Manage cogs at runtime."""
        await self.list_cogs(ctx)

    @cog.command(name="list")
    async def list_cogs(self, ctx: commands.Context) -> None:
        """Show every cog and whether it is loaded."""
        extensions = self._extensions()
        if not extensions:
            await self._say(ctx, "Cogs", "No cog files were found in `cogs/`.")
            return

        lines: list[str] = []
        loaded = 0
        stale: list[str] = []
        for name, extension in extensions.items():
            is_loaded = extension in self.bot.extensions
            loaded += is_loaded
            if not is_loaded:
                marker, detail = "⚪", "not loaded"
            else:
                cog = self._cog_for(extension)
                count = len(cog.get_commands()) if cog else 0
                if self._has_update(name, extension):
                    stale.append(name)
                    marker = "⬆️"
                    detail = f"{count} command(s) · **update available**"
                else:
                    marker = "🟢"
                    detail = f"{count} command(s)" if cog else "loaded"
            lines.append(f"{marker} `{name}` — {detail}")

        body = f"**{loaded}/{len(extensions)} loaded**\n" + "\n".join(lines)
        body += (
            f"\n\n-# `{PREFIX}cog load <name>` · `{PREFIX}cog unload <name>` · "
            f"`{PREFIX}cog reload <name>`"
        )
        if stale:
            summary = f"**{loaded}/{len(extensions)} loaded**"
            body = body.replace(
                summary, summary + f" · **{len(stale)} update(s) available**", 1
            )
            body += f" — run `{PREFIX}cog update` for " + ", ".join(
                f"`{item}`" for item in stale
            )
        await self._say(ctx, "Cogs", body)

    async def _do_load(
        self, ctx: commands.Context, name: str, extension: str, *, publish: bool = True
    ) -> None:
        try:
            await self.bot.load_extension(extension)
        except Exception as error:
            logger.exception("Failed to load %s", extension)
            await self._say(ctx, "Load failed", f"```{type(error).__name__}: {error}```")
            return
        await self._persist(name, loaded=True)
        self._record_baseline(name, extension)
        suffix = await self._publish(ctx) if publish else ""
        await self._say(ctx, "Cog loaded", f"`{extension}` is running now.{suffix}")

    async def _do_unload(self, ctx: commands.Context, name: str, extension: str) -> None:
        if type(self).__module__ == extension:
            await self._say(
                ctx, "Nice try", "I can't unload the cog that runs this command."
            )
            return
        try:
            await self.bot.unload_extension(extension)
        except Exception as error:
            logger.exception("Failed to unload %s", extension)
            await self._say(
                ctx, "Unload failed", f"```{type(error).__name__}: {error}```"
            )
            return
        await self._persist(name, loaded=False)
        self._state["baselines"].pop(extension, None)
        suffix = await self._publish(ctx) if publish else ""
        await self._say(ctx, "Cog unloaded", f"`{extension}` stopped.{suffix}")

    async def _do_reload(
        self, ctx: commands.Context, extension: str, *, publish: bool = True
    ) -> None:
        name = extension.split(".", 1)[1] if "." in extension else extension
        try:
            await self.bot.reload_extension(extension)
        except Exception as error:
            logger.exception("Failed to reload %s", extension)
            await self._say(
                ctx, "Reload failed", f"```{type(error).__name__}: {error}```"
            )
            return
        self._record_baseline(name, extension)
        suffix = await self._publish(ctx) if publish else ""
        await self._say(
            ctx, "Cog reloaded", f"`{extension}` came back cleanly.{suffix}"
        )

    @cog.command(name="load")
    async def load_cog(self, ctx: commands.Context, *, name: str) -> None:
        """Load a cog that isn't running yet."""
        extension, close = self._resolve(name)
        if extension is None:
            await self._unknown(ctx, name, close)
            return
        if extension in self.bot.extensions:
            await self._say(
                ctx,
                "Already loaded",
                f"`{extension}` is already running. Use `{PREFIX}cog reload {name}` "
                "to restart it.",
            )
            return
        await self._do_load(ctx, _clean_name(name), extension)

    @cog.command(name="enable")
    async def enable_cog(self, ctx: commands.Context, *, name: str) -> None:
        """Bring a cog online (loads it, or reloads it if already running)."""
        extension, close = self._resolve(name)
        if extension is None:
            await self._unknown(ctx, name, close)
            return
        if extension in self.bot.extensions:
            await self._do_reload(ctx, extension)
        else:
            await self._do_load(ctx, _clean_name(name), extension)

    @cog.command(name="unload")
    async def unload_cog(self, ctx: commands.Context, *, name: str) -> None:
        """Stop a cog without touching the others."""
        extension, close = self._resolve(name)
        if extension is None:
            await self._unknown(ctx, name, close)
            return
        if extension not in self.bot.extensions:
            await self._say(ctx, "Not loaded", f"`{extension}` isn't running.")
            return
        await self._do_unload(ctx, _clean_name(name), extension)

    @cog.command(name="disable")
    async def disable_cog(self, ctx: commands.Context, *, name: str) -> None:
        """Stop a cog (alias for unload); stays disabled across restarts."""
        extension, close = self._resolve(name)
        if extension is None:
            await self._unknown(ctx, name, close)
            return
        if extension not in self.bot.extensions:
            await self._say(ctx, "Not loaded", f"`{extension}` isn't running.")
            return
        await self._do_unload(ctx, _clean_name(name), extension)

    @cog.command(name="reload")
    async def reload_cog(self, ctx: commands.Context, *, name: str) -> None:
        """Reload a cog after editing it."""
        extension, close = self._resolve(name)
        if extension is None:
            await self._unknown(ctx, name, close)
            return
        if extension not in self.bot.extensions:
            await self._say(
                ctx,
                "Not loaded",
                f"`{extension}` isn't running. Use `{PREFIX}cog load {name}` instead.",
            )
            return
        await self._do_reload(ctx, extension)

    @cog.command(name="update")
    async def update_cogs(self, ctx: commands.Context, *, name: str | None = None) -> None:
        """Reload cogs whose source files changed since they were loaded.

        With no name, every loaded cog with a newer file is reloaded. Pass a
        name to force a reload of that one regardless of its timestamp.
        """
        if name:
            extension, close = self._resolve(name)
            if extension is None:
                await self._unknown(ctx, name, close)
                return
            if extension not in self.bot.extensions:
                await self._say(
                    ctx,
                    "Not loaded",
                    f"`{extension}` isn't running. Use `{PREFIX}cog load {name}`.",
                )
                return
            await self._do_reload(ctx, extension)
            return

        stale = [
            (cog_name, extension)
            for cog_name, extension in self._extensions().items()
            if self._has_update(cog_name, extension)
        ]
        if not stale:
            await self._say(
                ctx,
                "Up to date",
                "No loaded cogs have changed since they started.",
            )
            return

        sep = chr(10)
        updated: list[str] = []
        failed: list[str] = []
        for cog_name, extension in stale:
            try:
                await self.bot.reload_extension(extension)
            except Exception as error:
                logger.exception("Failed to update %s", extension)
                failed.append(f"`{extension}` ({type(error).__name__}: {error})")
                continue
            self._record_baseline(cog_name, extension)
            updated.append(extension)

        parts: list[str] = []
        if updated:
            parts.append("✅ **Reloaded**" + sep + sep.join(f"`{e}`" for e in updated))
        if failed:
            parts.append("🚫 **Failed**" + sep + sep.join(failed))
        if updated:
            parts.append(await self._publish(ctx))
        await self._say(ctx, "Cog update", sep.join(parts))

    @cog.command(name="sync")
    async def sync_commands(self, ctx: commands.Context, scope: str = "server") -> None:
        """Push the slash command tree to Discord."""
        wanted = (scope or "server").strip().lower()
        if wanted in {"global", "all", "everywhere"} or ctx.guild is None:
            try:
                synced = await self.bot.tree.sync()
            except Exception as error:
                logger.exception("Slash command sync failed")
                await self._say(
                    ctx, "Sync failed", f"```{type(error).__name__}: {error}```"
                )
                return
            await self._say(
                ctx,
                "Synced",
                f"Published **{len(synced)}** command(s) globally (can take up to an "
                "hour to appear everywhere).",
            )
            return
        if wanted not in {"server", "guild", "here"}:
            await self._say(
                "Unknown scope",
                f"Use `{PREFIX}cog sync` for this server or "
                f"`{PREFIX}cog sync global` for everywhere.",
            )
            return
        try:
            synced = await self.bot.tree.sync(guild=ctx.guild)
        except Exception as error:
            logger.exception("Slash command sync failed")
            await self._say(ctx, "Sync failed", f"```{type(error).__name__}: {error}```")
            return
        await self._say(
            ctx, "Synced", f"Published **{len(synced)}** command(s) to this server."
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CogManager(bot))
