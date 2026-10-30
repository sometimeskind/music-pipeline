"""Tests for pipeline.scan — pure-Python logic."""

import logging
import os
from pathlib import Path
import unittest.mock as mock

import pytest

from music_fetch import store


@pytest.fixture(autouse=True)
def _store_dir(tmp_path: Path, monkeypatch):
    """Every test gets its own, empty playlist store directory (homelab#1914)."""
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "store")


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
# run
# ---------------------------------------------------------------------------


def _make_mock_lib() -> mock.MagicMock:
    mock_lib = mock.MagicMock()
    mock_lib.__enter__ = mock.MagicMock(return_value=mock_lib)
    mock_lib.__exit__ = mock.MagicMock(return_value=False)
    mock_lib.items_by_source = mock.MagicMock(return_value=[])
    mock_lib.items_added_since = mock.MagicMock(return_value=[])
    mock_lib.item_count = mock.MagicMock(return_value=0)
    return mock_lib


def test_run_trigger_scan_failure_sets_success_false(tmp_path: Path) -> None:
    """When trigger_scan raises, last_run_success=0 is pushed and the exception propagates."""
    from music_scan import scan

    captured: list = []

    class FakeMetrics:
        def __init__(self):
            self.success = True
            self.failure_reason = ""
            self.tracks_imported = 0
            self.quarantined_tracks = 0
            self.duration_seconds = 0
            captured.append(self)

        def push(self):
            pass

    with mock.patch("music_scan.scan.MusicLibrary", return_value=_make_mock_lib()), \
         mock.patch("music_scan.scan.run_beet_import", return_value=0), \
         mock.patch("music_scan.scan.run_beet_update"), \
         mock.patch("music_scan.scan._move_asis_eligible", return_value=0), \
         mock.patch("music_scan.scan.INBOX", tmp_path), \
         mock.patch("music_scan.scan.SPOTDL_DIR", tmp_path), \
         mock.patch("music_scan.scan.QUARANTINE", tmp_path), \
         mock.patch("music_scan.scan.PLAYLISTS", tmp_path), \
         mock.patch("music_scan.scan.ScanMetrics", FakeMetrics), \
         mock.patch("music_scan.scan.trigger_scan", side_effect=RuntimeError("connection refused")):
        with pytest.raises(RuntimeError, match="connection refused"):
            scan.run()

    assert len(captured) == 1
    assert captured[0].success is False
    assert captured[0].failure_reason == "navidrome_trigger_failed"


def test_run_asis_import_failure_continues_to_beet_update(tmp_path: Path) -> None:
    """A failure in import_asis_from_quarantine is logged but does not suppress beet update."""
    from music_scan import scan

    update_called = []

    with mock.patch("music_scan.scan.import_asis_from_quarantine", side_effect=RuntimeError("staging error")), \
         mock.patch("music_scan.scan.MusicLibrary", return_value=_make_mock_lib()), \
         mock.patch("music_scan.scan.run_beet_import", return_value=0), \
         mock.patch("music_scan.scan.run_beet_update", side_effect=lambda: update_called.append(True)), \
         mock.patch("music_scan.scan.INBOX", tmp_path), \
         mock.patch("music_scan.scan.SPOTDL_DIR", tmp_path), \
         mock.patch("music_scan.scan.QUARANTINE", tmp_path), \
         mock.patch("music_scan.scan.PLAYLISTS", tmp_path), \
         mock.patch("music_scan.scan.ScanMetrics"), \
         mock.patch("music_scan.scan.trigger_scan"):
        scan.run()

    assert update_called, "beet update should proceed even when asis-import raises"


def test_run_beet_update_failure_continues_to_regen_playlists(tmp_path: Path) -> None:
    """A failure in run_beet_update is logged but does not suppress playlist regeneration."""
    from music_scan import scan

    regen_called = []

    with mock.patch("music_scan.scan.run_beet_update", side_effect=RuntimeError("db gone")), \
         mock.patch("music_scan.scan.MusicLibrary", return_value=_make_mock_lib()), \
         mock.patch("music_scan.scan.run_beet_import", return_value=0), \
         mock.patch("music_scan.scan.regen_playlists", side_effect=lambda *_: regen_called.append(True)), \
         mock.patch("music_scan.scan.import_asis_from_quarantine", return_value=0), \
         mock.patch("music_scan.scan.INBOX", tmp_path), \
         mock.patch("music_scan.scan.SPOTDL_DIR", tmp_path), \
         mock.patch("music_scan.scan.QUARANTINE", tmp_path), \
         mock.patch("music_scan.scan.PLAYLISTS", tmp_path), \
         mock.patch("music_scan.scan.ScanMetrics"), \
         mock.patch("music_scan.scan.trigger_scan"):
        scan.run()

    assert regen_called, "regen_playlists should proceed even when beet update raises"


