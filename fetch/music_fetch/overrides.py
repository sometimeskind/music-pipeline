"""Search overrides, holds and track pins in the music database (homelab#2484).

Three per-album and per-track fixes the operator sets while diagnosing:

- a **search override** (#197): an album's Usenet search words, which also
  steer the edition ranking;
- a **hold** (#288): the album tick never resets the album for an automatic
  reimport;
- a **track pin** (#247): a source URL the nightly downloads a track from,
  for a track spotdl's search can't find.

They lived in the homelab ConfigMap (``album-overrides.conf``,
``track-pins.conf``), each change a PR and a pod restart.  Now they are rows
(migration 5 in :mod:`music_fetch.db`), set through ``music-overrides`` and
the Music MCP's tools, and the tick and the nightly read them on every run.
Every write records an ``override_set`` or ``override_cleared`` event in the
same transaction: the event log is the audit trail git history was.

An override or hold names its album's **row**, not its release group: most
rows have no release group until their import, and those are the albums an
override is for.  The row keeps it through a release group arriving, and a
merge or a deletion moves it by ref (:func:`state.attach_overrides`).  So the
selector is anything that names a row: its key, any Spotify album ID, its
release group, or ``artist — album`` (:func:`albums.find_albums`).  A pin
names a Spotify track ID, what spotdl matches a song by.

Which copy is read: the tables once ``music-overrides import --apply`` has run
(the ``overrides-imported`` kv marker), else the ConfigMap's files, so an image
with this module deploys before the import without dropping an override.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from music_fetch import events, ids
from music_fetch import state as statedb

logger = logging.getLogger(__name__)

SEARCH, HOLD, PIN = "search", "hold", "pin"
KINDS = (SEARCH, HOLD, PIN)
TRACK_URL = "https://open.spotify.com/track/"
# beets' library, read for whether a pinned track is held already.
LIBRARY_DB = Path("/root/.config/beets/library.db")


@dataclasses.dataclass
class AlbumOverride:
    """A search override (*words*) or a hold on the album row *album_id*;
    *refs* name that row (None *album_id*: detached, or a file entry)."""
    kind: str
    refs: list[str]
    words: str | None = None
    album_id: int | None = None
    reason: str = ""
    set_by: str = ""
    set_at: datetime | None = None
    id: int | None = None


@dataclasses.dataclass
class Pin:
    spotify_track_id: str | None
    url: str
    isrc: str | None = None
    label: str = ""
    reason: str = ""
    set_by: str = ""
    set_at: datetime | None = None
    id: int | None = None


class Refused(RuntimeError):
    """A write that can't be done (no such album, not imported yet): exit 1 with the reason."""


# ---------------------------------------------------------------------------
# The ConfigMap files (read until the import, and by the import)
# ---------------------------------------------------------------------------


def _entries(path: Path) -> list[tuple[list[str], str, str]]:
    """Each entry line of *path* as (its words, its trailing comment, the
    comment block above it).  A comment block applies to the entries right
    below it, up to the next blank line, so a header separated by a blank line
    is nobody's."""
    if not path.is_file():
        return []
    out = []
    block: list[str] = []
    entries_since = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            block, entries_since = [], False
            continue
        if line.startswith("#"):
            if entries_since:
                block, entries_since = [], False
            block.append(line.lstrip("#").strip())
            continue
        text, _, comment = line.partition(" #")
        out.append((text.split(), comment.strip(), " ".join(block)))
        entries_since = True
    return out


def parse_albums(path: Path) -> list[AlbumOverride]:
    """album-overrides.conf: ``<spotify album id>  <search words…>`` and ``hold
    <spotify album id>`` lines, each with its comment as the reason."""
    from music_fetch.usenet import normalise  # noqa: PLC0415 — usenet imports the whole fetch side

    out = []
    for words, comment, block in _entries(path):
        reason = "; ".join(r for r in (block, comment) if r)
        if words[0] == HOLD and len(words) >= 2:
            out.append(AlbumOverride(HOLD, [words[1]], reason=reason, set_by="album-overrides.conf"))
        elif len(words) >= 2 and (search := normalise(" ".join(words[1:]))):
            out.append(AlbumOverride(SEARCH, [words[0]], words=search, reason=reason, set_by="album-overrides.conf"))
    return out


