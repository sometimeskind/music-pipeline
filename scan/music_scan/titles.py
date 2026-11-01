"""music-audit-titles: items titled as another version than Spotify's entry (homelab#2544).

The pipeline imports every file as a singleton, and beets writes the closest
MusicBrainz recording's ``title`` and ``trackdisambig`` over the file's.  When
MusicBrainz holds several recordings of one song (stereo and 5.1, studio and
live, album and outtake) the closest can be a sibling, so an album track shows
as *Oh! Sweet Nuthin' (5.1 mix)* or carries ``trackdisambig`` "live, 1992‐05‐28:
Ahoy Arena…" when the audio is the stereo or studio track.  The import fix
keeps Usenet files' own titles; spotdl imports can still pick a sibling, so
this audit keeps watching.

It lists the items whose ``title`` or ``trackdisambig`` names a version
(:data:`VERSIONS`: 5.1, live, demo, alternate, mono, take, outtake) that
Spotify's entry at the item's slot doesn't name.  Spotify's entry is the
playlist-store song record (homelab#1914) for one of the item's Spotify IDs:
the one on the item's ``spotify_album_id`` (where canon filed it), else the
one canon would prefer (album over single over compilation, then the earliest).
Items with no Spotify ID, or whose IDs no store entry describes, can't be
compared: they are counted, not listed.

A word is a version only when ours names it more often than Spotify's does,
so a song whose name holds it (*Live Forever*, *Take Five*) is not flagged
against the same name, and *Live Forever (Live)* against *Live Forever* is.
``trackdisambig`` holds version notes only, so one of its words counts when
Spotify's version part (after `` - `` or in brackets) lacks it.  Words only
Spotify's title names are not flagged: Spotify's own ``- Live at …`` slot
titled as a studio outtake flags on ``take``, not on ``live``.  A word in
Spotify's album name counts as Spotify's, since a live album's tracks are
often titled bare.

The fix is the album's, not the track's: reset and reimport it.  Read-only:
the beets library, the playlist stores and the album store, no Spotify or
MusicBrainz call, no writes, no events.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import re
import unicodedata
from collections.abc import Iterable

from music_scan.canon import TYPE_RANK
from music_scan.identity import item_spotify_ids, spotify_id

logger = logging.getLogger(__name__)

# The version a title or trackdisambig can name, each with what spells it.
# "5.1" is guarded by digits and dots so a date (2015.1, 5.10) never counts;
# "alt"/"alternative" are spellings of "alternate" (Spotify writes "Alt. Version").
VERSIONS: dict[str, re.Pattern] = {
    "5.1": re.compile(r"(?<![\d.])5\.1(?![\d.])"),
    "live": re.compile(r"\blive\b"),
    "demo": re.compile(r"\bdemos?\b"),
    "alternate": re.compile(r"\balt(?:ernate|ernative)?\b"),
    "mono": re.compile(r"\bmono\b"),
    "take": re.compile(r"\btake\b"),
    "outtake": re.compile(r"\bout-?takes?\b"),
}
# Spotify's version part: after its " - " (any dash), or in brackets.
_SPOTIFY_SUFFIX = re.compile(r"\s[-‐-―]\s(.*)$")
_BRACKETS = re.compile(r"[(\[]([^)\]]*)[)\]]")


def _norm(text: str | None) -> str:
    return unicodedata.normalize("NFKC", text or "").casefold()


def counts(text: str | None) -> dict[str, int]:
    """How often *text* names each of :data:`VERSIONS`."""
    text = _norm(text)
    return {word: len(pattern.findall(text)) for word, pattern in VERSIONS.items()}


def version_part(title: str | None) -> str:
    """Spotify's version notes in *title*: after the first `` - `` and in
    brackets (``Song - Live at X``, ``Song (Mono)``); empty for a bare name."""
    title = title or ""
    parts = [m.group(1) for m in _BRACKETS.finditer(title)]
    if m := _SPOTIFY_SUFFIX.search(title):
        parts.append(m.group(1))
    return " ".join(parts)


def differing(title: str | None, trackdisambig: str | None, spotify: str | None,
              spotify_album: str | None = None) -> list[str]:
    """The versions our *title* or *trackdisambig* names and Spotify's title
    doesn't (see the module docstring), in :data:`VERSIONS` order.  A word in
    Spotify's album name counts as Spotify's: a live album's tracks are often
    titled bare (*Live 1975*, *Live at Leeds*)."""
    ours, notes, album = counts(title), counts(trackdisambig), counts(spotify_album)
    theirs, spotify_notes = counts(spotify), counts(version_part(spotify))
    return [w for w in VERSIONS
            if ours[w] > theirs[w] + album[w] or (notes[w] and not spotify_notes[w] + album[w])]


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def songs_by_id(songs: Iterable[dict]) -> dict[str, list[dict]]:
    """Store song records by Spotify track ID, one per release."""
    by_id: dict[str, dict[str, dict]] = {}
    for song in songs:
        if sid := song.get("song_id") or spotify_id(song.get("url")):
            by_id.setdefault(sid, {}).setdefault(song.get("album_id") or "", song)
    return {sid: list(albums.values()) for sid, albums in by_id.items()}


def spotify_entry(item, by_id: dict[str, list[dict]]) -> dict | None:
    """Spotify's entry at *item*'s slot: the store record for one of its IDs,
    the one on its ``spotify_album_id`` first, then canon's preference
    (``canon.Placement.key`` without the named-today step); None when no store
    entry describes any of its IDs."""
    found = [song for sid in sorted(item_spotify_ids(item)) for song in by_id.get(sid, ())]
    if not found:
        return None
    home = item.get("spotify_album_id")
    return min(found, key=lambda s: (s.get("album_id") != home, TYPE_RANK.get(s.get("album_type"), len(TYPE_RANK)),
                                     s.get("date") or str(s.get("year") or "9999"), s.get("album_id") or ""))


@dataclasses.dataclass
class Flagged:
    """One item titled as a version its Spotify entry doesn't name."""

    id: int
    record: str
    artist: str
    album: str
    disc: int
    track: int
    title: str
    trackdisambig: str
    spotify: str
    words: list[str]

    @property
    def album_label(self) -> str:
        return f"{self.artist} — {self.album}"


