"""Console entry points for the fetch container."""

from __future__ import annotations

import logging

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)


def main() -> None:
    """Entry point: music-ingest."""
    try:
        from pipeline.ingest import run  # noqa: PLC0415
    except ImportError as exc:
        import sys
        print(f"[music-ingest] Failed to import pipeline: {exc}. Check container installation.", file=sys.stderr)
        sys.exit(1)
    run()
