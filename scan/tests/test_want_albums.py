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
    monkeypatch.setattr(want_albums, "SPOTDL_DIR", spotdl_dir)
    monkeypatch.setattr(want_albums, "QUARANTINE", tmp_path / "quarantine")
    for name in ("pl", "keep", "mood"):
        store.save(store.Store.from_songs(name, SONGS[:3]))
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


def _item(item_id: int, album: str | None, sid: str | None = None, tracktotal: int = 0, record: str | None = None):
    fields = {"spotify_album_id": album, "spotify_ids": sid, "tracktotal": tracktotal, "album_record": record,
              "albumartist": "Artist", "album": f"Album {album}"}
    item = mock.MagicMock(id=item_id, path=b"/nonexistent")
    item.get.side_effect = lambda k, d=None: fields.get(k) or d
    return item


def _wants(key: str, *sids: str, tracks: int = 10) -> want_albums.AlbumWants:
    return want_albums.AlbumWants(key, "Artist", f"Album {key}", tracks, [_song(s, key, tracks=tracks) for s in sids], 0)


def test_library_albums_count_held_recordings_once_and_add_the_wanted() -> None:
    items = [
        _item(1, "A", "a1", tracktotal=3), _item(2, "A", "a1"),  # two copies of one recording
        _item(3, "B", "b1"), _item(4, None, "z1"),
    ]
    albums, unplaced = want_albums.library_albums(items, [_wants("B", "b2"), _wants("A", "a2", tracks=12),
                                                          _wants("artist/local", "l1")], {})

    by_key = {a.key: a for a in albums}
    assert unplaced == 1
    assert set(by_key) == {"A", "B"}  # the local-file key is left out
    assert (len(by_key["A"].held), len(by_key["A"].wanted), by_key["A"].tracks) == (1, 1, 12)  # Spotify's count wins
    assert by_key["B"].refs == 2


def test_library_buckets() -> None:
    def album(held, wanted=(), tracks=10, record=None):
        return want_albums.LibraryAlbum("K", "Artist", "Album", tracks, list(held),
                                        [_song(s, "K") for s in wanted], record)

    loose = [_item(1, "K", "k1"), _item(2, "K", "k2")]
    assert album(loose).bucket == "backfill"
    assert album(loose[:1], ["k3"]).bucket == "grab"
    assert album(loose, tracks=2).bucket == "whole"
    assert album([_item(1, "K", "k1", record="K"), loose[1]]).bucket == "whole"  # a release import placed it
    assert album(loose, record={"origin": "wants"}).bucket == "queued"
    assert album(loose, record={"origin": "album"}).bucket == "album playlist"
    assert album(loose).to_land == 8 and album(loose, tracks=0).to_land == 0


def test_log_library_summarises_the_buckets_and_lists_the_grabs(caplog) -> None:
    items = [_item(1, "A", "a1"), _item(2, "A", "a2"), _item(3, "B", "b1"), _item(4, "W", "w1", tracktotal=1),
             _item(5, "W", "w2"), _item(6, None)]
    albums, unplaced = want_albums.library_albums(items, [_wants("B", "b2"), _wants("C", "c1")], {})

    with caplog.at_level(logging.INFO, logger="music_scan.want_albums"):
        want_albums.log_library(albums, unplaced)

    lines = [r.getMessage() for r in caplog.records]
    assert lines[0] == "[LIBRARY] 5 recording(s) held on 4 Spotify album(s), 2 wanted; 1 item(s) with no Spotify album (not counted)"
    assert lines[1] == "[LIBRARY] 3 album(s) with 2+ track(s): whole 1, album playlist 0, queued 0, grab 1, backfill 1"
    assert lines[2].startswith("[LIBRARY] grabbing those 2 whole lands ~9 track(s) (~0.0 GB")
    assert lines[2].endswith("1 album(s) of unknown size), supersedes up to 3 loose copies")
    assert lines[3:] == ["  [BACKFILL] 2 held + 0 wanted: Artist — Album A (A)",
                         "  [GRAB] 1 held + 1 wanted of 10: Artist — Album B (B)"]


def test_library_view_puts_every_store_track_on_its_album() -> None:
    items = [_item(1, "A", "a1"), _item(2, "Z", "z1")]
    stores = {"pl": [_song("a1", "A"), _song("a2", "A")], "mood": [_song("a2", "A"), _song("b1", "B")]}
    albums, unplaced = want_albums.library_view(items, stores, lambda s: s["song_id"] == "a1")

    by_key = {a.key: a for a in albums}
    assert unplaced == 0
    assert (len(by_key["A"].held), [s["song_id"] for s in by_key["A"].wanted]) == (1, ["a2"])  # a2 counts once
    assert {n: [s["song_id"] for s in songs] for n, songs in by_key["A"].on.items()} == {"pl": ["a1", "a2"], "mood": ["a2"]}
    assert by_key["Z"].on == {}  # held, on no playlist
    assert not by_key["A"].whole and want_albums.LibraryAlbum("K", "", "", 1, [items[0]]).whole
