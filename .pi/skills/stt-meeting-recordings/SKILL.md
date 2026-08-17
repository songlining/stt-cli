---
name: stt-meeting-recordings
description: 'Start, stop, catalogue, and later transcribe meeting or ad-hoc audio recordings using the local stt CLI without transcribing during capture. Use whenever the user asks to record a meeting/call/conversation/audio note, start or stop a recording, list pending meeting recordings, or transcribe a previously recorded meeting. Also use when the user says "record this", "start meeting recording", "stop recording", "transcribe that meeting", or asks to update the Obsidian meeting recordings kanban. When starting a recording, check the user''s Outlook calendar (via the outlook-cli skill) to identify the current or forthcoming meeting so the session is titled after the real meeting and timed recordings can be sized to its end. Also use for timed or one-shot meeting requests ("record this for 45 mins", "record this meeting for an hour"), which run the full pipeline: auto-stop the recording after the duration, transcribe, distill into a summary, file and update the customer''s latest meeting notes when a matching note exists in the vault, and remove the session''s .wav files.'
compatibility: macOS with the local stt CLI project at <stt-cli-repo> and Obsidian vault at ~/obsidian-notes.
disable-model-invocation: true
---

# STT Meeting Recordings

Use this skill to operate the local `stt` CLI in two deliberately separate modes:

1. **Record mode**: start or stop a recording and save audio into the Obsidian vault. Do **not** transcribe while recording; transcription can consume too much CPU/GPU and should only happen after the user asks.
2. **Transcribe mode**: transcribe a saved recording later, write transcript artifacts into the same session folder, and move/update its card on the Obsidian kanban board.

The bundled helper script handles repeatable details: choosing the local `stt` binary, creating session folders, creating/updating the Obsidian kanban board, tracking active recording PID/state, stopping via Ctrl-C/SIGINT, and selecting the right transcription strategy. For meeting recordings it transcribes `mic.wav` and `system.wav` separately, then merges the timestamped transcripts so overlapping microphone speech is not masked by system audio in `mixed.wav`.

## Default locations

- Obsidian vault: `~/obsidian-notes`
- Recordings root: `media/meeting-recordings/` inside the vault
- Kanban board: `meeting recordings.md` at the vault root
- Active recording state: `media/meeting-recordings/.active_recording.json` (written as a pre-launch reservation -- PID/PGID null, command/session/log already recorded -- *before* the recorder process is started, then atomically updated with the real PID/PGID immediately after launch, using the same blocked-signal launch protocol described under "Active transcription tracking" below -- including the child-side `exec-unblocked` shim that unblocks SIGINT/SIGTERM in the real recorder process before it execs, so a parent-side blocked signal mask around the launch never survives into the recorder itself). A scheduled recording (`start --start-at`/`--delay`) also writes this file with `status: "scheduled"` (pid still null) while it waits for the start time, so `status` reports it and a second `start` is refused; the schedule is cancelled via `stop` or SIGINT/SIGTERM
- Active transcription state: `media/meeting-recordings/.active_transcription.json` (session, a fresh UUID `run_id` for this attempt, child PID, command, log path, this run's own staging directory + staged output paths, start time, requested options, input audio fingerprints, and the resolved per-track timeout for any in-progress `transcribe` run)
- Per-run staging: `media/meeting-recordings/<session>/.stt-staging/<run_id>/` (the `stt` child writes its final transcript and per-track artifacts here first; they are only promoted into the session folder, via a journalled transactional promotion with rollback/crash recovery, after passing output validation)
- Vault lock: `media/meeting-recordings/.recordings.lock` (an `fcntl`-based advisory lock the helper takes internally around check-then-launch-then-reserve sequences and terminal state transitions; you should never need to touch it)

You may override the vault with `OBSIDIAN_VAULT=/path/to/vault` and the CLI with `STT_BIN=/path/to/stt` if needed.

## Record mode

### Meeting lookup (Outlook calendar)

Before starting a recording, resolve the real meeting from the user's Outlook calendar so the session is titled after what is actually happening (and so timed recordings can be sized to the meeting's end). Run:

```bash
python3 scripts/meeting_lookup.py
```

This wraps the outlook-cli skill's `outlook calendar --days 1 --timezone Australia/Sydney --json` command (the outlook-cli skill is the source of truth for the CLI; the helper just parses the JSON envelope) and prints one JSON object:

- `found: true` → `match` is the meeting currently in progress (`status: "current"`) or starting within the next few minutes (`status: "upcoming"`; default window 3 minutes, widen with `--within-minutes N`). `match` carries `subject`, `start`/`end`, `duration_seconds`, `starts_in_seconds` / `ends_in_seconds`, `location`, `organizer`, `attendees` (a list of `{"name", "address"}` objects — the meeting invite list, which doubles as the expected-speaker whitelist for later diarisation), and `online_meeting_url` (Teams/Meet join link) when present.
- `found: false` → no meeting is happening now or starting within the window; `next` still reports the first meeting after the window (or after now) for context.
- `ok: false` → the lookup itself failed (outlook not installed/authenticated, timeout, non-JSON). Treat it as "no meeting found": fall back to a user-provided title or the timestamped default, and note the lookup failure in your report rather than blocking the recording.

**Capture the invite list with the session (the diarisation whitelist).** Whenever a meeting is matched (and also when the user supplies an invite list themselves), pass the attendees to `start` with `--attendees` so the expected-speaker whitelist is persisted with the recording:

```bash
# Build the flag from the lookup's match.attendees (each 'Name <email>', comma-separated)
python3 scripts/meeting_lookup.py
# → match.attendees = [{"name": "Alice", "address": "alice@example.com"}, ...]
python3 .../recordings.py start --title "<subject>" --mode meeting \
  --attendees "Alice <alice@example.com>, bob@example.com" --attendees-source calendar
```

The list is stored as `attendees` in the session's `metadata.json` (with `attendees_source`, `calendar` vs `manual`, for provenance) and shown under **Attendees (expected speakers)** in `session.md`. Later, during transcription/diarisation, this whitelist tells the agent who to expect so speakers can be cross-checked against invitees — see "Speaker diarisation & identification" below. When a matched meeting has no attendees or the lookup failed, note that the whitelist is empty rather than inventing names.

**Title precedence for `start --title`:** explicit user title (use it; you may still run the lookup to cross-check the meeting) → matched meeting `subject` from the lookup → timestamped fallback (`Untitled recording`). When the user gives no title and the lookup matches, use the subject verbatim (optionally trimmed) so the kanban card and session folder reflect the real meeting.

**Timed recordings sized from the meeting** — when the user asks to record "this meeting", with or without a stated duration:
- with an explicit duration → honor the user's number;
- without one and `match.status == "current"` → `--duration` = seconds until the meeting ends (`ends_in_seconds`, rounded up to the next minute);
- without one and `match.status == "upcoming"` → `--duration` = `duration_seconds` and schedule the start with `--start-at "<match.start>"` (or `--delay <starts_in_seconds>`) so the recorder launches when the meeting begins and captures the whole meeting. The helper handles the "wait to start" — the `stt` recorder itself has no delayed-start option.

Never add more than a couple of minutes of padding beyond the meeting end unless the user asks — the point is to capture the meeting, not arbitrary extra audio.

**Scheduled / delayed start (`start --start-at` / `--delay`)** — when the user wants a recording to begin at a specific time rather than now ("start at 15:00, not now", "the meeting is in 6 minutes"):

```bash
# Wall-clock start: HH:MM, HH:MM:SS, or ISO-8601 datetime (meeting_lookup.py's match.start)
python3 scripts/recordings.py start --title "<title>" --mode meeting \
  --attendees "<invite list>" --attendees-source calendar --start-at "2026-08-12T15:00:00+10:00" --duration 3600

# Or a relative delay in seconds (e.g. meeting_lookup.py's starts_in_seconds)
python3 .../recordings.py start --title "<title>" --mode meeting --delay 300 --duration 3600
```

- Only one of `--start-at` / `--delay` may be given, and the requested start must be in the future (a past `--start-at` is rejected).
- The helper immediately creates the session and a pre-launch reservation (`status: "scheduled"`, pid null) and prints a `scheduled` JSON payload (title, session dir, `scheduled_start_at`, `starts_in_seconds`, `expected_stop_at` when a duration is given, board path). It then waits for the start time **without holding the vault lock**, so `status` shows the schedule (as `active: true`, `status: "scheduled"`, with `starts_in_seconds`) and a second `start` is refused while it is pending.
- The kanban card sits in `Recording` while scheduled: `scheduled for 2026-08-12 15:00`.
- **Cancelling a schedule:** `stop` (or SIGINT/SIGTERM to the waiting helper) cancels a scheduled recording cleanly — nothing has been launched yet, so there is no recorder process to signal; the reservation is cleared, the session becomes `scheduled_cancelled` and moves to `Failed / Needs Attention` with the cancel reason on the card. If the helper is killed hard mid-wait (e.g. a harness timeout), a later `status`/`list` reconciles the stale reservation once its start time has passed (with a grace window) into `scheduled_cancelled`.
- A scheduled start can be combined with `--duration` (recorder auto-stops at start+duration) and with `--wait` (the helper stays attached through the wait and the whole recording, so budget the tool-call timeout as delay + duration + margin). Report the `scheduled` payload first (title, session dir, expected start and stop), then wait for the helper and report the `recording_finished` details as usual; transcription is not started.

