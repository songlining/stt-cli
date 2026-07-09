"""Tests for ``stt_vibevoice.diarize``.

All tests are deterministic and never load speechbrain/ECAPA. The pure
clustering tests exercise the math directly on synthetic vectors; the
end-to-end tests use the stdlib-only ``mfcc-test`` provider.
"""

from __future__ import annotations

import json
import math
import struct
import wave
from pathlib import Path

import pytest

from stt_vibevoice import diarize as diarize_mod


# ---------------------------------------------------------------------------
# WAV synthesis helpers (stdlib only)
# ---------------------------------------------------------------------------

def _write_sine_wav(
    path: Path,
    duration_seconds: float,
    framerate: int = 16000,
    frequency: float = 220.0,
    amplitude: float = 8000.0,
) -> None:
    """Write a mono 16-bit PCM sine WAV using only the stdlib."""
    n = int(duration_seconds * framerate)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(framerate)
        frames = bytearray()
        for i in range(n):
            sample = int(amplitude * math.sin(2.0 * math.pi * frequency * (i / framerate)))
            frames += struct.pack("<h", sample)
        handle.writeframes(bytes(frames))


def _write_regions_wav(
    path: Path,
    regions: list,
    framerate: int = 16000,
) -> None:
    """Concatenate (duration, frequency, amplitude) regions into one WAV.

    Distinct regions yield distinct ``mfcc-test`` embeddings (the mean-abs /
    zero-crossing statistics differ), so segments sliced from different regions
    are separable. Used where a test needs more than one cluster.
    """
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(framerate)
        frames = bytearray()
        for duration_seconds, frequency, amplitude in regions:
            n = int(duration_seconds * framerate)
            for i in range(n):
                sample = int(amplitude * math.sin(2.0 * math.pi * frequency * (i / framerate)))
                frames += struct.pack("<h", sample)
        handle.writeframes(bytes(frames))


def _segment(text: str, start: float, end: float) -> dict:
    return {
        "text": text,
        "start_time": start,
        "end_time": end,
        "duration": round(end - start, 3),
        "speaker_id": None,
    }


# ---------------------------------------------------------------------------
# Pure clustering logic
# ---------------------------------------------------------------------------

class TestClusterSpeakers:
    def test_two_clusters_with_num_speakers(self):
        v = [1.0, 0.0]
        w = [0.0, 1.0]
        labels = diarize_mod.cluster_speakers([v, v, w, w], num_speakers=2)
        assert labels == ["0", "0", "1", "1"]

    def test_labels_sequential_by_first_appearance(self):
        # w appears before v -> w becomes "0", v becomes "1".
        w = [0.0, 1.0]
        v = [1.0, 0.0]
        labels = diarize_mod.cluster_speakers([w, v, v, w], distance_threshold=0.5)
        assert labels == ["0", "1", "1", "0"]

    def test_single_cluster_collapse(self):
        v = [1.0, 0.0]
        labels = diarize_mod.cluster_speakers([v, v, v, v], num_speakers=1)
        assert labels == ["0", "0", "0", "0"]

    def test_auto_two_clusters_via_distance(self):
        v = [1.0, 0.0]
        w = [0.0, 1.0]
        labels = diarize_mod.cluster_speakers([v, v, w, w], distance_threshold=0.5)
        assert labels == ["0", "0", "1", "1"]
        assert len(set(labels)) == 2

    def test_three_clusters(self):
        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        c = [0.0, 0.0, 1.0]
        labels = diarize_mod.cluster_speakers([a, b, c], num_speakers=3)
        assert len(set(labels)) == 3
        assert sorted(set(labels)) == ["0", "1", "2"]

    def test_num_speakers_larger_than_points_collapses(self):
        # Only two distinct clusters exist; asking for 3 still yields 2.
        v = [1.0, 0.0]
        w = [0.0, 1.0]
        labels = diarize_mod.cluster_speakers([v, v, w, w], num_speakers=3)
        assert len(set(labels)) == 2

    def test_empty_and_single(self):
        assert diarize_mod.cluster_speakers([]) == []
        assert diarize_mod.cluster_speakers([[1.0, 2.0, 3.0]]) == ["0"]


# ---------------------------------------------------------------------------
# End-to-end via the deterministic mfcc-test provider
# ---------------------------------------------------------------------------

