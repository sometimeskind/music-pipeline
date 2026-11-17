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

# How long after a busy scan with files waiting that could not wait for the pipeline
# (homelab#2761) the retry runs (homelab#2751).
SCAN_RETRY_SECONDS = 5 * 60

# How long after a scan that a MusicBrainz outage left files in the inbox (#264)
# its retry runs (#508).  Longer than a busy scan's: an outage lasts hours (503s
# from 00:05 to 03:38 UTC on 2026-11-16), MusicBrainz answers 503 to load as
# well as when down, and each retry is a whole scan holding ``pipeline``, so 5
# minutes would run some 40 scans into one outage to save a few minutes after it.
SCAN_OUTAGE_RETRY_SECONDS = 30 * 60


def schedule_scan(delay: int, idempotency_key: str | None = None) -> bool:
    """Schedule a ``music-scan`` run *delay* seconds from now, unless one is
    already waiting: a run scheduled or pending (a retry, or a trigger yet to
    start) tries the pipeline itself and schedules the next if it is still busy,
    so at most one waits (homelab#2751).  A running scan waiting for the
    pipeline isn't counted here: ``flows.SCAN_WAITER_SLOT`` keeps those to one
    (homelab#2761).  Returns whether this call scheduled one.  Needs a
    Prefect server, and raises when it can't be reached."""
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415

    from prefect import get_client  # noqa: PLC0415
    from prefect.client.schemas.filters import (  # noqa: PLC0415
        FlowRunFilter, FlowRunFilterDeploymentId, FlowRunFilterState, FlowRunFilterStateType)
    from prefect.client.schemas.objects import StateType  # noqa: PLC0415
    from prefect.states import Scheduled  # noqa: PLC0415

    now = datetime.now(timezone.utc)
    with get_client(sync_client=True) as client:
        deployment = client.read_deployment_by_name(SCAN_DEPLOYMENT)
        waiting = client.read_flow_runs(
            flow_run_filter=FlowRunFilter(
                deployment_id=FlowRunFilterDeploymentId(any_=[deployment.id]),
                state=FlowRunFilterState(type=FlowRunFilterStateType(
                    any_=[StateType.SCHEDULED, StateType.PENDING])),
            ),
            limit=1,
        )
        if waiting:
            return False
        client.create_flow_run_from_deployment(
            deployment.id, state=Scheduled(scheduled_time=now + timedelta(seconds=delay)),
            idempotency_key=idempotency_key,
        )
    return True


def schedule_scan_retry(delay: int = SCAN_RETRY_SECONDS) -> bool:
    """A busy scan's retry (homelab#2751), or one after a MusicBrainz outage
    (#508): :func:`schedule_scan`, *delay* seconds on.  The idempotency key, one
    per *delay* window, keeps two scans that skip at once from both adding one.
    Called from inside the scan flow, against a Prefect server."""
    import time  # noqa: PLC0415

    return schedule_scan(delay, idempotency_key=f"scan-retry-{int(time.time()) // delay}")


# How many runs one read of :func:`crash_orphaned_runs` takes: the first
# restart after #513 finds some 145.
_ORPHAN_PAGE = 200


def crash_orphaned_runs(deployment_names: list[str], message: str) -> list[tuple[str, str]]:
    """Set every RUNNING or PENDING run of *deployment_names* (``flow/deployment``)
    to Crashed with *message* (#513).  For the service's start, before ``serve()``:
    the pod is these deployments' only runner, so such a run died with the last
    pod, and Prefect would show it RUNNING for ever.  A deployment the server
    doesn't know yet (a first start) has no runs.  Returns ``(flow, run id)`` for
    each run set to Crashed, and logs each as ``[ORPHAN] <flow> <run id> →
    Crashed``.  Needs a Prefect server, and raises when it can't be reached."""
    from prefect import get_client  # noqa: PLC0415
    from prefect.client.schemas.filters import (  # noqa: PLC0415
        FlowRunFilter, FlowRunFilterDeploymentId, FlowRunFilterState, FlowRunFilterStateType)
    from prefect.client.schemas.objects import StateType  # noqa: PLC0415
    from prefect.client.schemas.responses import SetStateStatus  # noqa: PLC0415
    from prefect.exceptions import ObjectNotFound  # noqa: PLC0415
    from prefect.states import Crashed  # noqa: PLC0415

    crashed: list[tuple[str, str]] = []
    with get_client(sync_client=True) as client:
        flows: dict = {}
        for name in deployment_names:
            try:
                flows[client.read_deployment_by_name(name).id] = name.split("/", 1)[0]
            except ObjectNotFound:
                continue
        if not flows:
            return crashed
        flow_filter = FlowRunFilter(
            deployment_id=FlowRunFilterDeploymentId(any_=list(flows)),
            state=FlowRunFilterState(type=FlowRunFilterStateType(any_=[StateType.RUNNING, StateType.PENDING])),
        )
        # Read them all first: paging while their states change would skip some.
        orphans: list = []
        while True:
            page = client.read_flow_runs(flow_run_filter=flow_filter, limit=_ORPHAN_PAGE, offset=len(orphans))
            orphans += page
            if len(page) < _ORPHAN_PAGE:
                break
        for run in orphans:
            result = client.set_flow_run_state(run.id, Crashed(message=message))
            if result.status == SetStateStatus.ACCEPT:
                crashed.append((flows[run.deployment_id], str(run.id)))
                logger.info("[ORPHAN] %s %s → Crashed", flows[run.deployment_id], run.id)
            else:
                logger.warning("[ORPHAN] %s %s: Prefect refused Crashed (%s)",
                               flows[run.deployment_id], run.id, result.status.value)
    return crashed


_album_tick_lock = threading.Lock()

# A RUNNING run older than this doesn't count as pending (#513): its runner died
# without the restart that :func:`crash_orphaned_runs` cleans up after.  Far
# above any real run ``pending_state`` is asked about: the longest scan in 30
# days took 84 min, plus its 30-min wait for the pipeline slot
# (``flows.SCAN_WAIT_SECONDS``); an album tick waits 20 min at most.
STALE_RUNNING_SECONDS = 6 * 3600


async def _pending_state(deployment_name: str) -> str | None:
    """The state of a run of *deployment_name* that is pending or running: a
    scheduled run counts once it is due, so the next interval run doesn't, and
    a run RUNNING for over ``STALE_RUNNING_SECONDS`` doesn't either (#513)."""
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
    now = datetime.now(timezone.utc)
    due = now + timedelta(seconds=5)
    stale = now - timedelta(seconds=STALE_RUNNING_SECONDS)
    for run in runs:
        state = run.state_type
        if state == StateType.RUNNING and run.start_time and run.start_time < stale:
            continue
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
        # The busy scan's waiter (homelab#2761, flows.SCAN_WAITER_SLOT): at most one waits.
        await client.upsert_global_concurrency_limit_by_name(
            name="scan-wait",
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
        logger.info("Prefect concurrency limits ensured: beet-import=1, pipeline=1, albums=1, scan-wait=1")
    except Exception as exc:
        logger.warning("Could not upsert Prefect concurrency limit: %s", exc)
