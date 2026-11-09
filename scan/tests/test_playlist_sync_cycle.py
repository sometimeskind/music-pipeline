"""The two-way playlist sync's dry-run cycle (homelab#2361): coverage, the
inputs it hands the merge core, its skips and end checks, the snapshot it
takes once and never advances, orphaned snapshots, and its report."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

from music_fetch import config, events, state, store
from music_scan import metrics, navidrome, playlist_sync_cycle as cycle
from music_scan.playlist_sync import Entry

URL = "http://navidrome.example.com"
HEADERS = {"X-ND-Authorization": "Bearer jwt"}
# Navidrome's updatedAt for an imported playlist: after any .m3u the tests write.
LATER = "2099-01-01T00:00:00.123456789Z"
CONF = """\
pl      https://open.spotify.com/playlist/ppppppppppppppppppppp1
other   https://open.spotify.com/playlist/ppppppppppppppppppppp2
frozen  https://open.spotify.com/playlist/ppppppppppppppppppppp3  nosync
locked  https://open.spotify.com/playlist/ppppppppppppppppppppp4  oneway
"""


def _item(title, path, item_id, **flex):
    it = mock.MagicMock(title=title, artist="Artist", albumartist="Artist")
    it.id = item_id
    it.path = str(path).encode()
    it.get.side_effect = lambda k, d=None: flex.get(k, d)
    return it


def _song(name, song_id):
    return {"name": name, "artists": ["Artist"], "url": f"https://open.spotify.com/track/{song_id}",
            "song_id": song_id}


def _row(rel, song, missing=False):
    return {"id": "1", "mediaFileId": song, "playlistId": "p", "libraryPath": "/music", "path": rel,
            "title": "T", "artist": "Artist", "missing": missing}


def _pls(pid, name, sync=True, updated=LATER, owner="u"):
    return {"id": pid, "name": name, "path": f"/music/playlists/{name}.m3u", "sync": sync, "updatedAt": updated,
            "ownerName": owner}


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """A library of three items under ``tmp_path/library``; ``playlists.conf``
    with ``pl`` and ``other`` covered and ``frozen``/``locked`` not; store
    ``pl`` lists one and two by Spotify ID, then local entry three; Navidrome
    idle, holding ``pl`` as imported from pl.m3u with all three tracks."""
    lib_root = tmp_path / "library"
    items = [_item("One", lib_root / "one.m4a", 1, spotify_ids="S1"),
             _item("Two", lib_root / "two.m4a", 2, spotify_ids="S2"),
             _item("Three", lib_root / "three.m4a", 3)]
    lib = mock.MagicMock()
    lib.__enter__ = mock.MagicMock(return_value=lib)
    lib.__exit__ = mock.MagicMock(return_value=False)
    lib.all_items.return_value = items
    monkeypatch.setattr("music_scan.scan.LIBRARY", lib_root)
    monkeypatch.setattr("music_scan.scan.PLAYLISTS", tmp_path / "playlists")
    monkeypatch.setattr("music_scan.scan.SPOTDL_DIR", tmp_path / "spotdl")
    monkeypatch.setattr("music_scan.scan._album_records", lambda: {})
    monkeypatch.setattr(cycle, "MusicLibrary", mock.MagicMock(return_value=lib))
    (tmp_path / "playlists").mkdir()
    conf = tmp_path / "playlists.conf"
    conf.write_text(CONF)
    real_load = config.load_playlists
    monkeypatch.setattr(config, "load_playlists", lambda path=conf: real_load(path))
    for var, value in (("NAVIDROME_URL", URL), ("NAVIDROME_USER", "u"), ("NAVIDROME_PASSWORD", "pw")):
        monkeypatch.setenv(var, value)
    for var in ("NAVIDROME_PLAYLISTS_DIR", "NAVIDROME_MUSIC_DIR", cycle.PLAYLISTS_ENV):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(cycle.MODE_ENV, "dry-run")

    pl = store.Store.from_songs("pl", [_song("One", "S1"), _song("Two", "S2")])
    pl.entries.append({store.LOCAL: {"id": 3, "path": "x", "title": "Three", "artist": "Artist"}})
    store.save(pl)
    (tmp_path / "playlists" / "pl.m3u").write_text("one\n")

    nd = mock.MagicMock()
    nd.tmp = tmp_path
    nd.items = items  # the library: a test may add to it
    nd.lib_root = lib_root
    nd.scanning.return_value = False
    nd.list_playlists.return_value = [_pls("imp", "pl")]
    nd.tracks = {"imp": [_row("library/one.m4a", "s1"), _row("library/two.m4a", "s2"),
                         _row("library/three.m4a", "s3")]}
    nd.playlist_tracks.side_effect = lambda url, headers, pid: nd.tracks[pid]
    nd.pushed = {}
    monkeypatch.setattr(navidrome, "scanning", nd.scanning)
    monkeypatch.setattr(navidrome, "_login", lambda *a: HEADERS)
    monkeypatch.setattr(navidrome, "list_playlists", nd.list_playlists)
    monkeypatch.setattr(navidrome, "playlist_tracks", nd.playlist_tracks)
    monkeypatch.setattr(metrics, "_push", lambda body, job: nd.pushed.__setitem__(job, body))
    # A dry run writes no store and records no event.
    monkeypatch.setattr(store, "save", mock.Mock(side_effect=AssertionError("the dry run saved a store")))
    monkeypatch.setattr(events, "record", mock.Mock(side_effect=AssertionError("the dry run recorded an event")))
    return nd


def _free():
    return True


def _snapshots():
    with state.session() as conn:
        return state.load_snapshots(conn)


def _seed(name, held):
    with state.session() as conn:
        state.save_snapshot(conn, name, held)


def _series(body):
    return [line for line in body.splitlines() if not line.startswith("#")]


# --- pure parts -----------------------------------------------------------------------------


def test_coverage_leaves_out_oneway_nosync_and_album():
    conf = [config.PlaylistConfig("a", "u"), config.PlaylistConfig("b", "u", nosync=True),
            config.PlaylistConfig("c", "u", oneway=True), config.PlaylistConfig("d", "u", album=True),
            config.PlaylistConfig("e", "u", twoway=True)]
    covered, excluded = cycle.coverage(conf)
    assert covered == ["a", "e"]
    assert excluded == {"b": "flagged nosync", "c": "flagged oneway", "d": "flagged album"}


def test_coverage_narrowed_keeps_conf_order_and_excludes_nothing_more():
    conf = [config.PlaylistConfig(n, "u") for n in ("a", "b", "c")]
    assert cycle.coverage(conf, {"c", "a", "zzz"}) == (["a", "c"], {})


@pytest.mark.parametrize("value, expected", [
    ("2026-11-09T03:41:07.987654321Z", datetime(2026, 11, 9, 3, 41, 7, 987654, tzinfo=timezone.utc)),
    ("2026-11-09T03:41:07Z", datetime(2026, 11, 9, 3, 41, 7, tzinfo=timezone.utc)),
    ("2026-11-09T04:41:07.5+01:00", datetime(2026, 11, 9, 3, 41, 7, 500000, tzinfo=timezone.utc)),
])
def test_parse_time_reads_navidrome_times(value, expected):
    assert cycle.parse_time(value) == expected


@pytest.mark.parametrize("value", ["", "yesterday", "2026-11-09T03:41:07"])
def test_parse_time_refuses_anything_else(value):
    with pytest.raises(ValueError):
        cycle.parse_time(value)


def test_mode(monkeypatch):
    monkeypatch.delenv(cycle.MODE_ENV, raising=False)
    assert cycle.mode() == "off"
    monkeypatch.setenv(cycle.MODE_ENV, " Dry-Run ")
    assert cycle.mode() == "dry-run"
    monkeypatch.setenv(cycle.MODE_ENV, "on")
    with pytest.raises(ValueError, match="PLAYLIST_SYNC='on'"):
        cycle.mode()


def test_snapshot_state_round_trip():
    _seed("pl", [(1, "s1"), (2, None)])
    _seed("empty", [])
    assert _snapshots() == {"empty": [], "pl": [(1, "s1"), (2, None)]}
    _seed("pl", [(3, "s3")])
    assert _snapshots()["pl"] == [(3, "s3")]
    with state.session() as conn:
        state.drop_snapshots(conn, ["pl"])
        assert conn.execute("SELECT count(*) FROM playlist_sync_snapshot").fetchone()[0] == 0
    assert _snapshots() == {"empty": []}


# --- the inputs -----------------------------------------------------------------------------


def test_entries_come_from_the_store_view_slots_in_store_order(env):
    """Each Spotify entry's item is its store_view slot's; a local entry keeps
    its place; a URL-only entry is unplayable; a local item that left the
    library resolves to nothing."""
    from music_scan import playlists as pls_mod, scan
    from music_scan.identity import ItemIndex

    lib = cycle.MusicLibrary(scan.LIBRARY_DB)
    library = ItemIndex(lib.all_items())
    playlist = store.Store("pl", [
        {"song": _song("Two", "S2")},
        {store.LOCAL: {"id": 3}},
        {"song": {"name": "Gone", "url": "https://example.com/x"}},
        {"song": _song("Nope", "S9")},
        {store.LOCAL: {"id": 99}},
        {"song": _song("One", "S1")},
    ])
    with mock.patch.object(scan, "store_view", wraps=scan.store_view) as view:
        entries = cycle.entries_of("pl", playlist, library, pls_mod.by_album_id([]), {}, {1, 2, 3})
    assert view.call_args.kwargs["playlist"] is playlist
    assert entries == [Entry("spotify:S2", 2), Entry("local:3", 3, local=True),
                       Entry("url:https://example.com/x", None, playable=False), Entry("spotify:S9", None),
                       Entry("local:99", None, local=True), Entry("spotify:S1", 1)]


def test_entries_refuse_slots_that_dont_line_up(env, monkeypatch):
    from music_scan import scan

    monkeypatch.setattr(scan, "store_view", lambda *a, **k: ([], []))
    with pytest.raises(RuntimeError, match="0 slots for 1 store songs"):
        cycle.entries_of("pl", store.Store("pl", [{"song": _song("One", "S1")}]), None, {}, {}, set())


def test_a_track_is_its_media_file_and_the_item_at_its_path(env):
    folded = cycle.by_fold(env.items)
    nd_music = cycle.PurePosixPath("/music")
    one, ambiguous = cycle.to_track(_row("library/one.m4a", "mf1"), nd_music, folded)
    assert (one.song, one.item, one.missing, one.promotable, ambiguous) == ("mf1", 1, False, True, False)
    three, _ = cycle.to_track(_row("library/three.m4a", "mf3", missing=True), nd_music, folded)
    assert (three.item, three.missing, three.promotable, three.mapped) == (3, True, False, None)
    assert cycle.to_track(_row("elsewhere.m4a", "mfx"), nd_music, folded)[0].item is None
    with pytest.raises(KeyError):
        cycle.to_track({"path": "library/one.m4a", "libraryPath": "/music", "id": "1"}, nd_music, folded)


def test_fold_matches_paths_as_navidrome_does():
    """SQLite NOCASE folds ASCII letters only; Unicode forms are one path."""
    assert cycle.fold("Hot Chip/04 - Look At Where We Are.m4a") == cycle.fold("hot chip/04 - Look at where we are.m4a")
    assert cycle.fold("Beyonce\u0301/x.m4a") == cycle.fold("Beyonc\u00e9/x.m4a")
    assert cycle.fold("\u00c9tude/x.m4a") != cycle.fold("\u00e9tude/x.m4a")


@pytest.mark.parametrize("candidates, exact, prefer, expected", [
    ([], None, [], None),
    (["A"], "A", [], "A"),
    (["A", "B"], "B", [{1}], "A"),           # the view's twin, though Navidrome linked B
    (["A", "B"], "B", [set(), {2}], "B"),    # else the snapshot's
    (["A", "B"], "A", [{1, 2}], "A"),        # both in the view: the exact one
    (["A", "B"], "B", [{1, 2}], "B"),
    (["A", "B", "C"], "C", [{1, 2}], "ambiguous"),  # two in the view, neither exact
    (["A", "B"], "A", [set(), set()], "ambiguous"),  # neither on the playlist
])
def test_pick_decides_between_case_twins(candidates, exact, prefer, expected):
    items = {"A": mock.Mock(id=1), "B": mock.Mock(id=2), "C": mock.Mock(id=3)}
    got = cycle.pick([items[c] for c in candidates], items.get(exact), prefer)
    if expected == "ambiguous":
        assert got is cycle.AMBIGUOUS
    else:
        assert got is (None if expected is None else items[expected])


# --- the cycle ------------------------------------------------------------------------------


def test_off_runs_nothing(env, monkeypatch):
    monkeypatch.setenv(cycle.MODE_ENV, "off")
    free = mock.Mock(return_value=True)
    assert cycle.run(free) == "off"
    free.assert_not_called()
    env.list_playlists.assert_not_called()
    assert env.pushed == {}


def test_first_cycle_takes_the_snapshot_and_reports_nothing(env, caplog):
    with caplog.at_level(logging.INFO, logger="music_scan.playlist_sync_cycle"):
        assert cycle.run(_free) == "ok"
    assert _snapshots() == {"pl": [(1, "s1"), (2, "s2"), (3, "s3")]}
    assert "[SYNC-DRY] pl: snapshot taken: 3 of the view's 3 item(s), navidrome 3" in caplog.text
    assert "2 covered, 1 compared (1 snapshot(s) taken), 1 skipped (other (no_navidrome_playlist))" in caplog.text
    outcome = _series(env.pushed["music_playlist_sync"])
    assert 'music_playlist_sync_outcome{outcome="ok"} 1' in outcome
    assert 'music_playlist_sync_outcome{outcome="error"} 0' in outcome
    plans = _series(env.pushed["music_playlist_sync_ok"])
    assert 'music_playlist_sync_pending{playlist="pl",kind="local_delete"} 0' in plans
    assert 'music_playlist_sync_would_trip{playlist="pl",rule="writes"} 0' in plans
    assert 'music_playlist_sync_skipped{playlist="other",reason="no_navidrome_playlist"} 1' in plans
    assert not any('playlist="other",kind=' in line for line in plans)


def test_a_dry_run_reports_a_local_delete_and_never_advances_the_snapshot(env, caplog):
    _seed("pl", [(1, "s1"), (2, "s2"), (3, "s3")])
    env.tracks["imp"] = env.tracks["imp"][:1] + env.tracks["imp"][2:]  # Two deleted in Symfonium
    with caplog.at_level(logging.INFO, logger="music_scan.playlist_sync_cycle"):
        assert cycle.run(_free) == "ok"
        assert cycle.run(_free) == "ok"
    assert _snapshots() == {"pl": [(1, "s1"), (2, "s2"), (3, "s3")]}
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[SYNC-DRY] pl:")]
    assert lines[0].startswith("[SYNC-DRY] pl: view 3, navidrome 2, local delete 1, local add 0, promotion 0, ")
    assert "spotify remove 1" in lines[0] and "navidrome remove 0" in lines[0]
    assert "[SYNC-DRY]   pl local delete: 2 Artist — Two" in caplog.text
    plans = _series(env.pushed["music_playlist_sync_ok"])
    assert 'music_playlist_sync_pending{playlist="pl",kind="local_delete"} 1' in plans
    assert 'music_playlist_sync_pending{playlist="pl",kind="spotify_remove"} 1' in plans


def test_a_case_twin_navidrome_linked_is_the_views_item(env):
    """homelab#2742: the .m3u lists one.m4a, Navidrome linked One.m4a (another
    item with its own Spotify ID).  It is the view's item, never an add or a promotion."""
    env.items.append(_item("One", env.lib_root / "One.m4a", 4, spotify_ids="S1X"))
    env.tracks["imp"][0] = _row("library/One.m4a", "s1x")
    assert cycle.run(_free) == "ok"
    assert _snapshots() == {"pl": [(1, "s1x"), (2, "s2"), (3, "s3")]}
    assert cycle.run(_free) == "ok"
    plans = _series(env.pushed["music_playlist_sync_ok"])
    for kind in ("local_add", "promotion", "navidrome_add", "navidrome_remove", "ambiguous"):
        assert f'music_playlist_sync_pending{{playlist="pl",kind="{kind}"}} 0' in plans


