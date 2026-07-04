#!/usr/bin/env zsh
set -euo pipefail

# Bounded local validation for the Swift STT CLI. This intentionally avoids
# broad filesystem scans and uses short, finite recording smoke tests.

SCRIPT_DIR="${0:A:h}"
REPO_ROOT="${SCRIPT_DIR:h}"
SMOKE_DIR="${SMOKE_DIR:-/tmp/stt-smoke-validate}"
APP_BIN="${REPO_ROOT}/dist/stt.app/Contents/MacOS/stt"
MIC_WAV="${SMOKE_DIR}/mic.wav"
FIXTURE_WAV="${SMOKE_DIR}/fixture.wav"
SKIP_MIC_HARDWARE="${STT_SKIP_MIC_HARDWARE:-0}"

source "${REPO_ROOT}/scripts/lib/bundle-check.sh"
source "${REPO_ROOT}/scripts/lib/system-fallback-check.sh"

cd "${REPO_ROOT}"

print "== Shell script syntax =="
zsh -n scripts/bootstrap-python-backend.sh
zsh -n scripts/build-app-bundle.sh
zsh -n scripts/check-app-bundle.sh
zsh -n scripts/check-python-backend.sh
zsh -n scripts/manual-tcc-smoke.sh
zsh -n scripts/validate.sh
zsh -n scripts/lib/bundle-check.sh
zsh -n scripts/lib/system-fallback-check.sh
./scripts/bootstrap-python-backend.sh --help >/dev/null
./scripts/check-python-backend.sh --help >/dev/null

print "== Swift build/test =="
swift build
swift test
swift run sttUnitChecks

if [[ -x "python/.venv/bin/python" ]]; then
  print "== Python tests =="
  python/.venv/bin/python -m pytest python/tests

  print "== Python backend status CLI =="
  ./scripts/check-python-backend.sh --backend python --python python/.venv/bin/python --json | python3 -m json.tool >/dev/null
  STATUS_OUT="${SMOKE_DIR}/stt-status-check.out"
  STATUS_ERR="${SMOKE_DIR}/stt-status-check.err"
  mkdir -p "${SMOKE_DIR}"
  set +e
  ./scripts/check-python-backend.sh --backend python --python python/.venv/bin/python --strict >"${STATUS_OUT}" 2>"${STATUS_ERR}"
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

print "== Custom bundle ID smoke test =="
CUSTOM_BUNDLE_ID="com.hashicorp.stt.validate"
CUSTOM_DIST_DIR="${SMOKE_DIR}/dist-custom"
CUSTOM_APP_BIN="${CUSTOM_DIST_DIR}/stt.app/Contents/MacOS/stt"
DIST_DIR="${CUSTOM_DIST_DIR}" BUNDLE_ID="${CUSTOM_BUNDLE_ID}" ./scripts/build-app-bundle.sh >/dev/null
BUNDLE_ID="${CUSTOM_BUNDLE_ID}" ./scripts/check-app-bundle.sh "${CUSTOM_DIST_DIR}/stt.app"
CUSTOM_DOCTOR_OUTPUT="$(${CUSTOM_APP_BIN} doctor)"
print "${CUSTOM_DOCTOR_OUTPUT}" | grep -q "Bundle identifier: ${CUSTOM_BUNDLE_ID}"
CUSTOM_PERMISSIONS_OUTPUT="$(${CUSTOM_APP_BIN} permissions reset-help --bundle-id "${CUSTOM_BUNDLE_ID}")"
print "${CUSTOM_PERMISSIONS_OUTPUT}" | grep -q "Expected bundle identifier: ${CUSTOM_BUNDLE_ID}"
print "${CUSTOM_PERMISSIONS_OUTPUT}" | grep -q "tccutil reset Microphone ${CUSTOM_BUNDLE_ID}"
print "${CUSTOM_PERMISSIONS_OUTPUT}" | grep -q "tccutil reset AudioCapture ${CUSTOM_BUNDLE_ID}"
print "${CUSTOM_PERMISSIONS_OUTPUT}" | grep -q "tccutil reset ScreenCapture ${CUSTOM_BUNDLE_ID}"

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

