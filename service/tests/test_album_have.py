"""Album hooks' have (#268): the library holds the album whole. No Prefect server needed."""

from __future__ import annotations

from pathlib import Path


def _add(lib, title: str, path: str, **fields):
    from beets.library import Item

    item = Item(**{"title": title, "artist": "Artist", "path": path.encode(), **fields})
    lib._lib.add(item)
    return item


def test_have_needs_the_whole_album_and_then_links_the_playlist(tmp_path: Path) -> None:
    from music_scan.library import MusicLibrary
    from music_service.flows import _album_library_hooks

    record = {"name": "Album", "tracks_count": 2, "playlists": {"later": [["One", "Artist", "S1", None, 1, 1]]}}
    with MusicLibrary(tmp_path / "library.db", tmp_path) as lib:
        _add(lib, "One", "/lib/mix/one.m4a", album="Album", sources="mix", spotify_ids="S1", spotify_album_id="ALB")
        have, _, _ = _album_library_hooks(lib)
        assert not have("ALB", record)  # one track of two

    with MusicLibrary(tmp_path / "library.db", tmp_path) as lib:
        _add(lib, "Two", "/lib/mix/two.m4a", album="Album", sources="mix", spotify_ids="S2", spotify_album_id="ALB")
        have, _, _ = _album_library_hooks(lib)
        assert have("ALB", record)
        one = next(i for i in lib.all_items() if i.title == "One")
        assert one.get("sources") == "mix,later"


def test_have_and_missing_resolve_by_id_or_isrc_never_by_sources(tmp_path: Path) -> None:
    """homelab#1914: ``sources`` is provenance, not membership.  An item tagged
    with the playlist but holding none of the entry's IDs doesn't fill it, as
    the store view wouldn't; one with the entry's ISRC does, whatever its tags."""
    from music_scan.library import MusicLibrary
    from music_service.flows import _album_library_hooks

    tracks = [["One", "Artist", "S1", None, 1, 1], ["Two", "Artist", "S2", "ISRC2", 1, 2]]
    record = {"name": "Album", "tracks_count": 2, "playlists": {"later": tracks}}
    with MusicLibrary(tmp_path / "library.db", tmp_path) as lib:
        _add(lib, "One", "/lib/a/one.m4a", album="Album", sources="later", spotify_ids="S1", spotify_album_id="ALB")
        _add(lib, "Two", "/lib/a/two.m4a", album="Album", sources="later", spotify_album_id="ALB")
        have, missing, _ = _album_library_hooks(lib)
        assert not have("ALB", record)
        assert missing("later", tracks) == [tracks[1]]

    with MusicLibrary(tmp_path / "library.db", tmp_path) as lib:
        _add(lib, "Two (Remastered)", "/lib/b/two.m4a", album="Album", isrc="ISRC2", spotify_album_id="ALB")
        have, missing, _ = _album_library_hooks(lib)
        assert missing("later", tracks) == []
        assert have("ALB", record)
