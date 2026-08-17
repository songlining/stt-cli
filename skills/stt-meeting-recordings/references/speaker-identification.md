# Speaker diarisation, identification & safe enrollment (reference)

Loaded on demand from SKILL.md. Covers the full diarisation → name/enroll → identify workflow, the safe-enrollment guard commands (audit / purity-preview / suggest-labels / enroll / enroll-ranges / relabel), worked examples, generated artifacts, and speaker failure handling.

## Speaker diarisation & identification

The session's captured `attendees` whitelist (see "Meeting lookup (Outlook calendar)" — stored in `metadata.json` and `session.md` as **Attendees (expected speakers)**) is the reference set for who should be in the recording. Use it when interpreting diarisation output: identified speakers that match an invitee are expected; `Speaker N` clusters should be checked against the whitelist first, and names not on the list flagged rather than silently accepted. If a whitelisted attendee appears to be missing from the transcript or an unexpected voice appears, surface that to the user.

The default `recordings.py transcribe` flow already runs diarisation and identification automatically for meeting sessions — you do not need to do anything special. This section documents the underlying `stt` CLI commands for manual control, troubleshooting, or enrolling new speakers.

The full workflow is: **diarise** (cluster speakers) → **name/enroll** (interactively confirm and save speaker profiles) → **identify** (auto-label known speakers in future meetings). Diarisation and identification failures are non-fatal to transcription, but enrollment creates local profiles and sample audio, while relabeling modifies transcript artifacts. Describe those mutations before performing them.

### 1. Diarise — assign Speaker 0, Speaker 1, …

Add `--diarize` to `stt transcribe-meeting`. After each track is transcribed, an ECAPA-TDNN speaker embedding model clusters segments into distinct speakers. The mic track is diarised first (speakers 0…N), then the system track continues the numbering (speakers N+1…), guaranteeing unique speaker IDs across the merged transcript.

```bash
stt transcribe-meeting mic.wav system.wav \
  --output transcript.txt --json transcript.json \
  --diarize --diarize-python-backend runtime/
```

Key flags:
- `--diarize` — enable diarisation (ECAPA + clustering).
- `--diarize-python-backend runtime/` — the venv with speechbrain/torch (the ASR venv `python/.venv` is MLX-only and cannot run ECAPA).
- `--diarize-num-speakers N` — hint the exact number of speakers (skips auto-clustering).
- `--diarize-distance-threshold 0.40` — auto-clustering sensitivity (default 0.40; lower = more speakers). Tuned so intra-speaker ECAPA distance (~0.24) merges correctly while cross-speaker distance separates.

The merged transcript shows `Speaker 0`, `Speaker 1`, etc. Diarisation failures are non-fatal: they warn to stderr and the transcript is produced without speaker labels (never lose a 57-minute transcript to an ECAPA hiccup).

### 2. Name speakers — interactive play → name → enroll loop

Once a transcript is diarised, name each speaker by listening to a short clip:

```bash
stt name-speakers \
  --transcript transcript.json \
  --mic mic.wav --system system.wav \
  --python-backend runtime/
```

For each speaker (most speech first), the command:
1. Builds a playable clip from their **clearest speech segments** (energy-ranked, not just the first ones in chronological order) — non-speech tags like `[Silence]`, `[Environmental Sounds]` are automatically excluded so no time is wasted on dead air.
2. **Normalizes loudness** so quiet system-audio tracks (remote participants captured via the CoreAudio process tap are typically ~30 dB quieter than the mic) play as loud as the mic track.
3. Plays a ~12s preview via `afplay`.
4. Prompts: `[name] enroll as | [r] replay | [s] skip` (both the single letter and full word work: `s`/`skip`, `r`/`replay`).
5. If named → extracts a 192-dim ECAPA embedding from ALL their speech and enrolls a speaker profile.

