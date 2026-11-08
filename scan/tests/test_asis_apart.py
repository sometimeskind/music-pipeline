"""Tests for music_scan.asis_apart: the as-is pass's other recordings (homelab#2698)."""

from __future__ import annotations

import unittest.mock as mock
from pathlib import Path

import pytest

from music_scan import asis_apart

TRACK = "https://open.spotify.com/track/"


@pytest.fixture
def lib(tmp_path):
    from music_scan.library import MusicLibrary

    library = MusicLibrary(tmp_path / "library.db", tmp_path / "library")
    yield library._lib
    library._lib._close()


@pytest.fixture
def quarantined(clip, tmp_path):
    """Write a quarantined spotdl file tagged for the as-is pass."""

    def make(rel: str, sid: str, title: str = "Song", artist: str = "Artist", isrc: str | None = None) -> Path:
        from mutagen.mp4 import MP4, MP4FreeForm

        path = tmp_path / "quarantine" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        clip(path, ("tone", 1), url=TRACK + sid)
        audio = MP4(path)
        audio["\xa9nam"], audio["\xa9ART"], audio["\xa9alb"], audio["trkn"] = title, artist, "Album", [(1, 0)]
        if isrc:
            audio["----:spotdl:ISRC"] = [MP4FreeForm(isrc.encode())]
        audio.save()
        return path

    return make


def _add(lib, title: str = "Song", artist: str = "Artist", **flex):
    from beets.library import Item

    added = flex.pop("added", None)
    item = Item(title=title, artist=artist, album="Other", **{"length": 1.0, **flex})
    lib.add(item)
    if added is not None:  # add() stamps it
        item.added = added
        item.store()
    return item


def test_no_duplicate(lib, quarantined):
    _add(lib, title="Another Song")
    assert asis_apart.classify(lib, quarantined("spotdl/p/a.m4a", "NEW")).kind == asis_apart.NO_DUPLICATE


def test_duplicate_sharing_the_spotify_id_is_the_same_recording(lib, quarantined):
    _add(lib, via="spotdl", spotify_ids="NEW")
    assert asis_apart.classify(lib, quarantined("spotdl/p/a.m4a", "NEW")).kind == asis_apart.SAME


def test_duplicate_sharing_the_isrc_is_the_same_recording(lib, quarantined):
    _add(lib, via="usenet", isrc="GB0000000001")
    verdict = asis_apart.classify(lib, quarantined("spotdl/p/a.m4a", "NEW", isrc="GB0000000001"))
    assert verdict.kind == asis_apart.SAME


def test_duplicate_with_other_isrcs_is_split(lib, quarantined):
    dup = _add(lib, via="usenet", isrc="GB0000000002")
    verdict = asis_apart.classify(lib, quarantined("spotdl/p/a.m4a", "NEW", isrc="GB0000000001"))
    assert verdict.kind == asis_apart.SPLIT
    assert [d.id for d in verdict.duplicates] == [dup.id]


def test_duplicate_without_ids_is_apart(lib, quarantined):
    _add(lib, via="spotdl", spotify_ids="OLD")
    assert asis_apart.classify(lib, quarantined("spotdl/p/a.m4a", "NEW", isrc="GB0000000001")).kind \
        == asis_apart.APART


def test_move_apart_moves_only_other_recordings(lib, quarantined, tmp_path):
    _add(lib, title="Split", via="usenet", isrc="GB0000000002")
    _add(lib, title="Same", via="spotdl", spotify_ids="SAME")
    quarantine = tmp_path / "quarantine"
    split = quarantined("spotdl/p/split.m4a", "S1", title="Split", isrc="GB0000000001")
    same = quarantined("spotdl/p/same.m4a", "SAME", title="Same")
    fresh = quarantined("spotdl/p/fresh.m4a", "F1", title="Fresh")
    apart = tmp_path / "apart"

    moved = asis_apart.move_apart(quarantine, apart, lib)

    assert [v.kind for v in moved] == [asis_apart.SPLIT]
    assert (apart / "spotdl/p/split.m4a").exists() and not split.exists()
    assert same.exists() and fresh.exists()


