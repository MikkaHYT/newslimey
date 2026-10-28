# cogs/livelyrics.py
#
# ,livelyric [1:34]        -- live, timed lyrics for whatever you are playing
#                            right now, starting from the point you are at
#                            (aliases: ,livelyrics ,llyric)
#
# The song comes from the member's Spotify status when there is one, so the
# card starts at the second they are actually at and keeps the same clock as
# the song. Without a Spotify status the same ``user.getRecentTracks`` call
# behind ``,np`` is read for their most recent scrobble, and the card starts at
# the top. Juice WRLD tracks are looked up in the Juice WRLD Vault first: its
# sheets carry line timings and, where the site has aligned them, word timings.
# Everything else, and any Juice WRLD recording the vault has no timed sheet
# for, falls back to lrclib - the source behind the radio's lyric card. The
# search tolerates the tags scrobbles keep and the site's titles don't, so
# ``I'm Still (v1)`` finds ``I'm Still`` rather than a leak with the version in
# its name.
#
# Nothing is being played here, so the clock is the start point plus the time
# since the card ran: from Spotify that point is where the song is now, from
# Last.fm it is whatever ``,livelyric 1:34`` was given (zero without one). The
# card mirrors the radio's: the current line in bold with the word being sung
# underlined when the source has word timings, the lines around it muted, a
# play clock and a scrubber, and one button to stop it.

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp
import discord
from discord.ext import commands

from config import EMBED_COLOR, PREFIX
from utils.cooldown import unified_cooldown
from utils.core import juicevault
from utils.core import lyrics as lyricmatch
from utils.core import radio
from utils.core.views import SimpleLayout

logger = logging.getLogger("slimey.livelyrics")

#: How often the card checks whether the highlighted line or word moved. The
#: tick is also the edit rate: one message edit per 1.5s stays inside Discord's
#: per-channel bucket while still following word timings.
TICK = 1.5
#: How often the card is repainted even with no line change, so the play clock
#: and the scrubber keep up. Repainting every tick would be wasted edits.
REPAINT = 10.0
#: How many lines of the synced sheet are shown around the current one.
WINDOW = 9
#: The longest a card may follow a song when its length is unknown.
MAX_RUNTIME = 20 * 60
#: How long past its last line a song with no known length is treated as over.
GRACE = 30.0
#: The least time a card stays live. A matched sheet that is really a snippet,
#: or a recording whose length looks wrong, would otherwise end the card
#: seconds after it started.
MIN_RUNTIME = 45.0

LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"
LASTFM_TIMEOUT = aiohttp.ClientTimeout(total=20)

#: ``m:ss`` / ``h:mm:ss`` / ``90`` / ``1m34s`` — the shapes a start point takes.
_CLOCK = re.compile(r"^\d{1,2}:\d{1,2}(?::\d{1,2}(?:\.\d{1,3})?)?$")
_SECONDS = re.compile(r"^\d{1,4}(?:\.\d{1,3})?$")
_SUFFIXES = re.compile(r"^(?:(\d{1,2})h)?(?:(\d{1,3})m)?(?:(\d{1,3})s)$", re.IGNORECASE)
#: The longest start point that can make sense for a song.
MAX_OFFSET = 24 * 60 * 60


def parse_offset(value: str) -> float | None:
    """``1:34`` -> ``94.0``; ``90`` -> ``90.0``; ``1m34s`` -> ``94.0``.

    ``None`` means the text was not a time at all, which the command reports
    rather than silently starting at zero.
    """
    text = (value or "").strip().replace(" ", "")
    if not text:
        return 0.0
    if ":" in text:
        if not _CLOCK.match(text):
            return None
        parts = text.split(":")
        # Only the last field is seconds: ``1:75`` is a typo, not 2:15.
        if any(float(part) >= 60 for part in parts[1:]):
            return None
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + float(part)
    elif _SECONDS.match(text):
        seconds = float(text)
    else:
        match = _SUFFIXES.match(text.lower())
        if match is None:
            return None
        hours, minutes, secs = (int(group or 0) for group in match.groups())
        seconds = hours * 3600 + minutes * 60 + secs
    if seconds > MAX_OFFSET:
        return None
    return seconds


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _escape(value: str) -> str:
    return discord.utils.escape_markdown(str(value or ""))


