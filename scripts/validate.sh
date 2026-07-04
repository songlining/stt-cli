#!/usr/bin/env zsh
set -euo pipefail

# Bounded local validation for the Swift STT CLI. This intentionally avoids
# broad filesystem scans and uses short, finite recording smoke tests.

SCRIPT_DIR="${0:A:h}"
REPO_ROOT="${SCRIPT_DIR:h}"
SMOKE_DIR="${SMOKE_DIR:-/tmp/stt-smoke-validate}"
APP_BIN="${REPO_ROOT}/dist/stt.app/Contents/MacOS/stt"
MIC_WAV="${SMOKE_DIR}/mic.wav"

cd "${REPO_ROOT}"

print "== Swift build/test =="
swift build
swift test
swift run sttUnitChecks

if [[ -x "python/.venv/bin/python" ]]; then
  print "== Python tests =="
  python/.venv/bin/python -m pytest python/tests
else
  print "== Python tests skipped: python/.venv/bin/python not found =="
fi

print "== App bundle =="
./scripts/build-app-bundle.sh
"${APP_BIN}" doctor
codesign --verify --deep --strict --verbose=2 dist/stt.app

PACKAGED_BACKEND="${REPO_ROOT}/dist/stt.app/Contents/Resources/python/stt_vibevoice/status.py"
if [[ ! -f "${PACKAGED_BACKEND}" ]]; then
  print -u2 "error: packaged Python backend missing at ${PACKAGED_BACKEND}"
  exit 1
fi

print "== Bundled backend lookup from outside repo =="
BUNDLED_DOCTOR_OUTPUT="$(cd /tmp && "${APP_BIN}" doctor)"
print "${BUNDLED_DOCTOR_OUTPUT}" | grep -q "Transcription backend:"
print "${BUNDLED_DOCTOR_OUTPUT}" | grep -q "overall ready:"

print "== Finite mic smoke test =="
mkdir -p "${SMOKE_DIR}"
"${APP_BIN}" record --mode mic --duration 2 --output "${MIC_WAV}"
ls -lh "${MIC_WAV}"
ffprobe -v error -show_entries format=duration,size -of default=nw=1 "${MIC_WAV}"
MIC_SIZE="$(stat -f%z "${MIC_WAV}")"
if [[ "${MIC_SIZE}" -le 4096 ]]; then
  print -u2 "error: mic smoke test produced header-only/suspiciously small file (${MIC_SIZE} bytes)"
  exit 1
fi

print "== Pipeline metadata smoke test =="
PIPE_HOME="${SMOKE_DIR}/pipeline-home-$(date +%Y%m%d%H%M%S)"
mkdir -p "${PIPE_HOME}"
set +e
STT_HOME="${PIPE_HOME}" "${APP_BIN}" pipeline --mode mic --name smoke --duration 1 --transcribe-timeout 5 --device cpu >"${PIPE_HOME}/stdout.txt" 2>"${PIPE_HOME}/stderr.txt"
PIPE_STATUS=$?
set -e
if [[ ${PIPE_STATUS} -eq 0 ]]; then
  print "Pipeline completed successfully."
else
  print "Pipeline exited ${PIPE_STATUS} after recording; this is acceptable when the local MLX backend is not installed."
fi
METADATA_PATH="$(find "${PIPE_HOME}" -maxdepth 4 -name metadata.json -type f | head -1)"
if [[ -z "${METADATA_PATH}" ]]; then
  print -u2 "error: pipeline metadata.json was not written"
  print -u2 "stdout:"
  head -80 "${PIPE_HOME}/stdout.txt" >&2 || true
  print -u2 "stderr:"
  head -80 "${PIPE_HOME}/stderr.txt" >&2 || true
  exit 1
fi
python3 -m json.tool "${METADATA_PATH}" >/dev/null
python3 - "${METADATA_PATH}" <<'PY'
import json
import sys
from pathlib import Path

metadata_path = Path(sys.argv[1])
payload = json.loads(metadata_path.read_text())
required = ["runID", "name", "mode", "startedAt", "finishedAt", "durationSeconds", "outputPaths"]
missing = [key for key in required if key not in payload]
if missing:
    raise SystemExit(f"metadata missing keys: {missing}")
if not payload["outputPaths"]:
    raise SystemExit("metadata outputPaths is empty")
PY
print "Pipeline metadata: ${METADATA_PATH}"

print "Validation complete. Smoke artifacts: ${SMOKE_DIR}"
