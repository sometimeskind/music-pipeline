"""The album copy supersedes loose copies that predate it (#266).

Library tests generate audio with ffmpeg (skipped where it is missing; the dev image has it).
"""

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ALBUM = {"album": "Blood On The Tracks", "albumartist": "Bob Dylan", "artist": "Bob Dylan", "year": 1975,
         "tracktotal": 10, "disc": 1, "disctotal": 1, "spotify_album_id": "BOTT"}
GRAB = datetime(2026, 10, 10, 21, 0, tzinfo=timezone.utc)
IMPORTED = datetime(2026, 10, 10, 22, 0, tzinfo=timezone.utc)


@pytest.fixture
def ffmpeg():
    import shutil

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")


@pytest.fixture
def lib(tmp_path):
    from beets import config

    from music_scan.library import MusicLibrary

    config["paths"].set({"default": "$albumartist/$album/$track - $title",
                         "singleton": "$albumartist/$album/$track - $title"})
    with MusicLibrary(tmp_path / "library.db", tmp_path / "library") as library:
        yield library


def _add(lib, path: Path, **fields):
    from beets.library import Item

    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:a", "aac", str(path)], check=True)
    item = Item(path=str(path), **fields)
    lib._lib.add(item)
    item.write()
    item.added = (GRAB + timedelta(minutes=30)).timestamp() if fields.get("via") == "usenet" else GRAB.timestamp() - 1
    item.store()
    return item


def _dylan(tmp_path, lib, **album_fields):
    """A spotdl Simple Twist of Fate (aaaaaah) on the album, and the Usenet release's copy (keep)."""
    root = tmp_path / "library" / "Bob Dylan" / "Blood On The Tracks"
    loose = _add(lib, root / "04 - Simple Twist of Fate.m4a", title="Simple Twist of Fate", track=4, via="spotdl",
                 sources="aaaaaah", spotify_ids="TWIST", isrc="USSM10000004", mb_trackid="REC-OLD", **ALBUM)
    fields = {**ALBUM, "spotify_album_id": None, "title": "Simple Twist Of Fate", "track": 4, "via": "usenet",
              "sources": "keep", "mb_trackid": "REC-NEW", **album_fields}
    album = _add(lib, root / "04 - Simple Twist Of Fate.1.m4a", **fields)
    tangled = _add(lib, root / "01 - Tangled Up in Blue.m4a", title="Tangled Up in Blue", track=1, via="usenet",
                   sources="keep", spotify_ids="TANGLED", **ALBUM)
    return loose, album, tangled


def test_disc_track_on_the_album_supersedes_the_loose_copy_and_its_playlists_follow(tmp_path, ffmpeg, lib,
                                                                                     monkeypatch):
    from music_scan import scan, supersede

    loose, album, tangled = _dylan(tmp_path, lib)
    pairs, kept = supersede.plan([album, tangled], lib.all_items(), tracks_count=10)
    assert [(p.album.id, [i.id for i in p.loose], p.how) for p in pairs] == [(album.id, [loose.id], ["disc/track"])]
    assert kept == []
    assert lib.get_item(loose.id) is not None  # planning changes nothing

    replaced = tmp_path / "quarantine" / "replaced"
    assert supersede.apply_pairs(pairs, replaced) == 1
    assert lib.get_item(loose.id) is None
    assert not Path(os.fsdecode(loose.path)).exists() and any(replaced.iterdir())
    kept_item = lib.get_item(album.id)
    assert set(kept_item.sources.split(",")) == {"keep", "aaaaaah"}
    assert kept_item.spotify_ids == "TWIST" and "USSM10000004" in kept_item.isrc
    assert kept_item.mb_trackid == "REC-OLD"  # Navidrome keeps the loose item's plays and stars

    # aaaaaah's .m3u (its store's view, homelab#1914) now lists the album copy.
    from music_fetch import store

    m3u = tmp_path / "playlists"
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "store")
    store.Store.from_songs("aaaaaah", [
        {"name": "Simple Twist of Fate", "artists": ["Bob Dylan"], "url": "https://open.spotify.com/track/TWIST"}],
    ).save(store.path("aaaaaah"))
    monkeypatch.setattr(scan, "PLAYLISTS", m3u)
    monkeypatch.setattr(scan, "LIBRARY_DB", tmp_path / "library.db")
    monkeypatch.setattr(scan, "SPOTDL_DIR", tmp_path / "spotdl")
    scan.regen_playlists()
    assert (m3u / "aaaaaah.m3u").read_text().strip() == os.path.relpath(os.fsdecode(kept_item.path), m3u)


