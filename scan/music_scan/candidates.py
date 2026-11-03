"""The recordings an item could be, from the release it sits on (homelab#2623).

``music-mb-fingerprint --candidates ITEM_ID`` (the Music MCP's
``recording_candidates``): for an item whose ``mb_trackid`` names a sibling
recording that AcoustID agrees with (a 5.1 mix, a live take: a remaster and its
fold-down or live take fingerprint alike), the evidence is the release the item
sits on.  Its ``mb_albumid``, disc and track number name a recording, whose
length and disambiguation can be set beside the file's.

It reads the item's release from MusicBrainz (``inc=recordings+isrcs``) and
lists:

- ``[AT-POSITION]`` the recording at the item's disc and track (an item with no
  disc number counts its track across the release, as beets numbers a release
  without per-disc numbering);
- ``[SAME-TITLE]`` every other recording on the release whose title is the
  item's song (version notes dropped, :func:`~music_scan.identity.different_song`);

each with its MBID, title, disambiguation, length (and how far it is from the
file's), ISRCs and positions, the one the item holds now marked ``← current``.
When the item's disc or track isn't on its release (``[VARIANT]``: the release
group's variants order their discs differently, as *Nebraska '82* does), it
also reads up to :data:`MAX_VARIANTS` releases of the group whose disc layout
fits the item (digital first, then CD, those with the item's disc count first) and lists their recordings at the
item's position and of the same song, with the release each is on.

It never decides: two siblings are told apart by a person, who sets the one
the file is with ``set_recording``.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

# A MusicBrainz web service read: (path, **params) -> the JSON, or None for a 404.
Get = Callable[..., "dict | None"]

MAX_VARIANTS = 3
# The variants read first: the format a Usenet WEB release is, then CDs.
FORMAT_RANK = {"Digital Media": 0, "CD": 1}


@dataclasses.dataclass
class Candidate:
    recording: str
    title: str
    disambiguation: str
    length: int | None  # ms
    isrcs: tuple[str, ...]
    at: list[str] = dataclasses.field(default_factory=list)  # "disc 1 #10", "disc 4 #9 on 3905d186 (CD, US 2025-10-24)"
    at_position: bool = False


def _tracks(release: dict):
    """(disc, track position, track) for every track of *release*, in order."""
    for medium in release.get("media") or []:
        for track in medium.get("tracks") or []:
            yield medium.get("position") or 0, track.get("position") or 0, track


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def position_track(release: dict, disc: int, track: int) -> dict | None:
    """The track at *disc* and *track* on *release*; with no disc (0), the
    *track*-th track counted across the release."""
    if track <= 0:
        return None
    if disc <= 0:
        listed = [t for _, _, t in _tracks(release)]
        return listed[track - 1] if track <= len(listed) else None
    return next((t for d, p, t in _tracks(release) if d == disc and p == track), None)


def _count(medium: dict) -> int:
    return medium.get("track-count") or len(medium.get("tracks") or [])


def fits(release: dict, disc: int, track: int, tracktotal: int) -> bool:
    """True when the item's position fits *release*: its disc has its track and,
    when the item knows its tracktotal, as many tracks (or the release does, for a
    tracktotal counted across the release).  With no disc, the release has its track."""
    media = release.get("media") or []
    if disc <= 0:
        return position_track(release, disc, track) is not None
    medium = next((m for m in media if m.get("position") == disc), None)
    if medium is None or not 0 < track <= _count(medium):
        return False
    return not tracktotal or tracktotal in (_count(medium), sum(_count(m) for m in media))


def _label(release: dict) -> str:
    media = release.get("media") or []
    formats = "/".join(dict.fromkeys(m.get("format") or "?" for m in media)) or "?"
    when = " ".join(filter(None, (release.get("country"), release.get("date"))))
    return f"{release.get('id', '?')[:8]} ({formats}{', ' + when if when else ''})"


def _rank(release: dict, disctotal: int) -> tuple:
    media = release.get("media") or []
    return (min((FORMAT_RANK.get(m.get("format") or "", 2) for m in media), default=2),
            bool(disctotal) and len(media) != disctotal, release.get("status") != "Official",
            release.get("date") or "9999", release.get("id") or "")


def _seconds(ms) -> str:
    return f"{ms / 1000:.1f} s" if isinstance(ms, (int, float)) else "no length"


def _same_song(title: str, other: str) -> bool:
    """*other* names *title*'s song, version notes dropped; a title with no words matches nothing."""
    from music_scan.identity import clean_album, different_song, release_words  # noqa: PLC0415

    return all(release_words(clean_album(t)) for t in (title, other)) and not different_song(title, other)


