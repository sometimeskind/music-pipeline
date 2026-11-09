"""music-audit-case-twins: library items whose paths differ only in case (homelab#2742).

Navidrome links a ``.m3u`` line to a file with SQLite's ``NOCASE`` over the
NFC and NFD forms of the path, so of two files whose paths differ only in
ASCII case (``04 - Look At Where We Are.m4a``, ``04 - Look at Where We Are.m4a``;
an album folder ``Lose``, ``LOSE``) or only in Unicode normalisation, a
playlist plays whichever its query returns, not the item the store lists, and
the two-way sync reads the other as a user edit.  The library's filesystem is
case sensitive, so the import and canon used to place such *case twins*
side by side; since homelab#2742 they treat one as a clash
(:mod:`music_scan.pathfold`).  This audit lists the twins already there.

It groups every item by its folded path (:func:`music_scan.pathfold.fold`, as
Navidrome compares) and lists each group of two or more: per item its ID, path,
``via`` and length; per pair what agrees: Spotify ID, ISRC, MusicBrainz
recording (with lengths within the duplicate audit's slack), and the stored
fingerprints, compared as ``music-audit-dupes`` does.  An ID that agrees with
the same audio is a duplicate (``merge_items``); no ID and different audio are
two recordings, kept apart and one renamed; an ID that agrees over different
audio is another master or a wrong download (``compare_items``,
``replace_item``).  An item with no stored fingerprint is not compared
(``fingerprint_items`` first).

Read-only and quick: beets' SQLite opened read-only (never through beets'
``Library``) with a busy timeout, the paths read in one query and the twins'
fields in two more, then closed before anything is compared, so it holds no
lock while it works and can't stop an import (homelab#2665).  No file is
opened, no filesystem walk, no writes, no events.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import logging
import os
import sqlite3
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from urllib.parse import quote

from music_scan.identity import item_isrcs, item_spotify_ids, lengths_agree, same_mb_recording
from music_scan.pathfold import fold

logger = logging.getLogger(__name__)

BUSY_TIMEOUT_SECONDS = 10
# The fields a pair is judged on, fixed columns or flexible attributes (beets keeps either).
FIELDS = ("title", "artist", "length", "added", "isrc", "mb_trackid", "acoustid_id", "acoustid_fingerprint",
          "spotify_ids", "spotify_url", "via", "album_record")

DUPLICATE = "duplicate"
OTHER_AUDIO = "same ID, different audio"
SAME_AUDIO = "same audio, no ID"
DISTINCT = "distinct"
UNCOMPARED = "not compared"


class Row(dict):
    """An item as read from the database: a dict with attribute access, as the identity helpers and
    :func:`music_scan.dupes.audio_match` read a beets item."""

    @property
    def id(self) -> int:
        return self["id"]

    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


@dataclasses.dataclass
class Pair:
    a: Row
    b: Row
    shared: list[str]  # the IDs that agree, as "Spotify ID X", "ISRC Y", "MusicBrainz recording Z"
    audio: str  # what the fingerprints say, or why they weren't compared
    same_audio: bool | None  # None: not compared
    verdict: str


@dataclasses.dataclass
class Group:
    items: list[Row]
    pairs: list[Pair]


@dataclasses.dataclass
class Report:
    groups: list[Group]
    scanned: int


def _text(value):
    if isinstance(value, (bytes, memoryview)):
        return os.fsdecode(bytes(value))
    return value


def read(db: Path) -> tuple[list[Row], int]:
    """The items whose folded path another item shares, with :data:`FIELDS`, and the
    number of items read.  Opened read-only, closed before returning."""
    uri = f"file:{quote(str(db))}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_SECONDS)) as conn:
        rows = conn.execute("SELECT id, path FROM items").fetchall()
        by_fold: dict[str, list[tuple[int, str]]] = {}
        for item_id, path in rows:
            if path:
                by_fold.setdefault(fold(_text(path)), []).append((item_id, _text(path)))
        twins = {i: p for held in by_fold.values() if len(held) > 1 for i, p in held}
        if not twins:
            return [], len(rows)
        columns = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
        fixed = [f for f in FIELDS if f in columns]
        marks = ",".join("?" * len(twins))
        ids = sorted(twins)
        items = {r[0]: Row({"id": r[0], "path": twins[r[0]], **{f: _text(v) for f, v in zip(fixed, r[1:])}})
                 for r in conn.execute(f'SELECT id, {", ".join(fixed)} FROM items '  # noqa: S608 — known columns
                                       f"WHERE id IN ({marks})", ids)}
        flexible = [f for f in FIELDS if f not in columns]
        if flexible:
            keys = ",".join("?" * len(flexible))
            for entity, key, value in conn.execute(
                    f"SELECT entity_id, key, value FROM item_attributes "  # noqa: S608 — placeholders only
                    f"WHERE key IN ({keys}) AND entity_id IN ({marks})", [*flexible, *ids]):
                if entity in items:
                    items[entity][key] = _text(value)
    return [items[i] for i in ids if i in items], len(rows)


def _shared(a: Row, b: Row) -> list[str]:
    out = [f"Spotify ID {s}" for s in sorted(item_spotify_ids(a) & item_spotify_ids(b))]
    out += [f"ISRC {c}" for c in sorted(item_isrcs(a) & item_isrcs(b))]
    if same_mb_recording(a.get("mb_trackid"), a.get("length"), b.get("mb_trackid"), b.get("length")):
        out.append(f"MusicBrainz recording {a.get('mb_trackid')}")
    return out


def _mb_apart(a: Row, b: Row) -> str:
    """A note when the MusicBrainz recording agrees but the lengths don't (a sibling, homelab#2540)."""
    mbid = a.get("mb_trackid")
    if mbid and mbid == b.get("mb_trackid") and not lengths_agree(a.get("length"), b.get("length")):
        return f"MusicBrainz recording {mbid} agrees, but {abs(float(a.length or 0) - float(b.length or 0)):.0f}s apart"
    return ""


