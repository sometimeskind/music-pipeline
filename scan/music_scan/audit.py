"""music-audit-lengths: find library items whose audio is not the Spotify track (#165).

The length guard only checks new downloads.  This audit reads every configured
playlist, ``nosync`` and ``album`` ones included, from Spotify's item pages only
(``SpotifyPlaylists.songs``: about one call per 100 tracks, never per-track
calls) and compares each entry's ``duration`` with its library item's length.
Items are matched by Spotify ID, then ISRC anywhere in the library, then
title+artist among the playlist's own items.  Suspects are listed with the
YouTube video spotdl downloaded them from (the file's comment tag).

``--silence`` also scans every matched file for mid-track silence (slow: it
decodes each one).  ``--quarantine`` runs the guard's checks over the files
already in quarantine and logs what it would reject.

``--replace ITEM_ID [--youtube URL] --apply`` fixes one suspect **in place**.
A wrong-audio file carries the right Spotify ID and ISRC, so a re-download
through the inbox would be merged into the old item by the duplicate hook and
discarded.  Instead the entry is downloaded to a staging directory (pinned to
``--youtube``, which is what spotdl's ``youtube_url|spotify_url`` form does,
with no extra Spotify call), checked by the guard, and swapped in for the old
file, which moves to ``quarantine/replaced/``.  The beets item keeps its id,
tags, ``sources`` and IDs, so the ``.m3u`` files are unchanged.

Dry run by default; ``--apply`` replaces.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import logging
import os
import shutil
import tempfile
from pathlib import Path

from music_scan import guard
from music_scan.identity import BY_WORDS, ItemIndex, item_spotify_ids, spotify_id, split_list

logger = logging.getLogger(__name__)

QUARANTINE = Path("/root/Music/quarantine")
LIBRARY = Path("/root/Music/library")
REPLACED = QUARANTINE / "replaced"
MIN_DELTA_SECONDS = 5
LOCK_TIMEOUT_SECONDS = 1800


@dataclasses.dataclass
class Suspect:
    item: object
    song: dict
    playlists: list[str]
    expected: int
    actual: float
    rung: str
    silence: tuple[float, float] | None = None

    @property
    def delta(self) -> float:
        return self.actual - self.expected


def _song_id(song: dict) -> str | None:
    return song.get("song_id") or spotify_id(song.get("url"))


def _label(song: dict) -> str:
    return f"{(song.get('artists') or ['?'])[0]} — {song.get('name', '?')}"


def item_path(item) -> Path:
    raw = Path(item.path.decode() if isinstance(item.path, bytes) else item.path)
    return raw if raw.is_absolute() else LIBRARY / raw


def off_by(expected: float, actual: float, tolerance: float, min_delta: float) -> bool:
    delta = abs(actual - expected)
    return delta > expected * tolerance and delta >= min_delta


def audit(items: list, songs_by_playlist: dict[str, list[dict]], tolerance: float = guard.TOLERANCE,
          min_delta: float = MIN_DELTA_SECONDS, silence: bool = False) -> tuple[list[Suspect], dict]:
    """Suspects (most off first) and counts: ``matched``, ``unmatched``, ``no_duration``."""
    library = ItemIndex(items)
    counts = {"matched": 0, "unmatched": 0, "no_duration": 0}
    suspects: dict[int, Suspect] = {}
    checked: set[int] = set()
    for playlist, songs in songs_by_playlist.items():
        own = ItemIndex(i for i in items if playlist in split_list(i.get("sources")))
        for song in songs:
            item, rung = library.match_song(song, words=False)
            if item is None:
                item, rung = own.match_song(song)
            if item is None:
                counts["unmatched"] += 1
                continue
            counts["matched"] += 1
            if item.id in suspects:
                suspects[item.id].playlists.append(playlist)
                continue
            if item.id in checked:
                continue
            checked.add(item.id)
            expected, actual = song.get("duration"), float(item.length or 0)
            gap = None
            if silence and item_path(item).exists():
                gap = guard.mid_track_silence(guard.silence_windows(item_path(item)), actual)
            if not expected:
                counts["no_duration"] += 1
            if gap or (expected and off_by(expected, actual, tolerance, min_delta)):
                suspects[item.id] = Suspect(item, song, [playlist], expected or 0, actual, rung, gap)
    return sorted(suspects.values(), key=lambda s: -abs(s.delta)), counts


def report(suspects: list[Suspect], counts: dict) -> None:
    for s in suspects:
        why = f"expected {s.expected}s, has {s.actual:.0f}s ({s.delta:+.0f}s)"
        if s.silence:
            why += f"; silent {s.silence[0]:.0f}–{s.silence[1]:.0f}s"
        if s.rung == BY_WORDS:
            why += "; matched by title+artist only"
        logger.info(
            "[SUSPECT] %s: %s — %s — item %s, %s, source %s",
            ",".join(s.playlists), _label(s.song), why, s.item.id, item_path(s.item), s.item.get("comments") or "?",
        )
    logger.info(
        "Audit: %d suspect(s) among %d matched playlist entr(ies); %d entr(ies) with no library item, "
        "%d with no Spotify duration",
        len(suspects), counts["matched"], counts["unmatched"], counts["no_duration"],
    )


def audit_quarantine(quarantine: Path, songs_by_playlist: dict[str, list[dict]]) -> int:
    """Log what the guard would reject among the quarantine's files; returns the count."""
    durations = {s["url"]: s["duration"] for songs in songs_by_playlist.values()
                 for s in songs if s.get("url") and s.get("duration")}
    count = 0
    for f in sorted(quarantine.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in {".m4a", ".mp3", ".opus"}:
            continue
        if f.relative_to(quarantine).parts[0] in ("usenet", "rejected", "replaced"):
            continue
        rejection = guard.check_file(f, durations.get(guard.song_url(f) or ""))
        if rejection:
            count += 1
            logger.warning("[WOULD-REJECT] %s — %s: %s", f.relative_to(quarantine), rejection.reason, rejection.detail)
    logger.info("Quarantine: the guard would reject %d file(s)", count)
    return count


def find_song(item, songs_by_playlist: dict[str, list[dict]]) -> dict | None:
    """The playlist entry for *item*, by its Spotify IDs."""
    ids = item_spotify_ids(item)
    return next((s for songs in songs_by_playlist.values() for s in songs if _song_id(s) in ids), None)


@contextlib.contextmanager
def pipeline_lock():
    """Hold the service's ``pipeline`` lock, so no scan runs during the swap."""
    if not os.environ.get("PREFECT_API_URL"):
        logger.warning("PREFECT_API_URL unset — replacing without the pipeline lock")
        yield
        return
    from prefect.concurrency.sync import concurrency  # noqa: PLC0415

    with concurrency("pipeline", occupy=1, timeout_seconds=LOCK_TIMEOUT_SECONDS):
        yield


def swap_in(item, new_file: Path, replaced_dir: Path, download_url: str | None) -> Path:
    """Put *new_file*'s audio under *item*, keeping the item and its tags.  Returns the old file's new home."""
    from beets.util import bytestring_path  # noqa: PLC0415

    old = item_path(item)
    backup = replaced_dir / f"{item.id}-{old.name}"
    backup.parent.mkdir(parents=True, exist_ok=True)
    dest = old.with_suffix(new_file.suffix)
    shutil.move(str(old), backup)
    shutil.move(str(new_file), dest)
    item.path = bytestring_path(str(dest))
    if download_url:
        item["comments"] = download_url
    item.write()  # beets' tags onto the new file
    item.read()  # length, bitrate, format and mtime from it
    item.store()
    return backup


def replace(item, song: dict, youtube: str | None, cookie_file: Path, force: bool = False,
            replaced_dir: Path = REPLACED) -> bool:
    """Download *song* (pinned to *youtube*) and swap it in for *item*'s file.  True when replaced."""
    from music_fetch.spotdl_ops import download_song  # noqa: PLC0415

    song = dict(song, download_url=youtube) if youtube else dict(song)
    with tempfile.TemporaryDirectory(prefix="replace-") as staging:
        new = download_song(song, Path(staging), cookie_file)
        if new is None:
            logger.warning("[REPLACE] %s: download failed; item %s left as is", _label(song), item.id)
            return False
        new = Path(new)
        url = guard.read_comment(new) or youtube
        if not youtube and url and url == item.get("comments"):
            logger.warning("[REPLACE] %s: spotdl picked the same video (%s); pass --youtube", _label(song), url)
            return False
        rejection = guard.check_file(new, song.get("duration"))
        if rejection and not force:
            logger.warning("[REPLACE] %s: the new download fails the guard too (%s: %s); --force to use it anyway",
                           _label(song), rejection.reason, rejection.detail)
            return False
        with pipeline_lock():
            backup = swap_in(item, new, replaced_dir, url)
    logger.info("[REPLACE] %s: item %s now %s from %s (%.0fs); old file → %s",
                _label(song), item.id, item_path(item), url or "?", float(item.length or 0), backup)
    return True


def run(apply: bool = False, tolerance: float = guard.TOLERANCE, min_delta: float = MIN_DELTA_SECONDS,
        silence: bool = False, quarantine: bool = False, replace_id: int | None = None,
        youtube: str | None = None, force: bool = False) -> list[Suspect]:
    from music_fetch import ingest  # noqa: PLC0415
    from music_fetch.config import load_playlists  # noqa: PLC0415
    from music_fetch.spotdl_ops import SpotifyPlaylists  # noqa: PLC0415
    from music_scan.library import LIBRARY_DB, MusicLibrary  # noqa: PLC0415
    from music_scan.navidrome import trigger_scan  # noqa: PLC0415

    with MusicLibrary(LIBRARY_DB) as lib:
        target = lib.get_item(replace_id) if replace_id is not None else None
        if replace_id is not None and target is None:
            raise SystemExit(f"no library item with id {replace_id}")
        playlists = load_playlists(ingest.CONF_PATH)
        if target is not None:
            # Only the playlists the item is on: fewer Spotify calls.
            mine = [p for p in playlists if p.name in split_list(target.get("sources"))]
            playlists = mine or playlists
        reader = SpotifyPlaylists(ingest.COOKIE_FILE)
        songs_by_playlist = {}
        for pl in playlists:
            songs_by_playlist[pl.name] = reader.songs(pl.url)
            logger.info("Read %s: %d track(s)", pl.name, len(songs_by_playlist[pl.name]))

        if target is not None:
            song = find_song(target, songs_by_playlist)
            if song is None:
                raise SystemExit(f"item {replace_id} matches no entry on {', '.join(songs_by_playlist)} by Spotify ID")
            logger.info("Replace item %s (%s, %.0fs, source %s) with %s (%ss) from %s",
                        replace_id, item_path(target), float(target.length or 0), target.get("comments") or "?",
                        _label(song), song.get("duration"), youtube or "a new YouTube search")
            if not apply:
                logger.info("Dry run — nothing downloaded. Re-run with --apply to replace.")
                return []
            if replace(target, song, youtube, ingest.COOKIE_FILE, force=force):
                trigger_scan()
            return []

        suspects, counts = audit(lib.all_items(), songs_by_playlist, tolerance, min_delta, silence)
        report(suspects, counts)
    if quarantine:
        audit_quarantine(QUARANTINE, songs_by_playlist)
    return suspects


def main() -> None:
    parser = argparse.ArgumentParser(prog="music-audit-lengths", description=__doc__.splitlines()[0])
    parser.add_argument("--tolerance", type=float, default=guard.TOLERANCE,
                        help="flag lengths off by more than this fraction (default %(default)s)")
    parser.add_argument("--min-delta", type=float, default=MIN_DELTA_SECONDS,
                        help="and by at least this many seconds (default %(default)s)")
    parser.add_argument("--silence", action="store_true", help="also check every matched file for mid-track silence")
    parser.add_argument("--quarantine", action="store_true", help="also run the guard over the quarantine's files")
    parser.add_argument("--replace", type=int, metavar="ITEM_ID", help="re-download this item and swap it in")
    parser.add_argument("--youtube", metavar="URL", help="with --replace: download from this YouTube video")
    parser.add_argument("--force", action="store_true", help="with --replace: swap in even if the guard rejects it")
    parser.add_argument("--apply", action="store_true", help="with --replace: do it (default: dry run)")
    args = parser.parse_args()
    if (args.youtube or args.force or args.apply) and args.replace is None:
        parser.error("--youtube, --force and --apply need --replace")
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    run(apply=args.apply, tolerance=args.tolerance, min_delta=args.min_delta, silence=args.silence,
        quarantine=args.quarantine, replace_id=args.replace, youtube=args.youtube, force=args.force)
