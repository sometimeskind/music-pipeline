"""Spotify cover on Usenet album tracks (#204).

Fixtures are generated with ffmpeg (skipped where it is missing; the dev image has it).
"""

import json
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

COVER_URL = "https://i.scdn.co/image/ab67616d0000b273cover"
# The homelab beets config's convert command (kubernetes/music-pipeline/configmaps.yaml).
CONVERT = "ffmpeg -i $source -y -vn -map_metadata 0 -c:a aac -b:a 256k -movflags +faststart $dest"


@pytest.fixture
def ffmpeg():
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")


def _jpeg(path: Path, colour: str = "red") -> bytes:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={colour}:s=64x64",
                    "-frames:v", "1", str(path)], check=True)
    return path.read_bytes()


def _flac_with_picture(path: Path, picture: bytes) -> Path:
    from mutagen.flac import FLAC, Picture

    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-c:a", "flac", str(path)], check=True)
    pic = Picture()
    pic.type, pic.mime, pic.data = 3, "image/jpeg", picture
    audio = FLAC(path)
    audio["title"], audio["artist"], audio["album"] = "One", "Artist", "Album"
    audio.add_picture(pic)
    audio.save()
    return path


def _convert(source: Path, dest: Path) -> Path:
    cmd = CONVERT.replace("$source", shlex.quote(str(source))).replace("$dest", shlex.quote(str(dest)))
    subprocess.run(shlex.split(cmd), check=True, capture_output=True)
    return dest


def _m4a(path: Path) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:a", "aac", str(path)], check=True)
    return path


def _images(path: Path) -> list:
    from mediafile import MediaFile

    return MediaFile(str(path)).images or []


def _item(lib, path: Path, *, via="usenet", spotify_ids="", album="Album", added=200.0):
    from beets.library import Item

    item = Item(path=str(path), title=path.stem, artist="Artist", albumartist="Artist", album=album,
                via=via, spotify_ids=spotify_ids, sources="later", added=added)
    lib._lib.add(item)
    return item


@pytest.fixture
def lib(tmp_path):
    from music_scan.library import MusicLibrary

    with MusicLibrary(tmp_path / "library.db", tmp_path) as library:
        yield library


def _spotdl(tmp_path: Path, songs: list[dict]) -> Path:
    spotdl_dir = tmp_path / "spotdl"
    spotdl_dir.mkdir(exist_ok=True)
    (spotdl_dir / "later.spotdl").write_text(json.dumps({"type": "sync", "songs": songs}), encoding="utf-8")
    return spotdl_dir


def test_flac_through_convert_loses_its_picture_and_gets_the_spotify_cover(tmp_path, ffmpeg, lib) -> None:
    from music_scan.cover import covers_by_id, embed_covers

    release_art = _jpeg(tmp_path / "release.jpg", "blue")
    spotify_art = _jpeg(tmp_path / "spotify.jpg", "red")
    flac = _flac_with_picture(tmp_path / "one.flac", release_art)
    assert _images(flac)
    m4a = _convert(flac, tmp_path / "one.m4a")
    assert _images(m4a) == []  # -vn drops the FLAC's picture: the cause of #204

    item = _item(lib, m4a, spotify_ids="T1")
    covers = covers_by_id(_spotdl(tmp_path, [{"song_id": "T1", "cover_url": COVER_URL}]))
    fetched = []
    assert embed_covers([item], covers, fetch=lambda url: fetched.append(url) or spotify_art) == 1

    assert fetched == [COVER_URL]
    images = _images(m4a)
    assert len(images) == 1 and images[0].data == spotify_art

    from mutagen.mp4 import MP4

    assert MP4(str(m4a)).tags["covr"]  # what Navidrome reads, as on spotdl's tracks


def test_items_with_art_or_not_from_usenet_are_left_alone(tmp_path, ffmpeg, lib) -> None:
    from music_scan.cover import embed_covers, embed

    has_art = _item(lib, _m4a(tmp_path / "a.m4a"), spotify_ids="T1")
    embed(has_art, _jpeg(tmp_path / "own.jpg", "blue"))
    spotdl = _item(lib, _m4a(tmp_path / "b.m4a"), via="spotdl", spotify_ids="T2")
    covers = {"T1": COVER_URL, "T2": COVER_URL}

    assert embed_covers([has_art, spotdl], covers, fetch=lambda url: pytest.fail("fetched")) == 0
    assert _images(tmp_path / "b.m4a") == []


