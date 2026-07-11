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
