"""A kept file back on its album (homelab#2815).

``music-album-audit-releases --restore FILE --album QUERY [--allow-held]
[--apply]`` (the MCP's ``restore_kept``) puts one file from
``quarantine/keep/`` back into the library, as a local-only extra of the album
record *QUERY* names.  ``--keep`` (homelab#2623) took the file out of the
purge's reach; before this nothing brought it back, so a take retired because
an item held its recording by a wrong ID stayed out of the library even once
the ID was fixed (17052, the live *Reason To Believe (Nebraska Live)*,
homelab#2609).

The file is imported on its own tags, in-process, as the as-is pass would:
beets matches nothing.  Its tags are what the library item had when it left,
so its ``mb_trackid`` (and the ISRCs and work beets copied with it) may be the
wrong recording that got it retired.  The import's own rule decides
(:func:`music_scan.music_pipeline.wrong_recording`, homelab#2544, #2609): each
library item that shares the file's ``mb_trackid`` or an ISRC is weighed as the
recording beets matched, its title and ``trackdisambig`` against the file's
title and album.  When one is another song, another version
(:func:`music_scan.identity.version_clash`) or another artist's, the file gets
no recording rather than that one: its :data:`RECORDING_FIELDS` are cleared,
and the ISRCs such an item holds go with them.  ISRCs no such item holds stay.
When no item shares the file's ``mb_trackid`` (the item that held it fixed since),
MusicBrainz's recording of it is weighed the same way, its title and
disambiguation, one lookup; a failed lookup refuses the restore, as nothing
then says the ID is the file's.

What is left is weighed as the release pass weighs an extra, so the next scan
keeps it (:func:`music_scan.release.retire_held`, ``retire_merged``):

- A release item of the named record (its items without ``album_extra``) that
  holds the file's recording (:class:`~music_scan.identity.Recordings`: a
  Spotify ID, an ISRC, or a ``mb_trackid`` at a length within 2s) or is a
  certain-tier duplicate of its audio (:func:`music_scan.dupes.audio_match`)
  would retire or merge it again at the next scan: refused, always.
- Any other item that holds it is the library holding the file already:
  refused unless ``--allow-held``, which imports it beside that item.
- The record must exist, not be mid-import (its import weighs the record's
  extras itself), and have release items; the file's library path must be free.

The item is stamped as :func:`music_scan.release.retire` keeps an extra:
``album_record`` (the record's key), ``album_extra`` and ``canon_wait``, so the
next scan's canon files it under the album's tags; ``via`` ``spotdl`` when its
tags carry spotdl's Spotify URL, else ``usenet``, and the record's playlists as
``sources``.  It has no ``release`` stamp: it is no release's item, and
``merged`` and ``other_albums`` read only release items.  ``retire_item`` takes
it off again.

The dry run says what it imports (the file, its tags and identity tags), the
recording it gets and why, the items that would then hold the same recording,
and where the file goes.  The apply, under the pipeline lock and only with the
music database, adds the item, moves the file into the library and writes its
tags, with a ``restored`` event in the same transaction (a failure undoes the
import), then regenerates the ``.m3u`` files and asks Navidrome to rescan.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import shutil
from pathlib import Path

from music_fetch import events
from music_fetch.state import primary_spotify

logger = logging.getLogger(__name__)

NOTHING = "nothing imported"


@dataclasses.dataclass
class Restore:
    """What :func:`plan` decided for one kept file."""

    src: Path
    item: object  # the beets item read from the file, stamped, not yet in the library
    key: str
    record: dict
    dest: Path
    was: int | None  # the item ID the file name starts with
    dropped: dict  # the recording fields cleared, with the values the file had
    why: str | None  # wrong_recording's reason, when the recording was dropped
    versions: list[str]
    clashing: list  # the items whose recording the file's ID was
    holders: list[tuple[object, str]]  # (item, what it shares) once imported
    fingerprint: str | None  # "tags", "computed", or None when there is none
    release_group: str | None  # the record's release items'
    lines: list[str]  # the dry run's account, in order
    refusals: list[str]


def _label(item) -> str:
    return f"{item.id} {item.get('artist')} — {item.get('title')} [{item.get('album')}]"


def _minutes(seconds) -> str:
    s = round(float(seconds or 0))
    return f"{s // 60}:{s % 60:02d}"


def _holds(item, keys: frozenset[str], length) -> list[str]:
    """What of *keys* (a recording's identity keys) *item* holds, as the release
    pass counts it (:class:`~music_scan.identity.Recordings`)."""
    from music_scan.identity import Recordings, item_recording_ids  # noqa: PLC0415

    shared = sorted(item_recording_ids(item) & keys)
    return [k for k in shared if Recordings([item]).holds(frozenset([k]), length)]


def _key_label(key: str) -> str:
    kind, _, value = key.partition(":")
    return f"{'mb_trackid' if kind == 'mb' else kind} {value}"


def _destination(item, lib) -> Path:
    """Where *lib* (the beets library) files *item*, which isn't in it yet: beets'
    ``destination()`` wants a row, so the item stands in for one with a placeholder
    ID, dropped again before ``lib.add`` gives it its own."""
    item._db, item.id = lib, -1
    try:
        return Path(os.fsdecode(item.destination()))
    finally:
        item.id = None
        item.__dict__.pop("db", None)  # beets caches the checked database


def plan(name: str, key: str, record: dict, items: list, lib, keep_dir: Path, allow_held: bool = False,
         fingerprinter=None, decoder=None, recordings=None) -> Restore:
    """Weigh restoring the file *name* names in *keep_dir* (``quarantine/keep/``) onto
    album record *key*: read it, decide its recording and who holds it, stamp the item
    and find its library path.  *items* are the library's items, *lib* the beets
    library (for the path).  *fingerprinter* computes a file's fingerprint when its
    tags have none (:func:`music_scan.identity.fingerprint`), *decoder* decodes them
    (:func:`music_scan.dupes.decode`), *recordings* looks a recording up on
    MusicBrainz (:func:`music_scan.dupes.recording_lookup`).  Raises
    :class:`SystemExit` when the file can't be found or read; any other refusal is in
    ``refusals``."""
    import http.client  # noqa: PLC0415

    from beets.library import Item  # noqa: PLC0415

    from music_fetch.resolve import in_flight  # noqa: PLC0415
    from music_scan import dupes  # noqa: PLC0415
    from music_scan.canon import WAIT  # noqa: PLC0415
    from music_scan.identity import (  # noqa: PLC0415
        add_isrcs, add_to_list, artist_credit, compilation, different_artist, item_isrcs, item_recording_ids,
        lengths_agree, spotify_id,
    )
    from music_scan.identity import fingerprint as compute  # noqa: PLC0415
    from music_scan.music_pipeline import RECORDING_FIELDS, _read_spotdl_tags, wrong_recording  # noqa: PLC0415
    from music_scan.pathfold import held_by, library_paths  # noqa: PLC0415
    from music_scan.release import EXTRA, KEEP, _release_group, named_file  # noqa: PLC0415

    src = named_file(name, keep_dir, "--restore", KEEP, NOTHING)
    rel = src.relative_to(keep_dir)
    try:
        item = Item.from_path(str(src))
    except Exception as exc:  # noqa: BLE001 — any unreadable file is refused the same way
        raise SystemExit(f"--restore: the tags of keep/{rel} can't be read ({exc}); {NOTHING}") from exc
    head, sep, _ = src.name.partition("-")
    was = int(head) if sep and head.isdigit() else None
    lines = [f"[RESTORE] keep/{rel} ({src.stat().st_size / 1e6:.1f} MB, "
             f"{f'was item {was}' if was else 'no item ID in its name'}) → album {key} "
             f"{record.get('artist')} — {record.get('name')}, as a local-only extra"]
    disc = f"disc {item.get('disc')} " if item.get("disc") else ""
    lines.append(f"    tags: {item.get('artist') or '?'} — {item.get('title') or '?'} [{item.get('album') or '?'}], "
                 f"{disc}track {item.get('track') or '?'}, {_minutes(item.get('length'))}")

    spotdl = _read_spotdl_tags(str(src))
    sid = spotify_id(spotdl.url)
    if sid:
        item["spotify_ids"] = sid
    if spotdl.isrc:
        add_isrcs(item, [spotdl.isrc])
    mb = (item.get("mb_trackid") or "").strip().lower()
    isrcs = item_isrcs(item)
    ids = [f"spotify {sid}"] if sid else []
    ids += [f"isrc {c}" for c in sorted(isrcs)] + ([f"mb_trackid {mb}"] if mb else [])
    lines.append(f"    identity tags: {', '.join(ids) or 'none'}")

    # The recording: the import's rule against each item that shares the file's ID (homelab#2609).
    title, album = item.get("title") or "", item.get("album") or ""
    file_credit = artist_credit(item)
    clashing, why, versions = [], None, []
    for other in items:
        if not ((mb and (other.get("mb_trackid") or "").strip().lower() == mb) or isrcs & item_isrcs(other)):
            continue
        another_artist = (bool(item.get("artist")) and not compilation(item)
                          and different_artist(file_credit, artist_credit(other)))
        reason, named = wrong_recording(title, album, other.get("title") or "", other.get("trackdisambig") or "",
                                        another_artist)
        if reason is not None:
            clashing.append(other)
            why, versions = why or reason, versions or named
            note = f"{reason}, {', '.join(named)}" if named else reason
            disambig = other.get("trackdisambig")
            lines.append(f"    [CLASH] {_label(other)}{f' (trackdisambig {disambig!r})' if disambig else ''} "
                         f"shares its ID: {note}")
    refusals: list[str] = []
    if mb and not clashing:
        # No library item says the ID is another take's: ask MusicBrainz what the recording
        # is, as the import weighs beets' match (homelab#2609).
        try:
            found = (recordings or dupes.recording_lookup())(mb)
        except (OSError, ValueError, http.client.HTTPException) as exc:  # urllib's errors, a cut or non-JSON body
            found = None
            refusals.append(f"the MusicBrainz lookup of its recording {mb} failed ({exc}), so nothing says it is "
                            "the file's; try again")
        else:
            if not found:
                why = "MusicBrainz has no such recording"
                lines.append(f"    [CLASH] MusicBrainz has no recording {mb}")
        if found:
            rec_title, notes = found.get("title") or "", found.get("disambiguation") or ""
            reason, named = wrong_recording(title, album, rec_title, notes)
            label = f"{rec_title!r}{f' ({notes})' if notes else ''}"
            if reason is not None:
                why, versions = reason, named
                note = f"{reason}, {', '.join(named)}" if named else reason
                lines.append(f"    [CLASH] MusicBrainz recording {mb} is {label}: {note}")
            else:
                lines.append(f"    [MB] recording {mb} is {label}: the file's title and album agree")
    dropped: dict = {}
    if clashing or why:
        for field in RECORDING_FIELDS:
            if field != "isrc" and item.get(field):
                dropped[field] = item.get(field)
                item[field] = [] if isinstance(item.get(field), list) else ""
        theirs = set().union(*(item_isrcs(i) for i in clashing))
        if gone := sorted(isrcs & theirs):
            dropped["isrc"] = gone
            item["isrc"] = ";".join(c for c in item_isrcs(item) if c not in theirs)
        by_mb = mb and any((i.get("mb_trackid") or "").strip().lower() == mb for i in clashing)
        shared = f"mb_trackid {mb}" if by_mb or not clashing else "ISRC"
        whose = f"{', '.join(str(i.id) for i in clashing)}'s recording" if clashing else "MusicBrainz's"
        kind = f"{why} ({', '.join(versions)})" if versions else why
        lines.append(f"    [RECORDING] none: the file's {shared} is {whose}, {kind}; cleared: "
                     f"{', '.join(sorted(dropped))} (the import's rule, homelab#2609)")
    elif mb:
        lines.append(f"    [RECORDING] {mb}: the file's own; no item that shares it, nor MusicBrainz, names "
                     "another song or version")
    else:
        lines.append("    [RECORDING] none: the file names none; the fingerprint recheck can name it later")

    fp = None
    if item.get("acoustid_fingerprint"):
        fp = "tags"
    else:
        try:
            item["acoustid_fingerprint"] = (fingerprinter or compute)(src)
            fp = "computed"
        except Exception as exc:  # noqa: BLE001 — no fingerprint is a finding, not a failure
            lines.append(f"    [NOFP] no fingerprint: {exc}; the audio isn't compared")

    # Who holds it once it is in: the release pass's rule, then the duplicate audit's audio rule.
    keys = item_recording_ids(item)
    length = item.get("length")
    release = [i for i in items if i.get("album_record") == key and not i.get(EXTRA)]
    release_ids = {i.id for i in release}
    holders: list[tuple[object, str]] = []
    frames_of = dupes.frames_reader(decoder or dupes.decode)
    audio = bool(item.get("acoustid_fingerprint") or item.get("acoustid_id"))
    words = dupes._artist_words(item)
    for other in items:
        held = _holds(other, keys, length)
        what = ", ".join(map(_key_label, held))
        for k in sorted(keys & item_recording_ids(other)):
            if k.startswith("mb:") and not lengths_agree(length, other.get("length")):
                gap = abs(float(length or 0) - float(other.get("length") or 0))
                lines.append(f"    [SIBLING] {_label(other)} shares {_key_label(k)} {gap:.0f}s apart: "
                             "another length, so it doesn't hold the recording")
        match = None
        if audio and words & dupes._artist_words(other) and (other.get("acoustid_fingerprint")
                                                             or other.get("acoustid_id")):
            match = dupes.audio_match(item, other, frames_of)
        if match and match[0] != dupes.CERTAIN:
            lines.append(f"    [AUDIO] {_label(other)}: {match[1]} (uncertain: not held)")
            match = None
        if match:
            what = ", ".join(filter(None, [what, f"same audio, {match[1]}"]))
        if not what:
            continue
        holders.append((other, what))
        if other.id in release_ids:
            rule = "retire it again (extra, now held)" if held else f"merge it into {other.id} (extra, same audio)"
            refusals.append(f"{_label(other)}, a release item of album {key}, holds its recording ({what}): the next "
                            f"scan's retire_held would {rule}")
        elif not allow_held:
            refusals.append(f"the library holds it already: {_label(other)} ({what}); allow_held imports it beside")
    for other, what in holders:
        lines.append(f"    [HOLDER] {_label(other)} would hold the same recording: {what}")
    if not holders:
        lines.append("    [HOLDER] none: no library item would hold its recording")

    if in_flight(record):
        refusals.append(f"album {key} is mid-import, and its import weighs its extras itself")
    if not release_ids:
        refusals.append(f"album {key} has no release items, so the file would be all it holds")

    # The stamp the release pass gives an extra (release.retire), and the item's provenance.
    item["album_record"] = key
    item[EXTRA] = "1"
    item[WAIT] = "1"  # the next scan's canon files it under the record's album
    item["via"] = "spotdl" if sid else "usenet"
    for playlist in sorted(record.get("playlists") or {}):
        add_to_list(item, "sources", playlist)
    dest = _destination(item, lib)
    taken = held_by(library_paths(lib), dest)
    if taken or dest.exists():
        by = ", ".join(f"item {i}" for i in sorted(taken)) or "a file not in the library"
        refusals.append(f"its library path {dest} is taken by {by}")
    lines.append(f"    → {dest}, via {item['via']}, sources {item.get('sources') or '-'}; the next scan's canon "
                 "files it under the album's tags")
    return Restore(src, item, key, record, dest, was, dropped, why, versions, clashing, holders, fp,
                   _release_group(release), lines, refusals)


def show(r: Restore) -> None:
    """Log :func:`plan`'s account, then raise :class:`SystemExit` if anything refuses it."""
    for line in r.lines:
        logger.info("%s", line)
    for why in r.refusals:
        logger.info("[REFUSED] %s", why)
    if r.refusals:
        raise SystemExit(f"--restore: refused ({len(r.refusals)} reason(s)); {NOTHING}")


def restore(r: Restore, lib, conn) -> object:
    """Import *r*'s item: add it to the beets library *lib*, move its file into the
    library and write its tags, with its ``restored`` event on *conn* in one
    transaction.  A failure anywhere undoes the import: the file goes back to
    ``quarantine/keep/`` and the row leaves.  Returns the item."""
    item = r.item
    added = moved = False
    try:
        with conn.transaction():
            lib.add(item)
            added = True
            r.dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(r.src), r.dest)
            moved = True
            item["path"] = os.fsencode(r.dest)
            # Written first: write() takes the file's new mtime, which the store keeps, so the
            # scan's beet update reads the same tags back (the cleared recording, the fingerprint).
            if not item.try_write():
                raise OSError(f"could not write the tags of {r.dest}")
            item.store()
            message = (f"[RESTORE] {_label(item)}: keep/{r.src.name} back on album {r.key} as a local-only extra"
                       f"{'; its recording dropped: ' + r.why if r.why else ''}")
            events.insert(conn, "restored", message, source="release", item_id=item.id,
                          spotify_album_id=primary_spotify(r.record), release_group=r.release_group,
                          data={**events.item_fields(item), "source": str(r.src), "key": r.key, "was": r.was,
                                "via": item.get("via"), "recording": item.get("mb_trackid") or None,
                                "dropped": r.dropped or None, "dropped_why": r.why, "versions": r.versions or None,
                                "clashing": [i.id for i in r.clashing] or None,
                                "holders": [i.id for i, _ in r.holders] or None, "fingerprint": r.fingerprint})
    except BaseException:
        if moved and r.dest.exists():
            r.src.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(r.dest), r.src)
        if added and item.id is not None:
            item.remove(delete=False)
        raise
    logger.info("  %s", message)
    return item


