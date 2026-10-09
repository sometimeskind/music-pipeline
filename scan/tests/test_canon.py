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
        # url: the linked release, or {Spotify album ID: release} to link only some URLs (#229).
        self.url, self.recordings, self.barcode = url, recordings or {}, barcode
        self.calls = 0
        self.paths = []

    def get(self, path, **params):
        self.calls += 1
        self.paths.append(path)
        if path == "url":
            rel = self.url.get(params["resource"].rsplit("/", 1)[1]) if isinstance(self.url, dict) else self.url
            return {"relations": [{"target-type": "release", "release": {"id": rel}}]} if rel else None
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


def test_url_rung_tries_the_twins_urls_and_a_miss_is_retried_when_a_twin_joins(tmp_path, caplog):
    import logging

    # Against All Logic *2012 - 2017* (#229): MusicBrainz links the other twin, not the representative.
    mb = FakeMB(url={"TWIN": "TWINREL"})
    r = _resolver(mb, tmp_path)
    assert r.fields(_album())["mb_albumid"] == "" and r.cache["SP"]["rung"] == "none"  # alone: a miss
    r.save()
    r = _resolver(mb, tmp_path)
    with caplog.at_level(logging.INFO):
        fields = r.fields(_album(twins=("TWIN",)))  # looked up again now, not after a week
    assert fields["mb_albumid"] == "TWINREL" and r.cache["SP"]["rung"] == "url"
    assert r.cache["SP"]["twins"] == ["TWIN"] and mb.paths[3:] == ["url", "url", "release/TWINREL"]
    assert "[MB-URL] SP: MusicBrainz links its twin release TWIN" in caplog.text
    # A miss that already tried these twins stays a miss until the usual retry.
    mb = FakeMB()
    r = _resolver(mb, tmp_path, cached=False)
    assert r.fields(_album(twins=("TWIN",)))["mb_albumid"] == "" and r.cache["SP"]["twins"] == ["TWIN"]
    calls = mb.calls
    assert r.fields(_album(twins=("TWIN",)))["mb_albumid"] == "" and mb.calls == calls


def test_isrc_rung_needs_every_isrc_track_count_and_title(tmp_path):
    def rel(i, title="Album", count=2):
        return {"id": i, "title": title, "track-count": count, "status": "Official"}

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


def test_musicbrainz_503_is_retried_once(monkeypatch):
    import io
    import urllib.error
    import urllib.request

    from music_scan.mb_release import MusicBrainz

    answers = []

    def urlopen(req, timeout):
        code = answers.pop(0)
        if code != 200:
            raise urllib.error.HTTPError(req.full_url, code, "busy", {}, None)
        return io.BytesIO(b'{"ok": true}')

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    mb = MusicBrainz(interval=0, retry_after=0)
    answers[:] = [503, 200]
    assert mb.get("url") == {"ok": True} and mb.calls == 2
    answers[:] = [503, 503, 200]
    with pytest.raises(urllib.error.HTTPError):
        mb.get("url")
    assert answers == [200]  # one retry, then the album stays pending
    answers[:] = [500, 200]
    with pytest.raises(urllib.error.HTTPError):
        mb.get("url")


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

    item = Item(path=str(path), **{"artist": "Artist", "albumartist": "Artist", **fields})
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


