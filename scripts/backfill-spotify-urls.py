#!/usr/bin/env python3
"""One-shot migration: populate spotify_url flex attr in beets DB from .spotdl snapshots.

Before this migration the beets scrub plugin (auto: yes) stripped the
----:spotdl:WOAS freeform tag on import, so reconcile_snapshot always saw
an empty library_urls set and dropped every snapshot entry as stale.

This script reads each .spotdl snapshot, matches its songs against beets
items by normalised title, and writes spotify_url= on each matched item so
that reconcile can verify them without re-reading file tags.

Run once after deploying the fix for #100:
    just backfill-spotify-urls [-- --dry-run] [-- --playlist <name>]

The script matches on normalised title (lowercase, punctuation stripped).
When a title is ambiguous (multiple library items match a single Spotify
title) it skips — manual review is safer than a wrong URL.

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
    return re.sub(r"[^\w\s]", "", s.lower()).strip()


def backfill_playlist(lib, playlist_name: str, dry_run: bool) -> tuple[int, int, int]:
    """Backfill spotify_url for one playlist. Returns (updated, skipped_ambiguous, no_match)."""
    spotdl_file = SPOTDL_DIR / f"{playlist_name}.spotdl"
    if not spotdl_file.exists():
        print(f"  WARNING: {spotdl_file} not found — skipping", file=sys.stderr)
        return 0, 0, 0

    with open(spotdl_file, encoding="utf-8") as fh:
        data = json.load(fh)
    songs = data.get("songs", []) if isinstance(data, dict) else data

    url_by_norm: dict[str, str] = {}
    for song in songs:
        name = song.get("name") or ""
        url = song.get("url") or ""
        if name and url:
            url_by_norm[_norm(name)] = url

    items = list(lib.items(f"source:{playlist_name}"))
    needs_url = [item for item in items if not (item.get("spotify_url") or "")]

    if not needs_url:
        print(f"{playlist_name}: all {len(items)} item(s) already have spotify_url — nothing to do")
        return 0, 0, 0

    print(f"{playlist_name}: {len(needs_url)}/{len(items)} item(s) need spotify_url")

    # Build title → [items] map within this playlist to detect ambiguity
    title_map: dict[str, list] = {}
    for item in needs_url:
        key = _norm(item.title or "")
        if key:
            title_map.setdefault(key, []).append(item)

    updated = skipped_ambiguous = no_match = 0

    for norm_title, candidates in sorted(title_map.items()):
        url = url_by_norm.get(norm_title)
        if not url:
            no_match += len(candidates)
            for item in candidates:
                print(f"  [NO MATCH] {item.title!r} — {item.artist!r}")
            continue

        if len(candidates) > 1:
            skipped_ambiguous += len(candidates)
            print(f"  [AMBIGUOUS] {len(candidates)} items share title {norm_title!r} — skipped:")
            for item in candidates:
                print(f"    id={item.id}  {item.title!r} — {item.artist!r}")
            continue

        item = candidates[0]
        action = "DRY-RUN" if dry_run else "SET"
        print(f"  [{action}] spotify_url → {item.title!r} — {item.artist!r}")
        if not dry_run:
            item["spotify_url"] = url
            item.store()
        updated += 1

    return updated, skipped_ambiguous, no_match


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--playlist", help="Limit to one playlist name (default: all)")
    parser.add_argument("--dry-run", action="store_true", help="Print what would change; don't write")
    args = parser.parse_args()

    if not LIBRARY_DB.exists():
        print(f"ERROR: {LIBRARY_DB} not found", file=sys.stderr)
        sys.exit(1)

    try:
        from beets.library import Library
    except ImportError:
        print("ERROR: beets not installed in this environment", file=sys.stderr)
        sys.exit(1)

    lib = Library(str(LIBRARY_DB))

    if args.playlist:
        playlists = [args.playlist]
    else:
        playlists = [f.stem for f in sorted(SPOTDL_DIR.glob("*.spotdl"))]

    if not playlists:
        print("No .spotdl files found.")
        lib._close()
        return

    if args.dry_run:
        print("--- DRY RUN — no changes will be written ---\n")

    total_updated = total_ambiguous = total_no_match = 0
    for playlist in playlists:
        updated, ambiguous, no_match = backfill_playlist(lib, playlist, args.dry_run)
        total_updated += updated
        total_ambiguous += ambiguous
        total_no_match += no_match

    lib._close()

    print()
    print(f"Total: {total_updated} updated, {total_ambiguous} skipped (ambiguous title), {total_no_match} no match in snapshot")
    if args.dry_run:
        print("(dry-run — no changes written)")
    if total_no_match:
        print("No-match items may have titles that differ significantly after MusicBrainz tagging.")
        print("Check these manually: beet ls source:<playlist> ^spotify_url:.")


if __name__ == "__main__":
    main()
