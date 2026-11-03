"""The playlist shadow check (homelab#2361): each playlist's Navidrome playlist
against its store's Navidrome view, read-only.  Navidrome's read functions,
the path mapping, the diff, and the run, which writes nothing."""

from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath
from unittest import mock

import pytest
import requests

from music_fetch import store
from music_scan import navidrome, playlist_shadow
from music_scan.playlist_shadow import Track, diff, pipeline_path

URL = "http://navidrome.example.com"
HEADERS = {"X-ND-Authorization": "Bearer jwt"}


def _resp(body, headers=None, status=200):
    resp = mock.Mock(status_code=status, headers=headers or {})
    resp.json.return_value = body
    resp.raise_for_status.side_effect = None if status < 400 else requests.HTTPError(f"{status}")
    return resp


def _row(rel, title="T", missing=False, library="/music"):
    return {"id": "1", "mediaFileId": "mf", "playlistId": "p", "libraryPath": library, "path": rel,
            "title": title, "artist": "Artist", "missing": missing}


# --- Navidrome reads ------------------------------------------------------------------------


@pytest.mark.parametrize("scanning", [True, False])
def test_scanning_reads_get_scan_status(scanning):
    body = {"subsonic-response": {"status": "ok", "scanStatus": {"scanning": scanning, "count": 3}}}
    with mock.patch("music_scan.navidrome.requests.get", return_value=_resp(body)) as get:
        assert navidrome.scanning(URL, "u", "pw") is scanning
    assert get.call_args[0][0] == f"{URL}/rest/getScanStatus.view"
    assert get.call_args[1]["params"]["f"] == "json"


def test_scanning_raises_when_unreadable():
    failed = {"subsonic-response": {"status": "failed", "error": {"code": 40}}}
    with mock.patch("music_scan.navidrome.requests.get", return_value=_resp(failed)):
        with pytest.raises(RuntimeError, match="non-ok"):
            navidrome.scanning(URL, "u", "pw")
    with mock.patch("music_scan.navidrome.requests.get", side_effect=requests.ConnectionError("down")):
        with pytest.raises(RuntimeError, match="scan status"):
            navidrome.scanning(URL, "u", "pw")


def test_list_playlists():
    with mock.patch("music_scan.navidrome.requests.get", return_value=_resp([{"id": "a"}])) as get:
        assert navidrome.list_playlists(URL, HEADERS) == [{"id": "a"}]
    assert get.call_args[0][0] == f"{URL}/api/playlist"
    assert get.call_args[1]["headers"] == HEADERS
    with mock.patch("music_scan.navidrome.requests.get", return_value=_resp(None, status=500)):
        with pytest.raises(RuntimeError, match="list Navidrome playlists"):
            navidrome.list_playlists(URL, HEADERS)


def test_playlist_tracks_pages_in_playlist_order():
    """Pages by ``_start``/``_end`` sorted by the row id (playlist order) until
    ``X-Total-Count`` rows are in."""
    pages = [_resp([_row("a"), _row("b")], {"X-Total-Count": "3"}), _resp([_row("c")], {"X-Total-Count": "3"})]
    with mock.patch("music_scan.navidrome.requests.get", side_effect=pages) as get:
        rows = navidrome.playlist_tracks(URL, HEADERS, "p1", page=2)
    assert [r["path"] for r in rows] == ["a", "b", "c"]
    assert get.call_count == 2
    first, second = (c[1]["params"] for c in get.call_args_list)
    assert first == {"_start": 0, "_end": 2, "_sort": "id", "_order": "ASC"}
    assert second == {"_start": 2, "_end": 4, "_sort": "id", "_order": "ASC"}
    assert get.call_args[0][0] == f"{URL}/api/playlist/p1/tracks"


def test_playlist_tracks_empty():
    with mock.patch("music_scan.navidrome.requests.get", return_value=_resp([], {"X-Total-Count": "0"})) as get:
        assert navidrome.playlist_tracks(URL, HEADERS, "p1") == []
    assert get.call_count == 1