def parse_pins(path: Path) -> list[Pin]:
    """track-pins.conf: ``<spotify track id or URL>  <youtube url>  [label…]``."""
    out = []
    for words, comment, block in _entries(path):
        try:
            if len(words) < 2:
                raise ValueError("no URL")
            url = pin_source(words[1])
        except ValueError:
            logger.warning("[PIN] ignoring %r in %s: want <spotify track id> <youtube video url>",
                           " ".join(words), path.name)
            continue
        out.append(Pin(track_id(words[0]), url, label=" ".join(words[2:]),
                       reason="; ".join(r for r in (block, comment) if r), set_by="track-pins.conf"))
    return out


def track_id(ref: str) -> str:
    """A Spotify track ID from an ID or an open.spotify.com track URL."""
    return ref.split("?")[0].rstrip("/").rsplit("/", 1)[-1]


def pin_source(ref: str) -> str:
    """The URL a pin downloads from: a YouTube or YouTube Music video, as its
    canonical watch URL (homelab#2411), on music.youtube.com when *ref* was a
    YouTube Music link.  Anything else (another site, a playlist) raises
    ValueError: a pin's URL reaches yt-dlp, which must fetch only YouTube."""
    from urllib.parse import urlsplit  # noqa: PLC0415

    video = ids.youtube_id(ref)
    music = (urlsplit(ref.strip()).hostname or "").lower() == "music.youtube.com"
    return f"https://music.youtube.com/watch?v={video}" if music else ids.YOUTUBE_WATCH_URL + video


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def imported(conn: psycopg.Connection | None) -> bool:
    return conn is not None and statedb.get(conn, statedb.OVERRIDES_IMPORTED) is not None


def load_album_overrides(conn: psycopg.Connection) -> list[AlbumOverride]:
    return [AlbumOverride(kind, list(refs), words, album_id, reason, set_by, set_at, i)
            for i, kind, refs, words, album_id, reason, set_by, set_at in conn.execute(
                "SELECT id, kind, refs, words, album_id, reason, set_by, set_at FROM album_overrides ORDER BY id")]


def load_pins(conn: psycopg.Connection) -> list[Pin]:
    return [Pin(track, url, isrc, label, reason, set_by, set_at, i)
            for i, track, isrc, url, label, reason, set_by, set_at in conn.execute(
                "SELECT id, spotify_track_id, isrc, url, label, reason, set_by, set_at FROM track_pins ORDER BY id")]


def album_overrides(path: Path) -> list[AlbumOverride]:
    """The overrides and holds the tick applies: the table's once imported,
    else *path*'s (album-overrides.conf).  Raises :class:`state.Unavailable`
    when the state is in the database and the database is down."""
    with statedb.session() as conn:
        if imported(conn):
            assert conn is not None
            return load_album_overrides(conn)
    return parse_albums(path)


def pins(path: Path) -> dict[str, str]:
    """Source URL by Spotify track URL, what the nightly matches a song by:
    the table's pins once imported, else *path*'s (track-pins.conf).  A pin
    with an ISRC and no Spotify track ID (for homelab#2478's MusicBrainz-built
    tracks) has no Spotify URL, so it is not here yet."""
    with statedb.session() as conn:
        if imported(conn):
            assert conn is not None
            listed = load_pins(conn)
        else:
            listed = parse_pins(path)
    return {TRACK_URL + p.spotify_track_id: p.url for p in listed if p.spotify_track_id}


def keyed(overrides: Iterable[AlbumOverride], rows: statedb.AlbumRows) -> tuple[dict[str, str], set[str]]:
    """*overrides* by the album key of the record each names in *rows*: (search
    words by key, held keys).  A row ID finds its record; failing that, a ref
    does (a file entry, or a row merged away in this tick and not saved yet).
    A record's own override wins over one found by ref, as
    :func:`state.attach_overrides` decides at the save."""
    by_id = {r["_id"]: k for k, r in rows.albums.items() if r.get("_id") is not None}
    by_ref = rows.by_ref()
    by_group = {r["release_group"]: k for k, r in rows.albums.items() if r.get("release_group")}

    def by_refs(o: AlbumOverride) -> str | None:
        return next((k for ref in o.refs
                     if (k := by_ref.get(ref) or by_group.get(ref) or (ref if ref in rows.albums else None))), None)

    search: dict[str, str] = {}
    holds: set[str] = set()
    own = [(o, by_id.get(o.album_id)) for o in overrides]
    for o, key in [*((o, k) for o, k in own if k), *((o, by_refs(o)) for o, k in own if not k)]:
        if key is None:
            continue
        if o.kind == HOLD:
            holds.add(key)
        else:
            search.setdefault(key, o.words or "")
    return search, holds


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def caller() -> str:
    """Who sets an override: the MCP caller a job runs for, else whoever runs the CLI."""
    return os.environ.get("MUSIC_EVENT_CALLER") or f"cli:{os.environ.get('USER') or 'root'}"


