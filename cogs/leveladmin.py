"""Owner tools for the level system: the level-up switch and the role ladder.

    !admin popups [on|off]          level-up cards, bot-wide
    !admin popups @member [on|off]  one member's own switch
    !admin rolelevel 5 @Role        a role handed out on reaching level 5
    !admin rolelevel 5 @Role remove take that one role back off the level
    !admin rolelevel 5 clear        clear every role at level 5
    !admin rolelevel clear all      wipe the ladder
    !admin rolelevel 5 move to 10   relocate a tier
    !admin rolelevel 5 copy to 10   duplicate it instead
    !admin rolelevels               the ladder, its switches, what can't be given
    !admin rolesettings mode replace|stack, strip|announce|dm on|off
    !admin rolecheck @member        dry run: what the ladder would change
    !admin syncroles @member|all    re-apply the ladder to someone (or everyone)
    !admin disable <command>        disable a command for this server
    !admin enable <command>         re-enable it here
    !admin serverpfp <image|url>    the bot's avatar for this server only
    !admin servername <name>        the bot's nickname here
    !admin status <text>            pin a status on the bot until it restarts
    !admin resetstatus              hand the presence back to the rotation
    !admin allow <id>               add a user ID to the bot admin list
    !admin deny <id>                remove them from it
    !admin admins                   list the IDs that were added
    !admin logs                     which servers are left out of the command log
    !admin logs exclude <id>        stop logging one server's commands
    !admin logs include <id>        log that server again
    !admin panel [member]           the clickable editor for one member
    !admin money <member> set 5000  edit wallet / bank / level / xp / job
    !admin bank <member> add 250    (set / add / remove)
    !admin item <member> chain 2    give or take inventory items
    !admin resetbal <member>        empty wallet and bank
    !admin reset <member>           wipe an account back to factory settings

Every command here is gated on the owner IDs the bot was started with, plus any
extra IDs added with `!admin allow`, so anyone else gets nothing at all.

The ladder lives in :mod:`utils.core.levelroles` - the very module the level-up
handler calls - so the dry run a moderator previews and the grant that actually
happens are computed by exactly the same code and can never disagree.

There are two separate level-up switches, deliberately. ``!admin popups`` is the
bot-wide one (how the bot behaves for everyone); ``!popups`` is the one each
member flips for themselves. Turning the bot-wide switch off silences the cards
without touching anyone's own setting, and rewards, XP and level roles keep
paying out either way - only the announcement is skipped.
"""

from __future__ import annotations

import asyncio
import logging
import re
from decimal import Decimal

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from config import EMBED_COLOR, EMBED_COLORS, PREFIX, leveling
from utils.core import levelroles, preferences, userprefs
from utils.core.http_session import borrowed
from utils.core.converters import find_member
from utils.core.economy import format_cash, parse_amount
from utils.core.leveling import xp_for_level
from utils.core.settings import LEVEL_POPUPS_KEY
from utils.core.views import SimpleLayout, swap_layout_message, usage_card

logger = logging.getLogger("slimey.leveladmin")


def panel(*items: discord.ui.Item, won: bool | None = None) -> SimpleLayout:
    """A card in the house style, tinted by whether the command worked."""
    colour = EMBED_COLOR
    if won is True:
        colour = EMBED_COLORS["success"]
    elif won is False:
        colour = EMBED_COLORS["error"]
    box = discord.ui.Container(accent_color=colour)
    for item in items:
        box.add_item(item)
    return SimpleLayout(box)


def notice(message: str, *, emoji: str = "❌") -> SimpleLayout:
    """A card that only explains a command, so it doesn't spend a cooldown."""
    return usage_card(message, emoji=emoji)


def divider() -> discord.ui.Separator:
    return discord.ui.Separator(spacing=discord.SeparatorSpacing.small)


#: The edit modes offered by ``!admin panel`` and the value commands.
MODES = ("set", "add", "remove")

#: ``field -> (label, emoji, needs a value?)``. Anything with no value runs
#: straight away when it is applied instead of opening a modal.
FIELDS: dict[str, tuple[str, str, bool]] = {
    "wallet": ("Wallet", "💰", True),
    "bank": ("Bank", "🏦", True),
    "level": ("Level", "🏅", True),
    "xp": ("XP", "✨", True),
    "job": ("Job", "🧾", True),
    "item": ("Inventory item", "🎒", True),
    "balance": ("Reset wallet and bank", "💸", False),
    "reset": ("Reset the whole account", "♻️", False),
}


async def owner_check(ctx: commands.Context) -> bool:
    """Only the IDs the bot was started with get past this."""
    if await ctx.bot.is_owner(ctx.author):
        return True
    raise commands.NotOwner("Only the bot owner can use the admin menu.")


