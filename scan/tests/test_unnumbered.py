"""Album items imported with no track number, placed by their fingerprint (homelab#2680).

The case is the library's: album 582, Prince — *Love Symbol*, imported from
``Prince-Love Symbol-1992-SataN INT``.  Its 18 files carry no track numbers,
so every item (22861–22878) imported at track 0, disc 0, with no ISRC and no
``mb_trackid``.  The record's release (``release_files``) is 132908e4; its
tracks 1-2, 1-7 and 1-9 are the recordings of the three wanted entries (Sexy
M.F. on ``cool`` and ``kinetic``, Blue Light and Sweet Baby on ``fancy``), as
``recording_candidates`` read them on the group's other release.  AcoustID and
MusicBrainz are fakes here.
"""

from __future__ import annotations

import contextlib
import os
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from music_scan import backfill, mb_fingerprint, unnumbered
from music_scan.mb_fingerprint import LookupFailed

needs_pg = pytest.mark.skipif(not os.environ.get("MUSIC_TEST_DATABASE_URL"),
                              reason="MUSIC_TEST_DATABASE_URL is unset")

RELEASE = "132908e4-1391-4dd0-bdef-824f9d3375ec"
GROUP = "764f7332-b637-3a71-ae3c-934cdfff3b40"
SEXY_MF = "5124f0c4-c3eb-49cd-a312-3a4ff43b542b"
BLUE_LIGHT = "9d78a745-3d92-4734-a37d-4cd7d89edd45"
SWEET_BABY = "73ebc9fa-0a01-419b-8a07-4262eaee6256"
ELSEWHERE = "11111111-2222-3333-4444-555555555555"  # a recording on no track of the release

# The release's tracklist, in order; 1-2, 1-7 and 1-9 are the wanted entries' recordings.
TITLES = ["My Name Is Prince", "Sexy M.F.", "Love 2 The 9's", "The Morning Papers", "The Max", "Phone Call 1",
          "Blue Light", "I Wanna Melt With U", "Sweet Baby", "The Continental", "Damn U", "Arrogance",
          "The Flow", "7", "And God Created Woman", "3 Chains O' Gold", "The Sacrifice of Victor", "Phone Call 2"]
WANTED = {2: SEXY_MF, 7: BLUE_LIGHT, 9: SWEET_BABY}
# The library's item for each title (22861 Sexy M.F., 22869 Blue Light, 22871 Sweet Baby).
ITEM_IDS = {"Sexy M.F.": 22861, "The Continental": 22862, "The Sacrifice of Victor": 22863,
            "My Name Is Prince": 22864, "Love 2 The 9's": 22865, "The Morning Papers": 22866, "The Max": 22867,
            "Phone Call 1": 22868, "Blue Light": 22869, "I Wanna Melt With U": 22870, "Sweet Baby": 22871,
            "Damn U": 22872, "Arrogance": 22873, "The Flow": 22874, "7": 22875, "And God Created Woman": 22876,
            "3 Chains O' Gold": 22877, "Phone Call 2": 22878}
ENTRIES = {"cool": [["Sexy M.F.", "Prince", "3DqvBu9MpNNz4UToUXDJ4u", "USWB19900587", 1, 2]],
           "fancy": [["Blue Light", "Prince", "13E2DOSIob68Mw1rt9sP86", "USWB19900592", 1, 7],
                     ["Sweet Baby", "Prince", "1485gA5QrMZ3bWbZpRiS5P", "USWB19900594", 1, 9]],
           "kinetic": [["Sexy M.F.", "Prince", "3DqvBu9MpNNz4UToUXDJ4u", "USWB19900587", 1, 2]]}
RECORD = {"status": "imported", "playlists": ENTRIES, "tracks_count": 18,
          "release_files": {"mb_albumid": RELEASE, "mb": {"mb_albumid": RELEASE, "mb_releasegroupid": GROUP}}}


def _recording(n: int) -> str:
    return WANTED.get(n) or f"00000000-0000-0000-0000-{n:012d}"


def _release(tracks=None) -> dict:
    tracks = tracks if tracks is not None else [(n, title, _recording(n)) for n, title in enumerate(TITLES, 1)]
    return {"id": RELEASE, "media": [{"position": 1, "track-count": len(tracks), "tracks": [
        {"position": n, "number": str(n), "title": title, "recording": {"id": rec, "title": title}}
        for n, title, rec in tracks]}]}