# ---------------------------------------------------------------------------
# regen_playlists: each .m3u is its playlist store's view (homelab#1914)
# ---------------------------------------------------------------------------


def _make_mock_item(title: str, artist: str, path: Path, **flex: str) -> mock.MagicMock:
    item = mock.MagicMock()
    item.title = title
    item.artist = artist
    item.albumartist = ""
    item.path = str(path).encode()
    item.get.side_effect = lambda k, d=None: flex.get(k, d)
    return item


def _sid(name: str) -> str:
    return f"ID-{name}"


def _write_store(name: str, songs: list[tuple[str, str]]) -> None:
    """A store of *songs* (title, artist), each with Spotify ID ``ID-<title>``."""
    store.Store.from_songs(name, [
        {"name": title, "artists": [artist], "url": f"https://open.spotify.com/track/{_sid(title)}",
         "song_id": _sid(title)}
        for title, artist in songs
    ]).save(store.path(name))


def _regen_lib(items: list) -> mock.MagicMock:
    mock_lib = mock.MagicMock()
    mock_lib.__enter__ = mock.MagicMock(return_value=mock_lib)
    mock_lib.__exit__ = mock.MagicMock(return_value=False)
    mock_lib.all_items = mock.MagicMock(return_value=items)
    mock_lib.items_by_source = mock.MagicMock(side_effect=AssertionError("regen never reads sources"))
    return mock_lib


def _run_regen(tmp_path: Path, items: list, metrics=None) -> tuple[dict, Path, mock.MagicMock]:
    from music_scan.scan import regen_playlists

    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir(exist_ok=True)
    mock_lib = _regen_lib(items)
    with mock.patch("music_scan.scan.SPOTDL_DIR", tmp_path / "spotdl"), \
         mock.patch("music_scan.scan.PLAYLISTS", playlists_dir), \
         mock.patch("music_scan.scan.LIBRARY_DB", tmp_path / "library.db"), \
         mock.patch("music_scan.scan.MusicLibrary", return_value=mock_lib):
        counts = regen_playlists(metrics)
    return counts, playlists_dir, mock_lib


def test_regen_playlists_writes_m3u_with_relative_paths(tmp_path: Path) -> None:
    track_path = tmp_path / "library" / "Artist" / "Album" / "01 - Song.m4a"
    _write_store("my-playlist", [("Song", "Artist")])

    counts, playlists_dir, _ = _run_regen(
        tmp_path, [_make_mock_item("Song", "Artist", track_path, spotify_ids=_sid("Song"))])

    m3u = playlists_dir / "my-playlist.m3u"
    assert m3u.read_text(encoding="utf-8") == os.path.relpath(track_path, playlists_dir) + "\n"
    assert counts == {"my-playlist": 1}


def test_regen_playlists_empty_store_writes_empty_m3u(tmp_path: Path) -> None:
    _write_store("empty-playlist", [])

    counts, playlists_dir, _ = _run_regen(tmp_path, [])

    assert (playlists_dir / "empty-playlist.m3u").read_text(encoding="utf-8") == ""
    assert counts == {"empty-playlist": 0}


def test_regen_playlists_spotify_order(tmp_path: Path) -> None:
    """Tracks are emitted in store (Spotify) order."""
    lib_root = tmp_path / "library"
    path_a, path_b, path_c = lib_root / "Alpha.m4a", lib_root / "Bravo.m4a", lib_root / "Charlie.m4a"
    _write_store("pl", [("Charlie", "Art"), ("Alpha", "Art"), ("Bravo", "Art")])
    items = [
        _make_mock_item("Alpha", "Art", path_a, spotify_ids=_sid("Alpha")),
        _make_mock_item("Bravo", "Art", path_b, spotify_ids=_sid("Bravo")),
        _make_mock_item("Charlie", "Art", path_c, spotify_ids=_sid("Charlie")),
    ]

    _, playlists_dir, _ = _run_regen(tmp_path, items)

    lines = (playlists_dir / "pl.m3u").read_text(encoding="utf-8").splitlines()
    assert lines == [
        os.path.relpath(path_c, playlists_dir),
        os.path.relpath(path_a, playlists_dir),
        os.path.relpath(path_b, playlists_dir),
    ]


