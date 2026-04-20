"""Subprocess helpers: run beet commands with SIGTERM forwarding."""

from __future__ import annotations

import logging
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

logger = logging.getLogger(__name__)

IMPORT_LOG = Path("/root/.config/beets/import.log")


@contextmanager
def _forward_sigterm(proc: subprocess.Popen) -> Generator[None, None, None]:
    """Context manager: while active, SIGTERM is forwarded to *proc*.

    signal.signal() only works in the main thread. When called from a
    background thread (e.g. the orchestrator scan thread), we skip SIGTERM
    forwarding — the process will be reaped normally when it finishes.
    """
    def _handler(sig: int, frame: object) -> None:
        logger.info("Shutdown signal received — forwarding to child process (pid %d)", proc.pid)
        proc.send_signal(signal.SIGTERM)

    try:
        old = signal.signal(signal.SIGTERM, _handler)
    except ValueError:
        # Not in the main thread — SIGTERM forwarding unavailable; run without it.
        yield
        return

    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, old)


def _watch_for_skips(log_path: Path, start_pos: int, skip_limit: int,
                     proc: subprocess.Popen, stop: threading.Event) -> None:
    """Poll the beets import log; SIGTERM *proc* after *skip_limit* new skips."""
    skip_count = 0
    while not stop.is_set():
        try:
            with open(log_path) as f:
                f.seek(start_pos)
                new_lines = f.read().splitlines()
            new_skips = sum(1 for l in new_lines if l.startswith("skip "))
            if new_skips > skip_count:
                skip_count = new_skips
                logger.info("Skip count: %d / %d limit", skip_count, skip_limit)
            if skip_count >= skip_limit:
                logger.warning("Skip limit %d reached — terminating beet import early", skip_limit)
                proc.terminate()
                return
        except FileNotFoundError:
            pass
        time.sleep(0.5)


def run_beet_import(inbox_dir: Path, skip_limit: int | None = None, asis: bool = False) -> None:
    """Run ``beet import --quiet <inbox_dir>``, forwarding SIGTERM to beet.

    Args:
        skip_limit: If set, terminate beet after this many skipped tracks (for threshold
                    testing without waiting through a full library run).
        asis: If True, pass ``--asis`` to import using existing embedded tags without
              MusicBrainz lookups.
    """
    cmd = ["beet", "import", "--quiet"]
    if asis:
        cmd.append("-A")
    cmd.append(str(inbox_dir))
    logger.debug("Running: %s", " ".join(cmd))

    log_start = IMPORT_LOG.stat().st_size if IMPORT_LOG.exists() else 0
    proc = subprocess.Popen(cmd)

    stop = threading.Event()
    if skip_limit is not None:
        t = threading.Thread(
            target=_watch_for_skips,
            args=(IMPORT_LOG, log_start, skip_limit, proc, stop),
            daemon=True,
        )
        t.start()

    with _forward_sigterm(proc):
        rc = proc.wait()
    stop.set()

    # rc=-15 (SIGTERM) is expected when skip_limit fires — not an error
    if rc not in (0, -15):
        raise subprocess.CalledProcessError(rc, cmd)


def run_beet_update() -> None:
    """Run ``beet update`` (non-fatal on failure)."""
    result = subprocess.run(["beet", "update"], check=False)
    if result.returncode != 0:
        logger.warning("beet update exited with code %d (non-fatal)", result.returncode)
