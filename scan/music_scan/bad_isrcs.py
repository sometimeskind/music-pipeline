"""music-clear-bad-isrcs: clear the values in items' ``isrc`` that aren't ISRCs (homelab#2672).

Some Usenet releases carry their upload group's tag (``PMEDIA``) or a store's
track key (``qobuz_<n>``) in the ISRC tag, and beets reads the file's tag into
``isrc``.  :func:`music_scan.identity.item_isrcs` ignores such a value, so it
no longer makes two items one recording, and an import no longer stores one;
this clears the ones already in the library.

Dry run by default: it lists each item and the values it would clear, and
counts them.  ``--apply`` takes them off, writes the file (an ISRC is a tag,
and the scan's ``beet update`` would take the file's back otherwise,
homelab#2726) and records an ``ids_written`` event per item.  The item's
ISRCs stay, and no playlist entry resolves by such a value, so nothing else
changes.  Safe to re-run.
"""

from __future__ import annotations

import argparse
import logging

from music_fetch import events
from music_scan.identity import bad_isrcs, drop_bad_isrcs

logger = logging.getLogger(__name__)

VIA = "music-clear-bad-isrcs"


def _label(item) -> str:
    return f"{item.id} {item.get('artist')} — {item.get('title')}"


def clear(items, apply: bool) -> int:
    """Clear :func:`~music_scan.identity.bad_isrcs` from *items*; with *apply*
    only.  Returns how many items hold some."""
    held = [(item, bad) for item in items if (bad := bad_isrcs(item))]
    for item, bad in held:
        logger.info("[ISRC] %s: %s isn't an ISRC%s", _label(item), "; ".join(bad),
                    "" if apply else ", would be cleared")
    logger.info("%d item(s) hold a value in isrc that isn't an ISRC", len(held))
    if not apply:
        if held:
            logger.info("Dry run — nothing cleared. Re-run with --apply.")
        return len(held)

    for item, _ in held:
        before = item.get("isrc") or None
        cleared = drop_bad_isrcs(item)
        # Written first, so the store keeps the file's new mtime and beet update reads the tag back (homelab#2723).
        if not item.try_write():
            logger.warning("[ISRC] %s: the file couldn't be written, so the next scan's beet update takes "
                           "its ISRC tag back", _label(item))
        item.store()
        after = item.get("isrc") or None
        events.record("ids_written", f"[IDS] {_label(item)}: isrc {'; '.join(cleared)} cleared, not an ISRC "
                                     f"({VIA})", source="backfill", item_id=item.id,
                      spotify_album_id=events.spotify_album(item.get("spotify_album_id")),
                      release_group=item.get("mb_releasegroupid") or None,
                      data={**events.item_fields(item), "changed": {"isrc": [before, after]}, "via": VIA})
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