print "== Invalid python-backend override fails fast =="
INVALID_BACKEND="${SMOKE_DIR}/definitely-missing-stt-backend-$(date +%Y%m%d%H%M%S)-$$"
if [[ -e "${INVALID_BACKEND}" ]]; then
  print -u2 "error: invalid-backend smoke path unexpectedly exists: ${INVALID_BACKEND}"
  exit 1
fi
INVALID_BACKEND_DIR="${SMOKE_DIR}/invalid-backend-override"
mkdir -p "${INVALID_BACKEND_DIR}"

set +e
"${APP_BIN}" doctor --python-backend "${INVALID_BACKEND}" >"${INVALID_BACKEND_DIR}/doctor.out" 2>&1
INVALID_DOCTOR_EXIT=$?
"${APP_BIN}" transcribe /tmp/definitely-missing-stt-audio.wav --python-backend "${INVALID_BACKEND}" >"${INVALID_BACKEND_DIR}/transcribe.out" 2>&1
INVALID_TRANSCRIBE_EXIT=$?
INVALID_PIPE_HOME="${INVALID_BACKEND_DIR}/pipeline-home"
mkdir -p "${INVALID_PIPE_HOME}"
STT_HOME="${INVALID_PIPE_HOME}" "${APP_BIN}" pipeline --mode mic --duration 1 --python-backend "${INVALID_BACKEND}" >"${INVALID_BACKEND_DIR}/pipeline.out" 2>&1
INVALID_PIPE_EXIT=$?
set -e

if [[ ${INVALID_DOCTOR_EXIT} -eq 0 ]]; then
  print -u2 "error: doctor accepted invalid --python-backend override"
  head -80 "${INVALID_BACKEND_DIR}/doctor.out" >&2 || true
  exit 1
fi
if ! grep -q -- "--python-backend must point to an existing directory" "${INVALID_BACKEND_DIR}/doctor.out"; then
  print -u2 "error: doctor invalid-backend output did not include expected message"
  head -80 "${INVALID_BACKEND_DIR}/doctor.out" >&2 || true
  exit 1
fi

if [[ ${INVALID_TRANSCRIBE_EXIT} -eq 0 ]]; then
  print -u2 "error: transcribe accepted invalid --python-backend override"
  head -80 "${INVALID_BACKEND_DIR}/transcribe.out" >&2 || true
  exit 1
fi
if ! grep -q -- "--python-backend must point to an existing directory" "${INVALID_BACKEND_DIR}/transcribe.out"; then
  print -u2 "error: transcribe invalid-backend output did not include expected message"
  head -80 "${INVALID_BACKEND_DIR}/transcribe.out" >&2 || true
  exit 1
fi

if [[ ${INVALID_PIPE_EXIT} -eq 0 ]]; then
  print -u2 "error: pipeline accepted invalid --python-backend override"
  head -80 "${INVALID_BACKEND_DIR}/pipeline.out" >&2 || true
  exit 1
fi
if ! grep -q -- "--python-backend must point to an existing directory" "${INVALID_BACKEND_DIR}/pipeline.out"; then
  print -u2 "error: pipeline invalid-backend output did not include expected message"
  head -80 "${INVALID_BACKEND_DIR}/pipeline.out" >&2 || true
  exit 1
fi
INVALID_BACKEND_METADATA="$(find "${INVALID_PIPE_HOME}" -maxdepth 4 -name metadata.json -type f | head -1)"
if [[ -n "${INVALID_BACKEND_METADATA}" ]]; then
  print -u2 "error: invalid-backend pipeline wrote metadata despite failing backend preflight: ${INVALID_BACKEND_METADATA}"
  exit 1
fi

MEETING_MISSING_DEVICE="definitely-missing-stt-meeting-device"
if [[ "${SKIP_MIC_HARDWARE}" == "1" ]]; then
  print "== Finite mic smoke test skipped (STT_SKIP_MIC_HARDWARE=1) =="
  print "== Optional system fallback smoke test skipped (STT_SKIP_MIC_HARDWARE=1) =="
  print "== Meeting recording missing-device smoke test skipped (STT_SKIP_MIC_HARDWARE=1) =="
