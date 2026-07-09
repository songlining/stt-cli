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
import os
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


def _speechbrain_cache_dir(model_id: str) -> Path:
    """Stable local cache dir for a downloaded speechbrain model.

    Defaults to ``~/.cache/stt-cli/speechbrain/<model-slug>`` so repeated
    extractions reuse the already-downloaded model instead of speechbrain's
    default behavior of writing ``./pretrained_models/...`` relative to the
    current working directory. Override with ``STT_SPEECHBRAIN_CACHE``.
    """
    cache_root = os.environ.get("STT_SPEECHBRAIN_CACHE", "").strip()
    base = Path(cache_root).expanduser() if cache_root else Path.home() / ".cache" / "stt-cli" / "speechbrain"
    slug = model_id.replace("/", "__")
    return base / slug


def _speechbrain_run_opts() -> Dict[str, str]:
    """Device selection for SpeechBrain inference.

    Always returns CPU. SpeechBrain 1.1's EncoderClassifier is not fully
    MPS-compatible (raises 'object has no attribute device_type') on Apple
    Silicon. CPU is correct and fast enough for short enrollment/ID clips.
    Override is possible via the STT_SPEECHBRAIN_DEVICE env var if a future
    SpeechBrain release adds reliable MPS support.
    """
    import os

    return {"device": os.environ.get("STT_SPEECHBRAIN_DEVICE", "cpu")}


# Sample rate expected by the speechbrain ECAPA-VoxCeleb model.
ECAPA_SAMPLE_RATE = 16000


def _resample(data, orig_sr: int, target_sr: int):
    """Resample a 1-D float numpy array from ``orig_sr`` to ``target_sr``.

    Uses ``scipy.signal.resample_poly`` (available wherever speechbrain is).
    Falls back to linear interpolation via ``numpy.interp`` if scipy is absent.
    """
    if orig_sr == target_sr:
        return data
    try:
        from math import gcd
        from scipy.signal import resample_poly  # type: ignore
        g = gcd(int(orig_sr), int(target_sr))
        return resample_poly(data, int(target_sr) // g, int(orig_sr) // g).astype(
            data.dtype
        )
    except ImportError:
        import numpy as np
        n_out = int(round(len(data) * target_sr / orig_sr))
        return np.interp(
            np.linspace(0, len(data) - 1, n_out), np.arange(len(data)), data
        ).astype(data.dtype)


def _load_audio_tensor(audio_path: Path):
    """Load a WAV file as a (1, samples) float32 torch tensor at 16 kHz mono.

    Multi-channel audio is downmixed to mono; audio not already at 16 kHz is
    resampled (the ECAPA model expects 16 kHz). Uses soundfile (bundled with
    speechbrain) instead of torchaudio.load, because torchaudio >= 2.11
    requires the optional torchcodec package. Falls back to torchaudio if
    soundfile is unavailable (that path also normalises to mono + 16 kHz).
    """
    import numpy as np  # noqa: F401  (used by fallback path)
    import torch  # type: ignore
    try:
        import soundfile as sf  # type: ignore
        data, sample_rate = sf.read(str(audio_path))
        # soundfile returns (samples, channels) for multi-channel,
        # (samples,) for mono.
        if data.ndim > 1:
            data = data.mean(axis=1)  # downmix to mono
        if sample_rate != ECAPA_SAMPLE_RATE:
            data = _resample(data, sample_rate, ECAPA_SAMPLE_RATE)
        signal = torch.from_numpy(np.ascontiguousarray(data)).float().unsqueeze(0)
        return signal
    except ImportError:
        import torchaudio  # type: ignore
        signal, sample_rate = torchaudio.load(str(audio_path))
        if signal.shape[0] > 1:
            signal = signal.mean(dim=0, keepdim=True)  # downmix to mono
        if sample_rate != ECAPA_SAMPLE_RATE:
            resampler = torchaudio.transforms.Resample(sample_rate, ECAPA_SAMPLE_RATE)
            signal = resampler(signal)
        return signal


def _import_speechbrain_encoder():
    """Import the speechbrain EncoderClassifier, raising an actionable error
    if the optional dependency stack is missing.

    Centralised so the single-file and batched embedding paths share one
    missing-dependency message.
    """
    try:
        from speechbrain.inference.speaker import EncoderClassifier  # type: ignore
        return EncoderClassifier
    except Exception as error:
        raise SpeakerIdError(
            "The 'speechbrain' provider requires the optional 'speechbrain' and "
            "'torchaudio' packages, plus a Python 3.11 or 3.12 runtime (PyTorch "
            "does not yet support newer CPython releases on Apple Silicon as of "
            "this writing). Install them (e.g. `./scripts/bootstrap-python-backend.sh "
            "--python python3.11 --speechbrain --check`, or `pip install speechbrain "
            "torchaudio`) or use --provider mfcc-test for testing."
        ) from error