def test_regen_playlists_store_entry_missing_from_library_skipped(tmp_path: Path) -> None:
    """Entries no library item holds are left out (an empty slot)."""
    from music_scan.metrics import ScanMetrics

    path_a = tmp_path / "library" / "Alpha.m4a"
    _write_store("pl", [("Alpha", "Art"), ("Bravo", "Art")])  # Bravo is NOT in the library
    metrics = ScanMetrics()

    _, playlists_dir, _ = _run_regen(tmp_path, [_make_mock_item("Alpha", "Art", path_a, spotify_ids=_sid("Alpha"))],
                                     metrics)

    assert (playlists_dir / "pl.m3u").read_text(encoding="utf-8").splitlines() == [
        os.path.relpath(path_a, playlists_dir)]
    assert metrics.slots_empty == {"pl": 1}


def test_regen_playlists_never_lists_a_tagged_item_no_entry_holds(tmp_path: Path) -> None:
    """homelab#1914 stage c: ``sources`` is provenance only.  A legacy import
    tagged with the playlist, and a title+artist match without an ID, are not
    on it, and the scan writes no local entry for them."""
    lib_root = tmp_path / "library"
    path_b, path_c, path_w = lib_root / "Bravo.m4a", lib_root / "Charlie.m4a", lib_root / "Words.m4a"
    _write_store("pl", [("Bravo", "Art"), ("Words", "Art")])
    before = store.load("pl").entries
    items = [
        _make_mock_item("Bravo", "Art", path_b, spotify_ids=_sid("Bravo"), sources="pl"),
        _make_mock_item("Charlie", "Legacy", path_c, sources="pl"),
        _make_mock_item("Words", "Art", path_w, sources="pl"),
    ]

    counts, playlists_dir, _ = _run_regen(tmp_path, items)

    assert (playlists_dir / "pl.m3u").read_text(encoding="utf-8").splitlines() == [
        os.path.relpath(path_b, playlists_dir)]
    assert counts == {"pl": 1}
    assert store.load("pl").locals == []
    assert store.load("pl").entries == before


def test_regen_playlists_regenerates_every_store(tmp_path: Path) -> None:
    lib_root = tmp_path / "library"
    _write_store("one", [("Alpha", "Art")])
    _write_store("two", [("Bravo", "Art"), ("Alpha", "Art")])
    items = [_make_mock_item("Alpha", "Art", lib_root / "Alpha.m4a", spotify_ids=_sid("Alpha")),
             _make_mock_item("Bravo", "Art", lib_root / "Bravo.m4a", spotify_ids=_sid("Bravo"))]

    counts, playlists_dir, _ = _run_regen(tmp_path, items)

    assert counts == {"one": 1, "two": 2}
    assert (playlists_dir / "two.m3u").read_text(encoding="utf-8").splitlines() == [
        "../library/Bravo.m4a", "../library/Alpha.m4a"]


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


def test_run_beet_update_passes_mtime_with_the_default_fields() -> None:
    """Plain ``beet update`` never stores mtime, so it re-reads touched files every scan (#340)."""
    from beets.library import Item
    from music_scan.process import run_beet_update

    with mock.patch("subprocess.run", return_value=mock.MagicMock(returncode=0)) as run:
        run_beet_update()

    cmd = run.call_args[0][0]
    fields = set(cmd[3::2])
    assert cmd[:2] == ["beet", "update"]
    assert set(cmd[2::2]) == {"-F"}
    assert "mtime" in fields
    assert Item._media_fields <= fields


