"""music-backfill-ids: give existing library items their Spotify identity (#176).

Items imported before #177 carry a single ``spotify_url`` and only the ISRCs
MusicBrainz gave them.  This one-shot reads every configured playlist from
Spotify's playlist item pages (about one call per 100 tracks; the .spotdl files
are incomplete, because reconcile used to drop entries merged under another
track ID) and, per playlist entry:

1. its *own* item — the one whose ``spotify_url`` is the entry, i.e. the file
   was downloaded for it — gets the entry's ID and ISRC;
2. otherwise the identity ladder (Spotify ID, ISRC, then title+artist words)
   finds the item among the playlist's items.  A match whose ISRCs are disjoint
   from the entry's is a **wrong version**: an earlier artist+title duplicate
   check merged a live take, remaster or radio edit into another recording.
   Other matches get the entry's ID and ISRC.

Wrong versions are only listed unless ``--redownload N``: then the first N are
downloaded from their entries straight into the playlist inbox (so ``nosync``
and album playlists work too; no Spotify calls), and the wrong item loses the
playlist's source tag and the entry's ID.  The next scan imports the download
as its own recording.

Dry run by default; ``--apply`` writes.  Safe to re-run.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging

from music_scan.identity import (
    BY_ISRC,
    BY_WORDS,
    ItemIndex,
    add_isrcs,
    add_to_list,
    item_isrcs,
    spotify_id,
    split_list,
)

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class WrongVersion:
    playlist: str
    song: dict
    item: object


@dataclasses.dataclass
class Plan:
    changed: dict = dataclasses.field(default_factory=dict)  # item.id → item
    ids_from_url: int = 0
    ids_added: int = 0
    isrcs_added: int = 0
    by_words: int = 0
    missing: int = 0
    # Own entries whose Spotify ISRC MusicBrainz doesn't list, of those MusicBrainz tagged.
    disagree: int = 0
    compared: int = 0
    wrong: list[WrongVersion] = dataclasses.field(default_factory=list)
    # (playlist, item.id) pairs that some entry rightly maps to.
    claimed: set = dataclasses.field(default_factory=set)


def _song_id(song: dict) -> str | None:
    return song.get("song_id") or spotify_id(song.get("url"))


def _label(song: dict) -> str:
    return f"{(song.get('artists') or ['?'])[0]} — {song.get('name', '?')}"


def _record(plan: Plan, playlist: str, song: dict, item) -> None:
    """The entry maps to *item*: record its ID and ISRC there."""
    plan.claimed.add((playlist, item.id))
    if add_to_list(item, "spotify_ids", _song_id(song)):
        plan.ids_added += 1
        plan.changed[item.id] = item
    if add_isrcs(item, [song.get("isrc")]):
        plan.isrcs_added += 1
        plan.changed[item.id] = item


def _contradicts(song: dict, item) -> bool:
    isrc, have = song.get("isrc"), item_isrcs(item)
    return bool(isrc and have and isrc not in have)


def plan_backfill(items: list, songs_by_playlist: dict[str, list[dict]]) -> Plan:
    """Work out the backfill on in-memory *items*; changes are made on them but not stored."""
    plan = Plan()
    for item in items:
        if add_to_list(item, "spotify_ids", spotify_id(item.get("spotify_url"))):
            plan.ids_from_url += 1
            plan.changed[item.id] = item

    # 1. Own entries first, so every downloaded file has its Spotify ISRC before
    #    other entries are checked against it.
    own = {sid: item for item in items if (sid := spotify_id(item.get("spotify_url")))}
    pending: dict[str, list[dict]] = {}
    for playlist, songs in songs_by_playlist.items():
        for song in songs:
            item = own.get(_song_id(song))
            if item is None or playlist not in split_list(item.get("sources")):
                pending.setdefault(playlist, []).append(song)
                continue
            if song.get("isrc") and item_isrcs(item):
                plan.compared += 1
                plan.disagree += _contradicts(song, item)
            _record(plan, playlist, song, item)

    # 2. The rest by the ladder.  IDs other than spotify_url's came from earlier
    #    artist+title merges, so an ID hit is checked against the ISRCs too.
    for playlist, songs in pending.items():
        index = ItemIndex(i for i in items if playlist in split_list(i.get("sources")))
        for song in songs:
            item, rung = index.match_song(song)
            if item is None:
                plan.missing += 1
            elif rung != BY_ISRC and _contradicts(song, item):
                plan.wrong.append(WrongVersion(playlist, song, item))
            else:
                if rung == BY_WORDS:
                    plan.by_words += 1
                    logger.info("  [WORDS] %s: %s matched by title+artist only", playlist, _label(song))
                _record(plan, playlist, song, item)
    return plan


def unlink_wrong(plan: Plan, wrong: WrongVersion) -> None:
    """Take the playlist's source (unless another entry maps to the item) and the entry's ID off the wrong item."""
    item = wrong.item
    if (wrong.playlist, item.id) not in plan.claimed:
        item["sources"] = ",".join(p for p in split_list(item.get("sources")) if p != wrong.playlist)
    sid = _song_id(wrong.song)
    if sid != spotify_id(item.get("spotify_url")):
        item["spotify_ids"] = ",".join(i for i in split_list(item.get("spotify_ids")) if i != sid)
    plan.changed[item.id] = item


def report(plan: Plan) -> None:
    for w in plan.wrong:
        path = w.item.path.decode() if isinstance(w.item.path, bytes) else w.item.path
        logger.info(
            "[WRONG] %s: %s (%s, ISRC %s) is held by %s (ISRC %s)",
            w.playlist, _label(w.song), _song_id(w.song), w.song.get("isrc"), path, w.item.get("isrc"),
        )
    logger.info(
        "Backfill: %d item(s) to change — %d spotify_ids from spotify_url, %d Spotify ID(s) and %d ISRC(s) "
        "added, %d by title+artist only; %d wrong version(s); %d playlist entr(ies) with no library item",
        len(plan.changed), plan.ids_from_url, plan.ids_added, plan.isrcs_added, plan.by_words,
        len(plan.wrong), plan.missing,
    )
    logger.info(
        "ISRCs: Spotify and MusicBrainz disagree on %d of %d downloaded track(s) both tagged",
        plan.disagree, plan.compared,
    )


def run(apply: bool = False, redownload: int = 0) -> Plan:
    from music_fetch import ingest  # noqa: PLC0415
    from music_fetch.config import load_playlists  # noqa: PLC0415
    from music_fetch.spotdl_ops import SpotifyPlaylists, download_song  # noqa: PLC0415
    from music_scan.library import LIBRARY_DB, MusicLibrary  # noqa: PLC0415

    reader = SpotifyPlaylists(ingest.COOKIE_FILE)
    songs_by_playlist = {}
    for pl in load_playlists(ingest.CONF_PATH):
        songs_by_playlist[pl.name] = reader.songs(pl.url)
        logger.info("Read %s: %d track(s)", pl.name, len(songs_by_playlist[pl.name]))

    with MusicLibrary(LIBRARY_DB) as lib:
        plan = plan_backfill(lib.all_items(), songs_by_playlist)
        report(plan)
        if not apply:
            logger.info("Dry run — nothing written. Re-run with --apply to write.")
            return plan

        for wrong in plan.wrong[:redownload]:
            out = ingest.SPOTDL_DIR / wrong.playlist
            out.mkdir(parents=True, exist_ok=True)
            path = download_song(wrong.song, out, ingest.COOKIE_FILE)
            if path is None:
                logger.warning("[REDL] %s: %s — download failed; left as is", wrong.playlist, _label(wrong.song))
                continue
            unlink_wrong(plan, wrong)
            logger.info("[REDL] %s: %s — downloaded to %s", wrong.playlist, _label(wrong.song), path)
        for item in plan.changed.values():
            item.store()
        logger.info("Wrote %d item(s)", len(plan.changed))
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(prog="music-backfill-ids", description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    parser.add_argument(
        "--redownload", type=int, default=0, metavar="N",
        help="with --apply: download the first N wrong versions' right versions",
    )
    args = parser.parse_args()
    if args.redownload and not args.apply:
        parser.error("--redownload needs --apply")
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    run(apply=args.apply, redownload=args.redownload)
