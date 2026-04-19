"""Tests for pipeline.scan — pure-Python logic."""

import json
import os
from pathlib import Path
import unittest.mock as mock

import pytest

from music_fetch.ingest import PendingRemovals, RemovedTrack


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
    from music_scan.scan import _count_quarantine, QUARANTINE
    import unittest.mock as mock

    fake_quarantine = tmp_path / "quarantine"
    fake_quarantine.mkdir()

    with mock.patch("music_scan.scan.QUARANTINE", fake_quarantine):
        from music_scan import scan
        count = scan._count_quarantine()
    assert count == 0


def test_count_quarantine_with_files(tmp_path: Path) -> None:
    import unittest.mock as mock

    fake_quarantine = tmp_path / "quarantine"
    fake_quarantine.mkdir()
    (fake_quarantine / "a.mp3").touch()
    (fake_quarantine / "b.m4a").touch()

    with mock.patch("music_scan.scan.QUARANTINE", fake_quarantine):
        from music_scan import scan
        count = scan._count_quarantine()
    assert count == 2


def test_quarantine_leftovers(tmp_path: Path) -> None:
    from music_scan.scan import _quarantine_inbox_leftovers

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

    with mock.patch("music_scan.scan.INBOX", inbox), mock.patch("music_scan.scan.QUARANTINE", quarantine):
        moved = _quarantine_inbox_leftovers()

    assert moved == 2
    assert (quarantine / "unmatched.m4a").exists()
    assert (quarantine / "Artist" / "Album" / "01 - Track.mp3").exists()
    assert (inbox / "readme.txt").exists()  # non-audio left in place


# ---------------------------------------------------------------------------
# _apply_pending_removals
# ---------------------------------------------------------------------------


def _make_mock_lib() -> mock.MagicMock:
    mock_lib = mock.MagicMock()
    mock_lib.__enter__ = mock.MagicMock(return_value=mock_lib)
    mock_lib.__exit__ = mock.MagicMock(return_value=False)
    mock_lib.clear_source_tag = mock.MagicMock(return_value=True)
    mock_lib.items_by_source = mock.MagicMock(return_value=[])
    mock_lib.items_added_since = mock.MagicMock(return_value=[])
    mock_lib.paths_by_source = mock.MagicMock(return_value=[])
    return mock_lib


def test_apply_pending_removals_clears_source_tags(tmp_path: Path) -> None:
    """Track removals call lib.clear_source_tag with typed fields."""
    from music_scan.scan import _apply_pending_removals

    pending = PendingRemovals(
        tracks=[RemovedTrack(title="Song A", artist="Artist 1", source="my-playlist")],
        remove_sources=[],
    )
    mock_lib = _make_mock_lib()

    count = _apply_pending_removals(pending, mock_lib)

    assert count == 1
    mock_lib.clear_source_tag.assert_called_once_with(
        title="Song A", artist="Artist 1", source="my-playlist"
    )


def test_apply_pending_removals_remove_sources(tmp_path: Path) -> None:
    """Source removals call items_by_source, clear tags, and delete the .m3u."""
    from music_scan.scan import _apply_pending_removals

    playlists = tmp_path / "playlists"
    playlists.mkdir()
    m3u = playlists / "old-playlist.m3u"
    m3u.touch()

    pending = PendingRemovals(tracks=[], remove_sources=["old-playlist"])
    mock_item = mock.MagicMock()
    mock_lib = _make_mock_lib()
    mock_lib.items_by_source = mock.MagicMock(return_value=[mock_item])

    with mock.patch("music_scan.scan.PLAYLISTS", playlists):
        count = _apply_pending_removals(pending, mock_lib)

    assert count == 1
    mock_lib.items_by_source.assert_called_once_with("old-playlist")
    mock_item.__setitem__.assert_called_once_with("source", "")
    mock_item.store.assert_called_once()
    assert not m3u.exists()


