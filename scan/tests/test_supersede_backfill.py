"""Supersede's backfill over a generated library (homelab#3025).

A whole-library ``music-canon-albums`` spent hours in its supersede phase, since
each record's plan walked the whole library.  These tests pin what the backfill
decides (its pairs, rungs and lines, in order) on a library varied enough to
reach every rung, veto and tie (the golden file was written by the
implementation before the :class:`~music_scan.supersede.Index`), and that its
cost grows with the library, not with records times the library.

Items carry no audio: the fingerprints come from a fake decoder, and an apply's
files are empty.  Regenerate the golden file, only for a change meant to alter
the output, with ``SUPERSEDE_GOLDEN_WRITE=1``.
"""

import logging
import os
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

GOLDEN = Path(__file__).parent / "supersede_backfill.golden"
GRAB = datetime(2026, 10, 10, 21, 0, tzinfo=timezone.utc)
IMPORTED = datetime(2026, 10, 10, 22, 0, tzinfo=timezone.utc)
ARTISTS = ["Arbor", "Basalt", "Cinder", "Dune", "Ember", "Fjord"]
WORDS = ["Amber", "Birch", "Coral", "Delta", "Elm", "Fern", "Garnet", "Heron", "Iris", "Jade", "Kestrel",
         "Lichen", "Maple", "Nettle", "Onyx", "Pine", "Quartz", "Rowan", "Sorrel", "Thistle", "Umber", "Vetch",
         "Willow", "Yarrow", "Zinnia", "Aspen", "Bramble", "Cedar", "Dahlia", "Elder"]
TITLES = ["Lantern", "Harbour Lights", "Paper Moon", "Static", "Undertow", "Glass", "Northern", "Tide", "Ash",
          "Vesper", "Orbit", "Hollow"]
SUFFIXES = ["", "", "", "", " - Remastered 2011", " (Live)", " - Extended Mix", " (Demo)"]
LENGTHS = [200.0, 201.5, 230.0, 260.0]


def _frames(seed: int, flips: int = 0) -> list[int]:
    rng, noise = random.Random(seed), random.Random(9)
    out = []
    for _ in range(400):
        f = rng.getrandbits(32)
        for _ in range(flips):
            f ^= 1 << noise.randrange(32)
        out.append(f)
    return out


# Six recordings, each as itself (a), a re-encode (b, BER ~0.03) and a look-alike (c, ~0.12).
FPS = {f"fp{n}{v}": _frames(n, flips) for n in range(6) for v, flips in (("a", 0), ("b", 1), ("c", 4))}


@pytest.fixture
def lib(tmp_path):
    from beets import config

    from music_scan.library import MusicLibrary

    config["paths"].set({"default": "$albumartist/$album/$track - $title",
                         "singleton": "$albumartist/$album/$track - $title"})
    with MusicLibrary(tmp_path / "library.db", tmp_path / "library") as library:
        yield library


@pytest.fixture
def fake_audio(monkeypatch):
    from music_scan import dupes

    monkeypatch.setattr(dupes, "decode", FPS.__getitem__)


