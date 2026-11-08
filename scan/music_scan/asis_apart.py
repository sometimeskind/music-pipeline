"""The as-is pass's other recordings (homelab#2698).

The as-is pass imports quarantined spotdl files on their own tags, with
``duplicate_action: skip``: under ``--asis`` beets sends the plugin no
``import_task_choice``, so its duplicate hook doesn't run, and the main
config's ``remove`` would delete the library item (#202).  beets' own test for
a singleton is its artist and title, though.  A file that shares those with a
library item but no ID (Spotify track ID, ISRC or MusicBrainz recording) is
another recording, a live take or a remaster, which the main import brings in
beside it (``[SPLIT]``, ``[APART]``, #255).  Skipped, it stayed in quarantine
for good, retried at every scan without a log line.

:func:`move_apart` takes those files out of the as-is staging tree, and the
scan imports them with :data:`KEEP_CONFIG`.  A file whose duplicate shares an
ID is the same recording, and stays skipped as before.  Two staged copies of
one recording are each another recording than their library duplicate, so
only one of them moves (:func:`one_per_recording`); the other is skipped
beside it, and at the next scan it shares an ID with the new item.

``python -m music_scan.asis_apart`` counts what the quarantine holds, read-only
and counts only.  It uses nothing newer than this module, so piped into a pod
running an image without it (``python - < asis_apart.py``) it counts too.
"""

from __future__ import annotations

import functools
import logging
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import Callable, Iterator, NamedTuple

logger = logging.getLogger(__name__)

KEEP_CONFIG = "import:\n  duplicate_action: keep\n"
# beets' default import.duplicate_keys.item, which the pipeline's config doesn't set.
DUPLICATE_KEYS = ["artist", "title"]

NO_DUPLICATE = "no duplicate"
SAME = "duplicate shares an ID"
SPLIT = "other recording, ISRCs differ"
APART = "other recording, no shared ID"
UNREADABLE = "unreadable"
OTHER_RECORDING = frozenset({SPLIT, APART})


class Verdict(NamedTuple):
    """How the as-is pass's duplicate test sees one file."""

    kind: str
    incoming: object = None
    duplicates: tuple = ()
    # The beets item read from the file, and where the file is: in staging, or
    # under the apart dir once move_apart has moved it.
    item: object = None
    path: Path | None = None