@pytest.mark.parametrize("fields, how", [
    ({"spotify_ids": "TWIST", "tracktotal": 0}, "spotify id"),
    ({"isrc": "USSM10000004", "tracktotal": 0}, "isrc"),
    ({"mb_trackid": "REC-OLD", "tracktotal": 0}, "recording id"),
])
def test_an_id_supersedes_anywhere(tmp_path, ffmpeg, lib, fields, how):
    from music_scan import supersede

    loose, album, tangled = _dylan(tmp_path, lib, **fields)
    pairs, _ = supersede.plan([album, tangled], lib.all_items(), tracks_count=10)
    assert [(p.album.id, p.how) for p in pairs] == [(album.id, [how])]


@pytest.mark.parametrize("fields", [
    {"spotify_ids": "TWIST", "words_ids": "TWIST", "tracktotal": 0},  # tag_album_ids' title rung
    {"tracktotal": 12},  # another edition's numbering
    {"isrc": "GBAAA0000001"},  # disjoint ISRCs: another recording at that slot
    {"track": 5},
])
def test_no_id_match_keeps_both_and_reports(tmp_path, ffmpeg, lib, caplog, fields):
    import logging

    from music_scan import supersede

    caplog.set_level(logging.INFO)
    loose, album, tangled = _dylan(tmp_path, lib, **fields)
    pairs, kept = supersede.plan([album, tangled], lib.all_items(), tracks_count=10)
    assert pairs == [] and [k.item.id for k in kept] == [loose.id]
    supersede.log(pairs, kept, apply=False)
    assert "[KEEP]" in caplog.text


def test_a_manual_import_is_never_superseded(tmp_path, ffmpeg, lib):
    from music_scan import supersede

    loose, album, tangled = _dylan(tmp_path, lib)
    loose["via"] = "manual"
    loose.store()
    pairs, kept = supersede.plan([album, tangled], lib.all_items(), tracks_count=10)
    assert pairs == [] and kept == []


def test_backfill_attributes_the_release_and_applies_only_with_apply(tmp_path, ffmpeg, lib):
    from music_scan import supersede

    loose, album, tangled = _dylan(tmp_path, lib)
    records = {"BOTT": {"status": "imported", "name": "Blood On The Tracks", "artist": "Bob Dylan",
                        "tracks_count": 10, "grabbed_at": GRAB.isoformat(), "imported_at": IMPORTED.isoformat()}}
    assert [p.album.id for p in supersede.backfill(lib, records, apply=False)] == [album.id]
    assert lib.get_item(loose.id) is not None
    replaced = tmp_path / "replaced"
    supersede.backfill(lib, records, apply=True, replaced_dir=replaced)
    assert lib.get_item(loose.id) is None and any(replaced.iterdir())
    assert supersede.backfill(lib, records, apply=True, replaced_dir=replaced) == []


KAPUTT = {"album": "Kaputt", "albumartist": "Destroyer", "artist": "Destroyer", "year": 2011, "tracktotal": 9,
          "disc": 1, "disctotal": 1, "title": "Kaputt", "track": 6}
# The representative's tracklist entry: [name, artist, song_id, isrc, disc, track].
KAPUTT_6 = ["Kaputt", "Destroyer", "REP6", "USMRG1100006", 1, 6]


def _kaputt(tmp_path, lib, loose_isrc="USMRG1100006", album_isrc=None):
    """A spotdl Kaputt from the twin release (TWIN6), which canon moved onto the
    representative (REP), and the Usenet copy beside it as a .1, holding REP6 (#274)."""
    root = tmp_path / "library" / "Destroyer" / "Kaputt"
    loose = _add(lib, root / "06 - Kaputt.m4a", via="spotdl", sources="kaputt-single", spotify_ids="TWIN6",
                 spotify_album_id="REP", **({"isrc": loose_isrc} if loose_isrc else {}), **KAPUTT)
    album = _add(lib, root / "06 - Kaputt.1.m4a", via="usenet", sources="destroyer", spotify_ids="REP6",
                 spotify_album_id="REP", **({"isrc": album_isrc} if album_isrc else {}), **KAPUTT)
    return loose, album