def test_apply_pending_removals_returns_total_count() -> None:
    """Return value is tracks + sources combined."""
    from music_scan.scan import _apply_pending_removals

    pending = PendingRemovals(
        tracks=[
            RemovedTrack(title="A", artist="X", source="pl1"),
            RemovedTrack(title="B", artist="Y", source="pl2"),
        ],
        remove_sources=["gone-pl"],
    )
    mock_lib = _make_mock_lib()

    with mock.patch("music_scan.scan.PLAYLISTS", mock.MagicMock()):
        count = _apply_pending_removals(pending, mock_lib)

    assert count == 3


def test_run_with_pending_none_skips_apply(tmp_path: Path) -> None:
    """run(pending=None) never calls _apply_pending_removals."""
    from music_scan import scan

    with mock.patch("music_scan.scan._apply_pending_removals") as mock_apply, \
         mock.patch("music_scan.scan.MusicLibrary", return_value=_make_mock_lib()), \
         mock.patch("music_scan.scan.run_beet_import"), \
         mock.patch("music_scan.scan.run_beet_update"), \
         mock.patch("music_scan.scan._move_asis_eligible", return_value=0), \
         mock.patch("music_scan.scan.INBOX", tmp_path), \
         mock.patch("music_scan.scan.SPOTDL_DIR", tmp_path), \
         mock.patch("music_scan.scan.QUARANTINE", tmp_path), \
         mock.patch("music_scan.scan.PLAYLISTS", tmp_path), \
         mock.patch("music_scan.scan.ScanMetrics"):
        scan.run(pending=None)

    mock_apply.assert_not_called()


# ---------------------------------------------------------------------------
# _regen_playlists
# ---------------------------------------------------------------------------


def test_regen_playlists_writes_m3u_with_relative_paths(tmp_path: Path) -> None:
    from music_scan.scan import _regen_playlists

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

    with mock.patch("music_scan.scan.SPOTDL_DIR", spotdl_dir), \
         mock.patch("music_scan.scan.PLAYLISTS", playlists_dir), \
         mock.patch("music_scan.scan.LIBRARY_DB", tmp_path / "library.db"), \
         mock.patch("music_scan.scan.MusicLibrary", return_value=mock_lib):
        _regen_playlists()

    m3u = playlists_dir / "my-playlist.m3u"
    assert m3u.exists()
    content = m3u.read_text(encoding="utf-8")
    expected_rel = os.path.relpath(track_path, playlists_dir)
    assert content == expected_rel + "\n"
    mock_lib.paths_by_source.assert_called_once_with("my-playlist")


def test_regen_playlists_empty_playlist_writes_empty_m3u(tmp_path: Path) -> None:
    from music_scan.scan import _regen_playlists

    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    (spotdl_dir / "empty-playlist.spotdl").touch()

    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()

    mock_lib = mock.MagicMock()
    mock_lib.__enter__ = mock.MagicMock(return_value=mock_lib)
    mock_lib.__exit__ = mock.MagicMock(return_value=False)
    mock_lib.paths_by_source = mock.MagicMock(return_value=[])

    with mock.patch("music_scan.scan.SPOTDL_DIR", spotdl_dir), \
         mock.patch("music_scan.scan.PLAYLISTS", playlists_dir), \
         mock.patch("music_scan.scan.LIBRARY_DB", tmp_path / "library.db"), \
         mock.patch("music_scan.scan.MusicLibrary", return_value=mock_lib):
        _regen_playlists()

    m3u = playlists_dir / "empty-playlist.m3u"
    assert m3u.exists()
    assert m3u.read_text(encoding="utf-8") == ""


# ---------------------------------------------------------------------------
# _snapshot_inbox, _name_words, _check_import_names
# ---------------------------------------------------------------------------


def test_snapshot_inbox_finds_audio_files(tmp_path: Path) -> None:
    from music_scan.scan import _snapshot_inbox

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "sub").mkdir()
    (inbox / "DJ Koze - Pick Up.m4a").touch()
    (inbox / "sub" / "Four Tet - Pyramid.flac").touch()
    (inbox / "readme.txt").touch()

    with mock.patch("music_scan.scan.AUDIO_EXTS", {".m4a", ".flac"}):
        stems = _snapshot_inbox(inbox)

    assert stems == ["DJ Koze - Pick Up", "Four Tet - Pyramid"]


