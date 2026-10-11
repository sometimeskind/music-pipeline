"""Album mode: download album-only playlists as whole albums from Usenet (#168).

A playlist flagged ``album`` in playlists.conf is never synced by spotdl.  Instead
:func:`tick` runs every ``ALBUM_POLL_SECONDS``:

1. Poll each album playlist's Spotify ``snapshot_id``.  Only a changed snapshot
   re-reads the track list, which is written to the playlist's ``.spotdl`` file
   and reduced to distinct albums.
2. Top the Usenet queue up to ``ALBUM_MAX_IN_FLIGHT``: for each wanted album not
   already whole in the library, search Prowlarr, rank the releases and send
   the best to SABnzbd.
3. Hand what Usenet can't supply to spotdl (#205): a missing or failed album's
   absent tracks become ``fallback`` tracks the nightly sync downloads into the
   playlist's inbox.  Once the library holds them all the album is ``filled``.

Album playlists sync at the album level (#268).  Spotify decides *which*
albums are on the playlist; the local release decides *what is on* each album.
An album is on the playlist while at least one of its tracks is, and leaves it
(its tracks unlinked, its lines gone from the ``.m3u``) when none remain:
removing one track of several does nothing.  So the "tracks" of an album are
its Spotify tracklist (:func:`album_tracks`), not the playlist's: an imported
release with tracklist differences is the album, and never queues a fallback.

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
# fallback_from values for an imported release, before #268 stopped chasing its gaps.
PARTIAL, GAP = "partial", "gap"

MISSING_RETRY = timedelta(days=7)
# Releases tried per album before giving up, so an album beets can't match
# doesn't burn the grab budget.
MAX_ATTEMPTS = 3
# Import crashes retried before one counts against the release like a failed import (#261).
MAX_IMPORT_ERRORS = 3
# Deferred imports (MusicBrainz down, #264) retried before one counts against the
# release.  The lost-trigger recovery retries every tick (30 min) once the grab is
# an hour old, so this waits out an outage of about 6 hours.
MAX_IMPORT_DEFERRALS = 12
# A trigger lost for longer than this is recovered from SABnzbd history.
LOST_TRIGGER_AFTER = timedelta(hours=1)
# Albums in a row whose searches find nothing at all before a tick stops (#263):
# a guard against an indexer failing silently without Prowlarr's backoff.
EMPTY_RUN_LIMIT = 3

# SABnzbd's complete dir (/downloads/complete in its pod) and the inbox root
# album imports are moved to; both on the music-data volume, so the move is a
# rename.  beets' music_pipeline plugin reads the playlist from the inbox path.
USENET_COMPLETE = Path("/root/Music/usenet/complete")
USENET_INBOX = Path("/root/Music/inbox/usenet")
USENET_QUARANTINE = Path("/root/Music/quarantine/usenet")
WINDOW = timedelta(hours=24)
# Spotify album tracklists read per tick for spotdl fallbacks (#268): two calls each.
TRACKLISTS_PER_TICK = 10


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
    # album's playlist tracks, if more) is the album: not re-grabbed (#268).
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
        count as the album.  At least one track must have imported."""
        return 0 < missing < tracks and missing <= max(self.partial_max_missing, tracks * self.partial_max_percent // 100)


# ---------------------------------------------------------------------------
# Playlist → albums
# ---------------------------------------------------------------------------


def album_key(song: dict) -> str:
    """Spotify album id, or artist/name when a song has none (local files)."""
    return song.get("album_id") or f"{song.get('album_artist', '')}/{song.get('album_name', '')}".lower()


def track_entry(song: dict) -> list:
    """A song as an album record's track: ``[name, artist, song_id, isrc, disc,
    track]``.  The IDs let the completion tag the imported items with the
    entries they satisfy (#176)."""
    return [
        song.get("name", ""), (song.get("artists") or [""])[0],
        song.get("song_id"), song.get("isrc"), song.get("disc_number"), song.get("track_number"),
    ]


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
        album["tracks"].append(track_entry(song))
    # The playlist may hold part of the album: scale its tracks' duration up to
    # the whole album, which the matcher's size bound uses (#189).
    for album in albums.values():
        if album["tracks_count"] > len(album["tracks"]):
            album["duration"] = album["duration"] * album["tracks_count"] // len(album["tracks"])
    return albums


def album_tracks(record: dict) -> list[list]:
    """The album's tracks as spotdl would fetch them (#268): its Spotify
    tracklist once read (``album_songs``), else the tracks its playlists hold,
    each once, in playlist order."""
    if record.get("album_songs"):
        return [track_entry(s) for s in record["album_songs"]]
    seen: set = set()
    tracks = []
    for entries in record.get("playlists", {}).values():
        for t in entries:
            key = (t[2] if len(t) > 2 else None) or (t[0], t[1])
            if key not in seen:
                seen.add(key)
                tracks.append(t)
    return tracks


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
    def album_songs(self, album_id: str) -> list[dict]: ...


# have(key, record): the library holds the album whole (#268), so it needs no download.
Have = Callable[[str, dict], bool]


@dataclasses.dataclass
class TickResult:
    refreshed: list[str] = dataclasses.field(default_factory=list)
    searched: int = 0
    grabbed: int = 0
    # Per playlist, the former songs of the albums that left it (#268).
    removed_songs: dict[str, list[dict]] = dataclasses.field(default_factory=dict)


def refresh_playlists(
    state: State,
    playlists: list[tuple[str, str]],
    spotify: SpotifySource,
    spotdl_dir: Path,
    result: TickResult,
) -> None:
    """Re-read every album playlist whose Spotify snapshot changed.

    An album leaves the playlist only when none of its tracks remain (#268):
    its former songs go to ``result.removed_songs`` to be unlinked.  A track
    removed from an album that still has others on the playlist is not."""
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
        kept = {album_key(s) for s in songs}
        removed = [s for s in old_songs if album_key(s) not in kept]
        if removed:
            result.removed_songs[name] = removed
            for key, album in reduce_to_albums(removed).items():
                logger.info("    [UNLINK] %s — %s: no track left on %s", album["artist"], album["name"], name)
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


def _taken(state: State, key: str) -> tuple[set[str], set[str]]:
    """Releases other album records hold: (in flight, imported) GUIDs (#243).

    An original edition and its remaster can both be on the playlists and
    match the same release.  One in flight is waited for, so the second record
    doesn't download it again; one imported holds nothing the library lacks,
    so it is never grabbed again."""
    flight, done = set(), set()
    for other_key, record in state.albums.items():
        guid = (record.get("candidate") or {}).get("guid")
        if other_key == key or not guid:
            continue
        status = record.get("status")
        if status == GRABBED:
            flight.add(guid)
        elif status == IMPORTED or status in (FALLBACK, FILLED) and record.get("fallback_from") == PARTIAL:
            done.add(guid)
    return flight, done


def _check_indexers(prowlarr) -> None:
    """Raise when an indexer is in Prowlarr's failure backoff (#263).  Prowlarr
    then answers every search with an instant empty list, which would read as
    a miss and demote the album to spotdl."""
    blocked = prowlarr.blocked()
    if blocked:
        raise RuntimeError("indexer in Prowlarr backoff: " + ", ".join(
            f"{b.get('indexerId')} until {b.get('disabledTill')}" for b in blocked))


def top_up(
    state: State,
    settings: Settings,
    prowlarr,
    sabnzbd,
    have: Have,
    result: TickResult,
    now: Callable[[], datetime] = _now,
    overrides: dict[str, str] | None = None,
) -> None:
    """Search and grab wanted albums until the queue or a budget is full.

    *overrides* default to ``album-overrides.conf``, so every caller searches
    the same way (#202).

    The indexers' status is checked before the tick's first search and after
    every empty result: one in backoff stops the tick like a failed search, so
    no album changes status and no further hit is spent (#263).  So do
    ``EMPTY_RUN_LIMIT`` albums in a row with no results at all."""
    overrides = load_overrides() if overrides is None else overrides
    checked = False
    empty_run = 0
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
        if have(key, record):
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
            if not checked:
                _check_indexers(prowlarr)
                checked = True
            for query in search.queries:
                if sent and (releases or state.used("hit", now()) >= settings.hits_per_day):
                    break
                state.record("hit", now())
                result.searched += 1
                sent.append(query)
                releases = prowlarr.search(query)
                if not releases:
                    _check_indexers(prowlarr)
        except Exception as exc:
            logger.warning("[ERR]  %s: search failed: %s", label, exc)
            break  # Prowlarr or the indexer is down; the next tick retries.
        if not releases and len(sent) < len(search.queries):
            logger.info("Indexer API budget spent (%d/24h) before the artist-only retry for %s — stopping",
                        settings.hits_per_day, label)
            break  # The album keeps its status; the next window searches it again.
        empty_run = 0 if releases else empty_run + 1
        if empty_run >= EMPTY_RUN_LIMIT:
            logger.warning("[ERR]  %s: %d albums in a row with 0 results — stopping; is the indexer failing silently?",
                           label, empty_run)
            break  # This album keeps its status; the next tick searches it first.
        tracks = record.get("tracks_count") or max(len(t) for t in record["playlists"].values())
        in_flight, imported = _taken(state, key)
        blocklist = set(record["blocklist"]) | imported
        ranked = rank(releases, search.artist, search.album, tracks, blocklist, record.get("year"),
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
        if settings.mode == "on" and best.guid in in_flight:
            logger.info("[WAIT] %s: %s is in flight for another album — searching again after its import",
                        label, best.title)
            continue
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
# album_songs(album id): the Spotify album's tracklist, in the .spotdl song format.
AlbumSongs = Callable[[str], list[dict]]


def _spotify_album(key: str) -> bool:
    """Whether *key* is a Spotify album ID (not a local files' artist/name key)."""
    return bool(key) and "/" not in key


def _fallback_tracks(record: dict, missing: Missing) -> dict[str, list[str]]:
    """The Spotify IDs of the album's tracks the library lacks (:func:`album_tracks`),
    under the first playlist: spotdl fetches them into its inbox once (#268).
    Tracks without an ID (local files) can't be fetched."""
    playlists = list(record.get("playlists", {}))
    if not playlists:
        return {}
    ids = list(dict.fromkeys(t[2] for t in missing(playlists[0], album_tracks(record)) if len(t) > 2 and t[2]))
    return {playlists[0]: ids} if ids else {}


def _queue_fallback(record: dict, missing: Missing, source: str) -> None:
    """Leave the album's absent tracks to spotdl; *source* is the status it leaves."""
    record.update(status=FALLBACK, fallback_from=source, fallback_at=_iso(_now()),
                  fallback_tracks=_fallback_tracks(record, missing))
    n = sum(len(ids) for ids in record["fallback_tracks"].values())
    logger.info("[FALLBACK] %s — %s: %d track(s) left to spotdl (%s)", record["artist"], record["name"], n, source)


def settle_imported(record: dict) -> bool:
    """An imported release's fallback from before #268 (``fallback_from``
    ``partial`` or ``gap``) becomes ``imported``: the release is the album, so
    the tracks spotdl already fetched stay and the rest are no longer chased.
    True when *record* changed."""
    if record.get("status") != FALLBACK or record.get("fallback_from") not in (PARTIAL, GAP):
        return False
    record["status"] = IMPORTED
    record["settled_from"] = record.pop("fallback_from")
    # A gap release was never stamped imported; extras are attributed by this window.
    record.setdefault("imported_at", record.get("fallback_at"))
    record.pop("fallback_at", None)
    record.pop("fallback_tracks", None)
    return True


def fallbacks(state: State, missing: Missing, album_songs: AlbumSongs | None = None) -> None:
    """Hand missing and failed albums to spotdl, and mark fallback albums the
    library now completes as filled (``on`` only).  An album in fallback is no
    longer searched.

    spotdl fetches the Spotify album's whole tracklist (#268), read once per
    album by *album_songs* (``TRACKLISTS_PER_TICK`` a tick; a failed read stops
    the reads until the next tick).  Until it is read the playlist's tracks
    stand in, and the album can't be ``filled``.  An imported release's old
    partial or gap fallback is settled (:func:`settle_imported`)."""
    reads = 0
    for key, record in state.albums.items():
        if settle_imported(record):
            logger.info("[SETTLED] %s — %s: the release is the album; its %s fallback is no longer chased",
                        record["artist"], record["name"], record["settled_from"])
            continue
        status = record.get("status")
        if status not in (MISSING, FAILED, FALLBACK):
            continue
        if album_songs is not None and _spotify_album(key) and "album_songs" not in record \
                and reads < TRACKLISTS_PER_TICK:
            reads += 1
            try:
                record["album_songs"] = album_songs(key)
            except Exception as exc:
                logger.warning("[ERR]  %s — %s: reading the album's tracklist failed: %s",
                               record["artist"], record["name"], exc)
                reads = TRACKLISTS_PER_TICK
        if status in (MISSING, FAILED):
            _queue_fallback(record, missing, status)
        known = "album_songs" in record or not _spotify_album(key)
        record["fallback_tracks"] = _fallback_tracks(record, missing)
        if known and not record["fallback_tracks"]:
            record.update(status=FILLED, filled_at=_iso(_now()), fallback_tracks={})
            logger.info("[FILLED] %s — %s", record["artist"], record["name"])


def fallback_queue(state: State) -> int:
    """Tracks waiting for spotdl, over every fallback album."""
    return sum(
        len(ids) for r in state.albums.values() if r.get("status") == FALLBACK
        for ids in r.get("fallback_tracks", {}).values()
    )


def fallback_songs(state: State, playlist: str, songs: list[dict]) -> list[dict]:
    """The songs of *playlist* left to spotdl, album by album: from each
    fallback album's Spotify tracklist once read (#268), else from the
    playlist's .spotdl *songs*, in their order."""
    out: list[dict] = []
    seen: set[str] = set()
    for record in state.albums.values():
        ids = set(record.get("fallback_tracks", {}).get(playlist, [])) if record.get("status") == FALLBACK else set()
        for song in record.get("album_songs") or songs:
            if song.get("song_id") in ids and song["song_id"] not in seen:
                seen.add(song["song_id"])
                out.append(song)
    return out


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
    have: Have,
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
            fallbacks(state, missing, spotify.album_songs)
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


class ImportDeferred(Exception):
    """*import_inbox* could not judge the release: a service it needs, such as
    MusicBrainz, was unavailable (#264).  Retried without blame, up to
    MAX_IMPORT_DEFERRALS times."""


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
    fills the gaps.  An import only a few tracks short (``Settings.partial``,
    ``[PART]``) is the album too (#268): the release decides what is on it, so
    the tracks it lacks are not chased.  beets' quarantined tracks of it stay
    for review.  A failed album's tracks go to spotdl (#205).

    Usenet files carry no Spotify IDs, so after the import *tag_ids* maps each
    playlist's tracks onto the library items and records their Spotify IDs
    (#176), also when the album didn't import completely.

    An exception from *import_inbox* is not the release's fault (#261): the job
    dir goes back to the completed dir and the record stays ``grabbed``, so the
    next tick's lost-trigger recovery imports it again.  A completed job whose
    dir is gone is logged ``[LOST]`` and grabbed again, without a blocklist
    entry or an attempt.  An ``ImportDeferred`` (MusicBrainz down, #264) is
    retried the same way, under its own, longer cap.

    A blocklisted release is rolled back (#238): *rollback(playlist, since)*
    quarantines the items it imported that match no playlist entry (an
    expanded edition's outtakes); its matched items stay.  A release missing
    the same tracks as the previous one is a structural gap (``[GAP]``): the
    tracklists differ, so it is the album, imported like ``[PART]``, with no
    blocklist entry and no rollback.
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
    imported = False
    short = ""  # imported short of the playlist's tracks, yet the album: PART or GAP
    started: float | None = None
    gap_keys: list[str] | None = None
    if completion.ok and source is not None and source.is_dir() and playlists:
        first = playlists[0]
        dest = inbox_root / first / job_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(dest, ignore_errors=True)
        shutil.move(str(source), dest)
        logger.info("[IMPT] %s: importing %s", label, job_name)
        # A retried import counts its items from the first try's start (#261).
        started = record.setdefault("import_started", time.time())
        gave_up = ""  # why a retried import now counts against the release
        try:
            import_inbox()
        except ImportDeferred as exc:
            record["import_deferrals"] = record.get("import_deferrals", 0) + 1
            if record["import_deferrals"] <= MAX_IMPORT_DEFERRALS:
                logger.warning("[RETRY] %s: %s — moving %s back, the next tick retries (%d/%d)",
                               label, exc, job_name, record["import_deferrals"], MAX_IMPORT_DEFERRALS)
                if dest.exists():
                    shutil.move(str(dest), source)
                return GRABBED
            # An outage this long: count it against the release rather than wait forever.
            logger.warning("[FAIL] %s: the import of %s was deferred %d times: %s",
                           label, job_name, record["import_deferrals"], exc)
            gave_up = f"the import was deferred {record['import_deferrals']} times"
        except Exception:
            record["import_errors"] = record.get("import_errors", 0) + 1
            if record["import_errors"] < MAX_IMPORT_ERRORS:
                logger.exception("[RETRY] %s: the import of %s failed (%d/%d) — moving it back, the next tick retries",
                                 label, job_name, record["import_errors"], MAX_IMPORT_ERRORS)
                if dest.exists():
                    shutil.move(str(dest), source)
                return GRABBED
            # The same crash every time: count it against the release rather than loop.
            logger.exception("[FAIL] %s: the import of %s failed %d times", label, job_name, record["import_errors"])
            gave_up = f"the import crashed {MAX_IMPORT_ERRORS} times"
        # Leftovers beets doesn't import (nfo, sfv, cue, cover scans).
        shutil.rmtree(dest, ignore_errors=True)
        record.pop("import_started", None)
        record.pop("import_errors", None)
        record.pop("import_deferrals", None)
        tracks = record["playlists"][first]
        # Before counting: a track only the release-scoped rung finds counts as imported (#240).
        tag_ids(first, tracks, started, record.get("tracks_count") or 0)
        gaps = missing(first, tracks)
        imported = not gaps
        if not imported:
            reason = f"beets did not import {len(gaps)} of {len(tracks)} track(s) (quarantined or unmatched)"
            if gave_up:
                reason = f"{gave_up}; {reason}"
            # Records from before #176 hold only [name, artist].
            gap_keys = sorted({(t[2] if len(t) > 2 else None) or f"{t[0]} — {t[1]}" for t in gaps})
            if settings.partial(len(gaps), len(tracks)):
                short = "PART"
            elif gap_keys == record.get("gaps"):
                short = "GAP"
                reason += ", the same as the previous release"
        if imported or short:
            # The inbox path names one playlist; tag the album's tracks for the others.
            for other in playlists[1:]:
                add_source(first, other, record["playlists"][other])
        for playlist in playlists[1:]:
            tag_ids(playlist, record["playlists"][playlist], started, record.get("tracks_count") or 0)
    elif completion.ok and source is not None and not source.is_dir():
        # Nothing says the release is bad (#261): grab again, without an attempt.
        logger.warning("[LOST] %s: completed job dir not found: %r — not blocklisted, grabbing again",
                       label, completion.path)
        record["status"] = WANTED
        record.pop("nzo_id", None)
        record.pop("import_started", None)
        record.pop("import_errors", None)
        record.pop("import_deferrals", None)
        return WANTED
    elif completion.ok:
        reason = f"completed job dir not usable: {completion.path!r}"

    if imported or short:
        record["status"] = IMPORTED
        record["imported_at"] = _iso(_now())
        if imported:
            logger.info("[DONE] %s", label)
        else:
            record["gaps"] = gap_keys
            logger.info("[%s] %s: %s — the release is the album, not re-grabbing", short, label, reason)
        return IMPORTED

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
    if gap_keys is not None:
        record["gaps"] = gap_keys
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


# ---------------------------------------------------------------------------
# music-album-reset: undo a release attempt that wasn't the release's fault (#261)
# ---------------------------------------------------------------------------


def reset_album(state: State, query: str) -> tuple[str, dict, dict] | None:
    """Undo the last release attempt of the one album whose ``artist — name``
    contains *query* (case-insensitive): status ``wanted``, the last candidate
    off the blocklist, one attempt fewer, and its fallback cleared, so the next
    tick searches it again.  Returns ``(key, before, after)``; None when no
    album or more than one matches (logged)."""
    q = query.lower()
    hits = [(k, r) for k, r in state.albums.items() if q in f"{r.get('artist', '')} — {r.get('name', '')}".lower()]
    if len(hits) != 1:
        logger.error("%d album(s) match %r%s", len(hits), query,
                     "".join(f"\n  {k}: {r.get('artist')} — {r.get('name')} ({r.get('status')})" for k, r in hits))
        return None
    key, record = hits[0]
    before = json.loads(json.dumps(record))
    guid = (record.get("candidate") or {}).get("guid")
    if guid in record.get("blocklist", []):
        record["blocklist"].remove(guid)
    record["attempts"] = max(0, record.get("attempts", 0) - 1)
    record["status"] = WANTED
    for field in ("nzo_id", "import_started", "import_errors", "import_deferrals", "fallback_from", "fallback_at", "fallback_tracks"):
        record.pop(field, None)
    return key, before, record


def _state_lock(apply: bool):
    """The ``albums`` slot the tick and the import flow hold while they read and
    write .albums.json; a dry run needs none."""
    from contextlib import nullcontext  # noqa: PLC0415
    if not (apply and os.environ.get("PREFECT_API_URL")):
        return nullcontext()
    from prefect.concurrency.sync import concurrency  # noqa: PLC0415
    return concurrency("albums", occupy=1)


def reset_main() -> None:
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="music-album-reset",
                                     description="Undo an album's last release attempt (#261); dry run unless --apply")
    parser.add_argument("query", help="part of 'artist — album' that matches exactly one album")
    parser.add_argument("--apply", action="store_true", help="write .albums.json (default: dry run)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    with _state_lock(args.apply):
        state = State.load()
        result = reset_album(state, args.query)
        if result is None:
            raise SystemExit(1)
        key, before, after = result
        fields = ("status", "attempts", "blocklist", "candidate", "nzo_id", "fallback_from", "fallback_tracks")
        logger.info("%s: %s — %s", key, after.get("artist"), after.get("name"))
        for f in fields:
            if before.get(f) != after.get(f):
                logger.info("  %s: %s → %s", f, json.dumps(before.get(f)), json.dumps(after.get(f)))
        if args.apply:
            state.save()
            logger.info("Saved; the next album tick searches it again")
        else:
            logger.info("Dry run; --apply to write")


# ---------------------------------------------------------------------------
# music-album-requeue: undo fallbacks a silent indexer outage caused (#263)
# ---------------------------------------------------------------------------


def requeue(state: State, since: datetime | None = None, keys: list[str] | None = None) -> list[tuple[str, dict]]:
    """Put albums a miss demoted to spotdl back to ``wanted``: ``fallback``
    records with ``fallback_from`` ``missing`` that fell back at or after
    *since*, or whose key is in *keys*.  Their ``fallback_tracks``, the tracks
    spotdl hasn't downloaded yet, leave the spotdl queue; tracks it already
    fetched stay in the library, and beets skips them as duplicates when the
    album is imported.  Returns ``(key, before)`` per requeued album; a key in
    *keys* that isn't such a record is logged and skipped."""
    keys = set(keys or [])

    def demoted(record: dict | None) -> bool:
        return record is not None and record.get("status") == FALLBACK and record.get("fallback_from") == MISSING

    for key in sorted(keys):
        if not demoted(state.albums.get(key)):
            logger.warning("%s: not an album a miss demoted — skipping", key)
    found: list[tuple[str, dict]] = []
    for key, record in state.albums.items():
        if not demoted(record):
            continue
        at = record.get("fallback_at")
        if key not in keys and (since is None or not at or datetime.fromisoformat(at) < since):
            continue
        found.append((key, json.loads(json.dumps(record))))
        record["status"] = WANTED
        for field in ("fallback_from", "fallback_at", "fallback_tracks"):
            record.pop(field, None)
    return found


def requeue_main() -> None:
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(
        prog="music-album-requeue",
        description="Put albums a miss demoted to spotdl back to wanted (#263); dry run unless --apply")
    parser.add_argument("keys", nargs="*", metavar="album_id", help="album keys to requeue")
    parser.add_argument("--since", type=datetime.fromisoformat,
                        help="requeue every fallback from a miss at or after this ISO time (UTC if no offset)")
    parser.add_argument("--apply", action="store_true", help="write .albums.json (default: dry run)")
    args = parser.parse_args()
    if not args.keys and args.since is None:
        parser.error("give album ids, --since, or both")
    since = args.since
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    with _state_lock(args.apply):
        state = State.load(STATE_FILE)
        found = requeue(state, since, args.keys)
        for key, before in found:
            n = sum(len(ids) for ids in before.get("fallback_tracks", {}).values())
            logger.info("%s: %s — %s (fell back %s): wanted, %d track(s) off the spotdl queue",
                        key, before.get("artist"), before.get("name"), before.get("fallback_at"), n)
        logger.info("%d album(s) to requeue", len(found))
        if not found:
            return
        if args.apply:
            state.save(STATE_FILE)
            logger.info("Saved; the next album tick searches them again")
        else:
            logger.info("Dry run; --apply to write")
