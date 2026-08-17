#!/usr/bin/env python3
"""Manage stt meeting recordings inside an Obsidian vault.

This script intentionally separates recording and transcription:
- `start` launches `stt record` in the background and records no transcript. It also supports a scheduled
  start (`--start-at`/`--delay`): the session and a pid-null reservation are created immediately
  (status `scheduled`, visible via `status` and on the board), the helper waits for the start time
  without holding the vault lock, then launches the recorder. The schedule can be cancelled with
  `stop` or SIGINT/SIGTERM while waiting.
- `transcribe` only runs later when explicitly requested.

Reliability design notes (see SKILL.md for the user-facing summary):

- Every `transcribe` attempt gets a fresh UUID `run_id` and a run-specific
  staging directory under `<session>/.stt-staging/<run_id>/`. The `stt`
  child writes its final transcript and per-track artifacts into that
  staging directory, never directly into the session root. Only after this
  run's own staged outputs are validated are they promoted into the session
  root as the canonical `transcript.md` / `transcript.json` (+ per-source
  artifacts) using a journalled transactional promotion with rollback and
  crash recovery (see `promote_run_outputs`/`reconcile_promotion_journal`):
  every destination's new content and a backup of any pre-existing content
  are staged and journalled *before* anything is replaced, the whole set is
  then replaced and the journal cleared on success, a caught failure rolls
  every already-replaced destination back, and a journal left behind by a
  crash is reconciled (rolled back, or safely completed if every
  destination was already replaced) before any later invocation reads or
  finalizes that session's state. This is a multi-file transaction with
  rollback/recovery, not true filesystem multi-file atomicity -- there is
  no OS primitive that replaces several independent files as one atomic
  operation -- but it guarantees the canonical set is never left showing a
  mix of old and new files. This also prevents a new attempt from ever
  being mistaken for successful based on an old canonical artifact left
  over from a previous run.
- A vault-scoped advisory lock (`fcntl.flock` on a lock file under the
  recordings root) serializes the check-then-launch-then-reserve sequence
  and terminal state transitions for `start`/`transcribe`/`status`/
  `list`/`stop`, so two concurrent invocations cannot both "see no active
  work" and both launch, and one run's finalize/interrupt cannot clear
  another run's active-state reservation. The lock is held only around
  these short state transitions, never around the long transcription wait.
- The active-transcription reservation is written *before* `Popen`, then
  atomically updated with the child PID immediately after `Popen` returns,
  with SIGINT/SIGTERM converted into a catchable exception across that
  whole window so an interrupt between launch and PID-persistence cannot
  orphan an untracked child.
- Blocking this parent's own SIGINT/SIGTERM around `Popen` only stops this
  process from being interrupted mid-launch; a *blocked* signal mask
  (unlike an installed handler) survives `exec()`, so launching the real
  `stt` binary directly would hand it that same blocked mask, leaving the
  real recorder/transcription child permanently unable to receive the very
  signals `stop()` sends its process group. Both `start()` and
  `transcribe()` route their `Popen` call through a hidden
  `exec-unblocked` shim (`wrap_launch_command`/`exec_unblocked_shim`): the
  immediate child re-invokes this script, which unblocks SIGINT/SIGTERM
  and restores their default disposition, then `os.execv`s the real target
  in its own place -- same PID, real signal handling restored, and every
  PID-based identity/liveness check elsewhere is unaffected.

- Recording kill-safety (`stop`) verifies process identity (PGID == PID,
  command line references the expected session) immediately before every
  `os.killpg` call and fails closed (never signals) if identity cannot be
  confirmed.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_VAULT = Path("~/obsidian-notes")
STT_REPO = Path(__file__).resolve().parents[4]
DEFAULT_MODEL = "mlx-community/VibeVoice-ASR-8bit"

LISTS = ["Recording", "To Transcribe", "Transcribed", "Failed / Needs Attention"]

# Minimum time (seconds) between two size checks of an input audio file used
# to detect "still growing" (still-recording) audio before transcription.
AUDIO_GROWTH_CHECK_SECONDS = float(os.environ.get("RECORDINGS_GROWTH_CHECK_SECONDS", "0.15"))

# How long past a scheduled recording's start time a pending (not-yet-launched)
# reservation stays "active" before reconciliation treats it as a dead schedule
# (scheduler process died before launching) and cancels it. Covers clock/sleep
# skew where the launcher is about to launch under the vault lock.
SCHEDULED_START_GRACE_SECONDS = float(os.environ.get("RECORDINGS_SCHEDULED_START_GRACE_SECONDS", "90"))


def now_token() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def now_human() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def iso_now() -> str:
    # Microsecond precision (not just seconds) so that ordering comparisons
    # against file mtimes (e.g. legacy-recovery "canonical output newer than
    # attempt start") remain correct even when both events happen within the
    # same wall-clock second.
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = value.strip("-")
    return value[:80] or "untitled-recording"


def vault_path() -> Path:
    return Path(os.environ.get("OBSIDIAN_VAULT", str(DEFAULT_VAULT))).expanduser().resolve()


def recordings_root(vault: Path) -> Path:
    return vault / "media" / "meeting-recordings"


def board_path(vault: Path) -> Path:
    return vault / "meeting recordings.md"


def active_state_path(vault: Path) -> Path:
    return recordings_root(vault) / ".active_recording.json"


def active_transcription_state_path(vault: Path) -> Path:
    return recordings_root(vault) / ".active_transcription.json"


def vault_lock_path(vault: Path) -> Path:
    return recordings_root(vault) / ".recordings.lock"


# ---------------------------------------------------------------------------
# Vault-scoped advisory locking
# ---------------------------------------------------------------------------

_lock_local = threading.local()


class VaultLock:
    """Vault-scoped advisory lock (fcntl.flock) around state transitions.

    Serializes check-then-launch-then-reserve sequences and terminal state
    transitions across concurrent invocations of this script (start,
    transcribe, status, list, stop) so two processes cannot both observe "no
    active work" and both launch, and one run cannot clear/finalize another
    run's active-state reservation out from under it.

    Reentrant *within a single thread* (tracked via thread-local state) so
    that a function already holding the lock can call another helper that
    also acquires it (e.g. `transcribe()` calling `reconcile_active_transcription()`)
    without deadlocking itself. Different threads/processes still serialize
    against each other via the real OS-level flock.

    The lock is only ever held around short state-transition sections; the
    multi-hour `stt` child wait in `transcribe()` happens with the lock
    released.
    """

    def __init__(self, vault: Path):
        self.vault = vault
        self.path = vault_lock_path(vault)
        self._fh = None
        self._acquired_here = False

    def __enter__(self) -> "VaultLock":
        held = getattr(_lock_local, "held", None)
        if held is None:
            held = set()
            _lock_local.held = held
        key = str(self.path)
        if key in held:
            self._acquired_here = False
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        held.add(key)
        self._acquired_here = True
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._acquired_here:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                held = getattr(_lock_local, "held", None)
                if held is not None:
                    held.discard(str(self.path))
        return False


def find_stt_bin() -> Path:
    env = os.environ.get("STT_BIN")
    candidates = []
    if env:
        candidates.append(Path(env).expanduser())
    candidates.extend(
        [
            STT_REPO / "dist" / "stt.app" / "Contents" / "MacOS" / "stt",
            STT_REPO / ".build" / "debug" / "stt",
        ]
    )
    path_bin = shutil.which("stt")
    if path_bin:
        candidates.append(Path(path_bin))
    for candidate in candidates:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    raise SystemExit(
        "Could not find stt binary. Set STT_BIN or build <stt-cli-repo>."
    )


def runtime_backend_path() -> Path | None:
    """Return the runtime venv directory (speechbrain/torch) if it exists.

    Diarisation and identification need the runtime venv (Python 3.11 +
    speechbrain + torch + scipy); the ASR venv at python/.venv is MLX-only.
    Returns the directory (e.g. runtime/) or None if not found.
    """
    candidate = STT_REPO / "runtime"
    if (candidate / ".venv").exists():
        return candidate
    return None


def speaker_profiles_exist() -> bool:
    """Check whether any speaker profiles are enrolled.

    The default profiles directory is ~/Library/Application Support/stt/speakers.
    Profiles live under <dir>/profiles/*.json (excluding index.json).
    """
    profiles_dir = Path.home() / "Library" / "Application Support" / "stt" / "speakers" / "profiles"
    if not profiles_dir.exists():
        return False
    return any(
        p.suffix == ".json" and p.name != "index.json"
        for p in profiles_dir.iterdir()
    )

def relative_to_vault(path: Path, vault: Path) -> str:
    return str(path.resolve().relative_to(vault.resolve())).replace(os.sep, "/")


def lane_for_status(status: str) -> str:
    mapping = {
        "recording": "Recording",
        "scheduled": "Recording",
        "stopped": "To Transcribe",
        "transcribing": "To Transcribe",
        "transcribed": "Transcribed",
        "transcription_failed": "Failed / Needs Attention",
        "transcription_interrupted": "Failed / Needs Attention",
        "recording_failed": "Failed / Needs Attention",
        "scheduled_cancelled": "Failed / Needs Attention",
    }
    return mapping.get(status, "To Transcribe")


def session_wikilink(session_dir: Path, vault: Path, title: str) -> str:
    note = session_dir / "session.md"
    rel = relative_to_vault(note.with_suffix(""), vault)
    safe_title = title.replace("|", "-").replace("]]", "] ]")
    return f"[[{rel}|{safe_title}]]"


def card_status_text(session: dict[str, Any]) -> str:
    status = session.get("status", "")
    if status == "transcribed":
        return f"transcribed {session.get('transcribed_at_human') or session.get('transcribed_at') or ''}"
    if status == "transcription_failed":
        when = session.get("failed_at_human") or session.get("failed_at") or session.get("ended_at_human") or ""
        reason = session.get("failure_reason", "")
        suffix = f" — {reason}" if reason else ""
        return f"failed {when}{suffix}"
    if status == "recording_failed":
        when = session.get("failed_at_human") or session.get("failed_at") or ""
        reason = session.get("failure_reason", "")
        suffix = f" — {reason}" if reason else ""
        return f"recording failed to launch {when}{suffix}"
    if status == "transcription_interrupted":
        when = session.get("interrupted_at_human") or session.get("interrupted_at") or session.get("ended_at_human") or ""
        return f"interrupted {when} — resume with transcribe --resume"
    if status == "transcribing":
        return f"transcribing (PID {session.get('transcription_pid', '?')}) since {session.get('transcription_started_at', '')}"
    if status == "stopped":
        return f"stopped {session.get('ended_at_human') or session.get('ended_at') or ''}"
    if status == "recording":
        return f"started {session.get('started_at_human') or session.get('started_at') or ''}"
    if status == "scheduled":
        return f"scheduled for {session.get('scheduled_start_at_human') or session.get('scheduled_start_at') or ''}"
    if status == "scheduled_cancelled":
        when = session.get("cancelled_at_human") or session.get("cancelled_at") or ""
        reason = session.get("cancel_reason", "")
        suffix = f" — {reason}" if reason else ""
        return f"scheduled start cancelled {when}{suffix}"
    return status


def make_card(session: dict[str, Any], vault: Path) -> str:
    session_dir = Path(session["session_dir"])
    link = session_wikilink(session_dir, vault, session.get("title", "Untitled"))
    mode = session.get("mode", "unknown")
    return f"- [ ] {link} — {card_status_text(session)} — mode {mode}"


def rebuild_board(vault: Path) -> None:
    """Reconstruct the Obsidian Kanban board from all session metadata.

    Rebuilding the whole board every time avoids fragile line-based edits that
    broke Markdown spacing (cards butted against the next lane header, which
    Obsidian's Kanban plugin could not parse).
    """
    buckets: dict[str, list[dict[str, Any]]] = {name: [] for name in LISTS}
    for s in load_sessions(vault):
        buckets.setdefault(lane_for_status(s.get("status", "")), []).append(s)

    lines = ["---", "kanban-plugin: board", "---", ""]
    for name in LISTS:
        lines.append(f"## {name}")
        lines.append("")
        for s in sorted(buckets.get(name, []), key=lambda x: x.get("started_at", ""), reverse=True):
            lines.append(make_card(s, vault))
            lines.append("")
    lines.append("%% kanban:settings")
    lines.append("```")
    lines.append(json.dumps({"kanban-plugin": "board", "list-collapse": [False] * len(LISTS)}))
    lines.append("```")
    lines.append("%%")
    board_path(vault).write_text("\n".join(lines) + "\n", encoding="utf-8")


def ensure_board(vault: Path) -> None:
    rebuild_board(vault)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _fsync_dir(path: Path) -> None:
    """Best-effort fsync of a directory so a just-performed rename/unlink
    in it is durable across power loss.

    The file-level fsync in `write_json`/`_copy_bytes_to_sibling_temp`
    makes *content* durable, but the directory entry created by
    `os.replace`/`unlink` lives in the directory itself; without fsyncing
    it, a power loss can silently drop the rename even though the file's
    own bytes were durable. Directory fsync is not supported on every
    platform/filesystem, so failures are swallowed -- the content-level
    fsync + atomic rename is still the dominant durability guarantee, and
    this only strengthens the "machine killed mid-promotion" recovery.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically: write to a sibling temp file, fsync, then rename.

    Avoids leaving truncated/partial JSON behind if the process is killed
    mid-write (important for active-state files that other invocations read
    to decide whether work is already running). The rename itself is also
    made durable via a directory fsync.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _copy_bytes_to_sibling_temp(dst: Path, src: Path, tag: str) -> Path:
    """Copy `src`'s bytes into a fresh temp file next to `dst`, fsynced.

    Never touches `dst` itself. Used both to stage a destination's new
    content and to snapshot its current (pre-promotion) content as a
    backup, so both are durable on disk before any destination is actually
    replaced.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dst.name}.{tag}.", suffix=".tmp", dir=str(dst.parent))
    try:
        with os.fdopen(fd, "wb") as out_fh, src.open("rb") as in_fh:
            shutil.copyfileobj(in_fh, out_fh)
            out_fh.flush()
            os.fsync(out_fh.fileno())
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return Path(tmp_name)


def wav_duration_seconds(path: Path) -> float | None:
    """Best-effort WAV duration in seconds, or None if it cannot be read."""
    try:
        with wave.open(str(path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate <= 0:
                return None
            return frames / float(rate)
    except (wave.Error, OSError, EOFError):
        return None


def gather_audio_durations(track_pair: tuple[Path, Path] | None, audio: Path | None) -> list[float]:
    paths: list[Path] = list(track_pair) if track_pair is not None else ([audio] if audio is not None else [])
    durations = []
    for p in paths:
        d = wav_duration_seconds(Path(p))
        if d is not None:
            durations.append(d)
    return durations


def resolve_transcription_timeout(explicit: float | None, durations: list[float]) -> float:
    """Resolve the per-track transcription timeout.

    An explicit value always wins. Otherwise adapt to the longest input WAV:
    max(1800, 4 * longest_duration + 300), falling back to 1800 if no
    duration could be determined.
    """
    if explicit is not None:
        return float(explicit)
    if not durations:
        return 1800.0
    longest = max(durations)
    return max(1800.0, 4.0 * longest + 300.0)


def audio_fingerprint(path: Path) -> dict[str, Any]:
    st = path.stat()
    return {"path": str(path), "size": st.st_size, "mtime": st.st_mtime}


def assert_audio_not_growing(paths: list[Path], settle_seconds: float | None = None) -> list[dict[str, Any]]:
    """Refuse to transcribe audio that appears to still be growing.

    Best-effort guard against transcribing a file that a recorder is still
    actively writing to (e.g. a stale/incorrectly-reconciled "recording"
    status, or a race with `stop`): stat each input file, wait a short
    settle interval, then stat again. If any file's size changed, refuse
    rather than risk transcribing a truncated snapshot.
    """
    wait = AUDIO_GROWTH_CHECK_SECONDS if settle_seconds is None else settle_seconds
    before = [audio_fingerprint(p) for p in paths]
    if wait > 0:
        time.sleep(wait)
    after = [audio_fingerprint(p) for p in paths]
    for b, a in zip(before, after):
        if b["size"] != a["size"]:
            raise SystemExit(
                f"Audio file {b['path']} changed size between checks ({b['size']} -> {a['size']} bytes); "
                "it looks like it is still being written (a recording may still be in progress). "
                "Refusing to transcribe a growing/incomplete file."
            )
    return after


def validate_transcript_json_shape(data: Any) -> bool:
    """True if `data` has the expected transcript JSON shape.

    Accepted shapes: a dict with a `segments` list, or (for callers that
    genuinely produce a bare top-level list of segments) a list.
    """
    if isinstance(data, dict):
        return isinstance(data.get("segments"), list)
    if isinstance(data, list):
        return True
    return False


def per_track_artifact_names(track_pair: tuple[Path, Path] | None) -> list[str]:
    if track_pair is None:
        return []
    names = []
    for label in ("mic", "system"):
        names.append(f"transcript.{label}.txt")
        names.append(f"transcript.{label}.json")
    return names


def validate_staged_outputs(staging_dir: Path, track_pair: tuple[Path, Path] | None) -> tuple[bool, str]:
    """Validate a run's staged transcript.md/transcript.json postconditions.

    Returns (ok, reason). A zero exit code from `stt` is not sufficient on
    its own: the JSON must parse and have the expected shape, and the
    Markdown must be non-empty. Per-track artifacts are not required for
    overall success (diarisation/identification failures are non-fatal
    upstream) but are promoted if present.
    """
    md = staging_dir / "transcript.md"
    js = staging_dir / "transcript.json"
    if not md.exists():
        return False, f"missing {md.name} in staged run output"
    if not js.exists():
        return False, f"missing {js.name} in staged run output"
    if md.stat().st_size == 0:
        return False, f"{md.name} is empty"
    if js.stat().st_size == 0:
        return False, f"{js.name} is empty"
    try:
        data = json.loads(js.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        return False, f"{js.name} is not valid JSON: {e}"
    if not validate_transcript_json_shape(data):
        return False, f"{js.name} does not have the expected shape (dict with a 'segments' list, or a top-level list)"
    return True, ""


def promotion_journal_path(session_dir: Path) -> Path:
    return session_dir / ".promotion_journal.json"


def _file_sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _promotion_entry_was_replaced(entry: dict[str, Any]) -> bool:
    """Detect replacement even if a crash preceded the journal flag write.

    There is an unavoidable instruction window between `os.replace` and the
    following journal update. The durable new-content digest closes that
    window during rollback/recovery: if the destination already contains the
    prepared new bytes, treat it as replaced even when `replaced` is false.
    """
    if entry.get("replaced"):
        return True
    expected = entry.get("new_sha256")
    return bool(expected) and _file_sha256(Path(entry["dst"])) == expected


def _rollback_promotion_entries(entries: list[dict[str, Any]]) -> None:
    """Roll every destination containing promoted bytes back to its prior state."""
    for entry in entries:
        if not _promotion_entry_was_replaced(entry):
            continue
        dst = Path(entry["dst"])
        backup = entry.get("backup")
        if entry.get("existed"):
            if backup and Path(backup).exists():
                os.replace(backup, dst)
        elif dst.exists():
            try:
                dst.unlink()
            except OSError:
                pass
        _fsync_dir(dst.parent)


def _cleanup_promotion_artifacts(entries: list[dict[str, Any]], journal_path: Path) -> None:
    for entry in entries:
        for key in ("temp", "backup"):
            p = entry.get(key)
            if p:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError:
                    pass
    journal_path.unlink(missing_ok=True)


def reconcile_promotion_journal(vault: Path, session_dir: Path) -> None:
    """Reconcile a leftover multi-file canonical-promotion journal, if any.

    `promote_run_outputs` never replaces canonical transcript artifacts
    directly. It first stages every destination's new-content temp file and
    (for any destination that already exists) a backup of its current
    content, durably journals all of that -- including, per destination,
    whether it existed before this promotion -- and only then performs the
    actual replace step for each destination in turn, persisting `replaced:
    true` for each one immediately after its `os.replace` succeeds. If this
    helper (or the whole machine) is killed mid-promotion, that journal is
    left behind on disk.

    Deterministic reconciliation policy, applied the next time any code
    path is about to read or finalize this session's state:

    - No journal file: nothing to do.
    - Every entry has `replaced: true`: the full destination set was
      already replaced before the crash (only backup/temp/journal cleanup
      was interrupted). It is safe to keep the new content and just finish
      cleanup (remove backups + temps + the journal) without touching any
      destination.
    - At least one entry does not have `replaced: true`: the promotion was
      interrupted partway through replacing the destination set (e.g.
      `transcript.md` replaced but `transcript.json` not yet). Roll every
      *already-replaced* destination back to its pre-promotion state
      (restore its backup, or remove it if this promotion would have
      created it fresh); destinations never reached need no action. This
      guarantees the canonical set as a whole is never left showing a mix
      of old and new files.

    This is a journalled transaction with rollback/recovery, not true
    filesystem multi-file atomicity: there is no OS primitive that replaces
    several independent files as a single atomic operation. Must be called
    before any code path reads this session's canonical transcript
    artifacts or metadata for a finalize decision. Serialized by
    `VaultLock` (reentrant, so safe to call from a caller that already
    holds it).
    """
    with VaultLock(vault):
        journal_path = promotion_journal_path(session_dir)
        if not journal_path.exists():
            return
        try:
            journal = load_json(journal_path)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            # Unreadable journal: nothing safe to reconstruct from it. Leave
            # it in place for manual inspection rather than guessing, and
            # warn so callers know the session's canonical state is
            # uncertain. `promote_run_outputs` refuses to proceed while a
            # journal still exists, so an unreadable journal is never
            # silently overwritten by a later promotion.
            print(
                f"WARNING: promotion journal {journal_path} is unreadable (corrupt or truncated); "
                "leaving it in place for manual inspection. This session's canonical state is "
                "uncertain; any attempt to promote new output into it will be refused until the "
                "journal is resolved.",
                file=sys.stderr,
            )
            return
        entries = journal.get("entries", [])
        fully_applied = bool(entries) and all(_promotion_entry_was_replaced(e) for e in entries)
        if not fully_applied:
            _rollback_promotion_entries(entries)
        _cleanup_promotion_artifacts(entries, journal_path)


def promote_run_outputs(
    vault: Path,
    staging_dir: Path,
    session_dir: Path,
    track_pair: tuple[Path, Path] | None,
) -> None:
    """Journalled transactional promotion of a validated run's staged
    outputs into the session root as the new canonical artifacts.

    This can replace several files at once (`transcript.md`,
    `transcript.json`, and per-track artifacts for meeting sessions) as one
    logical unit. Each individual destination is still replaced with a
    single atomic `os.replace` (write-temp-then-rename), but the *set* of
    replacements across multiple files is not an OS-level atomic operation
    -- there is no such primitive for several independent files on a normal
    filesystem. Instead this uses a journalled transaction: every
    destination's new-content temp file, and (for any destination that
    already exists) a backup of its current content, are prepared and
    durably recorded in a per-session promotion journal *before* anything
    is replaced. Only after that journal is durable does the actual replace
    step run for every destination, marking each one `replaced: true` in
    the journal immediately after it succeeds. If any replace fails
    partway through, the caught failure rolls every already-replaced
    destination back to its pre-promotion state, so the canonical set is
    never left showing a mix of old and new files, and the run's staging
    directory (the source of truth) is left completely untouched
    throughout. On full success the journal, backups, and temp files are
    cleared. A leftover journal from a crash (not just a caught exception)
    is reconciled by `reconcile_promotion_journal` the next time any code
    path reads this session's state.
    """
    reconcile_promotion_journal(vault, session_dir)

    # If a journal still exists after reconciliation, it was unreadable and
    # deliberately left in place. Refuse to promote new output over an
    # uncertain canonical state -- and never overwrite the unreadable
    # journal -- until the user resolves it.
    leftover = promotion_journal_path(session_dir)
    if leftover.exists():
        raise SystemExit(
            f"Refusing to promote output into {session_dir}: a leftover promotion journal ({leftover}) "
            "is unreadable and could not be reconciled. Inspect and resolve it manually before retrying."
        )

    names = ["transcript.md", "transcript.json"] + per_track_artifact_names(track_pair)
    sources = [(name, staging_dir / name) for name in names if (staging_dir / name).exists()]
    if not sources:
        return

    journal_path = promotion_journal_path(session_dir)
    entries: list[dict[str, Any]] = []
    journal = {"run_id": uuid.uuid4().hex, "session_dir": str(session_dir), "entries": entries}

    try:
        for name, src in sources:
            dst = session_dir / name
            existed = dst.exists()
            temp_path = _copy_bytes_to_sibling_temp(dst, src, "promote")
            backup_path = _copy_bytes_to_sibling_temp(dst, dst, "promote-backup") if existed else None
            entries.append(
                {
                    "name": name,
                    "dst": str(dst),
                    "temp": str(temp_path),
                    "backup": str(backup_path) if backup_path is not None else None,
                    "existed": existed,
                    "new_sha256": _file_sha256(temp_path),
                    "replaced": False,
                }
            )

        # All prep (new content + backups) is durable on disk before this
        # point; only now do we start touching canonical destinations.
        write_json(journal_path, journal)

        for entry in entries:
            os.replace(entry["temp"], entry["dst"])
            _fsync_dir(session_dir)
            entry["replaced"] = True
            write_json(journal_path, journal)
    except BaseException:
        _rollback_promotion_entries(entries)
        _cleanup_promotion_artifacts(entries, journal_path)
        raise

    _cleanup_promotion_artifacts(entries, journal_path)


def has_valid_final_transcript(session_dir: Path) -> bool:
    """True if the *canonical* transcript.md + transcript.json pair in the
    session root looks complete and schema-valid.

    Used by legacy-recovery paths and `list --pending`. Callers that need to
    decide whether a specific *run* succeeded should use
    `validate_staged_outputs` against that run's own staging directory
    instead -- this function says nothing about which run (if any) produced
    the canonical files.
    """
    transcript_md = session_dir / "transcript.md"
    transcript_json = session_dir / "transcript.json"
    if not (transcript_md.exists() and transcript_json.exists()):
        return False
    if transcript_md.stat().st_size == 0 or transcript_json.stat().st_size == 0:
        return False
    try:
        data = json.loads(transcript_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return False
    return validate_transcript_json_shape(data)


def legacy_canonical_recoverable(session_dir: Path, started_at: str | None) -> bool:
    """True only for legacy sessions with no run-tracking info at all.

    Accepts the canonical transcript.md/json in the session root as
    evidence of *this* attempt's success only if they are schema-valid AND
    both files are newer than the recorded attempt start time. This is
    intentionally conservative: it exists purely to recover sessions created
    before run-specific staging was tracked, and must never be used to
    "recover" a modern, run-tracked attempt using stale canonical artifacts
    from some earlier run.
    """
    if not has_valid_final_transcript(session_dir):
        return False
    if not started_at:
        return False
    try:
        started_dt = datetime.fromisoformat(started_at)
    except ValueError:
        return False
    for name in ("transcript.md", "transcript.json"):
        p = session_dir / name
        try:
            mtime_dt = datetime.fromtimestamp(p.stat().st_mtime).astimezone()
        except OSError:
            return False
        if mtime_dt <= started_dt:
            return False
    return True


def process_command_line(pid: int) -> str | None:
    """Best-effort full command line for `pid`, or None if it cannot be read."""
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return None
    cmdline = out.stdout.strip()
    return cmdline or None


def process_matches_session(pid: int, session_dir: Path) -> bool:
    """Best-effort check that `pid` looks like it belongs to `session_dir`.

    Reduces (but cannot eliminate) the risk of a reused PID being mistaken
    for the transcription child that originally used it: if we can read the
    process command line, require the session path to appear in it. If we
    cannot read it (no `ps`, or it returned nothing), fail open and assume it
    matches, since we have no stronger signal either way.

    This is only used for *reporting* whether a transcription looks
    genuinely in progress (to refuse duplicate `transcribe`/`start` calls);
    this script never signals the transcription child directly, so failing
    open here is a reporting-only risk. Contrast with `recorder_identity_verified`
    below, which gates real `os.killpg` calls and must fail closed instead.
    """
    cmdline = process_command_line(pid)
    if cmdline is None:
        return True
    return str(session_dir) in cmdline


def process_group_id(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        return None


def recorder_identity_verified(session: dict[str, Any]) -> bool:
    """Fail-closed identity check gating recorder kill safety.

    Before treating a detached recorder as genuinely active, or -- most
    critically -- immediately before every `os.killpg` call, require:

    - the PID is alive (and not a zombie);
    - its process-group id equals its own PID (consistent with the
      `start_new_session=True` launch, which makes the child its own group
      leader) and, if we recorded an expected PGID at launch, matches it;
    - its command line can be read and references this session's expected
      directory/output path, AND -- if a `stt` binary path was recorded at
      launch -- also references that binary path.

    Both the session-path and (when recorded) the binary-path checks must
    hold; matching only one is not sufficient. A cmdline that merely
    contains the same `stt` binary (e.g. a reused PID now running an
    unrelated `stt` invocation for a *different* session) must not be
    treated as this recorder, and a cmdline that merely mentions this
    session's directory without the expected binary must not either. If the
    command line cannot be read at all, identity cannot be confirmed and
    this returns False (fail closed) -- unlike the best-effort,
    reporting-only `process_matches_session` used for the transcription
    child, which fails open since it never gates a real signal.
    """
    pid = int(session.get("pid", 0) or 0)
    if not pid or not process_alive(pid):
        return False
    pgid = process_group_id(pid)
    if pgid is None or pgid != pid:
        return False
    expected_pgid = session.get("pgid")
    if expected_pgid is not None:
        try:
            if int(expected_pgid) != pgid:
                return False
        except (TypeError, ValueError):
            return False
    cmdline = process_command_line(pid)
    if not cmdline:
        return False
    session_dir = str(Path(session.get("session_dir", "")))
    if session_dir not in cmdline:
        return False
    stt_bin = session.get("stt_bin", "") or ""
    if stt_bin and stt_bin not in cmdline:
        return False
    return True


def process_alive(pid: int) -> bool:
    """True if `pid` is a running or stopped process, but not a reaped zombie.

    A SIGKILLed child whose parent hasn't reaped it remains as a zombie; for
    that zombie `os.kill(pid, 0)` still succeeds even though the process is
    gone. We treat zombies as dead so the stop escalation (which ends in
    SIGKILL) reports success once the process has actually been killed.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # Still have a PID entry -- confirm it isn't a zombie (state 'Z').
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return True  # `ps` unavailable; assume alive (safe default)
    state = out.stdout.strip()
    if not state:
        return False  # ps found no such process
    return not state.startswith("Z")


def write_session_note(session: dict[str, Any], vault: Path) -> None:
    session_dir = Path(session["session_dir"])
    rel_dir = relative_to_vault(session_dir, vault)
    audio_files = [Path(p) for p in session.get("audio_files", []) if Path(p).exists()]
    transcript = session.get("transcript_path")
    transcript_json = session.get("transcript_json_path")

    lines = [
        "---",
        f"title: {json.dumps(session['title'])[1:-1]}",
        f"started: {session.get('started_at', '')}",
        f"ended: {session.get('ended_at', '')}",
        f"mode: {session.get('mode', '')}",
        f"status: {session.get('status', '')}",
        "---",
        "",
        f"# {session['title']}",
        "",
        f"- Session folder: `{rel_dir}`",
        f"- Mode: `{session.get('mode', '')}`",
        f"- Status: `{session.get('status', '')}`",
        f"- Started: {session.get('started_at_human', session.get('started_at', ''))}",
    ]
    if session.get("ended_at_human"):
        lines.append(f"- Ended: {session['ended_at_human']}")
    if session.get("pid"):
        lines.append(f"- Recorder PID: `{session['pid']}`")
    if session.get("status") == "scheduled":
        lines.append(f"- Scheduled start: {session.get('scheduled_start_at_human', session.get('scheduled_start_at', ''))}")
    if session.get("status") == "scheduled_cancelled":
        lines.append(
            f"- Scheduled start was cancelled at "
            f"{session.get('cancelled_at_human', session.get('cancelled_at', ''))}: "
            f"{session.get('cancel_reason', 'see metadata.json')}"
        )
    if session.get("stt_bin"):
        lines.append(f"- STT binary: `{session['stt_bin']}`")
    if session.get("command"):
        lines.append(f"- Command: `{' '.join(session['command'])}`")
    if session.get("status") == "transcribing":
        lines.append(f"- Transcription PID: `{session.get('transcription_pid', '')}`")
        lines.append(f"- Transcription started: {session.get('transcription_started_at', '')}")
        if session.get("transcription_run_id"):
            lines.append(f"- Transcription run id: `{session.get('transcription_run_id')}`")
    if session.get("status") == "transcription_interrupted":
        lines.append(
            f"- Transcription was interrupted at "
            f"{session.get('interrupted_at_human', session.get('interrupted_at', ''))} before finishing; "
            "partial per-track artifacts may exist in this attempt's staging directory. "
            "Run `transcribe --resume` to finalize if a valid transcript was already produced, or retry."
        )
    if session.get("status") == "transcription_failed":
        lines.append(
            f"- Transcription failed at {session.get('failed_at_human', session.get('failed_at', ''))}: "
            f"{session.get('failure_reason', 'see transcription.log')}"
        )
    if session.get("status") == "recording_failed":
        lines.append(
            f"- Recording failed to launch at {session.get('failed_at_human', session.get('failed_at', ''))}: "
            f"{session.get('failure_reason', 'see recording.log')}"
        )

    lines.extend(["", "## Attendees (expected speakers)"])
    attendees = session.get("attendees", [])
    if attendees:
        source = session.get("attendees_source", "manual")
        lines.append(f"- Source: `{source}`")
        for att in attendees:
            name = att.get("name") or ""
            address = att.get("address") or ""
            if name and address:
                lines.append(f"- {name} <{address}>")
            elif address:
                lines.append(f"- {address}")
            else:
                lines.append(f"- {name}")
    else:
        lines.append("- No attendee list captured (run the meeting lookup or pass `--attendees`).")

    lines.extend(["", "## Audio files"])
    if audio_files:
        for audio in audio_files:
            rel = relative_to_vault(audio, vault)
            lines.append(f"- [[{rel}|{audio.name}]]")
    else:
        lines.append("- Recording in progress or no audio files detected yet.")

    lines.extend(["", "## Transcript"])
    if transcript and Path(transcript).exists():
        rel = relative_to_vault(Path(transcript), vault)
        lines.append(f"- [[{rel}|transcript.md]]")
    else:
        lines.append("- Not transcribed yet.")
    if transcript_json and Path(transcript_json).exists():
        rel = relative_to_vault(Path(transcript_json), vault)
        lines.append(f"- JSON: [[{rel}|transcript.json]]")

    note = session_dir / "session.md"
    note.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def discover_audio_files(session: dict[str, Any]) -> list[str]:
    session_dir = Path(session["session_dir"])
    ordered = []
    for name in ["mixed.wav", "mic.wav", "system.wav"]:
        path = session_dir / name
        if path.exists() and path.stat().st_size > 44:
            ordered.append(str(path))
    for path in sorted(session_dir.glob("*.wav")):
        if str(path) not in ordered and path.stat().st_size > 44:
            ordered.append(str(path))
    return ordered


def best_audio_for_transcription(session: dict[str, Any]) -> Path:
    files = discover_audio_files(session)
    if not files:
        raise SystemExit(f"No non-empty .wav files found in {session['session_dir']}")
    return Path(files[0])


def finalize_stopped_session(session: dict[str, Any], vault: Path) -> dict[str, Any]:
    """Persist a recording as stopped after an explicit or natural process exit."""
    with VaultLock(vault):
        session["status"] = "stopped"
        audio_files = discover_audio_files(session)
        session["audio_files"] = audio_files
        if not session.get("ended_at"):
            if audio_files:
                end_ts = max(Path(path).stat().st_mtime for path in audio_files)
                end_dt = datetime.fromtimestamp(end_ts).astimezone()
                session["ended_at"] = end_dt.isoformat(timespec="seconds")
                session["ended_at_human"] = end_dt.strftime("%Y-%m-%d %H:%M")
            else:
                session["ended_at"] = iso_now()
                session["ended_at_human"] = now_human()
        write_json(Path(session["session_dir"]) / "metadata.json", session)
        write_session_note(session, vault)
        active_state_path(vault).unlink(missing_ok=True)
        rebuild_board(vault)
    return session


def reconcile_active_recording(vault: Path) -> dict[str, Any] | None:
    """Finalize stale active-state files when the recorder exited on its own.

    Uses the fail-closed `recorder_identity_verified` check: a live PID whose
    identity we cannot confirm (PGID/command-line mismatch) is *not* treated
    as blocking further work, but this function never signals it -- it only
    updates our own bookkeeping.

    A `scheduled` reservation (created by `start --start-at`/`--delay`, pid
    still null until the recorder launches at the start time) is genuinely
    active and is returned as-is, so `status` reports it and a second `start`
    is refused. If the reservation is `scheduled` but the start time has
    already passed (the helper was killed mid-wait, e.g. machine reboot or
    harness timeout), the schedule is stale: it is cancelled and cleared so it
    does not block future work -- this is the reconciliation path, never a
    signal to a real recorder.
    """
    with VaultLock(vault):
        active = active_state_path(vault)
        if not active.exists():
            return None
        session = load_json(active)
        if session.get("status") == "scheduled":
            scheduled_at = session.get("scheduled_start_at") or ""
            try:
                target = datetime.fromisoformat(scheduled_at) if scheduled_at else None
            except ValueError:
                target = None
            now = datetime.now().astimezone()
            stale = target is None or now >= target + timedelta(seconds=SCHEDULED_START_GRACE_SECONDS)
            if not stale:
                session["active"] = True
                session["starts_in_seconds"] = max(0, int((target - now).total_seconds()))
                return session
            # Stale schedule: the waiting helper is gone. Cancel it so it
            # stops blocking new recordings/transcriptions.
            session["status"] = "scheduled_cancelled"
            session["cancelled_at"] = iso_now()
            session["cancelled_at_human"] = now_human()
            session["cancel_reason"] = "scheduled start never launched (waiting helper exited); schedule cancelled on reconciliation"
            write_json(Path(session["session_dir"]) / "metadata.json", session)
            write_session_note(session, vault)
            active.unlink(missing_ok=True)
            rebuild_board(vault)
            session["active"] = False
            return session
        if recorder_identity_verified(session):
            session["active"] = True
            return session
        session = finalize_stopped_session(session, vault)
        session["active"] = False
        return session


def mark_recording_failed(
    session: dict[str, Any],
    vault: Path,
    reservation_id: str | None,
    reason: str,
) -> dict[str, Any]:
    """Persist a recorder-launch failure and clear only the reservation this
    call itself created.

    Called when `Popen` itself raises for the recorder child (e.g. the stt
    binary could not be executed at all, as opposed to a signal interrupting
    the launch). Must be called while still holding `VaultLock(vault)` (the
    whole `start()` launch sequence already does). Only clears
    `.active_recording.json` if it still names this exact
    `reservation_id` -- never blindly deletes whatever reservation happens
    to be on disk, so a foreign reservation (in principle, from some other
    invocation) is never clobbered by this one's failure.
    """
    session_dir = Path(session["session_dir"])
    session["status"] = "recording_failed"
    session["failure_reason"] = reason
    session["failed_at"] = iso_now()
    session["failed_at_human"] = now_human()
    write_json(session_dir / "metadata.json", session)
    write_session_note(session, vault)
    active = active_state_path(vault)
    if active.exists():
        try:
            current = load_json(active)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            current = {}
        if reservation_id is None or current.get("recording_reservation_id") == reservation_id:
            active.unlink(missing_ok=True)
    rebuild_board(vault)
    return session


def active_transcription_owned_by(vault: Path, run_id: str | None) -> bool:
    if not run_id:
        return False
    path = active_transcription_state_path(vault)
    if not path.exists():
        return False
    try:
        payload = load_json(path)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return False
    return payload.get("run_id") == run_id


def clear_owned_reservation(vault: Path, run_id: str | None) -> None:
    with VaultLock(vault):
        if run_id is None or active_transcription_owned_by(vault, run_id):
            active_transcription_state_path(vault).unlink(missing_ok=True)


class ForeignRunOwnership(Exception):
    """Raised when `run_id` no longer owns the right to mutate/promote a session.

    A later run may have already claimed the active-transcription
    reservation (a genuinely concurrent later attempt), or the session's
    on-disk `metadata.json` may already record a different, later run's
    `transcription_run_id` (e.g. a prior run's finalize already ran while
    this run's own outputs were still staged). Either way, this run's
    outputs are stale: they must never be promoted into the session root,
    and its terminal-state writes must never clobber the newer run's
    bookkeeping or clear the newer run's active-state reservation.
    """


def assert_run_owns_session(vault: Path, session: dict[str, Any], run_id: str | None) -> None:
    """Refuse mutation/promotion if `run_id` no longer owns this session.

    Must be called while the caller already holds `VaultLock(vault)`; this
    function does not acquire the lock itself, so it can be composed inside
    an already-locked state transition without deadlocking (`VaultLock` is
    only reentrant within the same thread).

    `run_id is None` is the legacy/reconciliation escape hatch and is always
    allowed: those callers (`reconcile_active_transcription`'s own
    finalize/interrupt calls, and `try_resume_finalize`'s legacy-canonical
    branch) already derived their finalize decision directly from the
    currently-locked state, not from some possibly-stale prior run's
    perspective, so there is nothing foreign for them to be guarded against.

    For every other (modern, run-tracked) `run_id`, ownership is granted
    only if:

    - `.active_transcription.json` is absent AND the session's own on-disk
      `metadata.json` still names this `run_id` in `transcription_run_id`
      (i.e. no later run has since claimed the reservation and overwritten
      metadata out from under it), or
    - `.active_transcription.json` exists and is owned by this `run_id`.

    Otherwise -- a foreign run currently holds the active reservation, or
    on-disk metadata already names a different run -- raises
    `ForeignRunOwnership` rather than mutate or promote anything.
    """
    if run_id is None:
        return
    active_path = active_transcription_state_path(vault)
    if active_path.exists():
        try:
            active_payload = load_json(active_path)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            active_payload = {}
        active_run_id = active_payload.get("run_id")
        if active_run_id != run_id:
            raise ForeignRunOwnership(
                f"run {run_id!r} no longer owns the active-transcription reservation for "
                f"{session.get('session_dir')!r} (currently held by {active_run_id!r})"
            )
        return
    session_dir = Path(session.get("session_dir", ""))
    metadata_path = session_dir / "metadata.json"
    if metadata_path.exists():
        try:
            on_disk = load_json(metadata_path)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            on_disk = {}
        on_disk_run_id = on_disk.get("transcription_run_id")
        if on_disk_run_id is not None and on_disk_run_id != run_id:
            raise ForeignRunOwnership(
                f"run {run_id!r} no longer owns session {session_dir!s} "
                f"(on-disk metadata now names run {on_disk_run_id!r})"
            )


def promote_run_outputs_if_owned(
    vault: Path,
    session: dict[str, Any],
    run_id: str | None,
    staging_dir: Path,
    session_dir: Path,
    track_pair: tuple[Path, Path] | None,
) -> None:
    """Guarded wrapper around `promote_run_outputs`.

    Only copies `run_id`'s staged outputs into the session root if `run_id`
    still owns this session per `assert_run_owns_session` -- raises
    `ForeignRunOwnership` (and promotes nothing) otherwise. Must be called
    while holding `VaultLock(vault)`, immediately before every modern-run
    (`run_id is not None`) promotion.
    """
    assert_run_owns_session(vault, session, run_id)
    promote_run_outputs(vault, staging_dir, session_dir, track_pair)


def finalize_transcribed_session(
    session: dict[str, Any],
    vault: Path,
    transcript_path: Path,
    transcript_json_path: Path,
    log_path: str | Path | None,
    audio_desc: str,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Centralized success path: persist a session as transcribed.

    Used by the normal transcribe flow, by `--resume` when a valid
    run-specific (or, for legacy sessions, sufficiently-newer canonical)
    transcript already exists, and by stale-state reconciliation when a dead
    transcription child turns out to have finished successfully.

    Only clears `.active_transcription.json` if it is unset or still owned
    by `run_id` (or `run_id` is None, meaning the caller already derived this
    finalize decision directly from that file's own contents) -- this
    prevents one run's finalize from clearing a different, later run's
    active-state reservation.

    Before any of that, `assert_run_owns_session` guards the whole mutation:
    a modern (`run_id is not None`) run whose active reservation has since
    been claimed by a foreign run, or whose on-disk metadata already names a
    different run, is refused outright (`ForeignRunOwnership`) rather than
    overwrite a newer run's state.
    """
    with VaultLock(vault):
        assert_run_owns_session(vault, session, run_id)
        session_dir = Path(session["session_dir"])
        session.update(
            {
                "status": "transcribed",
                "transcribed_at": iso_now(),
                "transcribed_at_human": now_human(),
                "transcribed_audio_path": audio_desc,
                "transcript_path": str(transcript_path),
                "transcript_json_path": str(transcript_json_path),
                "audio_files": discover_audio_files(session),
            }
        )
        if log_path:
            session["transcription_log_path"] = str(log_path)
        session.pop("transcription_pid", None)
        session.pop("transcription_started_at", None)
        write_json(session_dir / "metadata.json", session)
        write_session_note(session, vault)
        if run_id is None or active_transcription_owned_by(vault, run_id):
            active_transcription_state_path(vault).unlink(missing_ok=True)
        rebuild_board(vault)
    return session


def mark_transcription_interrupted(
    session: dict[str, Any],
    vault: Path,
    log_path: str | Path | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Persist a session as interrupted and clear the active-transcription state.

    Guarded by `assert_run_owns_session`: a modern run that no longer owns
    this session (per the active reservation or on-disk metadata) is
    refused rather than allowed to overwrite a newer run's state.
    """
    with VaultLock(vault):
        assert_run_owns_session(vault, session, run_id)
        session_dir = Path(session["session_dir"])
        session["status"] = "transcription_interrupted"
        session["interrupted_at"] = iso_now()
        session["interrupted_at_human"] = now_human()
        session.pop("transcription_pid", None)
        session.pop("transcription_started_at", None)
        if log_path:
            session["transcription_log_path"] = str(log_path)
        write_json(session_dir / "metadata.json", session)
        write_session_note(session, vault)
        if run_id is None or active_transcription_owned_by(vault, run_id):
            active_transcription_state_path(vault).unlink(missing_ok=True)
        rebuild_board(vault)
    return session


def mark_transcription_failed(
    session: dict[str, Any],
    vault: Path,
    log_path: str | Path | None,
    reason: str,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Persist a session as transcription_failed with a clear failure reason.

    Never deletes the transcription log or the run's staging directory --
    both are left on disk for inspection.

    Guarded by `assert_run_owns_session`: a modern run that no longer owns
    this session (per the active reservation or on-disk metadata) is
    refused rather than allowed to overwrite a newer run's state.
    """
    with VaultLock(vault):
        assert_run_owns_session(vault, session, run_id)
        session_dir = Path(session["session_dir"])
        session["status"] = "transcription_failed"
        session["failure_reason"] = reason
        session["failed_at"] = iso_now()
        session["failed_at_human"] = now_human()
        session.pop("transcription_pid", None)
        session.pop("transcription_started_at", None)
        if log_path:
            session["transcription_log_path"] = str(log_path)
        write_json(session_dir / "metadata.json", session)
        write_session_note(session, vault)
        if run_id is None or active_transcription_owned_by(vault, run_id):
            active_transcription_state_path(vault).unlink(missing_ok=True)
        rebuild_board(vault)
    return session


def reconcile_active_transcription(vault: Path) -> dict[str, Any] | None:
    """Finalize or clear a stale `.active_transcription.json` state file.

    - If the recorded child PID is alive and its command line still
      references the session directory (best-effort check), the
      transcription is genuinely in progress: return it with `active: True`
      so callers can refuse duplicate work.
    - Otherwise, validate *only this run's own staged output*
      (`staging_dir` recorded in the active-state payload) using
      `validate_staged_outputs`. If valid, promote it into the session root
      and finalize as transcribed. A recorded `staging_dir` that is missing
      or invalid is never papered over with old canonical artifacts.
    - Only for legacy payloads with no `run_id` at all (predating
      run-specific staging) does this fall back to accepting a
      schema-valid canonical transcript.md/json newer than the recorded
      attempt start time.
    - If none of the above apply, mark the session transcription_interrupted
      and clear the stale active state; any staged/partial artifacts are
      preserved untouched on disk.
    """
    with VaultLock(vault):
        active = active_transcription_state_path(vault)
        if not active.exists():
            return None
        payload = load_json(active)
        pid = int(payload.get("pid", 0) or 0)
        session_dir = Path(payload.get("session_dir", ""))
        if pid and process_alive(pid) and process_matches_session(pid, session_dir):
            payload["active"] = True
            return payload

        reconcile_promotion_journal(vault, session_dir)
        metadata_path = session_dir / "metadata.json"
        session = load_json(metadata_path) if metadata_path.exists() else {
            "session_dir": str(session_dir),
            "title": payload.get("title", "Untitled recording"),
            "mode": payload.get("mode", "unknown"),
        }
        log_path = payload.get("log_path")
        run_id = payload.get("run_id")
        track_pair = meeting_track_pair(session)

        finalized: dict[str, Any] | None = None
        staging_dir_raw = payload.get("staging_dir")
        if staging_dir_raw:
            staging_dir = Path(staging_dir_raw)
            if staging_dir.exists():
                ok, _reason = validate_staged_outputs(staging_dir, track_pair)
                if ok:
                    promote_run_outputs_if_owned(vault, session, run_id, staging_dir, session_dir, track_pair)
                    audio_desc = session.get("transcribed_audio_path") or payload.get("audio_desc") or ""
                    finalized = finalize_transcribed_session(
                        session, vault, session_dir / "transcript.md", session_dir / "transcript.json",
                        log_path, audio_desc, run_id=run_id,
                    )
            # A run-specific staging path was recorded: never fall back to
            # possibly-stale canonical artifacts for this (modern) attempt.
        elif legacy_canonical_recoverable(session_dir, payload.get("started_at")):
            audio_desc = session.get("transcribed_audio_path") or payload.get("audio_desc") or ""
            finalized = finalize_transcribed_session(
                session, vault, session_dir / "transcript.md", session_dir / "transcript.json",
                log_path, audio_desc, run_id=run_id,
            )

        if finalized is not None:
            session = finalized
        else:
            session = mark_transcription_interrupted(session, vault, log_path, run_id=run_id)
        session["active"] = False
        return session


def monitor(args: argparse.Namespace) -> None:
    """Wait for a finite recorder to exit, then finalize its Obsidian state."""
    deadline = time.time() + args.max_wait if args.max_wait else None
    while process_alive(args.pid):
        if deadline and time.time() > deadline:
            raise SystemExit(f"Recorder PID {args.pid} still alive after {args.max_wait}s; leaving active state unchanged.")
        time.sleep(0.5)
    time.sleep(0.5)
    reconcile_active_recording(vault_path())


def launch_duration_monitor(session: dict[str, Any], duration: float | None) -> None:
    if duration is None:
        return
    monitor_log = Path(session.get("monitor_log_path") or (Path(session["session_dir"]) / "monitor.log"))
    max_wait = max(duration + 120.0, duration * 2.0)
    with monitor_log.open("ab") as log:
        subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "monitor",
                "--pid",
                str(session["pid"]),
                "--max-wait",
                str(max_wait),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def wait_for_recording_to_finish(session: dict[str, Any], vault: Path, max_wait: float | None, proc: subprocess.Popen[Any] | None = None) -> dict[str, Any]:
    pid = int(session.get("pid", 0) or 0)
    if proc is not None:
        try:
            proc.wait(timeout=max_wait)
        except subprocess.TimeoutExpired:
            raise SystemExit(f"Recorder PID {pid} still alive after {max_wait}s; leaving active state unchanged.")
    else:
        deadline = time.time() + max_wait if max_wait else None
        while pid and process_alive(pid):
            if deadline and time.time() > deadline:
                raise SystemExit(f"Recorder PID {pid} still alive after {max_wait}s; leaving active state unchanged.")
            time.sleep(0.5)
    time.sleep(0.5)
    reconciled = reconcile_active_recording(vault)
    metadata_path = Path(session["session_dir"]) / "metadata.json"
    final_session = load_json(metadata_path) if metadata_path.exists() else (reconciled or session)
    final_session["active"] = False
    return final_session


def meeting_track_pair(session: dict[str, Any]) -> tuple[Path, Path] | None:
    if session.get("mode") != "meeting":
        return None
    session_dir = Path(session["session_dir"])
    mic = session_dir / "mic.wav"
    system = session_dir / "system.wav"
    if mic.exists() and mic.stat().st_size > 44 and system.exists() and system.stat().st_size > 44:
        return mic, system
    return None


def make_session(title: str, mode: str, vault: Path) -> dict[str, Any]:
    token = now_token()
    session_dir = recordings_root(vault) / f"{token}-{slugify(title)}"
    session_dir.mkdir(parents=True, exist_ok=False)
    started_human = now_human()
    return {
        "title": title,
        "mode": mode,
        "status": "recording",
        "session_dir": str(session_dir),
        "started_at": iso_now(),
        "started_at_human": started_human,
        "audio_files": [],
    }


def parse_attendees(raw: str | None) -> list[dict[str, str]]:
    """Parse a --attendees value into [{"name", "address"}, ...].

    Accepts comma-separated entries; each entry may be a bare email
    ("a@b.com"), a display name + email ("Alice <a@b.com>"), or name only.
    A comma is only a separator when it follows a closing `>` (the end of a
    bracketed email) or when an email starts immediately after it -- so
    Exchange "Lastname, First <email>" names keep their inner comma. This
    becomes the session's expected-speaker whitelist for later
    diarisation/identification.
    """
    if not raw:
        return []

    chunks: list[str] = []
    current: list[str] = []
    last_nonspace = ""
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == ",":
            # A comma is a separator unless it sits inside a display name
            # that is followed by a bracketed email later in the same entry
            # (Exchange "Lastname, First <email>"), or it ends one
            # (last_nonspace == ">").
            nxt = raw.find(",", i + 1)
            between = raw[i + 1 : nxt if nxt != -1 else n]
            is_separator = last_nonspace == ">" or "<" not in between
            if is_separator:
                chunks.append("".join(current).strip())
                current = []
                i += 1
                continue
        if ch not in " \t" or current:
            current.append(ch)
            if ch not in " \t":
                last_nonspace = ch
        i += 1
    tail = "".join(current).strip()
    if tail:
        chunks.append(tail)

    parsed: list[dict[str, str]] = []
    for chunk in chunks:
        if not chunk:
            continue
        m = re.match(r"^(.*?)\s*<([^<>]+)>\s*$", chunk)
        if m:
            name = m.group(1).strip()
            address = m.group(2).strip()
        elif re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", chunk):
            name = ""
            address = chunk
        else:
            name = chunk
            address = ""
        parsed.append({"name": name, "address": address})
    return parsed


def command_for_recording(stt_bin: Path, session: dict[str, Any], duration: float | None) -> list[str]:
    mode = session["mode"]
    session_dir = Path(session["session_dir"])
    cmd = [str(stt_bin), "record", "--mode", mode]
    if mode == "meeting":
        cmd += ["--output-dir", str(session_dir)]
    elif mode == "mic":
        cmd += ["--output", str(session_dir / "mic.wav")]
    elif mode == "system":
        cmd += ["--output", str(session_dir / "system.wav")]
    else:
        raise SystemExit(f"Unsupported mode: {mode}")
    cmd.append("--fail-if-empty")
    if duration is not None:
        cmd += ["--duration", str(duration)]
    return cmd


def parse_scheduled_start(value: str) -> datetime:
    """Parse a --start-at wall-clock value into a timezone-aware datetime.

    Accepts HH:MM, HH:MM:SS (today, local time) or an ISO-8601 datetime
    (e.g. 2026-08-12T15:00:00 or 2026-08-12T15:00:00+10:00). Naive ISO
    datetimes are interpreted as local time.
    """
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", raw)
    if not m:
        raise SystemExit(
            f"Invalid --start-at {value!r}: use HH:MM, HH:MM:SS, or an ISO-8601 datetime."
        )
    h, mi = int(m.group(1)), int(m.group(2))
    s = int(m.group(3) or 0)
    if not (0 <= h <= 23 and 0 <= mi <= 59 and 0 <= s <= 59):
        raise SystemExit(f"Invalid --start-at {value!r}: time out of range.")
    now = datetime.now().astimezone()
    return now.replace(hour=h, minute=mi, second=s, microsecond=0)


def scheduled_start_wait_seconds(args: argparse.Namespace) -> float | None:
    """Return seconds to wait before launching the recorder, or None.

    Implements the --start-at / --delay scheduling pair: at most one may be
    given. --delay is a relative duration from now; --start-at is a wall-clock
    time (HH:MM, HH:MM:SS, or ISO-8601). Raises SystemExit on invalid input or
    a start time that is already in the past.
    """
    if getattr(args, "start_at", None) is not None and getattr(args, "delay", None) is not None:
        raise SystemExit("Give only one of --start-at or --delay, not both.")
    delay = getattr(args, "delay", None)
    if delay is not None:
        if delay < 0:
            raise SystemExit("--delay must be >= 0 seconds.")
        return float(delay)
    start_at = getattr(args, "start_at", None)
    if start_at is None:
        return None
    target = parse_scheduled_start(start_at)
    wait = (target - datetime.now().astimezone()).total_seconds()
    if wait <= 0:
        raise SystemExit(
            f"--start-at {args.start_at} is in the past ({target.isoformat()}); "
            "give a future time."
        )
    return wait


def interruptible_wait(seconds: float, cancel_check=None) -> bool:
    """Sleep for `seconds`, converting SIGINT/SIGTERM into a catchable
    interrupt so a scheduled recording can be cancelled cleanly.

    `cancel_check` is an optional zero-arg callable invoked at least once per
    second; when it returns True the wait aborts early (e.g. the schedule was
    cancelled by `stop` while we slept) and this returns True so the caller
    treats it like an interruption. Returns True if the wait was interrupted
    or aborted, False if it completed.
    """
    previous_sigint = previous_sigterm = None
    signals_overridden = False
    try:
        previous_sigint = signal.getsignal(signal.SIGINT)
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGINT, _raise_transcription_interrupted)
        signal.signal(signal.SIGTERM, _raise_transcription_interrupted)
        signals_overridden = True
    except ValueError:
        pass
    try:
        deadline = time.time() + seconds
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return False
            if cancel_check is not None and cancel_check():
                return True
            time.sleep(min(remaining, 0.5))
    except (KeyboardInterrupt, TranscriptionInterrupted):
        return True
    finally:
        if signals_overridden:
            signal.signal(signal.SIGINT, previous_sigint)
            signal.signal(signal.SIGTERM, previous_sigterm)


def start(args: argparse.Namespace) -> None:
    vault = vault_path()
    root = recordings_root(vault)
    root.mkdir(parents=True, exist_ok=True)
    active = active_state_path(vault)
    wait_seconds = scheduled_start_wait_seconds(args)  # None if not scheduled

    with VaultLock(vault):
        rebuild_board(vault)
        existing = reconcile_active_recording(vault)
        if existing and existing.get("active"):
            pid = int(existing.get("pid", 0) or 0)
            if existing.get("status") == "scheduled":
                raise SystemExit(
                    f"A recording is already scheduled for {existing.get('scheduled_start_at_human', '?')}: {existing.get('title')}\n"
                    f"Session: {existing.get('session_dir')}\n"
                    "Run `recordings.py status` or `stop` (to cancel the schedule) before starting another."
                )
            raise SystemExit(
                f"A recording is already active (PID {pid}): {existing.get('title')}\n"
                f"Session: {existing.get('session_dir')}\n"
                "Run `recordings.py status` or stop it before starting another."
            )
        existing_transcription = reconcile_active_transcription(vault)
        if existing_transcription and existing_transcription.get("active"):
            pid = int(existing_transcription.get("pid", 0) or 0)
            raise SystemExit(
                f"A transcription is currently active (PID {pid}) for session "
                f"{existing_transcription.get('session_dir')}. Wait for it to finish, or check `status`, "
                "before starting a new recording."
            )

        title = args.title or "Untitled recording"
        mode = args.mode
        session = make_session(title, mode, vault)
        if wait_seconds is not None:
            # Scheduled start: the session is created and reserved immediately
            # (status "scheduled") so it shows on the board/status and a second
            # `start` is refused, but the recorder is not launched until the
            # start time. The pre-launch reservation below carries the same
            # pid-null fields as a normal start; the launch itself happens
            # after the wait, in a second lock scope.
            session["status"] = "scheduled"
            target = datetime.now().astimezone() + timedelta(seconds=wait_seconds)
            session["scheduled_start_at"] = target.isoformat(timespec="seconds")
            session["scheduled_start_at_human"] = target.strftime("%Y-%m-%d %H:%M")
            session["scheduled_start_in_seconds"] = int(wait_seconds)
        attendees = parse_attendees(args.attendees)
        if attendees:
            session["attendees"] = attendees
            session["attendees_source"] = "calendar" if args.attendees_source == "calendar" else "manual"
        session_dir = Path(session["session_dir"])
        stt_bin = find_stt_bin()
        cmd = command_for_recording(stt_bin, session, args.duration)
        # `cmd` is launched via a hidden `exec-unblocked` shim (see
        # `wrap_launch_command`) so the real recorder process never
        # inherits this launch's blocked SIGINT/SIGTERM signal mask across
        # exec -- without it, a blocked mask survives exec and the
        # recorder would be permanently unable to receive the very signals
        # `stop()` sends to its process group. `command` below still
        # records the real, unwrapped stt command (what actually matters
        # for identity checks and for the user); `launched_command` records
        # what was actually passed to `Popen` for debugging.
        launch_cmd = wrap_launch_command(cmd)
        log_path = session_dir / "recording.log"
        reservation_id = uuid.uuid4().hex

        # Pre-launch reservation: persisted *before* Popen (pid/pgid still
        # unknown/null) so an interrupt between here and PID/PGID
        # persistence still leaves a discoverable, owned reservation on disk
        # (session dir, command, log path) instead of a silently untracked
        # child with no trace on disk.
        session.update(
            {
                "pid": None,
                "pgid": None,
                "stt_bin": str(stt_bin),
                "command": cmd,
                "launched_command": launch_cmd,
                "log_path": str(log_path),
                "recording_reservation_id": reservation_id,
            }
        )
        if args.duration is not None:
            session["monitor_log_path"] = str(session_dir / "monitor.log")
        write_json(session_dir / "metadata.json", session)
        write_json(active, session)
        write_session_note(session, vault)
        rebuild_board(vault)

    if wait_seconds is not None:
        # Scheduled start: report the schedule immediately, then wait for the
        # start time WITHOUT holding the vault lock, so `status`/`stop`/`list`
        # stay responsive during the wait and the schedule can be cancelled
        # (via `stop` or SIGINT/SIGTERM) instead of blocking everything.
        scheduled_payload = {
            "status": "scheduled",
            "title": title,
            "mode": mode,
            "session_dir": str(session_dir),
            "scheduled_start_at": session["scheduled_start_at"],
            "scheduled_start_at_human": session["scheduled_start_at_human"],
            "starts_in_seconds": int(wait_seconds),
            "board": str(board_path(vault)),
            "log": str(log_path),
            "transcribing": False,
        }
        if args.duration is not None:
            scheduled_payload["expected_stop_at"] = (
                datetime.now().astimezone() + timedelta(seconds=wait_seconds + args.duration)
            ).isoformat(timespec="minutes")
        print(json.dumps(scheduled_payload, indent=2))

        def schedule_cancelled_while_waiting() -> bool:
            """True when the schedule is no longer ours while we sleep (e.g.
            a concurrent `stop` cancelled it, or reconciliation cleared it).
            Reads the active-state file without the lock: write_json is atomic
            (temp + rename), so a lock-free read always sees a complete file.
            """
            try:
                current = load_json(active) if active.exists() else {}
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                return False
            return current.get("recording_reservation_id") != reservation_id

        interrupted = interruptible_wait(wait_seconds, cancel_check=schedule_cancelled_while_waiting)
        with VaultLock(vault):
            current = load_json(active) if active.exists() else {}
            if current.get("recording_reservation_id") != reservation_id:
                # The schedule was cancelled/stopped by another invocation
                # while we waited; nothing was launched.
                print(json.dumps({
                    "status": "scheduled_cancelled",
                    "title": title,
                    "session_dir": str(session_dir),
                    "cancelled_at_human": now_human(),
                    "reason": "recording was stopped or cancelled while waiting for the scheduled start",
                    "board": str(board_path(vault)),
                }, indent=2))
                return
            if interrupted:
                # User interrupted the wait (SIGINT/SIGTERM); nothing was
                # launched yet, so cancelling the schedule is safe and
                # complete (unlike a launch-window interrupt, no child can
                # exist at this point).
                session["status"] = "scheduled_cancelled"
                session["cancelled_at"] = iso_now()
                session["cancelled_at_human"] = now_human()
                session["cancel_reason"] = "interrupted while waiting for the scheduled start"
                write_json(session_dir / "metadata.json", session)
                write_session_note(session, vault)
                active.unlink(missing_ok=True)
                rebuild_board(vault)
                print(json.dumps({
                    "status": "scheduled_cancelled",
                    "title": title,
                    "session_dir": str(session_dir),
                    "cancelled_at_human": session["cancelled_at_human"],
                    "reason": session["cancel_reason"],
                    "board": str(board_path(vault)),
                }, indent=2))
                return
            # Start time reached and the reservation is still ours: flip to
            # recording and launch below.
            session["status"] = "recording"
            write_json(session_dir / "metadata.json", session)
            write_json(active, session)
            write_session_note(session, vault)
            rebuild_board(vault)

    with VaultLock(vault):
        log = log_path.open("ab")

        # Convert SIGINT/SIGTERM into a catchable exception across the
        # launch, using the same blocked-signal protocol as `transcribe()`
        # (see `_blocked_signals`): catchable handlers installed first, then
        # (when possible) the signals are genuinely blocked across Popen +
        # PID/PGID persistence so an interrupt cannot land between child
        # creation and the `proc = ...` assignment and orphan an untracked
        # child.
        previous_sigint = previous_sigterm = None
        signals_overridden = False
        try:
            previous_sigint = signal.getsignal(signal.SIGINT)
            previous_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGINT, _raise_transcription_interrupted)
            signal.signal(signal.SIGTERM, _raise_transcription_interrupted)
            signals_overridden = True
        except ValueError:
            pass

        def restore_signals() -> None:
            if signals_overridden:
                signal.signal(signal.SIGINT, previous_sigint)
                signal.signal(signal.SIGTERM, previous_sigterm)

        block_cm = _blocked_signals() if signals_overridden else contextlib.nullcontext(False)

        proc: subprocess.Popen[Any] | None = None
        launch_ambiguous = False
        try:
            with block_cm as signals_blocked:
                if not signals_blocked:
                    # Conservative fallback: true blocking is unavailable
                    # here. The catchable-handler protection still applies,
                    # but the child-created-before-assignment-completes race
                    # is not fully closed -- never treat `proc is None`
                    # below as proof nothing was launched in that case.
                    launch_ambiguous = True
                try:
                    proc = subprocess.Popen(
                        launch_cmd,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        cwd=str(STT_REPO),
                        start_new_session=True,
                    )
                except OSError as e:
                    restore_signals()
                    log.close()
                    mark_recording_failed(session, vault, reservation_id, f"failed to launch stt binary: {e}")
                    raise SystemExit(f"Failed to launch stt binary: {e}")

                # start_new_session=True makes the child its own
                # process-group leader, so pgid == pid at launch; record it
                # for later kill-safety identity verification. Persisted
                # before this `with` block can unblock signals, so PID/PGID
                # state is durable before a deferred signal can be
                # delivered.
                session["pid"] = proc.pid
                session["pgid"] = proc.pid
                write_json(session_dir / "metadata.json", session)
                write_json(active, session)
                write_session_note(session, vault)
                rebuild_board(vault)
        except (KeyboardInterrupt, TranscriptionInterrupted):
            restore_signals()
            log.close()
            if proc is None:
                if launch_ambiguous:
                    # No true blocking was available, so we cannot rule out
                    # that a child was actually created before this
                    # interrupt landed even though our own `proc` variable
                    # never got assigned. Never clear the reservation on
                    # pure speculation.
                    raise SystemExit(
                        "Recording launch was interrupted and, because signal blocking was unavailable on "
                        "this platform, whether the recorder process actually started could not be "
                        f"confirmed; a pre-launch reservation for session {session_dir} (reservation id "
                        f"{reservation_id}) remains on disk rather than being discarded on pure speculation. "
                        "Run `status` to check for an orphaned recorder process before retrying."
                    )
                # True blocking was available and in effect for this whole
                # window, so reaching here with `proc is None` can only mean
                # the interrupt landed before Popen was even entered --
                # nothing was launched, but the pre-launch reservation is
                # left in place (not cleared) since it is still a truthful
                # record of what this invocation intended to launch.
                raise SystemExit(
                    "Recording launch was interrupted before the recorder process could be confirmed "
                    f"started; a pre-launch reservation for session {session_dir} (reservation id "
                    f"{reservation_id}) remains on disk -- run `status` to check for an orphaned recorder "
                    "process before retrying."
                )
            # PID/PGID were already durably persisted before this signal
            # could be delivered (blocked case), or the interrupt landed
            # right at unblock time after that persistence had already
            # completed either way -- the recording is tracked normally, so
            # report it as started rather than losing track of it.
            print(
                json.dumps(
                    {
                        "status": "recording_started",
                        "helper_interrupted": True,
                        "title": title,
                        "mode": mode,
                        "pid": proc.pid,
                        "session_dir": str(session_dir),
                        "board": str(board_path(vault)),
                        "log": str(log_path),
                        "transcribing": False,
                    },
                    indent=2,
                )
            )
            return
        restore_signals()
        log.close()

        launch_duration_monitor(session, args.duration)
        rebuild_board(vault)

    started_payload = {
        "status": "recording_started",
        "title": title,
        "mode": mode,
        "pid": proc.pid,
        "session_dir": session["session_dir"],
        "board": str(board_path(vault)),
        "log": str(log_path),
        "transcribing": False,
    }
    if not args.wait:
        print(json.dumps(started_payload, indent=2))
        return

    max_wait = args.wait_timeout or (max((args.duration or 0) + 120.0, (args.duration or 0) * 2.0) if args.duration else None)
    final_session = wait_for_recording_to_finish(session, vault, max_wait, proc)
    print(json.dumps({
        **started_payload,
        "status": "recording_finished",
        "recording_status": final_session.get("status"),
        "active": final_session.get("active", False),
        "ended_at": final_session.get("ended_at"),
        "ended_at_human": final_session.get("ended_at_human"),
        "audio_files": final_session.get("audio_files", []),
    }, indent=2))


def stop(args: argparse.Namespace) -> None:
    vault = vault_path()
    with VaultLock(vault):
        active = active_state_path(vault)
        if not active.exists():
            raise SystemExit("No active recording state found.")
        session = load_json(active)
        if session.get("status") == "scheduled":
            # A scheduled recording has not launched yet (pid is null), so
            # there is no recorder process to signal. `stop` on a schedule
            # cancels it: clear the reservation and mark the session
            # cancelled so the start-time wait (still running in the helper
            # that created the schedule, or a stale reservation left by a
            # killed helper) does not launch anything.
            session["status"] = "scheduled_cancelled"
            session["cancelled_at"] = iso_now()
            session["cancelled_at_human"] = now_human()
            session["cancel_reason"] = "cancelled by `stop` before the scheduled start"
            write_json(Path(session["session_dir"]) / "metadata.json", session)
            write_session_note(session, vault)
            active.unlink(missing_ok=True)
            rebuild_board(vault)
            print(json.dumps({
                "status": "recording_cancelled",
                "title": session.get("title"),
                "session_dir": session.get("session_dir"),
                "cancelled_at_human": session.get("cancelled_at_human"),
                "board": str(board_path(vault)),
                "transcribed": False,
            }, indent=2))
            return
        pid = int(session.get("pid", 0) or 0)
        if pid and process_alive(pid):
            if not recorder_identity_verified(session):
                # Fail closed: never signal a PID/PGID we cannot confirm is
                # this recorder's own detached process (could be a reused
                # PID belonging to an unrelated process). Reconcile our own
                # bookkeeping so we stop blocking future work, but do not
                # touch the real process either way.
                session = finalize_stopped_session(session, vault)
                print(json.dumps({
                    "status": "recording_stop_unverified",
                    "title": session.get("title"),
                    "session_dir": session.get("session_dir"),
                    "message": (
                        f"PID {pid} could not be verified as this recording's own detached process "
                        "(process-group id and/or command line did not match what was recorded at launch); "
                        "refusing to signal it to avoid killing an unrelated or reused-PID process. "
                        "Bookkeeping was reconciled without touching that process -- inspect it manually "
                        f"with `ps -p {pid} -o pid,pgid,stat,command` before taking any further action."
                    ),
                    "board": str(board_path(vault)),
                }, indent=2))
                return

            # Escalate through graceful -> forceful signals so `stop` always
            # succeeds. SIGINT triggers the recorder's normal teardown (flushing
            # WAV headers, optionally mixing). If that doesn't return in time --
            # e.g. a long post-capture mix, or a wedged process -- escalate to
            # SIGTERM, then SIGKILL. The stt binary restores default signal
            # disposition during its teardown phase, and StreamingWAVWriter keeps
            # the on-disk WAV header crash-safe, so even a SIGKILL during capture
            # leaves playable, transcribable audio.
            signals = [
                (signal.SIGINT, args.timeout),   # graceful: flush headers + mix
                (signal.SIGTERM, args.term_timeout),  # forceful termination
                (signal.SIGKILL, 5.0),           # last resort, unrecoverable
            ]
            killed = False
            for sig, wait in signals:
                # Re-verify identity immediately before every signal: never
                # signal a process we can no longer confirm belongs to us.
                if not recorder_identity_verified(session):
                    break
                try:
                    os.killpg(os.getpgid(pid), sig)
                except ProcessLookupError:
                    killed = True
                    break
                deadline = time.time() + wait
                while time.time() < deadline and process_alive(pid):
                    time.sleep(0.25)
                if not process_alive(pid):
                    killed = True
                    break
            if not killed:
                # Extremely defensive: process somehow survived SIGKILL (e.g. zombie
                # being reaped, a permission issue, or identity became
                # unverifiable partway through escalation). Surface it rather
                # than hang or blindly keep signalling.
                raise SystemExit(
                    f"Recorder PID {pid} did not stop after SIGINT/SIGTERM/SIGKILL, or its identity could not "
                    "be re-verified partway through shutdown. Inspect the process manually before proceeding "
                    "(bookkeeping was left unchanged so it is not marked stopped while identity is uncertain)."
                )

        session = finalize_stopped_session(session, vault)

        print(json.dumps({
            "status": "recording_stopped",
            "title": session.get("title"),
            "session_dir": session.get("session_dir"),
            "audio_files": session.get("audio_files", []),
            "board": str(board_path(vault)),
            "transcribed": False,
        }, indent=2))


def status(args: argparse.Namespace) -> None:
    vault = vault_path()
    with VaultLock(vault):
        active = active_state_path(vault)
        if active.exists():
            recording = reconcile_active_recording(vault) or {"active": False}
        else:
            recording = {"active": False}

        transcription_active = active_transcription_state_path(vault)
        if transcription_active.exists():
            transcription = reconcile_active_transcription(vault) or {"active": False}
        else:
            transcription = {"active": False}

    payload = dict(recording)
    payload["transcription"] = transcription
    payload.setdefault("board", str(board_path(vault)))
    payload.setdefault("recordings_root", str(recordings_root(vault)))
    print(json.dumps(payload, indent=2))


def load_sessions(vault: Path) -> list[dict[str, Any]]:
    sessions = []
    for metadata in recordings_root(vault).glob("*/metadata.json"):
        try:
            reconcile_promotion_journal(vault, metadata.parent)
            session = load_json(metadata)
            session["metadata_path"] = str(metadata)
            sessions.append(session)
        except Exception:
            continue
    return sorted(sessions, key=lambda s: s.get("started_at", ""), reverse=True)


