"""Tests for the meeting speaker-naming helper (``name_one_speaker.py``).

These cover the Task 04 ``purity-preview`` deliverable:

- Unit: chronological window selection (``select_purity_windows``) for short,
  medium, and long clusters.
- Unit: bracket-only segments do not produce preview windows.
- Integration/e2e: ``purity-preview --no-play`` against a fixture session
  produces all expected clip metadata without mutating profiles/transcripts.

The helper lives outside this repo (in the Pi skills directory) and runs under
the system python3, so tests import it via ``sys.path``. The path is taken from
the ``STT_HELPER_SCRIPTS`` env var when set, otherwise the known default.
"""

from __future__ import annotations

import json
import os
import sys
import wave
from pathlib import Path

import pytest

HELPER_SCRIPTS = Path(
    os.environ.get(
        "STT_HELPER_SCRIPTS",
        "/Users/larry.song/.pi/agent/skills/stt-meeting-recordings/scripts",
    )
)

if str(HELPER_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(HELPER_SCRIPTS))

# Skip the entire module if the helper is not installed (e.g. CI on a machine
# without the Pi skills directory). On the development machine it is present.
pytestmark = pytest.mark.skipif(
    not (HELPER_SCRIPTS / "name_one_speaker.py").exists(),
    reason=f"name_one_speaker.py not found under {HELPER_SCRIPTS}",
)

import name_one_speaker as helper  # noqa: E402


# ---------------------------------------------------------------------------
# Small WAV + transcript fixture builders (mirror test_speaker_id patterns)
# ---------------------------------------------------------------------------


def _write_wav(path: Path, samples, framerate=16000):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(framerate)
        frames = bytearray()
        for sample in samples:
            frames += int(sample).to_bytes(2, byteorder="little", signed=True)
        handle.writeframes(bytes(frames))


