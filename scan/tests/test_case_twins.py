"""music-audit-case-twins (homelab#2742): items whose paths differ only in case, read-only."""

import sqlite3
import sys
import unicodedata

import pytest

# Made-up fingerprints, decoded by a fake decoder: CI has no libchromaprint.
SAME = [0x0F0F0F0F] * 120
OTHER = [0x0F0F0F0F ^ 0xFFFFFFFF] * 120
FRAMES = {"AQAAsame": SAME, "AQAAsame2": SAME, "AQAAother": OTHER}


@pytest.fixture
def library(tmp_path):
    """A beets library of items with no files: the audit reads the database alone."""
    from beets.library import Item, Library

    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path / "library"))

    def add(path: str, **fields) -> int:
        item = Item(path=str(tmp_path / "library" / path), **{"title": "Song", "artist": "Artist", "length": 200.0,
                                                               **fields})
        return lib.add(item)

    yield lib, add
    lib._close()


def _run(tmp_path, decoder=FRAMES.__getitem__):
    from music_scan import case_twins

    return case_twins.run(tmp_path / "library.db", decoder=decoder)


def test_pairs_are_grouped_by_folded_path_and_judged_by_ids_and_audio(tmp_path, library, caplog):
    _, add = library
    spotdl = add("Hot Chip/In Our Heads/04 - Look At Where We Are.m4a", via="spotdl", spotify_ids="T1",
                 isrc="GBAAA1200001", acoustid_fingerprint="AQAAsame")
    usenet = add("Hot Chip/In Our Heads/04 - Look at Where We Are.m4a", via="usenet", album_record="17",
                 spotify_ids="T1", acoustid_fingerprint="AQAAsame2")
    master = add("Bob Dylan/Blood On The Tracks/02 - Simple Twist Of Fate.m4a", via="spotdl",
                 isrc="USSM1", acoustid_fingerprint="AQAAsame")
    other = add("Bob Dylan/Blood On The Tracks/02 - Simple Twist of Fate.m4a", via="usenet",
                isrc="USSM1", acoustid_fingerprint="AQAAother")
    folder = add("Magnetic Fields/Lose/01 - Song.m4a", acoustid_fingerprint="AQAAsame")
    folder_twin = add("Magnetic Fields/LOSE/01 - Song.m4a", length=260.0, mb_trackid="rec-1")
    add("Somebody/Else/01 - Song.m4a")  # no twin: not listed

    with caplog.at_level("INFO"):
        report = _run(tmp_path)
    assert report.scanned == 7
    groups = {tuple(i.id for i in g.items): g.pairs for g in report.groups}
    assert set(groups) == {(spotdl, usenet), (master, other), (folder, folder_twin)}
    (dup,), (masters,), (apart,) = groups[(spotdl, usenet)], groups[(master, other)], groups[(folder, folder_twin)]
    assert dup.shared == ["Spotify ID T1"] and dup.same_audio is True and dup.verdict == "duplicate"
    assert masters.shared == ["ISRC USSM1"] and masters.same_audio is False
    assert masters.verdict == "same ID, different audio"
    assert apart.shared == [] and apart.same_audio is None and apart.verdict == "not compared"
    assert f"{folder_twin}  Magnetic Fields/LOSE/01 - Song.m4a  (manual, 260s" in caplog.text
    assert f"{usenet}  Hot Chip/In Our Heads/04 - Look at Where We Are.m4a  (usenet, album record 17" in caplog.text
    assert f"{spotdl} ~ {usenet}: Spotify ID T1; same audio: fingerprint" in caplog.text
    assert "Case twins: 3 group(s), 6 item(s) of 7; pairs: 1 duplicate, 1 same ID, different audio, " \
           "1 not compared" in caplog.text


