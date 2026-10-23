"""Scenario 2 — Manual File Drop, Scenario 3 — Playlist Import, Scenario 4 — Duplicate Handling.

All import tests use beets_asis_config (autotag=off, ASIS) so beets imports
without any MusicBrainz or network calls. This makes tests stable in CI.

The noise → quarantine test uses the production config (autotag=on) to exercise
the real MusicBrainz path — a noise file has no match at any confidence level.
"""

from __future__ import annotations

import json

from conftest import (
    SERVICE_IMAGE,
    beet_ls,
    cat_in_volume,
    ls_in_volume,
    mkdir_in_volume,
    put_bytes,
    put_file,
    run_scan,
    scan_binds,
    scan_binds_test,
)


# ---------------------------------------------------------------------------
# Scenario 2 — Manual File Drop into Inbox
# ---------------------------------------------------------------------------


def test_file_drop_known_track_imported_to_library(
    docker_client, volumes, fixture_audio, beets_asis_config
):
    """Case A: a track in the inbox is moved to the library by music-scan."""
    put_file(docker_client, volumes, "/root/Music/inbox", fixture_audio)

    exit_code, logs = run_scan(
        docker_client, volumes,
        binds=scan_binds_test(volumes, beets_asis_config),
    )
    assert exit_code == 0, f"music-scan exited {exit_code}. Logs:\n{logs}"

    library_files = ls_in_volume(docker_client, volumes, "/root/Music/library")
    assert library_files, (
        "No files found in /root/Music/library after import.\n"
        f"Scan logs:\n{logs}"
    )


def test_file_drop_noise_goes_to_quarantine(docker_client, volumes):
    """Case B: a tagless noise file that won't match MusicBrainz is moved to quarantine
    and stays there — the asis pass skips files without sufficient embedded metadata."""
    # Generate a short noise file inside the container using ffmpeg
    docker_client.containers.run(
        SERVICE_IMAGE,
        command=[
            "ffmpeg", "-f", "lavfi", "-i", "anoisesrc=d=5",
            "-ar", "44100", "-y", "/root/Music/inbox/noise.mp3",
        ],
        volumes=scan_binds(volumes),
        remove=True,
    )

    exit_code, logs = run_scan(docker_client, volumes)
    assert exit_code == 0, f"music-scan exited {exit_code}. Logs:\n{logs}"

    quarantine_files = ls_in_volume(docker_client, volumes, "/root/Music/quarantine")
    assert any("noise" in f for f in quarantine_files), (
        f"noise.mp3 not found in quarantine. Quarantine contents: {quarantine_files}"
    )


# ---------------------------------------------------------------------------
# Scenario 3 — Simulated spotdl Playlist Import
# ---------------------------------------------------------------------------


def test_playlist_import_source_tag_applied(
    docker_client, volumes, fixture_audio, beets_asis_config
):
    """Track imported from a spotdl playlist dir is tagged sources=<playlist-name>."""
    _setup_playlist(docker_client, volumes, "test-playlist", fixture_audio)

    exit_code, logs = run_scan(
        docker_client, volumes,
        binds=scan_binds_test(volumes, beets_asis_config),
    )
    assert exit_code == 0, f"music-scan exited {exit_code}. Logs:\n{logs}"

    output = beet_ls(docker_client, volumes, "sources:test-playlist")
    assert output.strip(), (
        "No tracks found with sources=test-playlist in the beets library.\n"
        f"Scan logs:\n{logs}"
    )


def test_playlist_import_m3u_generated(
    docker_client, volumes, fixture_audio, beets_asis_config
):
    """A playlist's .m3u lists its store's entries with relative library paths.

    The fixture has no Spotify ID or ISRC for a store entry to resolve by, so
    the store holds it as a local entry (homelab#1914), written once the first
    scan has imported it and given it an id.
    """
    _setup_playlist(docker_client, volumes, "test-playlist", fixture_audio)
    binds = scan_binds_test(volumes, beets_asis_config)
    run_scan(docker_client, volumes, binds=binds)

    # Imported, but on no store entry: sources alone never lists a track.
    assert not cat_in_volume(docker_client, volumes, "/root/Music/playlists/test-playlist.m3u").strip()

    ids = [line.strip() for line in beet_ls(docker_client, volumes, "-f$id").splitlines() if line.strip().isdigit()]
    assert len(ids) == 1, f"expected one imported item, got {ids}"
    local = {"local": {"id": int(ids[0]), "path": "", "title": "7 Ghosts I", "artist": "Nine Inch Nails"}}
    put_bytes(
        docker_client,
        volumes,
        "/root/Music/inbox/spotdl/.store/test-playlist.json",
        json.dumps({"version": 1, "playlist": "test-playlist", "entries": [local]}).encode(),
    )
    run_scan(docker_client, volumes, binds=binds)

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
    docker_client, volumes, fixture_audio, beets_asis_config
):
    """Re-importing an already-present track does not create a second beets entry."""
    _setup_playlist(docker_client, volumes, "test-playlist", fixture_audio)
    run_scan(
        docker_client, volumes,
        binds=scan_binds_test(volumes, beets_asis_config),
    )

    # Import the same track again via a second scan
    _setup_playlist(docker_client, volumes, "test-playlist", fixture_audio)
    exit_code, logs = run_scan(
        docker_client, volumes,
        binds=scan_binds_test(volumes, beets_asis_config),
    )
    assert exit_code == 0, f"Second scan exited {exit_code}. Logs:\n{logs}"

    output = beet_ls(docker_client, volumes, "sources:test-playlist")
    entries = [line for line in output.strip().splitlines() if line]
    assert len(entries) == 1, (
        f"Expected exactly 1 beets entry for test-playlist, found {len(entries)}.\n"
        + "\n".join(entries)
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_playlist(client, vol_names: dict, playlist_name: str, audio_path) -> None:
    """Create a fake spotdl playlist directory with the test track and an empty
    playlist store, which is what gives the playlist a .m3u (homelab#1914)."""
    playlist_dir = f"/root/Music/inbox/spotdl/{playlist_name}"
    mkdir_in_volume(client, vol_names, playlist_dir)
    put_file(client, vol_names, playlist_dir, audio_path)
    mkdir_in_volume(client, vol_names, "/root/Music/inbox/spotdl/.store")
    put_bytes(
        client,
        vol_names,
        f"/root/Music/inbox/spotdl/.store/{playlist_name}.json",
        json.dumps({"version": 1, "playlist": playlist_name, "entries": []}).encode(),
    )
