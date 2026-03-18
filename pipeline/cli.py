"""Console entry points for all music-pipeline commands.

Each function is registered as a console_script in pyproject.toml and
installed to /usr/local/bin/ via the pip package.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)

SPOTDL_DIR = Path("/root/Music/inbox/spotdl")
PLAYLISTS = Path("/root/Music/playlists")
COOKIE_FILE = Path("/root/.config/spotdl/cookies.txt")
LIBRARY_DB = Path("/root/.config/beets/library.db")
DEFAULT_CONF = Path("/root/.config/music-pipeline/playlists.conf")


# ---------------------------------------------------------------------------
# music-scan
# ---------------------------------------------------------------------------


def scan() -> None:
    """Entry point: music-scan."""
    from pipeline.scan import run  # noqa: PLC0415

    run()


# ---------------------------------------------------------------------------
# music-ingest
# ---------------------------------------------------------------------------


def ingest() -> None:
    """Entry point: music-ingest."""
    from pipeline.ingest import run  # noqa: PLC0415

    run()


# ---------------------------------------------------------------------------
# music-import
# ---------------------------------------------------------------------------


def import_cmd() -> None:
    """Entry point: music-import.

    Imports all audio from inbox to beets; moves unmatched files to quarantine.
    Called by music-scan; also useful standalone.
    """
    from pipeline.process import run_beet_import  # noqa: PLC0415
    from pipeline.scan import INBOX, QUARANTINE, _quarantine_inbox_leftovers  # noqa: PLC0415

    import subprocess

    logging.getLogger().info("==> Importing from inbox...")
    try:
        run_beet_import(INBOX)
    except subprocess.CalledProcessError as exc:
        logging.getLogger().error("beet import failed with exit code %d", exc.returncode)
        sys.exit(exc.returncode)

    logging.getLogger().info("==> Quarantining skipped files...")
    moved = _quarantine_inbox_leftovers()
    logging.getLogger().info("Quarantined : %d file(s) → %s", moved, QUARANTINE)
    logging.getLogger().info("Log         : ~/.config/beets/import.log")


# ---------------------------------------------------------------------------
# music-setup
# ---------------------------------------------------------------------------


def setup() -> None:
    """Entry point: music-setup.

    Creates local dirs, runs spotdl save to create the initial .spotdl file.
    Non-interactive with --name / --url; interactive otherwise.
    Idempotent — skips if .spotdl already exists.
    """
    from pipeline.spotdl_ops import save_playlist  # noqa: PLC0415

    parser = argparse.ArgumentParser(description="Add a new Spotify playlist to the pipeline.")
    parser.add_argument("--name", help="Playlist slug (e.g. liked-songs)")
    parser.add_argument("--url", help="Spotify playlist URL")
    args = parser.parse_args()

    # Pre-flight
    if not COOKIE_FILE.exists():
        print(f"Error: YouTube Premium cookies not found at {COOKIE_FILE}", file=sys.stderr)
        print("See README for export instructions.", file=sys.stderr)
        sys.exit(1)
    if not os.environ.get("SPOTIFY_CLIENT_ID") or not os.environ.get("SPOTIFY_CLIENT_SECRET"):
        print("Error: SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET must be set", file=sys.stderr)
        print(
            "Run via: op run --env-file=.env.tpl -- docker compose run --rm pipeline music-setup",
            file=sys.stderr,
        )
        sys.exit(1)

    name: str = args.name or ""
    url: str = args.url or ""

    if not name:
        name = input("Enter a name for this playlist (slug, e.g. liked-songs): ").strip()
    if not name:
        print("Error: playlist name required", file=sys.stderr)
        sys.exit(1)

    if not url:
        url = input(f"Enter the Spotify URL for '{name}': ").strip()
    if not url:
        print("Error: playlist URL required", file=sys.stderr)
        sys.exit(1)

    print("==> Creating Music directory structure...")
    for d in [
        Path("/root/Music/inbox/spotdl"),
        Path("/root/Music/library"),
        Path("/root/Music/quarantine"),
        Path("/root/Music/playlists"),
    ]:
        d.mkdir(parents=True, exist_ok=True)

    spotdl_file = SPOTDL_DIR / f"{name}.spotdl"
    output_dir = SPOTDL_DIR / name

    if spotdl_file.exists():
        print(f"==> Playlist '{name}' already exists at {spotdl_file} — skipping.")
        sys.exit(0)

    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n==> Saving spotdl sync file for '{name}'...")
    save_playlist(url=url, spotdl_file=spotdl_file, output_dir=output_dir, cookie_file=COOKIE_FILE)

    print(
        f"\nSetup complete.\n"
        f"  Playlist    : {name}\n"
        f"  Sync file   : {spotdl_file}\n"
        f"  Download dir: {output_dir}/\n"
        f"\nRun 'music-ingest' to download and import all playlists."
    )


# ---------------------------------------------------------------------------
# music-remove
# ---------------------------------------------------------------------------


def remove() -> None:
    """Entry point: music-remove.

    Removes a playlist's .spotdl file, download dir, .m3u, and clears
    source= beets tags.  Does not delete library files.
    """
    from pipeline.library import MusicLibrary  # noqa: PLC0415

    parser = argparse.ArgumentParser(description="Remove a playlist from the pipeline.")
    parser.add_argument("name", nargs="?", help="Playlist name to remove")
    args = parser.parse_args()

    name: str = args.name or ""
    if not name:
        name = input("Enter playlist name to remove: ").strip()
    if not name:
        print("Error: playlist name required", file=sys.stderr)
        sys.exit(1)

    spotdl_file = SPOTDL_DIR / f"{name}.spotdl"
    nosync_file = SPOTDL_DIR / f"{name}.nosync"
    download_dir = SPOTDL_DIR / name
    m3u_file = PLAYLISTS / f"{name}.m3u"

    if not spotdl_file.exists() and not download_dir.exists() and not m3u_file.exists():
        print(f"Error: no playlist named '{name}' found", file=sys.stderr)
        sys.exit(1)

    print("This will remove:")
    if spotdl_file.exists():
        print(f"  {spotdl_file}")
    if nosync_file.exists():
        print(f"  {nosync_file}")
    if download_dir.exists():
        print(f"  {download_dir}/")
    if m3u_file.exists():
        print(f"  {m3u_file}")
    print(f"  beets source:{name} tags")
    print("\nLibrary files are kept.\n")

    confirm = input("Continue? [y/N] ").strip().lower()
    if confirm not in ("y", "yes"):
        print("Aborted.")
        sys.exit(0)

    import shutil

    spotdl_file.unlink(missing_ok=True)
    nosync_file.unlink(missing_ok=True)
    if download_dir.exists():
        shutil.rmtree(download_dir)
    m3u_file.unlink(missing_ok=True)

    print(f"==> Clearing beets source tags for {name}...")
    with MusicLibrary(LIBRARY_DB) as lib:
        items = lib.items_by_source(name)
        if items:
            for item in items:
                item["source"] = ""
                item.store()
            print(f"  Cleared source tag on {len(items)} item(s).")
        else:
            print(f"  No beets entries found for source:{name}")

    print(f"\nPlaylist '{name}' removed. Run 'music-setup' to re-add it.")


# ---------------------------------------------------------------------------
# music-provision
# ---------------------------------------------------------------------------


def provision() -> None:
    """Entry point: music-provision.

    Non-interactive. Reads playlists.conf, calls music-setup for each entry,
    and reconciles .nosync sentinel files.  Idempotent.
    """
    from pipeline.config import load_playlists  # noqa: PLC0415
    from pipeline.spotdl_ops import save_playlist  # noqa: PLC0415

    parser = argparse.ArgumentParser(
        description="Provision all playlists from config onto the PVC."
    )
    parser.add_argument(
        "conf",
        nargs="?",
        type=Path,
        default=DEFAULT_CONF,
        help=f"Path to playlists.conf (default: {DEFAULT_CONF})",
    )
    args = parser.parse_args()
    conf: Path = args.conf

    if not conf.exists():
        print(f"Error: playlist config not found at {conf}", file=sys.stderr)
        print("Mount config/playlists.conf to that path, or pass it as an argument.", file=sys.stderr)
        sys.exit(1)

    playlists = load_playlists(conf)
    provisioned = 0

    for pl in playlists:
        print(f"==> Provisioning: {pl.name}")

        spotdl_file = SPOTDL_DIR / f"{pl.name}.spotdl"
        output_dir = SPOTDL_DIR / pl.name
        nosync_file = SPOTDL_DIR / f"{pl.name}.nosync"

        if not spotdl_file.exists():
            output_dir.mkdir(parents=True, exist_ok=True)
            save_playlist(
                url=pl.url,
                spotdl_file=spotdl_file,
                output_dir=output_dir,
                cookie_file=COOKIE_FILE,
            )
        else:
            print(f"==> Playlist '{pl.name}' already exists at {spotdl_file} — skipping.")

        # Reconcile .nosync sentinel to match config
        if pl.nosync:
            if not nosync_file.exists():
                print(f"    Creating .nosync sentinel for {pl.name}")
                nosync_file.touch()
        else:
            if nosync_file.exists():
                print(f"    Removing .nosync sentinel for {pl.name} (nosync flag removed from config)")
                nosync_file.unlink()

        provisioned += 1

    print(f"\nProvisioning complete: {provisioned} playlist(s) processed.")
