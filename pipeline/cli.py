"""Console entry points for all music-pipeline commands.

Each function is registered as a console_script in pyproject.toml and
installed to /usr/local/bin/ via the pip package.
"""

from __future__ import annotations

import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)


# ---------------------------------------------------------------------------
# music-scan
# ---------------------------------------------------------------------------


def scan() -> None:
    """Entry point: music-scan."""
    from pipeline.scan import run  # noqa: PLC0415

    run()


# ---------------------------------------------------------------------------
# music-ingest
# ---------------------------------------------------------------------------


def ingest() -> None:
    """Entry point: music-ingest."""
    from pipeline.ingest import run  # noqa: PLC0415

    run()


# ---------------------------------------------------------------------------
# music-import
# ---------------------------------------------------------------------------


def import_cmd() -> None:
    """Entry point: music-import.

    Imports all audio from inbox to beets; moves unmatched files to quarantine.
    Called by music-scan; also useful standalone.
    """
    from pipeline.process import run_beet_import  # noqa: PLC0415
    from pipeline.scan import INBOX, QUARANTINE, _quarantine_inbox_leftovers  # noqa: PLC0415

    import subprocess

    logging.getLogger().info("==> Importing from inbox...")
    try:
        run_beet_import(INBOX)
    except subprocess.CalledProcessError as exc:
        logging.getLogger().error("beet import failed with exit code %d", exc.returncode)
        sys.exit(exc.returncode)

    logging.getLogger().info("==> Quarantining skipped files...")
    moved = _quarantine_inbox_leftovers()
    logging.getLogger().info("Quarantined : %d file(s) → %s", moved, QUARANTINE)
    logging.getLogger().info("Log         : ~/.config/beets/import.log")
