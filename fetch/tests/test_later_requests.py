"""Automatic requests for Later's incomplete albums (homelab#2535): which albums
the album tick requests, once each, and music-later auto-requests' listing."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from music_fetch import album_requests as req
from music_fetch import albums, later_cli, later_queue, later_requests, state

sys.path.insert(0, str(Path(__file__).parent))  # importable however pytest imports this file
from mb_fake import OTHER_RG, R12, RG, FakeMB  # noqa: E402

HELD_RG = "7c3f5a2e-1b4d-4e6f-8a9b-0c1d2e3f4a5b"
ASKED_RG = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
SPOTIFY_REF = "0123456789abcdefABCDEF"  # a Spotify album ID: the row has no release group yet


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(albums, "OVERRIDES_FILE", tmp_path / "album-overrides.conf")


def _later_row(group: str | None, status: str, name: str) -> dict:
    """A former ``later`` album playlist row (no origin), with its Spotify tracklist."""
    spotify = [SPOTIFY_REF] if group is None else []
    return {"status": status, "release_group": group, "unresolved": None if group else state.PENDING,
            "spotify_album_ids": spotify, "names": [], "name": name, "artist": "Band", "tracks_count": 2,
            "blocklist": [], "playlists": {"later": [["One", "Band", "sp-1", None, 1, 1],
                                                     ["Two", "Band", "sp-2", None, 1, 2]]}}


def _setup() -> None:
    """Later: one album held whole, one incomplete (RG, the one MusicBrainz knows), one
    requested already, one queued by ref, and one with no album row (OTHER_RG)."""
    rows = {
        HELD_RG: _later_row(HELD_RG, albums.IMPORTED, "Held"),
        RG: _later_row(RG, albums.FALLBACK, "Partial"),
        ASKED_RG: _later_row(ASKED_RG, albums.MISSING, "Asked"),
        SPOTIFY_REF: _later_row(None, albums.FALLBACK, "No Group"),
    }
    with state.connect() as conn:
        state.save_albums(conn, state.AlbumRows(albums=rows))
        conn.execute("INSERT INTO album_requests (release_group, tracklist_release, artist, name, data, tracks, "
                     "requested_by) VALUES (%s, %s, 'Band', 'Asked', '{}', '[]', 'sub-tom')", (ASKED_RG, R12))
        for group in (HELD_RG, RG, ASKED_RG):
            conn.execute("INSERT INTO later_queue (release_group) VALUES (%s)", (group,))
        conn.execute("INSERT INTO later_queue (ref) VALUES (%s)", (SPOTIFY_REF,))
        conn.execute("INSERT INTO later_queue (release_group) VALUES (%s)", (OTHER_RG,))
        conn.commit()


def _plan() -> later_requests.Plan:
    with state.connect() as conn:
        return later_requests.plan(conn, state.load_albums(conn))


def _requests() -> dict[str, req.Request]:
    with state.connect() as conn:
        return {r.release_group: r for r in req.load(conn)}


def _events(kind: str) -> list[tuple[str, str, dict]]:
    with state.connect() as conn:
        return [(g, m, d) for g, m, d in conn.execute(
            "SELECT release_group::text, message, data FROM events WHERE kind = %s ORDER BY id", (kind,))]


def test_the_plan_takes_the_incomplete_unrequested_albums_with_a_release_group():
    _setup()
    p = _plan()
    assert [e.release_group for e, _ in p.request] == [RG, OTHER_RG]  # queue order
    assert p.request[0][1][1]["name"] == "Partial" and p.request[1][1] is None  # no album row: a new one
    assert [e.ref for e, _ in p.unresolved] == [SPOTIFY_REF]
    assert p.refused == []


def test_a_tick_requests_them_once_as_auto_later(caplog):
    _setup()
    mb = FakeMB()
    with caplog.at_level(logging.INFO, logger=later_requests.__name__):
        with state.connect() as conn:
            later_requests.tick(state.load_albums(conn), mb, apply=False)  # dry run: counts only
        assert _requests().keys() == {ASKED_RG} and mb.calls == []
        assert "[LATER-REQ] dry run: 2 to request, 1 waiting for a release group" in caplog.text
        with state.connect() as conn:
            later_requests.tick(state.load_albums(conn), mb, apply=True)
    found = _requests()
    assert found[RG].requested_by == later_requests.BY and len(found[RG].tracks) == 12
    assert found[ASKED_RG].requested_by == "sub-tom"  # a hand request is left as it is
    [(group, message, data)] = _events("album_requested")
    assert group == RG and data["requested_by"] == later_requests.BY and "automatic (auto:later)" in message
    # OTHER_RG: MusicBrainz has no such release group, so it is refused and skipped for RETRY.
    [(group, message, data)] = _events("failure")
    assert group == OTHER_RG and data["what"] == later_requests.WHAT and message.startswith("[FAIL] Later: ")
    assert "1 requested automatically, 1 refused by MusicBrainz, 0 left" in caplog.text
    p = _plan()
    assert p.request == [] and [e.release_group for e, _ in p.refused] == [OTHER_RG]
    calls = len(mb.calls)
    with state.connect() as conn:
        later_requests.tick(state.load_albums(conn), mb, apply=True)
    assert len(mb.calls) == calls and len(_events("album_requested")) == 1  # idempotent, no MusicBrainz read


def test_a_musicbrainz_outage_stops_the_ticks_requests_without_a_trace(caplog):
    _setup()

    def down(path: str, **params):
        raise OSError("connection refused")

    with caplog.at_level(logging.INFO, logger=later_requests.__name__), state.connect() as conn:
        later_requests.tick(state.load_albums(conn), down, apply=True)
    assert _requests().keys() == {ASKED_RG} and _events("failure") == []
    assert "stopped: reading release group" in caplog.text
    assert [e.release_group for e, _ in _plan().request] == [RG, OTHER_RG]  # the next tick tries again


def test_a_tick_requests_at_most_per_tick(monkeypatch):
    _setup()
    monkeypatch.setattr(later_requests, "PER_TICK", 1)
    with state.connect() as conn:
        later_requests.tick(state.load_albums(conn), FakeMB(), apply=True)
    assert _requests().keys() == {ASKED_RG, RG} and _events("failure") == []


def test_the_requests_the_tick_adds_are_fed_the_same_tick_and_fall_back_from_musicbrainz():
    """The former later row in fallback (its Spotify fallback dead since the cutover) becomes
    a request's: its tracks the MusicBrainz tracklist, its fallback built from MusicBrainz."""
    _setup()
    with state.connect() as conn:
        rows = state.load_albums(conn)
        later_requests.tick(rows, FakeMB(), apply=True)
    s = albums.State.load()
    albums.feed_requests(s, req.album_requests(), [])
    [record] = [r for r in s.albums.values() if r.get("release_group") == RG]
    assert record["origin"] == albums.REQUEST and record["tracks_count"] == 12
    assert list(record["playlists"]) == [req.SOURCE] and albums._request_fallback(record)
    held = next(r for r in s.albums.values() if r.get("release_group") == HELD_RG)
    assert list(held["playlists"]) == ["later"]  # not requested, and held: untouched


