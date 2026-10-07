"""Playlist slots (#228): the resolver, its logging and the read-only audit."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from unittest import mock

import pytest

from music_scan.identity import BY_ID, BY_ISRC, BY_WORDS, ItemIndex, PlaylistTrack
from music_scan.playlists import (
    BY_SLOT_ID, BY_SLOT_ISRC, Entry, ReplacedFile, audit, collisions, empty_slots, keeper_candidates, playlist_entries,
    replaced_matches, resolve, tail_items,
)


def _item(title, path="x.m4a", **flex):
    it = mock.MagicMock(title=title, artist="Artist", albumartist="Artist")
    it.path = str(path).encode()
    it.get.side_effect = lambda k, d=None: flex.get(k, d)
    return it


def _song(name, song_id=None, isrc=None, url=None):
    song = {"name": name, "artists": ["Artist"], "url": url or f"https://open.spotify.com/track/{song_id or name}"}
    if song_id:
        song["song_id"] = song_id
    if isrc:
        song["isrc"] = isrc
    return song


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------


def test_entry_from_song_takes_the_id_from_the_url_when_song_id_is_absent() -> None:
    entry = Entry.from_song({"name": "Song", "artists": ["Artist"], "url": "https://open.spotify.com/track/ABC?si=1"})
    assert entry == Entry("Song", "Artist", "ABC", None)
    assert Entry.from_song(_song("Song", "SID", "GBX1")) == Entry("Song", "Artist", "SID", "GBX1")


def test_entry_from_track_reads_an_album_record_entry() -> None:
    assert Entry.from_track(PlaylistTrack.from_entry(["Song", "Artist", "SID", "GBX1", 1, 2])) == Entry("Song", "Artist", "SID", "GBX1")
    assert Entry.from_track(PlaylistTrack.from_entry(["Song", "Artist"])) == Entry("Song", "Artist", None, None)


def test_playlist_entries_reads_the_spotdl_in_order(tmp_path: Path) -> None:
    spotdl = tmp_path / "pl.spotdl"
    spotdl.write_text(json.dumps({"songs": [_song("B", "B1"), _song("A", "A1")]}), encoding="utf-8")
    assert [e.song_id for e in playlist_entries(spotdl)] == ["B1", "A1"]
    assert playlist_entries(tmp_path / "missing.spotdl") == []


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_resolve_takes_todays_path_first_then_the_whole_library() -> None:
    """A source item wins by every rung, words included; the library fills only
    the slots left empty, by ID then ISRC; an entry nothing holds stays empty."""
    tagged_id = _item("Tagged by id", "tagged-id.m4a", spotify_ids="T1")
    tagged_words = _item("Only words", "tagged-words.m4a")
    elsewhere_id = _item("Elsewhere", "elsewhere.m4a", spotify_ids="E1")
    elsewhere_isrc = _item("Elsewhere (remaster)", "elsewhere-isrc.m4a", isrc="GBX2")
    twin_words = _item("Only words", "twin.m4a", spotify_ids="W1")  # same words, outside the playlist
    source = ItemIndex([tagged_id, tagged_words])
    library = ItemIndex([tagged_id, tagged_words, elsewhere_id, elsewhere_isrc, twin_words])

    entries = [Entry.from_song(s) for s in (
        _song("Tagged by id", "T1"), _song("Only words", "W1"), _song("Elsewhere", "E1"),
        _song("Elsewhere", "E2", "GBX2"), _song("Nowhere", "N1", "GBX9"),
    )]
    slots = resolve(entries, source, library)

    assert [(s.item, s.rung) for s in slots] == [
        (tagged_id, BY_ID),
        (tagged_words, BY_WORDS),  # today's path beats the twin's exact ID: additive
        (elsewhere_id, BY_SLOT_ID),
        (elsewhere_isrc, BY_SLOT_ISRC),
        (None, None),
    ]
    assert [s.filled_from_library for s in slots] == [False, False, True, True, False]
    assert empty_slots(slots) == 1


def test_resolve_never_fills_from_the_library_by_words() -> None:
    library = ItemIndex([_item("Song", "song.m4a", spotify_ids="LIVE")])
    slots = resolve([Entry("Song", "Artist", "STUDIO", None)], ItemIndex([]), library)
    assert slots[0].item is None


def test_resolve_without_a_library_is_todays_path() -> None:
    tagged = _item("Song", "song.m4a", spotify_ids="S1")
    slots = resolve([Entry("Song", "Artist", "S1", None), Entry("Other", "Artist", "O1", None)], ItemIndex([tagged]))
    assert [s.item for s in slots] == [tagged, None]


def test_resolve_counts_source_rungs_on_the_source_index() -> None:
    """Callers that read ``ItemIndex.rungs`` (the [WORDS] rate) still can."""
    source = ItemIndex([_item("Song", "s.m4a", isrc="GBX1"), _item("Words", "w.m4a")])
    resolve([Entry("Song", "Artist", None, "GBX1"), Entry("Words", "Artist", "W1", None)], source, ItemIndex([]))
    assert source.rungs == {BY_ISRC: 1, BY_WORDS: 1}


# ---------------------------------------------------------------------------
# The audit: nothing disappears
# ---------------------------------------------------------------------------


def _regen_env(tmp_path: Path, items_by_source: dict[str, list], all_items: list):
    spotdl_dir, playlists_dir = tmp_path / "spotdl", tmp_path / "playlists"
    spotdl_dir.mkdir()
    playlists_dir.mkdir()
    lib = mock.MagicMock()
    lib.__enter__ = mock.MagicMock(return_value=lib)
    lib.__exit__ = mock.MagicMock(return_value=False)
    lib.items_by_source.side_effect = lambda name: items_by_source.get(name, [])
    lib.all_items.return_value = all_items
    patches = (
        mock.patch("music_scan.scan.SPOTDL_DIR", spotdl_dir),
        mock.patch("music_scan.scan.PLAYLISTS", playlists_dir),
        mock.patch("music_scan.library.LIBRARY_DB", tmp_path / "library.db"),
        mock.patch("music_scan.library.MusicLibrary", return_value=lib),
        mock.patch("music_scan.scan.MusicLibrary", return_value=lib),
    )
    return spotdl_dir, playlists_dir, patches


def test_audit_reports_the_slot_fills_and_no_loss(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Against the .m3u today's path wrote: the slots add lines and lose none."""
    lib_root = tmp_path / "library"
    tagged = _item("Tagged", lib_root / "tagged.m4a", spotify_ids="T1", sources="pl")
    words = _item("Words", lib_root / "words.m4a", sources="pl")
    extra = _item("Extra", lib_root / "extra.m4a", sources="pl")  # tagged, no entry: the alphabetical tail
    extra.id = 9
    elsewhere = _item("Elsewhere", lib_root / "elsewhere.m4a", spotify_ids="E1", sources="other")
    spotdl_dir, playlists_dir, patches = _regen_env(
        tmp_path, {"pl": [tagged, words, extra]}, [tagged, words, extra, elsewhere],
    )
    (spotdl_dir / "pl.spotdl").write_text(json.dumps({"songs": [
        _song("Tagged", "T1"), _song("Words", "W1"), _song("Elsewhere", "E1"), _song("Nowhere", "N1"),
    ]}), encoding="utf-8")
    rel = lambda p: os.path.relpath(p, playlists_dir)  # noqa: E731
    # What today's regen writes: the tagged items, entry order then alphabetical.
    (playlists_dir / "pl.m3u").write_text("\n".join(rel(p) for p in (
        lib_root / "tagged.m4a", lib_root / "words.m4a", lib_root / "extra.m4a")) + "\n", encoding="utf-8")

    with patches[0], patches[1], patches[2], patches[3], patches[4], \
         caplog.at_level(logging.INFO, logger="music_scan.playlists"):
        assert audit() == 0

    assert ("pl: on disk 3 | today's path 3 | with slots 4 | filled from library 1 | "
            "entries 4 = own file 3 + shared file 0 + empty 1 | tail lines 1 | empty in replaced 0") in caplog.text
    assert "~ item 9 extra.m4a: Artist — Extra  spotify_ids=- isrc=-  (tagged sources=pl, no entry matches it" in caplog.text
    assert f"+ {rel(lib_root / 'elsewhere.m4a')}  (slot:id: Artist — Elsewhere)" in caplog.text
    assert "No playlist loses an entry; every entry has its own file or is empty" in caplog.text
    assert "  - " not in caplog.text and "  = " not in caplog.text


