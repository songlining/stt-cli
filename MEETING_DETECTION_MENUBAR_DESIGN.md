# Design: Meeting Detection (`stt watch`) and Menu Bar Controller (`sttBar`)

Status: Draft
Date: 2026-02-08

## Summary

Add two capabilities to stt-cli:

1. **Meeting detection** — watch the macOS CoreAudio HAL for processes actively using the microphone, and detect when a conferencing app (Teams, Zoom, Slack huddle, FaceTime, Webex) joins a call. Exposed headlessly as `stt watch`.
2. **A menu bar agent app (`sttBar`)** — an `NSStatusItem` (no Dock icon) that shows detection/recording state and lets the user start/stop recording and transcription without a terminal, optionally auto-recording when a meeting is detected.

This mirrors how Notion's AI Meeting Notes detects meetings: the desktop app observes *which processes are using the microphone* at the OS level (it does not listen to audio), then prompts the user to start transcription. No Teams/Zoom API integration is required; detection is app-agnostic.

## Goals

- Detect "a conferencing app is now using the mic" within ~5 seconds, with low false-positive rate.
- Zero additional permissions for detection itself (observing HAL process state is not audio capture).
- Reuse the existing meeting pipeline (`MeetingRecorder`, process-tap system audio, separate mic/system transcription) unchanged.
- Menu bar control of the full loop: idle → meeting detected → recording → transcribing → done.
- Both CLI and menu bar app share one TCC identity (one permission grant covers both).
- Keep the headless path (`stt watch`) fully scriptable and CI-testable without any GUI.

## Non-goals

- Auto-joining meetings as a bot (Otter/Fireflies style). We only capture local audio.
- Auto-starting recording without user consent by default (`--auto-record` is opt-in).
- Browser-meeting disambiguation beyond heuristics (Chrome helper processes are indistinguishable; see Risks).
- Calendar integration (EventKit correlation) — noted as a future enhancement only.
- Distribution signing/notarization (unchanged from current project state).

## Background: the detection mechanism

macOS 12+ exposes every process connected to the CoreAudio HAL as a **process object**:

- `kAudioHardwarePropertyProcessObjectList` on `kAudioObjectSystemObject` → array of `AudioObjectID`.
- Per process object:
  - `kAudioProcessPropertyPID`
  - `kAudioProcessPropertyBundleID`
  - `kAudioProcessPropertyIsRunningInput` — 1 while the process has at least one active input stream (i.e. using the mic)
  - `kAudioProcessPropertyIsRunningOutput` — 1 while playing audio

This is the same state that drives the orange microphone indicator in the macOS menu bar. Querying it requires **no microphone permission** — it is metadata, not audio capture.

Verified on the development machine (Apple Silicon, macOS current): a prototype polled the process list while `stt record` captured the mic; the recorder's PID flipped `IsRunningInput` to 1 for the duration of the capture and back to 0 on stop. Teams' helper processes (`com.microsoft.teams2.*`) are visible with bundle IDs; bare CLI binaries report no bundle ID (PID-only matching).

## Architecture

One app bundle, one TCC identity, two executables, one shared library:

```
stt.app  (bundle id com.larrysong.stt — unchanged)
  Contents/MacOS/
    stt      existing CLI; gains `watch` subcommand
    sttBar   NEW: AppKit agent app (LSUIElement, menu bar only)
             ^ both link sttCore

sttCore (existing library target, Sources/stt/)
  Detection/   NEW — CoreAudioProcessList, MicActivityMonitor, MeetingDetector
  Audio/       existing — MeetingRecorder, NativeTapLifecycle, ...
  Transcription/, Permissions/, Util/   existing
```

Decisions:

- **In-process, no IPC.** `sttBar` links `sttCore` and calls `MeetingRecorder` directly. Spawning the CLI as a subprocess would split TCC attribution between two processes and force IPC for recording state. XPC is unnecessary complexity for a single-user local tool.
- **Detection code lives in `sttCore`, not `sttBar`.** Both the CLI `watch` command and the menu bar app consume it; putting it in the AppKit target would make the headless path impossible.
- **Same bundle ID for both executables.** Microphone and system-audio permissions granted once cover the CLI and the menu bar app. `CFBundleExecutable` becomes `sttBar` so double-clicking the app launches the agent; the CLI stays at `stt.app/Contents/MacOS/stt`.
- **`LSUIElement=YES` is safe on the shared bundle.** It only affects processes that connect to the window server; the CLI never does, so terminal behavior is unchanged.

## Components

### `CoreAudioProcessList` (sttCore/Detection)

Thin wrapper over `AudioObjectGetPropertyData` returning a snapshot:

