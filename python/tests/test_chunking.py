from __future__ import annotations

import wave
from pathlib import Path

import pytest

from stt_vibevoice.chunking import (
    compute_chunk_windows,
    flatten_segments,
    merge_chunk_segments,
    offset_segment,
    probe_wav,
    segments_are_duplicates,
)


def _write_silent_wav(path: Path, duration_seconds: float, framerate: int = 16000) -> None:
    n_frames = int(duration_seconds * framerate)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(framerate)
        handle.writeframes(b"\x00\x00" * n_frames)


class TestProbeWav:
    def test_probe_wav_reads_expected_properties(self, tmp_path):
        wav_path = tmp_path / "sample.wav"
        _write_silent_wav(wav_path, duration_seconds=2.0, framerate=16000)

        info = probe_wav(wav_path)

        assert info.sample_rate == 16000
        assert info.channels == 1
        assert info.sample_width == 2
        assert info.duration_seconds == pytest.approx(2.0, abs=0.01)
        assert info.frame_count == 32000

    def test_probe_wav_missing_file_raises(self, tmp_path):
        with pytest.raises(Exception):
            probe_wav(tmp_path / "does-not-exist.wav")


class TestComputeChunkWindows:
    def test_short_audio_returns_single_window(self):
        windows = compute_chunk_windows(duration_seconds=60, chunk_length_seconds=300, overlap_seconds=15)
        assert windows == [(0.0, 60.0)]

    def test_zero_duration_returns_empty(self):
        assert compute_chunk_windows(0, 300, 15) == []

    def test_long_audio_is_split_with_overlap(self):
        # 20 minutes, 5 minute chunks, 15s overlap.
        duration = 20 * 60
        chunk_length = 5 * 60
        overlap = 15
        windows = compute_chunk_windows(duration, chunk_length, overlap)

        # Every window (except possibly the last) should be chunk_length long.
        for start, end in windows[:-1]:
            assert end - start == pytest.approx(chunk_length)

        # Windows should be contiguous with the configured overlap.
        for (prev_start, prev_end), (next_start, _next_end) in zip(windows, windows[1:]):
            step = next_start - prev_start
            assert step == pytest.approx(chunk_length - overlap)
            # Confirm actual overlap amount between consecutive windows.
            overlap_amount = prev_end - next_start
            assert overlap_amount == pytest.approx(overlap)

        # Final window ends exactly at the total duration.
        assert windows[-1][1] == pytest.approx(duration)
        assert windows[0][0] == 0.0

    def test_rejects_invalid_chunk_length(self):
        with pytest.raises(ValueError):
            compute_chunk_windows(100, 0, 10)

    def test_rejects_overlap_larger_than_chunk(self):
        with pytest.raises(ValueError):
            compute_chunk_windows(100, 30, 30)

    def test_rejects_negative_overlap(self):
        with pytest.raises(ValueError):
            compute_chunk_windows(100, 30, -1)


class TestOffsetSegment:
    def test_offsets_start_and_end(self):
        segment = {"text": "hello", "start_time": 1.0, "end_time": 2.5}
        offset = offset_segment(segment, 100.0)
        assert offset["start_time"] == pytest.approx(101.0)
        assert offset["end_time"] == pytest.approx(102.5)
        assert offset["duration"] == pytest.approx(1.5)

    def test_handles_missing_times(self):
        segment = {"text": "hello"}
        offset = offset_segment(segment, 10.0)
        assert "start_time" not in offset or offset.get("start_time") is None


class TestSegmentsAreDuplicates:
    def test_identical_text_and_close_times_is_duplicate(self):
        a = {"text": "hello world", "start_time": 10.0, "end_time": 11.0}
        b = {"text": "hello world", "start_time": 10.2, "end_time": 11.2}
        assert segments_are_duplicates(a, b) is True

    def test_different_text_is_not_duplicate(self):
        a = {"text": "hello world", "start_time": 10.0, "end_time": 11.0}
        b = {"text": "completely unrelated sentence here", "start_time": 10.0, "end_time": 11.0}
        assert segments_are_duplicates(a, b) is False

    def test_empty_text_is_not_duplicate(self):
        a = {"text": "", "start_time": 10.0, "end_time": 11.0}
        b = {"text": "hello world", "start_time": 10.0, "end_time": 11.0}
        assert segments_are_duplicates(a, b) is False


class TestMergeChunkSegments:
    def test_merges_two_chunks_without_overlap(self):
        chunk_a_segments = [{"text": "hello there", "start_time": 0.0, "end_time": 1.0}]
        chunk_b_segments = [{"text": "goodbye now", "start_time": 0.0, "end_time": 1.0}]

        merged = merge_chunk_segments(
            [(0.0, chunk_a_segments), (100.0, chunk_b_segments)],
            overlap_seconds=15.0,
        )

        assert len(merged) == 2
        assert merged[0]["text"] == "hello there"
        assert merged[0]["start_time"] == pytest.approx(0.0)
        assert merged[1]["text"] == "goodbye now"
        assert merged[1]["start_time"] == pytest.approx(100.0)

    def test_dedupes_overlapping_boundary_segment(self):
        # Chunk 1 covers [0, 300), chunk 2 starts at 285 (15s overlap).
        # The same sentence appears at the tail of chunk 1 and the head of
        # chunk 2 (as it would in real overlapping-window transcription).
        chunk_1_segments = [
            {"text": "this is the shared sentence", "start_time": 290.0, "end_time": 295.0},
        ]
        # Chunk 2 is locally-timed (0 == global 285), and the duplicate
        # segment appears near the start of chunk 2, i.e. globally at 290-295.
        chunk_2_segments = [
            {"text": "this is the shared sentence", "start_time": 5.0, "end_time": 10.0},
            {"text": "new unique content afterwards", "start_time": 20.0, "end_time": 25.0},
        ]

        merged = merge_chunk_segments(
            [(0.0, chunk_1_segments), (285.0, chunk_2_segments)],
            overlap_seconds=15.0,
        )

        texts = [segment["text"] for segment in merged]
        assert texts.count("this is the shared sentence") == 1
        assert "new unique content afterwards" in texts

    def test_merged_output_is_time_ordered(self):
        chunk_a = [{"text": "b segment", "start_time": 5.0, "end_time": 6.0}]
        chunk_b = [{"text": "a segment", "start_time": 100.0, "end_time": 101.0}]

        merged = merge_chunk_segments([(0.0, chunk_a), (90.0, chunk_b)], overlap_seconds=15.0)

        starts = [segment["start_time"] for segment in merged]
        assert starts == sorted(starts)

    def test_empty_chunks_returns_empty(self):
        assert merge_chunk_segments([], overlap_seconds=15.0) == []


class TestFlattenSegments:
    def test_flattens_plain_and_diarised_text(self):
        segments = [
            {"text": "hi", "start_time": 0.0, "end_time": 1.0, "speaker_id": 1},
            {"text": "there", "start_time": 1.0, "end_time": 2.0, "speaker_id": 2},
        ]
        plain, diarised = flatten_segments(segments)
        assert plain == "hi\n\nthere"
        assert "Speaker 1: hi" in diarised
        assert "Speaker 2: there" in diarised

    def test_empty_segments_returns_empty_and_none(self):
        plain, diarised = flatten_segments([])
        assert plain == ""
        assert diarised is None

    def test_skips_blank_text_segments(self):
        segments = [{"text": "  ", "start_time": 0.0, "end_time": 1.0}]
        plain, diarised = flatten_segments(segments)
        assert plain == ""
        assert diarised is None
