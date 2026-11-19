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


# --- events (homelab#2590) ---


def test_a_merge_and_a_rekey_are_events_once_recorded(monkeypatch):
    recorded = []
    monkeypatch.setattr(resolve.events, "record", lambda kind, message, **kw: recorded.append((kind, message, kw)))
    rows = state.AlbumRows(albums={"sp1": _rec("sp1"), "sp2": _rec("sp2", status="imported"), "sp3": _rec("sp3")})
    report = resolve.resolve(rows, {"sp1": _cached(RG), "sp2": _cached(RG, "upc"), "sp3": _cached(RG2)}, {})
    assert recorded == []  # a dry run records nothing
    report.record_events()
    assert [(k, m.split(" ")[0]) for k, m, _ in recorded] == [
        ("album_merged", "[MERGE]"), ("spotify_id_resolved", "[REKEY]"), ("spotify_id_resolved", "[REKEY]")]
    (_, _, merged), (_, _, rekeyed), (_, _, other) = recorded
    assert merged["release_group"] == RG and merged["spotify_album_id"] == "sp2" and merged["source"] == "resolve"
    assert [a["key"] for a in merged["data"]["absorbed"]] == ["sp1"]
    assert merged["data"]["absorbed"][0]["spotify_album_ids"] == ["sp1"]
    assert rekeyed["data"]["key"] == "sp2" and rekeyed["data"]["via"] == "cache:upc"
    assert other["release_group"] == RG2 and other["data"]["via"] == "cache:url"


def _recorded(monkeypatch) -> list:
    recorded = []
    monkeypatch.setattr(resolve.events, "record", lambda kind, message, **kw: recorded.append((kind, message, kw)))
    return recorded


def test_a_rekey_carries_the_conflict_it_settled_with_no_row_of_its_own(monkeypatch):
    recorded = _recorded(monkeypatch)
    rows = state.AlbumRows(albums={"sp1": _rec("sp1", **_imported(RG))}, mixed=[])
    resolve.resolve(rows, {"sp1": _cached(RG2)}, {}).record_events()
    [(kind, message, kw)] = recorded
    assert (kind, kw["once_hours"]) == ("spotify_id_resolved", None)
    assert kw["data"]["conflict"] == f"the import's {RG} beats {RG2}" and RG2 not in message


def test_a_rekey_with_nothing_to_settle_has_no_conflict(monkeypatch):
    recorded = _recorded(monkeypatch)
    rows = state.AlbumRows(albums={"sp1": _rec("sp1")}, mixed=[])
    resolve.resolve(rows, {"sp1": _cached(RG)}, {}).record_events()
    assert "conflict" not in recorded[0][2]["data"]


def test_an_unresolved_conflict_is_a_daily_event_on_the_record(monkeypatch):
    recorded = _recorded(monkeypatch)
    rows = state.AlbumRows(albums={"sp1": _rec("sp1")}, mixed=[])
    resolve.resolve(rows, {"sp1": _cached(RG)}, {"sp1": {RG2}}).record_events()
    [(kind, message, kw)] = recorded
    assert (kind, kw["once_hours"], kw["spotify_album_id"], kw["release_group"]) == ("conflict", 24, "sp1", None)
    assert message.startswith("[CONFLICT] Artist — Album (sp1): ")
    assert kw["data"]["key"] == "sp1" and RG in kw["data"]["conflict"] and RG2 in kw["data"]["conflict"]
    assert rows.albums["sp1"]["unresolved"] == state.CONFLICT


def test_a_standing_conflict_repeats_its_message_so_once_hours_can_match_it(monkeypatch):
    recorded = _recorded(monkeypatch)
    rows = state.AlbumRows(albums={"sp1": _rec("sp1")}, mixed=[])
    for _ in range(2):
        resolve.resolve(rows, {"sp1": _cached(RG)}, {"sp1": {RG2}}).record_events()
    assert recorded[0][1] == recorded[1][1]


def test_an_absorbed_records_conflict_goes_on_the_merge(monkeypatch):
    recorded = _recorded(monkeypatch)
    rows = state.AlbumRows(albums={"sp1": _rec("sp1", release_group=RG, status="imported"),
                                   "sp2": _rec("sp2", **_imported(RG))}, mixed=[])
    resolve.resolve(rows, {"sp2": _cached(RG2)}, {}).record_events()
    [(kind, _, kw)] = recorded
    assert kind == "album_merged"
    assert kw["data"]["conflict"] == f"Artist — Album (sp2): the import's {RG} beats {RG2}"


