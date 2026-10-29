"""Search overrides, holds and track pins in the music database (homelab#2484):
the ConfigMap files' parsing, keying by album row, re-attaching by ref after a
merge or a deletion, the one-shot import, and music-overrides' dry runs."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

from music_fetch import albums, overrides, overrides_cli, spotdl_ops, state, state_import, store
from music_fetch.overrides import HOLD, SEARCH, AlbumOverride

sys.path.insert(0, str(Path(__file__).parent))  # importable however pytest imports this file
from pg_fixture import pg_url  # noqa: E402, F401 — a fixture

needs_pg = pytest.mark.skipif(not os.environ.get("MUSIC_TEST_DATABASE_URL"),
                              reason="MUSIC_TEST_DATABASE_URL is unset")

RG = "6e0a6c2a-2d38-3d3c-9d38-4f0c2b5b7a11"
ALBUMS_CONF = """\
# Album-mode search overrides (music-pipeline#197) — one entry per line.
# Format: <spotify album id>  <search words…>

6OxEdoTceemiC0d7GgdXEz  wingdings four tet

# Talking Heads — Stop Making Sense (Live): the words are also the edition.
4FR8Z6TvIsC56NLyNomNRE  talking heads stop making sense remastered deluxe

# Holds (music-pipeline#288): `hold <spotify album id>`.
hold 3SpeakingInTongues00000  # curated mix
"""
PINS_CONF = """\
# Source URLs for tracks spotdl's search can't find.
# Format: <spotify track id>  <source url>  [label…]

# Wingdings / Four Tet: glyph-only titles, never found.
68ozhSrI4eLbnUgih1SlOT  https://music.youtube.com/watch?v=29Sh3yEaBP8&si=abc  track 1
https://open.spotify.com/track/66i52Sbv3xAYXv5IlGbko6?si=x  https://music.youtube.com/watch?v=aVqJbrkkdYk  track 2

