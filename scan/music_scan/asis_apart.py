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
ID is the same recording, and stays skipped as before.

``python -m music_scan.asis_apart`` counts what the quarantine holds, read-only
and counts only.  It uses nothing newer than this module, so piped into a pod
running an image without it (``python - < asis_apart.py``) it counts too.
"""

from __future__ import annotations

import logging
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import Iterator, NamedTuple

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


def classify(lib, path: Path) -> Verdict:
    """*path* against the beets library *lib*: beets' duplicates for it (its
    artist and title), and whether any of them, or any other item, is the same
    recording by ID, as the main import's duplicate hook decides."""
    from beets.library import Item  # noqa: PLC0415

    from music_scan.music_pipeline import _different_recording, _find_same_recording  # noqa: PLC0415

    try:
        item = Item.from_path(str(path))
    except Exception:
        return Verdict(UNREADABLE)
    if not item.get("artist"):
        return Verdict(NO_DUPLICATE)  # beets skips the check without an artist
    duplicates = tuple(lib.items(item.duplicates_query(DUPLICATE_KEYS)))
    if not duplicates:
        return Verdict(NO_DUPLICATE)
    incoming = _incoming(item, path)
    if _find_same_recording(lib, incoming):
        return Verdict(SAME, incoming, duplicates)
    kind = SPLIT if any(_different_recording(incoming, d) for d in duplicates) else APART
    return Verdict(kind, incoming, duplicates)


def move_apart(staging: Path, apart: Path, lib) -> list[Verdict]:
    """Move each file in *staging* that is another recording than its
    artist-and-title duplicates to the same place under *apart*; log each one."""
    from music_scan.scan import AUDIO_EXTS  # noqa: PLC0415

    moved = []
    for f in sorted(staging.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in AUDIO_EXTS:
            continue
        verdict = classify(lib, f)
        if verdict.kind not in OTHER_RECORDING:
            continue
        dest = apart / f.relative_to(staging)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), dest)
        for dup in verdict.duplicates:
            logger.warning("  [%s] %s — %s: %s, item %d; importing it separately (as-is)",
                           "SPLIT" if verdict.kind == SPLIT else "APART",
                           dup.get("artist"), dup.get("title"), verdict.kind, dup.id)
        moved.append(verdict)
    return moved


def record_kept(lib, moved: list[Verdict]) -> int:
    """The ``kept_apart`` events for the files :func:`move_apart` moved that are
    now in the library, found by their Spotify ID.  Returns how many imported."""
    from music_fetch import events  # noqa: PLC0415
    from music_scan.identity import item_spotify_ids  # noqa: PLC0415

    imported = 0
    for verdict in moved:
        sid = verdict.incoming.spotify_id
        dup_ids = {d.id for d in verdict.duplicates}
        found = [i for i in lib.items(f"spotify_url:{sid}") if sid in item_spotify_ids(i) and i.id not in dup_ids] \
            if sid else []
        if not found:
            continue
        item = max(found, key=lambda i: i.id)
        imported += 1
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
    return imported


def main() -> int:
    """Count, read-only, how the as-is pass's duplicate test sees the quarantine."""
    from music_scan.library import LIBRARY_DB, MusicLibrary  # noqa: PLC0415
    from music_scan.scan import QUARANTINE  # noqa: PLC0415

    kinds: Counter = Counter()
    vias: Counter = Counter()
    with MusicLibrary(LIBRARY_DB) as library:
        for f in eligible(QUARANTINE):
            verdict = classify(library._lib, f)
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
