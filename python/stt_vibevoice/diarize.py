"""Speaker diarisation for ASR segments.

Assigns a ``speaker_id`` (``"0"``, ``"1"``, ...) to each ASR segment by
clustering per-segment speaker embeddings. This is the component that *writes*
``speaker_id`` -- the ASR backend (MLX ``VibeVoice-ASR-8bit``) emits segments
with text + start/end timestamps but no speaker labels, and the downstream stack
(``chunking.py``, ``TranscriptMerger``, ``speaker_id.py``) only *reads*
``speaker_id`` if present.

Pipeline
--------
1. **Embed per segment.** For each input segment with valid ``start_time`` /
   ``end_time``, slice its audio out of the source WAV (``wav_slicing``) and
   extract a speaker embedding (``speaker_id.embed_audio_file``). Segments
   shorter than ``min_speech_seconds`` (default 1.0s) are *not* clustered --
   their embeddings are too noisy -- but they are still returned with an
   assigned speaker (see "Short-segment rule" below).
2. **Cluster.** Build an embedding matrix, compute cosine distance
   (``scipy.spatial.distance.pdist``), average-linkage agglomerative clustering
   (``scipy.cluster.hierarchy.linkage``), then ``fcluster``. With
   ``num_speakers`` the tree is cut to exactly N clusters (``maxclust``);
   otherwise it is cut at ``distance_threshold`` (``criterion='distance'``).
3. **Assign labels.** Map cluster ids to stable sequential speaker ids
   (``"0"``..``"k-1"``) ordered by first-appearance time.
4. **Output.** A JSON dict with the diarised segments plus per-speaker stats.

Heavy ML imports (``speechbrain`` / ``torch``) are performed lazily inside
``speaker_id.embed_audio_file`` so ``--help`` and the ``mfcc-test`` test path
never require them. ``scipy``/``numpy`` are imported lazily inside the cluster
function so a bare ``import stt_vibevoice.diarize`` stays cheap.

Short-segment rule
------------------
A segment skipped for clustering (too short, or missing timestamps) adopts the
speaker of the **nearest preceding clustered segment** (segments are processed in
list order, which ASR guarantees is chronological). If no clustered segment
precedes it (e.g. it is the very first segment and too short), it adopts the
**dominant** clustered speaker -- the speaker with the greatest total speech
duration among clustered segments. If there are no clustered segments at all,
every segment is assigned ``"0"``.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .speaker_id import SpeakerIdError, embed_audio_files
from .wav_slicing import write_wav_slice

# Segments shorter than this (seconds) are embedded-skipped for clustering.
MIN_SPEECH_SECONDS = 1.0
# Clusters whose total speech falls below this are considered fragments of a
# nearby larger speaker (ECAPA embeddings drift on meeting audio, producing
# spurious 1-3 segment clusters) and merged into the nearest larger cluster.
MIN_CLUSTER_SPEECH_SECONDS = 30.0
# Default cosine-distance cut for auto (unknown-speaker-count) mode.
# Calibrated against real meeting audio: intra-speaker ECAPA-VoxCeleb cosine
# *distance* on meeting-quality segments is ~0.24 median (embeddings are
# L2-normalised), so a cut at 0.15 severely over-splits (same-speaker pairs
# land above it). 0.40 (similarity ~0.60) clusters same-speaker segments
# together while keeping distinct speakers apart. Tune via
# --distance-threshold.
DEFAULT_DISTANCE_THRESHOLD = 0.40


class DiarizeError(Exception):
    """Raised for structured, user-facing diarisation failures."""


# ---------------------------------------------------------------------------
# Segment helpers
# ---------------------------------------------------------------------------

def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_input_segments(segments: Sequence[dict]) -> List[dict]:
    """Return a list of segment dicts with canonical time keys filled in.

    Preserves any extra keys from the input. Ensures ``start_time`` /
    ``end_time`` are floats (or ``None``) and derives ``duration`` when missing.
    Accepts ``start``/``end`` as aliases on read.
    """
    out: List[dict] = []
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        normalized = dict(seg)
        start = _to_float(seg.get("start_time", seg.get("start")))
        end = _to_float(seg.get("end_time", seg.get("end")))
        normalized["start_time"] = start
        normalized["end_time"] = end
        duration = _to_float(seg.get("duration"))
        if duration is None and start is not None and end is not None:
            duration = round(end - start, 3)
        normalized["duration"] = duration
        out.append(normalized)
    return out


def _seg_duration(seg: dict) -> Optional[float]:
    duration = _to_float(seg.get("duration"))
    if duration is not None:
        return duration
    start = seg.get("start_time")
    end = seg.get("end_time")
    if start is not None and end is not None:
        return end - start
    return None


def _is_nonspeech_tag(seg: dict) -> bool:
    """True if the segment text is a non-speech event tag.

    ASR backends emit bracketed pseudo-words like ``[Silence]``,
    ``[Environmental Sounds]``, ``[Human Sounds]``, ``[Music]`` for non-speech
    regions. Their speaker embeddings are noise, so clustering them
    produces wildly over-split speaker counts. Such segments are skipped for
    clustering (they inherit a speaker via the short-segment rule instead).
    """
    text = str(seg.get("text", "")).strip()
    return text.startswith("[") and text.endswith("]")


def _is_clusterable(seg: dict, min_speech_seconds: float) -> bool:
    if _is_nonspeech_tag(seg):
        return False
    start = seg.get("start_time")
    end = seg.get("end_time")
    if start is None or end is None:
        return False
    duration = _seg_duration(seg)
    return duration is not None and duration >= min_speech_seconds


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------

def _cluster_embeddings(
    embeddings: List[List[float]],
    num_speakers: Optional[int],
    distance_threshold: float,
) -> List[int]:
    """Cluster embeddings, returning raw ``fcluster`` labels (1-indexed).

    Returns an empty list when there are no embeddings and ``[1]`` for a single
    embedding (scipy cannot build a linkage tree from one point).
    """
    n = len(embeddings)
    if n == 0:
        return []
    if n == 1:
        return [1]

    # Lazy import so `import stt_vibevoice.diarize` and `--help` stay cheap.
    import numpy as np  # type: ignore
    from scipy.cluster.hierarchy import fcluster, linkage  # type: ignore
    from scipy.spatial.distance import pdist  # type: ignore

    matrix = np.asarray(embeddings, dtype=float)
    condensed = pdist(matrix, metric="cosine")
    # Zero-norm vectors yield NaN cosine distance; treat those pairs as
    # identical (distance 0) so e.g. silent slices cluster together.
    condensed = np.nan_to_num(condensed, nan=0.0)
    linkage_matrix = linkage(condensed, method="average")

    if num_speakers is not None:
        # Cut to exactly N clusters; if there are fewer points than N, scipy
        # yields at most n clusters (one per point).
        target = max(1, min(int(num_speakers), n))
        labels = fcluster(linkage_matrix, t=target, criterion="maxclust")
    else:
        labels = fcluster(
            linkage_matrix, t=float(distance_threshold), criterion="distance"
        )
    return labels.tolist()


def _sequential_labels(cluster_labels: Sequence[int]) -> List[str]:
    """Remap raw cluster ids to ``"0"``..``"k-1"`` ordered by first appearance."""
    remap: Dict[int, str] = {}
    next_id = 0
    out: List[str] = []
    for label in cluster_labels:
        if label not in remap:
            remap[label] = str(next_id)
            next_id += 1
        out.append(remap[label])
    return out


def cluster_speakers(
    embeddings: Sequence[Sequence[float]],
    num_speakers: Optional[int] = None,
    distance_threshold: float = DEFAULT_DISTANCE_THRESHOLD,
) -> List[str]:
    """Cluster embeddings into sequential speaker ids (``"0"``..``"k-1"``).

    Labels are stable and ordered by first appearance, so identical clusterings
    always map to the same speaker numbering regardless of scipy's internal
    cluster-id assignment.
    """
    raw = _cluster_embeddings(list(embeddings), num_speakers, distance_threshold)
    return _sequential_labels(raw)


def _merge_tiny_clusters(
    embeddings: List[List[float]],
    speaker_labels: List[str],
    durations: List[float],
    min_cluster_seconds: float = MIN_CLUSTER_SPEECH_SECONDS,
) -> List[str]:
    """Merge clusters with little speech into the nearest larger cluster.

    ECAPA embeddings drift on meeting-quality audio, so a single speaker often
    splinters into one large cluster plus several tiny (1-3 segment) fragments.
    This collapses fragments below ``min_cluster_seconds`` into the nearest
    non-tiny cluster by centroid cosine distance, then re-sequentialises.

    When ``num_speakers`` semantics matter (forced count) this is skipped by the
    caller. ``embeddings``/``durations`` align 1:1 with ``speaker_labels``
    (clusterable segments only). Returns updated ``speaker_labels``.
    """
    if not speaker_labels:
        return speaker_labels
    import numpy as np  # type: ignore

    labels = np.asarray(speaker_labels)
    unique = sorted(set(speaker_labels), key=lambda s: int(s))
    total = {
        s: sum(durations[i] for i in range(len(labels)) if labels[i] == s)
        for s in unique
    }
    big = [s for s in unique if total[s] >= min_cluster_seconds]
    tiny = [s for s in unique if total[s] < min_cluster_seconds]
    # Nothing to merge if there are no tiny clusters, or no big cluster to absorb.
    if not tiny or not big:
        return speaker_labels

    # Centroid per cluster (L2-normalised so dot product == cosine similarity).
    def centroid(s: str) -> np.ndarray:
        members = [np.asarray(embeddings[i]) for i in range(len(labels)) if labels[i] == s]
        c = np.mean(members, axis=0)
        norm = np.linalg.norm(c)
        return c / norm if norm > 0 else c

    big_centroids = {s: centroid(s) for s in big}
    remap: Dict[str, str] = {}
    for s in tiny:
        c = centroid(s)
        # Nearest big cluster by cosine similarity (highest dot product).
        nearest = max(big, key=lambda b: float(np.dot(c, big_centroids[b])))
        remap[s] = nearest

    merged = [remap.get(l, l) for l in speaker_labels]
    return _sequential_labels(merged)


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------

def _dominant_speaker(
    clusterable_speaker: Dict[int, str],
    segments: List[dict],
    clusterable_indices: List[int],
) -> str:
    """Speaker with the greatest total clustered speech; ``"0"`` if none."""
    if not clusterable_indices:
        return "0"
    totals: Dict[str, float] = {}
    for idx in clusterable_indices:
        speaker = clusterable_speaker[idx]
        duration = _seg_duration(segments[idx]) or 0.0
        totals[speaker] = totals.get(speaker, 0.0) + duration
    # Greatest total duration; tie-break deterministically on lowest speaker id.
    return min(totals.items(), key=lambda kv: (-kv[1], int(kv[0])))[0]


def _assign_speakers(
    segments: List[dict],
    clusterable_indices: List[int],
    speaker_labels: List[str],
) -> List[str]:
    """Return a speaker id for every segment (short ones use the rule above)."""
    clusterable_speaker: Dict[int, str] = {
        idx: speaker_labels[k] for k, idx in enumerate(clusterable_indices)
    }
    dominant = _dominant_speaker(clusterable_speaker, segments, clusterable_indices)

    assigned: List[str] = []
    last_speaker: Optional[str] = None
    for i, _seg in enumerate(segments):
        if i in clusterable_speaker:
            speaker = clusterable_speaker[i]
            assigned.append(speaker)
            last_speaker = speaker
        elif last_speaker is not None:
            # Nearest preceding clustered segment's speaker.
            assigned.append(last_speaker)
        else:
            # No clustered segment precedes -> dominant speaker.
            assigned.append(dominant)
    return assigned


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def diarize(
    audio_path: Path,
    segments: Sequence[dict],
    provider: str = "speechbrain",
    num_speakers: Optional[int] = None,
    distance_threshold: float = DEFAULT_DISTANCE_THRESHOLD,
    min_speech_seconds: float = MIN_SPEECH_SECONDS,
    speaker_id_offset: int = 0,
) -> Dict[str, Any]:
    """Diarise ``segments`` against ``audio_path`` and return the result dict.

    See module docstring for the full pipeline and the short-segment rule.
    ``speaker_id_offset`` (if non-zero) is added to every assigned speaker id
    after clustering, so separate diarisation runs over disjoint audio tracks
    (e.g. mic vs system) can share one unique speaker-id namespace without
    collisions.
    """
    audio_path = Path(audio_path)
    norm_segments = _normalize_input_segments(segments)

    clusterable_indices = [
        i
        for i, seg in enumerate(norm_segments)
        if _is_clusterable(seg, min_speech_seconds)
    ]

    # Embed each clusterable segment by slicing its audio out of the source WAV,
    # then batch-embed all slices with a single model load (critical for the
    # speechbrain provider: loading the ~80MB model per segment would be
    # catastrophically slow on a real recording).
    embeddings: List[List[float]] = []
    model_id: Optional[str] = None
    if clusterable_indices:
        with tempfile.TemporaryDirectory(prefix="stt_diarize_") as tmp:
            tmp_dir = Path(tmp)
            slice_paths: List[Path] = []
            for idx in clusterable_indices:
                seg = norm_segments[idx]
                slice_path = tmp_dir / f"seg-{idx:06d}.wav"
                write_wav_slice(audio_path, seg["start_time"], seg["end_time"], slice_path)
                slice_paths.append(slice_path)
            embeddings, model_id = embed_audio_files(slice_paths, provider)

    speaker_labels = cluster_speakers(
        embeddings,
        num_speakers=num_speakers,
        distance_threshold=distance_threshold,
    )
    # In auto mode, collapse tiny fragment clusters (< MIN_CLUSTER_SPEECH_SECONDS)
    # into the nearest larger cluster. Skipped when num_speakers is forced: the
    # caller has asserted the exact count.
    if num_speakers is None and clusterable_indices:
        clusterable_durations = [
            _seg_duration(norm_segments[idx]) or 0.0 for idx in clusterable_indices
        ]
        speaker_labels = _merge_tiny_clusters(
            embeddings, speaker_labels, clusterable_durations
        )
    assigned = _assign_speakers(norm_segments, clusterable_indices, speaker_labels)

    # Offset speaker ids so independent diarisation runs over disjoint audio
    # tracks (mic vs system) occupy a single unique namespace. Speaker labels
    # are numeric strings "0".."k-1"; the per-speaker stats below index
    # ``assigned[i]`` and sort speakers by ``int(s)``, so the offset
    # propagates correctly and numSpeakers is unchanged.
    if speaker_id_offset:
        assigned = [str(int(label) + speaker_id_offset) for label in assigned]

    out_segments: List[dict] = []
    for i, seg in enumerate(norm_segments):
        out_seg = dict(seg)
        out_seg["speaker_id"] = assigned[i]
        out_segments.append(out_seg)

    # Per-speaker statistics.
    stats: Dict[str, Dict[str, float]] = {}
    for i, seg in enumerate(norm_segments):
        speaker = assigned[i]
        duration = _seg_duration(seg) or 0.0
        entry = stats.setdefault(speaker, {"count": 0, "seconds": 0.0})
        entry["count"] += 1
        entry["seconds"] += duration
    speakers = [
        {
            "id": speaker,
            "segmentCount": int(stats[speaker]["count"]),
            "totalSpeechSeconds": round(stats[speaker]["seconds"], 3),
        }
        for speaker in sorted(stats.keys(), key=lambda s: int(s))
    ]

    return {
        "provider": provider,
        "model": model_id,
        "embeddingModel": model_id,
        "numSpeakers": len(speakers),
        "distanceThreshold": None if num_speakers is not None else float(distance_threshold),
        "segments": out_segments,
        "speakers": speakers,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_segments(path: Path) -> List[dict]:
    """Read a transcript JSON: ``{"segments": [...]}`` or a bare ``[...]``."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        return payload.get("segments") or []
    if isinstance(payload, list):
        return payload
    raise DiarizeError(
        f"--segments must be a JSON list or an object with a 'segments' list, got {type(payload).__name__}"
    )


