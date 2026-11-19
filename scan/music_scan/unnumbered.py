"""Album items imported with no track number, placed by their audio (homelab#2680).

A Usenet release whose files carry no track numbers imports at track 0, every
item of it (album 582, Prince — *Love Symbol*: 18 files, none numbered, none
matched to a recording).  ``tag_album_ids``' number rung needs a disc and a
track, and its ISRC rung an ISRC, so none of the album's playlist entries could
link, and spotdl downloaded the wanted tracks again.

The file's name is no evidence of its number, and a title is no identity
(#279).  Its fingerprint is: :func:`place` looks the item up on AcoustID and,
when exactly one track of the album's release holds a recording AcoustID links
to the file at :data:`~music_scan.mb_fingerprint.MIN_SCORE` or more, the item
takes that track's disc and number and its recording (``mb_trackid``), with a
``track_placed`` event in the same transaction, and the file's tags are
written so ``beet update`` reads the same back.  The number rung then links
the entry at that slot as it would a numbered file: by position and title
together, never by title alone.

Nothing changes when AcoustID has no confident recording, when none of them is
on the release, when they sit on several of its tracks, when the item's own
``mb_trackid`` names another recording (``recheck_recordings`` settles that
first), when the release's track names another song than the item's title
(an AcoustID collision or a wrong download, as in ``request_fill``), or when
another item of the album holds or takes the same slot.  Each such item stays at
track 0 and is listed with why (``[NOPLACE]``); ``library_status``' shapes
count every item at track 0.

The album's release is the one the files were matched to as a whole at the
import (the record's ``release_files``, what canon tags the items with), else
the item's own ``mb_albumid``.  It runs at two places:

- at the album import (:func:`at_import`), before ``tag_album_ids``, for the
  release's own items: the lookups run before the pipeline slot is taken, the
  writes under it.  An import whose files are numbered looks nothing up.
- in ``music-backfill-ids --albums`` (``backfill_album_ids``), before its
  number rung, for the albums already imported and any import whose lookups
  failed (:func:`plan`, :func:`store`).

Only the items at track 0 cost a lookup (pyacoustid keeps to AcoustID's 3
requests a second; the release is read once per album through the pipeline's
MusicBrainz client).  A failed AcoustID lookup, tried once more, ends the
lookups for the run: the rest stay at track 0 until the next one.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import http.client
import logging
import os
from collections.abc import Callable, Iterable, Mapping

from music_scan.mb_fingerprint import (
    MIN_SCORE,
    FileStat,
    LookupFailed,
    Recordings,
    ReleaseLookup,
    _lookup,
    acoustid_recordings,
    file_stat,
    musicbrainz_releases,
)

logger = logging.getLogger(__name__)

KIND = "track_placed"
EXTRA = "album_extra"  # release.EXTRA: a local-only extra is no item of the release

Lookup = Callable[[str], Recordings]
# The release an item's album was matched to (an MBID), or None.
ReleaseOf = Callable[[object], "str | None"]


@dataclasses.dataclass
class Placed:
    """An item the release places by its fingerprint: the slot and recording it takes."""

    item_id: int
    key: str  # the album record
    release: str
    recording: str
    disc: int
    track: int
    score: float
    title: str  # the release track's title
    file: FileStat = ("", None, None)  # the file at its lookup: the store skips an item whose file changed since


@dataclasses.dataclass
class Placing:
    placed: list[Placed] = dataclasses.field(default_factory=list)
    left: list[tuple[str, int, str, str]] = dataclasses.field(default_factory=list)  # key, item ID, title, why
    unnumbered: int = 0
    failed: bool = False  # AcoustID couldn't be asked: the items after it weren't looked up


def _track(item) -> int:
    try:
        return int(getattr(item, "track", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _disc_track(item) -> tuple[int, int]:
    """As :func:`music_scan.identity.disc_track`: disc 0 counts as disc 1."""
    try:
        disc = int(getattr(item, "disc", 0) or 0)
    except (TypeError, ValueError):
        disc = 0
    return disc or 1, _track(item)


def _title(item) -> str:
    return getattr(item, "title", None) or ""


def _path(item) -> str:
    return os.fsdecode(item.path) if isinstance(item.path, bytes) else str(item.path)


def release_items(items: Iterable) -> dict[str, list]:
    """An album record's release items (``via=usenet``, stamped ``album_record``,
    not a local-only extra) by the record's key."""
    by_key: dict[str, list] = {}
    for item in items:
        if item.get("album_record") and item.get("via") == "usenet" and not item.get(EXTRA):
            by_key.setdefault(str(item.get("album_record")), []).append(item)
    return by_key


def unnumbered(items: Iterable) -> list:
    """The release items of *items* with no track number, each once, in ID order."""
    seen: dict[int, object] = {}
    for group in release_items(items).values():
        for item in group:
            if not _track(item):
                seen.setdefault(item.id, item)
    return [seen[i] for i in sorted(seen)]


