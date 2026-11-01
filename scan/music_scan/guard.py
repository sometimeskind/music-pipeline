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
an ID agrees, never by title.  Any other answer rejects it, whatever
``LENGTH_GUARD`` says, for one of two reasons (homelab#2546): AcoustID has no
MusicBrainz recording for the audio at all (``acoustid-unknown``: a new or
obscure release AcoustID hasn't linked yet, so the file may well be right), or
it names other recordings, or ours only below the score (``fingerprint``).  A
file that can't be fingerprinted is ``fingerprint`` too.  A lookup that fails
leaves it for the next scan.  :func:`request_rejects` reads the reasons back
for ``list_requests``.

An ``acoustid-unknown`` reject is re-checked rather than downloaded again
(:func:`recheck_unknown`, the operator's call on homelab#2546): AcoustID links
new releases over time, and a lookup costs a second.  Each scan asks AcoustID
again about the held files last checked a day or more ago, under the same
rule, and stops after :data:`RECHECK_DAYS`, leaving the file to the operator.
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from music_fetch import events
from music_fetch.album_requests import UNKNOWN_REJECT
from music_scan.held import QUARANTINE_DIR

logger = logging.getLogger(__name__)

TOLERANCE = 0.10
SILENCE_NOISE = "-50dB"
SILENCE_MIN_SECONDS = 8
EDGE_SECONDS = 5
# A request fallback download AcoustID knows no MusicBrainz recording for (homelab#2546).
UNKNOWN = UNKNOWN_REJECT
REASONS = ("duration", "silence", "fingerprint", UNKNOWN)
# AcoustID's fingerprint score a request fallback's recording must reach (music-mb-fingerprint's).
FINGERPRINT_MIN_SCORE = 0.85
# AcoustID asks for at most three lookups a second.
ACOUSTID_DELAY = 0.35
# Seconds an AcoustID lookup may take before it counts as failed (music-mb-fingerprint's).
LOOKUP_TIMEOUT = 30
# An acoustid-unknown reject is asked about again at most this often (homelab#2546) ...
RECHECK_SECONDS = 24 * 3600
# ... for this many days after it was held (ACOUSTID_RECHECK_DAYS); then the operator decides.
# Nothing purges quarantine/rejected/, so the file stays.
RECHECK_DAYS = 30
# recheck_unknown's outcomes: confirmed and moved to the inbox, now a real mismatch,
# still unknown, a failed lookup (the pass stops), and past RECHECK_DAYS this scan.
RECHECKS = ("verified", "fingerprint", UNKNOWN, "failed", "stopped")

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


def acoustid_recordings(path: Path) -> dict[str, float]:
    """Every MusicBrainz recording AcoustID links to *path*'s fingerprint, at
    its best score, whatever the score: empty when AcoustID has no result, or
    only results with no recording (pyacoustid skips those).  Raises
    :class:`LookupFailed` when there is no API key or the web service fails or
    times out, and ``ValueError`` when the file can't be fingerprinted (a
    rejection)."""
    import acoustid  # noqa: PLC0415

    key = os.environ.get("ACOUSTID_APIKEY")
    if not key:
        raise LookupFailed("ACOUSTID_APIKEY is not set")
    best: dict[str, float] = {}
    try:
        for score, rid, *_ in acoustid.match(key, str(path), timeout=LOOKUP_TIMEOUT):
            if rid:
                best[rid] = max(score or 0.0, best.get(rid, 0.0))
    except acoustid.FingerprintGenerationError as exc:
        raise ValueError(f"no fingerprint: {exc}") from exc
    except acoustid.WebServiceError as exc:
        raise LookupFailed(str(exc)) from exc
    return best


def fingerprint_rejection(recording: str, found: Mapping[str, float]) -> Rejection | None:
    """Why AcoustID's answer *found* (:func:`acoustid_recordings`) rejects a
    download that should be *recording*, or None when it names *recording* at
    :data:`FINGERPRINT_MIN_SCORE` or more.  No recording at all is
    :data:`UNKNOWN` (homelab#2546): AcoustID can't tell, so the file may be
    right.  Any recording named, ours below the score included, is
    ``fingerprint``: AcoustID heard something, and it isn't confidently ours."""
    if found.get(recording, 0.0) >= FINGERPRINT_MIN_SCORE:
        return None
    if not found:
        return Rejection(UNKNOWN, f"not recording {recording}: AcoustID knows no MusicBrainz recording for it")
    others = {r: s for r, s in found.items() if r != recording}
    if not others:
        return Rejection("fingerprint", f"not recording {recording}: AcoustID names it only at score "
                                        f"{found[recording]:.2f}")
    top, score = max(others.items(), key=lambda kv: (kv[1], kv[0]))
    return Rejection("fingerprint", f"not recording {recording}: AcoustID names {len(others)} other recording(s) "
                                    f"(best {top} at {score:.2f})")


def request_rejects(rejected_dir: Path = QUARANTINE_DIR / "rejected") -> dict[str, str]:
    """``{recording: reason}`` of the album request fallback downloads held in
    *rejected_dir*/``request/`` (homelab#2546), from each file's ``WOAS`` and
    its ``.reason`` sidecar, for ``list_requests``.  A recording rejected twice
    keeps a reason other than :data:`UNKNOWN`, so a request reads as unknown to
    AcoustID only when every download of it was.  Empty when the directory
    can't be read."""
    from music_fetch.album_requests import SOURCE, url_recording  # noqa: PLC0415

    out: dict[str, str] = {}
    try:
        sidecars = sorted((rejected_dir / SOURCE).rglob("*.reason"))
    except OSError:
        return out
    for sidecar in sidecars:
        audio = sidecar.with_name(sidecar.name.removesuffix(".reason"))
        try:
            reason = sidecar.read_text(encoding="utf-8").split(":", 1)[0].strip()
        except OSError:
            continue
        if not audio.is_file() or not reason:
            continue  # the operator took the file away: the nightly fetches it again
        recording = url_recording(song_url(audio))
        if recording and out.get(recording) in (None, UNKNOWN):
            out[recording] = reason
    return out


def request_songs() -> dict[str, dict] | None:
    """``{recording URL: song}`` of every album request's fallback songs, from
    the album state; None when it can't be read, so no download is judged
    against an empty state (every one would read as unwanted)."""
    from music_fetch import albums  # noqa: PLC0415

    try:
        records = albums.State.load().albums
    except Exception as exc:
        logger.warning("  [GUARD] could not read the album state (%s); request fallbacks wait", exc)
        return None
    return {s["url"]: s for r in records.values() for s in r.get("album_songs") or () if s.get("url")}


def verify_requests(unverified: Path, inbox: Path, rejected_dir: Path, audio_exts: set[str],
                    songs: dict[str, dict] | None = None,
                    recordings: Callable[[Path], Mapping[str, float]] = acoustid_recordings) -> Counter:
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
    if songs is None:
        return rejected
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
                rejection = Rejection("fingerprint", f"not recording {recording}: {exc}")
            else:
                rejection = fingerprint_rejection(recording, found)
            finally:
                time.sleep(ACOUSTID_DELAY)
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


def recheck_days() -> int:
    try:
        return int(os.environ.get("ACOUSTID_RECHECK_DAYS", "") or RECHECK_DAYS)
    except ValueError:
        logger.error("ACOUSTID_RECHECK_DAYS must be an integer — using %d", RECHECK_DAYS)
        return RECHECK_DAYS


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_sidecar(sidecar: Path) -> tuple[str, dict[str, float]]:
    """A ``.reason`` sidecar's first line (``<reason>: <detail>``) and the
    re-check's ``<key>: <UTC time>`` lines after it (homelab#2546): ``held``
    (since when), ``checked`` (the last lookup) and ``stopped`` (past
    :data:`RECHECK_DAYS`).  Readers of the reason read the first line only."""
    lines = sidecar.read_text(encoding="utf-8").splitlines()
    stamps: dict[str, float] = {}
    for line in lines[1:]:
        key, _, value = line.partition(": ")
        try:
            stamps[key.strip()] = datetime.fromisoformat(value.strip()).timestamp()
        except ValueError:
            continue
    return (lines[0] if lines else ""), stamps


def write_sidecar(sidecar: Path, first: str, stamps: Mapping[str, float]) -> None:
    sidecar.write_text(first + "\n" + "".join(f"{k}: {_utc(v)}\n" for k, v in stamps.items()), encoding="utf-8")


def recheck_unknown(rejected_dir: Path, inbox: Path, songs: dict[str, dict] | None = None,
                    recordings: Callable[[Path], Mapping[str, float]] = acoustid_recordings,
                    now: float | None = None, days: int | None = None) -> Counter:
    """Ask AcoustID again about the album request fallback downloads held in
    *rejected_dir*/``request/`` as :data:`UNKNOWN` (homelab#2546), each at most
    once every :data:`RECHECK_SECONDS`, under :func:`fingerprint_rejection`'s
    rule.  AcoustID now names the track's own recording at
    :data:`FINGERPRINT_MIN_SCORE` or more: the file moves to *inbox*/``request/``
    as a verified download does, and imports this scan (its URL is held by the
    inbox until then, and by the library after).  It names other recordings: the
    reason becomes ``fingerprint``, never asked again.  Still nothing: the
    sidecar records the check.  A failed lookup changes nothing and ends the
    pass, so a down AcoustID costs one timeout a scan, not one per file.

    A file held *days* (:func:`recheck_days`) or more is no longer asked about:
    it stays held, logged once (``stopped``), for the operator.  A file whose
    request was reset or answered is skipped, as is every file while the album
    state can't be read.  The fingerprint is computed again each time (about a
    second); the length and silence checks it already passed aren't repeated.
    Returns the outcomes (:data:`RECHECKS`)."""
    from music_fetch.album_requests import SOURCE, url_recording  # noqa: PLC0415

    now = time.time() if now is None else now
    days = recheck_days() if days is None else days
    outcomes: Counter = Counter({o: 0 for o in RECHECKS})
    try:
        sidecars = sorted((rejected_dir / SOURCE).rglob("*.reason"))
    except OSError:
        return outcomes
    for sidecar in sidecars:
        audio = sidecar.with_name(sidecar.name.removesuffix(".reason"))
        try:
            first, stamps = read_sidecar(sidecar)
            written = sidecar.stat().st_mtime
        except OSError:
            continue
        if first.split(":", 1)[0].strip() != UNKNOWN or "stopped" in stamps or not audio.is_file():
            continue
        # A reject from before the re-check has no stamps: held since its sidecar was written.
        stamps.setdefault("held", written)
        if now - stamps["held"] >= days * 86400:
            write_sidecar(sidecar, first, {**stamps, "stopped": now})
            outcomes["stopped"] += 1
            logger.warning("  [UNKNOWN] %s: %s — held %d+ days as unknown to AcoustID; no longer re-checked, "
                           "the operator decides", SOURCE, audio.name, days)
            continue
        if now - stamps.get("checked", stamps["held"]) < RECHECK_SECONDS:
            continue
        url = song_url(audio)
        recording = url_recording(url)
        if songs is None:
            songs = request_songs()
            if songs is None:
                break
        if recording is None or (url or "") not in songs:
            continue  # its request was reset or answered: nothing wants it imported
        try:
            found = recordings(audio)
        except LookupFailed as exc:
            outcomes["failed"] += 1
            logger.warning("  [GUARD] %s: %s: AcoustID re-check failed (%s); the held files are re-checked at "
                           "the next scan", SOURCE, audio.name, exc)
            break
        except ValueError as exc:
            rejection: Rejection | None = Rejection("fingerprint", f"not recording {recording}: {exc}")
        else:
            rejection = fingerprint_rejection(recording, found)
        finally:
            time.sleep(ACOUSTID_DELAY)
        if rejection is None:
            dest = inbox / audio.relative_to(rejected_dir)
            if dest.exists():
                logger.warning("  [GUARD] %s: %s — AcoustID names recording %s on re-check, but %s exists; "
                               "left held", SOURCE, audio.name, recording, dest)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(audio), dest)
            sidecar.unlink(missing_ok=True)
            outcomes["verified"] += 1
            logger.info("  [VERIFIED] %s: %s — AcoustID names recording %s on re-check", SOURCE, audio.name,
                        recording)
            continue
        stamps["checked"] = now
        if rejection.reason == UNKNOWN:
            write_sidecar(sidecar, first, stamps)
            outcomes[UNKNOWN] += 1
            logger.info("  [UNKNOWN] %s: %s — still unknown to AcoustID on re-check", SOURCE, audio.name)
            continue
        write_sidecar(sidecar, f"{rejection.reason}: {rejection.detail}", stamps)
        outcomes[rejection.reason] += 1
        logger.warning("  [REJECT] %s: %s — %s: %s, on re-check (was %s)", SOURCE, audio.name, rejection.reason,
                       rejection.detail, UNKNOWN)
        _reject_event(SOURCE, audio, rejection, audio)
    if any(outcomes.values()):
        logger.info("AcoustID re-check of held unknown downloads: %d verified, %d now fingerprint, %d still "
                    "unknown, %d failed lookup(s), %d no longer re-checked (%d+ days)", outcomes["verified"],
                    outcomes["fingerprint"], outcomes[UNKNOWN], outcomes["failed"], outcomes["stopped"], days)
    return outcomes


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
