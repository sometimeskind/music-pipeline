"""Tests for music_scan.dupes — music-audit-dupes (#210)."""

import random
from pathlib import Path

import pytest

from music_scan import dupes
from music_scan.dupes import CERTAIN, UNCERTAIN, bit_error_rate, find_dupes, keep_order, merge
from music_scan.identity import ItemIndex


class FakeItem:
    """Enough of a beets Item: attributes for fixed fields, ``get``/``[]`` for all."""

    def __init__(self, id_, title, artist="Artist", length=200.0, bitrate=256000, path=None, **data):
        self.id, self.title, self.artist, self.albumartist = id_, title, artist, artist
        self.length, self.bitrate = length, bitrate
        self.path = (path or f"/lib/{artist}/{title}-{id_}.m4a").encode()
        self.data = data
        self.stored = self.written = self.removed = False

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

    def remove(self, delete=False):
        assert delete is False, "a merge never deletes files"
        self.removed = True


def _frames(seed: int, n: int = 400) -> list[int]:
    rng = random.Random(seed)
    return [rng.getrandbits(32) for _ in range(n)]


def _noisy(frames: list[int], flips: int, seed: int = 9) -> list[int]:
    """*frames* with *flips* random bits flipped per sub-fingerprint (a re-encode)."""
    rng = random.Random(seed)
    out = []
    for f in frames:
        for _ in range(flips):
            f ^= 1 << rng.randrange(32)
        out.append(f)
    return out


FPS = {"same-a": _frames(1), "same-b": _noisy(_frames(1), 2), "other": _frames(2),
       "shifted": [0] * 12 + _frames(1)}


def _decoder(fp):
    return FPS[fp]


def _tiers(items):
    certain, uncertain, distinct = find_dupes(items, decoder=_decoder)
    as_ids = lambda groups: sorted(sorted(i.id for i in g.items) for g in groups)  # noqa: E731
    return as_ids(certain), as_ids(uncertain), distinct, certain, uncertain


# --- fingerprint comparison ---------------------------------------------------

def test_bit_error_rate_matches_a_reencode_and_a_shift_but_not_another_recording() -> None:
    assert bit_error_rate(FPS["same-a"], FPS["same-b"]) < 0.1
    assert bit_error_rate(FPS["same-a"], FPS["shifted"]) == 0.0
    assert bit_error_rate(FPS["same-a"], FPS["other"]) > 0.4


def test_bit_error_rate_needs_enough_overlap() -> None:
    assert bit_error_rate(_frames(1, 10), _frames(1, 10)) == 1.0


# --- certain tier -------------------------------------------------------------

@pytest.mark.parametrize("field,a,b", [
    ("spotify_ids", "S1,S2", "S2"),
    ("isrc", "GB1;US9", "US9"),
    ("mb_trackid", "rec-1", "rec-1"),
])
def test_a_shared_id_is_certain_whatever_the_lengths(field, a, b) -> None:
    one = FakeItem(1, "Song", length=200.0, **{field: a})
    clip = FakeItem(2, "Song (clip)", length=30.0, **{field: b})
    certain, uncertain, *_ = _tiers([one, clip])
    assert certain == [[1, 2]] and uncertain == []


def test_spotify_url_counts_as_a_spotify_id() -> None:
    old = FakeItem(1, "Song", spotify_url="https://open.spotify.com/track/S1")
    new = FakeItem(2, "Song", spotify_ids="S1")
    assert _tiers([old, new])[0] == [[1, 2]]


def test_fingerprint_match_within_two_seconds_is_certain() -> None:
    a = FakeItem(1, "Song", length=200.0, acoustid_fingerprint="same-a")
    b = FakeItem(2, "Song (Remastered)", length=201.5, acoustid_fingerprint="same-b")
    certain, uncertain, _, groups, _ = _tiers([a, b])
    assert certain == [[1, 2]] and uncertain == []
    assert "fingerprint" in groups[0].evidence[0][2]


def test_shared_acoustid_id_is_a_fingerprint_match() -> None:
    a = FakeItem(1, "Song", acoustid_id="acid")
    b = FakeItem(2, "Track", acoustid_id="acid")
    assert _tiers([a, b])[0] == [[1, 2]]


