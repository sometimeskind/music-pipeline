"""music-mb-fingerprint: MusicBrainz recording IDs from AcoustID.

Without flags it fingerprints each item that has no ``mb_trackid`` and writes
the recording AcoustID names with a score of at least :data:`MIN_SCORE`.  No
other metadata is changed.  Safe to re-run: it skips items that have one.

``--recheck`` (homelab#2543) checks the items that have one.  beets imports
every file as a singleton and names its recording by a guess from title,
artist and length, so a sibling recording (a 5.1 mix, a live take, an
alternate version) can win.  The file's own audio settles it: the item is
looked up on AcoustID and its ``mb_trackid`` checked against the recordings
AcoustID links to its fingerprint at :data:`MIN_SCORE` or more.

- Among them: nothing is reported.
- Not among them: ``[RECHECK]``.  With exactly one such recording, that one
  replaces the guess on ``--apply``; with several, none is chosen and the item
  is left alone (never guess).
- No recording at :data:`MIN_SCORE`: ``[NOMATCH]``, left alone (some releases
  are unknown to AcoustID, homelab#2546).

A request fallback's ``mb_trackid`` (``fallback_recording``) is not a guess:
AcoustID named it at its import, so the item is skipped.  A merge or supersede
keeper (``merged_files``) is checked like any item and marked as one: a merge
joins only copies with the same audio or a shared ID, so the keeper's audio is
the merged recording's, and the ID it carries from a loser is that loser's beets
guess (supersede kept it for Navidrome's plays and stars, a loss the operator
accepted on 2026-10-31).  Its losers' files stay superseded by ``merged_files``
(while their title and artist agree with the keeper's, homelab#2539).

It is a dry run without ``--apply``.  The lookups run first, without a lock
(pyacoustid keeps to AcoustID's 3 requests a second, so the whole library takes
hours).  The pipeline's ticks go on meanwhile and may move, retire or purge a
file, so each item is re-read from the library just before its lookup; one gone
from the library, or whose file went away between the read and the fingerprint,
is counted as gone and not checked.  ``[PROGRESS]`` every :data:`PROGRESS_EVERY`
items says how far the run has got.  Then ``--apply`` takes the pipeline lock
and, item by item, re-reads the item, skips it when its ``mb_trackid`` changed
meanwhile, and stores the new ID with a ``recording_replaced`` event in the same
Postgres transaction (it refuses to run without ``MUSIC_DATABASE_URL``), then
writes the file's tags.
``mb_releasetrackid`` and the title stay as they were (titles are homelab#2544).
An item whose file changed since its lookup (path, size or mtime: replace_item
swaps the audio and keeps ``mb_trackid``) is skipped too.

A recorded run (homelab#2553): ``--recheck --run-id ID`` stores its checkpoint
every :data:`CHECKPOINT_EVERY` items and its findings in the database
(:mod:`music_scan.recheck_runs`), and the same command continues it after a
restart, from the item after its checkpoint.  It is always a dry run;
``--replace-run ID`` then says what its apply would do as the library stands,
item by item, and ``--replace-run ID --apply`` does it under the pipeline lock,
recording each finding's outcome with its event.  An interrupted apply is
finished by running it again: an item that holds its replacement already is
written again and counted as done.

Requires the ACOUSTID_APIKEY env var.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import functools
import logging
import os
import time
from collections import Counter
from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)

LIBRARY_DB = "/root/.config/beets/library.db"
LIBRARY_DIR = "/root/Music/library"
MIN_SCORE = 0.85
REQUEST_DELAY = 0.35
# --recheck: a lookup that fails is tried once more after this long; this many
# failed lookups in a row (AcoustID down, a bad key, rate-limited) end the run.
RETRY_SECONDS = 5.0
MAX_FAILURES_IN_A_ROW = 10
LOOKUP_TIMEOUT = 30
PROGRESS_EVERY = 500
EVENT_KIND = "recording_replaced"
SOURCE = "mb-fingerprint"
# Flexible attributes that say the pipeline, not beets' guess, set mb_trackid.
FALLBACK_RECORDING = "fallback_recording"  # music_pipeline.FALLBACK_RECORDING
MERGED_FILES = "merged_files"  # dupes.MERGED_FILES

Recordings = list[tuple[float, str]]


def run(library_db: str = LIBRARY_DB, library_dir: str = LIBRARY_DIR) -> None:
    import acoustid
    import beets.library

    api_key = os.environ.get("ACOUSTID_APIKEY")
    if not api_key:
        raise RuntimeError("ACOUSTID_APIKEY env var is not set")

    # directory must be passed explicitly — without it Library defaults to ~/Music
    lib = beets.library.Library(library_db, directory=library_dir)
    try:
        items = [i for i in lib.items() if not i.mb_trackid]
        total = len(items)
        logger.info("Fingerprinting %d item(s) without mb_trackid", total)

        tagged = no_match = errors = 0
        for n, item in enumerate(items, 1):
            path = item.path.decode() if isinstance(item.path, bytes) else item.path
            label = f"{item.artist or item.albumartist or '?'} – {item.title or '?'}"
            try:
                results = list(acoustid.match(api_key, path))
                if results:
                    score, rec_id, *_ = results[0]
                    if score >= MIN_SCORE and rec_id:
                        item.mb_trackid = rec_id
                        item.store()
                        logger.info("[%d/%d] %s -> %s (%.2f)", n, total, label, rec_id, score)
                        tagged += 1
                        continue
                    logger.info("[%d/%d] %s: low confidence (%.2f)", n, total, label, score)
                else:
                    logger.info("[%d/%d] %s: no results", n, total, label)
                no_match += 1
            except Exception as exc:
                logger.warning("[%d/%d] %s: %s", n, total, label, exc)
                errors += 1
            time.sleep(REQUEST_DELAY)
    finally:
        lib._close()

    logger.info(
        "Done: %d tagged, %d no confident match, %d errors",
        tagged, no_match, errors,
    )


# ---------------------------------------------------------------------------
# --recheck (homelab#2543)
# ---------------------------------------------------------------------------


class LookupFailed(RuntimeError):
    """AcoustID couldn't be asked (the web service failed)."""