def test_audit_counts_an_entry_the_file_on_disk_would_lose(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    lib_root = tmp_path / "library"
    tagged = _item("Tagged", lib_root / "tagged.m4a", spotify_ids="T1", sources="pl")
    spotdl_dir, playlists_dir, patches = _regen_env(tmp_path, {"pl": [tagged]}, [tagged])
    (spotdl_dir / "pl.spotdl").write_text(json.dumps({"songs": [_song("Tagged", "T1")]}), encoding="utf-8")
    (playlists_dir / "pl.m3u").write_text("../library/tagged.m4a\n../library/gone.m4a\n", encoding="utf-8")

    with patches[0], patches[1], patches[2], patches[3], patches[4], \
         caplog.at_level(logging.INFO, logger="music_scan.playlists"):
        assert audit() == 1

    assert "- ../library/gone.m4a  (on disk, resolves no more)" in caplog.text
    assert "1 entr(ies) would disappear" in caplog.text


def test_collisions_groups_the_entries_sharing_one_file() -> None:
    one = _item("Song", "/lib/one.m4a", spotify_ids="A,B")
    other = _item("Other", "/lib/other.m4a", spotify_ids="C")
    slots = resolve([Entry("Song", "Artist", "A", None), Entry("Other", "Artist", "C", None),
                     Entry("Song (single)", "Artist", "B", "GBX1"), Entry("Gone", "Artist", "Z", None)],
                    ItemIndex([one, other]))
    groups = collisions(slots)
    assert list(groups) == [Path("/lib/one.m4a")]
    assert [(pos, s.entry.song_id) for pos, s in groups[Path("/lib/one.m4a")]] == [(1, "A"), (3, "B")]


def test_audit_reports_entries_that_share_a_file(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Two entries resolving to one item (a #210 merge) are no loss against the
    collapsed .m3u, so the audit must list them itself (#232)."""
    lib_root = tmp_path / "library"
    merged = _item("Song", lib_root / "song.m4a", spotify_ids="A,B", isrc="GBX1", sources="pl")
    merged.id = 42
    spotdl_dir, playlists_dir, patches = _regen_env(tmp_path, {"pl": [merged]}, [merged])
    (spotdl_dir / "pl.spotdl").write_text(json.dumps({"songs": [_song("Song", "A"), _song("Song (single)", "B", "GBX1")]}),
                                          encoding="utf-8")
    (playlists_dir / "pl.m3u").write_text("../library/song.m4a\n", encoding="utf-8")

    with patches[0], patches[1], patches[2], patches[3], patches[4], \
         caplog.at_level(logging.INFO, logger="music_scan.playlists"):
        assert audit() == 0

    assert "entries 2 = own file 0 + shared file 2 + empty 0" in caplog.text
    assert "= ../library/song.m4a (item 42) is the file of 2 entries, written once:" in caplog.text
    assert "#1 Artist — Song  id=A isrc=- (id)" in caplog.text
    assert "#2 Artist — Song (single)  id=B isrc=GBX1 (id)" in caplog.text
    assert "No playlist loses an entry, but 2 entr(ies) in 1 group(s) share a file" in caplog.text


def test_tail_items_are_the_tagged_items_no_entry_resolves_to() -> None:
    tagged = _item("Song", "/lib/song.m4a", spotify_ids="S1")
    restored = _item("Calico", "/lib/calico.m4a", sources="wedding")
    slots = resolve([Entry("Song", "Artist", "S1", None)], ItemIndex([tagged, restored]))
    assert tail_items(slots, [tagged, restored]) == [restored]


def test_replaced_matches_and_keeper_candidates() -> None:
    """An empty entry whose ISRC a quarantine/replaced file carries is a merged-away
    track; the items sharing its ISRC, MusicBrainz id or words are the possible keepers."""
    # The merge kept the item but did not carry the loser's ISRC: the entry is empty.
    keeper = _item("Song", "/lib/song.m4a", spotify_ids="OTHER", isrc="GBX0", mb_trackid="mb-1")
    words_only = _item("Lost", "/lib/lost.m4a")
    library = ItemIndex([keeper, words_only])
    loser = ReplacedFile(Path("/q/replaced/77-Song.m4a"), "Song", "Artist", frozenset(), frozenset({"GBX1"}), "mb-1")
    lost_file = ReplacedFile(Path("/q/replaced/78-Lost.m4a"), "Lost", "Artist", frozenset({"L1"}), frozenset(), None)
    slots = resolve([Entry("Song", "Artist", "S2", "GBX1"), Entry("Lost", "Artist", "L1", None),
                     Entry("Here", "Artist", "OTHER", None), Entry("Nowhere", "Artist", "N1", "GBX9")],
                    ItemIndex([]), library)

    gone = replaced_matches(slots, [loser, lost_file])
    assert [(pos, rf.path.name) for pos, _, rf in gone] == [(1, "77-Song.m4a"), (2, "78-Lost.m4a")]
    assert keeper_candidates(loser, library) == [("mb_trackid mb-1", keeper)]
    assert keeper_candidates(lost_file, library) == [("title+artist words", words_only)]


def test_audit_reports_empty_entries_whose_file_was_replaced(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    lib_root = tmp_path / "library"
    keeper = _item("Song", lib_root / "song.m4a", spotify_ids="A", isrc="GBX1", sources="later")
    keeper.id = 7
    spotdl_dir, playlists_dir, patches = _regen_env(tmp_path, {"later": [keeper]}, [keeper])
    (spotdl_dir / "later.spotdl").write_text(json.dumps({"songs": [_song("Song", "A"), _song("Song (single)", "B", "GBX1X")]}),
                                             encoding="utf-8")
    replaced = tmp_path / "replaced"
    replaced.mkdir()
    (replaced / "77-Song (single).m4a").write_bytes(b"")
    tags = ReplacedFile(replaced / "77-Song (single).m4a", "Song (single)", "Artist", frozenset(), frozenset({"GBX1X"}), None)

    with patches[0], patches[1], patches[2], patches[3], patches[4], \
         mock.patch("music_scan.playlists._file_tags", return_value=tags), \
         caplog.at_level(logging.INFO, logger="music_scan.playlists"):
        assert audit(replaced_dir=replaced) == 0

    assert "entries 2 = own file 1 + shared file 0 + empty 1 | tail lines 0 | empty in replaced 1" in caplog.text
    assert "? #2 Artist — Song (single)  id=B isrc=GBX1X: empty, but 77-Song (single).m4a carries it (isrc=GBX1X mb_trackid=-)" in caplog.text
    assert "no library item shares its ISRC, MusicBrainz track id or words" in caplog.text
    assert "1 empty entr(ies) have their track in quarantine/replaced/" in caplog.text
