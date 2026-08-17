# stt-cli

Native macOS speech-to-text CLI prototype in Swift, with a Python/MLX VibeVoice transcription backend.

[![CI](https://github.com/songlining/stt-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/songlining/stt-cli/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

## Current status

Implemented:

- Swift CLI commands: `doctor`, `devices`, `record`, `mix`, `transcribe`, `transcribe-meeting`, `pipeline`, `permissions`.
- Finite recordings via `--duration` for safe smoke tests.
- Microphone recording to WAV.
- Native CoreAudio process-tap system-output capture (macOS 14.4+ `AudioHardwareCreateProcessTap`), used as the primary `--mode system`/`--mode meeting` path and verified to capture real, non-silent system audio on Apple Silicon.
- Named input-device fallback for system audio (e.g. BlackHole/Aggregate Device), used automatically when the native tap is unavailable or fails.
- `.app` bundle wrapper for better macOS TCC attribution.
- Ad-hoc local signing with microphone entitlement.
- Python backend diagnostics in `stt doctor`.
- Pipeline metadata (`metadata.json`) written under each run directory.
- Best-effort WAV mix-down for compatible meeting-mode `mic.wav` + `system.wav` tracks.
- Meeting pipelines default to separate mic/system transcription and timestamp-based transcript merging so overlapping microphone speech is not masked by system audio. `mixed.wav` remains available for playback/reference, and `--meeting-transcription mixed` preserves the legacy single-pass mixed-audio behavior.
- Speaker identification via pluggable embedding providers (`mfcc-test` for tests, `speechbrain` for real ECAPA-VoxCeleb embeddings), including a **safe speaker enrollment workflow** that guards against mixed and duplicate diarisation clusters: `stt speaker audit`, `purity-preview`, `suggest-labels`, `enroll-ranges`, and segment-level `relabel` — with provenance metadata recorded on enrolled profiles. See the **Speaker identification** section below.
- Bounded validation script: `scripts/validate.sh`.

Still incomplete:

- BlackHole/named-device fallback requires real routed audio to produce useful system-audio content (it is only used when the native tap is unavailable or fails).
- Fresh TCC prompt attribution must be manually confirmed after resetting permissions.
- Distribution signing/notarization is not implemented.

## Validate locally

```bash
./scripts/validate.sh
```

This runs:

- `swift build`
- `swift test`
- `swift run sttUnitChecks`
- Python tests via `python/.venv/bin/python -m pytest python/tests`
- Python backend status CLI JSON/readiness checks via `scripts/check-python-backend.sh`
- App bundle build/sign smoke test
- Static app-bundle TCC metadata and entitlement checks, including custom `BUNDLE_ID` override coverage
- Bundled `doctor`
- Codesign verification
- Finite mic recording smoke test (unless `STT_SKIP_MIC_HARDWARE=1`)
- Optional system fallback smoke test when `STT_SYSTEM_DEVICE` is set
- Meeting missing-device smoke test (unless `STT_SKIP_MIC_HARDWARE=1`)
- Pipeline metadata smoke test (unless `STT_SKIP_MIC_HARDWARE=1`)
- Standalone WAV mix smoke test
- Standalone transcribe smoke test with a generated fake backend and synthetic WAV fixture
- Invalid `--python-backend` preflight smoke tests
- Successful/failing/timeout pipeline smoke tests with generated fake backends (unless `STT_SKIP_MIC_HARDWARE=1`)

By default validation allows the transcription backend to be missing MLX dependencies, so recording and metadata checks still run on a fresh machine. To require full backend readiness:

```bash
STT_REQUIRE_BACKEND_READY=1 ./scripts/validate.sh
```

For CI or other non-interactive environments without microphone/TCC access, run the hardware-free subset:

```bash
STT_SKIP_MIC_HARDWARE=1 ./scripts/validate.sh
```

This still runs build/test, Python tests, app-bundle checks, backend readiness checks, synthetic WAV/fake-backend transcribe checks, invalid-backend preflight checks, and static/deterministic validation. It intentionally skips real microphone capture and pipeline paths that would open the microphone.

To validate a routed virtual/aggregate system-audio device, first route audio into the device, then run:

```bash
STT_SYSTEM_DEVICE="BlackHole 2ch" ./scripts/validate.sh
```

The optional check fails if the system fallback output is header-only/suspiciously small.

## Continuous integration

GitHub Actions runs `.github/workflows/ci.yml` on macOS with:

```bash
./scripts/bootstrap-python-backend.sh
STT_SKIP_MIC_HARDWARE=1 ./scripts/validate.sh
```

CI intentionally does not install MLX, reset TCC, open the microphone, validate BlackHole/system routing, or exercise native CoreAudio process-tap capture. Run the full local validation and manual TCC/system-audio smoke tests before relying on hardware-specific behavior.

## Build app bundle

```bash
./scripts/build-app-bundle.sh
./scripts/check-app-bundle.sh
./dist/stt.app/Contents/MacOS/stt doctor
./dist/stt.app/Contents/MacOS/stt doctor --python-backend ./python
./dist/stt.app/Contents/MacOS/stt doctor --require-backend-ready
```

The app bundle uses bundle identifier `com.larrysong.stt` by default and packages the local `python/stt_vibevoice` module under `Contents/Resources/python`, so `doctor`, `transcribe`, and `pipeline` can locate the backend module even when launched outside the repo root. Python dependencies still come from the selected Python environment. `scripts/check-app-bundle.sh` statically verifies the bundle identifier, TCC usage-description strings, and microphone entitlement without launching the app.

Override the bundle ID for local experiments:

```bash
BUNDLE_ID=com.example.stt ./scripts/build-app-bundle.sh
```

Local builds are ad-hoc signed by default. Developer ID signing and hardened runtime are opt-in release-maintainer actions:

```bash
CODESIGN_IDENTITY="Developer ID Application: Example, Inc. (TEAMID)" HARDENED_RUNTIME=1 ./scripts/build-app-bundle.sh
```

See [`DISTRIBUTION.md`](docs/DISTRIBUTION.md) for the Developer ID signing, notarization, and Homebrew cask plan.

## Manual TCC attribution smoke test

Run without resetting permissions:

```bash
./scripts/manual-tcc-smoke.sh
```

Run with a microphone permission reset for the app bundle ID:

```bash
STT_RESET_TCC=1 ./scripts/manual-tcc-smoke.sh
```

When using `STT_RESET_TCC=1`, confirm the macOS microphone prompt is attributed to `stt` / `com.larrysong.stt`, not Terminal.

To validate native CoreAudio process-tap system capture with real system audio playing:

```bash
./scripts/manual-native-tap-smoke.sh
```

To also validate a routed virtual/aggregate system-audio device during the manual smoke test:

```bash
STT_SYSTEM_DEVICE="BlackHole 2ch" ./scripts/manual-tcc-smoke.sh
```

The scripts fail if the relevant recording is header-only/suspiciously small.

Print permission reset guidance for the default bundle ID, or for a custom local bundle ID:

```bash
./dist/stt.app/Contents/MacOS/stt permissions reset-help
./dist/stt.app/Contents/MacOS/stt permissions reset-help --bundle-id com.example.stt
```

## Common commands

List input devices:

```bash
./dist/stt.app/Contents/MacOS/stt devices
```

Record microphone for two seconds:

```bash
./dist/stt.app/Contents/MacOS/stt record --mode mic --duration 2 --fail-if-empty --output /tmp/mic.wav
```

Record using a named virtual input device fallback:

```bash
./dist/stt.app/Contents/MacOS/stt record --mode system --input-device "BlackHole 2ch" --duration 5 --fail-if-empty --output /tmp/system.wav
```

Record meeting tracks and attempt a post-capture `mixed.wav` when both tracks are compatible 16-bit PCM WAV. If mic/system sample rates differ, mix-down upsamples the lower-rate track to the higher rate before mixing:

```bash
./dist/stt.app/Contents/MacOS/stt record --mode meeting --input-device "BlackHole 2ch" --duration 5 --output-dir /tmp/meeting
```

Successful mix-down emits a non-fatal drift note when mic and system track durations differ by more than 0.25s. Use `--separate-tracks` to keep only `mic.wav` and `system.wav` without attempting mix-down when downstream diarization or manual alignment needs the original tracks. For transcription correctness, prefer the default separate-track transcription path rather than feeding `mixed.wav` into one ASR pass.

Mix two existing compatible WAV tracks manually:

```bash
./dist/stt.app/Contents/MacOS/stt mix /tmp/meeting/mic.wav /tmp/meeting/system.wav --output /tmp/meeting/mixed.wav --fail-if-empty
```

Run a bounded microphone pipeline:

```bash
STT_HOME=/tmp/stt-run \
./dist/stt.app/Contents/MacOS/stt pipeline \
  --mode mic \
  --name smoke \
  --duration 2 \
  --fail-if-empty \
  --transcribe-timeout 30 \
  --device cpu \
  --model mlx-community/VibeVoice-ASR-8bit \
  --max-new-tokens 4096
```

Fail before recording if the transcription backend is not ready:

```bash
./dist/stt.app/Contents/MacOS/stt pipeline \
  --mode mic \
  --duration 2 \
  --require-backend-ready
```

For meeting pipelines, the CLI records `mic.wav` and `system.wav`, attempts to create `mixed.wav`, then by default transcribes `mic.wav` and `system.wav` independently and merges their timestamped transcript segments into the final transcript. This avoids the common failure mode where a single ASR pass over `mixed.wav` follows the louder/clearer system-audio speaker and drops overlapping microphone speech. If mix-down fails, the original tracks are still transcribed separately. If mix-down succeeds but mic/system durations differ by more than 0.25s, the non-fatal drift note is written to metadata. Pipeline metadata includes `transcribedAudioPath` so downstream tooling can see which audio files were sent to the backend. Use `--meeting-transcription mixed` only when you explicitly want the legacy single-pass `mixed.wav` transcription behavior.

If MLX/mlx-audio is not installed, the pipeline records audio and writes failure details into `metadata.json`.

Transcribe existing meeting tracks with separate mic/system ASR passes and a merged timestamped transcript:

```bash
./dist/stt.app/Contents/MacOS/stt transcribe-meeting /tmp/meeting/mic.wav /tmp/meeting/system.wav \
  --output /tmp/meeting/transcript.md \
  --json /tmp/meeting/transcript.json \
  --device gpu \
  --timeout 300 \
  --model mlx-community/VibeVoice-ASR-8bit \
  --max-new-tokens 4096 \
  --python-backend ./python \
  --require-backend-ready
```

This also writes per-source artifacts next to the merged output, such as `transcript.mic.txt`, `transcript.mic.json`, `transcript.system.txt`, and `transcript.system.json`.

The ASR model (e.g. `mlx-community/VibeVoice-ASR-8bit`) is downloaded from Hugging Face on first use and cached in the default Hugging Face cache (`~/.cache/huggingface`). Ensure network access for the first run, or pre-populate the cache directory on another machine and copy it over.

Transcribe an existing single audio file with explicit backend settings:

```bash
./dist/stt.app/Contents/MacOS/stt transcribe /tmp/mic.wav \
  --output /tmp/mic.txt \
  --json /tmp/mic.json \
  --device gpu \
  --timeout 300 \
  --model mlx-community/VibeVoice-ASR-8bit \
  --max-new-tokens 4096 \
  --python-backend ./python \
  --require-backend-ready
```

## Speaker identification (speechbrain provider)

`stt speaker enroll`, `stt identify`, and `--identify-speakers` use a pluggable embedding provider (see `docs/SPEAKER_IDENTIFICATION_PLAN.md`). Two providers exist:

- `mfcc-test`: stdlib-only, deterministic, but not real speaker recognition. Used for tests/smoke checks only -- never present it to users as accurate identity matching.
- `speechbrain`: real speaker embeddings using `speechbrain/spkrec-ecapa-voxceleb` (ECAPA-TDNN trained on VoxCeleb). This is the provider to use for actual speaker identification.

### Requirements

The `speechbrain` provider needs the optional `speechbrain` and `torchaudio` packages (which pull in PyTorch). As of this writing, PyTorch does not reliably support every CPython release on Apple Silicon, so:

- Use a **Python 3.11 or 3.12** interpreter for the backend venv, not the newest CPython (e.g. not the default Homebrew `python3` if it is 3.13/3.14). Check available interpreters with `command -v python3.12 python3.11 python3`.
- CPU (and, best-effort, Apple Silicon MPS) inference is used; no CUDA/GPU passthrough is needed on macOS.

### Install

```bash
./scripts/bootstrap-python-backend.sh --python python3.11 --speechbrain --check
```

Or for a non-default runtime:

```bash
STT_VIBEVOICE_RUNTIME=/opt/stt-runtime ./scripts/bootstrap-python-backend.sh --python python3.11 --speechbrain
export STT_VIBEVOICE_RUNTIME=/opt/stt-runtime
```

Equivalent manual install:

```bash
python3.11 -m venv python/.venv
python/.venv/bin/pip install --upgrade pip setuptools wheel
python/.venv/bin/pip install -e 'python[speechbrain]'
```

### Model download and cache

The first `speechbrain` extraction downloads `speechbrain/spkrec-ecapa-voxceleb` from Hugging Face and caches it locally under `~/.cache/stt-cli/speechbrain/<model-slug>` (override with `STT_SPEECHBRAIN_CACHE`). Subsequent extractions reuse the cached model and work offline. Ensure network access is available for the first run, or pre-populate the cache directory on another machine and copy it over.

### Readiness check

`python3 -m stt_vibevoice.status` reports both provider availability and a Python-version hint:

```bash
PYTHONPATH=./python python/.venv/bin/python -m stt_vibevoice.status
# speaker-id provider mfcc-test: available
# speaker-id provider speechbrain: not installed
# speaker-id provider speechbrain hint: current interpreter is Python 3.14.6; use Python 3.11 or 3.12 ...
```

Speaker identification is opt-in and does not affect the overall `ready` status used by `--require-backend-ready` for transcription; that flag only covers the MLX/VibeVoice ASR backend.

## Agent skill

This repo bundles the `stt-meeting-recordings` agent skill at `skills/stt-meeting-recordings/` — SKILL.md plus the meeting-recordings helper scripts (`recordings.py`, `meeting_lookup.py`, `name_one_speaker.py`) and their tests. The directory follows the common `skills/` layout so any harness can use it: pi picks it up via the repo's `.pi/settings.json` (which points at `../skills`); Claude Code and other agents can symlink or configure the same directory. It is a sanitized copy of the author's personal workflow: set `STT_BIN` and `OBSIDIAN_VAULT` to your own stt build and notes vault (defaults: repo-root build, `~/obsidian-notes`). Instructions for the Obsidian kanban/`outlook-cli` integration are documented in the skill; the underlying `record`/`transcribe`/`speaker` commands are what this README covers.

### Usage

```bash
stt speaker enroll "Larry Song" --audio /path/to/larry.wav --provider speechbrain
stt identify /tmp/clip.wav --provider speechbrain --json /tmp/clip-id.json
stt transcribe meeting.wav --identify-speakers --speaker-provider speechbrain
```

If `speechbrain`/`torchaudio` are missing, or the current interpreter can't import them, these commands fail with an actionable `SpeakerIdError` message rather than a raw stack trace.

### Safe speaker enrollment (mixed & duplicate clusters)

Real diarisation can produce **mixed clusters** (two people collapsed into one `Speaker N`) and **duplicate clusters** (the same person split across mic/system tracks). Enrolling a whole mixed cluster would build a profile from two voices and contaminate it. `stt speaker` ships read-only safety commands plus a guarded `enroll-ranges` for confirmed-speech enrollment. All four commands need the helper scripts directory (see `STT_HELPER_SCRIPTS` / `--helper-script` below) and a ready Python backend:

```bash
# 1. Audit every cluster (read-only) → speaker_audit.json with safe_to_enroll_whole_cluster flags
stt speaker audit --transcript <session>/transcript.json \
  --mic <session>/mic.wav --system <session>/system.wav --python-backend ./python --helper-script skills/stt-meeting-recordings/scripts

# 2. Audio-confirm a suspicious cluster (early/middle/late + best-energy clips)
stt speaker purity-preview --transcript <session>/transcript.json \
  --speaker-id <id> --mic <session>/mic.wav --system <session>/system.wav --python-backend ./python --helper-script skills/stt-meeting-recordings/scripts

# 3. Match clusters against profiles; flags duplicate/mixed clusters (read-only)
stt speaker suggest-labels --transcript <session>/transcript.json \
  --mic <session>/mic.wav --system <session>/system.wav --provider speechbrain --python-backend ./python --helper-script skills/stt-meeting-recordings/scripts

# 4. Enroll a mixed cluster from only confirmed ranges (never whole-cluster audio)
stt speaker enroll-ranges "<name>" --transcript <session>/transcript.json \
  --speaker-id <id> --range 12.0-45.0 --range 200.0-240.0 \
  --mic <session>/mic.wav --system <session>/system.wav --python-backend ./python --helper-script skills/stt-meeting-recordings/scripts
```

`audit` classifies each cluster as `pure_likely` (safe for whole-cluster enrollment), `mixed_suspected` (unsafe — use `enroll-ranges`), or `unknown` (too little speech). `enroll-ranges` builds a range-limited sample from **only** the requested ranges and records provenance (source session, transcript, track, diarised speaker id, confirmed ranges) on the profile; it never falls back to whole-cluster audio. `enroll-ranges --no-enroll` is a validation-only dry run.

For the turn-by-turn agent workflow (preview → wait for the user's name → enroll → relabel transcript segments by confirmed name while preserving `speaker_id`), the `name_one_speaker.py` helper exposes the same commands plus a guarded `enroll` (which refuses unsafe whole clusters before any audio playback) and a `relabel` command. The helper ships in this repo as part of the bundled agent skill at `skills/stt-meeting-recordings/scripts/` (see **Agent skill** below). Point the CLI at it with the `STT_HELPER_SCRIPTS` environment variable or the `--helper-script <dir>` option. If neither is set, the `speaker audit` / `purity-preview` / `enroll-ranges` commands fail with an actionable error rather than guessing a location.

## Python backend readiness

`stt doctor` prints Python backend readiness and an actionable setup hint when dependencies are missing. Add `--require-backend-ready` when you want `doctor` to exit non-zero unless the transcription backend is fully ready. The backend is ready only when Apple Silicon, ffmpeg/ffprobe, and required modules are available. The Python status module also supports machine-readable checks:

```bash
python3 -m stt_vibevoice.status --json
python3 -m stt_vibevoice.status --fail-if-not-ready
./scripts/check-python-backend.sh --json
./scripts/check-python-backend.sh --strict
```

Backend lookup order is:

1. explicit `--python-backend <dir>` on `doctor` / `transcribe` / `pipeline`
2. `STT_PYTHON_BACKEND`
3. `./python` from the current working directory
4. `stt.app/Contents/Resources/python`


Bootstrap the local Python backend environment without MLX (fast/dev default):

```bash
./scripts/bootstrap-python-backend.sh
```

Install the MLX/VibeVoice dependencies and check readiness:

```bash
./scripts/bootstrap-python-backend.sh --mlx --check
```

For a separate runtime root:

```bash
STT_VIBEVOICE_RUNTIME=/opt/stt-runtime ./scripts/bootstrap-python-backend.sh --mlx
export STT_VIBEVOICE_RUNTIME=/opt/stt-runtime
```

Equivalent manual install commands:

```bash
python3 -m pip install --upgrade pip setuptools wheel
python3 -m pip install 'mlx-audio[stt]'
```

## Notes on tests

This machine has Command Line Tools but not full Xcode/XCTest. The Swift package therefore uses Swift Testing plus an executable check harness (`sttUnitChecks`) for deterministic validation in this environment.

## License

Licensed under the [MIT License](LICENSE).
