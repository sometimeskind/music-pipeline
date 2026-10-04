"""Tests for music_scan.identity and the library's use of spotify_ids."""

from pathlib import Path
from unittest.mock import MagicMock

from music_scan.identity import add_to_list, spotify_id, split_list


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
