"""The event store (homelab#2413).

The best-effort tests need no database.  The rest run against a real Postgres
when ``MUSIC_TEST_DATABASE_URL`` names one whose role may create databases;
each test gets a fresh database, dropped afterwards.  Locally:

    podman run -d --rm --name music-test-pg -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:17
    MUSIC_TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres pytest fetch/tests/test_events.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import psycopg
import pytest

from music_fetch import db, events

sys.path.insert(0, str(Path(__file__).parent))  # importable however pytest imports this file
from pg_fixture import pg_url  # noqa: E402, F401 — a fixture

TEST_URL = os.environ.get("MUSIC_TEST_DATABASE_URL")
needs_pg = pytest.mark.skipif(not TEST_URL, reason="MUSIC_TEST_DATABASE_URL is unset")

RG = "6e0a6c2a-2d38-3d3c-9d38-4f0c2b5b7a11"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    for var in (db.ENV, "MUSIC_EVENT_CALLER", "MUSIC_EVENT_JOB"):
        monkeypatch.delenv(var, raising=False)
    pushes = []
    monkeypatch.setattr(events.metrics, "_push", lambda body, job: pushes.append((body, job)))
    events._reset()
    yield pushes
    events._reset()


def _rows(url, sql="SELECT kind, source, spotify_album_id, release_group::text, item_id, caller, job_id, "
                   "message, data FROM events ORDER BY id"):
    with psycopg.connect(url) as conn:
        return conn.execute(sql).fetchall()


# --- best-effort, no database ---


def test_unset_url_records_nothing_and_is_no_failure(_fresh, caplog):
    caplog.set_level("INFO")
    assert events.record("grab", "a", source="albums") is False
    assert events.record("grab", "b", source="albums") is False
    assert events.failures == 0
    assert _fresh == []
    assert sum("events are not recorded" in r.message for r in caplog.records) == 1


def test_unknown_kind_is_a_failure_not_an_exception(_fresh, monkeypatch):
    monkeypatch.setenv(db.ENV, "postgresql://nowhere.invalid/music")
    assert events.record("imported", "x", source="albums") is False
    assert events.failures == 1
    assert [job for _, job in _fresh] == ["music_events"]


def test_database_down_backs_off_and_stamps_once(_fresh, monkeypatch, caplog):
    monkeypatch.setenv(db.ENV, "postgresql://nowhere.invalid/music")
    calls = []

    def refuse(url=None):
        calls.append(url)
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(db, "connect", refuse)
    assert events.record("grab", "one", source="albums") is False
    assert events.record("grab", "two", source="albums") is False
    # One connection attempt per RETRY_SECONDS; the second event is still lost, and counted.
    assert len(calls) == 1
    assert events.failures == 2
    body, job = _fresh[0]
    assert job == "music_events" and events.FAILURE_METRIC in body
    assert len(_fresh) == 1  # throttled to one push per PUSH_SECONDS
    lost = [r.message for r in caplog.records if "not recorded" in r.message]
    assert "connection refused" in lost[0] and "database unavailable" in lost[1]


def test_retry_after_backoff(_fresh, monkeypatch):
    monkeypatch.setenv(db.ENV, "postgresql://nowhere.invalid/music")
    calls = []

    def refuse(url=None):
        calls.append(url)
        raise psycopg.OperationalError("down")

    monkeypatch.setattr(db, "connect", refuse)
    clock = [1000.0]
    monkeypatch.setattr(events.time, "monotonic", lambda: clock[0])
    events.record("grab", "one", source="albums")
    clock[0] += events.RETRY_SECONDS + 1
    events.record("grab", "two", source="albums")
    assert len(calls) == 2
    assert len(_fresh) == 2


@pytest.mark.parametrize("value, expected", [
    ("", None),
    (None, None),
    (RG, RG),
    (RG.upper(), RG),
    ("not-an-mbid", None),
])
def test_release_group_id(value, expected):
    assert events.release_group_id(value) == expected


@pytest.mark.parametrize(("key", "expected"), [
    ("0ETFjACtuP2ADo6LFhL6HN", "0ETFjACtuP2ADo6LFhL6HN"),
    ("Artist/Album", None),
    ("", None),
    (None, None),
])
def test_spotify_album(key, expected):
    assert events.spotify_album(key) == expected


def test_item_fields_decodes_a_bytes_path():
    item = {"artist": "A", "title": "T", "album": "Al", "path": b"/music/A/Al/01 T.m4a"}
    assert events.item_fields(item) == {"artist": "A", "title": "T", "album": "Al", "path": "/music/A/Al/01 T.m4a"}


def test_every_kind_is_in_a_migration():
    allowed = set(re.findall(r"'([a-z_]+)'", "".join(db.MIGRATIONS)))
    assert set(db.EVENT_KINDS) <= allowed


# --- against Postgres ---


@needs_pg
def test_migrate_is_idempotent(pg_url):
    with psycopg.connect(pg_url, autocommit=True) as conn:
        assert db.migrate(conn) == len(db.MIGRATIONS)
        assert db.migrate(conn) == len(db.MIGRATIONS)
        versions = [v for (v,) in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
    assert versions == list(range(1, len(db.MIGRATIONS) + 1))


@needs_pg
def test_an_image_before_migration_12_runs_on_schema_12(pg_url, monkeypatch, caplog):
    """The rollback path for homelab#2553: an image that knows 11 migrations
    connects to a database at 12, migrates nothing, and records and prunes
    events as before, ignoring long_runs and recheck_findings."""
    with psycopg.connect(pg_url, autocommit=True) as conn:
        assert db.migrate(conn) == 12
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:11])
    with db.connect(pg_url) as conn:
        assert db.migrate(conn) == 12
        events.insert(conn, "recording_replaced", "[RECHECK] 1 — T: mb_trackid a → b", source="mb-fingerprint",
                      item_id=1)
        assert events.prune(conn) == 0
    assert "music database is at schema 12, this image knows 11" in caplog.text
    with psycopg.connect(pg_url) as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone() == (1,)
        assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone() == (12,)


@needs_pg
def test_a_newer_schema_is_left_alone(pg_url):
    with psycopg.connect(pg_url, autocommit=True) as conn:
        db.migrate(conn)
        conn.execute("INSERT INTO schema_migrations (version) VALUES (99)")
        assert db.migrate(conn) == 99


@needs_pg
def test_kind_check_matches_event_kinds(pg_url):
    db.connect().close()
    (definition,) = _rows(pg_url, "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                                  "WHERE conname = 'events_kind_check'")[0]
    assert set(re.findall(r"'([a-z_]+)'", definition)) == set(db.EVENT_KINDS)


@needs_pg
def test_record_writes_a_row(pg_url, monkeypatch):
    monkeypatch.setenv("MUSIC_EVENT_CALLER", "pilot")
    monkeypatch.setenv("MUSIC_EVENT_JOB", "95872a84f134437e")
    assert events.record("import_done", "[DONE] Artist — Album", source="albums",
                         spotify_album_id="0ETFjACtuP2ADo6LFhL6HN", release_group=RG.upper(),
                         data={"tracks": 10})
    assert events.record("retire", "[RETIRE] 12 Artist — Song", source="scan", item_id=12)
    assert _rows(pg_url) == [
        ("import_done", "albums", "0ETFjACtuP2ADo6LFhL6HN", RG, None, "pilot", "95872a84f134437e",
         "[DONE] Artist — Album", {"tracks": 10}),
        ("retire", "scan", None, None, 12, "pilot", "95872a84f134437e", "[RETIRE] 12 Artist — Song", {}),
    ]
    assert events.failures == 0


@needs_pg
def test_reconnects_after_the_connection_breaks(pg_url):
    assert events.record("grab", "one", source="albums")
    events._conn.close()
    assert events.record("grab", "two", source="albums")
    assert len(_rows(pg_url)) == 2


@needs_pg
def test_prune_keeps_recent_events_and_each_albums_latest(pg_url):
    db.connect().close()
    with psycopg.connect(pg_url, autocommit=True) as conn:
        insert = ("INSERT INTO events (at, kind, source, spotify_album_id, release_group, message) "
                  "VALUES (now() - make_interval(days => %s), 'grab', 'albums', %s, %s, %s)")
        for i in range(25):  # album A: 25 old events, keyed by Spotify ID
            conn.execute(insert, (100 + i, "A" * 22, None, f"a{i}"))
        for i in range(3):  # album B: 3 old events under its release group
            conn.execute(insert, (200 + i, "B" * 22, RG, f"b{i}"))
        conn.execute(insert, (100, None, None, "old, no album"))
        conn.execute(insert, (1, None, None, "recent, no album"))
        conn.execute(insert, (1, "A" * 22, None, "a recent"))

        deleted = events.prune(conn, keep=20)
        left = [m for (m,) in conn.execute("SELECT message FROM events ORDER BY id")]
    # A keeps its latest 20 (the recent one and a0..a18); B keeps all 3; the old album-less one goes.
    assert deleted == 7
    assert left == [f"a{i}" for i in range(19)] + ["b0", "b1", "b2", "recent, no album", "a recent"]


@needs_pg
def test_explicit_caller_and_job_win_over_the_environment(pg_url, monkeypatch):
    monkeypatch.setenv("MUSIC_EVENT_CALLER", "env")
    assert events.record("mcp_job", "end", source="mcp", caller="pilot", job_id="j1")
    assert [(c, j) for *_, c, j, _, _ in _rows(pg_url)] == [("pilot", "j1")]


@needs_pg
def test_once_hours_records_a_standing_condition_once(pg_url):
    for _ in range(3):
        assert events.record("clash", "[CLASH] a → b", source="canon", item_id=7, release_group=RG,
                             data={"dest": "b"}, once_hours=24)
    assert events.record("clash", "[CLASH] c → d", source="canon", once_hours=24)
    rows = _rows(pg_url)
    assert [r[7] for r in rows] == ["[CLASH] a → b", "[CLASH] c → d"]
    assert rows[0][3] == RG and rows[0][4] == 7 and rows[0][8] == {"dest": "b"}
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("UPDATE events SET at = now() - interval '25 hours'")
    assert events.record("clash", "[CLASH] a → b", source="canon", once_hours=24)
    assert len(_rows(pg_url)) == 3
