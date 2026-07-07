"""Speaker embedding extraction and matching backend.

Implements the ``extract`` and ``match`` CLI contracts documented in
``SPEAKER_IDENTIFICATION_PLAN.md``. Heavy ML dependencies (e.g.
``speechbrain``) are imported lazily inside provider functions so that
``python -m stt_vibevoice.speaker_id --help`` and the default test suite
never require them.

Providers:

- ``mfcc-test``: a lightweight, fully deterministic stdlib-only provider
  used for tests and smoke checks. It is *not* a real speaker
  recognition model and must never be presented to users as accurate
  identity matching -- it exists purely so the surrounding contract
  (extraction, matching, CLI plumbing) can be exercised without any ML
  runtime installed.
- ``speechbrain``: optional real provider, imported lazily. Raises a
  clear error if the ``speechbrain`` package is not installed.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .chunking import probe_wav
from .wav_slicing import concatenate_wav_segments, read_pcm16_mono_samples

MINIMUM_SEGMENT_SECONDS = 0.5


class SpeakerIdError(Exception):
    """Raised for structured, user-facing speaker-id backend failures."""


# ---------------------------------------------------------------------------
# Provider framework
# ---------------------------------------------------------------------------


def _mfcc_test_model_id() -> str:
    return "stt-vibevoice/mfcc-test-v1"


def _mfcc_test_embed_samples(samples: Sequence[int], num_bands: int = 13) -> List[float]:
    """Deterministic, dependency-free pseudo-embedding.

    Splits the sample stream into ``num_bands`` roughly-equal frames and
    computes two cheap per-frame statistics (mean absolute amplitude and
    zero-crossing rate), producing a fixed-length vector. Same audio
    content always yields the same vector; different audio content
    generally yields different vectors. This is deliberately *not* a real
    MFCC/speaker-embedding implementation.
    """
    dims = num_bands * 2
    if not samples:
        return [0.0] * dims

    frame_size = max(1, len(samples) // num_bands)
    feats: List[float] = []
    for i in range(num_bands):
        start = i * frame_size
        end = start + frame_size if i < num_bands - 1 else len(samples)
        frame = samples[start:end] or [0]
        mean_abs = sum(abs(s) for s in frame) / len(frame)
        crossings = sum(
            1 for a, b in zip(frame, frame[1:]) if (a >= 0) != (b >= 0)
        )
        zcr = crossings / max(1, len(frame) - 1)
        feats.append(float(mean_abs))
        feats.append(float(zcr) * 1000.0)

    norm = math.sqrt(sum(f * f for f in feats)) or 1.0
    return [f / norm for f in feats]


def _mfcc_test_embed_file(audio_path: Path) -> List[float]:
    samples = read_pcm16_mono_samples(audio_path)
    return _mfcc_test_embed_samples(samples)


def _speechbrain_model_id() -> str:
    return "speechbrain/spkrec-ecapa-voxceleb"


def _speechbrain_embed_file(audio_path: Path) -> List[float]:
    try:
        import torchaudio  # type: ignore
        from speechbrain.inference.speaker import EncoderClassifier  # type: ignore
    except Exception as error:  # pragma: no cover - exercised only with optional deps installed
        raise SpeakerIdError(
            "The 'speechbrain' provider requires the optional 'speechbrain' and "
            "'torchaudio' packages. Install them (e.g. `pip install speechbrain "
            "torchaudio`) or use --provider mfcc-test for testing."
        ) from error

    classifier = EncoderClassifier.from_hparams(source=_speechbrain_model_id())
    signal, _fs = torchaudio.load(str(audio_path))
    embedding = classifier.encode_batch(signal)
    return embedding.squeeze().tolist()


_PROVIDERS: Dict[str, Dict[str, Any]] = {
    "mfcc-test": {"model_id": _mfcc_test_model_id, "embed_file": _mfcc_test_embed_file},
    "speechbrain": {"model_id": _speechbrain_model_id, "embed_file": _speechbrain_embed_file},
}


def known_providers() -> List[str]:
    return sorted(_PROVIDERS.keys())


def _provider(name: str) -> Dict[str, Any]:
    if name not in _PROVIDERS:
        raise SpeakerIdError(
            f"Unknown speaker-id provider '{name}'. Known providers: {', '.join(known_providers())}"
        )
    return _PROVIDERS[name]


def embed_audio_file(audio_path: Path, provider: str) -> Tuple[List[float], str]:
    """Returns (embedding, model_id) for the given audio file and provider."""
    spec = _provider(provider)
    embedding = spec["embed_file"](audio_path)
    return embedding, spec["model_id"]()


# ---------------------------------------------------------------------------
# Segment selection
# ---------------------------------------------------------------------------


def select_speaker_segments(segments: Sequence[dict], speaker_id: str) -> List[Tuple[float, float]]:
    """Selects (start, end) ranges for a given diarized ``speaker_id``.

    Segments shorter than ``MINIMUM_SEGMENT_SECONDS`` are ignored. Segments
    missing start/end times are ignored.
    """
    ranges: List[Tuple[float, float]] = []
    for segment in segments:
        seg_speaker = segment.get("speaker_id")
        if seg_speaker is None or str(seg_speaker) != str(speaker_id):
            continue
        start = segment.get("start_time", segment.get("start"))
        end = segment.get("end_time", segment.get("end"))
        if start is None or end is None:
            continue
        try:
            start_f = float(start)
            end_f = float(end)
        except (TypeError, ValueError):
            continue
        if end_f - start_f < MINIMUM_SEGMENT_SECONDS:
            continue
        ranges.append((start_f, end_f))
    return ranges


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def extract_whole_audio(
    audio_path: Path,
    provider: str,
    minimum_speech_seconds: float,
    speaker_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Extracts an embedding from the entire given audio file (enrollment path)."""
    info = probe_wav(audio_path)
    if info.duration_seconds < minimum_speech_seconds:
        return _too_short_result(speaker_id, provider, info.duration_seconds, segment_count=1)

    embedding, model_id = embed_audio_file(audio_path, provider)
    return {
        "speakerId": speaker_id,
        "provider": provider,
        "model": model_id,
        "embedding": embedding,
        "durationSeconds": round(info.duration_seconds, 3),
        "segmentCount": 1,
        "status": "ok",
    }