class FakeItem:
    """Enough of a beets Item: the fixed fields as attributes, ``get``/``[]``, store and write."""

    def __init__(self, id_, title, track=0, disc=0, **data):
        self.id, self.title, self.track, self.disc, self.tracktotal = id_, title, track, disc, 0
        self.artist = data.pop("artist", "Prince")
        self.added = data.pop("added", 1793911799.0)
        self.path = f"/lib/Prince/[Love Symbol]/00 - {id_}.mp3".encode()
        self.data = {"via": "usenet", "album_record": "582", "mb_trackid": "", "isrc": "", **data}
        self.stored = self.written = 0

    def get(self, key, default=None):
        if key in ("title", "artist", "track", "disc", "tracktotal"):
            return getattr(self, key)
        return self.data.get(key, default)

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    def store(self):
        self.stored += 1

    def try_write(self):
        self.written += 1
        return True


class FakeLib:
    def __init__(self, items):
        self.by_id = {i.id: i for i in items}

    def get_item(self, item_id):
        return self.by_id.get(item_id)

    def items_by_source(self, _source):
        return list(self.by_id.values())

    def all_items(self):
        return list(self.by_id.values())


def _love_symbol(**per_title):
    """Album 582's 18 items, as imported: track 0, disc 0, no ISRC, no recording."""
    return [FakeItem(ITEM_IDS[t], t, **per_title.get(t, {})) for t in TITLES]


def _lookup(table, fails=()):
    """AcoustID over *table* (item ID → recordings), raising for the item IDs in *fails*."""
    calls = []

    def lookup(path: str):
        item_id = int(path.rsplit(" ", 1)[1].split(".")[0])
        calls.append(item_id)
        if item_id in fails:
            raise LookupFailed("HTTP Error 503")
        return table.get(item_id, [])

    lookup.calls = calls
    return lookup


def _releases(release=None):
    calls = []

    def lookup(album):
        calls.append(album)
        return (release or _release()) if album == RELEASE else None

    lookup.calls = calls
    return lookup


# What AcoustID names for the three wanted files; it knows none of the others.
ACOUSTID = {22861: [(0.96, SEXY_MF)], 22869: [(0.98, BLUE_LIGHT), (0.40, ELSEWHERE)], 22871: [(0.97, SWEET_BABY)]}


def _events():
    with psycopg.connect(os.environ["MUSIC_DATABASE_URL"]) as conn:
        return conn.execute("SELECT kind, source, release_group::text, item_id, message, data "
                            "FROM events ORDER BY id").fetchall()


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch):
    monkeypatch.setattr(mb_fingerprint, "RETRY_SECONDS", 0)


# ---------------------------------------------------------------------------
# place: the rule
# ---------------------------------------------------------------------------


def test_an_agreeing_fingerprint_names_the_releases_slot_and_recording():
    item = FakeItem(22869, "Blue Light")
    assert unnumbered.place(item, [(0.98, BLUE_LIGHT), (0.40, ELSEWHERE)], _release()) == \
        ((1, 7, BLUE_LIGHT, 0.98, "Blue Light"), "")


@pytest.mark.parametrize("found, why", [
    ([(0.97, ELSEWHERE)], f"none of its AcoustID recordings ({ELSEWHERE}) is on release {RELEASE}"),
    ([], "AcoustID has nothing at 0.85 or more (no recording)"),
    ([(0.60, BLUE_LIGHT)], f"AcoustID has nothing at 0.85 or more (best {BLUE_LIGHT} 0.60)"),
])
def test_no_agreeing_fingerprint_places_nothing(found, why):
    assert unnumbered.place(FakeItem(22869, "Blue Light"), found, _release()) == (None, why)


def test_a_title_alone_places_nothing():
    """Blue Light at 1-7 by its title, but its audio names no recording of the release."""
    item = FakeItem(22869, "Blue Light")
    assert unnumbered.place(item, [(0.97, ELSEWHERE)], _release())[0] is None


def test_a_recording_on_several_tracks_of_the_release_places_nothing():
    tracks = [(n, t, _recording(n)) for n, t in enumerate(TITLES, 1)] + [(19, "Blue Light", BLUE_LIGHT)]
    hit, why = unnumbered.place(FakeItem(22869, "Blue Light"), [(0.98, BLUE_LIGHT)], _release(tracks))
    assert hit is None and why == f"its AcoustID recordings are on 2 tracks of release {RELEASE} (1-7, 1-19)"


def test_a_slot_of_another_song_is_suspect():
    hit, why = unnumbered.place(FakeItem(22869, "The Max"), [(0.98, BLUE_LIGHT)], _release())
    assert hit is None and why.startswith("[SUSPECT] AcoustID places it at 1-7")


