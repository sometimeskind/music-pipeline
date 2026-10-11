"""The album copy supersedes loose copies that predate it (#266).

A Usenet album import is the album: when the library already holds one of its
tracks as a loose item (a spotdl download for another playlist, say
``aaaaaah`` → *Simple Twist of Fate*), the album item replaces it.  The album
item takes the loose item's ``sources``, ``spotify_ids``, ISRCs and
``mb_trackid`` (so Navidrome keeps its play counts and stars); the loose file
moves to ``quarantine/replaced/`` and its row leaves the database, like a
``music-audit-dupes`` merge.  The ``.m3u`` files are then regenerated, so every
playlist that listed the loose item lists the album item.

Only identity decides which album item replaces a loose one (#255), first hit wins:

1. a shared Spotify track ID, except one ``tag_album_ids`` gave the album
   item by title words (``words_ids``);
2. a shared ISRC;
3. the same MusicBrainz recording ID;
4. disc and track: the loose item sits on the album's Spotify album (its
   ``spotify_album_id``, set by canon from Spotify's numbering) at the slot an
   album item with no Spotify ID holds, and the release has Spotify's track
   count (``tag_album_ids``' guard: editions renumber tracks).  Disjoint
   ISRCs veto it.

Title words never supersede.  A loose item on the album that no rung matches
is kept and logged ``[KEEP]``; ``music-audit-dupes`` compares the pair's
fingerprints.  Loose means not ``via=usenet``: another release's item is an
album copy too, and a manually imported item (any other ``via``) is never
replaced (``[MANUAL]``).

The album completion runs it after ``tag_album_ids`` and canon, for the items
the import added; ``music-canon-albums`` runs it for every album record's
successful release (dry run by default).
"""

from __future__ import annotations

import dataclasses
import logging
import os
from collections import Counter
from pathlib import Path

from music_scan.identity import item_isrcs, item_spotify_ids, split_list

logger = logging.getLogger(__name__)

MANAGED_VIA = frozenset({"spotdl", "usenet"})


@dataclasses.dataclass
class Pair:
    album: object  # the album item, kept
    loose: list  # the older items it replaces
    how: list  # per loose item, the rung that matched


def _path(item) -> str:
    return os.fsdecode(item.path)


def _album_id(release: list) -> str | None:
    """The Spotify album most of *release* was canonicalised onto."""
    on = Counter(i.get("spotify_album_id") for i in release if i.get("spotify_album_id"))
    return min(on, key=lambda a: (-on[a], a)) if on else None


def _slot(item) -> tuple[int, int]:
    return item.disc or 1, item.track or 0


def _id_rung(loose, album) -> str | None:
    ids = item_spotify_ids(album) - set(split_list(album.get("words_ids")))
    if ids & item_spotify_ids(loose):
        return "spotify id"
    if item_isrcs(album) & item_isrcs(loose):
        return "isrc"
    if album.get("mb_trackid") and album.get("mb_trackid") == loose.get("mb_trackid"):
        return "recording id"
    return None


def plan(release: list, library: list, tracks_count: int) -> tuple[list[Pair], list]:
    """``(pairs, kept)``: which *library* items each item of *release* (one
    import's usenet items) supersedes, and the loose items on its album no
    rung matched.  Leaves the items as they were."""
    mine = {i.id for i in release}
    album_id = _album_id(release)
    # Positional rung: the release's items with no Spotify ID, by their own numbering.
    numbered = {_slot(i): i for i in release
                if tracks_count and not item_spotify_ids(i) and i.tracktotal == tracks_count}
    pairs: dict[int, Pair] = {}
    kept = []
    for loose in sorted(library, key=lambda i: i.id):
        if loose.id in mine or loose.get("via") == "usenet":
            continue
        on_album = album_id is not None and loose.get("spotify_album_id") == album_id
        match, how = None, None
        for album in release:
            how = _id_rung(loose, album)
            if how:
                match = album
                break
        if match is None and on_album and numbered:
            album = numbered.get(_slot(loose))
            if album is not None and not (item_isrcs(album) and item_isrcs(loose)
                                          and not item_isrcs(album) & item_isrcs(loose)):
                # One loose item per slot: another one there is another copy, kept.
                match, how = numbered.pop(_slot(loose)), "disc/track"
        if match is None:
            if on_album:
                kept.append(loose)
            continue
        if (loose.get("via") or "") not in MANAGED_VIA:
            logger.info("  [MANUAL] %s %s: the same track as album item %s (%s), but imported by hand; kept",
                        loose.id, _path(loose), match.id, how)
            continue
        pair = pairs.setdefault(match.id, Pair(match, [], []))
        pair.loose.append(loose)
        pair.how.append(how)
    return list(pairs.values()), kept


def log(pairs: list[Pair], kept: list, apply: bool) -> None:
    for p in pairs:
        for loose, how in zip(p.loose, p.how):
            logger.info("  [SUPERSEDE] %s %s ← %s %s (%s; playlists %s)%s", p.album.id, _path(p.album), loose.id,
                        _path(loose), how, loose.get("sources") or "-", "" if apply else " (dry run)")
    for loose in kept:
        logger.info("  [KEEP] %s %s — %s %s: on the album, but no ID matches an item of the release; kept",
                    loose.id, loose.get("artist"), loose.get("title"), _path(loose))


def apply_pairs(pairs: list[Pair], replaced_dir: Path | None = None) -> int:
    """Merge each pair's loose items onto its album item.  Returns the count replaced."""
    from music_scan.dupes import REPLACED, merge  # noqa: PLC0415

    n = 0
    for p in pairs:
        # The loose item's recording ID wins: Navidrome keys play counts and stars on it.
        mbid = next((i.get("mb_trackid") for i in p.loose if i.get("mb_trackid")), None)
        retag = bool(mbid) and p.album.get("mb_trackid") != mbid
        if retag:
            p.album["mb_trackid"] = mbid
        merge(p.album, p.loose, replaced_dir or REPLACED)
        if retag:
            p.album.try_write()
        n += len(p.loose)
    return n


def after_import(lib, release: list, tracks_count: int) -> int:
    """The album completion's hook: supersede what *release* replaces.  Returns the count."""
    pairs, kept = plan(release, lib.all_items(), tracks_count)
    log(pairs, kept, apply=True)
    return apply_pairs(pairs)


def release_items(items: list, record: dict) -> list:
    """The usenet *items* *record*'s successful release added (attributed as
    ``music-rollback-releases`` does)."""
    from music_scan.rollback import _claims, _from_success  # noqa: PLC0415

    return [i for i in items if i.get("via") == "usenet" and _claims(record, i) and _from_success(record, i.added or 0)]


def backfill(lib, records: dict[str, dict], apply: bool, replaced_dir: Path | None = None) -> list[Pair]:
    """Supersede for every album record's successful release; with *apply*, merge."""
    done: list[Pair] = []
    for record in records.values():
        items = lib.all_items()
        release = release_items(items, record)
        if not release:
            continue
        pairs, kept = plan(release, items, record.get("tracks_count") or 0)
        if pairs or kept:
            logger.info("[ALBUM] %s — %s: %d loose item(s) superseded, %d kept", record.get("artist"),
                        record.get("name"), sum(len(p.loose) for p in pairs), len(kept))
        log(pairs, kept, apply)
        if apply:
            apply_pairs(pairs, replaced_dir)
        done += pairs
    return done
