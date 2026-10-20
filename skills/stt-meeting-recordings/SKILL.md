---
name: stt-meeting-recordings
description: 'Record, catalogue, and later transcribe meetings or ad-hoc audio with the local stt CLI; never transcribe during capture. Use when the user asks to record a meeting/call/conversation/voice note ("record this", "start/stop meeting recording"), list pending recordings, or transcribe a saved one. Checks the Outlook calendar (via outlook-cli) to title sessions after the real meeting and start them on time. Without an explicit duration the recording is capped at the booked calendar end + 30 min and stops itself there (it does not transcribe on its own); a meeting that is not in the calendar runs until the user says stop. If the user asks you not to check the calendar at all, a fixed fallback cap applies instead (90 min by default). Also one-shot timed requests ("record this for 45 mins"): record → transcribe → distill → file → cleanup. Also transcribes EXTERNAL audio — podcasts, YouTube, downloaded files ("transcribe this podcast/episode", "get the text of this video"), speaker naming, transcript translation — see references/external-audio.md.'
compatibility: macOS with a local stt CLI build (this repo) and an Obsidian vault; override with STT_BIN / OBSIDIAN_VAULT.
disable-model-invocation: true
---

# STT Meeting Recordings

Two deliberately separate modes:

1. **Record mode**: start/stop a recording, save audio into the Obsidian vault. **Never transcribe while recording** — transcription is CPU/GPU heavy and only runs after the user asks.
2. **Transcribe mode**: transcribe a saved recording later, write transcript artifacts into the session folder, move its kanban card.
3. **External-audio mode**: transcribe non-live audio (podcasts, YouTube, files) — acquisition, conversion, single-track diarisation, translation: `references/external-audio.md`.

The bundled helper scripts handle repeatable details: binary selection, session folders, kanban updates, PID/state tracking, stopping via SIGINT, and transcription strategy. Meeting sessions transcribe `mic.wav` and `system.wav` separately, then merge timestamped transcripts so overlapping mic speech is not masked by system audio.

Detail lives in on-demand reference files (read when the task needs them):

- `references/speaker-identification.md` — diarisation, naming/enrolling speakers, safe-enrollment guards, relabeling
- `references/external-audio.md` — transcribing podcasts/YouTube/audio files: acquisition (incl. Spotify scrape), single-track diarisation, translation, cleanup
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

Before starting a recording, resolve the real meeting so the session is titled after it and starts at the right time (immediately for a current meeting, scheduled for an upcoming one):

```bash
python3 scripts/meeting_lookup.py
```

Wraps the outlook-cli skill's `outlook calendar --days 1 --timezone Australia/Sydney --json` and prints one JSON object: `found: true` with a `match` (`subject`, `start`/`end`, `duration_seconds`, `starts_in_seconds`/`ends_in_seconds`, `max_recording_seconds` — the default auto-stop cap, see below — `attendees` as `{"name","address"}` objects — the expected-speaker whitelist for diarisation), or `found: false` with `next` for context, or `ok: false` (treat as no meeting found; note the failure, don't block).

- **Later meetings need a wider window**: the default lookahead is 1 day, so a meeting named for a future date ("the next ACME meeting on 11am") will not appear. Pass `--days N` (e.g. `meeting_lookup.py --days 14`); only bypass the wrapper for a raw `outlook calendar` query when you need the full attendee objects.
- **A transient lookup failure is not fatal**: this wrapper has been seen to return `{"ok": false, "code": "bad_json"}` once and succeed on an immediate retry — re-run it before telling the user the calendar is unavailable.
- **Opt-out: skip the lookup, keep a cap** — only when the user explicitly asks not to check the calendar ("no calendar", "don't check Outlook", "skip the lookup", "offline"): skip `scripts/meeting_lookup.py` entirely and pass `--duration <cap>` with **no** `--wait`, where `<cap>` is `STT_DEFAULT_MAX_RECORDING_SECONDS` when that is a positive integer of at least 60, otherwise **5400** (90 min) — resolve it yourself, neither the helper nor `stt` reads that variable. Never pass 0, a negative or a non-number: `stt record` rejects it and the session dies immediately after "Recording started". Title comes from the user's words (else `Untitled recording`); with no invite list there is no attendee whitelist — say so, because there is then nothing to check `Speaker N` clusters against, so unnamed voices stay unnamed unless already enrolled. This is an explicit opt-in only — the default path below (lookup → booked end + 30 min) is unchanged, and a lookup **failure** still just means "no meeting found" (no cap) unless the user asks for this fallback.

