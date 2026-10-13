# Live captions sidecar (optional, best-effort)

A companion to recording mode: an MLX streaming-ASR sidecar appends a running
transcript while the recorder captures. The batch transcription pipeline
remains the canonical artifact — the sidecar exists to (a) answer questions
about an in-progress meeting in chat and (b) provide an instant draft and
fallback transcript. It is deliberately NOT part of the helper: no state
files, no kanban states, no helper changes.

This does not violate the "never transcribe while recording" rule: the
streaming model's load is a ~0.9s GPU burst every ~3.5s (RTF ≈ 0.26 on an
M4 Pro, 8-bit quant), not a batch transcription run.

## Runtime facts

- Script: `~/models/live_asr.py` (helper default model `~/models/vibevoice-asr-streaming-1.5b-mlx-8bit`; override with `STT_LIVE_CAPTIONS_MODEL=<path>`)
- Env: `~/models/venv-mlx-audio/bin/python` (mlx + mlx-audio, Metal)
- Output: stdout, plus `--text` (running transcript, `[+MM:SS]` per line) and
  `--srt` (arrival-based captions) sidecar files
- Model emits `[Noise]` / silence markers — skip these when answering
- Model choice: the **1.5B** (2.8GB, battery-friendly) is the only local
  checkpoint — `~/models/vibevoice-asr-streaming-1.5b-mlx-8bit` is the default in
  both the helper and `live_asr.py`. `STT_LIVE_CAPTIONS_MODEL=<path>` can still
  point at a different checkpoint if one is ever added.

## Launch (opt-in, off by default)

Live captions cost a streaming model's GPU/RAM for the whole recording, so the
sidecar is **off by default**. `recordings.py start` launches it only when asked:
`--live-captions` or `STT_LIVE_CAPTIONS=1` (meeting/mic modes; `system`-only capture
is skipped — no mic feed). `STT_NO_LIVE_CAPTIONS=1` force-disables. The started
payload reports the sidecar PID as `live_captions_pid` (null when not launched).
Scheduled starts launch it at the actual start time. `recordings.py stop` (and
auto-stop/reconciliation) shuts it down via `.live-captions.pid`.

Manual equivalent (only if launching outside the helper):

```bash
SESSION="<session dir>"
nohup "$HOME/models/venv-mlx-audio/bin/python" "$HOME/models/live_asr.py" \
  --model "$HOME/models/vibevoice-asr-streaming-1.5b-mlx-8bit" \
  --text "$SESSION/live-captions.txt" --srt "$SESSION/live-captions.srt" \
  --seconds 86400 \
  > "$SESSION/live-captions.log" 2>&1 &
echo $! > "$SESSION/.live-captions.pid"
```

- **The sidecar self-exits after `--seconds` — the script's default is 45!**
  The helper always passes `--seconds 86400` (24h) so captions outlive any
  meeting; when launching manually, pass it too. If captions stop mid-meeting,
  check the process is alive and the log lacks `done.`/a traceback, and
  relaunch with the same command.

- Report captions as ready only after `live-captions.log` shows
  `>>> SPEAK NOW <<<` (model load takes ~15s)
- **Recording first, captions best-effort**: never delay, fail, or abort a
  recording because the sidecar failed; if it dies, note it and move on
- The sidecar is not tracked by `status`; when launched manually it is stopped
  explicitly: `kill "$(cat "$SESSION/.live-captions.pid")"` after `recordings.py stop`
- Sidecar files (`live-captions.txt/.srt/.log`, `.live-captions.pid`) are not
  audio cleanup targets — leave them to the session's normal artifact handling

## Answering questions about an active meeting

When the user asks about an ongoing/recent meeting in chat ("what did we just
agree with X?", "action items so far?"):

1. Resolve the session: read `media/meeting-recordings/.active_recording.json`
   for the session dir; if inactive, use the most recent session that has a
   `live-captions.txt`.
2. Read `live-captions.txt` in full (tens of KB at most) and answer from it.
   No search/index machinery — read the file.
3. Cite meeting-relative time using the `[+MM:SS]` markers. If the transcript
   does not contain the answer, say so — do not extrapolate from the session
   title or prior customer notes.

Limits: streaming output has rough punctuation and chunk-granularity (~3s)
timing; treat it as content-accurate but not verbatim. The batch
`transcript.md` remains canonical once it lands.
