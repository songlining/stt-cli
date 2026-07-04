#!/usr/bin/env zsh
set -euo pipefail

# Manual TCC attribution smoke test for the bundled stt.app.
#
# By default this script does NOT reset permissions. To force a fresh macOS
# microphone prompt for stt's bundle identifier, run:
#
#   STT_RESET_TCC=1 ./scripts/manual-tcc-smoke.sh
#
# The reset is intentionally opt-in because it changes local privacy state for
# com.hashicorp.stt.

SCRIPT_DIR="${0:A:h}"
REPO_ROOT="${SCRIPT_DIR:h}"
BUNDLE_ID="${BUNDLE_ID:-com.hashicorp.stt}"
APP_BIN="${REPO_ROOT}/dist/stt.app/Contents/MacOS/stt"
SMOKE_DIR="${SMOKE_DIR:-/tmp/stt-tcc-smoke}"
MIC_WAV="${SMOKE_DIR}/mic.wav"

source "${REPO_ROOT}/scripts/lib/bundle-check.sh"
source "${REPO_ROOT}/scripts/lib/system-fallback-check.sh"

cd "${REPO_ROOT}"

print "== Build bundled app =="
./scripts/build-app-bundle.sh
assert_app_bundle_tcc_configuration "${REPO_ROOT}/dist/stt.app" "${BUNDLE_ID}"

if [[ "${STT_RESET_TCC:-0}" == "1" ]]; then
  print "== Reset microphone permission for ${BUNDLE_ID} =="
  tccutil reset Microphone "${BUNDLE_ID}"
  print "The next recording command should show a macOS microphone prompt attributed to stt."
else
  print "== TCC reset skipped =="
  print "Set STT_RESET_TCC=1 to reset Microphone permission for ${BUNDLE_ID} before this smoke test."
fi

print "== Doctor should report .app bundle attribution =="
"${APP_BIN}" doctor

print "== Finite mic recording smoke =="
mkdir -p "${SMOKE_DIR}"
"${APP_BIN}" record --mode mic --duration 2 --output "${MIC_WAV}"
ls -lh "${MIC_WAV}"
ffprobe -v error -show_entries format=duration,size -of default=nw=1 "${MIC_WAV}"
assert_audio_file_has_payload \
  "${MIC_WAV}" \
  "mic TCC smoke" \
  "If a permission prompt appeared, confirm it was accepted for ${BUNDLE_ID}."

run_optional_system_fallback_smoke \
  "${APP_BIN}" \
  "${SMOKE_DIR}" \
  "Optional system fallback recording smoke" \
  "Check that ${STT_SYSTEM_DEVICE:-the selected device} is receiving routed system audio."

print "== Manual check =="
print "If STT_RESET_TCC=1 was used, confirm the macOS prompt named stt/com.hashicorp.stt, not Terminal."
print "Smoke artifact: ${MIC_WAV}"