def acoustid_recordings(path: str, api_key: str) -> Recordings:
    """Every recording AcoustID links to *path*'s fingerprint, each once at its
    best score, best first.  Raises :class:`LookupFailed` when the web service
    fails and ``ValueError`` when the file can't be fingerprinted."""
    import acoustid  # noqa: PLC0415

    best: dict[str, float] = {}
    try:
        for score, rid, *_ in acoustid.match(api_key, path, timeout=LOOKUP_TIMEOUT):
            if rid:
                best[rid] = max(score, best.get(rid, 0.0))
    except acoustid.FingerprintGenerationError as exc:
        raise ValueError(f"no fingerprint: {exc}") from exc
    except acoustid.WebServiceError as exc:
        raise LookupFailed(str(exc)) from exc
    return sorted(((s, r) for r, s in best.items()), key=lambda p: (-p[0], p[1]))


FileStat = tuple[str, int | None, int | None]  # path, size, mtime in ns (None when it couldn't be read)


@dataclasses.dataclass
class Flagged:
    """An item whose ``mb_trackid`` (*guess*) is not among its confident AcoustID recordings."""

    item_id: int
    guess: str
    found: Recordings  # the recordings at MIN_SCORE or more, best first
    keeper: bool  # a merge or supersede keeper (merged_files), marked in the report and event
    # The file as it was at the lookup: the apply skips the item when it changed
    # since (replace_item swaps the audio and keeps mb_trackid, homelab#2553).
    file: FileStat = ("", None, None)

    @property
    def replacement(self) -> str | None:
        """The one confident recording, or None when there are several (never guess)."""
        return self.found[0][1] if len(self.found) == 1 else None


def _label(item) -> str:
    return f"{item.id} — {item.get('title') or '?'}"


def _path(item) -> str:
    return os.fsdecode(item.path) if isinstance(item.path, bytes) else str(item.path)


def file_stat(path: str) -> FileStat:
    try:
        st = os.stat(path)
    except OSError:
        return (path, None, None)
    return (path, st.st_size, st.st_mtime_ns)


def _lookup(lookup: Callable[[str], Recordings], path: str) -> Recordings:
    """*lookup*, tried once more after :data:`RETRY_SECONDS` when the service fails."""
    try:
        return lookup(path)
    except LookupFailed:
        time.sleep(RETRY_SECONDS)
        return lookup(path)


COUNTS = ("checked", "agree", "flagged", "replaceable", "ambiguous", "keepers", "nomatch", "errors", "gone",
          "skipped")
# A recorded run (--run-id) stores its checkpoint every this many items: a restart repeats at most these lookups.
CHECKPOINT_EVERY = 25

