"""One cycle of the two-way playlist sync (homelab#2361): dry-run only so far.

The ``music-playlist-sync`` flow runs a cycle every 5 minutes while
``PLAYLIST_SYNC`` is ``dry-run`` (``off``, the default, runs none).  A cycle
reads, for each covered playlist, its store, its snapshot and its Navidrome
playlist, and hands them to the merge core (:func:`playlist_sync.plan`).  It
writes nothing to Spotify, Navidrome or the store, and records no event: it
reports what the first ``on`` cycle would do.

**Covered** is every playlist in ``playlists.conf`` that could become two-way:
none of ``oneway``, ``nosync`` or ``album`` (``later`` is not in it), narrowed
to ``PLAYLIST_SYNC_PLAYLISTS`` (comma-separated) when that is set.  ``twoway``
doesn't gate it: adding ``twoway`` is what triggers the cutover.

**The inputs.**  Each store entry's item is the one its slot in
:func:`scan.store_view` resolves to (Spotify ID, then ISRC, never title
words), so the cycle can't resolve an entry differently from the ``.m3u``; a
local entry's is its own item while that is in the library.  A Spotify entry
with no track ID (entry key ``url:``) is not playable (rule 3).  The Navidrome
playlist is the one imported from the playlist's ``.m3u`` (matched by file
path, never by name; of two imported from one file, the first with sync on,
with a warning), read through the native API.  A track's song is its row's
``mediaFileId``, never its ``id`` (the row's position), and its item the beets
item at its path, Navidrome's music folder swapped for the pipeline's.

**Skips.**  A cycle is skipped (``busy``) when the ``pipeline`` slot is taken
(a fetch or a scan: the probe is the flow's), and (``scanning``) while
Navidrome scans.  Both are checked again at the end, and a cycle that a run or
a scan started during is discarded.  The cycle probes the slot and doesn't
hold it: a scan that found it held would skip, and its inbox wait for the
next trigger.  A covered playlist is left out of one cycle, and reported as
skipped with its reason, when it has no imported Navidrome playlist, its
Navidrome playlist has sync off, its ``.m3u`` is newer than the Navidrome
playlist's ``updatedAt`` (written and not re-imported yet: Navidrome stamps
``updatedAt`` at every import, so until it has, the tracks are the last
``.m3u``'s and a track downloaded since would read as an add), or reading it
failed.  A missing store is the ``no_store`` would-trip (the merge core's).

**Snapshots** (``playlist_sync``, migration 24).  The first cycle that reads
a playlist takes its snapshot; a dry run never advances one, so pending is
what the first ``on`` cycle would do.  The snapshot of a playlist that left
the coverage by ``playlists.conf`` (removed, or flagged ``oneway``, ``nosync``
or ``album``) is dropped, so one that comes back is taken afresh rather than
diffed against a stale one.  Narrowing by ``PLAYLIST_SYNC_PLAYLISTS`` drops
nothing.  Snapshots are written only once the cycle has passed its end checks.

**Report.**  Per compared playlist a ``[SYNC-DRY]`` line with
:meth:`Plan.pending`'s counts and the would-trips, at INFO when a local
delete, local add, promotion or would-trip is pending (the kinds the phase-2
done-when needs at zero), else DEBUG, then the items of each local delete
and add.  One ``[SYNC]`` line per cycle.  On the Pushgateway, job
``music_playlist_sync`` (every cycle): ``music_playlist_sync_outcome{outcome}``
(1 for this cycle's outcome, 0 for the others) and
``music_playlist_sync_last_cycle_timestamp_seconds``; job
``music_playlist_sync_ok`` (an ``ok`` cycle only, so a skip leaves the last
plan's numbers in place): ``music_playlist_sync_last_ok_timestamp_seconds``,
``music_playlist_sync_duration_seconds`` (wall time),
``music_playlist_sync_pending{playlist,kind}`` (compared playlists only),
``music_playlist_sync_would_trip{playlist,rule}`` and
``music_playlist_sync_skipped{playlist,reason}``.  A skipped playlist has no
pending series that cycle, so it never reads as zero pending.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import re
import time
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import datetime
from pathlib import PurePosixPath

from music_fetch import config, state, store
from music_scan import metrics, navidrome, playlist_shadow, playlist_sync, playlists, scan
from music_scan.identity import ItemIndex, item_isrcs, item_spotify_ids
from music_scan.library import MusicLibrary
from music_scan.playlist_sync import Entry, Held, Plan, Track

logger = logging.getLogger(__name__)

MODE_ENV = "PLAYLIST_SYNC"
PLAYLISTS_ENV = "PLAYLIST_SYNC_PLAYLISTS"
OFF, DRY_RUN = "off", "dry-run"
MODES = (OFF, DRY_RUN)

# Cycle outcomes.
OK, BUSY, SCANNING, ERROR = "ok", "busy", "scanning", "error"
OUTCOMES = (OK, BUSY, SCANNING, ERROR)
# Why a covered playlist was left out of a cycle.
NO_NAVIDROME, SYNC_OFF, M3U_NEWER, FAILED = "no_navidrome_playlist", "navidrome_sync_off", "m3u_newer", "failed"
# Breaker rules, for the would-trip series.
RULES = (playlist_sync.NO_STORE, playlist_sync.EMPTY, playlist_sync.WRITES, playlist_sync.DELETES)
# The pending kinds the phase-2 done-when needs at zero; the others are reported only.
FAILING = ("local_delete", "local_add", "promotion")

# How far a .m3u's mtime may run ahead of Navidrome's updatedAt before the
# playlist is skipped: the two pods' clocks.  An import stamps updatedAt after
# it has read the file, so a re-imported playlist is never newer by more.
M3U_SLACK_SECONDS = 5
# Items listed per kind in the detail lines.
LISTED = 10

JOB, OK_JOB = "music_playlist_sync", "music_playlist_sync_ok"


def mode() -> str:
    """``PLAYLIST_SYNC``: ``off`` (the default) or ``dry-run``.  Raises
    ValueError on anything else: ``on`` comes with the Spotify writes."""
    value = (os.environ.get(MODE_ENV) or OFF).strip().lower()
    if value not in MODES:
        raise ValueError(f"{MODE_ENV}={value!r} is not one of {', '.join(MODES)}")
    return value


@dataclasses.dataclass
class Result:
    """One covered playlist's part of a cycle: its *plan*, or why it was *skipped*;
    *view* the store's view (what the ``.m3u`` holds), *tracks* Navidrome's count."""

    name: str
    plan: Plan | None = None
    skipped: str | None = None
    view: int = 0
    tracks: int = 0
    owner: str = ""


@dataclasses.dataclass
class Cycle:
    results: list[Result]
    # Snapshots of playlists that left the coverage, with why.
    orphans: dict[str, str]
    user: str = ""
    labels: Mapping[int, str] = dataclasses.field(default_factory=dict)


def coverage(conf: Sequence[config.PlaylistConfig], only: Collection[str] = ()) -> tuple[list[str], dict[str, str]]:
    """The covered playlists in ``playlists.conf`` order (narrowed to *only*
    when given), and the excluded ones with why."""
    covered, excluded = [], {}
    for pl in conf:
        flags = [f for f in ("oneway", "nosync", "album") if getattr(pl, f)]
        if flags:
            excluded[pl.name] = f"flagged {', '.join(flags)}"
        elif not only or pl.name in only:
            covered.append(pl.name)
    return covered, excluded


def _only() -> set[str]:
    return {n.strip() for n in (os.environ.get(PLAYLISTS_ENV) or "").split(",") if n.strip()}


_RFC3339 = re.compile(r"(.+?)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)")


def parse_time(value: str) -> datetime:
    """A Navidrome RFC 3339 time (nanoseconds, ``Z``) as an aware datetime."""
    m = _RFC3339.fullmatch(value or "")
    if not m:
        raise ValueError(f"not an RFC 3339 time: {value!r}")
    base, frac, zone = m.groups()
    return datetime.fromisoformat(base + (f".{frac[:6]}" if frac else "") + ("+00:00" if zone == "Z" else zone))


def m3u_newer(name: str, pls: Mapping) -> bool:
    """Whether playlist *name*'s ``.m3u`` was written after Navidrome last
    imported it (*pls*'s ``updatedAt``), allowing :data:`M3U_SLACK_SECONDS`.
    False without a ``.m3u``.  Raises ValueError when ``updatedAt`` can't be read."""
    try:
        mtime = (scan.PLAYLISTS / f"{name}.m3u").stat().st_mtime
    except FileNotFoundError:
        return False
    return mtime > parse_time(pls.get("updatedAt") or "").timestamp() + M3U_SLACK_SECONDS


def entries_of(name: str, playlist: store.Store, library: ItemIndex, by_album, stamps,
               items: Collection[int]) -> list[Entry]:
    """*playlist*'s entries for the merge core, in store order, each with the
    item its :func:`scan.store_view` slot resolves to (a local entry: its own
    item while it is in *items*).  Raises RuntimeError when the slots don't
    line up with the store's songs (an album playlist's are per album)."""
    view = scan.store_view(name, library, by_album, stamps, playlist=playlist)
    if view is None:
        raise RuntimeError(f"{name}: its store view failed")
    slots = view[1]
    if len(slots) != len(playlist.songs):
        raise RuntimeError(f"{name}: {len(slots)} slots for {len(playlist.songs)} store songs")
    it = iter(slots)
    out: list[Entry] = []
    for entry in playlist.entries:
        key = store.entry_key(entry)
        if entry.get("song"):
            slot = next(it)
            out.append(Entry(key, None if slot.item is None else slot.item.id, playable=not key.startswith("url:")))
        elif local := entry.get(store.LOCAL):
            item = local.get("id")
            out.append(Entry(key, item if item in items else None, local=True))
    return out


def to_track(row: Mapping, nd_music: PurePosixPath, by_path: Mapping) -> Track:
    """A Navidrome playlist track row for the merge core: its song is the
    row's ``mediaFileId`` (KeyError without one: the contract broke)."""
    path = playlist_shadow.pipeline_path(row, nd_music, scan.LIBRARY.parent)
    item = by_path.get(path) if path is not None else None
    return Track(row["mediaFileId"], None if item is None else item.id, bool(row.get("missing")),
                 promotable=item is not None and bool(item_spotify_ids(item) or item_isrcs(item)))


def halted(plan: Plan) -> bool:
    """Whether *plan* stopped at a trip that leaves nothing else filled in (rules 5 and 6)."""
    return any(t in (playlist_sync.NO_STORE, playlist_sync.EMPTY) for t in plan.trips)


def _label(item) -> str:
    return f"{item.artist or ''} — {item.title or ''}"


def read() -> Cycle:
    """Read and plan every covered playlist.  Writes nothing.  Raises when
    Navidrome, ``playlists.conf``, the database or the library can't be read;
    a playlist that fails alone is skipped (:data:`FAILED`)."""
    nd = navidrome._config()
    if nd is None:
        raise RuntimeError("NAVIDROME_URL is unset: there is no Navidrome playlist to compare")
    url, user, password = nd
    nd_dir = PurePosixPath(os.environ.get("NAVIDROME_PLAYLISTS_DIR") or navidrome.DEFAULT_PLAYLISTS_DIR)
    nd_music = PurePosixPath(os.environ.get("NAVIDROME_MUSIC_DIR") or navidrome.DEFAULT_MUSIC_DIR)
    conf = config.load_playlists()
    covered, excluded = coverage(conf, _only())
    headers = navidrome._login(url, user, password)
    found = playlist_shadow._nd_playlists(navidrome.list_playlists(url, headers), nd_dir, tag="SYNC")
    with state.session() as conn:
        snapshots = state.load_snapshots(conn)
    in_conf = {pl.name for pl in conf}
    orphans = {n: excluded.get(n, "not in playlists.conf") for n in snapshots if n not in in_conf or n in excluded}

    results: list[Result] = []
    with MusicLibrary(scan.LIBRARY_DB) as lib:
        all_items = lib.all_items()
        library = ItemIndex(all_items)
        by_album = playlists.by_album_id(all_items)
        stamps = playlists.record_stamps(scan._album_records())
        by_path = {scan._item_path(item): item for item in all_items}
        items = {item.id for item in all_items}
        labels = {item.id: _label(item) for item in all_items}
        for name in covered:
            pls = found.get(name)
            if pls is None:
                results.append(Result(name, skipped=NO_NAVIDROME))
                continue
            if not pls.get("sync"):
                results.append(Result(name, skipped=SYNC_OFF))
                continue
            try:
                playlist = store.load(name)
                entries = None if playlist is None else entries_of(name, playlist, library, by_album, stamps, items)
                tracks = [to_track(r, nd_music, by_path) for r in navidrome.playlist_tracks(url, headers, pls["id"])]
                # After the read: a .m3u written meanwhile is newer than the listing's updatedAt too.
                if m3u_newer(name, pls):
                    results.append(Result(name, skipped=M3U_NEWER))
                    continue
            except Exception:
                logger.error("[SYNC-DRY] %s: could not be read; skipped this cycle", name, exc_info=True)
                results.append(Result(name, skipped=FAILED))
                continue
            held = snapshots.get(name)
            snapshot = None if held is None else [Held(item, song) for item, song in held]
            results.append(Result(name, plan=playlist_sync.plan(entries, snapshot, tracks, items),
                                  view=len(playlist_sync.view_of(entries or [])), tracks=len(tracks),
                                  owner=pls.get("ownerName") or ""))
    return Cycle(results, orphans, user, labels)


def commit(cycle: Cycle) -> None:
    """Write what a dry-run cycle may: the snapshots it took, and the orphans' removal."""
    taken = [r for r in cycle.results if r.plan is not None and r.plan.taken]
    if not (taken or cycle.orphans):
        return
    with state.session() as conn:
        for r in taken:
            state.save_snapshot(conn, r.name, [(h.item, h.song) for h in r.plan.snapshot])
        if cycle.orphans:
            state.drop_snapshots(conn, cycle.orphans)


def _items(kind: str, name: str, items: Sequence[int], labels: Mapping[int, str]) -> None:
    for item in items[:LISTED]:
        logger.info("[SYNC-DRY]   %s %s: %s %s", name, kind, item, labels.get(item, "(not in the library)"))
    if len(items) > LISTED:
        logger.info("[SYNC-DRY]   %s %s: … and %d more", name, kind, len(items) - LISTED)


def report(cycle: Cycle, elapsed: float) -> None:
    """Log the cycle's ``[SYNC-DRY]`` and ``[SYNC]`` lines."""
    for name, why in sorted(cycle.orphans.items()):
        logger.info("[SYNC] %s: snapshot dropped, the playlist left the sync (%s)", name, why)
    totals: Counter = Counter()
    taken = trips = 0
    for r in cycle.results:
        p = r.plan
        if p is None:
            continue
        if halted(p):
            trips += 1
            logger.warning("[SYNC-DRY] %s: would trip %s", r.name, ", ".join(p.trips))
            continue
        if p.taken:
            taken += 1
            logger.info("[SYNC-DRY] %s: snapshot taken: %d of the view's %d item(s), navidrome %d", r.name,
                        len(p.snapshot), r.view, r.tracks)
            if r.owner and r.owner != cycle.user:
                logger.warning("[SYNC-DRY] %s: the Navidrome playlist belongs to %s, not %s, whom the sync logs in as",
                               r.name, r.owner, cycle.user)
            continue
        pending = p.pending()
        totals.update(pending)
        trips += bool(p.trips)
        loud = bool(p.trips) or any(pending[k] for k in FAILING)
        logger.log(logging.INFO if loud else logging.DEBUG,
                   "[SYNC-DRY] %s: view %d, navidrome %d, %s%s", r.name, r.view, r.tracks,
                   ", ".join(f"{k.replace('_', ' ')} {n}" for k, n in pending.items()),
                   f"; would trip {', '.join(p.trips)}" if p.trips else "")
        _items("local delete", r.name, p.local_deletes, cycle.labels)
        _items("local add", r.name, [a.item for a in p.adds if not a.promote], cycle.labels)
        _items("promotion", r.name, [a.item for a in p.promotions], cycle.labels)
    skipped = [f"{r.name} ({r.skipped})" for r in cycle.results if r.skipped]
    compared = sum(1 for r in cycle.results if r.plan is not None)
    logger.info("[SYNC] dry-run cycle ok in %.1fs: %d covered, %d compared (%d snapshot(s) taken), %d skipped%s, "
                "%d would-trip(s); pending %s", elapsed, len(cycle.results), compared, taken, len(skipped),
                f" ({', '.join(skipped)})" if skipped else "", trips,
                ", ".join(f"{k.replace('_', ' ')} {totals[k]}" for k in FAILING))


def _family(name: str, samples: Sequence[tuple[Mapping[str, str], int | float]]) -> list[str]:
    lines = [f"# TYPE {name} gauge"]
    for labels, value in samples:
        pairs = ",".join(f'{k}="{v}"' for k, v in labels.items())
        lines.append(f"{name}{{{pairs}}} {value}" if pairs else f"{name} {value}")
    return lines


def push_outcome(outcome: str) -> None:
    """Push job ``music_playlist_sync``: this cycle's outcome."""
    lines = _family("music_playlist_sync_outcome", [({"outcome": o}, int(o == outcome)) for o in OUTCOMES])
    lines += _family("music_playlist_sync_last_cycle_timestamp_seconds", [({}, int(time.time()))])
    metrics._push("\n".join(lines), JOB)


def push_plans(cycle: Cycle, elapsed: float) -> None:
    """Push job ``music_playlist_sync_ok``: an ``ok`` cycle's plans."""
    pending, trips, skipped = [], [], []
    for r in cycle.results:
        if r.skipped:
            skipped.append(({"playlist": r.name, "reason": r.skipped}, 1))
            continue
        assert r.plan is not None
        trips += [({"playlist": r.name, "rule": rule}, int(rule in r.plan.trips)) for rule in RULES]
        if not halted(r.plan):
            pending += [({"playlist": r.name, "kind": k}, n) for k, n in r.plan.pending().items()]
    lines = _family("music_playlist_sync_last_ok_timestamp_seconds", [({}, int(time.time()))])
    lines += _family("music_playlist_sync_duration_seconds", [({}, round(elapsed, 3))])
    for name, samples in (("music_playlist_sync_pending", pending), ("music_playlist_sync_would_trip", trips),
                          ("music_playlist_sync_skipped", skipped)):
        if samples:
            lines += _family(name, samples)
    metrics._push("\n".join(lines), OK_JOB)


def run(free: Callable[[], bool]) -> str:
    """Run one cycle; returns its outcome (``off`` when ``PLAYLIST_SYNC`` is).
    *free*: whether the ``pipeline`` slot is free, probed at the start and the
    end.  Never raises: a failure is the ``error`` outcome, logged."""
    start = time.monotonic()
    try:
        if mode() == OFF:
            logger.debug("[SYNC] off (%s)", MODE_ENV)
            return OFF
        if not free():
            logger.info("[SYNC] skipped: the pipeline is busy (a fetch or a scan is running)")
            outcome = BUSY
        elif navidrome.scanning():
            logger.info("[SYNC] skipped: Navidrome is scanning")
            outcome = SCANNING
        else:
            cycle = read()
            if not any(r.plan for r in cycle.results) and any(r.skipped == FAILED for r in cycle.results):
                raise RuntimeError("no covered playlist could be read")
            if navidrome.scanning():
                logger.info("[SYNC] discarded: Navidrome started scanning during the cycle")
                outcome = SCANNING
            elif not free():
                logger.info("[SYNC] discarded: a fetch or a scan started during the cycle")
                outcome = BUSY
            else:
                commit(cycle)
                elapsed = time.monotonic() - start
                report(cycle, elapsed)
                push_plans(cycle, elapsed)
                outcome = OK
    except Exception:
        logger.error("[SYNC] cycle failed", exc_info=True)
        outcome = ERROR
    push_outcome(outcome)
    return outcome