def judge(a: Row, b: Row, frames_of: Callable[[object], list[int]]) -> Pair:
    from music_scan.dupes import CERTAIN, MATCH_BER, audio_match, bit_error_rate  # noqa: PLC0415

    shared = _shared(a, b)
    match = audio_match(a, b, frames_of)
    if match is not None:
        tier, evidence = match
        audio, same = (f"same audio: {evidence}" if tier == CERTAIN else f"audio matches, {evidence}"), True
    elif unprinted := [i.id for i in (a, b) if not frames_of(i)]:
        audio, same = f"no stored fingerprint on {', '.join(map(str, unprinted))}", None
    else:
        audio, same = (f"different audio: ber={bit_error_rate(frames_of(a), frames_of(b)):.2f}, "
                       f"a match is at most {MATCH_BER:.2f}"), False
    if same is None:
        verdict = UNCOMPARED
    elif shared:
        verdict = DUPLICATE if same else OTHER_AUDIO
    else:
        verdict = SAME_AUDIO if same else DISTINCT
    return Pair(a, b, shared, audio, same, verdict)


def audit(items: Iterable[Row], scanned: int, decoder: Callable[[str], list[int]] | None = None) -> Report:
    """Group *items* by folded path and judge each pair of a group."""
    from music_scan.dupes import decode, frames_reader  # noqa: PLC0415

    frames_of = frames_reader(decoder or decode)
    by_fold: dict[str, list[Row]] = {}
    for item in items:
        by_fold.setdefault(fold(item["path"]), []).append(item)
    groups = []
    for _, held in sorted(by_fold.items()):
        if len(held) < 2:
            continue
        held = sorted(held, key=lambda i: i.id)
        groups.append(Group(held, [judge(a, b, frames_of) for a, b in combinations(held, 2)]))
    return Report(groups, scanned)


def _added(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def _describe(item: Row) -> str:
    record = f", album record {item.get('album_record')}" if item.get("album_record") else ""
    return (f"    {item.id}  {item['path']}  ({item.get('via') or 'manual'}{record}, "
            f"{float(item.get('length') or 0):.0f}s, added {_added(item.get('added'))})")


FIXES = {
    DUPLICATE: "one track twice: merge_items",
    OTHER_AUDIO: "an ID agrees over other audio (another master, or a wrong download): compare_items, "
                 "then replace_item or keep both apart",
    SAME_AUDIO: "the same audio with no ID agreeing: compare_items, then merge_items",
    DISTINCT: "two recordings: keep both, and give one another path",
    UNCOMPARED: "fingerprint_items first, then run this again",
}


def log_report(report: Report) -> None:
    """One ``[CASETWIN]`` block per group, then the counts by verdict."""
    for group in report.groups:
        logger.info("[CASETWIN] %d item(s) on one path as Navidrome matches it:", len(group.items))
        for item in group.items:
            logger.info("%s", _describe(item))
        for p in group.pairs:
            ids = ", ".join(p.shared) if p.shared else "no ID agrees" + (f" ({n})" if (n := _mb_apart(p.a, p.b)) else "")
            logger.info("    %s ~ %s: %s; %s → %s: %s", p.a.id, p.b.id, ids, p.audio, p.verdict, FIXES[p.verdict])
    verdicts: dict[str, int] = {}
    for group in report.groups:
        for p in group.pairs:
            verdicts[p.verdict] = verdicts.get(p.verdict, 0) + 1
    logger.info("Case twins: %d group(s), %d item(s) of %d; pairs: %s. Paths are compared as Navidrome links a "
                ".m3u line (NFC, ASCII case folded), from the beets database alone: a file on disk that no item "
                "holds isn't seen",
                len(report.groups), sum(len(g.items) for g in report.groups), report.scanned,
                ", ".join(f"{verdicts[v]} {v}" for v in FIXES if v in verdicts) or "none")


def run(db: Path | None = None, decoder: Callable[[str], list[int]] | None = None) -> Report:
    from music_scan.library import LIBRARY_DB  # noqa: PLC0415

    items, scanned = read(db or LIBRARY_DB)
    report = audit(items, scanned, decoder)
    log_report(report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(prog="music-audit-case-twins", description=__doc__.splitlines()[0])
    parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    run()
