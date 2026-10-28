"""Read-only index of the ``Mixtapes/`` folder.

Layout::

    Mixtapes/
        <Project>/
            ART/    cover art, tracklists, ...
            MP3/    the actual songs

Everything is discovered on :meth:`MixtapeLibrary.reload`, so dropping a new
project folder in and running ``!cog reload mixtapes`` is enough to publish it.
Nothing in here talks to Discord; the cog renders whatever this returns.

The same folder can live in a Cloudflare R2 bucket instead. Set the ``R2_*``
variables described in :mod:`utils.core.r2` and the bucket becomes the source:
the listing is indexed once and mirrored to a local manifest so a restart can
serve ``!project`` instantly, a track is downloaded the first time it is asked
for and kept in ``.cache/mixtapes``, and each project's ``ZIP/`` archive is
served as a link to the bucket's public subdomain instead of being uploaded to
Discord. An archive that is missing from the bucket is built, cached and
published once, so the whole tracklist is only ever packed a single time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Sequence

from utils.core.r2 import R2Client, R2Config, StoredObject, config_note

logger = logging.getLogger("slimey.mixtapes")

ROOT = Path(__file__).resolve().parent.parent.parent
MIXTAPES_DIR = ROOT / "Mixtapes"
ART_DIR_NAME = "ART"
AUDIO_DIR_NAME = "MP3"
ZIP_DIR_NAME = "ZIP"

#: Where a bucket-backed library keeps what it has pulled down and packed.
#: Never committed; see ``.gitignore``.
CACHE_DIR = ROOT / ".cache" / "mixtapes"

#: Scratch space for the previews a single command needs: cover art about to be
#: uploaded. Kept on the project's drive rather than in the system temp
#: directory, because this host keeps the bot on a large volume while the root
#: partition is small and nearly full, and one of these holds tens of megabytes
#: of pictures.
TEMP_DIR = ROOT / ".cache" / "tmp"

#: How long a scratch directory may be left behind before it is swept. The
#: cleanup in :meth:`MixtapeLibrary.reload` does not run when the process is
#: killed, so anything from an earlier run is removed when the next one starts;
#: a day is far longer than any single command needs one.
TEMP_MAX_AGE = 24 * 60 * 60
MANIFEST_NAME = "index.json"
MANIFEST_VERSION = 1
#: How long a cached index is trusted before the next look refreshes it.
MANIFEST_TTL = 900.0

#: A project archive only needs repacking when the tracklist behind it changes.
ZIP_SIGNATURE_SUFFIX = ".json"

AUDIO_EXTS = frozenset({".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus"})
IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})

# Filename fragments that make a picture unlikely to be the official cover.
_ART_PENALTIES: tuple[tuple[str, int], ...] = (
    ("tracklist", -60),
    ("track list", -60),
    ("tracklisting", -60),
    ("back", -40),
    ("alt", -15),
    ("alternative", -15),
    ("disc", -10),
    ("scan", -5),
    ("inside", -5),
    ("sticker", -5),
)

# Filename fragments that make a picture likely to be the official cover.
_ART_BONUSES: tuple[tuple[str, int], ...] = (
    ("cover art", 40),
    ("coverart", 40),
    ("official", 30),
    ("offical", 30),  # the folder really does spell it that way
    ("front", 25),
    ("cover", 25),
    ("final", 10),
    ("deluxe", 5),
)

_SEPARATOR = "::"
_MIN_SCORE = 0.45


def _strip_prefix(key: str, prefix: str) -> str:
    """``Mixtapes/Slime WRLD/MP3/a.mp3`` -> ``Slime WRLD/MP3/a.mp3``."""
    prefix = (prefix or "").strip("/")
    if not prefix:
        return key.lstrip("/")
    if key == prefix:
        return ""
    if key.startswith(f"{prefix}/"):
        return key[len(prefix) + 1 :]
    return key.lstrip("/")


def _safe_part(part: str) -> str:
    """One path segment of a bucket key, safe to put under the cache dir."""
    cleaned = part.replace("\\", "_").strip()
    return cleaned or "_"


def _remote_kind(parts: Sequence[str]) -> str | None:
    """Classify a path inside a project: songs, art, zips, or nothing.

    Mirrors what :meth:`MixtapeLibrary._scan_project` finds on disk: audio in
    ``MP3/`` (or loose in the project), pictures in ``ART/`` (or loose), and the
    archive in ``ZIP/``.
    """
    first = parts[0]
    suffix = Path(parts[-1]).suffix.lower()
    if len(parts) == 1:
        if suffix in AUDIO_EXTS:
            return "songs"
        if suffix in IMAGE_EXTS:
            return "art"
        return None
    if first.upper() == AUDIO_DIR_NAME:
        return "songs" if suffix in AUDIO_EXTS else None
    if first.upper() == ART_DIR_NAME:
        return "art" if suffix in IMAGE_EXTS else None
    if first.upper() == ZIP_DIR_NAME:
        return "zips" if suffix == ".zip" else None
    return None


def _client_from_env() -> R2Client | None:
    """The bucket client, or ``None`` when R2 isn't configured."""
    config = R2Config.from_env()
    return R2Client(config) if config is not None else None


def _temp_root() -> Path:
    """The scratch directory, falling back to the system temp if unusable."""
    try:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        return TEMP_DIR
    except OSError:
        return Path(tempfile.gettempdir())


def _temp_dir(prefix: str) -> Path:
    """A new scratch directory, sweeping the ones earlier runs left behind.

    A bot that is killed never runs the cleanup in
    :meth:`MixtapeLibrary.reload`, so without the sweep these directories
    accumulate for as long as the deployment lives.
    """
    root = _temp_root()
    cutoff = time.time() - TEMP_MAX_AGE
    for stale in root.glob(f"{prefix}*"):
        try:
            if stale.is_dir() and stale.stat().st_mtime < cutoff:
                shutil.rmtree(stale, ignore_errors=True)
        except OSError:
            continue
    return Path(tempfile.mkdtemp(prefix=prefix, dir=root))