def test_music_later_auto_requests_lists_the_plan_and_writes_nothing(caplog):
    _setup()
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=later_cli.__name__):
        later_cli.run(later_cli._parser().parse_args(["auto-requests"]))
    lines = [r.getMessage() for r in caplog.records if r.name == later_cli.__name__]
    assert lines[0] == f"Request 1 of 2: Band — Partial [release group {RG}]: fallback; this tick"
    assert lines[1] == f"Request 2 of 2: release group {OTHER_RG}: no album row yet; this tick"
    assert lines[2].startswith(f"Waiting: Band — No Group [album ref {SPOTIFY_REF}]: fallback")
    assert lines[-1].startswith("Later: 2 to request, 1 waiting for a release group, 0 refused")
    assert _requests().keys() == {ASKED_RG}


def test_with_the_database_down_the_tick_logs_and_carries_on(monkeypatch, caplog):
    def down(url=None):
        raise state.Unavailable("the music database is unreachable")

    monkeypatch.setattr(state, "connect", down)
    with caplog.at_level(logging.ERROR, logger=later_requests.__name__):
        later_requests.tick(state.AlbumRows(), FakeMB(), apply=True)  # doesn't raise: the album tick goes on
    assert any("next tick tries again" in r.getMessage() for r in caplog.records)


def test_the_entry_held_by_ref_is_counted_from_its_row_status():
    assert later_queue.held({"status": albums.FILLED}) and not later_queue.held({"status": albums.FALLBACK})
