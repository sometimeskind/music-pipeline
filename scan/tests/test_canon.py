"""Canonical album tags from Spotify (#209).

Library tests generate audio with ffmpeg (skipped where it is missing; the dev image has it).
"""

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

COVER = "https://i.scdn.co/image/canonical"


def _song(sid, album_id="A1", name="Album", album_type="album", date="2019-09-17", track=1, disc=1,
          tracks=10, isrc=None, **extra):
    return {"song_id": sid, "album_id": album_id, "album_name": name, "album_artist": "Artist",
            "album_type": album_type, "date": date, "track_number": track, "disc_number": disc,
            "tracks_count": tracks, "isrc": isrc, "cover_url": COVER, **extra}


class FakeItem(dict):
    def get(self, key, default=None):
        return super().get(key, default)


# ----------------------------------------------------------------------
# Release preference
# ----------------------------------------------------------------------


def _choose(spotify_ids, *sources):
    from music_scan.canon import choose, placements_from

    p = choose(FakeItem(spotify_ids=spotify_ids), placements_from(sources))
    return p.release.album_id if p else None


def test_album_beats_single_and_compilation():
    songs = [_song("S", "SINGLE", album_type="single", date="2018-01-01"),
             _song("C", "COMP", album_type="compilation", date="2010-01-01"),
             _song("A", "ALBUM", date="2019-01-01")]
    assert _choose("S,C,A", (songs, True)) == "ALBUM"
    assert _choose("S,C", (songs, True)) == "SINGLE"
    assert _choose("C", (songs, True)) == "COMP"


def test_named_today_beats_an_old_snapshot_then_earliest_then_id():
    pages = [_song("NEW", "LATER", date="2021-01-01")]
    old = [_song("OLD", "EARLIER", date="2019-01-01")]
    assert _choose("OLD,NEW", (pages, True), (old, False)) == "LATER"
    both = [_song("X", "B2", date="2020-05-01"), _song("Y", "B1", date="2019-05-01")]
    assert _choose("X,Y", (both, True)) == "B1"
    same = [_song("X", "ZZ", date="2020"), _song("Y", "AA", date="2020")]
    assert _choose("Y,X", (same, True)) == _choose("X,Y", (same, True)) == "AA"


def test_no_spotify_id_or_no_entry_is_left_alone():
    songs = [_song("A")]
    assert _choose("", (songs, True)) is None
    assert _choose("UNKNOWN", (songs, True)) is None


def test_placements_first_source_wins_and_disc_total_is_the_highest_seen():
    from music_scan.canon import placements_from, spotify_fields

    placed = placements_from([
        ([_song("T1", disc=1, isrc="I1"), _song("T2", disc=2, track=3, isrc="I2"), {"song_id": "NOALBUM"}], True),
        ([_song("T1", "OTHER")], False),
    ])
    assert set(placed) == {"T1", "T2"}
    assert placed["T1"].release.album_id == "A1" and placed["T1"].named
    assert placed["T2"].release.isrcs == ("I1", "I2")
    assert spotify_fields(placed["T2"]) == {
        "album": "Album", "albumartist": "Artist", "year": 2019, "month": 9, "day": 17,
        "track": 3, "tracktotal": 10, "disc": 2, "disctotal": 2,
    }
    full = placements_from([([_song("T", disc_count=3, date="2019")], True)])["T"]
    assert spotify_fields(full)["disctotal"] == 3
    assert (spotify_fields(full)["month"], spotify_fields(full)["day"]) == (0, 0)


# ----------------------------------------------------------------------
# MusicBrainz release ladder
# ----------------------------------------------------------------------


RELEASE = {"id": "REL", "disambiguation": "", "release-group": {"id": "RG", "disambiguation": ""},
           "artist-credit": [{"artist": {"id": "ART"}}]}