5sKY9vib0WClIKDr0qkaWH  https://music.youtube.com/watch?v=yxh6HEkWtvI
not-a-pin
58DVO497p71nqDTNOm7ZYA  https://evil.example/watch?v=njXRPPotTPo
"""


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(albums, "OVERRIDES_FILE", tmp_path / "album-overrides.conf")
    monkeypatch.setattr(spotdl_ops, "PINS_FILE", tmp_path / "track-pins.conf")
    monkeypatch.setattr(state, "SWITCH", tmp_path / ".state-in-db")
    monkeypatch.setattr(overrides, "LIBRARY_DB", tmp_path / "library.db")
    (tmp_path / "album-overrides.conf").write_text(ALBUMS_CONF, encoding="utf-8")
    (tmp_path / "track-pins.conf").write_text(PINS_CONF, encoding="utf-8")


@pytest.fixture
def switched(tmp_path: Path, pg_url):
    (tmp_path / ".state-in-db").touch()
    return pg_url


def _record(key: str, name: str = "Album", **fields) -> dict:
    return state.new_record(key, status=albums.WANTED, blocklist=[], name=name, artist="Artist", **fields)


# --- the files ---


def test_album_entries_take_the_comment_block_above_them_as_their_reason(tmp_path: Path):
    parsed = overrides.parse_albums(tmp_path / "album-overrides.conf")
    assert [(o.kind, o.refs, o.words, o.reason) for o in parsed] == [
        (SEARCH, ["6OxEdoTceemiC0d7GgdXEz"], "wingdings four tet", ""),  # the header is nobody's
        (SEARCH, ["4FR8Z6TvIsC56NLyNomNRE"], "talking heads stop making sense remastered deluxe",
         "Talking Heads — Stop Making Sense (Live): the words are also the edition."),
        (HOLD, ["3SpeakingInTongues00000"], None,
         "Holds (music-pipeline#288): `hold <spotify album id>`.; curated mix"),
    ]
    # The legacy readers, by Spotify album ID.
    assert albums.load_overrides()["6OxEdoTceemiC0d7GgdXEz"] == "wingdings four tet"
    assert albums.load_holds() == {"3SpeakingInTongues00000"}


def test_a_pin_block_covers_the_run_of_entries_below_it(tmp_path: Path, caplog):
    parsed = overrides.parse_pins(tmp_path / "track-pins.conf")
    assert [(p.spotify_track_id, p.url, p.label, p.reason) for p in parsed] == [
        ("68ozhSrI4eLbnUgih1SlOT", "https://music.youtube.com/watch?v=29Sh3yEaBP8", "track 1",
         "Wingdings / Four Tet: glyph-only titles, never found."),
        ("66i52Sbv3xAYXv5IlGbko6", "https://music.youtube.com/watch?v=aVqJbrkkdYk", "track 2",
         "Wingdings / Four Tet: glyph-only titles, never found."),
        ("5sKY9vib0WClIKDr0qkaWH", "https://music.youtube.com/watch?v=yxh6HEkWtvI", "", ""),
    ]
    assert "ignoring 'not-a-pin'" in caplog.text
    assert "ignoring '58DVO497p71nqDTNOm7ZYA https://evil.example" in caplog.text  # yt-dlp fetches YouTube only
    assert overrides.pin_source("https://youtu.be/njXRPPotTPo?si=x") == "https://www.youtube.com/watch?v=njXRPPotTPo"
    with pytest.raises(ValueError):
        overrides.pin_source("https://music.youtube.com/playlist?list=OLAK5uy_l7iC3zWrsZbNvuTSvO")
    assert spotdl_ops.load_pins()["https://open.spotify.com/track/66i52Sbv3xAYXv5IlGbko6"].endswith("aVqJbrkkdYk")


def test_keyed_finds_a_record_by_row_then_by_ref():
    rows = state.AlbumRows(albums={
        "sp1": {**_record("sp1"), "_id": 7, "spotify_album_ids": ["sp1", "sp2"]},
        "Local/Album": {**_record("Local/Album"), "_id": 8},
        RG: {**state.new_record(RG, status=albums.WANTED), "release_group": RG, "unresolved": None,
             "spotify_album_ids": [], "names": []},
    })
    search, holds = overrides.keyed([
        AlbumOverride(SEARCH, ["sp2"], "merged-away words", album_id=99),  # sp1's own one wins
        AlbumOverride(SEARCH, ["gone"], "row words", album_id=7),
        AlbumOverride(HOLD, ["Local/Album"]),
        AlbumOverride(HOLD, [RG]),
        AlbumOverride(HOLD, ["nobody"]),
    ], rows)
    assert search == {"sp1": "row words"}
    assert holds == {"Local/Album", RG}


# --- against Postgres ---


def _save(rows: state.AlbumRows) -> None:
    with state.connect() as conn:
        state.save_albums(conn, rows)


def _override_rows(conn) -> list[tuple]:
    return conn.execute("SELECT kind, album_id, refs, words FROM album_overrides ORDER BY id").fetchall()


@needs_pg
def test_a_write_records_its_event_in_the_same_transaction(switched, monkeypatch):
    monkeypatch.setenv("MUSIC_EVENT_CALLER", "someone")
    rows = state.AlbumRows(albums={"sp1": _record("sp1")})
    _save(rows)
    record = rows.albums["sp1"]
    with state.connect() as conn:
        overrides.set_album(conn, SEARCH, "sp1", record, words="artist album words", reason="glyphs")
        overrides.set_album(conn, SEARCH, "sp1", record, words="other words")
        assert _override_rows(conn) == [(SEARCH, record["_id"], ["sp1"], "other words")]
        assert overrides.clear_album(conn, SEARCH, "sp1", record)
        assert not overrides.clear_album(conn, SEARCH, "sp1", record)
        got = conn.execute("SELECT kind, caller, spotify_album_id, data->>'kind', data->>'words' FROM events "
                           "ORDER BY id").fetchall()
    assert got == [("override_set", "someone", "sp1", SEARCH, "artist album words"),
                   ("override_set", "someone", "sp1", SEARCH, "other words"),
                   ("override_cleared", "someone", "sp1", SEARCH, "other words")]


@needs_pg
def test_an_override_follows_a_merge_and_an_album_that_comes_back(switched):
    rows = state.AlbumRows(albums={"sp1": _record("sp1"), "sp2": _record("sp2", name="Album (Deluxe)")})
    _save(rows)
    with state.connect() as conn:
        overrides.set_album(conn, HOLD, "sp2", rows.albums["sp2"], reason="mixed on purpose")
        overrides.set_album(conn, SEARCH, "sp2", rows.albums["sp2"], words="loser words")
        overrides.set_album(conn, SEARCH, "sp1", rows.albums["sp1"], words="survivor words")
    # sp2 merges into sp1: its hold moves, its search override clashes with sp1's and stays detached.
    survivor = rows.albums["sp1"]
    survivor["spotify_album_ids"] = ["sp1", "sp2"]
    del rows.albums["sp2"]
    _save(rows)
    with state.connect() as conn:
        assert _override_rows(conn) == [(HOLD, survivor["_id"], ["sp1", "sp2"], None),
                                        (SEARCH, None, ["sp2"], "loser words"),
                                        (SEARCH, survivor["_id"], ["sp1", "sp2"], "survivor words")]
        listed = overrides.listing(conn, state.load_albums(conn), SEARCH)
    assert [(e["detached"], e["words"]) for e in listed] == [(True, "loser words"), (False, "survivor words")]
    # The album leaves its playlist, then comes back as a new row: the hold finds it by ref.
    del rows.albums["sp1"]
    _save(rows)
    rows.albums["sp2"] = _record("sp2")
    _save(rows)
    with state.connect() as conn:
        assert _override_rows(conn)[0] == (HOLD, rows.albums["sp2"]["_id"], ["sp2"], None)
        # The detached search override takes the new row too, now that it has none.
        assert _override_rows(conn)[1] == (SEARCH, rows.albums["sp2"]["_id"], ["sp2"], "loser words")


@needs_pg
def test_the_import_resolves_through_the_refs_and_keeps_an_entry_with_no_row(switched, tmp_path: Path):
    rows = state.AlbumRows(albums={
        "4FR8Z6TvIsC56NLyNomNRE": {**_record("4FR8Z6TvIsC56NLyNomNRE"),
                                   "spotify_album_ids": ["4FR8Z6TvIsC56NLyNomNRE", "otherEdition"]},
        "3SpeakingInTongues00000": _record("3SpeakingInTongues00000"),
    })
    _save(rows)
    with state.connect() as conn:
        state.save_store(conn, "wingdings", [{"song": {"song_id": "68ozhSrI4eLbnUgih1SlOT", "name": "〠",
                                                       "artist": "Four Tet", "isrc": "GBCFB2400001"}}])
        assert overrides.album_overrides(tmp_path / "album-overrides.conf")[0].album_id is None  # the file's
        out_albums, out_pins, report = overrides_cli.import_plan(
            conn, state.load_albums(conn), tmp_path / "album-overrides.conf", tmp_path / "track-pins.conf")
        assert any(line.startswith("[NOROW] search 6OxEdoTceemiC0d7GgdXEz") for line in report)
        assert (len(out_albums), len(out_pins)) == (3, 3)
        overrides_cli.import_write(conn, out_albums, out_pins)
        assert overrides.imported(conn)
        assert [(k, a, w) for k, a, _, w in _override_rows(conn)] == [
            (SEARCH, None, "wingdings four tet"),
            (SEARCH, rows.albums["4FR8Z6TvIsC56NLyNomNRE"]["_id"], "talking heads stop making sense remastered deluxe"),
            (HOLD, rows.albums["3SpeakingInTongues00000"]["_id"], None)]
        assert conn.execute("SELECT isrc FROM track_pins WHERE spotify_track_id = '68ozhSrI4eLbnUgih1SlOT'"
                            ).fetchone() == ("GBCFB2400001",)
        assert conn.execute("SELECT count(*) FROM events WHERE kind = 'override_set'").fetchone() == (6,)
        # Run again: everything is there already.
        out_albums, out_pins, report = overrides_cli.import_plan(
            conn, state.load_albums(conn), tmp_path / "album-overrides.conf", tmp_path / "track-pins.conf")
        assert (out_albums, out_pins) == ([], [])
        assert all(line.startswith("[SKIP]") for line in report)
    # From now on the tables are read, and the files ignored.
    (tmp_path / "album-overrides.conf").write_text("", encoding="utf-8")
    (tmp_path / "track-pins.conf").write_text("", encoding="utf-8")
    fixes = albums.album_fixes()
    assert len(fixes) == 3
    assert overrides.keyed(fixes, albums.State.load())[1] == {"3SpeakingInTongues00000"}
    assert len(spotdl_ops.load_pins()) == 3
    # The detached override attaches once an album row carries its Spotify ID.
    rows = state.AlbumRows(albums={"6OxEdoTceemiC0d7GgdXEz": _record("6OxEdoTceemiC0d7GgdXEz")})
    _save(rows)
    search, _ = overrides.keyed(albums.album_fixes(), albums.State.load())
    assert search["6OxEdoTceemiC0d7GgdXEz"] == "wingdings four tet"


@needs_pg
def test_a_state_reimport_keeps_the_overrides_and_their_marker(switched):
    rows = state.AlbumRows(albums={"sp1": _record("sp1")})
    _save(rows)
    with state.connect() as conn:
        overrides.set_album(conn, HOLD, "sp1", rows.albums["sp1"], reason="kept")
        state.put(conn, state.OVERRIDES_IMPORTED, {"at": "then"})
        plan = state_import.Plan(rows=state.AlbumRows(albums={"sp1": _record("sp1")}), stores={}, failures={},
                                 kv={}, report=state_import.resolve.Report())
        state_import.write(conn, plan, replace=True)
        assert overrides.imported(conn)
        new_id = state.load_albums(conn).albums["sp1"]["_id"]
        assert _override_rows(conn) == [(HOLD, new_id, ["sp1"], None)]


@needs_pg
def test_the_cli_refuses_a_write_before_the_import_and_dry_runs_repeat(switched, caplog):
    rows = state.AlbumRows(albums={"6OxEdoTceemiC0d7GgdXEz": {**_record("6OxEdoTceemiC0d7GgdXEz", name="〠"),
                                                              "status": albums.MISSING, "query": "artist",
                                                              "searched_at": "2026-10-28T00:00:00+00:00"}})
    _save(rows)
    with pytest.raises(SystemExit):
        overrides_cli.main(["search", "--", "6OxEdoTceemiC0d7GgdXEz", "wingdings", "four", "tet"])
    assert "not imported yet" in caplog.text
    with state.connect() as conn:
        state.put(conn, state.OVERRIDES_IMPORTED, {"at": "now"})

    def dry_run() -> list[str]:
        caplog.clear()
        with caplog.at_level(logging.INFO):
            overrides_cli.main(["search", "--reason=glyphs", "--", "Artist — 〠", "Wingdings Four Tet"])
        return [r.getMessage() for r in caplog.records]

    first = dry_run()
    assert first == dry_run()
    assert first[1] == 'Search override: none → "wingdings four tet" (glyphs)'
    assert first[2] == 'Next search: "artist" (artist-only) → "wingdings four tet" (override)'
    assert first[3] == 'Searched again: at the next tick (missing, last searched for "artist")'
    overrides_cli.main(["search", "--apply", "--reason=glyphs", "--", "6OxEdoTceemiC0d7GgdXEz", "wingdings four tet"])
    search, _ = overrides.keyed(albums.album_fixes(), albums.State.load())
    assert search == {"6OxEdoTceemiC0d7GgdXEz": "wingdings four tet"}
