# Speaker Enrollment and Identification Plan

## Goal

Add local speaker enrollment and speaker identification to `stt-cli` so diarized speakers from later recordings can be mapped to known people, while keeping all storage and artifact export paths configurable.

Core rule: **no Obsidian-specific or Larry-specific path logic in `stt-cli` core**. Obsidian filing can be achieved by user config and hook scripts, but core code treats every export target as a generic directory.

## Requirements

1. Speaker profile storage is configurable.
2. Meeting/transcript artifact export is configurable.
3. The implementation is split into subagent-sized slices with explicit interfaces.
4. Voice samples and embeddings are treated as sensitive local data.
5. Speaker identity labels are confidence-based. Low-confidence matches stay anonymous.

## Current repo context

- Swift CLI: `Sources/stt/CLI/Commands.swift`, using `swift-argument-parser`.
- Swift path helpers: `Sources/stt/Util/Paths.swift`, already supports `STT_HOME`.
- Swift session metadata: `Sources/stt/Util/SessionState.swift`.
- Swift to Python bridge: `Sources/stt/Transcription/PythonTranscriber.swift`.
- Python backend: `python/stt_vibevoice`.
- Python backend convention: lazy-import heavy ML dependencies so help/status/tests do not require MLX or optional voice-ID packages.
- Tests use Swift Testing and `sttUnitChecks`, not XCTest-only patterns.

## Vocabulary

- **Diarization**: split a recording into anonymous speakers, e.g. `Speaker 0`, `Speaker 1`.
- **Speaker enrollment**: save a known speaker profile from a clean sample.
- **Speaker embedding**: numeric voice vector.
- **Speaker identification**: compare a new diarized speaker embedding with enrolled profiles and relabel if confidence is high enough.

## Config strategy

### Discovery order

`STTConfig.load(...)` resolves config in this order:

1. explicit CLI `--config <path>` on path-using commands
2. `STT_CONFIG=/path/to/config.json`
3. `~/.config/stt/config.json`
4. built-in defaults

Missing config is non-fatal. Malformed config fails with a clear validation error.

### Commands that must support config

Every command that reads/writes speaker profiles or exports artifacts must accept `--config`:

- `stt speaker enroll`
- `stt speaker list`
- `stt speaker rename`
- `stt speaker remove`
- `stt identify`
- `stt transcribe --identify-speakers`
- `stt pipeline --identify-speakers`
- `stt pipeline` artifact export path

Implementation note: do not rely on a top-level global option unless it is proven to work reliably with `swift-argument-parser` subcommands. Repeating `--config` on path-using commands is acceptable and explicit.

### Config schema

```json
{
  "speakerProfilesDir": "/Users/me/Documents/stt-speakers",
  "speakerIdentification": {
    "enabled": true,
    "provider": "speechbrain",
    "matchThreshold": 0.78,
    "matchMargin": 0.05,
    "minimumSpeechSeconds": 8.0
  },
  "artifactExport": {
    "enabled": true,
    "targetDir": "/Users/me/Documents/Meetings",
    "includeAudio": false,
    "overwrite": false,
    "postPipelineCommand": {
      "executable": "/Users/me/bin/file-meeting-note",
      "arguments": ["--transcript", "{transcriptPath}", "--target", "{targetDir}"]
    }
  }
}
```

Validation:

- `matchThreshold` must be `0...1`.
- `matchMargin` must be `>= 0`.
- `minimumSpeechSeconds` must be `> 0`.
- Empty strings for paths are invalid.
- `postPipelineCommand` must be structured as executable plus arguments, not a raw shell string.

Defaults:

