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

    # Root-level file
    (inbox / "unmatched.m4a").touch()
    # Subdirectory file (e.g. spotdl playlist or artist/album folder)
    subdir = inbox / "Artist" / "Album"
    subdir.mkdir(parents=True)
    (subdir / "01 - Track.mp3").touch()
    # Non-audio file — must not be touched
    (inbox / "readme.txt").touch()

    with mock.patch("pipeline.scan.INBOX", inbox), mock.patch("pipeline.scan.QUARANTINE", quarantine):
        moved = _quarantine_inbox_leftovers()

    assert moved == 2
    assert (quarantine / "unmatched.m4a").exists()
    assert (quarantine / "Artist" / "Album" / "01 - Track.mp3").exists()
    assert (inbox / "readme.txt").exists()  # non-audio left in place


# ---------------------------------------------------------------------------
# _process_pending_removals — new {tracks, remove_sources} format
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
    data = {"tracks": [{"title": "Song A", "artist": "Artist 1", "source": "my-playlist"}], "remove_sources": []}
    fake_path.write_text(json.dumps(data), encoding="utf-8")

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


def test_process_pending_removals_empty_data(tmp_path: Path) -> None:
    from pipeline.scan import _process_pending_removals

    fake_path = tmp_path / ".pending-removals.json"
    fake_path.write_text(json.dumps({"tracks": [], "remove_sources": []}), encoding="utf-8")

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


def test_process_pending_removals_multi_track(tmp_path: Path) -> None:
    from pipeline.scan import _process_pending_removals

    fake_path = tmp_path / ".pending-removals.json"
    data = {
        "tracks": [
            {"title": "Song A", "artist": "Artist 1", "source": "playlist-1"},
            {"title": "Song B", "artist": "Artist 2", "source": "playlist-2"},
        ],
        "remove_sources": [],
    }
    fake_path.write_text(json.dumps(data), encoding="utf-8")

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


def test_process_pending_removals_remove_sources(tmp_path: Path) -> None:
    """remove_sources → items_by_source called, source tags cleared, .m3u deleted."""
    from pipeline.scan import _process_pending_removals

    fake_path = tmp_path / ".pending-removals.json"
    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()
    m3u = playlists_dir / "old-playlist.m3u"
    m3u.touch()

    data = {"tracks": [], "remove_sources": ["old-playlist"]}
    fake_path.write_text(json.dumps(data), encoding="utf-8")

    mock_item = mock.MagicMock()
    mock_lib = mock.MagicMock()
    mock_lib.__enter__ = mock.MagicMock(return_value=mock_lib)
    mock_lib.__exit__ = mock.MagicMock(return_value=False)
    mock_lib.items_by_source = mock.MagicMock(return_value=[mock_item])

    with mock.patch("pipeline.scan.PENDING_REMOVALS", fake_path), \
         mock.patch("pipeline.scan.PLAYLISTS", playlists_dir), \
         mock.patch("pipeline.scan.MusicLibrary", return_value=mock_lib):
        count = _process_pending_removals()

    assert count == 1
    assert not fake_path.exists()
    mock_lib.items_by_source.assert_called_once_with("old-playlist")
    mock_item.__setitem__.assert_called_once_with("source", "")
    mock_item.store.assert_called_once()
    assert not m3u.exists()


def test_process_pending_removals_remove_source_missing_m3u(tmp_path: Path) -> None:
    """remove_sources processing doesn't fail if .m3u doesn't exist."""
    from pipeline.scan import _process_pending_removals

    fake_path = tmp_path / ".pending-removals.json"
    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()

    data = {"tracks": [], "remove_sources": ["gone-playlist"]}
    fake_path.write_text(json.dumps(data), encoding="utf-8")

    mock_lib = mock.MagicMock()
    mock_lib.__enter__ = mock.MagicMock(return_value=mock_lib)
    mock_lib.__exit__ = mock.MagicMock(return_value=False)
    mock_lib.items_by_source = mock.MagicMock(return_value=[])

    with mock.patch("pipeline.scan.PENDING_REMOVALS", fake_path), \
         mock.patch("pipeline.scan.PLAYLISTS", playlists_dir), \
         mock.patch("pipeline.scan.MusicLibrary", return_value=mock_lib):
        count = _process_pending_removals()

    assert count == 1


def test_process_pending_removals_backward_compat_old_list_format(tmp_path: Path) -> None:
    """Old list format is still processed correctly."""
    from pipeline.scan import _process_pending_removals

    fake_path = tmp_path / ".pending-removals.json"
    old_format = [{"title": "Song A", "artist": "Artist 1", "source": "my-playlist"}]
    fake_path.write_text(json.dumps(old_format), encoding="utf-8")

    mock_lib = mock.MagicMock()
    mock_lib.__enter__ = mock.MagicMock(return_value=mock_lib)
    mock_lib.__exit__ = mock.MagicMock(return_value=False)
    mock_lib.clear_source_tag = mock.MagicMock(return_value=True)

    with mock.patch("pipeline.scan.PENDING_REMOVALS", fake_path), \
         mock.patch("pipeline.scan.MusicLibrary", return_value=mock_lib):
        count = _process_pending_removals()

    assert count == 1
    mock_lib.clear_source_tag.assert_called_once_with(
        title="Song A", artist="Artist 1", source="my-playlist"
    )


# ---------------------------------------------------------------------------
# _regen_playlists
# ---------------------------------------------------------------------------


