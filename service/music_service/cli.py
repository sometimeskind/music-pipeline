"""Console entry point: music-pipeline."""

from __future__ import annotations

import logging
import os
import sys


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)

logger = logging.getLogger(__name__)


def main() -> None:
    """Entry point: music-pipeline."""
    missing = [
        v for v in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "API_BEARER_TOKEN")
        if not os.environ.get(v)
    ]
    if missing:
        for var in missing:
            logger.error("Required environment variable not set: %s", var)
        sys.exit(1)

    from music_service.orchestrator import Orchestrator  # noqa: PLC0415
    from music_service.api import create_app  # noqa: PLC0415
    import waitress  # noqa: PLC0415

    orchestrator = Orchestrator()
    orchestrator.start()

    app = create_app(orchestrator)
    logger.info("Starting music-pipeline service on 0.0.0.0:8080")
    waitress.serve(app, host="0.0.0.0", port=8080)