def test_update_fields_stop_the_empty_composer_diff_repeating(tmp_path: Path, capsys) -> None:
    """An empty ©wrt atom reads as [''] against the database's []: with mtime stored
    the diff prints once, then the file is skipped until it changes again (#340)."""
    import subprocess
    import shutil

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    from beets.library import Item, Library
    from beets.ui.commands.update import update_items
    from mutagen.mp4 import MP4
    from music_scan.process import update_fields

    path = tmp_path / "a.m4a"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:a", "aac", str(path)], check=True)
    audio = MP4(path)
    audio["\xa9nam"], audio["\xa9wrt"] = ["One"], [""]
    audio.save()
    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path))
    lib.add(Item.from_path(str(path)))
    os.utime(path, (os.path.getmtime(path) + 10,) * 2)  # touched outside beets

    outputs = []
    for _ in range(2):
        update_items(lib, list(lib.items()), move=False, pretend=False, fields=update_fields())
        outputs.append(capsys.readouterr().out)

    assert "composers" in outputs[0]
    assert outputs[1] == ""


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


def test_tag_album_ids_falls_back_to_disc_and_track_when_the_title_agrees() -> None:
    # Edition notes aside: "One (2011 Remaster)" is Spotify's "One - Remastered 2011".
    a = _lib_item("One (2011 Remaster)", track=1, artist="Someone Else")
    b = _lib_item("Two [Remastered]", track=2, artist="Someone Else")
    tracks = [["One - Remastered 2011", "Artist", "sid1", None, 1, 1], ["Two", "Artist", "sid2", None, 1, 2]]
    assert _tag([a, b], tracks) == 2
    assert (a.data["spotify_ids"], b.data["spotify_ids"]) == ("sid1", "sid2")
    assert "words_ids" not in a.data


def test_tag_album_ids_a_number_alone_is_not_identity(caplog) -> None:
    """#278: the item at Spotify's slot has another title, so it gets no ID by number."""
    a = _lib_item("Uno", track=1)
    with caplog.at_level("INFO", logger="music_scan.scan"):
        assert _tag([a], [["One", "Artist", "sid1", None, 1, 1]]) == 0
    assert a.data["spotify_ids"] == ""
    assert "[NUMBER]" in caplog.text


def test_tag_album_ids_track_number_needs_same_edition_and_this_import() -> None:
    deluxe = _lib_item("Uno", track=1, tracktotal=18)
    older = _lib_item("Dos", track=1, added=10.0)
    spotdl = _lib_item("Tres", track=1, via="spotdl")
    assert _tag([deluxe, older, spotdl], [["One", "Artist", "sid1", None, 1, 1]]) == 0


def test_tag_album_ids_skips_tagged_and_idless_tracks() -> None:
    a = _lib_item("One", spotify_ids="sid1")
    b = _lib_item("Two", track=2)
    # sid1 already recorded; an old [name, artist] entry has no ID to record.
    assert _tag([a, b], [["One", "Artist", "sid1", None, 1, 1], ["Two", "Artist"]]) == 0
    a.store.assert_not_called()
    b.store.assert_not_called()


def test_add_source_matches_by_id_and_records_it() -> None:
    from music_scan.scan import add_source

    retitled = _lib_item("Uno", spotify_ids="sid1")
    lib = mock.MagicMock()
    lib.items_by_source.return_value = [retitled]
    assert add_source(lib, "later", "keep", [["One", "Artist", "sid1", None, 1, 1]]) == 1
    assert retitled.data["sources"] == "later,keep"


def test_regen_playlists_matches_by_id_before_words(tmp_path: Path, caplog) -> None:
    """Two releases of a title: each store entry gets the item it asked for, by
    Spotify ID (URL or song_id) or ISRC, not the first one with the same words
    (#176).  An entry no item holds by ID or ISRC stays empty: words never fill."""
    lib_root = tmp_path / "library"
    studio, live, remaster = lib_root / "Studio.m4a", lib_root / "Live.m4a", lib_root / "Remaster.m4a"
    store.Store.from_songs("pl", [
        {"name": "Song", "artists": ["Art"], "url": "https://open.spotify.com/track/LIVE"},
        {"name": "Song", "artists": ["Art"], "url": "u", "song_id": "X", "isrc": "GBREM0000001"},
        {"name": "Other", "artists": ["Art"], "url": "u2"},
    ]).save(store.path("pl"))
    items = [
        _make_mock_item("Song", "Art", studio, spotify_ids="STUDIO"),
        _make_mock_item("Song", "Art", live, spotify_url="https://open.spotify.com/track/LIVE"),
        _make_mock_item("Song - 2011 Remaster", "Art", remaster, isrc="USX;GBREM0000001"),
        _make_mock_item("Other", "Art", lib_root / "Other.m4a"),
    ]

    with caplog.at_level("INFO", logger="music_scan.playlists"):
        _, playlists_dir, _ = _run_regen(tmp_path, items)

    lines = (playlists_dir / "pl.m3u").read_text(encoding="utf-8").splitlines()
    rel = lambda p: os.path.relpath(p, playlists_dir)  # noqa: E731
    assert lines == [rel(live), rel(remaster)]
    assert "[SLOT] pl: 2 entr(ies) filled from the library by ID/ISRC" in caplog.text
    assert "matched by title+artist only" not in caplog.text


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


