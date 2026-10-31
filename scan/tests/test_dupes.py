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


# A re-encode flips about 1 bit in 32 (BER ~0.03); another track by a repetitive
# artist about 4 (~0.12, The Field in #221).
FPS = {"same-a": _frames(1), "same-b": _noisy(_frames(1), 1), "other": _frames(2),
       "shifted": [0] * 12 + _frames(1), "lookalike": _noisy(_frames(1), 4)}


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
])
def test_a_shared_id_is_certain_whatever_the_lengths(field, a, b) -> None:
    one = FakeItem(1, "Song", length=200.0, **{field: a})
    clip = FakeItem(2, "Song (clip)", length=30.0, **{field: b})
    certain, uncertain, *_ = _tiers([one, clip])
    assert certain == [[1, 2]] and uncertain == []


def test_a_shared_recording_id_is_certain_only_with_lengths_that_agree() -> None:
    """homelab#2540: beets guessed the 5.1 mix's recording for the stereo 6930, which
    6994 (the real 5.1 mix, 11 s longer) holds.  A recording ID is a best guess per
    file, so a shared one is certain only within LENGTH_SLACK."""
    stereo = FakeItem(6930, "Oh! Sweet Nuthin' (5.1 mix)", length=445.2, mb_trackid="d0063aee")
    surround = FakeItem(6994, "Oh! Sweet Nuthin' (5.1 mix)", length=456.0, mb_trackid="d0063aee")
    reencode = FakeItem(7000, "Oh! Sweet Nuthin'", length=446.5, mb_trackid="d0063aee")
    certain, uncertain, _, groups, loose = _tiers([stereo, surround, reencode])
    assert certain == [[6930, 7000]]
    assert groups[0].evidence == [(6930, 7000, "mbid d0063aee")]
    assert uncertain == [[6930, 6994, 7000]]
    assert {"mbid d0063aee but 11s apart", "mbid d0063aee but 10s apart"} <= {why for *_, why in loose[0].evidence}


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
    b = FakeItem(2, "Song", acoustid_id="acid")
    assert _tiers([a, b])[0] == [[1, 2]]


def test_a_lookalike_track_by_a_repetitive_artist_is_no_match() -> None:
    """The Field: track 04 scored 0.13 against track 02, 1s apart (#221)."""
    paw = FakeItem(973, "A Paw in My Face", artist="The Field", length=325.0, acoustid_fingerprint="same-a")
    heart = FakeItem(982, "The Little Heart Beats So Fast", artist="The Field", length=326.0,
                     acoustid_fingerprint="lookalike")
    assert 0.08 < bit_error_rate(FPS["same-a"], FPS["lookalike"]) < 0.15
    assert _tiers([paw, heart])[:2] == ([], [])


def test_a_fingerprint_match_between_different_titles_is_wrong_audio_not_a_dupe() -> None:
    """Talk Talk: 1577 is filed as Eden but its audio is Wealth (#221)."""
    wealth = FakeItem(1527, "Wealth - 1997 Remaster", artist="Talk Talk", length=404.0, spotify_ids="W",
                      acoustid_fingerprint="same-a")
    eden = FakeItem(1577, "Eden - 1997 Remaster", artist="Talk Talk", length=404.0, spotify_ids="E",
                    acoustid_fingerprint="same-b")
    certain, uncertain, _, _, groups = _tiers([wealth, eden])
    assert certain == [] and uncertain == [[1527, 1577]]
    assert "titles differ" in groups[0].evidence[0][2] and "--replace" in groups[0].evidence[0][2]


def test_a_remaster_suffix_does_not_make_titles_differ() -> None:
    """KC & The Sunshine Band: Get Down Tonight / Get Down Tonight - 2004 Remaster, BER 0.06."""
    a = FakeItem(739, "Get Down Tonight", artist="KC & The Sunshine Band", length=317.0, acoustid_fingerprint="same-a")
    b = FakeItem(786, "Get Down Tonight - 2004 Remaster", artist="KC & The Sunshine Band", length=317.0,
                 acoustid_fingerprint="same-b")
    assert _tiers([a, b])[0] == [[739, 786]]