def test_fingerprints_are_compared_only_between_items_sharing_an_artist_word() -> None:
    a = FakeItem(1, "Song", artist="Somebody", acoustid_fingerprint="same-a")
    b = FakeItem(2, "Tune", artist="Else", acoustid_fingerprint="same-b")
    assert _tiers([a, b])[:2] == ([], [])


def test_groups_are_transitive() -> None:
    a = FakeItem(1, "Song", spotify_ids="S1")
    b = FakeItem(2, "Song", spotify_ids="S1", isrc="GB1")
    c = FakeItem(3, "Song", isrc="GB1")
    certain, uncertain, *_ = _tiers([a, b, c])
    assert certain == [[1, 2, 3]] and uncertain == []


# --- uncertain tier -----------------------------------------------------------

def test_fingerprint_match_with_different_lengths_is_uncertain() -> None:
    album = FakeItem(1, "Song", length=240.0, acoustid_fingerprint="same-a")
    edit = FakeItem(2, "Song (Radio Edit)", length=200.0, acoustid_fingerprint="same-b")
    certain, uncertain, _, _, groups = _tiers([album, edit])
    assert certain == [] and uncertain == [[1, 2]]
    assert "40s apart" in groups[0].evidence[0][2]


def test_title_and_artist_after_normalisation_is_uncertain() -> None:
    a = FakeItem(1, "WOR$T GIRL", artist="Slayyyter")
    b = FakeItem(2, "Worst Girl", artist="Slayyyter feat. Someone")
    assert _tiers([a, b])[:2] == ([], [[1, 2]])


def test_title_and_artist_with_disjoint_isrcs_is_distinct_not_listed() -> None:
    studio = FakeItem(1, "The 1975", artist="The 1975", isrc="GB1")
    other = FakeItem(2, "The 1975", artist="The 1975", isrc="GB2")
    certain, uncertain, distinct, *_ = _tiers([studio, other])
    assert (certain, uncertain, distinct) == ([], [], 1)


def test_title_and_artist_with_one_side_missing_isrcs_stays_uncertain() -> None:
    a = FakeItem(1, "Song", isrc="GB1")
    b = FakeItem(2, "Song")
    assert _tiers([a, b])[1] == [[1, 2]]


def test_a_certain_pair_is_not_also_listed_uncertain() -> None:
    a = FakeItem(1, "Song", spotify_ids="S1")
    b = FakeItem(2, "Song", spotify_ids="S1")
    assert _tiers([a, b])[:2] == ([[1, 2]], [])


# --- keep rule ----------------------------------------------------------------

def test_keep_prefers_spotify_length_then_bitrate_then_playlists_then_older() -> None:
    expected = [200.0]
    clip = FakeItem(1, "S", length=30.0, bitrate=320000)
    right = FakeItem(2, "S", length=200.4, bitrate=128000)
    better = FakeItem(3, "S", length=199.8, bitrate=256000)
    more = FakeItem(4, "S", length=200.0, bitrate=256000, sources="a,b")
    older = FakeItem(0, "S", length=200.0, bitrate=256000, sources="c,d")
    order = sorted([clip, right, better, more, older], key=lambda i: keep_order(i, expected))
    assert [i.id for i in order] == [0, 4, 3, 2, 1]


def test_keep_without_spotify_length_uses_bitrate() -> None:
    low, high = FakeItem(1, "S", bitrate=127000), FakeItem(2, "S", bitrate=128000)
    assert sorted([low, high], key=lambda i: keep_order(i, []))[0] is high


# --- merge --------------------------------------------------------------------

