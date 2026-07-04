"""Pure, stdlib-only helpers for audio chunking and transcript merging.

Everything in this module is deliberately free of MLX/mlx-audio/numpy
dependencies so it can be exercised by fast unit tests without any ML
runtime installed. It mirrors the chunking/merge strategy used by
vibecorder's ``VibeVoiceTranscriber`` (overlapping windows + boundary
dedup) but in a simplified, framework-agnostic form.
"""

from __future__ import annotations

import re
import wave
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence, Tuple, Union

PathLike = Union[str, Path]


@dataclass(frozen=True)
class WavInfo:
    """Basic properties of a WAV file, read via the stdlib ``wave`` module."""

    path: str
    duration_seconds: float
    sample_rate: int
    channels: int
    sample_width: int
    frame_count: int


def probe_wav(path: PathLike) -> WavInfo:
    """Read duration/sample-rate/channels/sample-width from a WAV file.

    Uses only the stdlib ``wave`` module - no ffprobe/ffmpeg required.
    Raises ``wave.Error`` (or ``FileNotFoundError``) if the file cannot be
    read as a WAV container.
    """
    wav_path = Path(path)
    with wave.open(str(wav_path), "rb") as handle:
        frame_rate = handle.getframerate()
        frame_count = handle.getnframes()
        channels = handle.getnchannels()
        sample_width = handle.getsampwidth()
        duration = frame_count / frame_rate if frame_rate else 0.0

    return WavInfo(
        path=str(wav_path),
        duration_seconds=duration,
        sample_rate=frame_rate,
        channels=channels,
        sample_width=sample_width,
        frame_count=frame_count,
    )


def compute_chunk_windows(
    duration_seconds: float,
    chunk_length_seconds: float,
    overlap_seconds: float = 0.0,
) -> List[Tuple[float, float]]:
    """Split ``duration_seconds`` into overlapping ``(start, end)`` windows.

    - Each window is at most ``chunk_length_seconds`` long.
    - Consecutive windows overlap by ``overlap_seconds`` (except there is
      naturally no leading overlap on the first window).
    - The final window's ``end`` always equals ``duration_seconds`` exactly.
    - If ``duration_seconds`` fits within a single chunk, returns one window
      covering the whole file.
    """
    if duration_seconds <= 0:
        return []
    if chunk_length_seconds <= 0:
        raise ValueError("chunk_length_seconds must be positive")
    if overlap_seconds < 0:
        raise ValueError("overlap_seconds must be non-negative")
    if overlap_seconds >= chunk_length_seconds:
        raise ValueError("overlap_seconds must be smaller than chunk_length_seconds")

    if duration_seconds <= chunk_length_seconds:
        return [(0.0, duration_seconds)]

    step = chunk_length_seconds - overlap_seconds
    windows: List[Tuple[float, float]] = []
    start = 0.0
    while start < duration_seconds:
        end = min(duration_seconds, start + chunk_length_seconds)
        windows.append((start, end))
        if end >= duration_seconds:
            break
        start += step

    return windows