def _add(lib, root: Path, added: datetime, used: set, clash: bool = False, **fields):
    """An item with an empty file where beets would file it (a ``.1`` copy when
    that path is taken, or with *clash*): enough for a merge to move it."""
    from beets.library import Item

    stem = (root / str(fields.get("albumartist") or "-") / str(fields.get("album") or "-")
            / f"{fields.get('track') or 0:02d} - {fields.get('title')}")
    n = 1 if clash else 0
    while (path := Path(f"{stem}{f'.{n}' if n else ''}.m4a")) in used:
        n += 1
    used.add(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    item = Item(path=str(path), **{k: v for k, v in fields.items() if v is not None})
    lib._lib.add(item)
    item.added = added.timestamp()
    item.store()
    return item


def _title(rng: random.Random) -> str:
    return rng.choice(TITLES) + rng.choice(SUFFIXES)


def _noise(rng: random.Random, fields: dict, isrcs: list[str], sids: list[str]) -> dict:
    """Now and then an ID of another album's item, a second ISRC or a fingerprint: vetoes and ties."""
    if isrcs and rng.random() < 0.12:
        fields["isrc"] = ";".join(filter(None, [fields.get("isrc"), rng.choice(isrcs)]))
    if sids and rng.random() < 0.06:
        fields["spotify_ids"] = ",".join(filter(None, [fields.get("spotify_ids"), rng.choice(sids)]))
    if rng.random() < 0.05:
        fields["acoustid_id"] = f"aid-{rng.randrange(4)}"
    if "acoustid_fingerprint" not in fields and rng.random() < 0.6:
        fields["acoustid_fingerprint"] = f"fp{rng.randrange(6)}{rng.choice('abc')}"
    return fields


def build(lib, root: Path, seed: int, albums: int = 30, loose: int = 450) -> dict[str, dict]:
    """A library of *albums* Usenet releases and *loose* other items, most of
    them related to a release item by one rung's key and perturbed, so that
    rungs, vetoes and ties all come up; returns the album records."""
    rng = random.Random(seed)
    window = GRAB + timedelta(minutes=30)
    before = GRAB - timedelta(days=3)
    records: dict[str, dict] = {}
    releases: list[tuple[dict, list, list]] = []  # (home, release items, tracklist)
    isrcs, sids = [], []
    used: set = set()
    for k in range(albums):
        artist, name = rng.choice(ARTISTS), WORDS[k % len(WORDS)]
        album_id, count = f"SA{k}", rng.choice([8, 10, 12])
        home = {"album": name if rng.random() < 0.8 else f"{name} (Deluxe Edition)", "albumartist": artist,
                "artist": artist, "spotify_album_id": album_id}
        status = rng.choices(["imported", "failed", "partial"], [0.85, 0.1, 0.05])[0]
        release = []
        for n in range(rng.randint(3, 8)):
            fields = {**home, "title": _title(rng), "via": "usenet", "sources": "keep",
                      "track": rng.choice([n + 1, n + 1, n + 1, rng.randint(0, 12)]),
                      "disc": rng.choice([1, 1, 1, 0, 2]), "tracktotal": rng.choice([count, count, count, 14]),
                      "length": rng.choice(LENGTHS)}
            if rng.random() < 0.65:
                fields["spotify_ids"] = f"S{k}x{n}"
                sids.append(fields["spotify_ids"])
                if rng.random() < 0.2:
                    fields["words_ids"] = fields["spotify_ids"]
            if rng.random() < 0.6:
                fields["isrc"] = f"USAAA26{k:02d}{n:03d}"
                isrcs.append(fields["isrc"])
            if rng.random() < 0.5:
                fields["mb_trackid"] = f"rec-{k}-{n}"
            if rng.random() < 0.75:
                fields["acoustid_fingerprint"] = f"fp{rng.randrange(6)}a"
                if rng.random() < 0.4:
                    fields["acoustid_id"] = f"aid-{k}-{n}"
            if rng.random() < 0.15:
                fields["spotify_album_id"] = rng.choice([None, f"SA{rng.randrange(albums)}"])
            added = window if rng.random() < 0.9 else before
            release.append(_add(lib, root, added, used, clash=rng.random() < 0.2, **_noise(rng, fields, isrcs, sids)))
        tracks = []
        if rng.random() < 0.75:
            for n, item in enumerate(release):
                own = sorted(set((item.get("spotify_ids") or "").split(",")) - {""})
                song = own[0] if own and rng.random() < 0.75 else f"TW{k}x{n}"
                isrc = rng.choices([(item.get("isrc") or "").split(";")[0] or None, f"GBBBB26{k:02d}{n:03d}", None],
                                   [0.4, 0.3, 0.3])[0]
                slot = (item.disc or 1, item.track or 0) if rng.random() < 0.85 else (1, rng.randint(1, 12))
                tracks.append([item.title if rng.random() < 0.8 else _title(rng), artist, song, isrc, *slot])
            for n in range(rng.randint(0, 3)):
                tracks.append([_title(rng), artist, f"TE{k}x{n}", None, 1, rng.randint(1, 12)])
        releases.append((home, release, tracks))
        records[f"R{k}"] = {"status": status, "name": name, "artist": artist, "tracks_count": count,
                            "grabbed_at": GRAB.isoformat(), "imported_at": IMPORTED.isoformat(),
                            **({"fallback_from": "partial"} if status == "partial" else {}),
                            **({"playlists": {"pl": tracks}} if tracks else {})}
    # A record that claims another's items, and one with no release in the library.
    records["R-twin"] = {**records["R0"], "tracks_count": 10}
    records["R-none"] = {**records["R1"], "name": "Nowhere"}
    best = {"album": "Greatest Hits", "albumartist": "Arbor", "artist": "Arbor", "spotify_album_id": "BEST"}
    for n in range(loose):
        home, release, tracks = rng.choice(releases)
        target = rng.choice(release)
        entry = rng.choice(tracks) if tracks else None
        spotify_only = [e for e in tracks if (e[3] or "").startswith("GB")]
        kind = rng.choices(["on", "single", "best", "other"], [0.6, 0.22, 0.06, 0.12])[0]
        title = target.title if rng.random() < 0.5 else _title(rng)
        if kind == "on":
            where = dict(home)
            if rng.random() < 0.1:
                where["spotify_album_id"] = rng.choice([None, f"SA{rng.randrange(albums)}"])
        elif kind == "single":
            artist = home["artist"] if rng.random() < 0.8 else rng.choice(ARTISTS)
            where = {"album": f"{title} {n}" if rng.random() < 0.85 else title, "albumartist": artist,
                     "artist": artist, "spotify_album_id": f"SG{n}"}
        elif kind == "best":
            where = best
        else:
            artist = rng.choice(ARTISTS)
            where = {"album": rng.choice(WORDS), "albumartist": artist, "artist": artist,
                     "spotify_album_id": f"OT{rng.randrange(5)}"}
        fields = {**where, "title": title, "via": rng.choices(["spotdl", "manual", "usenet", "", "youtube"],
                                                                [0.8, 0.06, 0.06, 0.04, 0.04])[0],
                  "sources": f"pl{rng.randrange(4)}", "track": rng.randint(0, 12), "disc": rng.choice([1, 1, 0, 2]),
                  "length": rng.choice(LENGTHS)}
        how = rng.choices(["sid", "isrc", "mb", "slot", "entry isrc", "entry slot", "audio", "entry id", "none"],
                          [0.1, 0.08, 0.1, 0.12, 0.14, 0.12, 0.2, 0.06, 0.08])[0]
        twin = f"TX{n}" if rng.random() < 0.85 else None
        if how == "sid" and target.get("spotify_ids"):
            fields["spotify_ids"] = target.get("spotify_ids")
        elif how == "isrc" and target.get("isrc"):
            fields["isrc"] = target.get("isrc")
        elif how == "mb" and target.get("mb_trackid"):
            fields["mb_trackid"] = target.get("mb_trackid")
            fields["length"] = target.length + rng.choice([0.0, 1.5, 12.0])
        elif how == "slot":
            fields.update(track=target.track, disc=target.disc, spotify_ids=twin)
            fields["title"] = target.title + rng.choice(["", " - Remastered 2011"]) if rng.random() < 0.8 else title
        elif how in ("entry isrc", "entry slot", "entry id") and entry:
            fields["spotify_ids"] = entry[2] if how == "entry id" else twin
            if how == "entry isrc" and spotify_only:
                fields["isrc"] = rng.choice(spotify_only)[3]
            if how == "entry slot":
                fields.update(disc=entry[4], track=entry[5])
                fields["title"] = entry[0] if rng.random() < 0.7 else title
        elif how == "audio" and target.get("acoustid_fingerprint"):
            fields["acoustid_fingerprint"] = target.get("acoustid_fingerprint")[:-1] + rng.choice("abbc")
            fields["length"] = target.length + rng.choice([0.0, 0.5, 30.0])
            fields["title"] = target.title if rng.random() < 0.7 else _title(rng)
            if rng.random() < 0.7:
                fields.update(track=target.track, disc=target.disc)
            if target.get("acoustid_id") and rng.random() < 0.7:
                fields["acoustid_id"] = target.get("acoustid_id")
            fields["spotify_ids"] = twin
        else:
            fields["spotify_ids"] = twin
        if rng.random() < 0.08:
            fields["album_record"] = f"R{rng.randrange(albums)}"
        _add(lib, root, before, used, **_noise(rng, fields, isrcs, sids))
    return records


MARKERS = ["(spotify id;", "(isrc;", "(recording id;", "(disc/track;", "(tracklist isrc;", "(tracklist disc/track;",
           "(same audio, fingerprint", "(same audio, acoustid", "[MANUAL]", "[MOVE]", "filed under",
           "it has no Spotify ID", "the record has no tracklist", "which no item of the release holds",
           "holds by title words only", "differ from track", "another title", "another loose item already took track",
           "the fingerprints differ", "apart, but the titles differ", "s apart;",
           "have no fingerprint, so the same-audio rung skipped them",
           "it has no fingerprint, so the same-audio rung was skipped", "at its disc/track",
           "no entry sits at its disc/track", "is not on the album's tracklist (a twin release's?); track",
           "[MERGE]"]


def _run(lib, records, tmp_path, caplog, apply: bool) -> list[str]:
    from music_scan import supersede

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="music_scan"):
        pairs = supersede.backfill(lib, records, apply=apply, replaced_dir=tmp_path / "replaced")
    lines = [r.getMessage() for r in caplog.records if r.name.startswith("music_scan")]
    # The elapsed time is the one thing that differs between runs.
    lines = [line.split(" in ")[0] if line.startswith("Supersede backfill:") else line for line in lines]
    lines += [f"pair {p.album.id} <- {[i.id for i in p.loose]} {p.how}" for p in pairs]
    return [line.replace(str(tmp_path), "<tmp>") for line in lines]


