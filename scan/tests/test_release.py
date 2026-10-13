"""A Usenet release imported as one album (#279)."""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from music_scan import release


class FakeItem(dict):
    def __init__(self, id_=1, added=100.0, **fields):
        super().__init__(**fields)
        self.id, self.added, self.stored = id_, added, 0

    def get(self, key, default=None):
        return super().get(key, default)

    def store(self):
        self.stored += 1


@pytest.fixture
def ffmpeg():
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")


def _m4a(path: Path, **tags) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:a", "aac", str(path)], check=True)
    if tags:
        from mediafile import MediaFile

        mf = MediaFile(str(path))
        for k, v in tags.items():
            setattr(mf, k, v)
        mf.save()
    return path


def _match(distance, album_id="MB1", extra_tracks=0, extra_items=0, tracks=10):
    info = SimpleNamespace(album_id=album_id, album="Ready to Die", tracks=[object()] * tracks,
                           releasegroup_id="RG1", artist_id="A1", artists_ids=["A1"], albumdisambig="",
                           releasegroupdisambig="")
    return SimpleNamespace(distance=distance, info=info, extra_tracks=[object()] * extra_tracks,
                           extra_items=[object()] * extra_items)


def test_match_folder_reports_the_closest_release_and_its_missing_tracks(tmp_path, ffmpeg) -> None:
    files = [_m4a(tmp_path / f"{n:02d}.m4a") for n in (1, 2)]
    proposal = SimpleNamespace(candidates=[_match(0.3, "FAR"), _match(0.05, "CD94", extra_tracks=1, extra_items=1)])
    found = release.match_folder(files, tag_album=lambda source: proposal)
    assert found["mb_albumid"] == "CD94" and found["missing"] == 1 and found["unmatched"] == 1
    assert found["files"] == 2 and found["mb"] == {"mb_albumid": "CD94", "mb_releasegroupid": "RG1",
                                                   "mb_albumartistid": "A1", "mb_albumartistids": ["A1"]}


def test_match_folder_with_no_close_release_identifies_nothing(tmp_path, ffmpeg) -> None:
    files = [_m4a(tmp_path / "01.m4a")]
    assert release.match_folder(files, tag_album=lambda s: SimpleNamespace(candidates=[_match(0.4)])) is None
    assert release.match_folder(files, tag_album=lambda s: SimpleNamespace(candidates=[])) is None
    assert release.match_folder([], tag_album=lambda s: pytest.fail("no files, no search")) is None


def test_identify_runs_beets_in_a_subprocess(tmp_path, caplog) -> None:
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="noise\n" + json.dumps(
            {"mb_albumid": "CD94", "album": "Ready to Die", "tracks": 17, "missing": 0, "unmatched": 1}),
            stderr="")

    with caplog.at_level("INFO", logger="music_scan.release"):
        found = release.identify(tmp_path, run=run)
    assert found["mb_albumid"] == "CD94" and calls[0][1:] == ["-m", "music_scan.release", str(tmp_path)]
    assert "[RELEASE]" in caplog.text
    assert release.identify(tmp_path, run=lambda c, **kw: SimpleNamespace(returncode=0, stdout="null",
                                                                            stderr="")) is None
    with pytest.raises(RuntimeError):
        release.identify(tmp_path, run=lambda c, **kw: SimpleNamespace(returncode=1, stdout="", stderr="boom"))


def test_import_rest_imports_the_unmatched_files_as_they_are(tmp_path) -> None:
    quarantine, inbox = tmp_path / "quarantine", tmp_path / "inbox"
    job = quarantine / "usenet" / "keep" / "Job"
    job.mkdir(parents=True)
    for name in ("01.flac", "02.flac"):
        (job / name).write_bytes(b"x")
    calls = []

    def run_import(folder, asis=False, config=None):
        calls.append((folder, asis, config.read_text()))
        (folder / "01.flac").unlink()  # beets imported it; 02 it skipped

    assert release.import_rest(quarantine, inbox, "keep", "Job", run_import=run_import) == 2
    assert calls == [(inbox / "usenet" / "keep" / "Job", True, "import:\n  duplicate_action: keep\n")]
    assert sorted(p.name for p in job.iterdir()) == ["02.flac"]  # skipped: back in quarantine
    assert not (inbox / "usenet" / "keep" / "Job").exists()
    assert release.import_rest(quarantine, inbox, "keep", "Nothing", run_import=run_import) == 0


