"""A request's items without a recording ID, placed on its tracklist by AcoustID (homelab#2589).

The case is the library's: key 162, A.A.L. (Against All Logic) — *2012–2017*.
Its request's tracklist release has 11 tracks; item 61, *Such a Bad Way*, a
spotdl download of the 10-track Spotify edition, is track 5 there and track 6
on the tracklist, and has no ``mb_trackid``.  Item 458 is the same case.
"""

from __future__ import annotations

import os

import psycopg
import pytest

from music_scan import request_fill
from music_scan.mb_fingerprint import LookupFailed

needs_pg = pytest.mark.skipif(not os.environ.get("MUSIC_TEST_DATABASE_URL"),
                              reason="MUSIC_TEST_DATABASE_URL is unset")

MB = "https://musicbrainz.org/recording/"
KNOW_YOU = "c4ce3e06-61d9-42d3-9d8e-9e92d74b493b"
SUCH_A_BAD_WAY = "d60e6315-b2fb-417c-85f3-7f3ea7421161"
SCREAM = "a95d2516-336e-4bec-839b-5a23d42e444b"
ELSEWHERE = "11111111-2222-3333-4444-555555555555"  # a recording on no track of the request
TRACKLIST = [(5, "Know You", KNOW_YOU), (6, "Such a Bad Way", SUCH_A_BAD_WAY),
             (10, "You Are Going to Love Me and Scream", SCREAM)]
RECORD = {"artist": "A.A.L. (Against All Logic)", "name": "2012–2017",
          "release_group": "20f7f4bb-c9cb-424e-9106-cbd41a00e18f",
          "album_songs": [{"name": name, "track_number": n, "song_id": rec, "url": MB + rec}
                          for n, name, rec in TRACKLIST]}


class FakeItem:
    """Enough of a beets Item: ``get``/``[]``, ``id``, ``path``, store and write."""

    def __init__(self, id_, title, **data):
        self.id, self.path = id_, f"/lib/{title}-{id_}.m4a".encode()
        self.data = {"title": title, **data}
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


class FakeLib:
    def __init__(self, items):
        self.by_id = {i.id: i for i in items}

    def get_item(self, item_id):
        return self.by_id.get(item_id)


def _lookup(table, fails=()):
    """AcoustID over *table* (item ID → recordings), raising for the item IDs in *fails*."""
    calls = []

    def lookup(path: str):
        item_id = int(path.rsplit("-", 1)[1].split(".")[0])
        calls.append(item_id)
        if item_id in fails:
            raise LookupFailed("HTTP Error 503")
        return table.get(item_id, [])

    lookup.calls = calls
    return lookup


def _item_61(**data):
    return FakeItem(61, "Such a Bad Way", track=5, tracktotal=10, spotify_album_id="66MpVM5Cdjt3Vglq83gIzf",
                    mb_trackid="", artist="Against All Logic", album="2012 - 2017", **data)


def _filler(items, lookup):
    return request_fill.Filler(FakeLib(items), lookup=lookup)


def _events():
    with psycopg.connect(os.environ["MUSIC_DATABASE_URL"]) as conn:
        return conn.execute("SELECT kind, source, release_group::text, item_id, message, data "
                            "FROM events ORDER BY id").fetchall()


@pytest.fixture(autouse=True)
def _fresh_process():
    request_fill._unplaced.clear()
    yield
    request_fill._unplaced.clear()


def test_the_tracklist_is_the_requests_musicbrainz_recordings():
    assert list(request_fill.tracklist(RECORD)) == [KNOW_YOU, SUCH_A_BAD_WAY, SCREAM]
    spotify = {"album_songs": [{"name": "X", "song_id": "4XpOgkkAnA0GHmsk9ZxcDX",
                                "url": "https://open.spotify.com/track/4XpOgkkAnA0GHmsk9ZxcDX"}]}
    assert request_fill.tracklist(spotify) == {} and request_fill.tracklist({}) == {}


@pytest.mark.parametrize("found, expected", [
    ([(0.97, SUCH_A_BAD_WAY), (0.90, ELSEWHERE)], (SUCH_A_BAD_WAY, 0.97)),  # another release's recording: not ours
    ([(0.84, SUCH_A_BAD_WAY)], None),                                      # below MIN_SCORE
    ([(0.97, SUCH_A_BAD_WAY), (0.91, KNOW_YOU)], None),                     # two of the request's tracks: never guess
    ([(0.97, ELSEWHERE)], None),
    ([], None),
])
def test_placed_is_the_one_track_acoustid_names(found, expected):
    assert request_fill.placed(found, request_fill.tracklist(RECORD)) == expected