def _on_disk(tmp_path: Path, item: FakeItem) -> FakeItem:
    path = tmp_path / "library" / item.path.decode().lstrip("/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"audio")
    item.path = str(path).encode()
    return item


def test_merge_moves_ids_and_playlists_and_quarantines_the_file(tmp_path) -> None:
    keeper = _on_disk(tmp_path, FakeItem(1, "Song", spotify_ids="A", isrc="GB1", sources="keep"))
    loser = _on_disk(tmp_path, FakeItem(2, "Song", spotify_url="https://open.spotify.com/track/B",
                                        isrc="GB1;US2", mb_trackid="rec", sources="keep,later"))
    old = Path(loser.path.decode())
    moved = merge(keeper, [loser], tmp_path / "replaced")
    assert keeper.get("spotify_ids") == "A,B"
    assert keeper.get("isrc") == "GB1;US2"
    assert keeper.get("sources") == "keep,later"
    assert keeper.get("mb_trackid") == "rec"
    assert keeper.stored and keeper.written
    assert loser.removed and not old.exists()
    assert moved == [tmp_path / "replaced" / f"2-{old.name}"] and moved[0].read_bytes() == b"audio"


def test_merge_never_overwrites_a_replaced_file(tmp_path) -> None:
    keeper = _on_disk(tmp_path, FakeItem(1, "Song"))
    loser = _on_disk(tmp_path, FakeItem(2, "Song"))
    name = Path(loser.path.decode()).name
    (tmp_path / "replaced").mkdir()
    (tmp_path / "replaced" / f"2-{name}").write_bytes(b"earlier")
    moved = merge(keeper, [loser], tmp_path / "replaced")
    assert (tmp_path / "replaced" / f"2-{name}").read_bytes() == b"earlier"
    assert moved[0].read_bytes() == b"audio" and moved[0].name != f"2-{name}"


def test_merge_without_media_changes_does_not_rewrite_the_keeper(tmp_path) -> None:
    keeper = _on_disk(tmp_path, FakeItem(1, "Song", spotify_ids="A", sources="keep"))
    loser = _on_disk(tmp_path, FakeItem(2, "Song", spotify_ids="A", sources="later"))
    merge(keeper, [loser], tmp_path / "replaced")
    assert keeper.stored and not keeper.written


# --- the Vegyn clash (#209's skipped move) ----------------------------------

ROAD = "Vegyn/The Road To Hell Is Paved With Good Intentions"


def _vegyn(tmp_path):
    single = FakeItem(553, "A Dream Goes On Forever", artist="Vegyn", length=279.0, bitrate=127000,
                      path="/Vegyn/A Dream Goes On Forever/01 - A Dream Goes On Forever.m4a",
                      spotify_ids="5OY32VyyPJDPBf2C2RTX9v", sources="aaaaaaah", acoustid_fingerprint="same-a")
    album = FakeItem(555, "A Dream Goes On Forever", artist="Vegyn", length=279.0, bitrate=128000,
                     path=f"/{ROAD}/01 - A Dream Goes On Forever.m4a",
                     spotify_ids="1CGT5LV74SNzXFnVmTdAE8", isrc="GB5952300271",
                     mb_trackid="1f4ace47-aa9d-4c12-b4cc-ef4dd0c01dd4", sources="aaaaaaah",
                     acoustid_fingerprint="same-b")
    other = FakeItem(455, "Halo Flip", artist="Vegyn", length=415.0, bitrate=128000,
                     path=f"/{ROAD}/04 - Halo Flip.m4a", spotify_ids="1s1gephZljhEtwZbW8uAN8",
                     sources="aaaaaaah", acoustid_fingerprint="other")
    return [_on_disk(tmp_path, i) for i in (single, album, other)]


def test_vegyn_single_and_album_track_merge_onto_the_album_item(tmp_path) -> None:
    items = _vegyn(tmp_path)
    single, album, _ = items
    certain, uncertain, _, groups, _ = _tiers(items)
    assert certain == [[553, 555]] and uncertain == []

    keeper, *losers = sorted(groups[0].items, key=lambda i: keep_order(i, [279.0]))
    assert keeper is album
    single_path = Path(single.path.decode())
    merge(keeper, losers, tmp_path / "replaced")
    assert not single_path.exists() and (tmp_path / "replaced" / f"553-{single_path.name}").exists()
    assert set(album.get("spotify_ids").split(",")) == {"1CGT5LV74SNzXFnVmTdAE8", "5OY32VyyPJDPBf2C2RTX9v"}

    # The .m3u regen walks this ladder: the single's playlist entry now finds the album item.
    survivors = [i for i in items if not i.removed]
    song = {"song_id": "5OY32VyyPJDPBf2C2RTX9v", "name": "A Dream Goes On Forever", "artists": ["Vegyn"]}
    assert ItemIndex(survivors).match_song(song, words=False) == (album, "id")
    # A re-run finds nothing certain.
    assert _tiers(survivors)[0] == []


# --- run ----------------------------------------------------------------------

def test_run_dry_run_reports_and_pushes_without_merging(monkeypatch, tmp_path) -> None:
    items = _vegyn(tmp_path)
    events = _patch_run(monkeypatch, items, tmp_path)
    certain, _ = dupes.run()
    assert [sorted(i.id for i in g.items) for g in certain] == [[553, 555]]
    assert not any(i.removed for i in items)
    assert PUSHED == [(1, 0)]
    assert events == []


def test_run_apply_merges_regens_and_reports_zero_left(monkeypatch, tmp_path) -> None:
    items = _vegyn(tmp_path)
    events = _patch_run(monkeypatch, items, tmp_path)
    dupes.run(apply=True)
    assert [i.id for i in items if i.removed] == [553]
    # The .m3u files point at the keeper before Navidrome rescans (#218).
    assert events == ["m3u", "rescan"]
    assert PUSHED == [(0, 0)]


PUSHED: list = []


def _patch_run(monkeypatch, items, tmp_path):
    import contextlib
    from unittest.mock import MagicMock

    from music_scan import library, navidrome, scan

    PUSHED.clear()
    events: list = []
    lib = MagicMock()
    lib.__enter__.return_value.all_items.return_value = items
    monkeypatch.setattr(library, "MusicLibrary", lambda *_: lib)
    monkeypatch.setattr(navidrome, "trigger_scan", lambda: events.append("rescan"))
    monkeypatch.setattr(scan, "regen_playlists", lambda: events.append("m3u") or {"aaaaaaah": 2})
    monkeypatch.setattr(scan, "SPOTDL_DIR", tmp_path)
    monkeypatch.setattr(dupes, "REPLACED", tmp_path / "replaced")
    monkeypatch.setattr(dupes, "decode", _decoder)
    monkeypatch.setattr(dupes, "pipeline_lock", contextlib.nullcontext)
    monkeypatch.setattr(dupes, "push_metrics", lambda c, u: PUSHED.append((c, u)))
    return events


# --- fingerprint backfill -----------------------------------------------------

def test_fingerprint_missing_dry_run_counts_only(monkeypatch) -> None:
    monkeypatch.setattr(dupes, "fingerprint", lambda path: pytest.fail("a dry run fingerprints nothing"))
    items = [FakeItem(1, "A", acoustid_fingerprint="fp"), FakeItem(2, "B")]
    assert dupes.fingerprint_missing(items, apply=False) == 1
    assert not items[1].stored


def test_fingerprint_missing_stores_only_the_missing(monkeypatch) -> None:
    import contextlib

    monkeypatch.setattr(dupes, "pipeline_lock", contextlib.nullcontext)
    monkeypatch.setattr(dupes, "fingerprint", lambda path: "NEW")
    have, missing = FakeItem(1, "A", acoustid_fingerprint="fp"), FakeItem(2, "B")
    assert dupes.fingerprint_missing([have, missing], apply=True) == 1
    assert have.get("acoustid_fingerprint") == "fp" and not have.stored
    assert missing.get("acoustid_fingerprint") == "NEW" and missing.stored


def test_fingerprint_ids_replaces_a_stale_fingerprint_and_acoustid(monkeypatch) -> None:
    import contextlib

    monkeypatch.setattr(dupes, "pipeline_lock", contextlib.nullcontext)
    monkeypatch.setattr(dupes, "fingerprint", lambda path: "NEW")
    stale = FakeItem(873, "The Lemon of Pink I", acoustid_fingerprint="OLD", acoustid_id="old-acid")
    other = FakeItem(1, "Other")
    assert dupes.fingerprint_missing([stale, other], apply=True, ids=[873, 999]) == 1
    assert stale.get("acoustid_fingerprint") == "NEW" and stale.get("acoustid_id") == ""
    assert not other.stored
