"""Audio normalization: convert arbitrary input audio to 16kHz mono 16-bit PCM WAV.

Mirrors the ffmpeg invocation used by vibecorder's transcriber
(``ffmpeg -y -i <input> -ar 16000 -ac 1 -c:a pcm_s16le <output>``), but adds a
fast path that skips the ffmpeg subprocess entirely when the input is
already a WAV file matching the target format.

Caveat (documented deliberately): the stdlib ``wave`` module can only tell
us ``sampwidth``, ``framerate``, and ``nchannels`` - it cannot verify the
underlying codec is truly linear PCM (e.g. it won't distinguish PCM from a
WAV-wrapped compressed format in all cases). We treat
``sampwidth == 2 and framerate == 16000 and nchannels == 1`` as "close
enough to already normalized" for the fast path, matching the target
format produced by our own ffmpeg conversion.
"""

from __future__ import annotations

import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Optional, Union

PathLike = Union[str, Path]

TARGET_SAMPLE_RATE = 16000
TARGET_CHANNELS = 1
TARGET_SAMPLE_WIDTH = 2  # bytes -> 16-bit PCM


def is_already_normalized(path: PathLike) -> bool:
    """Return True if ``path`` is a WAV file already matching the target format.

    Returns False (never raises) if the file cannot be opened as a WAV,
    e.g. because it's actually a webm/mp3/m4a file with a misleading
    extension, or a genuinely corrupt file.
    """
    wav_path = Path(path)
    if wav_path.suffix.lower() != ".wav":
        return False

    try:
        with wave.open(str(wav_path), "rb") as handle:
            return (
                handle.getsampwidth() == TARGET_SAMPLE_WIDTH
                and handle.getframerate() == TARGET_SAMPLE_RATE
                and handle.getnchannels() == TARGET_CHANNELS
            )
    except (wave.Error, EOFError, OSError):
        return False


def normalize_audio(
    input_path: PathLike,
    output_path: Optional[PathLike] = None,
    timeout: int = 120,
) -> Path:
    """Normalize ``input_path`` to 16kHz mono 16-bit PCM WAV.

    - If the input is already a WAV file matching the target format, this
      is a no-op: the original path is returned unchanged (no ffmpeg call,
      no file copy).
    - Otherwise, shells out to ``ffmpeg`` to convert the file. If
      ``output_path`` is not given, a temp file is created.
    - Raises ``RuntimeError`` (with captured ffmpeg stderr) if the
      conversion fails, and ``FileNotFoundError`` if ``ffmpeg`` itself is
      not installed.
    """
    src = Path(input_path)
    if not src.exists():
        raise FileNotFoundError(f"Input audio file not found: {src}")

    if is_already_normalized(src):
        return src

    if output_path is not None:
        dest = Path(output_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
    else:
        fd, tmp_name = tempfile.mkstemp(prefix="stt_vibevoice_normalized_", suffix=".wav")
        import os as _os

        _os.close(fd)
        dest = Path(tmp_name)

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-ar",
        str(TARGET_SAMPLE_RATE),
        "-ac",
        str(TARGET_CHANNELS),
        "-c:a",
        "pcm_s16le",
        str(dest),
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise FileNotFoundError(
            "ffmpeg is not installed or not on PATH. Install it with "
            "`brew install ffmpeg` and try again."
        ) from error

    if result.returncode != 0 or not dest.exists():
        stderr = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"ffmpeg failed to normalize '{src}' -> '{dest}' "
            f"(exit code {result.returncode}): {stderr or 'no error output captured'}"
        )

    return dest
