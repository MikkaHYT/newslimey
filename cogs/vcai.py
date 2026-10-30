"""Live AI that sits in a voice channel and talks back.

The bot joins a voice channel, listens to every speaker separately (one audio
stream and one streaming-transcription session per member, so two people
talking at once never get merged into one jumbled sentence), and answers out
loud through the same Fish Audio voice the text AI uses.

``cogs.ai`` supplies the personality and, more importantly, the per-server rules
``!aiprompt`` collects — the voice assistant follows exactly the same ones.
Deepgram does the speech-to-text (one websocket per speaker) and the reply text
is spoken as soon as it is generated, so the pipeline is
audio -> STT -> model -> TTS, with no waiting for a whole turn to be buffered.

    ,vcai join            bring the AI into your voice channel
    ,vcai leave           take it out again
    ,vcai status          what it is hearing and who it last spoke to
    ,vcai wake <words>    extra names it answers to ("juice", "yo ai", ...)

It only speaks up when someone says its name, plus an occasional unprompted
roast once a conversation has been running for a while without it.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import threading
import time
from collections import deque
from typing import Any, Optional

import discord
from discord.ext import commands
from google.genai import types

import cogs.ai as ai_module
from cogs.tts import resolve_ai_voice
from utils.core.deepgram_stt import DeepgramStream
from utils.core.prefixes import prefix_for
from utils.core.settings import AI_PROMPT_KEY
from utils.core.voice_receive import (
    VoiceReceiver,
    connect_listening,
    ensure_opus,
    unregister_channel_hook,
)
from utils.embeds import Embeds
from utils.guardrails import check_slash_guardrails

logger = logging.getLogger("slimey.vcai")

#: Speak unprompted once this many seconds have passed since the last reply and
#: the room has kept talking. Keeps spontaneous roasts from being constant.
ROAST_GAP_SECONDS = 95.0
#: ...and only after this many utterances nobody aimed at the bot.
MIN_UTTERANCES_BEFORE_ROAST = 4
#: How much of the call the model sees, oldest first.
MAX_HISTORY_LINES = 24
#: Speech faster than this is dropped rather than queued: replies stay live.
MAX_REPLY_CHARS = 600
#: Talking over the bot for this long cuts it off mid-sentence.
BARGE_IN_SECONDS = 0.45
#: Longest single spoken reply, in characters, matching the TTS cap.
MAX_SPOKEN_CHARS = 500

WAKE_WORD_KEY = "vcai_wake_words"

_EMOJI_RE = re.compile(
    "[\U0001f000-\U0001faff\u2190-\u21ff\u2300-\u27bf\u2b00-\u2bff\ufe0f\u200d]"
)
_MARKDOWN_RE = re.compile(r"[*_`~|#>]")
_URL_RE = re.compile(r"https?://\S+")
#: A sentence ends on ., ! or ? followed by space; short fragments are kept so
#: abbreviations do not get their own TTS request.
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
MIN_SENTENCE_CHARS = 12


def split_sentence(buffer: str) -> tuple[Optional[str], str]:
    """Split the first finished sentence off ``buffer``.

    Returns ``(sentence, rest)``; ``sentence`` is ``None`` while the buffer
    still only holds the start of one, so the caller can wait for more text.
    """
    for match in _SENTENCE_RE.finditer(buffer):
        head = buffer[: match.start()].strip()
        if len(head) >= MIN_SENTENCE_CHARS:
            return head, buffer[match.end():]
    return None, buffer


def clean_for_speech(text: str) -> str:
    """Strip anything a TTS voice cannot pronounce."""
    text = _URL_RE.sub(" a link ", text or "")
    text = _MARKDOWN_RE.sub(" ", text)
    text = _EMOJI_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_SPOKEN_CHARS:
        text = text[: MAX_SPOKEN_CHARS - 1].rstrip() + "…"
    return text


class VoiceSession:
    """One guild's live voice conversation."""

    def __init__(self, cog: "VoiceAI", channel: Any) -> None:
        self.cog = cog
        self.bot = cog.bot
        self.channel = channel
        self.guild: discord.Guild = channel.guild
        self.channel_id: int = channel.id
        self.voice_client: Optional[discord.VoiceClient] = None
        self.api_key: str = ""
        self.receiver: Optional[VoiceReceiver] = None
        self.streams: dict[int, DeepgramStream] = {}
        self.history: deque[tuple[str, str]] = deque(maxlen=MAX_HISTORY_LINES)
        self.utterances_since_reply = 0
        self.replies = 0
        self.heard = 0
        self.last_reply_at = time.monotonic()
        self.last_latency: Optional[float] = None
        self.last_reply_text = ""
        self.last_speaker = ""
        self.playing = False
        self.interrupted = 0
        self._source_bytes: Optional[io.BytesIO] = None
        self._reply_task: Optional[asyncio.Task] = None
        self._frame_gap: dict[int, float] = {}
        self._speech_run: dict[int, float] = {}
        self._closed = False

    # -- names it answers to -------------------------------------------
    def trigger_names(self) -> set[str]:
        names: set[str] = {"ai"}
        bot_user = self.bot.user
        candidates = [
            getattr(bot_user, "name", "") or "",
            getattr(getattr(self.guild, "me", None), "display_name", "") or "",
        ] + list(self.cog.extra_wake_words(self.guild.id))
        for candidate in candidates:
            words = candidate.strip().lower()
            if not words:
                continue
            # A multi-word name only counts as the whole phrase, so a bot called
            # "Juice WRLD" does not fire every time someone says "juice".
            names.add(words)
        return {name for name in names if name}

    def is_addressed(self, text: str) -> bool:
        lowered = " ".join((text or "").lower().split())
        if not lowered:
            return False
        bot_user = self.bot.user
        if bot_user is not None and (
            f"<@{bot_user.id}>" in text or f"<@!{bot_user.id}>" in text
        ):
            return True
        for name in self.trigger_names():
            if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", lowered):
                return True
        return False

    # -- lifecycle -----------------------------------------------------
    async def start(self) -> bool:
        api_key = os.getenv("DEEPGRAM_API_KEY", "").strip()
        if not api_key or not ensure_opus():
            return False
        self.api_key = api_key

        def factory(voice_client: discord.VoiceClient) -> VoiceReceiver:
            return VoiceReceiver(voice_client, on_pcm=self._on_pcm)

        try:
            self.voice_client, self.receiver = await connect_listening(
                self.channel, receiver_factory=factory
            )
        except Exception:
            logger.exception("Could not start listening in guild %s", self.guild.id)
            return False
        return True

    async def close(self) -> None:
        self._closed = True
        if self.channel_id is not None:
            unregister_channel_hook(self.channel_id)
        if self._reply_task is not None and not self._reply_task.done():
            self._reply_task.cancel()
        for stream in list(self.streams.values()):
            try:
                await stream.close()
            except Exception:
                logger.debug("Could not close a transcript stream", exc_info=True)
        self.streams.clear()
        if self.receiver is not None:
            self.receiver.detach()
        try:
            if self.voice_client is not None and self.voice_client.is_connected():
                await self.voice_client.disconnect(force=True)
        except Exception:
            logger.debug("Could not disconnect the voice client", exc_info=True)

    # -- audio in ------------------------------------------------------
    def _on_pcm(self, user_id: int, pcm: bytes) -> None:
        if self._closed or not pcm:
            return
        now = time.monotonic()
        gap = now - self._frame_gap.get(user_id, 0.0)
        self._frame_gap[user_id] = now
        # A 20 ms frame every time means they have been talking continuously.
        self._speech_run[user_id] = (
            self._speech_run.get(user_id, 0.0) + 0.02 if gap < 0.1 else 0.02
        )
        if self.playing and self._speech_run[user_id] >= BARGE_IN_SECONDS:
            self._cut_playback()
            self._speech_run[user_id] = 0.0

        stream = self.streams.get(user_id)
        if stream is None:
            stream = self._open_stream(user_id)
        if stream is not None:
            stream.push(pcm)

    def _open_stream(self, user_id: int) -> Optional[DeepgramStream]:
        """Create this speaker's transcription session, once."""
        if self._closed or not self.api_key:
            return None
        # Negative ids are the receiver's placeholder for audio no SPEAKING
        # event has named; anything else is a real member.
        member = self.guild.get_member(user_id) if user_id > 0 else None
        if member is not None:
            label = member.display_name
        elif user_id < 0:
            label = "someone in the call"
        else:
            label = f"user {user_id}"

        def on_utterance(text: str) -> None:
            self._on_utterance(user_id, label, text)

        stream = DeepgramStream(
            self.api_key,
            on_utterance=on_utterance,
            on_error=lambda message: logger.warning(
                "Transcription for %s in %s: %s", label, self.guild.id, message
            ),
        )
        self.streams[user_id] = stream
        asyncio.create_task(self._start_stream(stream, label))
        return stream

    async def _start_stream(self, stream: DeepgramStream, label: str) -> None:
        if not await stream.start():
            logger.warning("Could not start transcription for %s", label)

    def _on_utterance(self, user_id: int, label: str, text: str) -> None:
        text = text.strip()
        if not text or self._closed:
            return
        self.heard += 1
        self.history.append((label, text))
        self.utterances_since_reply += 1
        self._maybe_reply(addressed=self.is_addressed(text), speaker=label)

    # -- deciding to talk ----------------------------------------------
    def _maybe_reply(self, *, addressed: bool, speaker: str) -> None:
        if self._closed:
            return
        if self._reply_task is not None and not self._reply_task.done():
            return
        roast = (
            not addressed
            and self.utterances_since_reply >= MIN_UTTERANCES_BEFORE_ROAST
            and time.monotonic() - self.last_reply_at >= ROAST_GAP_SECONDS
        )
        if not addressed and not roast:
            return
        self._reply_task = asyncio.create_task(
            self._reply(addressed=addressed, speaker=speaker)
        )

    def transcript_block(self) -> str:
        if not self.history:
            return "(nothing heard yet)"
        return "\n".join(f"{name}: {line}" for name, line in self.history)

    def build_instruction(self, guild: discord.Guild | None) -> str:
        """The voice-call personality plus this server's own AI rules."""
        server_rules = self.cog.server_prompt_section(guild)
        return (
            "You are on a LIVE VOICE CALL in the Discord server "
            f"'{getattr(guild, 'name', 'this server')}'. Everything you write is "
            "spoken out loud by a text-to-speech voice within a second, and people "
            "answer you out loud, so this is a spoken conversation and not text "
            "chat.\n"
            "STYLE:\n"
            "- One to three short spoken sentences. Never lists, never headings, "
            "never markdown, never emoji, no stage directions or asterisks.\n"
            "- You ARE Juice WRLD in every reply: his voice, his slang, his "
            "energy. Never say you are a bot, an AI or a model.\n"
            "- You are witty and you roast people here. When someone talks "
            "greasy, repeats themselves, begs or says something daft, clap back "
            "in Juice's words - specific and cutting, not generic.\n"
            "- When someone is just talking normally, answer them like a mate on "
            "a call: relaxed, quick and natural.\n"
            "- People can and will interrupt you, so get to the point immediately "
            "and do not waste the first sentence on pleasantries.\n"
            "- You can HEAR everyone, but you only see the transcript below, so "
            "never claim to have missed something that is written there.\n"
            "TRANSCRIPT FORMAT: every line starts with the speaker's name and a "
            "colon, like 'alex: yo'. The name is who actually said it - never "
            "assume a line came from whoever is speaking to you now.\n"
            f"{server_rules}"
        )

    async def _reply(self, *, addressed: bool, speaker: str) -> None:
        """Generate and speak a reply, sentence by sentence as it arrives.

        The model stream is consumed while the first sentence is already being
        spoken, so time-to-first-audio is one sentence of generation instead of
        the whole answer.
        """
        started = time.perf_counter()
        prompt = self._reply_prompt(addressed=addressed, speaker=speaker)
        spoken: list[str] = []
        buffer = ""
        try:
            async for piece in self.cog.reply_sentences(
                system_instruction=self.build_instruction(self.guild), prompt=prompt
            ):
                buffer += piece
                while True:
                    sentence, buffer = split_sentence(buffer)
                    if sentence is None:
                        break
                    await self._speak_piece(sentence, spoken, started, speaker)
            await self._speak_piece(buffer, spoken, started, speaker)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Voice reply stream failed in guild %s", self.guild.id)
            if spoken:
                return
        if spoken:
            return
        # Nothing was spoken: the streaming call failed or said nothing, so ask
        # once more the ordinary way rather than leaving the room unanswered.
        await self._fallback_reply(prompt, started, speaker)

    async def _speak_piece(
        self, raw: str, spoken: list[str], started: float, speaker: str
    ) -> None:
        text = clean_for_speech(raw)
        if not text:
            return
        if not spoken:
            # First audio of this reply: this is what "fast" is measured on.
            self.last_latency = time.perf_counter() - started
            self.last_reply_at = time.monotonic()
            self.last_speaker = speaker
            self.replies += 1
            self.utterances_since_reply = 0
        spoken.append(text)
        self.last_reply_text = " ".join(spoken)[:MAX_REPLY_CHARS]
        await self.speak(text)

    async def _fallback_reply(self, prompt: str, started: float, speaker: str) -> None:
        try:
            text = await self.cog.generate_reply(
                system_instruction=self.build_instruction(self.guild), prompt=prompt
            )
        except Exception:
            logger.exception("Voice reply failed in guild %s", self.guild.id)
            return
        if text:
            await self._speak_piece(text, [], started, speaker)

    def _reply_prompt(self, *, addressed: bool, speaker: str) -> str:
        who = speaker or "someone"
        if addressed:
            ask = (
                f"{who} just spoke to you by name. Answer them out loud, as if you "
                "were on the call with them."
            )
        else:
            ask = (
                f"Nobody said your name. You have been listening for a while, so "
                f"jump in with one short, funny roast aimed at {who} (or whoever "
                "deserves it most from the last few lines) - then let them talk "
                "again."
            )
        return (
            "LIVE CALL TRANSCRIPT (oldest first):\n"
            f"```\n{self.transcript_block()}\n```\n"
            f"{ask}"
        )

    # -- audio out -----------------------------------------------------
    async def speak(self, text: str) -> None:
        tts = self.bot.get_cog("TTS")
        if tts is None or self._closed:
            return
        data = await tts.render(text, reference_id=resolve_ai_voice(self.bot))
        if not data or self.voice_client is None:
            return
        if not self.voice_client.is_connected():
            return
        # Hold the buffer: ffmpeg reads it while the audio plays.
        self._source_bytes = io.BytesIO(data)
        source = discord.FFmpegPCMAudio(self._source_bytes, pipe=True)
        if self.voice_client.is_playing():
            self.voice_client.stop()
        finished = asyncio.Event()
        loop = asyncio.get_running_loop()

        def after(error: Optional[Exception]) -> None:
            if error is not None:
                logger.debug("Voice playback error: %s", error)
            loop.call_soon_threadsafe(finished.set)

        try:
            self.voice_client.play(source, after=after)
        except discord.ClientException:
            logger.debug("Voice client was not ready to play")
            return
        self.playing = True
        try:
            await asyncio.wait_for(finished.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            logger.debug("Playback did not report completion")
        finally:
            self.playing = False

    def _cut_playback(self) -> None:
        """Someone is talking over the reply: stop it."""
        if self.voice_client is None or not self.voice_client.is_playing():
            self.playing = False
            return
        self.interrupted += 1
        try:
            self.voice_client.stop()
        except Exception:
            logger.debug("Could not stop voice playback", exc_info=True)


class VoiceAI(commands.Cog, name="VoiceAI"):
    """Live conversational AI in a voice channel."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.sessions: dict[int, VoiceSession] = {}

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await check_slash_guardrails(self, interaction)

    async def cog_unload(self) -> None:
        for session in list(self.sessions.values()):
            await session.close()
        self.sessions.clear()

    # ------------------------------------------------------------------
    # Shared bits the session needs
    # ------------------------------------------------------------------
    def extra_wake_words(self, guild_id: int) -> list[str]:
        settings = getattr(self.bot, "dash_settings", None)
        if settings is None:
            return []
        value = settings.stored(WAKE_WORD_KEY, guild_id=int(guild_id))
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value if str(item).strip()]
        return []

    def server_prompt_section(self, guild: Optional[discord.Guild]) -> str:
        """This server's ``!aiprompt`` rules, the same ones the text AI follows."""
        guild_id = guild.id if guild else None
        ai_cog = self.bot.get_cog("AI")
        if ai_cog is not None:
            try:
                return ai_cog._server_prompt_section(guild)
            except Exception:
                logger.debug("Could not read the AI server prompt", exc_info=True)
        settings = getattr(self.bot, "dash_settings", None)
        if settings is None or guild_id is None:
            return ""
        raw = settings.stored(AI_PROMPT_KEY, guild_id=int(guild_id))
        lines = [str(line) for line in raw] if isinstance(raw, (list, tuple)) else []
        if isinstance(raw, str) and raw.strip():
            lines = [raw.strip()]
        if not lines:
            return ""
        numbered = "\n".join(f"{index}. {line}" for index, line in enumerate(lines, 1))
        return (
            "\n\nSERVER RULES (extra instructions added by this server's admins - "
            f"follow them all):\n{numbered}\n"
        )

    async def reply_sentences(self, *, system_instruction: str, prompt: str):
        """Yield the model's answer in pieces as it is generated.

        The SDK's streaming call is synchronous, so it runs in a worker thread
        and hands text to the event loop through a queue.
        """
        contents = [
            types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
        ]
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=1.0,
            max_output_tokens=160,
        )
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def post(item: Any) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:  # loop already closed during shutdown
                pass

        def worker() -> None:
            try:
                stream = ai_module.client.models.generate_content_stream(
                    model=ai_module.MODEL_ID, contents=contents, config=config
                )
                for chunk in stream:
                    text = getattr(chunk, "text", "") or ""
                    if text:
                        post(text)
            except BaseException as error:  # handed to the caller below
                post(error)
            finally:
                post(None)

        threading.Thread(target=worker, name="vcai-model", daemon=True).start()
        while True:
            item = await queue.get()
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    async def generate_reply(self, *, system_instruction: str, prompt: str) -> str:
        """One fast model call using the AI cog's model and retry handling."""
        contents = [
            types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
        ]
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=1.0,
            max_output_tokens=160,
        )
        ai_cog = self.bot.get_cog("AI")
        if ai_cog is not None:
            response = await ai_cog._generate_content(contents, config)
        else:
            # No AI cog loaded: fall back to a direct call on the same client.
            response = await asyncio.to_thread(
                ai_module.client.models.generate_content,
                model=ai_module.MODEL_ID,
                contents=contents,
                config=config,
            )
        return (getattr(response, "text", "") or "").strip()

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    @commands.hybrid_group(
        name="vcai",
        aliases=["aicall", "liveai"],
        invoke_without_command=True,
        description="Live conversational AI in a voice channel.",
    )
    @commands.guild_only()
    async def vcai(self, ctx: commands.Context) -> None:
        """Show the voice AI commands."""
        prefix = await prefix_for(ctx)
        lines = [
            f"`{prefix}vcai join` — bring the AI into your voice channel",
            f"`{prefix}vcai leave` — take it back out",
            f"`{prefix}vcai status` — what it is hearing right now",
            f"`{prefix}vcai wake <words>` — extra names it answers to",
            "",
            "It listens to each speaker separately, answers out loud in the AI "
            "voice when you say its name, and follows this server's `aiprompt` "
            "rules like the text AI does.",
        ]
        await Embeds.blurple(ctx, "\n".join(lines), title="🎙️ Live VC AI", delete_after=None)

    @vcai.command(name="join")
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.default_permissions(manage_guild=True)
    async def vcai_join(self, ctx: commands.Context) -> None:
        """Join your voice channel and start the live conversation."""
        guild = ctx.guild
        existing = self.sessions.get(guild.id)
        if existing is not None:
            return await Embeds.info(
                ctx,
                f"The AI is already live in this server — {existing.channel.mention}.",
                delete_after=10,
            )
        # Music and radio own the voice client here; taking it over would cut
        # whatever is playing, so ask them to stop first.
        if guild.voice_client is not None:
            return await Embeds.error(
                ctx,
                "I'm already connected to a voice channel here (music or radio) — "
                "stop that first, then try again.",
                delete_after=15,
            )
        author = ctx.author
        channel = getattr(getattr(author, "voice", None), "channel", None)
        if channel is None:
            return await Embeds.error(
                ctx, "Join a voice channel first, then run this.", delete_after=10
            )
        required = discord.Permissions(connect=True, speak=True)
        if not channel.permissions_for(guild.me).is_superset(required):
            return await Embeds.error(
                ctx,
                f"I need **Connect** and **Speak** in {channel.mention}.",
                delete_after=10,
            )
        if not os.getenv("DEEPGRAM_API_KEY", "").strip():
            return await Embeds.error(
                ctx,
                "Live transcription needs a `DEEPGRAM_API_KEY` in the "
                "environment before I can listen.",
                delete_after=15,
            )
        if not ensure_opus():
            return await Embeds.error(
                ctx,
                "I could not load the Opus library, so I cannot decode voice "
                "audio on this host.",
                delete_after=15,
            )

        session = VoiceSession(self, channel)
        started = await session.start()
        if not started:
            unregister_channel_hook(channel.id)
            await session.close()
            return await Embeds.error(
                ctx,
                f"I couldn't get into {channel.mention} or start listening — "
                "check the bot logs.",
                delete_after=15,
            )
        self.sessions[guild.id] = session
        await Embeds.success(
            ctx,
            f"🎙️ Live AI is in {channel.mention}. Say my name and I'll answer out "
            "loud; I'll also chip in with a roast now and then.",
            delete_after=15,
        )

    @vcai.command(name="leave")
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.default_permissions(manage_guild=True)
    async def vcai_leave(self, ctx: commands.Context) -> None:
        """Stop the live conversation and disconnect."""
        session = self.sessions.pop(ctx.guild.id, None)
        if session is None:
            return await Embeds.info(
                ctx, "The live AI is not connected here.", delete_after=10
            )
        await session.close()
        await Embeds.success(ctx, "🎙️ Live AI left the call.", delete_after=10)

    @vcai.command(name="status")
    async def vcai_status(self, ctx: commands.Context) -> None:
        """Show what the live AI is hearing."""
        session = self.sessions.get(ctx.guild.id)
        if session is None:
            return await Embeds.info(
                ctx,
                f"The AI is not live here. Start it with "
                f"`{await prefix_for(ctx)}vcai join`.",
                title="🎙️ Live VC AI",
                delete_after=None,
            )
        lines = [
            f"**Channel:** {session.channel.mention}",
            f"**Speakers heard:** {len(session.streams)} · **utterances:** {session.heard}",
            f"**Replies spoken:** {session.replies} · **interruptions:** {session.interrupted}",
        ]
        receiver = session.receiver
        if receiver is not None:
            live = sum(1 for stream in session.streams.values() if stream.connected)
            lines.append(
                f"**Audio:** {receiver.decoded} frames decoded · "
                f"{receiver.dropped} dropped · {live}/{len(session.streams)} live "
                "transcription streams"
            )
            # Read defensively: reloading this cog re-imports it but not the
            # already-cached `utils.core.voice_receive`, whose older receiver
            # has no `last_error` property — the raw attribute is there either
            # way, so a reload can never break this card.
            problem = getattr(receiver, "last_error", None) or getattr(
                receiver, "_last_error", None
            )
            if problem:
                lines.append(f"**Last audio problem:** {problem}")
            if receiver.decoded == 0:
                lines.append(
                    "-# No audio decoded yet: no voice packets are arriving, so "
                    "this is not a wake word or model problem."
                )
        if session.last_latency is not None:
            lines.append(
                f"**Last reply:** {session.last_latency:.2f}s to first audio"
            )
        if session.last_speaker:
            lines.append(f"**Last answered:** {session.last_speaker}")
        if session.last_reply_text:
            lines.append(f"-# “{session.last_reply_text[:200]}”")
        lines.append("")
        lines.append("**Recent transcript**")
        recent = list(session.history)[-6:] or [("—", "nothing heard yet")]
        lines.extend(f"> **{name}:** {text}" for name, text in recent)
        await Embeds.info(
            ctx, "\n".join(lines), title="🎙️ Live VC AI", delete_after=None
        )

    @vcai.command(name="wake")
    @commands.has_permissions(manage_guild=True)
    @discord.app_commands.default_permissions(manage_guild=True)
    async def vcai_wake(self, ctx: commands.Context, *, words: str = "") -> None:
        """Set the extra names the AI answers to, one per line or by commas."""
        settings = getattr(self.bot, "dash_settings", None)
        if settings is None:
            return await Embeds.error(
                ctx, "The settings store isn't ready yet.", delete_after=10
            )
        cleaned = [
            part.strip().lower().lstrip("@,")
            for part in re.split(r"[,\n]", words or "")
            if part.strip()
        ]
        if not cleaned:
            current = self.extra_wake_words(ctx.guild.id)
            listed = ", ".join(f"`{word}`" for word in current) or "*none*"
            return await Embeds.info(
                ctx,
                f"**Extra names:** {listed}\n"
                f"-# Set them with `{await prefix_for(ctx)}vcai wake juice, yo ai`.",
                title="🎙️ Live VC AI",
                delete_after=None,
            )
        await settings.set_value(WAKE_WORD_KEY, cleaned, guild_id=ctx.guild.id)
        listed = ", ".join(f"`{word}`" for word in cleaned)
        await Embeds.success(
            ctx, f"🎙️ It will also answer to {listed}.", delete_after=10
        )

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Drop the session when the bot is moved out or disconnected."""
        if self.bot.user is None or member.id != self.bot.user.id:
            return
        session = self.sessions.get(member.guild.id)
        if session is None:
            return
        if after.channel is None or after.channel.id != session.channel_id:
            self.sessions.pop(member.guild.id, None)
            await session.close()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VoiceAI(bot))