def list_recordings(args: argparse.Namespace) -> None:
    vault = vault_path()
    with VaultLock(vault):
        reconcile_active_recording(vault)
        reconcile_active_transcription(vault)
        sessions = load_sessions(vault)
    if args.pending:
        sessions = [
            s for s in sessions
            if s.get("status") not in ("recording", "transcribing")
            and not has_valid_final_transcript(Path(s.get("session_dir", "")))
        ]
    summary = [
        {
            "title": s.get("title"),
            "status": s.get("status"),
            "mode": s.get("mode"),
            "started_at": s.get("started_at_human") or s.get("started_at"),
            "session_dir": s.get("session_dir"),
            "has_transcript": has_valid_final_transcript(Path(s.get("session_dir", ""))),
            "audio_files": discover_audio_files(s),
            "attendees": s.get("attendees", []),
        }
        for s in sessions[: args.limit]
    ]
    print(json.dumps(summary, indent=2))


def choose_session(vault: Path, explicit: str | None) -> dict[str, Any]:
    root = recordings_root(vault).resolve()
    if explicit:
        path = Path(explicit).expanduser().resolve()
        metadata = path / "metadata.json" if path.is_dir() else path
        if not metadata.exists():
            raise SystemExit(f"No metadata.json found for session: {explicit}")
        try:
            metadata.resolve().relative_to(root)
        except ValueError:
            raise SystemExit(
                f"Refusing to use a session/metadata path outside the recordings root ({root}): {metadata}"
            )
        reconcile_promotion_journal(vault, metadata.resolve().parent)
        session = load_json(metadata)
        session["metadata_path"] = str(metadata.resolve())
        return session
    for session in load_sessions(vault):
        if session.get("status") in ("recording", "transcribing"):
            continue
        # Same validated check as `list --pending`: a session is only
        # "already transcribed" when a schema-valid canonical pair exists.
        # Relying on the metadata `transcript_path` field alone could skip
        # a session whose claimed transcript is missing/broken, or pick one
        # whose stale metadata still points at a now-removed file.
        if has_valid_final_transcript(Path(session.get("session_dir", ""))):
            continue
        if discover_audio_files(session):
            return session
    raise SystemExit("No stopped untranscribed recording with audio was found.")


