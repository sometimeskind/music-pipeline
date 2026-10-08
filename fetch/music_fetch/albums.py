"""Album mode: download album-only playlists as whole albums from Usenet (#168).

A playlist flagged ``album`` in playlists.conf is never synced by spotdl.  Instead
:func:`tick` runs every ``ALBUM_POLL_SECONDS``:

1. Poll each album playlist's Spotify ``snapshot_id``.  Only a changed snapshot
   re-reads the track list, which is written to the playlist's ``.spotdl`` file
   (so .m3u ordering and removals work as for spotdl playlists) and reduced to
   distinct albums.
2. Top the Usenet queue up to ``ALBUM_MAX_IN_FLIGHT``: for each wanted album not
   already complete in the library, search Prowlarr, rank the releases and send
   the best to SABnzbd.
3. Hand what Usenet can't supply to spotdl (#205): a missing or failed album's
   absent tracks, and the few a partial import left out, become ``fallback``
   tracks the nightly sync downloads into the playlist's inbox.  Once the
   library holds them all the album is ``filled``.

``ALBUM_MODE`` is ``off`` (default), ``dry-run`` (search and log the pick, never
grab) or ``on``.  Indexer use is capped by a rolling-24h budget counted from our
own timestamps, because the indexer's reset window is unknown.

State lives in one JSON file next to the .spotdl files (on the persistent
pipeline-state volume): losing it would forget blocklists and the grab budget.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol

from music_fetch.metrics import _gauge, _push
from music_fetch.usenet import Release, clean_album, normalise, rank, readable

logger = logging.getLogger(__name__)

STATE_FILE = Path("/root/Music/inbox/spotdl/.albums.json")
# Search text for albums whose names no normalisation reaches (#197); mounted
# from the homelab repo next to playlists.conf.
OVERRIDES_FILE = Path("/root/.config/music-pipeline/album-overrides.conf")

WANTED = "wanted"      # waiting for a search slot
DRY_RUN = "dry-run"    # searched in dry-run; the pick is in `candidate`
GRABBED = "grabbed"    # sent to SABnzbd; waits for /trigger-album-import
MISSING = "missing"    # no matching release; searched again after MISSING_RETRY
HAVE = "have"          # every playlist track is already in the library
IMPORTED = "imported"  # downloaded and imported by album mode
FAILED = "failed"      # MAX_ATTEMPTS releases failed; becomes FALLBACK in `on`
FALLBACK = "fallback"  # the absent tracks are left to spotdl (`fallback_tracks`)
FILLED = "filled"      # complete, with tracks from spotdl

MISSING_RETRY = timedelta(days=7)
# Releases tried per album before giving up, so an album beets can't match
# doesn't burn the grab budget.
MAX_ATTEMPTS = 3
# A trigger lost for longer than this is recovered from SABnzbd history.
LOST_TRIGGER_AFTER = timedelta(hours=1)

# SABnzbd's complete dir (/downloads/complete in its pod) and the inbox root
# album imports are moved to; both on the music-data volume, so the move is a
# rename.  beets' music_pipeline plugin reads the playlist from the inbox path.
USENET_COMPLETE = Path("/root/Music/usenet/complete")
USENET_INBOX = Path("/root/Music/inbox/usenet")
USENET_QUARANTINE = Path("/root/Music/quarantine/usenet")
WINDOW = timedelta(hours=24)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        logger.error("%s must be an integer — using %d", name, default)
        return default


@dataclasses.dataclass
class Settings:
    mode: str = "off"
    max_in_flight: int = 3
    grabs_per_day: int = 18
    hits_per_day: int = 90
    # dry-run searches cost API hits too, and `on` searches every album again,
    # so dry-run only samples: a few per tick, dry_run_limit in total.
    dry_run_per_tick: int = 3
    dry_run_limit: int = 25
    # A release that imports all but this many tracks (or this share of the
    # album's playlist tracks, if more) isn't re-grabbed: spotdl gets the rest.
    partial_max_missing: int = 2
    partial_max_percent: int = 20

    @classmethod
    def from_env(cls) -> "Settings":
        mode = os.environ.get("ALBUM_MODE", "off").strip().lower()
        if mode not in ("off", "dry-run", "on"):
            logger.error("ALBUM_MODE must be off, dry-run or on, got %r — using off", mode)
            mode = "off"
        return cls(
            mode=mode,
            max_in_flight=_env_int("ALBUM_MAX_IN_FLIGHT", 3),
            grabs_per_day=_env_int("ALBUM_GRABS_PER_DAY", 18),
            hits_per_day=_env_int("ALBUM_HITS_PER_DAY", 90),
            dry_run_limit=_env_int("ALBUM_DRY_RUN_LIMIT", 25),
            partial_max_missing=_env_int("ALBUM_PARTIAL_MAX_MISSING", 2),
            partial_max_percent=_env_int("ALBUM_PARTIAL_MAX_PERCENT", 20),
        )

    def partial(self, missing: int, tracks: int) -> bool:
        """True when an import missing *missing* of *tracks* is close enough to
        leave the rest to spotdl.  At least one track must have imported."""
        return 0 < missing < tracks and missing <= max(self.partial_max_missing, tracks * self.partial_max_percent // 100)


# ---------------------------------------------------------------------------
# Playlist → albums
# ---------------------------------------------------------------------------


def album_key(song: dict) -> str:
    """Spotify album id, or artist/name when a song has none (local files)."""
    return song.get("album_id") or f"{song.get('album_artist', '')}/{song.get('album_name', '')}".lower()


def reduce_to_albums(songs: list[dict]) -> dict[str, dict]:
    """Distinct albums in playlist order, with the playlist's tracks on each."""
    albums: dict[str, dict] = {}
    for song in songs:
        key = album_key(song)
        album = albums.setdefault(key, {
            "name": song.get("album_name", ""),
            "artist": song.get("album_artist") or (song.get("artists") or [""])[0],
            "year": song.get("year"),
            "tracks_count": song.get("tracks_count") or 0,
            "album_type": song.get("album_type"),
            "duration": 0,
            "tracks": [],
        })
        album["duration"] += song.get("duration") or 0
        # [name, artist, song_id, isrc, disc, track]: the IDs let the completion
        # tag the imported items with the entries they satisfy (#176).
        album["tracks"].append([
            song.get("name", ""), (song.get("artists") or [""])[0],
            song.get("song_id"), song.get("isrc"), song.get("disc_number"), song.get("track_number"),
        ])
    # The playlist may hold part of the album: scale its tracks' duration up to
    # the whole album, which the matcher's size bound uses (#189).
    for album in albums.values():
        if album["tracks_count"] > len(album["tracks"]):
            album["duration"] = album["duration"] * album["tracks_count"] // len(album["tracks"])
    return albums


