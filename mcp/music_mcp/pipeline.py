"""The pipeline's /mcp-api/* (service/MCP_API.md), the only service the adapter reaches."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import httpx
from fastmcp.exceptions import ToolError

from music_mcp import metrics
from music_mcp.auth import Caller

log = logging.getLogger(__name__)

# A run waits up to MCP_INLINE_SECONDS (default 20) on the pipeline side before answering 202.
TIMEOUT = httpx.Timeout(10.0, read=90.0)


class Pipeline:
    def __init__(self, base_url: str, api_token: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/mcp-api",
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=TIMEOUT,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get(self, caller: Caller, path: str, **params: Any) -> dict:
        params = {k: v for k, v in params.items() if v is not None}
        return await self._call(caller, "GET", path, params=params)

    async def post(self, caller: Caller, path: str, body: dict | None = None) -> dict:
        return await self._call(caller, "POST", path, json=body or {})

    async def _call(self, caller: Caller, method: str, path: str, **kw: Any) -> dict:
        headers = {"X-MCP-Caller": caller.id, "X-MCP-Tier": caller.tier}
        try:
            resp = await self._http.request(method, path, headers=headers, **kw)
        except httpx.TimeoutException:
            metrics.UPSTREAM_FAILURES.labels("timeout").inc()
            log.warning("pipeline %s %s timed out", method, path)
            raise ToolError("the music pipeline did not answer in time; try get_job or retry later") from None
        except httpx.HTTPError as exc:
            metrics.UPSTREAM_FAILURES.labels("connect").inc()
            log.warning("pipeline %s %s failed: %s", method, path, exc)
            raise ToolError("the music pipeline is unreachable") from None

        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            body = None

        if resp.status_code == 401:
            metrics.UPSTREAM_FAILURES.labels("unauthorized").inc()
            log.error("pipeline refused MUSIC_MCP_API_TOKEN on %s %s", method, path)
            raise ToolError("the adapter's pipeline token was refused (operator: MCP_API_TOKEN mismatch)")
        # A status the contract defines (ambiguous, not_found, refused, busy, unknown,
        # not_implemented, already_pending, submitted, done…) is an answer: hand it over.
        if body is not None and "status" in body:
            # busy (locked library, full queue), failed (Prefect, not us) and not_implemented
            # are the pipeline reporting, not failing to answer.
            if resp.status_code >= 500 and body.get("status") not in ("busy", "failed", "not_implemented"):
                metrics.UPSTREAM_FAILURES.labels("server_error").inc()
            return body
        if resp.status_code >= 500:
            metrics.UPSTREAM_FAILURES.labels("server_error").inc()
            log.warning("pipeline %s %s: HTTP %s", method, path, resp.status_code)
            raise ToolError(f"the music pipeline failed (HTTP {resp.status_code})")
        if resp.status_code >= 400:
            error = (body or {}).get("error") or f"HTTP {resp.status_code}"
            raise ToolError(str(error))
        if body is None:
            metrics.UPSTREAM_FAILURES.labels("bad_response").inc()
            raise ToolError("the music pipeline answered with something other than a JSON object")
        return body


def seg(value: str | int) -> str:
    """One path segment, so a name can't step into another route."""
    text = str(value)
    if text in ("", ".", ".."):
        raise ToolError(f"not a valid name: {text!r}")
    return quote(text, safe="")