Checkpoint = Callable[[int, int, Counter, list[Flagged]], None]  # (position, last item ID, counts, new findings)


def recheck(lib, ids: Iterable[int], lookup: Callable[[str], Recordings], *, start: int = 0,
            total: int | None = None, counts: dict | None = None,
            checkpoint: Checkpoint | None = None) -> tuple[Counter, list[Flagged]]:
    """Check each item of *ids* that has an ``mb_trackid`` against *lookup* (the
    recordings for a file's audio, best first).  Each item is read from *lib*
    just before its lookup, so a tick that moved its file meanwhile doesn't leave
    a stale path.  Logs one line per finding and returns the counts and the
    flagged items.

    A continued run passes *start* (the items walked before), *total* and the
    *counts* so far; *checkpoint* is called every :data:`CHECKPOINT_EVERY` items
    and after the last, with the findings since the call before."""
    ids = list(ids)
    total = start + len(ids) if total is None else total
    counts = _counts(counts)
    flagged: list[Flagged] = []
    fresh: list[Flagged] = []
    failures = 0
    n = start
    for item_id in ids:
        n += 1
        if n > 1 and (n - 1) % PROGRESS_EVERY == 0:
            logger.info("[PROGRESS] %d/%d, %d flagged so far", n - 1, total, counts["flagged"])
        try:
            f = _check(lib, item_id, lookup, counts)
        except LookupFailed as exc:
            failures += 1
            if failures >= MAX_FAILURES_IN_A_ROW:
                raise SystemExit(f"--recheck: {failures} AcoustID lookups failed in a row; stopping "
                                 f"(checked {counts['checked']} so far, nothing written)") from exc
        else:
            if f is not _UNCHECKED:
                failures = 0  # a lookup answered
            if isinstance(f, Flagged):
                flagged.append(f)
                fresh.append(f)
        if checkpoint is not None and (n % CHECKPOINT_EVERY == 0 or n == start + len(ids)):
            checkpoint(n, item_id, counts, fresh)
            fresh = []
    return counts, flagged


def _counts(so_far: dict | None = None) -> Counter:
    counts = Counter({k: 0 for k in COUNTS})
    counts.update(so_far or {})
    return counts


_UNCHECKED = object()  # _check's answer for an item it didn't look up (gone, skipped, unreadable)


def _check(lib, item_id: int, lookup: Callable[[str], Recordings], counts: Counter):
    """One item: its :class:`Flagged`, None (looked up, nothing to report) or
    :data:`_UNCHECKED`; raises :class:`LookupFailed` (counted) when AcoustID
    couldn't be asked."""
    item = lib.get_item(item_id)
    if item is None:
        counts["gone"] += 1
        logger.info("[GONE] %s: no longer in the library; not checked", item_id)
        return _UNCHECKED
    guess = item.get("mb_trackid")
    if not guess:
        return _UNCHECKED
    if item.get(FALLBACK_RECORDING):
        counts["skipped"] += 1  # AcoustID named it at its import (the request guard)
        return _UNCHECKED
    path = _path(item)
    stat = file_stat(path)
    try:
        found = _lookup(lookup, path)
    except LookupFailed as exc:
        counts["errors"] += 1
        logger.warning("[ERROR] %s: AcoustID lookup failed (%s)", _label(item), exc)
        raise
    except (OSError, ValueError) as exc:
        if not os.path.exists(path):  # a tick moved or removed it since the read
            counts["gone"] += 1
            logger.info("[GONE] %s: its file went away during the run; not checked (re-run with its ID)",
                        _label(item))
        else:
            counts["errors"] += 1
            logger.warning("[ERROR] %s: %s", _label(item), exc)
        return _UNCHECKED
    counts["checked"] += 1
    confident = [(s, r) for s, r in found if s >= MIN_SCORE]
    if not confident:
        counts["nomatch"] += 1
        best = f"best {found[0][1]} {found[0][0]:.2f}" if found else "no recording"
        logger.info("[NOMATCH] %s: AcoustID has nothing at %.2f or more (%s); mb_trackid %s left alone",
                    _label(item), MIN_SCORE, best, guess)
        return None
    if guess in {r for _, r in confident}:
        counts["agree"] += 1
        return None
    f = Flagged(item.id, guess, confident, keeper=bool(item.get(MERGED_FILES)), file=stat)
    counts["flagged"] += 1
    counts["keepers"] += f.keeper
    if f.replacement is None:
        counts["ambiguous"] += 1
        outcome = (f"{len(confident)} recordings ("
                   + ", ".join(f"{r} {s:.2f}" for s, r in confident) + "), none chosen; left alone")
    else:
        counts["replaceable"] += 1
        outcome = f"→ {f.replacement}" + (" (a merge keeper)" if f.keeper else "")
    logger.info("[RECHECK] %s: mb_trackid %s is not among its AcoustID recordings (%s %.2f): %s",
                _label(item), guess, confident[0][1], confident[0][0], outcome)
    return f


