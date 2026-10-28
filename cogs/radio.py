"""Mixtape Radio: a voice station that shuffles everything in ``Mixtapes/``.

    ,slimefm                    shuffle the folder into your voice channel
    ,slimefm start [song]       start on one track instead of a random one
    ,slimefm request <song>     queue a track to play next
    ,slimefm songs [query]      post a request dropdown (up to 25 picks)
    ,slimefm skip / previous    move through the queue
    ,slimefm pause / resume     hold the music, then let it go
    ,slimefm loop               repeat whatever is playing
    ,slimefm now                repost the now-playing card
    ,slimefm stop               clear the queue and leave the channel
    ,setradio [#channel]        post the now-playing card in a text channel
    ,setradiovc [#channel]      park the station in a voice channel 24/7

The command is ``,slimefm`` so the song station keeps ``,radio`` to itself; the
mixtape station and the song station are otherwise unrelated.

Everything is read through the same :class:`MixtapeLibrary` the ``,song``
and ``,project`` commands use, so a project dropped into the folder shows up
on the station as soon as the index is reloaded.

**Metadata comes from the file first.** Title, artist, album, length and the
cover are read out of the mp3's own tags with ffprobe, and that embedded picture
is what the card shows. Only when a track carries no artwork does the card fall
back to its project's ``ART`` folder.

One advance routine, guarded by a lock so two triggers can't drop a queue entry,
an ``after`` callback ffmpeg fires when a track ends, and a history stack behind
"previous" paired with a forward stack so skipping after stepping back returns
to the song you left — that is the whole transport.

**Requests.** ``,slimefm songs`` posts one mixtape at a time, with Previous and
Next stepping through the projects so the dropdown only ever holds the selected
project's tracks; paging is open to everybody, because the card is posted in a
text channel where most people are only browsing.

``,setradio #channel`` turns a text channel into the station's inbox and posts
the now-playing card there, so nothing has to be commanded in that channel. Any
song name typed there is deleted and turned into a request — a unique match
queues straight away, an ambiguous one gets a dropdown — and every answer,
including a "queued" confirmation, clears itself after a few seconds so the
channel holds nothing but the card. Which channel that is lives in the
dashboard settings (``radio_channel``), so it survives a restart.

``,setradiovc #channel`` parks the station in a voice channel for good: it
joins that channel now and again on every start, shuffles through the mixtapes
on its own, and is the one station allowed to sit in a channel nobody is in.
Anyone sitting there can drive its card. That channel lives in the settings too
(``radio_vc``).

Any other station signs off by itself once the humans leave the channel, so it
never sits parked in an empty voice channel.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from discord.utils import DEFAULT_FILE_SIZE_LIMIT_BYTES

from config import EMBED_COLOR, EMBED_COLORS, PREFIX
from utils.core.mixtapes import Song, TrackInfo
from utils.core.views import SimpleLayout, swap_layout_message, usage_card

logger = logging.getLogger("slimey.radio")

MAX_OPTIONS = 25
#: How many tracks to try before giving up on picking something playable.
PLAY_ATTEMPTS = 5
#: The request menu offers this many tracks, and searches return this many.
MENU_SIZE = 25
#: How long a posted request dropdown waits to be answered.
SELECT_TIMEOUT = 120.0
#: How long any answer stays up before it is tidied away. This covers the
#: inbox's own replies and every "queued" confirmation, so the request channel
#: is left holding nothing but the station's own cards.
FLASH_SECONDS = 5.0

#: Radio-only cover choices. The library normally prefers artwork embedded in
#: each MP3, but these projects need one consistent image across the station.
RADIO_COVER_OVERRIDES: dict[str, str] = {
    "the ysleaks": "The YSLleaks (V2).png",
}

_UNSAFE = re.compile(r"[^A-Za-z0-9_.\- ()]+")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def divider() -> discord.ui.Separator:
    return discord.ui.Separator(spacing=discord.SeparatorSpacing.small)


def container(*items: discord.ui.Item, colour: discord.Color | None = None) -> discord.ui.Container:
    box = discord.ui.Container(accent_color=colour or EMBED_COLOR)
    for item in items:
        box.add_item(item)
    return box


def panel(*items: discord.ui.Item, colour: discord.Color | None = None) -> SimpleLayout:
    return SimpleLayout(container(*items, colour=colour))


def notice(message: str, *, emoji: str = "❌") -> SimpleLayout:
    """A card that only explains a command, so it doesn't spend a cooldown."""
    return usage_card(message, emoji=emoji)


def _safe_name(value: str, *, max_length: int = 48) -> str:
    cleaned = _UNSAFE.sub("_", value).strip(" ._")
    return (cleaned or "cover")[:max_length]


def _upload_limit(target: Any) -> int:
    guild = getattr(target, "guild", None)
    return getattr(guild, "filesize_limit", None) or DEFAULT_FILE_SIZE_LIMIT_BYTES


def _is_playing(voice: discord.VoiceClient | None) -> bool:
    return bool(voice and voice.is_playing())


def _is_paused(voice: discord.VoiceClient | None) -> bool:
    return bool(voice and voice.is_paused())


def _is_active(voice: discord.VoiceClient | None) -> bool:
    return _is_playing(voice) or _is_paused(voice)


