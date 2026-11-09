"""Tests for music_scan.backfill (#176)."""

from unittest.mock import MagicMock, patch

import pytest

from music_scan.backfill import (
    apply_album_ids,
    main,
    mb_recordings,
    plan_album_ids,
    plan_backfill,
    report,
    report_albums,
    run,
    run_albums,
    unlink_wrong,
)

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


def test_disjoint_isrcs_on_the_same_musicbrainz_recording_is_not_wrong() -> None:
    """Crabs In A Bucket (#191): Spotify's ISRC differs from MusicBrainz's, but
    MusicBrainz lists it on the item's recording, so it is the same recording."""
    item = _item(1, "Crabs In A Bucket", "keep", isrc="USUM71705082", mb_trackid="f956a6fa")
    lookups = []

    def recordings(isrc):
        lookups.append(isrc)
        return {"f956a6fa", "other"}

    plan = plan_backfill([item], {"keep": [_song("CRABS", "Crabs In A Bucket", isrc="USUM71705065")]}, recordings)
    assert plan.wrong == []
    assert plan.same_recording == 1
    assert lookups == ["USUM71705065"]
    assert item.data["isrc"] == "USUM71705082;USUM71705065"
    assert item.data["spotify_ids"] == "CRABS"


def test_disjoint_isrcs_stay_wrong_when_musicbrainz_disagrees_or_fails() -> None:
    def other(_):
        return {"other"}

    def failed(_):
        return None

    for recordings in (other, failed, None):
        studio = _item(1, "Song", "a", isrc="GBSTUDIO", mb_trackid="mb-studio")
        plan = plan_backfill([studio], {"a": [_song("LIVE", isrc="GBLIVE")]}, recordings)
        assert len(plan.wrong) == 1, recordings
        assert plan.same_recording == 0