def test_curly_apostrophes_and_case_do_not_make_titles_differ() -> None:
    a = FakeItem(341, "This Guy's In Love With You", artist="These New Puritans", acoustid_fingerprint="same-a")
    b = FakeItem(1662, "This Guy\u2019s in Love With You", artist="These New Puritans", acoustid_fingerprint="same-b")
    assert _tiers([a, b])[0] == [[341, 1662]]


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

def test_keep_prefers_spotify_length_then_bitrate_then_older() -> None:
    """Playlists don't break a tie (homelab#1914): ``sources`` is provenance, and the
    stores list a recording, so every copy has the same playlists."""
    expected = [200.0]
    clip = FakeItem(1, "S", length=30.0, bitrate=320000)
    right = FakeItem(2, "S", length=200.4, bitrate=128000)
    better = FakeItem(3, "S", length=199.8, bitrate=256000)
    more = FakeItem(4, "S", length=200.0, bitrate=256000, sources="a,b,c")
    older = FakeItem(0, "S", length=200.0, bitrate=256000)
    order = sorted([clip, right, better, more, older], key=lambda i: keep_order(i, expected))
    assert [i.id for i in order] == [0, 3, 4, 2, 1]


def test_spotify_durations_read_every_store_first_listing_wins(tmp_path, monkeypatch) -> None:
    """homelab#1914: durations come from the playlist stores, by song ID or URL; a
    ``.spotdl`` file is never read."""
    from music_fetch import store

    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "store")
    store.Store.from_songs("a", [{"song_id": "S1", "duration": 200}, {"song_id": "S2"},
                                 {"url": "https://open.spotify.com/track/S3", "duration": 180.5}]).save(store.path("a"))
    store.Store.from_songs("b", [{"song_id": "S1", "duration": 999}, {"song_id": "S4", "duration": 90}]
                           ).save(store.path("b"))
    (tmp_path / "c.spotdl").write_text('{"songs": [{"song_id": "OLD", "duration": 1}]}')
    assert dupes.spotify_durations() == {"S1": 200.0, "S3": 180.5, "S4": 90.0}


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


def test_merge_moves_ids_and_playlists_and_quarantines_the_file(tmp_path, monkeypatch) -> None:
    from music_fetch import events

    recorded = []
    monkeypatch.setattr(events, "record", lambda kind, message, **kw: recorded.append((kind, message, kw)))
    keeper = _on_disk(tmp_path, FakeItem(1, "Song", spotify_ids="A", isrc="GB1", sources="keep"))
    loser = _on_disk(tmp_path, FakeItem(2, "Song", spotify_url="https://open.spotify.com/track/B",
                                        isrc="GB1;US2", mb_trackid="rec", sources="keep,later"))
    old = Path(loser.path.decode())
    moved = merge(keeper, [loser], tmp_path / "replaced")
    (kind, message, kw), = recorded
    assert kind == "merge" and message == f"[MERGE] keep 1 ← 2; old file → {moved[0]}"
    assert kw["item_id"] == 1 and kw["data"]["merged"] == 2 and kw["data"]["why"] == "duplicate"
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


def test_merge_records_the_losers_files_on_the_keeper_and_passes_them_on(tmp_path) -> None:
    """#303: the keeper names the files it superseded, and a later keeper inherits them."""
    from music_scan.identity import Recordings
    from music_scan.release import split_replaced

    keeper = _on_disk(tmp_path, FakeItem(1, "Song", isrc="GB1"))
    bare = _on_disk(tmp_path, FakeItem(2, "Song"))  # no identity at all, as #302's legacy extras
    moved = merge(keeper, [bare], tmp_path / "replaced")
    assert keeper.get("merged_files") == moved[0].name and keeper.stored
    assert split_replaced(moved, Recordings([keeper])) == (moved, [])
    assert split_replaced(moved, Recordings([FakeItem(3, "Song", isrc="GB1")])) == ([], moved)

    final = _on_disk(tmp_path, FakeItem(4, "Song", isrc="GB1"))
    later = merge(final, [keeper], tmp_path / "replaced")
    assert final.get("merged_files") == f"{moved[0].name}/{later[0].name}"
    assert split_replaced(moved + later, Recordings([final])) == (moved + later, [])


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


