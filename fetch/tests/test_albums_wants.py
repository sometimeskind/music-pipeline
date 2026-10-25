"""Tests for albums-first playlists (#324): the album tick's wants feed, the
hold, and the rules that differ for a record with ``origin: "wants"``."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import music_fetch.albums as albums
from music_fetch import store
from music_fetch.albums import Settings, State, feed_wants, held_tracks, refresh_playlists, tick, top_up
from music_fetch.usenet import Release

MB = 1_000_000


@pytest.fixture(autouse=True)
def _no_push():
    with patch.object(albums, "_push"):
        yield


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(albums, "OVERRIDES_FILE", tmp_path / "album-overrides.conf")


def song(n: int, album_id: str = "A", tracks_count: int = 10, album_type: str = "album", **extra) -> dict:
    return {
        "name": f"T{n}", "artists": ["Artist"], "album_id": album_id, "album_name": f"Album {album_id}",
        "album_artist": "Artist", "tracks_count": tracks_count, "album_type": album_type, "year": 2020,
        "duration": 200, "song_id": f"{album_id}{n}", "isrc": None, "disc_number": 1, "track_number": n,
        "url": f"https://open.spotify.com/track/{album_id}{n}", **extra,
    }


def hook(groups: dict[str, list[tuple[str, list[dict]]]], calls: list | None = None):
    """A wants hook returning *groups* per playlist, as want_albums ranks them."""
    def wants(name, minimum):
        if calls is not None:
            calls.append((name, minimum))
        return [(k, s) for k, s in groups.get(name, []) if len(s) >= minimum]
    return wants


def album_record(status: str = albums.WANTED) -> dict:
    return {"status": status, "blocklist": [], "name": "Whole", "artist": "Artist", "tracks_count": 2,
            "playlists": {"albums": [["One", "Artist", "w1"], ["Two", "Artist", "w2"]]}}


def wants_record(status: str = albums.WANTED, playlist: str = "disc", ids=("A1", "A2")) -> dict:
    return {"status": status, "blocklist": [], "origin": albums.WANTS, "name": "Album A", "artist": "Artist",
            "tracks_count": 10, "playlists": {playlist: [[f"T{i}", "Artist", i] for i in ids]}}


# ---------------------------------------------------------------------------
# The feed
# ---------------------------------------------------------------------------


def test_feed_queues_an_album_with_its_wanted_tracks_only(caplog) -> None:
    caplog.set_level("INFO")
    state = State()
    calls: list = []
    feed_wants(state, ["disc"], hook({"disc": [("A", [song(3), song(7)])]}, calls), Settings(mode="on"))
    record = state.albums["A"]
    assert calls == [("disc", 2)]
    assert record["origin"] == albums.WANTS and record["status"] == albums.WANTED
    assert record["playlists"] == {"disc": [["T3", "Artist", "A3", None, 1, 3], ["T7", "Artist", "A7", None, 1, 7]]}
    # The album's size, for the matcher, is the whole album's.
    assert record["tracks_count"] == 10 and record["duration"] == 2000
    assert "[WANTS] disc: 1 album(s) queued (2 wanted track(s)), 2 held off spotdl" in caplog.text


def test_feed_holds_nothing_unless_on(caplog) -> None:
    caplog.set_level("INFO")
    feed_wants(State(), ["disc"], hook({"disc": [("A", [song(1), song(2)])]}), Settings(mode="dry-run"))
    assert "1 album(s) queued (2 wanted track(s)), 0 held off spotdl" in caplog.text


def test_feed_threshold_comes_from_the_settings(monkeypatch) -> None:
    monkeypatch.setenv("ALBUM_WANT_MIN", "3")
    settings = Settings.from_env()
    assert settings.want_min == 3
    state = State()
    feed_wants(state, ["disc"], hook({"disc": [("A", [song(1), song(2), song(3)]), ("B", [song(1, "B"), song(2, "B")])]}),
               settings)
    assert list(state.albums) == ["A"]
    assert Settings().want_min == 2


def test_feed_skips_local_keys_only(caplog) -> None:
    caplog.set_level("INFO")
    local = [song(1, None), song(2, None)]
    state = State()
    feed_wants(state, ["disc"], hook({"disc": [
        (albums.album_key(local[0]), local),
        ("C", [song(1, "C", album_type="compilation"), song(2, "C", album_type="compilation")]),
        ("B", [song(1, "B", tracks_count=31), song(2, "B", tracks_count=31)]),
        ("U", [song(1, "U", tracks_count=0), song(2, "U", tracks_count=0)]),
        ("A", [song(1), song(2)]),
    ]}), Settings())
    # Compilations and long albums are grabbed like any other.
    assert list(state.albums) == ["C", "B", "U", "A"]
    assert "; 1 local skipped" in caplog.text


def test_feed_leaves_an_album_an_album_playlist_wants_whole() -> None:
    state = State(albums={"A": album_record()})
    feed_wants(state, ["disc"], hook({"disc": [("A", [song(1), song(2)])]}), Settings())
    assert "origin" not in state.albums["A"] and "disc" not in state.albums["A"]["playlists"]


def test_feed_orders_album_playlist_records_first_then_by_rank() -> None:
    state = State(albums={"B": wants_record(), "W": album_record()})
    groups = {"disc": [("A", [song(1), song(2), song(3)]), ("B", [song(1, "B"), song(2, "B")])],
              "mix": [("C", [song(1, "C"), song(2, "C")]), ("A", [song(4), song(5)])]}
    feed_wants(state, ["disc", "mix"], hook(groups), Settings())
    assert list(state.albums) == ["W", "A", "B", "C"]
    # An album on two flagged playlists holds each one's wanted tracks.
    assert [t[2] for t in state.albums["A"]["playlists"]["mix"]] == ["A4", "A5"]
    assert len(state.albums["A"]["playlists"]["disc"]) == 3


def test_feed_updates_a_waiting_record_but_not_a_grabbed_one() -> None:
    state = State(albums={"A": wants_record(), "B": wants_record(albums.GRABBED, ids=("B1", "B2"))})
    feed_wants(state, ["disc"], hook({"disc": [("A", [song(1), song(2), song(3)]),
                                               ("B", [song(1, "B"), song(2, "B"), song(3, "B")])]}), Settings())
    assert len(state.albums["A"]["playlists"]["disc"]) == 3
    assert len(state.albums["B"]["playlists"]["disc"]) == 2


@pytest.mark.parametrize("status", [albums.WANTED, albums.DRY_RUN, albums.MISSING, albums.HAVE])
def test_feed_deletes_a_waiting_record_whose_album_dropped_under_the_threshold(status) -> None:
    state = State(albums={"A": wants_record(status)})
    feed_wants(state, ["disc"], hook({"disc": [("A", [song(1)])]}), Settings())
    assert state.albums == {}


@pytest.mark.parametrize("status", [albums.GRABBED, albums.IMPORTED, albums.FAILED])
def test_feed_keeps_a_record_past_its_search(status) -> None:
    state = State(albums={"A": wants_record(status)})
    feed_wants(state, ["disc"], hook({}), Settings())
    assert state.albums["A"]["playlists"] == wants_record(status)["playlists"]


def test_feed_drops_one_playlist_of_a_record_two_hold() -> None:
    record = wants_record()
    record["playlists"]["mix"] = record["playlists"]["disc"]
    state = State(albums={"A": record})
    feed_wants(state, ["disc", "mix"], hook({"mix": [("A", [song(1), song(2)])]}), Settings())
    assert list(state.albums["A"]["playlists"]) == ["mix"]


def test_feed_drops_the_waiting_records_of_a_playlist_no_longer_flagged() -> None:
    state = State(albums={"A": wants_record(), "B": wants_record(albums.GRABBED)})
    feed_wants(state, [], hook({}), Settings())
    assert list(state.albums) == ["B"]


def test_feed_never_touches_album_playlist_records() -> None:
    state = State(albums={"W": album_record(albums.MISSING)})
    feed_wants(state, ["albums"], hook({}), Settings())
    assert state.albums["W"] == album_record(albums.MISSING)


def test_an_album_playlist_takes_a_wants_record_over() -> None:
    state = State(albums={"A": wants_record()})
    spotify = MagicMock()
    spotify.snapshot_id.return_value = "s1"
    spotify.songs.return_value = [song(1), song(2)]
    refresh_playlists(state, [("albums", "url")], spotify, albums.TickResult())
    record = state.albums["A"]
    assert "origin" not in record and list(record["playlists"]) == ["albums"]


# ---------------------------------------------------------------------------
# The hold
# ---------------------------------------------------------------------------


def test_held_tracks_are_the_waiting_and_grabbed_wants_records_tracks() -> None:
    state = State(albums={
        "A": wants_record(ids=("A1", "A2")),
        "B": wants_record(albums.GRABBED, ids=("B1", "B2")),
        "C": wants_record(albums.MISSING, ids=("C1", "C2")),
        "D": wants_record(albums.IMPORTED, ids=("D1", "D2")),
        "E": wants_record(albums.FAILED, ids=("E1", "E2")),
        "W": album_record(),
    })
    assert held_tracks(state) == {"A1", "A2", "B1", "B2"}


# ---------------------------------------------------------------------------
# Rules that differ for a wants record
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [albums.MISSING, albums.FAILED])
def test_a_missing_or_failed_wants_record_queues_no_fallback(status) -> None:
    state = State(albums={"A": wants_record(status), "W": album_record(status)})
    album_songs = MagicMock(return_value=[])
    albums.fallbacks(state, lambda pl, tracks: tracks, album_songs)
    assert state.albums["A"]["status"] == status and "fallback_tracks" not in state.albums["A"]
    assert state.albums["W"]["status"] == albums.FALLBACK
    # Nothing is read for it, and it leaves the hold.
    assert [c.args for c in album_songs.call_args_list] == [("W",)]
    assert albums.fallback_queue(state) == 2 and held_tracks(state) == set()


def release(guid: str = "r1") -> Release:
    return Release(guid=guid, title="Artist-Album-WEB-FLAC-2020", size=60 * MB, indexer_id=1,
                   download_url=f"http://prowlarr/1/download?id={guid}", categories=[3040])


def test_top_up_grabs_album_playlist_records_before_wants_records(caplog) -> None:
    caplog.set_level("INFO")
    state = State(albums={"W": album_record()})
    feed_wants(state, ["disc"], hook({"disc": [("A", [song(1), song(2)])]}), Settings())
    state.albums = {"A": state.albums["A"], "W": state.albums["W"]}  # as a refresh appends a new album
    feed_wants(state, ["disc"], hook({"disc": [("A", [song(1), song(2)])]}), Settings())
    prowlarr = MagicMock()
    prowlarr.search.return_value = [release()]
    prowlarr.blocked.return_value = []
    sab = MagicMock()
    sab.add_url.return_value = "nzo"
    with patch.object(albums, "rank", side_effect=lambda releases, *a, **k: releases):
        top_up(state, Settings(mode="on", max_in_flight=1), prowlarr, sab, lambda k, r: False, albums.TickResult(),
               overrides={})
        assert state.albums["W"]["status"] == albums.GRABBED and state.albums["A"]["status"] == albums.WANTED
        prowlarr.search.return_value = [release("r2")]
        top_up(state, Settings(mode="on", max_in_flight=2), prowlarr, sab, lambda k, r: False, albums.TickResult(),
               overrides={})
    assert state.albums["A"]["status"] == albums.GRABBED
    assert "[GRAB] Artist — Album A (wants: disc) →" in caplog.text


def test_a_wants_miss_logs_its_tracks_going_back(caplog) -> None:
    caplog.set_level("INFO")
    state = State(albums={"A": wants_record()})
    prowlarr = MagicMock()
    prowlarr.search.return_value = []
    prowlarr.blocked.return_value = []
    top_up(state, Settings(mode="on"), prowlarr, MagicMock(), lambda k, r: False, albums.TickResult(), overrides={})
    assert state.albums["A"]["status"] == albums.MISSING
    assert "its 2 wanted track(s) go back to spotdl" in caplog.text


@pytest.fixture
def roots(tmp_path: Path):
    complete_root = tmp_path / "complete"
    job = complete_root / "album" / "Artist-Album-FLAC"
    job.mkdir(parents=True)
    (job / "01.flac").write_bytes(b"x")
    return complete_root, tmp_path / "inbox", tmp_path / "quarantine"


def complete(state, completion, roots, titles):
    complete_root, inbox, quarantine = roots
    tagged = []
    status = albums.complete(
        state, completion, lambda: None, lambda pl, since, key, guid, job: titles,
        add_source=lambda *a: None, tag_ids=lambda pl, tracks, since, count: tagged.append((pl, tracks)),
        retire=lambda *a: 0, identify=lambda folder: None, import_rest=lambda pl, job: 0,
        complete_root=complete_root, inbox_root=inbox, quarantine_root=quarantine,
    )
    return status, tagged


def grabbed(**extra) -> State:
    record = wants_record(albums.GRABBED, ids=("A1", "A2", "A3", "A4"))
    record.update(nzo_id="nzo1", candidate={"guid": "r1", "title": "Artist-Album-FLAC"}, **extra)
    return State(albums={"A": record})


def test_a_release_lacking_wanted_tracks_is_imported_and_they_go_back(roots, caplog) -> None:
    caplog.set_level("INFO")
    state = grabbed()
    status, tagged = complete(state, albums.Completion("nzo1", True, "album/Artist-Album-FLAC"), roots,
                              ["TA1", "TA2", "TA3", "Other"])
    assert status == albums.IMPORTED
    # Its items get the wanted tracks' Spotify IDs, for the playlist's .m3u.
    assert [pl for pl, _ in tagged] == ["disc"] and len(tagged[0][1]) == 4
    assert "[DONE] Artist — Album A (wants: disc): 4 file(s)" in caplog.text
    assert "1 of its 4 wanted track(s) not in this release go back to spotdl" in caplog.text
    assert held_tracks(state) == set()


def test_a_failed_wants_record_sends_only_its_wanted_tracks_back(roots, caplog) -> None:
    state = grabbed(attempts=albums.MAX_ATTEMPTS - 1)
    status, _ = complete(state, albums.Completion("nzo1", False, "album/Artist-Album-FLAC"), roots, [])
    assert status == albums.FAILED
    assert "its 4 wanted track(s) go back to spotdl" in caplog.text
    albums.fallbacks(state, lambda pl, tracks: tracks)
    assert state.albums["A"]["status"] == albums.FAILED and albums.fallback_queue(state) == 0


# ---------------------------------------------------------------------------
# Tick and metrics
# ---------------------------------------------------------------------------


def test_tick_feeds_before_the_top_up_and_pushes_the_origin(tmp_path: Path) -> None:
    state_file = tmp_path / ".albums.json"
    State(albums={"W": album_record()}).save(state_file)
    seen = []
    with patch.object(albums, "_push") as push, \
            patch.object(albums, "top_up", side_effect=lambda state, *a, **k: seen.append(list(state.albums))):
        tick([], MagicMock(), MagicMock(), MagicMock(), lambda k, r: False, Settings(mode="on"),
             state_file=state_file, wants_first=["disc"], wants=hook({"disc": [("A", [song(1), song(2)])]}))
    assert seen == [["W", "A"]]
    assert State.load(state_file).albums["A"]["origin"] == albums.WANTS
    body = push.call_args.args[0]
    assert 'music_albums{status="wanted"} 2' in body
    assert 'music_albums_status{status="wanted",origin="album"} 1' in body
    assert 'music_albums_status{status="wanted",origin="wants"} 1' in body
    assert body.count("# TYPE music_albums_status gauge") == 1


def test_tick_without_a_wants_hook_feeds_nothing(tmp_path: Path) -> None:
    with patch.object(albums, "feed_wants") as feed, patch.object(albums, "top_up"):
        tick([], MagicMock(), MagicMock(), MagicMock(), lambda k, r: False, Settings(mode="on"),
             state_file=tmp_path / ".albums.json")
    feed.assert_not_called()
