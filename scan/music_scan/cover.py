"""Embed Spotify's album cover on Usenet album tracks (#204).

Usenet imports end up with no art: the convert command's ``-vn`` drops a
FLAC's embedded picture, and ``fetchart``/``embedart`` act on album imports
only, never on singletons.  The cover comes from the same place spotdl's
does: every ``.spotdl`` song carries ``cover_url``, the album's largest
Spotify image, so no Spotify API calls are needed.

An item finds its cover by its ``spotify_ids`` (set after the import by
``tag_album_ids``).  An item with none (a ``[NOID]`` track) borrows the cover
of another item on the same album.  Items that already have art are left
alone.

At import the album completion embeds the covers of the tracks it imported.
``music-embed-covers`` backfills every ``via=usenet`` item with no art: dry
run by default, ``--apply`` writes.  Safe to re-run.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path

from music_scan.identity import item_spotify_ids, spotify_id

logger = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT = 30


def covers_by_id(spotdl_dir: Path) -> dict[str, str]:
    """``{spotify track id: cover_url}`` from every ``.spotdl`` snapshot."""
    covers: dict[str, str] = {}
    for f in sorted(spotdl_dir.glob("*.spotdl")):
        try:
            songs = json.loads(f.read_text(encoding="utf-8")).get("songs", [])
        except Exception:
            continue
        for song in songs:
            sid = song.get("song_id") or spotify_id(song.get("url"))
            url = song.get("cover_url")
            if sid and isinstance(url, str) and url.startswith("https://"):
                covers.setdefault(sid, url)
    return covers


def _path(item) -> str:
    return os.fsdecode(item.path)


def has_art(item) -> bool:
    """True when the file has an embedded image, or can't be read (nothing to embed into)."""
    from mediafile import MediaFile  # noqa: PLC0415

    try:
        return bool(MediaFile(_path(item)).images)
    except Exception as exc:
        logger.warning("  [NOART] %s: unreadable: %s", _path(item), exc)
        return True


def _album(item) -> tuple[str, str]:
    return ((item.albumartist or item.artist or "").lower(), (item.album or "").lower())


def plan_covers(items: Iterable, covers: dict[str, str]) -> tuple[dict[int, tuple[object, str]], list]:
    """Usenet items with no art → their cover URL.

    Returns ``({item.id: (item, url)}, [items with no cover found])``.
    """
    bare = [i for i in items if i.get("via") == "usenet" and not has_art(i)]
    planned: dict[int, tuple[object, str]] = {}
    album_cover: dict[tuple[str, str], str] = {}
    for item in bare:
        url = next((covers[s] for s in sorted(item_spotify_ids(item)) if s in covers), None)
        if url:
            planned[item.id] = (item, url)
            album_cover.setdefault(_album(item), url)
    missing = []
    for item in bare:
        if item.id in planned:
            continue
        url = album_cover.get(_album(item))
        if url:
            planned[item.id] = (item, url)
        else:
            missing.append(item)
    return planned, missing


def download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as resp:  # noqa: S310 — https only, from .spotdl
        return resp.read()


def embed(item, data: bytes) -> None:
    """Write *data* as the item file's front cover."""
    from mediafile import Image, ImageType, MediaFile  # noqa: PLC0415

    mf = MediaFile(_path(item))
    mf.images = [Image(data=data, type=ImageType.front)]
    mf.save()


def _label(album: tuple[str, str]) -> str:
    return f"{album[0] or '?'} — {album[1] or '?'}"


def embed_covers(
    items: Iterable,
    covers: dict[str, str],
    apply: bool = True,
    fetch: Callable[[str], bytes] | None = None,
) -> int:
    """Embed the Spotify cover on every usenet item in *items* with no art.

    Logs one ``[ART]`` line per album and ``[NOART]`` per item with no cover.
    A failed download or write is logged and skipped: the backfill retries it.
    Returns the count of items embedded (or that would be, on a dry run).
    """
    fetch = fetch or download
    planned, missing = plan_covers(items, covers)
    for item in missing:
        logger.warning("  [NOART] %s: no Spotify cover found (no matching .spotdl entry)", _path(item))

    by_album: dict[tuple[str, str], list[tuple[object, str]]] = {}
    for item, url in planned.values():
        by_album.setdefault(_album(item), []).append((item, url))

    images: dict[str, bytes | None] = {}
    count = 0
    for album, entries in by_album.items():
        if not apply:
            logger.info("  [ART] %s: would embed the cover on %d track(s)", _label(album), len(entries))
            count += len(entries)
            continue
        done = 0
        for item, url in entries:
            if url not in images:
                try:
                    images[url] = fetch(url)
                except Exception as exc:
                    logger.warning("  [NOART] %s: cover download failed (%s): %s", _label(album), url, exc)
                    images[url] = None
            if images[url] is None:
                continue
            try:
                embed(item, images[url])
            except Exception as exc:
                logger.warning("  [NOART] %s: embedding failed: %s", _path(item), exc)
                continue
            done += 1
        if done:
            logger.info("  [ART] %s: embedded the cover on %d track(s)", _label(album), done)
        count += done
    return count


def run(apply: bool = False) -> int:
    from music_fetch import ingest  # noqa: PLC0415
    from music_scan.library import LIBRARY_DB, LIBRARY_DIR, MusicLibrary  # noqa: PLC0415
    from music_scan.navidrome import trigger_scan  # noqa: PLC0415

    covers = covers_by_id(ingest.SPOTDL_DIR)
    with MusicLibrary(LIBRARY_DB, LIBRARY_DIR) as lib:
        items = [i for i in lib.all_items() if i.get("via") == "usenet"]
        logger.info("%d usenet item(s); %d cover URL(s) in the .spotdl files", len(items), len(covers))
        count = embed_covers(items, covers, apply=apply)
    if not apply:
        logger.info("Dry run: %d item(s) would get a cover. Re-run with --apply to write.", count)
        return count
    logger.info("Embedded the cover on %d item(s)", count)
    if count:
        trigger_scan()
    return count


def main() -> None:
    parser = argparse.ArgumentParser(prog="music-embed-covers", description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the covers (default: dry run)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    run(apply=args.apply)
