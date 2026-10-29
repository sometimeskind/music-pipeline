"""Spotify album → MusicBrainz release group, for the album store's key (homelab#2410).

The album store is keyed by release group (homelab#2478, decision 2).  A pass
over the records the store can't key yet (``release_group`` unset) takes the
first evidence that answers, and never a fuzzy match:

1. ``import``:  the release group of the release album mode imported for it
   (``release_files``), what is on disk.  It wins over everything; evidence that
   disagrees is reported as a conflict.
2. ``cache``:   the MusicBrainz release found for its Spotify album by URL,
   ISRC or barcode (:mod:`music_scan.mb_release`'s cache, the ``mb-releases``
   kv document).  Those rungs are exact already: the ISRC rung needs every
   sampled ISRC on one release with Spotify's track count and title.
3. ``library``: the beets items tagged with its Spotify album ID, when every
   one that has a release group has the same one.  A mixed album, its items on
   several release groups, gives no evidence.

A record with several Spotify album IDs (a merged one) takes evidence from
each; when the cache and library evidence name two release groups, it stays
unresolved as ``conflict``.  With no evidence it stays ``no-match`` when the
cache's lookup found nothing for every one of its Spotify IDs, else
``pending`` (never looked up).

Two records that resolve to one release group are editions of one album and
merge into one row ("the album is the unit"), but never mid-import: when one
of them is grabbed or importing, none of them merges and the newcomers stay
``in-flight`` until a later pass.  The survivor is the row that already had
the release group, else the one furthest along (:data:`STATUS_RANK`).  It
keeps its own fields, and takes the others' Spotify IDs, playlists' tracks
and blocklisted releases, the higher attempt count and the strongest origin
(an album playlist's, then wants, then library).

The pass is idempotent: run it again after the cache or the library learns
more, and it resolves and merges what it now can (``music-album-resolve``).
"""

from __future__ import annotations

import dataclasses
import logging
import sqlite3
from collections.abc import Mapping
from pathlib import Path

from music_fetch import state

logger = logging.getLogger(__name__)

IMPORT, CACHE, LIBRARY = "import", "cache", "library"
# Survivor order for a merge: an album held beats one still wanted.
STATUS_RANK = {"imported": 0, "filled": 0, "have": 0, "fallback": 1, "failed": 2, "missing": 3,
               "dry-run": 4, "wanted": 5, "grabbed": 6}
# A record's origin, strongest first; absent means an album playlist's.
ORIGIN_RANK = {state.ALBUM: 0, "wants": 1, "library": 2}
# A grabbed record's import in progress (albums.IMPORT_FIELDS, not imported here: albums imports state).
_IMPORT_FIELDS = ("import_started", "import_errors", "import_deferrals", "import_path", "import_identity")


def library_groups(db_path: Path) -> dict[str, set[str]]:
    """Spotify album ID → the release groups of the beets items tagged with it
    (items without one left out).  Read straight from beets' SQLite, read-only."""
    out: dict[str, set[str]] = {}
    uri = f"file:{db_path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        for album_id, group in conn.execute(
                "SELECT a.value, i.mb_releasegroupid FROM items i JOIN item_attributes a "
                "ON a.entity_id = i.id AND a.key = 'spotify_album_id' "
                "WHERE a.value != '' AND i.mb_releasegroupid IS NOT NULL AND i.mb_releasegroupid != ''"):
            out.setdefault(album_id, set()).add(group)
    return out


def in_flight(record: Mapping) -> bool:
    return record.get("status") == "grabbed" or any(f in record for f in _IMPORT_FIELDS)


def label(key: str, record: Mapping) -> str:
    return f"{record.get('artist', '')} — {record.get('name', '')} ({key})"


@dataclasses.dataclass
class Verdict:
    group: str | None = None
    via: str = ""
    unresolved: str | None = None
    conflict: str = ""


def judge(record: Mapping, cache: Mapping[str, dict], library: Mapping[str, set[str]]) -> Verdict:
    """The release group the evidence gives *record*, or why there is none."""
    imported = ((record.get("release_files") or {}).get("mb") or {}).get("mb_releasegroupid") or None
    found: dict[str, set[str]] = {}  # release group → the evidence naming it
    looked_up = True
    ids = record.get("spotify_album_ids") or []
    for album_id in ids:
        entry = cache.get(album_id)
        if entry is None:
            looked_up = False
        elif entry.get("rung") != "none":
            group = ((entry.get("fields") or {}).get("mb_releasegroupid")) or None
            if group:
                found.setdefault(group, set()).add(f"{CACHE}:{entry.get('rung')}")
        groups = library.get(album_id) or set()
        if len(groups) == 1:
            found.setdefault(next(iter(groups)), set()).add(LIBRARY)
    if imported:
        others = sorted(g for g in found if g != imported)
        return Verdict(imported, IMPORT, conflict=f"the import's {imported} beats {', '.join(others)}" if others else "")
    if len(found) == 1:
        group, vias = next(iter(found.items()))
        return Verdict(group, "+".join(sorted(vias)))
    if found:
        return Verdict(unresolved=state.CONFLICT,
                       conflict="; ".join(f"{g} by {'+'.join(sorted(v))}" for g, v in sorted(found.items())))
    return Verdict(unresolved=state.NO_MATCH if ids and looked_up else state.PENDING)


