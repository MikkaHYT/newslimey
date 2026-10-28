"""Radio: a station that plays *any* song, with timed lyrics that follow it.

    !radio <song or link>     resolve, join your channel and play it
    !radio play <...>         the same, spelled out — playlists included
    !radio queue [song]       show the queue, or add to it
    !radio skip / previous    move through the queue
    !radio pause / resume     hold the music, then let it go
    !radio loop / shuffle     repeat, or play the queue out of order
    !radio autoplay           keep going when the queue runs dry — 24/7
    !radio karaoke            suppress the vocals and push the instrumental up
    !radio filter <name>      bass boost, clarity and other EQ presets
    !radio setvc [#channel]   park the station in a voice channel for good
    !radio volume <0-200>     set the output level
    !radio remove <n>         drop one entry from the queue
    !radio clear              empty the queue
    !radio now                repost the now-playing card
    !radio lyrics             post the live, timed-lyrics card
    !radio search <song>      numbered results — reply with the number to queue one
    !radio stop               clear everything and leave the channel
    !dj @role                 only that role may drive the station (off = anyone)

Unlike the mixtape station — ``,slimefm``, in ``cogs/radio.py`` — which shuffles
a fixed folder, this one takes anything the web can serve: a SoundCloud or
YouTube link (through cobalt or yt-dlp), a Spotify link (its public metadata is
read and the song is found on YouTube, since Spotify serves no audio), or a
plain song name.

**Where a song comes from.** The song API behind the ``,np`` getsong grabber is
asked first — it serves whole songs — and SoundCloud through yt-dlp is the
fallback. Every download is measured against the length the catalogue
advertised, so a 30-second SoundCloud preview is swapped for the full song on
YouTube rather than playing as a track that cuts out. Whatever comes back is
downloaded once and played from disk, so a track never dies mid-song on an
expiring CDN link.

**Playlists.** Pasting a Spotify playlist or album, a YouTube playlist or a
SoundCloud set queues the whole thing (up to ``PLAYLIST_LIMIT`` tracks): the
entries are read as names, the first one starts playing and the audio of each is
fetched only as it comes up. Spotify playlists are read from their public embed
page, so no API key is needed, and their tracks are looked up by name.

**Karaoke** (``!radio karaoke``) swaps the live source for a
vocal-suppressed render of the same file: the two stereo channels are subtracted
against each other, which cancels a centred lead vocal, the centre's low end is
mixed back so the backing keeps its body, and the result is normalised. That
side signal is played to *both* channels in phase, so a mono device (a phone
speaker, a single-speaker Bluetooth box) hears the instrumental rather than the
silence two mirrored channels would sum to. A file with no stereo image to
cancel — a mono or dual-mono upload, where subtracting the channels would take
the whole song with it — is measured first and scooped around the vocal range
instead, so it comes back quiet where the voice was rather than silent. Toggling
it mid-song restarts the track where it was, so nobody loses their place.

**Filters** (``!radio filter``) put one EQ preset in front of the music — bass
boost, deep bass, clarity, vocal-forward, club and loud — as an ffmpeg filter on
the voice stream, so it stacks with karaoke and with the volume, and switching
one restarts the current track where it was so the change is heard immediately.

**Autoplay** never runs dry. When the queue empties it looks for more from the
same artist, then widens to a YouTube search for a mix around them, and only
gives up with something to say when even that finds nothing. With autoplay on
the next track is fetched ahead of time, so the station rolls on continuously —
and ``!radio setvc #channel`` parks it in a voice channel 24/7, re-joining on
every start the way ``,setradiovc`` does for the mixtapes.

The card follows the now-playing card people are used to — cover, title, length,
who asked, and which song it took over from — and the lyrics card mirrors the
current line in bold as the song runs, the way the reference screenshots show.
When one song takes over from the last, the card is retired and posted again
underneath the new song, so the controls always sit with the music they belong
to; the lyric card goes with it and lyrics start switched off until the button
is pressed again. Stepping back and forward walks the songs the station has
already played in both directions, rather than picking something new on the way
forward.

**Picking from a list.** ``!radio search`` posts numbered results and
remembers them for that person in that channel; the next message they send as a
bare number queues that track, exactly as if they had typed the request out.
The pending pick is keyed by ``(channel, user)`` and expires, the same way the
AI cog's "reply with the number" choices work.

**Who may drive it.** ``!dj @role`` names the role that may play songs, skip,
pause, change the volume or touch autoplay, shuffle and karaoke; until one is
set, whoever started the station (or anybody in its voice channel) can work the
controls, and server managers and the bot's own staff always can. The card's
buttons ask for the same permission as the commands.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from pathlib import Path
from typing import Any

import aiohttp
import discord
from discord.ext import commands

from config import EMBED_COLOR, EMBED_COLORS, PREFIX
from utils.core import eras, radio, separate
from utils.core.views import SimpleLayout, usage_card

logger = logging.getLogger("slimey.station")

#: How many tracks "previous" can walk back through.
HISTORY_LIMIT = 25
#: How often the lyric card checks whether the highlighted line moved.
LYRIC_TICK = 1.5
#: How many lines of a synced lyric sheet the card shows around the current one.
LYRIC_WINDOW = 9
#: How many tracks ahead of the one playing to have downloaded and ready, so a
#: skip (or a track simply ending) lands on a file that is already on disk
#: instead of waiting on a download. Autoplay keeps the queue topped up to the
#: same depth, so the 24/7 station rolls on without a gap between songs.
PREFETCH_AHEAD = 4
#: How many times a track may be restarted after its voice stream dropped, and
#: how much of it has to be left before restarting is worth it at all.
RESUME_LIMIT = 2
RESUME_MIN_GAP = 15.0
#: How many results `!radio search` offers to pick from.
SEARCH_LIMIT = 10
#: How long a posted search waits for its number, matching the AI cog's
#: ``CHOICE_TTL_SECONDS``. After this the pick is dropped rather than a stray
#: number in chat queuing something from a stale list.
SEARCH_PICK_TTL = 180.0
#: A bare number, optionally ``#12.`` or ``7)`` — the same shape the AI cog's
#: pending choices accept.
_PICK_RE = re.compile(r"#?(\d{1,2})[.)]?\Z")

#: Where a parked station's voice channel is remembered, and where the DJ role
#: is kept. Both match the constants in ``utils.core.settings``; they are spelled
#: out here on purpose, since importing a name a reloaded process's older
#: settings module may not have would take the whole cog down.
STATION_VOICE_KEY = "station_voice"
LEGACY_STATION_VOICE_KEY = "wipradio_voice"
DJ_ROLE_KEY = "dj_role"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def stored_id(settings: Any, key: str, guild_id: int, *, accessor: str) -> int | None:
    """Read an id the dashboard stores, or ``None`` when it was never set.

    ``DashboardSettings`` grows a dedicated accessor for each new setting, but a
    cog reload does not re-import that module: reloading ``cogs.station`` into a
    process started before the accessor existed would otherwise fail with an
    ``AttributeError``. The generic store the accessor is built on has been
    there all along, so that is the fallback.
    """
    if settings is None:
        return None
    getter = getattr(settings, accessor, None)
    if callable(getter):
        return getter(int(guild_id))
    reader = getattr(settings, "stored", None)
    if not callable(reader):
        return None
    value = reader(key, guild_id=int(guild_id))
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def store_id(
    settings: Any, key: str, guild_id: int, value: int | None, *, setter: str
) -> None:
    """Remember (or clear) an id, preferring the dedicated accessor."""
    if settings is None:
        return
    write = getattr(settings, setter, None)
    if callable(write):
        await write(int(guild_id), value)
        return
    writer = getattr(settings, "set_value", None)
    if not callable(writer):
        logger.warning("DashboardSettings cannot store %s", key)
        return
    await writer(key, value, guild_id=int(guild_id))


def parked_voice_id(settings: Any, guild_id: int) -> int | None:
    """The voice channel a 24/7 station was parked in, or ``None``.

    The key this lived under before the cog was called something else is read
    too, so a station parked by that build is still found on the next start;
    new parks are written under the current key only.
    """
    found = stored_id(settings, STATION_VOICE_KEY, guild_id, accessor="station_voice_id")
    if found is not None:
        return found
    reader = getattr(settings, "stored", None)
    if not callable(reader):
        return None
    try:
        legacy = reader(LEGACY_STATION_VOICE_KEY, guild_id=int(guild_id))
        return int(legacy) if legacy else None
    except (TypeError, ValueError):
        return None


async def store_parked_voice(
    settings: Any, guild_id: int, channel_id: int | None
) -> None:
    """Remember (or forget) the channel a 24/7 station is parked in."""
    await store_id(
        settings, STATION_VOICE_KEY, guild_id, channel_id, setter="set_station_voice"
    )


def dj_role_id_for(settings: Any, guild_id: int) -> int | None:
    """The role allowed to drive the station, or ``None`` if everyone may."""
    return stored_id(settings, DJ_ROLE_KEY, guild_id, accessor="dj_role_id")


async def store_dj_role(settings: Any, guild_id: int, role_id: int | None) -> None:
    """Set (or clear) the role allowed to drive the station."""
    await store_id(settings, DJ_ROLE_KEY, guild_id, role_id, setter="set_dj_role")


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
    return usage_card(message, emoji=emoji)


def _is_playing(voice: discord.VoiceClient | None) -> bool:
    return bool(voice and voice.is_playing())


def _is_paused(voice: discord.VoiceClient | None) -> bool:
    return bool(voice and voice.is_paused())


def _is_active(voice: discord.VoiceClient | None) -> bool:
    return _is_playing(voice) or _is_paused(voice)


#: Where the bot caches its own application emoji, by name (set once at startup
#: by ``utils.core.eras.load_application_emojis``).
_APP_EMOJIS_ATTR = "_era_application_emojis"


def filter_presets() -> dict[str, tuple[str, str]]:
    """The EQ presets the loaded radio module has, or an empty mapping.

    Read through ``getattr`` because a cog reload does not re-import
    ``utils.core.radio``: a process started before the presets existed would
    otherwise fail to play a song instead of playing it untouched.
    """
    return getattr(radio, "PLAYBACK_FILTERS", None) or {}


def filter_chain(name: str) -> str:
    """The ffmpeg chain for a preset name, or ``""`` when there isn't one."""
    return (filter_presets().get(name) or ("", ""))[1]


