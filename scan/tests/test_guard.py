"""Tests for music_scan.guard — the length and silence guard (#165).

Fixtures are generated with ffmpeg's lavfi sources (sine tone and silence).
"""

import json
import subprocess
from pathlib import Path

import pytest

from music_scan import guard
from music_scan.guard import Rejection, check_file, guard_inbox, mid_track_silence

AUDIO_EXTS = {".m4a"}
URL = "https://open.spotify.com/track/abc123"


def _clip(path: Path, *parts: tuple[str, float], url: str | None = URL) -> Path:
    """Concatenate ``("tone"|"silence", seconds)`` parts into an AAC m4a tagged like spotdl."""
    args, labels = ["ffmpeg", "-v", "error", "-y"], ""
    for i, (kind, secs) in enumerate(parts):
        src = f"sine=frequency=440:duration={secs}" if kind == "tone" else f"anullsrc=r=44100:cl=mono:d={secs}"
        args += ["-f", "lavfi", "-i", src]
        labels += f"[{i}:a]aresample=44100,aformat=channel_layouts=mono[a{i}];"
    concat = "".join(f"[a{i}]" for i in range(len(parts)))
    args += ["-filter_complex", f"{labels}{concat}concat=n={len(parts)}:v=0:a=1[out]",
             "-map", "[out]", "-c:a", "aac", str(path)]
    subprocess.run(args, check=True)
    if url:
        from mutagen.mp4 import MP4, MP4FreeForm

        audio = MP4(path)
        audio["----:spotdl:WOAS"] = [MP4FreeForm(url.encode())]
        audio.save()
    return path


def _spotdl(spotdl_dir: Path, playlist: str, duration: int | None, url: str = URL) -> None:
    song = {"url": url, "name": "Song", "artists": ["Artist"]}
    if duration is not None:
        song["duration"] = duration
    (spotdl_dir / f"{playlist}.spotdl").write_text(json.dumps({"songs": [song]}))


# ---------------------------------------------------------------------------
# check_file
# ---------------------------------------------------------------------------

def test_right_length_passes(tmp_path: Path) -> None:
    assert check_file(_clip(tmp_path / "a.m4a", ("tone", 30)), 30) is None


def test_twenty_percent_short_is_rejected(tmp_path: Path) -> None:
    r = check_file(_clip(tmp_path / "a.m4a", ("tone", 24)), 30)
    assert r is not None and r.reason == "duration"
    assert "expected 30s, got 24s" in r.detail


def test_within_tolerance_passes(tmp_path: Path) -> None:
    assert check_file(_clip(tmp_path / "a.m4a", ("tone", 32)), 30) is None


def test_mid_track_silence_is_rejected(tmp_path: Path) -> None:
    path = _clip(tmp_path / "a.m4a", ("tone", 15), ("silence", 10), ("tone", 15))
    r = check_file(path, 40)
    assert r is not None and r.reason == "silence"


def test_leading_and_trailing_silence_pass(tmp_path: Path) -> None:
    path = _clip(tmp_path / "a.m4a", ("silence", 10), ("tone", 20), ("silence", 10))
    assert check_file(path, 40) is None


def test_no_expected_length_still_checks_silence(tmp_path: Path) -> None:
    assert check_file(_clip(tmp_path / "a.m4a", ("tone", 30)), None) is None
    gap = _clip(tmp_path / "b.m4a", ("tone", 15), ("silence", 10), ("tone", 15))
    assert check_file(gap, None) == Rejection("silence", check_file(gap, None).detail)


def test_mid_track_silence_window_rules() -> None:
    assert mid_track_silence([(0.0, 9.0)], 60) is None  # leading
    assert mid_track_silence([(50.0, None)], 60) is None  # runs to the end
    assert mid_track_silence([(50.0, 58.0)], 60) is None  # ends within the edge
    assert mid_track_silence([(20.0, 30.0)], 60) == (20.0, 30.0)


# ---------------------------------------------------------------------------
# guard_inbox
# ---------------------------------------------------------------------------

@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    inbox, rejected = tmp_path / "inbox" / "spotdl", tmp_path / "quarantine" / "rejected"
    (inbox / "wedding").mkdir(parents=True)
    return inbox, rejected


