"""music-audit-swapped-ids (homelab#2707): items holding a same-titled item's Spotify ID or ISRC."""

import sqlite3
import sys

import pytest

URL = "https://open.spotify.com/track/"


@pytest.fixture
def library(tmp_path):
    """A beets library of items with no files: the audit reads the database alone."""
    from beets.library import Item, Library

    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path / "library"))
    count = [0]

    def add(**fields) -> int:
        count[0] += 1
        item = Item(path=str(tmp_path / "library" / f"{count[0]}.m4a"),
                    **{"title": "Song", "artist": "Artist", "length": 200.0, "via": "spotdl", **fields})
        return lib.add(item)

    yield lib, add
    lib._close()


def _run(tmp_path):
    from music_scan import swapped_ids

    return swapped_ids.run(tmp_path / "library.db")


def _homes(add, other_length=241.0) -> tuple[int, int]:
    """The issue's reproduction: the first stored took the other's spotify_url, Spotify ID and ISRC."""
    victim = add(title="Home", artist="Artist", spotify_ids="AAA,BBB", spotify_url=URL + "BBB",
                 isrc="GBAAA2600001;GBBBB2600002", sources="222")
    other = add(title="HOME", artist="Other", length=other_length, spotify_ids="BBB", spotify_url=URL + "BBB",
                isrc="GBBBB2600002", sources="222")
    return victim, other


def test_the_issues_two_homes_are_certain_with_the_victims_fix(tmp_path, library, caplog):
    _, add = library
    victim, other = _homes(add)

    with caplog.at_level("INFO"):
        report = _run(tmp_path)

    (finding,) = report.findings
    assert finding.tier == "certain"
    assert finding.victim.id == victim and finding.other.id == other
    assert finding.spotify == ["BBB"] and finding.isrcs == ["GBBBB2600002"]
    text = caplog.text
    assert f"[SWAPPED] {victim} holds {other}'s Spotify ID BBB, ISRC GBBBB2600002" in text
    # The bug wrote the other's ID into spotify_url, which unlink_spotify_id refuses: said, not guessed.
    assert f"fix: Spotify ID BBB: no tool takes it off, as it is {victim}'s spotify_url" in text
    assert f"{victim}'s own: AAA" in text
    assert "fix: ISRC GBBBB2600002: no tool takes a valid ISRC off an item" in text
    assert "1 certain, 0 to check by hand, 0 pair(s)" in text


def test_a_victim_whose_spotify_url_is_its_own_gets_the_unlink_call(tmp_path, library, caplog):
    """After a merge the wrong ID can sit in spotify_ids alone: then unlink_spotify_id takes it off."""
    _, add = library
    victim = add(title="Home", artist="Artist", spotify_ids="AAA,BBB", spotify_url=URL + "AAA",
                 isrc="GBAAA2600001")
    add(title="Home", artist="Other", length=150.0, spotify_ids="BBB", spotify_url=URL + "BBB",
        isrc="GBBBB2600002")

    with caplog.at_level("INFO"):
        (finding,) = _run(tmp_path).findings
    assert finding.tier == "certain" and finding.victim.id == victim
    assert f'fix: unlink_spotify_id(id={victim}, track="BBB")' in caplog.text


def test_different_recordings_tell_two_tracks_apart_when_the_lengths_agree(tmp_path, library):
    _, add = library
    add(title="Home", artist="Artist", spotify_ids="AAA,BBB", spotify_url=URL + "BBB", mb_trackid="rec-a")
    add(title="Home", artist="Other", length=201.0, spotify_ids="BBB", spotify_url=URL + "BBB", mb_trackid="rec-b")
    (finding,) = _run(tmp_path).findings
    assert finding.tier == "certain"


def test_other_artists_at_one_length_are_checked_by_hand(tmp_path, library, caplog):
    """Different artists alone can be one recording spelled two ways: a second sign is needed."""
    _, add = library
    victim, other = _homes(add, other_length=201.0)
    with caplog.at_level("INFO"):
        (finding,) = _run(tmp_path).findings
    assert finding.tier == "check by hand" and finding.victim.id == victim
    assert f"[SWAPCHECK] {victim} ~ {other} share Spotify ID BBB, ISRC GBBBB2600002" in caplog.text
    assert f"compare_items(ids=[{victim}, {other}]) first" in caplog.text


