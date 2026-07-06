# stt-cli

Native macOS speech-to-text CLI prototype in Swift, with a Python/MLX VibeVoice transcription backend.

## Current status

Implemented:

- Swift CLI commands: `doctor`, `devices`, `record`, `transcribe`, `pipeline`, `permissions`.
- Finite recordings via `--duration` for safe smoke tests.
- Microphone recording to WAV.
- Native CoreAudio process-tap system-output capture (macOS 14.4+ `AudioHardwareCreateProcessTap`), used as the primary `--mode system`/`--mode meeting` path and verified to capture real, non-silent system audio on Apple Silicon.
- Named input-device fallback for system audio (e.g. BlackHole/Aggregate Device), used automatically when the native tap is unavailable or fails.
- `.app` bundle wrapper for better macOS TCC attribution.
- Ad-hoc local signing with microphone entitlement.
- Python backend diagnostics in `stt doctor`.
- Pipeline metadata (`metadata.json`) written under each run directory.
- Best-effort WAV mix-down for compatible meeting-mode `mic.wav` + `system.wav` tracks.
- Meeting pipelines transcribe `mixed.wav` when mix-down succeeds and fall back to `mic.wav` with a metadata note when it does not.
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

See [`DISTRIBUTION.md`](DISTRIBUTION.md) for the Developer ID signing, notarization, and Homebrew cask plan.

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

Successful mix-down emits a non-fatal drift note when mic and system track durations differ by more than 0.25s. Use `--separate-tracks` to keep only `mic.wav` and `system.wav` without attempting mix-down when downstream diarization or manual alignment needs the original tracks.

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

For meeting pipelines, the CLI records `mic.wav` and `system.wav`, attempts to create `mixed.wav`, and transcribes the mixed track when possible. If mix-down fails, it transcribes `mic.wav` and records the fallback note in `metadata.json`. If mix-down succeeds but mic/system durations differ by more than 0.25s, the non-fatal drift note is also written to metadata. Pipeline metadata includes `transcribedAudioPath` so downstream tooling can see exactly which audio file was sent to the backend.

If MLX/mlx-audio is not installed, the pipeline records audio and writes failure details into `metadata.json`.

Transcribe an existing audio file with explicit backend settings:

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
