"""A value in the ISRC tag that isn't an ISRC is never one (homelab#2672).

``[PMEDIA]`` uploads carry their tag in the ISRC field, and a Qobuz rip its
``qobuz_<n>`` key: 25 unrelated items shared ``PMEDIA``, so
``music-audit-dupes`` called them one recording.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from contextlib import nullcontext
from pathlib import Path

import pytest

from music_scan import bad_isrcs, music_pipeline, process, scan
from music_scan.dupes import find_dupes
from music_scan.mb_fingerprint import aligned_isrcs


class FakeItem:
    """Enough of a beets Item for the audit and the clear."""

    def __init__(self, id_, title, artist="Artist", length=200.0, **data):
        self.id, self.title, self.artist, self.albumartist = id_, title, artist, artist
        self.length, self.bitrate = length, 256000
        self.path = f"/lib/{artist}/{title}-{id_}.m4a".encode()
        self.data = {"title": title, "artist": artist, **data}
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


def test_items_sharing_pmedia_or_a_qobuz_key_are_no_pair() -> None:
    """The audit's pairs: an ID shared is the evidence, and PMEDIA is none."""
    items = [FakeItem(1, "Cramps", isrc="PMEDIA"), FakeItem(2, "ASAP", isrc="PMEDIA", length=200.5),
             FakeItem(3, "Get Up", isrc="qobuz_123456"), FakeItem(4, "Butterfly", isrc="qobuz_123456")]
    certain, uncertain, _ = find_dupes(items, decoder=lambda fp: [])
    assert (certain, uncertain) == ([], [])


def test_aligning_a_recording_drops_a_value_that_isnt_an_isrc() -> None:
    item = FakeItem(1, "To You", isrc="PMEDIA")
    isrc, notes = aligned_isrcs(item, {"id": "rec", "isrcs": ["AUUM72200123"]}, None)
    assert isrc == "AUUM72200123"
    assert notes[0] == "PMEDIA goes: it isn't an ISRC"


def test_clear_dry_run_counts_and_changes_nothing(caplog) -> None:
    items = [FakeItem(1, "To You", isrc="PMEDIA"), FakeItem(2, "Track", isrc="qobuz_42;USSM10104366"),
             FakeItem(3, "Fine", isrc="USSM10104367"), FakeItem(4, "None")]
    caplog.set_level("INFO")
    assert bad_isrcs.clear(items, apply=False) == 2
    assert [i.get("isrc") for i in items] == ["PMEDIA", "qobuz_42;USSM10104366", "USSM10104367", None]
    assert not any(i.stored or i.written for i in items)
    assert "[ISRC] 2 Artist — Track: qobuz_42 isn't an ISRC, would be cleared" in caplog.text
    assert "2 item(s) hold a value in isrc that isn't an ISRC" in caplog.text


def test_clear_apply_writes_the_file_and_records_each_item(monkeypatch) -> None:
    recorded = []
    monkeypatch.setattr(bad_isrcs.events, "record", lambda kind, message, **kw: recorded.append((kind, message, kw)))
    items = [FakeItem(1, "To You", isrc="PMEDIA"), FakeItem(2, "Track", isrc="qobuz_42;USSM10104366"),
             FakeItem(3, "Fine", isrc="USSM10104367")]
    assert bad_isrcs.clear(items, apply=True) == 2
    assert [i.get("isrc") for i in items] == ["", "USSM10104366", "USSM10104367"]
    assert [(i.written, i.stored) for i in items] == [(True, True), (True, True), (False, False)]
    assert [(kind, kw["item_id"], kw["data"]["changed"]) for kind, _, kw in recorded] == [
        ("ids_written", 1, {"isrc": ["PMEDIA", None]}),
        ("ids_written", 2, {"isrc": ["qobuz_42;USSM10104366", "USSM10104366"]}),
    ]
    assert recorded[0][1] == "[IDS] 1 Artist — To You: isrc PMEDIA cleared, not an ISRC (music-clear-bad-isrcs)"
    assert all(kw["data"]["via"] == "music-clear-bad-isrcs" for *_, kw in recorded)
    assert bad_isrcs.clear(items, apply=True) == 0  # safe to re-run


ONE, TWO, THREE = "ZZAAA2600001", "ZZAAA2600002", "ZZBBB2600003"
"""Made-up ISRCs in the ISRC form."""


def _comma_items() -> list[FakeItem]:
    return [FakeItem(1, "Both", isrc=f"{ONE}, {TWO}"), FakeItem(2, "Packed", isrc=f"{ONE},{TWO};{THREE}"),
            FakeItem(3, "Mixed", isrc=f"{ONE}, PMEDIA"), FakeItem(4, "Twice", isrc=f"{TWO}, qobuz_9;{TWO}")]


def test_clear_dry_run_neither_lists_nor_counts_isrcs_joined_by_a_comma(caplog) -> None:
    """homelab#2672: two ISRCs in one entry, joined by a comma, were listed whole as a
    value that isn't an ISRC, and an apply would have deleted both."""
    items = _comma_items()
    caplog.set_level("INFO")
    assert bad_isrcs.clear(items, apply=False) == 2
    assert not any(i.stored or i.written for i in items)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[ISRC]")]
    assert lines == ["[ISRC] 3 Artist — Mixed: PMEDIA isn't an ISRC, would be cleared",
                     "[ISRC] 4 Artist — Twice: qobuz_9 isn't an ISRC, would be cleared"]
    assert "2 item(s) hold a value in isrc that isn't an ISRC" in caplog.text