Useful flags:
- `--preview-seconds N` — how long to play (default 12).
- `--sample-seconds N` — cap the stored reference clip to N seconds of speech (default 60; keeps profiles small — ~5MB instead of hundreds of MB). The embedding is always extracted from the speaker's full speech regardless.
- `--no-normalize` — disable loudness normalization of preview clips (on by default).
- `--no-enroll` — dry run: play and prompt but don't save profiles.

Profiles are stored locally at `~/Library/Application Support/stt/speakers/` (never uploaded). Manage them with `stt speaker list` and `stt speaker rename`. Removal is destructive: obtain explicit user authorization, then run `stt speaker remove "<name>" --yes`.

#### Agent-friendly one-speaker-at-a-time naming

When the user asks to name speakers one at a time — for example, "play one, wait for my name, then move to the next" — use the turn-based helper instead of running the blocking `stt name-speakers` loop directly. The helper exposes separate `list`, `preview`, and `enroll` actions so the agent can pause between speakers and wait for the user's reply.

```bash
# List unnamed diarised speakers, sorted by useful speech time
python3 scripts/name_one_speaker.py list \
  --session "<session>"

# Play one speaker's normalized preview clip only
python3 scripts/name_one_speaker.py preview \
  --session "<session>" --speaker-id <id>

# After the user gives the name, follow the safe-enrollment workflow below.
# Only after a fresh audit, production-provider suggestions, purity preview,
# and human confirmation should guarded whole-cluster enrollment run:
python3 scripts/name_one_speaker.py enroll \
  --session "<session>" --speaker-id <id> --name "<display name>"
```

Important behavior:
- `preview` uses the same `stt_vibevoice.speaker_id concatenate` backend as `stt name-speakers`: clearest segments first, bracket-only non-speech tags skipped, loudness normalization on by default.
- `enroll` creates a one-speaker filtered transcript and delegates back to `stt name-speakers`, so the embedding is still extracted from **all** speech for that speaker while the stored reference clip is capped by `--sample-seconds` (default 60s).
- A normal preview is not a purity check. Before calling `enroll`, complete the **Safe speaker enrollment** sequence below, including `audit --force`, `suggest-labels --provider speechbrain --force`, `purity-preview`, and explicit human confirmation.
- After each preview, stop and ask the user for the speaker's name; do not proceed until enrollment succeeds, the user says skip, or the user asks to stop.
- If enrollment reports that the display name already exists, do not overwrite or create a duplicate. Ask whether to relabel the cluster to the existing profile, use a different name, or explicitly manage the existing profile.

### 3. Identify — auto-label known speakers in future meetings

Once profiles exist, future transcriptions can auto-label speakers:

```bash
stt transcribe-meeting mic.wav system.wav \
  --output transcript.txt --json transcript.json \
  --diarize --diarize-python-backend runtime/ \
  --identify --identify-python-backend runtime/
```

(Profiles are read from the default directory `~/Library/Application Support/stt/speakers/`; override with `--identify-profiles-dir` if needed.)

After diarisation+merge, each speaker cluster's embedding is matched against enrolled profiles. Confident matches (above threshold + margin) are relabeled with real names (e.g. `Mic Ada:`); unmatched speakers keep `Speaker N`. If multiple clusters match the same profile, only the highest-confidence cluster receives the name and lower-confidence duplicates remain anonymous with `duplicate_profile_match`; review them with `suggest-labels` rather than assuming they are unknown people. The JSON transcript gains a `speaker_name` field per relabeled segment while `speaker_id` (numeric) is always preserved.

Flags:
- `--identify` — enable identification (requires `--diarize`).
- `--identify-profiles-dir` — override the profiles directory.
- `--identify-threshold` / `--identify-margin` — match confidence/margin (defaults 0.78 / 0.05).
- `--identify-python-backend runtime/` — venv for extraction+matching.

Identification failures are non-fatal: they warn to stderr and the transcript keeps numeric `Speaker N` labels.

### Complete workflow

The simplest workflow uses the helper for everything. Diarisation and identification are automatic:

```bash
# 1. Record
recordings.py start --title "Project sync" --mode meeting
# ...stop when done...
# 2. Transcribe (auto: diarise + identify if profiles exist)
recordings.py transcribe
```