# The verdicts on a flagged item at its apply.
REPLACE, ALREADY, SKIP = "replace", "already", "skip"


def verdict(item, f: Flagged) -> tuple[str, str]:
    """Whether *f* may be applied to *item* (the item as the library holds it now), and why not."""
    if item is None:
        return SKIP, "gone from the library"
    now = item.get("mb_trackid")
    if now == f.replacement:
        return ALREADY, f"mb_trackid is {now} already"
    if now != f.guess:
        return SKIP, f"mb_trackid is {now} now, not {f.guess}"
    if file_stat(_path(item)) != tuple(f.file):
        return SKIP, "its file changed since its check; recheck it by ID"
    return REPLACE, ""


def _store(conn, item, f: Flagged, job_id: str | None = None, run_id: str | None = None,
           event: bool = True) -> str:
    """Store *f*'s replacement on *item* with its ``recording_replaced`` event (and,
    for a recorded run, the finding's outcome) in one transaction; returns the message."""
    from music_fetch import events  # noqa: PLC0415

    from music_scan import recheck_runs  # noqa: PLC0415

    score = f.found[0][0]
    message = f"[RECHECK] {_label(item)}: mb_trackid {f.guess} → {f.replacement} (AcoustID {score:.2f})"
    with conn.transaction():
        if event:
            events.insert(conn, EVENT_KIND, message, source=SOURCE,
                          spotify_album_id=events.spotify_album(item.get("spotify_album_id")),
                          release_group=item.get("mb_releasegroupid"), item_id=item.id,
                          data={**events.item_fields(item), "old": f.guess, "new": f.replacement,
                                "score": score, "keeper": f.keeper, **({"run": run_id} if run_id else {})})
        if run_id:
            recheck_runs.record_outcome(conn, run_id, item.id, "replaced", None, job_id)
        if item.get("mb_trackid") != f.replacement:
            item["mb_trackid"] = f.replacement
            item.store()
    return message


def replace(lib, flagged: list[Flagged]) -> int:
    """Replace each flagged item's guess with its one confident recording, under
    the pipeline lock, each with its ``recording_replaced`` event in the same
    transaction.  An item whose ``mb_trackid`` or file changed since its lookup
    (or that left the library) is skipped.  Returns the count replaced."""
    from music_fetch import db  # noqa: PLC0415
    from music_scan.audit import pipeline_lock  # noqa: PLC0415

    todo = [f for f in flagged if f.replacement]
    if not todo:
        return 0
    replaced = 0
    with pipeline_lock(), contextlib.closing(db.connect()) as conn:
        for f in todo:
            item = lib.get_item(f.item_id)
            what, why = verdict(item, f)
            if what != REPLACE:
                logger.warning("[SKIP] %s: %s; left alone", f.item_id, why)
                continue
            message = _store(conn, item, f)
            item.try_write()
            logger.info("%s", message)
            replaced += 1
    return replaced


def _flagged(row: dict) -> Flagged:
    return Flagged(row["item_id"], row["guess"], [(s, r) for s, r in row["found"]], row["keeper"],
                   (row["file_path"] or "", row["file_size"], row["file_mtime_ns"]))