def _tracks(release: Mapping):
    from music_scan.candidates import _int, _tracks  # noqa: PLC0415

    for disc, position, track in _tracks(release):
        yield _int(disc), _int(position), track


def place(item, found: Recordings, release: Mapping) -> tuple[tuple[int, int, str, float, str] | None, str]:
    """Where *release* (``release/<id>?inc=recordings``) places *item*, by
    *found* (AcoustID's recordings for its audio): ``((disc, track, recording,
    score, track title), why)``, or ``(None, why not)``.  Never by a title or a
    file name: the recording must be AcoustID's and the release's both."""
    from music_scan.identity import different_song  # noqa: PLC0415

    confident: dict[str, float] = {}
    for score, rec in found:
        if score >= MIN_SCORE:
            confident[rec] = max(score, confident.get(rec, 0.0))
    album = release.get("id") or "?"
    if not confident:
        best = f"best {found[0][1]} {found[0][0]:.2f}" if found else "no recording"
        return None, f"AcoustID has nothing at {MIN_SCORE:.2f} or more ({best})"
    on = [(d, p, t) for d, p, t in _tracks(release) if (t.get("recording") or {}).get("id") in confident]
    if not on:
        return None, f"none of its AcoustID recordings ({', '.join(sorted(confident))}) is on release {album}"
    if len(on) > 1:
        slots = ", ".join(f"{d}-{p}" for d, p, _ in on)
        return None, f"its AcoustID recordings are on {len(on)} tracks of release {album} ({slots})"
    disc, position, track = on[0]
    recording = track["recording"]["id"]
    have = item.get("mb_trackid") or ""
    if have and have != recording:
        return None, (f"its mb_trackid {have} is another recording than the one AcoustID and the release agree on "
                      f"({recording}); recheck_recordings settles that first")
    title = (track.get("recording") or {}).get("title") or track.get("title") or ""
    if different_song(_title(item), title):
        return None, f"[SUSPECT] AcoustID places it at {disc}-{position} of release {album}, {title!r}, another song"
    return (disc, position, recording, confident[recording], title), ""


def plan(todo: list, release_of: ReleaseOf, lookup: Lookup, release_lookup: ReleaseLookup,
         siblings: Iterable = ()) -> Placing:
    """Look each item of *todo* (items at track 0) up and place it on its
    release (*release_of*).  *siblings* are the albums' other items: a slot one
    of them holds is taken already.  Changes nothing."""
    placing = Placing(unnumbered=len(todo))
    taken: dict[tuple[str, int, int], list[int]] = {}
    for item in siblings:
        if _track(item) and item.get("album_record"):
            taken.setdefault((str(item.get("album_record")), *_disc_track(item)), []).append(item.id)
    found_at: dict[tuple[str, int, int], list[Placed]] = {}

    def leave(item, why: str) -> None:
        placing.left.append((str(item.get("album_record") or ""), item.id, _title(item), why))

    for item in todo:
        key = str(item.get("album_record") or "")
        album = release_of(item)
        if not album:
            leave(item, "no release is known for its album (release_files, mb_albumid)")
            continue
        try:
            release = release_lookup(album)
        except (OSError, ValueError, http.client.HTTPException) as exc:  # urllib's errors, a cut or non-JSON body
            leave(item, f"the MusicBrainz lookup of release {album} failed ({exc})")
            continue
        if not release:
            leave(item, f"MusicBrainz has no release {album}")
            continue
        if placing.failed:
            leave(item, "not looked up: AcoustID couldn't be asked")
            continue
        path = _path(item)
        stat = file_stat(path)
        try:
            found = _lookup(lookup, path)
        except LookupFailed as exc:
            placing.failed = True
            leave(item, f"AcoustID couldn't be asked ({exc})")
            continue
        except (OSError, ValueError) as exc:
            leave(item, f"no fingerprint ({exc})")
            continue
        hit, why = place(item, found, release)
        if hit is None:
            leave(item, why)
            continue
        disc, track, recording, score, title = hit
        found_at.setdefault((key, disc, track), []).append(
            Placed(item.id, key, release.get("id") or album, recording, disc, track, score, title, stat))
    titles = {item.id: _title(item) for item in todo}
    for (key, disc, track), placed in sorted(found_at.items()):
        holders = taken.get((key, disc, track), [])
        if len(placed) == 1 and not holders:
            placing.placed.append(placed[0])
            continue
        others = sorted({*holders, *(p.item_id for p in placed)})
        for p in placed:
            rest = ", ".join(str(i) for i in others if i != p.item_id)
            placing.left.append((key, p.item_id, titles.get(p.item_id, ""),
                                 f"AcoustID places it at {disc}-{track}, and so it does item(s) {rest}"
                                 if not holders else f"AcoustID places it at {disc}-{track}, which item(s) {rest} "
                                                     "hold already"))
    placing.left.sort(key=lambda row: row[1])
    return placing


