"""Album requests (homelab#2478 stage 2): albums asked for by MusicBrainz release group.

A request is an ``album_requests`` row (migration 7 in :mod:`music_fetch.db`):
the release group, an optional preferred release, who asked and when, and the
album's tracklist as read from MusicBrainz when it was requested.  Each album
tick (:func:`albums.feed_requests`) gives every request an album row with
``origin: "request"`` whose tracks, under the pseudo-playlist :data:`SOURCE`,
are that tracklist, or upgrades the row that has the release group already
(one record per album).  From then on the album is searched, grabbed and
imported like an album playlist's: the import files it under
``inbox/usenet/request/``.

The tracklist (:func:`tracklist`) is the preferred release's, else the one of
the release group's official releases with its most common track count (the
earliest of them), video media left out.  It is stored as album mode's track
entries, ``[name, artist, None, isrc, disc, track]``: no Spotify track ID, so
search, the wrong-album check and the import read it unchanged.  Without
Spotify IDs there is no spotdl fallback yet: a request Usenet misses stays
``missing`` (homelab#2478 decisions).

``music-requests`` (:mod:`music_fetch.album_requests_cli`) lists and adds
requests; the Music MCP's ``request_album`` and ``list_requests`` run on it.
"""

from __future__ import annotations

import dataclasses
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from music_fetch import events
from music_fetch import state as statedb

# The pseudo-playlist a request's tracks are filed under in its album record:
# the import's inbox folder (inbox/usenet/request/) and its items' sources.
SOURCE = "request"
# Requests one call takes: each costs two or three MusicBrainz reads (one a second).
MAX_PER_CALL = 25
# A request whose album the tick has not queued yet.
PENDING = "pending"
# MusicBrainz release statuses a tracklist is taken from when no release is preferred.
OFFICIAL = "Official"
# Media whose tracks are not audio.
VIDEO_FORMATS = frozenset({"DVD-Video", "Blu-ray", "HD-DVD", "VHS", "VCD", "SVCD", "UMD Video", "LaserDisc"})
# Releases read per page when browsing a release group's (MusicBrainz's maximum), and pages at most.
PAGE = 100
MAX_PAGES = 5

_MBID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# get(path, **params): a MusicBrainz web service read (music_scan.mb_release.MusicBrainz.get), None on 404.
Get = Callable[..., "dict | None"]


class Refused(RuntimeError):
    """The request can't be made as asked; nothing is written."""


def mbid(value: str, kind: str) -> str:
    """The MBID *value* names: an MBID, or a ``musicbrainz.org/<kind>/<mbid>`` URL."""
    text = value.strip().lower()
    if _MBID.fullmatch(text):
        return text
    m = re.fullmatch(rf"(?:https?://)?(?:beta\.)?musicbrainz\.org/{kind}/({_MBID.pattern})(?:[/?#].*)?", text)
    if m:
        return m.group(1)
    raise ValueError(f"{value!r} is not a MusicBrainz {kind} ID or URL")


def caller() -> str:
    from music_fetch.overrides import caller as overrides_caller  # noqa: PLC0415

    return overrides_caller()


# ---------------------------------------------------------------------------
# The tracklist, from MusicBrainz
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Tracklist:
    """A release group's album as a request stores it."""

    release_group: str
    release: str  # the release the tracks are read from
    artist: str
    name: str
    tracks: list[list]
    year: int | None = None
    album_type: str = "album"
    duration: int = 0  # seconds
    how: str = ""  # "preferred", or how the release was chosen
    release_label: str = ""  # the release as the dry run names it

    def fields(self) -> dict:
        """The album record's fields the request sets (album mode's names)."""
        return {"name": self.name, "artist": self.artist, "year": self.year, "tracks_count": len(self.tracks),
                "album_type": self.album_type, "duration": self.duration}


def _credit(credits: Iterable[dict] | None) -> str:
    """An artist credit as MusicBrainz prints it: names and join phrases."""
    return "".join(f"{c.get('name') or (c.get('artist') or {}).get('name', '')}{c.get('joinphrase', '')}"
                   for c in credits or ())


def _audio(medium: Mapping) -> bool:
    return medium.get("format") not in VIDEO_FORMATS


def _track_count(release: Mapping) -> int:
    return sum(m.get("track-count") or len(m.get("tracks") or ()) for m in release.get("media") or () if _audio(m))