def test_clear_apply_keeps_isrcs_joined_by_a_comma(monkeypatch) -> None:
    recorded = []
    monkeypatch.setattr(bad_isrcs.events, "record", lambda kind, message, **kw: recorded.append((kind, message, kw)))
    items = _comma_items()
    assert bad_isrcs.clear(items, apply=True) == 2
    assert [i.get("isrc") for i in items] == [f"{ONE}, {TWO}", f"{ONE},{TWO};{THREE}", ONE, TWO]
    assert [(i.written, i.stored) for i in items] == [(False, False), (False, False), (True, True), (True, True)]
    assert [(kw["item_id"], kw["data"]["changed"]) for _, _, kw in recorded] == [
        (3, {"isrc": [f"{ONE}, PMEDIA", ONE]}), (4, {"isrc": [f"{TWO}, qobuz_9;{TWO}", TWO]})]
    assert bad_isrcs.clear(items, apply=True) == 0


def test_aligning_a_recording_reads_isrcs_joined_by_a_comma() -> None:
    """Each ISRC of a comma-joined entry is judged alone: the recording's stays, none
    goes as "not an ISRC"."""
    item = FakeItem(1, "Both", isrc=f"{ONE}, {TWO}")
    isrc, notes = aligned_isrcs(item, {"id": "rec", "isrcs": [ONE]}, None)
    assert isrc == f"{ONE};{TWO}"
    assert notes == [f"{TWO} stays: not checked"]
    assert aligned_isrcs(item, {"id": "rec"}, None) == (f"{ONE}, {TWO}", [])  # looked up without ISRCs


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
            return [FakeItem(1, "To You", isrc="PMEDIA")]

    monkeypatch.setattr(library, "MusicLibrary", Lib)
    monkeypatch.setattr(bad_isrcs.events, "record", lambda *a, **kw: None)
    assert bad_isrcs.run(apply=False) == 1 and not taken
    assert bad_isrcs.run(apply=True) == 1 and taken == [1]


# --- the import, with real beets ---------------------------------------------

needs_tools = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("fpcalc") and shutil.which("beet")),
    reason="ffmpeg, fpcalc or beet not installed (the bare CI runner has no fpcalc); the dev image has all three",
)


def _m4a(path: Path, title: str, track: int, isrc: str, hz: int) -> Path:
    """A 20 s tone tagged well enough for an as-is import, its ISRC tag *isrc*."""
    from mediafile import MediaFile

    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={hz}:duration=20", "-c:a", "aac",
         "-metadata", f"title={title}", "-metadata", "artist=Test Artist", "-metadata", "album=Test Album",
         "-metadata", f"track={track}", str(path)],
        check=True,
    )
    tags = MediaFile(str(path))
    tags.isrc = isrc
    tags.save()
    return path


@pytest.fixture
def beets(tmp_path: Path, monkeypatch) -> Path:
    """A beets home for an as-is import with the plugin alone (no network), as
    ``test_fpcalc``'s; returns the library database."""
    home = tmp_path / "beets"
    home.mkdir()
    db = home / "library.db"
    (home / "config.yaml").write_text(textwrap.dedent(f"""\
        directory: {tmp_path / "library"}
        library: {db}
        import:
          quiet: yes
          move: yes
          singletons: yes
          autotag: no
          log: {home / "import.log"}
        pluginpath:
          - {Path(music_pipeline.__file__).parent}
        plugins: music_pipeline
        """))
    monkeypatch.setenv("BEETSDIR", str(home))
    monkeypatch.setattr(scan, "INBOX", tmp_path / "inbox")
    monkeypatch.setattr(scan, "LIBRARY_DB", db)
    monkeypatch.setattr(scan, "QUARANTINE", tmp_path / "quarantine")
    monkeypatch.setattr(process, "IMPORT_LOG", home / "import.log")
    return db


@needs_tools
def test_an_import_stores_no_pmedia_and_takes_it_off_the_file(tmp_path, beets) -> None:
    """An as-is import writes no tags of its own: the plugin writes the file, so
    the next scan's beet update doesn't read PMEDIA back."""
    from beets.library import Library
    from mediafile import MediaFile

    batch = tmp_path / "inbox" / "spotdl" / "p"
    _m4a(batch / "01 - Cramps.m4a", "Cramps", 1, "PMEDIA", 440)
    _m4a(batch / "02 - Yard.m4a", "Yard", 2, "PMEDIA;USSM10104366", 660)

    imported = scan.run_inbox_import()

    assert sorted(title for title, _ in imported) == ["Cramps", "Yard"]
    lib = Library(str(beets), directory=str(tmp_path / "library"))
    try:
        items = {item.title: item for item in lib.items()}
        assert {t: i.isrc for t, i in items.items()} == {"Cramps": "", "Yard": "USSM10104366"}
        assert {t: MediaFile(i.path).isrc or "" for t, i in items.items()} == {"Cramps": "", "Yard": "USSM10104366"}
        assert all(i.mtime for i in items.values())  # stored after the write: beet update leaves them
    finally:
        lib._close()