def test_move_apart_moves_one_copy_of_a_recording(lib, quarantined, tmp_path):
    """Two staged copies sharing a Spotify ID are each apart from the library
    item; only the one with an ISRC moves, the other stays to be skipped."""
    _add(lib, via="spotdl", spotify_ids="OLD")
    quarantine = tmp_path / "quarantine"
    bare = quarantined("spotdl/p/a.m4a", "NEW")
    with_isrc = quarantined("spotdl/p/b.m4a", "NEW", isrc="GB0000000001")
    other = quarantined("spotdl/q/c.m4a", "ELSE")
    apart = tmp_path / "apart"

    moved = asis_apart.move_apart(quarantine, apart, lib)

    assert sorted(v.path for v in moved) == [apart / "spotdl/p/b.m4a", apart / "spotdl/q/c.m4a"]
    assert bare.exists() and not with_isrc.exists() and not other.exists()


def _verdict(sid, isrcs=(), bitrate=0, name="x.m4a", mb=None):
    from beets.library import Item

    from music_scan.music_pipeline import Incoming

    return asis_apart.Verdict(asis_apart.APART, Incoming(sid, frozenset(isrcs), mb, 200.0), (),
                              Item(title="Song", artist="Artist", bitrate=bitrate), Path(name))


def test_one_per_recording_prefers_an_isrc_then_the_bitrate():
    low, high = _verdict("A", bitrate=128000, name="low"), _verdict("A", bitrate=256000, name="high")
    isrc = _verdict("B", {"GB0000000001"}, bitrate=96000, name="isrc")
    bare = _verdict(None, {"GB0000000001"}, bitrate=320000, name="bare")
    alone = _verdict("C", name="alone")

    keep, leave = asis_apart.one_per_recording([low, high, bare, isrc, alone])

    assert [v.path.name for v in keep] == ["high", "bare", "alone"]
    assert [v.path.name for v in leave] == ["low", "isrc"]


def test_one_per_recording_needs_an_mb_recording_to_agree_in_length():
    a = _verdict(None, mb="rec-1", name="a")
    b = _verdict(None, mb="rec-1", name="b")
    far = _verdict(None, mb="rec-1", name="far")._replace(
        incoming=_verdict(None, mb="rec-1").incoming._replace(length=320.0))

    keep, leave = asis_apart.one_per_recording([a, b, far])

    assert [v.path.name for v in keep] == ["a", "far"] and [v.path.name for v in leave] == ["b"]


def test_record_kept_names_the_new_item(lib, monkeypatch):
    from music_scan.music_pipeline import Incoming

    recorded = []
    monkeypatch.setattr("music_fetch.events.record", lambda kind, message, **kw: recorded.append((kind, kw)))
    dup = _add(lib, via="usenet", isrc="GB0000000002", added=1.0)
    new = _add(lib, via="spotdl", spotify_url=TRACK + "NEW", spotify_ids="NEW", isrc="GB0000000001")
    missing = asis_apart.Verdict(asis_apart.APART, Incoming("GONE", frozenset(), None), (dup,))
    split = asis_apart.Verdict(asis_apart.SPLIT, Incoming("NEW", frozenset({"GB0000000001"}), None), (dup,))

    assert asis_apart.record_kept(lib, [missing, split], since=2.0) == 1
    (kind, kw), = recorded
    assert kind == "kept_apart" and kw["item_id"] == new.id
    assert kw["data"]["why"] == "ISRCs differ" and kw["data"]["duplicate"]["item_id"] == dup.id