@dataclasses.dataclass
class Report:
    rekeyed: list[str] = dataclasses.field(default_factory=list)
    flagged: dict[str, list[str]] = dataclasses.field(default_factory=dict)
    merged: list[str] = dataclasses.field(default_factory=list)
    conflicts: list[str] = dataclasses.field(default_factory=list)
    absorbed: int = 0

    def summary(self) -> str:
        flags = ", ".join(f"{len(v)} {k}" for k, v in sorted(self.flagged.items()))
        return (f"{len(self.rekeyed)} rekeyed, {sum(len(v) for v in self.flagged.values())} flagged"
                f"{f' ({flags})' if flags else ''}, {self.absorbed} merged into {len(self.merged)} album(s), "
                f"{len(self.conflicts)} conflict(s)")

    def log(self, verbose: bool = False) -> None:
        for line in self.merged:
            logger.info("  [MERGE] %s", line)
        for line in self.conflicts:
            logger.info("  [CONFLICT] %s", line)
        for reason, labels in sorted(self.flagged.items()):
            if verbose or reason in (state.CONFLICT, state.IN_FLIGHT):
                for item in labels:
                    logger.info("  [FLAG] %s: %s", reason, item)
        if verbose:
            for line in self.rekeyed:
                logger.info("  [REKEY] %s", line)
        logger.info("Resolve: %s", self.summary())


def _union(*lists) -> list:
    return list(dict.fromkeys(x for xs in lists for x in (xs or ())))


def _track_id(track: list) -> object:
    return (track[2] if len(track) > 2 else None) or tuple(track[:2])


def merge_into(survivor: dict, other: Mapping) -> None:
    """Fold *other*'s Spotify IDs, names, playlists' tracks, blocklist, attempts and origin into *survivor*."""
    survivor["spotify_album_ids"] = _union(survivor.get("spotify_album_ids"), other.get("spotify_album_ids"))
    survivor["names"] = _union(survivor.get("names"), other.get("names"))
    playlists = survivor.setdefault("playlists", {})
    for name, tracks in (other.get("playlists") or {}).items():
        have = playlists.setdefault(name, [])
        seen = {_track_id(t) for t in have}
        have.extend(t for t in tracks if _track_id(t) not in seen)
    if other.get("blocklist"):
        survivor["blocklist"] = _union(survivor.get("blocklist"), other.get("blocklist"))
    if other.get("attempts"):
        survivor["attempts"] = max(survivor.get("attempts") or 0, other["attempts"])
    origin = min((survivor.get("origin") or state.ALBUM, other.get("origin") or state.ALBUM), key=ORIGIN_RANK.get)
    if origin == state.ALBUM:
        survivor.pop("origin", None)
    else:
        survivor["origin"] = origin


def resolve(rows: state.AlbumRows, cache: Mapping[str, dict], library: Mapping[str, set[str]],
            overrides: Mapping[str, str] | None = None) -> Report:
    """Resolve and merge *rows*' unresolved records in place; returns what changed."""
    overrides = overrides or {}
    report = Report()
    targets: dict[str, list[str]] = {}
    for key, record in rows.albums.items():
        if record.get("release_group"):
            continue
        verdict = judge(record, cache, library)
        if verdict.conflict:
            report.conflicts.append(f"{label(key, record)}: {verdict.conflict}")
        if verdict.group:
            targets.setdefault(verdict.group, []).append(key)
        else:
            record["unresolved"] = verdict.unresolved
            report.flagged.setdefault(verdict.unresolved, []).append(label(key, record))
    rename: dict[str, str] = {}  # old key → new key
    absorbed: set[str] = set()
    for group, keys in targets.items():
        existing = group if group in rows.albums else None  # a row already keyed by it
        members = ([existing] if existing else []) + keys
        if len(members) > 1 and any(in_flight(rows.albums[k]) for k in members):
            for k in keys:
                rows.albums[k]["unresolved"] = state.IN_FLIGHT
                report.flagged.setdefault(state.IN_FLIGHT, []).append(f"{label(k, rows.albums[k])} → {group}")
            continue
        survivor = existing or min(keys, key=lambda k: (STATUS_RANK.get(rows.albums[k].get("status"), 9),
                                                        list(rows.albums).index(k)))
        record = rows.albums[survivor]
        others = [k for k in members if k != survivor]
        for k in others:
            merge_into(record, rows.albums[k])
            absorbed.add(k)
        if others:
            report.merged.append(f"{label(survivor, record)} ← {', '.join(label(k, rows.albums[k]) for k in others)} "
                                 f"as {group}")
            report.absorbed += len(others)
            words = {overrides[i] for i in record.get("spotify_album_ids", []) if i in overrides}
            if len(words) > 1:
                report.conflicts.append(f"{label(survivor, record)}: search overrides differ ({' | '.join(sorted(words))});"
                                        " the primary Spotify ID's wins")
        if not existing:
            record["release_group"], record["unresolved"] = group, None
            rename[survivor] = group
            report.rekeyed.append(f"{label(survivor, record)} → {group}")
    if rename or absorbed:
        rows.albums = {rename.get(k, k): r for k, r in rows.albums.items() if k not in absorbed}
        rows.mixed = list(dict.fromkeys(rename.get(k, k) for k in rows.mixed if k not in absorbed))
    return report
