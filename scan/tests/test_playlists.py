"""Playlist slots (#228): entries and the resolver."""

from __future__ import annotations

from unittest import mock

from music_scan.identity import BY_ID, BY_ISRC, BY_WORDS, ItemIndex, PlaylistTrack
from music_scan.playlists import BY_SLOT_ID, BY_SLOT_ISRC, Entry, empty_slots, resolve


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


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_resolve_takes_the_source_index_first_then_the_whole_library() -> None:
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
        (tagged_words, BY_WORDS),  # the source index beats the twin's exact ID
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


def test_resolve_without_a_library_uses_the_source_index_only() -> None:
    tagged = _item("Song", "song.m4a", spotify_ids="S1")
    slots = resolve([Entry("Song", "Artist", "S1", None), Entry("Other", "Artist", "O1", None)], ItemIndex([tagged]))
    assert [s.item for s in slots] == [tagged, None]


def test_resolve_with_an_empty_source_is_the_store_view() -> None:
    """The store view (homelab#1914) resolves with an empty source index: every
    item comes from the whole library by ID or ISRC, a words-only item never."""
    by_id = _item("Song", "id.m4a", spotify_ids="S1")
    by_isrc = _item("Other (remaster)", "isrc.m4a", isrc="GBX1")
    words = _item("Words", "words.m4a")
    slots = resolve([Entry("Song", "Artist", "S1", None), Entry("Other", "Artist", "O1", "GBX1"),
                     Entry("Words", "Artist", "W1", None)], ItemIndex([]), ItemIndex([by_id, by_isrc, words]))
    assert [(s.item, s.rung) for s in slots] == [(by_id, BY_SLOT_ID), (by_isrc, BY_SLOT_ISRC), (None, None)]
    assert all(s.filled_from_library for s in slots[:2])
    assert empty_slots(slots) == 1


def test_resolve_counts_source_rungs_on_the_source_index() -> None:
    """Callers that read ``ItemIndex.rungs`` (the [WORDS] rate) still can."""
    source = ItemIndex([_item("Song", "s.m4a", isrc="GBX1"), _item("Words", "w.m4a")])
    resolve([Entry("Song", "Artist", None, "GBX1"), Entry("Words", "Artist", "W1", None)], source, ItemIndex([]))
    assert source.rungs == {BY_ISRC: 1, BY_WORDS: 1}
