"""spotdl operations implemented via the spotdl Python library.

We use spotdl's internal classes directly instead of subprocess so that:
- The snapshot diff is computed before downloading (no temp file needed).
- Removed tracks are identified natively as a set difference.
- We control sync_without_deleting explicitly (we do soft deletes via beets).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_spotdl_instance = None  # process-wide singleton (SpotifyClient + ProgressHandler can't be reinitialised)

# Backoff schedules in days; the last value repeats indefinitely.
# "miss": no YouTube source found — unlikely to change quickly.
# "fail": a source was found but the download failed — usually transient (yt-dlp/YouTube
#         breakage), so retry soon, but still back off so permanently broken tracks don't
#         consume the per-run track budget forever.
_BACKOFF_SCHEDULES = {"miss": [7, 14, 28], "fail": [1, 2, 4]}


def _backoff_days(attempts: int, kind: str = "miss") -> int:
    schedule = _BACKOFF_SCHEDULES[kind]
    return schedule[min(attempts, len(schedule)) - 1]


@dataclasses.dataclass(frozen=True)
class SyncResult:
    """Outcome of one sync_playlist() call.

    - removed_urls: Spotify track URLs removed from the playlist since the last sync
    - attempted: tracks sent to spotdl this session (budget is consumed per attempt so a
      stuck [MISS] doesn't loop forever)
    - downloaded: tracks spotdl actually wrote to disk (path is not None)
    - missed: tracks with no YouTube source found ([MISS])
    - failed: tracks where a source was found but the download failed ([FAIL])
    - fail_reasons: Spotify track URL → reason string for every [FAIL] track, including
      the chained yt-dlp cause when spotdl exposed it (e.g. "HTTP Error 403: Forbidden")
    """

    removed_urls: set[str]
    attempted: int
    downloaded: int
    missed: int
    failed: int
    fail_reasons: dict[str, str]


_MAX_CAUSE_DEPTH = 5


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """Return the exceptions *exc* was raised from, outermost first (``__cause__`` then ``__context__``)."""
    chain: list[BaseException] = []
    seen = {id(exc)}
    while len(chain) < _MAX_CAUSE_DEPTH:
        nxt = exc.__cause__
        if nxt is None and not exc.__suppress_context__:
            nxt = exc.__context__
        if nxt is None or id(nxt) in seen:
            break
        chain.append(nxt)
        seen.add(id(nxt))
        exc = nxt
    return chain


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _download_error_reasons(errors, causes: dict[str, BaseException] | None = None) -> dict[str, str]:
    """Map Spotify track URL → "ExceptionName: message" from spotdl's Downloader.errors.

    spotdl appends ``f"{song.url} - {exc.__class__.__name__}: {exc}"`` for every failed
    download.  Entries without a URL prefix (e.g. "Song is missing required fields: …")
    are ignored.  Anything that isn't a list (e.g. a test double) yields no reasons.

    Only the formatted string survives in Downloader.errors, and spotdl wraps every yt-dlp
    failure in ``AudioProviderError("YT-DLP download error - <url>")`` — the real cause
    (a 403 on the media URL, "Sign in to confirm", …) is on ``__cause__``.  *causes* maps
    track URL → the exception object captured by :func:`_install_cause_capture`; when an
    entry is present its chain is appended as ``" (caused by Type: msg; Type: msg)"`` so the
    reason stays a single line that :func:`music_fetch.ingest.classify_failure` can read.
    """
    reasons: dict[str, str] = {}
    if not isinstance(errors, list):
        return reasons
    if not isinstance(causes, dict):
        causes = {}
    for err in errors:
        url, sep, reason = str(err).partition(" - ")
        if not (sep and url.startswith("http")):
            continue
        reason = _one_line(reason)
        exc = causes.get(url)
        chain = _exception_chain(exc) if isinstance(exc, BaseException) else []
        if chain:
            links = "; ".join(f"{c.__class__.__name__}: {_one_line(str(c))}" for c in chain)
            reason = f"{reason} (caused by {links})"
        reasons[url] = reason
    return reasons


_CAUSES_ATTR = "_music_fetch_causes"


def _install_cause_capture(spotdl_obj) -> dict[str, BaseException] | None:
    """Return a dict that receives ``song.url → exception`` for every failed download.

    spotdl only keeps the *string* of a download error in ``Downloader.errors``; the
    exception object (with its chained yt-dlp cause) is handed to the song's progress
    tracker via ``SongTracker.notify_error(message, exception, finish)`` and then dropped.
    We wrap ``ProgressHandler.get_new_tracker`` on the (process-wide singleton) Spotdl
    instance so each tracker's ``notify_error`` records the exception first.  The hook is
    installed once; callers clear the returned dict before every batch.

    Returns None when the object doesn't look like a real Spotdl (e.g. a test double
    without a progress handler), in which case reasons fall back to spotdl's string.
    """
    handler = getattr(getattr(spotdl_obj, "downloader", None), "progress_handler", None)
    if handler is None:
        return None
    existing = getattr(handler, _CAUSES_ATTR, None)
    if isinstance(existing, dict):
        return existing
    original_get_new_tracker = getattr(handler, "get_new_tracker", None)
    if not callable(original_get_new_tracker):
        return None

    captured: dict[str, BaseException] = {}

    def get_new_tracker(song):
        tracker = original_get_new_tracker(song)
        original_notify_error = tracker.notify_error

        def notify_error(message, traceback, finish=False):
            if isinstance(traceback, BaseException):
                captured[getattr(song, "url", "")] = traceback
            return original_notify_error(message, traceback, finish)

        tracker.notify_error = notify_error
        return tracker

    try:
        handler.get_new_tracker = get_new_tracker
        setattr(handler, _CAUSES_ATTR, captured)
    except AttributeError:
        return None
    return captured


def _record_failure(failures: dict, url: str, kind: str, reason: str = "") -> tuple[int, int]:
    """Bump the backoff entry for *url*; returns (attempts, backoff_days).

    Attempts restart at 1 when the failure kind changes (miss ↔ fail).  Entries written
    before the "kind" field existed are treated as "miss"; entries written before the
    "reason" field existed simply lack it.
    """
    entry = failures.get(url, {})
    attempts = entry.get("attempts", 0) + 1 if entry.get("kind", "miss") == kind else 1
    days = _backoff_days(attempts, kind)
    retry_after = (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()
    failures[url] = {"kind": kind, "attempts": attempts, "retry_after": retry_after, "reason": reason}
    return attempts, days


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
        use_official_api=True,
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
) -> SyncResult:
    """Sync a playlist from its .spotdl file.

    Downloads tracks new to the Spotify playlist, up to *track_limit* new
    downloads this session.  When *track_limit* is None all new tracks are
    downloaded.  Tracks deferred by the limit are excluded from the updated
    snapshot so they re-appear as new on the next run.

    Does NOT delete downloaded files for removed tracks — we handle that
    separately via beets source-tag removal (soft delete).

    Returns a :class:`SyncResult` (see its docstring for the per-field meaning).

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

    # Apply MISS/FAIL backoff: filter out tracks whose retry window hasn't expired yet.
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
            entry = failures[song.url]
            logger.info(
                "[BACK] %s → %s backed off until %s",
                _song_label(song),
                entry.get("kind", "miss"),
                entry["retry_after"][:10],
            )
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
    # spotdl records why each download failed in Downloader.errors and never clears the
    # list — and our Spotdl instance is a process-wide singleton — so reset it before the
    # batch and read it back afterwards.
    # The exception objects (with the chained yt-dlp cause) only pass through the progress
    # tracker, so capture them there as well (issue #159).
    downloader_errors = getattr(getattr(spotdl_obj, "downloader", None), "errors", None)
    if isinstance(downloader_errors, list):
        downloader_errors.clear()
    causes = _install_cause_capture(spotdl_obj)
    if causes is not None:
        causes.clear()
    results = spotdl_obj.download_songs(truly_new)
    reasons = _download_error_reasons(downloader_errors, causes)

    # Log per-track outcomes.
    # MISS vs FAIL: a recorded LookupError means spotdl found no YouTube source.  Anything
    # else (AudioProviderError, DownloaderError, …) — or no recorded error at all — means a
    # source was found but the download itself failed.  song.download_url can't be used as
    # the discriminator: spotdl only sets it on success (issue #151).
    n_missed = n_failed = 0
    fail_reasons: dict[str, str] = {}
    for song, path in results:
        if path is not None:
            logger.info("[OK]   %s", _song_label(song))
            failures.pop(song.url, None)
            continue
        reason = reasons.get(song.url, "")
        if reason.startswith("LookupError"):
            n_missed += 1
            attempts, days = _record_failure(failures, song.url, "miss", reason)
            logger.info("[MISS] %s → no source found; backing off %d days (attempt %d)", _song_label(song), days, attempts)
        else:
            n_failed += 1
            reason = reason or "download failed (no error reported)"
            fail_reasons[song.url] = reason
            attempts, days = _record_failure(failures, song.url, "fail", reason)
            logger.info(
                "[FAIL] %s → %s; backing off %d days (attempt %d)",
                _song_label(song),
                reason,
                days,
                attempts,
            )

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

    return SyncResult(
        removed_urls=removed_urls,
        attempted=len(truly_new),
        downloaded=len(downloaded_urls),
        missed=n_missed,
        failed=n_failed,
        fail_reasons=fail_reasons,
    )


def find_track_in_snapshot(snapshot: list[dict], url: str) -> dict | None:
    """Return the first song entry in a .spotdl snapshot that matches *url*."""
    return next((t for t in snapshot if t.get("url") == url), None)
