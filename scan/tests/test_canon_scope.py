"""``music-canon-albums`` over a generated library (homelab#3025).

These tests pin what the whole-library run decides (its lines, in order, and
the library after its apply), before a change to how a run picks its items.

Every item is a copy of one short m4a.  MusicBrainz and Spotify's ISRC search
are fakes.  The run reads the album records from the music database, so these
tests skip without ``MUSIC_TEST_DATABASE_URL``.  Regenerate the golden file,
only for a change meant to alter the output, with ``CANON_GOLDEN_WRITE=1``.
"""

import logging
import os
import random
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

GOLDEN = Path(__file__).parent / "canon_run.golden"
GRAB = datetime(2026, 10, 10, 21, 0, tzinfo=timezone.utc)
COVER = "https://i.scdn.co/image/canonical"
ARTISTS = ["Arbor", "Basalt", "Cinder", "Dune"]
WORDS = ["Amber", "Birch", "Coral", "Delta", "Elm", "Fern", "Garnet", "Heron", "Iris", "Jade", "Kestrel", "Lichen"]
TITLES = ["Lantern", "Harbour", "Paper", "Static", "Undertow", "Glass", "Northern", "Tide", "Ash", "Vesper",
          "Orbit", "Hollow"]
RELEASE = {"disambiguation": "", "release-group": {"id": "RG", "disambiguation": ""},
           "artist-credit": [{"artist": {"id": "ART"}}]}


class FakeMB:
    """MusicBrainz: the URL rung links the albums in *linked*; no other rung finds anything."""

    def __init__(self, linked: set[str]):
        self.linked, self.calls = linked, 0

    def get(self, path, **params):
        self.calls += 1
        if path == "url":
            album = params["resource"].rsplit("/", 1)[1]
            return ({"relations": [{"target-type": "release", "release": {"id": f"rel-{album}"}}]}
                    if album in self.linked else None)
        if path == "recording":
            return {"recordings": []}
        if path == "release":
            return {"releases": []}
        if path.startswith("release/"):
            return {**RELEASE, "id": path.split("/", 1)[1]}
        raise AssertionError(path)


def _song(sid, album_id, name, artist, track, isrc=None, album_type="album", date="2019-09-17", tracks=10):
    return {"song_id": sid, "album_id": album_id, "album_name": name, "album_artist": artist, "artist": artist,
            "album_type": album_type, "date": date, "track_number": track, "disc_number": 1, "tracks_count": tracks,
            "isrc": isrc, "cover_url": COVER}


def _hit(album_id, name, artist, track, isrc, album_type="album", tracks=10, date="2019-09-17"):
    return {"album": {"id": album_id, "name": name, "album_type": album_type, "artists": [{"name": artist}],
                      "release_date": date, "total_tracks": tracks, "images": [{"url": COVER, "width": 640}]},
            "track_number": track, "disc_number": 1, "external_ids": {"isrc": isrc}}


@pytest.fixture
def lib(tmp_path):
    from beets import config

    from music_scan.library import MusicLibrary

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    config["paths"].set({"default": "$albumartist/$album/$track - $title",
                         "singleton": "$albumartist/$album/$track - $title"})
    with MusicLibrary(tmp_path / "library.db", tmp_path / "library") as library:
        yield library


class Library:
    """The generated library's inputs: pages, records, the search's hits."""

    def __init__(self) -> None:
        self.pages: list[dict] = []
        self.records: dict[str, dict] = {}
        self.hits: dict[str, list[dict]] = {}
        self.linked: set[str] = set()