```swift
struct AudioProcessInfo: Equatable {
    let pid: pid_t
    let bundleID: String?
    let isRunningInput: Bool
    let isRunningOutput: Bool
}
```

HAL access goes through a mockable operations struct (same pattern as `NativeTapCoreAudioOperations`) so detection logic is unit-testable without real hardware.

### `MicActivityMonitor` (sttCore/Detection)

- `DispatchSourceTimer` polling every 1.5s.
- Filters: drop `pid <= 1`, drop invalid object IDs, drop Apple daemons unless allowlisted (`com.apple.FaceTime` is the only allowlisted `com.apple.*`).
- Emits `[AudioProcessInfo]` snapshots to subscribers.

Conferencing allowlist (initial):

```
com.microsoft.teams2          us.zoom.xos
com.tinyspeck.slackmacgap     com.apple.FaceTime
com.cisco.webexmeetingsapp
```

### `MeetingDetector` (sttCore/Detection)

State machine over monitor snapshots:

```
idle ──3 consecutive positive samples──▶ meetingDetected(process)
meetingDetected ──2 consecutive negative samples──▶ idle
```

- **Positive sample** = at least one allowlisted process with `isRunningInput == true`.
- **"In a call" heuristic**: a process with *both* input and output active scores as a stronger candidate than input-only (input-only also matches dictation, Voice Memos, etc.). When multiple candidates exist, prefer input+output.
- Debounce at 3×1.5s ⇒ detection within ~4.5s of joining; 2 negative samples to reset, tolerating transient dropouts (mute does not close the audio stream, so mute is not a false negative).
- Pure logic — fully unit-tested with synthetic snapshot sequences.

### `stt watch` (CLI)

New subcommand in `Sources/stt/CLI/Commands.swift`:

```
stt watch [--json] [--auto-record] [--allowlist <bundleID,…>]
```

- Emits JSON lines on transitions: `{"event":"meeting_detected","bundleID":"us.zoom.xos","pid":1234,"input":true,"output":true}` / `{"event":"meeting_ended"}`.
- Without `--auto-record` it is purely observational (no permissions needed).
- With `--auto-record`, on `meeting_detected` it starts the existing meeting pipeline; on `meeting_ended` (or SIGINT) it stops and transcribes — reusing `Pipeline` logic, not duplicating it.

### `sttBar` (menu bar app)

New executable target `Sources/sttBar/` linking `sttCore`:

| File | Responsibility |
|---|---|
| `main.swift` / `AppDelegate.swift` | NSApplication run loop; owns monitor, detector, controllers |
| `StatusBarController.swift` | `NSStatusItem`, icon + menu per state |
| `RecordingController.swift` | Detector events → `MeetingRecorder` → transcription; `UserDefaults` for prefs |

#### Icon states