class FakeMB:
    def __init__(self, url=None, recordings=None, barcode=None):
        self.url, self.recordings, self.barcode = url, recordings or {}, barcode
        self.calls = 0
        self.paths = []

    def get(self, path, **params):
        self.calls += 1
        self.paths.append(path)
        if path == "url":
            return {"relations": [{"target-type": "release", "release": {"id": self.url}}]} if self.url else None
        if path == "recording":
            code = params["query"].split(":", 1)[1]
            return {"recordings": [{"releases": self.recordings.get(code, [])}]}
        if path == "release":
            return {"releases": self.barcode or []}
        if path.startswith("release/"):
            return {**RELEASE, "id": path.split("/", 1)[1]}
        raise AssertionError(path)


def _album(**kw):
    from music_scan.mb_release import SpotifyAlbum

    return SpotifyAlbum(**{"album_id": "SP", "name": "Album (Deluxe)", "tracks_count": 2, "isrcs": ("I1", "I2"), **kw})


def _resolver(mb, tmp_path, upc=None, cached=True, **kw):
    from music_scan.mb_release import Resolver

    return Resolver(cache_file=tmp_path / "mb.json" if cached else None, mb=mb, upc_of=lambda _: upc, **kw)


def test_url_rung_wins(tmp_path):
    r = _resolver(FakeMB(url="URLREL"), tmp_path)
    fields = r.fields(_album())
    assert fields["mb_albumid"] == "URLREL" and fields["mb_albumartistids"] == ["ART"]
    assert r.cache["SP"]["rung"] == "url"


def test_isrc_rung_needs_every_isrc_track_count_and_title(tmp_path):
    rel = lambda i, title="Album", count=2: {"id": i, "title": title, "track-count": count, "status": "Official"}
    recordings = {
        "I1": [rel("CD"), rel("BOX", count=40), rel("OTHER", title="Best Of")],
        "I2": [rel("CD"), rel("BOX", count=40), {**rel("WEB"), "media": [{"format": "Digital Media"}]}],
    }
    r = _resolver(FakeMB(recordings=recordings), tmp_path)
    assert r.fields(_album())["mb_albumid"] == "CD"
    recordings["I1"].append({**rel("WEB"), "media": [{"format": "Digital Media"}]})
    r = _resolver(FakeMB(recordings=recordings), tmp_path, cached=False)
    assert r.fields(_album())["mb_albumid"] == "WEB"  # official digital over CD


def test_upc_rung_then_none_clears(tmp_path):
    hit = [{"id": "UPCREL", "barcode": "0012345678905", "title": "Album", "track-count": 2}]
    r = _resolver(FakeMB(barcode=hit), tmp_path, upc="12345678905", cached=False)
    assert r.fields(_album())["mb_albumid"] == "UPCREL" and r.cache["SP"]["rung"] == "upc"

    r = _resolver(FakeMB(), tmp_path, cached=False)
    fields = r.fields(_album())
    assert r.cache["SP"]["rung"] == "none"
    assert fields["mb_albumid"] == "" and fields["mb_albumartistids"] == []


def test_budget_leaves_albums_pending_and_misses_are_retried_after_a_week(tmp_path):
    mb = FakeMB()
    r = _resolver(mb, tmp_path, budget=1)
    assert r.fields(_album(album_id="ONE")) is not None
    assert r.fields(_album(album_id="TWO")) is None  # pending: budget spent
    r.save()

    calls = mb.calls
    r = _resolver(mb, tmp_path)
    assert r.fields(_album(album_id="ONE"))["mb_albumid"] == "" and mb.calls == calls  # cached miss
    r.cache["ONE"]["checked"] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    mb.url = "LATER"
    assert r.fields(_album(album_id="ONE"))["mb_albumid"] == "LATER"