def test_backfill_dry_run_apply_then_zero(tmp_path, ffmpeg, lib, monkeypatch, caplog):
    import logging

    caplog.set_level(logging.INFO)
    from music_fetch import ingest
    from music_scan import canon, cover, library, navidrome, scan
    from music_scan.mb_release import Resolver

    from music_fetch import albums as albums_state

    _, _, _, songs = _editions(tmp_path, lib)
    monkeypatch.setattr(albums_state, "STATE_FILE", tmp_path / "no-albums.json")
    monkeypatch.setattr(library, "LIBRARY_DB", tmp_path / "library.db")
    monkeypatch.setattr(library, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(ingest, "SPOTDL_DIR", tmp_path / "nospotdl")
    monkeypatch.setattr(canon, "read_pages", lambda refresh=False: {"later": songs})
    monkeypatch.setattr(canon, "Resolver", lambda budget=None: Resolver(
        cache_file=tmp_path / "mb.json", mb=FakeMB(), upc_of=lambda _: None))
    art = _jpeg(tmp_path / "c.jpg")
    monkeypatch.setattr(cover, "download", lambda url: art)
    events = []
    monkeypatch.setattr(navidrome, "trigger_scan", lambda: events.append("rescan"))
    monkeypatch.setattr(scan, "regen_playlists", lambda: events.append("m3u") or {"later": 54, "keep": 565})

    assert len(canon.run(apply=False).changes) == 2 and events == []
    plan = canon.run(apply=True)
    assert len(plan.changes) == 2 and plan.moved
    # The moved files' .m3u entries are rewritten before Navidrome rescans (#218), and said so (#223).
    assert events == ["m3u", "rescan"]
    assert f"Moved {plan.moved} file(s); playlists regenerated: keep 565, later 54" in caplog.text
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
    assert item.album == "Album (Japan Edition)" and not item.get("spotify_album_id")  # nothing written

    art = _jpeg(tmp_path / "c.jpg")
    plan = canonicalize(lib.all_items(), placements, None, fetch=lambda u: art)
    assert {c.item.id for c in plan.changes if c.art} >= {jp.id}
    assert MediaFile(os.fsdecode(lib.get_item(jp.id).path)).images[0].data == art


def _with_art(item, data):
    from music_scan import cover

    cover.embed(item, data)
    return item


def test_cover_follows_only_a_renamed_album_or_missing_art(tmp_path, ffmpeg, lib):
    from mediafile import MediaFile

    from music_scan.canon import canonicalize, placements_from

    jp, std, _, songs = _editions(tmp_path, lib)
    old = _jpeg(tmp_path / "old.jpg")
    _with_art(jp, old)
    _with_art(std, old)
    new = (tmp_path / "new.jpg")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=64x64",
                    "-frames:v", "1", str(new)], check=True)

    plan = canonicalize(lib.all_items(), placements_from([(songs, True)]), None, fetch=lambda u: new.read_bytes())
    art = {c.item.id: c.art for c in plan.changes}
    assert art == {jp.id: True, std.id: False}  # jp is renamed; std keeps its name and its art
    assert MediaFile(os.fsdecode(lib.get_item(jp.id).path)).images[0].data == new.read_bytes()
    assert MediaFile(os.fsdecode(lib.get_item(std.id).path)).images[0].data == old


def test_beets_only_changes_skip_the_file_write(tmp_path, ffmpeg, lib, caplog):
    from music_scan.canon import canonicalize, placements_from, spotify_fields

    songs = [_song("T1", track=1)]
    placements = placements_from([(songs, True)])
    path = _m4a(tmp_path / "library" / "Artist" / "Album" / "01 - One.m4a")
    fields = {k: v for k, v in spotify_fields(placements["T1"]).items() if k != "albumartist"}
    item = _add(lib, path, title="One", spotify_ids="T1", **fields)
    _with_art(item, _jpeg(tmp_path / "c.jpg"))
    os.utime(path, (0, 0))

    plan = canonicalize(lib.all_items(), placements, None, apply=False)
    assert [c.write for c in plan.changes] == [False] and plan.writes == 0
    assert dict(plan.kinds) == {"beets-only": 1}
    canonicalize(lib.all_items(), placements, None, fetch=lambda u: pytest.fail("no cover is due"))
    stored = lib.get_item(item.id)
    assert stored.get("mb_album_via") == "pending" and stored.get("spotify_album_id") == "A1"
    assert os.stat(path).st_mtime == 0  # the file was not rewritten


def test_report_counts_every_name_an_album_merges(tmp_path, ffmpeg, lib, caplog):
    import logging

    from music_scan.canon import canonicalize, placements_from, report

    _, _, _, songs = _editions(tmp_path, lib)
    art = _jpeg(tmp_path / "c.jpg")
    plan = canonicalize(lib.all_items(), placements_from([(songs, True)]), None, fetch=lambda u: art)
    with caplog.at_level(logging.INFO):
        report(plan, None)
    # "Album" (std, unrenamed) and "Album (Japan Edition)" (jp) become one album, counted after the apply too.
    assert "Albums: 2 current album name(s) become 1 Spotify album(s)" in caplog.text
    assert "2 current album name(s): Album; Album (Japan Edition)" in caplog.text
    assert "2 file write(s), 0 database-only" in caplog.text


# ----------------------------------------------------------------------
# Single → album by ISRC
# ----------------------------------------------------------------------


def _hit(album_id, name, album_type="album", artist="Artist", date="2020-01-01", track=4, isrc="ISRC1", tracks=12):
    return {"album": {"id": album_id, "name": name, "album_type": album_type, "artists": [{"name": artist}],
                      "release_date": date, "total_tracks": tracks, "images": [{"url": COVER, "width": 640}]},
            "track_number": track, "disc_number": 1, "external_ids": {"isrc": isrc}}