def report(placing: Placing, dry_run: bool = True) -> None:
    """One line per item left at track 0 and, in a dry run, per item it would
    place (:func:`store` logs each it places), then the totals."""
    for p in placing.placed if dry_run else ():
        logger.info("[WOULD-PLACE] %s: %s → %d-%d, recording %s (AcoustID %.2f: %r on release %s)",
                    p.key, p.item_id, p.disc, p.track, p.recording, p.score, p.title, p.release)
    for key, item_id, title, why in placing.left:
        logger.info("[NOPLACE] %s: %s — %s: %s; left at track 0", key, item_id, title or "?", why)
    if placing.unnumbered:
        logger.info("Unnumbered: %d album item(s) at track 0, %d %s by AcoustID, %d left at track 0",
                    placing.unnumbered, len(placing.placed), "to place" if dry_run else "placeable",
                    len(placing.left))


def overlay(items: Iterable, placed: list[Placed]) -> None:
    """Give *items* their placements in memory only, for a dry run's link plan: never stored."""
    by_id = {p.item_id: p for p in placed}
    for item in items:
        if (p := by_id.get(item.id)) is not None:
            item.disc, item.track = p.disc, p.track
            item["mb_trackid"] = p.recording


def store(lib, placed: list[Placed], *, lock: Callable[[], contextlib.AbstractContextManager],
          connect: Callable | None = None, source: str = "albums") -> list[int]:
    """Store each placement under *lock*, with its ``track_placed`` event in
    the same transaction, re-reading the item first; then write the file's
    tags.  An item gone, numbered, given another recording or whose file
    changed since its lookup is skipped.  Returns the IDs stored."""
    from music_fetch import db, events  # noqa: PLC0415

    if not placed:
        return []
    stored: list[int] = []
    with lock(), contextlib.closing((connect or db.connect)()) as conn:
        for p in placed:
            fresh = lib.get_item(p.item_id)
            why = None
            if fresh is None:
                why = "gone from the library"
            elif _track(fresh):
                why = f"it is track {_track(fresh)} now"
            elif (fresh.get("mb_trackid") or "") not in ("", p.recording):
                why = f"its mb_trackid is {fresh.get('mb_trackid')} now"
            elif file_stat(_path(fresh)) != tuple(p.file):
                why = "its file changed since its lookup"
            if why:
                logger.warning("[SKIP] %s: %s; left at track 0", p.item_id, why)
                continue
            old = fresh.get("mb_trackid") or None
            message = (f"[PLACED] {p.key}: {fresh.id} — {_title(fresh) or '?'} is {p.disc}-{p.track} of release "
                       f"{p.release} by AcoustID ({p.score:.2f}); mb_trackid → {p.recording}")
            with conn.transaction():
                events.insert(conn, KIND, message, source=source,
                              spotify_album_id=events.spotify_album(fresh.get("spotify_album_id")),
                              release_group=fresh.get("mb_releasegroupid") or None, item_id=fresh.id,
                              data={**events.item_fields(fresh), "key": p.key, "release": p.release, "old": old,
                                    "new": p.recording, "score": p.score, "disc": p.disc, "track": p.track,
                                    "track_title": p.title})
                fresh.disc, fresh.track = p.disc, p.track
                fresh["mb_trackid"] = p.recording
                fresh.store()
            fresh.try_write()
            logger.info("%s", message)
            stored.append(fresh.id)
    return stored


def acoustid() -> Lookup | None:
    """AcoustID's recordings for a file, with ``ACOUSTID_APIKEY``; None without the key."""
    key = os.environ.get("ACOUSTID_APIKEY")
    return functools.partial(acoustid_recordings, api_key=key) if key else None


def at_import(lib, playlist: str, key: str, since: float, release: str | None, *,
              lock: Callable[[], contextlib.AbstractContextManager], lookup: Lookup | None = None,
              release_lookup: ReleaseLookup | None = None, connect: Callable | None = None) -> list[int]:
    """The album import's placement (see the module), before ``tag_album_ids``:
    the release items of record *key* imported since *since* that have no track
    number, placed on *release* (the release the files were matched to).  The
    lookups run first, without *lock*; the writes under it.  Returns the IDs
    stored."""
    items = [i for i in lib.items_by_source(playlist)
             if str(i.get("album_record") or "") == str(key) and (i.added or 0) >= since]
    todo = unnumbered(items)
    if not todo:
        return []
    if not release:
        logger.info("[NOPLACE] %s: %d item(s) at track 0 and no release identified for the files; left at track 0 "
                    "(backfill_album_ids tries again)", key, len(todo))
        return []
    lookup = lookup or acoustid()
    if lookup is None:
        logger.warning("[NOPLACE] %s: %d item(s) at track 0, and ACOUSTID_APIKEY is unset; left at track 0",
                       key, len(todo))
        return []
    placing = plan(todo, lambda _: release, lookup, release_lookup or musicbrainz_releases(), siblings=items)
    report(placing, dry_run=False)
    return store(lib, placing.placed, lock=lock, connect=connect)
