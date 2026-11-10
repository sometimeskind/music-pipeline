"""A scan cut short keeps its files (homelab#2763).

A restart's SIGTERM reached the scan while ``beet import`` ran.  beets exited
early, the scan read that as a finished import, quarantined every inbox file
beets never reached and staged them for the as-is pass in the container's
``/tmp``, and the files went with the container.  These tests run the real
``run_beet_import`` against a stand-in ``beet`` and send the process a real
SIGTERM, and kill a real as-is pass partway.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import unittest.mock as mock
from collections import Counter
from pathlib import Path

import pytest

from music_scan import process, scan

# A stand-in for beets: "imports" (moves to $FAKE_LIBRARY) the first audio
# file under the folder it is handed, says so in $FAKE_MARKER, then does what
# $FAKE_BEET says: waits to be stopped, kills itself, or logs skips.
FAKE_BEET = '''\
import os, signal, sys, time
from pathlib import Path
root = Path(sys.argv[-1])
files = sorted(f for f in root.rglob("*") if f.is_file())
if files:
    lib = Path(os.environ["FAKE_LIBRARY"])
    lib.mkdir(parents=True, exist_ok=True)
    files[0].rename(lib / files[0].name)
mode = os.environ["FAKE_BEET"]
if mode == "skip":
    with open(os.environ["FAKE_IMPORT_LOG"], "a") as log:
        log.write("skip " + str(root) + "\\n")
Path(os.environ["FAKE_MARKER"]).touch()
if mode.startswith("kill-"):
    os.kill(os.getpid(), getattr(signal, mode[len("kill-"):]))
time.sleep(60)
'''

TRACKS = ["01.m4a", "02.m4a", "03.m4a", "04.m4a", "05.m4a"]


@pytest.fixture
def no_shutdown():
    """The shutdown flag is process-wide: clear it after the test that set it.
    And a no-op SIGTERM handler under the test, so a stray signal never stops pytest."""
    previous = signal.signal(signal.SIGTERM, _ignore)
    yield
    signal.signal(signal.SIGTERM, previous)
    process._shutdown.clear()


def _ignore(*_args) -> None:
    pass


@pytest.fixture
def fake_beet(tmp_path, monkeypatch):
    """Put the stand-in ``beet`` first on PATH; returns a function setting its mode."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    beet = bin_dir / "beet"
    beet.write_text(f"#!{sys.executable}\n{FAKE_BEET}")
    beet.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LIBRARY", str(tmp_path / "library"))
    monkeypatch.setenv("FAKE_MARKER", str(tmp_path / "beet-ran"))
    import_log = tmp_path / "import.log"
    monkeypatch.setenv("FAKE_IMPORT_LOG", str(import_log))
    monkeypatch.setattr(process, "IMPORT_LOG", import_log)

    def mode(name: str) -> Path:
        monkeypatch.setenv("FAKE_BEET", name)
        return tmp_path / "beet-ran"

    return mode


def _sigterm_once_beets_runs(marker: Path) -> threading.Thread:
    """SIGTERM this process, as the runner does to a flow run at a pod's
    shutdown, once the stand-in has imported its file and
    ``_forward_sigterm`` is in place (the handler is no longer the test's)."""

    def send() -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if marker.exists() and signal.getsignal(signal.SIGTERM) is not _ignore:
                os.kill(os.getpid(), signal.SIGTERM)
                return
            time.sleep(0.02)

    thread = threading.Thread(target=send, daemon=True)
    thread.start()
    return thread


def _inbox(tmp_path: Path) -> Path:
    inbox = tmp_path / "inbox"
    batch = inbox / "spotdl" / "p"
    batch.mkdir(parents=True)
    for name in TRACKS:
        (batch / name).write_bytes(name.encode())
    return inbox