def test_a_twins_loose_copy_is_superseded_through_the_tracklist_and_its_path_freed(tmp_path, ffmpeg, lib,
                                                                                   caplog):
    import logging

    from music_scan import supersede

    caplog.set_level(logging.INFO)
    loose, album = _kaputt(tmp_path, lib)
    pairs, kept = supersede.plan([album], lib.all_items(), tracks_count=9, tracks=[KAPUTT_6])
    assert [(p.album.id, [i.id for i in p.loose], p.how) for p in pairs] == [
        (album.id, [loose.id], ["tracklist isrc"])]
    assert kept == []
    supersede.log(pairs, kept, apply=False)
    assert f"[MOVE] {album.id} " in caplog.text and "06 - Kaputt.m4a: the path its loose copy held" in caplog.text

    loose_path = os.fsdecode(loose.path)
    assert supersede.apply_pairs(pairs, tmp_path / "replaced") == 1
    kept_item = lib.get_item(album.id)
    assert os.fsdecode(kept_item.path) == loose_path and Path(loose_path).exists()  # no .1 clash left
    assert not (tmp_path / "library" / "Destroyer" / "Kaputt" / "06 - Kaputt.1.m4a").exists()
    assert set(kept_item.spotify_ids.split(",")) == {"REP6", "TWIN6"}
    assert kept_item.sources.split(",") == ["destroyer", "kaputt-single"]


def test_a_twins_loose_copy_with_no_isrc_is_superseded_by_its_slot(tmp_path, ffmpeg, lib):
    from music_scan import supersede

    loose, album = _kaputt(tmp_path, lib, loose_isrc=None)
    pairs, _ = supersede.plan([album], lib.all_items(), tracks_count=9, tracks=[KAPUTT_6])
    assert [(p.album.id, p.how) for p in pairs] == [(album.id, ["tracklist disc/track"])]


@pytest.mark.parametrize("kwargs, tracks, holder, why", [
    ({}, [], {}, "no ID matches an item of the release, and the record has no tracklist"),
    ({"loose_isrc": "GBAAA0000001"}, [KAPUTT_6], {},
     "its ISRCs GBAAA0000001 differ from track 1-6's (USMRG1100006, album item"),
    ({}, [["Kaputt", "Destroyer", "TWIN6", None, 1, 6]], {},
     "its Spotify ID is the album's track 1-6, which no item of the release holds"),
    ({"loose_isrc": None}, [KAPUTT_6], {"words_ids": "REP6"}, "holds by title words only (#255)"),
    ({"loose_isrc": None}, [[*KAPUTT_6[:5], 7]], {},
     "its Spotify ID is not on the album's tracklist (a twin release's?) and no entry sits at its disc/track 1-6"),
])
def test_a_kept_loose_item_says_why(tmp_path, ffmpeg, lib, caplog, kwargs, tracks, holder, why):
    import logging

    from music_scan import supersede

    caplog.set_level(logging.INFO)
    loose, album = _kaputt(tmp_path, lib, **kwargs)
    for k, v in holder.items():
        album[k] = v
    album.store()
    pairs, kept = supersede.plan([album], lib.all_items(), tracks_count=9, tracks=tracks)
    assert pairs == [] and [k.item.id for k in kept] == [loose.id]
    supersede.log(pairs, kept, apply=False)
    assert f"[KEEP] {loose.id} " in caplog.text and why in caplog.text


def test_backfill_resolves_a_twin_through_the_records_tracklist(tmp_path, ffmpeg, lib):
    from music_scan import supersede

    loose, album = _kaputt(tmp_path, lib)
    records = {"REP": {"status": "imported", "name": "Kaputt", "artist": "Destroyer", "tracks_count": 9,
                       "playlists": {"destroyer": [KAPUTT_6]},
                       "grabbed_at": GRAB.isoformat(), "imported_at": IMPORTED.isoformat()}}
    assert [(p.album.id, p.how) for p in supersede.backfill(lib, records, apply=False)] == [
        (album.id, ["tracklist isrc"])]


