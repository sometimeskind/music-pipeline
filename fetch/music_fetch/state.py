"""The pipeline's own state in the ``music`` database (homelab#2410).

Every store that was a JSON dot file in ``inbox/spotdl/`` is read and written
here: the album store (``.albums.json``), the playlist stores (``.store/``),
the spotdl backoff (``.spotdl-failures.json``) and the small keyed documents
(:data:`KV_NAMES`: the MusicBrainz release and ISRC caches, the playlist-page
cache, the Spotify rate-limit stamp, the empty-slots report).  The tables are
migration 4 in :mod:`music_fetch.db`.

The album store is keyed by MusicBrainz release group (homelab#2478, decision
2).  An album whose release group is not known yet has none, and its
``unresolved`` flag says why: ``pending`` (not looked up), ``no-match``,
``conflict`` (the evidence disagrees) or ``in-flight`` (a merge waits for an
import to finish).  Every Spotify album ID of an album, and a local album's
``artist/name``, is a ref to its row: two Spotify editions of one release group
are one row with two refs, and an override or hold keyed by a Spotify album ID
still finds it.

In memory an album record is the dict album mode always used, plus:

- ``release_group`` and ``unresolved``;
- ``spotify_album_ids``: its Spotify album IDs, the primary first (the one its
  fallback tracklist and events use);
- ``names``: its ``artist/name`` refs, for an album with no Spotify ID;
- ``_id``: its row, absent until it is first saved.

:func:`album_key` is the record's key in :attr:`AlbumRows.albums`: its release
group, else its primary Spotify album ID, else its name.  It names the album,
the row ``id`` identifies it, so a resolved album changes key without moving.

Postgres down: :func:`connect` raises :class:`Unavailable`, and the caller
skips its run cleanly rather than run without its state (homelab#2410).
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from music_fetch import db

logger = logging.getLogger(__name__)

# Serialises album saves; the migration lock is db._MIGRATION_LOCK.
_SAVE_LOCK = 2410

# Why an album row has no release group (the albums_unresolved_check CHECK).
PENDING, NO_MATCH, CONFLICT, IN_FLIGHT = "pending", "no-match", "conflict", "in-flight"
UNRESOLVED = (PENDING, NO_MATCH, CONFLICT, IN_FLIGHT)
# A record's origin when it has none in memory: an album playlist's.
ALBUM = "album"
SPOTIFY, NAME = "spotify", "name"
# The record fields that are columns or refs, not part of ``data``.
_COLUMNS = ("_id", "status", "origin", "release_group", "unresolved", "spotify_album_ids", "names")

# The kv documents: name → what it held as a dot file.
MB_RELEASES = "mb-releases"          # .mb-releases.json, keyed by Spotify album ID
CANON_ISRC = "canon-isrc"            # .canon-isrc.json, keyed by ISRC
CANON_PAGES = "canon-pages"          # .canon-pages.json, one document
SPOTIFY_LIMIT = "spotify-rate-limit"  # .spotify-rate-limit.json, one document
EMPTY_SLOTS = "empty-slots"          # .empty-slots.json, one document
KV_NAMES = (MB_RELEASES, CANON_ISRC, CANON_PAGES, SPOTIFY_LIMIT, EMPTY_SLOTS)


class Unavailable(RuntimeError):
    """The ``music`` database is not configured or not reachable."""


def connect(url: str | None = None) -> psycopg.Connection:
    """A migrated connection to the state database, or :class:`Unavailable`."""
    if url is None and not db.configured():
        raise Unavailable(f"{db.ENV} is unset")
    try:
        return db.connect(url)
    except psycopg.Error as exc:
        raise Unavailable(f"the music database is unreachable: {exc}") from exc


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Albums
# ---------------------------------------------------------------------------


def album_key(record: Mapping) -> str:
    """The record's key: its release group, else its primary Spotify album ID, else its name."""
    key = record.get("release_group") or next(iter(record.get("spotify_album_ids") or ()), None) \
        or next(iter(record.get("names") or ()), None)
    if not key:
        raise ValueError("an album record needs a release group, a Spotify album ID or a name")
    return key


@dataclasses.dataclass
class AlbumRows:
    """The album store as album mode's ``State`` holds it: the album
    playlists' snapshots, the records by :func:`album_key`, the indexer
    budget's timestamps and the keys of the mixed albums.  *snapshot* is what
    each row held when loaded or last saved, so a save writes only the rows
    that changed."""

    playlists: dict[str, dict] = dataclasses.field(default_factory=dict)
    albums: dict[str, dict] = dataclasses.field(default_factory=dict)
    indexer: list[dict] = dataclasses.field(default_factory=list)
    mixed: list[str] = dataclasses.field(default_factory=list)
    snapshot: dict[int, str] = dataclasses.field(default_factory=dict)

    def by_ref(self) -> dict[str, str]:
        """Every Spotify album ID and name ref → its record's key."""
        out: dict[str, str] = {}
        for key, record in self.albums.items():
            for ref in (*(record.get("spotify_album_ids") or ()), *(record.get("names") or ())):
                out[ref] = key
        return out


