"""music-album-audit-releases --keep: one file out of quarantine/replaced/ (homelab#2623)."""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

import psycopg
import pytest

from music_scan import release

# fetch/tests in the repo (CI), /app/fetch_tests in the dev image (just test).
_here = Path(__file__).resolve().parent
sys.path.insert(0, str(next((p for p in (_here.parent.parent / "fetch" / "tests", _here.parent / "fetch_tests")
                             if (p / "pg_fixture.py").exists()), _here)))
from pg_fixture import pg_url  # noqa: E402, F401 — a fixture

needs_pg = pytest.mark.skipif(not os.environ.get("MUSIC_TEST_DATABASE_URL"),
                              reason="MUSIC_TEST_DATABASE_URL is unset")

LIVE = "17052-03-10 - Reason To Believe (Nebraska Live).m4a"


@pytest.fixture
def quarantine(tmp_path):
    replaced = tmp_path / "replaced"
    (replaced / "sub").mkdir(parents=True)
    for name in (LIVE, "17060-01 - Other.m4a", "17060-02 - Other.m4a", "sub/9-x.m4a", "17052-cover.jpg"):
        (replaced / name).write_bytes(b"audio")
    return tmp_path


@pytest.mark.parametrize("name, expected", [
    ("17052", LIVE),
    (LIVE, LIVE),
    (f"replaced/{LIVE}", LIVE),  # as list_quarantine shows it
    ("sub/9-x.m4a", "sub/9-x.m4a"),
])
def test_keep_target_names_one_file(quarantine, name, expected):
    replaced = quarantine / "replaced"
    assert release.keep_target(name, replaced) == replaced / expected


@pytest.mark.parametrize("name, error", [
    ("17060", "2 files have a file name starting with 17060-"),
    ("1705", "no audio file"),  # a prefix of an ID is not the ID
    ("17052-cover.jpg", "no audio file"),
    ("../replaced/" + LIVE, "must be a path inside quarantine/replaced/"),
    ("/etc/passwd", "must be a path inside quarantine/replaced/"),
    ("  ", "name a file"),
])
def test_keep_target_refuses_none_or_several(quarantine, name, error):
    with pytest.raises(SystemExit, match=error.replace("(", r"\(")):
        release.keep_target(name, quarantine / "replaced")


def test_a_dry_run_moves_nothing(quarantine, caplog):
    caplog.set_level("INFO")
    assert release.keep_replaced("17052", quarantine / "replaced", quarantine / "keep") is None
    assert (quarantine / "replaced" / LIVE).exists() and not (quarantine / "keep").exists()
    assert f"[KEEP] replaced/{LIVE} (tags unreadable, 0.0 MB) → keep/{LIVE}" in caplog.text
    assert "Dry run — nothing moved" in caplog.text


def test_keep_refuses_a_file_kept_already(quarantine):
    (quarantine / "keep").mkdir()
    (quarantine / "keep" / LIVE).write_bytes(b"older")
    with pytest.raises(SystemExit, match="exists already"):
        release.keep_replaced("17052", quarantine / "replaced", quarantine / "keep")


def _events(url):
    with psycopg.connect(url) as conn:
        return conn.execute("SELECT kind, source, item_id, message, data FROM events ORDER BY id").fetchall()


@needs_pg
def test_keep_moves_the_file_with_its_event(quarantine, pg_url, caplog):
    from music_fetch import db

    caplog.set_level("INFO")
    replaced, keep = quarantine / "replaced", quarantine / "keep"
    with contextlib.closing(db.connect()) as conn:
        assert release.keep_replaced(LIVE, replaced, keep, conn) == keep / LIVE
    assert (keep / LIVE).read_bytes() == b"audio" and not (replaced / LIVE).exists()
    [(kind, source, item_id, message, data)] = _events(pg_url)
    assert (kind, source, item_id) == ("kept", "release", 17052)
    assert message == f"[KEEP] replaced/{LIVE} → keep/{LIVE}: kept by hand, out of the purge's reach"
    assert data == {"path": str(replaced / LIVE), "dest": str(keep / LIVE)}


@needs_pg
def test_a_failed_move_records_nothing(quarantine, pg_url, monkeypatch):
    from music_fetch import db

    def fail(*_):
        raise OSError("read-only file system")

    monkeypatch.setattr(release.shutil, "move", fail)
    with contextlib.closing(db.connect()) as conn, pytest.raises(OSError):
        release.keep_replaced("17052", quarantine / "replaced", quarantine / "keep", conn)
    assert _events(pg_url) == [] and (quarantine / "replaced" / LIVE).exists()
