"""The album copy supersedes loose copies that predate it (#266).

Library tests generate audio with ffmpeg (skipped where it is missing; the dev image has it).
"""

import json
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

    # aaaaaah's .m3u now lists the album copy.
    spotdl, m3u = tmp_path / "spotdl", tmp_path / "playlists"
    spotdl.mkdir()
    (spotdl / "aaaaaah.spotdl").write_text(json.dumps({"songs": [
        {"name": "Simple Twist of Fate", "artists": ["Bob Dylan"], "url": "https://open.spotify.com/track/TWIST"}]}))
    monkeypatch.setattr(scan, "SPOTDL_DIR", spotdl)
    monkeypatch.setattr(scan, "PLAYLISTS", m3u)
    monkeypatch.setattr(scan, "LIBRARY_DB", tmp_path / "library.db")
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
