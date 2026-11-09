"""The two-way sync's merge core (homelab#2361): the classification table, the
six rules, the snapshot take, the Navidrome writes and the breaker.  Pure data
in, a plan out."""

from __future__ import annotations

import pytest

from music_scan import playlist_sync as ps
from music_scan.playlist_sync import Add, Entry, Held, Track, plan


def sp(n: int, item: int | None = None, **kw) -> Entry:
    """Spotify entry ``spotify:s<n>``, resolving to *item* (default *n*)."""
    return Entry(f"spotify:s{n}", n if item is None else item, **kw)


def loc(item: int) -> Entry:
    return Entry(f"local:{item}", item, local=True)


def tr(item: int | None, song: str | None = None, **kw) -> Track:
    """Navidrome track of *item*, its song ``m<item>`` by default."""
    return Track(song or f"m{item}", item, **kw)


def held(*items: int) -> list[Held]:
    return [Held(i, f"m{i}") for i in items]


LIB = frozenset(range(1, 100))


def steady(n: int = 3):
    """A playlist in step: store, snapshot and Navidrome all hold items 1..n."""
    return [sp(i) for i in range(1, n + 1)], held(*range(1, n + 1)), [tr(i) for i in range(1, n + 1)]


def test_in_step_is_a_no_op():
    store, snap, tracks = steady()
    p = plan(store, snap, tracks, LIB)
    assert p.trips == [] and not p.taken
    assert p.classes == {1: ps.PRESENT, 2: ps.PRESENT, 3: ps.PRESENT}
    assert p.local_deletes == p.adds == p.spotify_removes == p.navidrome_adds == p.navidrome_removes == []
    assert p.view == [1, 2, 3] and p.snapshot == held(1, 2, 3)
    assert set(p.pending().values()) == {0}


# --- The classification table ---------------------------------------------------------------


@pytest.mark.parametrize("tracks, library, store, expected", [
    # A track maps to it by path: present (even under another song ID).
    ([tr(1), tr(2, song="new"), tr(3)], LIB, None, ps.PRESENT),
    # Its song is there but missing: unmapped.
    ([tr(1), tr(2, missing=True), tr(3)], LIB, None, ps.UNMAPPED),
    # Its song is there but the path maps to no item (a wrong mount mapping): unmapped.
    ([tr(1), tr(None, song="m2"), tr(3)], LIB, None, ps.UNMAPPED),
    # A missing track of the item under another song: unmapped.
    ([tr(1), tr(2, song="other", missing=True), tr(3)], LIB, None, ps.UNMAPPED),
    # Its song is missing and the item left the library: still unmapped, since a track is there.
    ([tr(1), tr(None, song="m2", missing=True), tr(3)], LIB - {2}, None, ps.UNMAPPED),
    # No track, the item left beets (retired, superseded, merged): hidden.
    ([tr(1), tr(3)], LIB - {2}, None, ps.HIDDEN),
    # No track, the item exists but no store entry resolves to it now: hidden.
    ([tr(1), tr(3)], LIB, [sp(1), Entry("spotify:s2"), sp(3)], ps.HIDDEN),
    # No track, store entries resolve to it now: local delete.
    ([tr(1), tr(3)], LIB, None, ps.LOCAL_DELETE),
])
def test_classification_table(tracks, library, store, expected):
    base, snap, _ = steady()
    p = plan(store or base, snap, tracks, library)
    assert p.classes[2] == expected
    deleting = expected == ps.LOCAL_DELETE
    assert p.local_deletes == ([2] if deleting else [])
    assert p.spotify_removes == (["spotify:s2"] if deleting else [])


def test_a_wrong_mount_mapping_blinds_instead_of_deleting():
    store, snap, _ = steady(5)
    tracks = [tr(None, song=f"m{i}") for i in range(1, 6)]
    p = plan(store, snap, tracks, LIB)
    assert set(p.classes.values()) == {ps.UNMAPPED}
    assert p.local_deletes == p.spotify_removes == p.navidrome_adds == p.navidrome_removes == []
    assert p.snapshot == held(1, 2, 3, 4, 5)


def test_supersede_is_hidden_plus_held():
    """Item 2 replaced by 7 (same recording): the store's entry now resolves to 7,
    the .m3u holds 7, and 2 left beets.  Two no-ops."""
    store = [sp(1), sp(2, item=7), sp(3)]
    p = plan(store, held(1, 2, 3), [tr(1), tr(7), tr(3)], LIB - {2})
    assert p.classes[2] == ps.HIDDEN
    assert p.adds == p.local_deletes == p.navidrome_adds == p.navidrome_removes == []
    assert p.view == [1, 7, 3]
    assert p.snapshot == held(1, 7, 3)


