"""music-clear-release-tracks: clear the recording IDs beets stored as items' ``mb_releasetrackid`` (homelab#2624).

beets 2.14's ``TrackInfo`` maps ``release_track_id or track_id`` to
``mb_releasetrackid``, and a singleton match has no release track, so every
item imported before music-pipeline#437 holds its **recording** ID there: the
value names no release track, and it goes stale once the recording is
replaced.  The plugin's ``clear_singleton_release_track`` keeps new imports
from storing it; this clears the ones already in the library.

Only the shape beets wrote is cleared: ``mb_releasetrackid`` equal to the
item's own ``mb_trackid``.  A release-track ID that differs (a real one, or one
``set_recording`` aligned) stays.

Dry run by default: it counts the items first, then lists each with the value
it would clear.  ``--apply`` clears it, writes the file (the value is a tag,
MusicBrainz Release Track Id, and the scan's ``beet update`` would take the
file's back otherwise, homelab#2726) and records an ``ids_written`` event per
item.  Nothing in the pipeline reads ``mb_releasetrackid``, so nothing else
changes.  Safe to re-run.
"""

from __future__ import annotations

import argparse
import logging

from music_fetch import events

logger = logging.getLogger(__name__)

VIA = "music-clear-release-tracks"
# An apply writes every file, so it says how far it has got (get_job's progress).
PROGRESS_EVERY = 500


def _label(item) -> str:
    return f"{item.id} {item.get('artist')} — {item.get('title')}"


def holds_recording(item) -> bool:
    """Whether *item*'s ``mb_releasetrackid`` is its own recording ID, as beets stored it."""
    release_track = item.get("mb_releasetrackid") or ""
    return bool(release_track) and release_track == (item.get("mb_trackid") or "")


def clear(items, apply: bool) -> int:
    """Clear ``mb_releasetrackid`` on the *items* where it is the recording ID
    (:func:`holds_recording`); with *apply* only.  Returns how many hold it."""
    held = [item for item in items if holds_recording(item)]
    # The count first: on a whole library the list runs to thousands of lines.
    logger.info("%d item(s) hold their recording ID in mb_releasetrackid", len(held))
    for item in held:
        logger.info("[RELEASETRACK] %s: mb_releasetrackid %s is its recording ID%s", _label(item),
                    item.get("mb_releasetrackid"), "" if apply else ", would be cleared")
    if not apply:
        if held:
            logger.info("Dry run — nothing cleared. Re-run with --apply.")
        return len(held)

    for n, item in enumerate(held, 1):
        before = item.get("mb_releasetrackid") or None
        item["mb_releasetrackid"] = ""
        # Written first, so the store keeps the file's new mtime and beet update reads the tag back (homelab#2723).
        if not item.try_write():
            logger.warning("[RELEASETRACK] %s: the file couldn't be written, so the next scan's beet update "
                           "takes its release track ID back", _label(item))
        item.store()
        events.record("ids_written", f"[IDS] {_label(item)}: mb_releasetrackid {before} cleared, its recording "
                                     f"ID ({VIA})", source="backfill", item_id=item.id,
                      spotify_album_id=events.spotify_album(item.get("spotify_album_id")),
                      release_group=item.get("mb_releasegroupid") or None,
                      data={**events.item_fields(item),
                            "changed": {"mb_releasetrackid": [before, item.get("mb_releasetrackid") or None]},
                            "via": VIA})
        if n % PROGRESS_EVERY == 0 and n < len(held):
            logger.info("[PROGRESS] %d/%d cleared", n, len(held))
    logger.info("Cleared %d item(s)", len(held))
    return len(held)


def run(apply: bool = False) -> int:
    from music_scan.audit import pipeline_lock  # noqa: PLC0415
    from music_scan.library import LIBRARY_DB, MusicLibrary  # noqa: PLC0415

    with MusicLibrary(LIBRARY_DB) as lib:
        if not apply:
            return clear(lib.all_items(), False)
        with pipeline_lock():
            return clear(lib.all_items(), True)


def main() -> None:
    parser = argparse.ArgumentParser(prog=VIA, description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="clear them (default: dry run)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    run(apply=args.apply)


if __name__ == "__main__":
    main()
