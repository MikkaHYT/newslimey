"""Streaming speech-to-text over Deepgram's websocket API.

One stream per speaker: the caller hands this client 16 kHz mono PCM and gets
back finished utterances. Partials are optional and only used for the "someone
is talking" signal, because every utterance is spoken by a known member and the
speaker label comes from the caller, not from diarization.

The connection is kept alive with Deepgram's KeepAlive frames and reopened with
a short backoff if the socket drops, so a long voice session does not go deaf
after one network hiccup.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable, Optional

import aiohttp

logger = logging.getLogger("slimey.vc_stt")

DEEPGRAM_URL = "wss://api.deepgram.com/v1/listen"

#: Audio is 16-bit little-endian mono at this rate (Discord's 48k decimated).
SAMPLE_RATE = 16_000
#: 20 ms of 16 kHz mono s16le.
FRAME_BYTES = SAMPLE_RATE * 2 // 50
#: Never let a stalled socket grow an unbounded backlog: two seconds of audio
#: is plenty of slack before frames start being dropped.
MAX_QUEUED_FRAMES = 100
KEEPALIVE_SECONDS = 8.0


class DeepgramStream:
    """A single speaker's live transcript stream.

    Parameters
    ----------
    api_key:
        Deepgram API key.
    on_utterance:
        Called with the finished text once the speaker stops for long enough.
    on_partial:
        Called with the in-progress text; used only to detect speech activity.
    """

    def __init__(
        self,
        api_key: str,
        *,
        on_utterance: Callable[[str], None],
        on_partial: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        model: str = "nova-3",
        language: str = "en",
        sample_rate: int = SAMPLE_RATE,
        endpointing_ms: int = 300,
        utterance_end_ms: int = 1000,
        url: str = DEEPGRAM_URL,
    ) -> None:
        self.api_key = api_key
        self.url = url
        self.on_utterance = on_utterance
        self.on_partial = on_partial
        self.on_error = on_error
        self.model = model
        self.language = language
        self.sample_rate = sample_rate
        self.endpointing_ms = endpointing_ms
        self.utterance_end_ms = utterance_end_ms

        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue(
            maxsize=MAX_QUEUED_FRAMES
        )
        self._tasks: list[asyncio.Task] = []
        self._closing = False
        self._connected = False
        self._final_text = ""
        #: Utterances delivered, for the session's stats card.
        self.utterances = 0
        self.dropped_frames = 0

    # -- lifecycle -----------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._connected

    def params(self) -> dict[str, str]:
        """The query string Deepgram expects for this stream."""
        return {
            "model": self.model,
            "language": self.language,
            "encoding": "linear16",
            "sample_rate": str(self.sample_rate),
            "channels": "1",
            "interim_results": "true",
            "punctuate": "true",
            "smart_format": "true",
            "vad_events": "true",
            "endpointing": str(self.endpointing_ms),
            "utterance_end_ms": str(self.utterance_end_ms),
        }

    async def start(self) -> bool:
        """Open the first connection. Returns whether it is live."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        self._closing = False
        ok = await self._connect()
        if ok:
            self._tasks = [
                asyncio.create_task(self._sender(), name="stt-sender"),
                asyncio.create_task(self._keeper(), name="stt-keepalive"),
                asyncio.create_task(self._receiver(), name="stt-receiver"),
            ]
        return ok

    async def _connect(self) -> bool:
        assert self._session is not None
        try:
            self._ws = await self._session.ws_connect(
                self.url,
                headers={"Authorization": f"Token {self.api_key}"},
                params=self.params(),
                heartbeat=20.0,
                max_msg_size=0,
            )
        except Exception as error:
            self._connected = False
            logger.warning("Deepgram stream could not connect: %s", error)
            if self.on_error is not None:
                self.on_error(f"stt connect failed: {type(error).__name__}")
            return False
        self._connected = True
        return True

    def push(self, pcm: bytes) -> None:
        """Queue a chunk of audio. Synchronous: callers are on the event loop."""
        if not pcm or self._closing:
            return
        if self._queue.full():
            try:
                self._queue.get_nowait()
                self.dropped_frames += 1
            except asyncio.QueueEmpty:  # pragma: no cover - racy but harmless
                pass
        try:
            self._queue.put_nowait(pcm)
        except asyncio.QueueFull:  # pragma: no cover - guarded above
            self.dropped_frames += 1

    async def _sender(self) -> None:
        while not self._closing:
            frame = await self._queue.get()
            if frame is None:
                break
            if not await self._send_frame(frame):
                # Reconnect once per stall; the audio that piled up meanwhile is
                # still queued behind this frame.
                if await self._reconnect():
                    await self._send_frame(frame)

    async def _send_frame(self, frame: bytes) -> bool:
        ws = self._ws
        if ws is None or ws.closed:
            return False
        try:
            await ws.send_bytes(frame)
        except Exception:
            logger.debug("Deepgram send failed", exc_info=True)
            return False
        return True

    async def _keeper(self) -> None:
        while not self._closing:
            await asyncio.sleep(KEEPALIVE_SECONDS)
            ws = self._ws
            if ws is None or ws.closed:
                continue
            try:
                await ws.send_str(json.dumps({"type": "KeepAlive"}))
            except Exception:
                logger.debug("Deepgram keepalive failed", exc_info=True)

    async def _reconnect(self) -> bool:
        if self._closing:
            return False
        for delay in (0.5, 1.0, 2.0, 4.0):
            await asyncio.sleep(delay)
            if self._closing:
                return False
            if await self._connect():
                logger.info("Deepgram stream reconnected")
                self._tasks.append(
                    asyncio.create_task(self._receiver(), name="stt-receiver")
                )
                return True
        if self.on_error is not None:
            self.on_error("stt disconnected")
        return False

    async def _receiver(self) -> None:
        ws = self._ws
        if ws is None:
            return
        async for message in ws:
            if self._closing:
                break
            if message.type == aiohttp.WSMsgType.TEXT:
                self._handle_message(message.data)
            elif message.type in (
                aiohttp.WSMsgType.ERROR,
                aiohttp.WSMsgType.CLOSED,
            ):
                break
        self._connected = False

    def _handle_message(self, raw: str) -> None:
        """Parse one Deepgram frame. Kept separate so it can be tested directly."""
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return
        kind = data.get("type")
        if kind == "Results":
            channel = data.get("channel") or {}
            alternatives = channel.get("alternatives") or []
            text = (alternatives[0].get("transcript") or "").strip() if alternatives else ""
            if text:
                self._final_text = text
                if self.on_partial is not None:
                    self.on_partial(text)
            if text and data.get("is_final"):
                self._final_text = text
            if data.get("speech_final"):
                self._flush()
        elif kind == "UtteranceEnd":
            self._flush()
        elif kind == "Error":
            description = data.get("description") or data.get("message") or "unknown"
            logger.warning("Deepgram stream error: %s", description)
            if self.on_error is not None:
                self.on_error(str(description))

    def _flush(self) -> None:
        text = self._final_text.strip()
        self._final_text = ""
        if not text:
            return
        self.utterances += 1
        self.on_utterance(text)

    async def close(self) -> None:
        self._closing = True
        self._connected = False
        ws = self._ws
        if ws is not None and not ws.closed:
            try:
                await ws.send_str(json.dumps({"type": "CloseStream"}))
                await ws.close()
            except Exception:
                pass
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except BaseException:
                pass
        self._tasks.clear()
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
        self._ws = None