def test_a_download_since_the_snapshot_is_absorbed():
    """Against the view, item 4 (downloaded after the last cycle) would read as
    a local delete or add; against the snapshot it is neither."""
    store = [sp(1), sp(2), sp(3), sp(4)]
    snap = held(1, 2, 3)
    assert plan(store, snap, [tr(1), tr(2), tr(3), tr(4)], LIB).pending()["local_add"] == 0
    p = plan(store, snap, [tr(1), tr(2), tr(3)], LIB)
    assert p.local_deletes == [] and p.navidrome_adds == [4]


def test_a_spotify_removal_reaches_navidrome_and_is_hidden():
    """The nightly dropped entry 2 and the .m3u followed: hidden.  Before the
    re-import Navidrome still holds it: a Navidrome remove, not a local add."""
    store = [sp(1), sp(3)]
    assert plan(store, held(1, 2, 3), [tr(1), tr(3)], LIB).classes[2] == ps.HIDDEN
    p = plan(store, held(1, 2, 3), [tr(1), tr(2), tr(3)], LIB)
    assert p.classes[2] == ps.PRESENT
    assert p.adds == [] and p.navidrome_removes == ["m2"]


# --- Local adds -----------------------------------------------------------------------------


def test_local_add_promotable_is_a_promotion_after_the_track_before_it():
    store, snap, _ = steady()
    p = plan(store, snap, [tr(1), tr(2), tr(9, promotable=True), tr(3)], LIB)
    assert p.adds == [Add(9, "spotify:s2", True)]
    assert p.pending()["promotion"] == 1 and p.spotify_writes == 1
    assert [e.key for e in p.entries] == ["spotify:s1", "spotify:s2", "promote:9", "spotify:s3"]
    assert p.view == [1, 2, 9, 3] and p.navidrome_adds == []
    assert Held(9, "m9") in p.snapshot


def test_local_add_without_identity_is_a_local_entry():
    store, snap, _ = steady()
    p = plan(store, snap, [tr(9), tr(1), tr(2), tr(3)], LIB)
    assert p.adds == [Add(9, None, False)]
    assert p.entries[0] == Entry("local:9", 9, local=True)
    assert p.spotify_writes == 0 and p.pending()["local_add"] == 1


def test_an_unmapped_or_missing_track_is_never_an_add():
    store, snap, tracks = steady()
    p = plan(store, snap, [*tracks, tr(None, song="x"), tr(9, missing=True)], LIB)
    assert p.adds == [] and p.navidrome_removes == []


def test_a_duplicate_track_is_one_add_and_its_copy_is_removed():
    store, snap, tracks = steady()
    p = plan(store, snap, [*tracks, tr(9), tr(9, song="m9b")], LIB)
    assert p.adds == [Add(9, "spotify:s3", False)]
    assert p.navidrome_removes == ["m9b"]


# --- Rule 1: a conflict goes to Spotify -----------------------------------------------------


def test_rule1_local_delete_of_an_item_spotify_just_added():
    """Store entry 2 resolves to item 2; Spotify also just added another entry
    (s8) resolving to 2.  The local delete is dropped: Spotify's state wins."""
    store, snap, _ = steady()
    spotify = [*store, sp(8, item=2)]
    p = plan(store, snap, [tr(1), tr(3)], LIB, spotify=spotify)
    assert p.classes[2] == ps.CONFLICT and p.conflicts == [2]
    assert p.local_deletes == p.spotify_removes == []
    assert p.navidrome_adds == [2]


def test_rule1_an_item_spotify_just_removed_is_never_an_add():
    """9 was downloaded after the snapshot, so it isn't in it; Spotify then
    removed it.  The store held it, so Navidrome holding it is no add: the
    removal flows to Navidrome."""
    store = [sp(1), sp(2), sp(3), sp(9)]
    p = plan(store, held(1, 2, 3), [tr(1), tr(2), tr(3), tr(9, promotable=True)], LIB,
             spotify=[sp(1), sp(2), sp(3)])
    assert p.adds == [] and p.spotify_writes == 0
    assert p.navidrome_removes == ["m9"] and p.view == [1, 2, 3]


def test_rule1_concurrent_add_on_both_sides_is_one_entry():
    store, snap, _ = steady()
    spotify = [*store, sp(9)]
    p = plan(store, snap, [tr(1), tr(2), tr(3), tr(9, promotable=True)], LIB, spotify=spotify)
    assert p.adds == [] and p.spotify_writes == 0
    assert p.view == [1, 2, 3, 9]


def test_both_sides_deleting_is_agreement():
    store, snap, _ = steady()
    p = plan(store, snap, [tr(1), tr(3)], LIB, spotify=[sp(1), sp(3)])
    assert p.classes[2] == ps.LOCAL_DELETE and p.spotify_removes == []