- **Capture the invite list**: pass matched `attendees` to `start` as `--attendees "Name <email>, email" --attendees-source calendar`. Stored in the session's `metadata.json` and shown in `session.md` as **Attendees (expected speakers)**. If the lookup matched but has no attendees, say the whitelist is empty rather than inventing names.
- **Title precedence**: explicit user title → matched meeting `subject` (verbatim, optionally trimmed) → `Untitled recording`.
- **Start timing (default cap = booked end + 30 min)**: an explicit duration from the user wins over everything. Otherwise pass a cap: `--duration <match.max_recording_seconds>` — the lookup computes the seconds from *recorder launch* to booked end + 30 min grace, so pass it verbatim whatever the branch. `current` → start immediately; `upcoming` → `--delay <match.starts_in_seconds>` (second-exact; `--start-at "<match.start>"` is minute-truncated) plus the same `--duration`; `recently_ended` (booked end already passed but at least 5 min of the grace is left) → **confirm the derived title and the short cap with the user before starting**, then start immediately with that smaller `--duration`. Do **not** pass `--wait` for the default cap: the recorder stops itself and the detached monitor finalizes the session (`To Transcribe`) without the call waiting for the recording. The user can still stop earlier with `recordings.py stop`. The cap stops capture only — it must never start transcription.
- **Timezone**: always pass an IANA name (`--timezone Australia/Sydney`); the outlook global config's Windows tz ID is rejected by `calendar`. Local-time offsets need no conversion.

### Start recording

Default to **`meeting` mode** for anything ambiguous (meetings, calls, webinars, demos, conversations, "test this"). `mic` only for quick voice notes or explicit mic-only requests; `system` only for explicit system-audio-only requests.

```bash
python3 scripts/recordings.py start --title "<title>" --mode meeting --attendees "<invite list>" --attendees-source calendar
# Voice note / finite smoke test:
python3 scripts/recordings.py start --title "Voice note" --mode mic
python3 scripts/recordings.py start --title "Smoke test" --mode meeting --duration 10 --wait
```

Report: title, mode, session folder, PID, kanban path, captured attendee whitelist (or explicitly none), live-captions sidecar PID if launched (or explicitly skipped — it is off by default), the auto-stop cap if one was set (e.g. "stops itself at 14:00 = booked 13:30 + 30 min; ask me to stop it sooner"), and **"Recording started; transcription has not been started."** When the cap fires, the detached monitor finalizes the session on its own — the helper then reports `active: false` and card in `To Transcribe`; nothing transcribes until the user asks. For `--duration N --wait`, wait for the helper and also report finish details (audio files, `active: false`, card moved to `To Transcribe`).

### How system audio is captured (meeting/system mode)

`meeting`/`system` use the **native macOS CoreAudio process tap** (macOS 14.4+; no BlackHole or virtual loopback needed on this machine). BlackHole/named-device is only an automatic fallback if the tap is unavailable.

**Optional live-captions sidecar (OFF by default)**: for in-progress meeting Q&A ("what did we just agree?"), a best-effort MLX streaming sidecar can append `live-captions.txt`/`.srt` to the session folder while the recorder runs — launch/stop/answer conventions in `references/live-captions.md`. It is **opt-in** because the streaming model costs GPU/RAM on every recording: enable with `--live-captions` or `STT_LIVE_CAPTIONS=1` (`STT_NO_LIVE_CAPTIONS=1` force-disables). Best-effort only: never let it delay, fail, or alter a recording. A meeting recording produces `mic.wav` + `system.wav` only — `mixed.wav` is not auto-generated; run `stt mix <session>/mic.wav <session>/system.wav --output <session>/mixed.wav` afterwards if a reference mix is wanted.

First meeting/system run may trigger a one-time **System Audio Recording** permission prompt attributed to the `stt` app bundle (`com.larrysong.stt`); approve once. If denied, system audio is silently empty — fix under System Settings → Privacy & Security → System Audio Recording.

### Scheduled / delayed start

`--start-at "HH:MM[(:SS)]"` or ISO datetime, or `--delay <seconds>` (only one; must be future). The helper creates the session immediately with `status: "scheduled"` (pid null), waits **without holding the vault lock**, and prints a `scheduled` JSON payload (title, session dir, `scheduled_start_at`, `starts_in_seconds`, `expected_stop_at`, board path). A second `start` is refused while pending; `stop` (or SIGINT/SIGTERM to the waiting helper) cancels cleanly → `scheduled_cancelled` on `Failed / Needs Attention`. Combine with `--duration` and `--wait`; budget tool-call timeout as delay + duration + margin. The default calendar cap passes `--duration` **without** `--wait`, but a scheduled start still **holds the tool call through the whole pre-wait** (the payload is printed first, then the helper blocks until launch) — keep `--delay` short (the default 3-minute upcoming window) or run the call in the background; widen `--days` rather than `--within-minutes` when looking further ahead. Once launched, the call returns and the detached monitor finalizes when the cap fires.

### Stop recording

```bash
python3 scripts/recordings.py stop
```

