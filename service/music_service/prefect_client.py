"""Sync helpers for submitting Prefect deployment runs.

When PREFECT_API_URL is set, runs are submitted to the Prefect server and
appear in the UI. Without it (integration tests, standalone compose without
the prefect-server service), flows are executed directly in daemon threads
using the same concurrency lock semantics as the old orchestrator.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def _has_server() -> bool:
    return bool(os.environ.get("PREFECT_API_URL"))


# ---------------------------------------------------------------------------
# Prefect API path (PREFECT_API_URL is set)
# ---------------------------------------------------------------------------


async def _submit_deployment(name: str, parameters: dict | None = None) -> None:
    from prefect.deployments import run_deployment
    await run_deployment(name, parameters=parameters, timeout=0)


def _via_api(deployment_name: str) -> bool:
    try:
        asyncio.run(_submit_deployment(deployment_name))
        return True
    except Exception as exc:
        logger.error("Failed to submit %s: %s", deployment_name, exc)
        return False


# ---------------------------------------------------------------------------
# Direct in-process path (no PREFECT_API_URL)
# ---------------------------------------------------------------------------


def _run_fetch_and_scan() -> None:
    import music_fetch.ingest as ingest
    try:
        logger.info("==> Fetch starting")
        ingest.run()
        logger.info("==> Fetch complete")
    except Exception:
        logger.exception("fetch failed")
    finally:
        _lock.release()


def _run_scan() -> None:
    import music_scan.scan as scan
    try:
        logger.info("==> Scan starting")
        scan.run()
        logger.info("==> Scan complete")
    except Exception:
        logger.exception("scan failed")
    finally:
        _lock.release()


def _direct(target) -> bool:
    """Non-blocking: acquire lock and run *target* in a daemon thread.

    If a run is already in progress the new request is silently dropped and
    True is returned (caller gets 202 — same semantics as Prefect queuing).
    """
    if not _lock.acquire(blocking=False):
        logger.info("Pipeline busy — ignoring duplicate trigger")
        return True
    try:
        threading.Thread(target=target, daemon=True).start()
    except Exception:
        _lock.release()
        raise
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def trigger_fetch() -> bool:
    """Submit a fetch run. Returns True if submitted/accepted."""
    if _has_server():
        return _via_api("music-fetch/music-fetch")
    return _direct(_run_fetch_and_scan)


def trigger_scan() -> bool:
    """Submit a scan run. Fire-and-forget; True if submitted/accepted."""
    if _has_server():
        try:
            asyncio.run(_submit_deployment("music-scan/music-scan"))
            return True
        except Exception as exc:
            logger.error("Failed to submit scan: %s", exc)
            return False
    return _direct(_run_scan)


SCAN_DEPLOYMENT = "music-scan/music-scan"
ALBUMS_DEPLOYMENT = "music-albums/music-albums"

_album_tick_lock = threading.Lock()


async def _pending_state(deployment_name: str) -> str | None:
    """The state of a run of *deployment_name* that is pending or running: a
    scheduled run counts once it is due, so the next interval run doesn't."""
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415

    from prefect import get_client  # noqa: PLC0415
    from prefect.client.schemas.filters import (  # noqa: PLC0415
        FlowRunFilter, FlowRunFilterDeploymentId, FlowRunFilterState, FlowRunFilterStateType)
    from prefect.client.schemas.objects import StateType  # noqa: PLC0415

    async with get_client() as client:
        deployment = await client.read_deployment_by_name(deployment_name)
        runs = await client.read_flow_runs(
            flow_run_filter=FlowRunFilter(
                deployment_id=FlowRunFilterDeploymentId(any_=[deployment.id]),
                state=FlowRunFilterState(type=FlowRunFilterStateType(
                    any_=[StateType.SCHEDULED, StateType.PENDING, StateType.RUNNING])),
            ),
            limit=50,
        )
    due = datetime.now(timezone.utc) + timedelta(seconds=5)
    for run in runs:
        state = run.state_type
        if state != StateType.SCHEDULED or (run.expected_start_time and run.expected_start_time <= due):
            return state.value
    return None


def pending_state(deployment_name: str) -> str | None:
    """A pending or running run's state for *deployment_name* (the MCP's
    triggers, homelab#2412), or None.  Direct mode: the in-process locks."""
    if _has_server():
        return asyncio.run(_pending_state(deployment_name))
    lock = _lock if deployment_name == SCAN_DEPLOYMENT else _album_tick_lock
    return "RUNNING" if lock.locked() else None


def _run_album_tick() -> None:
    from music_service.flows import albums_flow  # noqa: PLC0415
    try:
        albums_flow()
    except Exception:
        logger.exception("album tick failed")
    finally:
        _album_tick_lock.release()


def trigger_album_tick() -> bool:
    """Submit an album tick (``music-albums``). Returns True if submitted/accepted."""
    if _has_server():
        return _via_api(ALBUMS_DEPLOYMENT)
    if not _album_tick_lock.acquire(blocking=False):
        return True
    threading.Thread(target=_run_album_tick, daemon=True).start()
    return True


def trigger_album_import(params: dict) -> bool:
    """Submit an album-import run for one finished SABnzbd job. Returns True if submitted.

    Runs are queued, never dropped: each one is a different job, and the flow
    waits for the albums lock itself.
    """
    if _has_server():
        try:
            asyncio.run(_submit_deployment("music-album-import/music-album-import", params))
            return True
        except Exception as exc:
            logger.error("Failed to submit album import for %s: %s", params.get("nzo_id"), exc)
            return False
    from music_service.flows import album_import_flow
    threading.Thread(target=album_import_flow, kwargs=params, daemon=True).start()
    return True


async def _upsert_limits() -> None:
    from prefect import get_client
    async with get_client() as client:
        await client.upsert_global_concurrency_limit_by_name(
            name="beet-import",
            limit=1,
        )
        await client.upsert_global_concurrency_limit_by_name(
            name="pipeline",
            limit=1,
        )
        await client.upsert_global_concurrency_limit_by_name(
            name="albums",
            limit=1,
        )


def ensure_concurrency_limits() -> None:
    """Create/update Prefect global concurrency limits required by the flows.

    No-op when PREFECT_API_URL is not set (direct/test mode uses a threading
    lock instead).
    """
    if not _has_server():
        return
    try:
        asyncio.run(_upsert_limits())
        logger.info("Prefect concurrency limits ensured: beet-import=1, pipeline=1, albums=1")
    except Exception as exc:
        logger.warning("Could not upsert Prefect concurrency limit: %s", exc)
