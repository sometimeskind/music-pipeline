"""A file libchromaprint can't take in-process doesn't abort the import (#517).

pyacoustid fingerprints with libchromaprint in the calling process when it
loads, fed by audioread, and a 3- or 6-channel file trips its assertion
(``length % m_num_channels == 0``): SIGABRT, and in ``beet import`` the whole
import died at every scan, so the inbox never drained.  These tests build such
a file with ffmpeg and run real beets on it, with the plugin, which routes
every fingerprint through ``fpcalc`` (:func:`music_scan.identity.use_fpcalc`).
Anything that might abort runs in a child process, never in pytest's.
"""

from __future__ import annotations

import re
import shutil
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from music_scan import music_pipeline, process, scan

needs_tools = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("fpcalc") and shutil.which("beet")),
    reason="ffmpeg, fpcalc or beet not installed (the bare CI runner has no fpcalc); the dev image has all three",
)

# A 3.0 layout: three channels, so audioread's 8192-byte blocks (4096 samples) never hold whole frames.
THREE_CHANNELS = "pan=3.0|c0=c0|c1=c0|c2=c0"
STEREO = "pan=stereo|c0=c0|c1=c0"


def _m4a(path: Path, layout: str, title: str, track: int) -> Path:
    """A 20 s tone in *layout*, tagged well enough for an as-is import."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
         "-af", layout, "-c:a", "aac",
         "-metadata", f"title={title}", "-metadata", "artist=Test Artist", "-metadata", "album=Test Album",
         "-metadata", f"track={track}", str(path)],
        check=True,
    )
    return path


def _child(code: str, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code), *args], capture_output=True, text=True,
                          timeout=120, env=env)


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    """A playlist's batch: one 3-channel file between two stereo ones."""
    batch = tmp_path / "inbox" / "spotdl" / "p"
    _m4a(batch / "01 - First.m4a", STEREO, "First", 1)
    _m4a(batch / "02 - Three.m4a", THREE_CHANNELS, "Three", 2)
    _m4a(batch / "03 - Third.m4a", STEREO, "Third", 3)
    return tmp_path / "inbox"


@pytest.fixture
def beets(tmp_path: Path, monkeypatch) -> Path:
    """A beets home for an as-is import with the plugin alone (no network), as
    the integration tests' ``beets_asis_config``; returns the library database."""
    home = tmp_path / "beets"
    home.mkdir()
    db = home / "library.db"
    (home / "config.yaml").write_text(textwrap.dedent(f"""\
        directory: {tmp_path / "library"}
        library: {db}
        import:
          quiet: yes
          move: yes
          singletons: yes
          autotag: no
          log: {home / "import.log"}
        pluginpath:
          - {Path(music_pipeline.__file__).parent}
        plugins: music_pipeline
        """))
    monkeypatch.setenv("BEETSDIR", str(home))
    monkeypatch.setattr(scan, "INBOX", tmp_path / "inbox")
    monkeypatch.setattr(scan, "LIBRARY_DB", db)
    monkeypatch.setattr(scan, "QUARANTINE", tmp_path / "quarantine")
    monkeypatch.setattr(process, "IMPORT_LOG", home / "import.log")
    return db


def _fingerprints(db: Path) -> dict[str, str | None]:
    """``{title: acoustid_fingerprint}`` of the library's items."""
    from beets.library import Library

    lib = Library(str(db))
    try:
        return {item.title: item.get("acoustid_fingerprint") for item in lib.items()}
    finally:
        lib._close()


@needs_tools
def test_libchromaprint_in_process_aborts_on_a_three_channel_file(tmp_path) -> None:
    """The bug: pyacoustid's default path takes the process down.  If this
    fails, libchromaprint no longer aborts on such a file, and the fpcalc
    routing guards against nothing this test can show."""
    path = _m4a(tmp_path / "three.m4a", THREE_CHANNELS, "Three", 1)
    done = _child("""
        import sys, acoustid
        assert acoustid.have_chromaprint and acoustid.have_audioread
        acoustid.fingerprint_file(sys.argv[1])
        """, str(path))
    assert done.returncode == -signal.SIGABRT, done.stderr
    assert "length % m_num_channels == 0" in done.stderr


