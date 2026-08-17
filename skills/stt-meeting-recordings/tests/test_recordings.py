#!/usr/bin/env python3
"""Unit tests for scripts/recordings.py reliability behavior.

Covers: adaptive per-track timeout resolution, atomic active-transcription
PID persistence + success finalization, run-specific staging + promotion,
postcondition validation (malformed/missing output), stale-artifact
false-success prevention (both live reconciliation and `--resume`), legacy
canonical recovery gated on freshness, duplicate-transcription prevention
(including concurrent launch ownership via the vault lock), recorder
kill-safety identity verification, explicit session safety checks,
`--resume` finalizing without invoking stt when appropriate, blocked-signal
launch semantics for both `transcribe()` and `start()` (including the
conservative no-pthread_sigmask fallback), and journalled transactional
canonical-artifact promotion with fault-injected rollback + crash-journal
reconciliation.

Run with:
    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
import wave
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
TESTS_DIR = Path(__file__).resolve().parent
FAKE_STT = TESTS_DIR / "fake_stt.py"

sys.path.insert(0, str(SCRIPTS_DIR))
import recordings as r  # noqa: E402


def write_wav(path: Path, seconds: float, framerate: int = 16000) -> None:
    n_frames = int(seconds * framerate)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(framerate)
        wf.writeframes(b"\x00\x00" * n_frames)


def poll_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def run_capturing_stdout(func, args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        func(args)
    return buf.getvalue()


def parse_json_stream(raw: str) -> list[dict]:
    """Decode consecutive (possibly pretty-printed) JSON objects from one
    stdout buffer, e.g. a `scheduled` payload followed by a
    `recording_started` payload."""
    decoder = json.JSONDecoder()
    results: list[dict] = []
    i = 0
    n = len(raw)
    while i < n:
        while i < n and raw[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        obj, end = decoder.raw_decode(raw, i)
        results.append(obj)
        i = end
    return results


class VaultTestCase(unittest.TestCase):
    """Base test case with an isolated temp vault + fake stt binary."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="stt-test-vault-")
        self.vault = Path(self._tmpdir)
        r.recordings_root(self.vault).mkdir(parents=True, exist_ok=True)

        self._env_overrides = {
            "OBSIDIAN_VAULT": str(self.vault),
            "STT_BIN": str(FAKE_STT),
        }
        self._old_env: dict[str, str | None] = {}
        for key, value in self._env_overrides.items():
            self._old_env[key] = os.environ.get(key)
            os.environ[key] = value
        for key in (
            "FAKE_STT_SLEEP",
            "FAKE_STT_EXIT_CODE",
            "FAKE_STT_NO_WRITE",
            "FAKE_STT_INVOCATION_LOG",
            "FAKE_STT_MALFORMED_JSON",
            "FAKE_STT_EMPTY_MD",
        ):
            self._old_env[key] = os.environ.get(key)
            os.environ.pop(key, None)

        # Speed up tests: skip the real growth-check sleep (its logic is
        # covered directly by GrowingAudioGuardTests).
        self._old_growth_check = r.AUDIO_GROWTH_CHECK_SECONDS
        r.AUDIO_GROWTH_CHECK_SECONDS = 0.0

        self._spawned_pids: list[int] = []

    def tearDown(self) -> None:
        r.AUDIO_GROWTH_CHECK_SECONDS = self._old_growth_check
        for pid in self._spawned_pids:
            try:
                os.kill(pid, 9)
            except OSError:
                pass
            try:
                os.waitpid(pid, 0)
            except (ChildProcessError, OSError):
                pass
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def make_session(
        self,
        title: str = "Test session",
        mode: str = "mic",
        status: str = "stopped",
        token: str = "20260101-000000-test-session",
    ) -> tuple[dict, Path]:
        session_dir = r.recordings_root(self.vault) / token
        session_dir.mkdir(parents=True, exist_ok=True)
        wav_path = session_dir / "mic.wav"
        write_wav(wav_path, 1.0)
        session = {
            "title": title,
            "mode": mode,
            "status": status,
            "session_dir": str(session_dir),
            "started_at": r.iso_now(),
            "started_at_human": r.now_human(),
            "ended_at": r.iso_now(),
            "ended_at_human": r.now_human(),
            "audio_files": [str(wav_path)],
        }
        r.write_json(session_dir / "metadata.json", session)
        r.write_session_note(session, self.vault)
        return session, session_dir

    def spawn_process(self, args: list[str]) -> subprocess.Popen:
        proc = subprocess.Popen(args)
        self._spawned_pids.append(proc.pid)
        return proc

    def make_sleep_script(self, seconds: float = 30.0) -> Path:
        """A tiny standalone executable that just sleeps.

        Deliberately not `sys.executable` + `-c ...`: on some platforms
        (e.g. macOS framework Python builds routed through an internal
        `Python.app/Contents/MacOS/Python` launcher stub) the on-disk
        process image path reported by `ps` differs from `sys.executable`
        itself, which would make an `stt_bin`-in-cmdline substring check
        against `sys.executable` spuriously fail. A plain shell script
        avoids that indirection entirely: `ps` reports its own real path
        verbatim.
        """
        script = Path(self._tmpdir) / f"sleep-{uuid.uuid4().hex}.sh"
        script.write_text(f"#!/bin/sh\nsleep {seconds}\n", encoding="utf-8")
        script.chmod(0o755)
        return script

    def spawn_dead_pid(self, marker: str = "") -> int:
        """Spawn and fully reap a trivial subprocess; its PID is now dead."""
        proc = subprocess.Popen([sys.executable, "-c", "pass", marker])
        proc.wait()
        return proc.pid

    def transcribe_args(self, **overrides):
        defaults = dict(
            session=None,
            device="auto",
            timeout=None,
            model=r.DEFAULT_MODEL,
            max_new_tokens=64,
            require_backend_ready=False,
            diarize=False,
            identify=False,
            resume=False,
        )
        defaults.update(overrides)
        return argparse_namespace(defaults)


def argparse_namespace(d: dict):
    import argparse
    return argparse.Namespace(**d)