# --- Rule 2: placement ----------------------------------------------------------------------


def test_rule2_walks_back_past_tracks_deleted_this_cycle():
    store = [sp(1), sp(2), sp(3)]
    # Navidrome: 1, 2, (new 9).  Spotify removed 2 meanwhile.
    p = plan(store, held(1, 2, 3), [tr(1), tr(2), tr(9), tr(3)], LIB, spotify=[sp(1), sp(3)])
    assert p.adds == [Add(9, "spotify:s1", False)]
    assert [e.key for e in p.entries] == ["spotify:s1", "local:9", "spotify:s3"]


def test_rule2_walks_back_past_a_local_delete():
    store, snap, _ = steady()
    p = plan(store, snap, [tr(1), tr(9), tr(3)], LIB)  # 2 deleted, 9 added in its place
    assert p.local_deletes == [2]
    assert [e.key for e in p.entries] == ["spotify:s1", "local:9", "spotify:s3"]


def test_rule2_head_when_nothing_before_survives():
    store, snap, _ = steady()
    p = plan(store, snap, [tr(9), tr(2), tr(3)], LIB)  # 1 deleted, 9 before everything
    assert p.adds == [Add(9, None, False)] and p.entries[0].key == "local:9"


def test_rule2_consecutive_adds_keep_their_order():
    store, snap, _ = steady()
    p = plan(store, snap, [tr(1), tr(8), tr(9), tr(2), tr(3)], LIB)
    assert p.adds == [Add(8, "spotify:s1", False), Add(9, "local:8", False)]
    assert p.view == [1, 8, 9, 2, 3]


def test_rule2_existing_local_entries_keep_their_place_across_a_spotify_reorder():
    store = [sp(1), sp(2), loc(9), sp(3)]
    snap = held(1, 2, 9, 3)
    spotify = [sp(3), sp(2), sp(1)]
    p = plan(store, snap, [tr(1), tr(2), tr(9), tr(3)], LIB, spotify=spotify)
    assert [e.key for e in p.entries] == ["spotify:s3", "spotify:s2", "local:9", "spotify:s1"]


def test_rule2_local_entries_after_a_duplicate_key_stay_with_their_copy():
    store = [sp(1), loc(8), sp(1), loc(9)]
    p = plan(store, held(1, 8, 9), [tr(1), tr(8), tr(9)], LIB)
    assert [e.key for e in p.entries] == [e.key for e in store]


# --- Rule 3: unplayable Spotify entries -----------------------------------------------------


def test_rule3_unplayable_entries_are_in_no_view_and_never_deleted():
    store = [sp(1), Entry("url:spotify:local:x", 2, playable=False), sp(3)]
    p = plan(store, held(1, 3), [tr(1), tr(3)], LIB)
    assert p.view == [1, 3]
    assert p.spotify_removes == [] and p.navidrome_adds == []
    # Item 2 in Navidrome is a local add, but the unplayable entry isn't what holds it.
    p = plan(store, held(1, 3), [tr(1), tr(2), tr(3)], LIB)
    assert p.adds == [Add(2, "spotify:s1", False)]


def test_rule3_unplayable_entries_count_toward_no_rule():
    store = [sp(1)] + [Entry(f"url:x{i}", 50 + i, playable=False) for i in range(30)]
    p = plan(store, held(1), [tr(1)], LIB)
    assert p.trips == [] and p.spotify_writes == 0
    # A store of only unplayable entries isn't "the store holds Spotify entries".
    assert plan(store[1:], [], [], LIB, spotify=[]).trips == []


# --- Rule 4: duplicates ---------------------------------------------------------------------


def test_rule4_duplicate_entries_both_stay_and_a_delete_counts_both():
    store = [sp(1), sp(2), sp(2), sp(5, item=2), sp(3)]
    p = plan(store, held(1, 2, 3), [tr(1), tr(2), tr(3)], LIB)
    assert len(p.entries) == 5 and p.view == [1, 2, 3]
    p = plan(store, held(1, 2, 3), [tr(1), tr(3)], LIB)
    assert p.spotify_removes == ["spotify:s2", "spotify:s2", "spotify:s5"]
    assert p.spotify_writes == 3


def test_rule4_a_local_delete_drops_a_local_entry_without_a_spotify_write():
    store = [sp(1), loc(9), sp(3)]
    p = plan(store, held(1, 9, 3), [tr(1), tr(3)], LIB)
    assert p.local_deletes == [9] and p.spotify_removes == []
    assert [e.key for e in p.entries] == ["spotify:s1", "spotify:s3"]


# --- Rule 5: empty reads --------------------------------------------------------------------