@pytest.mark.parametrize("pages, match", [
    ([_resp([_row("a")], {"X-Total-Count": "2"}), _resp([_row("b")], {"X-Total-Count": "3"})], "changed while"),
    ([_resp([_row("a")], {"X-Total-Count": "2"}), _resp([], {"X-Total-Count": "2"})], "read 1 of 2"),
    ([_resp([_row("a")])], "tracks"),  # no X-Total-Count
    ([_resp(None, status=404)], "tracks"),
])
def test_playlist_tracks_raises_on_an_inconsistent_read(pages, match):
    with mock.patch("music_scan.navidrome.requests.get", side_effect=pages):
        with pytest.raises(RuntimeError, match=match):
            navidrome.playlist_tracks(URL, HEADERS, "p1", page=1)


# --- Path mapping ---------------------------------------------------------------------------


def test_pipeline_path_swaps_the_music_folder():
    music = Path("/root/Music")
    nd = PurePosixPath("/music")
    assert pipeline_path(_row("library/A/B/01 x.m4a"), nd, music) == Path("/root/Music/library/A/B/01 x.m4a")
    assert pipeline_path(_row("x.m4a", library="/elsewhere"), nd, music) is None
    assert pipeline_path(_row("library/x.m4a", library="/srv/music"), PurePosixPath("/srv/music"), music) == \
        Path("/root/Music/library/x.m4a")


# --- The diff -------------------------------------------------------------------------------

A, B, C, D = (Path(f"/root/Music/library/{n}.m4a") for n in "abcd")
ITEMS = {A: "a", B: "b", C: "c", D: "d"}


def _t(path, missing=False):
    return Track(path, str(path), "Artist", path.stem if path else "?", missing)


def test_diff_clean():
    shadow = diff([A, B, C], [_t(A), _t(B), _t(C)], ITEMS)
    assert shadow.clean and shadow.order_same
    assert (shadow.view, shadow.tracks) == (3, 3)


def test_diff_adds_and_deletes():
    shadow = diff([A, B, C], [_t(A), _t(D), _t(C)], ITEMS)
    assert [t.path for t in shadow.would_add] == [D]
    assert shadow.would_delete == [B]
    assert shadow.order_same and not shadow.clean


def test_diff_unmapped_takes_no_further_part():
    """A track on no beets item, or outside the music folder, is unmapped, not an add."""
    stray = Path("/root/Music/library/stray.m4a")
    shadow = diff([A], [_t(A), _t(stray), Track(None, "/elsewhere/x.m4a")], ITEMS)
    assert [t.nd_path for t in shadow.unmapped] == [str(stray), "/elsewhere/x.m4a"]
    assert shadow.would_add == [] and shadow.would_delete == []
    assert shadow.tracks == 3


def test_diff_missing_is_reported_with_its_mapping():
    shadow = diff([A, B], [_t(A), _t(B, missing=True)], ITEMS)
    assert [t.path for t in shadow.missing] == [B]
    assert shadow.would_delete == [] and not shadow.clean


def test_diff_dedupes_both_sides_first_copy_kept():
    shadow = diff([A, B, A], [_t(A), _t(B), _t(A)], ITEMS)
    assert shadow.view == 2 and shadow.tracks == 3
    assert [t.path for t in shadow.duplicates] == [A]
    assert shadow.order_same and not shadow.would_add and not shadow.would_delete


def test_diff_order_over_the_shared_entries():
    """An add or delete alone doesn't make the order differ; a swap does."""
    assert diff([A, B, C], [_t(A), _t(D), _t(C)], ITEMS).order_same
    swapped = diff([A, B, C], [_t(A), _t(C), _t(B)], ITEMS)
    assert not swapped.order_same
    assert swapped.first_moved == (2, B, C)
    assert not swapped.would_add and not swapped.would_delete


