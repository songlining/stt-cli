# External audio transcription (podcasts, YouTube, files)

External-audio mode: transcribe anything that is **not a live recording** — podcasts (Spotify/Anchor), YouTube, interviews, downloaded audio files, audiobooks — into a session folder in the Obsidian vault, following the same kanban/metadata conventions as meeting sessions so results show up on the `meeting recordings.md` board.

The heavy lifting (chunked transcription, staged promotion, kanban bookkeeping) is done by the helper scripts in this same skill (`recordings.py` start/stop/transcribe/status/list, `name_one_speaker.py`). This mode adds: acquiring audio from arbitrary sources, converting it, single-track speaker diarisation, translation, and safe cleanup of source audio.

## Prerequisites / key paths

| Thing | Path |
|---|---|
| stt CLI (binary) | `<stt-cli-repo>/dist/stt.app/Contents/MacOS/stt` |
| ASR backend venv (MLX VibeVoice) | `<stt-cli-repo>/python/.venv` |
| Diarisation venv (speechbrain/torch, py3.11) | `<stt-cli-repo>/runtime/.venv` |
| ASR model (cached) | `mlx-community/VibeVoice-ASR-8bit` in `~/.cache/huggingface/hub` |
| Vault recordings root | `~/obsidian-notes/media/meeting-recordings/` |
| Helper scripts | `scripts/` |

Check the stack once with `stt doctor` (needs `--python-backend <stt-cli-repo>/python`). It reports Apple Silicon, ffmpeg, and backend readiness (mlx/mlx_audio/sentencepiece missing means the ASR venv is not set up — bootstrap it in stt-cli first).

## Workflow

### 1. Acquire the audio

Determine the source type first:

- **Spotify podcast episode** (creators.spotify.com or podcasters.spotify.com): **yt-dlp does NOT work** — `open.spotify.com` is DRM-locked and the creators pages are unsupported by yt-dlp's generic extractor. Instead, scrape the direct MP3 URL:
  ```bash
  curl -sL -A "Mozilla/5.0" "https://creators.spotify.com/pod/profile/<show>/episodes/<slug>-<episodeid>/a-<...>" -o page.html
  grep -oE '"audioUrl":"[^"]*"' page.html
  ```
  The value is JSON-escaped (`https:\u002F\u002F...`) — decode `\u002F` to `/`. Download it as `episode.mp3`. Also grab `"title":"..."` for the session title. Use the bundled script instead:
  ```bash
  python3 scripts/fetch_audio.py "<url>" --out-dir <session-dir>
  ```
- **YouTube / most other sites**: `yt-dlp -f bestaudio <url> -o episode.%(ext)s` works.
- **Local file**: use it directly, no fetch needed.

### 2. Convert to WAV

```bash
ffmpeg -y -v error -i episode.mp3 -ar 16000 -ac 1 -c:a pcm_s16le episode.wav
```

The backend normalises to 16 kHz mono itself, but pre-converting makes the session folder self-contained and is required for the helper to discover the file (it only globs `*.wav`). Verify with `ffprobe -show_entries format=duration` — you need the duration for the transcript header and timeout planning.

### 3. Create the session folder + metadata

```bash
mkdir -p media/meeting-recordings/<YYYYMMDD>-<slug>
```

Write a minimal `metadata.json` so the helper accepts the session (it refuses sessions outside the recordings root and sessions without metadata):

```json
{
  "title": "<episode title>",
  "mode": "mic",
  "status": "stopped",
  "session_dir": "/abs/path/to/session",
  "started_at": "<ISO>", "started_at_human": "<YYYY-MM-DD HH:MM>",
  "ended_at": "<ISO>", "ended_at_human": "<YYYY-MM-DD HH:MM>",
  "source": "<original URL>",
  "audio_files": ["/abs/path/to/episode.wav"]
}
```

`mode: mic` is important — it makes the helper treat this as a single-file session (a meeting session expects `mic.wav` + `system.wav`). Confirm the helper sees it: `recordings.py list --pending` should show the session with `has_transcript: false`.

### 4. Transcribe (chunk by chunk)

```bash
cd <session-dir>
nohup python3 scripts/recordings.py transcribe --session "<session-dir>" > transcribe_helper.log 2>&1 &
```

`recordings.py transcribe` runs the correct `stt transcribe episode.wav` with the adaptive timeout, writes into a per-run staging dir (`.stt-staging/<run_id>/`), validates output, then **journalled-promotes** `transcript.md`/`transcript.json` into the session root, updates `metadata.json`, `session.md`, and moves the kanban card to `Transcribed`. Diarisation is NOT included for single-file sessions (the helper only passes `--diarize` for meeting track pairs) — do that in step 5.

