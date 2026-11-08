"""Tests for the inbox file watcher in music_service.cli."""

from __future__ import annotations

import threading
import time

import pytest

from music_service.cli import _start_file_watcher


@pytest.fixture
def watch(tmp_path, monkeypatch):
    """Start the watcher on a temp inbox; yields the inbox and an event set on each callback."""
    monkeypatch.setenv("MUSIC_INBOX", str(tmp_path))
    fired = threading.Event()
    observer = _start_file_watcher(fired.set)
    time.sleep(0.2)
    yield tmp_path, fired
    observer.stop()
    observer.join()


def test_new_audio_file_fires(watch):
    inbox, fired = watch
    (inbox / "track.m4a").write_bytes(b"x")
    assert fired.wait(2)


def test_audio_renamed_into_place_fires(watch):
    """rclone (homelab#2698) writes a .partial and renames it when complete."""
    inbox, fired = watch
    partial = inbox / "track.m4a.abc123.partial"
    partial.write_bytes(b"x")
    assert not fired.wait(0.5)
    partial.rename(inbox / "track.m4a")
    assert fired.wait(2)


def test_rename_to_other_file_does_not_fire(watch):
    inbox, fired = watch
    (inbox / "notes.txt").write_bytes(b"x")
    (inbox / "notes.txt").rename(inbox / "notes.json")
    assert not fired.wait(0.5)
