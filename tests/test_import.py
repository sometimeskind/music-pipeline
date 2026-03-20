"""Scenario 2 — Manual File Drop, Scenario 3 — Playlist Import, Scenario 4 — Duplicate Handling.

Two beets config variants are used (both derived from production config):

  beets_autotag_config  — autotag=off; used for all tests where we expect the
      track to land in the library. beets imports using embedded tags (ASIS)
      without any MusicBrainz lookup. import_task_choice still fires so the
      music_pipeline plugin writes source= and via= correctly.

  beets_test_config     — autotag=on, strong_rec_thresh=0.30, no chroma; used
      for the noise-goes-to-quarantine test, which specifically needs beets to
      attempt MusicBrainz matching and fail so the file gets quarantined.

This module tests pipeline mechanics (inbox → library → source tag → .m3u),
not MusicBrainz matching accuracy, which is beets' own concern.
"""

from __future__ import annotations

import json

from conftest import (
    SCAN_IMAGE,
    beet_import_verbose,
    beet_ls,
    cat_in_volume,
    ls_in_volume,
    mkdir_in_volume,
    put_bytes,
    put_file,
    run_scan,
    scan_binds_test,
)


# ---------------------------------------------------------------------------
# Scenario 2 — Manual File Drop into Inbox
# ---------------------------------------------------------------------------


def test_file_drop_known_track_imported_to_library(
    docker_client, volumes, fixture_audio, beets_test_config
):
    """Case A: a well-known track lands in the library, not the quarantine."""
    put_file(docker_client, volumes, "/root/Music/inbox", fixture_audio)

    verbose = beet_import_verbose(
        docker_client, volumes, "/root/Music/inbox", beets_test_config
    )
    exit_code, logs = run_scan(
        docker_client, volumes,
        binds=scan_binds_test(volumes, beets_test_config),
    )
    assert exit_code == 0, f"music-scan exited {exit_code}. Logs:\n{logs}"

    library_files = ls_in_volume(docker_client, volumes, "/root/Music/library")
    assert library_files, (
        "No files found in /root/Music/library after import.\n"
        f"beet -vv import output:\n{verbose}\n"
        f"Scan logs:\n{logs}"
    )


def test_file_drop_inbox_cleared_after_import(
    docker_client, volumes, fixture_audio, beets_test_config
):
    """After import, no audio files remain loose in the inbox root."""
    put_file(docker_client, volumes, "/root/Music/inbox", fixture_audio)
    beet_import_verbose(
        docker_client, volumes, "/root/Music/inbox", beets_test_config
    )
    run_scan(
        docker_client, volumes,
        binds=scan_binds_test(volumes, beets_test_config),
    )

    # Only check inbox root (maxdepth 1); spotdl/ subdir is fine to have files
    result = docker_client.containers.run(
        SCAN_IMAGE,
        command=[
            "find", "/root/Music/inbox", "-maxdepth", "1",
            "-type", "f", "-name", "*.mp3",
        ],
        volumes={volumes["music"]: {"bind": "/root/Music", "mode": "ro"}},
        remove=True,
    )
    leftover = result.decode().strip()
    assert not leftover, (
        f"Audio files still in inbox root after import: {leftover}"
    )


def test_file_drop_noise_goes_to_quarantine(
    docker_client, volumes, beets_test_config
):
    """Case B: a noise file that won't match MusicBrainz is moved to quarantine."""
    # Generate a short noise file inside the container using ffmpeg
    docker_client.containers.run(
        SCAN_IMAGE,
        command=[
            "ffmpeg", "-f", "lavfi", "-i", "anoisesrc=d=5",
            "-ar", "44100", "-y", "/root/Music/inbox/noise.mp3",
        ],
        volumes=scan_binds_test(volumes, beets_test_config),
        remove=True,
    )

    exit_code, logs = run_scan(
        docker_client, volumes,
        binds=scan_binds_test(volumes, beets_test_config),
    )
    assert exit_code == 0, f"music-scan exited {exit_code}. Logs:\n{logs}"

    quarantine_files = ls_in_volume(docker_client, volumes, "/root/Music/quarantine")
    assert any("noise" in f for f in quarantine_files), (
        f"noise.mp3 not found in quarantine. Quarantine contents: {quarantine_files}"
    )