def load_overrides(path: Path | None = None) -> dict[str, str]:
    """Search text by Spotify album ID, for names no normalisation can reach.

    Format: one ``<album id>  <search words…>`` per line; ``#`` comments and
    blank lines are ignored.  A missing file means no overrides.
    """
    path = path or OVERRIDES_FILE
    if not path.exists():
        return {}
    overrides: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        parts = raw.split("#")[0].split(maxsplit=1)
        if len(parts) == 2 and normalise(parts[1]):
            overrides[parts[0]] = normalise(parts[1])
    return overrides


@dataclasses.dataclass
class Plan:
    """How to search for an album: the queries in order (the second, the artist
    alone, only when the first finds nothing) and what rank() matches on."""
    queries: list[str]
    artist: str = ""
    album: str = ""
    any_album: bool = False
    note: str = ""


def plan(key: str, record: dict, overrides: dict[str, str]) -> Plan:
    """The searches for an album (#197).  An override wins; a title with no
    readable words searches the artist alone and matches any of their releases
    on size; with neither, there is nothing to search (``queries`` empty)."""
    if key in overrides:
        return Plan([overrides[key]], album=overrides[key], note=" (override)")
    artist = record["artist"] if readable(record["artist"]) else ""
    name = clean_album(record["name"])
    if readable(name):
        first = normalise(f"{artist} {name}")
        # Retrying a compilation as "various artists" would only list compilations.
        retry = artist and artist.lower() != "various artists"
        return Plan([first, normalise(artist)] if retry else [first], artist=artist, album=record["name"])
    if artist:
        return Plan([normalise(artist)], artist=artist, album=record["name"], any_album=True, note=" (artist-only)")
    return Plan([])


# ---------------------------------------------------------------------------
# State and budget
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