def validate_session_for_transcription(session: dict[str, Any], vault: Path) -> dict[str, Any]:
    """Explicit session safety checks before transcription.

    Determines the session's *real* on-disk location from where its
    `metadata.json` actually lives (via the `metadata_path` hint set by
    `choose_session`/`load_sessions`), not from the possibly-tampered
    `session_dir` field inside the metadata content itself. Requires that
    real location to resolve under `recordings_root(vault)`, that the
    metadata content's own `session_dir` field matches that real location,
    and that the current status is not `recording` or `transcribing`.
    Returns the freshly-reloaded on-disk metadata (the canonical copy, not
    whatever the caller happened to pass in).
    """
    root = recordings_root(vault).resolve()
    metadata_path_hint = session.get("metadata_path")
    if metadata_path_hint:
        metadata_path = Path(metadata_path_hint).resolve()
        resolved = metadata_path.parent
    else:
        session_dir_raw = Path(session.get("session_dir", ""))
        try:
            resolved = session_dir_raw.resolve()
        except OSError as e:
            raise SystemExit(f"Session directory does not resolve: {session_dir_raw} ({e})")
        metadata_path = resolved / "metadata.json"
    try:
        resolved.relative_to(root)
    except ValueError:
        raise SystemExit(
            f"Refusing to transcribe a session outside the recordings root ({root}): {resolved}"
        )
    if not metadata_path.exists():
        raise SystemExit(f"No metadata.json found at {metadata_path}; refusing to transcribe.")
    reconcile_promotion_journal(vault, resolved)
    on_disk = load_json(metadata_path)
    on_disk_dir = Path(on_disk.get("session_dir", ""))
    try:
        on_disk_resolved = on_disk_dir.resolve()
    except OSError:
        on_disk_resolved = None
    if on_disk_resolved != resolved:
        raise SystemExit(
            f"metadata.json at {metadata_path} records session_dir={on_disk_dir!s}, which does not match "
            f"its own location ({resolved}); refusing to transcribe a possibly-tampered or mismatched session."
        )
    status_ = on_disk.get("status")
    if status_ in ("recording", "transcribing"):
        raise SystemExit(f"Session {resolved} is currently '{status_}'; cannot transcribe until it settles.")
    return on_disk


