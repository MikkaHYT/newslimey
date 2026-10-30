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
        self.dropped = 0
        self.decoded = 0
        #: Frames whose audio arrived without a SPEAKING event naming it.
        self.unidentified = 0
        self._last_error: Optional[str] = None
        #: Encryption modes already reported, so an unknown one is logged once
        #: instead of on every packet.
        self._warned_modes: set[str] = set()

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
    def _on_datagram(self, data: bytes) -> None:
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
        user_id = self.ssrc_users.get(ssrc)
        if user_id is None:
            # No SPEAKING event has named this voice. Decode it anyway under a
            # placeholder identity rather than going deaf: if the audio decodes
            # it was speech, whatever the missing event would have called it.
            user_id = -int(ssrc) or -1
            self.unidentified += 1
            if self._last_error != "unknown speaker":
                self._last_error = "unknown speaker"
                logger.warning(
                    "Voice from ssrc %s has no SPEAKING event; transcribing it "
                    "as an unnamed speaker",
                    ssrc,
                )

        header = packet[:header_len]
        payload = packet[header_len:]
        frame = self._decrypt(state, header, payload, bytes(secret), user_id)
        if frame is None:
            self.dropped += 1
            return None
        decoder = self._decoder_for(ssrc)
        if decoder is None:
            return None
        try:
            pcm48 = decoder.decode(frame)
        except Exception:
            self._last_error = "opus"
            logger.debug("Opus decode failed for ssrc %s", ssrc, exc_info=True)
            return None
        if not pcm48:
            return None
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

        session = getattr(state, "dave_session", None)
        if frame.startswith(DAVE_MAGIC):
            if session is not None and _DAVE_READY:
                try:
                    frame = session.decrypt(user_id, _DAVE_AUDIO, frame)
                    if frame.startswith(DAVE_MAGIC):
                        # Passthrough answers can keep the marker; the decoder
                        # cannot take it either way.
                        frame = frame[len(DAVE_MAGIC):]
                except Exception as error:
                    if self._last_error != "dave":
                        logger.warning(
                            "DAVE decrypt failed for user %s: %s", user_id, error
                        )
                    self._last_error = "dave"
                    return None
            else:
                # Passthrough mode leaves the Opus frame unencrypted behind the
                # marker; with no session those two bytes are all that stands
                # between the packet and the decoder.
                frame = frame[len(DAVE_MAGIC):]
        return frame or None

    def _decrypt_transport(
        self, mode: str, header: bytes, payload: bytes, key: bytes
    ) -> Optional[bytes]:
        """Mirror discord.py's encryption for each transport mode it negotiates.

        The mode is compared with its underscores stripped. discord.py names the
        live mode ``aead_xchacha20_poly1305_rtpsize`` while this check used to
        expect ``aead_xchacha20poly1305_rtpsize``, so no branch matched, every
        packet was decrypted to ``None`` and nothing was ever transcribed.
        """
        normalised = (mode or "").replace("_", "").lower()
        try:
            if normalised == "aeadxchacha20poly1305rtpsize":
                if len(payload) < 5:
                    return None
                # discord.py appends the 32-bit counter to the payload, and the
                # 24-byte AEAD nonce is that counter followed by zeros.
                nonce = bytes(payload[-4:]) + b"\x00" * 20
                return bindings.crypto_aead_xchacha20poly1305_ietf_decrypt(
                    bytes(payload[:-4]), header, nonce, key
                )
            if normalised == "xsalsa20poly1305":
                nonce = header + b"\x00" * 12
                return bindings.crypto_secretbox_open(bytes(payload), nonce, key)
            if normalised == "xsalsa20poly1305suffix":
                if len(payload) < 25:
                    return None
                nonce = bytes(payload[-24:])
                return bindings.crypto_secretbox_open(bytes(payload[:-24]), nonce, key)
            if normalised == "xsalsa20poly1305lite":
                if len(payload) < 5:
                    return None
                nonce = bytes(payload[-4:]) + b"\x00" * 20
                return bindings.crypto_secretbox_open(bytes(payload[:-4]), nonce, key)
        except Exception:
            self._last_error = "decrypt"
            logger.debug("Could not decrypt a voice packet (%s)", mode, exc_info=True)
            return None
        # A mode this receiver does not know: say so once, by name, rather than
        # dropping every frame in silence.
        if mode not in self._warned_modes:
            self._warned_modes.add(mode)
            self._last_error = f"unsupported encryption mode {mode!r}"
            logger.warning(
                "Voice packets use encryption mode %r, which is not supported; "
                "audio cannot be decoded",
                mode,
            )
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
