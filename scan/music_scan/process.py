"""Subprocess helpers: run beet commands with SIGTERM forwarding."""

from __future__ import annotations

import logging
import re
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

logger = logging.getLogger(__name__)

IMPORT_LOG = Path("/root/.config/beets/import.log")

# beets gives up on a MusicBrainz request after its own retries (beets 2.14:
# urllib3 Retry(total=6, backoff_factor=0.5) on 5xx/429, not configurable),
# logs it, skips the track and still exits 0 (#264).
_MB_UNAVAILABLE = re.compile(r"host='musicbrainz\.org'.*Max retries exceeded")


def musicbrainz_unavailable(line: str) -> bool:
    """True when a beets stderr line says MusicBrainz could not be reached."""
    return bool(_MB_UNAVAILABLE.search(line))


# Set once this process has had a SIGTERM while beets ran (homelab#2763): the
# pod is shutting down, and nothing more is imported, quarantined or staged.
_shutdown = threading.Event()


class ImportInterrupted(BaseException):
    """``beet import`` was stopped because the process is shutting down (homelab#2763).

    The SIGTERM that stops a pod reaches the flow-run process while beets
    runs, and :func:`_forward_sigterm` hands it to beets in place of Prefect's
    handler.  beets then exits early, and before this, that read as a finished
    import: the scan quarantined every inbox file beets never reached and
    staged them for the as-is pass on the way out, and 68 files were lost with
    the container.  Now what beets didn't reach stays where it is (the inbox,
    or the as-is staging dir) for the scan after the restart.

    A BaseException, as Prefect's own ``TerminationSignal`` and
    ``KeyboardInterrupt`` are, so the steps that log an ``Exception`` and carry
    on don't carry on, and an album import's retry (``albums.complete``)
    doesn't count it against the release: the run crashes, and the job folder
    goes back at the next start (``[RESUME]``, #344)."""


def shutdown_requested() -> bool:
    """True once this process has had a SIGTERM while beets ran (homelab#2763)."""
    return _shutdown.is_set()


@contextmanager
def _forward_sigterm(proc: subprocess.Popen) -> Generator[None, None, None]:
    """Context manager: while active, SIGTERM is forwarded to *proc*, and
    recorded (:func:`shutdown_requested`), so :func:`run_beet_import` raises
    :class:`ImportInterrupted` once beets has exited (homelab#2763).

    The handler replaces the one in place, Prefect's in a flow run, which
    would raise ``TerminationSignal`` in the middle of ``proc.wait()`` and
    leave beets running on its own; the run ends with ``ImportInterrupted``
    instead.

    signal.signal() only works in the main thread. When called from a
    background thread (e.g. the orchestrator scan thread), we skip SIGTERM
    forwarding — the process will be reaped normally when it finishes.
    """
    def _handler(sig: int, frame: object) -> None:
        logger.info("Shutdown signal received — forwarding to child process (pid %d)", proc.pid)
        _shutdown.set()
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
                     proc: subprocess.Popen, stop: threading.Event, fired: threading.Event | None = None) -> None:
    """Poll the beets import log; SIGTERM *proc* after *skip_limit* new skips,
    setting *fired* first."""
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
                if fired is not None:
                    fired.set()
                proc.terminate()
                return
        except FileNotFoundError:
            pass
        time.sleep(0.5)


def run_beet_import(inbox_dir: Path, skip_limit: int | None = None, asis: bool = False,
                    config: Path | None = None) -> int:
    """Run ``beet import --quiet <inbox_dir>``, forwarding SIGTERM to beet.

    Returns how many stderr lines said MusicBrainz was unavailable (#264).

    Raises :class:`ImportInterrupted` when the process had a SIGTERM while
    beets ran, or before it started (homelab#2763), whatever beets' exit
    code; and ``CalledProcessError`` when beets exited non-zero or was killed
    by a signal, except by the skip limit's own SIGTERM.  Before, any SIGTERM
    exit (``-15``) passed as the skip limit's, so a shutdown read as a
    finished import.

    Args:
        skip_limit: If set, terminate beet after this many skipped tracks (for threshold
                    testing without waiting through a full library run).
        asis: If True, pass ``--asis`` to import using existing embedded tags without
              MusicBrainz lookups.
        config: If set, a beets config file layered over the main one (``beet -c``).
    """
    cmd = ["beet", *(["-c", str(config)] if config else []), "import", "--quiet"]
    if asis:
        cmd.append("-A")
    cmd.append(str(inbox_dir))
    if _shutdown.is_set():
        raise ImportInterrupted("the service is shutting down; beet import not started")
    logger.debug("Running: %s", " ".join(cmd))

    log_start = IMPORT_LOG.stat().st_size if IMPORT_LOG.exists() else 0
    proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)
    mb_errors = 0

    def _stream_stderr() -> None:
        nonlocal mb_errors
        assert proc.stderr is not None
        for line in proc.stderr:
            text = line.decode(errors="replace").rstrip()
            if text:
                logger.warning("beet: %s", text)
                mb_errors += musicbrainz_unavailable(text)

    stderr_thread = threading.Thread(target=_stream_stderr, daemon=True)
    stderr_thread.start()

    stop = threading.Event()
    skipped = threading.Event()
    if skip_limit is not None:
        t = threading.Thread(
            target=_watch_for_skips,
            args=(IMPORT_LOG, log_start, skip_limit, proc, stop, skipped),
            daemon=True,
        )
        t.start()

    with _forward_sigterm(proc):
        rc = proc.wait()
    stop.set()
    stderr_thread.join()

    if _shutdown.is_set():
        logger.warning("[INTERRUPTED] beet import stopped by a shutdown signal (exit %d)", rc)
        raise ImportInterrupted(f"beet import stopped by a shutdown signal (exit {rc})")
    # rc=-15 (SIGTERM) is expected when skip_limit fires — not an error.  Any
    # other signal, or a SIGTERM from anywhere else, is (homelab#2763).
    if rc == -signal.SIGTERM and skipped.is_set():
        return mb_errors
    if rc != 0:
        if rc < 0:
            logger.warning("[INTERRUPTED] beet import was killed by signal %d", -rc)
        raise subprocess.CalledProcessError(rc, cmd)
    return mb_errors


def update_fields() -> list[str]:
    """The fields ``beet update`` stores: its default set plus ``mtime``.

    With no ``-F``, beets 2.14 stores only the item's media fields, never the
    file mtime that ``read()`` just took, so a file touched outside beets is
    re-read on every scan and a no-op diff (an empty ``©wrt`` atom read as
    ``['']`` against ``[]``) prints every time (#340).  ``Album.item_keys``
    keeps the album side as wide as the default."""
    from beets.library import Album, Item  # noqa: PLC0415

    return sorted(Item._media_fields | set(Album.item_keys) | {"mtime"})


def run_beet_update() -> None:
    """Run ``beet update`` (non-fatal on failure)."""
    cmd = ["beet", "update"]
    for field in update_fields():
        cmd += ["-F", field]
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        logger.warning("beet update exited with code %d (non-fatal)", result.returncode)