# ---------------------------------------------------------------------------
# Player
# ---------------------------------------------------------------------------
class MixtapeRadio:
    """The station for one guild: a queue, a history stack and a voice client.

    State lives here rather than on the cog so two channels in one server can't
    fight over a single queue, and so ``;radio stop`` can throw everything away
    without touching the music playing in another guild.
    """

    #: How many tracks stay reachable with "previous".
    HISTORY_LIMIT = 25

    def __init__(self, cog: "Radio", guild_id: int) -> None:
        self.cog = cog
        self.guild_id = int(guild_id)
        self.voice: discord.VoiceClient | None = None
        self.channel_id: int | None = None
        self.invoker_id: int | None = None
        self.message: discord.Message | None = None
        self.current: Song | None = None
        self.info: TrackInfo | None = None
        self.cover: Path | None = None
        #: The standalone request picker posted alongside the now-playing card
        #: (``,setradio`` and ``,slimefm songs``), so the two can share one card
        #: instead of stacking new ones up.
        self.request_message: discord.Message | None = None
        self.queue: list[Song] = []
        self.history: list[Song] = []
        #: The other half of the history: songs "previous" stepped away from,
        #: waiting for a skip to walk back to them instead of rolling a new one.
        self.future: list[Song] = []
        self.loop = False
        self.paused = False
        #: Which mixtape the request picker is currently showing. Lives on the
        #: station rather than on a card so every copy of the picker agrees.
        self.request_index = 0
        #: A permanent station is the 24/7 one: it never leaves an empty
        #: channel, and anybody in the server may drive it.
        self.permanent = False
        self._advance_lock = asyncio.Lock()
        self._skipping = False

    # -- queue -----------------------------------------------------------
    @property
    def bot(self) -> commands.Bot:
        return self.cog.bot

    @property
    def library(self):
        return self.cog.library

    def enqueue(self, song: Song) -> int:
        """Add a request to the end of the queue and return the new length."""
        self.queue.append(song)
        return len(self.queue)

    def dequeue(self, song: Song) -> bool:
        try:
            self.queue.remove(song)
        except ValueError:
            return False
        return True

    def may_control(self, user: Any) -> bool:
        """Who gets to press the buttons.

        The member who started the station always can, and so can anyone
        sitting in the voice channel with it — which is how a shared channel
        should work. A permanent (24/7) station is different: it usually has
        nobody in the channel at all, so the server's own channel permissions
        are the gate and anybody may drive it.
        """
        user_id = getattr(user, "id", None)
        if user_id is not None and user_id == self.invoker_id:
            return True
        if self.permanent:
            return True
        channel = self.voice.channel if self.voice and self.voice.is_connected() else None
        if channel is None:
            return False
        state = getattr(user, "voice", None)
        return state is not None and state.channel == channel

    # -- connection ------------------------------------------------------
    async def connect(
        self,
        channel: discord.VoiceChannel,
        invoker_id: int | None,
        *,
        permanent: bool = False,
    ) -> bool:
        self.invoker_id = invoker_id
        self.permanent = permanent
        existing = self.voice
        if existing is not None and existing.is_connected():
            if existing.channel != channel:
                await existing.move_to(channel)
            return True
        try:
            self.voice = await channel.connect(self_deaf=True, timeout=20.0)
        except (asyncio.TimeoutError, discord.ClientException, discord.HTTPException, OSError):
            logger.exception("Could not join %s", channel.id)
            self.voice = None
            return False
        return True

    # -- playback --------------------------------------------------------
    async def _next_song(self) -> Song | None:
        """Queued requests first, then a random track from the folder."""
        while self.queue:
            return self.queue.pop(0)
        return self.library.random_song()

    async def advance(self) -> None:
        """Pick and start the next track.

        Serialized with a lock: the ffmpeg "after" callback and a skip button
        can fire close together, and without this both would pop the queue and
        one of the two tracks would vanish.
        """
        async with self._advance_lock:
            if not self.voice or not self.voice.is_connected():
                return
            if self.voice.is_playing() or self.voice.is_paused():
                return

            # An explicit skip replays the song that "previous" stepped away
            # from, the way a browser's forward button does. A track that ran
            # out on its own just moves on.
            skipped = self._skipping
            self._skipping = False

            if self.loop and self.current is not None and not skipped:
                await self.play(self.current)
                return

            if skipped and self.future:
                song = self.future.pop()
                if await self.play(song, reset_future=False):
                    return

            for _ in range(PLAY_ATTEMPTS):
                song = await self._next_song()
                if song is None:
                    await self.refresh()
                    return
                if await self.play(song):
                    return
            await self.refresh()

    async def _cover_for(self, song: Song) -> Path | None:
        """Choose radio artwork, honoring project-specific fixed covers."""
        project = self.library.get_project(song.project)
        wanted_name = (
            RADIO_COVER_OVERRIDES.get(project.name.casefold())
            if project is not None
            else None
        )
        if project is not None and wanted_name is not None:
            for path in project.gallery:
                if path.name.casefold() == wanted_name.casefold():
                    cover = await self.library.uploadable_artwork(
                        path, _upload_limit(self)
                    )
                    if cover is not None:
                        return cover
            logger.warning(
                "Radio cover override is not indexed for project %s", project.name
            )

        return await self.library.cover_for(song, _upload_limit(self))

    async def play(
        self,
        song: Song,
        *,
        push_history: bool = True,
        reset_future: bool = True,
    ) -> bool:
        """Start one track, loading its tags and cover on the way in.

        ``reset_future`` is what ``previous`` and the skip that follows it turn
        off: stepping backwards and forwards through the history has to keep the
        redo stack, while playing anything genuinely new retires it.
        """
        if not self.voice or not self.voice.is_connected():
            return False
        # A bucket-backed track is fetched into the cache the first time it is
        # played; everything after that is a local file like any other.
        path = await self.library.local_audio(song)
        if path is None:
            logger.warning("Missing audio file: %s", song.path)
            return False

        info = await self.library.track_info(song)
        cover = await self._cover_for(song)
        previous = self.current

        try:
            # A local file needs no reconnect options, and ``-vn`` keeps ffmpeg
            # from trying to decode the embedded cover art as video.
            source = discord.FFmpegPCMAudio(
                str(path), before_options="-nostdin", options="-vn"
            )
            if self.voice.is_playing():
                self.voice.stop()
            self.voice.play(source, after=self._finished)
        except (discord.ClientException, OSError):
            logger.exception("Could not play %s", song.path)
            return False

        self.current = song
        self.info = info
        self.cover = cover
        self.paused = False

        if push_history and previous is not None and previous is not song:
            self.history.append(previous)
            if len(self.history) > self.HISTORY_LIMIT:
                self.history.pop(0)
        if reset_future:
            self.future.clear()

        if info.title and info.title != song.title:
            logger.debug("Playing %r (tagged %r)", song.title, info.title)
        await self.refresh()
        return True

    def _finished(self, error: Exception | None = None) -> None:
        """ffmpeg calls this from its own thread once a track runs out."""
        if error is not None:
            logger.warning("Playback error: %s", error)
        try:
            future = asyncio.run_coroutine_threadsafe(self._finished_async(), self.bot.loop)
        except RuntimeError:  # loop already closed (shutdown)
            return
        future.add_done_callback(
            lambda done: done.exception() and logger.warning(
                "Could not advance the radio: %s", done.exception()
            )
        )

    async def _finished_async(self) -> None:
        """Move on once ffmpeg has finished with the file handle."""
        await asyncio.sleep(0.4)
        if self.current is None:
            return
        if self.voice and self.voice.is_connected():
            await self.advance()

    async def skip(self) -> bool:
        """Stop the current track so ``advance`` picks the next one."""
        if not _is_active(self.voice):
            return False
        self._skipping = True
        self.voice.stop()
        return True

    async def previous(self) -> bool:
        """Step back one track.

        Whatever was on is parked on the forward stack rather than dropped, so
        the next skip returns to it instead of picking something new.
        """
        if not self.history:
            return False
        song = self.history.pop()
        if self.current is not None and self.current is not song:
            self.future.append(self.current)
            if len(self.future) > self.HISTORY_LIMIT:
                self.future.pop(0)
        self._skipping = True
        if _is_active(self.voice):
            if self.voice.is_paused():
                self.voice.resume()
            self.voice.stop()
        async with self._advance_lock:
            played = await self.play(song, push_history=False, reset_future=False)
            if played:
                self._skipping = False
        return played

    async def resume(self) -> bool:
        """Un-pause, or start the next track when nothing is loaded."""
        if _is_paused(self.voice):
            self.voice.resume()
            self.paused = False
            await self.refresh()
            return True
        if _is_playing(self.voice):
            return False
        await self.advance()
        return True

    async def pause(self) -> bool:
        if not _is_playing(self.voice):
            return False
        self.voice.pause()
        self.paused = True
        await self.refresh()
        return True

    async def toggle_loop(self) -> bool:
        self.loop = not self.loop
        await self.refresh()
        return self.loop

    # -- lifecycle -------------------------------------------------------
    def stopped_view(self, headline: str, body: str) -> SimpleLayout:
        return panel(
            discord.ui.TextDisplay(f"# {headline}\n{body}"),
            colour=EMBED_COLORS.get("warning", EMBED_COLOR),
        )

    async def ensure_project_emojis(self) -> None:
        """Warm the project->emoji map so dropdowns can show project logos.

        The map is owned by the Mixtapes cog and filled lazily the first time
        one of its commands runs; the radio can be used without ever touching
        those, so it asks for the map itself before painting a card.
        """
        mixtapes = self.cog.bot.get_cog("Mixtapes")
        ensure = getattr(mixtapes, "_ensure_emojis", None)
        if ensure is None:
            return
        try:
            await ensure()
        except Exception:
            logger.debug("Could not warm project emojis", exc_info=True)

    async def refresh(self) -> None:
        """Repaint the now-playing card, or post it the first time."""
        await self.ensure_project_emojis()
        view = RadioView(self)
        files = view.attachments()
        try:
            if self.message is not None:
                await self.message.edit(view=view, attachments=files)
                return
        except discord.NotFound:
            self.message = None
        except discord.HTTPException:
            logger.exception("Could not update the radio card")
            return

        channel = self.text_channel()
        if channel is None:
            return
        try:
            self.message = await channel.send(view=view, files=files)
        except discord.HTTPException:
            logger.exception("Could not post the radio card")

    def text_channel(self) -> discord.abc.Messageable | None:
        if self.channel_id is None:
            return None
        channel = self.bot.get_channel(self.channel_id)
        return channel if isinstance(channel, discord.abc.Messageable) else None

    async def show_picker(self, channel: discord.abc.Messageable) -> None:
        """Post (or repaint) the standalone request picker in ``channel``.

        The card is remembered on the station and reused when it is still
        sitting in the same channel, so ``,slimefm songs`` pressed twice — or a
        ``,setradio`` after one — refreshes one card rather than leaving a
        stack of them behind.
        """
        await self.ensure_project_emojis()
        card = RequestMenu(self)
        message = self.request_message
        if message is not None and getattr(message.channel, "id", None) == getattr(channel, "id", None):
            try:
                await message.edit(view=card)
                return
            except discord.NotFound:
                self.request_message = None
            except discord.HTTPException:
                logger.exception("Could not repaint the request picker")
                return
        try:
            self.request_message = await channel.send(view=card)
        except discord.HTTPException:
            logger.exception("Could not post the request picker")

    async def spawn_interface(self, channel: discord.abc.Messageable) -> None:
        """Draw the station's now-playing card in ``channel``.

        Used by ``,setradio`` and ``,setradiovc`` to give a channel the station's
        controls. A card left behind in some other channel is taken down first,
        so the station is only ever drawn in one place.
        """
        previous = self.message
        if previous is not None and getattr(previous.channel, "id", None) != getattr(channel, "id", None):
            try:
                await previous.delete()
            except discord.HTTPException:
                pass
            self.message = None
        self.channel_id = getattr(channel, "id", self.channel_id)
        await self.refresh()

    async def teardown(self, *, disconnect: bool = True, headline: str | None = None) -> None:
        """Stop the music and (optionally) leave the channel."""
        voice = self.voice
        if voice is not None:
            try:
                if _is_active(voice):
                    voice.stop()
                if disconnect and voice.is_connected():
                    await voice.disconnect(force=True)
            except Exception:
                logger.exception("Could not leave the voice channel")
        self.voice = None
        self.current = None
        self.info = None
        self.cover = None
        self.queue.clear()
        self.history.clear()
        self.future.clear()
        self.paused = False
        if headline and self.message is not None:
            try:
                await self.message.edit(
                    view=self.stopped_view(headline, "-# Run the station again whenever you like."),
                    attachments=[],
                )
            except discord.HTTPException:
                pass
        self.message = None


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------
class RadioView(discord.ui.LayoutView):
    """The now-playing card: cover art, tags, transport and the request menu."""

    def __init__(self, player: MixtapeRadio) -> None:
        super().__init__(timeout=None)
        self.player = player
        self.cover_path = player.cover
        self.cover_name = (
            f"{_safe_name(player.current.title if player.current else 'cover')}"
            f"{player.cover.suffix.lower()}"
            if player.cover is not None
            else None
        )

        song = player.current
        info = player.info
        playing = _is_playing(player.voice)
        paused = _is_paused(player.voice)

        colour = EMBED_COLOR
        if paused:
            colour = EMBED_COLORS.get("warning", EMBED_COLOR)
        elif playing:
            colour = EMBED_COLORS.get("success", EMBED_COLOR)
        box = discord.ui.Container(accent_color=colour)

        if song is None:
            box.add_item(
                discord.ui.TextDisplay(
                    "# 📻 Mixtape Radio\n-# Nothing queued up yet."
                )
            )
            box.add_item(divider())
            box.add_item(discord.ui.ActionRow(*self._buttons(playing, paused)))
            box.add_item(divider())
            for item in request_section(player):
                box.add_item(item)
            self.add_item(box)
            return

        # Title and tags. The tag values win over the filename when present.
        title = (info.title if info and info.title else song.title) or song.title
        credit = info.credit if info else ""
        header = f"# {title}"
        if credit:
            header += f"\n-# {credit}"

        if self.cover_name:
            box.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay(header),
                    accessory=discord.ui.Thumbnail(media=f"attachment://{self.cover_name}"),
                )
            )
        else:
            box.add_item(discord.ui.TextDisplay(header))

        # The card stays deliberately tiny: the title, the artist/album line
        # and the cover are all that earn their space above the controls.
        box.add_item(discord.ui.ActionRow(*self._buttons(playing, paused)))
        box.add_item(divider())
        for item in request_section(player):
            box.add_item(item)
        self.add_item(box)

    # -- pieces ----------------------------------------------------------
    def attachments(self) -> list[discord.File]:
        """Fresh handles every time: an edit has to re-upload the cover."""
        if self.cover_path is None or self.cover_name is None:
            return []
        try:
            return [discord.File(self.cover_path, filename=self.cover_name)]
        except OSError:
            logger.warning("Cover art went missing: %s", self.cover_path)
            return []

    def _buttons(self, playing: bool, paused: bool) -> list[discord.ui.Item]:
        """The transport row: emoji only, so the card stays as short as possible."""
        player = self.player
        return [
            TransportButton(
                player,
                action="previous",
                emoji="⏮️",
                style=discord.ButtonStyle.secondary,
                disabled=not player.history,
            ),
            TransportButton(
                player,
                action="toggle",
                emoji="▶️" if not playing else "⏸️",
                style=discord.ButtonStyle.success if not playing else discord.ButtonStyle.secondary,
                disabled=not playing and not paused and player.current is None,
            ),
            TransportButton(
                player,
                action="skip",
                emoji="⏭️",
                style=discord.ButtonStyle.secondary,
                disabled=not (playing or paused),
            ),
            TransportButton(
                player,
                action="loop",
                emoji="🔂" if player.loop else "🔁",
                style=discord.ButtonStyle.success if player.loop else discord.ButtonStyle.secondary,
            ),
        ]


