"""Album requests (homelab#2478 stage 2): the MusicBrainz tracklist, the album
tick's request records, and music-requests' dry run, apply and listing."""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from music_fetch import album_requests as req
from music_fetch import album_requests_cli as cli
from music_fetch import albums, resolve, state, store
from music_fetch.albums import Settings, State, tick

sys.path.insert(0, str(Path(__file__).parent))  # importable however pytest imports this file
from mb_fake import (  # noqa: E402
    GROUP, OTHER_RG, R12, R14, RBOOT, RELEASES, RG, RLIVE, FakeMB, _medium, _release, recording,
)
from pg_fixture import pg_url  # noqa: E402, F401 — a fixture

needs_pg = pytest.mark.skipif(not os.environ.get("MUSIC_TEST_DATABASE_URL"),
                              reason="MUSIC_TEST_DATABASE_URL is unset")

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


def _missing_request(status: str = albums.MISSING, **fields) -> dict:
    return {"status": status, "origin": albums.REQUEST, "name": "The Album", "artist": "Band",
            "release_group": RG, "unresolved": None, "spotify_album_ids": [], "names": [], "blocklist": [],
            "requested": True, "tracklist_release": R12, "album_type": "album", "year": 1999,
            "playlists": {req.SOURCE: _request().tracks}, **fields}


def test_the_feed_gives_a_request_its_tracklist_release():
    s = State()
    albums.feed_requests(s, [_request()])
    assert s.albums[RG]["tracklist_release"] == R12


def test_without_its_hooks_a_request_stays_missing():
    record = _missing_request()
    s = State(albums={RG: record})
    albums.fallbacks(s, lambda playlist, tracks: tracks, lambda album_id: [])
    assert record["status"] == albums.MISSING and "fallback_tracks" not in record


def test_a_missing_request_falls_back_from_its_musicbrainz_tracklist_then_fills():
    record = _missing_request()
    s = State(albums={RG: record})
    held: set[str] = {recording(1, 2)}
    reads = []

    def songs(r):
        reads.append(r["tracklist_release"])
        return req.release_songs(FakeMB(), r)

    albums.fallbacks(s, lambda pl, ts: ts, None, songs, lambda key: held)
    assert record["status"] == albums.FALLBACK and record["fallback_from"] == albums.MISSING
    left = [recording(1, i) for i in range(1, 13) if i != 2]
    assert record["fallback_tracks"] == {req.SOURCE: left}
    assert [s["song_id"] for s in albums.fallback_songs(s, req.SOURCE, [])] == left
    assert albums.fallback_queue(s) == 11

    held |= set(left)  # downloaded, verified, imported and stamped
    albums.fallbacks(s, lambda pl, ts: ts, None, songs, lambda key: held)
    assert record["status"] == albums.FILLED and record["fallback_tracks"] == {}
    assert reads == [R12]  # read once


def test_a_request_is_not_filled_before_its_tracklist_is_read():
    record = _missing_request(albums.FAILED)
    s = State(albums={RG: record})

    def broken(r):
        raise OSError("MusicBrainz 503")

    albums.fallbacks(s, lambda pl, ts: ts, None, broken, lambda key: set())
    assert record["status"] == albums.FALLBACK and record["fallback_tracks"] == {}
    assert "album_songs" not in record


def test_a_request_a_playlist_lists_falls_back_as_the_playlists_album():
    record = {**_missing_request(), "origin": albums.ALBUM, "spotify_album_ids": ["sp1"],
              "playlists": {"later": [["Song 1", "Band", "sp1-1", None, 1, 1]]}}
    s = State(albums={RG: record})
    asked = []
    albums.fallbacks(s, lambda pl, ts: ts, lambda album_id: [], lambda r: asked.append(r) or [], lambda key: set())
    assert record["fallback_tracks"] == {"later": ["sp1-1"]} and asked == []


