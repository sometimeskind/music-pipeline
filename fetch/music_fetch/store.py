"""The playlist store (homelab#1914 step 1): one file per playlist, the single
source of truth for its entries.

A store holds the playlist's full entry list in order, each entry with the
full Spotify song record (the dict a ``.spotdl`` file holds, which ``canon``
needs for album data).  Whether an entry is downloaded is never recorded: it
is the entry resolving to a library item by Spotify ID or ISRC, recomputed
every time.  Step 1 has one-way semantics: Spotify decides membership and
order, and the store keeps the richest record known for each entry.

``PLAYLIST_STORE=dry-run`` builds each playlist's store next to today's state
(``.spotdl``, ``sources``) and writes what the store would produce to
:data:`DRYRUN_DIR`: the store itself (``<playlist>.json``), its ``.m3u`` lines
(``<playlist>.m3u``) and its nightly want list (``<playlist>.want``), each with
a ``.diff`` against what today's path produced, and one ``[STORE-DIFF]`` log
line and ``history.tsv`` row per comparison.  Nothing reads the side
directory, so the dry run changes no playlist, download or tag.  ``off`` (the
default) does nothing.

``PLAYLIST_STORE=on`` (homelab#1914 stage a) does the same in
:data:`STORE_DIR`, and a spotdl or ``nosync`` playlist's ``.m3u`` is the
store's view, followed by its ``sources`` tail: the items tagged with the
playlist that no entry resolves, its local-only tracks.  The diffs keep
running against what today's path would have written.

Stage b moves the rest onto the store, with ``.spotdl`` and ``sources``
still written alongside: a tail item becomes a :data:`LOCAL` entry, kept at
the playlist's end; the nightly also downloads an entry no library item
holds that its ``.spotdl`` lists; and album mode writes each album
playlist's tracklist to its store.  Whether a spotdl copy is a top-up stays
with ``sources``; the store's answer is logged where it differs
(``[TOPUP-DIFF]``).  ``.spotdl``, the unlink queue and ``reconcile`` go in
stage c, and the top-up check once the diff lines are read.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

STORE_ENV = "PLAYLIST_STORE"
OFF, DRY_RUN, ON = "off", "dry-run", "on"
MODES = (OFF, DRY_RUN, ON)

SPOTDL_DIR = Path("/root/Music/inbox/spotdl")
# On pipeline-state, next to the .spotdl files.  Dot directories: beets'
# import ignores them, and they hold no audio for the inbox sweep to move.
DRYRUN_DIR = SPOTDL_DIR / ".store-dryrun"
STORE_DIR = SPOTDL_DIR / ".store"
VERSION = 1
# Store.source of a store built from a Spotify fetch.  A ``nosync`` playlist's
# .spotdl is not its whole Spotify list when it ran as a spotdl playlist before
# it was frozen (homelab#1914: wedding), so its store is fetched once instead.
SPOTIFY = "spotify"
# The key of a local-only entry: ``{"local": {"id", "path", "title", "artist"}}``,
# a library item on the playlist that is not on its Spotify list (stage b).
LOCAL = "local"


def mode() -> str:
    """``PLAYLIST_STORE``: ``off`` (default), ``dry-run`` or ``on``.  Anything
    else is logged and treated as ``off``: an unknown mode never writes."""
    value = (os.environ.get(STORE_ENV) or OFF).strip().lower()
    if value not in MODES:
        logger.error("%s=%r is not one of %s — treating it as %s", STORE_ENV, value, "|".join(MODES), OFF)
        return OFF
    return value


def dry_run() -> bool:
    return mode() == DRY_RUN


def active() -> bool:
    """The store is built: ``dry-run`` or ``on``."""
    return mode() != OFF


def store_dir() -> Path:
    """Where the stores and their views live: :data:`STORE_DIR` when ``on``,
    else :data:`DRYRUN_DIR`."""
    return STORE_DIR if mode() == ON else DRYRUN_DIR


def path(name: str) -> Path:
    return store_dir() / f"{name}.json"


def load(name: str) -> "Store | None":
    """Playlist *name*'s store, or None.  When ``on``, a store the dry run
    built and :data:`STORE_DIR` doesn't hold yet is read from :data:`DRYRUN_DIR`,
    so the switch keeps every store the dry run built (the next save moves it)."""
    playlist = Store.load(path(name))
    if playlist is None and mode() == ON:
        playlist = Store.load(DRYRUN_DIR / f"{name}.json")
    return playlist


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
        return cls(data.get("playlist") or path.stem, data.get("entries") or [], data.get("updated") or "",
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


@dataclasses.dataclass(frozen=True)
class Diff:
    added: list[str]
    removed: list[str]
    reordered: bool

    @property
    def same(self) -> bool:
        return not self.added and not self.removed and not self.reordered


def diff(today: list[str], store: list[str]) -> Diff:
    """What the store's lines add to and drop from *today*'s, and whether the
    lines both hold are in another order."""
    in_today, in_store = set(today), set(store)
    common_today = [line for line in today if line in in_store]
    common_store = [line for line in store if line in in_today]
    return Diff(
        added=[line for line in store if line not in in_today],
        removed=[line for line in today if line not in in_store],
        reordered=common_today != common_store,
    )


def write_view(name: str, kind: str, today: list[str], store: list[str], directory: Path | None = None) -> Diff:
    """Write the store's *kind* view of playlist *name* (``m3u`` or ``want``)
    to *directory* (:func:`store_dir`), with a ``.diff`` against *today*'s, log one
    ``[STORE-DIFF]`` line and append a ``history.tsv`` row."""
    directory = store_dir() if directory is None else directory
    d = diff(today, store)
    _write(directory / f"{name}.{kind}", "\n".join(store) + ("\n" if store else ""))
    lines = [f"+ {line}" for line in d.added] + [f"- {line}" for line in d.removed]
    if d.reordered:
        lines.append("~ the lines both hold are in another order")
    _write(directory / f"{name}.{kind}.diff", "\n".join(lines) + ("\n" if lines else ""))
    # INFO either way: differences are expected until the store's rules are settled.
    logger.info("[STORE-DIFF] %s %s: today %d | store %d | +%d -%d%s", name, kind, len(today), len(store),
        len(d.added), len(d.removed), " | order differs" if d.reordered else "")
    history = directory / "history.tsv"
    try:
        with history.open("a", encoding="utf-8") as fh:
            fh.write(f"{_now()}\t{name}\t{kind}\t{len(today)}\t{len(store)}\t{len(d.added)}\t{len(d.removed)}"
                     f"\t{int(d.reordered)}\n")
    except OSError:
        logger.warning("Could not append to %s", history, exc_info=True)
    return d
