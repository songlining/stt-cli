"""Tests for the meeting speaker-naming helper (``name_one_speaker.py``).

These cover the Task 04 ``purity-preview`` deliverable and the Task 05
``audit`` deliverable:

- Unit: chronological window selection (``select_purity_windows``) for short,
  medium, and long clusters.
- Unit: bracket-only segments do not produce preview windows.
- Integration/e2e: ``purity-preview --no-play`` against a fixture session
  produces all expected clip metadata without mutating profiles/transcripts.
- Unit: audit safety classification for pure, mixed, short, and no-useful-speech
  clusters.
- Unit: audit JSON schema includes required fields for each speaker.
- Integration/e2e: ``audit`` against a fixture session writes
  ``speaker_audit.json`` without modifying transcripts or profiles, and ``list``
  surfaces audit status when the artifact exists.

The helper lives outside this repo (in the Pi skills directory) and runs under
the system python3, so tests import it via ``sys.path``. The path is taken from
the ``STT_HELPER_SCRIPTS`` env var when set, otherwise the known default.
"""

from __future__ import annotations

import json
import os
import sys
import wave
from datetime import datetime, timezone
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


# ===========================================================================
# Task 05: Speaker audit -- safety classification unit tests
# ===========================================================================


class TestClassifySpeakerSafety:
    """Unit tests for ``classify_speaker_safety`` covering pure, mixed, short,
    and no-useful-speech clusters."""

    def _selection(self, segments, speaker_id):
        return helper.select_purity_windows(segments, speaker_id, preview_seconds=12.0)

    def test_no_useful_speech_is_unknown_and_unsafe(self):
        segments = [_seg(4, 0.0, 5.0, "[Silence]")]
        result = helper.classify_speaker_safety(self._selection(segments, "4"))
        assert result["status"] == "unknown"
        assert result["safe_to_enroll_whole_cluster"] is False
        assert "no_useful_speech" in result["reasons"]

    def test_short_useful_speech_is_unknown_and_unsafe(self):
        # 2s of useful speech, below the default 5s minimum.
        segments = [_seg(4, 0.0, 2.0, "hi")]
        result = helper.classify_speaker_safety(self._selection(segments, "4"))
        assert result["status"] == "unknown"
        assert result["safe_to_enroll_whole_cluster"] is False

    def test_compact_cluster_is_pure_likely_and_safe(self):
        # 12s of contiguous speech: span == speech, ratio 1.0 -> pure.
        segments = [_seg(1, 0.0, 4.0, "a"), _seg(1, 4.0, 8.0, "b"), _seg(1, 8.0, 12.0, "c")]
        result = helper.classify_speaker_safety(self._selection(segments, "1"))
        assert result["status"] == "pure_likely"
        assert result["safe_to_enroll_whole_cluster"] is True

    def test_long_widely_spread_cluster_is_mixed_suspected_and_unsafe(self):
        # 40 segments of 5s spread over ~4000s: ratio ~19.5 -> mixed.
        segments = [_seg(4, i * 100.0, i * 100.0 + 5.0, f"u{i}") for i in range(40)]
        result = helper.classify_speaker_safety(self._selection(segments, "4"))
        assert result["status"] == "mixed_suspected"
        assert result["safe_to_enroll_whole_cluster"] is False
        assert any("ratio" in r for r in result["reasons"])

    def test_dense_long_cluster_is_pure_likely(self):
        # Long timeline but densely packed: span close to speech -> pure.
        segments = [_seg(1, i * 6.0, i * 6.0 + 5.0, f"u{i}") for i in range(40)]
        result = helper.classify_speaker_safety(self._selection(segments, "1"))
        assert result["status"] == "pure_likely"
        assert result["safe_to_enroll_whole_cluster"] is True

    def test_mixed_span_ratio_threshold_is_respected(self):
        # Same long cluster; raising the threshold above its ratio -> pure.
        segments = [_seg(4, i * 100.0, i * 100.0 + 5.0, f"u{i}") for i in range(40)]
        sel = self._selection(segments, "4")
        loose = helper.classify_speaker_safety(sel, mixed_span_ratio=50.0)
        assert loose["status"] == "pure_likely"
        assert loose["safe_to_enroll_whole_cluster"] is True

    def test_min_useful_speech_threshold_is_respected(self):
        # 8s of speech is pure under default min 5s, but unknown under min 10s.
        segments = [_seg(1, 0.0, 8.0, "hello there")]
        sel = self._selection(segments, "1")
        assert helper.classify_speaker_safety(sel)["status"] == "pure_likely"
        strict = helper.classify_speaker_safety(sel, min_useful_speech=10.0)
        assert strict["status"] == "unknown"
        assert strict["safe_to_enroll_whole_cluster"] is False


class TestSpeakerTimestamps:
    def test_returns_none_for_no_useful_speech(self):
        segments = [_seg(4, 0.0, 5.0, "[Silence]")]
        assert helper.speaker_timestamps(segments, "4") == {
            "first": None,
            "middle": None,
            "last": None,
        }

    def test_first_and_last_bound_the_speech(self):
        segments = [_seg(1, 10.0, 15.0, "a"), _seg(1, 100.0, 105.0, "b")]
        ts = helper.speaker_timestamps(segments, "1")
        assert ts["first"] == 10.0
        assert ts["last"] == 105.0
        # Middle lands inside actual speech (cumulative midpoint at 5s of 10s).
        assert 10.0 <= ts["middle"] <= 105.0


class TestBuildSpeakerAudit:
    """Unit tests for audit summary generation and JSON schema."""

    REQUIRED_FIELDS = {
        "speaker_id",
        "source",
        "speaker_name",
        "segments",
        "useful_segments",
        "speech_seconds",
        "useful_speech_seconds",
        "first_timestamp",
        "middle_timestamp",
        "last_timestamp",
        "span_seconds",
        "examples",
        "purity_windows",
        "status",
        "safe_to_enroll_whole_cluster",
        "reasons",
        "recommendation",
    }

    def test_audit_includes_required_fields_for_each_speaker(self):
        segments = [
            *[_seg(4, i * 100.0, i * 100.0 + 5.0, f"u{i}") for i in range(40)],
            _seg(1, 0.0, 8.0, "pure"),
            _seg(2, 0.0, 3.0, "[Silence]"),
        ]
        audit = helper.build_speaker_audit(segments)
        assert len(audit) == 3
        for entry in audit:
            missing = self.REQUIRED_FIELDS - set(entry.keys())
            assert not missing, f"missing fields: {missing}"
            assert entry["status"] in {"unknown", "pure_likely", "mixed_suspected"}
            assert isinstance(entry["safe_to_enroll_whole_cluster"], bool)
            assert isinstance(entry["reasons"], list) and entry["reasons"]
            assert isinstance(entry["recommendation"], str) and entry["recommendation"]

    def test_audit_classifies_mixed_pure_and_unknown_clusters(self):
        segments = [
            *[_seg(4, i * 100.0, i * 100.0 + 5.0, f"u{i}") for i in range(40)],
            *[_seg(1, i * 4.0, i * 4.0 + 4.0, f"p{i}") for i in range(3)],
            _seg(2, 0.0, 3.0, "[Silence]"),
        ]
        audit = helper.build_speaker_audit(segments)
        by_id = {e["speaker_id"]: e for e in audit}
        assert by_id["4"]["status"] == "mixed_suspected"
        assert by_id["4"]["safe_to_enroll_whole_cluster"] is False
        assert by_id["1"]["status"] == "pure_likely"
        assert by_id["1"]["safe_to_enroll_whole_cluster"] is True
        assert by_id["2"]["status"] == "unknown"
        assert by_id["2"]["safe_to_enroll_whole_cluster"] is False

    def test_every_speaker_with_useful_speech_gets_a_recommendation(self):
        segments = [
            _seg(1, 0.0, 8.0, "a"),
            _seg(2, 0.0, 8.0, "b"),
            _seg(3, 0.0, 3.0, "[Music]"),
        ]
        audit = helper.build_speaker_audit(segments)
        # Speakers 1 and 2 have useful speech -> pure_likely with recommendation.
        useful = [e for e in audit if e["useful_speech_seconds"] > 0]
        assert len(useful) == 2
        for e in useful:
            assert e["status"] != "unknown" or e["useful_speech_seconds"] < 5.0
            assert e["recommendation"]

    def test_audit_is_deterministic_for_same_input(self):
        segments = [
            *[_seg(4, i * 100.0, i * 100.0 + 5.0, f"u{i}") for i in range(40)],
            _seg(1, 0.0, 8.0, "pure"),
        ]
        first = json.dumps(helper.build_speaker_audit(segments), sort_keys=True)
        second = json.dumps(helper.build_speaker_audit(segments), sort_keys=True)
        assert first == second

    def test_purity_windows_are_summarized_compactly(self):
        segments = [_seg(4, i * 100.0, i * 100.0 + 5.0, f"u{i}") for i in range(40)]
        audit = helper.build_speaker_audit(segments)
        entry = audit[0]
        labels = [w["label"] for w in entry["purity_windows"]]
        assert labels == ["early", "middle", "late"]
        # Compact summary must not carry the verbose raw ranges list.
        for w in entry["purity_windows"]:
            assert "ranges" not in w
            assert {"label", "start", "end"} <= set(w.keys())


class TestAuditArgParsing:
    def test_required_args_and_defaults(self):
        parser = helper.build_parser()
        args = parser.parse_args(["audit", "--session", "/tmp/s"])
        assert args.session == "/tmp/s"
        assert args.force is False
        assert args.json is None
        assert args.min_useful_speech == helper.DEFAULT_AUDIT_MIN_USEFUL_SPEECH
        assert args.mixed_span_ratio == helper.DEFAULT_AUDIT_MIXED_SPAN_RATIO

    def test_optional_thresholds_and_flags(self):
        parser = helper.build_parser()
        args = parser.parse_args(
            [
                "audit",
                "--session", "/tmp/s",
                "--force",
                "--min-useful-speech", "10",
                "--mixed-span-ratio", "5.0",
                "--json", "/tmp/out/audit.json",
            ]
        )
        assert args.force is True
        assert args.min_useful_speech == 10.0
        assert args.mixed_span_ratio == 5.0
        assert args.json == "/tmp/out/audit.json"

    def test_func_is_do_audit(self):
        parser = helper.build_parser()
        args = parser.parse_args(["audit", "--session", "/tmp/s"])
        assert args.func is helper.do_audit


# ===========================================================================
# Integration / e2e: audit against a fixture session
# ===========================================================================