# Same audio (#325): fingerprints as the fake decoder gives them.  A re-encode
# flips about 1 bit in 32 (BER ~0.03); another track by a repetitive artist
# about 4 (~0.12).
def _frames(seed: int, flips: int = 0) -> list[int]:
    import random

    rng, noise = random.Random(seed), random.Random(9)
    out = []
    for _ in range(400):
        f = rng.getrandbits(32)
        for _ in range(flips):
            f ^= 1 << noise.randrange(32)
        out.append(f)
    return out


FPS = {"love": _frames(1), "love-reencode": _frames(1, flips=1), "love-lookalike": _frames(1, flips=4),
       "other": _frames(2), "other-2": _frames(3)}
PUMPKINS = {"album": "Rotten Apples", "albumartist": "The Smashing Pumpkins", "artist": "The Smashing Pumpkins",
            "tracktotal": 18, "disc": 1, "disctotal": 1, "spotify_album_id": "APPLES"}
# Spotify's tracklist: Stand Inside Your Love is 1-16, held by the loose copy's ID.
APPLES = [["Stand Inside Your Love", "The Smashing Pumpkins", "LOVE", None, 1, 16],
          ["Perfect", "The Smashing Pumpkins", "PERFECT", None, 1, 15]]


def _pumpkins(tmp_path, lib, loose_fp="love-reencode", album_fp="love", **album_fields):
    """The spotdl Stand Inside Your Love at Spotify's 1-16, and the release's
    copy, which the release numbered 15 and tag_album_ids gave no Spotify ID
    (item 548, 2026-10-12)."""
    root = tmp_path / "library" / "The Smashing Pumpkins" / "Rotten Apples"
    loose = _add(lib, root / "16 - Stand Inside Your Love.m4a", title="Stand Inside Your Love", track=16,
                 via="spotdl", sources="aaaaaah", spotify_ids="LOVE", length=254.0,
                 **({"acoustid_fingerprint": loose_fp} if loose_fp else {}), **PUMPKINS)
    fields = {**PUMPKINS, "title": "Stand Inside Your Love", "track": 15, "via": "usenet", "sources": "keep",
              "length": 253.4, **({"acoustid_fingerprint": album_fp} if album_fp else {}), **album_fields}
    album = _add(lib, root / "15 - Stand Inside Your Love.m4a", **fields)
    sixteen = _add(lib, root / "16 - Perfect.m4a", title="Perfect", track=16, via="usenet", sources="keep",
                   spotify_ids="PERFECT", length=203.0, acoustid_fingerprint="other", **PUMPKINS)
    return loose, album, sixteen


def test_the_same_audio_numbered_differently_supersedes_the_loose_copy(tmp_path, ffmpeg, lib, caplog):
    import logging

    from music_scan import supersede

    caplog.set_level(logging.INFO)
    loose, album, sixteen = _pumpkins(tmp_path, lib)
    pairs, kept = supersede.plan([album, sixteen], lib.all_items(), tracks_count=18, tracks=APPLES,
                                 decoder=FPS.__getitem__)
    assert [(p.album.id, [i.id for i in p.loose]) for p in pairs] == [(album.id, [loose.id])]
    assert pairs[0].how[0].startswith("same audio, fingerprint ber=0.0") and kept == []
    supersede.log(pairs, kept, apply=False)
    assert f"[SUPERSEDE] {album.id} " in caplog.text and "(same audio, fingerprint" in caplog.text

    assert supersede.apply_pairs(pairs, tmp_path / "replaced") == 1
    assert lib.get_item(loose.id) is None
    assert lib.get_item(album.id).spotify_ids == "LOVE"


@pytest.mark.parametrize("loose_fp, album_fp, album_fields, why", [
    ("love-lookalike", "love", {}, "the fingerprints differ"),  # BER ~0.12: another track
    ("love-reencode", "love", {"length": 380.0}, "fingerprint ber=0.0"),  # an extended mix's length
    # Wrong audio under its tags, at the loose item's slot (numbered off Spotify's count, so no disc/track rung).
    ("love-reencode", "love", {"title": "Perfect (Live)", "track": 16, "tracktotal": 0}, "the titles differ"),
    (None, "love", {}, "it has no fingerprint, so the same-audio rung was skipped"),
    ("love-reencode", None, {}, "have no fingerprint, so the same-audio rung skipped them"),
])
def test_the_same_audio_rung_vetoes_and_skips(tmp_path, ffmpeg, lib, caplog, loose_fp, album_fp, album_fields,
                                              why):
    import logging

    from music_scan import supersede

    caplog.set_level(logging.INFO)
    loose, album, sixteen = _pumpkins(tmp_path, lib, loose_fp, album_fp, **album_fields)
    pairs, kept = supersede.plan([album, sixteen], lib.all_items(), tracks_count=18, tracks=APPLES,
                                 decoder=FPS.__getitem__)
    assert pairs == [] and [k.item.id for k in kept] == [loose.id]
    supersede.log(pairs, kept, apply=False)
    assert "which no item of the release holds; " in caplog.text and why in caplog.text


