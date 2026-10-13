#!/usr/bin/env python3
"""Regenerate transcript.md (and text/diarised_text in transcript.json) from the diarised JSON.

After `stt diarize` or `name_one_speaker.py relabel` updates transcript.json,
the markdown and the JSON's text fields can go stale or lose the metadata
header (relabel rewrites transcript.md body-only). This script recomputes
`text`/`diarised_text` from the segments (speaker_name wins over speaker_id,
mirroring the helper's render), rewrites transcript.md line-by-line as
`[start - end] <name|Speaker id>: text`, strips the `Unknown ` source-label
prefix (single-source sessions have no `source` field), and optionally
restores the metadata header.

Usage:
    python3 regenerate_transcript_md.py --session <session-dir> [--header]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STT_PYTHON = Path(__file__).resolve().parents[3] / "python"


def render(segments: list[dict[str, Any]]) -> str:
    """Mirror name_one_speaker.render_transcript_text (speaker_name wins)."""
    lines: list[str] = []
    for seg in segments:
        name = seg.get("speaker_name")
        sid = seg.get("speaker_id")
        speaker = f" {name}" if name else (f" Speaker {sid}" if sid is not None else "")
        start = seg.get("start_time", seg.get("start"))
        end = seg.get("end_time", seg.get("end"))
        text = (seg.get("text") or "").strip()
        lines.append(f"[{float(start):.2f} - {float(end):.2f}]{speaker}: {text}")
    return "\n".join(lines) + "\n"


def header_lines(data: dict[str, Any]) -> list[str]:
    audio = Path(data.get("audio_file", "episode.wav"))
    return [
        f"Session: {audio.stem}",
        f"File: {audio.name}",
        f"Date: {datetime.now(timezone.utc).isoformat()}",
        f"Transcription backend: {data.get('backend')}",
        f"Device: {data.get('device')}",
        f"Model: {data.get('model_path')}",
        f"Duration (s): {data.get('duration_seconds')}",
        "=" * 60,
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", required=True, help="Session directory")
    ap.add_argument("--header", action="store_true", help="Prepend the metadata header block")
    args = ap.parse_args()

    session = Path(args.session).expanduser().resolve()
    jp, mp = session / "transcript.json", session / "transcript.md"
    if not jp.exists():
        raise SystemExit(f"No transcript.json in {session}")

    data = json.loads(jp.read_text(encoding="utf-8"))
    segments = data.get("segments") or []
    if not segments:
        raise SystemExit("transcript.json has no segments")

    # Single source of truth: the rendered body (names win, like the helper).
    body = render(segments).replace("] Unknown ", "] ")
    data["text"] = body.strip()
    data["diarised_text"] = body.strip()
    data["generated_at"] = datetime.now(timezone.utc).isoformat()
    jp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.header:
        body = "\n".join(header_lines(data)) + "\n" + body
    mp.write_text(body, encoding="utf-8")
    print(f"Wrote {mp} ({len(segments)} segments)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