**Timezone caveat:** the outlook CLI's global config sets `timezone: AUS Eastern Standard Time`, a Windows tz ID that the `calendar` command rejects ("Unknown timezone: AUS Eastern Standard Time"). Always pass an IANA name explicitly (`--timezone Australia/Sydney`) — the helper does this by default. Calendar `start`/`end` come back already in that local timezone (AEST, +10:00), so no manual offset conversion is needed.

### Start recording

When the user asks to start recording, first resolve the real meeting with the meeting lookup (see "Meeting lookup (Outlook calendar)" above): run `meeting_lookup.py` and use the matched meeting's `subject` as the recording title when the user did not provide one; if the user gave an explicit title, prefer it but still run the lookup to confirm the actual meeting. If nothing matches (or the lookup fails), use the user's title if given, otherwise a timestamped fallback like `Untitled recording`. Mention the generated session folder and, when a meeting was matched, its subject and time.

When in doubt, or when the user does not explicitly specify a mode, **always default to `meeting` mode** — including for tests, smoke tests, and ambiguous requests like "测试"/"test this". `meeting` mode is the catch-all default for meetings, calls, webinars, demos, conversations, and general recording requests. Only use `mic` for quick voice notes or when the user explicitly asks for microphone-only capture, and `system` only when the user explicitly asks for system audio only.

### How system audio is captured (meeting/system mode)

`meeting` and `system` modes capture system output (YouTube, Zoom/Teams/Meet participants, shared audio) using the **native macOS CoreAudio process tap** (macOS 14.4+ `AudioHardwareCreateProcessTap`). This is built into the local `stt` CLI and **does not require BlackHole or any virtual loopback device** on this machine — verified working on Apple Silicon. BlackHole is only an automatic fallback if the native tap is unavailable or fails.

A `meeting` recording produces `mic.wav` + `system.wav` only. The `stt record --mode meeting` command no longer generates `mixed.wav` at all (mixing was moved out of the recording process so that stopping is always fast). If you ever want a combined reference mix, run it separately afterwards: `stt mix <session>/mic.wav <session>/system.wav --output <session>/mixed.wav`. Transcription uses separate mic/system passes and merges the timestamped transcript segments; overlapping microphone speech is not masked by system audio.

The first time a meeting/system recording runs, macOS may show a one-time **System Audio Recording** permission prompt attributed to the `stt` app bundle (`com.larrysong.stt`). The user must approve it once; afterwards system audio is captured silently. If the prompt was denied, system audio will be silent/empty — in that case direct the user to System Settings → Privacy & Security → System Audio Recording and re-enable `stt`.

Run:

```bash
python3 scripts/recordings.py start --title "<title>" --mode meeting --attendees "<invite list from lookup>" --attendees-source calendar
```

Pass `--attendees` whenever the meeting lookup (or the user) provides an invite list — it becomes the session's diarisation whitelist (see "Meeting lookup (Outlook calendar)" above and "Speaker diarisation & identification" below). If the lookup found no meeting and the user gave no list, omit `--attendees` and say so.

Optional examples:

```bash
# Microphone-only note
python3 scripts/recordings.py start --title "Voice note" --mode mic

# If user needs a finite smoke test rather than a real meeting, wait and report completion
python3 scripts/recordings.py start --title "Smoke test" --mode meeting --duration 10 --wait
```

Report back with:

- title
- mode
- session folder
- PID
- kanban board path
- captured attendee whitelist (names/emails) or an explicit note that none was captured
- explicit note: "Recording started; transcription has not been started."

For finite smoke tests (`--duration N --wait`), wait until the helper returns and report both start and finish details, including audio files produced, `active: false`, kanban moved to `To Transcribe`, and the explicit note that transcription has not been started.

For scheduled starts (`--start-at`/`--delay`), report the `scheduled` payload first — title, session folder, scheduled start time, seconds until start, expected stop (when a duration is given), kanban board path — and note that the recorder has **not** launched yet. After the helper finishes, report the launch/finish details as above (for finite schedules) and confirm the kanban card moved out of `Recording` (to `To Transcribe` once stopped, or `Failed / Needs Attention` if the schedule was cancelled).

Before launching the recorder, the helper writes a pre-launch reservation to `.active_recording.json` and the session's own `metadata.json` (PID/PGID `null`, but session dir/command/log path already recorded) using the same blocked-signal launch protocol as `transcribe` (see "Active transcription tracking" below, including the child-side `exec-unblocked` shim), then atomically persists the real PID/PGID immediately after the recorder process starts, before unblocking signals. If the recorder process itself fails to launch (e.g. the `stt` binary cannot be executed), the session is marked `recording_failed` with a `failure_reason` and moves to the `Failed / Needs Attention` kanban lane, and only the reservation this launch attempt itself created is cleared.

### Stop recording

When the user asks to stop the current recording (or to cancel a recording that is scheduled but has not started yet):

```bash
python3 scripts/recordings.py stop
```

For a live recording this stops the recorder (SIGINT → SIGTERM → SIGKILL escalation, finalizing the session regardless). For a **scheduled** recording (`status: "scheduled"`, pid null, recorder not yet launched) `stop` instead **cancels the schedule**: it reports `recording_cancelled`, clears the reservation, marks the session `scheduled_cancelled` and moves it to `Failed / Needs Attention` — no recorder process exists to signal, so cancellation is immediate and the waiting helper aborts without launching.

Report back with:

- session folder
- audio file(s) produced (`mic.wav` + `system.wav` for meeting mode; `mixed.wav` is **not** auto-generated — mention `stt mix` if a combined reference track is wanted)
- kanban status moved to `To Transcribe` (live recordings) or `Failed / Needs Attention` with `recording_cancelled` (cancelled schedules)
- reminder that they can ask this same skill to transcribe later

### Timed recording (auto-stop after N minutes)

When the user asks for a recording with a set duration — "record this for 45 mins", "for an hour", "a 90-minute meeting" — parse the duration into seconds (45 min → 2700s, 1 h → 3600s, 1.5 h → 5400s) and start with `--duration` and `--wait` so the recorder stops itself and the helper finalizes the session without a separate `stop`. If the user asks to record "this meeting" without stating a duration, size `--duration` from the meeting lookup instead (see the sizing rule under "Meeting lookup (Outlook calendar)").

```bash
python3 scripts/recordings.py start --title "<title>" --mode meeting --attendees "<invite list>" --attendees-source calendar --duration 2700 --wait
```

- `--duration <seconds>` is passed to the `stt` recorder, which stops itself once the time elapses; no `stop` invocation is needed.
- `--wait` keeps the helper attached until the recorder exits, then finalizes the session (kanban card → `To Transcribe`) and prints completion details. The default wait budget already scales with the duration (`max(duration + 120s, duration × 2)`); only pass `--wait-timeout` to override it.
- Report start details right away (title, mode, session folder, PID, expected stop time, kanban board path), then wait for the helper, then report finish details: audio files produced, `active: false`, kanban moved to `To Transcribe`, and the explicit note that transcription has not been started.
- If a recording is already active, `start` is refused — show `status` and ask whether to stop the existing recording first (never kill it yourself). If the refusal says a recording is **scheduled**, `stop` cancels the schedule (see "Stop recording").
- A timed recording can be combined with a scheduled start (`--start-at`/`--delay`) so it launches at the meeting time and auto-stops at start+duration; see "Scheduled / delayed start" above.

### Status/list pending recordings

```bash
python3 scripts/recordings.py status
python3 scripts/recordings.py list --pending
```

Use `status` when checking the active recording. Use `list --pending` when the user asks what recordings are waiting for transcription; plain `list` includes completed sessions too. `--pending` checks for a real, validated final transcript (existing, non-empty, schema-valid JSON) rather than trusting a `transcript_path` metadata field alone, so a session with a broken or missing transcript still shows up as pending even if stale metadata claims otherwise; `has_transcript` in `list`'s JSON output reflects the same validated check. Each session in `list` also carries its captured `attendees` whitelist (empty list when none was captured), so you can see which sessions have a known expected-speaker set before transcribing.

