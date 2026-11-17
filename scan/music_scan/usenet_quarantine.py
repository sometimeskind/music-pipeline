"""What an album import's job leaves in ``quarantine/usenet/`` (homelab#2667).

An album import quarantines the files of its job that beets couldn't match to
``quarantine/usenet/<playlist>/<job>/`` (:func:`music_scan.scan.quarantine_inbox_leftovers`),
imports them as they are (:func:`music_scan.release.import_rest`), and the
files beets skips even then stay there.  A failed release's folder is removed
unless it was the album's last try (``music_fetch.albums.complete``); nothing
removed a successful import's, so the folder only grew.

:func:`purge` runs after every scan, beside ``release.purge_replaced``, on the
same ``REPLACED_RETENTION_DAYS`` (default 14; ``0`` turns it off), counted from
the move (the file's ctime).  A file goes only when both hold:

- **its job imported**: a library item came from that job folder
  (``via=usenet``, ``usenet_job`` the folder's name, the playlist in its
  ``sources``).  A folder no item came from (the album's last failed release,
  kept for review; a release from before ``usenet_job`` existed; a copy retired
  since) stays whole: its album didn't import from it.
- **it is junk or superseded**: *junk* holds no audio, so it is the only copy
  of nothing: an empty file, a file under 1 MiB no tag reader can open or that
  says it has no length, or one truncated to under 8 kbps over the length its
  header states.  *Superseded*: the library holds its recording by its
  identity tags (a Spotify ID or ISRC, or a MusicBrainz recording at its
  length: :class:`music_scan.identity.Recordings`, the rule
  ``quarantine/replaced/``'s purge uses).

Anything else is an **only copy** and stays, whatever its age: a person
decides on it.  An emptied job folder, and an emptied playlist folder, go too.

``music-album-audit-releases --usenet`` (the Music MCP's ``audit_releases``
with ``usenet=true``) lists every file with its verdict, its reason and its
size, and what the scan would delete now, later and never.  It deletes nothing.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from music_fetch import events

logger = logging.getLogger(__name__)

USENET = Path("/root/Music/quarantine/usenet")

# Junk: under this size, a file no tag reader opens, or one with no length, holds no audio.
STUB_BYTES = 1 << 20
# Junk: a file this far under its header's length is truncated (the lowest real encodes are ~32 kbps).
TRUNCATED_BPS = 8000

# Verdict kinds.  JUNK and SUPERSEDED go once due; ONLY_COPY and NOT_IMPORTED stay.
JUNK, SUPERSEDED, ONLY_COPY, NOT_IMPORTED = "junk", "superseded", "only_copy", "not_imported"
KINDS = (JUNK, SUPERSEDED, ONLY_COPY, NOT_IMPORTED)


@dataclasses.dataclass(frozen=True)
class Verdict:
    """One file under ``quarantine/usenet/``: its *kind*, why, and whether the purge deletes it now."""

    path: Path
    job: str  # <playlist>/<job>, or the path's first part when it sits in no job folder
    size: int
    kind: str
    why: str
    due: bool = False  # deletable and past the retention
    left_days: float = 0.0  # days until it is due, when deletable and not yet due
    artist: str = ""
    title: str = ""

    @property
    def deletable(self) -> bool:
        return self.kind in (JUNK, SUPERSEDED)


def imported_jobs(items: Iterable) -> set[tuple[str, str]]:
    """``(playlist, job folder)`` of every library item an album import brought in."""
    from music_scan.identity import split_list  # noqa: PLC0415

    jobs = set()
    for item in items:
        job = item.get("usenet_job")
        if item.get("via") == "usenet" and job:
            jobs.update((playlist, job) for playlist in split_list(item.get("sources")))
    return jobs


def _audio_files(root: Path) -> list[Path]:
    from music_scan.release import audio_files  # noqa: PLC0415

    return audio_files(root) if root.is_dir() else []


def _junk(f: Path, size: int, tags) -> str:
    """Why *f* holds no audio, or ``""`` when it may."""
    if size == 0:
        return "empty"
    length = tags.length if tags is not None else None
    if size < STUB_BYTES and tags is None:
        return f"unreadable, {size} bytes"
    if size < STUB_BYTES and not length:
        return f"no length, {size} bytes"
    if length and size * 8 / length < TRUNCATED_BPS:
        return f"truncated: {size} bytes for {length:.0f} s"
    return ""


def judge(root: Path, items: list, days: int, now: float | None = None) -> list[Verdict]:
    """A :class:`Verdict` for every audio file under *root* (``quarantine/usenet/``),
    by *items* (the library's) and the retention *days* (``0``: nothing is due)."""
    from music_scan.identity import Recordings  # noqa: PLC0415
    from music_scan.playlists import _file_tags  # noqa: PLC0415
    from music_scan.release import _moved_at  # noqa: PLC0415

    now = time.time() if now is None else now
    imported = imported_jobs(items)
    library: Recordings | None = None
    verdicts = []
    for f in _audio_files(root):
        parts = f.relative_to(root).parts
        job = "/".join(parts[:2]) if len(parts) > 2 else "/".join(parts[:-1]) or "."
        try:
            st = f.stat()
        except OSError as exc:
            logger.warning("  [USENET] %s: %s", f, exc)
            continue
        if len(parts) <= 2 or (parts[0], parts[1]) not in imported:
            verdicts.append(Verdict(f, job, st.st_size, NOT_IMPORTED, "no library item came from its job folder"))
            continue
        tags = _file_tags(f) if st.st_size else None
        artist, title = (tags.artist, tags.title) if tags is not None else ("", "")
        why = _junk(f, st.st_size, tags)
        kind = JUNK
        if not why:
            if library is None:
                library = Recordings(items)
            if tags is not None and library.holds(tags.recording_ids, tags.length):
                kind, why = SUPERSEDED, "the library holds its recording"
            else:
                kind = ONLY_COPY
                why = ("unreadable" if tags is None else
                       "no library item holds its recording" if tags.recording_ids else
                       "no identity tags (Spotify ID, ISRC or MusicBrainz recording) say the library holds it")
                verdicts.append(Verdict(f, job, st.st_size, kind, why, artist=artist, title=title))
                continue
        age = (now - _moved_at(f)) / 86400
        due = days > 0 and age > days
        verdicts.append(Verdict(f, job, st.st_size, kind, why, due=due,
                                left_days=0.0 if due or days <= 0 else days - age, artist=artist, title=title))
    return verdicts


def counts(verdicts: list[Verdict]) -> dict[str, int]:
    """Files by kind, every kind present."""
    n = Counter(v.kind for v in verdicts)
    return {k: n[k] for k in KINDS}


def _prune(job_dir: Path) -> None:
    """Remove the empty folders of *job_dir*, then it and its playlist folder when they are empty."""
    subs = sorted((d for d in job_dir.rglob("*") if d.is_dir()), key=lambda d: len(d.parts), reverse=True)
    for d in [*subs, job_dir, job_dir.parent]:
        try:
            d.rmdir()
        except OSError:
            pass


def purge(root: Path, items: list, days: int, now: float | None = None) -> dict[str, int]:
    """Delete the junk and superseded files of imported jobs under *root* that
    have sat there more than *days* days, each a ``purged`` event, and the
    folders that leaves empty.  Never an only copy, never a file of a job no
    library item came from.  Returns the files left by kind, their ``bytes``,
    and the count ``purged``."""
    verdicts = judge(root, items, days, now)
    purged, left = 0, []
    touched: set[Path] = set()  # the job folders a file left
    for v in verdicts:
        if not v.due:
            left.append(v)
            continue
        try:
            v.path.unlink()
        except OSError as exc:
            logger.warning("  [PURGE] %s: %s", v.path, exc)
            left.append(v)
            continue
        purged += 1
        touched.add(root.joinpath(*v.path.relative_to(root).parts[:2]))
        message = f"[PURGE] {v.path}: {v.kind} ({v.why}), in quarantine/usenet {days}+ days, its job imported"
        logger.info("  %s", message)
        events.record("purged", message, source="release",
                      data={"path": str(v.path), "artist": v.artist, "title": v.title, "why": v.kind, "days": days})
    for job_dir in sorted(touched):
        _prune(job_dir)
    result = counts(left)
    result["bytes"] = sum(v.size for v in left)
    if purged or left:
        logger.info("quarantine/usenet: deleted %d file(s) of imported jobs; %d left (%s), %s", purged, len(left),
                    ", ".join(f"{n} {k}" for k, n in result.items() if k in KINDS and n), _size(result["bytes"]))
    result["purged"] = purged
    return result


def _size(n: int) -> str:
    return f"{n / (1 << 20):.1f} MiB"


def report(root: Path, items: list, days: int, now: float | None = None) -> dict[str, int]:
    """Log every file under *root* by job folder, with its verdict, why, and
    when the scan deletes it; then the totals.  Deletes nothing.  Returns the
    files by kind and ``due``, ``later`` and ``kept`` with their bytes."""
    verdicts = judge(root, items, days, now)
    by_job: dict[str, list[Verdict]] = {}
    for v in verdicts:
        by_job.setdefault(v.job, []).append(v)
    for job in sorted(by_job, key=str.casefold):
        vs = by_job[job]
        size = sum(v.size for v in vs)
        if vs[0].kind == NOT_IMPORTED:
            logger.info("[JOB] %s: %d file(s), %s; no library item came from this job: kept whole", job, len(vs),
                        _size(size))
            continue
        logger.info("[JOB] %s: %d file(s), %s; its album imported from this job", job, len(vs), _size(size))
        for v in vs:
            when = ("the next scan deletes it" if v.due else
                    f"the scan deletes it in {v.left_days:.0f} day(s)" if v.deletable and days > 0 else
                    "kept (REPLACED_RETENTION_DAYS is 0)" if v.deletable else "kept")
            label = {JUNK: "JUNK", SUPERSEDED: "SUPERSEDED", ONLY_COPY: "ONLYCOPY"}[v.kind]
            logger.info("  [%s] %s: %s; %s", label, v.path.relative_to(root), v.why, when)
    due = [v for v in verdicts if v.due]
    later = [v for v in verdicts if v.deletable and not v.due]
    kept = [v for v in verdicts if not v.deletable]
    result = counts(verdicts)
    for name, vs in (("due", due), ("later", later), ("kept", kept)):
        result[name] = len(vs)
        result[f"{name}_bytes"] = sum(v.size for v in vs)
    only = [v for v in kept if v.kind == ONLY_COPY]
    logger.info("[SUMMARY] %s: %d file(s), %s, in %d job folder(s). The scan deletes %d (%s) now and %d (%s) once "
                "they have sat %d days; it keeps %d only copies (%s) and %d file(s) (%s) of %d job folder(s) no "
                "library item came from", root, len(verdicts), _size(sum(v.size for v in verdicts)), len(by_job),
                len(due), _size(result["due_bytes"]), len(later), _size(result["later_bytes"]), days, len(only),
                _size(sum(v.size for v in only)), len(kept) - len(only),
                _size(result["kept_bytes"] - sum(v.size for v in only)),
                len({v.job for v in kept if v.kind == NOT_IMPORTED}))
    return result


def retention_days() -> int:
    from music_scan.release import retention_days as days  # noqa: PLC0415

    return days()


def main(root: Path = USENET) -> None:
    """``music-album-audit-releases --usenet``: :func:`report` over the library."""
    from music_scan.library import LIBRARY_DB, MusicLibrary  # noqa: PLC0415

    with MusicLibrary(LIBRARY_DB) as lib:
        items = list(lib.all_items())
    report(root, items, retention_days())