def replace_run(run_id: str, apply: bool = False, library_db: str = LIBRARY_DB,
                library_dir: str = LIBRARY_DIR) -> Counter:
    """``--replace-run``: a recorded run's replacements, item by item, as the
    library holds each now.  Without *apply* it says what an apply would do;
    with it, under the pipeline lock, it stores each.  Re-running it after an
    interrupted apply finishes the job: an item that holds its replacement
    already is written again (its tags) and counted as done."""
    import beets.library  # noqa: PLC0415

    from music_fetch import db  # noqa: PLC0415
    from music_scan import recheck_runs  # noqa: PLC0415
    from music_scan.audit import pipeline_lock  # noqa: PLC0415

    if not db.configured():
        raise SystemExit(f"--replace-run: {db.ENV} is unset; the run's findings are in the database")
    job_id = os.environ.get("MUSIC_EVENT_JOB") or None
    counts: Counter = Counter({k: 0 for k in ("replaceable", "replaced", "already", "skipped", "ambiguous")})
    lib = beets.library.Library(library_db, directory=library_dir)
    try:
        with contextlib.closing(db.connect()) as conn, (pipeline_lock() if apply else contextlib.nullcontext()):
            try:
                recheck_runs.for_apply(conn, recheck_runs.check_id(run_id))
            except recheck_runs.Refused as exc:
                raise SystemExit(f"--replace-run: {exc}") from None
            rows = recheck_runs.findings(conn, run_id)
            logger.info("Run %s: %d finding(s)", run_id, len(rows))
            for row in rows:
                f = _flagged(row)
                if f.replacement is None:
                    counts["ambiguous"] += 1
                    continue
                counts["replaceable"] += 1
                item = lib.get_item(f.item_id)
                what, why = verdict(item, f)
                if what == SKIP:
                    counts["skipped"] += 1
                    logger.info("[SKIP] %s: %s; left alone", f.item_id, why)
                    if apply:
                        recheck_runs.record_outcome(conn, run_id, f.item_id, "skipped", why, job_id)
                    continue
                if what == ALREADY:
                    counts["already"] += 1
                    logger.info("[DONE] %s: holds %s already", _label(item), f.replacement)
                    if apply:
                        # Stored before a restart: its event and outcome may not be, nor its tags.
                        _store(conn, item, f, job_id, run_id, event=row["outcome"] != "replaced")
                        item.try_write()
                    continue
                if not apply:
                    logger.info("[WOULD-REPLACE] %s: mb_trackid %s → %s (AcoustID %.2f)%s", _label(item), f.guess,
                                f.replacement, f.found[0][0], " (a merge keeper)" if f.keeper else "")
                    continue
                logger.info("%s", _store(conn, item, f, job_id, run_id))
                item.try_write()
                counts["replaced"] += 1
    finally:
        lib._close()
    logger.info("[SUMMARY] run %s: %d replaceable: %s, %d already replaced, %d skipped; "
                "%d with several recordings left alone",
                run_id, counts["replaceable"],
                f"{counts['replaced']} replaced" if apply else
                f"{counts['replaceable'] - counts['already'] - counts['skipped']} to replace (re-run with --apply)",
                counts["already"], counts["skipped"], counts["ambiguous"])
    return counts


def run_recheck(apply: bool = False, ids: list[int] | None = None,
                library_db: str = LIBRARY_DB, library_dir: str = LIBRARY_DIR,
                lookup: Callable[[str], Recordings] | None = None, run_id: str | None = None) -> Counter:
    """``--recheck``: check every item with an ``mb_trackid`` (or the items *ids*),
    and with *apply* replace the flagged guesses.  Returns the counts.

    With *run_id* (homelab#2553) the run is recorded: its checkpoint and findings
    go to the database, the same ID continues it after a restart, and
    ``--replace-run`` applies it."""
    import beets.library  # noqa: PLC0415

    from music_fetch import db  # noqa: PLC0415

    if apply and run_id:
        raise SystemExit("--apply: a recorded run is applied with --replace-run, after its dry run")
    if (apply or run_id) and not db.configured():
        raise SystemExit(f"{'--apply' if apply else '--run-id'}: {db.ENV} is unset; every replacement is "
                         "recorded as an event, and a recorded run in the database, so nothing runs without it")
    if lookup is None:
        api_key = os.environ.get("ACOUSTID_APIKEY")
        if not api_key:
            raise SystemExit("ACOUSTID_APIKEY is not set")
        lookup = functools.partial(acoustid_recordings, api_key=api_key)

    lib = beets.library.Library(library_db, directory=library_dir)
    try:
        if run_id:
            counts = _recorded(lib, run_id, ids, lookup)
        else:
            counts, flagged = recheck(lib, _todo(lib, ids), lookup)
            counts["replaced"] = replace(lib, flagged) if apply else 0
    finally:
        lib._close()

    logger.info("[SUMMARY] checked %d: %d agree, %d flagged (%d replaceable, %d with several recordings, "
                "%d merge keepers), %d no match, %d errors; %d gone during the run, %d request fallbacks skipped; "
                "%s",
                counts["checked"], counts["agree"], counts["flagged"], counts["replaceable"],
                counts["ambiguous"], counts["keepers"], counts["nomatch"], counts["errors"],
                counts["gone"], counts["skipped"],
                f"run {run_id} recorded, nothing replaced (preview it with --replace-run {run_id})" if run_id else
                f"{counts['replaced']} replaced" if apply else "dry run, nothing replaced (re-run with --apply)")
    return counts


