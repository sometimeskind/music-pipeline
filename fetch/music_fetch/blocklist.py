"""Why a release is on an album's blocklist (homelab#2661).

The record's ``blocklist`` holds bare GUIDs.  What each one was and why it is
there are in the event store, so they are joined when read, and the stored
list stays as it is: a ``grab`` event carries the release's GUID, title and
size, and the outcome after it the reason.  That outcome is a ``failure`` (a
broken download, a ``[WRONG]`` album or a crashed import, each with its
``reason``; an ``[ERR]`` grab, which has no ``grab`` event before it) or an
``import_incomplete``.  An outcome names its GUID since homelab#2661; an older
one belongs to the grab before it.

Events began on 2026-10-29 and are pruned (:data:`events.KEEP_DAYS`), so an
older entry answers :data:`UNKNOWN`.  So does an ``[ERR]`` grab from before
homelab#2661: its event holds the title but not the GUID.

The reason sorts an entry into one of two kinds (:func:`kind`, homelab#2757),
which say how far it reaches now that two indexers can return one release
under two GUIDs (:class:`usenet.Copies`).  A :data:`WRONG_RELEASE` is a verdict
on what the release held, another album or an incomplete copy: every copy of
it, on any indexer, holds the same, so it blocks them all.  A
:data:`DEAD_DOWNLOAD` is any other failure, a broken download above all: it
blocks only its own GUID, as another indexer's copy may be another post, and
complete, so that copy may be tried once; a second dead copy of the title
blocks the rest.  An entry whose reason isn't known blocks only its GUID, as
every entry did before.  The record stores no kind, so the records written
before homelab#2757 read the same way.  A later "blocklist this release" tool
(homelab#2750) adds a wrong release by appending its GUID and recording a
``failure`` event with that GUID, the release's title and a reason that begins
with one of :data:`VERDICTS`.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from music_fetch.usenet import Copies, repost_name

UNKNOWN = "unknown"
UNREADABLE = "unknown: the event store can't be read"
# How the two verdicts on what a release held begin (:func:`kind`).
ANOTHER_ALBUM = "another album: "
LACKS = "lacks "
VERDICTS = (ANOTHER_ALBUM, LACKS)
# The kinds of entry (:func:`kind`).
WRONG_RELEASE = "wrong release"
DEAD_DOWNLOAD = "dead download"
# A dead download blocks every copy of its title from this many dead copies on.
DEAD_COPIES = 2

# Only the album tick's: spotdl's own failures carry the album's Spotify ID too.
_EVENTS = """
SELECT at, kind, data FROM events
 WHERE source = 'albums' AND kind = ANY(%(kinds)s)
   AND (spotify_album_id = ANY(%(refs)s) OR release_group = %(group)s::uuid OR data->>'key' = ANY(%(refs)s))
 ORDER BY at, id
"""


def album_events(conn, refs: list[str], group: str | None) -> list[tuple[datetime, str, dict]]:
    """The album's grabs and their outcomes, oldest first (*refs*: :func:`events.album_refs`)."""
    return conn.execute(_EVENTS, {"kinds": ["grab", "failure", "import_incomplete"], "refs": list(refs),
                                  "group": group}).fetchall()


def _reason(kind: str, data: dict) -> str:
    if kind == "import_incomplete":
        return f"{LACKS}{data.get('missing')} track(s) of its own release"
    return data.get("reason") or UNKNOWN


