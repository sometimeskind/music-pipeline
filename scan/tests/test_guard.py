"""Tests for music_scan.guard — the length and silence guard (#165).

Fixtures are generated with ffmpeg's lavfi sources (sine tone and silence).
"""

import json
from pathlib import Path

import pytest

from music_scan import guard
from music_scan.guard import Rejection, check_file, guard_inbox, mid_track_silence

AUDIO_EXTS = {".m4a"}
URL = "https://open.spotify.com/track/abc123"



def _store(playlist: str, duration: int | None, url: str = URL) -> None:
    """Write *playlist*'s store with one song (homelab#1914); the ``dirs`` fixture points the stores."""
    from music_fetch import store

    song = {"url": url, "name": "Song", "artists": ["Artist"]}
    if duration is not None:
        song["duration"] = duration
    store.save(store.Store.from_songs(playlist, [song]))


# ---------------------------------------------------------------------------
# check_file
# ---------------------------------------------------------------------------

def test_right_length_passes(tmp_path: Path, clip) -> None:
    assert check_file(clip(tmp_path / "a.m4a", ("tone", 30)), 30) is None


def test_twenty_percent_short_is_rejected(tmp_path: Path, clip) -> None:
    r = check_file(clip(tmp_path / "a.m4a", ("tone", 24)), 30)
    assert r is not None and r.reason == "duration"
    assert "expected 30s, got 24s" in r.detail


def test_within_tolerance_passes(tmp_path: Path, clip) -> None:
    assert check_file(clip(tmp_path / "a.m4a", ("tone", 32)), 30) is None


def test_mid_track_silence_is_rejected(tmp_path: Path, clip) -> None:
    path = clip(tmp_path / "a.m4a", ("tone", 15), ("silence", 10), ("tone", 15))
    r = check_file(path, 40)
    assert r is not None and r.reason == "silence"


def test_leading_and_trailing_silence_pass(tmp_path: Path, clip) -> None:
    path = clip(tmp_path / "a.m4a", ("silence", 10), ("tone", 20), ("silence", 10))
    assert check_file(path, 40) is None