def _year(date: str | None) -> int | None:
    return int(date[:4]) if date and date[:4].isdigit() else None


def _release_label(release: Mapping) -> str:
    formats = Counter(m.get("format") or "?" for m in release.get("media") or ())
    media = " + ".join(f"{n}×{f}" if n > 1 else f for f, n in formats.items()) or "no media"
    extra = ", ".join(x for x in (release.get("date"), release.get("country"), media,
                                  release.get("disambiguation")) if x)
    return f"{release.get('title', '')} ({extra})"


def _live(release: Mapping) -> bool:
    return bool(re.search(r"\blive\b", (release.get("disambiguation") or "").lower()))


def _browse(get: Get, group: str) -> list[dict]:
    releases: list[dict] = []
    for page in range(MAX_PAGES):
        data = get("release", **{"release-group": group, "inc": "media", "limit": str(PAGE),
                                 "offset": str(page * PAGE)}) or {}
        releases += data.get("releases") or []
        if len(releases) >= (data.get("release-count") or 0) or not data.get("releases"):
            break
    return releases


def choose_release(group: Mapping, releases: list[dict]) -> tuple[dict, str]:
    """The release a request without a preferred one takes its tracklist from,
    and how it was chosen: of the official releases (those not marked live in
    their disambiguation, unless the release group is a live one or none is
    left), the earliest with the most common track count; a tie between counts
    goes to the count of the earliest release."""
    official = [r for r in releases if r.get("status") == OFFICIAL and _track_count(r)]
    if not official:
        raise Refused(f"release group {group['id']} has no official release with tracks: name a preferred_release")
    if "Live" not in (group.get("secondary-types") or ()):
        official = [r for r in official if not _live(r)] or official
    ordered = sorted(official, key=lambda r: (r.get("date") or "9999", r["id"]))
    counts = Counter(_track_count(r) for r in ordered)
    first = {}
    for r in ordered:
        first.setdefault(_track_count(r), r)
    best = max(counts, key=lambda n: (counts[n], -ordered.index(first[n])))
    how = (f"the earliest of {counts[best]} official release(s) with {best} tracks, the most common count "
           f"among {len(official)}" + (f" (also: {', '.join(f'{n} tracks ×{c}' for n, c in sorted(counts.items()) if n != best)})"
                                       if len(counts) > 1 else ""))
    return first[best], how


def tracklist(get: Get, group_id: str, preferred: str | None = None) -> Tracklist:
    """Release group *group_id*'s album and tracklist, from *preferred* or
    :func:`choose_release`'s pick.  Raises :class:`Refused` when the release
    group or the release doesn't exist, or the release is another group's."""
    group = get(f"release-group/{group_id}", inc="artist-credits")
    if group is None:
        raise Refused(f"MusicBrainz has no release group {group_id}")
    if preferred:
        how = "preferred"
    else:
        picked, how = choose_release(group, _browse(get, group_id))
        preferred = picked["id"]
    release = get(f"release/{preferred}", inc="recordings+isrcs+artist-credits+release-groups")
    if release is None:
        raise Refused(f"MusicBrainz has no release {preferred}")
    of = (release.get("release-group") or {}).get("id")
    if of != group_id:
        raise Refused(f"release {preferred} is of release group {of}, not {group_id}")
    artist = _credit(group.get("artist-credit")) or _credit(release.get("artist-credit"))
    tracks: list[list] = []
    seconds = 0
    for medium in release.get("media") or ():
        if not _audio(medium):
            continue
        for track in medium.get("tracks") or ():
            recording = track.get("recording") or {}
            isrcs = recording.get("isrcs") or []
            tracks.append([track.get("title") or recording.get("title") or "",
                           _credit(track.get("artist-credit")) or artist, None, isrcs[0] if isrcs else None,
                           medium.get("position"), track.get("position")])
            seconds += (track.get("length") or recording.get("length") or 0) // 1000
    if not tracks:
        raise Refused(f"release {preferred} has no audio tracks on MusicBrainz")
    album_type = "single" if group.get("primary-type") == "Single" else "album"
    return Tracklist(group_id, preferred, artist, group.get("title") or release.get("title") or "", tracks,
                     _year(group.get("first-release-date")), album_type, seconds, how, _release_label(release))