@needs_pg
def test_an_item_is_placed_by_its_audio_not_its_position(caplog):
    """Key 162: item 61 is track 5 of its edition, so by position it would be "Know You";
    AcoustID names track 6's recording, which its mb_trackid becomes, with its event."""
    caplog.set_level("INFO")
    item = _item_61()
    lookup = _lookup({61: [(0.97, SUCH_A_BAD_WAY)]})
    assert _filler([item], lookup)("162", RECORD, [item], set()) == {SUCH_A_BAD_WAY}
    assert item["mb_trackid"] == SUCH_A_BAD_WAY and item.stored and item.written and lookup.calls == [61]
    [(kind, source, group, item_id, message, data)] = _events()
    assert (kind, source, group, item_id) == ("recording_filled", "albums", RECORD["release_group"], 61)
    assert message == (f"[HELD] A.A.L. (Against All Logic) — 2012–2017: 61 — Such a Bad Way is the request's "
                       f"track 6 by AcoustID (0.97); mb_trackid → {SUCH_A_BAD_WAY}")
    assert (data["key"], data["old"], data["new"], data["score"]) == ("162", None, SUCH_A_BAD_WAY, 0.97)
    assert (data["track"], data["item_track"], data["track_title"]) == (6, 5, "Such a Bad Way")
    assert message in caplog.text


def test_an_item_acoustid_cant_place_is_not_held_whatever_its_position(caplog):
    """Track 5 titled like the tracklist's track 5, but no confident recording: still missing,
    and not looked up again by the next tick's filler while its file is unchanged."""
    caplog.set_level("INFO")
    item = FakeItem(7, "Know You", track=5, mb_trackid="")
    lookup = _lookup({7: [(0.60, KNOW_YOU)]})
    assert _filler([item], lookup)("162", RECORD, [item], set()) == set()
    assert not item.stored and item["mb_trackid"] == ""
    assert "[NOFILL] 7 — Know You: AcoustID names none of A.A.L. (Against All Logic) — 2012–2017's tracks" \
        in caplog.text
    assert _filler([item], lookup)("162", RECORD, [item], set()) == set()
    assert lookup.calls == [7]


def test_an_item_placed_on_another_song_is_suspect_and_left_alone(caplog):
    item = _item_61()
    lookup = _lookup({61: [(0.95, SCREAM)]})
    assert _filler([item], lookup)("162", RECORD, [item], set()) == set()
    assert not item.stored and item["mb_trackid"] == ""
    assert "[SUSPECT] 61 — Such a Bad Way: AcoustID places it on the request's track 10" in caplog.text


def test_a_failed_lookup_ends_the_ticks_lookups_and_remembers_nothing(caplog):
    items = [_item_61(), FakeItem(458, "You Are Going to Love Me and Scream", mb_trackid="")]
    lookup = _lookup({458: [(0.96, SCREAM)]}, fails=(61,))
    filler = _filler(items, lookup)
    assert filler("162", RECORD, items, set()) == set() and filler("133", RECORD, items, set()) == set()
    assert lookup.calls == [61] and "[NOFILL] AcoustID couldn't be asked (HTTP Error 503)" in caplog.text
    assert not request_fill._unplaced


@pytest.mark.parametrize("record, held, items", [
    (RECORD, {KNOW_YOU, SUCH_A_BAD_WAY, SCREAM}, [_item_61()]),          # the request lacks no track
    ({**RECORD, "album_songs": []}, set(), [_item_61()]),                 # no tracklist read yet
    (RECORD, set(), [FakeItem(3, "Know You", mb_trackid=KNOW_YOU)]),      # every item has its recording
    (RECORD, set(), [FakeItem(4, "Know You", fallback_recording=KNOW_YOU, mb_trackid="")]),
    (RECORD, set(), []),
])
def test_nothing_is_looked_up_without_a_missing_track_and_an_item_without_an_id(record, held, items):
    lookup = _lookup({})
    assert _filler(items, lookup)("162", record, items, held) == set() and lookup.calls == []


def test_without_the_acoustid_key_nothing_is_looked_up(monkeypatch, caplog):
    monkeypatch.delenv("ACOUSTID_APIKEY", raising=False)
    item = _item_61()
    assert request_fill.Filler(FakeLib([item]))("162", RECORD, [item], set()) == set()
    assert "[NOFILL] ACOUSTID_APIKEY is unset" in caplog.text


@needs_pg
def test_an_item_given_a_recording_since_the_tick_read_it_gets_no_event():
    stale, fresh = _item_61(), _item_61()
    fresh["mb_trackid"] = SUCH_A_BAD_WAY
    filler = request_fill.Filler(FakeLib([fresh]), lookup=_lookup({61: [(0.97, SUCH_A_BAD_WAY)]}))
    assert filler("162", RECORD, [stale], set()) == {SUCH_A_BAD_WAY}
    assert not fresh.stored and _events() == []


@needs_pg
def test_each_item_once_and_the_writes_under_the_lock():
    held_lock = []

    class Lock:
        def __enter__(self):
            held_lock.append(True)

        def __exit__(self, *_):
            held_lock.append(False)

    item = _item_61()
    lookup = _lookup({61: [(0.97, SUCH_A_BAD_WAY)]})
    filler = request_fill.Filler(FakeLib([item]), lock=Lock, lookup=lookup)
    assert filler("162", RECORD, [item, item], set()) == {SUCH_A_BAD_WAY}
    assert lookup.calls == [61] and held_lock == [True, False] and len(_events()) == 1
