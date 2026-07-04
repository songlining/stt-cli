#!/usr/bin/env zsh
set -euo pipefail

# Bounded local validation for the Swift STT CLI. This intentionally avoids
# broad filesystem scans and uses short, finite recording smoke tests.

SCRIPT_DIR="${0:A:h}"
REPO_ROOT="${SCRIPT_DIR:h}"
SMOKE_DIR="${SMOKE_DIR:-/tmp/stt-smoke-validate}"
APP_BIN="${REPO_ROOT}/dist/stt.app/Contents/MacOS/stt"
MIC_WAV="${SMOKE_DIR}/mic.wav"

source "${REPO_ROOT}/scripts/lib/bundle-check.sh"
source "${REPO_ROOT}/scripts/lib/system-fallback-check.sh"

cd "${REPO_ROOT}"

print "== Shell script syntax =="
zsh -n scripts/bootstrap-python-backend.sh
zsh -n scripts/build-app-bundle.sh
zsh -n scripts/check-app-bundle.sh
zsh -n scripts/manual-tcc-smoke.sh
zsh -n scripts/validate.sh
zsh -n scripts/lib/bundle-check.sh
zsh -n scripts/lib/system-fallback-check.sh
./scripts/bootstrap-python-backend.sh --help >/dev/null

print "== Swift build/test =="
swift build
swift test
swift run sttUnitChecks

if [[ -x "python/.venv/bin/python" ]]; then
  print "== Python tests =="
  python/.venv/bin/python -m pytest python/tests

  print "== Python backend status CLI =="
  PYTHONPATH=python python/.venv/bin/python -m stt_vibevoice.status --json | python3 -m json.tool >/dev/null
  STATUS_OUT="${SMOKE_DIR}/stt-status-check.out"
  STATUS_ERR="${SMOKE_DIR}/stt-status-check.err"
  mkdir -p "${SMOKE_DIR}"
  set +e
  PYTHONPATH=python python/.venv/bin/python -m stt_vibevoice.status --fail-if-not-ready >"${STATUS_OUT}" 2>"${STATUS_ERR}"
  STATUS_READY_EXIT=$?
  set -e
  if [[ ${STATUS_READY_EXIT} -eq 0 ]]; then
    print "Python backend reports ready."
  elif [[ ${STATUS_READY_EXIT} -eq 1 ]]; then
    if [[ "${STT_REQUIRE_BACKEND_READY:-0}" == "1" ]]; then
      print -u2 "error: Python backend is not ready and STT_REQUIRE_BACKEND_READY=1"
      head -80 "${STATUS_OUT}" >&2 || true
      head -80 "${STATUS_ERR}" >&2 || true
      exit 1
    fi
    print "Python backend reports not ready; continuing because validation also covers pre-setup environments."
    print "Set STT_REQUIRE_BACKEND_READY=1 to make this a hard failure."
  else
    print -u2 "error: backend readiness check failed unexpectedly with exit ${STATUS_READY_EXIT}"
    head -80 "${STATUS_OUT}" >&2 || true
    head -80 "${STATUS_ERR}" >&2 || true
    exit 1
  fi
else
  print "== Python tests skipped: python/.venv/bin/python not found =="
fi

print "== App bundle =="
./scripts/build-app-bundle.sh
"${APP_BIN}" doctor
codesign --verify --deep --strict --verbose=2 dist/stt.app
./scripts/check-app-bundle.sh "${REPO_ROOT}/dist/stt.app"

PACKAGED_BACKEND="${REPO_ROOT}/dist/stt.app/Contents/Resources/python/stt_vibevoice/status.py"
if [[ ! -f "${PACKAGED_BACKEND}" ]]; then
  print -u2 "error: packaged Python backend missing at ${PACKAGED_BACKEND}"
  exit 1
fi

print "== Bundled backend lookup from outside repo =="
BUNDLED_DOCTOR_OUTPUT="$(cd /tmp && "${APP_BIN}" doctor)"
print "${BUNDLED_DOCTOR_OUTPUT}" | grep -q "Transcription backend:"
print "${BUNDLED_DOCTOR_OUTPUT}" | grep -q "overall ready:"