- `speakerProfilesDir`: app-support path derived by `Paths.swift`, scoped by `STT_HOME` when set.
- `speakerIdentification.enabled`: false.
- `speakerIdentification.provider`: `speechbrain` for real use once installed. A lightweight `mfcc-test` provider may exist for tests and smoke checks, but should not be presented as high-accuracy identity matching.
- `matchThreshold`: provider-specific default, initially `0.78` for `speechbrain`, separate calibration doc required.
- `minimumSpeechSeconds`: 8.0.
- `artifactExport.enabled`: false.
- `artifactExport.includeAudio`: false.
- `artifactExport.overwrite`: false.
- `artifactExport.hookTimeoutSeconds`: 30.

If `artifactExport.enabled` is true, `targetDir` is required.

## Privacy and storage rules

Speaker samples and embeddings are biometric-like data.

Rules:

1. Store locally by default.
2. Never export speaker profiles unless the user explicitly points `speakerProfilesDir` or artifact export there.
3. Prefer copying enrollment samples into the configured profile directory instead of retaining arbitrary absolute source paths.
4. Store original source path only if explicitly enabled later. Initial implementation should avoid it.
5. `stt speaker remove` deletes profile JSON and copied samples for that profile.
6. Docs must clearly explain how to delete speaker data manually by removing the configured profile directory.

## Data model contracts

### Speaker profile

One JSON file per profile:

```json
{
  "id": "immutable-uuid",
  "displayName": "Larry Song",
  "createdAt": "2026-07-06T12:00:00Z",
  "updatedAt": "2026-07-06T12:00:00Z",
  "embeddingProvider": "speechbrain",
  "embeddingModel": "speechbrain/spkrec-ecapa-voxceleb",
  "embedding": [0.01, -0.03, 0.42],
  "samplePaths": ["samples/immutable-uuid/20260706-120000.wav"],
  "sampleDurationSeconds": 15.0,
  "notes": null
}
```

Important:

- `id` is immutable UUID, not display name or slug.
- `displayName` is mutable.
- `samplePaths` are relative to `speakerProfilesDir`.
- Provider/model must match before comparing embeddings. If mismatched, skip profile and report why.

Profile directory layout:

```text
<speakerProfilesDir>/
  profiles/
    index.json
    <profile-id>.json
  samples/
    <profile-id>/
      20260706-120000.wav
```

`profiles/index.json` schema:

```json
{
  "profiles": [
    {
      "id": "immutable-uuid",
      "displayName": "Larry Song",
      "embeddingProvider": "speechbrain",
      "embeddingModel": "speechbrain/spkrec-ecapa-voxceleb",
      "updatedAt": "2026-07-06T12:00:00Z",
      "sampleCount": 1
    }
  ]
}
```

### Flattened match input contract

Swift owns profile storage. Python matcher receives a temporary flattened JSON payload from Swift:

```json
{
  "profiles": [
    {
      "id": "immutable-uuid",
      "displayName": "Larry Song",
      "embeddingProvider": "speechbrain",
      "embeddingModel": "speechbrain/spkrec-ecapa-voxceleb",
      "embedding": [0.01, -0.03, 0.42]
    }
  ]
}
```

This avoids Python needing to understand the profile directory/index layout.

### Speaker identity output

Transcript/session JSON may include:

```json
{
  "speakerLabels": {
    "0": {
      "displayName": "Larry Song",
      "profileId": "immutable-uuid",
      "confidence": 0.91,
      "margin": 0.12,
      "matchStatus": "matched"
    },
    "1": {
      "displayName": "Speaker 1",
      "profileId": null,
      "confidence": 0.44,
      "margin": 0.01,
      "matchStatus": "unknown"
    }
  }
}
```

Conflict policy:

- No profiles: keep all speakers anonymous, report `no_profiles`.
- No diarized `speaker_id`: skip identification, report `no_diarization`.
- Speaker total speech below `minimumSpeechSeconds`: keep anonymous, report `too_short`.
- Best score below threshold: keep anonymous, report `below_threshold`.
- Best score above threshold but margin below `matchMargin`: keep anonymous, report `ambiguous`.
- Two diarized speakers match the same profile: assign the profile only to the highest-confidence speaker if the margin is safe; keep the others anonymous with `duplicate_profile_match`.
- Provider/model mismatch: skip that profile for the candidate.

