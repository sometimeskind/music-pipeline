"""The Music MCP's history reads (homelab#2413): ``album_history``, ``find_events`` and ``digest_summary``.

History is the event store's (:mod:`music_fetch.events`), opened for each
request.  ``digest_summary``'s library issues come from the album store,
beets, the last scan's empty slots and quarantine, so they still answer while
the database is down: only its ``history`` section then holds an error.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from music_service import mcp_reads

logger = logging.getLogger(__name__)

# Entries listed per list; every list carries its total.
LISTED = 25
# A grab SABnzbd hasn't finished, and an album spotdl hasn't filled, after this long is stuck.
# A download takes minutes and a MusicBrainz outage's deferrals about 7 h, so 12 h names a
# stalled SABnzbd (its URL grabber hung for 23 h, homelab#2516) in the next digest.
STALE_GRAB = timedelta(hours=12)
STALE_FALLBACK = timedelta(days=14)

# The kinds whose events digest_summary lists with their item and data: the digest
# judges them (a merge or move between unrelated recordings or albums, an extra left by hand,
# a recording ID AcoustID replaced, homelab#2543, or filled on a request's item, homelab#2589). An album_merged event's data.absorbed names
# the records two editions merged from, whose tracks canon then moves: expected, not a
# likely problem (homelab#2607).
WITH_DATA = ("merge", "moved", "local", "recording_replaced", "recording_set", "recording_filled", "album_merged")

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


def album_refs(key: str, record: dict) -> list[str]:
    """What the album's events may name it by: its key now (a row id since
    homelab#2478), every Spotify album ID and local name it has, which were its
    keys before."""
    return sorted({key, *(record.get("spotify_album_ids") or ()), *(record.get("names") or ())})


def uuid_or_none(value: str) -> str | None:
    """*value* as a release group MBID, or None: a dropped album is looked up by the
    key or Spotify ID it was known by, or its release group."""
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def album_history(conn, refs: list[str], release_group: str | None, since: str | None) -> list[dict]:
    """The album's events, newest first: by one of its Spotify IDs, its release
    group, or the key an event recorded (*refs*, :func:`album_refs`)."""
    rows = conn.execute(
        f"SELECT {_FIELDS} FROM events"  # noqa: S608 — _FIELDS is a constant
        " WHERE (spotify_album_id = ANY(%(refs)s) OR release_group = %(group)s::uuid OR data->>'key' = ANY(%(refs)s))"
        "   AND (%(since)s::timestamptz IS NULL OR at >= %(since)s::timestamptz)"
        " ORDER BY at DESC, id DESC",
        {"refs": list(refs), "group": release_group, "since": since}).fetchall()
    return [event_view(r) for r in rows]


# ---------------------------------------------------------------------------
# find_events (homelab#2590)
# ---------------------------------------------------------------------------

# The window find_events reads without a since.
FIND_DAYS = 7

# Every filter rides the (kind, at) index; item_id and source only narrow what it finds.
_FIND = (" FROM events WHERE kind = ANY(%(kinds)s)"
         "   AND at >= %(since)s::timestamptz AND at < %(until)s::timestamptz"
         "   AND (%(item_id)s::integer IS NULL OR item_id = %(item_id)s::integer)"
         "   AND (%(source)s::text IS NULL OR source = %(source)s::text)")


def find_events(conn, kinds: list[str], since: str, until: str, *, item_id: int | None = None,
                source: str | None = None, offset: int = 0, limit: int = mcp_reads.PAGE_ENTRIES,
                data: bool = False) -> tuple[dict[str, int], list[dict]]:
    """The events of *kinds* in [*since*, *until*): each kind's count over the whole
    window, and up to *limit* events from *offset*, newest first.  An event names its
    album by ``data.key`` (the album store's key), its Spotify album ID and release
    group; *data* keeps the rest of ``data``, which makes the output large."""
    params = {"kinds": list(kinds), "since": since, "until": until, "item_id": item_id, "source": source}
    counts = dict(conn.execute("SELECT kind, count(*)" + _FIND + " GROUP BY kind ORDER BY kind",
                               params).fetchall())
    rows = conn.execute(
        f"SELECT {_FIELDS}" + _FIND + " ORDER BY at DESC, id DESC LIMIT %(limit)s OFFSET %(offset)s",  # noqa: S608 — constants
        {**params, "limit": limit, "offset": offset}).fetchall()
    return counts, [found_view(r, data) for r in rows]


def found_view(row, with_data: bool) -> dict:
    view = event_view(row)
    extra = view.pop("data") or {}
    key = extra.get("key") if isinstance(extra, dict) else None
    out = {"at": view.pop("at"), "kind": view.pop("kind"), "source": view.pop("source"),
           "key": None if key is None else str(key), **view}
    if with_data:
        out["data"] = extra
    return out


def find_since_default(now: datetime) -> str:
    return _utc(now - timedelta(days=FIND_DAYS))


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
        if view["kind"] in WITH_DATA:
            brief.update(item_id=view["item_id"], data=view["data"])
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


def requests(state, rejected: Path) -> dict:
    """The album requests (homelab#2478) whose album ended ``missing`` or
    ``failed``, or is in ``fallback`` with tracks spotdl hasn't got (each
    entry's ``album.fallback_left``, with ``fallback_rejected`` and
    ``acoustid_unknown_only`` read from the rejects in *rejected*,
    homelab#2546), and an ``error`` when the database can't be read."""
    import psycopg  # noqa: PLC0415

    from music_fetch import album_requests  # noqa: PLC0415
    from music_fetch import state as statedb  # noqa: PLC0415
    from music_scan.guard import request_rejects  # noqa: PLC0415

    out: dict[str, list[dict]] = {"missing": [], "failed": [], "fallback": []}
    try:
        with statedb.session() as conn:
            found = album_requests.listing(conn, state, rejects=request_rejects(rejected))
    except (statedb.Unavailable, psycopg.Error) as exc:
        return {"error": f"the music database: {type(exc).__name__}", **{k: listed(v) for k, v in out.items()}}
    for entry in found:
        if entry["status"] in out:
            out[entry["status"]].append(entry)
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
    library = Recordings(lib.identities(("spotify_ids", "spotify_url", "isrc", "mb_trackid", MERGED_FILES, "length",
                                         "title", "artist", "albumartist")))
    _, only = release.split_replaced(release.audio_files(replaced), library)
    return listed([str(f.relative_to(replaced.parent)) for f in only])


def path_clashes(lib: mcp_reads.Library) -> dict:
    """The album records' items canon tagged in place, because another item holds their path (#306)."""
    from music_scan.canon import CLASH  # noqa: PLC0415

    ids = lib.ids_where(CLASH, "1")
    return listed([{"id": i["id"], "path": i["path"], "album": i.get("album")} for i in lib.items(ids)])


def empty_slots() -> dict:
    """The last scan's empty playlist slots (the ``empty-slots`` document), playlists with none left out."""
    from music_fetch import state  # noqa: PLC0415

    try:
        report = state.load_doc(state.EMPTY_SLOTS)
    except state.Unavailable:
        logger.warning("Could not read the empty slots", exc_info=True)
        return {"error": "the music database is unavailable"}
    if report is None:
        return {"at": None, "playlists": {}}
    return {"at": report.get("at"),
            "playlists": {n: p for n, p in (report.get("playlists") or {}).items() if p.get("total")}}


def rejects(quarantine: Path) -> dict:
    """The rejected downloads still in ``quarantine/rejected/``: the album
    request fallback's (homelab#2546), and any the dropped length guard left (#165)."""
    return listed([f["path"] for f in mcp_reads.quarantine_files(quarantine, "rejected", None)])


def since_default(now: datetime) -> str:
    return _utc(now - timedelta(hours=24))