@needs_tools
def test_fingerprint_runs_fpcalc(tmp_path) -> None:
    """``identity.fingerprint`` (the plugin's fingerprint_if_missing, the
    audits, restore) fingerprints the file instead of aborting."""
    path = _m4a(tmp_path / "three.m4a", THREE_CHANNELS, "Three", 1)
    done = _child("""
        import sys
        from music_scan.identity import fingerprint
        print(fingerprint(sys.argv[1]))
        """, str(path))
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip()


@needs_tools
def test_the_plugin_sends_chroma_through_fpcalc(tmp_path) -> None:
    """In the beets process ``chroma`` calls pyacoustid itself, at
    ``import_task_start`` of every autotagged import: once the plugin is
    loaded, its fingerprint is fpcalc's.  The AcoustID lookup after it is
    refused here, as offline."""
    path = _m4a(tmp_path / "three.m4a", THREE_CHANNELS, "Three", 1)
    done = _child("""
        import sys
        import acoustid
        import beetsplug.chroma as chroma
        from beets import logging
        from music_scan.music_pipeline import MusicPipelinePlugin

        MusicPipelinePlugin()

        def offline(*args, **kwargs):
            raise acoustid.WebServiceError("offline")

        acoustid.lookup = offline
        path = sys.argv[1].encode()
        chroma.acoustid_match(logging.getLogger("beets"), path)
        print(chroma._fingerprints[path])
        """, str(path))
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip()


@needs_tools
def test_a_three_channel_file_imports_with_the_rest_of_the_inbox(inbox, beets) -> None:
    """The scan's own import, real beets: the 3-channel file no longer kills
    ``beet import`` with signal 6, and the whole batch imports, each item with
    its fingerprint."""
    imported = scan.run_inbox_import()

    assert sorted(title for title, _ in imported) == ["First", "Third", "Three"]
    assert all(_fingerprints(beets).values()), _fingerprints(beets)
    assert scan.quarantine_inbox_leftovers(inbox) == 0


@needs_tools
def test_a_fingerprint_that_crashes_costs_only_that_fingerprint(inbox, beets, tmp_path, monkeypatch) -> None:
    """Whatever else crashes the fingerprinter now crashes fpcalc, a child of
    beets: pyacoustid reads its exit as a ``FingerprintGenerationError``, the
    plugin logs it, and the import goes on.  The file imports without a
    fingerprint (an autotagged one that MusicBrainz can't match goes to
    quarantine as any other), and the rest of the inbox imports."""
    fake = tmp_path / "bin" / "fpcalc"
    fake.parent.mkdir()
    fake.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        case "$*" in
          *Three*) kill -ABRT $$ ;;
        esac
        exec {shutil.which("fpcalc")} "$@"
        """))
    fake.chmod(0o755)
    monkeypatch.setenv("FPCALC", str(fake))

    imported = scan.run_inbox_import()

    assert sorted(title for title, _ in imported) == ["First", "Third", "Three"]
    fingerprints = _fingerprints(beets)
    assert not fingerprints["Three"]
    assert fingerprints["First"] and fingerprints["Third"]


def test_every_pyacoustid_call_in_the_packages_forces_fpcalc() -> None:
    """A fingerprint call that leaves pyacoustid to choose runs libchromaprint in
    the calling process, which the next 3-channel file aborts: every call in the
    packages passes ``force_fpcalc=True`` (beets' ``chroma`` is covered by
    :func:`~music_scan.identity.use_fpcalc`)."""
    import music_fetch
    import music_scan
    import music_service

    call = re.compile(r"\bacoustid\.(?:fingerprint_file|match)\(")
    calls = [
        (path.name, line.strip())
        for package in (music_fetch, music_scan, music_service)
        for path in sorted(Path(package.__file__).parent.rglob("*.py"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if call.search(line)
    ]
    assert len(calls) >= 3, calls  # identity, guard, mb_fingerprint: the scan found the sources
    assert [c for c in calls if "force_fpcalc=True" not in c[1]] == []
