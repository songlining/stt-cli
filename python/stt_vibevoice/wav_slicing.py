"""Shared, stdlib-only WAV slicing/concatenation helpers.

Refactored out of ``transcribe.py`` (chunking support) so the speaker
identification backend (``speaker_id.py``) can reuse the same slicing logic
for per-speaker segment extraction instead of duplicating it. Everything
here uses only the stdlib ``wave`` module -- no numpy/ffmpeg required -- so
it stays fast and dependency-free for unit tests.
"""

from __future__ import annotations

import wave
from pathlib import Path
from typing import List, Sequence, Tuple, Union

PathLike = Union[str, Path]


def write_wav_slice(source_path: PathLike, start: float, end: float, dest_path: PathLike) -> None:
    """Write the ``[start, end)`` seconds slice of ``source_path`` to ``dest_path``.

    Both paths must be WAV files (or, for ``dest_path``, will be created as
    one). Negative/out-of-range frame indices are clamped rather than
    raising, mirroring the previous inline behavior in ``transcribe.py``.
    """
    source_path = Path(source_path)
    dest_path = Path(dest_path)

    with wave.open(str(source_path), "rb") as source:
        params = source.getparams()
        frame_rate = source.getframerate()
        frame_count = source.getnframes()
        start_frame = max(0, int(start * frame_rate))
        end_frame = min(frame_count, max(0, int(end * frame_rate)))
        source.setpos(min(start_frame, frame_count))
        frames = source.readframes(max(0, end_frame - start_frame))

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(dest_path), "wb") as dest:
        dest.setparams(params)
        dest.writeframes(frames)


def concatenate_wav_segments(
    source_path: PathLike,
    segments: Sequence[Tuple[float, float]],
    dest_path: PathLike,
) -> float:
    """Concatenate the given ``[start, end)`` second ranges of ``source_path``
    into a single WAV file at ``dest_path``, in the given order.

    Returns the total duration (seconds) of audio written. Segments with
    ``end <= start`` are skipped. Raises if ``segments`` is empty or all
    segments are degenerate (nothing written).
    """
    source_path = Path(source_path)
    dest_path = Path(dest_path)

    with wave.open(str(source_path), "rb") as source:
        params = source.getparams()
        frame_rate = source.getframerate()
        frame_count = source.getnframes()

        all_frames = bytearray()
        total_seconds = 0.0
        for start, end in segments:
            if end <= start:
                continue
            start_frame = max(0, int(start * frame_rate))
            end_frame = min(frame_count, max(0, int(end * frame_rate)))
            if end_frame <= start_frame:
                continue
            source.setpos(min(start_frame, frame_count))
            frame_span = end_frame - start_frame
            all_frames += source.readframes(frame_span)
            total_seconds += frame_span / float(frame_rate)

    if total_seconds <= 0:
        raise ValueError("No non-empty segments to concatenate")

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(dest_path), "wb") as dest:
        dest.setparams(params)
        dest.writeframes(bytes(all_frames))

    return total_seconds


def read_pcm16_mono_samples(path: PathLike) -> List[int]:
    """Read a 16-bit PCM WAV file's samples as a flat list of ints.

    If the file has multiple channels, channels are averaged down to mono.
    Used by the ``mfcc-test`` deterministic embedding provider, which needs
    raw sample access without numpy.
    """
    import struct

    path = Path(path)
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        sample_width = handle.getsampwidth()
        frame_count = handle.getnframes()
        raw = handle.readframes(frame_count)

    if sample_width != 2:
        raise ValueError(f"Only 16-bit PCM WAV is supported, got sample width {sample_width}")

    total_samples = len(raw) // 2
    unpacked = struct.unpack(f"<{total_samples}h", raw[: total_samples * 2])

    if channels <= 1:
        return list(unpacked)

    mono: List[int] = []
    for i in range(0, len(unpacked) - channels + 1, channels):
        frame = unpacked[i : i + channels]
        mono.append(sum(frame) // len(frame))
    return mono