def test_an_item_whose_recording_is_another_is_left_for_the_recheck():
    item = FakeItem(22869, "Blue Light", mb_trackid=ELSEWHERE)
    hit, why = unnumbered.place(item, [(0.98, BLUE_LIGHT)], _release())
    assert hit is None and "recheck_recordings settles that first" in why
    held = FakeItem(22869, "Blue Light", mb_trackid=BLUE_LIGHT)
    assert unnumbered.place(held, [(0.98, BLUE_LIGHT)], _release())[0] == (1, 7, BLUE_LIGHT, 0.98, "Blue Light")


# ---------------------------------------------------------------------------
# plan: which items are looked up
# ---------------------------------------------------------------------------


def test_a_numbered_item_is_not_looked_up_or_changed():
    numbered = FakeItem(22869, "Blue Light", track=7, disc=1)
    lookup = _lookup(ACOUSTID)
    assert unnumbered.unnumbered([numbered]) == []
    assert unnumbered.at_import(FakeLib([numbered]), "fancy", "582", 0.0, RELEASE, lock=contextlib.nullcontext,
                                lookup=lookup, release_lookup=_releases()) == []
    assert lookup.calls == [] and (numbered.track, numbered.disc, numbered.stored) == (7, 1, 0)


def test_only_a_records_release_items_count():
    spotdl = FakeItem(31756, "Sexy M.F.", via="spotdl")
    extra = FakeItem(9, "Sexy M.F.", album_extra="1")
    loose = FakeItem(10, "Sexy M.F.", album_record="")
    assert unnumbered.unnumbered([spotdl, extra, loose]) == []


def test_582s_shape_places_the_three_files_acoustid_knows_and_lists_the_rest(caplog):
    caplog.set_level("INFO")
    items = _love_symbol()
    lookup, releases = _lookup(ACOUSTID), _releases()
    placing = unnumbered.plan(unnumbered.unnumbered(items), lambda _: RELEASE, lookup, releases, siblings=items)
    assert [(p.item_id, p.disc, p.track, p.recording) for p in placing.placed] == [
        (22861, 1, 2, SEXY_MF), (22869, 1, 7, BLUE_LIGHT), (22871, 1, 9, SWEET_BABY)]
    assert placing.unnumbered == 18 and len(placing.left) == 15 and not placing.failed
    assert sorted(lookup.calls) == sorted(ITEM_IDS.values())
    unnumbered.report(placing)
    assert (f"[WOULD-PLACE] 582: 22869 → 1-7, recording {BLUE_LIGHT} (AcoustID 0.98: 'Blue Light' on release "
            f"{RELEASE})") in caplog.text
    assert "[NOPLACE] 582: 22862 — The Continental: AcoustID has nothing at 0.85 or more (no recording); " \
           "left at track 0" in caplog.text
    assert "Unnumbered: 18 album item(s) at track 0, 3 to place by AcoustID, 15 left at track 0" in caplog.text
    assert all(i.track == 0 and not i.stored for i in items), "a plan changes nothing"


def test_two_items_on_one_slot_and_a_slot_held_already_place_neither():
    one, two = FakeItem(22869, "Blue Light"), FakeItem(22880, "Blue Light")
    held = FakeItem(22871, "Sweet Baby", track=9, disc=1)
    lone = FakeItem(22881, "Sweet Baby")
    lookup = _lookup({22869: [(0.98, BLUE_LIGHT)], 22880: [(0.97, BLUE_LIGHT)], 22881: [(0.97, SWEET_BABY)]})
    items = [one, two, held, lone]
    placing = unnumbered.plan(unnumbered.unnumbered(items), lambda _: RELEASE, lookup, _releases(), siblings=items)
    assert placing.placed == []
    assert [(i, why) for _, i, _, why in placing.left] == [
        (22869, "AcoustID places it at 1-7, and so it does item(s) 22880"),
        (22880, "AcoustID places it at 1-7, and so it does item(s) 22869"),
        (22881, "AcoustID places it at 1-9, which item(s) 22871 hold already")]


def test_a_failed_lookup_ends_the_lookups():
    items = _love_symbol()
    lookup = _lookup(ACOUSTID, fails=(22861,))
    placing = unnumbered.plan(unnumbered.unnumbered(items), lambda _: RELEASE, lookup, _releases(), siblings=items)
    assert placing.failed and placing.placed == []
    assert lookup.calls == [22861, 22861], "tried once more, then no other item is looked up"
    assert len(placing.left) == 18


