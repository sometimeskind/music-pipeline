"""Reject wrong-length and silence-padded spotdl downloads before import (#165).

spotdl picks the YouTube source by search, so it sometimes downloads the wrong
recording: a radio edit, an extended mix, a preview, or an upload padded with
silence to get past Content ID.  The file still carries Spotify's tags, so beets
(and certainly the ``--asis`` pass) imports it as the right track.

Before ``beet import``, every spotdl inbox file is checked against its playlist
entry:

1. **Duration**: the ``ffprobe`` length must be within ``TOLERANCE`` of the
   entry's ``duration`` (whole seconds, from the playlist store).
2. **Mid-track silence**: no ``silencedetect`` window may start more than
   ``EDGE_SECONDS`` after the beginning and end more than ``EDGE_SECONDS``
   before the end.  Leading and trailing silence (fades, hidden tracks) is fine.

A file with no entry or no duration fails open, with a log line.  Rejected files
move to ``quarantine/rejected/<playlist>/`` with a ``<file>.reason`` sidecar;
the asis pass skips that directory, and the nightly holds their URLs back
(:mod:`music_scan.held`), so spotdl does not download the same wrong video again.

``LENGTH_GUARD``: ``on`` rejects, ``dry-run`` (default) only logs
``[WOULD-REJECT]``, ``off`` skips the check.  Usenet albums are not checked:
the whole-release match verifies them.

An album request's fallback (homelab#2478) is built from MusicBrainz, so
spotdl found its audio by ISRC or by artist, title and length alone.  Its
downloads wait outside the inbox, in ``unverified/request/``, and
:func:`verify_requests` checks each against the track its ``WOAS`` names (its
MusicBrainz recording URL): the length and silence checks above against
MusicBrainz's length, then an AcoustID lookup of its fingerprint.  A file is
accepted, and moved to ``inbox/spotdl/request/`` for the import, only when
AcoustID names the track's own recording: two items are one track only when
an ID agrees, never by title.  Any other answer, none included, rejects it
(``fingerprint``), whatever ``LENGTH_GUARD`` says; a lookup that fails leaves
it for the next scan.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from music_fetch import events

logger = logging.getLogger(__name__)

TOLERANCE = 0.10
SILENCE_NOISE = "-50dB"
SILENCE_MIN_SECONDS = 8
EDGE_SECONDS = 5
REASONS = ("duration", "silence", "fingerprint")
# AcoustID's fingerprint score a request fallback's recording must reach (music-mb-fingerprint's).
FINGERPRINT_MIN_SCORE = 0.85
# AcoustID asks for at most three lookups a second.
ACOUSTID_DELAY = 0.35

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


def expected_durations() -> dict[str, dict[str, int]]:
    """``{playlist: {spotify_url: duration}}`` from every playlist store (homelab#1914)."""
    from music_fetch import store  # noqa: PLC0415

    return {name: {s["url"]: s["duration"] for s in songs if s.get("url") and s.get("duration")}
            for name, songs in store.songs_by_playlist().items()}


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


def _reject_event(playlist: str, f: Path, rejection: Rejection, dest: Path) -> None:
    events.record("reject", f"[REJECT] {playlist}: {f.name} — {rejection.reason}: {rejection.detail} → {dest}",
                  source="guard", data={"playlist": playlist, "file": f.name, "reason": rejection.reason,
                                        "detail": rejection.detail, "dest": str(dest)})


class LookupFailed(RuntimeError):
    """AcoustID couldn't be asked: the file waits for the next scan."""


def acoustid_recordings(path: Path) -> set[str]:
    """The MusicBrainz recordings AcoustID links to *path*'s fingerprint with a
    score of at least :data:`FINGERPRINT_MIN_SCORE`.  Raises
    :class:`LookupFailed` when there is no API key or the web service fails,
    and ``ValueError`` when the file can't be fingerprinted (a rejection)."""
    import acoustid  # noqa: PLC0415

    key = os.environ.get("ACOUSTID_APIKEY")
    if not key:
        raise LookupFailed("ACOUSTID_APIKEY is not set")
    try:
        results = list(acoustid.match(key, str(path)))
    except acoustid.FingerprintGenerationError as exc:
        raise ValueError(f"no fingerprint: {exc}") from exc
    except acoustid.WebServiceError as exc:
        raise LookupFailed(str(exc)) from exc
    return {rid for score, rid, *_ in results if rid and score >= FINGERPRINT_MIN_SCORE}


def request_songs() -> dict[str, dict]:
    """``{recording URL: song}`` of every album request's fallback songs, from
    the album state; empty when it can't be read."""
    from music_fetch import albums  # noqa: PLC0415

    try:
        records = albums.State.load().albums
    except Exception as exc:
        logger.warning("  [GUARD] could not read the album state (%s); request fallbacks wait", exc)
        return {}
    return {s["url"]: s for r in records.values() for s in r.get("album_songs") or () if s.get("url")}


def verify_requests(unverified: Path, inbox: Path, rejected_dir: Path, audio_exts: set[str],
                    songs: dict[str, dict] | None = None,
                    recordings: Callable[[Path], set[str]] = acoustid_recordings) -> Counter:
    """Check every album request fallback download in *unverified*
    (``<source>/<file>``) against the track its ``WOAS`` names (homelab#2478):
    move it to *inbox*/``<source>/`` when AcoustID names that track's own
    recording, else to *rejected_dir*/``<source>/`` with its reason, so the
    nightly holds it back.  *songs*: :func:`request_songs`.  Returns the
    rejections per reason."""
    from music_fetch.album_requests import url_recording  # noqa: PLC0415

    rejected: Counter = Counter({r: 0 for r in REASONS})
    files = [f for f in sorted(unverified.rglob("*")) if f.is_file() and f.suffix.lower() in audio_exts] \
        if unverified.exists() else []
    if not files:
        return rejected
    songs = request_songs() if songs is None else songs
    for f in files:
        source = f.relative_to(unverified).parts[0]
        url = song_url(f)
        recording = url_recording(url)
        song = songs.get(url or "")
        if recording is None or song is None:
            # Its request was reset or answered meanwhile, or it isn't one of ours: it isn't wanted.
            rejection = Rejection("fingerprint", f"no album request's track is {url or 'named'}")
        else:
            rejection = check_file(f, song.get("duration") or None)
        if rejection is None:
            try:
                found = recordings(f)
            except LookupFailed as exc:
                logger.warning("  [GUARD] %s: %s: AcoustID lookup failed (%s); checked at the next scan",
                               source, f.name, exc)
                continue
            except ValueError as exc:
                found, why = set(), str(exc)
            else:
                why = (f"AcoustID names {len(found)} other recording(s)" if found
                       else "AcoustID knows no recording for it")
            finally:
                time.sleep(ACOUSTID_DELAY)
            if recording not in found:
                rejection = Rejection("fingerprint", f"not recording {recording}: {why}")
        if rejection is not None:
            dest = reject(f, rejection, unverified, rejected_dir)
            rejected[rejection.reason] += 1
            logger.warning("  [REJECT] %s: %s — %s: %s → %s", source, f.name, rejection.reason, rejection.detail,
                           dest)
            _reject_event(source, f, rejection, dest)
            continue
        dest = inbox / f.relative_to(unverified)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), dest)
        logger.info("  [VERIFIED] %s: %s — AcoustID names recording %s", source, f.name, recording)
    return rejected


def guard_inbox(inbox: Path, rejected_dir: Path, audio_exts: set[str], mode: str | None = None) -> Counter:
    """Check every spotdl inbox file; returns the count of files rejected per reason.

    *inbox* is ``inbox/spotdl``: files sit at ``<playlist>/<file>``.  In ``dry-run``
    nothing moves and the count stays 0.
    """
    mode = mode or guard_mode()
    rejected: Counter = Counter({r: 0 for r in REASONS})
    if mode == "off" or not inbox.exists():
        return rejected
    from music_fetch.album_requests import SOURCE  # noqa: PLC0415

    durations = expected_durations()
    for f in sorted(inbox.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in audio_exts:
            continue
        playlist = f.relative_to(inbox).parts[0]
        if playlist == SOURCE:
            continue  # an album request's fallback: verify_requests checked it
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
        _reject_event(playlist, f, rejection, dest)
    return rejected