def test_distinct_recordings_and_same_audio_without_an_id(tmp_path, library):
    _, add = library
    a = add("A/B/01 - X.m4a", acoustid_fingerprint="AQAAsame", isrc="I1")
    b = add("a/b/01 - x.m4a", acoustid_fingerprint="AQAAother", isrc="I2")
    c = add("C/D/01 - Y.m4a", acoustid_fingerprint="AQAAsame")
    d = add("c/d/01 - y.m4a", acoustid_fingerprint="AQAAsame2")
    verdicts = {(p.a.id, p.b.id): p.verdict for g in _run(tmp_path).groups for p in g.pairs}
    assert verdicts == {(a, b): "distinct", (c, d): "same audio, no ID"}


def test_a_musicbrainz_recording_counts_only_with_lengths_that_agree(tmp_path, library, caplog):
    """homelab#2540: a sibling recording (a 5.1 mix, a live take) shares the ID at another length."""
    _, add = library
    add("A/B/01 - X.m4a", mb_trackid="rec-1")
    add("A/B/01 - x.m4a", mb_trackid="rec-1", length=201.0)
    add("C/D/01 - Y.m4a", mb_trackid="rec-2")
    add("C/D/01 - y.m4a", mb_trackid="rec-2", length=320.0)
    with caplog.at_level("INFO"):
        pairs = [p for g in _run(tmp_path).groups for p in g.pairs]
    assert [p.shared for p in pairs] == [["MusicBrainz recording rec-1"], []]
    assert "no ID agrees (MusicBrainz recording rec-2 agrees, but 120s apart)" in caplog.text


def test_paths_in_another_unicode_form_are_twins_and_non_ascii_case_is_not(tmp_path, library):
    """As Navidrome: NFC, and SQLite's NOCASE folds ASCII letters only."""
    _, add = library
    nfc = add(unicodedata.normalize("NFC", "A/Café/01 - X.m4a"))
    nfd = add(unicodedata.normalize("NFD", "A/Café/01 - X.m4a"))
    add("A/É/01 - X.m4a")
    add("A/é/01 - X.m4a")
    assert [[i.id for i in g.items] for g in _run(tmp_path).groups] == [[nfc, nfd]]


def test_a_clean_library_lists_nothing(tmp_path, library, caplog):
    _, add = library
    add("A/B/01 - X.m4a")
    with caplog.at_level("INFO"):
        report = _run(tmp_path, decoder=lambda fp: pytest.fail("nothing to compare"))
    assert report.groups == [] and "Case twins: 0 group(s), 0 item(s) of 1; pairs: none" in caplog.text


def test_it_holds_no_lock_while_it_compares(tmp_path, library):
    """homelab#2665: a read tool must not stop an import.  The database is closed before the
    comparisons, so a writer commits with no wait while they run."""
    _, add = library
    add("A/B/01 - X.m4a", acoustid_fingerprint="AQAAsame")
    add("a/b/01 - x.m4a", acoustid_fingerprint="AQAAsame2")
    writes = []

    def decoder(fp):
        conn = sqlite3.connect(tmp_path / "library.db", timeout=0)
        try:
            conn.execute("UPDATE items SET comments = 'written meanwhile'")
            conn.commit()
            writes.append(fp)
        finally:
            conn.close()
        return FRAMES[fp]

    assert _run(tmp_path, decoder=decoder).groups[0].pairs[0].verdict == "same audio, no ID"
    assert writes


def test_it_opens_the_database_read_only(tmp_path, library, monkeypatch):
    from music_scan import case_twins

    opened = []
    real = sqlite3.connect
    monkeypatch.setattr(case_twins.sqlite3, "connect", lambda database, **kw: opened.append(database) or real(database, **kw))
    _run(tmp_path)
    assert opened and all(u.endswith("?mode=ro") for u in opened)


def test_main_runs_the_audit(tmp_path, library, monkeypatch):
    from music_scan import case_twins, library as library_module

    monkeypatch.setattr(library_module, "LIBRARY_DB", tmp_path / "library.db")
    monkeypatch.setattr(sys, "argv", ["music-audit-case-twins"])
    case_twins.main()