def _single_songs():
    return [_song("SGL", "SINGLE", name="Munch", album_type="single", date="2022-08-01", tracks=1, isrc="ISRC1")]


def _finder(tmp_path, hits, **kw):
    from music_scan.canon import AlbumsByIsrc

    searched = []

    def search(isrc):
        searched.append(isrc)
        return hits

    return AlbumsByIsrc(cache_file=tmp_path / "isrc.json", search=search, interval=0, **kw), searched


def test_single_moves_to_the_same_artists_album_and_keeps_the_current_one(tmp_path):
    from music_scan.canon import placements_from

    placements = placements_from([(_single_songs(), True)])
    p = placements["SGL"]
    hits = [_hit("COMP", "Hits 2023", album_type="compilation", date="2019-01-01"),
            _hit("OTHER", "Covers", artist="Someone Else", date="2018-01-01"),
            _hit("STD", "Like..?", date="2023-01-20", track=4),
            _hit("DLX", "Like..? (Deluxe)", date="2023-07-21", track=6),
            _hit("WRONG", "Remix", isrc="ISRC9")]
    albums, searched = _finder(tmp_path, hits)
    assert albums.album_for(FakeItem(album="Munch"), p, placements).release.album_id == "STD"  # earliest
    deluxe = albums.album_for(FakeItem(album="Like..? (Deluxe)"), p, placements)
    assert (deluxe.release.album_id, deluxe.track, deluxe.release.artist) == ("DLX", 6, "Artist")
    assert searched == ["ISRC1"]  # cached after the first search
    albums.save()

    again, searched = _finder(tmp_path, [])
    assert again.album_for(FakeItem(album="Munch"), p, placements).release.album_id == "STD" and searched == []


def test_single_with_no_album_stays_and_a_miss_is_retried_after_a_week(tmp_path):
    from music_scan.canon import placements_from

    placements = placements_from([(_single_songs(), True)])
    albums, searched = _finder(tmp_path, [_hit("COMP", "Hits", album_type="compilation")])
    assert albums.album_for(FakeItem(), placements["SGL"], placements) is None
    albums.cache["ISRC1"]["checked"] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    albums.search = lambda isrc: [_hit("STD", "Heavy Metal")]
    assert albums.album_for(FakeItem(), placements["SGL"], placements).release.name == "Heavy Metal"


def test_budget_or_rate_limit_leaves_the_single_waiting(tmp_path):
    from music_fetch.spotify_limit import SpotifyRateLimited
    from music_scan.canon import placements_from

    placements = placements_from([(_single_songs(), True)])
    albums, searched = _finder(tmp_path, [], budget=0)
    assert albums.album_for(FakeItem(), placements["SGL"], placements) is False and searched == []

    def limited(_):
        raise SpotifyRateLimited(datetime.now(timezone.utc), 999)

    albums, _ = _finder(tmp_path, [])
    albums.search = limited
    assert albums.album_for(FakeItem(), placements["SGL"], placements) is False and albums.limited
    assert albums.calls == 1 and albums.tracks("ISRC2") is None and albums.calls == 1  # no more calls


def test_single_track_is_retagged_to_its_album_and_waits_when_unsearched(tmp_path, ffmpeg, lib, caplog):
    import logging

    from music_scan.canon import WAIT, canonicalize, placements_from, report

    item = _add(lib, _m4a(tmp_path / "in" / "munch.m4a"), title="Munch", album="Munch", spotify_ids="SGL")
    placements = placements_from([(_single_songs(), True)])
    art = _jpeg(tmp_path / "c.jpg")

    waiting, _ = _finder(tmp_path, [], budget=0)
    plan = canonicalize(lib.all_items(), placements, None, fetch=lambda u: art, albums=waiting)
    stored = lib.get_item(item.id)
    assert plan.waiting == 1 and stored.album == "Munch" and stored.get(WAIT) == "1"

    albums, _ = _finder(tmp_path, [_hit("STD", "Heavy Metal", track=4)])
    plan = canonicalize(lib.all_items(), placements, None, fetch=lambda u: art, albums=albums)
    stored = lib.get_item(item.id)
    assert plan.singles == 1
    assert (stored.album, stored.track, stored.get("spotify_album_id")) == ("Heavy Metal", 4, "STD")
    assert WAIT not in stored
    with caplog.at_level(logging.INFO):
        report(plan, None, albums)
    assert "Singles: 1 item(s) on their album instead of a single, by ISRC; Spotify: 1 ISRC search(es)" in caplog.text


