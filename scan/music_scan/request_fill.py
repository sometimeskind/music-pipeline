"""A request's tracks the library holds without a recording ID (homelab#2589).

A request's missing tracks (``albums._request_tracks``, its ``fallback_left``
and what its spotdl fallback downloads) are the recordings of its MusicBrainz
tracklist that no item on the album holds, as ``fallback_recording`` or
``mb_trackid``.  An item beets matched to no recording has no ``mb_trackid``
(about a third of the library, mostly spotdl downloads from before requests),
so it counted as missing and the fallback would download it again.

Its position on its release is no evidence: an edition numbers its tracks
otherwise (key 162's item 61, *Such a Bad Way*, is track 5 of 10 on its
Spotify album and track 6 of 11 on the request's tracklist).  A title alone is
none either.  Its audio is: :class:`Filler` looks the item up on AcoustID and,
when exactly one of the tracklist's recordings is among those AcoustID links to
its fingerprint at :data:`~music_scan.mb_fingerprint.MIN_SCORE` or more, and
that track's title is the item's song, stores that recording as the item's
``mb_trackid`` with a ``recording_filled`` event in the same transaction
(migration 18), then writes the file's tags.  The request's held check then
counts it, and so does everything else keyed on ``mb_trackid`` (``audit_dupes``'
certain tier, Navidrome).

Only the items on a request's album that lack a recording ID are looked up,
and only while the request still lacks a track, so a tick makes a handful of
lookups at most (pyacoustid keeps to AcoustID's 3 requests a second).  An item
AcoustID can't place is remembered for the process, by its file's path and
mtime, so it isn't looked up again every tick; a restart asks again.  A failed
lookup (AcoustID down) ends the lookups for the tick, remembering nothing.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Callable, Iterable, Mapping

from music_scan.mb_fingerprint import MIN_SCORE, LookupFailed, Recordings, acoustid_recordings, file_stat

logger = logging.getLogger(__name__)

KIND = "recording_filled"
SOURCE = "albums"
FALLBACK_RECORDING = "fallback_recording"  # music_pipeline.FALLBACK_RECORDING

Lookup = Callable[[str], Recordings]

# Items AcoustID placed on no request track, by (item ID, path, mtime): not asked again this process.
_unplaced: set[tuple[int, str, int | None]] = set()


def tracklist(record: Mapping) -> dict[str, dict]:
    """A request's tracklist songs by recording: its ``album_songs`` read from
    MusicBrainz (a Spotify tracklist names no recording, so it gives none)."""
    from music_fetch.album_requests import url_recording  # noqa: PLC0415

    return {rec: song for song in record.get("album_songs") or () if (rec := url_recording(song.get("url")))}


def placed(found: Recordings, songs: Mapping[str, dict]) -> tuple[str, float] | None:
    """The one recording of *songs* among *found* at :data:`MIN_SCORE` or more,
    with its score; None for none or several (never guess)."""
    hits: dict[str, float] = {}
    for score, rec in found:
        if score >= MIN_SCORE and rec in songs:
            hits[rec] = max(score, hits.get(rec, 0.0))
    return next(iter(hits.items())) if len(hits) == 1 else None


def unplaced(items: Iterable) -> list:
    """The *items* with no recording ID, each once."""
    seen: dict[int, object] = {}
    for item in items:
        if not item.get("mb_trackid") and not item.get(FALLBACK_RECORDING):
            seen.setdefault(item.id, item)
    return list(seen.values())


def _path(item) -> str:
    return os.fsdecode(item.path) if isinstance(item.path, bytes) else str(item.path)


def _acoustid() -> Lookup | None:
    key = os.environ.get("ACOUSTID_APIKEY")
    return (lambda path: acoustid_recordings(path, key)) if key else None


class Filler:
    """The album tick's AcoustID fill over one open library (see the module).

    *lock* is the pipeline lock's context manager factory (beets' SQLite has one
    writer), *connect* opens the music database (``db.connect``), *lookup* asks
    AcoustID for a file's recordings (default: :func:`acoustid_recordings` with
    ``ACOUSTID_APIKEY``; without the key nothing is looked up)."""

    def __init__(self, lib, *, lock: Callable[[], contextlib.AbstractContextManager] = contextlib.nullcontext,
                 connect: Callable | None = None, lookup: Lookup | None = None) -> None:
        self.lib, self.lock, self._connect, self._lookup = lib, lock, connect, lookup
        self.failed = False  # a lookup failed this tick: no more

    def lookup(self) -> Lookup | None:
        if self._lookup is None:
            self._lookup = _acoustid() or False
            if not self._lookup:
                logger.warning("[NOFILL] ACOUSTID_APIKEY is unset: a request's items without a recording ID "
                               "count as missing")
        return self._lookup or None

    def __call__(self, key: str, record: Mapping, items: Iterable, held: set[str]) -> set[str]:
        """The recordings of *record*'s tracklist that *items* hold by their audio
        and *held* lacks; each such item's ``mb_trackid`` is filled with its event."""
        songs = tracklist(record)
        if not songs or not set(songs) - held:
            return set()
        todo = unplaced(items)
        if not todo or self.failed or not (lookup := self.lookup()):
            return set()
        filled: set[str] = set()
        for item in todo:
            path = _path(item)
            seen = (item.id, path, file_stat(path)[2])
            if seen in _unplaced:
                continue
            try:
                found = lookup(path)
            except LookupFailed as exc:
                logger.warning("[NOFILL] AcoustID couldn't be asked (%s); %s's items without a recording ID "
                               "count as missing until the next tick", exc, record.get("name"))
                self.failed = True
                break
            except ValueError as exc:  # no fingerprint: the file is unreadable or gone
                logger.info("[NOFILL] %s — %s: %s", item.id, item.get("title") or "?", exc)
                _unplaced.add(seen)
                continue
            hit = placed(found, songs)
            if hit is None:
                best = ", ".join(f"{r} {s:.2f}" for s, r in found[:3]) or "nothing"
                logger.info("[NOFILL] %s — %s: AcoustID names %s of %s — %s's tracks at %.2f (it found %s)",
                            item.id, item.get("title") or "?", "several" if self._several(found, songs) else "none",
                            record.get("artist"), record.get("name"), MIN_SCORE, best)
                _unplaced.add(seen)
                continue
            rec, score = hit
            if self._suspect(item, songs[rec]):
                _unplaced.add(seen)
                continue
            try:
                if self._store(key, record, item, rec, score, songs[rec]):
                    filled.add(rec)
            except Exception:
                logger.exception("[NOFILL] %s — %s: storing its recording %s failed; the next tick tries again",
                                 item.id, item.get("title") or "?", rec)
        return filled

    @staticmethod
    def _several(found: Recordings, songs: Mapping[str, dict]) -> bool:
        return len({r for s, r in found if s >= MIN_SCORE and r in songs}) > 1

    @staticmethod
    def _suspect(item, song: Mapping) -> bool:
        """True when the track AcoustID placed the item on names another song
        than the item's title (an AcoustID collision or a wrong download,
        homelab#2583): left alone, logged ``[SUSPECT]``."""
        from music_scan.identity import different_song  # noqa: PLC0415

        title = item.get("title") or ""
        if not different_song(title, song.get("name") or ""):
            return False
        logger.warning("[SUSPECT] %s — %s: AcoustID places it on the request's track %s %r, another song; "
                       "left without a recording ID", item.id, title, song.get("track_number"), song.get("name"))
        return True

    def _store(self, key: str, record: Mapping, item, rec: str, score: float, song: Mapping) -> bool:
        """Store *rec* as the item's ``mb_trackid`` with its event, under the lock,
        re-reading the item first; True when the item holds *rec* afterwards."""
        from music_fetch import db, events  # noqa: PLC0415

        connect = self._connect or db.connect
        with self.lock(), contextlib.closing(connect()) as conn:
            fresh = self.lib.get_item(item.id)
            if fresh is None:
                return False
            if fresh.get("mb_trackid"):  # set since the tick read the library
                return fresh.get("mb_trackid") == rec
            title = fresh.get("title") or "?"
            message = (f"[HELD] {record.get('artist')} — {record.get('name')}: {fresh.id} — {title} is the "
                       f"request's track {song.get('track_number')} by AcoustID ({score:.2f}); mb_trackid → {rec}")
            with conn.transaction():
                events.insert(conn, KIND, message, source=SOURCE,
                              spotify_album_id=events.spotify_album(fresh.get("spotify_album_id")),
                              release_group=record.get("release_group"), item_id=fresh.id,
                              data={**events.item_fields(fresh), "key": str(key), "old": None, "new": rec,
                                    "score": score, "track": song.get("track_number"),
                                    "track_title": song.get("name"), "item_track": fresh.get("track")})
                fresh["mb_trackid"] = rec
                fresh.store()
            fresh.try_write()
        logger.info("%s", message)
        return True
