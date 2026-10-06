"""Canonical album tags from Spotify (#209).

Every import is a singleton filed under ``$albumartist/$album``, so one album
splits in Navidrome when its tracks come from different editions or sources.
Spotify is authoritative for album grouping (homelab#1889): an item with a
Spotify ID takes ``album``, ``albumartist``, the release date and its
track/disc numbering from the Spotify album its playlist entry is on, and beets
moves the file to match.  Items with no Spotify ID, or whose IDs no playlist
entry describes (``[NOALBUM]``), are left alone.

The album data comes from the playlist item pages, as the ``.spotdl`` snapshots
store them: no Spotify calls at import.  The pages carry no disc count, so
``disctotal`` is the highest disc number seen for the album unless an older,
fully fetched entry has ``disc_count``.

Release preference, for an item whose ``spotify_ids`` are on several releases:
``album`` over ``single`` over ``compilation``; then a release a playlist entry
names today over one known only from an old snapshot; then the earliest
release; then the album ID, so every run agrees.

Navidrome groups by the MusicBrainz album ID before the tags, so the album-level
MusicBrainz tags follow the canonical album too (:mod:`music_scan.mb_release`);
``mb_album_via`` records how (``url``, ``isrc``, ``upc``, ``none`` or ``pending``).
Embedded art follows the canonical album.

At import the scan canonicalises new items and those still pending; the album
completion does it after ``tag_album_ids``.  ``music-canon-albums`` backfills
the library: dry run by default, ``--apply`` writes.  Safe to re-run.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import time
from collections import Counter
from collections.abc import Callable, Iterable
from pathlib import Path

from music_scan.identity import item_spotify_ids, spotify_id
from music_scan.mb_release import MB_ALBUM_FIELDS, Resolver, SpotifyAlbum

logger = logging.getLogger(__name__)

TYPE_RANK = {"album": 0, "single": 1, "compilation": 2}
# The backfill's page reads, reused by an --apply within a day of the dry run.
PAGES_CACHE = Path("/root/Music/inbox/spotdl/.canon-pages.json")
PAGES_MAX_AGE = 24 * 3600
# MusicBrainz lookups per scan run; the rest wait in the queue for the next.
IMPORT_BUDGET = 20
PENDING = "pending"
SPOTIFY_FIELDS = ("album", "albumartist", "year", "month", "day", "track", "tracktotal", "disc", "disctotal")


@dataclasses.dataclass(frozen=True)
class Release:
    album_id: str
    name: str
    artist: str
    album_type: str
    date: str
    tracks_count: int
    disc_count: int
    cover_url: str | None
    isrcs: tuple[str, ...]

    def spotify_album(self) -> SpotifyAlbum:
        return SpotifyAlbum(self.album_id, self.name, self.tracks_count, self.isrcs)


@dataclasses.dataclass(frozen=True)
class Placement:
    """One Spotify track on its release."""

    release: Release
    track: int
    disc: int
    named: bool  # a playlist entry names it today

    def key(self):
        r = self.release
        return (TYPE_RANK.get(r.album_type, len(TYPE_RANK)), not self.named, r.date or "9999", r.album_id)


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def placements_from(sources: Iterable[tuple[list[dict], bool]]) -> dict[str, Placement]:
    """``{spotify track id: Placement}`` from ``.spotdl``-format songs.

    *sources* are ``(songs, named)`` pairs; the first one to place a track wins,
    so pass current playlist pages before old snapshots.
    """
    sources = [(list(songs), named) for songs, named in sources]
    albums: dict[str, dict] = {}
    for songs, _ in sources:
        for song in songs:
            album_id = song.get("album_id")
            if not album_id or not song.get("album_name"):
                continue
            a = albums.setdefault(album_id, {"song": song, "discs": 0, "isrcs": {}})
            a["discs"] = max(a["discs"], _int(song.get("disc_count")), _int(song.get("disc_number")))
            if song.get("isrc"):
                a["isrcs"].setdefault(song["isrc"], None)
    releases = {
        album_id: Release(
            album_id=album_id,
            name=a["song"]["album_name"],
            artist=a["song"].get("album_artist") or a["song"].get("artist") or "",
            album_type=a["song"].get("album_type") or "",
            date=a["song"].get("date") or str(a["song"].get("year") or ""),
            tracks_count=_int(a["song"].get("tracks_count")),
            disc_count=max(a["discs"], 1),
            cover_url=a["song"].get("cover_url") if str(a["song"].get("cover_url", "")).startswith("https://") else None,
            isrcs=tuple(a["isrcs"]),
        )
        for album_id, a in albums.items()
    }
    placed: dict[str, Placement] = {}
    for songs, named in sources:
        for song in songs:
            sid = song.get("song_id") or spotify_id(song.get("url"))
            release = releases.get(song.get("album_id"))
            if sid and release and sid not in placed:
                placed[sid] = Placement(release, _int(song.get("track_number")), _int(song.get("disc_number")) or 1, named)
    return placed


def spotdl_songs(spotdl_dir: Path) -> list[dict]:
    songs: list[dict] = []
    for f in sorted(spotdl_dir.glob("*.spotdl")):
        try:
            songs.extend(json.loads(f.read_text(encoding="utf-8")).get("songs", []))
        except Exception:
            continue
    return songs


def choose(item, placements: dict[str, Placement]) -> Placement | None:
    """The item's canonical release (see the module docstring), or None."""
    found = [placements[s] for s in item_spotify_ids(item) if s in placements]
    return min(found, key=Placement.key) if found else None


