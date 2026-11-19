"""The recording IDs beets stored as ``mb_releasetrackid`` are cleared (homelab#2624).

beets 2.14 maps ``release_track_id or track_id`` to ``mb_releasetrackid``, and
a singleton match has no release track, so every item imported before
music-pipeline#437 holds its recording ID there.
"""

from __future__ import annotations

import shutil
import subprocess
from contextlib import nullcontext
from pathlib import Path

import pytest

from music_scan import release_tracks

REC = "8892df94-8c42-4bbd-9fef-b9b37d68f20f"
OTHER_REC = "5019cf99-4659-4b09-bd19-dd8d31badfac"
RELEASE_TRACK = "73dddf6b-09fa-4bf2-8804-6f0029cf3fa4"


class FakeItem:
    """Enough of a beets Item for the clear."""

    def __init__(self, id_, title, artist="Artist", **data):
        self.id = id_
        self.data = {"title": title, "artist": artist, "path": f"/lib/{artist}/{title}-{id_}.m4a".encode(), **data}
        self.stored = self.written = False

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    def store(self):
        self.stored = True

    def try_write(self):
        self.written = True
        return True


def _items() -> list[FakeItem]:
    return [
        FakeItem(1, "Tigers Blood", mb_trackid=REC, mb_releasetrackid=REC),  # beets' shape
        FakeItem(2, "Oh! Sweet Nuthin'", mb_trackid=OTHER_REC, mb_releasetrackid=RELEASE_TRACK),  # a real one
        FakeItem(3, "Two Weeks", mb_trackid="", mb_releasetrackid=""),  # neither
        FakeItem(4, "Martha"),  # neither field at all
        FakeItem(5, "Recording only", mb_trackid=REC, mb_releasetrackid=""),
        FakeItem(6, "Release track only", mb_trackid="", mb_releasetrackid=RELEASE_TRACK),
    ]


def test_only_a_release_track_id_equal_to_the_items_recording_id_is_held() -> None:
    assert [release_tracks.holds_recording(i) for i in _items()] == [True, False, False, False, False, False]


def test_clear_dry_run_counts_first_and_changes_nothing(caplog) -> None:
    items = _items()
    caplog.set_level("INFO")
    assert release_tracks.clear(items, apply=False) == 1
    assert [i.get("mb_releasetrackid") for i in items] == [REC, RELEASE_TRACK, "", None, "", RELEASE_TRACK]
    assert not any(i.stored or i.written for i in items)
    lines = [r.getMessage() for r in caplog.records]
    assert lines[0] == "1 item(s) hold their recording ID in mb_releasetrackid"
    assert lines[1] == (f"[RELEASETRACK] 1 Artist — Tigers Blood: mb_releasetrackid {REC} is its recording ID, "
                        "would be cleared")
    assert lines[-1] == "Dry run — nothing cleared. Re-run with --apply."
    assert RELEASE_TRACK not in caplog.text


def test_clear_apply_writes_the_file_and_records_each_item(monkeypatch) -> None:
    recorded = []
    monkeypatch.setattr(release_tracks.events, "record", lambda kind, message, **kw: recorded.append((kind, message, kw)))
    items = [*_items(), FakeItem(7, "Song", mb_trackid=OTHER_REC, mb_releasetrackid=OTHER_REC,
                                 spotify_album_id="2n3HUMLmNl0Cm2atVwWSK6",
                                 mb_releasegroupid="f9aa9b53-2c89-4901-926e-e992f1e4d8c0")]
    assert release_tracks.clear(items, apply=True) == 2
    assert [i.get("mb_releasetrackid") for i in items] == ["", RELEASE_TRACK, "", None, "", RELEASE_TRACK, ""]
    assert [i.get("mb_trackid") for i in items] == [REC, OTHER_REC, "", None, REC, "", OTHER_REC]  # the recording stays
    assert [(i.written, i.stored) for i in items] == [(True, True), *[(False, False)] * 5, (True, True)]
    assert [(kind, kw["item_id"], kw["data"]["changed"]) for kind, _, kw in recorded] == [
        ("ids_written", 1, {"mb_releasetrackid": [REC, None]}),
        ("ids_written", 7, {"mb_releasetrackid": [OTHER_REC, None]}),
    ]
    assert recorded[0][1] == (f"[IDS] 1 Artist — Tigers Blood: mb_releasetrackid {REC} cleared, its recording ID "
                              "(music-clear-release-tracks)")
    assert all(kw["data"]["via"] == "music-clear-release-tracks" and kw["source"] == "backfill" for *_, kw in recorded)
    assert recorded[1][2]["release_group"] == "f9aa9b53-2c89-4901-926e-e992f1e4d8c0"
    assert recorded[1][2]["data"]["path"] == "/lib/Artist/Song-7.m4a"
    assert release_tracks.clear(items, apply=True) == 0  # safe to re-run