print "== Strict doctor readiness semantics =="
set +e
STRICT_DOCTOR_OUTPUT="$(cd /tmp && "${APP_BIN}" doctor --require-backend-ready 2>&1)"
STRICT_DOCTOR_EXIT=$?
set -e
if print "${BUNDLED_DOCTOR_OUTPUT}" | grep -q "overall ready: no"; then
  if [[ ${STRICT_DOCTOR_EXIT} -eq 0 ]]; then
    print -u2 "error: doctor --require-backend-ready succeeded even though backend is not ready"
    print -u2 "${STRICT_DOCTOR_OUTPUT}"
    exit 1
  fi
else
  if [[ ${STRICT_DOCTOR_EXIT} -ne 0 ]]; then
    print -u2 "error: doctor --require-backend-ready failed even though backend appears ready"
    print -u2 "${STRICT_DOCTOR_OUTPUT}"
    exit 1
  fi
fi

if print "${BUNDLED_DOCTOR_OUTPUT}" | grep -q "overall ready: no"; then
  print "== Strict transcribe/pipeline preflight semantics =="
  set +e
  STRICT_TRANSCRIBE_OUTPUT="$(cd /tmp && "${APP_BIN}" transcribe /tmp/definitely-missing-stt-audio.wav --require-backend-ready 2>&1)"
  STRICT_TRANSCRIBE_EXIT=$?
  set -e
  if [[ ${STRICT_TRANSCRIBE_EXIT} -eq 0 ]]; then
    print -u2 "error: transcribe --require-backend-ready succeeded even though backend is not ready"
    print -u2 "${STRICT_TRANSCRIBE_OUTPUT}"
    exit 1
  fi

  STRICT_PIPE_HOME="${SMOKE_DIR}/strict-pipeline-home-$(date +%Y%m%d%H%M%S)"
  mkdir -p "${STRICT_PIPE_HOME}"
  set +e
  STT_HOME="${STRICT_PIPE_HOME}" "${APP_BIN}" pipeline --mode mic --name strict --duration 1 --require-backend-ready >"${STRICT_PIPE_HOME}/stdout.txt" 2>"${STRICT_PIPE_HOME}/stderr.txt"
  STRICT_PIPE_EXIT=$?
  set -e
  if [[ ${STRICT_PIPE_EXIT} -eq 0 ]]; then
    print -u2 "error: pipeline --require-backend-ready succeeded even though backend is not ready"
    head -80 "${STRICT_PIPE_HOME}/stdout.txt" >&2 || true
    head -80 "${STRICT_PIPE_HOME}/stderr.txt" >&2 || true
    exit 1
  fi
  STRICT_METADATA_PATH="$(find "${STRICT_PIPE_HOME}" -maxdepth 4 -name metadata.json -type f | head -1)"
  if [[ -n "${STRICT_METADATA_PATH}" ]]; then
    print -u2 "error: strict pipeline wrote metadata despite failing readiness preflight: ${STRICT_METADATA_PATH}"
    exit 1
  fi
fi

print "== Finite mic smoke test =="
mkdir -p "${SMOKE_DIR}"
"${APP_BIN}" record --mode mic --duration 2 --output "${MIC_WAV}"
ls -lh "${MIC_WAV}"
ffprobe -v error -show_entries format=duration,size -of default=nw=1 "${MIC_WAV}"
assert_audio_file_has_payload "${MIC_WAV}" "mic smoke test" "Check microphone permission and selected input device."

run_optional_system_fallback_smoke \
  "${APP_BIN}" \
  "${SMOKE_DIR}" \
  "Optional system fallback smoke test" \
  "Check that ${STT_SYSTEM_DEVICE:-the selected device} is receiving routed system audio before running this optional check."

validate_metadata() {
  local metadata_path="$1"
  python3 -m json.tool "${metadata_path}" >/dev/null
  python3 - "${metadata_path}" <<'PY'
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
}

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
validate_metadata "${METADATA_PATH}"
print "Pipeline metadata: ${METADATA_PATH}"

print "== Fake backend smoke tests =="
FAKE_BACKEND="${SMOKE_DIR}/fake-backend"
mkdir -p "${FAKE_BACKEND}/stt_vibevoice"
touch "${FAKE_BACKEND}/stt_vibevoice/__init__.py"
cat >"${FAKE_BACKEND}/stt_vibevoice/transcribe.py" <<'PY'
from __future__ import annotations

