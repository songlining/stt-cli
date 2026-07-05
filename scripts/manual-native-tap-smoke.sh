#!/usr/bin/env zsh
set -euo pipefail

# Manual native CoreAudio process-tap smoke test for the bundled stt.app.
#
# This script is intentionally opt-in and is not executed by validate.sh/CI. It
# requires macOS system audio to be playing during the recording window and may
# require a first-run TCC prompt attributed to stt/com.larrysong.stt.
#
# Usage:
#   ./scripts/manual-native-tap-smoke.sh
#   STT_NATIVE_SMOKE_DURATION=5 ./scripts/manual-native-tap-smoke.sh
#
# Do not use STT_RESET_TCC here; run scripts/manual-tcc-smoke.sh separately if
# you explicitly want to reset local privacy state.

SCRIPT_DIR="${0:A:h}"
REPO_ROOT="${SCRIPT_DIR:h}"
BUNDLE_ID="${BUNDLE_ID:-com.larrysong.stt}"
APP_BIN="${REPO_ROOT}/dist/stt.app/Contents/MacOS/stt"
SMOKE_ROOT="${SMOKE_DIR:-/tmp/stt-native-tap-smoke}"
SMOKE_DIR="${SMOKE_ROOT}/$(date +%Y%m%d-%H%M%S)"
SYSTEM_WAV="${SMOKE_DIR}/system-native.wav"
DURATION="${STT_NATIVE_SMOKE_DURATION:-3}"

source "${REPO_ROOT}/scripts/lib/bundle-check.sh"
source "${REPO_ROOT}/scripts/lib/system-fallback-check.sh"

cd "${REPO_ROOT}"

print "== Native CoreAudio process-tap manual smoke =="
print "This test records real system output through the native tap path."
print "Start audible system audio now (music/video/test tone), then continue."
print "Artifacts: ${SMOKE_DIR}"

print "== Build bundled app =="
./scripts/build-app-bundle.sh
assert_app_bundle_tcc_configuration "${REPO_ROOT}/dist/stt.app" "${BUNDLE_ID}"

print "== Doctor with opt-in native tap diagnostics =="
STT_NATIVE_TAP_DIAGNOSTIC=1 STT_NATIVE_TAP_PAYLOAD_DIAGNOSTIC=1 "${APP_BIN}" doctor

print "== Native system recording (${DURATION}s) =="
mkdir -p "${SMOKE_DIR}"

set +e
"${APP_BIN}" record \
  --mode system \
  --duration "${DURATION}" \
  --fail-if-empty \
  --output "${SYSTEM_WAV}" \
  >"${SMOKE_DIR}/stdout.txt" \
  2>"${SMOKE_DIR}/stderr.txt"
RECORD_EXIT=$?
set -e

cat "${SMOKE_DIR}/stdout.txt"
if [[ -s "${SMOKE_DIR}/stderr.txt" ]]; then
  cat "${SMOKE_DIR}/stderr.txt" >&2
fi

if [[ ${RECORD_EXIT} -ne 0 ]]; then
  print -u2 "error: native system recording failed with exit ${RECORD_EXIT}"
  print -u2 "If this is the first run, approve the macOS prompt for ${BUNDLE_ID} and rerun."
  print -u2 "If native capture is unavailable, validate fallback separately with STT_SYSTEM_DEVICE=... ./scripts/validate.sh."
  exit "${RECORD_EXIT}"
fi

if ! grep -Eq "Recording system audio via native CoreAudio process tap|native system-audio tap" "${SMOKE_DIR}/stdout.txt"; then
  print -u2 "error: system recording completed but did not report native CoreAudio process-tap capture"
  print -u2 "This script validates the native CoreAudio path only, not the named input-device fallback."
  exit 1
fi

ls -lh "${SYSTEM_WAV}"
ffprobe -v error -show_entries format=duration,size -of default=nw=1 "${SYSTEM_WAV}"
assert_audio_file_has_payload \
  "${SYSTEM_WAV}" \
  "native CoreAudio process-tap smoke" \
  "Native capture produced no meaningful payload. Ensure system audio was playing and TCC permissions were granted for ${BUNDLE_ID}."

python3 - "${SYSTEM_WAV}" "${BUNDLE_ID}" <<'PY'
import struct
import sys
import wave
from pathlib import Path

audio_path = Path(sys.argv[1])
bundle_id = sys.argv[2]
with wave.open(str(audio_path), "rb") as wav:
    frames = wav.readframes(wav.getnframes())
    samples = struct.unpack("<" + "h" * (len(frames) // 2), frames)
nonzero = sum(1 for sample in samples if sample)
peak = max((abs(sample) for sample in samples), default=0)
print(f"payload samples={len(samples)} nonzero={nonzero} peak={peak}")
if nonzero == 0 or peak == 0:
    raise SystemExit(
        "native CoreAudio process-tap ran but captured all-zero/silent payload. "
        "If launched from a terminal, macOS attributes System Audio Recording permission "
        "to the terminal app (Ghostty/Terminal/iTerm), not necessarily to "
        f"{bundle_id}. Grant System Audio Recording to the responsible app in "
        "System Settings > Privacy & Security > Screen & System Audio Recording, "
        "then rerun this smoke test."
    )
PY

print "== Manual check =="
print "Confirm the captured WAV contains the system audio that was playing."
print "If a privacy prompt appeared, confirm it was attributed to stt/${BUNDLE_ID}, not Terminal."
print "Native smoke artifact: ${SYSTEM_WAV}"