def _worded_line(text: str, words: list[Any], position: float) -> str:
    """A line with the word being sung underlined.

    The vault's words keep the whitespace they had in the line, so joining
    them back together reproduces ``text`` exactly. Lines whose words were not
    aligned never reach here, and a gap between words leaves the line whole.
    """
    rendered: list[str] = []
    highlighted = False
    for word in words:
        raw = str(getattr(word, "text", "") or "")
        start = getattr(word, "start", None)
        end = getattr(word, "end", None)
        core = raw.rstrip()
        tail = raw[len(core):]
        if not core:
            rendered.append(raw)
            continue
        if (
            not highlighted
            and start is not None
            and end is not None
            and start <= position < max(end, start + 0.001)
        ):
            rendered.append(f"__{core}__{tail}")
            highlighted = True
        else:
            rendered.append(raw)
    return "".join(rendered) if highlighted else text


def divider() -> discord.ui.Separator:
    return discord.ui.Separator(spacing=discord.SeparatorSpacing.small)


def container(
    *items: discord.ui.Item, colour: discord.Color | int | None = None
) -> discord.ui.Container:
    box = discord.ui.Container(accent_color=colour or EMBED_COLOR)
    for item in items:
        box.add_item(item)
    return box


@dataclass(slots=True)
class Scrobble:
    """The track a member is listening to, from Spotify or Last.fm.

    Spotify supplies where in the song the member is and how long it runs;
    Last.fm only names the track, so its cards start at the top (or at the
    point given on the command).
    """

    title: str
    artist: str
    now_playing: bool
    played_at: datetime | None = None
    source: str = "Last.fm"
    position: float | None = None
    duration: float | None = None
    #: When the presence says the song started, so a slow lyric lookup can
    #: still open the card on the second the song is at now.
    starts_at: datetime | None = None


def spotify_track(member: Any) -> Scrobble | None:
    """The member's Spotify status, with how far into the song they are now.

    Discord puts the track's start and end on the presence, so the position
    is read from the wall clock rather than guessed. A status left behind by a
    song that already finished is ignored: its position would be past the end,
    which would open a lyric card on the last line of the wrong song.
    """
    for activity in getattr(member, "activities", None) or ():
        if not isinstance(activity, discord.Spotify):
            continue
        title = str(getattr(activity, "title", "") or "").strip()
        if not title:
            continue
        try:
            start = activity.start
            end = activity.end
        except (AttributeError, KeyError, OSError, TypeError, ValueError):
            start = end = None

        duration = position = None
        if start is not None and end is not None:
            length = (end - start).total_seconds()
            duration = length if length > 0 else None
        if start is not None:
            position = max(0.0, (datetime.now(timezone.utc) - start).total_seconds())
            if duration is not None and position > duration + 2:
                continue

        artists = getattr(activity, "artists", None) or ()
        return Scrobble(
            title=title,
            artist=", ".join(str(name).strip() for name in artists if str(name).strip()),
            now_playing=True,
            source="Spotify",
            position=position,
            duration=duration,
            starts_at=start,
        )
    return None