**First time with a new group of people** (no profiles yet): the transcript comes out with `Speaker 0`, `Speaker 1`, … labels. Enroll them through the **Safe speaker enrollment** workflow below; do not run the blocking `stt name-speakers` whole-cluster loop directly because a diarised cluster may contain multiple voices.

**All subsequent meetings** with the same people: `recordings.py transcribe` automatically identifies profiles in the default `~/Library/Application Support/stt/speakers/` location and labels confident matches by name. New/unknown participants and lower-confidence duplicate matches remain `Speaker N` until reviewed. `recordings.py` does not auto-detect custom profile directories configured through `STT_HOME` or stt config; use the manual CLI with `--identify-profiles-dir` for those.

Custom stores need extra care in `name_one_speaker.py`: `suggest-labels` and `enroll-ranges` use `STT_SPEAKER_PROFILES_DIR` (the speaker-store root containing `profiles/`), while guarded whole-cluster `enroll` delegates to `stt name-speakers`, which uses stt's default config/`STT_HOME` resolution and ignores `STT_SPEAKER_PROFILES_DIR`. Before enrollment, ensure both mechanisms resolve to the same store; prefer the default store unless custom configuration is intentional.

**Plain transcript only** (skip speaker labelling for speed):

```bash
recordings.py transcribe --no-diarize
```

The manual `stt transcribe-meeting --diarize --identify` commands documented above give you finer control (custom thresholds, profile directories, etc.) but are not needed for normal use.

## Safe speaker enrollment (mixed & duplicate clusters)

The one-speaker-at-a-time `enroll` workflow above assumes each diarised cluster is a single, pure speaker. In real meetings that assumption sometimes fails:

- **Mixed clusters** — diarisation collapses two different people into one `Speaker N` cluster (their speech is spread across a wide wall-clock span). Enrolling the whole cluster from all their speech would build a profile from two voices and **contaminate** it.
- **Duplicate clusters** — the same real person is split across two clusters (e.g. one on the mic track, one on the system track) that both match one enrolled profile. Enrolling both creates two profiles for one person.

The safe workflow screens clusters **before** enrolling, enrolls only confirmed speech for mixed clusters, and relabels transcript segments by confirmed name. The commands are available both as the `name_one_speaker.py` helper (agent-friendly, turn-based) and as `stt speaker` CLI subcommands.

The audit is a heuristic pre-screen, not voice recognition: `safe_to_enroll_whole_cluster: true` means the guard may proceed, not that the cluster is proven pure. Never enroll a new whole cluster without a fresh audit, production-provider duplicate check, purity preview, and human confirmation.

### Recommended order

1. **`audit --force`** — recompute the pre-screen without modifying transcripts or profiles; it writes/refreshes `speaker_audit.json`. Never trust a cached artifact after transcript or diarisation changes.
2. **`purity-preview`** — generate and play early/middle/late + best-energy clips for every new whole-cluster candidate, especially suspicious clusters, and ask the user to confirm whether it is one voice or multiple voices. It writes preview artifacts under `.speaker-clips/` but does not modify transcripts or profiles.
3. **`suggest-labels --provider speechbrain --force`** — match every cluster against real enrolled profiles and flag duplicate/mixed groups. It writes/refreshes `speaker_label_suggestions.json` without modifying transcripts or profiles. Re-run after transcript, diarisation, relabeling, or profile changes. `mfcc-test` is test-only and its embeddings are incompatible with SpeechBrain profiles.
4. **`enroll-ranges`** (mixed cluster) **or guarded `enroll`** (not flagged + human-confirmed cluster) — use only confirmed ranges for mixed clusters. Treat the guard's `allow` result as permission, not proof of purity.
5. **`relabel`** — apply a human-confirmed name to selected transcript segments (by speaker id + time ranges), preserving `speaker_id`; use `--dry-run` first.

### Commands

#### 1. audit — heuristic pre-screen (transcript/profile read-only)

