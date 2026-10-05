"""Reject wrong-length and silence-padded spotdl downloads before import (#165).

spotdl picks the YouTube source by search, so it sometimes downloads the wrong
recording: a radio edit, an extended mix, a preview, or an upload padded with
silence to get past Content ID.  The file still carries Spotify's tags, so beets
(and certainly the ``--asis`` pass) imports it as the right track.

Before ``beet import``, every spotdl inbox file is checked against its playlist
entry:

1. **Duration**: the ``ffprobe`` length must be within ``TOLERANCE`` of the
   entry's ``duration`` (whole seconds, from the ``.spotdl`` snapshot).
2. **Mid-track silence**: no ``silencedetect`` window may start more than
   ``EDGE_SECONDS`` after the beginning and end more than ``EDGE_SECONDS``
   before the end.  Leading and trailing silence (fades, hidden tracks) is fine.

A file with no entry or no duration fails open, with a log line.  Rejected files
move to ``quarantine/rejected/<playlist>/`` with a ``<file>.reason`` sidecar;
the asis pass skips that directory, and reconcile keeps their URLs, so spotdl
does not download the same wrong video again.

``LENGTH_GUARD``: ``on`` rejects, ``dry-run`` (default) only logs
``[WOULD-REJECT]``, ``off`` skips the check.  Usenet albums are not checked:
the whole-release match verifies them.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

TOLERANCE = 0.10
SILENCE_NOISE = "-50dB"
SILENCE_MIN_SECONDS = 8
EDGE_SECONDS = 5
REASONS = ("duration", "silence")

_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


@dataclass(frozen=True)
class Rejection:
    reason: str  # one of REASONS
    detail: str


def guard_mode() -> str:
    mode = os.environ.get("LENGTH_GUARD", "dry-run").strip().lower()
    return mode if mode in ("on", "dry-run", "off") else "dry-run"


def probe_duration(path: Path) -> float | None:
    """The file's length in seconds, or None if ffprobe can't read it."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout.strip()
        return float(out)
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


def silence_windows(path: Path) -> list[tuple[float, float | None]]:
    """``(start, end)`` of each silent stretch; ``end`` is None when it runs to the end of the file."""
    proc = subprocess.run(
        ["ffmpeg", "-nostats", "-hide_banner", "-i", str(path),
         "-af", f"silencedetect=noise={SILENCE_NOISE}:d={SILENCE_MIN_SECONDS}", "-f", "null", "-"],
        capture_output=True, text=True, timeout=300,
    )
    windows: list[tuple[float, float | None]] = []
    start: float | None = None
    for line in proc.stderr.splitlines():
        if m := _SILENCE_START.search(line):
            start = max(0.0, float(m.group(1)))
        elif (m := _SILENCE_END.search(line)) and start is not None:
            windows.append((start, float(m.group(1))))
            start = None
    if start is not None:
        windows.append((start, None))
    return windows


def mid_track_silence(windows: list[tuple[float, float | None]], length: float) -> tuple[float, float] | None:
    """The first silent stretch away from both ends, or None."""
    for start, end in windows:
        if end is not None and start > EDGE_SECONDS and end < length - EDGE_SECONDS:
            return start, end
    return None


def check_file(path: Path, expected: float | None, silence: bool = True) -> Rejection | None:
    """Why *path* is the wrong audio for a track *expected* seconds long, or None if it looks right."""
    actual = probe_duration(path)
    if actual is None:
        logger.info("  [GUARD] %s: ffprobe could not read the length; not checked", path.name)
        return None
    if expected and abs(actual - expected) > expected * TOLERANCE:
        return Rejection("duration", f"expected {expected:.0f}s, got {actual:.0f}s ({(actual - expected) / expected:+.0%})")
    if silence and (gap := mid_track_silence(silence_windows(path), actual)):
        return Rejection("silence", f"silent from {gap[0]:.1f}s to {gap[1]:.1f}s of {actual:.0f}s")
    return None


def song_url(path: Path) -> str | None:
    """The Spotify URL spotdl embedded in an inbox file."""
    from music_scan.music_pipeline import _read_spotdl_tags  # noqa: PLC0415

    return _read_spotdl_tags(str(path)).url


def read_comment(path: Path) -> str | None:
    """The comment tag, where spotdl records the YouTube URL it downloaded from."""
    try:
        from mutagen.mp4 import MP4  # noqa: PLC0415

        return (MP4(str(path)).tags or {}).get("\xa9cmt", [None])[0]
    except Exception:
        return None


def expected_durations(spotdl_dir: Path) -> dict[str, dict[str, int]]:
    """``{playlist: {spotify_url: duration}}`` from every ``.spotdl`` snapshot."""
    out: dict[str, dict[str, int]] = {}
    for f in sorted(spotdl_dir.glob("*.spotdl")):
        try:
            songs = json.loads(f.read_text(encoding="utf-8")).get("songs", [])
        except Exception:
            continue
        out[f.stem] = {s["url"]: s["duration"] for s in songs if s.get("url") and s.get("duration")}
    return out


def _expected(durations: dict[str, dict[str, int]], playlist: str, url: str | None) -> int | None:
    """The entry's duration: its own playlist first, then any (backfill re-downloads may be missing from it)."""
    if not url:
        return None
    if url in durations.get(playlist, {}):
        return durations[playlist][url]
    return next((d[url] for d in durations.values() if url in d), None)


def reject(path: Path, rejection: Rejection, inbox: Path, rejected_dir: Path) -> Path:
    """Move *path* to ``rejected_dir/<playlist>/`` with a ``.reason`` sidecar."""
    dest = rejected_dir / path.relative_to(inbox)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), dest)
    dest.with_name(dest.name + ".reason").write_text(f"{rejection.reason}: {rejection.detail}\n", encoding="utf-8")
    return dest


def guard_inbox(inbox: Path, spotdl_dir: Path, rejected_dir: Path, audio_exts: set[str],
                mode: str | None = None) -> Counter:
    """Check every spotdl inbox file; returns the count of files rejected per reason.

    *inbox* is ``inbox/spotdl``: files sit at ``<playlist>/<file>``.  In ``dry-run``
    nothing moves and the count stays 0.
    """
    mode = mode or guard_mode()
    rejected: Counter = Counter({r: 0 for r in REASONS})
    if mode == "off" or not inbox.exists():
        return rejected
    durations = expected_durations(spotdl_dir)
    for f in sorted(inbox.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in audio_exts:
            continue
        playlist = f.relative_to(inbox).parts[0]
        expected = _expected(durations, playlist, song_url(f))
        if expected is None:
            logger.info("  [GUARD] %s: %s has no Spotify duration; length not checked", playlist, f.name)
        rejection = check_file(f, expected)
        if rejection is None:
            continue
        if mode == "dry-run":
            logger.warning("  [WOULD-REJECT] %s: %s — %s: %s", playlist, f.name, rejection.reason, rejection.detail)
            continue
        dest = reject(f, rejection, inbox, rejected_dir)
        rejected[rejection.reason] += 1
        logger.warning("  [REJECT] %s: %s — %s: %s → %s", playlist, f.name, rejection.reason, rejection.detail, dest)
    return rejected
