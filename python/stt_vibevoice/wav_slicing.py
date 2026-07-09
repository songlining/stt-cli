"""Shared, stdlib-only WAV slicing/concatenation helpers.

Refactored out of ``transcribe.py`` (chunking support) so the speaker
identification backend (``speaker_id.py``) can reuse the same slicing logic
for per-speaker segment extraction instead of duplicating it. Everything
here uses only the stdlib ``wave`` module -- no numpy/ffmpeg required -- so
it stays fast and dependency-free for unit tests.
"""

from __future__ import annotations

import math
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


_LOUDNESS_FRAME_SECONDS = 0.05  # 50ms analysis frames
_LOUDNESS_SILENCE_FLOOR = 0.002  # ignore near-digital-silence frames
_LOUDNESS_LOW_PERCENTILE = 50  # drop quiet frames (pauses, breaths)
_LOUDNESS_HIGH_PERCENTILE = 95  # drop loud outlier frames (clicks, notification dings)


def _percentile(sorted_values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile over an already-sorted sequence (stdlib-only,
    no numpy dependency -- mirrors ``numpy.percentile``'s linear method
    closely enough for loudness estimation purposes)."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = rank - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def _speech_loudness(mono_floats: Sequence[float], frame_rate: int) -> float:
    """Estimates the perceived loudness of the *speech* content in a mono
    signal (values in [-1, 1]), robust to both silence and transient spikes.

    Splits the signal into short frames, computes per-frame RMS, discards
    near-silent frames, then averages the RMS of frames between the 50th
    and 95th percentile of remaining loudness -- this represents "typical
    loud speech" while ignoring pauses/breaths (low end) and transient
    clicks/notification dings (high end) that would otherwise skew a
    plain whole-signal RMS. Returns 0.0 for effectively silent input.
    """
    frame_len = max(1, int(_LOUDNESS_FRAME_SECONDS * frame_rate))
    frame_rms: List[float] = []
    n = len(mono_floats)
    for start in range(0, n - frame_len + 1, frame_len):
        chunk = mono_floats[start : start + frame_len]
        rms = math.sqrt(sum(v * v for v in chunk) / len(chunk))
        if rms > _LOUDNESS_SILENCE_FLOOR:
            frame_rms.append(rms)
    if len(frame_rms) < 10:
        # Too little signal to estimate percentiles meaningfully; fall back
        # to plain RMS over everything (still better than silence -> inf gain).
        if not mono_floats:
            return 0.0
        return math.sqrt(sum(v * v for v in mono_floats) / len(mono_floats))

    frame_rms.sort()
    lo = _percentile(frame_rms, _LOUDNESS_LOW_PERCENTILE)
    hi = _percentile(frame_rms, _LOUDNESS_HIGH_PERCENTILE)
    speech = [v for v in frame_rms if lo <= v <= hi]
    if not speech:
        return frame_rms[len(frame_rms) // 2]
    return math.sqrt(sum(v * v for v in speech) / len(speech))


def normalize_wav_file(
    path: PathLike,
    target_dbfs: float = -19.0,
    max_gain: float = 40.0,
) -> float:
    """Normalizes a 16-bit PCM WAV file in place so its perceived speech
    loudness matches ``target_dbfs`` (dBFS). Preserves channel count and
    sample rate. Uses a percentile-filtered, frame-based loudness estimate
    (see ``_speech_loudness``) rather than plain RMS/peak, so a track with a
    few loud transient spikes (e.g. a system-audio tap picking up
    notification dings) is not mistaken for already being loud enough while
    its actual speech content stays buried far below other tracks.

    Returns the applied linear gain factor (1.0 means no change was needed,
    already at/above target, or the signal was silent).

    Raises if the file is not 16-bit PCM (mirrors ``read_pcm16_mono_samples``).
    """
    import struct

    path = Path(path)
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        sample_width = handle.getsampwidth()
        frame_rate = handle.getframerate()
        frame_count = handle.getnframes()
        raw = handle.readframes(frame_count)

    if sample_width != 2:
        raise ValueError(f"Only 16-bit PCM WAV is supported, got sample width {sample_width}")

    total_samples = len(raw) // 2
    if total_samples == 0:
        return 1.0
    unpacked = struct.unpack(f"<{total_samples}h", raw[: total_samples * 2])

    # Downmix to mono (loudness measurement only; gain is applied per-channel
    # below, so the original channel layout is preserved on write-back).
    if channels <= 1:
        mono_floats = [v / 32768.0 for v in unpacked]
    else:
        mono_floats = []
        for i in range(0, len(unpacked) - channels + 1, channels):
            frame = unpacked[i : i + channels]
            mono_floats.append((sum(frame) / len(frame)) / 32768.0)

    loudness = _speech_loudness(mono_floats, frame_rate)
    if loudness <= 1e-9:
        return 1.0  # silent/near-silent input; nothing sensible to normalize to

    target_linear = 10 ** (target_dbfs / 20.0)
    gain = min(target_linear / loudness, max_gain)
    if gain <= 1.0:
        return 1.0  # already at/above target; never attenuate

    boosted = [max(-32768, min(32767, int(round(v * gain)))) for v in unpacked]
    out_raw = struct.pack(f"<{len(boosted)}h", *boosted)

    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(sample_width)
        handle.setframerate(frame_rate)
        handle.writeframes(out_raw)

    return gain


def rank_ranges_by_energy(
    source_path: PathLike,
    ranges: Sequence[Tuple[float, float]],
) -> List[Tuple[float, float, float]]:
    """Scores each ``[start, end)`` range by its mean RMS energy and returns
    them sorted from loudest to quietest, as ``(start, end, rms)`` triples.

    Used to pick the clearest-speech segments for a preview/sample clip so the
    limited playback budget is spent on actual talking rather than faint or
    near-silent stretches. Reads the source WAV once (downmixed to mono for
    measurement). stdlib-only.
    """
    import struct

    source_path = Path(source_path)
    with wave.open(str(source_path), "rb") as handle:
        channels = handle.getnchannels()
        sample_width = handle.getsampwidth()
        frame_rate = handle.getframerate()
        frame_count = handle.getnframes()
        raw = handle.readframes(frame_count)

    if sample_width != 2:
        raise ValueError(f"Only 16-bit PCM WAV is supported, got sample width {sample_width}")

    total_samples = len(raw) // 2
    unpacked = struct.unpack(f"<{total_samples}h", raw[: total_samples * 2])

    scored: List[Tuple[float, float, float]] = []
    for start, end in ranges:
        if end <= start:
            continue
        start_frame = max(0, int(start * frame_rate))
        end_frame = min(total_samples, max(0, int(end * frame_rate)))
        if end_frame <= start_frame:
            continue
        if channels <= 1:
            window = unpacked[start_frame:end_frame]
        else:
            window = []
            for i in range(start_frame, end_frame, channels):
                frame = unpacked[i : min(i + channels, total_samples)]
                window.append(sum(frame) / len(frame))
        if not window:
            continue
        mean_sq = sum(v * v for v in window) / len(window)
        rms = math.sqrt(mean_sq) / 32768.0
        scored.append((start, end, rms))

    scored.sort(key=lambda t: t[2], reverse=True)
    return scored
