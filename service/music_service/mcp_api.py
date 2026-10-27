"""The ``/mcp-api/*`` routes for the Music MCP adapter (homelab#2412); the contract is ``service/MCP_API.md``.

``auth.setup_auth`` has checked ``MCP_API_TOKEN`` before these run; this
blueprint checks ``X-MCP-Caller`` and, on write routes, ``X-MCP-Tier``.
"""

from __future__ import annotations

import functools
import logging
import os
from pathlib import Path

from flask import Blueprint, g, jsonify, request

from music_service import mcp_jobs, mcp_reads
from music_service.mcp_tools import TOOLS, WRITE, Ambiguous, BadRequest, NotFound, NotImplementedYet

logger = logging.getLogger("music_service.mcp_jobs")  # one logger for every [MCP] line

DEFAULT_INLINE_SECONDS = 20.0


def inline_seconds() -> float:
    try:
        return float(os.environ.get("MCP_INLINE_SECONDS", "") or DEFAULT_INLINE_SECONDS)
    except ValueError:
        return DEFAULT_INLINE_SECONDS


def _offset() -> int:
    try:
        return max(0, int(request.args.get("offset", 0)))
    except ValueError:
        raise BadRequest("offset must be a non-negative integer") from None


def make_blueprint(jobs: mcp_jobs.JobQueue) -> Blueprint:
    from music_fetch.albums import STATE_FILE, State  # noqa: PLC0415
    from music_scan.library import LIBRARY_DB, LIBRARY_DIR  # noqa: PLC0415

    bp = Blueprint("mcp", __name__, url_prefix="/mcp-api")
    quarantine = Path(os.environ.get("MUSIC_QUARANTINE", "/root/Music/quarantine"))

    def records() -> dict[str, dict]:
        return State.load(STATE_FILE).albums

    def library() -> mcp_reads.Library:
        return mcp_reads.Library(LIBRARY_DB, LIBRARY_DIR)

    @bp.before_request
    def headers():
        caller = request.headers.get("X-MCP-Caller", "").strip()
        if not caller or len(caller) > 200:
            return jsonify({"error": "X-MCP-Caller required"}), 400
        tier = request.headers.get("X-MCP-Tier", "").strip()
        if tier not in ("", "read", "write"):
            return jsonify({"error": "X-MCP-Tier must be read or write"}), 400
        g.caller, g.tier = caller, tier or "read"
        return None

    def write_tier(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            if g.tier != WRITE:
                return jsonify({"error": "write tier required"}), 403
            return view(*args, **kwargs)
        return wrapped

    @bp.errorhandler(BadRequest)
    def bad_request(exc):
        return jsonify({"error": str(exc)}), 400

    @bp.errorhandler(mcp_reads.LibraryBusy)
    def busy_library(_):
        return jsonify({"status": "busy", "retry": True, "error": "beets library is locked"}), 503

    @bp.errorhandler(Ambiguous)
    def ambiguous(exc):
        return jsonify({"status": "ambiguous", "query": exc.query, "candidates": exc.candidates}), 200

    @bp.errorhandler(NotFound)
    def not_found(exc):
        return jsonify({"status": "not_found", "query": exc.query}), 404

    def read(tool: str, **detail) -> None:
        logger.info("[MCP] read tool=%s caller=%s tier=%s %s", tool, g.caller, g.tier,
                    " ".join(f"{k}={v!r}" for k, v in detail.items()))

    # ------------------------------------------------------------------
    # Structured reads
    # ------------------------------------------------------------------

    @bp.get("/library_status")
    def library_status():
        read("library_status")
        from music_fetch import store  # noqa: PLC0415

        with library() as lib:
            items = lib.count()
        albums = records()
        by_status: dict[str, int] = {}
        for record in albums.values():
            by_status[record.get("status", "?")] = by_status.get(record.get("status", "?"), 0) + 1
        active = jobs.active()
        running = next((j for j in active if j["status"] != mcp_jobs.QUEUED), None)
        return jsonify({"status": "ok", "items": items,
                        "albums": {"total": len(albums), "by_status": by_status},
                        "album_mode": os.environ.get("ALBUM_MODE", "off") or "off",
                        "quarantine": mcp_reads.quarantine_counts(quarantine),
                        "playlists": store.names(),
                        "jobs": {"running": running,
                                 "queued": sum(j["status"] == mcp_jobs.QUEUED for j in active),
                                 "depth": jobs.depth}})

    @bp.get("/albums")
    def find_albums():
        query = request.args.get("query", "").strip()
        if not query:
            raise BadRequest("query required")
        read("find_albums", query=query)
        with library() as lib:
            found = mcp_reads.find_albums(lib, records(), query)
        return jsonify({"status": "ok", "query": query, "albums": found})

    @bp.get("/album")
    def get_album():
        from music_service.mcp_tools import resolve_album  # noqa: PLC0415

        query = request.args.get("query", "")
        read("get_album", query=query)
        albums = records()
        key = resolve_album(query, lambda: albums)
        with library() as lib:
            return jsonify({"status": "ok", "query": query.strip(), **mcp_reads.get_album(lib, key, albums[key])})

    @bp.get("/items/<int:item_id>")
    def get_item(item_id: int):
        read("get_item", id=item_id)
        from music_scan.scan import store_listings  # noqa: PLC0415

        with library() as lib:
            found = lib.items([item_id])
        if not found:
            return jsonify({"status": "not_found", "id": item_id}), 404
        return jsonify({"status": "ok", "item": mcp_reads.item_view(found[0]),
                        "listed_on": sorted(store_listings()(found[0]))})

    @bp.get("/quarantine")
    def list_quarantine():
        kind = request.args.get("kind", "")
        if kind not in mcp_reads.QUARANTINE_KINDS:
            raise BadRequest(f"kind must be one of {', '.join(mcp_reads.QUARANTINE_KINDS)}")
        query = request.args.get("query") or None
        offset = _offset()
        read("list_quarantine", kind=kind, query=query)
        files = mcp_reads.quarantine_files(quarantine, kind, query)
        shown, next_offset = mcp_reads.page(files, offset)
        return jsonify({"status": "ok", "kind": kind, "query": query, "total": len(files), "offset": offset,
                        "next_offset": next_offset, "files": shown})

    @bp.get("/playlists/<name>")
    def get_playlist(name: str):
        from music_fetch import store  # noqa: PLC0415

        offset = _offset()
        read("get_playlist", name=name)
        playlist = store.load(name) if name in store.names() else None
        if playlist is None:
            return jsonify({"status": "not_found", "name": name}), 404
        with library() as lib:
            entries = mcp_reads.playlist_entries(lib, playlist)
        resolved = sum(e["item_id"] is not None for e in entries)
        shown, next_offset = mcp_reads.page(entries, offset)
        return jsonify({"status": "ok", "name": playlist.name, "updated": playlist.updated or None,
                        "source": playlist.source or None,
                        "counts": {"entries": len(entries), "resolved": resolved, "missing": len(entries) - resolved},
                        "offset": offset, "next_offset": next_offset, "entries": shown})

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    def job_view(job: dict, offset: int = 0) -> dict:
        finished = job["status"] not in mcp_jobs.ACTIVE
        lines = jobs.output(job["job_id"])
        output, next_offset = mcp_jobs.page(lines, min(offset, len(lines)), finished)
        return {**job, "output": output, "offset": offset, "next_offset": next_offset, "total_lines": len(lines)}

    @bp.get("/jobs/<job_id>")
    def get_job(job_id: str):
        offset = _offset()
        job = jobs.get(job_id)
        if job is None:
            return jsonify({"status": "unknown", "error": "unknown (pruned or lost to a restart)"}), 404
        return jsonify(job_view(job, offset))

    @bp.post("/run/<tool_name>")
    def run(tool_name: str):
        tool = TOOLS.get(tool_name)
        if tool is None:
            return jsonify({"error": f"unknown tool {tool_name}"}), 404
        if tool.tier == WRITE and g.tier != WRITE:
            return jsonify({"error": "write tier required"}), 403
        body = request.get_json(silent=True)
        if body is None and request.get_data():
            raise BadRequest("the body must be a JSON object")
        if not isinstance(body, (dict, type(None))):
            raise BadRequest("the body must be a JSON object")
        body = dict(body or {})
        apply = body.pop("apply", False)
        if not isinstance(apply, bool) or (apply and tool.tier != WRITE):
            raise BadRequest("apply must be true or false, and only a write tool takes it")
        try:
            args = tool.normalise(body, records)
        except NotImplementedYet as exc:
            return jsonify({"status": "not_implemented", "error": str(exc)}), 501

        try:
            if apply:
                dry = jobs.find_dry_run(tool.name, args)
                if dry is None:
                    return jsonify({"status": "refused", "reason": "no_dry_run", "args": args,
                                    "error": f"run {tool.name} with apply=false first (same args, within 15 min)"}), 409
                job = jobs.submit(tool.name, args, "apply", g.caller, g.tier, tool.commands(args, False),
                                  apply_commands=tool.commands(args, True), expected_hash=dry["dry_run_hash"],
                                  locks=tool.locks)
            else:
                job = jobs.submit(tool.name, args, "dry_run", g.caller, g.tier, tool.commands(args, False))
        except mcp_jobs.Busy as exc:
            return jsonify({"status": "busy", "queue": exc.queue}), 503

        job = jobs.wait(job["job_id"], inline_seconds()) or job
        return jsonify(job_view(job)), (202 if job["status"] in mcp_jobs.ACTIVE else 200)

    # ------------------------------------------------------------------
    # Triggers
    # ------------------------------------------------------------------

    def trigger(flow: str, deployment: str, submit):
        from music_service import prefect_client  # noqa: PLC0415

        logger.info("[MCP] trigger flow=%s caller=%s tier=%s", flow, g.caller, g.tier)
        try:
            state = prefect_client.pending_state(deployment)
        except Exception as exc:  # noqa: BLE001 — the Prefect server is unreachable
            logger.error("[MCP] could not read %s's runs: %s", flow, exc)
            return jsonify({"status": "failed", "error": "failed to submit run — is the Prefect server reachable?"}), 503
        if state is not None:
            return jsonify({"status": "already_pending", "flow": flow, "state": state})
        if not submit():
            return jsonify({"status": "failed", "error": "failed to submit run — is the Prefect server reachable?"}), 503
        return jsonify({"status": "submitted", "flow": flow})

    @bp.post("/trigger/scan")
    @write_tier
    def trigger_scan():
        from music_service import prefect_client  # noqa: PLC0415
        return trigger("music-scan", prefect_client.SCAN_DEPLOYMENT, prefect_client.trigger_scan)

    @bp.post("/trigger/album_tick")
    @write_tier
    def trigger_album_tick():
        from music_service import prefect_client  # noqa: PLC0415
        return trigger("music-albums", prefect_client.ALBUMS_DEPLOYMENT, prefect_client.trigger_album_tick)

    return bp