def radio_mark(bot: Any) -> str:
    """The station's mark on its cards: the app's own ``radio`` emoji.

    The era module's helper is used when the loaded copy has it, and otherwise
    the emoji cache is read directly: a cog reload does not re-import
    ``utils.core.eras``, so importing the helper *by name* would stop this cog
    loading into a process that started before the helper existed. The heart the
    cards used to carry is the last resort, so a card always renders.
    """
    helper = getattr(eras, "application_emoji", None)
    emoji = helper(bot, "radio") if callable(helper) else None
    if emoji is None:
        cache = getattr(bot, _APP_EMOJIS_ATTR, None) or {}
        emoji = cache.get("radio")
    if emoji is None:
        for candidate in getattr(bot, "emojis", ()) or ():
            if getattr(candidate, "name", "").lower() == "radio":
                emoji = candidate
                break
    return str(emoji) if emoji is not None else "💜"


# ---------------------------------------------------------------------------
# Player
# ---------------------------------------------------------------------------
class StationPlayer:
    """One guild's station: a queue, a history and a voice client."""

    def __init__(self, cog: "Station", guild_id: int) -> None:
        self.cog = cog
        self.guild_id = int(guild_id)
        self.voice: discord.VoiceClient | None = None
        self.channel_id: int | None = None
        self.invoker_id: int | None = None
        self.message: discord.Message | None = None
        self.current: radio.Track | None = None
        self.queue: list[radio.Track] = []
        #: Back and forward, the way a browser does it: ``history`` is what
        #: "previous" steps back to and ``future`` is what "next" steps forward
        #: to, so walking back and forth lands on the same songs in order.
        self.history: list[radio.Track] = []
        self.future: list[radio.Track] = []
        #: The track this one took over from, named on the card.
        self.came_from: radio.Track | None = None
        self.loop = False
        self.shuffle = False
        self.autoplay = False
        #: Karaoke mode: play the vocal-suppressed render instead of the track.
        self.karaoke = False
        #: The EQ preset in front of the stream; a key of ``radio.PLAYBACK_FILTERS``.
        self.filter: str = "off"
        #: Parked in a configured channel for good, so it never signs off.
        self.persistent = False
        self.volume = 0.5
        self.paused = False
        #: Timed lyrics for the current track, and the card that follows them.
        self.lyrics: radio.Lyrics | None = None
        self.lyrics_message: discord.Message | None = None
        self._lyrics_task: asyncio.Task | None = None
        self._lyrics_index = -2
        #: Play-clock bookkeeping. ``_started_at`` is ``None`` while paused.
        self._position_base = 0.0
        self._started_at: float | None = None
        self._advance_lock = asyncio.Lock()
        self._skipping = False
        self._idle = False
        #: How many times this track has been restarted because the voice
        #: stream dropped before the file was over.
        self._resume_attempts = 0
        #: Karaoke renders we have already made, keyed by the source file, so a
        #: track is only ever stem-split once.
        self._karaoke_paths: dict[Path, Path] = {}
        #: Autoplay bookkeeping: what we have already played, so the station
        #: doesn't repeat itself, and the prefetch task that keeps it fed.
        self._autoplay_urls: set[str] = set()
        self._prefetch_task: asyncio.Task | None = None

    # -- wiring ----------------------------------------------------------
    @property
    def bot(self) -> commands.Bot:
        return self.cog.bot

    def position(self) -> float:
        """Seconds into the current track, honouring pauses."""
        if self._started_at is None:
            return self._position_base
        return self._position_base + (time.monotonic() - self._started_at)

    def may_control(self, user: discord.abc.User) -> bool:
        """Whether this member may drive the station.

        Delegates to the cog, so the DJ role governs the card's buttons and the
        commands alike rather than the two drifting apart.
        """
        return self.cog.may_control(user, self)

    def in_channel(self, user: discord.abc.User) -> bool:
        """The unlocked rule: whoever started it, or anyone in its channel."""
        user_id = getattr(user, "id", None)
        if user_id is not None and user_id == self.invoker_id:
            return True
        channel = self.voice.channel if self.voice and self.voice.is_connected() else None
        if channel is None:
            return False
        state = getattr(user, "voice", None)
        return state is not None and state.channel == channel

    async def connect(self, channel: discord.VoiceChannel, invoker_id: int | None) -> bool:
        self.invoker_id = invoker_id
        existing = self.voice or channel.guild.voice_client
        if existing is not None and existing.is_connected():
            if existing.channel != channel:
                await existing.move_to(channel)
            self.voice = existing
            return True
        try:
            self.voice = await channel.connect(self_deaf=True, timeout=20.0)
        except (asyncio.TimeoutError, discord.ClientException, discord.HTTPException, OSError):
            logger.exception("radio could not join %s", channel.id)
            self.voice = None
            return False
        return True

    # -- playback --------------------------------------------------------
    def _next_queued(self) -> radio.Track | None:
        if not self.queue:
            return None
        if self.shuffle:
            return self.queue.pop(random.randrange(len(self.queue)))
        return self.queue.pop(0)

    async def playback_path(self, track: radio.Track) -> Path | None:
        """The file to hand ffmpeg: the karaoke render when that mode is on.

        A render is made on first use and remembered, and if ffmpeg can't make
        one the original still plays rather than the station going silent.
        """
        if track.path is None:
            return None
        if not self.karaoke:
            return track.path
        # A separated instrumental beats a DSP render, and it is what the first
        # karaoke use was preparing in the background, so it wins over the
        # remembered render as soon as it exists.
        separated = separate.instrumental_for(track.path)
        if separated is not None:
            return separated
        cached = self._karaoke_paths.get(track.path)
        if cached is not None and cached.exists():
            return cached
        rendered = await radio.make_karaoke(track.path)
        if rendered is None:
            logger.info("radio could not render karaoke for %s", track.path)
            return track.path
        self._karaoke_paths[track.path] = rendered
        return rendered

    def _ffmpeg_options(self) -> str:
        """``-vn`` plus the station's EQ preset, when one is switched on."""
        chain = filter_chain(self.filter)
        return f"-vn -af {chain}" if chain else "-vn"

    async def play(
        self,
        track: radio.Track,
        *,
        push_history: bool = True,
        seek: float = 0.0,
        reset_future: bool = True,
    ) -> bool:
        """Start one track.

        ``push_history`` records the track being replaced on the back stack;
        ``reset_future`` throws the forward stack away because something new is
        starting. ``previous`` and the skip that comes after it turn both off,
        which is what lets back-and-forth land on the same songs.
        """
        if self.voice is None or not self.voice.is_connected():
            return False
        if track.path is None or not track.path.exists():
            track = await radio.download(self.cog.session, track)
            if track is None or track.path is None:
                return False

        path = await self.playback_path(track)
        if path is None:
            return False

        previous = self.current
        before = "-nostdin"
        if seek > 0.5:
            # Restarting a track in place (a karaoke toggle, say) picks up where
            # the listener was instead of dropping them back to the intro.
            before += f" -ss {seek:.2f}"
        source: discord.AudioSource = discord.FFmpegPCMAudio(
            str(path), before_options=before, options=self._ffmpeg_options()
        )
        source = discord.PCMVolumeTransformer(source, volume=self.volume)
        try:
            if self.voice.is_playing():
                self.voice.stop()
            self.voice.play(source, after=self._finished)
        except (discord.ClientException, OSError):
            logger.exception("radio could not play %s", path)
            return False

        self.current = track
        self.paused = False
        self._idle = False
        self._position_base = max(0.0, seek)
        self._started_at = time.monotonic()
        if seek <= 0.5:
            # A fresh track: any restarts counted against the last one are done.
            self._resume_attempts = 0
        self._autoplay_urls.add(track.key)
        new_song = previous is not track
        if push_history and previous is not None and new_song:
            self.history.append(previous)
            del self.history[:-HISTORY_LIMIT]
        if new_song:
            # A new song takes the cards over from the last one: they are
            # retired here and the card is sent again underneath this song.
            self.came_from = previous
            await self._retire_cards()
        if reset_future and new_song:
            # Something genuinely new retires the forward stack; stepping back
            # and forward again keeps it, so a restart in place keeps it too.
            self.future.clear()

        await self.cog.load_lyrics(track)
        self.lyrics = self.cog.lyrics_for(track)
        self._lyrics_index = -2
        self._start_lyrics_task()
        self._schedule_prefetch()
        await self.refresh()
        return True

    async def toggle_karaoke(self) -> bool | None:
        """Flip karaoke mode, restarting the current track in place if there is one.

        Returns the new state, or ``None`` when a render was needed and ffmpeg
        couldn't make one — the caller says so rather than pretending it worked.
        """
        desired = not self.karaoke
        track = self.current
        if desired and track is not None and track.path is not None:
            render = await radio.make_karaoke(track.path)
            if render is None:
                return None
            self._karaoke_paths[track.path] = render
        self.karaoke = desired
        if track is not None and _is_active(self.voice):
            position = self.position()
            was_paused = self.voice.is_paused() if self.voice else False
            if self.voice is not None and self.voice.is_paused():
                self.voice.resume()
            if await self.play(track, push_history=False, seek=position, reset_future=False):
                if was_paused and self.voice is not None:
                    self.voice.pause()
                    self.paused = True
                    self._position_base = position
                    self._started_at = None
        await self.refresh()
        return self.karaoke

    async def set_filter(self, name: str) -> str:
        """Switch the EQ preset, restarting the current track where it was.

        The preset is an ffmpeg option on the voice stream, so it only takes
        effect when ffmpeg starts; restarting in place is what makes it audible
        now rather than on the next song.
        """
        self.filter = name
        track = self.current
        if track is not None and _is_active(self.voice):
            position = self.position()
            was_paused = self.voice.is_paused() if self.voice else False
            if self.voice is not None and self.voice.is_paused():
                self.voice.resume()
            if await self.play(track, push_history=False, seek=position, reset_future=False):
                if was_paused and self.voice is not None:
                    self.voice.pause()
                    self.paused = True
                    self._position_base = position
                    self._started_at = None
        await self.refresh()
        return self.filter

    # -- prefetch ---------------------------------------------------------
    def _upcoming(self) -> list[radio.Track]:
        """The next few tracks in the queue, in the order they will play."""
        return list(self.queue[:PREFETCH_AHEAD])

    def _needs_prefetch(self) -> bool:
        """Whether any of the next few queued tracks is still only metadata."""
        return any(
            track.path is None or not track.path.exists() for track in self._upcoming()
        )

    def _schedule_prefetch(self) -> None:
        """Fetch the next few tracks in the background.

        Fetching a song only once it is due to play leaves a silence while it
        downloads, and a run of skips waits on one download after another;
        having the files already on disk is what makes a skip instant.
        """
        if not self.autoplay and not self._needs_prefetch():
            return
        if self._prefetch_task is not None and not self._prefetch_task.done():
            return
        self._prefetch_task = self.bot.loop.create_task(self._prefetch())

    async def _prefetch(self) -> None:
        try:
            await self._prefetch_queue()
            while self.autoplay and len(self.queue) < PREFETCH_AHEAD:
                track = await self.cog.autoplay_track(self)
                if track is None or track.path is None:
                    return
                self.queue.append(track)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Prefetch is only an optimisation: if it fails the station still
            # fetches the next track when the current one ends, so this stays a
            # one-liner rather than a traceback.
            logger.info("radio prefetch failed: %s", error)

    async def _prefetch_queue(self) -> None:
        """Download what is already queued, so skipping never waits on it."""
        for track in self._upcoming():
            if track.path is not None and track.path.exists():
                continue
            try:
                downloaded = await radio.download(self.cog.session, track)
            except Exception as error:
                logger.info("radio could not prefetch %r: %s", track.credit, error)
                continue
            if downloaded is not None and downloaded.path is not None:
                track.path = downloaded.path
                track.duration = downloaded.duration or track.duration

    def _finished(self, error: Exception | None = None) -> None:
        if error is not None:
            logger.warning("radio playback error: %s", error)
        try:
            future = asyncio.run_coroutine_threadsafe(self._finished_async(), self.bot.loop)
        except RuntimeError:  # loop closed during shutdown
            return
        future.add_done_callback(
            lambda done: done.exception()
            and logger.warning("radio advance failed: %s", done.exception())
        )

    async def _finished_async(self) -> None:
        await asyncio.sleep(0.4)
        track = self.current
        if track is None:
            return
        if self.voice is None or not self.voice.is_connected():
            return
        if await self._resume_if_cut_short(track):
            return
        await self.advance()

    async def _resume_if_cut_short(self, track: radio.Track) -> bool:
        """Pick a track back up when the stream stopped before it was over.

        A dropped voice stream ends playback mid-file, which is heard as the
        song cutting out; restarting it at the point it stopped turns that into
        a hiccup instead of a lost song. Loops, skips and files of unknown
        length carry on to the next track, and a couple of restarts are enough
        before the station moves on.
        """
        if self._skipping or self.loop or self._resume_attempts >= RESUME_LIMIT:
            return False
        if not track.duration:
            return False
        played = self.position()
        if played + RESUME_MIN_GAP >= track.duration:
            return False
        self._resume_attempts += 1
        logger.info(
            "%r stopped at %ss of %ss — carrying on from there",
            track.title,
            round(played, 1),
            round(track.duration, 1),
        )
        return await self.play(track, push_history=False, seek=played, reset_future=False)

    async def advance(self) -> None:
        """Pick and start the next track, serialized so two triggers can't race."""
        async with self._advance_lock:
            if self.voice is None or not self.voice.is_connected():
                return
            if self.voice.is_playing() or self.voice.is_paused():
                return

            skipped = self._skipping
            self._skipping = False

            if skipped and self.future:
                # An explicit skip after stepping back replays the song that
                # "previous" moved away from, the way a browser's forward
                # button does, rather than picking something new.
                track = self.future.pop()
                if await self.play(track, reset_future=False):
                    return

            if self.loop and self.current is not None and not skipped:
                # A replay, not a new song: the forward stack stays put.
                if await self.play(self.current, push_history=False, reset_future=False):
                    return

            while self.queue:
                track = self._next_queued()
                if track is not None and await self.play(track):
                    return

            if self.autoplay:
                track = await self.cog.autoplay_track(self)
                if track is not None and await self.play(track):
                    return

            await self._go_idle()

    async def _go_idle(self) -> None:
        self._idle = True
        self.current = None
        self.lyrics = None
        self._position_base = 0.0
        self._started_at = None
        task = self._prefetch_task
        self._prefetch_task = None
        if task is not None and not task.done():
            task.cancel()
        await self._stop_lyrics_task()
        if self.lyrics_message is not None:
            try:
                await self.lyrics_message.delete()
            except discord.HTTPException:
                pass
            self.lyrics_message = None
        await self.refresh()

    async def skip(self) -> bool:
        if not _is_active(self.voice):
            return False
        self._skipping = True
        self.voice.stop()
        return True

    async def previous(self) -> bool:
        """Step back one track, leaving it on the forward stack for "next"."""
        if not self.history:
            return False
        track = self.history.pop()
        if self.current is not None and self.current is not track:
            self.future.append(self.current)
            del self.future[:-HISTORY_LIMIT]
        if _is_active(self.voice):
            if self.voice.is_paused():
                self.voice.resume()
            self._skipping = True
            self.voice.stop()
        async with self._advance_lock:
            played = await self.play(track, push_history=False, reset_future=False)
            if played:
                self._skipping = False
            return played

    async def pause(self) -> bool:
        if not _is_playing(self.voice):
            return False
        self._position_base = self.position()
        self._started_at = None
        self.voice.pause()
        self.paused = True
        await self.refresh()
        return True

    async def resume(self) -> bool:
        if _is_paused(self.voice):
            self.voice.resume()
            self._started_at = time.monotonic()
            self.paused = False
            await self.refresh()
            return True
        if _is_playing(self.voice):
            return False
        await self.advance()
        return True

    async def set_volume(self, percent: int) -> float:
        self.volume = max(0.0, min(2.0, percent / 100.0))
        source = getattr(self.voice, "source", None) if self.voice else None
        if isinstance(source, discord.PCMVolumeTransformer):
            source.volume = self.volume
        return self.volume

    # -- lyrics ----------------------------------------------------------
    def _start_lyrics_task(self) -> None:
        if self.lyrics_message is None or not (self.lyrics and self.lyrics.is_synced):
            return
        if self._lyrics_task is None or self._lyrics_task.done():
            self._lyrics_task = self.bot.loop.create_task(self._lyrics_loop())

    async def _stop_lyrics_task(self) -> None:
        task = self._lyrics_task
        self._lyrics_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _lyrics_loop(self) -> None:
        """Repaint the lyric card only when the highlighted line actually moves."""
        try:
            while True:
                await asyncio.sleep(LYRIC_TICK)
                message = self.lyrics_message
                if message is None or self.lyrics is None or not self.lyrics.is_synced:
                    return
                if not _is_active(self.voice) and not self.paused:
                    return
                index = radio.line_index(self.lyrics.synced, self.position())
                if index != self._lyrics_index:
                    self._lyrics_index = index
                    try:
                        await message.edit(view=LyricsView(self))
                    except discord.NotFound:
                        self.lyrics_message = None
                        return
                    except discord.HTTPException:
                        logger.debug("radio could not update lyrics", exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("radio lyrics loop crashed")

    async def show_lyrics(self, channel: discord.abc.Messageable) -> None:
        if self.lyrics_message is not None:
            try:
                await self.lyrics_message.delete()
            except discord.HTTPException:
                pass
            self.lyrics_message = None
            await self._stop_lyrics_task()
            return
        if self.current is None or self.lyrics is None:
            return
        self._lyrics_index = -2
        self.lyrics_message = await channel.send(view=LyricsView(self))
        self._start_lyrics_task()

    # -- card ------------------------------------------------------------
    async def _drop_card(self) -> None:
        """Delete the card posted last, so its stale buttons can't be pressed."""
        if self.message is None:
            return
        try:
            await self.message.delete()
        except discord.HTTPException:
            pass
        self.message = None

    async def _retire_cards(self) -> None:
        """Clear the last song's cards so the new one starts with a clean pair.

        The controls card is deleted rather than edited, so :meth:`refresh`
        posts a fresh one under the song it belongs to, and the lyric card goes
        with it and stays off: lyrics are only ever pulled up on request, never
        carried over from the song before.
        """
        await self._stop_lyrics_task()
        if self.lyrics_message is not None:
            try:
                await self.lyrics_message.delete()
            except discord.HTTPException:
                pass
            self.lyrics_message = None
        await self._drop_card()

    async def refresh(self) -> None:
        """Repaint the now-playing card, or post it the first time."""
        view = NowPlayingView(self)
        if self.message is not None:
            try:
                await self.message.edit(view=view)
                return
            except discord.NotFound:
                self.message = None
            except discord.HTTPException:
                logger.exception("radio could not update its card")
                return
        if self.channel_id is None:
            return
        channel = self.bot.get_channel(self.channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return
        try:
            self.message = await channel.send(view=view)
        except discord.HTTPException:
            logger.exception("radio could not post its card")

    async def teardown(self) -> None:
        await self._stop_lyrics_task()
        task = self._prefetch_task
        self._prefetch_task = None
        if task is not None and not task.done():
            task.cancel()
        voice = self.voice
        if voice is not None:
            try:
                if _is_active(voice):
                    voice.stop()
                if voice.is_connected():
                    await voice.disconnect(force=True)
            except Exception:
                logger.exception("radio could not leave the voice channel")
        self.voice = None
        self.current = None
        self.lyrics = None
        self.queue.clear()
        self.history.clear()
        self.future.clear()
        self.filter = "off"
        self.came_from = None
        self.persistent = False
        self._autoplay_urls.clear()
        self.paused = False
        self._position_base = 0.0
        self._started_at = None
        if self.lyrics_message is not None:
            try:
                await self.lyrics_message.delete()
            except discord.HTTPException:
                pass
            self.lyrics_message = None
        if self.message is not None:
            try:
                await self.message.edit(
                    view=panel(
                        discord.ui.TextDisplay(
                            f"# {radio_mark(self.bot)} Radio signed off\n"
                            "-# Queue cleared and left the channel."
                        )
                    )
                )
            except discord.HTTPException:
                pass
            self.message = None


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------
class PlayerButton(discord.ui.Button):
    """One transport action, wired straight to the player."""

    def __init__(
        self,
        player: StationPlayer,
        action: str,
        *,
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
        await self.player.cog.handle_action(self.player, self.action)


class NowPlayingView(SimpleLayout):
    """The station card: cover, credit, play clock and the transport rows."""

    def __init__(self, player: StationPlayer) -> None:
        self.player = player
        super().__init__(container(*self._body(), colour=self._colour()))

    def _colour(self) -> int:
        if self.player.paused:
            return EMBED_COLORS.get("warning", EMBED_COLOR)
        if _is_playing(self.player.voice):
            return EMBED_COLORS.get("success", EMBED_COLOR)
        return EMBED_COLOR

    def _body(self) -> list[discord.ui.Item]:
        player = self.player
        track = player.current
        if track is None:
            return [
                discord.ui.TextDisplay(
                    f"# {radio_mark(player.bot)} Radio\n"
                    f"-# Nothing playing. `{PREFIX}radio <song or link>` starts it."
                ),
                divider(),
                discord.ui.ActionRow(*self._rows()[0]),
            ]

        credit = track.credit
        if track.source_url:
            credit = f"[{track.credit}]({track.source_url})"
        header = (
            f"# {radio_mark(player.bot)} | Now Playing:\n"
            f"{credit}\n"
            f"Duration: {track.duration_text}"
        )
        if track.requester_id is not None:
            header += f"\nRecommended by <@{track.requester_id}>"
        previous = player.came_from
        if previous is not None and previous is not track:
            header += f"\n-# Finished {previous.credit}"
        preset = filter_presets().get(player.filter)
        if preset and preset[1]:
            header += f"\n-# Filter: {preset[0]}"

        items: list[discord.ui.Item] = []
        if track.artwork:
            items.append(
                discord.ui.Section(
                    discord.ui.TextDisplay(header),
                    accessory=discord.ui.Thumbnail(media=track.artwork),
                )
            )
        else:
            items.append(discord.ui.TextDisplay(header))

        for row in self._rows():
            items.append(discord.ui.ActionRow(*row))
        flags = self._flags()
        if flags:
            items.append(divider())
            items.append(discord.ui.TextDisplay(f"-# {flags}"))
        return items

    def _flags(self) -> str:
        """The station's switches, shown only when at least one is on."""
        player = self.player
        flags = []
        if player.karaoke:
            flags.append("🎤 karaoke")
        if player.autoplay:
            flags.append("🔁 autoplay")
        if player.loop:
            flags.append("🔂 loop")
        if player.shuffle:
            flags.append("🔀 shuffle")
        return " · ".join(flags)

    def _rows(self) -> list[list[discord.ui.Item]]:
        player = self.player
        playing = _is_playing(player.voice)
        paused = _is_paused(player.voice)
        active = playing or paused
        return [
            [
                PlayerButton(
                    player,
                    "previous",
                    emoji="⏮️",
                    style=discord.ButtonStyle.secondary,
                    disabled=not player.history,
                ),
                PlayerButton(
                    player,
                    "toggle",
                    emoji="⏸️" if playing else "▶️",
                    style=discord.ButtonStyle.success if not playing else discord.ButtonStyle.secondary,
                    disabled=not active and player.current is None,
                ),
                PlayerButton(
                    player,
                    "skip",
                    emoji="⏭️",
                    style=discord.ButtonStyle.secondary,
                    disabled=not active,
                ),
                PlayerButton(
                    player,
                    "stop",
                    emoji="⏹️",
                    style=discord.ButtonStyle.danger,
                ),
            ],
            [
                PlayerButton(
                    player,
                    "autoplay",
                    emoji="🔁",
                    style=discord.ButtonStyle.success if player.autoplay else discord.ButtonStyle.secondary,
                ),
                PlayerButton(
                    player,
                    "shuffle",
                    emoji="🔀",
                    style=discord.ButtonStyle.success if player.shuffle else discord.ButtonStyle.secondary,
                ),
                PlayerButton(
                    player,
                    "queue",
                    emoji="📋",
                    style=discord.ButtonStyle.secondary,
                ),
                PlayerButton(
                    player,
                    "lyrics",
                    emoji="📜",
                    style=discord.ButtonStyle.success if player.lyrics_message else discord.ButtonStyle.secondary,
                ),
                PlayerButton(
                    player,
                    "karaoke",
                    emoji="🎤",
                    style=discord.ButtonStyle.success if player.karaoke else discord.ButtonStyle.secondary,
                    disabled=player.current is None,
                ),
            ],
        ]


class LyricsView(SimpleLayout):
    """The timed-lyrics card, with the line being sung set in bold."""

    def __init__(self, player: StationPlayer) -> None:
        self.player = player
        super().__init__(container(*self._body()))

    def _body(self) -> list[discord.ui.Item]:
        player = self.player
        track = player.current
        lyrics = player.lyrics
        if track is None or lyrics is None:
            return [
                discord.ui.TextDisplay(
                    "# 📜 Lyrics\n-# Nothing playing, or no lyrics were found for this track."
                )
            ]

        position = player.position()
        header = (
            f"# 📜 {track.title}\n"
            f"-# {track.artist or 'Unknown artist'}\n"
            f"`{radio.clock(position)} {radio.progress_bar(position, track.duration)} "
            f"{radio.clock(track.duration)}`"
        )
        items: list[discord.ui.Item] = [discord.ui.TextDisplay(header), divider()]

        if lyrics.is_synced:
            items.append(discord.ui.TextDisplay(self._synced_block(lyrics, position)))
        else:
            plain = lyrics.plain.strip() or "No lyrics found."
            items.append(discord.ui.TextDisplay(plain[:3900]))
            items.append(
                discord.ui.TextDisplay("-# No timed lyrics for this track — showing plain text.")
            )
        return items

    @staticmethod
    def _synced_block(lyrics: radio.Lyrics, position: float) -> str:
        lines = lyrics.synced
        current = radio.line_index(lines, position)
        half = LYRIC_WINDOW // 2
        start = max(0, min(current - half, max(0, len(lines) - LYRIC_WINDOW)))
        window = lines[start:start + LYRIC_WINDOW]
        rendered: list[str] = []
        for offset, (_, text) in enumerate(window):
            index = start + offset
            line = text or "♪"
            rendered.append(f"**{line}**" if index == current else f"-# {line}")
        return "\n".join(rendered)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------
class Station(commands.Cog):
    """Radio: play any song, with live timed lyrics."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.players: dict[int, StationPlayer] = {}
        self.session = aiohttp.ClientSession()
        self._lyrics_cache: dict[tuple[str, str], radio.Lyrics | None] = {}
        #: (channel id, user id) -> the numbered results they were last shown, so
        #: the next bare number they send can be turned into a request.
        self._pending_searches: dict[tuple[int, int], dict[str, Any]] = {}

    async def cog_unload(self) -> None:
        for player in list(self.players.values()):
            await player.teardown()
        self.players.clear()
        await self.session.close()

    # -- helpers ---------------------------------------------------------
    def player_for(self, guild_id: int, *, create: bool = False) -> StationPlayer | None:
        player = self.players.get(int(guild_id))
        if player is None and create:
            player = StationPlayer(self, int(guild_id))
            self.players[int(guild_id)] = player
        return player

    def _voice_channel(self, ctx: commands.Context) -> discord.VoiceChannel | None:
        state = getattr(ctx.author, "voice", None)
        channel = getattr(state, "channel", None)
        return channel if isinstance(channel, discord.VoiceChannel) else None

    # -- who may drive the station ----------------------------------------
    def _station_settings(self):
        return getattr(self.bot, "dash_settings", None)

    def dj_role_id(self, guild_id: int) -> int | None:
        """The role allowed to drive the station, or ``None`` for everyone."""
        return dj_role_id_for(self._station_settings(), guild_id)

    def is_dj(self, user: discord.abc.User | None, guild_id: int) -> bool:
        """A DJ: the configured role, a server manager, or the bot's own staff."""
        if user is None:
            return False
        user_id = getattr(user, "id", None)
        if user_id is not None and user_id in set(
            getattr(self.bot, "bot_admin_ids", ()) or ()
        ):
            return True
        if user_id is not None and user_id in (getattr(self.bot, "owner_ids", None) or ()):
            return True
        permissions = getattr(user, "guild_permissions", None)
        if permissions is not None and (
            permissions.administrator or permissions.manage_guild
        ):
            return True
        role_id = self.dj_role_id(guild_id)
        if role_id is None:
            return False
        return any(
            getattr(role, "id", None) == role_id
            for role in (getattr(user, "roles", None) or ())
        )

    def may_control(
        self, user: discord.abc.User | None, player: "StationPlayer | None" = None
    ) -> bool:
        """Whether this member is allowed to drive the station.

        With a DJ role configured, only a DJ gets through. Without one the
        station behaves the way it always has: whoever started it, or anybody
        sitting in its voice channel, can work the controls.
        """
        guild_id = getattr(player, "guild_id", None)
        if guild_id is None:
            guild = getattr(user, "guild", None)
            guild_id = getattr(guild, "id", 0)
        guild_id = int(guild_id or 0)
        if self.is_dj(user, guild_id):
            return True
        if self.dj_role_id(guild_id) is not None:
            return False
        return player is None or player.in_channel(user)

    async def ensure_control(
        self, ctx: commands.Context, player: "StationPlayer | None"
    ) -> bool:
        """Guard a transport command. ``False``, with a reply, when refused."""
        if player is None:
            await ctx.send(view=notice("The station isn't running."))
            return False
        if self.may_control(ctx.author, player):
            return True
        role_id = self.dj_role_id(ctx.guild.id)
        if role_id is not None:
            message = (
                f"Only <@&{role_id}> can drive the station. A DJ can hand the "
                f"controls over with `{PREFIX}dj @role`."
            )
        else:
            message = "Join the station's voice channel to drive it."
        await ctx.send(view=notice(message))
        return False

    async def _toggle_karaoke(
        self, player: "StationPlayer", channel: discord.abc.Messageable | None = None
    ) -> SimpleLayout:
        """Flip karaoke and describe what happened, for button and command alike."""
        if channel is None and player.channel_id is not None:
            candidate = self.bot.get_channel(player.channel_id)
            channel = candidate if isinstance(candidate, discord.abc.Messageable) else None

        on = await player.toggle_karaoke()
        if on is None:
            card = notice(
                "I couldn't build a karaoke mix for that track — ffmpeg failed "
                "while rendering it.",
                emoji="🎤",
            )
        elif on:
            card = panel(
                discord.ui.TextDisplay(
                    "# 🎤 Karaoke on\n"
                    "Vocals pulled down and the backing track lifted. It works "
                    "best on songs with a centred lead vocal."
                )
            )
        else:
            card = panel(
                discord.ui.TextDisplay("# 🎙️ Karaoke off\n-# Back to the original mix.")
            )
        if channel is not None:
            try:
                await channel.send(view=card, delete_after=8)
            except discord.HTTPException:
                logger.debug("could not report the karaoke toggle", exc_info=True)
        return card

    async def _toggle_lyrics(
        self, player: "StationPlayer", channel: discord.abc.Messageable
    ) -> None:
        """Show, hide or explain the lyric card — the same answer either way in."""
        if player.current is None:
            await channel.send(view=notice("Nothing is playing right now."))
            return
        if player.lyrics is None:
            await channel.send(
                view=notice(
                    f"No lyrics found for **{player.current.title}**.", emoji="📜"
                )
            )
            return
        await player.show_lyrics(channel)

    def _card_channel(
        self, guild: discord.Guild, voice_channel: discord.VoiceChannel
    ) -> discord.abc.Messageable | None:
        """Where a parked station's card goes.

        The configured request channel when there is one, since that is where
        people already type song names; otherwise the voice channel's own text
        chat, so the controls stay an arm's length from the music.
        """
        settings = self._station_settings()
        text_id = settings.radio_channel_id(guild.id) if settings is not None else None
        if text_id is not None:
            channel = guild.get_channel(text_id)
            if isinstance(channel, discord.abc.Messageable):
                return channel
        if isinstance(voice_channel, discord.abc.Messageable):
            return voice_channel
        return None

    async def _seed_track(self) -> radio.Track | None:
        """Something to open a parked station with when nothing is queued.

        The mixtapes are the music already sitting on this bot, so one of them
        makes a sensible first record; autoplay takes it from there.
        """
        library = getattr(self.bot, "mixtapes", None)
        songs = list(getattr(library, "songs", []) or [])
        if not songs:
            return None
        random.shuffle(songs)
        for song in songs[:5]:
            try:
                track = await radio.resolve(self.session, song.title)
            except Exception:
                logger.debug("radio seed resolve failed for %r", song.title, exc_info=True)
                continue
            if track is not None and track.path is not None:
                return track
        return None

    async def _bring_station_up(
        self, guild: discord.Guild, voice_channel: discord.VoiceChannel
    ) -> bool:
        """Park the station in ``voice_channel`` and leave it playing forever.

        Shared by ``!radio setvc`` and the startup restore, so the two can
        never disagree about how the 24/7 station comes up. ``False`` means the
        bot couldn't join, which callers report rather than retry.
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
        player.persistent = True
        # A parked station is a station that never stops, so autoplay is the
        # whole point of parking it.
        player.autoplay = True
        if not await player.connect(voice_channel, None):
            player.persistent = False
            return False

        card = self._card_channel(guild, voice_channel)
        if card is not None:
            player.channel_id = card.id
        else:
            logger.warning("No text channel to draw the station card in")

        if not already_playing:
            player.queue.clear()
            player.history.clear()
            player.future.clear()
            await player.advance()
            if player.current is None:
                seed = await self._seed_track()
                if seed is not None:
                    await player.play(seed)
        return True

    async def _restore_stations(self) -> None:
        """Bring every parked 24/7 station back up. Safe to call again."""
        settings = self._station_settings()
        if settings is None:
            return
        for guild in list(self.bot.guilds):
            voice_id = parked_voice_id(settings, guild.id)
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
                logger.warning("Could not park the station in %s", channel.id)

    async def cog_load(self) -> None:
        """Park the 24/7 stations again when the cog loads onto a live bot."""
        if self.bot.is_ready():
            await self._restore_stations()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """And on every connect, which is when the channel cache is populated."""
        await self._restore_stations()

    async def load_lyrics(self, track: radio.Track) -> None:
        """Fetch and memoize lyrics for a track, best-effort."""
        key = (track.title.strip().casefold(), track.artist.strip().casefold())
        if key in self._lyrics_cache:
            return
        try:
            self._lyrics_cache[key] = await radio.fetch_lyrics(
                self.session,
                title=track.title,
                artist=track.artist,
                duration=track.duration,
            )
        except Exception:
            logger.debug("radio could not fetch lyrics", exc_info=True)
            self._lyrics_cache[key] = None

    def lyrics_for(self, track: radio.Track) -> radio.Lyrics | None:
        key = (track.title.strip().casefold(), track.artist.strip().casefold())
        return self._lyrics_cache.get(key)

    async def autoplay_track(self, player: StationPlayer) -> radio.Track | None:
        """Pick the next track to keep the 24/7 station rolling.

        The same artist comes first, on SoundCloud where underground catalogues
        live; when that is thin the net widens to YouTube — a mix around the
        artist, then a beat tape — so even a small artist never runs the queue
        dry. Anything already queued or already played is skipped, the seed's
        own title is never repeated back, and a candidate that fails to
        download just falls through to the next one.
        """
        last = player.current
        if last is None:
            return None
        seed = (last.artist or last.title).strip()
        if not seed:
            return None

        avoid = {track.key for track in player.queue} | set(player._autoplay_urls)
        avoid.add(last.key)
        title = last.title.strip().casefold()

        # The song API first — whole songs from a catalogue — then SoundCloud,
        # then a YouTube mix around the artist, so even a small artist keeps the
        # station rolling.
        pool: list[radio.Track] = []
        try:
            pool.extend(await radio.search_catalogue(self.session, seed, limit=15))
        except Exception:
            logger.debug("radio autoplay catalogue search failed", exc_info=True)
        for query, source in (
            (seed, "soundcloud"),
            (f"{last.title} {last.artist}".strip(), "soundcloud"),
            (f"{seed} mix", "youtube"),
            (f"{seed} type beat", "youtube"),
        ):
            if len(pool) >= 5:
                break
            try:
                pool.extend(
                    await radio.search(self.session, query, limit=15, source=source)
                )
            except Exception:
                logger.debug("radio autoplay search failed for %r", query, exc_info=True)

        candidates = [
            track
            for track in pool
            if track.key not in avoid and track.title.strip().casefold() != title
        ]
        random.shuffle(candidates)
        for track in candidates:
            try:
                downloaded = await radio.download(self.session, track)
            except Exception as error:
                logger.info("radio autoplay skipped %r: %s", track.credit, error)
                continue
            if downloaded is not None and downloaded.path is not None:
                player._autoplay_urls.add(downloaded.key)
                return downloaded
        return None

    async def handle_action(self, player: StationPlayer, action: str) -> None:
        if action == "previous":
            await player.previous()
        elif action == "skip":
            await player.skip()
        elif action == "toggle":
            if _is_playing(player.voice):
                await player.pause()
            else:
                await player.resume()
        elif action == "stop":
            await player.teardown()
            self.players.pop(player.guild_id, None)
        elif action == "autoplay":
            player.autoplay = not player.autoplay
            if player.autoplay:
                player._schedule_prefetch()
            await player.refresh()
        elif action == "karaoke":
            await self._toggle_karaoke(player)
        elif action == "shuffle":
            player.shuffle = not player.shuffle
            await player.refresh()
        elif action == "lyrics":
            channel = self.bot.get_channel(player.channel_id) if player.channel_id else None
            if isinstance(channel, discord.abc.Messageable):
                await self._toggle_lyrics(player, channel)
            await player.refresh()
        elif action == "queue":
            channel = self.bot.get_channel(player.channel_id) if player.channel_id else None
            if isinstance(channel, discord.abc.Messageable):
                await channel.send(view=queue_card(player))

    async def _play_request(self, ctx: commands.Context, query: str) -> None:
        query = (query or "").strip()
        if not query:
            player = self.player_for(ctx.guild.id)
            if player is not None and player.current is not None:
                player.channel_id = ctx.channel.id
                await player._drop_card()
                await player.refresh()
                return
            await ctx.send(
                view=notice(
                    f"Give me a song or a link: `{PREFIX}radio through da storm`."
                )
            )
            return

        # Both checks come before resolving rather than after, so someone who
        # may not queue a song isn't made to wait on a download to be refused.
        player = self.player_for(ctx.guild.id)
        if not self.may_control(ctx.author, player):
            role_id = self.dj_role_id(ctx.guild.id)
            await ctx.send(
                view=notice(
                    f"Only <@&{role_id}> can queue songs for the station."
                    if role_id
                    else "Join the station's voice channel to queue songs."
                )
            )
            return
        if player is None and self._voice_channel(ctx) is None:
            await ctx.send(
                view=notice("Join a voice channel first, then start the station.")
            )
            return

        if radio.playlist_source(query) is not None:
            await self._play_playlist(ctx, query)
            return

        loading = await ctx.send(
            view=panel(discord.ui.TextDisplay(f"🔎 Searching for **{query}**…"))
        )
        try:
            track = await radio.resolve(
                self.session, query, requester_id=ctx.author.id
            )
        except Exception:
            logger.exception("radio resolve crashed")
            track = None

        if track is None:
            await loading.edit(
                view=notice(
                    f"Couldn't grab **{query}** from cobalt, yt-dlp or the tracks API."
                )
            )
            return

        await self._start_or_queue(ctx, track, loading=loading)

    async def _play_playlist(self, ctx: commands.Context, query: str) -> None:
        """Queue everything a pasted playlist holds, playing the first track.

        Spotify playlists are read from their public embed page, YouTube
        playlists and SoundCloud sets through yt-dlp; only the names come back
        at this point, so a long playlist costs no downloads until it plays.
        """
        loading = await ctx.send(
            view=panel(
                discord.ui.TextDisplay(f"🔎 Reading the playlist **{query}**…")
            )
        )
        try:
            name, tracks = await radio.resolve_playlist(
                self.session, query, requester_id=ctx.author.id
            )
        except Exception:
            logger.exception("radio could not read a playlist")
            name, tracks = "", []
        if not tracks:
            await loading.edit(
                view=notice(
                    "I couldn't read that playlist. Spotify playlists have to "
                    "be public, and the link has to point at the playlist "
                    "itself."
                )
            )
            return

        player = self.player_for(ctx.guild.id, create=True)
        assert player is not None
        on_air = _is_active(player.voice) or player.current is not None
        if not on_air:
            channel = self._voice_channel(ctx)
            if channel is None:
                await loading.edit(
                    view=notice("Join a voice channel first, then start the station.")
                )
                return
            if not await player.connect(channel, ctx.author.id):
                await loading.edit(view=notice("I couldn't join that voice channel."))
                return

        player.channel_id = ctx.channel.id
        first = tracks[0]
        if on_air:
            player.queue.extend(tracks)
            player._schedule_prefetch()
            await loading.edit(view=queued_playlist_card(name, tracks, 0))
            await player.refresh()
            return

        if not await player.play(first):
            await loading.edit(view=notice(f"I couldn't play **{first.title}**."))
            return
        player.queue.extend(tracks[1:])
        player._schedule_prefetch()
        await loading.edit(view=queued_playlist_card(name, tracks, len(tracks) - 1))

    async def _start_or_queue(
        self,
        ctx: commands.Context,
        track: radio.Track,
        *,
        loading: discord.Message | None = None,
    ) -> bool:
        """Give a resolved track to the station: queue it, or start it up.

        Shared by ``!radio <song or link>`` and the number that answers a
        ``!radio search``, so a number in chat behaves exactly like the
        request typed out by hand — including joining the voice channel when the
        station is not on air yet.
        """

        async def report(view: SimpleLayout) -> None:
            """Update the loading card, or post the news if there isn't one."""
            if loading is not None:
                try:
                    await loading.edit(view=view)
                    return
                except discord.HTTPException:
                    logger.debug("radio could not edit its loading card", exc_info=True)
            try:
                await ctx.send(view=view)
            except discord.HTTPException:
                logger.debug("radio could not report back", exc_info=True)

        player = self.player_for(ctx.guild.id)
        on_air = player is not None and (
            _is_active(player.voice) or player.current is not None
        )

        if not on_air:
            channel = self._voice_channel(ctx)
            if channel is None:
                await report(
                    notice("Join a voice channel first, then start the station.")
                )
                return False
            player = self.player_for(ctx.guild.id, create=True)
            assert player is not None
            if not await player.connect(channel, ctx.author.id):
                await report(notice("I couldn't join that voice channel."))
                return False

        assert player is not None
        player.channel_id = ctx.channel.id

        if on_air:
            position = len(player.queue) + 1
            player.queue.append(track)
            player._schedule_prefetch()
            await report(queued_card(track, position, len(player.queue)))
            await player.refresh()
            return True

        if await player.play(track):
            if loading is not None:
                try:
                    await loading.delete()
                except discord.HTTPException:
                    pass
            return True

        await report(notice(f"I couldn't play **{track.title}**."))
        return False

    # -- commands --------------------------------------------------------
    @commands.group(
        name="radio",
        aliases=["wip", "wipfm", "wipradio"],
        invoke_without_command=True,
    )
    @commands.guild_only()
    async def wipradio(self, ctx: commands.Context, *, query: str = "") -> None:
        f"""Play any song or link on the station.

        Usage: `{PREFIX}radio <song or link>` — joins you and plays it, or
        queues it when something is already on."""
        await self._play_request(ctx, query)

    @wipradio.command(name="play", aliases=["add", "request"])
    @commands.guild_only()
    async def wipradio_play(self, ctx: commands.Context, *, query: str) -> None:
        """Play or queue a song by name or link."""
        await self._play_request(ctx, query)

    @wipradio.command(name="skip", aliases=["next"])
    @commands.guild_only()
    async def wipradio_skip(self, ctx: commands.Context) -> None:
        """Skip to the next track."""
        player = self.player_for(ctx.guild.id)
        if not await self.ensure_control(ctx, player):
            return
        assert player is not None
        if not await player.skip():
            await ctx.send(view=notice("Nothing is playing right now."))

    @wipradio.command(name="previous", aliases=["prev", "back"])
    @commands.guild_only()
    async def wipradio_previous(self, ctx: commands.Context) -> None:
        """Go back to the last track."""
        player = self.player_for(ctx.guild.id)
        if not await self.ensure_control(ctx, player):
            return
        assert player is not None
        if not await player.previous():
            await ctx.send(view=notice("There's nothing to go back to yet."))

    @wipradio.command(name="pause")
    @commands.guild_only()
    async def wipradio_pause(self, ctx: commands.Context) -> None:
        """Pause the current track."""
        player = self.player_for(ctx.guild.id)
        if not await self.ensure_control(ctx, player):
            return
        assert player is not None
        if not await player.pause():
            await ctx.send(view=notice("Nothing is playing right now."))

    @wipradio.command(name="resume", aliases=["unpause"])
    @commands.guild_only()
    async def wipradio_resume(self, ctx: commands.Context) -> None:
        """Resume, or start the next track."""
        player = self.player_for(ctx.guild.id)
        if not await self.ensure_control(ctx, player):
            return
        assert player is not None
        await player.resume()

    @wipradio.command(name="stop", aliases=["leave", "off", "disconnect"])
    @commands.guild_only()
    async def wipradio_stop(self, ctx: commands.Context) -> None:
        """Clear the queue and leave the voice channel."""
        player = self.player_for(ctx.guild.id)
        if not await self.ensure_control(ctx, player):
            return
        assert player is not None
        # Stopping a parked station also unpark it, or it would walk straight
        # back into the voice channel on the next restart.
        settings = self._station_settings()
        if parked_voice_id(settings, ctx.guild.id) is not None:
            await store_parked_voice(settings, ctx.guild.id, None)
        await player.teardown()
        self.players.pop(int(ctx.guild.id), None)

    @commands.command(name="dj", aliases=["setdj", "djrole"])
    @commands.guild_only()
    @commands.check_any(commands.is_owner(), commands.has_permissions(manage_guild=True))
    async def dj(self, ctx: commands.Context, *, role: str | None = None) -> None:
        """Set, show or clear the role that may drive the station.

        Usage: `{PREFIX}dj @DJ` — from then on only that role, the server's
        managers and the bot's staff can play songs, skip, change the volume or
        touch autoplay, karaoke and the rest. `{PREFIX}dj off` hands the
        controls back to whoever is in the station's voice channel.
        """
        settings = self._station_settings()

        if role is None:
            current = self.dj_role_id(ctx.guild.id)
            await ctx.send(
                view=panel(
                    discord.ui.TextDisplay(
                        f"# 🎧 DJ role\n"
                        + (
                            f"<@&{current}> may drive the station.\n-# "
                            f"`{PREFIX}dj off` lets anyone in the voice channel "
                            "control it again."
                            if current
                            else "Nobody is set as DJ, so anyone in the "
                            f"station's voice channel can control it.\n-# "
                            f"`{PREFIX}dj @role` changes that."
                        )
                    )
                )
            )
            return

        if role.strip().lower() in {"off", "none", "clear", "disable", "reset"}:
            await store_dj_role(settings, ctx.guild.id, None)
            await ctx.send(
                view=panel(
                    discord.ui.TextDisplay(
                        "# 🎧 DJ role cleared\n"
                        "-# Anyone in the station's voice channel can control it "
                        "again."
                    )
                )
            )
            return

        try:
            target = await commands.RoleConverter().convert(ctx, role)
        except commands.BadArgument:
            await ctx.send(view=notice("That isn't a role I can see."))
            return

        await store_dj_role(settings, ctx.guild.id, target.id)
        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    f"# 🎧 DJ role set\n"
                    f"> **{target.name}**\n\n"
                    "From now on only that role (and the server's managers) can "
                    "play songs, skip, pause, change the volume, or switch "
                    "autoplay, shuffle and karaoke.\n"
                    f"-# `{PREFIX}dj off` hands the controls back to the channel."
                )
            )
        )

    @wipradio.command(name="setvc", aliases=["stationvc", "park"])
    @commands.guild_only()
    @commands.check_any(commands.is_owner(), commands.has_permissions(manage_guild=True))
    async def wipradio_setvc(self, ctx: commands.Context, *, channel: str | None = None) -> None:
        """Park the station in a voice channel, 24/7.

        Usage: `!radio setvc #radio` — the bot joins that channel now and
        again on every start, and with autoplay on it never runs out of music.
        Anyone in the channel can drive its card. `!radio setvc off` releases
        it again.
        """
        settings = self._station_settings()

        if channel is not None and channel.strip().lower() in {
            "off", "none", "clear", "disable", "stop",
        }:
            await store_parked_voice(settings, ctx.guild.id, None)
            player = self.player_for(ctx.guild.id)
            if player is not None:
                await player.teardown()
                self.players.pop(int(ctx.guild.id), None)
            await ctx.send(
                view=panel(
                    discord.ui.TextDisplay(
                        f"# {radio_mark(self.bot)} Station released\n"
                        "-# It won't rejoin on its own any more."
                    )
                ),
                delete_after=5,
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
            target = self._voice_channel(ctx)
            if target is None:
                await ctx.send(
                    view=notice("Join a voice channel first, or name the one to park in.")
                )
                return

        if not target.permissions_for(ctx.guild.me).connect:
            await ctx.send(view=notice(f"I'm not allowed to join **{target.name}**."))
            return

        await store_parked_voice(settings, ctx.guild.id, target.id)

        if not await self._bring_station_up(ctx.guild, target):
            await ctx.send(view=notice("I couldn't join that voice channel."))
            return

        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    f"# {radio_mark(self.bot)} Station parked\n"
                    f"> **{target.mention}**\n\n"
                    "Autoplay is on and stays on: when the queue runs dry I'll "
                    "find more from the same artist, then widen the search so "
                    "the music never stops.\n"
                    f"-# Anyone in the channel can drive the card · "
                    f"`{PREFIX}radio setvc off` releases it."
                )
            ),
            delete_after=6,
        )

    @wipradio.command(name="loop", aliases=["repeat"])
    @commands.guild_only()
    async def wipradio_loop(self, ctx: commands.Context) -> None:
        """Repeat the current track instead of moving on."""
        player = self.player_for(ctx.guild.id)
        if not await self.ensure_control(ctx, player):
            return
        assert player is not None
        player.loop = not player.loop
        await player.refresh()
        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    f"# {'🔂 Looping' if player.loop else '➡️ Looping off'}"
                )
            ),
            delete_after=5,
        )

    @wipradio.command(name="shuffle")
    @commands.guild_only()
    async def wipradio_shuffle(self, ctx: commands.Context) -> None:
        """Play the queue out of order."""
        player = self.player_for(ctx.guild.id)
        if not await self.ensure_control(ctx, player):
            return
        assert player is not None
        player.shuffle = not player.shuffle
        await player.refresh()
        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    f"# {'🔀 Shuffle on' if player.shuffle else '➡️ Shuffle off'}"
                )
            ),
            delete_after=5,
        )

    @wipradio.command(name="autoplay", aliases=["radio"])
    @commands.guild_only()
    async def wipradio_autoplay(self, ctx: commands.Context) -> None:
        """Keep going with the same artist when the queue runs dry."""
        player = self.player_for(ctx.guild.id)
        if not await self.ensure_control(ctx, player):
            return
        assert player is not None
        player.autoplay = not player.autoplay
        if player.autoplay:
            player._schedule_prefetch()
        await player.refresh()
        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(
                    f"# {'🔁 Autoplay on' if player.autoplay else '➡️ Autoplay off'}"
                )
            ),
            delete_after=5,
        )

    @wipradio.command(name="karaoke", aliases=["instrumental", "vocals"])
    @commands.guild_only()
    async def wipradio_karaoke(self, ctx: commands.Context) -> None:
        """Suppress the vocals and push the instrumental up.

        The current track restarts where it was, so nobody loses their place.
        """
        player = self.player_for(ctx.guild.id)
        if not await self.ensure_control(ctx, player):
            return
        assert player is not None
        # The button runs the same helper, so both report the same way.
        await self._toggle_karaoke(player, ctx.channel)

    @wipradio.command(name="filter", aliases=["eq", "filters"])
    @commands.guild_only()
    async def wipradio_filter(self, ctx: commands.Context, *, name: str = "") -> None:
        """Put an EQ preset in front of the music, or list the presets.

        Usage: `{prefix}radio filter bass` — the current track restarts where it
        was, so the change is audible straight away. `{prefix}radio filter off`
        clears it.
        """
        player = self.player_for(ctx.guild.id)
        if not await self.ensure_control(ctx, player):
            return
        assert player is not None

        presets = filter_presets()
        key = name.strip().lower()
        if key in {"none", "clear", "default", "reset"}:
            key = "off"
        if not presets:
            # A reloaded cog in a process whose radio module predates the
            # presets: say so instead of pretending the filter worked.
            await ctx.send(
                view=notice(
                    "The filters need a bot restart — they live in a module this "
                    "process loaded before they existed."
                )
            )
            return
        if not key:
            active = (presets.get(player.filter) or ("Off", ""))[0]
            listing = "\n".join(
                f"`{preset}` · {label}`" + ("" if preset != player.filter else " ← on")
                for preset, (label, _chain) in presets.items()
            )
            await ctx.send(
                view=panel(
                    discord.ui.TextDisplay(
                        f"# 🎚️ Filters\n-# Currently **{active}**\n\n{listing}\n\n"
                        f"-# `{PREFIX}radio filter <name>` switches, `{PREFIX}radio filter off` clears."
                    )
                )
            )
            return
        if key not in presets:
            await ctx.send(
                view=notice(
                    f"`{name}` isn't one of my filters — try "
                    + ", ".join(f"`{preset}`" for preset in presets)
                    + "."
                )
            )
            return

        await player.set_filter(key)
        await ctx.send(
            view=panel(discord.ui.TextDisplay(f"# 🎚️ Filter: {presets[key][0]}")),
            delete_after=5,
        )

    @wipradio.command(name="volume", aliases=["vol"])
    @commands.guild_only()
    async def wipradio_volume(self, ctx: commands.Context, percent: int) -> None:
        """Set the output level, 0–200."""
        player = self.player_for(ctx.guild.id)
        if not await self.ensure_control(ctx, player):
            return
        assert player is not None
        level = await player.set_volume(int(percent))
        await ctx.send(
            view=panel(discord.ui.TextDisplay(f"🔊 Volume set to **{int(level * 100)}%**")),
            delete_after=5,
        )

    @wipradio.command(name="queue", aliases=["q", "list"])
    @commands.guild_only()
    async def wipradio_queue(self, ctx: commands.Context, *, query: str = "") -> None:
        """Show the queue, or add a song to it."""
        if query.strip():
            await self._play_request(ctx, query)
            return
        player = self.player_for(ctx.guild.id)
        if player is None:
            await ctx.send(view=notice("The station isn't running."))
            return
        await ctx.send(view=queue_card(player))

    @wipradio.command(name="remove", aliases=["drop"])
    @commands.guild_only()
    async def wipradio_remove(self, ctx: commands.Context, position: int) -> None:
        """Drop one entry from the queue by its number."""
        player = self.player_for(ctx.guild.id)
        if not await self.ensure_control(ctx, player):
            return
        assert player is not None
        if not player.queue:
            await ctx.send(view=notice("The queue is empty."))
            return
        if not 1 <= position <= len(player.queue):
            await ctx.send(view=notice(f"Pick a number between 1 and {len(player.queue)}."))
            return
        removed = player.queue.pop(position - 1)
        await player.refresh()
        await ctx.send(
            view=panel(discord.ui.TextDisplay(f"🗑️ Removed **{removed.credit}** from the queue.")),
            delete_after=5,
        )

    @wipradio.command(name="clear", aliases=["wipe"])
    @commands.guild_only()
    async def wipradio_clear(self, ctx: commands.Context) -> None:
        """Empty the queue without stopping what's playing."""
        player = self.player_for(ctx.guild.id)
        if not await self.ensure_control(ctx, player):
            return
        assert player is not None
        player.queue.clear()
        await player.refresh()
        await ctx.send(
            view=panel(discord.ui.TextDisplay("# 🧹 Queue cleared")),
            delete_after=5,
        )

    @wipradio.command(name="now", aliases=["playing", "current"])
    @commands.guild_only()
    async def wipradio_now(self, ctx: commands.Context) -> None:
        """Repost the now-playing card.

        Looking is free: the card's own buttons still ask for the DJ role.
        """
        player = self.player_for(ctx.guild.id)
        if player is None or player.current is None:
            await ctx.send(view=notice("Nothing is playing right now."))
            return
        player.channel_id = ctx.channel.id
        await player._drop_card()
        await player.refresh()

    @wipradio.command(name="lyrics", aliases=["ly", "lrc"])
    @commands.guild_only()
    async def wipradio_lyrics(self, ctx: commands.Context) -> None:
        """Post (or remove) the live, timed-lyrics card."""
        player = self.player_for(ctx.guild.id)
        if player is None:
            await ctx.send(view=notice("The station isn't running."))
            return
        player.channel_id = ctx.channel.id
        await self._toggle_lyrics(player, ctx.channel)

    @wipradio.command(name="search")
    @commands.guild_only()
    async def wipradio_search(self, ctx: commands.Context, *, query: str) -> None:
        """Search SoundCloud, then queue whichever number you send back.

        Usage: `!radio search polo g` — the list is remembered for a couple
        of minutes, and the next message you send as a bare number queues that
        track.
        """
        # The song API first, since its entries are whole songs; SoundCloud is
        # the fallback, and anything it hands back is checked for a preview
        # before it plays.
        results = await radio.search_catalogue(self.session, query, limit=SEARCH_LIMIT)
        source = "song API"
        if not results:
            results = await radio.search(self.session, query, limit=SEARCH_LIMIT)
            source = "SoundCloud"
        if not results:
            await ctx.send(view=notice(f"Nothing found for **{query}**."))
            return
        results = results[:SEARCH_LIMIT]
        lines = [
            f"`{index + 1}.` **{track.title}** - {track.artist or 'Unknown'} "
            f"`[ {track.duration_text} ]`"
            for index, track in enumerate(results)
        ]
        self._pending_searches[(ctx.channel.id, ctx.author.id)] = {
            "query": query,
            "results": results,
            "created": time.time(),
        }
        await ctx.send(
            view=panel(
                discord.ui.TextDisplay(f"# 🔎 Results for {query}\n" + "\n".join(lines)),
                divider(),
                discord.ui.TextDisplay(
                    f"-# From the {source} · reply in this channel with a "
                    f"number (1–{len(results)}) to queue it · {ctx.author.mention} "
                    f"· expires in {int(SEARCH_PICK_TTL / 60)} minutes"
                ),
            )
        )

    # -- events ----------------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """A bare number answers the last search that person was shown.

        Only that person's own pending list answers, so two people searching in
        the same channel never pick from each other's results, and only a
        message that is nothing but a number counts.
        """
        if message.author.bot or message.guild is None:
            return
        key = (message.channel.id, message.author.id)
        pending = self._pending_searches.get(key)
        if pending is None:
            return
        if time.time() - float(pending.get("created", 0.0)) > SEARCH_PICK_TTL:
            self._pending_searches.pop(key, None)
            return

        match = _PICK_RE.match((message.content or "").strip())
        if match is None:
            return

        results: list[radio.Track] = pending.get("results") or []
        index = int(match.group(1))
        if index < 1 or index > len(results):
            await message.reply(
                f"Pick a number between 1 and {len(results)}.", mention_author=False
            )
            return

        self._pending_searches.pop(key, None)
        ctx = await self.bot.get_context(message)
        track = results[index - 1]
        loading = await message.channel.send(
            view=panel(discord.ui.TextDisplay(f"🔎 Fetching **{track.credit}**…"))
        )
        await self._start_or_queue(ctx, track, loading=loading)


def queued_playlist_card(
    name: str, tracks: list[radio.Track], queued: int
) -> SimpleLayout:
    """The card a pasted playlist gets: what it was, and what came with it."""
    lines = [
        f"`{index + 1}.` **{track.title}** - {track.artist or 'Unknown'}"
        for index, track in enumerate(tracks[:10])
    ]
    if len(tracks) > 10:
        lines.append(f"-# +{len(tracks) - 10} more")
    body = (
        f"# 📻 {'Playing' if queued else 'Queued'} **{name}**\n"
        f"-# {len(tracks)} track{'s' if len(tracks) != 1 else ''} from the playlist"
        + (f" · {queued} waiting" if queued else "")
    )
    return panel(
        discord.ui.TextDisplay(body),
        divider(),
        discord.ui.TextDisplay("\n".join(lines)),
    )


#: Built outside the class so both the cog and the buttons can reach it.
def queued_card(track: radio.Track, position: int, total: int) -> SimpleLayout:
    credit = track.credit
    if track.source_url:
        credit = f"[{track.credit}]({track.source_url})"
    body = discord.ui.TextDisplay(
        f"# 📻 Queued at position {position}\n"
        f"{credit} `[ {track.duration_text} ]`"
    )
    items: list[discord.ui.Item] = [body]
    if track.artwork:
        # Rebuild with a thumbnail section so the queued card looks like the
        # station cards rather than a plain block of text.
        section = discord.ui.Section(
            discord.ui.TextDisplay(body.content),
            accessory=discord.ui.Thumbnail(media=track.artwork),
        )
        items = [section]
    items.append(divider())
    items.append(
        discord.ui.TextDisplay(
            "-# Not the correct track? Try being more specific, or use "
            f"`{PREFIX}radio search`."
        )
    )
    return panel(*items)


def queue_card(player: StationPlayer) -> SimpleLayout:
    items: list[discord.ui.Item] = []
    current = player.current
    if current is not None:
        items.append(discord.ui.TextDisplay(f"# 📻 Now playing\n**{current.credit}**"))
    else:
        items.append(discord.ui.TextDisplay("# 📻 Nothing playing"))
    items.append(divider())
    if player.queue:
        lines = [
            f"`{index + 1}.` **{track.title}** - {track.artist or 'Unknown'} "
            f"`[ {track.duration_text} ]`"
            for index, track in enumerate(player.queue[:20])
        ]
        if len(player.queue) > 20:
            lines.append(f"-# +{len(player.queue) - 20} more")
        items.append(discord.ui.TextDisplay("**Up next**\n" + "\n".join(lines)))
    else:
        items.append(discord.ui.TextDisplay("-# The queue is empty."))
    flags = []
    if player.loop:
        flags.append("🔂 loop")
    if player.shuffle:
        flags.append("🔀 shuffle")
    if player.autoplay:
        flags.append("🔁 autoplay")
    if player.karaoke:
        flags.append("🎤 karaoke")
    if player.persistent:
        flags.append("📻 24/7")
    if flags:
        items.append(divider())
        items.append(discord.ui.TextDisplay("-# " + " · ".join(flags)))
    return panel(*items)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Station(bot))
    logger.debug("Station cog initialized successfully")