else
  print "== Finite mic smoke test =="
  mkdir -p "${SMOKE_DIR}"
  "${APP_BIN}" record --mode mic --duration 2 --fail-if-empty --output "${MIC_WAV}"
  ls -lh "${MIC_WAV}"
  ffprobe -v error -show_entries format=duration,size -of default=nw=1 "${MIC_WAV}"
  assert_audio_file_has_payload "${MIC_WAV}" "mic smoke test" "Check microphone permission and selected input device."

  run_optional_system_fallback_smoke \
    "${APP_BIN}" \
    "${SMOKE_DIR}" \
    "Optional system fallback smoke test" \
    "Check that ${STT_SYSTEM_DEVICE:-the selected device} is receiving routed system audio before running this optional check."

  print "== Meeting recording missing-device smoke test =="
  MEETING_MISSING_DIR="${SMOKE_DIR}/meeting-missing-device-$(date +%Y%m%d%H%M%S)"
  mkdir -p "${MEETING_MISSING_DIR}"
  set +e
  "${APP_BIN}" record --mode meeting --input-device "${MEETING_MISSING_DEVICE}" --duration 1 --output-dir "${MEETING_MISSING_DIR}/recording" >"${MEETING_MISSING_DIR}/stdout.txt" 2>"${MEETING_MISSING_DIR}/stderr.txt"
  MEETING_MISSING_EXIT=$?
  set -e
  if [[ ${MEETING_MISSING_EXIT} -eq 0 ]]; then
    print -u2 "error: meeting recording succeeded despite missing system fallback device"
    head -80 "${MEETING_MISSING_DIR}/stdout.txt" >&2 || true
    head -80 "${MEETING_MISSING_DIR}/stderr.txt" >&2 || true
    exit 1
  fi
  if ! grep -q "No input device found matching \"${MEETING_MISSING_DEVICE}\"" "${MEETING_MISSING_DIR}/stderr.txt" "${MEETING_MISSING_DIR}/stdout.txt"; then
    print -u2 "error: meeting missing-device output did not include expected device guidance"
    head -80 "${MEETING_MISSING_DIR}/stdout.txt" >&2 || true
    head -80 "${MEETING_MISSING_DIR}/stderr.txt" >&2 || true
    exit 1
  fi
fi

print "== Standalone WAV mix smoke test =="
MIX_SMOKE_DIR="${SMOKE_DIR}/mix-smoke"
mkdir -p "${MIX_SMOKE_DIR}"
python3 - "${MIX_SMOKE_DIR}" <<'PY'
import struct
import sys
import wave
from pathlib import Path

out = Path(sys.argv[1])
fixtures = {
    "mic.wav": [1000, 2000, -3000] + [0] * 2997,
    "system.wav": [3000, -1000] + [0] * 2998,
}
for name, samples in fixtures.items():
    with wave.open(str(out / name), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"".join(struct.pack("<h", s) for s in samples))
PY
"${APP_BIN}" mix \
  "${MIX_SMOKE_DIR}/mic.wav" \
  "${MIX_SMOKE_DIR}/system.wav" \
  --output "${MIX_SMOKE_DIR}/mixed.wav" \
  --fail-if-empty
ffprobe -v error -show_entries format=duration,size -of default=nw=1 "${MIX_SMOKE_DIR}/mixed.wav"
python3 - "${MIX_SMOKE_DIR}/mixed.wav" <<'PY'
import struct
import sys
import wave
from pathlib import Path

with wave.open(str(Path(sys.argv[1])), "rb") as wav:
    assert wav.getnchannels() == 1
    assert wav.getsampwidth() == 2
    assert wav.getframerate() == 8000
    frames = wav.readframes(wav.getnframes())
