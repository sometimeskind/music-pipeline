"""music-mcp entrypoint: serve the MCP on :8000 and metrics on :9090."""

from __future__ import annotations

import asyncio
import logging


async def serve() -> None:
    import asyncpg
    from prometheus_client import start_http_server

    from music_mcp.auth import build_auth, caller_resolver, postgres_store
    from music_mcp.config import ServerSettings
    from music_mcp.pipeline import Pipeline
    from music_mcp.server import build_server

    settings = ServerSettings.from_env()
    # The OAuth proxy's store is the only thing in this database.
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=4)
    pipeline = Pipeline(settings.pipeline_url, settings.api_token)
    auth, static = build_auth(settings, postgres_store(pool))
    mcp = build_server(pipeline, caller_resolver(settings.required_group, static), auth=auth)
    # A plain socket on "::" also accepts IPv4 (unlike uvicorn's, see ServerSettings.host).
    start_http_server(settings.metrics_port, addr="::")
    try:
        await mcp.run_async(transport="http", host=settings.host, port=settings.port)
    finally:
        await pipeline.aclose()
        await pool.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(serve())


if __name__ == "__main__":
    main()
