"""music-audit-artists: items whose artist shares nothing with their album's (homelab#2649).

beets' singleton match can name another artist's song of the same title: 18518,
Springsteen's *I'm on Fire* on *Born in the U.S.A.*, took a 5000 Volts recording
and its artist.  The import refutes such a match now; this audit finds the ones
already in the library, by the import's own comparison
(:func:`~music_scan.identity.different_artist`), which never calls two names
different when it can't tell.

An item's album is its album record, else its album artist and album name; the
album's artist is the item's album artist and the record's.  A compilation has
no artist to differ from, so these are left out, and counted:

- a record whose ``album_type`` is ``compilation``, or an item whose tags say so
  (:func:`~music_scan.identity.compilation`);
- an album at least half of whose items are by two or more other artists: a
  compilation in all but name.  One other artist is listed, however few items
  the album has, since a single wrong match on a loose track looks like that.

Items with no album name, and names the comparison can't read (no album artist,
another script, Various Artists as a track's artist), are counted, not listed.

The output is for a person deciding what to change, and some of what it lists is
right: a guest with a track of their own, a soundtrack's song by someone else.
The fix is the item's, not the album's: ``recording_candidates``, then
``set_recording``.  Read-only: the beets library and the album store, no
MusicBrainz call, no writes, no events.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
from collections.abc import Iterable

from music_scan.identity import Credit, artist_credit, comparable_artists, compilation, different_artist

logger = logging.getLogger(__name__)

COMPILATION = "compilation"


@dataclasses.dataclass
class Flagged:
    """One item credited to an artist its album's shares nothing with."""

    id: int
    record: str
    albumartist: str
    album: str
    disc: int
    track: int
    title: str
    artist: str
    via: str

    @property
    def album_label(self) -> str:
        return f"{self.albumartist} — {self.album}"


@dataclasses.dataclass
class Report:
    flagged: list[Flagged]
    compared: int = 0
    compilations: int = 0
    """Items on a compilation, by its tags or by its record."""
    mostly_others: int = 0
    """Items on an album at least half by two or more other artists."""
    unreadable: int = 0
    """Items whose artist or album artist the comparison can't read."""
    no_album: int = 0
    """Items with no album name."""

    def albums(self) -> list[tuple[str, list[Flagged]]]:
        """The flagged items grouped by album, albums by name, items by disc and track."""
        groups: dict[tuple, list[Flagged]] = {}
        for f in self.flagged:
            groups.setdefault((f.record, f.albumartist, f.album), []).append(f)
        ordered = sorted(groups.items(), key=lambda kv: (kv[1][0].album_label.casefold(), kv[0][0]))
        return [(key[0], sorted(items, key=lambda f: (f.disc, f.track, f.id))) for key, items in ordered]


def _album_credit(item, record: dict | None) -> Credit:
    """Who *item*'s album credits: its album artist's tags and its record's artist."""
    credit = artist_credit(item, album=True)
    return credit | Credit((record["artist"],)) if record and record.get("artist") else credit


def _flagged(item, key: str) -> Flagged:
    return Flagged(item.id, key, item.get("albumartist") or "", item.get("album") or "", item.get("disc") or 0,
                   item.get("track") or 0, item.get("title") or "", item.get("artist") or "", item.get("via") or "")


def audit(items: Iterable, records: dict[str, dict]) -> Report:
    """Compare every item's artist with its album's, album by album."""
    report = Report([])
    albums: dict[tuple, list] = {}
    for item in items:
        if not item.get("album"):
            report.no_album += 1
            continue
        record = str(item.get("album_record") or "")
        albums.setdefault((record,) if record else ("", item.get("albumartist"), item.get("album")), []).append(item)
    for key, album in albums.items():
        record = records.get(key[0])
        if (record or {}).get("album_type") == COMPILATION or any(map(compilation, album)):
            report.compilations += len(album)
            continue
        credits = [(item, artist_credit(item), _album_credit(item, record)) for item in album]
        others = [item for item, ours, theirs in credits if different_artist(ours, theirs)]
        if len({item.get("artist") for item in others}) > 1 and 2 * len(others) >= len(album):
            report.mostly_others += len(album)
            continue
        readable = sum(comparable_artists(ours, theirs) for _, ours, theirs in credits)
        report.compared += readable
        report.unreadable += len(album) - readable
        report.flagged += [_flagged(item, key[0]) for item in others]
    return report


def log_report(report: Report) -> None:
    """``[ALBUM]`` per album, an ``[ARTIST]`` line per item with its fix, then the counts."""
    albums = report.albums()
    for key, items in albums:
        logger.info("[ALBUM] %s%s: %d item(s)", items[0].album_label, f" (record {key})" if key else "", len(items))
        for f in items:
            logger.info('  [ARTIST] %d %d-%02d "%s" by "%s" (via %s): recording_candidates %d, then set_recording',
                        f.id, f.disc, f.track, f.title, f.artist, f.via or "?", f.id)
    logger.info("%d item(s) on %d album(s) credited to an artist their album's shares nothing with, of %d compared; "
                "left out: %d on compilations, %d on albums mostly by others; not compared: %d whose names can't be "
                "compared, %d with no album. A guest's own track is right as it is: check before set_recording",
                len(report.flagged), len(albums), report.compared, report.compilations, report.mostly_others,
                report.unreadable, report.no_album)


def run() -> Report:
    """Audit the whole library against the album store."""
    from music_fetch.albums import State  # noqa: PLC0415
    from music_scan.library import LIBRARY_DB, MusicLibrary  # noqa: PLC0415

    try:
        records = State.load().albums
    except (OSError, ValueError):
        records = {}
    with MusicLibrary(LIBRARY_DB) as lib:
        report = audit(lib.all_items(), records)
    log_report(report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(prog="music-audit-artists", description=__doc__.splitlines()[0])
    parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    run()