| State | Icon | Notes |
|---|---|---|
| Monitoring (idle) | `waveform` | Template image, adapts to light/dark menu bar |
| Meeting detected | `waveform` + badge or `person.wave.2` | Draws attention without requiring a notification |
| Recording | `record.circle` in red | Non-template image (templates can't tint red) — custom-drawn dot overlay |
| Transcribing | `ellipsis.circle` | Subtle pulse via alternating frames |
| Error | `exclamationmark.triangle` | Until dismissed |
| Monitoring paused | `waveform.slash` | If the user disables watching |

#### Menus per state

Idle / monitoring (default):

```
  Monitoring for meetings              ← disabled status line
  ─────────────────────────────
  Start Recording Now                  ← manual mode: in-person meetings,
                                         browser calls, anything not detected
  ─────────────────────────────
✓ Auto-record when meeting detected
✓ Notify when meeting detected
  ─────────────────────────────
  Recent Meetings            ▸         ← submenu built from run-dir metadata.json:
    Teams — 10:32 (transcribed)            click → reveal transcript in Finder
    Zoom — Yesterday
  Open Output Folder
  ─────────────────────────────
  Launch at Login
  Quit stt
```

Meeting detected:

```
  Meeting detected: Microsoft Teams    ← bundle ID mapped to friendly name
  ─────────────────────────────
▶ Start Recording                      ← emphasized; one click to go
  Dismiss
```

Recording:

```
● Recording — 12:34                    ← live elapsed timer, 1s refresh
  Meeting: Microsoft Teams
  ─────────────────────────────
■ Stop Recording                       ← transitions to transcribing
```

Transcribing:

```
  Transcribing… (system track)
  ─────────────────────────────
  Cancel Transcription                 ← kills the Python backend process
```

#### Interaction decisions

- **Consent gate on auto-record.** The first time "Auto-record when meeting detected" is enabled, show a one-time alert: stt will start recording automatically when a conferencing app uses the mic, plays no consent announcement, and the user is responsible for informing participants. Persist the acknowledgment in `UserDefaults`; never record without either this toggle or an explicit menu click.
- **"Start Recording Now" covers the detection gaps** (browser-based Teams/Meet, non-allowlisted apps). This is the supported answer to the Chrome-helper ambiguity, not allowlist tuning.
- **Recent Meetings submenu** is derived from existing run-directory `metadata.json` files — no new state store. Entries show app name, date, and status.
- **Launch at Login** via `SMAppService` (macOS 13+ API; no helper app needed).
- **Permission failures become a menu state**, not a crash: if mic/system-audio permission is missing at record time, the status line shows "Permission needed — Open Settings", deep-linking to the correct System Settings pane (reusing the existing `permissions` diagnostics).
- **Completion feedback**: on transcription finish the icon returns to `waveform` and, if "Notify when meeting detected" is on, a notification ("Transcript ready — Teams, 32 min") opens the transcript on click.
- **Single-instance guard**: lock file under `~/Library/Application Support/stt/`; second launch exits.

Deliberately excluded (for now):

- **Pause/resume** — the pipeline doesn't support it; Stop + Start creates a new run dir.
- **Global hotkey** — ruled out; would add an accessibility-permission dependency.
- **Live transcript preview** — transcription is post-hoc in the current backend.
- **Preferences window** — everything fits in checkable menu items until the item count outgrows the menu.

## Packaging and signing

`scripts/build-app-bundle.sh` changes:

1. `swift build` both `stt` and `sttBar`; copy both into `Contents/MacOS/`.
2. `Resources/stt-app/Info.plist`: add `LSUIElement=true`; set `CFBundleExecutable` to `sttBar`.
3. Entitlements unchanged (`com.apple.security.device.audio-input`); one codesign pass covers both binaries.
4. `check-app-bundle.sh` and `validate.sh` assert both binaries exist and LSUIElement is set.

## Testing strategy

- **Unit (sttTests)**: `CoreAudioProcessList` via mock ops; `MeetingDetector` transitions including debounce edges, input-only vs input+output preference, allowlist filtering, daemon filtering.
- **Unit checks (sttUnitChecks)**: a live HAL snapshot check that prints current process objects (smoke-level, hardware-dependent).
- **Headless e2e**: `stt watch` running while `stt record` grabs the mic — already proven manually with the prototype; scriptable as a bounded test.
- **Manual**: menu bar app launch (no Dock icon), state transitions, full Teams/Zoom meeting with auto-record, TCC behavior after rebuild (ad-hoc re-signing may re-prompt — known limitation).

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Browser meetings (Teams/Meet in Chrome) appear only as `com.google.Chrome(.helper)` — cannot distinguish a call from a video playing | Input+output heuristic; leave Chrome out of the default allowlist; document limitation; future: EventKit calendar correlation for meeting links |
| Ad-hoc rebuilds change code hash → TCC re-prompts | Documented (DISTRIBUTION.md); same as today; Developer ID later |
| Brief `IsRunningInput` blips during device enumeration | 3-sample debounce |
| Process objects exist even when idle (Teams helpers are always connected to the HAL) | Detection keys off `IsRunningInput`, not process presence |
| Long recordings block the UI | Recording/transcription on background queues; only menu state touches the main queue |
| Two `sttBar` instances racing | Single-instance lock |

## Implementation phases

1. `CoreAudioProcessList` + `MicActivityMonitor` in sttCore — unit tests (HAL path already validated live).
2. `MeetingDetector` state machine — unit tests.
3. `stt watch` CLI command — headless, JSON, `--auto-record`; e2e checkpoint.
4. `sttBar` skeleton — builds, shows in menu bar, no Dock icon.
5. Wire detector → status bar; manual start/stop recording.
6. `RecordingController` auto-record + prefs + open-output-folder.
7. Build script, bundle checks, `validate.sh` green; manual Teams/Zoom verification.

## Open questions

- Should `stt watch --auto-record` prompt once per meeting in the terminal (consent affordance) or is the flag itself sufficient consent? Current leaning: flag is consent for personal use; document it.
- ~~Notification style for "meeting detected" when auto-record is off~~ — resolved: a "Notify when meeting detected" menu toggle (default on); icon state change always happens regardless.
- Keep `CFBundleExecutable = sttBar`, or ship the agent as a separate `sttBar.app`? Single bundle is simpler for TCC; separate apps are cleaner for distribution later. Decision deferred to Phase 7.