def test_regen_playlists_writes_m3u_with_relative_paths(tmp_path: Path) -> None:
    from pipeline.scan import _regen_playlists

    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    (spotdl_dir / "my-playlist.spotdl").touch()

    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()

    track_path = tmp_path / "library" / "Artist" / "Album" / "01 - Song.m4a"

    mock_lib = mock.MagicMock()
    mock_lib.__enter__ = mock.MagicMock(return_value=mock_lib)
    mock_lib.__exit__ = mock.MagicMock(return_value=False)
    mock_lib.paths_by_source = mock.MagicMock(return_value=[track_path])

    with mock.patch("pipeline.scan.SPOTDL_DIR", spotdl_dir), \
         mock.patch("pipeline.scan.PLAYLISTS", playlists_dir), \
         mock.patch("pipeline.scan.LIBRARY_DB", tmp_path / "library.db"), \
         mock.patch("pipeline.scan.MusicLibrary", return_value=mock_lib):
        _regen_playlists()

    m3u = playlists_dir / "my-playlist.m3u"
    assert m3u.exists()
    content = m3u.read_text(encoding="utf-8")
    expected_rel = os.path.relpath(track_path, playlists_dir)
    assert content == expected_rel + "\n"
    mock_lib.paths_by_source.assert_called_once_with("my-playlist")


def test_regen_playlists_empty_playlist_writes_empty_m3u(tmp_path: Path) -> None:
    from pipeline.scan import _regen_playlists

    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    (spotdl_dir / "empty-playlist.spotdl").touch()

    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()

    mock_lib = mock.MagicMock()
    mock_lib.__enter__ = mock.MagicMock(return_value=mock_lib)
    mock_lib.__exit__ = mock.MagicMock(return_value=False)
    mock_lib.paths_by_source = mock.MagicMock(return_value=[])

    with mock.patch("pipeline.scan.SPOTDL_DIR", spotdl_dir), \
         mock.patch("pipeline.scan.PLAYLISTS", playlists_dir), \
         mock.patch("pipeline.scan.LIBRARY_DB", tmp_path / "library.db"), \
         mock.patch("pipeline.scan.MusicLibrary", return_value=mock_lib):
        _regen_playlists()

    m3u = playlists_dir / "empty-playlist.m3u"
    assert m3u.exists()
    assert m3u.read_text(encoding="utf-8") == ""


# ---------------------------------------------------------------------------
# _snapshot_inbox, _name_words, _check_import_names
# ---------------------------------------------------------------------------


def test_snapshot_inbox_finds_audio_files(tmp_path: Path) -> None:
    from pipeline.scan import _snapshot_inbox

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "sub").mkdir()
    (inbox / "DJ Koze - Pick Up.m4a").touch()
    (inbox / "sub" / "Four Tet - Pyramid.flac").touch()
    (inbox / "readme.txt").touch()

    with mock.patch("pipeline.scan.AUDIO_EXTS", {".m4a", ".flac"}):
        stems = _snapshot_inbox(inbox)

    assert stems == ["DJ Koze - Pick Up", "Four Tet - Pyramid"]


def test_name_words_normalises_correctly() -> None:
    from pipeline.scan import _name_words

    assert _name_words("DJ Koze - Pick Up") == {"koze", "pick"}
    assert _name_words("Four Tet - Pyramid") == {"four", "tet", "pyramid"}
    # stop words and short words filtered
    assert "the" not in _name_words("The End of the World")
    assert "of" not in _name_words("Battle of Evermore")


def test_check_import_names_no_flags_on_good_match(caplog: pytest.LogCaptureFixture) -> None:
    from pipeline.scan import _check_import_names
    import logging

    inbox = ["DJ Koze - Pick Up", "Four Tet - Pyramid"]
    imported = [("Pick Up", "DJ Koze"), ("Pyramid", "Four Tet")]

    with caplog.at_level(logging.INFO, logger="pipeline.scan"):
        _check_import_names(inbox, imported)

    assert "Name check OK" in caplog.text
    assert "!!" not in caplog.text


def test_check_import_names_flags_bad_match(caplog: pytest.LogCaptureFixture) -> None:
    from pipeline.scan import _check_import_names
    import logging

    inbox = ["DJ Koze - Pick Up"]
    imported = [("Never Be Like You", "Flume")]  # completely different

    with caplog.at_level(logging.WARNING, logger="pipeline.scan"):
        _check_import_names(inbox, imported)

    assert "!!" in caplog.text
    assert "Never Be Like You" in caplog.text


def test_run_beet_import_asis_flag() -> None:
    from pipeline.process import run_beet_import
    import subprocess
    import unittest.mock as mock

    with mock.patch("subprocess.Popen") as mock_popen:
        mock_proc = mock.MagicMock()
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc
        run_beet_import(Path("/some/dir"), asis=True)

    cmd = mock_popen.call_args[0][0]
    assert "-A" in cmd
    assert "--quiet" in cmd


def test_run_beet_import_no_asis_flag_by_default() -> None:
    from pipeline.process import run_beet_import
    import subprocess
    import unittest.mock as mock

    with mock.patch("subprocess.Popen") as mock_popen:
        mock_proc = mock.MagicMock()
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc
        run_beet_import(Path("/some/dir"))

    cmd = mock_popen.call_args[0][0]
    assert "-A" not in cmd
    assert "--quiet" in cmd


def test_process_pending_removals_file_deleted_before_processing(tmp_path: Path) -> None:
    """File is unlinked before processing so an exception mid-processing doesn't re-block scans."""
    from pipeline.scan import _process_pending_removals

    fake_path = tmp_path / ".pending-removals.json"
    data = {"tracks": [{"title": "Song A", "artist": "Artist 1", "source": "my-playlist"}], "remove_sources": []}
    fake_path.write_text(json.dumps(data), encoding="utf-8")

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