def _cache_dir_from_env() -> Path:
    """Where downloads and archives go: ``R2_CACHE_DIR``, else the default.

    Kept off the bot's own folder when asked, so a host with a small disk can
    point the cache at a bigger volume.
    """
    value = os.getenv("R2_CACHE_DIR", "").strip()
    if not value:
        return CACHE_DIR
    path = Path(value).expanduser()
    # Relative paths hang off the project rather than whatever cwd the process
    # happens to have been started from.
    return path if path.is_absolute() else ROOT / path


def normalize(text: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _tokens(text: str) -> set[str]:
    return set(normalize(text).split())


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "mixtape"


def emoji_key(text: str) -> str:
    """The emoji name a project's logo should use.

    ``"Slime WRLD"`` -> ``slimewrld`` and ``"Barter 3"`` -> ``barter3``, so a
    Discord emoji called ``:slimewrld:`` lines up with the folder name without
    anyone having to keep a mapping in sync by hand.
    """
    return re.sub(r"[^a-z0-9]+", "", text.lower())


#: Shown next to a project that has no matching emoji uploaded yet.
DEFAULT_PROJECT_EMOJI = "💿"


def format_size(num_bytes: int) -> str:
    megabytes = num_bytes / (1024 * 1024)
    if megabytes >= 1:
        return f"{megabytes:.1f} MB"
    return f"{round(num_bytes / 1024)} KB"


@dataclass(slots=True, frozen=True)
class Song:
    """One audio file inside a project."""

    title: str
    path: Path
    project: str
    #: Size in the bucket, so a card can show it before anything is downloaded.
    remote_size: int | None = None
    #: Object key in the bucket, when the library is bucket-backed.
    remote_key: str | None = None

    @property
    def size(self) -> int:
        if self.remote_size is not None:
            return self.remote_size
        return self.path.stat().st_size

    @property
    def qualified(self) -> str:
        """The ``Project::Title`` key used by autocomplete and deep links."""
        return f"{self.project}{_SEPARATOR}{self.title}"

    @property
    def filename(self) -> str:
        return self.path.name


@dataclass(slots=True)
class Project:
    """A folder under ``Mixtapes/``."""

    name: str
    path: Path
    songs: list[Song] = field(default_factory=list)
    artwork: Path | None = None
    extras: list[Path] = field(default_factory=list)
    #: The project's ``ZIP/`` archive in the bucket, when it has one.
    zip_key: str | None = None
    zip_size: int = 0

    @property
    def count(self) -> int:
        return len(self.songs)

    @property
    def gallery(self) -> list[Path]:
        """Cover art first, then every other picture found for the project."""
        pics: list[Path] = []
        if self.artwork is not None:
            pics.append(self.artwork)
        pics.extend(self.extras)
        return pics


@dataclass(slots=True)
class TrackInfo:
    """What an mp3 says about itself in its own tags.

    Falls back to the filename and project name when a file carries no tags, so
    a card always has something to show. ``duration`` is in seconds.
    """

    title: str
    artist: str | None = None
    album: str | None = None
    year: str | None = None
    genre: str | None = None
    duration: float | None = None
    has_cover: bool = False
    #: True when the values came from the file's own ID3 tags.
    tagged: bool = False

    @property
    def runtime(self) -> str:
        """``m:ss``, or ``--:--`` when the file wouldn't report a length."""
        if not self.duration:
            return "--:--"
        total = int(self.duration)
        return f"{total // 60}:{total % 60:02d}"

    @property
    def credit(self) -> str:
        """``artist`` and ``album`` on one line, skipping whichever is missing."""
        return " · ".join(part for part in (self.artist, self.album) if part)

    @property
    def details(self) -> str:
        """The small print: genre, year and where the metadata came from."""
        bits = [part for part in (self.genre, self.year) if part]
        return " · ".join(bits)


@dataclass(slots=True)
class PreparedArchive:
    """A zip of a project, built ready to upload.

    The caller decides what to do when it is bigger than the server's upload
    limit - :attr:`size` is there so it can say so instead of handing Discord a
    file it will reject.
    """

    path: Path | None = None
    size: int = 0
    tracks: int = 0
    error: str | None = None
    #: Set when the archive is a link into the bucket rather than a file on the
    #: message, which is how a zip past Discord's upload limit still gets out.
    url: str | None = None
    _tempdir: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and (self.path is not None or self.url is not None)

    def cleanup(self) -> None:
        if self._tempdir:
            shutil.rmtree(self._tempdir, ignore_errors=True)
            self._tempdir = None


@dataclass(slots=True)
class PreparedAudio:
    """An mp3 that is guaranteed to fit Discord's upload limit."""

    path: Path | None = None
    transcoded: bool = False
    note: str | None = None
    error: str | None = None
    _tempdir: str | None = None

    @property
    def ok(self) -> bool:
        return self.path is not None and self.error is None

    def cleanup(self) -> None:
        if self._tempdir:
            shutil.rmtree(self._tempdir, ignore_errors=True)
            self._tempdir = None


class MixtapeLibrary:
    """Scans ``Mixtapes/`` and answers lookups against the result."""

    #: How many re-encoded songs to keep on disk for instant repeat downloads.
    AUDIO_CACHE_LIMIT = 16

    def __init__(
        self,
        root: Path | None = None,
        *,
        cache_dir: Path | None = None,
        client: R2Client | None = None,
    ) -> None:
        self.root = Path(root) if root is not None else MIXTAPES_DIR
        self.cache_dir = (
            Path(cache_dir) if cache_dir is not None else _cache_dir_from_env()
        )
        #: ``None`` reads the folder on disk; a client reads the bucket.
        self.client = client if client is not None else _client_from_env()
        self.manifest_path = self.cache_dir / MANIFEST_NAME
        #: When the index was last built, from the disk or from a listing.
        self.updated_at = 0.0
        #: Set when the bucket is configured but unusable, or when a listing
        #: fails, so ``!library`` can say why instead of just reporting 0
        #: projects as though the bucket were empty.
        self.status_note: str | None = None
        #: ``local path -> bucket key`` for everything currently indexed, so a
        #: download can be looked up from the path the rest of the code holds.
        self._keys: dict[str, str] = {}
        #: The raw listing behind :attr:`projects`, kept so a freshly uploaded
        #: archive can be folded back into the cached manifest.
        self._objects: list[StoredObject] = []
        self.projects: dict[str, Project] = {}
        # Lowercased project name -> the emoji standing in for it in dropdowns.
        # Filled in by the mixtapes cog, which is the only part that talks to
        # Discord; empty here means every project falls back to the default.
        self.emojis: dict[str, object] = {}
        self._art_cache: dict[tuple[str, int, int], Path] = {}
        self._art_tempdir: Path | None = None
        self._audio_cache: OrderedDict[tuple[str, int, int], Path] = OrderedDict()
        self._audio_tempdir: Path | None = None
        # Tag reads and extracted covers, keyed by (path, mtime, size) so an
        # edited file is re-read instead of served stale.
        self._track_cache: OrderedDict[tuple[str, int, int], TrackInfo] = OrderedDict()
        self._cover_cache: dict[tuple[str, int, int], Path] = {}

        if self.client is not None:
            # The cached index, if there is one, comes back right away; the
            # listing that refreshes it runs in the background (see
            # :meth:`refresh`), so a restart never waits on the bucket.
            logger.info(
                "Mixtape library is backed by R2 (%s)", self.client.config.label
            )
            self._load_manifest()
        else:
            # No client, which is normal (a local folder library) unless the
            # R2_* variables are set, in which case this is the reason.
            self.status_note = config_note()
            self.reload()

    @property
    def remote(self) -> bool:
        """Whether the index is built from the bucket instead of the disk."""
        return self.client is not None

    @property
    def source_label(self) -> str:
        """Where the library is reading from, for the ``!library`` card."""
        if self.client is None:
            return f"local folder `{self.root}`"
        return f"Cloudflare R2 `{self.client.config.label}`"

    async def close(self) -> None:
        """Release the bucket connection, if there is one."""
        if self.client is not None:
            await self.client.close()

    # -- scanning --------------------------------------------------------
    def reload(self) -> int:
        """Re-scan the folder and drop every cached re-encode."""
        self.projects.clear()
        self._art_cache.clear()
        self._audio_cache.clear()
        self._track_cache.clear()
        self._cover_cache.clear()
        for attribute in ("_art_tempdir", "_audio_tempdir"):
            directory = getattr(self, attribute)
            if directory is not None:
                shutil.rmtree(directory, ignore_errors=True)
                setattr(self, attribute, None)

        if not self.root.is_dir():
            logger.warning("Mixtapes folder not found: %s", self.root)
            return 0

        for directory in sorted(self.root.iterdir(), key=lambda p: p.name.lower()):
            if not directory.is_dir() or directory.name.startswith((".", "_")):
                continue
            project = self._scan_project(directory)
            if project is not None:
                self.projects[project.name] = project

        logger.info(
            "Indexed %d project(s), %d song(s) from %s",
            len(self.projects),
            self.song_count,
            self.root,
        )
        return len(self.projects)

    def _scan_project(self, directory: Path) -> Project | None:
        songs: list[Song] = []
        for audio_root in self._audio_roots(directory):
            for path in sorted(audio_root.iterdir(), key=lambda p: p.name.lower()):
                if path.is_file() and path.suffix.lower() in AUDIO_EXTS:
                    songs.append(Song(title=path.stem, path=path, project=directory.name))

        pictures = self._pictures(directory)
        if not songs and not pictures:
            return None

        artwork, extras = self._pick_artwork(directory.name, pictures)
        return Project(
            name=directory.name,
            path=directory,
            songs=songs,
            artwork=artwork,
            extras=extras,
        )

    def _audio_roots(self, directory: Path) -> list[Path]:
        mp3_dir = directory / AUDIO_DIR_NAME
        if mp3_dir.is_dir():
            return [mp3_dir]
        # Fall back to searching the project itself for audio files.
        return [directory]

    def _pictures(self, directory: Path) -> list[Path]:
        roots = [directory / ART_DIR_NAME, directory]
        found: list[Path] = []
        seen: set[Path] = set()
        for root in roots:
            if not root.is_dir():
                continue
            for path in sorted(root.iterdir(), key=lambda p: p.name.lower()):
                if (
                    path.is_file()
                    and path.suffix.lower() in IMAGE_EXTS
                    and path not in seen
                ):
                    seen.add(path)
                    found.append(path)
        return found

    def _pick_artwork(
        self, project_name: str, pictures: list[Path]
    ) -> tuple[Path | None, list[Path]]:
        if not pictures:
            return None, []

        def score(path: Path) -> float:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            return self._artwork_score(project_name, path.stem, size)

        ranked = sorted(pictures, key=score, reverse=True)
        return ranked[0], ranked[1:]

    @staticmethod
    def _artwork_score(project_name: str, stem: str, size: int) -> float:
        """Rank a cover candidate. Bigger files are usually the full art."""
        name = normalize(stem)
        score = 0.0
        for fragment, delta in _ART_PENALTIES:
            if fragment in name:
                score += delta
        for fragment, delta in _ART_BONUSES:
            if fragment in name:
                score += delta
        if normalize(project_name) in name:
            score += 5
        score += min(size / (1024 * 1024), 8) / 8
        return score

    # -- bucket-backed indexing ------------------------------------------
    async def refresh(self, *, force: bool = False) -> int:
        """Rebuild the index, from the bucket when one is configured.

        Returns the number of projects. A fresh cached index is reused unless
        ``force`` is set, which is what keeps the first ``!project`` after a
        restart instant instead of waiting on a listing round trip.
        """
        if self.client is None:
            return self.reload()
        if not force and not self._manifest_stale():
            return len(self.projects)

        objects = await self.client.list_objects(self.client.config.prefix)
        if objects is None:
            # Couldn't reach the bucket. Keep serving what is already indexed
            # rather than dropping the whole library on a network blip.
            logger.warning("Keeping the cached mixtape index; the listing failed")
            self.status_note = (
                self.client.last_error
                or "R2 didn't answer the listing; check the credentials and bucket."
            )
            return len(self.projects)

        self.status_note = None
        self._objects = objects
        self._index_remote(objects)
        self._write_manifest()
        logger.info(
            "Indexed %d project(s), %d song(s) from R2 (%s)",
            len(self.projects),
            self.song_count,
            self.client.config.label,
        )
        return len(self.projects)

    def _manifest_stale(self, ttl: float = MANIFEST_TTL) -> bool:
        return (time.time() - self.updated_at) > ttl

    def forget_manifest(self) -> int:
        """Delete the cached listing so the next refresh rebuilds it.

        Only the file on disk goes. The index already in memory keeps serving
        -- it is what a failed re-listing falls back to -- so a reload can
        never leave the library empty. Used by ``,cachereload``; the number
        returned is how many bucket objects were in the dropped listing.
        """
        count = len(self._objects)
        self._objects = []
        # A zero timestamp makes the manifest stale, so the next (even
        # non-forced) refresh lists the source again instead of trusting it.
        self.updated_at = 0.0
        try:
            self.manifest_path.unlink(missing_ok=True)
        except OSError:
            logger.exception("Could not remove the cached mixtape index")
        return count

    def _load_manifest(self) -> None:
        """Bring back the last listing so commands work before the first sync."""
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.info("No cached mixtape index yet; building one now")
            return
        if payload.get("version") != MANIFEST_VERSION:
            return
        prefix = payload.get("prefix")
        if prefix not in (None, "", self.client.config.prefix):
            # The bucket prefix changed, so the cached keys mean nothing now.
            return

        objects = [
            StoredObject(key=str(key), size=int(size))
            for key, size in payload.get("objects", [])
        ]
        self._objects = objects
        self._index_remote(objects)
        try:
            self.updated_at = float(payload.get("generated") or 0.0)
        except (TypeError, ValueError):
            self.updated_at = 0.0
        logger.info(
            "Loaded the cached mixtape index (%d project(s), %d song(s))",
            len(self.projects),
            self.song_count,
        )

    def _write_manifest(self) -> None:
        """Mirror the current listing to disk so the next start is instant."""
        if self.client is None:
            return
        payload = {
            "version": MANIFEST_VERSION,
            "generated": time.time(),
            "prefix": self.client.config.prefix,
            "objects": [[obj.key, obj.size] for obj in self._objects],
        }
        try:
            self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.manifest_path.with_name(f"{MANIFEST_NAME}.part")
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(temporary, self.manifest_path)
        except OSError:
            logger.exception("Could not cache the mixtape index")
            return
        self.updated_at = payload["generated"]

    def _index_remote(self, objects: Sequence[StoredObject]) -> None:
        """Turn a bucket listing into the same shape a disk scan produces."""
        assert self.client is not None
        prefix = self.client.config.prefix
        grouped: dict[str, dict[str, list[StoredObject]]] = {}

        for obj in objects:
            relative = _strip_prefix(obj.key, prefix)
            if not relative or relative.endswith("/"):
                continue
            parts = relative.split("/")
            if len(parts) < 2:
                # A file loose at the bucket root, not inside a project.
                continue
            kind = _remote_kind(parts[1:])
            if kind is None:
                continue
            grouped.setdefault(parts[0], {"songs": [], "art": [], "zips": []})[
                kind
            ].append(obj)

        self.projects.clear()
        self._keys.clear()
        for name in sorted(grouped, key=str.lower):
            group = grouped[name]
            if not group["songs"] and not group["art"]:
                continue
            self.projects[name] = self._remote_project(name, group)

    def _remote_project(
        self, name: str, group: dict[str, list[StoredObject]]
    ) -> Project:
        """Build one project from the objects found under its folder."""
        songs: list[Song] = []
        for obj in sorted(group["songs"], key=lambda item: item.key.lower()):
            path = self._cache_path(obj.key)
            self._keys[str(path)] = obj.key
            songs.append(
                Song(
                    title=Path(obj.key).stem,
                    path=path,
                    project=name,
                    remote_size=obj.size,
                    remote_key=obj.key,
                )
            )

        art: list[tuple[Path, StoredObject]] = []
        for obj in sorted(group["art"], key=lambda item: item.key.lower()):
            path = self._cache_path(obj.key)
            self._keys[str(path)] = obj.key
            art.append((path, obj))

        artwork, extras = self._pick_remote_artwork(name, art)
        archive = self._pick_zip(group["zips"])
        return Project(
            name=name,
            path=self.cache_dir / "files" / _safe_part(name),
            songs=songs,
            artwork=artwork,
            extras=extras,
            zip_key=archive.key if archive is not None else None,
            zip_size=archive.size if archive is not None else 0,
        )

    def _pick_remote_artwork(
        self, project_name: str, art: list[tuple[Path, StoredObject]]
    ) -> tuple[Path | None, list[Path]]:
        """Same cover ranking as the disk scan, using the listing's sizes."""
        if not art:
            return None, []
        ranked = sorted(
            art,
            key=lambda pair: self._artwork_score(
                project_name, pair[0].stem, pair[1].size
            ),
            reverse=True,
        )
        return ranked[0][0], [path for path, _ in ranked[1:]]

    @staticmethod
    def _pick_zip(zips: list[StoredObject]) -> StoredObject | None:
        """The project's archive; sorted so the choice is stable."""
        if not zips:
            return None
        return sorted(zips, key=lambda obj: obj.key.lower())[0]

    def _cache_path(self, key: str) -> Path:
        """Where the object at ``key`` is (or will be) kept on this machine."""
        prefix = self.client.config.prefix if self.client is not None else ""
        relative = _strip_prefix(key, prefix)
        parts = [
            _safe_part(part)
            for part in relative.split("/")
            if part not in ("", ".", "..")
        ]
        return self.cache_dir / "files" / Path(*parts)

    async def ensure_local(self, path: Path | None) -> Path | None:
        """The on-disk copy of ``path``, downloading it from the bucket if needed.

        Everything the library hands out reads from a real file (ffmpeg, PIL,
        ``discord.File``), so a bucket-backed entry is materialised once and
        then served from the cache like any other file.
        """
        if path is None:
            return None
        if path.is_file():
            return path
        if self.client is None:
            return None
        key = self._keys.get(str(path))
        if key is None:
            return None

        data = await self.client.get(key)
        if data is None:
            return None
        try:
            await asyncio.to_thread(_write_bytes, path, data)
        except OSError:
            logger.exception("Could not cache %s", key)
            return None
        return path if path.is_file() else None

    async def _ensure_local(self, path: Path | None) -> bool:
        return await self.ensure_local(path) is not None

    async def local_audio(self, song: Song) -> Path | None:
        """The playable file for ``song``, fetched on first use."""
        return await self.ensure_local(song.path)

    async def song_url(self, song: Song) -> str | None:
        """A shareable link to ``song``'s object in the bucket, if there is one.

        Uses the bucket's public subdomain when one is configured and falls
        back to a presigned URL, so a private bucket still hands out something
        that opens. A local-folder library has no bucket behind it, which is
        what ``None`` says.
        """
        if self.client is None:
            return None
        key = song.remote_key or self._keys.get(str(song.path))
        if not key:
            return None
        return self.client.public_url(key) or await self.client.presign(key)

    async def project_url(self, project: Project) -> str | None:
        """A shareable link to ``project``'s archive in the bucket.

        ``None`` when the library is a local folder or the bucket has no zip
        for the project yet — the cue to send people to ``,project`` instead.
        Nothing is packed or uploaded here, so a lookup stays cheap.
        """
        if project.zip_key is None:
            return None
        return await self._archive_url(project.zip_key)

    def _remember_zip_key(self, key: str, size: int) -> None:
        """Fold a freshly uploaded archive into the cached listing."""
        if any(obj.key == key for obj in self._objects):
            return
        self._objects.append(StoredObject(key=key, size=size))
        self._write_manifest()

    # -- lookups ---------------------------------------------------------
    @property
    def song_count(self) -> int:
        return sum(project.count for project in self.projects.values())

    def project_names(self) -> list[str]:
        return [project.name for project in self.projects.values()]

    def emoji_for(self, project_name: str):
        """The emoji to show for a project in a dropdown.

        Falls back to a plain disc so the option still reads as a project when
        no matching emoji has been uploaded.
        """
        return self.emojis.get(project_name.lower(), DEFAULT_PROJECT_EMOJI)

    def all_songs(self) -> list[Song]:
        return [song for project in self.projects.values() for song in project.songs]

    def is_empty(self) -> bool:
        return not self.projects

    def get_project(self, name: str) -> Project | None:
        if not name:
            return None
        if name in self.projects:
            return self.projects[name]
        wanted = normalize(name)
        for project in self.projects.values():
            if normalize(project.name) == wanted:
                return project
        return None

    def artwork_for(self, song: Song) -> Path | None:
        """The official artwork of the project ``song`` belongs to."""
        project = self.get_project(song.project)
        return project.artwork if project is not None else None

    async def uploadable_artwork(self, path: Path | None, limit: int) -> Path | None:
        """Return ``path`` in a form Discord will accept, or ``None``.

        Covers are often 10-12 MB PNGs, which is over an unboosted server's
        upload limit. Those are re-encoded to a smaller JPEG once and cached for
        the lifetime of the index, so the artwork still shows up everywhere.
        """
        if path is None:
            return None
        # A bucket-backed cover is pulled down once and cached like any other.
        if not await self._ensure_local(path):
            return None
        try:
            stat = path.stat()
        except OSError:
            return None

        if stat.st_size <= limit:
            return path

        key = (str(path), int(stat.st_mtime), limit)
        cached = self._art_cache.get(key)
        if cached is not None and cached.exists():
            return cached

        prepared = await asyncio.to_thread(
            _shrink_image, path, limit, self._art_temp_dir()
        )
        if prepared is None:
            return None

        self._art_cache[key] = prepared
        return prepared

    async def track_info(self, song: Song) -> TrackInfo:
        """Read a song's tags, remembering the answer per file version.

        Probing means spawning ffprobe, so the result is cached against the
        file's size and mtime and dropped when the file changes underneath us.
        """
        if not await self._ensure_local(song.path):
            return _fallback_track_info(song)
        try:
            stat = song.path.stat()
        except OSError:
            return _fallback_track_info(song)

        key = (str(song.path), int(stat.st_mtime), int(stat.st_size))
        cached = self._track_cache.get(key)
        if cached is not None:
            return cached

        info = await read_track_info(song.path)
        if info is None:
            info = _fallback_track_info(song)
        else:
            if not info.title:
                info.title = song.title
            if not info.album:
                # No album tag: the folder it sits in is the mixtape.
                info.album = song.project

        self._track_cache[key] = info
        if len(self._track_cache) > 256:
            for stale in list(self._track_cache)[:64]:
                self._track_cache.pop(stale, None)
        return info

    async def embedded_cover(self, song: Song) -> Path | None:
        """The artwork packed inside the mp3, written out as a JPEG.

        Preferred over the project folder's art because it is the cover the
        track was actually released with. Returns ``None`` when the file has no
        embedded picture or when the extraction fails, which is the cue to fall
        back to :meth:`artwork_for`.
        """
        if not await self._ensure_local(song.path):
            return None
        try:
            stat = song.path.stat()
        except OSError:
            return None

        key = (str(song.path), int(stat.st_mtime), int(stat.st_size))
        cached = self._cover_cache.get(key)
        if cached is not None and cached.exists():
            return cached

        extracted = await asyncio.to_thread(
            _extract_cover, song.path, self._art_temp_dir()
        )
        if extracted is None:
            return None

        self._cover_cache[key] = extracted
        return extracted

    async def cover_for(self, song: Song, limit: int, *, prefer_embedded: bool = True) -> Path | None:
        """The picture to show for a song: its own tag art, else the project's."""
        if prefer_embedded:
            embedded = await self.embedded_cover(song)
            if embedded is not None:
                prepared = await self.uploadable_artwork(embedded, limit)
                if prepared is not None:
                    return prepared
        return await self.uploadable_artwork(self.artwork_for(song), limit)

    async def uploadable_gallery(self, project: Project, limit: int) -> list[Path]:
        """Every uploadable picture for a project, cover art first."""
        prepared: list[Path] = []
        for picture in project.gallery:
            ready = await self.uploadable_artwork(picture, limit)
            if ready is not None:
                prepared.append(ready)
        return prepared

    def _art_temp_dir(self) -> Path:
        if self._art_tempdir is None:
            self._art_tempdir = _temp_dir("slimey-art-")
        return self._art_tempdir

    async def uploadable_audio(self, path: Path, limit: int) -> PreparedAudio:
        """Like :func:`prepare_audio`, but remembers the re-encode.

        Re-encoding a long track takes several seconds, and the project
        dropdown invites people to click through a whole tracklist, so results
        are cached on disk (bounded, oldest evicted first) and the same file is
        handed back instantly the next time.
        """
        if not await self._ensure_local(path):
            return PreparedAudio(error=f"`{path.name}` isn't available any more.")
        try:
            stat = path.stat()
        except OSError as error:
            return PreparedAudio(error=f"Could not read that file: `{error}`")

        if stat.st_size <= limit:
            return PreparedAudio(path=path)

        key = (str(path), int(stat.st_mtime), limit)
        cached = self._audio_cache.get(key)
        if cached is not None and cached.exists():
            self._audio_cache.move_to_end(key)
            return PreparedAudio(
                path=cached,
                transcoded=True,
                note=f"re-encoded to fit Discord's upload limit ({format_size(cached.stat().st_size)})",
            )

        prepared = await prepare_audio(path, limit, workdir=self._audio_temp_dir())
        if not prepared.ok or prepared.path is None or not prepared.transcoded:
            return prepared

        self._audio_cache[key] = prepared.path
        self._evict_audio_cache()
        return prepared

    def _evict_audio_cache(self) -> None:
        while len(self._audio_cache) > self.AUDIO_CACHE_LIMIT:
            _, stale = self._audio_cache.popitem(last=False)
            stale.unlink(missing_ok=True)

    def _audio_temp_dir(self) -> Path:
        if self._audio_tempdir is None:
            self._audio_tempdir = _temp_dir("slimey-mix-")
        return self._audio_tempdir

    def _zip_cache_path(self, name: str) -> Path:
        return self.cache_dir / "zips" / f"{slugify(name)}.zip"

    async def project_zip(self, name: str, songs: Sequence[Song]) -> PreparedArchive:
        """Pack ``songs`` into a single zip named after the project.

        Stored rather than deflated: mp3s are already compressed, so deflating
        them burns CPU for nothing. The archive is built off the event loop into
        the cache directory, and only repacked when the tracklist behind it has
        changed, so the download button can be pressed twice without the whole
        project being zipped again.
        """
        tracks: list[Song] = []
        for song in songs:
            if await self._ensure_local(song.path):
                tracks.append(song)
        if not tracks:
            return PreparedArchive(error="That project has no mp3 files to zip.")

        target = self._zip_cache_path(name)
        signature = _archive_signature(tracks)
        if target.is_file() and _read_signature(target) == signature:
            return PreparedArchive(
                path=target, size=target.stat().st_size, tracks=len(tracks)
            )

        try:
            await asyncio.to_thread(_write_archive, target, tracks)
            size = target.stat().st_size
        except OSError as error:
            logger.exception("Could not build the archive for %s", name)
            return PreparedArchive(error=f"Could not build that zip: `{error}`")

        _write_signature(target, signature)
        return PreparedArchive(path=target, size=size, tracks=len(tracks))

    async def project_archive(
        self, name: str, songs: Sequence[Song]
    ) -> PreparedArchive:
        """The project's zip: a bucket link when possible, else a built file.

        With R2 configured the answer is the public URL for
        ``<prefix>/<Project>/ZIP/<project>.zip``, which has no upload limit to
        worry about. When the bucket has no archive for the project yet, one is
        built, cached and uploaded once, then linked, so the next press is a
        plain link lookup.
        """
        project = self.get_project(name)
        if project is not None and project.zip_key is not None:
            url = await self._archive_url(project.zip_key)
            if url is not None:
                return PreparedArchive(
                    url=url, size=project.zip_size, tracks=project.count
                )

        built = await self.project_zip(name, songs)
        if (
            not built.ok
            or built.path is None
            or project is None
            or self.client is None
        ):
            return built

        key = self.client.key_for(
            f"{_safe_part(project.name)}/{ZIP_DIR_NAME}/{built.path.name}"
        )
        try:
            payload = await asyncio.to_thread(built.path.read_bytes)
        except OSError:
            logger.exception("Could not read the archive for %s", name)
            return built

        if not await self.client.put(key, payload, content_type="application/zip"):
            return built

        project.zip_key = key
        project.zip_size = built.size
        self._remember_zip_key(key, built.size)
        url = await self._archive_url(key)
        if url is None:
            return built
        return PreparedArchive(url=url, size=built.size, tracks=built.tracks)

    async def _archive_url(self, key: str) -> str | None:
        """A link to ``key``: the bucket's domain, or a presigned URL."""
        if self.client is None:
            return None
        return self.client.public_url(key) or await self.client.presign(key)

    def search_projects(self, query: str, limit: int = 25) -> list[Project]:
        return [project for project, _ in self._rank(query, self.projects, limit)]

    def search_songs(self, query: str, limit: int = 25) -> list[Song]:
        """Rank every song across every project against ``query``.

        ``Project::Title`` (as produced by autocomplete) resolves exactly.
        """
        if not query:
            return []
        if _SEPARATOR in query:
            project_name, _, title = query.partition(_SEPARATOR)
            project = self.get_project(project_name)
            if project is not None:
                wanted = normalize(title)
                for song in project.songs:
                    if normalize(song.title) == wanted:
                        return [song]
        index = {song.qualified: song for song in self.all_songs()}
        if not index:
            return []
        return [song for song, _ in self._rank(query, index, limit)]

    def _rank(self, query: str, candidates: dict, limit: int) -> list[tuple]:
        """Score ``candidates`` (mapping display name -> value) against a query."""
        wanted = normalize(query)
        if not wanted:
            return []

        wanted_tokens = _tokens(query)
        scored: list[tuple[object, float]] = []

        for name, value in candidates.items():
            haystack = normalize(name)
            if haystack == wanted:
                score = 1.0
            elif haystack.startswith(wanted):
                score = 0.9
            elif wanted in haystack:
                score = 0.8
            elif wanted_tokens and wanted_tokens <= _tokens(name):
                score = 0.7
            else:
                score = 0.6 * SequenceMatcher(None, wanted, haystack).ratio()
            if score >= _MIN_SCORE:
                scored.append((value, score))

        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:limit]

    def random_song(self) -> Song | None:
        import random

        songs = self.all_songs()
        return random.choice(songs) if songs else None


