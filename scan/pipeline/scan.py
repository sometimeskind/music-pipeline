"""music-scan: import inbox → beets, refresh metadata, regenerate .m3u playlists,
push Prometheus metrics.

Called frequently (every 5 min by default) and also after music-ingest completes.
No Spotify or YouTube calls.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from pipeline.library import MusicLibrary
from pipeline.metrics import ScanMetrics
from pipeline.navidrome import trigger_scan
from pipeline.process import run_beet_import, run_beet_update

logger = logging.getLogger(__name__)

AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wav", ".wma", ".aiff", ".ape", ".mpc"}
SPOTDL_DIR = Path("/root/Music/inbox/spotdl")
QUARANTINE = Path("/root/Music/quarantine")
PLAYLISTS = Path("/root/Music/playlists")
INBOX = Path("/root/Music/inbox")
LIBRARY_DB = Path("/root/.config/beets/library.db")
PENDING_REMOVALS = Path("/root/Music/inbox/.pending-removals.json")


_STOP_WORDS = frozenset({"the", "and", "for", "feat", "ft", "vs", "with", "a", "an", "of", "in", "on"})


def _name_words(s: str) -> frozenset[str]:
    """Normalise a track/filename string to a set of significant lowercase words."""
    words = re.sub(r"[^\w\s]", " ", s.lower()).split()
    return frozenset(w for w in words if len(w) > 2 and w not in _STOP_WORDS)


def _snapshot_inbox(inbox: Path) -> list[str]:
    """Return sorted list of audio filename stems currently in the inbox tree."""
    return sorted(
        f.stem for f in inbox.rglob("*")
        if f.is_file() and f.suffix.lower() in AUDIO_EXTS
    )


def _check_import_names(inbox_stems: list[str], imported: list[tuple[str, str]]) -> None:
    """Compare imported track names against the pre-import inbox snapshot.

    Flags tracks whose title+artist shares less than 40% Jaccard word-overlap
    with the closest matching inbox filename — a signal that beets may have
    applied a wildly wrong match.
    """
    if not inbox_stems or not imported:
        return

    inbox_word_sets = [_name_words(s) for s in inbox_stems]

    flagged = []
    for title, artist in imported:
        lib_words = _name_words(f"{title} {artist}")
        if not lib_words:
            continue
        best = max(
            (len(lib_words & iws) / max(len(lib_words | iws), 1) for iws in inbox_word_sets),
            default=0.0,
        )
        if best < 0.4:
            flagged.append((title, artist, best))

    if flagged:
        logger.warning("==> %d imported track(s) look unlike anything in the inbox (possible bad match):", len(flagged))
        for title, artist, score in sorted(flagged, key=lambda x: x[2]):
            logger.warning("  !! %s — %s  (best inbox overlap: %.0f%%)", title, artist, score * 100)
    else:
        logger.info("==> Name check OK: all %d imported tracks resemble their inbox source files", len(imported))


def _count_quarantine() -> int:
    if not QUARANTINE.exists():
        return 0
    return sum(1 for _ in QUARANTINE.rglob("*") if _.is_file())


def _quarantine_inbox_leftovers() -> int:
    """Move any audio files still anywhere in the inbox tree to quarantine.

    After ``beet import`` has processed everything it can, un-matched audio
    files remain in the inbox.  We move them to quarantine for manual review,
    preserving the relative path so it's clear which playlist/album they came
    from.  Returns the count of files moved.
    """
    QUARANTINE.mkdir(parents=True, exist_ok=True)
    moved = 0
    for f in INBOX.rglob("*"):
        if f.is_file() and f.suffix.lower() in AUDIO_EXTS:
            dest = QUARANTINE / f.relative_to(INBOX)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), dest)
            moved += 1
    return moved


def _process_pending_removals() -> int:
    """Clear beets source tags for tracks/playlists removed by the fetch container.

    Reads .pending-removals.json from the shared volume, processes each entry,
    then deletes the file.  Returns the number of entries processed.

    Supports both formats:
    - Old (list): [{title, artist, source}, ...]
    - New (dict): {tracks: [{title, artist, source}], remove_sources: [name, ...]}
    """
    if not PENDING_REMOVALS.exists():
        return 0

    content = PENDING_REMOVALS.read_text(encoding="utf-8")
    PENDING_REMOVALS.unlink()

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        logger.error(
            "Discarded malformed .pending-removals.json — source-tag cleanup skipped. "
            "Manually run `beet modify source= source:<name>` for affected playlists."
        )
        return 0

    # Support old list format for backward compatibility.
    if isinstance(data, list):
        tracks: list[dict] = data
        remove_sources: list[str] = []
    else:
        tracks = data.get("tracks", [])
        remove_sources = data.get("remove_sources", [])

    if not tracks and not remove_sources:
        return 0

    logger.info(
        "==> Processing pending removals: %d track(s), %d source(s)...",
        len(tracks),
        len(remove_sources),
    )
    total = 0
    with MusicLibrary(LIBRARY_DB) as lib:
        for entry in tracks:
            title = entry.get("title", "")
            artist = entry.get("artist", "")
            source = entry.get("source", "")
            found = lib.clear_source_tag(title=title, artist=artist, source=source)
            if not found:
                logger.warning(
                    "  WARNING: not found in beets — may need manual cleanup: %s by %s (source=%s)",
                    title,
                    artist,
                    source,
                )
            total += 1

        for source_name in remove_sources:
            logger.info("==> Removing all tracks from playlist: %s", source_name)
            items = lib.items_by_source(source_name)
            for item in items:
                item["source"] = ""
                item.store()
            m3u = PLAYLISTS / f"{source_name}.m3u"
            m3u.unlink(missing_ok=True)
            logger.info("  Cleared %d item(s) and removed .m3u for source=%s", len(items), source_name)
            total += 1

    return total


_ASIS_REQUIRED_TAGS = ("title", "artist", "album", "tracknumber")


def _move_asis_eligible(quarantine: Path, staging: Path) -> int:
    """Move audio files from *quarantine* that have all required tags to *staging*.

    Files missing title, artist, album, or tracknumber are left in quarantine.
    Returns the count of files moved.
    """
    from mutagen import File as MutagenFile  # noqa: PLC0415 — beets dep, always available

    moved = 0
    for f in sorted(quarantine.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in AUDIO_EXTS:
            continue
        try:
            tags = MutagenFile(f, easy=True)
            if tags is None or not all(tags.get(k) for k in _ASIS_REQUIRED_TAGS):
                continue
        except Exception:
            continue
        dest = staging / f.relative_to(quarantine)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), dest)
        moved += 1
    return moved


def _regen_playlists() -> None:
    """Regenerate .m3u files for every .spotdl playlist."""
    PLAYLISTS.mkdir(parents=True, exist_ok=True)
    spotdl_files = sorted(SPOTDL_DIR.glob("*.spotdl"))
    if not spotdl_files:
        logger.debug("No .spotdl files found — no playlists to generate")
        return

    with MusicLibrary(LIBRARY_DB) as lib:
        for spotdl_file in spotdl_files:
            name = spotdl_file.stem
            m3u = PLAYLISTS / f"{name}.m3u"
            logger.info("    Generating: %s", m3u)
            paths = lib.paths_by_source(name)
            lines = [os.path.relpath(p, PLAYLISTS) for p in sorted(paths)]
            m3u.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def run() -> None:
    """Execute the full scan pipeline, push metrics on completion."""
    metrics = ScanMetrics()
    start = time.monotonic()

    try:
        logger.info("==> music-scan starting")

        logger.info("==> Processing pending removals from fetch container...")
        metrics.tracks_removed = _process_pending_removals()

        quarantined_before = _count_quarantine()

        logger.info("==> Importing from inbox...")
        skip_limit_env = os.environ.get("BEET_SKIP_LIMIT")
        skip_limit = int(skip_limit_env) if skip_limit_env else None
        if skip_limit is not None:
            logger.info("Skip limit    : %d (early termination enabled)", skip_limit)
        inbox_snapshot = _snapshot_inbox(INBOX)
        logger.info("Inbox snapshot : %d audio file(s) queued for import", len(inbox_snapshot))
        import_start = time.time()
        run_beet_import(INBOX, skip_limit=skip_limit)

        with MusicLibrary(LIBRARY_DB) as lib:
            imported = lib.items_added_since(import_start)
        logger.info("Newly imported : %d track(s)", len(imported))
        _check_import_names(inbox_snapshot, imported)

        logger.info("==> Quarantining skipped files...")
        moved = _quarantine_inbox_leftovers()
        logger.info("Quarantined : %d file(s) → %s", moved, QUARANTINE)
        logger.info("Log         : ~/.config/beets/import.log")

        logger.info("==> Importing quarantine with existing tags (--asis)...")
        asis_start = time.time()
        with tempfile.TemporaryDirectory(prefix="asis-staging-") as staging_str:
            staging = Path(staging_str)
            staged = _move_asis_eligible(QUARANTINE, staging)
            logger.info("Asis eligible : %d file(s) with sufficient tags", staged)
            if staged:
                run_beet_import(staging, asis=True)
                # Return any files beet skipped (e.g. duplicates) to quarantine
                for remaining in staging.rglob("*"):
                    if remaining.is_file() and remaining.suffix.lower() in AUDIO_EXTS:
                        dest = QUARANTINE / remaining.relative_to(staging)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(remaining), dest)
        with MusicLibrary(LIBRARY_DB) as lib:
            asis_imported = lib.items_added_since(asis_start)
        logger.info("Asis pass     : %d track(s) imported from quarantine", len(asis_imported))
        metrics.tracks_imported = len(imported) + len(asis_imported)

        logger.info("==> Refreshing library metadata...")
        run_beet_update()

        logger.info("==> Regenerating playlists...")
        _regen_playlists()

        quarantined_after = _count_quarantine()
        metrics.quarantined_tracks = max(0, quarantined_after - quarantined_before)

        logger.info("==> music-scan complete")
        if imported or asis_imported:
            trigger_scan()

    except Exception:
        metrics.success = False
        metrics.failure_reason = "unexpected_error"
        logger.exception("music-scan failed")
        raise
    finally:
        metrics.duration_seconds = int(time.monotonic() - start)
        metrics.push()
