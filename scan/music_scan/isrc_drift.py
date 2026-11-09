"""Count library items whose database ISRC differs from their file's (homelab#2726).

``python -m music_scan.isrc_drift`` prints counts only, never an item.  It reads
the library database and each file's tags and writes nothing.

An ISRC is a tag field: setting it in the database alone zeroes the item's
stored mtime, so the next scan's ``beet update`` re-reads the file and takes
the file's ISRCs back.  So the count sees only the items no scan has re-read
since; one a scan already reverted agrees with its file again.  ``mtime 0``
counts the items the next scan re-reads, whatever changed.
"""

from __future__ import annotations

from collections import Counter

from music_scan.identity import split_list

FIELDS = ("items", "unreadable", "isrc differs", "file lacks some of the database's ISRCs",
          "mb_trackid differs", "mtime 0")


def drift(items) -> Counter:
    """The counts in :data:`FIELDS` for *items*."""
    from beets.util import syspath  # noqa: PLC0415
    from mediafile import MediaFile  # noqa: PLC0415

    counts: Counter = Counter({field: 0 for field in FIELDS})
    for item in items:
        counts["items"] += 1
        if not item.mtime:
            counts["mtime 0"] += 1
        try:
            tags = MediaFile(syspath(item.path))
        except Exception:  # noqa: BLE001 — a missing or unreadable file is counted, not fatal
            counts["unreadable"] += 1
            continue
        db, file = set(split_list(item.get("isrc"), ";")), set(split_list(tags.isrc, ";"))
        if db != file:
            counts["isrc differs"] += 1
            if db - file:
                counts["file lacks some of the database's ISRCs"] += 1
        if (item.get("mb_trackid") or "") != (tags.mb_trackid or ""):
            counts["mb_trackid differs"] += 1
    return counts


def main() -> None:
    from music_scan.library import LIBRARY_DB, MusicLibrary  # noqa: PLC0415

    with MusicLibrary(LIBRARY_DB) as lib:
        counts = drift(lib.all_items())
    print(", ".join(f"{field}: {counts[field]}" for field in FIELDS))


if __name__ == "__main__":
    main()