class Admin(commands.Cog):
    """Owner-only switches for level-up cards and the level-role ladder."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        return await owner_check(ctx)

    # ------------------------------------------------------------------
    # Member lookup
    # ------------------------------------------------------------------
    async def _target(
        self, ctx: commands.Context, query: str
    ) -> discord.Member | None:
        """Resolve a member from a mention, an ID, or the closest name.

        Deliberately loose: a moderator half-remembers a nickname, and "I can't
        find that member" for a typo is worse than editing the person they
        obviously meant.
        """
        if ctx.guild is None:
            await ctx.send(view=notice("That only works inside a server."))
            return None
        member = await find_member(ctx.guild, query)
        if member is None:
            await ctx.send(
                view=notice(
                    f"I couldn't match `{query}` to anyone in this server — "
                    "mention them or paste their ID."
                )
            )
            return None
        return member

    # ------------------------------------------------------------------
    # Values editor (!admin panel)
    # ------------------------------------------------------------------
    @property
    def engine(self):
        """The reference economy engine, when the compat layer is online."""
        return getattr(self.bot, "economy", None)

    async def _sql_wallet(self, user_id: int) -> tuple[Decimal, Decimal]:
        """The shop economy's wallet and bank for *user_id*."""
        wallet_id = await self.bot.database.get_wallet_id_for_user(int(user_id))
        wallet = Decimal(str(await self.bot.database.get_wallet_balance(wallet_id) or 0))
        bank = Decimal(str(await self.bot.database.get_bank_balance(wallet_id) or 0))
        return wallet, bank

    async def _level_xp(self, user_id: int, *, guild_id: int) -> tuple[int, int]:
        row = await self.bot.db.fetchrow(
            "SELECT level, xp FROM user_levels "
            "WHERE guild_id = %s AND user_id = %s",
            int(guild_id),
            int(user_id),
        )
        return (int(row[0]), int(row[1])) if row else (1, 0)

    async def _write_level(
        self, user_id: int, level: int, xp: int, *, guild_id: int
    ) -> None:
        await self.bot.db.execute(
            "INSERT INTO user_levels (guild_id, user_id, level, xp) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
            "level = excluded.level, xp = excluded.xp",
            int(guild_id),
            int(user_id),
            max(1, int(level)),
            max(0, int(xp)),
        )

    async def _sync_engine(self, user_id: int, **balances) -> None:
        """Mirror a balance onto the rank ledger, if it is available.

        The bot keeps two ledgers (the shop economy and the reference economy
        the rank card reads); an owner edit writes both so ``!balance`` and
        ``!rank`` never disagree after an edit.
        """
        engine = self.engine
        if engine is None:
            return
        try:
            await engine.set_balances(int(user_id), **balances)
        except Exception:
            logger.exception("Could not sync the rank ledger for %s", user_id)

    def _jobs(self) -> dict:
        """The live job catalogue from the economy cog."""
        cog = self.bot.get_cog("Economy")
        return dict(getattr(cog, "JOBS", {}) or {})

    def resolve_job(self, query: str) -> str | None:
        wanted = (query or "").strip().lower().replace(" ", "")
        jobs = self._jobs()
        for key, data in jobs.items():
            title = str(data.get("title", "")).lower().replace(" ", "")
            if wanted in {key.lower(), title}:
                return key
        matches = [key for key in jobs if key.lower().startswith(wanted)]
        return matches[0] if len(matches) == 1 else None

    async def resolve_item(self, query: str):
        """A shop item from a name or a unique prefix of one."""
        wanted = (query or "").strip().lower().replace(" ", "")
        if not wanted:
            return None
        items = await self.bot.database.list_shop_items()
        for item in items:
            if wanted == item.name.lower().replace(" ", ""):
                return item
        matches = [
            item
            for item in items
            if item.name.lower().replace(" ", "").startswith(wanted)
        ]
        return matches[0] if len(matches) == 1 else None

    async def snapshot(self, member: discord.Member) -> dict[str, object]:
        wallet, bank = await self._sql_wallet(member.id)
        level, xp = await self._level_xp(member.id, guild_id=member.guild.id)
        job = await self.bot.database.get_job(member.id)
        inventory = await self.bot.database.get_user_inventory_grouped(member.id)
        items = sum(int(entry.get("quantity") or 0) for entry in inventory)
        return {
            "wallet": wallet,
            "bank": bank,
            "level": level,
            "xp": xp,
            "job": job.title if job else None,
            "items": items,
        }

    def state_block(self, member: discord.Member, state: dict[str, object]) -> str:
        job = state.get("job") or "unemployed"
        return (
            f"**Wallet** {format_cash(state.get('wallet', 0))} · "
            f"**Bank** {format_cash(state.get('bank', 0))}\n"
            f"**Level** {state.get('level', 1)} · "
            f"**XP** {int(state.get('xp', 0)):,}\n"
            f"**Job** {job} · **Items** {state.get('items', 0)}"
        )

    async def apply_value(
        self, member: discord.Member, field: str, mode: str, raw: str
    ) -> tuple[bool, str]:
        """Run one edit. Returns ``(ok, message)``."""
        user_id = member.id
        mode = mode if mode in MODES else "set"

        if field in {"wallet", "bank"}:
            wallet, bank = await self._sql_wallet(user_id)
            current = int(wallet if field == "wallet" else bank)
            # The rank ledger keeps its own balance, so the same operation is
            # applied to each from its own starting figure — an ``add`` adds
            # everywhere instead of forcing the (much smaller) rank wallet up to
            # the shop wallet's value.
            engine = self.engine
            engine_current = None
            if engine is not None:
                try:
                    account = await engine.account(user_id)
                    engine_current = int(
                        account.wallet if field == "wallet" else account.bank
                    )
                except Exception:
                    logger.exception("Could not read the rank ledger for %s", user_id)
            # ``available=0`` keeps ``parse_amount`` from clamping: an owner
            # setting a wallet to 5,000 doesn't want it capped at the old value.
            amount = parse_amount(raw, 0)
            if amount is None:
                return False, f"I need an actual number — `{raw}` doesn't give me one."
            amount = int(amount)

            def _applied(base: int) -> int:
                if mode == "set":
                    return amount
                if mode == "add":
                    return base + amount
                return max(0, base - amount)

            target = max(0, _applied(current))
            await self.bot.database.admin_set_balances(user_id, **{field: target})
            if engine is not None and engine_current is not None:
                await self._sync_engine(
                    user_id, **{field: max(0, _applied(engine_current))}
                )
            label = FIELDS[field][0]
            return True, f"**{label}** {mode} → {format_cash(target)}"

        if field in {"level", "xp"}:
            level, xp = await self._level_xp(user_id, guild_id=member.guild.id)
            try:
                number = int(float(str(raw).strip().replace(",", "")))
            except ValueError:
                return False, "That has to be a whole number."
            if field == "level":
                if mode == "set":
                    level = max(1, number)
                elif mode == "add":
                    level = max(1, level + number)
                else:
                    level = max(1, level - number)
                # Clamp the progress bar into the new level so the card is sane.
                xp = min(xp, max(0, xp_for_level(level) - 1))
            else:
                if mode == "set":
                    xp = max(0, number)
                elif mode == "add":
                    xp = max(0, xp + number)
                else:
                    xp = max(0, xp - number)
            await self._write_level(user_id, level, xp, guild_id=member.guild.id)
            return True, (
                f"**{FIELDS[field][0]}** {mode} → "
                f"{level if field == 'level' else xp:,}"
            )

        if field == "job":
            target = (raw or "").strip().lower()
            if target in {"none", "quit", "fire", "unemployed", ""}:
                await self.bot.database.fire_employee(user_id)
                return True, "**Job** → unemployed"
            key = self.resolve_job(target)
            if key is None:
                known = ", ".join(f"`{name}`" for name in self._jobs()) or "none"
                return False, f"I don't know that job. Try {known}."
            data = self._jobs()[key]
            await self.bot.database.fire_employee(user_id)
            await self.bot.database.apply_for_job(
                user_id=user_id,
                job_title=data["title"],
                base_salary=Decimal(str(data["base_salary"])),
            )
            return True, f"**Job** → {data['title']}"

        if field == "item":
            parts = (raw or "").split()
            quantity = 1
            if len(parts) > 1 and parts[-1].lstrip("-").isdigit():
                quantity = int(parts.pop())
            item = await self.resolve_item(" ".join(parts))
            if item is None:
                known = await self.bot.database.list_shop_items()
                sample = ", ".join(f"`{it.name}`" for it in known[:8])
                return False, f"I don't know that item. Try {sample}."
            if quantity < 0 or mode == "remove":
                taken = await self.bot.database.admin_remove_item(
                    user_id, item.name, abs(quantity) or 1
                )
                if not taken:
                    return False, f"{member.display_name} doesn't have **{item.name}**."
                return True, f"Removed **{taken}x {item.name}**"
            give = max(1, quantity)
            # One row per copy — the inventory view counts rows, so a single
            # row of quantity 3 would look like one item.
            for _ in range(give):
                await self.bot.database.add_item_to_inventory(
                    user_id=user_id,
                    name=item.name,
                    description=item.description,
                    quantity=1,
                    item_type=item.item_type,
                    category=item.category,
                    rarity=item.rarity,
                    effect=item.effect,
                    effect_value=item.effect_value,
                    effect_duration=item.effect_duration,
                    cooldown_seconds=item.cooldown_seconds,
                    targetable=bool(item.targetable),
                    daily_limit=item.daily_limit,
                    global_daily_limit=item.global_daily_limit,
                    tradable=bool(item.tradable),
                )
            return True, f"Gave **{give}x {item.name}**"

        return False, "I don't know how to edit that."

    async def reset_balance(self, member: discord.Member) -> tuple[bool, str]:
        """Empty a member's wallet and bank; nothing else is touched."""
        wallet, bank = await self._sql_wallet(member.id)
        await self.bot.database.admin_set_balances(member.id, wallet=0, bank=0)
        await self._sync_engine(member.id, wallet=0, bank=0)
        return True, (
            f"**Wallet** {format_cash(wallet)} → {format_cash(0)}\n"
            f"**Bank** {format_cash(bank)} → {format_cash(0)}\n"
            "-# Level, XP, items and job were left alone."
        )

    async def run_action(self, member: discord.Member, field: str) -> tuple[bool, str]:
        """The edits that need no value."""
        if field == "balance":
            return await self.reset_balance(member)

        if field == "reset":
            await self.bot.database.admin_set_balances(member.id, wallet=0, bank=0)
            await self.bot.database.fire_employee(member.id)
            inventory = await self.bot.database.get_user_inventory_grouped(member.id)
            for entry in inventory:
                await self.bot.database.admin_remove_item(
                    member.id, entry["name"], int(entry.get("quantity") or 0) or 1
                )
            await self._write_level(member.id, 1, 0, guild_id=member.guild.id)
            await self._sync_engine(member.id, wallet=0, bank=0, streak=0)
            return True, (
                "Reset to **0**, level **1**, empty inventory and no job."
            )

        return False, "That isn't an action I can run on its own."

    async def open_panel(self, ctx: commands.Context, member: discord.Member) -> None:
        view = AdminPanel(self, member=member, invoker=ctx.author.id)
        await view.load()
        view.message = await ctx.send(view=view)

    # ------------------------------------------------------------------
    # The level-up switch
    # ------------------------------------------------------------------
    def popups_state(self) -> bool:
        """Whether level-up cards are on for the bot as a whole."""
        settings = getattr(self.bot, "dash_settings", None)
        if settings is None:
            return leveling.announce_level_ups
        return leveling.announce_level_ups and settings.flag(LEVEL_POPUPS_KEY, True)

    async def _set_popups(self, value: bool | None) -> tuple[bool, str]:
        """Write the bot-wide switch. ``None`` restores the default."""
        settings = getattr(self.bot, "dash_settings", None)
        if settings is None:
            return False, "The settings store isn't ready yet — try again in a moment."
        await settings.set_flag(LEVEL_POPUPS_KEY, value)
        if value is None:
            return True, "🎉 **Level-up messages** → reset to their default (on)"
        return True, f"🎉 **Level-up messages** → **{'on' if value else 'off'}**"

    async def _popup_card(
        self, member: discord.Member | None = None
    ) -> SimpleLayout:
        """The level-up switch, bot-wide, plus one member's own setting."""
        broad = self.popups_state()
        lines = [
            "# 🎉 Level-up messages",
            f"> **Everyone** {'on' if broad else 'off'} — "
            f"`{PREFIX}admin popups {'off' if broad else 'on'}`",
        ]
        if member is not None:
            personal = preferences.is_on(
                await preferences.read(self.bot.db, member.id, preferences.POPUPS)
            )
            lines.append(
                f"> **{member.display_name}** {'on' if personal else 'off'} — "
                f"`{PREFIX}admin popups {member.display_name} "
                f"{'off' if personal else 'on'}`"
            )
        if not broad:
            lines.append(
                "-# the bot-wide switch is off, so nobody gets cards right now"
            )
        lines.append(f"-# members can flip their own with `{PREFIX}popups`")
        return panel(discord.ui.TextDisplay("\n".join(lines)), won=True)

    # ------------------------------------------------------------------
    # The ladder
    # ------------------------------------------------------------------
    async def ladder(self, guild_id: int) -> levelroles.Ladder:
        """This guild's ladder plus its behaviour switches."""
        return await levelroles.load(self.bot.db, guild_id)

    def _levels_cog(self):
        """The levels cog, found by capability rather than by name."""
        return next(
            (
                candidate
                for candidate in self.bot.cogs.values()
                if hasattr(candidate, "sync_level_roles")
            ),
            None,
        )

    async def _level_of(self, user_id: int, *, guild_id: int) -> int:
        row = await self.bot.db.fetchval(
            "SELECT level FROM user_levels WHERE guild_id = %s AND user_id = %s",
            int(guild_id),
            int(user_id),
        )
        return int(row or 1)

    def ladder_block(self, guild: discord.Guild, ladder: levelroles.Ladder) -> str:
        """Render the ladder, flagging roles that can't actually be handed out."""
        if ladder.empty:
            return (
                "No level roles yet. Add one with "
                f"`{PREFIX}admin rolelevel 5 @YourRole`."
            )
        lines = []
        for level in ladder.tiers():
            shown: list[str] = []
            for role_id in ladder.roles_at(level):
                role = guild.get_role(role_id)
                if role is None:
                    shown.append(f"⚠️ deleted (`{role_id}`)")
                elif not role.is_assignable():
                    shown.append(f"⚠️ {role.mention} sits above my top role")
                else:
                    shown.append(role.mention)
            lines.append(f"> **Level {level}** — " + ", ".join(shown))
        return "\n".join(lines)

    @staticmethod
    def settings_block(ladder: levelroles.Ladder) -> str:
        mode = (
            "replace — only the highest tier they've reached"
            if ladder.mode == "replace"
            else "stack — every tier up to their level"
        )
        return (
            f"> **Mode** {mode}\n"
            f"> **Strip stale** {'on' if ladder.strip_stale else 'off'}"
            " — take back ladder roles they no longer earn\n"
            f"> **Announce** {'on' if ladder.announce else 'off'}"
            " — name the role on the level-up card\n"
            f"> **DM** {'on' if ladder.dm else 'off'}"
            " — message the member about it too"
        )

    async def _show_ladder(
        self, ctx: commands.Context, *, headline: str, note: str = ""
    ) -> None:
        """Post the ladder, its settings and the follow-up hint."""
        if ctx.guild is None:
            return
        ladder = await self.ladder(ctx.guild.id)
        lead = f"{note}\n\n" if note else ""
        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    f"# {headline}\n"
                    f"{lead}"
                    f"{self.ladder_block(ctx.guild, ladder)}\n\n"
                    f"**Settings**\n"
                    f"{self.settings_block(ladder)}\n\n"
                    f"-# `{PREFIX}admin syncroles @member` applies the ladder to "
                    "someone who is already past a level."
                ),
                won=True,
            )
        )

    @staticmethod
    def _as_level(token: str) -> int | None:
        try:
            value = int(str(token).strip())
        except (TypeError, ValueError):
            return None
        return value if value >= 1 else None

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    @commands.group(name="admin", invoke_without_command=True)
    async def admin(self, ctx: commands.Context) -> None:
        """Open the level-system menu."""
        container = discord.ui.Container(accent_color=EMBED_COLOR)
        container.add_item(
            discord.ui.TextDisplay(
                f"# 🛠️ Level admin\n"
                f"-# `{PREFIX}admin popups on|off` · level-up cards, bot-wide\n"
                f"-# `{PREFIX}admin rolelevels` · the role ladder\n"
                f"-# `{PREFIX}admin rolelevel <level> @Role` · reward a role\n"
                f"-# `{PREFIX}admin disable|enable <command>` · per-server toggles\n"
                f"-# `{PREFIX}admin serverpfp <image>` · per-server avatar\n"
                f"-# `{PREFIX}admin servername <name>` · per-server nickname\n"
                f"-# `{PREFIX}admin status <text>` · pin a status until restart\n"
                f"-# `{PREFIX}admin resetstatus` · back to the rotating status\n"
                f"-# `{PREFIX}admin allow|deny <id>` · add or remove a bot admin\n"
                f"-# `{PREFIX}admin logs exclude|include <id>` · command log\n"
                f"-# `{PREFIX}admin panel [member]` · edit balances, XP, jobs, items"
            )
        )
        container.add_item(divider())
        if ctx.guild is not None:
            ladder = await self.ladder(ctx.guild.id)
            container.add_item(
                discord.ui.TextDisplay(self.ladder_block(ctx.guild, ladder))
            )
            container.add_item(divider())
        container.add_item(
            discord.ui.TextDisplay(
                f"> **Level-up cards** "
                f"{'on' if self.popups_state() else 'off'}"
            )
        )
        container.add_item(
            discord.ui.TextDisplay(
                f"-# members flip their own with `{PREFIX}popups`"
            )
        )
        await ctx.send(view=SimpleLayout(container))

    # ------------------------------------------------------------------
    # The values editor
    # ------------------------------------------------------------------
    @admin.command(name="panel", aliases=("menu", "edit"))
    async def panel_command(
        self, ctx: commands.Context, *, member: str | None = None
    ) -> None:
        """Open the clickable editor for one member.

        Usage: `{PREFIX}admin panel` for yourself, or
        `{PREFIX}admin panel @member` — a partial name works too.
        """
        target = ctx.author
        if member and member.strip():
            found = await self._target(ctx, member.strip())
            if found is None:
                return
            target = found
        await self.open_panel(ctx, target)

    @staticmethod
    def _parse_edit(value: str) -> tuple[str, str]:
        """Split ``set 500`` into ``("set", "500")``; defaults to set."""
        text = (value or "").strip()
        parts = text.split(maxsplit=1)
        if len(parts) == 2 and parts[0].lower() in MODES:
            return parts[0].lower(), parts[1]
        return "set", text

    async def _edit(
        self,
        ctx: commands.Context,
        member: discord.Member,
        field: str,
        mode: str,
        raw: str,
    ) -> None:
        if member.bot:
            await ctx.send(view=notice("Bots don't have accounts."))
            return
        ok, message = await self.apply_value(member, field, mode, raw)
        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    f"# {'✅ Applied' if ok else '❌ Rejected'}\n"
                    f"> **Target** {member.mention}\n"
                    f"> {message}"
                ),
                won=ok,
            )
        )

    @admin.command(name="money", aliases=("wallet", "cash"))
    async def money(self, ctx: commands.Context, member: str, *, value: str) -> None:
        """Edit someone's wallet.

        Usage: `{PREFIX}admin money @member set 5000` ·
        `{PREFIX}admin money @member add 250` · `... remove all`.
        """
        target = await self._target(ctx, member)
        if target is None:
            return
        mode, raw = self._parse_edit(value)
        await self._edit(ctx, target, "wallet", mode, raw)

    @admin.command(name="bank")
    async def bank(self, ctx: commands.Context, member: str, *, value: str) -> None:
        """Edit someone's bank balance.

        Usage: `{PREFIX}admin bank @member set 10000`.
        """
        target = await self._target(ctx, member)
        if target is None:
            return
        mode, raw = self._parse_edit(value)
        await self._edit(ctx, target, "bank", mode, raw)

    @admin.command(name="level", aliases=("lvl",))
    async def level(self, ctx: commands.Context, member: str, *, value: str) -> None:
        """Edit someone's level.

        Usage: `{PREFIX}admin level @member set 25` · `... add 5`.
        """
        target = await self._target(ctx, member)
        if target is None:
            return
        mode, raw = self._parse_edit(value)
        await self._edit(ctx, target, "level", mode, raw)

    @admin.command(name="xp")
    async def xp(self, ctx: commands.Context, member: str, *, value: str) -> None:
        """Edit someone's XP inside their current level.

        Usage: `{PREFIX}admin xp @member set 500`.
        """
        target = await self._target(ctx, member)
        if target is None:
            return
        mode, raw = self._parse_edit(value)
        await self._edit(ctx, target, "xp", mode, raw)

    @admin.command(name="job")
    async def job(
        self, ctx: commands.Context, member: str, *, title: str = "none"
    ) -> None:
        """Set or clear someone's job.

        Usage: `{PREFIX}admin job @member developer` · `... none`.
        """
        target = await self._target(ctx, member)
        if target is None:
            return
        await self._edit(ctx, target, "job", "set", title)

    @admin.command(name="item", aliases=("give", "inventory"))
    async def item(self, ctx: commands.Context, member: str, *, value: str) -> None:
        """Give or take shop items.

        Usage: `{PREFIX}admin item @member Padlock 2` · `... Padlock -2`.
        """
        target = await self._target(ctx, member)
        if target is None:
            return
        await self._edit(ctx, target, "item", "add", value)

    @admin.command(name="resetbal", aliases=("resetbalance", "clearbal"))
    async def resetbal(self, ctx: commands.Context, member: str) -> None:
        """Empty someone's wallet and bank (nothing else is touched)."""
        target = await self._target(ctx, member)
        if target is None:
            return
        if target.bot:
            await ctx.send(view=notice("Bots don't have accounts."))
            return
        ok, message = await self.reset_balance(target)
        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    f"# 💸 Balance reset\n"
                    f"> **Target** {target.mention}\n"
                    f"> {message}"
                ),
                won=ok,
            )
        )

    @admin.command(name="reset", aliases=("wipe",))
    async def reset(self, ctx: commands.Context, member: str) -> None:
        """Wipe an account back to factory settings."""
        target = await self._target(ctx, member)
        if target is None:
            return
        if target.bot:
            await ctx.send(view=notice("Bots don't have accounts."))
            return
        ok, message = await self.run_action(target, "reset")
        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(f"# ♻️ {target.mention}\n> {message}"),
                won=ok,
            )
        )

    # ------------------------------------------------------------------
    # Per-server command availability
    # ------------------------------------------------------------------
    def _command_exists(self, name: str) -> bool:
        """Whether *name* is a prefix command, slash command or a group."""
        if self.bot.get_command(name) is not None:
            return True
        if self.bot.tree.get_command(name) is not None:
            return True
        for cmd in self.bot.tree.walk_commands():
            if isinstance(cmd, app_commands.Group):
                if cmd.name == name:
                    return True
                if any(sub.name == name for sub in cmd.walk_commands()):
                    return True
        return False

    def _qualified_command_name(self, name: str) -> str | None:
        """The canonical name the invoke/guardrail checks compare against."""
        cmd = self.bot.get_command(name)
        if cmd is not None:
            return cmd.qualified_name
        app = self.bot.tree.get_command(name)
        if app is not None:
            return app.qualified_name
        for group in self.bot.tree.walk_commands():
            if isinstance(group, app_commands.Group):
                if group.name == name:
                    return group.qualified_name
                for sub in group.walk_commands():
                    if sub.name == name:
                        return sub.qualified_name
        return None

    async def _set_enabled(
        self, ctx: commands.Context, raw: str, enabled: bool
    ) -> None:
        """Toggle one command for this server only."""
        if ctx.guild is None:
            await ctx.send(view=notice("That's a per-server switch."))
            return
        token = (raw or "").strip().lstrip("!./").strip()
        if not token:
            action = "enable" if enabled else "disable"
            await ctx.send(
                view=notice(f"Usage: `{PREFIX}admin {action} <command>`.")
            )
            return
        if not self._command_exists(token):
            await ctx.send(view=notice(f"The command `{token}` does not exist."))
            return

        name = self._qualified_command_name(token) or token
        current = await self.bot.database.get_command_status_explicit(
            name, channel_id=None, guild_id=ctx.guild.id
        )

        cleared = 0
        if enabled:
            # Enabling for the server is authoritative: wipe any per-channel
            # disables so the command works for everyone again, not just in
            # channels that were never touched.
            channel_ids = [c.id for c in (getattr(ctx.guild, "channels", None) or [])]
            cleared = await self.bot.database.clear_channel_command_statuses(
                name, channel_ids
            )
            if current is True and not cleared:
                await ctx.send(
                    view=notice(
                        f"`{name}` is already enabled in this server.", emoji="ℹ️"
                    )
                )
                return
        elif current is False:
            await ctx.send(
                view=notice(
                    f"`{name}` is already disabled in this server.", emoji="ℹ️"
                )
            )
            return

        await self.bot.database.set_command_status(
            name, enabled=enabled, channel_id=None, guild_id=ctx.guild.id
        )
        verb = "enabled" if enabled else "disabled"
        cleared_note = ""
        if enabled and cleared:
            plural = "s" if cleared != 1 else ""
            cleared_note = (
                f"\n> Cleared **{cleared}** per-channel override{plural}."
            )
        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    f"# {'✅' if enabled else '🚫'} `{name}`\n"
                    f"> This command is now **{verb}** in this server.{cleared_note}"
                ),
                won=enabled,
            )
        )

    @admin.command(name="disable")
    async def disable(self, ctx: commands.Context, *, command: str = "") -> None:
        """Disable a command in this server.

        Usage: `{PREFIX}admin disable <command>`.
        """
        await self._set_enabled(ctx, command, False)

    @admin.command(name="enable")
    async def enable(self, ctx: commands.Context, *, command: str = "") -> None:
        """Enable a previously disabled command in this server.

        Usage: `{PREFIX}admin enable <command>`.
        """
        await self._set_enabled(ctx, command, True)

    # ------------------------------------------------------------------
    # Extra bot admins
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_id(raw: str) -> int | None:
        """Pull a Discord ID out of a mention or a bare ID."""
        token = (raw or "").strip()
        match = re.fullmatch(r"<@!?(\d+)>", token)
        if match:
            return int(match.group(1))
        if token.isdigit() and 15 <= len(token) <= 21:
            return int(token)
        return None

    def _admin_ids(self) -> set[int]:
        return set(getattr(self.bot, "bot_admin_ids", None) or set())

    @admin.command(name="allow", aliases=("addadmin", "adminallow"))
    async def allow_admin(self, ctx: commands.Context, *, user: str = "") -> None:
        """Give a user ID the owner/admin commands.

        Usage: `{PREFIX}admin allow <user id>` (a mention also works).
        """
        user_id = self._parse_id(user)
        if user_id is None:
            await ctx.send(
                view=notice(
                    f"Usage: `{PREFIX}admin allow <user id>` — paste their ID "
                    "or mention them."
                )
            )
            return
        try:
            added = await self.bot.add_bot_admin(user_id)
        except RuntimeError:
            await ctx.send(
                view=notice("The settings store isn't ready yet — try again.")
            )
            return
        if not added:
            await ctx.send(
                view=notice(f"<@{user_id}> is already a bot admin.", emoji="ℹ️")
            )
            return
        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    f"# ✅ Bot admin added\n"
                    f"> <@{user_id}> can now run the owner commands "
                    f"(`{PREFIX}admin`, `{PREFIX}bank`, …).\n"
                    f"-# Remove them again with `{PREFIX}admin deny {user_id}`."
                ),
                won=True,
            )
        )

    @admin.command(name="deny", aliases=("removeadmin", "admindeny"))
    async def deny_admin(self, ctx: commands.Context, *, user: str = "") -> None:
        """Take away a user ID's added owner/admin access.

        Usage: `{PREFIX}admin deny <user id>` (a mention also works).
        """
        user_id = self._parse_id(user)
        if user_id is None:
            await ctx.send(
                view=notice(
                    f"Usage: `{PREFIX}admin deny <user id>` — paste their ID "
                    "or mention them."
                )
            )
            return
        try:
            removed = await self.bot.remove_bot_admin(user_id)
        except RuntimeError:
            await ctx.send(
                view=notice("The settings store isn't ready yet — try again.")
            )
            return
        if not removed:
            await ctx.send(
                view=notice(
                    f"<@{user_id}> isn't on the added bot admin list.", emoji="ℹ️"
                )
            )
            return
        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    f"# 🚫 Bot admin removed\n"
                    f"> <@{user_id}> no longer has the owner commands.\n"
                    "-# Discord team owners always keep their access."
                ),
                won=True,
            )
        )

    @admin.command(name="admins", aliases=("adminlist", "botadmins"))
    async def list_admins(self, ctx: commands.Context) -> None:
        """List the user IDs added on top of the Discord team owners."""
        added = sorted(self._admin_ids())
        if added:
            body = "\n".join(f"> <@{user_id}> · `{user_id}`" for user_id in added)
        else:
            body = "> No extra admins added yet."
        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    f"# 🛡️ Bot admins\n"
                    f"{body}\n\n"
                    f"-# Add one with `{PREFIX}admin allow <id>`, remove with "
                    f"`{PREFIX}admin deny <id>`. Discord team owners are always "
                    "admins."
                )
            )
        )

    # ------------------------------------------------------------------
    # The developer-channel command log
    # ------------------------------------------------------------------
    @admin.group(name="logs", aliases=("commandlog",), invoke_without_command=True)
    async def logs(self, ctx: commands.Context) -> None:
        """Show which servers are kept out of the developer-channel command log.

        Usage: `{PREFIX}admin logs exclude <server id>` to stop logging a
        server, `{PREFIX}admin logs include <server id>` to log it again. Every
        server is logged unless it is excluded.
        """
        await self._show_command_log(ctx)

    async def _show_command_log(self, ctx: commands.Context) -> None:
        """The excluded-server list, plus how to change it."""
        excluded = sorted(getattr(self.bot, "command_log_excluded_guilds", ()) or ())
        if excluded:
            lines = []
            for guild_id in excluded:
                guild = self.bot.get_guild(guild_id)
                label = f"**{guild.name}** · " if guild else ""
                lines.append(f"> {label}`{guild_id}`")
            body = "\n".join(lines)
        else:
            body = "> Every server is logged."
        if self.bot.dev_log_channel() is None:
            warning = (
                f"\n\n-# ⚠️ `{PREFIX}` can't see the developer channel right now "
                "(`DEVELOPER_CHANNEL_ID`), so nothing is being posted."
            )
        else:
            warning = ""
        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    f"# 📋 Command log\n"
                    f"-# every command, who ran it, when and where\n\n"
                    f"**Excluded servers**\n"
                    f"{body}\n\n"
                    f"-# Stop logging a server with "
                    f"`{PREFIX}admin logs exclude <server id>`, start again with "
                    f"`{PREFIX}admin logs include <server id>`."
                    f"{warning}"
                )
            )
        )

    @logs.command(name="list", aliases=("servers", "status"))
    async def logs_list(self, ctx: commands.Context) -> None:
        """List the servers whose commands are kept out of the log."""
        await self._show_command_log(ctx)

    @logs.command(name="exclude", aliases=("mute", "off"))
    async def logs_exclude(self, ctx: commands.Context, *, server: str = "") -> None:
        """Stop logging the commands run in one server.

        Usage: `{PREFIX}admin logs exclude <server id>`.
        """
        guild_id = self._parse_id(server)
        if guild_id is None:
            await ctx.send(
                view=notice(
                    f"Usage: `{PREFIX}admin logs exclude <server id>` — paste "
                    "the server's ID (right-click the server › Copy Server ID)."
                )
            )
            return
        try:
            changed = await self.bot.set_command_log_excluded(guild_id, True)
        except RuntimeError:
            await ctx.send(
                view=notice("The settings store isn't ready yet — try again.")
            )
            return
        guild = self.bot.get_guild(guild_id)
        label = f"**{guild.name}**" if guild else f"`{guild_id}`"
        if not changed:
            await ctx.send(
                view=notice(
                    f"{label} is already excluded from the command log.", emoji="ℹ️"
                )
            )
            return
        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    f"# 🙈 Server excluded\n"
                    f"> Commands run in {label} (`{guild_id}`) no longer appear "
                    "in the developer channel.\n"
                    f"-# Put it back with `{PREFIX}admin logs include {guild_id}`."
                ),
                won=True,
            )
        )

    @logs.command(name="include", aliases=("unmute", "on"))
    async def logs_include(self, ctx: commands.Context, *, server: str = "") -> None:
        """Start logging a server's commands again.

        Usage: `{PREFIX}admin logs include <server id>`.
        """
        guild_id = self._parse_id(server)
        if guild_id is None:
            await ctx.send(
                view=notice(
                    f"Usage: `{PREFIX}admin logs include <server id>` — paste "
                    "the server's ID."
                )
            )
            return
        try:
            changed = await self.bot.set_command_log_excluded(guild_id, False)
        except RuntimeError:
            await ctx.send(
                view=notice("The settings store isn't ready yet — try again.")
            )
            return
        guild = self.bot.get_guild(guild_id)
        label = f"**{guild.name}**" if guild else f"`{guild_id}`"
        if not changed:
            await ctx.send(
                view=notice(
                    f"{label} was already being logged.", emoji="ℹ️"
                )
            )
            return
        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    f"# 👀 Server included\n"
                    f"> Commands run in {label} (`{guild_id}`) are logged to the "
                    "developer channel again.\n"
                    f"-# Turn it off with `{PREFIX}admin logs exclude {guild_id}`."
                ),
                won=True,
            )
        )

    # ------------------------------------------------------------------
    # Per-server bot profile
    # ------------------------------------------------------------------
    async def _read_image(self, ctx: commands.Context, image: str) -> bytes | None:
        """Read image bytes from an attachment, the replied-to message, or a URL."""
        attachments = list(ctx.message.attachments)
        reference = getattr(ctx.message, "reference", None)
        if not attachments and reference is not None:
            resolved = getattr(reference, "resolved", None)
            attachments = list(getattr(resolved, "attachments", []) or [])
        for attachment in attachments:
            if (attachment.content_type or "").startswith("image/"):
                try:
                    return await attachment.read()
                except discord.HTTPException:
                    return None

        url = (image or "").strip()
        if url.startswith(("http://", "https://")):
            try:
                timeout = aiohttp.ClientTimeout(total=15)
                async with borrowed() as session:
                    async with session.get(url, timeout=timeout) as response:
                        if response.status != 200:
                            return None
                        return await response.read()
            except aiohttp.ClientError:
                return None
        return None

    @admin.command(name="serverpfp", aliases=("serverbanner",))
    async def serverpfp(self, ctx: commands.Context, *, image: str = "") -> None:
        """Set the bot's profile picture for this server only.

        Usage: `{PREFIX}admin serverpfp` with an image attached, or
        `{PREFIX}admin serverpfp <image url>`.
        """
        if ctx.guild is None:
            await ctx.send(view=notice("That's a per-server setting."))
            return
        data = await self._read_image(ctx, image)
        if data is None:
            await ctx.send(
                view=notice(
                    "Attach an image or give me an image URL — "
                    f"`{PREFIX}admin serverpfp https://…`."
                )
            )
            return
        try:
            await ctx.guild.me.edit(
                avatar=data, reason=f"Per-server pfp set by {ctx.author}"
            )
        except discord.Forbidden:
            await ctx.send(
                view=notice("I don't have permission to change my server avatar.")
            )
            return
        except discord.HTTPException as error:
            await ctx.send(view=notice(f"Discord refused that image: `{error}`"))
            return
        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    "# 🖼️ Server profile picture set\n"
                    f"> My avatar is now custom to **{ctx.guild.name}**."
                ),
                won=True,
            )
        )

    @admin.command(name="servername", aliases=("servernick",))
    async def servername(self, ctx: commands.Context, *, name: str = "") -> None:
        """Set the bot's nickname (its per-server name) here.

        Usage: `{PREFIX}admin servername <name>`.
        """
        if ctx.guild is None:
            await ctx.send(view=notice("That's a per-server setting."))
            return
        cleaned = (name or "").strip()[:32]
        if not cleaned:
            await ctx.send(
                view=notice(f"Usage: `{PREFIX}admin servername <name>`.")
            )
            return
        try:
            await ctx.guild.me.edit(
                nick=cleaned, reason=f"Per-server name set by {ctx.author}"
            )
        except discord.Forbidden:
            await ctx.send(
                view=notice("I don't have permission to change my nickname here.")
            )
            return
        except discord.HTTPException as error:
            await ctx.send(view=notice(f"Discord refused that name: `{error}`"))
            return
        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    "# 🏷️ Server name set\n"
                    f"> I'm now known as **{cleaned}** in **{ctx.guild.name}**."
                ),
                won=True,
            )
        )

    # ------------------------------------------------------------------
    # Custom bot status
    # ------------------------------------------------------------------
    #: The status loop rotates a handful of strings every fifteen minutes.
    #: A pinned status simply cancels it, so the custom text holds until the
    #: process restarts and the loop starts again on its own. Nothing is
    #: written to the database on purpose - "until a restart" is the contract.
    def _status_task(self):
        """The bot's rotating-status loop, if it has one."""
        return getattr(self.bot, "status_task", None)

    def _pinned_status(self) -> str | None:
        """The status currently pinned by an owner, if any."""
        return getattr(self.bot, "custom_status", None)

    async def _pin_status(self, text: str) -> None:
        """Show *text* and keep the rotation from overwriting it."""
        task = self._status_task()
        if task is not None:
            task.cancel()
        self.bot.custom_status = text
        await self.bot.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name=text)
        )

    async def _unpin_status(self) -> bool:
        """Let the rotation take the presence back. False if it cannot."""
        task = self._status_task()
        if task is None:
            return False
        self.bot.custom_status = None
        if getattr(self.bot, "debug_mode_active", False):
            # Debug mode keeps the bot on its own DND status, so leave it be.
            return False
        if task.is_running():
            # An earlier cancel may still be unwinding; restart() waits its turn.
            task.restart()
        else:
            try:
                task.start()
            except RuntimeError:
                task.restart()
        return True

    @admin.command(name="status", aliases=("botstatus", "setstatus"))
    async def status(self, ctx: commands.Context, *, text: str = "") -> None:
        """Pin a custom status on the bot until the next restart.

        Usage: `{PREFIX}admin status <text>`.
        """
        cleaned = " ".join((text or "").split())
        if not cleaned:
            current = self._pinned_status()
            hint = (
                f"It is currently showing `{current}`."
                if current
                else f"It is currently rotating on its own."
            )
            await ctx.send(
                view=notice(
                    f"Usage: `{PREFIX}admin status <text>`. {hint}", emoji="ℹ️"
                )
            )
            return
        if len(cleaned) > 128:
            await ctx.send(
                view=notice(
                    "Discord caps a status at 128 characters; "
                    f"yours is {len(cleaned)}."
                )
            )
            return

        await self._pin_status(cleaned)
        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    "# 📌 Status pinned\n"
                    f"> The bot is now watching **{cleaned}**.\n"
                    "-# This holds until the bot restarts — "
                    f"`{PREFIX}admin resetstatus` hands the rotation back sooner."
                ),
                won=True,
            )
        )

    @admin.command(name="resetstatus", aliases=("statusreset", "unpinstatus"))
    async def resetstatus(self, ctx: commands.Context) -> None:
        """Drop the pinned status and go back to the rotating one.

        Usage: `{PREFIX}admin resetstatus`.
        """
        if self._pinned_status() is None and self._status_task() is None:
            await ctx.send(
                view=notice("This bot has no status rotation to go back to.")
            )
            return

        resumed = await self._unpin_status()
        if not resumed:
            await ctx.send(
                view=notice(
                    "Debug mode is holding the presence right now — "
                    "turn it off and the rotation starts on its own.",
                    emoji="ℹ️",
                )
            )
            return

        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    "# 🔄 Rotation restored\n"
                    "> The rotating status has the presence back."
                ),
                won=True,
            )
        )

    @admin.command(name="popups", aliases=("levelups", "announce"))
    async def popups(self, ctx: commands.Context, *, args: str = "") -> None:
        """Turn level-up messages on or off, for everyone or one member.

        Usage:
        `{PREFIX}admin popups` — show the current setting
        `{PREFIX}admin popups off` — stop the cards bot-wide
        `{PREFIX}admin popups @member off` — mute them for one member
        """
        tokens = (args or "").split()
        states = [token for token in tokens if userprefs.parse_flag(token) is not None]
        names = [token for token in tokens if userprefs.parse_flag(token) is None]

        if not tokens:
            await ctx.send(view=await self._popup_card())
            return

        # A bare on/off is the bot-wide switch. It lives in the global row on
        # purpose: "stop the level-up spam" is not a per-server thought.
        if not names:
            if len(states) != 1:
                await ctx.send(view=notice("Give me one of `on` or `off`."))
                return
            ok, message = await self._set_popups(userprefs.parse_flag(states[0]))
            await ctx.send(
                view=panel(
                    discord.ui.TextDisplay(f"# {'✅' if ok else '❌'} {message}"),
                    won=ok,
                )
            )
            return

        member = await self._target(ctx, " ".join(names))
        if member is None:
            return

        if not states:
            await ctx.send(view=await self._popup_card(member))
            return

        enabled = bool(userprefs.parse_flag(states[-1]))
        await preferences.write(self.bot.db, member.id, preferences.POPUPS, enabled)
        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    f"# {'🔔' if enabled else '🔕'} {member.mention}\n"
                    f"> Level-up messages **{'on' if enabled else 'off'}**"
                ),
                won=True,
            )
        )

    @admin.command(name="rolelevel", aliases=("levelrole", "levelrolerole"))
    async def rolelevel(self, ctx: commands.Context, *args: str) -> None:
        """Build the level -> role ladder.

        Usage:
        `{PREFIX}admin rolelevel 5 @Role` — add a role to level 5
        `{PREFIX}admin rolelevel 5 @Role remove` — take that one role back off
        `{PREFIX}admin rolelevel 5 clear` — clear every role at level 5
        `{PREFIX}admin rolelevel clear all` — wipe the whole ladder
        `{PREFIX}admin rolelevel 5 move to 10` — relocate a level
        `{PREFIX}admin rolelevel 5 copy to 10` — duplicate it instead

        A level can hand out as many roles as you like; the `mode` setting on
        `{PREFIX}admin rolesettings` decides whether tiers stack or replace.
        """
        if ctx.guild is None:
            await ctx.send(view=notice("Level roles are per-server."))
            return
        if not args:
            await self._show_ladder(ctx, headline="🪜 Level roles")
            return

        lowered = [token.lower() for token in args]

        # -- clear 5 / clear all --------------------------------------------
        if lowered[0] == "clear":
            if len(lowered) < 2:
                await ctx.send(
                    view=notice(
                        f"Usage: `{PREFIX}admin rolelevel clear <level>` "
                        f"or `{PREFIX}admin rolelevel clear all`."
                    )
                )
                return
            if lowered[1] in {"all", "*", "everything"}:
                await levelroles.clear_all(self.bot.db, ctx.guild.id)
                await self._show_ladder(ctx, headline="🧹 Ladder wiped")
                return
            level = self._as_level(lowered[1])
            if level is None:
                await ctx.send(view=notice(f"`{args[1]}` isn't a level."))
                return
            await levelroles.clear_level(self.bot.db, ctx.guild.id, level)
            await self._show_ladder(
                ctx,
                headline=f"🧹 Level {level} cleared",
                note=f"Level {level} no longer hands anything out. Anyone already "
                f"wearing it keeps it — `{PREFIX}admin syncroles @member` takes "
                "it back off.",
            )
            return

        # -- 5 move to 10 / 5 copy to 10 ------------------------------------
        verb = next(
            (index for index, word in enumerate(lowered) if word in {"move", "copy"}),
            None,
        )
        if verb is not None:
            numbers = [
                value
                for value in (self._as_level(token) for token in lowered)
                if value is not None
            ]
            if len(numbers) < 2:
                await ctx.send(
                    view=notice(
                        f"Usage: `{PREFIX}admin rolelevel <from> move to <to>` "
                        f"(or `copy to`)."
                    )
                )
                return
            source, target = numbers[0], numbers[1]
            action = lowered[verb]
            if not (await self.ladder(ctx.guild.id)).roles_at(source):
                await ctx.send(
                    view=notice(f"Level {source} has no roles to {action}.")
                )
                return
            if action == "move":
                await levelroles.move_level(self.bot.db, ctx.guild.id, source, target)
            else:
                await levelroles.copy_level(self.bot.db, ctx.guild.id, source, target)
            await self._show_ladder(
                ctx,
                headline=(
                    f"{'➡️' if action == 'move' else '📑'} Level {source} "
                    f"{'moved to' if action == 'move' else 'copied to'} "
                    f"level {target}"
                ),
            )
            return

        # -- 5 @Role / 5 @Role remove ---------------------------------------
        level = self._as_level(lowered[0])
        if level is None:
            await ctx.send(
                view=notice(f"`{args[0]}` isn't a level — levels start at 1.")
            )
            return

        if len(args) < 2:
            ladder = await self.ladder(ctx.guild.id)
            roles = [
                role
                for role in (ctx.guild.get_role(rid) for rid in ladder.roles_at(level))
                if role is not None
            ]
            if roles:
                await ctx.send(
                    view=notice(
                        f"Level {level} already hands out "
                        f"{', '.join(role.mention for role in roles)}. Pass a role "
                        f"to add another, or `{PREFIX}admin rolelevel {level} clear` "
                        "to reset the level.",
                        emoji="ℹ️",
                    )
                )
            else:
                await ctx.send(
                    view=notice(
                        f"Level {level} has nothing yet — "
                        f"`{PREFIX}admin rolelevel {level} @Role` adds one.",
                        emoji="ℹ️",
                    )
                )
            return

        try:
            role = await commands.RoleConverter().convert(ctx, args[1])
        except commands.BadArgument:
            role = None
        if role is None:
            await ctx.send(view=notice(f"I can't find a role called `{args[1]}`."))
            return

        problems: list[str] = []
        if role.is_default():
            problems.append("@everyone can't be a reward")
        elif not role.is_assignable():
            problems.append(
                f"{role.mention} sits at or above my top role, so I can't hand it out"
            )
        if problems:
            await ctx.send(
                view=notice(
                    "# Can't use that role\n"
                    + "\n".join(f"> {line}" for line in problems)
                )
            )
            return

        removing = len(args) > 2 and lowered[2] in {"remove", "delete", "off", "no"}
        if removing:
            await levelroles.remove_grant(self.bot.db, ctx.guild.id, level, role.id)
            headline = f"🧹 Level {level} no longer gives {role.mention}"
        else:
            await levelroles.add_grant(self.bot.db, ctx.guild.id, level, role.id)
            headline = f"🎖️ Level {level} → {role.mention}"
        await self._show_ladder(
            ctx,
            headline=headline,
            note=f"Reach level {level} and the role is handed over automatically.",
        )

    @admin.command(name="rolelevels", aliases=("ladder",))
    async def rolelevels(self, ctx: commands.Context) -> None:
        """Show the ladder, the settings and whether each role can be given."""
        if ctx.guild is None:
            await ctx.send(view=notice("Level roles are per-server."))
            return
        await self._show_ladder(ctx, headline="🪜 Level roles")

    @admin.command(name="rolesettings", aliases=("roleconfig",))
    async def rolesettings(
        self, ctx: commands.Context, setting: str = "", value: str = ""
    ) -> None:
        """Tune how the ladder is applied.

        Usage:
        `{PREFIX}admin rolesettings mode replace|stack`
        `{PREFIX}admin rolesettings strip on|off` — take back superseded roles
        `{PREFIX}admin rolesettings announce on|off` — name roles on the card
        `{PREFIX}admin rolesettings dm on|off` — DM the member too

        Run it with no arguments to see the current switches.
        """
        if ctx.guild is None:
            await ctx.send(view=notice("Level roles are per-server."))
            return

        key = setting.strip().lower()
        if not key:
            await self._show_ladder(ctx, headline="⚙️ Level role settings")
            return

        if key == "mode":
            wanted = value.strip().lower()
            if wanted not in levelroles.MODES:
                await ctx.send(
                    view=notice(
                        "Mode has to be one of "
                        + ", ".join(f"`{mode}`" for mode in levelroles.MODES)
                        + "."
                    )
                )
                return
            await levelroles.set_mode(self.bot.db, ctx.guild.id, wanted)
            await self._show_ladder(ctx, headline=f"⚙️ Mode set to {wanted}")
            return

        names = {
            "strip": "strip_stale",
            "stale": "strip_stale",
            "strip_stale": "strip_stale",
            "announce": "announce",
            "dm": "dm",
        }
        name = names.get(key)
        if name is None:
            await ctx.send(
                view=notice("Unknown setting. Try `mode`, `strip`, `announce` or `dm`.")
            )
            return

        flag = userprefs.parse_flag(value)
        if flag is None:
            await ctx.send(view=notice("Give me `on` or `off`."))
            return

        await levelroles.set_flag(self.bot.db, ctx.guild.id, name, flag)
        await self._show_ladder(
            ctx, headline=f"⚙️ {name.replace('_', ' ').title()} → {'on' if flag else 'off'}"
        )

    @admin.command(name="rolecheck", aliases=("rolespreview", "roledryrun"))
    async def rolecheck(self, ctx: commands.Context, member: str) -> None:
        """Preview exactly what the ladder would change for someone."""
        if ctx.guild is None:
            await ctx.send(view=notice("Level roles are per-server."))
            return
        target = await self._target(ctx, member)
        if target is None:
            return
        ladder = await self.ladder(ctx.guild.id)
        level = await self._level_of(target.id, guild_id=ctx.guild.id)
        outcome = levelroles.plan(target, level, ladder)
        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    f"# 🔍 Dry run\n"
                    f"> **Member** {target.mention}\n"
                    f"> **Level** {level}\n"
                    f"> **Would give** "
                    f"{', '.join(role.mention for role in outcome.granted) or '—'}\n"
                    f"> **Would take back** "
                    f"{', '.join(role.mention for role in outcome.removed) or '—'}\n"
                    f"> **Can't touch** "
                    f"{', '.join(role.mention for role in outcome.blocked) or '—'}"
                    + (f"\n\n-# {outcome.reason}" if outcome.reason else "")
                ),
                won=True,
            )
        )

    @admin.command(name="syncroles", aliases=("levelrolesrefresh", "rolesync"))
    async def syncroles(self, ctx: commands.Context, *, target: str = "me") -> None:
        """Re-apply the ladder to someone already past a level.

        Usage: `{PREFIX}admin syncroles @member` · `{PREFIX}admin syncroles all`.
        """
        if ctx.guild is None:
            await ctx.send(view=notice("Level roles are per-server."))
            return
        cog = self._levels_cog()
        if cog is None:
            await ctx.send(view=notice("The levels cog isn't loaded."))
            return

        wanted = target.strip().lower()
        if wanted in {"all", "*", "everyone"}:
            await self._sync_everyone(ctx, cog)
            return

        if wanted in {"me", "self"}:
            member: discord.Member | None = ctx.author  # type: ignore[assignment]
        else:
            member = await self._target(ctx, target)
            if member is None:
                return

        level = await self._level_of(member.id, guild_id=ctx.guild.id)
        try:
            outcome = await cog.sync_level_roles(member, level)
        except Exception as error:
            logger.exception("Role resync failed for %s", member.id)
            await ctx.send(view=notice(f"I couldn't do that: `{type(error).__name__}`"))
            return

        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    f"# 🎖️ Roles resynced\n"
                    f"> **Member** {member.mention}\n"
                    f"> **Level** {level}\n\n"
                    f"{outcome.summary()}"
                    + (f"\n-# {outcome.reason}" if outcome.reason else "")
                ),
                won=outcome.changed or outcome.reason is None,
            )
        )

    async def _sync_everyone(self, ctx: commands.Context, cog) -> None:
        """Walk the levelled members, paced so Discord's role bucket is happy."""
        if ctx.guild is None:
            return
        ladder = await self.ladder(ctx.guild.id)
        if ladder.empty:
            await ctx.send(view=notice("No level roles are configured yet."))
            return

        rows = await self.bot.db.execute(
            "SELECT user_id, level FROM user_levels WHERE guild_id = %s "
            "ORDER BY level DESC LIMIT %s",
            ctx.guild.id,
            levelroles.BULK_LIMIT,
        )
        note = await ctx.send(
            view=notice(
                f"Working through **{len(rows)}** levelled member(s)…", emoji="⏳"
            )
        )

        checked = changed = removed = 0
        for user_id, level in rows:
            member = ctx.guild.get_member(int(user_id))
            if member is None or member.bot:
                continue
            checked += 1
            try:
                outcome = await cog.sync_level_roles(member, int(level))
            except Exception:
                logger.exception("Bulk role sync failed for %s", user_id)
                continue
            if outcome.changed:
                changed += 1
                removed += len(outcome.removed)
            # Roughly one member edit per second; the role bucket is the
            # tightest limit here and bursting it just gets us 429'd.
            await asyncio.sleep(1.0)

        capped = (
            f"\n-# Stopped at {levelroles.BULK_LIMIT} members — run it again "
            "for the rest"
            if len(rows) >= levelroles.BULK_LIMIT
            else ""
        )
        try:
            await note.edit(
                view=panel(
                    discord.ui.TextDisplay(
                        f"# 🎖️ Everybody resynced\n"
                        f"> **Checked** {checked} member(s)\n"
                        f"> **Updated** {changed} member(s)\n"
                        f"> **Roles taken back** {removed}"
                        f"{capped}"
                    ),
                    won=True,
                )
            )
        except discord.HTTPException:
            pass


