"""Resolving the album store's key and merging editions (homelab#2410)."""

from __future__ import annotations

import sqlite3

from music_fetch import resolve, state

RG = "6e0a6c2a-2d38-3d3c-9d38-4f0c2b5b7a11"
RG2 = "0b4a7c4e-5d2f-4a51-8b1e-9a3c2d1e0f42"


def _rec(*ids, status="wanted", **fields) -> dict:
    return {"status": status, "release_group": None, "unresolved": state.PENDING,
            "spotify_album_ids": list(ids), "names": [], "name": "Album", "artist": "Artist", **fields}


def _cached(group: str | None, rung: str = "url") -> dict:
    return {"rung": "none"} if group is None else {"rung": rung, "fields": {"mb_releasegroupid": group}}


def _imported(group: str) -> dict:
    return {"release_files": {"mb": {"mb_releasegroupid": group}}}


# --- judge: the evidence ---


def test_the_import_wins_and_disagreement_is_a_conflict():
    v = resolve.judge(_rec("sp1", **_imported(RG)), {"sp1": _cached(RG2)}, {})
    assert (v.group, v.via) == (RG, resolve.IMPORT)
    assert RG2 in v.conflict


def test_cache_and_library_that_agree_resolve():
    v = resolve.judge(_rec("sp1"), {"sp1": _cached(RG, "isrc")}, {"sp1": {RG}})
    assert (v.group, v.via, v.conflict) == (RG, "cache:isrc+library", "")


def test_cache_and_library_that_disagree_stay_unresolved():
    v = resolve.judge(_rec("sp1"), {"sp1": _cached(RG)}, {"sp1": {RG2}})
    assert (v.group, v.unresolved) == (None, state.CONFLICT)


def test_a_mixed_library_album_is_no_evidence():
    assert resolve.judge(_rec("sp1"), {}, {"sp1": {RG, RG2}}).unresolved == state.PENDING


def test_two_spotify_ids_naming_two_groups_conflict():
    v = resolve.judge(_rec("sp1", "sp2"), {"sp1": _cached(RG), "sp2": _cached(RG2)}, {})
    assert v.unresolved == state.CONFLICT


def test_no_match_only_when_every_id_was_looked_up():
    assert resolve.judge(_rec("sp1"), {"sp1": _cached(None)}, {}).unresolved == state.NO_MATCH
    assert resolve.judge(_rec("sp1", "sp2"), {"sp1": _cached(None)}, {}).unresolved == state.PENDING
    assert resolve.judge({**_rec(), "names": ["a/b"]}, {}, {}).unresolved == state.PENDING


# --- resolve: rekeys and merges ---


def test_rekey_keeps_order_and_mixed():
    rows = state.AlbumRows(albums={"sp0": _rec("sp0"), "sp1": _rec("sp1"), "sp2": _rec("sp2")}, mixed=["sp1"])
    report = resolve.resolve(rows, {"sp1": _cached(RG)}, {})
    assert list(rows.albums) == ["sp0", "sp1", "sp2"]  # the key stays the primary Spotify ID
    assert rows.albums["sp1"]["release_group"] == RG and rows.albums["sp1"]["unresolved"] is None
    assert rows.mixed == ["sp1"]
    assert len(report.rekeyed) == 1 and len(report.flagged[state.PENDING]) == 2