import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("audio_path")
parser.add_argument("--device", default="auto")
parser.add_argument("--output")
parser.add_argument("--json", dest="json_output")
parser.add_argument("--model")
parser.add_argument("--max-new-tokens")
args = parser.parse_args()

text = "fake transcript from validation backend"
if args.output:
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(text + "\n", encoding="utf-8")
if args.json_output:
    payload = {
        "backend": "fake-validation-backend",
        "text": text,
        "duration": 1.0,
        "audio_file": args.audio_path,
        "device": args.device,
    }
    Path(args.json_output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json_output).write_text(json.dumps(payload), encoding="utf-8")
print(json.dumps({"backend": "fake-validation-backend", "text": text, "duration": 1.0}))
PY

print "== Standalone transcribe smoke test with fake backend =="
FAKE_TRANSCRIBE_DIR="${SMOKE_DIR}/transcribe-success-$(date +%Y%m%d%H%M%S)"
mkdir -p "${FAKE_TRANSCRIBE_DIR}"
"${APP_BIN}" transcribe "${MIC_WAV}" \
  --output "${FAKE_TRANSCRIBE_DIR}/transcript.txt" \
  --json "${FAKE_TRANSCRIBE_DIR}/transcript.json" \
  --device cpu \
  --python-backend "${FAKE_BACKEND}" \
  >"${FAKE_TRANSCRIBE_DIR}/stdout.txt" \
  2>"${FAKE_TRANSCRIBE_DIR}/stderr.txt"
python3 -m json.tool "${FAKE_TRANSCRIBE_DIR}/transcript.json" >/dev/null
grep -q "fake transcript" "${FAKE_TRANSCRIBE_DIR}/transcript.txt"
grep -q "fake transcript" "${FAKE_TRANSCRIBE_DIR}/stdout.txt"

print "== Successful pipeline smoke test with fake backend =="
FAKE_PIPE_HOME="${SMOKE_DIR}/pipeline-success-home-$(date +%Y%m%d%H%M%S)"
mkdir -p "${FAKE_PIPE_HOME}"
STT_HOME="${FAKE_PIPE_HOME}" "${APP_BIN}" pipeline --mode mic --name success --duration 1 --transcribe-timeout 5 --device cpu --python-backend "${FAKE_BACKEND}" >"${FAKE_PIPE_HOME}/stdout.txt" 2>"${FAKE_PIPE_HOME}/stderr.txt"
FAKE_METADATA_PATH="$(find "${FAKE_PIPE_HOME}" -maxdepth 4 -name metadata.json -type f | head -1)"
if [[ -z "${FAKE_METADATA_PATH}" ]]; then
  print -u2 "error: successful fake-backend pipeline did not write metadata.json"
  head -80 "${FAKE_PIPE_HOME}/stdout.txt" >&2 || true
  head -80 "${FAKE_PIPE_HOME}/stderr.txt" >&2 || true
  exit 1
fi
validate_metadata "${FAKE_METADATA_PATH}"
python3 - "${FAKE_METADATA_PATH}" <<'PY'
import json
import sys
from pathlib import Path

metadata_path = Path(sys.argv[1])
payload = json.loads(metadata_path.read_text())
if payload.get("backend") != "fake-validation-backend":
    raise SystemExit(f"unexpected backend in metadata: {payload.get('backend')}")
if payload.get("notes") is not None:
    raise SystemExit(f"unexpected notes in successful metadata: {payload.get('notes')}")
text_path = Path(payload["transcriptTextPath"])
json_path = Path(payload["transcriptJSONPath"])
if not text_path.exists():
    raise SystemExit(f"missing transcript text: {text_path}")
if not json_path.exists():
    raise SystemExit(f"missing transcript json: {json_path}")
if "fake transcript" not in text_path.read_text(encoding="utf-8"):
    raise SystemExit("fake transcript text not written")
PY
print "Successful fake-backend pipeline metadata: ${FAKE_METADATA_PATH}"

print "Validation complete. Smoke artifacts: ${SMOKE_DIR}"