def spotify_fields(p: Placement) -> dict:
    parts = [_int(x) for x in p.release.date.split("-")[:3]] + [0, 0, 0]
    return {
        "album": p.release.name,
        "albumartist": p.release.artist,
        "year": parts[0],
        "month": parts[1],
        "day": parts[2],
        "track": p.track,
        "tracktotal": p.release.tracks_count,
        "disc": p.disc,
        "disctotal": max(p.release.disc_count, p.disc),
    }


def _path(item) -> str:
    return os.fsdecode(item.path)


@dataclasses.dataclass
class Change:
    item: object
    placement: Placement
    diff: dict  # field → (old, new)
    dest: str
    art: bool


@dataclasses.dataclass
class Plan:
    changes: list[Change] = dataclasses.field(default_factory=list)
    scanned: int = 0
    with_id: int = 0
    noalbum: int = 0
    unchanged: int = 0
    clashes: int = 0
    kinds: Counter = dataclasses.field(default_factory=Counter)

    @property
    def moved(self) -> int:
        return sum(1 for c in self.changes if c.dest != _path(c.item))

    @property
    def art(self) -> int:
        return sum(1 for c in self.changes if c.art)


def _kind(diff: dict) -> str:
    if "album" in diff or "albumartist" in diff:
        return "album"
    if any(f in diff for f in ("year", "month", "day")):
        return "date"
    if any(f in diff for f in ("track", "tracktotal", "disc", "disctotal")):
        return "numbering"
    return "musicbrainz"


def plan_changes(items: Iterable, placements: dict[str, Placement], resolver: Resolver | None) -> Plan:
    """What canonicalising *items* would change.  Leaves the items as they were."""
    from music_scan.cover import has_art  # noqa: PLC0415

    plan = Plan()
    dests: dict[str, object] = {}
    for item in items:
        plan.scanned += 1
        if not item_spotify_ids(item):
            continue
        plan.with_id += 1
        p = choose(item, placements)
        if p is None:
            plan.noalbum += 1
            logger.debug("  [NOALBUM] %s: no playlist entry describes its Spotify IDs", _path(item))
            continue
        fields = spotify_fields(p)
        mb = resolver.fields(p.release.spotify_album()) if resolver is not None else None
        if mb is None:
            fields["mb_album_via"] = PENDING
        else:
            fields.update(mb)
            fields["mb_album_via"] = resolver.cache[p.release.album_id]["rung"]
        fields["spotify_album_id"] = p.release.album_id
        diff = {k: (item.get(k), v) for k, v in fields.items() if item.get(k) != v}
        moved_album = "spotify_album_id" in diff
        art = bool(p.release.cover_url) and (moved_album or not has_art(item))
        if not diff and not art:
            plan.unchanged += 1
            continue
        old = {k: item.get(k) for k in diff}
        for k, (_, v) in diff.items():
            item[k] = v
        dest = os.fsdecode(item.destination())
        for k, v in old.items():
            item[k] = v
        other = dests.get(dest)
        if other is not None or (dest != _path(item) and os.path.exists(dest)):
            plan.clashes += 1
            logger.warning("  [CLASH] %s → %s: another item already has that path", _path(item), dest)
        dests[dest] = item
        plan.changes.append(Change(item, p, diff, dest, art))
        plan.kinds[_kind(diff)] += 1
    return plan