@dataclasses.dataclass
class State:
    playlists: dict[str, dict] = dataclasses.field(default_factory=dict)
    albums: dict[str, dict] = dataclasses.field(default_factory=dict)
    # [{"t": iso, "kind": "hit" | "grab"}], pruned to the last WINDOW.
    indexer: list[dict] = dataclasses.field(default_factory=list)

    @classmethod
    def load(cls, path: Path = STATE_FILE) -> "State":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            playlists=data.get("playlists", {}),
            albums=data.get("albums", {}),
            indexer=data.get("indexer", []),
        )

    def save(self, path: Path = STATE_FILE) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dataclasses.asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def used(self, kind: str, now: datetime) -> int:
        cutoff = now - WINDOW
        return sum(1 for e in self.indexer if e["kind"] == kind and datetime.fromisoformat(e["t"]) > cutoff)

    def record(self, kind: str, now: datetime) -> None:
        cutoff = now - WINDOW
        self.indexer = [e for e in self.indexer if datetime.fromisoformat(e["t"]) > cutoff]
        self.indexer.append({"t": _iso(now), "kind": kind})

    def in_flight(self) -> int:
        return sum(1 for a in self.albums.values() if a.get("status") == GRABBED)


# ---------------------------------------------------------------------------
# Tick
# ---------------------------------------------------------------------------


class SpotifySource(Protocol):
    def snapshot_id(self, url: str) -> str: ...
    def songs(self, url: str) -> list[dict]: ...


@dataclasses.dataclass
class TickResult:
    refreshed: list[str] = dataclasses.field(default_factory=list)
    searched: int = 0
    grabbed: int = 0
    removed_songs: dict[str, list[dict]] = dataclasses.field(default_factory=dict)


def refresh_playlists(
    state: State,
    playlists: list[tuple[str, str]],
    spotify: SpotifySource,
    spotdl_dir: Path,
    result: TickResult,
) -> None:
    """Re-read every album playlist whose Spotify snapshot changed."""
    for name, url in playlists:
        snapshot = spotify.snapshot_id(url)
        entry = state.playlists.setdefault(name, {})
        if entry.get("snapshot_id") == snapshot:
            continue
        logger.info("==> Album playlist changed: %s — re-reading tracks", name)
        songs = spotify.songs(url)

        spotdl_file = spotdl_dir / f"{name}.spotdl"
        old_songs: list[dict] = []
        if spotdl_file.exists():
            old_songs = json.loads(spotdl_file.read_text(encoding="utf-8")).get("songs", [])
        new_urls = {s.get("url") for s in songs}
        removed = [s for s in old_songs if s.get("url") not in new_urls]
        if removed:
            result.removed_songs[name] = removed
        spotdl_file.write_text(
            json.dumps({"type": "sync", "query": [url], "songs": songs}, indent=4, ensure_ascii=False),
            encoding="utf-8",
        )

        wanted = reduce_to_albums(songs)
        for key, album in wanted.items():
            record = state.albums.setdefault(key, {"status": WANTED, "blocklist": []})
            record.update({k: v for k, v in album.items() if k != "tracks"})
            record.setdefault("playlists", {})[name] = album["tracks"]
        # Albums this playlist no longer holds.
        for key, record in list(state.albums.items()):
            if key not in wanted and name in record.get("playlists", {}):
                del record["playlists"][name]
                if not record["playlists"] and record.get("status") in (WANTED, DRY_RUN, MISSING, HAVE, IMPORTED, FAILED, FALLBACK, FILLED):
                    del state.albums[key]
        entry["snapshot_id"] = snapshot
        result.refreshed.append(name)
        logger.info("    %d track(s) on %d album(s)", len(songs), len(wanted))


def _requery(record: dict, search: Plan) -> bool:
    """A missing album whose query has changed since its search (a fix to the
    normalisation, or a new override) is searched again at once."""
    return record.get("status") == MISSING and record.get("query") != (search.queries[0] if search.queries else "")


def _due(record: dict, now: datetime, mode: str, search: Plan) -> bool:
    status = record.get("status")
    if status == WANTED:
        return True
    if status == DRY_RUN:
        return mode == "on"
    if status == MISSING:
        return _requery(record, search) or now - datetime.fromisoformat(record["searched_at"]) >= MISSING_RETRY
    return False