class TranscriptionInterrupted(Exception):
    """Raised when this process receives SIGINT/SIGTERM while a launch/wait runs.

    Despite the name (kept for backwards compatibility with existing call
    sites), this is also used by the recording-launch protocol in `start()`
    -- both launch protocols convert SIGINT/SIGTERM into this catchable
    exception instead of the default disposition.
    """


def _raise_transcription_interrupted(signum: int, frame: Any) -> None:
    raise TranscriptionInterrupted(f"received signal {signum}")


_LAUNCH_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def wrap_launch_command(cmd: list[str]) -> list[str]:
    """Wrap `cmd` so the real child process image is exec'd with
    SIGINT/SIGTERM unblocked, closing the child-side half of the blocked-
    signal launch protocol.

    `_blocked_signals` blocks SIGINT/SIGTERM in *this* process's signal mask
    across `Popen` + PID persistence so a signal cannot land in the gap
    between child creation and durable state being written. But a blocked
    signal mask is inherited by `fork()` and, critically, **survives
    `exec()`** per POSIX -- unlike installed handler dispositions (which
    `exec()` resets to default for any signal not set to `SIG_IGN`, but only
    because the old handler's code address is meaningless in the new
    program image; the *mask* itself is untouched by `exec()`). That means
    launching the real target directly while this parent has the launch
    signals blocked would hand the child that exact same blocked mask
    across the fork+exec, and the child would inherit it as **its own**
    process-wide signal mask for its entire lifetime -- the real `stt`
    child would then have SIGINT/SIGTERM permanently blocked, unable to be
    interrupted/terminated by exactly the signals `stop()` sends to its
    process group (SIGINT for graceful teardown, SIGTERM before the final
    SIGKILL escalation), forcing every stop to fall through to SIGKILL.

    The fix has to run *inside* the child, after `fork()` but before the
    real target is exec'd, since only code running in the child's own
    address space can change the child's own inherited signal mask. This
    wraps `cmd` so the immediate child is instead this same script,
    re-invoked via
    `[sys.executable, <this file>, "exec-unblocked", "--", *cmd]`: once
    that process starts, `exec_unblocked_shim` unblocks SIGINT/SIGTERM
    (`signal.pthread_sigmask(SIG_UNBLOCK, ...)`) and restores their default
    disposition, then `os.execv`s the real target in place of itself --
    replacing the process image but keeping the same PID, so every
    PID-based liveness/identity/kill-safety check elsewhere in this script
    is completely unaffected; the shim only exists for the brief instant
    between fork and that final `execv`, and its own argv (which still
    contains the real command, including the stt binary path and session
    directory) keeps process-identity matching working correctly even
    during that instant.
    """
    return [sys.executable, str(Path(__file__).resolve()), "exec-unblocked", "--", *cmd]


