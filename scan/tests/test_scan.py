"""Tests for pipeline.scan — pure-Python logic."""

import json
import logging
import os
from pathlib import Path
import unittest.mock as mock

import pytest

from music_fetch.ingest import PendingRemovals, RemovedTrack


def _relative_path(track_path: Path, playlists_dir: Path) -> str:
    """Replicate the relative-path logic used in regen_playlists."""
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
    from music_scan.scan import quarantine_inbox_leftovers

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
        moved = quarantine_inbox_leftovers()

    assert moved == 2
    assert (quarantine / "unmatched.m4a").exists()
    assert (quarantine / "Artist" / "Album" / "01 - Track.mp3").exists()
    assert (inbox / "readme.txt").exists()  # non-audio left in place


# ---------------------------------------------------------------------------
# apply_pending_removals
# ---------------------------------------------------------------------------


def _make_mock_lib() -> mock.MagicMock:
    mock_lib = mock.MagicMock()
    mock_lib.__enter__ = mock.MagicMock(return_value=mock_lib)
    mock_lib.__exit__ = mock.MagicMock(return_value=False)
    mock_lib.clear_source_tag = mock.MagicMock(side_effect=lambda **kw: [{"title": kw["title"], "artist": kw["artist"]}])
    mock_lib.items_by_source = mock.MagicMock(return_value=[])
    mock_lib.items_added_since = mock.MagicMock(return_value=[])
    mock_lib.paths_by_source = mock.MagicMock(return_value=[])
    mock_lib.item_count = mock.MagicMock(return_value=0)
    return mock_lib


def testapply_pending_removals_clears_source_tags(tmp_path: Path) -> None:
    """Track removals call lib.clear_source_tag with typed fields."""
    from music_scan.scan import apply_pending_removals

    pending = PendingRemovals(
        tracks=[RemovedTrack(title="Song A", artist="Artist 1", source="my-playlist")],
        remove_sources=[],
    )
    mock_lib = _make_mock_lib()

    count = apply_pending_removals(pending, mock_lib)

    assert count == 1
    mock_lib.clear_source_tag.assert_called_once_with(
        title="Song A", artist="Artist 1", source="my-playlist", spotify_id=None, isrc=None
    )


def test_removed_spotdl_entry_clears_that_items_source(tmp_path: Path) -> None:
    """End to end (#169): a track dropped from the .spotdl snapshot unlinks the item
    with its Spotify ID, not the same-titled one beside it."""
    from beets.library import Item

    from music_fetch import ingest
    from music_fetch.metrics import IngestMetrics
    from music_fetch.spotdl_ops import SyncResult
    from music_scan.library import MusicLibrary
    from music_scan.scan import apply_pending_removals

    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    live_url = "https://open.spotify.com/track/LIVE"
    (spotdl_dir / "a.spotdl").write_text(json.dumps({
        "type": "sync",
        "query": ["https://open.spotify.com/playlist/X"],
        "songs": [
            {"url": "https://open.spotify.com/track/STUDIO", "name": "Song", "artists": ["Artist"]},
            {"url": live_url, "name": "Song", "artists": ["Artist"]},
        ],
    }), encoding="utf-8")

    with mock.patch.object(ingest, "SPOTDL_DIR", spotdl_dir), \
         mock.patch.object(ingest, "CONF_PATH", tmp_path / "missing.conf"), \
         mock.patch.object(ingest, "COOKIE_FILE", tmp_path / "cookies.txt"), \
         mock.patch.object(ingest, "FAILURES_FILE", tmp_path / ".failures.json"), \
         mock.patch("music_fetch.ingest.sync_playlist", return_value=SyncResult({live_url}, 0, 0, 0, 0, {})), \
         mock.patch("music_fetch.ingest.time.sleep"):
        pending = ingest.sync_playlists([], IngestMetrics())

    lib = MusicLibrary(tmp_path / "library.db", tmp_path)
    with lib:
        items = {}
        for sid in ("STUDIO", "LIVE"):
            items[sid] = Item(title="Song", artist="Artist", sources="a", spotify_ids=sid)
            lib._lib.add(items[sid])

        assert apply_pending_removals(pending, lib) == 1
        assert lib._lib.get_item(items["LIVE"].id).get("sources") == ""
        assert lib._lib.get_item(items["STUDIO"].id).get("sources") == "a"


def testapply_pending_removals_remove_sources(tmp_path: Path) -> None:
    """Source removals call items_by_source, clear tags, and delete the .m3u."""
    from music_scan.scan import apply_pending_removals

    playlists = tmp_path / "playlists"
    playlists.mkdir()
    m3u = playlists / "old-playlist.m3u"
    m3u.touch()

    pending = PendingRemovals(tracks=[], remove_sources=["old-playlist"])
    mock_item = mock.MagicMock()
    mock_item.get = mock.MagicMock(return_value="old-playlist")
    mock_lib = _make_mock_lib()
    mock_lib.items_by_source = mock.MagicMock(return_value=[mock_item])

    with mock.patch("music_scan.scan.PLAYLISTS", playlists):
        count = apply_pending_removals(pending, mock_lib)

    assert count == 1
    mock_lib.items_by_source.assert_called_once_with("old-playlist")
    mock_item.__setitem__.assert_called_once_with("sources", "")
    mock_item.store.assert_called_once()
    assert not m3u.exists()


def testapply_pending_removals_strips_one_source_from_multi(tmp_path: Path) -> None:
    """Removing a playlist strips only that name from a comma-separated sources field."""
    from music_scan.scan import apply_pending_removals

    playlists = tmp_path / "playlists"
    playlists.mkdir()

    pending = PendingRemovals(tracks=[], remove_sources=["old-playlist"])
    mock_item = mock.MagicMock()
    mock_item.get = mock.MagicMock(return_value="old-playlist,other-playlist")
    mock_lib = _make_mock_lib()
    mock_lib.items_by_source = mock.MagicMock(return_value=[mock_item])

    with mock.patch("music_scan.scan.PLAYLISTS", playlists):
        apply_pending_removals(pending, mock_lib)

    mock_item.__setitem__.assert_called_once_with("sources", "other-playlist")