def _fingerprint(record: Mapping, mixed: bool) -> str:
    return _dumps({**{k: v for k, v in record.items() if k != "_id"}, "_mixed": mixed})


def load_albums(conn: psycopg.Connection) -> AlbumRows:
    rows = AlbumRows()
    refs: dict[int, list[tuple[str, str]]] = {}
    for album_id, ref, kind in conn.execute("SELECT album_id, ref, kind FROM album_refs ORDER BY album_id, ref"):
        refs.setdefault(album_id, []).append((ref, kind))
    for album_id, group, unresolved, primary, status, origin, mixed, data in conn.execute(
            "SELECT id, release_group::text, unresolved, spotify_album_id, status, origin, mixed, data "
            "FROM albums ORDER BY id"):
        spotify = [r for r, k in refs.get(album_id, []) if k == SPOTIFY]
        if primary in spotify:
            spotify = [primary, *(r for r in spotify if r != primary)]
        record = {**data, "status": status, "release_group": group, "unresolved": unresolved,
                  "spotify_album_ids": spotify, "names": [r for r, k in refs.get(album_id, []) if k == NAME]}
        if origin != ALBUM:
            record["origin"] = origin
        key = album_key(record)
        rows.albums[key] = record
        if mixed:
            rows.mixed.append(key)
        rows.snapshot[album_id] = _fingerprint(record, mixed)
        record["_id"] = album_id
    rows.playlists = {name: data for name, data in conn.execute(
        "SELECT playlist, data FROM album_playlists ORDER BY playlist")}
    rows.indexer = [{"t": at.astimezone(timezone.utc).isoformat(timespec="seconds"), "kind": kind} for at, kind in conn.execute(
        "SELECT at, kind FROM indexer_use ORDER BY at")]
    return rows


def _row(record: Mapping, mixed: bool) -> dict:
    group = record.get("release_group") or None
    spotify = list(record.get("spotify_album_ids") or ())
    return {
        "release_group": group,
        "unresolved": None if group else (record.get("unresolved") or PENDING),
        "spotify_album_id": spotify[0] if spotify else None,
        "status": record["status"],
        "origin": record.get("origin") or ALBUM,
        "mixed": mixed,
        "data": Jsonb({k: v for k, v in record.items() if k not in _COLUMNS}),
    }


def save_albums(conn: psycopg.Connection, rows: AlbumRows) -> int:
    """Write *rows* in one transaction: insert the new records (setting their
    ``_id``), update the changed ones, delete the ones loaded and since
    removed.  Rows another process added meanwhile are left alone.  Returns
    the count of rows written or deleted."""
    mixed = set(rows.mixed)
    changed: list[tuple[dict, bool]] = []
    for key, record in rows.albums.items():
        is_mixed = key in mixed
        if record.get("_id") is None or rows.snapshot.get(record["_id"]) != _fingerprint(record, is_mixed):
            changed.append((record, is_mixed))
    present = {r["_id"] for r in rows.albums.values() if r.get("_id") is not None}
    gone = [i for i in rows.snapshot if i not in present]
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SAVE_LOCK,))
        if gone:
            conn.execute("DELETE FROM albums WHERE id = ANY(%s)", (gone,))
        # A ref may move between two changed rows (a merge): free them all before re-adding any.
        updated = [r["_id"] for r, _ in changed if r.get("_id") is not None]
        if updated:
            conn.execute("DELETE FROM album_refs WHERE album_id = ANY(%s)", (updated,))
        for record, is_mixed in changed:
            values = _row(record, is_mixed)
            if record.get("_id") is None:
                row = conn.execute(
                    "INSERT INTO albums (release_group, unresolved, spotify_album_id, status, origin, mixed, data) "
                    "VALUES (%(release_group)s, %(unresolved)s, %(spotify_album_id)s, %(status)s, %(origin)s, "
                    "%(mixed)s, %(data)s) RETURNING id", values).fetchone()
                assert row is not None
                record["_id"] = row[0]
            else:
                conn.execute(
                    "UPDATE albums SET release_group = %(release_group)s, unresolved = %(unresolved)s, "
                    "spotify_album_id = %(spotify_album_id)s, status = %(status)s, origin = %(origin)s, "
                    "mixed = %(mixed)s, data = %(data)s, updated_at = now() WHERE id = %(id)s",
                    {**values, "id": record["_id"]})
        refs = [(ref, record["_id"], kind) for record, _ in changed
                for kind, field in ((SPOTIFY, "spotify_album_ids"), (NAME, "names"))
                for ref in dict.fromkeys(record.get(field) or ())]
        if refs:
            with conn.cursor() as cur:
                cur.executemany("INSERT INTO album_refs (ref, album_id, kind) VALUES (%s, %s, %s)", refs)
        conn.execute("DELETE FROM album_playlists")
        if rows.playlists:
            with conn.cursor() as cur:
                cur.executemany("INSERT INTO album_playlists (playlist, data) VALUES (%s, %s)",
                                [(name, Jsonb(data)) for name, data in rows.playlists.items()])
        conn.execute("DELETE FROM indexer_use")
        if rows.indexer:
            with conn.cursor() as cur:
                cur.executemany("INSERT INTO indexer_use (at, kind) VALUES (%s, %s)",
                                [(datetime.fromisoformat(e["t"]), e["kind"]) for e in rows.indexer])
    for i in gone:
        del rows.snapshot[i]
    for record, is_mixed in changed:
        rows.snapshot[record["_id"]] = _fingerprint(record, is_mixed)
    return len(changed) + len(gone)