def collect(item, release: dict, variants: list[dict]) -> tuple[list[Candidate], bool]:
    """The candidates for *item* on *release* and its fitting *variants*, and whether
    the item's position is on *release* itself."""
    disc, track = _int(item.get("disc")), _int(item.get("track"))
    title = item.get("title") or ""
    found: dict[str, Candidate] = {}

    def add(t: dict, where: str, at_position: bool) -> None:
        r = t.get("recording") or {}
        c = found.get(r.get("id"))
        if c is None:
            c = found[r["id"]] = Candidate(r["id"], r.get("title") or "", r.get("disambiguation") or "",
                                           r.get("length"), tuple(r.get("isrcs") or ()))
        if where not in c.at:
            c.at.append(where)
        c.at_position = c.at_position or at_position

    at = position_track(release, disc, track)
    for releases, own in (([release], True), (variants, False)):
        for rel in releases:
            on = "" if own else f" on {_label(rel)}"
            hit = at if own else position_track(rel, disc, track)
            for d, p, t in _tracks(rel):
                is_position = hit is not None and t is hit
                if is_position or _same_song(title, (t.get("recording") or {}).get("title") or t.get("title") or ""):
                    add(t, f"disc {d} #{p}{on}", is_position)
    ordered = sorted(found.values(), key=lambda c: (not c.at_position, c.at[0]))
    return ordered, at is not None


def recording_candidates(item, get: Get) -> list[Candidate]:
    """Log the recordings *item* could be (module doc); returns them.  Raises
    :class:`SystemExit` when the item has no release, or its lookup fails."""
    album = item.get("mb_albumid") or ""
    label = f"{item.id} — {item.get('title')}"
    if not album:
        raise SystemExit(f"--candidates: item {label} has no mb_albumid, so no release to read its "
                         "recording from")
    release = get(f"release/{album}", inc="recordings+isrcs")
    if not release:
        raise SystemExit(f"--candidates: MusicBrainz has no release {album} (item {label}'s mb_albumid)")
    disc, track, tracktotal = _int(item.get("disc")), _int(item.get("track")), _int(item.get("tracktotal"))
    current = item.get("mb_trackid") or ""
    length = item.get("length") or 0
    media = release.get("media") or []
    logger.info("[ITEM] %s by %s: %s, disc %s, track %s; mb_trackid %s; ISRC %s", label, item.get("artist") or "?",
                _seconds(length * 1000), disc or "unknown", track or "unknown", current or "unset",
                item.get("isrc") or "none")
    logger.info("[RELEASE] %s %r%s: %d disc(s) of %s track(s)", album, release.get("title"),
                f" ({release['disambiguation']})" if release.get("disambiguation") else "", len(media),
                "+".join(str(m.get("track-count") or len(m.get("tracks") or [])) for m in media))
    if disc <= 0 and track > 0:
        logger.info("[NOTE] the item has no disc number: track %d is counted across the release", track)
    variants: list[dict] = []
    if not fits(release, disc, track, tracktotal):
        variants = _variants(release, item, get)
    candidates, on_release = collect(item, release, variants)
    if not on_release:
        logger.info("[NOTE] nothing on the item's release sits at disc %s track %s", disc or "?", track or "?")
    for c in candidates:
        delta = (f", {(c.length / 1000 - length):+.1f} s against the file"
                 if isinstance(c.length, (int, float)) and length else "")
        logger.info("[%s] %s %r%s — %s%s; %s; %s%s", "AT-POSITION" if c.at_position else "SAME-TITLE", c.recording,
                    c.title, f" ({c.disambiguation})" if c.disambiguation else "", _seconds(c.length), delta,
                    ", ".join(c.at), f"ISRCs {', '.join(c.isrcs)}" if c.isrcs else "no ISRC",
                    "  ← current" if c.recording == current else "")
    if current and all(c.recording != current for c in candidates):
        logger.info("[CURRENT] the item's recording %s is on none of these releases at its song", current)
    logger.info("%d candidate(s). This only lists: compare length and disambiguation with the file and its "
                "title, pick one with the user, and set it with set_recording (--set-recording %s <MBID>)",
                len(candidates), item.id)
    return candidates


def _variants(release: dict, item, get: Get) -> list[dict]:
    """Up to :data:`MAX_VARIANTS` releases of *release*'s group whose disc layout fits *item*."""
    disc, track = _int(item.get("disc")), _int(item.get("track"))
    disctotal, tracktotal = _int(item.get("disctotal")), _int(item.get("tracktotal"))
    group = (release.get("release-group") or {}).get("id") or item.get("mb_releasegroupid") or ""
    media = release.get("media") or []
    logger.info("[VARIANT] the item's disc %s track %s (of %s disc(s), %s track(s)) doesn't fit its release "
                "(%d disc(s)): its files are likely another release of the group, so the group's releases "
                "that fit are read too", disc or "?", track or "?", disctotal or "?", tracktotal or "?", len(media))
    if not group:
        logger.info("[VARIANT] the item has no release group; only its own release is read")
        return []
    listing = get("release", **{"release-group": group, "inc": "media", "limit": "100"}) or {}
    fitting = sorted((r for r in listing.get("releases") or []
                      if r.get("id") != release.get("id") and fits(r, disc, track, tracktotal)),
                     key=lambda r: _rank(r, disctotal))
    if not fitting:
        logger.info("[VARIANT] no release of %s fits disc %s track %s", group, disc, track)
        return []
    logger.info("[VARIANT] %d release(s) of the group fit; reading %s", len(fitting),
                ", ".join(_label(r) for r in fitting[:MAX_VARIANTS]))
    return [full for r in fitting[:MAX_VARIANTS] if (full := get(f"release/{r['id']}", inc="recordings+isrcs"))]
