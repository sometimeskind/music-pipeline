"""Tests for music_fetch.store, the playlist store (homelab#1914 step 1)."""

from datetime import datetime, timezone

import pytest

from music_fetch import store

URL = "https://open.spotify.com/track/"


def song(sid: str, **extra) -> dict:
    return {"url": URL + sid, "song_id": sid, "name": f"Song {sid}", "artists": ["Artist"], **extra}


@pytest.mark.parametrize("value, expected", [
    (None, store.OFF), ("", store.OFF), ("off", store.OFF), ("dry-run", store.DRY_RUN),
    (" Dry-Run ", store.DRY_RUN), ("on", store.OFF), ("yes", store.OFF),
])
def test_mode(monkeypatch, value, expected) -> None:
    if value is None:
        monkeypatch.delenv(store.STORE_ENV, raising=False)
    else:
        monkeypatch.setenv(store.STORE_ENV, value)
    assert store.mode() == expected


def test_store_round_trip(tmp_path) -> None:
    s = store.Store.from_songs("pl", [song("A"), song("B")])
    s.save(tmp_path / "pl.json")
    loaded = store.Store.load(tmp_path / "pl.json")
    assert loaded is not None
    assert loaded.name == "pl" and loaded.songs == [song("A"), song("B")]
    assert loaded.updated
    assert not (tmp_path / "pl.json.tmp").exists()


def test_store_load_missing_or_broken(tmp_path) -> None:
    assert store.Store.load(tmp_path / "none.json") is None
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    assert store.Store.load(tmp_path / "bad.json") is None


def test_merge_spotify_takes_order_and_membership_from_spotify() -> None:
    full_b = song("B", album_name="Full record")
    merged = store.merge_spotify([song("C"), song("B"), song("C")], {URL + "B": full_b, URL + "A": song("A")})
    # A left Spotify and is gone; B keeps its richer record; the duplicate C stays.
    assert merged == [song("C"), full_b, song("C")]


def test_want_list() -> None:
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    failures = {
        URL + "D": {"retry_after": "2026-10-20T00:00:00+00:00"},  # still backed off
        URL + "E": {"retry_after": "2026-10-01T00:00:00+00:00"},  # backoff over
    }
    songs = [song(x) for x in "ABCDEFA"]
    wanted = store.want_list(songs, resolves=lambda s: s["song_id"] == "B", held={URL + "C"}, failures=failures,
                             now=now)
    assert wanted == [URL + "A", URL + "E", URL + "F"]


def test_diff() -> None:
    d = store.diff(["a", "b", "c"], ["a", "c", "b", "d"])
    assert d.added == ["d"] and d.removed == [] and d.reordered
    assert store.diff(["a", "b"], ["a", "b"]).same
    d = store.diff(["a", "x", "b"], ["a", "b"])
    assert d.removed == ["x"] and not d.reordered


def test_write_view(tmp_path) -> None:
    d = store.write_view("pl", "m3u", ["a", "x"], ["a", "y"], directory=tmp_path)
    assert not d.same
    assert (tmp_path / "pl.m3u").read_text(encoding="utf-8") == "a\ny\n"
    assert (tmp_path / "pl.m3u.diff").read_text(encoding="utf-8") == "+ y\n- x\n"
    store.write_view("pl", "want", [], [], directory=tmp_path)
    assert (tmp_path / "pl.want").read_text(encoding="utf-8") == ""
    rows = (tmp_path / "history.tsv").read_text(encoding="utf-8").splitlines()
    assert [r.split("\t")[1:] for r in rows] == [["pl", "m3u", "2", "2", "1", "1", "0"],
                                                ["pl", "want", "0", "0", "0", "0", "0"]]


# ---------------------------------------------------------------------------
# The nightly's dry run (ingest.sync_playlists)
# ---------------------------------------------------------------------------


def _sync(tmp_path, monkeypatch, result, mode: str | None = store.DRY_RUN, snapshot=(), nosync=False,
          fetched=None, **kwargs):
    import json
    from unittest import mock

    from music_fetch import ingest
    from music_fetch.metrics import IngestMetrics

    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir(exist_ok=True)
    (spotdl_dir / "pl.spotdl").write_text(json.dumps({"type": "sync", "query": ["q"], "songs": list(snapshot)}),
                                          encoding="utf-8")
    if nosync:
        (spotdl_dir / "pl.nosync").touch()
    if mode is None:
        monkeypatch.delenv(store.STORE_ENV, raising=False)
    else:
        monkeypatch.setenv(store.STORE_ENV, mode)
    monkeypatch.setattr(store, "DRYRUN_DIR", tmp_path / "side")
    with mock.patch.object(ingest, "SPOTDL_DIR", spotdl_dir), \
         mock.patch.object(ingest, "CONF_PATH", tmp_path / "missing.conf"), \
         mock.patch.object(ingest, "COOKIE_FILE", tmp_path / "cookies.txt"), \
         mock.patch.object(ingest, "FAILURES_FILE", tmp_path / ".failures.json"), \
         mock.patch("music_fetch.ingest.sync_playlist", return_value=result), \
         mock.patch("music_fetch.ingest.fetch_songs", side_effect=fetched), \
         mock.patch("music_fetch.ingest.time.sleep"):
        ingest.sync_playlists([], IngestMetrics(), **kwargs)
    return tmp_path / "side"