def test_rule5_navidrome_empty_with_a_snapshot_trips_and_reports_nothing():
    store, snap, _ = steady()
    p = plan(store, snap, [], LIB)
    assert p.trips == [ps.EMPTY]
    assert p.classes == {} and p.local_deletes == [] and p.spotify_removes == []


def test_rule5_spotify_empty_with_a_store_trips():
    store, snap, tracks = steady()
    assert plan(store, snap, tracks, LIB, spotify=[]).trips == [ps.EMPTY]


def test_rule5_empty_both_ways_with_an_empty_snapshot_is_fine():
    assert plan([], [], [], LIB).trips == []


# --- Rule 6: missing store or snapshot ------------------------------------------------------


def test_rule6_missing_store_trips():
    p = plan(None, held(1), [tr(1)], LIB)
    assert p.trips == [ps.NO_STORE] and p.classes == {}


def test_rule6_missing_snapshot_is_taken_with_nothing_reported():
    store, _, tracks = steady()
    p = plan(store, None, [*tracks, tr(9)], LIB)
    assert p.taken and p.trips == []
    assert p.snapshot == held(1, 2, 3)  # the track outside the view is left out
    assert set(p.pending().values()) == {0}
    # So the next cycle reports it.
    assert plan(store, p.snapshot, [*tracks, tr(9)], LIB).adds == [Add(9, "spotify:s3", False)]


def test_rule6_a_take_that_reads_navidrome_empty_trips():
    store, _, _ = steady()
    assert plan(store, None, [], LIB).trips == [ps.EMPTY]
    assert plan([], None, [], LIB).taken


def test_take_skips_missing_unmapped_and_duplicate_tracks():
    view = [1, 2, 3]
    tracks = [tr(1), tr(1, song="dup"), tr(2, missing=True), tr(None, song="x"), tr(3)]
    assert ps.take(view, tracks) == held(1, 3)


# --- Navidrome writes -----------------------------------------------------------------------


def test_navidrome_writes_against_the_playlist_now():
    store = [sp(1), sp(2), sp(4)]
    p = plan(store, held(1, 2, 3), [tr(1), tr(2), tr(3)], LIB)
    assert p.navidrome_adds == [4] and p.navidrome_removes == ["m3"]
    assert p.snapshot == [Held(1, "m1"), Held(2, "m2"), Held(4, None)]


def test_an_unmapped_item_keeps_its_old_song_and_gets_no_add():
    store, snap, _ = steady()
    p = plan(store, snap, [tr(1), tr(2, missing=True), tr(3)], LIB)
    assert p.navidrome_adds == [] and p.navidrome_removes == []
    assert p.snapshot == held(1, 2, 3)


def test_a_missing_track_of_a_new_view_item_gets_no_duplicate_add():
    store = [sp(1), sp(2), sp(3), sp(4)]
    p = plan(store, held(1, 2, 3), [tr(1), tr(2), tr(3), tr(4, missing=True)], LIB)
    assert p.navidrome_adds == [] and p.adds == []
    assert p.snapshot[-1] == Held(4, "m4")


# --- Breaker --------------------------------------------------------------------------------


def test_breaker_writes_cap_counts_merge_writes_including_promotions():
    store = [sp(i) for i in range(1, 12)]
    snap = held(*range(1, 12))
    tracks = [tr(i, promotable=True) for i in range(20, 30)]
    p = plan(store, snap, [tr(1), *tracks], LIB)  # 10 deletes, 10 promotions: at the cap
    assert p.spotify_writes == 10 + 10
    assert ps.WRITES not in p.trips
    p = plan(store, snap, [*tracks, tr(30, promotable=True)], LIB)
    assert p.spotify_writes == 11 + 11 and ps.WRITES in p.trips


def test_breaker_deletes_per_hour():
    store, snap, _ = steady(8)
    assert ps.DELETES not in plan(store, snap, [tr(i) for i in range(1, 4)], LIB).trips  # 5 deletes
    assert ps.DELETES in plan(store, snap, [tr(i) for i in range(1, 3)], LIB).trips  # 6
    assert ps.DELETES in plan(store, snap, [tr(i) for i in range(1, 8)], LIB, recent_deletes=5).trips


def test_breaker_counts_merge_writes_not_raw_deltas():
    """A week of no-op churn (downloads absorbed, supersedes, Spotify trims) is no Spotify write."""
    store = [sp(i) for i in range(1, 40)] + [sp(i, item=i + 100) for i in range(40, 70)]
    snap = held(*range(1, 60))
    tracks = [tr(i) for i in range(1, 40)] + [tr(i + 100) for i in range(40, 70)]
    p = plan(store, snap, tracks, LIB | set(range(140, 170)))
    assert p.spotify_writes == 0 and p.trips == []
    assert p.pending()["hidden"] == 20
