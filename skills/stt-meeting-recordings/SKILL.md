---
name: stt-meeting-recordings
description: 'Start, stop, catalogue, and later transcribe meeting or ad-hoc audio recordings using the local stt CLI without transcribing during capture. Use whenever the user asks to record a meeting/call/conversation/audio note, start or stop a recording, list pending meeting recordings, or transcribe a previously recorded meeting. Also use when the user says "record this", "start meeting recording", "stop recording", "transcribe that meeting", or asks to update the Obsidian meeting recordings kanban. When starting a recording, check the user''s Outlook calendar (via the outlook-cli skill) to identify the current or forthcoming meeting so the session is titled after the real meeting and timed recordings can be sized to its end. Also use for timed or one-shot meeting requests ("record this for 45 mins"), which run the full pipeline: auto-stop, transcribe, distill, file meeting notes, and clean up audio.'
compatibility: macOS with a local stt CLI build (this repo) and an Obsidian vault; override with STT_BIN / OBSIDIAN_VAULT.
disable-model-invocation: true
---

# STT Meeting Recordings

Two deliberately separate modes:

1. **Record mode**: start/stop a recording, save audio into the Obsidian vault. **Never transcribe while recording** — transcription is CPU/GPU heavy and only runs after the user asks.
2. **Transcribe mode**: transcribe a saved recording later, write transcript artifacts into the session folder, move its kanban card.

The bundled helper scripts handle repeatable details: binary selection, session folders, kanban updates, PID/state tracking, stopping via SIGINT, and transcription strategy. Meeting sessions transcribe `mic.wav` and `system.wav` separately, then merge timestamped transcripts so overlapping mic speech is not masked by system audio.

Detail lives in on-demand reference files (read when the task needs them):

- `references/speaker-identification.md` — diarisation, naming/enrolling speakers, safe-enrollment guards, relabeling
- `references/reliability-internals.md` — run tracking, interruption/timeout semantics, staged transcript promotion
- `references/kanban-states.md` — full failure matrix with recovery commands, kanban card format

## Default locations

- Obsidian vault: `~/obsidian-notes` (override `OBSIDIAN_VAULT`)
- Recordings root: `media/meeting-recordings/` inside the vault
- Kanban board: `meeting recordings.md` at the vault root
- Active recording / transcription state: `media/meeting-recordings/.active_recording.json` / `.active_transcription.json` (helper-managed; never hand-edit or delete)
- Per-run staging: `<session>/.stt-staging/<run_id>/` (helper-managed; never delete)
- Vault lock: `media/meeting-recordings/.recordings.lock` (helper-internal)

## Record mode

### Meeting lookup (Outlook calendar)

Before starting a recording, resolve the real meeting so the session is titled after it and timed recordings can be sized to its end:

```bash
python3 scripts/meeting_lookup.py
```

Wraps the outlook-cli skill's `outlook calendar --days 1 --timezone Australia/Sydney --json` and prints one JSON object: `found: true` with a `match` (`subject`, `start`/`end`, `duration_seconds`, `starts_in_seconds`/`ends_in_seconds`, `attendees` as `{"name","address"}` objects — the expected-speaker whitelist for diarisation), or `found: false` with `next` for context, or `ok: false` (treat as no meeting found; note the failure, don't block).

- **Capture the invite list**: pass matched `attendees` to `start` as `--attendees "Name <email>, email" --attendees-source calendar`. Stored in the session's `metadata.json` and shown in `session.md` as **Attendees (expected speakers)**. If the lookup matched but has no attendees, say the whitelist is empty rather than inventing names.
- **Title precedence**: explicit user title → matched meeting `subject` (verbatim, optionally trimmed) → `Untitled recording`.
- **Timed sizing**: explicit duration wins; otherwise `current` → `--duration` = `ends_in_seconds` rounded up to the next minute; `upcoming` → `--duration` = `duration_seconds` plus `--start-at "<match.start>"` (or `--delay <starts_in_seconds>`) so capture spans the whole meeting. Never pad more than a couple of minutes past the end.
- **Timezone**: always pass an IANA name (`--timezone Australia/Sydney`); the outlook global config's Windows tz ID is rejected by `calendar`. Local-time offsets need no conversion.