SIMPLE = {"album": "Simple Things", "albumartist": "Zero 7", "artist": "Zero 7", "tracktotal": 12, "disc": 1,
          "disctotal": 1, "spotify_album_id": "SIMPLE"}


def test_an_extended_mix_on_the_album_stays_kept(tmp_path, ffmpeg, lib, caplog):
    """Zero 7's Destiny - Extended Mix: the release holds the album mix, the
    same base title and the same opening audio, minutes shorter."""
    import logging

    from music_scan import supersede

    caplog.set_level(logging.INFO)
    root = tmp_path / "library" / "Zero 7" / "Simple Things"
    loose = _add(lib, root / "13 - Destiny - Extended Mix.m4a", title="Destiny - Extended Mix", track=13,
                 via="spotdl", sources="aaaaaah", spotify_ids="DESTINY-EXT", length=486.0,
                 acoustid_fingerprint="love-reencode", **SIMPLE)
    album = _add(lib, root / "03 - Destiny.m4a", title="Destiny", track=3, via="usenet", sources="keep",
                 spotify_ids="DESTINY", length=336.0, acoustid_fingerprint="love", **SIMPLE)
    pairs, kept = supersede.plan([album], lib.all_items(), tracks_count=12,
                                 tracks=[["Destiny", "Zero 7", "DESTINY", None, 1, 3],
                                         ["Destiny - Extended Mix", "Zero 7", "DESTINY-EXT", None, 1, 13]],
                                 decoder=FPS.__getitem__)
    assert pairs == [] and [k.item.id for k in kept] == [loose.id]
    supersede.log(pairs, kept, apply=False)
    assert f"album item {album.id}: fingerprint ber=0.03 but 150s apart" in caplog.text


def test_remixes_on_the_expanded_edition_stay_kept(tmp_path, ffmpeg, lib):
    """Two Four Tet remixes on the album's expanded edition: the release holds
    the originals, the same base titles and other audio."""
    from music_scan import supersede

    fields = {"album": "Expanded", "albumartist": "Somebody", "artist": "Somebody", "tracktotal": 14, "disc": 1,
              "disctotal": 1, "spotify_album_id": "EXPANDED"}
    root = tmp_path / "library" / "Somebody" / "Expanded"
    one = _add(lib, root / "01 - One.m4a", title="One", track=1, via="usenet", sources="keep", spotify_ids="ONE",
               length=240.0, acoustid_fingerprint="love", **fields)
    two = _add(lib, root / "02 - Two.m4a", title="Two", track=2, via="usenet", sources="keep", spotify_ids="TWO",
               length=300.0, acoustid_fingerprint="other", **fields)
    remixes = [_add(lib, root / f"{n} - {t} (Four Tet Remix).m4a", title=f"{t} (Four Tet Remix)", track=n,
                    via="spotdl", sources="aaaaaah", spotify_ids=f"{t.upper()}-RMX", length=length,
                    acoustid_fingerprint=fp, **fields)
               for n, t, length, fp in [(13, "One", 241.0, "love-lookalike"), (14, "Two", 299.0, "other-2")]]
    tracks = [["One", "Somebody", "ONE", None, 1, 1], ["Two", "Somebody", "TWO", None, 1, 2],
              ["One (Four Tet Remix)", "Somebody", "ONE-RMX", None, 1, 13],
              ["Two (Four Tet Remix)", "Somebody", "TWO-RMX", None, 1, 14]]
    pairs, kept = supersede.plan([one, two], lib.all_items(), tracks_count=14, tracks=tracks,
                                 decoder=FPS.__getitem__)
    assert pairs == [] and [k.item.id for k in kept] == [r.id for r in remixes]
    assert all("the fingerprints differ" in k.why for k in kept)