def test_claim_stamps_this_imports_usenet_items() -> None:
    def usenet(id_, added=200.0, job="RTD-Job", **f):
        return FakeItem(id_, added, via="usenet", usenet_job=job, **f)

    fresh = usenet(1, sources="keep", title="Juicy")
    other_playlist = usenet(2, sources="later")
    older = usenet(3, 50.0, sources="keep")
    spotdl = FakeItem(4, 200.0, via="spotdl", sources="keep")
    held = usenet(5, sources="keep", album_record="OTHER")
    # Another job's leftovers the same scan imported (#284).
    leftover = usenet(6, sources="keep", job="Hello Sadness-Job")
    mine = release.claim([fresh, other_playlist, older, spotdl, held, leftover], "keep", 100.0, "RTD", "r1",
                         "RTD-Job")
    assert mine == [fresh] and fresh["album_record"] == "RTD" and fresh["release"] == "r1" and fresh.stored == 1
    assert not leftover.get("album_record")


RTD = {"name": "Ready to Die (The Remaster)", "artist": "The Notorious B.I.G."}


def _legacy(id_, **f):
    return FakeItem(id_, via="usenet", album="Ready to Die", albumartist="The Notorious B.I.G.", **f)


def test_copies_takes_every_other_copy_and_the_legacy_items_of_the_album() -> None:
    kept = FakeItem(1, album_record="RTD", release="r2")
    earlier = FakeItem(2, album_record="RTD", release="r1")
    legacy = _legacy(3, spotify_album_id="RTD")
    legacy_no_album = _legacy(4)
    another_album = _legacy(5, spotify_album_id="BESTOF")
    other_record = FakeItem(6, album_record="OTHER", release="r9")
    spotdl = FakeItem(7, via="spotdl", album="Ready to Die", albumartist="The Notorious B.I.G.",
                      spotify_album_id="RTD", sources="aaaaaah")  # a loose copy: supersede's
    items = [kept, earlier, legacy, legacy_no_album, another_album, other_record, spotdl]
    assert release.copies(items, "RTD", RTD, "r2", None) == [earlier, legacy, legacy_no_album]
    assert release.copies(items, "RTD", RTD, None, "r1") == [earlier]


def test_mixed_reasons_name_what_the_track_rules_did() -> None:
    record = {"status": "imported", "blocklist": ["r0"], "rejected_tracks": {"keep": ["s3"]}, "gaps": ["s3"]}
    items = [FakeItem(1, via="usenet", spotify_album_id="RTD"), FakeItem(2, via="spotdl", spotify_album_id="RTD")]
    reasons = release.mixed_reasons("RTD", record, items)
    assert len(reasons) == 4 and "1 spotdl track(s) on the album" in reasons
    whole = [FakeItem(1, via="usenet", spotify_album_id="RTD", album_record="RTD")]
    assert release.mixed_reasons("RTD", record, whole) == []
    assert release.mixed_reasons("RTD", {"status": "fallback", "fallback_from": "failed"}, items) == []


def test_replaced_copies_by_album_and_artist_tags(tmp_path, ffmpeg) -> None:
    mine = _m4a(tmp_path / "3108-05 - Warning.m4a", album="Ready to Die", albumartist="The Notorious B.I.G.",
                artist="The Notorious B.I.G.")
    _m4a(tmp_path / "77-01 - Hypnotize.m4a", album="Life After Death", albumartist="The Notorious B.I.G.",
         artist="The Notorious B.I.G.")
    assert release.replaced_copies(RTD, tmp_path) == [mine]
    assert release.replaced_copies(RTD, tmp_path / "absent") == []


def test_copies_tells_two_imports_of_one_release_apart() -> None:
    """After music-album-reset --reimport the same release may be grabbed again: the old import goes."""
    old = FakeItem(1, album_record="RTD", release="r1@100")
    new = FakeItem(2, album_record="RTD", release="r1@200")
    assert release.copies([old, new], "RTD", RTD, "r1@200", None) == [old]