def top_up(
    state: State,
    settings: Settings,
    prowlarr,
    sabnzbd,
    have: Callable[[str, list[list[str]]], bool],
    result: TickResult,
    now: Callable[[], datetime] = _now,
    overrides: dict[str, str] | None = None,
) -> None:
    """Search and grab wanted albums until the queue or a budget is full.

    *overrides* default to ``album-overrides.conf``, so every caller searches
    the same way (#202)."""
    overrides = load_overrides() if overrides is None else overrides
    if settings.mode == "on":
        slots = settings.max_in_flight - state.in_flight()
    else:
        sampled = sum(1 for a in state.albums.values() if a.get("dry_run"))
        slots = min(settings.dry_run_per_tick, settings.dry_run_limit - sampled)
        if slots <= 0:
            logger.info("Dry-run sample complete (%d album(s), ALBUM_DRY_RUN_LIMIT) — not searching", sampled)
    for key, record in list(state.albums.items()):
        label = f"{record['artist']} — {record['name']}"
        search = plan(key, record, overrides)
        if not _due(record, now(), settings.mode, search):
            continue
        if not search.queries:
            # Costs nothing, so it is logged even when no slot is free.
            record.update(status=MISSING, searched_at=_iso(now()), query="")
            logger.warning("[NOWORDS] %s: %s: add a search override", key, label)
            continue
        # In dry-run, re-checking a sampled miss after its query changed takes no sample slot.
        recheck = settings.mode != "on" and record.get("dry_run") and _requery(record, search)
        if slots <= 0 and not recheck:
            continue
        if all(have(pl, tracks) for pl, tracks in record.get("playlists", {}).items()):
            record["status"] = HAVE
            continue
        if state.used("hit", now()) >= settings.hits_per_day:
            logger.info("Indexer API budget spent (%d/24h) — stopping", settings.hits_per_day)
            break
        if settings.mode == "on" and state.used("grab", now()) >= settings.grabs_per_day:
            logger.info("Grab budget spent (%d/24h) — stopping", settings.grabs_per_day)
            break

        sent: list[str] = []
        releases: list[Release] = []
        try:
            for query in search.queries:
                if sent and (releases or state.used("hit", now()) >= settings.hits_per_day):
                    break
                state.record("hit", now())
                result.searched += 1
                sent.append(query)
                releases = prowlarr.search(query)
        except Exception as exc:
            logger.warning("[ERR]  %s: search failed: %s", label, exc)
            break  # Prowlarr or the indexer is down; the next tick retries.
        if not releases and len(sent) < len(search.queries):
            logger.info("Indexer API budget spent (%d/24h) before the artist-only retry for %s — stopping",
                        settings.hits_per_day, label)
            break  # The album keeps its status; the next window searches it again.
        tracks = record.get("tracks_count") or max(len(t) for t in record["playlists"].values())
        ranked = rank(releases, search.artist, search.album, tracks, set(record["blocklist"]), record.get("year"),
                      album_type=record.get("album_type"), seconds=record.get("duration") or 0,
                      any_album=search.any_album)
        record["searched_at"] = _iso(now())
        record["query"] = search.queries[0]
        if settings.mode != "on":
            record["dry_run"] = True
        if not ranked:
            record["status"] = MISSING
            logger.info("[MISS] %s%s: %d result(s), none match; queries: %s",
                        label, search.note, len(releases), ", ".join(f'"{q}"' for q in sent))
            continue

        best: Release = ranked[0]
        record["candidate"] = best.as_dict()
        if settings.mode != "on":
            record["status"] = DRY_RUN
            logger.info("[PICK] %s%s → %s (%.0f MB, %d grabs, %d other match(es))",
                        label, search.note, best.title, best.size / 1e6, best.grabs, len(ranked) - 1)
            if not recheck:
                slots -= 1
            continue

        state.record("grab", now())
        try:
            location = prowlarr.nzb_location(best)
        except Exception as exc:
            # Release-specific (gone from the indexer, or refused): try the next one later.
            logger.warning("[ERR]  %s: grab of %s failed, blocklisting: %s", label, best.title, exc)
            record["blocklist"].append(best.guid)
            record["status"] = WANTED
            continue
        try:
            record["nzo_id"] = sabnzbd.add_url(location, best.title)
        except Exception as exc:
            # SABnzbd is down: not the release's fault.  The next tick retries.
            logger.warning("[ERR]  %s: SABnzbd refused %s: %s", label, best.title, exc)
            record["status"] = WANTED
            break
        record["status"] = GRABBED
        record["grabbed_at"] = _iso(now())
        result.grabbed += 1
        slots -= 1
        logger.info("[GRAB] %s%s → %s (%.0f MB)", label, search.note, best.title, best.size / 1e6)


