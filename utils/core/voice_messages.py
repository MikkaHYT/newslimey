"""Send an audio file as a real Discord voice message.

Discord marks a voice message with a flag on the message itself and expects the
attachment to be an Ogg Opus stream. discord.py's ``Messageable.send`` has no
parameter for that flag, so this drops to ``handle_message_parameters`` and the
HTTP layer directly.

Kept in its own module (rather than alongside the other view helpers) so a
``,cog reload`` picks it up: an already-imported module is served from
``sys.modules``, which is why a newly added function there wouldn't be visible
until the whole bot restarted.
"""

from __future__ import annotations

import logging
from pathlib import Path

import discord

logger = logging.getLogger("slimey.voice")

#: The message flag that turns an attachment into a voice message. discord.py
#: doesn't name this one, so it is spelled out here. The file also has to be
#: Ogg Opus or Discord will refuse to play it.
VOICE_MESSAGE_FLAG = 1 << 13


async def send_voice_message(
    channel: discord.abc.Messageable,
    path: Path,
    *,
    reference: discord.Message | None = None,
) -> discord.Message | None:
    """Post ``path`` as a Discord voice message, or give up quietly.

    Any failure — no permission, a server that won't take voice notes, an
    unreadable file — just returns ``None`` so the caller keeps whatever it
    already sent.
    """
    from discord.http import handle_message_parameters

    state = getattr(channel, "_state", None)
    if state is None or not hasattr(channel, "id"):
        return None

    parameters: dict[str, object] = {
        "file": discord.File(str(path), filename="voice-message.ogg"),
        "flags": discord.MessageFlags._from_value(VOICE_MESSAGE_FLAG),
    }
    if reference is not None:
        parameters["message_reference"] = reference.to_message_reference_dict()

    params = handle_message_parameters(**parameters)
    try:
        with params:
            data = await state.http.send_message(channel.id, params=params)
    except (discord.HTTPException, OSError, ValueError) as error:
        logger.info("Could not send a voice message: %s", error)
        return None

    try:
        return state.create_message(channel=channel, data=data)
    except Exception:  # pragma: no cover - the message already exists
        return None
