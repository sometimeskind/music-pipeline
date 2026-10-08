"""Roll back a failed Usenet release's unmatched tracks (#238).

A release that album mode's ``complete()`` blocklists may already have put
tracks in the library: beets imports what it can and skips the rest.  Those
matching a playlist entry stay (the next release only fills the gaps); the
rest, an expanded edition's 5.1 mixes and outtakes, match nothing and would
sit in the library forever, never canonicalised or listed.  Rolling back moves
their files to ``quarantine/replaced/`` (never deleted, #202) and drops their
rows, like a ``music-audit-dupes`` merge.

"Matches an entry" is the #228 resolver's whole-library rung: a Spotify ID or
ISRC of any entry in any playlist's ``.spotdl``.

Bonus tracks of a *successful* release (a deluxe edition's extras) stay as
local-only items (operator decision on #238): only failed releases roll back.

``music-rollback-releases`` cleans up the ones imported before this existed:
``via=usenet`` items matching no entry that were not added by their album's
successful release.  An item is attributed to an album record by album name
(edition suffixes dropped) and artist; it belongs to the successful release
when it was added between that record's last grab and its import, and to a
failed one when it was added before the last grab or the album never
succeeded.  Items no record claims are listed and kept.  A failed release's
item that is one of its album's tracks the library still lacks (same title,
the artist within a ``feat.`` credit, #240) is linked to that track's Spotify
ID instead of quarantined.  Dry run by default; ``--apply`` links,
quarantines and regenerates the playlists (#218).
"""

from __future__ import annotations

import argparse
import logging
import shutil
from datetime import datetime
from pathlib import Path

from music_fetch.albums import FALLBACK, FILLED, IMPORTED
from music_fetch.usenet import clean_album, words
from music_scan.audit import REPLACED, item_path, pipeline_lock
from music_scan.dupes import _free
from music_scan.identity import (
    ItemIndex, PlaylistTrack, add_to_list, item_isrcs, item_spotify_ids, release_match,
)
from music_scan.playlists import playlist_entries

logger = logging.getLogger(__name__)

def entry_keys(spotdl_dir: Path) -> tuple[set[str], set[str]]:
    """Spotify IDs and ISRCs of every entry in every ``.spotdl`` playlist."""
    ids: set[str] = set()
    isrcs: set[str] = set()
    for spotdl_file in sorted(spotdl_dir.glob("*.spotdl")):
        for entry in playlist_entries(spotdl_file):
            if entry.song_id:
                ids.add(entry.song_id)
            if entry.isrc:
                isrcs.add(entry.isrc)
    return ids, isrcs


def unmatched(items, ids: set[str], isrcs: set[str]) -> list:
    """The *items* whose Spotify IDs and ISRCs are on no playlist entry."""
    return [i for i in items if not (item_spotify_ids(i) & ids or item_isrcs(i) & isrcs)]


def quarantine(items, replaced_dir: Path = REPLACED) -> list[Path]:
    """Move each item's file to *replaced_dir* and drop its row.  Returns where the files went."""
    moved = []
    for item in items:
        old = item_path(item)
        if old.exists():
            backup = _free(replaced_dir / f"{item.id}-{old.name}")
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old), backup)
            moved.append(backup)
            logger.info("  [ROLLBACK] %s %s — %s → %s", item.id, item.get("artist"), item.get("title"), backup)
        else:
            logger.warning("  [ROLLBACK] %s %s — %s: its file %s was already gone",
                           item.id, item.get("artist"), item.get("title"), old)
        item.remove(delete=False)
    return moved


def rollback_release(lib, playlist: str, since: float, spotdl_dir: Path, replaced_dir: Path = REPLACED) -> int:
    """Quarantine the usenet items *playlist*'s import added since *since* that
    match no playlist entry.  Returns the count."""
    fresh = [i for i in lib.items_by_source(playlist) if i.get("via") == "usenet" and (i.added or 0) >= since]
    gone = unmatched(fresh, *entry_keys(spotdl_dir))
    quarantine(gone, replaced_dir)
    return len(gone)


# ---------------------------------------------------------------------------
# music-rollback-releases: the cleanup for releases that failed before #238
# ---------------------------------------------------------------------------


def _timestamp(iso: str | None) -> float | None:
    return datetime.fromisoformat(iso).timestamp() if iso else None


def _claims(record: dict, item) -> bool:
    """True when *item* looks like a track of *record*'s album: every word of
    the album name (edition suffixes dropped) and of the artist is on the item."""
    album = set(words(clean_album(record.get("name") or "")))
    artist = set(words(record.get("artist") or ""))
    item_album = set(words(item.get("album") or ""))
    item_artist = set(words(f"{item.get('albumartist') or ''} {item.get('artist') or ''}"))
    return bool(album) and album <= item_album and artist <= item_artist


