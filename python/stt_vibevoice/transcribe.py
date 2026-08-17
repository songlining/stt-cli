"""Main entry point for local MLX VibeVoice transcription.

Usage:
    python3 -m stt_vibevoice.transcribe <audio_path> \
        [--output out.txt] [--json out.json] \
        [--device auto|gpu|cpu] [--model <path>] [--max-new-tokens N]

Design notes (see docs/SWIFT_CLI_AUDIO_CAPTURE_DECISION.md):
  - Audio is normalized to 16kHz mono 16-bit PCM WAV first (normalize.py).
  - mlx / mlx_audio are imported lazily, inside functions, so that running
    `--help` or `doctor`-style checks never requires MLX to be installed.
  - Long audio is chunked with overlap (chunking.py) and results are merged
    with boundary dedup, mirroring a reference VibeVoice transcriber.
  - Device selection defaults to "auto", with automatic fallback to CPU on
    Metal timeout / out-of-memory errors.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from .chunking import (
    compute_chunk_windows,
    flatten_segments,
    merge_chunk_segments,
    probe_wav,
)
from .normalize import normalize_audio
from .wav_slicing import write_wav_slice

DEFAULT_MODEL_PATH = "mlx-community/VibeVoice-ASR-8bit"
DEFAULT_DEVICE = "auto"
DEFAULT_MAX_NEW_TOKENS = 4096

CHUNKING_THRESHOLD_SECONDS = 10 * 60
CHUNK_LENGTH_SECONDS = 5 * 60
CHUNK_OVERLAP_SECONDS = 15

MLX_INSTALL_HINT = (
    "MLX / mlx-audio are not installed in this Python environment.\n"
    "This backend requires Apple Silicon macOS.\n\n"
    "To install:\n"
    "    python3 -m pip install --upgrade pip setuptools wheel\n"
    "    python3 -m pip install 'mlx-audio[stt]'\n\n"
    "Then re-run this command."
)


def _install_transformers_newline_tokenizer_compat() -> None:
    """Work around mlx-lm's Transformers 5 tokenizer registration bug.

    mlx-lm 0.31.x registers its internal NewlineTokenizer with
    `AutoTokenizer.register("NewlineTokenizer", ...)`. Transformers 5 expects a
    config class there and raises AttributeError for the string before VibeVoice
    transcription can start. The registration is unrelated to the VibeVoice ASR
    model path, so treating string-based registrations as a no-op preserves the
    supported mlx-audio/Transformers dependency set while avoiding the import
    crash.
    """
    try:
        from transformers import AutoTokenizer  # type: ignore
    except Exception:
        return

    current_register = getattr(AutoTokenizer, "register", None)
    if current_register is None or getattr(current_register, "_stt_string_compat", False):
        return

    def register_compat(config_class, *args, **kwargs):
        if isinstance(config_class, str):
            return None
        return current_register(config_class, *args, **kwargs)

    register_compat._stt_string_compat = True  # type: ignore[attr-defined]
    AutoTokenizer.register = register_compat  # type: ignore[method-assign]


def _import_mlx_backend():
    """Lazily import mlx / mlx_audio.stt.generate.

    Returns (mx, generate_transcription) on success. Raises ImportError on
    failure so the caller can print an actionable message and exit
    non-zero, instead of crashing the whole CLI module at import time.
    """
    import mlx.core as mx  # type: ignore
    from mlx_audio.stt.generate import generate_transcription  # type: ignore

    _install_transformers_newline_tokenizer_compat()
    return mx, generate_transcription


def _is_metal_timeout_error(message: str) -> bool:
    normalized = (message or "").lower()
    return "gpu timeout error" in normalized or "command buffer execution failed" in normalized


def _is_memory_pressure_error(message: str) -> bool:
    normalized = (message or "").lower()
    markers = (
        "out of memory",
        "resource exhausted",
        "insufficient memory",
        "memory pressure",
        "failed to allocate",
        "allocation failed",
    )
    return any(marker in normalized for marker in markers)


def _normalize_segment(segment: dict) -> dict:
    start_time = segment.get("start_time", segment.get("start"))
    end_time = segment.get("end_time", segment.get("end"))
    duration = segment.get("duration")
    if duration is None and start_time is not None and end_time is not None:
        try:
            duration = round(float(end_time) - float(start_time), 3)
        except (TypeError, ValueError):
            duration = None
    return {
        "text": (segment.get("text") or "").strip(),
        "start_time": start_time,
        "end_time": end_time,
        "duration": duration,
        "speaker_id": segment.get("speaker_id", segment.get("speaker")),
    }


def _normalize_segments(segments) -> List[dict]:
    normalized = []
    for segment in segments or []:
        if not isinstance(segment, dict):
            continue
        normalized_segment = _normalize_segment(segment)
        if normalized_segment["text"]:
            normalized.append(normalized_segment)
    return normalized


def _run_model_on_file(
    generate_transcription,
    mx,
    audio_path: Path,
    model_path: str,
    device: str,
    max_new_tokens: int,
) -> dict:
    """Invoke mlx_audio.stt.generate.generate_transcription on one file.

    Mirrors the call pattern in a reference VibeVoice transcriber.
    """
    if device == "cpu":
        mx.set_default_device(mx.cpu)
    elif device in {"gpu", "mps", "cuda", "auto", "xpu"}:
        mx.set_default_device(mx.gpu)

    with tempfile.TemporaryDirectory(prefix="stt-vibevoice-") as temp_dir:
        output_stem = Path(temp_dir) / audio_path.stem
        generate_transcription(
            model=model_path,
            audio=str(audio_path),
            output_path=str(output_stem),
            format="json",
            max_tokens=max_new_tokens,
        )

        output_file = output_stem.with_suffix(".json")
        if not output_file.exists():
            candidates = sorted(output_stem.parent.glob(f"{output_stem.name}*.json"))
            if not candidates:
                raise RuntimeError(
                    f"MLX transcription did not produce a JSON file for {audio_path}"
                )
            output_file = candidates[0]

        with open(output_file, "r", encoding="utf-8") as handle:
            payload = json.load(handle)

    payload["device"] = device
    return payload


def _transcribe_with_fallback(
    generate_transcription,
    mx,
    audio_path: Path,
    model_path: str,
    device: str,
    max_new_tokens: int,
) -> dict:
    """Run the model, falling back to CPU on Metal timeout/OOM errors."""
    try:
        return _run_model_on_file(
            generate_transcription, mx, audio_path, model_path, device, max_new_tokens
        )
    except RuntimeError as error:
        message = str(error)
        if device != "cpu" and (
            _is_metal_timeout_error(message) or _is_memory_pressure_error(message)
        ):
            print(
                f"[stt_vibevoice] GPU run failed ({message}); retrying on CPU...",
                file=sys.stderr,
            )
            return _run_model_on_file(
                generate_transcription, mx, audio_path, model_path, "cpu", max_new_tokens
            )
        raise


def transcribe_file(
    audio_path: Path,
    model_path: str = DEFAULT_MODEL_PATH,
    device: str = DEFAULT_DEVICE,
    max_new_tokens: int = DEFAULT_MAX_NEW_TOKENS,
) -> Dict[str, Any]:
    """Full transcription pipeline: normalize -> (chunk) -> model -> merge.

    Returns a result dict with keys: text, diarised_text, segments,
    duration_seconds, backend, device, model_path.
    """
    mx, generate_transcription = _import_mlx_backend()

    normalized_path = normalize_audio(audio_path)
    wav_info = probe_wav(normalized_path)
    duration = wav_info.duration_seconds

    should_chunk = duration >= CHUNKING_THRESHOLD_SECONDS

    if not should_chunk:
        payload = _transcribe_with_fallback(
            generate_transcription, mx, normalized_path, model_path, device, max_new_tokens
        )
        segments = _normalize_segments(payload.get("segments") or [])
        plain_text, diarised_text = flatten_segments(segments)
        used_device = payload.get("device", device)
    else:
        windows = compute_chunk_windows(duration, CHUNK_LENGTH_SECONDS, CHUNK_OVERLAP_SECONDS)
        chunk_results: List[Tuple[float, List[dict]]] = []
        used_device = device
        with tempfile.TemporaryDirectory(prefix="stt-vibevoice-chunks-") as chunk_dir:
            for index, (start, end) in enumerate(windows):
                chunk_path = Path(chunk_dir) / f"chunk_{index:03d}.wav"
                write_wav_slice(normalized_path, start, end, chunk_path)
                payload = _transcribe_with_fallback(
                    generate_transcription, mx, chunk_path, model_path, device, max_new_tokens
                )
                used_device = payload.get("device", used_device)
                chunk_segments = _normalize_segments(payload.get("segments") or [])
                chunk_results.append((start, chunk_segments))

        segments = merge_chunk_segments(chunk_results, overlap_seconds=CHUNK_OVERLAP_SECONDS)
        plain_text, diarised_text = flatten_segments(segments)

    return {
        "text": plain_text or "No speech detected in audio",
        "diarised_text": diarised_text,
        "segments": segments,
        "duration_seconds": duration,
        "backend": "vibevoice-mlx",
        "device": used_device,
        "model_path": model_path,
        "chunked": should_chunk,
    }


def _write_text_transcript(
    output_path: Path,
    audio_path: Path,
    result: Dict[str, Any],
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    header_lines = [
        f"Session: {audio_path.stem}",
        f"File: {audio_path.name}",
        f"Date: {now}",
        f"Transcription backend: {result.get('backend')}",
        f"Device: {result.get('device')}",
        f"Model: {result.get('model_path')}",
        f"Duration (s): {result.get('duration_seconds')}",
        "=" * 60,
    ]
    body = result.get("diarised_text") or result.get("text") or ""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(header_lines) + "\n" + body + "\n", encoding="utf-8")


def _write_json_transcript(
    output_path: Path,
    audio_path: Path,
    result: Dict[str, Any],
) -> None:
    payload = {
        "audio_file": str(audio_path),
        "backend": result.get("backend"),
        "device": result.get("device"),
        "model_path": result.get("model_path"),
        "duration_seconds": result.get("duration_seconds"),
        "chunked": result.get("chunked"),
        "text": result.get("text"),
        "diarised_text": result.get("diarised_text"),
        "segments": result.get("segments"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m stt_vibevoice.transcribe",
        description="Transcribe an audio file locally using MLX VibeVoice-ASR.",
    )
    parser.add_argument("audio_path", help="Path to the input audio file")
    parser.add_argument("--output", help="Path to write the human-readable .txt transcript")
    parser.add_argument("--json", dest="json_output", help="Path to write the machine-readable JSON transcript")
    parser.add_argument(
        "--device",
        choices=["auto", "gpu", "cpu"],
        default=DEFAULT_DEVICE,
        help="Compute device to use (default: auto)",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL_PATH,
        help=f"MLX VibeVoice model path (default: {DEFAULT_MODEL_PATH})",
    )
    parser.add_argument(
        "--max-new-tokens",
        type=int,
        default=DEFAULT_MAX_NEW_TOKENS,
        help=f"Maximum new tokens for generation (default: {DEFAULT_MAX_NEW_TOKENS})",
    )
    parser.add_argument("--version", action="version", version=f"stt_vibevoice {__version__}")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    audio_path = Path(args.audio_path).expanduser()
    if not audio_path.exists():
        print(f"Error: audio file not found: {audio_path}", file=sys.stderr)
        return 2

    try:
        result = transcribe_file(
            audio_path,
            model_path=args.model,
            device=args.device,
            max_new_tokens=args.max_new_tokens,
        )
    except ImportError:
        print(f"Error: {MLX_INSTALL_HINT}", file=sys.stderr)
        return 3
    except RuntimeError as error:
        print(f"Error: transcription failed: {error}", file=sys.stderr)
        return 4

    output_path = Path(args.output) if args.output else audio_path.with_suffix(".txt")
    _write_text_transcript(output_path, audio_path, result)
    print(f"Wrote transcript: {output_path}")

    json_path = Path(args.json_output) if args.json_output else None
    if json_path:
        _write_json_transcript(json_path, audio_path, result)
        print(f"Wrote JSON transcript: {json_path}")

    # Print a compact machine-readable summary after human-readable log lines
    # so the Swift CLI can parse transcript text/backend/duration from stdout
    # even when files were also written.
    summary = {
        "audio_file": str(audio_path),
        "backend": result.get("backend"),
        "device": result.get("device"),
        "model_path": result.get("model_path"),
        "duration": result.get("duration_seconds"),
        "duration_seconds": result.get("duration_seconds"),
        "text": result.get("text"),
        "transcript_text": result.get("text"),
        "diarised_text": result.get("diarised_text"),
        "transcript_file": str(output_path),
        "json_file": str(json_path) if json_path else None,
        "chunked": result.get("chunked"),
    }
    print(json.dumps(summary, sort_keys=True))

    return 0


if __name__ == "__main__":
    sys.exit(main())
