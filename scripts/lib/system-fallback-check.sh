#!/usr/bin/env zsh

# Shared audio smoke helpers.
#
# The system fallback check is opt-in via STT_SYSTEM_DEVICE because it only
# makes sense after the user routes real audio into a virtual/aggregate input
# device such as BlackHole. These helpers intentionally fail on
# header-only/suspiciously-small output so routing/permission mistakes do not
# masquerade as successful capture.

assert_audio_file_has_payload() {
  local audio_path="$1"
  local context="${2:-audio smoke test}"
  local hint="${3:-Check permissions, selected device, and audio routing.}"

  local size
  size="$(stat -f%z "${audio_path}")"
  if [[ "${size}" -le 4096 ]]; then
    print -u2 "error: ${context} produced header-only/suspiciously small file (${size} bytes)"
    print -u2 "${hint}"
    exit 1
  fi
}

run_optional_system_fallback_smoke() {
  local app_bin="$1"
  local smoke_dir="$2"
  local label="${3:-Optional system fallback smoke test}"
  local routing_hint="${4:-Check that ${STT_SYSTEM_DEVICE:-the selected device} is receiving routed system audio.}"

  if [[ -n "${STT_SYSTEM_DEVICE:-}" ]]; then
    print "== ${label} (${STT_SYSTEM_DEVICE}) =="
    local system_wav="${smoke_dir}/system.wav"
    "${app_bin}" record --mode system --input-device "${STT_SYSTEM_DEVICE}" --duration 2 --output "${system_wav}"
    ls -lh "${system_wav}"
    ffprobe -v error -show_entries format=duration,size -of default=nw=1 "${system_wav}" || true
    assert_audio_file_has_payload "${system_wav}" "system fallback smoke test" "${routing_hint}"
  else
    print "== ${label} skipped =="
    print "Set STT_SYSTEM_DEVICE (for example, 'BlackHole 2ch') after routing audio to validate system fallback capture."
  fi
}