# --- --merge: an uncertain pair the operator confirms (#276) -------------------

def _bright_eyes(tmp_path):
    """Old Soul Song: the same recording on two items, listed uncertain on title+artist (homelab#1889)."""
    old = FakeItem(92, "Old Soul Song (For The New World Order)", artist="Bright Eyes", length=269.0,
                   bitrate=127000, spotify_ids="OLD", sources="aaaaaaah", via="spotdl")
    new = FakeItem(2898, "Old Soul Song (for the New World Order)", artist="Bright Eyes", length=269.0,
                   bitrate=258000, spotify_ids="NEW", sources="keep", via="usenet")
    return [_on_disk(tmp_path, i) for i in (old, new)]


def test_merge_dry_run_changes_nothing(monkeypatch, tmp_path, caplog) -> None:
    items = _bright_eyes(tmp_path)
    events = _patch_run(monkeypatch, items, tmp_path)
    caplog.set_level("INFO")
    dupes.run(merge_ids=[2898, 92])
    assert not any(i.removed or i.stored for i in items)
    assert all(Path(i.path.decode()).exists() for i in items)
    assert events == [] and PUSHED == []
    assert "keep 2898" in caplog.text and "drop 92" in caplog.text and "Dry run" in caplog.text
    assert "new playlists: aaaaaaah" in caplog.text


def test_merge_apply_merges_and_the_dropped_items_playlists_point_at_the_keeper(monkeypatch, tmp_path) -> None:
    items = _bright_eyes(tmp_path)
    old, new = items
    old_path = Path(old.path.decode())
    events = _patch_run(monkeypatch, items, tmp_path)
    dupes.run(apply=True, merge_ids=[2898, 92])
    assert old.removed and not new.removed
    assert not old_path.exists() and (tmp_path / "replaced" / f"92-{old_path.name}").exists()
    assert new.get("sources") == "keep,aaaaaaah"
    # The .m3u regen finds the keeper for the dropped item's playlist entry.
    song = {"song_id": "OLD", "name": "Old Soul Song (For The New World Order)", "artists": ["Bright Eyes"]}
    assert ItemIndex([new]).match_song(song, words=False) == (new, "id")
    assert events == ["m3u", "rescan"]
    assert PUSHED == []


@pytest.mark.parametrize("ids,why", [
    ([2898, 999], "no library item with id 999"),
    ([2898, 92, 2898], "keeper 2898 is also among the drops"),
    ([2898], "needs a keeper and at least one"),
])
def test_merge_refuses_before_changing_anything(monkeypatch, tmp_path, ids, why) -> None:
    items = _bright_eyes(tmp_path)
    events = _patch_run(monkeypatch, items, tmp_path)
    with pytest.raises(SystemExit, match=why):
        dupes.run(apply=True, merge_ids=ids)
    assert not any(i.removed or i.stored for i in items) and events == []


def test_merge_refuses_a_manual_drop_unless_allowed(monkeypatch, tmp_path) -> None:
    items = _bright_eyes(tmp_path)
    items[0]["via"] = "manual"
    events = _patch_run(monkeypatch, items, tmp_path)
    with pytest.raises(SystemExit, match=r"92 \(via=manual\) imported by hand"):
        dupes.run(apply=True, merge_ids=[2898, 92])
    assert not items[0].removed and events == []
    dupes.run(apply=True, merge_ids=[2898, 92], allow_manual=True)
    assert items[0].removed and events == ["m3u", "rescan"]