def test_two_true_duplicates_of_one_track_are_not_certain(tmp_path, library, caplog):
    _, add = library
    # Downloaded twice for one entry: the same IDs, artist and length.
    add(title="Song", artist="Band", spotify_ids="T1", spotify_url=URL + "T1", isrc="USAAA0000001")
    add(title="Song", artist="Band", length=201.0, spotify_ids="T1", spotify_url=URL + "T1", isrc="USAAA0000001")
    # One holds the single's ID too: the signature's shape, but nothing says two tracks.
    single = add(title="Other Song", artist="Band", spotify_ids="ALBUM,SINGLE", spotify_url=URL + "ALBUM",
                 isrc="USAAA0000002")
    album = add(title="Other Song (Remastered)", artist="The Band", spotify_ids="ALBUM", spotify_url=URL + "ALBUM",
                isrc="USAAA0000002")

    with caplog.at_level("INFO"):
        report = _run(tmp_path)
    assert report.duplicates == 1
    (finding,) = report.findings
    assert finding.tier == "check by hand" and {finding.a.id, finding.b.id} == {single, album}
    assert "0 certain, 1 to check by hand, 1 pair(s)" in caplog.text


def test_two_ids_no_same_titled_item_shares_are_not_flagged(tmp_path, library):
    _, add = library
    add(title="Home", artist="Artist", spotify_ids="AAA,CCC", spotify_url=URL + "AAA", isrc="GBAAA2600001")
    add(title="Home", artist="Other", length=150.0, spotify_ids="BBB", spotify_url=URL + "BBB", isrc="GBBBB2600002")
    # Shares an ID, but under another title: not this bug.
    add(title="Away", artist="Other", length=150.0, spotify_ids="AAA", isrc="GBAAA2600001")
    report = _run(tmp_path)
    assert report.findings == [] and report.duplicates == 0
    assert report.scanned == 3 and report.groups == 1


def test_an_empty_library(tmp_path, library, caplog):
    with caplog.at_level("INFO"):
        report = _run(tmp_path)
    assert report.findings == [] and report.scanned == 0 and report.groups == 0
    assert "0 certain, 0 to check by hand, 0 pair(s)" in caplog.text


def test_a_value_that_isnt_an_isrc_is_never_shared(tmp_path, library):
    """A release's tag in the ISRC field names no recording (homelab#2672)."""
    _, add = library
    add(title="Home", artist="Artist", isrc="PMEDIA;GBAAA2600001")
    add(title="Home", artist="Other", length=150.0, isrc="PMEDIA")
    assert _run(tmp_path).findings == []


def test_ids_that_point_both_ways_are_checked_by_hand(tmp_path, library):
    """Each holds a Spotify ID the other doesn't: nothing says which took the other's."""
    _, add = library
    add(title="Home", artist="Artist", spotify_ids="AAA,BBB", spotify_url=URL + "AAA")
    add(title="Home", artist="Other", length=150.0, spotify_ids="BBB,CCC", spotify_url=URL + "BBB")
    (finding,) = _run(tmp_path).findings
    assert finding.tier == "check by hand" and finding.victim is None


def test_titles_group_by_their_words_without_edition_notes(tmp_path):
    from music_scan.swapped_ids import title_key

    assert title_key("HOME") == title_key("Home") == title_key("Home (2011 Remaster)") == title_key("Home!")
    assert title_key("Home") != title_key("Homes")
    assert title_key("家") == "家" and title_key("") == ""


def test_it_reads_by_id_ranges_from_a_read_only_database(tmp_path, library, monkeypatch):
    from music_scan import swapped_ids

    _, add = library
    for _ in range(5):
        add()
    monkeypatch.setattr(swapped_ids, "READ_BATCH", 2)
    opened, queries = [], []
    real = sqlite3.connect

    class Spy:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, *args):
            queries.append(sql)
            return self.conn.execute(sql, *args)

        def close(self):
            self.conn.close()

    monkeypatch.setattr(swapped_ids.sqlite3, "connect",
                        lambda database, **kw: opened.append(database) or Spy(real(database, **kw)))
    report = _run(tmp_path)
    assert opened and all(u.endswith("?mode=ro") for u in opened)
    assert sum("BETWEEN" in q for q in queries) == 3  # IDs 1-2, 3-4, 5
    assert not any("acoustid_fingerprint" in q for q in queries)
    assert report.scanned == 5 and report.groups == 1


def test_main_runs_the_audit(tmp_path, library, monkeypatch):
    from music_scan import library as library_module
    from music_scan import swapped_ids

    monkeypatch.setattr(library_module, "LIBRARY_DB", tmp_path / "library.db")
    monkeypatch.setattr(sys, "argv", ["music-audit-swapped-ids"])
    swapped_ids.main()
