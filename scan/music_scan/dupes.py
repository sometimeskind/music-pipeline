"""music-audit-dupes: find and merge fuzzy duplicates in the library (#210).

The duplicate hook stops most duplicates at import; this finds the ones already
there: the same recording on the album and the single, imported before track
identity (#176), or with no IDs at all.  Spotify IDs are primary (homelab#1889);
ISRC, MusicBrainz recording ID and AcoustID fingerprints are supporting evidence.

Two tiers, grouped transitively:

* **Certain:** a shared Spotify ID, ISRC or MusicBrainz recording ID, or an
  AcoustID match (a shared ``acoustid_id``, or fingerprints within
  :data:`MATCH_BER`) with lengths within :data:`LENGTH_SLACK` seconds and the
  same title once edition suffixes are dropped.
* **Uncertain:** the same title and artist after the album matcher's
  normalisation (#197), or an AcoustID match with lengths further apart.  An
  AcoustID match between different titles is uncertain too: one file is
  likely the other's audio under the wrong tags (a wrong download, #221), which
  ``music-audit-lengths --replace`` fixes and a merge would make worse.
  Reported only: a demo, live take or radio edit must stay its own item.  A
  title+artist pair whose ISRCs are both known and disjoint is the duplicate
  hook's ``[SPLIT]`` decision, so it is counted as distinct and not listed.

Fingerprints are compared only between items that share an artist word, so the
run stays a few thousand comparisons rather than every pair.

``--apply`` merges each certain group onto one item, chosen by: length closest
to Spotify's duration for any of the group's Spotify IDs (from the ``.spotdl``
snapshots, no Spotify calls), then bitrate, then playlists, then the older
item.  The keeper takes the others' ``spotify_ids``, ``isrc``, ``sources`` and
a missing ``mb_trackid``; their files move to ``quarantine/replaced/`` (never
deleted) and their rows leave the database.  The ``.m3u`` files are then
regenerated, so every entry resolves to the keeper.  The keeper is not moved:
``music-canon-albums --apply`` files it under its canonical album afterwards.

``--fingerprint`` fingerprints items that have no ``acoustid_fingerprint`` (dry
run counts them; with ``--apply`` it computes and stores them in the database
only, leaving the files alone).  Run it before the first audit.  New asis
imports are fingerprinted by the plugin and ``music-audit-lengths --replace``
refingerprints, but items replaced before that carry the old audio's
fingerprint: ``--fingerprint ITEM_ID...`` recomputes those.

Each audit pushes ``music_dupes_groups{tier=...}`` to the Pushgateway.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import shutil
import struct
import time
from collections import defaultdict
from collections.abc import Callable
from itertools import combinations
from pathlib import Path

from music_fetch.usenet import clean_album, normalise, words
from music_scan.audit import LIBRARY, REPLACED, item_path, pipeline_lock
from music_scan.identity import (
    add_isrcs, add_to_list, fingerprint, item_isrcs, item_spotify_ids, spotify_id, split_list,
)
from music_scan.metrics import _gauge, _push

logger = logging.getLogger(__name__)

CERTAIN, UNCERTAIN = "certain", "uncertain"
# An AcoustID match is certain only with lengths this close (seconds).
LENGTH_SLACK = 2.0
# Fingerprint comparison: a window of WINDOW sub-fingerprints (about 8 a second)
# slid up to MAX_SHIFT either way; a bit error rate at or under MATCH_BER is a
# match.  Different recordings sit near 0.5, but repetitive tracks by one artist
# score 0.10-0.15 against each other (The Field); re-encodes and remasters of one
# recording scored 0.00-0.06 on the library (#221).
WINDOW = 240
MAX_SHIFT = 40
MIN_FRAMES = 40
MATCH_BER = 0.08
_ARTIST_STOP = frozenset({"the", "and", "feat", "ft", "featuring", "with", "vs", "x"})


@dataclasses.dataclass
class Group:
    tier: str
    items: list
    # (item id, item id, evidence), one per pair that joined the group.
    evidence: list[tuple[int, int, str]]


class _Union:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        self.parent[self.find(a)] = self.find(b)


def decode(fp: str) -> list[int]:
    """A beets ``acoustid_fingerprint`` (compressed, base64) as unsigned 32-bit sub-fingerprints."""
    import chromaprint  # noqa: PLC0415 — pyacoustid's ctypes binding to libchromaprint

    data = fp.encode() if isinstance(fp, str) else fp
    frames, _ = chromaprint.decode_fingerprint(data)
    return [f & 0xFFFFFFFF for f in frames]


def _pack(frames: list[int]) -> int:
    return int.from_bytes(struct.pack(f"<{len(frames)}I", *frames), "little")


def bit_error_rate(a: list[int], b: list[int]) -> float:
    """The lowest share of differing bits between *a* and *b* over the shifts tried."""
    best = 1.0
    for shift in range(-MAX_SHIFT, MAX_SHIFT + 1):
        sa, sb = max(0, -shift), max(0, shift)
        n = min(WINDOW, len(a) - sa, len(b) - sb)
        if n < MIN_FRAMES:
            continue
        diff = _pack(a[sa:sa + n]) ^ _pack(b[sb:sb + n])
        best = min(best, diff.bit_count() / (32 * n))
    return best


def _base_title(item) -> str:
    """The title without edition suffixes: ``Get Down Tonight - 2004 Remaster`` → ``get down tonight``."""
    return normalise(clean_album(str(item.title or "")))


def _title_key(item) -> str:
    return normalise(str(item.title or ""))


def _artist_key(item) -> str:
    artist = str(item.artist or item.albumartist or "")
    for cut in (" feat.", " feat ", " ft.", " featuring ", ","):
        artist = artist.split(cut)[0]
    return normalise(artist)


def _artist_words(item) -> set[str]:
    names = f"{item.artist or ''} {item.albumartist or ''}"
    return {w for w in words(names) if len(w) > 1 and w not in _ARTIST_STOP}


def find_dupes(items: list, decoder: Callable[[str], list[int]] = decode) -> tuple[list[Group], list[Group], int]:
    """Certain groups, uncertain groups and the number of distinct (``[SPLIT]``) pairs."""
    by_id = {item.id: item for item in items}
    certain = _Union()
    certain_edges: list[tuple[int, int, str]] = []
    uncertain_edges: list[tuple[int, int, str]] = []

    def join(a, b, why: str) -> None:
        if certain.find(a.id) != certain.find(b.id):
            certain.union(a.id, b.id)
            certain_edges.append((a.id, b.id, why))

    # Shared IDs.
    seen: dict[tuple[str, str], object] = {}
    for item in items:
        keys = [("spotify", s) for s in sorted(item_spotify_ids(item))]
        keys += [("isrc", c) for c in sorted(item_isrcs(item))]
        if mbid := item.get("mb_trackid"):
            keys.append(("mbid", mbid))
        for key in keys:
            if key in seen:
                join(seen[key], item, f"{key[0]} {key[1]}")
            else:
                seen[key] = item

    # AcoustID, between items sharing an artist word.
    by_word: dict[str, list] = defaultdict(list)
    for item in items:
        if item.get("acoustid_fingerprint") or item.get("acoustid_id"):
            for w in _artist_words(item):
                by_word[w].append(item)
    frames: dict[int, list[int]] = {}

    def frames_of(item) -> list[int]:
        if item.id not in frames:
            fp = item.get("acoustid_fingerprint")
            try:
                frames[item.id] = decoder(fp) if fp else []
            except Exception as exc:  # noqa: BLE001 — one bad fingerprint must not stop the audit
                logger.warning("  item %s: unreadable fingerprint (%s)", item.id, exc)
                frames[item.id] = []
        return frames[item.id]

    compared: set[tuple[int, int]] = set()
    for bucket in by_word.values():
        for a, b in combinations(bucket, 2):
            pair = (min(a.id, b.id), max(a.id, b.id))
            if pair in compared or certain.find(a.id) == certain.find(b.id):
                continue
            compared.add(pair)
            if a.get("acoustid_id") and a.get("acoustid_id") == b.get("acoustid_id"):
                why = f"acoustid {a.get('acoustid_id')}"
            elif (fa := frames_of(a)) and (fb := frames_of(b)) and (ber := bit_error_rate(fa, fb)) <= MATCH_BER:
                why = f"fingerprint ber={ber:.2f}"
            else:
                continue
            gap = abs(float(a.length or 0) - float(b.length or 0))
            if gap > LENGTH_SLACK:
                uncertain_edges.append((a.id, b.id, f"{why} but {gap:.0f}s apart"))
            elif _base_title(a) != _base_title(b):
                uncertain_edges.append((a.id, b.id, f"{why}, {gap:.0f}s apart, but the titles differ: "
                                                    "wrong audio? (music-audit-lengths --replace)"))
            else:
                join(a, b, f"{why}, {gap:.0f}s apart")

    # Title + artist.
    distinct = 0
    by_words: dict[tuple[str, str], list] = defaultdict(list)
    for item in items:
        if (key := (_title_key(item), _artist_key(item)))[0] and key[1]:
            by_words[key].append(item)
    for bucket in by_words.values():
        for a, b in combinations(bucket, 2):
            if certain.find(a.id) == certain.find(b.id):
                continue
            ia, ib = item_isrcs(a), item_isrcs(b)
            if ia and ib and not ia & ib:
                distinct += 1
                continue
            uncertain_edges.append((a.id, b.id, "title+artist"))

    groups: dict[int, Group] = {}
    for a, b, why in certain_edges:
        group = groups.setdefault(certain.find(a), Group(CERTAIN, [], []))
        group.evidence.append((a, b, why))
    for root, group in groups.items():
        group.items = sorted((i for i in items if certain.find(i.id) == root), key=lambda i: i.id)

    # Uncertain groups join certain groups, so one listed group is one recording each side.
    loose = _Union()
    uncertain_groups: dict[int, Group] = {}
    uncertain_edges = [(a, b, why) for a, b, why in uncertain_edges if certain.find(a) != certain.find(b)]
    for a, b, _ in uncertain_edges:
        loose.union(certain.find(a), certain.find(b))
    for a, b, why in uncertain_edges:
        group = uncertain_groups.setdefault(loose.find(certain.find(a)), Group(UNCERTAIN, [], []))
        group.evidence.append((a, b, why))
    for group in uncertain_groups.values():
        ids = {i for a, b, _ in group.evidence for i in (a, b)}
        group.items = [by_id[i] for i in sorted(ids)]
    return list(groups.values()), list(uncertain_groups.values()), distinct


def spotify_durations(spotdl_dir: Path) -> dict[str, float]:
    """Spotify track ID → duration (seconds), from the ``.spotdl`` snapshots."""
    from music_scan.scan import _spotdl_songs  # noqa: PLC0415

    durations: dict[str, float] = {}
    for f in sorted(spotdl_dir.glob("*.spotdl")):
        for song in _spotdl_songs(f):
            sid = song.get("song_id") or spotify_id(song.get("url"))
            if sid and song.get("duration"):
                durations.setdefault(sid, float(song["duration"]))
    return durations


def expected_lengths(group: Group, durations: dict[str, float]) -> list[float]:
    ids = {sid for item in group.items for sid in item_spotify_ids(item)}
    return sorted({durations[sid] for sid in ids if sid in durations})


def keep_order(item, expected: list[float]) -> tuple:
    """Sort key: the item to keep sorts first."""
    off = min((abs(float(item.length or 0) - d) for d in expected), default=0.0)
    return (round(off), -int(item.bitrate or 0), -len(split_list(item.get("sources"))), item.id)


def _rel(item) -> str:
    path = item_path(item)
    return str(path.relative_to(LIBRARY)) if path.is_relative_to(LIBRARY) else str(path)


def report(certain: list[Group], uncertain: list[Group], distinct: int, durations: dict[str, float]) -> None:
    for group in certain:
        expected = expected_lengths(group, durations)
        keeper, *_ = sorted(group.items, key=lambda i: keep_order(i, expected))
        _log_group(group, keeper, expected)
    for group in uncertain:
        _log_group(group, None, expected_lengths(group, durations))
    logger.info(
        "Dupes: %d certain group(s) (%d surplus item(s)), %d uncertain group(s), %d distinct pair(s) (disjoint ISRCs)",
        len(certain), sum(len(g.items) - 1 for g in certain), len(uncertain), distinct,
    )


def _log_group(group: Group, keeper, expected: list[float]) -> None:
    first = group.items[0]
    logger.info("[%s] %s — %s: %s", group.tier.upper(), first.artist or first.albumartist or "?", first.title or "?",
                "; ".join(f"{a}~{b} {why}" for a, b, why in group.evidence))
    for item in group.items:
        role = "    " if keeper is None else ("keep" if item is keeper else "drop")
        logger.info("    %s %s  %.0fs %dk  %s  %s", role, item.id, float(item.length or 0),
                    int(item.bitrate or 0) // 1000, item.get("sources") or "-", _rel(item))
    if expected:
        logger.info("    Spotify: %s", ", ".join(f"{d:.0f}s" for d in expected))


def _free(path: Path) -> Path:
    """*path*, or the first ``.N`` variant that doesn't exist: never overwrite a replaced file."""
    candidate, n = path, 1
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}.{n}{path.suffix}")
        n += 1
    return candidate


