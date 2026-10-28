"""Run Demucs' separator here, where torchaudio cannot write audio files.

``demucs`` reads its input through ``sphn`` and writes each stem with
``torchaudio.save``. The torchaudio installed on this host routes every write
through TorchCodec, whose native library will not load against this build of
torch (``libtorch.so`` is not where it expects to find it), so a plain
``python -m demucs --two-stems vocals`` separates the song and then dies at the
last step with::

    ImportError: TorchCodec is required for save_with_torchcodec.

Separation itself is unaffected — only the file write. This module replaces that
one call with a 16-bit PCM WAV writer built from the standard library, then
hands over to Demucs' own command line, so everything else (model choice,
chunking, ``--two-stems``, output layout) stays exactly as documented upstream.

``utils/core/separate.py`` runs this instead of ``-m demucs``. It is not meant
to be imported by the bot: importing it loads torch, which is the reason
separation happens in a subprocess at all.
"""

from __future__ import annotations

import sys
import wave

import numpy as np
import torch

#: Bits per sample written by :func:`save_wav`; matches the 16-bit PCM that
#: Demucs asks torchaudio for when ``--int24`` is not given.
BITS_PER_SAMPLE = 16


def save_wav(uri: str, src: torch.Tensor, sample_rate: int = 16000, **_: object) -> None:
    """Write ``src`` as 16-bit PCM WAV, standing in for ``torchaudio.save``.

    ``src`` is ``(channels, frames)`` in ``[-1, 1]``, which is the shape and
    range torchaudio takes. Any other keyword Demucs passes (``encoding``,
    ``bits_per_sample``, ``as_float``) is accepted and ignored, because signed
    16-bit PCM is the only format this needs to write.
    """
    samples = src.detach().cpu().numpy()
    if samples.ndim == 1:
        samples = samples[None, :]
    frames = np.ascontiguousarray((np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").T)
    with wave.open(str(uri), "wb") as handle:
        handle.setnchannels(frames.shape[1])
        handle.setsampwidth(BITS_PER_SAMPLE // 8)
        handle.setframerate(int(sample_rate))
        handle.writeframes(frames.tobytes())


def main(argv: list[str] | None = None) -> int:
    """Demucs' command line, with its file writer replaced."""
    import torchaudio

    torchaudio.save = save_wav  # Demucs' only use of torchaudio for writing

    from demucs.separate import main as demucs_main

    sys.argv = ["demucs", *(argv if argv is not None else sys.argv[1:])]
    demucs_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