def build(lib, root: Path, seed: int, albums: int = 12) -> Library:
    """Album records with their release items (stamped, or older ones claimed by
    name and import time), twins, singles a search moves onto an album, a
    compilation, and loose spotdl copies on and off the albums."""
    from beets.library import Item

    rng = random.Random(seed)
    src = root.parent / "src.m4a"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:a", "aac", str(src)], check=True)
    out = Library()
    used: set[Path] = set()

    def add(added: datetime, **fields) -> Item:
        stem = (root / str(fields.get("albumartist") or "-") / str(fields.get("album") or "-")
                / f"{fields.get('track') or 0:02d} - {fields.get('title')}")
        n = 0
        while (path := Path(f"{stem}{f'.{n}' if n else ''}.m4a")) in used:
            n += 1
        used.add(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, path)
        item = Item(path=str(path), **{k: v for k, v in fields.items() if v is not None})
        lib._lib.add(item)
        item.write()
        item.added = added.timestamp()
        item.store()
        return item

    before = GRAB - timedelta(days=3)
    compilation = []
    tracks_of: dict[int, list[int]] = {}
    for k in range(albums):
        artist, name = ARTISTS[k % len(ARTISTS)], WORDS[k]
        # The key is the record's primary Spotify album: the scheme a store without row keys loads (homelab#2478).
        sa, count = f"SA{k}", rng.choice([6, 8, 10])
        key = sa
        date = f"20{10 + k}-0{1 + k % 9}-1{k % 9}"
        listed = [t for t in range(1, count + 1) if rng.random() < 0.75] or [1]
        tracks_of[k] = listed
        out.pages += [_song(f"S{k}x{t}", sa, name, artist, t, f"USA{k:02d}{t:03d}", date=date, tracks=count)
                      for t in listed]
        if rng.random() < 0.6:
            out.linked.add(sa)
        if rng.random() < 0.35:  # Spotify lists the album twice (#214)
            for t in listed[:2]:
                out.pages.append(_song(f"B{k}x{t}", f"SB{k}", name, artist, t, f"USA{k:02d}{t:03d}",
                                       date="2023-01-01", tracks=count))
        if rng.random() < 0.5:  # a single the search finds on the album
            t = rng.choice(listed)
            isrc = f"USA{k:02d}{t:03d}"
            out.pages.append(_song(f"G{k}", f"SG{k}", TITLES[t % 12], artist, 1, isrc, album_type="single",
                                   date="2018-01-01", tracks=1))
            out.hits[isrc] = [_hit(sa, name, artist, t, isrc, tracks=count, date=date),
                              _hit(f"SG{k}", TITLES[t % 12], artist, 1, isrc, album_type="single", tracks=1)]
        if rng.random() < 0.4:
            t = rng.choice(listed)
            compilation.append(_song(f"C{k}x{t}", "COMP", "Hits", "Various", len(compilation) + 1,
                                     f"USA{k:02d}{t:03d}", album_type="compilation", date="2005-01-01", tracks=20))
        files = rng.random() < 0.4
        # Imports run one at a time: each release's own hour.
        grabbed = GRAB + timedelta(hours=2 * k)
        out.records[key] = {
            "status": "imported", "name": name, "artist": artist, "spotify_album_ids": [sa], "tracks_count": count,
            "grabbed_at": grabbed.isoformat(), "imported_at": (grabbed + timedelta(hours=1)).isoformat(),
            **({"release_files": {"mb": {"mb_albumid": f"files-{k}", "mb_releasegroupid": f"rg-{k}"}}}
               if files else {}),
            "playlists": {"pl": [[TITLES[t % 12], artist, f"S{k}x{t}", f"USA{k:02d}{t:03d}", 1, t] for t in listed]}}
        stamped = rng.random() < 0.7  # else released before #279: no album_record, claimed by name and time
        for t in range(1, rng.randint(3, count) + 1):
            fields = {"title": TITLES[t % 12], "artist": artist, "albumartist": artist, "via": "usenet",
                      "album": name if rng.random() < 0.8 else f"{name} (Deluxe Edition)",
                      "track": t if rng.random() < 0.75 else 0, "disc": 1, "tracktotal": count, "sources": "keep",
                      "length": 200.0}
            if stamped:
                fields["album_record"] = key
            if rng.random() < 0.6:
                fields["spotify_ids"] = f"S{k}x{t}"
            if rng.random() < 0.6:
                fields["isrc"] = f"USA{k:02d}{t:03d}"
            if rng.random() < 0.3:
                fields["spotify_album_id"] = sa
            if k and t in listed and rng.random() < 0.35:
                # Also on an earlier album, which canon put it on before #278 ([HOME]).
                o = rng.randrange(k)
                fields.update(spotify_ids=f"S{k}x{t},S{o}x{rng.choice(tracks_of[o])}", spotify_album_id=f"SA{o}")
            add(grabbed + timedelta(minutes=30, seconds=10 * t), **fields)
    out.pages += compilation
    # Loose spotdl items, on the albums and off them.
    for n in range(albums * 6):
        k = rng.randrange(albums)
        artist, name, t = ARTISTS[k % len(ARTISTS)], WORDS[k], rng.choice(tracks_of[k])
        other = rng.randrange(albums)
        kind = rng.choices(["album", "twin", "single", "comp", "two", "isrc", "none"],
                           [0.3, 0.1, 0.12, 0.1, 0.12, 0.16, 0.1])[0]
        sids = {"album": f"S{k}x{t}", "twin": f"B{k}x{t}", "single": f"G{k}", "comp": f"C{k}x{t}",
                "two": f"S{k}x{t},S{other}x{rng.choice(tracks_of[other])}", "isrc": f"X{n}", "none": None}[kind]
        filed = rng.choice([name, f"{TITLES[t % 12]} - Single", "Hits", WORDS[other]])
        fields = {"title": TITLES[t % 12], "artist": artist, "albumartist": artist if filed != "Hits" else "Various",
                  "album": filed, "track": rng.randint(1, 12), "via": "spotdl", "sources": f"pl{n % 3}",
                  "spotify_ids": sids, "length": 200.0,
                  "spotify_album_id": rng.choice([None, f"SA{k}", f"SA{other}", f"SG{k}", "COMP"])}
        if kind == "isrc" or rng.random() < 0.3:
            fields["isrc"] = f"USA{k:02d}{t:03d}"
        add(before + timedelta(seconds=n), **fields)
    return out