def exec_unblocked_shim(rest: list[str]) -> None:
    """Child-side half of the blocked-signal launch protocol.

    Entry point for the hidden `exec-unblocked` subcommand (see
    `wrap_launch_command`). Never invoked directly by a user; it is always
    the immediate argv of a `Popen` call made by `start()`/`transcribe()`
    while this script's own SIGINT/SIGTERM are blocked around the launch.

    Unblocks SIGINT/SIGTERM in this process's inherited signal mask (the
    actual fix -- a blocked mask otherwise survives `exec()` and would
    leave the real child permanently unable to receive those signals),
    restores their disposition to `SIG_DFL` for good measure (defensive:
    `exec()` already resets any non-`SIG_IGN` handler disposition to
    default on its own, since a Python callable's address means nothing in
    the new program image, but this makes the child-side reset explicit
    and correct even if some future caller changes how this shim is
    invoked), then replaces this process's image with the real target via
    `os.execv` -- preserving the PID.
    """
    if rest[:1] == ["--"]:
        rest = rest[1:]
    if not rest:
        raise SystemExit("exec-unblocked requires a command to run after `--`")
    if hasattr(signal, "pthread_sigmask"):
        try:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, _LAUNCH_SIGNALS)
        except (ValueError, OSError):
            pass
    for sig in _LAUNCH_SIGNALS:
        try:
            signal.signal(sig, signal.SIG_DFL)
        except (ValueError, OSError):
            pass
    os.execv(rest[0], rest)