def test_a_track_twins_decide_nothing_for_is_ambiguous_not_an_add(env, caplog):
    env.items += [_item("X", env.lib_root / "x.m4a", 5, spotify_ids="SX"),
                  _item("X", env.lib_root / "X.m4a", 6, spotify_ids="SX2")]
    _seed("pl", [(1, "s1"), (2, "s2"), (3, "s3")])
    env.tracks["imp"].append(_row("library/x.m4a", "sx"))
    with caplog.at_level(logging.DEBUG, logger="music_scan.playlist_sync_cycle"):
        assert cycle.run(_free) == "ok"
    plans = _series(env.pushed["music_playlist_sync_ok"])
    assert 'music_playlist_sync_pending{playlist="pl",kind="ambiguous"} 1' in plans
    assert 'music_playlist_sync_pending{playlist="pl",kind="local_add"} 0' in plans
    assert 'music_playlist_sync_pending{playlist="pl",kind="promotion"} 0' in plans
    assert "ambiguous 1" in caplog.text


def test_a_clean_playlist_logs_at_debug_only(env, caplog):
    _seed("pl", [(1, "s1"), (2, "s2"), (3, "s3")])
    with caplog.at_level(logging.INFO, logger="music_scan.playlist_sync_cycle"):
        assert cycle.run(_free) == "ok"
    assert "[SYNC-DRY] pl:" not in caplog.text
    assert "pending local delete 0, local add 0, promotion 0" in caplog.text