class TestMfccTestEndToEnd:
    def test_every_segment_gets_speaker(self, tmp_path):
        wav_path = tmp_path / "audio.wav"
        _write_sine_wav(wav_path, 3.0)

        segments = [
            _segment("first", 0.0, 1.0),
            _segment("second", 1.0, 2.0),
            _segment("third", 2.0, 3.0),
        ]

        result = diarize_mod.diarize(
            audio_path=wav_path,
            segments=segments,
            provider="mfcc-test",
            min_speech_seconds=1.0,
        )

        assert result["provider"] == "mfcc-test"
        assert result["embeddingModel"] == result["model"]
        assert result["numSpeakers"] >= 1
        assert len(result["segments"]) == 3
        for seg in result["segments"]:
            assert seg["speaker_id"] is not None
            assert isinstance(seg["speaker_id"], str)
        # Speaker stats line up with the segments.
        assert sum(s["segmentCount"] for s in result["speakers"]) == 3

    def test_short_segment_inherits_preceding_speaker(self, tmp_path):
        wav_path = tmp_path / "audio.wav"
        _write_sine_wav(wav_path, 3.0)

        segments = [
            _segment("long one", 0.0, 1.2),
            _segment("tiny", 1.2, 1.5),   # < min_speech_seconds -> not clustered
            _segment("long two", 1.5, 2.7),
        ]

        result = diarize_mod.diarize(
            audio_path=wav_path,
            segments=segments,
            provider="mfcc-test",
            min_speech_seconds=1.0,
        )

        out = result["segments"]
        assert len(out) == 3
        # Every segment, including the short one, has a speaker id.
        for seg in out:
            assert seg["speaker_id"] is not None
        # The short middle segment adopts the preceding (clustered) speaker.
        assert out[1]["speaker_id"] == out[0]["speaker_id"]

    def test_num_speakers_honoured(self, tmp_path):
        # Distinct audio regions so the slices are not bit-identical: even tiny
        # non-zero cosine distance lets maxclust force the requested split.
        wav_path = tmp_path / "audio.wav"
        _write_regions_wav(
            wav_path,
            [(1.0, 220.0, 8000.0), (1.0, 220.0, 1000.0), (1.0, 440.0, 8000.0)],
        )

        segments = [_segment(f"seg {i}", float(i), float(i + 1)) for i in range(3)]

        result = diarize_mod.diarize(
            audio_path=wav_path,
            segments=segments,
            provider="mfcc-test",
            num_speakers=2,
        )

        assert result["numSpeakers"] == 2
        assert result["distanceThreshold"] is None
        assert {s["speaker_id"] for s in result["segments"]} == {"0", "1"}

    def test_no_clusterable_segments_assigns_zero(self, tmp_path):
        wav_path = tmp_path / "audio.wav"
        _write_sine_wav(wav_path, 2.0)

        # All segments too short to cluster -> everything gets speaker "0".
        segments = [
            _segment("a", 0.0, 0.3),
            _segment("b", 0.3, 0.6),
        ]

        result = diarize_mod.diarize(
            audio_path=wav_path,
            segments=segments,
            provider="mfcc-test",
            min_speech_seconds=1.0,
        )

        assert result["numSpeakers"] == 1
        assert [s["speaker_id"] for s in result["segments"]] == ["0", "0"]

    def test_speaker_id_offset_shifts_namespace(self, tmp_path):
        # Two distinct audio regions -> two clusters (speaker "0","0","1").
        # With speaker_id_offset=5 the ids become "5","5","6" and numSpeakers
        # stays at 2 (offset only shifts the namespace, not the count).
        wav_path = tmp_path / "audio.wav"
        _write_regions_wav(
            wav_path,
            [(1.0, 220.0, 8000.0), (1.0, 220.0, 8000.0), (1.0, 440.0, 1000.0)],
        )

        segments = [_segment(f"seg {i}", float(i), float(i + 1)) for i in range(3)]

        result = diarize_mod.diarize(
            audio_path=wav_path,
            segments=segments,
            provider="mfcc-test",
            num_speakers=2,
            speaker_id_offset=5,
        )

        ids = [s["speaker_id"] for s in result["segments"]]
        assert ids == ["5", "5", "6"], ids
        assert result["numSpeakers"] == 2
        assert {s["id"] for s in result["speakers"]} == {"5", "6"}


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

class TestCli:
    def test_segments_file_dict_and_list_shapes(self, tmp_path):
        wav_path = tmp_path / "audio.wav"
        _write_sine_wav(wav_path, 3.0)
        segments = [_segment(f"seg {i}", float(i), float(i + 1)) for i in range(3)]

        # {"segments": [...]} shape
        dict_path = tmp_path / "dict.json"
        dict_path.write_text(json.dumps({"segments": segments}), encoding="utf-8")
        out_dict = tmp_path / "out_dict.json"
        rc = diarize_mod.main(
            [
                "--audio", str(wav_path),
                "--segments", str(dict_path),
                "--provider", "mfcc-test",
                "--json", str(out_dict),
            ]
        )
        assert rc == 0
        written = json.loads(out_dict.read_text(encoding="utf-8"))
        assert written["numSpeakers"] >= 1
        assert all("speaker_id" in s for s in written["segments"])

        # bare [...] shape
        list_path = tmp_path / "list.json"
        list_path.write_text(json.dumps(segments), encoding="utf-8")
        rc2 = diarize_mod.main(
            [
                "--audio", str(wav_path),
                "--segments", str(list_path),
                "--provider", "mfcc-test",
            ]
        )
        assert rc2 == 0

    def test_missing_audio_returns_error_code(self, tmp_path, capsys):
        segments_path = tmp_path / "segs.json"
        segments_path.write_text(json.dumps([_segment("x", 0.0, 1.0)]), encoding="utf-8")
        rc = diarize_mod.main(
            [
                "--audio", str(tmp_path / "nope.wav"),
                "--segments", str(segments_path),
                "--provider", "mfcc-test",
            ]
        )
        assert rc == 1
        captured = capsys.readouterr()
        assert "error" in json.loads(captured.err)
