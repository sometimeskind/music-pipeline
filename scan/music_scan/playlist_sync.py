"""The two-way playlist sync's merge core (homelab#2361): pure, no I/O.

One cycle for one playlist takes plain data and returns a :class:`Plan`: what
changed on each side since the last applied cycle, the writes each side needs
to match the merged store, and the breaker rules the writes would trip.  The
flow reads the inputs (the store, the snapshot, Navidrome, later Spotify) and
decides what to do with the plan; nothing here reads or writes anything.

Terms:

- **Store entries** (:class:`Entry`): the playlist store's entries in order,
  each with the library item it resolves to *now*.  The resolution is the
  store view's (:func:`music_scan.scan.store_view`: Spotify ID, then ISRC,
  never title words), so the core can't disagree with the ``.m3u``.  A local
  entry resolves to its own item while that item is in the library.
- **View**: the items the store's entries resolve to, deduped, in order: what
  the Navidrome playlist should hold.
- **Snapshot** (:class:`Held`): what Navidrome held at the end of the last
  applied cycle, as ``(item, song)`` pairs.  The song (Navidrome's media file
  ID) is kept beside the item so that a track that is still there but no
  longer maps to its item (its file is ``missing``, or the path maps to no
  item: a wrong mount mapping, a moved file) reads as *unmapped*, never as a
  delete.  A cycle that finds no snapshot takes one and reports nothing.
- **Navidrome deltas** are the snapshot against the current playlist; the
  **Spotify deltas** the current Spotify list against the store's Spotify
  entries (empty while Spotify isn't read: phase 2).

A snapshot item missing from the current Navidrome playlist is classified:

========================================================  ================
Current Navidrome state                                   Class
========================================================  ================
a track that isn't ``missing`` maps to it by path         ``present``
its song is there (missing, or mapping elsewhere/nowhere)  ``unmapped``
no track; the item left the library                        ``hidden``
no track; no store entry resolves to the item now          ``hidden``
no track; store entries resolve to it now                  ``local_delete``
========================================================  ================

A local delete removes every Spotify entry resolving to the item, each one a
Spotify write; a local entry of the item is dropped without one.  A present
track whose item is neither in the snapshot nor held by any store entry is a
**local add**: a promotion (a Spotify write) when its item has a Spotify ID or
ISRC, else a new local entry.  An item in the snapshot or the view is never an
add, so a track downloaded since the last cycle is absorbed, not reported.

Rules (each table-tested):

1. A conflict on one item in one cycle goes to Spotify's state: a local delete
   of an item Spotify just added is dropped (``conflict``), and the item goes
   back to Navidrome.  The other way round needs no rule: an item Spotify just
   removed was held by the store, so Navidrome holding it is never an add, and
   the removal reaches Navidrome as a write.  An add on both sides is one
   entry.
2. A new local entry (or promotion) sits after the store entry of the nearest
   Navidrome track before it that survives the merge, walking back past
   tracks deleted in the same cycle; at the head when there is none.  Existing
   local entries keep their place the same way across a Spotify reorder.
3. A Spotify entry that is null, unavailable or a Spotify local file
   (``playable=False``) is in no view, is never a local delete, and counts
   toward no rule.
4. Duplicate store entries both stay; a local delete of their item removes and
   counts both.
5. A Navidrome playlist reading empty while its snapshot isn't (or Spotify
   reading empty while the store holds Spotify entries) is the ``empty`` trip,
   not a mass delete: nothing else is reported.
6. A missing store is the ``no_store`` trip; a missing snapshot is taken, with
   nothing reported.  A take that reads Navidrome empty while the view isn't is
   the ``empty`` trip instead, so an empty snapshot is never frozen in.

The breaker (rules 5 and 6 plus two caps) guards Spotify writes and counts the
writes the merge produces, not the raw deltas: ``writes`` when more than
:data:`MAX_WRITES` are pending in the cycle (promotions included), ``deletes``
when the cycle's Spotify removals plus those applied in the last hour exceed
:data:`MAX_DELETES_PER_HOUR`.  In dry-run the flow reports every trip as a
would-trip and latches nothing.

Navidrome writes are computed against the current playlist: songs to remove
(a present track whose item left the view, and every copy of an item after
its first) and items to add (in the view, held by no track).  Missing and
unmapped tracks are never written.  Order is not synced (homelab#2361 phase 7).
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from collections.abc import Collection, Sequence

MAX_WRITES = 20
MAX_DELETES_PER_HOUR = 5

# Snapshot item classes.
PRESENT, UNMAPPED, HIDDEN, LOCAL_DELETE, CONFLICT = "present", "unmapped", "hidden", "local_delete", "conflict"
# Breaker rules.
NO_STORE, EMPTY, WRITES, DELETES = "no_store", "empty", "writes", "deletes"
# The key prefix of a promotion's placeholder entry, swapped for the Spotify
# record once the write returns.
PROMOTE = "promote:"
_LOCAL = "local:"


@dataclasses.dataclass(frozen=True)
class Entry:
    """A store entry, or a Spotify playlist entry: its :func:`music_fetch.store.entry_key`
    and the library item it resolves to now (None: not downloaded).  *local*
    for a local entry; *playable* False for a null, unavailable or Spotify
    local-file entry (rule 3)."""

    key: str
    item: int | None = None
    local: bool = False
    playable: bool = True

    @property
    def resolved(self) -> int | None:
        return self.item if self.playable else None


@dataclasses.dataclass(frozen=True)
class Track:
    """A Navidrome playlist track, in playlist order: its *song* (media file
    ID), the beets *item* its path maps to (None: outside the music folder, or
    no item at that path), Navidrome's *missing* flag, and whether the item
    has a Spotify ID or ISRC (*promotable*)."""

    song: str
    item: int | None = None
    missing: bool = False
    promotable: bool = False

    @property
    def mapped(self) -> int | None:
        return None if self.missing else self.item


@dataclasses.dataclass(frozen=True)
class Held:
    """A snapshot row: *item* as Navidrome held it, as *song* (None for an
    item the cycle's own write adds; the flow fills it in)."""

    item: int
    song: str | None


@dataclasses.dataclass(frozen=True)
class Add:
    """A local add: *item* placed after the entry keyed *after* (None: the
    head), promoted to Spotify when *promote*."""

    item: int
    after: str | None
    promote: bool


@dataclasses.dataclass
class Plan:
    """One playlist's cycle.  *trips* lists the breaker rules the writes trip;
    with ``no_store`` or ``empty`` nothing else is filled in.  *taken* when the
    cycle took the first snapshot (nothing reported).

    *classes* maps each snapshot item to its class; *local_deletes* and
    *adds* are the Navidrome deltas that survived rule 1, and *conflicts* the
    items dropped by it.  *spotify_removes* are the Spotify entry keys to
    remove, one per entry (a duplicate twice); promotions are the ``promote``
    *adds*.  *entries* is the merged store (promotion placeholders keyed
    :data:`PROMOTE`), *view* its items, *snapshot* what the next cycle diffs
    against once the writes are applied."""

    trips: list[str] = dataclasses.field(default_factory=list)
    taken: bool = False
    classes: dict[int, str] = dataclasses.field(default_factory=dict)
    local_deletes: list[int] = dataclasses.field(default_factory=list)
    adds: list[Add] = dataclasses.field(default_factory=list)
    conflicts: list[int] = dataclasses.field(default_factory=list)
    spotify_removes: list[str] = dataclasses.field(default_factory=list)
    navidrome_adds: list[int] = dataclasses.field(default_factory=list)
    navidrome_removes: list[str] = dataclasses.field(default_factory=list)
    entries: list[Entry] = dataclasses.field(default_factory=list)
    view: list[int] = dataclasses.field(default_factory=list)
    snapshot: list[Held] = dataclasses.field(default_factory=list)

    @property
    def promotions(self) -> list[Add]:
        return [a for a in self.adds if a.promote]

    @property
    def spotify_writes(self) -> int:
        return len(self.spotify_removes) + len(self.promotions)

    def pending(self) -> dict[str, int]:
        """Counts per kind, for ``music_playlist_sync_pending{playlist,kind}``."""
        classes = Counter(self.classes.values())
        return {
            "local_delete": len(self.local_deletes),
            "local_add": len(self.adds) - len(self.promotions),
            "promotion": len(self.promotions),
            "conflict": len(self.conflicts),
            "spotify_remove": len(self.spotify_removes),
            "navidrome_add": len(self.navidrome_adds),
            "navidrome_remove": len(self.navidrome_removes),
            "hidden": classes[HIDDEN],
            "unmapped": classes[UNMAPPED],
        }


def view_of(entries: Sequence[Entry]) -> list[int]:
    """The items *entries* resolve to, deduped, in order."""
    return list(dict.fromkeys(e.resolved for e in entries if e.resolved is not None))


def take(view: Sequence[int], tracks: Sequence[Track]) -> list[Held]:
    """A first snapshot: each view item a mapped track holds, as its first
    track's song.  A track outside the view is left out, so the next cycle
    reports it as a local add rather than silently removing it."""
    in_view = set(view)
    out: dict[int, Held] = {}
    for t in tracks:
        if t.mapped in in_view and t.mapped not in out:
            out[t.mapped] = Held(t.mapped, t.song)
    return list(out.values())


def classify(held: Held, tracks: Sequence[Track], library: Collection[int], resolving: Collection[int]) -> str:
    """The class of snapshot row *held* against the current *tracks* (see the
    module table).  *resolving*: the items store entries resolve to now."""
    if any(t.mapped == held.item for t in tracks):
        return PRESENT
    if any(t.song == held.song or (t.missing and t.item == held.item) for t in tracks):
        return UNMAPPED
    if held.item not in library or held.item not in resolving:
        return HIDDEN
    return LOCAL_DELETE


def _place(entries: list[Entry], entry: Entry, before: Sequence[int | None]) -> str | None:
    """Insert *entry* into *entries* after the first entry resolving to the
    nearest item of *before* (walking back) that one resolves to; at the head
    when none does.  Returns the anchor's key."""
    first: dict[int, int] = {}
    for i, e in enumerate(entries):
        if e.resolved is not None:
            first.setdefault(e.resolved, i)
    for item in reversed(before):
        if item in first:
            i = first[item]
            entries.insert(i + 1, entry)
            return entries[i].key
    entries.insert(0, entry)
    return None


def _merge_remote(store: Sequence[Entry], remote: Sequence[Entry]) -> list[Entry]:
    """The store's Spotify part replaced by *remote*'s, each existing local
    entry kept after the store entry before it that survives (rule 2)."""
    out = list(remote)
    for i, e in enumerate(store):
        if not e.local:
            continue
        at = 0
        for j in range(i - 1, -1, -1):
            # The anchor is store[j] as its nth copy; a copy the merge lost
            # falls back to the last one left.
            nth = sum(1 for x in store[:j] if x.key == store[j].key)
            copies = [k for k, x in enumerate(out) if x.key == store[j].key]
            if copies:
                at = copies[min(nth, len(copies) - 1)] + 1
                break
        out.insert(at, e)
    return out


def _count_diff(a: Sequence[Entry], b: Sequence[Entry]) -> list[Entry]:
    """The entries of *a* that *b* lacks, by key, counted."""
    spare = Counter(e.key for e in b)
    out = []
    for e in a:
        if spare[e.key]:
            spare[e.key] -= 1
        else:
            out.append(e)
    return out


def plan(
    store: Sequence[Entry] | None,
    snapshot: Sequence[Held] | None,
    tracks: Sequence[Track],
    library: Collection[int],
    spotify: Sequence[Entry] | None = None,
    recent_deletes: int = 0,
) -> Plan:
    """Merge one playlist's cycle.  *store*: its store's entries (None: no
    store); *snapshot*: the last applied cycle's (None: none yet); *tracks*:
    the Navidrome playlist now; *library*: every beets item ID; *spotify*: the
    Spotify list now (None: not read, so no Spotify deltas); *recent_deletes*:
    the Spotify removals applied in the last hour."""
    if store is None:
        return Plan(trips=[NO_STORE])
    ours = [e for e in store if not e.local]
    if spotify is not None and not spotify and any(e.playable for e in ours):
        return Plan(trips=[EMPTY])
    view = view_of(store)
    if snapshot is None:
        if not tracks and view:
            return Plan(trips=[EMPTY])
        return Plan(taken=True, entries=list(store), view=view, snapshot=take(view, tracks))
    if not tracks and snapshot:
        return Plan(trips=[EMPTY])

    out = Plan()
    remote = list(ours if spotify is None else spotify)
    spotify_added = {e.resolved for e in _count_diff(remote, ours)} - {None}
    entries = _merge_remote(store, remote)
    resolving = set(view)

    # Navidrome deletes.
    for held in snapshot:
        cls = classify(held, tracks, library, resolving)
        if cls == LOCAL_DELETE and held.item in spotify_added:
            cls = CONFLICT
            out.conflicts.append(held.item)
        out.classes[held.item] = cls
        if cls != LOCAL_DELETE:
            continue
        out.local_deletes.append(held.item)
        gone = [e for e in entries if e.resolved == held.item]
        out.spotify_removes += [e.key for e in gone if not e.local]
        entries = [e for e in entries if e.resolved != held.item]

    # Navidrome adds, in playlist order, each placed after the track before it.
    known = {h.item for h in snapshot} | resolving
    before: list[int | None] = []
    seen: set[int] = set()
    for t in tracks:
        item = t.mapped
        if item is not None and item not in known and item not in seen:
            seen.add(item)
            if item not in view_of(entries):  # else Spotify added it too: agreement
                key = f"{PROMOTE}{item}" if t.promotable else f"{_LOCAL}{item}"
                anchor = _place(entries, Entry(key, item, local=not t.promotable), before)
                out.adds.append(Add(item, anchor, t.promotable))
        before.append(item)

    # Navidrome writes against the playlist now.
    out.entries = entries
    out.view = view_of(entries)
    target = set(out.view)
    # Items a track still stands for though it doesn't map (the song-ID swap,
    # later): never added again, and the snapshot keeps their song.
    old = {h.item: h.song for h in snapshot}
    stale = {item: old[item] for item, cls in out.classes.items() if cls == UNMAPPED}
    for t in tracks:
        if t.missing and t.item is not None:
            stale.setdefault(t.item, t.song)
    songs: dict[int, str] = {}
    for t in tracks:
        item = t.mapped
        if item is None:
            continue
        if item in target and item not in songs:
            songs[item] = t.song
        else:
            out.navidrome_removes.append(t.song)
    out.navidrome_adds = [i for i in out.view if i not in songs and i not in stale]
    out.snapshot = [Held(i, songs[i] if i in songs else stale.get(i)) for i in out.view]

    if out.spotify_writes > MAX_WRITES:
        out.trips.append(WRITES)
    if len(out.spotify_removes) + recent_deletes > MAX_DELETES_PER_HOUR:
        out.trips.append(DELETES)
    return out
