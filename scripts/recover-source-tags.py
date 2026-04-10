#!/usr/bin/env python3
"""One-shot recovery: tag beets items that belong to a playlist but are missing source/via.

Reads the .spotdl snapshot for a given playlist, queries the beets library for
items whose title matches a Spotify track title, and sets source=<playlist> and
via=spotdl on any that are currently untagged.

Usage (inside the music-pipeline-scan container or equivalent environment):
    python3 recover-source-tags.py --playlist aaaaaaah [--dry-run]

The script matches on normalised title (lowercase, punctuation stripped).  When
a track title is ambiguous (multiple library items match a single Spotify title)
it prints a warning and skips — manual review is safer than a wrong tag.

Exit codes: 0 = success, 1 = error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SPOTDL_DIR = Path("/root/Music/inbox/spotdl")
LIBRARY_DB = Path("/root/.config/beets/library.db")


def _norm(s: str) -> str:
    """Lowercase + strip punctuation for fuzzy title matching."""
    return re.sub(r"[^\w\s]", "", s.lower()).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--playlist", required=True, help="Playlist name (e.g. aaaaaaah)")
    parser.add_argument("--dry-run", action="store_true", help="Print what would change; don't write")
    args = parser.parse_args()

    spotdl_file = SPOTDL_DIR / f"{args.playlist}.spotdl"
    if not spotdl_file.exists():
        print(f"ERROR: {spotdl_file} not found", file=sys.stderr)
        sys.exit(1)

    if not LIBRARY_DB.exists():
        print(f"ERROR: {LIBRARY_DB} not found", file=sys.stderr)
        sys.exit(1)

    # --- Load Spotify track titles from .spotdl snapshot ---
    with open(spotdl_file, encoding="utf-8") as fh:
        data = json.load(fh)

    # Support both bare list (old spotdl format) and {songs: [...]} dict (new format)
    if isinstance(data, dict):
        data = data.get("songs", [])
    if not isinstance(data, list):
        print("ERROR: unexpected .spotdl format (expected JSON array or {songs:[...]})", file=sys.stderr)
        sys.exit(1)

    spotify_titles: set[str] = set()
    for entry in data:
        name = entry.get("name", "")
        if name:
            spotify_titles.add(_norm(name))

    print(f"Spotify snapshot: {len(data)} tracks, {len(spotify_titles)} unique normalised titles")

    # --- Load beets library ---
    try:
        from beets.library import Library
    except ImportError:
        print("ERROR: beets not installed in this environment", file=sys.stderr)
        sys.exit(1)

    lib = Library(str(LIBRARY_DB))

    # Build a map of normalised title → list of items (to detect ambiguity)
    all_items = list(lib.items())
    title_map: dict[str, list] = {}
    for item in all_items:
        key = _norm(item.title or "")
        if key:
            title_map.setdefault(key, []).append(item)

    # --- Find items already tagged with this playlist ---
    already_tagged = {item.id for item in lib.items(f"source:{args.playlist}")}
    print(f"Already tagged source={args.playlist}: {len(already_tagged)} items")

    # --- Identify candidates: in Spotify snapshot, not tagged, no existing source ---
    tagged = 0
    skipped_ambiguous = 0
    skipped_has_source = 0
    not_found = 0

    for norm_title in sorted(spotify_titles):
        matches = title_map.get(norm_title, [])
        if not matches:
            not_found += 1
            continue

        # Filter to items not already tagged with this playlist
        untagged = [m for m in matches if m.id not in already_tagged]
        if not untagged:
            continue  # already tagged — nothing to do

        # Skip items that already have a DIFFERENT non-empty source tag
        needs_tag = [
            m for m in untagged
            if not (m.get("source") or "").strip()
        ]

        has_other_source = [m for m in untagged if (m.get("source") or "").strip()]
        if has_other_source:
            skipped_has_source += len(has_other_source)
            for m in has_other_source:
                print(f"  SKIP (has source={m.get('source')!r}): {m.title} — {m.artist}")

        if not needs_tag:
            continue

        if len(needs_tag) > 1:
            skipped_ambiguous += 1
            print(f"  AMBIGUOUS ({len(needs_tag)} matches) for title {norm_title!r}:")
            for m in needs_tag:
                print(f"    id={m.id}  {m.title!r} — {m.artist!r}  path={m.path}")
            continue

        item = needs_tag[0]
        action = "DRY-RUN" if args.dry_run else "TAG"
        print(f"  {action}: source={args.playlist} via=spotdl  →  {item.title!r} — {item.artist!r}")

        if not args.dry_run:
            item["source"] = args.playlist
            item["via"] = "spotdl"
            item.store()
            tagged += 1
        else:
            tagged += 1

    lib._close()

    print()
    print(f"Result: {tagged} tagged, {skipped_ambiguous} skipped (ambiguous), "
          f"{skipped_has_source} skipped (other source), {not_found} not in library")
    if args.dry_run:
        print("(dry-run — no changes written)")


if __name__ == "__main__":
    main()
