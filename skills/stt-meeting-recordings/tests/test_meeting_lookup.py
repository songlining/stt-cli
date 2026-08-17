#!/usr/bin/env python3
"""Unit tests for scripts/meeting_lookup.py.

Covers: current-meeting selection (latest-started wins), upcoming-meeting
selection within the window, all-day event skipping, next-meeting fallback,
outlook CLI failure modes (missing binary, non-zero exit, bad JSON), and the
IANA timezone being passed through to `outlook calendar` (the global config
timezone is a Windows tz ID that the calendar command rejects).

Run with:
    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
import meeting_lookup as ml  # noqa: E402

NOW = dt.datetime(2026, 8, 12, 9, 30, tzinfo=dt.timezone(dt.timedelta(hours=10)))


def event(subject: str, start: str, end: str, **extra) -> dict:
    ev = {
        "subject": subject,
        "start": start,
        "end": end,
        "location": "Microsoft Teams Meeting",
        "organizer": {"name": "Jane Doe", "address": "organizer@example.com"},
        "attendees": [
            {"email": {"name": "Alice", "address": "alice@example.com"}, "type": "Required"}
        ],
        "is_online_meeting": True,
        "online_meeting_url": None,
        "is_all_day": False,
    }
    ev.update(extra)
    return ev


def iso(h: int, m: int = 0) -> str:
    return dt.datetime(2026, 8, 12, h, m, tzinfo=dt.timezone(dt.timedelta(hours=10))).isoformat()


class SelectMeetingTests(unittest.TestCase):
    def test_current_meeting_wins_over_upcoming(self):
        events = [
            event("Past call", iso(8, 0), iso(8, 30)),
            event("Current standup", iso(9, 0), iso(9, 45)),
            event("Upcoming review", iso(10, 0), iso(11, 0)),
        ]
        match, nxt = ml.select_meeting(events, NOW, within_minutes=3)
        self.assertIsNotNone(match)
        self.assertEqual(match["subject"], "Current standup")
        self.assertEqual(match["status"], "current")
        self.assertEqual(match["starts_in_seconds"], None)
        self.assertEqual(match["ends_in_seconds"], 15 * 60)
        self.assertEqual(nxt["subject"], "Upcoming review")
        self.assertEqual(nxt["status"], "next")

    def test_latest_started_current_meeting_wins_on_overlap(self):
        events = [
            event("Long overlapping call", iso(9, 0), iso(10, 30)),
            event("Recently joined huddle", iso(9, 20), iso(9, 50)),
        ]
        match, _ = ml.select_meeting(events, NOW, within_minutes=3)
        self.assertEqual(match["subject"], "Recently joined huddle")

    def test_upcoming_within_window(self):
        events = [event("Client QBR", iso(9, 32), iso(10, 30))]
        match, _ = ml.select_meeting(events, NOW, within_minutes=3)
        self.assertIsNotNone(match)
        self.assertEqual(match["subject"], "Client QBR")
        self.assertEqual(match["status"], "upcoming")
        self.assertEqual(match["starts_in_seconds"], 2 * 60)

    def test_upcoming_outside_window_not_matched_but_reported_as_next(self):
        events = [event("Later sync", iso(10, 30), iso(11, 0))]
        match, nxt = ml.select_meeting(events, NOW, within_minutes=3)
        self.assertIsNone(match)
        self.assertEqual(nxt["subject"], "Later sync")

    def test_all_day_events_skipped(self):
        events = [
            event("Public holiday", iso(0, 0), iso(23, 59), is_all_day=True),
            event("Real meeting", iso(9, 25), iso(9, 40)),
        ]
        match, _ = ml.select_meeting(events, NOW, within_minutes=3)
        self.assertEqual(match["subject"], "Real meeting")

    def test_no_meetings(self):
        events = [event("Yesterday", iso(0, 0), iso(1, 0))]
        match, nxt = ml.select_meeting(events, NOW, within_minutes=3)
        self.assertIsNone(match)
        self.assertIsNone(nxt)

    def test_summary_excludes_html_bodies(self):
        ev = event("With body", iso(9, 0), iso(9, 30), body="<html>lots of markup</html>")
        summary = ml.summarize_event(ev, NOW)
        self.assertNotIn("body", summary)
        self.assertEqual(summary["attendees"], [{"name": "Alice", "address": "alice@example.com"}])


class RunLookupTests(unittest.TestCase):
    def _fake_payload(self, events):
        return {"ok": True, "schema_version": "1", "data": events}

    def test_timezone_and_json_flags_passed_to_outlook(self):
        events = [event("Standup", iso(9, 0), iso(9, 45))]
        fake_now = dt.datetime(2026, 8, 12, 9, 30, tzinfo=dt.timezone(dt.timedelta(hours=10)))
        with mock.patch.object(
            ml.subprocess, "run",
            return_value=mock.Mock(returncode=0, stdout=json.dumps(self._fake_payload(events)), stderr=""),
        ) as run:
            result = ml.run_lookup("outlook", 1, "Australia/Sydney", 3, now=fake_now)
        self.assertTrue(result["ok"])
        self.assertEqual(result["match"]["subject"], "Standup")
        cmd = run.call_args.args[0]
        self.assertIn("calendar", cmd)
        self.assertIn("--timezone", cmd)
        self.assertEqual(cmd[cmd.index("--timezone") + 1], "Australia/Sydney")
        self.assertIn("--json", cmd)

    def test_missing_binary(self):
        result = ml.run_lookup("/nonexistent/outlook", 1, "Australia/Sydney", 3)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "outlook_not_found")

    def test_nonzero_exit(self):
        with mock.patch.object(
            ml.subprocess, "run",
            return_value=mock.Mock(returncode=1, stdout="", stderr="boom"),
        ):
            result = ml.run_lookup("outlook", 1, "Australia/Sydney", 3)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "calendar_failed")

    def test_bad_json(self):
        with mock.patch.object(
            ml.subprocess, "run",
            return_value=mock.Mock(returncode=0, stdout="not json", stderr=""),
        ):
            result = ml.run_lookup("outlook", 1, "Australia/Sydney", 3)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "bad_json")

    def test_outlook_envelope_error(self):
        with mock.patch.object(
            ml.subprocess, "run",
            return_value=mock.Mock(
                returncode=0,
                stdout=json.dumps({"ok": False, "error": {"code": "rate_limited", "message": "nope"}}),
                stderr="",
            ),
        ):
            result = ml.run_lookup("outlook", 1, "Australia/Sydney", 3)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "rate_limited")


if __name__ == "__main__":
    unittest.main()