# ---------------------------------------------------------------------------
# The values-editor panel
# ---------------------------------------------------------------------------


class FieldSelect(discord.ui.Select):
    def __init__(self, view: "AdminPanel") -> None:
        self.owner = view
        super().__init__(
            placeholder="Field to edit…",
            options=[
                discord.SelectOption(
                    label=label,
                    value=key,
                    emoji=emoji,
                    description=(
                        "needs a value"
                        if needs_value
                        else "runs straight away when applied"
                    ),
                    default=key == view.field,
                )
                for key, (label, emoji, needs_value) in FIELDS.items()
            ],
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.owner.field = self.values[0]
        await self.owner.refresh(interaction)


class ModeSelect(discord.ui.Select):
    def __init__(self, view: "AdminPanel") -> None:
        self.owner = view
        super().__init__(
            placeholder="Mode…",
            options=[
                discord.SelectOption(
                    label=label,
                    value=key,
                    emoji=emoji,
                    default=key == view.mode,
                )
                for key, label, emoji in (
                    ("set", "Set to", "🎯"),
                    ("add", "Add", "➕"),
                    ("remove", "Remove", "➖"),
                )
            ],
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.owner.mode = self.values[0]
        await self.owner.refresh(interaction)


class _ApplyButton(discord.ui.Button):
    def __init__(self, view: "AdminPanel", *, target: bool = False) -> None:
        self.owner = view
        self.is_target = target
        super().__init__(
            style=(
                discord.ButtonStyle.secondary
                if target
                else discord.ButtonStyle.primary
            ),
            label="Change target" if target else "Apply",
            emoji="👤" if target else "✅",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.is_target:
            await self.owner.prompt_target(interaction)
        else:
            await self.owner.apply(interaction)


class ValueModal(discord.ui.Modal):
    """Collects the one value an edit needs."""

    def __init__(self, view: "AdminPanel", field: str, mode: str) -> None:
        label = FIELDS[field][0]
        super().__init__(title=f"{mode.title()} {label}"[:45])
        self.panel = view
        self.field = field
        self.mode = mode
        self.value = discord.ui.TextInput(
            label=label[:45],
            placeholder="e.g. 5000, all, half · for items: Padlock 2",
            required=True,
            max_length=64,
        )
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.panel.submit(
            interaction, self.field, self.mode, str(self.value.value)
        )


class TargetModal(discord.ui.Modal):
    """Lets the owner retarget the panel by name, mention or ID."""

    def __init__(self, view: "AdminPanel") -> None:
        super().__init__(title="Change target")
        self.panel = view
        self.value = discord.ui.TextInput(
            label="Member name, ID or mention",
            placeholder="e.g. jhonny, @someone or 1092314528634646679",
            required=True,
            max_length=64,
        )
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.panel.retarget(interaction, str(self.value.value))


class AdminPanel(discord.ui.LayoutView):
    """The clickable half of the values editor."""

    def __init__(self, cog: Admin, *, member: discord.Member, invoker: int) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.member = member
        self.invoker = invoker
        self.field = "wallet"
        self.mode = "set"
        self.message: discord.Message | None = None
        self.note = "Pick a field and a mode, then hit Apply."
        self._state: dict[str, object] | None = None
        self.rebuild()

    def _mine(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.invoker

    def rebuild(self) -> None:
        state = self._state or {}
        box = discord.ui.Container(accent_color=EMBED_COLOR)
        box.add_item(
            discord.ui.Section(
                discord.ui.TextDisplay(
                    f"# 🔧 Admin menu\n"
                    f"### {self.member.display_name}\n"
                    f"-# {self.member.id}"
                ),
                accessory=discord.ui.Thumbnail(media=self.member.display_avatar.url),
            )
        )
        box.add_item(divider())
        box.add_item(
            discord.ui.TextDisplay(
                self.cog.state_block(self.member, state) if state else "-# loading…"
            )
        )
        box.add_item(divider())
        box.add_item(
            discord.ui.TextDisplay(
                f"**Editing** {FIELDS[self.field][1]} {FIELDS[self.field][0]} · "
                f"mode **{self.mode}**\n"
                f"-# {self.note}\n"
                f"-# wallet/bank stay in sync with the rank ledger"
            )
        )
        box.add_item(discord.ui.ActionRow(FieldSelect(self)))
        box.add_item(discord.ui.ActionRow(ModeSelect(self)))
        box.add_item(
            discord.ui.ActionRow(_ApplyButton(self), _ApplyButton(self, target=True))
        )
        self.clear_items()
        self.add_item(box)

    async def load(self) -> None:
        self._state = await self.cog.snapshot(self.member)
        self.rebuild()

    async def refresh(self, interaction: discord.Interaction) -> None:
        if not self._mine(interaction):
            await interaction.response.send_message("Not your menu.", ephemeral=True)
            return
        self._state = await self.cog.snapshot(self.member)
        self.rebuild()
        if not interaction.response.is_done():
            await interaction.response.defer()
        await swap_layout_message(interaction, self)

    async def apply(self, interaction: discord.Interaction) -> None:
        if not self._mine(interaction):
            await interaction.response.send_message("Not your menu.", ephemeral=True)
            return

        field = self.field
        if FIELDS[field][2]:
            await interaction.response.send_modal(ValueModal(self, field, self.mode))
            return

        ok, message = await self.cog.run_action(self.member, field)
        self.note = message
        self._state = await self.cog.snapshot(self.member)
        self.rebuild()
        if not interaction.response.is_done():
            await interaction.response.defer()
        await swap_layout_message(interaction, self)
        await interaction.followup.send(
            f"{'✅' if ok else '❌'} {message}", ephemeral=True
        )

    async def _repaint(self, interaction: discord.Interaction, message: str) -> None:
        """Refresh the panel after a modal submit.

        A modal submit has its own interaction token, so ``@original`` no longer
        points at the panel message; the stored message is edited directly.
        """
        if not interaction.response.is_done():
            await interaction.response.defer()
        self._state = await self.cog.snapshot(self.member)
        self.note = message
        self.rebuild()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                logger.exception("Could not refresh the admin panel")
        await interaction.followup.send(message, ephemeral=True)

    async def submit(
        self, interaction: discord.Interaction, field: str, mode: str, raw: str
    ) -> None:
        ok, message = await self.cog.apply_value(self.member, field, mode, raw)
        await self._repaint(interaction, f"{'✅' if ok else '❌'} {message}")

    async def prompt_target(self, interaction: discord.Interaction) -> None:
        if not self._mine(interaction):
            await interaction.response.send_message("Not your menu.", ephemeral=True)
            return
        await interaction.response.send_modal(TargetModal(self))

    async def retarget(self, interaction: discord.Interaction, raw: str) -> None:
        """Point the panel at whoever ``raw`` most looks like."""
        query = raw.strip()
        guild = interaction.guild or (self.message.guild if self.message else None)
        member = await find_member(guild, query)
        if member is None:
            await interaction.response.send_message(
                f"I couldn't match `{query}` to anyone in this server — "
                "mention them or paste their ID.",
                ephemeral=True,
            )
            return
        self.member = member
        await self._repaint(interaction, f"👤 now editing {member.display_name}")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(bot))