def test_a_requests_musicbrainz_songs_give_way_once_a_playlist_lists_the_album():
    mb_songs = req.release_songs(FakeMB(), _missing_request())
    record = {**_missing_request(albums.FALLBACK), "origin": albums.ALBUM, "spotify_album_ids": ["sp1"],
              "album_songs": mb_songs, "fallback_from": albums.MISSING,
              "playlists": {"later": [["Song 1", "Band", "sp1-1", None, 1, 1]]}}
    s = State(albums={RG: record})
    spotify = [{"song_id": "sp1-1", "url": "https://open.spotify.com/track/sp1-1"}]
    albums.fallbacks(s, lambda pl, ts: ts, lambda album_id: spotify, None, None)
    assert record["album_songs"] == spotify and record["fallback_tracks"] == {"later": ["sp1-1"]}


def test_release_songs_are_whole_spotdl_songs_with_no_spotify_id():
    from spotdl.types.song import Song

    songs = req.release_songs(FakeMB(), _missing_request())
    assert len(songs) == 12
    first = songs[0]
    assert first["song_id"] == recording(1, 1) and first["url"] == f"https://musicbrainz.org/recording/{recording(1, 1)}"
    assert req.url_recording(first["url"]) == recording(1, 1)
    assert first["isrc"] == "GBAAA001001" and first["duration"] == 200 and first["album_id"] == R12
    assert (first["disc_number"], first["track_number"], first["tracks_count"], first["disc_count"]) == (1, 1, 12, 1)
    assert first["album_name"] == "The Album" and first["album_artist"] == "Band" and first["cover_url"] is None
    assert "spotify" not in str(songs)
    song = Song.from_dict(first)  # every field, none extra
    # spotdl asks Spotify again for a song missing any of these.
    assert all(getattr(song, f) is not None for f in ("genres", "disc_count", "tracks_count", "track_number",
                                                       "album_id", "album_artist"))


def test_release_songs_take_the_cover_art_archive_front():
    releases = {R12: {**RELEASES[R12], "cover-art-archive": {"front": True}}}
    [first, *_] = req.release_songs(FakeMB(releases=releases), _missing_request())
    assert first["cover_url"] == f"https://coverartarchive.org/release/{R12}/front-500"


def test_release_songs_refuse_another_groups_release():
    with pytest.raises(req.Refused, match="not"):
        req.release_songs(FakeMB(), _missing_request(release_group=OTHER_RG))


@pytest.mark.parametrize("url", [None, "https://open.spotify.com/track/abc", "https://musicbrainz.org/recording/x"])
def test_url_recording_is_none_for_anything_else(url):
    assert req.url_recording(url) is None


def _search_state(status: str, searched_days_ago: int, **fields) -> State:
    record = _missing_request(status, searched_at=albums._iso(albums._now() - timedelta(days=searched_days_ago)),
                              query="band the album", **fields)
    return State(albums={RG: record})


def test_a_request_with_gaps_is_searched_on_usenet_again_weekly_and_stays_in_fallback_on_a_miss():
    from music_fetch.albums import Settings

    s = _search_state(albums.FALLBACK, 8, fallback_from=albums.MISSING, fallback_tracks={req.SOURCE: ["r"]})
    prowlarr, sabnzbd = MagicMock(), MagicMock()
    prowlarr.blocked.return_value = []
    prowlarr.search.return_value = []
    albums.top_up(s, Settings(mode="on"), prowlarr, sabnzbd, lambda k, r: False, albums.TickResult(),
                  overrides={})
    record = s.albums[RG]
    assert prowlarr.search.called
    assert record["status"] == albums.FALLBACK and record["fallback_tracks"] == {req.SOURCE: ["r"]}
    assert albums._now() - datetime.fromisoformat(record["searched_at"]) < timedelta(minutes=1)


