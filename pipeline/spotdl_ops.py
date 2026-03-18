"""spotdl operations implemented via the spotdl Python library.

We use spotdl's internal classes directly instead of subprocess so that:
- The snapshot diff is computed before downloading (no temp file needed).
- Removed tracks are identified natively as a set difference.
- We control sync_without_deleting explicitly (we do soft deletes via beets).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def _make_downloader_settings(
    cookie_file: Path,
    output_dir: Path | None = None,
    save_file: Path | None = None,
    sync_without_deleting: bool = True,
) -> dict:
    settings: dict = {
        "cookie_file": str(cookie_file),
        "format": "m4a",
        "bitrate": "disable",
        "overwrite": "skip",
        "sync_without_deleting": sync_without_deleting,
        "load_config": False,
        "threads": 4,
    }
    if output_dir is not None:
        settings["output"] = str(output_dir)
    if save_file is not None:
        settings["save_file"] = str(save_file)
    return settings


def _make_spotdl(settings: dict):
    """Initialise and return a Spotdl instance."""
    from spotdl import Spotdl  # noqa: PLC0415

    client_id = os.environ["SPOTIFY_CLIENT_ID"]
    client_secret = os.environ["SPOTIFY_CLIENT_SECRET"]
    return Spotdl(
        client_id=client_id,
        client_secret=client_secret,
        downloader_settings=settings,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def save_playlist(url: str, spotdl_file: Path, output_dir: Path, cookie_file: Path) -> None:
    """Fetch a Spotify playlist's metadata and write a .spotdl sync file.

    Equivalent to: spotdl save <url> --save-file <file>
    Idempotent: callers should check whether the file already exists before calling.
    """
    from spotdl.console.save import save as _save  # noqa: PLC0415

    spotdl_obj = _make_spotdl(
        _make_downloader_settings(
            cookie_file=cookie_file,
            output_dir=output_dir,
            save_file=spotdl_file,
        )
    )
    logger.info("Fetching playlist metadata for %s", url)
    _save([url], spotdl_obj.downloader)


def sync_playlist(
    spotdl_file: Path,
    output_dir: Path,
    cookie_file: Path,
    track_limit: int | None = None,
) -> tuple[set[str], int]:
    """Sync a playlist from its .spotdl file.

    Downloads tracks new to the Spotify playlist, up to *track_limit* new
    downloads this session.  When *track_limit* is None all new tracks are
    downloaded.  Tracks deferred by the limit are excluded from the updated
    snapshot so they re-appear as new on the next run.

    Does NOT delete downloaded files for removed tracks — we handle that
    separately via beets source-tag removal (soft delete).

    Returns a tuple of:
      - the set of Spotify track URLs removed from the playlist since the last sync
      - the number of tracks sent to spotdl this session (tracks *sent*, not confirmed
        completions — spotdl may download fewer if some are unavailable on YouTube)

    Note on ordering: when *track_limit* is set, the batch is taken from the front of
    the list returned by spotdl.search(), which for Spotify playlists is typically
    playlist order (oldest-added first for Liked Songs).  This means the same leading
    batch is retried each session until fully downloaded, then the next batch follows.
    """
    with open(spotdl_file, encoding="utf-8") as fh:
        sync_data = json.load(fh)

    if sync_data.get("type") != "sync":
        raise ValueError(f"Not a valid spotdl sync file: {spotdl_file}")

    old_urls: set[str] = {s["url"] for s in sync_data.get("songs", [])}
    query: list[str] = sync_data["query"]

    spotdl_obj = _make_spotdl(
        _make_downloader_settings(
            cookie_file=cookie_file,
            output_dir=output_dir,
            sync_without_deleting=True,
        )
    )

    # Fetch current Spotify playlist state.
    logger.info("Fetching current Spotify state for %s", spotdl_file.stem)
    new_songs = spotdl_obj.search(query)
    new_urls: set[str] = {s.url for s in new_songs}

    removed_urls = old_urls - new_urls
    if removed_urls:
        logger.info("%d track(s) removed from Spotify playlist", len(removed_urls))

    # Identify tracks not yet downloaded (absent from the previous snapshot).
    truly_new = [s for s in new_songs if s.url not in old_urls]
    total_new = len(truly_new)

    if track_limit is not None and total_new > track_limit:
        logger.info(
            "Track budget: downloading %d of %d new track(s) this session (%d deferred to next run)",
            track_limit,
            total_new,
            total_new - track_limit,
        )
        truly_new = truly_new[:track_limit]

    # Download only the new batch; existing tracks are already on disk (overwrite=skip).
    spotdl_obj.download_songs(truly_new)

    # Persist: previously known songs still on Spotify + newly downloaded batch.
    # Unprocessed songs are intentionally excluded — they'll be picked up next session.
    batch_urls = {s.url for s in truly_new}
    songs_to_write = [s for s in new_songs if s.url in old_urls or s.url in batch_urls]

    with open(spotdl_file, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "type": "sync",
                "query": query,
                "songs": [s.json for s in songs_to_write],
            },
            fh,
            indent=4,
            ensure_ascii=False,
        )

    return removed_urls, len(truly_new)


def find_track_in_snapshot(snapshot: list[dict], url: str) -> dict | None:
    """Return the first song entry in a .spotdl snapshot that matches *url*."""
    return next((t for t in snapshot if t.get("url") == url), None)
