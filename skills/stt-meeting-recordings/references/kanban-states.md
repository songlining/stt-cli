# Failure states & kanban board (reference)

Loaded on demand from SKILL.md. Full failure matrix with recovery commands, and the kanban card/board format the helper maintains.

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