# --- The run --------------------------------------------------------------------------------


def _item(title, path, item_id, **flex):
    it = mock.MagicMock(title=title, artist="Artist", albumartist="Artist")
    it.id = item_id
    it.path = str(path).encode()
    it.get.side_effect = lambda k, d=None: flex.get(k, d)
    return it


def _song(name, song_id):
    return {"name": name, "artists": ["Artist"], "url": f"https://open.spotify.com/track/{song_id}",
            "song_id": song_id}


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """A library of three items under ``tmp_path/library`` (the pipeline's
    ``/root/Music`` is ``tmp_path``); store ``pl`` lists one/two by Spotify ID
    then local entry three; Navidrome reachable and idle, with no playlists."""
    lib_root = tmp_path / "library"
    one = _item("One", lib_root / "one.m4a", 1, spotify_ids="S1")
    two = _item("Two", lib_root / "two.m4a", 2, spotify_ids="S2")
    three = _item("Three", lib_root / "three.m4a", 3)
    lib = mock.MagicMock()
    lib.__enter__ = mock.MagicMock(return_value=lib)
    lib.__exit__ = mock.MagicMock(return_value=False)
    lib.all_items.return_value = [one, two, three]
    monkeypatch.setattr("music_scan.scan.LIBRARY", lib_root)
    monkeypatch.setattr("music_scan.scan.SPOTDL_DIR", tmp_path / "spotdl")
    monkeypatch.setattr("music_scan.scan._album_records", lambda: {})
    monkeypatch.setattr(playlist_shadow, "MusicLibrary", mock.MagicMock(return_value=lib))
    for var, value in (("NAVIDROME_URL", URL), ("NAVIDROME_USER", "u"), ("NAVIDROME_PASSWORD", "pw")):
        monkeypatch.setenv(var, value)
    monkeypatch.delenv("NAVIDROME_PLAYLISTS_DIR", raising=False)
    monkeypatch.delenv("NAVIDROME_MUSIC_DIR", raising=False)

    pl = store.Store.from_songs("pl", [_song("One", "S1"), _song("Two", "S2")])
    pl.entries.append({store.LOCAL: {"id": 3, "path": "x", "title": "Three", "artist": "Artist"}})
    store.save(pl)
    nd = mock.MagicMock()
    nd.scanning.return_value = False
    nd.list_playlists.return_value = []
    nd.tracks = {}
    nd.playlist_tracks.side_effect = lambda url, headers, pid: nd.tracks[pid]
    nd.save = store.save  # the real one, for seeding
    monkeypatch.setattr(navidrome, "scanning", nd.scanning)
    monkeypatch.setattr(navidrome, "_login", lambda *a: HEADERS)
    monkeypatch.setattr(navidrome, "list_playlists", nd.list_playlists)
    monkeypatch.setattr(navidrome, "playlist_tracks", nd.playlist_tracks)
    # Read-only: nothing saves a store.
    monkeypatch.setattr(store, "save", mock.Mock(side_effect=AssertionError("the shadow check saved a store")))
    return nd


def _shadow_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("[SHADOW] pl:")]


def test_run_clean(env, caplog):
    """The view (store entries, then the local tail) against the Navidrome
    playlist imported from pl.m3u, matched by path: one under the same name
    with no path is ignored."""
    env.list_playlists.return_value = [
        {"id": "made", "name": "pl", "path": "", "sync": False},
        {"id": "imp", "name": "Renamed", "path": "/music/playlists/pl.m3u", "sync": True},
    ]
    env.tracks["imp"] = [_row("library/one.m4a"), _row("library/two.m4a"), _row("library/three.m4a")]
    with caplog.at_level(logging.INFO, logger="music_scan.playlist_shadow"):
        assert playlist_shadow.run() == 0
    env.playlist_tracks.assert_called_once_with(URL, HEADERS, "imp")
    assert _shadow_lines(caplog) == ["[SHADOW] pl: view 3, navidrome 3, would add 0, would delete 0, unmapped 0, "
                                     "missing 0, order same"]
    assert "1 playlist(s): 1 the same, 0 differ, 0 with no Navidrome playlist" in caplog.text