@contextlib.contextmanager
def _blocked_signals(signals: tuple[int, ...] = _LAUNCH_SIGNALS):
    """Block `signals` for the duration of the context, if the platform allows it.

    Must be entered only after catchable handlers have already been
    installed (via `signal.signal`) for every signal in `signals` -- e.g.
    `_raise_transcription_interrupted` -- so that blocking-then-unblocking
    here defers delivery to *our own* handler rather than the process
    default (which, for SIGTERM, is immediate termination with no chance to
    clean up).

    Uses `signal.pthread_sigmask` (POSIX-only, and only callable from the
    main thread of the main interpreter) to genuinely defer signal delivery
    across a launch-and-persist critical section (`Popen` + PID/PGID
    persistence), closing the race where a signal lands after a child
    process has actually been created but before the `proc = ...`
    assignment (and the subsequent durable state writes) complete --
    without blocking, the interpreter could raise the catchable exception
    at exactly that point, leaving `proc` unassigned even though a real
    child now exists.

    Yields `True` if signals were actually blocked for the whole context,
    `False` if blocking was unavailable (no `pthread_sigmask` on this
    platform, not the main thread, or any other runtime restriction).
    Callers must treat `False` as a conservative fallback only: the
    catchable-handler protection installed separately still applies (a
    delivered signal still raises `TranscriptionInterrupted` instead of
    killing the process outright), but the specific "child created,
    assignment not yet complete" race is not fully closed in that case, so
    callers must never treat an unassigned local variable as proof that
    nothing was launched when this yielded `False`.
    """
    blocked = False
    if hasattr(signal, "pthread_sigmask"):
        try:
            signal.pthread_sigmask(signal.SIG_BLOCK, signals)
            blocked = True
        except (ValueError, OSError):
            blocked = False
    try:
        yield blocked
    finally:
        if blocked:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, signals)