```bash
# Helper (agent-friendly, takes --session)
python3 scripts/name_one_speaker.py audit \
  --session "<session>" --force

# CLI equivalent
stt speaker audit --transcript "<session>/transcript.json" \
  --mic "<session>/mic.wav" --system "<session>/system.wav" \
  --python-backend runtime/ --force
```

`audit` classifies each cluster into one of three heuristic statuses:

| status | meaning | guard field |
|---|---|---|
| `pure_likely` | no risky span pattern detected; still requires purity preview + human confirmation | `safe_to_enroll_whole_cluster: true` |
| `mixed_suspected` | conservative wide-span risk signal; may be one long-running speaker or multiple voices, so audio confirmation is required | `safe_to_enroll_whole_cluster: false` |
| `unknown` | too little useful speech to assess (< `--min-useful-speech`, default 5.0s) | `safe_to_enroll_whole_cluster: false` |

The heuristic uses useful-speech duration and wall-clock span; it cannot recognize whether two voices occur in a compact interval. Key flags: `--force` (recompute even if `speaker_audit.json` exists), `--min-useful-speech` (default 5.0s), `--mixed-span-ratio` (default 3.0), and `--json` (extra copy).

It writes `<session>/speaker_audit.json`. `list` surfaces the status inline, and the `enroll` guard consumes it. The artifact has no transcript hash/freshness check, so always use `--force` immediately before an enrollment decision.

#### 2. purity-preview — audio-confirm a suspicious cluster (transcript/profile read-only)

```bash
# Helper
python3 scripts/name_one_speaker.py purity-preview \
  --session "<session>" --speaker-id <id>

# CLI equivalent
stt speaker purity-preview --transcript "<session>/transcript.json" \
  --speaker-id <id> --mic "<session>/mic.wav" --system "<session>/system.wav" \
  --python-backend runtime/
```

Builds and plays ~12s clips from the **early, middle, and late** parts of the cluster plus a best-energy clip (loudness-normalized), so the user can hear whether the cluster is one consistent voice or two. It writes those preview files under `<session>/.speaker-clips/` but does not alter transcripts or profiles. Pass `--range` (repeatable) to restrict the universe of speech before window selection. Use `--no-play` to build clips without playing them.

Key flags: `--preview-seconds` (default 12), `--range` (repeatable; restrict to time ranges first), `--no-play`, `--no-normalize`.

After the user listens, ask them to confirm: one voice (eligible for guarded enrollment after the other checks), or multiple voices (use `enroll-ranges` and `relabel` instead).

#### 3. suggest-labels — match clusters against profiles (transcript/profile read-only)

```bash
# Helper: force a fresh production-provider match
python3 scripts/name_one_speaker.py suggest-labels \
  --session "<session>" --provider speechbrain --force

# CLI equivalent (the CLI does not cache its output)
stt speaker suggest-labels --transcript "<session>/transcript.json" \
  --mic "<session>/mic.wav" --system "<session>/system.wav" \
  --provider speechbrain --python-backend runtime/
```

Matches every cluster against enrolled profiles and writes `<session>/speaker_label_suggestions.json` with:

- `clusters` — per-cluster match suggestion (`reuse_profile` with a name, or `no_match`).
- `duplicateClusterGroups` — two or more clusters confidently matching the **same existing profile** (likely the same speaker); `recommendation: "merge_or_relabel"` means confirm and relabel/merge all matching clusters to that profile without enrolling another one.
- `mixedClusterWarnings` — clusters whose early vs late windows match **different** profiles (the embedding signature of two people in one cluster).

It writes `<session>/speaker_label_suggestions.json` but never modifies the transcript or profiles; the decision to relabel is left to the agent. With a custom speaker store, export `STT_SPEAKER_PROFILES_DIR="<speaker-store-root>"` before running the helper.

