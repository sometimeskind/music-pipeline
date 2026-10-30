"""``music-requests fallback-sample``: the MusicBrainz-built fallback's hit rate (homelab#2478).

An album request's fallback (:func:`music_fetch.album_requests.release_songs`,
:func:`music_scan.guard.verify_requests`) builds spotdl songs from a
MusicBrainz release and imports a download only when AcoustID names the
track's own recording.  There are too few requests to measure that on, so this
runs the same steps on albums the library already holds whole: N album records
with a release group whose stamped items name one MusicBrainz release.  For
each it reads that release's songs, downloads them with spotdl into a scratch
directory, and checks each file as the scan would (length and silence, then
AcoustID).  The library's own copy of each recording is the ground truth: a
download whose fingerprint matches it (``music-audit-dupes``' bit error rate) is
the right audio, whatever AcoustID said.

Nothing is imported, tagged or kept: the scratch directory is deleted.  It
reads MusicBrainz and AcoustID and downloads from YouTube, about a dozen
tracks an album: an operator step (exec), never run by the pipeline.
"""

from __future__ import annotations

import dataclasses
import logging
import random
import shutil
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path

logger = logging.getLogger(__name__)

# Albums sampled by default, and at most: each costs a dozen YouTube downloads.
DEFAULT_N, MAX_N = 10, 25
# The statuses of an album the library holds whole.
HELD = ("imported", "have", "filled")

# download(songs, directory): spotdl's downloads of *songs* into *directory*.
Download = Callable[[list[dict], Path], None]


@dataclasses.dataclass
class Album:
    key: str
    label: str
    record: dict
    release: str  # the MusicBrainz release the library's copy is
    held: dict[str, object]  # recording → the library's item of it


@dataclasses.dataclass
class Outcome:
    """One sampled track: what the fallback would have done with it."""

    verdict: str  # "accepted", "duration", "silence", "fingerprint", "lookup", "not found"
    same_audio: bool | None  # the download's fingerprint matches the library's copy (None: no copy or no file)


def candidates(records: Mapping[str, dict], items: list) -> list[Album]:
    """The album records the library holds whole that have a release group,
    each with the one MusicBrainz release its stamped items name."""
    stamped: dict[str, list] = {}
    for item in items:
        if item.get("album_record") and not item.get("album_extra"):
            stamped.setdefault(item.get("album_record"), []).append(item)
    out = []
    for key, record in records.items():
        mine = stamped.get(key, [])
        if record.get("status") not in HELD or not record.get("release_group") or not mine:
            continue
        releases = Counter(i.get("mb_albumid") for i in mine if i.get("mb_albumid"))
        if len(releases) != 1:
            continue
        held = {i.get("mb_trackid"): i for i in mine if i.get("mb_trackid")}
        out.append(Album(key, f"{record.get('artist')} — {record.get('name')}", record,
                         next(iter(releases)), held))
    return out


def judge(path: Path, song: dict, held, recordings: Callable[[Path], set[str]],
          same: Callable[[Path, object], bool]) -> Outcome:
    """The fallback's verdict on one download, and whether it is the library's audio."""
    from music_scan.guard import LookupFailed, check_file  # noqa: PLC0415

    right = same(path, held) if held is not None else None
    rejection = check_file(path, song.get("duration") or None)
    if rejection is not None:
        return Outcome(rejection.reason, right)
    try:
        found = recordings(path)
    except LookupFailed:
        return Outcome("lookup", right)
    except ValueError:
        found = set()
    return Outcome("accepted" if song["song_id"] in found else "fingerprint", right)


