"""Who holds a concurrency slot, and since when (homelab#2841).

Prefect's global limits know their holders, but its API lists none, so a run
that found ``albums`` or ``pipeline`` taken could only say "held by another
run".  A run that takes one of them now writes a record of itself (flow, run
name, since, pid) to :data:`SLOTS_DIR`, and removes it when it lets go.  Every
flow run, MCP job and CLI run of the service is a process in the one container,
and the directory is outside the volumes, so a restart starts it empty.

- A run that waits for a slot, or gives up on one, names the holder from it
  (:func:`describe`).
- The service's main process pushes the holders to the Pushgateway every
  :data:`REPORT_SECONDS` (job ``music_slots``): ``music_slot_held{slot}`` (1
  while a live holder is recorded) and, once a hold has lasted
  :data:`REPORT_AFTER_SECONDS`,
  ``music_slot_held_since_timestamp_seconds{slot,flow,run}``.  homelab's
  ``MusicSlotHeldLong`` alerts on its age.  The holder's process is checked: a
  run that died holding a slot is not reported, and Prefect frees its slot when
  the lease runs out.

Only the run's flow and name and the times go in a record, never what it
works on.  A CLI run in the pod (``kubectl exec``) takes the slots without a
record, and shows as an unrecorded holder."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SLOTS = ("albums", "pipeline")
SLOTS_DIR = Path(os.environ.get("MUSIC_SLOTS_DIR") or "/tmp/music-slots")  # noqa: S108
METRICS_JOB = "music_slots"
# Every two minutes, so a dead Pushgateway costs a warning that often.
REPORT_SECONDS = 120
# Holds shorter than this aren't exported: most last minutes (an album import's
# about 6), and a series per run name would churn for nothing.
REPORT_AFTER_SECONDS = 10 * 60


@dataclass(frozen=True)
class Holder:
    slot: str
    flow: str
    run: str
    since: float
    pid: int
    token: str

    def alive(self) -> bool:
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def describe(self, now: float | None = None) -> str:
        age = (time.time() if now is None else now) - self.since
        since = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(self.since))
        text = f"{self.flow} run '{self.run}', held since {since} ({_duration(age)})"
        if not self.alive():
            text += "; its process has exited, so Prefect frees the slot when the lease runs out (5 min)"
        return text


def _duration(seconds: float) -> str:
    minutes = max(0, int(seconds // 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes} min"


def _path(slot: str) -> Path:
    return SLOTS_DIR / f"{slot}.json"


def _run_names() -> tuple[str, str]:
    """This process's flow and flow run name, from Prefect's run context."""
    try:
        from prefect.runtime import flow_run  # noqa: PLC0415

        return flow_run.flow_name or "unknown", flow_run.name or "unknown"
    except Exception:  # noqa: BLE001 - a name is a nicety, never a failure
        return "unknown", "unknown"


@contextlib.contextmanager
def held(slot: str, flow: str | None = None, run: str | None = None) -> Iterator[None]:
    """Record this process as *slot*'s holder for the block.  Enter it once the
    slot is taken.  *flow* and *run* default to the current Prefect flow run's.
    Never raises on the record itself: a holder that can't be named still holds."""
    if flow is None or run is None:
        names = _run_names()
        flow, run = flow or names[0], run or names[1]
    holder = Holder(slot=slot, flow=flow, run=run, since=time.time(), pid=os.getpid(), token=uuid.uuid4().hex)
    path = _path(slot)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{holder.token}.tmp")
        tmp.write_text(json.dumps(asdict(holder)))
        tmp.replace(path)
    except OSError:
        logger.warning("Could not record the %s slot's holder", slot, exc_info=True)
    try:
        yield
    finally:
        # Only our own record: a run that outlived its lease may find another's.
        current = holder_of(slot)
        if current is not None and current.token == holder.token:
            with contextlib.suppress(OSError):
                path.unlink()


@contextlib.contextmanager
def held_all(slots: Iterable[str], flow: str, run: str) -> Iterator[None]:
    with contextlib.ExitStack() as stack:
        for slot in slots:
            stack.enter_context(held(slot, flow, run))
        yield


def holder_of(slot: str) -> Holder | None:
    """*slot*'s recorded holder, alive or not; None when there is no record."""
    try:
        return Holder(**json.loads(_path(slot).read_text()))
    except (OSError, ValueError, TypeError):
        return None


def describe(slot: str) -> str:
    """Who holds *slot*, for a log line."""
    holder = holder_of(slot)
    if holder is None:
        return "no holder recorded: a CLI run in the pod?"
    return holder.describe()


def metrics_body(now: float | None = None) -> str:
    now = time.time() if now is None else now
    live = {slot: h for slot in SLOTS if (h := holder_of(slot)) is not None and h.alive()}
    lines = ["# TYPE music_slot_held gauge"]
    lines += [f'music_slot_held{{slot="{slot}"}} {int(slot in live)}' for slot in SLOTS]
    long = [h for h in live.values() if now - h.since >= REPORT_AFTER_SECONDS]
    if long:
        lines.append("# TYPE music_slot_held_since_timestamp_seconds gauge")
        lines += [f'music_slot_held_since_timestamp_seconds{{slot="{h.slot}",flow="{_label(h.flow)}",'
                  f'run="{_label(h.run)}"}} {int(h.since)}' for h in long]
    return "\n".join(lines)


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def push() -> None:
    from music_fetch.metrics import _push  # noqa: PLC0415

    _push(metrics_body(), METRICS_JOB)


def start_reporter(interval: float = REPORT_SECONDS) -> threading.Thread:
    """Push the holders now and every *interval* seconds, from a daemon thread."""

    def loop() -> None:
        while True:
            try:
                push()
            except Exception:  # noqa: BLE001 - the reporter must outlive one bad read
                logger.warning("Could not push the slot holders", exc_info=True)
            time.sleep(interval)

    thread = threading.Thread(target=loop, name="slot-reporter", daemon=True)
    thread.start()
    return thread