def _show(v) -> str:
    return repr(v) if isinstance(v, str) else str(v)


def log_change(change: Change, apply: bool) -> None:
    shown = [f"{k} {_show(o)}→{_show(n)}" for k, (o, n) in change.diff.items()
             if k in SPOTIFY_FIELDS or k == "mb_album_via"]
    if change.art:
        shown.append("cover")
    where = f" → {change.dest}" if change.dest != _path(change.item) else ""
    logger.info("  [RETAG] %s %s: %s%s%s", change.item.id, _path(change.item), ", ".join(shown),
                where, "" if apply else " (dry run)")


def apply_change(change: Change, images: dict[str, bytes | None], fetch: Callable[[str], bytes]) -> bool:
    """Write the new tags (and cover) to the file, move it, store the item."""
    from beets.util import MoveOperation  # noqa: PLC0415

    from music_scan import cover  # noqa: PLC0415

    item = change.item
    for k, (_, v) in change.diff.items():
        item[k] = v
    if not item.try_write():
        # Storing tags the file doesn't have would be reverted by the scan's beet update.
        raise OSError("could not write the tags")
    url = change.placement.release.cover_url
    if change.art and url:
        if url not in images:
            try:
                images[url] = fetch(url)
            except Exception as exc:
                logger.warning("  [NOART] %s: cover download failed (%s): %s", change.placement.release.name, url, exc)
                images[url] = None
        embedded = False
        if images[url] is not None:
            try:
                cover.embed(item, images[url])
                embedded = True
            except Exception as exc:
                logger.warning("  [NOART] %s: embedding failed: %s", _path(item), exc)
        if not embedded and "spotify_album_id" in change.diff:
            # Keep the old album ID so the next run sees the cover still to follow.
            item["spotify_album_id"] = change.diff["spotify_album_id"][0] or ""
    item.move(operation=MoveOperation.MOVE, store=False)
    item.store()
    return True


def canonicalize(
    items: Iterable,
    placements: dict[str, Placement],
    resolver: Resolver | None,
    apply: bool = True,
    fetch: Callable[[str], bytes] | None = None,
) -> Plan:
    """Plan, log and (with *apply*) write the canonical tags on *items*."""
    from music_scan import cover  # noqa: PLC0415

    fetch = fetch or cover.download
    plan = plan_changes(items, placements, resolver)
    images: dict[str, bytes | None] = {}
    for change in plan.changes:
        log_change(change, apply)
        if apply:
            try:
                apply_change(change, images, fetch)
            except Exception as exc:
                logger.warning("  [RETAG] %s: failed, retried next run: %s", _path(change.item), exc)
    if resolver is not None:
        resolver.save()
    return plan


def canonicalize_items(items: list, spotdl_dir: Path | None = None, budget: int | None = IMPORT_BUDGET) -> int:
    """The import hook: canonicalise *items* from the ``.spotdl`` snapshots."""
    from music_fetch import ingest  # noqa: PLC0415

    if not items:
        return 0
    placements = placements_from([(spotdl_songs(spotdl_dir or ingest.SPOTDL_DIR), True)])
    plan = canonicalize(items, placements, Resolver(budget=budget))
    if plan.changes:
        logger.info("Canonical albums: retagged %d item(s), moved %d", len(plan.changes), plan.moved)
    return len(plan.changes)


def after_scan(lib, since: float) -> int:
    """Items the scan just imported, plus those whose MusicBrainz release is still
    pending or missing (the queue and its weekly retry)."""
    items = [
        i for i in lib.all_items()
        if (i.added or 0) >= since or i.get("mb_album_via") in (PENDING, "none")
    ]
    return canonicalize_items(items)


# ----------------------------------------------------------------------
# Backfill
# ----------------------------------------------------------------------