`status` now reports both recording and transcription state in one JSON payload: the top-level fields are unchanged (recording `active`, PID, session, etc.), and a nested `"transcription"` object reports whether a `transcribe` run is currently active (with its PID, run id, session, and resolved timeout) or, if one is not, the outcome of reconciling any stale state (`transcribed`, `transcription_failed`, or `transcription_interrupted`). Every invocation of `status`, `start`, `list`, and `transcribe` reconciles `.active_transcription.json` first, under an internal vault-scoped lock so two invocations can't race each other: if the recorded child PID is still alive (and, best-effort, its process command line still references the session path), the transcription is genuinely in progress and duplicate work is refused; if the PID is dead, the helper validates *that specific run's own staged output* (JSON shape, non-empty Markdown) and promotes + finalizes as `transcribed` only if it passes, otherwise marks it `transcription_interrupted` and clears the stale state file — an old canonical transcript left over from a previous run is never mistaken for this run's success. The same invocations also reconcile `.active_recording.json`: a `scheduled` reservation whose start time has already passed (the waiting helper died, e.g. a harness timeout) is cancelled into `scheduled_cancelled` so it stops blocking new work, while a future-dated schedule is reported as `active: true` with `starts_in_seconds`. `status` reflects whatever is actually persisted: it reports `transcription_failed` with a `failure_reason` only because the helper genuinely writes that reason to `metadata.json` on postcondition failure, not as a general promise beyond what's implemented.

## Transcribe mode

**Agent-harness timeout requirement:** `transcribe` runs synchronously and can legitimately take well over an hour for long meetings (adaptive timeout, see below). When invoking `transcribe` through this skill from an agent harness, set the outer/tool-call timeout generously and scale it to the input: the minimum is **10800 seconds (3 hours)** for a single meeting, but when the calculated requirement is higher use at least `track_count * resolved_per_track_timeout + 900` (e.g. a 2-track meeting session with a 3-hour resolved per-track timeout needs at least `2 * 10800 + 900 = 22500` seconds, not a flat 10800). If you don't yet know the resolved timeout, estimate it from the adaptive formula below using the longest input's duration, or query `status`/a prior run's `resolved_timeout` first. If the harness's own timeout fires first, it only kills this synchronous helper process — it does **not** signal the background `stt` child process, which is launched detached (`start_new_session=True`) and is *designed* to keep running and possibly finish and write a valid transcript on its own; this is not a guarantee, since parent/group-timeout behavior can vary by harness, so always verify rather than assume. After a harness timeout, run `status` (to see if the child is still active) or `transcribe --resume` (to finalize if it already finished, or safely retry otherwise) rather than assuming the transcription failed or succeeded.

Only transcribe after the user explicitly asks. Never start transcription as part of `start` or while a recording is still active.

If the user does not specify which recording, transcribe the most recent stopped recording that does not already have a transcript:

```bash
python3 scripts/recordings.py transcribe
```

If they specify a session folder:

```bash
python3 scripts/recordings.py transcribe --session "~/obsidian-notes/media/meeting-recordings/<session>"
```

For `meeting` sessions, the helper transcribes `mic.wav` and `system.wav` separately with `stt transcribe-meeting`, then merges timestamped segments into one transcript. **Speaker diarisation and identification are on by default** — the helper automatically passes `--diarize --identify` using the runtime venv, so each speaker cluster is matched against enrolled profiles and labeled with real names when a confident match exists. Pass `--no-diarize` or `--no-identify` to opt out. If no speaker profiles are enrolled yet, identification is skipped automatically (diarisation still runs, producing `Speaker 0`, `Speaker 1`, … labels). If one meeting track is genuinely missing/empty, or for `mic`/`system` sessions, it falls back to the best single audio file in this order:

1. `mixed.wav`
2. `mic.wav`
3. `system.wav`
4. any single `.wav` in the session folder

It writes (into a fresh per-attempt staging directory first, then promotes into the session folder only after passing output validation, using a journalled transactional promotion with rollback/crash recovery -- see "Canonical artifact promotion" below):

- `transcript.md` (must be non-empty)
- `transcript.json` (must parse and have the expected shape: a dict with a `segments` list, or a supported top-level list; includes `speaker_id` and `speaker_name` fields when diarisation/identification succeed)
- for meeting mode, per-source artifacts such as `transcript.mic.txt` / `transcript.mic.json` and `transcript.system.txt` / `transcript.system.json`
- updates `session.md`
- moves the kanban card to `Transcribed`

A zero exit code from `stt` is not treated as success on its own: if the staged output is missing, empty, or schema-invalid, the session is marked `transcription_failed` with a clear `failure_reason` instead, and the transcription log plus the run's staging directory are preserved for inspection — never delete them yourself without checking first.

Report back with transcript path, JSON path, speaker-labeling outcome (e.g. "4 speakers diarised, 3 identified"), and relevant warnings. The helper's final JSON reports artifact paths but not those counts or captured CLI warnings: inspect distinct `speaker_id` / non-empty `speaker_name` values in `transcript.json`, and read `transcription.log` before reporting the outcome.

To transcribe without speaker labelling (faster, plain transcript only):

```bash
python3 scripts/recordings.py transcribe --no-diarize
```

### Adaptive per-track timeout

`transcribe` passes `--timeout` to `stt` for each track it processes. By default (no `--timeout` given) the helper computes an **adaptive** timeout from the actual input WAV duration: `max(1800, 4 × longest_input_duration_seconds + 300)`, falling back to a flat `1800` seconds if a WAV's duration cannot be read. Passing an explicit `--timeout <seconds>` always overrides the adaptive value. The helper's final JSON (and the active-transcription state file while running) reports `resolved_timeout` so you can tell the user what limit was actually applied — report it, especially for long recordings.

### Active transcription tracking, duplicate prevention, and interruption

While `transcribe` runs, the helper writes `media/meeting-recordings/.active_transcription.json` (atomically, under the internal vault lock) *before* launching the `stt` child process — including a fresh `run_id`, this run's own staging directory and staged output paths, and input audio fingerprints (path/size/mtime) — then atomically updates it with the real child PID immediately after launch. SIGINT/SIGTERM are converted into a catchable interruption for this whole window, and (on platforms with `signal.pthread_sigmask`, the normal case on macOS) genuinely *blocked* across the `Popen` call and the subsequent PID/metadata persistence, closing the race where a signal lands after the child is created but before that state is durable; only once the reservation and metadata durably reflect the real PID are the signals unblocked, so a signal delivered right at that point is still caught with the child fully tracked rather than orphaned. On a platform without `pthread_sigmask` the catchable-handler protection still applies, but the helper falls back to a conservative rule: it never discards the pre-launch reservation just because its own interrupted code path didn't get as far as recording the PID, since a real child may already exist. The session's own `metadata.json`/`session.md`/kanban card move to a `transcribing` state at the same time. If another `transcribe` (or `start`) is attempted while this state shows a genuinely alive, matching process, it is refused with the active session's details — the vault lock ensures two concurrent attempts can't both observe "nothing active" and both launch; check `status` instead of re-running blindly.

Blocking the parent's own signal mask around the launch is only half of the problem: a *blocked* signal mask (unlike an installed handler, which `exec()` resets to default) survives `exec()` per POSIX, so simply launching the real `stt` binary directly while this mask is blocked would hand it down across fork+exec as the child's own, permanently blocked mask — the real recorder/transcription process would then be unable to ever receive SIGINT/SIGTERM, exactly the signals `stop()` sends to its process group, forcing every stop straight to an unrecoverable SIGKILL. Both `start()` and `transcribe()` avoid this with a hidden child-side unblocking shim: the actual `Popen` target is not the real `stt` command directly but this same script re-invoked as `python3 recordings.py exec-unblocked -- <real stt command...>`. That shim process immediately unblocks SIGINT/SIGTERM in its own inherited mask (`signal.pthread_sigmask(SIG_UNBLOCK, ...)`), restores their default disposition, and then `os.execv`s the real `stt` binary + arguments in its own place — replacing the process image but keeping the same PID throughout, so PID-based liveness/identity/kill-safety checks elsewhere are unaffected and the recorder/transcription child is reachable by SIGINT/SIGTERM again from the moment it actually starts running. The persisted `command` field (used for identity checks and reporting) always records the real, unwrapped `stt` invocation; a `launched_command` field records what was actually passed to `Popen` (the shim-wrapped form) for debugging.

The `stt` child writes its final transcript and per-track artifacts into this run's own staging directory (`<session>/.stt-staging/<run_id>/`), never directly into the session folder. Only after that specific run's output passes validation (JSON shape, non-empty Markdown) is it promoted into the session folder as the canonical `transcript.md`/`transcript.json` (+ per-source artifacts) via the journalled transactional promotion described below. A stale or invalid staging directory for a tracked run is never papered over by an older canonical transcript that happens to already sit in the session folder from a previous attempt.