# ---------------------------------------------------------------------------
# Fallback: what Usenet can't supply goes to spotdl (#205)
# ---------------------------------------------------------------------------

# missing(playlist, tracks): the entries of *tracks* the library lacks.
Missing = Callable[[str, list[list]], list[list]]


def _fallback_tracks(record: dict, missing: Missing) -> dict[str, list[str]]:
    """Per playlist, the Spotify IDs of the album's tracks the library lacks.  A
    track on several playlists is fetched once, into the first; the fill check
    tags it for the others.  Tracks without an ID (local files) can't be fetched."""
    seen: set[str] = set()
    queued: dict[str, list[str]] = {}
    for playlist, tracks in record.get("playlists", {}).items():
        ids = [t[2] for t in missing(playlist, tracks) if t[2] and t[2] not in seen]
        seen.update(ids)
        if ids:
            queued[playlist] = ids
    return queued


def _queue_fallback(record: dict, missing: Missing, source: str) -> None:
    """Leave the album's absent tracks to spotdl; *source* is why (the status it
    leaves, or ``partial``)."""
    record.update(status=FALLBACK, fallback_from=source, fallback_at=_iso(_now()),
                  fallback_tracks=_fallback_tracks(record, missing))
    n = sum(len(ids) for ids in record["fallback_tracks"].values())
    logger.info("[FALLBACK] %s — %s: %d track(s) left to spotdl (%s)", record["artist"], record["name"], n, source)


def fallbacks(state: State, have: Callable[[str, list[list]], bool], missing: Missing) -> None:
    """Hand missing and failed albums to spotdl, and mark fallback albums the
    library now completes as filled (``on`` only).  An album in fallback is no
    longer searched."""
    for record in state.albums.values():
        status = record.get("status")
        if status in (MISSING, FAILED):
            _queue_fallback(record, missing, status)
        elif status != FALLBACK:
            continue
        if all(have(pl, tracks) for pl, tracks in record.get("playlists", {}).items()):
            record.update(status=FILLED, filled_at=_iso(_now()), fallback_tracks={})
            logger.info("[FILLED] %s — %s", record["artist"], record["name"])
        else:
            record["fallback_tracks"] = _fallback_tracks(record, missing)


def fallback_queue(state: State) -> int:
    """Tracks waiting for spotdl, over every fallback album."""
    return sum(
        len(ids) for r in state.albums.values() if r.get("status") == FALLBACK
        for ids in r.get("fallback_tracks", {}).values()
    )


def fallback_songs(state: State, playlist: str, songs: list[dict]) -> list[dict]:
    """The .spotdl *songs* of *playlist* left to spotdl, in playlist order."""
    ids = {
        song_id for r in state.albums.values() if r.get("status") == FALLBACK
        for song_id in r.get("fallback_tracks", {}).get(playlist, [])
    }
    return [s for s in songs if s.get("song_id") in ids]


def status_counts(state: State) -> dict[str, int]:
    counts = {s: 0 for s in (WANTED, DRY_RUN, GRABBED, MISSING, HAVE, IMPORTED, FAILED, FALLBACK, FILLED)}
    for record in state.albums.values():
        status = record.get("status", WANTED)
        counts[status] = counts.get(status, 0) + 1
    return counts


def push_metrics(state: State, success: bool) -> None:
    # One TYPE line per name: the Pushgateway rejects a repeated one, so the
    # labelled series are written by hand rather than through _gauge.
    now = _now()
    lines = ["# TYPE music_albums gauge"]
    lines += [f'music_albums{{status="{s}"}} {n}' for s, n in status_counts(state).items()]
    lines += [
        _gauge("music_albums_fallback_tracks", fallback_queue(state)),
        "# TYPE music_albums_indexer_used_24h gauge",
        f'music_albums_indexer_used_24h{{kind="hit"}} {state.used("hit", now)}',
        f'music_albums_indexer_used_24h{{kind="grab"}} {state.used("grab", now)}',
        _gauge("music_albums_last_tick_success", int(success)),
        _gauge("music_albums_last_tick_timestamp_seconds", int(now.timestamp())),
    ]
    _push("\n".join(lines), "music_albums")