def _normalize_match_text(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _normalized_words(text: Any) -> List[str]:
    normalized = _normalize_match_text(text)
    return normalized.split() if normalized else []


def _coerce_time(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def offset_segment(segment: dict, offset_seconds: float) -> dict:
    """Return a copy of ``segment`` with start/end times shifted globally."""
    adjusted = dict(segment)
    start = _coerce_time(adjusted.get("start_time", adjusted.get("start")))
    end = _coerce_time(adjusted.get("end_time", adjusted.get("end")))

    if start is not None:
        adjusted["start_time"] = round(start + offset_seconds, 3)
    if end is not None:
        adjusted["end_time"] = round(end + offset_seconds, 3)
    if "duration" not in adjusted and start is not None and end is not None:
        adjusted["duration"] = round(end - start, 3)

    adjusted.pop("start", None)
    adjusted.pop("end", None)
    return adjusted


def segments_are_duplicates(left: dict, right: dict, text_ratio_threshold: float = 0.85) -> bool:
    """Best-effort duplicate detection for two segments near a chunk boundary.

    Considers segments duplicates when their normalized text is equal,
    one contains the other, or fuzzy word-level similarity is high, AND
    (when timing is available) their time ranges are close or overlapping.
    """
    left_text = _normalize_match_text(left.get("text"))
    right_text = _normalize_match_text(right.get("text"))
    if not left_text or not right_text:
        return False

    text_matches = (
        left_text == right_text
        or left_text in right_text
        or right_text in left_text
        or SequenceMatcher(None, left_text, right_text).ratio() >= text_ratio_threshold
    )

    if not text_matches:
        left_words = _normalized_words(left.get("text"))
        right_words = _normalized_words(right.get("text"))
        if left_words and right_words:
            shorter, longer = sorted((left_words, right_words), key=len)
            text_matches = (
                SequenceMatcher(None, longer[-len(shorter):], shorter).ratio() >= 0.72
            )

    if not text_matches:
        return False

    left_start = _coerce_time(left.get("start_time"))
    right_start = _coerce_time(right.get("start_time"))
    left_end = _coerce_time(left.get("end_time"))
    right_end = _coerce_time(right.get("end_time"))

    if None in (left_start, right_start, left_end, right_end):
        # No timing info to disambiguate further; trust the text match.
        return True

    return (
        abs(left_start - right_start) <= 3.0
        or abs(left_end - right_end) <= 3.0
        or max(left_start, right_start) <= min(left_end, right_end)
    )


def merge_chunk_segments(
    chunks: Sequence[Tuple[float, Iterable[dict]]],
    overlap_seconds: float = 15.0,
) -> List[dict]:
    """Merge per-chunk transcript segments into one ordered, deduped list.

    Args:
        chunks: sequence of ``(chunk_start_seconds, segments)`` pairs, in
            chronological order. ``segments`` are chunk-local (i.e. their
            ``start_time``/``end_time`` are relative to the start of that
            chunk, not the whole file) and will be offset by
            ``chunk_start_seconds``.
        overlap_seconds: the overlap window used when the chunks were cut;
            used to decide which segments near a chunk boundary should be
            checked for duplication against the previously merged output.

    Returns:
        A single list of segments in global time order, with boundary
        duplicates removed.
    """
    merged: List[dict] = []

    for chunk_start, chunk_segments in chunks:
        adjusted = [offset_segment(segment, chunk_start) for segment in chunk_segments]
        overlap_cutoff = chunk_start + overlap_seconds + 1.0

        # Only compare against merged segments that are plausibly within
        # the overlap region (to keep this O(n) instead of O(n^2) globally).
        candidates = [
            segment
            for segment in merged
            if (_coerce_time(segment.get("end_time")) or 0.0) >= max(0.0, chunk_start - overlap_seconds - 5.0)
        ]

        for segment in adjusted:
            segment_end = _coerce_time(segment.get("end_time"))
            in_overlap_region = segment_end is not None and segment_end <= overlap_cutoff

            if in_overlap_region and any(
                segments_are_duplicates(segment, existing) for existing in candidates
            ):
                continue

            merged.append(segment)

    # Keep output strictly ordered by start time (stable for ties).
    merged.sort(key=lambda s: (_coerce_time(s.get("start_time")) or 0.0,))
    return merged


def flatten_segments(segments: Sequence[dict]) -> Tuple[str, Optional[str]]:
    """Flatten a segment list into (plain_text, diarised_text_or_None)."""
    if not segments:
        return "", None

    plain_parts: List[str] = []
    diarised_parts: List[str] = []

    for segment in segments:
        text = (segment.get("text") or "").strip()
        if not text:
            continue
        speaker = segment.get("speaker_id")
        start = segment.get("start_time")
        end = segment.get("end_time")
        time_prefix = ""
        if start is not None or end is not None:
            time_prefix = f"[{start if start is not None else '?'} - {end if end is not None else '?'}] "

        plain_parts.append(text)
        if speaker is not None:
            diarised_parts.append(f"{time_prefix}Speaker {speaker}: {text}")
        else:
            diarised_parts.append(f"{time_prefix}{text}".strip())

    plain_text = "\n\n".join(plain_parts).strip()
    diarised_text = "\n".join(diarised_parts).strip() or None
    return plain_text, diarised_text
