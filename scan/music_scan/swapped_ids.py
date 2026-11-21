"""music-audit-swapped-ids: items holding a same-titled item's Spotify ID or ISRC (homelab#2707).

Before music-pipeline#474 the import kept the tags it read from an inbox file
by the file's lowercased title, one entry per title.  When one ``beet import``
held two files with the same title, the first stored took the entry of the
last one read: its ``spotify_url`` became the other's, the other's Spotify ID
joined its ``spotify_ids`` and the other's ISRC its ``isrc`` (its ``sources``,
``via`` and Usenet job folder too, which this audit doesn't judge).  The other
item found nothing left and kept only its own.  The ISRC was set in the
database alone, which zeroes beets' stored mtime, so until music-pipeline#481
the next scan's ``beet update`` read the file's own ISRC back over it; the
Spotify fields are flexible attributes, which it leaves alone.  So most victims
likely hold only the other's Spotify ID now (read in code, not measured).  The
issue's reproduction::

    Artist|HOME|ids=AAA,BBB|isrc=ISRCAAA;ISRCBBB   (spotify_url …/BBB)
    Other|HOME|ids=BBB|isrc=ISRCBBB

So the signature is a pair of items with one title that share a Spotify ID or
ISRC, where one (the *victim*) holds IDs beyond the shared ones and the other
holds none beyond them.  An ID that agrees is the library's proof of one track,
and this bug is the case where it lies, so a pair is **certain** only when the
items are plainly two tracks without it: their artists share nothing
(:func:`music_scan.identity.different_artist`) and their lengths are further
apart than one recording's copies (:data:`~music_scan.identity.LENGTH_SLACK`)
or their MusicBrainz recordings differ.  A pair that could be one track (true
duplicates share IDs legitimately, and a single's and an album's track ID
both sit on one item), or whose IDs don't say which side took the other's, is
listed to **check by hand**.  A pair with neither side holding more and
nothing telling them apart is counted as duplicates, ``audit_dupes``' business.

Titles are grouped by their words once edition notes are dropped (as
:func:`~music_scan.identity.same_base_title` drops them), a superset of the
lowercased title the import keyed by, so a title a match later gave an
edition note is still grouped.

The fix for a certain pair is the victim's: ``unlink_spotify_id`` takes a Spotify
ID off ``spotify_ids``, but it refuses an item's ``spotify_url`` ID, which is
where this bug wrote the other's, and no tool rewrites ``spotify_url`` or takes
a valid ISRC off an item; the line says which.  Read-only and cheap: beets'
SQLite opened read-only with a busy timeout, every item's ID and title read by
ranges of IDs (as ``audit_dupes`` reads, homelab#2665), then only the
same-titled items' few fields, closed before anything is judged.  No
fingerprint is read, no file opened, no writes, no events.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import logging
import os
import re
import sqlite3
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from urllib.parse import quote

from music_fetch.usenet import clean_album
from music_scan.identity import (
    artist_credit,
    different_artist,
    item_isrcs,
    item_spotify_ids,
    lengths_agree,
    spotify_id,
)

logger = logging.getLogger(__name__)

BUSY_TIMEOUT_SECONDS = 10
# Items per query: a range of IDs, or an IN list, each its own short read (homelab#2665).
READ_BATCH = 500
# The fields a pair is judged on and shown with, fixed columns or flexible attributes.
FIELDS = ("title", "artist", "artist_credit", "mb_artistid", "length", "added", "isrc", "mb_trackid",
          "spotify_ids", "spotify_url", "via")

CERTAIN = "certain"
CHECK = "check by hand"
DUPLICATES = "duplicates"


class Row(dict):
    """An item as read from the database: a dict with attribute access, as the identity helpers read
    a beets item."""

    @property
    def id(self) -> int:
        return self["id"]

    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


@dataclasses.dataclass
class Finding:
    tier: str
    a: Row
    b: Row
    spotify: list[str]  # the Spotify IDs both hold
    isrcs: list[str]  # the ISRCs both hold
    victim: Row | None  # the item holding IDs beyond the shared ones, when only one does
    evidence: list[str]  # what tells the two apart, or that nothing does

    @property
    def other(self) -> Row | None:
        if self.victim is None:
            return None
        return self.b if self.victim is self.a else self.a


@dataclasses.dataclass
class Report:
    findings: list[Finding]
    duplicates: int  # pairs sharing an ID that look like one track, counted only
    groups: int  # titles held by two or more items
    scanned: int


def _text(value):
    if isinstance(value, (bytes, memoryview)):
        return os.fsdecode(bytes(value))
    return value


_WORD = re.compile(r"\w+")


def title_key(title: str | None) -> str:
    """The key items are grouped by: the lowercased title's words, in any script, once edition notes
    are dropped as :func:`~music_fetch.usenet.clean_album` drops them, else the title lowercased.  Two
    titles that are one lowercased (the import's key) always share it.  Not :func:`~music_fetch.usenet.words`, which
    folds every title through unidecode: two thirds of the audit's time on 36,000 items."""
    lowered = (title or "").strip().lower()
    return " ".join(_WORD.findall(clean_album(lowered))) or lowered


def _chunks(ids: list[int]) -> Iterator[list[int]]:
    for start in range(0, len(ids), READ_BATCH):
        yield ids[start:start + READ_BATCH]


def read(db: Path) -> tuple[list[Row], int, int]:
    """The items whose title another item shares, with :data:`FIELDS`; the number of items read; and
    the number of shared titles.  Opened read-only and closed before returning; each query reads at
    most :data:`READ_BATCH` items, so an import commits between two."""
    uri = f"file:{quote(str(db))}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_SECONDS)) as conn:
        ((top,),) = conn.execute("SELECT coalesce(max(id), 0) FROM items").fetchall()
        by_title: dict[str, list[int]] = {}
        scanned = 0
        for start in range(1, top + 1, READ_BATCH):
            rows = conn.execute("SELECT id, title FROM items WHERE id BETWEEN ? AND ?",
                                (start, start + READ_BATCH - 1)).fetchall()
            scanned += len(rows)
            for item_id, title in rows:
                if key := title_key(_text(title)):
                    by_title.setdefault(key, []).append(item_id)
        shared = [held for held in by_title.values() if len(held) > 1]
        ids = sorted(i for held in shared for i in held)
        del by_title
        if not ids:
            return [], scanned, 0
        columns = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
        fixed = [f for f in FIELDS if f in columns]
        flexible = [f for f in FIELDS if f not in columns]
        items: dict[int, Row] = {}
        for chunk in _chunks(ids):
            marks = ",".join("?" * len(chunk))
            for r in conn.execute(f'SELECT id, {", ".join(fixed)} FROM items '  # noqa: S608 — known columns
                                  f"WHERE id IN ({marks})", chunk):
                items[r[0]] = Row({"id": r[0], **{f: _text(v) for f, v in zip(fixed, r[1:])}})
            if flexible:
                keys = ",".join("?" * len(flexible))
                for entity, key, value in conn.execute(
                        f"SELECT entity_id, key, value FROM item_attributes "  # noqa: S608 — placeholders only
                        f"WHERE key IN ({keys}) AND entity_id IN ({marks})", [*flexible, *chunk]):
                    if entity in items:
                        items[entity][key] = _text(value)
    return [items[i] for i in ids if i in items], scanned, len(shared)


def _role(a: Row, b: Row, mine: set[str], theirs: set[str]) -> Row | None:
    """The one of *a* and *b* that holds IDs of a kind beyond the ones both hold, when only one does."""
    shared = mine & theirs
    a_more, b_more = bool(mine - shared), bool(theirs - shared)
    if a_more and not b_more:
        return a
    if b_more and not a_more:
        return b
    return None


def victim_of(a: Row, b: Row) -> Row | None:
    """The item that took the other's IDs, by the bug's signature: it holds Spotify IDs (else ISRCs)
    beyond the shared ones and the other holds none beyond them.  None when the IDs don't say, or the
    two kinds say opposite things."""
    sa, sb, ia, ib = item_spotify_ids(a), item_spotify_ids(b), item_isrcs(a), item_isrcs(b)
    by_spotify = _role(a, b, sa, sb) if sa & sb else None
    if sa & sb and by_spotify is None and sa != sb:
        return None  # each holds Spotify IDs the other doesn't
    by_isrc = _role(a, b, ia, ib) if ia & ib else None
    roles = {id(r): r for r in (by_spotify, by_isrc) if r is not None}
    return next(iter(roles.values())) if len(roles) == 1 else None


def _length(item: Row) -> float:
    try:
        return float(item.get("length") or 0)
    except (TypeError, ValueError):
        return 0.0


def evidence(a: Row, b: Row) -> tuple[bool, bool, list[str]]:
    """Whether *a* and *b* are plainly two tracks, whether they could be one, and why."""
    artists_apart = different_artist(artist_credit(a), artist_credit(b))
    la, lb = _length(a), _length(b)
    lengths_apart = bool(la and lb) and not lengths_agree(la, lb)
    mb_a, mb_b = (a.get("mb_trackid") or "").strip().lower(), (b.get("mb_trackid") or "").strip().lower()
    mb_apart = bool(mb_a and mb_b) and mb_a != mb_b
    why = []
    why.append("artists share nothing" if artists_apart else "artists alike or not comparable")
    if la and lb:
        why.append(f"{abs(la - lb):.0f}s apart" if lengths_apart else "lengths agree")
    else:
        why.append("a length unknown")
    if mb_apart:
        why.append("different MusicBrainz recordings")
    elif mb_a and mb_a == mb_b:
        why.append("one MusicBrainz recording")
    different = artists_apart and (lengths_apart or mb_apart)
    one_track = not artists_apart and not lengths_apart and not mb_apart
    return different, one_track, why


def judge(a: Row, b: Row) -> Finding | None:
    """The finding for a same-titled pair, or None when they share no ID."""
    spotify = sorted(item_spotify_ids(a) & item_spotify_ids(b))
    isrcs = sorted(item_isrcs(a) & item_isrcs(b))
    if not spotify and not isrcs:
        return None
    victim = victim_of(a, b)
    different, one_track, why = evidence(a, b)
    if victim is not None and different:
        tier = CERTAIN
    elif victim is None and one_track:
        tier = DUPLICATES
    else:
        tier = CHECK
    return Finding(tier, a, b, spotify, isrcs, victim, why)


def audit(items: Iterable[Row], scanned: int, groups: int) -> Report:
    """Judge each pair of same-titled items that share a Spotify ID or ISRC."""
    by_title: dict[str, list[Row]] = {}
    for item in items:
        by_title.setdefault(title_key(item.get("title")), []).append(item)
    findings, duplicates = [], 0
    for _, held in sorted(by_title.items()):
        if len(held) < 2:
            continue
        holders: dict[str, list[Row]] = {}
        for item in held:
            for key in {*(f"s:{s}" for s in item_spotify_ids(item)), *(f"i:{c}" for c in item_isrcs(item))}:
                holders.setdefault(key, []).append(item)
        pairs = {(x.id, y.id): (x, y) for sharing in holders.values()
                 for x, y in combinations(sorted(sharing, key=lambda i: i.id), 2)}
        for _, (x, y) in sorted(pairs.items()):
            finding = judge(x, y)
            if finding is None:
                continue
            if finding.tier == DUPLICATES:
                duplicates += 1
            else:
                findings.append(finding)
    return Report(findings, duplicates, groups, scanned)


def _added(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def _describe(item: Row) -> str:
    return (f"{item.id} ({item.get('artist') or '?'} — {item.get('title') or '?'}, {item.get('via') or 'manual'}, "
            f"{_length(item):.0f}s, added {_added(item.get('added'))}; spotify_ids "
            f"{item.get('spotify_ids') or 'none'}, spotify_url ID {spotify_id(item.get('spotify_url')) or 'none'}, "
            f"isrc {item.get('isrc') or 'none'})")


def _held(f: Finding) -> str:
    return ", ".join([*(f"Spotify ID {s}" for s in f.spotify), *(f"ISRC {c}" for c in f.isrcs)])


def fixes(f: Finding) -> list[str]:
    """What takes the other item's IDs off the victim, a line per ID: the exact call where a tool can,
    else why none can."""
    v = f.victim
    assert v is not None
    url_id = spotify_id(v.get("spotify_url"))
    own = sorted(item_spotify_ids(v) - set(f.spotify))
    out = []
    for sid in f.spotify:
        if sid == url_id:
            out.append(f"Spotify ID {sid}: no tool takes it off, as it is {v.id}'s spotify_url (where this bug "
                       f"wrote it): unlink_spotify_id refuses an item's spotify_url ID, and no tool rewrites "
                       f"spotify_url; {v.id}'s own: {', '.join(own) or 'none left'}")
        else:
            out.append(f'unlink_spotify_id(id={v.id}, track="{sid}")')
    for code in f.isrcs:
        out.append(f"ISRC {code}: no tool takes a valid ISRC off an item (clear_bad_isrcs takes only values that "
                   "aren't ISRCs)")
    return out


def log_report(report: Report) -> None:
    """One ``[SWAPPED]`` block per certain pair, then one ``[SWAPCHECK]`` block per pair to check by
    hand, then the counts and what the audit didn't look at."""
    certain = [f for f in report.findings if f.tier == CERTAIN]
    check = [f for f in report.findings if f.tier == CHECK]
    for f in certain:
        other = f.other
        assert f.victim is not None and other is not None
        logger.info("[SWAPPED] %s holds %s's %s: %s", f.victim.id, other.id, _held(f), "; ".join(f.evidence))
        logger.info("    took them: %s", _describe(f.victim))
        logger.info("    their own: %s", _describe(other))
        for line in fixes(f):
            logger.info("    fix: %s", line)
    for f in check:
        logger.info("[SWAPCHECK] %s ~ %s share %s: %s", f.a.id, f.b.id, _held(f), "; ".join(f.evidence))
        for item in (f.a, f.b):
            logger.info("    %s", _describe(item))
        if f.victim is None:
            logger.info("    the IDs don't say which took the other's: compare_items(ids=[%s, %s]), get_item on each",
                        f.a.id, f.b.id)
        else:
            logger.info("    %s holds IDs beyond the shared ones, so it took them if these are two tracks: "
                        "compare_items(ids=[%s, %s]) first; if two tracks:", f.victim.id, f.a.id, f.b.id)
            for line in fixes(f):
                logger.info("    fix: %s", line)
    logger.info("Swapped IDs: %d certain, %d to check by hand, %d pair(s) sharing an ID that look like one "
                "track (duplicates: audit_dupes), among %d title(s) two or more of the %d items hold. "
                "Only same-titled pairs with a shared Spotify ID or ISRC are judged, from the beets database "
                "alone: an item whose title changed since its import, or whose other item has left the "
                "library, isn't seen; sources, via and the Usenet job folder the bug also took aren't judged",
                len(certain), len(check), report.duplicates, report.groups, report.scanned)


def run(db: Path | None = None) -> Report:
    from music_scan.library import LIBRARY_DB  # noqa: PLC0415

    items, scanned, groups = read(db or LIBRARY_DB)
    report = audit(items, scanned, groups)
    log_report(report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(prog="music-audit-swapped-ids", description=__doc__.splitlines()[0])
    parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    run()