def test_copies_takes_spotdls_top_ups_but_not_a_loose_copy_another_playlist_lists() -> None:
    """Mellon Collie's 14 tracks spotdl fetched (#272/#205) go with a reset's re-import (#279);
    a spotdl copy aaaaaah also lists stays, for supersede."""
    record = {**RTD, "playlists": {"keep": [], "later": []}}
    topup = FakeItem(1, via="spotdl", spotify_album_id="RTD", sources="keep")
    unlinked = FakeItem(2, via="spotdl", spotify_album_id="RTD", sources="")
    loose = FakeItem(3, via="spotdl", spotify_album_id="RTD", sources="keep,aaaaaah")
    elsewhere = FakeItem(4, via="spotdl", spotify_album_id="OTHER", sources="keep")
    assert release.copies([topup, unlinked, loose, elsewhere], "RTD", record, "r1@1", None) == [topup, unlinked]
    assert release.copies([topup], "RTD", record, None, "r1@1") == []


def test_album_query_falls_back_to_the_key_for_twins() -> None:
    """The audit prints a reset command that selects one album (#289)."""
    records = {
        "std": {"artist": "Los Campesinos!", "name": "Hold on Now, Youngster..."},
        "dlx": {"artist": "Los Campesinos!", "name": "Hold On Now, Youngster... (2018 Deluxe Remaster)"},
        "tw1": {"artist": "Braids", "name": "Euphoric Recall"},
        "tw2": {"artist": "Braids", "name": "Euphoric Recall"},
        "q": {"artist": "Vince Staples", "name": "Summertime '06"},
    }
    assert release.album_query(records, "std", "Los Campesinos! — Hold on Now, Youngster...") == \
        "Los Campesinos! — Hold on Now, Youngster..."
    assert release.album_query(records, "tw2", "Braids — Euphoric Recall") == "tw2"
    assert release.album_query(records, "q", "Vince Staples — Summertime '06") == "Vince Staples — Summertime '\\''06"


class FakeLib:
    def __init__(self, items):
        self.items = items

    def all_items(self):
        return list(self.items)


@pytest.fixture
def quarantined(monkeypatch):
    from music_scan import rollback

    gone: list = []
    monkeypatch.setattr(rollback, "quarantine", lambda items: gone.extend(items) or [])
    return gone


BOYS = {"name": "Boys and Girls in America", "artist": "The Hold Steady", "playlists": {"keep": []}}


def _cd(release_id, n, **f):
    """Track *n* of a release import: ISRC USHS0600n, as MusicBrainz tags it."""
    return FakeItem(100 + n if release_id == "cd@2" else n, via="usenet", album_record="BOYS", release=release_id,
                    isrc=f"USHS0600{n:02d}", title=f"Track {n}", **f)


def test_retire_keeps_the_recordings_a_smaller_edition_lacks_as_extras(quarantined) -> None:
    """Boys and Girls in America (#287): a reimport of the 11-track CD over the
    deluxe retires the 11 tracks the CD holds; the deluxe's bonus tracks and
    demos, and a spotdl top-up the CD doesn't have, stay on the album."""
    deluxe = [_cd("deluxe@1", n) for n in range(1, 18)]
    cd = [_cd("cd@2", n) for n in range(1, 12)]
    # A legacy copy matched by MusicBrainz recording only, and one with no identity at all.
    legacy = {"via": "usenet", "album": "Boys and Girls in America", "albumartist": "The Hold Steady"}
    cd[0]["mb_trackid"] = "rec-1"
    legacy_held = FakeItem(50, title="Track 1", mb_trackid="rec-1", **legacy)
    legacy_unknown = FakeItem(51, title="Track 3", **legacy)  # the same title: not the same recording
    topup = FakeItem(52, via="spotdl", spotify_album_id="BOYS", sources="keep", spotify_ids="SPX",
                     title="Stevie Nix (Live)")
    lib = FakeLib([*deluxe, *cd, legacy_held, legacy_unknown, topup])
    assert release.retire(lib, "BOYS", BOYS, "cd@2", None) == 12
    assert quarantined == [*deluxe[:11], legacy_held]
    extras = [*deluxe[11:], legacy_unknown, topup]
    for item in extras:
        assert item["album_record"] == "BOYS" and item[release.EXTRA] == "1" and item["canon_wait"] == "1"
        assert item.stored == 1
    assert all(not i.get(release.EXTRA) for i in cd)
    # Canon keeps them on the album, and the album is no longer mixed.
    for item in [*cd, *extras]:
        item["spotify_album_id"] = "BOYS"
    assert release.mixed_reasons("BOYS", {"status": "imported", "blocklist": ["x"]}, [*cd, *extras]) == []