## Per-speaker audio extraction contract

The relabeling feature needs one embedding per diarized speaker cluster.

Responsibility: Python `speaker_id.py` extracts per-speaker embeddings directly from the original recording plus transcript JSON.

CLI contract:

```bash
python -m stt_vibevoice.speaker_id extract \
  --audio recording.wav \
  --segments transcript.json \
  --speaker-id 0 \
  --provider speechbrain \
  --minimum-speech-seconds 8.0 \
  --json speaker-0.embedding.json
```

Behavior:

1. Read transcript JSON segments with `speaker_id`, start, and end times.
2. Select segments matching `--speaker-id`.
3. Ignore segments shorter than a small minimum, e.g. `0.5s`.
4. Concatenate selected audio into a temporary in-memory or temp-file WAV.
5. If total selected speech is below the provided `--minimum-speech-seconds` value, return structured JSON with status `too_short` and a non-zero exit code. Swift maps this to `matchStatus: too_short`. Swift passes this value from `STTConfig.speakerIdentification.minimumSpeechSeconds`.
6. Extract embedding from the concatenated speaker-only audio.
7. Return JSON:

```json
{
  "speakerId": "0",
  "provider": "speechbrain",
  "model": "speechbrain/spkrec-ecapa-voxceleb",
  "embedding": [0.1, 0.2],
  "durationSeconds": 12.3,
  "segmentCount": 6
}
```

Enrollment extraction from a known clean sample uses the simpler contract:

```bash
python -m stt_vibevoice.speaker_id extract --audio larry.wav --provider speechbrain --minimum-speech-seconds 8.0 --json embedding.json
```

Shared helper requirement:

- Refactor current private WAV slicing logic from `python/stt_vibevoice/transcribe.py` into a reusable helper module before implementing segment extraction.
- Do not duplicate WAV slicing logic in Swift and Python.

## Python matching contract

```bash
python -m stt_vibevoice.speaker_id match \
  --candidate speaker-0.embedding.json \
  --profiles flattened-profiles.json \
  --threshold 0.78 \
  --margin 0.05 \
  --json speaker-0.match.json
```

Output:

```json
{
  "bestMatch": {
    "profileId": "immutable-uuid",
    "displayName": "Larry Song",
    "confidence": 0.91,
    "margin": 0.12,
    "matched": true,
    "status": "matched"
  },
  "candidates": [
    {"profileId": "immutable-uuid", "displayName": "Larry Song", "confidence": 0.91}
  ],
  "skippedProfiles": [
    {"profileId": "other-uuid", "displayName": "Other", "reason": "provider_model_mismatch"}
  ],
  "warnings": []
}
```

Python may calculate cosine similarity, but Swift owns final transcript conflict resolution across all speakers.

## Product UX

```bash
# Enroll from an existing sample
stt speaker enroll "Larry Song" --audio /path/to/larry.wav --provider speechbrain

# Enroll by recording a short mic sample
stt speaker enroll "Larry Song" --duration 15 --provider speechbrain

# List profiles
stt speaker list

# Rename/remove profiles
stt speaker rename "Larry" "Larry Song"
stt speaker remove "Larry Song" --yes

# Identify a single clip
stt identify /tmp/clip.wav --provider speechbrain --json /tmp/clip-id.json

# Transcribe and identify diarized speakers
stt transcribe meeting.wav --identify-speakers --speaker-provider speechbrain
stt pipeline --mode meeting --identify-speakers --duration 600
```

Enrollment behavior:

- `--audio` uses an existing file.
- `--duration` records a mic sample using existing recording machinery.
- Exactly one of `--audio` or `--duration` is required for initial implementation.
- Existing name without `--replace` fails with a helpful error.
- `--replace` replaces the embedding and sample set for that speaker.
- Future `--add-sample` can average multiple embeddings. Do not silently drift embeddings in v1.
- Copy the sample into `speakerProfilesDir/samples/<id>/...`.