def test_name_words_normalises_correctly() -> None:
    from music_scan.scan import _name_words

    assert _name_words("DJ Koze - Pick Up") == {"koze", "pick"}
    assert _name_words("Four Tet - Pyramid") == {"four", "tet", "pyramid"}
    # stop words and short words filtered
    assert "the" not in _name_words("The End of the World")
    assert "of" not in _name_words("Battle of Evermore")


def test_check_import_names_no_flags_on_good_match(caplog: pytest.LogCaptureFixture) -> None:
    from music_scan.scan import _check_import_names
    import logging

    inbox = ["DJ Koze - Pick Up", "Four Tet - Pyramid"]
    imported = [("Pick Up", "DJ Koze"), ("Pyramid", "Four Tet")]

    with caplog.at_level(logging.INFO, logger="music_scan.scan"):
        _check_import_names(inbox, imported)

    assert "Name check OK" in caplog.text
    assert "!!" not in caplog.text


def test_check_import_names_flags_bad_match(caplog: pytest.LogCaptureFixture) -> None:
    from music_scan.scan import _check_import_names
    import logging

    inbox = ["DJ Koze - Pick Up"]
    imported = [("Never Be Like You", "Flume")]  # completely different

    with caplog.at_level(logging.WARNING, logger="music_scan.scan"):
        _check_import_names(inbox, imported)

    assert "!!" in caplog.text
    assert "Never Be Like You" in caplog.text


def _fake_tags(overrides: dict | None = None) -> dict:
    base = {"title": ["Song"], "artist": ["Artist"], "album": ["Album"], "tracknumber": ["1"]}
    if overrides:
        base.update(overrides)
    return base


def test_move_asis_eligible_moves_fully_tagged_file(tmp_path: Path) -> None:
    from music_scan.scan import _move_asis_eligible

    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    (quarantine / "tagged.m4a").touch()

    with mock.patch("mutagen.File", return_value=_fake_tags()):
        count = _move_asis_eligible(quarantine, staging)

    assert count == 1
    assert (staging / "tagged.m4a").exists()
    assert not (quarantine / "tagged.m4a").exists()


def test_move_asis_eligible_leaves_untagged_file(tmp_path: Path) -> None:
    from music_scan.scan import _move_asis_eligible

    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    (quarantine / "noise.mp3").touch()

    with mock.patch("mutagen.File", return_value={}):
        count = _move_asis_eligible(quarantine, staging)

    assert count == 0
    assert (quarantine / "noise.mp3").exists()
    assert not list(staging.iterdir())


def test_move_asis_eligible_leaves_partially_tagged_file(tmp_path: Path) -> None:
    from music_scan.scan import _move_asis_eligible

    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    (quarantine / "partial.flac").touch()

    # Has title and artist but missing album and tracknumber
    with mock.patch("mutagen.File", return_value=_fake_tags({"album": None, "tracknumber": None})):
        count = _move_asis_eligible(quarantine, staging)

    assert count == 0
    assert (quarantine / "partial.flac").exists()


def test_run_beet_import_asis_flag() -> None:
    from music_scan.process import run_beet_import
    import unittest.mock as mock

    mock_proc = mock.MagicMock()
    mock_proc.wait.return_value = 0
    with mock.patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
         mock.patch("music_scan.process.IMPORT_LOG") as mock_log:
        mock_log.exists.return_value = False
        run_beet_import(Path("/some/dir"), asis=True)

    cmd = mock_popen.call_args[0][0]
    assert "-A" in cmd
    assert "--quiet" in cmd


def test_run_beet_import_no_asis_flag_by_default() -> None:
    from music_scan.process import run_beet_import
    import unittest.mock as mock

    mock_proc = mock.MagicMock()
    mock_proc.wait.return_value = 0
    with mock.patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
         mock.patch("music_scan.process.IMPORT_LOG") as mock_log:
        mock_log.exists.return_value = False
        run_beet_import(Path("/some/dir"))

    cmd = mock_popen.call_args[0][0]
    assert "-A" not in cmd
    assert "--quiet" in cmd