def testapply_pending_removals_returns_total_count() -> None:
    """Return value counts items modified, by tracks and sources combined."""
    from music_scan.scan import apply_pending_removals

    pending = PendingRemovals(
        tracks=[
            RemovedTrack(title="A", artist="X", source="pl1"),
            RemovedTrack(title="B", artist="Y", source="pl2"),
        ],
        remove_sources=["gone-pl"],
    )
    mock_lib = _make_mock_lib()
    mock_lib.items_by_source = mock.MagicMock(return_value=[mock.MagicMock(), mock.MagicMock()])

    with mock.patch("music_scan.scan.PLAYLISTS", mock.MagicMock()):
        count = apply_pending_removals(pending, mock_lib)

    assert count == 4


def testapply_pending_removals_logs_unlinks_and_skips_not_found(caplog) -> None:
    """One [UNLINK] line per cleared item; an entry with no item isn't counted (#190)."""
    from music_scan.scan import apply_pending_removals

    pending = PendingRemovals(
        tracks=[
            RemovedTrack(title="A", artist="X", source="later", spotify_id="ID1"),
            RemovedTrack(title="B", artist="Y", source="later"),
        ],
        remove_sources=[],
    )
    mock_lib = _make_mock_lib()
    mock_lib.clear_source_tag = mock.MagicMock(side_effect=[[{"title": "A (Remastered)", "artist": "X"}], []])

    with caplog.at_level("INFO", logger="music_scan.scan"):
        count = apply_pending_removals(pending, mock_lib)

    assert count == 1
    assert "[UNLINK] later: A (Remastered) — X" in caplog.text
    assert "not found in beets" in caplog.text
    assert "on 1 item(s); 1 removed entr(ies) not found" in caplog.text


def test_run_trigger_scan_failure_sets_success_false(tmp_path: Path) -> None:
    """When trigger_scan raises, last_run_success=0 is pushed and the exception propagates."""
    from music_scan import scan

    captured: list = []

    class FakeMetrics:
        def __init__(self):
            self.success = True
            self.failure_reason = ""
            self.tracks_imported = 0
            self.tracks_removed = 0
            self.quarantined_tracks = 0
            self.duration_seconds = 0
            captured.append(self)

        def push(self):
            pass

    with mock.patch("music_scan.scan.MusicLibrary", return_value=_make_mock_lib()), \
         mock.patch("music_scan.scan.run_beet_import"), \
         mock.patch("music_scan.scan.run_beet_update"), \
         mock.patch("music_scan.scan._move_asis_eligible", return_value=0), \
         mock.patch("music_scan.scan.INBOX", tmp_path), \
         mock.patch("music_scan.scan.SPOTDL_DIR", tmp_path), \
         mock.patch("music_scan.scan.QUARANTINE", tmp_path), \
         mock.patch("music_scan.scan.PLAYLISTS", tmp_path), \
         mock.patch("music_scan.scan.ScanMetrics", FakeMetrics), \
         mock.patch("music_scan.scan.trigger_scan", side_effect=RuntimeError("connection refused")):
        with pytest.raises(RuntimeError, match="connection refused"):
            scan.run(pending=None)

    assert len(captured) == 1
    assert captured[0].success is False
    assert captured[0].failure_reason == "navidrome_trigger_failed"


def test_run_with_pending_none_skips_apply(tmp_path: Path) -> None:
    """run(pending=None) never calls apply_pending_removals."""
    from music_scan import scan

    with mock.patch("music_scan.scan.apply_pending_removals") as mock_apply, \
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


def test_run_pending_removals_failure_continues_to_import(tmp_path: Path) -> None:
    """A failure in apply_pending_removals is logged but does not suppress import."""
    from music_scan import scan
    from music_fetch.ingest import PendingRemovals

    pending = PendingRemovals(tracks=[], remove_sources=[])
    import_called = []

    def fake_import(*_args, **_kwargs):
        import_called.append(True)

    with mock.patch("music_scan.scan.apply_pending_removals", side_effect=RuntimeError("db error")), \
         mock.patch("music_scan.scan.clear_pending_removals") as mock_clear, \
         mock.patch("music_scan.scan.MusicLibrary", return_value=_make_mock_lib()), \
         mock.patch("music_scan.scan.run_beet_import", side_effect=fake_import), \
         mock.patch("music_scan.scan.run_beet_update"), \
         mock.patch("music_scan.scan._move_asis_eligible", return_value=0), \
         mock.patch("music_scan.scan.INBOX", tmp_path), \
         mock.patch("music_scan.scan.SPOTDL_DIR", tmp_path), \
         mock.patch("music_scan.scan.QUARANTINE", tmp_path), \
         mock.patch("music_scan.scan.PLAYLISTS", tmp_path), \
         mock.patch("music_scan.scan.ScanMetrics"), \
         mock.patch("music_scan.scan.trigger_scan"):
        scan.run(pending=pending)

    assert import_called, "import should proceed even when pending-removals raises"
    mock_clear.assert_not_called()  # the file stays for the next scan (#259)


def test_run_clears_the_removals_it_applied(tmp_path: Path) -> None:
    from music_fetch.ingest import PendingRemovals
    from music_scan import scan

    pending = PendingRemovals(tracks=[], remove_sources=[])
    with mock.patch("music_scan.scan.apply_pending_removals", return_value=0), \
         mock.patch("music_scan.scan.clear_pending_removals") as mock_clear, \
         mock.patch("music_scan.scan.MusicLibrary", return_value=_make_mock_lib()), \
         mock.patch("music_scan.scan.run_beet_import"), \
         mock.patch("music_scan.scan.run_beet_update"), \
         mock.patch("music_scan.scan._move_asis_eligible", return_value=0), \
         mock.patch("music_scan.scan.INBOX", tmp_path), \
         mock.patch("music_scan.scan.SPOTDL_DIR", tmp_path), \
         mock.patch("music_scan.scan.QUARANTINE", tmp_path), \
         mock.patch("music_scan.scan.PLAYLISTS", tmp_path), \
         mock.patch("music_scan.scan.ScanMetrics"), \
         mock.patch("music_scan.scan.trigger_scan"):
        scan.run(pending=pending)

    mock_clear.assert_called_once_with(pending)