def test_an_ep_counts_only_when_the_item_is_filed_under_it_and_an_album_still_wins(tmp_path):
    from music_scan.canon import placements_from

    placements = placements_from([(_single_songs(), True)])
    p = placements["SGL"]
    # Spotify files EPs as "single": Like..? and its deluxe are EPs; the 1-track single is Munch itself.
    eps = [_hit("SINGLE", "Munch", album_type="single", tracks=1, track=1),
           _hit("REMIX", "Munch (Remixes)", album_type="single", tracks=3, date="2022-09-01"),
           _hit("EP", "Like..?", album_type="single", tracks=6, date="2023-01-20"),
           _hit("DLX", "Like..? (Deluxe)", album_type="single", tracks=11, date="2023-07-21", track=10)]
    albums, _ = _finder(tmp_path, eps)
    assert albums.album_for(FakeItem(album="Like..? (Deluxe)"), p, placements).release.album_id == "DLX"
    assert albums.album_for(FakeItem(album="Munch"), p, placements) is None  # no move onto an EP

    (tmp_path / "x").mkdir()
    albums, _ = _finder(tmp_path / "x", eps + [_hit("LP", "Y2K!", date="2024-01-01")])
    assert albums.album_for(FakeItem(album="Like..? (Deluxe)"), p, placements).release.album_id == "LP"


def test_an_older_cache_entry_is_searched_again(tmp_path):
    from music_scan.canon import placements_from

    placements = placements_from([(_single_songs(), True)])
    (tmp_path / "isrc.json").write_text(json.dumps({"ISRC1": {"checked": datetime.now(timezone.utc).isoformat(),
                                                              "tracks": []}}), encoding="utf-8")
    albums, searched = _finder(tmp_path, [_hit("EP", "Like..?", album_type="single", tracks=6)])
    assert albums.album_for(FakeItem(album="Like..?"), placements["SGL"], placements).release.album_id == "EP"
    assert searched == ["ISRC1"]


def test_a_move_onto_another_items_file_is_left_alone(tmp_path, ffmpeg, lib):
    from music_scan.canon import canonicalize, placements_from, spotify_fields

    songs = [_song("A", track=1), _song("B", track=1)]  # two Spotify IDs for one track slot
    placements = placements_from([(songs, True)])
    fields = {k: v for k, v in spotify_fields(placements["A"]).items() if k != "albumartist"}
    there = _add(lib, _m4a(tmp_path / "library" / "Artist" / "Album" / "01 - One.m4a"), title="One",
                 spotify_ids="A", **fields)
    mover = _add(lib, _m4a(tmp_path / "in" / "one.m4a"), title="One", album="One", spotify_ids="B")
    art = _jpeg(tmp_path / "c.jpg")

    plan = canonicalize(lib.all_items(), placements, None, fetch=lambda u: art)
    assert plan.clashes == 1 and mover.id not in {c.item.id for c in plan.changes}
    stored = lib.get_item(mover.id)
    assert stored.album == "One" and Path(os.fsdecode(stored.path)) == tmp_path / "in" / "one.m4a"
    assert Path(os.fsdecode(lib.get_item(there.id).path)).exists()


# ----------------------------------------------------------------------
# Twin releases (#214)
# ----------------------------------------------------------------------


def _twin_songs():
    """One album listed twice on Spotify (A, B), a deluxe edition and a self-titled compilation."""
    return [
        _song("A1", "A", name="Untrue", track=1, isrc="I1"),
        _song("A2", "A", name="Untrue", track=2, isrc="I2"),
        _song("B3", "B", name="untrue ", date="2021-05-05", tracks=11, track=3, isrc="I3"),
        _song("D1", "DLX", name="Untrue (Deluxe)", tracks=14, isrc="I1"),
        _song("C1", "COMP", name="Untrue", album_type="compilation", isrc="I1"),
        _song("C2", "COMP2", name="Untrue", album_type="compilation", isrc="I2"),
    ]


