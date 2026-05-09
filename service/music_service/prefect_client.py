"""Sync helpers for submitting Prefect deployment runs from Flask routes and the debouncer."""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


def _run(coro):
    """Run *coro* in a new event loop on the calling thread. Safe from Waitress worker threads."""
    return asyncio.run(coro)


async def _trigger(deployment_name: str) -> None:
    from prefect.deployments import run_deployment
    await run_deployment(deployment_name, timeout=0)


def trigger_fetch() -> bool:
    """Submit a fetch-and-scan deployment run. Returns True if submitted."""
    try:
        _run(_trigger("fetch-and-scan/fetch-and-scan"))
        return True
    except Exception as exc:
        logger.error("Failed to trigger fetch-and-scan: %s", exc)
        return False


def trigger_scan() -> None:
    """Submit a scan deployment run. Fire-and-forget."""
    try:
        _run(_trigger("scan/scan"))
    except Exception as exc:
        logger.error("Failed to trigger scan: %s", exc)
