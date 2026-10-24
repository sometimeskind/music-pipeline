"""spotdl operations implemented via the spotdl Python library.

We use spotdl's internal classes directly instead of subprocess so that:
- The want list is computed from the playlist store before downloading.
- We control sync_without_deleting explicitly: removed tracks stay in the
  library (soft delete), they only leave the store and the playlist.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections import Counter
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import parse_qs, urlsplit

from music_fetch import spotify_limit
from music_fetch.names import SPOTDL_NAME_BYTES, truncate_utf8

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


# A source URL for a track spotdl's search can't find (#247), e.g. a glyph-only title.
PINS_FILE = Path("/root/.config/music-pipeline/track-pins.conf")
_TRACK_URL = "https://open.spotify.com/track/"


def load_pins(path: Path | None = None) -> dict[str, str]:
    """Source URLs by Spotify track URL (#247): one ``<spotify track id>  <url>``
    per line of track-pins.conf (the ID may be the track's open.spotify.com URL;
    anything after the source URL is a label).  Lines starting with ``#`` and
    blank lines are ignored; a missing file means no pins."""
    path = path or PINS_FILE
    if not path.is_file():
        return {}
    pins: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        parts = raw.split()
        if not parts or parts[0].startswith("#"):
            continue
        if len(parts) < 2 or not parts[1].startswith("https://"):
            logger.warning("[PIN] ignoring %r in %s: want <spotify track id> <https source url>", raw.strip(), path.name)
            continue
        track = parts[0].split("?")[0].rstrip("/").rsplit("/", 1)[-1]
        pins[_TRACK_URL + track] = _source_url(parts[1])
    return pins


def _source_url(url: str) -> str:
    """A YouTube share link without its tracking parameters (``si`` and the
    like): ``https://<host>/watch?v=<id>``, the form spotdl's own search
    returns.  Any other URL is kept as it is."""
    parsed = urlsplit(url)
    video = parse_qs(parsed.query).get("v")
    if parsed.netloc.endswith("youtube.com") and parsed.path == "/watch" and video:
        return f"https://{parsed.netloc}/watch?v={video[0]}"
    return url


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
    - linked: new tracks the library already had, tagged instead of downloaded (#187)
    - songs: the whole Spotify playlist in order, page records (sync_playlist only)
    - records: the richest record known per URL: a download's, the store's, a
      linked track's (sync_playlist only)
    """

    removed_urls: set[str]
    attempted: int
    downloaded: int
    missed: int
    failed: int
    fail_reasons: dict[str, str]
    linked: int = 0
    songs: list[dict] = dataclasses.field(default_factory=list)
    records: dict[str, dict] = dataclasses.field(default_factory=dict)


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


# spotdl's own cap is in characters; this is the length at which it would start
# shortening names, pushed out of the way so the byte cap below is the only one.
_NO_CHAR_CAP = 1 << 20


def _capped_file_name(song, template, file_extension, restrict=None, short=False, file_name_length=None):
    """spotdl's create_file_name with its 255-*character* cap replaced by a byte cap (#225).

    A name over ``SPOTDL_NAME_BYTES`` is cut on a character boundary and gets the
    Spotify track ID, so truncated names stay unique.  Shorter names are unchanged.
    """
    from spotdl.utils.formatter import create_file_name  # noqa: PLC0415

    del file_name_length  # spotdl's character limit: superseded by the byte cap
    path = create_file_name(song, template, file_extension, restrict=restrict, short=short,
                            file_name_length=_NO_CHAR_CAP)
    if len(path.name.encode("utf-8")) <= SPOTDL_NAME_BYTES:
        return path
    tail = f" [{song.song_id}].{file_extension}"
    stem = truncate_utf8(path.stem, SPOTDL_NAME_BYTES - len(tail.encode("utf-8"))).rstrip()
    capped = path.with_name(stem + tail)
    logger.warning("%s: file name is %d bytes, over the %d-byte cap; saving as %s",
                   _song_label(song), len(path.name.encode("utf-8")), SPOTDL_NAME_BYTES, capped.name)
    return capped


def _install_name_cap() -> None:
    """Route the downloader's output names through _capped_file_name (idempotent)."""
    from spotdl.download import downloader  # noqa: PLC0415

    if downloader.create_file_name is not _capped_file_name:
        downloader.create_file_name = _capped_file_name


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
    _install_name_cap()
    _spotdl_instance = Spotdl(
        client_id=client_id,
        client_secret=client_secret,
        use_official_api=True,
        downloader_settings=settings,
    )
    # Every Spotify reader goes through this client: fail fast on a long 429 (#195).
    from spotdl.utils.spotify import SpotifyClient  # noqa: PLC0415

    spotify_limit.install(SpotifyClient._instance)
    return _spotdl_instance


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _Batch:
    """What _download_batch did with one playlist's new tracks."""

    attempted: int
    results: list
    linked: dict[str, dict]
    missed: int
    failed: int
    fail_reasons: dict[str, str]

    def result(self, removed_urls: set[str], songs: list[dict] | None = None,
               records: dict[str, dict] | None = None) -> SyncResult:
        return SyncResult(
            removed_urls=removed_urls,
            attempted=self.attempted,
            downloaded=len({song.url for song, path in self.results if path is not None}),
            missed=self.missed,
            failed=self.failed,
            fail_reasons=self.fail_reasons,
            linked=len(self.linked),
            songs=songs or [],
            records=records or {},
        )


def want_list(
    songs: list,
    in_library: Callable[[dict], bool] | None,
    failures: dict,
    now: datetime | None = None,
    pins: Mapping[str, str] | None = None,
) -> tuple[list, list, list]:
    """A playlist's want list (#228): *songs* in order, minus the entries the
    library already holds (*in_library*; returned second) and the ones still
    backed off in *failures* (returned third).  A pinned track (*pins*, #247) is
    never backed off, and comes first.  The budget is spent on the result,
    across playlists in ``playlists.conf`` order."""
    pins = pins or {}
    linked = [] if in_library is None else [song for song in songs if in_library(song.json)]
    linked_urls = {song.url for song in linked}
    now = now or datetime.now(timezone.utc)
    wanted, backed_off = [], []
    for song in songs:
        if song.url in linked_urls:
            continue
        entry = failures.get(song.url)
        if song.url not in pins and entry and datetime.fromisoformat(entry["retry_after"]) > now:
            backed_off.append(song)
        else:
            wanted.append(song)
    wanted.sort(key=lambda song: song.url not in pins)
    return wanted, linked, backed_off


def _download_batch(
    spotdl_obj,
    truly_new: list,
    track_limit: int | None,
    failures_file: Path | None,
    in_library: Callable[[dict], bool] | None,
    removed_urls: set[str] = frozenset(),
    pins: Mapping[str, str] | None = None,
) -> _Batch:
    """Download the want list of *truly_new* in order: skip those the library has
    ([HAVE]) or that are backed off (one [BACK] count line), defer those over
    *track_limit* (the budget line), then log each outcome and record [MISS]/[FAIL]
    backoff in *failures_file*.  Nothing is logged per track unless the track is
    acted on (#320); the per-track [BACK] and [DEFER] lines are DEBUG.  A
    *track_limit* of 0 checks and defers everything.
    A pinned track (*pins*, default track-pins.conf, #247) downloads from its
    source URL instead of spotdl's search ([PIN])."""
    pins = load_pins() if pins is None else pins
    failures: dict = _load_failures(failures_file) if failures_file is not None else {}
    truly_new, linked_songs, backed_off = want_list(truly_new, in_library, failures, pins=pins)
    # Tracks the library already has under another playlist or from Usenet (#187).
    linked: dict[str, dict] = {song.url: song.json for song in linked_songs}
    for song in linked_songs:
        logger.info("[HAVE] %s → already in the library", _song_label(song))
        if song.url in pins:
            logger.info("[PIN] %s → already in the library; its pin is unused", _song_label(song))
    for url in removed_urls | linked.keys():
        failures.pop(url, None)
    if backed_off:
        kinds = Counter(failures[song.url].get("kind", "miss") for song in backed_off)
        logger.info(
            "[BACK] %d track(s) backed off (%s), next retry %s",
            len(backed_off),
            ", ".join(f"{kind} {n}" for kind, n in sorted(kinds.items())),
            min(failures[song.url]["retry_after"][:10] for song in backed_off),
        )
    for song in backed_off:
        entry = failures[song.url]
        logger.debug(
            "[BACK] %s → %s backed off until %s",
            _song_label(song),
            entry.get("kind", "miss"),
            entry["retry_after"][:10],
        )

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
            logger.debug("[DEFER] %s", _song_label(song))

    # spotdl downloads from a song's download_url when it is set, skipping its search.
    for song in truly_new:
        if source := pins.get(song.url):
            song.download_url = source
            logger.info("[PIN] %s → downloading from %s", _song_label(song), source)

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
    # Nothing to download (a spent budget still checks the playlist, #228): no spotdl call.
    results = spotdl_obj.download_songs(truly_new) if truly_new else []
    # spotdl turns a per-track Spotify error into a failed download; a rate limit
    # must fail the run instead of backing those tracks off as [FAIL] (#195).
    spotify_limit.check()
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

    return _Batch(len(truly_new), list(results), linked, n_missed, n_failed, fail_reasons)


def download_fallback(
    songs: list[dict],
    output_dir: Path,
    cookie_file: Path,
    track_limit: int | None = None,
    failures_file: Path | None = None,
    in_library: Callable[[dict], bool] | None = None,
) -> SyncResult:
    """Download an album playlist's fallback tracks (#205): the store's *songs*
    album mode could not get from Usenet, in playlist order.

    The same [HAVE] check, backoff and budget as :func:`sync_playlist`, but the
    playlist's store is left alone: the album tick owns it.  Downloads land in
    *output_dir*, the playlist's inbox, so the scan tags them with the playlist.
    """
    from spotdl.types.song import Song  # noqa: PLC0415

    spotdl_obj = _make_spotdl(_make_downloader_settings(cookie_file=cookie_file, output_dir=output_dir))
    batch = _download_batch(spotdl_obj, [Song.from_dict(s) for s in songs], track_limit, failures_file, in_library)
    return batch.result(set())


def fetch_songs(url: str, cookie_file: Path) -> list[dict]:
    """The Spotify playlist at *url*, in order, as page records, read the way
    :func:`sync_playlist` reads it.  Downloads nothing."""
    from spotdl.utils.search import get_simple_songs  # noqa: PLC0415

    _make_spotdl(_make_downloader_settings(cookie_file=cookie_file))
    return [song.json for song in get_simple_songs([url])]


def sync_playlist(
    url: str,
    name: str,
    previous: list[dict],
    wants: Callable[[dict], bool],
    output_dir: Path,
    cookie_file: Path,
    track_limit: int | None = None,
    failures_file: Path | None = None,
    in_library: Callable[[dict], bool] | None = None,
) -> SyncResult:
    """Sync playlist *name* from its Spotify playlist at *url* (homelab#1914).

    *previous* is the song list of the playlist's store before this sync.
    Each track on the playlist that the store *wants* (no library item holds
    it by Spotify ID or ISRC, and no download of it waits in the inbox or
    quarantine) goes on the want list, judged by its *previous* record when
    there is one: the page record may lack the ISRC an item matches by.  One
    summary line per playlist counts the tracks on Spotify, those held or waiting
    (not wanted), those wanted (split into tracks on the playlist before and new
    ones) and those removed; the per-track ``[SKIP]`` and ``[STORE-WANT]`` lines
    are DEBUG (#320).

    The want list then goes through *in_library* ([HAVE]: the library holds it
    by the full identity ladder, and the callback tagged it), the backoff and
    up to *track_limit* downloads this session; when *track_limit* is None
    all are downloaded.  Removed tracks are never deleted: they are gone from
    the next store, and so from the playlist (soft delete).

    Returns a :class:`SyncResult` whose *songs* and *records* the caller
    writes to the store.

    The playlist is read from its item pages only (get_simple_songs, one call per
    100 tracks).  Spotdl.search() would also re-fetch every track (track, artist and
    album calls) on every run (#183).  spotdl's downloader still re-fetches each song
    it downloads, since the page data lacks genres, so only new tracks cost per-track
    calls.  Known tracks keep their record from when they were downloaded.

    Note on ordering: when *track_limit* is set, the batch is taken from the front of
    the playlist, in playlist order (oldest-added first for Liked Songs).  This means
    the same leading batch is retried each session until fully downloaded, then the
    next batch follows.
    """
    from spotdl.utils.search import get_simple_songs  # noqa: PLC0415

    known: dict[str, dict] = {s["url"]: s for s in previous if s.get("url")}

    spotdl_obj = _make_spotdl(
        _make_downloader_settings(
            cookie_file=cookie_file,
            output_dir=output_dir,
            sync_without_deleting=True,
        )
    )

    logger.info("Fetching current Spotify state for %s", name)
    new_songs = get_simple_songs([url])
    new_urls: set[str] = {s.url for s in new_songs}

    removed_urls = set(known) - new_urls

    wanted = []
    seen: set[str] = set()
    n_skipped = n_known = 0
    for song in new_songs:
        if song.url in seen:
            continue
        seen.add(song.url)
        if not wants(known.get(song.url) or song.json):
            n_skipped += 1
            logger.debug("[SKIP] %s", _song_label(song))
            continue
        if song.url in known:
            n_known += 1
            logger.debug("[STORE-WANT] %s → on the playlist, but no library item holds it", _song_label(song))
        wanted.append(song)
    logger.info(
        "%s: %d track(s) on Spotify, %d held or waiting, %d wanted (%d on the playlist before, %d new), %d removed",
        name, len(seen), n_skipped, len(wanted), n_known, len(wanted) - n_known, len(removed_urls),
    )

    batch = _download_batch(spotdl_obj, wanted, track_limit, failures_file, in_library, removed_urls)

    # Downloaded songs carry the full metadata spotdl fetched for them; known tracks keep
    # their earlier record; linked tracks keep the page data.
    downloaded = {song.url: song.json for song, path in batch.results if path is not None}
    records = {**batch.linked, **known, **downloaded}
    return batch.result(removed_urls, [s.json for s in new_songs], records)


def download_song(song: dict, output_dir: Path, cookie_file: Path) -> Path | None:
    """Download one track from its song record (a store entry) into *output_dir*.

    The entry already holds the metadata, so this makes no Spotify calls.
    Returns the file's path, or None when the download failed.
    """
    from spotdl.types.song import Song  # noqa: PLC0415

    spotdl_obj = _make_spotdl(_make_downloader_settings(cookie_file=cookie_file, output_dir=output_dir))
    [(_, path)] = spotdl_obj.download_songs([Song.from_dict(song)])
    return path


class SpotifyPlaylists:
    """Playlist reads for album mode, through the same spotdl client as sync."""

    def __init__(self, cookie_file: Path) -> None:
        self._spotdl = _make_spotdl(_make_downloader_settings(cookie_file=cookie_file))

    def snapshot_id(self, url: str) -> str:
        """The playlist's version; Spotify changes it on every edit.  One small call."""
        from spotdl.utils.spotify import SpotifyClient  # noqa: PLC0415

        return SpotifyClient().playlist(url, fields="snapshot_id")["snapshot_id"]

    def songs(self, url: str) -> list[dict]:
        """Every track on the playlist, in spotdl's song format.

        Built from the playlist item pages only, one call per 100 tracks.
        Spotdl.search() would also re-fetch every track (track, artist and album
        calls), which got the app a 17h rate limit on a playlist of thousands (#178).
        """
        from spotdl.utils.search import get_simple_songs  # noqa: PLC0415

        return [s.json for s in get_simple_songs([url])]

    def album_songs(self, album_id: str) -> list[dict]:
        """The album's whole tracklist, in spotdl's song format (#268).

        Two calls (the album and its track pages), no per-track lookups; the
        songs carry no ISRC, which spotdl fills in when it downloads them.
        """
        from spotdl.utils.search import get_simple_songs  # noqa: PLC0415

        return [s.json for s in get_simple_songs([f"https://open.spotify.com/album/{album_id}"])]