**Chunking internals**: the backend (`stt_vibevoice/transcribe.py`) normalises to 16 kHz mono, and if the audio is **≥ 10 minutes** it splits it into **5-minute windows with 15 s overlap**, transcribes each window separately with the MLX VibeVoice-ASR-8bit model (GPU, CPU fallback on Metal timeout/OOM), then merges the per-chunk segments with boundary dedup (fuzzy word similarity ≥ 0.85 + timing overlap). Expect roughly **1–1.5 min of compute per 5-min chunk** — a 2-hour episode is ~25 chunks and ~30–40 min wall time.

Monitor progress (do not kill it):
```bash
python3 .../recordings.py status        # transcription.active shows pid/run_id/resolved_timeout
ls /var/folders/*/*/stt-vibevoice-chunks-*/ 2>/dev/null | wc -l   # chunk count so far
```
The resolved timeout is `max(1800, 4 × duration_s + 300)`. If a harness timeout kills the helper, the `stt` child keeps running detached — check `status` and use `transcribe --resume --session <dir>` rather than assuming failure.

When done, verify: `status` shows the session `transcribed`, and `transcript.md` (with metadata header) + `transcript.json` exist in the session root.

### 5. Diarise — assign Speaker 0 / Speaker 1…

The ASR model's own segmenter may already emit rough `speaker_id`s, but the canonical voice-cluster labels come from ECAPA embeddings. Run:

```bash
cd <session-dir>
cp transcript.json transcript.model-speakers.json   # keep the pre-diarisation copy
<stt-cli-repo>/dist/stt.app/Contents/MacOS/stt diarize \
  --audio episode.wav --transcript transcript.json \
  --python-backend <stt-cli-repo>/runtime/
```

This updates `transcript.json` in place (every segment gets `speaker_id`). A 2-person podcast should cluster to 2 speakers — one dominant (the guest/interviewee usually holds ~80% of speech) and one interviewer. Use `--num-speakers N` if auto-clustering over-splits. Then regenerate the markdown and `text`/`diarised_text` from the diarised segments:

```bash
python3 scripts/regenerate_transcript_md.py --session . --header
```

The bundled script recomputes `text`/`diarised_text` via the same `flatten_segments` logic the backend uses and rewrites `transcript.md` with the metadata header. It also strips the `Unknown ` source-label prefix that appears in single-source sessions (segments have no `source` field).

### 6. Name speakers (optional, cheap)

Once the user tells you who is who (e.g. "Speaker 1 is Steve Keen, Speaker 0 is Robinson Erhardt"), relabel — dry run first, then apply:

```bash
python3 .../name_one_speaker.py relabel --session . --speaker-id 1 --name "Steve Keen" --dry-run
python3 .../name_one_speaker.py relabel --session . --speaker-id 1 --name "Steve Keen"
```

`relabel` works purely on `transcript.json` (no audio needed) and preserves `speaker_id`, timestamps, and text. It regenerates `transcript.md` in body-only form, so re-run the header restore: `python3 .../regenerate_transcript_md.py --session . --header`. Note: relabel only labels this transcript — it does not enroll profiles. Enrolling (so future episodes auto-identify) needs audio slices, which requires the source WAV.

### 7. Translate (optional)

To produce a translated transcript: translate **segment by segment** (keeps quality high and preserves the `[start - end] Speaker: text` structure), writing to `transcript.<lang>.md` (e.g. `transcript.zh-CN.md`) with a small header (title, speakers, backend info, note that non-speech tags are kept as-is). After translating, **verify 1:1 coverage programmatically** — dropped/merged segments are the classic failure mode:

```bash
python3 .../verify_translation.py --source transcript.md --translated transcript.zh-CN.md
```

The script asserts every source timestamp appears exactly once, in order, with nothing extra. Link the translation in `session.md` under the Transcript section.

### 8. Clean up source audio (ONLY on explicit user request)

Follow the "Sound file cleanup" rules in the main SKILL.md (Trash not delete, status checks, enumerated scope, metadata journaling). Enumerate the exact audio files, move them to **macOS Trash** with `trash`, update `metadata.json` (`audio_files: []`, append `audio_cleanup_events`), and update only the `Audio files` section of `session.md`.

## Failure handling

- **`stt` binary or backend not ready**: run `stt doctor`; the ASR venv needs `mlx-audio[stt]`, the diarisation venv needs speechbrain/torch (Python 3.11). Bootstrap in stt-cli (`scripts/bootstrap-python-backend.sh`).
- **Model not cached**: first run downloads `mlx-community/VibeVoice-ASR-8bit` (~3 GB) from Hugging Face — takes a while; don't mistake it for a hang.
- **yt-dlp unsupported/DRM error**: the source is likely Spotify — switch to the scrape path (step 1) or ask the user for the creators.spotify.com link.
- **Diarisation over-splits** (3+ clusters for a 2-person show): retry with `--num-speakers 2`.
- **transcribe helper interrupted**: `status` first; then `transcribe --resume --session <dir>`.
- **Cleanup scope doubts**: stop and confirm the exact file list with the user before trashing anything.