If `transcribe` itself is interrupted (SIGINT/SIGTERM, e.g. an agent harness timeout) while the `stt` child is still running, the active state is left untouched and the helper reports `status: transcribing`, `helper_interrupted: true`, `active: true` — this is deliberately **not** a terminal `transcription_interrupted` state, since the detached child is still doing real work; check `status` later. If the child has already exited by the time the interruption is handled (or a later invocation reconciles stale state left by an ungraceful kill), the session is marked `transcription_interrupted` (with a dedicated `interrupted_at` timestamp, and the stale `transcription_pid`/`transcription_started_at` fields cleared) and the stale active-state file is cleared — any partial per-track artifacts already on disk are preserved untouched in that run's staging directory.

### Canonical artifact promotion (journalled transaction, not filesystem atomicity)

Promoting a validated run's staged output into the session root can mean replacing several files at once (`transcript.md`, `transcript.json`, and per-source artifacts for meeting sessions). There is no OS primitive that replaces several independent files as a single atomic operation, so the helper does **not** claim true filesystem multi-file atomicity for this set. Instead it uses a journalled transaction with rollback and crash recovery:

1. Before touching any canonical file, it prepares every destination's new-content temp file and, for any destination that already exists, a backup copy of its current content, and durably records all of that (destinations, temps, backups, and whether each destination existed) in a per-session promotion journal.
2. Only after that journal is durable does it replace each destination in turn, marking each one as replaced immediately afterwards. The journal also stores a digest of every prepared new file so crash recovery can detect a completed replace even if the process died in the tiny window before its `replaced` flag was persisted.
3. On full success, the whole set has been replaced; the journal, backups, and temp files are then cleared.
4. If a failure is caught partway through, every already-replaced destination is rolled back to its pre-promotion state (restored from its backup, or removed if this promotion would have created it fresh) so the canonical set is never left showing a mix of old and new files; the run's own staging directory is untouched throughout.
5. If the helper (or the machine) is killed mid-promotion, the journal is left on disk. The next helper invocation that reads or finalizes that session's state (e.g. `status`, `list`, or another `transcribe`) reconciles it first, using both the journal flags and the stored content digests: if every destination was already replaced before the crash, it keeps the new content and just finishes cleanup; otherwise it rolls back every destination that actually contains promoted bytes (leaving destinations never reached alone) before proceeding. All of this runs under the same vault-scoped lock and run-ownership checks as every other state transition.

You should never need to touch a leftover `.promotion_journal.json` or its backup/temp files yourself — run `status` or `list` (or retry `transcribe`) and the helper will reconcile it automatically.

### Resuming after an interruption or failure

```bash
python3 scripts/recordings.py transcribe --resume --session "<session>"
```

`--resume` is deliberately conservative and only ever trusts *this session's own most recently recorded run*:

1. If the session's metadata still records a `transcription_staging_dir` for its last attempt, `--resume` validates only that run's own staged output (JSON shape, non-empty Markdown). If valid, it promotes and finalizes as `transcribed` immediately **without invoking `stt` again**. If that staging directory is missing or invalid, `--resume` never falls back to some older canonical `transcript.md`/`transcript.json` that might already be sitting in the session folder from an earlier, unrelated run — it goes straight to a full rerun instead.
2. Only for **legacy sessions** with no recorded run id at all (created before this run-tracking existed) does `--resume` fall back to accepting a schema-valid canonical `transcript.md` + `transcript.json` pair, and only if both files are newer than the recorded `transcription_started_at` — never based on age/existence alone.
3. Otherwise, `--resume` reruns the full transcription command **using the currently requested options** (whatever `--device`/`--model`/`--diarize`/etc. flags this invocation was given) — this is not necessarily identical to whatever command produced the original interrupted attempt, since the caller may have changed flags between attempts. Say so if you changed any options before resuming.