# ---------------------------------------------------------------------------
# Playlist stores
# ---------------------------------------------------------------------------


def store_names(conn: psycopg.Connection) -> list[str]:
    return [name for (name,) in conn.execute("SELECT playlist FROM playlist_stores ORDER BY playlist")]


def load_store(conn: psycopg.Connection, playlist: str) -> dict | None:
    """Playlist *playlist*'s store as its JSON file held it, or None."""
    row = conn.execute("SELECT source, updated_at, entries FROM playlist_stores WHERE playlist = %s",
                       (playlist,)).fetchone()
    if row is None:
        return None
    source, updated, entries = row
    return {"playlist": playlist, "source": source, "updated": _iso(updated), "entries": entries}


def save_store(conn: psycopg.Connection, playlist: str, entries: list[dict], source: str = "",
               updated: datetime | None = None) -> str:
    """Write *playlist*'s store; returns its ``updated`` stamp."""
    updated = updated or datetime.now(timezone.utc).replace(microsecond=0)
    conn.execute(
        "INSERT INTO playlist_stores (playlist, source, updated_at, entries) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (playlist) DO UPDATE SET source = EXCLUDED.source, updated_at = EXCLUDED.updated_at, "
        "entries = EXCLUDED.entries", (playlist, source, updated, Jsonb(entries)))
    return _iso(updated)


def remove_store(conn: psycopg.Connection, playlist: str) -> None:
    conn.execute("DELETE FROM playlist_stores WHERE playlist = %s", (playlist,))


# ---------------------------------------------------------------------------
# spotdl backoff
# ---------------------------------------------------------------------------


def load_failures(conn: psycopg.Connection) -> dict[str, dict]:
    return {url: entry for url, entry in conn.execute("SELECT url, entry FROM spotdl_failures ORDER BY url")}


def save_failures(conn: psycopg.Connection, failures: Mapping[str, dict]) -> None:
    """Replace the backoff with *failures* (URL → entry)."""
    with conn.transaction():
        conn.execute("DELETE FROM spotdl_failures")
        if failures:
            with conn.cursor() as cur:
                cur.executemany("INSERT INTO spotdl_failures (url, entry) VALUES (%s, %s)",
                                [(url, Jsonb(entry)) for url, entry in failures.items()])


# ---------------------------------------------------------------------------
# Keyed documents
# ---------------------------------------------------------------------------


def _check(name: str) -> None:
    if name not in KV_NAMES:
        raise ValueError(f"unknown kv document {name!r}")


def get(conn: psycopg.Connection, name: str, key: str = "") -> Any:
    """Document *name*'s value at *key* (``""`` for a one-document name), or None."""
    _check(name)
    row = conn.execute("SELECT value FROM kv WHERE name = %s AND key = %s", (name, key)).fetchone()
    return None if row is None else row[0]


def get_all(conn: psycopg.Connection, name: str) -> dict[str, Any]:
    _check(name)
    return {key: value for key, value in conn.execute(
        "SELECT key, value FROM kv WHERE name = %s ORDER BY key", (name,))}


def put(conn: psycopg.Connection, name: str, value: Any, key: str = "") -> None:
    put_many(conn, name, {key: value})


def put_many(conn: psycopg.Connection, name: str, values: Mapping[str, Any]) -> None:
    """Upsert *values* (key → value) under *name*; other keys stay."""
    _check(name)
    if not values:
        return
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO kv (name, key, value) VALUES (%s, %s, %s) "
            "ON CONFLICT (name, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            [(name, key, Jsonb(value)) for key, value in values.items()])


def delete(conn: psycopg.Connection, name: str, keys: Iterable[str] = ("",)) -> None:
    _check(name)
    conn.execute("DELETE FROM kv WHERE name = %s AND key = ANY(%s)", (name, list(keys)))