@pytest.fixture
def canon_env(tmp_path, lib, monkeypatch):
    """canon.run on the generated library, its records saved, its fakes in place."""
    from music_fetch import albums as albums_state
    from music_scan import canon, cover, dupes, library, navidrome, scan
    from music_scan.mb_release import Resolver

    gen = build(lib, tmp_path / "library", seed=3025)
    albums_state.State(albums=gen.records).save()
    assert set(albums_state.State.load().albums) == set(gen.records)
    monkeypatch.setattr(library, "LIBRARY_DB", tmp_path / "library.db")
    monkeypatch.setattr(library, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(dupes, "REPLACED", tmp_path / "replaced")
    monkeypatch.setattr(canon, "read_pages", lambda refresh=False: {"pl": gen.pages})
    monkeypatch.setattr(canon, "store_songs", lambda: [])
    mb = FakeMB(gen.linked)
    monkeypatch.setattr(canon, "Resolver", lambda budget=None: Resolver(mb=mb, upc_of=lambda _: None, budget=budget))
    real = canon.AlbumsByIsrc
    monkeypatch.setattr(canon, "AlbumsByIsrc", lambda budget=None: real(
        budget=budget, search=lambda isrc: gen.hits.get(isrc, []), interval=0))
    art = (tmp_path / "c.jpg")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x64", "-frames:v", "1",
                    str(art)], check=True)
    monkeypatch.setattr(cover, "download", lambda url: art.read_bytes())
    monkeypatch.setattr(navidrome, "trigger_scan", lambda: None)
    monkeypatch.setattr(scan, "regen_playlists", lambda: {})
    return gen


def _run(tmp_path, caplog, **kw):
    from music_scan import canon

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="music_scan"):
        plan = canon.run(**kw)
    lines = [r.getMessage() for r in caplog.records if r.name.startswith("music_scan")]
    lines = [line.split(" in ")[0] if line.startswith("Supersede backfill:") else line for line in lines]
    return plan, [line.replace(str(tmp_path), "<tmp>") for line in lines]


FIELDS = ("path", "album", "albumartist", "track", "tracktotal", "disc", "disctotal", "year", "spotify_album_id",
          "mb_albumid", "mb_album_via", "canon_wait", "canon_clash", "spotify_ids", "isrc", "sources")


def _state(lib, tmp_path) -> dict[int, tuple]:
    return {i.id: tuple(str(i.get(k) if k != "path" else os.fsdecode(i.path)).replace(str(tmp_path), "<tmp>")
                        for k in FIELDS) for i in lib.all_items()}


def test_the_whole_library_run_decides_as_before(tmp_path, lib, canon_env, caplog):
    _, dry = _run(tmp_path, caplog, apply=False)
    _, applied = _run(tmp_path, caplog, apply=True)
    _, again = _run(tmp_path, caplog, apply=False)
    state = [" ".join(v) for _, v in sorted(_state(lib, tmp_path).items())]
    text = "\n".join(["# dry run", *dry, "# apply", *applied, "# dry run after", *again,
                      "# library after the apply", *state]) + "\n"
    # The library reaches the steps a scope has to get right.
    for marker in ("[HOME]", "[SUPERSEDE]", "[KEEP]", "[TWIN]", "[RETAG]", "[MB-URL]", "[MB-NONE]",
                   "item(s) on their album instead of a single", "→ <tmp>/library/"):
        assert marker in text, marker
    if os.environ.get("CANON_GOLDEN_WRITE"):
        GOLDEN.write_text(text)
        pytest.skip("golden file written")
    assert text == GOLDEN.read_text()