def test_twins_group_by_normalised_name_and_the_busiest_then_earliest_then_biggest_wins():
    from music_scan.canon import placements_from, twin_groups

    groups = twin_groups(placements_from([(_twin_songs(), True)]))
    assert set(groups) == {"A", "B"}  # a different name, a compilation: no group
    g = groups["A"]
    assert g is groups["B"] and g.representative.album_id == "A" and [r.album_id for r in g.others] == ["B"]
    assert g.entries == {"A": 2, "B": 1} and set(g.representative.isrcs) == {"I1", "I2", "I3"}

    # Equal entries: the earliest; equal dates: the most tracks; then the ID.
    songs = [_song("X1", "X", date="2020-01-01", tracks=10), _song("Y1", "Y", date="2019-01-01", tracks=10),
             _song("Z1", "Z", date="2019-01-01", tracks=12), _song("W1", "W", date="2019-01-01", tracks=12)]
    assert twin_groups(placements_from([(songs, True)]))["X"].representative.album_id == "W"
    # Entries an old snapshot knows don't count; EPs group, short singles don't.
    eps = [_song("E1", "E", name="EP", album_type="single", tracks=4), _song("E2", "F", name="EP",
                                                                            album_type="single", tracks=4)]
    singles = [_song("S1", "S", name="Sgl", album_type="single", tracks=1), _song("S2", "T", name="Sgl",
                                                                                  album_type="single", tracks=1)]
    assert set(twin_groups(placements_from([(eps + singles, False)]))) == {"E", "F"}


def test_twin_confirmed_by_an_entry_or_a_search_else_apart_or_waiting(tmp_path):
    from music_scan.canon import _on_representative, placements_from, twin_groups

    placements = placements_from([(_twin_songs(), True)])
    group = twin_groups(placements)["B"]
    p = placements["B3"]
    assert _on_representative(p, group, {}) is None  # no entry on A carries I3
    # An entry on the representative with the same ISRC: no search, its numbering, the group's ISRCs.
    with_a3 = placements_from([(_twin_songs() + [_song("A3", "A", name="Untrue", track=9, isrc="I3")], True)])
    on_a = _on_representative(p, group, {("A", "I3"): with_a3["A3"]})
    assert (on_a.release.album_id, on_a.track, on_a.isrc) == ("A", 9, "I3")
    assert set(on_a.release.isrcs) == {"I1", "I2", "I3"}

    hits = [_hit("B", "Untrue", track=3, isrc="I3"), _hit("A", "Untrue", track=4, isrc="I3")]
    albums, searched = _finder(tmp_path, hits)
    twin = albums.twin_for(p, group)
    assert (twin.release.album_id, twin.track, twin.named) == ("A", 4, True) and searched == ["I3"]
    (tmp_path / "apart").mkdir()
    albums, _ = _finder(tmp_path / "apart", [_hit("B", "Untrue", track=3, isrc="I3")])
    assert albums.twin_for(p, group) is None  # the recording is only on B: a self-titled record, not a twin
    albums, _ = _finder(tmp_path / "apart", [], budget=0)
    assert albums.twin_for(p, group) is False


def test_twin_items_take_the_representatives_id_date_and_numbering(tmp_path, ffmpeg, lib, caplog):
    import logging

    from music_scan.canon import WAIT, canonicalize, placements_from, report

    placements = placements_from([(_twin_songs(), True)])
    on_a = _add(lib, _m4a(tmp_path / "in" / "a.m4a"), title="One", album="Untrue", track=1, spotify_ids="A1")
    on_b = _add(lib, _m4a(tmp_path / "in" / "b.m4a"), title="Three", album="Untrue", track=3, spotify_ids="B3")
    art = _jpeg(tmp_path / "c.jpg")

    waiting, _ = _finder(tmp_path, [], budget=0)
    plan = canonicalize(lib.all_items(), placements, None, fetch=lambda u: art, albums=waiting)
    assert (plan.waiting, plan.twin_waiting) == (1, 1) and lib.get_item(on_b.id).get(WAIT) == "1"
    assert lib.get_item(on_a.id).get("spotify_album_id") == "A"

    albums, _ = _finder(tmp_path, [_hit("A", "Untrue", track=4, isrc="I3")])
    resolver = _resolver(FakeMB(url={"B": "BREL"}), tmp_path)  # only the twin's URL is linked (#229)
    with caplog.at_level(logging.INFO):
        plan = canonicalize(lib.all_items(), placements, resolver, fetch=lambda u: art, albums=albums)
    assert "[RETAG]" in caplog.text and "spotify_album_id None→'A'" in caplog.text  # the line shows it (#230)
    caplog.clear()
    b = lib.get_item(on_b.id)
    assert (b.get("spotify_album_id"), b.track, b.tracktotal, b.year, b.month) == ("A", 4, 10, 2019, 9)
    assert WAIT not in b and plan.twinned == 1 and plan.on_twin == {"A": 1, "B": 1}
    # One lookup for the group, keyed by the representative, through the twin's URL.
    assert list(resolver.cache) == ["A"] and resolver.cache["A"]["twins"] == ["B"]
    assert lib.get_item(on_a.id).get("mb_albumid") == lib.get_item(on_b.id).get("mb_albumid") == "BREL"
    assert plan.names == {"A": {("Artist", "Untrue")}}
    with caplog.at_level(logging.INFO):
        report(plan, None, albums)
    assert ("[TWIN] Artist — Untrue: A (2019-09-17, 10 tracks, 2 entries, 1 items) ← "
            "B (2021-05-05, 11 tracks, 1 entries, 1 items)") in caplog.text
    assert "Twins: 1 group(s) of same-name releases; 1 item(s) moved onto the representative, 0 wait" in caplog.text
    assert not canonicalize(lib.all_items(), placements, resolver, fetch=lambda u: art, albums=albums).changes