### Start recording

Default to **`meeting` mode** for anything ambiguous (meetings, calls, webinars, demos, conversations, "test this"). `mic` only for quick voice notes or explicit mic-only requests; `system` only for explicit system-audio-only requests.

```bash
python3 scripts/recordings.py start --title "<title>" --mode meeting --attendees "<invite list>" --attendees-source calendar
# Voice note / finite smoke test:
python3 scripts/recordings.py start --title "Voice note" --mode mic
python3 scripts/recordings.py start --title "Smoke test" --mode meeting --duration 10 --wait
```

Report: title, mode, session folder, PID, kanban path, captured attendee whitelist (or explicitly none), and **"Recording started; transcription has not been started."** For `--duration N --wait`, wait for the helper and also report finish details (audio files, `active: false`, card moved to `To Transcribe`).

### How system audio is captured (meeting/system mode)

`meeting`/`system` use the **native macOS CoreAudio process tap** (macOS 14.4+; no BlackHole or virtual loopback needed on this machine). BlackHole/named-device is only an automatic fallback if the tap is unavailable. A meeting recording produces `mic.wav` + `system.wav` only — `mixed.wav` is not auto-generated; run `stt mix <session>/mic.wav <session>/system.wav --output <session>/mixed.wav` afterwards if a reference mix is wanted.

First meeting/system run may trigger a one-time **System Audio Recording** permission prompt attributed to the `stt` app bundle (`com.larrysong.stt`); approve once. If denied, system audio is silently empty — fix under System Settings → Privacy & Security → System Audio Recording.

### Scheduled / delayed start

`--start-at "HH:MM[(:SS)]"` or ISO datetime, or `--delay <seconds>` (only one; must be future). The helper creates the session immediately with `status: "scheduled"` (pid null), waits **without holding the vault lock**, and prints a `scheduled` JSON payload (title, session dir, `scheduled_start_at`, `starts_in_seconds`, `expected_stop_at`, board path). A second `start` is refused while pending; `stop` (or SIGINT/SIGTERM to the waiting helper) cancels cleanly → `scheduled_cancelled` on `Failed / Needs Attention`. Combine with `--duration` and `--wait`; budget tool-call timeout as delay + duration + margin.

### Stop recording

```bash
python3 scripts/recordings.py stop
```

