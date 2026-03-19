"""Console entry points for the scan container."""

from __future__ import annotations

import logging

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)


def scan() -> None:
    """Entry point: music-scan."""
    from pipeline.scan import run  # noqa: PLC0415

    run()