def extract_speaker_segments(
    audio_path: Path,
    segments: Sequence[dict],
    speaker_id: str,
    provider: str,
    minimum_speech_seconds: float,
) -> Dict[str, Any]:
    """Extracts an embedding for one diarized speaker from a full recording.

    Concatenates all segments belonging to ``speaker_id`` (each at least
    ``MINIMUM_SEGMENT_SECONDS`` long) into a temporary WAV, then extracts an
    embedding from the concatenated speaker-only audio.
    """
    ranges = select_speaker_segments(segments, speaker_id)
    total_seconds = sum(end - start for start, end in ranges)

    if not ranges or total_seconds < minimum_speech_seconds:
        return _too_short_result(speaker_id, provider, total_seconds, segment_count=len(ranges))

    with tempfile.TemporaryDirectory(prefix="stt_speaker_id_") as tmp_dir:
        concat_path = Path(tmp_dir) / f"speaker-{speaker_id}.wav"
        actual_seconds = concatenate_wav_segments(audio_path, ranges, concat_path)
        embedding, model_id = embed_audio_file(concat_path, provider)

    return {
        "speakerId": speaker_id,
        "provider": provider,
        "model": model_id,
        "embedding": embedding,
        "durationSeconds": round(actual_seconds, 3),
        "segmentCount": len(ranges),
        "status": "ok",
    }


