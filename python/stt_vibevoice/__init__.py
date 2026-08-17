"""stt_vibevoice: MLX VibeVoice transcription backend for the stt-cli project.

This package provides the Python-side transcription pipeline used by the
Swift `stt` CLI (see docs/SWIFT_CLI_AUDIO_CAPTURE_DECISION.md).
It is intentionally split so that the pure-logic pieces (chunking math,
segment merging, environment diagnostics, audio normalization) can be unit
tested without requiring MLX, mlx-audio, or any downloaded model.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
