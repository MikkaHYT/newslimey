"""AI vocal separation for karaoke: a real instrumental when one can be made.

The DSP chains in :mod:`utils.core.radio` can only notch the band a lead vocal
sits in, which leaves the voice audible on a mix with no usable stereo image. A
separation model removes it properly, so karaoke prefers a separated
instrumental when this box can produce one:

* the backend is whatever is installed — ``demucs`` in this venv's Python, or
  any command named in the ``RADIO_SEPARATOR_CMD`` environment variable;
* the instrumental is written beside the song as
  ``<name>.karaoke.ai.m4a`` and reused from then on, so it is paid for once;
* separation takes far longer than a song, so it never blocks playback:
  :func:`prepare` runs the model in the background and returns immediately,
  and the DSP render keeps playing until the file lands.

Nothing here is required for the radio to work. With no backend installed
:func:`available` is ``False``, :func:`instrumental_for` returns ``None``, and
the DSP chain is used exactly as it was.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger("slimey.separate")

#: Name the separated instrumental is cached under, beside the song. The suffix
#: is fixed rather than versioned: it comes from the model, not from our chain,
#: so a change to the DSP recipe must never throw it away.
INSTRUMENTAL_SUFFIX = ".karaoke.ai.m4a"

#: How long one song may take before the model is given up on. CPU separation
#: of a full track is minutes, not seconds, so this is generous.
SEPARATE_TIMEOUT = 1800.0

#: The command to use, with ``{source}`` and ``{out}`` substituted. Anything
#: that writes the vocal-free audio to ``{out}`` works, which keeps this usable
#: with tools other than demucs (and lets the tests drive it with a stub).
COMMAND_ENV = "RADIO_SEPARATOR_CMD"

_FFMPEG = shutil.which("ffmpeg") or "ffmpeg"

#: The project root, used to keep model weights on the same drive as the code
#: instead of the home directory (which on this host is a small, nearly full
#: root partition — a model download there can fill the disk the OS lives on).
_ROOT = Path(__file__).resolve().parents[2]

#: Where a model may cache its weights, unless the environment already says.
_MODEL_CACHE = {
    "TORCH_HOME": str(_ROOT / ".cache" / "torch"),
    "HF_HOME": str(_ROOT / ".cache" / "huggingface"),
    "XDG_CACHE_HOME": str(_ROOT / ".cache"),
    # Scratch too: the system /tmp is on the small, nearly full root partition,
    # and one song's stems are tens of megabytes.
    "TMPDIR": str(_ROOT / ".cache" / "tmp"),
}


#: Only one model runs at a time. Each run holds a full copy of the framework
#: and the model in memory, and a second concurrent run is what turns a slow
#: separation into an out-of-memory kill.
_ONE_AT_A_TIME = asyncio.Semaphore(1)


def _child_env() -> dict[str, str]:
    """Environment for the separator, with model weights kept in the project."""
    env = dict(os.environ)
    for key, default in _MODEL_CACHE.items():
        env.setdefault(key, default)
        Path(env[key]).mkdir(parents=True, exist_ok=True)
    return env


def _scratch_dir() -> str:
    """A working directory on the project's drive, falling back if it is gone."""
    try:
        scratch = Path(_MODEL_CACHE["TMPDIR"])
        scratch.mkdir(parents=True, exist_ok=True)
        return str(scratch)
    except OSError:
        return tempfile.gettempdir()

#: Preparations already running, keyed by the output file, so two karaoke
#: toggles on the same song only ever start one model run.
_running: dict[Path, asyncio.Task] = {}


def output_path(source: Path) -> Path:
    """Where the separated instrumental for ``source`` is cached.

    Always beside the song, never in a caller's render folder: the station looks
    for it next to the file it is playing, and one location means the cache can
    never be missed (or written twice).
    """
    source = Path(source)
    return source.parent / f"{source.stem}{INSTRUMENTAL_SUFFIX}"


def instrumental_for(source: Path) -> Path | None:
    """The cached separated instrumental for ``source``, or ``None``."""
    if source is None:
        return None
    candidate = output_path(Path(source))
    if candidate.is_file() and candidate.stat().st_size > 0:
        return candidate
    return None


def _demucs_available() -> bool:
    """Whether ``python -m demucs`` can run in this interpreter.

    Probed by looking for the package rather than importing it: demucs pulls in
    the whole framework, which is hundreds of megabytes of resident memory the
    bot itself never needs (separation happens in a subprocess).
    """
    try:
        return importlib.util.find_spec("demucs") is not None
    except Exception:
        return False


