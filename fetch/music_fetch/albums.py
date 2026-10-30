"""Album mode: download album-only playlists as whole albums from Usenet (#168).

A playlist flagged ``album`` in playlists.conf is never synced by spotdl.  Instead
:func:`tick` runs every ``ALBUM_POLL_SECONDS``:

1. Poll each album playlist's Spotify ``snapshot_id``.  Only a changed snapshot
   re-reads the track list, which is written to the playlist's store
   (homelab#1914) and reduced to distinct albums.
2. Top the Usenet queue up to ``ALBUM_MAX_IN_FLIGHT``: for each wanted album not
   already whole in the library, search Prowlarr, rank the releases (Spotify's
   edition first, #279) and send the best to SABnzbd.
3. Hand a missing or failed album to spotdl whole (#205): its tracks become
   ``fallback`` tracks the nightly sync downloads into the playlist's inbox.
   Once the library holds them all the album is ``filled``.  A request's
   tracks come from MusicBrainz instead (homelab#2478).

Before the top-up, :func:`auto_reimport` resets a few albums mixed from
several releases (#288), as ``music-album-reset --reimport`` does but
keeping the blocklist (#305), so the top-up grabs them again whole, never a
release that failed for them before: ``ALBUM_AUTO_REIMPORTS_PER_DAY``
(default 3, ``0`` turns it off) in a rolling 24 hours, while the grab budget
has room.  An album the operator holds (a ``hold <album id>`` line in
album-overrides.conf) is never reset, and an album is reset automatically only
once.  A reimport that finds no release left keeps the album it has.  An album
imported whole since #279 is never mixed (#295): a loose spotdl copy or a
local-only extra left on it is not something a reimport would retire.

On album playlists the album is the unit, not its tracks (#279, operator
2026-10-12).  The release grabbed for an album is imported whole as that
album, whatever its tracklist: an edition other than Spotify's, a missing or
extra track, a file beets can't match.  Nothing of it is quarantined, and
nothing it lacks goes to spotdl.  A release fails only for a broken download,
for being another album (:func:`wrong_album`), or for lacking tracks of its own
MusicBrainz release, when another copy of the same edition is tried
(:func:`complete`).

Album playlists sync at the album level (#268).  Spotify decides *which*
albums are on the playlist; the local release decides *what is on* each album.
An album is on the playlist while at least one of its tracks is, and leaves it
(its lines gone from the ``.m3u``) when none remain: removing one track of
several does nothing.

Every spotdl playlist gets its wanted tracks from Usenet first (#324):
:func:`feed_wants` turns each album holding ``ALBUM_WANT_MIN`` (``0`` turns
it off) or more of a playlist's wanted tracks into a record with ``origin:
"wants"`` whose tracks are the wanted ones only.  Such a record is an album nobody asked for
whole: a wanted track its release lacks, or every wanted track of an album
that goes missing or fails, goes back to spotdl's want list, never a
whole-album fallback.  While its grab is near (within a day's grab budget)
or its import pending, its tracks are held off spotdl (:func:`held_tracks`).

An album request (homelab#2478, :mod:`music_fetch.album_requests`) names a
MusicBrainz release group: :func:`feed_requests` gives it a record with
``origin: "request"`` whose tracks are its MusicBrainz tracklist, or upgrades
the record that has the release group already.  It is searched with the album
playlists' albums, and imported whole under ``inbox/usenet/request/``.  When
Usenet misses or fails, it falls back to spotdl from its MusicBrainz tracklist
(:func:`fallbacks`): the nightly downloads the tracks no item of the album
holds, the scan imports only those whose AcoustID fingerprint names the
track's own recording, and stamps them with the album's key, so a track
AcoustID doesn't know stays a gap.  A request in ``fallback`` is searched on
Usenet again every ``MISSING_RETRY`` while it has gaps (:func:`_due`): a
release found then is the album, and retires the spotdl copies.

``ALBUM_MODE`` is ``off`` (default), ``dry-run`` (search and log the pick, never
grab) or ``on``.  Indexer use is capped by a rolling-24h budget counted from our
own timestamps, because the indexer's reset window is unknown.

State lives in the music database once ``music-state-import`` has moved it
there (homelab#2410, :mod:`music_fetch.state`), and in one JSON file next to
the playlist stores (on the persistent pipeline-state volume) before that:
losing it would forget blocklists and the grab budget.  In the database the
album rows are keyed by MusicBrainz release group, and two Spotify editions of
one release group are one record (:mod:`music_fetch.resolve`, run every tick).
A record's key is its row id once ``music-album-rekey --apply`` has run
(homelab#2478), else its primary Spotify album ID (else its release group, else
a local album's ``artist/name``); a record not saved yet is keyed the old way
until its first save.  Never read a key for a Spotify album ID: that is
:func:`state.primary_spotify`.  A Spotify album ID from a song finds its record
through :meth:`State.refs`, and an override or hold names the record's row
(:mod:`music_fetch.overrides`, homelab#2484).
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

from music_fetch import album_requests, events, overrides, resolve, store
from music_fetch import state as statedb
from music_fetch.ids import spotify_key
from music_fetch.metrics import _gauge, _push
from music_fetch.usenet import Release, clean_album, edition, normalise, rank, readable, words

logger = logging.getLogger(__name__)

STATE_FILE = Path("/root/Music/inbox/spotdl/.albums.json")
# Search text for albums whose names no normalisation reaches (#197), and
# holds; mounted from the homelab repo next to playlists.conf until
# ``music-overrides import`` moves them to the music database (homelab#2484).
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
# A record's ``origin`` (#324): absent for an album playlist's, WANTS for one
# :func:`feed_wants` made from a spotdl playlist's want list, LIBRARY for one
# :func:`feed_library` made from the library's and every store's tracks
# (homelab#2403), REQUEST for one :func:`feed_requests` made from an album
# request (homelab#2478).
ALBUM, WANTS, LIBRARY, REQUEST = "album", "wants", "library", "request"
# A wants record not yet grabbed: the feed updates it, or drops it.
WAITING = (WANTED, DRY_RUN, MISSING, HAVE)

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
# A grabbed record's fields for its import in progress: an import retried (#261)
# or resumed after a restart (#344) keeps them, a finished or reset one drops them.
IMPORT_FIELDS = ("import_started", "import_errors", "import_deferrals", "import_path", "import_identity")
# Albums in a row whose searches find nothing at all before a tick stops (#263):
# a guard against an indexer failing silently without Prowlarr's backoff.
EMPTY_RUN_LIMIT = 3

# SABnzbd's complete dir (/downloads/complete in its pod) and the inbox root
# album imports are moved to; both on the music-data volume, so the move is a
# rename.  beets' music_pipeline plugin reads the playlist from the inbox path.
USENET_COMPLETE = Path("/root/Music/usenet/complete")
USENET_INBOX = Path("/root/Music/inbox/usenet")
USENET_QUARANTINE = Path("/root/Music/quarantine/usenet")
AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wav", ".wma", ".aiff", ".ape", ".mpc"}
WINDOW = timedelta(hours=24)
# Spotify album tracklists read per tick for spotdl fallbacks (#268): two calls each.
TRACKLISTS_PER_TICK = 10
# Albums shorter than this aren't judged by their titles (#279): a single's
# one title proves little either way.
WRONG_ALBUM_MIN_TRACKS = 4


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        logger.error("%s must be an integer — using %d", name, default)
        return default


def _env_choice(name: str, choices: tuple[str, ...]) -> str:
    value = os.environ.get(name, choices[0]).strip().lower()
    if value not in choices:
        logger.error("%s must be %s, got %r — using %s", name, " or ".join(choices), value, choices[0])
        return choices[0]
    return value


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
    # Mixed albums reset for a reimport per rolling 24h (#288); 0 turns it off.
    auto_reimports_per_day: int = 3
    # spotdl playlists (#324): wanted tracks an album needs to be grabbed whole; 0 turns it off.
    want_min: int = 2
    # ...and how many grabs ahead, in ALBUM_MAX_IN_FLIGHT batches, their tracks leave spotdl.
    hold_ahead: int = 4
    # Library-wide (homelab#2403): off, dry-run (log what it would queue) or on.
    library: str = "off"

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
            auto_reimports_per_day=_env_int("ALBUM_AUTO_REIMPORTS_PER_DAY", 3),
            want_min=_env_int("ALBUM_WANT_MIN", 2),
            hold_ahead=_env_int("ALBUM_WANT_HOLD_AHEAD", 4),
            library=_env_choice("ALBUM_LIBRARY", ("off", "dry-run", "on")),
        )


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
    """album-overrides.conf's search text by Spotify album ID, for names no
    normalisation can reach (#197).

    Format: one ``<album id>  <search words…>`` per line; ``#`` comments and
    blank lines are ignored.  A missing file means no overrides.  ``hold``
    lines are :func:`load_holds`'.  The tick reads :func:`album_fixes`.
    """
    return {o.refs[0]: o.words or "" for o in overrides.parse_albums(path or OVERRIDES_FILE)
            if o.kind == overrides.SEARCH}


def load_holds(path: Path | None = None) -> set[str]:
    """The album IDs album-overrides.conf holds (#288): ``hold <album id>``
    lines.  A held album is never reset automatically."""
    return {o.refs[0] for o in overrides.parse_albums(path or OVERRIDES_FILE) if o.kind == overrides.HOLD}


def album_fixes(path: Path | None = None) -> list[overrides.AlbumOverride]:
    """The search overrides and holds the tick applies (homelab#2484): the
    music database's once imported, else album-overrides.conf's.  Raises
    :class:`state.Unavailable` when the database holds the state and is down."""
    return overrides.album_overrides(path or OVERRIDES_FILE)


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
    # By album key (overrides.keyed), or by any of the record's Spotify IDs (a file's, homelab#2410).
    words = next((overrides[ref] for ref in _refs(key, record) if ref in overrides), None)
    if words:
        return Plan([words], album=words, note=" (override)")
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
class State(statedb.AlbumRows):
    """The album store (:class:`state.AlbumRows`): ``indexer`` holds
    ``{"t": iso, "kind": "hit" | "grab" | "reimport"}``, pruned to the last
    WINDOW ("reimport" counts automatic reimports, #288, not indexer use), and
    ``mixed`` the keys of the imported albums mixed from several releases at
    the last tick.  *in_db*: loaded from the music database, and saved there."""

    in_db: bool = False

    @classmethod
    def load(cls, path: Path = STATE_FILE) -> "State":
        """The album store: the music database's once the state is there (raises
        :class:`state.Unavailable` when it is down), else *path*'s JSON."""
        with statedb.session() as conn:
            if conn is not None:
                rows = statedb.load_albums(conn)
                return cls(**{f.name: getattr(rows, f.name) for f in dataclasses.fields(rows)}, in_db=True)
        if not path.exists():
            return cls()
        rows = statedb.rows_from_json(json.loads(path.read_text(encoding="utf-8")))
        return cls(**{f.name: getattr(rows, f.name) for f in dataclasses.fields(rows)})

    def save(self, path: Path = STATE_FILE) -> None:
        if self.in_db:
            with statedb.session() as conn:
                if conn is None:
                    raise statedb.Unavailable("the state left the database while a tick held it")
                statedb.save_albums(conn, self)
            return
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(statedb.rows_to_json(self), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def refs(self) -> dict[str, str]:
        """Every Spotify album ID (and local name) of a record → that record's key."""
        return self.by_ref()

    def find(self, ref: str) -> tuple[str, dict] | None:
        """The record a key, Spotify album ID or local name names."""
        key = ref if ref in self.albums else self.refs().get(ref)
        return None if key is None else (key, self.albums[key])

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
# mixed(key, record): why the record's album is mixed from several releases
# (music_scan.release.mixed_reasons), empty when it is one release (#288).
Mixed = Callable[[str, dict], list[str]]


@dataclasses.dataclass
class TickResult:
    refreshed: list[str] = dataclasses.field(default_factory=list)
    searched: int = 0
    grabbed: int = 0
    reimported: int = 0


def refresh_playlists(
    state: State,
    playlists: list[tuple[str, str]],
    spotify: SpotifySource,
    result: TickResult,
) -> None:
    """Re-read every album playlist whose Spotify snapshot changed, and write
    its tracklist to its store.

    An album leaves the playlist only when none of its tracks remain (#268):
    the ``.m3u`` lists whole albums, so a track removed from an album that
    still has others on the playlist changes nothing."""
    for name, url in playlists:
        snapshot = spotify.snapshot_id(url)
        entry = state.playlists.setdefault(name, {})
        if entry.get("snapshot_id") == snapshot:
            continue
        logger.info("==> Album playlist changed: %s — re-reading tracks", name)
        songs = spotify.songs(url)

        previous = store.load(name)
        kept = {album_key(s) for s in songs}
        removed = [] if previous is None else [s for s in previous.songs if album_key(s) not in kept]
        for key, album in reduce_to_albums(removed).items():
            logger.info("    [GONE] %s — %s: no track left on %s", album["artist"], album["name"], name)
        _save_store(name, songs, previous)

        wanted = reduce_to_albums(songs)
        # Two Spotify editions of one release group are one record (homelab#2410):
        # its tracks on this playlist are both editions', its fields its primary's.
        refs = state.refs()
        held: set[str] = set()
        for album_id, album in wanted.items():
            key = refs.get(album_id, album_id)
            record = state.albums.setdefault(key, statedb.new_record(key, status=WANTED, blocklist=[]))
            if record.pop("origin", None) in (WANTS, LIBRARY):
                # The album playlist wants it whole: its wanted tracks are no longer the point.
                record["playlists"] = {}
            primary = _primary_ref(record)
            if album_id == primary or primary not in wanted:
                record.update({k: v for k, v in album.items() if k != "tracks"})
            tracks = record.setdefault("playlists", {})
            tracks[name] = [*tracks[name], *album["tracks"]] if key in held else album["tracks"]
            held.add(key)
        # Albums this playlist no longer holds.
        for key, record in list(state.albums.items()):
            if key not in held and name in record.get("playlists", {}):
                del record["playlists"][name]
                if not record["playlists"] and not record.get("requested") \
                        and record.get("status") in (WANTED, DRY_RUN, MISSING, HAVE, IMPORTED, FAILED, FALLBACK, FILLED):
                    # A requested album stays: feed_requests gives it the request's tracks (homelab#2478).
                    del state.albums[key]
        entry["snapshot_id"] = snapshot
        result.refreshed.append(name)
        logger.info("    %d track(s) on %d album(s)", len(songs), len(wanted))


def _save_store(name: str, songs: list[dict], previous: store.Store | None) -> None:
    """An album playlist's store is its Spotify tracklist (homelab#1914), then
    *previous*'s local entries.  A failure fails the tick before the snapshot
    is recorded, so the next tick re-reads the playlist."""
    playlist = store.Store.from_songs(name, songs, keep=previous)
    playlist.source = store.SPOTIFY
    store.save(playlist)


# wants(playlist): every album of *playlist*'s want list (music_scan.want_albums),
# as ``(album key, playlist position of its first wanted track, wanted songs)``.
WantAlbums = Callable[[str], list[tuple[str, int, list[dict]]]]


def _wants(record: dict) -> bool:
    return record.get("origin") == WANTS


def _library(record: dict) -> bool:
    return record.get("origin") == LIBRARY


def _requested(record: dict) -> bool:
    return record.get("origin") == REQUEST


def _request_fallback(record: dict) -> bool:
    """A request's record with the request's own tracklist (homelab#2478): its
    fallback is built from MusicBrainz, not from a Spotify tracklist.  A
    record a playlist lists keeps the playlist's Spotify tracks, and falls
    back as an album playlist's does when it is one."""
    return _requested(record) and album_requests.SOURCE in record.get("playlists", {})


def _fed(record: dict) -> bool:
    """A record a feed made (wants or library), not an album playlist's: it
    never falls back, and its tracks are held off spotdl only near its turn."""
    return _wants(record) or _library(record)


def _label(record: dict) -> str:
    label = f"{record['artist']} — {record['name']}"
    if _wants(record):
        label += f" (wants: {', '.join(record.get('playlists', {}))})"
    elif _library(record):
        label += " (library)"
    elif _requested(record):
        label += " (request)"
    return label


def _back(record: dict) -> str:
    """A log note for a wants or library record that is missing or failed: no
    fallback, its wanted tracks go back to spotdl (#324)."""
    if _library(record):
        return "; its wanted tracks stay with spotdl"
    if _requested(record):
        return "; it falls back to spotdl from its MusicBrainz tracklist"
    if not _wants(record):
        return ""
    return f"; its {sum(len(t) for t in record.get('playlists', {}).values())} wanted track(s) go back to spotdl"


def _release_group(record: dict) -> str | None:
    """The album's MusicBrainz release group: its key in the database (homelab#2410),
    else the one an import identified its release by."""
    return record.get("release_group") or ((record.get("release_files") or {}).get("mb") or {}).get("mb_releasegroupid")


def _event(kind: str, key: str, record: dict, message: str, **data) -> None:
    """Record an album's event in the event store (homelab#2413); *message* is its log line."""
    events.record(kind, message, source="albums", spotify_album_id=statedb.primary_spotify(record),
                  release_group=_release_group(record),
                  data={"album": f"{record.get('artist')} — {record.get('name')}", "key": key,
                        "origin": record.get("origin") or "album", **data})


def _track_id(song: dict) -> str:
    return song.get("song_id") or song.get("url") or f"{song.get('name')}/{(song.get('artists') or [''])[0]}"


def feed_wants(
    state: State,
    playlists: list[str],
    wants: WantAlbums,
    settings: Settings,
) -> None:
    """Queue the albums the spotdl playlists' want lists share (#324).

    The threshold counts across *playlists*: an album with ``want_min`` or
    more distinct wanted tracks over all of them (a track two playlists list
    counts once) becomes or updates a record with ``origin: "wants"`` whose
    ``playlists[<name>]`` holds each playlist's wanted tracks only, one of
    them included.  Skipped, so their tracks stay on spotdl: local keys (no
    Spotify album to search or tag against) and albums an album playlist
    already wants whole.  Only a record not yet grabbed is updated.  A wants
    record whose album is no longer listed for a playlist (its tracks
    removed, or downloaded another way, spotdl's included while it was not
    held), or whose playlist is no longer in *playlists* (now an album or
    nosync playlist, or removed), leaves that playlist while ``wanted``,
    ``dry-run``, ``missing`` or ``have``, and is deleted with its last
    playlist; so is one whose total fell under the threshold.

    Then the records are re-ordered, since :func:`top_up` searches them in
    order: the album playlists' first, then the wants records most wanted
    first, then by their first wanted track (playlists in *playlists* order,
    then position), then the other wants records.  One ``[WANTS]`` line per
    playlist, with the tracks :func:`held_tracks` holds off spotdl."""
    # key → {"on": {playlist: songs}, "at": (playlist index, position), "id": the album ID its songs carry}
    found: dict[str, dict] = {}
    refs = state.refs()  # another edition's Spotify album ID → its record (homelab#2410)
    for index, name in enumerate(playlists):
        for album_id, first, songs in wants(name):
            key = refs.get(album_id, album_id)
            entry = found.setdefault(key, {"on": {}, "at": (index, first), "id": album_id})
            entry["on"][name] = [*entry["on"].get(name, []), *songs]

    def total(key: str) -> int:
        return len({_track_id(s) for songs in found[key]["on"].values() for s in songs})

    ranked = sorted((k for k in found if total(k) >= settings.want_min), key=lambda k: (-total(k), found[k]["at"]))
    listed_on: dict[str, set[str]] = {name: set() for name in playlists}
    skipped_on: dict[str, dict[str, int]] = {name: {} for name in playlists}
    for key in ranked:
        on = found[key]["on"]
        record = state.albums.get(key)
        why = ("local" if not _spotify_album(found[key]["id"])
               else ("request" if _requested(record) else "album playlist") if record and not _fed(record)
               else "library" if record and _library(record) and record.get("status") not in WAITING else "")
        for name in on:
            if why:
                skipped_on[name][why] = skipped_on[name].get(why, 0) + 1
            else:
                listed_on[name].add(key)
        if why:
            continue
        if record is None:
            record = state.albums[key] = statedb.new_record(key, status=WANTED, blocklist=[], origin=WANTS)
        elif _library(record):
            # A waiting library record (homelab#2403): the wanted tracks take it over.
            record.update(origin=WANTS, playlists={})
            record.pop("references", None)
        if record.get("status") in WAITING:
            distinct = list({_track_id(s): s for songs in on.values() for s in songs}.values())
            record.update({k: v for k, v in reduce_to_albums(distinct)[found[key]["id"]].items() if k != "tracks"})
            playlists_of = record.setdefault("playlists", {})
            for name, songs in on.items():
                playlists_of[name] = [track_entry(s) for s in songs]
    for key, record in list(state.albums.items()):
        if _wants(record) and record.get("status") in WAITING:
            record["playlists"] = {n: t for n, t in record.get("playlists", {}).items() if key in listed_on.get(n, ())}
            if not record["playlists"]:
                del state.albums[key]
    order = [k for k, r in state.albums.items() if not _fed(r)] + [k for k in ranked if k in state.albums]
    order += [k for k in state.albums if k not in set(order)]
    state.albums = {k: state.albums[k] for k in order}
    held = _held(state, settings)
    for name in playlists:
        queued = [(k, r["playlists"].get(name, [])) for k, r in state.albums.items()
                  if k in listed_on[name] and r.get("status") in (WANTED, DRY_RUN, GRABBED)]
        logger.info("[WANTS] %s: %d album(s) queued (%d wanted track(s)), %d held off spotdl%s", name, len(queued),
                    sum(len(t) for _, t in queued), sum(len(t) for k, t in queued if k in held),
                    "".join(f"; {n} {why} skipped" for why, n in sorted(skipped_on[name].items())))


# library(): every Spotify album the library's items and the stores' tracks
# reference (homelab#2403), as (key, references, Spotify's track count, whole,
# {playlist: every store track on the album}); references count each recording once.
LibraryAlbums = Callable[[], list[tuple[str, int, int, bool, dict[str, list[dict]]]]]


def feed_library(state: State, library: LibraryAlbums, settings: Settings) -> None:
    """Queue every album ``want_min`` or more tracks reference, held or
    wanted, on any playlist or none (homelab#2403), as a record with
    ``origin: "library"`` whose ``playlists`` hold every store track on the
    album, so ``have`` and the import treat it as an album playlist's.

    Skipped: albums the library holds whole (Spotify's track count, or placed
    by a release import), local keys, albums another record has (an album
    playlist's, or wants: :func:`feed_wants` runs first and takes a waiting
    library record over), and albums no store lists a track of (an import
    files the album under a playlist).  Ranked most referenced first, after
    the album playlists' and the wants records.  A waiting library record
    whose album no longer qualifies is dropped.

    ``ALBUM_LIBRARY``: ``off`` queues nothing and drops the waiting library
    records; ``dry-run`` only logs what ``on`` would queue; so does an
    ``ALBUM_WANT_MIN`` of 0 turn it off.  One ``[LIBRARY]`` summary a tick."""
    mode = settings.library if settings.want_min > 0 else "off"
    found = library() if mode != "off" else []
    ranked: list[tuple[str, int, int, dict[str, list[dict]]]] = []
    skipped: dict[str, int] = {}
    album_ids: dict[str, str] = {}  # key → the album ID its songs carry (another edition's, homelab#2410)
    by_ref = state.refs()
    for album_id, refs, tracks, whole, on in found:
        if refs < settings.want_min or whole:
            continue
        key = by_ref.get(album_id, album_id)
        record = state.albums.get(key)
        why = ("local" if not _spotify_album(album_id) else "no playlist track" if not on
               else "" if record is None or _library(record) else "wants" if _wants(record)
               else "request" if _requested(record) else "album playlist")
        if why:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        if key in album_ids:
            continue  # the record's other edition, already ranked
        album_ids[key] = album_id
        ranked.append((key, refs, tracks, on))
    ranked.sort(key=lambda c: (-c[1], c[0]))
    new = [key for key, *_ in ranked if key not in state.albums]
    if mode == "on":
        for key, refs, tracks, on in ranked:
            record = state.albums.setdefault(
                key, statedb.new_record(key, status=WANTED, blocklist=[], origin=LIBRARY))
            if record.get("status") not in WAITING:
                continue
            distinct = list({_track_id(s): s for songs in on.values() for s in songs}.values())
            record.update({k: v for k, v in reduce_to_albums(distinct)[album_ids[key]].items() if k != "tracks"})
            record["tracks_count"] = record.get("tracks_count") or tracks
            record["references"] = refs
            record["playlists"] = {name: [track_entry(s) for s in songs] for name, songs in on.items()}
    listed = {key for key, *_ in ranked}
    for key, record in list(state.albums.items()):
        if _library(record) and record.get("status") in WAITING and key not in listed:
            del state.albums[key]
    order = [k for k, r in state.albums.items() if not _fed(r)] + [k for k, r in state.albums.items() if _wants(r)]
    order += [k for k, *_ in ranked if k in state.albums]
    order += [k for k in state.albums if k not in set(order)]
    state.albums = {k: state.albums[k] for k in order}
    skips = "".join(f"; {n} {why} skipped" for why, n in sorted(skipped.items()))
    if mode == "dry-run":
        logger.info("[LIBRARY] dry-run: %d album(s) would be queued (%d new)%s", len(ranked), len(new), skips)
        for key, refs, tracks, on in ranked[:10]:
            album = reduce_to_albums([s for songs in on.values() for s in songs])[album_ids[key]]
            logger.info("  [LIBRARY] would queue %s — %s (%s): %d of %d track(s) referenced",
                        album["artist"], album["name"], key, refs, album["tracks_count"] or tracks)
    elif mode == "on":
        queued = sum(1 for k in listed if state.albums.get(k, {}).get("status") in (WANTED, DRY_RUN, GRABBED))
        logger.info("[LIBRARY] %d album(s) queued (%d new), %d held off spotdl%s", queued, len(new),
                    sum(1 for k in _held(state, settings) if k in listed), skips)


def feed_requests(state: State, requests: list[album_requests.Request]) -> None:
    """Give every album request (homelab#2478, :mod:`music_fetch.album_requests`)
    its album record, one per release group:

    - none has the release group: a new record, ``origin: "request"``, whose
      tracks under :data:`album_requests.SOURCE` are the request's tracklist;
    - a wants or library record not grabbed yet: the request takes it over,
      the whole album wanted instead of its wanted tracks; one grabbed or
      done keeps its tracks and becomes a request's;
    - an album playlist's record keeps its playlists' Spotify tracks while
      it has any; once it has none it becomes a request's, with the
      request's tracks.

    Every record a request holds is ``requested``, which
    :func:`refresh_playlists` never deletes.  A playlist's tracks always
    stand in for the request's: the request's tracks are dropped once a
    playlist lists the album again (an album playlist adding it, or a merge
    with its Spotify edition), so ``have`` and the import's checks read one
    tracklist.  An album not yet keyed by release group (a Spotify album the
    resolver hasn't looked up) gets a record of its own, which the resolver
    merges into the request's once it resolves.  The requests' records are
    searched with the album playlists', ahead of the fed ones.  One
    ``[REQUESTS]`` line."""
    if not requests:
        return
    by_group = {r["release_group"]: k for k, r in state.albums.items() if r.get("release_group")}
    new = took = 0
    for request in requests:
        tracks = [list(t) for t in request.tracks]
        key = by_group.get(request.release_group)
        record = state.albums.get(key) if key else None
        if record is None:
            record = {"status": WANTED, "blocklist": [], "origin": REQUEST, **request.fields(),
                      "release_group": request.release_group, "unresolved": None, "spotify_album_ids": [],
                      "names": [], "playlists": {album_requests.SOURCE: tracks}}
            by_group[request.release_group] = key = state.key(record)
            state.albums[key] = record
            new += 1
        elif _fed(record) and record.get("status") in WAITING:
            took += 1
            record.update(origin=REQUEST, playlists={album_requests.SOURCE: tracks}, **request.fields())
            record.pop("references", None)
        else:
            if _fed(record):
                took += 1
                record["origin"] = REQUEST
            on = record.setdefault("playlists", {})
            if any(name != album_requests.SOURCE for name in on):
                on.pop(album_requests.SOURCE, None)
            else:
                on.setdefault(album_requests.SOURCE, tracks)
                record.setdefault("origin", REQUEST)
        record["requested"] = True
        if album_requests.SOURCE in record.get("playlists", {}):
            # The release the tracks were read from: the fallback reads it again for its songs.
            record["tracklist_release"] = request.tracklist_release
    order = [k for k, r in state.albums.items() if not _fed(r)]
    order += [k for k in state.albums if k not in set(order)]
    state.albums = {k: state.albums[k] for k in order}
    statuses: dict[str, int] = {}
    for request in requests:
        status = state.albums[by_group[request.release_group]].get("status", WANTED)
        statuses[status] = statuses.get(status, 0) + 1
    logger.info("[REQUESTS] %d request(s): %d new album(s), %d taken over from wants or library; %s", len(requests),
                new, took, ", ".join(f"{s}={n}" for s, n in sorted(statuses.items())))


def _held(state: State, settings: Settings) -> set[str]:
    """The keys of the wants records whose tracks are held off spotdl (#324):
    every one ``grabbed``, and the first ``wanted`` ones, in search order, that
    the next grabs reach after the album playlists' ``wanted`` records:
    ``hold_ahead`` batches of ``max_in_flight``, and never more than a day's
    grab budget.  SABnzbd's queue, not the budget, sets the pace, so the rest
    of the queue stays on spotdl's want list until its turn nears, and a long
    queue never starves the nightly.  Nothing in ``off`` or ``dry-run``, where
    nothing grabs them."""
    if settings.mode != "on":
        return set()
    room = min(settings.max_in_flight * settings.hold_ahead, settings.grabs_per_day) - sum(
        1 for r in state.albums.values() if not _fed(r) and r.get("status") == WANTED)
    held = set()
    for key, record in _search_order(state):
        if not _fed(record):
            continue
        if record.get("status") == GRABBED:
            held.add(key)
        elif record.get("status") == WANTED and room > 0:
            held.add(key)
            room -= 1
    return held


def _search_order(state: State) -> list[tuple[str, dict]]:
    """``state.albums`` in the order :func:`top_up` searches them: a record a
    ``[LOST]`` import put back first (#344), then the rest in order."""
    return sorted(state.albums.items(), key=lambda kv: not kv[1].get("lost"))


def held_tracks(state: State, settings: Settings) -> set[str]:
    """The Spotify track IDs of the wants records :func:`_held` holds: the
    nightly leaves them to album mode."""
    keys = _held(state, settings)
    return {t[2] for k in keys for tracks in state.albums[k].get("playlists", {}).values()
            for t in tracks if len(t) > 2 and t[2]}


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
    if status == FALLBACK and _request_fallback(record) and record.get("fallback_tracks") \
            and record.get("searched_at"):
        # A request spotdl hasn't filled is searched on Usenet again, weekly as a miss is (homelab#2478).
        return now - datetime.fromisoformat(record["searched_at"]) >= MISSING_RETRY
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
        if record.get("best_copy"):
            done.add(record["best_copy"]["guid"])
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

    *overrides* (search words by album key) default to :func:`album_fixes`,
    so every caller searches the same way (#202).

    The indexers' status is checked before the tick's first search and after
    every empty result: one in backoff stops the tick like a failed search, so
    no album changes status and no further hit is spent (#263).  So do
    ``EMPTY_RUN_LIMIT`` albums in a row with no results at all."""
    if overrides is None:
        overrides = _search_overrides(state)
    checked = False
    empty_run = 0
    if settings.mode == "on":
        slots = settings.max_in_flight - state.in_flight()
    else:
        sampled = sum(1 for a in state.albums.values() if a.get("dry_run"))
        slots = min(settings.dry_run_per_tick, settings.dry_run_limit - sampled)
        if slots <= 0:
            logger.info("Dry-run sample complete (%d album(s), ALBUM_DRY_RUN_LIMIT) — not searching", sampled)
    for key, record in _search_order(state):
        label = _label(record)
        search = plan(key, record, overrides)
        if not _due(record, now(), settings.mode, search):
            continue
        if not search.queries:
            # Costs nothing, so it is logged even when no slot is free.
            record.update(status=MISSING if record.get("status") != FALLBACK else FALLBACK,
                          searched_at=_iso(now()), query="")
            logger.warning("[NOWORDS] %s: %s: add a search override", key, label)
            continue
        # In dry-run, re-checking a sampled miss after its query changed takes no sample slot.
        recheck = settings.mode != "on" and record.get("dry_run") and _requery(record, search)
        if slots <= 0 and not recheck:
            continue
        # A copy kept for now, or a reset to import again, isn't "have" (#279).
        if not (record.get("best_copy") or record.get("reimport")) and have(key, record):
            record["status"] = HAVE
            record.pop("lost", None)
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
        record.pop("lost", None)
        if settings.mode != "on":
            record["dry_run"] = True
        if record.get("edition") is not None:
            # An incomplete copy is in the library: only the same edition replaces it (#279).
            ranked = [r for r in ranked if sorted(edition(r.title)) == record["edition"]]
            if not ranked and record.get("best_copy"):
                best_copy = record["best_copy"]
                _done(key, record, label, f": no other copy of {best_copy['title']}'s edition; it is the album, "
                                     f"lacking {best_copy['missing']} track(s) of its own release")
                continue
        if not ranked and record.get("reimport"):
            # The library holds the album; a reimport finding nothing better leaves it (#305).
            record["status"] = IMPORTED
            record.pop("reimport", None)
            logger.info("[STAYS] %s%s: %d result(s), no release left to reimport (%d blocklisted); the album stays "
                        "as it is", label, search.note, len(releases), len(record["blocklist"]))
            continue
        if not ranked:
            # A request's fallback goes on: its gaps stay with spotdl until the next retry.
            if record.get("status") != FALLBACK:
                record["status"] = MISSING
            logger.info("[MISS] %s%s: %d result(s), none match; queries: %s%s",
                        label, search.note, len(releases), ", ".join(f'"{q}"' for q in sent),
                        "; its gaps stay with spotdl" if record["status"] == FALLBACK else _back(record))
            continue

        best: Release = ranked[0]
        if settings.mode == "on" and best.guid in in_flight:
            logger.info("[WAIT] %s: %s is in flight for another album — searching again after its import",
                        label, best.title)
            continue
        # Not its download_url: that carries Prowlarr's API key, and the record is shown (#343).
        record["candidate"] = {k: v for k, v in best.as_dict().items() if k != "download_url"}
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
        _event("grab", key, record, f"[GRAB] {label}{search.note} → {best.title} ({best.size / 1e6:.0f} MB)",
               release=best.title, size=best.size, guid=best.guid, reimport=bool(record.get("reimport")))


# ---------------------------------------------------------------------------
# Automatic reimport of albums mixed from several releases (#288)
# ---------------------------------------------------------------------------


def auto_reimport(
    state: State,
    settings: Settings,
    mixed: Mixed,
    holds: set[str],
    result: TickResult,
    now: Callable[[], datetime] = _now,
) -> None:
    """Record the imported albums *mixed* names in ``state.mixed``, and reset
    a few for a reimport, as ``music-album-reset --reimport`` does
    (:func:`reset_record`): the top-up then grabs each again and the import
    retires the copies the new release holds (#287).  Unlike the operator's
    reset it keeps the blocklist (#305): a release that failed for the album
    before (another album, an incomplete copy, a broken download, or the
    track-level rules' ``[FAIL]``) is not grabbed again.  The last release
    leaves it, as any reset's does: it is the one the album was imported from.

    Only in ``on``, at most ``auto_reimports_per_day`` in a rolling 24 hours,
    and only while the grab budget has room for the resets not yet grabbed.
    An album in *holds* is never reset, nor one reset automatically before
    (``auto_reimported_at``): if its reimport left it mixed, the operator
    decides (``music-album-audit-releases`` lists it).  One ``[AUTORESET]``
    line per reset."""
    before = set(state.mixed)
    state.mixed = []
    reasons: dict[str, list[str]] = {}
    for key, record in state.albums.items():
        if why := mixed(key, record):
            state.mixed.append(key)
            reasons[key] = why
            if key not in before:
                # An event when an album turns mixed, not at every tick it stays so.
                _event("mixed", key, record,
                       f"[MIXED] {record.get('artist')} — {record.get('name')} ({key}): {'; '.join(why)}",
                       reasons=why)
    if settings.mode != "on" or settings.auto_reimports_per_day <= 0:
        return
    for key in state.mixed:
        record = state.albums[key]
        if not holds.isdisjoint(_refs(key, record)) or record.get("auto_reimported_at"):
            continue
        if state.used("reimport", now()) >= settings.auto_reimports_per_day:
            logger.info("Automatic reimports spent (%d/24h, ALBUM_AUTO_REIMPORTS_PER_DAY)",
                        settings.auto_reimports_per_day)
            return
        waiting = sum(1 for r in state.albums.values() if r.get("reimport") and r.get("status") == WANTED)
        if state.used("grab", now()) + waiting >= settings.grabs_per_day:
            logger.info("Grab budget has no room for another reimport (%d/24h)", settings.grabs_per_day)
            return
        reset_record(record, reimport=True, keep_blocklist=True)
        record["auto_reimported_at"] = _iso(now())
        state.record("reimport", now())
        result.reimported += 1
        logger.info("[AUTORESET] %s — %s (%s): %s; reimporting", record.get("artist"), record.get("name"), key,
                    "; ".join(reasons[key]))
        _event("autoreset", key, record, f"[AUTORESET] {record.get('artist')} — {record.get('name')} ({key}): "
                                         f"{'; '.join(reasons[key])}; reimporting", reasons=reasons[key])


# ---------------------------------------------------------------------------
# Fallback: what Usenet can't supply goes to spotdl (#205)
# ---------------------------------------------------------------------------

# missing(playlist, tracks): the entries of *tracks* the library lacks.
Missing = Callable[[str, list[list]], list[list]]
# album_songs(album id): the Spotify album's tracklist, in spotdl's song format.
AlbumSongs = Callable[[str], list[dict]]
# request_songs(record): a request's tracklist release as spotdl songs, from
# MusicBrainz (album_requests.release_songs).
RequestSongs = Callable[[dict], list[dict]]
# recordings(key, record): the MusicBrainz recordings of the library items stamped
# with album record *key* (``album_record``), or on one of its Spotify albums: the
# request tracks it holds.
Recordings = Callable[[str, dict], set[str]]


def _refs(key: str, record: dict) -> tuple[str, ...]:
    """The album IDs an override, a hold or an operator's argument may name a record by:
    its key and every Spotify album ID it has (two editions of one release group, homelab#2410)."""
    return (key, *(record.get("spotify_album_ids") or ()))


def _spotify_album(ref: str) -> bool:
    """Whether a song's album *ref* (:func:`album_key`) is a Spotify album ID,
    not a local files' artist/name.  Never ask it of a record's key."""
    return statedb.is_spotify_album(ref)


def _primary_ref(record: dict) -> str | None:
    """The song album ref (:func:`album_key`) the record's fields come from:
    its primary Spotify album ID, else its local name."""
    return statedb.primary_spotify(record) or next(iter(record.get("names") or ()), None)


def _fallback_tracks(record: dict, missing: Missing) -> dict[str, list[str]]:
    """The Spotify IDs of the album's tracks the library lacks (:func:`album_tracks`),
    under the first playlist: spotdl fetches them into its inbox once (#268).
    Tracks without an ID (local files) can't be fetched."""
    playlists = list(record.get("playlists", {}))
    if not playlists:
        return {}
    ids = list(dict.fromkeys(t[2] for t in missing(playlists[0], album_tracks(record)) if len(t) > 2 and t[2]))
    return {playlists[0]: ids} if ids else {}


def _request_tracks(key: str, record: dict, recordings: Recordings) -> dict[str, list[str]]:
    """The recordings of a request's tracks no item stamped with its key holds,
    under :data:`album_requests.SOURCE`, once its songs are read (homelab#2478)."""
    held = recordings(key, record)
    ids = list(dict.fromkeys(s["song_id"] for s in record.get("album_songs") or () if s["song_id"] not in held))
    return {album_requests.SOURCE: ids} if ids else {}


def _mb_songs(record: dict) -> bool:
    """Whether the record's ``album_songs`` are a request's MusicBrainz songs, not a Spotify tracklist."""
    return bool(album_requests.url_recording(next(iter(record.get("album_songs") or ()), {}).get("url")))


def _queue_fallback(key: str, record: dict, tracks: dict[str, list[str]], source: str) -> None:
    """Leave the album's absent *tracks* to spotdl; *source* is the status it leaves."""
    record.update(status=FALLBACK, fallback_from=source, fallback_at=_iso(_now()), fallback_tracks=tracks)
    n = sum(len(ids) for ids in record["fallback_tracks"].values())
    logger.info("[FALLBACK] %s — %s: %d track(s) left to spotdl (%s)", record["artist"], record["name"], n, source)
    _event("fallback", key, record, f"[FALLBACK] {record['artist']} — {record['name']}: {n} track(s) left to "
                                    f"spotdl ({source})", tracks=n, left=source)


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


def fallbacks(state: State, missing: Missing, album_songs: AlbumSongs | None = None,
              request_songs: RequestSongs | None = None, recordings: Recordings | None = None) -> None:
    """Hand missing and failed albums to spotdl, and mark fallback albums the
    library now completes as filled (``on`` only).  An album in fallback is no
    longer searched, but for a request's with gaps, weekly (:func:`_due`).

    spotdl fetches the Spotify album's whole tracklist (#268), read once per
    album by *album_songs* (``TRACKLISTS_PER_TICK`` a tick; a failed read stops
    the reads until the next tick).  Until it is read the playlist's tracks
    stand in, and the album can't be ``filled``.  An imported release's old
    partial or gap fallback is settled (:func:`settle_imported`).  An imported
    release sends nothing to spotdl (#279): the release is the album.  A
    wants record (#324) never falls back: its wanted tracks are back on the
    playlist's own want list once it is missing or failed.

    A request's record (homelab#2478) falls back from MusicBrainz instead:
    *request_songs* reads its tracklist release as spotdl songs, once per
    album and from the same ``TRACKLISTS_PER_TICK``, and its fallback tracks are
    the recordings no item stamped with its key holds (*recordings*).  It is
    ``filled`` once the stamped items hold every one.  Without both hooks it
    stays missing or failed."""
    reads = 0
    for key, record in state.albums.items():
        if _request_fallback(record):
            if request_songs is None or recordings is None or record.get("status") not in (MISSING, FAILED, FALLBACK):
                continue
            if record.get("album_songs") and not _mb_songs(record):
                # A Spotify tracklist from when a playlist listed it (the stage 3 cutover): a
                # request's fallback is built from MusicBrainz.
                record.pop("album_songs")
            if "album_songs" not in record and reads < TRACKLISTS_PER_TICK:
                reads += 1
                try:
                    record["album_songs"] = request_songs(record)
                except Exception as exc:
                    logger.warning("[ERR]  %s — %s: reading the request's MusicBrainz tracklist failed: %s",
                                   record["artist"], record["name"], exc)
                    reads = TRACKLISTS_PER_TICK
            _fall_back(key, record, _request_tracks(key, record, recordings), bool(record.get("album_songs")))
            continue
        if _fed(record) or _requested(record):
            continue
        if settle_imported(record):
            logger.info("[SETTLED] %s — %s: the release is the album; its %s fallback is no longer chased",
                        record["artist"], record["name"], record["settled_from"])
            continue
        status = record.get("status")
        if status not in (MISSING, FAILED, FALLBACK):
            continue
        if _mb_songs(record):
            # A request's MusicBrainz songs, from before a playlist listed the album: its
            # fallback is the playlist's now, from the Spotify tracklist.
            record.pop("album_songs")
        primary = statedb.primary_spotify(record)
        if album_songs is not None and primary and "album_songs" not in record \
                and reads < TRACKLISTS_PER_TICK:
            reads += 1
            try:
                record["album_songs"] = album_songs(primary)
            except Exception as exc:
                logger.warning("[ERR]  %s — %s: reading the album's tracklist failed: %s",
                               record["artist"], record["name"], exc)
                reads = TRACKLISTS_PER_TICK
        _fall_back(key, record, _fallback_tracks(record, missing), "album_songs" in record or not primary)


def _fall_back(key: str, record: dict, tracks: dict[str, list[str]], known: bool) -> None:
    """Queue a missing or failed album's absent *tracks* for spotdl, update a
    fallback album's, and mark it filled once none are left and its whole
    tracklist is *known*."""
    if record.get("status") in (MISSING, FAILED):
        _queue_fallback(key, record, tracks, record["status"])
    record["fallback_tracks"] = tracks
    if known and not tracks:
        record.update(status=FILLED, filled_at=_iso(_now()), fallback_tracks={})
        logger.info("[FILLED] %s — %s", record["artist"], record["name"])
        _event("filled", key, record, f"[FILLED] {record['artist']} — {record['name']}")


def _spotdl_tracks(record: dict) -> dict[str, list[str]]:
    """The Spotify IDs of *record*'s tracks left to spotdl, by playlist: a
    fallback album's absent tracks.  An imported release's rejected ones no
    longer go (#279)."""
    if record.get("status") == FALLBACK:
        return record.get("fallback_tracks", {})
    return {}


def fallback_queue(state: State) -> int:
    """Tracks waiting for spotdl, over every album."""
    return sum(len(ids) for r in state.albums.values() for ids in _spotdl_tracks(r).values())


def fallback_songs(state: State, playlist: str, songs: list[dict]) -> list[dict]:
    """The songs of *playlist* left to spotdl, album by album: from each
    fallback album's Spotify tracklist once read (#268), else from the
    playlist's store *songs*, in their order."""
    out: list[dict] = []
    seen: set[str] = set()
    for record in state.albums.values():
        ids = set(_spotdl_tracks(record).get(playlist, []))
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


def origin_counts(state: State) -> dict[tuple[str, str], int]:
    """Records by ``(status, origin)`` (#324): ``album`` for an album
    playlist's, ``wants`` for a spotdl playlist's (:func:`feed_wants`),
    ``library`` for the library-wide feed's (:func:`feed_library`),
    ``request`` for an album request's (:func:`feed_requests`)."""
    counts = {(s, o): 0 for s in status_counts(State()) for o in (ALBUM, WANTS, LIBRARY, REQUEST)}
    for record in state.albums.values():
        k = (record.get("status", WANTED), record.get("origin") or ALBUM)
        counts[k] = counts.get(k, 0) + 1
    return counts


def push_metrics(state: State, success: bool) -> None:
    # One TYPE line per name: the Pushgateway rejects a repeated one, so the
    # labelled series are written by hand rather than through _gauge.
    now = _now()
    lines = ["# TYPE music_albums gauge"]
    lines += [f'music_albums{{status="{s}"}} {n}' for s, n in status_counts(state).items()]
    lines.append("# TYPE music_albums_status gauge")
    lines += [f'music_albums_status{{status="{s}",origin="{o}"}} {n}' for (s, o), n in origin_counts(state).items()]
    lines += [
        _gauge("music_albums_fallback_tracks", fallback_queue(state)),
        "# TYPE music_albums_indexer_used_24h gauge",
        f'music_albums_indexer_used_24h{{kind="hit"}} {state.used("hit", now)}',
        f'music_albums_indexer_used_24h{{kind="grab"}} {state.used("grab", now)}',
        _gauge("music_albums_mixed", len(state.mixed)),
        _gauge("music_albums_auto_reimports_24h", state.used("reimport", now)),
        _gauge("music_albums_last_tick_success", int(success)),
        _gauge("music_albums_last_tick_timestamp_seconds", int(now.timestamp())),
    ]
    _push("\n".join(lines), "music_albums")


def _search_overrides(state: State, fixes: list[overrides.AlbumOverride] | None = None) -> dict[str, str]:
    return overrides.keyed(album_fixes() if fixes is None else fixes, state)[0]


def resolve_keys(state: State, fixes: list[overrides.AlbumOverride] | None = None,
                 library_db: Path = resolve.LIBRARY_DB) -> resolve.Report | None:
    """Key the records the database can't key yet by release group, and merge
    two editions of one (homelab#2410, :mod:`music_fetch.resolve`): a new
    album, or one an import has just identified.  In the database only; a
    failure leaves the records as they are for the next tick."""
    if not state.in_db:
        return None
    try:
        with statedb.session() as conn:
            cache = statedb.get_all(conn, statedb.MB_RELEASES) if conn is not None else {}
        library = resolve.library_groups(library_db) if os.path.isfile(library_db) else {}
        report = resolve.resolve(state, cache, library, _search_overrides(state, fixes))
    except Exception:
        logger.exception("Resolving the album keys failed; the next tick tries again")
        return None
    if report.rekeyed or report.merged or report.conflicts:
        report.log()
    return report


def tick(
    playlists: list[tuple[str, str]],
    spotify: SpotifySource,
    prowlarr,
    sabnzbd,
    have: Have,
    settings: Settings | None = None,
    state_file: Path = STATE_FILE,
    on_completion: Callable[[State, "Completion"], object] | None = None,
    overrides_file: Path | None = None,
    missing: Missing | None = None,
    mixed: Mixed | None = None,
    want_playlists: list[str] | None = None,
    wants: WantAlbums | None = None,
    library: LibraryAlbums | None = None,
    request_songs: RequestSongs | None = None,
    recordings: Recordings | None = None,
) -> TickResult:
    """One album-mode pass: refresh changed playlists, queue the requested
    albums (:func:`feed_requests`), the albums of the spotdl playlists *want_playlists* (*wants*, :func:`feed_wants`) and the
    library-wide ones (*library*, :func:`feed_library`), recover lost import
    triggers (*on_completion*, ``on`` only), reset a few mixed albums for a
    reimport (*mixed*, :func:`auto_reimport`), top the queue up, then hand
    what Usenet can't supply to spotdl (*missing*, and for requests
    *request_songs* and *recordings*, ``on`` only)."""
    settings = settings or Settings.from_env()
    result = TickResult()
    if settings.mode == "off":
        return result
    try:
        state = State.load(state_file)
        # Read with the state: a tick never runs without its overrides and holds (homelab#2484),
        # or its requests (homelab#2478).
        fixes = album_fixes(overrides_file)
        requests = album_requests.album_requests()
    except statedb.Unavailable as exc:
        # Never run on the stale JSON (homelab#2410): skip, and let
        # MusicAlbumTickFailing say so once the outage outlasts 2h.
        logger.warning("[DB-DOWN] Album tick skipped — the music database is down: %s", exc)
        _push("\n".join([_gauge("music_albums_last_tick_success", 0),
                         _gauge("music_albums_last_tick_timestamp_seconds", int(_now().timestamp()))]),
              "music_albums")
        return result
    start = time.monotonic()
    success = False
    try:
        refresh_playlists(state, playlists, spotify, result)
        feed_requests(state, requests)
        if wants is not None:
            # ALBUM_WANT_MIN=0: no playlist is fed, and the waiting wants records go.
            feed_wants(state, (want_playlists or []) if settings.want_min > 0 else [], wants, settings)
        if library is not None:
            feed_library(state, library, settings)
        resolve_keys(state, fixes)
        # Keyed after the resolve, which may merge or rekey a record.
        search, holds = overrides.keyed(fixes, state)
        if settings.mode == "on" and on_completion is not None:
            for completion in lost_completions(state, sabnzbd):
                logger.info("Recovering lost import trigger for %s", completion.nzo_id)
                on_completion(state, completion)
        if mixed is not None:
            try:
                auto_reimport(state, settings, mixed, holds, result)
            except Exception:
                # The library's side failed: grabbing what is wanted needn't wait for it.
                logger.exception("Automatic reimports failed; the next tick tries again")
        top_up(state, settings, prowlarr, sabnzbd, have, result, overrides=search)
        if settings.mode == "on" and missing is not None:
            fallbacks(state, missing, spotify.album_songs, request_songs, recordings)
        success = True
    finally:
        state.save(state_file)
        push_metrics(state, success)
    counts = status_counts(state)
    logger.info(
        "Album tick (%s) in %.0fs: %d searched, %d grabbed, %d reset for a reimport; albums %s; "
        "indexer 24h: %d hit(s), %d grab(s)",
        settings.mode, time.monotonic() - start, result.searched, result.grabbed, result.reimported,
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


# identify(job dir): the release's own MusicBrainz match (#279), or None:
# {"mb_albumid": …, "album": …, "tracks": n, "files": n, "missing": n, "mb": {MB album fields}}.
Identify = Callable[[Path], "dict | None"]
# claim(playlist, since, key, release, job): stamp the release's fresh items (those
# imported from inbox folder *job*, #284) with their album record and this import of
# the release (``<guid>@<start>``: a release grabbed again after a reset is another
# copy); returns their titles.
Claim = Callable[[str, float, str, str, str], list[str]]
# retire(key, record, keep, drop): quarantine the record's other copies whose
# recording release *keep* holds, keeping the rest on the album as local-only
# extras (#287), or release *drop*'s items.  Returns the count quarantined.
Retire = Callable[[str, dict, "str | None", "str | None"], int]


def base_title(title: str) -> tuple[str, ...]:
    """A title's words with edition notes dropped, as release names spell them."""
    return tuple(words(clean_album(title or "")))


def titles_found(record: dict, titles: list[str]) -> tuple[int, int]:
    """``(found, total)``: how many of the album's Spotify titles (edition
    notes dropped) the release's items carry."""
    have = {base_title(t) for t in titles}
    spotify = [base_title(t[0]) for t in album_tracks(record) if t and t[0]]
    return sum(1 for t in spotify if t in have), len(spotify)


def wrong_album(found: int, total: int) -> bool:
    """A release that names fewer than half the album's titles is another album
    (#279).  Records of fewer than WRONG_ALBUM_MIN_TRACKS tracks aren't judged."""
    return total >= WRONG_ALBUM_MIN_TRACKS and found * 2 < total


def _done(key: str, record: dict, label: str, note: str) -> str:
    record["status"] = IMPORTED
    record["imported_at"] = _iso(_now())
    for field in ("best_copy", "edition", "reimport", "gaps", "rejected_tracks"):
        record.pop(field, None)
    logger.info("[DONE] %s%s", label, note)
    _event("import_done", key, record, f"[DONE] {label}{note}")
    return IMPORTED


def complete(
    state: State,
    completion: Completion,
    import_inbox: Callable[[], None],
    claim: Claim,
    add_source: Callable[[str, str, list[list[str]]], None],
    tag_ids: Callable[[str, list[list], float, int], None],
    retire: Retire,
    identify: Identify | None = None,
    import_rest: Callable[[str, str], int] | None = None,
    complete_root: Path = USENET_COMPLETE,
    inbox_root: Path = USENET_INBOX,
    quarantine_root: Path = USENET_QUARANTINE,
) -> str | None:
    """Handle one finished SABnzbd job.  Returns the album's new status, or None
    when the job isn't one of ours (a manual SABnzbd add).

    The album is the unit (#279): the whole release is the album, whatever its
    tracklist.  The job dir moves to ``inbox/usenet/<playlist>/``; *identify*
    matches it as a whole to a MusicBrainz release first (the files' own
    edition, and how many of that release's tracks have no file).
    *import_inbox* runs the beets import, and *import_rest* imports, as they
    are, the files beets couldn't match (a file present is never missing).
    *claim* stamps the release's items with the album record and the release,
    and the import is judged:

    - a release naming under half of the album's Spotify titles is another
      album (``[WRONG]``): its items are retired, and it is blocklisted;
    - a release missing tracks of its own MusicBrainz release is an incomplete
      copy (``[INCOMPLETE]``): blocklisted, and the next release of the same
      edition is grabbed.  The most complete copy stays in the library; after
      MAX_ATTEMPTS, or when no copy of that edition is left (:func:`top_up`),
      it is the album;
    - anything else is ``[DONE]``, with a note when the tracklist differs from
      Spotify's.  Nothing is quarantined, nothing goes to spotdl, except
      for a wants record (#324): the wanted tracks the release lacks stay on
      the playlist's want list, no longer held off spotdl.

    Whichever copy is kept, *retire* sends the record's other copies (earlier
    releases, legacy items) whose recording it holds to
    ``quarantine/replaced/``; a copy of a recording it lacks stays on the album
    as a local-only extra (#287).  Then *tag_ids* gives
    the kept copy Spotify track IDs as a best effort, retires the extras
    whose recording those IDs show the kept copy holds (#294), and
    canonicalises the album, onto paths the retired copies no longer hold
    (#283); album membership never depends on the IDs.

    An exception from *import_inbox* is not the release's fault (#261): the job
    dir goes back to the completed dir and the record stays ``grabbed``, so the
    next tick's lost-trigger recovery imports it again, judged on the first
    try's MusicBrainz match (beets has moved the files it imported out of the
    dir).  So does a restart mid-import, once :func:`park_interrupted` has
    moved the job back (#344).  A completed job whose dir is gone is logged
    ``[LOST]`` and searched again ahead of the queue, without a blocklist
    entry or an attempt.  An ``ImportDeferred`` (MusicBrainz down, #264) is
    retried the same way, under its own, longer cap.  A broken download is
    blocklisted; after MAX_ATTEMPTS the album is ``failed``, and the next tick
    hands it to spotdl whole (#205), unless an incomplete copy is in the
    library, which is then the album.  A failed release's files beets left in
    quarantine are removed; the last one's stay for review.
    """
    found_record = find_by_nzo(state, completion.nzo_id)
    if found_record is None:
        logger.info("Album import: %s is not an album-mode job — ignoring", completion.nzo_id)
        return None
    key, record = found_record
    label = _label(record)
    playlists = list(record.get("playlists", {}))
    source = _inside(complete_root, completion.path)
    job_name = source.name if source else ""
    guid = (record.get("candidate") or {}).get("guid") or ""
    title = (record.get("candidate") or {}).get("title") or job_name

    reason = completion.fail_message or "download failed"
    imported: list[str] | None = None  # the release's item titles, once imported
    started: float | None = None
    if completion.ok and source is not None and source.is_dir() and playlists:
        first = playlists[0]
        dest = inbox_root / first / job_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(dest, ignore_errors=True)
        shutil.move(str(source), dest)
        logger.info("[IMPT] %s: importing %s", label, job_name)
        # A retried import counts its items from the first try's start (#261).
        started = record.setdefault("import_started", time.time())
        # Where the job came from, so a restart's leftovers go back there (#344).
        record["import_path"] = completion.path
        identity = None
        if "import_identity" in record:
            # Judged on the first try's files: those beets imported are gone from the dir.
            identity = record["import_identity"]
        elif identify is not None:
            try:
                identity = record["import_identity"] = identify(dest)
            except Exception:
                logger.exception("[NOMB] %s: identifying %s failed; its completeness is unknown", label, job_name)
        gave_up = ""  # why a retried import now counts against the release
        try:
            import_inbox()
            if import_rest is not None:
                import_rest(first, job_name)
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
        for field in IMPORT_FIELDS:
            record.pop(field, None)
        this = f"{guid}@{int(started)}"
        titles = claim(first, started, key, this, job_name)
        found, total = titles_found(record, titles)
        if gave_up:
            reason = gave_up
        elif not titles:
            reason = "beets imported none of its files"
        elif wrong_album(found, total):
            reason = f"another album: {found} of the album's {total} titles in it"
            logger.warning("[WRONG] %s: %s is %s", label, job_name, reason)
            # Not the album's release group: identity is the other album's.
            _event("import_wrong", key, record, f"[WRONG] {label}: {job_name} is {reason}", release=title,
                   found=found, total=total, items=len(titles))
        else:
            imported = titles
        if imported is None and titles:
            n = retire(key, record, None, this)
            logger.info("[ROLLBACK] %s: %d item(s) of %s → quarantine/replaced/", label, n, job_name)
        if imported is not None:
            # The inbox path names one playlist; tag the album's items for the others.
            for other in playlists[1:]:
                add_source(first, other, record["playlists"][other])
            if identity:
                record["release_files"] = identity
            else:
                record.pop("release_files", None)
            note = f"; {total - found} of Spotify's {total} titles not in this release" if found < total else ""
            if _wants(record) and found < total:
                note = f"; {total - found} of its {total} wanted track(s) not in this release go back to spotdl"
            missing = (identity or {}).get("missing") or 0
            best = record.get("best_copy")

            def tag() -> None:
                # After retire (#283): the copies it quarantined no longer hold the
                # canonical paths, so canon can move every new item onto them.
                for playlist in playlists:
                    tag_ids(playlist, record["playlists"][playlist], started, record.get("tracks_count") or 0)

            if not missing:
                n = retire(key, record, this, None)
                tag()
                retired = f"; {n} item(s) of other copies → quarantine/replaced/" if n else ""
                return _done(key, record, label, f": {len(titles)} file(s) from {title}{note}{retired}")
            mine = {"guid": guid, "release": this, "title": title, "missing": missing}
            if best and best["missing"] <= missing:
                retire(key, record, best.get("release", best["guid"]), None)
                logger.info("[INCOMPLETE] %s: %s lacks %d track(s) of its own release; %s (%d) stays",
                            label, title, missing, best["title"], best["missing"])
                _event("import_incomplete", key, record,
                       f"[INCOMPLETE] {label}: {title} lacks {missing} track(s) of its own release; "
                       f"{best['title']} ({best['missing']}) stays",
                       release=title, missing=missing, kept=best["title"])
            else:
                retire(key, record, this, None)
                tag()
                record["best_copy"] = best = mine
                logger.info("[INCOMPLETE] %s: %s lacks %d track(s) of its own release (%s); kept for now",
                            label, title, missing, identity.get("album") or identity.get("mb_albumid"))
                _event("import_incomplete", key, record,
                       f"[INCOMPLETE] {label}: {title} lacks {missing} track(s) of its own release "
                       f"({identity.get('album') or identity.get('mb_albumid')}); kept for now",
                       release=title, missing=missing, kept=title)
            if guid and guid not in record["blocklist"]:
                record["blocklist"].append(guid)
            record["attempts"] = record.get("attempts", 0) + 1
            if record["attempts"] >= MAX_ATTEMPTS:
                return _done(key, record, label, f": the most complete copy, {best['title']}, lacks {best['missing']} "
                                            f"track(s) of its own release{note}")
            record["edition"] = sorted(edition(title))
            record["status"] = WANTED
            return WANTED
    elif completion.ok and source is not None and not source.is_dir():
        # Nothing says the release is bad (#261): grab again, without an attempt,
        # and ahead of the queue (#344), which may be days long.
        logger.warning("[LOST] %s: completed job dir not found: %r — not blocklisted; searched again at the next "
                       "top-up, ahead of the queue", label, completion.path)
        _event("import_lost", key, record, f"[LOST] {label}: completed job dir not found: {completion.path!r}",
               path=completion.path)
        record["status"] = WANTED
        record["lost"] = True
        record.pop("nzo_id", None)
        for field in IMPORT_FIELDS:
            record.pop(field, None)
        return WANTED
    elif completion.ok:
        reason = f"completed job dir not usable: {completion.path!r}"

    if guid and guid not in record["blocklist"]:
        record["blocklist"].append(guid)
    record["attempts"] = record.get("attempts", 0) + 1
    if source is not None:
        shutil.rmtree(source, ignore_errors=True)
    best = record.get("best_copy")
    if record["attempts"] >= MAX_ATTEMPTS and best:
        logger.warning("[FAIL] %s: %s — gave up after %d release(s)", label, reason, record["attempts"])
        _event("failure", key, record, f"[FAIL] {label}: {reason} — gave up after {record['attempts']} release(s)",
               reason=reason, attempts=record["attempts"], gave_up=True)
        return _done(key, record, label, f": the most complete copy, {best['title']}, lacks {best['missing']} "
                                    "track(s) of its own release")
    record["status"] = FAILED if record["attempts"] >= MAX_ATTEMPTS else WANTED
    if record["status"] == FAILED:
        logger.warning("[FAIL] %s: %s — gave up after %d release(s)%s", label, reason, record["attempts"],
                       _back(record))
        _event("failure", key, record,
               f"[FAIL] {label}: {reason} — gave up after {record['attempts']} release(s){_back(record)}",
               reason=reason, attempts=record["attempts"], gave_up=True)
    else:
        if job_name:
            for playlist in playlists:
                shutil.rmtree(quarantine_root / playlist / job_name, ignore_errors=True)
        logger.warning("[FAIL] %s: %s — blocklisted, will try the next release", label, reason)
        _event("failure", key, record, f"[FAIL] {label}: {reason} — blocklisted, will try the next release",
               reason=reason, attempts=record["attempts"], gave_up=False)
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


def park_interrupted(state: State, complete_root: Path = USENET_COMPLETE, inbox_root: Path = USENET_INBOX) -> int:
    """Move the job dir of every import a restart cut short back to SABnzbd's
    complete dir (#344).  Returns how many.

    :func:`complete` moves a job into ``inbox/usenet/<playlist>/`` before
    beets imports it; a pod replaced mid-import leaves the rest of its files
    there, and the next scan, any album's or the nightly's, would import them
    loose, without the release's tagging.  Back in the complete dir, the
    record still ``grabbed``, the lost-trigger recovery imports them as a
    retry (#261): its items count from the first try's start, so the release
    claims those beets imported before the restart too."""
    parked = 0
    for record in state.albums.values():
        path = record.get("import_path")
        if record.get("status") != GRABBED or not path or not record.get("playlists"):
            continue
        source = _inside(complete_root, path)
        if source is None or source.exists():
            continue
        dest = inbox_root / next(iter(record["playlists"])) / source.name
        if not dest.is_dir():
            continue
        source.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(dest), source)
        parked += 1
        logger.warning("[RESUME] %s: a restart cut the import of %s short — moved back to the complete dir; the "
                       "lost-trigger recovery imports it again", _label(record), source.name)
    return parked


def park_interrupted_imports(state_file: Path = STATE_FILE) -> int:
    """:func:`park_interrupted` over the saved state, at service start: before
    the API, the file watcher or a flow can start a scan.  Never raises."""
    try:
        state = State.load(state_file)
        parked = park_interrupted(state)
        if parked:
            state.save(state_file)
        return parked
    except Exception:
        logger.exception("Moving interrupted album imports back failed; a scan may import their files loose")
        return 0


# ---------------------------------------------------------------------------
# music-album-reset: undo a release attempt that wasn't the release's fault (#261)
# ---------------------------------------------------------------------------


def find_albums(albums: dict[str, dict], query: str) -> list[tuple[str, dict]]:
    """The records *query* names, from the most exact tier that has any (#289): the record whose key
    (its primary Spotify album ID) it is, or another of its Spotify album IDs or its release
    group (homelab#2410); else those whose ``artist — name`` it equals
    (case-insensitive); else those whose ``artist — name`` contains it.  An
    exact name wins over the longer names that contain it (an album and its
    ``(2018 Deluxe Remaster)``)."""
    if query in albums:
        return [(query, albums[query])]
    by_ref = [(k, r) for k, r in albums.items() if query in _refs(k, r) or query == r.get("release_group")]
    if by_ref:
        return by_ref
    q = query.lower()
    named = [(k, r, f"{r.get('artist', '')} — {r.get('name', '')}".lower()) for k, r in albums.items()]
    return [(k, r) for k, r, n in named if n == q] or [(k, r) for k, r, n in named if q in n]


def reset_album(state: State, query: str, reimport: bool = False) -> tuple[str, dict, dict] | None:
    """Undo the last release attempt of the one album *query* names
    (:func:`find_albums`): status ``wanted``, the last candidate
    off the blocklist, one attempt fewer, and its fallback cleared, so the next
    tick searches it again.

    With *reimport* (#279) the album starts over from one release: the whole
    blocklist and every attempt are cleared, and the record is grabbed even
    though the library holds its tracks.  The import then retires the copies
    already there (an album mixed from several releases, spotdl's top-ups
    aside).  Returns ``(key, before, after)``; None when no album or more than
    one matches (logged)."""
    hits = find_albums(state.albums, query)
    if len(hits) != 1:
        logger.error("%d album(s) match %r%s", len(hits), query,
                     "".join(f"\n  {k}: {r.get('artist')} — {r.get('name')} ({r.get('status')})" for k, r in hits))
        return None
    key, record = hits[0]
    before = json.loads(json.dumps(record))
    reset_record(record, reimport)
    return key, before, record


def reset_record(record: dict, reimport: bool = False, keep_blocklist: bool = False) -> None:
    """:func:`reset_album` on one *record*: also the album tick's automatic
    reimport of a mixed album (:func:`auto_reimport`, #288), which passes
    *keep_blocklist* so a reimport clears only the last release (#305)."""
    guid = (record.get("candidate") or {}).get("guid")
    if guid in record.get("blocklist", []):
        record["blocklist"].remove(guid)
    record["attempts"] = max(0, record.get("attempts", 0) - 1)
    record["status"] = WANTED
    for field in ("nzo_id", *IMPORT_FIELDS, "lost", "fallback_from", "fallback_at",
                  "fallback_tracks", "rejected_tracks", "best_copy", "edition"):
        record.pop(field, None)
    if reimport:
        record.update(attempts=0, reimport=True)
        if not keep_blocklist:
            record["blocklist"] = []
        for field in ("gaps", "settled_from", "filled_at", "release_files"):
            record.pop(field, None)


def _state_lock(apply: bool):
    """The ``albums`` slot the tick and the import flow hold while they read and
    write .albums.json; a dry run needs none, nor a run whose MCP job already
    holds it (``MUSIC_LOCKS_HELD``, homelab#2412)."""
    from contextlib import nullcontext  # noqa: PLC0415
    held = "albums" in os.environ.get("MUSIC_LOCKS_HELD", "").split(",")
    if held or not (apply and os.environ.get("PREFECT_API_URL")):
        return nullcontext()
    from prefect.concurrency.sync import concurrency  # noqa: PLC0415
    return concurrency("albums", occupy=1)


def _album_ref(ref: str) -> str:
    """An album argument with an ``open.spotify.com`` URL reduced to its ID (homelab#2411)."""
    return spotify_key(ref, "album")


def reset_main() -> None:
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="music-album-reset",
                                     description="Undo an album's last release attempt (#261); dry run unless --apply")
    parser.add_argument("query", type=_album_ref,
                        help="the album's Spotify ID or open.spotify.com URL, its 'artist — album', or a part of it "
                             "that matches exactly one album")
    parser.add_argument("--reimport", action="store_true",
                        help="start over from one release: clear the blocklist and attempts, grab it although the "
                             "library has its tracks, and retire the copies there on import (#279)")
    parser.add_argument("--apply", action="store_true", help="write .albums.json (default: dry run)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    with _state_lock(args.apply):
        state = State.load()
        result = reset_album(state, args.query, reimport=args.reimport)
        if result is None:
            raise SystemExit(1)
        key, before, after = result
        fields = ("status", "attempts", "blocklist", "candidate", "nzo_id", "fallback_from", "fallback_tracks",
                  "rejected_tracks", "best_copy", "edition", "reimport")
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
    refs = state.refs()  # another edition's Spotify album ID names its record (homelab#2410)
    keys = {refs.get(k, k) for k in keys or []}

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
    parser.add_argument("keys", nargs="*", metavar="album_id", type=_album_ref,
                        help="album keys (or open.spotify.com album URLs) to requeue")
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
