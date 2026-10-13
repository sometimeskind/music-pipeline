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
  its recording after all (#294), logged ``[RETIRE] … (extra, now held)``.
  :func:`retire` runs before ``tag_album_ids`` (#283), so a release beets
  matched to nothing in MusicBrainz (``[NOMB]``) has no ISRC or recording ID
  yet, holds nothing, and keeps every copy as an extra; ``tag_album_ids`` then
  gives it Spotify IDs by ISRC or disc/track.  The album completion runs it
  after ``tag_album_ids`` and before canon, and every scan before its canon,
  so IDs that arrive later count too.  Either way the extras leave before
  canon moves the release's items onto paths they may hold.

``music-album-audit-releases`` (:func:`audit_main`) lists the albums still
mixed from several releases (:func:`mixed_reasons`), each with its reset
command.  An album imported whole since #279 is never mixed, and neither
extras nor a loose spotdl copy count (#295): a reimport retires neither.
``--replaced`` lists one album's files in ``quarantine/replaced/`` (:func:`replaced_copies`)
and with ``--apply`` deletes those whose recording the library holds
(``[REPLACED]``, superseded).  A file whose recording no library item holds
is the only copy (``[ONLYCOPY]``) and stays, unless ``--only-copies`` is given
too (#287).

:func:`purge_replaced` deletes, after every scan (#288), the files in
``quarantine/replaced/`` whose recording the library holds once they have sat
there ``REPLACED_RETENTION_DAYS`` (default 14; ``0`` turns it off), counted
from the move (the file's ctime: a rename keeps the mtime).  It never deletes
an only copy.  The scan pushes what is left there by kind.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

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


# The flex attr that marks an album record's local-only extra (#287).
EXTRA = "album_extra"


def copies(items, key: str, record: dict, keep: str | None, drop: str | None) -> list:
    """The record's copies :func:`retire` weighs: with *drop*, that release's;
    else every copy but release *keep*'s, legacy items, spotdl top-ups and
    earlier extras included."""
    from music_scan.rollback import _claims  # noqa: PLC0415

    if drop is not None:
        return [i for i in items if i.get("album_record") == key and i.get("release") == drop]
    return [i for i in items if (i.get("album_record") == key and i.get("release") != keep)
            or (not i.get("album_record") and i.get("via") == "usenet" and _claims(record, i)
                and i.get("spotify_album_id") in (None, "", key))
            or (not i.get("album_record") and _topup(i, key, record))]


def _topup(item, key: str, record: dict) -> bool:
    """A spotdl copy on the album's Spotify album that no playlist but the
    record's own lists: spotdl's top-up, which a reimport retires.  A spotdl
    copy another playlist also lists is a loose copy, left to supersede."""
    from music_scan.identity import split_list  # noqa: PLC0415

    return (item.get("via") == "spotdl" and item.get("spotify_album_id") == key
            and set(split_list(item.get("sources"))) <= set(record.get("playlists") or {}))


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


def retire(lib, key: str, record: dict, keep: str | None, drop: str | None) -> int:
    """Quarantine the :func:`copies` whose recording release *keep* holds to
    ``quarantine/replaced/``, and keep the others on the album as local-only
    extras (#287); with *drop*, quarantine that release's items.  Returns the
    count quarantined."""
    from music_scan.canon import WAIT  # noqa: PLC0415
    from music_scan.rollback import quarantine  # noqa: PLC0415

    items = list(lib.all_items())
    others = copies(items, key, record, keep, drop)
    gone, extras = (others, []) if drop is not None else superseded(items, key, keep, others)
    for item in gone:
        logger.info("  [RETIRE] %s %s — %s [%s] (%s)", item.id, item.get("artist"), item.get("title"),
                    item.get("album"), _origin(item))
    for item in extras:
        logger.info("  [LOCAL] %s %s — %s [%s] (%s): the kept release lacks its recording; stays on the album",
                    item.id, item.get("artist"), item.get("title"), item.get("album"), _origin(item))
        if item.get("album_record") != key or not item.get(EXTRA):
            item["album_record"] = key
            item[EXTRA] = "1"
            item[WAIT] = "1"  # the next scan's canon files it under the record's album
            item.store()
    quarantine(gone)
    return len(gone)


def retire_held(lib) -> int:
    """Quarantine every local-only extra (``album_extra``) whose recording its
    album record's kept release now holds (#294): the release's items without
    ``album_extra``.  :func:`retire` weighs the copies before ``tag_album_ids``
    gives the release Spotify IDs, so a release beets matched to nothing in
    MusicBrainz holds no recording yet and keeps every copy as an extra.  The
    album completion runs this after ``tag_album_ids`` and before canon, and
    every scan runs it before its canon, so IDs that arrive later count too.
    Only an extra leaves, and only for a recording another item of its record
    holds: the library keeps one copy of it.  Returns the count quarantined."""
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
    quarantine(gone)
    return len(gone)


# ---------------------------------------------------------------------------
# music-album-audit-releases: albums mixed from several releases before #279
# ---------------------------------------------------------------------------


def mixed_reasons(key: str, record: dict, items: list) -> list[str]:
    """Why an imported album record's album is not one release: what the
    track-level rules before #279 did to it, and a reimport would undo.
    Empty for one release.

    An album imported whole since #279 (an item of the record's own release
    on it) is one release: its import retired every copy :func:`copies`
    takes, so a reimport would only grab the same album again (#295).  Nor
    do local-only extras count, or a spotdl copy another playlist also lists:
    :func:`retire` leaves both on purpose."""
    from music_fetch.albums import GAP, IMPORTED, PARTIAL  # noqa: PLC0415

    if record.get("status") != IMPORTED and record.get("fallback_from") not in (PARTIAL, GAP):
        return []
    mine = [i for i in items if i.get("spotify_album_id") == key and not i.get(EXTRA)
            and not (i.get("via") == "spotdl" and not _topup(i, key, record))]
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


def replaced_copies(record: dict, replaced_dir: Path) -> list[Path]:
    """The files in *replaced_dir* whose album and artist tags *record* claims
    (as ``music-rollback-releases`` attributes items): its rolled-back,
    superseded and merged-away copies."""
    from mediafile import MediaFile  # noqa: PLC0415

    from music_scan.rollback import _claims  # noqa: PLC0415

    found = []
    for f in audio_files(replaced_dir) if replaced_dir.is_dir() else []:
        try:
            mf = MediaFile(str(f))
        except Exception:
            continue
        if _claims(record, _Tags(album=mf.album, albumartist=mf.albumartist, artist=mf.artist)):
            found.append(f)
    return found


def split_replaced(files: list[Path], library) -> tuple[list[Path], list[Path]]:
    """``(superseded, only_copies)``: which replaced *files* hold a recording
    *library* (a :class:`music_scan.identity.Recordings` of the library's
    items) holds by their identity tags (Spotify ID, ISRC, ``mb_trackid``),
    and which hold one it doesn't (#287).  An unreadable file, or one with no
    identity tags, is an only copy: nothing says the library has it."""
    from music_scan.playlists import _file_tags  # noqa: PLC0415

    held, only = [], []
    for f in files:
        rf = _file_tags(f)
        (held if rf is not None and library.holds(rf.recording_ids) else only).append(f)
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


def audit_main() -> None:
    import argparse  # noqa: PLC0415

    from music_fetch.albums import STATE_FILE, State, find_albums  # noqa: PLC0415
    from music_scan.audit import REPLACED  # noqa: PLC0415
    from music_scan.identity import Recordings  # noqa: PLC0415
    from music_scan.library import LIBRARY_DB, MusicLibrary  # noqa: PLC0415

    parser = argparse.ArgumentParser(
        prog="music-album-audit-releases",
        description="List imported albums mixed from several releases before #279, each with its reset command; "
                    "with --replaced, list (and with --apply delete) one album's copies in quarantine/replaced/")
    parser.add_argument("--replaced", metavar="QUERY",
                        help="the album's Spotify ID, its 'artist — album', or a part of it that matches one album")
    parser.add_argument("--apply", action="store_true",
                        help="with --replaced: delete the files whose recording the library holds")
    parser.add_argument("--only-copies", action="store_true",
                        help="with --replaced --apply: delete the only copies too (#287)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    records = State.load(STATE_FILE).albums
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
    with MusicLibrary(LIBRARY_DB) as lib:
        items = list(lib.all_items())
    n = 0
    for key, record in records.items():
        reasons = mixed_reasons(key, record, items)
        if not reasons:
            continue
        n += 1
        name = f"{record.get('artist')} — {record.get('name')}"
        logger.info("[MIXED] %s (%s): %s", name, key, "; ".join(reasons))
        logger.info("    music-album-reset '%s' --reimport", album_query(records, key, name))
    logger.info("%d imported album(s) mixed from several releases; reset each with --reimport (dry run, then "
                "--apply), then clear its old copies with --replaced", n)


def main() -> None:
    """``python -m music_scan.release <dir>``: print :func:`match_folder`'s result as JSON."""
    from beets import config, plugins  # noqa: PLC0415

    logging.basicConfig(level=logging.WARNING)
    config.read()
    plugins.load_plugins()
    print(json.dumps(match_folder(audio_files(Path(sys.argv[1])))))


if __name__ == "__main__":
    main()
