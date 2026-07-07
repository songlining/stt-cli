"""Environment diagnostics for the MLX VibeVoice transcription backend.

``doctor()`` never raises: every check is wrapped so that a missing
dependency, missing binary, or unset env var just shows up as ``False``/
``None`` in the report rather than crashing the CLI. This mirrors the
"doctor"/"setup" command idea from LEARNINGS.md.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# Modules mlx-audio's VibeVoice ASR path depends on. Mirrors
# VibeVoiceTranscriber.REQUIRED_MODULES in vibecorder's src/transcriber.py.
REQUIRED_MODULES = ("mlx", "mlx_audio", "huggingface_hub", "sentencepiece")

RUNTIME_ENV_VAR = "STT_VIBEVOICE_RUNTIME"

# Default model cache locations used by huggingface_hub / mlx-community models.
DEFAULT_HF_CACHE_DIRS = (
    Path.home() / ".cache" / "huggingface" / "hub",
    Path.home() / ".cache" / "huggingface" / "models",
)


def _is_apple_silicon() -> bool:
    try:
        return platform.system() == "Darwin" and platform.machine().lower() in {
            "arm64",
            "aarch64",
        }
    except Exception:
        return False


def _which_safe(binary: str) -> Optional[str]:
    try:
        return shutil.which(binary)
    except Exception:
        return None


def _module_importable(name: str) -> bool:
    """Best-effort check that a module can be imported.

    Uses ``importlib.util.find_spec`` first (cheap, no side effects), and
    falls back to a real import attempt in case ``find_spec`` itself raises
    (some packages misbehave under introspection). Never raises.
    """
    try:
        spec = importlib.util.find_spec(name)
        if spec is not None:
            return True
    except Exception:
        pass

    try:
        __import__(name)
        return True
    except Exception:
        return False


def _check_runtime_path() -> Dict[str, Any]:
    configured = os.environ.get(RUNTIME_ENV_VAR, "").strip()
    if not configured:
        return {
            "configured": False,
            "path": None,
            "exists": False,
            "python_exists": False,
        }

    runtime_path = Path(configured).expanduser()
    python_path = runtime_path / ".venv" / "bin" / "python"
    return {
        "configured": True,
        "path": str(runtime_path),
        "exists": runtime_path.exists(),
        "python_exists": python_path.exists(),
        "python_path": str(python_path),
    }


def _check_model_cache() -> Dict[str, Any]:
    hf_home = os.environ.get("HF_HOME", "").strip()
    candidate_dirs = []
    if hf_home:
        candidate_dirs.append(Path(hf_home).expanduser() / "hub")
    candidate_dirs.extend(DEFAULT_HF_CACHE_DIRS)

    for cache_dir in candidate_dirs:
        try:
            if cache_dir.exists():
                entries = []
                try:
                    entries = [p.name for p in cache_dir.iterdir()][:25]
                except Exception:
                    entries = []
                return {
                    "path": str(cache_dir),
                    "exists": True,
                    "entries": entries,
                }
        except Exception:
            continue

    return {
        "path": str(candidate_dirs[0]) if candidate_dirs else None,
        "exists": False,
        "entries": [],
    }


def _check_speaker_identification() -> Dict[str, Any]:
    """Reports speaker-identification provider availability.

    ``mfcc-test`` is always available (stdlib-only, used for tests/smoke
    checks). ``speechbrain`` is optional and only reported as available if
    both ``speechbrain`` and ``torchaudio`` are importable. This section is
    purely informational and never affects the overall ``ready`` flag,
    since speaker identification is an opt-in feature.

    PyTorch (and therefore ``speechbrain``/``torchaudio``) does not reliably
    support every CPython release on Apple Silicon; Python 3.11 or 3.12 is
    the recommended interpreter for the speechbrain provider as of this
    writing. ``python_version_supported`` is a best-effort hint, not a hard
    gate: even on an unsupported interpreter we still just report whatever
    ``speechbrain``/``torchaudio`` import checks actually find.
    """
    speechbrain_available = _module_importable("speechbrain") and _module_importable("torchaudio")
    version_info = sys.version_info
    python_version_supported = (version_info.major, version_info.minor) in {(3, 11), (3, 12)}
    return {
        "providers": {
            "mfcc-test": True,
            "speechbrain": speechbrain_available,
        },
        "python_version": platform.python_version(),
        "python_version_recommended_for_speechbrain": python_version_supported,
    }


def doctor() -> Dict[str, Any]:
    """Run environment checks and return a structured report.

    Guaranteed to never raise, even when MLX/mlx-audio/ffmpeg/the runtime
    venv are entirely absent - that's the whole point of a doctor command.
    """
    report: Dict[str, Any] = {}

    machine = ""
    system = ""
    try:
        machine = platform.machine()
        system = platform.system()
    except Exception:
        pass

    report["platform"] = {
        "system": system,
        "machine": machine,
        "is_apple_silicon": _is_apple_silicon(),
    }

    report["binaries"] = {
        "ffmpeg": _which_safe("ffmpeg"),
        "ffprobe": _which_safe("ffprobe"),
    }

    modules: Dict[str, bool] = {}
    for name in REQUIRED_MODULES:
        modules[name] = _module_importable(name)
    report["modules"] = modules
    report["modules_all_available"] = all(modules.values())

    report["runtime"] = _check_runtime_path()
    report["model_cache"] = _check_model_cache()
    report["speaker_identification"] = _check_speaker_identification()

    report["ready"] = bool(
        report["platform"]["is_apple_silicon"]
        and report["binaries"]["ffmpeg"]
        and report["binaries"]["ffprobe"]
        and report["modules_all_available"]
    )

    return report


def missing_requirements(report: Dict[str, Any]) -> list[str]:
    """Return human-readable blockers that keep the backend from being ready."""
    missing: list[str] = []

    platform_info = report.get("platform", {})
    if not platform_info.get("is_apple_silicon"):
        missing.append("Apple Silicon macOS")

    binaries = report.get("binaries", {})
    for name in ("ffmpeg", "ffprobe"):
        if not binaries.get(name):
            missing.append(name)

    modules = report.get("modules", {})
    for name in REQUIRED_MODULES:
        if not modules.get(name):
            missing.append(f"python module {name}")

    return missing


def setup_hint(report: Dict[str, Any]) -> str:
    """Return an actionable setup hint for missing backend dependencies."""
    missing = missing_requirements(report)
    if not missing:
        return "backend ready; no setup needed"

    commands = [
        "python3 -m pip install --upgrade pip setuptools wheel",
        "python3 -m pip install 'mlx-audio[stt]'",
    ]
    return "missing: {}; setup: {}".format(
        ", ".join(missing),
        " && ".join(commands),
    )


def format_report(report: Dict[str, Any]) -> str:
    """Render a doctor() report as human-readable text for CLI output."""
    lines = []
    platform_info = report.get("platform", {})
    lines.append(
        "Apple Silicon: {status} ({system} {machine})".format(
            status="yes" if platform_info.get("is_apple_silicon") else "no",
            system=platform_info.get("system") or "unknown",
            machine=platform_info.get("machine") or "unknown",
        )
    )

    binaries = report.get("binaries", {})
    for name in ("ffmpeg", "ffprobe"):
        path = binaries.get(name)
        lines.append(f"{name}: {'found at ' + path if path else 'NOT FOUND'}")

    modules = report.get("modules", {})
    for name, available in modules.items():
        lines.append(f"module {name}: {'available' if available else 'missing'}")

    runtime = report.get("runtime", {})
    if runtime.get("configured"):
        lines.append(
            "runtime venv ({}): {}".format(
                runtime.get("path"),
                "python present" if runtime.get("python_exists") else "python missing",
            )
        )
    else:
        lines.append(f"runtime venv: not configured (set {RUNTIME_ENV_VAR})")

    cache = report.get("model_cache", {})
    lines.append(
        "model cache ({}): {}".format(
            cache.get("path"),
            "exists" if cache.get("exists") else "not found",
        )
    )

    speaker_id = report.get("speaker_identification", {})
    providers = speaker_id.get("providers", {})
    for name, available in providers.items():
        lines.append(f"speaker-id provider {name}: {'available' if available else 'not installed'}")
    if providers.get("speechbrain") is False:
        py_version = speaker_id.get("python_version", "unknown")
        recommended = speaker_id.get("python_version_recommended_for_speechbrain")
        if recommended is False:
            lines.append(
                f"speaker-id provider speechbrain hint: current interpreter is Python "
                f"{py_version}; use Python 3.11 or 3.12 (e.g. "
                "./scripts/bootstrap-python-backend.sh --python python3.11 --speechbrain), "
                "then install with `pip install speechbrain torchaudio`."
            )
        else:
            lines.append(
                "speaker-id provider speechbrain hint: install with "
                "./scripts/bootstrap-python-backend.sh --speechbrain (or `pip install "
                "speechbrain torchaudio`)."
            )

    lines.append("overall ready: {}".format("yes" if report.get("ready") else "no"))
    if not report.get("ready"):
        lines.append(f"setup hint: {setup_hint(report)}")
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m stt_vibevoice.status",
        description="Check local readiness for the MLX VibeVoice transcription backend.",
    )
    parser.add_argument("--json", action="store_true", help="Print the structured status report as JSON")
    parser.add_argument(
        "--fail-if-not-ready",
        action="store_true",
        help="Exit non-zero when required backend dependencies are missing",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    report = doctor()

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_report(report))

    if args.fail_if_not_ready and not report.get("ready"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