def _speechbrain_from_hparams(EncoderClassifier):
    """Instantiate the ECAPA classifier with the standard cache/device options.

    Returns ``(classifier, model_id)``. Centralised so the single-file and
    batched embedding paths never drift on model id / cache dir / run opts.
    """
    model_id = _speechbrain_model_id()
    classifier = EncoderClassifier.from_hparams(
        source=model_id,
        savedir=str(_speechbrain_cache_dir(model_id)),
        run_opts=_speechbrain_run_opts(),
    )
    return classifier, model_id


def _speechbrain_embed_with(classifier, audio_path: Path) -> List[float]:
    """Encode one audio file through an already-loaded classifier."""
    signal = _load_audio_tensor(audio_path)
    embedding = classifier.encode_batch(signal)
    return embedding.squeeze().tolist()


def _speechbrain_load_classifier():
    """Load the ECAPA classifier once (for batched embedding)."""
    EncoderClassifier = _import_speechbrain_encoder()
    try:
        return _speechbrain_from_hparams(EncoderClassifier)
    except SpeakerIdError:
        raise
    except Exception as error:
        model_id = _speechbrain_model_id()
        raise SpeakerIdError(
            f"speechbrain provider failed to load model '{model_id}': {error}. "
            "This is often a missing/corrupt model download (check network access and "
            f"{_speechbrain_cache_dir(model_id)})."
        ) from error


def _speechbrain_embed_file(audio_path: Path) -> List[float]:
    """Embed a single file, loading the model for this call."""
    try:
        EncoderClassifier = _import_speechbrain_encoder()
        classifier, _model_id = _speechbrain_from_hparams(EncoderClassifier)
        return _speechbrain_embed_with(classifier, audio_path)
    except SpeakerIdError:
        raise
    except Exception as error:
        model_id = _speechbrain_model_id()
        raise SpeakerIdError(
            f"speechbrain provider failed to extract an embedding from '{audio_path}': {error}. "
            "This is often a missing/corrupt model download (check network access and "
            f"{_speechbrain_cache_dir(model_id)}) or an unsupported audio file."
        ) from error


def _speechbrain_embed_files(audio_paths: Sequence[Path]) -> Tuple[List[List[float]], str]:
    """Embed multiple files, loading the ECAPA model exactly once.

    This is critical for diarisation, which may embed hundreds of segments:
    loading the ~80MB model per segment (as ``_speechbrain_embed_file`` does)
    would be catastrophically slow on a real recording.
    """
    classifier, model_id = _speechbrain_load_classifier()
    embeddings: List[List[float]] = []
    for path in audio_paths:
        try:
            embeddings.append(_speechbrain_embed_with(classifier, Path(path)))
        except SpeakerIdError:
            raise
        except Exception as error:
            raise SpeakerIdError(
                f"speechbrain provider failed to extract an embedding from '{path}': {error}. "
                "This is often an unsupported/corrupt audio slice; the source recording "
                f"and model cache ({_speechbrain_cache_dir(model_id)}) may be worth checking."
            ) from error
    return embeddings, model_id