def test_apply_pending_removals_skips_an_entry_that_raises(tmp_path: Path, caplog) -> None:
    """One bad entry is logged and skipped; the entries after it still apply (#259)."""
    from music_fetch.ingest import PendingRemovals, RemovedTrack
    from music_scan.scan import apply_pending_removals

    def clear(**kw):
        if kw["title"] == "Bad":
            raise ValueError("No closing quotation")
        return [{"title": kw["title"], "artist": kw["artist"]}]

    mock_lib = _make_mock_lib()
    mock_lib.clear_source_tag = mock.MagicMock(side_effect=clear)
    pending = PendingRemovals(
        tracks=[RemovedTrack("A", "X", "keep"), RemovedTrack("Bad", "X", "keep"), RemovedTrack("C", "X", "keep")],
        remove_sources=[],
    )
    with caplog.at_level("INFO", logger="music_scan.scan"):
        assert apply_pending_removals(pending, mock_lib) == 2
    assert mock_lib.clear_source_tag.call_count == 3
    assert "[SKIP] keep: removing Bad by X failed" in caplog.text


def test_run_asis_import_failure_continues_to_beet_update(tmp_path: Path) -> None:
    """A failure in import_asis_from_quarantine is logged but does not suppress beet update."""
    from music_scan import scan

    update_called = []

    with mock.patch("music_scan.scan.import_asis_from_quarantine", side_effect=RuntimeError("staging error")), \
         mock.patch("music_scan.scan.MusicLibrary", return_value=_make_mock_lib()), \
         mock.patch("music_scan.scan.run_beet_import"), \
         mock.patch("music_scan.scan.run_beet_update", side_effect=lambda: update_called.append(True)), \
         mock.patch("music_scan.scan.INBOX", tmp_path), \
         mock.patch("music_scan.scan.SPOTDL_DIR", tmp_path), \
         mock.patch("music_scan.scan.QUARANTINE", tmp_path), \
         mock.patch("music_scan.scan.PLAYLISTS", tmp_path), \
         mock.patch("music_scan.scan.ScanMetrics"), \
         mock.patch("music_scan.scan.trigger_scan"):
        scan.run(pending=None)

    assert update_called, "beet update should proceed even when asis-import raises"


def test_run_beet_update_failure_continues_to_regen_playlists(tmp_path: Path) -> None:
    """A failure in run_beet_update is logged but does not suppress playlist regeneration."""
    from music_scan import scan

    regen_called = []

    with mock.patch("music_scan.scan.run_beet_update", side_effect=RuntimeError("db gone")), \
         mock.patch("music_scan.scan.MusicLibrary", return_value=_make_mock_lib()), \
         mock.patch("music_scan.scan.run_beet_import"), \
         mock.patch("music_scan.scan.regen_playlists", side_effect=lambda *_: regen_called.append(True)), \
         mock.patch("music_scan.scan.import_asis_from_quarantine", return_value=0), \
         mock.patch("music_scan.scan.INBOX", tmp_path), \
         mock.patch("music_scan.scan.SPOTDL_DIR", tmp_path), \
         mock.patch("music_scan.scan.QUARANTINE", tmp_path), \
         mock.patch("music_scan.scan.PLAYLISTS", tmp_path), \
         mock.patch("music_scan.scan.ScanMetrics"), \
         mock.patch("music_scan.scan.trigger_scan"):
        scan.run(pending=None)

    assert regen_called, "regen_playlists should proceed even when beet update raises"


# ---------------------------------------------------------------------------
# regen_playlists
# ---------------------------------------------------------------------------


def _make_mock_item(title: str, artist: str, path: Path, **flex: str) -> mock.MagicMock:
    item = mock.MagicMock()
    item.title = title
    item.artist = artist
    item.albumartist = ""
    item.path = str(path).encode()
    item.get.side_effect = lambda k, d=None: flex.get(k, d)
    return item


def _write_spotdl(path: Path, songs: list[tuple[str, str]]) -> None:
    data = {
        "query": "https://open.spotify.com/playlist/test",
        "songs": [
            {"name": name, "artists": [artist], "url": f"url-{i}"}
            for i, (name, artist) in enumerate(songs)
        ],
    }
    path.write_text(json.dumps(data), encoding="utf-8")


def _regen_lib(spotdl_dir: Path, playlists_dir: Path, items: list) -> mock.MagicMock:
    mock_lib = mock.MagicMock()
    mock_lib.__enter__ = mock.MagicMock(return_value=mock_lib)
    mock_lib.__exit__ = mock.MagicMock(return_value=False)
    mock_lib.items_by_source = mock.MagicMock(return_value=items)
    return mock_lib


def test_regen_playlists_writes_m3u_with_relative_paths(tmp_path: Path) -> None:
    from music_scan.scan import regen_playlists

    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    # Empty songs list → falls back to alphabetical
    _write_spotdl(spotdl_dir / "my-playlist.spotdl", [])

    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()

    track_path = tmp_path / "library" / "Artist" / "Album" / "01 - Song.m4a"
    mock_lib = _regen_lib(spotdl_dir, playlists_dir, [_make_mock_item("Song", "Artist", track_path)])

    with mock.patch("music_scan.scan.SPOTDL_DIR", spotdl_dir), \
         mock.patch("music_scan.scan.PLAYLISTS", playlists_dir), \
         mock.patch("music_scan.scan.LIBRARY_DB", tmp_path / "library.db"), \
         mock.patch("music_scan.scan.MusicLibrary", return_value=mock_lib):
        regen_playlists()

    m3u = playlists_dir / "my-playlist.m3u"
    assert m3u.exists()
    content = m3u.read_text(encoding="utf-8")
    expected_rel = os.path.relpath(track_path, playlists_dir)
    assert content == expected_rel + "\n"
    mock_lib.items_by_source.assert_called_once_with("my-playlist")