def test_no_expected_length_still_checks_silence(tmp_path: Path, clip) -> None:
    assert check_file(clip(tmp_path / "a.m4a", ("tone", 30)), None) is None
    gap = clip(tmp_path / "b.m4a", ("tone", 15), ("silence", 10), ("tone", 15))
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
def dirs(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    inbox, rejected = tmp_path / "inbox" / "spotdl", tmp_path / "quarantine" / "rejected"
    (inbox / "wedding").mkdir(parents=True)
    return inbox, rejected


def test_guard_rejects_into_playlist_dir_with_reason(dirs, clip, monkeypatch) -> None:
    from music_fetch import events

    inbox, rejected = dirs
    _store("wedding", 30)
    clip(inbox / "wedding" / "Artist - Song.m4a", ("tone", 20))
    recorded = []
    monkeypatch.setattr(events, "record", lambda kind, message, **kw: recorded.append((kind, message, kw)))

    counts = guard_inbox(inbox, rejected, AUDIO_EXTS, mode="on")

    assert counts == {"duration": 1, "silence": 0, "fingerprint": 0, "acoustid-unknown": 0}
    (kind, message, kw), = recorded
    assert kind == "reject" and message.startswith("[REJECT] wedding: Artist - Song.m4a — duration: ")
    assert kw["data"]["playlist"] == "wedding" and kw["data"]["reason"] == "duration"
    assert not (inbox / "wedding" / "Artist - Song.m4a").exists()
    moved = rejected / "wedding" / "Artist - Song.m4a"
    assert moved.exists()
    assert (rejected / "wedding" / "Artist - Song.m4a.reason").read_text().startswith("duration: expected 30s")


def test_guard_dry_run_moves_nothing(dirs, caplog, clip) -> None:
    inbox, rejected = dirs
    _store("wedding", 30)
    clip(inbox / "wedding" / "Artist - Song.m4a", ("tone", 20))

    counts = guard_inbox(inbox, rejected, AUDIO_EXTS, mode="dry-run")

    assert sum(counts.values()) == 0
    assert (inbox / "wedding" / "Artist - Song.m4a").exists()
    assert "[WOULD-REJECT]" in caplog.text


def test_guard_song_missing_from_snapshot_fails_open(dirs, caplog, clip) -> None:
    inbox, rejected = dirs
    _store("wedding", 30, url="https://open.spotify.com/track/other")
    clip(inbox / "wedding" / "Artist - Song.m4a", ("tone", 20))

    with caplog.at_level("INFO"):
        counts = guard_inbox(inbox, rejected, AUDIO_EXTS, mode="on")

    assert sum(counts.values()) == 0
    assert (inbox / "wedding" / "Artist - Song.m4a").exists()
    assert "no Spotify duration" in caplog.text


def test_guard_song_without_duration_fails_open(dirs, clip) -> None:
    inbox, rejected = dirs
    _store("wedding", None)
    clip(inbox / "wedding" / "Artist - Song.m4a", ("tone", 20))

    assert sum(guard_inbox(inbox, rejected, AUDIO_EXTS, mode="on").values()) == 0


def test_guard_finds_duration_in_another_playlist(dirs, clip) -> None:
    """A backfill re-download may sit in a playlist whose store lacks the entry."""
    inbox, rejected = dirs
    _store("keep", 30)
    clip(inbox / "wedding" / "Artist - Song.m4a", ("tone", 20))

    assert guard_inbox(inbox, rejected, AUDIO_EXTS, mode="on")["duration"] == 1


def test_expected_durations_read_every_store_never_a_spotdl_file(dirs) -> None:
    """homelab#1914: durations by playlist from the stores; songs without a URL or a
    duration are left out, and a ``.spotdl`` snapshot beside them is ignored."""
    from music_fetch import store

    inbox, _ = dirs
    store.save(store.Store.from_songs("keep", [{"url": URL, "duration": 30}, {"url": "https://x/2"},
                                    {"duration": 40}]))
    _store("wedding", 200, url="https://x/3")
    (inbox / "old.spotdl").write_text(json.dumps({"songs": [{"url": URL, "duration": 1}]}))
    assert guard.expected_durations() == {"keep": {URL: 30}, "wedding": {"https://x/3": 200}}


def test_guard_off_checks_nothing(dirs, monkeypatch, clip) -> None:
    inbox, rejected = dirs
    monkeypatch.setattr(guard, "check_file", lambda *a, **k: pytest.fail("checked with LENGTH_GUARD=off"))
    clip(inbox / "wedding" / "Artist - Song.m4a", ("tone", 20))
    guard_inbox(inbox, rejected, AUDIO_EXTS, mode="off")


@pytest.mark.parametrize("value,mode", [("on", "on"), ("OFF", "off"), ("", "dry-run"), ("bogus", "dry-run")])
def test_guard_mode_env(monkeypatch, value, mode) -> None:
    monkeypatch.setenv("LENGTH_GUARD", value)
    assert guard.guard_mode() == mode


# ---------------------------------------------------------------------------
# Rejected files never reach the library through the asis pass
# ---------------------------------------------------------------------------

def test_asis_pass_skips_rejected(tmp_path: Path, clip) -> None:
    from music_scan.scan import _move_asis_eligible

    quarantine, staging = tmp_path / "quarantine", tmp_path / "staging"
    f = quarantine / "rejected" / "wedding" / "a.m4a"
    f.parent.mkdir(parents=True)
    clip(f, ("tone", 5))
    from mutagen.mp4 import MP4

    audio = MP4(f)
    audio["\xa9nam"], audio["\xa9ART"], audio["\xa9alb"], audio["trkn"] = ["T"], ["A"], ["Al"], [(1, 1)]
    audio.save()

    assert _move_asis_eligible(quarantine, staging) == 0
    assert f.exists()


# ---------------------------------------------------------------------------
# verify_requests: an album request's fallback (homelab#2478)
# ---------------------------------------------------------------------------

REC = "00000001-0000-4000-8000-000000000001"
REC_URL = f"https://musicbrainz.org/recording/{REC}"
OTHER = "00000002-0000-4000-8000-000000000002"


@pytest.fixture
def requests_dirs(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    from music_fetch import events

    monkeypatch.setattr(guard, "ACOUSTID_DELAY", 0)
    monkeypatch.setattr(events, "record", lambda *a, **kw: None)
    unverified = tmp_path / "unverified"
    (unverified / "request").mkdir(parents=True)
    return unverified, tmp_path / "inbox" / "spotdl", tmp_path / "quarantine" / "rejected"


def _verify(dirs, found, songs=None):
    unverified, inbox, rejected = dirs
    songs = {REC_URL: {"url": REC_URL, "song_id": REC, "duration": 30}} if songs is None else songs

    def recordings(path):
        if isinstance(found, Exception):
            raise found
        return found

    return guard.verify_requests(unverified, inbox, rejected, AUDIO_EXTS, songs, recordings)


def test_a_fallback_download_acoustid_names_as_its_recording_moves_to_the_inbox(requests_dirs, clip) -> None:
    unverified, inbox, rejected = requests_dirs
    clip(unverified / "request" / "Band - Song.m4a", ("tone", 30), url=REC_URL)
    counts = _verify(requests_dirs, {REC: 0.91, OTHER: 0.95})
    assert sum(counts.values()) == 0
    assert (inbox / "request" / "Band - Song.m4a").exists()
    assert not (unverified / "request" / "Band - Song.m4a").exists()


@pytest.mark.parametrize("found,reason,why", [
    # The strict rule (music-pipeline#375): another recording rejects, even with ours beside it below the score.
    ({OTHER: 0.97}, "fingerprint", f"AcoustID names 1 other recording(s) (best {OTHER} at 0.97)"),
    ({REC: 0.6, OTHER: 0.4}, "fingerprint", f"AcoustID names 1 other recording(s) (best {OTHER} at 0.40)"),
    # Ours, but too weakly to count as a match.
    ({REC: 0.6}, "fingerprint", "AcoustID names it only at score 0.60"),
    (ValueError("no fingerprint: bad file"), "fingerprint", "no fingerprint: bad file"),
    # homelab#2546: AcoustID has nothing to say, which is not a mismatch.
    ({}, "acoustid-unknown", "AcoustID knows no MusicBrainz recording for it"),
])
def test_a_fallback_download_no_id_confirms_is_rejected(requests_dirs, clip, found, reason, why) -> None:
    unverified, inbox, rejected = requests_dirs
    clip(unverified / "request" / "Band - Song.m4a", ("tone", 30), url=REC_URL)
    counts = _verify(requests_dirs, found)
    assert counts == {**dict.fromkeys(guard.REASONS, 0), reason: 1}
    assert not (inbox / "request").exists()
    sidecar = (rejected / "request" / "Band - Song.m4a.reason").read_text()
    assert sidecar == f"{reason}: not recording {REC}: {why}\n"
    # Held from then on, whatever the reason: the nightly doesn't download the same video again.
    from music_scan.held import held_urls

    assert REC_URL in held_urls(rejected.parent, inbox, unverified)
    assert guard.request_rejects(rejected) == {REC: reason}


def test_a_fallback_download_of_the_wrong_length_is_rejected_before_the_lookup(requests_dirs, clip) -> None:
    unverified, _, rejected = requests_dirs
    clip(unverified / "request" / "Band - Song.m4a", ("tone", 20), url=REC_URL)
    counts = _verify(requests_dirs, AssertionError("not looked up"))
    assert counts["duration"] == 1 and (rejected / "request" / "Band - Song.m4a").exists()


def test_a_failed_lookup_leaves_the_download_for_the_next_scan(requests_dirs, clip, caplog) -> None:
    unverified, inbox, rejected = requests_dirs
    path = clip(unverified / "request" / "Band - Song.m4a", ("tone", 30), url=REC_URL)
    counts = _verify(requests_dirs, guard.LookupFailed("503"))
    assert sum(counts.values()) == 0 and path.exists()
    assert "AcoustID lookup failed (503)" in caplog.text


def test_a_download_no_request_wants_is_rejected(requests_dirs, clip) -> None:
    unverified, _, rejected = requests_dirs
    clip(unverified / "request" / "Band - Song.m4a", ("tone", 30), url=REC_URL)
    counts = _verify(requests_dirs, AssertionError("not looked up"), songs={})
    assert counts["fingerprint"] == 1
    assert "no album request's track is" in (rejected / "request" / "Band - Song.m4a.reason").read_text()


def _never(path):
    raise AssertionError(f"{path.name} looked up")


def test_downloads_wait_when_the_album_state_cant_be_read(requests_dirs, clip, monkeypatch) -> None:
    """An unreadable state is not an empty one: every download would read as unwanted and be rejected."""
    unverified, inbox, rejected = requests_dirs
    path = clip(unverified / "request" / "Band - Song.m4a", ("tone", 30), url=REC_URL)
    monkeypatch.setattr(guard, "request_songs", lambda: None)
    counts = guard.verify_requests(unverified, inbox, rejected, AUDIO_EXTS, None, _never)
    assert sum(counts.values()) == 0 and path.exists() and not rejected.exists()


# AcoustID's lookup responses (homelab#2546), through pyacoustid's own parsing: only the
# web service and the fingerprinting are faked.
def _response(*results):
    return {"status": "ok", "results": list(results)}


@pytest.mark.parametrize("response,expected", [
    (_response(), {}),  # no result: AcoustID doesn't know the fingerprint
    (_response({"id": "a1", "score": 0.97}), {}),  # a known fingerprint linked to no recording
    (_response({"id": "a1", "score": 0.97, "recordings": []}), {}),
    (_response({"id": "a1", "score": 0.97, "recordings": [{"id": OTHER}]},
               {"id": "a2", "score": 0.6, "recordings": [{"id": REC}, {"id": OTHER}]}),
     {OTHER: 0.97, REC: 0.6}),  # each recording once, at its best score
])
def test_acoustid_recordings_reads_each_response_shape(monkeypatch, tmp_path, response, expected) -> None:
    import acoustid

    monkeypatch.setenv("ACOUSTID_APIKEY", "key")
    monkeypatch.setattr(acoustid, "fingerprint_file", lambda path, force_fpcalc=False: (30.0, "FP"))
    asked = []
    monkeypatch.setattr(acoustid, "lookup", lambda key, fp, duration, meta, timeout: asked.append(timeout) or response)
    assert guard.acoustid_recordings(tmp_path / "a.m4a") == expected
    assert asked == [guard.LOOKUP_TIMEOUT]


@pytest.mark.parametrize("failure", [
    {"status": "error", "error": {"code": 4, "message": "invalid API key"}},
    "HTTP request timed out (30s)",
])
def test_an_acoustid_error_or_timeout_is_a_failed_lookup_not_a_reason(monkeypatch, tmp_path, failure) -> None:
    import acoustid

    def lookup(*args, **kwargs):
        if isinstance(failure, str):
            raise acoustid.WebServiceError(failure)
        return failure

    monkeypatch.setenv("ACOUSTID_APIKEY", "key")
    monkeypatch.setattr(acoustid, "fingerprint_file", lambda path, force_fpcalc=False: (30.0, "FP"))
    monkeypatch.setattr(acoustid, "lookup", lookup)
    with pytest.raises(guard.LookupFailed):
        guard.acoustid_recordings(tmp_path / "a.m4a")


def test_a_file_that_cant_be_fingerprinted_is_a_value_error(monkeypatch, tmp_path) -> None:
    import acoustid

    def broken(path, force_fpcalc=False):
        raise acoustid.FingerprintGenerationError("fpcalc exited with status 2")

    monkeypatch.setenv("ACOUSTID_APIKEY", "key")
    monkeypatch.setattr(acoustid, "fingerprint_file", broken)
    with pytest.raises(ValueError, match="no fingerprint"):
        guard.acoustid_recordings(tmp_path / "a.m4a")


@pytest.mark.parametrize("response,reason", [
    (_response(), "acoustid-unknown"),
    (_response({"id": "a1", "score": 0.97}), "acoustid-unknown"),
    (_response({"id": "a1", "score": 0.97, "recordings": [{"id": OTHER}]}), "fingerprint"),
    (_response({"id": "a1", "score": 0.97, "recordings": [{"id": OTHER}, {"id": REC}]}), None),
])
def test_the_scan_splits_the_reasons_on_the_real_lookup(requests_dirs, clip, monkeypatch, response, reason) -> None:
    import acoustid

    unverified, inbox, rejected = requests_dirs
    monkeypatch.setenv("ACOUSTID_APIKEY", "key")
    monkeypatch.setattr(acoustid, "fingerprint_file", lambda path, force_fpcalc=False: (30.0, "FP"))
    monkeypatch.setattr(acoustid, "lookup", lambda *a, **kw: response)
    clip(unverified / "request" / "Band - Song.m4a", ("tone", 30), url=REC_URL)
    songs = {REC_URL: {"url": REC_URL, "song_id": REC, "duration": 30}}
    counts = guard.verify_requests(unverified, inbox, rejected, AUDIO_EXTS, songs)
    if reason is None:
        assert sum(counts.values()) == 0 and (inbox / "request" / "Band - Song.m4a").exists()
    else:
        assert counts[reason] == 1 and (rejected / "request" / "Band - Song.m4a").exists()


def _held(rejected: Path, clip, name: str, url: str | None, reason: str) -> Path:
    (rejected / "request").mkdir(parents=True, exist_ok=True)
    path = clip(rejected / "request" / name, ("tone", 1), url=url)
    path.with_name(name + ".reason").write_text(f"{reason}: detail\n", encoding="utf-8")
    return path


def test_request_rejects_reads_each_held_download_by_its_recording(tmp_path, clip) -> None:
    rejected = tmp_path / "rejected"
    third = "00000003-0000-4000-8000-000000000003"
    _held(rejected, clip, "a.m4a", REC_URL, "acoustid-unknown")
    _held(rejected, clip, "b.m4a", f"https://musicbrainz.org/recording/{OTHER}", "acoustid-unknown")
    _held(rejected, clip, "c.m4a", f"https://musicbrainz.org/recording/{OTHER}", "fingerprint")  # rejected twice
    gone = _held(rejected, clip, "d.m4a", f"https://musicbrainz.org/recording/{third}", "duration")
    gone.unlink()  # the operator took it away: the nightly fetches it again
    _held(rejected, clip, "e.m4a", URL, "duration")  # not a recording URL
    (rejected / "chill").mkdir()
    other_playlist = clip(rejected / "chill" / "f.m4a", ("tone", 1), url=REC_URL)  # the length guard's, not a request's
    other_playlist.with_name("f.m4a.reason").write_text("silence: detail\n", encoding="utf-8")
    assert guard.request_rejects(rejected) == {REC: "acoustid-unknown", OTHER: "fingerprint"}
    assert guard.request_rejects(tmp_path / "nowhere") == {}


def test_the_length_guard_leaves_verified_request_files_alone(dirs, clip, monkeypatch) -> None:
    inbox, rejected = dirs
    (inbox / "request").mkdir()
    path = clip(inbox / "request" / "Band - Song.m4a", ("tone", 15), ("silence", 10), ("tone", 15), url=REC_URL)
    assert sum(guard_inbox(inbox, rejected, AUDIO_EXTS, mode="on").values()) == 0
    assert path.exists()


# ---------------------------------------------------------------------------
# recheck_unknown: a held acoustid-unknown download asked about again (homelab#2546)
# ---------------------------------------------------------------------------

NOW = 1_800_000_000.0
DAY = 86400
UNKNOWN_LINE = f"acoustid-unknown: not recording {REC}: AcoustID knows no MusicBrainz recording for it"


def _held_unknown(rejected: Path, clip, name: str = "Band - Song.m4a", url: str = REC_URL,
                  held: float | None = NOW - 3 * DAY, checked: float | None = None,
                  first: str = UNKNOWN_LINE) -> Path:
    (rejected / "request").mkdir(parents=True, exist_ok=True)
    path = clip(rejected / "request" / name, ("tone", 1), url=url)
    stamps = {k: v for k, v in (("held", held), ("checked", checked)) if v is not None}
    guard.write_sidecar(path.with_name(name + ".reason"), first, stamps)
    return path


def _recheck(dirs, found, songs=None, now=NOW, days=None):
    """Run the re-check with AcoustID answering *found*; returns the outcomes and the files looked up."""
    _, inbox, rejected = dirs
    songs = {REC_URL: {"url": REC_URL, "song_id": REC, "duration": 30}} if songs is None else songs
    asked: list[str] = []

    def recordings(path):
        asked.append(path.name)
        if isinstance(found, Exception):
            raise found
        return found

    return guard.recheck_unknown(rejected, inbox, songs, recordings, now=now, days=days), asked


def _outcomes(**counts) -> dict[str, int]:
    return {**dict.fromkeys(guard.RECHECKS, 0), **counts}


def test_a_held_unknown_file_acoustid_now_confirms_moves_to_the_inbox(requests_dirs, clip, caplog) -> None:
    import logging

    caplog.set_level(logging.INFO)
    _, inbox, rejected = requests_dirs
    path = _held_unknown(rejected, clip, checked=NOW - DAY)
    outcomes, asked = _recheck(requests_dirs, {REC: 0.9})
    assert outcomes == _outcomes(verified=1) and asked == [path.name]
    # Where a verified download goes, for this scan's import; the reject is gone.
    assert (inbox / "request" / "Band - Song.m4a").exists()
    assert not path.exists() and not path.with_name(path.name + ".reason").exists()
    assert guard.request_rejects(rejected) == {}
    assert f"[VERIFIED] request: Band - Song.m4a — AcoustID names recording {REC} on re-check" in caplog.text
    assert "1 verified, 0 now fingerprint, 0 still unknown, 0 failed lookup(s)" in caplog.text


@pytest.mark.parametrize("found,why", [
    ({OTHER: 0.99}, f"AcoustID names 1 other recording(s) (best {OTHER} at 0.99)"),
    ({REC: 0.6, OTHER: 0.4}, f"AcoustID names 1 other recording(s) (best {OTHER} at 0.40)"),
    ({REC: 0.84}, "AcoustID names it only at score 0.84"),
    (ValueError("no fingerprint: bad file"), "no fingerprint: bad file"),
])
def test_a_recheck_accepts_only_the_tracks_own_recording_at_the_score(requests_dirs, clip, found, why) -> None:
    """The strict rule holds on a re-check: another recording, or ours below 0.85, is a real mismatch."""
    _, inbox, rejected = requests_dirs
    path = _held_unknown(rejected, clip)
    outcomes, _ = _recheck(requests_dirs, found)
    assert outcomes == _outcomes(fingerprint=1)
    assert path.exists() and not (inbox / "request").exists()
    first, stamps = guard.read_sidecar(path.with_name(path.name + ".reason"))
    assert first == f"fingerprint: not recording {REC}: {why}"
    assert stamps == {"held": NOW - 3 * DAY, "checked": NOW}
    assert guard.request_rejects(rejected) == {REC: "fingerprint"}
    # Never asked again.
    assert _recheck(requests_dirs, AssertionError("not looked up"), now=NOW + 5 * DAY)[0] == _outcomes()


def test_a_held_file_acoustid_still_doesnt_know_stays_and_is_asked_again_a_day_later(requests_dirs, clip) -> None:
    _, _, rejected = requests_dirs
    path = _held_unknown(rejected, clip)
    sidecar = path.with_name(path.name + ".reason")
    assert _recheck(requests_dirs, {})[0] == _outcomes(**{"acoustid-unknown": 1})
    assert path.exists()
    assert guard.read_sidecar(sidecar) == (UNKNOWN_LINE, {"held": NOW - 3 * DAY, "checked": NOW})
    assert guard.request_rejects(rejected) == {REC: "acoustid-unknown"}
    # Once a day at most.
    assert _recheck(requests_dirs, AssertionError("not looked up"), now=NOW + DAY - 60)[0] == _outcomes()
    outcomes, asked = _recheck(requests_dirs, {}, now=NOW + DAY)
    assert outcomes == _outcomes(**{"acoustid-unknown": 1}) and asked == [path.name]


def test_a_fresh_reject_waits_a_day_for_its_first_recheck(requests_dirs, clip) -> None:
    _, _, rejected = requests_dirs
    _held_unknown(rejected, clip, held=NOW - 2 * 3600)
    assert _recheck(requests_dirs, AssertionError("not looked up"))[0] == _outcomes()


def test_a_reject_from_before_the_recheck_is_held_since_its_sidecar_was_written(requests_dirs, clip) -> None:
    import os

    _, _, rejected = requests_dirs
    path = _held_unknown(rejected, clip, held=None)
    sidecar = path.with_name(path.name + ".reason")
    sidecar.write_text(UNKNOWN_LINE + "\n", encoding="utf-8")  # what reject() writes
    os.utime(sidecar, (NOW - 2 * DAY, NOW - 2 * DAY))
    assert _recheck(requests_dirs, {})[0] == _outcomes(**{"acoustid-unknown": 1})
    assert guard.read_sidecar(sidecar)[1] == {"held": NOW - 2 * DAY, "checked": NOW}


def test_a_failed_lookup_changes_nothing_and_ends_the_pass(requests_dirs, clip, caplog) -> None:
    _, _, rejected = requests_dirs
    a = _held_unknown(rejected, clip, "a.m4a")
    b = _held_unknown(rejected, clip, "b.m4a")
    before = {p: p.with_name(p.name + ".reason").read_text() for p in (a, b)}
    outcomes, asked = _recheck(requests_dirs, guard.LookupFailed("HTTP request timed out (30s)"))
    assert outcomes == _outcomes(failed=1) and asked == ["a.m4a"]  # AcoustID is down: b isn't tried
    assert {p: p.with_name(p.name + ".reason").read_text() for p in (a, b)} == before
    assert "AcoustID re-check failed (HTTP request timed out (30s))" in caplog.text
    # Tried again at the next scan, not a day later.
    assert _recheck(requests_dirs, {}, now=NOW + 300)[0] == _outcomes(**{"acoustid-unknown": 2})


def test_after_the_recheck_days_the_file_stays_held_for_the_operator(requests_dirs, clip, caplog) -> None:
    _, _, rejected = requests_dirs
    path = _held_unknown(rejected, clip, held=NOW - 30 * DAY, checked=NOW - DAY)
    outcomes, asked = _recheck(requests_dirs, {REC: 0.99})
    assert outcomes == _outcomes(stopped=1) and asked == []
    assert path.exists()
    assert guard.read_sidecar(path.with_name(path.name + ".reason"))[1]["stopped"] == NOW
    assert guard.request_rejects(rejected) == {REC: "acoustid-unknown"}
    assert caplog.text.count("held 30+ days as unknown to AcoustID; no longer re-checked") == 1
    # Logged once: later scans leave it alone.
    caplog.clear()
    assert _recheck(requests_dirs, AssertionError("not looked up"), now=NOW + 2 * DAY)[0] == _outcomes()
    assert "[UNKNOWN]" not in caplog.text


def test_the_recheck_days_come_from_the_environment(monkeypatch, requests_dirs, clip) -> None:
    monkeypatch.setenv("ACOUSTID_RECHECK_DAYS", "7")
    assert guard.recheck_days() == 7
    _, _, rejected = requests_dirs
    _held_unknown(rejected, clip, held=NOW - 8 * DAY)
    assert _recheck(requests_dirs, AssertionError("not looked up"))[0] == _outcomes(stopped=1)
    monkeypatch.setenv("ACOUSTID_RECHECK_DAYS", "soon")
    assert guard.recheck_days() == guard.RECHECK_DAYS


def test_only_unknown_files_a_request_still_wants_are_rechecked(requests_dirs, clip) -> None:
    _, _, rejected = requests_dirs
    _held_unknown(rejected, clip, "real.m4a", first=f"fingerprint: not recording {REC}: detail")
    _held_unknown(rejected, clip, "short.m4a", first="duration: expected 30s, got 20s (-33%)")
    _held_unknown(rejected, clip, "dropped.m4a", url=f"https://musicbrainz.org/recording/{OTHER}")
    gone = _held_unknown(rejected, clip, "gone.m4a")
    gone.unlink()  # the operator took it away
    assert _recheck(requests_dirs, AssertionError("not looked up"))[0] == _outcomes()


def test_no_file_is_rechecked_while_the_album_state_cant_be_read(requests_dirs, clip, monkeypatch) -> None:
    _, _, rejected = requests_dirs
    path = _held_unknown(rejected, clip)
    monkeypatch.setattr(guard, "request_songs", lambda: None)
    _, inbox, _ = requests_dirs
    assert guard.recheck_unknown(rejected, inbox, None, _never, now=NOW) == _outcomes()
    assert guard.read_sidecar(path.with_name(path.name + ".reason"))[1] == {"held": NOW - 3 * DAY}