def handle_transcription_interruption(
    session: dict[str, Any],
    vault: Path,
    log_path: str | Path,
    proc: subprocess.Popen[Any],
    run_id: str | None = None,
) -> dict[str, Any]:
    """Decide what to do when this helper is interrupted while `proc` is running.

    If the child is still alive, we deliberately leave it and the active
    state file alone -- the harness only killed the synchronous helper, not
    the background transcription -- and report `status: transcribing`,
    `helper_interrupted: true`, `active: true` (this is *not* a terminal
    interrupted state; the detached child is still doing real work). If the
    child is dead, the work genuinely stopped, so mark the session
    transcription_interrupted and clear the stale active-transcription state
    (only if still owned by `run_id`); partial per-track artifacts (if any)
    are left in the run's staging directory untouched.
    """
    session_dir = Path(session["session_dir"])
    if process_alive(proc.pid):
        return {
            "status": "transcribing",
            "helper_interrupted": True,
            "active": True,
            "child_alive": True,
            "session_dir": str(session_dir),
            "pid": proc.pid,
            "message": (
                f"Transcription helper was interrupted, but the stt child process (PID {proc.pid}) is still "
                f"running in the background for session {session_dir}. Active-transcription state was left "
                "unchanged; check `status` later or let it finish, then run `transcribe --resume`."
            ),
        }
    mark_transcription_interrupted(session, vault, log_path, run_id=run_id)
    return {
        "status": "transcription_interrupted",
        "helper_interrupted": True,
        "active": False,
        "child_alive": False,
        "session_dir": str(session_dir),
        "pid": proc.pid,
        "message": (
            f"Transcription was interrupted and the stt child process ended for session {session_dir}. "
            "Partial per-track artifacts (if any) were preserved in this attempt's staging directory. "
            "Run `transcribe --resume` to finalize if a valid transcript was already produced, or retry (a "
            "full rerun may overwrite partial staged output)."
        ),
    }


def try_resume_finalize(
    session: dict[str, Any],
    vault: Path,
    session_dir: Path,
    transcript_path: Path,
    transcript_json_path: Path,
) -> dict[str, Any] | None:
    """Conservative `--resume` finalize.

    Only trusts this session's own most-recently-recorded run: if
    `transcription_staging_dir` is set on the session's metadata, only that
    run's own staged output is validated and (if valid) promoted -- a
    missing/invalid staging directory for a modern, run-tracked attempt
    never falls back to canonical artifacts of unknown provenance.

    Only for legacy sessions with no `transcription_run_id`/
    `transcription_staging_dir` recorded at all does this fall back to
    accepting a schema-valid canonical transcript.md/json newer than
    `transcription_started_at`.

    Never merges partial per-track artifacts. Returns the JSON-ready result
    payload if finalized, or None if a full rerun is required.
    """
    track_pair = meeting_track_pair(session)
    last_run_id = session.get("transcription_run_id")
    last_staging = session.get("transcription_staging_dir")

    if last_staging:
        staging_path = Path(last_staging)
        if staging_path.exists():
            ok, _reason = validate_staged_outputs(staging_path, track_pair)
            if ok:
                try:
                    promote_run_outputs_if_owned(
                        vault, session, last_run_id, staging_path, session_dir, track_pair
                    )
                except ForeignRunOwnership:
                    # A later run has since taken ownership of this session;
                    # refuse to promote this stale run's output and fall
                    # through to "no valid artifacts to resume from".
                    return None
                audio_desc = session.get("transcribed_audio_path") or ""
                finalized = finalize_transcribed_session(
                    session, vault, transcript_path, transcript_json_path,
                    session.get("transcription_log_path"), audio_desc, run_id=last_run_id,
                )
                return {
                    "status": "transcribed",
                    "resumed": True,
                    "note": "Finalized this attempt's own staged transcript without invoking stt again.",
                    "title": finalized.get("title"),
                    "session_dir": str(session_dir),
                    "transcript": str(transcript_path),
                    "json": str(transcript_json_path),
                    "board": str(board_path(vault)),
                }
        # A run-specific staging path was recorded but is missing/invalid --
        # never fall back to possibly-stale canonical files for this attempt.
        return None

    # Legacy session: no run-specific staging info was ever recorded.
    if legacy_canonical_recoverable(session_dir, session.get("transcription_started_at")):
        audio_desc = session.get("transcribed_audio_path") or ""
        finalized = finalize_transcribed_session(
            session, vault, transcript_path, transcript_json_path,
            session.get("transcription_log_path"), audio_desc, run_id=last_run_id,
        )
        return {
            "status": "transcribed",
            "resumed": True,
            "note": (
                "Legacy session had no recorded run id; accepted a schema-valid canonical transcript newer "
                "than transcription_started_at."
            ),
            "title": finalized.get("title"),
            "session_dir": str(session_dir),
            "transcript": str(transcript_path),
            "json": str(transcript_json_path),
            "board": str(board_path(vault)),
        }
    return None