@pytest.mark.parametrize("status,days,tracks", [
    (albums.FALLBACK, 3, {req.SOURCE: ["r"]}),  # not a week yet
    (albums.FALLBACK, 8, {}),  # no gaps
    (albums.FILLED, 30, {}),
])
def test_a_request_without_gaps_or_within_the_week_is_not_searched(status, days, tracks):
    from music_fetch.albums import Settings

    s = _search_state(status, days, fallback_from=albums.MISSING, fallback_tracks=tracks)
    prowlarr = MagicMock()
    prowlarr.blocked.return_value = []
    albums.top_up(s, Settings(mode="on"), prowlarr, MagicMock(), lambda k, r: False, albums.TickResult(),
                  overrides={})
    assert not prowlarr.search.called


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


# --- search (2c) ---


class SearchMB(FakeMB):
    """FakeMB with a release-group search: the album and a live album of the same songs."""

    LIVE_RG = "99999999-0000-4000-8000-000000000001"

    def __call__(self, path: str, **params) -> dict | None:
        if path == "release-group":
            self.calls.append((path, params))
            live = {"id": self.LIVE_RG, "title": "The Album Live", "primary-type": "Album",
                    "secondary-types": ["Live"], "artist-credit": [{"name": "Band"}]}
            return {"release-groups": [GROUP, live][:int(params["limit"])]}
        if path == "release" and params.get("release-group") == self.LIVE_RG:
            self.calls.append((path, params))
            return {"release-count": 1, "releases": [RELEASES[RLIVE]]}
        return super().__call__(path, **params)


def _search(argv: list[str], mb, caplog) -> list[str]:
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="music_fetch.album_requests_cli"):
        cli.run(cli._parser().parse_args(argv), get=mb)
    return [r.getMessage() for r in caplog.records if r.name == "music_fetch.album_requests_cli"]


def test_search_lists_release_groups_with_their_releases_and_flags(caplog):
    """homelab#2478 2c: the query goes to MusicBrainz as typed; live and bootleg are flagged."""
    mb = SearchMB()
    lines = _search(["search", "--", "band", "  the album"], mb, caplog)
    assert mb.calls[0] == ("release-group", {"query": "band the album", "limit": "5"})
    assert lines[0] == f"1. Band & Friend — The Album [release group {RG}; Album; 1999]"
    assert lines[1] == "  5 release(s):"
    # Earliest first: the bootleg (1998), then the 1999 release.
    assert lines[2].startswith(f"  - {RBOOT}: The Album (1998-01-01, GB, CD), 14 track(s) [flagged: bootleg]")
    assert lines[3] == f"  - {R14}: The Album (1999-03-01, GB, CD), 14 track(s)"
    assert any(line.endswith("[flagged: live]") and RLIVE in line for line in lines)
    live = lines.index(f"2. Band — The Album Live [release group {SearchMB.LIVE_RG}; Album + Live]")
    assert lines[live + 1] == "  Flagged: live: not a studio album"
    assert lines[-1].startswith("2 release group(s) for 'band the album'; request one with request_album")
    assert _search(["search", "--limit", "1", "--", "band"], SearchMB(), caplog)[-1].startswith("1 release group(s)")


def test_search_refuses_a_bad_limit_or_query_and_a_musicbrainz_error(caplog):
    for argv in (["search", "--limit", "0", "--", "x"], ["search", "--limit", "11", "--", "x"],
                 ["search", "--", "x" * 201]):
        with pytest.raises(req.Refused):
            _search(argv, SearchMB(), caplog)

    def down(path, **params):
        raise OSError("HTTP Error 400: Bad Request")

    with pytest.raises(req.Refused, match="searching MusicBrainz failed"):
        _search(["search", "--", "x"], down, caplog)


def test_release_flags():
    assert req.release_flags({"status": "Official", "disambiguation": "Live at the BBC, remixes"}) == ["live", "remix"]
    assert req.release_flags({"status": "Promotion"}) == ["promotion"]
    assert req.release_flags({"status": None, "disambiguation": "deluxe"}) == []
    assert req.group_flags({"secondary-types": ["Remix", "Compilation", "Soundtrack"]}) == ["remix", "compilation"]
