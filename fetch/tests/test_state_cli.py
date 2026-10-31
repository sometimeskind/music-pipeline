"""``music-state``: hand edits of the state (homelab#2503).

The database tests run against a real Postgres when ``MUSIC_TEST_DATABASE_URL``
is set (pg_fixture's ``pg_url``); the rest need none.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from pathlib import Path

import pytest

from music_fetch import albums, ingest, state, state_cli

sys.path.insert(0, str(Path(__file__).parent))  # importable however pytest imports this file
from pg_fixture import pg_url  # noqa: E402, F401 — a fixture

needs_pg = pytest.mark.skipif(not os.environ.get("MUSIC_TEST_DATABASE_URL"),
                              reason="MUSIC_TEST_DATABASE_URL is unset")

FAILURES = {
    "https://open.spotify.com/track/a": {"kind": "auth", "attempts": 1, "retry_after": "2026-11-02T03:00:00+00:00",
                                         "reason": "HTTP Error 403"},
    "https://open.spotify.com/track/b": {"kind": "miss", "attempts": 2, "retry_after": "2026-11-01T03:00:00+00:00",
                                         "reason": "no source found"},
}


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(state, "SWITCH", tmp_path / ".state-in-db")
    monkeypatch.setattr(state_cli, "FAILURES_FILE", tmp_path / ".spotdl-failures.json")


@pytest.fixture
def switched(tmp_path: Path, pg_url):
    (tmp_path / ".state-in-db").touch()
    return pg_url


def _run(argv: list[str], caplog) -> list[str]:
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=state_cli.__name__):
        state_cli.main(argv)
    return [r.getMessage() for r in caplog.records if r.name == state_cli.__name__]


def _events() -> list[tuple]:
    with state.connect() as conn:
        return conn.execute("SELECT kind, source, message, data FROM events ORDER BY id").fetchall()


# --- no database ---


def test_the_backoff_file_is_ingests(monkeypatch):
    monkeypatch.undo()
    assert state_cli.FAILURES_FILE == ingest.FAILURES_FILE


def test_before_the_switch_clear_backoff_deletes_the_file_on_apply_only(caplog):
    path = state_cli.FAILURES_FILE
    path.write_text("{}", encoding="utf-8")
    assert _run(["clear-backoff"], caplog)[-1] == "Dry run; --apply to write"
    assert path.exists()
    assert _run(["clear-backoff", "--apply"], caplog)[-1] == "Applied"
    assert not path.exists()
    assert _run(["clear-backoff", "--apply"], caplog)[-1] == "Nothing to clear"


def test_before_the_switch_the_store_edits_refuse(caplog):
    with pytest.raises(SystemExit):
        _run(["drop-store", "--apply", "--", "mix"], caplog)
    assert "not in the music database yet" in caplog.text


def test_a_write_takes_its_locks_in_order_and_a_dry_run_none(monkeypatch):
    taken: list[str] = []

    @contextlib.contextmanager
    def lock(name):
        taken.append(name)
        yield

    monkeypatch.setattr(albums, "_state_lock", lambda apply: lock("albums"))
    from music_scan import audit
    monkeypatch.setattr(audit, "pipeline_lock", lambda: lock("pipeline"))
    with state_cli._locks(state_cli.LOCKS["drop-store"], apply=False):
        pass
    assert taken == []
    for command in ("drop-store", "clear-snapshot", "clear-backoff"):
        with state_cli._locks(state_cli.LOCKS[command], apply=True):
            pass
    assert taken == ["albums", "pipeline", "albums", "pipeline"]


def test_a_lock_that_times_out_releases_the_one_before_it(monkeypatch, caplog):
    held: list[str] = []

    @contextlib.contextmanager
    def albums_lock():
        held.append("albums")
        try:
            yield
        finally:
            held.remove("albums")

    def busy():
        raise TimeoutError

    monkeypatch.setattr(albums, "_state_lock", lambda apply: albums_lock())
    from music_scan import audit
    monkeypatch.setattr(audit, "pipeline_lock", busy)
    (state.SWITCH).touch()
    with pytest.raises(SystemExit):
        _run(["drop-store", "--apply", "--", "mix"], caplog)
    assert held == []
    assert "nothing was written" in caplog.text


# --- against Postgres ---


@needs_pg
def test_clear_backoff_empties_the_table_with_its_event(switched, caplog):
    with state.connect() as conn:
        state.save_failures(conn, FAILURES)
    lines = _run(["backoff"], caplog)
    assert lines[0] == "https://open.spotify.com/track/a: auth, attempt 1, until 2026-11-02; HTTP Error 403"
    assert lines[-1] == "The backoff holds 2 track(s) (auth 1, miss 1), next retry 2026-11-01"
    dry = _run(["clear-backoff"], caplog)
    assert dry == _run(["clear-backoff"], caplog)
    assert dry[-1] == "Dry run; --apply to write"
    with state.connect() as conn:
        assert len(state.load_failures(conn)) == 2
    assert _run(["clear-backoff", "--apply"], caplog)[-1] == "Applied"
    with state.connect() as conn:
        assert state.load_failures(conn) == {}
    assert _events() == [("state_edited", "state", "[STATE] backoff cleared: 2 track(s)",
                          {"what": "backoff", "tracks": 2, "kinds": {"auth": 1, "miss": 1}})]
    assert _run(["clear-backoff", "--apply"], caplog)[-1] == "Nothing to clear"
    assert len(_events()) == 1


@needs_pg
def test_drop_store_deletes_one_store_and_says_what_is_lost(switched, caplog):
    entries = [{"song": {"song_id": "s1"}}, {"song": {"song_id": "s2"}},
               {"local": {"id": 7, "path": "/x", "title": "T", "artist": "A"}}]
    with state.connect() as conn:
        state.save_store(conn, "mix", entries, "spotify")
        state.save_store(conn, "other", entries[:1])
    lines = _run(["stores"], caplog)
    assert lines[0].startswith("mix: 2 Spotify + 1 local entr(ies), source spotify, updated ")
    assert lines[-1] == "2 playlist store(s)"
    dry = _run(["drop-store", "--", "mix"], caplog)
    assert "Lost: its 1 local entr(ies); a rebuild from Spotify doesn't bring them back" in dry
    assert "Rebuilt: by the next nightly (03:00 UTC), from Spotify" in dry
    with state.connect() as conn:
        assert state.store_names(conn) == ["mix", "other"]
    _run(["drop-store", "--apply", "--", "mix"], caplog)
    with state.connect() as conn:
        assert state.store_names(conn) == ["other"]
    [(kind, _, message, data)] = _events()
    assert (kind, message) == ("state_edited", "[STATE] store mix dropped: 2 Spotify + 1 local entr(ies)")
    assert data["what"] == "store" and data["local"] == 1
    with pytest.raises(SystemExit):
        _run(["drop-store", "--apply", "--", "mix"], caplog)
    assert "no playlist store is named 'mix'" in caplog.text
    assert len(_events()) == 1


@needs_pg
def test_clear_snapshot_makes_the_next_tick_reread_the_playlist(switched, caplog):
    rows = state.AlbumRows(playlists={"albums-a": {"snapshot_id": "snap1", "other": 1}, "albums-b": {}})
    with state.connect() as conn:
        state.save_albums(conn, rows)
        state.save_store(conn, "albums-a", [])
    assert _run(["stores"], caplog)[0].endswith("(album playlist)")
    with pytest.raises(SystemExit):
        _run(["clear-snapshot", "--", "mix"], caplog)
    assert "'mix' is not an album playlist; the album playlists are: albums-a, albums-b" in caplog.text
    dry = _run(["clear-snapshot", "--", "albums-a"], caplog)
    assert dry[0] == "Album playlist albums-a: snapshot snap1"
    assert albums.State.load().playlists["albums-a"]["snapshot_id"] == "snap1"
    _run(["clear-snapshot", "--apply", "--", "albums-a"], caplog)
    assert albums.State.load().playlists["albums-a"] == {"other": 1}
    assert _events()[0][3] == {"what": "snapshot", "playlist": "albums-a", "snapshot_id": "snap1"}
    assert _run(["clear-snapshot", "--apply", "--", "albums-a"], caplog)[-1].startswith("Nothing to clear")
    assert len(_events()) == 1
