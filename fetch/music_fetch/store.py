"""The playlist store (homelab#1914): one file per playlist, the single
source of truth for its entries.

A store holds the playlist's full entry list in Spotify order, each entry with
the full Spotify song record (the dict spotdl's ``Song.json`` gives, which
``canon`` needs for album data), then its :data:`LOCAL` entries: library
items on the playlist that are not on its Spotify list.  Whether an entry is
downloaded is never recorded: it is the entry resolving to a library item by
Spotify ID or ISRC, recomputed every time.  Spotify decides membership and
order, and the store keeps the richest record known for each entry.

The nightly writes a spotdl playlist's store from its Spotify fetch and
downloads its want list; the album tick writes an ``album`` playlist's; a
``nosync`` playlist's is built from Spotify once and kept.  A track removed on
Spotify is gone from the next store, and so from the ``.m3u``: nothing is
unlinked.  The beets ``sources`` field records where an item came from and is
never read as membership.

``.spotdl`` files, the unlink queue and ``reconcile`` were retired in stage c.

Once the state is in the music database (homelab#2410, :func:`state.in_db`),
the stores are rows of ``playlist_stores`` and the files in :data:`STORE_DIR`
are left as the rollback copy; :func:`load`, :func:`names`, :func:`remove`,
:func:`songs_by_playlist` and :func:`save` go to whichever holds the state.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from music_fetch import state

logger = logging.getLogger(__name__)

SPOTDL_DIR = Path("/root/Music/inbox/spotdl")
# On pipeline-state, in the inbox.  A dot directory: beets' import ignores it,
# and it holds no audio for the inbox sweep to move.
STORE_DIR = SPOTDL_DIR / ".store"
VERSION = 1
# Store.source of a store built from a Spotify fetch: a ``nosync`` playlist's
# store is fetched once and kept from then on.
SPOTIFY = "spotify"
# The key of a local-only entry: ``{"local": {"id", "path", "title", "artist"}}``,
# a library item on the playlist that is not on its Spotify list (stage b).
LOCAL = "local"


def path(name: str) -> Path:
    return STORE_DIR / f"{name}.json"


def load(name: str) -> "Store | None":
    """Playlist *name*'s store, or None."""
    with state.session() as conn:
        if conn is None:
            return Store.load(path(name))
        data = state.load_store(conn, name)
    return None if data is None else Store.from_data(data, name)


def names() -> list[str]:
    """The playlists that have a store, sorted."""
    with state.session() as conn:
        if conn is None:
            return sorted(p.stem for p in STORE_DIR.glob("*.json"))
        return state.store_names(conn)


def remove(name: str) -> None:
    """Delete playlist *name*'s store: the playlist left ``playlists.conf``."""
    with state.session() as conn:
        if conn is None:
            path(name).unlink(missing_ok=True)
        else:
            state.remove_store(conn, name)


def save(playlist: "Store") -> None:
    """Write *playlist*'s store."""
    with state.session() as conn:
        if conn is None:
            playlist.save(path(playlist.name))
        else:
            playlist.updated = state.save_store(conn, playlist.name, playlist.entries, playlist.source)


def songs_by_playlist() -> dict[str, list[dict]]:
    """Every store's Spotify song records, by playlist."""
    with state.session() as conn:
        if conn is not None:
            return {name: Store.from_data(data, name).songs for name, data in state.load_stores(conn).items()}
    out: dict[str, list[dict]] = {}
    for name in names():
        playlist = load(name)
        if playlist is not None:
            out[name] = playlist.songs
    return out


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write(path: Path, text: str) -> None:
    """Write *text* to *path* through a temporary file, so a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


@dataclasses.dataclass
class Store:
    """One playlist's store: its entries in order, each ``{"song": <record>}``,
    then its :data:`LOCAL` entries.  *source* is :data:`SPOTIFY` when the
    entries came from a Spotify fetch."""

    name: str
    entries: list[dict]
    updated: str = ""
    source: str = ""

    @property
    def songs(self) -> list[dict]:
        return [e["song"] for e in self.entries if e.get("song")]

    @property
    def locals(self) -> list[dict]:
        return [e[LOCAL] for e in self.entries if e.get(LOCAL)]

    @classmethod
    def from_songs(cls, name: str, songs: list[dict], keep: "Store | None" = None) -> "Store":
        """A store of *songs*, followed by *keep*'s local entries: a rebuild
        from Spotify never drops a local-only track."""
        local = [] if keep is None else [e for e in keep.entries if e.get(LOCAL)]
        return cls(name, [{"song": song} for song in songs] + local)

    @classmethod
    def load(cls, path: Path) -> "Store | None":
        """The store at *path*, or None when it is absent or unreadable."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception:
            logger.warning("Could not read the playlist store %s", path, exc_info=True)
            return None
        return cls.from_data(data, path.stem)

    @classmethod
    def from_data(cls, data: dict, name: str) -> "Store":
        """A store from its file's (or row's) fields."""
        return cls(data.get("playlist") or name, data.get("entries") or [], data.get("updated") or "",
                   data.get("source") or "")

    def save(self, path: Path) -> None:
        self.updated = _now()
        _write(path, json.dumps(
            {"version": VERSION, "playlist": self.name, "updated": self.updated, "source": self.source,
             "entries": self.entries},
            indent=2, ensure_ascii=False,
        ))


def merge_spotify(spotify: list[dict], known: dict[str, dict]) -> list[dict]:
    """One-way merge: the store's songs are *spotify*'s, in its order (a
    duplicate entry stays), each the record in *known* (by URL) when there is
    one, else Spotify's page record.  *known* holds the richer records:
    a download's full metadata, an earlier store's entry."""
    return [known.get(song.get("url") or "") or song for song in spotify]


def want_list(
    songs: list[dict],
    resolves: Callable[[dict], bool],
    held: set[str] | frozenset[str] = frozenset(),
    failures: dict | None = None,
    now: datetime | None = None,
) -> list[str]:
    """The store's want list: the URLs of *songs*, in order and once each,
    that no library item holds by Spotify ID or ISRC (*resolves*), that are
    not *held* (a download in quarantine, never fetched again) and not backed
    off in *failures*.  The budget is spent on it, as on today's."""
    now = now or datetime.now(timezone.utc)
    failures = failures or {}
    wanted: list[str] = []
    seen: set[str] = set()
    for song in songs:
        url = song.get("url") or ""
        if not url or url in seen or url in held or resolves(song):
            continue
        seen.add(url)
        entry = failures.get(url)
        if entry and datetime.fromisoformat(entry["retry_after"]) > now:
            continue
        wanted.append(url)
    return wanted