def musicbrainz() -> Get:
    """The pipeline's rate-limited MusicBrainz reads."""
    from music_scan.mb_release import MusicBrainz  # noqa: PLC0415 — the image holds both packages

    return MusicBrainz().get


# ---------------------------------------------------------------------------
# The rows
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Request:
    release_group: str
    tracklist_release: str
    artist: str
    name: str
    tracks: list[list]
    data: dict = dataclasses.field(default_factory=dict)
    preferred_release: str | None = None
    requested_by: str = ""
    requested_at: datetime | None = None

    def fields(self) -> dict:
        """The album record's fields the request sets."""
        return {"name": self.name, "artist": self.artist, "year": self.data.get("year"),
                "tracks_count": len(self.tracks), "album_type": self.data.get("album_type") or "album",
                "duration": self.data.get("duration") or 0}


def load(conn: psycopg.Connection) -> list[Request]:
    """Every request, oldest first."""
    return [Request(group, release, artist, name, tracks, data, preferred, by, at)
            for group, preferred, release, artist, name, data, tracks, by, at in conn.execute(
                "SELECT release_group::text, preferred_release::text, tracklist_release::text, artist, name, data, "
                "tracks, requested_by, requested_at FROM album_requests ORDER BY requested_at, id")]


def album_requests() -> list[Request]:
    """The requests the album tick feeds (:func:`albums.feed_requests`): none
    while the JSON files are the state.  Raises :class:`state.Unavailable`
    when the database holds the state and is down."""
    with statedb.session() as conn:
        return [] if conn is None else load(conn)


def add(conn: psycopg.Connection, found: Tracklist, by: str, key: str | None = None,
        record: Mapping | None = None) -> bool:
    """Insert the request for *found* with its ``album_requested`` event, in
    one transaction; False when the release group is requested already (a
    concurrent request).  *key*/*record*: the album row that has the release
    group, if any."""
    data = {"year": found.year, "album_type": found.album_type, "duration": found.duration}
    preferred = found.release if found.how == "preferred" else None
    with conn.transaction():
        row = conn.execute(
            "INSERT INTO album_requests (release_group, preferred_release, tracklist_release, artist, name, data, "
            "tracks, requested_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (release_group) DO NOTHING RETURNING id",
            (found.release_group, preferred, found.release, found.artist, found.name, Jsonb(data),
             Jsonb(found.tracks), by)).fetchone()
        if row is None:
            return False
        events.insert(conn, "album_requested",
                      f"[REQUEST] {found.artist} — {found.name} ({found.release_group}): {len(found.tracks)} "
                      f"track(s) from release {found.release}" + (" (preferred)" if preferred else ""),
                      source="requests", release_group=found.release_group,
                      spotify_album_id=statedb.primary_spotify(record) if record else None,
                      data={"album": f"{found.artist} — {found.name}", "key": key, "requested_by": by,
                            "preferred_release": preferred, "tracklist_release": found.release,
                            "tracks": len(found.tracks)})
    return True


def find_row(rows: statedb.AlbumRows, group: str) -> tuple[str, dict] | None:
    """The album row that has release group *group*."""
    return next(((k, r) for k, r in rows.albums.items() if r.get("release_group") == group), None)


def iso(t: datetime | None) -> str | None:
    return None if t is None else t.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def entry(request: Request, rows: statedb.AlbumRows) -> dict:
    """One request as ``list_requests`` shows it: its album row's key, status and origin, or ``pending``."""
    found = find_row(rows, request.release_group)
    album: dict[str, Any] | None = None
    if found is not None:
        key, record = found
        album = {"key": key, "status": record.get("status"), "origin": record.get("origin") or statedb.ALBUM,
                 "searched_at": record.get("searched_at"), "query": record.get("query")}
    return {"release_group": request.release_group, "artist": request.artist, "name": request.name,
            "preferred_release": request.preferred_release, "tracklist_release": request.tracklist_release,
            "tracks": len(request.tracks), "requested_by": request.requested_by,
            "requested_at": iso(request.requested_at),
            "status": album["status"] if album else PENDING, "album": album}


def listing(conn: psycopg.Connection, rows: statedb.AlbumRows, status: str | None = None) -> list[dict]:
    """Every request, oldest first, with its album's status; only *status*'s when given."""
    out = [entry(r, rows) for r in load(conn)]
    return [e for e in out if status is None or e["status"] == status]
