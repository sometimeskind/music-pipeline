"""Tests for pipeline.scan — pure-Python logic."""

import json
import os
from pathlib import Path
import unittest.mock as mock

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


# ---------------------------------------------------------------------------
# _process_pending_removals
# ---------------------------------------------------------------------------


def test_process_pending_removals_no_file(tmp_path: Path) -> None:
    from pipeline.scan import _process_pending_removals

    fake_path = tmp_path / ".pending-removals.json"
    with mock.patch("pipeline.scan.PENDING_REMOVALS", fake_path):
        count = _process_pending_removals()
    assert count == 0


def test_process_pending_removals_reads_and_deletes(tmp_path: Path) -> None:
    from pipeline.scan import _process_pending_removals

    fake_path = tmp_path / ".pending-removals.json"
    entries = [{"title": "Song A", "artist": "Artist 1", "source": "my-playlist"}]
    fake_path.write_text(json.dumps(entries), encoding="utf-8")

    mock_lib = mock.MagicMock()
    mock_lib.__enter__ = mock.MagicMock(return_value=mock_lib)
    mock_lib.__exit__ = mock.MagicMock(return_value=False)
    mock_lib.clear_source_tag = mock.MagicMock(return_value=True)

    with mock.patch("pipeline.scan.PENDING_REMOVALS", fake_path), \
         mock.patch("pipeline.scan.MusicLibrary", return_value=mock_lib):
        count = _process_pending_removals()

    assert count == 1
    assert not fake_path.exists()
    mock_lib.clear_source_tag.assert_called_once_with(
        title="Song A", artist="Artist 1", source="my-playlist"
    )


def test_process_pending_removals_empty_list(tmp_path: Path) -> None:
    from pipeline.scan import _process_pending_removals

    fake_path = tmp_path / ".pending-removals.json"
    fake_path.write_text("[]", encoding="utf-8")

    with mock.patch("pipeline.scan.PENDING_REMOVALS", fake_path):
        count = _process_pending_removals()

    assert count == 0
    assert not fake_path.exists()


def test_process_pending_removals_malformed_json_deletes_file(tmp_path: Path) -> None:
    """Malformed JSON must not leave the file in place (would break every future scan run)."""
    from pipeline.scan import _process_pending_removals

    fake_path = tmp_path / ".pending-removals.json"
    fake_path.write_text("not valid json {{{{", encoding="utf-8")

    with mock.patch("pipeline.scan.PENDING_REMOVALS", fake_path):
        count = _process_pending_removals()

    assert count == 0
    assert not fake_path.exists()  # file consumed even though JSON was invalid


def test_process_pending_removals_multi_entry(tmp_path: Path) -> None:
    from pipeline.scan import _process_pending_removals

    fake_path = tmp_path / ".pending-removals.json"
    entries = [
        {"title": "Song A", "artist": "Artist 1", "source": "playlist-1"},
        {"title": "Song B", "artist": "Artist 2", "source": "playlist-2"},
    ]
    fake_path.write_text(json.dumps(entries), encoding="utf-8")

    mock_lib = mock.MagicMock()
    mock_lib.__enter__ = mock.MagicMock(return_value=mock_lib)
    mock_lib.__exit__ = mock.MagicMock(return_value=False)
    mock_lib.clear_source_tag = mock.MagicMock(return_value=True)

    with mock.patch("pipeline.scan.PENDING_REMOVALS", fake_path), \
         mock.patch("pipeline.scan.MusicLibrary", return_value=mock_lib):
        count = _process_pending_removals()

    assert count == 2
    assert not fake_path.exists()
    assert mock_lib.clear_source_tag.call_count == 2


def test_process_pending_removals_file_deleted_before_processing(tmp_path: Path) -> None:
    """File is unlinked before processing so an exception mid-processing doesn't re-block scans."""
    from pipeline.scan import _process_pending_removals

    fake_path = tmp_path / ".pending-removals.json"
    entries = [{"title": "Song A", "artist": "Artist 1", "source": "my-playlist"}]
    fake_path.write_text(json.dumps(entries), encoding="utf-8")

    mock_lib = mock.MagicMock()
    mock_lib.__enter__ = mock.MagicMock(return_value=mock_lib)
    mock_lib.__exit__ = mock.MagicMock(return_value=False)
    mock_lib.clear_source_tag = mock.MagicMock(side_effect=RuntimeError("beets exploded"))

    with mock.patch("pipeline.scan.PENDING_REMOVALS", fake_path), \
         mock.patch("pipeline.scan.MusicLibrary", return_value=mock_lib):
        with pytest.raises(RuntimeError):
            _process_pending_removals()

    # File was deleted before processing started — future scans are unblocked
    assert not fake_path.exists()
