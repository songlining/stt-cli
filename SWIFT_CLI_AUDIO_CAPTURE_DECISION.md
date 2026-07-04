# Swift CLI Decision for macOS Native Audio Capture

## Summary

For a macOS-only, latest-macOS-only speech-to-text CLI that needs native system audio capture, building the CLI in **Swift** is likely the best balance.

The main reason is that the hardest part of the project is not command-line parsing or transcription orchestration; it is reliable **macOS system/output audio capture**. That work sits directly in Apple API territory: CoreAudio taps and ScreenCaptureKit for system output audio (AVFoundation can only capture the microphone, **not** system output), plus macOS permissions, app identity, signing, and entitlements.

Using Swift avoids the need to ship Electron while also avoiding much of the Rust FFI complexity around Apple frameworks.

## Recommendation

Build the CLI as a native Swift command-line tool.

Use Swift for:

- CLI commands and options
- microphone recording
- input device listing and selection
- native system audio capture
- meeting mode capture
- macOS permissions checks
- WAV/chunk writing
- launching the transcription backend

Keep the transcription backend separate if needed, for example Python/MLX, and call it from Swift using `Foundation.Process`.

## Why not Electron?

Electron plus `electron-audio-loopback` would probably be the fastest way to reuse an already-working system-audio capture path. However, it adds a large runtime and packaging burden.

Pros of Electron helper:

- Faster path if `electron-audio-loopback` already works
- Avoids deep CoreAudio/ScreenCaptureKit implementation initially
- Browser APIs make audio stream mixing convenient

Cons:

- Ships Chromium/Electron for a CLI
- Much larger dependency footprint
- More complicated packaging
- Slower startup
- Less native-feeling CLI
- Still has macOS permission/signing considerations

If the goal is a lightweight macOS-native CLI, Electron should be avoided unless speed-to-prototype is more important than product shape.

## Why Swift over Rust?

Rust is excellent for cross-platform CLI tools, but this project is constrained to macOS and depends heavily on Apple-native APIs.

Swift is better suited for this project because it provides first-class access to:

- CoreAudio
- AVFoundation
- ScreenCaptureKit
- AudioToolbox
- CoreFoundation/Foundation
- macOS app identity and permissions behavior
- Xcode signing and entitlement workflows

In Rust, the same functionality would likely require:

- FFI bindings
- `unsafe` CoreAudio/CoreFoundation handling
- manual bridging of Apple types
- sparse examples for modern audio tap APIs
- more time spent fighting platform interop than building the product

## Decision Matrix

| Area | Swift | Rust | Notes |
|---|---:|---:|---|
| macOS native APIs | Excellent | Awkward | Swift is the natural Apple API language. |
| CoreAudio / audio taps | Excellent | Possible but harder | Rust likely needs FFI and unsafe code. |
| ScreenCaptureKit | Excellent | Awkward | Swift examples and API support are stronger. |
| macOS permissions/signing | Better | Harder | Swift/Xcode fits the TCC/signing model. |
| CLI parsing | Good | Excellent | Swift ArgumentParser is good enough. |
| Cross-platform future | Poor | Excellent | Rust wins if Windows/Linux become important. |
| Single small binary | Good | Better | Swift has runtime/platform assumptions. |
| Audio capture implementation speed | Faster | Slower | Especially for system audio. |

## Target Architecture

```text
stt Swift CLI
  ├── CLI command parsing
  ├── audio device discovery
  ├── microphone capture
  ├── native system audio capture
  ├── meeting mode capture
  ├── permissions and diagnostics
  ├── WAV/chunk output
  └── transcription orchestration
        └── Python/MLX or another backend
```

The CLI can remain a normal command-line tool while using native macOS APIs internally.

Example commands:

```bash
stt doctor
stt devices
stt record --mode mic --output mic.wav
stt record --mode system --output system.wav
stt record --mode meeting --output meeting.wav
stt transcribe meeting.wav
stt pipeline --mode meeting
```

## Proposed Project Structure

```text
stt-cli/
├── Package.swift
├── README.md
├── LEARNINGS.md
├── RUST_CLI_INVESTIGATION.md
├── SWIFT_CLI_AUDIO_CAPTURE_DECISION.md
├── python/
│   └── stt_vibevoice/
│       ├── __init__.py
│       ├── transcribe.py
│       ├── chunking.py
│       └── status.py
└── Sources/
    └── stt/
        ├── main.swift
        ├── CLI/
        │   ├── Commands.swift
        │   └── Options.swift
        ├── Audio/
        │   ├── DeviceList.swift
        │   ├── MicRecorder.swift
        │   ├── SystemAudioRecorder.swift
        │   ├── MeetingRecorder.swift
        │   └── WAVWriter.swift
        ├── Permissions/
        │   └── AudioPermissions.swift
        ├── Transcription/
        │   └── PythonTranscriber.swift
        └── Util/
            ├── Paths.swift
            ├── ProcessRunner.swift
            └── SessionState.swift
```

