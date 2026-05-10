#!/usr/bin/env python3
"""Rebuild .spotdl snapshot with only library-confirmed tracks.

Run in the SCAN container after `beet import -A /root/Music/library/` has
populated the beets DB. Reads the temp JSON written by fetch-spotify-snapshot.py,
cross-references against the beets DB by normalised title, and writes only
library-matched songs back to the .spotdl file. Unmatched Spotify tracks remain
absent from the snapshot so the next music-ingest run re-downloads them normally.

Cleans up the temp file on success.

Usage:
    python3 rebuild-spotdl-snapshot.py --playlist aaaaaaah [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

SPOTDL_DIR = Path("/root/Music/inbox/spotdl")
LIBRARY_DB = Path("/root/.config/beets/library.db")
TEMP_FILE = SPOTDL_DIR / ".spotify-full.json"


def _norm(s: str) -> str:
    return re.sub(r"[^\w\s]", "", s.lower()).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--playlist", required=True, help="Playlist name (e.g. aaaaaaah)")
    parser.add_argument("--dry-run", action="store_true", help="Print what would change; don't write")
    args = parser.parse_args()

    spotdl_file = SPOTDL_DIR / f"{args.playlist}.spotdl"

    for path in (TEMP_FILE, spotdl_file, LIBRARY_DB):
        if not path.exists():
            print(f"ERROR: {path} not found")
            raise SystemExit(1)

    with open(TEMP_FILE, encoding="utf-8") as fh:
        spotify_songs: list[dict] = json.load(fh)

    with open(spotdl_file, encoding="utf-8") as fh:
        original: dict = json.load(fh)

    try:
        from beets.library import Library
    except ImportError:
        print("ERROR: beets not installed in this environment")
        raise SystemExit(1)

    lib = Library(str(LIBRARY_DB))
    beets_titles = {_norm(item.title or "") for item in lib.items()}
    lib._close()

    matched: list[dict] = []
    unmatched: list[str] = []
    for song in spotify_songs:
        norm = _norm(song.get("name", ""))
        if norm and norm in beets_titles:
            matched.append(song)
        else:
            unmatched.append(song.get("name", "?"))

    print(f"Spotify total  : {len(spotify_songs)}")
    print(f"Matched in lib : {len(matched)}")
    print(f"Not in library : {len(unmatched)}")
    if unmatched:
        print("  Unmatched (first 10):", unmatched[:10])

    if args.dry_run:
        print("(dry-run — no changes written)")
        return

    original["songs"] = matched
    with open(spotdl_file, "w", encoding="utf-8") as fh:
        json.dump(original, fh, indent=4, ensure_ascii=False)
    print(f"Written {len(matched)} songs to {spotdl_file}")

    TEMP_FILE.unlink(missing_ok=True)
    print(f"Removed {TEMP_FILE}")


if __name__ == "__main__":
    main()