class AdaptiveTimeoutTests(unittest.TestCase):
    def test_explicit_value_wins(self) -> None:
        self.assertEqual(r.resolve_transcription_timeout(42.0, [999999.0]), 42.0)

    def test_no_duration_falls_back_to_1800(self) -> None:
        self.assertEqual(r.resolve_transcription_timeout(None, []), 1800.0)

    def test_short_duration_floors_at_1800(self) -> None:
        self.assertEqual(r.resolve_transcription_timeout(None, [10.0]), 1800.0)

    def test_scales_with_longest_duration(self) -> None:
        # 4 * 600 + 300 = 2700, above the 1800 floor.
        self.assertEqual(r.resolve_transcription_timeout(None, [600.0, 30.0]), 2700.0)

    def test_gather_audio_durations_reads_real_wav(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            wav_path = Path(td) / "sample.wav"
            write_wav(wav_path, 2.0, framerate=8000)
            durations = r.gather_audio_durations(None, wav_path)
            self.assertEqual(len(durations), 1)
            self.assertAlmostEqual(durations[0], 2.0, places=2)

    def test_gather_audio_durations_track_pair(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            mic = Path(td) / "mic.wav"
            system = Path(td) / "system.wav"
            write_wav(mic, 1.0)
            write_wav(system, 3.0)
            durations = r.gather_audio_durations((mic, system), None)
            self.assertEqual(sorted(durations), [1.0, 3.0])

    def test_unreadable_wav_is_skipped_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bogus = Path(td) / "not-a-wav.wav"
            bogus.write_text("not really a wav file")
            durations = r.gather_audio_durations(None, bogus)
            self.assertEqual(durations, [])
            # Falls back to the 1800 floor when nothing could be read.
            self.assertEqual(r.resolve_transcription_timeout(None, durations), 1800.0)


class AtomicWriteJsonTests(unittest.TestCase):
    def test_write_json_round_trips_and_leaves_no_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "state.json"
            r.write_json(target, {"a": 1, "b": [1, 2, 3]})
            self.assertEqual(r.load_json(target), {"a": 1, "b": [1, 2, 3]})
            leftovers = [p for p in Path(td).iterdir() if p.name != "state.json"]
            self.assertEqual(leftovers, [])

    def test_write_json_overwrite_never_leaves_truncated_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "state.json"
            r.write_json(target, {"first": True})
            r.write_json(target, {"second": True, "payload": list(range(200))})
            self.assertEqual(r.load_json(target), {"second": True, "payload": list(range(200))})


class TranscriptShapeValidationTests(unittest.TestCase):
    def test_dict_with_segments_list_is_valid(self) -> None:
        self.assertTrue(r.validate_transcript_json_shape({"segments": []}))

    def test_top_level_list_is_valid(self) -> None:
        self.assertTrue(r.validate_transcript_json_shape([{"text": "hi"}]))

    def test_dict_without_segments_is_invalid(self) -> None:
        self.assertFalse(r.validate_transcript_json_shape({"foo": "bar"}))

    def test_segments_not_a_list_is_invalid(self) -> None:
        self.assertFalse(r.validate_transcript_json_shape({"segments": "nope"}))

    def test_scalar_is_invalid(self) -> None:
        self.assertFalse(r.validate_transcript_json_shape("just a string"))


class ValidateStagedOutputsTests(unittest.TestCase):
    def test_missing_files_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ok, reason = r.validate_staged_outputs(Path(td), None)
            self.assertFalse(ok)
            self.assertIn("missing", reason)

    def test_empty_markdown_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            staging = Path(td)
            (staging / "transcript.md").write_text("", encoding="utf-8")
            (staging / "transcript.json").write_text(json.dumps({"segments": []}), encoding="utf-8")
            ok, reason = r.validate_staged_outputs(staging, None)
            self.assertFalse(ok)
            self.assertIn("empty", reason)

    def test_malformed_json_shape_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            staging = Path(td)
            (staging / "transcript.md").write_text("hello", encoding="utf-8")
            (staging / "transcript.json").write_text(json.dumps({"foo": "bar"}), encoding="utf-8")
            ok, reason = r.validate_staged_outputs(staging, None)
            self.assertFalse(ok)
            self.assertIn("shape", reason)

    def test_invalid_json_syntax_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            staging = Path(td)
            (staging / "transcript.md").write_text("hello", encoding="utf-8")
            (staging / "transcript.json").write_text("{not json", encoding="utf-8")
            ok, reason = r.validate_staged_outputs(staging, None)
            self.assertFalse(ok)

    def test_valid_dict_shape_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            staging = Path(td)
            (staging / "transcript.md").write_text("hello", encoding="utf-8")
            (staging / "transcript.json").write_text(json.dumps({"segments": [{"text": "hi"}]}), encoding="utf-8")
            ok, reason = r.validate_staged_outputs(staging, None)
            self.assertTrue(ok, reason)

    def test_valid_top_level_list_shape_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            staging = Path(td)
            (staging / "transcript.md").write_text("hello", encoding="utf-8")
            (staging / "transcript.json").write_text(json.dumps([{"text": "hi"}]), encoding="utf-8")
            ok, reason = r.validate_staged_outputs(staging, None)
            self.assertTrue(ok, reason)


class GrowingAudioGuardTests(unittest.TestCase):
    def test_growing_audio_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "audio.wav"
            path.write_bytes(b"0" * 100)

            def grow() -> None:
                time.sleep(0.05)
                with open(path, "ab") as fh:
                    fh.write(b"1" * 50)

            t = threading.Thread(target=grow)
            t.start()
            with self.assertRaises(SystemExit):
                r.assert_audio_not_growing([path], settle_seconds=0.2)
            t.join()

    def test_stable_audio_is_accepted_and_fingerprinted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "audio.wav"
            path.write_bytes(b"0" * 100)
            fingerprints = r.assert_audio_not_growing([path], settle_seconds=0.05)
            self.assertEqual(fingerprints[0]["size"], 100)
            self.assertEqual(fingerprints[0]["path"], str(path))
            self.assertIn("mtime", fingerprints[0])


class ActiveTranscriptionPersistenceTests(VaultTestCase):
    def test_pid_and_run_id_persisted_before_completion_and_finalized_on_success(self) -> None:
        session, session_dir = self.make_session()
        os.environ["FAKE_STT_SLEEP"] = "0.4"

        args = self.transcribe_args(session=str(session_dir))
        result_holder: dict = {}

        def run():
            result_holder["stdout"] = run_capturing_stdout(r.transcribe, args)

        thread = threading.Thread(target=run)
        thread.start()

        active_path = r.active_transcription_state_path(self.vault)
        seen_active = poll_until(
            lambda: active_path.exists() and r.load_json(active_path).get("pid") is not None,
            timeout=5.0,
        )
        self.assertTrue(seen_active, "active_transcription.json with a persisted PID was not created promptly")

        payload = r.load_json(active_path)
        self.assertEqual(payload["session_dir"], str(session_dir))
        self.assertIn("pid", payload)
        self.assertTrue(payload["pid"] > 0)
        self.assertTrue(r.process_alive(payload["pid"]), "child PID should still be alive while stt 'runs'")
        self.assertIn("command", payload)
        self.assertIn(str(FAKE_STT), payload["command"][0])
        self.assertIn("log_path", payload)
        self.assertIn("started_at", payload)
        self.assertIn("requested_options", payload)
        self.assertIn("resolved_timeout", payload)
        # New reliability fields: run id, run-specific staging dir, input
        # fingerprints -- all persisted before waiting on the child.
        self.assertTrue(payload.get("run_id"))
        self.assertIn("staging_dir", payload)
        self.assertTrue(Path(payload["staging_dir"]).exists())
        self.assertIn(".stt-staging", payload["staging_dir"])
        self.assertIn("input_fingerprints", payload)
        self.assertTrue(len(payload["input_fingerprints"]) >= 1)
        # No explicit --timeout was passed, so the adaptive default applies.
        self.assertGreaterEqual(payload["resolved_timeout"], 1800.0)

        # Metadata must reflect "transcribing" status before we finish waiting.
        mid_metadata = r.load_json(session_dir / "metadata.json")
        self.assertEqual(mid_metadata["status"], "transcribing")
        self.assertEqual(mid_metadata["transcription_pid"], payload["pid"])
        self.assertEqual(mid_metadata["transcription_run_id"], payload["run_id"])
        self.assertEqual(mid_metadata["transcription_staging_dir"], payload["staging_dir"])

        thread.join(timeout=10.0)
        self.assertFalse(thread.is_alive(), "transcribe() did not finish in time")

        final_metadata = r.load_json(session_dir / "metadata.json")
        self.assertEqual(final_metadata["status"], "transcribed")
        self.assertTrue(Path(final_metadata["transcript_path"]).exists())
        self.assertTrue(Path(final_metadata["transcript_json_path"]).exists())
        # Canonical (promoted) files live in the session root, not staging.
        self.assertEqual(Path(final_metadata["transcript_path"]).parent, session_dir)
        self.assertFalse(active_path.exists(), "active-transcription state should be cleared on success")
        self.assertIn("transcribed", result_holder["stdout"])

    def test_failure_marks_transcription_failed_and_clears_active_state(self) -> None:
        session, session_dir = self.make_session()
        os.environ["FAKE_STT_EXIT_CODE"] = "3"
        args = self.transcribe_args(session=str(session_dir))
        with self.assertRaises(SystemExit):
            r.transcribe(args)
        metadata = r.load_json(session_dir / "metadata.json")
        self.assertEqual(metadata["status"], "transcription_failed")
        self.assertIn("failure_reason", metadata)
        self.assertIn("failed_at", metadata)
        self.assertNotIn("transcription_pid", metadata)
        self.assertNotIn("transcription_started_at", metadata)
        self.assertFalse(r.active_transcription_state_path(self.vault).exists())


class PostconditionValidationTests(VaultTestCase):
    """B: a zero exit code alone must not be treated as success."""

    def test_zero_exit_no_output_marks_failed_preserves_log_and_staging(self) -> None:
        session, session_dir = self.make_session()
        os.environ["FAKE_STT_NO_WRITE"] = "1"
        args = self.transcribe_args(session=str(session_dir))
        with self.assertRaises(SystemExit) as ctx:
            r.transcribe(args)
        self.assertIn("output validation failed", str(ctx.exception))

        metadata = r.load_json(session_dir / "metadata.json")
        self.assertEqual(metadata["status"], "transcription_failed")
        self.assertIn("missing", metadata["failure_reason"])
        self.assertTrue((session_dir / "transcription.log").exists(), "log must be preserved on failure")
        staging_dirs = list((session_dir / ".stt-staging").glob("*"))
        self.assertEqual(len(staging_dirs), 1, "staging directory must be preserved on failure, not deleted")
        self.assertFalse(r.active_transcription_state_path(self.vault).exists())
        # No canonical transcript.md/json should have been created.
        self.assertFalse((session_dir / "transcript.md").exists())
        self.assertFalse((session_dir / "transcript.json").exists())

    def test_zero_exit_malformed_json_marks_failed(self) -> None:
        session, session_dir = self.make_session()
        os.environ["FAKE_STT_MALFORMED_JSON"] = "1"
        args = self.transcribe_args(session=str(session_dir))
        with self.assertRaises(SystemExit):
            r.transcribe(args)
        metadata = r.load_json(session_dir / "metadata.json")
        self.assertEqual(metadata["status"], "transcription_failed")
        self.assertIn("shape", metadata["failure_reason"])

    def test_zero_exit_empty_markdown_marks_failed(self) -> None:
        session, session_dir = self.make_session()
        os.environ["FAKE_STT_EMPTY_MD"] = "1"
        args = self.transcribe_args(session=str(session_dir))
        with self.assertRaises(SystemExit):
            r.transcribe(args)
        metadata = r.load_json(session_dir / "metadata.json")
        self.assertEqual(metadata["status"], "transcription_failed")
        self.assertIn("empty", metadata["failure_reason"])

    def test_old_canonical_artifacts_survive_failed_current_run(self) -> None:
        """A: an old canonical transcript must never be mistaken for a new,
        failed attempt's success, and must be left completely untouched."""
        session, session_dir = self.make_session()
        (session_dir / "transcript.md").write_text("# Old\n\nSpeaker 0: old content\n", encoding="utf-8")
        (session_dir / "transcript.json").write_text(
            json.dumps({"segments": [{"text": "old"}]}), encoding="utf-8"
        )
        old_md_content = (session_dir / "transcript.md").read_text(encoding="utf-8")
        old_json_content = (session_dir / "transcript.json").read_text(encoding="utf-8")
        old_md_mtime = (session_dir / "transcript.md").stat().st_mtime

        os.environ["FAKE_STT_NO_WRITE"] = "1"
        args = self.transcribe_args(session=str(session_dir))
        with self.assertRaises(SystemExit):
            r.transcribe(args)

        metadata = r.load_json(session_dir / "metadata.json")
        self.assertEqual(metadata["status"], "transcription_failed")
        self.assertEqual((session_dir / "transcript.md").read_text(encoding="utf-8"), old_md_content)
        self.assertEqual((session_dir / "transcript.json").read_text(encoding="utf-8"), old_json_content)
        self.assertEqual((session_dir / "transcript.md").stat().st_mtime, old_md_mtime)


class StaleReconciliationTests(VaultTestCase):
    def test_dead_pid_with_valid_run_specific_staging_is_finalized(self) -> None:
        """A: reconciliation must validate and promote *this run's own*
        staged output, not just look at whatever sits in the session root."""
        session, session_dir = self.make_session(status="transcribing")
        run_id = "run-aaaa"
        staging_dir = session_dir / ".stt-staging" / run_id
        staging_dir.mkdir(parents=True, exist_ok=True)
        (staging_dir / "transcript.md").write_text("# Transcript\n\nSpeaker 0: hi\n", encoding="utf-8")
        (staging_dir / "transcript.json").write_text(json.dumps({"segments": []}), encoding="utf-8")

        dead_pid = self.spawn_dead_pid(marker=str(session_dir))
        r.write_json(
            r.active_transcription_state_path(self.vault),
            {
                "session_dir": str(session_dir),
                "pid": dead_pid,
                "run_id": run_id,
                "staging_dir": str(staging_dir),
                "command": [str(FAKE_STT), "transcribe", str(session_dir / "mic.wav")],
                "log_path": str(session_dir / "transcription.log"),
                "started_at": r.iso_now(),
                "requested_options": {},
                "resolved_timeout": 1800.0,
                "active": True,
            },
        )

        result = r.reconcile_active_transcription(self.vault)
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "transcribed")
        self.assertFalse(result["active"])
        metadata = r.load_json(session_dir / "metadata.json")
        self.assertEqual(metadata["status"], "transcribed")
        self.assertFalse(r.active_transcription_state_path(self.vault).exists())
        # Promoted into the session root.
        self.assertTrue((session_dir / "transcript.md").exists())
        self.assertTrue((session_dir / "transcript.json").exists())

    def test_dead_pid_without_valid_artifacts_marks_interrupted(self) -> None:
        session, session_dir = self.make_session(status="transcribing")
        # No transcript.md/json written -- simulates a killed mid-run child.

        dead_pid = self.spawn_dead_pid(marker=str(session_dir))
        r.write_json(
            r.active_transcription_state_path(self.vault),
            {
                "session_dir": str(session_dir),
                "pid": dead_pid,
                "command": [str(FAKE_STT), "transcribe", str(session_dir / "mic.wav")],
                "log_path": str(session_dir / "transcription.log"),
                "started_at": r.iso_now(),
                "requested_options": {},
                "resolved_timeout": 1800.0,
                "active": True,
            },
        )

        result = r.reconcile_active_transcription(self.vault)
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "transcription_interrupted")
        self.assertFalse(result["active"])
        metadata = r.load_json(session_dir / "metadata.json")
        self.assertEqual(metadata["status"], "transcription_interrupted")
        self.assertIn("interrupted_at", metadata)
        self.assertFalse(r.active_transcription_state_path(self.vault).exists())

    def test_dead_pid_with_only_old_canonical_artifacts_and_invalid_new_staging_marks_interrupted(self) -> None:
        """A: an old canonical transcript from a previous run must never be
        used to finalize a *new*, run-tracked attempt whose own staged
        output is missing/invalid."""
        session, session_dir = self.make_session(status="transcribing")
        (session_dir / "transcript.md").write_text("# Old\n\nold\n", encoding="utf-8")
        (session_dir / "transcript.json").write_text(json.dumps({"segments": [{"text": "old"}]}), encoding="utf-8")
        old_mtime = (session_dir / "transcript.md").stat().st_mtime

        run_id = "run-empty"
        staging_dir = session_dir / ".stt-staging" / run_id
        staging_dir.mkdir(parents=True, exist_ok=True)  # exists but empty/invalid

        dead_pid = self.spawn_dead_pid(marker=str(session_dir))
        r.write_json(
            r.active_transcription_state_path(self.vault),
            {
                "session_dir": str(session_dir),
                "pid": dead_pid,
                "run_id": run_id,
                "staging_dir": str(staging_dir),
                "command": [str(FAKE_STT)],
                "log_path": str(session_dir / "transcription.log"),
                "started_at": r.iso_now(),
                "requested_options": {},
                "resolved_timeout": 1800.0,
                "active": True,
            },
        )

        result = r.reconcile_active_transcription(self.vault)
        self.assertEqual(result["status"], "transcription_interrupted")
        self.assertFalse(result["active"])
        metadata = r.load_json(session_dir / "metadata.json")
        self.assertEqual(metadata["status"], "transcription_interrupted")
        # Old canonical artifacts must be completely untouched.
        self.assertEqual((session_dir / "transcript.md").stat().st_mtime, old_mtime)
        self.assertIn("old", (session_dir / "transcript.md").read_text(encoding="utf-8"))

    def test_status_command_surfaces_reconciled_transcription(self) -> None:
        session, session_dir = self.make_session(status="transcribing")
        dead_pid = self.spawn_dead_pid(marker=str(session_dir))
        r.write_json(
            r.active_transcription_state_path(self.vault),
            {
                "session_dir": str(session_dir),
                "pid": dead_pid,
                "command": ["irrelevant"],
                "log_path": str(session_dir / "transcription.log"),
                "started_at": r.iso_now(),
                "requested_options": {},
                "resolved_timeout": 1800.0,
                "active": True,
            },
        )
        buf_text = run_capturing_stdout(r.status, argparse_namespace({}))
        payload = json.loads(buf_text)
        self.assertIn("transcription", payload)
        self.assertEqual(payload["transcription"]["status"], "transcription_interrupted")
        self.assertFalse(payload["transcription"]["active"])
        # Recording-only shape must still work (no active recording here).
        self.assertFalse(payload.get("active", False))