## Implementation phases

### Phase 1: Config and path foundation

Files:

- Add `Sources/stt/Util/STTConfig.swift`
- Update `Sources/stt/Util/Paths.swift`
- Add `Tests/sttTests/STTConfigTests.swift`

Scope:

- Define `STTConfig`, `SpeakerIdentificationConfig`, `ArtifactExportConfig`, and `PostPipelineCommand`.
- Implement `STTConfig.load(explicitPath:environment:fileManager:)`.
- Implement validation rules above.
- Add `Paths.speakerProfilesDirectory(config:environment:)`.
- Add test hooks so tests can use temp HOME/config directories without touching the real machine.
- Do not wire CLI commands yet except as needed for compilation.

Tests:

- defaults when no config exists
- `STT_CONFIG` override
- explicit path beats env/default
- malformed JSON error
- invalid threshold/margin/path errors
- speaker profile directory respects config
- fallback speaker profile directory respects `STT_HOME`

### Phase 2: Speaker profile storage

Files:

- Add `Sources/stt/Util/SpeakerProfile.swift`
- Add `Sources/stt/Util/SpeakerProfileStore.swift`
- Add `Tests/sttTests/SpeakerProfileStoreTests.swift`

Scope:

- Implement profile/index models from this plan exactly.
- Implement CRUD and `findByName(_:)`.
- Copy enrollment samples into the profile dir only when called by Phase 5. Store currently only exposes paths/metadata helpers.
- Duplicate display names are allowed only if lookup by name is not used, but `findByName` must error when ambiguous.

Tests:

- save/load round trip
- index consistency after save/delete/rename
- duplicate-name ambiguity
- relative sample paths
- deletion removes profile JSON and samples for that profile

### Phase 3: Python speaker backend

Files:

- Add `python/stt_vibevoice/speaker_id.py`
- Add/refactor reusable WAV slicing helper from `transcribe.py`
- Update `python/stt_vibevoice/status.py`
- Update `python/pyproject.toml`
- Add `python/tests/test_speaker_id.py`

Scope:

- Implement `extract` for whole audio.
- Implement `extract` for `--segments + --speaker-id`.
- Implement `match` using flattened profiles input.
- Add provider framework:
  - `mfcc-test`: lightweight deterministic provider for tests and smoke checks, not marketed as accurate identity matching.
  - `speechbrain`: optional real provider behind extra, if installed.
- Lazy-import optional dependencies.

Tests:

- cosine similarity
- threshold and margin behavior
- provider/model mismatch filtering
- segment selection and duration checks using synthetic WAV/transcript JSON
- no real ML dependency in default Python tests

### Phase 4: Swift to Python bridge

Files:

- Add `Sources/stt/Transcription/PythonSpeakerIdentifier.swift`
- Add `Tests/sttTests/PythonSpeakerIdentifierTests.swift`

Scope:

- Mirror `PythonTranscriber.swift` process patterns.
- Reuse backend discovery and preferred `.venv` behavior.
- Parse extraction and match JSON into Swift structs.
- Use fake Python backend stubs in tests.

Tests:

- whole-audio extraction command
- segment extraction command
- match command
- JSON parsing
- backend failure messaging

### Phase 5: CLI commands and transcript relabeling

Files:

- Update `Sources/stt/CLI/Commands.swift`
- Add `Sources/stt/Util/SpeakerLabelResolver.swift`
- Update `Sources/stt/Util/SessionState.swift`
- Update `Tests/sttTests/CLIParsingTests.swift`
- Add `Tests/sttTests/SpeakerIdentificationPipelineTests.swift`

Scope:

- Add `speaker` subcommand group.
- Add `identify` command.
- Add `--identify-speakers`, `--speaker-provider`, `--speaker-threshold`, `--speaker-margin`, `--profiles-dir`, and `--config` where needed.
- Implement enrollment behavior from UX section.
- Implement pure `SpeakerLabelResolver` conflict policy from this plan.
- Wire identification into `transcribe` and `pipeline` after transcript JSON exists.

Tests:

- command parsing
- enrollment argument validation
- no profiles
- no diarization
- short speaker duration
- high-confidence relabel
- ambiguous margin remains anonymous
- duplicate profile match resolution
- no Obsidian paths in outputs

### Phase 6: Generic artifact export

Files:

- Add `Sources/stt/Util/ArtifactExporter.swift`
- Wire into `Pipeline.run()` after state/transcript artifacts are written
- Add `Tests/sttTests/ArtifactExporterTests.swift`

Scope:

- If enabled and `targetDir` is set, copy deterministic artifact set:
  - transcript text if present
  - transcript JSON if present
  - session metadata JSON if present
  - audio only when `includeAudio` is true
- Naming:
  - default subdirectory: `<targetDir>/<safe-session-name>-<timestamp>/`
  - never overwrite unless `overwrite` is true
  - if collision and overwrite false, append numeric suffix
- Symlink handling:
  - do not follow symlinked target directories by default unless existing `FileManager` behavior is explicitly reviewed
  - never delete target contents
- Hook:
  - structured executable + args only
  - no shell interpolation
  - placeholders substituted per argument
  - closed stdin
  - captured stdout/stderr
  - default timeout 30 seconds, configurable with `hookTimeoutSeconds`
  - timeout or non-zero exit is non-fatal and recorded as warning

Tests:

- deterministic copy set
- creates target dir
- collision suffix
- placeholder substitution
- hook failure non-fatal
- hook timeout non-fatal

### Phase 7: Docs and validation

Files:

- Update `README.md`
- Update `LEARNINGS.md` if useful
- Update `scripts/validate.sh`

Scope:

- Document config schema.
- Document local storage and deletion.
- Document speaker enrollment and identification limitations.
- Add fake/lightweight validation path.
- Add optional real-provider smoke test instructions.

## Subagent breakdown

### Subagent A: Config foundation

Implement Phase 1 only.

Deliverables:

- `STTConfig.swift`
- `Paths.swift` profile directory resolver
- `STTConfigTests.swift`
- passing `swift test`

Do not touch ML, speaker CLI commands, artifact export, or docs beyond what is needed for compile/tests.

### Subagent B: Profile store

Implement Phase 2 only.

Depends on:

- `STTConfig`
- `Paths.speakerProfilesDirectory(config:environment:)`

Deliverables:

- `SpeakerProfile.swift`
- `SpeakerProfileStore.swift`
- tests

### Subagent C: Python speaker backend

Implement Phase 3 only.

Depends on:

- JSON contracts in this plan

Deliverables:

- `speaker_id.py`
- reusable WAV slicing helper
- Python tests

### Subagent D: Swift bridge

Implement Phase 4 only.

Depends on:

- Python CLI JSON contracts in this plan

Deliverables:

- `PythonSpeakerIdentifier.swift`
- tests with fake backend

### Subagent E: CLI integration

Implement Phase 5 only after A-D are merged or available.

Deliverables:

- `speaker` commands
- `identify`
- transcript/pipeline identification flags
- label resolver
- tests

### Subagent F: Artifact export

Implement Phase 6 only.

Depends on:

- `STTConfig.ArtifactExportConfig`

Deliverables:

- `ArtifactExporter.swift`
- tests

### Subagent G: Docs and validation

Implement Phase 7 after code lands.

Deliverables:

- README updates
- validation updates
- privacy docs

## Initial coding decision

Start with Subagent A only.

Reason: configuration and path resolution are the foundation for the user's most important requirement. Without this slice, later subagents may accidentally bake in local paths or Obsidian assumptions.