def test_a_long_apply_says_how_far_it_has_got(monkeypatch, caplog) -> None:
    monkeypatch.setattr(release_tracks, "PROGRESS_EVERY", 2)
    monkeypatch.setattr(release_tracks.events, "record", lambda *a, **kw: None)
    items = [FakeItem(n, f"Song {n}", mb_trackid=f"rec-{n}", mb_releasetrackid=f"rec-{n}") for n in range(1, 6)]
    caplog.set_level("INFO")
    assert release_tracks.clear(items, apply=True) == 5
    assert [r.getMessage() for r in caplog.records if r.getMessage().startswith("[PROGRESS]")] == [
        "[PROGRESS] 2/5 cleared", "[PROGRESS] 4/5 cleared"]


def test_a_file_that_cant_be_written_is_said(monkeypatch, caplog) -> None:
    monkeypatch.setattr(release_tracks.events, "record", lambda *a, **kw: None)
    item = FakeItem(1, "Tigers Blood", mb_trackid=REC, mb_releasetrackid=REC)
    item.try_write = lambda: False
    caplog.set_level("INFO")
    assert release_tracks.clear([item], apply=True) == 1
    assert item.stored and item.get("mb_releasetrackid") == ""
    assert "the file couldn't be written" in caplog.text


def test_run_takes_the_pipeline_lock_only_to_apply(monkeypatch) -> None:
    from music_scan import audit, library

    taken = []
    monkeypatch.setattr(audit, "pipeline_lock", lambda: taken.append(1) or nullcontext())

    class Lib:
        def __init__(self, path):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def all_items(self):
            return [FakeItem(1, "Tigers Blood", mb_trackid=REC, mb_releasetrackid=REC)]

    monkeypatch.setattr(library, "MusicLibrary", Lib)
    monkeypatch.setattr(release_tracks.events, "record", lambda *a, **kw: None)
    assert release_tracks.run(apply=False) == 1 and not taken
    assert release_tracks.run(apply=True) == 1 and taken == [1]


# --- a real file, with real beets ----------------------------------------------

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed; the dev image has it")


@needs_ffmpeg
def test_the_clear_takes_the_tag_off_the_file_so_beet_update_reads_none_back(tmp_path: Path, monkeypatch) -> None:
    """The value is a file tag (MusicBrainz Release Track Id): cleared in the
    database alone, the next scan's ``beet update`` would read it back."""
    from beets.library import Item, Library
    from mediafile import MediaFile

    monkeypatch.setattr(release_tracks.events, "record", lambda *a, **kw: None)
    path = tmp_path / "library" / "01 - Tigers Blood.m4a"
    path.parent.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-c:a", "aac", "-metadata", "title=Tigers Blood", str(path)], check=True)
    tags = MediaFile(str(path))
    tags.mb_trackid = tags.mb_releasetrackid = REC
    tags.save()

    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path / "library"))
    try:
        item = Item.from_path(str(path))
        assert item.mb_releasetrackid == REC
        lib.add(item)

        assert release_tracks.clear(lib.items(), apply=True) == 1

        assert not MediaFile(str(path)).mb_releasetrackid
        assert MediaFile(str(path)).mb_trackid == REC
        stored = lib.get_item(item.id)
        assert (stored.mb_trackid, stored.mb_releasetrackid) == (REC, "")
        assert stored.mtime  # stored after the write: beet update leaves it
        stored.read()  # what beet update does with a file newer than its row
        assert (stored.mb_trackid, stored.mb_releasetrackid) == (REC, "")
    finally:
        lib._close()