def test_spotify_rate_limit_or_mb_error_leaves_the_album_pending(tmp_path):
    from music_fetch.spotify_limit import SpotifyRateLimited

    def limited(_):
        raise SpotifyRateLimited(datetime.now(timezone.utc), 999)

    from music_scan.mb_release import Resolver

    r = Resolver(cache_file=tmp_path / "mb.json", mb=FakeMB(), upc_of=limited)
    assert r.fields(_album()) is None and "SP" not in r.cache

    class Down(FakeMB):
        def get(self, path, **params):
            raise OSError("down")

    r = Resolver(cache_file=tmp_path / "mb.json", mb=Down(), upc_of=lambda _: None)
    assert r.fields(_album()) is None


# ----------------------------------------------------------------------
# Library: retag, move, cover, re-run
# ----------------------------------------------------------------------


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


def _m4a(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:a", "aac", str(path)], check=True)
    return path


def _jpeg(path: Path) -> bytes:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x64",
                    "-frames:v", "1", str(path)], check=True)
    return path.read_bytes()


def _add(lib, path, **fields):
    from beets.library import Item

    item = Item(path=str(path), artist="Artist", albumartist="Artist", **fields)
    lib._lib.add(item)
    item.write()
    return item


def _editions(tmp_path, lib):
    """Two tracks of one Spotify album from two editions, plus one with no Spotify ID."""
    jp = _add(lib, _m4a(tmp_path / "in" / "jp.m4a"), title="One", album="Album (Japan Edition)", track=7,
              tracktotal=14, year=2020, spotify_ids="T1", via="usenet", mb_albumid="JPREL", mb_trackid="REC1")
    std = _add(lib, _m4a(tmp_path / "in" / "std.m4a"), title="Two", album="Album", track=2, tracktotal=10,
               year=2019, spotify_ids="SINGLE2,T2", via="spotdl")
    loose = _add(lib, _m4a(tmp_path / "in" / "loose.m4a"), title="Loose", album="Whatever")
    songs = [_song("T1", track=1, isrc="I1"), _song("T2", track=2, isrc="I2"),
             _song("SINGLE2", "SINGLE", name="Two", album_type="single", date="2018-01-01", tracks=1)]
    return jp, std, loose, songs


def test_editions_merge_into_one_spotify_album_and_rerun_is_zero(tmp_path, ffmpeg, lib):
    from mediafile import MediaFile

    from music_scan.canon import canonicalize, placements_from
    from music_scan.mb_release import Resolver

    jp, std, loose, songs = _editions(tmp_path, lib)
    placements = placements_from([(songs, True)])
    resolver = Resolver(cache_file=tmp_path / "mb.json", mb=FakeMB(url="SPOTREL"), upc_of=lambda _: None)
    art = _jpeg(tmp_path / "c.jpg")

    dry = canonicalize(lib.all_items(), placements, resolver, apply=False, fetch=lambda u: art)
    assert len(dry.changes) == 2 and dry.moved == 2 and dry.noalbum == 0
    assert lib.get_item(jp.id).album == "Album (Japan Edition)" and Path(os.fsdecode(lib.get_item(jp.id).path)).exists()

    plan = canonicalize(lib.all_items(), placements, resolver, fetch=lambda u: art)
    assert len(plan.changes) == 2
    for item_id, track in ((jp.id, 1), (std.id, 2)):
        item = lib.get_item(item_id)
        path = Path(os.fsdecode(item.path))
        assert (item.album, item.albumartist, item.year, item.month, item.day) == ("Album", "Artist", 2019, 9, 17)
        assert (item.track, item.tracktotal, item.disc, item.disctotal) == (track, 10, 1, 1)
        assert path.parent == tmp_path / "library" / "Artist" / "Album" and path.exists()
        assert item.mb_albumid == "SPOTREL" and item.mb_albumartistid == "ART"
        assert item.get("spotify_album_id") == "A1" and item.get("mb_album_via") == "url"
        mf = MediaFile(str(path))
        assert mf.album == "Album" and mf.mb_albumid == "SPOTREL" and mf.images[0].data == art
    assert lib.get_item(jp.id).mb_trackid == "REC1"  # track-level IDs stay
    assert lib.get_item(loose.id).album == "Whatever"
    assert not (tmp_path / "in" / "jp.m4a").exists()

    again = canonicalize(lib.all_items(), placements, resolver, fetch=lambda u: art)
    assert again.changes == [] and again.unchanged == 2