def test_regen_playlists_empty_playlist_writes_empty_m3u(tmp_path: Path) -> None:
    from music_scan.scan import regen_playlists

    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    _write_spotdl(spotdl_dir / "empty-playlist.spotdl", [])

    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()

    mock_lib = _regen_lib(spotdl_dir, playlists_dir, [])

    with mock.patch("music_scan.scan.SPOTDL_DIR", spotdl_dir), \
         mock.patch("music_scan.scan.PLAYLISTS", playlists_dir), \
         mock.patch("music_scan.scan.LIBRARY_DB", tmp_path / "library.db"), \
         mock.patch("music_scan.scan.MusicLibrary", return_value=mock_lib):
        regen_playlists()

    m3u = playlists_dir / "empty-playlist.m3u"
    assert m3u.exists()
    assert m3u.read_text(encoding="utf-8") == ""


def test_regen_playlists_spotify_order(tmp_path: Path) -> None:
    """Tracks are emitted in .spotdl order when all library tracks match."""
    from music_scan.scan import regen_playlists

    lib_root = tmp_path / "library"
    path_a = lib_root / "Alpha.m4a"
    path_b = lib_root / "Bravo.m4a"
    path_c = lib_root / "Charlie.m4a"

    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    # Spotify order: C, A, B
    _write_spotdl(spotdl_dir / "pl.spotdl", [("Charlie", "Art"), ("Alpha", "Art"), ("Bravo", "Art")])

    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()

    items = [
        _make_mock_item("Alpha", "Art", path_a),
        _make_mock_item("Bravo", "Art", path_b),
        _make_mock_item("Charlie", "Art", path_c),
    ]
    mock_lib = _regen_lib(spotdl_dir, playlists_dir, items)

    with mock.patch("music_scan.scan.SPOTDL_DIR", spotdl_dir), \
         mock.patch("music_scan.scan.PLAYLISTS", playlists_dir), \
         mock.patch("music_scan.scan.LIBRARY_DB", tmp_path / "library.db"), \
         mock.patch("music_scan.scan.MusicLibrary", return_value=mock_lib):
        regen_playlists()

    lines = (playlists_dir / "pl.m3u").read_text(encoding="utf-8").splitlines()
    assert lines == [
        os.path.relpath(path_c, playlists_dir),
        os.path.relpath(path_a, playlists_dir),
        os.path.relpath(path_b, playlists_dir),
    ]


def test_regen_playlists_spotdl_missing_from_library_skipped(tmp_path: Path) -> None:
    """Tracks in .spotdl but absent from the library are silently skipped."""
    from music_scan.scan import regen_playlists

    lib_root = tmp_path / "library"
    path_a = lib_root / "Alpha.m4a"
    # Bravo is in .spotdl but NOT in the library

    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    _write_spotdl(spotdl_dir / "pl.spotdl", [("Alpha", "Art"), ("Bravo", "Art")])

    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()

    mock_lib = _regen_lib(spotdl_dir, playlists_dir, [_make_mock_item("Alpha", "Art", path_a)])

    with mock.patch("music_scan.scan.SPOTDL_DIR", spotdl_dir), \
         mock.patch("music_scan.scan.PLAYLISTS", playlists_dir), \
         mock.patch("music_scan.scan.LIBRARY_DB", tmp_path / "library.db"), \
         mock.patch("music_scan.scan.MusicLibrary", return_value=mock_lib):
        regen_playlists()

    lines = (playlists_dir / "pl.m3u").read_text(encoding="utf-8").splitlines()
    assert lines == [os.path.relpath(path_a, playlists_dir)]


def test_regen_playlists_library_extras_appended_alphabetically(tmp_path: Path) -> None:
    """Library tracks not in .spotdl are appended after ordered tracks, sorted."""
    from music_scan.scan import regen_playlists

    lib_root = tmp_path / "library"
    path_a = lib_root / "Alpha.m4a"
    path_b = lib_root / "Bravo.m4a"   # in .spotdl
    path_c = lib_root / "Charlie.m4a"  # NOT in .spotdl (legacy import)

    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    _write_spotdl(spotdl_dir / "pl.spotdl", [("Bravo", "Art"), ("Alpha", "Art")])

    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()

    items = [
        _make_mock_item("Alpha", "Art", path_a),
        _make_mock_item("Bravo", "Art", path_b),
        _make_mock_item("Charlie", "Legacy", path_c),
    ]
    mock_lib = _regen_lib(spotdl_dir, playlists_dir, items)

    with mock.patch("music_scan.scan.SPOTDL_DIR", spotdl_dir), \
         mock.patch("music_scan.scan.PLAYLISTS", playlists_dir), \
         mock.patch("music_scan.scan.LIBRARY_DB", tmp_path / "library.db"), \
         mock.patch("music_scan.scan.MusicLibrary", return_value=mock_lib):
        regen_playlists()

    lines = (playlists_dir / "pl.m3u").read_text(encoding="utf-8").splitlines()
    assert lines == [
        os.path.relpath(path_b, playlists_dir),  # ordered first
        os.path.relpath(path_a, playlists_dir),  # ordered second
        os.path.relpath(path_c, playlists_dir),  # unmatched, appended alphabetically
    ]


def test_regen_playlists_no_spotdl_songs_falls_back_to_alphabetical(tmp_path: Path) -> None:
    """Empty songs list in .spotdl yields alphabetical order (same as before)."""
    from music_scan.scan import regen_playlists

    lib_root = tmp_path / "library"
    path_b = lib_root / "B.m4a"
    path_a = lib_root / "A.m4a"

    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    _write_spotdl(spotdl_dir / "pl.spotdl", [])  # no songs

    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()

    items = [_make_mock_item("B", "Art", path_b), _make_mock_item("A", "Art", path_a)]
    mock_lib = _regen_lib(spotdl_dir, playlists_dir, items)

    with mock.patch("music_scan.scan.SPOTDL_DIR", spotdl_dir), \
         mock.patch("music_scan.scan.PLAYLISTS", playlists_dir), \
         mock.patch("music_scan.scan.LIBRARY_DB", tmp_path / "library.db"), \
         mock.patch("music_scan.scan.MusicLibrary", return_value=mock_lib):
        regen_playlists()

    lines = (playlists_dir / "pl.m3u").read_text(encoding="utf-8").splitlines()
    assert lines == [
        os.path.relpath(path_a, playlists_dir),
        os.path.relpath(path_b, playlists_dir),
    ]


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