# ----------------------------------------------------------------------
# Items given a Spotify ID after their import (#244)
# ----------------------------------------------------------------------


def test_after_scan_takes_an_item_linked_after_its_import(tmp_path, ffmpeg, lib, monkeypatch):
    """A music-rollback-releases [LINK] gives an old item a Spotify ID: the next scan canonicalises it."""
    from music_fetch import ingest
    from music_scan import canon, cover
    from music_scan.mb_release import Resolver

    linked = _add(lib, _m4a(tmp_path / "in" / "Drake feat. X" / "Take Care" / "proud.m4a"), title="Make Me Proud",
                  album="Take Care", via="usenet", spotify_ids="T1")
    linked.added = 0
    linked.store()
    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir()
    (spotdl_dir / "p.spotdl").write_text(json.dumps({"songs": [_song("T1", name="Take Care (Deluxe)")]}))
    monkeypatch.setattr(ingest, "SPOTDL_DIR", spotdl_dir)
    monkeypatch.setattr(cover, "download", lambda url: _jpeg(tmp_path / "c.jpg"))
    monkeypatch.setattr(canon, "Resolver", lambda budget=None: Resolver(
        cache_file=tmp_path / "mb.json", mb=FakeMB(url="REL"), upc_of=lambda _: None))

    assert canon.after_scan(lib, since=10**12) == 1
    item = lib.get_item(linked.id)
    assert (item.album, item.get("spotify_album_id"), item.mb_albumid) == ("Take Care (Deluxe)", "A1", "REL")
    assert canon.after_scan(lib, since=10**12) == 0  # canonical now: no longer picked up


# ----------------------------------------------------------------------
# A successful release's extras (#245)
# ----------------------------------------------------------------------


GRAB = datetime(2026, 10, 8, 22, 0, tzinfo=timezone.utc)
IMPORTED = datetime(2026, 10, 8, 22, 12, tzinfo=timezone.utc)


def _set_added(lib, item, when: datetime):
    item.added = when.timestamp()
    item.store()
    return item


def _deluxe(tmp_path, lib):
    """LCD's This Is Happening: two canonical tracks and the deluxe release's three extras."""
    art = _jpeg(tmp_path / "c.jpg")
    canonical = {"album": "This Is Happening", "albumartist": "LCD Soundsystem", "year": 2010, "month": 5, "day": 17,
                 "tracktotal": 9, "disctotal": 1, "spotify_album_id": "SPALB", "mb_album_via": "url",
                 "mb_albumid": "REL", "mb_releasegroupid": "RG", "mb_albumartistid": "ART"}
    lib_dir = tmp_path / "library" / "LCD Soundsystem" / "This Is Happening"
    tracks = []
    for n, sid in ((1, "S1"), (2, "S2")):
        t = _add(lib, _m4a(lib_dir / f"{n:02d} - T{n}.m4a"), title=f"T{n}", track=n, via="usenet", spotify_ids=sid,
                 **canonical)
        _with_art(_set_added(lib, t, GRAB + timedelta(minutes=5)), art)
        tracks.append(t)
    release = tmp_path / "library" / "LCD Soundsystem" / "This Is Happening (Deluxe Edition)"
    extras = [_set_added(lib, _add(lib, _m4a(release / f"{n:02d} - {title}.m4a"), title=title, track=n,
                                   album="This Is Happening (Deluxe Edition)", albumartist="LCD Soundsystem",
                                   tracktotal=15, year=2010, via="usenet", mb_albumid="DELUXE"),
                         GRAB + timedelta(minutes=5))
              for n, title in ((2, "Oh You (Christmas Blues)"), (10, "All I Want (London Session)"))]
    failed = _set_added(lib, _add(lib, _m4a(release / "11 - Outtake.m4a"), title="Outtake", track=11,
                                  album="This Is Happening (Deluxe Edition)", via="usenet"),
                        GRAB - timedelta(days=1))
    records = {"lcd": {"status": "imported", "name": "This Is Happening", "artist": "LCD Soundsystem",
                       "grabbed_at": GRAB.isoformat(), "imported_at": IMPORTED.isoformat(),
                       "playlists": {"p": [["T1", "LCD Soundsystem", "S1"], ["T2", "LCD Soundsystem", "S2"]]}}}
    return tracks, extras, failed, records, art


