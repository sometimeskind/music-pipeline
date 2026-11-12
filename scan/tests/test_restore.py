"""music-album-audit-releases --restore: a kept file back on its album (homelab#2815)."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from music_scan import release, restore

# fetch/tests in the repo (CI), /app/fetch_tests in the dev image (just test).
_here = Path(__file__).resolve().parent
sys.path.insert(0, str(next((p for p in (_here.parent.parent / "fetch" / "tests", _here.parent / "fetch_tests")
                             if (p / "pg_fixture.py").exists()), _here)))
from pg_fixture import pg_url  # noqa: E402, F401 — a fixture

needs_pg = pytest.mark.skipif(not os.environ.get("MUSIC_TEST_DATABASE_URL"),
                              reason="MUSIC_TEST_DATABASE_URL is unset")

LIVE = "17052-03-10 - Reason To Believe (Nebraska Live).m4a"
STUDIO = "c7e7f3f2-0000-4000-8000-000000000001"  # 9045's recording, which 17052 took at its import
OWN, OTHER = "nebraska-own@100", "nebraska-82@300"  # 1781's own release, and 1184's merged into it
RECORDS = {"1781": {"artist": "Bruce Springsteen", "name": "Nebraska", "status": "imported",
                    "spotify_album_ids": ["1781SPOTIFYALBUM"], "imported_at": "1970-01-01T00:03:20+00:00",
                    "playlists": {"springsteen": []}}}


def _no_fpcalc(path):
    raise OSError("no fpcalc here")


def _audio(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:a", "aac", str(path)], check=True)


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

    _audio(path)
    item = Item(path=str(path), artist="Bruce Springsteen", albumartist="Bruce Springsteen", **fields)
    lib._lib.add(item)
    item.write()
    item.store()
    return item


@pytest.fixture
def nebraska(lib, tmp_path):
    """Album 1781 as 2026-11-04 left it: its own release, one of 1184's tracks merged in,
    and the live take's file in quarantine/keep/ with the studio recording it took."""
    root = tmp_path / "library" / "Bruce Springsteen" / "Nebraska"
    studio = _add(lib, root / "10 - Reason to Believe.m4a", title="Reason to Believe", album="Nebraska", track=10,
                  mb_trackid=STUDIO, isrc="USSM18200390", trackdisambig="version 1b", album_record="1781",
                  release=OWN, via="usenet", sources="springsteen")
    title = _add(lib, root / "01 - Nebraska.m4a", title="Nebraska", album="Nebraska", track=1,
                 mb_trackid="aaaaaaaa-0000-4000-8000-000000000001", album_record="1781", release=OWN, via="usenet")
    merged = _add(lib, root / "01-05 - Born in the U.S.A..m4a", title="Born in the U.S.A.", album="Nebraska",
                  disc=1, track=5, mb_trackid="bbbbbbbb-0000-4000-8000-000000000001", album_record="1781",
                  release=OTHER, via="usenet")
    keep = tmp_path / "quarantine" / "keep"
    _kept(keep / LIVE, title="Reason To Believe (Nebraska Live)", mb_trackid=STUDIO)
    return {"lib": lib, "keep": keep, "studio": studio, "title": title, "merged": merged}


def _kept(path: Path, **tags) -> None:
    from mediafile import MediaFile

    _audio(path)
    mf = MediaFile(str(path))
    fields = {"artist": "Bruce Springsteen", "albumartist": "Bruce Springsteen",
              "album": "Nebraska '82: Expanded Edition", "disc": 3, "disctotal": 4, "track": 10, **tags}
    for k, v in fields.items():
        setattr(mf, k, v)
    mf.save()


# MusicBrainz's recordings, by ID: the studio take, as the import's lookup reads it.
MB = {STUDIO: {"id": STUDIO, "title": "Reason to Believe", "disambiguation": "version 1b"}}


def _plan(n, name="17052", key="1781", records=None, **kw):
    records = records or RECORDS
    return restore.plan(name, key, records[key], n["lib"].all_items(), n["lib"]._lib, n["keep"],
                        fingerprinter=kw.pop("fingerprinter", _no_fpcalc), recordings=kw.pop("recordings", MB.get),
                        **kw)


def _lines(r) -> str:
    return "\n".join(r.lines + [f"[REFUSED] {w}" for w in r.refusals])


def test_the_live_take_gets_no_recording_rather_than_the_studio_one(nebraska):
    r = _plan(nebraska)
    text = _lines(r)
    assert r.refusals == [], text
    assert f"[RESTORE] keep/{LIVE} (" in text and "was item 17052) → album 1781 Bruce Springsteen — Nebraska" in text
    assert ("tags: Bruce Springsteen — Reason To Believe (Nebraska Live) [Nebraska '82: Expanded Edition], "
            "disc 3 track 10, 0:01") in text
    assert f"identity tags: mb_trackid {STUDIO}" in text
    studio = nebraska["studio"].id
    assert (f"[CLASH] {studio} Bruce Springsteen — Reason to Believe [Nebraska] (trackdisambig 'version 1b') "
            "shares its ID: another version, live") in text
    assert (f"[RECORDING] none: the file's mb_trackid {STUDIO} is {studio}'s recording, another version (live); "
            "cleared: mb_trackid") in text
    assert "[HOLDER] none: no library item would hold its recording" in text
    assert r.item.get("mb_trackid") == "" and r.dropped == {"mb_trackid": STUDIO}
    assert (r.item["album_record"], r.item["album_extra"], r.item["canon_wait"], r.item["via"], r.item["sources"]) == (
        "1781", "1", "1", "usenet", "springsteen")
    assert r.dest.name == "10 - Reason To Believe (Nebraska Live).m4a"
    # A dry run changes nothing.
    assert (nebraska["keep"] / LIVE).exists() and len(nebraska["lib"].all_items()) == 3