@pytest.mark.parametrize("subtree", ["replaced", "usenet", "rejected", "something-new"])
def test_move_asis_eligible_skips_non_spotdl_subtrees(tmp_path: Path, subtree: str) -> None:
    """Only spotdl/ is allow-listed: replaced/ re-imported five wrong files (#202)."""
    from music_scan.scan import _move_asis_eligible

    quarantine, staging = tmp_path / "quarantine", tmp_path / "staging"
    f = quarantine / subtree / "1569-10 - Liberty Tree.m4a"
    f.parent.mkdir(parents=True)
    f.touch()

    with mock.patch("mutagen.File", return_value=_fake_tags()):
        count = _move_asis_eligible(quarantine, staging)

    assert count == 0
    assert f.exists()


def test_move_asis_eligible_moves_spotdl_playlist_file(tmp_path: Path) -> None:
    from music_scan.scan import _move_asis_eligible

    quarantine, staging = tmp_path / "quarantine", tmp_path / "staging"
    f = quarantine / "spotdl" / "keep" / "Artist - Song.m4a"
    f.parent.mkdir(parents=True)
    f.touch()

    with mock.patch("mutagen.File", return_value=_fake_tags()):
        count = _move_asis_eligible(quarantine, staging)

    assert count == 1
    assert (staging / "spotdl" / "keep" / "Artist - Song.m4a").exists()


def test_asis_import_skips_duplicates(tmp_path: Path) -> None:
    """The plugin's duplicate hook doesn't run under --asis, so the pass must
    not inherit duplicate_action: remove, which deletes the existing item (#202)."""
    import music_scan.scan as scan

    seen: dict = {}

    def fake_import(inbox_dir, asis=False, config=None, **_):
        seen["asis"], seen["config"] = asis, config.read_text()

    with mock.patch.object(scan, "_move_asis_eligible", return_value=1), \
         mock.patch.object(scan, "run_beet_import", side_effect=fake_import), \
         mock.patch.object(scan, "MusicLibrary", return_value=_make_mock_lib()):
        scan.import_asis_from_quarantine()

    assert seen["asis"] is True
    assert "duplicate_action: skip" in seen["config"]


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


def test_run_beet_import_config_overlay() -> None:
    from music_scan.process import run_beet_import

    mock_proc = mock.MagicMock()
    mock_proc.wait.return_value = 0
    with mock.patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
         mock.patch("music_scan.process.IMPORT_LOG") as mock_log:
        mock_log.exists.return_value = False
        run_beet_import(Path("/some/dir"), asis=True, config=Path("/tmp/c.yaml"))

    assert mock_popen.call_args[0][0][:4] == ["beet", "-c", "/tmp/c.yaml", "import"]


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




def test_missing_tracks_matches_like_regen_playlists() -> None:
    from music_scan.identity import ItemIndex
    from music_scan.scan import missing_tracks

    index = ItemIndex([_lib_item("Song One"), _lib_item("Song Two")])
    assert missing_tracks(index, [["Song One", "Artist"], ["Song Two", "Artist"]]) == []
    assert missing_tracks(index, [["Song One", "Artist"], ["Song Three", "Artist"]]) == [["Song Three", "Artist"]]


def test_missing_tracks_matches_a_retitled_item_by_isrc() -> None:
    from music_scan.identity import ItemIndex
    from music_scan.scan import missing_tracks

    index = ItemIndex([_lib_item("Song One (2011 Remaster)", isrc="GBUM71029604")])
    assert missing_tracks(index, [["Song One", "Artist", "sid1", "GBUM71029604", 1, 1]]) == []


def test_asis_skips_usenet_quarantine(tmp_path: Path) -> None:
    from music_scan.scan import _move_asis_eligible

    quarantine, staging = tmp_path / "q", tmp_path / "s"
    for rel in ("spotdl/later/a.m4a", "usenet/later/job/b.flac"):
        (quarantine / rel).parent.mkdir(parents=True)
        (quarantine / rel).write_bytes(b"x")
    tags = {"title": ["t"], "artist": ["a"], "album": ["al"], "tracknumber": ["1"]}
    with mock.patch("mutagen.File", return_value=tags):
        assert _move_asis_eligible(quarantine, staging) == 1
    assert (staging / "spotdl/later/a.m4a").exists()
    assert (quarantine / "usenet/later/job/b.flac").exists()


def test_add_source_tags_matching_items_once() -> None:
    from music_scan.scan import add_source

    def item(title, sources):
        it = mock.MagicMock(title=title, artist="Artist", albumartist="Artist")
        data = {"sources": sources}
        it.get.side_effect = lambda k, d=None: data.get(k, d)
        it.__setitem__.side_effect = lambda k, v: data.__setitem__(k, v)
        it.data = data
        return it

    one, two, other = item("One", "later"), item("Two", "later,keep"), item("Else", "later")
    lib = mock.MagicMock()
    lib.items_by_source.return_value = [one, two, other]
    assert add_source(lib, "later", "keep", [["One", "Artist"], ["Two", "Artist"]]) == 1
    assert one.data["sources"] == "later,keep"
    assert two.data["sources"] == "later,keep"
    assert other.data["sources"] == "later"


# ---------------------------------------------------------------------------
# count_lossless_items (FLAC guard)
# ---------------------------------------------------------------------------


def test_lossless_item_count_matches_codec_not_extension() -> None:
    from music_scan.library import MusicLibrary

    lib = MusicLibrary.__new__(MusicLibrary)
    lib._lib = mock.MagicMock()
    lib._lib.items.return_value = [
        mock.MagicMock(format="AAC"),
        mock.MagicMock(format="FLAC"),
        mock.MagicMock(format="ALAC"),  # lossless inside .m4a
        mock.MagicMock(format="MP3"),
        mock.MagicMock(format="WAVE"),
    ]
    assert lib.lossless_item_count() == 3