Key flags: `--force`, `--provider`, `--threshold` (default 0.78), `--margin` (default 0.05), `--minimum-speech-seconds` (default 8), `--no-windows` / `--n-windows` (default 2), and `--output`. The helper defaults to `mfcc-test` for deterministic tests, but real identity/safety decisions must explicitly use `--provider speechbrain`; otherwise real SpeechBrain profiles are skipped as `provider_model_mismatch`.

#### 4. enroll (guarded) — safe whole-cluster enrollment

`enroll` now loads `<session>/speaker_audit.json` and decides before enrolling:

- `safe_to_enroll_whole_cluster == true` (status `pure_likely`) → the guard **allows** enrollment, but proceed only after `purity-preview` and human confirmation.
- `false` (status `mixed_suspected` or `unknown`) → **refuse**: exits before any audio playback or profile creation and prints the exact `purity-preview` + `enroll-ranges` commands to run instead.
- **no audit found** / **speaker not in audit** → the implementation warns and allows, but the agent must stop and run `audit --force`; do not treat the warning path as authorization.

```bash
# Dry run — show the guard's decision WITHOUT enrolling (no audio, no profile)
python3 scripts/name_one_speaker.py enroll \
  --session "<session>" --speaker-id <id> --name "<display name>" --no-enroll

# Real enrollment (only proceeds for safe clusters; refused clusters exit non-zero)
python3 scripts/name_one_speaker.py enroll \
  --session "<session>" --speaker-id <id> --name "<display name>"
```

**`--no-enroll` is now the guard-aware dry run** (it evaluates the guard and reports `would_enroll`, never touching the backend), not the old backend dry run. Always run it first to see the safety decision.

#### 4b. enroll-ranges — enroll only confirmed speech (mixed clusters)

For a mixed cluster, the user confirms specific time ranges of one voice via `purity-preview`, then enroll only those ranges:

```bash
# Dry run — validate ranges without writing files
python3 scripts/name_one_speaker.py enroll-ranges \
  --session "<session>" --speaker-id <id> --name "<display name>" \
  --range 12.0-45.0 --range 200.0-240.0 --no-enroll

# Real enrollment — builds a range-limited sample and enrolls a profile from it
python3 scripts/name_one_speaker.py enroll-ranges \
  --session "<session>" --speaker-id <id> --name "<display name>" \
  --range 12.0-45.0 --range 200.0-240.0
```

CLI equivalent:

```bash
stt speaker enroll-ranges "<display name>" --transcript "<session>/transcript.json" \
  --speaker-id <id> --range 12.0-45.0 --range 200.0-240.0 \
  --mic "<session>/mic.wav" --system "<session>/system.wav" --python-backend runtime/
```

`--range` is repeatable and required (at least one). Ranges accept `start-end` seconds (`12.0-45.0`), `MM:SS` (`02:03-03:00`), or `HH:MM:SS`. The range-limited sample is built from **only** the requested ranges (never falls back to whole-cluster audio), so a contaminated cluster cannot leak the second voice into the profile.

If the display name already exists, enrollment is **skipped** rather than overwritten. If it is the same person, relabel to the existing profile. Otherwise use a different name, or obtain explicit user authorization before renaming/removing a profile (`stt speaker remove "<name>" --yes`).

#### 5. relabel — apply a confirmed name to transcript segments

`relabel` applies a human-confirmed `speaker_name` to a subset of one cluster's segments (by speaker id + time ranges), **preserving `speaker_id`, `source`, timestamps, and text**. This lets a mixed cluster carry two human names (e.g. the early segments → "Dana", the late segments → "Ada") without changing the underlying diarisation ids.

```bash
# Dry run — report which segments would change without writing
python3 scripts/name_one_speaker.py relabel \
  --session "<session>" --speaker-id <id> --name "<display name>" \
  --range 12.0-120.0 --dry-run

# Apply — updates transcript.json + regenerated transcript.md (+ per-source .json in place)
python3 scripts/name_one_speaker.py relabel \
  --session "<session>" --speaker-id <id> --name "<display name>" \
  --range 12.0-120.0
```

