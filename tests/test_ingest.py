"""Tests for pipeline.ingest — pure-Python logic (no I/O, no external tools)."""

import pytest

from pipeline.ingest import classify_failure
from pipeline.spotdl_ops import find_track_in_snapshot


# ---------------------------------------------------------------------------
# classify_failure
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "msg, expected",
    [
        ("SpotifyError: invalid credentials", "auth_spotify"),
        ("Invalid Credentials provided", "auth_spotify"),
        ("HTTP Error 403: Forbidden", "auth_youtube"),
        ("Sign in to confirm your age", "auth_youtube"),
        ("cookies are required", "auth_youtube"),
        ("429 Too Many Requests", "rate_limited"),
        ("too many requests, back off", "rate_limited"),
        ("some unexpected download error", "spotdl_error"),
        ("", "spotdl_error"),
    ],
)
def test_classify_failure(msg: str, expected: str) -> None:
    assert classify_failure(msg) == expected


# ---------------------------------------------------------------------------
# find_track_in_snapshot
# ---------------------------------------------------------------------------

SNAPSHOT = [
    {"url": "https://spotify.com/track/A", "name": "Song A", "artists": ["Artist 1"]},
    {"url": "https://spotify.com/track/B", "name": "Song B", "artists": ["Artist 2"]},
]


def test_find_track_found() -> None:
    result = find_track_in_snapshot(SNAPSHOT, "https://spotify.com/track/A")
    assert result is not None
    assert result["name"] == "Song A"


def test_find_track_not_found() -> None:
    result = find_track_in_snapshot(SNAPSHOT, "https://spotify.com/track/Z")
    assert result is None


def test_find_track_empty_snapshot() -> None:
    assert find_track_in_snapshot([], "https://spotify.com/track/A") is None


# ---------------------------------------------------------------------------
# Snapshot diff logic (as it would be used in ingest.run)
# ---------------------------------------------------------------------------

def _diff(old: list[dict], new_urls: set[str]) -> set[str]:
    """Replicate the removal-detection logic from ingest.run."""
    old_urls = {s["url"] for s in old}
    return old_urls - new_urls


def test_diff_detects_removal() -> None:
    old = [
        {"url": "https://spotify.com/track/A"},
        {"url": "https://spotify.com/track/B"},
    ]
    new_urls = {"https://spotify.com/track/A"}
    removed = _diff(old, new_urls)
    assert removed == {"https://spotify.com/track/B"}


def test_diff_no_removal() -> None:
    old = [{"url": "https://spotify.com/track/A"}]
    new_urls = {"https://spotify.com/track/A", "https://spotify.com/track/B"}
    assert _diff(old, new_urls) == set()


def test_diff_all_removed() -> None:
    old = [{"url": "https://spotify.com/track/A"}, {"url": "https://spotify.com/track/B"}]
    assert _diff(old, set()) == {"https://spotify.com/track/A", "https://spotify.com/track/B"}


def test_diff_empty_old() -> None:
    assert _diff([], {"https://spotify.com/track/A"}) == set()


# ---------------------------------------------------------------------------
# Track budget logic (as it would be used in spotdl_ops.sync_playlist)
# ---------------------------------------------------------------------------

def _apply_budget(
    old_urls: set[str],
    all_new_urls: list[str],
    budget: int | None,
) -> tuple[list[str], list[str]]:
    """Replicate the track-limiting logic from spotdl_ops.sync_playlist.

    Returns (batch_downloaded, songs_written_to_spotdl_file).
    """
    truly_new = [u for u in all_new_urls if u not in old_urls]
    batch = truly_new if budget is None else truly_new[:budget]
    batch_set = set(batch)
    written = [u for u in all_new_urls if u in old_urls or u in batch_set]
    return batch, written


def test_budget_none_downloads_all_new() -> None:
    old = {"https://spotify.com/track/A"}
    new = ["https://spotify.com/track/A", "https://spotify.com/track/B", "https://spotify.com/track/C"]
    batch, written = _apply_budget(old, new, budget=None)
    assert set(batch) == {"https://spotify.com/track/B", "https://spotify.com/track/C"}
    assert set(written) == set(new)


def test_budget_limits_downloads_and_defers_remainder() -> None:
    old: set[str] = set()
    new = ["https://spotify.com/track/A", "https://spotify.com/track/B", "https://spotify.com/track/C"]
    batch, written = _apply_budget(old, new, budget=2)
    assert batch == ["https://spotify.com/track/A", "https://spotify.com/track/B"]
    # C is deferred — not written to the snapshot
    assert "https://spotify.com/track/C" not in written
    assert set(written) == {"https://spotify.com/track/A", "https://spotify.com/track/B"}


def test_budget_gte_new_behaves_like_unlimited() -> None:
    old: set[str] = set()
    new = ["https://spotify.com/track/A", "https://spotify.com/track/B"]
    batch_limited, written_limited = _apply_budget(old, new, budget=10)
    batch_unlimited, written_unlimited = _apply_budget(old, new, budget=None)
    assert batch_limited == batch_unlimited
    assert written_limited == written_unlimited


def test_budget_deferred_tracks_reappear_next_session() -> None:
    """Tracks excluded from the snapshot show up as new on the next run."""
    old: set[str] = set()
    new = ["https://spotify.com/track/A", "https://spotify.com/track/B", "https://spotify.com/track/C"]

    # Session 1: budget=1, downloads A
    batch1, written1 = _apply_budget(old, new, budget=1)
    assert batch1 == ["https://spotify.com/track/A"]

    # Session 2: old_urls = what was written to .spotdl after session 1
    old2 = set(written1)
    batch2, written2 = _apply_budget(old2, new, budget=1)
    assert batch2 == ["https://spotify.com/track/B"]

    # Session 3
    old3 = set(written2)
    batch3, _ = _apply_budget(old3, new, budget=1)
    assert batch3 == ["https://spotify.com/track/C"]


def test_budget_spans_playlists() -> None:
    """Simulate budget consumption across two playlists."""
    budget = 5

    # Playlist A: 3 new tracks
    old_a: set[str] = set()
    new_a = [f"https://spotify.com/track/A{i}" for i in range(3)]
    batch_a, _ = _apply_budget(old_a, new_a, budget=budget)
    budget -= len(batch_a)  # budget → 2

    # Playlist B: 4 new tracks, only 2 slots remain
    old_b: set[str] = set()
    new_b = [f"https://spotify.com/track/B{i}" for i in range(4)]
    batch_b, written_b = _apply_budget(old_b, new_b, budget=budget)

    assert len(batch_a) == 3
    assert len(batch_b) == 2  # capped by remaining budget
    # B2 and B3 are deferred
    assert "https://spotify.com/track/B2" not in written_b
    assert "https://spotify.com/track/B3" not in written_b
