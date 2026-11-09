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
   Unless MusicBrainz lists the entry's ISRC on the item's recording
   (``mb_trackid``): one recording often has several ISRCs (editions,
   clean/explicit, labels), so that is the same recording, as in the
   duplicate hook (#191).  Only these candidates are looked up.
   Other matches get the entry's ID and ISRC.

Wrong versions are only listed unless ``--redownload N``: then the first N are
downloaded from their entries straight into the playlist inbox (so ``nosync``
and album playlists work too; no Spotify calls), and the wrong item loses the
playlist's source tag and the entry's ID.  The next scan imports the download
as its own recording.

Dry run by default; ``--apply`` writes.  Safe to re-run.

``--albums`` is a pass of its own, over albums already imported
(homelab#2664): it reads no Spotify pages and uses no title words.  Each entry
of an imported album record that no library item holds by Spotify ID or ISRC
goes through ``tag_album_ids``' number rung
(:func:`music_scan.identity.number_match`) against the record's release items
(``album_record``), as if the rung as it stands today had run at the import.
Only ``spotify_ids`` and ``words_ids`` change, in the database and not in the
file, and the ``.m3u`` files are regenerated.  An entry nothing holds has no
item sharing its ID, so no retire, supersede or merge follows from a link.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable

from music_fetch import events
from music_scan.identity import (
    BY_ISRC,
    BY_WORDS,
    NO_ITEM,
    ItemIndex,
    PlaylistTrack,
    add_isrcs,
    add_to_list,
    item_isrcs,
    number_match,
    skip_note,
    spotify_id,
    split_list,
)

logger = logging.getLogger(__name__)

MB_ISRC_URL = "https://musicbrainz.org/ws/2/isrc/{}?fmt=json"
MB_USER_AGENT = "music-pipeline-backfill/1.0 ( https://github.com/sometimeskind/music-pipeline )"
MB_INTERVAL = 1.0  # MusicBrainz allows one request a second per client.


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
    # Disjoint ISRCs, but MusicBrainz lists the entry's ISRC on the item's recording.
    same_recording: int = 0
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


class MusicBrainzRecordings:
    """ISRC → MusicBrainz recording IDs, cached and rate-limited; None when the lookup fails."""

    def __init__(self) -> None:
        self._cache: dict[str, set[str] | None] = {}
        self._last = 0.0

    def __call__(self, isrc: str) -> set[str] | None:
        if isrc not in self._cache:
            self._cache[isrc] = self._fetch(isrc)
        return self._cache[isrc]

    def _fetch(self, isrc: str) -> set[str] | None:
        time.sleep(max(0.0, self._last + MB_INTERVAL - time.monotonic()))
        self._last = time.monotonic()
        url = MB_ISRC_URL.format(urllib.parse.quote(isrc))
        req = urllib.request.Request(url, headers={"User-Agent": MB_USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return {r["id"] for r in json.load(resp).get("recordings", [])}
        except urllib.error.HTTPError as exc:
            if exc.code == 404:  # MusicBrainz has no recording with this ISRC
                return set()
            logger.warning("MusicBrainz lookup of ISRC %s failed: %s", isrc, exc)
        except (OSError, ValueError) as exc:
            logger.warning("MusicBrainz lookup of ISRC %s failed: %s", isrc, exc)
        return None


def mb_recordings() -> Callable[[str], dict | None]:
    """``recording/<id>?inc=isrcs`` through the pipeline's MusicBrainz client,
    cached; None when there is no such recording or the lookup fails."""
    from music_scan.mb_release import MusicBrainz  # noqa: PLC0415

    mb = MusicBrainz()
    cache: dict[str, dict | None] = {}

    def lookup(recording_id: str) -> dict | None:
        if recording_id not in cache:
            try:
                cache[recording_id] = mb.get(f"recording/{recording_id}", inc="isrcs")
            except (OSError, ValueError) as exc:
                logger.warning("MusicBrainz lookup of recording %s failed: %s", recording_id, exc)
                cache[recording_id] = None
        return cache[recording_id]

    return lookup


def _same_recording(song: dict, item, recordings: Callable[[str], set[str] | None] | None) -> bool:
    """True if MusicBrainz lists the entry's ISRC on the item's recording."""
    mbid = item.get("mb_trackid")
    if not (recordings and mbid):
        return False
    return mbid in (recordings(song["isrc"]) or set())


def plan_backfill(
    items: list,
    songs_by_playlist: dict[str, list[dict]],
    recordings: Callable[[str], set[str] | None] | None = None,
) -> Plan:
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
        # Words reach identified items too: a hit on one that the ISRCs contradict is a wrong version.
        index = ItemIndex((i for i in items if playlist in split_list(i.get("sources"))), identified_words=True)
        for song in songs:
            item, rung = index.match_song(song)
            if item is None:
                plan.missing += 1
            elif rung != BY_ISRC and _contradicts(song, item):
                if _same_recording(song, item, recordings):
                    plan.same_recording += 1
                    logger.info(
                        "  [SAME] %s: %s (ISRC %s) is the recording of %s (ISRC %s) on MusicBrainz",
                        playlist, _label(song), song.get("isrc"), item.get("mb_trackid"), item.get("isrc"),
                    )
                    _record(plan, playlist, song, item)
                else:
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
        "added, %d by title+artist only; %d wrong version(s), %d same recording by MusicBrainz; "
        "%d playlist entr(ies) with no library item",
        len(plan.changed), plan.ids_from_url, plan.ids_added, plan.isrcs_added, plan.by_words,
        len(plan.wrong), plan.same_recording, plan.missing,
    )
    logger.info(
        "ISRCs: Spotify and MusicBrainz disagree on %d of %d downloaded track(s) both tagged",
        plan.disagree, plan.compared,
    )


@dataclasses.dataclass
class AlbumLink:
    key: str
    playlist: str
    track: PlaylistTrack
    item: object


@dataclasses.dataclass
class AlbumSkip:
    key: str
    playlist: str
    track: PlaylistTrack
    why: dict


@dataclasses.dataclass
class AlbumPlan:
    links: list[AlbumLink] = dataclasses.field(default_factory=list)
    skips: list[AlbumSkip] = dataclasses.field(default_factory=list)
    albums: int = 0


def _record_order(key: str) -> tuple[int, str]:
    return (int(key), "") if key.isdigit() else (1 << 62, key)


def plan_album_ids(items: list, records: dict[str, dict],
                   recordings: Callable[[str], dict | None]) -> AlbumPlan:
    """The ``--albums`` pass on in-memory *items* and the album store's
    *records*.  Changes nothing."""
    from music_scan.release import EXTRA  # noqa: PLC0415

    library = ItemIndex(items)
    releases: dict[str, list] = {}
    for item in items:
        if item.get("album_record") and item.get("via") == "usenet" and not item.get(EXTRA):
            releases.setdefault(str(item.get("album_record")), []).append(item)
    plan = AlbumPlan()
    for key in sorted(records, key=_record_order):
        record, release = records[key], releases.get(key)
        if record.get("status") != "imported" or not release:
            continue
        plan.albums += 1
        seen: set[str] = set()
        for playlist, tracks in sorted((record.get("playlists") or {}).items()):
            for track in map(PlaylistTrack.from_entry, tracks):
                if not track.song_id or track.song_id in seen:
                    continue
                seen.add(track.song_id)
                if library.match(track.song_id, track.isrc, words=False)[0] is not None:
                    continue
                item, why = number_match(track, release, recordings) if track.disc and track.track else (None, None)
                if item is not None:
                    plan.links.append(AlbumLink(key, playlist, track, item))
                else:
                    plan.skips.append(AlbumSkip(key, playlist, track, why or {"why": NO_ITEM}))
    return plan


def report_albums(plan: AlbumPlan) -> None:
    for link in plan.links:
        logger.info("[LINK] %s %s: %s — %s → item %s at %d-%d", link.key, link.playlist, link.track.name,
                    link.track.artist, link.item.id, link.track.disc, link.track.track)
    for skip in plan.skips:
        logger.info("[NOID] %s %s: %s — %s %s", skip.key, skip.playlist, skip.track.name, skip.track.artist,
                    skip_note(skip.why))
    why = Counter(skip.why["why"] for skip in plan.skips)
    logger.info("Album IDs: %d imported album(s) read, %d empty entr(ies) to link, %d left empty (%s)", plan.albums,
                len(plan.links), len(plan.skips), ", ".join(f"{n} {w}" for w, n in sorted(why.items())) or "none")


def apply_album_ids(plan: AlbumPlan) -> None:
    """Write *plan*'s links: the Spotify ID in ``spotify_ids`` and ``words_ids``
    (never superseding, as in ``tag_album_ids``), and one ``id_linked`` event per album."""
    by_key: dict[str, list[AlbumLink]] = {}
    for link in plan.links:
        add_to_list(link.item, "spotify_ids", link.track.song_id)
        add_to_list(link.item, "words_ids", link.track.song_id)
        link.item.store()
        by_key.setdefault(link.key, []).append(link)
    for key, links in by_key.items():
        linked = [{"item_id": k.item.id, "song_id": k.track.song_id, "via": "number"} for k in links]
        message = f"[IDS] {key}: {len(links)} Spotify track ID(s) linked by music-backfill-ids --albums"
        events.record("id_linked", message, source="backfill", item_id=links[0].item.id if len(links) == 1 else None,
                      data={"key": key, "playlist": links[0].playlist, "linked": linked, "skipped": []})


def run_albums(apply: bool = False) -> AlbumPlan:
    from music_fetch import state  # noqa: PLC0415
    from music_scan.audit import pipeline_lock  # noqa: PLC0415
    from music_scan.library import LIBRARY_DB, MusicLibrary  # noqa: PLC0415
    from music_scan.navidrome import trigger_scan  # noqa: PLC0415
    from music_scan.scan import regen_playlists  # noqa: PLC0415

    with state.session() as conn:
        records = state.load_albums(conn).albums
    with MusicLibrary(LIBRARY_DB) as lib:
        plan = plan_album_ids(lib.all_items(), records, mb_recordings())
        report_albums(plan)
        if not apply:
            logger.info("Dry run — nothing written. Re-run with --apply to write.")
            return plan
        if plan.links:
            with pipeline_lock():
                apply_album_ids(plan)
                counts = regen_playlists()
            logger.info("Linked %d; playlists regenerated: %s", len(plan.links),
                        ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
            trigger_scan()
    return plan


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
        plan = plan_backfill(lib.all_items(), songs_by_playlist, MusicBrainzRecordings())
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
            item = wrong.item
            events.record("wrong_version", f"[WRONG] {wrong.playlist}: {_label(wrong.song)} ({_song_id(wrong.song)}, "
                          f"ISRC {wrong.song.get('isrc')}) is held by {events.item_fields(item)['path']} "
                          f"(ISRC {item.get('isrc')}); downloaded to {path}", source="backfill",
                          spotify_album_id=events.spotify_album(wrong.song.get("album_id")), item_id=item.id,
                          data={**events.item_fields(item), "playlist": wrong.playlist,
                                "song_id": _song_id(wrong.song), "song_isrc": wrong.song.get("isrc"),
                                "downloaded": str(path)})
        store_changed(plan)
        logger.info("Wrote %d item(s)", len(plan.changed))
    return plan


def store_changed(plan: Plan) -> None:
    """Store *plan*'s changed items, writing the files of those whose ISRCs changed.

    An ISRC is a tag field: setting it zeroes the item's stored mtime, so the
    scan's ``beet update`` would re-read the file and take its ISRCs back
    (homelab#2726).  Written first, so the store keeps the file's new mtime.
    Under the pipeline lock: a scan moves and writes these files too.
    """
    from music_scan.audit import pipeline_lock  # noqa: PLC0415

    with pipeline_lock():
        for item in plan.changed.values():
            if not item.mtime and not item.try_write():
                logger.warning("item %s: the tags couldn't be written, so the next scan's beet update "
                               "takes the file's ISRCs back", item.id)
            item.store()


def main() -> None:
    parser = argparse.ArgumentParser(prog="music-backfill-ids", description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    parser.add_argument(
        "--redownload", type=int, default=0, metavar="N",
        help="with --apply: download the first N wrong versions' right versions",
    )
    parser.add_argument(
        "--albums", action="store_true",
        help="link imported albums' empty entries by disc/track and title; reads no Spotify pages",
    )
    args = parser.parse_args()
    if args.redownload and not args.apply:
        parser.error("--redownload needs --apply")
    if args.redownload and args.albums:
        parser.error("--redownload is not for --albums")
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    if args.albums:
        run_albums(apply=args.apply)
    else:
        run(apply=args.apply, redownload=args.redownload)