def test_a_playlist_whose_m3u_is_newer_than_its_import_is_skipped(env, caplog):
    """Written and not re-imported yet: a track downloaded since would read as an add."""
    _seed("pl", [(1, "s1")])
    m3u = env.tmp / "playlists" / "pl.m3u"
    stamp = datetime(2026, 11, 9, 3, 0, tzinfo=timezone.utc).timestamp()
    env.list_playlists.return_value = [_pls("imp", "pl", updated="2026-11-09T03:00:00Z")]
    os.utime(m3u, (stamp + cycle.M3U_SLACK_SECONDS, stamp + cycle.M3U_SLACK_SECONDS))
    with caplog.at_level(logging.INFO, logger="music_scan.playlist_sync_cycle"):
        assert cycle.run(_free) == "ok"  # within the slack: compared
    assert 'music_playlist_sync_pending{playlist="pl",kind="local_add"} 0' in env.pushed["music_playlist_sync_ok"]
    os.utime(m3u, (stamp + 60, stamp + 60))
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="music_scan.playlist_sync_cycle"):
        assert cycle.run(_free) == "ok"
    plans = _series(env.pushed["music_playlist_sync_ok"])
    assert 'music_playlist_sync_skipped{playlist="pl",reason="m3u_newer"} 1' in plans
    assert not any('playlist="pl",kind=' in line for line in plans)
    assert "pl (m3u_newer)" in caplog.text


