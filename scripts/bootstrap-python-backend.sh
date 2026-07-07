#!/usr/bin/env zsh
set -euo pipefail

# Create/update the Python environment used by the transcription backend.
#
# Defaults are intentionally conservative: install the local backend package
# and developer/test dependencies, but do not install MLX unless --mlx is
# passed because that can be large and network-dependent.

SCRIPT_DIR="${0:A:h}"
REPO_ROOT="${SCRIPT_DIR:h}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
RUNTIME_ROOT="${STT_VIBEVOICE_RUNTIME:-${REPO_ROOT}/python}"
INSTALL_DEV=1
INSTALL_MLX=0
INSTALL_SPEECHBRAIN=0
RUN_CHECK=0

usage() {
  cat <<'EOF'
Usage: scripts/bootstrap-python-backend.sh [options]

Options:
  --python <path>       Python interpreter to use for venv creation (default: python3 or $PYTHON_BIN)
  --runtime <dir>       Runtime root containing .venv/ (default: $STT_VIBEVOICE_RUNTIME or ./python)
  --no-dev             Do not install dev/test extras
  --mlx                Install MLX/VibeVoice transcription dependencies
  --speechbrain        Install speechbrain/torchaudio for real speaker
                       identification (requires Python 3.11 or 3.12; PyTorch
                       does not yet reliably support newer CPython releases
                       on Apple Silicon as of this writing -- pass
                       `--python python3.11` or `--python python3.12`)
  --check              Run backend readiness check after installation
  -h, --help           Show this help

Examples:
  ./scripts/bootstrap-python-backend.sh
  ./scripts/bootstrap-python-backend.sh --mlx --check
  ./scripts/bootstrap-python-backend.sh --python python3.11 --speechbrain --check
  STT_VIBEVOICE_RUNTIME=/opt/stt-runtime ./scripts/bootstrap-python-backend.sh --mlx

After running with a non-default runtime, export:
  export STT_VIBEVOICE_RUNTIME=<runtime-dir>
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      PYTHON_BIN="$2"
      shift 2
      ;;
    --runtime)
      RUNTIME_ROOT="$2"
      shift 2
      ;;
    --no-dev)
      INSTALL_DEV=0
      shift
      ;;
    --mlx)
      INSTALL_MLX=1
      shift
      ;;
    --speechbrain)
      INSTALL_SPEECHBRAIN=1
      shift
      ;;
    --check)
      RUN_CHECK=1
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

cd "${REPO_ROOT}"

VENV_DIR="${RUNTIME_ROOT}/.venv"
VENV_PYTHON="${VENV_DIR}/bin/python"

print "== Python backend bootstrap =="
print "runtime root: ${RUNTIME_ROOT}"
print "venv: ${VENV_DIR}"

if [[ ! -x "${VENV_PYTHON}" ]]; then
  print "Creating venv with ${PYTHON_BIN}"
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

"${VENV_PYTHON}" -m pip install --upgrade pip setuptools wheel

EXTRAS=()
if [[ "${INSTALL_DEV}" == "1" ]]; then
  EXTRAS+=(dev)
fi
if [[ "${INSTALL_MLX}" == "1" ]]; then
  EXTRAS+=(mlx)
fi
if [[ "${INSTALL_SPEECHBRAIN}" == "1" ]]; then
  EXTRAS+=(speechbrain)
fi

if [[ ${#EXTRAS[@]} -gt 0 ]]; then
  EXTRA_SPEC="[$(IFS=,; print "${EXTRAS[*]}")]"
else
  EXTRA_SPEC=""
fi

print "Installing local backend package: -e python${EXTRA_SPEC}"
"${VENV_PYTHON}" -m pip install -e "python${EXTRA_SPEC}"

print "Backend environment ready."
print "For non-default runtimes, run: export STT_VIBEVOICE_RUNTIME=${RUNTIME_ROOT}"

if [[ "${INSTALL_SPEECHBRAIN}" == "1" ]]; then
  py_version="$("${VENV_PYTHON}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  if [[ "${py_version}" != "3.11" && "${py_version}" != "3.12" ]]; then
    print -u2 "warning: venv interpreter is Python ${py_version}. speechbrain/torchaudio (PyTorch)"
    print -u2 "         does not reliably support this release on Apple Silicon as of this writing."
    print -u2 "         Recreate the venv with --python python3.11 or --python python3.12 if"
    print -u2 "         'pip install -e python[speechbrain]' fails."
  fi
fi

if [[ "${RUN_CHECK}" == "1" ]]; then
  print "== Backend readiness check =="
  set +e
  STT_VIBEVOICE_RUNTIME="${RUNTIME_ROOT}" PYTHONPATH="${REPO_ROOT}/python" "${VENV_PYTHON}" -m stt_vibevoice.status --fail-if-not-ready
  check_exit=$?
  set -e
  if [[ ${check_exit} -ne 0 ]]; then
    print -u2 "Backend is not fully ready yet. If MLX modules are missing, rerun with --mlx."
    exit ${check_exit}
  fi
fi
