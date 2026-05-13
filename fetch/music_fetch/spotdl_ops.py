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
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_spotdl_instance = None  # process-wide singleton (SpotifyClient + ProgressHandler can't be reinitialised)

_BACKOFF_SCHEDULE = [7, 14, 28]  # days; last value repeats indefinitely


def _backoff_days(attempts: int) -> int:
    return _BACKOFF_SCHEDULE[min(attempts, len(_BACKOFF_SCHEDULE)) - 1]


def _load_failures(failures_file: Path) -> dict:
    if not failures_file.exists():
        return {}
    try:
        return json.loads(failures_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Could not read %s — treating as empty", failures_file)
        return {}


def _save_failures(failures_file: Path, data: dict) -> None:
    failures_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _song_label(song) -> str:
    """Format 'Artist - Title' from a Song or Song-like object for log output."""
    j = song.json
    name = j.get("name", "?")
    artists = j.get("artists", [])
    return f"{artists[0] if artists else '?'} - {name}"


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
        "yt_dlp_args": "--js-runtimes node",
    }
    if output_dir is not None:
        settings["output"] = str(output_dir)
    if save_file is not None:
        settings["save_file"] = str(save_file)
    return settings


def _make_spotdl(settings: dict):
    """Return a Spotdl instance, creating it on the first call.

    Both SpotifyClient and Rich's ProgressHandler are process-wide singletons
    that cannot be reinitialised.  We keep one module-level instance and update
    its Downloader settings on each call so per-playlist output_dir is respected.
    """
    global _spotdl_instance  # noqa: PLW0603
    from spotdl import Spotdl  # noqa: PLC0415

    if _spotdl_instance is not None:
        _spotdl_instance.downloader.settings.update(settings)
        return _spotdl_instance

    client_id = os.environ["SPOTIFY_CLIENT_ID"]
    client_secret = os.environ["SPOTIFY_CLIENT_SECRET"]
    _spotdl_instance = Spotdl(
        client_id=client_id,
        client_secret=client_secret,
        downloader_settings=settings,
    )
    return _spotdl_instance


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def save_playlist(url: str, spotdl_file: Path) -> None:
    """Write a stub .spotdl sync file for a newly provisioned playlist.

    Writes an empty songs list so the first sync treats all tracks as new and
    downloads them.  The snapshot is populated by sync_playlist() as tracks are
    downloaded; it records what has been downloaded, not what Spotify reports.
    Idempotent: callers should check whether the file already exists before calling.
    """
    sync_data = {
        "type": "sync",
        "query": [url],
        "songs": [],
    }
    with open(spotdl_file, "w", encoding="utf-8") as fh:
        json.dump(sync_data, fh, indent=4, ensure_ascii=False)
    logger.info("Provisioned stub .spotdl file for %s", spotdl_file.stem)


def sync_playlist(
    spotdl_file: Path,
    output_dir: Path,
    cookie_file: Path,
    track_limit: int | None = None,
    failures_file: Path | None = None,
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

    # Log SKIP for tracks already in the snapshot (not re-attempted this session).
    for song in new_songs:
        if song.url in old_urls:
            logger.info("[SKIP] %s", _song_label(song))

    # Identify tracks not yet downloaded (absent from the previous snapshot).
    truly_new = [s for s in new_songs if s.url not in old_urls]

    # Apply MISS backoff: filter out tracks whose retry window hasn't expired yet.
    failures: dict = {}
    if failures_file is not None:
        failures = _load_failures(failures_file)
        for url in removed_urls:
            failures.pop(url, None)
        now = datetime.now(timezone.utc)
        due, backed_off = [], []
        for song in truly_new:
            entry = failures.get(song.url)
            if entry and datetime.fromisoformat(entry["retry_after"]) > now:
                backed_off.append(song)
            else:
                due.append(song)
        for song in backed_off:
            logger.info("[BACK] %s → backed off until %s", _song_label(song), failures[song.url]["retry_after"][:10])
        truly_new = due

    total_new = len(truly_new)

    if track_limit is not None and total_new > track_limit:
        deferred = truly_new[track_limit:]
        truly_new = truly_new[:track_limit]
        logger.info(
            "Track budget: downloading %d of %d new track(s) this session (%d deferred to next run)",
            track_limit,
            total_new,
            total_new - track_limit,
        )
        for song in deferred:
            logger.info("[DEFER] %s", _song_label(song))

    # Download only the new batch; existing tracks are already on disk (overwrite=skip).
    results = spotdl_obj.download_songs(truly_new)

    # Log per-track outcomes.
    # MISS vs FAIL: if song.download_url is None, spotdl found no YouTube source (LookupError);
    # if it's set, a source was found but the download itself failed (AudioProviderError).
    for song, path in results:
        if path is not None:
            logger.info("[OK]   %s", _song_label(song))
            failures.pop(song.url, None)
        elif getattr(song, "download_url", None):
            logger.info("[FAIL] %s → download failed", _song_label(song))
        else:
            entry = failures.get(song.url, {"attempts": 0})
            attempts = entry["attempts"] + 1
            days = _backoff_days(attempts)
            retry_after = (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()
            failures[song.url] = {"attempts": attempts, "retry_after": retry_after}
            logger.info("[MISS] %s → no source found; backing off %d days (attempt %d)", _song_label(song), days, attempts)

    if failures_file is not None:
        _save_failures(failures_file, failures)

    # Only persist songs that were actually downloaded (path is not None).
    # Songs where spotdl returned None failed silently — exclude them from the snapshot
    # so they are retried as 'truly_new' on the next run.
    downloaded_urls = {song.url for song, path in results if path is not None}
    songs_to_write = [s for s in new_songs if s.url in old_urls or s.url in downloaded_urls]

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
