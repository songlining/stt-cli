#!/usr/bin/env python3
"""Verify a translated transcript covers the source 1:1 (same timestamps, same order).

Translation is done segment-by-segment; the classic failure mode is a dropped or
merged segment (this happened when transcribing a 2-hour episode: one segment
was silently skipped and another over-merged). Run this after translating and
fix any gaps before reporting done.

Usage:
    python3 verify_translation.py --source transcript.md --translated transcript.zh-CN.md
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

TS_RE = re.compile(r"^\[([0-9.]+) - ([0-9.]+)\]")


def timestamps(path: Path) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = TS_RE.match(line)
        if m:
            out.append((float(m.group(1)), float(m.group(2))))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", required=True, help="Source transcript.md")
    ap.add_argument("--translated", required=True, help="Translated transcript file")
    args = ap.parse_args()

    src = timestamps(Path(args.source))
    txn = timestamps(Path(args.translated))
    if not src:
        raise SystemExit(f"No timestamped segments found in {args.source}")
    if not txn:
        raise SystemExit(f"No timestamped segments found in {args.translated}")

    missing = [t for t in src if t not in set(txn)]
    extra = [t for t in txn if t not in set(src)]
    in_order = src == txn

    print(f"source: {len(src)} segments | translated: {len(txn)} segments")
    if missing:
        print(f"MISSING {len(missing)}: {missing}")
    if extra:
        print(f"EXTRA {len(extra)}: {extra}")
    if not in_order:
        print("ORDER MISMATCH: translated segments are not in source order")
    if not missing and not extra and in_order:
        print("OK: 1:1 timestamp coverage, in order")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