def _tone(duration_seconds: float, framerate=16000, amplitude=8000, period=64):
    n = int(duration_seconds * framerate)
    return [amplitude if (i // period) % 2 == 0 else -amplitude for i in range(n)]


def _write_transcript(path: Path, segments):
    path.write_text(
        json.dumps({"segments": segments, "text": "", "diarised_text": ""}),
        encoding="utf-8",
    )


def _seg(speaker, start, end, text, source="system"):
    return {
        "speaker_id": str(speaker),
        "start_time": float(start),
        "end_time": float(end),
        "text": text,
        "source": source,
    }


# ===========================================================================
# Unit: bracket-only / useful-text helpers
# ===========================================================================


class TestIsUsefulText:
    def test_plain_text_is_useful(self):
        assert helper.is_useful_text("hello world") is True

    def test_bracket_only_is_not_useful(self):
        assert helper.is_useful_text("[Silence]") is False

    def test_empty_is_not_useful(self):
        assert helper.is_useful_text("") is False
        assert helper.is_useful_text("   ") is False
        assert helper.is_useful_text(None) is False

    def test_text_with_brackets_but_words_is_useful(self):
        # Only *purely* bracket-only text is excluded.
        assert helper.is_useful_text("[laughs] that was great") is True


# ===========================================================================
# Unit: collect_useful_ranges
# ===========================================================================


class TestCollectUsefulRanges:
    def test_returns_sorted_useful_ranges_for_speaker(self):
        segments = [
            _seg(4, 0.0, 3.0, "a"),
            _seg(1, 3.0, 6.0, "other"),
            _seg(4, 6.0, 9.0, "b"),
        ]
        assert helper.collect_useful_ranges(segments, "4") == [(0.0, 3.0), (6.0, 9.0)]

    def test_excludes_bracket_only_segments(self):
        segments = [
            _seg(4, 0.0, 3.0, "[Silence]"),
            _seg(4, 3.0, 6.0, "hi"),
            _seg(4, 6.0, 9.0, "[Music]"),
        ]
        assert helper.collect_useful_ranges(segments, "4") == [(3.0, 6.0)]

    def test_all_bracket_only_yields_empty(self):
        segments = [
            _seg(4, 0.0, 3.0, "[Silence]"),
            _seg(4, 3.0, 6.0, "[Environmental Sounds]"),
        ]
        assert helper.collect_useful_ranges(segments, "4") == []

    def test_range_intersection_clips_to_boundaries(self):
        segments = [_seg(4, 0.0, 10.0, "hi")]
        result = helper.collect_useful_ranges(segments, "4", ranges=[(3.0, 7.0)])
        assert result == [(3.0, 7.0)]

    def test_range_non_overlapping_yields_empty(self):
        segments = [_seg(4, 0.0, 5.0, "hi")]
        assert helper.collect_useful_ranges(segments, "4", ranges=[(100.0, 200.0)]) == []


# ===========================================================================
# Unit: select_purity_windows (short / medium / long / bracket-only)
# ===========================================================================


class TestSelectPurityWindowsShort:
    """Short cluster: all speech fits in the preview budget -> one window."""

    def test_short_cluster_collapses_to_single_window(self):
        segments = [_seg(4, 0.0, 5.0, "hi")]
        result = helper.select_purity_windows(segments, "4", preview_seconds=12.0)
        labels = [w["label"] for w in result["windows"]]
        assert labels == ["early"]
        assert result["useful_speech_seconds"] == pytest.approx(5.0, abs=0.01)
        assert result["useful_segment_count"] == 1
        # Collapsed duplicates are surfaced as non-fatal warnings.
        assert "middle_window_collapsed_duplicate" in result["warnings"]
        assert "late_window_collapsed_duplicate" in result["warnings"]


class TestSelectPurityWindowsLong:
    """Long cluster: distinct early / middle / late windows."""

    def test_long_cluster_yields_distinct_early_middle_late(self):
        # 40 segments of ~5s each with large gaps -> span >> total speech.
        segments = [_seg(4, i * 100.0, i * 100.0 + 5.0, f"u{i}") for i in range(40)]
        result = helper.select_purity_windows(segments, "4", preview_seconds=12.0)
        labels = [w["label"] for w in result["windows"]]
        assert labels == ["early", "middle", "late"]
        early, middle, late = result["windows"]
        # Each window captures ~budget seconds of speech.
        assert early["speech_seconds"] == pytest.approx(12.0, abs=0.5)
        assert middle["speech_seconds"] == pytest.approx(12.0, abs=0.5)
        assert late["speech_seconds"] == pytest.approx(12.0, abs=0.5)
        # Distinct and chronologically ordered: early ends well before late starts.
        assert early["end"] < middle["start"]
        assert middle["end"] < late["start"]
        # Span reflects the wide timeline.
        assert result["span_seconds"] > 3000.0

    def test_early_and_late_windows_do_not_overlap_for_long_cluster(self):
        segments = [_seg(4, i * 100.0, i * 100.0 + 5.0, f"u{i}") for i in range(40)]
        result = helper.select_purity_windows(segments, "4", preview_seconds=12.0)
        early = next(w for w in result["windows"] if w["label"] == "early")
        late = next(w for w in result["windows"] if w["label"] == "late")
        assert early["end"] < late["start"]


class TestSelectPurityWindowsMedium:
    """Medium cluster: early/late distinct; middle may or may not collapse."""

    def test_medium_cluster_produces_at_least_early_and_late(self):
        # 8 segments of 3s = 24s speech, budget 12s.
        segments = [_seg(4, i * 10.0, i * 10.0 + 3.0, f"u{i}") for i in range(8)]
        result = helper.select_purity_windows(segments, "4", preview_seconds=12.0)
        labels = {w["label"] for w in result["windows"]}
        assert "early" in labels
        assert "late" in labels
        # Early and late should be distinct (early ends before late starts).
        early = next(w for w in result["windows"] if w["label"] == "early")
        late = next(w for w in result["windows"] if w["label"] == "late")
        assert early["start"] < late["start"]


class TestSelectPurityWindowsBracketOnly:
    """Bracket-only segments must not produce any preview windows."""

    def test_all_bracket_only_produces_no_windows(self):
        segments = [
            _seg(4, 0.0, 5.0, "[Silence]"),
            _seg(4, 5.0, 10.0, "[Music]"),
        ]
        result = helper.select_purity_windows(segments, "4", preview_seconds=12.0)
        assert result["windows"] == []
        assert result["useful_segment_count"] == 0
        assert "no_useful_speech" in result["warnings"]

    def test_mixed_bracket_and_speech_ignores_bracket_segments(self):
        segments = [
            _seg(4, 0.0, 3.0, "[Silence]"),
            _seg(4, 3.0, 6.0, "real speech"),
            _seg(4, 6.0, 9.0, "[Environmental Sounds]"),
        ]
        result = helper.select_purity_windows(segments, "4", preview_seconds=12.0)
        # Only the one useful segment counts.
        assert result["useful_segment_count"] == 1
        assert len(result["windows"]) >= 1
        assert result["windows"][0]["label"] == "early"


class TestSelectPurityWindowsRangeRestricted:
    """The optional --range restricts the universe before window selection."""

    def test_range_restriction_limits_useful_speech(self):
        segments = [_seg(4, i * 100.0, i * 100.0 + 5.0, f"u{i}") for i in range(40)]
        full = helper.select_purity_windows(segments, "4", preview_seconds=12.0)
        limited = helper.select_purity_windows(
            segments, "4", preview_seconds=12.0, ranges=[(0.0, 300.0)]
        )
        # 0-300s contains 4 segments (0,100,200,300-ish) -> less speech than full.
        assert limited["useful_segment_count"] < full["useful_segment_count"]
        assert limited["span_seconds"] <= 300.0


# ===========================================================================
# Unit: parse_ranges / _parse_one_range
# ===========================================================================


class TestParseRanges:
    def test_none_returns_none(self):
        assert helper.parse_ranges(None) is None

    def test_empty_returns_none(self):
        assert helper.parse_ranges([]) is None

    def test_seconds_format(self):
        assert helper.parse_ranges(["123.4-180.0"]) == [(123.4, 180.0)]

    def test_mmss_format(self):
        assert helper.parse_ranges(["02:03-03:00"]) == [(123.0, 180.0)]

    def test_hhmmss_format(self):
        assert helper.parse_ranges(["00:41:30-00:57:00"]) == [(2490.0, 3420.0)]

    def test_multiple_ranges(self):
        result = helper.parse_ranges(["0-5", "10-20"])
        assert result == [(0.0, 5.0), (10.0, 20.0)]


# ===========================================================================
# CLI: purity-preview argument parsing
# ===========================================================================


class TestPurityPreviewArgParsing:
    def test_required_args(self):
        parser = helper.build_parser()
        args = parser.parse_args(
            [
                "purity-preview",
                "--session", "/tmp/s",
                "--speaker-id", "4",
            ]
        )
        assert args.session == "/tmp/s"
        assert args.speaker_id == "4"
        assert args.preview_seconds == 12.0
        assert args.no_play is False
        assert args.no_normalize is False
        assert getattr(args, "range") is None

    def test_repeated_range(self):
        parser = helper.build_parser()
        args = parser.parse_args(
            [
                "purity-preview",
                "--session", "/tmp/s",
                "--speaker-id", "4",
                "--range", "0-5",
                "--range", "10-20",
                "--preview-seconds", "8",
                "--no-play",
                "--no-normalize",
            ]
        )
        assert getattr(args, "range") == ["0-5", "10-20"]
        assert args.preview_seconds == 8.0
        assert args.no_play is True
        assert args.no_normalize is True

    def test_func_is_do_purity_preview(self):
        parser = helper.build_parser()
        args = parser.parse_args(
            ["purity-preview", "--session", "/tmp/s", "--speaker-id", "4"]
        )
        assert args.func is helper.do_purity_preview


# ===========================================================================
# Integration / e2e: purity-preview --no-play against a fixture session
# ===========================================================================


class TestPurityPreviewEndToEnd:
    """Run the real ``purity-preview --no-play`` against a fixture session and
    verify all expected clip metadata is produced, without mutating profiles or
    the transcript.
    """

    def _make_session(self, tmp_path: Path, segments) -> Path:
        session = tmp_path / "session"
        session.mkdir()
        # System source WAV long enough to cover all segment ranges.
        last_end = max((s["end_time"] for s in segments), default=30.0)
        _write_wav(session / "system.wav", _tone(last_end + 1.0))
        _write_transcript(session / "transcript.json", segments)
        return session

    def test_long_cluster_produces_early_middle_late_and_best_energy_clips(
        self, tmp_path, monkeypatch
    ):
        # Arrange: a long mixed-style cluster (40 segments spanning ~4000s).
        segments = [_seg(4, i * 100.0, i * 100.0 + 5.0, f"u{i}") for i in range(40)]
        session = self._make_session(tmp_path, segments)

        # Snapshot the transcript so we can prove purity-preview does not mutate it.
        transcript_path = session / "transcript.json"
        transcript_before = transcript_path.read_bytes()

        # Act: invoke the command as an agent would.
        rc = helper.main(
            [
                "purity-preview",
                "--session", str(session),
                "--speaker-id", "4",
                "--preview-seconds", "12",
                "--no-play",
            ]
        )
        assert rc is None or rc == 0  # do_purity_preview returns None

        # Assert: clip artifacts exist under .speaker-clips/.
        clips_dir = session / ".speaker-clips"
        assert clips_dir.is_dir()
        clip_files = list(clips_dir.glob("*.wav"))
        json_files = list(clips_dir.glob("*.json"))
        assert len(clip_files) >= 4  # early + middle + late + best_energy
        assert len(json_files) >= 4

    def test_output_json_is_machine_readable_and_has_expected_shape(
        self, tmp_path, capsys
    ):
        segments = [_seg(4, i * 100.0, i * 100.0 + 5.0, f"u{i}") for i in range(40)]
        session = self._make_session(tmp_path, segments)

        helper.main(
            [
                "purity-preview",
                "--session", str(session),
                "--speaker-id", "4",
                "--preview-seconds", "12",
                "--no-play",
            ]
        )
        captured = capsys.readouterr()
        payload = json.loads(captured.out)

        assert payload["action"] == "purity-preview"
        assert payload["speaker_id"] == "4"
        assert payload["source"] == "system"
        assert payload["played"] is False
        assert payload["preview_seconds"] == 12.0
        assert isinstance(payload["previews"], list)
        labels = [p["label"] for p in payload["previews"]]
        assert "early" in labels
        assert "middle" in labels
        assert "late" in labels
        assert "best_energy" in labels

        # Each chronological preview has a clip path, window metadata, and a
        # non-fatal playback result (skipped under --no-play).
        early = next(p for p in payload["previews"] if p["label"] == "early")
        assert early["command_ok"] is True
        assert early["clip"]
        assert Path(early["clip"]).exists()
        assert early["window"]["start"] < early["window"]["end"]
        assert early["playback"]["skipped"] is True

        # Early and late windows must differ for a long cluster.
        early_w = early["window"]
        late = next(p for p in payload["previews"] if p["label"] == "late")
        late_w = late["window"]
        assert early_w["end"] < late_w["start"]

        # best_energy clip carries its own clip path.
        best = next(p for p in payload["previews"] if p["label"] == "best_energy")
        assert best["command_ok"] is True
        assert Path(best["clip"]).exists()

        # A human/agent-readable guidance note is present.
        assert "note" in payload

    def test_does_not_mutate_transcript(self, tmp_path, capsys):
        segments = [_seg(4, i * 100.0, i * 100.0 + 5.0, f"u{i}") for i in range(40)]
        session = self._make_session(tmp_path, segments)
        transcript_path = session / "transcript.json"
        before = transcript_path.read_bytes()

        helper.main(
            [
                "purity-preview",
                "--session", str(session),
                "--speaker-id", "4",
                "--no-play",
            ]
        )
        capsys.readouterr()  # drain

        assert transcript_path.read_bytes() == before

    def test_short_cluster_still_produces_at_least_one_preview(self, tmp_path, capsys):
        segments = [_seg(4, 0.0, 5.0, "hi")]
        session = self._make_session(tmp_path, segments)

        helper.main(
            [
                "purity-preview",
                "--session", str(session),
                "--speaker-id", "4",
                "--no-play",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        chronological = [p for p in payload["previews"] if p["label"] != "best_energy"]
        assert len(chronological) >= 1
        # Short cluster collapses to a single early window.
        assert chronological[0]["label"] == "early"

    def test_playback_failure_is_nonfatal(self, tmp_path, capsys, monkeypatch):
        """When afplay is missing/fails, the command must still succeed and
        include clip paths + a warning in the output."""
        segments = [_seg(4, i * 100.0, i * 100.0 + 5.0, f"u{i}") for i in range(4)]
        session = self._make_session(tmp_path, segments)

        # Force afplay to appear missing so play_clip reports a nonfatal warning.
        original_exists = Path.exists

        def _fake_exists(self):
            if str(self) == "/usr/bin/afplay":
                return False
            return original_exists(self)

        monkeypatch.setattr(Path, "exists", _fake_exists)

        # We still pass --no-play so playback is skipped; but exercise the
        # play path too by NOT passing --no-play and confirming nonfatality.
        helper.main(
            [
                "purity-preview",
                "--session", str(session),
                "--speaker-id", "4",
                "--preview-seconds", "8",
                # no --no-play: attempts playback, which fails gracefully
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        # Command still completed and produced previews with clip paths.
        assert len(payload["previews"]) >= 1
        for preview in payload["previews"]:
            if preview.get("command_ok"):
                assert preview["clip"]
            # playback result is always present (never crashes the command)
            assert "playback" in preview

    def test_missing_speaker_errors_cleanly(self, tmp_path):
        segments = [_seg(4, 0.0, 5.0, "hi")]
        session = self._make_session(tmp_path, segments)
        with pytest.raises(SystemExit):
            helper.main(
                [
                    "purity-preview",
                    "--session", str(session),
                    "--speaker-id", "99",
                    "--no-play",
                ]
            )