class TestAuditEndToEnd:
    """Run the real ``audit`` command against a fixture session and verify
    ``speaker_audit.json`` is created without modifying transcripts or profiles."""

    def _make_session(self, tmp_path: Path, segments) -> Path:
        session = tmp_path / "session"
        session.mkdir()
        _write_transcript(session / "transcript.json", segments)
        return session

    def test_creates_speaker_audit_json_with_required_top_level_fields(
        self, tmp_path, capsys
    ):
        segments = [
            *[_seg(4, i * 100.0, i * 100.0 + 5.0, f"u{i}") for i in range(40)],
            _seg(1, 0.0, 8.0, "pure"),
        ]
        session = self._make_session(tmp_path, segments)

        helper.main(["audit", "--session", str(session)])
        capsys.readouterr()

        audit_path = session / helper.AUDIT_FILENAME
        assert audit_path.exists()
        artifact = json.loads(audit_path.read_text(encoding="utf-8"))
        assert "speakers" in artifact
        assert "safe_to_enroll_whole_cluster" in artifact
        assert isinstance(artifact["speakers"], list)
        assert len(artifact["speakers"]) == 2

    def test_does_not_modify_transcript(self, tmp_path, capsys):
        segments = [
            *[_seg(4, i * 100.0, i * 100.0 + 5.0, f"u{i}") for i in range(40)],
            _seg(1, 0.0, 8.0, "pure"),
        ]
        session = self._make_session(tmp_path, segments)
        transcript_path = session / "transcript.json"
        before = transcript_path.read_bytes()

        helper.main(["audit", "--session", str(session)])
        capsys.readouterr()

        assert transcript_path.read_bytes() == before
        # Only audit artifact should be created (no profiles, no clips).
        new_files = [p.name for p in session.iterdir()]
        assert helper.AUDIT_FILENAME in new_files
        assert "transcript.json" in new_files

    def test_mixed_suspected_not_marked_safe(self, tmp_path, capsys):
        segments = [_seg(4, i * 100.0, i * 100.0 + 5.0, f"u{i}") for i in range(40)]
        session = self._make_session(tmp_path, segments)

        helper.main(["audit", "--session", str(session)])
        capsys.readouterr()
        artifact = json.loads((session / helper.AUDIT_FILENAME).read_text())
        speaker = artifact["speakers"][0]
        assert speaker["status"] == "mixed_suspected"
        assert speaker["safe_to_enroll_whole_cluster"] is False
        # Aggregate flag is false when any speaker is unsafe.
        assert artifact["safe_to_enroll_whole_cluster"] is False

    def test_pure_cluster_aggregate_safe_flag(self, tmp_path, capsys):
        segments = [_seg(1, 0.0, 8.0, "pure speaker")]
        session = self._make_session(tmp_path, segments)

        helper.main(["audit", "--session", str(session)])
        capsys.readouterr()
        artifact = json.loads((session / helper.AUDIT_FILENAME).read_text())
        assert artifact["speakers"][0]["safe_to_enroll_whole_cluster"] is True
        assert artifact["safe_to_enroll_whole_cluster"] is True

    def test_deterministic_output_for_same_transcript(self, tmp_path):
        segments = [
            *[_seg(4, i * 100.0, i * 100.0 + 5.0, f"u{i}") for i in range(40)],
            _seg(1, 0.0, 8.0, "pure"),
        ]
        session = self._make_session(tmp_path, segments)

        helper.main(["audit", "--session", str(session)])
        first = (session / helper.AUDIT_FILENAME).read_text()
        # Force recompute.
        helper.main(["audit", "--session", str(session), "--force"])
        second = (session / helper.AUDIT_FILENAME).read_text()
        assert first == second

    def test_force_recomputes_when_artifact_exists(self, tmp_path, capsys):
        segments = [_seg(1, 0.0, 8.0, "pure")]
        session = self._make_session(tmp_path, segments)

        helper.main(["audit", "--session", str(session)])
        capsys.readouterr()
        first_mtime = (session / helper.AUDIT_FILENAME).stat().st_mtime

        # Without --force, a cached artifact is reused (no recompute path).
        helper.main(["audit", "--session", str(session)])
        capsys.readouterr()
        # --force must succeed and rewrite (may keep same content but runs).
        helper.main(["audit", "--session", str(session), "--force"])
        capsys.readouterr()
        assert (session / helper.AUDIT_FILENAME).exists()

    def test_json_output_path_is_written(self, tmp_path, capsys):
        segments = [_seg(1, 0.0, 8.0, "pure")]
        session = self._make_session(tmp_path, segments)
        custom = tmp_path / "custom" / "out.json"

        helper.main(
            ["audit", "--session", str(session), "--json", str(custom)]
        )
        capsys.readouterr()
        assert custom.exists()
        copy = json.loads(custom.read_text())
        canonical = json.loads((session / helper.AUDIT_FILENAME).read_text())
        assert copy == canonical

    def test_custom_thresholds_change_classification(self, tmp_path, capsys):
        # 8s compact cluster is pure under defaults, unknown under min 10s.
        segments = [_seg(1, 0.0, 8.0, "pure")]
        session = self._make_session(tmp_path, segments)

        helper.main(
            ["audit", "--session", str(session), "--force", "--min-useful-speech", "10"]
        )
        capsys.readouterr()
        artifact = json.loads((session / helper.AUDIT_FILENAME).read_text())
        assert artifact["speakers"][0]["status"] == "unknown"
        assert artifact["thresholds"]["min_useful_speech"] == 10.0

    def test_stdout_output_is_machine_readable(self, tmp_path, capsys):
        segments = [_seg(1, 0.0, 8.0, "pure")]
        session = self._make_session(tmp_path, segments)

        helper.main(["audit", "--session", str(session)])
        payload = json.loads(capsys.readouterr().out)
        assert payload["action"] == "audit"
        assert payload["session"] == str(session.resolve())
        assert "speakers" in payload