def sample(albums: list[Album], songs_of: Callable[[dict], list[dict]], download: Download,
           recordings: Callable[[Path], set[str]], same: Callable[[Path, object], bool],
           scratch: Path) -> list[tuple[Album, list[Outcome]]]:
    """Run the fallback on each album into *scratch*; its files are deleted after each album."""
    from music_scan.guard import song_url  # noqa: PLC0415

    out = []
    for album in albums:
        try:
            songs = songs_of({**album.record, "tracklist_release": album.release})
        except Exception as exc:
            logger.warning("[SAMPLE] %s: reading release %s failed: %s", album.label, album.release, exc)
            continue
        folder = scratch / album.key
        folder.mkdir(parents=True, exist_ok=True)
        try:
            download(songs, folder)
            files = {song_url(f): f for f in sorted(folder.rglob("*.m4a"))}
            outcomes = []
            for song in songs:
                path = files.get(song["url"])
                held = album.held.get(song["song_id"])
                outcomes.append(Outcome("not found", None) if path is None
                                else judge(path, song, held, recordings, same))
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        out.append((album, outcomes))
        logger.info("[SAMPLE] %s (release %s): %s", album.label, album.release, summary(outcomes))
    return out


def summary(outcomes: list[Outcome]) -> str:
    verdicts = Counter(o.verdict for o in outcomes)
    accepted = [o for o in outcomes if o.verdict == "accepted"]
    rejected = [o for o in outcomes if o.verdict not in ("accepted", "not found", "lookup")]
    parts = [f"{len(outcomes)} track(s)", f"{verdicts['accepted']} accepted"]
    parts.append(f"{sum(1 for o in accepted if o.same_audio)} of them the library's audio, "
                 f"{sum(1 for o in accepted if o.same_audio is False)} not")
    parts.append(f"{len(rejected)} rejected ({', '.join(f'{r} {verdicts[r]}' for r in ('duration', 'silence', 'fingerprint') if verdicts[r]) or 'none'}), "
                 f"{sum(1 for o in rejected if o.same_audio)} of them the library's audio")
    parts.append(f"{verdicts['not found']} not found by spotdl")
    if verdicts["lookup"]:
        parts.append(f"{verdicts['lookup']} AcoustID lookup(s) failed")
    return "; ".join(parts)


def same_audio(path: Path, item) -> bool:
    """Whether the file at *path* is *item*'s audio: their fingerprints within
    ``music-audit-dupes``' bit error rate."""
    from music_scan.dupes import MATCH_BER, bit_error_rate, decode  # noqa: PLC0415
    from music_scan.identity import fingerprint  # noqa: PLC0415

    theirs = item.get("acoustid_fingerprint")
    if not theirs:
        return False
    try:
        return bit_error_rate(decode(fingerprint(path)), decode(theirs)) <= MATCH_BER
    except Exception:
        return False


def run(n: int = DEFAULT_N, seed: int = 0) -> list[tuple[Album, list[Outcome]]]:
    """Sample *n* held albums (chosen by *seed*) and log the fallback's hit rate."""
    from music_fetch import album_requests, albums, ingest  # noqa: PLC0415
    from music_fetch.spotdl_ops import download_fallback  # noqa: PLC0415

    from music_scan.guard import acoustid_recordings  # noqa: PLC0415
    from music_scan.library import MusicLibrary  # noqa: PLC0415
    from music_scan.scan import LIBRARY_DB  # noqa: PLC0415

    records = albums.State.load(albums.STATE_FILE).albums
    with MusicLibrary(LIBRARY_DB) as lib:
        found = candidates(records, list(lib.all_items()))
        logger.info("[SAMPLE] %d album(s) held whole with a release group and one release; sampling %d (seed %d)",
                    len(found), min(n, len(found)), seed)
        chosen = random.Random(seed).sample(found, min(n, len(found)))
        get = album_requests.musicbrainz()

        def download(songs: list[dict], folder: Path) -> None:
            download_fallback(songs, output_dir=folder, cookie_file=ingest.COOKIE_FILE)

        with tempfile.TemporaryDirectory(prefix="fallback-sample-") as scratch:
            results = sample(chosen, lambda r: album_requests.release_songs(get, r), download,
                             acoustid_recordings, same_audio, Path(scratch))
    outcomes = [o for _, album_outcomes in results for o in album_outcomes]
    logger.info("[SAMPLE] total over %d album(s): %s", len(results), summary(outcomes))
    if outcomes:
        accepted = sum(1 for o in outcomes if o.verdict == "accepted")
        logger.info("[SAMPLE] hit rate: %d of %d track(s) (%.0f%%) would be imported", accepted, len(outcomes),
                    100 * accepted / len(outcomes))
    return results