def _state(lib) -> list[str]:
    return [" ".join(str(i.get(k) or "-") for k in ("id", "sources", "spotify_ids", "isrc", "mb_trackid"))
            for i in sorted(lib.all_items(), key=lambda i: i.id)]


def test_backfill_over_a_generated_library_decides_as_before(tmp_path, lib, fake_audio, caplog):
    records = build(lib, tmp_path / "library", seed=3025)
    out = ["# dry run", *_run(lib, records, tmp_path, caplog, apply=False)]
    out += ["# apply", *_run(lib, records, tmp_path, caplog, apply=True)]
    out += ["# library after the apply", *_state(lib)]
    text = "\n".join(out) + "\n"

    # The library reaches every rung, and the ties and vetoes around them.
    missing = [m for m in MARKERS if m not in text]
    assert not missing
    if os.environ.get("SUPERSEDE_GOLDEN_WRITE"):
        GOLDEN.write_text(text)
        pytest.skip("golden file written")
    assert text == GOLDEN.read_text()


def _filler(lib, root: Path, n: int, start: int = 0) -> None:
    """*n* spotdl items no rung relates to any release: other IDs, titles and albums."""
    used: set = set()
    for k in range(start, start + n):
        _add(lib, root, GRAB - timedelta(days=3), used, title=f"Filler {k}", album=f"Filler Album {k % 10}",
             albumartist="Nobody", artist="Nobody", spotify_album_id=f"FA{k % 10}", spotify_ids=f"F{k}",
             isrc=f"ZZAAA26{k:05d}", mb_trackid=f"filler-{k}", via="spotdl", sources="pl0", track=k % 12 + 1,
             length=180.0, acoustid_fingerprint="fp0c")


def test_backfill_reads_each_item_a_bounded_number_of_times(tmp_path, lib, fake_audio, monkeypatch):
    """homelab#3025: each record's plan read every item's fields again, so a
    whole-library dry run took 641 s at 8,000 items and 16,607 s at 35,500.
    The reads an extra item costs no longer depend on how many records there are."""
    from beets.library import Item

    from music_scan import supersede

    records = build(lib, tmp_path / "library", seed=1, albums=20, loose=80)
    reads = []
    for name in ("get", "__getitem__"):
        real = getattr(Item, name)
        monkeypatch.setattr(Item, name, lambda self, *a, _real=real, **kw: reads.append(1) or _real(self, *a, **kw))

    def count() -> int:
        reads.clear()
        supersede.backfill(lib, records, apply=False)
        return len(reads)

    _filler(lib, tmp_path / "library", 100)
    small = count()
    _filler(lib, tmp_path / "library", 400, start=100)
    large = count()
    # About 15 reads per item (the index, the claim, the sort); the plan per record read ~50 per record.
    assert (large - small) / 400 < 30