def test_a_manual_keeper_is_fine(monkeypatch, tmp_path) -> None:
    items = _bright_eyes(tmp_path)
    items[1]["via"] = ""
    _patch_run(monkeypatch, items, tmp_path)
    dupes.run(apply=True, merge_ids=[2898, 92])
    assert items[0].removed


def test_the_audit_prints_a_merge_command_per_uncertain_pair(monkeypatch, tmp_path, caplog) -> None:
    """Talking Heads: a studio take and two live ones chain into one group; each pair gets its own command."""
    studio = FakeItem(2538, "This Must Be the Place (Naïve Melody)", artist="Talking Heads", length=294.0,
                      bitrate=262000)
    live = FakeItem(2561, "This Must Be the Place (Naive Melody)", artist="Talking Heads", length=297.0,
                    bitrate=268000, acoustid_id="f67c8b1f")
    live2 = FakeItem(2595, "This Must Be the Place (Naive Melody) (live)", artist="Talking Heads", length=294.0,
                     bitrate=267000, acoustid_id="f67c8b1f")
    items = [_on_disk(tmp_path, i) for i in (studio, live, live2)]
    _patch_run(monkeypatch, items, tmp_path)
    caplog.set_level("INFO")
    _, uncertain = dupes.run()
    assert [sorted(i.id for i in g.items) for g in uncertain] == [[2538, 2561, 2595]]
    assert "music-audit-dupes --merge 2561 2595" in caplog.text
    assert "music-audit-dupes --merge 2561 2538" in caplog.text
    assert "--merge 2561 2538 2595" not in caplog.text


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
    from music_fetch import store

    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(dupes, "REPLACED", tmp_path / "replaced")
    monkeypatch.setattr(dupes, "decode", _decoder)
    monkeypatch.setattr(dupes, "pipeline_lock", contextlib.nullcontext)
    monkeypatch.setattr(dupes, "push_metrics", lambda c, u: PUSHED.append((c, u)))
    return events


# --- --unlink: a Spotify ID an item holds wrongly (homelab#2530) ---------------

PICTURES = "2o49Twc3qrNMOt8gq9W06L"


def _pictures_of_you(tmp_path):
    """Easy on Your Own? holds Pictures Of You's ID from a wrong merge; the real track holds its ISRC."""
    alvvays = FakeItem(9674, "Easy on Your Own?", artist="Alvvays", spotify_ids=PICTURES, isrc="US3R42246502",
                       sources="cool,aaaaaaah", via="usenet")
    cure = FakeItem(9778, "Pictures of You", artist="The Cure", isrc="GBAKW8901034;GBUM71000205", via="usenet")
    return [_on_disk(tmp_path, i) for i in (alvvays, cure)]


SONGS = {"aaaaaaah": [{"song_id": PICTURES, "name": "Pictures Of You", "artists": ["The Cure"],
                       "isrc": "GBUM71000205"}],
         "cool": [{"song_id": "1111111111111111111111", "name": "Easy On Your Own?", "artists": ["Alvvays"]}]}


def test_unlink_dry_run_reports_the_entries_new_item_and_changes_nothing(tmp_path, caplog) -> None:
    items = _pictures_of_you(tmp_path)
    caplog.set_level("INFO")
    assert dupes.unlink_spotify_id(items, 9674, PICTURES, False, SONGS) is False
    assert items[0].get("spotify_ids") == PICTURES and not items[0].stored
    assert f"spotify_ids {PICTURES} → (none)" in caplog.text
    assert "aaaaaaah: Pictures Of You — The Cure (ISRC GBUM71000205) → 9778 by ISRC" in caplog.text
    assert "Dry run" in caplog.text


