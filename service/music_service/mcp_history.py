"""The Music MCP's history reads (homelab#2413): ``album_history`` and ``digest_summary``.

History is the event store's (:mod:`music_fetch.events`), opened for each
request.  ``digest_summary``'s library issues come from the album store,
beets, the last scan's empty slots and quarantine, so they still answer while
the database is down: only its ``history`` section then holds an error.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from music_service import mcp_reads

logger = logging.getLogger(__name__)

# Entries listed per list; every list carries its total.
LISTED = 25
# A grab SABnzbd hasn't finished, and an album spotdl hasn't filled, after this long is stuck.
STALE_GRAB = timedelta(days=2)
STALE_FALLBACK = timedelta(days=14)

_FIELDS = "id, at, kind, source, spotify_album_id, release_group::text, item_id, caller, job_id, message, data"


class Unavailable(Exception):
    """The event store can't be read: unset, or the database is down."""


def connect():
    from music_fetch import db  # noqa: PLC0415

    if not db.configured():
        raise Unavailable(f"the event store is not configured ({db.ENV} is unset)")
    try:
        return db.connect()
    except Exception as exc:
        raise Unavailable(f"the event store is unavailable: {type(exc).__name__}") from exc


def _utc(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def event_view(row) -> dict:
    _, at, kind, source, sid, group, item_id, caller, job_id, message, data = row
    return {"at": _utc(at), "kind": kind, "source": source, "spotify_album_id": sid, "release_group": group,
            "item_id": item_id, "caller": caller, "job_id": job_id, "message": message, "data": data}


def listed(entries: list) -> dict:
    return {"total": len(entries), "listed": entries[:LISTED]}


# ---------------------------------------------------------------------------
# album_history
# ---------------------------------------------------------------------------


def album_group(record: dict) -> str | None:
    """The album's release group, once an import identified its release (``albums._release_group``)."""
    from music_fetch.albums import _release_group  # noqa: PLC0415

    return _release_group(record)


def album_history(conn, key: str, release_group: str | None, since: str | None) -> list[dict]:
    """The album's events, newest first: by its Spotify ID, its release group, or
    (a local files' album) its store key."""
    rows = conn.execute(
        f"SELECT {_FIELDS} FROM events"  # noqa: S608 — _FIELDS is a constant
        " WHERE (spotify_album_id = %(key)s OR release_group = %(group)s::uuid OR data->>'key' = %(key)s)"
        "   AND (%(since)s::timestamptz IS NULL OR at >= %(since)s::timestamptz)"
        " ORDER BY at DESC, id DESC",
        {"key": key, "group": release_group, "since": since}).fetchall()
    return [event_view(r) for r in rows]


# ---------------------------------------------------------------------------
# digest_summary
# ---------------------------------------------------------------------------


def history(conn, since: str) -> dict:
    """Every kind's count since *since*, and each kind's latest :data:`LISTED` events, oldest first."""
    counts = dict(conn.execute("SELECT kind, count(*) FROM events WHERE at >= %(since)s::timestamptz "
                               "GROUP BY kind ORDER BY kind", {"since": since}).fetchall())
    rows = conn.execute(
        f"SELECT {_FIELDS} FROM ("  # noqa: S608 — _FIELDS is a constant
        f"  SELECT *, row_number() OVER (PARTITION BY kind ORDER BY at DESC, id DESC) AS n"
        f"    FROM events WHERE at >= %(since)s::timestamptz) latest"
        " WHERE n <= %(listed)s ORDER BY kind, at, id",
        {"since": since, "listed": LISTED}).fetchall()
    events: dict[str, list[dict]] = {}
    for row in rows:
        view = event_view(row)
        brief = {"at": view["at"], "message": view["message"]}
        if view["kind"] in ("failure", "import_done") and "gave_up" in view["data"]:
            brief["gave_up"] = view["data"]["gave_up"]
        events.setdefault(view["kind"], []).append(brief)
    return {"counts": counts, "events": events}


def _album(key: str, record: dict, **extra) -> dict:
    return {"key": key, "album": f"{record.get('artist')} — {record.get('name')}",
            "origin": record.get("origin") or "album", **extra}


def _older(value: str | None, age: timedelta, now: datetime) -> bool:
    if not value:
        return False
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        return False
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return now - t > age


def stuck(records: dict[str, dict], now: datetime) -> dict:
    """Albums that don't move on by themselves: failed and missing ones (a feed's,
    which never fall back), grabs SABnzbd hasn't finished in :data:`STALE_GRAB`,
    and fallbacks spotdl hasn't filled in :data:`STALE_FALLBACK`."""
    from music_fetch import albums  # noqa: PLC0415

    out: dict[str, list[dict]] = {"failed": [], "missing": [], "grabbed": [], "fallback": []}
    for key, r in sorted(records.items(), key=lambda kv: (str(kv[1].get("artist")), str(kv[1].get("name")))):
        status = r.get("status")
        if status == albums.FAILED:
            out["failed"].append(_album(key, r, attempts=r.get("attempts", 0)))
        elif status == albums.MISSING:
            out["missing"].append(_album(key, r, searched_at=r.get("searched_at")))
        elif status == albums.GRABBED and _older(r.get("grabbed_at"), STALE_GRAB, now):
            out["grabbed"].append(_album(key, r, grabbed_at=r.get("grabbed_at")))
        elif status == albums.FALLBACK and _older(r.get("fallback_at"), STALE_FALLBACK, now):
            out["fallback"].append(_album(key, r, fallback_at=r.get("fallback_at"),
                                          tracks=sum(len(t) for t in (r.get("fallback_tracks") or {}).values())))
    return {k: listed(v) for k, v in out.items()}


def mixed(state) -> dict:
    """The imported albums mixed from several releases at the last album tick (#288)."""
    return listed([_album(k, state.albums[k]) for k in state.mixed if k in state.albums])


def only_copies(lib: mcp_reads.Library, replaced: Path) -> dict:
    """The files in ``quarantine/replaced/`` that hold a recording the library
    doesn't (``release.split_replaced``): never purged, the operator decides."""
    from music_scan import release  # noqa: PLC0415
    from music_scan.identity import MERGED_FILES, Recordings  # noqa: PLC0415

    if not replaced.is_dir():
        return listed([])
    library = Recordings(lib.identities(("spotify_ids", "spotify_url", "isrc", "mb_trackid", MERGED_FILES)))
    _, only = release.split_replaced(release.audio_files(replaced), library)
    return listed([str(f.relative_to(replaced.parent)) for f in only])


def path_clashes(lib: mcp_reads.Library) -> dict:
    """The album records' items canon tagged in place, because another item holds their path (#306)."""
    from music_scan.canon import CLASH  # noqa: PLC0415

    ids = lib.ids_where(CLASH, "1")
    return listed([{"id": i["id"], "path": i["path"], "album": i.get("album")} for i in lib.items(ids)])


def empty_slots(path: Path) -> dict:
    """The last scan's empty playlist slots (``scan.EMPTY_SLOTS_NAME``), playlists with none left out."""
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"at": None, "playlists": {}}
    except (OSError, ValueError) as exc:
        return {"error": f"{path.name} is unreadable: {exc}"}
    return {"at": report.get("at"),
            "playlists": {n: p for n, p in (report.get("playlists") or {}).items() if p.get("total")}}


def rejects(quarantine: Path) -> dict:
    """The length guard's rejects still in ``quarantine/rejected/`` (#165)."""
    return listed([f["path"] for f in mcp_reads.quarantine_files(quarantine, "rejected", None)])


def since_default(now: datetime) -> str:
    return _utc(now - timedelta(hours=24))