class LiveSession:
    """One live lyric card, following a song from the point it was started at."""

    def __init__(
        self,
        cog: "LiveLyrics",
        channel: discord.abc.Messageable,
        track: Scrobble,
        lyrics: lyricmatch.Lyrics,
        offset: float,
        invoker_id: int,
        synced: bool = False,
        word_timing: list[list[Any]] | None = None,
    ) -> None:
        self.cog = cog
        self.channel = channel
        self.track = track
        self.lyrics = lyrics
        #: One list of timed words per synced line, when the source supplies
        #: them (the Juice WRLD Vault does). Kept beside the sheet rather than
        #: on the shared Lyrics object so a cog reload never depends on a
        #: reload of utils.core.lyrics, which the running process caches.
        self.word_timing = word_timing or []
        self.offset = max(0.0, offset)
        self.invoker_id = invoker_id
        #: Whether the start point came from the member's Spotify status.
        self.synced = synced
        self.message: discord.Message | None = None
        self.task: asyncio.Task | None = None
        self.index = -2
        #: The index of the underlined word on the current line, so the loop
        #: can repaint when it moves without repainting every tick.
        self.word = -2
        self.finished = False
        self.started_at = 0.0
        self.painted_at = 0.0
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def duration(self) -> float | None:
        """The recording's length: the sheet's, or the one Spotify gave."""
        return self.lyrics.duration or self.track.duration or None

    @property
    def sheet_end(self) -> float:
        """The timestamp of the sheet's last line, or ``0`` for a plain sheet."""
        return self.lyrics.synced[-1][0] if self.lyrics.synced else 0.0

    def position(self) -> float:
        """Where the song is now: the start point plus the time since it ran."""
        loop = self._loop or asyncio.get_running_loop()
        return self.offset + max(0.0, loop.time() - self.started_at)

    def end_at(self) -> float:
        """When the card has nothing left to follow.

        The recording's length and where the sheet's timestamps stop usually
        agree. When they don't — a snippet's lyric sheet under a full song's
        name, or a length shorter than the words — the later of the two wins
        rather than cutting the song off. Either way the card is never ended
        within :data:`MIN_RUNTIME` of being started, so a bad match can't make
        it say "finished" on arrival.
        """
        recording = self.duration if self.duration and self.duration > 0 else 0.0
        if recording and recording >= self.sheet_end:
            end = recording + 2.0
        elif self.sheet_end:
            end = self.sheet_end + GRACE
        elif recording:
            end = recording + 2.0
        else:
            end = self.offset + MAX_RUNTIME
        return min(max(end, self.offset + MIN_RUNTIME), self.offset + MAX_RUNTIME)

    async def start(self, ctx: commands.Context) -> None:
        if self.synced and self.track.starts_at is not None:
            # Finding the lyrics took a moment. Anchoring on the presence's
            # own start time puts the card on the line being sung now, not
            # the one that was current when the command was typed.
            self.offset = max(
                0.0, (datetime.now(timezone.utc) - self.track.starts_at).total_seconds()
            )
            if self.duration:
                self.offset = min(self.offset, self.duration)
        self._loop = asyncio.get_running_loop()
        self.started_at = self._loop.time()
        self.painted_at = self.started_at
        self.index = radio.line_index(self.lyrics.synced, self.position())
        self.word = self.word_at(self.index, self.position())
        self.message = await ctx.send(view=LiveLyricsView(self))
        if not self.lyrics.is_synced:
            return
        self.cog.remember(self)
        self.task = self._loop.create_task(self.run())

    async def run(self) -> None:
        try:
            while not self.finished:
                await asyncio.sleep(TICK)
                if self.finished:
                    return
                if self.position() >= self.end_at():
                    logger.info(
                        "Live lyrics for %r finished at %s "
                        "(recording %s, sheet ends %s, started at %s)",
                        self.track.title,
                        radio.clock(self.position()),
                        radio.clock(self.duration),
                        radio.clock(self.sheet_end),
                        radio.clock(self.offset),
                    )
                    await self.paint(finished=True)
                    return
                position = self.position()
                index = radio.line_index(self.lyrics.synced, position)
                word = self.word_at(index, position)
                now = self._loop.time() if self._loop is not None else 0.0
                if (
                    index != self.index
                    or word != self.word
                    or now - self.painted_at >= REPAINT
                ):
                    await self.paint()
        except asyncio.CancelledError:
            raise
        except discord.NotFound:
            self.finished = True
        except Exception:
            logger.exception("Live lyric loop for %r crashed", self.track.title)
        finally:
            self.cog.forget(self)

    def word_at(self, index: int, position: float) -> int:
        """Index of the word being sung on line ``index``, or ``-1``.

        The vault's word stamps have gaps between them (breaths, rests), and a
        line whose words were not aligned has none at all; both leave the line
        whole rather than underlining the wrong token.
        """
        if index < 0 or index >= len(self.word_timing):
            return -1
        for offset, word in enumerate(self.word_timing[index]):
            start = getattr(word, "start", None)
            end = getattr(word, "end", None)
            if (
                start is not None
                and end is not None
                and start <= position < max(end, start + 0.001)
            ):
                return offset
        return -1

    async def paint(self, *, finished: bool = False) -> None:
        """Rewrite the card, with the line at the current position in bold."""
        self.finished = self.finished or finished
        position = self.position()
        self.index = radio.line_index(self.lyrics.synced, position)
        self.word = self.word_at(self.index, position)
        if self._loop is not None:
            self.painted_at = self._loop.time()
        if self.message is None:
            return
        try:
            await self.message.edit(view=LiveLyricsView(self, finished=self.finished))
        except discord.NotFound:
            self.finished = True
        except discord.HTTPException:
            logger.debug("Could not repaint the live lyric card", exc_info=True)

    async def stop(self) -> None:
        """Cancel the loop and leave the card showing that it ended."""
        if self.finished:
            await self.paint(finished=True)
            return
        self.finished = True
        await self._cancel()
        self.cog.forget(self)
        await self.paint(finished=True)

    async def _cancel(self) -> None:
        task, self.task = self.task, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("Live lyric task ended with an error", exc_info=True)


