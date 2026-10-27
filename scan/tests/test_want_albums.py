"""music-audit-want-albums (#322): the want list grouped by album."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest import mock

import pytest

from music_fetch import store
from music_scan import want_albums

URL = "https://open.spotify.com/track/"


def _song(sid: str, album: str | None, name: str = "Song", tracks: int = 10, artist: str = "Artist") -> dict:
    song = {"name": name, "artists": [artist], "url": URL + sid, "song_id": sid, "album_artist": artist,
            "album_name": f"Album {album}", "tracks_count": tracks}
    if album:
        song["album_id"] = album
    return song


SONGS = [
    _song("a1", "A"), _song("b1", "B"), _song("a2", "A"), _song("c1", "C", tracks=0),
    _song("b2", "B"), _song("a3", "A"), _song("h1", "H"), _song("x1", "A"), _song("d1", None, "Local"),
]


def _held(song: dict) -> bool:
    return song["song_id"] == "x1"  # the library holds it


def test_group_counts_wanted_tracks_per_album_in_playlist_order() -> None:
    report = want_albums.group("pl", SONGS, _held, frozenset({URL + "h1"}))

    assert report.entries == 9
    assert report.wanted == 7  # x1 is held by the library, h1 waits in the inbox
    by_key = {a.key: a for a in report.albums}
    assert [s["song_id"] for s in by_key["A"].wanted] == ["a1", "a2", "a3"]
    assert (by_key["A"].first, by_key["B"].first, by_key["C"].first) == (0, 1, 3)
    assert by_key["artist/album none"].name == "Album None"  # album_key falls back to artist/name
    assert report.singletons == 2


def test_over_lists_most_wanted_first_then_earliest() -> None:
    report = want_albums.group("pl", SONGS, lambda s: False, frozenset())

    assert [a.key for a in report.over(2)] == ["A", "B"]
    assert report.covered(2) == 6  # A holds a1, a2, a3 and x1 when nothing resolves
    assert [a.key for a in report.over(1)][:3] == ["A", "B", "C"]
    assert report.over(5) == []


def test_log_reports_summary_albums_and_total(caplog) -> None:
    report = want_albums.group("pl", SONGS, _held, frozenset({URL + "h1"}))
    other = want_albums.group("big", [_song("z1", "Z", tracks=4), _song("z2", "Z", tracks=4)], lambda s: False, frozenset())

    with caplog.at_level(logging.INFO, logger="music_scan.want_albums"):
        want_albums.log_reports([report, other], minimum=2)

    lines = [r.getMessage() for r in caplog.records]
    assert lines[0] == "[WANT] pl: 7 wanted of 9 entries on 4 album(s); 2 album(s) hold 2+ (5 wanted (71%)), 2 singleton(s)"
    assert lines[1] == "  [ALBUM] pl: 3 of 10 track(s) wanted, first at #1: Artist — Album A (A)"
    assert lines[2] == "  [ALBUM] pl: 2 of 10 track(s) wanted, first at #2: Artist — Album B (B)"
    assert lines[3] == "[WANT] big: 2 wanted of 2 entries on 1 album(s); 1 album(s) hold 2+ (2 wanted (100%)), 0 singleton(s)"
    assert lines[4] == "  [ALBUM] big: 2 of 4 track(s) wanted, first at #1: Artist — Album Z (Z)"
    assert lines[5] == "[WANT] total: 9 wanted of 11 entries on 5 album(s); 3 album(s) hold 2+ (7 wanted (77%)), 2 singleton(s)"


def test_total_merges_albums_and_counts_a_shared_track_once(caplog) -> None:
    # s1 is on both playlists; album S has one other wanted track on each, so
    # each playlist alone sees it under the threshold but the total doesn't.
    soft = want_albums.group("soft", [_song("s1", "S"), _song("s2", "S"), _song("q1", "Q")], lambda s: False, frozenset())
    fancy = want_albums.group("fancy", [_song("s1", "S"), _song("s3", "S")], lambda s: False, frozenset())
    single = want_albums.group("one", [_song("t1", "T")], lambda s: False, frozenset())

    with caplog.at_level(logging.INFO, logger="music_scan.want_albums"):
        want_albums.log_reports([soft, fancy, single], minimum=3)

    assert caplog.records[-1].getMessage() == (
        "[WANT] total: 5 wanted of 6 entries on 3 album(s); 1 album(s) hold 3+ (3 wanted (60%)), 2 singleton(s)"
    )
    by_key = {a.key: a for a in want_albums.merged([soft, fancy, single])}
    assert [s["song_id"] for s in by_key["S"].wanted] == ["s1", "s2", "s3"]
    assert [s["song_id"] for s in soft.albums[0].wanted] == ["s1", "s2"]  # the playlist's own report is untouched


def test_summary_of_an_album_with_unknown_track_count_omits_it(caplog) -> None:
    report = want_albums.group("pl", [_song("c1", "C", tracks=0), _song("c2", "C", tracks=0)], lambda s: False, frozenset())
    with caplog.at_level(logging.INFO, logger="music_scan.want_albums"):
        want_albums.log_reports([report])
    assert "  [ALBUM] pl: 2 track(s) wanted, first at #1: Artist — Album C (C)" in caplog.text


@pytest.fixture
def stores(tmp_path: Path, monkeypatch):
    """Three stores: ``pl`` (spotdl), ``keep`` (album) and ``mood`` (nosync)."""
    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    (spotdl_dir / "keep.album").touch()
    (spotdl_dir / "mood.nosync").touch()
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(want_albums, "SPOTDL_DIR", spotdl_dir)
    monkeypatch.setattr(want_albums, "QUARANTINE", tmp_path / "quarantine")
    for name in ("pl", "keep", "mood"):
        store.Store.from_songs(name, SONGS[:3]).save(store.path(name))
    return spotdl_dir


def test_spotdl_playlists_skip_album_and_nosync_sentinels(stores: Path) -> None:
    assert want_albums.spotdl_playlists(stores) == ["pl"]


def test_run_reads_the_library_once_and_reports_the_chosen_playlists(stores: Path, monkeypatch, caplog) -> None:
    item = mock.MagicMock(title="Song", artist="Artist", albumartist="Artist")
    item.get.side_effect = lambda k, d=None: {"spotify_ids": "a1"}.get(k, d)
    lib = mock.MagicMock()
    lib.__enter__ = mock.MagicMock(return_value=lib)
    lib.__exit__ = mock.MagicMock(return_value=False)
    lib.all_items.return_value = [item]
    library = mock.MagicMock(return_value=lib)
    monkeypatch.setattr(want_albums, "MusicLibrary", library)

    with caplog.at_level(logging.INFO, logger="music_scan.want_albums"):
        reports = want_albums.run()
        everything = want_albums.run(all_stores=True)
        one = want_albums.run(playlist="missing")

    assert [r.playlist for r in reports] == ["pl"]
    assert reports[0].wanted == 2  # a1 is in the library
    assert [r.playlist for r in everything] == ["keep", "mood", "pl"]
    assert one == []
    assert "[WANT] missing: no readable store" in caplog.text
    assert library.call_count == 3