def test_run_reports_differences_and_lists_them_at_debug(env, caplog):
    env.list_playlists.return_value = [{"id": "imp", "name": "pl", "path": "/music/playlists/pl.m3u", "sync": True}]
    env.tracks["imp"] = [_row("library/three.m4a"), _row("library/one.m4a"), _row("library/gone.m4a", "Gone", True)]
    with caplog.at_level(logging.DEBUG, logger="music_scan.playlist_shadow"):
        assert playlist_shadow.run() == 0
    assert _shadow_lines(caplog)[0] == ("[SHADOW] pl: view 3, navidrome 3, would add 0, would delete 1, unmapped 1, "
                                        "missing 1, order differs")
    assert "pl would delete: Artist — Two" in caplog.text
    assert "pl unmapped: Artist — Gone (/music/library/gone.m4a)" in caplog.text


def test_run_reports_a_playlist_with_no_navidrome_counterpart(env, caplog):
    with caplog.at_level(logging.INFO, logger="music_scan.playlist_shadow"):
        assert playlist_shadow.run() == 0
    assert _shadow_lines(caplog) == ["[SHADOW] pl: view 3, no Navidrome playlist imported from /music/playlists/pl.m3u"]
    env.playlist_tracks.assert_not_called()


def test_run_skips_later_and_unknown_names(env, caplog):
    env.save(store.Store.from_songs("later", [_song("One", "S1")]))
    with caplog.at_level(logging.INFO, logger="music_scan.playlist_shadow"):
        assert playlist_shadow.run(["later", "nope"]) == 0
    assert "[SHADOW] later: no playlist store (the Later queue writes later.m3u); skipped" in caplog.text
    assert "[SHADOW] nope: no playlist store; skipped" in caplog.text
    assert not _shadow_lines(caplog)


def test_run_is_skipped_while_navidrome_scans(env, caplog):
    env.scanning.return_value = True
    with caplog.at_level(logging.INFO, logger="music_scan.playlist_shadow"):
        assert playlist_shadow.run() == 0
    assert "Navidrome is scanning" in caplog.text
    env.list_playlists.assert_not_called()
    env.playlist_tracks.assert_not_called()


def test_run_fails_when_navidrome_cant_be_read(env, monkeypatch, caplog):
    env.list_playlists.side_effect = RuntimeError("Could not list Navidrome playlists: down")
    assert playlist_shadow.run() == 1
    env.list_playlists.side_effect = None
    env.list_playlists.return_value = [{"id": "imp", "name": "pl", "path": "/music/playlists/pl.m3u", "sync": True}]
    env.playlist_tracks.side_effect = RuntimeError("changed while it was read")
    assert playlist_shadow.run() == 1
    monkeypatch.delenv("NAVIDROME_URL")
    assert playlist_shadow.run() == 1


def test_run_leaves_the_store_as_it_was(env):
    """A local entry the view now holds is dropped by the scan's tail, which
    saves; the shadow check lists it once and saves nothing."""
    pl = store.load("pl")
    pl.entries.insert(0, {store.LOCAL: {"id": 1, "path": "x", "title": "One", "artist": "Artist"}})
    env.save(pl)
    before = store.load("pl")
    env.list_playlists.return_value = [{"id": "imp", "name": "pl", "path": "/music/playlists/pl.m3u", "sync": True}]
    env.tracks["imp"] = [_row("library/one.m4a"), _row("library/two.m4a"), _row("library/three.m4a")]
    assert playlist_shadow.run() == 0
    assert store.load("pl") == before
    store.save.assert_not_called()


def test_main_exit_status(env):
    with pytest.raises(SystemExit) as exc:
        playlist_shadow.main(["pl"])
    assert exc.value.code == 0