def _album_label(key: str, record: Mapping) -> str:
    return f"{record.get('artist', '')} — {record.get('name', '')} ({key})"


def _album_event(conn, event: str, message: str, key: str, record: Mapping, **data) -> None:
    events.insert(conn, event, message, source="overrides", spotify_album_id=statedb.primary_spotify(record),
                  release_group=record.get("release_group"),
                  data={"key": key, "album": f"{record.get('artist', '')} — {record.get('name', '')}", **data})


def _row_refs(record: Mapping) -> list[str]:
    return sorted({*(record.get("spotify_album_ids") or ()), *(record.get("names") or ()),
                   *([record["release_group"]] if record.get("release_group") else [])})


def current(conn: psycopg.Connection, kind: str, album_id: int) -> AlbumOverride | None:
    return next((o for o in load_album_overrides(conn) if o.album_id == album_id and o.kind == kind), None)


def set_album(conn: psycopg.Connection, kind: str, key: str, record: Mapping, *, words: str | None = None,
              reason: str = "", by: str | None = None) -> None:
    """Set the search override (*words*) or the hold on *record*'s row, replacing its own, with its event."""
    by = by or caller()
    with conn.transaction():
        conn.execute("DELETE FROM album_overrides WHERE album_id = %s AND kind = %s", (record["_id"], kind))
        conn.execute("INSERT INTO album_overrides (kind, album_id, refs, words, reason, set_by) "
                     "VALUES (%s, %s, %s, %s, %s, %s)", (kind, record["_id"], _row_refs(record), words, reason, by))
        what = f'search override "{words}"' if kind == SEARCH else "hold"
        _album_event(conn, "override_set", f"[OVERRIDE] {_album_label(key, record)}: {what} set by {by}"
                     + (f" ({reason})" if reason else ""), key, record, kind=kind, words=words, reason=reason)


def clear_album(conn: psycopg.Connection, kind: str, key: str, record: Mapping, by: str | None = None) -> bool:
    """Clear *record*'s search override or hold, with its event; False when it had none."""
    by = by or caller()
    with conn.transaction():
        gone = conn.execute("DELETE FROM album_overrides WHERE album_id = %s AND kind = %s RETURNING words, reason",
                            (record["_id"], kind)).fetchone()
        if gone is None:
            return False
        what = f'search override "{gone[0]}"' if kind == SEARCH else "hold"
        _album_event(conn, "override_cleared", f"[OVERRIDE] {_album_label(key, record)}: {what} cleared by {by}",
                     key, record, kind=kind, words=gone[0], reason=gone[1])
    return True


def set_pin(conn: psycopg.Connection, pin: Pin, title: str = "", by: str | None = None) -> None:
    by = by or caller()
    with conn.transaction():
        conn.execute("DELETE FROM track_pins WHERE spotify_track_id = %s", (pin.spotify_track_id,))
        conn.execute("INSERT INTO track_pins (spotify_track_id, isrc, url, label, reason, set_by) "
                     "VALUES (%s, %s, %s, %s, %s, %s)",
                     (pin.spotify_track_id, pin.isrc, pin.url, pin.label, pin.reason, by))
        events.insert(conn, "override_set", f"[PIN] {title or pin.spotify_track_id}: pinned to {pin.url} by {by}",
                      source="overrides", data={"kind": PIN, "spotify_track_id": pin.spotify_track_id,
                                                "isrc": pin.isrc, "url": pin.url, "label": pin.label,
                                                "reason": pin.reason})