## Current Implementation Status

As of this iteration, the Swift CLI has progressed through the low-risk foundation work and validation harnesses:

- CLI commands exist for `doctor`, `devices`, `record`, `transcribe`, `pipeline`, and `permissions`.
- Finite recording with `--duration` is implemented for repeatable smoke tests.
- Microphone recording works through the bundled app and produces valid WAV output in `scripts/validate.sh`.
- A local `.app` wrapper is built by `scripts/build-app-bundle.sh`, ad-hoc signed with microphone entitlement, and verified by `codesign`.
- The local Python backend module is packaged into `stt.app/Contents/Resources/python` and can be located when launched outside the repo.
- `stt doctor` reports bundle attribution, permission status, native CoreAudio process-tap symbol availability, and Python backend readiness with actionable setup hints.
- `stt transcribe` and `stt pipeline` support bounded backend calls (`--timeout` / `--transcribe-timeout`), explicit backend selection (`--python-backend`), model options, and `--require-backend-ready` preflight checks.
- `stt transcribe`, `stt pipeline`, and `stt mix` now fail fast for missing or empty/corrupt audio inputs where Swift can validate them deterministically before launching the Python backend.
- Pipeline metadata (`metadata.json`) is written for both failure and success paths and includes `transcribedAudioPath` so downstream tooling can identify the exact file sent to the backend.
- Meeting pipelines record separate `mic.wav`/`system.wav` tracks, transcribe `mixed.wav` when compatible mix-down succeeds, and fall back to `mic.wav` with a metadata note when mixing fails.
- `scripts/validate.sh` covers Swift tests, Python tests, app bundle build/sign, bundled backend lookup, strict readiness semantics, invalid-backend and missing-audio preflights, mic smoke recording, optional system-fallback validation, failure metadata, corrupt-WAV mix rejection, and fake-backend transcribe/pipeline success/failure/timeout smoke tests.
- `STT_SKIP_MIC_HARDWARE=1 ./scripts/validate.sh` provides a hardware-free CI-safe validation path, and `.github/workflows/ci.yml` runs it on macOS after bootstrapping the Python backend without MLX.
- `scripts/manual-tcc-smoke.sh` supports opt-in TCC reset and optional routed system fallback smoke testing.

Still incomplete:

- Native CoreAudio process-tap system-output capture is probed but not wired.
- Fresh TCC prompt attribution still requires a user-visible manual reset run: `STT_RESET_TCC=1 ./scripts/manual-tcc-smoke.sh`.
- Full real transcription requires installing MLX/mlx-audio dependencies, e.g. `./scripts/bootstrap-python-backend.sh --mlx --check`.
- BlackHole/Aggregate-device system fallback requires real routed audio and should be validated with `STT_SYSTEM_DEVICE="BlackHole 2ch" ./scripts/validate.sh`.
- Developer ID signing, notarization, and distribution packaging are not implemented.

## Implementation Plan

### Phase 1: Swift CLI skeleton + TCC attribution spike (riskiest-first)

Implement basic command structure using `swift-argument-parser`, and **de-risk the hardest unknown first**: how macOS TCC attributes permission when the tool runs.

The key concern: a bare CLI run from Terminal causes TCC (mic / screen-capture) prompts to be attributed to **Terminal.app**, not to `stt`. Granting the prompt then hands the permission to *every* process launched from that terminal — a real security problem and confusing UX. This must be resolved before building capture on top of it, so it is the first spike, not a Phase 4 afterthought.

Commands:

```bash
stt doctor
stt devices
```

Goals:

- Confirm Swift Package Manager setup
- Confirm CLI installation/running workflow
- Add structured command layout
- Add basic environment checks
- **TCC spike:** verify whether a bare binary attributes mic permission to Terminal; test whether wrapping `stt` in a minimal `.app` bundle (or a bundled helper) makes TCC attribute the prompt to `stt` instead. Lock in whichever packaging shape gives correct attribution before Phase 2.

### Phase 2: Microphone recording

Implement:

```bash
stt record --mode mic --output test.wav
```

Goals:

- Capture microphone input
- Write valid WAV output
- Handle Ctrl-C cleanly
- Print output path and recording duration

### Phase 3: Explicit input device support

Implement:

```bash
stt devices
stt record --input-device "BlackHole 2ch" --output system.wav
```

Goals:

- List available audio input devices
- Allow selecting an input device by name or ID
- Support BlackHole/Aggregate Device as an immediate fallback for system audio

This phase gives a working non-Electron system-audio path before native taps are complete.

### Phase 4: Native system audio capture

Implement:

```bash
stt record --mode system --output system.wav
```

Candidate APIs (only these two can capture system *output* audio; AVFoundation **cannot** and is intentionally excluded here — it is used only for the microphone in earlier phases):

- **CoreAudio CATap** (Core Audio taps API, macOS 14.4+)
  - Audio-centric API, better conceptual fit for "capture system audio".
  - TCC requirement: audio-capture / microphone-class permission (lighter prompt).
  - Preferred primary path.
- **ScreenCaptureKit**
  - Can capture system audio, but drags in a **Screen Recording** TCC permission (heavier, scarier prompt for users).
  - Fallback if CATap proves insufficient on the targeted macOS versions.

The differing TCC requirements (audio-capture vs. screen-recording) should drive which API is chosen: prefer CATap to avoid the heavier Screen Recording permission.

Goals:

- Capture system/output audio without Electron
- Handle permission prompts or permission errors clearly
- Write WAV/chunk output reliably

### Phase 5: Meeting mode

Implement:

```bash
stt record --mode meeting --output meeting.wav
```

Goals:

- Capture microphone and system audio together
- Decide whether to mix into one stream or preserve separate channels
- Prefer separate channels if diarization/speaker separation may matter later

Possible output modes:

```bash
stt record --mode meeting --output meeting.wav
stt record --mode meeting --output-dir session/ --separate-tracks
```

### Phase 6: Transcription pipeline

Implement:

```bash
stt transcribe meeting.wav
stt pipeline --mode meeting
```

Goals:

- Normalize audio if needed
- Call Python/MLX transcription backend
- Save transcript text and structured metadata
- Keep recording and transcription stages independently debuggable

## Permission and Packaging Considerations

The hardest non-audio part will likely be macOS permissions.

Resolved in the Phase 1 spike (see above):

- A bare CLI run from Terminal attributes TCC prompts to **Terminal.app**, not `stt`. The tool must be wrapped in an `.app` bundle (or a bundled helper) so permissions attribute correctly. This is locked in during the Phase 1 spike before any capture work.

Still to resolve per API:

- Which entitlements are needed for the selected capture API (CATap vs. ScreenCaptureKit differ — see Phase 4).
- How should the CLI explain permission denial with actionable recovery steps?
- How should users reset permissions if they previously denied access?

Recommended commands:

```bash
stt doctor audio
stt permissions
stt permissions reset-help
```

These should provide actionable guidance rather than generic failures.

## Distribution & Notarization

For anything beyond personal use, the binary must be properly signed and notarized; ad-hoc signing is not enough for capture entitlements.

- **Hardened Runtime** is required for notarization and for mic/audio-capture entitlements to behave correctly.
- **Developer ID Application** signing identity for distribution outside the App Store.
- **Notarization** via `notarytool`, with the hardened runtime and a stapled ticket.
- **`.app` bundle** (see Phase 1 spike) — both for correct TCC attribution and so notarization/stapling works as expected.
- **Distribution channels:** Homebrew tap/formula, or a notarized zip/`pkg` for direct download. A Homebrew cask may be appropriate if an `.app` bundle is required.
- Document the install path so the macOS Gatekeeper prompt resolves cleanly on first run.

## Testing & CI

TCC permissions (microphone, screen recording, audio capture) **cannot be granted headlessly**, so a standard headless CI runner cannot truly exercise the capture path.

Approach:

- **Unit tests** (audio parsing, WAV writing, CLI arg parsing, transcription orchestration) run in normal headless CI.
- **Capture smoke tests** require either:
  - a **self-hosted Mac runner** with TCC permissions pre-granted to the test bundle, or
  - a **manual smoke-test checklist** run before each release (mic record → system record → meeting record → transcribe).
- Treat the permission-gated flows as a manual release gate, not an automated CI gate, until a self-hosted runner exists.

## Risks

Main risks:

1. Native system-audio APIs may require more entitlement/signing work than expected.
2. TCC attribution for a bare CLI (mitigated by the Phase 1 `.app`-bundle spike, but worth verifying on the targeted macOS versions).
3. Different recent macOS versions may behave differently.
4. Capturing system audio and mic together may require careful clock/sync handling.
5. Separate-channel output may complicate transcription, but is likely worth preserving.

## Fallback Strategy

If native system audio takes longer than expected, support this interim path:

```bash
stt record --input-device "BlackHole 2ch"
stt record --input-device "Aggregate Device"
```

This keeps the app useful while native capture matures.

## Final Decision

For a macOS-only CLI targeting latest macOS versions, the preferred approach is:

> Build the whole CLI in Swift, avoid Electron, and use native Apple audio APIs for system audio capture.

This is the best match for the product constraints and should reduce implementation friction compared with Rust while keeping the tool lighter and more native than an Electron-based helper.
