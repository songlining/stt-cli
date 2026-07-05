#!/usr/bin/env zsh
set -euo pipefail

# Static checks for a built stt.app bundle. This does not run the app or touch
# TCC; it verifies Info.plist usage descriptions and signing entitlements that
# macOS uses when presenting/attributing permission prompts.

SCRIPT_DIR="${0:A:h}"
REPO_ROOT="${SCRIPT_DIR:h}"
APP_DIR="${1:-${REPO_ROOT}/dist/stt.app}"
EXPECTED_BUNDLE_ID="${BUNDLE_ID:-com.larrysong.stt}"

source "${REPO_ROOT}/scripts/lib/bundle-check.sh"

assert_app_bundle_tcc_configuration "${APP_DIR}" "${EXPECTED_BUNDLE_ID}"
print "App bundle TCC metadata OK: ${APP_DIR} (${EXPECTED_BUNDLE_ID})"