def test_no_release_and_an_unknown_release_place_nothing():
    item = FakeItem(22869, "Blue Light")
    lookup = _lookup(ACOUSTID)
    none = unnumbered.plan([item], lambda _: None, lookup, _releases())
    unknown = unnumbered.plan([item], lambda _: "ffffffff-0000-0000-0000-000000000000", lookup, _releases())
    assert none.placed == unknown.placed == [] and lookup.calls == []
    assert "no release is known" in none.left[0][3] and "MusicBrainz has no release" in unknown.left[0][3]


# ---------------------------------------------------------------------------
# store and the import
# ---------------------------------------------------------------------------


@needs_pg
def test_an_agreeing_fingerprint_sets_the_number_and_recording_with_its_event():
    items = _love_symbol()
    lib = FakeLib(items)
    placing = unnumbered.plan(unnumbered.unnumbered(items), lambda _: RELEASE, _lookup(ACOUSTID), _releases(),
                              siblings=items)
    assert unnumbered.store(lib, placing.placed, lock=contextlib.nullcontext) == [22861, 22869, 22871]
    blue = lib.get_item(22869)
    assert (blue.disc, blue.track, blue["mb_trackid"], blue.stored, blue.written) == (1, 7, BLUE_LIGHT, 1, 1)
    assert all(i.track == 0 and not i.stored for i in items if i.id not in (22861, 22869, 22871))
    events = _events()
    assert [(e[0], e[1], e[3]) for e in events] == [("track_placed", "albums", i) for i in (22861, 22869, 22871)]
    _, _, _, _, message, data = events[1]
    assert message == (f"[PLACED] 582: 22869 — Blue Light is 1-7 of release {RELEASE} by AcoustID (0.98); "
                       f"mb_trackid → {BLUE_LIGHT}")
    assert (data["key"], data["release"], data["old"], data["new"], data["disc"], data["track"]) == \
        ("582", RELEASE, None, BLUE_LIGHT, 1, 7)


@needs_pg
def test_an_item_changed_since_its_lookup_is_skipped():
    stale = FakeItem(22869, "Blue Light")
    placing = unnumbered.plan([stale], lambda _: RELEASE, _lookup(ACOUSTID), _releases())
    numbered = FakeItem(22869, "Blue Light", track=3)
    other = FakeItem(22869, "Blue Light", mb_trackid=ELSEWHERE)
    for fresh in (numbered, other, None):
        lib = FakeLib([fresh] if fresh else [])
        assert unnumbered.store(lib, placing.placed, lock=contextlib.nullcontext) == []
    assert (numbered.track, other["mb_trackid"], numbered.stored, other.stored) == (3, ELSEWHERE, 0, 0)
    assert _events() == []


@needs_pg
def test_the_import_looks_up_outside_the_slot_and_writes_under_it():
    held: list[bool] = []
    during: list[bool] = []

    @contextlib.contextmanager
    def lock():
        held.append(True)
        yield
        held.append(False)

    table = _lookup(ACOUSTID)

    def lookup(path):
        during.append(bool(held) and held[-1])
        return table(path)

    items = _love_symbol()
    stored = unnumbered.at_import(FakeLib(items), "fancy", "582", 0.0, RELEASE, lock=lock, lookup=lookup,
                                  release_lookup=_releases())
    assert stored == [22861, 22869, 22871] and held == [True, False] and not any(during)


def test_an_import_with_no_release_identified_looks_nothing_up(caplog):
    caplog.set_level("INFO")
    lookup = _lookup(ACOUSTID)
    assert unnumbered.at_import(FakeLib(_love_symbol()), "fancy", "582", 0.0, None, lock=contextlib.nullcontext,
                                lookup=lookup, release_lookup=_releases()) == []
    assert lookup.calls == [] and "[NOPLACE] 582: 18 item(s) at track 0 and no release identified" in caplog.text


def test_an_earlier_imports_items_are_not_the_imports():
    old = FakeItem(22869, "Blue Light", added=100.0)
    lookup = _lookup(ACOUSTID)
    assert unnumbered.at_import(FakeLib([old]), "fancy", "582", 200.0, RELEASE, lock=contextlib.nullcontext,
                                lookup=lookup, release_lookup=_releases()) == []
    assert lookup.calls == []


# ---------------------------------------------------------------------------
# The number rung after a placement: 582's wanted entries (backfill --albums)
# ---------------------------------------------------------------------------