def test_of_two_navidrome_playlists_from_one_m3u_the_synced_one_is_read(env, caplog):
    env.list_playlists.return_value = [_pls("old", "pl", sync=False), _pls("imp", "pl")]
    with caplog.at_level(logging.WARNING):
        assert cycle.run(_free) == "ok"
    env.playlist_tracks.assert_called_once_with(URL, HEADERS, "imp")
    assert "[SYNC] pl: 2 Navidrome playlists are imported from pl.m3u" in caplog.text


def test_a_navidrome_playlist_with_sync_off_is_skipped(env):
    env.list_playlists.return_value = [_pls("imp", "pl", sync=False)]
    assert cycle.run(_free) == "ok"
    env.playlist_tracks.assert_not_called()
    assert 'reason="navidrome_sync_off"' in env.pushed["music_playlist_sync_ok"]
    assert _snapshots() == {}


def test_a_snapshot_of_a_playlist_that_left_the_sync_is_dropped(env, monkeypatch, caplog):
    """Removed from playlists.conf or flagged nosync/oneway: dropped, so a
    return is taken afresh.  Narrowing by PLAYLIST_SYNC_PLAYLISTS drops nothing."""
    for name in ("pl", "other", "frozen", "locked", "removed"):
        _seed(name, [(1, "s1")])
    monkeypatch.setenv(cycle.PLAYLISTS_ENV, "pl")
    with caplog.at_level(logging.INFO, logger="music_scan.playlist_sync_cycle"):
        assert cycle.run(_free) == "ok"
    assert sorted(_snapshots()) == ["other", "pl"]
    assert "[SYNC] frozen: snapshot dropped, the playlist left the sync (flagged nosync)" in caplog.text
    assert "[SYNC] removed: snapshot dropped, the playlist left the sync (not in playlists.conf)" in caplog.text