def eligible(quarantine: Path) -> Iterator[Path]:
    """The quarantined audio files the as-is pass takes: every tag it needs, in
    an allowed subtree.

    Only spotdl leftovers (``spotdl/<playlist>/``) and loose files at the
    quarantine root are eligible; every other subtree is skipped (#202).  An
    allow-list, so a new quarantine subtree is skipped until it is known to be
    safe.  Skipped: album-mode releases (usenet/: verified by the strict match
    alone, a release beets can't match is blocklisted), the rejected downloads
    (rejected/, homelab#2546) and the files music-audit-lengths --replace
    swapped out (replaced/, #202): both carry the right tags on the wrong audio.
    """
    from mutagen import File as MutagenFile  # noqa: PLC0415 — beets dep, always available

    from music_scan.scan import _ASIS_REQUIRED_TAGS, _ASIS_SUBTREES, AUDIO_EXTS  # noqa: PLC0415

    for f in sorted(quarantine.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in AUDIO_EXTS:
            continue
        parts = f.relative_to(quarantine).parts
        if len(parts) > 1 and parts[0] not in _ASIS_SUBTREES:
            continue
        try:
            tags = MutagenFile(f, easy=True)
            if tags is None or not all(tags.get(k) for k in _ASIS_REQUIRED_TAGS):
                continue
        except Exception:
            continue
        yield f


def _incoming(item, path: Path):
    """The file's identity, as the plugin's duplicate hook reads it for an as-is import."""
    from music_fetch.album_requests import url_recording  # noqa: PLC0415
    from music_scan.identity import item_isrcs, split_list, spotify_id  # noqa: PLC0415
    from music_scan.music_pipeline import Incoming, _read_spotdl_tags  # noqa: PLC0415

    tags = _read_spotdl_tags(str(path))
    isrcs = {*split_list(tags.isrc, ";"), *item_isrcs(item)}
    recording = item.get("mb_trackid") or url_recording(tags.url) or None
    return Incoming(spotify_id(tags.url), frozenset(isrcs), recording, item.get("length"))


def library_recordings(lib):
    """The recordings *lib* holds (:class:`~music_scan.identity.Recordings`),
    read once for a whole pass: a flex-attribute query per file reads the whole
    attributes table, minutes for a quarantine's worth of files."""
    from music_scan.identity import Recordings  # noqa: PLC0415

    return Recordings(lib.items())


def _ids(incoming) -> frozenset[str]:
    from music_scan.identity import recording_ids  # noqa: PLC0415

    return recording_ids([incoming.spotify_id] if incoming.spotify_id else [], incoming.isrcs, incoming.mb_trackid)


def classify(lib, path: Path, recordings: Callable[[], object] | None = None) -> Verdict:
    """*path* against the beets library *lib*: beets' duplicates for it (its
    artist and title), and whether any of them, or any other item, is the same
    recording by ID, as the main import's duplicate hook decides
    (:func:`~music_scan.music_pipeline._find_same_recording`).

    *recordings* returns the library's :func:`library_recordings`, called only
    for a file with duplicates; a pass hands every file the same cached one.
    Without it, each call reads its own."""
    from beets.library import Item  # noqa: PLC0415

    from music_scan.music_pipeline import _different_recording  # noqa: PLC0415

    try:
        item = Item.from_path(str(path))
    except Exception:
        return Verdict(UNREADABLE, path=path)
    if not item.get("artist"):
        return Verdict(NO_DUPLICATE, item=item, path=path)  # beets skips the check without an artist
    duplicates = tuple(lib.items(item.duplicates_query(DUPLICATE_KEYS)))
    if not duplicates:
        return Verdict(NO_DUPLICATE, item=item, path=path)
    incoming = _incoming(item, path)
    held = (recordings or functools.partial(library_recordings, lib))()
    if held.holds(_ids(incoming), incoming.length):
        return Verdict(SAME, incoming, duplicates, item, path)
    kind = SPLIT if any(_different_recording(incoming, d) for d in duplicates) else APART
    return Verdict(kind, incoming, duplicates, item, path)


def _same_recording(a, b) -> bool:
    """True when two incoming files share a Spotify ID, an ISRC or a
    MusicBrainz recording (with lengths that agree, as the library's)."""
    from music_scan.identity import same_mb_recording  # noqa: PLC0415

    return (bool(a.spotify_id) and a.spotify_id == b.spotify_id) or not a.isrcs.isdisjoint(b.isrcs) \
        or same_mb_recording(a.mb_trackid, a.length, b.mb_trackid, b.length)


def one_per_recording(verdicts: list[Verdict]) -> tuple[list[Verdict], list[Verdict]]:
    """*verdicts* split into the ones to import and the copies to leave.

    Each is another recording than its library duplicates, but two of them can
    be one recording: spotdl fetched it twice, once with an ISRC and once
    without.  Imported under ``keep``, both would land.  So of the files that
    share an ID only the best moves: the one with an ISRC, then the higher
    bitrate, then the first by path.  Copies that share no ID (a fingerprint
    is all that says they're one) both import; ``music-audit-dupes`` finds them."""
    ranked = sorted(verdicts, key=lambda v: (not v.incoming.isrcs, -((v.item and v.item.get("bitrate")) or 0)))
    keep: list[Verdict] = []
    leave: list[Verdict] = []
    for v in ranked:
        (leave if any(_same_recording(v.incoming, k.incoming) for k in keep) else keep).append(v)
    order = {id(v): n for n, v in enumerate(verdicts)}
    return sorted(keep, key=lambda v: order[id(v)]), sorted(leave, key=lambda v: order[id(v)])


def move_apart(staging: Path, apart: Path, lib) -> list[Verdict]:
    """Move each file in *staging* that is another recording than its
    artist-and-title duplicates to the same place under *apart*, one copy per
    recording (:func:`one_per_recording`); log each one.  Returns their
    verdicts, each with its new path."""
    from music_scan.scan import AUDIO_EXTS  # noqa: PLC0415

    recordings = functools.cache(functools.partial(library_recordings, lib))
    others = []
    for f in sorted(staging.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in AUDIO_EXTS:
            continue
        verdict = classify(lib, f, recordings)
        if verdict.kind in OTHER_RECORDING:
            others.append(verdict)
    keep, leave = one_per_recording(others)
    if leave:
        logger.info("Asis apart    : %d file(s) left in quarantine, another copy of a file imported beside "
                    "its duplicate", len(leave))
        for v in leave:
            logger.info("  [COPY] %s: another copy of a file this pass imports; left in quarantine",
                        v.path.relative_to(staging))
    moved = []
    for verdict in keep:
        dest = apart / verdict.path.relative_to(staging)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(verdict.path), dest)
        for dup in verdict.duplicates:
            logger.warning("  [%s] %s — %s: %s, item %d; importing it separately (as-is)",
                           "SPLIT" if verdict.kind == SPLIT else "APART",
                           dup.get("artist"), dup.get("title"), verdict.kind, dup.id)
        moved.append(verdict._replace(path=dest))
    return moved


def _new_items(lib, moved: list[Verdict], since: float) -> list[tuple[Verdict, object]]:
    """Each of *moved* that is now in *lib*, with its own new item.

    The new items are the ones the keep run added (``added`` from *since* on,
    which only the files under the apart dir make).  Each goes to the file
    whose IDs it carries; a file with no ID left in common (a loose file
    imports without its spotdl tags) takes a new item with its artist and
    title.  No item goes to two files.  A file still under the apart dir
    wasn't imported."""
    from beets.dbcore.query import NumericQuery  # noqa: PLC0415

    from music_scan.identity import item_recording_ids  # noqa: PLC0415

    new = sorted(lib.items(NumericQuery("added", f"{since}..")), key=lambda i: i.id)
    claimed: set[int] = set()
    found: dict[int, object] = {}
    pending = [(n, v) for n, v in enumerate(moved) if not (v.path and v.path.exists())]

    def claim(n: int, match) -> None:
        for item in new:
            if item.id not in claimed and match(item):
                claimed.add(item.id)
                found[n] = item
                return

    for n, v in pending:
        ids = _ids(v.incoming)
        if ids:
            claim(n, lambda i: not ids.isdisjoint(item_recording_ids(i)))
    for n, v in pending:
        if n not in found and v.item is not None:
            claim(n, lambda i: (i.get("artist"), i.get("title")) == (v.item.get("artist"), v.item.get("title")))
    return [(moved[n], found[n]) for n, _ in pending if n in found]


def record_kept(lib, moved: list[Verdict], since: float) -> int:
    """The ``kept_apart`` events for the files :func:`move_apart` moved that
    the keep run, started at *since*, imported (:func:`_new_items`).  Returns
    how many imported."""
    from music_fetch import events  # noqa: PLC0415

    pairs = _new_items(lib, moved, since)
    for verdict, item in pairs:
        why = "ISRCs differ" if verdict.kind == SPLIT else "no shared ID"
        for dup in verdict.duplicates:
            events.record(
                "kept_apart", f"[{'SPLIT' if verdict.kind == SPLIT else 'APART'}] {item.get('artist')} — "
                              f"{item.get('title')}: {verdict.kind} from {dup}; imported separately (as-is)",
                source="scan", item_id=item.id,
                data={**events.item_fields(item), "why": why,
                      "duplicate": {"item_id": dup.id, **events.item_fields(dup), "via": dup.get("via"),
                                    "isrc": dup.get("isrc") or None},
                      "isrcs": sorted(verdict.incoming.isrcs)},
            )
    if len(pairs) < len(moved):
        logger.info("Asis apart    : %d of %d moved file(s) not imported", len(moved) - len(pairs), len(moved))
    return len(pairs)


def main() -> int:
    """Count, read-only, how the as-is pass's duplicate test sees the quarantine."""
    from music_scan.library import LIBRARY_DB, MusicLibrary  # noqa: PLC0415
    from music_scan.scan import QUARANTINE  # noqa: PLC0415

    kinds: Counter = Counter()
    vias: Counter = Counter()
    with MusicLibrary(LIBRARY_DB) as library:
        recordings = functools.cache(functools.partial(library_recordings, library._lib))
        for f in eligible(QUARANTINE):
            verdict = classify(library._lib, f, recordings)
            kinds[verdict.kind] += 1
            if verdict.kind in OTHER_RECORDING:
                vias.update(d.get("via") or "none" for d in verdict.duplicates)
    print(f"quarantine: {sum(kinds.values())} file(s) the as-is pass takes")
    for kind in (NO_DUPLICATE, SAME, SPLIT, APART, UNREADABLE):
        new = " (imported beside it from now on)" if kind in OTHER_RECORDING else ""
        print(f"  {kinds[kind]:5d}  {kind}{new}")
    if vias:
        print("  their duplicates by via: " + ", ".join(f"{v} {n}" for v, n in vias.most_common()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
