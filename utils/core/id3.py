"""The title and embedded cover art of an mp3, read straight from its ID3 tag.

Only what the upload cards need: the ``TIT2`` / ``TT2`` title frame, the
``TPE1`` / ``TP1`` artist frame and the ``APIC`` / ``PIC`` picture frame, pulled
out of the tag header by hand. No dependency and no full tag parse — an mp3 with
no usable tag just comes back empty, and the caller falls back to the filename.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("slimey.id3")

#: ID3v2.2's three-letter image format -> a MIME type.
_IMAGE_MIME = {
    "JPG": "image/jpeg",
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "GIF": "image/gif",
    "BMP": "image/bmp",
}

#: MIME type -> the extension a Discord attachment should carry.
_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}

#: A frame id -> codec, per the first byte of a text frame.
_CODECS = {0: "latin-1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}


@dataclass(slots=True)
class Mp3Tags:
    """What an ID3 tag had to offer."""

    title: str = ""
    artist: str = ""
    cover: bytes | None = None
    cover_mime: str = ""

    @property
    def cover_name(self) -> str:
        """A safe attachment filename for :attr:`cover`."""
        return f"cover{_MIME_EXT.get(self.cover_mime, '.jpg')}"


def _syncsafe(data: bytes) -> int:
    """An ID3v2 size: four bytes, seven usable bits each."""
    value = 0
    for byte in data:
        value = (value << 7) | (byte & 0x7F)
    return value


def _decode_text(payload: bytes) -> str:
    """A text frame's body: an encoding byte, then the text."""
    if not payload:
        return ""
    codec = _CODECS.get(payload[0], "latin-1")
    try:
        text = payload[1:].decode(codec, errors="replace")
    except Exception:  # pragma: no cover - defensive, decode already tolerant
        return ""
    return text.strip("\x00").strip()


def _picture(frame: bytes, frame_id: bytes) -> tuple[bytes | None, str]:
    """The image bytes and MIME type out of an ``APIC`` / ``PIC`` frame."""
    if len(frame) < 4:
        return None, ""

    encoding = frame[0]
    rest = frame[1:]

    if frame_id == b"PIC":  # ID3v2.2: a three-character image format
        fmt = rest[:3].decode("latin-1", "replace").strip("\x00").upper()
        mime = _IMAGE_MIME.get(fmt, "image/jpeg")
        rest = rest[3:]
    else:
        raw_mime, _, rest = rest.partition(b"\x00")
        mime = raw_mime.decode("latin-1", "replace").strip() or "image/jpeg"

    rest = rest[1:]  # the picture type byte

    # The description is text, so its terminator follows the frame's encoding.
    if encoding in (1, 2):
        index = 0
        while index + 1 < len(rest):
            if rest[index : index + 2] == b"\x00\x00":
                rest = rest[index + 2 :]
                break
            index += 2
        else:
            return None, ""
    else:
        _, _, rest = rest.partition(b"\x00")

    image = rest.strip(b"\x00")
    if len(image) < 100:
        return None, ""
    return image, mime


def read_tags(data: bytes) -> Mp3Tags:
    """Read ``TIT2``/``TT2``, ``TPE1``/``TP1`` and ``APIC``/``PIC`` from ``data``.

    Returns an empty :class:`Mp3Tags` for anything that isn't a readable ID3v2
    tag, so callers can treat "no title", "no artist" and "no cover" the same
    way.
    """
    tags = Mp3Tags()
    if len(data) < 10 or data[:3] != b"ID3":
        return tags

    version = data[3]
    flags = data[5]
    declared = _syncsafe(data[6:10])
    body = data[10 : 10 + declared] if declared else data[10:]

    # An extended header sits before the frames when the flag is set.
    if flags & 0x40 and body:
        if version == 4:
            body = body[_syncsafe(body[:4]) :]
        else:
            body = body[4 + int.from_bytes(body[:4], "big") :]

    index = 0
    three_byte_ids = version == 2
    while index + (6 if three_byte_ids else 10) <= len(body):
        if three_byte_ids:
            frame_id = body[index : index + 3]
            size = int.from_bytes(body[index + 3 : index + 6], "big")
            head = 6
        else:
            frame_id = body[index : index + 4]
            raw_size = body[index + 4 : index + 8]
            size = _syncsafe(raw_size) if version == 4 else int.from_bytes(raw_size, "big")
            head = 10

        if not frame_id.strip(b"\x00"):
            break
        frame = body[index + head : index + head + size]
        index += head + size

        if frame_id in (b"TIT2", b"TT2") and not tags.title:
            tags.title = _decode_text(frame)
        elif frame_id in (b"TPE1", b"TP1") and not tags.artist:
            tags.artist = _decode_text(frame)
        elif frame_id in (b"APIC", b"PIC") and tags.cover is None:
            cover, mime = _picture(frame, frame_id)
            if cover:
                tags.cover, tags.cover_mime = cover, mime

        if tags.title and tags.artist and tags.cover:
            break

    return tags
