"""The album_record stamps follow their album record's key (homelab#2478)."""

from __future__ import annotations

import contextlib

from music_fetch import state
from music_scan import restamp

RG = "6e0a6c2a-2d38-3d3c-9d38-4f0c2b5b7a11"


class FakeItem(dict):
    def __init__(self, id_=1, **fields):
        super().__init__(**fields)
        self.id, self.stored = id_, 0

    def get(self, key, default=None):
        return super().get(key, default)

    def store(self):
        self.stored += 1


class FakeLib:
    def __init__(self, items):
        self.items = items
        self._lib = self

    def all_items(self):
        return list(self.items)

    def transaction(self):
        return contextlib.nullcontext()


def _records(by_row: bool) -> dict[str, dict]:
    """Two albums as rows 17 and 18: a merged one (two editions) and a local one."""
    records = [
        {"_id": 17, "spotify_album_ids": ["sp1", "sp2"], "names": [], "release_group": RG,
         "artist": "Artist", "name": "Album"},
        {"_id": 18, "spotify_album_ids": [], "names": ["local/files"], "release_group": None,
         "artist": "local", "name": "files"},
    ]
    return {state.album_key(r, by_row): r for r in records}


def test_the_one_time_move_takes_every_old_key_to_its_row():
    items = [FakeItem(1, album_record="sp1"),  # the old key
             FakeItem(2, album_record="sp2"),  # a merged edition's, before #2478 never moved
             FakeItem(3, album_record="local/files"),
             FakeItem(4, album_record="17"),  # already moved
             FakeItem(5, album_record="gone"),  # a record deleted for good
             FakeItem(6)]
    moves, orphans = restamp.plan(items, _records(by_row=True))
    assert [(m.item.id, m.old, m.new) for m in moves] == [(1, "sp1", "17"), (2, "sp2", "17"),
                                                          (3, "local/files", "18")]
    assert orphans == 1
    restamp.apply(FakeLib(items), moves)
    assert [i.get("album_record") for i in items] == ["17", "17", "18", "17", "gone", None]
    assert [i.stored for i in items] == [1, 1, 1, 0, 0, 0]


def test_revert_takes_the_row_ids_back_to_the_old_keys():
    items = [FakeItem(1, album_record="17"), FakeItem(2, album_record="18"), FakeItem(3, album_record="sp1")]
    moves, _ = restamp.plan(items, _records(by_row=False))
    assert [(m.old, m.new) for m in moves] == [("17", "sp1"), ("18", "local/files")]


def test_a_gone_rows_items_move_to_the_row_holding_its_refs():
    """A merge's loser (row 9, its edition sp2 now on row 17) and an album that
    left its playlists and came back as a new row (row 5, now row 18)."""
    items = [FakeItem(1, album_record="9"), FakeItem(2, album_record="5"), FakeItem(3, album_record="4")]
    gone = {"9": ["sp2"], "5": ["local/files"], "4": ["sp-never-back"]}
    moves, orphans = restamp.plan(items, _records(by_row=True), gone)
    assert [(m.old, m.new) for m in moves] == [("9", "17"), ("5", "18")]
    assert orphans == 1


def test_a_value_two_records_claim_moves_nowhere():
    records = _records(by_row=True)
    records["18"]["spotify_album_ids"] = ["sp2"]  # never in the database (album_refs' key), but never guess
    moves, orphans = restamp.plan([FakeItem(1, album_record="sp2")], records)
    assert moves == [] and orphans == 1


def test_the_scan_moves_nothing_before_the_keys_are_row_ids():
    items = [FakeItem(1, album_record="sp1")]
    assert restamp.scan_step(FakeLib(items)) == 0
    assert items[0]["album_record"] == "sp1"


def test_apply_writes_beets_database_and_backup_copies_it(tmp_path):
    from beets.library import Item

    from music_scan.library import MusicLibrary

    db = tmp_path / "library.db"
    with MusicLibrary(db, tmp_path / "library") as lib:
        item = Item(path=str(tmp_path / "library" / "a.m4a"), title="A")
        item["album_record"] = "sp1"
        lib._lib.add(item)
        moves, _ = restamp.plan(lib.all_items(), _records(by_row=True))
        restamp.apply(lib, moves)
        copy = restamp.backup(db)
    with MusicLibrary(db, tmp_path / "library") as lib:
        assert [i.get("album_record") for i in lib.all_items()] == ["17"]
    assert copy.name.startswith("library.db.pre-rekey-")
    with MusicLibrary(copy, tmp_path / "library") as lib:
        assert [i.get("album_record") for i in lib.all_items()] == ["17"]  # taken after the write here
