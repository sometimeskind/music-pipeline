"""Subprocess helpers: run beet commands with SIGTERM forwarding."""

from __future__ import annotations

import logging
import signal
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

logger = logging.getLogger(__name__)


@contextmanager
def _forward_sigterm(proc: subprocess.Popen) -> Generator[None, None, None]:
    """Context manager: while active, SIGTERM is forwarded to *proc*."""

    def _handler(sig: int, frame: object) -> None:
        logger.info("Shutdown signal received — forwarding to child process (pid %d)", proc.pid)
        proc.send_signal(signal.SIGTERM)

    old = signal.signal(signal.SIGTERM, _handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, old)


def run_beet_import(inbox_dir: Path) -> None:
    """Run ``beet import --quiet <inbox_dir>``, forwarding SIGTERM to beet."""
    cmd = ["beet", "import", "--quiet", str(inbox_dir)]
    logger.debug("Running: %s", " ".join(cmd))
    proc = subprocess.Popen(cmd)
    with _forward_sigterm(proc):
        rc = proc.wait()
    if rc != 0:
        raise subprocess.CalledProcessError(rc, cmd)


def run_beet_update() -> None:
    """Run ``beet update`` (non-fatal on failure)."""
    result = subprocess.run(["beet", "update"], check=False)
    if result.returncode != 0:
        logger.warning("beet update exited with code %d (non-fatal)", result.returncode)