def test_two_dry_runs_print_the_same(nebraska):
    """The gate re-runs the dry run and compares its output."""
    assert _lines(_plan(nebraska)) == _lines(_plan(nebraska))


def test_the_same_recording_on_the_release_is_refused(nebraska):
    """Without a version word the file is the studio take's copy: retire_held would retire it again."""
    _kept(nebraska["keep"] / LIVE, title="Reason to Believe", mb_trackid=STUDIO)
    r = _plan(nebraska)
    studio = nebraska["studio"].id
    assert f"[RECORDING] {STUDIO}: the file's own" in _lines(r)
    assert r.refusals == [f"{studio} Bruce Springsteen — Reason to Believe [Nebraska], a release item of album 1781, "
                          f"holds its recording (mb_trackid {STUDIO}): the next scan's retire_held would retire it "
                          "again (extra, now held)"]
    with pytest.raises(SystemExit, match=r"refused \(1 reason\(s\)\); nothing imported"):
        restore.show(r)


def test_another_albums_copy_is_refused_unless_allow_held(nebraska):
    other = _add(nebraska["lib"], Path(os.fsdecode(nebraska["lib"]._lib.directory)) / "Bruce Springsteen" / "Live"
                 / "03 - Reason.m4a", title="Reason To Believe (Nebraska Live)", album="Live", isrc="USSM12500001", album_record="77",
                 via="usenet")
    _kept(nebraska["keep"] / LIVE, title="Reason To Believe (Nebraska Live)", isrc="USSM12500001")
    r = _plan(nebraska)
    assert r.refusals == [f"the library holds it already: {other.id} Bruce Springsteen — Reason To Believe (Nebraska "
                          "Live) [Live] (isrc USSM12500001); allow_held imports it beside"]
    r = _plan(nebraska, allow_held=True)
    assert r.refusals == []
    assert (f"[HOLDER] {other.id} Bruce Springsteen — Reason To Believe (Nebraska Live) [Live] would hold the same "
            "recording: isrc USSM12500001") in _lines(r)


def test_the_same_audio_as_a_release_item_is_refused(nebraska):
    """retire_held merges an extra that is a certain-tier duplicate of a release item."""
    studio = nebraska["studio"]
    studio["acoustid_id"] = "acoustid-1"
    studio.store()
    _kept(nebraska["keep"] / LIVE, title="Reason to Believe", acoustid_id="acoustid-1")
    r = _plan(nebraska)
    [why] = r.refusals
    assert why.startswith(f"{studio.id} Bruce Springsteen — Reason to Believe [Nebraska], a release item of album "
                          "1781, holds its recording (same audio, acoustid acoustid-1, ")
    assert why.endswith(f"would merge it into {studio.id} (extra, same audio)")


def test_a_recording_id_at_another_length_is_no_holder(nebraska):
    """homelab#2540: a shared mb_trackid holds a recording only within 2s."""
    _kept(nebraska["keep"] / LIVE, title="Reason to Believe", mb_trackid=STUDIO)
    studio = nebraska["studio"]
    studio["length"] = 30.0
    studio.store()
    r = _plan(nebraska)
    assert r.refusals == []
    assert f"[SIBLING] {studio.id} Bruce Springsteen — Reason to Believe [Nebraska] shares mb_trackid {STUDIO} 29s " \
           "apart" in _lines(r)


@pytest.mark.parametrize("change, refusal", [
    ({"status": "grabbed"}, "album 1781 is mid-import"),
    ({}, "album 1781 has no release items"),
])
def test_an_album_that_cant_take_an_extra_is_refused(nebraska, change, refusal):
    records = {"1781": {**RECORDS["1781"], **change}}
    if not change:
        for item in nebraska["lib"].all_items():
            item["album_record"] = "9"
            item.store()
    assert any(w.startswith(refusal) for w in _plan(nebraska, records=records).refusals)


def test_a_taken_library_path_is_refused(nebraska):
    dest = _plan(nebraska).dest
    _audio(dest)
    assert any(w.startswith(f"its library path {dest} is taken") for w in _plan(nebraska).refusals)


@pytest.mark.parametrize("name, error", [
    ("17060", "no audio file"),
    ("../keep/" + LIVE, "must be a path inside quarantine/keep/"),
])
def test_a_name_that_names_no_kept_file_is_refused(nebraska, name, error):
    with pytest.raises(SystemExit, match=error):
        _plan(nebraska, name=name)


