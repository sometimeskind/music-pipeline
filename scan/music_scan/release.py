"""A Usenet release imported as one album (#279).

On album playlists the album is the unit: the release grabbed for an album
record is that album, imported whole.  This module holds the library side of
``music_fetch.albums.complete()``:

- :func:`identify` matches the release folder *as a whole* to a MusicBrainz
  release, as beets' album matcher does (track count, durations, titles),
  without importing anything.  beets imports singletons here, so nothing else
  knows which edition the files are.  The result says how many of that
  release's tracks have no file (``missing``): a copy missing some is
  incomplete, and the next copy of the same edition is tried.  A file present
  but unmatched is not missing.  It runs ``beet``'s own config and plugins in
  a subprocess (``python -m music_scan.release <dir>``), so beets' global state
  stays out of the service.
- :func:`import_rest` imports, as they are, the release's files the singleton
  import couldn't match and left in ``quarantine/usenet/<playlist>/<job>/``:
  every audio file of the release is part of the album.
- :func:`claim` stamps the release's fresh items with ``album_record`` (the
  record's key, its Spotify album ID) and ``release`` (the release GUID).
  Canon, playlists and :func:`retire` read the album from them, never from
  track IDs.
- :func:`retire` sends a record's other copies to ``quarantine/replaced/``
  (never deleted, #202) when the kept release holds the same recording
  (#287).  The other copies are items of an earlier or worse release, the
  record's legacy items from before #279 (claimed by album name and artist, as
  ``music-rollback-releases`` attributes them, and on no other Spotify album),
  and spotdl's top-ups of the album (on its Spotify album and on no playlist
  but the record's own).  A spotdl copy another playlist also lists is a loose
  copy, left to supersede.

  One recording is one track only when a Spotify track ID, an ISRC or a
  MusicBrainz recording ID agrees, never by title
  (:class:`music_scan.identity.Recordings`, homelab#1889).  A copy whose
  recording the kept release lacks (a deluxe edition's bonus tracks when the
  release is the standard edition) is the library's only copy of it: it stays
  on the album as a local-only extra, logged ``[LOCAL]``.  It is stamped
  ``album_record`` (so canon keeps it on the record's album, playlists list
  it with the album, and :func:`mixed_reasons` doesn't count it) and
  ``album_extra``, and marked ``canon_wait`` so the next scan files it under
  the album's tags.  A later import that holds its recording retires it.
  Rolling a release back (*drop*) takes all of its items: they are the wrong
  files, not copies.
- :func:`retire_held` retires an extra once the record's kept release holds
  its recording after all (#294), logged ``[RETIRE] … (extra, now held)``,
  and merges one into the release item that is a certain-tier duplicate of
  it by ``music-audit-dupes``'s rule, the same audio under none of its IDs
  (#301), logged ``[RETIRE] … (extra, same audio)``.
  :func:`retire` runs before ``tag_album_ids`` (#283), so a release beets
  matched to nothing in MusicBrainz (``[NOMB]``) has no ISRC or recording ID
  yet, holds nothing, and keeps every copy as an extra; ``tag_album_ids`` then
  gives it Spotify IDs by ISRC or disc/track.  The album completion runs it
  after ``tag_album_ids`` and before canon, and every scan before its canon,
  so IDs that arrive later count too.  Either way the extras leave before
  canon moves the release's items onto paths they may hold.
- :func:`retire_merged` does :func:`retire`'s work, every scan, for a record a
  release-group merge left holding two imported releases (homelab#2545): the
  record's own release (:func:`own_release`) stays, and the other's copies go
  as its import would have sent them.  ``music-album-audit-releases
  --merged`` lists them (:func:`list_merged`).
- :func:`title_twins` finds the extras left that share a title (edition
  suffixes dropped) with a track of their record's kept release (#306): no ID
  agrees, so by homelab#1889 they are other recordings, but often another
  edit of the same song.  Titles only report: :func:`retire_held` logs each
  such extra once, ``[SAMETITLE]``, and stamps it ``album_extra_twin`` (the
  release items' IDs); nothing is merged, tagged or retired by title.

``music-album-audit-releases`` (:func:`audit_main`) lists the albums still
mixed from several releases (:func:`mixed_reasons`), each with its reset
command.  An album imported whole since #279 is never mixed, and neither
extras nor a loose spotdl copy count (#295): a reimport retires neither.  It
then lists the albums whose extras share a title with the release
(:func:`title_twins`, ``[SAMETITLE]``), each pair with its
``music-audit-dupes --merge`` command, for the operator to check and merge.
``--replaced`` lists one album's files in ``quarantine/replaced/`` (:func:`replaced_copies`)
and with ``--apply`` deletes those whose recording the library holds
(``[REPLACED]``, superseded).  A file whose recording no library item holds
is the only copy (``[ONLYCOPY]``) and stays, unless ``--only-copies`` is given
too (#287).  ``--replaced`` without a query (:func:`list_replaced`) lists every
only copy there, by the album records that claim it (:func:`group_replaced`)
with each one's ``--replaced`` command, and the files no record claims (a
merge's loser, ``music-audit-lengths --replace``'s old audio) by file, with
the item ID their name starts with (#298).  It only lists: its totals are the
scan's ``music_scan_replaced_files``.

:func:`purge_replaced` deletes, after every scan (#288), the files in
``quarantine/replaced/`` whose recording the library holds once they have sat
there ``REPLACED_RETENTION_DAYS`` (default 14; ``0`` turns it off), counted
from the move (the file's ctime: a rename keeps the mtime).  A merge's loser
counts as held while its keeper is in the library, by the file name the keeper
records (#303).  It never deletes an only copy.  The scan pushes what is left there by kind.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

from music_fetch import events
from music_fetch.state import primary_spotify

logger = logging.getLogger(__name__)

# A best match further than this (beets' medium recommendation) identifies nothing.
MATCH_MAX_DISTANCE = 0.25
IDENTIFY_TIMEOUT = 600
# The singleton import's duplicate check doesn't run under --asis (#202);
# "keep" imports a release file alongside a same-titled item, never over it.
_REST_CONFIG = "import:\n  duplicate_action: keep\n"


def _mb_fields(info) -> dict:
    """The album-level MusicBrainz tags of an ``AlbumInfo``, as canon writes them."""
    return {k: v for k, v in {
        "mb_albumid": info.album_id,
        "mb_releasegroupid": getattr(info, "releasegroup_id", None),
        "mb_albumartistid": getattr(info, "artist_id", None),
        "mb_albumartistids": getattr(info, "artists_ids", None),
        "albumdisambig": getattr(info, "albumdisambig", None),
        "releasegroupdisambig": getattr(info, "releasegroupdisambig", None),
    }.items() if v}


def match_folder(paths: list[Path], tag_album=None) -> dict | None:
    """The MusicBrainz release *paths* (one release's audio files) are, or
    None when no candidate is close enough.  *tag_album* is beets'
    ``autotag.tag_album`` (a fake in tests)."""
    from beets.autotag import Source  # noqa: PLC0415
    from beets.library import Item  # noqa: PLC0415

    if tag_album is None:
        from beets.autotag import tag_album  # noqa: PLC0415
    items = [Item.from_path(str(p)) for p in paths]
    if not items:
        return None
    candidates = sorted(tag_album(Source.from_items(items)).candidates, key=lambda m: float(m.distance))
    if not candidates or float(candidates[0].distance) > MATCH_MAX_DISTANCE:
        return None
    best = candidates[0]
    return {
        "mb_albumid": best.info.album_id,
        "album": best.info.album,
        "tracks": len(best.info.tracks),
        "files": len(items),
        "missing": len(best.extra_tracks),
        "unmatched": len(best.extra_items),
        "distance": round(float(best.distance), 3),
        "mb": _mb_fields(best.info),
    }


def audio_files(folder: Path) -> list[Path]:
    from music_fetch.albums import AUDIO_EXTS  # noqa: PLC0415

    return sorted(f for f in folder.rglob("*") if f.is_file() and f.suffix.lower() in AUDIO_EXTS)


def identify(folder: Path, run=subprocess.run) -> dict | None:
    """:func:`match_folder` on *folder*, in a beets subprocess.  Logs the result."""
    proc = run([sys.executable, "-m", "music_scan.release", str(folder)], capture_output=True, text=True,
               timeout=IDENTIFY_TIMEOUT, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"identifying {folder.name} failed ({proc.returncode}): {proc.stderr.strip()[-500:]}")
    found = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else None
    if found is None:
        logger.info("[NOMB] %s: no MusicBrainz release close enough; its completeness is unknown", folder.name)
    else:
        logger.info("[RELEASE] %s is %s (%s): %d of its %d track(s) have no file, %d file(s) unmatched",
                    folder.name, found["album"], found["mb_albumid"], found["missing"], found["tracks"],
                    found["unmatched"])
    return found


def import_rest(quarantine: Path, inbox: Path, playlist: str, job: str, run_import=None) -> int:
    """Import, as they are, the release's files the singleton import left in
    *quarantine*/usenet/<playlist>/<job>/.  Back in the usenet inbox first, so
    the plugin tags them ``via=usenet`` with the playlist.  Returns the count of
    files handed to beets; those it still skips go back to quarantine."""
    if run_import is None:
        from music_scan.process import run_beet_import as run_import  # noqa: PLC0415
    src = quarantine / "usenet" / playlist / job
    files = audio_files(src) if src.is_dir() else []
    if not files:
        return 0
    dest = inbox / "usenet" / playlist / job
    for f in files:
        target = dest / f.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), target)
    logger.info("[REST] %s: importing %d file(s) beets couldn't match, as they are", job, len(files))
    with tempfile.TemporaryDirectory(prefix="rest-config-") as config_dir:
        config = Path(config_dir) / "config.yaml"
        config.write_text(_REST_CONFIG)
        run_import(dest, asis=True, config=config)
    for f in audio_files(dest):
        target = src / f.relative_to(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), target)
    shutil.rmtree(dest, ignore_errors=True)
    return len(files)


def claim(items, playlist: str, since: float, key: str, guid: str, job: str) -> list:
    """Stamp the usenet *items* of *playlist* imported from job folder *job*
    since *since* that no record holds yet with ``album_record`` *key* and
    ``release`` *guid*.  Returns them.  The job folder, not the time window,
    says whose they are: one scan can import another job's leftovers (#284)."""
    from music_scan.identity import split_list  # noqa: PLC0415

    mine = [i for i in items if i.get("via") == "usenet" and (i.added or 0) >= since
            and i.get("usenet_job") == job
            and playlist in split_list(i.get("sources")) and not i.get("album_record")]
    for item in mine:
        item["album_record"] = key
        item["release"] = guid
        item.store()
    return mine


def claim_fallback(items, records: dict[str, dict]) -> list:
    """Stamp each album request's fallback track (homelab#2478) with its
    record's key: an item with a ``fallback_recording`` (the recording the
    scan's fingerprint check verified it as) and no ``album_record`` goes to
    the request record whose MusicBrainz songs hold that recording, the one in
    ``fallback`` when two do.  Canon then names it from the request, and the
    tick's ``recordings`` hook counts it.  Returns the stamped items; one no
    record or several claim is logged and left for the next scan."""
    from music_fetch.albums import FALLBACK, REQUEST  # noqa: PLC0415

    from music_scan.music_pipeline import FALLBACK_RECORDING  # noqa: PLC0415

    by_recording: dict[str, list[str]] = {}
    for key, record in records.items():
        if record.get("origin") != REQUEST:
            continue
        for song in record.get("album_songs") or ():
            if song.get("song_id"):
                by_recording.setdefault(song["song_id"], []).append(key)
    stamped = []
    for item in items:
        recording = item.get(FALLBACK_RECORDING)
        if not recording or item.get("album_record"):
            continue
        keys = list(dict.fromkeys(by_recording.get(recording, ())))
        if len(keys) > 1:
            keys = [k for k in keys if records[k].get("status") == FALLBACK]
        if len(keys) != 1:
            logger.warning("  [UNCLAIMED] %s %s: %s album request(s) hold recording %s; not stamped",
                           item.id, item.get("title"), len(keys) or "no", recording)
            continue
        item["album_record"] = keys[0]
        item.store()
        stamped.append(item)
        logger.info("  [CLAIM] %s %s: album request %s's track (recording %s)",
                    item.id, item.get("title"), keys[0], recording)
    return stamped


# The flex attr that marks an album record's local-only extra (#287).
EXTRA = "album_extra"
# The IDs of the kept release's items an extra shares a title with (#306).
TWIN = "album_extra_twin"


def copies(items, key: str, record: dict, keep: str | None, drop: str | None, listed) -> list:
    """The record's copies :func:`retire` weighs: with *drop*, that release's;
    else every copy but release *keep*'s, legacy items, spotdl top-ups
    (:func:`_topup`, by *listed*) and earlier extras included."""
    from music_scan.rollback import _claims  # noqa: PLC0415

    if drop is not None:
        return [i for i in items if i.get("album_record") == key and i.get("release") == drop]
    return [i for i in items if (i.get("album_record") == key and i.get("release") != keep)
            or (not i.get("album_record") and i.get("via") == "usenet" and _claims(record, i)
                and i.get("spotify_album_id") in (None, "", primary_spotify(record)))
            or (not i.get("album_record") and _topup(i, key, record, listed))]


def _topup(item, key: str, record: dict, listed) -> bool:
    """A spotdl copy on the album's Spotify album that no playlist but the
    record's own lists: spotdl's top-up, which a reimport retires.  A spotdl
    copy another playlist also lists is a loose copy, left to supersede, which
    replaces it rather than retiring it.

    *listed(item)* (:func:`music_scan.scan.store_listings`, homelab#1914) is
    the playlists whose store lists the copy's recording.  ``sources`` says
    where a copy came from, not what lists it, so it doesn't decide."""
    sid = primary_spotify(record)
    if item.get("via") != "spotdl" or not sid or item.get("spotify_album_id") != sid:
        return False
    return listed(item) <= set(record.get("playlists") or {})


def superseded(items, key: str, keep: str, others: list) -> tuple[list, list]:
    """``(superseded, extras)``: which of *others* (:func:`copies`) release
    *keep*'s items hold the recording of, and which they don't (#287)."""
    from music_scan.identity import Recordings  # noqa: PLC0415

    kept = Recordings(i for i in items if i.get("album_record") == key and i.get("release") == keep)
    held = [i for i in others if kept.holds_item(i)]
    return held, [i for i in others if not kept.holds_item(i)]


def _origin(item) -> str:
    if item.get("release"):
        return "release " + str(item.get("release"))
    return "spotdl" if item.get("via") == "spotdl" else "legacy"


def _release_group(items) -> str | None:
    """The release group most of *items* (an album's kept release) are tagged with."""
    groups = Counter(i.get("mb_releasegroupid") for i in items if i.get("mb_releasegroupid"))
    return groups.most_common(1)[0][0] if groups else None


def _album_of(items) -> str | None:
    """The Spotify album most of *items* (an album's kept release) are tagged with:
    canon gives a record's items its primary Spotify album ID."""
    albums = Counter(i.get("spotify_album_id") for i in items if i.get("spotify_album_id"))
    return albums.most_common(1)[0][0] if albums else None


def _event(kind: str, item, spotify_album: str | None, release_group: str | None, message: str,
           once_hours: int | None = None, **data) -> None:
    """Record an item's event in the event store (homelab#2413); *message* is its log line.
    *spotify_album* is the album record's primary Spotify album ID, never its key."""
    events.record(kind, message, source="release", spotify_album_id=spotify_album or None,
                  release_group=release_group, item_id=item.id, data={**events.item_fields(item), **data},
                  once_hours=once_hours)


def retire(lib, key: str, record: dict, keep: str | None, drop: str | None, listed) -> int:
    """Quarantine the :func:`copies` whose recording release *keep* holds to
    ``quarantine/replaced/``, and keep the others on the album as local-only
    extras (#287); with *drop*, quarantine that release's items.  *listed* is
    :func:`_topup`'s.  Returns the count quarantined."""
    from music_scan.canon import WAIT  # noqa: PLC0415
    from music_scan.rollback import quarantine  # noqa: PLC0415

    items = list(lib.all_items())
    others = copies(items, key, record, keep, drop, listed)
    gone, extras = (others, []) if drop is not None else superseded(items, key, keep, others)
    group = _release_group(i for i in items if i.get("album_record") == key and i.get("release") == keep)
    album = primary_spotify(record)
    for item in gone:
        logger.info("  [RETIRE] %s %s — %s [%s] (%s)", item.id, item.get("artist"), item.get("title"),
                    item.get("album"), _origin(item))
        _event("retire", item, album, group, f"[RETIRE] {item.id} {item.get('artist')} — {item.get('title')} "
                                             f"[{item.get('album')}] ({_origin(item)})", origin=_origin(item))
    for item in extras:
        logger.info("  [LOCAL] %s %s — %s [%s] (%s): the kept release lacks its recording; stays on the album",
                    item.id, item.get("artist"), item.get("title"), item.get("album"), _origin(item))
        _event("local", item, album, group, f"[LOCAL] {item.id} {item.get('artist')} — {item.get('title')} "
                                            f"[{item.get('album')}] ({_origin(item)}): the kept release lacks its "
                                            "recording; stays on the album", origin=_origin(item))
        if item.get("album_record") != key or not item.get(EXTRA):
            item["album_record"] = key
            item[EXTRA] = "1"
            item[WAIT] = "1"  # the next scan's canon files it under the record's album
            item.store()
    quarantine(gone)
    return len(gone)


def _started(release: str) -> int | None:
    """The import start a ``release`` stamp (``<guid>@<start>``) carries."""
    _, _, start = str(release).rpartition("@")
    try:
        return int(start)
    except ValueError:
        return None


def own_release(record: dict, releases) -> str | None:
    """Which of *releases* (``release`` stamps) is *record*'s own: its
    ``best_copy`` while it waits for a better copy, else the newest whose
    import started by the record's ``imported_at`` (homelab#2545).  A merge
    keeps the survivor's fields, its ``imported_at`` included, so a release the
    absorbed record imported later started after it, and one it imported
    earlier started before the survivor's own.  None when nothing tells."""
    from datetime import datetime  # noqa: PLC0415

    best = (record.get("best_copy") or {}).get("release")
    if best in releases:
        return best
    try:
        done = datetime.fromisoformat(record["imported_at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None
    before = [(s, r) for r in releases if (s := _started(r)) is not None and s <= done]
    return max(before)[1] if before else None


@dataclasses.dataclass
class Merged:
    """An album record holding items of several releases (:func:`merged`)."""
    key: str
    record: dict
    releases: Counter  # release stamp → its items on the record (extras left out)
    keep: str | None   # the record's own release, None when nothing tells (:func:`own_release`)


def merged(items, records: dict[str, dict]) -> list[Merged]:
    """The settled album records whose items (extras left out) are of two or
    more releases: a release-group merge of two imported records
    (:mod:`music_fetch.resolve`, homelab#2545) leaves both releases on the
    survivor once the scan restamps the absorbed record's items, and nothing
    else does (every import retires the record's other copies).  A record
    mid-import, or reset for a reimport, is left out: its import retires them."""
    from music_fetch.albums import IMPORTED  # noqa: PLC0415
    from music_fetch.resolve import in_flight  # noqa: PLC0415

    by_record: dict[str, Counter] = {}
    for item in items:
        if item.get("album_record") and item.get("release") and not item.get(EXTRA):
            by_record.setdefault(str(item.get("album_record")), Counter())[item.get("release")] += 1
    out = []
    for key, releases in sorted(by_record.items()):
        record = records.get(key)
        if len(releases) < 2 or record is None or in_flight(record):
            continue
        if record.get("status") != IMPORTED and not record.get("best_copy"):
            continue
        out.append(Merged(key, record, releases, own_release(record, releases)))
    return out


def retire_merged(lib, records: dict[str, dict], listed) -> int:
    """Every scan, after the restamp and before :func:`retire_held`: retire
    the other releases of each :func:`merged` record as its import would have
    (:func:`retire` with the record's own release kept, homelab#2545).  A copy
    of a recording the own release holds goes to ``quarantine/replaced/``, the
    rest stay on the album as local-only extras.  A record whose own release
    nothing tells is logged ``[MULTI]`` and left alone.  Returns the count
    quarantined."""
    n = 0
    for m in merged(lib.all_items(), records):
        label = f"{m.record.get('artist')} — {m.record.get('name')} ({m.key})"
        others = ", ".join(f"{r} ({c})" for r, c in sorted(m.releases.items()) if r != m.keep)
        if m.keep is None:
            logger.warning("  [MULTI] %s: items of %d releases (%s), and nothing tells which is its own; "
                           "left to music-album-audit-releases --merged", label, len(m.releases), others)
            continue
        logger.info("  [MERGED] %s: keeps its own release %s (%d); retiring %s", label, m.keep,
                    m.releases[m.keep], others)
        n += retire(lib, m.key, m.record, m.keep, None, listed)
    return n


def list_merged(items, records: dict[str, dict], listed) -> int:
    """``music-album-audit-releases --merged``: what :func:`retire_merged`
    would do, record by record, without moving anything.  Returns the records
    listed."""
    found = merged(items, records)
    for m in found:
        logger.info("[MERGED] %s — %s (%s): items of %d releases", m.record.get("artist"), m.record.get("name"),
                    m.key, len(m.releases))
        for r, c in sorted(m.releases.items()):
            logger.info("    %s %s: %d item(s)", "keep" if r == m.keep else "    ", r, c)
        if m.keep is None:
            logger.info("    nothing tells which release is its own (no best_copy, no imported_at after one "
                        "started): left alone")
            continue
        gone, extras = superseded(items, m.key, m.keep, copies(items, m.key, m.record, m.keep, None, listed))
        new_extras = [i for i in extras if not i.get(EXTRA)]
        for item in gone:
            logger.info("    [RETIRE] %s %s — %s (%s)", item.id, item.get("artist"), item.get("title"), _origin(item))
        for item in new_extras:
            logger.info("    [LOCAL] %s %s — %s (%s)", item.id, item.get("artist"), item.get("title"), _origin(item))
        logger.info("    %d → quarantine/replaced/, %d stay as local-only extras", len(gone), len(new_extras))
    logger.info("%d album record(s) hold items of several releases; every scan retires the others, keeping "
                "each record's own (dry run: nothing moved)", len(found))
    return len(found)


def retire_held(lib, decoder=None) -> int:
    """Retire every local-only extra (``album_extra``) its album record's kept
    release (the record's items without ``album_extra``) now holds.

    * **Held by ID** (#294): the release holds the extra's recording, a shared
      Spotify ID, ISRC or MusicBrainz recording ID.  The extra goes to
      ``quarantine/replaced/``, logged ``[RETIRE] … (extra, now held)``.
      :func:`retire` weighs the copies before ``tag_album_ids`` gives the
      release Spotify IDs, so a release beets matched to nothing in
      MusicBrainz holds no recording yet and keeps every copy as an extra.
    * **Same audio** (#301): no ID agrees, but one release item is a
      *certain*-tier duplicate of the extra by :func:`music_scan.dupes.find_dupes`'s
      own rule (an AcoustID match with lengths within ``LENGTH_SLACK`` and the
      same title once edition suffixes are dropped): a legacy copy with no IDs,
      or a release beets matched to another MusicBrainz recording.  The extra
      is merged onto that release item through :func:`music_scan.dupes.merge`,
      as ``music-audit-dupes --apply`` would, so the release item takes its
      ``spotify_ids``, ``isrc``, ``sources`` and a missing ``mb_trackid``; it
      is logged ``[RETIRE] … (extra, same audio)`` and its file goes to
      ``quarantine/replaced/``.  The release item is marked ``canon_wait``, so
      the next canon files it on the path the extra held.  The uncertain tier
      never merges here, nor does a group with several release items in it
      (two of the release's tracks one recording by an ID): those stay
      ``music-audit-dupes``'s, for the operator.

    The album completion runs this after ``tag_album_ids`` and before canon,
    and every scan runs it before its canon, so IDs that arrive later count
    too.  Only an extra leaves, and only for a recording another item of its
    record holds: the library keeps one copy of it.  *decoder* decodes
    fingerprints (:func:`music_scan.dupes.decode` by default).  Returns the
    count retired, quarantined and merged."""
    from music_scan import dupes  # noqa: PLC0415
    from music_scan.canon import WAIT  # noqa: PLC0415
    from music_scan.identity import Recordings  # noqa: PLC0415
    from music_scan.rollback import quarantine  # noqa: PLC0415

    items = list(lib.all_items())
    kept: dict[str, list] = {}
    for item in items:
        if item.get("album_record") and not item.get(EXTRA):
            kept.setdefault(item.get("album_record"), []).append(item)
    held = {key: Recordings(release) for key, release in kept.items()}
    gone = [i for i in items if i.get(EXTRA) and i.get("album_record") in held
            and held[i.get("album_record")].holds_item(i)]
    for item in gone:
        logger.info("  [RETIRE] %s %s — %s [%s] (extra, now held)", item.id, item.get("artist"), item.get("title"),
                    item.get("album"))
        key = item.get("album_record")
        _event("retire", item, _album_of(kept[key]), _release_group(kept[key]), f"[RETIRE] {item.id} {item.get('artist')} — "
               f"{item.get('title')} [{item.get('album')}] (extra, now held)", origin="extra, now held")
    quarantine(gone)

    gone_ids = {i.id for i in gone}
    merged_ids: set[int] = set()
    left: dict[str, list] = {}
    for item in items:
        if item.get(EXTRA) and item.get("album_record") in kept and item.id not in gone_ids:
            left.setdefault(item.get("album_record"), []).append(item)
    merged = 0
    for key, extras in left.items():
        release_ids = {i.id for i in kept[key]}
        certain, _, _ = dupes.find_dupes([*kept[key], *extras], decoder or dupes.decode)
        for group in certain:
            mine = [i for i in group.items if i.id in release_ids]
            losers = [i for i in group.items if i.id not in release_ids]
            if not losers:
                continue
            if len(mine) != 1:
                for item in losers:
                    logger.info("  [LOCAL] %s %s — %s [%s]: the same audio as %d of the release's items; "
                                "left to music-audit-dupes", item.id, item.get("artist"), item.get("title"),
                                item.get("album"), len(mine))
                    # It stands until someone merges it by hand, and every scan sees it: once a day.
                    _event("local", item, _album_of(kept[key]), _release_group(kept[key]),
                           f"[LOCAL] {item.id} {item.get('artist')} — {item.get('title')} [{item.get('album')}]: "
                           f"the same audio as {len(mine)} of the release's items; left to music-audit-dupes",
                           once_hours=24, origin="extra, same audio", left_to_dupes=True,
                           release_items=sorted(i.id for i in mine))
                continue
            keeper = mine[0]
            evidence = "; ".join(f"{a}~{b} {why}" for a, b, why in group.evidence)
            for item in losers:
                logger.info("  [RETIRE] %s %s — %s [%s] (extra, same audio): merged into %s: %s", item.id,
                            item.get("artist"), item.get("title"), item.get("album"), keeper.id, evidence)
            keeper[WAIT] = "1"  # the next canon files it on the path the extra held
            dupes.merge(keeper, losers, why="extra, same audio")
            merged += len(losers)
            merged_ids.update(i.id for i in losers)
    _flag_twins([i for i in items if i.id not in gone_ids | merged_ids])
    return len(gone) + merged


def title_twins(items) -> dict[str, list[tuple]]:
    """Album record → its ``(extra, release item)`` pairs: a local-only extra
    whose title, edition suffixes dropped, is the title of a track of its
    record's kept release (the record's items without ``album_extra``, #306).

    No ID agrees (:func:`retire_held` retires those, and merges the same audio
    of the certain tier), so they are two recordings (homelab#1889), but often
    two edits of one song: Stop Making Sense's 1984 LP over the 1999 and 2023
    editions' extras.  The operator decides each pair (``music-audit-dupes
    --merge``).  This only reports; titles never merge, tag or retire."""
    from music_scan.dupes import _base_title  # noqa: PLC0415

    kept: dict[str, dict[str, list]] = {}
    for item in items:
        if item.get("album_record") and not item.get(EXTRA) and (title := _base_title(item)):
            kept.setdefault(item.get("album_record"), {}).setdefault(title, []).append(item)
    pairs: dict[str, list[tuple]] = {}
    for item in items:
        key = item.get("album_record")
        if item.get(EXTRA) and key in kept:
            for twin in kept[key].get(_base_title(item), []):
                pairs.setdefault(key, []).append((item, twin))
    return pairs


def _flag_twins(items) -> None:
    """Stamp each extra :func:`title_twins` pairs with ``album_extra_twin``,
    the IDs of its release items, and log it ``[SAMETITLE]`` when that is new;
    clear it from an extra that no longer has one."""
    twins: dict[int, tuple] = {}
    for pairs in title_twins(items).values():
        for extra, twin in pairs:
            twins.setdefault(extra.id, (extra, []))[1].append(twin)
    for extra, release_items in twins.values():
        ids = ",".join(str(i.id) for i in release_items)
        if extra.get(TWIN) == ids:
            continue
        logger.warning("  [SAMETITLE] %s %s — %s [%s] (%.0fs): the release's %s has its title, but no ID agrees; "
                       "stays an extra, music-album-audit-releases lists the pair", extra.id, extra.get("artist"),
                       extra.get("title"), extra.get("album"), extra.length or 0,
                       ", ".join(f"{i.id} ({i.length or 0:.0f}s)" for i in release_items))
        extra[TWIN] = ids
        extra.store()
    for item in items:
        if item.get(TWIN) and item.id not in twins:
            del item[TWIN]
            item.store()


# ---------------------------------------------------------------------------
# music-album-audit-releases: albums mixed from several releases before #279
# ---------------------------------------------------------------------------


def mixed_reasons(key: str, record: dict, items: list, listed) -> list[str]:
    """Why an imported album record's album is not one release: what the
    track-level rules before #279 did to it, and a reimport would undo.
    Empty for one release.

    An album imported whole since #279 (an item of the record's own release
    on it) is one release: its import retired every copy :func:`copies`
    takes, so a reimport would only grab the same album again (#295).  Nor
    do local-only extras count, or a spotdl copy another playlist also lists:
    :func:`retire` leaves both on purpose.  *listed* is :func:`_topup`'s."""
    from music_fetch.albums import GAP, IMPORTED, PARTIAL  # noqa: PLC0415

    if record.get("status") != IMPORTED and record.get("fallback_from") not in (PARTIAL, GAP):
        return []
    if record.get("origin") == "request":
        # An album request's (homelab#2478) is imported whole, its release stamped, and its
        # spotdl fallback tracks are its own: never mixed, whatever its blocklist says.
        return []
    sid = primary_spotify(record)
    mine = [i for i in items if sid and i.get("spotify_album_id") == sid and not i.get(EXTRA)
            and not (i.get("via") == "spotdl" and not _topup(i, key, record, listed))]
    if any(i.get("album_record") == key for i in mine):
        return []  # imported whole since #279
    reasons = []
    if record.get("gaps") or record.get("settled_from") or record.get("fallback_from") in (PARTIAL, GAP):
        reasons.append("imported short of Spotify's tracklist ([PART]/[GAP])")
    if record.get("rejected_tracks") or record.get("filled_at"):
        reasons.append("its rejected tracks went to spotdl")
    if record.get("blocklist"):
        reasons.append(f"{len(record['blocklist'])} release(s) blocklisted before (their unmatched tracks rolled back)")
    spotdl = sum(1 for i in mine if i.get("via") == "spotdl")
    if spotdl:
        reasons.append(f"{spotdl} spotdl track(s) on the album")
    return reasons


def album_query(records: dict[str, dict], key: str, name: str) -> str:
    """The shell-quoted query that selects record *key*: its ``artist — name``
    when that names it alone, else its key (twins share a name, #289)."""
    from music_fetch.albums import find_albums  # noqa: PLC0415

    if [k for k, _ in find_albums(records, name)] == [key]:
        return name.replace("'", "'\\''")
    return key


class _Tags(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def _album_tags(f: Path) -> _Tags | None:
    """*f*'s album and artist tags, as :func:`music_scan.rollback._claims`
    reads an item's; ``None`` when unreadable."""
    from mediafile import MediaFile  # noqa: PLC0415

    try:
        mf = MediaFile(str(f))
    except Exception:
        return None
    return _Tags(album=mf.album, albumartist=mf.albumartist, artist=mf.artist, title=mf.title)


def replaced_copies(record: dict, replaced_dir: Path) -> list[Path]:
    """The files in *replaced_dir* whose album and artist tags *record* claims
    (as ``music-rollback-releases`` attributes items): its rolled-back,
    superseded and merged-away copies."""
    from music_scan.rollback import _claims  # noqa: PLC0415

    found = []
    for f in audio_files(replaced_dir) if replaced_dir.is_dir() else []:
        tags = _album_tags(f)
        if tags is not None and _claims(record, tags):
            found.append(f)
    return found


def group_replaced(records: dict[str, dict], files: list[Path]) -> tuple[dict[str, list[Path]], list[Path]]:
    """``(claimed, unclaimed)``: *files* by the keys of the album *records*
    whose :func:`replaced_copies` takes them, and the files no record claims
    (#298).  A file two records claim is under both, as each one's
    ``--replaced`` lists it."""
    from music_scan.rollback import _claims  # noqa: PLC0415

    claimed: dict[str, list[Path]] = {}
    unclaimed = []
    for f in files:
        tags = _album_tags(f)
        keys = [k for k, r in records.items() if tags is not None and _claims(r, tags)]
        for k in keys:
            claimed.setdefault(k, []).append(f)
        if not keys:
            unclaimed.append(f)
    return claimed, unclaimed


def _file_origin(f: Path) -> str:
    """What identifies an unclaimed replaced file: the library item it was
    (every move to ``quarantine/replaced/`` prefixes the item ID), its tags
    and its identity tags."""
    from music_scan.playlists import _file_tags  # noqa: PLC0415

    head, sep, _ = f.name.partition("-")
    parts = [f"item {head}" if sep and head.isdigit() else "no item ID"]
    tags = _album_tags(f)
    if tags is None:
        return "; ".join(parts + ["unreadable"])
    parts.append(f"{tags.get('albumartist') or tags.get('artist') or '?'} — {tags.get('title') or '?'} "
                 f"[{tags.get('album') or '?'}]")
    rf = _file_tags(f)
    ids = []
    if rf is not None:
        ids = [f"spotify {sid}" for sid in sorted(rf.spotify_ids)] + [f"isrc {c}" for c in sorted(rf.isrcs)]
        ids += [f"mb_trackid {rf.mb_trackid}"] if rf.mb_trackid else []
    parts.append(", ".join(ids) or "no identity tags")
    return "; ".join(parts)


def split_replaced(files: list[Path], library) -> tuple[list[Path], list[Path]]:
    """``(superseded, only_copies)``: which replaced *files* hold a recording
    *library* (a :class:`music_scan.identity.Recordings` of the library's
    items) holds by their identity tags (Spotify ID, ISRC, or ``mb_trackid``
    from an item whose length agrees with the file's, homelab#2540), and which
    hold one it doesn't (#287).  A merge's loser is superseded while
    its keeper is in the library, whatever its tags
    (:meth:`~music_scan.identity.Recordings.holds_merged`, #303).  Any other unreadable file, or one with no
    identity tags, is an only copy: nothing says the library has it."""
    from music_scan.playlists import _file_tags  # noqa: PLC0415

    held, only = [], []
    for f in files:
        if library.holds_merged(f.name):
            held.append(f)
            continue
        rf = _file_tags(f)
        (held if rf is not None and library.holds(rf.recording_ids, rf.length) else only).append(f)
    return held, only


def _moved_at(f: Path) -> float:
    """When *f* moved to ``quarantine/replaced/``: a rename sets the ctime, never the mtime."""
    st = f.stat()
    return max(st.st_ctime, st.st_mtime)


def retention_days() -> int:
    try:
        return int(os.environ.get("REPLACED_RETENTION_DAYS", "") or 14)
    except ValueError:
        logger.error("REPLACED_RETENTION_DAYS must be an integer — using 14")
        return 14


def purge_replaced(replaced_dir: Path, library, days: int, now: float | None = None) -> dict[str, int]:
    """Delete the files in *replaced_dir* whose recording *library* (a
    :class:`music_scan.identity.Recordings`) holds and that moved there more
    than *days* days ago (#288).  An only copy is never deleted.  *days* 0
    deletes nothing.  Returns the files left by kind (``superseded``,
    ``only_copy``) and the count ``purged``."""
    now = time.time() if now is None else now
    held, only = split_replaced(audio_files(replaced_dir) if replaced_dir.is_dir() else [], library)
    purged = 0
    for f in held:
        try:
            due = days > 0 and now - _moved_at(f) > days * 86400
            if due:
                f.unlink()
        except OSError as exc:
            logger.warning("  [PURGE] %s: %s", f, exc)
            continue
        if due:
            purged += 1
            logger.info("  [PURGE] %s: superseded %d+ days ago, the library holds its recording", f, days)
    if purged or only:
        logger.info("quarantine/replaced: deleted %d superseded file(s); %d left, %d only copies kept", purged,
                    len(held) - purged, len(only))
    return {"superseded": len(held) - purged, "only_copy": len(only), "purged": purged}


def list_replaced(records: dict[str, dict], replaced_dir: Path, library) -> dict[str, int]:
    """Log every only copy in *replaced_dir* (:func:`split_replaced`'s rule,
    as the scan counts them), by the album records that claim it, each with
    its ``--replaced`` command, then the unclaimed ones by file (#298).
    Deletes nothing.  Returns the counts by kind, as :func:`purge_replaced`
    leaves them with nothing due."""
    held, only = split_replaced(audio_files(replaced_dir) if replaced_dir.is_dir() else [], library)
    claimed, unclaimed = group_replaced(records, only)
    names = {k: f"{records[k].get('artist')} — {records[k].get('name')}" for k in claimed}
    claims = Counter(f for files in claimed.values() for f in files)
    for key in sorted(claimed, key=lambda k: (names[k].casefold(), k)):
        logger.info("[ALBUM] %s (%s): %d only copies", names[key], key, len(claimed[key]))
        logger.info("    music-album-audit-releases --replaced '%s'", album_query(records, key, names[key]))
        for f in claimed[key]:
            logger.info("  [ONLYCOPY] %s%s", f, " (another album claims it too)" if claims[f] > 1 else "")
    if unclaimed:
        logger.info("[UNCLAIMED] no album record claims these; decide on each by its file")
    for f in unclaimed:
        logger.info("  [ONLYCOPY] %s: %s", f, _file_origin(f))
    logger.info("%d only copies in %s: %d on %d album(s), %d unclaimed; %d superseded (the scan deletes those "
                "after REPLACED_RETENTION_DAYS)", len(only), replaced_dir, len(only) - len(unclaimed), len(claimed),
                len(unclaimed), len(held))
    return {"superseded": len(held), "only_copy": len(only)}


def audit_main() -> None:
    import argparse  # noqa: PLC0415

    from music_fetch.albums import State, find_albums  # noqa: PLC0415
    from music_scan.audit import REPLACED  # noqa: PLC0415
    from music_scan.identity import Recordings  # noqa: PLC0415
    from music_scan.library import LIBRARY_DB, MusicLibrary  # noqa: PLC0415

    parser = argparse.ArgumentParser(
        prog="music-album-audit-releases",
        description="List imported albums mixed from several releases before #279, each with its reset command; "
                    "with --merged, the records a release-group merge left holding several releases; "
                    "with --replaced, list (and with --apply delete) one album's copies in quarantine/replaced/; "
                    "with --replaced and no query, list every only copy there by album")
    parser.add_argument("--replaced", metavar="QUERY", nargs="?", const="",
                        help="the album's Spotify ID, its 'artist — album', or a part of it that matches one album; "
                             "without one, list every only copy in quarantine/replaced/ (read-only)")
    parser.add_argument("--apply", action="store_true",
                        help="with --replaced: delete the files whose recording the library holds")
    parser.add_argument("--only-copies", action="store_true",
                        help="with --replaced --apply: delete the only copies too (#287)")
    parser.add_argument("--merged", action="store_true",
                        help="list the album records holding items of several releases (a release-group merge of "
                             "two imported records) and what the next scan retires of each (read-only, "
                             "homelab#2545)")
    args = parser.parse_args()
    if args.merged and (args.replaced is not None or args.apply or args.only_copies):
        parser.error("--merged only lists; every scan retires what it lists")
    if args.replaced == "" and (args.apply or args.only_copies):
        parser.error("--replaced without a query only lists; give it an album to delete that album's copies")
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    records = State.load().albums
    if args.merged:
        from music_scan.scan import store_listings  # noqa: PLC0415

        with MusicLibrary(LIBRARY_DB) as lib:
            list_merged(list(lib.all_items()), records, store_listings())
        return
    if args.replaced == "":
        with MusicLibrary(LIBRARY_DB) as lib:
            library = Recordings(lib.all_items())
        list_replaced(records, REPLACED, library)
        return
    if args.replaced:
        hits = find_albums(records, args.replaced)
        if len(hits) != 1:
            logger.error("%d album(s) match %r", len(hits), args.replaced)
            raise SystemExit(1)
        with MusicLibrary(LIBRARY_DB) as lib:
            library = Recordings(lib.all_items())
        held, only = split_replaced(replaced_copies(hits[0][1], REPLACED), library)
        for f in held:
            logger.info("  [REPLACED] %s", f)
        for f in only:
            logger.info("  [ONLYCOPY] %s: no library item holds its recording", f)
        doomed = held + only if args.only_copies else held
        if not args.apply:
            logger.info("%d file(s) in %s: %d superseded, %d only copies; dry run, --apply deletes the superseded "
                        "ones (with --only-copies, the only copies too)", len(held) + len(only), REPLACED,
                        len(held), len(only))
            return
        for f in doomed:
            f.unlink(missing_ok=True)
        logger.info("Deleted %d file(s)%s", len(doomed),
                    f"; kept {len(only)} only copies (--only-copies deletes them)" if only and not args.only_copies
                    else "")
        return
    from music_scan.dupes import spotify_durations  # noqa: PLC0415
    from music_scan.scan import store_listings  # noqa: PLC0415

    with MusicLibrary(LIBRARY_DB) as lib:
        items = list(lib.all_items())
    listed = store_listings()
    n = 0
    for key, record in records.items():
        reasons = mixed_reasons(key, record, items, listed)
        if not reasons:
            continue
        n += 1
        name = f"{record.get('artist')} — {record.get('name')}"
        logger.info("[MIXED] %s (%s): %s", name, key, "; ".join(reasons))
        logger.info("    music-album-reset '%s' --reimport", album_query(records, key, name))
    logger.info("%d imported album(s) mixed from several releases; reset each with --reimport (dry run, then "
                "--apply), then clear its old copies with --replaced", n)
    twins = title_twins(items)
    list_twins(records, twins, spotify_durations() if twins else {})


def list_twins(records: dict[str, dict], twins: dict[str, list[tuple]], durations: dict[str, float]) -> int:
    """Log *twins* (:func:`title_twins`) album by album, each pair with its
    ``music-audit-dupes --merge`` command (the keeper the release item, then
    the Spotify length's by *durations*, as ``music-audit-dupes`` keeps).  Returns the albums listed."""
    from music_scan.dupes import merge_command  # noqa: PLC0415

    for key, pairs in twins.items():
        record = records.get(key) or {}
        logger.info("[SAMETITLE] %s — %s (%s): %d local-only extra(s) share a title with the release, no ID agrees",
                    record.get("artist"), record.get("name"), key, len({e.id for e, _ in pairs}))
        for extra, twin in pairs:
            logger.info("    %s %s (%.0fs, %s) ~ %s (%.0fs): %s", extra.id, extra.get("title"),
                        extra.length or 0, _origin(extra), twin.id, twin.length or 0,
                        merge_command(extra, twin, durations))
    logger.info("%d album(s) with local-only extras sharing a title with the release; check each pair "
                "(music-audit-dupes lists the evidence), then merge it with --apply", len(twins))
    return len(twins)


def main() -> None:
    """``python -m music_scan.release <dir>``: print :func:`match_folder`'s result as JSON."""
    from beets import config, plugins  # noqa: PLC0415

    logging.basicConfig(level=logging.WARNING)
    config.read()
    plugins.load_plugins()
    print(json.dumps(match_folder(audio_files(Path(sys.argv[1])))))


if __name__ == "__main__":
    main()
