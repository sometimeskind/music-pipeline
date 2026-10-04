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
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol

from music_fetch.metrics import _gauge, _push
from music_fetch.usenet import Release, clean_album, rank

logger = logging.getLogger(__name__)

STATE_FILE = Path("/root/Music/inbox/spotdl/.albums.json")

WANTED = "wanted"      # waiting for a search slot
DRY_RUN = "dry-run"    # searched in dry-run; the pick is in `candidate`
GRABBED = "grabbed"    # sent to SABnzbd; waits for /trigger-album-import
MISSING = "missing"    # no matching release; searched again after MISSING_RETRY
HAVE = "have"          # every playlist track is already in the library

MISSING_RETRY = timedelta(days=7)
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
    # dry-run searches cost API hits too; keep them well under the daily budget.
    dry_run_per_tick: int = 3

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
        )


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
            "tracks": [],
        })
        album["tracks"].append([song.get("name", ""), (song.get("artists") or [""])[0]])
    return albums


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
                if not record["playlists"] and record.get("status") in (WANTED, DRY_RUN, MISSING, HAVE):
                    del state.albums[key]
        entry["snapshot_id"] = snapshot
        result.refreshed.append(name)
        logger.info("    %d track(s) on %d album(s)", len(songs), len(wanted))


def _due(record: dict, now: datetime, mode: str) -> bool:
    status = record.get("status")
    if status == WANTED:
        return True
    if status == DRY_RUN:
        return mode == "on"
    if status == MISSING:
        return now - datetime.fromisoformat(record["searched_at"]) >= MISSING_RETRY
    return False


def top_up(
    state: State,
    settings: Settings,
    prowlarr,
    sabnzbd,
    have: Callable[[str, list[list[str]]], bool],
    result: TickResult,
    now: Callable[[], datetime] = _now,
) -> None:
    """Search and grab wanted albums until the queue or a budget is full."""
    if settings.mode == "on":
        slots = settings.max_in_flight - state.in_flight()
    else:
        slots = settings.dry_run_per_tick
    for key, record in list(state.albums.items()):
        if slots <= 0:
            break
        if not _due(record, now(), settings.mode):
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

        label = f"{record['artist']} — {record['name']}"
        query = f"{record['artist']} {clean_album(record['name'])}"
        state.record("hit", now())
        result.searched += 1
        try:
            releases = prowlarr.search(query)
        except Exception as exc:
            logger.warning("[ERR]  %s: search failed: %s", label, exc)
            break  # Prowlarr or the indexer is down; the next tick retries.
        tracks = record.get("tracks_count") or max(len(t) for t in record["playlists"].values())
        ranked = rank(releases, record["artist"], record["name"], tracks, set(record["blocklist"]))
        record["searched_at"] = _iso(now())
        if not ranked:
            record["status"] = MISSING
            logger.info("[MISS] %s: %d result(s), none match", label, len(releases))
            continue

        best: Release = ranked[0]
        record["candidate"] = best.as_dict()
        if settings.mode != "on":
            record["status"] = DRY_RUN
            logger.info("[PICK] %s → %s (%.0f MB, %d grabs, %d other match(es))",
                        label, best.title, best.size / 1e6, best.grabs, len(ranked) - 1)
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
        logger.info("[GRAB] %s → %s (%.0f MB)", label, best.title, best.size / 1e6)


def status_counts(state: State) -> dict[str, int]:
    counts = {s: 0 for s in (WANTED, DRY_RUN, GRABBED, MISSING, HAVE)}
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
) -> TickResult:
    """One album-mode pass: refresh changed playlists, then top the queue up."""
    settings = settings or Settings.from_env()
    result = TickResult()
    if settings.mode == "off":
        return result
    state = State.load(state_file)
    start = time.monotonic()
    success = False
    try:
        refresh_playlists(state, playlists, spotify, spotdl_dir, result)
        top_up(state, settings, prowlarr, sabnzbd, have, result)
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