Stops a live recording (SIGINT → SIGTERM → SIGKILL escalation, session finalized regardless, after re-verifying the PID is genuinely this recorder's detached process). For a **scheduled** recording, `stop` instead cancels the schedule (`recording_cancelled`, card → `Failed / Needs Attention`). Report: session folder, audio files produced (mic.wav + system.wav; mention `stt mix` for a combined track), kanban move, and that they can ask this skill to transcribe later.

### Timed recording (auto-stop after N minutes)

Parse the duration into seconds (45 min → 2700s) and pass `--duration <s> --wait` — the recorder stops itself and the helper finalizes (default wait budget already scales: `max(duration + 120s, duration × 2)`). If the user says "record this meeting" without a duration, size from the lookup (see above). If a recording is already active, `start` is refused — show `status` and ask; never kill it yourself.

### Status / list pending recordings

```bash
python3 scripts/recordings.py status
python3 scripts/recordings.py list --pending
```

`status` reports recording **and** transcription state in one JSON payload (nested `transcription` object: active PID/run id/resolved timeout, or reconciled outcome `transcribed` / `transcription_failed` / `transcription_interrupted`). Every helper invocation reconciles stale state files first under the vault lock — after a harness timeout or reboot, run `status` rather than assuming; it clears genuinely dead PIDs automatically. `list --pending` uses a real validated-transcript check (not stale metadata) and shows each session's captured `attendees` whitelist.

## Transcribe mode

**Agent-harness timeout requirement:** `transcribe` is synchronous and can take hours. Set the outer/tool-call timeout to at least **10800 s (3 h)** for one meeting, or `track_count × resolved_per_track_timeout + 900` when higher. The adaptive per-track timeout is `max(1800, 4 × longest_input_duration + 300)` (explicit `--timeout` overrides; `resolved_timeout` is reported in the helper JSON — report it for long recordings). If the harness timeout fires, it kills only the helper — the detached `stt` child is *designed* to keep running and may still finish: run `status`, then `transcribe --resume`; never assume failure or success.

Only transcribe after the user explicitly asks — never as part of `start` or while a recording is active.

```bash
# Most recent stopped recording without a valid transcript
python3 scripts/recordings.py transcribe
# A specific session
python3 scripts/recordings.py transcribe --session "~/obsidian-notes/media/meeting-recordings/<session>"
# Plain transcript, no speaker labelling
python3 scripts/recordings.py transcribe --no-diarize
```

For meeting sessions the helper runs `stt transcribe-meeting` on mic/system separately with `--diarize --identify` **on by default** (profiles auto-skip when none are enrolled; `--no-diarize`/`--no-identify` opt out). Missing/empty single tracks fall back to the best single file: `mixed.wav` → `mic.wav` → `system.wav` → any single `.wav`.

Outputs are written to a per-attempt staging directory first and **promoted into the session folder only after validation** (non-empty `transcript.md`, schema-valid `transcript.json` with `speaker_id`/`speaker_name` when diarisation ran; per-source artifacts like `transcript.mic.txt`): `transcript.md`, `transcript.json`, updated `session.md`, kanban card → `Transcribed`. **A zero exit code is not success** — invalid/missing output marks the session `transcription_failed` with `failure_reason`; the log and staging dir are preserved. The helper's JSON reports artifact paths but not speaker counts or CLI warnings: inspect distinct `speaker_id` / non-empty `speaker_name` in `transcript.json` and read `transcription.log` before reporting ("4 speakers diarised, 3 identified", plus warnings).

Interruption semantics in brief: a SIGINT/timeout while the child still runs leaves state as `transcribing` + `helper_interrupted` (deliberately not terminal); once the child is gone the session becomes `transcription_interrupted` and `transcribe --resume` finalizes from that run's own staged output without re-invoking `stt`, or safely reruns — it never trusts older canonical transcripts. Reruns may overwrite partial per-track artifacts in staging; say so before resuming. Full mechanics (blocked-signal launch, journalled promotion, resume trust rules): `references/reliability-internals.md`.

## One-shot timed meeting pipeline

"Record this meeting for 45 mins" (single request, full handling): run each stage in order, confirm completion before the next; transcription takes longer than the meeting — never assume it finished with the recording.

1. **Record with auto-stop.** Resolve the meeting, capture attendees, size `--duration` (or schedule with `--start-at`/`--delay` for future meetings), `start ... --duration <s> --wait`. Report the `scheduled` payload first for future starts, then verify `active: false` and card in `To Transcribe`.
2. **Transcribe.** `transcribe --session "<session>"` with the generous timeout above. On failure/interruption follow the failure steps (read log, `--resume`) — never file or delete from a session whose transcript did not land.
3. **Distill.** Write `<session>/summary.md` yourself (the helper never does): frontmatter, executive summary, confirmed speakers, key points/decisions/questions, action items, links to session + transcript.
4. **File & update the customer's running note (only if one exists).** Look for the customer's meeting-series note under your vault's customer layout (e.g. `customers/<Customer>/<series>.md`), deriving the customer from the title ("ACME cadence" → `customers/ACME/ACME cadence.md`). If missing, **skip entirely** (never invent folders/notes) and say so. When it exists, append a dated `# YYYY-MM-DD` section at the **end** linking the session and carrying the distilled points; preserve existing content and order.
5. **Remove the .wav files (always).** The one-shot request pre-authorizes removal of this session's own top-level audio only, still following "Sound file cleanup" (status checks, transcript validation, enumerated scope, Trash not delete). Never trash `.stt-staging/`, `.speaker-clips/*.json`, provenance files, or profile samples.

Report each stage as it completes: start (title, folder, PID, expected stop), finish, transcription (paths, speaker outcome), distill, filing (note + section, or skipped), cleanup (files trashed, size).

## Speaker identification (summary)

`recordings.py transcribe` diarises and identifies automatically. The captured `attendees` whitelist is the reference set: `Speaker N` clusters should be checked against it; identified names matching invitees are expected; unexpected or missing voices should be surfaced to the user.

When naming and enrolling new speakers — do **not** run the blocking `stt name-speakers` loop directly (a diarised cluster may contain multiple voices). Use the guarded workflow in `references/speaker-identification.md`: `audit --force` → `purity-preview` → `suggest-labels --provider speechbrain --force` → guarded `enroll` / `enroll-ranges` → `relabel`, with human confirmation before every profile mutation. Profiles live in `~/Library/Application Support/stt/speakers/` (local only). Enrollment, relabeling, and profile removal are mutations: describe them and get explicit authorization first — removal uses `stt speaker remove "<name>" --yes` only after the user approves.

## Sound file cleanup

Cleanup is destructive; there is no `recordings.py cleanup` command. Only after the user explicitly authorizes an exact set of files for one named session.

Three audio classes:

1. **Original session audio** (`mic.wav`, `system.wav`, `mixed.wav`) — required for retranscription/re-diarisation; removal may make results uncorrectable.
2. **Generated speaker clips** (`<session>/.speaker-clips/*.wav`) — working copies; their `.json`/provenance files are not audio and stay unless separately authorized.
3. **Canonical profile samples** (`<speaker-store>/samples/<id>/*.wav`) — never session-cleanup targets; manage via `stt speaker` only.

Before: require recording `active: false` **and** `transcription.active: false`; require session status `transcribed` with a **substantive** transcript (`transcript.md` non-empty; `transcript.json` valid with ≥1 real segment — not just `[Silence]`-style tags; file size alone is insufficient); confirm the user accepts the outputs; verify enrolled profiles and their samples if enrollment occurred; enumerate exact paths, classes, count, and size (explicitly excluding transcripts, metadata, logs, `.stt-staging/`, `.speaker-clips/*.json`, provenance, profile samples) and have the user confirm that scope.

Move approved files to **macOS Trash** with `trash` — never permanent deletion, never empty Trash. Record original paths before moving (Trash may rename on collision).

After: verify selected files gone and excluded artifacts intact; atomically update `metadata.json.audio_files` (only surviving top-level audio; `[]` when none) and append an `audio_cleanup_events` entry (timestamp, reason, `original_paths`, `audio_classes`, `destination: "macOS Trash"`, `recoverable_until_trash_emptied: true`); when all source audio is gone also set `source_audio_removed_at(_human)` / `source_audio_removal_reason`. Update only the `Audio files` section of `session.md` — `write_session_note()` may overwrite a manual cleanup explanation, so don't blindly regenerate. Keep status/card in `Transcribed`. Trash is temporary recovery, not a backup; restoring from Trash does not repair metadata — reconcile again afterwards.

## Failure handling

Common cases (full matrix with recovery commands: `references/kanban-states.md`):

- **`start` refused (recording/schedule/transcription active)**: never kill anything yourself — show `status`, ask, use `stop` (which also cancels schedules).
- **`recording_failed`**: the recorder never launched (bad `STT_BIN`/build). Show `failure_reason`; check the build before retrying.
- **`recording_stop_unverified`**: the helper refused to signal a PID it couldn't verify as this recorder's — inspect `ps -p <pid> -o pid,pgid,stat,command` before acting.
- **System track silent/empty**: suspect the one-time System Audio Recording TCC permission first (see above), not a missing BlackHole device.
- **`transcription_failed` / `transcription_interrupted`**: read `transcription.log` and the run's staging dir (preserved — never delete them), run `status`, then `transcribe --resume`.
- **Backend readiness failure**: show the output; suggest the stt backend bootstrap from the repo.
- **Stale active-state refusals after reboot/timeout**: run `status` (it reconciles automatically); never hand-edit the state files.

## Kanban board

The helper maintains `meeting recordings.md` with lanes `Recording`, `To Transcribe`, `Transcribed`, `Failed / Needs Attention`, linking each session note. Let the helper modify the board; never hand-edit unless the helper fails and you have inspected the file. Card/state details: `references/kanban-states.md`.
