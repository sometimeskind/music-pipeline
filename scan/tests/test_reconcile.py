"""Tests for music_scan.reconcile — pure-Python logic (no beets process, no network)."""

from __future__ import annotations

import json
import unittest.mock as mock
from pathlib import Path

import pytest

from music_scan.reconcile import reconcile_snapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_snapshot(path: Path, playlist_name: str, urls: list[str]) -> Path:
    spotdl_file = path / f"{playlist_name}.spotdl"
    songs = [{"url": url, "name": f"Track {i}", "artists": ["Artist"]} for i, url in enumerate(urls)]
    spotdl_file.write_text(
        json.dumps({"type": "sync", "query": ["https://open.spotify.com/playlist/x"], "songs": songs}),
        encoding="utf-8",
    )
    return spotdl_file


def _make_library_paths(base: Path, count: int) -> list[Path]:
    staging = base / "staging"
    staging.mkdir(exist_ok=True)
    paths = [staging / f"track_{i}.m4a" for i in range(count)]
    for p in paths:
        p.touch()
    return paths


def _mock_library(paths: list[Path]) -> mock.Mock:
    lib = mock.Mock()
    lib.paths_by_source.return_value = paths
    return lib


# ---------------------------------------------------------------------------
# reconcile_snapshot — all URLs present in library
# ---------------------------------------------------------------------------


def test_reconcile_all_present_no_changes(tmp_path: Path) -> None:
    """When every snapshot URL has a matching file in the library, nothing is dropped."""
    urls = [
        "https://open.spotify.com/track/A",
        "https://open.spotify.com/track/B",
        "https://open.spotify.com/track/C",
    ]
    spotdl_file = _write_snapshot(tmp_path, "mypl", urls)
    lib_paths = _make_library_paths(tmp_path, len(urls))
    lib = _mock_library(lib_paths)

    with mock.patch("music_scan.reconcile._read_spotify_url", side_effect=urls):
        dropped = reconcile_snapshot(spotdl_file, lib)

    assert dropped == 0
    data = json.loads(spotdl_file.read_text(encoding="utf-8"))
    assert {s["url"] for s in data["songs"]} == set(urls)


# ---------------------------------------------------------------------------
# reconcile_snapshot — one URL missing from library
# ---------------------------------------------------------------------------


def test_reconcile_one_missing_dropped(tmp_path: Path) -> None:
    """A URL absent from both library and quarantine is dropped and logged."""
    urls = [
        "https://open.spotify.com/track/A",
        "https://open.spotify.com/track/B",
        "https://open.spotify.com/track/C",
    ]
    spotdl_file = _write_snapshot(tmp_path, "mypl", urls)
    # Library only has A and B
    lib_paths = _make_library_paths(tmp_path, 2)
    lib = _mock_library(lib_paths)

    with mock.patch(
        "music_scan.reconcile._read_spotify_url",
        side_effect=["https://open.spotify.com/track/A", "https://open.spotify.com/track/B"],
    ):
        dropped = reconcile_snapshot(spotdl_file, lib)

    assert dropped == 1
    data = json.loads(spotdl_file.read_text(encoding="utf-8"))
    persisted = {s["url"] for s in data["songs"]}
    assert persisted == {"https://open.spotify.com/track/A", "https://open.spotify.com/track/B"}
    assert "https://open.spotify.com/track/C" not in persisted


def test_reconcile_one_missing_logs_warning(tmp_path: Path, caplog) -> None:
    """A WARNING is emitted for each dropped URL."""
    import logging

    urls = ["https://open.spotify.com/track/A", "https://open.spotify.com/track/B"]
    spotdl_file = _write_snapshot(tmp_path, "mypl", urls)
    lib = _mock_library(_make_library_paths(tmp_path, 1))

    with mock.patch(
        "music_scan.reconcile._read_spotify_url",
        side_effect=["https://open.spotify.com/track/A"],
    ):
        with caplog.at_level(logging.WARNING, logger="music_scan.reconcile"):
            reconcile_snapshot(spotdl_file, lib)

    assert any("https://open.spotify.com/track/B" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# reconcile_snapshot — all URLs missing from library
# ---------------------------------------------------------------------------


def test_reconcile_all_missing_drops_all(tmp_path: Path) -> None:
    """When no URLs are in the library or quarantine, all are dropped."""
    urls = ["https://open.spotify.com/track/A", "https://open.spotify.com/track/B"]
    spotdl_file = _write_snapshot(tmp_path, "mypl", urls)
    lib = _mock_library([])

    dropped = reconcile_snapshot(spotdl_file, lib)

    assert dropped == 2
    data = json.loads(spotdl_file.read_text(encoding="utf-8"))
    assert data["songs"] == []


# ---------------------------------------------------------------------------
# reconcile_snapshot — quarantine keeps URLs alive
# ---------------------------------------------------------------------------


def test_reconcile_quarantined_url_not_dropped(tmp_path: Path) -> None:
    """A URL whose file is in quarantine is retained (not re-downloaded)."""
    url_a = "https://open.spotify.com/track/A"
    url_b = "https://open.spotify.com/track/B"
    spotdl_file = _write_snapshot(tmp_path, "mypl", [url_a, url_b])

    lib_paths = _make_library_paths(tmp_path, 1)
    lib = _mock_library(lib_paths)

    # A is in the library; B is in quarantine (passed as safe_urls)
    with mock.patch("music_scan.reconcile._read_spotify_url", return_value=url_a):
        dropped = reconcile_snapshot(spotdl_file, lib, safe_urls={url_b})

    assert dropped == 0
    data = json.loads(spotdl_file.read_text(encoding="utf-8"))
    assert {s["url"] for s in data["songs"]} == {url_a, url_b}


# ---------------------------------------------------------------------------
# reconcile_snapshot — stale beets paths handled gracefully
# ---------------------------------------------------------------------------


def test_reconcile_stale_library_path_treated_as_missing(tmp_path: Path) -> None:
    """A beets library entry whose file no longer exists is not counted as verified."""
    url = "https://open.spotify.com/track/A"
    spotdl_file = _write_snapshot(tmp_path, "mypl", [url])

    # Library returns a path that does not exist on disk
    ghost_path = tmp_path / "staging" / "ghost.m4a"
    lib = _mock_library([ghost_path])

    dropped = reconcile_snapshot(spotdl_file, lib)

    assert dropped == 1
    data = json.loads(spotdl_file.read_text(encoding="utf-8"))
    assert data["songs"] == []


# ---------------------------------------------------------------------------
# reconcile_snapshot — empty snapshot is a no-op
# ---------------------------------------------------------------------------


def test_reconcile_empty_snapshot_no_op(tmp_path: Path) -> None:
    spotdl_file = tmp_path / "mypl.spotdl"
    spotdl_file.write_text(
        json.dumps({"type": "sync", "query": ["https://..."], "songs": []}),
        encoding="utf-8",
    )
    lib = _mock_library([])

    dropped = reconcile_snapshot(spotdl_file, lib)

    assert dropped == 0
    lib.paths_by_source.assert_not_called()
