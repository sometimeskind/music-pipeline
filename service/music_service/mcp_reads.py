"""The MCP's structured reads (homelab#2412): beets, the album store, the playlist stores, quarantine.

beets' SQLite is opened read-only with a 10 s busy timeout, never through
beets' ``Library`` (which opens it for writing); a lock that outlasts the
timeout raises :class:`LibraryBusy`.  An item is a dict of its fixed columns
and flexible attributes, ``path`` relative to the library root.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

BUSY_TIMEOUT_SECONDS = 10
PAGE_ENTRIES = 200
PAGE_BYTES = 32 * 1024
QUARANTINE_KINDS = ("unmatched", "rejected", "replaced")


class LibraryBusy(Exception):
    """beets' SQLite stayed locked past the busy timeout."""


class Item(dict):
    """A library item as a dict; ``.id`` and ``.get`` as the identity helpers expect."""

    @property
    def id(self) -> int:
        return self["id"]


def utc(ts) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(value):
    if isinstance(value, (bytes, memoryview)):
        return os.fsdecode(bytes(value))
    return value


class Library:
    """Read-only access to beets' SQLite."""

    def __init__(self, db: Path, directory: Path) -> None:
        self.db = db
        self.directory = directory

    def __enter__(self) -> "Library":
        uri = f"file:{quote(str(self.db))}?mode=ro"
        try:
            self.conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_SECONDS)
            self.columns = [r[1] for r in self.conn.execute("PRAGMA table_info(items)")]
        except sqlite3.OperationalError as exc:
            raise _busy_or(exc) from exc
        return self

    def __exit__(self, *_) -> None:
        self.conn.close()

    def _q(self, sql: str, params=()) -> list:
        try:
            return self.conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            raise _busy_or(exc) from exc

    def count(self) -> int:
        return self._q("SELECT COUNT(*) FROM items")[0][0]

    def attr(self, key: str) -> dict[int, object]:
        """*key* per item ID, fixed column or flexible attribute, for the items that have it."""
        if key in self.columns:
            rows = self._q(f'SELECT id, "{key}" FROM items')  # noqa: S608 — key is a known column
        else:
            rows = self._q("SELECT entity_id, value FROM item_attributes WHERE key = ?", (key,))
        return {i: _text(v) for i, v in rows if v not in (None, "", b"")}

    def ids_where(self, key: str, value: str) -> list[int]:
        return sorted(i for i, v in self.attr(key).items() if v == value)

    def items(self, ids: list[int]) -> list[Item]:
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = self._q(f"SELECT * FROM items WHERE id IN ({marks}) ORDER BY id", ids)  # noqa: S608
        out = {item["id"]: item for item in (Item(zip(self.columns, map(_text, r))) for r in rows)}
        for entity, key, value in self._q(
                f"SELECT entity_id, key, value FROM item_attributes WHERE entity_id IN ({marks})", ids):  # noqa: S608
            if entity in out:
                out[entity][key] = _text(value)
        for item in out.values():
            item["path"] = self.relative(item.get("path"))
        return [out[i] for i in ids if i in out]

    def relative(self, path) -> str | None:
        if not path:
            return None
        p = Path(path)
        try:
            return str(p.relative_to(self.directory)) if p.is_absolute() else str(p)
        except ValueError:
            return str(p)

    def identities(self, keys: tuple[str, ...] = ("spotify_ids", "spotify_url", "isrc")) -> list[Item]:
        """Every item that has one of *keys* (by default the attributes a playlist
        entry resolves by), with just those."""
        values = {k: self.attr(k) for k in keys}
        ids = {i for v in values.values() for i in v}
        return [Item({"id": i, **{k: values[k].get(i) for k in keys}}) for i in sorted(ids)]


def _busy_or(exc: sqlite3.OperationalError) -> Exception:
    text = str(exc).lower()
    return LibraryBusy(str(exc)) if "locked" in text or "busy" in text else exc


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------

_TOP = ("id", "title", "artist", "albumartist", "album", "track", "length", "bitrate", "format", "path")


def item_view(item: Item) -> dict:
    out = {k: item.get(k) for k in _TOP}
    out["added"] = utc(item.get("added"))
    out["fields"] = {k: v for k, v in item.items() if k not in _TOP and k != "added"}
    return out