def merge(keeper, losers: list, replaced_dir: Path = REPLACED) -> list[Path]:
    """Merge *losers* onto *keeper*; their files go to *replaced_dir*.  Returns where they went."""
    write = False
    for loser in losers:
        for sid in sorted(item_spotify_ids(loser) - item_spotify_ids(keeper)):
            add_to_list(keeper, "spotify_ids", sid)
        for source in split_list(loser.get("sources")):
            add_to_list(keeper, "sources", source)
        write |= add_isrcs(keeper, split_list(loser.get("isrc"), ";"))
        if loser.get("mb_trackid") and not keeper.get("mb_trackid"):
            keeper["mb_trackid"] = loser.get("mb_trackid")
            write = True
    keeper.store()
    if write:
        keeper.try_write()
    moved = []
    for loser in losers:
        old = item_path(loser)
        if old.exists():
            backup = _free(replaced_dir / f"{loser.id}-{old.name}")
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old), backup)
            moved.append(backup)
            logger.info("[MERGE] keep %s ← %s; old file → %s", keeper.id, loser.id, backup)
        else:
            logger.warning("[MERGE] keep %s ← %s; its file %s was already gone", keeper.id, loser.id, old)
        loser.remove(delete=False)
    return moved


def fingerprint_missing(items: list, apply: bool, ids: list[int] | None = None) -> int:
    """Fingerprint items with no ``acoustid_fingerprint``, or the items *ids* whatever they
    have (a stale one); with *apply*, store them.  Returns the count."""
    if ids:
        todo = [i for i in items if i.id in set(ids)]
        if missing := sorted(set(ids) - {i.id for i in todo}):
            logger.warning("No library item with id %s", ", ".join(map(str, missing)))
        logger.info("Refingerprinting %d item(s)", len(todo))
    else:
        todo = [i for i in items if not i.get("acoustid_fingerprint")]
        logger.info("%d of %d item(s) have no fingerprint", len(todo), len(items))
    if not apply:
        logger.info("Dry run — nothing fingerprinted. Re-run with --fingerprint --apply.")
        return len(todo)

    done = []
    for n, item in enumerate(todo, 1):
        try:
            item["acoustid_fingerprint"] = fingerprint(item_path(item))
        except Exception as exc:  # noqa: BLE001 — log and go on to the next file
            logger.warning("  item %s (%s): %s", item.id, _rel(item), exc)
            continue
        if ids:
            item["acoustid_id"] = ""  # looked up from the old audio
        done.append(item)
        if n % 100 == 0:
            logger.info("  %d/%d", n, len(todo))
    with pipeline_lock():
        for item in done:
            item.store()  # the database only: the files are left alone
    logger.info("Stored %d fingerprint(s); %d failed", len(done), len(todo) - len(done))
    return len(done)