def read_pages(refresh: bool = False, cache_file: Path = PAGES_CACHE) -> dict[str, list[dict]]:
    """Every configured playlist's songs from Spotify's item pages (one call per
    100 tracks), cached for a day so a dry run and its --apply read Spotify once."""
    from music_fetch import ingest  # noqa: PLC0415
    from music_fetch.config import load_playlists  # noqa: PLC0415
    from music_fetch.spotdl_ops import SpotifyPlaylists  # noqa: PLC0415

    if not refresh:
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if time.time() - cached["read_at"] < PAGES_MAX_AGE:
                logger.info("Using the playlist pages read %s ago", f"{(time.time() - cached['read_at']) / 60:.0f} min")
                return cached["playlists"]
        except (OSError, ValueError, KeyError):
            pass
    reader = SpotifyPlaylists(ingest.COOKIE_FILE)
    pages: dict[str, list[dict]] = {}
    for pl in load_playlists(ingest.CONF_PATH):
        pages[pl.name] = reader.songs(pl.url)
        logger.info("Read %s: %d track(s)", pl.name, len(pages[pl.name]))
    try:
        cache_file.write_text(json.dumps({"read_at": time.time(), "playlists": pages}), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not cache the playlist pages in %s: %s", cache_file, exc)
    return pages


def report(plan: Plan, resolver: Resolver | None) -> None:
    by_album: dict[str, list[Change]] = {}
    for c in plan.changes:
        by_album.setdefault(c.placement.release.album_id, []).append(c)
    for changes in by_album.values():
        r = changes[0].placement.release
        old = sorted({str(c.item.get("album") or "?") for c in changes})
        tag = {"single": "[SINGLE]", "compilation": "[COMP]"}.get(r.album_type, "[ALBUM]")
        logger.info("%s %s — %s (%s, %s, %s): %d item(s) from %d current album name(s): %s",
                    tag, r.artist, r.name, r.date or "?", r.album_type or "?", r.album_id,
                    len(changes), len(old), "; ".join(old))
    logger.info("Items: %d scanned, %d with a Spotify ID, %d with no album data (NOALBUM), %d unchanged",
                plan.scanned, plan.with_id, plan.noalbum, plan.unchanged)
    logger.info("Changes: %d item(s) (%s); %d file(s) move, %d cover(s) replaced, %d clash(es)",
                len(plan.changes), ", ".join(f"{k} {v}" for k, v in sorted(plan.kinds.items())) or "none",
                plan.moved, plan.art, plan.clashes)
    merged = {(str(c.item.get("albumartist")), str(c.item.get("album"))) for c in plan.changes
              if "album" in c.diff or "albumartist" in c.diff}
    logger.info("Albums: %d current album name(s) become %d Spotify album(s)", len(merged),
                len({c.placement.release.album_id for c in plan.changes if "album" in c.diff or "albumartist" in c.diff}))
    if resolver is not None:
        logger.info("MusicBrainz: looked up %d album(s) this run (%s), %d call(s); %d album(s) cached",
                    resolver.looked_up, ", ".join(f"{k} {v}" for k, v in sorted(resolver.rungs.items())) or "none",
                    resolver.mb.calls, len(resolver.cache))


def run(apply: bool = False, refresh: bool = False, mb_budget: int | None = None) -> Plan:
    from music_fetch import ingest  # noqa: PLC0415
    from music_scan.library import LIBRARY_DB, LIBRARY_DIR, MusicLibrary  # noqa: PLC0415
    from music_scan.navidrome import trigger_scan  # noqa: PLC0415

    pages = read_pages(refresh)
    placements = placements_from(
        [(songs, True) for songs in pages.values()] + [(spotdl_songs(ingest.SPOTDL_DIR), False)]
    )
    logger.info("%d Spotify track(s) placed on %d release(s)", len(placements),
                len({p.release.album_id for p in placements.values()}))
    resolver = Resolver(budget=mb_budget)
    with MusicLibrary(LIBRARY_DB, LIBRARY_DIR) as lib:
        plan = canonicalize(lib.all_items(), placements, resolver, apply=apply)
    report(plan, resolver)
    if not apply:
        logger.info("Dry run: %d item(s) would change. Re-run with --apply to write.", len(plan.changes))
        return plan
    logger.info("Retagged %d item(s)", len(plan.changes))
    if plan.changes:
        trigger_scan()
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(prog="music-canon-albums", description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the tags and move the files (default: dry run)")
    parser.add_argument("--refresh", action="store_true", help="re-read the playlist pages even if cached")
    parser.add_argument("--mb-budget", type=int, default=None, metavar="N",
                        help="look up at most N albums on MusicBrainz this run (default: all)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    run(apply=args.apply, refresh=args.refresh, mb_budget=args.mb_budget)