It does **not** attempt to merge or reuse partial per-track artifacts (e.g. a completed mic-track pass but no system-track pass): upstream `stt` cannot safely reconstruct diarisation/identification segment offsets from a partial run, so any partial artifacts are retained on disk (in that run's staging directory) but a full rerun may overwrite them. Tell the user this before resuming a session that has partial-looking output you want to keep.

## One-shot timed meeting pipeline

When the user asks in a single request to record a meeting **for a set duration and then handle everything afterwards** — e.g. "/skill:stt-meeting-recordings record this meeting for 45 mins" — run the whole pipeline in order: timed recording with auto-stop, transcription, distilling into a summary, filing and updating the latest meeting notes (only when a matching customer note exists in the vault), then removing the session's .wav files. Confirm each stage completes before starting the next; transcription takes longer than the meeting itself, so do not assume it finished just because the recording did.

1. **Record with auto-stop.** Resolve the meeting with the meeting lookup ("Meeting lookup (Outlook calendar)") to get the title, the invite list (pass as `--attendees "<names/emails>" --attendees-source calendar` so the diarisation whitelist is captured with the session), and, if the user gave no duration, size it from the meeting's end. Start as described under "Timed recording": `start --title "<title>" --mode meeting --attendees "<invite list>" --attendees-source calendar --duration <seconds> --wait`. When the user asks to record a meeting that has not started yet ("record this meeting", "the QBR at 15:00"), also pass `--start-at "<match.start>"` (or `--delay <starts_in_seconds>`) so the recorder launches at the meeting time; report the `scheduled` payload (expected start/stop), then wait for the helper and verify `active: false` and the kanban card in `To Transcribe`.

2. **Transcribe.** Run `transcribe --session "<session>"` with the generous agent-harness timeout described under Transcribe mode (at least 10800s, scaled up for long meetings). Wait for a `transcribed` outcome. On `transcription_failed` / `transcription_interrupted`, follow the failure-handling steps (read the log, `transcribe --resume`) before continuing — never file or delete anything from a session whose transcript did not actually land.

3. **Distill.** Read `<session>/transcript.md` (and the `speaker_name` labels in `transcript.json`) and write `<session>/summary.md`: a distilled, technically precise summary with frontmatter (title, meeting date, session link), a short executive summary, confirmed speakers, key points / decisions / questions, and an action-item list. Link the source session and full transcript. The helper never writes this file — it is the agent's distillation step.

4. **File & update the latest meeting notes (only if a customer note exists).** Look for the customer's running meeting note under `raw/HashiCorp/customers/<Customer>/<series>.md` in the Obsidian vault, deriving the customer from the meeting title (e.g. a title like "ACME cadence" → `raw/HashiCorp/customers/ACME/ACME cadence.md`). If the customer folder or a matching series note is missing, **skip this step entirely** — do not invent a folder or create a note the user did not ask for — and say so in the final report. When a matching note exists, append a dated `# YYYY-MM-DD` section to its **end** with a link to the session transcript/summary plus the distilled key points, decisions, and TODOs. Preserve existing content; never rewrite or reorder earlier meeting entries.

5. **Remove the .wav files (always, whether or not step 4 ran).** The one-shot request is explicit pre-authorization to remove **this session's own top-level audio** (`mic.wav` + `system.wav`). Still follow "Sound file cleanup": run `status` and require recording `active: false` and `transcription.active: false`; validate the transcript/summary are present and substantive; enumerate the exact files and total size; then move them to **macOS Trash** with `trash` (never permanent deletion). Update `metadata.json` (`audio_files`, `audio_cleanup_events`, `source_audio_removed_at` / `source_audio_removed_at_human` / `source_audio_removal_reason`) and the `Audio files` section of `session.md`; keep the kanban card in `Transcribed`. Never trash `.stt-staging/`, `.speaker-clips/*.json`, provenance files, or canonical speaker-profile samples.

Report each stage as it completes: start (title, session folder, PID, expected stop time), finish (audio files, `active: false`), transcription (transcript paths, speaker-labelling outcome), distill (`summary.md` path), filing (customer note + section, **or** that no matching customer note was found and the step was skipped), cleanup (files moved to Trash, reclaimed size).

## Speaker diarisation & identification

The session's captured `attendees` whitelist (see "Meeting lookup (Outlook calendar)" — stored in `metadata.json` and `session.md` as **Attendees (expected speakers)**) is the reference set for who should be in the recording. Use it when interpreting diarisation output: identified speakers that match an invitee are expected; `Speaker N` clusters should be checked against the whitelist first, and names not on the list flagged rather than silently accepted. If a whitelisted attendee appears to be missing from the transcript or an unexpected voice appears, surface that to the user.

The default `recordings.py transcribe` flow already runs diarisation and identification automatically for meeting sessions — you do not need to do anything special. This section documents the underlying `stt` CLI commands for manual control, troubleshooting, or enrolling new speakers.

The full workflow is: **diarise** (cluster speakers) → **name/enroll** (interactively confirm and save speaker profiles) → **identify** (auto-label known speakers in future meetings). Diarisation and identification failures are non-fatal to transcription, but enrollment creates local profiles and sample audio, while relabeling modifies transcript artifacts. Describe those mutations before performing them.

### 1. Diarise — assign Speaker 0, Speaker 1, …

Add `--diarize` to `stt transcribe-meeting`. After each track is transcribed, an ECAPA-TDNN speaker embedding model clusters segments into distinct speakers. The mic track is diarised first (speakers 0…N), then the system track continues the numbering (speakers N+1…), guaranteeing unique speaker IDs across the merged transcript.

```bash
stt transcribe-meeting mic.wav system.wav \
  --output transcript.txt --json transcript.json \
  --diarize --diarize-python-backend runtime/
```

Key flags:
- `--diarize` — enable diarisation (ECAPA + clustering).
- `--diarize-python-backend runtime/` — the venv with speechbrain/torch (the ASR venv `python/.venv` is MLX-only and cannot run ECAPA).
- `--diarize-num-speakers N` — hint the exact number of speakers (skips auto-clustering).
- `--diarize-distance-threshold 0.40` — auto-clustering sensitivity (default 0.40; lower = more speakers). Tuned so intra-speaker ECAPA distance (~0.24) merges correctly while cross-speaker distance separates.

The merged transcript shows `Speaker 0`, `Speaker 1`, etc. Diarisation failures are non-fatal: they warn to stderr and the transcript is produced without speaker labels (never lose a 57-minute transcript to an ECAPA hiccup).

### 2. Name speakers — interactive play → name → enroll loop

Once a transcript is diarised, name each speaker by listening to a short clip:

```bash
stt name-speakers \
  --transcript transcript.json \
  --mic mic.wav --system system.wav \
  --python-backend runtime/
```

For each speaker (most speech first), the command:
1. Builds a playable clip from their **clearest speech segments** (energy-ranked, not just the first ones in chronological order) — non-speech tags like `[Silence]`, `[Environmental Sounds]` are automatically excluded so no time is wasted on dead air.
2. **Normalizes loudness** so quiet system-audio tracks (remote participants captured via the CoreAudio process tap are typically ~30 dB quieter than the mic) play as loud as the mic track.
3. Plays a ~12s preview via `afplay`.
4. Prompts: `[name] enroll as | [r] replay | [s] skip` (both the single letter and full word work: `s`/`skip`, `r`/`replay`).
5. If named → extracts a 192-dim ECAPA embedding from ALL their speech and enrolls a speaker profile.

Useful flags:
- `--preview-seconds N` — how long to play (default 12).
- `--sample-seconds N` — cap the stored reference clip to N seconds of speech (default 60; keeps profiles small — ~5MB instead of hundreds of MB). The embedding is always extracted from the speaker's full speech regardless.
- `--no-normalize` — disable loudness normalization of preview clips (on by default).
- `--no-enroll` — dry run: play and prompt but don't save profiles.

Profiles are stored locally at `~/Library/Application Support/stt/speakers/` (never uploaded). Manage them with `stt speaker list` and `stt speaker rename`. Removal is destructive: obtain explicit user authorization, then run `stt speaker remove "<name>" --yes`.

#### Agent-friendly one-speaker-at-a-time naming

When the user asks to name speakers one at a time — for example, "play one, wait for my name, then move to the next" — use the turn-based helper instead of running the blocking `stt name-speakers` loop directly. The helper exposes separate `list`, `preview`, and `enroll` actions so the agent can pause between speakers and wait for the user's reply.

```bash
# List unnamed diarised speakers, sorted by useful speech time
python3 scripts/name_one_speaker.py list \
  --session "<session>"

# Play one speaker's normalized preview clip only
python3 scripts/name_one_speaker.py preview \
  --session "<session>" --speaker-id <id>

# After the user gives the name, follow the safe-enrollment workflow below.
# Only after a fresh audit, production-provider suggestions, purity preview,
# and human confirmation should guarded whole-cluster enrollment run:
python3 scripts/name_one_speaker.py enroll \
  --session "<session>" --speaker-id <id> --name "<display name>"
```

Important behavior:
- `preview` uses the same `stt_vibevoice.speaker_id concatenate` backend as `stt name-speakers`: clearest segments first, bracket-only non-speech tags skipped, loudness normalization on by default.
- `enroll` creates a one-speaker filtered transcript and delegates back to `stt name-speakers`, so the embedding is still extracted from **all** speech for that speaker while the stored reference clip is capped by `--sample-seconds` (default 60s).
- A normal preview is not a purity check. Before calling `enroll`, complete the **Safe speaker enrollment** sequence below, including `audit --force`, `suggest-labels --provider speechbrain --force`, `purity-preview`, and explicit human confirmation.
- After each preview, stop and ask the user for the speaker's name; do not proceed until enrollment succeeds, the user says skip, or the user asks to stop.
- If enrollment reports that the display name already exists, do not overwrite or create a duplicate. Ask whether to relabel the cluster to the existing profile, use a different name, or explicitly manage the existing profile.

### 3. Identify — auto-label known speakers in future meetings

Once profiles exist, future transcriptions can auto-label speakers:

```bash
stt transcribe-meeting mic.wav system.wav \
  --output transcript.txt --json transcript.json \
  --diarize --diarize-python-backend runtime/ \
  --identify --identify-python-backend runtime/
```

(Profiles are read from the default directory `~/Library/Application Support/stt/speakers/`; override with `--identify-profiles-dir` if needed.)

After diarisation+merge, each speaker cluster's embedding is matched against enrolled profiles. Confident matches (above threshold + margin) are relabeled with real names (e.g. `Mic Ada:`); unmatched speakers keep `Speaker N`. If multiple clusters match the same profile, only the highest-confidence cluster receives the name and lower-confidence duplicates remain anonymous with `duplicate_profile_match`; review them with `suggest-labels` rather than assuming they are unknown people. The JSON transcript gains a `speaker_name` field per relabeled segment while `speaker_id` (numeric) is always preserved.

Flags:
- `--identify` — enable identification (requires `--diarize`).
- `--identify-profiles-dir` — override the profiles directory.
- `--identify-threshold` / `--identify-margin` — match confidence/margin (defaults 0.78 / 0.05).
- `--identify-python-backend runtime/` — venv for extraction+matching.

Identification failures are non-fatal: they warn to stderr and the transcript keeps numeric `Speaker N` labels.

### Complete workflow

The simplest workflow uses the helper for everything. Diarisation and identification are automatic:

```bash
# 1. Record
recordings.py start --title "Project sync" --mode meeting
# ...stop when done...
# 2. Transcribe (auto: diarise + identify if profiles exist)
recordings.py transcribe
```

**First time with a new group of people** (no profiles yet): the transcript comes out with `Speaker 0`, `Speaker 1`, … labels. Enroll them through the **Safe speaker enrollment** workflow below; do not run the blocking `stt name-speakers` whole-cluster loop directly because a diarised cluster may contain multiple voices.

**All subsequent meetings** with the same people: `recordings.py transcribe` automatically identifies profiles in the default `~/Library/Application Support/stt/speakers/` location and labels confident matches by name. New/unknown participants and lower-confidence duplicate matches remain `Speaker N` until reviewed. `recordings.py` does not auto-detect custom profile directories configured through `STT_HOME` or stt config; use the manual CLI with `--identify-profiles-dir` for those.

Custom stores need extra care in `name_one_speaker.py`: `suggest-labels` and `enroll-ranges` use `STT_SPEAKER_PROFILES_DIR` (the speaker-store root containing `profiles/`), while guarded whole-cluster `enroll` delegates to `stt name-speakers`, which uses stt's default config/`STT_HOME` resolution and ignores `STT_SPEAKER_PROFILES_DIR`. Before enrollment, ensure both mechanisms resolve to the same store; prefer the default store unless custom configuration is intentional.

**Plain transcript only** (skip speaker labelling for speed):

```bash
recordings.py transcribe --no-diarize
```

The manual `stt transcribe-meeting --diarize --identify` commands documented above give you finer control (custom thresholds, profile directories, etc.) but are not needed for normal use.

## Safe speaker enrollment (mixed & duplicate clusters)

The one-speaker-at-a-time `enroll` workflow above assumes each diarised cluster is a single, pure speaker. In real meetings that assumption sometimes fails:

- **Mixed clusters** — diarisation collapses two different people into one `Speaker N` cluster (their speech is spread across a wide wall-clock span). Enrolling the whole cluster from all their speech would build a profile from two voices and **contaminate** it.
- **Duplicate clusters** — the same real person is split across two clusters (e.g. one on the mic track, one on the system track) that both match one enrolled profile. Enrolling both creates two profiles for one person.

The safe workflow screens clusters **before** enrolling, enrolls only confirmed speech for mixed clusters, and relabels transcript segments by confirmed name. The commands are available both as the `name_one_speaker.py` helper (agent-friendly, turn-based) and as `stt speaker` CLI subcommands.

The audit is a heuristic pre-screen, not voice recognition: `safe_to_enroll_whole_cluster: true` means the guard may proceed, not that the cluster is proven pure. Never enroll a new whole cluster without a fresh audit, production-provider duplicate check, purity preview, and human confirmation.

### Recommended order

1. **`audit --force`** — recompute the pre-screen without modifying transcripts or profiles; it writes/refreshes `speaker_audit.json`. Never trust a cached artifact after transcript or diarisation changes.
2. **`purity-preview`** — generate and play early/middle/late + best-energy clips for every new whole-cluster candidate, especially suspicious clusters, and ask the user to confirm whether it is one voice or multiple voices. It writes preview artifacts under `.speaker-clips/` but does not modify transcripts or profiles.
3. **`suggest-labels --provider speechbrain --force`** — match every cluster against real enrolled profiles and flag duplicate/mixed groups. It writes/refreshes `speaker_label_suggestions.json` without modifying transcripts or profiles. Re-run after transcript, diarisation, relabeling, or profile changes. `mfcc-test` is test-only and its embeddings are incompatible with SpeechBrain profiles.
4. **`enroll-ranges`** (mixed cluster) **or guarded `enroll`** (not flagged + human-confirmed cluster) — use only confirmed ranges for mixed clusters. Treat the guard's `allow` result as permission, not proof of purity.
5. **`relabel`** — apply a human-confirmed name to selected transcript segments (by speaker id + time ranges), preserving `speaker_id`; use `--dry-run` first.

### Commands

#### 1. audit — heuristic pre-screen (transcript/profile read-only)

```bash
# Helper (agent-friendly, takes --session)
python3 scripts/name_one_speaker.py audit \
  --session "<session>" --force

# CLI equivalent
stt speaker audit --transcript "<session>/transcript.json" \
  --mic "<session>/mic.wav" --system "<session>/system.wav" \
  --python-backend runtime/ --force
```

`audit` classifies each cluster into one of three heuristic statuses:

| status | meaning | guard field |
|---|---|---|
| `pure_likely` | no risky span pattern detected; still requires purity preview + human confirmation | `safe_to_enroll_whole_cluster: true` |
| `mixed_suspected` | conservative wide-span risk signal; may be one long-running speaker or multiple voices, so audio confirmation is required | `safe_to_enroll_whole_cluster: false` |
| `unknown` | too little useful speech to assess (< `--min-useful-speech`, default 5.0s) | `safe_to_enroll_whole_cluster: false` |

The heuristic uses useful-speech duration and wall-clock span; it cannot recognize whether two voices occur in a compact interval. Key flags: `--force` (recompute even if `speaker_audit.json` exists), `--min-useful-speech` (default 5.0s), `--mixed-span-ratio` (default 3.0), and `--json` (extra copy).

It writes `<session>/speaker_audit.json`. `list` surfaces the status inline, and the `enroll` guard consumes it. The artifact has no transcript hash/freshness check, so always use `--force` immediately before an enrollment decision.

#### 2. purity-preview — audio-confirm a suspicious cluster (transcript/profile read-only)

```bash
# Helper
python3 scripts/name_one_speaker.py purity-preview \
  --session "<session>" --speaker-id <id>

# CLI equivalent
stt speaker purity-preview --transcript "<session>/transcript.json" \
  --speaker-id <id> --mic "<session>/mic.wav" --system "<session>/system.wav" \
  --python-backend runtime/
```

Builds and plays ~12s clips from the **early, middle, and late** parts of the cluster plus a best-energy clip (loudness-normalized), so the user can hear whether the cluster is one consistent voice or two. It writes those preview files under `<session>/.speaker-clips/` but does not alter transcripts or profiles. Pass `--range` (repeatable) to restrict the universe of speech before window selection. Use `--no-play` to build clips without playing them.

Key flags: `--preview-seconds` (default 12), `--range` (repeatable; restrict to time ranges first), `--no-play`, `--no-normalize`.

After the user listens, ask them to confirm: one voice (eligible for guarded enrollment after the other checks), or multiple voices (use `enroll-ranges` and `relabel` instead).

#### 3. suggest-labels — match clusters against profiles (transcript/profile read-only)

```bash
# Helper: force a fresh production-provider match
python3 scripts/name_one_speaker.py suggest-labels \
  --session "<session>" --provider speechbrain --force

# CLI equivalent (the CLI does not cache its output)
stt speaker suggest-labels --transcript "<session>/transcript.json" \
  --mic "<session>/mic.wav" --system "<session>/system.wav" \
  --provider speechbrain --python-backend runtime/
```

Matches every cluster against enrolled profiles and writes `<session>/speaker_label_suggestions.json` with:

- `clusters` — per-cluster match suggestion (`reuse_profile` with a name, or `no_match`).
- `duplicateClusterGroups` — two or more clusters confidently matching the **same existing profile** (likely the same speaker); `recommendation: "merge_or_relabel"` means confirm and relabel/merge all matching clusters to that profile without enrolling another one.
- `mixedClusterWarnings` — clusters whose early vs late windows match **different** profiles (the embedding signature of two people in one cluster).

It writes `<session>/speaker_label_suggestions.json` but never modifies the transcript or profiles; the decision to relabel is left to the agent. With a custom speaker store, export `STT_SPEAKER_PROFILES_DIR="<speaker-store-root>"` before running the helper.

Key flags: `--force`, `--provider`, `--threshold` (default 0.78), `--margin` (default 0.05), `--minimum-speech-seconds` (default 8), `--no-windows` / `--n-windows` (default 2), and `--output`. The helper defaults to `mfcc-test` for deterministic tests, but real identity/safety decisions must explicitly use `--provider speechbrain`; otherwise real SpeechBrain profiles are skipped as `provider_model_mismatch`.

#### 4. enroll (guarded) — safe whole-cluster enrollment

`enroll` now loads `<session>/speaker_audit.json` and decides before enrolling:

- `safe_to_enroll_whole_cluster == true` (status `pure_likely`) → the guard **allows** enrollment, but proceed only after `purity-preview` and human confirmation.
- `false` (status `mixed_suspected` or `unknown`) → **refuse**: exits before any audio playback or profile creation and prints the exact `purity-preview` + `enroll-ranges` commands to run instead.
- **no audit found** / **speaker not in audit** → the implementation warns and allows, but the agent must stop and run `audit --force`; do not treat the warning path as authorization.

```bash
# Dry run — show the guard's decision WITHOUT enrolling (no audio, no profile)
python3 scripts/name_one_speaker.py enroll \
  --session "<session>" --speaker-id <id> --name "<display name>" --no-enroll

# Real enrollment (only proceeds for safe clusters; refused clusters exit non-zero)
python3 scripts/name_one_speaker.py enroll \
  --session "<session>" --speaker-id <id> --name "<display name>"
```

**`--no-enroll` is now the guard-aware dry run** (it evaluates the guard and reports `would_enroll`, never touching the backend), not the old backend dry run. Always run it first to see the safety decision.

#### 4b. enroll-ranges — enroll only confirmed speech (mixed clusters)

For a mixed cluster, the user confirms specific time ranges of one voice via `purity-preview`, then enroll only those ranges:

```bash
# Dry run — validate ranges without writing files
python3 scripts/name_one_speaker.py enroll-ranges \
  --session "<session>" --speaker-id <id> --name "<display name>" \
  --range 12.0-45.0 --range 200.0-240.0 --no-enroll

# Real enrollment — builds a range-limited sample and enrolls a profile from it
python3 scripts/name_one_speaker.py enroll-ranges \
  --session "<session>" --speaker-id <id> --name "<display name>" \
  --range 12.0-45.0 --range 200.0-240.0
```

CLI equivalent:

```bash
stt speaker enroll-ranges "<display name>" --transcript "<session>/transcript.json" \
  --speaker-id <id> --range 12.0-45.0 --range 200.0-240.0 \
  --mic "<session>/mic.wav" --system "<session>/system.wav" --python-backend runtime/
```

`--range` is repeatable and required (at least one). Ranges accept `start-end` seconds (`12.0-45.0`), `MM:SS` (`02:03-03:00`), or `HH:MM:SS`. The range-limited sample is built from **only** the requested ranges (never falls back to whole-cluster audio), so a contaminated cluster cannot leak the second voice into the profile.

If the display name already exists, enrollment is **skipped** rather than overwritten. If it is the same person, relabel to the existing profile. Otherwise use a different name, or obtain explicit user authorization before renaming/removing a profile (`stt speaker remove "<name>" --yes`).

#### 5. relabel — apply a confirmed name to transcript segments

`relabel` applies a human-confirmed `speaker_name` to a subset of one cluster's segments (by speaker id + time ranges), **preserving `speaker_id`, `source`, timestamps, and text**. This lets a mixed cluster carry two human names (e.g. the early segments → "Dana", the late segments → "Ada") without changing the underlying diarisation ids.

```bash
# Dry run — report which segments would change without writing
python3 scripts/name_one_speaker.py relabel \
  --session "<session>" --speaker-id <id> --name "<display name>" \
  --range 12.0-120.0 --dry-run

# Apply — updates transcript.json + regenerated transcript.md (+ per-source .json in place)
python3 scripts/name_one_speaker.py relabel \
  --session "<session>" --speaker-id <id> --name "<display name>" \
  --range 12.0-120.0
```

When `--range` is omitted, **all** of the speaker's segments are relabeled (whole-cluster relabel). Per-source artifacts (`transcript.<source>.json`) are relabeled in place for the matching speaker id + ranges so every JSON stays consistent; raw `transcript.<source>.txt` files are never speaker-labeled and are not regenerated.

`relabel` is helper-only (not a `stt speaker` subcommand). Use it after the user confirms the correct name for the selected ranges.

### Whole-cluster enrollment decisions

| Evidence | Audit status | Whole-cluster `enroll`? |
|---|---|---|
| Heuristic not triggered; preview confirms one voice | `pure_likely` | Guard allows; enroll only after human confirmation |
| Two voices collapsed into one cluster | `mixed_suspected` | ❌ Use `enroll-ranges` + `relabel` |
| Too little speech to assess | `unknown` | ❌ Collect more speech or review confirmed ranges manually |
| Missing or potentially stale audit | missing/stale | ❌ Run `audit --force`; do not use the guard's warning path |

The guard's refusal runs **before** any audio playback or `subprocess.run`, so a refused `enroll` creates no `.speaker-clips/` directory and no profile. Its allow result only reflects the audit artifact; it does not establish that the artifact is fresh or that the cluster contains one voice.

### Worked examples

#### Example A — duplicate clusters (Sam on the system track)

In the validated Example Exchange fixture, Sam appears in system-track `Speaker 1`, `Speaker 3`, and `Speaker 5`. The deterministic regression validated that duplicate grouping; the live `mfcc-test` run could not compare against SpeechBrain profiles, so always run the production-provider command on the actual session before acting.

```bash
# 1. Refresh suggestions against the existing production profiles
python3 scripts/name_one_speaker.py suggest-labels \
  --session "<session>" --provider speechbrain --force
# Proceed only if the result groups Speakers 1, 3, and 5 under Sam's existing profile.

# 2. Preview each cluster and ask the user to confirm it is Sam
for id in 1 3 5; do
  python3 scripts/name_one_speaker.py purity-preview \
    --session "<session>" --speaker-id "$id"
done

# 3. After confirmation, dry-run and then relabel ALL matching clusters.
# Do not call enroll: the Sam profile already exists.
for id in 1 3 5; do
  python3 scripts/name_one_speaker.py relabel \
    --session "<session>" --speaker-id "$id" --name "Sam Rivera" --dry-run
  python3 scripts/name_one_speaker.py relabel \
    --session "<session>" --speaker-id "$id" --name "Sam Rivera"
done
```

The lesson: a duplicate group already points to an enrolled profile. Confirm the live SpeechBrain result and the audio, then relabel/merge every matching cluster onto that profile; create no new profile.

#### Example B — mixed cluster (Dana early, Ada late in Speaker 4)

One diarisation cluster, `Speaker 4`, actually contains two people: Dana speaks early, Ada speaks late. Enrolling the whole cluster would mix two voices into one profile.

```bash
# 1. Force a fresh audit; it raises a conservative risk signal for the cluster
python3 scripts/name_one_speaker.py audit \
  --session "<session>" --force
# → Speaker 4: status mixed_suspected, safe_to_enroll_whole_cluster: false

# 2. Audio-confirm: play early/middle/late clips so the user hears two voices
python3 scripts/name_one_speaker.py purity-preview \
  --session "<session>" --speaker-id 4
# User confirms Dana Tamayo at 180.22–184.88 + 200.17–213.32,
# and Alex Chen at 774.00–818.05.

# 3. Refresh real-profile suggestions before creating any profile
python3 scripts/name_one_speaker.py suggest-labels \
  --session "<session>" --provider speechbrain --force
```

In the validated Example Exchange session, `Dana Tamayo` and `Alex Chen` profiles already exist, so **do not enroll**. Dry-run both relabels, inspect the changed segment counts, and then apply:

```bash
python3 scripts/name_one_speaker.py relabel \
  --session "<session>" --speaker-id 4 --name "Dana Tamayo" \
  --range 180.22-184.88 --range 200.17-213.32 --dry-run
python3 scripts/name_one_speaker.py relabel \
  --session "<session>" --speaker-id 4 --name "Dana Tamayo" \
  --range 180.22-184.88 --range 200.17-213.32
python3 scripts/name_one_speaker.py relabel \
  --session "<session>" --speaker-id 4 --name "Alex Chen" \
  --range 774.00-818.05 --dry-run
python3 scripts/name_one_speaker.py relabel \
  --session "<session>" --speaker-id 4 --name "Alex Chen" \
  --range 774.00-818.05
```

If a confirmed person has **no** reusable profile in another session, first validate only their ranges with `enroll-ranges ... --no-enroll`; after inspecting the dry-run and obtaining explicit confirmation, rerun without `--no-enroll`. Never enroll when suggestions show an existing profile.

After relabeling, those `transcript.json` segments carry `speaker_name` "Dana Tamayo" / "Alex Chen" while `speaker_id` stays `4` — so a mixed cluster can hold multiple human names without breaking diarisation.

### Generated artifacts

| Artifact | Location | Written by |
|---|---|---|
| Speaker audit | `<session>/speaker_audit.json` | `audit` |
| Label suggestions | `<session>/speaker_label_suggestions.json` | `suggest-labels` |
| Flattened profile copy (includes embeddings) | `<speaker-store-root>/.speaker-clips/_flattened_profiles.json` | helper `suggest-labels` |
| Purity/preview clips | `<session>/.speaker-clips/` | `preview`, `purity-preview` |
| Filtered one-speaker transcript | `<session>/.speaker-clips/speaker-<id>-only-transcript.json` | guarded whole-cluster `enroll` |
| Range-limited enrollment sample | `<session>/.speaker-clips/speaker-<id>-enroll-ranges-*.wav` (+ `.enroll.json` metadata) | `enroll-ranges` |
| Profile provenance | `<session>/.speaker-clips/speaker-<id>-enroll.provenance.json` | `enroll-ranges` → forwarded to `stt speaker enroll --provenance-json` |
| Enrolled profile | `~/Library/Application Support/stt/speakers/profiles/<id>.json` (+ `samples/<id>/*.wav`) | `stt speaker enroll` |

For `enroll-ranges`, the provenance payload records where the voice came from: source session, transcript, track, diarised speaker id, confirmed ranges, and `range-limited` confirmation mode. `samplePath` is filled by `stt speaker enroll` at enrollment time (canonical `samples/<id>/<ts>.wav`). Whole-audio/whole-cluster enrollment does not currently populate this provenance automatically, and legacy profiles also load with `provenance: nil`.

The flattened profile copy contains local speaker embeddings and persists until explicitly cleaned up. Treat it as sensitive local data, do not upload it, and ask before deleting it or any generated artifact.

### Failure handling

- **Refused whole-cluster enrollment** (`mixed_suspected` / `unknown`): the guard exits before any audio playback or profile creation. Follow the printed `purity-preview` → `enroll-ranges` commands. Never bypass the guard by enrolling from raw audio for a refused cluster.
- **Display name already exists**: `enroll` / `enroll-ranges` **skips** rather than overwriting. Prefer relabeling to the existing profile when it is the same person. Profile replacement/removal is destructive: obtain explicit user authorization before using `stt speaker enroll --replace` or `stt speaker remove "<name>" --yes`.
- **No usable speech in the requested ranges** (`enroll-ranges`): fails fast with a clear message; never falls back to whole-cluster audio. Ask the user to confirm wider ranges via `purity-preview`.
- **Playback failure** (`preview`/`purity-preview`): `afplay` may be blocked or the clip empty. Re-run with `--no-play` to build clips the user can play manually from `.speaker-clips/`.
- **No audit run yet or inputs changed**: the guard may warn and allow enrollment, but stop instead. Run `audit --force`, refresh `suggest-labels --provider speechbrain --force`, perform `purity-preview`, and obtain human confirmation before enrolling.

## Sound file cleanup

Cleanup is destructive and there is currently no `recordings.py cleanup` command. Perform it only after the user explicitly authorizes an exact set of files for one named session.

Distinguish three audio classes:

1. **Original session audio** — top-level files such as `<session>/mic.wav`, `system.wav`, `mixed.wav`, or another captured input. These are required for retranscription, re-diarisation, speaker previews, and correcting later mistakes.
2. **Generated speaker clips** — `<session>/.speaker-clips/*.wav`, including preview, purity-preview, and range-limited enrollment inputs. These are working copies. Their accompanying JSON and provenance files are not audio and should remain unless separately authorized.
3. **Canonical speaker-profile samples** — `<speaker-store>/samples/<profile-id>/*.wav`, referenced by profiles under `<speaker-store>/profiles/`. These are independent profile artifacts, not session-cleanup targets. Never include them in ordinary cleanup; manage profiles through `stt speaker` commands and obtain separate destructive authorization.

Before cleanup:

- Run `recordings.py status` and require both recording `active: false` and `transcription.active: false`.
- Require the named session's metadata status to be `transcribed`. Validate that `transcript.md` contains non-whitespace text and that `transcript.json` has the supported shape **and at least one substantive segment** whose `text` is non-whitespace and not only a bracketed non-speech tag such as `[Silence]`. File size or the helper's current permissive JSON-shape check alone is insufficient. Global inactivity does not prove that this session is safe to clean.
- Confirm the user has accepted the transcript, summary, speaker labels, and any other required outputs. Once source audio is gone, those results may no longer be correctable.
- If speaker enrollment occurred, verify every intended profile name, profile JSON, and referenced canonical sample in the active speaker store before removing enrollment working clips.
- Enumerate the exact paths, audio class, file count, and total size. Explicitly exclude transcripts, metadata, logs, `.stt-staging/`, `.speaker-clips/*.json`, provenance files, and canonical profile samples. Ask the user to confirm that exact scope.

Move approved files to **macOS Trash** with `trash`; never use permanent deletion or empty Trash. Common names such as `mic.wav` may be renamed after a Trash collision, so record the original session paths and cleanup scope before moving them.

After cleanup:

- Verify every selected audio file is absent and every excluded artifact and canonical profile sample remains.
- After **every** top-level audio removal, atomically replace `metadata.json.audio_files` with only the top-level audio files that still exist; use `[]` only when none remain. Append an `audio_cleanup_events` entry containing `timestamp`, `timestamp_human`, `reason`, `original_paths`, `audio_classes`, `destination: "macOS Trash"`, and `recoverable_until_trash_emptied: true`. When all source audio is gone, also set the compatibility fields `source_audio_removed_at`, `source_audio_removed_at_human`, and `source_audio_removal_reason`. Preserve `transcribed_audio_path` and input fingerprints as historical provenance, not as evidence that files still exist.
- Update only the `Audio files` section of `session.md` after every cleanup. List surviving top-level audio separately from files moved to Trash, including their original paths and cleanup time. Preserve summary and transcript links. `write_session_note()` does not currently understand cleanup metadata and may overwrite a manual cleanup explanation, so do not blindly regenerate the note.
- Keep the session status and kanban card in `Transcribed`; verify the board rather than hand-editing it unless the helper fails.
- Report what moved, reclaimed size, preserved artifacts, and the canonical profile samples that remain.

Trash is temporary recovery, not a backup. After Trash is emptied, retranscription, re-diarisation, speaker preview, or re-enrollment from the session may be impossible. Restoring audio from Trash does not automatically repair metadata or `session.md`; reconcile both again after any restoration.

## Failure handling

- If `start` is refused because a recording is already active, the helper will not kill it. Show `status` and ask whether to stop the existing recording first. If the refusal says a recording is **scheduled** for a future start time (session status `scheduled`, pid null, on the `Recording` lane), the same rule applies — run `stop` to cancel the schedule before starting another.
- If `start` is refused because a transcription is currently active, wait for it to finish (check `status`) before starting a new recording; the helper will not kill a running transcription.
- If `start` reports `recording_failed` (session status, kanban lane `Failed / Needs Attention`, `failure_reason` + `failed_at` recorded in `metadata.json`), the recorder process itself could not be launched (e.g. the `stt` binary could not be executed). This is distinct from a recording that started and was later stopped/interrupted. Show the failure reason and suggest checking `STT_BIN`/the stt-cli build before retrying.
- If a recording won't stop gracefully, the helper escalates automatically: SIGINT (graceful teardown + WAV header flush) → SIGTERM → SIGKILL, finalizing the session regardless — but only after re-verifying, immediately before every signal, that the PID is genuinely this recorder's own detached process (its process-group id equals its own PID and its command line references the session). If that identity cannot be confirmed (e.g. a reused PID belonging to an unrelated process), the helper reports `recording_stop_unverified` and refuses to signal it at all, reconciling its own bookkeeping without touching the real process — inspect the PID manually (`ps -p <pid> -o pid,pgid,stat,command`) before taking any action in that case. The `stt` recorder restores default signal disposition during its teardown phase, and its streaming WAV writer keeps the on-disk header crash-safe, so even a SIGKILL during capture leaves playable, transcribable audio.
- If meeting mode fails or the system track comes back silent/empty, first suspect the one-time System Audio Recording TCC permission (see above) rather than a missing BlackHole device. Explain the failure and offer to retry; only suggest a named-device fallback (BlackHole/Aggregate Device) if the native tap is genuinely unavailable on that machine.
- If transcription backend readiness fails, show the command output and suggest running the stt backend bootstrap from `<stt-cli-repo>`.
- If `transcribe` reports `transcription_failed` (session status, kanban lane `Failed / Needs Attention`, `failure_reason` + `failed_at` recorded in `metadata.json` and shown in the session note), this can mean either `stt` exited non-zero, or it exited 0 but produced missing/empty/schema-invalid output — both are treated as failure. The transcription log and that run's staging directory are preserved; read the log before retrying.
- If `transcribe` reports `transcription_interrupted` (session status, kanban lane `Failed / Needs Attention`, `interrupted_at` recorded, session note explains it), first run `status` to confirm no child `stt` process is still alive for that session, then run `transcribe --resume --session "<session>"`. It finalizes immediately if this attempt's own staged output (or, for a legacy session, a sufficiently new canonical transcript) is valid, otherwise safely reruns the full command with the currently requested options; warn the user that a full rerun may overwrite partial per-track artifacts left in that run's staging directory, since they cannot be safely merged.
- If `transcribe` or `start` refuses because a transcription is already active for a PID that you know is stale (e.g. the machine was rebooted), re-run `status` first — it reconciles `.active_transcription.json` on every invocation and will clear a genuinely dead PID automatically. Do not hand-edit or delete the active-transcription state file directly.
- Avoid deleting recordings, transcripts, or `.stt-staging/` run directories. If cleanup is needed, ask the user first — a staging directory may be the only evidence of what a failed/interrupted attempt actually produced.

## Kanban format

The helper creates/maintains an Obsidian Kanban board named `meeting recordings.md` with lists:

- `Recording`
- `To Transcribe`
- `Transcribed`
- `Failed / Needs Attention`

Cards link to each session note, e.g.:

```markdown
- [ ] [[media/meeting-recordings/20260706-130000-customer-call/session|Customer call]] — stopped 2026-07-06 13:45 — mode meeting
```

Sessions currently `transcribing` sit in `To Transcribe` with card text like `transcribing (PID 12345) since 2026-07-06 13:50`; the session note also lists the transcription PID, run id, and start time. Sessions marked `transcription_interrupted` move to `Failed / Needs Attention` with card text like `interrupted 2026-07-06 13:55 — resume with transcribe --resume` (using the dedicated `interrupted_at` timestamp); the session note explains that partial artifacts may exist in that run's staging directory and to run `transcribe --resume`. Sessions marked `transcription_failed` also move to `Failed / Needs Attention`, with card text like `failed 2026-07-06 13:55 — stt exited 0 but produced invalid/missing output: ...` using the dedicated `failed_at` timestamp and `failure_reason`. Sessions marked `recording_failed` (the recorder process itself never started) also move to `Failed / Needs Attention`, with card text like `recording failed to launch 2026-07-06 13:55 — failed to launch stt binary: ...`.

Sessions scheduled to start later (`start --start-at`/`--delay`) sit in `Recording` with card text like `scheduled for 2026-08-12 15:00` (using `scheduled_start_at_human`); the session note lists the scheduled start time. Cancelled schedules (`scheduled_cancelled`) move to `Failed / Needs Attention` with card text like `scheduled start cancelled 2026-08-12 14:55 — cancelled by `stop` before the scheduled start` (using `cancelled_at`/`cancelled_at_human` and `cancel_reason`).

Let the helper modify this board; do not hand-edit it unless the helper fails and you have inspected the file first.
