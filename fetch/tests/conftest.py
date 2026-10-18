"""Shared fixtures for the fetch tests."""

from __future__ import annotations

import unittest.mock as mock
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _no_pins(tmp_path: Path):
    """Keep every download batch off the pod's track-pins.conf (#247): CI runs
    as a user who can't even stat /root."""
    with mock.patch("music_fetch.spotdl_ops.PINS_FILE", tmp_path / "track-pins.conf"):
        yield
