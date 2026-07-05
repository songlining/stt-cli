#!/usr/bin/env zsh
set -euo pipefail

# Build a minimal macOS .app wrapper around the Swift CLI so macOS TCC
# permissions are attributed to stt's bundle identifier rather than to the
# parent terminal app. This is a local-development wrapper; distribution builds
# still need Developer ID signing, hardened runtime, notarization, and stapling.

SCRIPT_DIR="${0:A:h}"
REPO_ROOT="${SCRIPT_DIR:h}"
CONFIGURATION="${CONFIGURATION:-debug}"
BUNDLE_ID="${BUNDLE_ID:-com.larrysong.stt}"
APP_NAME="${APP_NAME:-stt}"
DIST_DIR="${DIST_DIR:-${REPO_ROOT}/dist}"
CODESIGN_IDENTITY="${CODESIGN_IDENTITY:--}"
HARDENED_RUNTIME="${HARDENED_RUNTIME:-0}"
APP_DIR="${DIST_DIR}/${APP_NAME}.app"
CONTENTS_DIR="${APP_DIR}/Contents"
MACOS_DIR="${CONTENTS_DIR}/MacOS"
RESOURCES_DIR="${CONTENTS_DIR}/Resources"
PLIST_TEMPLATE="${REPO_ROOT}/Resources/stt-app/Info.plist"
ENTITLEMENTS="${REPO_ROOT}/Resources/stt-app/stt.entitlements"

cd "${REPO_ROOT}"

swift build -c "${CONFIGURATION}"

BUILT_PRODUCTS_DIR="$(swift build -c "${CONFIGURATION}" --show-bin-path)"
STT_BINARY="${BUILT_PRODUCTS_DIR}/stt"
if [[ ! -x "${STT_BINARY}" ]]; then
  print -u2 "error: built stt binary not found at ${STT_BINARY}"
  exit 1
fi

mkdir -p "${MACOS_DIR}" "${RESOURCES_DIR}"
cp "${STT_BINARY}" "${MACOS_DIR}/stt"
chmod 755 "${MACOS_DIR}/stt"
cp "${PLIST_TEMPLATE}" "${CONTENTS_DIR}/Info.plist"

# Package the lightweight Python module alongside the app wrapper so `stt
# doctor`, `stt transcribe`, and `stt pipeline` can find it even when launched
# outside the repo root. Dependencies still come from the selected Python
# environment; this only packages the local stt_vibevoice code.
mkdir -p "${RESOURCES_DIR}/python"
ditto "${REPO_ROOT}/python/stt_vibevoice" "${RESOURCES_DIR}/python/stt_vibevoice"
cp "${REPO_ROOT}/python/pyproject.toml" "${RESOURCES_DIR}/python/pyproject.toml"

# Keep bundle ID overrideable without requiring another template file.
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier ${BUNDLE_ID}" "${CONTENTS_DIR}/Info.plist"

if command -v codesign >/dev/null 2>&1; then
  codesign_args=(--force --sign "${CODESIGN_IDENTITY}" --entitlements "${ENTITLEMENTS}")
  if [[ "${HARDENED_RUNTIME}" == "1" ]]; then
    codesign_args+=(--options runtime)
  fi
  codesign "${codesign_args[@]}" "${APP_DIR}" >/dev/null
  if [[ "${CODESIGN_IDENTITY}" == "-" ]]; then
    print "Ad-hoc signed ${APP_DIR} with ${ENTITLEMENTS}"
  else
    print "Signed ${APP_DIR} with ${CODESIGN_IDENTITY} (hardened runtime: ${HARDENED_RUNTIME})"
  fi
else
  print -u2 "warning: codesign not found; app bundle was created unsigned"
fi

print "Built ${APP_DIR}"
print "Smoke test: ${APP_DIR}/Contents/MacOS/stt doctor"
