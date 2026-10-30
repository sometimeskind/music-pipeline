"""Album requests (homelab#2478 stage 2): the MusicBrainz tracklist, the album
tick's request records, and music-requests' dry run, apply and listing."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from music_fetch import album_requests as req
from music_fetch import album_requests_cli as cli
from music_fetch import albums, resolve, state, store
from music_fetch.albums import Settings, State, tick

sys.path.insert(0, str(Path(__file__).parent))  # importable however pytest imports this file
from pg_fixture import pg_url  # noqa: E402, F401 — a fixture

needs_pg = pytest.mark.skipif(not os.environ.get("MUSIC_TEST_DATABASE_URL"),
                              reason="MUSIC_TEST_DATABASE_URL is unset")

RG = "6e0a6c2a-2d38-3d3c-9d38-4f0c2b5b7a11"
OTHER_RG = "0b4ed3a1-7d3c-4d9e-9b8e-0c5c6a8d2f10"
R12 = "11111111-1111-4111-8111-111111111111"   # 12 tracks, 2001
R12B = "22222222-2222-4222-8222-222222222222"  # 12 tracks, 2005
R14 = "33333333-3333-4333-8333-333333333333"   # 14 tracks, 1999: earliest, but the rarer count
RLIVE = "44444444-4444-4444-8444-444444444444"
RBOOT = "55555555-5555-4555-8555-555555555555"


def _medium(n: int, position: int = 1, fmt: str = "CD", isrc: bool = True) -> dict:
    return {"position": position, "format": fmt, "track-count": n, "tracks": [
        {"position": i, "title": f"Song {i}", "length": 200_000,
         "recording": {"title": f"Song {i}", "isrcs": [f"GBAAA00{position}{i:03d}"] if isrc else []}}
        for i in range(1, n + 1)]}


def _release(rid: str, n: int, date: str, status: str = "Official", group: str = RG, **extra) -> dict:
    return {"id": rid, "title": "The Album", "status": status, "date": date, "country": "GB",
            "media": [_medium(n)], "release-group": {"id": group}, **extra}


GROUP = {"id": RG, "title": "The Album", "primary-type": "Album", "secondary-types": [],
         "first-release-date": "1999-03-01",
         "artist-credit": [{"name": "Band", "joinphrase": " & "}, {"name": "Friend", "joinphrase": ""}]}
RELEASES = {
    R12: _release(R12, 12, "2001-01-01"),
    R12B: _release(R12B, 12, "2005-01-01"),
    R14: _release(R14, 14, "1999-03-01"),
    RLIVE: _release(RLIVE, 14, "2002-01-01", disambiguation="live in Tokyo"),
    RBOOT: _release(RBOOT, 14, "1998-01-01", status="Bootleg"),
}


class FakeMB:
    def __init__(self, group: dict | None = GROUP, releases: dict | None = None) -> None:
        self.group = group
        self.releases = RELEASES if releases is None else releases
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, path: str, **params) -> dict | None:
        self.calls.append((path, params))
        if path == f"release-group/{RG}":
            return self.group
        if path.startswith("release-group/"):
            return None
        if path == "release":
            assert params["release-group"] == RG and params["inc"] == "media"
            listed = list(self.releases.values())
            offset = int(params["offset"])
            return {"release-count": len(listed), "releases": listed[offset:offset + int(params["limit"])]}
        return self.releases.get(path.removeprefix("release/"))


# --- the tracklist ---


@pytest.mark.parametrize("value,kind,want", [
    (RG, "release-group", RG),
    (f" https://musicbrainz.org/release-group/{RG.upper()}?tab=x ", "release-group", RG),
    (f"musicbrainz.org/release/{R12}", "release", R12),
])
def test_mbid_takes_an_mbid_or_a_musicbrainz_url(value, kind, want):
    assert req.mbid(value, kind) == want


@pytest.mark.parametrize("value,kind", [
    ("not an id", "release-group"),
    (f"https://musicbrainz.org/release/{R12}", "release-group"),  # another kind's URL
    (f"https://evil.example/release-group/{RG}", "release-group"),
])
def test_mbid_refuses_anything_else(value, kind):
    with pytest.raises(ValueError):
        req.mbid(value, kind)


def test_without_a_preferred_release_the_most_common_official_count_wins():
    found = req.tracklist(FakeMB(), RG)
    # 12 tracks twice (R12, R12B) beats 14 once (R14): the live one and the bootleg don't count.
    assert found.release == R12
    assert len(found.tracks) == 12
    assert "the most common count among 3" in found.how and "14 tracks ×1" in found.how
    assert found.artist == "Band & Friend" and found.name == "The Album" and found.year == 1999
    assert found.album_type == "album" and found.duration == 12 * 200
    assert found.tracks[0] == ["Song 1", "Band & Friend", None, "GBAAA001001", 1, 1]


def test_a_tie_goes_to_the_count_of_the_earliest_release():
    releases = {k: RELEASES[k] for k in (R12, R14)}
    assert req.tracklist(FakeMB(releases=releases), RG).release == R14


def test_a_live_release_group_keeps_its_live_releases():
    group = {**GROUP, "secondary-types": ["Live"]}
    releases = {RLIVE: RELEASES[RLIVE]}
    assert req.tracklist(FakeMB(group=group, releases=releases), RG).release == RLIVE


def test_the_preferred_release_is_the_tracklist():
    mb = FakeMB()
    found = req.tracklist(mb, RG, R14)
    assert found.release == R14 and len(found.tracks) == 14 and found.how == "preferred"
    assert not any(path == "release" for path, _ in mb.calls)  # no browse


def test_video_media_are_left_out():
    release = {**RELEASES[R12], "media": [_medium(12), _medium(3, position=2, fmt="DVD-Video")]}
    found = req.tracklist(FakeMB(releases={R12: release}), RG, R12)
    assert len(found.tracks) == 12 and {t[4] for t in found.tracks} == {1}


@pytest.mark.parametrize("mb,preferred,match", [
    (FakeMB(group=None), None, "no release group"),
    (FakeMB(), "99999999-9999-4999-8999-999999999999", "no release"),
    (FakeMB(releases={R12: _release(R12, 12, "2001", group=OTHER_RG)}), R12, "of release group"),
    (FakeMB(releases={RBOOT: RELEASES[RBOOT]}), None, "no official release"),
])
def test_a_tracklist_that_cant_be_read_is_refused(mb, preferred, match):
    with pytest.raises(req.Refused, match=match):
        req.tracklist(mb, RG, preferred)


# --- the album tick's records ---


def _request(group: str = RG, n: int = 3) -> req.Request:
    tracks = [[f"Song {i}", "Band", None, f"ISRC{i}", 1, i] for i in range(1, n + 1)]
    return req.Request(group, R12, "Band", "The Album", tracks, {"year": 1999, "album_type": "album",
                                                                   "duration": 600})


def _wants_record(key: str, status: str = albums.WANTED, **fields) -> dict:
    return {**state.new_record(key, status=status, blocklist=[], origin=albums.WANTS, name="The Album",
                               artist="Band", playlists={"chill": [["Song 2", "Band", "sp-2", None, 1, 2]]}),
            "release_group": RG, "unresolved": None, **fields}


def test_a_request_with_no_record_gets_a_new_one():
    s = State()
    albums.feed_requests(s, [_request()])
    [(key, record)] = s.albums.items()
    assert key == RG  # not saved yet: keyed the old way, by its release group
    assert record["origin"] == albums.REQUEST and record["status"] == albums.WANTED
    assert record["release_group"] == RG and record["unresolved"] is None and record["spotify_album_ids"] == []
    assert record["playlists"] == {req.SOURCE: _request().tracks}
    assert record["tracks_count"] == 3 and record["duration"] == 600 and record["year"] == 1999
    assert albums.plan(key, record, {}).queries[0] == "band the album"
    albums.feed_requests(s, [_request()])
    assert len(s.albums) == 1  # idempotent


def test_requests_are_searched_ahead_of_the_fed_records():
    s = State(albums={"w": {**_wants_record("w"), "release_group": OTHER_RG}})
    albums.feed_requests(s, [_request()])
    assert list(s.albums) == [RG, "w"]


def test_a_request_takes_a_waiting_wants_record_over():
    s = State(albums={"sp1": _wants_record("sp1")})
    albums.feed_requests(s, [_request()])
    record = s.albums["sp1"]
    assert record["origin"] == albums.REQUEST
    assert record["playlists"] == {req.SOURCE: _request().tracks}  # the whole album, not the wanted track
    assert record["spotify_album_ids"] == ["sp1"]


def test_a_grabbed_wants_record_keeps_its_tracks_and_becomes_a_request():
    s = State(albums={"sp1": _wants_record("sp1", status=albums.GRABBED)})
    albums.feed_requests(s, [_request()])
    record = s.albums["sp1"]
    assert record["origin"] == albums.REQUEST and record["status"] == albums.GRABBED and record["requested"]
    assert list(record["playlists"]) == ["chill"]  # its import is judged on the tracks it was grabbed for


def test_an_album_playlist_record_keeps_its_origin_and_stays_when_it_leaves_the_playlist():
    record = {**state.new_record("sp1", status=albums.IMPORTED, blocklist=[], name="The Album", artist="Band",
                                 playlists={"later": [["Song 1", "Band", "sp1-1", None, 1, 1]]}),
              "release_group": RG, "unresolved": None}
    s = State(albums={"sp1": record}, playlists={"later": {"snapshot_id": "old"}})
    albums.feed_requests(s, [_request()])
    # The playlist's Spotify tracks stand in for the request's: one tracklist for have and the import.
    assert "origin" not in record and list(record["playlists"]) == ["later"] and record["requested"]
    spotify = MagicMock()
    spotify.snapshot_id.return_value = "new"
    spotify.songs.return_value = []
    with patch.object(store, "load", return_value=None), patch.object(albums, "_save_store"):
        albums.refresh_playlists(s, [("later", "url")], spotify, albums.TickResult())
    assert s.albums["sp1"] is record and record["playlists"] == {}  # off the playlist, still kept
    albums.feed_requests(s, [_request()])
    assert record["origin"] == albums.REQUEST and record["status"] == albums.IMPORTED
    assert record["playlists"] == {req.SOURCE: _request().tracks}


def test_a_request_record_an_album_playlist_lists_again_drops_the_request_tracks():
    record = {"status": albums.WANTED, "origin": albums.REQUEST, "release_group": RG, "unresolved": None,
              "spotify_album_ids": [], "names": [], "blocklist": [], "requested": True,
              "playlists": {req.SOURCE: _request().tracks}}
    s = State(albums={RG: record})
    resolve.merge_into(record, {"spotify_album_ids": ["sp1"], "playlists": {"later": [["Song 1", "Band", "sp1-1",
                                                                                         None, 1, 1]]}})
    albums.feed_requests(s, [_request()])
    assert list(record["playlists"]) == ["later"] and "origin" not in record


def test_a_request_never_falls_back_to_spotdl():
    record = {"status": albums.MISSING, "origin": albums.REQUEST, "name": "The Album", "artist": "Band",
              "release_group": RG, "unresolved": None, "spotify_album_ids": [], "names": [], "blocklist": [],
              "playlists": {req.SOURCE: _request().tracks}}
    s = State(albums={RG: record})
    albums.fallbacks(s, lambda playlist, tracks: tracks, lambda album_id: [])
    assert record["status"] == albums.MISSING and "fallback_tracks" not in record


def test_a_merge_with_an_album_playlist_edition_keeps_the_album_origin():
    survivor = {"origin": albums.REQUEST, "playlists": {req.SOURCE: []}}
    resolve.merge_into(survivor, {"playlists": {"later": []}})
    assert "origin" not in survivor
    wants = {"origin": albums.REQUEST}
    resolve.merge_into(wants, {"origin": albums.WANTS})
    assert wants["origin"] == albums.REQUEST


def test_the_status_metrics_count_requests():
    s = State(albums={RG: {"status": albums.MISSING, "origin": albums.REQUEST}})
    assert albums.origin_counts(s)[(albums.MISSING, albums.REQUEST)] == 1


# --- music-requests ---


def test_parse_dedupes_and_refuses_conflicting_preferences():
    assert cli.parse([RG, f"https://musicbrainz.org/release-group/{RG}", f"{OTHER_RG}={R12}"]) == [
        (RG, None), (OTHER_RG, R12)]
    with pytest.raises(req.Refused, match="twice"):
        cli.parse([RG, f"{RG}={R12}"])
    with pytest.raises(req.Refused):
        cli.parse([f"{RG}=nonsense"])


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(albums, "OVERRIDES_FILE", tmp_path / "album-overrides.conf")
    monkeypatch.setattr(state, "SWITCH", tmp_path / ".state-in-db")


@pytest.fixture
def switched(tmp_path: Path, pg_url):
    (tmp_path / ".state-in-db").touch()
    return pg_url


def _run(argv: list[str], mb: FakeMB, caplog) -> list[str]:
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="music_fetch.album_requests_cli"):
        cli.run(cli._parser().parse_args(argv), get=mb)
    return [r.getMessage() for r in caplog.records if r.name == "music_fetch.album_requests_cli"]


def test_music_requests_refuses_before_the_state_is_in_the_database(caplog):
    with pytest.raises(req.Refused, match="music-state-import"):
        _run(["list"], FakeMB(), caplog)


@needs_pg
def test_the_dry_run_then_the_apply_add_a_request(switched, caplog, monkeypatch):
    monkeypatch.setenv("MUSIC_EVENT_CALLER", "sub-tom")
    mb = FakeMB()
    dry = _run(["add", RG], mb, caplog)
    assert dry == _run(["add", RG], mb, caplog)  # nothing in it changes between two runs
    assert dry[0].startswith(f"Request 1 of 1: Band & Friend — The Album [release group {RG}; album; 1999]")
    assert f"12 track(s), 12 with an ISRC, from release {R12}" in dry[1]
    assert "none has the release group yet" in dry[2] and '"band and friend the album"' in dry[2]
    assert dry[-1] == "Dry run; --apply to write"
    with state.connect() as conn:
        assert req.load(conn) == []
    applied = _run(["add", "--apply", RG], mb, caplog)
    assert applied[-1] == "Applied: 1 request(s) added"
    with state.connect() as conn:
        [request] = req.load(conn)
        assert (request.release_group, request.tracklist_release, request.preferred_release,
                request.requested_by) == (RG, R12, None, "sub-tom")
        assert len(request.tracks) == 12 and request.tracks[0][2] is None
        event = conn.execute("SELECT kind, source, release_group::text, caller, data FROM events").fetchone()
        assert event[:4] == ("album_requested", "requests", RG, "sub-tom")
        assert event[4]["tracklist_release"] == R12 and event[4]["preferred_release"] is None
    again = _run(["add", RG], mb, caplog)
    assert "requested already, by sub-tom" in again[0] and "nothing changes" in again[0]
    listed = _run(["list"], mb, caplog)
    assert "pending: the next album tick queues it" in listed[0]


@needs_pg
def test_a_refused_request_writes_nothing(switched, caplog):
    with pytest.raises(req.Refused):
        _run(["add", "--apply", RG, f"{OTHER_RG}"], FakeMB(), caplog)
    with state.connect() as conn:
        assert req.load(conn) == []


@needs_pg
def test_the_tick_queues_a_request_and_the_listing_follows_its_album(switched, tmp_path: Path, caplog):
    with state.connect() as conn:
        state.save_albums(conn, state.AlbumRows(albums={"sp1": _wants_record("sp1")}))
    _run(["add", "--apply", f"{RG}={R14}"], FakeMB(), caplog)
    prowlarr = MagicMock()
    prowlarr.blocked.return_value = []
    prowlarr.search.return_value = []
    with patch.object(albums, "_push"):
        tick([], MagicMock(), prowlarr, MagicMock(), lambda k, r: False, Settings(mode="dry-run"),
             tmp_path / ".albums.json", library=None, wants=None)
    loaded = State.load()
    [record] = loaded.albums.values()
    assert record["origin"] == albums.REQUEST and record["tracks_count"] == 14
    assert list(record["playlists"]) == [req.SOURCE]
    with state.connect() as conn:
        [entry] = req.listing(conn, state.load_albums(conn))
        assert entry["status"] == record["status"] and entry["album"]["origin"] == albums.REQUEST
        assert entry["preferred_release"] == R14 and entry["tracks"] == 14
        assert req.listing(conn, state.load_albums(conn), req.PENDING) == []
        origin = conn.execute("SELECT origin FROM albums").fetchone()
        assert origin == ("request",)