def _from_success(record: dict, added: float) -> bool:
    """True when an item added at *added* came with *record*'s successful release."""
    status = record.get("status")
    if not (status == IMPORTED or status in (FALLBACK, FILLED) and record.get("fallback_from") == "partial"):
        return False
    grabbed, imported = _timestamp(record.get("grabbed_at")), _timestamp(record.get("imported_at"))
    return grabbed is not None and imported is not None and grabbed <= added <= imported


def failed_release_items(items, records: dict[str, dict]) -> tuple[list, list, list]:
    """Split unmatched usenet *items* into (from a failed release, from a
    successful one, unattributed)."""
    failed, kept, unknown = [], [], []
    for item in items:
        claimed = [r for r in records.values() if _claims(r, item)]
        if not claimed:
            unknown.append(item)
        elif any(_from_success(r, item.added or 0) for r in claimed):
            kept.append(item)
        else:
            failed.append(item)
    return failed, kept, unknown


def links(failed: list, records: dict[str, dict], library: ItemIndex) -> list[tuple[object, str]]:
    """``(item, spotify id)`` for each failed-release item that is one of its
    album's tracks the library lacks (#240): the release had the track, but
    its ``feat.`` credit or a curly apostrophe hid it from the ID tagging.
    Linked, not quarantined.  Each item and each track is taken once."""
    found, pool, done = [], list(failed), set()
    for record in records.values():
        mine = [i for i in pool if _claims(record, i)]
        for tracks in record.get("playlists", {}).values() if mine else ():
            for track in map(PlaylistTrack.from_entry, tracks):
                if not track.song_id or track.song_id in done:
                    continue
                if library.match(track.song_id, track.isrc, words=False)[0] is not None:
                    continue
                item = release_match(track.name, track.artist, mine)
                if item is None:
                    continue
                mine.remove(item)
                pool.remove(item)
                done.add(track.song_id)
                found.append((item, track.song_id))
    return found


def _describe(item) -> str:
    return f"{item.id} {item.get('artist')} — {item.get('title')}  [{item.get('album')}]  {item_path(item)}"


def run(apply: bool = False) -> int:
    """List (and with *apply*, quarantine) failed releases' unmatched items.  Returns the count."""
    from music_fetch.albums import STATE_FILE, State  # noqa: PLC0415
    from music_scan.library import LIBRARY_DB, MusicLibrary  # noqa: PLC0415
    from music_scan.navidrome import trigger_scan  # noqa: PLC0415
    from music_scan.scan import SPOTDL_DIR, regen_playlists  # noqa: PLC0415

    records = State.load(STATE_FILE).albums
    with MusicLibrary(LIBRARY_DB) as lib:
        usenet = [i for i in lib.all_items() if i.get("via") == "usenet"]
        loose = unmatched(usenet, *entry_keys(SPOTDL_DIR))
        failed, kept, unknown = failed_release_items(loose, records)
        linked = links(failed, records, ItemIndex(lib.all_items()))
        failed = [i for i in failed if all(i is not item for item, _ in linked)]
        logger.info("%d usenet item(s), %d on no playlist entry: %d from a failed release (%d of them "
                    "an album track to link), %d from a successful one, %d unattributed",
                    len(usenet), len(loose), len(failed) + len(linked), len(linked), len(kept), len(unknown))
        for item, song_id in linked:
            logger.info("  [LINK] %s → %s", _describe(item), song_id)
        for item in failed:
            logger.info("  [FAILED] %s", _describe(item))
        for item in kept:
            logger.info("  [KEPT] %s — a successful release's extra, local-only", _describe(item))
        for item in unknown:
            logger.info("  [UNKNOWN] %s — no album record claims it, kept", _describe(item))
        if not failed and not linked:
            return 0
        if not apply:
            logger.info("Dry run — nothing changed. Re-run with --apply to link the [LINK] items "
                        "and quarantine the [FAILED] ones.")
            return len(failed) + len(linked)
        with pipeline_lock():
            for item, song_id in linked:
                add_to_list(item, "spotify_ids", song_id)
                item.store()
            quarantine(failed)
            counts = regen_playlists()
        logger.info("Linked %d and rolled back %d item(s); playlists regenerated: %s", len(linked), len(failed),
                    ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    trigger_scan()
    return len(failed) + len(linked)


def main() -> None:
    parser = argparse.ArgumentParser(prog="music-rollback-releases", description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="quarantine the listed items (default: dry run)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    run(apply=args.apply)