def test_the_file_is_named_as_list_quarantine_shows_it(nebraska):
    assert _plan(nebraska, name=f"keep/{LIVE}").src == nebraska["keep"] / LIVE


def _events(url):
    with psycopg.connect(url) as conn:
        return conn.execute("SELECT kind, item_id, data FROM events WHERE kind = 'restored'").fetchall()


@needs_pg
def test_the_restored_take_stays_on_its_album_through_the_next_scan(nebraska, pg_url):
    """The point of the stamp: the release pass keeps it as the album's own extra."""
    from mediafile import MediaFile

    from music_fetch import db

    lib = nebraska["lib"]
    r = _plan(nebraska)
    with contextlib.closing(db.connect()) as conn:
        item = restore.restore(r, lib._lib, conn)
    assert not (nebraska["keep"] / LIVE).exists() and r.dest.exists()
    stored = lib.get_item(item.id)
    assert (stored["album_record"], stored["album_extra"], stored["canon_wait"]) == ("1781", "1", "1")
    assert stored.get("mb_trackid") == "" and MediaFile(str(r.dest)).mb_trackid in (None, "")
    [(kind, item_id, data)] = _events(pg_url)
    assert (kind, item_id, data["key"], data["was"]) == ("restored", item.id, "1781", 17052)
    assert data["dropped"] == {"mb_trackid": STUDIO} and data["dropped_why"] == "another version"
    assert data["clashing"] == [nebraska["studio"].id] and data["recording"] is None

    # The next scan's release pass: 1781 holds two releases (1184 merged in), so retire_merged
    # weighs every other copy against its own release, then retire_held every extra.
    assert release.retire_merged(lib, RECORDS, lambda item: set()) == 0
    assert release.retire_held(lib) == 0
    after = lib.get_item(item.id)
    assert after is not None and after.get("album_extra") == "1" and r.dest.exists()
    assert lib.get_item(nebraska["merged"].id).get("album_extra") == "1"  # retire_merged did weigh 1781


@needs_pg
def test_a_failed_write_undoes_the_import(nebraska, pg_url, monkeypatch):
    from beets.library import Item

    from music_fetch import db

    monkeypatch.setattr(Item, "try_write", lambda self, *a, **kw: False)
    r = _plan(nebraska)
    with contextlib.closing(db.connect()) as conn, pytest.raises(OSError, match="could not write the tags"):
        restore.restore(r, nebraska["lib"]._lib, conn)
    assert (nebraska["keep"] / LIVE).exists() and not r.dest.exists()
    assert len(nebraska["lib"].all_items()) == 3 and _events(pg_url) == []


@pytest.mark.parametrize("argv, error", [
    (["--restore", "17052"], "--restore and --album go together"),
    (["--album", "1781"], "--restore and --album go together"),
    (["--allow-held"], "--allow-held goes with --restore"),
    (["--restore", "17052", "--album", "1781", "--keep", "17052"], "--restore puts one file back"),
])
def test_the_cli_takes_restore_alone_with_its_album(argv, error, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["music-album-audit-releases", *argv])
    with pytest.raises(SystemExit):
        release.audit_main()
    assert error in capsys.readouterr().err


def test_with_no_item_sharing_its_id_musicbrainz_names_the_recording(nebraska):
    """Once 9045's recording is fixed nothing in the library holds the studio ID: the
    import's rule still refuses it, against MusicBrainz's recording."""
    studio = nebraska["studio"]
    studio["mb_trackid"] = "dddddddd-0000-4000-8000-000000000001"
    studio.store()
    r = _plan(nebraska)
    text = _lines(r)
    assert r.refusals == [], text
    assert (f"[CLASH] MusicBrainz recording {STUDIO} is 'Reason to Believe' (version 1b): another version, live"
            in text)
    assert (f"[RECORDING] none: the file's mb_trackid {STUDIO} is MusicBrainz's, another version (live); cleared: "
            "mb_trackid") in text
    assert r.item.get("mb_trackid") == "" and r.clashing == []


def test_the_files_own_recording_stays(nebraska):
    live = "eeeeeeee-0000-4000-8000-000000000001"
    _kept(nebraska["keep"] / LIVE, title="Reason To Believe (Nebraska Live)", mb_trackid=live)
    r = _plan(nebraska, recordings={live: {"title": "Reason to Believe", "disambiguation": "live, 2025"}}.get)
    text = _lines(r)
    assert r.refusals == [] and r.item.get("mb_trackid") == live, text
    assert f"[MB] recording {live} is 'Reason to Believe' (live, 2025): the file's title and album agree" in text
    assert f"[RECORDING] {live}: the file's own" in text


def test_a_failed_lookup_is_refused(nebraska):
    studio = nebraska["studio"]
    studio["mb_trackid"] = "dddddddd-0000-4000-8000-000000000001"
    studio.store()

    def down(_):
        raise OSError("503")

    [why] = _plan(nebraska, recordings=down).refusals
    assert why.startswith(f"the MusicBrainz lookup of its recording {STUDIO} failed (503)")
