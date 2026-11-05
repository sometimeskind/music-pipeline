"""music-audit-grabs: albums held from a release the album-name check now refuses (homelab#2656).

An album's name can share a word with its artist, and that word was enough for
any title by the artist: *Curtis Live!* took ``Curtis Mayfield-Live In Europe-…``,
13 files for an 18-track album.  The tick refuses such a title now
(:func:`~music_fetch.usenet.named_by_artist`); this audit runs the same check over
the release each album in the library came from, its stored ``candidate.title``,
since a wrong album whose tags all name the right artist shows up nowhere else.

An album is checked when the library holds its release: ``imported``, or
``fallback``/``filled`` from a partial release (the tick's own test, which keeps
it from being grabbed again).  Records with no stored title, and ones matched on
a search override or on size alone, have no artist part to read: counted, not
listed.  So are the albums the library doesn't hold from a release, by status: a
clean result says nothing about them.

The output is for a person deciding what to change, and a listed album may be
right: the title then only says less than the album's name.  Check its tracks
before the fix, which is the album's: ``reset_album`` with ``reimport=true``.
Read-only: the album store, no indexer call, no writes, no events.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
from collections import Counter
from collections.abc import Mapping

from music_fetch import albums
from music_fetch.usenet import Release, named_by_artist

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class Flagged:
    """One album held from a release that doesn't name it."""

    key: str
    artist: str
    album: str
    title: str
    why: str


@dataclasses.dataclass
class Report:
    flagged: list[Flagged]
    compared: int = 0
    no_title: int = 0
    """Albums in the library with no release title stored."""
    no_artist: int = 0
    """Albums matched on a search override or on size alone."""
    skipped: Counter[str] = dataclasses.field(default_factory=Counter)
    """Albums the library doesn't hold from a release, by status."""


def audit(records: Mapping[str, dict], search_words: Mapping[str, str]) -> Report:
    """Check every album the library holds from a release; *search_words* are
    the search overrides by album key."""
    report = Report([])
    for key, record in records.items():
        if not albums.from_release(record):
            report.skipped[record.get("status") or "no status"] += 1
            continue
        title = (record.get("candidate") or {}).get("title")
        if not title:
            report.no_title += 1
            continue
        search = albums.plan(key, record, dict(search_words))
        if not search.artist or search.any_album:
            report.no_artist += 1
            continue
        report.compared += 1
        release = Release(guid="", title=title, size=0, indexer_id=0, download_url="", categories=[])
        why = named_by_artist(release, search.artist, search.album)
        if why:
            report.flagged.append(Flagged(key, record["artist"], record["name"], title, why))
    return report


def log_report(report: Report) -> None:
    """``[NOTALBUM]`` per album with its fix, then the counts."""
    for f in sorted(report.flagged, key=lambda f: (f.artist.casefold(), f.album.casefold(), f.key)):
        logger.info('[NOTALBUM] %s: %s — %s came from "%s" (%s): check its tracks, then reset_album with reimport=true',
                    f.key, f.artist, f.album, f.title, f.why)
    logger.info("%d album(s) held from a release the album-name check refuses, of %d compared; not compared: %d with "
                "no release title stored, %d matched on a search override or on size alone; not held from a release, "
                "so not read: %s. A clean result says nothing about the albums not compared or not read. The title "
                "may only say less than the album's name: check the tracks before a reset",
                len(report.flagged), report.compared, report.no_title, report.no_artist,
                albums.by_status(report.skipped))


def run() -> Report:
    """Audit the album store."""
    state = albums.State.load()
    report = audit(state.albums, albums._search_overrides(state))
    log_report(report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(prog="music-audit-grabs", description=__doc__.splitlines()[0])
    parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    run()
