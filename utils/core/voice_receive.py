"""Per-speaker audio out of a Discord voice connection.

discord.py only ever writes to the voice socket, so this hooks the library's own
``SocketReader`` and does the receiving itself: strip the RTP header, decrypt the
payload with the connection's secret key (every transport mode discord.py can
send is supported), unwrap the DAVE frame on E2EE channels when that session is
available, decode the Opus frame, and hand the caller 16 kHz mono PCM tagged
with the member who spoke.

The SSRC -> member mapping comes from the voice websocket's SPEAKING events,
which :class:`ListeningVoiceClient` forwards to a hook registered per channel.
Audio from an SSRC no SPEAKING event has named is still decoded under a
placeholder identity: a speaker label is cosmetic, being deaf is not.

Nothing here blocks the event loop: the socket callback runs on discord.py's
reader thread, so the PCM is handed back with ``loop.call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from collections import OrderedDict
from typing import Any, Callable, Optional

import discord
import numpy as np
from nacl import bindings

from discord.voice_state import VoiceConnectionState

logger = logging.getLogger("slimey.voice_recv")

#: Opus always arrives at this rate; the STT stream wants a third of it.
DISCORD_RATE = 48_000
TARGET_RATE = 16_000
_DECIMATE = DISCORD_RATE // TARGET_RATE

#: DAVE (E2EE voice) wraps the Opus frame with this marker before transport
#: encryption, so it survives as the first two bytes after decrypting.
DAVE_MAGIC = b"\xfa\xfa"

try:  # davey ships with discord.py's voice extra.
    from davey import MediaType as _DaveMediaType

    # The binding wants the enum itself, not the integer behind it: passing
    # ``int(MediaType.audio)`` is rejected, which silently failed every E2EE
    # frame and left the assistant deaf.
    _DAVE_AUDIO: Any = _DaveMediaType.audio
    _DAVE_READY = True
except Exception:  # pragma: no cover - only if davey is missing
    _DAVE_AUDIO = None
    _DAVE_READY = False


# ---------------------------------------------------------------------------
# Opus
# ---------------------------------------------------------------------------
_OPUS_LOAD_TRIED = False


def _aes256gcm_decrypt(
    ciphertext: bytes, aad: bytes, nonce: bytes, key: bytes
) -> bytes:
    """AES-256-GCM open, with libsodium when it has AES and ``cryptography`` otherwise.

    Discord lists ``aead_aes256_gcm_rtpsize`` as a transport mode, but discord.py
    never negotiates it, so nothing in this project opened one — which is one of
    the few ways a live channel decrypts under *no* supported mode.
    """
    if getattr(bindings, "crypto_aead_aes256gcm_is_available", lambda: False)():
        return bindings.crypto_aead_aes256gcm_decrypt(ciphertext, aad, nonce, key)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM(key).decrypt(nonce, ciphertext, aad)


def ensure_opus() -> bool:
    """Load libopus for decoding, returning whether it is usable.

    Voice playback in this bot goes through ffmpeg, which never loads
    discord.py's Opus bindings, so this has to happen once before the first
    decoder is built.
    """
    global _OPUS_LOAD_TRIED
    if discord.opus.is_loaded():
        return True
    if _OPUS_LOAD_TRIED:
        return False
    _OPUS_LOAD_TRIED = True
    for name in ("libopus.so.0", "libopus.so", "libopus.0.dylib", "opus.dll"):
        try:
            discord.opus.load_opus(name)
        except Exception:
            continue
        if discord.opus.is_loaded():
            return True
    logger.warning("Could not load libopus; voice receive will not work")
    return False


# ---------------------------------------------------------------------------
# Voice websocket hook: SPEAKING events carry ssrc -> user_id
# ---------------------------------------------------------------------------
_HOOKS: dict[int, Callable[[Any, dict], Any]] = {}


def register_channel_hook(channel_id: int, hook: Callable[[Any, dict], Any]) -> None:
    _HOOKS[channel_id] = hook


def unregister_channel_hook(channel_id: int) -> None:
    _HOOKS.pop(channel_id, None)


def attach_speaking_hook(
    voice_client: discord.VoiceClient, hook: Callable[[Any, dict], Any]
) -> None:
    """Point the live voice websocket at ``hook`` for SPEAKING (op 5) frames.

    ``VoiceClient.__init__`` builds its connection state before the client is
    tied to a channel, so a hook registered by channel id — the only thing
    ``create_connection_state`` can look at — misses, and the websocket is left
    with discord.py's no-op hook. Without these frames there is no ssrc -> member
    mapping and every packet is dropped as "unknown speaker".

    A reconnect rebuilds the websocket from ``state.hook``, so both are set.
    """
    state = getattr(voice_client, "_connection", None)
    if state is None:
        return
    try:
        state.hook = hook
    except Exception:
        logger.debug("Could not set the voice state hook", exc_info=True)
    websocket = getattr(state, "ws", None)
    if websocket is not None:
        try:
            websocket._hook = hook
        except Exception:
            logger.debug("Could not set the voice websocket hook", exc_info=True)


class ListeningVoiceClient(discord.VoiceClient):
    """A ``VoiceClient`` that forwards every voice websocket frame to a hook.

    discord.py hands the connection state the hook it should call for each
    websocket message, and that is where SPEAKING events (op 5) arrive with the
    ``ssrc`` and ``user_id`` that make per-speaker audio possible.
    """

    def create_connection_state(self) -> VoiceConnectionState:
        channel_id = getattr(self.channel, "id", None)
        hook = _HOOKS.get(channel_id) if channel_id is not None else None
        return VoiceConnectionState(self, hook=hook)


# ---------------------------------------------------------------------------
# Receiver
# ---------------------------------------------------------------------------
class VoiceReceiver:
    """Turns the voice socket's datagrams into labelled 16 kHz PCM.

    Parameters
    ----------
    voice_client:
        The connected :class:`ListeningVoiceClient`.
    on_pcm:
        Called from the event loop with ``(user_id, pcm_bytes)`` for every
        decoded frame that belongs to a known speaker.
    target_rate:
        Sample rate of the PCM handed to ``on_pcm``. 16 kHz by default, which
        is what the streaming STT service expects.
    """

    def __init__(
        self,
        voice_client: discord.VoiceClient,
        *,
        on_pcm: Callable[[int, bytes], None],
        target_rate: int = TARGET_RATE,
    ) -> None:
        self.voice_client = voice_client
        self.on_pcm = on_pcm
        self.target_rate = target_rate
        self._loop = asyncio.get_event_loop()
        self._state: Optional[VoiceConnectionState] = None
        #: ssrc -> user id, filled from SPEAKING events.
        self.ssrc_users: dict[int, int] = {}
        #: Decoders are expensive to build, so one per speaker is kept warm.
        self._decoders: "OrderedDict[int, Any]" = OrderedDict()
        self._attached = False
        #: RTP packets that reached the decrypt stage. This separates "nothing
        #: is arriving" from "packets arrive and fail", which look identical
        #: on a status card that only shows a decode count.
        self.packets = 0
        self.dropped = 0
        self.decoded = 0
        #: Frames whose audio arrived without a SPEAKING event naming it.
        self.unidentified = 0
        #: Drop counts by stage, so the status card can name where audio stops.
        self.drop_reasons: dict[str, int] = {}
        #: The transport mode that actually authenticated inbound audio.
        self.working_mode: Optional[str] = None
        #: The mode the voice connection negotiated, for comparison in status.
        self.negotiated_mode: Optional[str] = None
        #: Shape of the packets that failed to decrypt, for the status card, so
        #: a key problem is visible without digging through the log.
        self.decrypt_detail: Optional[str] = None
        #: The layout variant that authenticated last, tried first next time.
        self._preferred: Optional[str] = None
        #: How many undecryptable packets have been dumped to the log, so the
        #: raw bytes are captured without flooding it.
        self._probe_logs = 0
        #: How many Opus decode failures have been dumped, and whether a good
        #: frame has been reported yet.
        self._opus_probe = 0
        self._opus_ok = False
        self._dave_strip_logged = False
        #: Stages of the last packet, kept for the one-shot debug dump.
        self._stage_transport: Optional[bytes] = None
        self._stage_dave: Optional[bytes] = None
        self._debug_left = 3
        #: Leading bytes that had to be skipped before Opus accepted a frame.
        self._frame_skip = 0
        #: One-shot logs for the DAVE path.
        self._dave_ready_logged = False
        self._dave_error_logged = False
        #: Whether the "DAVE is negotiated but this speaker is unnamed" note has
        #: been logged, so it appears once rather than on every frame.
        self._dave_unknown_logged = False
        #: One-shot capture of raw datagrams and decrypted frames, so the exact
        #: layout of what Discord sends can be inspected offline.
        self._raw_dumps_left = 4
        self._frame_dumps_left = 4
        self._last_error: Optional[str] = None
        #: Encryption modes already reported, so an unknown one is logged once
        #: instead of on every packet.
        self._warned_modes: set[str] = set()
        self._warned_unmapped = False

    # -- lifecycle -----------------------------------------------------
    @property
    def guild_id(self) -> Optional[int]:
        return getattr(getattr(self.voice_client, "guild", None), "id", None)

    def attach(self) -> bool:
        """Start reading the socket. Returns False when there is nothing to read."""
        if self._attached:
            return True
        state = getattr(self.voice_client, "_connection", None)
        if state is None or getattr(state, "socket", None) is None:
            return False
        self._state = state
        state.add_socket_listener(self._on_datagram)
        # ``register`` only unpauses a reader that paused itself, while discord.py
        # also pauses it explicitly on a stop/reconnect. A listener added after
        # that would sit waiting forever, so ask for it to resume by name.
        reader = getattr(state, "_socket_reader", None)
        if reader is not None:
            try:
                reader.resume()
            except Exception:
                logger.debug(
                    "Could not resume the voice socket reader", exc_info=True
                )
        self._attached = True
        return True

    @property
    def last_error(self) -> Optional[str]:
        """Why the last packet was dropped, for the session's status card."""
        return self._last_error

    def _fail(self, reason: str) -> None:
        """Record a drop against the stage that caused it.

        The error is the *latest* one rather than the first: a card that keeps
        reporting the first failure of the session points at the wrong stage
        once the pipeline moves on, which is exactly how "nothing heard yet"
        became unreadable.
        """
        self.dropped += 1
        self.drop_reasons[reason] = self.drop_reasons.get(reason, 0) + 1
        self._last_error = reason

    def detach(self) -> None:
        if self._state is not None and self._attached:
            try:
                self._state.remove_socket_listener(self._on_datagram)
            except Exception:
                logger.debug("Could not remove the voice socket listener", exc_info=True)
        self._attached = False
        for decoder in self._decoders.values():
            try:
                decoder.destroy()  # type: ignore[attr-defined]
            except Exception:
                pass
        self._decoders.clear()
        self.ssrc_users.clear()

    # -- speaking map --------------------------------------------------
    def note_speaking(self, payload: dict) -> None:
        """Record the ``ssrc`` -> ``user_id`` pair from a SPEAKING event."""
        user_id = payload.get("user_id")
        ssrc = payload.get("ssrc")
        if ssrc is None or user_id is None:
            return
        try:
            self.ssrc_users[int(ssrc)] = int(user_id)
        except (TypeError, ValueError):
            return

    def _decoder_for(self, ssrc: int):
        decoder = self._decoders.get(ssrc)
        if decoder is None:
            if not ensure_opus():
                return None
            decoder = discord.opus.Decoder()
            self._decoders[ssrc] = decoder
            while len(self._decoders) > 32:
                _ssrc, old = self._decoders.popitem(last=False)
                try:
                    old.destroy()  # type: ignore[attr-defined]
                except Exception:
                    pass
        else:
            self._decoders.move_to_end(ssrc)
        return decoder

    # -- packet handling -----------------------------------------------
    def _current_state(self) -> Optional[VoiceConnectionState]:
        """Follow the connection when it is rebuilt, or audio can't decrypt.

        A reconnect creates a new connection state with a new ``secret_key``.
        A receiver still pointing at the old one decrypts with a dead key, so
        every packet fails authentication and the bot looks deaf while packets
        keep arriving. Rebinding also re-registers this callback, since the
        reader belongs to the new state.
        """
        state = getattr(self.voice_client, "_connection", None)
        if state is None or state is self._state:
            return self._state
        logger.info("Voice connection was rebuilt; rebinding the receiver")
        self._state = state
        try:
            state.add_socket_listener(self._on_datagram)
            reader = getattr(state, "_socket_reader", None)
            if reader is not None:
                reader.resume()
        except Exception:
            logger.debug("Could not rebind to the new voice state", exc_info=True)
        return state

    def _dump_stage(self, label: str, payload: bytes) -> None:
        """Append one hex stage of a packet to ``data/voice_debug.txt``."""
        try:
            from pathlib import Path

            path = Path("data/voice_debug.txt")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"{label} ({len(payload)}B) {payload.hex()}\n")
        except Exception:
            logger.debug("Could not write the packet dump", exc_info=True)

    def _on_datagram(self, data: bytes) -> None:
        if self._raw_dumps_left > 0:
            self._raw_dumps_left -= 1
            self._dump_stage("datagram", data)
        try:
            self._current_state()
        except Exception:
            logger.debug("Could not refresh the voice state", exc_info=True)
        try:
            decoded = self._handle(data)
        except Exception:
            self._last_error = "packet"
            logger.debug("Could not handle a voice packet", exc_info=True)
            return
        if decoded is None:
            return
        user_id, pcm = decoded
        self.decoded += 1
        try:
            self._loop.call_soon_threadsafe(self.on_pcm, user_id, pcm)
        except RuntimeError:
            # The loop is shutting down; nothing left to deliver to.
            pass

    def _handle(self, packet: bytes) -> Optional[tuple[int, bytes]]:
        if len(packet) < 16 or self._state is None:
            return None
        state = self._state
        secret = getattr(state, "secret_key", None)
        if not isinstance(secret, (bytes, bytearray, list)) or not secret:
            return None

        first = packet[0]
        if first >> 6 != 2:  # not RTP version 2
            return None
        # RTCP control packets (receiver reports, transport feedback, ...) share
        # the socket and the version bits, but their payload type is 200-207.
        # Reading one as audio yields a garbage ssrc and a decoder failure,
        # which looks exactly like a deaf receiver.
        if 72 <= (packet[1] & 0x7F) <= 79:
            return None
        csrc_count = first & 0x0F
        header_len = 12 + csrc_count * 4
        if first & 0x10:  # header extension
            if len(packet) < header_len + 4:
                return None
            ext_words = struct.unpack_from(">H", packet, header_len + 2)[0]
            header_len += 4 + ext_words * 4
        if len(packet) <= header_len:
            return None

        ssrc = struct.unpack_from(">I", packet, 8)[0]
        self.packets += 1
        user_id = self.ssrc_users.get(ssrc)
        if user_id is None:
            # No SPEAKING event has named this voice. Decode it anyway under a
            # placeholder identity rather than going deaf: if the audio decodes
            # it was speech, whatever the missing event would have called it.
            user_id = -int(ssrc) or -1
            self.unidentified += 1
            if not self._warned_unmapped:
                self._warned_unmapped = True
                logger.warning(
                    "Voice from ssrc %s has no SPEAKING event; transcribing it "
                    "as an unnamed speaker",
                    ssrc,
                )

        header = packet[:header_len]
        payload = packet[header_len:]
        if self._frame_dumps_left > 0:
            self._dump_stage(
                f"header_len={header_len} mode={getattr(state, 'mode', '')!r} header",
                header,
            )
            self._dump_stage("payload", payload)
        frame = self._decrypt(state, header, payload, bytes(secret), user_id)
        if frame is None:
            # `_decrypt` already recorded the stage that refused the packet.
            return None
        if self._frame_dumps_left > 0:
            self._frame_dumps_left -= 1
            self._dump_stage("frame", frame)
            try:
                count = discord.opus.Decoder.packet_get_nb_frames(frame)
                per = discord.opus.Decoder.packet_get_samples_per_frame(frame)
                shape = (
                    f"nb_frames={count} samples_per_frame={per} "
                    f"total_ms={count * per / 48:.1f}"
                )
            except Exception as error:
                shape = f"opus_parse_error={type(error).__name__}"
            session = getattr(state, "dave_session", None)
            try:
                group = session.get_user_ids() if session is not None else None
            except Exception:
                group = "?"
            try:
                passthrough = (
                    session.can_passthrough(user_id)
                    if session is not None and user_id > 0
                    else None
                )
            except Exception:
                passthrough = "?"
            logger.warning(
                "Frame shape: %s | dave ready=%s version=%s group=%s passthrough=%s "
                "| first12=%s",
                shape,
                getattr(session, "ready", None),
                getattr(state, "dave_protocol_version", None),
                group,
                passthrough,
                frame[:12].hex(" "),
            )
        decoder = self._decoder_for(ssrc)
        if decoder is None:
            self._fail("opus bindings")
            return None
        try:
            pcm48 = self._decode(decoder, frame)
        except Exception as error:
            # A frame Opus rejects may still be valid a couple of bytes in: the
            # DAVE envelope and RTP framing differ between implementations.
            # Trying the offsets is cheap and the decoder rejects anything that
            # is not a parseable packet.
            self._fail("opus")
            self._dump_frame(ssrc, frame, error)
            if self._opus_probe < 3:
                self._opus_probe += 1
                session = getattr(state, "dave_session", None)
                logger.warning(
                    "Opus decode failed for ssrc %s (%s): frame(%d)=%s | "
                    "dave=%s ready=%s version=%s",
                    ssrc,
                    type(error).__name__,
                    len(frame),
                    bytes(frame[:10]).hex(" "),
                    session is not None,
                    getattr(session, "ready", None),
                    getattr(state, "dave_protocol_version", None),
                )
            return None
        if not self._opus_ok:
            self._opus_ok = True
            logger.info(
                "First voice frame decoded: ssrc=%s frame(%d)=%s",
                ssrc,
                len(frame),
                bytes(frame[:10]).hex(" "),
            )
        if not pcm48:
            return None
        # Audio came through end to end, so an earlier stage's complaint is no
        # longer the state of the pipeline.
        self._last_error = None
        return user_id, self._resample(pcm48)

    def _decrypt(
        self,
        state: VoiceConnectionState,
        header: bytes,
        payload: bytes,
        key: bytes,
        user_id: int,
    ) -> Optional[bytes]:
        """Undo the transport encryption, then DAVE if the channel is E2EE."""
        mode = getattr(state, "mode", "") or ""
        frame = self._decrypt_transport(mode, header, payload, key)
        if frame is None:
            return None
        self._stage_transport = frame

        if self._plaintext:
            # Audio that arrived with no transport encryption at all: it is
            # already decoded-ready Opus behind DAVE's passthrough marker, so
            # running it through the E2EE layer again would corrupt it.
            if frame.startswith(DAVE_MAGIC):
                frame = frame[len(DAVE_MAGIC):]
            if not frame:
                self._fail("empty frame")
                return None
            self._stage_dave = frame
            return frame

        session = getattr(state, "dave_session", None)
        ready = bool(
            session is not None
            and _DAVE_READY
            and getattr(session, "ready", False)
        )
        # davey keys every frame by the sender's user id and takes it as an
        # unsigned integer. Audio whose ssrc no SPEAKING event named is tracked
        # under a negative placeholder, which davey rejects before touching the
        # frame — so the call achieved nothing while logging a failure that
        # looked like the audio itself was unreadable.
        if ready and user_id < 0:
            if not self._dave_unknown_logged:
                self._dave_unknown_logged = True
                logger.info(
                    "DAVE is negotiated but ssrc %s has no SPEAKING event, so "
                    "its audio is passed through without per-user decryption",
                    -user_id,
                )
            ready = False
        if ready:
            # A ready group decrypts *every* frame, not only the ones carrying
            # the passthrough marker: davey hands plaintext frames back
            # unchanged for speakers still in passthrough and decrypts the rest.
            # Checking the marker first skipped exactly the encrypted frames,
            # so ciphertext reached the Opus decoder and came out as static.
            try:
                plain = session.decrypt(user_id, _DAVE_AUDIO, frame)
            except Exception as error:
                plain = None
                unencrypted = "Unencrypted" in type(error).__name__ or "Unencrypted" in str(
                    error
                )
                if not self._dave_error_logged:
                    self._dave_error_logged = True
                    # "UnencryptedWhenPassthroughDisabled" just means davey saw
                    # no DAVE magic on the frame, i.e. this speaker's audio is
                    # plaintext. That is information, not a failure.
                    logger.log(
                        logging.INFO if unencrypted else logging.WARNING,
                        "DAVE decrypt for user %s: %s (frame head %s, version=%s)",
                        user_id,
                        error,
                        bytes(frame[:8]).hex(" "),
                        getattr(state, "dave_protocol_version", None),
                    )
            if plain:
                frame = plain
                if frame.startswith(DAVE_MAGIC):
                    frame = frame[len(DAVE_MAGIC):]
                if not self._dave_ready_logged:
                    self._dave_ready_logged = True
                    logger.info(
                        "DAVE decrypt is working: frame(%d) head %s",
                        len(frame),
                        bytes(frame[:8]).hex(" "),
                    )
        elif frame.startswith(DAVE_MAGIC):
                # Passthrough mode leaves the Opus frame unencrypted behind the
                # marker; with no session those two bytes are all that stands
                # between the packet and the decoder. If the frame is really
                # E2EE and we have no session, the decoder is about to reject
                # ciphertext, so say which case this is.
                if not self._dave_strip_logged:
                    self._dave_strip_logged = True
                    logger.warning(
                        "Voice frame carries DAVE's %s marker but there is no "
                        "usable DAVE session (version=%s); passing it to the "
                        "Opus decoder as passthrough",
                        DAVE_MAGIC.hex(),
                        getattr(state, "dave_protocol_version", None),
                    )
                frame = frame[len(DAVE_MAGIC):]
        if not frame:
            self._fail("empty frame")
            return None
        self._stage_dave = frame
        return frame

    def _dump_frame(self, ssrc: int, frame: bytes, error: Exception) -> None:
        """Write one undecodable frame, stage by stage, for offline analysis."""
        if self._debug_left <= 0:
            return
        self._debug_left -= 1

        def describe(data: bytes) -> str:
            try:
                frames = discord.opus.Decoder.packet_get_nb_frames(data)
                per = discord.opus.Decoder.packet_get_samples_per_frame(data)
                return f"nb_frames={frames} samples_per_frame={per}"
            except Exception as problem:  # not a parseable Opus packet
                return f"parse_error={type(problem).__name__}"

        try:
            from pathlib import Path

            path = Path("data/voice_debug.txt")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"ssrc={ssrc} error={type(error).__name__} len={len(frame)}\n"
                    f"  input      {frame.hex()}\n"
                    f"  input      {describe(frame)}\n"
                    f"  skip2      {frame[2:].hex()}  ({describe(frame[2:])})\n"
                    f"  skip4      {frame[4:].hex()}  ({describe(frame[4:])})\n"
                    f"  transport  {self._stage_transport.hex() if self._stage_transport else ''}\n"
                    f"  dave       {self._stage_dave.hex() if self._stage_dave else ''}\n"
                )
        except Exception:
            logger.debug("Could not write the voice debug dump", exc_info=True)

    @staticmethod
    def _decode(decoder: Any, frame: bytes) -> bytes:
        """Decode one Opus frame from the start of the packet.

        After transport (and DAVE) decryption the payload *is* the Opus packet,
        TOC byte first, so it is decoded as-is. An earlier version scanned
        offsets 0-8 on the connection's first frame and remembered whichever
        one scored "most like speech"; that first frame is usually a tiny
        silence packet, the scan picked offset 4, and every real frame after it
        had its header cut off — which libopus happily turned into broadband
        noise, so the transcriber heard static instead of the speaker.

        Short prefixes are only tried when the frame genuinely will not parse
        at zero, and only for that frame: no offset is carried over.
        """
        try:
            return decoder.decode(frame)
        except Exception:
            for offset in (2, 4):
                if len(frame) <= offset + 2:
                    continue
                try:
                    return decoder.decode(frame[offset:])
                except Exception:
                    continue
            raise

    #: Transport modes this receiver can undo, tried in turn when the one the
    #: connection negotiated does not authenticate. Every one of these is a MAC
    #: or AEAD, so a candidate can only succeed on the frame the sender really
    #: wrote: a wrong key, nonce or mode raises instead of returning garbage.
    _MODES = (
        "aeadxchacha20poly1305rtpsize",
        "aeadaes256gcmrtpsize",
        "xsalsa20poly1305lite",
        "xsalsa20poly1305suffix",
        "xsalsa20poly1305",
    )

    def _decrypt_transport(
        self, mode: str, header: bytes, payload: bytes, key: bytes
    ) -> Optional[bytes]:
        """Undo the transport encryption, discovering the mode if need be.

        The negotiated mode is tried first, then the others. Discord negotiates
        one mode per connection, but when the name this build reports is not the
        encryption actually on the wire every packet fails its authentication
        and the receiver looks deaf. Falling back costs nothing in correctness
        because each candidate is authenticated — the first success is proof.
        """
        self.negotiated_mode = mode
        self._plaintext = False
        normalised = (mode or "").replace("_", "").lower()

        # The negotiated mode first, then the other standard modes, then the
        # layout variants that differ only in where the nonce counter sits or
        # whether the RTP header is authenticated. Every candidate is a MAC or
        # AEAD, so it can only succeed on the frame the sender really wrote: a
        # wrong key, nonce, AAD or mode raises instead of returning garbage.
        attempts: list[tuple[str, Callable[[], Optional[bytes]]]] = [
            (name, lambda n=name: self._decrypt_as(n, header, payload, key))
            for name in [normalised] + [m for m in self._MODES if m != normalised]
        ]
        attempts += self._layout_fns(header, payload, key)
        if self._preferred:
            attempts.sort(key=lambda item: item[0] != self._preferred)

        failures: list[str] = []
        for name, thunk in attempts:
            try:
                frame = thunk()
            except Exception as error:
                failures.append(f"{name}:{type(error).__name__}")
                continue
            if not frame:
                continue
            if self.working_mode != name:
                if name != normalised:
                    logger.warning(
                        "Voice audio decrypts as %s, not the negotiated %r; "
                        "using %s for this connection",
                        name,
                        mode,
                        name,
                    )
                self.working_mode = name
                self._preferred = name
            return frame

        if payload[: len(DAVE_MAGIC)] == DAVE_MAGIC:
            # No cipher can authenticate this because it was never encrypted:
            # Discord sends some audio as plaintext Opus behind DAVE's
            # passthrough marker. Handing it on is safe — the marker is an
            # explicit signal and the Opus decoder rejects anything else.
            if self.working_mode != "plaintext (dave)":
                self.working_mode = "plaintext (dave)"
                logger.warning(
                    "Voice audio is arriving unencrypted behind DAVE's %s marker; "
                    "using it as-is",
                    DAVE_MAGIC.hex(),
                )
            self._plaintext = True
            return bytes(payload)

        self._fail("decrypt")
        self.decrypt_detail = (
            f"negotiated {mode or 'nothing'}, {len(key)}-byte key, "
            f"{len(payload)}-byte payload starting {bytes(payload[:6]).hex(' ')}"
        )
        # Nothing authenticated. Capture the raw packet once or twice: the hex
        # of a real datagram is what settles whether the counter is a suffix, a
        # prefix or absent, which no amount of guessing at cipher names does.
        if self._probe_logs < 3:
            self._probe_logs += 1
            logger.warning(
                "Voice packet did not authenticate: header=%s payload(%d)=%s "
                "key=%d bytes mode=%r tried=%s",
                header.hex(" "),
                len(payload),
                bytes(payload).hex(" "),
                len(key),
                mode,
                ", ".join(failures[:10]),
            )
        elif mode not in self._warned_modes:
            self._warned_modes.add(mode)
            logger.warning(
                "No transport mode could decrypt a voice packet (negotiated %r, "
                "key %d bytes, payload %d bytes): %s",
                mode,
                len(key),
                len(payload),
                ", ".join(failures[:10]),
            )
        return None

    #: Where the authenticated part of the datagram ends. 24 is the header this
    #: receiver parses (12-byte RTP header plus a 0xbede extension); 12 is the
    #: case where the extension bit is set but nothing follows it; 20 and 16
    #: cover an extension whose declared length is off by a word.
    _SPLITS = (24, 12, 20, 16)

    def _layout_fns(
        self, header: bytes, payload: bytes, key: bytes
    ) -> list[tuple[str, Callable[[], Optional[bytes]]]]:
        """Every plausible split, counter order and AAD, with its cipher.

        A captured packet ended `... 02 00 00 00`, `03`, `04` — a 32-bit counter
        written little-endian, where discord.py writes it big-endian — and its
        header set the RTP extension bit. Candidates therefore differ in three
        things that no single guess can settle. All of them are authenticated,
        so the first one that returns a frame is the layout the sender used.
        """
        attempts: list[tuple[str, Callable[[], Optional[bytes]]]] = []
        for split in self._SPLITS:
            if split > len(header):
                continue
            stream = bytes(header[split:]) + bytes(payload)
            if len(stream) < 21:
                continue
            body, raw = stream[:-4], stream[-4:]
            for order in ("le", "be"):
                counter = raw if order == "le" else raw[::-1]
                for aad_name, aad in (
                    (f"h{split}", bytes(header[:split])),
                    ("h12", bytes(header[:12])),
                    ("none", b""),
                ):
                    attempts.append(
                        (
                            f"xchacha-{order}-{aad_name}-s{split}",
                            lambda aad=aad, counter=counter, body=body: (
                                bindings.crypto_aead_xchacha20poly1305_ietf_decrypt(
                                    body, aad, counter + b"\x00" * 20, key
                                )
                            ),
                        )
                    )
                    attempts.append(
                        (
                            f"aesgcm-{order}-{aad_name}-s{split}",
                            lambda aad=aad, counter=counter, body=body: (
                                _aes256gcm_decrypt(
                                    body, aad, counter + b"\x00" * 8, key
                                )
                            ),
                        )
                    )
                attempts.append(
                    (
                        f"xsalsa-{order}-s{split}",
                        lambda counter=counter, body=body: (
                            bindings.crypto_secretbox_open(
                                body, counter + b"\x00" * 20, key
                            )
                        ),
                    )
                )
        return attempts

    @staticmethod
    def _decrypt_as(
        name: str, header: bytes, payload: bytes, key: bytes
    ) -> Optional[bytes]:
        """Decrypt one payload as a single named transport mode."""
        if name == "aeadxchacha20poly1305rtpsize":
            if len(payload) < 5:
                return None
            # discord.py appends the 32-bit counter to the payload, and the
            # 24-byte AEAD nonce is that counter followed by zeros (verified
            # against `VoiceClient._encrypt_aead_xchacha20_poly1305_rtpsize`).
            nonce = bytes(payload[-4:]) + b"\x00" * 20
            return bindings.crypto_aead_xchacha20poly1305_ietf_decrypt(
                bytes(payload[:-4]), header, nonce, key
            )
        if name == "aeadaes256gcmrtpsize":
            if len(payload) < 5:
                return None
            # Same rtpsize layout as xchacha, but the GCM nonce is 12 bytes.
            nonce = bytes(payload[-4:]) + b"\x00" * 8
            return _aes256gcm_decrypt(bytes(payload[:-4]), header, nonce, key)
        if name == "xsalsa20poly1305":
            nonce = header + b"\x00" * 12
            return bindings.crypto_secretbox_open(bytes(payload), nonce, key)
        if name == "xsalsa20poly1305suffix":
            if len(payload) < 25:
                return None
            return bindings.crypto_secretbox_open(
                bytes(payload[:-24]), bytes(payload[-24:]), key
            )
        if name == "xsalsa20poly1305lite":
            if len(payload) < 5:
                return None
            nonce = bytes(payload[-4:]) + b"\x00" * 20
            return bindings.crypto_secretbox_open(bytes(payload[:-4]), nonce, key)
        return None

    def _resample(self, pcm48: bytes) -> bytes:
        """48 kHz stereo s16le -> target rate mono s16le."""
        if self.target_rate == DISCORD_RATE:
            return pcm48
        samples = np.frombuffer(pcm48, dtype=np.int16)
        if samples.size < 2:
            return b""
        if samples.size % 2:
            samples = samples[:-1]
        mono = samples.reshape(-1, 2).mean(axis=1)
        # Cheap anti-alias before dropping two of every three samples; speech
        # intelligibility survives this far better than a bare decimate.
        mono = np.convolve(mono, np.ones(_DECIMATE) / _DECIMATE, mode="same")
        mono = mono[::_DECIMATE]
        return np.clip(mono, -32768, 32767).astype(np.int16).tobytes()


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------
async def connect_listening(
    channel: discord.abc.Connectable,
    *,
    receiver_factory: Callable[[discord.VoiceClient], VoiceReceiver],
    self_deaf: bool = False,
    self_mute: bool = False,
) -> tuple[discord.VoiceClient, VoiceReceiver]:
    """Connect as a listening client and attach a receiver to it.

    The hook has to be in place *before* ``connect`` builds the websocket, so
    it is registered against the channel id and the client picks it up in
    ``create_connection_state``.
    """
    receiver: Optional[VoiceReceiver] = None
    #: SPEAKING frames can arrive while ``connect`` is still running, before
    #: there is a receiver to hand them to, so they are held until it exists.
    early_speaking: dict[int, int] = {}

    async def hook(_ws, message: dict) -> None:
        if message.get("op") != 5:
            return
        payload = message.get("d") or {}
        if receiver is not None:
            receiver.note_speaking(payload)
            return
        try:
            early_speaking[int(payload["ssrc"])] = int(payload["user_id"])
        except (KeyError, TypeError, ValueError):
            pass

    register_channel_hook(channel.id, hook)
    try:
        voice_client = await channel.connect(
            cls=ListeningVoiceClient, self_deaf=self_deaf, self_mute=self_mute
        )
    except Exception:
        unregister_channel_hook(channel.id)
        raise

    attach_speaking_hook(voice_client, hook)

    receiver = receiver_factory(voice_client)
    if early_speaking:
        receiver.ssrc_users.update(early_speaking)
    if not receiver.attach():
        # Never leave the bot sitting in the channel with nobody listening.
        unregister_channel_hook(channel.id)
        try:
            await voice_client.disconnect(force=True)
        except Exception:
            logger.debug("Could not leave the voice channel", exc_info=True)
        raise RuntimeError("The voice connection has no readable socket")
    return voice_client, receiver