# ---------------------------------------------------------------------------
# Scenario 3 — Simulated spotdl Playlist Import
# ---------------------------------------------------------------------------


def test_playlist_import_source_tag_applied(
    docker_client, volumes, fixture_audio, beets_test_config
):
    """Track imported from a spotdl playlist dir is tagged source=<playlist-name>."""
    _setup_playlist(docker_client, volumes, "test-playlist", fixture_audio)

    verbose = beet_import_verbose(
        docker_client, volumes, "/root/Music/inbox", beets_test_config
    )
    exit_code, logs = run_scan(
        docker_client, volumes,
        binds=scan_binds_test(volumes, beets_test_config),
    )
    assert exit_code == 0, f"music-scan exited {exit_code}. Logs:\n{logs}"

    output = beet_ls(docker_client, volumes, "source:test-playlist")
    assert output.strip(), (
        "No tracks found with source=test-playlist in the beets library.\n"
        f"beet -vv import output:\n{verbose}\n"
        f"Scan logs:\n{logs}"
    )


def test_playlist_import_m3u_generated(
    docker_client, volumes, fixture_audio, beets_test_config
):
    """After scan, a .m3u file exists for the playlist with a relative library path."""
    _setup_playlist(docker_client, volumes, "test-playlist", fixture_audio)
    beet_import_verbose(
        docker_client, volumes, "/root/Music/inbox", beets_test_config
    )
    run_scan(
        docker_client, volumes,
        binds=scan_binds_test(volumes, beets_test_config),
    )

    m3u = cat_in_volume(docker_client, volumes, "/root/Music/playlists/test-playlist.m3u")
    assert m3u.strip(), "test-playlist.m3u is empty"
    # Paths must be relative (for portability)
    assert not any(line.startswith("/") for line in m3u.splitlines() if line), (
        f"Expected relative paths in .m3u, got absolute:\n{m3u}"
    )
    assert "../library/" in m3u, (
        f"Expected paths traversing to sibling library/ dir, got:\n{m3u}"
    )


# ---------------------------------------------------------------------------
# Scenario 4 — Duplicate Handling
# ---------------------------------------------------------------------------


def test_duplicate_import_skipped(
    docker_client, volumes, fixture_audio, beets_test_config
):
    """Re-importing an already-present track does not create a second beets entry."""
    _setup_playlist(docker_client, volumes, "test-playlist", fixture_audio)
    beet_import_verbose(docker_client, volumes, "/root/Music/inbox", beets_test_config)
    run_scan(
        docker_client, volumes,
        binds=scan_binds_test(volumes, beets_test_config),
    )

    # Import the same track again via a second scan
    _setup_playlist(docker_client, volumes, "test-playlist", fixture_audio)
    verbose = beet_import_verbose(
        docker_client, volumes, "/root/Music/inbox", beets_test_config
    )
    exit_code, logs = run_scan(
        docker_client, volumes,
        binds=scan_binds_test(volumes, beets_test_config),
    )
    assert exit_code == 0, f"Second scan exited {exit_code}. Logs:\n{logs}"

    output = beet_ls(docker_client, volumes, "source:test-playlist")
    entries = [line for line in output.strip().splitlines() if line]
    assert len(entries) == 1, (
        f"Expected exactly 1 beets entry for test-playlist, found {len(entries)}.\n"
        f"beet -vv import output (second import):\n{verbose}\n"
        + "\n".join(entries)
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_playlist(client, vol_names: dict, playlist_name: str, audio_path) -> None:
    """Create a fake spotdl playlist directory with the test track and a .spotdl file."""
    playlist_dir = f"/root/Music/inbox/spotdl/{playlist_name}"
    mkdir_in_volume(client, vol_names, playlist_dir)
    put_file(client, vol_names, playlist_dir, audio_path)
    put_bytes(
        client,
        vol_names,
        f"/root/Music/inbox/spotdl/{playlist_name}.spotdl",
        json.dumps({"type": "sync", "query": [], "songs": []}).encode(),
    )