def test_retire_takes_every_copy_a_same_edition_reimport_holds(quarantined) -> None:
    old = [_cd("deluxe@1", n) for n in range(1, 12)]
    new = [_cd("cd@2", n) for n in range(1, 12)]
    topup = FakeItem(52, via="spotdl", spotify_album_id="BOYS", sources="keep", isrc="USHS060005")
    assert release.retire(FakeLib([*old, *new, topup]), "BOYS", BOYS, "cd@2", None) == 12
    assert quarantined == [*old, topup]
    assert not any(i.get(release.EXTRA) for i in [*old, *new])


def test_retire_extras_go_once_a_later_release_holds_them(quarantined) -> None:
    extra = _cd("deluxe@1", 12, album_extra="1")
    again = [_cd("cd@2", n) for n in range(1, 12)] + [FakeItem(200, album_record="BOYS", release="cd@2",
                                                              isrc="USHS060012")]
    assert release.retire(FakeLib([extra, *again]), "BOYS", BOYS, "cd@2", None) == 1
    assert quarantined == [extra]


def test_retire_with_drop_takes_the_whole_release(quarantined) -> None:
    """A rolled-back release (another album) goes whole: its files are the wrong ones."""
    kept = [_cd("deluxe@1", n) for n in range(1, 3)]
    wrong = [FakeItem(9, album_record="BOYS", release="wrong@3", isrc="XX0000000001")]
    assert release.retire(FakeLib([*kept, *wrong]), "BOYS", BOYS, None, "wrong@3") == 1
    assert quarantined == wrong and not wrong[0].get(release.EXTRA)


def test_split_replaced_deletes_only_what_the_library_holds(tmp_path, ffmpeg) -> None:
    """--replaced (#287): a mix of superseded files and only copies."""
    from music_scan.identity import Recordings

    by_isrc = _m4a(tmp_path / "1-01 - Stuck Between Stations.m4a", isrc="USHS06000001")
    by_mbid = _m4a(tmp_path / "2-02 - Chips Ahoy!.m4a", mb_trackid="rec-2")
    bonus = _m4a(tmp_path / "3-12 - Girls Like Status.m4a", isrc="USHS06000012", mb_trackid="rec-12")
    bare = _m4a(tmp_path / "4-03 - Hot Soft Light.m4a", title="Hot Soft Light")  # the title alone is no identity
    library = Recordings([FakeItem(1, isrc="USHS06000001"), FakeItem(2, mb_trackid="rec-2"),
                          FakeItem(3, title="Hot Soft Light")])
    held, only = release.split_replaced([by_isrc, by_mbid, bonus, bare, tmp_path / "gone.m4a"], library)
    assert held == [by_isrc, by_mbid]
    assert only == [bonus, bare, tmp_path / "gone.m4a"]


def test_purge_replaced_deletes_superseded_files_after_the_retention_only(tmp_path, ffmpeg) -> None:
    """#288: a superseded file goes once it has sat 14 days in quarantine/replaced/; an only copy never."""
    import time

    from music_scan.identity import Recordings

    superseded = _m4a(tmp_path / "1-01 - Stuck Between Stations.m4a", isrc="USHS06000001")
    only = _m4a(tmp_path / "3-12 - Girls Like Status.m4a", isrc="USHS06000012")
    library = Recordings([FakeItem(1, isrc="USHS06000001")])
    assert release.purge_replaced(tmp_path, library, 14) == {"superseded": 1, "only_copy": 1, "purged": 0}
    assert superseded.exists()
    assert release.purge_replaced(tmp_path, library, 0, now=time.time() + 30 * 86400)["purged"] == 0
    later = time.time() + 15 * 86400
    assert release.purge_replaced(tmp_path, library, 14, now=later) == {"superseded": 0, "only_copy": 1, "purged": 1}
    assert not superseded.exists() and only.exists()
    assert release.purge_replaced(tmp_path / "absent", library, 14) == {"superseded": 0, "only_copy": 0,
                                                                         "purged": 0}


def test_retention_days_setting(monkeypatch) -> None:
    assert release.retention_days() == 14
    monkeypatch.setenv("REPLACED_RETENTION_DAYS", "0")
    assert release.retention_days() == 0
    monkeypatch.setenv("REPLACED_RETENTION_DAYS", "soon")
    assert release.retention_days() == 14
