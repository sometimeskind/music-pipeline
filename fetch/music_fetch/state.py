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

:func:`album_key` is the record's key in :attr:`AlbumRows.albums`, in the
Music MCP and in the ``album_record`` beets stamps on its imported items.  Once
``music-album-rekey --apply`` has moved every stamp (homelab#2478, the
:data:`ALBUM_KEYS` document) it is the album's row ``id``: opaque, and the
same whether or not the release group is known yet, so an album that resolves
keeps its key.  Before that it is the scheme album mode always used: the
primary Spotify album ID, else the release group, else the name.  A record not
saved yet has no row: its key is the old scheme's until :func:`save_albums`
gives it one.  Never read a key's format for what it is; a Spotify album ID is
:func:`primary_spotify` or one of ``spotify_album_ids``.

A deleted row (a merge's loser, an album that left its playlists) leaves its
refs in ``album_rows_gone``, so an item still stamped with its id finds the
record that holds one of them now (:func:`gone_rows`, ``music_scan.restamp``).

Which copy is the state: the JSON files until ``music-state-import --apply``
has written the database, then the database.  The switch is the dot file
:data:`SWITCH`, which the import writes after its transaction commits, so a
process that has not been switched never needs Postgres, and the previous
image (the rollback) never sees it.  :func:`session` yields None while the JSON
is the state.

Postgres down once switched: :func:`connect` raises :class:`Unavailable`, and
the caller skips its run cleanly rather than run without its state, or on the
stale JSON (homelab#2410).
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
import re
from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from music_fetch import db

logger = logging.getLogger(__name__)

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
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
# Written by music-state-import --apply with the state it imports: from then on
# the pipeline reads and writes the database, and the JSON is the rollback copy.
IMPORTED = "state-imported"
# Written by music-overrides import --apply (homelab#2484): from then on the
# overrides, holds and pins are the tables', and the ConfigMap's files are ignored.
OVERRIDES_IMPORTED = "overrides-imported"
# Written by music-album-rekey --apply (homelab#2478) before it restamps the
# items: from then on an album's key is its row id.  Deleted by --revert.
ALBUM_KEYS = "album-keys"
KV_NAMES = (MB_RELEASES, CANON_ISRC, CANON_PAGES, SPOTIFY_LIMIT, EMPTY_SLOTS, IMPORTED, OVERRIDES_IMPORTED,
            ALBUM_KEYS)


# Present once music-state-import --apply has moved the state into the
# database.  A dot file beside the JSON it replaces (pipeline-state-files-in-dot-dirs).
SWITCH = Path("/root/Music/inbox/spotdl/.state-in-db")


class Unavailable(RuntimeError):
    """The ``music`` database is not configured or not reachable."""


def in_db() -> bool:
    """Whether the database, not the JSON files, holds the state.  An unreadable
    path reads as not switched (``os.path.exists``): the tests run as a user who
    can't see /root, and the pod runs as root, which owns pipeline-state."""
    return os.path.exists(SWITCH)


def check() -> None:
    """Raise :class:`Unavailable` when the state is in the database and the
    database is down: a run that needs the state skips (homelab#2410)."""
    with session():
        pass


@contextlib.contextmanager
def session() -> Iterator[psycopg.Connection | None]:
    """A connection to the state database once :func:`in_db`, else None (the
    JSON files are the state).  Raises :class:`Unavailable` when switched and
    the database is down."""
    if not in_db():
        yield None
        return
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


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


def album_key(record: Mapping, by_row: bool = False) -> str:
    """The record's key: *by_row*, its row id once it has one; else its primary
    Spotify album ID, else its release group, else its name."""
    if by_row and record.get("_id") is not None:
        return str(record["_id"])
    key = primary_spotify(record) or record.get("release_group") or next(iter(record.get("names") or ()), None)
    if not key:
        raise ValueError("an album record needs a release group, a Spotify album ID or a name")
    return key


def primary_spotify(record: Mapping) -> str | None:
    """The record's primary Spotify album ID (its fallback tracklist's, its
    events'), or None for a local album."""
    return next(iter(record.get("spotify_album_ids") or ()), None)


@dataclasses.dataclass
class AlbumRows:
    """The album store as album mode's ``State`` holds it: the album
    playlists' snapshots, the records by :func:`album_key`, the indexer
    budget's timestamps and the keys of the mixed albums.  *snapshot* is what
    each row held when loaded or last saved, so a save writes only the rows
    that changed.  *by_row*: the keys are row ids (:data:`ALBUM_KEYS`)."""

    playlists: dict[str, dict] = dataclasses.field(default_factory=dict)
    albums: dict[str, dict] = dataclasses.field(default_factory=dict)
    indexer: list[dict] = dataclasses.field(default_factory=list)
    mixed: list[str] = dataclasses.field(default_factory=list)
    snapshot: dict[int, str] = dataclasses.field(default_factory=dict)
    by_row: bool = False

    def key(self, record: Mapping) -> str:
        return album_key(record, self.by_row)

    def rekey(self) -> None:
        """Key every record by :meth:`key` again: a saved record gets its row id."""
        rename = {k: self.key(r) for k, r in self.albums.items()}
        if any(k != v for k, v in rename.items()):
            self.albums = {rename[k]: r for k, r in self.albums.items()}
            self.mixed = list(dict.fromkeys(rename.get(k, k) for k in self.mixed))

    def by_ref(self) -> dict[str, str]:
        """Every Spotify album ID and name ref → its record's key."""
        out: dict[str, str] = {}
        for key, record in self.albums.items():
            for ref in (*(record.get("spotify_album_ids") or ()), *(record.get("names") or ())):
                out[ref] = key
        return out


def is_spotify_album(key: str | None) -> bool:
    """Whether album key *key* is a Spotify album ID: not a local album's
    ``artist/name``, nor a release group's UUID."""
    return bool(key) and "/" not in key and not _UUID.fullmatch(key)


def new_record(key: str, **fields: Any) -> dict:
    """A new album record whose key is *key*: a Spotify album ID, or a local album's ``artist/name``."""
    spotify = is_spotify_album(key)
    return {**fields, "release_group": None, "unresolved": PENDING,
            "spotify_album_ids": [key] if spotify else [], "names": [] if spotify else [key]}


# The fields album mode's JSON never held: written to the database, stripped from the JSON.
DB_FIELDS = ("_id", "release_group", "unresolved", "spotify_album_ids", "names")


def rows_from_json(data: Mapping) -> AlbumRows:
    """``.albums.json``'s content as album rows: each record unresolved, its key its Spotify album ID (or name)."""
    albums = {key: {**record, **new_record(key)} for key, record in (data.get("albums") or {}).items()}
    return AlbumRows(playlists=dict(data.get("playlists") or {}), albums=albums,
                     indexer=list(data.get("indexer") or []), mixed=list(data.get("mixed") or []))


def rows_to_json(rows: AlbumRows) -> dict:
    """*rows* as ``.albums.json`` held them, for the previous image (the rollback) to read."""
    return {"playlists": rows.playlists,
            "albums": {key: {k: v for k, v in record.items() if k not in DB_FIELDS}
                       for key, record in rows.albums.items()},
            "indexer": rows.indexer, "mixed": rows.mixed}


def _fingerprint(record: Mapping, mixed: bool) -> str:
    return _dumps({**{k: v for k, v in record.items() if k != "_id"}, "_mixed": mixed})


def keyed_by_row(conn: psycopg.Connection) -> bool:
    """Whether album keys are row ids: ``music-album-rekey --apply`` has run (homelab#2478)."""
    return get(conn, ALBUM_KEYS) is not None


def load_albums(conn: psycopg.Connection) -> AlbumRows:
    rows = AlbumRows(by_row=keyed_by_row(conn))
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
        rows.snapshot[album_id] = _fingerprint(record, mixed)
        record["_id"] = album_id
        key = rows.key(record)
        rows.albums[key] = record
        if mixed:
            rows.mixed.append(key)
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
    the count of rows written or deleted.  A deleted row's refs go to
    ``album_rows_gone``; keyed by row (*rows.by_row*), the new records take
    their row ids as keys afterwards."""
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
            # Before the delete cascades their refs away: an item stamped with a
            # gone row's id finds whichever row holds one of them later.
            conn.execute(f"INSERT INTO album_rows_gone (id, refs) SELECT id, refs FROM ({_ROW_REFS}) r (id, refs) "
                         "ON CONFLICT (id) DO UPDATE SET refs = EXCLUDED.refs, gone_at = now()", (gone,))
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
        attach_overrides(conn, [r["_id"] for r, _ in changed])
    for i in gone:
        del rows.snapshot[i]
    for record, is_mixed in changed:
        rows.snapshot[record["_id"]] = _fingerprint(record, is_mixed)
    rows.rekey()
    return len(changed) + len(gone)


def gone_rows(conn: psycopg.Connection) -> dict[str, list[str]]:
    """Every deleted album row's id → the refs (Spotify album IDs, names,
    release group) it had when it went."""
    return {str(i): list(refs) for i, refs in conn.execute("SELECT id, refs FROM album_rows_gone ORDER BY id")}


_ROW_REFS = """
SELECT a.id, array_remove(array_agg(r.ref ORDER BY r.ref) || a.release_group::text, NULL)
  FROM albums a LEFT JOIN album_refs r ON r.album_id = a.id
 WHERE a.id = ANY(%s) GROUP BY a.id
"""


def attach_overrides(conn: psycopg.Connection, changed: Iterable[int] = ()) -> None:
    """Keep the overrides and holds (homelab#2484) on their albums' rows, in
    the caller's transaction: one whose row is gone (its ``album_id`` set
    null by the delete) moves to the row that now carries one of its refs, a
    merge's survivor or the album back on a playlist, unless that row has one
    of its kind already (then it stays detached, and ``list`` shows it); then
    the refs of the *changed* rows' overrides are brought up to date."""
    detached = conn.execute("SELECT id, kind, refs FROM album_overrides WHERE album_id IS NULL ORDER BY id").fetchall()
    moved: list[int] = []
    if detached:
        wanted = sorted({ref for _, _, refs in detached for ref in refs})
        owner = dict(conn.execute("SELECT ref, album_id FROM album_refs WHERE ref = ANY(%s)", (wanted,)).fetchall())
        owner.update(conn.execute("SELECT release_group::text, id FROM albums WHERE release_group::text = ANY(%s)",
                                  (wanted,)).fetchall())
        taken = set(conn.execute("SELECT album_id, kind FROM album_overrides WHERE album_id IS NOT NULL").fetchall())
        for override_id, kind, refs in detached:
            album_id = next((owner[r] for r in refs if r in owner), None)
            if album_id is None:
                continue
            if (album_id, kind) in taken:
                logger.warning("[OVERRIDE] the %s override %d (%s) names album row %d, which has one already; "
                               "left detached", kind, override_id, ", ".join(refs), album_id)
                continue
            conn.execute("UPDATE album_overrides SET album_id = %s WHERE id = %s", (album_id, override_id))
            taken.add((album_id, kind))
            moved.append(album_id)
            logger.info("[OVERRIDE] the %s override %d (%s) moved to album row %d", kind, override_id,
                        ", ".join(refs), album_id)
    ids = sorted({*changed, *moved})
    if ids:
        conn.execute(f"UPDATE album_overrides o SET refs = r.refs FROM ({_ROW_REFS}) r (id, refs) "
                     "WHERE o.album_id = r.id AND o.refs <> r.refs", (ids,))


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


def load_stores(conn: psycopg.Connection) -> dict[str, dict]:
    """Every playlist's store, by name."""
    return {playlist: {"playlist": playlist, "source": source, "updated": _iso(updated), "entries": entries}
            for playlist, source, updated, entries in conn.execute(
                "SELECT playlist, source, updated_at, entries FROM playlist_stores ORDER BY playlist")}


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


class KeyedCache:
    """A keyed kv document (*name*, e.g. :data:`MB_RELEASES`) as a dict:
    :meth:`load` reads it from the database once the state is there, else
    from its JSON file *path*; :meth:`save` writes back the keys that changed
    (the whole file, before the switch).  A cache is best-effort: a database
    that is down reads as empty and skips the save, with a warning."""

    def __init__(self, name: str, path: Path) -> None:
        _check(name)
        self.name = name
        self.path = path
        self._saved: dict[str, str] = {}

    def load(self) -> dict[str, Any]:
        try:
            with session() as conn:
                if conn is not None:
                    data = get_all(conn, self.name)
                    self._saved = {k: _dumps(v) for k, v in data.items()}
                    return data
        except Unavailable as exc:
            logger.warning("Ignoring the %s cache: %s", self.name, exc)
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            logger.warning("Ignoring unreadable %s cache %s", self.name, self.path)
            return {}

    def save(self, data: Mapping[str, Any]) -> None:
        try:
            with session() as conn:
                if conn is not None:
                    dumped = {k: _dumps(v) for k, v in data.items()}
                    put_many(conn, self.name, {k: data[k] for k, d in dumped.items() if self._saved.get(k) != d})
                    self._saved = dumped
                    return
        except (Unavailable, psycopg.Error) as exc:
            logger.warning("Could not save the %s cache: %s", self.name, exc)
            return
        try:
            self.path.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not save the %s cache %s: %s", self.name, self.path, exc)


def load_doc(name: str, path: Path) -> Any:
    """One-document kv *name* (``.canon-pages``, ``.empty-slots``): the
    database's once the state is there, else *path*'s JSON; None when absent
    or unreadable.  Raises :class:`Unavailable` when switched and down."""
    with session() as conn:
        if conn is not None:
            return get(conn, name)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        logger.warning("Ignoring unreadable %s", path)
        return None


def save_doc(name: str, path: Path, value: Any) -> None:
    """Write one-document kv *name*: to the database once the state is there,
    else to *path* through a temporary file.  Raises :class:`Unavailable` or
    OSError on failure."""
    with session() as conn:
        if conn is not None:
            put(conn, name, value)
            return
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
