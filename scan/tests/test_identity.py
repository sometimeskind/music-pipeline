"""Tests for music_scan.identity and the library's use of spotify_ids."""

from pathlib import Path
from unittest.mock import MagicMock

from music_scan.identity import (
    BY_ID,
    BY_ISRC,
    BY_WORDS,
    ItemIndex,
    add_isrcs,
    add_to_list,
    item_spotify_ids,
    spotify_id,
    split_list,
)


def _item(title="Song", **data):
    item = MagicMock(title=title, artist="Artist", albumartist="Artist")
    item.get = lambda k, d=None: data.get(k, d)
    item.__setitem__ = lambda _, k, v: data.__setitem__(k, v)
    item.data = data
    return item


def test_spotify_id_from_track_url() -> None:
    assert spotify_id("https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC") == "4uLU6hMCjMI75M1A2tKUQC"
    assert spotify_id("https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC?si=abc") == "4uLU6hMCjMI75M1A2tKUQC"


def test_spotify_id_rejects_other_urls() -> None:
    assert spotify_id(None) is None
    assert spotify_id("") is None
    assert spotify_id("https://open.spotify.com/album/1") is None
    assert spotify_id("https://www.youtube.com/watch?v=x") is None


def test_split_list_drops_blanks() -> None:
    assert split_list(" a, ,b,") == ["a", "b"]
    assert split_list(None) == []
    assert split_list("X;Y", sep=";") == ["X", "Y"]


def test_add_to_list_appends_once() -> None:
    data: dict = {}
    item = MagicMock()
    item.get = lambda k, d=None: data.get(k, d)
    item.__setitem__ = lambda _, k, v: data.__setitem__(k, v)

    assert add_to_list(item, "spotify_ids", "A")
    assert add_to_list(item, "spotify_ids", "B")
    assert not add_to_list(item, "spotify_ids", "A")
    assert not add_to_list(item, "spotify_ids", None)
    assert data["spotify_ids"] == "A,B"


def test_spotify_urls_by_source_includes_spotify_ids(tmp_path: Path) -> None:
    """A duplicate that took a second playlist keeps the first one's spotify_url;
    reconcile must still see the second entry's URL."""
    from beets.library import Item

    from music_scan.library import MusicLibrary

    with MusicLibrary(tmp_path / "library.db", tmp_path) as lib:
        item = Item(title="Song", artist="Artist")
        item["sources"] = "a,b"
        item["spotify_url"] = "https://open.spotify.com/track/ALBUM"
        item["spotify_ids"] = "ALBUM,SINGLE"
        lib._lib.add(item)
        legacy = Item(title="Old", artist="Artist")
        legacy["sources"] = "b"
        legacy["spotify_url"] = "https://open.spotify.com/track/OLD"
        lib._lib.add(legacy)

        assert lib.spotify_urls_by_source("b") == {
            "https://open.spotify.com/track/ALBUM",
            "https://open.spotify.com/track/SINGLE",
            "https://open.spotify.com/track/OLD",
        }


def test_item_spotify_ids_includes_legacy_spotify_url() -> None:
    item = _item(spotify_ids="A,B", spotify_url="https://open.spotify.com/track/C")
    assert item_spotify_ids(item) == {"A", "B", "C"}


def test_add_isrcs_unions_musicbrainz_and_spotify() -> None:
    item = _item(isrc="USX1;GBX2")
    assert add_isrcs(item, ["GBX2", "SEX3"])
    assert item.data["isrc"] == "USX1;GBX2;SEX3"
    assert not add_isrcs(item, ["USX1", None])


def test_item_index_ladder() -> None:
    by_id = _item("Song", spotify_ids="SID")
    by_isrc = _item("Song (Remastered)", isrc="A;B")
    by_words = _item("Other")
    index = ItemIndex([by_id, by_isrc, by_words])

    assert index.match("SID", "B", "Song", "Artist") == (by_id, BY_ID)
    assert index.match("NOPE", "B", "Song", "Artist") == (by_isrc, BY_ISRC)
    assert index.match(None, None, "Other", "Artist") == (by_words, BY_WORDS)
    assert index.match(None, None, "Other", "Artist", words=False) == (None, None)
    assert index.match_song({"name": "x", "url": "https://open.spotify.com/track/SID"}) == (by_id, BY_ID)
    assert dict(index.rungs) == {BY_ID: 2, BY_ISRC: 1, BY_WORDS: 1}


def _beets_lib(tmp_path: Path):
    from beets.library import Item

    from music_scan.library import MusicLibrary

    lib = MusicLibrary(tmp_path / "library.db", tmp_path)

    def add(title, **flex):
        item = Item(title=title, artist="Artist", isrc=flex.pop("isrc", ""))
        for k, v in flex.items():
            item[k] = v
        lib._lib.add(item)
        return item

    return lib, add


def test_clear_source_tag_matches_removed_entry_by_spotify_id(tmp_path: Path) -> None:
    """Studio and live share a title; removing the live entry leaves the studio take."""
    lib, add = _beets_lib(tmp_path)
    with lib:
        studio = add("Song", sources="a", spotify_ids="STUDIO")
        live = add("Song", sources="a", spotify_ids="LIVE")
        assert lib.clear_source_tag("Song", "Artist", "a", spotify_id="LIVE")
        assert lib._lib.get_item(live.id).get("sources") == ""
        assert lib._lib.get_item(studio.id).get("sources") == "a"


def test_clear_source_tag_by_isrc_then_words(tmp_path: Path, caplog) -> None:
    lib, add = _beets_lib(tmp_path)
    with lib:
        remaster = add("Song (Remastered)", sources="a,b", isrc="X1;X2")
        old = add("Old", sources="a")
        assert lib.clear_source_tag("Song", "Artist", "a", spotify_id="NOPE", isrc="X2")
        assert lib._lib.get_item(remaster.id).get("sources") == "b"
        with caplog.at_level("INFO", logger="music_scan.library"):
            assert lib.clear_source_tag("Old", "Artist", "a")
        assert lib._lib.get_item(old.id).get("sources") == ""
        assert "[WORDS]" in caplog.text