def _slot_regen(tmp_path: Path, songs: list[dict], all_items: list, metrics=None):
    """Run regen_playlists for one playlist ``pl`` whose store holds *songs*."""
    store.Store.from_songs("pl", songs).save(store.path("pl"))
    counts, playlists_dir, mock_lib = _run_regen(tmp_path, all_items, metrics)
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
        counts, lines, _ = _slot_regen(tmp_path, songs, [tagged, elsewhere, by_isrc], metrics)

    assert lines == ["../library/elsewhere.m4a", "../library/tagged.m4a", "../library/remaster.m4a"]
    assert counts == {"pl": 3}
    assert metrics.slots_empty == {"pl": 1}
    # The store view resolves every entry against the whole library.
    assert "[SLOT] pl: 3 entr(ies) filled from the library by ID/ISRC" in caplog.text
    for item in (tagged, elsewhere, by_isrc):
        item.__setitem__.assert_not_called()
        item.store.assert_not_called()


def test_regen_playlists_lists_the_item_with_the_entrys_id_not_a_words_match(tmp_path: Path) -> None:
    """An item tagged with the playlist that matches an entry by title+artist
    only is not on it; the item carrying the entry's Spotify ID is."""
    lib_root = tmp_path / "library"
    words = _make_mock_item("Song", "Art", lib_root / "words.m4a", sources="pl")
    twin = _make_mock_item("Song", "Art", lib_root / "twin.m4a", spotify_ids="S1", sources="keep")
    tail = _make_mock_item("Untracked", "Art", lib_root / "a-tail.m4a", sources="pl")
    songs = [{"name": "Song", "artists": ["Art"], "url": "u", "song_id": "S1"}]

    _, lines, _ = _slot_regen(tmp_path, songs, [words, twin, tail])

    assert lines == ["../library/twin.m4a"]


def test_regen_playlists_without_metrics_leaves_them_alone(tmp_path: Path) -> None:
    counts, lines, _ = _slot_regen(tmp_path, [], [])
    assert (counts, lines) == ({"pl": 0}, [])


def test_sync_hooks_hold_quarantine_and_inbox_downloads(tmp_path: Path) -> None:
    """The nightly's hooks: ``held`` is the spotdl URLs of the downloads in
    quarantine and in the inbox; ``resolves`` matches by ID/ISRC without
    tagging; ``in_library`` tags the item with the playlist (#187)."""
    from music_scan import held, scan

    quarantine, inbox = tmp_path / "quarantine", tmp_path / "inbox" / "spotdl"
    urls = {}
    for root, rel, url in ((quarantine, "rejected/spotdl/pl/q.m4a", "https://open.spotify.com/track/Q"),
                           (inbox, "pl/i.m4a", "https://open.spotify.com/track/I"),
                           (inbox, "pl/plain.m4a", None)):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(b"")
        urls[root / rel] = url
    have = _make_mock_item("Have", "Art", tmp_path / "have.m4a", spotify_ids="H1", sources="")
    have.data = {"sources": "", "spotify_ids": "H1"}
    have.get.side_effect = lambda k, d=None: have.data.get(k, d)
    have.__setitem__.side_effect = lambda k, v: have.data.__setitem__(k, v)

    with mock.patch.object(held, "_read_spotify_url", side_effect=lambda p: urls[p]), \
         mock.patch.object(scan, "QUARANTINE", quarantine), mock.patch.object(scan, "SPOTDL_DIR", inbox), \
         mock.patch("music_scan.scan.MusicLibrary", return_value=_regen_lib([have])):
        with scan.sync_hooks() as hooks:
            assert hooks["held"] == {"https://open.spotify.com/track/Q", "https://open.spotify.com/track/I"}
            song = {"name": "Have", "artists": ["Art"], "url": "https://open.spotify.com/track/H1"}
            assert hooks["resolves"](song)
            assert not hooks["resolves"]({"name": "Have", "artists": ["Art"], "url": "https://open.spotify.com/track/N"})
            have.store.assert_not_called()
            assert hooks["in_library"]("later", song)
            assert have.data["sources"] == "later"
            have.store.assert_called_once()


