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
"""

from __future__ import annotations

from datetime import datetime, timezone

UNKNOWN = "unknown"
UNREADABLE = "unknown: the event store can't be read"

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
        return f"lacks {data.get('missing')} track(s) of its own release"
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
    ``size``, ``reason`` and ``at``.  What the events lack stays None and the
    reason :data:`UNKNOWN`, but the record's last candidate and kept copy name
    themselves.  *rows* None: the event store couldn't be read."""
    found = {} if rows is None else outcomes(rows)
    held = {c["guid"]: c for c in (record.get("best_copy"), record.get("candidate")) if c and c.get("guid")}
    out = []
    for guid in record.get("blocklist") or ():
        known, copy = found.get(guid, {}), held.get(guid, {})
        out.append({"guid": guid, "title": known.get("title") or copy.get("title"),
                    "size": known.get("size") or copy.get("size"),
                    "reason": known.get("reason") or (UNREADABLE if rows is None else UNKNOWN),
                    "at": known.get("at")})
    return out


def describe(entry: dict) -> str:
    """One entry as a dry run lists it: what it is, and why it was blocklisted."""
    size = f" ({entry['size'] / 1e6:.0f} MB)" if entry["size"] else ""
    return f"{entry['title'] or entry['guid']}{size}: {entry['reason']}"