def test_count_lossless_items_returns_none_when_library_unreadable() -> None:
    from music_scan.scan import count_lossless_items

    with mock.patch("music_scan.scan.MusicLibrary", side_effect=OSError("locked")):
        assert count_lossless_items() is None


# ---------------------------------------------------------------------------
# tag_album_ids (#176)
# ---------------------------------------------------------------------------


def _lib_item(title, *, isrc="", spotify_ids="", via="usenet", added=100.0, disc=1, track=1, tracktotal=2,
              artist="Artist"):
    it = mock.MagicMock(title=title, artist=artist, albumartist="Artist", added=added,
                        disc=disc, track=track, tracktotal=tracktotal)
    data = {"isrc": isrc, "spotify_ids": spotify_ids, "via": via, "sources": "later"}
    it.get.side_effect = lambda k, d=None: data.get(k, d)
    it.__setitem__.side_effect = lambda k, v: data.__setitem__(k, v)
    it.data = data
    return it


def _tag(items, tracks, since=50.0, tracks_count=2):
    from music_scan.scan import tag_album_ids

    lib = mock.MagicMock()
    lib.items_by_source.return_value = items
    return tag_album_ids(lib, "later", tracks, since, tracks_count)


def test_tag_album_ids_prefers_isrc_over_track_number() -> None:
    # Retitled by MusicBrainz and at the other track number: only the ISRC links it.
    a = _lib_item("Uno", isrc="USX1;GBUM71029604", track=2)
    b = _lib_item("Dos", track=1)
    assert _tag([a, b], [["One", "Artist", "sid1", "GBUM71029604", 1, 1]]) == 1
    assert a.data["spotify_ids"] == "sid1"
    assert b.data["spotify_ids"] == ""


def test_tag_album_ids_falls_back_to_disc_and_track() -> None:
    a = _lib_item("Uno", track=1)
    b = _lib_item("Dos", track=2)
    assert _tag([a, b], [["One", "Artist", "sid1", None, 1, 1], ["Two", "Artist", "sid2", None, 1, 2]]) == 2
    assert (a.data["spotify_ids"], b.data["spotify_ids"]) == ("sid1", "sid2")


def test_tag_album_ids_track_number_needs_same_edition_and_this_import() -> None:
    deluxe = _lib_item("Uno", track=1, tracktotal=18)
    older = _lib_item("Dos", track=1, added=10.0)
    spotdl = _lib_item("Tres", track=1, via="spotdl")
    assert _tag([deluxe, older, spotdl], [["One", "Artist", "sid1", None, 1, 1]]) == 0


def test_tag_album_ids_words_fallback_is_logged(caplog) -> None:
    a = _lib_item("One", track=9, tracktotal=0)
    with caplog.at_level("INFO", logger="music_scan.scan"):
        assert _tag([a], [["One", "Artist", "sid1", None, 1, 1]]) == 1
    assert a.data["spotify_ids"] == "sid1"
    assert "[WORDS]" in caplog.text


def test_tag_album_ids_skips_tagged_and_idless_tracks() -> None:
    a = _lib_item("One", spotify_ids="sid1")
    b = _lib_item("Two", track=2)
    # sid1 already recorded; an old [name, artist] entry has no ID to record.
    assert _tag([a, b], [["One", "Artist", "sid1", None, 1, 1], ["Two", "Artist"]]) == 0
    a.store.assert_not_called()
    b.store.assert_not_called()


def test_tag_album_ids_reads_this_imports_feat_credits(caplog) -> None:
    """#240: CFCF's release credits `CFCF feat. …`, so the words rung missed 6 of 13."""
    feat = _lib_item("Kiss Me", artist="Artist feat. Guest", track=9, tracktotal=0)
    with caplog.at_level("INFO", logger="music_scan.scan"):
        assert _tag([feat], [["Kiss Me", "Artist", "sid1", None, 1, 1]]) == 1
    assert feat.data["spotify_ids"] == "sid1"
    assert "[WORDS]" in caplog.text


def test_tag_album_ids_release_rung_takes_each_item_once_and_only_this_import() -> None:
    first = _lib_item("The Real Her", artist="Artist feat. Guest", track=9, tracktotal=0)
    older = _lib_item("Bonus", artist="Artist feat. Guest", added=10.0, tracktotal=0)
    tracks = [["The Real Her", "Artist", "sid1", None, 1, 1], ["The Real Her", "Artist", "sid2", None, 2, 1],
              ["Bonus", "Artist", "sid3", None, 1, 3]]
    assert _tag([first, older], tracks) == 1
    assert first.data["spotify_ids"] == "sid1" and older.data["spotify_ids"] == ""



def test_tag_album_ids_never_gives_another_imports_item_the_id_by_words(caplog) -> None:
    """The live take already on the playlist must not take the studio entry's ID (#255)."""
    live = _lib_item("Girlfriend Is Better", added=10.0, tracktotal=16, track=14)
    with caplog.at_level("INFO", logger="music_scan.scan"):
        assert _tag([live], [["Girlfriend Is Better", "Artist", "studio", None, 1, 3]]) == 0
    assert live.data["spotify_ids"] == ""
    assert "[NOID]" in caplog.text

def test_add_source_matches_by_id_and_records_it() -> None:
    from music_scan.scan import add_source

    retitled = _lib_item("Uno", spotify_ids="sid1")
    lib = mock.MagicMock()
    lib.items_by_source.return_value = [retitled]
    assert add_source(lib, "later", "keep", [["One", "Artist", "sid1", None, 1, 1]]) == 1
    assert retitled.data["sources"] == "later,keep"