Stops a live recording (SIGINT → SIGTERM → SIGKILL escalation, session finalized regardless, after re-verifying the PID is genuinely this recorder's detached process). For a **scheduled** recording, `stop` instead cancels the schedule (`recording_cancelled`, card → `Failed / Needs Attention`). Report: session folder, audio files produced (mic.wav + system.wav; mention `stt mix` for a combined track), kanban move, and that they can ask this skill to transcribe later.

### Timed recording (auto-stop after N minutes)

Auto-stop can come from three sources. **Explicit user duration** ("record this for 45 mins"): parse it into seconds (45 min → 2700s) and pass `--duration <s> --wait` — the recorder stops itself and the helper finalizes (default wait budget already scales: `max(duration + 120s, duration × 2)`). **No duration, meeting in the calendar** (the default): pass the lookup's `max_recording_seconds` as `--duration` with **no `--wait`** — the recorder stops itself at booked end + 30 min and the detached monitor finalizes the session without the call waiting for the recording (a scheduled start still holds the call through its pre-wait). A meeting that is not in the calendar, or no meeting found at all, gets no cap: keep recording until the user says stop, and say so in the start report. Either way the auto-stop is capture only and never triggers transcription. **Explicit no-calendar opt-out**: same `--duration`-without-`--wait` mechanics, with the fallback cap defined above (`STT_DEFAULT_MAX_RECORDING_SECONDS` ≥ 60, else 5400 s = 90 min) in place of a calendar-derived value — only when the user asked you not to check the calendar. If a recording is already active, `start` is refused — show `status` and ask; never kill it yourself.

### Status / list pending recordings

```bash
python3 scripts/recordings.py status
python3 scripts/recordings.py list --pending
```

`status` reports recording **and** transcription state in one JSON payload (nested `transcription` object: active PID/run id/resolved timeout, or reconciled outcome `transcribed` / `transcription_failed` / `transcription_interrupted`). Every helper invocation reconciles stale state files first under the vault lock — after a harness timeout or reboot, run `status` rather than assuming; it clears genuinely dead PIDs automatically. `list --pending` uses a real validated-transcript check (not stale metadata) and shows each session's captured `attendees` whitelist.

## Transcribe mode

**Recording only works from a GUI-attributed process (2026-09-14 incident).** macOS TCC attributes audio capture to the *responsible process* of the launching process tree. Anything spawned under an ssh login is attributed to `/usr/libexec/sshd-keygen-wrapper`, a platform binary macOS **never prompts for** — so `stt record` is denied microphone *and* system-audio access with no dialog, no `TCC.db` row (`stt permissions` keeps saying `notDetermined`), and 35 minutes of digital silence. The recorder's own log still claims success.

- Symptom: `mic.wav`/`system.wav` both at `max_volume ≈ -91 dB`. Check with `ffmpeg -i <track>.wav -af volumedetect -f null -`.
- Diagnose the tree: `sudo launchctl procinfo <pid> | grep 'responsible path'`. `sshd-keygen-wrapper` = recording will be silent; `/Applications/Ghostty.app/...` (or any GUI app) = fine.
- Fix: restart the agent host **from the Mac's own GUI** (a terminal window on its display). Stop the server by **signal, never the CLI** — CLI commands issued over ssh spawn a *new* ssh-attributed server, which is the auto-restart loop: `kill -TERM <server-pid>`, then let a GUI client respawn it (verify with `procinfo`).
- Never start a recording from an ssh-attributed tree. Two guards now fail loudly instead of silently: a silent capture is finalised as `recording_failed` (not `stopped`), and an all-`[Silence]` transcript is refused at promotion (`transcription_failed`).

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

"Record this meeting for 45 mins" (single request, full handling): run each stage in order, confirm completion before the next; transcription takes longer than the meeting — never assume it finished with the recording. Without an explicit duration the request is not fully one-shot: stage 1 starts capture with the derived cap (calendar, or the opt-out fallback) and the pipeline **pauses until the user says stop or that cap fires** before stages 2+ (the cap never transcribes by itself).

1. **Record.** Resolve the meeting, capture attendees. Explicit duration in the request → `start ... --duration <s> --wait` (auto-stop; report the `scheduled` payload first for future starts, then verify `active: false` and card in `To Transcribe`). No duration → `--duration <match.max_recording_seconds>` (booked end + 30 min, or the leftover grace for `recently_ended`) with no `--wait`, starting immediately or at the calendar time. If the user opted out of the calendar, skip both the lookup and the attendee capture, and use the fallback cap (still no `--wait`). Then wait for the user's "stop" or the cap: `recordings.py status` shows `active: false` once the cap has finalized the session (poll it every few minutes, not in a tight loop). Either way, verify `active: false` before continuing — `stop` itself errors with "No active recording state found" after the cap already fired. A `recently_ended` match is the one branch to double-check with the user: the meeting may already be over and the remaining grace short, so confirm the title and cap instead of silently truncating a different conversation.
2. **Transcribe.** `transcribe --session "<session>"` with the generous timeout above. On failure/interruption follow the failure steps (read log, `--resume`) — never file or delete from a session whose transcript did not land.
3. **Distill (delegate).** Invoke the **`distillation` subagent** with the session dir + artifact paths + attendee whitelist + title; it writes `<session>/summary.md` (see "Distillation" below). The helper never writes it, and you should not hand-roll it from the transcript — a long transcript floods the parent context. Read the subagent's returned report and surface its uncertainties (unnamed clusters, unclear figures, log warnings) to the user.
4. **File & update the customer's running note (only if one exists).** Look for the customer's meeting-series note under your vault's customer layout (e.g. `customers/<Customer>/<series>.md`), deriving the customer from the title ("ACME cadence" → `customers/ACME/ACME cadence.md`). If missing, **skip entirely** (never invent folders/notes) and say so. When it exists, append a dated `# YYYY-MM-DD` section at the **end** linking the session and carrying the distilled points; preserve existing content and order.
5. **Remove the .wav files (always).** The one-shot request pre-authorizes removal of this session's own top-level audio only, still following "Sound file cleanup" (status checks, transcript validation, enumerated scope, Trash not delete). Never trash `.stt-staging/`, `.speaker-clips/*.json`, provenance files, or profile samples.

Report each stage as it completes: start (title, folder, PID, expected stop), finish, transcription (paths, speaker outcome), distill, filing (note + section, or skipped), cleanup (files trashed, size).

## Distillation (summary.md)

`summary.md` is written by a **separate summary subagent** (a cheap, isolated worker process), never by the helper and never by the parent agent reading the transcript itself — the transcript is the largest artifact in the session and reading it in the main context crowds out everything else in the turn.

- Invoke it with your harness's subagent/worker tool: task = session dir, the `transcript.json` / `transcript.md` / `session.md` paths, the attendee whitelist, and the meeting title.
- Contract: exactly one file written, `<session>/summary.md` — evidence only from the transcript, unnamed diarisation clusters left as `Speaker N`, empty sections declared empty, numbers and quotes verbatim.
- The parent stays responsible for: speaker naming/relabel decisions, filing into the customer's running note, cleanup, and the kanban card. The subagent must not touch audio, `metadata.json`, or `session.md`.
- If the subagent returns without a non-empty `summary.md`, treat the distill stage as **failed** and say so — never quietly hand-write a thinner summary in its place.
- Fallback, when the harness has no subagent tool: write `summary.md` yourself to the same output contract.

## Speaker identification (summary)

`recordings.py transcribe` diarises and identifies automatically. The captured `attendees` whitelist is the reference set: `Speaker N` clusters should be checked against it; identified names matching invitees are expected; unexpected or missing voices should be surfaced to the user.

**Expect an unnamed cluster — that is the normal outcome.** Identification runs at threshold 0.78 / margin 0.05, so real invitees frequently come back anonymous (on 2026-09-18, two clusters of real attendees — Morgan Reed and Priya Nair — probed at 0.54–0.74 and were both left unnamed). Never report that as a pipeline failure, and never invent a name for a cluster. To put confirmed names on clusters **without enrolling anyone**, use the read-only path in `references/speaker-identification.md` → "Naming clusters without enrolling": `suggest-labels --provider speechbrain --force` first, lower the threshold only to *read* candidates, confirm with the user, then `relabel` (helper-only — `name_one_speaker.py`, not `stt relabel`).

When naming and enrolling new speakers — do **not** run the blocking `stt name-speakers` loop directly (a diarised cluster may contain multiple voices). Use the guarded workflow in `references/speaker-identification.md`: `audit --force` → `purity-preview` → `suggest-labels --provider speechbrain --force` → guarded `enroll` / `enroll-ranges` → `relabel`, with human confirmation before every profile mutation. Naming an already-enrolled speaker is **not** one of those mutations — it is read-only plus a transcript rewrite; see "Naming clusters without enrolling" in the same reference. Profiles live in `~/Library/Application Support/stt/speakers/` (local only). Enrollment, relabeling, and profile removal are mutations: describe them and get explicit authorization first — removal uses `stt speaker remove "<name>" --yes` only after the user approves.

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
- **Backend readiness failure**: show the output; suggest the stt backend bootstrap from the stt-cli repo.
- **Stale active-state refusals after reboot/timeout**: run `status` (it reconciles automatically); never hand-edit the state files.

## Kanban board

The helper maintains `meeting recordings.md` with lanes `Recording`, `To Transcribe`, `Transcribed`, `Failed / Needs Attention`, linking each session note. Let the helper modify the board; never hand-edit unless the helper fails and you have inspected the file. Card/state details: `references/kanban-states.md`.