def outcomes(rows: list[tuple[datetime, str, dict]]) -> dict[str, dict]:
    """What *rows* say of each GUID: its ``title`` and ``size``, and the
    ``reason`` and time (``at``) of its latest outcome.  The album has one grab
    in flight at a time, so an outcome with no GUID is that grab's, and only
    the first after it is: a later one, its grab's event unwritten, is left out."""
    found: dict[str, dict] = {}
    grab: dict | None = None
    for at, kind, data in rows:
        guid = data.get("guid") or None
        if kind == "grab":
            grab = data
            found.setdefault(guid, {}).update(title=data.get("release"), size=data.get("size"))
            continue
        if guid is None and grab is not None and data.get("what") != "grab":
            guid = grab.get("guid")
        if guid is None:
            continue
        if grab is not None and grab.get("guid") == guid:
            grab = None
        entry = found.setdefault(guid, {})
        entry.update(reason=_reason(kind, data), at=at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        if data.get("release"):
            entry.setdefault("title", data["release"])
    return found


def entries(record: dict, rows: list[tuple[datetime, str, dict]] | None) -> list[dict]:
    """The record's blocklist, in its order, each entry its ``guid``, ``title``,
    ``size``, ``reason`` and ``at``, its :func:`kind` and ``every_copy``,
    whether it blocks every copy of its title (:func:`blocked`).  What the
    events lack stays None and the reason :data:`UNKNOWN`, but the record's
    last candidate and kept copy name themselves.  *rows* None: the event store
    couldn't be read."""
    found = {} if rows is None else outcomes(rows)
    held = {c["guid"]: c for c in (record.get("best_copy"), record.get("candidate")) if c and c.get("guid")}
    out = []
    for guid in record.get("blocklist") or ():
        known, copy = found.get(guid, {}), held.get(guid, {})
        entry = {"guid": guid, "title": known.get("title") or copy.get("title"),
                 "size": known.get("size") or copy.get("size"),
                 "reason": known.get("reason") or (UNREADABLE if rows is None else UNKNOWN),
                 "at": known.get("at")}
        entry["kind"] = kind(entry)
        out.append(entry)
    dead = Counter(_name(e) for e in out if e["kind"] == DEAD_DOWNLOAD and _name(e))
    for e in out:
        e["every_copy"] = bool(_name(e)) and (
            e["kind"] == WRONG_RELEASE or e["kind"] == DEAD_DOWNLOAD and dead[_name(e)] >= DEAD_COPIES)
    return out


def _name(entry: dict) -> str:
    return repost_name(entry["title"]) if entry["title"] else ""


def kind(entry: dict) -> str | None:
    """:data:`WRONG_RELEASE` when *entry*'s reason is a verdict on what the
    release held (:data:`VERDICTS`), :data:`DEAD_DOWNLOAD` for any other
    failure, and None when the reason isn't known."""
    reason = entry["reason"]
    if reason.startswith(VERDICTS):
        return WRONG_RELEASE
    if reason == UNKNOWN or reason.startswith(f"{UNKNOWN}:"):
        return None
    return DEAD_DOWNLOAD


def blocked(listed: list[dict]) -> Copies:
    """The releases the blocklist *listed* (:func:`entries`) keeps the tick from
    grabbing: every entry's GUID, and every copy of the entries whose
    ``every_copy`` is set (homelab#2757)."""
    copies = Copies()
    for e in listed:
        copies.add(e["guid"], e.get("title") if e.get("every_copy") else None)
    return copies


def stays(entry: dict) -> bool:
    """Whether a reset leaves *entry* on the blocklist (homelab#2661).  Only a
    :data:`WRONG_RELEASE` comes off, another album or an incomplete copy, as
    such a release can be wanted after all.  Any other failure stays, a broken
    download above all: it would fail the same way, an attempt each time, and
    the album is better missing than broken.  So does an entry whose reason
    isn't known."""
    return kind(entry) != WRONG_RELEASE


def reach(entry: dict) -> str:
    """What *entry* blocks, in words: its kind, and which copies."""
    if entry.get("kind") is None:
        return "why isn't known, so only this copy"
    if entry["every_copy"]:
        if entry["kind"] == WRONG_RELEASE:
            return "a wrong release, so every copy of it, on any indexer"
        return "a dead download, a second dead copy of its title, so every copy of it"
    if entry["kind"] == WRONG_RELEASE:
        return "a wrong release, but only this copy: its title isn't recorded"
    return "a dead download, so only this copy: another indexer's copy may be tried once"


def describe(entry: dict) -> str:
    """One entry as a dry run lists it: what it is, why it was blocklisted, and
    what it blocks (:func:`reach`)."""
    size = f" ({entry['size'] / 1e6:.0f} MB)" if entry["size"] else ""
    return f"{entry['title'] or entry['guid']}{size}: {entry['reason']} ({reach(entry)})"
