"""Album playlists at the album level (#268): every local track of every album
on the playlist, and an album is whole only when the library holds it all."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from music_scan.identity import ItemIndex


@pytest.fixture
def real_lib(tmp_path: Path):
    from music_scan.library import MusicLibrary

    with MusicLibrary(tmp_path / "library.db", tmp_path) as lib:
        yield lib


def _add(lib, title: str, path: str, **fields):
    from beets.library import Item

    item = Item(**{"title": title, "artist": "Artist", "path": path.encode(), **fields})
    lib._lib.add(item)
    return item


def _song(name: str, song_id: str, album_id: str = "ALB", album: str = "Album") -> dict:
    return {"name": name, "artists": ["Artist"], "song_id": song_id, "album_id": album_id, "album_name": album,
            "album_artist": "Artist", "tracks_count": 3, "url": f"https://open.spotify.com/track/{song_id}"}


def _release(lib) -> list:
    """A Usenet release of Album, canonicalised: three tracks, one an extra
    with no Spotify ID that adopted the album's tags (#245)."""
    return [
        _add(lib, "Three", "/lib/Album/03.m4a", album="Album", disc=1, track=3, via="usenet", sources="later",
             spotify_ids="S3", spotify_album_id="ALB"),
        _add(lib, "One", "/lib/Album/01.m4a", album="Album", disc=1, track=1, via="usenet", sources="later",
             spotify_ids="S1", spotify_album_id="ALB"),
        _add(lib, "Bonus", "/lib/Album/04.m4a", album="Album", disc=1, track=4, via="usenet", sources="later",
             spotify_album_id="ALB"),
    ]


def _regen(tmp_path: Path, lib, songs_by_playlist: dict[str, list[dict]], album: set[str]) -> dict[str, list[str]]:
    """Regen over playlist stores (homelab#1914): each playlist's songs are its
    store's; an ``album`` playlist has its ``.album`` sentinel.  No ``.spotdl``."""
    from music_fetch import store
    from music_scan.scan import regen_playlists

    spotdl_dir, playlists_dir, store_dir = tmp_path / "spotdl", tmp_path / "playlists", tmp_path / "store"
    spotdl_dir.mkdir(exist_ok=True)
    wrapper = mock.MagicMock()
    wrapper.__enter__ = mock.MagicMock(return_value=lib)
    wrapper.__exit__ = mock.MagicMock(return_value=False)
    with mock.patch.object(store, "STORE_DIR", store_dir), \
         mock.patch("music_scan.scan.SPOTDL_DIR", spotdl_dir), \
         mock.patch("music_scan.scan.PLAYLISTS", playlists_dir), \
         mock.patch("music_scan.scan.MusicLibrary", return_value=wrapper):
        for name, songs in songs_by_playlist.items():
            store.Store.from_songs(name, songs).save(store.path(name))
            if name in album:
                (spotdl_dir / f"{name}.album").touch()
        regen_playlists()
    assert not list(spotdl_dir.glob("*.spotdl"))
    return {name: [Path(line).name for line in (playlists_dir / f"{name}.m3u").read_text().splitlines()]
            for name in songs_by_playlist}


def test_an_album_playlist_lists_the_whole_release_in_disc_track_order(tmp_path: Path, real_lib) -> None:
    """One entry on Spotify lists the album's every local track, including an
    extra with no Spotify ID; a normal playlist stays track level."""
    _release(real_lib)
    lines = _regen(tmp_path, real_lib, {"later": [_song("Three", "S3")], "mix": [_song("Three", "S3")]}, {"later"})
    assert lines["later"] == ["01.m4a", "03.m4a", "04.m4a"]
    assert lines["mix"] == ["03.m4a"]


def test_albums_follow_playlist_order_and_each_file_is_listed_once(tmp_path: Path, real_lib) -> None:
    _release(real_lib)
    _add(real_lib, "Other", "/lib/Other/01.m4a", album="Other", disc=1, track=1, spotify_ids="O1",
         spotify_album_id="OTH")
    songs = [_song("Other", "O1", "OTH", "Other"), _song("One", "S1"), _song("Three", "S3")]
    assert _regen(tmp_path, real_lib, {"later": songs}, {"later"})["later"] == ["01.m4a", "01.m4a", "03.m4a", "04.m4a"]


def test_a_twin_lists_the_representatives_album(tmp_path: Path, real_lib) -> None:
    """#214: the entry is on twin TWIN, canon filed the items on ALB under the same name."""
    _release(real_lib)
    assert _regen(tmp_path, real_lib, {"later": [_song("One", "S1", "TWIN")]}, {"later"})["later"] == \
        ["01.m4a", "03.m4a", "04.m4a"]


def test_a_track_canon_moved_to_another_album_is_listed_alone(tmp_path: Path, real_lib) -> None:
    """A single's track filed on its standard album doesn't bring that album in."""
    _release(real_lib)
    assert _regen(tmp_path, real_lib, {"later": [_song("One", "S1", "SINGLE", "One - Single")]}, {"later"})["later"] == \
        ["01.m4a"]


def test_an_album_whose_tracks_all_left_is_not_listed(tmp_path: Path, real_lib) -> None:
    """The release's items are still in the library (and still tagged ``sources=later``), but no
    store entry is on their album any more: the listing drops them at once."""
    _release(real_lib)
    _add(real_lib, "Other", "/lib/Other/01.m4a", album="Other", spotify_ids="O1", spotify_album_id="OTH")
    assert _regen(tmp_path, real_lib, {"later": [_song("Other", "O1", "OTH", "Other")]}, {"later"})["later"] == \
        ["01.m4a"]


# ---------------------------------------------------------------------------
# have_album: the library holds the album whole
# ---------------------------------------------------------------------------


def _have(lib, record: dict) -> bool:
    from music_scan.playlists import by_album_id
    from music_scan.scan import have_album

    library = ItemIndex(lib.all_items())
    return have_album(lambda pl: ItemIndex(lib.items_by_source(pl)), library, by_album_id(library.items),
                      "ALB", record)


def test_one_track_in_the_library_does_not_make_the_album_have(real_lib) -> None:
    """#268: adding one song on Spotify brings in the whole album."""
    _add(real_lib, "One", "/lib/loose/one.m4a", album="Album", via="spotdl", sources="mix", spotify_ids="S1",
         spotify_album_id="ALB")
    record = {"name": "Album", "tracks_count": 3, "playlists": {"later": [["One", "Artist", "S1", None, 1, 1]]}}
    assert not _have(real_lib, record)


def test_the_whole_album_in_the_library_is_have(real_lib) -> None:
    _release(real_lib)
    record = {"name": "Album", "tracks_count": 3, "playlists": {"later": [["One", "Artist", "S1", None, 1, 1]]}}
    assert _have(real_lib, record)
    record["playlists"]["later"].append(["Gone", "Artist", "S9", None, 1, 9])  # a playlist track still lacking
    assert not _have(real_lib, record)


def test_an_album_records_release_alone_is_the_album() -> None:
    """#279: once its items carry album_record, the release is listed, nothing else on the album ID."""
    from types import SimpleNamespace

    from music_scan.playlists import album_items

    def item(path, disc, track, **f):
        return SimpleNamespace(path=path, disc=disc, track=track, album="Album", get=lambda k, d=None: f.get(k, d))

    second = item("/m/a/2.m4a", 1, 2, album_record="K")
    first = item("/m/a/1.m4a", 1, 1, album_record="K")
    stray = item("/m/b/1.m4a", 1, 1)
    assert album_items("K", "Album", [], {"K": [second, stray, first]}) == [first, second]