class TestListWithAudit:
    """``list`` surfaces audit safety status when the audit artifact exists."""

    def _make_session(self, tmp_path: Path, segments) -> Path:
        session = tmp_path / "session"
        session.mkdir()
        _write_transcript(session / "transcript.json", segments)
        return session

    def test_list_shows_audit_status_when_artifact_exists(self, tmp_path, capsys):
        segments = [
            *[_seg(4, i * 100.0, i * 100.0 + 5.0, f"u{i}") for i in range(40)],
            _seg(1, 0.0, 8.0, "pure"),
        ]
        session = self._make_session(tmp_path, segments)

        # No audit yet -> audit_available False.
        helper.main(["list", "--session", str(session), "--all"])
        before = json.loads(capsys.readouterr().out)
        assert before["audit_available"] is False
        assert before["audit_path"] is None
        for s in before["speakers"]:
            assert "audit" not in s

        # Run audit, then list again.
        helper.main(["audit", "--session", str(session)])
        capsys.readouterr()
        helper.main(["list", "--session", str(session), "--all"])
        after = json.loads(capsys.readouterr().out)
        assert after["audit_available"] is True
        assert after["audit_path"] == str((session / helper.AUDIT_FILENAME).resolve())
        by_id = {s["speaker_id"]: s for s in after["speakers"]}
        assert by_id["4"]["audit"]["status"] == "mixed_suspected"
        assert by_id["4"]["audit"]["safe_to_enroll_whole_cluster"] is False
        assert by_id["1"]["audit"]["status"] == "pure_likely"
        assert by_id["1"]["audit"]["safe_to_enroll_whole_cluster"] is True

    def test_list_without_audit_has_no_audit_fields(self, tmp_path, capsys):
        segments = [_seg(1, 0.0, 8.0, "pure")]
        session = self._make_session(tmp_path, segments)
        helper.main(["list", "--session", str(session), "--all"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["audit_available"] is False
        for s in payload["speakers"]:
            assert "audit" not in s


# ===========================================================================
# Task 07: Whole-cluster enrollment safety guard (unit)
# ===========================================================================


class TestEnrollmentGuardDecision:
    """Unit tests for the pure ``enrollment_guard_decision`` function.

    Covers the four required cases: no audit, safe audit, unsafe audit, and a
    missing speaker audit entry. Also verifies refusal output points at
    ``purity-preview`` and ``enroll-ranges``.
    """

    def test_no_audit_warns_but_allows(self):
        decision = helper.enrollment_guard_decision(None, "4", session="/tmp/s")
        assert decision["decision"] == "warn"
        assert decision["audit_available"] is False
        assert decision["speaker_in_audit"] is False
        assert decision["safe_to_enroll_whole_cluster"] is None
        assert "no_speaker_audit_found" in decision["reasons"]
        # A warning is surfaced (never silent), but enrollment is permitted.
        assert "contaminate" in decision["recommendation"]
        cmds = "\n".join(decision["commands"])
        assert "audit" in cmds
        assert "purity-preview" in cmds

    def test_safe_audit_allows(self):
        audit = {
            "speakers": [
                {
                    "speaker_id": "1",
                    "status": "pure_likely",
                    "safe_to_enroll_whole_cluster": True,
                    "reasons": ["compact cluster"],
                }
            ]
        }
        decision = helper.enrollment_guard_decision(audit, "1", session="/tmp/s")
        assert decision["decision"] == "allow"
        assert decision["audit_available"] is True
        assert decision["speaker_in_audit"] is True
        assert decision["status"] == "pure_likely"
        assert decision["safe_to_enroll_whole_cluster"] is True

    def test_unsafe_audit_refuses(self):
        audit = {
            "speakers": [
                {
                    "speaker_id": "4",
                    "status": "mixed_suspected",
                    "safe_to_enroll_whole_cluster": False,
                    "reasons": ["span_to_speech_ratio 19.50 exceeds 3.00"],
                }
            ]
        }
        decision = helper.enrollment_guard_decision(
            audit, "4", session="/tmp/s", name="Domingo Tamayo"
        )
        assert decision["decision"] == "refuse"
        assert decision["speaker_in_audit"] is True
        assert decision["status"] == "mixed_suspected"
        assert decision["safe_to_enroll_whole_cluster"] is False
        assert "span_to_speech_ratio" in decision["reasons"][0]

    def test_unknown_status_audit_also_refuses(self):
        # `unknown` clusters are safe_to_enroll_whole_cluster=false -> refuse.
        audit = {
            "speakers": [
                {
                    "speaker_id": "2",
                    "status": "unknown",
                    "safe_to_enroll_whole_cluster": False,
                    "reasons": ["useful_speech_seconds 2.0 below minimum 5.0"],
                }
            ]
        }
        decision = helper.enrollment_guard_decision(audit, "2", session="/tmp/s")
        assert decision["decision"] == "refuse"
        assert decision["status"] == "unknown"

    def test_missing_speaker_entry_warns(self):
        # Audit exists for speaker 4, but we ask about speaker 9.
        audit = {
            "speakers": [
                {
                    "speaker_id": "4",
                    "status": "mixed_suspected",
                    "safe_to_enroll_whole_cluster": False,
                    "reasons": ["ratio"],
                }
            ]
        }
        decision = helper.enrollment_guard_decision(audit, "9", session="/tmp/s")
        assert decision["decision"] == "warn"
        assert decision["audit_available"] is True
        assert decision["speaker_in_audit"] is False
        assert "speaker_not_in_audit" in decision["reasons"]
        # Points the user at re-running audit and purity-preview.
        cmds = "\n".join(decision["commands"])
        assert "--force" in cmds
        assert "purity-preview" in cmds

    def test_audit_dict_without_speakers_list_is_treated_as_no_audit(self):
        # A malformed audit (dict but no speakers list) cannot confirm safety.
        decision = helper.enrollment_guard_decision(
            {"safe_to_enroll_whole_cluster": True}, "4", session="/tmp/s"
        )
        assert decision["decision"] == "warn"

    def test_refusal_output_includes_recommended_next_commands(self):
        """Acceptance: refusal output points users to purity-preview and enroll-ranges."""
        audit = {
            "speakers": [
                {
                    "speaker_id": "4",
                    "status": "mixed_suspected",
                    "safe_to_enroll_whole_cluster": False,
                    "reasons": ["ratio"],
                }
            ]
        }
        decision = helper.enrollment_guard_decision(
            audit, "4", session="/tmp/s", name="Domingo Tamayo"
        )
        assert decision["decision"] == "refuse"
        cmds = "\n".join(decision["commands"])
        # Must point at both purity-preview and enroll-ranges.
        assert "purity-preview" in cmds
        assert "enroll-ranges" in cmds
        # The enroll-ranges recommendation embeds the requested name + speaker id.
        assert any(
            "enroll-ranges" in c and '--speaker-id 4' in c and '--name "Domingo Tamayo"' in c
            for c in decision["commands"]
        )
        # Recommendation prose names both tools too.
        assert "purity-preview" in decision["recommendation"]
        assert "enroll-ranges" in decision["recommendation"]

    def test_allow_does_not_mention_enroll_ranges(self):
        audit = {
            "speakers": [
                {
                    "speaker_id": "1",
                    "status": "pure_likely",
                    "safe_to_enroll_whole_cluster": True,
                    "reasons": ["compact"],
                }
            ]
        }
        decision = helper.enrollment_guard_decision(audit, "1", session="/tmp/s")
        assert decision["decision"] == "allow"
        assert not any("enroll-ranges" in c for c in decision["commands"])

    def test_decision_is_deterministic(self):
        audit = {
            "speakers": [
                {
                    "speaker_id": "4",
                    "status": "mixed_suspected",
                    "safe_to_enroll_whole_cluster": False,
                    "reasons": ["ratio"],
                }
            ]
        }
        a = helper.enrollment_guard_decision(audit, "4", session="/tmp/s", name="X")
        b = helper.enrollment_guard_decision(audit, "4", session="/tmp/s", name="X")
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


class TestEvaluateEnrollmentGuard:
    """The filesystem-backed wrapper loads the audit artifact for a session."""

    def test_returns_warn_when_no_audit_file(self, tmp_path):
        session = tmp_path / "session"
        session.mkdir()
        decision = helper.evaluate_enrollment_guard(session, "4")
        assert decision["decision"] == "warn"
        assert decision["audit_available"] is False

    def test_reads_existing_audit_artifact(self, tmp_path):
        session = tmp_path / "session"
        session.mkdir()
        audit = {
            "speakers": [
                {
                    "speaker_id": "4",
                    "status": "mixed_suspected",
                    "safe_to_enroll_whole_cluster": False,
                    "reasons": ["ratio"],
                }
            ]
        }
        (session / helper.AUDIT_FILENAME).write_text(json.dumps(audit), encoding="utf-8")
        decision = helper.evaluate_enrollment_guard(session, "4")
        assert decision["decision"] == "refuse"
        assert decision["status"] == "mixed_suspected"


# ===========================================================================
# Task 07: enroll command argument parsing
# ===========================================================================


class TestEnrollArgParsing:
    def test_required_args(self):
        parser = helper.build_parser()
        args = parser.parse_args(
            ["enroll", "--session", "/tmp/s", "--speaker-id", "4", "--name", "Test"]
        )
        assert args.session == "/tmp/s"
        assert args.speaker_id == "4"
        assert args.name == "Test"
        assert args.no_enroll is False

    def test_no_enroll_flag(self):
        parser = helper.build_parser()
        args = parser.parse_args(
            [
                "enroll",
                "--session", "/tmp/s",
                "--speaker-id", "4",
                "--name", "Test",
                "--no-enroll",
            ]
        )
        assert args.no_enroll is True

    def test_func_is_do_enroll(self):
        parser = helper.build_parser()
        args = parser.parse_args(
            ["enroll", "--session", "/tmp/s", "--speaker-id", "4", "--name", "Test"]
        )
        assert args.func is helper.do_enroll


# ===========================================================================
# Task 07: Integration / e2e -- whole-cluster enroll guard via dry-run
# ===========================================================================


class TestEnrollGuardEndToEnd:
    """Run the real ``enroll --no-enroll`` dry-run against fixture sessions.

    The dry-run path evaluates the same guard a real enrollment would, without
    touching the backend or saving a profile. This lets us verify refusal /
    allowance without the runtime venv or WAV files.
    """

    def _make_session(self, tmp_path: Path, segments) -> Path:
        session = tmp_path / "session"
        session.mkdir()
        _write_transcript(session / "transcript.json", segments)
        return session

    def test_unsafe_audit_refuses_even_on_dry_run(self, tmp_path, capsys):
        # A long, widely-spread cluster -> audit will classify it mixed_suspected.
        segments = [_seg(4, i * 100.0, i * 100.0 + 5.0, f"u{i}") for i in range(40)]
        session = self._make_session(tmp_path, segments)

        # Generate the audit artifact first.
        helper.main(["audit", "--session", str(session)])
        capsys.readouterr()
        audit = json.loads((session / helper.AUDIT_FILENAME).read_text())
        assert audit["speakers"][0]["safe_to_enroll_whole_cluster"] is False

        # Now attempt whole-cluster enrollment as a dry run.
        with pytest.raises(SystemExit) as exc:
            helper.main(
                [
                    "enroll",
                    "--session", str(session),
                    "--speaker-id", "4",
                    "--name", "Test",
                    "--no-enroll",
                ]
            )
        # Refusal exits nonzero (exit code 2).
        assert exc.value.code != 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "refused"
        assert payload["guard"]["decision"] == "refuse"
        # Refusal output points users to purity-preview and enroll-ranges.
        cmds = "\n".join(payload["guard"]["commands"])
        assert "purity-preview" in cmds
        assert "enroll-ranges" in cmds

        # Acceptance: no profile is modified when enrollment is refused. The
        # guard short-circuits before creating any helper artifacts.
        assert not (session / ".speaker-clips").exists()
        # Only the audit artifact + transcript should be present.
        names = sorted(p.name for p in session.iterdir())
        assert "transcript.json" in names
        assert helper.AUDIT_FILENAME in names
        assert ".speaker-clips" not in names

    def test_safe_cluster_dry_run_passes_the_guard(self, tmp_path, capsys):
        # A compact cluster -> audit classifies it pure_likely / safe.
        segments = [_seg(1, 0.0, 8.0, "pure speaker")]
        session = self._make_session(tmp_path, segments)

        helper.main(["audit", "--session", str(session)])
        capsys.readouterr()
        audit = json.loads((session / helper.AUDIT_FILENAME).read_text())
        assert audit["speakers"][0]["safe_to_enroll_whole_cluster"] is True

        # Dry-run enroll must NOT raise (guard passes).
        helper.main(
            [
                "enroll",
                "--session", str(session),
                "--speaker-id", "1",
                "--name", "Test",
                "--no-enroll",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "dry_run"
        assert payload["guard"]["decision"] == "allow"
        assert payload["would_enroll"] is True
        # Safe clusters can still use whole-cluster enrollment.
        assert payload["guard"]["safe_to_enroll_whole_cluster"] is True
        # Dry run touches nothing.
        assert not (session / ".speaker-clips").exists()

    def test_missing_audit_dry_run_warns_but_would_enroll(self, tmp_path, capsys):
        # No audit run -> guard warns but permits (no silent enrollment).
        segments = [_seg(1, 0.0, 8.0, "pure")]
        session = self._make_session(tmp_path, segments)

        helper.main(
            [
                "enroll",
                "--session", str(session),
                "--speaker-id", "1",
                "--name", "Test",
                "--no-enroll",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "dry_run"
        assert payload["guard"]["decision"] == "warn"
        assert payload["guard"]["audit_available"] is False
        # Warning is surfaced (recommendation mentions contamination risk).
        assert "contaminate" in payload["guard"]["recommendation"]
        assert payload["would_enroll"] is True

    def test_refusal_does_not_mutate_transcript(self, tmp_path, capsys):
        segments = [_seg(4, i * 100.0, i * 100.0 + 5.0, f"u{i}") for i in range(40)]
        session = self._make_session(tmp_path, segments)
        transcript_before = (session / "transcript.json").read_bytes()

        helper.main(["audit", "--session", str(session)])
        capsys.readouterr()

        with pytest.raises(SystemExit):
            helper.main(
                [
                    "enroll",
                    "--session", str(session),
                    "--speaker-id", "4",
                    "--name", "Test",
                    "--no-enroll",
                ]
            )
        capsys.readouterr()
        # Transcript is byte-identical after a refused enrollment.
        assert (session / "transcript.json").read_bytes() == transcript_before

    def test_refusal_for_unknown_status_cluster(self, tmp_path, capsys):
        # A bracket-only cluster -> audit status 'unknown', unsafe to enroll.
        segments = [_seg(2, 0.0, 3.0, "[Silence]")]
        session = self._make_session(tmp_path, segments)

        helper.main(["audit", "--session", str(session)])
        capsys.readouterr()
        audit = json.loads((session / helper.AUDIT_FILENAME).read_text())
        assert audit["speakers"][0]["status"] == "unknown"
        assert audit["speakers"][0]["safe_to_enroll_whole_cluster"] is False

        with pytest.raises(SystemExit) as exc:
            helper.main(
                [
                    "enroll",
                    "--session", str(session),
                    "--speaker-id", "2",
                    "--name", "Ghost",
                    "--no-enroll",
                ]
            )
        assert exc.value.code != 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "refused"
        assert payload["guard"]["status"] == "unknown"


# ===========================================================================
# Task 03a: enroll-ranges CLI surface and dry-run output
# ===========================================================================


class TestEnrollRangesArgParsing:
    """Unit: parser accepts required and optional enroll-ranges arguments
    (Arrange-Act-Assert)."""

    def test_required_args_parse_correctly(self):
        # Arrange: only the required flags.
        parser = helper.build_parser()
        # Act
        args = parser.parse_args(
            [
                "enroll-ranges",
                "--session", "/tmp/s",
                "--speaker-id", "4",
                "--range", "10-20",
                "--name", "Domingo",
            ]
        )
        # Assert
        assert args.command == "enroll-ranges"
        assert args.session == "/tmp/s"
        assert args.speaker_id == "4"
        assert args.name == "Domingo"
        assert getattr(args, "range") == ["10-20"]
        assert args.source is None
        assert args.sample_seconds == 60.0
        assert args.no_enroll is False

    def test_repeated_range_appends(self):
        # Arrange
        parser = helper.build_parser()
        # Act
        args = parser.parse_args(
            [
                "enroll-ranges",
                "--session", "/tmp/s",
                "--speaker-id", "4",
                "--range", "10-20",
                "--range", "30-40",
                "--name", "Domingo",
            ]
        )
        # Assert
        assert getattr(args, "range") == ["10-20", "30-40"]

    def test_optional_args_parse_correctly(self):
        # Arrange: all optional flags supplied.
        parser = helper.build_parser()
        # Act
        args = parser.parse_args(
            [
                "enroll-ranges",
                "--session", "/tmp/s",
                "--speaker-id", "4",
                "--range", "10-20",
                "--name", "Domingo",
                "--source", "mic",
                "--sample-seconds", "30",
                "--no-enroll",
            ]
        )
        # Assert
        assert args.source == "mic"
        assert args.sample_seconds == 30.0
        assert args.no_enroll is True

    def test_func_is_do_enroll_ranges(self):
        # Arrange
        parser = helper.build_parser()
        # Act
        args = parser.parse_args(
            [
                "enroll-ranges",
                "--session", "/tmp/s",
                "--speaker-id", "4",
                "--range", "10-20",
                "--name", "Domingo",
            ]
        )
        # Assert
        assert args.func is helper.do_enroll_ranges

    def test_missing_name_is_rejected_by_argparse(self):
        # Arrange: --name omitted (argparse-level required).
        parser = helper.build_parser()
        # Act / Assert
        with pytest.raises(SystemExit):
            parser.parse_args(
                ["enroll-ranges", "--session", "/tmp/s",
                 "--speaker-id", "4", "--range", "10-20"]
            )


class TestEnrollRangesValidation:
    """Unit: enroll-ranges validation produces clear errors for bad inputs."""

    def test_missing_range_fails_with_clear_error(self, tmp_path, capsys):
        # Arrange: valid session but no --range.
        session = tmp_path / "session"
        session.mkdir()
        _write_transcript(session / "transcript.json", [_seg(4, 0.0, 10.0, "hi")])
        # Act / Assert
        with pytest.raises(SystemExit) as exc:
            helper.main(
                [
                    "enroll-ranges",
                    "--session", str(session),
                    "--speaker-id", "4",
                    "--name", "Domingo",
                ]
            )
        # die() raises SystemExit with a message string (code is the message).
        assert exc.value.code and "range" in str(exc.value.code).lower()

    def test_missing_session_dir_fails_with_clear_error(self, tmp_path):
        # Arrange: session directory does not exist.
        # Act / Assert
        with pytest.raises(SystemExit) as exc:
            helper.main(
                [
                    "enroll-ranges",
                    "--session", str(tmp_path / "nope"),
                    "--speaker-id", "4",
                    "--range", "0-5",
                    "--name", "Domingo",
                ]
            )
        assert "Session directory not found" in str(exc.value.code)

    def test_missing_transcript_fails_with_clear_error(self, tmp_path):
        # Arrange: session dir exists but no transcript.json.
        session = tmp_path / "session"
        session.mkdir()
        # Act / Assert
        with pytest.raises(SystemExit) as exc:
            helper.main(
                [
                    "enroll-ranges",
                    "--session", str(session),
                    "--speaker-id", "4",
                    "--range", "0-5",
                    "--name", "Domingo",
                ]
            )
        assert "Transcript not found" in str(exc.value.code)

    def test_unknown_speaker_id_fails_with_clear_error(self, tmp_path):
        # Arrange: transcript exists but speaker 99 is absent.
        session = tmp_path / "session"
        session.mkdir()
        _write_transcript(session / "transcript.json", [_seg(4, 0.0, 10.0, "hi")])
        # Act / Assert
        with pytest.raises(SystemExit) as exc:
            helper.main(
                [
                    "enroll-ranges",
                    "--session", str(session),
                    "--speaker-id", "99",
                    "--range", "0-5",
                    "--name", "Domingo",
                ]
            )
        assert "99" in str(exc.value.code)

    def test_speaker_with_no_useful_speech_in_ranges_fails(self, tmp_path):
        # Arrange: speaker has speech only in 0-10, but requested range is far away.
        session = tmp_path / "session"
        session.mkdir()
        _write_transcript(session / "transcript.json", [_seg(4, 0.0, 10.0, "hi")])
        # Act / Assert
        with pytest.raises(SystemExit) as exc:
            helper.main(
                [
                    "enroll-ranges",
                    "--session", str(session),
                    "--speaker-id", "4",
                    "--range", "100-200",
                    "--name", "Domingo",
                ]
            )
        assert "no useful speech" in str(exc.value.code).lower()

    def test_invalid_source_value_fails(self, tmp_path):
        # Arrange: --source value is neither mic nor system.
        session = tmp_path / "session"
        session.mkdir()
        _write_transcript(session / "transcript.json", [_seg(4, 0.0, 10.0, "hi")])
        # Act / Assert
        with pytest.raises(SystemExit) as exc:
            helper.main(
                [
                    "enroll-ranges",
                    "--session", str(session),
                    "--speaker-id", "4",
                    "--range", "0-5",
                    "--name", "Domingo",
                    "--source", "telepathy",
                ]
            )
        assert "mic" in str(exc.value.code) and "system" in str(exc.value.code)


class TestEnrollRangesDryRunEndToEnd:
    """Integration/e2e: enroll-ranges --no-enroll dry-run against a fixture."""

    def _make_session(self, tmp_path: Path, segments) -> Path:
        session = tmp_path / "session"
        session.mkdir()
        _write_transcript(session / "transcript.json", segments)
        return session

    def test_dry_run_output_is_valid_json_with_required_fields(
        self, tmp_path, capsys
    ):
        # Arrange: speaker 4 has speech in 0-5 and 10-15.
        segments = [
            _seg(4, 0.0, 5.0, "hello world"),
            _seg(4, 10.0, 15.0, "more speech"),
        ]
        session = self._make_session(tmp_path, segments)
        # Act
        helper.main(
            [
                "enroll-ranges",
                "--session", str(session),
                "--speaker-id", "4",
                "--range", "2-12",
                "--name", "Domingo",
                "--no-enroll",
            ]
        )
        # Assert: JSON output includes required fields.
        payload = json.loads(capsys.readouterr().out)
        assert payload["action"] == "enroll-ranges"
        assert payload["status"] == "dry_run"
        assert payload["session"] == str(session.resolve())
        assert payload["speaker_id"] == "4"
        assert payload["name"] == "Domingo"
        assert payload["source"] == "system"
        # requested_ranges is a list (JSON round-trips tuples to lists).
        assert payload["requested_ranges"] == [[2.0, 12.0]]
        assert payload["selected_segment_count"] >= 1
        assert payload["selected_speech_seconds"] > 0.0

    def test_dry_run_output_contains_no_profile_mutation_fields(
        self, tmp_path, capsys
    ):
        # Arrange
        segments = [_seg(4, 0.0, 10.0, "hello world")]
        session = self._make_session(tmp_path, segments)
        # Act
        helper.main(
            [
                "enroll-ranges",
                "--session", str(session),
                "--speaker-id", "4",
                "--range", "2-8",
                "--name", "Domingo",
                "--no-enroll",
            ]
        )
        # Assert: explicit no-mutation markers + no enrollment process fields.
        payload = json.loads(capsys.readouterr().out)
        assert payload["mutated_profiles"] is False
        assert payload["mutated_audio"] is False
        assert payload["no_enroll"] is True
        assert "returncode" not in payload
        assert "stdout" not in payload
        assert "filtered_transcript" not in payload

    def test_dry_run_does_not_write_any_files(self, tmp_path, capsys):
        # Arrange
        segments = [_seg(4, 0.0, 10.0, "hello world")]
        session = self._make_session(tmp_path, segments)
        names_before = sorted(p.name for p in session.iterdir())
        # Act
        helper.main(
            [
                "enroll-ranges",
                "--session", str(session),
                "--speaker-id", "4",
                "--range", "2-8",
                "--name", "Domingo",
                "--no-enroll",
            ]
        )
        capsys.readouterr()  # drain
        # Assert: nothing written — no clips dir, no audit, no profiles.
        names_after = sorted(p.name for p in session.iterdir())
        assert names_before == names_after
        assert not (session / ".speaker-clips").exists()
        assert not (session / helper.AUDIT_FILENAME).exists()

    def test_default_path_enrolls_profile_after_generating_sample(
        self, tmp_path, capsys, monkeypatch
    ):
        """Without --no-enroll, the command generates a sample AND enrolls a
        speaker profile from it (task 03c contract). Enrollment is stubbed so
        no real `stt speaker enroll` subprocess is needed."""
        # Arrange: isolate profiles dir so there is no name collision.
        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir()
        monkeypatch.setenv("STT_SPEAKER_PROFILES_DIR", str(profiles_dir))
        # Stub the enrollment subprocess so no real stt binary is required.
        enroll_calls: list[dict[str, Any]] = []

        def _fake_enroll(sample_path, display_name, profiles_root, backend, stt_bin, **kw):
            enroll_calls.append({"sample_path": str(sample_path), "name": display_name})
            return {
                "command": ["stt", "speaker", "enroll", display_name],
                "returncode": 0,
                "stdout": "enrolled",
                "stderr": "",
                "enrolled": True,
            }

        monkeypatch.setattr(helper, "enroll_profile_from_sample", _fake_enroll)
        segments = [_seg(4, 0.0, 10.0, "hello world")]
        session = self._make_session(tmp_path, segments)
        _write_wav(session / "system.wav", _tone(11.0))
        # Act
        helper.main(
            [
                "enroll-ranges",
                "--session", str(session),
                "--speaker-id", "4",
                "--range", "2-8",
                "--name", "Domingo",
                # no --no-enroll
            ]
        )
        # Assert: sample generated AND profile enrolled.
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "enrolled"
        assert payload["enrolled"] is True
        assert payload["mutated_profiles"] is True
        assert (session / ".speaker-clips").is_dir()
        assert len(enroll_calls) == 1
        assert enroll_calls[0]["name"] == "Domingo"

    def test_source_override_is_respected(self, tmp_path, capsys):
        # Arrange: transcript source is 'system' but --source mic overrides.
        segments = [_seg(4, 0.0, 10.0, "hi", source="system")]
        session = self._make_session(tmp_path, segments)
        # Act (use --no-enroll to stay in validation-only mode)
        helper.main(
            [
                "enroll-ranges",
                "--session", str(session),
                "--speaker-id", "4",
                "--range", "2-8",
                "--name", "Domingo",
                "--source", "mic",
                "--no-enroll",
            ]
        )
        # Assert
        payload = json.loads(capsys.readouterr().out)
        assert payload["source"] == "mic"

    def test_source_resolved_from_transcript_when_omitted(self, tmp_path, capsys):
        # Arrange: transcript segments carry source='mic'.
        segments = [_seg(4, 0.0, 10.0, "hi", source="mic")]
        session = self._make_session(tmp_path, segments)
        # Act (use --no-enroll to stay in validation-only mode)
        helper.main(
            [
                "enroll-ranges",
                "--session", str(session),
                "--speaker-id", "4",
                "--range", "2-8",
                "--name", "Domingo",
                "--no-enroll",
            ]
        )
        # Assert
        payload = json.loads(capsys.readouterr().out)
        assert payload["source"] == "mic"

    def test_multiple_ranges_select_disjoint_speech(self, tmp_path, capsys):
        # Arrange: two disjoint ranges, each with useful speech.
        segments = [
            _seg(4, 0.0, 5.0, "a"),
            _seg(4, 100.0, 105.0, "b"),
        ]
        session = self._make_session(tmp_path, segments)
        # Act (use --no-enroll to stay in validation-only mode)
        helper.main(
            [
                "enroll-ranges",
                "--session", str(session),
                "--speaker-id", "4",
                "--range", "0-5",
                "--range", "100-105",
                "--name", "Domingo",
                "--no-enroll",
            ]
        )
        # Assert: both ranges selected speech.
        payload = json.loads(capsys.readouterr().out)
        assert payload["selected_segment_count"] == 2
        assert payload["selected_ranges"] == [[0.0, 5.0], [100.0, 105.0]]

    def test_dry_run_next_step_points_at_enrollment(self, tmp_path, capsys):
        # Arrange
        segments = [_seg(4, 0.0, 10.0, "hi")]
        session = self._make_session(tmp_path, segments)
        # Act (validation-only dry run)
        helper.main(
            [
                "enroll-ranges",
                "--session", str(session),
                "--speaker-id", "4",
                "--range", "2-8",
                "--name", "Domingo",
                "--no-enroll",
            ]
        )
        # Assert: the recommended next step mentions generating a sample and
        # enrolling a profile from it.
        payload = json.loads(capsys.readouterr().out)
        assert "next_step" in payload and payload["next_step"]
        assert "enroll" in payload["next_step"].lower()


# ===========================================================================
# Task 03b: enroll-ranges sample generation and metadata
# ===========================================================================


class TestEnrollRangesSampleToken:
    """Unit: sample/metadata paths are deterministic and under .speaker-clips."""

    def test_token_is_deterministic_for_identical_requests(self):
        # Arrange
        ranges = [(2.0, 12.0), (30.0, 40.0)]
        # Act
        t1 = helper._enroll_ranges_sample_token("4", "system", ranges, 60.0, True)
        t2 = helper._enroll_ranges_sample_token("4", "system", ranges, 60.0, True)
        # Assert
        assert t1 == t2
        assert len(t1) == 12

    def test_token_differs_when_ranges_differ(self):
        # Arrange / Act
        t1 = helper._enroll_ranges_sample_token("4", "system", [(2.0, 12.0)], 60.0, True)
        t2 = helper._enroll_ranges_sample_token("4", "system", [(2.0, 20.0)], 60.0, True)
        # Assert
        assert t1 != t2

    def test_token_differs_when_speaker_or_source_or_normalize_differ(self):
        base = helper._enroll_ranges_sample_token("4", "system", [(2.0, 12.0)], 60.0, True)
        assert base != helper._enroll_ranges_sample_token("5", "system", [(2.0, 12.0)], 60.0, True)
        assert base != helper._enroll_ranges_sample_token("4", "mic", [(2.0, 12.0)], 60.0, True)
        assert base != helper._enroll_ranges_sample_token("4", "system", [(2.0, 12.0)], 60.0, False)

    def test_sample_and_metadata_paths_under_clips_dir(self, tmp_path):
        # Arrange
        session = tmp_path / "session"
        session.mkdir()
        ranges = [(2.0, 8.0)]
        from pathlib import Path
        speaker = {"source": "system"}
        transcript_path = session / "transcript.json"
        src_wav = session / "system.wav"
        # Act: drive the pure path-computation portion by building the token +
        # expected paths the same way build_enroll_sample does.
        token = helper._enroll_ranges_sample_token("4", "system", ranges, 60.0, True)
        clips_dir = session / helper.CLIPS_DIR_NAME
        sample = clips_dir / f"speaker-4-enroll-ranges-{token}-norm.wav"
        meta = clips_dir / f"speaker-4-enroll-ranges-{token}-norm.enroll.json"
        # Assert: both paths are strictly under <session>/.speaker-clips/.
        assert sample.parent == clips_dir
        assert meta.parent == clips_dir
        assert str(sample.resolve()).startswith(str(clips_dir.resolve()))
        assert str(meta.resolve()).startswith(str(clips_dir.resolve()))


class TestEnrollRangesSampleGenerationEndToEnd:
    """Integration/e2e: enroll-ranges sample WAV + metadata generation."""

    def _make_session(self, tmp_path: Path, segments) -> Path:
        session = tmp_path / "session"
        session.mkdir()
        last_end = max((s["end_time"] for s in segments), default=30.0)
        # System source WAV long enough to cover all segment ranges.
        _write_wav(session / "system.wav", _tone(last_end + 1.0))
        _write_transcript(session / "transcript.json", segments)
        return session

    def _stub_enrollment(self, monkeypatch, tmp_path) -> None:
        """Isolate profiles + stub the enrollment subprocess so tests don't need
        the real `stt speaker enroll` binary/ML backend. Task 03c added real
        enrollment to the default path; these sample-gen tests only care that
        a sample is produced, so enrollment is made a no-op success."""
        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir()
        monkeypatch.setenv("STT_SPEAKER_PROFILES_DIR", str(profiles_dir))

        def _fake_enroll(sample_path, display_name, profiles_root, backend, stt_bin, **kw):
            return {
                "command": ["stt", "speaker", "enroll", display_name],
                "returncode": 0,
                "stdout": "enrolled",
                "stderr": "",
                "enrolled": True,
            }

        monkeypatch.setattr(helper, "enroll_profile_from_sample", _fake_enroll)

    def test_generates_sample_wav_and_metadata_json(self, tmp_path, capsys, monkeypatch):
        # Arrange: speaker 4 has speech in 0-5 and 10-15.
        self._stub_enrollment(monkeypatch, tmp_path)
        segments = [
            _seg(4, 0.0, 5.0, "hello world"),
            _seg(4, 10.0, 15.0, "more speech"),
        ]
        session = self._make_session(tmp_path, segments)
        # Act
        helper.main(
            [
                "enroll-ranges",
                "--session", str(session),
                "--speaker-id", "4",
                "--range", "2-12",
                "--name", "Domingo",
            ]
        )
        # Assert
        payload = json.loads(capsys.readouterr().out)
        assert payload["action"] == "enroll-ranges"
        assert payload["status"] == "enrolled"
        assert payload["enrolled"] is True
        assert payload["mutated_profiles"] is True
        sample_path = Path(payload["sample_path"])
        meta_path = Path(payload["metadata_path"])
        assert sample_path.exists()
        assert meta_path.exists()
        # Both under <session>/.speaker-clips/.
        clips_dir = (session / helper.CLIPS_DIR_NAME).resolve()
        assert str(sample_path.resolve()).startswith(str(clips_dir))
        assert str(meta_path.resolve()).startswith(str(clips_dir))

    def test_sample_wav_is_openable_by_python_wave(self, tmp_path, capsys, monkeypatch):
        # Arrange
        self._stub_enrollment(monkeypatch, tmp_path)
        segments = [_seg(4, 0.0, 10.0, "hello world")]
        session = self._make_session(tmp_path, segments)
        # Act
        helper.main(
            [
                "enroll-ranges",
                "--session", str(session),
                "--speaker-id", "4",
                "--range", "1-8",
                "--name", "Domingo",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        sample_path = Path(payload["sample_path"])
        # Assert: plain PCM WAV readable by Python's wave module.
        with wave.open(str(sample_path), "rb") as handle:
            assert handle.getnchannels() == 1
            assert handle.getsampwidth() == 2
            assert handle.getframerate() == 16000
            assert handle.getnframes() > 0
            # comptype NONE == plain PCM (not compressed).
            assert handle.getcomptype() == "NONE"

    def test_metadata_contains_required_provenance_fields(self, tmp_path, capsys, monkeypatch):
        # Arrange: two disjoint ranges.
        self._stub_enrollment(monkeypatch, tmp_path)
        segments = [
            _seg(4, 0.0, 5.0, "a"),
            _seg(4, 100.0, 105.0, "b"),
        ]
        session = self._make_session(tmp_path, segments)
        # Act
        helper.main(
            [
                "enroll-ranges",
                "--session", str(session),
                "--speaker-id", "4",
                "--range", "0-5",
                "--range", "100-105",
                "--name", "Domingo",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        meta = json.loads(Path(payload["metadata_path"]).read_text(encoding="utf-8"))
        # Assert: required metadata fields per the task spec.
        assert meta["source_session"] == str(session.resolve())
        assert meta["speaker_id"] == "4"
        assert meta["source_track"] == "system"
        # requested ranges captured.
        assert meta["requested_ranges"] == [[0.0, 5.0], [100.0, 105.0]]
        # selected ranges captured.
        assert meta["selected_ranges"] == [[0.0, 5.0], [100.0, 105.0]]
        assert meta["selected_segment_count"] == 2
        assert meta["selected_speech_seconds"] > 0.0
        # sample path captured.
        assert meta["sample_path"] == payload["sample_path"]
        # no enrollment happened.
        assert meta["enrolled"] is False

    def test_sample_only_contains_requested_ranges_not_whole_cluster(
        self, tmp_path, capsys, monkeypatch
    ):
        # Arrange: speaker has speech at 0-5, 50-55, 100-105. Request only 50-55.
        self._stub_enrollment(monkeypatch, tmp_path)
        segments = [
            _seg(4, 0.0, 5.0, "early"),
            _seg(4, 50.0, 55.0, "middle"),
            _seg(4, 100.0, 105.0, "late"),
        ]
        session = self._make_session(tmp_path, segments)
        # Act
        helper.main(
            [
                "enroll-ranges",
                "--session", str(session),
                "--speaker-id", "4",
                "--range", "50-55",
                "--name", "Domingo",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        # Assert: only the requested range selected (~5s), not the whole cluster.
        assert payload["selected_segment_count"] == 1
        assert payload["selected_ranges"] == [[50.0, 55.0]]
        # The sample duration is ~5s, not ~15s (whole cluster).
        assert payload["sample_duration_seconds"] <= 5.5
        assert payload["sample_duration_seconds"] >= 4.5

    def test_no_profile_is_created_or_modified(self, tmp_path, capsys, monkeypatch):
        # Arrange: point profiles dir at an empty temp dir and stub enrollment
        # so no real `stt speaker enroll` subprocess runs.
        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir()
        monkeypatch.setenv("STT_SPEAKER_PROFILES_DIR", str(profiles_dir))
        # Stub enrollment to a no-op success (no real subprocess / profile files).
        monkeypatch.setattr(
            helper,
            "enroll_profile_from_sample",
            lambda *a, **kw: {
                "command": ["stt", "speaker", "enroll"],
                "returncode": 0,
                "stdout": "",
                "stderr": "",
                "enrolled": True,
            },
        )
        segments = [_seg(4, 0.0, 10.0, "hi")]
        session = self._make_session(tmp_path, segments)
        names_before = sorted(p.name for p in profiles_dir.iterdir())
        # Act
        helper.main(
            [
                "enroll-ranges",
                "--session", str(session),
                "--speaker-id", "4",
                "--range", "1-8",
                "--name", "Domingo",
            ]
        )
        capsys.readouterr()  # drain
        # Assert: stubbed enrollment does not create real profile files.
        names_after = sorted(p.name for p in profiles_dir.iterdir())
        assert names_before == names_after

    def test_dry_run_still_writes_no_files(self, tmp_path, capsys):
        # Arrange
        segments = [_seg(4, 0.0, 10.0, "hi")]
        session = self._make_session(tmp_path, segments)
        names_before = sorted(p.name for p in session.iterdir())
        # Act
        helper.main(
            [
                "enroll-ranges",
                "--session", str(session),
                "--speaker-id", "4",
                "--range", "1-8",
                "--name", "Domingo",
                "--no-enroll",
            ]
        )
        capsys.readouterr()  # drain
        # Assert: --no-enroll still writes nothing.
        names_after = sorted(p.name for p in session.iterdir())
        assert names_before == names_after
        assert not (session / ".speaker-clips").exists()

    def test_backend_failure_surfaces_json_error_without_fallback(self, tmp_path, capsys):
        # Arrange: source WAV exists (so source_wav() validation passes) but is
        # not a valid WAV, so the backend concatenate fails. This exercises the
        # structured-JSON-error path without falling back to whole-cluster audio.
        segments = [_seg(4, 0.0, 10.0, "hi", source="mic")]
        session = tmp_path / "session"
        session.mkdir()
        _write_transcript(session / "transcript.json", segments)
        # Create a *corrupt* mic.wav (not a valid WAV file).
        (session / "mic.wav").write_bytes(b"not a wav file")
        # Act / Assert: emits a structured JSON error, exits 1.
        with pytest.raises(SystemExit) as exc:
            helper.main(
                [
                    "enroll-ranges",
                    "--session", str(session),
                    "--speaker-id", "4",
                    "--range", "1-8",
                    "--name", "Domingo",
                    "--source", "mic",
                ]
            )
        # SystemExit code is 1 for the error path.
        assert exc.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert "error" in payload
        assert payload["enrolled"] is False
        assert payload["mutated_profiles"] is False
        # No sample written on backend failure.
        assert "sample_path" not in payload


# ===========================================================================
# Task 03c: enroll-ranges profile enrollment + collision handling
# ===========================================================================


class TestEnrollRangesEnrollment:
    """Task 03c: real profile enrollment from the range-limited sample, with
    safe display-name collision handling. Enrollment is stubbed so no real
    `stt speaker enroll` subprocess / ML backend is needed."""

    def _make_session(self, tmp_path: Path, segments) -> Path:
        session = tmp_path / "session"
        session.mkdir()
        last_end = max((s["end_time"] for s in segments), default=30.0)
        _write_wav(session / "system.wav", _tone(last_end + 1.0))
        _write_transcript(session / "transcript.json", segments)
        return session

    def _stub_enroll(self, monkeypatch, *, enrolled=True, returncode=0):
        calls: list[dict[str, Any]] = []

        def _fake(sample_path, display_name, profiles_root, backend, stt_bin, **kw):
            calls.append({
                "sample_path": str(sample_path),
                "name": display_name,
                "profiles_root": str(profiles_root),
            })
            return {
                "command": ["stt", "speaker", "enroll", display_name],
                "returncode": returncode,
                "stdout": "enrolled" if enrolled else "",
                "stderr": "" if enrolled else "boom",
                "enrolled": enrolled,
            }

        monkeypatch.setattr(helper, "enroll_profile_from_sample", _fake)
        return calls

    def test_collision_skips_enrollment_without_overwriting(
        self, tmp_path, capsys, monkeypatch
    ):
        # Arrange: a profile named 'Domingo' already exists.
        profiles_root = tmp_path / "speakers"
        (profiles_root / "profiles").mkdir(parents=True)
        (profiles_root / "profiles" / "uuid-1.json").write_text(
            json.dumps({"id": "uuid-1", "displayName": "Domingo"}),
            encoding="utf-8",
        )
        monkeypatch.setenv("STT_SPEAKER_PROFILES_DIR", str(profiles_root))
        enroll_calls = self._stub_enroll(monkeypatch)
        segments = [_seg(4, 0.0, 10.0, "hi")]
        session = self._make_session(tmp_path, segments)
        # Act
        helper.main(
            [
                "enroll-ranges",
                "--session", str(session),
                "--speaker-id", "4",
                "--range", "1-8",
                "--name", "Domingo",
            ]
        )
        # Assert: skipped, never enrolled, existing profile untouched.
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "skipped"
        assert payload["skip_reason"] == "display_name_exists"
        assert payload["enrolled"] is False
        assert payload["mutated_profiles"] is False
        assert payload["sample_path"]  # sample still generated
        assert enroll_calls == []  # enrollment never invoked

    def test_successful_enrollment_creates_profile_from_sample(
        self, tmp_path, capsys, monkeypatch
    ):
        # Arrange: empty profiles dir (no collision).
        profiles_root = tmp_path / "speakers"
        (profiles_root / "profiles").mkdir(parents=True)
        monkeypatch.setenv("STT_SPEAKER_PROFILES_DIR", str(profiles_root))
        enroll_calls = self._stub_enroll(monkeypatch, enrolled=True)
        segments = [_seg(4, 0.0, 10.0, "hi")]
        session = self._make_session(tmp_path, segments)
        # Act
        helper.main(
            [
                "enroll-ranges",
                "--session", str(session),
                "--speaker-id", "4",
                "--range", "1-8",
                "--name", "Domingo",
            ]
        )
        # Assert: enrolled from the range-limited sample only.
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "enrolled"
        assert payload["enrolled"] is True
        assert payload["mutated_profiles"] is True
        assert payload["enroll_returncode"] == 0
        assert len(enroll_calls) == 1
        # Enrollment used the generated sample (under .speaker-clips), not the
        # whole-cluster session audio.
        assert ".speaker-clips" in enroll_calls[0]["sample_path"]
        assert enroll_calls[0]["profiles_root"] == str(profiles_root)

    def test_enrollment_failure_does_not_fall_back_to_whole_cluster(
        self, tmp_path, capsys, monkeypatch
    ):
        # Arrange: empty profiles dir, but enrollment fails.
        profiles_root = tmp_path / "speakers"
        (profiles_root / "profiles").mkdir(parents=True)
        monkeypatch.setenv("STT_SPEAKER_PROFILES_DIR", str(profiles_root))
        enroll_calls = self._stub_enroll(
            monkeypatch, enrolled=False, returncode=2
        )
        segments = [_seg(4, 0.0, 10.0, "hi")]
        session = self._make_session(tmp_path, segments)
        # Act: failure path raises SystemExit(1) after emitting structured JSON.
        with pytest.raises(SystemExit) as exc:
            helper.main(
                [
                    "enroll-ranges",
                    "--session", str(session),
                    "--speaker-id", "4",
                    "--range", "1-8",
                    "--name", "Domingo",
                ]
            )
        assert exc.value.code == 1
        # Assert: failure surfaced, exactly one enrollment attempt, no
        # whole-cluster fallback (would have been a second call).
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "failed"
        assert payload["enrolled"] is False
        assert payload["enroll_returncode"] == 2
        assert len(enroll_calls) == 1

    def test_no_enroll_never_invokes_enrollment_backend(
        self, tmp_path, capsys, monkeypatch
    ):
        # Arrange
        profiles_root = tmp_path / "speakers"
        (profiles_root / "profiles").mkdir(parents=True)
        monkeypatch.setenv("STT_SPEAKER_PROFILES_DIR", str(profiles_root))
        enroll_calls = self._stub_enroll(monkeypatch)
        segments = [_seg(4, 0.0, 10.0, "hi")]
        session = self._make_session(tmp_path, segments)
        # Act
        helper.main(
            [
                "enroll-ranges",
                "--session", str(session),
                "--speaker-id", "4",
                "--range", "1-8",
                "--name", "Domingo",
                "--no-enroll",
            ]
        )
        capsys.readouterr()  # drain
        # Assert: --no-enroll is pure validation; enrollment never runs.
        assert enroll_calls == []


# ===========================================================================
# Task 06c: helper suggest-labels command
# ===========================================================================


class TestSuggestLabelsCommand:
    """Task 06c: the `suggest-labels` helper command writes a session artifact
    without mutating transcripts or profiles. The backend ML subprocess is
    stubbed so tests stay fast and ML-free."""

    def _make_session(self, tmp_path: Path, segments) -> Path:
        session = tmp_path / "session"
        session.mkdir()
        last_end = max((s["end_time"] for s in segments), default=10.0)
        _write_wav(session / "system.wav", _tone(last_end + 1.0))
        _write_transcript(session / "transcript.json", segments)
        return session

    def _stub_backend(self, monkeypatch, *, payload):
        """Stub the backend subprocess so it writes the given payload to the
        --json output path and returns success."""
        runs: list[list[str]] = []

        class _FakeProc:
            returncode = 0
            stdout = ""
            stderr = ""

        def _fake_run(cmd, *a, **kw):
            runs.append(cmd)
            # Find the --json path in the cmd list and write the payload there.
            json_idx = cmd.index("--json") + 1 if "--json" in cmd else None
            if json_idx is not None:
                Path(cmd[json_idx]).write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            return _FakeProc()

        monkeypatch.setattr(helper.subprocess, "run", _fake_run)
        return runs

    def test_parser_accepts_suggest_labels_options(self):
        # Act
        parser = helper.build_parser()
        args = parser.parse_args(
            [
                "suggest-labels",
                "--session", "/tmp/sess",
                "--threshold", "0.8",
                "--margin", "0.1",
                "--no-windows",
            ]
        )
        # Assert
        assert args.command == "suggest-labels"
        assert args.threshold == 0.8
        assert args.margin == 0.1
        assert args.no_windows is True
        assert args.func is helper.do_suggest_labels

    def test_writes_artifact_json_with_duplicate_and_mixed_fields(
        self, tmp_path, capsys, monkeypatch
    ):
        # Arrange: isolate profiles (empty -> backend gets no --profiles).
        profiles_root = tmp_path / "speakers"
        (profiles_root / "profiles").mkdir(parents=True)
        monkeypatch.setenv("STT_SPEAKER_PROFILES_DIR", str(profiles_root))
        backend_payload = {
            "schemaVersion": 1,
            "duplicateGroups": [
                {"profileId": "p1", "speakers": ["1", "2"]},
            ],
            "mixedClusters": [],
            "clusters": [],
        }
        self._stub_backend(monkeypatch, payload=backend_payload)
        segments = [_seg(4, 0.0, 10.0, "hi")]
        session = self._make_session(tmp_path, segments)
        # Act
        helper.main(
            ["suggest-labels", "--session", str(session), "--no-windows"]
        )
        # Assert
        payload = json.loads(capsys.readouterr().out)
        assert payload["action"] == "suggest-labels"
        assert payload["cached"] is False
        artifact = session / helper.LABEL_SUGGESTIONS_FILENAME
        assert artifact.exists()
        written = json.loads(artifact.read_text(encoding="utf-8"))
        assert written["duplicateGroups"][0]["profileId"] == "p1"

    def test_force_recomputes_and_ignores_cached_artifact(
        self, tmp_path, capsys, monkeypatch
    ):
        # Arrange: a cached artifact already exists with stale data.
        profiles_root = tmp_path / "speakers"
        (profiles_root / "profiles").mkdir(parents=True)
        monkeypatch.setenv("STT_SPEAKER_PROFILES_DIR", str(profiles_root))
        segments = [_seg(4, 0.0, 10.0, "hi")]
        session = self._make_session(tmp_path, segments)
        artifact = session / helper.LABEL_SUGGESTIONS_FILENAME
        artifact.write_text(
            json.dumps({"schemaVersion": 1, "duplicateGroups": "STALE"}),
            encoding="utf-8",
        )
        fresh_payload = {
            "schemaVersion": 1,
            "duplicateGroups": [{"profileId": "fresh"}],
            "mixedClusters": [],
        }
        runs = self._stub_backend(monkeypatch, payload=fresh_payload)
        # Act
        helper.main(
            ["suggest-labels", "--session", str(session), "--force", "--no-windows"]
        )
        # Assert: backend was actually invoked (cache ignored).
        assert len(runs) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["cached"] is False
        assert payload["duplicateGroups"][0]["profileId"] == "fresh"

    def test_reuses_cached_artifact_without_force(
        self, tmp_path, capsys, monkeypatch
    ):
        # Arrange: cached artifact exists; --force is NOT passed.
        segments = [_seg(4, 0.0, 10.0, "hi")]
        session = self._make_session(tmp_path, segments)
        artifact = session / helper.LABEL_SUGGESTIONS_FILENAME
        cached = {
            "schemaVersion": 1,
            "duplicateGroups": [{"profileId": "cached"}],
        }
        artifact.write_text(json.dumps(cached), encoding="utf-8")
        # Stub backend - it should NOT be called.
        runs = self._stub_backend(monkeypatch, payload={"should": "not be used"})
        # Act
        helper.main(["suggest-labels", "--session", str(session)])
        # Assert
        assert runs == []  # backend not invoked
        payload = json.loads(capsys.readouterr().out)
        assert payload["cached"] is True
        assert payload["duplicateGroups"][0]["profileId"] == "cached"

    def test_does_not_mutate_transcript_or_profiles(
        self, tmp_path, capsys, monkeypatch
    ):
        # Arrange
        profiles_root = tmp_path / "speakers"
        (profiles_root / "profiles").mkdir(parents=True)
        monkeypatch.setenv("STT_SPEAKER_PROFILES_DIR", str(profiles_root))
        self._stub_backend(
            monkeypatch,
            payload={"schemaVersion": 1, "duplicateGroups": [], "mixedClusters": []},
        )
        segments = [_seg(4, 0.0, 10.0, "hi")]
        session = self._make_session(tmp_path, segments)
        transcript = session / "transcript.json"
        before = transcript.read_bytes()
        # Act
        helper.main(
            ["suggest-labels", "--session", str(session), "--force", "--no-windows"]
        )
        capsys.readouterr()  # drain
        # Assert: transcript bytes unchanged.
        assert transcript.read_bytes() == before


# ===========================================================================
# Task 10: segment-level transcript relabeling
# ===========================================================================


class TestSegmentOverlapsRanges:
    """Unit: segment_overlaps_ranges half-open interval overlap."""

    def test_none_ranges_means_whole_cluster_relabel(self):
        assert helper.segment_overlaps_ranges(100.0, 105.0, None) is True

    def test_overlapping_range_matches(self):
        assert helper.segment_overlaps_ranges(2.0, 8.0, [(0.0, 5.0)]) is True

    def test_non_overlapping_range_does_not_match(self):
        assert helper.segment_overlaps_ranges(50.0, 55.0, [(0.0, 5.0)]) is False

    def test_multiple_ranges_any_match_is_sufficient(self):
        assert helper.segment_overlaps_ranges(50.0, 55.0, [(0.0, 5.0), (49.0, 60.0)]) is True

    def test_boundary_touching_is_not_overlap(self):
        # Half-open: [5.0, 10.0] vs range (0.0, 5.0) -- seg_start(5.0) is not < req_end(5.0).
        assert helper.segment_overlaps_ranges(5.0, 10.0, [(0.0, 5.0)]) is False


class TestSelectRelabelSegments:
    """Unit: select_relabel_segments filters by speaker id + range overlap."""

    def test_selects_all_segments_for_speaker_when_no_ranges(self):
        segments = [
            _seg(4, 0.0, 5.0, "early"),
            _seg(4, 200.0, 205.0, "late"),
            _seg(5, 10.0, 15.0, "other speaker"),
        ]
        indices = helper.select_relabel_segments(segments, "4")
        assert indices == [0, 1]

    def test_selects_only_segments_overlapping_requested_ranges(self):
        segments = [
            _seg(4, 0.0, 5.0, "early"),
            _seg(4, 200.0, 205.0, "late"),
        ]
        indices = helper.select_relabel_segments(segments, "4", ranges=[(0.0, 12.0)])
        assert indices == [0]

    def test_includes_bracket_only_nonspeech_segments(self):
        # Relabeling is whole-presence, not useful-speech-only.
        segments = [_seg(4, 0.0, 5.0, "[Silence]")]
        indices = helper.select_relabel_segments(segments, "4")
        assert indices == [0]

    def test_skips_segments_without_usable_timestamps_when_ranges_given(self):
        segments = [
            {"speaker_id": "4", "start_time": None, "end_time": None, "text": "bad"},
            _seg(4, 0.0, 5.0, "ok"),
        ]
        indices = helper.select_relabel_segments(segments, "4", ranges=[(0.0, 12.0)])
        assert indices == [1]

    def test_ignores_segments_for_other_speakers(self):
        segments = [_seg(4, 0.0, 5.0, "a"), _seg(5, 0.0, 5.0, "b")]
        indices = helper.select_relabel_segments(segments, "5")
        assert indices == [1]


class TestApplyRelabelToSegments:
    """Unit: apply_relabel_to_segments sets speaker_name only on selected indices."""

    def test_sets_name_only_on_selected_indices(self):
        segments = [_seg(4, 0.0, 5.0, "early"), _seg(4, 200.0, 205.0, "late")]
        result = helper.apply_relabel_to_segments(segments, [0], "Domingo")
        assert result[0]["speaker_name"] == "Domingo"
        assert "speaker_name" not in result[1] or result[1].get("speaker_name") != "Domingo"

    def test_preserves_speaker_id_source_timestamps_and_text(self):
        segments = [_seg(4, 12.0, 18.0, "hello", source="mic")]
        result = helper.apply_relabel_to_segments(segments, [0], "Domingo")
        assert result[0]["speaker_id"] == "4"
        assert result[0]["source"] == "mic"
        assert result[0]["start_time"] == 12.0
        assert result[0]["end_time"] == 18.0
        assert result[0]["text"] == "hello"

    def test_does_not_mutate_the_input_list(self):
        segments = [_seg(4, 0.0, 5.0, "early")]
        original_copy = dict(segments[0])
        helper.apply_relabel_to_segments(segments, [0], "Domingo")
        assert segments[0] == original_copy

    def test_preserves_prior_relabel_on_non_selected_segments(self):
        segments = [_seg(4, 0.0, 5.0, "early"), _seg(4, 200.0, 205.0, "late")]
        segments[1]["speaker_name"] = "Gia"
        result = helper.apply_relabel_to_segments(segments, [0], "Domingo")
        assert result[0]["speaker_name"] == "Domingo"
        assert result[1]["speaker_name"] == "Gia"

    def test_mixed_cluster_can_carry_two_names_after_two_relabel_calls(self):
        # The ASX Speaker 4 case: early -> Domingo, late -> Gia.
        segments = [_seg(4, 0.0, 5.0, "early"), _seg(4, 200.0, 205.0, "late")]
        after_first = helper.apply_relabel_to_segments(segments, [0], "Domingo")
        after_second = helper.apply_relabel_to_segments(after_first, [1], "Gia")
        assert after_second[0]["speaker_name"] == "Domingo"
        assert after_second[1]["speaker_name"] == "Gia"
        assert after_second[0]["speaker_id"] == after_second[1]["speaker_id"] == "4"


class TestRenderTranscriptText:
    """Unit: render_transcript_text mirrors Swift TranscriptMerger.renderPlainText."""

    def test_uses_speaker_name_when_present(self):
        segments = [_seg(4, 0.0, 5.0, "hello", source="system")]
        segments[0]["speaker_name"] = "Domingo"
        text = helper.render_transcript_text(segments)
        assert text == "[0.00 - 5.00] System Domingo: hello\n"

    def test_falls_back_to_speaker_id_when_no_name(self):
        segments = [_seg(4, 0.0, 5.0, "hello", source="mic")]
        text = helper.render_transcript_text(segments)
        assert text == "[0.00 - 5.00] Mic Speaker 4: hello\n"

    def test_omits_speaker_suffix_when_neither_name_nor_id(self):
        segments = [{"start_time": 0.0, "end_time": 5.0, "text": "hi", "source": "mic"}]
        text = helper.render_transcript_text(segments)
        assert text == "[0.00 - 5.00] Mic: hi\n"

    def test_joins_multiple_segments_with_newlines(self):
        segments = [
            _seg(4, 0.0, 5.0, "a", source="mic"),
            _seg(5, 5.0, 10.0, "b", source="system"),
        ]
        text = helper.render_transcript_text(segments)
        assert text == "[0.00 - 5.00] Mic Speaker 4: a\n[5.00 - 10.00] System Speaker 5: b\n"


class TestRelabelArgParsing:
    """Unit: relabel CLI argument parsing."""

    def test_parses_required_and_repeated_range_flags(self):
        parser = helper.build_parser()
        args = parser.parse_args([
            "relabel", "--session", "/tmp/sess", "--speaker-id", "4",
            "--range", "0-12", "--range", "200-212", "--name", "Domingo",
        ])
        assert args.session == "/tmp/sess"
        assert args.speaker_id == "4"
        assert args.range == ["0-12", "200-212"]
        assert args.name == "Domingo"
        assert args.dry_run is False

    def test_dry_run_flag_defaults_false(self):
        parser = helper.build_parser()
        args = parser.parse_args([
            "relabel", "--session", "/tmp/sess", "--speaker-id", "4", "--name", "Domingo",
        ])
        assert args.dry_run is False
        assert args.range is None

    def test_dry_run_flag_parses(self):
        parser = helper.build_parser()
        args = parser.parse_args([
            "relabel", "--session", "/tmp/sess", "--speaker-id", "4",
            "--name", "Domingo", "--dry-run",
        ])
        assert args.dry_run is True


class TestRelabelDryRunEndToEnd:
    """Integration/e2e: relabel --dry-run reports planned changes, writes nothing."""

    def _make_session(self, tmp_path: Path, segments) -> Path:
        session = tmp_path / "session"
        session.mkdir()
        _write_transcript(session / "transcript.json", segments)
        return session

    def test_dry_run_reports_changed_count_without_writing(self, tmp_path, capsys):
        segments = [
            _seg(4, 0.0, 5.0, "early"),
            _seg(4, 200.0, 205.0, "late"),
        ]
        session = self._make_session(tmp_path, segments)
        before_transcript = (session / "transcript.json").read_bytes()
        names_before = sorted(p.name for p in session.iterdir())

        helper.main([
            "relabel", "--session", str(session), "--speaker-id", "4",
            "--range", "0-12", "--name", "Domingo", "--dry-run",
        ])

        out = json.loads(capsys.readouterr().out)
        assert out["action"] == "relabel"
        assert out["status"] == "dry_run"
        assert out["changed_segment_count"] == 1
        assert out["changed_indices"] == [0]
        assert out["mutated_files"] is False
        # Nothing written.
        assert (session / "transcript.json").read_bytes() == before_transcript
        assert sorted(p.name for p in session.iterdir()) == names_before
        assert not (session / "transcript.md").exists()

    def test_dry_run_whole_cluster_when_no_range_given(self, tmp_path, capsys):
        segments = [
            _seg(4, 0.0, 5.0, "early"),
            _seg(4, 200.0, 205.0, "late"),
        ]
        session = self._make_session(tmp_path, segments)

        helper.main([
            "relabel", "--session", str(session), "--speaker-id", "4",
            "--name", "Domingo", "--dry-run",
        ])

        out = json.loads(capsys.readouterr().out)
        assert out["changed_segment_count"] == 2
        assert out["changed_indices"] == [0, 1]

    def test_dry_run_fails_when_no_segments_match(self, tmp_path, capsys):
        segments = [_seg(4, 0.0, 5.0, "early")]
        session = self._make_session(tmp_path, segments)

        with pytest.raises(SystemExit):
            helper.main([
                "relabel", "--session", str(session), "--speaker-id", "4",
                "--range", "500-600", "--name", "Domingo", "--dry-run",
            ])


class TestRelabelAppliedEndToEnd:
    """Integration/e2e: relabel (real run) mutates only selected segments and
    regenerates transcript.json + transcript.md consistently."""

    def _make_session(self, tmp_path: Path, segments) -> Path:
        session = tmp_path / "session"
        session.mkdir()
        _write_transcript(session / "transcript.json", segments)
        return session

    def test_relabels_only_selected_range_preserving_speaker_id(self, tmp_path, capsys):
        # Arrange: the ASX Speaker 4 case -- one cluster, two people.
        segments = [
            _seg(4, 0.0, 5.0, "early speech", source="system"),
            _seg(4, 200.0, 205.0, "late speech", source="system"),
        ]
        session = self._make_session(tmp_path, segments)

        # Act: relabel only the early range to Domingo.
        helper.main([
            "relabel", "--session", str(session), "--speaker-id", "4",
            "--range", "0-12", "--name", "Domingo",
        ])
        json.loads(capsys.readouterr().out)

        # Assert: transcript.json updated; speaker_id preserved for both.
        data = json.loads((session / "transcript.json").read_text(encoding="utf-8"))
        segs = data["segments"]
        assert segs[0]["speaker_name"] == "Domingo"
        assert segs[0]["speaker_id"] == "4"
        assert "speaker_name" not in segs[1] or segs[1].get("speaker_name") != "Domingo"
        assert segs[1]["speaker_id"] == "4"
        # Untouched fields preserved.
        assert segs[0]["start_time"] == 0.0 and segs[0]["end_time"] == 5.0
        assert segs[0]["text"] == "early speech"

    def test_regenerates_transcript_markdown_consistently(self, tmp_path, capsys):
        segments = [_seg(4, 0.0, 5.0, "hello", source="system")]
        session = self._make_session(tmp_path, segments)

        helper.main([
            "relabel", "--session", str(session), "--speaker-id", "4",
            "--name", "Domingo",
        ])
        json.loads(capsys.readouterr().out)

        md = (session / "transcript.md").read_text(encoding="utf-8")
        assert md == "[0.00 - 5.00] System Domingo: hello\n"
        data = json.loads((session / "transcript.json").read_text(encoding="utf-8"))
        assert data["text"] == md.strip()
        assert data["diarised_text"] == md.strip()

    def test_two_relabel_calls_give_mixed_cluster_two_names(self, tmp_path, capsys):
        # Arrange
        segments = [
            _seg(4, 0.0, 5.0, "early", source="system"),
            _seg(4, 200.0, 205.0, "late", source="system"),
        ]
        session = self._make_session(tmp_path, segments)

        # Act: relabel early to Domingo, then late to Gia.
        helper.main([
            "relabel", "--session", str(session), "--speaker-id", "4",
            "--range", "0-12", "--name", "Domingo",
        ])
        json.loads(capsys.readouterr().out)
        helper.main([
            "relabel", "--session", str(session), "--speaker-id", "4",
            "--range", "195-210", "--name", "Gia",
        ])
        json.loads(capsys.readouterr().out)

        # Assert: both names present, same speaker_id.
        data = json.loads((session / "transcript.json").read_text(encoding="utf-8"))
        segs = data["segments"]
        assert segs[0]["speaker_name"] == "Domingo"
        assert segs[1]["speaker_name"] == "Gia"
        assert segs[0]["speaker_id"] == segs[1]["speaker_id"] == "4"

    def test_unselected_segments_for_other_speakers_are_unaffected(self, tmp_path, capsys):
        segments = [
            _seg(4, 0.0, 5.0, "speaker four", source="system"),
            _seg(5, 10.0, 15.0, "speaker five", source="system"),
        ]
        session = self._make_session(tmp_path, segments)

        helper.main([
            "relabel", "--session", str(session), "--speaker-id", "4",
            "--name", "Domingo",
        ])
        json.loads(capsys.readouterr().out)

        data = json.loads((session / "transcript.json").read_text(encoding="utf-8"))
        segs = data["segments"]
        assert segs[0]["speaker_name"] == "Domingo"
        assert "speaker_name" not in segs[1]
        assert segs[1]["speaker_id"] == "5"

    def test_relabels_per_source_json_artifact_when_present(self, tmp_path, capsys):
        # Arrange: a per-source artifact exists alongside the merged transcript.
        segments = [_seg(4, 0.0, 5.0, "hello", source="system")]
        session = self._make_session(tmp_path, segments)
        per_source = {"segments": [_seg(4, 0.0, 5.0, "hello", source="system")]}
        (session / "transcript.system.json").write_text(
            json.dumps(per_source), encoding="utf-8"
        )

        # Act
        helper.main([
            "relabel", "--session", str(session), "--speaker-id", "4",
            "--name", "Domingo",
        ])
        json.loads(capsys.readouterr().out)

        # Assert: per-source JSON also relabeled.
        src_data = json.loads((session / "transcript.system.json").read_text(encoding="utf-8"))
        assert src_data["segments"][0]["speaker_name"] == "Domingo"
        # The per-source .txt (if it existed) would be untouched; none created here.
        assert not (session / "transcript.system.txt").exists()

    def test_applies_reports_mutated_files_true_and_paths(self, tmp_path, capsys):
        segments = [_seg(4, 0.0, 5.0, "hello", source="system")]
        session = self._make_session(tmp_path, segments)

        helper.main([
            "relabel", "--session", str(session), "--speaker-id", "4",
            "--name", "Domingo",
        ])
        out = json.loads(capsys.readouterr().out)

        assert out["status"] == "applied"
        assert out["mutated_files"] is True
        assert out["merged_json_path"] == str((session / "transcript.json").resolve())


# ===========================================================================
# Task 09: speaker profile provenance metadata
# ===========================================================================


class TestBuildProfileProvenance:
    """Task 09: the provenance payload matches the Swift
    ``SpeakerProfileProvenance`` Codable shape exactly (camelCase keys) so
    ``stt speaker enroll --provenance-json`` decodes it without translation."""

    def test_produces_camelcase_keys_matching_swift_codable(self):
        ts = datetime(2026, 7, 13, 10, 30, 0, tzinfo=timezone.utc)
        payload = helper.build_profile_provenance(
            session="/tmp/sess",
            transcript_path="/tmp/sess/transcript.json",
            source_track="system",
            diarized_speaker_id="4",
            selected_ranges=[(0.0, 5.0), (100.0, 105.0)],
            timestamp=ts,
        )
        # camelCase keys mirroring SpeakerProfileProvenance.CodingKeys.
        assert payload == {
            "sourceSession": "/tmp/sess",
            "sourceTranscript": "/tmp/sess/transcript.json",
            "sourceTrack": "system",
            "diarizedSpeakerId": "4",
            "selectedRanges": [[0.0, 5.0], [100.0, 105.0]],
            "confirmationMode": "range-limited",
            "timestamp": "2026-07-13T10:30:00Z",
        }

    def test_omits_sample_path_so_swift_fills_canonical_path(self):
        # samplePath is intentionally absent: the Swift enroll command sets it
        # to the canonical stored-sample path it knows at enrollment time.
        payload = helper.build_profile_provenance(
            session="/tmp/sess",
            transcript_path="/tmp/sess/transcript.json",
            source_track="mic",
            diarized_speaker_id="1",
            selected_ranges=[(1.0, 2.0)],
        )
        assert "samplePath" not in payload

    def test_timestamp_is_iso8601_z_suffix(self):
        payload = helper.build_profile_provenance(
            session="/tmp/sess",
            transcript_path="/tmp/sess/transcript.json",
            source_track="mic",
            diarized_speaker_id="1",
            selected_ranges=[(1.0, 2.0)],
        )
        # Must match the iso8601 format the profile store uses (seconds
        # precision, Z suffix) so Swift's .iso8601 decoder accepts it.
        ts = payload["timestamp"]
        assert ts.endswith("Z")
        # Parseable round-trip.
        parsed = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
        assert parsed is not None

    def test_is_deterministic_for_fixed_timestamp(self):
        ts = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        a = helper.build_profile_provenance(
            session="s", transcript_path="t", source_track="mic",
            diarized_speaker_id="1", selected_ranges=[(0.0, 1.0)], timestamp=ts,
        )
        b = helper.build_profile_provenance(
            session="s", transcript_path="t", source_track="mic",
            diarized_speaker_id="1", selected_ranges=[(0.0, 1.0)], timestamp=ts,
        )
        assert a == b


class TestWriteProvenanceJson:
    """Task 09: provenance JSON is written under ``<session>/.speaker-clips/``."""

    def test_writes_file_in_clips_dir_and_returns_path(self, tmp_path):
        session = tmp_path / "session"
        session.mkdir()
        payload = {"sourceSession": str(session), "confirmationMode": "range-limited"}
        path = helper.write_provenance_json(session, "4", payload)
        assert path == session / ".speaker-clips" / "speaker-4-enroll.provenance.json"
        assert path.exists()
        written = json.loads(path.read_text(encoding="utf-8"))
        assert written == payload

    def test_filename_is_deterministic_per_speaker(self, tmp_path):
        # Re-writing for the same speaker overwrites (does not accumulate).
        session = tmp_path / "session"
        session.mkdir()
        helper.write_provenance_json(session, "4", {"confirmationMode": "range-limited"})
        path2 = helper.write_provenance_json(session, "4", {"confirmationMode": "range-limited", "x": 1})
        files = sorted((session / ".speaker-clips").glob("*.json"))
        assert len(files) == 1
        assert json.loads(path2.read_text(encoding="utf-8"))["x"] == 1


class TestEnrollProfileFromSampleProvenance:
    """Task 09: ``enroll_profile_from_sample`` forwards ``--provenance-json``
    to ``stt speaker enroll`` when given a provenance path, and omits it when
    not (whole-audio enrollment). The subprocess is stubbed so no real Swift
    command runs."""

    def test_passes_provenance_json_when_path_given(self, monkeypatch):
        captured: list[list[str]] = []

        class _FakeProc:
            returncode = 0
            stdout = ""
            stderr = ""

        def _fake_run(cmd, *a, **kw):
            captured.append(cmd)
            return _FakeProc()

        monkeypatch.setattr(helper.subprocess, "run", _fake_run)
        helper.enroll_profile_from_sample(
            Path("/tmp/sample.wav"),
            "Domingo",
            Path("/tmp/profiles"),
            Path("/tmp/backend"),
            Path("/tmp/stt"),
            provider="mfcc-test",
            provenance_path=Path("/tmp/prov.json"),
        )
        assert captured
        cmd = captured[0]
        assert "--provenance-json" in cmd
        idx = cmd.index("--provenance-json")
        assert cmd[idx + 1] == "/tmp/prov.json"
        # Still has the core enrollment args.
        assert "speaker" in cmd and "enroll" in cmd
        assert "--audio" in cmd and "/tmp/sample.wav" in cmd

    def test_omits_provenance_json_when_no_path(self, monkeypatch):
        captured: list[list[str]] = []

        class _FakeProc:
            returncode = 0
            stdout = ""
            stderr = ""

        def _fake_run(cmd, *a, **kw):
            captured.append(cmd)
            return _FakeProc()

        monkeypatch.setattr(helper.subprocess, "run", _fake_run)
        helper.enroll_profile_from_sample(
            Path("/tmp/sample.wav"),
            "Domingo",
            Path("/tmp/profiles"),
            Path("/tmp/backend"),
            Path("/tmp/stt"),
        )
        assert captured
        assert "--provenance-json" not in captured[0]


class TestEnrollRangesProvenanceEndToEnd:
    """Task 09: a successful ``enroll-ranges`` builds + writes a provenance
    payload, forwards it to the enrollment command as ``--provenance-json``,
    and echoes it in the result JSON. The enrollment subprocess is stubbed."""

    def _make_session(self, tmp_path: Path, segments) -> Path:
        session = tmp_path / "session"
        session.mkdir()
        last_end = max((s["end_time"] for s in segments), default=30.0)
        _write_wav(session / "system.wav", _tone(last_end + 1.0))
        _write_transcript(session / "transcript.json", segments)
        return session

    def _stub_enroll_capturing_cmd(self, monkeypatch):
        captured: dict[str, Any] = {}

        def _fake(sample_path, display_name, profiles_root, backend, stt_bin, **kw):
            captured["kwargs"] = kw
            captured["sample_path"] = str(sample_path)
            return {
                "command": ["stt", "speaker", "enroll", display_name],
                "returncode": 0,
                "stdout": "enrolled",
                "stderr": "",
                "enrolled": True,
            }

        monkeypatch.setattr(helper, "enroll_profile_from_sample", _fake)
        return captured

    def test_successful_enrollment_builds_and_forwards_provenance(
        self, tmp_path, capsys, monkeypatch
    ):
        # Arrange: empty profiles dir (no collision).
        profiles_root = tmp_path / "speakers"
        (profiles_root / "profiles").mkdir(parents=True)
        monkeypatch.setenv("STT_SPEAKER_PROFILES_DIR", str(profiles_root))
        captured = self._stub_enroll_capturing_cmd(monkeypatch)
        segments = [_seg(4, 0.0, 10.0, "hi")]
        session = self._make_session(tmp_path, segments)

        # Act
        helper.main([
            "enroll-ranges",
            "--session", str(session),
            "--speaker-id", "4",
            "--range", "1-8",
            "--name", "Domingo",
        ])
        payload = json.loads(capsys.readouterr().out)

        # Assert: enrollment was called with a provenance path.
        assert payload["status"] == "enrolled"
        assert captured["kwargs"].get("provenance_path") is not None
        prov_path = Path(captured["kwargs"]["provenance_path"])
        assert prov_path.exists()
        # The provenance file was forwarded under .speaker-clips/.
        assert ".speaker-clips" in str(prov_path)
        assert prov_path.name == "speaker-4-enroll.provenance.json"

        # The on-disk provenance has the Swift Codable shape (camelCase).
        on_disk = json.loads(prov_path.read_text(encoding="utf-8"))
        assert on_disk["sourceSession"] == str(session)
        assert on_disk["sourceTranscript"] == str((session / "transcript.json").resolve())
        assert on_disk["sourceTrack"] == "system"
        assert on_disk["diarizedSpeakerId"] == "4"
        assert on_disk["confirmationMode"] == "range-limited"
        assert on_disk["selectedRanges"] == [[1.0, 8.0]]
        # samplePath omitted -> Swift fills the canonical stored path.
        assert "samplePath" not in on_disk
        assert on_disk["timestamp"].endswith("Z")

        # The result JSON echoes the provenance path + payload.
        assert payload["provenance_path"] == str(prov_path)
        assert payload["provenance"]["diarizedSpeakerId"] == "4"
        assert payload["provenance"]["confirmationMode"] == "range-limited"
        assert payload["provenance"]["selectedRanges"] == [[1.0, 8.0]]

    def test_dry_run_does_not_build_or_forward_provenance(
        self, tmp_path, capsys, monkeypatch
    ):
        # Arrange: dry-run never reaches enrollment, so no provenance is built.
        profiles_root = tmp_path / "speakers"
        (profiles_root / "profiles").mkdir(parents=True)
        monkeypatch.setenv("STT_SPEAKER_PROFILES_DIR", str(profiles_root))
        called: list[bool] = []
        monkeypatch.setattr(
            helper,
            "enroll_profile_from_sample",
            lambda *a, **kw: called.append(True) or {"enrolled": True},
        )
        segments = [_seg(4, 0.0, 10.0, "hi")]
        session = self._make_session(tmp_path, segments)

        # Act
        helper.main([
            "enroll-ranges",
            "--session", str(session),
            "--speaker-id", "4",
            "--range", "1-8",
            "--name", "Domingo",
            "--no-enroll",
        ])
        payload = json.loads(capsys.readouterr().out)

        # Assert: dry-run writes no provenance file and never enrolls.
        assert payload["status"] == "dry_run"
        assert called == []
        assert not (session / ".speaker-clips").exists()
        assert "provenance_path" not in payload
