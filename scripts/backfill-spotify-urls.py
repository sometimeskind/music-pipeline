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

Matching strategy (two passes):
  1. Normalised title (lowercase, punctuation stripped) — fast, unambiguous.
  2. Duration fallback (±2 s) + normalised first artist — catches tracks whose
     MusicBrainz title diverged from the Spotify name.  Skipped if ambiguous.

When a match is ambiguous (multiple candidates) it is skipped — manual
review is safer than assigning the wrong URL.

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


_DURATION_TOLERANCE = 2.0  # seconds


def backfill_playlist(lib, playlist_name: str, dry_run: bool) -> tuple[int, int, int]:
    """Backfill spotify_url for one playlist. Returns (updated, skipped_ambiguous, no_match)."""
    spotdl_file = SPOTDL_DIR / f"{playlist_name}.spotdl"
    if not spotdl_file.exists():
        print(f"  WARNING: {spotdl_file} not found — skipping", file=sys.stderr)
        return 0, 0, 0

    with open(spotdl_file, encoding="utf-8") as fh:
        data = json.load(fh)
    songs = data.get("songs", []) if isinstance(data, dict) else data

    url_by_norm_title: dict[str, str] = {}
    # (norm_artist, duration_seconds) → url  — for the duration fallback pass
    duration_index: list[tuple[str, float, str]] = []
    for song in songs:
        name = song.get("name") or ""
        url = song.get("url") or ""
        if not (name and url):
            continue
        url_by_norm_title[_norm(name)] = url
        artist = ((song.get("artists") or [""]) + [""])[0]
        duration = song.get("duration")
        if duration is not None:
            duration_index.append((_norm(artist), float(duration), url))

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
    unmatched_after_title: list = []

    # Pass 1 — normalised title
    for norm_title, candidates in sorted(title_map.items()):
        url = url_by_norm_title.get(norm_title)
        if not url:
            unmatched_after_title.extend(candidates)
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

    # Pass 2 — duration fallback for items that didn't match by title
    for item in unmatched_after_title:
        item_duration = getattr(item, "length", None)
        item_artist = _norm(item.artist or item.albumartist or "")
        if item_duration is None or not duration_index:
            no_match += 1
            print(f"  [NO MATCH] {item.title!r} — {item.artist!r}")
            continue

        matches = [
            url for (norm_artist, dur, url) in duration_index
            if abs(dur - item_duration) <= _DURATION_TOLERANCE
            and norm_artist == item_artist
        ]
        # Deduplicate while preserving order
        seen: set[str] = set()
        unique_matches = [u for u in matches if not (u in seen or seen.add(u))]  # type: ignore[func-returns-value]

        if len(unique_matches) == 1:
            action = "DRY-RUN" if dry_run else "DURATION MATCH"
            print(f"  [{action}] spotify_url → {item.title!r} — {item.artist!r}  (duration ≈ {item_duration:.0f}s)")
            if not dry_run:
                item["spotify_url"] = unique_matches[0]
                item.store()
            updated += 1
        elif len(unique_matches) > 1:
            skipped_ambiguous += 1
            print(f"  [AMBIGUOUS DURATION] {item.title!r} — {item.artist!r}  ({len(unique_matches)} duration candidates)")
        else:
            no_match += 1
            print(f"  [NO MATCH] {item.title!r} — {item.artist!r}")

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
