#!/usr/bin/env zsh
set -euo pipefail

# Static/runtime readiness check for the Python transcription backend. This is a
# focused wrapper around `python -m stt_vibevoice.status` so users do not need
# to remember PYTHONPATH or which venv to use.

SCRIPT_DIR="${0:A:h}"
REPO_ROOT="${SCRIPT_DIR:h}"
BACKEND_DIR="${STT_PYTHON_BACKEND:-${REPO_ROOT}/python}"
PYTHON_BIN="${PYTHON_BIN:-}"
OUTPUT_JSON=0
STRICT=0

usage() {
  cat <<'EOF'
Usage: scripts/check-python-backend.sh [options]

Options:
  --backend <dir>      Backend directory containing stt_vibevoice (default: $STT_PYTHON_BACKEND or ./python)
  --python <path>      Python executable to use (default: <backend>/.venv/bin/python, then python3)
  --json               Print structured JSON status
  --strict             Exit non-zero if backend dependencies are not ready
  -h, --help           Show this help

Examples:
  ./scripts/check-python-backend.sh
  ./scripts/check-python-backend.sh --json
  ./scripts/check-python-backend.sh --strict
  ./scripts/check-python-backend.sh --backend ./python --python python/.venv/bin/python
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backend)
      BACKEND_DIR="$2"
      shift 2
      ;;
    --python)
      PYTHON_BIN="$2"
      shift 2
      ;;
    --json)
      OUTPUT_JSON=1
      shift
      ;;
    --strict)
      STRICT=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      print -u2 "error: unknown option: $1"
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! -d "${BACKEND_DIR}/stt_vibevoice" ]]; then
  print -u2 "error: backend directory does not contain stt_vibevoice: ${BACKEND_DIR}"
  exit 1
fi

if [[ -z "${PYTHON_BIN}" ]]; then
  if [[ -x "${BACKEND_DIR}/.venv/bin/python" ]]; then
    PYTHON_BIN="${BACKEND_DIR}/.venv/bin/python"
  else
    PYTHON_BIN="python3"
  fi
fi

ARGS=(-m stt_vibevoice.status)
if [[ "${OUTPUT_JSON}" == "1" ]]; then
  ARGS+=(--json)
fi
if [[ "${STRICT}" == "1" ]]; then
  ARGS+=(--fail-if-not-ready)
fi

PYTHONPATH="${BACKEND_DIR}" "${PYTHON_BIN}" "${ARGS[@]}"
