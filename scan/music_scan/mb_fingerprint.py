"""music-mb-fingerprint: add mb_trackid to beets items that lack one.

Fingerprints each file via AcoustID and writes mb_trackid back to the
beets library where a confident match (>= 0.85) is found. No other
metadata is changed.

Requires ACOUSTID_APIKEY env var. Safe to re-run: skips items that
already have mb_trackid.
"""

from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)

LIBRARY_DB = "/root/.config/beets/library.db"
MIN_SCORE = 0.85
REQUEST_DELAY = 0.35


def run(library_db: str = LIBRARY_DB) -> None:
    import acoustid
    import beets.library

    api_key = os.environ.get("ACOUSTID_APIKEY")
    if not api_key:
        raise RuntimeError("ACOUSTID_APIKEY env var is not set")

    lib = beets.library.Library(library_db)
    try:
        items = [i for i in lib.items() if not i.mb_trackid]
        total = len(items)
        logger.info("Fingerprinting %d item(s) without mb_trackid", total)

        tagged = no_match = errors = 0
        for n, item in enumerate(items, 1):
            path = item.path.decode() if isinstance(item.path, bytes) else item.path
            label = f"{item.artist or item.albumartist or '?'} – {item.title or '?'}"
            try:
                results = list(acoustid.match(api_key, path))
                if results:
                    score, rec_id, *_ = results[0]
                    if score >= MIN_SCORE and rec_id:
                        item.mb_trackid = rec_id
                        item.store()
                        logger.info("[%d/%d] %s -> %s (%.2f)", n, total, label, rec_id, score)
                        tagged += 1
                        continue
                    logger.info("[%d/%d] %s: low confidence (%.2f)", n, total, label, score)
                else:
                    logger.info("[%d/%d] %s: no results", n, total, label)
                no_match += 1
            except Exception as exc:
                logger.warning("[%d/%d] %s: %s", n, total, label, exc)
                errors += 1
            time.sleep(REQUEST_DELAY)
    finally:
        lib._close()

    logger.info(
        "Done: %d tagged, %d no confident match, %d errors",
        tagged, no_match, errors,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s",
                        datefmt="%Y-%m-%dT%H:%M:%S%z")
    run()
