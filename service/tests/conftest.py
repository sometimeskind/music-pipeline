"""Fixtures for every service test."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _slots_dir(tmp_path_factory, monkeypatch):
    """Slot holder records (homelab#2841) go to the test's own directory, not
    the pod's ``/tmp/music-slots``: a test's hold never names another test's."""
    from music_service import slots

    monkeypatch.setattr(slots, "SLOTS_DIR", tmp_path_factory.mktemp("music-slots"))