def test_regen_playlists_matches_by_id_before_words(tmp_path: Path, caplog) -> None:
    """Two releases of a title: each playlist entry gets the item it asked for,
    not the first one with the same words (#176)."""
    from music_scan.scan import regen_playlists

    lib_root = tmp_path / "library"
    studio, live, remaster = lib_root / "Studio.m4a", lib_root / "Live.m4a", lib_root / "Remaster.m4a"
    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    (spotdl_dir / "pl.spotdl").write_text(json.dumps({"songs": [
        {"name": "Song", "artists": ["Art"], "url": "https://open.spotify.com/track/LIVE"},
        {"name": "Song", "artists": ["Art"], "url": "u", "song_id": "X", "isrc": "GBREM0000001"},
        {"name": "Other", "artists": ["Art"], "url": "u2"},
    ]}), encoding="utf-8")
    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()
    items = [
        _make_mock_item("Song", "Art", studio, spotify_ids="STUDIO"),
        _make_mock_item("Song", "Art", live, spotify_url="https://open.spotify.com/track/LIVE"),
        _make_mock_item("Song - 2011 Remaster", "Art", remaster, isrc="USX;GBREM0000001"),
        _make_mock_item("Other", "Art", lib_root / "Other.m4a"),
    ]
    mock_lib = _regen_lib(spotdl_dir, playlists_dir, items)

    with mock.patch("music_scan.scan.SPOTDL_DIR", spotdl_dir), \
         mock.patch("music_scan.scan.PLAYLISTS", playlists_dir), \
         mock.patch("music_scan.scan.LIBRARY_DB", tmp_path / "library.db"), \
         mock.patch("music_scan.scan.MusicLibrary", return_value=mock_lib), \
         caplog.at_level("INFO", logger="music_scan.playlists"):
        regen_playlists()

    lines = (playlists_dir / "pl.m3u").read_text(encoding="utf-8").splitlines()
    rel = lambda p: os.path.relpath(p, playlists_dir)  # noqa: E731
    assert lines == [rel(live), rel(remaster), rel(lib_root / "Other.m4a"), rel(studio)]
    assert "pl: 1 of 3 track(s) matched by title+artist only" in caplog.text


# ---------------------------------------------------------------------------
# Library check before download (#187)
# ---------------------------------------------------------------------------


@pytest.fixture
def real_lib(tmp_path: Path):
    from music_scan.library import MusicLibrary

    with MusicLibrary(tmp_path / "library.db", tmp_path) as lib:
        yield lib


def _add(lib, **fields):
    from beets.library import Item

    item = Item(**{"title": "Song", "artist": "Artist", **fields})
    lib._lib.add(item)
    return item


def _reload(lib, item):
    return lib._lib.get_item(item.id)


def test_link_song_tags_the_item_matched_by_spotify_id(real_lib) -> None:
    from music_scan.identity import ItemIndex
    from music_scan.scan import link_song

    item = _add(real_lib, sources="liked", spotify_ids="A1")
    index = ItemIndex(real_lib.all_items())

    assert link_song(index, "later", {"url": "https://open.spotify.com/track/A1", "name": "Song", "artists": ["Artist"]})
    assert _reload(real_lib, item).get("sources") == "liked,later"
    assert _reload(real_lib, item).get("spotify_ids") == "A1"


def test_link_song_tags_the_item_matched_by_isrc(real_lib) -> None:
    from music_scan.identity import ItemIndex
    from music_scan.scan import link_song

    item = _add(real_lib, sources="", via="usenet", isrc="GBX1;GBX2")
    index = ItemIndex(real_lib.all_items())

    song = {"url": "https://open.spotify.com/track/B2", "song_id": "B2", "isrc": "GBX2", "name": "Other title"}
    assert link_song(index, "later", song)
    assert _reload(real_lib, item).get("sources") == "later"
    assert _reload(real_lib, item).get("spotify_ids") == "B2"


def test_link_song_never_skips_on_title_and_artist_alone(real_lib) -> None:
    """A wrong skip loses the track silently; a redundant download is merged by the hook."""
    from music_scan.identity import ItemIndex
    from music_scan.scan import link_song

    item = _add(real_lib, sources="liked", spotify_ids="LIVE")
    index = ItemIndex(real_lib.all_items())

    song = {"url": "https://open.spotify.com/track/STUDIO", "name": "Song", "artists": ["Artist"]}
    assert not link_song(index, "later", song)
    assert _reload(real_lib, item).get("sources") == "liked"


def test_have_or_link_counts_an_album_held_under_another_playlist(real_lib) -> None:
    from music_scan.identity import ItemIndex
    from music_scan.scan import have_or_link

    one = _add(real_lib, sources="liked", spotify_ids="T1")
    two = _add(real_lib, title="Two", sources="", isrc="ISRC2")
    tracks = [["Song", "Artist", "T1", None, 1, 1], ["Two", "Artist", "T2", "ISRC2", 1, 2]]

    assert have_or_link(ItemIndex(real_lib.items_by_source("later")), ItemIndex(real_lib.all_items()), "later", tracks)
    assert _reload(real_lib, one).get("sources") == "liked,later"
    assert _reload(real_lib, two).get("sources") == "later"
    assert _reload(real_lib, two).get("spotify_ids") == "T2"


def test_have_or_link_tags_nothing_when_a_track_is_missing(real_lib) -> None:
    from music_scan.identity import ItemIndex
    from music_scan.scan import have_or_link

    one = _add(real_lib, sources="liked", spotify_ids="T1")
    _add(real_lib, title="Two", sources="liked")  # title+artist only: not enough outside the playlist
    tracks = [["Song", "Artist", "T1", None, 1, 1], ["Two", "Artist", "T2", "ISRC2", 1, 2]]

    assert not have_or_link(ItemIndex(real_lib.items_by_source("later")), ItemIndex(real_lib.all_items()), "later", tracks)
    assert _reload(real_lib, one).get("sources") == "liked"


def test_missing_tracks_counts_other_playlists_by_id_only_and_tags_nothing(real_lib) -> None:
    """The fallback's view (#205): like have_or_link, per track, without tagging."""
    from music_scan.identity import ItemIndex
    from music_scan.scan import missing_tracks

    one = _add(real_lib, sources="liked", spotify_ids="T1")
    _add(real_lib, title="Two", sources="liked")  # title+artist only: not enough outside the playlist
    tracks = [["Song", "Artist", "T1", None, 1, 1], ["Two", "Artist", "T2", "ISRC2", 1, 2]]

    gaps = missing_tracks(ItemIndex(real_lib.items_by_source("later")), tracks, ItemIndex(real_lib.all_items()))
    assert gaps == [tracks[1]]
    assert _reload(real_lib, one).get("sources") == "liked"