def test_differing_overrides_go_on_the_merge_and_a_clean_merge_has_no_conflict(monkeypatch):
    recorded = _recorded(monkeypatch)
    cache = {"sp1": _cached(RG), "sp2": _cached(RG)}
    rows = state.AlbumRows(albums={"sp1": _rec("sp1"), "sp2": _rec("sp2")}, mixed=[])
    resolve.resolve(rows, cache, {}, {"sp1": "a b", "sp2": "c d"}).record_events()
    assert "search overrides differ (a b | c d)" in recorded[0][2]["data"]["conflict"]
    recorded.clear()
    rows = state.AlbumRows(albums={"sp1": _rec("sp1"), "sp2": _rec("sp2")}, mixed=[])
    resolve.resolve(rows, cache, {}).record_events()
    assert "conflict" not in recorded[0][2]["data"]


def test_a_conflict_waiting_on_an_import_records_nothing_until_it_lands(monkeypatch):
    recorded = _recorded(monkeypatch)
    rows = state.AlbumRows(albums={"sp1": _rec("sp1", release_group=RG, status="grabbed"),
                                   "sp2": _rec("sp2", **_imported(RG))}, mixed=[])
    report = resolve.resolve(rows, {"sp2": _cached(RG2)}, {})
    report.record_events()
    assert recorded == [] and rows.albums["sp2"]["unresolved"] == state.IN_FLIGHT and report.conflicts


def test_the_conflict_line_is_debug_unless_verbose(caplog):
    rows = state.AlbumRows(albums={"sp1": _rec("sp1", **_imported(RG))}, mixed=[])
    report = resolve.resolve(rows, {"sp1": _cached(RG2)}, {})
    with caplog.at_level("INFO", logger=resolve.logger.name):
        report.log()
        assert "[CONFLICT]" not in caplog.text
        report.log(verbose=True)
    assert "[CONFLICT]" in caplog.text


# --- regroup: a keyed record its import names another release group for (homelab#2691) ---

# Album 568's shape: keyed to the 1982 debut by its first, wrong import; the
# reimport's files are the 1993 album.
DEBUT = "a03d7530-7bd2-35f2-b49e-0f036ea3ecc4"
JANET = "785e1f49-f5b7-3ca3-a58e-e138a0e5e531"


def _keyed(*ids, group: str, files: str | None = None, marked: bool = True, **fields) -> dict:
    record = {**_rec(*ids, status="imported", **fields), "release_group": group, "unresolved": None}
    if files:
        record.update(_imported(files))
    if marked:
        record[resolve.REGROUP] = True
    return record


def _by_row(albums: dict[str, dict]) -> state.AlbumRows:
    """The store keyed by row id, as it is since the rekey: a numeric key is the record's ``_id``."""
    for key, record in albums.items():
        if key.isdigit():
            record["_id"] = int(key)
    return state.AlbumRows(albums=albums, by_row=True)


def test_a_reimport_moves_the_record_to_its_release_group(monkeypatch):
    recorded = _recorded(monkeypatch)
    rows = _by_row({"568": _keyed("sp1", group=DEBUT, files=JANET)})
    report = resolve.resolve(rows, {}, {"sp1": {DEBUT}})  # the library echoes the old import: no evidence
    record = rows.albums["568"]
    assert (record["release_group"], record["unresolved"]) == (JANET, None)
    assert resolve.REGROUP not in record
    assert report.regrouped == [f"Artist — Album (568): {DEBUT} → {JANET}"] and report.conflicts == []
    assert "1 regrouped" in report.summary()
    report.record_events()
    [(kind, message, kw)] = recorded
    assert (kind, kw["release_group"], kw["data"]["was"], kw["data"]["via"]) == ("album_regrouped", JANET, DEBUT, "import")
    assert message.startswith("[REGROUP] ") and "conflict" not in kw["data"]


def test_a_reimport_the_cache_agrees_with_regroups_without_a_conflict():
    rows = _by_row({"568": _keyed("sp1", group=DEBUT, files=JANET)})
    report = resolve.resolve(rows, {"sp1": _cached(JANET)}, {})
    assert rows.albums["568"]["release_group"] == JANET and report.conflicts == []


def test_cache_evidence_for_a_third_group_is_outranked_and_reported(monkeypatch):
    recorded = _recorded(monkeypatch)
    rows = _by_row({"568": _keyed("sp1", group=DEBUT, files=JANET)})
    resolve.resolve(rows, {"sp1": _cached(RG)}, {}).record_events()
    assert rows.albums["568"]["release_group"] == JANET
    assert recorded[0][2]["data"]["conflict"] == f"the import's {JANET} beats {RG}"