class LegacyRecoveryTests(VaultTestCase):
    """Legacy sessions predating run-specific staging: only recoverable via
    a schema-valid canonical transcript strictly newer than the recorded
    attempt start time."""

    def test_legacy_session_recovers_when_canonical_newer_than_start(self) -> None:
        session, session_dir = self.make_session(status="transcribing")
        started_at = r.iso_now()
        time.sleep(0.05)
        (session_dir / "transcript.md").write_text("# Transcript\n\nSpeaker 0: hi\n", encoding="utf-8")
        (session_dir / "transcript.json").write_text(json.dumps({"segments": []}), encoding="utf-8")

        dead_pid = self.spawn_dead_pid(marker=str(session_dir))
        r.write_json(
            r.active_transcription_state_path(self.vault),
            {
                "session_dir": str(session_dir),
                "pid": dead_pid,
                # No run_id at all -- legacy payload.
                "command": [str(FAKE_STT)],
                "log_path": str(session_dir / "transcription.log"),
                "started_at": started_at,
                "requested_options": {},
                "resolved_timeout": 1800.0,
                "active": True,
            },
        )

        result = r.reconcile_active_transcription(self.vault)
        self.assertEqual(result["status"], "transcribed")
        metadata = r.load_json(session_dir / "metadata.json")
        self.assertEqual(metadata["status"], "transcribed")

    def test_legacy_session_does_not_recover_when_canonical_older_than_start(self) -> None:
        session, session_dir = self.make_session(status="transcribing")
        (session_dir / "transcript.md").write_text("# Transcript\n\nSpeaker 0: hi\n", encoding="utf-8")
        (session_dir / "transcript.json").write_text(json.dumps({"segments": []}), encoding="utf-8")
        time.sleep(0.05)
        started_at = r.iso_now()  # started AFTER the canonical files -- stale evidence

        dead_pid = self.spawn_dead_pid(marker=str(session_dir))
        r.write_json(
            r.active_transcription_state_path(self.vault),
            {
                "session_dir": str(session_dir),
                "pid": dead_pid,
                "command": [str(FAKE_STT)],
                "log_path": str(session_dir / "transcription.log"),
                "started_at": started_at,
                "requested_options": {},
                "resolved_timeout": 1800.0,
                "active": True,
            },
        )

        result = r.reconcile_active_transcription(self.vault)
        self.assertEqual(result["status"], "transcription_interrupted")
        metadata = r.load_json(session_dir / "metadata.json")
        self.assertEqual(metadata["status"], "transcription_interrupted")


