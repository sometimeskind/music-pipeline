#!/usr/bin/env python3
"""Fetch current Spotify playlist state and write to a temp JSON file.

Run in the FETCH container (has spotdl + Spotify creds). No files are downloaded.
Output goes to /root/Music/inbox/spotdl/.spotify-full.json on the shared PVC,
where rebuild-spotdl-snapshot.py (scan container) can read it.

Usage:
    python3 fetch-spotify-snapshot.py --playlist aaaaaaah
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

SPOTDL_DIR = Path("/root/Music/inbox/spotdl")
OUTPUT = SPOTDL_DIR / ".spotify-full.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--playlist", required=True, help="Playlist name (e.g. aaaaaaah)")
    args = parser.parse_args()

    spotdl_file = SPOTDL_DIR / f"{args.playlist}.spotdl"
    if not spotdl_file.exists():
        print(f"ERROR: {spotdl_file} not found")
        raise SystemExit(1)

    with open(spotdl_file, encoding="utf-8") as fh:
        data = json.load(fh)

    query: list[str] = data["query"]

    from music_fetch.spotdl_ops import _make_downloader_settings, _make_spotdl

    cookie_file = Path("/root/.config/spotdl/cookies.txt")
    settings = _make_downloader_settings(cookie_file=cookie_file)
    spotdl_obj = _make_spotdl(settings)

    print(f"Fetching Spotify state for: {query}")
    songs = spotdl_obj.search(query)
    print(f"Found {len(songs)} tracks on Spotify")

    with open(OUTPUT, "w", encoding="utf-8") as fh:
        json.dump([s.json for s in songs], fh, indent=2, ensure_ascii=False)
    print(f"Written to {OUTPUT}")


if __name__ == "__main__":
    main()