actual = list(struct.unpack("<" + "h" * (len(frames) // 2), frames))
if len(actual) != 3000:
    raise SystemExit(f"unexpected mixed sample count: {len(actual)}")
expected_prefix = [4000, 1000, -3000]
if actual[:3] != expected_prefix:
    raise SystemExit(f"unexpected mixed prefix: {actual[:3]} != {expected_prefix}")
if any(actual[3:]):
    raise SystemExit("expected trailing mixed samples to be silence")
PY

print "== Synthetic WAV fixture for non-capture tests =="
python3 - "${FIXTURE_WAV}" <<'PY'
import math
import struct
import sys
import wave
from pathlib import Path

path = Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
sample_rate = 8000
samples = [int(1000 * math.sin(2 * math.pi * 440 * i / sample_rate)) for i in range(sample_rate // 2)]
with wave.open(str(path), "wb") as wav:
    wav.setnchannels(1)
    wav.setsampwidth(2)
    wav.setframerate(sample_rate)
    wav.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))
PY
ffprobe -v error -show_entries format=duration,size -of default=nw=1 "${FIXTURE_WAV}"
assert_audio_file_has_payload "${FIXTURE_WAV}" "synthetic WAV fixture" "The generated fixture should contain PCM audio samples."

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

if [[ "${SKIP_MIC_HARDWARE}" == "1" ]]; then
  print "== Pipeline metadata smoke test skipped (STT_SKIP_MIC_HARDWARE=1) =="
else
  print "== Pipeline metadata smoke test =="
  PIPE_HOME="${SMOKE_DIR}/pipeline-home-$(date +%Y%m%d%H%M%S)"
  mkdir -p "${PIPE_HOME}"
  set +e
  STT_HOME="${PIPE_HOME}" "${APP_BIN}" pipeline --mode mic --name smoke --duration 1 --fail-if-empty --transcribe-timeout 5 --device cpu >"${PIPE_HOME}/stdout.txt" 2>"${PIPE_HOME}/stderr.txt"
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
fi

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
"${APP_BIN}" transcribe "${FIXTURE_WAV}" \
  --output "${FAKE_TRANSCRIBE_DIR}/transcript.txt" \
  --json "${FAKE_TRANSCRIBE_DIR}/transcript.json" \
  --device cpu \
  --python-backend "${FAKE_BACKEND}" \
  >"${FAKE_TRANSCRIBE_DIR}/stdout.txt" \
  2>"${FAKE_TRANSCRIBE_DIR}/stderr.txt"
python3 -m json.tool "${FAKE_TRANSCRIBE_DIR}/transcript.json" >/dev/null
grep -q "fake transcript" "${FAKE_TRANSCRIBE_DIR}/transcript.txt"
grep -q "fake transcript" "${FAKE_TRANSCRIBE_DIR}/stdout.txt"

if [[ "${SKIP_MIC_HARDWARE}" == "1" ]]; then
  print "== Fake-backend pipeline smoke tests skipped (STT_SKIP_MIC_HARDWARE=1) =="
else
  print "== Successful pipeline smoke test with fake backend =="
  FAKE_PIPE_HOME="${SMOKE_DIR}/pipeline-success-home-$(date +%Y%m%d%H%M%S)"
mkdir -p "${FAKE_PIPE_HOME}"
STT_HOME="${FAKE_PIPE_HOME}" "${APP_BIN}" pipeline --mode mic --name success --duration 1 --fail-if-empty --transcribe-timeout 5 --device cpu --python-backend "${FAKE_BACKEND}" >"${FAKE_PIPE_HOME}/stdout.txt" 2>"${FAKE_PIPE_HOME}/stderr.txt"
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
if payload.get("transcribedAudioPath") != payload["outputPaths"][0]:
    raise SystemExit(f"unexpected transcribedAudioPath: {payload.get('transcribedAudioPath')} != {payload['outputPaths'][0]}")
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

print "== Failing meeting pipeline missing-device smoke test =="
MEETING_FAIL_PIPE_HOME="${SMOKE_DIR}/pipeline-meeting-missing-device-home-$(date +%Y%m%d%H%M%S)"
mkdir -p "${MEETING_FAIL_PIPE_HOME}"
set +e
STT_HOME="${MEETING_FAIL_PIPE_HOME}" "${APP_BIN}" pipeline --mode meeting --name meeting-fail --duration 1 --input-device "${MEETING_MISSING_DEVICE}" --transcribe-timeout 5 --device cpu --python-backend "${FAKE_BACKEND}" >"${MEETING_FAIL_PIPE_HOME}/stdout.txt" 2>"${MEETING_FAIL_PIPE_HOME}/stderr.txt"
MEETING_FAIL_PIPE_EXIT=$?
set -e
if [[ ${MEETING_FAIL_PIPE_EXIT} -eq 0 ]]; then
  print -u2 "error: meeting pipeline succeeded despite missing system fallback device"
  head -80 "${MEETING_FAIL_PIPE_HOME}/stdout.txt" >&2 || true
  head -80 "${MEETING_FAIL_PIPE_HOME}/stderr.txt" >&2 || true
  exit 1
fi
MEETING_FAIL_METADATA_PATH="$(find "${MEETING_FAIL_PIPE_HOME}" -maxdepth 4 -name metadata.json -type f | head -1)"
if [[ -z "${MEETING_FAIL_METADATA_PATH}" ]]; then
  print -u2 "error: failing meeting pipeline did not write metadata.json"
  head -80 "${MEETING_FAIL_PIPE_HOME}/stdout.txt" >&2 || true
  head -80 "${MEETING_FAIL_PIPE_HOME}/stderr.txt" >&2 || true
  exit 1
fi
validate_metadata "${MEETING_FAIL_METADATA_PATH}"
python3 - "${MEETING_FAIL_METADATA_PATH}" "${MEETING_MISSING_DEVICE}" <<'PY'
import json
import sys
from pathlib import Path

metadata_path = Path(sys.argv[1])
missing_device = sys.argv[2]
payload = json.loads(metadata_path.read_text())
notes = payload.get("notes") or ""
if not notes.startswith("Pipeline failed:"):
    raise SystemExit(f"expected Pipeline failed notes, got: {notes!r}")
if f"No input device found matching \"{missing_device}\"" not in notes:
    raise SystemExit(f"expected missing-device note, got: {notes!r}")
transcribed_audio_path = payload.get("transcribedAudioPath")
if not isinstance(transcribed_audio_path, str) or not transcribed_audio_path.endswith("mic.wav"):
    raise SystemExit(f"expected intended mic.wav transcribedAudioPath, got: {transcribed_audio_path!r}")
for key in ("transcriptTextPath", "transcriptJSONPath"):
    path_value = payload.get(key)
    if path_value and Path(path_value).exists():
        raise SystemExit(f"unexpected transcript output exists for meeting recording failure: {path_value}")
PY
print "Failing meeting pipeline metadata: ${MEETING_FAIL_METADATA_PATH}"

print "== Failing pipeline smoke test with fake backend =="
FAILING_BACKEND="${SMOKE_DIR}/fake-backend-failing"
mkdir -p "${FAILING_BACKEND}/stt_vibevoice"
touch "${FAILING_BACKEND}/stt_vibevoice/__init__.py"
cat >"${FAILING_BACKEND}/stt_vibevoice/transcribe.py" <<'PY'
from __future__ import annotations

import sys

print("fake backend intentional failure", file=sys.stderr)
raise SystemExit(7)
PY
FAILING_PIPE_HOME="${SMOKE_DIR}/pipeline-failing-home-$(date +%Y%m%d%H%M%S)"
mkdir -p "${FAILING_PIPE_HOME}"
set +e
STT_HOME="${FAILING_PIPE_HOME}" "${APP_BIN}" pipeline --mode mic --name failing --duration 1 --fail-if-empty --transcribe-timeout 5 --device cpu --python-backend "${FAILING_BACKEND}" >"${FAILING_PIPE_HOME}/stdout.txt" 2>"${FAILING_PIPE_HOME}/stderr.txt"
FAILING_PIPE_EXIT=$?
set -e
if [[ ${FAILING_PIPE_EXIT} -eq 0 ]]; then
  print -u2 "error: failing fake-backend pipeline unexpectedly succeeded"
  head -80 "${FAILING_PIPE_HOME}/stdout.txt" >&2 || true
  head -80 "${FAILING_PIPE_HOME}/stderr.txt" >&2 || true
  exit 1
fi
FAILING_METADATA_PATH="$(find "${FAILING_PIPE_HOME}" -maxdepth 4 -name metadata.json -type f | head -1)"
if [[ -z "${FAILING_METADATA_PATH}" ]]; then
  print -u2 "error: failing fake-backend pipeline did not write metadata.json"
  head -80 "${FAILING_PIPE_HOME}/stdout.txt" >&2 || true
  head -80 "${FAILING_PIPE_HOME}/stderr.txt" >&2 || true
  exit 1
fi
validate_metadata "${FAILING_METADATA_PATH}"
python3 - "${FAILING_METADATA_PATH}" <<'PY'
import json
import sys
from pathlib import Path

metadata_path = Path(sys.argv[1])
payload = json.loads(metadata_path.read_text())
notes = payload.get("notes") or ""
if not notes.startswith("Pipeline failed:"):
    raise SystemExit(f"expected Pipeline failed notes, got: {notes!r}")
for key in ("transcriptTextPath", "transcriptJSONPath"):
    path_value = payload.get(key)
    if path_value and Path(path_value).exists():
        raise SystemExit(f"unexpected transcript output exists for failing backend: {path_value}")
PY
print "Failing fake-backend pipeline metadata: ${FAILING_METADATA_PATH}"

print "== Timeout pipeline smoke test with fake backend =="
TIMEOUT_BACKEND="${SMOKE_DIR}/fake-backend-timeout"
mkdir -p "${TIMEOUT_BACKEND}/stt_vibevoice"
touch "${TIMEOUT_BACKEND}/stt_vibevoice/__init__.py"
cat >"${TIMEOUT_BACKEND}/stt_vibevoice/transcribe.py" <<'PY'
from __future__ import annotations

import signal
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
print("fake backend sleeping past timeout", flush=True)
time.sleep(30)
PY
TIMEOUT_PIPE_HOME="${SMOKE_DIR}/pipeline-timeout-home-$(date +%Y%m%d%H%M%S)"
mkdir -p "${TIMEOUT_PIPE_HOME}"
TIMEOUT_STARTED_AT="$(date +%s)"
set +e
STT_HOME="${TIMEOUT_PIPE_HOME}" "${APP_BIN}" pipeline --mode mic --name timeout --duration 1 --fail-if-empty --transcribe-timeout 2 --device cpu --python-backend "${TIMEOUT_BACKEND}" >"${TIMEOUT_PIPE_HOME}/stdout.txt" 2>"${TIMEOUT_PIPE_HOME}/stderr.txt"
TIMEOUT_PIPE_EXIT=$?
set -e
TIMEOUT_ELAPSED=$(( $(date +%s) - TIMEOUT_STARTED_AT ))
if [[ ${TIMEOUT_PIPE_EXIT} -eq 0 ]]; then
  print -u2 "error: timeout fake-backend pipeline unexpectedly succeeded"
  head -80 "${TIMEOUT_PIPE_HOME}/stdout.txt" >&2 || true
  head -80 "${TIMEOUT_PIPE_HOME}/stderr.txt" >&2 || true
  exit 1
fi
if [[ ${TIMEOUT_ELAPSED} -gt 12 ]]; then
  print -u2 "error: timeout fake-backend pipeline took too long (${TIMEOUT_ELAPSED}s)"
  head -80 "${TIMEOUT_PIPE_HOME}/stdout.txt" >&2 || true
  head -80 "${TIMEOUT_PIPE_HOME}/stderr.txt" >&2 || true
  exit 1
fi
TIMEOUT_METADATA_PATH="$(find "${TIMEOUT_PIPE_HOME}" -maxdepth 4 -name metadata.json -type f | head -1)"
if [[ -z "${TIMEOUT_METADATA_PATH}" ]]; then
  print -u2 "error: timeout fake-backend pipeline did not write metadata.json"
  head -80 "${TIMEOUT_PIPE_HOME}/stdout.txt" >&2 || true
  head -80 "${TIMEOUT_PIPE_HOME}/stderr.txt" >&2 || true
  exit 1
fi
validate_metadata "${TIMEOUT_METADATA_PATH}"
python3 - "${TIMEOUT_METADATA_PATH}" <<'PY'
import json
import sys
from pathlib import Path

metadata_path = Path(sys.argv[1])
payload = json.loads(metadata_path.read_text())
notes = payload.get("notes") or ""
if "Python transcription backend timed out" not in notes:
    raise SystemExit(f"expected transcription timeout notes, got: {notes!r}")
if "--timeout/--transcribe-timeout" not in notes:
    raise SystemExit(f"expected timeout recovery guidance in notes, got: {notes!r}")
PY
print "Timeout fake-backend pipeline metadata: ${TIMEOUT_METADATA_PATH} (${TIMEOUT_ELAPSED}s)"
fi

print "Validation complete. Smoke artifacts: ${SMOKE_DIR}"