def test_musicbrainz_lookup_reads_recordings_and_caches(monkeypatch) -> None:
    import io
    import json
    import urllib.error

    from music_scan import backfill

    calls = []

    def urlopen(req, timeout):
        calls.append(req.full_url)
        if "MISSING" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
        return io.BytesIO(json.dumps({"isrc": "X", "recordings": [{"id": "r1"}, {"id": "r2"}]}).encode())

    monkeypatch.setattr(backfill.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(backfill.time, "sleep", lambda _: None)
    lookup = backfill.MusicBrainzRecordings()
    assert lookup("USX1") == {"r1", "r2"}
    assert lookup("USX1") == {"r1", "r2"}
    assert lookup("MISSING") == set()
    assert len(calls) == 2
    assert calls[0].endswith("/isrc/USX1?fmt=json")


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
    assert old.data["words_ids"] == "S"
    assert "spotify_ids" not in old.data
    assert (plan.by_words, plan.missing) == (1, 1)
    assert plan.changed == {1: old}
    assert "[WORDS]" in caplog.text


def test_words_match_is_a_weak_link_only_and_a_second_run_changes_nothing(caplog) -> None:
    """homelab#2731: title+artist is no identity, so the entry's ID goes in
    ``words_ids`` alone: no ``spotify_ids``, no ISRC (which goes to the file).
    An item with an ID of its own is reached too, and keeps it."""
    bare = _item(1, "Song", "a")
    owned = _item(2, "Other", "a", spotify_url=URL + "OWN")
    songs = {"a": [_song("OWN", "Other"), _song("W1", isrc="XXWORDS0001"), _song("W2", "Other")]}
    with caplog.at_level("INFO", logger="music_scan.backfill"):
        plan = plan_backfill([bare, owned], songs)
        report(plan)
    assert bare.data == {"sources": "a", "words_ids": "W1"}
    assert owned.data["spotify_ids"] == "OWN"
    assert owned.data["words_ids"] == "W2"
    assert "isrc" not in owned.data
    assert (plan.ids_added, plan.isrcs_added, plan.by_words) == (0, 0, 2)
    assert set(plan.changed) == {1, 2}
    assert plan.wrong == []
    assert "2 by title+artist only" in caplog.text

    before = {1: dict(bare.data), 2: dict(owned.data)}
    again = plan_backfill([bare, owned], songs)
    assert again.changed == {}
    assert (again.ids_from_url, again.ids_added, again.isrcs_added, again.by_words) == (0, 0, 0, 2)
    assert {1: bare.data, 2: owned.data} == before


def test_an_id_match_on_a_weakly_linked_item_still_gets_its_id_and_isrc() -> None:
    """A words link doesn't stand in for an ID: the entry that holds the item
    by ISRC still records its Spotify ID there."""
    item = _item(1, "Song", "a", isrc="XXSTRONG001", words_ids="W1")
    plan = plan_backfill([item], {"a": [_song("S", "Another Title", isrc="XXSTRONG001")]})
    assert item.data["spotify_ids"] == "S"
    assert item.data["words_ids"] == "W1"
    assert (plan.ids_added, plan.by_words) == (1, 0)


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


# --albums (homelab#2664), on the issue's real items.


def _usenet(id_, title, track, key, **data):
    item = _item(id_, title, "fancy", via="usenet", album_record=key, **data)
    item.disc, item.track, item.tracktotal = 0, track, 0
    return item


def _record(*entries, status="imported", playlist="fancy"):
    return {"status": status, "playlists": {playlist: list(entries)}}


FALLING_DOWN = ["Falling Down", "Muse", "3vMrcGW4o35zY6vXjWb1p7", "GBCVT9900017", 1, 4]
SLIP = ["Never Let It Slip", "Jerry Harrison", "2Ya4yE3bFfMGKMIRQ53sOc", "GBUM70501082", 1, 8]
WABBITZ = ["Huntin’ Wabbitz", "J. Cole", "2cH8wHImerFokgmhjyhCTU", "USUG12402405", 1, 4]
DAVID = ["David", "Lorde", "6ghDayhHeBXAP4OOnnrFW9", "NZUM72500030", 1, 11]


def test_albums_links_what_the_rung_links_and_reports_the_rest() -> None:
    falling = _usenet(21317, "Falling Down", 4, "1658")
    slip = _usenet(21142, "Never Let It Slip", 7, "1653")
    cowboy = _usenet(21143, "Cowboy's Got to Go", 8, "1653")
    wabbitz = _usenet(21753, "Huntin’ Wabbitz", 4, "1699", isrc="USUG12402418", mb_trackid="clean", channels=2)
    david = _usenet(21978, "David", 11, "1718", isrc="NZUM72500041", mb_trackid="atmos", channels=2)
    records = {"1658": _record(FALLING_DOWN), "1653": _record(SLIP), "1699": _record(WABBITZ), "1718": _record(DAVID)}
    lookup = {"clean": {"isrcs": ["USUG12402418"], "disambiguation": "clean"},
              "atmos": {"isrcs": ["NZUM72500041"], "disambiguation": "Dolby Atmos mix"}}.get
    plan = plan_album_ids([falling, slip, cowboy, wabbitz, david], records, lookup)
    assert [(k.key, k.item.id) for k in plan.links] == [("1658", 21317), ("1718", 21978)]
    assert [(k.key, k.why["why"]) for k in plan.skips] == [("1653", "number, other title"),
                                                           ("1699", "number, other isrc")]
    assert plan.albums == 4
    assert "spotify_ids" not in falling.data, "a plan changes nothing"


def test_albums_leaves_entries_an_item_holds_and_records_not_imported() -> None:
    by_id = _usenet(1, "One", 1, "10", spotify_ids="S1")
    by_isrc = _usenet(2, "Two", 2, "10", isrc="GB2")
    other_copy = _item(3, "Three", "other", spotify_ids="S3", via="spotdl")
    third = _usenet(4, "Three", 3, "10")
    wanted = _usenet(5, "Five", 5, "11")
    records = {"10": _record(["One", "A", "S1", None, 1, 1], ["Two", "A", "S2", "GB2", 1, 2],
                             ["Three", "A", "S3", None, 1, 3]),
               "11": _record(["Five", "A", "S5", None, 1, 5], status="wanted")}
    plan = plan_album_ids([by_id, by_isrc, other_copy, third, wanted], records, lambda _: None)
    assert (plan.links, plan.skips, plan.albums) == ([], [], 1)


def test_albums_reads_only_the_records_release_items() -> None:
    """Not an extra, not a spotdl copy stamped with the record, not another record's item."""
    extra = _usenet(1, "One", 1, "10", album_extra="1")
    spotdl = _usenet(2, "One", 1, "10")
    spotdl.data["via"] = "spotdl"
    other = _usenet(3, "One", 1, "11")
    plan = plan_album_ids([extra, spotdl, other], {"10": _record(["One", "A", "S1", None, 1, 1])}, lambda _: None)
    assert plan.links == [] and plan.albums == 0


def test_albums_one_entry_on_two_playlists_links_once() -> None:
    item = _usenet(1, "One", 1, "10")
    entry = ["One", "A", "S1", None, 1, 1]
    record = {"status": "imported", "playlists": {"a": [entry], "b": [entry]}}
    assert len(plan_album_ids([item], {"10": record}, lambda _: None).links) == 1


def test_albums_apply_writes_ids_only_and_a_second_run_finds_nothing() -> None:
    item = _usenet(21317, "Falling Down", 4, "1658")
    records = {"1658": _record(FALLING_DOWN)}
    plan = plan_album_ids([item], records, lambda _: None)
    before = dict(item.data)
    with patch("music_scan.backfill.events.record") as record:
        apply_album_ids(plan)
    assert {k: v for k, v in item.data.items() if before.get(k) != v} == {
        "spotify_ids": "3vMrcGW4o35zY6vXjWb1p7", "words_ids": "3vMrcGW4o35zY6vXjWb1p7"}
    item.store.assert_called_once_with()
    item.try_write.assert_not_called()
    item.move.assert_not_called()
    (kind, _message), kw = record.call_args
    assert kind == "id_linked" and kw["source"] == "backfill" and kw["item_id"] == 21317
    assert kw["data"]["linked"] == [{"item_id": 21317, "song_id": "3vMrcGW4o35zY6vXjWb1p7", "via": "number"}]
    assert plan_album_ids([item], records, lambda _: None).links == []


def test_albums_report_lists_each_link_and_gap_and_the_totals(caplog) -> None:
    falling = _usenet(21317, "Falling Down", 4, "1658")
    records = {"1658": _record(FALLING_DOWN, ["Gone", "Muse", "S9", None, 1, 13])}
    with caplog.at_level("INFO", logger="music_scan.backfill"):
        report_albums(plan_album_ids([falling], records, lambda _: None))
    assert "[LINK] 1658 fancy: Falling Down — Muse → item 21317 at 1-4" in caplog.text
    assert "[NOID] 1658 fancy: Gone — Muse no item" in caplog.text
    assert "1 empty entr(ies) to link, 1 left empty (1 no item)" in caplog.text


@pytest.mark.parametrize("apply,links,written", [(False, True, False), (True, True, True), (True, False, False)])
def test_run_albums_writes_regenerates_and_rescans_only_on_an_apply_with_links(apply, links, written) -> None:
    item = _usenet(21317, "Falling Down", 4, "1658")
    records = {"1658": _record(FALLING_DOWN if links else ["Gone", "Muse", "S9", None, 1, 13])}
    lib = MagicMock()
    lib.all_items.return_value = [item]
    with patch("music_fetch.state.session"), \
            patch("music_fetch.state.load_albums", return_value=MagicMock(albums=records)), \
            patch("music_scan.library.MusicLibrary") as library, \
            patch("music_scan.audit.pipeline_lock"), \
            patch("music_scan.navidrome.trigger_scan") as rescan, \
            patch("music_scan.scan.regen_playlists", return_value={}) as regen, \
            patch("music_scan.backfill.events.record"):
        library.return_value.__enter__.return_value = lib
        run_albums(apply=apply)
    assert item.store.called is written
    assert regen.called is written and rescan.called is written


def test_main_refuses_redownload_with_albums(monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["music-backfill-ids", "--albums", "--apply", "--redownload", "1"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


def test_mb_recordings_asks_for_isrcs_once_and_a_failed_lookup_is_none() -> None:
    client = MagicMock()
    client.get.side_effect = [{"isrcs": ["X"]}, OSError("down")]
    with patch("music_scan.mb_release.MusicBrainz", return_value=client):
        lookup = mb_recordings()
    assert lookup("a") == {"isrcs": ["X"]}
    assert lookup("a") == {"isrcs": ["X"]}
    assert lookup("b") is None
    client.get.assert_any_call("recording/a", inc="isrcs")
    assert client.get.call_count == 2
