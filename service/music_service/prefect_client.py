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


async def _submit_deployment(name: str) -> None:
    from prefect.deployments import run_deployment
    await run_deployment(name, timeout=0)


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
    import music_scan.reconcile as reconcile
    import music_scan.scan as scan
    try:
        logger.info("==> Fetch starting")
        pending = ingest.run()
        logger.info("==> Scan starting")
        scan.run(pending)
        reconcile.reconcile_all()
        logger.info("==> Scan complete")
    except Exception:
        logger.exception("fetch-and-scan failed")
    finally:
        _lock.release()


def _run_scan() -> None:
    import music_scan.reconcile as reconcile
    import music_scan.scan as scan
    try:
        logger.info("==> Scan starting")
        scan.run(None)
        reconcile.reconcile_all()
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
    """Submit a fetch-and-scan run. Returns True if submitted/accepted."""
    if _has_server():
        return _via_api("fetch-and-scan/fetch-and-scan")
    return _direct(_run_fetch_and_scan)


def trigger_scan() -> None:
    """Submit a scan run. Fire-and-forget."""
    if _has_server():
        try:
            asyncio.run(_submit_deployment("scan/scan"))
        except Exception as exc:
            logger.error("Failed to submit scan: %s", exc)
    else:
        _direct(_run_scan)


async def _upsert_limits() -> None:
    from prefect import get_client
    async with get_client() as client:
        await client.upsert_global_concurrency_limit_by_name(
            name="beet-import",
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
        logger.info("Prefect concurrency limit 'beet-import' ensured (limit=1)")
    except Exception as exc:
        logger.warning("Could not upsert Prefect concurrency limit: %s", exc)
