"""What an album import's job leaves in quarantine/usenet/ (homelab#2667)."""

import logging
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from music_scan import usenet_quarantine as uq

DAY = 86400


class FakeItem(dict):
    def __init__(self, id_=1, **fields):
        super().__init__(**fields)
        self.id = id_

    def get(self, key, default=None):
        return super().get(key, default)


@pytest.fixture
def ffmpeg():
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")


@pytest.fixture
def recorded(monkeypatch):
    calls: list = []
    monkeypatch.setattr(uq.events, "record", lambda kind, message, **kw: calls.append((kind, message, kw)))
    return calls


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


def _bytes(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _imported(job: str, playlist: str = "soft", **fields) -> FakeItem:
    return FakeItem(1, via="usenet", usenet_job=job, sources=f"{playlist},other", **fields)


def test_purge_deletes_an_imported_jobs_junk_and_superseded_files_after_the_retention_only(
        tmp_path, ffmpeg, recorded) -> None:
    job = tmp_path / "soft" / "Artist-Album-FLAC-2023"
    held = _m4a(job / "01-held.m4a", isrc="USHS06000001", title="Held", artist="Artist")
    only = _m4a(job / "CD2" / "02-only.m4a", isrc="USHS06000002")
    untagged = _m4a(job / "03-untagged.m4a")
    empty = _bytes(job / "04-empty.flac", b"")
    stub = _bytes(job / "Artist.-.Album._2023_.Mp3", b"not audio\n" * 115)
    items = [_imported("Artist-Album-FLAC-2023", isrc="USHS06000001")]

    assert uq.purge(tmp_path, items, 14) == {"junk": 2, "superseded": 1, "only_copy": 2, "not_imported": 0,
                                            "bytes": sum(f.stat().st_size for f in (held, only, untagged, empty, stub)),
                                            "purged": 0}
    assert held.exists() and empty.exists() and stub.exists() and not recorded

    counts = uq.purge(tmp_path, items, 14, now=time.time() + 15 * DAY)
    assert counts["purged"] == 3 and counts["only_copy"] == 2 and counts["junk"] == counts["superseded"] == 0
    assert not held.exists() and not empty.exists() and not stub.exists()
    # Only copies stay, whatever their age: no ID says the library holds them.
    assert only.exists() and untagged.exists()
    kinds = sorted((kw["data"]["why"], kw["data"]["path"]) for kind, _, kw in recorded if kind == "purged")
    assert kinds == sorted([("superseded", str(held)), ("junk", str(empty)), ("junk", str(stub))])
    (_, message, kw), = [c for c in recorded if c[2]["data"]["path"] == str(held)]
    assert message.startswith(f"[PURGE] {held}: superseded (the library holds its recording)")
    assert kw["data"]["artist"] == "Artist" and kw["data"]["title"] == "Held" and kw["data"]["days"] == 14
    assert "item_id" not in kw  # a file in quarantine/usenet/ was never an item


def test_purge_keeps_a_job_no_library_item_came_from_whole(tmp_path, ffmpeg, recorded) -> None:
    """The album's last failed release (kept for review), or a job from before usenet_job: never deleted."""
    job = tmp_path / "soft" / "Failed-Release"
    held = _m4a(job / "01.m4a", isrc="USHS06000001")
    empty = _bytes(job / "02.flac", b"")
    loose = _bytes(tmp_path / "soft" / "loose.flac", b"")
    items = [FakeItem(1, isrc="USHS06000001", via="usenet", usenet_job="Another-Release", sources="soft"),
             # The same job name, but imported for another playlist.
             FakeItem(2, via="usenet", usenet_job="Failed-Release", sources="chill"),
             FakeItem(3, via="spotdl", usenet_job="Failed-Release", sources="soft")]

    counts = uq.purge(tmp_path, items, 14, now=time.time() + 400 * DAY)
    assert counts["purged"] == 0 and counts["not_imported"] == 3
    assert held.exists() and empty.exists() and loose.exists() and not recorded


def test_purge_removes_the_folders_it_empties(tmp_path, ffmpeg, recorded) -> None:
    job = tmp_path / "soft" / "Job"
    _bytes(job / "CD1" / "01.flac", b"")
    _m4a(job / "CD2" / "01.m4a", isrc="USHS06000001")
    kept = _bytes(tmp_path / "chill" / "Other" / "01.flac", b"")
    items = [_imported("Job", isrc="USHS06000001")]

    assert uq.purge(tmp_path, items, 14, now=time.time() + 15 * DAY)["purged"] == 2
    assert not (tmp_path / "soft").exists()
    assert tmp_path.is_dir() and kept.exists()


def test_purge_with_retention_zero_deletes_nothing(tmp_path, recorded) -> None:
    empty = _bytes(tmp_path / "soft" / "Job" / "01.flac", b"")
    assert uq.purge(tmp_path, [_imported("Job")], 0, now=time.time() + 400 * DAY)["purged"] == 0
    assert empty.exists()


def test_purge_of_an_absent_folder_counts_nothing(tmp_path) -> None:
    assert uq.purge(tmp_path / "absent", [], 14) == {"junk": 0, "superseded": 0, "only_copy": 0,
                                                     "not_imported": 0, "bytes": 0, "purged": 0}


@pytest.mark.parametrize(("size", "tags", "junk"), [
    (0, None, "empty"),
    (1158, None, "unreadable, 1158 bytes"),
    (77297, SimpleNamespace(length=0.0), "no length, 77297 bytes"),
    (8192, SimpleNamespace(length=240.0), "truncated: 8192 bytes for 240 s"),
    # Unreadable but large: maybe audio a tag reader can't open, so not junk.
    (2 << 20, None, ""),
    (17000, SimpleNamespace(length=1.0), ""),
    (8_000_000, SimpleNamespace(length=240.0), ""),
])
def test_junk_is_a_file_with_no_audio(size, tags, junk) -> None:
    assert uq._junk(Path("x.flac"), size, tags) == junk


def test_report_lists_every_file_and_deletes_nothing(tmp_path, ffmpeg, caplog) -> None:
    job = tmp_path / "soft" / "Job"
    held = _m4a(job / "01-held.m4a", isrc="USHS06000001")
    only = _m4a(job / "02-only.m4a", isrc="USHS06000002")
    empty = _bytes(job / "03-empty.flac", b"")
    failed = _bytes(tmp_path / "soft" / "Failed" / "01.flac", b"")
    items = [_imported("Job", isrc="USHS06000001")]

    with caplog.at_level(logging.INFO, logger=uq.logger.name):
        counts = uq.report(tmp_path, items, 14)
    assert held.exists() and only.exists() and empty.exists() and failed.exists()
    assert counts["due"] == 0 and counts["later"] == 2 and counts["kept"] == 2
    assert counts["only_copy"] == 1 and counts["not_imported"] == 1
    assert "[JOB] soft/Failed: 1 file(s), 0.0 MiB; no library item came from this job: kept whole" in caplog.text
    assert "[JOB] soft/Job: 3 file(s)" in caplog.text
    assert "[SUPERSEDED] soft/Job/01-held.m4a: the library holds its recording; the scan deletes it in 14 day(s)" \
        in caplog.text
    assert "[ONLYCOPY] soft/Job/02-only.m4a: no library item holds its recording; kept" in caplog.text
    assert "[JUNK] soft/Job/03-empty.flac: empty; the scan deletes it in 14 day(s)" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.INFO, logger=uq.logger.name):
        later = uq.report(tmp_path, items, 14, now=time.time() + 15 * DAY)
    assert later["due"] == 2 and later["later"] == 0
    assert "[JUNK] soft/Job/03-empty.flac: empty; the next scan deletes it" in caplog.text
    assert "The scan deletes 2 (" in caplog.text and "it keeps 1 only copies" in caplog.text
