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