def test_record_kept_gives_each_file_its_own_item(lib, monkeypatch, tmp_path):
    """Two files sharing a Spotify ID name two items, not the newest twice; a
    file with no ID in common names the new item with its title; a file still
    under the apart dir wasn't imported."""
    from beets.library import Item

    from music_scan.music_pipeline import Incoming

    recorded = []
    monkeypatch.setattr("music_fetch.events.record", lambda kind, message, **kw: recorded.append(kw["item_id"]))
    dup = _add(lib, via="spotdl", spotify_ids="OLD", added=1.0)
    # Not new: an older item that shares the ID.
    _add(lib, title="Elsewhere", via="spotdl", spotify_ids="NEW", added=1.0)
    first = _add(lib, via="spotdl", spotify_ids="NEW")
    second = _add(lib, via="spotdl", spotify_ids="NEW")
    loose = _add(lib)
    left = tmp_path / "apart/left.m4a"
    left.parent.mkdir()
    left.touch()
    file = Item(title="Song", artist="Artist")
    moved = [asis_apart.Verdict(asis_apart.APART, Incoming("NEW", frozenset(), None), (dup,), file),
             asis_apart.Verdict(asis_apart.APART, Incoming("NEW", frozenset(), None), (dup,), file),
             asis_apart.Verdict(asis_apart.APART, Incoming("LOOSE", frozenset(), None), (dup,), file),
             asis_apart.Verdict(asis_apart.APART, Incoming("LEFT", frozenset(), None), (dup,), file, left)]

    assert asis_apart.record_kept(lib, moved, since=2.0) == 3
    assert recorded == [first.id, second.id, loose.id]


def test_library_recordings_agree_with_the_duplicate_hook(lib):
    """The verdict's one index answers as the hook's per-file queries do."""
    from music_scan.identity import recording_ids
    from music_scan.music_pipeline import Incoming, _find_same_recording

    _add(lib, via="spotdl", spotify_ids="S1,S2")
    _add(lib, via="spotdl", spotify_url=TRACK + "S3")
    _add(lib, via="usenet", isrc="GB0000000001;GB0000000002")
    _add(lib, via="usenet", mb_trackid="rec-1", length=200.0)
    _add(lib, via="usenet", mb_trackid="rec-2", length=200.0)
    _add(lib, via="usenet", mb_trackid="rec-2", length=320.0)
    _add(lib, via="spotdl", spotify_ids="S10", isrc="GB0000000010")
    cases = [Incoming(sid, frozenset(isrcs), mb, length)
             for sid in (None, "S1", "S2", "S3", "S", "S10", "S4")
             for isrcs in ((), ("GB0000000002",), ("GB000000000",), ("GB0000000010", "GB0000000099"))
             for mb in (None, "rec-1", "rec-2", "rec-3")
             for length in (None, 200.0, 320.0)]
    held = asis_apart.library_recordings(lib)

    for incoming in cases:
        ids = recording_ids([incoming.spotify_id] if incoming.spotify_id else [], incoming.isrcs, incoming.mb_trackid)
        assert held.holds(ids, incoming.length) == bool(_find_same_recording(lib, incoming)), incoming


def test_import_runs_other_recordings_with_keep(tmp_path, monkeypatch):
    """The other recordings get their own as-is run, whose config keeps both."""
    from music_scan import scan

    quarantine = tmp_path / "quarantine"
    (quarantine / "spotdl/p").mkdir(parents=True)
    (quarantine / "spotdl/p/split.m4a").touch()
    (quarantine / "spotdl/p/plain.m4a").touch()
    monkeypatch.setattr(scan, "QUARANTINE", quarantine)

    def move_apart(staging, apart, lib):
        (apart / "spotdl/p").mkdir(parents=True)
        (staging / "spotdl/p/split.m4a").rename(apart / "spotdl/p/split.m4a")
        return [asis_apart.Verdict(asis_apart.SPLIT)]

    runs = []
    with mock.patch("mutagen.File", return_value={"title": ["t"], "artist": ["a"], "album": ["b"],
                                                   "tracknumber": ["1"]}), \
         mock.patch.object(asis_apart, "move_apart", side_effect=move_apart), \
         mock.patch.object(asis_apart, "record_kept", return_value=1) as record_kept, \
         mock.patch.object(scan, "MusicLibrary"), \
         mock.patch.object(scan, "run_beet_import",
                           side_effect=lambda d, asis, config: runs.append((sorted(p.name for p in d.rglob("*.m4a")),
                                                                            config.read_text()))):
        scan.import_asis_from_quarantine()

    assert runs == [(["plain.m4a"], scan._ASIS_CONFIG), (["split.m4a"], asis_apart.KEEP_CONFIG)]
    record_kept.assert_called_once()
    # beets imported neither in this test: both go back where they were.
    assert sorted(p.name for p in quarantine.rglob("*.m4a")) == ["plain.m4a", "split.m4a"]
