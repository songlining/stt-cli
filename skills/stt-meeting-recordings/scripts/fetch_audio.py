#!/usr/bin/env python3
"""Fetch audio for transcription from a URL (Spotify scrape or yt-dlp) or local file.

Spotify (creators.spotify.com / podcasters.spotify.com) cannot be downloaded with
yt-dlp (open.spotify.com is DRM-locked; the creators pages are unsupported). The
page HTML embeds the direct MP3 URL in a `"audioUrl"` JSON field — this script
scrapes it, unescapes the JSON-escaped URL, and downloads the MP3. For every
other source it falls back to yt-dlp bestaudio.

Usage:
    python3 fetch_audio.py <url-or-path> [--out-dir DIR] [--convert-wav]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

SPOTIFY_HOSTS = ("creators.spotify.com", "podcasters.spotify.com")
MP3_CDN_HINT = "cloudfront"


def _curl(url: str, timeout: int = 60) -> str:
    html = subprocess.run(
        ["curl", "-sL", "-A", "Mozilla/5.0", url],
        capture_output=True, text=True, timeout=timeout,
    ).stdout
    if not html:
        raise SystemExit(f"curl returned nothing for {url}")
    return html


def _unescape(s: str) -> str:
    # JSON-escaped URL, e.g. https:\u002F\u002Fd3ctxlq1ktw2nl.cloudfront.net\u002F...
    return json.loads(f'"{s}"')


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out)
    if dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        raise SystemExit(f"Downloaded empty file from {url}")


def fetch_spotify(url: str, out_dir: Path) -> dict:
    html = _curl(url)
    m = re.search(r'"audioUrl":"([^"]*)"', html)
    if not m:
        raise SystemExit(
            f"No audioUrl found in {url}. The episode may not be hosted on Spotify "
            "for Creators — try the yt-dlp path or ask the user for a different link."
        )
    audio_url = _unescape(m.group(1))
    if MP3_CDN_HINT not in audio_url:
        print(f"Warning: audio URL does not look like a podcast CDN: {audio_url[:80]}", file=sys.stderr)
    dest = out_dir / "episode.mp3"
    print(f"Downloading {audio_url}")
    _download(audio_url, dest)
    title = None
    tm = re.search(r'"title":"([^"]*)"', html)
    if tm:
        title = tm.group(1)
    return {"title": title, "audio_path": str(dest), "source_url": url}


def fetch_ytdlp(url: str, out_dir: Path) -> dict:
    probe = subprocess.run(
        ["yt-dlp", "--print", "%(title)s|%(extractor)s", "--skip-download", url],
        capture_output=True, text=True, timeout=120,
    )
    if probe.returncode != 0:
        raise SystemExit(f"yt-dlp cannot handle {url}:\n{probe.stderr.strip()}")
    title, extractor = (probe.stdout.strip().split("|", 1) + [""])[:2]
    print(f"yt-dlp ({extractor}) fetching {url}")
    subprocess.run(
        ["yt-dlp", "-f", "bestaudio", "-o", str(out_dir / "episode.%(ext)s"), url],
        check=True, timeout=600,
    )
    audio = next(out_dir.glob("episode.*"), None)
    if audio is None:
        raise SystemExit("yt-dlp finished but produced no episode.* file")
    return {"title": title, "audio_path": str(audio), "source_url": url}


def convert_wav(audio: Path, out_dir: Path) -> Path:
    wav = out_dir / "episode.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(audio), "-ar", "16000", "-ac", "1",
         "-c:a", "pcm_s16le", str(wav)],
        check=True,
    )
    return wav


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", help="URL (Spotify/YouTube/...) or local audio file")
    ap.add_argument("--out-dir", default=".", help="Directory for downloads (created if missing)")
    ap.add_argument("--convert-wav", action="store_true", help="Also produce 16 kHz mono episode.wav")
    args = ap.parse_args()

    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    src = args.source

    if src.startswith(("http://", "https://")):
        host = re.sub(r"^https?://([^/:]+).*$", r"\1", src)
        result = fetch_spotify(src, out_dir) if host in SPOTIFY_HOSTS else fetch_ytdlp(src, out_dir)
    else:
        path = Path(src).expanduser().resolve()
        if not path.exists():
            raise SystemExit(f"File not found: {path}")
        result = {"title": path.stem, "audio_path": str(path), "source_url": str(path)}

    print(json.dumps(result, indent=2, ensure_ascii=False))
    if args.convert_wav:
        wav = convert_wav(Path(result["audio_path"]), out_dir)
        print(json.dumps({"wav": str(wav), "wav_bytes": wav.stat().st_size}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