When `--range` is omitted, **all** of the speaker's segments are relabeled (whole-cluster relabel). Per-source artifacts (`transcript.<source>.json`) are relabeled in place for the matching speaker id + ranges so every JSON stays consistent; raw `transcript.<source>.txt` files are never speaker-labeled and are not regenerated.

`relabel` is helper-only (not a `stt speaker` subcommand). Use it after the user confirms the correct name for the selected ranges.

### Whole-cluster enrollment decisions

| Evidence | Audit status | Whole-cluster `enroll`? |
|---|---|---|
| Heuristic not triggered; preview confirms one voice | `pure_likely` | Guard allows; enroll only after human confirmation |
| Two voices collapsed into one cluster | `mixed_suspected` | ❌ Use `enroll-ranges` + `relabel` |
| Too little speech to assess | `unknown` | ❌ Collect more speech or review confirmed ranges manually |
| Missing or potentially stale audit | missing/stale | ❌ Run `audit --force`; do not use the guard's warning path |

The guard's refusal runs **before** any audio playback or `subprocess.run`, so a refused `enroll` creates no `.speaker-clips/` directory and no profile. Its allow result only reflects the audit artifact; it does not establish that the artifact is fresh or that the cluster contains one voice.

### Worked examples

#### Example A — duplicate clusters (Sam on the system track)

In the validated Example Exchange fixture, Sam appears in system-track `Speaker 1`, `Speaker 3`, and `Speaker 5`. The deterministic regression validated that duplicate grouping; the live `mfcc-test` run could not compare against SpeechBrain profiles, so always run the production-provider command on the actual session before acting.

```bash
# 1. Refresh suggestions against the existing production profiles
python3 scripts/name_one_speaker.py suggest-labels \
  --session "<session>" --provider speechbrain --force
# Proceed only if the result groups Speakers 1, 3, and 5 under Sam's existing profile.

# 2. Preview each cluster and ask the user to confirm it is Sam
for id in 1 3 5; do
  python3 scripts/name_one_speaker.py purity-preview \
    --session "<session>" --speaker-id "$id"
done

# 3. After confirmation, dry-run and then relabel ALL matching clusters.
# Do not call enroll: the Sam profile already exists.
for id in 1 3 5; do
  python3 scripts/name_one_speaker.py relabel \
    --session "<session>" --speaker-id "$id" --name "Sam Rivera" --dry-run
  python3 scripts/name_one_speaker.py relabel \
    --session "<session>" --speaker-id "$id" --name "Sam Rivera"
done
```

The lesson: a duplicate group already points to an enrolled profile. Confirm the live SpeechBrain result and the audio, then relabel/merge every matching cluster onto that profile; create no new profile.

#### Example B — mixed cluster (Dana early, Ada late in Speaker 4)

One diarisation cluster, `Speaker 4`, actually contains two people: Dana speaks early, Ada speaks late. Enrolling the whole cluster would mix two voices into one profile.

```bash
# 1. Force a fresh audit; it raises a conservative risk signal for the cluster
python3 scripts/name_one_speaker.py audit \
  --session "<session>" --force
# → Speaker 4: status mixed_suspected, safe_to_enroll_whole_cluster: false

# 2. Audio-confirm: play early/middle/late clips so the user hears two voices
python3 scripts/name_one_speaker.py purity-preview \
  --session "<session>" --speaker-id 4
# User confirms Dana Tamayo at 180.22–184.88 + 200.17–213.32,
# and Alex Chen at 774.00–818.05.

# 3. Refresh real-profile suggestions before creating any profile
python3 scripts/name_one_speaker.py suggest-labels \
  --session "<session>" --provider speechbrain --force
```

In the validated Example Exchange session, `Dana Tamayo` and `Alex Chen` profiles already exist, so **do not enroll**. Dry-run both relabels, inspect the changed segment counts, and then apply:

```bash
python3 scripts/name_one_speaker.py relabel \
  --session "<session>" --speaker-id 4 --name "Dana Tamayo" \
  --range 180.22-184.88 --range 200.17-213.32 --dry-run
python3 scripts/name_one_speaker.py relabel \
  --session "<session>" --speaker-id 4 --name "Dana Tamayo" \
  --range 180.22-184.88 --range 200.17-213.32
python3 scripts/name_one_speaker.py relabel \
  --session "<session>" --speaker-id 4 --name "Alex Chen" \
  --range 774.00-818.05 --dry-run
python3 scripts/name_one_speaker.py relabel \
  --session "<session>" --speaker-id 4 --name "Alex Chen" \
  --range 774.00-818.05
```

If a confirmed person has **no** reusable profile in another session, first validate only their ranges with `enroll-ranges ... --no-enroll`; after inspecting the dry-run and obtaining explicit confirmation, rerun without `--no-enroll`. Never enroll when suggestions show an existing profile.

After relabeling, those `transcript.json` segments carry `speaker_name` "Dana Tamayo" / "Alex Chen" while `speaker_id` stays `4` — so a mixed cluster can hold multiple human names without breaking diarisation.

### Generated artifacts

| Artifact | Location | Written by |
|---|---|---|
| Speaker audit | `<session>/speaker_audit.json` | `audit` |
| Label suggestions | `<session>/speaker_label_suggestions.json` | `suggest-labels` |
| Flattened profile copy (includes embeddings) | `<speaker-store-root>/.speaker-clips/_flattened_profiles.json` | helper `suggest-labels` |
| Purity/preview clips | `<session>/.speaker-clips/` | `preview`, `purity-preview` |
| Filtered one-speaker transcript | `<session>/.speaker-clips/speaker-<id>-only-transcript.json` | guarded whole-cluster `enroll` |
| Range-limited enrollment sample | `<session>/.speaker-clips/speaker-<id>-enroll-ranges-*.wav` (+ `.enroll.json` metadata) | `enroll-ranges` |
| Profile provenance | `<session>/.speaker-clips/speaker-<id>-enroll.provenance.json` | `enroll-ranges` → forwarded to `stt speaker enroll --provenance-json` |
| Enrolled profile | `~/Library/Application Support/stt/speakers/profiles/<id>.json` (+ `samples/<id>/*.wav`) | `stt speaker enroll` |

For `enroll-ranges`, the provenance payload records where the voice came from: source session, transcript, track, diarised speaker id, confirmed ranges, and `range-limited` confirmation mode. `samplePath` is filled by `stt speaker enroll` at enrollment time (canonical `samples/<id>/<ts>.wav`). Whole-audio/whole-cluster enrollment does not currently populate this provenance automatically, and legacy profiles also load with `provenance: nil`.

The flattened profile copy contains local speaker embeddings and persists until explicitly cleaned up. Treat it as sensitive local data, do not upload it, and ask before deleting it or any generated artifact.

### Failure handling

- **Refused whole-cluster enrollment** (`mixed_suspected` / `unknown`): the guard exits before any audio playback or profile creation. Follow the printed `purity-preview` → `enroll-ranges` commands. Never bypass the guard by enrolling from raw audio for a refused cluster.
- **Display name already exists**: `enroll` / `enroll-ranges` **skips** rather than overwriting. Prefer relabeling to the existing profile when it is the same person. Profile replacement/removal is destructive: obtain explicit user authorization before using `stt speaker enroll --replace` or `stt speaker remove "<name>" --yes`.
- **No usable speech in the requested ranges** (`enroll-ranges`): fails fast with a clear message; never falls back to whole-cluster audio. Ask the user to confirm wider ranges via `purity-preview`.
- **Playback failure** (`preview`/`purity-preview`): `afplay` may be blocked or the clip empty. Re-run with `--no-play` to build clips the user can play manually from `.speaker-clips/`.
- **No audit run yet or inputs changed**: the guard may warn and allow enrollment, but stop instead. Run `audit --force`, refresh `suggest-labels --provider speechbrain --force`, perform `purity-preview`, and obtain human confirmation before enrolling.