def item_summary(item: Item) -> dict:
    return {k: item.get(k) for k in ("id", "title", "artist", "track", "path", "via")}


def page(entries: list, offset: int) -> tuple[list, int | None]:
    """At most :data:`PAGE_ENTRIES` entries and about :data:`PAGE_BYTES` of JSON from *offset*."""
    out, size, i = [], 0, offset
    while i < len(entries) and len(out) < PAGE_ENTRIES:
        n = len(json.dumps(entries[i], ensure_ascii=False).encode())
        if out and size + n > PAGE_BYTES:
            break
        out.append(entries[i])
        size += n
        i += 1
    return out, (i if i < len(entries) else None)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def find_albums(lib: Library, records: dict[str, dict], query: str) -> list[dict]:
    from music_fetch.albums import find_albums as find  # noqa: PLC0415

    album_ids = lib.attr("spotify_album_id")
    counts: dict[str, int] = {}
    for key in album_ids.values():
        counts[key] = counts.get(key, 0) + 1
    return [{"key": k, "artist": r.get("artist"), "name": r.get("name"), "status": r.get("status"),
             "playlists": sorted(r.get("playlists", {})), "items": counts.get(k, 0)}
            for k, r in find(records, query)]


def get_album(lib: Library, key: str, record: dict) -> dict:
    items = lib.items(lib.ids_where("spotify_album_id", key))
    return {"album": {"key": key, "spotify_id": key, "record": record},
            "beets": {"album_ids": sorted({i["album_id"] for i in items if i.get("album_id")}),
                      "item_ids": [i["id"] for i in items]},
            "items": [item_summary(i) for i in items]}


def resolver(lib: Library):
    """``(song_id, isrc) -> item ID``: the lowest item ID holding that recording."""
    from music_scan.identity import item_isrcs, item_spotify_ids  # noqa: PLC0415

    by_sid: dict[str, int] = {}
    by_isrc: dict[str, int] = {}
    for item in lib.identities():
        for sid in item_spotify_ids(item):
            by_sid.setdefault(sid, item.id)
        for code in item_isrcs(item):
            by_isrc.setdefault(code, item.id)

    def resolve(sid: str | None, isrc: str | None) -> int | None:
        return (by_sid.get(sid) if sid else None) or (by_isrc.get(isrc) if isrc else None)

    return resolve


def playlist_entries(lib: Library, playlist) -> list[dict]:
    from music_scan.identity import spotify_id  # noqa: PLC0415

    resolve = resolver(lib)
    out = []
    for n, entry in enumerate(playlist.entries, 1):
        if song := entry.get("song"):
            sid = song.get("song_id") or spotify_id(song.get("url"))
            artists = song.get("artists") or []
            out.append({"position": n, "kind": "song", "title": song.get("name"),
                        "artist": ", ".join(artists) if isinstance(artists, list) else artists,
                        "album": song.get("album_name"), "song_id": sid, "isrc": song.get("isrc") or None,
                        "item_id": resolve(sid, song.get("isrc"))})
        elif local := entry.get("local"):
            out.append({"position": n, "kind": "local", "title": local.get("title"), "artist": local.get("artist"),
                        "item_id": local.get("id")})
    return out


def quarantine_files(root: Path, kind: str, query: str | None) -> list[dict]:
    q = (query or "").lower()
    out = []
    if not root.exists():
        return out
    for f in sorted(root.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(root)
        top = rel.parts[0] if len(rel.parts) > 1 else ""
        if (kind == "unmatched" and top in ("rejected", "replaced")) or (kind != "unmatched" and top != kind):
            continue
        if q and q not in str(rel).lower():
            continue
        stat = f.stat()
        out.append({"path": str(rel), "size": stat.st_size, "modified": utc(stat.st_mtime)})
    return out


def quarantine_counts(root: Path) -> dict[str, int]:
    counts = dict.fromkeys(QUARANTINE_KINDS, 0)
    if root.exists():
        for f in root.rglob("*"):
            if f.is_file():
                rel = f.relative_to(root)
                top = rel.parts[0] if len(rel.parts) > 1 else ""
                counts[top if top in ("rejected", "replaced") else "unmatched"] += 1
    return counts