@dataclasses.dataclass
class Report:
    flagged: list[Flagged]
    compared: int
    no_id: int
    """Items with no Spotify ID."""
    undescribed: int
    """Items whose Spotify IDs no store entry describes."""

    def albums(self) -> list[tuple[str, list[Flagged]]]:
        """The flagged items grouped by album (its record, else its tags),
        albums by name, items by disc and track."""
        groups: dict[tuple, list[Flagged]] = {}
        for f in self.flagged:
            groups.setdefault((f.record or "", f.artist, f.album), []).append(f)
        ordered = sorted(groups.items(), key=lambda kv: (kv[1][0].album_label.casefold(), kv[0][0]))
        return [(key[0], sorted(items, key=lambda f: (f.disc, f.track, f.id))) for key, items in ordered]


def audit(items: Iterable, songs: Iterable[dict]) -> Report:
    """Compare every item's ``title`` and ``trackdisambig`` with Spotify's
    entry at its slot (:func:`spotify_entry`)."""
    by_id = songs_by_id(songs)
    report = Report([], 0, 0, 0)
    for item in items:
        if not item_spotify_ids(item):
            report.no_id += 1
            continue
        song = spotify_entry(item, by_id)
        if song is None:
            report.undescribed += 1
            continue
        report.compared += 1
        title, notes = item.get("title") or "", item.get("trackdisambig") or ""
        if words := differing(title, notes, song.get("name"), song.get("album_name")):
            report.flagged.append(Flagged(
                item.id, str(item.get("album_record") or ""), item.get("albumartist") or item.get("artist") or "",
                item.get("album") or "", _int(item.get("disc")), _int(item.get("track")), title, notes,
                song.get("name") or "", words))
    return report


def log_report(report: Report, records: dict[str, dict]) -> None:
    """``[ALBUM]`` per album with its reset command when its record is known,
    a ``[VERSION]`` line per item, then the counts."""
    from music_scan.release import album_query  # noqa: PLC0415

    albums = report.albums()
    for key, items in albums:
        first = items[0]
        logger.info("[ALBUM] %s%s: %d item(s)", first.album_label, f" (record {key})" if key else "", len(items))
        if key and (record := records.get(key)):
            name = f"{record.get('artist')} — {record.get('name')}"
            logger.info("    music-album-reset '%s' --reimport", album_query(records, key, name))
        for f in items:
            notes = f" [trackdisambig: {f.trackdisambig}]" if f.trackdisambig else ""
            logger.info('  [VERSION] %d %d-%02d "%s"%s vs Spotify "%s": %s', f.id, f.disc, f.track, f.title, notes,
                        f.spotify, ", ".join(f.words))
    logger.info("%d item(s) on %d album(s) titled as a version Spotify's entry doesn't name, of %d compared; "
                "not compared: %d with no Spotify ID, %d whose IDs no playlist store describes. Fix an album "
                "whole: reset it with --reimport (dry run, then --apply)", len(report.flagged), len(albums),
                report.compared, report.no_id, report.undescribed)


def run() -> Report:
    """Audit the whole library against every playlist store."""
    from music_fetch.albums import State  # noqa: PLC0415
    from music_scan.canon import store_songs  # noqa: PLC0415
    from music_scan.library import LIBRARY_DB, MusicLibrary  # noqa: PLC0415

    try:
        records = State.load().albums
    except (OSError, ValueError):
        records = {}
    with MusicLibrary(LIBRARY_DB) as lib:
        report = audit(lib.all_items(), store_songs())
    log_report(report, records)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(prog="music-audit-titles", description=__doc__.splitlines()[0])
    parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    run()