def _single(tmp_path, lib, **fields):
    """The spotdl Stand Inside Your Love filed under its single (one track, an
    album of its own in Navidrome), no ID shared with the release (#328)."""
    root = tmp_path / "library" / "The Smashing Pumpkins" / "Stand Inside Your Love"
    single = {"album": "Stand Inside Your Love", "albumartist": "The Smashing Pumpkins",
              "artist": "The Smashing Pumpkins", "tracktotal": 1, "track": 1, "disc": 1, "disctotal": 1,
              "spotify_album_id": "SINGLE", "title": "Stand Inside Your Love", "via": "spotdl",
              "sources": "aaaaaah", "spotify_ids": "LOVE-SINGLE", "length": 254.0,
              "acoustid_fingerprint": "love-reencode", **fields}
    return _add(lib, root / f"{single['track']:02d} - {single['title']}.m4a", **single)


def test_the_same_audio_filed_under_its_single_supersedes_it(tmp_path, ffmpeg, lib, caplog):
    import logging

    from music_scan import supersede

    caplog.set_level(logging.INFO)
    _, album, sixteen = _pumpkins(tmp_path, lib)
    single = _single(tmp_path, lib)
    pairs, kept = supersede.plan([album, sixteen], [single, album, sixteen], tracks_count=18, tracks=APPLES,
                                 decoder=FPS.__getitem__)
    assert [(p.album.id, [i.id for i in p.loose]) for p in pairs] == [(album.id, [single.id])]
    assert pairs[0].how[0].startswith("same audio, fingerprint ber=0.0") and kept == []


@pytest.mark.parametrize("fields, why", [
    ({"acoustid_fingerprint": "love-lookalike"}, "filed under Stand Inside Your Love, but no release item has "
                                                 "its audio (album item"),
    ({"length": 300.0}, "but 47s apart"),
    ({"acoustid_fingerprint": None}, "it has no fingerprint, so the same-audio rung was skipped"),
])
def test_an_off_album_copy_with_other_audio_is_kept_and_reported(tmp_path, ffmpeg, lib, caplog, fields, why):
    import logging

    from music_scan import supersede

    caplog.set_level(logging.INFO)
    _, album, sixteen = _pumpkins(tmp_path, lib)
    single = _single(tmp_path, lib, **fields)
    pairs, kept = supersede.plan([album, sixteen], [single, album, sixteen], tracks_count=18, tracks=APPLES,
                                 decoder=FPS.__getitem__)
    assert pairs == [] and [k.item.id for k in kept] == [single.id]
    supersede.log(pairs, kept, apply=False)
    assert f"[KEEP] {single.id} " in caplog.text and why in caplog.text


@pytest.mark.parametrize("fields", [
    {"artist": "Somebody Else", "albumartist": "Somebody Else"},  # a cover: another artist's recording
    {"album_record": "OTHER"},  # another album's track: the album is the unit
    {"title": "Tonight, Tonight"},  # no candidate by title: silent
])
def test_an_off_album_copy_without_a_candidate_is_left_alone_silently(tmp_path, ffmpeg, lib, fields):
    from music_scan import supersede

    _, album, sixteen = _pumpkins(tmp_path, lib)
    single = _single(tmp_path, lib, **fields)
    pairs, kept = supersede.plan([album, sixteen], [single, album, sixteen], tracks_count=18, tracks=APPLES,
                                 decoder=FPS.__getitem__)
    assert pairs == [] and kept == []


def test_a_best_of_keeps_its_tracks(tmp_path, ffmpeg, lib):
    """A best-of spotdl downloaded track by track: the same audio as the
    release's Stand Inside Your Love, but its album has other tracks."""
    from music_scan import supersede

    _, album, sixteen = _pumpkins(tmp_path, lib)
    best = {"album": "Greatest Hits", "albumartist": "The Smashing Pumpkins", "tracktotal": 2}
    love = _single(tmp_path, lib, **best)
    other = _single(tmp_path, lib, title="Tonight, Tonight", track=2, spotify_ids="TONIGHT",
                    acoustid_fingerprint="other-2", **best)
    pairs, kept = supersede.plan([album, sixteen], [love, other, album, sixteen], tracks_count=18,
                                 tracks=APPLES, decoder=FPS.__getitem__)
    assert pairs == [] and kept == []
