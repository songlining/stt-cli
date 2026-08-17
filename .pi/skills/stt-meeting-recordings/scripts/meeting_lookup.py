#!/usr/bin/env python3
"""Find the current or forthcoming meeting from the user's Outlook calendar.

Wraps the `outlook` CLI (outlook365-cli, see the outlook-cli skill) so the
stt-meeting-recordings skill can name a recording session after the real
meeting it is about to capture. Returns the meeting that is happening right
now, or the next one starting within a short window (default 3 minutes).

Why not parse `outlook calendar` directly in the skill instructions:
- the calendar JSON embeds full HTML bodies (very token-heavy for an agent);
- the global outlook config timezone is a Windows tz ID
  (`AUS Eastern Standard Time`) which the `calendar` command rejects, so an
  explicit IANA `--timezone` must always be passed;
- current-vs-upcoming selection + "starts in N seconds" arithmetic is
  deterministic here rather than left to agent interpretation.

Usage:
    meeting_lookup.py [--within-minutes 3] [--days 1]
                      [--timezone Australia/Sydney] [--outlook-bin outlook]

Exit codes: 0 = lookup succeeded (found may be false), 1 = lookup failed.
Output is always one JSON envelope on stdout:

    {"ok": true, "found": true,
     "match": {"status": "current"|"upcoming", "subject": ..., "start": ...,
               "end": ..., "duration_seconds": ..., "starts_in_seconds": ...,
               "ends_in_seconds": ..., "location": ..., "organizer": {...},
               "attendees": [{"name": ..., "address": ...}, ...],
               "is_online_meeting": bool,
               "online_meeting_url": ...|null},
     "next": {...}|null, "now": ...}
    {"ok": true, "found": false, "match": null, "next": {...}|null, "now": ...}
    {"ok": false, "error": {"code": ..., "message": ...}}
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from typing import Any

DEFAULT_TIMEZONE = "Australia/Sydney"
DEFAULT_WITHIN_MINUTES = 3
DEFAULT_DAYS = 1
CALENDAR_TIMEOUT_SECONDS = 120  # allow for slow auth / auto re-login


def parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def summarize_event(ev: dict[str, Any], now: dt.datetime) -> dict[str, Any] | None:
    """Compact, token-light summary of one calendar event (no HTML bodies)."""
    start = parse_ts(ev.get("start"))
    end = parse_ts(ev.get("end"))
    if start is None or end is None:
        return None
    organizer = ev.get("organizer") or {}
    attendees = []
    for a in ev.get("attendees") or []:
        email = a.get("email") or {}
        address = email.get("address")
        if not address:
            continue
        name = email.get("name") or ""
        attendees.append({"name": name, "address": address})
    return {
        "subject": ev.get("subject", "(untitled)"),
        "start": start.isoformat(timespec="minutes"),
        "end": end.isoformat(timespec="minutes"),
        "duration_seconds": int((end - start).total_seconds()),
        "location": ev.get("location"),
        "organizer": {
            "name": organizer.get("name"),
            "address": organizer.get("address"),
        },
        "attendees": attendees,
        "is_online_meeting": bool(ev.get("is_online_meeting")),
        "online_meeting_url": ev.get("online_meeting_url"),
    }


def select_meeting(
    events: list[dict[str, Any]], now: dt.datetime, within_minutes: int
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Pick (match, next) from calendar events.

    match: current meeting (latest-started one wins) else the soonest meeting
    starting within `within_minutes`. next: first meeting strictly after the
    match's end (or after now when nothing matched), for context.
    All-day events are skipped (they are not recordable meetings).
    """
    window = dt.timedelta(minutes=within_minutes)
    parsed: list[tuple[dt.datetime, dt.datetime, dict[str, Any]]] = []
    for ev in events:
        if ev.get("is_all_day"):
            continue
        start = parse_ts(ev.get("start"))
        end = parse_ts(ev.get("end"))
        if start is None or end is None:
            continue
        parsed.append((start, end, ev))

    current = [
        (start, end, ev)
        for start, end, ev in parsed
        if start <= now < end
    ]
    upcoming = [
        (start, end, ev)
        for start, end, ev in parsed
        if start > now and start - now <= window
    ]

    match: dict[str, Any] | None = None
    if current:
        # Most recently started current meeting.
        start, end, ev = max(current, key=lambda t: t[0])
        match = summarize_event(ev, now)
        if match:
            match["status"] = "current"
            match["starts_in_seconds"] = None
            match["ends_in_seconds"] = int((end - now).total_seconds())
        anchor_end = end
    elif upcoming:
        # Soonest upcoming within the window.
        start, end, ev = min(upcoming, key=lambda t: t[0])
        match = summarize_event(ev, now)
        if match:
            match["status"] = "upcoming"
            match["starts_in_seconds"] = int((start - now).total_seconds())
            match["ends_in_seconds"] = None
        anchor_end = end
    else:
        anchor_end = now

    nxt: dict[str, Any] | None = None
    for start, end, ev in sorted(parsed, key=lambda t: t[0]):
        if start >= anchor_end:
            summary = summarize_event(ev, now)
            if summary:
                summary["status"] = "next"
                summary["starts_in_seconds"] = int((start - now).total_seconds())
                summary["ends_in_seconds"] = None
                nxt = summary
            break
    return match, nxt


def run_lookup(
    outlook_bin: str, days: int, timezone: str, within_minutes: int,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now = now or dt.datetime.now().astimezone()
    cmd = [
        outlook_bin,
        "calendar",
        "--days",
        str(days),
        "--timezone",
        timezone,
        "--json",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CALENDAR_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return {"ok": False, "error": {
            "code": "outlook_not_found",
            "message": f"outlook CLI not found at {outlook_bin!r} — install outlook365-cli (see the outlook-cli skill).",
        }}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": {
            "code": "timeout",
            "message": f"outlook calendar timed out after {CALENDAR_TIMEOUT_SECONDS}s.",
        }}

    if proc.returncode != 0:
        return {"ok": False, "error": {
            "code": "calendar_failed",
            "message": f"outlook calendar exited {proc.returncode}: {(proc.stderr or '').strip()[:500]}",
        }}

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": {
            "code": "bad_json",
            "message": "outlook calendar returned non-JSON output.",
        }}

    if not payload.get("ok"):
        err = payload.get("error") or {}
        return {"ok": False, "error": {
            "code": err.get("code", "calendar_error"),
            "message": str(err.get("message", "outlook calendar reported an error"))[:500],
        }}

    events = payload.get("data") or []
    match, nxt = select_meeting(events, now, within_minutes)
    result: dict[str, Any] = {
        "ok": True,
        "now": now.isoformat(timespec="minutes"),
        "found": match is not None,
        "match": match,
        "next": nxt,
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--within-minutes", type=int, default=DEFAULT_WITHIN_MINUTES,
                        help=f"upcoming-meeting window (default {DEFAULT_WITHIN_MINUTES})")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"calendar lookahead in days (default {DEFAULT_DAYS})")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE,
                        help="IANA timezone for the calendar query (default %(default)s)")
    parser.add_argument("--outlook-bin", default="outlook",
                        help="path to the outlook CLI (default %(default)s)")
    args = parser.parse_args(argv)

    result = run_lookup(args.outlook_bin, args.days, args.timezone, args.within_minutes)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
