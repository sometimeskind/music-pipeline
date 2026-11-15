"""Album import hooks write the library only while holding the pipeline slot (#506).

beets' SQLite has one writer.  tag_album_ids and canon ran outside the slot,
raced a scan's beet import and hit 'database is locked'.  No Prefect server needed."""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock, patch

import pytest


class _Slot:
    """A stand-in for flows.hold: records whether the pipeline slot is held, and
    fails a second hold inside the first, which waits forever on a 1-slot limit."""

    def __init__(self) -> None:
        self.held = False

    @contextlib.contextmanager
    def hold(self, name: str, wait: float | None = None):
        assert name == "pipeline"
        assert not self.held, "pipeline held twice in one run: the inner hold would wait forever"
        self.held = True
        try:
            yield
        finally:
            self.held = False


def _writer(slot: _Slot, calls: list, name: str, result=0):
    def write(*args, **kwargs):
        calls.append((name, slot.held))
        return result
    return write


def test_every_library_write_of_an_album_import_holds_the_pipeline_slot():
    from music_service import flows

    slot, calls = _Slot(), []
    lib = MagicMock()
    lib.items_by_source.return_value = [MagicMock(added=200.0)]

    def fake_complete(state, completion, import_inbox, claim, add_source, tag_ids, retire, **kw):
        add_source("later", "mix", [])
        tag_ids("later", [], 100.0, 2, "1")
        return "imported"

    with patch("music_fetch.albums.complete", side_effect=fake_complete), \
         patch.object(flows, "hold", slot.hold), \
         patch.object(flows, "concurrency"), \
         patch.object(flows.scan, "add_source", side_effect=_writer(slot, calls, "add_source")), \
         patch.object(flows.scan, "tag_album_ids", side_effect=_writer(slot, calls, "tag_album_ids")), \
         patch.object(flows.scan, "regen_playlists"), \
         patch("music_scan.release.retire_held", side_effect=_writer(slot, calls, "retire_held", 1)), \
         patch("music_scan.canon.canonicalize_items", side_effect=_writer(slot, calls, "canon")), \
         patch("music_scan.supersede.after_import", side_effect=_writer(slot, calls, "supersede", 1)), \
         patch("music_scan.cover.covers_by_id", return_value={}), \
         patch("music_scan.cover.embed_covers", side_effect=_writer(slot, calls, "embed_covers", 1)), \
         patch("music_scan.navidrome.trigger_scan"):
        _, _, complete = flows._album_library_hooks(lib)
        assert complete(MagicMock(), MagicMock()) == "imported"

    assert calls == [("add_source", True), ("tag_album_ids", True), ("retire_held", True), ("canon", True),
                     ("supersede", True), ("embed_covers", True)]
    assert not slot.held


@pytest.mark.parametrize("holds", [True, False])
def test_have_links_the_album_s_items_only_while_holding_the_pipeline_slot(holds):
    """have's link of the items found outside the playlist is a write; a miss
    writes nothing and takes no slot."""
    from music_service import flows

    slot, calls = _Slot(), []
    record = {"playlists": {"later": [["One", "Artist", "S1", None, 1, 1]]}}
    with patch.object(flows, "hold", slot.hold), \
         patch.object(flows.scan, "have_album", return_value=holds), \
         patch.object(flows.scan, "have_or_link", side_effect=_writer(slot, calls, "link", True)):
        have, _, _ = flows._album_library_hooks(MagicMock())
        assert have("1", record) is holds

    assert calls == ([("link", True)] if holds else [])