def _result(**kwargs):
    from music_fetch.spotdl_ops import SyncResult

    return SyncResult(set(), 0, 0, 0, 0, {}, **kwargs)


def test_nightly_dry_run_writes_the_store_and_its_want_list(tmp_path, monkeypatch) -> None:
    full_a = song("A", album_name="Full record")
    result = _result(wanted=[URL + "C"], songs=[song("A"), song("B"), song("C"), song("D")],
                     records={URL + "A": full_a})
    side = _sync(tmp_path, monkeypatch, result, resolves=lambda s: s["song_id"] == "A", held={URL + "B"})
    assert store.Store.load(side / "pl.json").songs == [full_a, song("B"), song("C"), song("D")]
    # D is wanted by the store but not today (e.g. a .spotdl entry no item holds by ID).
    assert (side / "pl.want").read_text(encoding="utf-8").splitlines() == [URL + "C", URL + "D"]
    assert (side / "pl.want.diff").read_text(encoding="utf-8").splitlines() == [f"+ {URL}D"]


def test_nightly_dry_run_keeps_an_earlier_stores_records(tmp_path, monkeypatch) -> None:
    full_b = song("B", album_name="Full record")
    (tmp_path / "side").mkdir()
    store.Store.from_songs("pl", [full_b]).save(tmp_path / "side" / "pl.json")
    side = _sync(tmp_path, monkeypatch, _result(songs=[song("B")]), resolves=lambda s: True)
    assert store.Store.load(side / "pl.json").songs == [full_b]


@pytest.mark.parametrize("mode, kwargs", [(None, {"resolves": lambda s: False}), (store.DRY_RUN, {})])
def test_nightly_writes_nothing_unless_dry_run_and_a_resolver(tmp_path, monkeypatch, mode, kwargs) -> None:
    side = _sync(tmp_path, monkeypatch, _result(songs=[song("A")]), mode=mode, **kwargs)
    assert not side.exists()


def test_nightly_dry_run_builds_a_nosync_store_from_spotify_once(tmp_path, monkeypatch, caplog) -> None:
    caplog.set_level("INFO")
    # The .spotdl lists only what was downloaded before the freeze: B is on Spotify but not in it.
    full_a = song("A", album_name="Full record")
    (tmp_path / "side").mkdir()
    store.Store.from_songs("pl", [full_a]).save(tmp_path / "side" / "pl.json")  # a scan seeded it from .spotdl
    side = _sync(tmp_path, monkeypatch, None, snapshot=[full_a], nosync=True, resolves=lambda s: True,
                 fetched=lambda f, c: [song("A"), song("B")])
    built = store.Store.load(side / "pl.json")
    assert built.source == store.SPOTIFY
    assert built.songs == [full_a, song("B")]
    assert "[STORE] pl: built from Spotify, 2 entries (.spotdl has 1)" in caplog.text

    # Kept from then on: no second fetch.
    _sync(tmp_path, monkeypatch, None, snapshot=[full_a], nosync=True, resolves=lambda s: True,
          fetched=AssertionError("fetched again"))
    assert "fetched again" not in caplog.text
    assert store.Store.load(side / "pl.json").songs == [full_a, song("B")]


def test_nightly_dry_run_nosync_fetch_failure_never_fails_the_sync(tmp_path, monkeypatch, caplog) -> None:
    side = _sync(tmp_path, monkeypatch, None, nosync=True, resolves=lambda s: True,
                 fetched=RuntimeError("spotify down"))
    assert "Playlist store dry run failed for pl" in caplog.text
    assert not (side / "pl.json").exists()


def test_nightly_fetches_no_nosync_playlist_unless_dry_run(tmp_path, monkeypatch) -> None:
    side = _sync(tmp_path, monkeypatch, None, mode=None, nosync=True, resolves=lambda s: True,
                 fetched=AssertionError("fetched"))
    assert not side.exists()


def test_nightly_dry_run_failure_never_fails_the_sync(tmp_path, monkeypatch, caplog) -> None:
    def boom(song):
        raise RuntimeError("boom")

    _sync(tmp_path, monkeypatch, _result(songs=[song("A")]), resolves=boom)
    assert "Playlist store dry run failed for pl" in caplog.text