class DuplicatePreventionTests(VaultTestCase):
    def test_alive_matching_pid_refuses_new_transcribe(self) -> None:
        session, session_dir = self.make_session(status="transcribing")
        proc = self.spawn_process(
            [sys.executable, "-c", "import time; time.sleep(5)", str(session_dir)]
        )
        try:
            r.write_json(
                r.active_transcription_state_path(self.vault),
                {
                    "session_dir": str(session_dir),
                    "pid": proc.pid,
                    "command": [sys.executable, "-c", "import time; time.sleep(5)", str(session_dir)],
                    "log_path": str(session_dir / "transcription.log"),
                    "started_at": r.iso_now(),
                    "requested_options": {},
                    "resolved_timeout": 1800.0,
                    "active": True,
                },
            )

            reconciled = r.reconcile_active_transcription(self.vault)
            self.assertTrue(reconciled["active"])

            args = self.transcribe_args(session=str(session_dir))
            with self.assertRaises(SystemExit) as ctx:
                r.transcribe(args)
            self.assertIn("already active", str(ctx.exception))

            args_start = argparse_namespace(
                dict(title="Another", mode="mic", duration=None, wait=False, wait_timeout=None)
            )
            with self.assertRaises(SystemExit) as ctx_start:
                r.start(args_start)
            self.assertIn("transcription is currently active", str(ctx_start.exception))
        finally:
            proc.kill()
            proc.wait()

    def test_alive_pid_with_mismatched_command_is_treated_as_reused(self) -> None:
        session, session_dir = self.make_session(status="transcribing")
        # Spawn a real, alive process whose command line does NOT mention
        # this session directory -- simulating an unrelated process that
        # happens to have reused a recycled PID.
        proc = self.spawn_process([sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            r.write_json(
                r.active_transcription_state_path(self.vault),
                {
                    "session_dir": str(session_dir),
                    "pid": proc.pid,
                    "command": ["some", "unrelated", "recorded", "command"],
                    "log_path": str(session_dir / "transcription.log"),
                    "started_at": r.iso_now(),
                    "requested_options": {},
                    "resolved_timeout": 1800.0,
                    "active": True,
                },
            )
            reconciled = r.reconcile_active_transcription(self.vault)
            # process_matches_session should fail to find the session path in
            # the live process's command line, so this is treated as stale
            # rather than a genuine duplicate.
            self.assertFalse(reconciled["active"])
            self.assertEqual(reconciled["status"], "transcription_interrupted")
        finally:
            proc.kill()
            proc.wait()


class ConcurrentLaunchTests(VaultTestCase):
    def test_concurrent_transcribe_only_one_launches(self) -> None:
        """C/D: the vault lock must serialize check+launch+reservation so
        two concurrent transcribe() calls on the same session cannot both
        launch; exactly one must be refused as already active."""
        session, session_dir = self.make_session()
        os.environ["FAKE_STT_SLEEP"] = "0.6"

        args_a = self.transcribe_args(session=str(session_dir))
        args_b = self.transcribe_args(session=str(session_dir))

        results: dict[str, tuple[str, str]] = {}

        def run(label: str, args) -> None:
            try:
                out = run_capturing_stdout(r.transcribe, args)
                results[label] = ("ok", out)
            except SystemExit as e:
                results[label] = ("exit", str(e))

        t1 = threading.Thread(target=run, args=("a", args_a))
        t2 = threading.Thread(target=run, args=("b", args_b))
        t1.start()
        time.sleep(0.05)  # give thread A a head start on acquiring the lock
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        self.assertIn("a", results)
        self.assertIn("b", results)
        outcomes = list(results.values())
        refusals = [o for o in outcomes if o[0] == "exit" and "already active" in o[1]]
        self.assertEqual(len(refusals), 1, f"expected exactly one refusal, got: {outcomes}")

        metadata = r.load_json(session_dir / "metadata.json")
        self.assertEqual(metadata["status"], "transcribed")
        self.assertFalse(r.active_transcription_state_path(self.vault).exists())


class InterruptionHandlingTests(VaultTestCase):
    def test_child_alive_reports_transcribing_not_terminal_interrupted(self) -> None:
        session, session_dir = self.make_session(status="transcribing")
        proc = self.spawn_process([sys.executable, "-c", "import time; time.sleep(5)"])
        active_path = r.active_transcription_state_path(self.vault)
        active_payload = {
            "session_dir": str(session_dir),
            "pid": proc.pid,
            "command": ["fake"],
            "log_path": str(session_dir / "transcription.log"),
            "started_at": r.iso_now(),
            "requested_options": {},
            "resolved_timeout": 1800.0,
            "active": True,
        }
        r.write_json(active_path, active_payload)
        try:
            result = r.handle_transcription_interruption(
                session, self.vault, session_dir / "transcription.log", proc
            )
            # G: helper-interrupted-but-child-alive must NOT be a terminal
            # "interrupted" state -- it's still transcribing, just with the
            # helper flagged as interrupted.
            self.assertEqual(result["status"], "transcribing")
            self.assertTrue(result["helper_interrupted"])
            self.assertTrue(result["active"])
            self.assertTrue(result["child_alive"])
            # Active state must be left completely untouched.
            self.assertTrue(active_path.exists())
            self.assertEqual(r.load_json(active_path), active_payload)
            metadata = r.load_json(session_dir / "metadata.json")
            self.assertEqual(metadata["status"], "transcribing")
        finally:
            proc.kill()
            proc.wait()

    def test_child_dead_marks_interrupted_and_clears_state(self) -> None:
        session, session_dir = self.make_session(status="transcribing")
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()  # ensure fully dead/reaped
        active_path = r.active_transcription_state_path(self.vault)
        r.write_json(
            active_path,
            {
                "session_dir": str(session_dir),
                "pid": proc.pid,
                "command": ["fake"],
                "log_path": str(session_dir / "transcription.log"),
                "started_at": r.iso_now(),
                "requested_options": {},
                "resolved_timeout": 1800.0,
                "active": True,
            },
        )
        result = r.handle_transcription_interruption(
            session, self.vault, session_dir / "transcription.log", proc
        )
        self.assertEqual(result["status"], "transcription_interrupted")
        self.assertFalse(result["child_alive"])
        self.assertFalse(result["active"])
        self.assertFalse(active_path.exists())
        metadata = r.load_json(session_dir / "metadata.json")
        self.assertEqual(metadata["status"], "transcription_interrupted")
        self.assertIn("interrupted_at", metadata)


class ResumeTests(VaultTestCase):
    def test_resume_finalizes_from_own_run_specific_staged_output(self) -> None:
        session, session_dir = self.make_session(status="transcription_interrupted")
        run_id = "resume-run-1"
        staging_dir = session_dir / ".stt-staging" / run_id
        staging_dir.mkdir(parents=True, exist_ok=True)
        (staging_dir / "transcript.md").write_text("# Transcript\n\nSpeaker 0: hi\n", encoding="utf-8")
        (staging_dir / "transcript.json").write_text(json.dumps({"segments": []}), encoding="utf-8")
        session["transcription_run_id"] = run_id
        session["transcription_staging_dir"] = str(staging_dir)
        r.write_json(session_dir / "metadata.json", session)

        invocation_log = session_dir / "fake_stt_invocations.jsonl"
        os.environ["FAKE_STT_INVOCATION_LOG"] = str(invocation_log)

        args = self.transcribe_args(session=str(session_dir), resume=True)
        out = run_capturing_stdout(r.transcribe, args)
        payload = json.loads(out)
        self.assertTrue(payload.get("resumed"))
        self.assertEqual(payload["status"], "transcribed")

        self.assertFalse(invocation_log.exists(), "stt must not be invoked again when --resume can finalize directly")
        self.assertTrue((session_dir / "transcript.md").exists())
        metadata = r.load_json(session_dir / "metadata.json")
        self.assertEqual(metadata["status"], "transcribed")
        self.assertFalse(r.active_transcription_state_path(self.vault).exists())

    def test_resume_finalizes_legacy_session_when_canonical_newer_than_start(self) -> None:
        session, session_dir = self.make_session(status="transcription_interrupted")
        session["transcription_started_at"] = r.iso_now()
        r.write_json(session_dir / "metadata.json", session)
        time.sleep(0.05)
        (session_dir / "transcript.md").write_text("# Transcript\n\nSpeaker 0: hi\n", encoding="utf-8")
        (session_dir / "transcript.json").write_text(json.dumps({"segments": []}), encoding="utf-8")

        invocation_log = session_dir / "fake_stt_invocations.jsonl"
        os.environ["FAKE_STT_INVOCATION_LOG"] = str(invocation_log)

        args = self.transcribe_args(session=str(session_dir), resume=True)
        out = run_capturing_stdout(r.transcribe, args)
        payload = json.loads(out)
        self.assertTrue(payload.get("resumed"))
        self.assertEqual(payload["status"], "transcribed")
        self.assertFalse(invocation_log.exists())

    def test_resume_does_not_fall_back_to_stale_canonical_when_run_specific_staging_invalid(self) -> None:
        """A: a modern (run-tracked) session's --resume must never fall back
        to an old canonical transcript from a previous, unrelated run."""
        session, session_dir = self.make_session(status="transcription_interrupted")
        (session_dir / "transcript.md").write_text("# OLD\n\nold content\n", encoding="utf-8")
        (session_dir / "transcript.json").write_text(json.dumps({"segments": [{"text": "old"}]}), encoding="utf-8")
        old_md = (session_dir / "transcript.md").read_text(encoding="utf-8")

        run_id = "resume-run-invalid"
        staging_dir = session_dir / ".stt-staging" / run_id
        staging_dir.mkdir(parents=True, exist_ok=True)  # exists but empty/invalid
        session["transcription_run_id"] = run_id
        session["transcription_staging_dir"] = str(staging_dir)
        session["transcription_started_at"] = r.iso_now()
        r.write_json(session_dir / "metadata.json", session)

        invocation_log = session_dir / "fake_stt_invocations.jsonl"
        os.environ["FAKE_STT_INVOCATION_LOG"] = str(invocation_log)

        args = self.transcribe_args(session=str(session_dir), resume=True)
        out = run_capturing_stdout(r.transcribe, args)
        payload = json.loads(out)
        self.assertNotIn("resumed", payload)
        self.assertTrue(invocation_log.exists(), "must have done a full rerun, not reused stale canonical files")

        metadata = r.load_json(session_dir / "metadata.json")
        self.assertEqual(metadata["status"], "transcribed")
        self.assertNotEqual((session_dir / "transcript.md").read_text(encoding="utf-8"), old_md)

    def test_resume_reruns_full_command_when_no_valid_artifacts(self) -> None:
        session, session_dir = self.make_session(status="transcription_interrupted")
        # No transcript.md/json present -- resume must fall back to a full rerun.
        invocation_log = session_dir / "fake_stt_invocations.jsonl"
        os.environ["FAKE_STT_INVOCATION_LOG"] = str(invocation_log)

        args = self.transcribe_args(session=str(session_dir), resume=True)
        out = run_capturing_stdout(r.transcribe, args)
        payload = json.loads(out)
        self.assertNotIn("resumed", payload)
        self.assertEqual(payload["status"], "transcribed")
        self.assertTrue(invocation_log.exists(), "stt should have been invoked for the full rerun")

        metadata = r.load_json(session_dir / "metadata.json")
        self.assertEqual(metadata["status"], "transcribed")


class SessionSafetyTests(VaultTestCase):
    def test_transcribe_refuses_metadata_outside_recordings_root(self) -> None:
        outside_dir = Path(tempfile.mkdtemp(prefix="stt-test-outside-"))
        try:
            wav_path = outside_dir / "mic.wav"
            write_wav(wav_path, 1.0)
            session = {
                "title": "Outside",
                "mode": "mic",
                "status": "stopped",
                "session_dir": str(outside_dir),
                "started_at": r.iso_now(),
                "started_at_human": r.now_human(),
                "audio_files": [str(wav_path)],
            }
            r.write_json(outside_dir / "metadata.json", session)
            args = self.transcribe_args(session=str(outside_dir))
            with self.assertRaises(SystemExit) as ctx:
                r.transcribe(args)
            self.assertIn("outside the recordings root", str(ctx.exception))
        finally:
            shutil.rmtree(outside_dir, ignore_errors=True)

    def test_transcribe_refuses_session_currently_recording(self) -> None:
        session, session_dir = self.make_session(status="recording")
        args = self.transcribe_args(session=str(session_dir))
        with self.assertRaises(SystemExit) as ctx:
            r.transcribe(args)
        self.assertIn("currently", str(ctx.exception))

    def test_transcribe_refuses_session_currently_transcribing(self) -> None:
        session, session_dir = self.make_session(status="transcribing")
        args = self.transcribe_args(session=str(session_dir))
        with self.assertRaises(SystemExit) as ctx:
            r.transcribe(args)
        self.assertIn("currently", str(ctx.exception))

    def test_transcribe_refuses_metadata_session_dir_mismatch(self) -> None:
        session, session_dir = self.make_session(status="stopped")
        tampered = dict(session)
        tampered["session_dir"] = str(session_dir.parent / "somewhere-else")
        r.write_json(session_dir / "metadata.json", tampered)
        args = self.transcribe_args(session=str(session_dir))
        with self.assertRaises(SystemExit) as ctx:
            r.transcribe(args)
        self.assertIn("does not match", str(ctx.exception))


class ChooseSessionTests(VaultTestCase):
    """`choose_session` (no explicit --session) must use the same validated
    canonical-transcript check as `list --pending`, not the stale
    metadata `transcript_path` field."""

    def test_skips_session_with_valid_canonical_pair_even_if_metadata_field_absent(self) -> None:
        # A valid pair exists on disk but the metadata has no transcript_path
        # field at all (stale/legacy metadata). The old field-based check
        # would have selected this already-transcribed session for
        # retranscription; the validated check must skip it.
        session, session_dir = self.make_session(status="transcribed", token="20260101-000001-valid-transcribed")
        (session_dir / "transcript.md").write_text("# Transcript\n\nhi\n", encoding="utf-8")
        (session_dir / "transcript.json").write_text(
            json.dumps({"segments": [{"text": "hi"}]}), encoding="utf-8"
        )
        session.pop("transcript_path", None)
        r.write_json(session_dir / "metadata.json", session)

        with self.assertRaises(SystemExit) as ctx:
            r.choose_session(self.vault, None)
        self.assertIn("No stopped untranscribed recording", str(ctx.exception))

    def test_picks_session_with_broken_transcript_despite_existing_metadata_field(self) -> None:
        # Metadata claims a transcript_path whose file exists, but the file
        # is malformed. The old field-based check skipped it forever (never
        # retranscribed); the validated check must pick it again.
        session, session_dir = self.make_session(status="transcribed", token="20260101-000002-broken-transcript")
        (session_dir / "transcript.md").write_text("# broken\n", encoding="utf-8")
        (session_dir / "transcript.json").write_text("{not json", encoding="utf-8")
        session["transcript_path"] = str(session_dir / "transcript.md")
        session["transcript_json_path"] = str(session_dir / "transcript.json")
        r.write_json(session_dir / "metadata.json", session)

        picked = r.choose_session(self.vault, None)
        self.assertEqual(Path(picked["session_dir"]), session_dir)


class ListPendingTests(VaultTestCase):
    def test_pending_uses_validated_final_artifact_not_just_metadata_field(self) -> None:
        """H: `list --pending` must check for a real, valid final transcript,
        not just a `transcript_path` metadata field that may point nowhere
        or to a malformed file."""
        session, session_dir = self.make_session(status="transcribed", token="20260101-000001-fake-transcribed")
        session["transcript_path"] = str(session_dir / "transcript.md")
        session["transcript_json_path"] = str(session_dir / "transcript.json")
        # transcript_path metadata is set, but no real files exist / are malformed.
        (session_dir / "transcript.json").write_text("{not json", encoding="utf-8")
        r.write_json(session_dir / "metadata.json", session)

        buf_text = run_capturing_stdout(r.list_recordings, argparse_namespace({"pending": True, "limit": 20}))
        payload = json.loads(buf_text)
        titles = [s["title"] for s in payload]
        self.assertIn(session["title"], titles, "session with a broken transcript must still show as pending")
        matching = [s for s in payload if s["session_dir"] == str(session_dir)][0]
        self.assertFalse(matching["has_transcript"])

    def test_pending_excludes_session_with_valid_final_transcript(self) -> None:
        session, session_dir = self.make_session(status="transcribed", token="20260101-000002-real-transcribed")
        (session_dir / "transcript.md").write_text("# Transcript\n\nhi\n", encoding="utf-8")
        (session_dir / "transcript.json").write_text(json.dumps({"segments": []}), encoding="utf-8")
        session["transcript_path"] = str(session_dir / "transcript.md")
        session["transcript_json_path"] = str(session_dir / "transcript.json")
        r.write_json(session_dir / "metadata.json", session)

        buf_text = run_capturing_stdout(r.list_recordings, argparse_namespace({"pending": True, "limit": 20}))
        payload = json.loads(buf_text)
        session_dirs = [s["session_dir"] for s in payload]
        self.assertNotIn(str(session_dir), session_dirs)


class RecorderKillSafetyTests(VaultTestCase):
    def test_identity_verification_fails_for_pgid_mismatch(self) -> None:
        # Spawn a real process WITHOUT start_new_session -- its pgid is the
        # test runner's group, not its own pid, unlike a genuine detached
        # `start_new_session=True` recorder.
        proc = self.spawn_process([sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            session = {
                "pid": proc.pid,
                "pgid": proc.pid,  # recorded as if it were its own group leader
                "session_dir": str(self.vault),
                "stt_bin": "/fake/stt",
            }
            self.assertFalse(r.recorder_identity_verified(session))
        finally:
            proc.kill()
            proc.wait()

    def test_stop_never_signals_unverified_process(self) -> None:
        proc = self.spawn_process([sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            session_dir = r.recordings_root(self.vault) / "20260101-000000-rec"
            session_dir.mkdir(parents=True, exist_ok=True)
            session = {
                "title": "Test",
                "mode": "mic",
                "status": "recording",
                "session_dir": str(session_dir),
                "pid": proc.pid,
                "pgid": proc.pid,
                "stt_bin": "/fake/stt",
                "started_at": r.iso_now(),
                "started_at_human": r.now_human(),
                "audio_files": [],
            }
            r.write_json(r.active_state_path(self.vault), session)
            r.write_json(session_dir / "metadata.json", session)

            killed = {"called": False}
            original_killpg = os.killpg

            def fake_killpg(*a, **k):
                killed["called"] = True
                return original_killpg(*a, **k)

            with mock.patch("os.killpg", side_effect=fake_killpg):
                args = argparse_namespace(dict(timeout=1.0, term_timeout=1.0))
                r.stop(args)

            self.assertFalse(killed["called"], "stop() must never signal a process it cannot verify")
            self.assertTrue(r.process_alive(proc.pid), "unverified process must be left alone, not killed")
        finally:
            proc.kill()
            proc.wait()

    def test_stop_signals_verified_recorder_process_group(self) -> None:
        # A genuine detached recorder: its own session/group leader (pgid ==
        # pid), with a command line that references both the recorded stt
        # binary and the expected session directory.
        session_dir = r.recordings_root(self.vault) / "20260101-000001-rec2"
        session_dir.mkdir(parents=True, exist_ok=True)
        script = self.make_sleep_script(30.0)
        stt_bin = str(script.resolve())
        proc = subprocess.Popen(
            [stt_bin, str(session_dir)],
            start_new_session=True,
        )
        self._spawned_pids.append(proc.pid)
        try:
            session = {
                "title": "Test",
                "mode": "mic",
                "status": "recording",
                "session_dir": str(session_dir),
                "pid": proc.pid,
                "pgid": proc.pid,
                "stt_bin": stt_bin,
                "started_at": r.iso_now(),
                "started_at_human": r.now_human(),
                "audio_files": [],
            }
            self.assertTrue(r.recorder_identity_verified(session))
            r.write_json(r.active_state_path(self.vault), session)
            r.write_json(session_dir / "metadata.json", session)

            args = argparse_namespace(dict(timeout=2.0, term_timeout=2.0))
            r.stop(args)
            self.assertFalse(r.process_alive(proc.pid), "verified recorder should have been signalled and stopped")
            metadata = r.load_json(session_dir / "metadata.json")
            self.assertEqual(metadata["status"], "stopped")
        finally:
            try:
                proc.kill()
                proc.wait()
            except OSError:
                pass

    def test_identity_fails_when_same_stt_bin_but_different_session(self) -> None:
        """A reused PID running the *same* stt binary for a *different*
        session must not be mistaken for this session's recorder: the AND
        (not OR) requirement means matching only the binary is insufficient.
        """
        script = self.make_sleep_script(30.0)
        stt_bin = str(script.resolve())
        actual_session_dir = r.recordings_root(self.vault) / "20260101-000002-other-session"
        actual_session_dir.mkdir(parents=True, exist_ok=True)
        recorded_session_dir = r.recordings_root(self.vault) / "20260101-000003-recorded-session"
        recorded_session_dir.mkdir(parents=True, exist_ok=True)

        # The live process's real command line references the *same* stt
        # binary, but a completely different session directory than the one
        # our bookkeeping expects.
        proc = subprocess.Popen(
            [stt_bin, str(actual_session_dir)],
            start_new_session=True,
        )
        self._spawned_pids.append(proc.pid)
        try:
            session = {
                "title": "Recorded session",
                "mode": "mic",
                "status": "recording",
                "session_dir": str(recorded_session_dir),
                "pid": proc.pid,
                "pgid": proc.pid,
                "stt_bin": stt_bin,
                "started_at": r.iso_now(),
                "started_at_human": r.now_human(),
                "audio_files": [],
            }
            self.assertFalse(
                r.recorder_identity_verified(session),
                "same stt_bin in cmdline must not be enough on its own; the expected session "
                "directory must also be present",
            )

            r.write_json(r.active_state_path(self.vault), session)
            r.write_json(recorded_session_dir / "metadata.json", session)

            killed = {"called": False}
            original_killpg = os.killpg

            def fake_killpg(*a, **k):
                killed["called"] = True
                return original_killpg(*a, **k)

            with mock.patch("os.killpg", side_effect=fake_killpg):
                args = argparse_namespace(dict(timeout=1.0, term_timeout=1.0))
                r.stop(args)

            self.assertFalse(
                killed["called"], "stop() must never signal a process matched only on stt_bin, not session"
            )
            self.assertTrue(r.process_alive(proc.pid), "unverified process must be left alone, not killed")
        finally:
            proc.kill()
            proc.wait()


class RunOwnershipGuardTests(VaultTestCase):
    """Run-transition ownership guard: a run_id may mutate session metadata
    or promote its staged outputs only if the active-transcription
    reservation is absent and on-disk metadata still names that run_id, or
    the reservation is owned by that run_id. A foreign active run (or
    metadata naming another run) must refuse mutation/promotion outright.
    """

    def _write_active_owned_by(self, session_dir: Path, run_id: str, pid: int = 999999) -> dict:
        payload = {
            "session_dir": str(session_dir),
            "pid": pid,
            "run_id": run_id,
            "command": ["fake"],
            "log_path": str(session_dir / "transcription.log"),
            "started_at": r.iso_now(),
            "requested_options": {},
            "resolved_timeout": 1800.0,
            "active": True,
        }
        r.write_json(r.active_transcription_state_path(self.vault), payload)
        return payload

    def test_late_run_cannot_promote_mutate_or_clear_foreign_active_state(self) -> None:
        """Run A finishing late, after Run B has already claimed the active
        reservation for the same session, must not be able to promote its
        stale staged output, overwrite session metadata, or clear Run B's
        active-transcription reservation.
        """
        session, session_dir = self.make_session(status="transcribing")
        session["transcription_run_id"] = "run-b"
        r.write_json(session_dir / "metadata.json", session)

        # Run B currently holds the active-transcription reservation.
        active_before = self._write_active_owned_by(session_dir, "run-b")
        metadata_before = r.load_json(session_dir / "metadata.json")

        # Run A is a stale, late-finishing attempt with its own staged output.
        run_a_id = "run-a"
        staging_dir_a = session_dir / ".stt-staging" / run_a_id
        staging_dir_a.mkdir(parents=True, exist_ok=True)
        (staging_dir_a / "transcript.md").write_text("# Run A\n\nstale content from run A\n", encoding="utf-8")
        (staging_dir_a / "transcript.json").write_text(json.dumps({"segments": []}), encoding="utf-8")

        session_for_a = dict(session)
        transcript_path = session_dir / "transcript.md"
        transcript_json_path = session_dir / "transcript.json"

        # 1) Cannot promote Run A's staged outputs.
        with self.assertRaises(r.ForeignRunOwnership):
            r.promote_run_outputs_if_owned(
                self.vault, session_for_a, run_a_id, staging_dir_a, session_dir, None
            )
        self.assertFalse(transcript_path.exists(), "run A's stale output must never be promoted")
        self.assertFalse(transcript_json_path.exists())

        # 2) Cannot finalize as transcribed.
        with self.assertRaises(r.ForeignRunOwnership):
            r.finalize_transcribed_session(
                session_for_a, self.vault, transcript_path, transcript_json_path,
                None, "audio-a", run_id=run_a_id,
            )

        # 3) Cannot mark failed.
        with self.assertRaises(r.ForeignRunOwnership):
            r.mark_transcription_failed(
                session_for_a, self.vault, None, "stale failure from run A", run_id=run_a_id,
            )

        # 4) Cannot mark interrupted.
        with self.assertRaises(r.ForeignRunOwnership):
            r.mark_transcription_interrupted(
                session_for_a, self.vault, None, run_id=run_a_id,
            )

        # Session metadata and Run B's active-transcription reservation must
        # be completely untouched by any of Run A's refused mutations.
        self.assertEqual(r.load_json(session_dir / "metadata.json"), metadata_before)
        active_path = r.active_transcription_state_path(self.vault)
        self.assertTrue(active_path.exists(), "run A must not clear run B's active-transcription reservation")
        self.assertEqual(r.load_json(active_path), active_before)
        self.assertFalse(transcript_path.exists())
        self.assertFalse(transcript_json_path.exists())

    def test_late_run_cannot_mutate_when_metadata_already_names_another_run(self) -> None:
        """Even with no active reservation at all, if on-disk metadata has
        already moved on to a later run_id (e.g. Run B already finalized and
        cleared its own reservation), Run A must still be refused.
        """
        session, session_dir = self.make_session(status="transcribed")
        session["transcription_run_id"] = "run-b"
        r.write_json(session_dir / "metadata.json", session)
        (session_dir / "transcript.md").write_text("# Run B\n\nfinal content from run B\n", encoding="utf-8")
        (session_dir / "transcript.json").write_text(json.dumps({"segments": []}), encoding="utf-8")
        metadata_before = r.load_json(session_dir / "metadata.json")
        md_before = (session_dir / "transcript.md").read_text(encoding="utf-8")

        self.assertFalse(r.active_transcription_state_path(self.vault).exists())

        run_a_id = "run-a"
        session_for_a = dict(session)
        with self.assertRaises(r.ForeignRunOwnership):
            r.mark_transcription_failed(
                session_for_a, self.vault, None, "stale failure from run A", run_id=run_a_id,
            )
        self.assertEqual(r.load_json(session_dir / "metadata.json"), metadata_before)
        self.assertEqual((session_dir / "transcript.md").read_text(encoding="utf-8"), md_before)

    def test_run_owns_session_when_active_state_matches_or_absent_with_matching_metadata(self) -> None:
        """Sanity check: the guard must not falsely refuse a run that still
        genuinely owns the session (either via the active reservation or,
        with no reservation at all, via matching on-disk metadata)."""
        session, session_dir = self.make_session(status="transcribing")
        session["transcription_run_id"] = "run-a"
        r.write_json(session_dir / "metadata.json", session)

        # Owned via the active reservation.
        self._write_active_owned_by(session_dir, "run-a")
        r.assert_run_owns_session(self.vault, session, "run-a")  # must not raise

        # Owned via matching on-disk metadata with no active reservation.
        r.active_transcription_state_path(self.vault).unlink()
        r.assert_run_owns_session(self.vault, session, "run-a")  # must not raise

        # Legacy run_id=None is always exempt.
        r.assert_run_owns_session(self.vault, session, None)  # must not raise


class BlockedSignalsContextTests(unittest.TestCase):
    """Direct, mocked-barrier tests of the `_blocked_signals` primitive used
    by both the transcription and recording launch protocols."""

    def test_signal_delivery_is_deferred_until_context_exit(self) -> None:
        if not hasattr(signal, "pthread_sigmask"):
            self.skipTest("pthread_sigmask not available on this platform")
        delivered: list[bool] = []
        old_handler = signal.signal(signal.SIGUSR1, lambda *_: delivered.append(True))
        try:
            with r._blocked_signals((signal.SIGUSR1,)) as blocked:
                self.assertTrue(blocked)
                os.kill(os.getpid(), signal.SIGUSR1)
                time.sleep(0.05)
                self.assertEqual(delivered, [], "signal must not be delivered while blocked")
            # The pending signal should be delivered promptly once unblocked.
            deadline = time.time() + 1.0
            while not delivered and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(delivered, [True], "signal must be delivered promptly after unblock")
        finally:
            signal.signal(signal.SIGUSR1, old_handler)
            signal.pthread_sigmask(signal.SIG_UNBLOCK, (signal.SIGUSR1,))

    def test_yields_false_when_pthread_sigmask_unavailable(self) -> None:
        if not hasattr(signal, "pthread_sigmask"):
            self.skipTest("already unavailable on this platform")
        original = signal.pthread_sigmask
        del signal.pthread_sigmask
        try:
            with r._blocked_signals((signal.SIGUSR1,)) as blocked:
                self.assertFalse(blocked, "must conservatively report no blocking when pthread_sigmask is gone")
        finally:
            signal.pthread_sigmask = original


class TranscriptionLaunchSignalBlockingTests(VaultTestCase):
    """Barrier test: a signal delivered mid-launch, while genuinely blocked,
    must be deferred until after PID/metadata persistence, and the catch
    path must then see a fully-tracked child (never `proc is None`)."""

    def test_signal_during_blocked_window_is_seen_after_pid_persisted(self) -> None:
        if not hasattr(signal, "pthread_sigmask"):
            self.skipTest("pthread_sigmask not available on this platform")
        session, session_dir = self.make_session()
        os.environ["FAKE_STT_SLEEP"] = "2.0"

        real_popen = subprocess.Popen
        signaled = threading.Event()

        def popen_and_self_signal(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            # Simulate a signal landing squarely inside the blocked launch
            # window: it must be deferred until this helper unblocks (right
            # after PID/metadata state is durable), not delivered here. Only
            # signal once -- `subprocess.run` (used internally by
            # `process_alive`/`process_command_line` for `ps` checks) also
            # goes through `Popen`, and those later calls happen *after*
            # signal handlers have already been restored to default, so
            # re-signaling there would kill this test process for real.
            if not signaled.is_set():
                signaled.set()
                os.kill(os.getpid(), signal.SIGTERM)
            return proc

        args = self.transcribe_args(session=str(session_dir))
        with mock.patch("subprocess.Popen", side_effect=popen_and_self_signal):
            out = run_capturing_stdout(r.transcribe, args)
        payload = json.loads(out)
        # The child was still alive (FAKE_STT_SLEEP=2.0) when the deferred
        # signal was finally delivered, so this must NOT be a terminal
        # interrupted state -- and, crucially, `proc` must not have been
        # None (which would have meant "nothing was launched").
        self.assertEqual(payload["status"], "transcribing")
        self.assertTrue(payload["helper_interrupted"])
        self.assertTrue(payload["child_alive"])
        self.assertTrue(payload["active"])
        self._spawned_pids.append(payload["pid"])

        # PID/metadata state persisted before the signal was delivered must
        # be intact and untouched (not cleared, not orphaned).
        active_path = r.active_transcription_state_path(self.vault)
        self.assertTrue(active_path.exists(), "active-transcription reservation must survive a post-unblock signal")
        active_payload = r.load_json(active_path)
        self.assertEqual(active_payload["pid"], payload["pid"])
        metadata = r.load_json(session_dir / "metadata.json")
        self.assertEqual(metadata["status"], "transcribing")
        self.assertEqual(metadata["transcription_pid"], payload["pid"])

    def test_no_pthread_sigmask_fallback_never_clears_reservation_on_proc_none(self) -> None:
        """Conservative fallback: with blocking unavailable, an interrupt
        that lands with `proc` still unassigned must leave the reservation
        in place rather than assuming nothing was launched."""
        session, session_dir = self.make_session()

        def popen_raises_transcription_interrupted(*args, **kwargs):
            raise r.TranscriptionInterrupted("synthetic mid-Popen interrupt")

        args = self.transcribe_args(session=str(session_dir))
        original_has_mask = hasattr(signal, "pthread_sigmask")
        original_mask = signal.pthread_sigmask if original_has_mask else None
        if original_has_mask:
            del signal.pthread_sigmask
        try:
            with mock.patch("subprocess.Popen", side_effect=popen_raises_transcription_interrupted):
                with self.assertRaises(SystemExit) as ctx:
                    r.transcribe(args)
            self.assertIn("reservation in place", str(ctx.exception))
        finally:
            if original_has_mask:
                signal.pthread_sigmask = original_mask

        # The reservation must still exist -- never cleared on speculation.
        self.assertTrue(r.active_transcription_state_path(self.vault).exists())


class RecordingLaunchSignalBlockingTests(VaultTestCase):
    """Recording launch (`start()`) uses the same blocked-signal launch
    protocol as transcription: pre-launch reservation, durable PID/PGID
    persistence, Popen-failure handling, and post-unblock signal safety."""

    def _start_args(self, **overrides):
        defaults = dict(title="Test recording", mode="mic", duration=None, wait=False, wait_timeout=None,
                        attendees=None, attendees_source="manual")
        defaults.update(overrides)
        return argparse_namespace(defaults)

    def test_prelaunch_reservation_written_before_popen_with_null_pid(self) -> None:
        real_popen = subprocess.Popen
        entered = threading.Event()
        release = threading.Event()

        def gated_popen(*args, **kwargs):
            entered.set()
            release.wait(timeout=5)
            return real_popen(*args, **kwargs)

        args = self._start_args(title="Gate test")
        result_holder: dict = {}

        def run() -> None:
            with mock.patch("subprocess.Popen", side_effect=gated_popen):
                result_holder["stdout"] = run_capturing_stdout(r.start, args)

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(entered.wait(timeout=5), "Popen was not called promptly")

        active_path = r.active_state_path(self.vault)
        seen = poll_until(lambda: active_path.exists(), timeout=2.0)
        self.assertTrue(seen, "pre-launch reservation was not written before Popen")
        payload = r.load_json(active_path)
        self.assertIsNone(payload.get("pid"))
        self.assertIsNone(payload.get("pgid"))
        self.assertIn("command", payload)
        self.assertIn("session_dir", payload)
        self.assertIn("log_path", payload)
        self.assertIn("recording_reservation_id", payload)

        metadata_path = Path(payload["session_dir"]) / "metadata.json"
        metadata = r.load_json(metadata_path)
        self.assertIsNone(metadata.get("pid"))
        self.assertIsNone(metadata.get("pgid"))

        release.set()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), "start() did not finish in time")

        final_payload = r.load_json(active_path)
        self.assertIsNotNone(final_payload.get("pid"))
        self.assertEqual(final_payload.get("pid"), final_payload.get("pgid"))
        self._spawned_pids.append(final_payload["pid"])

    def test_popen_failure_marks_recording_failed_and_clears_owned_reservation(self) -> None:
        args = self._start_args(title="Boom")
        with mock.patch("subprocess.Popen", side_effect=OSError("boom")):
            with self.assertRaises(SystemExit) as ctx:
                r.start(args)
        self.assertIn("Failed to launch stt binary", str(ctx.exception))

        self.assertFalse(
            r.active_state_path(self.vault).exists(),
            "the reservation this call itself created must be cleared on Popen failure",
        )
        sessions = r.load_sessions(self.vault)
        self.assertEqual(len(sessions), 1)
        session = sessions[0]
        self.assertEqual(session["status"], "recording_failed")
        self.assertIn("failure_reason", session)
        self.assertIn("failed_at", session)
        self.assertIsNone(session.get("pid"))

    def test_mark_recording_failed_clears_only_matching_reservation_id(self) -> None:
        session, session_dir = self.make_session(status="recording")
        session["recording_reservation_id"] = "res-mine"
        r.write_json(session_dir / "metadata.json", session)

        # A foreign reservation currently occupies the active-recording slot
        # (in principle, a different invocation's reservation).
        foreign = dict(session)
        foreign["recording_reservation_id"] = "res-foreign"
        r.write_json(r.active_state_path(self.vault), foreign)

        r.mark_recording_failed(session, self.vault, "res-mine", "simulated failure")

        self.assertTrue(r.active_state_path(self.vault).exists(), "a foreign reservation must not be cleared")
        self.assertEqual(
            r.load_json(r.active_state_path(self.vault)).get("recording_reservation_id"), "res-foreign"
        )
        metadata = r.load_json(session_dir / "metadata.json")
        self.assertEqual(metadata["status"], "recording_failed")
        self.assertEqual(metadata["failure_reason"], "simulated failure")

    def test_signal_during_blocked_launch_leaves_durable_pid_state(self) -> None:
        if not hasattr(signal, "pthread_sigmask"):
            self.skipTest("pthread_sigmask not available on this platform")
        real_popen = subprocess.Popen
        signaled = threading.Event()

        def popen_and_self_signal(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            # Only signal once -- later internal `subprocess.run` ("ps")
            # calls also go through `Popen` and happen after signal
            # handlers are restored to default, so re-signaling there would
            # kill this test process for real instead of exercising the
            # blocked-launch-window race.
            if not signaled.is_set():
                signaled.set()
                os.kill(os.getpid(), signal.SIGTERM)
            return proc

        args = self._start_args(title="Signal test")
        with mock.patch("subprocess.Popen", side_effect=popen_and_self_signal):
            out = run_capturing_stdout(r.start, args)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "recording_started")
        self.assertTrue(payload.get("helper_interrupted"))
        self._spawned_pids.append(payload["pid"])

        active_path = r.active_state_path(self.vault)
        self.assertTrue(active_path.exists())
        persisted = r.load_json(active_path)
        self.assertEqual(persisted["pid"], payload["pid"])
        self.assertEqual(persisted["pgid"], payload["pid"])
        metadata = r.load_json(Path(persisted["session_dir"]) / "metadata.json")
        self.assertEqual(metadata["status"], "recording")
        self.assertEqual(metadata["pid"], payload["pid"])

    def test_scheduled_start_writes_reservation_and_launches_after_wait(self) -> None:
        """start --delay writes a pid-null 'scheduled' reservation, waits
        for the start time (without launching), then launches and persists
        the real PID -- the launch flow itself is unchanged."""
        real_popen = subprocess.Popen
        entered = threading.Event()
        release = threading.Event()

        def gated_popen(*args, **kwargs):
            entered.set()
            release.wait(timeout=5)
            return real_popen(*args, **kwargs)

        args = self._start_args(title="Scheduled", delay=1.0)
        result_holder: dict = {}

        def run() -> None:
            with mock.patch("subprocess.Popen", side_effect=gated_popen):
                result_holder["stdout"] = run_capturing_stdout(r.start, args)

        thread = threading.Thread(target=run)
        thread.start()
        # The scheduled reservation must be on disk while still waiting,
        # with pid/pgid null and a future scheduled_start_at.
        active_path = r.active_state_path(self.vault)
        seen = poll_until(lambda: active_path.exists(), timeout=2.0)
        self.assertTrue(seen, "scheduled reservation was not written")
        payload = r.load_json(active_path)
        self.assertEqual(payload.get("status"), "scheduled")
        self.assertIsNone(payload.get("pid"))
        self.assertIsNone(payload.get("pgid"))
        self.assertIn("scheduled_start_at", payload)
        self.assertIn("scheduled_start_at_human", payload)
        self.assertIn("recording_reservation_id", payload)

        # Popen must NOT have been called yet (the helper is still waiting).
        self.assertFalse(entered.is_set(), "recorder launched before the scheduled start")

        # The wait is only 1s; the launch should proceed and gate at Popen.
        self.assertTrue(entered.wait(timeout=5), "Popen was not called after the wait")
        release.set()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), "start() did not finish in time")

        payloads = parse_json_stream(result_holder["stdout"])
        self.assertEqual(payloads[0]["status"], "scheduled")
        self.assertEqual(payloads[-1]["status"], "recording_started")

        final = r.load_json(active_path)
        self.assertEqual(final["status"], "recording")
        self.assertIsNotNone(final.get("pid"))
        self._spawned_pids.append(final["pid"])

    def test_scheduled_start_cancel_while_waiting(self) -> None:
        """A `stop` while the helper waits for a scheduled start cancels the
        schedule: the reservation is cleared, the session becomes
        scheduled_cancelled, and no recorder is launched."""
        real_popen = subprocess.Popen
        popen_called = threading.Event()

        def popen_should_not_be_called(*args, **kwargs):
            popen_called.set()
            return real_popen(*args, **kwargs)

        args = self._start_args(title="Scheduled", delay=30.0)
        result_holder: dict = {}

        def run() -> None:
            with mock.patch("subprocess.Popen", side_effect=popen_should_not_be_called):
                result_holder["stdout"] = run_capturing_stdout(r.start, args)

        thread = threading.Thread(target=run)
        thread.start()
        active_path = r.active_state_path(self.vault)
        seen = poll_until(lambda: active_path.exists(), timeout=2.0)
        self.assertTrue(seen, "scheduled reservation was not written")

        # Cancel via the same path `stop` uses (the active-state file is
        # unlinked and the session marked scheduled_cancelled).
        stop_args = argparse_namespace({})
        with mock.patch("subprocess.Popen", side_effect=popen_should_not_be_called):
            r.stop(stop_args)
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), "start() did not abort after stop")
        self.assertFalse(popen_called.is_set(), "recorder launched after the schedule was cancelled")

        payloads = parse_json_stream(result_holder["stdout"])
        self.assertEqual(payloads[0]["status"], "scheduled")
        self.assertEqual(payloads[-1]["status"], "scheduled_cancelled")

        self.assertFalse(active_path.exists(), "cancelled schedule must clear the active state")
        metadata_path = Path(payloads[0]["session_dir"]) / "metadata.json"
        metadata = r.load_json(metadata_path)
        self.assertEqual(metadata["status"], "scheduled_cancelled")
        self.assertIn("cancel_reason", metadata)

    def test_stale_scheduled_reservation_cancelled_on_reconcile(self) -> None:
        """A scheduled reservation whose start time has passed (waiting
        helper died, e.g. harness timeout) is cancelled by reconciliation so
        it stops blocking future work."""
        session, session_dir = self.make_session(status="scheduled")
        past = (datetime.now().astimezone() - timedelta(minutes=5)).isoformat(timespec="seconds")
        session["scheduled_start_at"] = past
        session["scheduled_start_at_human"] = "2026-01-01 00:00"
        session["recording_reservation_id"] = "res-scheduled"
        r.write_json(session_dir / "metadata.json", session)
        r.write_json(r.active_state_path(self.vault), session)

        reconciled = r.reconcile_active_recording(self.vault)
        self.assertIsNotNone(reconciled)
        self.assertFalse(reconciled.get("active"))
        self.assertEqual(reconciled.get("status"), "scheduled_cancelled")
        self.assertFalse(r.active_state_path(self.vault).exists())
        metadata = r.load_json(session_dir / "metadata.json")
        self.assertEqual(metadata["status"], "scheduled_cancelled")

    def test_future_scheduled_reservation_reported_active(self) -> None:
        """A scheduled reservation whose start time is still ahead is
        genuinely active: status reports it and a second start is refused."""
        session, session_dir = self.make_session(status="scheduled")
        future = (datetime.now().astimezone() + timedelta(minutes=5)).isoformat(timespec="seconds")
        session["scheduled_start_at"] = future
        session["scheduled_start_at_human"] = "2099-01-01 00:00"
        session["recording_reservation_id"] = "res-future"
        r.write_json(session_dir / "metadata.json", session)
        r.write_json(r.active_state_path(self.vault), session)

        reconciled = r.reconcile_active_recording(self.vault)
        self.assertIsNotNone(reconciled)
        self.assertTrue(reconciled.get("active"))
        self.assertEqual(reconciled.get("status"), "scheduled")
        self.assertIn("starts_in_seconds", reconciled)
        self.assertTrue(r.active_state_path(self.vault).exists())

        # A second start must refuse while the schedule is pending.
        args = self._start_args(title="Second")
        with self.assertRaises(SystemExit) as ctx:
            r.start(args)
        self.assertIn("scheduled", str(ctx.exception))

    def test_start_at_and_delay_are_mutually_exclusive(self) -> None:
        args = self._start_args(title="Both", start_at="12:00", delay=1.0)
        with self.assertRaises(SystemExit) as ctx:
            r.start(args)
        self.assertIn("only one of --start-at or --delay", str(ctx.exception))


class ExecUnblockedShimTests(unittest.TestCase):
    """Child-side half of the blocked-signal launch protocol: `Popen` is
    parent-blocked around launch (`_blocked_signals`), which a naive direct
    launch would hand down across fork+exec as a *permanently* blocked mask
    in the real child (POSIX signal masks survive `exec()`, unlike handler
    dispositions). `wrap_launch_command`/`exec_unblocked_shim` close that
    gap by re-invoking this script as a tiny shim that unblocks the mask
    and restores default dispositions *inside* the child, before `execv`ing
    the real target -- keeping the same PID throughout.
    """

    def test_final_child_reports_sigint_and_sigterm_unblocked(self) -> None:
        if not hasattr(signal, "pthread_sigmask"):
            self.skipTest("pthread_sigmask not available on this platform")
        script = (
            "import signal, sys\n"
            "mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])\n"
            "sys.stdout.write('sigint_blocked=%s sigterm_blocked=%s' % "
            "(signal.SIGINT in mask, signal.SIGTERM in mask))\n"
        )
        cmd = [sys.executable, "-c", script]
        launch_cmd = r.wrap_launch_command(cmd)
        self.assertEqual(launch_cmd[0], sys.executable)
        self.assertEqual(launch_cmd[2], "exec-unblocked")
        self.assertEqual(launch_cmd[3], "--")
        self.assertEqual(launch_cmd[4:], cmd)

        # Launch exactly like start()/transcribe() do: with this parent
        # process's own SIGINT/SIGTERM genuinely blocked across Popen.
        with r._blocked_signals() as blocked:
            self.assertTrue(blocked, "signal blocking must actually be in effect for this test to be meaningful")
            proc = subprocess.Popen(launch_cmd, stdout=subprocess.PIPE, text=True)
        out, _ = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("sigint_blocked=False", out)
        self.assertIn("sigterm_blocked=False", out)

    def test_recorder_like_child_exits_on_sigint_without_sigkill(self) -> None:
        """If practical: a detached, sleeping child launched through the
        shim while the parent's launch signals are blocked must still
        terminate on a plain SIGINT to its process group -- proving its own
        signal mask does *not* still have SIGINT blocked after exec."""
        if not hasattr(signal, "pthread_sigmask"):
            self.skipTest("pthread_sigmask not available on this platform")
        cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
        launch_cmd = r.wrap_launch_command(cmd)
        with r._blocked_signals() as blocked:
            self.assertTrue(blocked)
            proc = subprocess.Popen(launch_cmd, start_new_session=True)
        try:
            deadline = time.time() + 5.0
            # Give the shim a moment to unblock + execv before signalling.
            time.sleep(0.3)
            os.killpg(proc.pid, signal.SIGINT)
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self.fail("child did not exit on SIGINT alone; signal mask was not unblocked across exec")
            # -SIGINT (i.e. killed by SIGINT), never SIGKILL, was needed.
            self.assertEqual(proc.returncode, -signal.SIGINT)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_shim_requires_a_command_after_separator(self) -> None:
        with self.assertRaises(SystemExit):
            r.exec_unblocked_shim([])
        with self.assertRaises(SystemExit):
            r.exec_unblocked_shim(["--"])


class LaunchWrappingUsesShimTests(VaultTestCase):
    """Both `start()`'s recorder launch and `transcribe()`'s stt launch
    must actually pass a shim-wrapped command to `Popen`, while the
    persisted `command` metadata field keeps recording the real,
    unwrapped stt invocation (what identity/kill-safety checks and the
    user-facing report both rely on)."""

    def _start_args(self, **overrides):
        defaults = dict(title="Test recording", mode="mic", duration=None, wait=False, wait_timeout=None,
                        attendees=None, attendees_source="manual")
        defaults.update(overrides)
        return argparse_namespace(defaults)

    def test_start_launches_recorder_through_exec_unblocked_shim(self) -> None:
        captured: dict = {}
        real_popen = subprocess.Popen

        def capturing_popen(cmd, *a, **k):
            captured["cmd"] = cmd
            return real_popen(cmd, *a, **k)

        args = self._start_args(title="Shim test")
        with mock.patch("subprocess.Popen", side_effect=capturing_popen):
            out = run_capturing_stdout(r.start, args)
        payload = json.loads(out)
        self._spawned_pids.append(payload["pid"])

        launched = captured["cmd"]
        self.assertEqual(launched[0], sys.executable)
        self.assertEqual(Path(launched[1]).resolve(), (SCRIPTS_DIR / "recordings.py").resolve())
        self.assertEqual(launched[2], "exec-unblocked")
        self.assertEqual(launched[3], "--")
        # The real stt command (unwrapped) still starts with the fake stt
        # binary path, right after the `--` separator.
        self.assertEqual(launched[4], str(FAKE_STT))

        metadata = r.load_json(Path(payload["session_dir"]) / "metadata.json")
        self.assertEqual(metadata["command"][0], str(FAKE_STT))
        self.assertNotEqual(metadata["command"], launched)
        self.assertEqual(metadata["launched_command"], launched)

    def test_transcribe_launches_stt_through_exec_unblocked_shim(self) -> None:
        session, session_dir = self.make_session()
        os.environ["FAKE_STT_SLEEP"] = "0.4"
        captured: dict = {}
        real_popen = subprocess.Popen

        def capturing_popen(cmd, *a, **k):
            captured["cmd"] = cmd
            return real_popen(cmd, *a, **k)

        args = self.transcribe_args(session=str(session_dir))
        result_holder: dict = {}

        def run() -> None:
            with mock.patch("subprocess.Popen", side_effect=capturing_popen):
                result_holder["stdout"] = run_capturing_stdout(r.transcribe, args)

        thread = threading.Thread(target=run)
        thread.start()

        active_path = r.active_transcription_state_path(self.vault)
        seen_active = poll_until(lambda: active_path.exists() and r.load_json(active_path).get("pid") is not None)
        self.assertTrue(seen_active, "active-transcription state with a persisted PID was not created promptly")

        # Both the persisted active-transcription reservation and the
        # `Popen` call it was launched with must agree: the real, unwrapped
        # `command` is preserved for identity checks, while the shim-wrapped
        # `launched_command` is what was actually passed to `Popen`.
        active_payload = r.load_json(active_path)
        launched = captured["cmd"]
        self.assertEqual(launched, active_payload["launched_command"])
        self.assertEqual(active_payload["command"][0], str(FAKE_STT))
        self.assertNotEqual(active_payload["command"], launched)

        self.assertEqual(launched[0], sys.executable)
        self.assertEqual(Path(launched[1]).resolve(), (SCRIPTS_DIR / "recordings.py").resolve())
        self.assertEqual(launched[2], "exec-unblocked")
        self.assertEqual(launched[3], "--")
        self.assertEqual(launched[4], str(FAKE_STT))

        thread.join(timeout=10.0)
        self.assertFalse(thread.is_alive(), "transcribe() did not finish in time")
        payload = json.loads(result_holder["stdout"])
        self.assertEqual(payload["status"], "transcribed")
        metadata = r.load_json(session_dir / "metadata.json")
        self.assertEqual(metadata["status"], "transcribed")


class PromotionTransactionTests(VaultTestCase):
    """Journalled transactional multi-file canonical promotion: prepared
    temps + backups before any replace, fault-injected rollback, and
    crash-journal reconciliation on a later invocation."""

    def test_failure_between_md_and_json_rolls_back_both_destinations(self) -> None:
        session, session_dir = self.make_session()
        (session_dir / "transcript.md").write_text("# OLD MD\n", encoding="utf-8")
        (session_dir / "transcript.json").write_text(
            json.dumps({"segments": [{"text": "old"}]}), encoding="utf-8"
        )
        old_md = (session_dir / "transcript.md").read_text(encoding="utf-8")
        old_json = (session_dir / "transcript.json").read_text(encoding="utf-8")

        staging_dir = session_dir / ".stt-staging" / "run-fault"
        staging_dir.mkdir(parents=True, exist_ok=True)
        (staging_dir / "transcript.md").write_text("# NEW MD\n", encoding="utf-8")
        (staging_dir / "transcript.json").write_text(
            json.dumps({"segments": [{"text": "new"}]}), encoding="utf-8"
        )

        real_replace = os.replace

        def flaky_replace(src, dst, *a, **k):
            if str(dst).endswith("transcript.json") and not str(dst).endswith(".tmp"):
                raise OSError("simulated failure replacing transcript.json")
            return real_replace(src, dst, *a, **k)

        with mock.patch("os.replace", side_effect=flaky_replace):
            with self.assertRaises(OSError):
                r.promote_run_outputs(self.vault, staging_dir, session_dir, None)

        # Both old canonical files must remain, completely unchanged: the
        # already-replaced transcript.md must be rolled back too.
        self.assertEqual((session_dir / "transcript.md").read_text(encoding="utf-8"), old_md)
        self.assertEqual((session_dir / "transcript.json").read_text(encoding="utf-8"), old_json)
        self.assertFalse(r.promotion_journal_path(session_dir).exists())
        leftovers = [p.name for p in session_dir.iterdir() if p.name.startswith(".transcript")]
        self.assertEqual(leftovers, [])

    def test_failure_when_destination_did_not_exist_removes_newly_created_file(self) -> None:
        """A destination that did not exist before this promotion must be
        removed on rollback, not left behind as an orphaned new file."""
        session, session_dir = self.make_session()
        staging_dir = session_dir / ".stt-staging" / "run-fault-2"
        staging_dir.mkdir(parents=True, exist_ok=True)
        (staging_dir / "transcript.md").write_text("# NEW MD\n", encoding="utf-8")
        (staging_dir / "transcript.json").write_text(json.dumps({"segments": []}), encoding="utf-8")

        real_replace = os.replace

        def flaky_replace(src, dst, *a, **k):
            if str(dst).endswith("transcript.json") and not str(dst).endswith(".tmp"):
                raise OSError("simulated failure replacing transcript.json")
            return real_replace(src, dst, *a, **k)

        with mock.patch("os.replace", side_effect=flaky_replace):
            with self.assertRaises(OSError):
                r.promote_run_outputs(self.vault, staging_dir, session_dir, None)

        self.assertFalse((session_dir / "transcript.md").exists(), "newly-created destination must be removed on rollback")
        self.assertFalse((session_dir / "transcript.json").exists())
        self.assertFalse(r.promotion_journal_path(session_dir).exists())

    def test_successful_promotion_leaves_no_journal_or_backup_artifacts(self) -> None:
        session, session_dir = self.make_session()
        (session_dir / "transcript.md").write_text("# OLD MD\n", encoding="utf-8")
        (session_dir / "transcript.json").write_text(json.dumps({"segments": []}), encoding="utf-8")
        staging_dir = session_dir / ".stt-staging" / "run-ok"
        staging_dir.mkdir(parents=True, exist_ok=True)
        (staging_dir / "transcript.md").write_text("# NEW MD\n", encoding="utf-8")
        (staging_dir / "transcript.json").write_text(json.dumps({"segments": [{"text": "new"}]}), encoding="utf-8")

        r.promote_run_outputs(self.vault, staging_dir, session_dir, None)

        self.assertEqual((session_dir / "transcript.md").read_text(encoding="utf-8"), "# NEW MD\n")
        self.assertEqual(
            json.loads((session_dir / "transcript.json").read_text(encoding="utf-8")),
            {"segments": [{"text": "new"}]},
        )
        self.assertFalse(r.promotion_journal_path(session_dir).exists())
        leftovers = [p.name for p in session_dir.iterdir() if p.name.startswith(".transcript")]
        self.assertEqual(leftovers, [])


class PromotionJournalReconciliationTests(VaultTestCase):
    """Crash recovery: a leftover journal from a process that died
    mid-promotion (or between full replacement and cleanup) must be
    reconciled deterministically the next time any code path reads or
    finalizes this session's state."""

    def test_reconcile_rolls_back_partial_leftover_journal(self) -> None:
        session, session_dir = self.make_session()
        # Simulate a crash mid-promotion: transcript.md's replace already
        # happened (new content + a backup of the old content durably
        # recorded); transcript.json's replace never ran (still old content).
        backup_md = session_dir / ".transcript.md.promote-backup.bak"
        backup_md.write_text("# OLD MD\n", encoding="utf-8")
        (session_dir / "transcript.md").write_text("# NEW MD\n", encoding="utf-8")
        (session_dir / "transcript.json").write_text(
            json.dumps({"segments": [{"text": "old"}]}), encoding="utf-8"
        )

        journal = {
            "run_id": "crashed-run",
            "session_dir": str(session_dir),
            "entries": [
                {
                    "name": "transcript.md",
                    "dst": str(session_dir / "transcript.md"),
                    "temp": None,
                    "backup": str(backup_md),
                    "existed": True,
                    "replaced": True,
                },
                {
                    "name": "transcript.json",
                    "dst": str(session_dir / "transcript.json"),
                    "temp": str(session_dir / ".transcript.json.promote.tmp"),
                    "backup": None,
                    "existed": True,
                    "replaced": False,
                },
            ],
        }
        r.write_json(r.promotion_journal_path(session_dir), journal)

        r.reconcile_promotion_journal(self.vault, session_dir)

        self.assertEqual((session_dir / "transcript.md").read_text(encoding="utf-8"), "# OLD MD\n")
        self.assertEqual(
            json.loads((session_dir / "transcript.json").read_text(encoding="utf-8")),
            {"segments": [{"text": "old"}]},
        )
        self.assertFalse(r.promotion_journal_path(session_dir).exists())
        self.assertFalse(backup_md.exists())

    def test_reconcile_detects_replace_before_flag_write_by_digest(self) -> None:
        session, session_dir = self.make_session()
        backup_md = session_dir / ".transcript.md.promote-backup.bak"
        backup_md.write_text("# OLD MD\n", encoding="utf-8")
        new_md = session_dir / "transcript.md"
        new_md.write_text("# NEW MD\n", encoding="utf-8")
        old_json = session_dir / "transcript.json"
        old_json.write_text(json.dumps({"segments": [{"text": "old"}]}), encoding="utf-8")

        # Simulate a crash after os.replace(transcript.md) succeeded but
        # before the following journal update could set replaced=true.
        journal = {
            "run_id": "crash-before-flag",
            "session_dir": str(session_dir),
            "entries": [
                {
                    "name": "transcript.md",
                    "dst": str(new_md),
                    "temp": None,
                    "backup": str(backup_md),
                    "existed": True,
                    "new_sha256": r._file_sha256(new_md),
                    "replaced": False,
                },
                {
                    "name": "transcript.json",
                    "dst": str(old_json),
                    "temp": None,
                    "backup": None,
                    "existed": True,
                    "new_sha256": "not-the-old-json-digest",
                    "replaced": False,
                },
            ],
        }
        r.write_json(r.promotion_journal_path(session_dir), journal)

        r.reconcile_promotion_journal(self.vault, session_dir)

        self.assertEqual(new_md.read_text(encoding="utf-8"), "# OLD MD\n")
        self.assertEqual(json.loads(old_json.read_text(encoding="utf-8")), {"segments": [{"text": "old"}]})
        self.assertFalse(r.promotion_journal_path(session_dir).exists())

    def test_reconcile_keeps_fully_applied_promotion_and_just_cleans_up(self) -> None:
        session, session_dir = self.make_session()
        backup_md = session_dir / ".transcript.md.promote-backup.bak"
        backup_md.write_text("# OLD MD\n", encoding="utf-8")
        (session_dir / "transcript.md").write_text("# NEW MD\n", encoding="utf-8")
        (session_dir / "transcript.json").write_text(json.dumps({"segments": []}), encoding="utf-8")

        journal = {
            "run_id": "crashed-run-2",
            "session_dir": str(session_dir),
            "entries": [
                {
                    "name": "transcript.md",
                    "dst": str(session_dir / "transcript.md"),
                    "temp": None,
                    "backup": str(backup_md),
                    "existed": True,
                    "replaced": True,
                },
                {
                    "name": "transcript.json",
                    "dst": str(session_dir / "transcript.json"),
                    "temp": None,
                    "backup": None,
                    "existed": False,
                    "replaced": True,
                },
            ],
        }
        r.write_json(r.promotion_journal_path(session_dir), journal)

        r.reconcile_promotion_journal(self.vault, session_dir)

        # Fully-applied promotion: new content is kept, not rolled back.
        self.assertEqual((session_dir / "transcript.md").read_text(encoding="utf-8"), "# NEW MD\n")
        self.assertFalse(r.promotion_journal_path(session_dir).exists())
        self.assertFalse(backup_md.exists())

    def test_load_sessions_reconciles_leftover_journal_before_reading(self) -> None:
        """A later helper invocation (e.g. `list`) must reconcile a leftover
        promotion journal before reading session state."""
        session, session_dir = self.make_session(status="transcribed")
        backup_md = session_dir / ".transcript.md.promote-backup.bak"
        backup_md.write_text("# OLD MD\n", encoding="utf-8")
        (session_dir / "transcript.md").write_text("# NEW MD (partial)\n", encoding="utf-8")
        (session_dir / "transcript.json").write_text(json.dumps({"segments": []}), encoding="utf-8")

        journal = {
            "run_id": "crashed-run-3",
            "session_dir": str(session_dir),
            "entries": [
                {
                    "name": "transcript.md",
                    "dst": str(session_dir / "transcript.md"),
                    "temp": None,
                    "backup": str(backup_md),
                    "existed": True,
                    "replaced": True,
                },
                {
                    "name": "transcript.json",
                    "dst": str(session_dir / "transcript.json"),
                    "temp": None,
                    "backup": None,
                    "existed": True,
                    "replaced": False,
                },
            ],
        }
        r.write_json(r.promotion_journal_path(session_dir), journal)

        r.load_sessions(self.vault)

        self.assertEqual((session_dir / "transcript.md").read_text(encoding="utf-8"), "# OLD MD\n")
        self.assertFalse(r.promotion_journal_path(session_dir).exists())

    def test_reconcile_active_transcription_reconciles_leftover_journal_first(self) -> None:
        """`reconcile_active_transcription` reads this session's metadata as
        part of its own decision-making, so a leftover journal must be
        settled before it does."""
        session, session_dir = self.make_session(status="transcribing")
        backup_md = session_dir / ".transcript.md.promote-backup.bak"
        backup_md.write_text("# OLD MD\n", encoding="utf-8")
        (session_dir / "transcript.md").write_text("# NEW MD (partial)\n", encoding="utf-8")
        (session_dir / "transcript.json").write_text(json.dumps({"segments": [{"text": "old"}]}), encoding="utf-8")

        journal = {
            "run_id": "crashed-run-4",
            "session_dir": str(session_dir),
            "entries": [
                {
                    "name": "transcript.md",
                    "dst": str(session_dir / "transcript.md"),
                    "temp": None,
                    "backup": str(backup_md),
                    "existed": True,
                    "replaced": True,
                },
                {
                    "name": "transcript.json",
                    "dst": str(session_dir / "transcript.json"),
                    "temp": None,
                    "backup": None,
                    "existed": True,
                    "replaced": False,
                },
            ],
        }
        r.write_json(r.promotion_journal_path(session_dir), journal)

        dead_pid = self.spawn_dead_pid(marker=str(session_dir))
        r.write_json(
            r.active_transcription_state_path(self.vault),
            {
                "session_dir": str(session_dir),
                "pid": dead_pid,
                "command": ["irrelevant"],
                "log_path": str(session_dir / "transcription.log"),
                "started_at": r.iso_now(),
                "requested_options": {},
                "resolved_timeout": 1800.0,
                "active": True,
            },
        )

        r.reconcile_active_transcription(self.vault)

        self.assertEqual((session_dir / "transcript.md").read_text(encoding="utf-8"), "# OLD MD\n")
        self.assertFalse(r.promotion_journal_path(session_dir).exists())

    def test_reconcile_warns_and_leaves_unreadable_journal_in_place(self) -> None:
        """An unreadable journal cannot be safely reconstructed; reconciliation
        warns and leaves it on disk for manual inspection instead of guessing."""
        session, session_dir = self.make_session()
        journal_path = r.promotion_journal_path(session_dir)
        journal_path.write_text("{corrupt, not valid json", encoding="utf-8")

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            r.reconcile_promotion_journal(self.vault, session_dir)

        self.assertTrue(journal_path.exists())
        self.assertEqual(journal_path.read_text(encoding="utf-8"), "{corrupt, not valid json")
        self.assertIn("unreadable", buf.getvalue())

    def test_promotion_refused_while_unreadable_journal_remains(self) -> None:
        """A new promotion must refuse (and never overwrite) an unreadable
        leftover journal: the session's canonical state is uncertain, and
        clobbering the journal would destroy the only evidence."""
        session, session_dir = self.make_session()
        journal_path = r.promotion_journal_path(session_dir)
        journal_path.write_text("{corrupt, not valid json", encoding="utf-8")

        staging_dir = session_dir / ".stt-staging" / "run-new"
        staging_dir.mkdir(parents=True, exist_ok=True)
        (staging_dir / "transcript.md").write_text("# NEW MD\n", encoding="utf-8")
        (staging_dir / "transcript.json").write_text(
            json.dumps({"segments": [{"text": "new"}]}), encoding="utf-8"
        )

        with self.assertRaises(SystemExit) as ctx:
            r.promote_run_outputs(self.vault, staging_dir, session_dir, None)
        self.assertIn("unreadable", str(ctx.exception))

        # Journal preserved verbatim; no destination was touched.
        self.assertTrue(journal_path.exists())
        self.assertEqual(journal_path.read_text(encoding="utf-8"), "{corrupt, not valid json")
        self.assertFalse((session_dir / "transcript.md").exists())
        self.assertFalse((session_dir / "transcript.json").exists())


class AttendeeCaptureTests(VaultTestCase):
    """The calendar-derived invite list is captured with the session as the
    expected-speaker whitelist for later diarisation/identification."""

    def _start_args(self, **overrides):
        defaults = dict(title="Test recording", mode="mic", duration=None, wait=False, wait_timeout=None,
                        attendees=None, attendees_source="manual")
        defaults.update(overrides)
        return argparse_namespace(defaults)

    def test_parse_attendees_formats(self) -> None:
        parsed = r.parse_attendees(
            "Sam Rivera <sam.rivera@example.com>, alex.chen@example.com, Dana Vega"
        )
        self.assertEqual(parsed, [
            {"name": "Sam Rivera", "address": "sam.rivera@example.com"},
            {"name": "", "address": "alex.chen@example.com"},
            {"name": "Dana Vega", "address": ""},
        ])

    def test_parse_attendees_none_or_blank(self) -> None:
        self.assertEqual(r.parse_attendees(None), [])
        self.assertEqual(r.parse_attendees("  , , "), [])

    def test_parse_attendees_commas_inside_brackets_are_not_separators(self) -> None:
        # Exchange "Lastname, First <email>" display names contain commas.
        parsed = r.parse_attendees(
            "WU, Park <morgan.park@example.com>, Taylor Kim <taylor.kim@example.com>, jordan.lee@example.com"
        )
        self.assertEqual(parsed, [
            {"name": "WU, Park", "address": "morgan.park@example.com"},
            {"name": "Taylor Kim", "address": "taylor.kim@example.com"},
            {"name": "", "address": "jordan.lee@example.com"},
        ])

    def test_start_persists_attendees_and_source(self) -> None:
        captured: dict = {}
        real_popen = subprocess.Popen

        def capturing_popen(cmd, *a, **k):
            captured["cmd"] = cmd
            return real_popen(cmd, *a, **k)

        args = self._start_args(
            title="Example Bank Vault cadence",
            attendees="Alice <alice@example.com>, bob@example.com, Charlie",
            attendees_source="calendar",
        )
        with mock.patch("subprocess.Popen", side_effect=capturing_popen):
            out = run_capturing_stdout(r.start, args)
        payload = json.loads(out)
        self._spawned_pids.append(payload["pid"])

        metadata = r.load_json(Path(payload["session_dir"]) / "metadata.json")
        self.assertEqual(metadata["attendees_source"], "calendar")
        self.assertEqual(metadata["attendees"], [
            {"name": "Alice", "address": "alice@example.com"},
            {"name": "", "address": "bob@example.com"},
            {"name": "Charlie", "address": ""},
        ])

        # The session note exposes the whitelist for later diarisation work.
        note = (Path(payload["session_dir"]) / "session.md").read_text(encoding="utf-8")
        self.assertIn("## Attendees (expected speakers)", note)
        self.assertIn("Alice <alice@example.com>", note)
        self.assertIn("Source: `calendar`", note)

    def test_start_without_attendees_notes_missing_list(self) -> None:
        captured: dict = {}
        real_popen = subprocess.Popen

        def capturing_popen(cmd, *a, **k):
            captured["cmd"] = cmd
            return real_popen(cmd, *a, **k)

        args = self._start_args(title="No list")
        with mock.patch("subprocess.Popen", side_effect=capturing_popen):
            out = run_capturing_stdout(r.start, args)
        payload = json.loads(out)
        self._spawned_pids.append(payload["pid"])

        metadata = r.load_json(Path(payload["session_dir"]) / "metadata.json")
        self.assertNotIn("attendees", metadata)
        note = (Path(payload["session_dir"]) / "session.md").read_text(encoding="utf-8")
        self.assertIn("No attendee list captured", note)


if __name__ == "__main__":
    unittest.main()
