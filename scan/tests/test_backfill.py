"""Tests for music_scan.backfill (#176)."""

from unittest.mock import MagicMock, patch

from music_scan.backfill import plan_backfill, run, unlink_wrong

URL = "https://open.spotify.com/track/"


def _item(id_, title, sources, **data):
    item = MagicMock(id=id_, title=title, artist="Artist", albumartist="Artist", path=f"/lib/{title}.m4a".encode())
    data["sources"] = sources
    item.get = lambda k, d=None: data.get(k, d)
    item.__setitem__ = lambda _, k, v: data.__setitem__(k, v)
    item.data = data
    return item


def _song(sid, name="Song", isrc=None):
    return {"name": name, "artists": ["Artist"], "url": URL + sid, "song_id": sid, "isrc": isrc}


def test_spotify_url_becomes_spotify_ids_and_own_entry_fills_isrc() -> None:
    item = _item(1, "Song", "a", spotify_url=URL + "S")
    plan = plan_backfill([item], {"a": [_song("S", isrc="GBS1")]})
    assert item.data["spotify_ids"] == "S"
    assert item.data["isrc"] == "GBS1"
    assert (plan.ids_from_url, plan.isrcs_added, len(plan.changed)) == (1, 1, 1)


def test_isrc_disagreement_is_counted_and_unioned() -> None:
    tagged = _item(1, "Song", "a", spotify_url=URL + "S", isrc="USMB1")
    agrees = _item(2, "Other", "a", spotify_url=URL + "O", isrc="USMB2;GBO1")
    plan = plan_backfill([tagged, agrees], {"a": [_song("S", isrc="GBS1"), _song("O", "Other", isrc="GBO1")]})
    assert (plan.disagree, plan.compared) == (1, 2)
    assert tagged.data["isrc"] == "USMB1;GBS1"


def test_second_playlist_entry_of_a_merged_duplicate_gets_its_id() -> None:
    """The single (B) was merged into the album track (A): same ISRC, other track ID."""
    item = _item(1, "Song", "a,b", spotify_url=URL + "ALBUM")
    plan = plan_backfill([item], {"a": [_song("ALBUM", isrc="GBS1")], "b": [_song("SINGLE", isrc="GBS1")]})
    assert item.data["spotify_ids"] == "ALBUM,SINGLE"
    assert plan.wrong == []


def test_live_entry_merged_into_studio_is_a_wrong_version() -> None:
    studio = _item(1, "Song", "a,b", spotify_url=URL + "STUDIO")
    plan = plan_backfill([studio], {"a": [_song("STUDIO", isrc="GBSTUDIO")], "b": [_song("LIVE", isrc="GBLIVE")]})
    assert [(w.playlist, w.song["song_id"], w.item) for w in plan.wrong] == [("b", "LIVE", studio)]
    assert "LIVE" not in studio.data["spotify_ids"]


def test_wrong_version_merged_after_177_is_caught_through_its_id() -> None:
    """Merges since #177 recorded the live entry's ID on the studio item."""
    studio = _item(1, "Song", "a,b", spotify_url=URL + "STUDIO", spotify_ids="STUDIO,LIVE")
    plan = plan_backfill([studio], {"a": [_song("STUDIO", isrc="GBSTUDIO")], "b": [_song("LIVE", isrc="GBLIVE")]})
    assert len(plan.wrong) == 1

    unlink_wrong(plan, plan.wrong[0])
    assert studio.data["sources"] == "a"
    assert studio.data["spotify_ids"] == "STUDIO"


def test_unlink_keeps_the_source_when_another_entry_wants_the_item() -> None:
    """Playlist b lists both versions: the studio item stays on b."""
    studio = _item(1, "Song", "b", spotify_url=URL + "STUDIO")
    plan = plan_backfill([studio], {"b": [_song("STUDIO", isrc="GBSTUDIO"), _song("LIVE", isrc="GBLIVE")]})
    unlink_wrong(plan, plan.wrong[0])
    assert studio.data["sources"] == "b"


def test_words_match_without_isrcs_is_recorded_and_missing_is_counted(caplog) -> None:
    old = _item(1, "Song", "a")
    with caplog.at_level("INFO", logger="music_scan.backfill"):
        plan = plan_backfill([old], {"a": [_song("S"), _song("N", "Not Here")]})
    assert old.data["spotify_ids"] == "S"
    assert (plan.by_words, plan.missing) == (1, 1)
    assert "[WORDS]" in caplog.text


def _run(items, songs, apply=False, redownload=0, downloaded=True):
    lib = MagicMock()
    lib.__enter__.return_value = lib
    lib.all_items.return_value = items
    reader = MagicMock()
    reader.songs.side_effect = lambda url: songs[url]
    playlists = [MagicMock(url=name) for name in songs]
    for pl, name in zip(playlists, songs):
        pl.name = name
    with patch("music_scan.library.MusicLibrary", return_value=lib), \
         patch("music_fetch.config.load_playlists", return_value=playlists), \
         patch("music_fetch.spotdl_ops.SpotifyPlaylists", return_value=reader), \
         patch("music_fetch.spotdl_ops.download_song", return_value="/inbox/b/x.m4a" if downloaded else None) as dl, \
         patch("music_fetch.ingest.SPOTDL_DIR") as spotdl_dir:
        plan = run(apply=apply, redownload=redownload)
    return plan, dl, spotdl_dir


def _studio_and_live():
    studio = _item(1, "Song", "a,b", spotify_url=URL + "STUDIO")
    return studio, {"a": [_song("STUDIO", isrc="GBSTUDIO")], "b": [_song("LIVE", isrc="GBLIVE")]}


def test_dry_run_writes_nothing() -> None:
    studio, songs = _studio_and_live()
    _, dl, _ = _run([studio], songs)
    studio.store.assert_not_called()
    dl.assert_not_called()


def test_apply_writes_but_leaves_wrong_versions_without_redownload() -> None:
    studio, songs = _studio_and_live()
    _, dl, _ = _run([studio], songs, apply=True)
    studio.store.assert_called_once()
    dl.assert_not_called()
    assert studio.data["sources"] == "a,b"


def test_redownload_fetches_the_right_version_and_unlinks_the_wrong_one() -> None:
    studio, songs = _studio_and_live()
    _, dl, spotdl_dir = _run([studio], songs, apply=True, redownload=5)
    assert dl.call_args.args[0]["song_id"] == "LIVE"
    spotdl_dir.__truediv__.assert_called_with("b")
    assert studio.data["sources"] == "a"


def test_failed_redownload_keeps_the_wrong_version_on_the_playlist() -> None:
    studio, songs = _studio_and_live()
    _run([studio], songs, apply=True, redownload=5, downloaded=False)
    assert studio.data["sources"] == "a,b"