def test_track_without_spotify_id_borrows_its_albums_cover(tmp_path, ffmpeg, lib) -> None:
    from music_scan.cover import embed_covers

    art = _jpeg(tmp_path / "c.jpg")
    tagged = _item(lib, _m4a(tmp_path / "a.m4a"), spotify_ids="T1")
    noid = _item(lib, _m4a(tmp_path / "b.m4a"))
    other = _item(lib, _m4a(tmp_path / "c.m4a"), album="Other Album")

    assert embed_covers([noid, tagged, other], {"T1": COVER_URL}, fetch=lambda url: art) == 2
    assert _images(tmp_path / "b.m4a")[0].data == art
    assert _images(tmp_path / "c.m4a") == []


def test_dry_run_writes_nothing(tmp_path, ffmpeg, lib) -> None:
    from music_scan.cover import embed_covers

    item = _item(lib, _m4a(tmp_path / "a.m4a"), spotify_ids="T1")
    assert embed_covers([item], {"T1": COVER_URL}, apply=False, fetch=lambda url: pytest.fail("fetched")) == 1
    assert _images(tmp_path / "a.m4a") == []


def test_failed_download_is_skipped_and_fetched_once(tmp_path, ffmpeg, lib) -> None:
    from music_scan.cover import embed_covers

    items = [_item(lib, _m4a(tmp_path / f"{n}.m4a"), spotify_ids=n) for n in ("T1", "T2")]
    calls = []

    def fail(url):
        calls.append(url)
        raise OSError("timed out")

    assert embed_covers(items, {"T1": COVER_URL, "T2": COVER_URL}, fetch=fail) == 0
    assert calls == [COVER_URL]


def test_covers_by_id_reads_song_ids_and_urls(tmp_path) -> None:
    from music_scan.cover import covers_by_id

    spotdl_dir = _spotdl(tmp_path, [
        {"song_id": "T1", "cover_url": COVER_URL},
        {"url": "https://open.spotify.com/track/T2", "cover_url": COVER_URL + "2"},
        {"song_id": "T3", "cover_url": None},
        {"song_id": "T4", "cover_url": "Missing thumbnails"},
    ])
    (spotdl_dir / "broken.spotdl").write_text("{", encoding="utf-8")
    assert covers_by_id(spotdl_dir) == {"T1": COVER_URL, "T2": COVER_URL + "2"}


def test_backfill_embeds_only_usenet_items_and_rescans(tmp_path, ffmpeg, monkeypatch) -> None:
    from music_fetch import ingest
    from music_scan import cover, library, navidrome
    from music_scan.library import MusicLibrary

    db = tmp_path / "library.db"
    with MusicLibrary(db, tmp_path) as lib:
        _item(lib, _m4a(tmp_path / "a.m4a"), spotify_ids="T1")
        _item(lib, _m4a(tmp_path / "b.m4a"), via="spotdl", spotify_ids="T1")
    monkeypatch.setattr(ingest, "SPOTDL_DIR", _spotdl(tmp_path, [{"song_id": "T1", "cover_url": COVER_URL}]))
    monkeypatch.setattr(library, "LIBRARY_DB", db)
    monkeypatch.setattr(library, "LIBRARY_DIR", tmp_path)
    art = _jpeg(tmp_path / "c.jpg")
    monkeypatch.setattr(cover, "download", lambda url: art)
    scans = []
    monkeypatch.setattr(navidrome, "trigger_scan", lambda: scans.append(1))

    assert cover.run(apply=False) == 1
    assert _images(tmp_path / "a.m4a") == [] and scans == []
    assert cover.run(apply=True) == 1
    assert _images(tmp_path / "a.m4a")[0].data == art and _images(tmp_path / "b.m4a") == []
    assert scans == [1]
    assert cover.run(apply=True) == 0  # safe to re-run
