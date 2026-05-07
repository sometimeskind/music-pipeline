"""Reconcile .spotdl snapshots against the beets library.

After each scan, URLs in the snapshot that are absent from both the beets
library and the quarantine directory are dropped. This allows spotdl to
re-download them on the next fetch run rather than silently skipping them
forever.

Called by the orchestrator after every scan, before the library push.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from music_scan.library import MusicLibrary

logger = logging.getLogger(__name__)

SPOTDL_DIR = Path("/root/Music/inbox/spotdl")
LIBRARY_DB = Path("/root/.config/beets/library.db")
QUARANTINE_DIR = Path("/root/Music/quarantine")


def _read_spotify_url(path: Path) -> str | None:
    """Return the Spotify URL embedded in an M4A file by spotdl, or None."""
    try:
        from mutagen.mp4 import MP4  # noqa: PLC0415 — beets dep, always available

        audio = MP4(str(path))
        raw = (audio.tags or {}).get("----:spotdl:WOAS")
        if raw:
            return raw[0].decode("utf-8")
    except Exception:
        pass
    return None


def _quarantine_urls(quarantine_dir: Path) -> frozenset[str]:
    """Return the set of Spotify URLs found in the quarantine directory."""
    urls: set[str] = set()
    for m4a in quarantine_dir.rglob("*.m4a"):
        url = _read_spotify_url(m4a)
        if url:
            urls.add(url)
    return frozenset(urls)


def reconcile_snapshot(
    spotdl_file: Path,
    library: "MusicLibrary",
    safe_urls: frozenset[str] | set[str] = frozenset(),
) -> int:
    """Diff one .spotdl snapshot against the beets library.

    *safe_urls* is the union of library-verified URLs and quarantine URLs
    (pre-computed by the caller so the quarantine is only scanned once).

    Drops snapshot entries whose URL is absent from *safe_urls*, logging a
    WARNING for each. Returns the count of dropped URLs.
    """
    with open(spotdl_file, encoding="utf-8") as fh:
        sync_data = json.load(fh)

    songs = sync_data.get("songs", [])
    if not songs:
        return 0

    playlist_name = spotdl_file.stem

    # Build verified URL set from beets library paths for this playlist.
    library_urls: set[str] = set()
    for path in library.paths_by_source(playlist_name):
        if not path.exists():
            logger.warning("Beets has stale path for %s: %s", playlist_name, path)
            continue
        url = _read_spotify_url(path)
        if url:
            library_urls.add(url)
        else:
            logger.warning("Could not read Spotify URL from library file: %s", path)

    all_safe = library_urls | safe_urls

    kept: list[dict] = []
    dropped = 0
    for song in songs:
        url = song.get("url", "")
        if url and url not in all_safe:
            logger.warning(
                "Dropping stale URL from %s snapshot (absent from library and quarantine): %s",
                playlist_name,
                url,
            )
            dropped += 1
        else:
            kept.append(song)

    if dropped:
        sync_data["songs"] = kept
        with open(spotdl_file, "w", encoding="utf-8") as fh:
            json.dump(sync_data, fh, indent=4, ensure_ascii=False)

    return dropped


def reconcile_all(
    spotdl_dir: Path = SPOTDL_DIR,
    library_db: Path = LIBRARY_DB,
    quarantine_dir: Path = QUARANTINE_DIR,
) -> int:
    """Reconcile all .spotdl snapshots against the beets library.

    Scans the quarantine directory once, then verifies each snapshot.
    Skips silently if library_db does not exist (e.g. first run).
    Returns the total number of URLs dropped across all playlists.
    """
    from music_scan.library import MusicLibrary  # noqa: PLC0415 — deferred; beets not always present

    if not library_db.exists():
        logger.info("Beets library.db not found — skipping reconciliation")
        return 0

    spotdl_files = sorted(spotdl_dir.glob("*.spotdl"))
    if not spotdl_files:
        return 0

    q_urls = _quarantine_urls(quarantine_dir)
    if q_urls:
        logger.debug("Quarantine contains %d known Spotify URL(s)", len(q_urls))

    total_dropped = 0
    with MusicLibrary(db_path=library_db) as lib:
        for spotdl_file in spotdl_files:
            try:
                dropped = reconcile_snapshot(spotdl_file, lib, q_urls)
                total_dropped += dropped
            except Exception:
                logger.exception("Failed to reconcile snapshot: %s", spotdl_file.name)

    if total_dropped:
        logger.warning(
            "Reconciliation: dropped %d stale URL(s) across %d playlist(s) — will re-download next fetch",
            total_dropped,
            len(spotdl_files),
        )
    else:
        logger.info("Reconciliation: all snapshot URLs verified against beets library")

    return total_dropped