def test_sync_hooks_default_held_dirs_are_quarantine_and_the_spotdl_inbox() -> None:
    from music_scan import held, scan

    assert held.QUARANTINE_DIR == scan.QUARANTINE
    assert held.SPOTDL_DIR == scan.SPOTDL_DIR


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


# beets' stderr during the Yeezus import (#264), as Loki has it.
MB_503_LINES = [
    "musicbrainz: Error searching MusicBrainz: HTTPSConnectionPool(host='musicbrainz.org', port=443): Max retries "
    "exceeded with url: /ws/2/recording?limit=5&query=artist%3A%28kanye+west%29&fmt=json "
    "(Caused by ResponseError('too many 503 error responses'))",
    "urllib3.exceptions.MaxRetryError: HTTPSConnectionPool(host='musicbrainz.org', port=443): Max retries exceeded "
    "with url: /ws/2/recording?limit=5&fmt=json (Caused by ResponseError('too many 503 error responses'))",
    "requests.exceptions.RetryError: HTTPSConnectionPool(host='musicbrainz.org', port=443): Max retries exceeded "
    "with url: /ws/2/recording?limit=5&fmt=json (Caused by ResponseError('too many 503 error responses'))",
]


def test_musicbrainz_unavailable_matches_the_yeezus_lines() -> None:
    from music_scan.process import musicbrainz_unavailable

    assert all(musicbrainz_unavailable(line) for line in MB_503_LINES)
    assert not musicbrainz_unavailable("urllib3.exceptions.ResponseError: too many 503 error responses")
    assert not musicbrainz_unavailable(
        "HTTPSConnectionPool(host='api.acoustid.org', port=443): Max retries exceeded with url: /v2/lookup")
    assert not musicbrainz_unavailable("Skipping.")


def test_run_beet_import_counts_musicbrainz_errors() -> None:
    from music_scan.process import run_beet_import

    mock_proc = mock.MagicMock()
    mock_proc.wait.return_value = 0
    mock_proc.stderr = [line.encode() + b"\n" for line in ["Traceback (most recent call last):", *MB_503_LINES]]
    with mock.patch("subprocess.Popen", return_value=mock_proc), \
         mock.patch("music_scan.process.IMPORT_LOG") as mock_log:
        mock_log.exists.return_value = False
        assert run_beet_import(Path("/some/dir")) == len(MB_503_LINES)


def test_run_inbox_import_raises_when_musicbrainz_was_down(tmp_path: Path) -> None:
    """beets exits 0 after skipping every track; the import must not pass for a clean run (#264)."""
    import music_scan.scan as scan

    job = tmp_path / "usenet" / "keep" / "Kanye West-Yeezus-CD-FLAC-2013-PERFECT"
    job.mkdir(parents=True)
    (job / "01-on_sight.flac").touch()
    with mock.patch.object(scan, "INBOX", tmp_path), \
         mock.patch.object(scan, "run_beet_import", return_value=3), \
         mock.patch.object(scan, "MusicLibrary", return_value=_make_mock_lib()):
        with pytest.raises(scan.MusicBrainzUnavailable):
            scan.run_inbox_import()
    assert (job / "01-on_sight.flac").exists()