def backend() -> str | None:
    """How separation would be run: ``"env"``, ``"demucs"`` or ``None``."""
    if (os.getenv(COMMAND_ENV) or "").strip():
        if shutil.which("ffmpeg"):
            return "env"
    if _demucs_available():
        return "demucs"
    return None


def available() -> bool:
    """Whether a separator is installed, so karaoke can use one at all."""
    return backend() is not None


def _command(source: Path, out: Path) -> list[str]:
    """The argv that turns ``source`` into the instrumental at ``out``."""
    template = (os.getenv(COMMAND_ENV) or "").strip()
    if template:
        return shlex.split(template.format(source=str(source), out=str(out)))
    # demucs writes a directory tree; the vocal stem is dropped and only the
    # accompaniment is copied out, so the caller always gets one audio file.
    raise RuntimeError("demucs is run through _run_demucs")


async def _run(command: list[str], timeout: float, cwd: Path | None = None) -> bool:
    try:
        async with _ONE_AT_A_TIME:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=None if cwd is None else str(cwd),
                env=_child_env(),
            )
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except (OSError, asyncio.TimeoutError) as error:
        logger.info("separation command failed to run: %s", error)
        return False
    if process.returncode != 0:
        logger.info(
            "separation command exited %s: %s",
            process.returncode,
            stderr.decode(errors="ignore")[-300:],
        )
        return False
    return True


async def _run_env(source: Path, out: Path, timeout: float) -> bool:
    if not await _run(_command(source, out), timeout):
        return False
    return out.is_file() and out.stat().st_size > 0


async def _run_demucs(source: Path, out: Path, timeout: float) -> bool:
    """Separate with demucs and keep the accompaniment stem.

    demucs writes ``<tmp>/<model>/<track>/vocals.wav`` and ``no_vocals.wav``,
    so the instrumental is copied out of that tree and encoded to the same m4a
    the DSP renders use.

    It is run through :mod:`utils.core.vocals` rather than ``-m demucs``: this
    host's torchaudio writes through TorchCodec, which does not load here, and
    that module replaces the one call that breaks.
    """
    with tempfile.TemporaryDirectory(dir=_scratch_dir()) as tmp:
        command = [
            sys.executable, "-m", "utils.core.vocals",
            "--two-stems", "vocals",
            "--out", tmp,
            str(source),
        ]
        if not await _run(command, timeout, cwd=_ROOT):
            return False
        stems = list(Path(tmp).glob("*/*/no_vocals.*"))
        if not stems:
            logger.info("demucs produced no accompaniment stem for %s", source.name)
            return False
        return await _transcode(stems[0], out, timeout)


async def _transcode(source: Path, out: Path, timeout: float) -> bool:
    """Encode a separated stem to the m4a the station plays."""
    command = [
        _FFMPEG, "-y", "-v", "error", "-nostdin",
        "-i", str(source),
        "-vn", "-c:a", "aac", "-b:a", "192k",
        str(out),
    ]
    if not await _run(command, timeout):
        return False
    return out.is_file() and out.stat().st_size > 0


async def separate(source: Path) -> Path | None:
    """Run the model once and return the cached instrumental, or ``None``.

    Blocking for as long as the model takes (minutes per song on a CPU box), so
    :func:`prepare` is what callers use; this is the part that has to finish
    for the file to exist.
    """
    source = Path(source)
    if not source.is_file():
        return None
    kind = backend()
    if kind is None:
        return None
    out = output_path(source)
    if out.is_file() and out.stat().st_size > 0:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    logger.info("separating %s with %s (this takes a while)", source.name, kind)
    ok = (
        await _run_env(source, out, SEPARATE_TIMEOUT)
        if kind == "env"
        else await _run_demucs(source, out, SEPARATE_TIMEOUT)
    )
    if not ok:
        out.unlink(missing_ok=True)
        return None
    logger.info("separated instrumental ready: %s", out.name)
    return out


def prepare(source: Path) -> asyncio.Task | None:
    """Start separating ``source`` in the background; return the task.

    Called when karaoke is switched on so the model runs while the DSP render
    plays. The result is picked up by the next karaoke render, which is the
    point: nothing waits for it. ``None`` means it was already cached or no
    backend is installed.
    """
    source = Path(source)
    if not available() or instrumental_for(source) is not None:
        return None
    out = output_path(source)
    existing = _running.get(out)
    if existing is not None and not existing.done():
        return existing
    task = asyncio.get_running_loop().create_task(separate(source))
    _running[out] = task
    task.add_done_callback(lambda _t: _running.pop(out, None))
    return task