def test_two_editions_merge_into_the_one_further_along():
    wanted = _rec("sp1", playlists={"later": [["a", "x", "s1", None, 1, 1]], "keep": [["c", "x", "s3", None, 1, 3]]},
                  blocklist=["r1"], attempts=1)
    held = _rec("sp2", status="imported", origin="library", blocklist=["r2"], attempts=2,
                playlists={"later": [["a", "x", "s1", None, 1, 1], ["b", "x", "s2", None, 1, 2]]})
    rows = state.AlbumRows(albums={"sp1": wanted, "sp2": held})
    report = resolve.resolve(rows, {"sp1": _cached(RG), "sp2": _cached(RG, "upc")}, {})
    assert list(rows.albums) == ["sp2"]
    merged = rows.albums["sp2"]
    assert merged["status"] == "imported" and merged["release_group"] == RG
    assert merged["spotify_album_ids"] == ["sp2", "sp1"]
    assert "origin" not in merged  # the album playlist's origin is the strongest
    assert merged["blocklist"] == ["r2", "r1"]
    assert merged["attempts"] == 2
    assert [t[2] for t in merged["playlists"]["later"]] == ["s1", "s2"]
    assert [t[2] for t in merged["playlists"]["keep"]] == ["s3"]
    assert report.absorbed == 1 and len(report.merged) == 1
    assert "1 rekeyed" in report.summary() and "1 merged into 1" in report.summary()


def test_a_newcomer_merges_into_the_row_already_keyed():
    keyed = {**_rec("sp1", status="missing"), "release_group": RG, "unresolved": None}
    rows = state.AlbumRows(albums={"sp1": keyed, "sp2": _rec("sp2", status="imported")})
    report = resolve.resolve(rows, {"sp2": _cached(RG)}, {})
    assert list(rows.albums) == ["sp1"]
    assert rows.albums["sp1"]["spotify_album_ids"] == ["sp1", "sp2"]
    assert rows.albums["sp1"]["status"] == "missing"  # the keyed row survives
    assert report.rekeyed == []


def test_no_merge_mid_import():
    keyed = {**_rec("sp1", status="grabbed"), "release_group": RG, "unresolved": None}
    rows = state.AlbumRows(albums={"sp1": keyed, "sp2": _rec("sp2")})
    report = resolve.resolve(rows, {"sp2": _cached(RG)}, {})
    assert set(rows.albums) == {"sp1", "sp2"}
    assert rows.albums["sp2"]["unresolved"] == state.IN_FLIGHT
    assert len(report.flagged[state.IN_FLIGHT]) == 1
    # Once the import is done, a second pass merges them.
    keyed["status"] = "imported"
    resolve.resolve(rows, {"sp2": _cached(RG)}, {})
    assert list(rows.albums) == ["sp1"]


def test_a_lone_album_mid_import_is_still_rekeyed():
    rows = state.AlbumRows(albums={"sp1": _rec("sp1", status="grabbed", import_started="t")})
    resolve.resolve(rows, {"sp1": _cached(RG)}, {})
    assert rows.albums["sp1"]["release_group"] == RG


def test_a_name_only_album_takes_its_release_group_as_key():
    rows = state.AlbumRows(albums={"a/b": {**_rec(), "names": ["a/b"], **_imported(RG)}})
    resolve.resolve(rows, {}, {})
    assert list(rows.albums) == [RG]


def test_differing_overrides_on_a_merge_are_a_conflict():
    rows = state.AlbumRows(albums={"sp1": _rec("sp1"), "sp2": _rec("sp2")})
    report = resolve.resolve(rows, {"sp1": _cached(RG), "sp2": _cached(RG)}, {}, {"sp1": "a b", "sp2": "c d"})
    assert any("search overrides differ" in c for c in report.conflicts)


def test_library_groups_reads_beets(tmp_path):
    path = tmp_path / "library.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, mb_releasegroupid TEXT)")
        conn.execute("CREATE TABLE item_attributes (id INTEGER PRIMARY KEY, entity_id INTEGER, key TEXT, value TEXT)")
        conn.executemany("INSERT INTO items VALUES (?, ?)", [(1, RG), (2, RG), (3, RG2), (4, ""), (5, RG)])
        conn.executemany("INSERT INTO item_attributes (entity_id, key, value) VALUES (?, ?, ?)", [
            (1, "spotify_album_id", "sp1"), (2, "spotify_album_id", "sp1"), (3, "spotify_album_id", "sp2"),
            (4, "spotify_album_id", "sp2"), (5, "sources", "later")])
    assert resolve.library_groups(path) == {"sp1": {RG}, "sp2": {RG2}}