def _audio(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*.m4a")) if root.exists() else []


def _run_scan(tmp_path: Path, inbox: Path, raises: type[BaseException] | None = None):
    """``scan.run()`` over *inbox*, with the real import, quarantine and as-is
    pass, expecting it to raise *raises*; returns its metrics and the mocks of
    the steps after the import."""
    captured: list = []

    class FakeMetrics(scan.ScanMetrics):
        def __init__(self):
            super().__init__()
            captured.append(self)

        def push(self):
            pass

    lib = mock.MagicMock()
    lib.return_value.__enter__.return_value.items_added_since.return_value = []
    with mock.patch.object(scan, "MusicLibrary", lib), \
         mock.patch.object(scan, "check_request_downloads", return_value=Counter()), \
         mock.patch.object(scan, "INBOX", inbox), \
         mock.patch.object(scan, "SPOTDL_DIR", inbox / "spotdl"), \
         mock.patch.object(scan, "QUARANTINE", tmp_path / "quarantine"), \
         mock.patch.object(scan, "ScanMetrics", FakeMetrics), \
         mock.patch.object(scan, "count_lossless_items", return_value=0), \
         mock.patch.object(scan, "run_beet_update"), \
         mock.patch.object(scan, "regen_playlists") as regen, \
         mock.patch.object(scan, "trigger_scan"), \
         mock.patch.object(scan, "quarantine_inbox_leftovers", wraps=scan.quarantine_inbox_leftovers) as leftovers, \
         mock.patch.object(scan, "import_asis_from_quarantine", wraps=scan.import_asis_from_quarantine) as asis:
        if raises is None:
            scan.run()
        else:
            with pytest.raises(raises):
                scan.run()
    return captured[0], leftovers, asis, regen


def test_a_sigterm_during_the_inbox_import_leaves_the_rest_in_the_inbox(tmp_path, fake_beet, no_shutdown, caplog):
    """The bug: beets stopped by the forwarded SIGTERM read as a finished import."""
    inbox = _inbox(tmp_path)
    sender = _sigterm_once_beets_runs(fake_beet("wait"))

    metrics, leftovers, asis, regen = _run_scan(tmp_path, inbox, process.ImportInterrupted)
    sender.join(5)

    assert process.shutdown_requested()
    # beets imported one; the four it never reached are still in the inbox.
    assert _audio(tmp_path / "library") == ["01.m4a"]
    assert _audio(inbox) == [f"spotdl/p/{n}" for n in TRACKS[1:]]
    assert _audio(tmp_path / "quarantine") == []
    leftovers.assert_not_called()
    asis.assert_not_called()
    regen.assert_not_called()
    assert metrics.success is False and metrics.failure_reason == scan.INTERRUPTED
    assert "4 of 5 inbox file(s) stay in the inbox" in caplog.text
    # And nothing quarantines or stages for the rest of this process's life.
    with mock.patch.object(scan, "INBOX", inbox), mock.patch.object(scan, "QUARANTINE", tmp_path / "quarantine"):
        with pytest.raises(process.ImportInterrupted):
            scan.quarantine_inbox_leftovers()
        with pytest.raises(process.ImportInterrupted):
            scan.import_asis_from_quarantine()
        with pytest.raises(process.ImportInterrupted):
            process.run_beet_import(inbox)
    assert _audio(inbox) == [f"spotdl/p/{n}" for n in TRACKS[1:]]


@pytest.mark.parametrize("sig", ["SIGKILL", "SIGTERM"])
def test_a_beet_import_killed_by_a_signal_leaves_the_rest_in_the_inbox(tmp_path, fake_beet, no_shutdown, sig):
    """A SIGTERM from anywhere but the skip limit, or any other signal: exit -15
    passed as the skip limit's before."""
    inbox = _inbox(tmp_path)
    fake_beet(f"kill-{sig}")

    metrics, leftovers, asis, _ = _run_scan(tmp_path, inbox, subprocess.CalledProcessError)

    assert not process.shutdown_requested()
    assert _audio(inbox) == [f"spotdl/p/{n}" for n in TRACKS[1:]]
    assert _audio(tmp_path / "quarantine") == []
    leftovers.assert_not_called()
    asis.assert_not_called()
    assert metrics.success is False and metrics.failure_reason == "unexpected_error"


def test_the_skip_limits_own_sigterm_is_still_a_finished_import(tmp_path, fake_beet, no_shutdown):
    """BEET_SKIP_LIMIT's early stop (exit -15) is not an interruption."""
    inbox = _inbox(tmp_path)
    fake_beet("skip")

    assert process.run_beet_import(inbox, skip_limit=1) == 0
    assert not process.shutdown_requested()


def test_a_normal_scan_still_quarantines_its_leftovers_and_runs_the_as_is_pass(tmp_path, monkeypatch):
    inbox = _inbox(tmp_path)
    quarantine = tmp_path / "quarantine"
    library = tmp_path / "library"
    handed: list = []

    def beet_import(root, skip_limit=None, asis=False, config=None):
        handed.append((root, asis, _audio(root)))
        if not asis:  # beets matches the first file only
            library.mkdir(exist_ok=True)
            (root / "spotdl/p/01.m4a").rename(library / "01.m4a")
        return 0

    monkeypatch.setattr(scan, "run_beet_import", beet_import)
    with mock.patch("music_scan.asis_apart.eligible", lambda q: sorted(q.rglob("*.m4a"))), \
         mock.patch("music_scan.asis_apart.move_apart", return_value=[]):
        metrics, leftovers, asis, regen = _run_scan(tmp_path, inbox)

    staging = tmp_path / "asis-staging"
    assert handed == [
        (inbox, False, [f"spotdl/p/{n}" for n in TRACKS]),
        # Staged beside quarantine/, on its volume, not in /tmp.
        (staging, True, [f"spotdl/p/{n}" for n in TRACKS[1:]]),
    ]
    leftovers.assert_called_once()
    asis.assert_called_once()
    regen.assert_called_once()
    # beets took none as-is: all four are back in quarantine, and staging is empty.
    assert _audio(quarantine) == [f"spotdl/p/{n}" for n in TRACKS[1:]]
    assert _audio(staging) == [] and not any(staging.iterdir())
    assert _audio(inbox) == []
    assert metrics.success is True


# The as-is pass in a process of its own, killed (SIGKILL) while beets runs:
# beets imports the first staged file, the apart run has one other recording.
KILLED_PASS = '''\
import os, signal, sys
from pathlib import Path
from unittest import mock
from music_scan import asis_apart, scan

quarantine = Path(sys.argv[1])

def move_apart(staging, apart, lib):
    f = staging / "spotdl/p/05.m4a"
    (apart / "spotdl/p").mkdir(parents=True)
    f.rename(apart / "spotdl/p/05.m4a")
    return [asis_apart.Verdict(asis_apart.SPLIT)]

def killed(root, asis=False, config=None, **_):
    sorted(root.rglob("*.m4a"))[0].unlink()
    os.kill(os.getpid(), signal.SIGKILL)

with mock.patch.object(scan, "QUARANTINE", quarantine), \\
     mock.patch.object(asis_apart, "eligible", lambda q: sorted(q.rglob("*.m4a"))), \\
     mock.patch.object(asis_apart, "move_apart", side_effect=move_apart), \\
     mock.patch.object(scan, "MusicLibrary"), \\
     mock.patch.object(scan, "run_beet_import", side_effect=killed):
    scan.import_asis_from_quarantine()
'''


def test_an_as_is_pass_killed_partway_leaves_its_files_staged_and_startup_puts_every_one_back(tmp_path, monkeypatch):
    quarantine = tmp_path / "quarantine"
    (quarantine / "spotdl/p").mkdir(parents=True)
    for name in TRACKS:
        (quarantine / "spotdl/p" / name).write_bytes(name.encode())

    killed = subprocess.run([sys.executable, "-c", KILLED_PASS, str(quarantine)], capture_output=True, text=True)

    assert killed.returncode == -signal.SIGKILL, killed.stderr
    staging, apart = tmp_path / "asis-staging", tmp_path / "asis-apart"
    # Nothing back in quarantine: no finally ran.  All on the volume, none lost.
    assert _audio(quarantine) == []
    assert _audio(staging) == ["spotdl/p/02.m4a", "spotdl/p/03.m4a", "spotdl/p/04.m4a"]
    assert _audio(apart) == ["spotdl/p/05.m4a"]

    # Before the restart, a new download quarantined under one of their names.
    (quarantine / "spotdl/p/03.m4a").write_bytes(b"newer")
    monkeypatch.setattr(scan, "QUARANTINE", quarantine)

    assert scan.restore_asis_staging() == (4, 1)

    assert _audio(quarantine) == ["spotdl/p/02.m4a", "spotdl/p/03 (restored).m4a", "spotdl/p/03.m4a",
                                  "spotdl/p/04.m4a", "spotdl/p/05.m4a"]
    assert (quarantine / "spotdl/p/03.m4a").read_bytes() == b"newer"
    assert (quarantine / "spotdl/p/03 (restored).m4a").read_bytes() == b"03.m4a"
    assert (quarantine / "spotdl/p/05.m4a").read_bytes() == b"05.m4a"
    assert _audio(staging) == [] and _audio(apart) == []
    # Idempotent: a second run, or one with the dirs gone, moves nothing.
    assert scan.restore_asis_staging() == (0, 0)
    for d in (staging, apart):
        d.rmdir()
    assert scan.restore_asis_staging() == (0, 0)


def test_a_restored_name_that_is_taken_too_gets_the_next_one(tmp_path, monkeypatch):
    quarantine, staging = tmp_path / "quarantine", tmp_path / "asis-staging"
    (staging / "spotdl/p").mkdir(parents=True)
    (staging / "spotdl/p/a.m4a").write_bytes(b"staged")
    (quarantine / "spotdl/p").mkdir(parents=True)
    (quarantine / "spotdl/p/a.m4a").write_bytes(b"one")
    (quarantine / "spotdl/p/a (restored).m4a").write_bytes(b"two")
    monkeypatch.setattr(scan, "QUARANTINE", quarantine)

    assert scan.restore_asis_staging() == (1, 1)
    assert (quarantine / "spotdl/p/a (restored 2).m4a").read_bytes() == b"staged"
    assert (quarantine / "spotdl/p/a.m4a").read_bytes() == b"one"


def test_a_shutdown_during_the_as_is_import_puts_the_staged_files_back(tmp_path, monkeypatch, no_shutdown):
    quarantine = tmp_path / "quarantine"
    (quarantine / "spotdl/p").mkdir(parents=True)
    for name in TRACKS:
        (quarantine / "spotdl/p" / name).write_bytes(name.encode())
    monkeypatch.setattr(scan, "QUARANTINE", quarantine)

    def interrupted(root, **_):
        process._shutdown.set()
        raise process.ImportInterrupted("beet import stopped by a shutdown signal")

    with mock.patch("music_scan.asis_apart.eligible", lambda q: sorted(q.rglob("*.m4a"))), \
         mock.patch("music_scan.asis_apart.move_apart", return_value=[]), \
         mock.patch.object(scan, "MusicLibrary"), \
         mock.patch.object(scan, "run_beet_import", side_effect=interrupted):
        with pytest.raises(process.ImportInterrupted):
            scan.import_asis_from_quarantine()

    assert _audio(quarantine) == [f"spotdl/p/{n}" for n in TRACKS]
    assert _audio(tmp_path / "asis-staging") == []


def test_a_pass_returns_what_an_earlier_one_left_staged_first(tmp_path, monkeypatch, caplog):
    quarantine, staging = tmp_path / "quarantine", tmp_path / "asis-staging"
    (staging / "spotdl/p").mkdir(parents=True)
    (staging / "spotdl/p/left.m4a").write_bytes(b"x")
    monkeypatch.setattr(scan, "QUARANTINE", quarantine)
    seen: list = []

    with mock.patch("music_scan.asis_apart.eligible", lambda q: sorted(q.rglob("*.m4a"))), \
         mock.patch("music_scan.asis_apart.move_apart", return_value=[]), \
         mock.patch.object(scan, "MusicLibrary"), \
         mock.patch.object(scan, "run_beet_import", side_effect=lambda root, **_: seen.append(_audio(root))):
        scan.import_asis_from_quarantine()

    # Staged again through quarantine like any other file, then back.
    assert seen == [["spotdl/p/left.m4a"]]
    assert _audio(quarantine) == ["spotdl/p/left.m4a"]
    assert "[RESUME] 1 as-is file(s)" in caplog.text


def test_the_staging_dirs_are_beside_quarantine_and_where_the_plugin_looks():
    """Same volume as quarantine/ (renames), outside its tree (the purge, the
    audits and list_quarantine walk it), and the plugin finds a staged file's
    playlist there."""
    from music_scan.music_pipeline import ASIS_STAGING_ROOTS, _playlist_from_path

    staging, apart = scan.asis_dirs()
    assert (staging, apart) == ASIS_STAGING_ROOTS
    assert staging.parent == apart.parent == scan.QUARANTINE.parent
    assert not staging.is_relative_to(scan.QUARANTINE) and not apart.is_relative_to(scan.QUARANTINE)
    for root in ASIS_STAGING_ROOTS:
        assert _playlist_from_path(str(root / "spotdl/my-playlist/a.m4a")) == "my-playlist"
        assert _playlist_from_path(str(root / "loose.m4a")) is None