def tick(
    playlists: list[tuple[str, str]],
    spotify: SpotifySource,
    prowlarr,
    sabnzbd,
    have: Callable[[str, list[list[str]]], bool],
    spotdl_dir: Path,
    settings: Settings | None = None,
    state_file: Path = STATE_FILE,
    on_completion: Callable[[State, "Completion"], object] | None = None,
    overrides_file: Path | None = None,
    missing: Missing | None = None,
) -> TickResult:
    """One album-mode pass: refresh changed playlists, recover lost import
    triggers (*on_completion*, ``on`` only), top the queue up, then hand what
    Usenet can't supply to spotdl (*missing*, ``on`` only)."""
    settings = settings or Settings.from_env()
    result = TickResult()
    if settings.mode == "off":
        return result
    state = State.load(state_file)
    start = time.monotonic()
    success = False
    try:
        refresh_playlists(state, playlists, spotify, spotdl_dir, result)
        if settings.mode == "on" and on_completion is not None:
            for completion in lost_completions(state, sabnzbd):
                logger.info("Recovering lost import trigger for %s", completion.nzo_id)
                on_completion(state, completion)
        top_up(state, settings, prowlarr, sabnzbd, have, result, overrides=load_overrides(overrides_file))
        if settings.mode == "on" and missing is not None:
            fallbacks(state, have, missing)
        success = True
    finally:
        state.save(state_file)
        push_metrics(state, success)
    counts = status_counts(state)
    logger.info(
        "Album tick (%s) in %.0fs: %d searched, %d grabbed; albums %s; indexer 24h: %d hit(s), %d grab(s)",
        settings.mode, time.monotonic() - start, result.searched, result.grabbed,
        ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none",
        state.used("hit", _now()), state.used("grab", _now()),
    )
    return result


# ---------------------------------------------------------------------------
# Completion: import a finished download, or blocklist the release
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Completion:
    nzo_id: str
    ok: bool
    # The job's dir relative to USENET_COMPLETE (album-import.py sends it so).
    path: str = ""
    fail_message: str = ""


def find_by_nzo(state: State, nzo_id: str) -> tuple[str, dict] | None:
    for key, record in state.albums.items():
        if record.get("status") == GRABBED and record.get("nzo_id") == nzo_id:
            return key, record
    return None


def _inside(root: Path, rel: str) -> Path | None:
    """*root*/*rel*, or None when *rel* is empty or escapes *root*."""
    if not rel or rel == ".":
        return None
    target = (root / rel).resolve()
    if root.resolve() not in target.parents:
        return None
    return target