# ---------------------------------------------------------------------------
# Project archives
# ---------------------------------------------------------------------------
def _write_archive(target: Path, songs: Sequence[Song]) -> None:
    """Write every song into ``target`` at the archive root.

    Built beside the target and moved into place, so an interrupted pack never
    leaves a half-written zip for the cache to hand out later.
    """
    import zipfile

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.part")
    used: set[str] = set()
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_STORED) as archive:
            for song in songs:
                entry = song.path.name
                if entry in used:
                    # Two files can only collide when their extensions differ,
                    # but an archive with a duplicate name would silently lose
                    # one.
                    entry = (
                        f"{song.title}-{song.path.suffix.lstrip('.')}{song.path.suffix}"
                    )
                used.add(entry)
                archive.write(song.path, arcname=entry)
        os.replace(temporary, target)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _archive_signature(songs: Sequence[Song]) -> str:
    """What a cached archive was built from, to detect a changed tracklist."""
    return "|".join(sorted(f"{song.filename}:{song.size}" for song in songs))


def _signature_path(target: Path) -> Path:
    return target.with_name(f"{target.name}{ZIP_SIGNATURE_SUFFIX}")


def _read_signature(target: Path) -> str | None:
    try:
        payload = json.loads(_signature_path(target).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    signature = payload.get("tracks")
    return signature if isinstance(signature, str) else None


def _write_signature(target: Path, signature: str) -> None:
    try:
        _signature_path(target).write_text(
            json.dumps({"tracks": signature}), encoding="utf-8"
        )
    except OSError:
        logger.debug("Could not record the zip signature for %s", target, exc_info=True)


def _write_bytes(path: Path, data: bytes) -> None:
    """Write a downloaded object into the cache, atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.part")
    temporary.write_bytes(data)
    os.replace(temporary, path)


# ---------------------------------------------------------------------------
# Embedded metadata
# ---------------------------------------------------------------------------
_FFPROBE = shutil.which("ffprobe") or "ffprobe"
_FFMPEG = shutil.which("ffmpeg") or "ffmpeg"

#: ffprobe lower-cases tag names, but the key still differs between encoders.
_TITLE_KEYS = ("title", "name", "song")
_ARTIST_KEYS = ("artist", "album_artist", "albumartist", "performer", "composer")
_ALBUM_KEYS = ("album", "album_title", "collection")
_YEAR_KEYS = ("date", "year", "originaldate", "creation_time")
_GENRE_KEYS = ("genre", "style")


def _fallback_track_info(song: Song) -> TrackInfo:
    """What to show when a file has no usable tags at all."""
    return TrackInfo(title=song.title, album=song.project)


def _first_tag(tags: dict[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = tags.get(key)
        if value and str(value).strip():
            return str(value).strip()
    return None


async def read_track_info(path: Path, *, timeout: float = 25.0) -> TrackInfo | None:
    """ffprobe one file for its title, artist, album, length and cover art.

    Returns ``None`` when ffprobe isn't available or the file can't be read, so
    callers can fall back to what the folder itself says.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            _FFPROBE,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError):
        logger.warning("ffprobe is not available; falling back to filenames")
        return None

    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        logger.warning("Timed out reading tags from %s", path.name)
        return None

    if process.returncode != 0 or not stdout:
        return None

    try:
        payload = json.loads(stdout.decode("utf-8", "replace") or "{}")
    except ValueError:
        return None

    container = payload.get("format") or {}
    tags = {
        str(key).lower(): str(value)
        for key, value in (container.get("tags") or {}).items()
    }
    streams = payload.get("streams") or []
    has_cover = any(
        (stream.get("disposition") or {}).get("attached_pic") for stream in streams
    )

    duration: float | None = None
    for candidate in (container.get("duration"), *(s.get("duration") for s in streams)):
        try:
            duration = float(candidate)
        except (TypeError, ValueError):
            continue
        if duration > 0:
            break

    year = _first_tag(tags, _YEAR_KEYS)
    if year and len(year) >= 4:
        year = year[:4]

    return TrackInfo(
        title=_first_tag(tags, _TITLE_KEYS) or "",
        artist=_first_tag(tags, _ARTIST_KEYS),
        album=_first_tag(tags, _ALBUM_KEYS),
        year=year,
        genre=_first_tag(tags, _GENRE_KEYS),
        duration=duration,
        has_cover=has_cover,
        tagged=bool(tags),
    )