_PROVIDERS: Dict[str, Dict[str, Any]] = {
    "mfcc-test": {"model_id": _mfcc_test_model_id, "embed_file": _mfcc_test_embed_file},
    "speechbrain": {
        "model_id": _speechbrain_model_id,
        "embed_file": _speechbrain_embed_file,
        "embed_files": _speechbrain_embed_files,
    },
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


def embed_audio_files(audio_paths: Sequence[Path], provider: str) -> Tuple[List[List[float]], str]:
    """Embed multiple audio files, loading the provider model once.

    Returns ``(embeddings, model_id)``. For the speechbrain provider the ECAPA
    classifier is loaded a single time regardless of how many paths are given
    (essential for diarisation, which may embed hundreds of segments). For
    other providers this loops over the per-file embedder.
    """
    spec = _provider(provider)
    embed_files_fn = spec.get("embed_files")
    if embed_files_fn is not None:
        return embed_files_fn([Path(p) for p in audio_paths])
    embeddings = [spec["embed_file"](Path(p)) for p in audio_paths]
    return embeddings, spec["model_id"]()


# ---------------------------------------------------------------------------
# Segment selection
# ---------------------------------------------------------------------------


def select_speaker_segments(
    segments: Sequence[dict], speaker_id: str, *, skip_nonspeech: bool = True
) -> List[Tuple[float, float]]:
    """Selects (start, end) ranges for a given diarized ``speaker_id``.

    Segments shorter than ``MINIMUM_SEGMENT_SECONDS`` are ignored. Segments
    missing start/end times are ignored. When ``skip_nonspeech`` is True
    (the default), segments whose text is a bracketed non-speech event tag
    (``[Silence]``, ``[Environmental Sounds]``, ``[Human Sounds]``, …) are
    skipped, so preview clips and embeddings are built from actual speech
    rather than dead air or mouse-clicking noise.
    """
    ranges: List[Tuple[float, float]] = []
    for segment in segments:
        seg_speaker = segment.get("speaker_id")
        if seg_speaker is None or str(seg_speaker) != str(speaker_id):
            continue
        if skip_nonspeech:
            text = str(segment.get("text", "")).strip()
            if text.startswith("[") and text.endswith("]"):
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


def cap_ranges_by_duration(ranges: List[Tuple[float, float]], max_seconds: float) -> List[Tuple[float, float]]:
    """Truncates a list of (start, end) ranges so the total speech duration
    does not exceed ``max_seconds``. The final range is truncated mid-way if
    needed. An empty/zero ``max_seconds`` returns the ranges unchanged.
    """
    if max_seconds <= 0:
        return list(ranges)
    capped: List[Tuple[float, float]] = []
    accumulated = 0.0
    for start, end in ranges:
        remaining = max_seconds - accumulated
        if remaining <= 0:
            break
        duration = end - start
        if duration <= remaining:
            capped.append((start, end))
            accumulated += duration
        else:
            capped.append((start, start + remaining))
            accumulated += remaining
            break
    return capped


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


def _cmd_concatenate(args: argparse.Namespace) -> int:
    """Build a single playable WAV from one diarized speaker's segments.

    No ML involved -- just WAV slicing + concatenation -- so it is fast and
    runs in either venv. Used by the interactive ``stt name-speakers`` loop to
    get a preview+enrollment audio clip per speaker.
    """
    from .wav_slicing import concatenate_wav_segments, normalize_wav_file, rank_ranges_by_energy

    audio_path = Path(args.audio)
    if not audio_path.exists():
        raise SpeakerIdError(f"Audio file not found: {audio_path}")
    if not args.segments:
        raise SpeakerIdError("--segments is required")
    if args.speaker_id is None:
        raise SpeakerIdError("--speaker-id is required")

    payload = json.loads(Path(args.segments).read_text(encoding="utf-8"))
    segments = payload.get("segments") or []
    ranges = select_speaker_segments(segments, args.speaker_id)
    if not ranges:
        raise SpeakerIdError(
            f"No usable segments found for speaker_id={args.speaker_id} "
            f"(each segment must be >= {MINIMUM_SEGMENT_SECONDS}s with valid timestamps)."
        )
    max_seconds = getattr(args, "max_seconds", None)
    use_best = getattr(args, "best_segments", False)
    if max_seconds is not None and max_seconds > 0:
        if use_best:
            # Pick the clearest-speech segments by energy so the limited
            # playback budget is spent on actual talking, not faint/paused
            # stretches. Then restore chronological order for playback.
            scored = rank_ranges_by_energy(audio_path, ranges)
            if scored:
                cap = float(max_seconds)
                chosen: List[Tuple[float, float]] = []
                accumulated = 0.0
                for start, end, _rms in scored:
                    remaining = cap - accumulated
                    if remaining <= 0:
                        break
                    duration = end - start
                    if duration <= remaining:
                        chosen.append((start, end))
                        accumulated += duration
                    else:
                        chosen.append((start, start + remaining))
                        accumulated += remaining
                        break
                chosen.sort(key=lambda r: r[0])
                ranges = chosen
        else:
            ranges = cap_ranges_by_duration(ranges, float(max_seconds))
    total = concatenate_wav_segments(audio_path, ranges, Path(args.out))
    applied_gain = 1.0
    if getattr(args, "normalize", False):
        try:
            applied_gain = normalize_wav_file(
                Path(args.out), target_dbfs=float(getattr(args, "target_loudness", -19.0) or -19.0)
            )
        except Exception:
            # Normalization is a best-effort playback nicety; never let a
            # failure here abort the concatenate (the raw clip is still valid).
            applied_gain = 1.0
    _write_json(
        {
            "speakerId": args.speaker_id,
            "outputPath": args.out,
            "segmentCount": len(ranges),
            "durationSeconds": round(total, 3),
            "normalizedGain": round(applied_gain, 3),
            "status": "ok",
        },
        args.json,
    )
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

    concat_parser = subparsers.add_parser(
        "concatenate",
        help="Build a playable WAV from one speaker's diarized segments (no ML).",
    )
    concat_parser.add_argument("--audio", required=True, help="Path to source audio (WAV).")
    concat_parser.add_argument("--segments", required=True, help="Path to transcript JSON with diarized segments.")
    concat_parser.add_argument("--speaker-id", required=True, help="Diarized speaker id to extract.")
    concat_parser.add_argument("--out", required=True, help="Path to write the concatenated speaker WAV.")
    concat_parser.add_argument("--max-seconds", type=float, default=None, help="Cap total duration by truncating ranges (0/unset = all speech).")
    concat_parser.add_argument("--best-segments", dest="best_segments", action="store_true", default=True, help="Pick the clearest-speech segments by energy (default on; use --no-best-segments to keep chronological order).")
    concat_parser.add_argument("--no-best-segments", dest="best_segments", action="store_false", help="Keep segments in chronological order instead of ranking by energy.")
    concat_parser.add_argument("--normalize", action="store_true", help="Apply perceptual speech-loudness normalization so quiet tracks (e.g. system-audio taps) play as loud as mic tracks.")
    concat_parser.add_argument("--target-loudness", type=float, default=-19.0, help="Target loudness in dBFS for --normalize (default -19.0).")
    concat_parser.add_argument("--json", help="Path to write JSON result (also printed to stdout).")
    concat_parser.set_defaults(func=_cmd_concatenate)

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