def test_a_missing_store_is_the_no_store_would_trip(env, caplog):
    env.list_playlists.return_value = [_pls("imp", "pl"), _pls("o", "other")]
    env.tracks["o"] = [_row("library/one.m4a", "s1")]
    with caplog.at_level(logging.INFO, logger="music_scan.playlist_sync_cycle"):
        assert cycle.run(_free) == "ok"
    plans = _series(env.pushed["music_playlist_sync_ok"])
    assert 'music_playlist_sync_would_trip{playlist="other",rule="no_store"} 1' in plans
    assert not any('playlist="other",kind=' in line for line in plans)
    assert "[SYNC-DRY] other: would trip no_store" in caplog.text
    assert "other" not in _snapshots()


def test_the_owner_is_checked_when_the_snapshot_is_taken(env, caplog):
    env.list_playlists.return_value = [_pls("imp", "pl", owner="someone")]
    with caplog.at_level(logging.WARNING):
        assert cycle.run(_free) == "ok"
    assert "[SYNC-DRY] pl: the Navidrome playlist belongs to someone, not u" in caplog.text


@pytest.mark.parametrize("busy_at", ["start", "end"])
def test_a_busy_pipeline_skips_or_discards_the_cycle(env, busy_at):
    calls = iter([False] if busy_at == "start" else [True, False])
    assert cycle.run(lambda: next(calls)) == "busy"
    assert _snapshots() == {}
    assert 'music_playlist_sync_outcome{outcome="busy"} 1' in env.pushed["music_playlist_sync"]
    assert "music_playlist_sync_ok" not in env.pushed
    if busy_at == "start":
        env.list_playlists.assert_not_called()