def test_have_or_link_keeps_the_full_ladder_inside_the_playlist(real_lib) -> None:
    """Within the playlist, title+artist still counts."""
    from music_scan.identity import ItemIndex
    from music_scan.scan import have_or_link

    _add(real_lib, sources="later")
    tracks = [["Song", "Artist", "T1", None, 1, 1]]

    assert have_or_link(ItemIndex(real_lib.items_by_source("later")), ItemIndex(real_lib.all_items()), "later", tracks)


# ---------------------------------------------------------------------------
# Playlist slots (#228)
# ---------------------------------------------------------------------------


def _slot_regen(tmp_path: Path, songs: list[dict], source_items: list, all_items: list, metrics=None):
    """Run regen_playlists for one playlist ``pl`` over mocked source and library items."""
    from music_scan.scan import regen_playlists

    spotdl_dir, playlists_dir = tmp_path / "spotdl", tmp_path / "playlists"
    spotdl_dir.mkdir()
    playlists_dir.mkdir()
    (spotdl_dir / "pl.spotdl").write_text(json.dumps({"songs": songs}), encoding="utf-8")
    mock_lib = _regen_lib(spotdl_dir, playlists_dir, source_items)
    mock_lib.all_items.return_value = all_items
    with mock.patch("music_scan.scan.SPOTDL_DIR", spotdl_dir), \
         mock.patch("music_scan.scan.PLAYLISTS", playlists_dir), \
         mock.patch("music_scan.scan.LIBRARY_DB", tmp_path / "library.db"), \
         mock.patch("music_scan.scan.MusicLibrary", return_value=mock_lib):
        counts = regen_playlists(metrics)
    lines = (playlists_dir / "pl.m3u").read_text(encoding="utf-8").splitlines()
    return counts, lines, mock_lib


def test_regen_playlists_fills_a_slot_from_the_whole_library(tmp_path: Path, caplog) -> None:
    """An entry whose track is in the library under another playlist (or from
    Usenet, or a manual import) is listed, in playlist order, without a tag write."""
    from music_scan.metrics import ScanMetrics

    lib_root = tmp_path / "library"
    tagged = _make_mock_item("Tagged", "Art", lib_root / "tagged.m4a", spotify_ids="T1", sources="pl")
    elsewhere = _make_mock_item("Elsewhere", "Art", lib_root / "elsewhere.m4a", spotify_ids="E1", sources="keep")
    by_isrc = _make_mock_item("Remaster", "Art", lib_root / "remaster.m4a", isrc="GBX1", sources="")
    songs = [
        {"name": "Elsewhere", "artists": ["Art"], "url": "u1", "song_id": "E1"},
        {"name": "Tagged", "artists": ["Art"], "url": "u2", "song_id": "T1"},
        {"name": "Original", "artists": ["Art"], "url": "u3", "song_id": "O1", "isrc": "GBX1"},
        {"name": "Nowhere", "artists": ["Art"], "url": "u4", "song_id": "N1"},
    ]
    metrics = ScanMetrics()
    with caplog.at_level(logging.INFO, logger="music_scan.playlists"):
        counts, lines, lib = _slot_regen(tmp_path, songs, [tagged], [tagged, elsewhere, by_isrc], metrics)

    assert lines == ["../library/elsewhere.m4a", "../library/tagged.m4a", "../library/remaster.m4a"]
    assert counts == {"pl": 3}
    assert metrics.slots_empty == {"pl": 1}
    assert "[SLOT] pl: 2 entr(ies) filled from the library by ID/ISRC" in caplog.text
    for item in (tagged, elsewhere, by_isrc):
        item.__setitem__.assert_not_called()
        item.store.assert_not_called()


def test_regen_playlists_keeps_every_entry_todays_path_shows(tmp_path: Path) -> None:
    """Additive: an entry matched only by sources + title/artist words stays,
    even when another library item carries its exact Spotify ID."""
    lib_root = tmp_path / "library"
    words = _make_mock_item("Song", "Art", lib_root / "words.m4a", sources="pl")
    twin = _make_mock_item("Song", "Art", lib_root / "twin.m4a", spotify_ids="S1", sources="keep")
    tail = _make_mock_item("Untracked", "Art", lib_root / "a-tail.m4a", sources="pl")
    songs = [{"name": "Song", "artists": ["Art"], "url": "u", "song_id": "S1"}]

    _, lines, _ = _slot_regen(tmp_path, songs, [words, tail], [words, twin, tail])

    assert lines == ["../library/words.m4a", "../library/a-tail.m4a"]


def test_regen_playlists_without_metrics_leaves_them_alone(tmp_path: Path) -> None:
    counts, lines, _ = _slot_regen(tmp_path, [], [], [])
    assert (counts, lines) == ({"pl": 0}, [])


def test_missing_tracks_and_have_or_link_read_resolved_slots(real_lib) -> None:
    """The album hooks' view is the same resolution as the .m3u (#205, #228)."""
    from music_scan.identity import ItemIndex
    from music_scan.scan import have_or_link, missing_tracks

    _add(real_lib, title="One", sources="later", spotify_ids="T1")
    two = _add(real_lib, title="Two", sources="keep", isrc="ISRC2")
    tracks = [["One", "Artist", "T1", None, 1, 1], ["Two", "Artist", "T2", "ISRC2", 1, 2], ["Three", "Artist", "T3", None, 1, 3]]
    source, library = ItemIndex(real_lib.items_by_source("later")), ItemIndex(real_lib.all_items())

    assert missing_tracks(source, tracks, library) == [tracks[2]]
    assert missing_tracks(source, tracks) == tracks[1:]
    assert not have_or_link(source, library, "later", tracks)
    assert _reload(real_lib, two).get("sources") == "keep"  # nothing tagged while a track is missing
    assert have_or_link(source, library, "later", tracks[:2])
    assert _reload(real_lib, two).get("sources") == "keep,later"
