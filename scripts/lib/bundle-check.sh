#!/usr/bin/env zsh

# Shared static checks for the generated app bundle. These checks verify the
# metadata TCC depends on, not just that codesign says the bundle is valid.

assert_plist_key_non_empty() {
  local plist_path="$1"
  local key="$2"
  local value
  value="$(/usr/libexec/PlistBuddy -c "Print :${key}" "${plist_path}" 2>/dev/null || true)"
  if [[ -z "${value}" ]]; then
    print -u2 "error: ${plist_path} is missing non-empty ${key}"
    exit 1
  fi
}

assert_app_bundle_tcc_configuration() {
  local app_dir="$1"
  local expected_bundle_id="$2"
  local plist_path="${app_dir}/Contents/Info.plist"

  if [[ ! -d "${app_dir}" ]]; then
    print -u2 "error: app bundle not found: ${app_dir}"
    exit 1
  fi
  if [[ ! -f "${plist_path}" ]]; then
    print -u2 "error: Info.plist not found: ${plist_path}"
    exit 1
  fi

  local actual_bundle_id
  actual_bundle_id="$(/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "${plist_path}" 2>/dev/null || true)"
  if [[ "${actual_bundle_id}" != "${expected_bundle_id}" ]]; then
    print -u2 "error: CFBundleIdentifier mismatch: expected ${expected_bundle_id}, got ${actual_bundle_id:-<missing>}"
    exit 1
  fi

  assert_plist_key_non_empty "${plist_path}" "NSMicrophoneUsageDescription"
  assert_plist_key_non_empty "${plist_path}" "NSAudioCaptureUsageDescription"
  assert_plist_key_non_empty "${plist_path}" "NSScreenCaptureUsageDescription"

  local entitlements
  entitlements="$(codesign -d --entitlements :- "${app_dir}" 2>/dev/null || true)"
  if [[ -z "${entitlements}" ]]; then
    print -u2 "error: no codesign entitlements found for ${app_dir}"
    exit 1
  fi
  if ! print "${entitlements}" | grep -q "com.apple.security.device.audio-input"; then
    print -u2 "error: missing com.apple.security.device.audio-input entitlement"
    exit 1
  fi
  if ! print "${entitlements}" | grep -A1 "com.apple.security.device.audio-input" | grep -q "<true/>"; then
    print -u2 "error: com.apple.security.device.audio-input entitlement is not true"
    exit 1
  fi
}