def _todo(lib, ids: list[int] | None, after: int | None = None) -> list[int]:
    """The IDs to check, in ID order: *ids*' items (or the library's) with an mb_trackid, after *after*."""
    if ids:
        items = [i for i in (lib.get_item(n) for n in ids) if i is not None]
        if missing := sorted(set(ids) - {i.id for i in items}):
            logger.warning("No library item with id %s", ", ".join(map(str, missing)))
    else:
        items = list(lib.items())
    # Only the IDs: each item is re-read just before its lookup (recheck).
    todo = sorted(i.id for i in items if i.get("mb_trackid") and (after is None or i.id > after))
    del items
    return todo


def _recorded(lib, run_id: str, ids: list[int] | None, lookup: Callable[[str], Recordings]) -> Counter:
    """A recorded run: started, or continued after its checkpoint."""
    from music_fetch import db  # noqa: PLC0415
    from music_scan import recheck_runs  # noqa: PLC0415

    with contextlib.closing(db.connect()) as conn:
        try:
            recheck_runs.hold(conn, recheck_runs.check_id(run_id))
            # Without item IDs, an existing run continues with the ones it was started with.
            run = recheck_runs.get(conn, run_id)
            args = {"ids": sorted(set(ids))} if ids else (None if run else {"ids": None})
            run, continued = recheck_runs.open_run(conn, run_id, args, os.environ.get("MUSIC_EVENT_CALLER") or None,
                                                   os.environ.get("MUSIC_EVENT_JOB") or None)
        except recheck_runs.Refused as exc:
            raise SystemExit(f"--run-id: {exc}") from None
        todo = _todo(lib, run["args"]["ids"], after=run["cursor"] if continued else None)
        start = run["position"] if continued else 0
        total = start + len(todo)
        if continued:
            logger.info("[RESUME] run %s: continuing after item %s (%d of %d walked, %d flagged so far)",
                        run_id, run["cursor"], start, total, run["counts"].get("flagged", 0))
        else:
            logger.info("Rechecking %d item(s) with an mb_trackid against AcoustID (run %s)", len(todo), run_id)

        def save(position: int, cursor: int, counts: Counter, fresh: list[Flagged]) -> None:
            recheck_runs.checkpoint(conn, run_id, position, cursor, total, counts, fresh)

        try:
            counts, _ = recheck(lib, todo, lookup, start=start, total=total,
                                counts=run["counts"] if continued else None, checkpoint=save)
        except SystemExit:
            recheck_runs.finish(conn, run_id, recheck_runs.FAILED)
            raise
        recheck_runs.finish(conn, run_id, recheck_runs.DONE)
    return counts


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s",
                        datefmt="%Y-%m-%dT%H:%M:%S%z")
    parser = argparse.ArgumentParser(prog="music-mb-fingerprint",
                                     description="MusicBrainz recording IDs from AcoustID.")
    parser.add_argument("--recheck", action="store_true",
                        help="check items that have an mb_trackid against their AcoustID recordings (homelab#2543)")
    parser.add_argument("--run-id", metavar="ID",
                        help="with --recheck: record the run under ID (a dry run); the same ID continues it "
                             "after a restart (homelab#2553)")
    parser.add_argument("--replace-run", metavar="ID",
                        help="a recorded run's replacements: what an apply would do, or with --apply do it")
    parser.add_argument("--apply", action="store_true",
                        help="with --recheck: replace the flagged guesses; with --replace-run: apply the run")
    parser.add_argument("ids", nargs="*", type=int, help="with --recheck: only these item IDs")
    args = parser.parse_args()
    if args.replace_run:
        if args.recheck or args.run_id or args.ids:
            parser.error("--replace-run takes only --apply")
        replace_run(args.replace_run, apply=args.apply)
        return
    if args.run_id and args.apply:
        parser.error("a recorded run (--run-id) is a dry run: apply it with --replace-run ID --apply")
    if not args.recheck:
        if args.apply or args.ids or args.run_id:
            parser.error("--apply, --run-id and item IDs go with --recheck")
        run()
        return
    run_recheck(apply=args.apply, ids=args.ids, run_id=args.run_id)