def test_pending_musicbrainz_leaves_mb_tags_until_a_later_scan(tmp_path, ffmpeg, lib, monkeypatch):
    from music_scan import canon
    from music_scan.mb_release import Resolver

    jp, _, _, songs = _editions(tmp_path, lib)
    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    (spotdl_dir / "later.spotdl").write_text(json.dumps({"songs": songs}), encoding="utf-8")
    from music_fetch import ingest

    monkeypatch.setattr(ingest, "SPOTDL_DIR", spotdl_dir)
    from music_scan import cover

    art = _jpeg(tmp_path / "c.jpg")
    monkeypatch.setattr(cover, "download", lambda url: art)
    mb = FakeMB(url="LATE")
    monkeypatch.setattr(canon, "Resolver", lambda budget=None: Resolver(
        cache_file=tmp_path / "mb.json", mb=mb, upc_of=lambda _: None, budget=0))

    canon.after_scan(lib, since=0)
    item = lib.get_item(jp.id)
    assert item.album == "Album" and item.get("mb_album_via") == "pending" and item.mb_albumid == "JPREL"

    monkeypatch.setattr(canon, "Resolver", lambda budget=None: Resolver(
        cache_file=tmp_path / "mb.json", mb=mb, upc_of=lambda _: None))
    canon.after_scan(lib, since=10**12)  # nothing new: the pending item is picked up from the queue
    item = lib.get_item(jp.id)
    assert item.get("mb_album_via") == "url" and item.mb_albumid == "LATE"


def test_backfill_dry_run_apply_then_zero(tmp_path, ffmpeg, lib, monkeypatch):
    from music_fetch import ingest
    from music_scan import canon, cover, library, navidrome
    from music_scan.mb_release import Resolver

    _, _, _, songs = _editions(tmp_path, lib)
    monkeypatch.setattr(library, "LIBRARY_DB", tmp_path / "library.db")
    monkeypatch.setattr(library, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(ingest, "SPOTDL_DIR", tmp_path / "nospotdl")
    monkeypatch.setattr(canon, "read_pages", lambda refresh=False: {"later": songs})
    monkeypatch.setattr(canon, "Resolver", lambda budget=None: Resolver(
        cache_file=tmp_path / "mb.json", mb=FakeMB(), upc_of=lambda _: None))
    art = _jpeg(tmp_path / "c.jpg")
    monkeypatch.setattr(cover, "download", lambda url: art)
    scans = []
    monkeypatch.setattr(navidrome, "trigger_scan", lambda: scans.append(1))

    assert len(canon.run(apply=False).changes) == 2 and scans == []
    assert len(canon.run(apply=True).changes) == 2 and scans == [1]
    assert canon.run(apply=True).changes == []


def test_failed_cover_download_is_retried_next_run(tmp_path, ffmpeg, lib):
    from mediafile import MediaFile

    from music_scan.canon import canonicalize, placements_from

    jp, _, _, songs = _editions(tmp_path, lib)
    placements = placements_from([(songs, True)])

    def down(url):
        raise OSError("cdn down")

    canonicalize(lib.all_items(), placements, None, fetch=down)
    item = lib.get_item(jp.id)
    assert item.album == "Album" and not item.get("spotify_album_id")

    art = _jpeg(tmp_path / "c.jpg")
    plan = canonicalize(lib.all_items(), placements, None, fetch=lambda u: art)
    assert {c.item.id for c in plan.changes if c.art} >= {jp.id}
    assert MediaFile(os.fsdecode(lib.get_item(jp.id).path)).images[0].data == art