def push_metrics(certain: int, uncertain: int) -> None:
    _push("\n".join([
        "# TYPE music_dupes_groups gauge",
        f'music_dupes_groups{{tier="{CERTAIN}"}} {certain}',
        f'music_dupes_groups{{tier="{UNCERTAIN}"}} {uncertain}',
        _gauge("music_dupes_last_run_timestamp_seconds", int(time.time())),
    ]), "music_audit_dupes")


def run(apply: bool = False, fingerprints: list[int] | None = None) -> tuple[list[Group], list[Group]]:
    """Audit, and with *apply* merge; with *fingerprints* (a list, empty for every item
    missing one) fingerprint instead."""
    from music_scan.library import LIBRARY_DB, MusicLibrary  # noqa: PLC0415
    from music_scan.navidrome import trigger_scan  # noqa: PLC0415
    from music_scan.scan import SPOTDL_DIR, regen_playlists  # noqa: PLC0415

    with MusicLibrary(LIBRARY_DB) as lib:
        items = lib.all_items()
        if fingerprints is not None:
            fingerprint_missing(items, apply, fingerprints)
            return [], []
        certain, uncertain, distinct = find_dupes(items, decode)
        durations = spotify_durations(SPOTDL_DIR)
        report(certain, uncertain, distinct, durations)
        left = len(certain)
        if apply and certain:
            with pipeline_lock():
                for group in certain:
                    keeper, *losers = sorted(group.items, key=lambda i: keep_order(i, expected_lengths(group, durations)))
                    merge(keeper, losers, REPLACED)
                    left -= 1
                counts = regen_playlists()
            logger.info("Merged %d group(s); playlists regenerated: %s", len(certain),
                        ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
        elif certain:
            logger.info("Dry run — nothing merged. Re-run with --apply to merge the certain tier.")
    push_metrics(left, len(uncertain))
    if apply and certain:
        trigger_scan()
    return certain, uncertain


def main() -> None:
    parser = argparse.ArgumentParser(prog="music-audit-dupes", description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="merge the certain tier (default: dry run)")
    parser.add_argument("--fingerprint", nargs="*", type=int, metavar="ITEM_ID",
                        help="instead: fingerprint items that have none, or these items "
                             "(stored only with --apply)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    run(apply=args.apply, fingerprints=args.fingerprint)
