"""``music-album-resolve``: the resolve pass over the album store (homelab#2410).

The album tick resolves new albums every tick (:func:`albums.resolve_keys`);
this reruns the pass over every unkeyed album once the MusicBrainz cache or the
library knows more, and reports what it rekeyed, flagged and merged.  A dry
run unless ``--apply``.

It was ``music-state-import``'s companion, which moved the JSON state into the
database; the import was retired with the JSON (homelab#2502).
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os

from music_fetch import resolve, state

logger = logging.getLogger(__name__)

LIBRARY_DB = resolve.LIBRARY_DB


def _locks(apply: bool):
    """The ``albums`` and ``pipeline`` slots, so no tick, scan or nightly writes the store mid-run."""
    if not apply:
        return contextlib.nullcontext()
    from music_fetch.albums import _state_lock  # noqa: PLC0415
    from music_scan.audit import pipeline_lock  # noqa: PLC0415 — the image holds both packages

    stack = contextlib.ExitStack()
    stack.enter_context(_state_lock(True))
    stack.enter_context(pipeline_lock())
    return stack


def resolve_main() -> None:
    parser = argparse.ArgumentParser(
        prog="music-album-resolve",
        description="Resolve and merge the album store's unkeyed albums again (homelab#2410); dry run unless --apply")
    parser.add_argument("--apply", action="store_true", help="write the database")
    parser.add_argument("--verbose", action="store_true", help="list every rekeyed and flagged album")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    from music_fetch.albums import _state_lock, load_overrides  # noqa: PLC0415

    with _state_lock(args.apply), state.connect() as conn:
        rows = state.load_albums(conn)
        library = resolve.library_groups(LIBRARY_DB) if os.path.isfile(LIBRARY_DB) else {}
        report = resolve.resolve(rows, state.get_all(conn, state.MB_RELEASES), library, load_overrides())
        report.log(verbose=args.verbose)
        if args.apply:
            logger.info("Saved %d row(s)", state.save_albums(conn, rows))
        else:
            logger.info("Dry run; --apply to write")