def test_extras_take_the_albums_tags_cover_and_folder_and_stay_local_only(tmp_path, ffmpeg, lib, caplog):
    import logging

    from mediafile import MediaFile

    from music_scan.canon import adopt_extras

    caplog.set_level(logging.INFO)
    _, (christmas, london), failed, records, art = _deluxe(tmp_path, lib)

    dry = adopt_extras(lib.all_items(), records, apply=False)
    assert {e.item.id for e in dry} == {christmas.id, london.id}
    assert lib.get_item(christmas.id).album == "This Is Happening (Deluxe Edition)"
    assert "[EXTRA]" in caplog.text and "(dry run)" in caplog.text

    adopt_extras(lib.all_items(), records)
    album_dir = tmp_path / "library" / "LCD Soundsystem" / "This Is Happening"
    for item_id, track in ((christmas.id, 3), (london.id, 10)):  # track 2 is the album's: next free is 3
        item = lib.get_item(item_id)
        path = Path(os.fsdecode(item.path))
        assert (item.album, item.albumartist, item.year, item.month, item.day) == (
            "This Is Happening", "LCD Soundsystem", 2010, 5, 17)
        assert (item.track, item.tracktotal) == (track, 9)
        assert item.mb_albumid == "REL" and item.get("spotify_album_id") == "SPALB"
        assert not item.get("spotify_ids")  # local-only
        assert path.parent == album_dir and path.exists()
        assert MediaFile(str(path)).images[0].data == art
    assert lib.get_item(failed.id).album == "This Is Happening (Deluxe Edition)"  # not this release's extra

    assert adopt_extras(lib.all_items(), records) == []


def test_extras_wait_until_the_album_is_canonical(tmp_path, ffmpeg, lib, caplog):
    import logging

    from music_scan.canon import adopt_extras

    caplog.set_level(logging.INFO)
    tracks, _, _, records, _ = _deluxe(tmp_path, lib)
    for t in tracks:
        del t["spotify_album_id"]
        t.store()
    assert adopt_extras(lib.all_items(), records) == []
    assert "[NOREP] lcd: 2 extra(s) wait" in caplog.text


