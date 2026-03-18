"""Tests for pipeline.scan — pure-Python logic."""

import os
from pathlib import Path

import pytest


def _relative_path(track_path: Path, playlists_dir: Path) -> str:
    """Replicate the relative-path logic used in _regen_playlists."""
    return os.path.relpath(track_path, playlists_dir)


def test_relative_path_sibling_dir() -> None:
    # track is in /root/Music/library/..., playlists is /root/Music/playlists/
    # .m3u entries must use ../ to traverse to the sibling library directory
    playlists = Path("/root/Music/playlists")
    track = Path("/root/Music/library/Artist/Album/01 - Song.m4a")
    rel = _relative_path(track, playlists)
    assert rel == "../library/Artist/Album/01 - Song.m4a"


def test_relative_path_same_dir_file() -> None:
    playlists = Path("/root/Music/playlists")
    track = Path("/root/Music/playlists/some.m3u")
    rel = _relative_path(track, playlists)
    assert rel == "some.m3u"


def test_count_quarantine_empty(tmp_path: Path) -> None:
    from pipeline.scan import _count_quarantine, QUARANTINE
    import unittest.mock as mock

    fake_quarantine = tmp_path / "quarantine"
    fake_quarantine.mkdir()

    with mock.patch("pipeline.scan.QUARANTINE", fake_quarantine):
        from pipeline import scan
        count = scan._count_quarantine()
    assert count == 0


def test_count_quarantine_with_files(tmp_path: Path) -> None:
    import unittest.mock as mock

    fake_quarantine = tmp_path / "quarantine"
    fake_quarantine.mkdir()
    (fake_quarantine / "a.mp3").touch()
    (fake_quarantine / "b.m4a").touch()

    with mock.patch("pipeline.scan.QUARANTINE", fake_quarantine):
        from pipeline import scan
        count = scan._count_quarantine()
    assert count == 2


def test_quarantine_leftovers(tmp_path: Path) -> None:
    import unittest.mock as mock
    from pipeline.scan import _quarantine_inbox_leftovers

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    quarantine = tmp_path / "quarantine"

    # Place an audio file and a non-audio file in the inbox root
    (inbox / "unmatched.m4a").touch()
    (inbox / "readme.txt").touch()

    with mock.patch("pipeline.scan.INBOX", inbox), mock.patch("pipeline.scan.QUARANTINE", quarantine):
        moved = _quarantine_inbox_leftovers()

    assert moved == 1
    assert (quarantine / "unmatched.m4a").exists()
    assert (inbox / "readme.txt").exists()  # non-audio left in place