@pytest.mark.parametrize("scanning", [[True], [False, True]])
def test_a_navidrome_scan_skips_or_discards_the_cycle(env, scanning):
    env.scanning.side_effect = scanning
    assert cycle.run(_free) == "scanning"
    assert _snapshots() == {}
    assert "music_playlist_sync_ok" not in env.pushed


def test_a_playlist_that_fails_alone_is_skipped(env, caplog):
    env.list_playlists.return_value = [_pls("imp", "pl"), _pls("o", "other")]
    env.tracks["o"] = [{"path": "library/one.m4a"}]  # no mediaFileId
    with caplog.at_level(logging.ERROR):
        assert cycle.run(_free) == "ok"
    assert "[SYNC-DRY] other: could not be read" in caplog.text
    assert 'reason="failed"' in env.pushed["music_playlist_sync_ok"]
    assert "pl" in _snapshots()


def test_a_cycle_where_no_playlist_could_be_read_is_an_error(env):
    env.tracks["imp"] = [{"path": "x"}]
    assert cycle.run(_free) == "error"
    assert 'music_playlist_sync_outcome{outcome="error"} 1' in env.pushed["music_playlist_sync"]


@pytest.mark.parametrize("break_it", ["login", "mode", "conf", "unset"])
def test_a_failed_cycle_is_the_error_outcome_and_never_raises(env, monkeypatch, break_it):
    if break_it == "login":
        monkeypatch.setattr(navidrome, "_login", mock.Mock(side_effect=RuntimeError("Navidrome login failed")))
    elif break_it == "mode":
        monkeypatch.setenv(cycle.MODE_ENV, "on")
    elif break_it == "conf":
        monkeypatch.setattr(config, "load_playlists", mock.Mock(side_effect=ValueError("twoway with oneway")))
    else:
        monkeypatch.delenv("NAVIDROME_URL")
        monkeypatch.setattr(navidrome, "scanning", lambda: False)
    assert cycle.run(_free) == "error"
    assert 'music_playlist_sync_outcome{outcome="error"} 1' in env.pushed["music_playlist_sync"]
    assert "music_playlist_sync_ok" not in env.pushed


def test_each_metric_family_is_declared_once(env):
    env.list_playlists.return_value = [_pls("imp", "pl"), _pls("o", "other")]
    env.tracks["o"] = [_row("library/one.m4a", "s1")]
    assert cycle.run(_free) == "ok"
    for body in env.pushed.values():
        types = [line for line in body.splitlines() if line.startswith("# TYPE")]
        assert len(types) == len(set(types))
