# stt-cli

Native macOS speech-to-text CLI prototype in Swift, with a Python/MLX VibeVoice transcription backend.

## Current status

Implemented:

- Swift CLI commands: `doctor`, `devices`, `record`, `transcribe`, `pipeline`, `permissions`.
- Finite recordings via `--duration` for safe smoke tests.
- Microphone recording to WAV.
- Named input-device fallback for system audio, e.g. BlackHole/Aggregate Device.
- `.app` bundle wrapper for better macOS TCC attribution.
- Ad-hoc local signing with microphone entitlement.
- Python backend diagnostics in `stt doctor`.
- Pipeline metadata (`metadata.json`) written under each run directory.
- Bounded validation script: `scripts/validate.sh`.

Still incomplete:

- Native CoreAudio process-tap system-output capture is probed but not wired.
- BlackHole fallback requires real routed audio to produce useful system-audio content.
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
- Python backend status CLI JSON/readiness checks
- App bundle build/sign smoke test
- Bundled `doctor`
- Codesign verification
- Finite mic recording smoke test
- Optional system fallback smoke test when `STT_SYSTEM_DEVICE` is set
- Pipeline metadata smoke test
- Standalone transcribe smoke test with a generated fake backend
- Successful pipeline smoke test with a generated fake backend

By default validation allows the transcription backend to be missing MLX dependencies, so recording and metadata checks still run on a fresh machine. To require full backend readiness:

```bash
STT_REQUIRE_BACKEND_READY=1 ./scripts/validate.sh
```

To validate a routed virtual/aggregate system-audio device, first route audio into the device, then run:

```bash
STT_SYSTEM_DEVICE="BlackHole 2ch" ./scripts/validate.sh
```

The optional check fails if the system fallback output is header-only/suspiciously small.

## Build app bundle

```bash
./scripts/build-app-bundle.sh
./dist/stt.app/Contents/MacOS/stt doctor
./dist/stt.app/Contents/MacOS/stt doctor --python-backend ./python
./dist/stt.app/Contents/MacOS/stt doctor --require-backend-ready
```

The app bundle uses bundle identifier `com.hashicorp.stt` by default and packages the local `python/stt_vibevoice` module under `Contents/Resources/python`, so `doctor`, `transcribe`, and `pipeline` can locate the backend module even when launched outside the repo root. Python dependencies still come from the selected Python environment.

Override the bundle ID for local experiments:

```bash
BUNDLE_ID=com.example.stt ./scripts/build-app-bundle.sh
```

## Manual TCC attribution smoke test

Run without resetting permissions:

```bash
./scripts/manual-tcc-smoke.sh
```

Run with a microphone permission reset for the app bundle ID:

```bash
STT_RESET_TCC=1 ./scripts/manual-tcc-smoke.sh
```

When using `STT_RESET_TCC=1`, confirm the macOS microphone prompt is attributed to `stt` / `com.hashicorp.stt`, not Terminal.

To also validate a routed virtual/aggregate system-audio device during the manual smoke test:

```bash
STT_SYSTEM_DEVICE="BlackHole 2ch" ./scripts/manual-tcc-smoke.sh
```

The script fails if either mic or optional system fallback recording is header-only/suspiciously small.

## Common commands

List input devices:

```bash
./dist/stt.app/Contents/MacOS/stt devices
```

Record microphone for two seconds:

```bash
./dist/stt.app/Contents/MacOS/stt record --mode mic --duration 2 --output /tmp/mic.wav
```

Record using a named virtual input device fallback:

```bash
./dist/stt.app/Contents/MacOS/stt record --mode system --input-device "BlackHole 2ch" --duration 5 --output /tmp/system.wav
```

Run a bounded pipeline:

```bash
STT_HOME=/tmp/stt-run \
./dist/stt.app/Contents/MacOS/stt pipeline \
  --mode mic \
  --name smoke \
  --duration 2 \
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
