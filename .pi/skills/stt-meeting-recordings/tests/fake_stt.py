#!/usr/bin/env python3
"""Minimal stand-in for the real `stt` CLI, used only by the test suite.

Understands just enough of `stt transcribe` / `stt transcribe-meeting` to let
recordings.py's transcribe() flow run end-to-end against a fake binary:

- Finds --output / --json among its arguments.
- Optionally sleeps (FAKE_STT_SLEEP) so tests can observe the parent's
  in-flight active-transcription state before the child exits.
- Optionally logs every invocation's argv to FAKE_STT_INVOCATION_LOG (one
  JSON array per line) so tests can assert stt was/was not invoked again.
- Optionally exits non-zero (FAKE_STT_EXIT_CODE) to exercise the failure path.
- Optionally skips writing output (FAKE_STT_NO_WRITE=1) to exercise the
  interrupted/no-final-artifact path.
- Optionally writes a schema-invalid transcript.json (FAKE_STT_MALFORMED_JSON=1)
  while still exiting 0, to exercise the zero-exit-but-invalid-output
  postcondition failure path.
- Optionally writes an empty transcript.md (FAKE_STT_EMPTY_MD=1) while
  still exiting 0, to exercise the empty-markdown postcondition failure path.
"""
from __future__ import annotations

import json
import os
import sys
import time


def get_opt(argv: list[str], name: str) -> str | None:
    if name in argv:
        idx = argv.index(name)
        if idx + 1 < len(argv):
            return argv[idx + 1]
    return None


def main() -> int:
    argv = sys.argv[1:]

    invocation_log = os.environ.get("FAKE_STT_INVOCATION_LOG")
    if invocation_log:
        with open(invocation_log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(argv) + "\n")

    sleep_s = float(os.environ.get("FAKE_STT_SLEEP", "0") or "0")
    if sleep_s > 0:
        time.sleep(sleep_s)

    exit_code = int(os.environ.get("FAKE_STT_EXIT_CODE", "0") or "0")
    if exit_code != 0:
        return exit_code

    if os.environ.get("FAKE_STT_NO_WRITE") == "1":
        return 0

    output = get_opt(argv, "--output")
    json_out = get_opt(argv, "--json")
    empty_md = os.environ.get("FAKE_STT_EMPTY_MD") == "1"
    malformed_json = os.environ.get("FAKE_STT_MALFORMED_JSON") == "1"
    if output:
        with open(output, "w", encoding="utf-8") as fh:
            if not empty_md:
                fh.write("# Fake transcript\n\nSpeaker 0: hello world\n")
    if json_out:
        with open(json_out, "w", encoding="utf-8") as fh:
            if malformed_json:
                json.dump({"not_segments": "oops"}, fh)
            else:
                json.dump({"segments": [{"speaker_id": 0, "text": "hello world"}]}, fh)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
