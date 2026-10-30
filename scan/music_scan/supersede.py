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

A loose item canon moved onto the album from a twin release (#214) holds the
twin's Spotify ID, which no album item has, so the album's tracklist (the
record's entries) resolves it (#274):

5. a shared ISRC with a tracklist entry: the album item holding that entry's
   Spotify ID (not by title words);
6. disc and track: the entry at the loose item's slot (canon numbered it
   from the album), each slot once.  Disjoint ISRCs veto it.

A loose item on the album no ID rung matches can still be the same recording,
numbered differently (the release numbered it 15, Spotify 16) or with no IDs on
the release's side (#325):

7. same audio: the release item at the loose item's disc and track, then the
   release's items with its title once edition notes are dropped, the first
   that is a *certain*-tier duplicate of it by ``music-audit-dupes``' own rule
   (:func:`music_scan.dupes.audio_match`: an AcoustID match, BER at most
   ``MATCH_BER``, lengths within ``LENGTH_SLACK`` and the same base title).  A
   loose item or candidate with no fingerprint skips it.  A loose item filed
   under another album (the single or compilation spotdl tagged it with,
   #328) has only the release's items by its artist with its base title for
   candidates, and only when it is the one item of that album in the library
   (the one-track album Navidrome shows beside the real one): a best-of
   spotdl downloaded track by track keeps its tracks.  Never one that belongs
   to an album record (``album_record``: another album's track or extra, the
   album is the unit).

Title words alone never supersede.  A loose item on the album that no rung matches
is kept and logged ``[KEEP]`` with the reason, the same-audio rung's included
(each candidate's veto, or that it was skipped for a missing fingerprint).  The album item then takes the loose item's path
when it sat beside it as a ``.1`` copy.  Loose means not ``via=usenet``: another release's item is an
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
import time
from collections import Counter
from pathlib import Path

from music_scan.identity import PlaylistTrack, item_isrcs, item_spotify_ids, split_list

logger = logging.getLogger(__name__)

MANAGED_VIA = frozenset({"spotdl", "usenet"})


@dataclasses.dataclass
class Pair:
    album: object  # the album item, kept
    loose: list  # the older items it replaces
    how: list  # per loose item, the rung that matched


@dataclasses.dataclass
class Kept:
    item: object  # a loose item on the album no rung matched, or one off it a same-titled item didn't
    why: str
    where: str = "on the album"


def _path(item) -> str:
    return os.fsdecode(item.path)


def _album_id(release: list) -> str | None:
    """The Spotify album most of *release* was canonicalised onto."""
    on = Counter(i.get("spotify_album_id") for i in release if i.get("spotify_album_id"))
    return min(on, key=lambda a: (-on[a], a)) if on else None


def _slot(item) -> tuple[int, int]:
    return item.disc or 1, item.track or 0


def _album_key(item) -> tuple[str, str]:
    """The album *item* is filed under, as Navidrome groups it."""
    return (str(item.get("albumartist") or item.get("artist") or "").casefold(),
            str(item.get("album") or "").casefold())


def _id_rung(loose, album) -> str | None:
    ids = item_spotify_ids(album) - set(split_list(album.get("words_ids")))
    if ids & item_spotify_ids(loose):
        return "spotify id"
    if item_isrcs(album) & item_isrcs(loose):
        return "isrc"
    if album.get("mb_trackid") and album.get("mb_trackid") == loose.get("mb_trackid"):
        return "recording id"
    return None


def _isrcs(item) -> set[str]:
    return {c.upper() for c in item_isrcs(item)}


class _Tracklist:
    """The album's tracklist entries, and the release item holding each one's Spotify ID."""

    def __init__(self, release: list, tracks) -> None:
        self.entries = [e for e in map(PlaylistTrack.from_entry, tracks) if e.song_id]
        self.holder: dict[str, object] = {}  # Spotify ID → release item, not by title words
        self.by_words: dict[str, object] = {}
        for album in sorted(release, key=lambda i: i.id):
            words = set(split_list(album.get("words_ids")))
            for sid in item_spotify_ids(album):
                (self.by_words if sid in words else self.holder).setdefault(sid, album)
        self.taken: set[tuple[int, int]] = set()

    def at(self, slot: tuple[int, int]) -> PlaylistTrack | None:
        return next((e for e in self.entries if (e.disc or 1, e.track or 0) == slot), None)

    def resolve(self, loose) -> tuple[object, str | None, str]:
        """``(album item, rung, why not)`` for *loose* through the tracklist (#274)."""
        isrcs = _isrcs(loose)
        for e in self.entries:
            if e.isrc and e.isrc.upper() in isrcs and e.song_id in self.holder:
                return self.holder[e.song_id], "tracklist isrc", ""
        ids = item_spotify_ids(loose)
        own = next((e for e in self.entries if e.song_id in ids), None)
        if own is not None:
            return None, None, (f"its Spotify ID is the album's track {own.disc or 1}-{own.track or 0}, "
                                f"{self._unheld(own)}")
        slot = _slot(loose)
        e = self.at(slot)
        if e is None:
            return None, None, (f"its Spotify ID is not on the album's tracklist (a twin release's?) and "
                                f"no entry sits at its disc/track {slot[0]}-{slot[1]}")
        album = self.holder.get(e.song_id)
        if album is None:
            return None, None, (f"its Spotify ID is not on the album's tracklist (a twin release's?); "
                                f"track {slot[0]}-{slot[1]} is {e.song_id}, {self._unheld(e)}")
        known = ({e.isrc.upper()} if e.isrc else set()) | _isrcs(album)
        if isrcs and known and not isrcs & known:
            return None, None, (f"its ISRCs {';'.join(sorted(isrcs))} differ from track {slot[0]}-{slot[1]}'s "
                                f"({';'.join(sorted(known))}, album item {album.id})")
        if slot in self.taken:
            return None, None, f"another loose item already took track {slot[0]}-{slot[1]}"
        self.taken.add(slot)
        return album, "tracklist disc/track", ""

    def _unheld(self, e: PlaylistTrack) -> str:
        if e.song_id in self.by_words:
            return f"which album item {self.by_words[e.song_id].id} holds by title words only (#255)"
        return "which no item of the release holds"


def _same_audio(loose, release: list, titled: dict, frames_of, on_album: bool) -> tuple[object, str | None, str]:
    """``(album item, rung, why not)`` for *loose* by its audio (#325): the
    release item at its slot, then those with its base title (*titled*); off
    the album only those by its artist (#328), and no why-not without one."""
    from music_scan.dupes import CERTAIN, _artist_key, _base_title, audio_match  # noqa: PLC0415

    slot = _slot(loose)
    named = titled.get(_base_title(loose), []) if _base_title(loose) else []
    if on_album:
        at = sorted((i for i in release if _slot(i) == slot), key=lambda i: i.id)
        named = [i for i in named if _slot(i) != slot]
    else:
        at, named = [], [i for i in named if _artist_key(i) == _artist_key(loose)]
    if not at and not named:
        return None, None, (f"no release item sits at its disc/track {slot[0]}-{slot[1]} or has its title"
                            if on_album else "")
    if not frames_of(loose):
        return None, None, "it has no fingerprint, so the same-audio rung was skipped"
    vetoes, unprinted = [], []
    for album in [*at, *named]:
        if not frames_of(album):
            unprinted.append(album.id)
            continue
        match = audio_match(loose, album, frames_of)
        if match and match[0] == CERTAIN:
            return album, f"same audio, {match[1]}", ""
        vetoes.append(f"album item {album.id}: {match[1] if match else 'the fingerprints differ'}")
    if unprinted:
        vetoes.append(f"album item(s) {', '.join(map(str, unprinted))} have no fingerprint, "
                      "so the same-audio rung skipped them")
    return None, None, f"no release item has its audio ({'; '.join(vetoes)})"


def plan(release: list, library: list, tracks_count: int, tracks=(),
         decoder=None, frames_of=None) -> tuple[list[Pair], list[Kept]]:
    """``(pairs, kept)``: which *library* items each item of *release* (one
    import's usenet items) supersedes, and the loose items on its album no
    rung matched (and those off it a same-titled release item vetoed).
    *tracks* are the album's tracklist entries (:class:`PlaylistTrack`
    lists); *decoder* decodes fingerprints
    (:func:`music_scan.dupes.decode` by default), or *frames_of* reads them
    from a cache shared across calls.  Leaves the items as they were."""
    from music_scan import dupes  # noqa: PLC0415

    frames_of = frames_of or dupes.frames_reader(decoder or dupes.decode)
    titled: dict[str, list] = {}
    for album in sorted(release, key=lambda i: i.id):
        titled.setdefault(dupes._base_title(album), []).append(album)
    library = list(library)
    # Off the album, only a one-track album's item is a candidate: a best-of keeps its tracks (#328).
    albums = Counter(_album_key(i) for i in library)
    mine = {i.id for i in release}
    tracklist = _Tracklist(release, tracks)
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
        if match is None and on_album:
            if not item_spotify_ids(loose):
                why = "it has no Spotify ID"
            elif not tracklist.entries:
                why = "no ID matches an item of the release, and the record has no tracklist"
            else:
                match, how, why = tracklist.resolve(loose)
            if match is None:
                match, how, audio = _same_audio(loose, release, titled, frames_of, on_album=True)
                if match is None:
                    kept.append(Kept(loose, f"{why}; {audio}"))
        elif match is None and not loose.get("album_record") and albums[_album_key(loose)] == 1:
            match, how, audio = _same_audio(loose, release, titled, frames_of, on_album=False)
            if match is None and audio:
                kept.append(Kept(loose, audio, f"filed under {loose.get('album') or '-'}"))
        if match is None:
            continue
        if (loose.get("via") or "") not in MANAGED_VIA:
            logger.info("  [MANUAL] %s %s: the same track as album item %s (%s), but imported by hand; kept",
                        loose.id, _path(loose), match.id, how)
            continue
        pair = pairs.setdefault(match.id, Pair(match, [], []))
        pair.loose.append(loose)
        pair.how.append(how)
    return list(pairs.values()), kept


def _freed(p: Pair) -> str | None:
    """The path the album item moves to once its loose items are gone: one of theirs (a ``.1`` clash)."""
    dest = os.fsdecode(p.album.destination())
    return dest if dest != _path(p.album) and dest in {_path(i) for i in p.loose} else None


def log(pairs: list[Pair], kept: list[Kept], apply: bool) -> None:
    for p in pairs:
        for loose, how in zip(p.loose, p.how):
            logger.info("  [SUPERSEDE] %s %s ← %s %s (%s; playlists %s)%s", p.album.id, _path(p.album), loose.id,
                        _path(loose), how, loose.get("sources") or "-", "" if apply else " (dry run)")
        if dest := _freed(p):
            logger.info("  [MOVE] %s %s → %s: the path its loose copy held%s", p.album.id, _path(p.album), dest,
                        "" if apply else " (dry run)")
    for k in kept:
        logger.info("  [KEEP] %s %s — %s %s: %s, but %s; kept",
                    k.item.id, k.item.get("artist"), k.item.get("title"), _path(k.item), k.where, k.why)


def apply_pairs(pairs: list[Pair], replaced_dir: Path | None = None) -> int:
    """Merge each pair's loose items onto its album item.  Returns the count replaced."""
    from beets.util import MoveOperation  # noqa: PLC0415

    from music_scan.dupes import REPLACED, merge  # noqa: PLC0415

    n = 0
    for p in pairs:
        # The loose item's recording ID wins: Navidrome keys play counts and stars on it.
        mbid = next((i.get("mb_trackid") for i in p.loose if i.get("mb_trackid")), None)
        retag = bool(mbid) and p.album.get("mb_trackid") != mbid
        if retag:
            p.album["mb_trackid"] = mbid
        freed = _freed(p)
        merge(p.album, p.loose, replaced_dir or REPLACED, why="superseded")
        if retag:
            p.album.try_write()
        # The loose file is in replaced/ now: the album item leaves its .1 path (#274).
        if freed and not os.path.exists(freed):
            p.album.move(operation=MoveOperation.MOVE, store=False)
            p.album.store()
        n += len(p.loose)
    return n


def after_import(lib, release: list, tracks_count: int, tracks=()) -> int:
    """The album completion's hook: supersede what *release* replaces.  Returns the count."""
    pairs, kept = plan(release, lib.all_items(), tracks_count, tracks)
    log(pairs, kept, apply=True)
    return apply_pairs(pairs)


def release_items(items: list, record: dict) -> list:
    """The usenet *items* *record*'s successful release added (attributed as
    ``music-rollback-releases`` does)."""
    from music_scan.rollback import _claims, _from_success  # noqa: PLC0415

    return [i for i in items if i.get("via") == "usenet" and _claims(record, i) and _from_success(record, i.added or 0)]


def _releases(items: list, records: dict[str, dict]) -> dict[str, list]:
    """``{record key: its release's items}``: :func:`release_items` for every
    record at once, through :func:`music_scan.rollback.claimants`."""
    from music_scan.rollback import _from_success, claimants  # noqa: PLC0415

    claiming = claimants(records)
    out: dict[str, list] = {}
    for item in items:
        if item.get("via") != "usenet":
            continue
        for key in claiming(item):
            if _from_success(records[key], item.added or 0):
                out.setdefault(key, []).append(item)
    return out


def backfill(lib, records: dict[str, dict], apply: bool, replaced_dir: Path | None = None) -> list[Pair]:
    """Supersede for every album record's successful release; with *apply*, merge.

    The library is read once, and again only after a merge changed it: a read
    takes ~2 s in the pod, and once per record it took over an hour (homelab#2521)."""
    from music_fetch.albums import album_tracks  # noqa: PLC0415

    from music_scan import dupes  # noqa: PLC0415

    start = time.monotonic()
    reads = 1
    items = lib.all_items()
    releases = _releases(items, records)
    # Keyed by item ID: a merge removes loose items and leaves the album item's fingerprint as it was.
    frames_of = dupes.frames_reader(dupes.decode)
    done: list[Pair] = []
    for key, record in records.items():
        release = releases.get(key)
        if not release:
            continue
        pairs, kept = plan(release, items, record.get("tracks_count") or 0, album_tracks(record),
                           frames_of=frames_of)
        if pairs or kept:
            logger.info("[ALBUM] %s — %s: %d loose item(s) superseded, %d kept", record.get("artist"),
                        record.get("name"), sum(len(p.loose) for p in pairs), len(kept))
        log(pairs, kept, apply)
        if apply and apply_pairs(pairs, replaced_dir):
            reads += 1
            items = lib.all_items()
            releases = _releases(items, records)
        done += pairs
    logger.info("Supersede backfill: %d record(s), %d library read(s) in %.1fs", len(records), reads,
                time.monotonic() - start)
    return done
