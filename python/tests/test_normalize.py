from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path
from unittest.mock import patch

import pytest

from stt_vibevoice.normalize import (
    TARGET_CHANNELS,
    TARGET_SAMPLE_RATE,
    TARGET_SAMPLE_WIDTH,
    is_already_normalized,
    normalize_audio,
)


def _write_wav(path: Path, framerate: int, channels: int, sampwidth: int, duration_seconds: float = 0.5) -> None:
    n_frames = int(duration_seconds * framerate)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(sampwidth)
        handle.setframerate(framerate)
        handle.writeframes(b"\x00" * (n_frames * sampwidth * channels))


class TestIsAlreadyNormalized:
    def test_matching_wav_is_already_normalized(self, tmp_path):
        wav_path = tmp_path / "already_normalized.wav"
        _write_wav(wav_path, framerate=TARGET_SAMPLE_RATE, channels=TARGET_CHANNELS, sampwidth=TARGET_SAMPLE_WIDTH)

        assert is_already_normalized(wav_path) is True

    def test_non_matching_wav_is_not_already_normalized(self, tmp_path):
        wav_path = tmp_path / "not_normalized.wav"
        _write_wav(wav_path, framerate=44100, channels=2, sampwidth=2)

        assert is_already_normalized(wav_path) is False

    def test_non_wav_extension_is_not_already_normalized(self, tmp_path):
        fake_path = tmp_path / "recording.webm"
        fake_path.write_bytes(b"not really a wav file")

        assert is_already_normalized(fake_path) is False

    def test_corrupt_wav_file_returns_false_not_raise(self, tmp_path):
        corrupt_path = tmp_path / "corrupt.wav"
        corrupt_path.write_bytes(b"RIFF....WAVEfmt garbage")

        assert is_already_normalized(corrupt_path) is False


class TestNormalizeAudioFastPath:
    def test_returns_original_path_unchanged_when_already_normalized(self, tmp_path):
        wav_path = tmp_path / "already_normalized.wav"
        _write_wav(wav_path, framerate=TARGET_SAMPLE_RATE, channels=TARGET_CHANNELS, sampwidth=TARGET_SAMPLE_WIDTH)

        with patch("stt_vibevoice.normalize.subprocess.run") as mock_run:
            result = normalize_audio(wav_path)

        mock_run.assert_not_called()
        assert result == wav_path

    def test_missing_input_file_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            normalize_audio(tmp_path / "does-not-exist.wav")


class TestNormalizeAudioConversionPath:
    def test_non_matching_wav_triggers_ffmpeg_call_mocked(self, tmp_path):
        wav_path = tmp_path / "not_normalized.wav"
        _write_wav(wav_path, framerate=44100, channels=2, sampwidth=2)
        output_path = tmp_path / "converted.wav"

        def fake_run(command, capture_output, text, timeout):
            # Simulate ffmpeg succeeding by creating the expected output file.
            _write_wav(
                output_path,
                framerate=TARGET_SAMPLE_RATE,
                channels=TARGET_CHANNELS,
                sampwidth=TARGET_SAMPLE_WIDTH,
            )
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with patch("stt_vibevoice.normalize.subprocess.run", side_effect=fake_run) as mock_run:
            result = normalize_audio(wav_path, output_path=output_path)

        mock_run.assert_called_once()
        called_command = mock_run.call_args.args[0]
        assert called_command[0] == "ffmpeg"
        assert str(wav_path) in called_command
        assert result == output_path
        assert is_already_normalized(result) is True

    def test_ffmpeg_failure_raises_runtime_error_with_stderr(self, tmp_path):
        wav_path = tmp_path / "not_normalized.wav"
        _write_wav(wav_path, framerate=44100, channels=2, sampwidth=2)
        output_path = tmp_path / "converted.wav"

        fake_result = subprocess.CompletedProcess(
            args=["ffmpeg"], returncode=1, stdout="", stderr="Invalid data found when processing input"
        )

        with patch("stt_vibevoice.normalize.subprocess.run", return_value=fake_result):
            with pytest.raises(RuntimeError) as exc_info:
                normalize_audio(wav_path, output_path=output_path)

        assert "Invalid data found" in str(exc_info.value)

    @pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed on this machine")
    def test_real_ffmpeg_conversion_end_to_end(self, tmp_path):
        # Only runs if ffmpeg is actually present; exercises the real subprocess path.
        wav_path = tmp_path / "not_normalized.wav"
        _write_wav(wav_path, framerate=44100, channels=2, sampwidth=2, duration_seconds=0.2)
        output_path = tmp_path / "converted.wav"

        result = normalize_audio(wav_path, output_path=output_path)

        assert result == output_path
        assert is_already_normalized(result) is True