def test_unlink_apply_takes_only_that_id_off(tmp_path, monkeypatch) -> None:
    from music_fetch import events as ev

    recorded = []
    monkeypatch.setattr(ev, "record", lambda kind, message, **kw: recorded.append((kind, kw["item_id"])) or True)
    items = _pictures_of_you(tmp_path)
    items[0]["spotify_ids"] = f"OWN0000000000000000000,{PICTURES}"
    assert dupes.unlink_spotify_id(items, 9674, PICTURES, True, SONGS) is True
    assert items[0].get("spotify_ids") == "OWN0000000000000000000" and items[0].stored
    assert not items[1].stored
    assert recorded == [("wrong_version", 9674)]
    song = SONGS["aaaaaaah"][0]
    assert ItemIndex(items).match_song(song, words=False)[0] is items[1]


def test_unlink_says_when_the_slot_goes_empty_or_stays(tmp_path, caplog) -> None:
    items = _pictures_of_you(tmp_path)
    caplog.set_level("INFO")
    dupes.unlink_spotify_id(items[:1], 9674, PICTURES, False, SONGS)
    assert "→ no item: the slot goes empty" in caplog.text
    caplog.clear()
    items[0]["isrc"] = "GBUM71000205"
    dupes.unlink_spotify_id(items[:1], 9674, PICTURES, False, SONGS)
    assert "→ 9674 still, by ISRC" in caplog.text
    caplog.clear()
    dupes.unlink_spotify_id(items, 9674, PICTURES, False, {})
    assert f"no playlist store lists {PICTURES}" in caplog.text


@pytest.mark.parametrize("item_id,sid,why", [
    (999, PICTURES, "no library item with id 999"),
    (9778, PICTURES, f"item 9778 does not hold Spotify ID {PICTURES}"),
    (9674, "OWN0000000000000000000", "is item 9674's spotify_url"),
])
def test_unlink_refuses_before_changing_anything(monkeypatch, tmp_path, item_id, sid, why) -> None:
    items = _pictures_of_you(tmp_path)
    items[0]["spotify_url"] = "https://open.spotify.com/track/OWN0000000000000000000"
    items[0]["spotify_ids"] = f"OWN0000000000000000000,{PICTURES}"
    events = _patch_run(monkeypatch, items, tmp_path)
    with pytest.raises(SystemExit, match=why):
        dupes.run(apply=True, unlink=(item_id, sid))
    assert not any(i.stored for i in items) and events == []


def test_unlink_run_dry_run_then_apply_regens_and_rescans(monkeypatch, tmp_path) -> None:
    items = _pictures_of_you(tmp_path)
    events = _patch_run(monkeypatch, items, tmp_path)
    dupes.run(unlink=(9674, PICTURES))
    assert not items[0].stored and events == [] and PUSHED == []
    dupes.run(apply=True, unlink=(9674, PICTURES))
    assert items[0].get("spotify_ids") == "" and items[0].stored
    assert events == ["m3u", "rescan"] and PUSHED == []


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


# --- album records: the album is the unit (#279, #311) ------------------------

def _album_items(tmp_path, fields):
    """One recording on two items, one ISRC: *fields* maps each item ID to its album fields."""
    items = [FakeItem(id_, "Made Up Song", artist="Some Band", length=200.0 + n, isrc="XX0000000001",
                      sources="pl", via="usenet", **extra)
             for n, (id_, extra) in enumerate(fields.items())]
    return [_on_disk(tmp_path, i) for i in items]