def test_run_marks_a_musicbrainz_outage(tmp_path: Path) -> None:
    """The skipped files stay in the inbox for the next scan, not quarantine (#264)."""
    from music_scan import scan

    captured = []

    class FakeMetrics(scan.ScanMetrics):
        def __init__(self):
            super().__init__()
            captured.append(self)

        def push(self):
            pass

    inbox = tmp_path / "inbox"
    (inbox / "spotdl" / "keep").mkdir(parents=True)
    (inbox / "spotdl" / "keep" / "Artist - Song.m4a").touch()
    with mock.patch("music_scan.scan.MusicLibrary", return_value=_make_mock_lib()), \
         mock.patch("music_scan.scan.run_beet_import", return_value=1), \
         mock.patch("music_scan.scan.guard_inbox", return_value={}), \
         mock.patch("music_scan.scan.UNVERIFIED", tmp_path / "unverified"), \
         mock.patch("music_scan.scan.INBOX", inbox), \
         mock.patch("music_scan.scan.SPOTDL_DIR", inbox / "spotdl"), \
         mock.patch("music_scan.scan.QUARANTINE", tmp_path / "quarantine"), \
         mock.patch("music_scan.scan.ScanMetrics", FakeMetrics), \
         mock.patch("music_scan.scan.count_lossless_items", return_value=0), \
         mock.patch("music_scan.scan.run_beet_update"), \
         mock.patch("music_scan.scan.regen_playlists") as regen, \
         mock.patch("music_scan.scan.trigger_scan") as rescan:
        with pytest.raises(scan.MusicBrainzUnavailable):
            scan.run()

    assert captured[0].success is False and captured[0].failure_reason == "musicbrainz_unavailable"
    assert (inbox / "spotdl" / "keep" / "Artist - Song.m4a").exists()
    regen.assert_called_once()
    rescan.assert_called_once()


def test_tag_album_ids_love_tagged_bodies_takes_neither_id(caplog) -> None:
    """2026-10-11 (#278): beets matched Mellon Collie's *Love* (1-9) to *Bodies*' recording, so
    two of the release's items carry *Bodies*' ISRC.  Disc/track tells the real *Bodies* (2-2)
    apart; *Love*'s slot holds a file titled *Bodies*, so *Love* gets no ID by number."""
    love_as_bodies = _lib_item("Bodies (remastered 2012)", isrc="USVI29500016", disc=1, track=9, tracktotal=14)
    bodies = _lib_item("Bodies (remastered 2012)", isrc="USVI29500016", disc=2, track=2, tracktotal=14)
    tracks = [["Love - Remastered 2012", "The Smashing Pumpkins", "sid-love", "USVI29500011", 1, 9],
              ["Bodies - Remastered 2012", "The Smashing Pumpkins", "sid-bodies", "USVI29500016", 2, 2]]
    with caplog.at_level("INFO", logger="music_scan.scan"):
        assert _tag([love_as_bodies, bodies], tracks, tracks_count=14) == 1
    assert bodies.data["spotify_ids"] == "sid-bodies"
    assert love_as_bodies.data["spotify_ids"] == ""
    assert "[NUMBER]" in caplog.text


def test_tag_album_ids_two_release_items_with_one_isrc_and_no_tiebreak_get_no_id(caplog) -> None:
    """Spotify numbers the 30th Anniversary edition on one disc (*Bodies* is 1-16), so neither
    file's slot agrees: which one is *Bodies* takes a listen (#278)."""
    love_as_bodies = _lib_item("Bodies (remastered 2012)", isrc="USVI29500016", disc=1, track=9)
    bodies = _lib_item("Bodies (remastered 2012)", isrc="USVI29500016", disc=2, track=2)
    with caplog.at_level("INFO", logger="music_scan.scan"):
        assert _tag([love_as_bodies, bodies],
                    [["Bodies - Remastered 2012", "The Smashing Pumpkins", "sid-bodies", "USVI29500016", 1, 16]]) == 0
    assert love_as_bodies.data["spotify_ids"] == bodies.data["spotify_ids"] == ""
    assert "[AMBIG]" in caplog.text


def test_tag_album_ids_isrc_prefers_this_release_over_another_albums_copy() -> None:
    """The other album's copy is kept apart (#278): this release's item takes this album's ID."""
    other = _lib_item("1979", isrc="USVI29500020", spotify_ids="sid-best-of", added=10.0)
    mine = _lib_item("1979 (Remastered 2012)", isrc="USVI29500020", track=5)
    assert _tag([other, mine], [["1979 - Remastered 2012", "The Smashing Pumpkins", "sid-mc", "USVI29500020", 2, 5]],
                tracks_count=28) == 1
    assert mine.data["spotify_ids"] == "sid-mc" and other.data["spotify_ids"] == "sid-best-of"