def test_backfill_adopts_extras_after_the_retags(tmp_path, ffmpeg, lib, monkeypatch):
    from music_fetch import albums as albums_state
    from music_fetch import ingest
    from music_scan import canon, library, navidrome, scan

    _, (christmas, _), _, records, _ = _deluxe(tmp_path, lib)
    state = tmp_path / ".albums.json"
    albums_state.State(albums=records).save(state)
    monkeypatch.setattr(albums_state, "STATE_FILE", state)
    monkeypatch.setattr(library, "LIBRARY_DB", tmp_path / "library.db")
    monkeypatch.setattr(library, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(ingest, "SPOTDL_DIR", tmp_path / "nospotdl")
    monkeypatch.setattr(canon, "read_pages", lambda refresh=False: {})
    monkeypatch.setattr(canon, "Resolver", lambda budget=None: None)
    events = []
    monkeypatch.setattr(navidrome, "trigger_scan", lambda: events.append("rescan"))
    monkeypatch.setattr(scan, "regen_playlists", lambda: events.append("m3u") or {})

    assert len(canon.run(apply=False).extras) == 2 and events == []
    assert len(canon.run(apply=True).extras) == 2 and events == ["m3u", "rescan"]
    assert lib.get_item(christmas.id).album == "This Is Happening"
    assert canon.run(apply=True).extras == []


# ----------------------------------------------------------------------
# Extras that are one of their album's tracks (#250)
# ----------------------------------------------------------------------


@pytest.mark.parametrize("title, entry", [
    ("Psycho Killer (live)", "Psycho Killer - Live"),
    ("Once in a Lifetime (live version)", "Once in a Lifetime - Live"),
    ("Genius Of Love (live)", "Genius of Love (Tom Tom Club) - Live"),
    ("The Great Curve", "The Great Curve - 2005 Remaster"),
    ("Thank You for Sending Me an Angel (Country Angel version)", None),
    ("Cities (live version)", None),
    ("Drunk Girls (London Session)", None),
    ("Psycho Killer (live)", None),  # against the studio entry only: a live take is a bonus track
])
def test_album_entry_by_title_words(title, entry):
    from music_scan.canon import album_entry
    from music_scan.identity import PlaylistTrack

    entries = [PlaylistTrack("Psycho Killer", "Talking Heads", "STUDIO"),
               PlaylistTrack("Thank You for Sending Me an Angel - Live", "Talking Heads", "ANGEL"),
               PlaylistTrack("Drunk Girls", "LCD Soundsystem", "DRUNK")]
    if entry is not None:
        entries.append(PlaylistTrack(entry, "Talking Heads", "MATCH"))
    found = album_entry(type("Item", (), {"title": title})(), entries)
    assert (found.song_id if found else None) == ("MATCH" if entry else None)


def _live(tmp_path, lib):
    """Stop Making Sense: Psycho Killer has an item, Genius of Love none; the 2023 release added three more."""
    canonical = {"album": "Stop Making Sense (Live)", "albumartist": "Talking Heads", "year": 1984,
                 "tracktotal": 16, "disctotal": 1, "spotify_album_id": "SMS", "mb_album_via": "url"}
    album_dir = tmp_path / "library" / "Talking Heads" / "Stop Making Sense (Live)"
    psycho = _add(lib, _m4a(album_dir / "01 - Psycho Killer.m4a"), title="Psycho Killer", artist="Talking Heads",
                  track=1, via="usenet", spotify_ids="PSYCHO", **canonical)
    _with_art(psycho, _jpeg(tmp_path / "c.jpg"))
    release = tmp_path / "library" / "Talking Heads" / "Stop Making Sense"
    added = GRAB + timedelta(minutes=5)
    extras = {title: _set_added(lib, _add(lib, _m4a(release / f"{n:02d} - {title}.m4a"), title=title, track=n,
                                         artist="Talking Heads", album="Stop Making Sense",
                                         albumartist="Talking Heads", via="usenet"), added)
              for n, title in ((1, "Psycho Killer (live)"), (15, "Genius Of Love (live)"), (6, "Cities (live version)"))}
    records = {"sms": {"status": "fallback", "fallback_from": "partial", "name": "Stop Making Sense (Live)",
                       "artist": "Talking Heads", "grabbed_at": GRAB.isoformat(), "imported_at": IMPORTED.isoformat(),
                       "playlists": {"keep": [["Psycho Killer - Live", "Talking Heads", "PSYCHO"],
                                              ["Genius of Love (Tom Tom Club) - Live", "Talking Heads", "GENIUS"]]}}}
    return psycho, extras, records


def test_an_album_track_extra_is_linked_or_left_as_a_duplicate(tmp_path, ffmpeg, lib, caplog):
    import logging

    from music_scan.canon import adopt_extras, link_extras

    caplog.set_level(logging.INFO)
    _, extras, records = _live(tmp_path, lib)
    genius, dupe, cities = (extras[t] for t in ("Genius Of Love (live)", "Psycho Killer (live)",
                                                "Cities (live version)"))

    links, dupes = link_extras(lib.all_items(), records, apply=False)
    assert [(i.id, e.song_id) for i, e in links] == [(genius.id, "GENIUS")]
    assert [(i.id, e.song_id) for i, e in dupes] == [(dupe.id, "PSYCHO")]
    assert not lib.get_item(genius.id).get("spotify_ids")  # dry run
    assert "[DUPE]" in caplog.text and "(dry run)" in caplog.text

    link_extras(lib.all_items(), records)
    assert lib.get_item(genius.id).get("spotify_ids") == "GENIUS"
    adopted = adopt_extras(lib.all_items(), records)
    assert [e.item.id for e in adopted] == [cities.id]  # the bonus track only
    assert lib.get_item(dupe.id).album == "Stop Making Sense"  # left as it is
    links, dupes = link_extras(lib.all_items(), records)
    assert links == [] and [i.id for i, _ in dupes] == [dupe.id]  # a rerun links nothing again


def test_import_hook_canonicalises_the_links_before_adopting(tmp_path, ffmpeg, lib, monkeypatch):
    from music_scan import canon

    _, extras, records = _live(tmp_path, lib)
    seen = []
    monkeypatch.setattr(canon, "canonicalize_items", lambda items, **kw: seen.extend(i.title for i in items))
    adopted, links = canon.extras_after_import(lib, "sms", records["sms"])
    assert seen == ["Genius Of Love (live)"]
    assert [i.title for i, _ in links] == ["Genius Of Love (live)"]
    assert [e.item.title for e in adopted] == ["Cities (live version)"]