@pytest.mark.parametrize("fields,why", [
    ({1: {"album_record": "HITS", "release": "r1"}, 2: {"album_record": "STUDIO", "release": "r2"}},
     "on 2 album records (HITS, STUDIO): each album keeps its track"),
    ({1: {"album_record": "DELUXE", "release": "r1"}, 2: {"album_record": "DELUXE", "release": "r1"}},
     "2 release items of album record DELUXE"),
    ({1: {"album_record": "OLD", "album_extra": "1"}, 2: {"album_record": "OLD", "album_extra": "1"}},
     "2 local-only extras of album record OLD"),
    ({1: {"album_record": "HITS", "album_extra": "1"}, 2: {"album_record": "STUDIO", "release": "r2"}},
     "on 2 album records"),
])
def test_apply_leaves_a_certain_group_that_would_take_a_track_off_an_album(monkeypatch, tmp_path, caplog,
                                                                           fields, why) -> None:
    items = _album_items(tmp_path, fields)
    events = _patch_run(monkeypatch, items, tmp_path)
    caplog.set_level("INFO")
    certain, listed = dupes.run(apply=True)
    assert certain == [] and not any(i.removed for i in items)
    assert [(g.tier, sorted(i.id for i in g.items)) for g in listed] == [(dupes.ALBUM, [1, 2])]
    assert why in listed[0].reason and why in caplog.text
    assert "[ALBUM]" in caplog.text and "by hand, if the album may lose it: music-audit-dupes --merge" in caplog.text
    assert PUSHED == [(0, 1)] and events == []


def test_an_extra_merges_into_its_own_records_release_item(monkeypatch, tmp_path) -> None:
    """The release item keeps, though the extra is nearer Spotify's length: the album keeps its track."""
    items = _album_items(tmp_path, {1: {"album_record": "A", "album_extra": "1", "spotify_ids": "S"},
                                   2: {"album_record": "A", "release": "r1"}})
    extra, track = items
    _patch_run(monkeypatch, items, tmp_path)
    from music_fetch import store

    store.Store.from_songs("pl", [{"song_id": "S", "duration": 200}]).save(store.path("pl"))
    certain, listed = dupes.run(apply=True)
    assert [sorted(i.id for i in g.items) for g in certain] == [[1, 2]] and listed == []
    assert extra.removed and not track.removed


def test_a_stray_merges_into_the_album_item_whatever_the_lengths(tmp_path) -> None:
    stray, track = _album_items(tmp_path, {1: {}, 2: {"album_record": "A", "release": "r1"}})
    assert sorted([stray, track], key=lambda i: keep_order(i, [200.0]))[0] is track


def test_merge_by_hand_off_an_album_warns_and_still_merges(monkeypatch, tmp_path, caplog) -> None:
    items = _album_items(tmp_path, {1: {"album_record": "HITS", "release": "r1", "album": "Made Up Hits"},
                                   2: {"album_record": "STUDIO", "release": "r2"}})
    _patch_run(monkeypatch, items, tmp_path)
    caplog.set_level("INFO")
    dupes.run(merge_ids=[2, 1])
    assert "[ALBUM] 1 is a release item of album record HITS [Made Up Hits]: its album loses the track" in caplog.text
    assert not items[0].removed
    dupes.run(apply=True, merge_ids=[2, 1])
    assert items[0].removed


def test_merge_by_hand_of_an_extra_into_its_release_item_does_not_warn(monkeypatch, tmp_path, caplog) -> None:
    items = _album_items(tmp_path, {1: {"album_record": "A", "album_extra": "1"},
                                   2: {"album_record": "A", "release": "r1"}})
    _patch_run(monkeypatch, items, tmp_path)
    caplog.set_level("INFO")
    dupes.run(merge_ids=[2, 1])
    assert "[ALBUM]" not in caplog.text


def test_an_uncertain_pair_two_rules_found_prints_its_merge_command_once(monkeypatch, tmp_path, caplog) -> None:
    """An AcoustID match too far apart, and the same title and artist: one pair, one command."""
    a = FakeItem(10, "Made Up Song", artist="Some Band", length=200.0, acoustid_id="aid")
    b = FakeItem(11, "Made Up Song", artist="Some Band", length=230.0, acoustid_id="aid")
    items = [_on_disk(tmp_path, i) for i in (a, b)]
    _patch_run(monkeypatch, items, tmp_path)
    caplog.set_level("INFO")
    _, uncertain = dupes.run()
    assert len(uncertain[0].evidence) == 2
    assert caplog.text.count("if one recording: music-audit-dupes --merge") == 1