def test_a_group_the_cache_names_stays_and_the_import_is_a_conflict(monkeypatch):
    """homelab#2662's case: the record's group has evidence besides an import, so
    an import naming another is reported and acted on by nobody."""
    recorded = _recorded(monkeypatch)
    rows = _by_row({"862": _keyed("sp1", group=RG, files=RG2)})
    report = resolve.resolve(rows, {"sp1": _cached(RG, "isrc")}, {})
    assert rows.albums["862"]["release_group"] == RG and report.regrouped == []
    assert report.conflicts == [f"Artist — Album (862): the import's {RG2} is not its {RG}, which stays: "
                                f"the cache names {RG} (cache:isrc)"]
    report.record_events()
    [(kind, message, kw)] = recorded
    assert (kind, kw["release_group"], kw["once_hours"]) == ("conflict", RG, 24)
    assert message == f"[CONFLICT] {report.conflicts[0]}"
    # The mark is spent: the next pass doesn't report it again.
    assert resolve.REGROUP not in rows.albums["862"]
    assert resolve.resolve(rows, {"sp1": _cached(RG, "isrc")}, {}).conflicts == []


def test_a_group_another_row_has_is_not_taken():
    rows = _by_row({"568": _keyed("sp1", group=DEBUT, files=JANET),
                                   "900": _keyed("sp2", group=JANET, marked=False)})
    report = resolve.resolve(rows, {}, {})
    assert rows.albums["568"]["release_group"] == DEBUT and set(rows.albums) == {"568", "900"}
    assert report.conflicts == [f"Artist — Album (568): the import's {JANET} is not its {DEBUT}, which stays: "
                                f"Artist — Album (900) has {JANET}"]


def test_a_group_an_unkeyed_record_resolves_to_in_the_same_pass_is_not_taken():
    rows = _by_row({"568": _keyed("sp1", group=DEBUT, files=JANET), "sp2": _rec("sp2")})
    report = resolve.resolve(rows, {"sp2": _cached(JANET)}, {})
    assert rows.albums["568"]["release_group"] == DEBUT and set(rows.albums) == {"568", "sp2"}
    assert rows.albums["sp2"]["release_group"] == JANET
    assert "resolve to" in report.conflicts[0]


def test_the_freed_group_is_another_albums_not_the_regrouped_ones():
    """An unkeyed record the cache names the old group for is the album the wrong
    import was: it takes that group as a row of its own, not a merge."""
    rows = _by_row({"568": _keyed("sp1", group=DEBUT, files=JANET), "sp2": _rec("sp2")})
    resolve.resolve(rows, {"sp2": _cached(DEBUT)}, {})
    assert set(rows.albums) == {"568", "sp2"}
    assert (rows.albums["568"]["release_group"], rows.albums["sp2"]["release_group"]) == (JANET, DEBUT)


def test_a_requested_album_keeps_the_requests_group():
    rows = _by_row({"568": _keyed(group=DEBUT, files=JANET, requested=True, origin="request")})
    report = resolve.resolve(rows, {}, {})
    assert rows.albums["568"]["release_group"] == DEBUT
    assert "an album request names" in report.conflicts[0]


def test_an_album_grabbed_again_is_judged_once_that_import_lands():
    rows = _by_row({"568": _keyed("sp1", group=DEBUT, files=JANET, import_started="t")})
    report = resolve.resolve(rows, {}, {})
    assert rows.albums["568"]["release_group"] == DEBUT and report.regrouped == report.conflicts == []


def test_an_unmarked_record_is_judged_again_only_with_regroup_all():
    """The records keyed before an import marked them (713, 568 today): counted by
    ``music-album-resolve --regroup``, moved by its ``--apply``."""
    rows = _by_row({"568": _keyed("sp1", group=DEBUT, files=JANET, marked=False),
                                   "713": _keyed("sp2", group=RG, files=RG, marked=False)})
    assert resolve.resolve(rows, {}, {}).regrouped == []
    assert rows.albums["568"]["release_group"] == DEBUT
    report = resolve.resolve(rows, {}, {}, regroup_all=True)
    assert len(report.regrouped) == 1 and rows.albums["568"]["release_group"] == JANET
    assert rows.albums["713"]["release_group"] == RG  # its import agrees


def test_a_record_with_no_import_or_the_same_group_is_left_alone():
    rows = _by_row({"a": _keyed("sp1", group=RG), "b": _keyed("sp2", group=RG2, files=RG2)})
    report = resolve.resolve(rows, {}, {}, regroup_all=True)
    assert report.regrouped == report.conflicts == []
    assert [r["release_group"] for r in rows.albums.values()] == [RG, RG2]
