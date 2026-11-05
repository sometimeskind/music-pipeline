"""music-audit-zero-wanted: albums imported with none of their wanted tracks found (homelab#2662).

:func:`~music_fetch.albums.wrong_album` doesn't judge an album of fewer than
``WRONG_ALBUM_MIN_TRACKS`` tracks, and a wants record's tracks are only the
wanted ones, so a wrong album with few wanted tracks imports as ``[DONE]``:
*2 of its 2 wanted track(s) not in this release*.  This audit lists the albums
the library holds from a release whose last import found 0 of N.  It reports and
refuses nothing.

The count is the ``import_done`` event's ``found`` and ``total``, or, before
homelab#2662, the ``[DONE]`` line's sentence, which is left out when the release
held every title.  A kept copy's event from before then gives no count at all:
counted, not listed.  So are the albums with no ``import_done`` event (imported
before events began on 2026-10-29, or pruned) and, by status, the ones the
library doesn't hold from a release.

A listed album may be right: its titles may be written otherwise, or the wanted
tracks may be another edition's, as with a deluxe's bonus tracks.  A wrong album
sharing one wanted track isn't 0 of N, so a clean result doesn't clear one.

Read-only: the album store and the events.  It never opens the beets database,
so it can't hold it against an import (homelab#2665).
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime

from music_fetch import albums, db, events

logger = logging.getLogger(__name__)

_IMPORTS = """
SELECT at, spotify_album_id, message, data FROM events
 WHERE kind = 'import_done' AND source = 'albums'
 ORDER BY at, id
"""

# The [DONE] line's note (albums.complete), for an event without found and total.
_NOT_IN_RELEASE = re.compile(r"(\d+) of (?:its (\d+) wanted track\(s\)|Spotify's (\d+) titles) not in this release")


@dataclasses.dataclass
class Flagged:
    """One album whose last import found none of its wanted tracks."""

    key: str
    artist: str
    album: str
    title: str | None
    total: int
    at: datetime


@dataclasses.dataclass
class Report:
    flagged: list[Flagged]
    compared: int = 0
    no_event: int = 0
    """Albums in the library from a release with no ``import_done`` event."""
    no_count: int = 0
    """Albums whose last event says nothing of the titles found."""
    skipped: Counter[str] = dataclasses.field(default_factory=Counter)
    """Albums the library doesn't hold from a release, by status."""


def counted(message: str, data: dict) -> tuple[int, int] | None:
    """``(found, total)`` from one ``import_done`` event, ``(0, 0)`` when the release held
    every title; None when the event doesn't say (a kept copy's, before homelab#2662)."""
    if data.get("total") is not None:
        return data.get("found") or 0, data["total"]
    if match := _NOT_IN_RELEASE.search(message):
        total = int(match[2] or match[3])
        return total - int(match[1]), total
    return None if data.get("why") else (0, 0)


def latest_imports(records: Mapping[str, dict], rows: Iterable[tuple]) -> dict[str, tuple[datetime, str, dict]]:
    """Each album's last ``import_done``: album key → ``(at, message, data)``.  *rows* are
    :data:`_IMPORTS`'s, oldest first; an event names the album by any of its refs."""
    keys = {ref: key for key, record in records.items() for ref in events.album_refs(key, record)}
    latest: dict[str, tuple[datetime, str, dict]] = {}
    for at, spotify_album, message, data in rows:
        key = keys.get(str(data.get("key"))) or keys.get(spotify_album)
        if key is not None:
            latest[key] = (at, message, data)
    return latest


def audit(records: Mapping[str, dict], rows: Iterable[tuple]) -> Report:
    """Check every album the library holds from a release against its last import."""
    latest = latest_imports(records, rows)
    report = Report([])
    for key, record in records.items():
        if not albums.from_release(record):
            report.skipped[record.get("status") or "no status"] += 1
            continue
        if key not in latest:
            report.no_event += 1
            continue
        at, message, data = latest[key]
        count = counted(message, data)
        if count is None:
            report.no_count += 1
            continue
        report.compared += 1
        found, total = count
        if total and not found:
            title = data.get("kept") or (record.get("candidate") or {}).get("title")
            report.flagged.append(Flagged(key, record["artist"], record["name"], title, total, at))
    return report


def log_report(report: Report) -> None:
    """``[NOWANTED]`` per album with its fix, then the counts and what the audit can't see."""
    for f in sorted(report.flagged, key=lambda f: (f.artist.casefold(), f.album.casefold(), f.key)):
        logger.info('[NOWANTED] %s: %s — %s: 0 of its %d wanted track(s) in "%s", imported %s: check its tracks, '
                    "then reset_album with reimport=true if it is another album",
                    f.key, f.artist, f.album, f.total, f.title or "no title stored", f.at.date().isoformat())
    logger.info("%d album(s) imported with 0 of their wanted tracks found, of %d compared; not compared: %d with no "
                "import event, %d whose event gives no count; not held from a release, so not read: %s. A listed "
                "album may be right: title formats can differ, and the wanted tracks may be another edition's, so "
                "check its tracks. This audit can't see an import before 2026-10-29 or one whose event was pruned, "
                "nor a wrong album sharing one wanted track (1 of N is not 0): a clean result doesn't clear those",
                len(report.flagged), report.compared, report.no_event, report.no_count,
                albums.by_status(report.skipped))


def run() -> Report:
    """Audit the album store against the event store."""
    state = albums.State.load()
    with db.connect() as conn:
        rows = conn.execute(_IMPORTS).fetchall()
    report = audit(state.albums, rows)
    log_report(report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(prog="music-audit-zero-wanted", description=__doc__.splitlines()[0])
    parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    run()