class StopButton(discord.ui.Button):
    """Ends a live card early."""

    def __init__(self, session: LiveSession) -> None:
        self.session = session
        super().__init__(
            label="Stop",
            emoji="⏹️",
            style=discord.ButtonStyle.danger,
            disabled=session.finished,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self.session.stop()


class LiveLyricsView(SimpleLayout):
    """The live-lyrics card: the line being sung in bold, clock underneath."""

    def __init__(self, session: LiveSession, *, finished: bool = False) -> None:
        self.session = session
        super().__init__(
            container(*self._body(finished), colour=EMBED_COLOR), timeout=None
        )

    def _body(self, finished: bool) -> list[discord.ui.Item]:
        session = self.session
        track = session.track
        lyrics = session.lyrics
        position = session.position()
        duration = session.duration

        header = (
            f"# 📜 {_escape(track.title)}\n"
            f"-# {_escape(track.artist) or 'Unknown artist'}"
        )
        if track.now_playing:
            header += f"\n-# ▶ Now playing on {track.source}"
        elif track.played_at is not None:
            header += f"\n-# ⏹ Last scrobbled {discord.utils.format_dt(track.played_at, 'R')}"
        if lyrics.title and _norm(lyrics.title) != _norm(track.title):
            header += (
                f"\n-# Matched: {_escape(lyrics.artist) or 'unknown artist'} — "
                f"{_escape(lyrics.title)}"
            )
        header += (
            f"\n`{radio.clock(position)} "
            f"{radio.progress_bar(position, duration)} {radio.clock(duration)}`"
        )

        items: list[discord.ui.Item] = [discord.ui.TextDisplay(header), divider()]
        if lyrics.is_synced:
            items.append(discord.ui.TextDisplay(self._synced_block(position)))
        else:
            plain = (lyrics.plain or "").strip() or "No lyrics found."
            items.append(discord.ui.TextDisplay(plain[:3900]))
            items.append(
                discord.ui.TextDisplay(
                    "-# No timed lyrics for this song — showing the plain text."
                )
            )

        items.append(divider())
        if finished:
            items.append(discord.ui.TextDisplay("-# Lyrics finished."))
        else:
            footer = f"-# Live from {lyrics.source}"
            if any(session.word_timing):
                footer += " · word-timed"
            if session.synced:
                footer += (
                    f" · following your {track.source} status from "
                    f"{radio.clock(session.offset)}"
                )
            else:
                footer += f" · started at {radio.clock(session.offset)}"
            items.append(discord.ui.TextDisplay(footer))
            items.append(discord.ui.ActionRow(StopButton(session)))
        return items

    def _synced_block(self, position: float) -> str:
        lines = self.session.lyrics.synced
        word_lines = self.session.word_timing
        current = radio.line_index(lines, position)
        half = WINDOW // 2
        start = max(0, min(current - half, max(0, len(lines) - WINDOW)))
        window = lines[start : start + WINDOW]
        rendered: list[str] = []
        for offset, (_, text) in enumerate(window):
            index = start + offset
            line = text or "♪"
            if index == current:
                words = word_lines[index] if index < len(word_lines) else None
                if words:
                    line = _worded_line(line, words, position)
                rendered.append(f"**{line}**")
            else:
                rendered.append(f"-# {line}")
        return "\n".join(rendered)[:3900]


class LiveLyrics(commands.Cog):
    """Live lyrics for whatever a member is playing right now."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.session = aiohttp.ClientSession()
        self._sessions: dict[int, LiveSession] = {}

    async def cog_unload(self) -> None:
        for session in list(self._sessions.values()):
            session.finished = True
            await session._cancel()
        self._sessions.clear()
        await self.session.close()

    # -- wiring ----------------------------------------------------------
    def remember(self, session: LiveSession) -> None:
        self._sessions[session.channel.id] = session

    def forget(self, session: LiveSession) -> None:
        if self._sessions.get(session.channel.id) is session:
            self._sessions.pop(session.channel.id, None)

    async def _username(self, user_id: int) -> str | None:
        """The member's linked Last.fm name, from the LastFM cog or the database."""
        cog = self.bot.get_cog("LastFM")
        resolver = getattr(cog, "username_for", None) if cog is not None else None
        if resolver is not None:
            try:
                found = await resolver(user_id)
            except Exception:
                logger.debug("LastFM cog could not resolve %s", user_id, exc_info=True)
            else:
                if found:
                    return found
        try:
            return await self.bot.database.get_lastfm_username(int(user_id))
        except Exception:
            logger.debug("Database could not resolve %s", user_id, exc_info=True)
            return None

    async def _lastfm(self, method: str, **params: Any) -> Any:
        """Ask the LastFM cog first (it keeps the session and the key)."""
        cog = self.bot.get_cog("LastFM")
        api = getattr(cog, "api", None) if cog is not None else None
        if api is not None:
            return await api(method, **params)

        key = (os.getenv("LASTFM_API_KEY") or "").strip()
        if not key:
            raise RuntimeError("Last.fm isn't configured (no `LASTFM_API_KEY`).")
        query = {"method": method, "api_key": key, "format": "json", **params}
        async with self.session.get(
            LASTFM_API_URL, params=query, timeout=LASTFM_TIMEOUT
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"Last.fm returned HTTP {response.status}.")
            data = await response.json(content_type=None)
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(str(data.get("message") or "Last.fm API error."))
        return data

    async def _now_playing(self, username: str) -> Scrobble | None:
        """The member's most recent scrobble, currently playing or not."""
        data = await self._lastfm(
            "user.getRecentTracks", user=username, limit=2
        )
        tracks = data.get("recenttracks", {}).get("track", []) if isinstance(data, dict) else []
        if isinstance(tracks, dict):
            tracks = [tracks]
        if not tracks:
            return None

        track = tracks[0]
        if not isinstance(track, dict):
            return None
        title = str(track.get("name") or "").strip()
        if not title:
            return None
        artist_data = track.get("artist")
        if isinstance(artist_data, dict):
            artist = str(artist_data.get("#text") or "").strip()
        else:
            artist = str(artist_data or "").strip()

        attributes = track.get("@attr") or {}
        now_playing = (
            str(attributes.get("nowplaying") or "").lower() == "true"
            if isinstance(attributes, dict)
            else False
        )
        played_at = None
        date = track.get("date")
        if isinstance(date, dict):
            try:
                played_at = datetime.fromtimestamp(int(date["uts"]), tz=timezone.utc)
            except (KeyError, TypeError, ValueError, OSError):
                played_at = None
        return Scrobble(
            title=title, artist=artist, now_playing=now_playing, played_at=played_at
        )

    # -- the command -----------------------------------------------------
    @commands.command(name="livelyric", aliases=("livelyrics", "llyric"))
    @unified_cooldown(10)
    async def livelyric(self, ctx: commands.Context, *, start: str = "") -> None:
        """Live lyrics for what you're playing, from where you are right now."""
        given = bool((start or "").strip())
        offset = parse_offset(start)
        if offset is None:
            return await ctx.error(
                f"Usage: `{ctx.prefix or PREFIX}livelyric [1:34]` — the time to "
                "start the lyrics from, in `m:ss`. Leave it out to follow "
                "your Spotify status."
            )

        async with ctx.typing():
            # Spotify first: its status carries the position, so the card lines
            # up with the second the song is actually at. Last.fm only knows
            # what was scrobbled, not how far through it the member is.
            track = spotify_track(ctx.author)
            if track is None:
                username = await self._username(ctx.author.id)
                if not username:
                    return await ctx.error(
                        "You have no Spotify status on your profile and no "
                        f"Last.fm linked — try `{ctx.prefix or PREFIX}lf set <username>`."
                    )
                try:
                    track = await self._now_playing(username)
                except Exception as error:
                    logger.warning("Last.fm now-playing lookup failed: %s", error)
                    return await ctx.error(f"Last.fm didn't answer: `{error}`")
                if track is None:
                    return await ctx.error(
                        f"**{_escape(username)}** has no recent tracks on Last.fm."
                    )

            # A start point typed on the command is a deliberate override;
            # without one, follow Spotify and start at zero on Last.fm.
            begin = offset if given else (track.position or 0.0)
            synced = not given and track.position is not None

            # Juice WRLD has a dedicated vault with word-timed sheets, so his
            # songs are looked up there first. lrclib is still the fallback for
            # a recording the vault has no timed sheet for.
            lyrics = None
            word_timing = None
            vault_plain = None
            if juicevault.is_juice(track.artist):
                try:
                    found = await juicevault.find(
                        self.session,
                        title=track.title,
                        artist=track.artist,
                        duration=track.duration,
                    )
                except Exception as error:
                    logger.warning(
                        "Juice WRLD Vault lookup failed for %r: %s",
                        track.title,
                        error,
                    )
                else:
                    if found is not None:
                        if found.lyrics.is_synced:
                            lyrics = found.lyrics
                            word_timing = found.words
                        else:
                            vault_plain = found.lyrics

            if lyrics is None:
                try:
                    fallback = await lyricmatch.find(
                        self.session,
                        title=track.title,
                        artist=track.artist,
                        duration=track.duration,
                    )
                except Exception as error:
                    logger.warning(
                        "lrclib lookup failed for %r: %s", track.title, error
                    )
                    if vault_plain is None:
                        return await ctx.error(f"lrclib didn't answer: `{error}`")
                else:
                    # A timed lrclib sheet beats a plain vault one; otherwise
                    # the vault stays the preferred source for his songs.
                    if fallback is not None and (
                        fallback.is_synced or vault_plain is None
                    ):
                        lyrics = fallback
            if lyrics is None:
                lyrics = vault_plain

        if lyrics is None:
            return await ctx.error(
                f"I couldn't find lyrics for **{_escape(track.title)}** — "
                f"**{_escape(track.artist) or 'unknown artist'}**."
            )

        length = lyrics.duration or track.duration
        if length and begin >= length:
            return await ctx.error(
                f"**{_escape(track.title)}** runs `{radio.clock(length)}`, "
                f"so `{radio.clock(begin)}` is past the end of it."
            )

        previous = self._sessions.get(ctx.channel.id)
        if previous is not None and not previous.finished:
            await previous.stop()

        await LiveSession(
            self,
            ctx.channel,
            track,
            lyrics,
            begin,
            ctx.author.id,
            synced,
            word_timing=word_timing,
        ).start(ctx)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LiveLyrics(bot))
