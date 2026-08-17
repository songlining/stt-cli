#!/usr/bin/env python3
"""Turn-based speaker naming helper for stt meeting transcripts.

This complements `stt name-speakers`, whose built-in interactive loop names all
speakers in one blocking process. Agent conversations are turn-based, so this
helper exposes separate list/preview/enroll actions:

  1. list    - show diarised speakers and which remain unnamed
  2. preview - build/play one speaker's loudness-normalized sample
  3. enroll  - enroll that one speaker after the user provides a name

Enrollment intentionally delegates back to `stt name-speakers` using a
one-speaker filtered transcript, preserving the canonical full-speech embedding
behavior while only processing one speaker per agent turn.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Reuse path resolution from the primary skill helper instead of duplicating it.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from recordings import find_stt_bin, runtime_backend_path  # noqa: E402


TRANSCRIPT_NAME = "transcript.json"
CLIPS_DIR_NAME = ".speaker-clips"
AUDIT_FILENAME = "speaker_audit.json"
LABEL_SUGGESTIONS_FILENAME = "speaker_label_suggestions.json"

# Default speaker matching thresholds for the suggest-labels command. These
# mirror the backend ``match`` defaults so suggestions and explicit matches
# agree on what counts as a confident profile match.
DEFAULT_SUGGEST_THRESHOLD = 0.78
DEFAULT_SUGGEST_MARGIN = 0.05


# Conservative default safety thresholds for the audit command.
# A cluster with less than this much useful speech is "unknown" (too little
# to assess or enroll from).
DEFAULT_AUDIT_MIN_USEFUL_SPEECH = 5.0
# A cluster whose useful speech is spread over a time span more than this many
# times longer than the speech itself is "mixed_suspected" (different people
# may have been collapsed into one diarisation id across a wide timeline).
DEFAULT_AUDIT_MIXED_SPAN_RATIO = 3.0


def die(message: str) -> None:
    raise SystemExit(message)


def load_transcript(session: Path) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    transcript_path = session / TRANSCRIPT_NAME
    if not transcript_path.exists():
        die(f"Transcript not found: {transcript_path}")
    with transcript_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not isinstance(data.get("segments"), list):
        die(f"Unexpected transcript JSON shape: {transcript_path}")
    return transcript_path, data, data["segments"]


def is_bracket_only(text: str | None) -> bool:
    value = (text or "").strip()
    return bool(value) and re.fullmatch(r"\[[^\]]+\]", value) is not None


def is_useful_text(text: str | None) -> bool:
    value = (text or "").strip()
    return bool(value) and not is_bracket_only(value)


def speaker_id_of(segment: dict[str, Any]) -> str | None:
    value = segment.get("speaker_id")
    if value is None:
        return None
    return str(value)


def seconds(segment: dict[str, Any]) -> float:
    duration = segment.get("duration")
    if isinstance(duration, (int, float)):
        return max(0.0, float(duration))
    start = segment.get("start_time")
    end = segment.get("end_time")
    if isinstance(start, (int, float)) and isinstance(end, (int, float)):
        return max(0.0, float(end) - float(start))
    return 0.0


def summarize_speakers(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for segment in segments:
        sid = speaker_id_of(segment)
        if sid is None:
            continue
        item = grouped.setdefault(
            sid,
            {
                "speaker_id": sid,
                "source": segment.get("source") or "unknown",
                "speaker_name": None,
                "segments": 0,
                "useful_segments": 0,
                "speech_seconds": 0.0,
                "useful_speech_seconds": 0.0,
                "examples": [],
            },
        )
        item["segments"] += 1
        item["speech_seconds"] += seconds(segment)
        if item["source"] == "unknown" and segment.get("source"):
            item["source"] = segment.get("source")
        if not item["speaker_name"] and segment.get("speaker_name"):
            item["speaker_name"] = segment.get("speaker_name")
        if is_useful_text(segment.get("text")):
            item["useful_segments"] += 1
            item["useful_speech_seconds"] += seconds(segment)
            if len(item["examples"]) < 3:
                item["examples"].append(segment.get("text"))

    return sorted(
        grouped.values(),
        key=lambda item: (-item["useful_speech_seconds"], item["speaker_id"]),
    )


def source_wav(session: Path, source: str) -> Path:
    if source == "mic":
        path = session / "mic.wav"
    elif source == "system":
        path = session / "system.wav"
    else:
        die(f"Speaker source is {source!r}; expected 'mic' or 'system'.")
    if not path.exists():
        die(f"Required source WAV not found for source={source}: {path}")
    return path


def runtime_python() -> tuple[Path, Path]:
    backend = runtime_backend_path()
    if backend is None:
        die("stt runtime backend not found; expected <stt-cli-repo>/runtime/.venv")
    python = backend / ".venv" / "bin" / "python"
    if not python.exists():
        die(f"runtime python not found: {python}")
    return backend, python


def speaker_profiles_directory() -> Path:
    """Resolve the speaker profiles directory, mirroring ``stt``'s resolution.

    Resolution order (matches ``Paths.speakerProfilesDirectory`` in Swift):
      1. ``STT_HOME`` env var override -> ``$STT_HOME/speakers/profiles``
      2. default app support -> ``~/Library/Application Support/stt/speakers/profiles``
    """
    stt_home = os.environ.get("STT_HOME")
    if stt_home:
        base = Path(stt_home).expanduser()
    else:
        base = Path.home() / "Library" / "Application Support" / "stt"
    return base / "speakers" / "profiles"


def profile_name_exists(name: str) -> bool:
    """Check whether a display name already exists among enrolled profiles.

    Reads the ``displayName`` field from each profile JSON (excluding
    ``index.json``) under ``<speakers-root>/profiles/``. Returns ``False`` when
    the directory does not exist (no profiles enrolled yet).

    ``speaker_profiles_directory()`` returns the speakers *root* (e.g.
    ``~/Library/Application Support/stt/speakers``); individual profile JSONs
    live under ``<root>/profiles/<uuid>.json`` (mirroring
    ``SpeakerProfileStore`` in Swift).
    """
    profiles_dir = speaker_profiles_directory() / "profiles"
    if not profiles_dir.exists():
        return False
    target = str(name)
    for profile_path in profiles_dir.glob("*.json"):
        if profile_path.name == "index.json":
            continue
        try:
            with profile_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if str(data.get("displayName", "")) == target:
            return True
    return False


def speaker_summary_or_die(session: Path, speaker_id: str) -> tuple[dict[str, Any], Path, dict[str, Any], list[dict[str, Any]]]:
    transcript_path, data, segments = load_transcript(session)
    speakers = summarize_speakers(segments)
    for speaker in speakers:
        if speaker["speaker_id"] == str(speaker_id):
            return speaker, transcript_path, data, segments
    die(f"Speaker {speaker_id} not found in {transcript_path}")


# ---------------------------------------------------------------------------
# Purity preview: chronological window selection
# ---------------------------------------------------------------------------
#
# These helpers select chronological "windows" of one speaker's useful speech
# so an agent can play early/middle/late clips and detect mixed-speaker
# clusters (e.g. Speaker 4, where early segments were one person and late
# segments another) *before* enrolling a profile from the whole cluster.
#
# They are pure functions over transcript segments so they can be unit-tested
# directly (see python/tests/test_name_one_speaker.py).

# Wall-clock epsilon (seconds) below which two window boundaries are treated as
# the same point. Matches the backend MINIMUM_SEGMENT_SECONDS (0.5s) so a
# sub-half-second drift never invents a spurious distinct window.
WINDOW_EPSILON = 0.5


def _parse_one_range(value: str) -> tuple[float, float]:
    """Parse a single ``start-end`` range string into ``(start, end)`` seconds.

    Accepts the same formats as the backend range parser: plain seconds
    (``123.4-180.0``), ``MM:SS-MM:SS`` (``02:03-03:00``), and
    ``HH:MM:SS-HH:MM:SS``. Implemented locally (rather than importing
    ``stt_vibevoice.speaker_id``) because this helper runs under the system
    python3, which does not have the runtime venv on its path.
    """
    text = str(value).strip()
    if "-" not in text:
        die(f"Invalid time range {value!r}: expected 'start-end' (e.g. '123.4-180.0', '02:03-03:00').")
    dash_index = text.index("-")
    start_str, end_str = text[:dash_index], text[dash_index + 1 :]
    if not start_str or not end_str:
        die(f"Invalid time range {value!r}: both start and end are required.")
    return _parse_one_timestamp(start_str), _parse_one_timestamp(end_str)


def _parse_one_timestamp(value: str) -> float:
    """Parse seconds / MM:SS / HH:MM:SS into float seconds."""
    text = value.strip()
    parts = text.split(":")
    try:
        if len(parts) == 1:
            seconds = float(parts[0])
        elif len(parts) == 2:
            seconds = int(parts[0]) * 60.0 + float(parts[1])
        elif len(parts) == 3:
            seconds = int(parts[0]) * 3600.0 + int(parts[1]) * 60.0 + float(parts[2])
        else:
            die(f"Invalid timestamp {value!r}: expected seconds, MM:SS, or HH:MM:SS.")
    except ValueError:
        die(f"Invalid timestamp {value!r}: expected numeric seconds, MM:SS, or HH:MM:SS.")
    if seconds < 0:
        die(f"Invalid timestamp {value!r}: must not be negative.")
    return seconds


def parse_ranges(values: list[str] | None) -> list[tuple[float, float]] | None:
    """Parse repeated ``--range`` values into ``(start, end)`` tuples.

    Returns ``None`` when no ranges were given (so callers can distinguish
    "no restriction" from "empty list").
    """
    if not values:
        return None
    return [_parse_one_range(v) for v in values]


def collect_useful_ranges(
    segments: list[dict[str, Any]],
    speaker_id: str,
    ranges: list[tuple[float, float]] | None = None,
) -> list[tuple[float, float]]:
    """Collect sorted ``(start, end)`` useful-speech ranges for one speaker.

    "Useful" means the segment text is neither empty nor a bracket-only
    non-speech tag (``[Silence]``, ``[Environmental Sounds]``, ...). When
    ``ranges`` is given, each segment is *intersected* with the requested
    ranges (clipped to segment boundaries), mirroring the backend's
    ``select_speaker_segments`` intersection semantics. Pieces shorter than
    ``WINDOW_EPSILON`` are dropped.
    """
    target = str(speaker_id)
    out: list[tuple[float, float]] = []
    for segment in segments:
        if speaker_id_of(segment) != target:
            continue
        if not is_useful_text(segment.get("text")):
            continue
        start = segment.get("start_time", segment.get("start"))
        end = segment.get("end_time", segment.get("end"))
        if start is None or end is None:
            continue
        try:
            seg_start, seg_end = float(start), float(end)
        except (TypeError, ValueError):
            continue
        if ranges is None:
            if seg_end - seg_start >= WINDOW_EPSILON:
                out.append((seg_start, seg_end))
            continue
        for req_start, req_end in ranges:
            clip_start = max(seg_start, float(req_start))
            clip_end = min(seg_end, float(req_end))
            if clip_end - clip_start >= WINDOW_EPSILON:
                out.append((clip_start, clip_end))
    out.sort()
    return out


def _time_at_cumulative_speech(ranges: list[tuple[float, float]], target: float) -> float:
    """Wall-clock time at which cumulative *speech* duration reaches ``target``.

    Walks the sorted ranges accumulating actual speech duration (ignoring
    inter-segment gaps). When ``target`` falls inside a segment, returns the
    intra-segment time; when ``target`` is >= total speech, returns the final
    end time. This is the key primitive for placing early/middle/late windows
    by speech content rather than by raw wall-clock position (which would be
    skewed by long silences between utterances).
    """
    if not ranges:
        return 0.0
    acc = 0.0
    for start, end in ranges:
        dur = end - start
        if acc + dur >= target:
            return start + (target - acc)
        acc += dur
    return ranges[-1][1]


def select_purity_windows(
    segments: list[dict[str, Any]],
    speaker_id: str,
    *,
    preview_seconds: float = 12.0,
    ranges: list[tuple[float, float]] | None = None,
) -> dict[str, Any]:
    """Select chronological purity-preview windows for one speaker cluster.

    Returns up to four windows drawn from the speaker's useful speech:

      - ``early``: the first ``preview_seconds`` of speech (chronologically).
      - ``middle``: speech around the cumulative-speech midpoint.
      - ``late``: the last ``preview_seconds`` of speech.
      - ``best_energy``: NOT produced here (built separately via the backend's
        best-segments ranking); this function only returns chronological
        windows so it stays ML/audio-free and unit-testable.

    Short clusters (all speech fits in ``preview_seconds``) collapse to a
    single ``early`` window. Long clusters yield distinct early/middle/late
    windows. Windows that would duplicate an already-selected window (same
    bounds within ``WINDOW_EPSILON``) are dropped and a warning is recorded.

    Each window dict carries ``label``, ``start``, ``end`` (the bounding time
    range to hand to the backend's ``concatenate --range``), and the list of
    underlying ``ranges`` (the actual speaker segments within the window).

    The returned metadata also includes ``useful_segment_count``,
    ``useful_speech_seconds``, ``span_seconds`` (first-start to last-end),
    and ``warnings``.
    """
    useful = collect_useful_ranges(segments, speaker_id, ranges=ranges)
    total = sum(end - start for start, end in useful)
    base: dict[str, Any] = {
        "speaker_id": str(speaker_id),
        "useful_segment_count": len(useful),
        "useful_speech_seconds": round(total, 3),
        "span_seconds": round((useful[-1][1] - useful[0][0]) if useful else 0.0, 3),
        "windows": [],
        "warnings": [],
    }
    if not useful:
        base["warnings"].append("no_useful_speech")
        return base

    first_start = useful[0][0]
    last_end = useful[-1][1]
    budget = max(0.1, float(preview_seconds))

    # Window bounds placed by cumulative-speech position so wall-clock gaps do
    # not distort "first/middle/last N seconds of talking".
    early_end = _time_at_cumulative_speech(useful, min(budget, total))
    late_start = _time_at_cumulative_speech(useful, max(0.0, total - budget))
    mid_lo = _time_at_cumulative_speech(useful, max(0.0, total / 2.0 - budget / 2.0))
    mid_hi = _time_at_cumulative_speech(useful, min(total, total / 2.0 + budget / 2.0))

    candidates: list[tuple[str, float, float]] = [
        ("early", first_start, early_end),
        ("middle", mid_lo, mid_hi),
        ("late", late_start, last_end),
    ]

    def _close(a: float, b: float) -> bool:
        return abs(a - b) < WINDOW_EPSILON

    chosen: list[dict[str, Any]] = []
    for label, w_start, w_end in candidates:
        # Drop zero/empty windows (can happen for middle when total is tiny).
        if w_end - w_start < WINDOW_EPSILON:
            continue
        if any(_close(w_start, c["start"]) and _close(w_end, c["end"]) for c in chosen):
            base["warnings"].append(f"{label}_window_collapsed_duplicate")
            continue
        window_ranges = [
            (max(s, w_start), min(e, w_end))
            for s, e in useful
            if e > w_start and s < w_end and (min(e, w_end) - max(s, w_start)) >= WINDOW_EPSILON
        ]
        window_seconds = sum(e - s for s, e in window_ranges)
        chosen.append(
            {
                "label": label,
                "start": round(w_start, 3),
                "end": round(w_end, 3),
                "ranges": window_ranges,
                "segment_count": len(window_ranges),
                "speech_seconds": round(window_seconds, 3),
            }
        )

    base["windows"] = chosen
    return base


# ---------------------------------------------------------------------------
# Speaker audit: summarize clusters and flag possible mixed clusters
# ---------------------------------------------------------------------------
#
# The audit command is a read-only safety pre-screen. For each diarised
# speaker it records a compact summary (source, current name, useful-speech
# seconds, first/middle/last timestamps, example text), then classifies the
# cluster as one of:
#
#   - "unknown":        too little useful speech to assess or enroll from.
#   - "pure_likely":    compact cluster; whole-cluster enrollment is safe.
#   - "mixed_suspected": useful speech is spread across a wall-clock span that
#                       is much wider than the speech itself, which is the
#                       classic signature of two different people collapsed
#                       into one diarisation id (e.g. Speaker 4).
#
# Classification reuses the purity-window selection from task 04: a cluster
# yields distinct early AND late windows only when it is long enough, and the
# span/speech ratio measures how "smeared" across time it is. These are
# conservative, rule-based heuristics; embedding-based confirmation is task 06.
#
# The result is written to ``<session>/speaker_audit.json`` and consumed by:
#   - ``list`` (surfaces safety status inline), and
#   - the whole-cluster enrollment guard (task 07), which refuses to enroll a
#     cluster marked ``safe_to_enroll_whole_cluster == false``.


def speaker_timestamps(
    segments: list[dict[str, Any]],
    speaker_id: str,
    ranges: list[tuple[float, float]] | None = None,
) -> dict[str, float | None]:
    """First / middle / last wall-clock timestamps of a speaker's useful speech.

    ``first`` and ``last`` are the earliest start and latest end of any useful
    segment. ``middle`` is the wall-clock time at which cumulative useful
    speech reaches its midpoint (so it lands inside actual speech, not inside
    an inter-segment gap). Returns all-``None`` when the speaker has no useful
    speech.
    """
    useful = collect_useful_ranges(segments, speaker_id, ranges=ranges)
    if not useful:
        return {"first": None, "middle": None, "last": None}
    total = sum(end - start for start, end in useful)
    return {
        "first": round(useful[0][0], 3),
        "middle": round(_time_at_cumulative_speech(useful, total / 2.0), 3),
        "last": round(useful[-1][1], 3),
    }


def classify_speaker_safety(
    selection: dict[str, Any],
    *,
    min_useful_speech: float = DEFAULT_AUDIT_MIN_USEFUL_SPEECH,
    mixed_span_ratio: float = DEFAULT_AUDIT_MIXED_SPAN_RATIO,
) -> dict[str, Any]:
    """Classify one speaker cluster's enrollment safety (conservative rules).

    Inputs are the ``select_purity_windows`` metadata (``useful_speech_seconds``,
    ``span_seconds``, ``windows``). Returns a dict with ``status`` (one of
    ``unknown`` / ``pure_likely`` / ``mixed_suspected``),
    ``safe_to_enroll_whole_cluster`` (bool), and human-readable ``reasons``.
    """
    useful = float(selection.get("useful_speech_seconds") or 0.0)
    span = float(selection.get("span_seconds") or 0.0)
    windows = selection.get("windows") or []

    if useful <= 0.0:
        return {
            "status": "unknown",
            "safe_to_enroll_whole_cluster": False,
            "reasons": ["no_useful_speech"],
        }

    if useful < min_useful_speech:
        return {
            "status": "unknown",
            "safe_to_enroll_whole_cluster": False,
            "reasons": [
                f"useful_speech_seconds {useful:.1f} below minimum {min_useful_speech:.1f}"
            ],
        }

    labels = {w.get("label") for w in windows}
    has_early = "early" in labels
    has_late = "late" in labels
    ratio = span / useful if useful > 0 else 0.0

    # Mixed-suspected only when the cluster is long enough to have BOTH early
    # and late windows AND its useful speech is smeared across a wide span.
    if has_early and has_late and ratio > mixed_span_ratio:
        return {
            "status": "mixed_suspected",
            "safe_to_enroll_whole_cluster": False,
            "reasons": [
                f"span_to_speech_ratio {ratio:.2f} exceeds {mixed_span_ratio:.2f}",
                "distinct_early_and_late_windows_span_wide_timeline",
            ],
        }

    return {
        "status": "pure_likely",
        "safe_to_enroll_whole_cluster": True,
        "reasons": [
            f"span_to_speech_ratio {ratio:.2f} within threshold {mixed_span_ratio:.2f}",
            "no_widely_separated_window_pair",
        ],
    }


def audit_safety_recommendation(status: str) -> str:
    """Human/agent-readable next-step text for a given audit status."""
    if status == "mixed_suspected":
        return (
            "Do not enroll the whole cluster. Run `purity-preview` to confirm, "
            "then use `enroll-ranges` with confirmed ranges."
        )
    if status == "unknown":
        return (
            "Not enough useful speech to assess. Collect more speech or enroll "
            "manually after review."
        )
    return (
        "Cluster appears compact; whole-cluster enrollment is permitted. "
        "Run `purity-preview` first if you want audio confirmation."
    )


def build_speaker_audit(
    segments: list[dict[str, Any]],
    *,
    min_useful_speech: float = DEFAULT_AUDIT_MIN_USEFUL_SPEECH,
    mixed_span_ratio: float = DEFAULT_AUDIT_MIXED_SPAN_RATIO,
) -> list[dict[str, Any]]:
    """Build the per-speaker audit entries for a transcript's segments.

    Each entry includes: speaker_id, source, current speaker_name, segment /
    useful-segment counts, speech / useful-speech seconds, first/middle/last
    timestamps, span_seconds, example text, purity-window summary, the safety
    status + ``safe_to_enroll_whole_cluster`` flag + reasons, and a
    recommendation. Speakers are sorted by descending useful speech then id,
    matching ``summarize_speakers``.
    """
    speakers = summarize_speakers(segments)
    entries: list[dict[str, Any]] = []
    for speaker in speakers:
        sid = speaker["speaker_id"]
        selection = select_purity_windows(
            segments,
            sid,
            preview_seconds=12.0,
        )
        safety = classify_speaker_safety(
            selection,
            min_useful_speech=min_useful_speech,
            mixed_span_ratio=mixed_span_ratio,
        )
        timestamps = speaker_timestamps(segments, sid)
        # Compact window summary for the artifact (drop the verbose ranges list).
        window_summary = [
            {
                "label": w.get("label"),
                "start": w.get("start"),
                "end": w.get("end"),
                "segment_count": w.get("segment_count"),
                "speech_seconds": w.get("speech_seconds"),
            }
            for w in (selection.get("windows") or [])
        ]
        entries.append(
            {
                "speaker_id": sid,
                "source": speaker["source"],
                "speaker_name": speaker.get("speaker_name"),
                "segments": speaker["segments"],
                "useful_segments": speaker["useful_segments"],
                "speech_seconds": round(speaker["speech_seconds"], 3),
                "useful_speech_seconds": round(speaker["useful_speech_seconds"], 3),
                "first_timestamp": timestamps["first"],
                "middle_timestamp": timestamps["middle"],
                "last_timestamp": timestamps["last"],
                "span_seconds": selection["span_seconds"],
                "examples": speaker["examples"],
                "purity_windows": window_summary,
                "status": safety["status"],
                "safe_to_enroll_whole_cluster": safety["safe_to_enroll_whole_cluster"],
                "reasons": safety["reasons"],
                "recommendation": audit_safety_recommendation(safety["status"]),
            }
        )
    return entries


def do_audit(args: argparse.Namespace) -> None:
    """Summarize every speaker cluster and write a safety audit artifact.

    Read-only with respect to transcripts and profiles: the only file written
    is ``<session>/speaker_audit.json`` (plus an optional ``--json`` copy). The
    artifact is deterministic for a given transcript + thresholds, so it is
    reused by ``list`` and the whole-cluster enrollment guard (task 07).
    """
    session = Path(args.session).expanduser().resolve()
    transcript_path, _data, segments = load_transcript(session)
    audit_path = session / AUDIT_FILENAME

    # Reuse a cached artifact unless --force asks for a fresh computation.
    cached = load_audit(session)
    if cached is not None and not args.force:
        result = cached
    else:
        speakers_audit = build_speaker_audit(
            segments,
            min_useful_speech=float(args.min_useful_speech),
            mixed_span_ratio=float(args.mixed_span_ratio),
        )
        result = {
            "action": "audit",
            "session": str(session),
            "transcript": str(transcript_path),
            "thresholds": {
                "min_useful_speech": float(args.min_useful_speech),
                "mixed_span_ratio": float(args.mixed_span_ratio),
            },
            "speakers": speakers_audit,
            "safe_to_enroll_whole_cluster": all(
                entry["safe_to_enroll_whole_cluster"] for entry in speakers_audit
            )
            if speakers_audit
            else False,
        }
        with audit_path.open("w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
            f.write("\n")

    if args.json:
        out = Path(args.json).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
            f.write("\n")

    print(json.dumps(result, ensure_ascii=False, indent=2))


def build_clip(session: Path, speaker_id: str, max_seconds: float, normalize: bool = True) -> dict[str, Any]:
    speaker, transcript_path, _data, _segments = speaker_summary_or_die(session, speaker_id)
    src_wav = source_wav(session, speaker["source"])
    _backend, python = runtime_python()

    clips_dir = session / CLIPS_DIR_NAME
    clips_dir.mkdir(exist_ok=True)
    seconds_token = "all" if max_seconds <= 0 else f"{int(round(max_seconds))}s"
    normalize_token = "norm" if normalize else "raw"
    out_wav = clips_dir / f"speaker-{speaker_id}-{seconds_token}-{normalize_token}.wav"
    out_json = clips_dir / f"speaker-{speaker_id}-{seconds_token}-{normalize_token}.json"

    cmd = [
        str(python),
        "-m",
        "stt_vibevoice.speaker_id",
        "concatenate",
        "--audio",
        str(src_wav),
        "--segments",
        str(transcript_path),
        "--speaker-id",
        str(speaker_id),
        "--out",
        str(out_wav),
        "--best-segments",
        "--json",
        str(out_json),
    ]
    if max_seconds > 0:
        cmd.extend(["--max-seconds", str(max_seconds)])
    if normalize:
        cmd.append("--normalize")

    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        die(
            "Failed to build speaker clip.\n"
            f"Command: {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )
    with out_json.open("r", encoding="utf-8") as f:
        meta = json.load(f)
    meta.update(
        {
            "clip": str(out_wav),
            "meta": str(out_json),
            "speaker_id": str(speaker_id),
            "source": speaker["source"],
            "speaker_name": speaker.get("speaker_name"),
            "session": str(session),
        }
    )
    return meta


def play_clip(path: Path, seconds: float) -> dict[str, Any]:
    if not Path("/usr/bin/afplay").exists():
        return {"played": False, "warning": "afplay not found", "clip": str(path)}
    proc = subprocess.run(
        ["/usr/bin/afplay", "-t", str(max(0, int(round(seconds)))), str(path)],
        text=True,
        capture_output=True,
    )
    result = {"played": proc.returncode == 0, "returncode": proc.returncode, "clip": str(path)}
    if proc.returncode != 0:
        result["warning"] = (proc.stderr or proc.stdout or "afplay failed").strip()
    return result


def filtered_transcript_path(session: Path, speaker_id: str, data: dict[str, Any], segments: list[dict[str, Any]]) -> Path:
    clips_dir = session / CLIPS_DIR_NAME
    clips_dir.mkdir(exist_ok=True)
    filtered = dict(data)
    filtered["segments"] = [s for s in segments if speaker_id_of(s) == str(speaker_id)]
    if not filtered["segments"]:
        die(f"No segments found for speaker {speaker_id}")
    # Keep these fields present, but make clear they are filtered helper inputs.
    filtered["text"] = "\n".join(str(s.get("text") or "") for s in filtered["segments"])
    filtered["diarised_text"] = filtered["text"]
    out = clips_dir / f"speaker-{speaker_id}-only-transcript.json"
    with out.open("w", encoding="utf-8") as f:
        json.dump(filtered, f, ensure_ascii=False, indent=2)
    return out


def load_audit(session: Path) -> dict[str, Any] | None:
    """Return the cached audit artifact for a session, or None if absent."""
    audit_path = session / AUDIT_FILENAME
    if not audit_path.exists():
        return None
    try:
        with audit_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def do_list(args: argparse.Namespace) -> None:
    session = Path(args.session).expanduser().resolve()
    transcript_path, _data, segments = load_transcript(session)
    speakers = summarize_speakers(segments)
    audit = load_audit(session)
    audit_by_id: dict[str, dict[str, Any]] = {}
    if audit and isinstance(audit.get("speakers"), list):
        for entry in audit["speakers"]:
            sid = str(entry.get("speaker_id"))
            audit_by_id[sid] = {
                "status": entry.get("status"),
                "safe_to_enroll_whole_cluster": entry.get("safe_to_enroll_whole_cluster"),
                "recommendation": entry.get("recommendation"),
            }
    for speaker in speakers:
        sid = speaker["speaker_id"]
        if sid in audit_by_id:
            speaker["audit"] = audit_by_id[sid]
    if not args.all:
        speakers = [s for s in speakers if not s.get("speaker_name")]
    result = {
        "session": str(session),
        "transcript": str(transcript_path),
        "audit_available": audit is not None,
        "audit_path": str(session / AUDIT_FILENAME) if audit is not None else None,
        "speakers": speakers,
        "next_speaker_id": speakers[0]["speaker_id"] if speakers else None,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def do_preview(args: argparse.Namespace) -> None:
    session = Path(args.session).expanduser().resolve()
    meta = build_clip(session, str(args.speaker_id), float(args.preview_seconds), normalize=not args.no_normalize)
    play_result = {"played": False, "skipped": True}
    if not args.no_play:
        play_result = play_clip(Path(meta["clip"]), float(args.preview_seconds))
    meta["playback"] = play_result
    print(json.dumps(meta, ensure_ascii=False, indent=2))


def build_range_clip(
    session: Path,
    speaker: dict[str, Any],
    transcript_path: Path,
    src_wav: Path,
    python: Path,
    speaker_id: str,
    window: dict[str, Any],
    *,
    preview_seconds: float,
    normalize: bool,
) -> dict[str, Any]:
    """Build one chronological purity-preview clip via backend ``concatenate``.

    Hands the window's bounding ``start-end`` range to the backend as a single
    ``--range``; the backend then intersects it with the speaker's segments
    (excluding bracket-only non-speech and other speakers) and concatenates.
    Chronological order is preserved (``--no-best-segments``) so the clip is an
    honest chronological sample, not an energy-reordered one.
    """
    clips_dir = session / CLIPS_DIR_NAME
    clips_dir.mkdir(exist_ok=True)
    label = window["label"]
    range_token = f"{window['start']:.3f}-{window['end']:.3f}"
    normalize_token = "norm" if normalize else "raw"
    out_wav = clips_dir / f"speaker-{speaker_id}-purity-{label}-{range_token}-{normalize_token}.wav"
    out_json = clips_dir / f"speaker-{speaker_id}-purity-{label}-{range_token}-{normalize_token}.json"

    cmd = [
        str(python),
        "-m",
        "stt_vibevoice.speaker_id",
        "concatenate",
        "--audio",
        str(src_wav),
        "--segments",
        str(transcript_path),
        "--speaker-id",
        str(speaker_id),
        "--out",
        str(out_wav),
        "--no-best-segments",
        "--range",
        f"{window['start']}-{window['end']}",
        "--json",
        str(out_json),
    ]
    if normalize:
        cmd.append("--normalize")

    proc = subprocess.run(cmd, text=True, capture_output=True)
    clip_entry: dict[str, Any] = {
        "label": label,
        "window": {k: window[k] for k in ("start", "end", "segment_count", "speech_seconds")},
        "clip": str(out_wav),
        "meta": str(out_json),
        "command_ok": proc.returncode == 0,
    }
    if proc.returncode != 0:
        # Clip building is best-effort: surface the failure + paths so the
        # agent/user can still act, but never abort the whole preview.
        clip_entry["warning"] = (
            (proc.stderr or proc.stdout or "concatenate failed").strip()
        )
        clip_entry["stdout"] = proc.stdout
        clip_entry["stderr"] = proc.stderr
        return clip_entry
    with out_json.open("r", encoding="utf-8") as f:
        meta = json.load(f)
    clip_entry["duration_seconds"] = meta.get("durationSeconds")
    clip_entry["segment_count"] = meta.get("segmentCount")
    clip_entry["normalized_gain"] = meta.get("normalizedGain")
    return clip_entry


def do_purity_preview(args: argparse.Namespace) -> None:
    """Preview early/middle/late + best-energy clips of one speaker cluster.

    The point is to detect mixed-speaker clusters *before* enrollment: if the
    early clip and the late clip of the same diarisation cluster sound like
    different people, enrolling the whole cluster would contaminate a profile.
    The emitted JSON is machine-readable so an agent can ask the user "do
    these all sound like the same person?" and then decide between whole-cluster
    enrollment (``enroll``) and range-limited enrollment (``enroll-ranges``).

    This command never modifies speaker profiles or transcripts -- it only
    writes preview clips + metadata under ``<session>/.speaker-clips/``.
    """
    session = Path(args.session).expanduser().resolve()
    speaker, transcript_path, _data, segments = speaker_summary_or_die(session, str(args.speaker_id))
    src_wav = source_wav(session, speaker["source"])
    _backend, python = runtime_python()
    preview_seconds = float(args.preview_seconds)
    normalize = not args.no_normalize
    requested_ranges = parse_ranges(getattr(args, "range", None))

    selection = select_purity_windows(
        segments,
        str(args.speaker_id),
        preview_seconds=preview_seconds,
        ranges=requested_ranges,
    )

    clips_dir = session / CLIPS_DIR_NAME
    clips_dir.mkdir(exist_ok=True)
    warnings: list[str] = list(selection["warnings"])
    previews: list[dict[str, Any]] = []

    # Chronological windows (early/middle/late), built via backend concatenate.
    for window in selection["windows"]:
        clip_entry = build_range_clip(
            session,
            speaker,
            transcript_path,
            src_wav,
            python,
            str(args.speaker_id),
            window,
            preview_seconds=preview_seconds,
            normalize=normalize,
        )
        if not clip_entry.get("command_ok"):
            warnings.append(f"{window['label']}_clip_build_failed")
        play_result: dict[str, Any] = {"played": False, "skipped": True}
        if not args.no_play and clip_entry.get("command_ok"):
            play_result = play_clip(Path(clip_entry["clip"]), preview_seconds)
            if not play_result.get("played"):
                warnings.append(f"{window['label']}_playback_failed")
        clip_entry["playback"] = play_result
        previews.append(clip_entry)

    # Best-energy clip for comparison: the existing loudness-ranked sample that
    # ``preview`` produces. Kept distinct from the chronological windows so an
    # agent can compare "best snippet" vs "chronological samples".
    best_entry: dict[str, Any] = {"label": "best_energy"}
    try:
        best_meta = build_clip(session, str(args.speaker_id), preview_seconds, normalize=normalize)
        best_entry.update(
            {
                "clip": best_meta.get("clip"),
                "meta": best_meta.get("meta"),
                "duration_seconds": best_meta.get("durationSeconds"),
                "segment_count": best_meta.get("segmentCount"),
                "normalized_gain": best_meta.get("normalizedGain"),
                "command_ok": True,
            }
        )
    except SystemExit:
        # build_clip dies on backend failure; treat as non-fatal here.
        best_entry["command_ok"] = False
        best_entry["warning"] = "best_energy_clip_build_failed"
        warnings.append("best_energy_clip_build_failed")
    play_result = {"played": False, "skipped": True}
    if not args.no_play and best_entry.get("command_ok"):
        play_result = play_clip(Path(best_entry["clip"]), preview_seconds)
        if not play_result.get("played"):
            warnings.append("best_energy_playback_failed")
    best_entry["playback"] = play_result
    previews.append(best_entry)

    result = {
        "action": "purity-preview",
        "session": str(session),
        "speaker_id": str(args.speaker_id),
        "source": speaker["source"],
        "speaker_name": speaker.get("speaker_name"),
        "preview_seconds": preview_seconds,
        "normalized": normalize,
        "played": not args.no_play,
        "requested_ranges": requested_ranges,
        "useful_segment_count": selection["useful_segment_count"],
        "useful_speech_seconds": selection["useful_speech_seconds"],
        "span_seconds": selection["span_seconds"],
        "previews": previews,
        "warnings": warnings,
        "note": (
            "Play each clip and confirm whether they all sound like the same person. "
            "If early/late differ, use enroll-ranges with confirmed ranges instead of "
            "whole-cluster enroll."
        ),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# Whole-cluster enrollment safety guard (task 07)
# ---------------------------------------------------------------------------
#
# The whole-cluster ``enroll`` command can contaminate a speaker profile when
# a diarisation cluster actually mixes two people (e.g. Speaker 4). The
# audit command (task 05) records a per-speaker ``safe_to_enroll_whole_cluster``
# flag in ``<session>/speaker_audit.json``. This guard consumes that artifact
# before any backend/profile work and makes one of three decisions:
#
#   - "allow"  : audit marks the speaker safe; proceed with enrollment.
#   - "warn"   : no audit (or the speaker is absent from a stale audit). Still
#                permitted, but the warning is surfaced so enrollment is never
#                *silent*. The user is pointed at ``audit`` / ``purity-preview``.
#   - "refuse" : audit marks the speaker unsafe. The whole-cluster enrollment
#                MUST NOT proceed; the output points at ``purity-preview`` and
#                ``enroll-ranges`` instead.
#
# The decision logic is a pure function over the audit artifact so it can be
# unit-tested without a filesystem; ``evaluate_enrollment_guard`` is the thin
# wrapper that loads ``speaker_audit.json`` for a session.


def enrollment_guard_decision(
    audit: dict[str, Any] | None,
    speaker_id: str,
    *,
    session: str | Path | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Evaluate the whole-cluster enrollment safety guard (pure function).

    Given an audit artifact (``speaker_audit.json`` contents, or ``None``) and a
    speaker id, return a decision dict:

      - ``decision``:                      "allow" | "warn" | "refuse"
      - ``audit_available``:               bool (an audit artifact exists)
      - ``speaker_in_audit``:              bool (the speaker has an audit entry)
      - ``status``:                        audit status string or None
      - ``safe_to_enroll_whole_cluster``:  bool or None
      - ``reasons``:                       list[str]
      - ``recommendation``:                human/agent-readable next-step text
      - ``commands``:                       exact recommended next commands

    ``session`` and ``name`` are only used to render readable command strings.
    """
    session_str = str(session) if session is not None else "<session>"
    sid = str(speaker_id)
    name_token = f' --name "{name}"' if name else ""

    base: dict[str, Any] = {
        "session": session_str,
        "speaker_id": sid,
        "audit_available": audit is not None,
        "speaker_in_audit": False,
        "status": None,
        "safe_to_enroll_whole_cluster": None,
    }

    speakers: list[dict[str, Any]] = []
    if isinstance(audit, dict) and isinstance(audit.get("speakers"), list):
        speakers = audit["speakers"]

    entry: dict[str, Any] | None = None
    for candidate in speakers:
        if str(candidate.get("speaker_id")) == sid:
            entry = candidate
            break

    purity_cmd = (
        f"name_one_speaker.py purity-preview --session {session_str} "
        f"--speaker-id {sid}"
    )
    enroll_ranges_cmd = (
        f"name_one_speaker.py enroll-ranges --session {session_str} "
        f"--speaker-id {sid}{name_token} --range <start-end> --range <start-end>"
    )

    # Case 1: no audit artifact at all -> warn loudly but allow.
    if audit is None:
        return {
            **base,
            "decision": "warn",
            "reasons": ["no_speaker_audit_found"],
            "recommendation": (
                "No speaker audit found. Whole-cluster enrollment may "
                "contaminate speaker profiles if this cluster mixes multiple "
                "people. Run `audit` (or `purity-preview`) to confirm before "
                "enrolling."
            ),
            "commands": [
                f"name_one_speaker.py audit --session {session_str}",
                purity_cmd,
            ],
        }

    # Case 2: audit exists but this speaker is not in it (stale audit) -> warn.
    if entry is None:
        return {
            **base,
            "decision": "warn",
            "reasons": ["speaker_not_in_audit"],
            "recommendation": (
                f"Speaker {sid} is not present in speaker_audit.json; the audit "
                "may be stale. Re-run `audit --force` (or `purity-preview`) to "
                "confirm before enrolling."
            ),
            "commands": [
                f"name_one_speaker.py audit --session {session_str} --force",
                purity_cmd,
            ],
        }

    # Case 3/4: the speaker has an audit entry.
    base["speaker_in_audit"] = True
    base["status"] = entry.get("status")
    safe = bool(entry.get("safe_to_enroll_whole_cluster"))
    base["safe_to_enroll_whole_cluster"] = safe
    reasons = entry.get("reasons") or [f"audit_status_{entry.get('status')}"]

    if safe:
        return {
            **base,
            "decision": "allow",
            "reasons": reasons,
            "recommendation": (
                "Audit marks this cluster as safe for whole-cluster enrollment. "
                "Run `purity-preview` first if you want audio confirmation."
            ),
            "commands": [purity_cmd],
        }

    # Unsafe: refuse and point at purity-preview + enroll-ranges.
    status = entry.get("status") or "unsafe"
    return {
        **base,
        "decision": "refuse",
        "reasons": reasons,
        "recommendation": (
            f"Refusing whole-cluster enrollment: audit marks speaker {sid} as "
            f"{status} (safe_to_enroll_whole_cluster=false). Do not enroll the "
            "whole cluster. Confirm the cluster with `purity-preview`, then "
            "enroll only confirmed speech ranges with `enroll-ranges`."
        ),
        "commands": [purity_cmd, enroll_ranges_cmd],
    }


def evaluate_enrollment_guard(
    session: Path,
    speaker_id: str,
    *,
    name: str | None = None,
) -> dict[str, Any]:
    """Load the session audit (if any) and evaluate the enrollment guard."""
    audit = load_audit(session)
    return enrollment_guard_decision(audit, speaker_id, session=session, name=name)


def do_enroll(args: argparse.Namespace) -> None:
    session = Path(args.session).expanduser().resolve()
    # Validate the speaker exists in the transcript before the safety check so
    # a bad speaker id fails fast with a clear message.
    speaker, _transcript_path, data, segments = speaker_summary_or_die(session, str(args.speaker_id))

    # Whole-cluster enrollment safety guard (task 07). Evaluated before any
    # profile/WAV/backend is touched so a refusal never mutates state.
    guard = evaluate_enrollment_guard(session, str(args.speaker_id), name=args.name)

    if guard["decision"] == "refuse":
        refusal = {
            "action": "enroll",
            "status": "refused",
            "session": str(session),
            "speaker_id": str(args.speaker_id),
            "name": args.name,
            "guard": guard,
        }
        print(json.dumps(refusal, ensure_ascii=False, indent=2))
        # Exit code 2 distinguishes a safety refusal from a generic error (1).
        raise SystemExit(2)

    # Dry-run path: show the safety decision without enrolling. Honors the same
    # guard a real enrollment would, so `--no-enroll` against an unsafe cluster
    # still reports the refusal above, and against a safe cluster reports that
    # enrollment would proceed.
    if args.no_enroll:
        dry_run = {
            "action": "enroll",
            "status": "dry_run",
            "session": str(session),
            "speaker_id": str(args.speaker_id),
            "name": args.name,
            "guard": guard,
            "would_enroll": True,
        }
        print(json.dumps(dry_run, ensure_ascii=False, indent=2))
        return

    # Real whole-cluster enrollment. Guard allowed (or merely warned); the guard
    # decision is included in the output so a warning is never silent.
    source_wav(session, speaker["source"])  # validate early
    backend, _python = runtime_python()
    stt = find_stt_bin()
    filtered = filtered_transcript_path(session, str(args.speaker_id), data, segments)

    cmd = [
        str(stt),
        "name-speakers",
        "--transcript",
        str(filtered),
        "--preview-seconds",
        str(args.preview_seconds),
        "--sample-seconds",
        str(args.sample_seconds),
        "--python-backend",
        str(backend),
    ]
    mic = session / "mic.wav"
    system = session / "system.wav"
    if mic.exists():
        cmd.extend(["--mic", str(mic)])
    if system.exists():
        cmd.extend(["--system", str(system)])
    if args.no_normalize:
        cmd.append("--no-normalize")

    proc = subprocess.run(cmd, input=f"{args.name}\n", text=True, capture_output=True)
    result = {
        "status": "enrolled" if proc.returncode == 0 and "Enrolled" in proc.stdout else "completed",
        "speaker_id": str(args.speaker_id),
        "name": args.name,
        "session": str(session),
        "filtered_transcript": str(filtered),
        "guard": guard,
        "command": cmd,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }
    if proc.returncode != 0:
        result["status"] = "failed"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(proc.returncode)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _enroll_ranges_sample_token(
    speaker_id: str,
    source: str,
    requested_ranges: list[tuple[float, float]],
    sample_seconds: float,
    normalize: bool,
) -> str:
    """Deterministic short token identifying one enroll-ranges sample request.

    Derived from the speaker id, source track, the exact requested ranges, the
    sample-seconds cap, and normalization, so identical requests produce the
    same sample/metadata filenames (and different requests produce different
    ones, never overwriting each other).
    """
    canonical = json.dumps(
        {
            "speaker_id": str(speaker_id),
            "source": str(source),
            "ranges": [[round(float(s), 3), round(float(e), 3)] for s, e in requested_ranges],
            "sample_seconds": round(float(sample_seconds), 3),
            "normalize": bool(normalize),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]


def build_enroll_sample(
    session: Path,
    speaker: dict[str, Any],
    transcript_path: Path,
    src_wav: Path,
    python: Path,
    speaker_id: str,
    requested_ranges: list[tuple[float, float]],
    selected_ranges: list[tuple[float, float]],
    selected_speech_seconds: float,
    *,
    sample_seconds: float,
    normalize: bool,
) -> dict[str, Any]:
    """Build a range-limited enrollment sample WAV + metadata JSON.

    Reuses the backend range-aware ``concatenate`` command (task 02) to build a
    single plain-PCM WAV from *only* the requested speaker ranges. Both the
    sample WAV and the metadata JSON are written under
    ``<session>/.speaker-clips/``. No speaker profile is created or modified.

    The returned dict carries the sample/metadata paths plus the backend's own
    range metadata (duration, segment count) so the caller can emit a complete
    ``sample_ready`` result. Backend failures are surfaced as a dict with
    ``status == "error"`` (and the failing command + stderr) rather than a
    hard ``die()`` so the caller can emit structured JSON before exiting.
    """
    clips_dir = session / CLIPS_DIR_NAME
    clips_dir.mkdir(exist_ok=True)
    token = _enroll_ranges_sample_token(
        speaker_id, speaker.get("source") or "unknown", requested_ranges, sample_seconds, normalize
    )
    normalize_token = "norm" if normalize else "raw"
    out_wav = clips_dir / f"speaker-{speaker_id}-enroll-ranges-{token}-{normalize_token}.wav"
    # Distinct (non-.wav) filename for the metadata JSON so a glob of either
    # extension maps 1:1 to a sample, and so the metadata is never mistaken for
    # a backend concatenate result.
    out_meta = clips_dir / f"speaker-{speaker_id}-enroll-ranges-{token}-{normalize_token}.enroll.json"

    cmd = [
        str(python),
        "-m",
        "stt_vibevoice.speaker_id",
        "concatenate",
        "--audio",
        str(src_wav),
        "--segments",
        str(transcript_path),
        "--speaker-id",
        str(speaker_id),
        "--out",
        str(out_wav),
        "--no-best-segments",
    ]
    for rng in requested_ranges:
        cmd.extend(["--range", f"{rng[0]:g}-{rng[1]:g}"])
    if sample_seconds and sample_seconds > 0:
        cmd.extend(["--max-seconds", str(sample_seconds)])
    if normalize:
        cmd.append("--normalize")

    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        return {
            "status": "error",
            "error": "sample_concatenate_failed",
            "message": (
                "Backend range-aware concatenate failed; not falling back to "
                "whole-cluster audio."
            ),
            "command": cmd,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }

    # The backend writes a sidecar JSON via --json only when requested; we
    # instead read the produced WAV directly to confirm plain-PCM compatibility
    # and capture its real duration independently of the backend's accounting.
    try:
        import wave as _wave

        with _wave.open(str(out_wav), "rb") as handle:
            wav_channels = handle.getnchannels()
            wav_sampwidth = handle.getsampwidth()
            wav_framerate = handle.getframerate()
            wav_frames = handle.getnframes()
        wav_duration = round(wav_frames / float(wav_framerate), 3) if wav_framerate else 0.0
        wav_format = {"channels": wav_channels, "sample_width": wav_sampwidth, "framerate": wav_framerate}
    except Exception as exc:  # pragma: no cover - defensive; backend wrote a valid WAV
        return {
            "status": "error",
            "error": "sample_wav_unreadable",
            "message": f"Generated sample WAV could not be opened as PCM WAV: {exc}",
            "sample_path": str(out_wav),
        }

    metadata = {
        "source_session": str(session),
        "transcript": str(transcript_path),
        "speaker_id": str(speaker_id),
        "speaker_name": speaker.get("speaker_name"),
        "source_track": speaker.get("source") or "unknown",
        "source_wav": str(src_wav),
        "requested_ranges": requested_ranges,
        "selected_ranges": selected_ranges,
        "selected_segment_count": len(selected_ranges),
        "selected_speech_seconds": selected_speech_seconds,
        "sample_seconds_cap": sample_seconds,
        "normalized": normalize,
        "sample_path": str(out_wav),
        "metadata_path": str(out_meta),
        "sample_format": wav_format,
        "sample_duration_seconds": wav_duration,
        "enrolled": False,
        "note": (
            "Range-limited enrollment sample generated from only the requested "
            "speaker ranges. Profile enrollment is performed by the caller "
            "(do_enroll_ranges) after this sample is built."
        ),
    }
    with out_meta.open("w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)
        f.write("\n")

    return {
        "status": "sample_ready",
        "sample_path": str(out_wav),
        "metadata_path": str(out_meta),
        "sample_duration_seconds": wav_duration,
        "sample_format": wav_format,
        "selected_ranges": selected_ranges,
        "selected_segment_count": len(selected_ranges),
        "selected_speech_seconds": selected_speech_seconds,
    }


def enroll_profile_from_sample(
    sample_path: Path,
    display_name: str,
    profiles_dir: Path,
    backend_dir: Path,
    stt_bin: Path,
    *,
    provider: str | None = None,
    provenance_path: Path | None = None,
) -> dict[str, Any]:
    """Enroll a speaker profile from a pre-built sample WAV via ``stt speaker enroll``.

    Delegates to the Swift ``stt speaker enroll`` command with ``--audio`` pointing
    at the range-limited sample generated under ``.speaker-clips/``. The caller is
    responsible for checking display-name collisions *before* calling this -- the
    Swift command itself also checks (and errors without ``--replace``), but the
    helper-side check avoids unnecessary ML extraction work.

    When ``provenance_path`` is given, it is forwarded as ``--provenance-json`` so
    the created profile records where its enrollment sample came from (source
    session, transcript, track, diarized speaker id, confirmed ranges,
    confirmation mode, timestamp). Omit it for plain whole-audio enrollment,
    which leaves provenance unset on the profile.

    Never falls back to whole-cluster audio: if enrollment fails, the caller
    surfaces a ``failed`` status rather than retrying with the full session audio.

    Returns a dict with ``command``, ``returncode``, ``stdout``, ``stderr``, and
    ``enrolled`` (True iff returncode == 0).
    """
    cmd = [
        str(stt_bin),
        "speaker", "enroll",
        display_name,
        "--audio", str(sample_path),
        "--profiles-dir", str(profiles_dir),
        "--python-backend", str(backend_dir),
    ]
    if provider:
        cmd.extend(["--provider", provider])
    if provenance_path is not None:
        cmd.extend(["--provenance-json", str(provenance_path)])
    proc = subprocess.run(cmd, text=True, capture_output=True)
    return {
        "command": cmd,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "enrolled": proc.returncode == 0,
    }


# ---------------------------------------------------------------------------
# Speaker profile provenance metadata (task 09)
# ---------------------------------------------------------------------------
#
# Provenance records *how* a range-limited enrollment sample was produced so a
# profile can be traced back to its source session, transcript, diarized
# speaker, and the confirmed time ranges it was enrolled from. The payload is
# shaped to match the Swift ``SpeakerProfileProvenance`` Codable exactly
# (camelCase keys), so ``stt speaker enroll --provenance-json`` decodes it
# without any translation. Every field is optional; ``samplePath`` is
# intentionally omitted because the Swift enroll command fills it with the
# canonical stored-sample path.
#
# These helpers are pure (or only touch the clips dir) so they are unit-
# testable without invoking the Swift enrollment backend or any ML work.


def build_profile_provenance(
    *,
    session: str | Path,
    transcript_path: str | Path,
    source_track: str,
    diarized_speaker_id: str,
    selected_ranges: list[tuple[float, float]],
    confirmation_mode: str = "range-limited",
    timestamp: datetime | None = None,
) -> dict[str, Any]:
    """Build a provenance payload in ``SpeakerProfileProvenance``'s Codable shape.

    camelCase keys so the Swift ``speaker enroll --provenance-json`` decoder reads
    it directly. ``samplePath`` is omitted on purpose: the Swift command sets it
    to the canonical stored-sample path (``samples/<id>/<file>.wav``) it knows at
    enrollment time. ``timestamp`` defaults to now (UTC) and is formatted as
    ``YYYY-MM-DDTHH:MM:SSZ`` to match the iso8601 strategy the profile store
    uses for ``createdAt``/``updatedAt``.

    The payload is deterministic for given inputs (apart from the auto-now
    timestamp), so tests can inject a fixed ``timestamp``.
    """
    ts = timestamp if timestamp is not None else datetime.now(timezone.utc)
    return {
        "sourceSession": str(session),
        "sourceTranscript": str(transcript_path),
        "sourceTrack": str(source_track),
        "diarizedSpeakerId": str(diarized_speaker_id),
        "selectedRanges": [[float(start), float(end)] for start, end in selected_ranges],
        "confirmationMode": str(confirmation_mode),
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def write_provenance_json(
    session: Path,
    speaker_id: str,
    provenance: dict[str, Any],
) -> Path:
    """Write the provenance payload to ``<session>/.speaker-clips/`` and return it.

    Co-located with the range-limited enrollment sample/metadata so the audit
    trail for one enrollment stays together. The filename is deterministic for a
    speaker so re-running enroll-ranges overwrites (rather than accumulates)
    stale provenance files for the same speaker.
    """
    clips_dir = session / CLIPS_DIR_NAME
    clips_dir.mkdir(exist_ok=True)
    path = clips_dir / f"speaker-{speaker_id}-enroll.provenance.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(provenance, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return path


def do_enroll_ranges(args: argparse.Namespace) -> None:
    """Prepare a range-limited enrollment sample and enroll a speaker profile.

    Three modes share one validation phase:

      - ``--no-enroll``: pure validation dry-run. Emits structured JSON
        describing the planned range-limited enrollment and writes NO files.
        Status is ``dry_run``. Never calls the enrollment backend.
      - default (``--no-enroll`` omitted): build a range-limited sample WAV +
        metadata JSON under ``<session>/.speaker-clips/`` from only the
        requested ranges (task 03b), then check for a display-name collision
        and enroll a speaker profile from that sample via ``stt speaker enroll``
        (task 03c). Status is ``enrolled`` (success), ``skipped`` (display name
        already exists -- never overwritten without an explicit replace mode),
        or ``failed`` (enrollment command returned nonzero).

    Validation (session dir, transcript, speaker existence, at least one
    ``--range``, source resolution, useful-speech-in-range) runs before any
    audio/backend work in both modes. No-usable-speech fails fast with a clear
    message; backend sample-generation failures are surfaced as structured
    JSON errors without ever falling back to whole-cluster audio. Enrollment
    failures likewise never fall back to whole-cluster audio.
    """
    session = Path(args.session).expanduser().resolve()
    if not session.is_dir():
        die(f"Session directory not found: {session}")

    requested_ranges = parse_ranges(getattr(args, "range", None))
    if requested_ranges is None:
        die(
            "enroll-ranges requires at least one --range "
            "(e.g. --range 12.0-45.0). Repeatable for multiple ranges."
        )

    speaker, transcript_path, _data, segments = speaker_summary_or_die(
        session, str(args.speaker_id)
    )

    # Resolve the source track: explicit --source wins, else transcript data.
    source = _resolve_enroll_source(args.source, speaker)
    if source not in ("mic", "system"):
        die(
            f"Cannot resolve a valid audio source for speaker {args.speaker_id}; "
            f"got {source!r}. Pass --source mic or --source system."
        )

    # Validate the speaker has usable speech in at least one requested range.
    useful = collect_useful_ranges(
        segments, str(args.speaker_id), ranges=requested_ranges
    )
    if not useful:
        die(
            f"Speaker {args.speaker_id} has no useful speech within the requested "
            f"ranges in {transcript_path}."
        )
    selected_speech_seconds = round(sum(end - start for start, end in useful), 3)

    normalize = not args.no_normalize if hasattr(args, "no_normalize") else True

    # Dry-run path: validation only, no audio sample, no profile mutation.
    if args.no_enroll:
        result = {
            "action": "enroll-ranges",
            "status": "dry_run",
            "session": str(session),
            "transcript": str(transcript_path),
            "speaker_id": str(args.speaker_id),
            "speaker_name": speaker.get("speaker_name"),
            "name": args.name,
            "source": source,
            "requested_ranges": requested_ranges,
            "selected_ranges": useful,
            "selected_segment_count": len(useful),
            "selected_speech_seconds": selected_speech_seconds,
            "sample_seconds": float(args.sample_seconds),
            "mutated_profiles": False,
            "mutated_audio": False,
            "no_enroll": True,
            "next_step": (
                "Re-run without --no-enroll to generate the range-limited "
                "enrollment sample under .speaker-clips/ and enroll a speaker "
                "profile from that sample."
            ),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    # Sample-ready path: generate the sample WAV + metadata (no profile work
    # yet).
    src_wav = source_wav(session, source)
    _backend, python = runtime_python()
    sample = build_enroll_sample(
        session,
        speaker,
        transcript_path,
        src_wav,
        python,
        str(args.speaker_id),
        requested_ranges,
        useful,
        selected_speech_seconds,
        sample_seconds=float(args.sample_seconds),
        normalize=normalize,
    )

    if sample.get("status") == "error":
        error_result = {
            "action": "enroll-ranges",
            "status": "error",
            "session": str(session),
            "transcript": str(transcript_path),
            "speaker_id": str(args.speaker_id),
            "speaker_name": speaker.get("speaker_name"),
            "name": args.name,
            "source": source,
            "requested_ranges": requested_ranges,
            "selected_ranges": useful,
            "selected_segment_count": len(useful),
            "selected_speech_seconds": selected_speech_seconds,
            "mutated_profiles": False,
            "mutated_audio": False,
            "enrolled": False,
            "error": sample.get("error"),
            "message": sample.get("message"),
            "command": sample.get("command"),
            "returncode": sample.get("returncode"),
            "stderr": sample.get("stderr"),
        }
        # Keep stdout for diagnosis but it is large; include only when present.
        if sample.get("stdout"):
            error_result["stdout"] = sample["stdout"]
        print(json.dumps(error_result, ensure_ascii=False, indent=2))
        raise SystemExit(1)

    # Sample generated successfully. Before enrolling, check whether the
    # requested display name already exists among enrolled profiles. Fail
    # safely (skip) on collision -- never overwrite an existing profile
    # without an explicit future replace/add-sample mode.
    profiles_root = speaker_profiles_directory()
    name = str(args.name)
    collision = profile_name_exists(name)

    sample_fields = {
        "sample_path": sample["sample_path"],
        "metadata_path": sample["metadata_path"],
        "sample_duration_seconds": sample["sample_duration_seconds"],
        "sample_format": sample["sample_format"],
    }

    if collision:
        result = {
            "action": "enroll-ranges",
            "status": "skipped",
            "session": str(session),
            "transcript": str(transcript_path),
            "speaker_id": str(args.speaker_id),
            "speaker_name": speaker.get("speaker_name"),
            "name": name,
            "source": source,
            "requested_ranges": requested_ranges,
            "selected_ranges": sample["selected_ranges"],
            "selected_segment_count": sample["selected_segment_count"],
            "selected_speech_seconds": sample["selected_speech_seconds"],
            "sample_seconds": float(args.sample_seconds),
            **sample_fields,
            "mutated_profiles": False,
            "mutated_audio": True,
            "enrolled": False,
            "skip_reason": "display_name_exists",
            "message": (
                f"A speaker profile named {name!r} already exists. "
                "enroll-ranges does not overwrite existing profiles; "
                "use `stt speaker remove` first to replace."
            ),
            "next_step": (
                "Range-limited sample generated but enrollment skipped because "
                "the display name already exists. Remove the existing profile or "
                "choose a different name."
            ),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    # No collision: enroll a speaker profile from the range-limited sample.
    # The sample WAV is the ONLY audio source -- never fall back to
    # whole-cluster audio on failure.
    backend, _python2 = runtime_python()
    stt = find_stt_bin()
    provider = getattr(args, "provider", None)
    # Build + persist the enrollment provenance (task 09) so the created
    # profile can be traced to its source session/transcript/track/diarized
    # speaker/confirmed ranges. The payload is shaped to match the Swift
    # SpeakerProfileProvenance Codable; samplePath is filled by the enroll
    # command itself.
    provenance_payload = build_profile_provenance(
        session=session,
        transcript_path=transcript_path,
        source_track=source,
        diarized_speaker_id=str(args.speaker_id),
        selected_ranges=sample["selected_ranges"],
    )
    provenance_path = write_provenance_json(session, str(args.speaker_id), provenance_payload)
    enroll_result = enroll_profile_from_sample(
        Path(sample["sample_path"]),
        name,
        profiles_root,
        backend,
        stt,
        provider=provider,
        provenance_path=provenance_path,
    )

    if enroll_result["enrolled"]:
        result = {
            "action": "enroll-ranges",
            "status": "enrolled",
            "session": str(session),
            "transcript": str(transcript_path),
            "speaker_id": str(args.speaker_id),
            "speaker_name": speaker.get("speaker_name"),
            "name": name,
            "source": source,
            "requested_ranges": requested_ranges,
            "selected_ranges": sample["selected_ranges"],
            "selected_segment_count": sample["selected_segment_count"],
            "selected_speech_seconds": sample["selected_speech_seconds"],
            "sample_seconds": float(args.sample_seconds),
            **sample_fields,
            "mutated_profiles": True,
            "mutated_audio": True,
            "enrolled": True,
            "enroll_command": enroll_result["command"],
            "enroll_returncode": enroll_result["returncode"],
            "enroll_stdout": enroll_result["stdout"],
            "enroll_stderr": enroll_result["stderr"],
            "profiles_directory": str(profiles_root),
            "provenance_path": str(provenance_path),
            "provenance": provenance_payload,
            "next_step": (
                "Speaker profile enrolled from the range-limited sample. "
                "Verify with `stt speaker list`."
            ),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    # Enrollment command failed. Surface structured JSON and exit nonzero.
    # Never fall back to whole-cluster audio.
    result = {
        "action": "enroll-ranges",
        "status": "failed",
        "session": str(session),
        "transcript": str(transcript_path),
        "speaker_id": str(args.speaker_id),
        "speaker_name": speaker.get("speaker_name"),
        "name": name,
        "source": source,
        "requested_ranges": requested_ranges,
        "selected_ranges": sample["selected_ranges"],
        "selected_segment_count": sample["selected_segment_count"],
        "selected_speech_seconds": sample["selected_speech_seconds"],
        "sample_seconds": float(args.sample_seconds),
        **sample_fields,
        "mutated_profiles": False,
        "mutated_audio": True,
        "enrolled": False,
        "enroll_command": enroll_result["command"],
        "enroll_returncode": enroll_result["returncode"],
        "enroll_stdout": enroll_result["stdout"],
        "enroll_stderr": enroll_result["stderr"],
        "provenance_path": str(provenance_path),
        "message": (
            "Speaker enrollment command failed. The range-limited sample was "
            "generated but no profile was created. Not falling back to "
            "whole-cluster audio."
        ),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(1)


def _resolve_enroll_source(args_source: str | None, speaker: dict[str, Any]) -> str:
    """Resolve the audio source track for enroll-ranges.

    ``--source`` wins when provided; otherwise the source recorded on the
    speaker's transcript segments is used. Unlike :func:`source_wav`, this
    does NOT require the WAV file to exist (dry-run only) so that validation
    can run on a session whose audio has not been extracted yet.
    """
    if args_source:
        if args_source not in ("mic", "system"):
            die(f"--source is {args_source!r}; expected 'mic' or 'system'.")
        return args_source
    return str(speaker.get("source") or "unknown")


def speaker_profiles_directory() -> Path:
    """Resolve the enrolled-speaker profiles directory.

    Mirrors ``Paths.speakerProfilesDirectory`` in the Swift app: defaults to
    ``<appSupport>/stt/speakers`` (``~/Library/Application Support/stt/speakers``),
    overridable via ``STT_SPEAKER_PROFILES_DIR``. Returns the *root* dir
    (``.../speakers``); profile JSON files live under ``.../profiles/<id>.json``.
    """
    override = os.environ.get("STT_SPEAKER_PROFILES_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / "stt" / "speakers"


def load_flattened_profiles_file(root: Path) -> Path | None:
    """Load all enrolled profile JSONs under ``<root>/profiles`` into a single
    flattened JSON file and return it.

    Returns ``None`` when no profiles exist (the ``profiles`` dir is absent or
    contains only ``index.json``). The flattened file is written under the
    session's clips dir so it is co-located with the work product and cleaned
    up with the session.

    Each on-disk profile JSON (written by ``SpeakerProfileStore``) already has
    the keys ``match_candidate`` consumes (``id``, ``displayName``,
    ``embeddingProvider``, ``embeddingModel``, ``embedding``), so this is a
    straightforward gather-and-wrap into ``{"profiles": [...]}``.
    """
    profiles_dir = root / "profiles"
    if not profiles_dir.exists():
        return None
    gathered: list[dict[str, Any]] = []
    for entry in sorted(profiles_dir.iterdir()):
        if entry.suffix != ".json" or entry.name == "index.json":
            continue
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and ("id" in data or "embedding" in data):
            gathered.append(data)
    if not gathered:
        return None
    out = root / CLIPS_DIR_NAME / "_flattened_profiles.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"profiles": gathered}, ensure_ascii=False), encoding="utf-8")
    return out


def do_suggest_labels(args: argparse.Namespace) -> None:
    """Non-mutating speaker label suggestions for one session.

    Matches every diarised cluster against enrolled speaker profiles and
    writes ``<session>/speaker_label_suggestions.json`` describing duplicate-
    cluster candidates, mixed-cluster warnings, and per-cluster match
    suggestions. NEVER modifies the transcript, transcript Markdown, or
    profiles -- the decision to relabel is intentionally left to an agent.

    Delegates the extraction + matching + grouping to the runtime backend
    (``stt_vibevoice.speaker_id suggest-labels``) because this helper runs
    under system python3, which cannot import the runtime venv. The helper
    owns session/path/profile resolution and artifact placement.
    """
    session = Path(args.session).expanduser().resolve()
    transcript_path, _data, segments = load_transcript(session)
    out_path = (
        Path(args.output).expanduser().resolve()
        if getattr(args, "output", None)
        else session / LABEL_SUGGESTIONS_FILENAME
    )

    # Reuse a cached artifact unless --force asks for a fresh computation.
    if out_path.exists() and not getattr(args, "force", False):
        cached = json.loads(out_path.read_text(encoding="utf-8"))
        cached["action"] = "suggest-labels"
        cached["session"] = str(session)
        cached["cached"] = True
        print(json.dumps(cached, ensure_ascii=False, indent=2))
        return

    # Resolve per-source audio from the transcript + session files.
    sources = sorted({s.get("source") for s in segments if s.get("source")})
    audio_args: list[str] = []
    for source in sources:
        if source not in ("mic", "system"):
            continue
        wav = session / f"{source}.wav"
        if wav.exists():
            audio_args.append(f"{source}={wav}")
    # Fallback: if no source-tagged audio resolved, hand the backend any
    # available WAV under a default key so clusters still get extracted.
    if not audio_args:
        for candidate in ("mic.wav", "system.wav"):
            wav = session / candidate
            if wav.exists():
                audio_args.append(f"default={wav}")
                break
    if not audio_args:
        die(
            f"No source audio found in session {session}; expected mic.wav or "
            f"system.wav alongside the transcript."
        )

    # Load enrolled profiles (flattened into a temp JSON the backend reads).
    profiles_root = speaker_profiles_directory()
    profiles_file = load_flattened_profiles_file(profiles_root)

    _backend, python = runtime_python()
    cmd = [
        str(python),
        "-m",
        "stt_vibevoice.speaker_id",
        "suggest-labels",
        "--transcript",
        str(transcript_path),
        "--provider",
        str(args.provider),
        "--threshold",
        str(args.threshold),
        "--margin",
        str(args.margin),
        "--minimum-speech-seconds",
        str(args.minimum_speech_seconds),
        "--session",
        str(session),
    ]
    for a in audio_args:
        cmd.extend(["--audio", a])
    if profiles_file is not None:
        cmd.extend(["--profiles", str(profiles_file)])
    if getattr(args, "no_windows", False):
        cmd.append("--no-windows")
    cmd.extend(["--n-windows", str(args.n_windows)])
    cmd.extend(["--json", str(out_path)])

    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        die(
            "suggest-labels backend failed.\n"
            f"Command: {' '.join(cmd)}\n"
            f"returncode: {proc.returncode}\n"
            f"stderr:\n{proc.stderr}"
        )

    # Annotate the artifact with helper-level provenance without mutating the
    # backend payload's stable schema: wrap provenance in an ``action`` block.
    result = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    result["action"] = "suggest-labels"
    result["session"] = str(session)
    result["transcript"] = str(transcript_path)
    result["profilesConsideredDir"] = str(profiles_root)
    result["profilesLoaded"] = profiles_file is not None
    result["cached"] = False
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# Segment-level transcript relabeling (task 10)
# ---------------------------------------------------------------------------
#
# Relabeling applies a human-confirmed speaker name to a *subset* of one
# diarised cluster's segments (selected by speaker id and time ranges),
# while preserving every original ``speaker_id``, ``source``, timestamp, and
# text value. This solves the Speaker 4 case where one diarisation cluster
# actually contained two people (Dana early, Ada late): the agent relabels
# the early segments to "Dana" and the late segments to "Ada" without
# touching ``speaker_id`` -- so a mixed cluster can carry multiple human
# names after relabeling.
#
# The selection + relabel logic is split into pure functions so it can be
# unit-tested without a filesystem. The text regeneration mirrors the Swift
# ``TranscriptMerger.renderPlainText`` format exactly (``[start - end] Source
# <name|Speaker id>: text``) so the regenerated ``transcript.md`` is
# byte-for-byte consistent with what ``stt`` produces.
#
# Per-source artifacts (``transcript.<source>.json``) are relabeled in place
# for the matching speaker id + ranges when present, so every JSON artifact
# stays consistent. Per-source ``.txt`` files are raw transcriber output
# (never speaker-labeled) and are intentionally NOT regenerated.


def segment_overlaps_ranges(
    seg_start: float, seg_end: float, ranges: list[tuple[float, float]] | None
) -> bool:
    """True if ``[seg_start, seg_end]`` overlaps any requested range.

    Standard half-open interval overlap: ``seg_start < req_end and seg_end >
    req_start``. When ``ranges`` is ``None`` (no ``--range`` given), every
    segment overlaps -- i.e. whole-cluster relabeling.
    """
    if ranges is None:
        return True
    for req_start, req_end in ranges:
        if seg_start < float(req_end) and seg_end > float(req_start):
            return True
    return False


def select_relabel_segments(
    segments: list[dict[str, Any]],
    speaker_id: str,
    ranges: list[tuple[float, float]] | None = None,
) -> list[int]:
    """Return indices of segments matching ``speaker_id`` and overlapping ``ranges``.

    Selects ALL matching segments (including bracket-only non-speech like
    ``[Silence]``) because relabeling applies to a diarised speaker's whole
    presence within the confirmed time ranges, not only to useful-speech
    segments. Segments without usable timestamps are skipped (cannot range-test).
    Returns indices (not segment copies) so callers can both report which
    segments change and apply the relabel in place.
    """
    target = str(speaker_id)
    out: list[int] = []
    for i, segment in enumerate(segments):
        if speaker_id_of(segment) != target:
            continue
        start = segment.get("start_time", segment.get("start"))
        end = segment.get("end_time", segment.get("end"))
        if start is None or end is None:
            continue
        try:
            seg_start, seg_end = float(start), float(end)
        except (TypeError, ValueError):
            continue
        if segment_overlaps_ranges(seg_start, seg_end, ranges):
            out.append(i)
    return out


def apply_relabel_to_segments(
    segments: list[dict[str, Any]],
    indices: list[int],
    name: str,
) -> list[dict[str, Any]]:
    """Return a deep-ish copy of ``segments`` with ``speaker_name`` set to ``name``
    only on the segments at ``indices``.

    Preserves ``speaker_id``, ``source``, ``start_time``/``end_time``,
    ``duration``, and ``text`` on every segment. Only ``speaker_name`` changes,
    and only for the selected indices. Non-selected segments are copied
    unchanged (their existing ``speaker_name`` is preserved, including any
    prior relabel).
    """
    out: list[dict[str, Any]] = [dict(s) for s in segments]
    selected = set(indices)
    for i in selected:
        if 0 <= i < len(out):
            out[i] = dict(out[i])
            out[i]["speaker_name"] = str(name)
    return out


def _source_label(source: str | None) -> str:
    """Map a segment ``source`` to the display label used in transcript text.

    Mirrors ``TranscriptMerger.label(for:)`` in Swift: ``mic`` -> ``Mic``,
    ``system`` -> ``System``, other -> capitalized, missing -> ``Unknown``.
    """
    if source is None:
        return "Unknown"
    if source == "mic":
        return "Mic"
    if source == "system":
        return "System"
    return str(source).capitalize()


def _format_time(value: float) -> str:
    """Format a timestamp as ``%.2f`` seconds, mirroring Swift's ``format(_:)``."""
    return f"{float(value):.2f}"


def render_transcript_text(segments: list[dict[str, Any]]) -> str:
    """Render transcript segments to plain text mirroring Swift ``renderPlainText``.

    Each line: ``[<start> - <end>] <Source> <name|Speaker id|>: <text>`` joined by
    newlines with a trailing newline. ``speaker_name`` wins over ``speaker_id``;
    segments with neither produce no speaker suffix. This is the exact format
    used to (re)generate ``transcript.md`` so the file stays consistent with
    what ``stt`` writes.
    """
    lines: list[str] = []
    for segment in segments:
        source = _source_label(segment.get("source"))
        name = segment.get("speaker_name")
        sid = speaker_id_of(segment)
        if name:
            speaker = f" {name}"
        elif sid is not None:
            speaker = f" Speaker {sid}"
        else:
            speaker = ""
        start = segment.get("start_time", segment.get("start"))
        end = segment.get("end_time", segment.get("end"))
        text = segment.get("text", "") or ""
        lines.append(
            f"[{_format_time(start)} - {_format_time(end)}] {source}{speaker}: {text}"
        )
    return "\n".join(lines) + "\n"


def _per_source_artifact_paths(session: Path) -> list[tuple[str, Path, Path]]:
    """Discover per-source transcript JSON artifacts that exist in the session.

    Returns ``(source, json_path, txt_path)`` tuples for each source whose JSON
    artifact is present. The naming follows ``TranscriptMerger``'s
    ``sourceArtifactURL``: ``transcript.<source>.json`` / ``transcript.<source>.txt``.
    Only the JSON is relabeled; the per-source ``.txt`` is raw transcriber output
    (never speaker-labeled) and is intentionally left untouched.
    """
    out: list[tuple[str, Path, Path]] = []
    for source in ("mic", "system"):
        json_path = session / f"transcript.{source}.json"
        txt_path = session / f"transcript.{source}.txt"
        if json_path.exists():
            out.append((source, json_path, txt_path))
    return out


def relabel_session_artifacts(
    transcript_path: Path,
    data: dict[str, Any],
    segments: list[dict[str, Any]],
    indices: list[int],
    name: str,
    session: Path,
    *,
    speaker_id: str,
    ranges: list[tuple[float, float]] | None,
) -> dict[str, Any]:
    """Apply relabeling to the merged transcript + per-source artifacts in place.

    Mutates the loaded ``data`` dict's ``segments`` (sets ``speaker_name`` on
    selected indices), re-renders ``text`` and ``diarised_text``, writes the
    merged ``transcript.json`` back, and regenerates ``transcript.md``. Per-source
    JSON artifacts that exist are relabeled for the same speaker id + ranges
    (best-effort) so every JSON artifact stays consistent.

    Returns a dict describing what was written: merged paths and per-source
    relabel counts. Selection is the caller's responsibility (``indices`` must
    already be computed via :func:`select_relabel_segments`); the same
    ``speaker_id`` + ``ranges`` are replayed against each per-source JSON so its
    selection matches the merged selection.
    """
    # Apply to the merged transcript in place.
    for i in indices:
        if 0 <= i < len(segments):
            segments[i]["speaker_name"] = str(name)
    rendered = render_transcript_text(segments)
    data["segments"] = segments
    data["text"] = rendered.strip()
    data["diarised_text"] = rendered.strip()

    with transcript_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")

    md_path = session / "transcript.md"
    md_path.write_text(rendered, encoding="utf-8")

    per_source: list[dict[str, Any]] = []
    for source, json_path, _txt_path in _per_source_artifact_paths(session):
        try:
            with json_path.open("r", encoding="utf-8") as f:
                src_data = json.load(f)
        except (OSError, ValueError):
            continue
        src_segments = src_data.get("segments")
        if not isinstance(src_segments, list):
            continue
        # Replay the same speaker id + ranges against this source's segments
        # so the per-source selection matches the merged selection. Speaker ids
        # are consistent across merged and per-source JSONs (diarization is
        # applied per-source with an offset, and merge retains the ids).
        src_indices = select_relabel_segments(src_segments, speaker_id, ranges=ranges)
        for i in src_indices:
            if 0 <= i < len(src_segments):
                src_segments[i]["speaker_name"] = str(name)
        src_data["segments"] = src_segments
        # Per-source text is raw transcriber output; do NOT overwrite it.
        with json_path.open("w", encoding="utf-8") as f:
            json.dump(src_data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        per_source.append(
            {
                "source": source,
                "json_path": str(json_path),
                "relabel_segment_count": len(src_indices),
            }
        )

    return {
        "merged_json_path": str(transcript_path),
        "merged_md_path": str(md_path),
        "per_source": per_source,
    }


def do_relabel(args: argparse.Namespace) -> None:
    """Relabel selected transcript segments with a human-confirmed speaker name.

    Selects segments matching ``--speaker-id`` and overlapping the repeated
    ``--range`` values (or ALL of that speaker's segments when no ``--range`` is
    given), sets ``speaker_name`` to ``--name`` on exactly those segments, and
    regenerates ``transcript.json`` + ``transcript.md`` (plus per-source JSON
    artifacts when present). ``speaker_id``, ``source``, timestamps, and text are
    preserved on every segment.

    ``--dry-run`` computes and reports the planned changes without writing any
    files. The output JSON is machine-readable and includes the changed segment
    count, the selected indices, and (for real runs) the paths written.
    """
    session = Path(args.session).expanduser().resolve()
    if not session.is_dir():
        die(f"Session directory not found: {session}")
    transcript_path, data, segments = load_transcript(session)

    requested_ranges = parse_ranges(getattr(args, "range", None))
    name = str(args.name)

    # Validate the speaker exists before selecting.
    _speaker_summary, _tp, _data, _segs = speaker_summary_or_die(
        session, str(args.speaker_id)
    )

    indices = select_relabel_segments(
        segments, str(args.speaker_id), ranges=requested_ranges
    )

    if not indices:
        die(
            f"No segments for speaker {args.speaker_id} match the requested "
            f"ranges in {transcript_path}."
        )

    changed_count = len(indices)
    # Summarize the labels before/after for the report.
    labels_before = sorted(
        {str(segments[i].get("speaker_name") or "") for i in indices}
    )

    if args.dry_run:
        result = {
            "action": "relabel",
            "status": "dry_run",
            "session": str(session),
            "transcript": str(transcript_path),
            "speaker_id": str(args.speaker_id),
            "name": name,
            "ranges": requested_ranges,
            "changed_segment_count": changed_count,
            "changed_indices": indices,
            "labels_before": labels_before,
            "label_after": name,
            "mutated_files": False,
            "next_step": (
                "Re-run without --dry-run to apply the relabel and regenerate "
                "transcript.json + transcript.md."
            ),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    # Real run: apply + regenerate.
    written = relabel_session_artifacts(
        transcript_path,
        data,
        segments,
        indices,
        name,
        session,
        speaker_id=str(args.speaker_id),
        ranges=requested_ranges,
    )
    result = {
        "action": "relabel",
        "status": "applied",
        "session": str(session),
        "transcript": str(transcript_path),
        "speaker_id": str(args.speaker_id),
        "name": name,
        "ranges": requested_ranges,
        "changed_segment_count": changed_count,
        "changed_indices": indices,
        "labels_before": labels_before,
        "label_after": name,
        "mutated_files": True,
        "merged_json_path": written["merged_json_path"],
        "merged_md_path": written["merged_md_path"],
        "per_source": written["per_source"],
        "note": (
            "speaker_id values are preserved; only speaker_name changed on "
            "selected segments. transcript.md and per-source JSON artifacts "
            "regenerated."
        ),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Name/enroll diarised speakers one at a time.")
    sub = parser.add_subparsers(dest="command", required=True)

    list_p = sub.add_parser("list", help="List diarised speakers in a session")
    list_p.add_argument("--session", required=True, help="Recording session directory")
    list_p.add_argument("--all", action="store_true", help="Include speakers that already have speaker_name labels")
    list_p.set_defaults(func=do_list)

    audit_p = sub.add_parser(
        "audit",
        help="Summarize speaker clusters and flag possible mixed clusters (read-only)",
    )
    audit_p.add_argument("--session", required=True, help="Recording session directory")
    audit_p.add_argument(
        "--force",
        action="store_true",
        help="Recompute the audit even if <session>/speaker_audit.json already exists",
    )
    audit_p.add_argument(
        "--min-useful-speech",
        type=float,
        default=DEFAULT_AUDIT_MIN_USEFUL_SPEECH,
        help=(
            "Clusters with less than this many seconds of useful speech are "
            f"classified 'unknown' (default {DEFAULT_AUDIT_MIN_USEFUL_SPEECH})"
        ),
    )
    audit_p.add_argument(
        "--mixed-span-ratio",
        type=float,
        default=DEFAULT_AUDIT_MIXED_SPAN_RATIO,
        help=(
            "Clusters whose time-span to useful-speech ratio exceeds this are "
            f"classified 'mixed_suspected' (default {DEFAULT_AUDIT_MIXED_SPAN_RATIO})"
        ),
    )
    audit_p.add_argument(
        "--json",
        default=None,
        help="Optional path to also write the audit JSON (in addition to <session>/speaker_audit.json)",
    )
    audit_p.set_defaults(func=do_audit)

    preview_p = sub.add_parser("preview", help="Build and play one speaker preview clip")
    preview_p.add_argument("--session", required=True, help="Recording session directory")
    preview_p.add_argument("--speaker-id", required=True, help="Diarised speaker id")
    preview_p.add_argument("--preview-seconds", type=float, default=12.0, help="Seconds to include/play (default 12)")
    preview_p.add_argument("--no-play", action="store_true", help="Build clip without playing it")
    preview_p.add_argument("--no-normalize", action="store_true", help="Do not loudness-normalize the preview clip")
    preview_p.set_defaults(func=do_preview)

    purity_p = sub.add_parser(
        "purity-preview",
        help="Preview early/middle/late + best-energy clips of one cluster to detect mixed speakers",
    )
    purity_p.add_argument("--session", required=True, help="Recording session directory")
    purity_p.add_argument("--speaker-id", required=True, help="Diarised speaker id to preview")
    purity_p.add_argument(
        "--range",
        action="append",
        default=None,
        dest="range",
        help=(
            "Restrict the universe of speech to these time ranges before selecting "
            "early/middle/late windows (repeatable). Each value is 'start-end' in "
            "seconds (123.4-180.0), MM:SS (02:03-03:00), or HH:MM:SS."
        ),
    )
    purity_p.add_argument("--preview-seconds", type=float, default=12.0, help="Target seconds per chronological clip (default 12)")
    purity_p.add_argument("--no-play", action="store_true", help="Build all clips without playing them")
    purity_p.add_argument("--no-normalize", action="store_true", help="Do not loudness-normalize the preview clips")
    purity_p.set_defaults(func=do_purity_preview)

    enroll_p = sub.add_parser("enroll", help="Enroll one speaker after user supplies a name")
    enroll_p.add_argument("--session", required=True, help="Recording session directory")
    enroll_p.add_argument("--speaker-id", required=True, help="Diarised speaker id")
    enroll_p.add_argument("--name", required=True, help="Display name to enroll")
    enroll_p.add_argument("--preview-seconds", type=float, default=0.0, help="Seconds for stt name-speakers' internal preview during enrollment (default 0; preview separately first)")
    enroll_p.add_argument("--sample-seconds", type=float, default=60.0, help="Stored sample clip cap; embedding still uses all speech via stt name-speakers (default 60)")
    enroll_p.add_argument("--no-normalize", action="store_true", help="Do not normalize stored sample clip")
    enroll_p.add_argument(
        "--no-enroll",
        action="store_true",
        help=(
            "Dry run: evaluate the whole-cluster enrollment safety guard and "
            "show what would happen, without enrolling or saving a profile"
        ),
    )
    enroll_p.set_defaults(func=do_enroll)

    enroll_ranges_p = sub.add_parser(
        "enroll-ranges",
        help=(
            "Generate a range-limited enrollment sample for one speaker "
            "(no profile mutation in this version)"
        ),
    )
    enroll_ranges_p.add_argument("--session", required=True, help="Recording session directory")
    enroll_ranges_p.add_argument("--speaker-id", required=True, help="Diarised speaker id")
    enroll_ranges_p.add_argument(
        "--range",
        action="append",
        default=None,
        dest="range",
        help=(
            "Time range of confirmed speech to enroll from (repeatable; at least "
            "one required). Each value is 'start-end' in seconds (123.4-180.0), "
            "MM:SS (02:03-03:00), or HH:MM:SS."
        ),
    )
    enroll_ranges_p.add_argument("--name", required=True, help="Display name to enroll")
    enroll_ranges_p.add_argument(
        "--source",
        default=None,
        help="Audio source track ('mic' or 'system'); resolved from transcript if omitted",
    )
    enroll_ranges_p.add_argument(
        "--sample-seconds",
        type=float,
        default=60.0,
        help="Maximum seconds of audio to sample for enrollment (default 60)",
    )
    enroll_ranges_p.add_argument(
        "--no-normalize",
        action="store_true",
        help="Do not loudness-normalize the generated enrollment sample",
    )
    enroll_ranges_p.add_argument(
        "--no-enroll",
        action="store_true",
        help=(
            "Validation-only dry run: describe the planned enrollment and write "
            "no files. Without this flag, a range-limited sample WAV + metadata "
            "are generated under .speaker-clips/ (status sample_ready); profile "
            "enrollment is still NOT performed (task 03c)."
        ),
    )
    enroll_ranges_p.set_defaults(func=do_enroll_ranges)

    suggest_p = sub.add_parser(
        "suggest-labels",
        help=(
            "Non-mutating speaker label suggestions: match clusters against "
            "enrolled profiles, flag duplicate/mixed clusters (read-only)"
        ),
    )
    suggest_p.add_argument("--session", required=True, help="Recording session directory")
    suggest_p.add_argument("--force", action="store_true", help="Recompute even if <session>/speaker_label_suggestions.json already exists")
    suggest_p.add_argument("--threshold", type=float, default=DEFAULT_SUGGEST_THRESHOLD, help=f"Minimum confidence to consider a profile match (default {DEFAULT_SUGGEST_THRESHOLD})")
    suggest_p.add_argument("--margin", type=float, default=DEFAULT_SUGGEST_MARGIN, help=f"Minimum margin over runner-up profile to consider a match (default {DEFAULT_SUGGEST_MARGIN})")
    suggest_p.add_argument("--provider", default="mfcc-test", help="Embedding provider to use (default mfcc-test; use speechbrain for real recognition)")
    suggest_p.add_argument("--minimum-speech-seconds", type=float, default=8.0, help="Minimum total speech seconds to extract an embedding per cluster (default 8)")
    suggest_p.add_argument("--no-windows", dest="no_windows", action="store_true", help="Skip per-window mixed-cluster detection")
    suggest_p.add_argument("--n-windows", type=int, default=2, help="Number of chronological windows for mixed-cluster detection (default 2)")
    suggest_p.add_argument("--output", default=None, help="Optional path to write the suggestions JSON (default <session>/speaker_label_suggestions.json)")
    suggest_p.set_defaults(func=do_suggest_labels)

    relabel_p = sub.add_parser(
        "relabel",
        help=(
            "Apply a human-confirmed speaker_name to selected transcript "
            "segments (by speaker id + time ranges), preserving speaker_id"
        ),
    )
    relabel_p.add_argument("--session", required=True, help="Recording session directory")
    relabel_p.add_argument("--speaker-id", required=True, help="Diarised speaker id whose segments to relabel")
    relabel_p.add_argument(
        "--range",
        action="append",
        default=None,
        dest="range",
        help=(
            "Time range of segments to relabel (repeatable). Each value is "
            "'start-end' in seconds (123.4-180.0), MM:SS (02:03-03:00), or "
            "HH:MM:SS. When omitted, ALL of the speaker's segments are "
            "relabeled (whole-cluster relabel)."
        ),
    )
    relabel_p.add_argument("--name", required=True, help="Display name to apply to the selected segments")
    relabel_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the planned relabel without writing any files",
    )
    relabel_p.set_defaults(func=do_relabel)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