def _write_json(payload: Dict[str, Any], json_path: Optional[str]) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True)
    if json_path:
        Path(json_path).write_text(text, encoding="utf-8")
    print(text)


def _cmd_diarize(args: argparse.Namespace) -> int:
    audio_path = Path(args.audio)
    if not audio_path.exists():
        raise DiarizeError(f"Audio file not found: {audio_path}")
    if not args.segments:
        raise DiarizeError("--segments is required")

    segments = _load_segments(Path(args.segments))
    payload = diarize(
        audio_path=audio_path,
        segments=segments,
        provider=args.provider,
        num_speakers=args.num_speakers,
        distance_threshold=args.distance_threshold,
        min_speech_seconds=args.min_speech_seconds,
        speaker_id_offset=args.speaker_id_offset,
    )
    _write_json(payload, args.json)
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m stt_vibevoice.diarize",
        description=(
            "Assign a speaker_id to each ASR segment by clustering per-segment "
            "speaker embeddings."
        ),
    )
    parser.add_argument("--audio", required=True, help="Source WAV file.")
    parser.add_argument(
        "--segments",
        required=True,
        help="Transcript JSON: {'segments': [...]} or a bare [...].",
    )
    parser.add_argument(
        "--provider",
        default="speechbrain",
        help="Embedding provider (default: speechbrain).",
    )
    count_group = parser.add_mutually_exclusive_group()
    count_group.add_argument(
        "--num-speakers",
        type=int,
        default=None,
        help="Force exactly N speakers (cuts the cluster tree to N).",
    )
    count_group.add_argument(
        "--distance-threshold",
        type=float,
        default=DEFAULT_DISTANCE_THRESHOLD,
        help=(
            "Cosine-distance cut for auto speaker count "
            f"(default: {DEFAULT_DISTANCE_THRESHOLD})."
        ),
    )
    parser.add_argument(
        "--min-speech-seconds",
        type=float,
        default=MIN_SPEECH_SECONDS,
        help=(
            "Segments shorter than this are not clustered but still get a "
            f"speaker (default: {MIN_SPEECH_SECONDS})."
        ),
    )
    parser.add_argument(
        "--speaker-id-offset",
        type=int,
        default=0,
        help=(
            "Add this integer to every assigned speaker id (default: 0). "
            "Use to keep separate diarisation runs over disjoint audio tracks "
            "(e.g. mic vs system) in one unique speaker-id namespace."
        ),
    )
    parser.add_argument(
        "--json",
        default=None,
        help="Also write the JSON result to this path.",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        return _cmd_diarize(args)
    except (DiarizeError, SpeakerIdError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