class TransportButton(discord.ui.Button):
    """One button per transport action, wired straight to the player."""

    def __init__(
        self,
        player: MixtapeRadio,
        *,
        action: str,
        emoji: str,
        style: discord.ButtonStyle,
        disabled: bool = False,
    ) -> None:
        self.player = player
        self.action = action
        super().__init__(emoji=emoji, style=style, disabled=disabled)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not self.player.may_control(interaction.user):
            await interaction.response.send_message(
                "Join the voice channel to use the radio controls.", ephemeral=True
            )
            return
        await interaction.response.defer()
        player = self.player
        if self.action == "previous":
            await player.previous()
        elif self.action == "skip":
            await player.skip()
        elif self.action == "loop":
            await player.toggle_loop()
        elif self.action == "toggle":
            if _is_playing(player.voice):
                await player.pause()
            else:
                await player.resume()


class RequestSelect(discord.ui.Select):
    """The request dropdown: one mixtape's tracks, queued in one click.

    Every option carries the project's own emoji when one is uploaded, so a
    track is recognisable at a glance. The emoji comes from the live library
    map (never a raw name), so a project with no matching emoji falls back to a
    plain disc instead of a broken ``:name:id``.
    """

    def __init__(
        self,
        player: MixtapeRadio,
        songs: list[Song],
        *,
        gated: bool = True,
        owner_id: int | None = None,
        dismiss: bool = False,
    ) -> None:
        self.player = player
        self.songs = list(songs)[:MAX_OPTIONS]
        #: Whether queueing needs the member to be in the voice channel.
        self.gated = gated
        #: When set, only this member may pick (the inbox's own request).
        self.owner_id = owner_id
        #: Whether the card is disposable once something is picked.
        self.dismiss = dismiss
        library = player.library
        options = [
            discord.SelectOption(
                label=song.title[:100],
                value=song.qualified[:100],
                description=f"{song.project} · request"[:100],
                emoji=(
                    library.emoji_for(song.project) if library is not None else None
                ),
            )
            for song in self.songs
        ]
        super().__init__(
            placeholder="Request a song",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.owner_id is not None and interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Type your own request in this channel to queue a song.",
                ephemeral=True,
            )
            return
        if self.gated and not self.player.may_control(interaction.user):
            await interaction.response.send_message(
                "Join the voice channel to queue songs.", ephemeral=True
            )
            return

        song = next(
            (item for item in self.songs if item.qualified == self.values[0]), None
        )
        if song is None:
            await interaction.response.defer()
            return

        player = self.player
        # Nothing playing? Jump straight to the pick, otherwise queue it.
        if player.current is None or not _is_active(player.voice):
            await interaction.response.defer()
            if player.current is not None:
                player.enqueue(player.current)
            await player.play(song)
            await self._dismiss(interaction)
            return

        position = player.enqueue(song)
        await interaction.response.send_message(
            f"📻 **{song.title}** is queued at position {position}.", ephemeral=True
        )
        # The confirmation only exists to say it worked, so it is taken back
        # down again and the channel keeps nothing but the station's cards.
        asyncio.create_task(player.cog.expire_confirmation(interaction))
        await self._dismiss(interaction)
        await player.refresh()

    async def _dismiss(self, interaction: discord.Interaction) -> None:
        """Clear a disposable request card once its song is on the way."""
        if not self.dismiss or interaction.message is None:
            return
        try:
            await interaction.message.delete()
        except discord.HTTPException:
            pass