def complete(
    state: State,
    completion: Completion,
    import_inbox: Callable[[], None],
    missing: Missing,
    add_source: Callable[[str, str, list[list[str]]], None],
    tag_ids: Callable[[str, list[list], float, int], None],
    rollback: Callable[[str, float], int] | None = None,
    complete_root: Path = USENET_COMPLETE,
    inbox_root: Path = USENET_INBOX,
    quarantine_root: Path = USENET_QUARANTINE,
    settings: Settings | None = None,
) -> str | None:
    """Handle one finished SABnzbd job.  Returns the album's new status, or None
    when the job isn't one of ours (a manual SABnzbd add).

    On success the job dir moves to ``inbox/usenet/<playlist>/`` and
    *import_inbox* runs the beets import.  The album counts as imported only if
    every playlist track is then in the library (*missing* finds none): beets
    quarantining a track, or the download failing, blocklists the release.  The
    next tick, or the caller's top-up, tries the next one.  Tracks the library
    already had are skipped as duplicates by beets, so a second release only
    fills the gaps.  An import only a few tracks short (``Settings.partial``)
    isn't re-grabbed: those tracks go to spotdl, as do a failed album's (#205).

    Usenet files carry no Spotify IDs, so after the import *tag_ids* maps each
    playlist's tracks onto the library items and records their Spotify IDs
    (#176), also when the album didn't import completely.

    A blocklisted release is rolled back (#238): *rollback(playlist, since)*
    quarantines the items it imported that match no playlist entry (an
    expanded edition's outtakes); its matched items stay.  A release missing
    the same tracks as the previous one is a structural gap: they go to spotdl
    instead of another grab.
    """
    found = find_by_nzo(state, completion.nzo_id)
    if found is None:
        logger.info("Album import: %s is not an album-mode job — ignoring", completion.nzo_id)
        return None
    _, record = found
    label = f"{record['artist']} — {record['name']}"
    playlists = list(record.get("playlists", {}))
    source = _inside(complete_root, completion.path)
    job_name = source.name if source else ""

    settings = settings or Settings.from_env()
    reason = completion.fail_message or "download failed"
    imported = partial = False
    started: float | None = None
    gap_keys: list[str] | None = None
    if completion.ok and source is not None and source.is_dir() and playlists:
        first = playlists[0]
        dest = inbox_root / first / job_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(dest, ignore_errors=True)
        shutil.move(str(source), dest)
        logger.info("[IMPT] %s: importing %s", label, job_name)
        started = time.time()
        try:
            import_inbox()
        finally:
            # Leftovers beets doesn't import (nfo, sfv, cue, cover scans).
            shutil.rmtree(dest, ignore_errors=True)
        tracks = record["playlists"][first]
        gaps = missing(first, tracks)
        imported = not gaps
        partial = settings.partial(len(gaps), len(tracks))
        if imported:
            # The inbox path names one playlist; tag the album's tracks for the others.
            for other in playlists[1:]:
                add_source(first, other, record["playlists"][other])
        else:
            reason = f"beets did not import {len(gaps)} of {len(tracks)} track(s) (quarantined or unmatched)"
            # Records from before #176 hold only [name, artist].
            gap_keys = sorted({(t[2] if len(t) > 2 else None) or f"{t[0]} — {t[1]}" for t in gaps})
        for playlist in playlists:
            tag_ids(playlist, record["playlists"][playlist], started, record.get("tracks_count") or 0)
    elif completion.ok:
        reason = f"completed job dir not found: {completion.path!r}"

    if imported:
        record["status"] = IMPORTED
        record["imported_at"] = _iso(_now())
        logger.info("[DONE] %s", label)
        return IMPORTED

    if partial:
        logger.info("[PART] %s: %s — not re-grabbing", label, reason)
        if job_name:
            for playlist in playlists:
                shutil.rmtree(quarantine_root / playlist / job_name, ignore_errors=True)
        record["imported_at"] = _iso(_now())
        _queue_fallback(record, missing, "partial")
        return FALLBACK

    guid = (record.get("candidate") or {}).get("guid")
    if guid and guid not in record["blocklist"]:
        record["blocklist"].append(guid)
    record["attempts"] = record.get("attempts", 0) + 1
    if source is not None:
        shutil.rmtree(source, ignore_errors=True)
    if started is not None and rollback is not None:
        n = rollback(playlists[0], started)
        if n:
            logger.info("[ROLLBACK] %s: %d unmatched track(s) from %s → quarantine/replaced/", label, n, job_name)
    same_gap = gap_keys is not None and gap_keys == record.get("gaps")
    if gap_keys is not None:
        record["gaps"] = gap_keys
    if same_gap:
        # Like a failed album, the last release's quarantined tracks stay for review.
        logger.warning("[GAP] %s: %s, the same as the previous release — not re-grabbing", label, reason)
        _queue_fallback(record, missing, "gap")
        return FALLBACK
    record["status"] = FAILED if record["attempts"] >= MAX_ATTEMPTS else WANTED
    if record["status"] == FAILED:
        # Keep the last release's quarantined tracks for manual review.
        logger.warning("[FAIL] %s: %s — gave up after %d release(s)", label, reason, record["attempts"])
        _queue_fallback(record, missing, FAILED)
    else:
        if job_name:
            for playlist in playlists:
                shutil.rmtree(quarantine_root / playlist / job_name, ignore_errors=True)
        logger.warning("[FAIL] %s: %s — blocklisted, will try the next release", label, reason)
    return record["status"]


def lost_completions(state: State, sabnzbd, now: datetime | None = None) -> list[Completion]:
    """Finished jobs whose trigger never arrived, from SABnzbd history."""
    now = now or _now()
    stale = [
        r["nzo_id"] for r in state.albums.values()
        if r.get("status") == GRABBED and r.get("nzo_id")
        and now - datetime.fromisoformat(r["grabbed_at"]) >= LOST_TRIGGER_AFTER
    ]
    found = sabnzbd.finished(stale)
    return [
        Completion(
            nzo_id=nzo_id,
            ok=job["ok"],
            path=str(Path(job["storage"]).relative_to("/downloads/complete")) if job["storage"].startswith("/downloads/complete/") else "",
            fail_message=job["fail_message"],
        )
        for nzo_id, job in found.items()
    ]