def transcribe(args: argparse.Namespace) -> None:
    vault = vault_path()

    with VaultLock(vault):
        active_recording = reconcile_active_recording(vault)
        if active_recording and active_recording.get("active"):
            raise SystemExit(
                "A recording is still active. Stop it before transcribing.\n"
                f"Active session: {active_recording.get('session_dir')}"
            )
        active_transcription = reconcile_active_transcription(vault)
        if active_transcription and active_transcription.get("active"):
            pid = int(active_transcription.get("pid", 0) or 0)
            raise SystemExit(
                f"A transcription is already active (PID {pid}) for session "
                f"{active_transcription.get('session_dir')}. Wait for it to finish, check `status`, or use "
                "`transcribe --resume` once it completes."
            )

        session = choose_session(vault, args.session)
        session = validate_session_for_transcription(session, vault)
        session_dir = Path(session["session_dir"])
        transcript_path = session_dir / "transcript.md"
        transcript_json_path = session_dir / "transcript.json"

        if args.resume:
            resumed = try_resume_finalize(session, vault, session_dir, transcript_path, transcript_json_path)
            if resumed is not None:
                print(json.dumps(resumed, indent=2))
                return
            print(
                "--resume: no valid run-specific (or, for legacy sessions, sufficiently new canonical) "
                "transcript.md + transcript.json was found; rerunning the full transcription command using "
                "the currently requested options (not necessarily identical to whatever command produced the "
                "interrupted attempt). Any partial per-track artifacts on disk are left untouched but may not "
                "be reused, because upstream stt cannot safely reconstruct diarisation/identification offsets "
                "from a partial run.",
                file=sys.stderr,
            )

        track_pair = meeting_track_pair(session)
        audio = best_audio_for_transcription(session) if track_pair is None else None
        stt_bin = find_stt_bin()

        input_paths = list(track_pair) if track_pair is not None else [audio]
        fingerprints = assert_audio_not_growing(input_paths)

        durations = gather_audio_durations(track_pair, audio)
        resolved_timeout = resolve_transcription_timeout(args.timeout, durations)

        run_id = uuid.uuid4().hex
        staging_dir = session_dir / ".stt-staging" / run_id
        staging_dir.mkdir(parents=True, exist_ok=True)
        staging_transcript = staging_dir / "transcript.md"
        staging_transcript_json = staging_dir / "transcript.json"

        if track_pair is not None:
            mic, system = track_pair
            cmd = [
                str(stt_bin),
                "transcribe-meeting",
                str(mic),
                str(system),
                "--output",
                str(staging_transcript),
                "--json",
                str(staging_transcript_json),
                "--device",
                args.device,
                "--timeout",
                str(resolved_timeout),
                "--model",
                args.model,
                "--max-new-tokens",
                str(args.max_new_tokens),
            ]
        else:
            cmd = [
                str(stt_bin),
                "transcribe",
                str(audio),
                "--output",
                str(staging_transcript),
                "--json",
                str(staging_transcript_json),
                "--device",
                args.device,
                "--timeout",
                str(resolved_timeout),
                "--model",
                args.model,
                "--max-new-tokens",
                str(args.max_new_tokens),
            ]
        backend = STT_REPO / "python"
        if backend.exists():
            cmd += ["--python-backend", str(backend)]
        if args.require_backend_ready:
            cmd.append("--require-backend-ready")

        # Diarisation + identification are default-on for meeting sessions
        # (separate mic/system tracks). They require the runtime venv (speechbrain
        # + torch + scipy), separate from the ASR venv (MLX-only).
        diarize_backend = runtime_backend_path()
        if track_pair is not None and args.diarize and diarize_backend is not None:
            cmd += ["--diarize", "--diarize-python-backend", str(diarize_backend)]
            # Identification only makes sense if profiles are enrolled.
            if args.identify and speaker_profiles_exist():
                cmd += ["--identify", "--identify-python-backend", str(diarize_backend)]
            elif args.identify and not speaker_profiles_exist():
                print("No speaker profiles found; skipping --identify.", file=sys.stderr)
        elif track_pair is not None and args.diarize and diarize_backend is None:
            print(
                "Runtime venv not found at runtime/.venv; skipping diarisation + identification. "
                "Run bootstrap in the stt-cli repo to enable speaker labelling.",
                file=sys.stderr,
            )

        audio_desc = "+".join(str(p) for p in track_pair) if track_pair is not None else str(audio)
        log_path = session_dir / "transcription.log"
        started_at = iso_now()
        log = log_path.open("ab")

        # `cmd` (the real stt command) is launched via a hidden
        # `exec-unblocked` shim (see `wrap_launch_command`) so the stt
        # child never inherits this launch's blocked SIGINT/SIGTERM signal
        # mask across exec -- without it, a blocked mask survives exec and
        # the child would be permanently unable to receive those signals.
        # `command` in the persisted state below still records the real,
        # unwrapped stt command (what actually matters for identity checks
        # and for the user); `launched_command` records what was actually
        # passed to `Popen` for debugging.
        launch_cmd = wrap_launch_command(cmd)

        # Persist the active-transcription reservation (session, run id,
        # staging paths, command, log, start time, requested options,
        # resolved timeout, input fingerprints) *before* Popen, so a signal
        # between here and PID persistence cannot orphan an untracked child
        # -- reconciliation only needs the reservation to exist, not the PID.
        active_payload = {
            "session_dir": str(session_dir),
            "title": session.get("title"),
            "mode": session.get("mode"),
            "run_id": run_id,
            "pid": None,
            "command": cmd,
            "launched_command": launch_cmd,
            "log_path": str(log_path),
            "staging_dir": str(staging_dir),
            "staging_transcript_path": str(staging_transcript),
            "staging_transcript_json_path": str(staging_transcript_json),
            "input_fingerprints": fingerprints,
            "started_at": started_at,
            "requested_options": {
                "device": args.device,
                "model": args.model,
                "max_new_tokens": args.max_new_tokens,
                "diarize": args.diarize,
                "identify": args.identify,
                "require_backend_ready": args.require_backend_ready,
                "resume": args.resume,
                "explicit_timeout": args.timeout,
            },
            "resolved_timeout": resolved_timeout,
            "active": True,
        }
        session["status"] = "transcribing"
        session["transcription_run_id"] = run_id
        session["transcription_started_at"] = started_at
        session["transcription_log_path"] = str(log_path)
        session["transcription_staging_dir"] = str(staging_dir)
        session["transcription_input_fingerprints"] = fingerprints
        write_json(session_dir / "metadata.json", session)
        write_json(active_transcription_state_path(vault), active_payload)
        write_session_note(session, vault)
        rebuild_board(vault)

        # Convert SIGINT/SIGTERM into a catchable exception across the whole
        # launch-and-PID-persist window (and later the wait), so an
        # interrupted helper still gets a chance to preserve/report state
        # instead of dying silently or leaving an untracked child. Only
        # possible from the main thread of the main interpreter; if that
        # fails (e.g. embedded/threaded callers, or tests), fall back to
        # relying on KeyboardInterrupt alone -- stale-state reconciliation is
        # the ultimate safety net regardless.
        previous_sigint = previous_sigterm = None
        signals_overridden = False
        try:
            previous_sigint = signal.getsignal(signal.SIGINT)
            previous_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGINT, _raise_transcription_interrupted)
            signal.signal(signal.SIGTERM, _raise_transcription_interrupted)
            signals_overridden = True
        except ValueError:
            pass

        def restore_signals() -> None:
            if signals_overridden:
                signal.signal(signal.SIGINT, previous_sigint)
                signal.signal(signal.SIGTERM, previous_sigterm)

        # Block SIGINT/SIGTERM (when possible) across the whole launch +
        # PID-persistence critical section below, only after the catchable
        # handlers above are installed, so a signal arriving in that window
        # cannot land between child creation and the `proc = ...` assignment
        # (or the subsequent durable state writes) -- it is deferred until
        # this `with` block exits, right after PID/metadata state is
        # durable, at which point it is delivered to our own catchable
        # handler instead of orphaning an untracked child.
        block_cm = _blocked_signals() if signals_overridden else contextlib.nullcontext(False)

        proc: subprocess.Popen[Any] | None = None
        launch_ambiguous = False
        try:
            with block_cm as signals_blocked:
                if not signals_blocked:
                    # Conservative fallback: true signal blocking is
                    # unavailable here (no `pthread_sigmask`, not the main
                    # thread, or handler installation itself failed). The
                    # catchable-handler protection still applies, but the
                    # child-created-before-assignment-completes race is not
                    # fully closed -- never treat `proc is None` below as
                    # proof nothing was launched in that case.
                    launch_ambiguous = True
                try:
                    proc = subprocess.Popen(
                        launch_cmd,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        cwd=str(STT_REPO),
                        start_new_session=True,
                    )
                except OSError as e:
                    restore_signals()
                    log.close()
                    mark_transcription_failed(
                        session, vault, log_path, f"failed to launch stt binary: {e}", run_id=run_id
                    )
                    raise SystemExit(f"Failed to launch stt binary: {e}")

                # Close the launch gap immediately: persist the real PID
                # before doing anything else, so an interrupt at this exact
                # point cannot orphan an untracked child -- the reservation
                # already on disk (with pid=None) still records enough
                # (run_id, staging dir, command) to reconcile from. While
                # `signals_blocked`, SIGINT/SIGTERM cannot be delivered at
                # all until this whole `with` block exits (immediately after
                # the writes below), so this window is fully closed rather
                # than merely narrowed.
                active_payload["pid"] = proc.pid
                write_json(active_transcription_state_path(vault), active_payload)
                session["transcription_pid"] = proc.pid
                write_json(session_dir / "metadata.json", session)
                write_session_note(session, vault)
                rebuild_board(vault)
        except (KeyboardInterrupt, TranscriptionInterrupted):
            restore_signals()
            log.close()
            if proc is None:
                if launch_ambiguous:
                    # No true blocking was available, so we cannot rule out
                    # that a child was actually created before this
                    # interrupt landed even though our own `proc` variable
                    # never got assigned. Never clear the reservation on
                    # pure speculation -- leave it in place (still naming
                    # this run_id, staging dir, and command) so a later
                    # `status`/reconcile call can inspect for an orphaned
                    # child rather than silently discarding the only record
                    # of what was launched.
                    raise SystemExit(
                        "Transcription launch was interrupted and, because signal blocking was unavailable "
                        "on this platform, whether the stt process actually started could not be confirmed; "
                        "leaving the active-transcription reservation in place rather than guessing. Run "
                        "`status` to check for an orphaned stt process before retrying."
                    )
                # True blocking was available and in effect for this whole
                # window, so reaching here with `proc is None` can only mean
                # the interrupt landed before Popen was even entered --
                # nothing was launched, so release our own reservation.
                clear_owned_reservation(vault, run_id)
                raise SystemExit(
                    "Transcription launch was interrupted before the stt process could be confirmed started; "
                    "no child was launched, and the reservation was released."
                )
            try:
                result = handle_transcription_interruption(session, vault, log_path, proc, run_id=run_id)
            except ForeignRunOwnership as e:
                raise SystemExit(
                    f"Transcription run {run_id} for session {session_dir} was interrupted, but a later run "
                    f"has since taken ownership of this session ({e}); left the still-running child process "
                    "alone and did not touch session metadata or the active-transcription reservation."
                )
            if result["child_alive"]:
                print(json.dumps(result, indent=2))
                return
            raise SystemExit(result["message"])

    # --- VaultLock released here; the long transcription wait happens
    # unlocked so it never blocks status/list/start/stop from other
    # invocations. Signal handlers remain installed across this wait. ---

    try:
        returncode = proc.wait()
    except (KeyboardInterrupt, TranscriptionInterrupted):
        restore_signals()
        log.close()
        with VaultLock(vault):
            try:
                result = handle_transcription_interruption(session, vault, log_path, proc, run_id=run_id)
            except ForeignRunOwnership as e:
                raise SystemExit(
                    f"Transcription run {run_id} for session {session_dir} was interrupted, but a later run "
                    f"has since taken ownership of this session ({e}); left the still-running child process "
                    "alone and did not touch session metadata or the active-transcription reservation."
                )
        if result["child_alive"]:
            print(json.dumps(result, indent=2))
            return
        raise SystemExit(result["message"])
    finally:
        restore_signals()
        if not log.closed:
            log.close()

    with VaultLock(vault):
        if returncode != 0:
            mark_transcription_failed(
                session, vault, log_path, f"stt exited with code {returncode}", run_id=run_id
            )
            raise SystemExit(f"Transcription failed with exit code {returncode}. See {log_path}")

        ok, reason = validate_staged_outputs(staging_dir, track_pair)
        if not ok:
            # Zero exit code is not sufficient: postconditions failed. Mark
            # transcription_failed with a clear reason and preserve the log
            # and staging directory for inspection; never treat any
            # pre-existing canonical artifacts as evidence of success.
            mark_transcription_failed(
                session,
                vault,
                log_path,
                f"stt exited 0 but produced invalid/missing output: {reason}",
                run_id=run_id,
            )
            raise SystemExit(
                f"Transcription reported success (exit 0) but output validation failed: {reason}. "
                f"See {log_path}; staged output (if any) remains at {staging_dir} for inspection."
            )

        try:
            promote_run_outputs_if_owned(vault, session, run_id, staging_dir, session_dir, track_pair)
            session = finalize_transcribed_session(
                session, vault, transcript_path, transcript_json_path, log_path, audio_desc, run_id=run_id
            )
        except ForeignRunOwnership as e:
            raise SystemExit(
                f"Transcription run {run_id} for session {session_dir} finished successfully, but a later "
                f"run has since taken ownership of this session ({e}); refusing to promote this run's staged "
                f"output or touch session metadata/active-transcription state. This run's staged output "
                f"remains untouched at {staging_dir} for inspection."
            )

    print(json.dumps({
        "status": "transcribed",
        "title": session.get("title"),
        "session_dir": str(session_dir),
        "audio": audio_desc,
        "transcript": str(transcript_path),
        "json": str(transcript_json_path),
        "board": str(board_path(vault)),
        "log": str(log_path),
        "resolved_timeout": resolved_timeout,
        "run_id": run_id,
    }, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage stt recordings in Obsidian")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("start", help="Start a background recording; does not transcribe")
    p.add_argument("--title", default=None, help="Recording/meeting title")
    p.add_argument(
        "--attendees",
        default=None,
        help=(
            "Expected attendee/invitee list (comma-separated; each entry 'Name <email>' or a bare email). "
            "Stored with the session as the diarisation whitelist for later speaker identification."
        ),
    )
    p.add_argument(
        "--attendees-source",
        choices=["calendar", "manual"],
        default="manual",
        help="Where the attendee list came from (calendar lookup vs manually provided); recorded for provenance.",
    )
    p.add_argument("--mode", choices=["meeting", "mic", "system"], default="meeting")
    p.add_argument("--duration", type=float, default=None, help="Optional finite recording duration in seconds")
    p.add_argument(
        "--start-at",
        default=None,
        help=(
            "Optional scheduled start: wall-clock time in HH:MM, HH:MM:SS, or an ISO-8601 datetime "
            "(e.g. 2026-08-12T15:00:00 or with a +10:00 offset). The helper creates the session and "
            "reservation immediately (status 'scheduled'), prints a 'scheduled' payload, waits until the "
            "start time, then launches the recorder. Conflicts with --delay."
        ),
    )
    p.add_argument(
        "--delay",
        type=float,
        default=None,
        help="Optional scheduled start: seconds from now (e.g. the meeting lookup's starts_in_seconds). Conflicts with --start-at.",
    )
    p.add_argument("--wait", action="store_true", help="Wait until the recording process exits, finalize state, then print completion details")
    p.add_argument("--wait-timeout", type=float, default=None, help="Maximum seconds to wait with --wait")
    p.set_defaults(func=start)

    p = sub.add_parser("stop", help="Stop the active recording")
    p.add_argument("--timeout", type=float, default=15.0, help="Seconds to wait after SIGINT (graceful stop)")
    p.add_argument("--term-timeout", type=float, default=10.0, dest="term_timeout", help="Seconds to wait after SIGTERM before escalating to SIGKILL")
    p.set_defaults(func=stop)

    p = sub.add_parser("status", help="Show active recording status")
    p.set_defaults(func=status)

    p = sub.add_parser("list", help="List recordings")
    p.add_argument("--pending", action="store_true", help="Only stopped recordings without a validated final transcript")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=list_recordings)

    p = sub.add_parser("transcribe", help="Transcribe a stopped recording")
    p.add_argument("--session", default=None, help="Session directory or metadata.json; defaults to latest pending")
    p.add_argument("--device", choices=["auto", "cpu", "gpu"], default="auto")
    p.add_argument(
        "--timeout",
        type=float,
        default=None,
        help=(
            "Per-track transcription timeout in seconds passed to stt. Default: adaptive -- "
            "max(1800, 4 * longest input WAV duration + 300), falling back to 1800 if a duration "
            "cannot be determined. An explicit value here always overrides the adaptive default."
        ),
    )
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--max-new-tokens", type=int, default=4096)
    p.add_argument("--require-backend-ready", action="store_true")
    p.add_argument("--no-diarize", dest="diarize", action="store_false", default=True,
                   help="Disable speaker diarisation (default: on for meeting sessions)")
    p.add_argument("--no-identify", dest="identify", action="store_false", default=True,
                   help="Disable speaker identification against enrolled profiles (default: on)")
    p.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Conservative resume: if this session's own last recorded run has a valid staged transcript "
            "(or, for legacy sessions with no recorded run id, a schema-valid canonical transcript newer than "
            "transcription_started_at), finalize it without invoking stt again. Otherwise rerun the full "
            "command using the currently requested options -- partial per-track artifacts are never merged/"
            "reused (upstream stt cannot safely reconstruct diarisation/identification offsets from a partial "
            "run) and a full rerun may overwrite them."
        ),
    )
    p.set_defaults(func=transcribe)

    p = sub.add_parser("monitor", help=argparse.SUPPRESS)
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--max-wait", type=float, default=None)
    p.set_defaults(func=monitor)

    p = sub.add_parser("exec-unblocked", help=argparse.SUPPRESS)
    p.add_argument("argv", nargs=argparse.REMAINDER)
    p.set_defaults(func=lambda args: exec_unblocked_shim(args.argv))
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