def _extract_cover(path: Path, directory: Path) -> Path | None:
    """Write the picture embedded in ``path`` out as a cached JPEG.

    ``-c:v copy`` copies the packed image without re-encoding it, which keeps
    this to a few milliseconds; Pillow then re-saves it as a real JPEG because
    the embedded picture is just as often a PNG, and Discord needs the bytes
    and the extension to agree.
    """
    try:
        stat = path.stat()
    except OSError:
        return None

    target = directory / f"{path.stem}-{stat.st_mtime_ns:x}.jpg"
    if target.exists():
        return target

    from PIL import Image

    raw = directory / f".{path.stem}-{stat.st_mtime_ns:x}.cover"
    command = [
        _FFMPEG,
        "-y",
        "-v",
        "error",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-c:v",
        "copy",
        "-f",
        "image2",
        str(raw),
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("Could not extract artwork from %s", path.name, exc_info=True)
        return None

    if result.returncode != 0 or not raw.exists():
        raw.unlink(missing_ok=True)
        return None

    try:
        with Image.open(raw) as handle:
            handle.convert("RGB").save(target, "JPEG", quality=90)
    except Exception:
        logger.debug("Could not normalise artwork from %s", path.name, exc_info=True)
        raw.unlink(missing_ok=True)
        return None

    raw.unlink(missing_ok=True)
    return target if target.exists() else None


def _shrink_image(path: Path, limit_bytes: int, directory: Path) -> Path | None:
    """Re-encode ``path`` as a JPEG small enough to upload (best effort)."""
    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow is not installed; cannot shrink %s", path)
        return None

    try:
        with Image.open(path) as handle:
            image = handle.convert("RGB")
            original = image.copy()

            scale = 1.0
            quality = 90
            for attempt in range(4):
                if attempt:
                    size = (
                        max(1, int(original.width * scale)),
                        max(1, int(original.height * scale)),
                    )
                    working = original.resize(size, Image.LANCZOS)
                else:
                    working = original

                output = directory / f"{slugify(path.stem)}-{attempt}.jpg"
                working.save(output, "JPEG", quality=quality, optimize=True)
                if output.stat().st_size <= limit_bytes:
                    return output

                output.unlink(missing_ok=True)
                scale *= 0.8
                quality = max(60, quality - 8)
    except Exception:
        logger.exception("Could not shrink %s", path)
        return None

    logger.warning("%s could not be shrunk below the upload limit", path)
    return None


# ---------------------------------------------------------------------------
# Audio sizing
# ---------------------------------------------------------------------------

async def _probe_duration(path: Path) -> float | None:
    """Return the duration of ``path`` in seconds using ffprobe."""
    try:
        process = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return None

    stdout, _ = await process.communicate()
    if process.returncode != 0:
        return None
    try:
        return float(stdout.decode().strip())
    except ValueError:
        return None


# libmp3lame only accepts CBR bitrates from this table and silently rounds
# up to the next one, which is exactly how a "fits the limit" encode ends up
# over the limit. Always snap down instead.
MP3_BITRATES: tuple[int, ...] = (
    32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 192, 224, 256, 320,
)


def snap_bitrate(kbps: float) -> int:
    """Round ``kbps`` down to a bitrate libmp3lame actually supports."""
    supported = [value for value in MP3_BITRATES if value <= kbps]
    return supported[-1] if supported else MP3_BITRATES[0]


async def _encode(source: Path, destination: Path, kbps: int) -> str | None:
    """Encode ``source`` to ``destination`` at ``kbps``; return an error message."""
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-codec:a",
            "libmp3lame",
            "-b:a",
            f"{kbps}k",
            "-id3v2_version",
            "3",
            str(destination),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return "ffmpeg is not installed"

    _, stderr = await process.communicate()
    if process.returncode != 0 or not destination.is_file():
        detail = stderr.decode(errors="ignore").strip().splitlines()
        logger.warning("ffmpeg failed for %s: %s", source, detail[-1] if detail else "?")
        return "ffmpeg could not encode it"
    return None


async def prepare_audio(
    path: Path, limit_bytes: int, *, workdir: Path | None = None
) -> PreparedAudio:
    """Return an mp3 for ``path`` that can be uploaded to Discord.

    Files that already fit are used untouched. Anything bigger is re-encoded
    with ffmpeg at the highest supported bitrate that still fits, so ``!song``
    keeps working on unboosted servers (a 10 MiB upload limit).

    Pass ``workdir`` to own the output yourself (used by the library's cache);
    otherwise a temporary directory is created and cleaned up by
    :meth:`PreparedAudio.cleanup`.
    """
    try:
        size = path.stat().st_size
    except OSError as error:
        return PreparedAudio(error=f"Could not read that file: `{error}`")

    if size <= limit_bytes:
        return PreparedAudio(path=path)

    duration = await _probe_duration(path)
    if not duration:
        return PreparedAudio(
            error=(
                f"`{path.name}` is {format_size(size)}, over the "
                f"{format_size(limit_bytes)} upload limit, and its length could "
                "not be read so it can't be compressed."
            )
        )

    # 0.95 leaves room for container overhead; the retry loop below covers the
    # rest if the encoder still overshoots.
    target = snap_bitrate((limit_bytes * 8 * 0.95) / duration / 1000)
    attempts = [target]
    attempts.extend(
        value for value in reversed(MP3_BITRATES) if value < target
    )

    tempdir = Path(workdir) if workdir is not None else Path(tempfile.mkdtemp(prefix="slimey-mix-"))
    owned = workdir is None
    for kbps in attempts[:3]:
        output = tempdir / f"{slugify(path.stem)}-{kbps}.mp3"
        error = await _encode(path, output, kbps)
        if error is not None:
            if owned:
                shutil.rmtree(tempdir, ignore_errors=True)
            return PreparedAudio(error=f"Compressing `{path.name}` failed: {error}.")
        if output.stat().st_size <= limit_bytes:
            return PreparedAudio(
                path=output,
                transcoded=True,
                note=f"re-encoded to {kbps} kbps to fit Discord's upload limit",
                _tempdir=str(tempdir) if owned else None,
            )
        output.unlink(missing_ok=True)

    if owned:
        shutil.rmtree(tempdir, ignore_errors=True)
    return PreparedAudio(
        error=(
            f"`{path.name}` is {format_size(size)} and even heavily compressed "
            f"it does not fit the {format_size(limit_bytes)} upload limit."
        )
    )