def test_582s_wanted_entries_link_once_placed_and_not_before():
    """Before: every entry is [NOID].  Placed in memory (the dry run's overlay), the
    number rung links each wanted entry by its slot and title.  Sexy M.F. is held
    by spotdl's copy 31756 by Spotify ID, so its entry is not the release item's."""
    items = _love_symbol()
    before = backfill.plan_album_ids(items, {"582": RECORD}, lambda _: None)
    assert before.links == [] and len(before.skips) == 3

    placing = backfill.plan_unnumbered(items, {"582": RECORD}, lookup=_lookup(ACOUSTID), release_lookup=_releases())
    unnumbered.overlay(items, placing.placed)
    after = backfill.plan_album_ids(items, {"582": RECORD}, lambda _: None)
    assert sorted((k.track.name, k.item.id) for k in after.links) == [
        ("Blue Light", 22869), ("Sexy M.F.", 22861), ("Sweet Baby", 22871)]

    items = _love_symbol()
    spotdl = FakeItem(31756, "Sexy M.F.", track=2, disc=1, via="spotdl", album_record="",
                      spotify_ids="3DqvBu9MpNNz4UToUXDJ4u", isrc="USWB19900587")
    placing = backfill.plan_unnumbered([*items, spotdl], {"582": RECORD}, lookup=_lookup(ACOUSTID),
                                       release_lookup=_releases())
    unnumbered.overlay(items, placing.placed)
    held = backfill.plan_album_ids([*items, spotdl], {"582": RECORD}, lambda _: None)
    assert sorted((k.track.name, k.item.id) for k in held.links) == [("Blue Light", 22869), ("Sweet Baby", 22871)]


def test_a_title_alone_still_links_nothing_after_the_placement():
    """Blue Light's file names no recording of the release: it stays at track 0 and its entry empty."""
    items = _love_symbol()
    acoustid = {k: v for k, v in ACOUSTID.items() if k != 22869}
    placing = backfill.plan_unnumbered(items, {"582": RECORD}, lookup=_lookup(acoustid), release_lookup=_releases())
    unnumbered.overlay(items, placing.placed)
    plan = backfill.plan_album_ids(items, {"582": RECORD}, lambda _: None)
    assert "Blue Light" not in {k.track.name for k in plan.links}
    assert next(i for i in items if i.id == 22869).track == 0


def test_backfill_reads_the_records_release_first_then_the_items():
    item = FakeItem(22869, "Blue Light", mb_albumid="76a378e6-f85b-48d4-98b6-457cd726c51b")
    seen = _releases()
    backfill.plan_unnumbered([item], {"582": RECORD}, lookup=_lookup(ACOUSTID), release_lookup=seen)
    backfill.plan_unnumbered([item], {"582": {**RECORD, "release_files": None}}, lookup=_lookup(ACOUSTID),
                             release_lookup=seen)
    assert seen.calls == [RELEASE, "76a378e6-f85b-48d4-98b6-457cd726c51b"]


def test_backfill_looks_nothing_up_without_an_item_at_track_0_or_for_a_record_not_imported():
    lookup = _lookup(ACOUSTID)
    numbered = FakeItem(22869, "Blue Light", track=7, disc=1)
    assert backfill.plan_unnumbered([numbered], {"582": RECORD}, lookup=lookup).placed == []
    waiting = FakeItem(22869, "Blue Light")
    assert backfill.plan_unnumbered([waiting], {"582": {**RECORD, "status": "grabbed"}}, lookup=lookup).placed == []
    assert lookup.calls == []


@pytest.mark.parametrize("apply", [False, True])
def test_run_albums_places_only_on_an_apply_and_links_what_it_placed(apply, monkeypatch):
    items = _love_symbol()
    lib = FakeLib(items)
    monkeypatch.setattr(unnumbered, "acoustid", lambda: _lookup(ACOUSTID))
    with patch("music_fetch.state.session"), \
            patch("music_fetch.state.load_albums", return_value=MagicMock(albums={"582": RECORD})), \
            patch("music_scan.library.MusicLibrary") as library, \
            patch("music_scan.mb_fingerprint.musicbrainz_releases", return_value=_releases()), \
            patch("music_scan.audit.pipeline_lock", contextlib.nullcontext), \
            patch("music_scan.unnumbered.store", wraps=lambda lib_, placed, **kw: [
                setattr(lib_.get_item(p.item_id), "track", p.track) or p.item_id for p in placed]) as store, \
            patch("music_scan.navidrome.trigger_scan") as rescan, \
            patch("music_scan.scan.regen_playlists", return_value={}), \
            patch("music_scan.backfill.events.record"):
        library.return_value.__enter__.return_value = lib
        plan = backfill.run_albums(apply=apply)
    assert store.called is apply and rescan.called is apply
    assert sorted(k.item.id for k in plan.links) == [22861, 22869, 22871]