class PageButton(discord.ui.Button):
    """Previous / Next over the mixtape list.

    Deliberately ungated: paging only changes what the dropdown offers, and the
    card usually sits in a text channel where most members are just browsing.
    The station's own state holds the position (``request_index``), so whichever
    card the button was pressed on is the card that gets repainted.
    """

    def __init__(
        self,
        player: MixtapeRadio,
        delta: int,
        *,
        total: int,
        emoji: str,
        card: "RequestMenu | None" = None,
    ) -> None:
        self.player = player
        self.delta = delta
        self.total = max(1, total)
        self.card = card
        super().__init__(
            emoji=emoji,
            style=discord.ButtonStyle.secondary,
            disabled=self.total < 2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.player.request_index = (self.player.request_index + self.delta) % self.total
        if self.card is not None:
            self.card.rebuild()
            await swap_layout_message(interaction, self.card)
            return
        # No card of its own: this button lives on the now-playing message, so
        # the station repaints that one from its own state.
        await interaction.response.defer()
        await self.player.refresh()


def request_section(
    player: MixtapeRadio,
    card: "RequestMenu | None" = None,
) -> list[discord.ui.Item]:
    """The request picker: one mixtape, its tracks, and the pager.

    Shared by the now-playing card and the standalone ``,slimefm songs`` card so
    the two can never drift apart. The dropdown only ever holds the selected
    project's tracks — hence the name above it, which is the one piece of
    context the options themselves cannot carry — and Previous/Next walk the
    projects in name order, wrapping at either end.
    """
    projects = sorted(player.library.project_names(), key=str.casefold)
    if not projects:
        return [discord.ui.TextDisplay("-# No mixtapes to request from yet.")]

    index = player.request_index % len(projects)
    project = projects[index]
    found = player.library.get_project(project)
    songs = list(found.songs) if found is not None else []
    shown = songs[:MAX_OPTIONS]
    subtitle = f"{len(songs)} song(s)"
    if len(songs) > len(shown):
        subtitle += f" · showing {len(shown)}"

    items: list[discord.ui.Item] = [
        discord.ui.TextDisplay(f"-# 🎵 {project} · {index + 1}/{len(projects)} · {subtitle}"),
    ]
    if shown:
        items.append(discord.ui.ActionRow(RequestSelect(player, shown)))
    else:
        items.append(discord.ui.TextDisplay("-# No audio files in this project."))
    items.append(
        discord.ui.ActionRow(
            PageButton(player, -1, total=len(projects), emoji="◀️", card=card),
            PageButton(player, 1, total=len(projects), emoji="▶️", card=card),
        )
    )
    return items


class RequestMenu(SimpleLayout):
    """The standalone request card posted by ``,slimefm songs``.

    The now-playing card carries the very same section; this is that section on
    its own, for when the station's message is a long way up the channel.
    """

    def __init__(self, player: MixtapeRadio) -> None:
        self.player = player
        super().__init__(container(*request_section(player, self)))

    def rebuild(self) -> None:
        self.clear_items()
        self.add_item(container(*request_section(self.player, self)))


class InboxPicker(SimpleLayout):
    """The card posted when a typed request matches more than one song.

    Only the member who typed the request may pick from it, and the card takes
    itself away once something is queued or after ``SELECT_TIMEOUT`` — the
    inbox is meant to stay a tidy list of requests, not a pile of dropdowns.
    """

    def __init__(self, player: MixtapeRadio, matches: list[Song], *, requester: int) -> None:
        self.message: discord.Message | None = None
        box = container(
            discord.ui.TextDisplay(
                f"# 🎵 Which one?\n"
                f"-# {len(matches)} songs match that name · only the member who "
                "typed it can pick"
            ),
            divider(),
            discord.ui.ActionRow(
                RequestSelect(
                    player,
                    matches,
                    gated=False,
                    owner_id=requester,
                    dismiss=True,
                )
            ),
        )
        super().__init__(box, timeout=SELECT_TIMEOUT)

    async def on_timeout(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.delete()
        except discord.HTTPException:
            pass


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------
class Radio(commands.Cog):
    """Mixtape Radio: shuffle the whole folder into a voice channel."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.players: dict[int, MixtapeRadio] = {}
        # Prefix commands can't declare autocomplete; the slash bridge reads
        # these mappings when it builds the app commands. The key has to match
        # the subcommand's own parameter (``name`` for the two that take one
        # song, ``query`` for the browsing one) or the bridge ignores it.
        for name, parameter in (
            ("start", "name"),
            ("request", "name"),
            ("songs", "query"),
        ):
            child = self.radio.get_command(name)
            if isinstance(child, commands.Command):
                child.autocompletes = {parameter: self._song_autocomplete}

    @property
    def library(self):
        return self.bot.mixtapes

    # -- helpers ---------------------------------------------------------
    def player_for(self, guild_id: int, *, create: bool = False) -> MixtapeRadio | None:
        player = self.players.get(int(guild_id))
        if player is None and create:
            player = MixtapeRadio(self, int(guild_id))
            self.players[int(guild_id)] = player
        return player

    def _station_card_channel(
        self, guild: discord.Guild, voice_channel: discord.VoiceChannel
    ) -> discord.abc.Messageable:
        """Where the 24/7 station's card goes.

        The request channel when one is configured, since that is where people
        already type song names; otherwise the voice channel's own text chat, so
        the controls are still an arm's length from the music.
        """
        settings = getattr(self.bot, "dash_settings", None)
        text_id = settings.radio_channel_id(guild.id) if settings is not None else None
        if text_id is not None:
            channel = guild.get_channel(text_id)
            if isinstance(channel, discord.abc.Messageable):
                return channel
        return voice_channel

    async def _bring_station_up(
        self, guild: discord.Guild, voice_channel: discord.VoiceChannel
    ) -> bool:
        """Join the station's voice channel and start shuffling.

        Shared by ``,setradiovc`` and the startup restore, so the two can never
        disagree about how the 24/7 station comes up. ``False`` means the bot
        couldn't join, which callers report rather than retrying.
        """
        player = self.player_for(guild.id, create=True)
        if player is None:
            return False

        already_playing = (
            player.voice is not None
            and player.voice.is_connected()
            and player.voice.channel == voice_channel
            and player.current is not None
        )
        # A resident station is nobody's personal one, so no invoker gets the
        # member shortcut: being in the channel is what unlocks the controls.
        player.invoker_id = None
        if not await player.connect(voice_channel, None):
            return False

        # Draw the card where it belongs before anything plays: otherwise the
        # first refresh would keep repainting a card left in another channel.
        card = self._station_card_channel(guild, voice_channel)
        await player.spawn_interface(card)
        if not already_playing:
            player.queue.clear()
            player.history.clear()
            player.future.clear()
            await player.advance()
        return True

    async def _restore_stations(self) -> None:
        """Bring every configured 24/7 station back up. Safe to call again."""
        settings = getattr(self.bot, "dash_settings", None)
        if settings is None:
            return
        for guild in list(self.bot.guilds):
            voice_id = settings.radio_voice_id(guild.id)
            if voice_id is None:
                continue
            channel = guild.get_channel(voice_id)
            if not isinstance(channel, discord.VoiceChannel):
                continue
            player = self.player_for(guild.id)
            if (
                player is not None
                and player.voice is not None
                and player.voice.is_connected()
                and player.voice.channel == channel
            ):
                continue
            if not await self._bring_station_up(guild, channel):
                logger.warning("Could not park the radio in %s", channel.id)

    async def _clear(self, message: discord.Message) -> None:
        """Take a request out of the inbox, if the bot is allowed to."""
        try:
            await message.delete()
        except discord.HTTPException:
            logger.debug("Could not delete request %s", message.id, exc_info=True)

    async def expire_confirmation(
        self, interaction: discord.Interaction, *, seconds: float = FLASH_SECONDS
    ) -> None:
        """Take a private confirmation back down after a moment.

        A dropdown pick answers ephemerally, so there is no channel message to
        delete — the interaction's own response is removed instead, which stops
        the feedback from lingering on the member's screen.
        """
        await asyncio.sleep(seconds)
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass

    async def _flash(
        self,
        target: discord.abc.Messageable,
        text: str,
        *,
        seconds: float = FLASH_SECONDS,
    ) -> None:
        """Answer in the inbox for a moment, then tidy the answer away too."""
        try:
            message = await target.send(view=notice(text, emoji="📻"))
        except discord.HTTPException:
            logger.debug("Could not answer in the request inbox", exc_info=True)
            return
        await asyncio.sleep(seconds)
        try:
            await message.delete()
        except discord.HTTPException:
            pass

    async def _song_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        current = (current or "").strip()
        songs = (
            self.library.search_songs(current, limit=MAX_OPTIONS)
            if current
            else self.library.all_songs()[:MAX_OPTIONS]
        )
        return [
            app_commands.Choice(
                name=f"{song.title} · {song.project}"[:100], value=song.qualified[:100]
            )
            for song in songs
        ]

    def _voice_channel(self, ctx: commands.Context) -> discord.VoiceChannel | None:
        state = getattr(ctx.author, "voice", None)
        channel = getattr(state, "channel", None)
        return channel if isinstance(channel, discord.VoiceChannel) else None

    async def _running(self, ctx: commands.Context) -> MixtapeRadio | None:
        """The station for this guild, or a message explaining why there isn't one."""
        player = self.player_for(ctx.guild.id)
        if player is None or player.voice is None or not player.voice.is_connected():
            await ctx.send(view=notice(f"The radio isn't running. `{PREFIX}radio` starts it."))
            return None
        if not player.may_control(ctx.author):
            await ctx.send(
                view=notice(
                    "Join the voice channel the radio is playing in to control it."
                )
            )
            return None
        return player

    async def _resolve_song(
        self, ctx: commands.Context, query: str
    ) -> Song | None:
        query = (query or "").strip()
        if not query:
            return None
        matches = self.library.search_songs(query, limit=3)
        if not matches:
            await ctx.send(
                view=notice(f"I don't have anything matching **{query}** in the mixtapes.")
            )
            return None
        # Autocomplete hands back "Project::Title"; a plain search returns Songs.
        exact = next((song for song in matches if song.qualified.lower() == query.lower()), None)
        return exact or matches[0]

    async def _start(
        self, ctx: commands.Context, *, song: Song | None = None
    ) -> None:
        if self.library.is_empty():
            await ctx.send(
                view=notice("The `Mixtapes` folder is empty — nothing to play.")
            )
            return

        channel = self._voice_channel(ctx)
        if channel is None:
            await ctx.send(
                view=notice("Join a voice channel first, then start the radio.")
            )
            return

        player = self.player_for(ctx.guild.id, create=True)
        assert player is not None
        was_connected = player.voice is not None and player.voice.is_connected()

        if not await player.connect(channel, ctx.author.id):
            await ctx.send(view=notice("I couldn't join that voice channel."))
            return

        player.channel_id = ctx.channel.id

        if was_connected:
            # Already on air: this is a request, not a restart.
            if song is not None:
                position = player.enqueue(song)
                await ctx.send(
                    view=panel(
                        discord.ui.TextDisplay(
                            f"# 📻 Queued\n> **{song.title}** · {song.project}\n"
                            f"> position **{position}** of {len(player.queue)}"
                        )
                    ),
                    delete_after=FLASH_SECONDS,
                )
            await player.refresh()
            return

        player.queue.clear()
        player.history.clear()
        player.future.clear()
        if song is not None:
            player.queue.append(song)
        await player.advance()
        if player.current is None:
            await ctx.send(view=notice("I couldn't start anything from that folder."))
            return
        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    f"# 📻 On air\n> Joined **{channel.name}** · "
                    f"{self.library.song_count} song(s) in rotation"
                )
            )
        )

    # -- commands --------------------------------------------------------
    @commands.group(name="slimefm", aliases=["mixtaperadio"], invoke_without_command=True)
    @commands.guild_only()
    async def radio(self, ctx: commands.Context) -> None:
        f"""Start the mixtape radio in your voice channel.

        Usage: `{PREFIX}radio` — joins you and shuffles a random track from
        every project in `Mixtapes/`, then keeps going.
        """
        await self._start(ctx)

    @radio.command(name="start", aliases=["join"])
    @commands.guild_only()
    async def radio_start(self, ctx: commands.Context, *, name: str = "") -> None:
        f"""Start the radio, optionally on one specific song.

        Usage: `{PREFIX}radio start` for a random track, or
        `{PREFIX}radio start Bad Boy` to open on one.
        """
        song = await self._resolve_song(ctx, name) if name.strip() else None
        if name.strip() and song is None:
            return
        await self._start(ctx, song=song)

    @radio.command(name="stop", aliases=["leave", "off", "disconnect"])
    @commands.guild_only()
    async def radio_stop(self, ctx: commands.Context) -> None:
        f"""Clear the queue and leave the voice channel.

        Usage: `{PREFIX}radio stop`.
        """
        player = self.player_for(ctx.guild.id)
        if player is None:
            await ctx.send(view=notice("The radio isn't running."))
            return
        if not player.may_control(ctx.author):
            await ctx.send(view=notice("Join the voice channel to stop the radio."))
            return
        await player.teardown(headline="📻 Signed off")
        self.players.pop(int(ctx.guild.id), None)
        await ctx.send(view=panel(discord.ui.TextDisplay("# 📻 Signed off\n-# Queue cleared and left the channel.")))

    @radio.command(name="skip", aliases=["next"])
    @commands.guild_only()
    async def radio_skip(self, ctx: commands.Context) -> None:
        f"""Skip to the next track.

        Usage: `{PREFIX}radio skip`.
        """
        player = await self._running(ctx)
        if player is None:
            return
        if not await player.skip():
            await ctx.send(view=notice("Nothing is playing right now."))

    @radio.command(name="previous", aliases=["prev", "back"])
    @commands.guild_only()
    async def radio_previous(self, ctx: commands.Context) -> None:
        f"""Go back to the last track that played.

        Usage: `{PREFIX}radio previous`.
        """
        player = await self._running(ctx)
        if player is None:
            return
        if not await player.previous():
            await ctx.send(view=notice("There's nothing to go back to yet."))

    @radio.command(name="pause")
    @commands.guild_only()
    async def radio_pause(self, ctx: commands.Context) -> None:
        f"""Pause the current track.

        Usage: `{PREFIX}radio pause` · `{PREFIX}radio resume` to continue.
        """
        player = await self._running(ctx)
        if player is None:
            return
        if not await player.pause():
            await ctx.send(view=notice("Nothing is playing right now."))

    @radio.command(name="resume", aliases=["play", "unpause"])
    @commands.guild_only()
    async def radio_resume(self, ctx: commands.Context) -> None:
        f"""Resume a paused track, or start the next one.

        Usage: `{PREFIX}radio resume`.
        """
        player = await self._running(ctx)
        if player is None:
            return
        await player.resume()

    @radio.command(name="loop", aliases=["repeat"])
    @commands.guild_only()
    async def radio_loop(self, ctx: commands.Context) -> None:
        f"""Repeat the current track instead of moving on.

        Usage: `{PREFIX}radio loop` — run it again to switch looping off.
        """
        player = await self._running(ctx)
        if player is None:
            return
        looping = await player.toggle_loop()
        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    f"# {'🔂 Looping' if looping else '➡️ Looping off'}\n"
                    f"-# {'The current track will replay' if looping else 'Tracks play through once'}"
                )
            )
        )

    @radio.command(name="now", aliases=["playing", "current"])
    @commands.guild_only()
    async def radio_now(self, ctx: commands.Context) -> None:
        f"""Show (or repost) the now-playing card.

        Usage: `{PREFIX}radio now`.
        """
        player = self.player_for(ctx.guild.id)
        if player is None or player.current is None:
            await ctx.send(view=notice("Nothing is playing right now."))
            return
        player.channel_id = ctx.channel.id
        player.message = None
        await player.refresh()

    @radio.command(name="request", aliases=["queue", "add"])
    @commands.guild_only()
    async def radio_request(self, ctx: commands.Context, *, name: str) -> None:
        f"""Queue a specific song from the mixtapes.

        Usage: `{PREFIX}radio request Bad Boy` — it plays after whatever is on.
        """
        if not name.strip():
            await ctx.send(view=notice("Give me a song name to queue."))
            return
        song = await self._resolve_song(ctx, name)
        if song is None:
            return
        player = self.player_for(ctx.guild.id)
        if player is None or player.voice is None or not player.voice.is_connected():
            await self._start(ctx, song=song)
            return
        if not player.may_control(ctx.author):
            await ctx.send(view=notice("Join the voice channel to queue songs."))
            return
        position = player.enqueue(song)
        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    f"# 📻 Queued\n> **{song.title}** · {song.project}\n"
                    f"> position **{position}** of {len(player.queue)}"
                )
            ),
            delete_after=FLASH_SECONDS,
        )
        await player.refresh()

    @radio.command(name="songs", aliases=["menu", "browse", "picker"])
    @commands.guild_only()
    async def radio_songs(self, ctx: commands.Context, *, query: str = "") -> None:
        f"""Browse the mixtapes and queue from one of them.

        Usage: `{PREFIX}slimefm songs` opens the first mixtape, and
        `{PREFIX}slimefm songs barter` opens the one that matches. Previous and
        Next walk the whole folder, so anyone can browse without listening.
        """
        if self.library.is_empty():
            await ctx.send(view=notice("The `Mixtapes` folder is empty."))
            return

        # A query opens the project that holds its best match rather than
        # narrowing the dropdown, so the card always looks the same.
        index = 0
        query = query.strip()
        if query:
            matches = self.library.search_songs(query, limit=MENU_SIZE)
            if not matches:
                await ctx.send(
                    view=notice(f"Nothing in the mixtapes matches **{query}**.")
                )
                return
            projects = sorted(self.library.project_names(), key=str.casefold)
            wanted = matches[0].project
            index = next(
                (position for position, name in enumerate(projects) if name == wanted), 0
            )

        player = self.player_for(ctx.guild.id, create=True)
        assert player is not None
        player.channel_id = ctx.channel.id
        player.request_index = index
        await player.show_picker(ctx.channel)

    @commands.command(name="setradio", aliases=("radioinbox",))
    @commands.guild_only()
    @commands.check_any(commands.is_owner(), commands.has_permissions(manage_guild=True))
    async def setradio(self, ctx: commands.Context, *, channel: str | None = None) -> None:
        f"""Point the station's request inbox at a text channel.

        Usage: `{PREFIX}setradio #requests` — the station posts its now-playing
        card there, controls and request picker included. Anything typed there
        is read as a request too, and the message is cleared away: a unique
        match is queued straight away, an ambiguous one gets a dropdown. Every
        "queued" answer clears itself after a few seconds, so the channel is
        left holding just the card. Run `{PREFIX}setradio off` to close the
        inbox again.
        """
        settings = self.bot.dash_settings

        if channel is not None and channel.strip().lower() in {
            "off", "none", "clear", "disable", "stop",
        }:
            await settings.set_radio_channel(ctx.guild.id, None)
            await ctx.send(
                view=panel(
                    discord.ui.TextDisplay(
                        f"# 📻 Inbox closed\n"
                        f"-# Members can still request songs with "
                        f"`{PREFIX}radio request <song>`."
                    )
                ),
                delete_after=FLASH_SECONDS,
            )
            return

        target = ctx.channel
        if channel is not None:
            try:
                target = await commands.TextChannelConverter().convert(ctx, channel)
            except commands.BadArgument:
                await ctx.send(view=notice("That isn't a text channel I can post in."))
                return
        if not isinstance(target, discord.TextChannel):
            await ctx.send(view=notice("Pick a regular text channel."))
            return

        await settings.set_radio_channel(ctx.guild.id, target.id)

        # Post the station's card in the request channel, so requesting never
        # needs a command typed in there.
        player = self.player_for(ctx.guild.id, create=True)
        if player is not None:
            await player.spawn_interface(target)

        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    f"# 📻 Request inbox\n"
                    f"> **{target.mention}**\n\n"
                    "Type a song name there and it gets queued — no command "
                    "needed, and the message is deleted once it has been read.\n"
                    f"-# I need **Manage Messages** in that channel to clear them · "
                    f"`{PREFIX}setradio off` closes the inbox."
                )
            ),
            delete_after=FLASH_SECONDS,
        )

    @commands.command(name="setradiovc", aliases=("radiovc", "stationvc"))
    @commands.guild_only()
    @commands.check_any(commands.is_owner(), commands.has_permissions(manage_guild=True))
    async def setradiovc(self, ctx: commands.Context, *, channel: str | None = None) -> None:
        f"""Park the station in a voice channel, 24/7.

        Usage: `{PREFIX}setradiovc #radio` — the bot joins that channel now and
        again on every start, shuffling through the mixtapes on its own. Anyone
        sitting in the channel can use the controls on its card. Run
        `{PREFIX}setradiovc off` to let it go again.
        """
        settings = self.bot.dash_settings

        if channel is not None and channel.strip().lower() in {
            "off", "none", "clear", "disable", "stop",
        }:
            await settings.set_radio_voice(ctx.guild.id, None)
            player = self.player_for(ctx.guild.id)
            if player is not None:
                await player.teardown(headline="📻 Signed off")
                self.players.pop(int(ctx.guild.id), None)
            await ctx.send(
                view=panel(
                    discord.ui.TextDisplay(
                        f"# 📻 Station released\n"
                        "-# It won't rejoin on its own any more."
                    )
                ),
                delete_after=FLASH_SECONDS,
            )
            return

        target: discord.VoiceChannel | None = None
        if channel is not None:
            try:
                target = await commands.VoiceChannelConverter().convert(ctx, channel)
            except commands.BadArgument:
                await ctx.send(view=notice("That isn't a voice channel I can join."))
                return
        else:
            # No channel named: park it wherever the admin is sitting, the same
            # way ,slimefm reads their voice state.
            target = self._voice_channel(ctx)
            if target is None:
                await ctx.send(
                    view=notice(
                        "Join a voice channel first, or name the one to park in."
                    )
                )
                return

        if not target.permissions_for(ctx.guild.me).connect:
            await ctx.send(
                view=notice(f"I'm not allowed to join **{target.name}**.")
            )
            return

        await settings.set_radio_voice(ctx.guild.id, target.id)

        if not await self._bring_station_up(ctx.guild, target):
            await ctx.send(view=notice("I couldn't join that voice channel."))
            return

        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    f"# 📻 Station parked\n"
                    f"> **{target.mention}**\n\n"
                    "I'll shuffle the mixtapes in there, and rejoin on every "
                    "start.\n"
                    f"-# Anyone in the channel can drive the card · "
                    f"`{PREFIX}setradiovc off` releases it."
                )
            ),
            delete_after=FLASH_SECONDS,
        )

    # -- events ----------------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """The request inbox: a song name typed into the configured channel.

        The message is cleared as soon as it has been read and the answer is
        posted for a few seconds and cleared too, so the channel stays a list of
        requests rather than a chat log. A command is left alone — the station's
        own controls still work in there.
        """
        if message.author.bot or message.guild is None:
            return
        settings = getattr(self.bot, "dash_settings", None)
        if settings is None:
            return
        inbox = settings.radio_channel_id(message.guild.id)
        if inbox is None or message.channel.id != inbox:
            return

        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return

        query = (message.content or "").strip()
        if not query:
            return

        await self._clear(message)

        if self.library.is_empty():
            await self._flash(message.channel, "The `Mixtapes` folder is empty.")
            return

        matches = self.library.search_songs(query, limit=MENU_SIZE)
        if not matches:
            await self._flash(
                message.channel, f"Nothing in the mixtapes matches **{query}**."
            )
            return

        player = self.player_for(message.guild.id)
        if player is None or player.voice is None or not player.voice.is_connected():
            await self._flash(
                message.channel,
                f"The station is off air — `{PREFIX}radio` gets it going, then "
                "requests work in here.",
            )
            return

        exact = next(
            (song for song in matches if song.title.casefold() == query.casefold()), None
        )
        if exact is not None or len(matches) == 1:
            song = exact or matches[0]
            position = player.enqueue(song)
            await self._flash(
                message.channel,
                f"**{song.title}** · {song.project} — queued at position {position}.",
            )
            await player.refresh()
            return

        await player.ensure_project_emojis()
        picker = InboxPicker(player, matches, requester=message.author.id)
        picker.message = await message.channel.send(view=picker)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Leave when the humans do, so the bot isn't parked in an empty channel."""
        if member.bot or member.guild is None:
            return
        player = self.players.get(int(member.guild.id))
        if player is None or player.voice is None or not player.voice.is_connected():
            return
        channel = player.voice.channel
        if channel is None:
            return
        if any(not occupant.bot for occupant in channel.members):
            return
        # A 24/7 station is parked where it is on purpose, so an empty channel
        # is exactly what it is for. Everything else signs off here.
        settings = getattr(self.bot, "dash_settings", None)
        if settings is not None and settings.radio_voice_id(member.guild.id) == channel.id:
            return
        await player.teardown(headline="📻 Everyone left, so the station signed off")
        self.players.pop(int(member.guild.id), None)
        logger.info("Left the empty voice channel %s", channel.id)

    async def cog_load(self) -> None:
        """Park the 24/7 stations again when the cog loads onto a live bot."""
        if self.bot.is_ready():
            await self._restore_stations()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """And on every connect, which is when the channel cache is populated."""
        await self._restore_stations()

    async def cog_unload(self) -> None:
        """Take every station off air when the cog reloads or unloads."""
        for player in list(self.players.values()):
            try:
                await player.teardown()
            except Exception:
                logger.exception("Could not shut a radio down cleanly")
        self.players.clear()

    def cog_check(self, ctx: commands.Context) -> bool:  # noqa: D401 - inherited docs
        # Music needs a guild: there is no voice channel in a DM.
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Radio(bot))
