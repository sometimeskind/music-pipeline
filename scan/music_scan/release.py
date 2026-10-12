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
  (never deleted, #202): items of an earlier or worse release, the record's
  legacy items from before #279 (claimed by album name and artist, as
  ``music-rollback-releases`` attributes them, and on no other Spotify album),
  and spotdl's top-ups of the album (on its Spotify album and on no playlist
  but the record's own): the album stays one source.  A spotdl copy another
  playlist also lists is a loose copy, left to supersede.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import tempfile
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


def claim(items, playlist: str, since: float, key: str, guid: str) -> list:
    """Stamp the usenet *items* of *playlist* added since *since* that no
    record holds yet with ``album_record`` *key* and ``release`` *guid*.
    Returns them."""
    from music_scan.identity import split_list  # noqa: PLC0415

    mine = [i for i in items if i.get("via") == "usenet" and (i.added or 0) >= since
            and playlist in split_list(i.get("sources")) and not i.get("album_record")]
    for item in mine:
        item["album_record"] = key
        item["release"] = guid
        item.store()
    return mine


def copies(items, key: str, record: dict, keep: str | None, drop: str | None) -> list:
    """The record's items :func:`retire` takes: with *drop*, that release's;
    else every copy but release *keep*'s, legacy items and spotdl top-ups included."""
    from music_scan.identity import split_list  # noqa: PLC0415
    from music_scan.rollback import _claims  # noqa: PLC0415

    if drop is not None:
        return [i for i in items if i.get("album_record") == key and i.get("release") == drop]
    own = set(record.get("playlists") or {})

    def topup(i) -> bool:
        return (i.get("via") == "spotdl" and i.get("spotify_album_id") == key
                and set(split_list(i.get("sources"))) <= own)

    return [i for i in items if (i.get("album_record") == key and i.get("release") != keep)
            or (not i.get("album_record") and i.get("via") == "usenet" and _claims(record, i)
                and i.get("spotify_album_id") in (None, "", key))
            or (not i.get("album_record") and topup(i))]


def retire(lib, key: str, record: dict, keep: str | None, drop: str | None) -> int:
    """Quarantine :func:`copies` to ``quarantine/replaced/``.  Returns the count."""
    from music_scan.rollback import quarantine  # noqa: PLC0415

    gone = copies(lib.all_items(), key, record, keep, drop)
    for item in gone:
        logger.info("  [RETIRE] %s %s — %s [%s] (%s)", item.id, item.get("artist"), item.get("title"),
                    item.get("album"), "release " + str(item.get("release")) if item.get("release") else "legacy")
    quarantine(gone)
    return len(gone)


# ---------------------------------------------------------------------------
# music-album-audit-releases: albums mixed from several releases before #279
# ---------------------------------------------------------------------------


def mixed_reasons(key: str, record: dict, items: list) -> list[str]:
    """Why an imported album record's album is not one release: what the
    track-level rules before #279 did to it.  Empty for one release."""
    from music_fetch.albums import GAP, IMPORTED, PARTIAL  # noqa: PLC0415

    if record.get("status") != IMPORTED and record.get("fallback_from") not in (PARTIAL, GAP):
        return []
    mine = [i for i in items if i.get("spotify_album_id") == key]
    if mine and all(i.get("album_record") == key for i in mine):
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


def audit_main() -> None:
    import argparse  # noqa: PLC0415

    from music_fetch.albums import STATE_FILE, State  # noqa: PLC0415
    from music_scan.audit import REPLACED  # noqa: PLC0415
    from music_scan.library import LIBRARY_DB, MusicLibrary  # noqa: PLC0415

    parser = argparse.ArgumentParser(
        prog="music-album-audit-releases",
        description="List imported albums mixed from several releases before #279, each with its reset command; "
                    "with --replaced, list (and with --apply delete) one album's copies in quarantine/replaced/")
    parser.add_argument("--replaced", metavar="QUERY", help="part of 'artist — album' that matches one album")
    parser.add_argument("--apply", action="store_true", help="with --replaced: delete the listed files")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    records = State.load(STATE_FILE).albums
    if args.replaced:
        q = args.replaced.lower()
        hits = [r for r in records.values() if q in f"{r.get('artist', '')} — {r.get('name', '')}".lower()]
        if len(hits) != 1:
            logger.error("%d album(s) match %r", len(hits), args.replaced)
            raise SystemExit(1)
        files = replaced_copies(hits[0], REPLACED)
        for f in files:
            logger.info("  [REPLACED] %s", f)
        if not args.apply:
            logger.info("%d file(s) in %s; dry run, --apply deletes them", len(files), REPLACED)
            return
        for f in files:
            f.unlink(missing_ok=True)
        logger.info("Deleted %d file(s)", len(files))
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
        logger.info("    music-album-reset '%s' --reimport", name.replace("'", "'\\''"))
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