def remove_pin(conn: psycopg.Connection, track: str, title: str = "", by: str | None = None) -> bool:
    by = by or caller()
    with conn.transaction():
        gone = conn.execute("DELETE FROM track_pins WHERE spotify_track_id = %s RETURNING url, isrc",
                            (track,)).fetchone()
        if gone is None:
            return False
        events.insert(conn, "override_cleared", f"[PIN] {title or track}: pin to {gone[0]} removed by {by}",
                      source="overrides", data={"kind": PIN, "spotify_track_id": track, "url": gone[0],
                                                "isrc": gone[1]})
    return True


# ---------------------------------------------------------------------------
# The listing (music-overrides list, the MCP's list_overrides)
# ---------------------------------------------------------------------------


def _iso(t: datetime | None) -> str | None:
    return None if t is None else t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def listing(conn: psycopg.Connection, rows: statedb.AlbumRows, kind: str | None = None) -> list[dict]:
    """Every override, hold and pin (or those of *kind*), oldest first, each
    with its album or track, when and by whom it was set, and why.  An
    override whose album row is gone is ``detached``: it re-attaches when an
    album with one of its refs comes back."""
    by_id = {r["_id"]: (k, r) for k, r in rows.albums.items() if r.get("_id") is not None}
    out: list[dict] = []
    if kind in (None, SEARCH, HOLD):
        for o in load_album_overrides(conn):
            if kind not in (None, o.kind):
                continue
            hit = by_id.get(o.album_id)
            album = None if hit is None else {
                "key": hit[0], "artist": hit[1].get("artist"), "name": hit[1].get("name"),
                "status": hit[1].get("status"), "release_group": hit[1].get("release_group")}
            out.append({"kind": o.kind, "album": album, "detached": hit is None, "refs": o.refs,
                        **({"words": o.words} if o.kind == SEARCH else {}),
                        "reason": o.reason, "set_by": o.set_by, "set_at": _iso(o.set_at)})
    if kind in (None, PIN):
        titles = track_titles(conn)
        out.extend({"kind": PIN, "spotify_track_id": p.spotify_track_id, "isrc": p.isrc,
                    "track": titles.get(p.spotify_track_id or ""), "url": p.url, "label": p.label,
                    "reason": p.reason, "set_by": p.set_by, "set_at": _iso(p.set_at)} for p in load_pins(conn))
    return out


def track_titles(conn: psycopg.Connection) -> dict[str, str]:
    """``artist — title`` by Spotify track ID, from every playlist store."""
    return {song_id: f"{s.get('artist', '')} — {s.get('name', '')}" for _, s, song_id in _store_songs(conn)}


def _store_songs(conn: psycopg.Connection) -> Iterable[tuple[str, dict, str]]:
    """(playlist, song record, Spotify track ID) for each store entry (``{"song": …}``) with an ID."""
    for playlist, entries in conn.execute("SELECT playlist, entries FROM playlist_stores ORDER BY playlist"):
        for entry in entries or []:
            if (song := entry.get("song")) and (song_id := song.get("song_id")):
                yield playlist, song, song_id


def find_track(conn: psycopg.Connection, track: str) -> tuple[dict | None, list[str]]:
    """A Spotify track's song record from the playlist stores, and the playlists listing it."""
    hits = [(playlist, song) for playlist, song, song_id in _store_songs(conn) if song_id == track]
    return (hits[0][1] if hits else None), list(dict.fromkeys(p for p, _ in hits))


def library_items(track: str, isrc: str | None, db_path: Path = LIBRARY_DB) -> list[int]:
    """The beets items holding Spotify track *track* or ISRC *isrc*, read-only."""
    if not db_path.is_file():
        return []
    found: set[int] = set()
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        for item_id, ids in conn.execute("SELECT entity_id, value FROM item_attributes "
                                         "WHERE key = 'spotify_ids' AND value LIKE ?", (f"%{track}%",)):
            if track in ids.split(","):
                found.add(item_id)
        for item_id, url in conn.execute("SELECT entity_id, value FROM item_attributes "
                                         "WHERE key = 'spotify_url' AND value LIKE ?", (f"%{track}%",)):
            if track_id(url) == track:
                found.add(item_id)
        if isrc:
            for item_id, codes in conn.execute("SELECT id, isrc FROM items WHERE isrc LIKE ?", (f"%{isrc}%",)):
                if isrc in (codes or "").split(";"):
                    found.add(item_id)
    return sorted(found)
