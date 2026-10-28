"""Mixtape commands: songs, projects, artwork and the library listing.

    !song <name>       get a link to any song in any project
    !project <name>    one project, artwork as the thumbnail, song dropdown
    !artwork <name>    the cover art (plus tracklist) for a project
    !projects          every project, with a dropdown to open one
    !random            a random song from anywhere in the library
    !upload            upload an audio file to the bucket and get a link

The bot never re-uploads an mp3: a track is handed out as a link to its object
on the Cloudflare R2 bucket (see ``utils.core.r2``), shortened with the key-free
is.gd API (``utils.core.shorten``) and posted as a link button on the track's
card rather than as a raw URL. A library served from a local folder has no
bucket behind it, so those tracks report that no link is available instead.

``!upload`` is the one exception, and it is the other direction: a member's own
file is put on the bucket under ``uploads/<user>/<file>`` and handed back on a
link button, so servers that delete audio attachments can still share a track
without the file ever living in the channel. With a drive configured (see
``utils.core.drive``) that button opens the drive's public page for the very
same object — inline player, file details and a download button — because the
drive publishes the ``uploads/`` folder; without one it falls back to the
bucket's own link. Either way the object is stored with
``Content-Disposition: attachment``, so the fallback link downloads the mp3
rather than opening a player, and nothing is ever attached back into the
channel.

One extra on ``!project``, owned by ``!admin mixtapes``:

* **Project zip** — a button that packs the whole ``MP3/`` folder into one zip
  named after the project. Discord's upload limit decides whether it can be
  posted, so an oversized archive reports its size instead of failing silently.
  With a Cloudflare R2 bucket behind the library (see ``utils.core.r2``) the
  button instead hands back the archive's link on the bucket's own subdomain,
  which has no upload limit at all; a project whose archive is missing from the
  bucket is packed once, cached and published so the button only ever links.

Every one of these renders a Components V2 layout and is also available as a
slash command through the slash bridge (``cogs.help`` keeps them visible, so
``bridge_bot`` picks them up).

Note on dropdowns: Discord select options only carry a label, description and
emoji — there is no per-option image. The official project artwork is therefore
shown as the section thumbnail (and next to the selected track) instead of
beside each row. A select also holds at most 25 options, so a project with a
longer tracklist (``The YSLeaks`` has 57) is paged: the dropdown shows the
current 25 and a Previous / Next row steps through the rest.

Project emojis fill that gap. A project's emoji is its own name squashed
together, so ``Mixtapes/Slime WRLD/`` expects an emoji called ``:slimewrld:``
and ``Barter 3`` expects ``:barter3:``. Application emojis are preferred over
guild ones because they belong to the bot and therefore render everywhere; a
project with no matching emoji just falls back to a plain disc. Add an entry to
``config.PROJECT_EMOJI_NAMES`` for the rare emoji named something else, then run
``!library`` to pick it up.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from pathlib import Path
from typing import Optional, Sequence

import discord
from discord import app_commands
from discord.ext import commands
from discord.utils import DEFAULT_FILE_SIZE_LIMIT_BYTES

from config import EMBED_COLOR, PREFIX, PROJECT_EMOJI_NAMES
from utils.core import cache_registry
from utils.core.cache_registry import CacheProvider, ReloadResult
from utils.core.helpers import fmtseconds
from utils.core.id3 import read_tags
from utils.core.drive import link_for as drive_link_for
from utils.core.r2 import content_disposition
from utils.core.mixtapes import (
    DEFAULT_PROJECT_EMOJI,
    PreparedArchive,
    Project,
    Song,
    emoji_key,
    format_size,
)
from utils.core.settings import ANTI_MP3_KEY, GLOBAL_SCOPE, MIXTAPE_ZIP_KEY
from utils.core.shorten import shorten
from utils.core.views import SimpleLayout, swap_layout_message

logger = logging.getLogger("slimey.mixtapes")

MAX_OPTIONS = 25
_UNSAFE = re.compile(r"[^A-Za-z0-9_.\- ()]+")
#: Stricter still: what the drive accepts as one folder segment of a key.
_UNSAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")

#: Where member uploads land inside the bucket: ``uploads/<user>/<file>``.
#: The drive publishes this folder, so its bot API can wrap the object in a
#: public preview page (``utils.core.drive``).
UPLOAD_DIR = "uploads"
#: Audio files the ``,upload`` command accepts.
UPLOAD_EXTS = frozenset({".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus"})
#: The whole file is held in memory for the bucket PUT, so cap how big it may be.
MAX_UPLOAD_BYTES = 80 * 1024 * 1024


def _safe_name(value: str, *, max_length: int = 60) -> str:
    cleaned = _UNSAFE.sub("_", value).strip(" ._")
    return (cleaned or "file")[:max_length]


def _partial(emoji: discord.Emoji) -> discord.PartialEmoji:
    """A select-option-sized copy of an emoji.

    ``SelectOption`` only needs the name and id, and a ``PartialEmoji``
    serialises down to exactly the ``{name, id}`` pair Discord expects.
    """
    return discord.PartialEmoji(name=emoji.name, id=emoji.id, animated=emoji.animated)


def _art_file(path: Path, used: set[str] | None = None) -> tuple[str, discord.File]:
    """Return an attachment name (unique within ``used``) and its file."""
    stem = _safe_name(path.stem, max_length=48)
    name = f"{stem}{path.suffix.lower()}"
    if used is not None:
        candidate = name
        index = 2
        while candidate in used:
            candidate = f"{Path(name).stem}-{index}{path.suffix.lower()}"
            index += 1
        used.add(candidate)
        name = candidate
    return name, discord.File(path, filename=name)


def _upload_limit(target: commands.Context | discord.Interaction) -> int:
    guild = getattr(target, "guild", None)
    return getattr(guild, "filesize_limit", None) or DEFAULT_FILE_SIZE_LIMIT_BYTES


def _fits(path: Path | None, limit: int) -> bool:
    if path is None:
        return False
    try:
        return path.stat().st_size <= limit
    except OSError:
        return False


def _upload_filename(filename: str) -> str:
    """A safe, URL-friendly bucket filename, keeping the original extension."""
    stem = _safe_name(Path(filename or "audio").stem, max_length=60)
    stem = re.sub(r"\s+", "_", stem).strip("_.-") or "audio"
    suffix = Path(filename or "").suffix.lower() or ".mp3"
    return f"{stem}{suffix}"


def _bucket_user(value: str) -> str:
    """A bucket folder name for a member, in the drive's segment format.

    The drive's bot API documents folder segments as ``[A-Za-z0-9._-]``, so a
    display name with spaces or brackets is folded down to that here rather
    than becoming a key the drive may refuse when the link is asked for.
    """
    cleaned = _UNSAFE_SEGMENT.sub("_", value or "").strip("._-")
    return cleaned[:32].strip("._-") or "member"


def _audio_attachment(message: discord.Message | None) -> discord.Attachment | None:
    """The first audio attachment on ``message``, or ``None``."""
    for attachment in getattr(message, "attachments", None) or []:
        if Path(getattr(attachment, "filename", "") or "").suffix.lower() in UPLOAD_EXTS:
            return attachment
    return None


def _scope(target: commands.Context | discord.Interaction) -> int:
    """The guild a setting belongs to, or the bot-wide scope in DMs."""
    guild = getattr(target, "guild", None)
    return getattr(guild, "id", 0) or GLOBAL_SCOPE


async def _song_link(library: object | None, song: Song) -> str | None:
    """A short bucket link to ``song``, or ``None`` when there isn't one.

    The mp3 itself is never uploaded: the link *is* the delivery. Shortening is
    best-effort (see ``utils.core.shorten``), so a dead shortener still leaves a
    working long URL behind, and a local-folder library returns ``None``.
    """
    if library is None:
        return None
    url = await library.song_url(song)
    if not url:
        return None
    return await shorten(url)


#: Discord rejects a link button whose URL is longer than this — and rejects the
#: whole layout with it. A presigned bucket URL that the shortener couldn't
#: shrink can be this long, which is what :func:`_button_url` guards against.
MAX_BUTTON_URL = 512


def _button_url(url: str | None) -> str | None:
    """``url`` when it fits a link button, else ``None``.

    ``None`` is the cue to print the link in the body instead: a card that
    shows the URL is still useful, one Discord 400s on is not.
    """
    if not url or len(url) > MAX_BUTTON_URL:
        return None
    return url


def _link_button(
    label: str, url: str, *, emoji: str | None = None
) -> discord.ui.Button:
    """A button that opens ``url``, so a card never shows a raw link."""
    button = discord.ui.Button(style=discord.ButtonStyle.link, label=label, url=url)
    if emoji:
        button.emoji = emoji
    return button


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

class _SongSelect(discord.ui.Select):
    def __init__(self, view: "MixtapeView", placeholder: str) -> None:
        self.owner = view
        options = [
            discord.SelectOption(
                label=song.title[:100],
                value=song.qualified[:100],
                description=f"{song.project} · {format_size(song.size)}"[:100],
                # Each track carries its project's logo, which is the only cue
                # that tells projects apart in a mixed search result.
                emoji=view.emoji_for(song.project),
                default=view.selected is not None
                and song.qualified == view.selected.qualified,
            )
            for song in view.page_songs
        ]
        if view.page_count > 1:
            # A select can only hold 25 options, so say which slice this is.
            placeholder = (
                f"{placeholder} — page {view.page + 1}/{view.page_count}"
            )
        super().__init__(
            placeholder=placeholder[:150],
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.owner.download(interaction, self.values[0])


class _PageButton(discord.ui.Button):
    """Steps a dropdown through a tracklist longer than 25 songs."""

    def __init__(self, view: "MixtapeView", *, step: int) -> None:
        self.owner = view
        self.step = step
        target = view.page + step
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Previous" if step < 0 else "Next",
            # Disabled at either end rather than hidden, so the row keeps its
            # shape while someone works through a project.
            disabled=target < 0 or target >= view.page_count,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.owner.change_page(interaction, self.step)


class _ZipButton(discord.ui.Button):
    """Packs the project's whole MP3 folder into one zip on click.

    With a bucket behind the library the answer is a link button to the
    archive's object instead of a file, so nothing is ever uploaded twice.
    """

    def __init__(self, view: "MixtapeView") -> None:
        self.owner = view
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Download all (.zip)",
            emoji="📦",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.owner.send_archive(interaction)


class MixtapeView(discord.ui.LayoutView):
    """A project / search result: artwork thumbnail plus a song dropdown.

    Selecting a track rebuilds the layout with that track's title, cover and
    size, plus a link button to the mp3 on the bucket, so the dropdown doubles
    as a download button.

    ``link`` is that shortened URL — the mp3 file itself is never uploaded.
    ``archive`` adds the button that zips every song in the project together.
    """

    def __init__(
        self,
        *,
        songs: Sequence[Song],
        title: str,
        subtitle: str,
        artwork: Path | None,
        limit: int,
        library: object | None = None,
        invoker: int | None = None,
        selected: Song | None = None,
        note: str | None = None,
        show_art: bool = True,
        link: str | None = None,
        placeholder: str = "Pick a song to download…",
        archive: bool = False,
        page: int = 0,
    ) -> None:
        super().__init__(timeout=900)
        self.options = {
            "songs": list(songs),
            "title": title,
            "subtitle": subtitle,
            "artwork": artwork,
            "limit": limit,
            "library": library,
            "invoker": invoker,
            "selected": selected,
            "note": note,
            "show_art": show_art,
            "link": link,
            "placeholder": placeholder,
            "archive": archive,
            "page": page,
        }
        self.library = library
        self.title = title
        self.subtitle = subtitle
        #: The shortened bucket link to the selected track. The mp3 itself is
        #: never attached — this link is how a track is handed out.
        self.link = link
        #: The same link, kept only while it can be a button. An over-long one
        #: is printed in the body instead of taking the whole card down with it.
        self.button_link = _button_url(link)
        self.songs: list[Song] = list(songs)
        #: 25 is Discord's option cap per select, so a longer tracklist is split
        #: into pages the Previous / Next row walks through.
        self.page_count = max(1, (len(self.songs) + MAX_OPTIONS - 1) // MAX_OPTIONS)
        self.page = max(0, min(page, self.page_count - 1))
        self.invoker = invoker
        self.selected = selected
        self.note = note
        self.archive = bool(archive and songs)
        self.artwork = artwork if (show_art and _fits(artwork, limit)) else None

        container = discord.ui.Container(accent_color=EMBED_COLOR)
        header = f"# {title}\n-# {subtitle}"
        if self.artwork is not None:
            container.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay(header),
                    accessory=discord.ui.Thumbnail(
                        media=f"attachment://{_art_file(self.artwork)[0]}"
                    ),
                )
            )
        else:
            container.add_item(discord.ui.TextDisplay(header))

        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(discord.ui.TextDisplay(self.body()))
        download = self._download_row()
        if download is not None:
            container.add_item(download)

        if self.songs:
            container.add_item(
                discord.ui.ActionRow(_SongSelect(self, placeholder))
            )
        if self.page_count > 1:
            container.add_item(
                discord.ui.ActionRow(
                    _PageButton(self, step=-1), _PageButton(self, step=1)
                )
            )
        if self.archive:
            container.add_item(discord.ui.ActionRow(_ZipButton(self)))
        self.add_item(container)

    # -- rendering -------------------------------------------------------
    def body(self) -> str:
        lines: list[str] = []
        if self.selected is not None:
            # The heading already names the track for the song command, so only
            # repeat it when the layout is about something else.
            if self.title != self.selected.title:
                lines.append(f"### {self.selected.title}")
            lines.append(f"> **Project** {self.selected.project}")
            lines.append(f"> **Size** {format_size(self.selected.size)}")
            lines.append(f"> **File** `{self.selected.filename}`")
            # The download is a button; only a link that couldn't be one is
            # spelled out here, and only a local-folder library has no link.
            if self.link and not self.button_link:
                lines.append(f"> **Download** {self.link}")
            elif not self.link:
                lines.append(
                    "> **Download** unavailable — this track has no bucket link."
                )
        else:
            lines.append(self._tracklist_note())
        if self.note:
            lines.append(f"-# {self.note}")
        return "\n".join(lines)

    @property
    def page_songs(self) -> list[Song]:
        """The slice of :attr:`songs` the dropdown is currently showing."""
        start = self.page * MAX_OPTIONS
        return self.songs[start : start + MAX_OPTIONS]

    def _tracklist_note(self) -> str:
        """The small print under the header: what the dropdown is showing."""
        if self.page_count > 1:
            start = self.page * MAX_OPTIONS + 1
            end = start + len(self.page_songs) - 1
            rest = (
                "the last of them."
                if self.page == self.page_count - 1
                else "use **Next** for the rest."
            )
            return f"-# {len(self.songs)} track(s) — showing {start}-{end}, {rest}"
        return f"-# {len(self.songs)} track(s) — pick one for its link."

    def _download_row(self) -> discord.ui.ActionRow | None:
        """The download link for the selected track, as a button.

        Nothing is added while no track is picked, and nothing when the link is
        too long to be a button (the body prints it in that case). A track with
        no link at all keeps the row's shape with a dead button, so the card
        doesn't silently lose a control someone is looking for.
        """
        if self.selected is None or (self.link and self.button_link is None):
            return None
        if self.button_link:
            return discord.ui.ActionRow(
                _link_button("Download mp3", self.button_link, emoji="📥")
            )
        return discord.ui.ActionRow(
            discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label="No download link",
                emoji="📥",
                disabled=True,
            )
        )

    def artwork_files(self) -> list[discord.File]:
        if self.artwork is None:
            return []
        return [_art_file(self.artwork)[1]]

    def emoji_for(self, project_name: str):
        """The project's logo emoji, or a plain disc when there isn't one."""
        if self.library is None:
            return DEFAULT_PROJECT_EMOJI
        return self.library.emoji_for(project_name)

    def without_art(self) -> "MixtapeView":
        """The same layout without the cover thumbnail (upload fallback)."""
        return MixtapeView(**{**self.options, "show_art": False})

    # -- interaction -----------------------------------------------------
    async def change_page(self, interaction: discord.Interaction, step: int) -> None:
        """Walk the song dropdown to the next or previous page of a project."""
        target = self.page + step
        if not 0 <= target < self.page_count:
            await interaction.response.defer()
            return
        if self.invoker is not None and interaction.user.id != self.invoker:
            await interaction.response.send_message(
                f"Run {PREFIX}project yourself to browse tracks.", ephemeral=True
            )
            return

        if not interaction.response.is_done():
            await interaction.response.defer()

        # Paging away drops the selected track (and its link) so the dropdown
        # goes back to showing the whole tracklist.
        view = MixtapeView(
            **{
                **self.options,
                "page": target,
                "selected": None,
                "link": None,
                "note": None,
            }
        )
        self.stop()
        await swap_layout_message(interaction, view, attachments=view.artwork_files())

    async def download(self, interaction: discord.Interaction, value: str) -> None:
        song = next((item for item in self.songs if item.qualified == value), None)
        if song is None:
            await interaction.response.send_message(
                "That track is no longer in the library.", ephemeral=True
            )
            return
        if self.invoker is not None and interaction.user.id != self.invoker:
            await interaction.response.send_message(
                f"Run {PREFIX}song yourself to download tracks.", ephemeral=True
            )
            return

        # Resolving the link may hit the bucket (for a presigned URL) and the
        # shortener, so acknowledge before doing it.
        if not interaction.response.is_done():
            await interaction.response.defer()

        link = await _song_link(self.library, song)
        # No note for a missing link: the card's body already says so, and the
        # pick is free to be replaced by the next one from the dropdown.
        new_view = MixtapeView(
            **{**self.options, "selected": song, "note": None, "link": link}
        )
        self.stop()
        try:
            await swap_layout_message(
                interaction, new_view, attachments=self.artwork_files()
            )
        except discord.HTTPException:
            # The interaction is already deferred, so say something rather than
            # leaving the user with a spinner.
            logger.exception("Could not show the link for %s", song.title)
            await interaction.followup.send(
                f"Couldn't build a link for `{song.title}` "
                f"({format_size(song.size)}) — try again in a moment.",
                ephemeral=True,
            )

    async def send_archive(self, interaction: discord.Interaction) -> None:
        """Zip every mp3 in this project and post it as ``<Project>.zip``."""
        if self.invoker is not None and interaction.user.id != self.invoker:
            await interaction.response.send_message(
                f"Run {PREFIX}project yourself to download the whole project.",
                ephemeral=True,
            )
            return
        if self.library is None:
            await interaction.response.send_message(
                "That download isn't available any more.", ephemeral=True
            )
            return

        # Zipping a whole tracklist takes a moment, so acknowledge first.
        if not interaction.response.is_done():
            await interaction.response.defer(thinking=True)

        archive = await self.library.project_archive(self.title, self.songs)
        try:
            if not archive.ok:
                await interaction.followup.send(
                    archive.error or "I couldn't build that zip.", ephemeral=True
                )
                return

            # A bucket-backed archive is a link to the subdomain, so it is never
            # too big for Discord to accept as an attachment. The link goes on a
            # button like every other mixtape link.
            if archive.url:
                await interaction.followup.send(
                    view=_archive_layout(self.title, archive)
                )
                return

            if archive.path is None:
                await interaction.followup.send(
                    "I couldn't build that zip.", ephemeral=True
                )
                return

            limit = _upload_limit(interaction)
            if archive.size > limit:
                await interaction.followup.send(
                    f"📦 **{self.title}.zip** is **{format_size(archive.size)}** "
                    f"across {archive.tracks} track(s), which is over this "
                    f"server's {format_size(limit)} upload limit. Use "
                    f"`{PREFIX}song <name>` for one track at a time.",
                    ephemeral=True,
                )
                return

            await interaction.followup.send(
                content=(
                    f"📦 **{self.title}** — {archive.tracks} track(s) · "
                    f"{format_size(archive.size)}"
                ),
                file=discord.File(
                    archive.path, filename=f"{_safe_name(self.title)}.zip"
                ),
            )
        except discord.HTTPException:
            logger.exception("Could not upload the archive for %s", self.title)
            await interaction.followup.send(
                "Discord rejected that upload.", ephemeral=True
            )
        finally:
            archive.cleanup()


class _ProjectSelect(discord.ui.Select):
    def __init__(self, view: "ProjectListView") -> None:
        self.owner = view
        super().__init__(
            placeholder="Open a project…",
            options=[
                discord.SelectOption(
                    label=project.name[:100],
                    value=project.name[:100],
                    description=f"{project.count} song(s)",
                    emoji=(
                        view.library.emoji_for(project.name)
                        if view.library is not None
                        else DEFAULT_PROJECT_EMOJI
                    ),
                )
                for project in view.projects[:MAX_OPTIONS]
            ],
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        project = next(
            (item for item in self.owner.projects if item.name == self.values[0]), None
        )
        if project is None:
            await interaction.response.send_message(
                "That project is no longer in the library.", ephemeral=True
            )
            return
        if self.owner.invoker is not None and interaction.user.id != self.owner.invoker:
            await interaction.response.send_message(
                f"Run {PREFIX}projects yourself to browse.", ephemeral=True
            )
            return

        if not interaction.response.is_done():
            await interaction.response.defer()

        limit = _upload_limit(interaction)
        view = MixtapeView(
            songs=project.songs,
            title=project.name,
            subtitle=f"{project.count} song(s)",
            artwork=await self.owner.library.uploadable_artwork(project.artwork, limit),
            limit=limit,
            library=self.owner.library,
            invoker=self.owner.invoker,
            archive=self.owner.archive,
        )
        self.owner.stop()
        await swap_layout_message(interaction, view, attachments=view.artwork_files())


class ProjectListView(discord.ui.LayoutView):
    """``!projects``: every project with a dropdown that opens one."""

    def __init__(
        self,
        projects: Sequence[Project],
        *,
        library=None,
        invoker: int | None = None,
        archive: bool = True,
        timeout: float = 900.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.projects = list(projects)
        self.library = library
        self.invoker = invoker
        # Carried into whichever project the dropdown opens.
        self.archive = archive

        total_songs = sum(project.count for project in self.projects)
        container = discord.ui.Container(accent_color=EMBED_COLOR)
        container.add_item(
            discord.ui.TextDisplay(
                f"# Mixtapes\n-# {len(self.projects)} project(s) · {total_songs} song(s)"
            )
        )
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))

        lines = [
            f"**{project.name}**\n> {project.count} song(s)"
            + (" · 🎨 artwork" if project.artwork else "")
            for project in self.projects
        ]
        container.add_item(
            discord.ui.TextDisplay("\n".join(lines) or "No projects found.")
        )
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(
            discord.ui.TextDisplay(
                f"-# Pick a project below, or use `{PREFIX}project <name>`."
            )
        )
        if self.projects:
            container.add_item(discord.ui.ActionRow(_ProjectSelect(self)))
        self.add_item(container)


def _archive_layout(title: str, archive: PreparedArchive) -> SimpleLayout:
    """The project's zip on the bucket, as a card with a link button.

    Matches the track cards: the size is in the body and the link is on a
    button, falling back to printing it when the URL is longer than Discord
    accepts on one.
    """
    size = f" · {format_size(archive.size)}" if archive.size else ""
    container = discord.ui.Container(accent_color=EMBED_COLOR)
    container.add_item(
        discord.ui.TextDisplay(
            f"📦 **{title}**\n-# {archive.tracks} track(s){size}"
        )
    )
    link = _button_url(archive.url)
    if link:
        container.add_item(
            discord.ui.ActionRow(_link_button("Download .zip", link, emoji="📦"))
        )
    else:
        container.add_item(discord.ui.TextDisplay(f"> **Download** {archive.url}"))
    return SimpleLayout(container)


def build_artwork_layout(project: Project, names: Sequence[tuple[str, Path]]) -> SimpleLayout:
    """A gallery of the project's cover art (and anything else in ``ART/``)."""
    container = discord.ui.Container(accent_color=EMBED_COLOR)
    header = f"# {project.name}\n-# {project.count} song(s) · {len(names)} image(s)"

    if project.artwork is not None and names:
        container.add_item(
            discord.ui.Section(
                discord.ui.TextDisplay(header),
                accessory=discord.ui.Thumbnail(media=f"attachment://{names[0][0]}"),
            )
        )
    else:
        container.add_item(discord.ui.TextDisplay(header))

    container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
    if names:
        container.add_item(
            discord.ui.MediaGallery(
                *[
                    discord.MediaGalleryItem(
                        media=f"attachment://{name}",
                        description=path.stem[:200],
                    )
                    for name, path in names
                ]
            )
        )
    else:
        container.add_item(
            discord.ui.TextDisplay("No artwork that fits Discord's upload limit.")
        )
    return SimpleLayout(container)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class Mixtapes(commands.Cog):
    """Songs, artwork and full projects from the Mixtapes folder."""

    #: How long to wait before hunting for project emojis again after a miss.
    #: Emojis are usually added after the bot has been running, so giving up on
    #: the first look would mean the dropdowns stay on the fallback until the
    #: next restart.
    EMOJI_RETRY_AFTER = 300.0

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Project emojis are looked up from Discord once, then reused. The lock
        # keeps two commands racing at startup from both hitting the API.
        self._emoji_lock = asyncio.Lock()
        self._emojis_ready = False
        self._emojis_checked_at = 0.0
        # Prefix commands can't declare autocomplete directly; the slash bridge
        # reads these mappings when it builds the app commands.
        for attribute in ("song", "project", "artwork"):
            command = getattr(self, attribute, None)
            if isinstance(command, commands.Command):
                command.autocompletes = {"name": self._value_autocomplete}

    @property
    def library(self):
        return getattr(self.bot, "mixtapes", None)

    # ---------------------------------------------------------------
    # ,cachereload support
    # ---------------------------------------------------------------

    async def cog_load(self) -> None:
        cache_registry.register(
            CacheProvider(
                name="mixtapes",
                label="Mixtape library",
                aliases=("library", "mixtape", "projects"),
                describe=self._describe_mixtape_cache,
                reload=self._reload_mixtape_cache,
            )
        )

    def cog_unload(self) -> None:
        cache_registry.unregister("mixtapes")

    def _describe_mixtape_cache(self) -> str:
        library = self.library
        if library is None:
            return "not loaded"
        return (
            f"{len(library.projects)} project(s) · {library.song_count} song(s) · "
            f"{library.source_label}"
        )

    async def _reload_mixtape_cache(
        self,
        target: Optional[str],
        full: bool,
        progress: cache_registry.ProgressFn,
    ) -> ReloadResult:
        """Drop the cached listing and read the source again.

        This index maps projects to songs rather than to resolved files -- the
        audio is fetched from the bucket when it is asked for -- so the fix is
        a fresh listing, not a re-download. It is also all-or-nothing: the
        source is read in one pass, so a targeted reload has nothing smaller
        to work with.
        """
        result = ReloadResult(label="Mixtape library")
        library = self.library
        if library is None:
            result.note = "no library is loaded"
            return result
        if target is not None:
            result.note = "the whole library is listed in one pass; nothing to target"
            return result

        result.scope = f"the whole library from {library.source_label}"
        await progress("Reading the library and rewriting the index...")
        result.forgotten = library.forget_manifest()
        try:
            result.restored = await library.refresh(force=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[Mixtapes] Cache reload failed")
            result.failed = 1
            result.note = "the listing failed; see the log"
            return result

        note = getattr(library, "status_note", None)
        if note:
            result.failed = 1
            result.note = str(note)
        elif not result.restored:
            result.note = "the listing came back empty"
        logger.info(
            "[Mixtapes] Cache reload finished: %d project(s) indexed",
            result.restored,
        )
        return result

    # -- project emojis --------------------------------------------------
    async def _ensure_emojis(self) -> None:
        """Teach the library which emoji stands in for each project.

        Done lazily rather than in ``cog_load`` because cogs load before the bot
        logs in, and the emoji endpoints need a live connection.
        """
        if self._emojis_ready:
            return
        if time.monotonic() - self._emojis_checked_at < self.EMOJI_RETRY_AFTER:
            return
        async with self._emoji_lock:
            if self._emojis_ready:
                return
            found = await self._resolve_emojis()
            self.library.emojis = found
            if found:
                self._emojis_ready = True
                logger.info("Matched emojis for %d project(s)", len(found))
                return
            # Nothing matched. That usually just means the emojis haven't been
            # uploaded yet, so remember when we looked and try again later
            # rather than either hammering the API every command or giving up
            # on them until the bot is restarted.
            logger.info(
                "No project emojis matched; will look again in %s",
                fmtseconds(self.EMOJI_RETRY_AFTER),
            )
            self._emojis_checked_at = time.monotonic()

    async def _resolve_emojis(self) -> dict[str, object]:
        """Look up ``:slimewrld:`` style emojis for every indexed project.

        Application emojis win: they belong to the bot itself, so they render
        in every server the bot is in (and in DMs), unlike an emoji uploaded to
        one guild. Guild emojis are the fallback for anyone who added them the
        ordinary way.
        """
        candidates: dict[str, discord.PartialEmoji] = {}

        try:
            for emoji in await self.bot.fetch_application_emojis():
                candidates.setdefault(emoji.name.lower(), _partial(emoji))
        except Exception:
            # Not logged in yet, or the app has no emoji scope. The guild list
            # below still covers the common case.
            logger.debug("Application emoji lookup unavailable", exc_info=True)

        for emoji in getattr(self.bot, "emojis", ()):
            candidates.setdefault(emoji.name.lower(), _partial(emoji))

        # ``bot.emojis`` is whatever the gateway cache happens to hold, which
        # can be empty or stale (a fresh start, a reconnect, emojis added since
        # the last GUILD_CREATE). Asking each guild directly is the reliable
        # route, so it runs whenever the cache hasn't already matched anything.
        for guild in getattr(self.bot, "guilds", ()):
            if not self._projects_missing(candidates):
                break
            try:
                for emoji in await guild.fetch_emojis():
                    candidates.setdefault(emoji.name.lower(), _partial(emoji))
            except Exception:
                logger.debug("Emoji fetch failed for guild %s", guild.id, exc_info=True)

        if not candidates:
            return {}

        found: dict[str, object] = {}
        for project in self.library.projects.values():
            key = PROJECT_EMOJI_NAMES.get(project.name) or emoji_key(project.name)
            emoji = candidates.get(key.lower())
            if emoji is not None:
                found[project.name.lower()] = emoji
        return found

    def _projects_missing(self, candidates: dict[str, object]) -> bool:
        """Whether any project still has no emoji in ``candidates``."""
        for project in self.library.projects.values():
            key = PROJECT_EMOJI_NAMES.get(project.name) or emoji_key(project.name)
            if key.lower() not in candidates:
                return True
        return False

    def missing_emojis(self) -> list[str]:
        """Expected emoji names for projects that didn't match one."""
        return [
            PROJECT_EMOJI_NAMES.get(project.name) or emoji_key(project.name)
            for project in self.library.projects.values()
            if project.name.lower() not in self.library.emojis
        ]

    # -- shared helpers --------------------------------------------------
    def _zip_enabled(self, target: commands.Context | discord.Interaction) -> bool:
        """Whether the "download the whole project" button is offered."""
        settings = getattr(self.bot, "dash_settings", None)
        if settings is None:
            return True
        return settings.flag(MIXTAPE_ZIP_KEY, True, guild_id=_scope(target))

    def _project(self, query: str) -> Project | None:
        """Exact match first, then a single unambiguous fuzzy match."""
        project = self.library.get_project(query)
        if project is not None:
            return project
        matches = self.library.search_projects(query, limit=2)
        return matches[0] if len(matches) == 1 else None

    def _unknown_project(self, query: str) -> str:
        close = [project.name for project in self.library.search_projects(query, limit=3)]
        hint = ""
        if close:
            hint = "\n\nDid you mean " + ", ".join(f"`{name}`" for name in close) + "?"
        if self.library.is_empty():
            return "The mixtape library is empty — nothing has been indexed yet."
        return f"I don't know a project called `{query}`.{hint}"

    def _unknown_song(self, query: str) -> str:
        close = [song.title for song in self.library.search_songs(query, limit=3)]
        hint = ""
        if close:
            hint = "\n\nDid you mean " + ", ".join(f"`{title}`" for title in close) + "?"
        return f"I couldn't find a song matching `{query}`.{hint}"

    async def _error(self, ctx: commands.Context, body: str) -> None:
        container = discord.ui.Container(accent_color=EMBED_COLOR)
        container.add_item(discord.ui.TextDisplay(f"❌ {body}"))
        await ctx.send(view=SimpleLayout(container))

    async def _send_view_to(self, send, view: MixtapeView) -> None:
        """Send ``view`` through the ``send`` callable, dropping bad artwork.

        Shared by the prefix commands (``ctx.send``) and the AI, which passes a
        message's own ``send`` so the card lands in the channel it was asked in.
        """
        try:
            await send(view=view, files=view.artwork_files())
        except discord.HTTPException as error:
            if view.artwork is None:
                raise
            logger.warning("Artwork upload failed (%s); retrying without it", error)
            await send(view=view.without_art())

    async def _send_view(self, ctx: commands.Context, view: MixtapeView) -> None:
        await self._send_view_to(ctx.send, view)

    async def send_project_card(
        self, target, name: str, *, invoker: int
    ) -> str | None:
        """Post the `!project` card to ``target``'s channel for ``invoker``.

        Returns ``None`` on success, or a short error message. The card's
        dropdown and buttons only answer ``invoker`` — the same rule the prefix
        command uses — so whoever triggered it is the one who can use it.
        """
        project = self._project(name)
        if project is None:
            return self._unknown_project(name)

        await self._ensure_emojis()
        limit = _upload_limit(target)
        view = MixtapeView(
            songs=project.songs,
            title=project.name,
            subtitle=f"{project.count} song(s)",
            artwork=await self.library.uploadable_artwork(project.artwork, limit),
            limit=limit,
            library=self.library,
            invoker=invoker,
            placeholder="Pick a song to download…",
            archive=self._zip_enabled(target),
        )
        await self._send_view_to(target.send, view)
        return None

    async def send_projects_card(self, target, *, invoker: int) -> str | None:
        """Post the `!projects` card to ``target``'s channel for ``invoker``.

        Returns ``None`` on success, or a short error message when there is
        nothing indexed to list.
        """
        entries = sorted(self.library.projects.values(), key=lambda p: p.name.lower())
        if not entries:
            return "The mixtape library is empty — nothing has been indexed yet."

        await self._ensure_emojis()
        await target.send(
            view=ProjectListView(
                entries,
                library=self.library,
                invoker=invoker,
                archive=self._zip_enabled(target),
            )
        )
        return None

    async def _deliver(
        self,
        ctx: commands.Context,
        song: Song,
        *,
        title: str,
        subtitle: str,
        songs: Sequence[Song],
        placeholder: str,
        note: str | None = None,
    ) -> None:
        """Send ``song`` with a shortened link to it on the bucket."""
        limit = _upload_limit(ctx)
        link = await _song_link(self.library, song)
        view = MixtapeView(
            songs=songs,
            title=title,
            subtitle=subtitle,
            artwork=await self.library.uploadable_artwork(
                self.library.artwork_for(song), limit
            ),
            limit=limit,
            library=self.library,
            invoker=ctx.author.id,
            selected=song,
            note=note,
            link=link,
            placeholder=placeholder,
        )
        await self._send_view(ctx, view)

    # -- autocomplete ----------------------------------------------------
    async def _value_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        current = (current or "").strip()
        data = interaction.data or {}
        command = data.get("name") or getattr(
            getattr(interaction, "command", None), "name", ""
        )
        if command in {"artwork", "project"}:
            return [
                app_commands.Choice(
                    name=(
                        f"{project.name} — {project.count} song(s)"
                        if project.count
                        else project.name
                    )[:100],
                    value=project.name[:100],
                )
                for project in self.library.search_projects(current, limit=MAX_OPTIONS)
            ]

        if not current:
            songs = self.library.all_songs()[:MAX_OPTIONS]
        else:
            songs = self.library.search_songs(current, limit=MAX_OPTIONS)
        return [
            app_commands.Choice(
                name=f"{song.title} · {song.project}"[:100],
                value=song.qualified[:100],
            )
            for song in songs
        ]

    # -- commands --------------------------------------------------------
    @commands.command(name="song", aliases=["track", "mp3"])
    async def song(self, ctx: commands.Context, *, name: str) -> None:
        f"""Get a link to any song in any project.

        Usage: `{PREFIX}song Bad Boy` — partial titles are fine; the closest
        match is sent with a shortened link to it on the bucket.
        """
        if self.library.is_empty():
            await self._error(
                ctx, "The mixtape library is empty — nothing has been indexed yet."
            )
            return

        matches = self.library.search_songs(name, limit=6)
        if not matches:
            await self._error(ctx, self._unknown_song(name))
            return

        # The search is already ranked, so the top hit is the answer. The rest
        # are only mentioned, which keeps `!song` a one-shot lookup with no
        # dropdown to pick through.
        best = matches[0]
        others = [song for song in matches[1:] if song.qualified != best.qualified]
        note = None
        if others:
            names = ", ".join(f"`{song.title}`" for song in others[:3])
            note = f"Also matched {names} — be more specific to pin one down."

        await self._deliver(
            ctx,
            best,
            title=best.title,
            subtitle=best.project,
            songs=[],
            placeholder="",
            note=note,
        )

    @commands.command(name="project", aliases=["mixtape", "proj"])
    async def project(self, ctx: commands.Context, *, name: str) -> None:
        f"""Show a whole project: artwork, track count and a song dropdown.

        Usage: `{PREFIX}project Slime WRLD` — picking a track from the dropdown
        shows it with a download button, without leaving the message.
        """
        error = await self.send_project_card(ctx, name, invoker=ctx.author.id)
        if error is not None:
            await self._error(ctx, error)

    @commands.command(name="artwork", aliases=["art"])
    async def artwork(self, ctx: commands.Context, *, name: str) -> None:
        f"""Show the official artwork for a project.

        Usage: `{PREFIX}artwork Slime WRLD` — the cover art comes first, and the
        tracklist or back cover is included when the folder has one.
        """
        project = self._project(name)
        if project is None:
            await self._error(ctx, self._unknown_project(name))
            return

        limit = _upload_limit(ctx)
        used: set[str] = set()
        files: list[discord.File] = []
        names: list[tuple[str, Path]] = []
        for path in await self.library.uploadable_gallery(project, limit):
            attachment, file = _art_file(path, used)
            names.append((attachment, path))
            files.append(file)

        view = build_artwork_layout(project, names)
        await ctx.send(view=view, files=files)

    @commands.command(name="projects", aliases=["mixtapes", "albums"])
    async def projects(self, ctx: commands.Context) -> None:
        """List every project in the library."""
        error = await self.send_projects_card(ctx, invoker=ctx.author.id)
        if error is not None:
            await self._error(ctx, error)

    @commands.command(name="randomsong", aliases=["random", "shuffle"])
    async def randomsong(self, ctx: commands.Context) -> None:
        """Send a random song from anywhere in the library."""
        song = self.library.random_song()
        if song is None:
            await self._error(
                ctx, "The mixtape library is empty — nothing has been indexed yet."
            )
            return

        project = self.library.get_project(song.project)
        await self._ensure_emojis()
        await self._deliver(
            ctx,
            song,
            title=f"🎲 {song.title}",
            subtitle=song.project,
            songs=project.songs if project else [song],
            placeholder="Pick another song…",
        )

    async def _resolve_reference(self, message: discord.Message) -> discord.Message | None:
        """The message a reply points at, fetching it when Discord didn't cache it."""
        resolved = message.reference.resolved if message.reference else None
        if isinstance(resolved, discord.Message):
            return resolved
        if message.reference and message.reference.message_id:
            try:
                return await message.channel.fetch_message(message.reference.message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        return None

    async def _anti_audio_on(self, guild_id: int) -> bool:
        """Whether this server deletes audio files anyway.

        Two switches can say so: the audio filter's own — the one the
        ``antimp3`` command drives, and the one the filter itself reads — and
        the setting the ported cogs were written against. Either one being on
        means the shared mp3 must not be left sitting in the channel.
        """
        settings = getattr(self.bot, "dash_settings", None)
        if settings is not None and settings.flag(
            ANTI_MP3_KEY, False, guild_id=guild_id
        ):
            return True

        database = getattr(self.bot, "database", None)
        if database is None:
            return False
        try:
            return bool(await database.get_antiaudio_status(int(guild_id)))
        except Exception:
            # A switch we cannot read is not worth leaving an mp3 behind for.
            logger.exception("[Mixtapes] Could not read the audio filter state")
            return False

    async def _cleanup_upload_source(
        self, ctx: commands.Context, source: discord.Message
    ) -> None:
        """Take the shared mp3 back down once the command is finished with it.

        The audio filter is told to leave `upload` messages alone — it would
        otherwise delete the attachment before the command could read it — so
        when the filter is on that cleanup happens here instead: after the file
        has been read and uploaded, however that turned out. Only a message the
        caller actually posted an audio file to is touched.
        """
        if source.guild is None or source.author.id != ctx.author.id:
            return
        if _audio_attachment(source) is None:
            return
        if not await self._anti_audio_on(source.guild.id):
            return
        try:
            await source.delete()
        except discord.HTTPException:
            pass

    @commands.command(name="upload", aliases=["mp3upload", "sharemp3"])
    @commands.guild_only()
    async def upload(self, ctx: commands.Context, *, title: Optional[str] = None) -> None:
        f"""Upload an audio file to the bucket and get a public link.

        Attach the file to the command (or reply to a message that has one) and
        the bot uploads it and hands back the drive's public page for it (or
        the bucket link when no drive is configured) on a button, so the track
        never has to sit in the channel. Usage: `{PREFIX}upload` with the file
        attached.

        A file with no ID3 title is otherwise named after its filename, so a name
        can be given after the command instead: `{PREFIX}upload Night Shift`
        titles that card `Night Shift`. A name given on purpose also wins over a
        tag the file already carries.
        """
        source = ctx.message
        # The optional name stands in for a missing tag (and overrides one when
        # it was given deliberately); blank counts as "no name".
        caption = (title or "").strip()
        client = self.library.client
        if client is None:
            await self._error(
                ctx, "No bucket is configured, so uploads aren't available right now."
            )
            # Nothing could be uploaded, but the message was already waved
            # through the audio filter, so it still gets taken down.
            await self._cleanup_upload_source(ctx, source)
            return

        attachment = _audio_attachment(source)
        if attachment is None and ctx.message.reference is not None:
            replied = await self._resolve_reference(ctx.message)
            if replied is not None:
                attachment = _audio_attachment(replied)
                if attachment is not None:
                    source = replied

        if attachment is None:
            await self._error(
                ctx,
                "Attach an mp3 (or another audio file) to the command, or reply to "
                f"a message with one. Usage: `{PREFIX}upload [title]` with the "
                "file attached.",
            )
            return

        # The audio filter steps aside for `upload`, so the source message is
        # cleared here — after the file has been read and pushed to the bucket,
        # and even when the upload didn't work out.
        try:
            if attachment.size and attachment.size > MAX_UPLOAD_BYTES:
                await self._error(
                    ctx,
                    f"That file is **{format_size(attachment.size)}** — the upload "
                    f"limit is **{format_size(MAX_UPLOAD_BYTES)}**.",
                )
                return

            filename = _upload_filename(attachment.filename)
            folder = _bucket_user(ctx.author.name)
            key = f"{UPLOAD_DIR}/{folder}/{filename}"

            async with ctx.typing():
                try:
                    data = await attachment.read()
                except discord.HTTPException:
                    await self._error(ctx, "I couldn't download that attachment.")
                    return
                uploaded = await client.put(
                    key,
                    data,
                    content_type=attachment.content_type or "audio/mpeg",
                    # Stored as an attachment, so the bucket link below
                    # downloads the track instead of opening a player in a tab.
                    content_disposition=content_disposition(filename),
                )

            if not uploaded:
                await self._error(
                    ctx, "The upload to the bucket failed — try again in a moment."
                )
                return

            # The drive publishes `uploads/` and wraps the same object in a
            # page with a player, so that page is the link to hand out. The
            # bucket's own link stays as the fallback for a bot with no drive
            # configured, and for a drive that is unreachable right now — the
            # file is already uploaded either way.
            drive = await drive_link_for(key)
            url = drive.page_url if drive is not None else ""
            if not url:
                url = await client.download_url(key, filename)
            if not url:
                await self._error(
                    ctx, "The file uploaded, but I couldn't build a link for it."
                )
                return

            # Small and tidy: just the song name, its cover when the mp3 carries
            # one, the size, and a link button to the file. The button is a link
            # (not one that attaches the file back into the channel): on the
            # drive it opens the page with the player and a download button, and
            # on the bucket the object's `Content-Disposition: attachment` makes
            # the click download it rather than opening a player.
            tags = read_tags(data)
            # Whatever was typed after the command wins, then the file's own tag,
            # then the filename as the last resort.
            card_title = caption or tags.title or Path(filename).stem
            # Discord caps an embed title at 256 characters, and the note prefix
            # takes a couple of them.
            card = discord.Embed(
                title=f"🎵 {card_title[:250]}", color=EMBED_COLOR
            )
            card.add_field(name="Size", value=format_size(len(data)), inline=True)

            files: list[discord.File] = []
            if tags.cover:
                card.set_thumbnail(url=f"attachment://{tags.cover_name}")
                files.append(
                    discord.File(io.BytesIO(tags.cover), filename=tags.cover_name)
                )
            else:
                # No embedded artwork, so the card keeps a plain thumbnail. The
                # drive's 1200×630 page preview is deliberately not used: it is
                # a picture of the web page, not of the track.
                icon = ctx.guild.icon or self.bot.user.display_avatar
                card.set_thumbnail(url=icon.url)

            # Name the button after the file that was actually uploaded, so a
            # wav card offers "Download wav" rather than claiming it's an mp3 —
            # a drive link is the same page whatever the format, so it gets a
            # label that says what the page adds.
            file_format = Path(filename).suffix.lower().lstrip(".") or "mp3"
            label = (
                "Preview & download"
                if drive is not None
                else f"Download {file_format}"
            )
            view = discord.ui.View()
            view.add_item(
                discord.ui.Button(
                    label=label,
                    emoji="📥",
                    url=url,
                    style=discord.ButtonStyle.link,
                )
            )

            await ctx.send(embed=card, view=view, files=files)
        finally:
            await self._cleanup_upload_source(ctx, source)

    @commands.command(name="library", aliases=["reindex"])
    async def library_command(self, ctx: commands.Context) -> None:
        """Re-scan the mixtapes and report what was found.

        Reads the Mixtapes folder, or re-lists the bucket when one is
        configured, so a project added either way shows up right away. Also
        picks up any project emoji added since the last look, and names the
        problem when a configured bucket can't be read.
        """
        projects = await self.library.refresh(force=True)
        # Drop the memoised emoji lookup so a freshly uploaded emoji is found
        # right away, without waiting out the retry window.
        self._emojis_ready = False
        self._emojis_checked_at = 0.0
        await self._ensure_emojis()
        matched = len(self.library.emojis)
        missing = self.missing_emojis()

        container = discord.ui.Container(accent_color=EMBED_COLOR)
        container.add_item(
            discord.ui.TextDisplay(
                f"# Mixtapes indexed\n"
                f"> **Source** {self.library.source_label}\n"
                f"> **Projects** {projects}\n"
                f"> **Songs** {self.library.song_count}\n"
                f"> **Project emojis** {matched}/{projects}"
            )
        )
        # A bucket that is set up but unusable otherwise looks exactly like an
        # empty library, so the reason gets its own line above the emoji report.
        note = self.library.status_note
        if note:
            container.add_item(
                discord.ui.Separator(spacing=discord.SeparatorSpacing.small)
            )
            container.add_item(discord.ui.TextDisplay(f"⚠️ {note}"))
        if missing:
            container.add_item(
                discord.ui.Separator(spacing=discord.SeparatorSpacing.small)
            )
            container.add_item(
                discord.ui.TextDisplay(
                    "**No emoji found for**\n"
                    + "\n".join(f"> `:{name}:`" for name in missing)
                    + "\n-# Upload those as bot (application) emojis, or name them\n"
                    "-# explicitly in `config.PROJECT_EMOJI_NAMES`, then re-run "
                    f"`{PREFIX}library`. Projects without an emoji fall back to "
                    f"{DEFAULT_PROJECT_EMOJI}."
                )
            )
        await ctx.send(view=SimpleLayout(container))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Mixtapes(bot))