def test_guard_rejects_into_playlist_dir_with_reason(dirs) -> None:
    inbox, rejected = dirs
    _spotdl(inbox, "wedding", 30)
    _clip(inbox / "wedding" / "Artist - Song.m4a", ("tone", 20))

    counts = guard_inbox(inbox, inbox, rejected, AUDIO_EXTS, mode="on")

    assert counts == {"duration": 1, "silence": 0}
    assert not (inbox / "wedding" / "Artist - Song.m4a").exists()
    moved = rejected / "wedding" / "Artist - Song.m4a"
    assert moved.exists()
    assert (rejected / "wedding" / "Artist - Song.m4a.reason").read_text().startswith("duration: expected 30s")


def test_guard_dry_run_moves_nothing(dirs, caplog) -> None:
    inbox, rejected = dirs
    _spotdl(inbox, "wedding", 30)
    _clip(inbox / "wedding" / "Artist - Song.m4a", ("tone", 20))

    counts = guard_inbox(inbox, inbox, rejected, AUDIO_EXTS, mode="dry-run")

    assert sum(counts.values()) == 0
    assert (inbox / "wedding" / "Artist - Song.m4a").exists()
    assert "[WOULD-REJECT]" in caplog.text


def test_guard_song_missing_from_snapshot_fails_open(dirs, caplog) -> None:
    inbox, rejected = dirs
    _spotdl(inbox, "wedding", 30, url="https://open.spotify.com/track/other")
    _clip(inbox / "wedding" / "Artist - Song.m4a", ("tone", 20))

    with caplog.at_level("INFO"):
        counts = guard_inbox(inbox, inbox, rejected, AUDIO_EXTS, mode="on")

    assert sum(counts.values()) == 0
    assert (inbox / "wedding" / "Artist - Song.m4a").exists()
    assert "no Spotify duration" in caplog.text


def test_guard_song_without_duration_fails_open(dirs) -> None:
    inbox, rejected = dirs
    _spotdl(inbox, "wedding", None)
    _clip(inbox / "wedding" / "Artist - Song.m4a", ("tone", 20))

    assert sum(guard_inbox(inbox, inbox, rejected, AUDIO_EXTS, mode="on").values()) == 0


def test_guard_finds_duration_in_another_playlist(dirs) -> None:
    """A backfill re-download may sit in a playlist whose .spotdl lacks the entry."""
    inbox, rejected = dirs
    _spotdl(inbox, "keep", 30)
    _clip(inbox / "wedding" / "Artist - Song.m4a", ("tone", 20))

    assert guard_inbox(inbox, inbox, rejected, AUDIO_EXTS, mode="on")["duration"] == 1


def test_guard_off_checks_nothing(dirs, monkeypatch) -> None:
    inbox, rejected = dirs
    monkeypatch.setattr(guard, "check_file", lambda *a, **k: pytest.fail("checked with LENGTH_GUARD=off"))
    _clip(inbox / "wedding" / "Artist - Song.m4a", ("tone", 20))
    guard_inbox(inbox, inbox, rejected, AUDIO_EXTS, mode="off")


@pytest.mark.parametrize("value,mode", [("on", "on"), ("OFF", "off"), ("", "dry-run"), ("bogus", "dry-run")])
def test_guard_mode_env(monkeypatch, value, mode) -> None:
    monkeypatch.setenv("LENGTH_GUARD", value)
    assert guard.guard_mode() == mode


# ---------------------------------------------------------------------------
# Rejected files never reach the library through the asis pass
# ---------------------------------------------------------------------------

def test_asis_pass_skips_rejected(tmp_path: Path) -> None:
    from music_scan.scan import _move_asis_eligible

    quarantine, staging = tmp_path / "quarantine", tmp_path / "staging"
    f = quarantine / "rejected" / "wedding" / "a.m4a"
    f.parent.mkdir(parents=True)
    _clip(f, ("tone", 5))
    from mutagen.mp4 import MP4

    audio = MP4(f)
    audio["\xa9nam"], audio["\xa9ART"], audio["\xa9alb"], audio["trkn"] = ["T"], ["A"], ["Al"], [(1, 1)]
    audio.save()

    assert _move_asis_eligible(quarantine, staging) == 0
    assert f.exists()
