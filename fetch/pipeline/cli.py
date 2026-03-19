"""Console entry points for the fetch container."""

from __future__ import annotations

import logging

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)


def ingest() -> None:
    """Entry point: music-ingest."""
    from pipeline.ingest import run  # noqa: PLC0415

    run()