def _too_short_result(
    speaker_id: Optional[str], provider: str, duration_seconds: float, segment_count: int
) -> Dict[str, Any]:
    return {
        "speakerId": speaker_id,
        "provider": provider,
        "model": None,
        "embedding": None,
        "durationSeconds": round(duration_seconds, 3),
        "segmentCount": segment_count,
        "status": "too_short",
    }


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        raise SpeakerIdError(
            f"Embedding dimension mismatch: candidate has {len(a)} dims, profile has {len(b)} dims."
        )
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def match_candidate(
    candidate: Dict[str, Any],
    profiles: Sequence[Dict[str, Any]],
    threshold: float,
    margin: float,
) -> Dict[str, Any]:
    """Compares one candidate embedding against a flattened profile list.

    See ``SPEAKER_IDENTIFICATION_PLAN.md`` "Python matching contract" for
    the exact output schema. Swift owns final cross-speaker conflict
    resolution; this only scores one candidate against all profiles.
    """
    candidate_embedding = candidate.get("embedding")
    candidate_provider = candidate.get("provider")
    candidate_model = candidate.get("model")

    warnings: List[str] = []
    skipped: List[Dict[str, str]] = []
    candidates: List[Dict[str, Any]] = []

    if not candidate_embedding:
        warnings.append("Candidate has no embedding; cannot match.")
        return {
            "bestMatch": None,
            "candidates": [],
            "skippedProfiles": skipped,
            "warnings": warnings,
        }

    for profile in profiles:
        profile_id = profile.get("id")
        display_name = profile.get("displayName")
        profile_provider = profile.get("embeddingProvider")
        profile_model = profile.get("embeddingModel")
        profile_embedding = profile.get("embedding")

        if profile_provider != candidate_provider or profile_model != candidate_model:
            skipped.append(
                {
                    "profileId": profile_id,
                    "displayName": display_name,
                    "reason": "provider_model_mismatch",
                }
            )
            continue

        if not profile_embedding:
            skipped.append(
                {
                    "profileId": profile_id,
                    "displayName": display_name,
                    "reason": "missing_embedding",
                }
            )
            continue

        try:
            score = cosine_similarity(candidate_embedding, profile_embedding)
        except SpeakerIdError:
            skipped.append(
                {
                    "profileId": profile_id,
                    "displayName": display_name,
                    "reason": "embedding_dimension_mismatch",
                }
            )
            continue

        candidates.append(
            {"profileId": profile_id, "displayName": display_name, "confidence": score}
        )

    candidates.sort(key=lambda c: c["confidence"], reverse=True)

    best_match: Optional[Dict[str, Any]] = None
    if candidates:
        best = candidates[0]
        runner_up_score = candidates[1]["confidence"] if len(candidates) > 1 else None
        match_margin = (
            best["confidence"] - runner_up_score if runner_up_score is not None else best["confidence"]
        )

        if best["confidence"] < threshold:
            status = "below_threshold"
            matched = False
        elif match_margin < margin:
            status = "ambiguous"
            matched = False
        else:
            status = "matched"
            matched = True

        best_match = {
            "profileId": best["profileId"],
            "displayName": best["displayName"],
            "confidence": best["confidence"],
            "margin": round(match_margin, 6),
            "matched": matched,
            "status": status,
        }

    return {
        "bestMatch": best_match,
        "candidates": candidates,
        "skippedProfiles": skipped,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _write_json(payload: Dict[str, Any], json_path: Optional[str]) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True)
    if json_path:
        Path(json_path).write_text(text, encoding="utf-8")
    print(text)


def _cmd_extract(args: argparse.Namespace) -> int:
    audio_path = Path(args.audio)
    if not audio_path.exists():
        raise SpeakerIdError(f"Audio file not found: {audio_path}")

    if args.segments:
        if args.speaker_id is None:
            raise SpeakerIdError("--speaker-id is required when --segments is given")
        payload = json.loads(Path(args.segments).read_text(encoding="utf-8"))
        segments = payload.get("segments") or []
        result = extract_speaker_segments(
            audio_path=audio_path,
            segments=segments,
            speaker_id=args.speaker_id,
            provider=args.provider,
            minimum_speech_seconds=args.minimum_speech_seconds,
        )
    else:
        result = extract_whole_audio(
            audio_path=audio_path,
            provider=args.provider,
            minimum_speech_seconds=args.minimum_speech_seconds,
            speaker_id=args.speaker_id,
        )

    _write_json(result, args.json)
    return 0 if result.get("status") == "ok" else 2


def _cmd_match(args: argparse.Namespace) -> int:
    candidate = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
    profiles_payload = json.loads(Path(args.profiles).read_text(encoding="utf-8"))
    profiles = profiles_payload.get("profiles") or []

    result = match_candidate(
        candidate=candidate,
        profiles=profiles,
        threshold=args.threshold,
        margin=args.margin,
    )
    _write_json(result, args.json)
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m stt_vibevoice.speaker_id",
        description="Speaker embedding extraction and matching backend.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract_parser = subparsers.add_parser("extract", help="Extract a speaker embedding from audio.")
    extract_parser.add_argument("--audio", required=True, help="Path to source audio (WAV).")
    extract_parser.add_argument("--segments", help="Path to transcript JSON with diarized segments.")
    extract_parser.add_argument("--speaker-id", help="Diarized speaker id to extract (with --segments).")
    extract_parser.add_argument("--provider", default="mfcc-test", help="Embedding provider to use.")
    extract_parser.add_argument(
        "--minimum-speech-seconds", type=float, default=8.0, help="Minimum total speech seconds required."
    )
    extract_parser.add_argument("--json", help="Path to write JSON result (also printed to stdout).")
    extract_parser.set_defaults(func=_cmd_extract)

    match_parser = subparsers.add_parser("match", help="Match a candidate embedding against enrolled profiles.")
    match_parser.add_argument("--candidate", required=True, help="Path to candidate embedding JSON.")
    match_parser.add_argument("--profiles", required=True, help="Path to flattened profiles JSON.")
    match_parser.add_argument("--threshold", type=float, default=0.78, help="Minimum confidence to match.")
    match_parser.add_argument("--margin", type=float, default=0.05, help="Minimum margin over runner-up.")
    match_parser.add_argument("--json", help="Path to write JSON result (also printed to stdout).")
    match_parser.set_defaults(func=_cmd_match)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SpeakerIdError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