def main(name: str, query: str, allow_held: bool, apply: bool) -> None:
    """``--restore``: the dry run, or the apply under the pipeline lock and only with the
    music database (the restore is an event or nothing), then the ``.m3u`` files and
    Navidrome."""
    import contextlib  # noqa: PLC0415

    from music_fetch import db  # noqa: PLC0415
    from music_fetch.albums import State, find_albums  # noqa: PLC0415
    from music_scan.audit import QUARANTINE, pipeline_lock  # noqa: PLC0415
    from music_scan.library import LIBRARY_DB, MusicLibrary  # noqa: PLC0415
    from music_scan.navidrome import trigger_scan  # noqa: PLC0415
    from music_scan.release import KEEP  # noqa: PLC0415
    from music_scan.scan import regen_playlists  # noqa: PLC0415

    records = State.load().albums
    hits = find_albums(records, query)
    if len(hits) != 1:
        names = "; ".join(f"{k} {r.get('artist')} — {r.get('name')}" for k, r in hits[:10])
        raise SystemExit(f"--restore: {len(hits)} album(s) match {query!r}{f' ({names})' if names else ''}; "
                         f"{NOTHING}")
    key, record = hits[0]
    if not apply:
        with MusicLibrary(LIBRARY_DB) as lib:
            show(plan(name, key, record, lib.all_items(), lib._lib, QUARANTINE / KEEP, allow_held))
        logger.info("Dry run — %s. Re-run with --apply; retire_item takes it off the album again.", NOTHING)
        return
    if not db.configured():
        raise SystemExit(f"--restore --apply: {db.ENV} is unset; a restore is recorded as an event, so nothing "
                         "runs without it")
    with pipeline_lock(), contextlib.closing(db.connect()) as conn, MusicLibrary(LIBRARY_DB) as lib:
        r = plan(name, key, record, lib.all_items(), lib._lib, QUARANTINE / KEEP, allow_held)
        show(r)
        restore(r, lib._lib, conn)
        counts = regen_playlists()
    logger.info("Restored; playlists regenerated: %s", ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    trigger_scan()
